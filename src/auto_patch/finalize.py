"""Post-phase-1 finalisation: elevation field, geometry repair, and
terrain-transition feature emit.

Run after ``pipeline.build_airport_pavement`` has emitted the
phase-1 geometry layout (rects, junctions, terminals, runways).

Two public entry points:

``compute_elevations_and_repair_geometry``
  * Runway FAA-profile elevations via ``elevation._compute_elevations``
    (under the legacy solver this also solves pavement; under the
    per-surface solver pavement is solved later in pipeline.py).
  * Geometry repair passes (shared-vertex enforce + overlap clip)
    to handle subdivisions / decompositions the elevation step may
    have introduced.
  * Junction-altitude reconciliation (legacy solver only).

``emit_terrain_transition_features``
  * Airport boundary ribbon, groundside pavement, boundary→DEM
    bridges; taxi/road bridges + tunnel portals when
    ``EMIT_BRIDGES_AND_TUNNELS``.
"""
from __future__ import annotations

import math
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

import O4_UI_Utils as UI
from shapely.errors import GEOSException, TopologicalError

# Narrow exception tuple for feature-emit failures (shapely +
# OSM/DEM file I/O).  Programming errors propagate so they surface
# immediately rather than being silently masked at runtime.
_GEOM_EXC = (OSError, ValueError,
             GEOSException, TopologicalError)

from .boundary import (
    _emit_airport_boundary_shape,
    _emit_boundary_dem_bridge,
)
from .bridges import (
    _emit_taxi_bridges,
    _emit_through_airport_depressed_roads,
    _emit_tunnel_portals,
    _emit_underpass_road_approaches,
    _scenery_has_bridge_objects,
)
from .config import EMIT_BRIDGES_AND_TUNNELS
from .elevation import (
    SHARED_VERTEX_CLUSTER_TOL_M,
    _compute_elevations,
    _drop_overlap_against_fixed_shapes,
    _drop_thin_orphan_slivers,
    _enforce_shared_vertex_altitudes,
    _load_airport_dem,
    _merge_sliver_junctions_into_neighbours,
    _report_within_shape_violations,
    _smooth_within_junction_adjacent_pair_grade,
    _snap_junction_altitudes_to_rect_corners,
    _split_sloped_rects_at_violations,
)
from .groundside import (
    _separate_groundside_from_airside,
    _deconflict_groundside_overlaps,
    _merge_touching_groundside,
)
from .pavement.vertices import (
    _enforce_shared_vertices,
    _push_junction_vertices_off_taxi_rect_edges,
)

if TYPE_CHECKING:
    import O4_DEM_Utils
    from shapely.geometry import Polygon
    from .apt_dat_reader import Airport
    from .layout import PavementLayout


__all__ = ["compute_elevations_and_repair_geometry",
           "deconflict_road_features",
           "emit_terrain_transition_features"]


def deconflict_road_features(layout, icao: str = "") -> None:
    """Road-feature self-deconfliction (s70 KPHX overlap storm, user
    2026-06-10).  The portal / taxi-bridge / approach emitters walk
    connected OSM ways outward with no geometric check against EACH
    OTHER — parallel carriageways < 22 m apart emit near-identical
    ramp chains / plates / decks on top of one another.  Walk every
    tunnel_ramp / retaining_wall piece in EMIT ORDER (earlier emitter
    wins: depressed plates when enabled, then portal chains, then
    bridge walls/decks):

      * a piece ≥ 85 % covered by the running union of earlier pieces
        is redundant — drop it;
      * a partially-covered FLAT piece is clipped to its uncovered
        remainder (parts ≥ 1 m² kept);
      * a partially-covered SLOPED quad keeps its small overlap
        (clipping would break the 4-corner altitude_high/low
        convention) — sub-85 % overlaps between ramp chains are
        corner kisses in practice.

    The AIRSIDE pavement (runways / rects / junctions / aprons /
    terminals) seeds the union, so road features always yield to it —
    a portal wall or ramp nicking an apron edge is clipped/dropped
    rather than overlapping.  Idempotent — called once inside the
    feature emit block and again from the pipeline tail, because the
    bridge vertex post-processing
    (``_snap_bridge_vertices_to_runway_corners`` et al.) MOVES feature
    vertices after the first run and can re-introduce small overlaps.
    """
    _AIRSIDE_SEED_ROLES = ("building", "runway", "runway_crossing",
                           "primary_parallel", "secondary_parallel",
                           "stub", "cross_connector", "junction",
                           "apron",
                           # Object-bridge law plates (feature B, round
                           # 9): road features yield to them like to
                           # pavement — the legacy tunnel-portal
                           # emitters (tunnel=yes OSM ways under the
                           # KBNA deck) dropped ramp/wall pieces INSIDE
                           # the trench box at DEM-based values, and
                           # Triangle interpolated the trench interior
                           # from them (measured: 14 stray constrained
                           # vertices at 170.0-178.3 kept the corridor
                           # off its 161.01 floor).  Gate off => the
                           # roles never exist => seed unchanged.
                           "bridge_trench", "bridge_causeway")
    try:
        from shapely.ops import unary_union as _uu
        from .layout import (BuiltShape,
                             ROLE_TUNNEL_RAMP as _R_TR,
                             ROLE_RETAINING_WALL as _R_RW)
        _run_u = None
        try:
            _terms = [s.polygon for s in layout.shapes
                      if s.role in _AIRSIDE_SEED_ROLES
                      and s.polygon is not None
                      and not s.polygon.is_empty]
            if _terms:
                _run_u = _uu(_terms)
        except _GEOM_EXC:
            _run_u = None
        _n_drop = 0
        _n_clip = 0
        # Object-derived bridge features (feature B, gate
        # O4_OBJECT_BRIDGE_TERRAIN) walk FIRST: the classifier's corridor
        # plates/approaches are measured object geometry, senior to the
        # OSM-inference emitters (portals / implied bores / connectors)
        # that fire around the same roads — earlier-wins then keeps the
        # object values and drops the inference overlaps (stage 2b: the
        # taxiway-L 161.0 m plate was 85 %-covered by earlier portal
        # pieces and silently dropped).  With the gate off no
        # ``object_bridge*`` refs exist and the order — and behaviour —
        # is byte-identical.
        _road_features = [
            s for s in layout.shapes
            if s.role in (_R_TR, _R_RW)
            and s.polygon is not None
            and not s.polygon.is_empty
            and s.polygon.area > 0]
        _object_first = (
            [s for s in _road_features
             if (s.ref or "").startswith("object_bridge")]
            + [s for s in _road_features
               if not (s.ref or "").startswith("object_bridge")])
        for s in _object_first:
            try:
                if _run_u is not None \
                        and s.polygon.intersects(_run_u):
                    _cov = (s.polygon.intersection(_run_u)
                            .area / s.polygon.area)
                    if _cov >= 0.85:
                        s.polygon = None
                        _n_drop += 1
                        continue
                    is_sloped = (s.altitude_high is not None
                                 and s.altitude_low is not None)
                    if not is_sloped and _cov > 0.0005:
                        d = s.polygon.difference(_run_u)
                        parts = [g for g in
                                 (d.geoms if hasattr(d, "geoms")
                                  else [d])
                                 if g.geom_type == "Polygon"
                                 and not g.is_empty
                                 and g.area >= 1.0]
                        if not parts:
                            s.polygon = None
                            _n_drop += 1
                            continue
                        parts.sort(key=lambda g: -g.area)
                        s.polygon = parts[0]
                        for g in parts[1:]:
                            layout.shapes.append(BuiltShape(
                                polygon=g, role=s.role,
                                ref=s.ref,
                                altitude=s.altitude))
                        _n_clip += 1
                _run_u = (s.polygon if _run_u is None
                          else _run_u.union(s.polygon))
            except _GEOM_EXC:
                continue
        if _n_drop or _n_clip:
            layout.shapes = [s for s in layout.shapes
                             if s.polygon is not None]
            UI.vprint(1,
                f"  [pav-builder] {icao}: road-feature "
                f"deconflict — {_n_drop} redundant piece(s) "
                f"dropped, {_n_clip} clipped.")
    except _GEOM_EXC:
        pass


def compute_elevations_and_repair_geometry(layout: PavementLayout, icao: str, xplane_root: str,
               apt: Airport, *,
               nodes: dict[str, tuple[float, float]],
               ways: list[tuple[str, list[str], dict[str, str]]],
               to_m: Callable[[float, float], tuple[float, float]],
               apron_candidates: list[Polygon],
               tile_dem: O4_DEM_Utils.DEM | None = None,
               current_tile_lat: int | None = None,
               current_tile_lon: int | None = None) -> None:
    """Phase-2 elevation solve + feature emit.  Mutates layout.

    ``tile_dem`` (when supplied by the tile-pipeline driver) is the
    pre-loaded ``O4_DEM_Utils.DEM`` from Ortho4XP's
    ``smooth_raster_over_airports`` step.  Threaded through so the
    Phase-2 elevation solver and the boundary-shape emit consume
    the SAME smoothed DEM that drives Ortho4XP's flattening,
    without each per-airport pass loading DEM tiles independently.

    ``current_tile_lat`` / ``current_tile_lon`` identify the tile
    being processed by the driver — used to provide correct
    ``(tile_lat, tile_lon)`` context for DEM sampling on cross-tile
    airports (the airport's anchor tile may differ from the tile
    Ortho4XP is currently generating).  Default ``None`` falls back
    to ``floor(layout.anchor)`` for direct test runs.
    """
    from .geom_guard import coverage_probe as _covp0
    _compute_elevations(
        layout, icao, xplane_root, apt,
        osm_nodes=nodes, osm_ways=ways, to_m=to_m,
        apron_candidates_m=apron_candidates,
        tile_dem=tile_dem,
        current_tile_lat=current_tile_lat,
        current_tile_lon=current_tile_lon)
    _covp0(layout, "post-compute-elev")
    # Elevation phase can subdivide junctions, decompose holed
    # polygons, and otherwise modify polygon geometry — re-run
    # the shared-vertex collapse + overlap-clip so the
    # invariants survive into the final layout.
    _enforce_shared_vertices(
        layout, tol=SHARED_VERTEX_CLUSTER_TOL_M)
    _covp0(layout, "post-shared-verts-1")
    _drop_overlap_against_fixed_shapes(layout, icao=icao)
    _covp0(layout, "post-overlap-clip")
    _enforce_shared_vertices(
        layout, tol=SHARED_VERTEX_CLUSTER_TOL_M)
    _covp0(layout, "post-shared-verts-2")
    # The overlap-clip pass introduces new vertices at
    # intersection points that may land on a taxi rect's
    # edge interior (would split the rect's altitude_high/
    # altitude_low convention at render time).  Push any
    # such vertex off — pure geometry, doesn't touch
    # elevations.
    _push_junction_vertices_off_taxi_rect_edges(layout)
    # Per user 2026-05-03: when the per-surface solver runs the
    # legacy chain of post-elevation reconciliation passes
    # (snap-to-rect-corner, shared-vertex agree, adjacent-pair
    # smoother) re-introduces the within-junction grade violations
    # the solver just fixed.  In particular,
    # ``_snap_junction_altitudes_to_rect_corners`` propagates a
    # rect's ``altitude_high``/``altitude_low`` scalars to junction
    # vertices via the legacy [high, low, low, high] convention,
    # which restores the cross-section delta the solver had
    # equalised to flat.  Skip the chain when the flag is on; the
    # solver's writeback already gives every shared bucket one
    # value, so the snaps are redundant and harmful.
    from .elevation import USE_PER_SURFACE_SOLVER
    if not USE_PER_SURFACE_SOLVER:
        # Per user 2026-04-28: junction polygon vertices that
        # coincide with a runway / sloping-rect / terminal corner
        # MUST emit that shape's altitude tag value.  Run this
        # AFTER all the shared-vertex / overlap-clip passes since
        # they can rearrange polygon coords and put node_altitudes
        # out of sync with the rect's emitted altitude tags.
        _snap_junction_altitudes_to_rect_corners(layout)
        # Per user 2026-04-28: junctions sharing a boundary vertex
        # MUST agree on its altitude.  Subdivide / clamp passes can
        # leave sub-metre disagreement at shared buckets — average
        # them so X-Plane doesn't render a tear at the seam.
        _enforce_shared_vertex_altitudes(layout)
        # Re-run the rect-corner snap after the shared-vertex
        # average, since averaging can pull a shared-with-rect
        # bucket away from the rect's tag value.
        _snap_junction_altitudes_to_rect_corners(layout)
        # Per user 2026-04-28: smooth adjacent-vertex pair grade
        # WITHIN each junction.  Above passes enforce shared-
        # vertex agreement (across polygons) and rect-corner
        # alignment (junction ↔ sloping rect), but interior
        # junction vertices can still violate 1.5 % grade against
        # their immediate ring neighbours.  Iterate adjacent-pair
        # smoothing with hard-vertex anchoring to converge those.
        _smooth_within_junction_adjacent_pair_grade(layout)
        # Re-run shared-vertex + rect-corner snaps so any seam
        # vertex the smoother nudged off-target is restored.
        _enforce_shared_vertex_altitudes(layout)
        _snap_junction_altitudes_to_rect_corners(layout)
        # Junction ring curvature smoothing (user 2026-06-15) runs in the
        # PER-SURFACE path from pipeline (after per_surface_solve), since
        # this legacy chain is skipped under USE_PER_SURFACE_SOLVER.
    # Per user 2026-04-29: merge small junction slivers into
    # adjacent larger junctions.  Polygon-with-holes
    # decomposition + post-elevation subdivisions can carve
    # off small (< 1000 m²) pieces sharing a boundary segment
    # with a much larger neighbour (HECA -10244 = 485 m²
    # adjacent to -10243 = 30 k m²).  Merge them back so
    # JOSM doesn't show two near-duplicate polygons.
    from .geom_guard import coverage_probe as _covp
    _covp(layout, "pre-sliver-merge")
    _merge_sliver_junctions_into_neighbours(layout, icao=icao)
    _covp(layout, "post-sliver-merge")
    # Per user 2026-06-12: a wedge rect whose narrow end cannot carry
    # its plane differential dissolves into the adjacent junction so
    # per-vertex node_altitudes + twist smooth the transition (KPHL
    # stub K5: 0.2 m across a 3.3 m end = 6 %).
    from .junction_repair import _absorb_wedge_rects_into_junctions
    _absorb_wedge_rects_into_junctions(layout, icao=icao)
    _covp(layout, "post-wedge-absorb")
    # Per user 2026-05-12: drop thin orphan sliver junctions that
    # form residue along a stub / parallel rect's long edge.  These
    # appear when the apt.dat row-110 pavement boundary curves
    # inward between a rect's two corners, leaving a thin strip
    # uncovered.  The strip is geometrically isolated from the
    # surrounding apron (touches only via rect-shared vertices), so
    # the merge pass above can't catch it.  Visible as a thin
    # residue strip along diagonal-stub sloping edges.
    _drop_thin_orphan_slivers(layout, icao=icao)
    _covp(layout, "post-orphan-drop")
    # Progress: end of the pre-solve repair block — the per-surface solve
    # (the bulk of the elevation phase) takes the bar from here.
    from .progress import substep as _psub
    _psub(0.15, "Solving elevations — geometry repairs done")
    # Note: _split_sloped_rects_at_violations runs from pipeline.py
    # AFTER per_surface_solve has populated altitude_high/_low on
    # rect shapes.  Calling it here (before solver) would find
    # zero candidates because rects have None altitudes here.
    # Per user 2026-05-03: when the per-surface solver is on,
    # geometry passes above (overlap clip, push-off, sliver merge)
    # may have moved or added vertices since the solver finished.
    # Re-run the solver one more time so its constraints (per-axis
    # rect grade, all-pair junction grade, cross-section flatness)
    # apply to the final geometry the OSM emit will write.
    # Per user 2026-05-03: skip the mid-pipeline per-surface solver
    # call and run the final solver AT THE END (below) after every
    # geometry pass has settled.
    # WARN summary moved to pipeline.py after the absolute final
    # per-surface solver pass — the WARN that ran here would
    # report a stale state because the post-finalize junction-rule
    # passes (widen_junctions_to_runway_corners etc.) and the
    # final solver run after this point.
    #
    # Feature emit (airport boundary ribbon, groundside pavement,
    # boundary→DEM bridges, taxi/road bridges + tunnels) moved OUT of
    # ``compute_elevations_and_repair_geometry`` into :func:`emit_terrain_transition_features` (session 51 single-solve
    # refactor).  It now runs in pipeline.py AFTER the single elevation
    # solve, so the boundary ribbon clamps against the FINAL pavement
    # profile instead of a pre-solve snapshot.


def emit_terrain_transition_features(layout: PavementLayout, icao: str, xplane_root: str, *,
                  tile_dem: O4_DEM_Utils.DEM | None = None,
                  current_tile_lat: int | None = None,
                  current_tile_lon: int | None = None) -> None:
    """Emit terrain-transition features: airport boundary ribbon,
    groundside pavement, boundary→DEM bridges, and (when
    ``EMIT_BRIDGES_AND_TUNNELS``) taxi/road bridges + tunnel portals.

    Session 51 single-solve refactor: this runs AFTER the single
    elevation solve so each feature mirrors the FINAL pavement
    altitudes (the boundary ribbon clamps to settled runway/pavement,
    bridges span the settled surface).  Previously inlined at the end
    of :func:`compute_elevations_and_repair_geometry`, where it sampled a pre-solve snapshot.
    """
    # Per user 2026-04-28: emit a 5 m-wide ribbon polygon
    # tracing the airport boundary (apt.dat row-130) with
    # per-vertex altitudes clamped to ≤ 3 % grade from the
    # nearest runway within 400 m, falling back to DEM
    # beyond.  Provides the elevation transition between
    # airport pavement and surrounding terrain.
    try:
        _lat0, _lon0 = layout.anchor
        # Use the current tile being processed (from the driver)
        # for DEM sampling context.  When ``tile_dem`` is the DEM
        # for the current tile (-13/-78 for SPLP when generating
        # that tile), ``_sample_dem`` needs ``tile_lat=-13,
        # tile_lon=-78`` to convert lat/lon → DEM local coords
        # correctly.  Anchor-derived tile coords would be wrong
        # for cross-tile airports.
        _tile_lat = (current_tile_lat if current_tile_lat is not None
                     else int(math.floor(_lat0)))
        _tile_lon = (current_tile_lon if current_tile_lon is not None
                     else int(math.floor(_lon0)))
        _dem = _load_airport_dem(_lat0, _lon0, override_dem=tile_dem)
        # Adjacent-ground grade law (Noah 2026-07-08): when ON, the
        # per-role lateral corridor law is the ONLY terrain-transition
        # model beside pavement.  The boundary ribbon was clamping
        # perimeter altitudes near runways to force-fill terrain — that
        # fights the new model, whose zone-3 free floor leaves lawful
        # cliffs as DEM.  Skip the ENTIRE ribbon emission (not just the
        # DEM bridge below); terrain near the boundary is then pure DEM
        # + the pavement-edge strip law.  The ribbon CODE stays (deleted
        # with the bridge in the final slice, after in-sim soak).
        from .config import ADJACENT_GROUND_LAW_ENABLED
        if ADJACENT_GROUND_LAW_ENABLED:
            UI.vprint(1,
                "  [pav-builder] boundary ribbon: superseded by the "
                "adjacent-ground law.")
        else:
            n_b = _emit_airport_boundary_shape(
                layout, _dem, _tile_lat, _tile_lon)
            if n_b:
                UI.vprint(1,
                    f"  [pav-builder] emitted "
                    f"{n_b} airport-boundary shape piece(s).")
        # (refactor Phase 4) Groundside pavement EMIT + apron-island
        # absorption (``_absorb_apron_enclosed_groundside``) + orphan-junction
        # reclassification (``_reclassify_groundside_orphan_junctions``) moved
        # PRE-solve (see pipeline.py): groundside is DEM-following and
        # solve-independent (the per-surface solver only grades PAVEMENT_ROLES),
        # and folding apron-enclosed groundside into aprons / re-tagging orphan
        # junctions BEFORE the solve grades the absorbed pieces in place — the
        # cliff is gone at the source instead of being relocated by a
        # post-solve merge.  Only the SEPARATION below stays post-solve so it
        # mirrors the FINAL airside geometry.
        # Enforce groundside separation (user 2026-05-22): clip every
        # groundside polygon to a clearance gap from all terminal /
        # airside pavement so it shares no node or edge with them
        # (groundside = car/building pavement at DEM elevation, distinct
        # from the graded airside).
        try:
            n_mg = _merge_touching_groundside(
                layout, _dem, _tile_lat, _tile_lon)
            if n_mg:
                UI.vprint(1,
                    f"  [pav-builder] merged {n_mg} touching groundside "
                    f"piece(s) into one surface.")
        except _GEOM_EXC:
            pass
        try:
            from .geom_guard import coverage_probe as _covp2
            _covp2(layout, "pre-groundside-sep")
            n_sep = _separate_groundside_from_airside(
                layout, _dem, _tile_lat, _tile_lon,
                preserve_field=True)
            _covp2(layout, "post-groundside-sep")
            if n_sep:
                UI.vprint(1,
                    f"  [pav-builder] separated {n_sep} groundside "
                    f"polygon(s) from terminal/airside (clearance gap).")
        except _GEOM_EXC as _exc:
            # Keep the build alive (a geometry failure here must not kill the
            # tile), but never SILENTLY — a swallowed failure leaves
            # groundside overlapping airside (e.g. a building still enclosed
            # by a groundside lot).  Surface it so the cause is visible.
            UI.vprint(1,
                f"  [pav-builder] WARNING: groundside↔airside separation "
                f"raised {type(_exc).__name__}: {_exc} — groundside may "
                f"still overlap terminal/airside for this tile.")
        # Groundside↔groundside deconfliction: clip overlapping
        # groundside pieces so no two share interior area (the
        # separation above only handles groundside↔airside).
        try:
            n_gd = _deconflict_groundside_overlaps(
                layout, _dem, _tile_lat, _tile_lon)
            if n_gd:
                UI.vprint(1,
                    f"  [pav-builder] deconflicted {n_gd} overlapping "
                    f"groundside polygon(s).")
        except _GEOM_EXC:
            pass
        # Chord grade limit — LAST groundside-altitude writer: pull every
        # groundside field to the largest 4 %-Lipschitz field ≤ DEM over
        # straight-line pairs (the within-shape validator metric; the
        # ring-ramp limit alone leaves hillside pieces >4 % across the
        # interior).  Must follow the separation above, which re-derives
        # DEM altitudes for clipped results.
        try:
            from .config import GROUNDSIDE_MAX_GRADE as _GS_CAP
            from .groundside import _grade_limit_groundside_chords
            n_gl = _grade_limit_groundside_chords(layout)
            if n_gl:
                UI.vprint(1,
                    f"  [pav-builder] chord-grade-limited {n_gl} "
                    f"groundside polygon(s) to "
                    f"{100 * _GS_CAP:.0f}%.")
        except _GEOM_EXC:
            pass
        # Then emit DEM-bridge polygons inside the boundary
        # wherever the clamped boundary altitude differs from
        # raw DEM by > 5 m (per user 2026-04-28).  Kept POST-solve:
        # the runway-distance clamp anchors to ALL airside pavement
        # (incl. aprons/taxiways, whose altitudes are only known after
        # the solve), so the bridge PLACEMENT (|clamp − DEM| > 5 m) is
        # genuinely solve-dependent — see the refactor Phase 5 note.
        try:
            # Adjacent-ground grade law (slice 3): when ON, the lateral
            # corridor law SUPERSEDES the boundary→DEM bridge (the law's
            # graded strips carry the pavement-to-terrain transition, and
            # zone-3's free floor leaves lawful cliffs alone instead of
            # force-filling them).  The at-DEM boundary RIBBON path is
            # untouched; the bridge machinery is only SKIPPED here (deleted
            # in the final slice, after in-sim soak).
            from .config import ADJACENT_GROUND_LAW_ENABLED
            n_br = (0 if ADJACENT_GROUND_LAW_ENABLED
                    else _emit_boundary_dem_bridge(
                        layout, _dem, _tile_lat, _tile_lon))
            if n_br:
                UI.vprint(1,
                    f"  [pav-builder] emitted "
                    f"{n_br} boundary→DEM bridge "
                    f"polygon(s).")
        except _GEOM_EXC:
            pass
        # TODO(bridges): re-enable bridge / tunnel emission
        # after core pavement geometry is stable + refactored.
        # See ``EMIT_BRIDGES_AND_TUNNELS`` at module top for
        # the full to-do list.  Each gated call must carve its
        # footprint out of overlapping airside / groundside
        # pavement before emitting, or
        # ``test_no_self_overlap`` will fail.
        if EMIT_BRIDGES_AND_TUNNELS:
            # Per user 2026-04-29 (KPHX Sky Harbor Blvd): when
            # a public road passes under any airport bridge,
            # its ENTIRE inside-airport stretch must be at
            # apt_elev − 8 m, not just at the bridge crossings.
            # Run BEFORE _emit_tunnel_portals so the latter can
            # skip OSM ways already depressed here.
            _depressed_way_ids: set = set()
            from .config import EMIT_DEPRESSED_ROADS
            # (user 2026-06-10) Through-airport trenches OFF by
            # default: the tunnel-portal emitter below handles the
            # road's descent at each portal; the tunnel-tagged stretch
            # stays under the airport surface.  Empty exclusion set →
            # the portal emitter sees every tunnel way.
            _depressed_ok = not EMIT_DEPRESSED_ROADS
            try:
                if EMIT_DEPRESSED_ROADS:
                    n_dep, _depressed_way_ids = (
                        _emit_through_airport_depressed_roads(
                            layout, _dem, _tile_lat, _tile_lon,
                            xplane_root=xplane_root, icao=icao))
                    _depressed_ok = True
                    if n_dep:
                        UI.vprint(1,
                            f"  [pav-builder] emitted "
                            f"{n_dep} through-airport depressed "
                            f"road segment(s).")
            except _GEOM_EXC as exc:
                # The depressed-road emit may have already created
                # depressed segments for SOME OSM ways before
                # raising, but we no longer know which (the returned
                # excluded set is lost).  Running _emit_tunnel_portals
                # with an empty exclusion set would double-emit ramps
                # on those partially-handled ways.  Skip the tunnel
                # portals rather than corrupt geometry; log loudly so
                # the failure is visible (was a silent `pass`).
                _depressed_way_ids = set()
                UI.vprint(1,
                    f"  [pav-builder] WARN: {icao}: through-airport "
                    f"depressed-road emit failed mid-pass ({exc}); "
                    f"skipping tunnel-portal emit to avoid double-"
                    f"emitting ramps on partially-handled OSM ways.")
            # Per user 2026-04-29: re-enable tunnel-portal
            # emission.  For each big-roads tunnel crossing the
            # airport boundary, emit a sloped ramp + 3
            # retaining walls that transition the road from
            # outside-DEM elevation down to airport-elevation
            # − 6 m at the portal.  Subtracts tunnel zones from
            # the boundary ribbon and DEM-bridge polygons so
            # they don't overlap.  OSM way ids handled by the
            # through-airport depressed-road emit are excluded
            # so we don't double-emit on Sky-Harbor-style
            # multi-bridge crossings.  Only runs when the
            # depressed-road emit completed cleanly, so the
            # exclusion set is trustworthy.
            if _depressed_ok:
                _pre_tun_dump = os.environ.get("O4_DUMP_PRE_TUNNEL_LAYOUT")
                if _pre_tun_dump:
                    import pickle as _pk
                    try:
                        with open(_pre_tun_dump, "wb") as _fh:
                            _pk.dump(layout, _fh)
                    except Exception:
                        pass
                try:
                    n_tun = _emit_tunnel_portals(
                        layout, _dem, _tile_lat, _tile_lon,
                        excluded_way_ids=_depressed_way_ids)
                    if n_tun:
                        UI.vprint(1,
                            f"  [pav-builder] emitted "
                            f"{n_tun} tunnel-portal cluster(s) "
                            f"(ramp + walls along approach).")
                except _GEOM_EXC as exc:
                    # A swallowed failure here silently loses EVERY
                    # tunnel at the airport (KDFW: 14 portal clusters
                    # gone with no trace) — log loudly.
                    UI.vprint(1,
                        f"  [pav-builder] WARN: {icao}: tunnel-portal "
                        f"emit FAILED ({type(exc).__name__}: {exc}) — "
                        f"tunnels not emitted.")
            # Per user 2026-04-29: emit retaining walls along
            # taxi bridges (KBNA Taxiway A, KPHX taxis over
            # Sky Harbor Blvd) and road-following approach
            # shapes that descend from outside-DEM down to
            # apt_elev − 8 m under the bridge.  When the
            # scenery pack already includes 3D bridge OBJs
            # (KBNA), the user wants the road to cut straight
            # through and let the OBJ be the bridge — skip our
            # walls and emit the under-bridge flat polygon.
            # When it doesn't (KPHX), keep the terrain flat
            # for the taxiway and only ramp the road up to the
            # bridge edge — emit walls + skip under-bridge.
            try:
                _scn_bridge = _scenery_has_bridge_objects(layout)
            except _GEOM_EXC:
                _scn_bridge = False
            try:
                n_brg = _emit_taxi_bridges(
                    layout, _dem, _tile_lat, _tile_lon,
                    scenery_has_bridge_objects=_scn_bridge)
                if n_brg:
                    UI.vprint(1,
                        f"  [pav-builder] emitted "
                        f"{n_brg} taxi-bridge wall pair(s).")
                elif _scn_bridge:
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: scenery has "
                        f"3D bridge OBJ(s); skipping wall "
                        f"emission.")
            except _GEOM_EXC:
                pass
            try:
                n_app = _emit_underpass_road_approaches(
                    layout, _dem, _tile_lat, _tile_lon,
                    scenery_has_bridge_objects=_scn_bridge)
                if n_app:
                    UI.vprint(1,
                        f"  [pav-builder] emitted underpass-"
                        f"road approaches for {n_app} "
                        f"surface(s)"
                        f"{' (cut through under bridge OBJ)' if _scn_bridge else ' (ramp up to bridge edge)'}.")
            except _GEOM_EXC:
                pass
            deconflict_road_features(layout, icao)
            # Portal terrain airside raise (user ruling 2026-07-17):
            # crown + collar rise to the surrounding SOLVED airside
            # level where it stands above the object-derived crown —
            # runs after the solve and the approach emission, before
            # final_grade_projection / adjacent-ground bands, and
            # re-records the raised solver pins.
            try:
                from .bridges import raise_portal_terrain_to_airside
                raise_portal_terrain_to_airside(layout)
            except _GEOM_EXC:
                pass
            # Round 9 (user ruling): the written patch must hold
            # strictly NON-OVERLAPPING rings over the object-bridge
            # plates — cut every road-feature remnant against them
            # (gate off ⇒ no plates ⇒ no-op).
            from .bridges import enforce_bridge_plate_exclusivity
            enforce_bridge_plate_exclusivity(layout)
    except _GEOM_EXC:
        pass
