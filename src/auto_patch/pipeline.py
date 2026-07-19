"""End-to-end orchestration of the airport pavement builder.

Sequences the phase-1 (geometry + role) and phase-2 (elevation)
passes by calling out into the focused ``O4_Pavement_*`` modules:

* OSM tile + airport extraction.
* apt.dat selection (best-of-OSM vs custom-pack heuristics).
* Phase-1 construction → ``O4_Pavement_Rects``,
  ``O4_Pavement_Centerlines``, ``O4_Pavement_Stubs``,
  ``O4_Pavement_Junctions``, ``O4_Pavement_Terminals``.
* Phase-2 elevation → ``O4_Pavement_Elevation``.
* Feature emit → ``O4_Pavement_Boundary``,
  ``O4_Pavement_Groundside``, ``O4_Pavement_Bridges``.
* Output via ``PavementLayout.to_osm`` (in ``O4_Pavement_Layout``).

Public API:

    build_airport_pavement(icao, xplane_root, *, compute_elevations=True,
                            taxiway_data=None, tile_dem=None,
                            airport_boundary=None)

Backward-compat shim ``O4_Airport_Pavement_Builder`` re-exports
``build_airport_pavement`` (and a few helpers used by other
modules) so existing call sites keep working.
"""
from __future__ import annotations

import math
import os
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import (
    linemerge, nearest_points, transform as shp_transform, unary_union)

# Narrow exception tuple for shapely / numeric-geometry failure
# modes + file I/O.  Programming errors propagate so they surface
# immediately rather than being silently masked at runtime.
_GEOM_EXC = (OSError, ValueError,
             GEOSException, TopologicalError)

import O4_File_Names as FNAMES
import O4_UI_Utils as UI

from . import apt_dat_reader as APR
from .pavement import strips as PS


# ──────────────────────────────────────────────────────────────────
# Constants (re-exported from O4_Pavement_Config + O4_Pavement_Layout)
# ──────────────────────────────────────────────────────────────────
from .config import (
    MIN_SEGMENT_LEN_M,
    LOAD_DSF_PAVEMENT,
    DSF_BUILDINGS,
    DSF_BUILDING_OSM_OVERLAP_FRAC,
    TERM_BRIDGE_GROUPING,
    TERMINAL_SIMPLIFY_TOL_M,
    RUNWAY_APRON_AREA_RATIO,
    ABSORB_RUNWAY_IN_APRON,
    OSM_SMALL_ROAD_HIGHWAY_TYPES,
    SERVICE_ROAD_WIDTH_M,
    MIN_SERVICE_STRIP_LEN_M,
    SERVICE_ROAD_PAVEMENT_NEAR_M,
    ENABLE_SERVICE_ROADS,
    SERVICE_ROAD_CARVE,
    ENABLE_DISCOVERED_TAXIWAYS,
    HANGAR_PADS as HANGAR_PADS_GATE,
)
from .layout import (
    BuiltShape,
    PavementLayout,
    R_EARTH,
    ROLE_RUNWAY,
    ROLE_STUB,
    ROLE_BUILDING,
    _airport_anchor,
    _projection,
)


# ──────────────────────────────────────────────────────────────────
# Data model (re-exported from O4_Pavement_Layout)
# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# Input loaders
# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# OSM cache + apt.dat selection helpers
# ──────────────────────────────────────────────────────────────────
from .osm_load import (
    _load_osm_airports,
    _load_osm_big_roads,
    _load_osm_small_roads,
    _pick_best_apt_dat_against_osm,
)
from .pavement.service_roads import build_service_road_network
from . import finalize, junction_emit


# ──────────────────────────────────────────────────────────────────
# Meter-space projection (re-exported from O4_Pavement_Layout)
# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# Runway rects, crossings, shoulders (re-exported from
# O4_Pavement_Runways)
# ──────────────────────────────────────────────────────────────────
from .pavement.runways import (
    _detect_runway_shoulders,
    _detect_runway_border_strip_shoulders,
    _detect_runway_shoulder_extent,
    _runway_rect_m,
    _widen_runway_rect,
)


# ──────────────────────────────────────────────────────────────────
# Pavement union helpers (re-exported from pavement.union_helpers)
# ──────────────────────────────────────────────────────────────────
from .pavement.union_helpers import (
    _merge_near_touching,
)


# ──────────────────────────────────────────────────────────────────
# Stub residue / runway-bridge helpers
# (re-exported from pavement.stubs)
# ──────────────────────────────────────────────────────────────────


from .pavement.absorption import (
    _drop_primary_parallels_embedded_in_pavement,
)


# ──────────────────────────────────────────────────────────────────
# Tunable runway / parallel-taxiway centerline pull-backs (session 56).
# Exposed as module globals so the centerline output can be regenerated
# with different pull-backs for review.  Defaults = historical values.
# ──────────────────────────────────────────────────────────────────
_RWY_JUNCTION_BUFFER_M = 25.0   # runway-end pull-back (perpendicular)
_RWY_DIAG_BUFFER_M = 25.0       # runway-end pull-back (diagonal)
_PARALLEL_BUFFER_M = 15.0       # parallel-taxiway-end pull-back


# ──────────────────────────────────────────────────────────────────
# Airside node-unification (refactor Phases 6+7)
# ──────────────────────────────────────────────────────────────────

# Post-solve-emitted FEATURE roles (terrain transitions / clearance), which
# conform TO the frozen airside without the airside ever receiving a vertex.
# (boundary_dem_bridge + surface_clearance are overlay-exempt in conformance.)
_POSTSOLVE_FEATURE_OWNER_ROLES = frozenset({
    "boundary", "groundside_pavement", "tunnel_ramp", "retaining_wall",
})


def _unify_airside_geometry(layout, icao: str) -> None:
    """Settle the airside pavement node-set into a CONFORMING partition:
    re-connect discovered lane dead-ends, weld near-coincident airside
    vertices to one canonical coordinate, insert every shared-boundary
    vertex (full conformance), then the final near-corner / neighbour-corner
    snaps.  Adjacent airside shapes end up sharing identical vertices along
    every common edge — no T-junction slivers, no coincident-but-separate
    vertex pairs.

    Refactor Phases 6+7: this runs PRE-solve so the solver sees the FINAL
    node-set and writes ONE altitude per shared bucket — eliminating the
    post-solve coincident-vertex CLIFFS that arose when weld/conformance
    snapped vertices coincident AFTER the solver graded them to independent
    elevations (the HECA #291↔#371 class).  Pure geometry — no altitude
    dependency — so it is safe before any altitude is assigned.
    """
    from .canonical_points import weld_layout_vertices
    from .conformance import enforce_conformance
    from .junction_repair import (
        _snap_near_corner_vertices_to_rect_corners,
        _share_neighbour_corners_into_junctions)
    from .layout import (
        ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
        ROLE_CROSS_CONNECTOR, ROLE_JUNCTION, ROLE_RUNWAY,
        ROLE_RUNWAY_CROSSING, ROLE_APRON, ROLE_BUILDING)

    # Re-connect discovered (TX) lane dead-ends pulled away from their
    # residue junction (SPJC TX15), extending the junction back onto the
    # lane's end corners so the weld + conformance below share the vertices.
    if ENABLE_DISCOVERED_TAXIWAYS:
        from .junction_repair import (
            _connect_discovered_lane_dead_ends_to_junctions)
        _connect_discovered_lane_dead_ends_to_junctions(layout, icao=icao)

    # A junction EDGE that grazes past a rect/runway CORNER with no
    # junction vertex nearby never shares a node with the rect (the
    # vertex-based push/snap machinery can't see it) — route the edge
    # THROUGH the corner.  Runs here, at final pre-solve geometry, because
    # the graze often only exists after the junction-repair / overlap-clip
    # passes (the Phase-1 call in elevation.py catches the early cases).
    from .pavement.vertices import (
        _insert_junction_corners_into_grazing_apron_edges,
        _insert_rect_corners_into_grazing_junction_edges)
    _insert_rect_corners_into_grazing_junction_edges(layout)
    _insert_junction_corners_into_grazing_apron_edges(layout)

    # Weld near-coincident airside vertices to one fresh canonical
    # coordinate so a rect corner and the junction vertex beside it become a
    # single point (the conformance below then has only genuine T-junctions
    # left).  ROLE_BUILDING included so a terminal's boundary vertices weld
    # 1:1 with the surrounding apron's (one solver node, no tilt/wall).
    _weld_roles = {ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
                   ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
                   ROLE_RUNWAY, ROLE_RUNWAY_CROSSING, ROLE_APRON,
                   ROLE_BUILDING}
    n_welded = weld_layout_vertices(layout, _weld_roles)
    if n_welded:
        UI.vprint(1,
            f"  [pav-builder] {icao}: pre-solve welded shared vertices in "
            f"{n_welded} airside shape(s).")

    # Full conformance: insert every neighbour vertex that lies on a shape's
    # edge so the partition is conforming.  At this point the only non-airside
    # shapes present are groundside (separated post-solve); the boundary
    # ribbon / DEM bridge / clearance are emitted post-solve and conformed to
    # the (now frozen) airside by the one-sided post-solve feature conformance.
    n_shapes, n_verts = enforce_conformance(layout)
    if n_verts:
        UI.vprint(1,
            f"  [pav-builder] {icao}: pre-solve conformance — inserted "
            f"{n_verts} shared-boundary vertex(es) into {n_shapes} shape(s).")

    # PRE-SOLVE EDGE DENSIFY (user in-sim finding 2026-07-09): a
    # construction-born over-long pavement edge (CYXY: a 1,279 m
    # junction chord) gives the SOLVER nothing to hold the edge
    # profile with — the mesh then interpolates the pavement between
    # far-apart nodes and it sags against the graded strips beside it.
    # Inserted here the new vertices are solver nodes: the edge is
    # LAW-solved, not lerped.  60 m spacing matches the emit
    # decimators' MAX_CHORD, so the nodes survive to the mesh.
    from .conformance import densify_long_edges
    from .clearance import _AIRSIDE_PAVEMENT_ROLES as _DENSIFY_ROLES
    n_dense = densify_long_edges(layout, _DENSIFY_ROLES, 60.0)
    if n_dense:
        UI.vprint(1,
            f"  [pav-builder] {icao}: pre-solve edge densify — "
            f"inserted {n_dense} vertex(es) on over-60 m pavement "
            f"edges.")

    # FINAL near-corner snap: a non-rect vertex left on a sloped rect's edge
    # near a corner is snapped onto the corner so the two SHARE it; companion
    # share-neighbour-corners handles the 0.10–0.5 m junction case.
    _snap_near_corner_vertices_to_rect_corners(layout, icao=icao)
    _share_neighbour_corners_into_junctions(layout, icao=icao)


# Defect 4a (KBNA 2026-07-15) ring-needle collapse thresholds.  A needle is
# a ring apex whose interior angle is below ``_NEEDLE_ANGLE_DEG`` while BOTH
# bounding edges are longer than ``_NEEDLE_MIN_EDGE_M`` — a construction
# artifact (the slice/weld/conformance chain folding a long thin tongue to a
# near-collinear spike, e.g. KBNA junction 289's 2.8° apex between 48.6 m and
# 57.6 m edges), NOT real geometry.  The min-edge guard protects a genuine
# sharp corner built from short segments (a real taper toe carries a short
# edge); only wide-edged spikes with no plausible physical width are dropped.
#
# WIDENED 2026-07-16 (KBNA Donelson round 8): 5°/10 m left surviving spikes at
# 7.5°/7.6°/9.7° (edges 48/21 m) on junctions 289/290 and 4.4-14.9° apexes on
# the adjacent-ground bands that inherit those rings.  A 10°/8 m band is still
# firmly a degenerate spike: a 10° apex between two 8 m edges encloses only
# 0.5·8·8·sin10° ≈ 5.6 m² and is < 1.4 m wide at its base — no plausible
# real-pavement width, so no coverage is lost by collapsing it (the true
# pavement is recovered upstream by the shared-vertex weld heal in
# pavement.vertices, not by keeping these spikes).  Bands inherit the pavement
# rings, so collapsing the pavement spike dissolves the band spike with it.
_NEEDLE_ANGLE_DEG = 10.0
_NEEDLE_MIN_EDGE_M = 8.0
# A needle collapse deletes the apex, cutting the chord between its
# neighbours — the area removed is the apex triangle.  A genuine
# construction spike encloses almost nothing (KBNA 289/290's 7.5-9.7° apexes
# between 48/21 m edges remove ≈ 85 m²), but the SAME angle/edge test also
# matches the sharp TIP of a real (if thin) pavement wedge, where deleting the
# apex would carve out live pavement (KBNA Donelson: a 159 m² tip at the
# owner's spot).  Cap the CUMULATIVE per-ring removal at this floor — the same
# floor _record_airside_drop treats as a real drop — so artifact spikes still
# dissolve while no ring loses real pavement to the collapse (keeps the
# build-time airside-drop counter at zero).
_NEEDLE_MAX_DROP_AREA_M2 = 100.0


def _collapse_ring_needles(coords_open, na_open):
    """Drop degenerate needle apexes from an OPEN ring (no closing repeat).

    Iterates to a fixed point (nested spikes / a newly-exposed apex after a
    drop): at each vertex whose two bounding edges both exceed
    ``_NEEDLE_MIN_EDGE_M`` and whose interior angle is below
    ``_NEEDLE_ANGLE_DEG``, delete the apex (and its aligned altitude) —
    UNLESS doing so would push the cumulative area removed from this ring
    past ``_NEEDLE_MAX_DROP_AREA_M2`` (a real-pavement wedge tip, not a
    zero-area artifact spike; kept so no coverage is lost).
    Returns ``(coords_open, na_open, n_dropped)``; ``na_open`` may be
    ``None`` (geometry-only)."""
    import math as _math
    angle_deg = _NEEDLE_ANGLE_DEG
    min_edge = _NEEDLE_MIN_EDGE_M
    max_drop = _NEEDLE_MAX_DROP_AREA_M2
    coords = list(coords_open)
    na = list(na_open) if na_open is not None else None
    dropped = 0
    area_removed = 0.0
    changed = True
    while changed and len(coords) > 3:
        changed = False
        n = len(coords)
        for i in range(n):
            ax, ay = coords[(i - 1) % n]
            bx, by = coords[i]
            cx, cy = coords[(i + 1) % n]
            v1x, v1y = ax - bx, ay - by
            v2x, v2y = cx - bx, cy - by
            n1 = _math.hypot(v1x, v1y)
            n2 = _math.hypot(v2x, v2y)
            if n1 <= min_edge or n2 <= min_edge:
                continue
            cosv = max(-1.0, min(1.0, (v1x * v2x + v1y * v2y) / (n1 * n2)))
            ang = _math.degrees(_math.acos(cosv))
            if ang < angle_deg:
                # Apex-triangle area (the pavement the chord cut removes).
                apex_area = 0.5 * abs(v1x * v2y - v1y * v2x)
                if (area_removed + apex_area
                        > max_drop):
                    continue          # real-pavement tip — keep it
                del coords[i]
                if na is not None:
                    del na[i]
                dropped += 1
                area_removed += apex_area
                changed = True
                break
    return coords, na, dropped


def _dedup_coincident_ring_vertices(layout, icao: str, tol_m: float = 0.05,
                                    collapse_needles: bool = False):
    """Drop consecutive coincident exterior-ring vertices (zero-length
    edges) from every shape, keeping ``node_altitudes`` aligned.

    ``collapse_needles`` (defect 4a; passed True at the PRE-SOLVE call so
    the collapse is baked before the solver/validator read the ring): also
    drop degenerate ring-needle apexes (``_collapse_ring_needles``) from
    AIRSIDE shapes.  Off (post-solve idempotent copy) it is a pure
    zero-length dedup, byte-identical to before.

    The spine polygonize + weld/conformance can leave a vertex repeated at
    the same coordinate (SPJC: 44 zero-length edges).  X-Plane / Triangle4XP
    triangulate a zero-length edge into a degenerate (near-zero-area) sliver
    that renders as a STRETCHED / distorted texture.  Removing the duplicate
    is geometry-neutral (the kept vertex sits at the same spot, same
    altitude) so it cannot create a T-junction or move a seam — safe to run
    just before the final conformance check / emit.  Returns the count of
    shapes cleaned."""
    import math as _math
    from shapely.geometry import Polygon as _Poly
    from .clearance import _AIRSIDE_PAVEMENT_ROLES as _AIRSIDE_ROLES
    n_fixed = 0
    n_needle_shapes = 0
    n_needle_total = 0
    for s in layout.shapes:
        p = getattr(s, "polygon", None)
        if p is None or p.is_empty or p.geom_type != "Polygon":
            continue
        _area_before = p.area
        coords = list(p.exterior.coords)            # includes closing repeat
        if len(coords) < 4:
            continue
        na = s.node_altitudes
        # node_altitudes aligns 1:1 with exterior.coords (closing repeat) in
        # the canonical form; only dedup-with-altitude when that holds, else
        # dedup geometry only (flat / high-low shapes carry no per-vertex
        # list to misalign).
        aligned = na is not None and len(na) == len(coords)
        na_list = list(na) if aligned else None
        new_coords = [coords[0]]
        new_na = [na_list[0]] if aligned else None
        for k in range(1, len(coords)):
            px, py = new_coords[-1]
            x, y = coords[k]
            if _math.hypot(x - px, y - py) < tol_m:
                continue                            # skip duplicate
            new_coords.append((x, y))
            if aligned:
                new_na.append(na_list[k])
        # OUT-AND-BACK SPIKES (user 2026-07-04, CYUL apron #233): a ring
        # that visits a far vertex and RETURNS to the same point
        # (…A → S → A…) carries a zero-area needle — the "node out in
        # the grass" 251 m from its shape.  Remove the spike tip and the
        # returning duplicate (repeat until stable: nested spikes).
        changed_spike = True
        while changed_spike and len(new_coords) >= 4:
            changed_spike = False
            m = len(new_coords) - 1                 # closing repeat at [-1]
            for k in range(1, m):
                ax, ay = new_coords[k - 1]
                bx, by = new_coords[(k + 1) % m]
                if _math.hypot(ax - bx, ay - by) < tol_m:
                    del new_coords[k:k + 2]
                    if aligned:
                        del new_na[k:k + 2]
                    changed_spike = True
                    break
        # RING-NEEDLE COLLAPSE (defect 4a): drop degenerate wide-edged spike
        # apexes from airside shapes, pre-solve.  Runs on the OPEN ring so the
        # apex's aligned altitude is dropped with it.
        n_needle = 0
        if (collapse_needles
                and getattr(s, "role", None) in _AIRSIDE_ROLES
                and len(new_coords) >= 4):
            has_close = new_coords[0] == new_coords[-1]
            open_coords = new_coords[:-1] if has_close else list(new_coords)
            open_na = None
            if aligned:
                open_na = new_na[:-1] if has_close else list(new_na)
            open_coords, open_na, n_needle = _collapse_ring_needles(
                open_coords, open_na)
            if n_needle:
                new_coords = list(open_coords) + [open_coords[0]]
                if aligned:
                    new_na = list(open_na) + [open_na[0]]
        if len(new_coords) == len(coords) and n_needle == 0:
            continue                                # nothing removed
        # keep the ring closed
        if new_coords[0] != new_coords[-1]:
            new_coords.append(new_coords[0])
            if aligned:
                new_na.append(new_na[0])
        if len(new_coords) < 4:
            continue                                # would degenerate
        try:
            np_ = _Poly(new_coords, [list(r.coords) for r in p.interiors])
            if not np_.is_valid or np_.is_empty:
                continue
        except Exception:
            continue
        # A needle collapse should only ever shave a degenerate spike (a
        # 10° apex between 8 m edges encloses < 6 m²); if a single shape
        # loses more than the airside-drop floor to it, that is real
        # pavement, not an artifact — record it as a loud build-time verify
        # drop (must read ZERO on a healthy build).
        if n_needle and (_area_before - np_.area) > 100.0:
            from .pavement.vertices import _record_airside_drop
            _record_airside_drop(
                layout, s, p, _area_before - np_.area, "needle-collapse")
        s.polygon = np_
        if aligned:
            s.node_altitudes = new_na
        n_fixed += 1
        if n_needle:
            n_needle_shapes += 1
            n_needle_total += n_needle
    if n_fixed:
        UI.vprint(1,
            f"  [pav-builder] {icao}: removed zero-length edge(s) "
            f"(duplicate ring vertices) from {n_fixed} shape(s).")
    if n_needle_total:
        UI.vprint(1,
            f"  [pav-builder] {icao}: collapsed {n_needle_total} degenerate "
            f"ring needle(s) (<{_NEEDLE_ANGLE_DEG:.0f} deg apex, edges "
            f">{_NEEDLE_MIN_EDGE_M:.0f} m) from {n_needle_shapes} airside "
            f"shape(s).")
    return n_fixed


def _admit_dsf_building_footprint(
        outer_ring_lonlat,
        hole_rings_lonlat,
        to_meters_transform,
        airport_bounding_box_meters,
        boundary_gate_meters,
        building_polygons_meters) -> bool:
    """Admit one DSF building ring into the DSF building pool.

    The single downstream path shared by every DSF building source
    (``.fac`` facades / ``.agp`` hangars via ``read_dsf_buildings``, and
    OBJ8 structure footprints via ``read_dsf_object_buildings``):
    Polygon, ``buffer(0)`` repair, to-metres transform, bounding-box
    reject, boundary-CENTROID gate (keep the whole footprint rather than
    clipping a building that grazes the boundary), then append to
    ``building_polygons_meters``.  Returns True when the footprint was
    appended.
    """
    try:
        building_polygon_lonlat = Polygon(
            [(lon, lat) for (lon, lat) in outer_ring_lonlat],
            [[(lon, lat) for (lon, lat) in h]
             for h in hole_rings_lonlat if len(h) >= 3],
        )
        if not building_polygon_lonlat.is_valid:
            building_polygon_lonlat = building_polygon_lonlat.buffer(0)
        if (building_polygon_lonlat.is_empty
                or building_polygon_lonlat.geom_type != "Polygon"):
            return False
        building_polygon_meters = shp_transform(
            to_meters_transform, building_polygon_lonlat)
        if (building_polygon_meters.is_empty
                or building_polygon_meters.geom_type != "Polygon"):
            return False
        if airport_bounding_box_meters is not None:
            (bounds_min_x, bounds_min_y,
             bounds_max_x, bounds_max_y) = building_polygon_meters.bounds
            if (bounds_max_x < airport_bounding_box_meters[0]
                    or bounds_min_x > airport_bounding_box_meters[2]
                    or bounds_max_y < airport_bounding_box_meters[1]
                    or bounds_min_y > airport_bounding_box_meters[3]):
                return False
        if boundary_gate_meters is not None:
            try:
                if not boundary_gate_meters.contains(
                        building_polygon_meters.centroid):
                    return False
            except _GEOM_EXC:
                return False
        building_polygons_meters.append(building_polygon_meters)
        return True
    except _GEOM_EXC:
        return False


def _collect_dsf_object_building_footprints(
        dsf_path, xplane_root, admit_footprint) -> int:
    """Phase 1 of the DSF object integration: OBJ8 structure footprints
    join the DSF building pool beside the ``.fac`` facades, through the
    IDENTICAL downstream path (``admit_footprint`` =
    ``_admit_dsf_building_footprint`` with the caller's gates bound).
    Role ``"object"`` then flows through ``_cluster_dsf_building_facades``
    and ``_combine_building_sources`` exactly like a facade — additive,
    no overlap predicate (ruling R4; spec section 2.3).

    Gated on ``DSF_OBJECT_BUILDINGS`` here (function-local import so
    tests can monkeypatch the flag); the caller has already gated on
    ``DSF_BUILDINGS`` (the object source shares the building path).
    Returns the number of footprints admitted.
    """
    from .config import DSF_OBJECT_BUILDINGS
    if not DSF_OBJECT_BUILDINGS:
        return 0
    from . import dsf_reader as _DSFR
    admitted = 0
    for outer_ring, hole_rings, _role in _DSFR.read_dsf_object_buildings(
            dsf_path, xplane_root=xplane_root):
        if len(outer_ring) < 3:
            continue
        if admit_footprint(outer_ring, hole_rings):
            admitted += 1
    return admitted


# ──────────────────────────────────────────────────────────────────
# Top-level builder
# ──────────────────────────────────────────────────────────────────

def build_airport_pavement(icao: str, xplane_root: str,
                            *,
                            compute_elevations: bool = True,
                            taxiway_data=None,
                            tile_dem=None,
                            airport_boundary=None,
                            current_tile_lat=None,
                            current_tile_lon=None,
                            ) -> PavementLayout:
    """Build the complete role-classified layout for ``icao``.

    The layout is ready to compare against a target OSM via
    ``tools/compare_target.py``.

    When ``compute_elevations`` is True (default), a Phase-2
    elevation pass runs at the end:
      * Single runway shapes are replaced with per-100m segmented
        runway rects produced by the legacy CIFP+DEM generator.
      * Taxi rects get ``altitude_high``/``altitude_low`` tags
        from DEM sampling at axis endpoints, anchored to the
        adjacent runway segment when within 30 m.
      * Terminal pads get ``altitude`` from the DEM at centroid.
      * Junctions, buildings, aprons are left un-elevated (X-Plane
        triangulator interpolates them from neighbouring shared
        vertices).

    Optional integration parameters (all default to ``None`` so
    standalone callers — ``tools/build_target_osm.py``, the test
    harness — work unchanged):

      * ``taxiway_data``: per-airport list from
        ``osm_aeroway.extract_taxiway_info``.  Threaded through to
        Pipeline's centerline-union helper so reclassification /
        downstream consumers can see OSM taxiway centerlines that
        didn't survive into rects.
      * ``tile_dem``: pre-loaded ``O4_DEM_Utils.DEM`` for the
        containing tile (typically Ortho4XP's smoothed DEM after
        ``smooth_raster_over_airports``).  When supplied, the
        Phase-2 elevation solver and boundary-shape emit consume
        this DEM directly instead of calling
        ``_load_airport_dem`` per-airport — avoids redundant DEM
        loads in the tile-pipeline driver and keeps auto_patch's
        elevation field aligned with Ortho4XP's smoothed terrain.
      * ``airport_boundary``: optional pre-computed airport
        boundary (``dico_airports[icao]['boundary']`` from
        Ortho4XP's ``update_airport_boundaries``).  Currently
        reserved — the parameter is plumbed through but the
        boundary-shape emit still derives its outline from
        apt.dat row-130 until the source-of-truth question is
        decided.
    """
    from .progress import for_build as _progress_for_build
    _progress = _progress_for_build(icao, compute_elevations=compute_elevations)
    _progress.step()  # [1] Loading apt.dat & runway geometry

    # O4_FORCE_APT_DAT overrides source selection (e.g. compare Global Airports
    # vs a custom pack) — diagnostic only.
    apt_path = (os.environ.get("O4_FORCE_APT_DAT")
                or _pick_best_apt_dat_against_osm(xplane_root, icao))
    if apt_path is None:
        raise RuntimeError(f"No apt.dat found for {icao}")
    apt = APR.load_airport(apt_path, icao)
    if apt is None:
        raise RuntimeError(f"Could not load airport block for {icao}")

    # Complexity-based build-time prior (progress display only): the
    # apt.dat counts predict total time from past recorded builds, so
    # the window can show a ballpark "About m:ss remaining" from the
    # start and refine it phase by phase.  Full builds only — a
    # geometry-only build is a different (much shorter) animal.
    _build_started_at = time.time()
    _build_features = None
    if compute_elevations:
        try:
            from . import build_time_model as _time_model
            _build_features = _time_model.complexity_features(apt)
            _progress.set_time_model(
                _time_model.predict_total_seconds(icao, _build_features),
                _time_model.predict_phase_seconds(icao, _build_features))
        except Exception:
            pass

    anchor = _airport_anchor(apt)
    to_m = _projection(anchor)

    layout = PavementLayout(icao=icao, anchor=anchor,
                             apt_dat_path=apt_path)

    # Project the apt.dat row-130 airport boundary (in lat/lon) into
    # meter space so downstream emission has it ready when the
    # boundary shape is built.
    if apt.boundary is not None and not apt.boundary.is_empty:
        try:
            from shapely.ops import transform as _shp_transform
            # Smart source cleanup: drop digitization NEEDLES from the
            # hand-traced row-130 ring (sharp near-zero-area zigzags) so
            # every boundary consumer — ribbon, DEM bridge, interior
            # clip — sees a clean ring.  A needle's apex folds the
            # ribbon strip over itself → boundary∩boundary overlap
            # (HEAZ @ 30.10017,31.35442).
            from .boundary import _despike_airport_boundary
            layout.airport_boundary = _despike_airport_boundary(
                _shp_transform(
                    lambda lon, lat, z=None: to_m(lon, lat),
                    apt.boundary),
                icao=icao)
        except _GEOM_EXC:
            layout.airport_boundary = None

    # ── Runways ──────────────────────────────────────────────────
    runway_polys: List[Polygon] = []
    rwy_bearings: List[float] = []
    rwy_centerlines: List[LineString] = []
    for r in apt.runways:
        rect = _runway_rect_m(r, to_m)
        if rect.is_empty:
            continue
        runway_polys.append(rect)
        ref = f"{r.desig_a}/{r.desig_b}"
        layout.shapes.append(BuiltShape(
            polygon=rect, role=ROLE_RUNWAY, ref=ref))
        ax, ay = to_m(r.lon_a, r.lat_a)
        bx, by = to_m(r.lon_b, r.lat_b)
        if math.hypot(bx - ax, by - ay) > 1.0:
            rwy_centerlines.append(LineString([(ax, ay), (bx, by)]))
            rwy_bearings.append(
                math.degrees(math.atan2(bx - ax, by - ay)) % 180.0)
    layout.runway_union = unary_union(runway_polys) if runway_polys else None

    _progress.step()  # [2] Assembling pavement & runway shoulders

    # ── Pavement union in meter space ────────────────────────────
    pav_polys: List[Polygon] = []
    for pav in apt.pavements:
        if pav.polygon is None or pav.polygon.is_empty:
            continue
        pm = shp_transform(to_m, pav.polygon)
        if pm.is_empty:
            continue
        if pm.geom_type == "Polygon":
            pav_polys.append(pm)
        else:
            pav_polys.extend(g for g in getattr(pm, "geoms", [])
                             if g.geom_type == "Polygon")
    # Snapshot the apt.dat-only polygon list before DSF additions.
    # Terminal-pad selection prefers the SMALLEST containing
    # polygon, and DSF often ships small overlay-style polygons
    # over apt.dat pavement; without this snapshot a small DSF
    # overlay covering part of the apron will win over the larger
    # apt.dat terminal pavement and the resulting terminal pad
    # loses most of its area (SPJC terminal1 regressed from
    # 105 K m² → 35 K m² before this fix).
    apt_only_pav_polys: List[Polygon] = list(pav_polys)

    # Capture every apt.dat row-110 pavement polygon vertex so the
    # source-attribution test recognises junction perimeter
    # vertices inherited from row-110 (junctions are built as
    # ``pav_union.difference(rects)`` — see junction_emit.py).
    _seen = set()
    _boundary_lines = []
    for _p in pav_polys:
        try:
            rings = [_p.exterior, *_p.interiors]
        except _GEOM_EXC:
            continue
        for _r in rings:
            _boundary_lines.append(_r)
            for _x, _y in _r.coords:
                _key = (round(_x, 2), round(_y, 2))
                if _key in _seen:
                    continue
                _seen.add(_key)
                layout.apt_pavement_vertices.append((float(_x), float(_y)))
    # Also store the boundary line union so mid-edge points count
    # as legitimate row-110 inheritances.
    if _boundary_lines:
        try:
            layout.apt_pavement_boundary = unary_union(_boundary_lines)
        except _GEOM_EXC:
            layout.apt_pavement_boundary = None

    # ── Runway shoulder absorption ─────────────────────────────────
    # Long thin row-110 polygons parallel to a runway and touching
    # or overlapping it are runway shoulders (or, when wider than
    # the apt.dat row-100 width and centered on the runway, the
    # runway's own envelope polygon — apt.dat sometimes labels
    # these as "taxiways", e.g. HECA's "New Taxiway 1").  Fold them
    # into the runway: widen the runway emit to the union of
    # perpendicular extents, mutate the runway's apt.dat record so
    # downstream CIFP segmenting picks up the new width, and remove
    # the absorbed polygons from the pavement set so they don't
    # re-emit as junction polygons wrapping the runway.
    #
    # Asymmetric shoulders (one side only — common at gravel
    # crosswind runways like CYXY's 02/20) are handled by shifting
    # the runway centerline toward the shoulder midpoint while
    # widening to the union extent.  The CIFP threshold elevations
    # (anchored at the original runway thresholds) still apply
    # because the threshold lat/lon stays paired with the same
    # apt.dat designation; the perpendicular shift moves the
    # centerline by the shoulder offset (typically < 20 m, well
    # within DEM noise tolerance).
    absorbed_pav_indices: set = set()
    # Runways the whole-polygon pass widened — the later extent-based
    # shoulder pass skips these so the two don't compound.
    _shoulder_widened_refs: set = set()
    for ridx, r in enumerate(apt.runways):
        new_left, new_right, absorbed = _detect_runway_shoulders(
            r, to_m, pav_polys)
        new_width = new_right - new_left
        # Only widen when the new extent meaningfully exceeds the
        # apt.dat row-100 width (≥ 0.5 m on top of current).
        if new_width <= r.width_m + 0.5:
            continue
        offset = 0.5 * (new_left + new_right)
        # Shift centerline by ``offset`` perpendicular.  The
        # perpendicular vector in meter space is (nx, ny) =
        # (-uy, ux), where (ux, uy) is the runway's
        # along-axis unit vector.
        ax_m, ay_m = to_m(r.lon_a, r.lat_a)
        bx_m, by_m = to_m(r.lon_b, r.lat_b)
        udx = bx_m - ax_m
        udy = by_m - ay_m
        L_axis = math.hypot(udx, udy)
        if L_axis < 1.0:
            continue
        ux_m, uy_m = udx / L_axis, udy / L_axis
        nx_m, ny_m = -uy_m, ux_m
        # Apply offset back through the inverse of ``to_m``.  Our
        # to_m projection (see ``_projection``) is anchored at
        # ``layout.anchor`` = (lat0, lon0) and uses cos(lat0).
        lat0, lon0 = layout.anchor
        cos0 = math.cos(math.radians(lat0))
        d_lat_per_m = 1.0 / R_EARTH
        d_lon_per_m = 1.0 / (R_EARTH * cos0) if cos0 > 1e-9 else 0.0
        d_lat = math.degrees(ny_m * offset * d_lat_per_m)
        d_lon = math.degrees(nx_m * offset * d_lon_per_m)
        old_w = r.width_m
        old_lat_a, old_lon_a = r.lat_a, r.lon_a
        if abs(offset) > 0.05:
            r.lat_a = r.lat_a + d_lat
            r.lon_a = r.lon_a + d_lon
            r.lat_b = r.lat_b + d_lat
            r.lon_b = r.lon_b + d_lon
        r.width_m = new_width
        new_rect = _runway_rect_m(r, to_m)
        if new_rect.is_empty:
            r.lat_a, r.lon_a = old_lat_a, old_lon_a
            r.width_m = old_w
            continue
        runway_polys[ridx] = new_rect
        ref = f"{r.desig_a}/{r.desig_b}"
        _shoulder_widened_refs.add(ref)
        for s in layout.shapes:
            if s.role == ROLE_RUNWAY and s.ref == ref:
                s.polygon = new_rect
                break
        absorbed_pav_indices.update(absorbed)
        try:
            UI.vprint(1,
                f"  [pav-builder] {icao}: widened runway "
                f"{r.desig_a}/{r.desig_b}: {old_w:.1f}m → "
                f"{r.width_m:.1f}m"
                + (f" (centerline shifted {offset:+.1f}m)"
                if abs(offset) > 0.5 else "")
                + f" — absorbed {len(absorbed)} shoulder polygon(s).")
        except _GEOM_EXC:
            pass

    if absorbed_pav_indices:
        # Filter both pav_polys and the apt-only snapshot.  Use
        # WKB-identity to filter apt_only_pav_polys (its indices
        # don't necessarily match pav_polys' if either was modified;
        # the snapshot was taken just above so they're identical at
        # this point, but using WKB is robust to future changes).
        absorbed_wkbs = {pav_polys[i].wkb for i in absorbed_pav_indices
                         if 0 <= i < len(pav_polys)}
        pav_polys = [p for i, p in enumerate(pav_polys)
                     if i not in absorbed_pav_indices]
        apt_only_pav_polys = [p for p in apt_only_pav_polys
                              if p.wkb not in absorbed_wkbs]
        layout.runway_union = (unary_union(runway_polys)
                                if runway_polys else None)


    # Add draped pavement polygons from every available DSF for
    # this airport.  Some scenery packs (e.g. CYXY Whitehorse) ship
    # pavement geometry as DSF draped polygons referencing
    # ``lib/airport/pavement/*.pol`` definitions, with little or no
    # apt.dat row-110 coverage; for those airports the DSF is the
    # primary pavement source and we need to admit it.  Other packs
    # (e.g. SPJC Custom Scenery) ship apt.dat row-110 pavement AND
    # add layered visual overlays on top via DSF — emitting those
    # overlays as pavement duplicates the apt.dat coverage and pulls
    # non-pavement decoration into the layout.
    #
    # Three-tier filtering:
    #   1. ``O4_DSF_Reader._is_pavement_def`` admits only X-Plane
    #      stock pavement library paths (``lib/airport/pavement/...``
    #      and ``lib/airport/ground/pavement/...``).  Third-party
    #      libraries are dropped at this stage.
    #   2. Distance gate: the DSF tile is 1° × 1° (~110 km a side),
    #      and a single tile covers many airports' pavement.  Any
    #      DSF polygon whose bbox lies more than
    #      ``DSF_AIRPORT_RADIUS_M`` (5 km) from THIS airport's
    #      runway-bbox is somebody else's pavement — drop it.
    #      Caught the SPJC regression where 9 junctions ended up
    #      ~20 km away at SPLP because the SPJC custom scenery's
    #      DSF tile contains both airports' pavement.
    #   3. Overlay check: each surviving DSF polygon is compared
    #      against the apt.dat pavement union built so far.  If the
    #      polygon mostly overlaps existing pavement (≥ 80 %
    #      inside), treat it as an overlay and drop it entirely —
    #      preserves the apt.dat geometry.  Only DSF polygons that
    #      contribute substantially NEW coverage are appended.
    DSF_OVERLAY_FRAC = 0.80
    DSF_AIRPORT_RADIUS_M = 5_000.0
    # Boundary gate: the apt.dat row-130 boundary is authoritative for
    # what belongs to this airport — we must not pull in any pavement
    # outside it.  Foreign DSF/OSM pavement is CLIPPED to the boundary
    # (only the buffer below, a small tile-/projection-alignment slop,
    # is tolerated past the drawn line).  The bbox distance gate
    # (DSF_AIRPORT_RADIUS_M, 5 km) is far too coarse to separate
    # closely-spaced airports: HEAZ (Almaza) and HECA (Cairo Intl) sit
    # ~1 km apart in tile +30+031, so HEAZ's 5 km bbox swallowed HECA's
    # entire DSF apron/terminal pavement.  Both airports then emitted
    # overlapping polygons at conflicting elevations into the same tile
    # DSF — corrupting the mesh and crashing X-Plane on load.
    DSF_AIRPORT_BOUNDARY_BUFFER_M = 50.0
    apt_pav_union: Optional[Polygon] = None
    if pav_polys:
        try:
            apt_pav_union = unary_union(pav_polys)
        except _GEOM_EXC:
            apt_pav_union = None

    # Buffered row-130 boundary in meter space, used to gate DSF and
    # OSM pavement to this airport's own footprint.  None when the
    # airport has no usable boundary → falls back to the bbox-only
    # distance gate below (unchanged behaviour for sparse-apt.dat
    # airports like CYXY).
    boundary_gate_m: Optional[Polygon] = None
    _ab = getattr(layout, "airport_boundary", None)
    if _ab is not None and not _ab.is_empty:
        try:
            _bg = _ab.buffer(DSF_AIRPORT_BOUNDARY_BUFFER_M)
            if not _bg.is_empty:
                boundary_gate_m = _bg
        except _GEOM_EXC:
            boundary_gate_m = None

    # Load OSM airport data.  ``nodes``/``ways``/``relations`` are
    # reused by terminal extraction further below.  (DSF pavement is
    # no longer gated against an OSM-aeroway footprint — see the
    # resource-type note in the DSF loop.)
    nodes, ways, relations = _load_osm_airports(
        xplane_root, icao, anchor[0], anchor[1])
    # Compute the airport's bounding box from runway corners +
    # apt.dat pavement.  DSF polygons farther than
    # DSF_AIRPORT_RADIUS_M from this bbox are not this airport's.
    apt_bbox_m: Optional[Tuple[float, float, float, float]] = None
    bbox_polys = list(runway_polys) + list(pav_polys)
    if bbox_polys:
        try:
            uni = unary_union(bbox_polys)
            if not uni.is_empty:
                bx_min, by_min, bx_max, by_max = uni.bounds
                apt_bbox_m = (bx_min - DSF_AIRPORT_RADIUS_M,
                               by_min - DSF_AIRPORT_RADIUS_M,
                               bx_max + DSF_AIRPORT_RADIUS_M,
                               by_max + DSF_AIRPORT_RADIUS_M)
        except _GEOM_EXC:
            apt_bbox_m = None
    third_party_pav_ids: set = set()
    # Wide draped ``.lin`` border-strip placements collected in the DSF
    # sweep: ``(path_line_m, width_m, closed, def_path)``.  Filtered
    # after the sweep by the wraps-pavement test and unioned into the
    # pavement (user 2026-07-16, KBNA BordaTaxiway_* strips).
    dsf_border_line_candidates: List[tuple] = []
    # DSF terminal/hangar building footprints (meter space), collected
    # in the same DSF sweep as pavement and unioned with the OSM
    # building outlines at terminal-pad construction below.
    # DSF terminal / hangar / term_bridge building footprints (meter
    # space).  term_bridge_* slabs are admitted here too (gated by
    # TERM_BRIDGE_GROUPING) — they are just another component of a
    # complex building and union into the same per-building outline.
    dsf_building_polys: List[Polygon] = []
    n_dsf_buildings = 0
    n_dsf_object_buildings = 0
    try:
        if not LOAD_DSF_PAVEMENT:
            raise StopIteration  # skip the DSF block entirely
        from . import dsf_reader as _DSFR
        seen_dsf: set = set()
        # Read DSF pavement ONLY from the pack that supplied the chosen
        # apt.dat (``apt_path``).  Pulling in every pack's DSF re-imports
        # a foreign copy of the airport: when a Custom Scenery pack
        # overrides CYXY but the stock Global Airports pack still carries
        # the original geometry, its DSF re-introduces shapes the user
        # deleted from the custom pack (the overlay gate can't suppress a
        # polygon once the custom apt.dat union no longer covers it).
        # X-Plane itself shadows Global with the custom pack, so mirror
        # that: same-pack DSF only.
        all_apt_dats = [apt_path]
        n_dsf_kept = 0
        n_dsf_dropped_overlay = 0
        n_dsf_dropped_far = 0
        n_dsf_dropped_vehicle = 0
        dsf_vehicle_area_m2 = 0.0
        from .config import (
            DSF_OBJECT_PAVEMENT_MIN_AIRCRAFT_WIDTH_M
            as _OBJ_PAV_MIN_AIRCRAFT_WIDTH_M,
            DSF_OBJECT_PAVEMENT_OPENING_RATIO
            as _OBJ_PAV_OPENING_RATIO,
            DSF_OBJECT_PAVEMENT_SHOULDER_CONTACT_RATIO
            as _OBJ_PAV_SHOULDER_CONTACT)
        from . import object_footprints as _OBJ_FOOTPRINTS
        # Vehicle-classified object patches are DEFERRED, not dropped:
        # after the sweep (when every kept pavement polygon exists) the
        # shoulder pass readmits the ones in edge-contact with kept
        # pavement — a painted taxiway shoulder fails the width test
        # exactly like a road, and only contact separates them.
        deferred_vehicle_patches: List[Tuple[Polygon, str]] = []
        # Third-party .pol pavement (tier-2 reader admissions, e.g.
        # ZDP_Library concrete at KPHX): real base pavement for
        # coverage purposes, but excluded from apron-merge semantics
        # below (a full-airport base-texture layer under the runways
        # must not read as "an apron enclosing the runway" — it
        # apron-merged 65/67 KPHX runway segments).  Tracked in
        # ``third_party_pav_ids`` (initialised before this block).
        #
        # A cross-tile airport's scenery pack ships ONE DSF PER TILE
        # (KPHX straddles the −112 meridian; its south/east aprons
        # live only in the pack's +33-112.dsf).  Read every tile DSF
        # the airport's pavement bbox touches, not just the anchor
        # tile — the boundary/distance gates still clip each polygon
        # to this airport.
        _dsf_tiles: set = {(math.floor(anchor[0]),
                            math.floor(anchor[1]))}
        if apt_bbox_m is not None:
            _cos0 = math.cos(math.radians(anchor[0]))
            for _bx, _by in ((apt_bbox_m[0], apt_bbox_m[1]),
                             (apt_bbox_m[2], apt_bbox_m[3])):
                _blat = anchor[0] + math.degrees(_by / R_EARTH)
                _blon = anchor[1] + math.degrees(
                    _bx / (R_EARTH * _cos0)) if _cos0 > 1e-9 else anchor[1]
                _dsf_tiles.add((math.floor(_blat), math.floor(_blon)))
            # Fill the rectangle between the two corners (an airport
            # can only realistically span 2×2 tiles).
            _lats = sorted({t[0] for t in _dsf_tiles})
            _lons = sorted({t[1] for t in _dsf_tiles})
            _dsf_tiles = {(la, lo) for la in range(_lats[0], _lats[-1] + 1)
                          for lo in range(_lons[0], _lons[-1] + 1)}
        for ad in all_apt_dats:
          for _tlat, _tlon in sorted(_dsf_tiles):
            dsf = _DSFR.find_associated_dsf(ad, _tlat + 0.5, _tlon + 0.5)
            if dsf is None or dsf in seen_dsf:
                continue
            seen_dsf.add(dsf)
            # ``.pol`` draped pavement, then — behind its gate — the
            # draped-only OBJ8 ground-paint pages (HECA Tai Models:
            # base asphalt/concrete drawn as one whole-airport object
            # per texture).  Both readers return the same tuple shape,
            # so ONE loop applies the distance/boundary/overlay gates
            # and third-party marking to both sources (object def
            # paths are never stock ⇒ marked third-party below).
            _dsf_pavement_tuples = list(_DSFR.read_dsf_pavements(
                dsf, xplane_root=xplane_root))
            from .config import DSF_OBJECT_PAVEMENT as _DSF_OBJECT_PAVEMENT
            if _DSF_OBJECT_PAVEMENT:
                _dsf_pavement_tuples.extend(
                    _DSFR.read_dsf_object_pavements(
                        dsf, xplane_root=xplane_root))
            for outer, holes, def_path in _dsf_pavement_tuples:
                if len(outer) < 3:
                    continue
                try:
                    # Honour interior windings as holes — a perforated
                    # pavement ring (big outer + infield/building holes)
                    # must NOT be filled into a solid blob.
                    poly_ll = Polygon(
                        [(lon, lat) for (lon, lat) in outer],
                        [[(lon, lat) for (lon, lat) in h]
                         for h in holes if len(h) >= 3],
                    )
                    if not poly_ll.is_valid:
                        poly_ll = poly_ll.buffer(0)
                    if (poly_ll.is_empty
                            or poly_ll.geom_type != "Polygon"):
                        continue
                    pm = shp_transform(to_m, poly_ll)
                    if pm.is_empty or pm.geom_type != "Polygon":
                        continue
                    # Distance gate: skip polygons outside this
                    # airport's expanded bbox.
                    if apt_bbox_m is not None:
                        px_min, py_min, px_max, py_max = pm.bounds
                        if (px_max < apt_bbox_m[0]
                                or px_min > apt_bbox_m[2]
                                or py_max < apt_bbox_m[1]
                                or py_min > apt_bbox_m[3]):
                            n_dsf_dropped_far += 1
                            continue
                    # VEHICLE-PAVEMENT admission filter (owner direction
                    # 2026-07-18): an OBJECT-sourced ground-paint patch
                    # that is essentially nowhere as wide as aircraft
                    # pavement is a painted service road / drainage
                    # channel.  Morphological opening (erode by half the
                    # minimum aircraft width, dilate back) recovers the
                    # aircraft-capable cores; a low surviving-area ratio
                    # marks a vehicle corridor even when wide pockets
                    # (road intersections/plazas) survive plain erosion.
                    # Drop it HERE — before the union — so it never
                    # costs slice/weld/solve work and simply rides the
                    # DEM.  See config
                    # DSF_OBJECT_PAVEMENT_MIN_AIRCRAFT_WIDTH_M and
                    # object_footprints.is_vehicle_pavement_patch.
                    if (_OBJ_PAV_MIN_AIRCRAFT_WIDTH_M > 0.0
                            and def_path.lower().endswith(".obj")
                            and _OBJ_FOOTPRINTS.is_vehicle_pavement_patch(
                                pm, _OBJ_PAV_MIN_AIRCRAFT_WIDTH_M,
                                _OBJ_PAV_OPENING_RATIO)):
                        deferred_vehicle_patches.append((pm, def_path))
                        continue
                    # Boundary gate: clip the DSF polygon to this
                    # airport's row-130 boundary so nothing outside it
                    # (a neighbouring airport's pavement) is pulled in.
                    # A polygon entirely outside clips to empty → drop;
                    # one straddling the boundary keeps only its inside
                    # part.
                    if boundary_gate_m is not None:
                        try:
                            clipped_b = pm.intersection(boundary_gate_m)
                            if (clipped_b.geom_type == "MultiPolygon"
                                    and not clipped_b.is_empty):
                                clipped_b = max(clipped_b.geoms,
                                                key=lambda g: g.area)
                            if (clipped_b.is_empty
                                    or clipped_b.geom_type != "Polygon"
                                    or clipped_b.area < 5.0):
                                n_dsf_dropped_far += 1
                                continue
                            pm = clipped_b
                        except _GEOM_EXC:
                            n_dsf_dropped_far += 1
                            continue
                    # NOTE: DSF pavement is filtered by RESOURCE TYPE
                    # at read time — ``dsf_reader._is_pavement_def``
                    # admits only ``lib/airport/pavement/`` and
                    # ``lib/airport/ground/pavement/`` defs (real
                    # asphalt/concrete surface) and rejects
                    # ``ground/terrain/`` (grass/lawn), lines,
                    # markings, facades, etc.  Per user 2026-05-21 we
                    # TRUST that classification: every reader-admitted
                    # pavement polygon inside the airport boundary is
                    # kept.  The earlier OSM-aeroway-gap clip and the
                    # oversized-vs-apt.dat RATIO gate were dropped —
                    # they discarded real aprons and runway/taxiway
                    # shoulders (which aren't OSM-tagged and can be
                    # large single polygons) along with decoration.
                    # NOTE: the apparent "whole-airport" pavement tiles
                    # are perforated rings (a big outer winding with
                    # holes); their HOLES are honoured by the reader,
                    # so ``pm`` is already the true net surface — no
                    # area cap is needed.
                    # Overlay check: drop the polygon if most of its
                    # area lies inside the existing apt.dat pavement
                    # union (it's a decorative overlay rather than
                    # new pavement).
                    # The dropped polygon's OUTSIDE remainder is real
                    # pavement though (user 2026-07-02, SPJC: a mostly-
                    # overlapping DSF apron's outside strip was dropped
                    # with it, leaving terrain holes beside the stands)
                    # — keep any remainder piece above the sliver floor;
                    # a true tinted overlay is ~100 % inside and still
                    # drops entirely.
                    if apt_pav_union is not None:
                        try:
                            inter_area = pm.intersection(
                                apt_pav_union).area
                            if (pm.area > 0
                                    and inter_area / pm.area
                                    >= DSF_OVERLAY_FRAC):
                                n_dsf_dropped_overlay += 1
                                rem = pm.difference(apt_pav_union)
                                for g in (rem.geoms
                                          if rem.geom_type
                                          == "MultiPolygon"
                                          else [rem]):
                                    if (g.geom_type == "Polygon"
                                            and not g.is_empty
                                            and g.area >= 50.0):
                                        pav_polys.append(g)
                                        n_dsf_kept += 1
                                continue
                        except _GEOM_EXC:
                            pass
                    pav_polys.append(pm)
                    if not _DSFR.is_stock_pavement_def(def_path):
                        third_party_pav_ids.add(id(pm))
                    n_dsf_kept += 1
                except _GEOM_EXC:
                    continue
            # Wide ``.lin`` border strips from the SAME DSF (bbox-gated
            # here; the wraps-pavement test runs after the sweep, once
            # the full pavement union exists to test against).
            if os.environ.get("O4_DSF_BORDER_LINE_PAVEMENT", "1") == "1":
                for _bl_pts, _bl_width, _bl_closed, _bl_def in \
                        _DSFR.read_dsf_pavement_border_lines(
                            dsf, xplane_root=xplane_root):
                    if len(_bl_pts) < 2:
                        continue
                    try:
                        _bl_line = LineString(
                            [to_m(lon, lat) for (lon, lat) in _bl_pts])
                    except _GEOM_EXC:
                        continue
                    if _bl_line.is_empty or _bl_line.length < 5.0:
                        continue
                    if apt_bbox_m is not None:
                        _bx_min, _by_min, _bx_max, _by_max = \
                            _bl_line.bounds
                        if (_bx_max < apt_bbox_m[0]
                                or _bx_min > apt_bbox_m[2]
                                or _by_max < apt_bbox_m[1]
                                or _by_min > apt_bbox_m[3]):
                            continue
                    dsf_border_line_candidates.append(
                        (_bl_line, _bl_width, _bl_closed, _bl_def))
            # Terminal / hangar building footprints from the SAME DSF.
            # Same projection + distance + boundary gates as pavement,
            # but a CENTROID-in-boundary gate (keep the whole footprint
            # rather than clipping a building that grazes the boundary).
            if DSF_BUILDINGS:
                _admit = (lambda outer_ring, hole_rings:
                          _admit_dsf_building_footprint(
                              outer_ring, hole_rings, to_m, apt_bbox_m,
                              boundary_gate_m, dsf_building_polys))
                for b_outer, b_holes, _b_role in \
                        _DSFR.read_dsf_buildings(
                            dsf, xplane_root=xplane_root):
                    if len(b_outer) < 3:
                        continue
                    # term_bridge slabs are only admitted when the
                    # grouping gate is on; otherwise they are ignored
                    # entirely (byte-identical to the pre-grouping read).
                    if _b_role == "bridge" and not TERM_BRIDGE_GROUPING:
                        continue
                    if _admit(b_outer, b_holes):
                        n_dsf_buildings += 1
                # OBJ8 structure footprints from the SAME DSF (Phase 1
                # of the DSF object integration) join the pool through
                # the IDENTICAL admission path.  Gated inside on
                # DSF_OBJECT_BUILDINGS (default off).
                n_dsf_object_buildings += \
                    _collect_dsf_object_building_footprints(
                        dsf, xplane_root, _admit)
        # ── SHOULDER readmission (owner in-sim report 2026-07-18) ──
        # Runs after the whole sweep so the contact test sees EVERY
        # kept pavement polygon (apt.dat row-110 + .pol + wide object
        # sheets).  A vehicle-classified strip in edge-contact with
        # kept pavement for at least half its long side is a painted
        # taxiway shoulder — absorb it (third-party marked like every
        # object patch); the rest stay dropped and ride the DEM.
        n_dsf_shoulder_readmitted = 0
        if deferred_vehicle_patches:
            try:
                from shapely.strtree import STRtree
                _shoulder_tree = STRtree(pav_polys)
                for _vp, _vp_def in deferred_vehicle_patches:
                    try:
                        _near = [pav_polys[i] for i in
                                 _shoulder_tree.query(_vp)]
                    except _GEOM_EXC:
                        _near = list(pav_polys)
                    _contact = _OBJ_FOOTPRINTS.abutting_contact_ratio(
                        _vp, _near)
                    if _contact >= _OBJ_PAV_SHOULDER_CONTACT:
                        pav_polys.append(_vp)
                        third_party_pav_ids.add(id(_vp))
                        n_dsf_kept += 1
                        n_dsf_shoulder_readmitted += 1
                    else:
                        n_dsf_dropped_vehicle += 1
                        dsf_vehicle_area_m2 += _vp.area
            except _GEOM_EXC:
                for _vp, _vp_def in deferred_vehicle_patches:
                    n_dsf_dropped_vehicle += 1
                    dsf_vehicle_area_m2 += _vp.area
        if (n_dsf_kept or n_dsf_dropped_overlay
                or n_dsf_dropped_far or n_dsf_dropped_vehicle):
            try:
                msg = (f"  [pav-builder] {icao}: DSF pavement: "
                       f"{n_dsf_kept} kept, "
                       f"{n_dsf_dropped_overlay} dropped as overlay, "
                       f"{n_dsf_dropped_far} dropped as off-airport")
                if n_dsf_dropped_vehicle:
                    msg += (f", {n_dsf_dropped_vehicle} object patch(es) "
                            f"({dsf_vehicle_area_m2 / 1e4:.1f} ha) dropped "
                            f"as vehicle/drainage paint (nowhere "
                            f">= {_OBJ_PAV_MIN_AIRCRAFT_WIDTH_M:.0f} m "
                            f"wide - rides the DEM)")
                if n_dsf_shoulder_readmitted:
                    msg += (f", {n_dsf_shoulder_readmitted} narrow "
                            f"patch(es) readmitted as abutting shoulders")
                UI.vprint(1, msg + ".")
            except _GEOM_EXC:
                pass
        if n_dsf_buildings:
            try:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: DSF buildings: "
                    f"{n_dsf_buildings} terminal/hangar facade(s) "
                    f"inside boundary.")
            except _GEOM_EXC:
                pass
        if n_dsf_object_buildings:
            try:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: DSF object buildings: "
                    f"{n_dsf_object_buildings} OBJ8 structure "
                    f"footprint(s) inside boundary.")
            except _GEOM_EXC:
                pass
    except StopIteration:
        # DSF read intentionally disabled.
        try:
            UI.vprint(1,
                f"  [pav-builder] {icao}: DSF pavement read "
                f"disabled (LOAD_DSF_PAVEMENT=False).")
        except _GEOM_EXC:
            pass
    except _GEOM_EXC:
        pass

    # ── Pavement border-line strips (user 2026-07-16, KBNA hole) ─────
    # Construction style: a pack draws pavement as ``.pol`` polygons
    # PLUS wide draped ``.lin`` borders traced ALONG the polygon
    # outlines (KBNA ``BordaTaxiway_*``: 4-31 m wide, declared by the
    # resource's SCALE/TEX_WIDTH/S_OFFSET).  X-Plane centers the strip
    # on its path, so half of it is rendered pavement OUTSIDE the
    # ``.pol`` union — at KBNA that outer half is the missing 5-13 m
    # edge band plus the whole junction gap at 36.1156,-86.6682 (two
    # taxiways' 27 m concrete borders meet there).  A candidate strip
    # is included as pavement only when its path WRAPS the pavement:
    # at least half its sampled length runs within a few meters of the
    # pavement-union boundary.  Painted markings never qualify (the
    # reader's ≥ 2 m width floor), and a strip elsewhere on the field
    # (not along pavement) fails the wrap test.  Gate
    # O4_DSF_BORDER_LINE_PAVEMENT=0 restores the ``.pol``-only union.
    _BORDER_WRAP_SAMPLE_STEP_M = 5.0
    _BORDER_WRAP_EDGE_TOL_M = 3.0
    _BORDER_WRAP_MIN_FRACTION = 0.5
    if dsf_border_line_candidates:
        try:
            _border_reference = unary_union(
                [g for g in (list(pav_polys) + list(runway_polys))
                 if g is not None and not g.is_empty])
            _border_boundary = (_border_reference.boundary
                                if not _border_reference.is_empty
                                else None)
            _n_border_kept = 0
            _border_area = 0.0
            for (_bl_line, _bl_width, _bl_closed,
                 _bl_def) in dsf_border_line_candidates:
                if _border_boundary is None or _border_boundary.is_empty:
                    break
                try:
                    _n_samples = max(
                        2, int(_bl_line.length
                               / _BORDER_WRAP_SAMPLE_STEP_M))
                    _n_on_edge = sum(
                        1 for k in range(_n_samples)
                        if _border_boundary.distance(_bl_line.interpolate(
                            (k + 0.5) / _n_samples, normalized=True))
                        <= _BORDER_WRAP_EDGE_TOL_M)
                    if (_n_on_edge / _n_samples
                            < _BORDER_WRAP_MIN_FRACTION):
                        continue
                    _line_eff = _bl_line
                    if _bl_closed:
                        _coords = list(_bl_line.coords)
                        if _coords[0] != _coords[-1]:
                            _line_eff = LineString(
                                _coords + [_coords[0]])
                    _strip = _line_eff.buffer(_bl_width / 2.0,
                                              cap_style=2)
                    if boundary_gate_m is not None:
                        _strip = _strip.intersection(boundary_gate_m)
                except _GEOM_EXC:
                    continue
                for _piece in (_strip.geoms
                               if hasattr(_strip, "geoms")
                               else [_strip]):
                    if (_piece.geom_type == "Polygon"
                            and not _piece.is_empty
                            and _piece.area >= 25.0):
                        pav_polys.append(_piece)
                        third_party_pav_ids.add(id(_piece))
                        _border_area += _piece.area
                _n_border_kept += 1
            if _n_border_kept:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: DSF border-line strips: "
                    f"{_n_border_kept} of "
                    f"{len(dsf_border_line_candidates)} wide .lin "
                    f"path(s) wrap pavement — {_border_area:.0f} m2 "
                    f"added as pavement.")
        except _GEOM_EXC:
            pass

    pav_union = unary_union(pav_polys) if pav_polys else None
    # Merge near-touching apt.dat polygons so the union is one big
    # coverage (with real holes only) — see ``_merge_near_touching``.
    pav_union = _merge_near_touching(pav_union)
    # Per user 2026-05-05: simplify pav_union FIRST so all
    # downstream consumers (rect snap, junction emit) see a clean
    # 1 m-resolution coverage polygon.  Apt.dat row-110 polygons
    # routinely contain over-resolved curves (sub-meter steps) and
    # 0.2 m doubled-vertex needles that would otherwise survive
    # into the residue.  Rect corners snap to this simplified
    # boundary, so subtracting rects from pav_union should align
    # perfectly.
    from .pavement.union_helpers import (
        _close_open_clean,
        _simplify_pavement_polygon,
    )
    # Seam cleanup (user 2026-05-21): where apt.dat and DSF pavement
    # share a boundary, their slightly-different vertices leave BOTH thin
    # interior holes (gaps) AND thin exterior lips (spurs).  A single
    # mitre-join close-then-open removes both — narrower than the
    # effective-width threshold — with no net area change, replacing the
    # old separate ``_drop_sliver_holes`` (+ never-wired
    # ``_trim_sliver_spurs``).  Real grass infields and smooth bezier
    # curves (mitre-preserved) are untouched.  Run BEFORE the simplify so
    # the final boundary is clean for the downstream rect snap / junction
    # emit.
    pav_union = _close_open_clean(pav_union)
    # Simplify to a clean ~2 m-resolution coverage polygon: drop apt.dat
    # over-resolution (sub-meter curve steps) and doubled-vertex needles
    # so rect corners snap to a stable boundary and subtracting rects
    # aligns.  (User-approved tol=2.0 to match the reviewed union.)
    #
    # User 2026-06-28: DISABLED by default.  The 2 m vertex moves blur the
    # narrow truck-route necks that distinguish a groundside parking LOT from
    # the apron it abuts (CYXY 101/102 = a 12.5k m² apt.dat⊕DSF polygon meeting
    # the main apron over a 7.6 m truck-only neck) — we want pav_union to keep
    # the EXACT source outline so those connections stay legible for role
    # classification.  Set O4_PAV_UNION_SIMPLIFY=1 to restore the old behaviour.
    if os.environ.get("O4_PAV_UNION_SIMPLIFY", "0") == "1":
        pav_union = _simplify_pavement_polygon(pav_union, tol=2.0)

    # Record the SOURCE pavement union (apt.dat row-110 ⊕ DSF, before
    # runway subtraction) for build-time verification: every emitted
    # pavement shape must rest on this (∪ runway).  Captured here, before
    # the runway / ground-zone differences below mutate ``pav_union``.
    layout.source_pavement_union = pav_union

    # Source-attribution boundary for the junction-vertex test: junctions
    # are cut from THIS union (apt.dat row-110 + DSF pavement), so a
    # junction perimeter vertex following the union boundary is legitimately
    # sourced — even where the boundary comes from DSF pavement, which the
    # row-110-only ``apt_pavement_vertices`` capture above doesn't include.
    # (Test-only; the builder never reads ``apt_pavement_boundary``.  The
    # row-110-only ``apt_pavement_vertices`` still feeds canonical_points,
    # so the snap seed is unchanged.)  UNION with the existing row-110
    # boundary rather than replacing it: the union is simplified (tol=2.0)
    # so it can sit ~2 m off an unsimplified row-110 vertex — keeping both
    # is strictly more permissive and can't regress airports without DSF.
    if pav_union is not None and not pav_union.is_empty:
        try:
            _pub = pav_union.boundary
            if layout.apt_pavement_boundary is not None:
                layout.apt_pavement_boundary = unary_union(
                    [layout.apt_pavement_boundary, _pub])
            else:
                layout.apt_pavement_boundary = _pub
        except _GEOM_EXC:
            pass

    # ── Border-strip-derived shoulder DECLARATION (user 2026-07-17) ──
    # KBNA construction style: the runway's own ``.pol`` pieces are
    # exact runway width and the shoulder is a wide draped ``.lin``
    # border traced along the runway outline — the strip's declared
    # width states the shoulder width EXACTLY (width/2; 13/31's 24 m
    # border ⇒ 12 m, 02C/20C's 20 m ⇒ 10 m).  Rather than grow a new
    # widening mechanism, DECLARE the measurement into the apt.dat
    # coded-shoulder path below ("this runway HAS 12 m shoulders"):
    # the established block widens symmetrically, junctions cut at the
    # shoulder edge (the OMAA-proven model), downstream segmentation
    # reads the widened ``width_m``, and the extent pass skips the
    # runway via the coded skip-set update.  Shoulders are symmetric
    # even when the border evidence is one-sided — the other side's
    # shoulder band is simply covered by abutting taxiway/apron
    # ``.pol`` pavement (13/31's right edge), and cutting junctions at
    # the shoulder line there is exactly the coded-shoulder semantics.
    # NOTE: the runway-segmentation contact budget
    # (RUNWAY_SHOULDER_SEGMENT) ran BEFORE DSF ingest, so it cannot
    # see a border-derived code — inert at KBNA (zero row-110
    # contacts); revisit if a border-styled pack ships row-110
    # contact geometry.
    # Without the wide-biased extent clamp these runways used to get
    # (15 m/side against continuous flanking pavement), the emitted
    # rect lands on the author's true shoulder edge instead of eating
    # ~4 m of taxiway complex per side and shredding the junctions.
    from .config import (
        RUNWAY_BORDER_SHOULDER,
        RUNWAY_BORDER_SHOULDER_EDGE_TOL_M,
        RUNWAY_BORDER_SHOULDER_MIN_SIDE_COVER_M,
        RUNWAY_BORDER_SHOULDER_MIN_STRIP_COVER_M,
        RUNWAY_BORDER_SHOULDER_SAMPLE_STEP_M,
        RUNWAY_SHOULDER_EXTENT_MAX_M,
        RUNWAY_SHOULDER_EXTENT_MIN_M,
    )
    if (RUNWAY_BORDER_SHOULDER and runway_polys
            and dsf_border_line_candidates):
        _border_strip_lines = [
            (_bl_line, _bl_width)
            for (_bl_line, _bl_width, _bl_closed, _bl_def)
            in dsf_border_line_candidates]
        for r in apt.runways:
            if r.shoulder_code // 100 >= 1:
                continue        # apt.dat already declares a width
            ref = f"{r.desig_a}/{r.desig_b}"
            if ref in _shoulder_widened_refs:
                continue
            try:
                border_shoulder_w = (
                    _detect_runway_border_strip_shoulders(
                        r, to_m, _border_strip_lines,
                        edge_tol_m=RUNWAY_BORDER_SHOULDER_EDGE_TOL_M,
                        sample_step_m=(
                            RUNWAY_BORDER_SHOULDER_SAMPLE_STEP_M),
                        min_strip_cover_m=(
                            RUNWAY_BORDER_SHOULDER_MIN_STRIP_COVER_M),
                        min_side_cover_m=(
                            RUNWAY_BORDER_SHOULDER_MIN_SIDE_COVER_M),
                        min_w=RUNWAY_SHOULDER_EXTENT_MIN_M,
                        max_w=RUNWAY_SHOULDER_EXTENT_MAX_M))
            except _GEOM_EXC:
                continue
            if border_shoulder_w is None:
                continue
            coded_width_m = int(round(border_shoulder_w))
            if coded_width_m < 1:
                continue
            r.shoulder_code = (100 * coded_width_m
                               + (r.shoulder_code % 100))
            UI.vprint(1,
                f"  [pav-builder] {icao}: runway {ref}: .lin border "
                f"strips declare {coded_width_m} m shoulders "
                f"(strip width / 2) — handled by the coded-shoulder "
                f"path.")

    # ── Runway shoulder widening (user 2026-05-23) ──────────────────
    # apt.dat row 100 field 4 encodes the runway shoulder as
    # ``100 * shoulder_width_m + surface_code`` (X-Plane 12 spec): when
    # the value is > 100 the 100s/1000s digits give the shoulder width in
    # whole metres per side (e.g. HECA's 701 = surface 1 + 7 m, 724 =
    # surface 24 + 7 m); a value < 100 is a bare surface code with no
    # explicit width (X-Plane's own 3-5 m default scaling — we leave
    # those runways unwidened, e.g. SPJC 27/28, CYXY 1).  The shoulder
    # strip isn't in the row-100 width, so without accounting for it it
    # falls into the junction/apron residue as thin "wings" running along
    # the runway.  For each runway with an explicit shoulder width, widen
    # its rect symmetrically by that width per side (spec-driven, no
    # pavement analysis), keeping it a clean 4-corner rect.  Done here,
    # BEFORE the runway is subtracted from the pavement union below, so
    # the strip becomes runway and the junctions come out clean; Phase-2
    # segmentation (reads ``width_m``) builds sub-rects at the widened
    # width.  Skip runways already widened by the pre-DSF whole-polygon
    # shoulder pass so the two don't compound.
    if runway_polys:
        _widened_any = False
        for ridx, r in enumerate(apt.runways):
            if ridx >= len(runway_polys):
                continue
            shoulder_w_m = r.shoulder_code // 100   # encoded width (m)/side
            if shoulder_w_m < 1:
                continue
            ref = f"{r.desig_a}/{r.desig_b}"
            if ref in _shoulder_widened_refs:
                continue
            rect = runway_polys[ridx]
            if rect is None or rect.is_empty:
                continue
            half = r.width_m / 2.0
            old_w = r.width_m
            new_rect = _widen_runway_rect(
                r, layout.anchor,
                -(half + shoulder_w_m), half + shoulder_w_m, to_m)
            if new_rect is None or new_rect.is_empty:
                continue
            runway_polys[ridx] = new_rect
            for s in layout.shapes:
                if s.role == ROLE_RUNWAY and s.ref == ref:
                    s.polygon = new_rect
                    break
            _widened_any = True
            UI.vprint(1,
                f"  [pav-builder] {icao}: shoulder-widened runway "
                f"{ref}: {old_w:.1f}m → {r.width_m:.1f}m "
                f"(+{shoulder_w_m}m/side, apt.dat shoulder code "
                f"{r.shoulder_code}).")
        if _widened_any:
            layout.runway_union = (unary_union(runway_polys)
                                   if runway_polys else None)
        for ridx, r in enumerate(apt.runways):
            # Row-100-widened runways join the skip set so the
            # extent pass below doesn't compound on top.
            if r.shoulder_code // 100 >= 1:
                _shoulder_widened_refs.add(f"{r.desig_a}/{r.desig_b}")

    # ── Extent-based runway shoulder widening (user 2026-06-12) ────
    # Shoulders carried only by DSF pavement (KPHL StarSim: a whole-
    # airport Groundtextures asphalt.pol ring, 3.7 M m²/87 holes) have
    # no discrete row-110 strip polygon for the whole-polygon pass
    # (which also runs pre-DSF) and no row-100 declared width for the
    # spec pass above — the strip along each runway edge falls into
    # residue and emits as apron pieces hugging the runway.  Measure
    # the pavement itself: when a consistent strip of shoulder-range
    # width runs along a runway edge in the FINAL union and is mostly
    # NOT row-110-covered (the DSF gap — measured KPHL q1/median
    # extents 3-11 m at 100 % station coverage on every side), widen
    # the rect over it here, BEFORE the runway subtraction below, so
    # the strip becomes runway.  Row-110-carried shoulders keep their
    # established handling (whole-polygon absorption; SPJC's envelope
    # shoulders deliberately live in the junction cut — see the
    # INTERSECTION_PROX_M budget above).
    from .config import (
        RUNWAY_SHOULDER_EXTENT,
        RUNWAY_SHOULDER_EXTENT_MAX_APT_FRAC,
        RUNWAY_SHOULDER_EXTENT_MAX_M,
        RUNWAY_SHOULDER_EXTENT_MIN_COVERAGE,
        RUNWAY_SHOULDER_EXTENT_MIN_M,
        RUNWAY_SHOULDER_EXTENT_STATION_M,
        RUNWAY_SHOULDER_EXTENT_STEP_M,
    )
    if (RUNWAY_SHOULDER_EXTENT and runway_polys
            and pav_union is not None and not pav_union.is_empty):
        _apt_only_union = None
        try:
            _apt_only_union = unary_union(
                [p for p in apt_only_pav_polys
                 if p is not None and not p.is_empty])
        except _GEOM_EXC:
            _apt_only_union = None
        _ext_widened_any = False
        for ridx, r in enumerate(apt.runways):
            if ridx >= len(runway_polys):
                continue
            ref = f"{r.desig_a}/{r.desig_b}"
            if ref in _shoulder_widened_refs:
                continue
            rect = runway_polys[ridx]
            if rect is None or rect.is_empty:
                continue
            try:
                extent = _detect_runway_shoulder_extent(
                    r, to_m, pav_union, _apt_only_union,
                    station_m=RUNWAY_SHOULDER_EXTENT_STATION_M,
                    step_m=RUNWAY_SHOULDER_EXTENT_STEP_M,
                    min_w=RUNWAY_SHOULDER_EXTENT_MIN_M,
                    max_w=RUNWAY_SHOULDER_EXTENT_MAX_M,
                    min_coverage=RUNWAY_SHOULDER_EXTENT_MIN_COVERAGE,
                    max_apt_frac=RUNWAY_SHOULDER_EXTENT_MAX_APT_FRAC)
            except _GEOM_EXC:
                continue
            if extent is None:
                continue
            new_left, new_right = extent
            old_w = r.width_m
            new_rect = _widen_runway_rect(
                r, layout.anchor, new_left, new_right, to_m)
            if new_rect is None or new_rect.is_empty:
                continue
            runway_polys[ridx] = new_rect
            for s in layout.shapes:
                if s.role == ROLE_RUNWAY and s.ref == ref:
                    s.polygon = new_rect
                    break
            _shoulder_widened_refs.add(ref)
            _ext_widened_any = True
            UI.vprint(1,
                f"  [pav-builder] {icao}: extent-widened runway "
                f"{ref}: {old_w:.1f}m → {r.width_m:.1f}m "
                f"(measured shoulder strip "
                f"{-new_left - old_w / 2.0:+.1f}m/"
                f"{new_right - old_w / 2.0:+.1f}m per side).")
        if _ext_widened_any:
            layout.runway_union = (unary_union(runway_polys)
                                   if runway_polys else None)

    # ── Pavement-runway intersection points (per user 2026-05-05) ──
    # RELOCATED POST-DSF (user ruling 2026-07-17: "segmentation
    # contacts definitely need to run after we've processed the DSF or
    # we could miss important things").  This block previously ran
    # before the DSF sweep on ``apt_only_pav_polys`` — at packs that
    # ship ALL their taxiway pavement as draped ``.pol`` (KBNA, KCLT)
    # it collected ZERO exit contacts and the runways segmented on
    # thresholds alone.  It now runs on the FULL pavement set (apt.dat
    # ⊕ DSF ⊕ admitted border strips) AFTER every shoulder-widening
    # pass, so contacts are measured against the final widened rect at
    # the true paved connection points.  Two consequences of the new
    # position: pavement polygons wholly INSIDE a runway's rect are
    # skipped (a segmented-runway pack draws the runway itself as ~95
    # short ``.pol`` pieces — their internal joints are not exit
    # contacts), and the RUNWAY_SHOULDER_SEGMENT coded-budget
    # extension is retired (the rect is already widened to the coded /
    # border-derived shoulder edge before contacts are collected;
    # extending the band again would double-reach past the shoulder
    # and catch parallel taxiways).
    # Walk each pavement polygon's exterior; collect
    # the vertices that sit within ``INTERSECTION_PROX_M`` of a
    # runway's 4-corner rect boundary AND project to a centerline
    # parameter strictly between 0 and 1 (not at the runway ends).
    # These t-values become segment seam corners during Phase 2's
    # runway segmentation, so chain corners (= the runway-union
    # exterior) align exactly with apt.dat-pavement boundaries.
    # Without this, the junction-widening pass has to bridge the
    # gap with boundary-trace waypoints — the alignment makes that
    # unnecessary.  Per user direction: dedup intersections within
    # 2 m centerline distance (a junction can span a 2 m gap
    # without needing a node there).
    #
    # Per user 2026-05-11: tolerance widened from 0.5 m to 3.0 m so
    # apt.dat pavement boundaries drawn slightly INSIDE the row-100
    # runway rect (1-2 m offsets are common — SPJC's row-110 stops
    # 1.75 m short of 16R/34L at the V1 throat) still register as
    # runway intersection points.  Without this, the segmenter
    # doesn't insert a seam corner at the chart-level pavement
    # transition, and the downstream junction-widening pass can't
    # share a vertex with the runway there — leaving a visible
    # 1-2 m sliver gap between every taxi-junction and the runway
    # boundary.  The runway segmenter projects each kept point onto
    # the centerline and emits the seam corner there; the actual
    # corner sits on the runway boundary (rect-perpendicular at
    # half-width) regardless of how far the source pavement vertex
    # was off-edge, so widening the tolerance just unlocks more
    # near-runway pavement landmarks as segmentation breakpoints
    # without distorting the segmenter's output geometry.
    #
    # Per user 2026-05-14: bumped 3.0 → 6.0 m to capture SPLP's
    # north-end pavement corners drawn ~5 m INSIDE the runway
    # rect.  At 3 m those corners were missed; the runway
    # segmenter put no seam there, and the apron junction between
    # the A stub and the runway had to span ~110 m of runway
    # boundary (segment 25's full length) without a shared-vertex
    # snap point — the junction's runway edge ran past the A-stub
    # corner with no clean trapezoid shape.
    #
    # Per user 2026-05-17: budget = RUNWAY_SHOULDER_M (standard
    # FAA shoulder allowance, ~7.6 m per side) + CHART_TOL_M.
    # The runway rect from apt.dat row 100 covers only the
    # published runway width; the actual paved area extends past
    # this by the shoulder width on each side (SPJC's row 100
    # declares shoulder surface code 27/28 for 16L/34R — shoulders
    # are present but not given an explicit width in apt.dat).
    # apt.dat row-110 boundary polygons that include the shoulder
    # area sit up to ~RUNWAY_SHOULDER_M past the row-100 rect.
    # Without this allowance the intersection collector misses
    # SPJC's apt.dat boundary vertex at lat -12.036366 lon
    # -77.107520 (11.3 m perpendicular from runway 16L rect — the
    # shoulder edge), no runway seam fires at Lima's natural
    # pavement termination, and Lima's end junction degenerates
    # into a triangle.
    RUNWAY_SHOULDER_M = 7.6
    CHART_TOL_M = 4.4
    INTERSECTION_PROX_M = RUNWAY_SHOULDER_M + CHART_TOL_M  # 12.0 m
    # Dedup proportionally to PROX so multi-vertex clusters of a
    # single pavement transition (row-110 boundaries drawn with 3-4
    # vertices within a 3 m span at the runway edge) collapse to one
    # seam corner instead of fragmenting the runway segment.
    INTERSECTION_DEDUP_M = 5.0
    pav_runway_intersections: dict = {}
    for ridx, r in enumerate(apt.runways):
        if ridx >= len(runway_polys):
            continue
        rect = runway_polys[ridx]
        if rect is None or rect.is_empty:
            continue
        rect_boundary = rect.exterior
        cl_ax, cl_ay = to_m(r.lon_a, r.lat_a)
        cl_bx, cl_by = to_m(r.lon_b, r.lat_b)
        cl_dx = cl_bx - cl_ax
        cl_dy = cl_by - cl_ay
        cl_L2 = cl_dx * cl_dx + cl_dy * cl_dy
        if cl_L2 < 1.0:
            continue
        # Extend centerline endpoints by blast pads so ``t`` is
        # computed in the blast-extended frame — matching both the
        # rect_boundary (which includes blast pads, see
        # ``_runway_rect_m``) and the segmenter's own phys_end_a/b
        # parameterisation (which also absorbs blast pads).  Without
        # this, a row-110 vertex sitting in the blast-pad zone (e.g.
        # SPJC 16R V1 throat at 1.79 m off the runway boundary,
        # ax≈−5 m in row-100 frame) lands at t<0 and gets dropped
        # by ``end_skirt`` — the segmenter never sees it as a
        # candidate breakpoint, so the sloped end-segment (which
        # must stay 4-corner) can't split there to give the apron
        # junction a shared snap node.
        blast_a_m = r.blast_a_m or 0.0
        blast_b_m = r.blast_b_m or 0.0
        if blast_a_m > 0.0 or blast_b_m > 0.0:
            row100_dist = math.sqrt(cl_L2)
            ux = cl_dx / row100_dist
            uy = cl_dy / row100_dist
            cl_ax -= ux * blast_a_m
            cl_ay -= uy * blast_a_m
            cl_bx += ux * blast_b_m
            cl_by += uy * blast_b_m
            cl_dx = cl_bx - cl_ax
            cl_dy = cl_by - cl_ay
            cl_L2 = cl_dx * cl_dx + cl_dy * cl_dy
        phys_dist = math.sqrt(cl_L2)
        # Avoid the runway end zones — the segmenter handles those
        # via thresholds + physical-end anchors and we don't want
        # spurious end-zone seams.
        end_skirt_t = 5.0 / phys_dist
        intersections: List[Tuple[float, float]] = []
        # Split the runway at each adjacent pavement shape's CONTACT with
        # it — the NEAR and FAR edges of where the shape's boundary runs
        # along / abuts / crosses the runway — NOT at every intermediate
        # node (user 2026-05-23).  Intersect each pavement polygon's
        # boundary with a narrow band around the runway boundary: each
        # contiguous arc within the band is ONE contact (one abutting
        # shape's run along the runway edge), so we cut only at the arc's
        # two along-runway extremes and skip its interior nodes.  This
        # makes a junction whose straight edge runs 100 m along the runway
        # with no intermediate vertices ONE contact → ONE runway sub-rect,
        # and — being PROXIMITY-based, not crossing-based — also catches a
        # shape that comes right up to the runway edge without crossing
        # it.  Blast pads / displaced thresholds are part of the runway
        # rect and treated the same (contacts there cut too); only the
        # CIFP threshold cut itself comes from the segmenter's anchored
        # fractions.
        # The generic FAA-allowance budget from the FINAL (widened)
        # rect edge.  The former RUNWAY_SHOULDER_SEGMENT coded-budget
        # extension (OMAA 20 m ⇒ reach 24.4 m from the UNWIDENED rect)
        # is retired by the post-DSF relocation: every coded /
        # border-derived shoulder has already widened the rect to the
        # shoulder edge, so the generic band measured from that edge
        # reaches the same physical connection points — and extending
        # it again would double-reach past the shoulder into parallel
        # taxiway pavement.
        prox_m = INTERSECTION_PROX_M
        try:
            prox_band = rect_boundary.buffer(prox_m)
        except _GEOM_EXC:
            prox_band = None
        if prox_band is not None:
            for pav_poly in pav_polys:
                # Quick reject + own-pavement skip: a polygon wholly
                # inside the rect is runway pavement (segmented-piece
                # packs), not an exit contact.
                try:
                    if pav_poly.distance(rect_boundary) > prox_m:
                        continue
                    if rect.contains(pav_poly):
                        continue
                except _GEOM_EXC:
                    pass
                try:
                    near = pav_poly.boundary.intersection(prox_band)
                except _GEOM_EXC:
                    continue
                if near.is_empty:
                    continue
                arcs = (list(near.geoms)
                        if near.geom_type in ("MultiLineString",
                                              "GeometryCollection",
                                              "MultiPoint")
                        else [near])
                for arc in arcs:
                    if arc.is_empty:
                        continue
                    if arc.geom_type == "Point":
                        acoords = [(arc.x, arc.y)]
                    elif arc.geom_type == "LineString":
                        acoords = list(arc.coords)
                    else:
                        continue
                    a_ts = [((px - cl_ax) * cl_dx
                             + (py - cl_ay) * cl_dy) / cl_L2
                            for px, py in acoords]
                    # Cut at the contact arc's near + far edges only.
                    for t in (min(a_ts), max(a_ts)):
                        if t <= end_skirt_t or t >= 1.0 - end_skirt_t:
                            continue
                        intersections.append((t, cl_ax + t * cl_dx,
                                              cl_ay + t * cl_dy))
        # Per user (session 44): also break EVERY runway at its RUNWAY
        # CROSSINGS — where another runway's pavement overlaps this
        # one.  Without a seam there, a runway with no pavement-vertex
        # breakpoints emits as ONE rect that runs un-split across the
        # other runway, so ``_resolve_runway_crossings`` has no sub-rect
        # boundary to isolate and the crossing never becomes a clean
        # junction (CYXY 02/20).  Use RECT-OVERLAP, not centerline×
        # centerline: at CYXY the short 02/20 crosses 14L/32R's pavement
        # but 02/20's centerline ends before reaching 14L/32R's
        # centerline, so a centerline-intersection test misses that
        # crossing entirely.  Clip on BOTH sides of each crossing:
        # project the overlap REGION onto this runway's centerline and
        # add a seam at its ENTRY (t_lo) and EXIT (t_hi), so the crossing
        # region becomes its own segment (the segmenter splits there, the
        # two runways' crossing sub-rects overlap exactly, and
        # ``_resolve_runway_crossings`` merges them into one clean
        # crossing-junction; the apron can absorb a freed runway end —
        # CYXY runway-02 end).  ``rect`` includes blast pads / displaced-
        # threshold pavement, so crossings in those paved zones split too.
        for r2idx in range(len(apt.runways)):
            if r2idx == ridx or r2idx >= len(runway_polys):
                continue
            r2_rect = runway_polys[r2idx]
            if r2_rect is None or r2_rect.is_empty:
                continue
            try:
                ov = rect.intersection(r2_rect)
            except _GEOM_EXC:
                continue
            if ov.is_empty or ov.area < 1.0:
                continue
            # Project every vertex of the overlap region onto this
            # runway's centerline → t-range [t_lo, t_hi].
            ov_polys = (list(ov.geoms)
                        if ov.geom_type == "MultiPolygon" else [ov])
            ov_ts: List[float] = []
            for op in ov_polys:
                if op.geom_type != "Polygon" or op.is_empty:
                    continue
                for ox, oy in op.exterior.coords:
                    ov_ts.append(
                        ((ox - cl_ax) * cl_dx
                         + (oy - cl_ay) * cl_dy) / cl_L2)
            if not ov_ts:
                continue
            for t in (min(ov_ts), max(ov_ts)):
                if t <= end_skirt_t or t >= 1.0 - end_skirt_t:
                    continue
                # Seam point ON this runway's centerline at parameter t.
                intersections.append((t, cl_ax + t * cl_dx,
                                      cl_ay + t * cl_dy))
        if not intersections:
            continue
        # Sort by centerline t and dedup.  Per user 2026-05-11: dedup
        # by EUCLIDEAN distance between consecutive candidate points
        # rather than centerline-t alone.  A row-110 boundary that
        # approaches the runway with a slight angle puts multiple
        # close-together apt.dat vertices on the runway edge —
        # each at a slightly different axial position but only ~3 m
        # apart in real space.  Centerline-t dedup keeps them all
        # (their t values differ by ≥ dedup_t_gap); euclidean dedup
        # merges them into a single seam corner, which is what the
        # runway segmenter actually needs.  Without this, every
        # close-together row-110 vertex becomes a runway-segment
        # seam corner and the downstream junction polygon has to
        # wrap around all of them (the cluster of -349/-351/-352
        # corners on -10109's east edge that pinched -10182).
        intersections.sort(key=lambda x: x[0])
        dedup_m2 = INTERSECTION_DEDUP_M * INTERSECTION_DEDUP_M
        # Per user 2026-05-14: also dedup by ALONG-AXIS distance.
        # With INTERSECTION_PROX_M widened to 6 m we pick up
        # opposite-side pavement vertices at the same chart-level
        # runway transition (e.g. SPLP north end: an outside-edge
        # vertex on the west boundary at one t and an inside-the-
        # rect vertex on the east boundary at a t value only 4 m
        # along the runway).  Euclidean dedup misses these (10 m
        # apart across the runway), but they represent the SAME
        # transition and should collapse to one seam.  Otherwise
        # the runway segmenter inserts two adjacent seams ~4 m
        # apart and the resulting micro-segment fails the grade
        # check at the 23 % vertex-pair grade on its short edge.
        INTERSECTION_DEDUP_ALONG_M = 5.0
        deduped: List[Tuple[float, float, float]] = []
        for t, px, py in intersections:
            if deduped:
                dpx = px - deduped[-1][1]
                dpy = py - deduped[-1][2]
                if dpx * dpx + dpy * dpy < dedup_m2:
                    continue
                dt_along_m = abs(t - deduped[-1][0]) * phys_dist
                if dt_along_m < INTERSECTION_DEDUP_ALONG_M:
                    continue
            deduped.append((t, px, py))
        # Convert intersection meter-coords back to lat/lon via the
        # layout's m_to_ll (the segmenter consumes lat/lon).  Store
        # under both designator orderings so the segmenter lookup
        # finds them regardless of which key it tries.
        ll_pts = [layout.m_to_ll(px, py) for _, px, py in deduped]
        # Also store under the canonical (zero-padding-reconciled) pair
        # so the segmenter — which iterates CIFP's zero-padded ``RW09``
        # designators — matches regardless of whether THIS apt.dat
        # zero-pads its single-digit runways (see
        # ``runway_segments.canonical_runway_desig``).
        from .pavement.runway_segments import canonical_runway_desig
        ca = canonical_runway_desig(r.desig_a)
        cb = canonical_runway_desig(r.desig_b)
        for key in (
                (r.desig_a, r.desig_b),
                (r.desig_b, r.desig_a),
                ("RW" + r.desig_a.lstrip("RW"),
                 "RW" + r.desig_b.lstrip("RW")),
                ("RW" + r.desig_b.lstrip("RW"),
                 "RW" + r.desig_a.lstrip("RW")),
                (ca, cb), (cb, ca)):
            pav_runway_intersections[key] = list(ll_pts)
    layout._pav_runway_intersections = pav_runway_intersections

    # Stash the pre-runway-subtraction pavement polygon list for
    # the apron-merged-runway detection in _compute_elevations.
    # A runway segment is "apron-merged" when the apt.dat polygon
    # CONTAINING it is much larger than the segment itself —
    # apron pavement enclosing a runway is far wider than the
    # runway, while a normal runway lies inside a runway-shaped
    # apt.dat polygon that's only marginally larger than itself.
    # Third-party DSF base-texture polys are NOT apron candidates: a
    # full-airport ``.pol`` layer under the runways would apron-merge
    # every runway segment (KPHX lost 65/67 to ZDP concrete).
    apron_candidates = [p for p in pav_polys
                        if id(p) not in third_party_pav_ids]
    if pav_union is not None and layout.runway_union is not None:
        # Per user 2026-04-28: where a runway passes through a much
        # larger apron polygon, the runway is "apron-merged" — the
        # apron physically covers the runway pavement and the
        # downstream runway-segment-chain processing will drop the
        # apron-merged segments.  Don't subtract those parts from
        # pav_union now: the apron junctions should cover them
        # naturally, with no runway-shaped void to fill later.
        #
        # Detection mirrors ``_compute_elevations``'s segment-level
        # check (line ~3469) but applied to the original runway
        # polygons: the runway/candidate intersection counts as
        # apron-merged when the candidate is ≥
        # RUNWAY_APRON_AREA_RATIO × the intersection area.  A small
        # taxiway-sized candidate doesn't qualify (intersection is
        # most of the candidate); only big apron polygons do.
        apron_merged_regions: List[Polygon] = []
        for r_poly in (runway_polys if ABSORB_RUNWAY_IN_APRON else ()):
            for cand in apron_candidates:
                try:
                    inter = r_poly.intersection(cand)
                    if inter.is_empty or inter.area < 1.0:
                        continue
                    if cand.area > inter.area * RUNWAY_APRON_AREA_RATIO:
                        # Take the intersection as the apron-merged
                        # region — extracted as Polygon parts only.
                        if inter.geom_type == "Polygon":
                            apron_merged_regions.append(inter)
                        elif hasattr(inter, "geoms"):
                            for g in inter.geoms:
                                if (g.geom_type == "Polygon"
                                        and not g.is_empty):
                                    apron_merged_regions.append(g)
                except _GEOM_EXC:
                    continue
        if apron_merged_regions:
            try:
                merged_union = unary_union(apron_merged_regions)
                effective_runway = layout.runway_union.difference(
                    merged_union)
            except _GEOM_EXC:
                effective_runway = layout.runway_union
        else:
            effective_runway = layout.runway_union
        # Two pav_union variants:
        #   * ``pav_union_for_rects`` — full runway subtraction.
        #     Used by ``_build_taxi_rects`` for centerline clipping
        #     and the apron-interior boundary check.  Keeps F-style
        #     rects from extending into apron-merged-runway regions
        #     and failing the corner-on-boundary check (regression
        #     observed at CYXY's North F when residue switched to
        #     effective-runway subtraction).
        #   * ``pav_union`` (mutated below) — effective_runway
        #     subtraction so the residue / apron junctions cover
        #     apron-merged regions naturally.
        pav_union_for_rects = pav_union.difference(layout.runway_union)
        pav_union = pav_union.difference(effective_runway)
        layout._effective_runway_union = effective_runway
        layout._pav_union_for_rects = pav_union_for_rects

    # Collect all apt.dat pavement vertices (pre-union, real apt.dat
    # coord set) + runway corners.  This is the authoritative vertex
    # set the target snapper uses; rect corners will snap to these
    # preferentially so output shares vertices with target.
    apt_pav_vertices: List[Tuple[float, float]] = []
    for _pp in pav_polys:
        if _pp.is_empty or _pp.geom_type != "Polygon":
            continue
        _ec = list(_pp.exterior.coords)
        if _ec and _ec[0] == _ec[-1]:
            _ec = _ec[:-1]
        apt_pav_vertices.extend(_ec)
        for _ring in _pp.interiors:
            _rc = list(_ring.coords)
            if _rc and _rc[0] == _rc[-1]:
                _rc = _rc[:-1]
            apt_pav_vertices.extend(_rc)
    for _rp in runway_polys:
        _rc = list(_rp.exterior.coords)
        if _rc and _rc[0] == _rc[-1]:
            _rc = _rc[:-1]
        apt_pav_vertices.extend(_rc)

    _progress.step()  # [3] Building taxiways & terminals

    # ── Taxi centerlines (apt.dat primary, OSM fallback) ─────────
    # Per user 2026-05-12: apt.dat row 1201/1202 taxi-network is
    # the authoritative source for the taxi graph at airports that
    # have it.  Since the pavement footprint is also drawn from
    # apt.dat row-110 polygons, the taxi-network endpoints align
    # exactly with the pavement boundary — eliminating the OSM-vs-
    # apt.dat boundary mismatch where OSM centerlines clipped
    # against ``pav_union`` produced empty / too-short intersections
    # (CYXY's long E parallel, all of F and G — the user's
    # "taxiways turning into big junctions" report).  Fall back to
    # OSM only when the apt.dat block has no taxi-network at all
    # (some custom packs omit rows 1201/1202).
    # (session 51 per user 2026-05-27) Taxi centerlines come ONLY from
    # apt.dat (rows 1201/1202).  The OSM fallback was REMOVED: OSM
    # geometry didn't align with apt.dat pavement boundaries and produced
    # mis-clipped centerlines.  If apt.dat is missing a taxi network the
    # build will have no taxiway rects — fix the apt.dat input, don't
    # synthesise from OSM.
    apt_centerlines = APR.taxi_centerlines(
        apt, to_m, rwy_centerlines=rwy_centerlines)
    osm_centerlines = apt_centerlines  # legacy name retained; see below
    if apt_centerlines:
        UI.vprint(1,
            f"  [pav-builder] {icao}: using {len(apt_centerlines)} "
            f"apt.dat taxi-network centerline(s) "
            f"({len(apt.taxi_nodes)} nodes, "
            f"{len(apt.taxi_edges)} edges).")
        # ICAO taxiway size now travels PER-SEGMENT on each ``TaxiCenterline``
        # (``seg_sizes``, from the row-1202 edge ``kind``) — there is no name→
        # letter table; size consumers read it off the geometry (user 2026-06-29).
        # Runway THRESHOLDS (both ends of each runway, layout-local metres) —
        # the hard anchors the building route-feasibility metric routes to
        # (P4, building_feasibility.py).  to_m(lon, lat) -> (x, y).
        layout.runway_thresholds = []
        for _r in apt.runways:
            layout.runway_thresholds.append(to_m(_r.lon_a, _r.lat_a))
            layout.runway_thresholds.append(to_m(_r.lon_b, _r.lat_b))
    else:
        # No 1201/1202 network — fall back to the airport's PAINTED
        # taxiway centerlines (row 120, paint code 1/7/51/57).  Small
        # Global Airports fields routinely ship only the painted
        # lines; they carry the authored bezier curves and connect
        # aprons to the runway, where strip discovery reconstructs a
        # much cruder network (user 2026-06-11; KOQN).  Gated by
        # PAINTED_CENTERLINE_FALLBACK; airports WITH a network are
        # untouched (cross-referencing painted geometry against the
        # network is a separate, future step).
        from .config import PAINTED_CENTERLINE_FALLBACK
        painted_cl: list = []
        if PAINTED_CENTERLINE_FALLBACK:
            painted_cl = APR.painted_taxi_centerlines(
                apt, to_m,
                pavement_union_m=pav_union,
                runway_union_m=layout.runway_union)
        if painted_cl:
            osm_centerlines = painted_cl
            UI.vprint(1,
                f"  [pav-builder] {icao}: no apt.dat taxi network — "
                f"using {len(painted_cl)} painted (row-120) taxiway "
                f"centerline(s) instead.")
        else:
            UI.vprint(1,
                f"  [pav-builder] {icao}: apt.dat has no taxi network "
                f"(and no usable painted centerlines) — no taxi rects "
                f"will be emitted (fix the apt.dat input).")
    # Preserve the full input centerline set for the apron-
    # reclassification pass (junction_repair).  Surviving rect
    # ``source_axis`` lines cover only the part of the network
    # that emitted as taxi rects; centerlines absorbed into
    # junction polygons or dropped during decomposition are no
    # longer reachable from layout.shapes.
    layout.apt_taxi_centerlines = list(osm_centerlines)

    # Painted (row-120) taxiway centerlines carry the authored BEZIER
    # curves that the straight 1201/1202 edges lack — continuous arcs
    # through junctions where the routing graph is disconnected at the
    # node.  The fallback above only used them when the 1201/1202 network
    # was ABSENT; when the junction-spine is on, compute them HERE even
    # WITH a network and stash them for the spine, which prefers the
    # curves and fills any gaps from the 1201/1202 edges (junction_spine.
    # _full_centerlines).  Gated, so gate-off stays byte-identical; only
    # needed in the WITH-network case (no-network already left the painted
    # curves in ``apt_taxi_centerlines``).
    from .config import JUNCTION_CENTERLINE_SPINE as _JCS
    if _JCS and apt.taxi_edges:
        try:
            _painted = APR.painted_taxi_centerlines(
                apt, to_m, pavement_union_m=pav_union,
                runway_union_m=layout.runway_union)
            layout._painted_centerlines = [ln for ln, _nm in _painted]
        except Exception:
            layout._painted_centerlines = []

    # RAW painted lines in METERS (ALL row-120 features, paint codes IGNORED — a
    # code marks centerline OR edge line unreliably, so the taxi-fillet extractor
    # discriminates GEOMETRICALLY: a fillet rides the route centerlines, an edge
    # line sits a half-width off).  Populated whenever painted lines exist.
    try:
        from shapely.geometry import LineString as _LS_pl
        layout._painted_lines_m = [
            _LS_pl([to_m(lo, la) for lo, la in pl.line.coords])
            for pl in (apt.painted_lines or [])
            if pl.line is not None and len(pl.line.coords) >= 2]
    except Exception:
        layout._painted_lines_m = []

    # RECOGNIZED CURVED CENTERLINES (user 2026-06-30, gate O4_RECOGNIZED_CENTERLINES):
    # swap each straight taxi route for the painted centerline that rides it (real
    # curves), before the rects / spine consume ``apt_taxi_centerlines``.  Routes
    # with no riding painted centerline keep their raw straight geometry.
    try:
        from .centerline_recognition import recognize_curved_centerlines
        recognize_curved_centerlines(layout, icao)
        # ONE source: when recognition ran, the taxi RECTS must be built from the
        # SAME recognized centerlines the spine uses — not the separate straight
        # ``osm_centerlines`` (user 2026-07-01: no multiple/conflicting sources).
        if os.environ.get("O4_RECOGNIZED_CENTERLINES", "0") == "1":
            osm_centerlines = list(layout.apt_taxi_centerlines)
    except Exception:
        UI.vprint(1, f"  [pav-builder] {icao}: curved-centerline recognition "
                  f"skipped (error).")

    # apt.dat ramp starts (stands) + ground-vehicle service roads.
    # 1206 service roads become 4 %-grade ``service_road`` rects when the
    # feature is enabled.  Parsing the apt.dat 1206 centerlines is skipped
    # while service roads are disabled (don't derive routes we won't use).
    # SERVICE_ROAD_CARVE (s79, docs/service_road_carve.md) needs the same
    # merged 1206 centerlines for its ON-pavement road detection.
    if ENABLE_SERVICE_ROADS or SERVICE_ROAD_CARVE:
        layout.apt_service_centerlines = APR.service_road_centerlines(apt, to_m)
        # Two one-way truck routes on ONE road (or a loop's out-and-back
        # legs) collapse onto a single shared line until they diverge —
        # one spine down the middle instead of a per-leg ridge (user
        # 2026-07-04, CYXY 'Crew cars').
        if os.environ.get("O4_MERGE_PARALLEL_SVC", "1") == "1":
            _n_par = APR.snap_parallel_service_runs(
                layout.apt_service_centerlines)
            if _n_par:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: merged {_n_par} parallel "
                    f"truck-route run(s) onto a single line.")
        if layout.apt_service_centerlines:
            UI.vprint(1,
                f"  [pav-builder] {icao}: "
                f"{len(layout.apt_service_centerlines)} service-road "
                f"centerline(s) ({len(apt.truck_edges)} truck edges).")

    # ── Terminal groundside-pavement subtraction (user 2026-04-29):
    # remove curbside / drop-off / parking pavement from pav_union
    # before downstream rect / junction construction sees it.
    # Groundside pavement sits at a different elevation than the
    # building's airside apron, so allowing it to become an apron
    # junction grade-clamps the building to the wrong altitude.
    # Subtract a perpendicular outward strip from each terminal
    # building's groundside edges (classified by OSM aeroway /
    # highway adjacency + apt.dat-pavement connectivity).
    try:
        _osm_terminal_buildings = _extract_osm_terminals(
            nodes, ways, relations, to_m)
        # Per user 2026-04-30 (CYXY -10123 NW phantom groundside):
        # pass the FULL apt.dat pavement polygon list so the
        # classifier can BFS from runway-touching polys through
        # transitive touches.  Without this, edges of the
        # terminal next to apron pavement that's not directly
        # touching a runway (e.g. the apron extends NW past the
        # terminal) classified UNKNOWN and got mis-promoted to
        # groundside.
        # Connectivity indicator fallback (KCLT, 2026-07-07): payware
        # packs that draw ALL pavement in the DSF ship an apt.dat with
        # zero row-110 polygons, so the apt-only snapshot is empty and
        # the airside-reachability BFS degenerates to "within 100 m of
        # a runway" — never true at a terminal.  Fall back to the full
        # pavement list (which includes the DSF polygons) so the BFS
        # still has a chain to walk; airports with real row-110
        # pavement keep the apt-only list, exactly as before.
        _ground_zone = _terminal_groundside_zone(
            _osm_terminal_buildings, nodes, ways, to_m,
            apt_pavement_seeds=runway_polys,
            apt_pavement_polys=(apt_only_pav_polys or pav_polys),
            relations=relations)
        # O4_COVERAGE_PROBE at the ground-zone boundary: report, per probe
        # point, whether the PRE-subtraction pav_union covers it and whether
        # the ground zone claims it — the earliest coverage handoff, before
        # the slice ever sees the union (a point can only be lost downstream
        # of wherever this says it still exists).
        _cpz = os.environ.get("O4_COVERAGE_PROBE")
        if _cpz:
            try:
                from shapely.geometry import Point as _CpPt
                for _tok in _cpz.split(";"):
                    _la, _lo = (float(v) for v in _tok.split(","))
                    _px, _py = to_m(_lo, _la)
                    _in_pu = (pav_union is not None
                              and pav_union.covers(_CpPt(_px, _py)))
                    _in_gz = (_ground_zone is not None
                              and not _ground_zone.is_empty
                              and _ground_zone.covers(_CpPt(_px, _py)))
                    print(f"  [gz-probe] ({_la:.5f},{_lo:.5f}): "
                          f"pav_union(pre-subtract)={_in_pu} "
                          f"ground_zone={_in_gz}")
            except Exception as _e:
                print(f"  [gz-probe] ERROR {_e!r}")
        if (_ground_zone is not None and not _ground_zone.is_empty
                and pav_union is not None and not pav_union.is_empty):
            # ``pav_union`` is None when the airport has no apt.dat
            # pavement polygons at all (OSM-terminal-only fields, e.g.
            # in tile +44-094) — there is then nothing to intersect /
            # subtract the groundside zone against, so skip the whole
            # capture rather than crash on ``None.intersection`` (the
            # downstream ``pav_union.difference`` would fail too).
            # Capture the groundside-only visible pavement BEFORE
            # the subtraction below empties pav_union of it.  Per
            # user 2026-04-29: groundside pavement should remain
            # in the output but follow DEM (with a 0.1 m gap from
            # the terminal building) rather than being flattened
            # to airside-apron elevation.  The shapes captured
            # here are emitted later with per-vertex DEM altitudes;
            # the 0.1 m terminal gap is enforced inside the emit
            # function using the LAYOUT's terminal shapes (which
            # may differ slightly from the OSM source extracts due
            # to apt.dat row-110 / DSF residue absorption).
            try:
                _groundside_visible = pav_union.intersection(
                    _ground_zone)
                _gs_polys: List[Polygon] = []
                if _groundside_visible is not None:
                    if _groundside_visible.geom_type == "Polygon":
                        if (not _groundside_visible.is_empty
                                and _groundside_visible.area >= 5.0):
                            _gs_polys.append(_groundside_visible)
                    elif (_groundside_visible.geom_type
                            == "MultiPolygon"):
                        for _g in _groundside_visible.geoms:
                            if (_g.geom_type == "Polygon"
                                    and not _g.is_empty
                                    and _g.area >= 5.0):
                                _gs_polys.append(_g)
                # Stash on the layout so the elevation pass can
                # find them once DEM is loaded.  (Apron-island
                # absorption — pieces wrongly carved out of the apron by
                # the groundside strip — happens later in
                # ``_emit_groundside_pavement_dem`` where the built apron
                # shapes exist to measure enclosure against; pav_union is
                # one undifferentiated blob here so apron vs road can't be
                # told apart yet.)
                layout._groundside_polys = _gs_polys
            except _GEOM_EXC:
                layout._groundside_polys = []
            try:
                pav_union = pav_union.difference(_ground_zone)
                if hasattr(layout, "_pav_union_for_rects"):
                    layout._pav_union_for_rects = (
                        layout._pav_union_for_rects.difference(
                            _ground_zone))
                # Also subtract from the granular polygon list so
                # downstream apron-merged-runway / rect-corner
                # detection sees a consistent pavement footprint.
                _new_pav_polys: List[Polygon] = []
                for _p in pav_polys:
                    try:
                        _q = _p.difference(_ground_zone)
                    except _GEOM_EXC:
                        _new_pav_polys.append(_p)
                        continue
                    if _q.is_empty:
                        continue
                    if _q.geom_type == "Polygon":
                        _new_pav_polys.append(_q)
                    elif _q.geom_type == "MultiPolygon":
                        for _g in _q.geoms:
                            if (_g.geom_type == "Polygon"
                                    and not _g.is_empty
                                    and _g.area >= 1.0):
                                _new_pav_polys.append(_g)
                pav_polys[:] = _new_pav_polys
                # Same for apt_only_pav_polys (used for terminal
                # containment + rect-corner snapping).
                _new_apt_only: List[Polygon] = []
                for _p in apt_only_pav_polys:
                    try:
                        _q = _p.difference(_ground_zone)
                    except _GEOM_EXC:
                        _new_apt_only.append(_p)
                        continue
                    if _q.is_empty:
                        continue
                    if _q.geom_type == "Polygon":
                        _new_apt_only.append(_q)
                    elif _q.geom_type == "MultiPolygon":
                        for _g in _q.geoms:
                            if (_g.geom_type == "Polygon"
                                    and not _g.is_empty
                                    and _g.area >= 1.0):
                                _new_apt_only.append(_g)
                apt_only_pav_polys[:] = _new_apt_only
                # Also subtract from apron_candidates — captured at
                # line 2519 BEFORE this subtract — so apron-junction
                # construction in _compute_elevations can't wrap
                # around the terminal into the groundside zone (per
                # user 2026-04-29 / way -10125 vs -10111: the apron
                # junction was extending past the terminal to the
                # upper-side roads, sharing an edge with the new
                # DEM-following groundside pavement and creating a
                # 7 m vertical cliff at CYXY).
                _new_apron_cand: List[Polygon] = []
                for _p in apron_candidates:
                    try:
                        _q = _p.difference(_ground_zone)
                    except _GEOM_EXC:
                        _new_apron_cand.append(_p)
                        continue
                    if _q.is_empty:
                        continue
                    if _q.geom_type == "Polygon":
                        _new_apron_cand.append(_q)
                    elif _q.geom_type == "MultiPolygon":
                        for _g in _q.geoms:
                            if (_g.geom_type == "Polygon"
                                    and not _g.is_empty
                                    and _g.area >= 1.0):
                                _new_apron_cand.append(_g)
                apron_candidates[:] = _new_apron_cand
                try:
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: subtracted "
                        f"{_ground_zone.area:,.0f} m² of "
                        f"groundside pavement (terminal "
                        f"curbside / drop-off / parking).")
                except _GEOM_EXC:
                    pass
            except _GEOM_EXC:
                pass
    except _GEOM_EXC:
        pass

    # ── Discover unreferenced taxiways (no centerline/ref) ───────
    # Strip-shaped pavement that carries no apt.dat/OSM centerline (common at
    # small/remote airports) otherwise dissolves into all-pair junction/apron
    # residue.  Synthesise a centerline for each such strip on the raw
    # pav_union and add it to the network, so the SINGLE _build_taxi_rects
    # pass below turns it into an axial taxi rect like any referenced taxiway.
    # The builder's long-edge-at-boundary + apron-interior gates ensure only
    # strips with nothing along their sloping edge survive as rects.
    if ENABLE_DISCOVERED_TAXIWAYS and pav_union is not None \
            and not pav_union.is_empty:
        from .pavement.discovered_taxiways import (
            discover_unreferenced_centerlines)
        _disc_bld_polys = [p for p in dsf_building_polys
                           if p is not None and not p.is_empty]
        try:                          # OSM terminals/hangars (meters) too — a
            _disc_bld_polys += [p for p in _osm_terminal_buildings  # noqa: F821
                                if p is not None and not p.is_empty]
        except NameError:
            pass
        _disc_bld_u = None
        if _disc_bld_polys:
            try:
                _disc_bld_u = unary_union(_disc_bld_polys)
            except Exception:
                _disc_bld_u = None
        _discovered = discover_unreferenced_centerlines(
            pav_union, osm_centerlines, rwy_centerlines,
            runway_union=layout.runway_union, building_union=_disc_bld_u)
        if _discovered:
            osm_centerlines = list(osm_centerlines) + _discovered
            # Persist the discovered (unreferenced TX) centerlines so the
            # junction-spine pass can densify the FULL route graph through
            # junctions.  ``layout.apt_taxi_centerlines`` was snapshotted
            # ABOVE (before discovery), so it carries only apt.dat refs;
            # the spine pass unions these in (see junction_spine.py).
            layout._discovered_centerlines = list(_discovered)
            UI.vprint(1,
                f"  [pav-builder] {icao}: discovered "
                f"{len(_discovered)} unreferenced taxiway centerline(s).")

    # ── Project the SPINE model (TaxiCenterline, connectivity + per-segment size,
    # already snapshotted onto ``layout.apt_taxi_centerlines`` above) down to the
    # ``(line, name)`` tuples the RECT-BUILDING pipeline consumes (user 2026-06-29).
    # The rect builder + its trimming/splitting transforms only need geometry +
    # label, not size, so they stay tuple-based; the size model lives on
    # ``layout.apt_taxi_centerlines`` for the grade solve.
    osm_centerlines = [
        (c.line, c.name) if hasattr(c, "line") else c for c in osm_centerlines]

    # ── Per-ref OVERALL chord bearings (pre-split) ───────────────
    # Used by ``_classify_role`` to disambiguate diagonal-overall
    # taxis whose curving ends happen to align near-parallel to
    # the runway locally.  Without this, a B/C/E/G stub at SPJC —
    # which enters the runway at a shallow angle — gets a small
    # post-curve segment classified as PRIMARY_PARALLEL because
    # the segment's local bearing falls inside the 20° parallel
    # window, even though the OVERALL B/C/E/G chord is diagonal.
    # The parent's overall chord bearing is the right reference.
    ref_overall_bearings: Dict[str, float] = {}
    _ref_longest_len: Dict[str, float] = {}
    for _ls, _ref in osm_centerlines:
        if not _ref:
            continue
        if _ls.length <= _ref_longest_len.get(_ref, 0.0):
            continue
        _coords = list(_ls.coords)
        if len(_coords) < 2:
            continue
        _dx = _coords[-1][0] - _coords[0][0]
        _dy = _coords[-1][1] - _coords[0][1]
        if math.hypot(_dx, _dy) < 1.0:
            continue
        ref_overall_bearings[_ref] = (
            math.degrees(math.atan2(_dx, _dy)) % 180.0)
        _ref_longest_len[_ref] = _ls.length

    # ── Primary-parallel SPINE LINES (pre-split) ─────────────────
    # Per user 2026-04-27: identify each primary-parallel taxiway
    # (overall db < 20° to the nearest runway) and build a single
    # extended SPINE LINE running through it.  Diagonal stubs
    # (B/C/D/E/G at SPJC) should END where they cross this spine —
    # not where the diagonal's OSM polyline happens to terminate
    # (which can be inside the apron, past where the parallel
    # ought to "continue" through).  The spine line lets us
    # imagine the parallel's centerline continuing through gaps
    # / aprons in the OSM data, providing a stable inland-side
    # bound for diagonal-stub trimming.
    parallel_spines: List[LineString] = []
    parallel_corridors: List[Tuple[str, Polygon]] = []
    SPINE_EXTEND_M = 800.0  # extend each spine ±800 m past its
                              # OSM-fragment endpoints so it acts
                              # as a guide line through aprons.
    PARALLEL_CORRIDOR_HALF_WIDTH_M = 30.0
                              # half-width of the imagined
                              # primary-parallel pavement corridor.
                              # Used to TRIM diagonal stubs at the
                              # corridor's runway-facing edge — so
                              # B/C/D/E at SPJC stop where they
                              # enter A's pavement (real or
                              # imagined-via-apron) rather than
                              # extending deep into the apron.
                              # 30 m is wider than a typical taxi
                              # half-width (22 m) so the corridor
                              # forgives small OSM/apt.dat
                              # misalignment.
    if rwy_centerlines:
        # Bearing of the FIRST runway centerline; we use it to
        # decide whether a ref qualifies as a primary parallel
        # (db < 20°).  All SPJC runways are parallel so any
        # runway works as the reference.
        _r0 = rwy_centerlines[0]
        _rc = list(_r0.coords)
        _rdx = _rc[-1][0] - _rc[0][0]
        _rdy = _rc[-1][1] - _rc[0][1]
        if math.hypot(_rdx, _rdy) > 1e-6:
            _rwy_bearing = (
                math.degrees(math.atan2(_rdx, _rdy)) % 180.0)
            for _ref, _bearing in ref_overall_bearings.items():
                _db = abs(_bearing - _rwy_bearing)
                _db = min(_db, 180.0 - _db)
                if _db >= 20.0:
                    continue  # not a primary parallel
                # Find the longest OSM centerline for this ref;
                # use its endpoints to define the spine direction
                # and base position.
                best_ls: Optional[LineString] = None
                best_len = 0.0
                for _ls, _r2 in osm_centerlines:
                    if _r2 != _ref:
                        continue
                    if _ls.length > best_len:
                        best_len = _ls.length
                        best_ls = _ls
                if best_ls is None or best_len < 100.0:
                    continue
                _ec = list(best_ls.coords)
                _eax, _eay = _ec[0]
                _ebx, _eby = _ec[-1]
                _edx = _ebx - _eax
                _edy = _eby - _eay
                _emag = math.hypot(_edx, _edy)
                if _emag < 1e-6:
                    continue
                _ux = _edx / _emag
                _uy = _edy / _emag
                _start = (_eax - _ux * SPINE_EXTEND_M,
                          _eay - _uy * SPINE_EXTEND_M)
                _end = (_ebx + _ux * SPINE_EXTEND_M,
                        _eby + _uy * SPINE_EXTEND_M)
                try:
                    _spine = LineString([_start, _end])
                    parallel_spines.append(_spine)
                    # Build the corridor polygon: spine buffered to
                    # PARALLEL_CORRIDOR_HALF_WIDTH_M, square ends so
                    # the corridor's apron-facing extension stays
                    # rectangular.
                    _corridor = _spine.buffer(
                        PARALLEL_CORRIDOR_HALF_WIDTH_M,
                        cap_style=2, join_style=2)
                    if (not _corridor.is_empty
                            and _corridor.geom_type == "Polygon"):
                        parallel_corridors.append((_ref, _corridor))
                except _GEOM_EXC:
                    pass

    # ── Pavement source-of-truth (user 2026-04-28): apt.dat row-110
    # ∪ DSF pavement, period.  Earlier revisions augmented pav_union
    # with a 30 m-wide synthetic buffer around any OSM centerline
    # that didn't intersect apt.dat/DSF; the rationale was to keep
    # rect-extraction working at airports where the OSM taxiway
    # network is more complete than the apt.dat coverage.  That
    # workaround is dropped: OSM centerlines drive WHICH taxiways
    # exist (geometry, ref tag, role), but the actual pavement
    # surface comes from apt.dat ∪ DSF only.  Centerlines without
    # matching apt.dat/DSF coverage produce no rect — that's an
    # apt.dat data gap to be fixed at the source, not papered over
    # with a synthetic strip whose width arbitrarily differs from
    # the OSM-tagged taxi width.

    # ── Terminals: expand OSM building outlines to the containing
    # apt.dat pavement polygon (or buffer if no polygon contains).
    # The target terminal is the "pad" — apt.dat pavement up to the
    # apron boundary.  OSM aeroway=terminal gives the building
    # footprint; we use that as a seed.
    osm_terminal_polys = _extract_osm_terminals(
        nodes, ways, relations, to_m)
    # Union the DSF terminal/hangar building footprints with the OSM
    # outlines (user 2026-06-12).  Stacked / abutting facade pieces are
    # first clustered into one outline per building; the merge PREFERS
    # the DSF (the sim renders the building there) and lets OSM fill any
    # gap the DSF didn't place.  Off (DSF_BUILDINGS=0) → OSM-only, the
    # pre-existing behaviour.
    if DSF_BUILDINGS and dsf_building_polys:
        dsf_seed_polys = _cluster_dsf_building_facades(dsf_building_polys)
        combined_n_before = len(osm_terminal_polys)
        osm_terminal_polys = _combine_building_sources(
            dsf_seed_polys, osm_terminal_polys,
            DSF_BUILDING_OSM_OVERLAP_FRAC)
        UI.vprint(1,
            f"  [pav-builder] {icao}: building sources merged — "
            f"{len(dsf_seed_polys)} DSF building(s) + "
            f"{combined_n_before} OSM → {len(osm_terminal_polys)} "
            f"seed(s) (DSF-preferred).")
    # Building-pad simplification: strip sub-pad noise (closely-spaced
    # OSM vertices and the arc facets left by the DSF facade-cluster
    # snap-buffer) that would only spawn sliver triangles in the
    # eventual ear-clip, while PRESERVING the real building corners.
    # Tolerance is TERMINAL_SIMPLIFY_TOL_M (config; dialled back to
    # 0.5 m so articulated terminal pads keep their genuine corners —
    # user 2026-06-14).
    terminal_polys: List[Polygon] = []
    for otp in osm_terminal_polys:
        # Boundary gate: OSM aeroway=terminal buildings are loaded
        # from a wide region and include neighbouring airports'
        # terminals.  A terminal belongs wholly to one airport, so
        # keep it only when its centroid lies within this airport's
        # row-130 boundary.
        if boundary_gate_m is not None:
            try:
                if not boundary_gate_m.contains(otp.centroid):
                    continue
            except _GEOM_EXC:
                pass
        # Apt.dat-only candidates — DSF polygons (overlays, gap
        # fills) shouldn't compete for terminal-pad selection.
        pad0 = _terminal_pad_from_building(otp, apt_only_pav_polys)
        if pad0 is None:
            continue
        # Absorb finger-pier gate stands into simple pad(s) (all sources);
        # a split pier comes back as one pad per piece.
        for pad in _close_building_outline(pad0):
            try:
                simp = pad.simplify(
                    TERMINAL_SIMPLIFY_TOL_M, preserve_topology=True)
                if (simp.geom_type == "Polygon"
                        and not simp.is_empty
                        and simp.area >= 100.0):
                    pad = simp
            except _GEOM_EXC:
                pass
            terminal_polys.append(pad)
    if osm_terminal_polys:
        UI.vprint(1,
            f"  [pav-builder] {icao}: building pads "
            f"{len(terminal_polys)}/{len(osm_terminal_polys)} kept "
            f"(boundary-gate/area filters).")

    # ── Terminal gap for depressed roads (user 2026-06-10) ───────
    # Where a depressed road (KPHX Sky Harbor Blvd class — a public
    # road passing under aeroway bridges inside the boundary) runs
    # through a terminal pad, the terminal must SPLIT and leave a
    # gap for the road to pass through: carve the road corridor
    # (half-width + 0.5 m clearance) out of the pad and keep each
    # surviving piece as its own terminal.  Runs at construction —
    # pre-solve — because terminals are frozen airside after the
    # solve (geom-guard).  Gated on the airport actually having
    # aeroway bridges so the big_roads OSM layer isn't parsed for
    # the common no-bridge airport.
    from .config import EMIT_DEPRESSED_ROADS as _EMIT_DEPRESSED
    if _EMIT_DEPRESSED and terminal_polys and any(
            _tags.get("aeroway") and _tags.get("bridge", "")
            in ("yes", "viaduct")
            for _wid, _nds, _tags in ways):
        try:
            from .bridges import _depressed_road_corridor_band
            _road_band = _depressed_road_corridor_band(
                layout, xplane_root, icao)
        except _GEOM_EXC:
            _road_band = None
        if _road_band is not None and not _road_band.is_empty:
            _carved: List[Polygon] = []
            _n_split = 0
            for tp in terminal_polys:
                try:
                    if not tp.intersects(_road_band):
                        _carved.append(tp)
                        continue
                    diff = tp.difference(_road_band)
                except _GEOM_EXC:
                    _carved.append(tp)
                    continue
                pieces = [g for g in
                          (diff.geoms if hasattr(diff, "geoms")
                           else [diff])
                          if g.geom_type == "Polygon"
                          and not g.is_empty and g.area >= 100.0]
                if pieces:
                    _carved.extend(pieces)
                    _n_split += 1
                # else: pad entirely inside the corridor — the road
                # wins; the pad is dropped.
            if _n_split:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: split {_n_split} "
                    f"terminal pad(s) around depressed-road "
                    f"corridor(s) ({len(terminal_polys)} → "
                    f"{len(_carved)} pads).")
                terminal_polys = _carved

    terminal_union = (unary_union(terminal_polys)
                      if terminal_polys else None)
    for i, tp in enumerate(terminal_polys):
        # ref "building{N}" (user 2026-06-15): the pad pool now mixes
        # terminals, hangars, and DSF term_bridge slabs, so a generic
        # "building" label is more accurate than "terminal".  The ROLE is
        # already ROLE_BUILDING; only the display ref changes.
        layout.shapes.append(BuiltShape(
            polygon=tp, role=ROLE_BUILDING, ref=f"building{i+1}"))

    # Drop discovered (TX) centerlines that thread THROUGH a building (user
    # 2026-06-26: CYXY TX16).  Run HERE — the FINAL building shapes are now in
    # ``layout.shapes`` (the merge/simplify/gate stages reshape them, so the
    # discovery-time DSF-only set missed the OSM building TX16 crosses).  Removes
    # from BOTH the centerline list (→ no rect) and the persisted discovered set
    # (→ not in the route graph), before rects are built below.
    if ENABLE_DISCOVERED_TAXIWAYS:
        _bld_shapes = [s.polygon for s in layout.shapes
                       if s.role == ROLE_BUILDING and s.polygon is not None
                       and not s.polygon.is_empty]
        _disc_bu = None
        if _bld_shapes:
            try:
                _disc_bu = unary_union(_bld_shapes)
            except _GEOM_EXC:
                _disc_bu = None

        def _tx_thru_bld(entry):
            ls = entry.line if hasattr(entry, "line") else (entry[0] if isinstance(entry, tuple) else entry)
            ref = (entry[1] if isinstance(entry, tuple)
                   and len(entry) > 1 else None)
            if (_disc_bu is None or ls is None or ls.is_empty
                    or not str(ref or "").startswith("TX")):
                return False
            try:
                return ls.intersection(_disc_bu).length > 1.0
            except _GEOM_EXC:
                return False

        _nb = len(osm_centerlines)
        osm_centerlines = [e for e in osm_centerlines if not _tx_thru_bld(e)]
        _dropped_tx = _nb - len(osm_centerlines)
        if getattr(layout, "_discovered_centerlines", None):
            layout._discovered_centerlines = [
                e for e in layout._discovered_centerlines
                if not _tx_thru_bld(e)]
        if _dropped_tx:
            UI.vprint(1, f"  [pav-builder] {icao}: dropped {_dropped_tx} "
                      f"discovered TX centerline(s) crossing a building.")

    # ── Identify junction node CLUSTERS ──────────────────────────
    # Per user 2026-05-12: when the taxi graph comes from apt.dat
    # row 1201/1202, the junction nodes ARE explicit in the data —
    # any taxi node referenced by edges of ≥ 2 distinct names, or
    # by a runway-cross edge, is a chart-level junction.  Use
    # those points so ``_split_centerlines_at_points`` trims the
    # apt.dat centerlines at the right places (otherwise long
    # parallel taxis terminate deep inside apron polygons and
    # ``_snap_corners_to_pavement`` rejects them as
    # apron-interior).  Fall back to OSM topology when apt.dat
    # has no taxi network.
    if apt_centerlines:
        junction_points = APR.taxi_junction_points(apt, to_m)
        # Also add geometric crossing points between centerlines of
        # different names — covers the case where a taxi node was
        # tagged with one name but the connecting edge has another.
        # Same behaviour ``_find_junction_points`` provides for OSM.
        for i in range(len(osm_centerlines)):
            ls1, ref1 = osm_centerlines[i]
            for j in range(i + 1, len(osm_centerlines)):
                ls2, ref2 = osm_centerlines[j]
                if ref1 and ref2 and ref1 == ref2:
                    continue
                try:
                    if not ls1.intersects(ls2):
                        continue
                    inter = ls1.intersection(ls2)
                except _GEOM_EXC:
                    continue
                if inter.is_empty:
                    continue
                if inter.geom_type == "Point":
                    junction_points.append((inter.x, inter.y))
                elif inter.geom_type == "MultiPoint":
                    for p in inter.geoms:
                        junction_points.append((p.x, p.y))
    else:
        # Any OSM node referenced by ≥2 taxi ways is a potential
        # junction point.  Nodes within JUNCTION_CLUSTER_DIST of
        # each other are merged into one cluster (target junctions
        # often span a whole multi-way intersection, not just a
        # single OSM node).
        junction_points = _find_junction_points(
            nodes, ways, to_m, osm_centerlines=osm_centerlines)

    # Width-transition breakpoints: experimented with this
    # (user 2026-05-12 option A) — found that splitting V at
    # detected width transitions creates new V rects in regions
    # where target treats the pavement as junction territory.
    # Those new rects' sloping edges introduce cross-shape
    # altitude-step violations against adjacent junctions
    # (failed pavement-grade test).  Disabled.  V under-
    # segmentation (target 5 rects vs v20 3 rects) remains a
    # known limitation of the apt.dat-derived centerline.
    pass

    # ── Diagonal-stub trim at primary-parallel SPINES ────────────
    # Per user 2026-04-27: a diagonal stub (B/C/D/E/G overall db
    # ∈ [20°, 45°)) should END where its centerline crosses the
    # nearest primary-parallel SPINE LINE — even when the spine
    # passes through an apron with no OSM coverage.  Without this
    # the stub's apron-side OSM endpoint can sit deep inside the
    # apron, producing an over-long rect that the surrounding
    # junction has to wrap around.  Adding the spine intersection
    # to ``junction_points`` lets the existing
    # ``_split_centerlines_at_points`` machinery clip the diagonal
    # at the right place using its standard junction-margin rule.
    if parallel_spines and rwy_centerlines:
        _rwy_first = rwy_centerlines[0]
        _rfc = list(_rwy_first.coords)
        _rfdx = _rfc[-1][0] - _rfc[0][0]
        _rfdy = _rfc[-1][1] - _rfc[0][1]
        _rfmag = math.hypot(_rfdx, _rfdy)
        if _rfmag > 1e-6:
            _rfb = (math.degrees(math.atan2(_rfdx, _rfdy))
                    % 180.0)
            for _ls, _ref in osm_centerlines:
                if not _ref:
                    continue
                _b = ref_overall_bearings.get(_ref)
                if _b is None:
                    continue
                _db = abs(_b - _rfb)
                _db = min(_db, 180.0 - _db)
                # Only diagonal stubs (db ∈ [20°, 45°)) need this.
                if not (20.0 <= _db < 45.0):
                    continue
                # Find the closest spine and intersect.
                best_pt = None
                best_d = float("inf")
                for _spine in parallel_spines:
                    try:
                        _x = _ls.intersection(_spine)
                    except _GEOM_EXC:
                        continue
                    if _x.is_empty:
                        # Try extending the centerline ends to reach
                        # the spine — handles diagonals whose OSM
                        # path stops short of the spine's location.
                        continue
                    if _x.geom_type == "Point":
                        _pt = (_x.x, _x.y)
                    elif _x.geom_type == "MultiPoint":
                        # Pick the intersection furthest from the
                        # closest runway centerline — that's the
                        # apron-side cut we want for trimming.
                        _pt = None
                        _far = -1.0
                        for _g in _x.geoms:
                            _gp = (_g.x, _g.y)
                            try:
                                _gd = min(
                                    Point(_gp).distance(_r)
                                    for _r in rwy_centerlines)
                            except _GEOM_EXC:
                                _gd = 0.0
                            if _gd > _far:
                                _far = _gd
                                _pt = _gp
                        if _pt is None:
                            continue
                    else:
                        continue
                    # Distance along centerline from runway end —
                    # used to pick the closest spine intersection.
                    try:
                        _proj = _ls.project(Point(_pt))
                    except _GEOM_EXC:
                        continue
                    _d = abs(_proj - _ls.length / 2)
                    if _d < best_d:
                        best_d = _d
                        best_pt = _pt
                if best_pt is not None:
                    junction_points.append(best_pt)

    # Trim runway-approaching centerlines at a BUFFERED runway
    # polygon so the last rect on a taxi that crosses or enters
    # the runway stops short of the runway-junction approach.
    # The runway boundary IS an intersection (rule 70 % of gap
    # between intersections), but the physical junction — where
    # the taxi widens into the runway apron — extends some
    # distance OUTSIDE the runway polygon too.  Pulling the
    # centerline end back from the approach widening keeps the
    # rect from "encroaching into intersections" (user
    # 2026-04-21).
    #
    # Two thresholds:
    #   * Perpendicular (perp_diff < 25°): pull back 30 m.  These
    #     taxis hit the runway head-on and the widening is large.
    #   * Diagonal (25° ≤ perp_diff < 70°): pull back 15 m.
    #     The widening is gentler at oblique angles (B/C/D/E/G at
    #     SPJC) but still significant.  Per user 2026-04-27: "B,
    #     C, D, E, G should be handled like V3 stub" — V3 is a
    #     near-perpendicular sub-ref and gets the 30 m buffer; the
    #     diagonals get a smaller buffer so they don't over-shrink
    #     while still getting the same kind of pull-back.
    RWY_JUNCTION_BUFFER_M = _RWY_JUNCTION_BUFFER_M
    RWY_DIAG_BUFFER_M = _RWY_DIAG_BUFFER_M
    PERP_TRIM_MAX_DEG = 25.0
    DIAG_TRIM_MAX_DEG = 70.0
    if (layout.runway_union is not None
            and not layout.runway_union.is_empty
            and rwy_centerlines):
        try:
            rwy_buffered = layout.runway_union.buffer(
                RWY_JUNCTION_BUFFER_M)
        except _GEOM_EXC:
            rwy_buffered = layout.runway_union
        # Nearest-runway bearing for angle check
        def _perp_diff_to_runway(ls: LineString) -> float:
            c = list(ls.coords)
            if len(c) < 2:
                return 90.0
            dx = c[-1][0] - c[0][0]
            dy = c[-1][1] - c[0][1]
            mag = math.hypot(dx, dy)
            if mag < 1e-6:
                return 90.0
            ax_bearing = math.degrees(
                math.atan2(dx, dy)) % 180.0
            mid = ls.interpolate(ls.length / 2)
            best = min(rwy_centerlines,
                       key=lambda r: mid.distance(r))
            rc = list(best.coords)
            rx = rc[-1][0] - rc[0][0]
            ry = rc[-1][1] - rc[0][1]
            rmag = math.hypot(rx, ry)
            if rmag < 1e-6:
                return 90.0
            rwy_bearing = math.degrees(
                math.atan2(rx, ry)) % 180.0
            delta = abs(ax_bearing - rwy_bearing)
            delta = min(delta, 180.0 - delta)
            return abs(delta - 90.0)

        trimmed_centerlines: List[Tuple[LineString, str]] = []
        for ls, ref in osm_centerlines:
            # Per user 2026-05-12: extend the 30 m runway-buffer
            # pull-back to DIAGONAL centerlines too (was perpendicular-
            # only).  Reason: a diagonal stub's centerline at, say,
            # 27° to the runway axis can have its runway-side endpoint
            # 10-15 m off the runway pavement edge.  Without
            # perpendicular clearance, the rect built from that
            # centerline has its runway-side long-edge snapped onto
            # the runway boundary (SPJC stubs E + G ended up 0.5 m
            # off the runway edge — the snap collapsed the adjacent
            # junction polygon to a sliver).  Apply the 30 m
            # buffer-pullback to everything that's NOT essentially
            # parallel to the runway (perp_diff < 70°); pure
            # parallels skip — they shouldn't shorten by 30 m on
            # their runway-facing end.
            pd = _perp_diff_to_runway(ls)
            if pd >= DIAG_TRIM_MAX_DEG:
                trimmed_centerlines.append((ls, ref))
                continue
            _buf = rwy_buffered
            try:
                diff = ls.difference(_buf)
            except _GEOM_EXC:
                trimmed_centerlines.append((ls, ref))
                continue
            if diff.is_empty:
                trimmed_centerlines.append((ls, ref))
                continue
            if diff.geom_type == "LineString":
                if diff.length >= MIN_SEGMENT_LEN_M:
                    trimmed_centerlines.append((diff, ref))
                else:
                    trimmed_centerlines.append((ls, ref))
            elif diff.geom_type == "MultiLineString":
                longest = max(diff.geoms, key=lambda g: g.length)
                if longest.length >= MIN_SEGMENT_LEN_M:
                    trimmed_centerlines.append((longest, ref))
                else:
                    trimmed_centerlines.append((ls, ref))
            else:
                trimmed_centerlines.append((ls, ref))
        osm_centerlines = trimmed_centerlines

    # Trim PERPENDICULAR centerlines at a BUFFERED PARALLEL-
    # CENTERLINE polygon.  Per user (2026-04-22): at SPLP
    # (unrefed airport), perpendicular stubs come from long
    # multipurpose taxis whose gap is bounded by a POINT
    # crossing with the primary axis.  That crossing doesn't
    # account for the primary's WIDTH — the 70 % rect then
    # extends into the primary's rect zone, reading as
    # "off-center toward the taxiway".  A 15 m buffer around
    # each parallel-to-runway centerline (= primary half-width)
    # pulls the perpendicular stub's primary-side endpoint
    # out to the primary's physical edge.  SPJC doesn't need
    # this because each sub-ref stub has its own dedicated OSM
    # way — the geometry inherently excludes the primary
    # width.
    PARALLEL_BUFFER_M = _PARALLEL_BUFFER_M
    PARALLEL_MIN_LEN_M = 200.0
    if rwy_centerlines:
        parallel_polys = []
        for ls, _ref in osm_centerlines:
            if ls.length < PARALLEL_MIN_LEN_M:
                continue
            # Check if this centerline is PARALLEL to runway
            # (perp_diff > 75°).
            c = list(ls.coords)
            if len(c) < 2:
                continue
            dx = c[-1][0] - c[0][0]
            dy = c[-1][1] - c[0][1]
            mag = math.hypot(dx, dy)
            if mag < 1e-6:
                continue
            ax_bearing = math.degrees(
                math.atan2(dx, dy)) % 180.0
            mid = ls.interpolate(ls.length / 2)
            best_r = min(rwy_centerlines,
                         key=lambda r: mid.distance(r))
            rc = list(best_r.coords)
            rx = rc[-1][0] - rc[0][0]
            ry = rc[-1][1] - rc[0][1]
            rmag = math.hypot(rx, ry)
            if rmag < 1e-6:
                continue
            rwy_bearing = math.degrees(
                math.atan2(rx, ry)) % 180.0
            delta = abs(ax_bearing - rwy_bearing)
            delta = min(delta, 180.0 - delta)
            perp_diff = abs(delta - 90.0)
            if perp_diff > 75.0:
                # Parallel to runway → create buffer polygon
                try:
                    parallel_polys.append(
                        ls.buffer(PARALLEL_BUFFER_M))
                except _GEOM_EXC:
                    pass
        if parallel_polys:
            try:
                parallel_union = unary_union(parallel_polys)
            except _GEOM_EXC:
                parallel_union = None
            if parallel_union is not None and not parallel_union.is_empty:
                def _perp_diff_to_rwy(ls):
                    c = list(ls.coords)
                    if len(c) < 2:
                        return 90.0
                    dx = c[-1][0] - c[0][0]
                    dy = c[-1][1] - c[0][1]
                    mag = math.hypot(dx, dy)
                    if mag < 1e-6:
                        return 90.0
                    ax_b = math.degrees(
                        math.atan2(dx, dy)) % 180.0
                    mid_p = ls.interpolate(ls.length / 2)
                    best = min(rwy_centerlines,
                               key=lambda r: mid_p.distance(r))
                    rc = list(best.coords)
                    rx = rc[-1][0] - rc[0][0]
                    ry = rc[-1][1] - rc[0][1]
                    rmag = math.hypot(rx, ry)
                    if rmag < 1e-6:
                        return 90.0
                    rw_b = math.degrees(
                        math.atan2(rx, ry)) % 180.0
                    dlt = abs(ax_b - rw_b)
                    dlt = min(dlt, 180.0 - dlt)
                    return abs(dlt - 90.0)

                trimmed_perp: List[Tuple[LineString, str]] = []
                for ls, ref in osm_centerlines:
                    # Trim perpendicular AND diagonal centerlines
                    # — both cross the parallel-parallel corridor
                    # and benefit from starting at the primary
                    # EDGE not axis.  Skip only near-parallel ones
                    # (perp_diff >= 75°) which are themselves
                    # primaries.
                    if _perp_diff_to_rwy(ls) >= 75.0:
                        trimmed_perp.append((ls, ref))
                        continue
                    try:
                        diff = ls.difference(parallel_union)
                    except _GEOM_EXC:
                        trimmed_perp.append((ls, ref))
                        continue
                    if diff.is_empty:
                        trimmed_perp.append((ls, ref))
                        continue
                    if diff.geom_type == "LineString":
                        if diff.length >= MIN_SEGMENT_LEN_M:
                            trimmed_perp.append((diff, ref))
                        else:
                            trimmed_perp.append((ls, ref))
                    elif diff.geom_type == "MultiLineString":
                        # Multiple pieces — keep ALL so each stub
                        # between parallels emits separately.
                        for g in diff.geoms:
                            if g.length >= MIN_SEGMENT_LEN_M:
                                trimmed_perp.append((g, ref))
                    else:
                        trimmed_perp.append((ls, ref))
                osm_centerlines = trimmed_perp

    # ── Diagonal-stub corridor trim ──────────────────────────────
    # Per user 2026-04-27: when a diagonal stub (B/C/D/E/G at SPJC)
    # connects directly to a large apron rather than crossing a
    # primary parallel's actual rect, the stub's apron-end ends up
    # floating in the apron (no nearby pavement edge to snap to).
    # The fix: subtract the IMAGINED PRIMARY PARALLEL CORRIDOR
    # (spine ± PARALLEL_CORRIDOR_HALF_WIDTH_M, extended through
    # gaps in the OSM coverage) from each diagonal stub's
    # centerline.  The diagonal then ends at the corridor's
    # runway-facing edge — exactly where the imagined primary
    # parallel's "near" pavement boundary sits.  Downstream rect-
    # build + corner-snap-to-pavement then puts the rect's apron-
    # end corners on the pavement edge between runway and apron.
    if parallel_corridors and ref_overall_bearings:
        _r0c = list(rwy_centerlines[0].coords)
        _r0db = math.degrees(math.atan2(
            _r0c[-1][0] - _r0c[0][0],
            _r0c[-1][1] - _r0c[0][1])) % 180.0
        corridor_trimmed: List[Tuple[LineString, str]] = []
        for ls, ref in osm_centerlines:
            if not ref:
                corridor_trimmed.append((ls, ref))
                continue
            _b = ref_overall_bearings.get(ref)
            if _b is None:
                corridor_trimmed.append((ls, ref))
                continue
            _db_local = abs(_b - _r0db)
            _db_local = min(_db_local, 180.0 - _db_local)
            # Only diagonal stubs (overall db ∈ [20°, 45°)) need
            # this corridor trim.  Sub-refs (V3 etc) would also
            # qualify and benefit, so include them.
            if not (20.0 <= _db_local < 45.0):
                corridor_trimmed.append((ls, ref))
                continue
            # Subtract every OTHER ref's corridor from this
            # centerline.  Skip the centerline's own corridor
            # (so e.g. an A1 stub doesn't subtract A's corridor
            # from itself if it was somehow classified as
            # diagonal).
            # Subtract the union of every OTHER ref's corridor at once.
            # Keep ALL SUBSTANTIAL surviving pieces, not just the longest
            # (user 2026-05-30): a diagonal STUB ends at the apron, so the
            # subtraction leaves one body piece (+ maybe a short float we
            # drop).  But a long diagonal THROUGH-taxiway (HECA R, 1.9 km
            # at db~30°) CROSSES a parallel's corridor in its middle —
            # subtraction leaves TWO long pieces, and keeping only the
            # longest DISCARDED ~670 m of R.  Keeping every piece >=
            # SUBSTANTIAL_PIECE_M preserves the through-taxi (its corridor
            # crossing becomes a junction at the split) while still
            # dropping short apron-floats.
            SUBSTANTIAL_PIECE_M = 150.0
            others = [corr for c_ref, corr in parallel_corridors
                      if c_ref != ref]
            kept_pieces = [ls]
            if others:
                try:
                    diff = ls.difference(unary_union(others))
                except _GEOM_EXC:
                    diff = ls
                if diff.is_empty:
                    kept_pieces = [ls]
                elif diff.geom_type == "LineString":
                    kept_pieces = ([diff]
                                   if diff.length >= MIN_SEGMENT_LEN_M
                                   else [ls])
                elif diff.geom_type == "MultiLineString":
                    subs = [g for g in diff.geoms
                            if g.length >= SUBSTANTIAL_PIECE_M]
                    if len(subs) >= 2:
                        # ≥ 2 substantial pieces survive ⇒ this is a
                        # through-TAXIWAY crossing other corridors in
                        # its middle, NOT a stub ending in one apron.
                        # Subtracting the corridors here leaves a
                        # permanent GAP at each crossing (HECA R lost
                        # ~372 m, coverage 100→88.7 %).  Keep the
                        # centerline WHOLE; the real crossing junctions
                        # are cut downstream by _split_centerlines_at_
                        # points with a small junction margin instead
                        # of a corridor-width hole.
                        kept_pieces = [ls]
                    elif subs:
                        kept_pieces = subs
                    else:
                        longest = max(diff.geoms, key=lambda g: g.length)
                        kept_pieces = ([longest]
                                       if longest.length >= MIN_SEGMENT_LEN_M
                                       else [ls])
            for piece in kept_pieces:
                corridor_trimmed.append((piece, ref))
        osm_centerlines = corridor_trimmed

    # Per user rule (2026-04-18): "Implement splitting at cross ref
    # crossings" — split every centerline at each multi-ref
    # junction node along its path, so each straight section
    # between intersections emits as a single rect.
    # Split per user rules 1+4 (2026-04-20):
    #   Rule 1: stop rects at intersections; cluster close
    #           intersections into single junction region.
    #   Rule 4: cover only narrowest straight sections; widened
    #           pavement belongs to junctions.
    # Uses OSM multi-ref nodes as intersection anchors, gated by
    # gap + pav-width clustering at the midpoint.
    # Uniform pipeline per user (2026-04-20): intersections +
    # sharp curves define break points; 70% rect between
    # consecutive breaks.  No width analysis.
    # Per user 2026-05-05: use the SAME pav_union for rect building
    # and junction emit.  Previously rects used ``pav_union_for_rects``
    # (full runway subtraction) while junctions used ``pav_union``
    # (effective_runway subtraction, retaining apron-merged-runway
    # pavement).  The split was added to fix CYXY's F primary
    # parallel, but it caused SPJC rect corners to land on
    # pav_for_rects.boundary that doesn't exist on pav_union.boundary
    # — leaving thin tabs in the residue.  Single-source-of-truth
    # boundary lets corners snap consistently.
    osm_centerlines = _split_centerlines_at_points(
        osm_centerlines, junction_points, approach_tol_m=25.0,
        pav_union=pav_union, rwy_union=layout.runway_union,
        rwy_centerlines=rwy_centerlines)

    # ── Trim short bend-hooks (straight-piece target) ────────────
    # The hand target represents taxiways as straight pieces; a sharp
    # bend with a short arm is a hook into a junction/apron the target
    # omits.  Keep the long straight run (user 2026-05-31).
    from .pavement.centerlines import _trim_short_bend_hooks
    osm_centerlines = _trim_short_bend_hooks(osm_centerlines)

    # ── (s79) ON-PAVEMENT service-road centerlines (SVC refs) ─────
    # docs/service_road_carve.md: qualifying apt.dat 1206 truck-route
    # runs (narrow dedicated strips, away from terminals) join the
    # centerline set here and ride the SAME rect → junction →
    # absorption decomposition as a taxiway (user 2026-06-11), with
    # role ``service_road`` forced by the SVC ref prefix in
    # ``_build_taxi_rects``.  Late join — after the taxi-specific
    # trim / spine passes (roads are not aircraft taxi paths), before
    # the off-corridor drop and rect construction.
    _svc_widths: Dict[str, float] = {}   # SVC run ref → measured width
    if SERVICE_ROAD_CARVE and pav_union is not None \
            and not pav_union.is_empty:
        _svc_routes = list(getattr(layout, "apt_service_centerlines",
                                   None) or [])
        if _svc_routes:
            from .pavement.service_roads import detect_road_runs
            from .pavement.centerlines import split_merged_centerline
            # Mode-B source polys = apt.dat-AUTHORED pavement only:
            # narrow DSF strips at HECA qualified runs the user ruled
            # NOT roads (2026-06-11 verdict pinned HECA's set); the
            # edge-blended-road case (CYXY pav[1] "New Taxiway 40")
            # is an apt.dat 110 polygon.
            _svc_runs = detect_road_runs(
                _svc_routes, pav_union,
                terminal_polys=terminal_polys,
                runway_union=layout.runway_union,
                source_polys=apt_only_pav_polys)
            from .pavement.service_roads import _split_at_bends as _svc_bends
            _svc_bend_split = (
                os.environ.get("O4_SVC_BEND_SPLIT", "1") == "1")
            _svc_lines: List[Tuple[LineString, str]] = []
            for _k, (_run, _w, _rname) in enumerate(_svc_runs, 1):
                _ref = f"SVC{_k}"
                _svc_widths[_ref] = float(_w)
                for _piece, _pr in split_merged_centerline(
                        _run, _ref, rwy_centerlines):
                    if _piece.is_empty or _piece.length < 1.0:
                        continue
                    # split_merged_centerline's bend-clustering over the long
                    # winding 1206 route can still emit a piece that SPANS a sharp
                    # corner (CYXY 188: a 33° turn mid-piece) — the taxi-rect builder
                    # then lays ONE rect across the curve, drifting off the route into
                    # a skewed wedge.  Bend-split each piece at sharp turns so the
                    # STRAIGHT parts each get a clean even-width rect and the corner
                    # falls out as junction residue (the taxiway model, user
                    # 2026-06-27).  Gate off → single piece.
                    _runs = (_svc_bends(list(_piece.coords))
                             if _svc_bend_split else [list(_piece.coords)])
                    for _sub in _runs:
                        if len(_sub) < 2:
                            continue
                        try:
                            _subln = LineString(_sub)
                        except _GEOM_EXC:
                            continue
                        if _subln.is_empty or _subln.length < 1.0:
                            continue
                        _svc_lines.append((_subln, _ref))
            if _svc_lines:
                # ★ 1206 provenance vs DISCOVERED (TX) lanes resolves at
                # the rect OVERLAP pass (SVC beats TX there), NOT here:
                # a feed-time line yield killed lanes whose road rect
                # then failed to emit (coverage/degenerate drops) —
                # SPJC lost TX6/TX9 secondary_parallels to roads that
                # never materialised.  Arbitrating on EMITTED geometry
                # keeps whichever surface actually exists.
                _n_tx_dropped = 0
                osm_centerlines = list(osm_centerlines) + _svc_lines
                # Reclassification / repair passes measure junction
                # territory against the FULL preserved centerline set —
                # roads included, so the road-junction territory at
                # bends (the #198 U-turn) stays junction, not apron.  On the
                # SPINE model these are TaxiCenterline(is_service=True) so the
                # taxi-spine consumers skip them by flag (not a name prefix).
                layout.apt_taxi_centerlines = (
                    list(layout.apt_taxi_centerlines)
                    + [APR.TaxiCenterline(line=_ln, is_service=True, name=_rf,
                                          seg_sizes=[""] * max(0, len(_ln.coords) - 1))
                       for (_ln, _rf) in _svc_lines])
                UI.vprint(1,
                    f"  [pav-builder] {icao}: {len(_svc_lines)} "
                    f"service-road centerline piece(s) from "
                    f"{len(_svc_runs)} qualifying 1206 run(s)"
                    + (f"; {_n_tx_dropped} discovered TX line(s) "
                       f"yielded to road provenance."
                       if _n_tx_dropped else "."))

    # ── Drop runway-crossing + junction-buried centerlines ────────
    # A taxi centerline whose body lies inside the runway (the runway
    # emit covers that surface) or buried in the middle of a wide
    # junction/apron (no narrow corridor) does not correspond to a
    # taxiway rect — drop it before rect construction (user 2026-05-31).
    from .pavement.centerlines import _drop_offcorridor_centerlines
    _n_before = len(osm_centerlines)
    # (s79) SVC road lines are EXEMPT from the off-corridor drop:
    # `detect_road_runs` already qualified them, and an edge-blended
    # road (its own strip ALONG apron pavement) reads "junction-buried"
    # to the corridor test even though it is a real road lane.
    _svc_keep = [(c, r) for (c, r) in osm_centerlines
                 if str(r or "").startswith("SVC")]
    _non_svc = [(c, r) for (c, r) in osm_centerlines
                if not str(r or "").startswith("SVC")]
    _non_svc, _n_rwy, _n_buried = _drop_offcorridor_centerlines(
        _non_svc, pav_union, layout.runway_union)
    osm_centerlines = _non_svc + _svc_keep
    if _n_rwy or _n_buried:
        UI.vprint(1,
            f"  [pav-builder] {icao}: dropped {_n_rwy} runway-crossing + "
            f"{_n_buried} junction-buried centerline(s) "
            f"(of {_n_before}).")

    # ── Canonical-point registry (user 2026-05-18) ────────────────
    # Build the shared registry now, before any rect / junction
    # construction, and store on the layout so every downstream
    # pass that creates or modifies a polygon vertex resolves
    # through it.  Seeded with apt.dat row-110 pavement vertices
    # and runway corners — the immutable input geometry — so the
    # registry's "first wins" rule starts from real apt.dat data
    # rather than from whichever rect happens to register first.
    from .canonical_points import CanonicalPointRegistry
    from .layout import SHARED_VERTEX_TOL_M
    layout.canonical_points = CanonicalPointRegistry(
        tol_m=SHARED_VERTEX_TOL_M)
    layout.canonical_points.seed(layout.apt_pavement_vertices)
    if layout.runway_union is not None and not layout.runway_union.is_empty:
        try:
            _ru = layout.runway_union
            for _rp in (_ru.geoms
                        if _ru.geom_type == "MultiPolygon" else [_ru]):
                if _rp.geom_type != "Polygon":
                    continue
                _ext = list(_rp.exterior.coords)
                if _ext and _ext[0] == _ext[-1]:
                    _ext = _ext[:-1]
                layout.canonical_points.seed(_ext)
        except _GEOM_EXC:
            pass

    # ── (s81) Taxilanes stop at building edges ───────────────────
    # User ruling 2026-06-12: a lane that intersects a building pad
    # (terminal / hangar) ends AT the pad boundary and welds to it —
    # shared edge/nodes/elevation, like rects ending against aprons.
    # Trim the rect-feeding centerlines against the pad union so no
    # rect is ever built over (or through) a building.  The FULL
    # centerline set stashed on ``layout.apt_taxi_centerlines``
    # stays untrimmed — route-graph laws still measure the route
    # up to the building.
    if HANGAR_PADS_GATE and terminal_union is not None \
            and not terminal_union.is_empty:
        from .terminals import trim_centerlines_at_buildings
        osm_centerlines, _n_cl_trim = trim_centerlines_at_buildings(
            osm_centerlines, terminal_union)
        if _n_cl_trim:
            UI.vprint(1,
                f"  [pav-builder] {icao}: trimmed {_n_cl_trim} "
                f"centerline(s) at building-pad edges.")

    _progress.step()  # [4] Building taxi rects, junctions & service roads

    # ── Build taxi rects from centerlines ────────────────────────
    taxi_rects = _build_taxi_rects(
        osm_centerlines, pav_union, layout.runway_union,
        rwy_centerlines, apt_vertices=apt_pav_vertices,
        ref_overall_bearings=ref_overall_bearings,
        registry=layout.canonical_points,
        svc_widths=_svc_widths or None)

    # (s81) Building pads win rect overlap: an axis trimmed at the
    # pad boundary can still sweep a rect corner INTO the pad when
    # the building edge is oblique to the lane.  Clip each rect
    # against the pad union (largest surviving piece); the
    # degenerate sub-quad drop below then culls what no longer has
    # a usable 4-corner footprint.
    if HANGAR_PADS_GATE and terminal_union is not None \
            and not terminal_union.is_empty:
        _pad_clipped: list = []
        _n_rect_clip = 0
        for _re in taxi_rects:
            _rp = _re[0]
            try:
                if _rp is None or _rp.is_empty \
                        or not _rp.intersects(terminal_union):
                    _pad_clipped.append(_re)
                    continue
                _diff = _rp.difference(terminal_union)
            except _GEOM_EXC:
                _pad_clipped.append(_re)
                continue
            _n_rect_clip += 1
            if _diff.is_empty:
                continue  # rect entirely under the building
            if _diff.geom_type == "MultiPolygon":
                _diff = max(_diff.geoms, key=lambda g: g.area)
            if _diff.geom_type != "Polygon" or _diff.is_empty:
                continue
            _pad_clipped.append((_diff,) + tuple(_re[1:]))
        if _n_rect_clip:
            UI.vprint(1,
                f"  [pav-builder] {icao}: clipped {_n_rect_clip} "
                f"taxi rect(s) against building pads.")
        taxi_rects = _pad_clipped

    # Drop DEGENERATE clipped taxi rects (user 2026-06-01).  The rect builder
    # sizes each rect to the local pavement width and clips it to the pavement
    # boundary.  Where a centerline segment runs through the tapering EDGE of
    # an apron, the clip collapses the rect to a TRIANGLE (one short edge
    # shrinks to a point) — a thin sliver that is not a usable taxi corridor
    # and, kept, becomes a sloping stub hugging the apron's long edge at a
    # different level (HECA J1: a 978 m² triangle → a 3.4 m cliff vs the
    # apron, and ~41 edge-steps).  A valid taxi rect has 4 corners; a clipped
    # corner yields a pentagon/hexagon (≥ 4, kept).  Only sub-quad shapes are
    # degenerate.  Dropping loses no coverage — apron = pav_union − rects, so
    # the footprint simply becomes apron residue.
    def _distinct_corners(poly, tol_m: float = 0.5) -> int:
        try:
            c = list(poly.exterior.coords)
        except _GEOM_EXC:
            return 0
        if c and c[0] == c[-1]:
            c = c[:-1]
        # Merge consecutive near-coincident vertices so a collapsed (≈0-length)
        # edge counts as one corner — a quad with a vanished end edge IS a
        # triangle.
        merged: list = []
        for p in c:
            if not merged or math.hypot(p[0] - merged[-1][0],
                                        p[1] - merged[-1][1]) > tol_m:
                merged.append(p)
        if (len(merged) > 1
                and math.hypot(merged[0][0] - merged[-1][0],
                               merged[0][1] - merged[-1][1]) <= tol_m):
            merged.pop()
        return len(merged)

    _n_before = len(taxi_rects)
    taxi_rects = [e for e in taxi_rects
                  if e[0] is not None and not e[0].is_empty
                  and _distinct_corners(e[0]) >= 4]
    _n_degen = _n_before - len(taxi_rects)
    if _n_degen:
        UI.vprint(1, f"  [pav-builder] {icao}: dropped {_n_degen} degenerate "
                     f"(sub-quad) clipped taxi rect(s) → apron residue.")

    # Scope discovery to clean free-strip lanes: drop discovered (TX) rects
    # the builder left sheared — those are apron-EMBEDDED lanes (snap fit the
    # rect to the ragged apron boundary).  They stay as residue and are handled
    # by the Phase-2 apron narrow-neck decomposition, not rammed through here.
    if ENABLE_DISCOVERED_TAXIWAYS:
        from .pavement.discovered_taxiways import (
            reject_curved_discovered_rects)
        taxi_rects, _n_curved = reject_curved_discovered_rects(taxi_rects)
        if _n_curved:
            UI.vprint(1,
                f"  [pav-builder] {icao}: deferred {_n_curved} apron-embedded "
                f"discovered taxi rect(s) to residue (Phase 2).")

    # Filter stubs by user's runway-connection rule: a stub rect is
    # kept only if its OSM centerline reaches a runway.  Stubs whose
    # pavement is only between apron and parallel (or apron-internal)
    # get dropped — that pavement folds into the apron or junction.
    # This removes OSM sub-refs like A1-A6, D1/D2, F1, M1-M3, R1/R2, N
    # that the user's target doesn't emit.
    # Look up raw OSM ways per ref (untrimmed).  A stub "connects to
    # a runway" iff one of its raw endpoints lies within
    # RUNWAY_ENDPOINT_DIST_M of the runway footprint.  The threshold
    # accounts for the junction polygon between the stub rect and
    # the runway: target stub-rect corners are 22–66 m from the
    # runway edge (measured from the actual target), so 80 m gives
    # a small margin for freehand drift.
    #
    # Per user 2026-05-11: SKIP this filter when the taxi graph
    # comes from apt.dat (rows 1201/1202).  apt.dat doesn't carry
    # OSM-noise sub-refs; every row-1202 edge is a canonical taxi
    # path.  CYXY's G runs apron-to-apron without ever touching
    # a runway — under OSM-only filtering it gets dropped here as
    # "apron-internal" even though apt.dat declares it a real
    # taxiway.  The downstream absorption pass
    # (``_drop_primary_parallels_embedded_in_pavement``) is the
    # authoritative place to decide whether an apron-running taxi
    # rect should be partially absorbed or kept intact.
    RUNWAY_ENDPOINT_DIST_M = 80.0
    raw_endpoints_by_ref: Dict[str, List[Tuple[float, float]]] = {}
    if not apt_centerlines:
        for wid, nds, tags in ways:
            if tags.get("aeroway") != "taxiway":
                continue
            ref = tags.get("ref", "")
            pts = []
            for n in nds:
                if n in nodes:
                    lat, lon = nodes[n]
                    pts.append(to_m(lon, lat))
            if len(pts) >= 2:
                raw_endpoints_by_ref.setdefault(ref, []).append(pts[0])
                raw_endpoints_by_ref.setdefault(ref, []).append(pts[-1])

    if not apt_centerlines and layout.runway_union is not None:
        rwy_boundary = layout.runway_union.boundary
        filtered: List[Tuple[Polygon, LineString, str, str]] = []
        for rect, axis, role, ref in taxi_rects:
            if role != ROLE_STUB:
                filtered.append((rect, axis, role, ref))
                continue
            # Reject stubs whose RECT touches (or sits inside) the
            # runway polygon.  SPLP has a short unrefed taxi at the
            # SW end whose rect at (-463,-1266) has one corner
            # exactly on the runway boundary — user wants "only a
            # single stub at the south end" so this one must go.
            try:
                if rect.distance(rwy_boundary) < 5.0 and (
                        rect.intersects(layout.runway_union)
                        or rect.distance(layout.runway_union) < 2.0):
                    continue
            except _GEOM_EXC:
                pass
            reaches_runway = False
            for (px, py) in raw_endpoints_by_ref.get(ref, []):
                if Point(px, py).distance(
                        layout.runway_union) <= RUNWAY_ENDPOINT_DIST_M:
                    reaches_runway = True
                    break
            if reaches_runway:
                filtered.append((rect, axis, role, ref))
        taxi_rects = filtered

    # ── Runway-end stubs for primary parallels ─────────────────────
    # Per user (2026-04-21): primary parallels whose OSM path
    # terminates INSIDE the runway polygon (i.e. the taxi merges
    # onto runway pavement) should emit a wider-than-normal STUB
    # at the transition — the "ramp" where A / F meet the runway
    # short-edge.  Target A stub (688,1562) L=79 W=73 and F stub
    # (2243,-1666) L=93 W=78 both sit ~100 m out from the runway
    # polygon boundary.  Add an extra stub rect at the path vertex
    # just OUTSIDE the runway along the path.
    # Per user 2026-05-14: prefer apt.dat taxi-network as the
    # authoritative source for runway-end stub detection.  Build
    # one linemerged polyline per apt.dat taxi name (UNTRIMMED —
    # the trimming in ``taxi_centerlines`` runs ``split_merged_
    # centerline`` which curve-skips at runway-end transitions,
    # losing exactly the endpoints this function needs to detect).
    # The function falls back to OSM ways when apt.dat is absent.
    apt_merged_polylines: Optional[
        List[Tuple[LineString, str]]] = None
    if apt is not None and apt.taxi_nodes and apt.taxi_edges:
        from shapely.ops import linemerge as _linemerge
        by_name_segs: Dict[str, List[LineString]] = {}
        for edge in apt.taxi_edges:
            if edge.kind == "runway":
                continue
            if (edge.node_from not in apt.taxi_nodes
                    or edge.node_to not in apt.taxi_nodes):
                continue
            na = apt.taxi_nodes[edge.node_from]
            nb = apt.taxi_nodes[edge.node_to]
            ax, ay = to_m(na.lon, na.lat)
            bx, by = to_m(nb.lon, nb.lat)
            if (ax - bx) ** 2 + (ay - by) ** 2 < 0.01:
                continue
            try:
                by_name_segs.setdefault(
                    edge.name, []).append(
                    LineString([(ax, ay), (bx, by)]))
            except _GEOM_EXC:
                continue
        apt_merged_polylines = []
        for name, segs in by_name_segs.items():
            if len(segs) == 1:
                apt_merged_polylines.append((segs[0], name))
                continue
            try:
                merged = _linemerge(MultiLineString(segs))
            except _GEOM_EXC:
                for s in segs:
                    apt_merged_polylines.append((s, name))
                continue
            if merged.is_empty:
                continue
            if merged.geom_type == "LineString":
                apt_merged_polylines.append((merged, name))
            elif merged.geom_type == "MultiLineString":
                for g in merged.geoms:
                    if not g.is_empty:
                        apt_merged_polylines.append((g, name))
    extra_stubs = _emit_primary_parallel_runway_stubs(
        nodes, ways, to_m, layout.runway_union, pav_union,
        apt_pav_vertices, taxi_rects,
        apt_centerlines=apt_merged_polylines,
        rwy_centerlines=rwy_centerlines)
    taxi_rects.extend(extra_stubs)

    # ── Drop overlapping taxi rects ───────────────────────────────
    # Pavement layout invariant (user 2026-04-26): rects MUST NOT
    # overlap each other.  Junctions are constructed as the residue
    # of pavement minus rects/runways/terminals; if rects overlap,
    # the residue is wrong and junctions can't tile the gaps.
    #
    # Sources of rect-rect overlap from upstream rect extraction:
    #   - OSM centerlines too close to each other (a main taxi and a
    #     service road parallel to it; each produces a rect whose
    #     half-width buffer covers the other).
    #   - Apron-emit retries that try to add more rects to simplify
    #     a junction polygon — without an explicit overlap check the
    #     new rect can land on top of an existing one.
    #
    # Drop rule: walk all rect pairs; if their intersection exceeds
    # RECT_OVERLAP_NOISE_M2 (a tiny float-noise threshold so corners
    # that "kiss" but don't actually overlap aren't flagged), drop
    # the rect with the SHORTER axis — the longer rect is more
    # likely the real centerline.  Tie-break by larger area.
    # Iterate until no rect-pair overlap remains.
    RECT_OVERLAP_NOISE_M2 = 1.0
    # Drop only when the overlap is a SIGNIFICANT fraction of the
    # smaller rect's area; a diagonal stub joining a primary parallel
    # legitimately produces a small corner-overlap triangle (e.g. at
    # SPJC, C joining A produces a 24 m² corner overlap on a 10 051 m²
    # C rect — 0.2 %, not a duplicate).  5 % is the empirical
    # boundary: above that, the rects clearly cover the same area;
    # below that, it's a diagonal-stub corner kiss.
    RECT_OVERLAP_FRAC_TOL = 0.05
    if len(taxi_rects) >= 2:
        kept = list(taxi_rects)
        changed = True
        while changed:
            changed = False
            n = len(kept)
            drop_idx: set = set()
            for i in range(n):
                if i in drop_idx:
                    continue
                rect_i, axis_i, role_i, ref_i = kept[i]
                len_i = axis_i.length if axis_i is not None else 0.0
                for j in range(i + 1, n):
                    if j in drop_idx:
                        continue
                    rect_j, axis_j, role_j, ref_j = kept[j]
                    len_j = axis_j.length if axis_j is not None else 0.0
                    try:
                        if not rect_i.intersects(rect_j):
                            continue
                        inter = rect_i.intersection(rect_j)
                        if inter.is_empty:
                            continue
                        if inter.area <= RECT_OVERLAP_NOISE_M2:
                            continue
                        # Diagonal stubs that join a primary parallel
                        # (e.g. SPJC's B/C/E/G connecting to A/F/L/V)
                        # legitimately share a small corner triangle
                        # with their parent.  Drop only when the
                        # overlap is a SIGNIFICANT fraction of the
                        # smaller rect — not just a corner kiss.
                        smaller_area = min(rect_i.area, rect_j.area)
                        if (smaller_area > 0
                                and inter.area
                                / smaller_area
                                <= RECT_OVERLAP_FRAC_TOL):
                            continue
                        # (s79) 1206 PROVENANCE BEATS DISCOVERY (user
                        # 2026-06-11): a road (SVC) rect overlapping a
                        # medial-DISCOVERED (TX) rect wins regardless of
                        # axis length — the lane is a road.  apt.dat
                        # aircraft rows — INCLUDING unnamed ones (the
                        # SPLP lesson: unref 1202 rows are real
                        # taxiways; SPJC lost secondary_parallel/stub
                        # floors when blank refs yielded to roads) —
                        # rank via the normal length rule below.
                        _svc_i = ref_i.startswith("SVC")
                        _svc_j = ref_j.startswith("SVC")
                        _disc_i = ref_i.startswith("TX")
                        _disc_j = ref_j.startswith("TX")
                        if _svc_i and _disc_j:
                            drop_idx.add(j)
                            continue
                        if _svc_j and _disc_i:
                            drop_idx.add(i)
                            break
                        # A road vs an AIRCRAFT rect (referenced or
                        # unnamed apt.dat row): the road ALWAYS yields
                        # — vehicles drive on the taxiway, aircraft
                        # rules govern (the absorption ruling).  The
                        # length rule below would let a LONG road beat
                        # a short stub (SPJC lost 2 secondary_parallels
                        # + a stub to SVC8's 5,441 m² rect).
                        if _svc_i and not _svc_j:
                            drop_idx.add(i)
                            break
                        if _svc_j and not _svc_i:
                            drop_idx.add(j)
                            continue
                        if _svc_i and _svc_j:
                            # (s79) ROAD-vs-ROAD overlap: an out-and-
                            # back 1206 route yields two parallel runs
                            # whose rects overlap; dropping one loses
                            # real road coverage (CYXY "Crew cars" /
                            # pav[1]).  CLIP the shorter rect to its
                            # non-overlapping remainder instead; drop
                            # only when nothing usable remains.
                            #
                            # CONTAINMENT (CYXY SVC7⊃SVC8): when one rect
                            # (almost) fully contains the other, the
                            # difference is a DONUT.  The donut survives to
                            # the layout, but the pre-solve weld /
                            # conformance rebuilds rings from the EXTERIOR
                            # only (``_open_ring``) and silently FILLS the
                            # hole — re-creating the very overlap the clip
                            # removed (the 237 m² CYXY self-overlap).  The
                            # contained rect adds no coverage the container
                            # lacks, so DROP it whole and keep the container
                            # intact: no donut, no coverage loss.
                            if inter.area >= 0.9 * smaller_area:
                                drop_c = (i if rect_i.area <= rect_j.area
                                          else j)
                                drop_idx.add(drop_c)
                                if drop_c == i:
                                    break
                                continue
                            li, lj = ((i, j) if len_i < len_j
                                      or (abs(len_i - len_j) < 0.5
                                          and rect_i.area < rect_j.area)
                                      else (j, i))
                            r_lose, a_lose, ro_lose, rf_lose = kept[li]
                            r_win = kept[lj][0]
                            try:
                                rem = r_lose.difference(r_win)
                            except _GEOM_EXC:
                                rem = None
                            best_rem = None
                            if rem is not None and not rem.is_empty:
                                cand = (rem.geoms if rem.geom_type
                                        == "MultiPolygon" else [rem])
                                # Reject HOLED remainders too: a donut
                                # passes the area floor but its hole is
                                # dropped downstream (see above), so it is
                                # not a usable non-overlapping piece.
                                polys9 = [g for g in cand
                                          if g.geom_type == "Polygon"
                                          and g.area >= 150.0
                                          and len(g.interiors) == 0]
                                if polys9:
                                    best_rem = max(polys9,
                                                   key=lambda g: g.area)
                            if best_rem is None:
                                drop_idx.add(li)
                            else:
                                try:
                                    a_new = a_lose.difference(r_win)
                                except _GEOM_EXC:
                                    a_new = a_lose
                                if a_new.geom_type == "MultiLineString" \
                                        and not a_new.is_empty:
                                    a_new = max(a_new.geoms,
                                                key=lambda g: g.length)
                                if (a_new.is_empty
                                        or a_new.geom_type
                                        != "LineString"):
                                    a_new = a_lose
                                kept[li] = (best_rem, a_new, ro_lose,
                                            rf_lose)
                                changed = True
                            if li == i:
                                break
                            continue
                        # Drop the shorter-axis rect; tie-break by
                        # smaller area.
                        if len_i < len_j or (
                                abs(len_i - len_j) < 0.5
                                and rect_i.area < rect_j.area):
                            drop_idx.add(i)
                            if os.environ.get("O4_RECT_DROP_DEBUG") == "1":
                                print(f"[rect-drop] loser ref={ref_i} "
                                      f"len={len_i:.0f} area={rect_i.area:.0f} "
                                      f"bounds={tuple(round(v,1) for v in rect_i.bounds)} "
                                      f"winner ref={ref_j} len={len_j:.0f} "
                                      f"inter={inter.area:.0f}")
                            break
                        else:
                            drop_idx.add(j)
                            if os.environ.get("O4_RECT_DROP_DEBUG") == "1":
                                print(f"[rect-drop] loser ref={ref_j} "
                                      f"len={len_j:.0f} area={rect_j.area:.0f} "
                                      f"bounds={tuple(round(v,1) for v in rect_j.bounds)} "
                                      f"winner ref={ref_i} len={len_i:.0f} "
                                      f"inter={inter.area:.0f}")
                    except _GEOM_EXC:
                        continue
            if drop_idx:
                kept = [k for idx, k in enumerate(kept)
                        if idx not in drop_idx]
                changed = True
        if len(kept) < len(taxi_rects):
            try:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: dropped "
                    f"{len(taxi_rects) - len(kept)} taxi rect(s) "
                    f"that overlapped another rect.")
            except _GEOM_EXC:
                pass
            taxi_rects = kept

    # Per user 2026-04-27 invariant: a junction polygon must NEVER run
    # along a sloping rect's sloping edge.  When a primary_parallel rect
    # sits FULLY INSIDE the airport's pavement union (apt.dat row-110
    # ⊕ DSF ⊕ OSM-synthetic — i.e. the same union that becomes the
    # residue), the surrounding apron junction unavoidably wraps the
    # rect's sloping edges as it traces around the rect-shaped hole.
    # The fix is not to emit the rect at all: let the apron absorb
    # the rect's footprint and slope multi-directionally.  Primary
    # parallels that legitimately cross unpaved area are unaffected
    # (their sloping edges aren't inside pavement).
    #
    # Note: ``pav_union`` is now apt.dat ∪ DSF only (no OSM-synth);
    # that's the right denominator for the absorption check.
    # Including DSF is essential — at SPJC's SE apron, F's long
    # edges are 0 % / 24 % inside row-110 alone but ≈100 % inside
    # apt.dat ∪ DSF.
    # Embedded-parallel removal (full drop + partial clip) is part of the
    # old absorption model: a taxilane rect whose footprint lies inside
    # apron pavement was dropped/clipped so the apron covered it.  In the
    # no-absorption model (ABSORB_RECTS_ALONGSIDE_APRONS=False) we KEEP the
    # rect so the taxilane through the apron stays a directionally-graded
    # rect; it's subtracted from the apron (apron = pav_union − rects), so
    # the apron simply wraps it — no overlap, perfect node parity.
    # Phase-1 absorption — always-on (session 51 per user 2026-05-27):
    # drop fully-embedded primary_parallels AND partial-clip half-embedded
    # ones.  This is the "build it right at construction" stage,
    # independent of the elevation-block post-emit absorb.  Without the
    # drop, the rect shape is kept but doesn't fit the surrounding free-
    # form apt.dat pavement well, leaving the rect's "extra" area as a
    # residue piece that downstream passes (decompose / sliver merge /
    # overlap-clip) end up losing — CYXY Taxi E example: rect kept ->
    # 3626 m^2 adjacent area lost from apron coverage.
    #
    # Trade-off (accepted by user 2026-05-27): named taxiways whose long
    # edges are embedded in surrounding apron (SPJC Taxi A1) get dropped
    # too — their footprint becomes apron rather than a directional rect.
    # The audit-flagged residue-loss path is a future fix; the drop is the
    # only thing currently keeping CYXY's coverage intact.
    taxi_rects = _drop_primary_parallels_embedded_in_pavement(
        taxi_rects, pav_union, runway_polys=runway_polys)
    from .pavement.absorption import (
        _split_primary_parallels_at_pavement_boundary)
    taxi_rects = _split_primary_parallels_at_pavement_boundary(
        taxi_rects, pav_union)
    # (session 51) The session-47 `build_apron_lane_rects` (fixed-code-letter-
    # width lanes for taxi centerlines through OPEN apron interiors) was
    # REMOVED per user 2026-05-27.  In the clean model, those middle-of-apron
    # centerlines produce no rect — the apron wraps the whole footprint as
    # one polygon.  No directional grading for those lanes; cleaner geometry.

    # ── Detect bridge taxi rects from OSM (user 2026-04-29) ───────
    # Per OSM convention, bridge taxiways carry ``bridge=yes`` (or
    # any non-empty bridge=*) on their parent way.  Build a list of
    # raw OSM bridge-tagged taxiway LineStrings; a rect is a
    # "bridge rect" if its axis lies within a small lateral
    # tolerance of one of those LineStrings.  We use spatial
    # proximity rather than tag-pass-through because
    # ``_extract_osm_taxi_centerlines`` linemerges per-ref, which
    # loses the per-way bridge tag.
    bridge_lines: List[LineString] = []
    for _wid, _nds, _tags in ways:
        if _tags.get("aeroway") != "taxiway":
            continue
        b = _tags.get("bridge", "")
        if not b or b == "no":
            continue
        _pts = []
        for _n in _nds:
            if _n in nodes:
                _lat, _lon = nodes[_n]
                _pts.append(to_m(_lon, _lat))
        if len(_pts) < 2:
            continue
        try:
            _ls = LineString(_pts)
        except _GEOM_EXC:
            continue
        if _ls.is_empty or _ls.length < 5.0:
            continue
        bridge_lines.append(_ls)
    bridge_rect_indices: set = set()
    BRIDGE_AXIS_PROXIMITY_M = 5.0
    if bridge_lines:
        for ri, (_rect, _axis, _role, _ref) in enumerate(taxi_rects):
            if _axis is None or _axis.is_empty:
                continue
            for _bl in bridge_lines:
                # An axis is on a bridge if it lies within
                # BRIDGE_AXIS_PROXIMITY_M of the bridge LineString
                # along most of its length AND has overlap.
                try:
                    if _axis.distance(_bl) > BRIDGE_AXIS_PROXIMITY_M:
                        continue
                    inter = _axis.intersection(
                        _bl.buffer(BRIDGE_AXIS_PROXIMITY_M))
                    if (not inter.is_empty
                            and hasattr(inter, "length")
                            and inter.length
                            >= 0.5 * _axis.length):
                        bridge_rect_indices.add(ri)
                        break
                except _GEOM_EXC:
                    continue

    # ── Hole-aware sloping-edge snap (user 2026-05-04) ────────────
    # When a sloping rect's long edge runs near and parallel to an
    # apt.dat row-110 hole boundary, snap the rect so its sloping
    # edge LIES ON the hole boundary.  Without this, the rect's body
    # sits inside the hole's interior or crosses the hole's perimeter,
    # which then gets absorbed by ``pav_union.difference(rect)`` (the
    # "tunnel" effect): the apron polygon then spans across the hole
    # and ends up sharing boundary with the rect's sloping side.
    # Aligning the rect's sloping edge with the hole boundary makes
    # the surrounding junction wrap around the hole via the rect's
    # CROSS edges instead.
    taxi_rects = _snap_rect_sloping_edges_to_holes(taxi_rects, pav_union)

    # Square slanted rect ends (gate RECT_SQUARE_ENDS) — LAST word after the
    # per-corner pavement snap and the hole-edge snap, either of which can
    # leave a taxi rect's end following an angled junction /
    # hole boundary (CYXY cross_connector G).  Keeps each end perpendicular so
    # the junction (pav_union - rect) absorbs the angled-pavement wedge.
    from .pavement.rects import _square_taxi_rect_ends
    taxi_rects = _square_taxi_rect_ends(taxi_rects, pav_union)

    # ── CURVE-NATIVE SPINE v2 (gate O4_CURVE_NATIVE_SPINE) ───────────
    # Instead of emitting straight rects + carving junctions out of the
    # residue (and the whole downstream sliver/weld/conformance cleanup),
    # cut the real pav_union by the recognized centerlines in ONE global
    # arrangement.  The faces are conformant by construction, so the rect
    # build, junction emit and spine slice are all bypassed under the gate.
    # docs/curve_native_spine_v2_plan.md.
    from .config import CURVE_NATIVE_SPINE, ROUTE_ARC_SPINE
    emitted_taxi_rects: List[Polygon] = []
    # ROUTE-ARC SPINE (user ruling 2026-07-02): with the full route-arc spine
    # the taxi-rect pipeline is disabled entirely — the spine feeds the SAME
    # global slice, so it runs everywhere (rects would otherwise confine it
    # to junction/apron residue and double-cut every junction: arcs + their
    # corner legs sliced separately measured 2.25x the constrained pairs).
    # Recognized painted centerlines, when on, stay the spine source.
    _global_slice_spine = CURVE_NATIVE_SPINE or ROUTE_ARC_SPINE
    if _global_slice_spine:
        if (ROUTE_ARC_SPINE
                and os.environ.get("O4_RECOGNIZED_CENTERLINES", "0") != "1"):
            from .pavement.route_arcs import apply_route_arc_spine
            apply_route_arc_spine(layout, icao)
        from .pavement.global_slice import (
            build_global_slice_faces, classify_faces, dedup_centerlines)
        from .layout import ROLE_APRON, ROLE_JUNCTION
        _cn_pav = pav_union
        # SOURCE FIDELITY (user 2026-07-02, test_pavement_rests_on_source):
        # the slice emits EVERY face of its input, so the input must be
        # real source pavement (apt.dat row-110 ∪ DSF ∪ runway).  The
        # working pav_union accretes non-source area downstream of the
        # early source snapshot (lot merges, closing, absorbed residue) —
        # under the rect model those regions never became shapes (rect
        # residue rules dropped them), but the slice would emit them as
        # pavement over grass (SPLP faces at 20-24 % on source) AND their
        # bulk displaces/clips the boundary→DEM bridges.
        _src = getattr(layout, "source_pavement_union", None)
        if os.environ.get("O4_SLICE_SOURCE_CLIP", "1") != "1":
            _src = None
        if _src is not None and not _src.is_empty:
            try:
                _src_all = _src
                if (layout.runway_union is not None
                        and not layout.runway_union.is_empty):
                    _src_all = _src_all.union(layout.runway_union)
                _cn_pav = _cn_pav.intersection(_src_all.buffer(0.5))
            except _GEOM_EXC:
                _cn_pav = pav_union
        if terminal_union is not None and not terminal_union.is_empty:
            try:
                _cn_pav = _cn_pav.difference(terminal_union)
            except _GEOM_EXC:
                pass
        _cn_cls, _cn_svc, _cn_seen = [], [], set()
        for _it in (getattr(layout, "apt_taxi_centerlines", []) or []):
            _ln = getattr(_it, "chained_line", None) or getattr(_it, "line", None)
            if (_ln is None or _ln.is_empty or _ln.length < 1.0
                    or id(_ln) in _cn_seen):
                continue
            _cn_seen.add(id(_ln))
            # SERVICE ROADS (user 2026-07-02): narrow truck routes are
            # sliced too — the road centerline cuts its strip out of the
            # surrounding pavement and the resulting narrow faces emit as
            # ROLE_SERVICE_JUNCTION at the road grade cap (4 %), which also
            # feeds the law's road_zone carve relaxation.  Kept in a
            # SEPARATE list so face classification can tell taxi spine
            # from truck path (a face touching both is taxi territory).
            if getattr(_it, "is_service", False):
                _cn_svc.append(_ln)
            else:
                _cn_cls.append(_ln)
        # De-dup once here so classification indexes the SAME effective set the
        # slice tagged faces against (coincident lines bury duplicates).
        # ROUTE-ARC source: NO dedup — the route-arc graph is planarized,
        # noded and arc-deduped by construction, and the 3.5 m paint-dedup
        # eats the SHORT junction connector fragments (SPJC 481→399), which
        # disconnects the spine chains through junctions: PHASE A then solves
        # adjacent route chains to different levels and freezes both (the
        # 2.6 m frozen-spine walls, 1622 spine/spine residual edges).
        _cn_eff = (list(_cn_cls) if ROUTE_ARC_SPINE
                   else dedup_centerlines(_cn_cls))
        # Diagnostic (O4_DUMP_SLICE_INPUT=<prefix>): dump the exact pav_union +
        # spine fed to the slice as two JOSM layers, to verify inputs.
        _cn_dump = os.environ.get("O4_DUMP_SLICE_INPUT")
        if _cn_dump:
            from .pavement.global_slice import dump_slice_inputs_osm
            dump_slice_inputs_osm(layout, _cn_pav, _cn_eff, _cn_dump,
                                  runway_union=layout.runway_union)
        # Taxi centerlines first, service roads appended — a face's
        # ``centerline_ids`` >= len(_cn_eff) are truck routes.
        _cn_all = list(_cn_eff) + _cn_svc
        _cn_dbg_pts = None
        _cp_spec = os.environ.get("O4_COVERAGE_PROBE")
        if _cp_spec:
            from .layout import _projection as _cp_proj
            _cp_to_m = _cp_proj(layout.anchor)
            _cn_dbg_pts = []
            for _part in _cp_spec.split(";"):
                _la, _lo = (float(v) for v in _part.split(","))
                _cn_dbg_pts.append(_cp_to_m(_lo, _la))
        _cn_faces = build_global_slice_faces(
            _cn_pav, _cn_all, runway_union=layout.runway_union, dedup=False,
            debug_pts=_cn_dbg_pts)
        _svc_base = len(_cn_eff)
        _svc_faces = set()
        for _fi, _f in enumerate(_cn_faces):
            _taxi_ids = [i for i in _f.centerline_ids if i < _svc_base]
            _svc_ids = [i for i in _f.centerline_ids if i >= _svc_base]
            if _svc_ids and not _taxi_ids:
                # A face whose ONLY centerlines are truck routes is road
                # territory (user 2026-07-02: service roads between aprons
                # and parking lots) — ROLE_SERVICE_JUNCTION at any width.
                # This also SEVERS the runway touch-chain there, so lot
                # faces beyond it demote to groundside (DEM-follow) via
                # _reclassify_runway_disconnected_to_groundside, matching
                # the rect model where the road CARVE separated the lots.
                _svc_faces.add(_fi)
            # classification below reasons over TAXI spine only; a face
            # touching both taxi and truck lines is taxi territory.
            _f.centerline_ids = _taxi_ids
        classify_faces(_cn_faces, _cn_all)
        from .layout import ROLE_SERVICE_JUNCTION as _ROLE_SVC_JCT
        _cn_roles = {"corridor": 0, "junction": 0, "apron": 0, "service": 0}
        for _fi, _f in enumerate(_cn_faces):
            if _fi in _svc_faces:
                _f.kind = "service"
                _f.axis = None
            _cn_roles[_f.kind] = _cn_roles.get(_f.kind, 0) + 1
            if _f.kind == "service":
                _role = _ROLE_SVC_JCT
            elif _f.kind == "apron":
                _role = ROLE_APRON
            else:
                _role = ROLE_JUNCTION
            layout.shapes.append(BuiltShape(
                polygon=_f.polygon, role=_role, ref="", source_axis=_f.axis))
        UI.vprint(1, f"  [pav-builder] {icao}: curve-native global slice — "
                  f"{len(_cn_faces)} face(s) from {len(_cn_all)} centerline(s) "
                  f"({_cn_roles['corridor']} corridor / "
                  f"{_cn_roles['junction']} junction / {_cn_roles['apron']} apron / "
                  f"{_cn_roles['service']} service; "
                  f"rects/junction-emit/spine bypassed).")
    else:
        # Emit taxi rects (already trimmed to narrow-width portion).
        for ri, (rect, axis, role, ref) in enumerate(taxi_rects):
            emitted_taxi_rects.append(rect)
            layout.shapes.append(BuiltShape(
                polygon=rect, role=role, ref=ref, source_axis=axis,
                is_bridge=(ri in bridge_rect_indices)))

    from .geom_guard import coverage_probe as _covp
    _covp(layout, "post-slice")

    # ── NARROW truck-route strips → centered service corridors ────────
    # (user 2026-07-04, CYXY): the truck route is a SPINE in the CENTER
    # of its road — where the contiguous pavement cross-section at the
    # spine is narrow, the WHOLE strip is service pavement.  The slice
    # cuts pavement ALONG the route, so each half of a narrow strip
    # merges into the big face it touches and no narrow face ever exists
    # for classify_faces — one side (or neither) read as road.  Carving
    # the centered corridor also severs the aircraft touch-chain, so
    # lots/pads beyond it demote to groundside downstream.  Gate
    # O4_SERVICE_STRIP_CARVE=0 restores the sliced-halves behaviour.
    _n_strip = 0
    if _global_slice_spine and os.environ.get(
            "O4_SERVICE_STRIP_CARVE", "1") == "1":
        from .groundside import carve_narrow_service_strips
        _n_strip = carve_narrow_service_strips(
            layout, pav_union, terminal_union)
        if _n_strip:
            UI.vprint(1,
                f"  [pav-builder] {icao}: carved {_n_strip} narrow "
                f"truck-route strip piece(s) as centered service "
                f"corridor(s).")
        _covp(layout, "post-service-strip-carve")

    # ── Rectless SVC connector → service_junction ────────────────
    # A service-road (SVC) centerline piece too CURVED/short for the rect
    # builder produces NO rect (the bend-split chops it into sub-min-length /
    # degenerate pieces — CYXY truck-route 'D' arm to lots 102/104/105).
    # Without an emitted service shape its pavement stays FUSED into the apron,
    # so the lot it serves never reads as road-only and wrongly stays apron.
    # Emit the piece's uncovered pavement CORRIDOR as a ``service_junction``
    # (all-pair 4 %, no axial rect needed); it separates the lot like a rect
    # would and the lot then reclassifies road-only → groundside.
    #
    # ONLY a NARROW CONNECTOR ROAD BETWEEN larger shapes qualifies — never a
    # truck route running THROUGH a large apron (user 2026-06-29).  The test is
    # CONNECTIVITY: a real connector is a BRIDGE — removing its corridor splits
    # the LOCAL pavement into two sizable pieces (the apron and the lot it
    # feeds).  A truck route inside one apron, or a rect-trim edge remainder,
    # leaves the local pavement in one piece, so it is skipped.
    #
    # DEFAULT OFF (user 2026-06-29): carving the connector out of the apron is a
    # NET NEGATIVE as it stands — the lots do reclassify to groundside, but the
    # apron edges re-grade (+~100 moderate within-shape pairs at CYXY) and the
    # all-pair-4 % service_junction connectors are themselves steep on slope.
    # NEEDS MORE WORK before default-on: emit the connector as DEM-following
    # groundside (merged with the lot) rather than an all-pair junction, and/or
    # clip the apron cleanly so it does not fragment.  Enable with
    # O4_SVC_CURVED_JUNCTION=1 to experiment.
    if os.environ.get("O4_SVC_CURVED_JUNCTION", "0") == "1":
        from .layout import ROLE_SERVICE_JUNCTION
        _SVC_JCT_MIN_AREA_M2 = 30.0
        _BRIDGE_REACH_M = 60.0       # local pavement window around a connector
        _BRIDGE_PIECE_M2 = 50.0      # a split-off piece this big = a real shape
        _covered = (unary_union(emitted_taxi_rects)
                    if emitted_taxi_rects else None)
        _n_svc_jct = 0
        for _ln, _ref in osm_centerlines:
            if (not str(_ref or "").startswith("SVC")
                    or _ln is None or _ln.is_empty):
                continue
            _hw = max((_svc_widths or {}).get(_ref, 5.0) / 2.0, 2.5)
            try:
                _corr = _ln.buffer(_hw, cap_style=2).intersection(pav_union)
                if terminal_union is not None and not terminal_union.is_empty:
                    _corr = _corr.difference(terminal_union)
                if _covered is not None:
                    _corr = _corr.difference(_covered)
            except _GEOM_EXC:
                continue
            _parts = ([_corr] if _corr.geom_type == "Polygon"
                      else [g for g in getattr(_corr, "geoms", ())
                            if g.geom_type == "Polygon"])
            for _p in _parts:
                if _p.is_empty or _p.area < _SVC_JCT_MIN_AREA_M2:
                    continue
                # Bridge test: does removing this corridor disconnect the local
                # pavement into >=2 sizable pieces?
                try:
                    _nbhd = pav_union.intersection(_p.buffer(_BRIDGE_REACH_M))
                    _without = _nbhd.difference(_p.buffer(0.5))
                    _wc = ([_without] if _without.geom_type == "Polygon"
                           else [g for g in getattr(_without, "geoms", ())
                                 if g.geom_type == "Polygon"])
                    if sum(1 for g in _wc
                           if g.area >= _BRIDGE_PIECE_M2) < 2:
                        continue          # not a bridge between two shapes
                except _GEOM_EXC:
                    continue
                emitted_taxi_rects.append(_p)
                _covered = (unary_union([_covered, _p])
                            if _covered is not None else _p)
                layout.shapes.append(BuiltShape(
                    polygon=_p, role=ROLE_SERVICE_JUNCTION, ref=_ref))
                _n_svc_jct += 1
        if _n_svc_jct:
            UI.vprint(1,
                f"  [pav-builder] {icao}: emitted {_n_svc_jct} narrow SVC "
                f"connector(s) as service_junction (rectless, between shapes).")

    # ── Junction emission (finalize/repair runs downstream) ──────
    # Under the curve-native / route-arc gates the global slice already
    # produced the junction/apron faces from pav_union, so there is no rect
    # residue to carve — skip junction emit entirely.
    if not _global_slice_spine:
        junction_emit.emit_junctions(
            layout,
            pav_union=pav_union,
            emitted_taxi_rects=emitted_taxi_rects,
            terminal_union=terminal_union,
            taxi_rects=taxi_rects,
            icao=icao)

    # ── Ground-vehicle service_road rects (4 %) ──────────────────
    # Combine apt.dat 1206 truck routes with OSM small roads inside the
    # boundary (+ a small outside buffer), and emit 4 %-grade rects ONLY
    # where a route is a dedicated strip OUTSIDE aircraft pavement (the
    # builder drops the on-apron portions; aircraft rules apply there).
    # These also act as apron↔DEM transition ramps.
    _service_lines: List[Tuple[LineString, str]] = (
        list(getattr(layout, "apt_service_centerlines", []) or [])
        if ENABLE_SERVICE_ROADS else [])
    if ENABLE_SERVICE_ROADS and pav_union is not None and not pav_union.is_empty:
        # Keep-region = within SERVICE_ROAD_PAVEMENT_NEAR_M (25 m) of any
        # apt.dat/DSF pavement.  Keeps only the apron-access / crossing
        # roads that join the airfield; drops the deep-interior road grid
        # of large airports (HECA ~852 → a handful).  apt.dat 1206 truck
        # routes are added unconstrained above.
        try:
            _bound_buf = pav_union.buffer(SERVICE_ROAD_PAVEMENT_NEAR_M)
        except _GEOM_EXC:
            _bound_buf = None
        if _bound_buf is not None and not _bound_buf.is_empty:
            sn_nodes, sn_ways = _load_osm_small_roads(anchor[0], anchor[1])
            # Dense cities (HECA: ~9k small roads) make a full
            # intersection() per road prohibitive.  Cheap bbox reject +
            # prepared-geometry intersects() pre-check skip the roads that
            # don't touch the keep-region before paying for the exact clip.
            from shapely.prepared import prep as _prep
            _bb_minx, _bb_miny, _bb_maxx, _bb_maxy = _bound_buf.bounds
            _bound_prep = _prep(_bound_buf)
            for _wid, _nds, _tags in sn_ways:
                if _tags.get("highway") not in OSM_SMALL_ROAD_HIGHWAY_TYPES:
                    continue
                _pts = []
                for _n in _nds:
                    if _n in sn_nodes:
                        _la, _lo = sn_nodes[_n]
                        _pts.append(to_m(_lo, _la))
                if len(_pts) < 2:
                    continue
                # Bounding-box reject (cheap): road entirely outside the
                # boundary-buffer bbox can't contribute.
                _xs = [p[0] for p in _pts]
                _ys = [p[1] for p in _pts]
                if (max(_xs) < _bb_minx or min(_xs) > _bb_maxx
                        or max(_ys) < _bb_miny or min(_ys) > _bb_maxy):
                    continue
                try:
                    _ls = LineString(_pts)
                    if not _bound_prep.intersects(_ls):
                        continue
                    _clip = _ls.intersection(_bound_buf)
                except _GEOM_EXC:
                    continue
                if _clip.is_empty:
                    continue
                _nm = _tags.get("name", "") or _tags.get("highway", "road")
                if _clip.geom_type == "LineString":
                    _service_lines.append((_clip, _nm))
                elif _clip.geom_type == "MultiLineString":
                    for _g in _clip.geoms:
                        if not _g.is_empty:
                            _service_lines.append((_g, _nm))
    if _service_lines:
        _svc_rects, _svc_junctions = build_service_road_network(
            _service_lines, pav_union,
            width=SERVICE_ROAD_WIDTH_M, min_len=MIN_SERVICE_STRIP_LEN_M)
        for _rect, _axis, _role, _ref in _svc_rects:
            layout.shapes.append(BuiltShape(
                polygon=_rect, role=_role, ref=_ref, source_axis=_axis))
        for _jpoly, _jrole, _jref in _svc_junctions:
            layout.shapes.append(BuiltShape(
                polygon=_jpoly, role=_jrole, ref=_jref))
        if _svc_rects or _svc_junctions:
            UI.vprint(1,
                f"  [pav-builder] {icao}: {len(_svc_rects)} service_road "
                f"rect(s) + {len(_svc_junctions)} service_junction(s) "
                f"(4% ground-vehicle network off aircraft pavement).")


    # ── Phase-2 elevations + feature emit ────────────────────────
    # Pre-solve geometry guard snapshot handle (assigned right before the
    # solve inside the block below; default None so the post-solve report
    # is a no-op when elevations are not computed / guard is disabled).
    _geom_guard_snap = None
    # Whether the airside node-unification (weld + full conformance + corner
    # snaps) already ran PRE-solve (refactor Phases 6+7).  The per-surface
    # path runs it before the solve; the post-solve block below then conforms
    # only the post-solve-emitted FEATURES (one-sided) to the frozen airside.
    # When False (non-per-surface / no-elevation path), the post-solve block
    # runs the full unification as a fallback.
    _airside_unified_presolve = False
    if compute_elevations:
        _progress.step()  # [5] Solving elevations (FAA grade compliance)
        _covp(layout, "pre-finalize-repair")
        finalize.compute_elevations_and_repair_geometry(
            layout, icao, xplane_root, apt,
            nodes=nodes, ways=ways, to_m=to_m,
            apron_candidates=apron_candidates,
            tile_dem=tile_dem,
            current_tile_lat=current_tile_lat,
            current_tile_lon=current_tile_lon)

        # Final Rule 2 enforcement (user 2026-05-01).  Triangulation
        # densification and Laplacian-solver vertex insertions can
        # leave a few junction vertices within SLOPING_EDGE_SNAP_M of a
        # sloping rect's sloping edge despite the in-densify guard.  A
        # final post-elevation snap clears these residual cases.
        # Aligned per-vertex altitudes are preserved by index, so the
        # snap is altitude-safe (a 10 m planar move at typical
        # taxi-grade ≤ 1.5 % shifts elevation by ≤ 15 cm — well
        # within the within-shape grade tolerance).
        from .junction_rules import (
            _enforce_runway_1to1_sharing,
            _snap_junction_vertices_to_rect_flat_edge_corners,
            _snap_to_sloping_edge_corners,
            stitch_pavement_to_flat_runways,
            widen_junctions_to_runway_corners,
        )
        # (session 51) `_align_rect_slope_to_axis` was DROPPED: it
        # reacted to the SOLVED slope (flattening rects whose solved
        # slope came out perpendicular to source_axis), which has no
        # place in a single pre-finalized-geometry solve — the cascade
        # grades each rect along its own source_axis, so a
        # perpendicular-slope rect should not arise.
        #
        # (session 51) The two corner-snaps that used to run HERE were
        # removed: with the altitude gate they were no-ops at this
        # pre-solve point (rects have None altitudes), and they now do
        # their real work in the post-geometry block below (after
        # split/absorb/reclassify), on the settled junction set — the
        # same place the 2-solve order ran them effectively.
        # VEER FIX 2026-07-03 (user report "spines veer to a runway corner
        # at the very end"): the pass now SPARES spine cut/contact nodes
        # (ring vertices ON a taxi centerline) — blanket retirement under
        # the slice was measured WORSE (SPLP junction↔runway seam needs the
        # corner sharing: 4 new >0.5 m steps without it).  Debug gate only.
        if os.environ.get("O4_RWY_1TO1", "1") == "1":
            _enforce_runway_1to1_sharing(layout)
        _covp(layout, "post-rwy-1to1")
        # Rule 1 v6 widening (user 2026-05-02): runs ONLY here,
        # post-elevation, after the runway is segmented.  Inserts
        # outboard runway corners as new junction vertices with
        # matching altitudes.  Pre-elevation widening was disabled
        # because the cascade with Rule 4 + segmentation re-run
        # over-grew junctions past the 4-node cap.
        # (measured innocent in the veer investigation — it only ADDS
        # adjacent runway corners next to shared vertices; debug gate kept)
        if os.environ.get("O4_WIDEN_RWY_CORNERS", "1") == "1":
            widen_junctions_to_runway_corners(layout)
        _covp(layout, "post-widen-to-rwy-corners")
        # Stitch pavement to flat runway shapes (user 2026-05-09):
        # for blast pads / flat-interior runway segments, insert a
        # shared vertex on the runway boundary at the projection of
        # every adjacent pavement vertex that sits within edge
        # tolerance.  These new shared vertices become HARD anchors
        # at the runway altitude when the per-surface solver runs,
        # cutting cap-projection distance from the runway corners
        # (often 100s of m apart on long blast pads) down to tens
        # of metres — adjacent junctions / stubs lift toward the
        # runway elevation instead of stalling at terrain.
        stitch_pavement_to_flat_runways(layout)
        _covp(layout, "post-stitch-flat-rwys")

        # Per user 2026-05-03: per-surface solver runs AS THE LAST
        # STEP of the pipeline, after every junction rule and
        # runway-corner insertion.  ``widen_junctions_to_runway_
        # corners`` inserts vertices with raw runway altitudes that
        # may violate the surrounding junction's per-axis grade
        # rule with respect to neighbouring (terrain-following)
        # vertices; the solver pass cap-projects them into
        # compliance.
        from .elevation import USE_PER_SURFACE_SOLVER
        if USE_PER_SURFACE_SOLVER and layout.anchor is not None:
            from .elevation import _load_airport_dem
            from .elevation_per_surface import solve as per_surface_solve
            # Per user 2026-05-12: keep DEM-tile and indexing-coords
            # in lockstep.  ``_load_airport_dem`` returns the override
            # (= driver's current-build-tile DEM) if provided, else
            # loads the anchor tile.  So the coords to use are:
            #   - current_tile_lat/lon when tile_dem is provided
            #     (DEM is the current build tile);
            #   - floor(anchor) when tile_dem is None (DEM is the
            #     anchor tile, standalone / test path).
            # Previously used floor(anchor) unconditionally — WRONG
            # for cross-tile airports during a neighbour-tile build
            # where ``current_tile`` and ``anchor_tile`` diverge
            # (e.g. SPLP anchor in -13/-78 while Ortho4XP is building
            # -13/-77), causing _sample_dem to read the wrong row.
            dem = tile_dem if tile_dem is not None else _load_airport_dem(
                layout.anchor[0], layout.anchor[1])
            if tile_dem is not None and current_tile_lat is not None:
                tile_lat = current_tile_lat
                tile_lon = current_tile_lon
            else:
                tile_lat = int(math.floor(layout.anchor[0]))
                tile_lon = int(math.floor(layout.anchor[1]))

            # Capture elevation provenance off the ACTUAL DEM this solve grades
            # against: which airport-elevation insets baked into it (stamped on
            # the DEM object by bake_airport_insets_into_alt_dem) or the loud
            # RAW marker when none did.  The standalone raw-load path never
            # bakes, so this correctly reports RAW even with an inset cached.
            from . import provenance as _provenance
            layout.dem_inset_provenance = (
                _provenance.dem_provenance_from_dem(dem, icao=layout.icao)
                if dem is not None else None)

            # ── Seam-anchor pipeline (user 2026-05-13) ────────────
            # 1) Insert ring vertices at integer lat/lon line crossings
            #    and convert sloped rects to node_altitudes.
            # 2) Sample the SMOOTHED DEM at each seam vertex
            #    (elevation._sample_dem, per the seam ruling;
            #    deterministic across tiles via preserve_boundary).
            # 3) Redistribute the runway profile (user 2026-05-19):
            #    fold seam DEM altitudes into the FAA-compliant
            #    profile that ``runway_segments.generate_patch_osm``
            #    emitted, run the same gates (envelope clamp + hard
            #    cap + rate-of-grade-change), and rewrite every
            #    runway sub-rect's altitudes per-vertex via axis
            #    projection.  Replaces the older threshold-only
            #    ``regrade_runways_in_layout`` step — that approach
            #    only adjusted the two threshold corners and left
            #    interior segment-boundary corners at their emit-time
            #    CIFP values, so the runway's combined profile after
            #    seam DEM anchors entered was no longer FAA-compliant.
            # 4) Solver runs as before — every runway vertex is
            #    HARD-anchored (whole-runway authoritative) and
            #    adjacent shapes grade themselves against it.
            from .seam_anchors import (
                split_pavement_at_seams, apply_seam_dem_anchors)
            from .runway_redistribute import redistribute_runway_profile
            n_split = split_pavement_at_seams(layout)
            n_seam = apply_seam_dem_anchors(
                layout, dem, tile_lat, tile_lon)
            n_redistributed = redistribute_runway_profile(
                layout, dem, tile_lat, tile_lon)
            seam_keys = getattr(layout, "_seam_anchor_keys", set())
            if seam_keys or n_seam or n_redistributed:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: seam pipeline — "
                    f"{len(seam_keys)} seam vert(s), "
                    f"{n_seam} DEM-anchored, "
                    f"{n_redistributed} runway shape(s) redistributed.")

            # Feature B stage 2 (O4_OBJECT_BRIDGE_TERRAIN, docs/object_
            # terrain_features_spec.md section 3.2): classify the pack's
            # bridge objects NOW (pre-solve; the same cached result feeds
            # the post-solve corridor emitters) and hard-pin pavement at
            # the deck ends / across profile-carried spans using the
            # seam-anchor idiom above.  Gate off ⇒ attach is a no-op and
            # both pin writers return 0 without touching a shape.
            try:
                from . import object_terrain_assembly
                _bridge_classification = (
                    object_terrain_assembly.attach_bridge_classification(
                        layout, xplane_root))
                from .bridges import (
                    build_bridge_layout_shapes,
                    insert_bridge_deck_end_pins,
                    insert_bridge_profile_pins)
                # User ruling R12: the trench and causeway are
                # FIRST-CLASS layout shapes, born HERE with law values
                # (plus the R8 flush-seat cut and the bridge-object
                # building-pad removal) before the solve — the solver
                # and every later mutation pass leave them alone by
                # construction (roles outside every pass's role set).
                (n_bridge_trench, n_bridge_causeway,
                 n_bridge_pads_removed) = build_bridge_layout_shapes(
                    layout, dem, tile_lat, tile_lon)
                if (n_bridge_trench or n_bridge_causeway
                        or n_bridge_pads_removed):
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: object-bridge layout "
                        f"shapes — {n_bridge_trench} trench, "
                        f"{n_bridge_causeway} causeway, "
                        f"{n_bridge_pads_removed} building pad(s) "
                        f"removed.")
                # Feature A stage (O4_OBJECT_TUNNEL_TERRAIN, spec section
                # 3.3 + amendment A1, ruling R12): whole-body tunnel trench
                # + rim collar, born pre-solve as first-class layout shapes
                # from the SAME cached classification.  Gate off ⇒ no-op.
                (n_tunnel_trench, n_tunnel_rim) = (
                    object_terrain_assembly.build_tunnel_layout_shapes(
                        layout, dem, tile_lat, tile_lon))
                if n_tunnel_trench or n_tunnel_rim:
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: object-tunnel layout "
                        f"shapes — {n_tunnel_trench} trench floor, "
                        f"{n_tunnel_rim} rim collar.")
                n_bridge_deck_pins = insert_bridge_deck_end_pins(
                    layout, dem, tile_lat, tile_lon)
                n_bridge_profile_pins = insert_bridge_profile_pins(
                    layout, dem, tile_lat, tile_lon)
                # ALWAYS print the summary when the classifier ran —
                # a zero here is the coupling-failure signal, and a
                # silent zero is this project's classic failure mode
                # (stage 2b: the first gated KBNA build pinned nothing
                # and said nothing at verbosity 1).
                if _bridge_classification is not None:
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: object-bridge pins — "
                        f"{n_bridge_deck_pins} deck-end, "
                        f"{n_bridge_profile_pins} profile.")
            except Exception as _object_bridge_error:  # never fail the build
                UI.vprint(1,
                          "   [object-bridge] solve-side pins skipped:",
                          _object_bridge_error)

            # CROSSING INFLUENCE ZONE (Phase 1, docs/specs/crossing-
            # terrain-ownership.md): publish the ONE pre-solve zone every
            # terrain writer honors — the portal pairs / classification it
            # reads were cached by ``build_bridge_layout_shapes`` above,
            # and every consumer construction (runway-end skirts, gap-fill
            # spines, adjacent-ground bands, clearance) runs after this
            # point and consults the published zone instead of
            # reconstructing crossing geometry itself.
            try:
                from .crossing_terrain import (
                    publish_crossing_influence_zones)
                n_crossing_zones = publish_crossing_influence_zones(layout)
                if n_crossing_zones:
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: crossing influence "
                        f"zone published — {n_crossing_zones} "
                        f"component(s).")
            except Exception as _crossing_zone_error:  # never fail the build
                UI.vprint(1,
                          "   [crossing-zone] publication FAILED — "
                          "crossing keep-outs are INACTIVE this build:",
                          _crossing_zone_error)

            # (session 51 single-solve) The first solver pass + the
            # grade-based `_subdivide_violating_junctions` loop were
            # REMOVED here.  The pipeline now finalizes ALL geometry
            # before a SINGLE solve at the end, so there is no
            # geometry↔altitude loop to bootstrap: downstream geometry
            # passes (stitch / split / absorb) are de-coupled from
            # altitudes (role-based detection), and grade relief is
            # handled inside the solver (directional + hop-hierarchy)
            # plus the pre-solve apron neck-split, not by post-solve
            # subdivision.

        # Stitch pavement to terminal pads (user 2026-05-04): make
        # the two share an identical vertex sequence on every shared
        # edge — pavement vertices near a terminal corner snap to it,
        # vertices in the edge interior get inserted into the
        # terminal polygon.  Eliminates the 4 sub-metre "step"
        # artefacts that survived the densify-skip guard.
        from .junction_rules import (
            stitch_pavement_polygons,
            stitch_pavement_to_terminals,
        )
        stitch_pavement_to_terminals(layout)
        _covp(layout, "post-stitch-terminals")
        # Adjacent junction polygons whose rings have parallel-but-
        # near-coincident edges should share OSM nids on every shared
        # boundary segment.  Inserts vertices into the other polygon's
        # ring at the projected point with z linearly interpolated
        # along the host edge.  Companion to
        # ``stitch_pavement_to_terminals`` for junction-junction
        # adjacency.  Runs before the corner-snap reconciliation so
        # ``_enforce_shared_vertex_altitudes`` can average shared-
        # bucket altitudes the stitch makes coincident.
        stitch_pavement_polygons(layout)
        _covp(layout, "post-stitch-polygons")

        # (session 51 single-solve) The cross-shape ALTITUDE
        # reconciliation chain (`_snap_junction_altitudes_to_rect_corners`
        # + `_enforce_shared_vertex_altitudes`) and the second
        # `_subdivide_violating_junctions` loop were REMOVED here.  They
        # existed to patch up altitude disagreements introduced by
        # geometry passes that ran AFTER the first solve.  With all
        # geometry finalized before a SINGLE solve, the solver's
        # writeback is lossless — one elevation per shared bucket, so
        # adjacent shapes agree at shared corners by construction and
        # there is nothing to reconcile.

        # Split sloped 4-corner rects where a junction vertex lies on a
        # sloping (long) edge (user 2026-05-12).  De-coupled (session
        # 51): detects sloped rects by ROLE and runs PRE-solve, so the
        # violating junction vertex coincides with a sub-rect short-edge
        # corner (a shared node) before the solver assigns altitudes —
        # the solver then gives both the same value (no step).
        from .junction_repair import _split_sloped_rects_at_violations
        _split_sloped_rects_at_violations(layout, icao=icao)
        _covp(layout, "post-split-sloped")
        # Re-run flat-edge corner snap: the rect split above introduces
        # new sub-rect corners that may not align with adjacent junction
        # vertices.  Snap "almost-at-the-corner" junction vertices
        # (perpendicular ≤ 2 m, corner ≤ 10 m) to the new corners.
        _snap_junction_vertices_to_rect_flat_edge_corners(layout)
        # The split can carve a WEDGE sub-rect whose narrow end cannot
        # carry a plane differential (KPHL K5: a 16.9 m -> 3.3 m quad;
        # the solve later puts 0.2 m across the 3.3 m end = 6 %).
        # Absorb it into the adjacent junction so per-vertex
        # node_altitudes + twist smooth the transition (user
        # 2026-06-12).  The finalize-time pass runs BEFORE this split
        # and cannot see these sub-rects.
        from .junction_repair import _absorb_wedge_rects_into_junctions
        _absorb_wedge_rects_into_junctions(layout, icao=icao)
        _covp(layout, "post-wedge-absorb-2")
        # Single-pass sloping-edge absorption at end of pipeline
        # (user 2026-05-17).  At this point all post-elevation
        # junction-refinement passes have run, so the FINAL
        # geometry is settled.  Transient shared-edge artifacts
        # at junction-emit time (SPJC B/C/E stubs with raw apron
        # residue along their sloping edges) have been resolved
        # by intermediate passes; what remains are GENUINE
        # violations the user wants absorbed (CYXY E sub-rects
        # inside the south apron, etc.).  Replaces the earlier
        # _drop_rects_with_shared_sloping_edge_and_absorb pass
        # which only handled the consecutive-corner case.
        # Absorption (session 47, default OFF): only KEEP rects that share
        # a sloping edge with an apron/junction — a taxilane through an
        # apron should stay a directionally-graded rect, not dissolve into
        # the all-pair apron.  Node parity is automatic (apron = union −
        # rects), so the apron adopts the rect's sloping-edge altitudes
        # with no cliff.  Flip ABSORB_RECTS_ALONGSIDE_APRONS to restore.
        # Apron reclassification (user 2026-05-18): a junction whose
        # boundary strays > 55 m from any taxi/runway centerline
        # contains apron-territory pavement (no centerline running
        # through it) and should be tagged ``role=apron``.  Geometric,
        # not area-based — a 6-way mega-intersection stays a junction.
        #
        # (session 51) Runs BEFORE absorption now: in the single-solve
        # order absorb runs pre-solve, and if it ran on the raw junction
        # set it dissolved nearly every rect against transient
        # apron-territory residue still tagged ``junction`` (CYXY 21->0,
        # SPJC 74->3).  Reclassifying first converts that residue to
        # ROLE_APRON so absorb only targets genuine final junctions.
        from .junction_repair import _reclassify_apron_junctions
        _reclassify_apron_junctions(layout, icao=icao)
        _covp(layout, "post-apron-reclass")
        # (s79) SERVICE-JUNCTION re-role (docs/service_road_carve.md):
        # a junction OR apron whose pavement neighbours are EXCLUSIVELY
        # ``service_road`` rects (the #198 U-turn bulge between the two
        # road legs; the HECA SVC29↔SVC30 connector #336; the HECA
        # SVC29/35/36 plaza #290 that reads as an apron) is road
        # territory — 4 % ``service_junction``, not a 1.5 % aircraft
        # junction/apron.  A shape shared with any aircraft pavement (the
        # road's mouth at taxiway S) stays its original role.
        #
        # ★ MUST run BEFORE the runway-disconnected→groundside pass
        # below: that pass excludes service roads from its airside
        # connectivity graph, so a road-only junction/apron reads
        # "runway-disconnected" and is demoted to DEM groundside before
        # this re-role can claim it — leaving the road runs split by a
        # DEM blob they cannot grade through (the HECA #336 cliff between
        # SVC29 and SVC30; the #290/#312 plaza severed from its SVC roads
        # by the 1 m groundside clearance gap).  Re-roling first makes it
        # a ``service_junction`` (excluded from that pass) so the road
        # network grades continuously across it.  An APRON only qualifies
        # when it touches NO aircraft pavement at all — a real aircraft
        # apron always chains to a taxiway/stub/building, so the guard
        # claims only genuine road plazas.
        if SERVICE_ROAD_CARVE:
            from .layout import ROLE_SERVICE_JUNCTION, ROLE_SERVICE_ROAD
            _aircraft_roles = {
                ROLE_RUNWAY, "primary_parallel", "secondary_parallel",
                ROLE_STUB, "cross_connector", "apron", "building"}
            _n_svc_j = _n_svc_r = 0
            for _ji, _js in enumerate(layout.shapes):
                if _js.role not in ("junction", "apron") \
                        or _js.polygon is None or _js.polygon.is_empty:
                    continue
                _has_road = _has_aircraft = False
                _road_neighbors = 0
                for _os9 in layout.shapes:
                    if _os9 is _js or _os9.polygon is None \
                            or _os9.polygon.is_empty:
                        continue
                    if _os9.role not in _aircraft_roles \
                            and _os9.role != ROLE_SERVICE_ROAD:
                        continue
                    try:
                        if _js.polygon.distance(_os9.polygon) > 0.2:
                            continue
                    except _GEOM_EXC:
                        continue
                    if _os9.role == ROLE_SERVICE_ROAD:
                        _has_road = True
                        _road_neighbors += 1
                    else:
                        _has_aircraft = True
                        break
                # A service road is NARROW (< 15 m wide, user 2026-06-26): only a
                # narrow road/connector piece becomes a service_junction; a WIDE
                # road-only residue (a parking lot carved beside the road — CYXY
                # the 31×61 m piece @(-473,403)) is NOT road, it stays apron/
                # junction (→ groundside if runway-disconnected) so the lot is one
                # surface, not split into a 4 % road plaza + DEM groundside (cliff).
                _narrow = True
                if os.environ.get("O4_SVC_REROLE_NARROW_ONLY", "1") == "1":
                    try:
                        _narrow = _js.polygon.buffer(-7.5).is_empty
                    except _GEOM_EXC:
                        _narrow = True
                if _has_road and not _has_aircraft and _narrow:
                    # A piece that is the sole extension of ONE road is a CONNECTOR
                    # corridor → ``service_road`` (grades AXIALLY, a ramp toward DEM).
                    # ≥2 roads meeting = a real INTERSECTION → all-pair
                    # ``service_junction`` (user 2026-06-27).  A piece that links a
                    # road to a GROUNDSIDE lot is also a connector, but groundside is
                    # not emitted yet here — caught by the post-emit pass below.
                    if (os.environ.get("O4_SVC_CONNECTOR_AS_ROAD", "1") == "1"
                            and _road_neighbors == 1):
                        _js.role = ROLE_SERVICE_ROAD
                        _n_svc_r += 1
                    else:
                        _js.role = ROLE_SERVICE_JUNCTION
                        _n_svc_j += 1
            if _n_svc_j or _n_svc_r:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: re-roled road-only junction/apron(s) "
                    f"→ {_n_svc_r} service_road + {_n_svc_j} service_junction (4 %).")

        # ── FULL-WIDTH SERVICE CORRIDOR consolidation, pass 1 ────────
        # (user 2026-07-05 full-width corridor): a service road is ONE
        # corridor — the truck-route spine has pavement on BOTH sides
        # and the two half-strips are the same surface.  The slice cut
        # each road in half along its own spine and the carve/re-role
        # passes left along-route fragment chains; merge them into
        # full-width corridor pieces NOW, before the road-lot /
        # runway-disconnection classifiers below, so any conversion of
        # road pavement to groundside decides over the full-width slab
        # (whole-shape membership on a full-width shape), never a
        # half-strip notch.  Gate O4_FULL_WIDTH_SERVICE_CORRIDOR.
        from .groundside import consolidate_full_width_service_corridors
        _n_fw = consolidate_full_width_service_corridors(layout)
        if _n_fw:
            UI.vprint(1,
                f"  [pav-builder] {icao}: consolidated service-corridor "
                f"fragments into full-width corridor(s) "
                f"(−{_n_fw} shape(s)).")
            _covp(layout, "post-full-width-corridor-1")

        # A wide paved LOT reachable only via a service road is landside —
        # ONE groundside surface, not a road carved through it.  The
        # on-pavement 1206 carve shreds such a lot into a service_road rect
        # + narrow service_junction frames (CYXY 'Crew cars' loops the lot
        # rim); each fragment is individually narrow so the wide-lot guard
        # in the re-role above never fires.  This runs AFTER that re-role
        # (so the frames are already service_junction) and BEFORE the
        # runway-disconnected pass below (so the merged lot can grade as
        # groundside): a morphological opening on each connected service
        # component extracts the 2-D lot core and reclassifies it to
        # groundside, leaving the narrow connector strips as service_road.
        from .config import ROAD_ONLY_LOT_GROUNDSIDE
        _covp(layout, "pre-road-lots")
        if ROAD_ONLY_LOT_GROUNDSIDE:
            from .junction_repair import (
                _reclassify_road_only_lots_to_groundside)
            _reclassify_road_only_lots_to_groundside(
                layout, icao=icao, dem=dem,
                tile_lat=tile_lat, tile_lon=tile_lon)

        # An apron must have a touch-chain back to a runway (user
        # 2026-06-09); pavement islands without one are landside ramps /
        # parking → groundside (4 %).  MUST run before tile_cut: the
        # tile clip severs cross-tile chains and would false-positive
        # legitimately connected aprons.  Runs AFTER the service-junction
        # re-role above so road-only junctions are already ``service_
        # junction`` (excluded here) rather than being demoted to a DEM
        # groundside blob that splits the road network.
        _covp(layout, "post-road-lots")
        from .junction_repair import (
            _reclassify_runway_disconnected_to_groundside)
        _reclassify_runway_disconnected_to_groundside(
            layout, icao=icao, dem=dem,
            tile_lat=tile_lat, tile_lon=tile_lon)
        _covp(layout, "post-rwy-disconnected")

        # Single-pass sloping-edge absorption (user 2026-05-17): dissolve
        # a sloping rect that shares a sloping edge with a genuine
        # junction perimeter into that junction.  Only KEEP rects that
        # share a sloping edge with an apron/junction — a taxilane
        # through an apron should stay a directionally-graded rect, not
        # dissolve into the all-pair apron.  Node parity is automatic
        # (apron = union − rects).  Flip ABSORB_RECTS_ALONGSIDE_APRONS.
        from .config import ABSORB_RECTS_ALONGSIDE_APRONS
        if ABSORB_RECTS_ALONGSIDE_APRONS:
            from .junction_repair import (
                _absorb_rects_at_junction_perimeters)
            _absorb_rects_at_junction_perimeters(layout, icao=icao)
            # Re-run sloping-edge / flat-edge cleanup against the new
            # geometry: clipped sub-rects from absorption may have new
            # corners that don't yet align with adjacent junction
            # vertices (junction vertex sitting on the new sub-rect's
            # sloping edge interior).
            _split_sloped_rects_at_violations(layout, icao=icao)
            _snap_junction_vertices_to_rect_flat_edge_corners(layout)
            # The split can carve a WEDGE sub-rect whose narrow end
            # cannot carry its plane differential (KPHL K5: 0.2 m
            # across a 3.3 m end).  Absorb it into the adjacent
            # junction (per-vertex altitudes sample the junction's
            # surface) — user 2026-06-12.
            from .junction_repair import (
                _absorb_wedge_rects_into_junctions as _awj)
            _awj(layout, icao=icao)

        # Rule-2 sloping-edge snap, re-run on the FINAL junction set.
        # ``_absorb_rects_at_junction_perimeters`` extends junction
        # perimeters along absorbed-rect edges, which can leave a
        # junction vertex within SLOPING_EDGE_SNAP_M of a NEIGHBOURING
        # sloped rect's long edge (SPJC junction#154 near stub G, #174
        # near parallel U).  It MUST run AFTER apron reclassification:
        # a junction destined to become an apron (Rule 2 doesn't apply
        # to aprons) would otherwise have a boundary vertex yanked
        # across grass to a far rect corner (SPJC apron #189 → M's
        # corner, ~28 m).  Running post-reclassification snaps only
        # genuine final junctions (user 2026-05-20).
        _snap_to_sloping_edge_corners(layout)

        # (session 51) The boundary→DEM bridge re-emit moved POST-solve
        # into the feature-emit phase below — bridges (like the boundary
        # ribbon and groundside) now emit ONCE, after the single solve,
        # against the fully-settled pavement profile.

        # Per user 2026-05-10: shapes cannot cross integer lat/lon
        # tile boundaries (X-Plane / Ortho4XP render each 1°x1° tile
        # separately).  Cut a 10 m gap along every tile boundary
        # line that passes through the airport's pavement footprint;
        # Ortho4XP + X-Plane stitch the seam at render time.
        from .tile_cut import (
            cut_layout_at_tile_boundaries,
            nudge_runway_corners_at_seam_junctions,
        )
        n_tile_delta = cut_layout_at_tile_boundaries(
            layout,
            current_tile_lat=current_tile_lat,
            current_tile_lon=current_tile_lon,
            dem=dem,
        )

        # Tile_cut can leave a junction bridging a runway corner and a
        # terrain-pinned seam stub at an ungradeable step (the runway
        # follows its FAA profile while the stub is pinned to the
        # immutable seam DEM).  Nudge the abutting runway corner toward
        # the seam (user 2026-05-20) so the final solver below can grade
        # the junction.  Detectable only here — the stub is created by
        # tile_cut, after runway-profile redistribution.
        # GLOBAL-SLICE spine: SKIP.  The rule assumes a seam piece is a
        # SMALL stub whose whole body carries the seam DEM — a sliced
        # face can reach the tile line from hundreds of metres away, so
        # the nudge dragged SPLP's runway 02 threshold 5.4 m off its FAA
        # profile through a face vertex 480 m from the seam (cap x 480 m
        # = 7.2 m of legal spread — the slice solver grades the drop
        # across the network; the runway keeps its profile).
        if not _global_slice_spine:
            n_rwy_nudged = nudge_runway_corners_at_seam_junctions(layout)
            if n_rwy_nudged:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: nudged {n_rwy_nudged} runway "
                    f"sub-rect(s) toward seam pavement at junction bridges.")

        # THE single per-surface solver pass (session 51) — runs ONCE,
        # against the FULLY-FINALIZED geometry (all stitch / split /
        # absorb / reclassify / Rule-2 / tile_cut passes above have
        # settled, and every one is de-coupled from altitudes).  The
        # solver seeds soft nodes from the DEM, HARD-anchors runway +
        # seam vertices, and writes one elevation per shared bucket, so
        # the result is grade-compliant AND lossless at shared corners
        # with nothing left to reconcile afterward.
        #
        # Post-cut tile-edge vertices sit at ``half_width_m`` offset
        # from the integer seam line (5 m by default).  The seam
        # vertex itself was removed by the cut; the new vertex is
        # NOT a seam anchor — both this tile's auto_patch and the
        # adjacent tile's auto_patch independently pick altitudes
        # for their own (offset) boundary vertices, and Ortho4XP's
        # terrain mesh interpolates across the 10-m gap at render
        # time.  So the final solver pass is free to cap-project
        # post-cut boundary vertices toward grade compliance.
        # (session 51 per user 2026-05-27) Apron neck-split — split large
        # apron polygons at their narrow necks (taxi-width arm mouths) into
        # convex pads joined by connectors.  Runs at geometry-FINAL (right
        # before the single solve) so the mouth-pair vertices added by
        # stitches / snaps / conformance are all in the ring; running this
        # earlier (in junction_emit) missed many user-expected mouths
        # because one endpoint of each pair wasn't yet a ring vertex.
        from .config import ENABLE_APRON_NECK_SPLIT
        if ENABLE_APRON_NECK_SPLIT:
            from .pavement.apron_necks import split_polygon_at_necks
            from .layout import ROLE_APRON as _R_APRON, BuiltShape as _BS
            # Piece-role re-evaluation for RECLASSIFIED parents (user
            # 2026-07-06): ``_reclassify_apron_junctions`` flips a whole
            # junction to apron when ANY boundary corner strays past the
            # 55 m cap — a 53 k m² spine corridor with one distant bulge
            # (HECA 30.1143,31.4157) flips entirely, and the neck-split
            # pieces inherited apron unconditionally, so strings of
            # corridor cells along the spine emitted under the stand-apron
            # law.  Re-test each piece of a FLIPPED parent with the same
            # geometry rule (``_reeval_apron_piece_role``, promotion-only):
            # pieces hugging a centerline return to ROLE_JUNCTION; open
            # pavement beyond route reach stays apron.  Born-apron parents
            # are NOT re-tested (a genuine stand apron beside a taxiway
            # must keep the apron law).
            from .junction_repair import (
                _aeroway_centerlines_union,
                _APRON_RECLASSIFY_MAX_DISTANCE_M as _RECLASS_CAP_M)
            from .junction_spine import _reeval_apron_piece_role
            _reeval_centerlines = None       # computed lazily, once
            _split_count = 0
            _n_piece_promoted = 0
            _new_shapes: list = []
            for _s in layout.shapes:
                if (_s.role != _R_APRON or _s.polygon is None
                        or _s.polygon.is_empty
                        or _s.polygon.geom_type != "Polygon"):
                    _new_shapes.append(_s)
                    continue
                _pieces = split_polygon_at_necks(_s.polygon)
                if len(_pieces) <= 1:
                    _new_shapes.append(_s)
                    continue
                _split_count += 1
                for _p in _pieces:
                    _piece_role = _R_APRON
                    if getattr(_s, "reclassified_from_junction", False):
                        if _reeval_centerlines is None:
                            _reeval_centerlines = \
                                _aeroway_centerlines_union(layout)
                        _piece_role = _reeval_apron_piece_role(
                            _p, _reeval_centerlines, _RECLASS_CAP_M)
                        if _piece_role != _R_APRON:
                            _n_piece_promoted += 1
                    _ns = _BS(polygon=_p, role=_piece_role, ref=_s.ref)
                    _new_shapes.append(_ns)
            if _split_count:
                layout.shapes = _new_shapes
                _promo_note = (f"; {_n_piece_promoted} corridor piece(s) "
                               f"re-evaluated back to junction"
                               if _n_piece_promoted else "")
                UI.vprint(1,
                    f"  [pav-builder] {icao}: neck-split "
                    f"{_split_count} apron(s) at geometry-final"
                    f"{_promo_note}.")
            # APRON ROUTE-PROXIMITY CUT (USER RULING 2026-07-06): "the
            # portion of any shape more than 50 m from a centerline or
            # runway COULD be apron, but anything less than 50 m from a
            # centerline or runway is NOT apron."  Every ROLE_APRON shape
            # is cut at the config.APRON_ROUTE_PROXIMITY_M contour around
            # the taxi centerlines (service/truck routes excluded — a
            # stand is legitimately served by a road) and the runway
            # union: the near band becomes ROLE_JUNCTION (maneuvering
            # surface, taxi law), the far part keeps the apron/stand law.
            # "No apron ever touches a runway" follows as a corollary.
            # An apron entirely inside the zone re-roles whole; entirely
            # outside stays whole.  Mitre joins / flat caps keep the cut
            # boundary arc-free (same reason as the groundside clip).
            from .config import APRON_ROUTE_PROXIMITY_M
            from .layout import ROLE_JUNCTION as _R_JCT_NEAR
            _near_zone = None
            try:
                _zone_parts = []
                _rwy_union = getattr(layout, "runway_union", None)
                _candidate_lines = []
                for _cl_item in (getattr(layout, "apt_taxi_centerlines",
                                         None) or []):
                    if getattr(_cl_item, "is_service", False):
                        continue
                    _ln = (getattr(_cl_item, "chained_line", None)
                           or getattr(_cl_item, "line", None))
                    if _ln is not None and not _ln.is_empty:
                        _candidate_lines.append(_ln)
                # THROUGH-ROUTES ONLY (KCLT terminal regression,
                # 2026-07-06): a gate LEAD-IN taxilane dead-ends on the
                # stand it serves — counting it toward the 50 m zone
                # makes every stand "near-route" (KCLT: 63 stand aprons
                # re-roled whole; the apron-island merges then lost
                # their hosts and the terminal ramp demoted to DEM
                # groundside).  A centerline joins the zone only when
                # EACH end either joins another taxi centerline or
                # reaches the runway union — dead-end spurs are the
                # stand's own territory, not the movement network.
                _JOIN_TOL_M = 2.0
                _taxi_lines = []
                for _k1, _ln in enumerate(_candidate_lines):
                    try:
                        _ends = (Point(*_ln.coords[0]),
                                 Point(*_ln.coords[-1]))
                    except Exception:
                        continue
                    _through = True
                    for _end in _ends:
                        _joined = (
                            _rwy_union is not None
                            and not _rwy_union.is_empty
                            and _rwy_union.distance(_end) <= _JOIN_TOL_M)
                        if not _joined:
                            for _k2, _other in enumerate(_candidate_lines):
                                if _k2 == _k1:
                                    continue
                                if _other.distance(_end) <= _JOIN_TOL_M:
                                    _joined = True
                                    break
                        if not _joined:
                            _through = False
                            break
                    if _through:
                        _taxi_lines.append(_ln)
                if _taxi_lines:
                    _zone_parts.append(unary_union(_taxi_lines).buffer(
                        APRON_ROUTE_PROXIMITY_M, cap_style=2,
                        join_style=2))
                if _rwy_union is not None and not _rwy_union.is_empty:
                    _zone_parts.append(_rwy_union.buffer(
                        APRON_ROUTE_PROXIMITY_M, join_style=2))
                if _zone_parts:
                    _near_zone = unary_union(_zone_parts)
            except _GEOM_EXC:
                _near_zone = None
            _n_cut = _n_whole = 0
            if _near_zone is not None and not _near_zone.is_empty:
                _cut_shapes: list = []
                for _s in layout.shapes:
                    if (_s.role != _R_APRON or _s.polygon is None
                            or _s.polygon.is_empty
                            or _s.polygon.geom_type != "Polygon"):
                        _cut_shapes.append(_s)
                        continue
                    try:
                        _far = _s.polygon.difference(_near_zone)
                    except _GEOM_EXC:
                        _cut_shapes.append(_s)
                        continue
                    _far_polys = [g for g in getattr(
                        _far, "geoms", [_far])
                        if g.geom_type == "Polygon" and g.area > 1.0]
                    if not _far_polys:
                        # wholly inside the zone → maneuvering surface
                        _s.role = _R_JCT_NEAR
                        _cut_shapes.append(_s)
                        _n_whole += 1
                        continue
                    if sum(g.area for g in _far_polys) \
                            >= _s.polygon.area - 1.0:
                        _cut_shapes.append(_s)      # wholly beyond → apron
                        continue
                    try:
                        _near_part = _s.polygon.intersection(_near_zone)
                    except _GEOM_EXC:
                        _cut_shapes.append(_s)
                        continue
                    _near_polys = [g for g in getattr(
                        _near_part, "geoms", [_near_part])
                        if g.geom_type == "Polygon" and g.area > 1.0]
                    for _g in _near_polys:
                        _cut_shapes.append(_BS(
                            polygon=_g, role=_R_JCT_NEAR, ref=_s.ref,
                            from_route_proximity_cut=True))
                    for _g in _far_polys:
                        _cut_shapes.append(_BS(
                            polygon=_g, role=_R_APRON, ref=_s.ref,
                            from_route_proximity_cut=True))
                    _n_cut += 1
                    if os.environ.get("O4_SLIVER_DEBUG") == "1":
                        _c = _s.polygon.representative_point()
                        print(f"    [apron-cut] area="
                              f"{_s.polygon.area:.0f} at ({_c.x:.0f},"
                              f"{_c.y:.0f}) -> {len(_near_polys)} "
                              f"junction + {len(_far_polys)} apron "
                              f"piece(s)")
                layout.shapes = _cut_shapes
            if _n_cut or _n_whole:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: apron route-proximity cut "
                    f"— {_n_cut} apron(s) split at the "
                    f"{APRON_ROUTE_PROXIMITY_M:.0f} m contour, "
                    f"{_n_whole} re-roled whole (inside the zone).")

        # Reclassify-to-apron + neck-split (above) run AFTER the
        # mid-finalize overlap-clip, so they can leave aprons overlapping
        # junctions / each other (HECA dense S/T/W/J/R cluster).  Resolve
        # those here, BEFORE the final solve, so the solver derives clean
        # node_altitudes for the clipped pieces (pure geometry pass).
        from .elevation import _drop_overlap_against_fixed_shapes
        _drop_overlap_against_fixed_shapes(
            layout, icao=icao, include_aprons=True)

        # (user 2026-06-03) Fold small apron fragments fully enclosed by apron/
        # terminal into their larger neighbour BEFORE the solve — the fragment's
        # edges/elevation disappear and the solver grades the unified apron, so
        # there is no post-solve step to reconcile (a post-solve merge just
        # relocated the cliff).  Runs AFTER the overlap-clip, which itself
        # creates small clipped apron fragments.  Hole-slice safe.
        _covp(layout, "pre-apron-frag-merge")
        from .groundside import merge_small_apron_fragments
        _n_frag = merge_small_apron_fragments(layout)
        if _n_frag:
            UI.vprint(1,
                f"  [pav-builder] {icao}: merged {_n_frag} small apron "
                f"fragment(s) into their host apron (pre-solve).")

        # ── Boundary-interior clip: RETIRED (user 2026-07-16) ─────────
        # The "no shape may straddle the row-130 boundary" invariant
        # (user 2026-05-22) served the boundary RIBBON: straddling
        # pavement was clipped back to the ribbon's inner edge so the
        # two tiled conformingly.  The adjacent-ground law superseded
        # the ribbon (nothing emits ROLE_BOUNDARY shapes any more), and
        # the clip actively harmed features that legitimately live
        # across the line — EGPB's tunnel-ramp chain straddled row-130
        # and the nearest-neighbour altitude resample flattened its
        # sloped rects into a hump.  Shapes now simply keep their
        # geometry wherever they land relative to row-130.

        # ── Groundside emit + absorb/reclassify (refactor Phase 4, PRE-solve) ─
        # Emit groundside pavement (DEM-following, solve-INDEPENDENT — the
        # per-surface solver only grades PAVEMENT_ROLES and is blind to
        # groundside), then settle the airside↔groundside role boundary
        # BEFORE the solve:
        #   * ``_absorb_apron_enclosed_groundside`` folds apron-enclosed
        #     groundside back into the bordering apron (node-shared MERGE), so
        #     the solver grades it as part of the apron — the apron-island
        #     flat-vs-graded cliff is gone at the source instead of being
        #     relocated by a post-solve merge.
        #   * ``_reclassify_groundside_orphan_junctions`` re-tags junctions
        #     that touch ONLY groundside as DEM groundside (the solver then
        #     ignores them) — no airside-flat-vs-DEM cliff at their seam.
        # The groundside DEM-altitude is final here; the post-solve
        # ``_separate_groundside_from_airside`` still opens the clearance gap
        # against the FINAL airside geometry.
        from .groundside import (
            _emit_groundside_pavement_dem as _emit_gs_dem,
            _absorb_apron_enclosed_groundside as _absorb_gs,
            _reclassify_groundside_orphan_junctions as _reclass_gs)
        try:
            _n_gs = _emit_gs_dem(layout, dem, tile_lat, tile_lon)
            if _n_gs:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: emitted {_n_gs} groundside "
                    f"pavement polygon(s) with DEM altitudes (pre-solve).")
        except _GEOM_EXC:
            pass
        _covp(layout, "post-groundside-emit")
        try:
            _n_abs = _absorb_gs(layout)
            if _n_abs:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: absorbed {_n_abs} airside-wedged "
                    f"piece(s) back into apron (pre-solve).")
        except _GEOM_EXC:
            pass
        try:
            _n_orph = _reclass_gs(layout, dem, tile_lat, tile_lon)
            if _n_orph:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: reclassified {_n_orph} "
                    f"groundside-orphan junction(s) as DEM pavement (pre-solve).")
        except _GEOM_EXC:
            pass

        # GROUNDSIDE ROUTE CORRIDORS → service_road (user 2026-07-04, CYXY
        # #206): an OSM-captured groundside piece that IS a truck-route
        # road corridor (route runs through it end-to-end) grades as a
        # ROAD along its route, never as a re-levelled destination lot.
        try:
            from .groundside import reclassify_groundside_route_corridors
            _n_corr = reclassify_groundside_route_corridors(layout)
            if _n_corr:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: re-roled {_n_corr} groundside "
                    f"route-corridor piece(s) → service_road (rides a truck "
                    f"route).")
        except _GEOM_EXC:
            pass

        # GROUNDSIDE-CONNECTOR re-role (user 2026-06-27): a narrow service_junction
        # that links a service road to a groundside lot is a CONNECTOR corridor, not
        # an intersection — re-role it ``service_road`` so it grades AXIALLY (a ramp
        # toward DEM).  Runs HERE, after groundside is emitted, because the earlier
        # service-junction re-role cannot see groundside (not emitted yet).  Gate
        # off → no change.
        if os.environ.get("O4_SVC_CONNECTOR_AS_ROAD", "1") == "1":
            from .layout import (ROLE_SERVICE_JUNCTION, ROLE_SERVICE_ROAD,
                                 ROLE_GROUNDSIDE_PAVEMENT)
            _gs_polys = [g.polygon for g in layout.shapes
                         if g.role == ROLE_GROUNDSIDE_PAVEMENT
                         and g.polygon is not None and not g.polygon.is_empty]
            _n_cr = 0
            for _sj in layout.shapes:
                if (_sj.role != ROLE_SERVICE_JUNCTION or _sj.polygon is None
                        or _sj.polygon.is_empty):
                    continue
                try:
                    if any(_sj.polygon.distance(_gp) <= 0.2 for _gp in _gs_polys):
                        _sj.role = ROLE_SERVICE_ROAD
                        _n_cr += 1
                except _GEOM_EXC:
                    continue
            if _n_cr:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: re-roled {_n_cr} groundside-connector "
                    f"service_junction(s) → service_road (axial ramp).")

        # The road↔lot connection is FIRST-CLASS shared geometry (user
        # 2026-07-04, CYXY P4): insert shared vertices into groundside
        # lot rings at every service-shape mouth so the two emit welded
        # nodes and the groundside mouth-anchor weld binds by canonical
        # key (a mouth landing mid-edge on the lot ring had no key to
        # weld to, and the road emitted 3.1 m below the lot it serves).
        from .groundside import conform_service_mouths_to_groundside
        _n_mouth = conform_service_mouths_to_groundside(layout)
        if _n_mouth:
            UI.vprint(1,
                f"  [pav-builder] {icao}: conformed {_n_mouth} service-road "
                f"mouth vertex(es) into groundside lot ring(s).")
        # Parallel service roads within 2 m get matching projected
        # vertices so the DEM-follow's proximity coupling can bind them
        # (user 2026-07-06, HECA #578↔#64: offset nodes left a 0.9 m
        # mid-edge wall across a 1 m gap).
        from .groundside import conform_parallel_service_edges
        _n_parallel = conform_parallel_service_edges(layout)
        if _n_parallel:
            UI.vprint(1,
                f"  [pav-builder] {icao}: conformed {_n_parallel} "
                f"parallel service-road vertex(es) across narrow gaps.")

        # APRON-EDGE GRADE ADOPTION (USER RULING 2026-07-06, clarified):
        # "the portion of service road that is inside, or running along
        # the edge of an apron follows apron grading.  A service road
        # that leaves the apron ... the portion beyond the apron grades
        # at service road rules."  PORTION-based, like the 50 m cut:
        # eligible roads/junctions (≥ 1 m shared boundary with an apron)
        # are SPLIT at the apron-adjacency band contour — the band =
        # apron union buffered one road width (a road running alongside
        # adopts across its full width; a road leaving the apron exits
        # the band within ~a road width of the mouth).  Inside pieces
        # set ``adopts_apron_grade`` (solver caps + o4_grade_law tag);
        # outside pieces keep the service law.
        from .config import SERVICE_ROAD_WIDTH_M as _SVC_W_ADOPT
        from .layout import (ROLE_SERVICE_ROAD as _RSR_ADOPT,
                             ROLE_SERVICE_JUNCTION as _RSJ_ADOPT,
                             ROLE_APRON as _RAPR_ADOPT,
                             BuiltShape as _BS_ADOPT)
        _APRON_EDGE_BAND_M = float(_SVC_W_ADOPT) + 2.0
        _apron_polys_adopt = [
            _s.polygon for _s in layout.shapes
            if _s.role == _RAPR_ADOPT and _s.polygon is not None
            and not _s.polygon.is_empty
            and _s.polygon.geom_type == "Polygon"]
        _adopt_band = None
        if _apron_polys_adopt:
            try:
                _adopt_band = unary_union(_apron_polys_adopt).buffer(
                    _APRON_EDGE_BAND_M, join_style=2)
            except _GEOM_EXC:
                _adopt_band = None
        _n_adopt_whole = _n_adopt_split = 0
        if _adopt_band is not None and not _adopt_band.is_empty:
            _adopt_boundary = unary_union(
                [_p.exterior for _p in _apron_polys_adopt])
            _adopt_shapes: list = []
            for _s in layout.shapes:
                if (_s.role not in (_RSR_ADOPT, _RSJ_ADOPT)
                        or _s.polygon is None or _s.polygon.is_empty
                        or _s.polygon.geom_type != "Polygon"):
                    _adopt_shapes.append(_s)
                    continue
                try:
                    _shares_edge = (
                        _s.polygon.exterior.distance(_adopt_boundary)
                        < 0.05
                        and _s.polygon.exterior.buffer(0.05)
                        .intersection(_adopt_boundary).length >= 1.0)
                except _GEOM_EXC:
                    _shares_edge = False
                if not _shares_edge:
                    _adopt_shapes.append(_s)
                    continue
                try:
                    _outside = _s.polygon.difference(_adopt_band)
                except _GEOM_EXC:
                    _adopt_shapes.append(_s)
                    continue
                _outside_polys = [g for g in getattr(
                    _outside, "geoms", [_outside])
                    if g.geom_type == "Polygon" and g.area > 1.0]
                if not _outside_polys:
                    # wholly inside/alongside the apron → adopts whole
                    _s.adopts_apron_grade = True
                    _adopt_shapes.append(_s)
                    _n_adopt_whole += 1
                    continue
                try:
                    _inside = _s.polygon.intersection(_adopt_band)
                except _GEOM_EXC:
                    _adopt_shapes.append(_s)
                    continue
                _inside_polys = [g for g in getattr(
                    _inside, "geoms", [_inside])
                    if g.geom_type == "Polygon" and g.area > 1.0]
                if not _inside_polys:
                    _adopt_shapes.append(_s)
                    continue
                for _g in _inside_polys:
                    _adopt_shapes.append(_BS_ADOPT(
                        polygon=_g, role=_s.role, ref=_s.ref,
                        adopts_apron_grade=True,
                        from_route_proximity_cut=True))
                for _g in _outside_polys:
                    _adopt_shapes.append(_BS_ADOPT(
                        polygon=_g, role=_s.role, ref=_s.ref,
                        from_route_proximity_cut=True))
                _n_adopt_split += 1
            layout.shapes = _adopt_shapes
        if _n_adopt_whole or _n_adopt_split:
            UI.vprint(1,
                f"  [pav-builder] {icao}: apron-edge grade adoption — "
                f"{_n_adopt_whole} service shape(s) adopted whole, "
                f"{_n_adopt_split} split at the apron band (user rule).")

        # TAXIWAY-EDGE GRADE ADOPTION (USER RULING 2026-07-07, durable —
        # STATUS part 29 item 4): like the apron-edge rule, the PORTION of
        # a service road that is INSIDE, or SHARES A LONG EDGE with, a
        # TAXIWAY follows the more limiting (taxiway) grade law — 1.5 %
        # (letter-aware) instead of the road's 5 %.  Only isolated narrow-
        # road stretches (nothing along their long edge) keep the full road
        # cap.  PORTION-based, split at the band exactly like the apron
        # rule.  The taxiway band = union of the taxi family (ROLE_JUNCTION
        # corridors + taxi rect roles) buffered one road width + 2 m; a road
        # running alongside adopts across its width; a road leaving exits the
        # band within ~a road width of the mouth.  APRON (1 %) is MORE
        # limiting than taxi (1.5 %), so a piece already adopting apron is
        # left untouched — this pass runs AFTER the apron pass and skips
        # apron-adopted shapes.
        from .layout import (taxi_shape_code_letter as _TAXI_LETTER,
                             ROLE_JUNCTION as _RJ_TX,
                             ROLE_PRIMARY_PARALLEL as _RPP_TX,
                             ROLE_SECONDARY_PARALLEL as _RSP_TX,
                             ROLE_STUB as _RSTUB_TX,
                             ROLE_CROSS_CONNECTOR as _RCC_TX)
        _TAXI_FAMILY_TX = frozenset({
            _RJ_TX, _RPP_TX, _RSP_TX, _RSTUB_TX, _RCC_TX})
        _TAXI_EDGE_BAND_M = float(_SVC_W_ADOPT) + 2.0
        _taxi_shapes_tx = [
            _s for _s in layout.shapes
            if _s.role in _TAXI_FAMILY_TX and _s.polygon is not None
            and not _s.polygon.is_empty
            and _s.polygon.geom_type == "Polygon"]
        _taxi_polys_tx = [_s.polygon for _s in _taxi_shapes_tx]
        _taxi_band = None
        if _taxi_polys_tx:
            try:
                _taxi_union_tx = unary_union(_taxi_polys_tx)
                _taxi_band = _taxi_union_tx.buffer(
                    _TAXI_EDGE_BAND_M, join_style=2)
            except _GEOM_EXC:
                _taxi_band = None

        def _adjacent_taxi_letter(_road_poly):
            """ICAO code letter of the taxi shape this road hugs (nearest
            by boundary distance); None when unavailable → uniform 1.5 %."""
            _best_d, _best_let = float("inf"), None
            for _ts in _taxi_shapes_tx:
                try:
                    _d = _road_poly.distance(_ts.polygon)
                except _GEOM_EXC:
                    continue
                if _d < _best_d:
                    _best_d = _d
                    _best_let = _TAXI_LETTER(layout, _ts)
            return _best_let

        _n_taxi_whole = _n_taxi_split = 0
        if _taxi_band is not None and not _taxi_band.is_empty:
            _taxi_boundary = unary_union(
                [_p.exterior for _p in _taxi_polys_tx])
            _taxi_adopt_shapes: list = []
            for _s in layout.shapes:
                if (_s.role not in (_RSR_ADOPT, _RSJ_ADOPT)
                        or _s.polygon is None or _s.polygon.is_empty
                        or _s.polygon.geom_type != "Polygon"
                        # apron (1 %) already claimed this piece → keep it
                        or getattr(_s, "adopts_apron_grade", False)):
                    _taxi_adopt_shapes.append(_s)
                    continue
                try:
                    _shares_edge = (
                        _s.polygon.exterior.distance(_taxi_boundary)
                        < 0.05
                        and _s.polygon.exterior.buffer(0.05)
                        .intersection(_taxi_boundary).length >= 1.0)
                except _GEOM_EXC:
                    _shares_edge = False
                # INSIDE a taxiway (road overlapping taxi pavement) adopts
                # whole even if it shares no boundary edge.
                try:
                    _inside_taxi = (
                        _s.polygon.intersection(_taxi_union_tx).area > 1.0)
                except _GEOM_EXC:
                    _inside_taxi = False
                if not _shares_edge and not _inside_taxi:
                    _taxi_adopt_shapes.append(_s)
                    continue
                _let = _adjacent_taxi_letter(_s.polygon)
                try:
                    _outside = _s.polygon.difference(_taxi_band)
                except _GEOM_EXC:
                    _taxi_adopt_shapes.append(_s)
                    continue
                _outside_polys = [g for g in getattr(
                    _outside, "geoms", [_outside])
                    if g.geom_type == "Polygon" and g.area > 1.0]
                if not _outside_polys:
                    # wholly inside/alongside the taxiway → adopts whole
                    _s.adopts_taxi_grade = True
                    _s.adopted_taxi_letter = _let
                    _taxi_adopt_shapes.append(_s)
                    _n_taxi_whole += 1
                    continue
                try:
                    _inside = _s.polygon.intersection(_taxi_band)
                except _GEOM_EXC:
                    _taxi_adopt_shapes.append(_s)
                    continue
                _inside_polys = [g for g in getattr(
                    _inside, "geoms", [_inside])
                    if g.geom_type == "Polygon" and g.area > 1.0]
                if not _inside_polys:
                    _taxi_adopt_shapes.append(_s)
                    continue
                for _g in _inside_polys:
                    _taxi_adopt_shapes.append(_BS_ADOPT(
                        polygon=_g, role=_s.role, ref=_s.ref,
                        adopts_taxi_grade=True, adopted_taxi_letter=_let,
                        from_route_proximity_cut=True))
                for _g in _outside_polys:
                    _taxi_adopt_shapes.append(_BS_ADOPT(
                        polygon=_g, role=_s.role, ref=_s.ref,
                        from_route_proximity_cut=True))
                _n_taxi_split += 1
            layout.shapes = _taxi_adopt_shapes
        if _n_taxi_whole or _n_taxi_split:
            UI.vprint(1,
                f"  [pav-builder] {icao}: taxiway-edge grade adoption — "
                f"{_n_taxi_whole} service shape(s) adopted whole, "
                f"{_n_taxi_split} split at the taxi band (user rule).")

        # (refactor Phase 5) The boundary ribbon + boundary→DEM bridge emit
        # and their airside vertex touches (_snap_bridge_vertices_to_runway_
        # corners, _insert_bridge_contacts_into_junctions) CANNOT move
        # pre-solve: the ribbon/bridge runway-distance clamp anchors to ALL
        # airside pavement (incl. aprons/taxiways, whose altitudes are only
        # known after the solve), so the bridge PLACEMENT is solve-dependent.
        # They stay post-solve; the bridge-contact insert is altitude-neutral
        # (collinear) and the post-solve feature conformance (Phase 7) keeps
        # the partition conforming without moving frozen airside vertices.

        # (refactor Phases 6+7) The partial pre-solve apron/junction
        # conformance is SUPERSEDED by the full airside node-unification
        # (_unify_airside_geometry) run below, just before the solve.

        # ── Pre-solve shape-drop passes (refactor Phase 2) ────────────
        # Drop small floating-orphan junctions left by
        # pav_union.difference(rects) — a wedge past a rect's edge that
        # shares no vertex with any shape (so no merge/sliver pass can
        # absorb it) and whose corners are all orphans (SPLP #33).  Pure
        # topology (area + shared-vertex test, no altitude dependency), so
        # it moved PRE-solve: the dropped wedge never enters the solver's
        # node graph or the weld/conformance node-set.
        from .junction_repair import _drop_floating_orphan_junctions
        _drop_floating_orphan_junctions(layout, icao=icao)

        # Drop small apron/junction residue that rests almost entirely OFF
        # the source pavement union — a thin strip beside a shoulder-widened
        # runway, or residue from a dropped runway-parallel centerline
        # (HECA #258/#228).  source_pavement_union is the authoritative real-
        # pavement footprint, so off-source residue is spurious.  Pure
        # geometry (area + on-source fraction), also moved PRE-solve.
        from .junction_repair import _drop_off_source_residue
        _drop_off_source_residue(layout, icao=icao)

        # Hole-free normalization: decompose any apron/junction still
        # carrying an interior ring (e.g. the overlap-clip's carve of a
        # wholly-contained terminal pad) into hole-free pieces with
        # conforming cuts.  MUST run before _unify_airside_geometry —
        # its weld rebuilds Polygon(exterior) and silently FILLS holes,
        # re-covering the carved terminal (KSDL terminal1, HECA ×3);
        # and to_osm writes exterior rings only, so a hole could never
        # reach the patch anyway.
        from .junction_repair import _decompose_airside_holed_shapes
        _decompose_airside_holed_shapes(layout, icao=icao)
        # Re-run the off-source residue drop on the decompose output:
        # splitting a big holed apron exposes filled boundary-bay
        # pockets (1to1 straightening chords over grass) as separate
        # ~0%-on-source pieces that the pre-decompose pass could not
        # see inside the monolithic shape.
        _drop_off_source_residue(layout, icao=icao)

        # Rect end-caps (gate RECT_END_CAPS; rect_end_caps.py).  Carve a
        # flat cap off each junction-facing sloping-rect end RIGHT BEFORE the
        # spine — after every dissolve/merge/drop pass that would otherwise
        # eat it, and after the junctions exist.  The rect body shrinks to a
        # full-length 4-corner plane; the cap takes its vacated 2 m so the
        # spine welds onto the flat cap (a soft junction edge) instead of the
        # rect's sloping edge, leaving the rect free to grade its whole
        # length.  Gate-off = no carve (byte-identical).
        from .config import RECT_END_CAPS, RECT_END_CAP_DEPTH_M
        if RECT_END_CAPS:
            from .rect_end_caps import carve_rect_end_caps_before_spine
            carve_rect_end_caps_before_spine(
                layout, depth_m=RECT_END_CAP_DEPTH_M)

        # Junction/apron centerline-spine SLICE (gated JUNCTION_CENTERLINE_
        # SPINE; docs/junction_centerline_spine.md).  PURE GEOMETRY, run
        # HERE — right after the hole cuts and BEFORE _unify_airside_
        # geometry + the solver — so the unify pass welds the new shared
        # centerline nodes and the elevation solver grades the sliced
        # surface coherently (the corridor profile is the solver's job).
        # Gate-off = no-op (shapes survive unchanged → byte-identical).
        # SYNTHETIC turn-fillet arcs (user 2026-06-30, gate O4_TAXI_FILLET): the
        # sparse 1201/1202 network has no arc where two taxi routes meet, so add
        # the standard-radius fillet to the ROUTE GRAPH here — BEFORE the spine
        # slice — and it is cut into the junctions like any taxiway centerline.
        # Curve-native / route-arc gates: the global slice already cut every
        # centerline into the pav_union, so the fillet/synthetic/slice feeders
        # are all bypassed (the faces are the spine).  NOTE the route-arc
        # spine itself now runs at the GLOBAL-SLICE stage (user ruling
        # 2026-07-02: with the full spine, no taxi rects — the spine runs
        # everywhere), not at this per-junction pre-slice hook.
        if not _global_slice_spine:
            from .taxi_route_fillets import add_junction_fillet_arcs
            add_junction_fillet_arcs(layout, icao)

            # SYNTHETIC junction spines (user 2026-06-26): every junction must
            # have a route through it.  Append synthetic centerlines for
            # spineless junctions BEFORE the slice, so they are sliced + graded
            # like any taxiway.  Recognized painted centerlines are the real
            # spine; do NOT manufacture straight synthetic centerlines for
            # spineless junctions (user 2026-06-30: no synthetic centerlines for
            # any spine when recognition is on).
            if (os.environ.get("O4_SYNTH_JUNCTION_SPINE", "1") == "1"
                    and os.environ.get("O4_RECOGNIZED_CENTERLINES", "0") != "1"):
                from .synthetic_junction_spine import synthesize_junction_spines
                synthesize_junction_spines(layout, icao)

            from .junction_spine import apply_junction_centerline_spine
            apply_junction_centerline_spine(layout)

        # LATERAL corridor nodes (user 2026-06-26): a vertex on each apron/
        # junction edge within ±half-taxi-width of a spine, so the lateral grade
        # spine→apron is solved AND validated (else a steep runway-side drop is
        # invisible to the vertex-based check).  BEFORE conformance so the new
        # vertices propagate to neighbouring shapes.
        if os.environ.get("O4_LATERAL_SPINE_NODES", "1") == "1":
            from .lateral_spine_nodes import insert_lateral_spine_nodes
            insert_lateral_spine_nodes(layout, icao)

        # DENSIFY JUNCTION EDGES (user 2026-06-26): a junction is a taxiway that
        # follows its spine, so every junction exterior edge is subdivided to the
        # spine node spacing — a long edge with only its 2 corners can't track the
        # spine's rise (junction #97's 500 m flat edge).
        if os.environ.get("O4_DENSIFY_JUNCTION_EDGES", "1") == "1":
            from .lateral_spine_nodes import densify_junction_edges
            densify_junction_edges(layout, icao)

        # SPINE-FIRST service roads (config.SVC_SPINE_FIRST, part 30m):
        # project each SERVICE (truck-route) spine station onto the road /
        # service-junction edges so the road's new within-shape law
        # (grade_graph SOFT_VISIBILITY_ROLES gains service_road under the
        # same gate) binds aligned cross-section pairs at station spacing —
        # a road's long edges are otherwise vertex-free for 70-100 m and
        # the 2 % transverse law has nothing to sample (the CYXY cross-road
        # tear).  Same pre-conformance slot as the taxi lateral pass above.
        from .config import SVC_SPINE_FIRST as _SVC_SPINE_FIRST
        if _SVC_SPINE_FIRST:
            from .lateral_spine_nodes import insert_service_lateral_nodes
            insert_service_lateral_nodes(layout, icao)

        # Round tight pavement turn-backs (sharp tip / narrow flat end) into a
        # ~5-node half-circle so the boundary turns on a smooth arc (user
        # 2026-06-30, gate O4_ROUND_TURNBACK).  Pre-solve so the arc is graded;
        # the unify/clip/planarize below reconcile shared edges.
        from .pavement.vertices import _round_turnback_corners
        _round_turnback_corners(layout, icao)

        # ── Formation-time SOURCE CLIP (KCLT off-source phantom, Fix C) ──
        # An apron / junction face can acquire off-source area through
        # DOWNSTREAM recuts (route-proximity cut, frontage straightening) even
        # though the slice birthed it 100 % on source — KCLT #278 (8.3 k m² at
        # 35 %) is the near-runway band carved off a real 18R-end apron.  Clip
        # every < 50 %-on-source apron / junction back to the source pavement
        # (∪ runway halo) HERE — after merge_small_apron_fragments / groundside
        # emit / full-width-corridor consolidation (pass 1) have settled the
        # apron/junction set, and BEFORE _unify_airside_geometry below so the
        # clipped edges are re-noded, welded, and graded like any other pre-
        # solve geometry.  Gate O4_SOURCE_CLIP → no-op (byte-identical) off.
        if os.environ.get("O4_SOURCE_CLIP", "1") == "1":
            from .junction_repair import source_clip_partial_coverage_shapes
            source_clip_partial_coverage_shapes(layout, icao=icao)
            _covp(layout, "post-source-clip")

        # ── Airside node-unification (refactor Phases 6+7, PRE-solve) ──
        # Weld + full conformance + final corner snaps, run HERE so the solver
        # sees the FINAL node-set and grades every shared vertex to ONE
        # altitude — the post-solve coincident-vertex cliffs (#291↔#371 class)
        # are eliminated at the source.  THE cliff fix.
        _unify_airside_geometry(layout, icao)
        _airside_unified_presolve = True

        # (s79) FINAL pre-solve overlap clip: the unify pass's vertex
        # snaps (weld / corner snaps / conformance) can sweep an apron
        # or junction edge back ONTO a fixed rect — measured at CYXY:
        # the pav[1] ROAD rect re-acquired a 13.5 m² apron overlap
        # AFTER the mid-finalize clip had zeroed it.  The clip is
        # idempotent, pure geometry, and pre-solve (node_altitudes not
        # yet derived), so re-running it here closes the window.
        if SERVICE_ROAD_CARVE:
            from .elevation import _drop_overlap_against_fixed_shapes
            _drop_overlap_against_fixed_shapes(
                layout, icao=icao, include_aprons=True)

        # Finalize airside geometry PRE-solve (single-grade-graph Phase 0,
        # docs/single_grade_graph.md): the solver must grade the SAME node-set
        # the validator checks, so every airside GEOMETRY change must precede
        # the solve.  These two are pure-geometry cleanups of artifacts the
        # slice / weld / conformance chain leaves behind — coincident ring
        # vertices and illegal mid-flat-edge nodes on sloping rects — that used
        # to run post-solve.  Moving them here makes the graded geometry final
        # before the solve; the idempotent post-solve copies below then find
        # nothing to do (so the geom-guard reports 0 airside changes, modulo the
        # documented solve-dependent bridge-contact inserts).
        if os.environ.get("O4_PRESOLVE_CLEAN", "1") == "1":
            # O4_RING_NEEDLE_COLLAPSE=0 is the defect-4a A/B lever (keeps
            # the zero-length dedup, skips only the needle collapse).
            _dedup_coincident_ring_vertices(
                layout, icao,
                collapse_needles=os.environ.get(
                    "O4_RING_NEEDLE_COLLAPSE", "1") == "1")
            from .flatedge_snap import drop_flatedge_nodes as _pre_flatedge
            _pre_flatedge(layout)

        # Pre-solve geometry guard (dev, O4_GEOM_GUARD=1): snapshot every
        # airside shape's ring geometry HERE, immediately before the solve,
        # so the comparison at emit can report how many airside shapes had
        # their geometry changed by a post-solve pass — the metric the
        # pre-solve-geometry refactor drives to 0 (see
        # docs/presolve_geometry_refactor.md).  No behaviour change.
        from .geom_guard import snapshot_airside_geometry
        _geom_guard_snap = snapshot_airside_geometry(layout)

        # ROAD-ONLY junction re-role (user 2026-06-27), at FINAL pre-solve geometry:
        # a junction with a GROUND-TRUCK (apt.dat 1206) route running THROUGH it and
        # NO aircraft taxiway is part of the service-road network, not an aircraft
        # junction — re-role it ``service_road`` so it grades AXIALLY / follows DEM
        # instead of being pinned flat at the airside bowl level.  Two ways in: a
        # truck route runs through it (wide truck yard / corridor — CYXY shape 151,
        # 340×45 m, 365 m of 1206 through, 0 m taxi), OR it is a thin residue strip
        # sharing nodes only with a service road (the spine-slice sliver — CYXY
        # SVC4's 109×0.8 m piece).  The earlier road-only re-role ran before the
        # slice and the sliver-merge only folds junction→junction, so these survive.
        # Aircraft adjacency (apron/runway/taxi rect) or an aircraft taxi-line
        # through it VETOES the re-role; boundary adjacency does not.  Gate → no-op.
        if os.environ.get("O4_SVC_CONNECTOR_AS_ROAD", "1") == "1":
            from .layout import (ROLE_SERVICE_ROAD, ROLE_JUNCTION)
            _cps = layout.canonical_points

            def _rk(x, y):
                return _cps.get_or_add(float(x), float(y))

            def _ring_keys(sh):
                c = list(sh.polygon.exterior.coords)
                if c and c[0] == c[-1]:
                    c = c[:-1]
                return {_rk(x, y) for (x, y) in c}

            # AIRCRAFT pavement (sharing nodes with it = a real aircraft junction).
            _aircraft = (ROLE_RUNWAY, "primary_parallel", "secondary_parallel",
                         ROLE_STUB, "cross_connector", "apron", "building")
            _aircraft_keys: set = set()
            _road_keys: set = set()
            for _o in layout.shapes:
                if _o.polygon is None or _o.polygon.is_empty:
                    continue
                if _o.role in _aircraft:
                    _aircraft_keys |= _ring_keys(_o)
                elif _o.role == ROLE_SERVICE_ROAD:
                    _road_keys |= _ring_keys(_o)
            _svc_lines = [cl.line for cl
                          in (getattr(layout, "apt_service_centerlines", None)
                              or []) if cl.line is not None and not cl.line.is_empty]
            _taxi_lines = [cl.line for cl
                           in (getattr(layout, "apt_taxi_centerlines", None) or [])
                           if cl.line is not None and not cl.line.is_empty
                           and not cl.is_service]
            _n_sl = 0
            for _s in layout.shapes:
                if (_s.role != ROLE_JUNCTION or _s.polygon is None
                        or _s.polygon.is_empty):
                    continue
                _keys = _ring_keys(_s)
                _airc = len(_keys & _aircraft_keys)
                if _airc > 2:                       # real aircraft adjacency → keep
                    continue
                try:
                    if any(ln.intersects(_s.polygon)
                           and ln.intersection(_s.polygon).length > 5.0
                           for ln in _taxi_lines):  # aircraft taxi through → keep
                        continue
                    _truck = sum(ln.intersection(_s.polygon).length
                                 for ln in _svc_lines if ln.intersects(_s.polygon))
                    _narrow = _s.polygon.buffer(-7.5).is_empty
                except _GEOM_EXC:
                    continue
                _road_shared = len(_keys & _road_keys)
                # (a) a truck route runs through it, or (b) a thin residue strip
                # sharing nodes with a service road.
                if _truck >= 15.0 or (_narrow and _road_shared >= 1):
                    _s.role = ROLE_SERVICE_ROAD
                    _n_sl += 1
            if _n_sl:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: re-roled {_n_sl} road-only "
                    f"junction(s) → service_road (truck route / sliver).")

            # The junction→service_road re-role above runs AFTER the
            # connectivity classifier (it can only see a junction's road-only
            # adjacency once unify/overlap-clip/tile_cut have settled), so an
            # apron cluster bridged to the airside only through that junction is
            # now orphaned behind a service road but still tagged ``apron``
            # (LPHR: 6 west-side aprons).  Re-run the runway-disconnection
            # classifier, SCOPED to components that touch a service road, to
            # demote those orphans to groundside.  ``require_service_adjacency``
            # keeps the post-tile_cut re-run from false-positiving an apron
            # whose aircraft-pavement chain was merely severed by the seam gap.
            # ... and the narrow-strip CARVE orphans the same way (user
            # 2026-07-04, CYXY): a lot/pad severed from the aircraft
            # chain by a carved service corridor — plus the apron
            # neck-split above can mint the orphan fragment only after
            # the first classifier run.
            if _n_sl or _n_strip:
                from .junction_repair import (
                    _reclassify_runway_disconnected_to_groundside)
                _reclassify_runway_disconnected_to_groundside(
                    layout, icao=icao, dem=dem,
                    tile_lat=tile_lat, tile_lon=tile_lon,
                    require_service_adjacency=True)

        # NOTE (user 2026-06-30): sub-2000 m² aprons are NOT demoted to junction.
        # The goal of the old demotion was to keep them UNANCHORED (a small
        # decomposition fragment shouldn't pin the network to its DEM level); that
        # is now handled directly by the apron-seat area filter in
        # build_nobuilding_apron_seats, so the shape keeps role=apron — correct
        # geometry that passes the junction invariants (HECA #455 stays an apron).

        # ── SIMPLE-SHAPES INVARIANT (user 2026-07-03) ─────────────────────
        # No airside shape may reach the solver/emit with INTERIOR rings:
        # the OSM emit drops them (rect-era X-Plane compat), which paves
        # over the grass hole.  Slice-level hole keyholes handle most; the
        # post-slice MERGE passes can re-create an annulus by unioning the
        # faces around a hole.  Decompose any survivor with the (modernised)
        # hole slicer — the same _decompose_polygon_with_holes the rect
        # model ran in junction_emit, which the global slice bypasses.
        if _global_slice_spine:
            from .pavement.junctions import _decompose_polygon_with_holes
            from .layout import ROLE_SERVICE_JUNCTION as _RSJ2
            _new_shapes = []
            _n_decomp = 0
            for _s in layout.shapes:
                _p = _s.polygon
                if (_s.role in (ROLE_APRON, ROLE_JUNCTION, _RSJ2)
                        and _p is not None and not _p.is_empty
                        and _p.geom_type == "Polygon" and _p.interiors):
                    try:
                        _pieces = _decompose_polygon_with_holes(_p)
                    except Exception:
                        _pieces = [_p]
                    if _pieces and (len(_pieces) > 1
                                    or not _pieces[0].interiors):
                        _n_decomp += 1
                        for _pc in _pieces:
                            _new_shapes.append(BuiltShape(
                                polygon=_pc, role=_s.role, ref=_s.ref,
                                source_axis=_s.source_axis,
                                is_bridge=_s.is_bridge))
                        continue
                _new_shapes.append(_s)
            if _n_decomp:
                layout.shapes[:] = _new_shapes
                UI.vprint(1,
                    f"  [pav-builder] {icao}: decomposed {_n_decomp} "
                    f"holed airside shape(s) into simple pieces "
                    f"(pre-solve).")

        # FINAL scoped runway-disconnection sweep (user 2026-07-04): the
        # narrow-strip carve + the passes after the 2nd classifier run
        # (simple-shapes decomposition, overlap clips) can leave an apron
        # fragment whose ONLY remaining touches are service shapes —
        # orphaned too late for the earlier runs (CYXY: the 692 m² pad at
        # (60.710421,-135.0725738) that is only accessed via the Crew-cars
        # road).  Service-adjacency scoping keeps seam-gapped aprons safe.
        if _n_strip:
            from .junction_repair import (
                _reclassify_runway_disconnected_to_groundside)
            _reclassify_runway_disconnected_to_groundside(
                layout, icao=icao, dem=dem,
                tile_lat=tile_lat, tile_lon=tile_lon,
                require_service_adjacency=True)
            # A lot and the demoted connector piece serving it are ONE
            # groundside surface (user 2026-07-04, CYXY P4: two
            # independently DEM-followed pieces met at coincident nodes
            # 2.6 m apart).  Merge BEFORE the solve so the road-mouth
            # weld binds against the single merged lot.
            from .groundside import _merge_touching_groundside
            _n_gsm = _merge_touching_groundside(
                layout, dem, tile_lat, tile_lon)
            if _n_gsm:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: merged {_n_gsm} touching "
                    f"groundside piece(s) (pre-solve).")

        # ── FULL-WIDTH SERVICE CORRIDOR consolidation, pass 2 ────────
        # (user 2026-07-05 full-width corridor): the groundside
        # route-corridor conversion, groundside-connector re-role and
        # the final road-only junction re-role above all mint NEW
        # service_road pieces after pass 1 ran — fold them into their
        # corridor now, at final pre-solve geometry, so the solver
        # grades each service corridor as ONE full-width surface.
        from .groundside import consolidate_full_width_service_corridors
        _n_fw2 = consolidate_full_width_service_corridors(layout)
        if _n_fw2:
            UI.vprint(1,
                f"  [pav-builder] {icao}: consolidated service-corridor "
                f"fragments into full-width corridor(s) "
                f"(−{_n_fw2} shape(s), final pre-solve).")
            _covp(layout, "post-full-width-corridor-2")

        # ── Runway-end-skirt PRE-SOLVE construction (Slice B stage B1,
        # gate O4_ONE_SOLVE_TERRAIN + O4_ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT,
        # both default OFF; docs/slice_b_solver_absorption_design.md §B1) ─
        # The runway-end skirt is the FIRST terrain feature absorbed into
        # the one-solve graph.  Its footprint + ring geometry are built
        # HERE, before ``per_surface_solve``, so the ring vertices join the
        # canonical node registry and the solver node list (the object-
        # bridge plate admission pattern, wired in stage B0's
        # ``_build_node_list`` hook) and every vertex becomes a HARD PIN at
        # its birth-computed profile value (``_seed_elevations`` skirt-pin
        # block).  The solver then grades neighbouring pavement to MEET the
        # pins and never reshapes them, exactly as it treats the object-
        # bridge deck pins.  Dependency (design §B1, verified at CYXY 2026-
        # 07-10): skirt values derive from the RUNWAY profile, which is
        # already redistributed to hard CIFP anchors far above (line ~4020)
        # — the taxi/apron/junction rects still carry None altitudes here,
        # so ``_nearest_pav_alt`` reads None off an unsolved neighbour and
        # the ref falls back to the runway value; the footprint is geometry-
        # only and identical to the legacy post-solve build (no CYXY skirt
        # abuts a post-solve-emitted feature — ribbon / groundside / tunnel
        # / bridge / clearance-cut — so the static-block clip against those
        # is a no-op at this airport).  With the gate OFF this block does
        # not run and the legacy post-solve emitter below fires unchanged.
        from .config import (ONE_SOLVE_TERRAIN,
                             ONE_SOLVE_TERRAIN_GAP_FILL_SPINE,
                             ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT)
        # GATE DEPENDENCY (Slice B stage B2, ratified 2026-07-10): the
        # gap sub-gate REQUIRES the skirt sub-gate — gap parents include
        # the runway-end skirts, which exist pre-solve only under B1.
        # HARD ERROR, not force-ON (fail-loudly doctrine): silently
        # widening the operator's gate scope would corrupt any A/B and
        # hide intent; the only supported configuration is both ON.
        if (ONE_SOLVE_TERRAIN and ONE_SOLVE_TERRAIN_GAP_FILL_SPINE
                and not ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT):
            raise ValueError(
                "O4_ONE_SOLVE_TERRAIN_GAP_FILL_SPINE requires "
                "O4_ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT: gap-fill "
                "pre-solve construction reads the runway-end skirts as "
                "gap parents, which exist pre-solve only under the "
                "skirt sub-gate (Slice B stage B2 dependency; "
                "docs/slice_b_solver_absorption_design.md).")
        _skirt_presolve = (ONE_SOLVE_TERRAIN
                           and ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT
                           and USE_PER_SURFACE_SOLVER
                           and layout.anchor is not None)
        _gap_presolve = (ONE_SOLVE_TERRAIN
                         and ONE_SOLVE_TERRAIN_GAP_FILL_SPINE
                         and USE_PER_SURFACE_SOLVER
                         and layout.anchor is not None)
        if _skirt_presolve:
            try:
                from .clearance import emit_runway_end_skirts as \
                    _emit_skirts_presolve
                _n_sk_pre = _emit_skirts_presolve(
                    layout, dem, tile_lat, tile_lon,
                    source_runways=apt.runways)
                if _n_sk_pre:
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: PRE-SOLVE emitted "
                        f"{_n_sk_pre} runway-end skirt polygon(s) "
                        f"(one-solve terrain absorption, stage B1).")
                    # Cross-tile skirts are sliced like every other shape;
                    # airside pavement was already cut pre-solve, so only the
                    # new skirt pieces are cut (no-op single-tile).
                    from .geom_guard import _AIRSIDE_ROLES as _skpre_skip
                    from .tile_cut import cut_layout_at_tile_boundaries as \
                        _skpre_tile_cut
                    _skpre_tile_cut(
                        layout,
                        current_tile_lat=current_tile_lat,
                        current_tile_lon=current_tile_lon,
                        dem=dem,
                        skip_roles=_skpre_skip,
                    )
            except _GEOM_EXC as _skpre_exc:
                UI.vprint(1, f"  [pav-builder] {icao}: pre-solve runway-end "
                             f"skirt emission FAILED: {_skpre_exc!r}")

        # ── Gap-fill spine PRE-SOLVE construction (Slice B stage B2,
        # gate O4_ONE_SOLVE_TERRAIN + O4_ONE_SOLVE_TERRAIN_GAP_FILL_
        # SPINE, both default OFF; ratified mechanism 2026-07-10,
        # docs/slice_b_solver_absorption_design.md §B2) ────────────────
        # The gap-fill drainage spines are the FIRST FREE terrain
        # variables in the one-solve graph.  Their GEOMETRY is built
        # here — after the pre-solve skirts (gap parents) and before
        # ``per_surface_solve`` — and staged on
        # ``layout.gap_fill_presolve``; the solver admits every spine
        # vertex (``_build_node_list``), constrains it with envelope
        # INTERVAL edges to its frozen-nearest pavement stations plus a
        # second-difference fairing, and writes the solved values back
        # into the store.  Face EMISSION stays at the post-solve slot
        # (blocker subjects only exist there); the emitter swaps its
        # retired analytic valuation for the store's solved values.
        # A construction failure degrades loudly to the analytic path
        # (the emitter warns per unmatched gap).
        if _gap_presolve:
            try:
                from .gap_fill import construct_gap_fill_presolve
                construct_gap_fill_presolve(layout)
            except _GEOM_EXC as _gappre_exc:
                UI.vprint(1, f"  [pav-builder] {icao}: pre-solve gap-fill "
                             f"spine construction FAILED: {_gappre_exc!r}")

        # ── Adjacent-ground band PRE-SOLVE construction (Slice B stage B3
        # ORDER 1, gate O4_ONE_SOLVE_TERRAIN +
        # O4_ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT, both default OFF;
        # docs/slice_b_solver_absorption_design.md §B3) ──────────────────
        # The band FOOTPRINT march moves here — before ``per_surface_solve``,
        # after the pre-solve skirts (their rings enter the terrain-facing
        # probe union) — from a DEM-seeded pavement-edge estimate, and the
        # raw band rings are staged on ``layout.adjacent_ground_presolve``.
        # The post-solve emitter CONSUMES those frozen footprints instead of
        # re-marching, but still clips them against the (post-solve) static
        # block and VALUES every vertex off the solved altitudes — so this is
        # a construction move only (values stay analytic; gate-ON is
        # value-equivalent to gate-OFF up to the enumerated seed/late-feature
        # footprint deltas).  It admits NO band vertex to the solver (that is
        # order 2 under the separate admission gate).  Requires the
        # adjacent-ground law itself (the emitter runs only under it).
        from .config import (
            ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT,
            ADJACENT_GROUND_LAW_ENABLED as _AGL_ENABLED)
        _band_construct = (ONE_SOLVE_TERRAIN
                           and ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT
                           and _AGL_ENABLED
                           and USE_PER_SURFACE_SOLVER
                           and layout.anchor is not None)
        if _band_construct:
            try:
                from .adjacent_ground import \
                    construct_adjacent_ground_presolve
                construct_adjacent_ground_presolve(
                    layout, dem, tile_lat, tile_lon,
                    source_runways=apt.runways)
            except _GEOM_EXC as _agpre_exc:
                UI.vprint(1, f"  [pav-builder] {icao}: pre-solve "
                             f"adjacent-ground band construction FAILED: "
                             f"{_agpre_exc!r}")

        if USE_PER_SURFACE_SOLVER and layout.anchor is not None:
            # Runway CIFP thresholds are LOCKED — the solver never moves them.
            # The old runway-threshold-relief passes (step 3
            # ``relieve_grade_via_runway_thresholds`` and step 5
            # ``relieve_grade_via_inter_runway_split``), which adjusted
            # thresholds and re-solved, were removed (user 2026-06-06): CIFP
            # thresholds are the only surveyed-correct elevation we have, so a
            # taxi route that can't reach a runway within grade is a
            # pavement-side gap to close, never a reason to move a known-good
            # threshold.  The solver therefore runs exactly once.
            per_surface_solve(layout, icao,
                               dem=dem,
                               tile_lat=tile_lat, tile_lon=tile_lon)
            # (Legacy junction ring-curvature smoothing removed: it was a no-op
            # under the single-grade-graph connecting solve, which produces a
            # smooth in-grade junction surface directly.)

        if n_tile_delta != 0:
            UI.vprint(1,
                f"  [pav-builder] {icao}: tile-boundary cut "
                f"adjusted shape count by {n_tile_delta:+d}.")

        # (refactor Phase 2) The two shape-drop passes
        # (_drop_floating_orphan_junctions, _drop_off_source_residue) moved
        # PRE-solve — see above, just before the geometry-guard snapshot.

        # Final within-shape grade WARN reflects the absolute
        # final state — junction / apron / terminal Euclidean caps
        # post-final-solver.  Per user 2026-05-03 the WARN was
        # previously firing mid-pipeline with stale numbers.
        from .config import REPORT_GRADE_AUDIT
        if REPORT_GRADE_AUDIT:
            from .elevation import _report_within_shape_violations
            _report_within_shape_violations(layout, icao)

        _progress.step()  # [6] Emitting terrain features & finalizing

        # Feature B (O4_OBJECT_BRIDGE_TERRAIN, docs/object_terrain_features_
        # spec.md): classify the airport pack's bridge/tunnel objects and
        # cache the result on the layout for the bridge emitters below.
        # No-op (nothing read, nothing attached) with the gate off.
        try:
            from . import object_terrain_assembly
            object_terrain_assembly.attach_bridge_classification(
                layout, xplane_root)
        except Exception as _object_bridge_error:  # never fail the build
            UI.vprint(1, "   [object-bridge] classification skipped:",
                      _object_bridge_error)

        # ── Terrain-transition feature emit (POST-solve) ──────────────
        # Boundary ribbon, boundary→DEM bridge and taxi/road bridges emit
        # HERE, after the single solve, so each mirrors the FINAL pavement
        # profile (the runway-distance clamp anchors to settled apron/taxi
        # altitudes — see refactor Phase 5).  None are airside pavement
        # roles, so the solver never touched them.
        _progress.substep(0.02, "Emitting terrain transition features")
        finalize.emit_terrain_transition_features(
            layout, icao, xplane_root,
            tile_dem=tile_dem,
            current_tile_lat=current_tile_lat,
            current_tile_lon=current_tile_lon)
        try:
            # Bridge vertex post-processing (POST-solve, with the bridge): the
            # boundary→DEM bridge is solve-dependent (Phase 5), so its airside
            # touches run here.  Collapse the bridge's ~1 m runway-clearance
            # arc onto shared runway/junction corners, and insert mid-edge
            # bridge contacts into junction rings so they share the node
            # (collinear → altitude-neutral, no grade change).
            from .boundary import (
                _snap_bridge_vertices_to_runway_corners as _snap_br,
                _insert_bridge_contacts_into_junctions as _ins_br)
            _snap_br(layout)
            _ins_br(layout)
        except _GEOM_EXC:
            pass
        # The boundary ribbon / bridges just emitted may cross an integer
        # tile line on cross-tile airports — slice them like every other
        # shape (pavement was already cut pre-solve, so only the new
        # feature pieces are cut; no-op for single-tile airports).
        # Under the one-profile solver, airside is the solve's sole elevation
        # truth (graded against the seam DEM anchors), so FREEZE it here — the
        # gapped airside edge grazes the cut buffer and would otherwise get a
        # spurious altitude re-sample (the SPLP seam clobber).
        from .geom_guard import _AIRSIDE_ROLES as _PS_AIRSIDE
        _ps_skip = _PS_AIRSIDE
        cut_layout_at_tile_boundaries(
            layout,
            current_tile_lat=current_tile_lat,
            current_tile_lon=current_tile_lon,
            dem=dem,
            skip_roles=_ps_skip,
        )

        # Wingtip / RESA terrain-clearance cuts (user 2026-05-22).  Cut
        # terrain that rises into a surface's lateral clearance band
        # (taxiway TOFA / runway graded strip) or runway-end safety area;
        # terrain at or below the surface is left alone (cut-only).
        #
        # Runs LAST, AFTER every elevation pass (user 2026-05-23): the
        # solver, runway redistribution, tile_cut resampling and the final
        # per-surface solve all change taxiway/runway profiles, and a
        # lateral clearance strip must MIRROR the FINAL profile of the
        # surface it shadows.  Emitting earlier sampled a stale profile and
        # left the cut floating below the settled surface (CYXY taxiway E:
        # clearance pinned ~6 m under the post-solve taxiway → a trench).
        # Because clearance is emitted here, no later solver touches it —
        # its node_altitudes are final by construction.
        # LEGACY-CHAIN RETIREMENT GATE (user in-sim review 2026-07-09):
        # the adjacent-ground bands + runway-end skirts supersede the
        # legacy surface_clearance strips, whose 5 m stationing (plus
        # every neighbour vertex the final weld inserts) is now the
        # dominant patch-density cost (CYXY shape 261).  Default ON
        # until the cross-airport coverage verification signs off;
        # O4_LEGACY_SURFACE_CLEARANCE=0 builds without the chain.  The ONE
        # B4 review switch (config.B4_FLIP_DEFAULTS) flips this default OFF
        # under the flip bundle; the explicit env var always wins.  Applied
        # as a post-assignment override so the gate keeps a plain "1"
        # literal (see config for the provenance-accuracy rationale).
        from .config import B4_FLIP_DEFAULTS as _b4_flip
        _legacy_clearance = os.environ.get(
            "O4_LEGACY_SURFACE_CLEARANCE", "1") == "1"
        if _b4_flip and "O4_LEGACY_SURFACE_CLEARANCE" not in os.environ:
            _legacy_clearance = False
        try:
            from .clearance import emit_surface_clearance_cuts
            _cl_tl = (current_tile_lat if current_tile_lat is not None
                      else math.floor(layout.anchor[0]))
            _cl_tn = (current_tile_lon if current_tile_lon is not None
                      else math.floor(layout.anchor[1]))
            n_cl = 0
            if _legacy_clearance:
                # Fractions are measured shares of the emit phase
                # (CYXY 2026-07-10 trace): the clearance + conformance
                # block below runs ~95% of the phase, so the later
                # emitters cluster near the end.
                _progress.substep(0.05, "Emitting clearance cuts")
                n_cl = emit_surface_clearance_cuts(
                    layout, dem, _cl_tl, _cl_tn,
                    source_runways=apt.runways)
            if n_cl:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: emitted {n_cl} "
                    f"surface-clearance cut polygon(s).")
                # A clearance strip alongside a seam-parallel surface, or a
                # RESA thrown up to 300 m off a runway end near a seam, can
                # cross an integer tile line — slice it like every other
                # shape.  Pavement was already cut above (10 m gap), so it
                # no longer crosses; only the new clearance pieces are cut,
                # and neighbour-tile pieces dropped.  No-op for single-tile
                # airports (no integer line in the footprint).
                cut_layout_at_tile_boundaries(
                    layout,
                    current_tile_lat=current_tile_lat,
                    current_tile_lon=current_tile_lon,
                    dem=dem,
                    skip_roles=_ps_skip,
                )
        except _GEOM_EXC:
            pass

    # ── Boundary-interior clip: RETIRED (user 2026-07-16) ─────────────
    # The "no shape may straddle the row-130 boundary" invariant (user
    # 2026-05-22) existed for the boundary ribbon, which the
    # adjacent-ground law superseded — see the matching note at the
    # (also retired) PRE-solve clip site.  Post-solve features
    # (clearance / tunnel ramps / groundside) now keep their geometry
    # across the row-130 line; EGPB's straddling tunnel-ramp rects were
    # being flattened into a hump by this clip's altitude resample.
    from .boundary import _conform_ribbon_to_pavement_seam

    # ── Boundary-conformance invariant (user 2026-05-22) ──────────────
    # RUNTIME requirement for EVERY airport: the emitted shapes must be a
    # CONFORMING partition — adjacent shapes share identical vertices
    # along common edges, no edge crossings.  Non-conformance (T-junctions
    # / crossings) makes Triangle4XP node the arrangement into degenerate
    # sub-cm² slivers (HECA: 119k→2.36M airport triangles, 9m40s load).
    # "No area overlap" (test_no_self_overlap) is necessary but blind to
    # zero-area T-junctions, which is how this slipped through.  Enforce
    # here (last geometry step), then assert the invariant holds.  The
    # boundary ribbon now participates (it tiles with pavement); only the
    # DEM bridge stays exempt (see conformance._OVERLAY_REFS).
    # ── Airside frozen; conform the post-solve FEATURES to it ─────────
    # (refactor Phases 6+7) The airside node-unification (re-connect
    # discovered lane dead-ends + weld + full conformance + near-corner /
    # neighbour-corner snaps) ran PRE-solve, so the airside partition is
    # already conforming and is FROZEN here.  Conform only the post-solve-
    # emitted FEATURES (boundary ribbon, groundside, tunnel ramps / walls) TO
    # that frozen airside — a ONE-SIDED pass (owner_roles=features) that
    # inserts vertices ONLY into feature edges, NEVER moving an airside
    # vertex (so the pre-solve-graded airside altitudes stay intact).  When
    # the pre-solve unification did NOT run (non-per-surface / no-elevation
    # path) fall back to the full both-sided unification here.
    # The bridge vertex post-processing and boundary-interior clip above
    # MOVE feature vertices after the emit-time deconflict — re-run it on
    # the settled geometry so road features stay single-cover; the
    # feature conformance below then heals the clipped seams.
    finalize.deconflict_road_features(layout, icao)

    from .conformance import (
        enforce_conformance, find_conformance_violations)
    if _airside_unified_presolve:
        n_shapes, n_verts = enforce_conformance(
            layout, owner_roles=set(_POSTSOLVE_FEATURE_OWNER_ROLES))
        if n_verts:
            UI.vprint(1,
                f"  [pav-builder] {icao}: post-solve feature conformance — "
                f"inserted {n_verts} vertex(es) into {n_shapes} feature "
                f"shape(s) (airside frozen).")
    else:
        _unify_airside_geometry(layout, icao)

    # Ribbon YIELDS its elevation to abutting pavement at every shared
    # seam node (incl. the ones conformance just inserted), so there is
    # no vertical wall between pavement and the ribbon.  Altitude-only —
    # does not change geometry, so the conformance invariant still holds.
    n_seam = _conform_ribbon_to_pavement_seam(layout)
    if n_seam:
        UI.vprint(1,
            f"  [pav-builder] {icao}: ribbon seam — adopted pavement "
            f"altitude on {n_seam} ribbon rect(s).")

    # Flatten torn vertical slivers where a DEM-bridge ribbon necks to
    # near-zero width against the perimeter strip — a bridge inner vertex
    # (pavement altitude) ends up ~1 m from a perimeter-strip vertex
    # (clamped altitude) at a several-metre altitude gap, which X-Plane
    # tears (see ``_flatten_bridge_pinch_necks``).  Runs LAST so it catches
    # the pinch vertices conformance / contact-insertion grafted onto the
    # bridge rings.  Altitude-only — conformance invariant preserved.
    from .boundary import _flatten_bridge_pinch_necks
    n_pinch = _flatten_bridge_pinch_necks(layout, icao=icao)
    if n_pinch:
        UI.vprint(1,
            f"  [pav-builder] {icao}: flattened {n_pinch} DEM-bridge "
            f"pinch-neck vertex(es) (anti-tear).")

    # Re-clip DEM bridges against the FINAL pavement + ribbon geometry.
    # The emit-time trim used the emit-time snapshot; the boundary-
    # interior clip and feature conformance above can reshape the
    # ribbon and pavement edges and leave a stale overlap (LMML: ribbon
    # pieces × 37.8 m² over a DEM bridge).  The DEM bridge is
    # conformance-exempt, so this never reintroduces a T-junction.
    from .boundary import _clip_boundary_bridges_against_pavement
    n_bclip = _clip_boundary_bridges_against_pavement(layout)
    if n_bclip:
        UI.vprint(1,
            f"  [pav-builder] {icao}: re-clipped {n_bclip} DEM-bridge "
            f"shape(s) against final pavement / ribbon.")

    # Strip zero-length edges (duplicate ring vertices) the geometry passes
    # left behind — they triangulate into degenerate slivers (stretched
    # textures).  Geometry-neutral, so it runs after conformance enforcement
    # and before the final conformance audit.
    _dedup_coincident_ring_vertices(layout, icao)

    # (Legacy post-solve cap-debulge removed: it ran only under the retired
    # O4_SINGLE_GRADE_GRAPH=0 path; the single-grade-graph connecting solve grades
    # the cap in-grade, so the post-solve band-aid is superseded.)

    # Enforce the rect flat-end rule (user 2026-06-19): only a sloping rect's
    # 2 CORNERS are legal shared vertices on its flat end.  The spine slice +
    # the weld/conformance chain can leave an apron/junction vertex mid-flat-
    # edge (SPJC SVC13 ran corner→MID→corner along SVC13's flat end) — an
    # illegal shared vertex that triangulates to a Triangle4XP T-junction tear.
    # Drop any such node so the edge straightens corner-to-corner.  LATE pass
    # (final geometry + altitudes), before the conformance audit below.
    from .flatedge_snap import drop_flatedge_nodes
    drop_flatedge_nodes(layout)

    # FINAL planarization (user 2026-06-30, gate O4_PLANARIZE_AIRSIDE): drive the
    # conformance invariant to 0 as the LAST geometry step — resolve edge
    # CROSSINGS (insert the intersection point on both edges) + collinear
    # T-junctions, both shape-preserving and altitude-interpolating so it is
    # safe post-solve.  Runs here, after EVERY geometry pass, so nothing can
    # re-introduce a crossing afterward (the pre-solve crossings returned because
    # later passes re-cut edges).
    if os.environ.get("O4_PLANARIZE_AIRSIDE", "1") == "1":
        from .conformance import planarize_airside
        planarize_airside(layout, icao=icao)

    # SERVICE lens deconfliction (user 2026-07-04): the canonical vertex
    # weld can cross two near-coincident service boundaries whose contact
    # chains carry different vertex sequences (corridor-converted road vs
    # the strip-carved junction it was trimmed against — 0.38 m² lens).
    # Runs BEFORE the final T-vertex weld so the clip's new on-edge
    # vertices get welded (running after left a residual T-junction).
    if compute_elevations:
        from .groundside import _deconflict_service_overlaps
        _n_svc_ov = _deconflict_service_overlaps(layout)
        if _n_svc_ov:
            UI.vprint(1,
                f"  [pav-builder] {icao}: deconflicted {_n_svc_ov} "
                f"overlapping service shape(s) (lens clip).")

    # LAST-WORD bridge re-clip: drop_flatedge_nodes / planarize above can
    # STRAIGHTEN a pavement edge that the emit-time bridge clip followed,
    # re-creating a pavement∩bridge overlap (CYXY apron#25: 6.7 m²).  Run
    # the re-clip again on the final geometry (idempotent).
    n_bclip2 = _clip_boundary_bridges_against_pavement(layout)
    if n_bclip2:
        UI.vprint(1,
            f"  [pav-builder] {icao}: final DEM-bridge re-clip — "
            f"{n_bclip2} shape(s).")

    # FINAL T-vertex weld (user 2026-07-02): a node lying ON another
    # shape's edge interior without a weld tears Triangle4XP's
    # triangulation (stretched textures).  Two classes escape the passes
    # above: (a) a FEATURE vertex on an AIRSIDE edge — the one-sided
    # feature conformance only inserts into feature edges, and
    # planarize_airside covers airside↔airside only; (b) DEM-bridge /
    # clearance overlay vertices — conformance-exempt on a "built-in
    # gap" premise that does not always hold (CYXY: bridge vertices
    # exactly on apron/boundary edges).  Insert-only at interpolated
    # altitudes (surface-neutral), so it is safe post-solve.  TIGHT
    # tolerance: only truly-ON-edge nodes (the tearing class sits at
    # 0.000-0.003 m); the full 0.5 m weld tolerance would bow an edge
    # outward by up to the tolerance and mint hairline overlaps
    # (zero-tolerance test_no_self_overlap).
    _n_ws, _n_wv = enforce_conformance(layout, tol=0.01,
                                       include_overlay_refs=True)
    if _n_wv:
        UI.vprint(1,
            f"  [pav-builder] {icao}: final T-vertex weld — inserted "
            f"{_n_wv} vertex(es) into {_n_ws} shape(s).")

    # LAST groundside↔airside separation (user 2026-07-04): the strip
    # carve + the late runway-disconnection sweeps demote shapes AFTER
    # the earlier separation runs, and a demoted lot's DEM-follow
    # rebuild can leave a hairline overlap with the service road it
    # abuts (CYXY: 0.6 m² lot∩road — zero-tolerance
    # test_no_self_overlap).  The pass is idempotent and keeps touching
    # service edges (share-svc), so a clean layout is unchanged.
    if compute_elevations:
        try:
            from .groundside import _separate_groundside_from_airside
            _dem_last = tile_dem if tile_dem is not None else None
            if _dem_last is None:
                from .elevation import _load_airport_dem as _lad_last
                _dem_last = _lad_last(layout.anchor[0], layout.anchor[1])
            _tl = (current_tile_lat if current_tile_lat is not None
                   else int(math.floor(layout.anchor[0])))
            _tn = (current_tile_lon if current_tile_lon is not None
                   else int(math.floor(layout.anchor[1])))
            _separate_groundside_from_airside(layout, _dem_last, _tl, _tn,
                                              preserve_field=True)
            # The separation re-derives DEM altitudes for any piece it
            # clipped — AFTER the finalize-stage chord limiter ran, so a
            # rebuilt hillside piece reads >4 % across its interior again
            # (CYXY #207: 8 % over 7.8 m).  Re-limit (idempotent).
            from .groundside import _grade_limit_groundside_chords
            _grade_limit_groundside_chords(layout)
        except _GEOM_EXC:
            pass

    tjs, crossings = find_conformance_violations(layout.shapes)
    if tjs or crossings:
        UI.vprint(1,
            f"  [pav-builder] WARN: {icao}: conformance invariant NOT "
            f"met — {len(tjs)} residual T-junction(s), {len(crossings)} "
            f"edge crossing(s) (→ Triangle4XP mesh slivers).")

    # Pre-solve geometry guard (dev): report how many airside shapes had
    # their geometry changed by a post-solve pass (target = 0).  Runs BEFORE
    # decimation so the report keeps measuring the unintended reshapes, not
    # the deliberate emit thinning.
    from .geom_guard import report_post_solve_changes
    report_post_solve_changes(layout, _geom_guard_snap, icao)

    # EMIT DECIMATION (user design 2026-07-03): drop 3D-collinear ring
    # vertices — node density follows the SOLVED profile (straights emit as
    # single segments; vertical transitions and curves keep their nodes).
    # Must see the final welded/conformant geometry, and a vertex may only
    # vanish when every ring sharing it agrees (no T-vertices minted).
    # BEFORE the final grade projection (user 2026-07-05): decimating a
    # junction ring re-triangulates its interior, so the decimated ring's
    # MESH has chords the pre-decimation law never contained — running the
    # projection on the decimated rings makes it the true last word on
    # values for the geometry X-Plane actually renders (SPJC 18-pair
    # 1.5-1.8 % junction class).  Gate O4_EMIT_DECIMATE.
    if compute_elevations:
        from .emit_decimate import (decimate_emit_nodes,
                                    normalize_runway_altitudes,
                                    repair_sliver_corners)
        # ONE runway representation (user 2026-07-06): any hi/lo
        # canonical rect still alive (incl. pieces minted by post-solve
        # splits) becomes per-vertex node_altitudes before the geometry
        # passes and the final projection.
        normalize_runway_altitudes(layout, icao)
        # Sliver-needle repair BEFORE decimation + the final projection
        # (user 2026-07-06): the emit-time repair removed needle vertices
        # AFTER the last law projection, merging two enforced ring edges
        # into one nobody enforced (SPJC apron 77 m blend pair).  The
        # emit-time scan remains as the backstop for quantization-born
        # needles.
        repair_sliver_corners(layout, icao)
        # LATE EDGE DENSIFY (user in-sim finding 2026-07-09): shapes
        # reshaped post-solve (junction merges / slice re-cuts) can be
        # born with over-long edges the pre-solve densify never saw
        # (CYXY: a 1,057 m junction chord).  Inserted here — BEFORE
        # decimation and the final grade projection (the ordering law)
        # — the lerped vertices are law-projected onto the mesh the
        # sim renders, and the 60 m spacing survives the decimators'
        # MAX_CHORD.
        from .conformance import densify_long_edges as _dle
        from .clearance import _AIRSIDE_PAVEMENT_ROLES as _dle_roles
        _n_dense2 = _dle(layout, _dle_roles, 60.0)
        if _n_dense2:
            UI.vprint(1,
                f"  [pav-builder] {icao}: late edge densify — inserted "
                f"{_n_dense2} vertex(es) on over-60 m pavement edges.")
        _progress.substep(0.90, "Decimating emitted geometry")
        decimate_emit_nodes(layout, icao)

    # FINAL GRADE PROJECTION (round 4, user 2026-07-03): the passes above
    # (planarize, welds, clips, merges, emit decimation) reshaped rings
    # AFTER the elevation solve, so the law pairs of the FINAL rings differ
    # from what the solve projected.  One last scalar GS projection on the
    # final geometry (runway/seam/feature-weld nodes hard, pads movable-
    # flat) closes the post-solve mutation classes the validator otherwise
    # flags.
    if compute_elevations:
        from .elevation_per_surface.route_profile.solve import (
            final_grade_projection)
        # DEM + tile frame for the flatness-certificate tier (user
        # 2026-07-05) — the SAME dem/tile pairing rule the elevation solve
        # uses (current tile when the driver provided tile_dem, else the
        # anchor tile that _load_airport_dem covers), so certificate seeds
        # are bit-identical to the solve's node seeds.  Failure to load a
        # DEM just disables certification (eager generation).
        _projection_dem = None
        _projection_tile_lat = _projection_tile_lon = 0
        if layout.anchor is not None:
            try:
                from .elevation import _load_airport_dem as _projection_load
                _projection_dem = (tile_dem if tile_dem is not None
                                   else _projection_load(layout.anchor[0],
                                                         layout.anchor[1]))
                if tile_dem is not None and current_tile_lat is not None:
                    _projection_tile_lat = current_tile_lat
                    _projection_tile_lon = current_tile_lon
                else:
                    _projection_tile_lat = int(math.floor(layout.anchor[0]))
                    _projection_tile_lon = int(math.floor(layout.anchor[1]))
            except _GEOM_EXC:
                _projection_dem = None
        # T1b (board): OWNER RULING 2026-07-18 late — default stays ON.
        # The in-sim comparison showed no visual difference either way,
        # and the owner chose the historic double projection over the
        # measured wall saving (mid-off quiet A/B: OTHH 299.2 s vs
        # 363.3, CYXY 39.8 s (−22), SPJC 87.7 s (−13); real law classes
        # identical; by-design break-region pairs grow, CYXY 239→400).
        # O4_FINAL_PROJECTION_MID=0 re-enables the experiment; the
        # conformance passes below then defer past the LATE projection
        # via the reorder machinery, which stays in place.
        _mid_projection_on = (
            os.environ.get("O4_FINAL_PROJECTION_MID", "1") == "1")
        if _mid_projection_on:
            final_grade_projection(layout, icao, dem=_projection_dem,
                                   tile_lat=_projection_tile_lat,
                                   tile_lon=_projection_tile_lon)

        def _post_projection_conformance_passes():
            # PAD-IN-SOLVED-PAVEMENT HOST LEVEL (user 2026-07-10, round 6
            # site 3): a building pad embedded in / abutting SOLVED pavement
            # must sit FLAT at the level the HOST pavement solved to at the
            # contact, not at its raw-DEM frontage seat.  The initial solve
            # already lifts a movable-flat pad to its host, but
            # ``final_grade_projection`` re-runs ``build_building_seats``
            # (DEM-biased) and re-stamps the pit value (CYXY building8 →
            # 705.0 while apron #129 solved 708.65; a -333 %/1.1 m step,
            # "a big hump in this apron").  Runs AFTER the projection so it
            # reads the FINAL host solution and nothing re-seats the pad
            # afterwards; it also lifts the shared apron lip so pad and
            # host weld at one flat level (no emit cliff).  Gate off →
            # no-op / byte-identical.
            from .elevation_per_surface.route_profile.anchors import (
                relevel_pads_to_host_pavement)
            _n_padhost = relevel_pads_to_host_pavement(layout)
            if _n_padhost:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: pad-host level — {_n_padhost} "
                    f"embedded pad(s) re-levelled to the host pavement "
                    f"solution.")
            # GROUNDSIDE RE-LIMIT after the projection (user 2026-07-06,
            # CYXY #184): groundside lots are NOT in the projection's
            # constraint roles, so enforcing a ROAD edge can nudge a welded
            # mouth a few cm past the lot ring's 4 % Lipschitz field
            # (measured: the limiter's lawful 699.80 pushed to 699.84 → a
            # 4.64 % lot pair).  The chord limiter is idempotent and its
            # weld re-adoption keeps road and lot emitting one value —
            # re-running it here re-levels the ring around the projected
            # weld; the cm-scale road-side drift it re-introduces sits
            # inside the validator's rounding envelope, unlike the
            # lot-side tear it removes.
            # RIBBON/BRIDGE RE-ADOPTION after the projection (user
            # 2026-07-06, CYXY apron #29): the seam cascade ran before
            # this projection — a pavement vertex the projection then
            # moved leaves the ribbon/bridge holding the STALE adopted
            # value, and the emit consensus drags the welded node back
            # over the pavement law (693.00 lawful → 692.85 emitted,
            # 1.25 % on a 1 % apron).  The cascade is altitude-only and
            # idempotent — re-running it re-adopts the projected values.
            try:
                from .boundary import _conform_ribbon_to_pavement_seam
                _conform_ribbon_to_pavement_seam(layout)
            except _GEOM_EXC:
                pass
            try:
                from .groundside import _grade_limit_groundside_chords
                layout._weld_relimit_moved_xy = []
                _grade_limit_groundside_chords(layout)
                # Quarantine welds the re-adoption MOVED: the projection
                # placed the road there, the lot law pulled it back — the
                # residual tension has no lawful joint value (see the
                # limiter's moved-weld note).  Their over-cap pairs report
                # as break-region blends, not actionable misses.
                _moved = getattr(layout, "_weld_relimit_moved_xy", None) \
                    or []
                if _moved:
                    _existing_ll = list(
                        getattr(layout, "_break_node_ll", None) or [])
                    _seen_ll = {(round(la, 7), round(lo, 7))
                                for (la, lo) in _existing_ll}
                    for (_wx, _wy) in _moved:
                        _la, _lo = layout.m_to_ll(_wx, _wy)
                        if (round(_la, 7), round(_lo, 7)) not in _seen_ll:
                            _existing_ll.append((_la, _lo))
                    layout._break_node_ll = _existing_ll
            except _GEOM_EXC:
                pass

        # T1b reorder (board): with the mid projection ON these passes run
        # here, reading mid-projected values (historic behavior).  With it
        # OFF they are DEFERRED until after the LATE projection — running
        # them here would seat pads / re-adopt ribbon values against
        # unprojected solver values, which the late projection then moves
        # again (measured OTHH: break-region pairs 42 → 61 without the
        # reorder).
        if _mid_projection_on:
            _post_projection_conformance_passes()

        # SPINE CROWN v2 (user ruling 2026-07-07, part 30): the crown is
        # built INSIDE the solve now — runway rings (uniform per-ref
        # profile − rate·half_width) and taxi/service corridor edges all
        # crown through the crown drop field applied at the solve's
        # writeback (crown.build_crown_drop_field), and the spine
        # breaklines are staged from the SOLVED route profiles
        # (crown.emit_crown_spines).  The v1 post-solve edge-drop pass
        # that lived here (freeze sets / vetoes / revoke valve) is gone.

        # Runway-end down-slope SKIRTS (Pass D, gate O4_RUNWAY_END_SKIRT):
        # the ABSOLUTE LAST emission — after decimation and the final
        # grade projection (which, since 8ca25a3, runs after decimation
        # and is the last word on values) — because the skirt bakes the
        # law floor from edge-interpolated pavement reads (end elevation
        # + entry grade) and any earlier placement reads rings/values
        # that later passes rewrite.  Emitting here, the emitter reads
        # exactly the geometry that renders — the same reads the
        # verification checker makes.  Skirts are freshly minted with a
        # pavement gap (no shared vertices), so arriving after
        # decimation mints no T-vertices.
        try:
            from .clearance import emit_runway_end_skirts
            _progress.substep(0.92, "Emitting runway-end skirts")
            # Stage B1: when the one-solve terrain gate built the skirts
            # PRE-SOLVE (above, as pinned solver-graph members), the legacy
            # post-solve emitter must NOT run again — the shapes already
            # exist.  The bridge↔skirt reconciliation below still runs so
            # boundary→DEM bridges are trimmed to the (now pre-solve) skirt
            # footprint regardless of which path emitted them.
            n_sk = 0 if _skirt_presolve else emit_runway_end_skirts(
                layout, _projection_dem,
                _projection_tile_lat, _projection_tile_lon,
                source_runways=apt.runways)
            if _skirt_presolve:
                # Reconcile pre-solve skirts with the post-solve boundary
                # bridges (no-op where none overlap, e.g. CYXY).
                try:
                    from .boundary import \
                        _reconcile_boundary_bridges_with_skirts as _rbbs_pre
                    _rbbs_pre(layout)
                except _GEOM_EXC:
                    pass
            if n_sk:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: emitted {n_sk} "
                    f"runway-end skirt polygon(s).")
                # A skirt reaches up to ~305 m off a runway end and can
                # cross an integer tile line — slice it like every other
                # post-solve feature (no-op single-tile).
                from .geom_guard import _AIRSIDE_ROLES as _skirt_skip
                from .tile_cut import cut_layout_at_tile_boundaries as \
                    _skirt_tile_cut
                _skirt_tile_cut(
                    layout,
                    current_tile_lat=current_tile_lat,
                    current_tile_lon=current_tile_lon,
                    dem=_projection_dem,
                    skip_roles=_skirt_skip,
                )
                # BRIDGE ↔ SKIRT reconciliation (user 2026-07-07): the
                # boundary→DEM bridge emitted in the feature phase
                # anchors to RAW DEM at a runway end and cannot match
                # the skirt/RESA surface emitted here (KCLT 18R: a ~10 m
                # bridge-vs-skirt step).  The skirt MUST stay last (it
                # bakes the floor from settled pavement), so instead of
                # reordering emission we trim the bridges to the
                # just-emitted skirt/RESA footprint — the skirt owns the
                # terrain transition inside its governed zone; the
                # bridge descends to the DEM only OUTSIDE it.
                from .boundary import \
                    _reconcile_boundary_bridges_with_skirts
                n_bsk = _reconcile_boundary_bridges_with_skirts(layout)
                if n_bsk:
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: reconciled {n_bsk} "
                        f"boundary→DEM bridge(s) with the skirt/RESA "
                        f"surface (matched at the runway-end zone).")
        except _GEOM_EXC as exc:
            UI.vprint(1, f"  [pav-builder] {icao}: runway-end skirt "
                         f"emission FAILED: {exc!r}")

        # (The object-bridge causeway plates moved to the PRE-solve
        # layout builder — user ruling R12, ``build_bridge_layout_
        # shapes``: first-class ROLE_BRIDGE_CAUSEWAY shapes born with
        # law values; no late emission remains for feature B.)

        # ── GAP-FILL + DRAINAGE SPINE (slice B pilot, user design
        # 2026-07-09; docs/chain_identity_one_solve_plan.md) ────────────
        # Ground ENCLOSED between pavements grades as ONE unit: boundary
        # = the pavement chains verbatim, interior = a drainage spine
        # splitting the gap into two faces sharing the spine chain.
        # ORDERING: after the skirts (their shapes bound end-adjacent
        # gaps) and BEFORE the adjacent-ground bands — the gap shapes
        # join the bands' static union, so the corridor march skips
        # gap-covered frontage and only true outer edges keep bands.
        try:
            from .gap_fill import emit_gap_fill_spines
            _progress.substep(0.94, "Emitting gap-fill drainage spines")
            n_gap = emit_gap_fill_spines(
                layout, _projection_dem,
                _projection_tile_lat, _projection_tile_lon,
                source_runways=apt.runways)
            if n_gap:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: emitted {n_gap} gap-fill "
                    f"spine face(s) (enclosed between-pavement ground).")
        except _GEOM_EXC as exc:
            UI.vprint(1, f"  [pav-builder] {icao}: gap-fill spine "
                         f"emission FAILED: {exc!r}")

        # ── ENCLOSED-POCKET INTERIOR DEPTH FLOOR (owner ruling
        # 2026-07-19) ───────────────────────────────────────────────────
        # Pockets the gap-fill emitter SKIPPED (over-width, foreign
        # shape, parent straddle) ride raw DEM; clamp their interiors to
        # (pavement lip − GAP_FILL_INTERIOR_FLOOR_DEPTH_M), emitting
        # flat pit-fill patches only where the DEM actually violates.
        # ORDERING: after the spine emission (treated gaps are covered
        # by their faces and skip by coverage) and before the
        # adjacent-ground bands (pit patches join the static union).
        try:
            from .gap_fill import emit_gap_interior_floor
            n_pit = emit_gap_interior_floor(
                layout, _projection_dem,
                _projection_tile_lat, _projection_tile_lon)
            if n_pit:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: emitted {n_pit} enclosed-"
                    f"pocket pit-floor patch(es).")
        except _GEOM_EXC as exc:
            UI.vprint(1, f"  [pav-builder] {icao}: pocket pit-floor "
                         f"emission FAILED: {exc!r}")

        # ── Adjacent-ground LATERAL grade law (slice 3, gate
        # O4_ADJACENT_GROUND_LAW, default OFF) ──────────────────────────
        # The lateral generalization of the runway-end skirt: graded
        # `graded_strip` bands off every terrain-facing airside pavement
        # edge, cut/filled to the lawful corridor
        # (grade_law.adjacent_ground_envelope).  ORDERING: MUST run AFTER
        # emit_runway_end_skirts + its tile_cut (the skirt shapes are in
        # the static block, so the bands clip against them at runway ends
        # and never double-write) and BEFORE the final epsilon-wedge weld
        # (its new constrained edges get welded like every other feature).
        # Imported inside the gate so the module has NO import side effect
        # when the law is off (byte-inert).
        from .config import ADJACENT_GROUND_LAW_ENABLED
        if ADJACENT_GROUND_LAW_ENABLED:
            try:
                from .adjacent_ground import emit_adjacent_ground_bands
                _progress.substep(0.96, "Emitting adjacent-ground bands")
                n_ag = emit_adjacent_ground_bands(
                    layout, _projection_dem,
                    _projection_tile_lat, _projection_tile_lon,
                    source_runways=apt.runways)
                if n_ag:
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: emitted {n_ag} "
                        f"adjacent-ground graded-strip/wall polygon(s).")
                    from .geom_guard import _AIRSIDE_ROLES as _ag_skip
                    from .tile_cut import cut_layout_at_tile_boundaries \
                        as _ag_tile_cut
                    _ag_tile_cut(
                        layout,
                        current_tile_lat=current_tile_lat,
                        current_tile_lon=current_tile_lon,
                        dem=_projection_dem,
                        skip_roles=_ag_skip,
                    )
            except _GEOM_EXC as exc:
                UI.vprint(1, f"  [pav-builder] {icao}: adjacent-ground "
                             f"band emission FAILED: {exc!r}")

        # Round 9 (user ruling): re-run the non-overlap rule AFTER the
        # adjacent-ground bands — the last feature emitters that can
        # lap onto the object-bridge plates.  Gate off ⇒ no plates ⇒
        # no-op.
        try:
            from .bridges import enforce_bridge_plate_exclusivity
            enforce_bridge_plate_exclusivity(layout)
        except _GEOM_EXC:
            pass

    # FINAL EPSILON-WEDGE WELD (part 30j): the T-vertex weld at
    # ``enforce_conformance(tol=0.01)`` above runs BEFORE the last three
    # geometry-mutating passes — ``_separate_groundside_from_airside``
    # (rebuilds groundside lot rings by re-sampling the DEM-follow
    # outline), ``decimate_emit_nodes`` (drops per-shape ring vertices
    # independently), and the runway-end skirts.  Those passes re-derive
    # a neighbour's outline with a DIFFERENT vertex set, so a groundside
    # lot edge that runs ALONG the boundary ribbon (or a junction edge
    # along a longer neighbour) ends up with a foreign vertex sitting ON
    # the edge WITHOUT a shared node — an EPSILON WEDGE: two constrained
    # edges share one node, run near-parallel (<0.01°), and diverge by
    # sub-millimetre.  Triangle4XP's Ruppert encroachment rule then
    # ping-pongs splits on that near-zero-area sliver down to machine
    # epsilon, exploding the tile (KJQF: the boundary↔groundside_pavement
    # seam alone drove ~2.0M triangles / 55 % of the tile).  Re-running
    # the tight T-vertex weld on the FINAL vertex sets inserts each such
    # on-edge vertex into the edge it lies on, so the two shapes share
    # the node and the sliver vanishes.  TIGHT tolerance (0.01 m): only
    # truly-on-edge nodes (the wedge class sits at 0.000-0.003 m perp);
    # a wider tolerance would bow an edge outward and mint hairline
    # overlaps.  Insert-only at interpolated altitudes (surface-neutral),
    # so it is safe as the last geometry touch.  Measured KJQF isolated
    # triangulation: 1,993,832 → 14,252 tris.
    if compute_elevations:
        from .conformance import (enforce_conformance as _enf_final,
                                  find_conformance_violations as _fcv)
        _n_ews, _n_ewv = _enf_final(layout, tol=0.01,
                                    include_overlay_refs=True)
        if _n_ewv:
            UI.vprint(1,
                f"  [pav-builder] {icao}: final epsilon-wedge weld — "
                f"inserted {_n_ewv} vertex(es) into {_n_ews} shape(s).")
        # RESIDUAL REPORT (chain identity, 2026-07-09): anything the
        # weld could NOT unify is a divergent chain the tile mesh will
        # Ruppert-refine — name each site so the build log localizes
        # the mint source instead of the bake discovering it.
        try:
            _res_tj, _res_x = _fcv(layout.shapes, tol=0.005)
            if _res_tj or _res_x:
                UI.vprint(1,
                    f"  [pav-builder] WARN {icao}: post-weld residual "
                    f"divergence — {len(_res_tj)} T-junction(s), "
                    f"{len(_res_x)} crossing(s):")
                for _x, _y in (_res_tj + _res_x)[:10]:
                    _la, _lo = layout.m_to_ll(_x, _y)
                    UI.vprint(1, f"      @ {_la:.7f},{_lo:.7f}")
        except _GEOM_EXC:
            pass
        # CROWN FIELD COMPLETION (B4 flip defect 2, 2026-07-15): the
        # adjacent-ground band emit + the epsilon-wedge weld above mint
        # pavement ring vertices AFTER ``final_grade_projection`` ran the
        # crown field extension, so those vertices read crown drop 0 while
        # a flanking solve-time vertex carries a real drop — the emitted
        # validators then measure the pair against the WRONG crown target
        # (CYXY junction: a lawful crown-ramp chord read as a 2.6 % spine
        # step).  Re-run the value-derived extension on the FINAL rings;
        # incremental — solve-time keys and already-extended keys are
        # never recomputed, values no longer move after this point.
        try:
            from .crown import extend_field_to_new_ring_nodes as _crown_ext
            _n_crown_ext = _crown_ext(layout, None)
            if _n_crown_ext:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: crown field completion — "
                    f"{_n_crown_ext} post-weld ring vertex(es) joined the "
                    f"crown drop field.")
        except _GEOM_EXC:
            pass

    # INTERIOR RUNWAY CROSS-EDGE CROWN (Phase 0 hotfix, user 2026-07-07;
    # docs/runway_single_polygon_plan.md): every interior segment cross-edge
    # of a crowned runway is a flat full-width mesh constraint at the dropped
    # (profile − rate·half_width) altitude, so the mesh dives from the
    # centerline ridge to the cross-edge and back at every segment line — a
    # visible centre DIP on every crowned runway.  Insert a centerline node at
    # the runway PROFILE altitude into BOTH abutting sub-rects (same canonical
    # point → the emit consensus welds them) so each cross-section reads as a
    # crown-matching tent.  ABSOLUTE LAST geometry touch (with the probe hook):
    # a mid-edge vertex on the crowned tent is the 3D-collinear class emit
    # decimation removes, so it must arrive after decimation / final
    # projection / skirts.  No-op when crown gated off or runways de-scoped.
    if compute_elevations:
        try:
            from .crown import insert_runway_crossedge_crown_nodes
            _n_xedge = insert_runway_crossedge_crown_nodes(layout)
            if _n_xedge:
                UI.vprint(1, f"  [pav-builder] {icao}: crowned {_n_xedge} "
                             f"interior runway cross-edge(s) (centerline "
                             f"node at profile level).")
        except _GEOM_EXC as exc:
            UI.vprint(1, f"  [pav-builder] {icao}: interior runway "
                         f"cross-edge crown FAILED: {exc!r}")

    # Diagnostic probe nodes (user 2026-07-07): O4_PROBE_NODES inserts
    # elevation-neutral ring vertices near given lat,lon points so
    # node-free straightaways carry inspectable altitudes in the patch.
    # ABSOLUTE LAST geometry touch: a lerped on-edge vertex is exactly
    # what emit decimation removes, so it must come after everything.
    _probe_spec = os.environ.get("O4_PROBE_NODES")
    if _probe_spec:
        from .geom_guard import insert_probe_nodes
        insert_probe_nodes(layout, _probe_spec)

    # ABSOLUTE-LAST EDGE DENSIFY (user in-sim finding 2026-07-09): a
    # post-solve pass after the mid-pipeline densifies still mints
    # over-long pavement chords (CYXY: 1,057 m on junction #101 —
    # minted between the emit-stage densify and here).  Run once more
    # as the LAST geometry touch: lerped vertices on a straight solved
    # edge are surface-neutral, both emit decimators are MAX_CHORD-
    # capped so nothing downstream re-thins, and the mesh gets a node
    # every <= 60 m to hold the pavement edge (the user's ruling).
    if compute_elevations:
        try:
            from .conformance import densify_long_edges as _dle3
            from .clearance import _AIRSIDE_PAVEMENT_ROLES as _dle3_r
            _n_dense3 = _dle3(layout, _dle3_r, 60.0)
            if _n_dense3:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: final edge densify — "
                    f"inserted {_n_dense3} vertex(es) on over-60 m "
                    f"pavement edges.")
        except _GEOM_EXC:
            pass

    # Cross-strip SEAM-STEP blend (2026-07-18, SPJC in-sim cliffs): strips
    # from DIFFERENT emitters (adjacent-ground bands, gap-fill spines)
    # grading off different hosts hold metre-scale value disagreements at
    # near-adjacent — or exactly stacked — boundary vertices, emitting
    # bare terrain cliffs (SPJC: 152 pairs, worst 4.4 m over 1.26 m).
    # Must run at PIPELINE level over the COMPLETE strip population (the
    # tearing seams are cross-family) and BEFORE the late projection,
    # whose strip freeze then anchors the blended values.
    if compute_elevations:
        try:
            from .adjacent_ground import (
                _raster_reach_band_active, blend_cross_strip_seam_steps)
            if _raster_reach_band_active():
                _n_seam_blend = blend_cross_strip_seam_steps(
                    layout.shapes, layout)
                if _n_seam_blend:
                    UI.vprint(1, f"  [pav-builder] {icao}: cross-strip "
                                 f"seam blend — re-levelled "
                                 f"{_n_seam_blend} vertex(es) at "
                                 f"strip-to-strip steps.")
        except _GEOM_EXC as _seam_blend_exc:
            UI.vprint(1, f"  [pav-builder] WARN {icao}: cross-strip seam "
                         f"blend failed ({_seam_blend_exc!r}).")

    # ── Stacked-conflict wall emission (owner ruling 2026-07-19: nodes
    # are NEVER stacked — same spot ⇒ one merged node, one elevation;
    # a genuine level change is horizontal wall geometry).  to_osm now
    # hard-merges every coincident claim, so a strip vertex coincident
    # with a designed-split authority corner (building pad, service
    # road, groundside — non-donor classes) would be AVERAGED into it,
    # bending the strip metres at one column.  Resolve those sites as
    # geometry instead: retreat the strip edge and emit a
    # retaining_wall face over the vacated band.  Runs after the seam
    # blend (soft↔soft steps level first) and BEFORE the late
    # projection, whose strip freeze then anchors the final values.
    if compute_elevations:
        # Post-merge tear heal FIRST: the hard-merge consensus + the
        # seam blend can mint sub-metre near-vertical pinches after the
        # adjacent-ground emit's own final heal ran (that one sees only
        # its own emit group, and earlier values).  Same doctrine: the
        # pinch edge is the only unlawful thing — collapse it by
        # dropping an unshared, non-donor vertex.  ORDER CONTRACT: the
        # heal must run BEFORE the wall pass — a wall-retreated vertex
        # is unshared by the heal's tests, and healing after the
        # retreat drops it, springing the strip edge back across the
        # already-emitted wall band (measured CYXY: a 2.16 m² strip∩
        # wall overlap, the zero-tolerance self-overlap invariant).
        try:
            from .adjacent_ground import (
                _heal_emitted_band_tears, _raster_reach_band_active)
            if _raster_reach_band_active():
                _strip_shapes = [s for s in layout.shapes
                                 if s.ref == "adjacent_ground"]
                _n_late_heal = _heal_emitted_band_tears(
                    _strip_shapes, layout)
                if _n_late_heal:
                    UI.vprint(1, f"  [pav-builder] {icao}: post-merge "
                                 f"tear heal — collapsed pinch edge(s) "
                                 f"in {_n_late_heal} strip(s).")
        except _GEOM_EXC as _late_heal_exc:
            UI.vprint(1, f"  [pav-builder] WARN {icao}: post-merge tear "
                         f"heal failed ({_late_heal_exc!r}).")
        try:
            from .adjacent_ground import emit_stacked_conflict_walls
            _n_conflict_walls = emit_stacked_conflict_walls(layout)
            if _n_conflict_walls:
                UI.vprint(1, f"  [pav-builder] {icao}: stacked-conflict "
                             f"walls — {_n_conflict_walls} retaining "
                             f"face(s) at strip-vs-designed-split level "
                             f"changes.")
        except _GEOM_EXC as _conflict_wall_exc:
            UI.vprint(1, f"  [pav-builder] WARN {icao}: stacked-conflict "
                         f"wall emission failed "
                         f"({_conflict_wall_exc!r}).")

    # ── LATE final grade projection (2026-07-17): the mid-pipeline
    # ``final_grade_projection`` is no longer last — band/gap emission,
    # tile cuts, conformance welds, crown completion and the densify
    # passes above all reshape rings after it, and the law pairs of the
    # TRULY final rings drift over their budgets (measured: SPJC 2 apron
    # pairs 0.05/0.13 m over — a building-seat vs junction level the
    # mid-graph could not satisfy but the post-insert graph can; HECA
    # 5,822 projection-exit over-cap edges whose worst survivors emitted
    # as the within-shape building class).  Re-run the projection on the
    # final geometry: warm-seeded, so only violated neighbourhoods move.
    # Post-band pavement moves are SAFE: at ``to_osm`` the authority
    # consensus welds shared nodes at the PAVEMENT value (soft strips
    # bend to it — the A2 doctrine), and band-interior values stay
    # within the mid-edge/tear envelopes.  This call also re-freezes the
    # lockstep ``pair_caps`` export at the last law build.
    if compute_elevations and os.environ.get(
            "O4_FINAL_PROJECTION_LATE", "1") == "1":
        try:
            from .elevation_per_surface.route_profile.solve import (
                final_grade_projection as _late_fgp)
            # No projection ever runs after this call, so the exit-time
            # scoped-snapshot recapture would have no reader — skip its
            # ~4-5 s (OTHH-class) of pure cost.
            _late_fgp(layout, icao, recapture_snapshot=False)
        except _GEOM_EXC as _late_fgp_exc:
            UI.vprint(1, f"  [pav-builder] WARN {icao}: late final "
                         f"grade projection failed ({_late_fgp_exc!r}) "
                         f"— mid-pipeline projection values kept.")
    # T1b reorder second half: with the MID projection gated off, the
    # pad-host / ribbon-re-adoption / groundside-re-limit passes were
    # deferred to run against LATE-projected values (see the closure at
    # the mid site).  Runs whether or not the late gate fired — with
    # both projections off this is simply their original semantic
    # position relative to the last projection that ran (none).
    if compute_elevations and not _mid_projection_on:
        try:
            _post_projection_conformance_passes()
        except _GEOM_EXC as _deferred_conformance_exc:
            UI.vprint(1, f"  [pav-builder] WARN {icao}: deferred "
                         f"post-projection conformance passes failed "
                         f"({_deferred_conformance_exc!r}).")

    # Record this build's actual per-phase and total wall time so the
    # NEXT build of this (or a similarly-sized) airport starts with a
    # trustworthy remaining-time estimate.  Must be the LAST thing
    # before the return: the trailing edge densify and late final
    # grade projection above are real build cost (measured 2026-07-18:
    # ~40 s at OTHH for the late projection alone), and recording
    # before them undercounted the store — and every baseline derived
    # from it.  The emit-phase timer is still open here, so
    # ``phase_seconds()`` attributes the tail to the final phase.
    # Skipped under pytest — the xdist workers run airports under
    # heavy parallel load, which would poison the calibration with
    # inflated times.
    if (compute_elevations and _build_features is not None
            and os.environ.get("PYTEST_CURRENT_TEST") is None):
        try:
            from . import build_time_model as _time_model
            _time_model.record_build(
                icao, _build_features, _progress.phase_seconds(),
                time.time() - _build_started_at)
        except Exception:
            pass

    return layout


# ══════════════════════════════════════════════════════════════════
# Phase-2: elevations
# ══════════════════════════════════════════════════════════════════


# ──────────────────────────────────────────────────────────────────
# DEM + CIFP + main elevation pipeline
# (re-exported from O4_Pavement_Junctions)
# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# DEM + CIFP + main elevation pipeline
# (re-exported from O4_Pavement_Elevation)
# ──────────────────────────────────────────────────────────────────
from .elevation import (
    _compute_elevations,
)


# ──────────────────────────────────────────────────────────────────
# Airport boundary shape (re-exported from O4_Pavement_Boundary)
# ──────────────────────────────────────────────────────────────────
from .boundary import _emit_airport_boundary_shape


# ──────────────────────────────────────────────────────────────────
# Groundside (curbside / drop-off) pavement
# (re-exported from O4_Pavement_Groundside)
# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# Boundary→DEM bridge polygons (re-exported from O4_Pavement_Boundary)
# ──────────────────────────────────────────────────────────────────
from .boundary import _emit_boundary_dem_bridge


# ──────────────────────────────────────────────────────────────────
# Taxi/road bridges + tunnel portals + depressed-road segments
# (re-exported from O4_Pavement_Bridges; gated by EMIT_BRIDGES_AND_TUNNELS)
# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# Per-shape elevation field + altitude reconciliation
# (re-exported from O4_Pavement_Elevation)
# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# Elevation finalization (corner buckets, clamp, sliver, overlap)
# (re-exported from O4_Pavement_Elevation)
# ──────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────
# OSM terminal pad extraction (re-exported from O4_Pavement_Terminals)
# ──────────────────────────────────────────────────────────────────
from .terminals import (
    _close_building_outline,
    _cluster_dsf_building_facades,
    _combine_building_sources,
    _extract_osm_terminals,
    _terminal_groundside_zone,
    _terminal_pad_from_building,
)


# ──────────────────────────────────────────────────────────────────
# Junction-polygon construction
# (re-exported from O4_Pavement_Junctions)
# ──────────────────────────────────────────────────────────────────
from .pavement.junctions import (
    _find_junction_points,
)


# Centerline-slice constants moved to O4_Pavement_Config (MIN_SEGMENT_LEN_M)
# and to O4_Pavement_Centerlines (RDP_SIMPLIFY_TOL_M, SIGNIFICANT_BEND_DEG,
# BEND_CLUSTER_M, GAP_BRIDGE_MAX_M) by slice 3e.
CLOSE_INTERSECTION_M = 200.0  # dead — kept until next cleanup pass
STUB_MAX_LEN_M = 250.0        # dead — kept until next cleanup pass


# ──────────────────────────────────────────────────────────────────
# Same-ref polyline bridging
# (re-exported from O4_Pavement_Centerlines)
# ──────────────────────────────────────────────────────────────────
from .pavement.centerlines import _bridge_same_ref_polylines


# ──────────────────────────────────────────────────────────────────
# Runway-end primary-parallel stub emission
# (re-exported from O4_Pavement_Stubs)
# ──────────────────────────────────────────────────────────────────
from .pavement.stubs import _emit_primary_parallel_runway_stubs


# ──────────────────────────────────────────────────────────────────
# OSM aeroway centerline extraction + splitting
# (re-exported from O4_Pavement_Centerlines)
# ──────────────────────────────────────────────────────────────────
from .pavement.centerlines import (
    _extract_osm_taxi_centerlines,
    _find_width_transition_breakpoints,
    _split_centerlines_at_points,
)


# ──────────────────────────────────────────────────────────────────
# Taxi rect construction (re-exported from O4_Pavement_Rects)
# ──────────────────────────────────────────────────────────────────
from .pavement.rects import (
    _build_taxi_rects,
    _classify_role,
    _snap_rect_sloping_edges_to_holes,
)
