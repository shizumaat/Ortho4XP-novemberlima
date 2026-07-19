"""Pavement elevation: anchors → graph → solver → grade clamp.

Phase-2 elevation work: layer altitudes onto the role-classified
shape topology produced by Phase-1.  Pipeline:

* DEM tile loading + sampling for terrain-derived elevations.
* CIFP threshold anchors for runway profile.
* Apron-side multi-source-Dijkstra grade cone (FAA apron cap).
* Unified Laplacian Jacobi solver over the per-shape elevation
  graph.
* Per-shape grade clamp (taxiway / apron / runway).
* Junction triangulation + free-vertex clamp for the residue
  polygons.
* Sliver-junction merge + violating-junction subdivision.
* Geometric finalization (overlap clip against fixed shapes,
  shared-vertex altitude reconciliation, terminal apron
  re-derivation).

This module is large because the network ↔ solver ↔ grade triple
is tightly coupled — splitting it produces leaky abstractions.
The plan envelope (~900 lines) was optimistic; current size sits
near 3,700 lines.  Future iteration may split into ``_Solver``
and ``_Grade`` siblings if either half can be cleanly separated.

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    Constants:
      APRON_MAX_GRADE, DEM_SUFFIX, ELEVATION_GRID_STEP_M,
      ELEVATION_SMOOTH_CONVERGE_M, ELEVATION_SMOOTH_MAX_ITERS,
      SHARED_AGREE_TOL_M, SUBDIVIDE_MAX_PAIR_DIST_M,
      SUBDIVIDE_SNAP_RADIUS_M, TAXI_ANCHOR_DIST_M, TAXI_MAX_GRADE,
      USE_PER_POLYGON_ELEVATION_FIELD

    Functions (DEM + CIFP):
      _load_airport_dem, _sample_dem, _find_cifp_path

    Functions (main pipeline):
      _compute_elevations, _resample_node_altitudes_nn,
      _apply_geometric_finalization,
      _solve_pavement_elevations_unified

    Functions (per-shape elevation field):
      _smooth_within_junction_adjacent_pair_grade,
      _rederive_terminal_altitude_from_apron_neighbours,
      _enforce_shared_vertex_altitudes,
      _snap_junction_altitudes_to_rect_corners,
      _re_emit_apron_merged_runway_segments,
      _latlon_to_m_local, _orient_rect_for_altitude,
      _planar_fit, _planar_fit_residuals, _match_elev,
      _smooth_polygon_grid

    Functions (corner buckets + clamp + finalization):
      _corner_elevation_bucket, _corner_elev_map,
      _triangulate_junctions, _build_clamp_geom_state,
      _clamp_junction_free_vertices,
      _subdivide_violating_junctions,
      _merge_sliver_junctions_into_neighbours,
      _report_within_shape_violations,
      _drop_overlap_against_fixed_shapes
"""
from __future__ import annotations

import math
import os
import sys
from collections.abc import Sequence

import O4_UI_Utils as UI

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import linemerge, nearest_points, unary_union

# Narrow exception tuple for shapely / numeric-geometry failure
# modes + DEM/file I/O.  Programming errors propagate so they
# surface immediately rather than being silently masked at runtime.
_GEOM_EXC = (OSError, ValueError,
             GEOSException, TopologicalError)

from . import apt_dat_reader as APR

from .config import (
    APRON_MAX_GRADE,
    ELEV_ROUNDING_NOISE_M,
    GRADE_VISIBILITY_BUFFER_M,
    ROLE_GRADE_LIMITS,
    ROUTE_FIELD_LOCAL_WINDOW_M,
    ROUTE_FIELD_MODEL,
    RUNWAY_APRON_AREA_RATIO,
    RUNWAY_INSIDE_APRON_FRAC,
    RUNWAY_SINGLE_POLY,
    SERVICE_ROAD_MAX_GRADE,
    TAXI_MAX_GRADE,
)
from .layout import (
    AEROWAY_FOR_ROLE,
    BuiltShape,
    PavementLayout,
    R_EARTH,
    ROLE_APRON,
    ROLE_BOUNDARY,
    ROLE_CROSS_CONNECTOR,
    ROLE_GROUNDSIDE_PAVEMENT,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL,
    ROLE_SERVICE_ROAD,
    ROLE_STUB,
    ROLE_BUILDING,
    ROLE_RETAINING_WALL,
    SHARED_VERTEX_TOL_M,
    vertex_bucket,
    corner_alts_from_high_low,
)
from .pavement.vertices import (
    _drop_spike_vertices,
    _enforce_shared_vertices,
    _insert_rect_corners_into_grazing_junction_edges,
    _push_junction_vertices_off_taxi_rect_edges,
    _snap_polygon_vertices_to_rect_corners,
    _validate_shared_vertex_invariant,
)
from .pavement.runways import (
    _insert_runway_chain_bridges,
    _resolve_runway_crossings,
    _sample_runway_segment_elev,
)


__all__ = [
    "APRON_MAX_GRADE",
    "DEM_SUFFIX",
    "ELEVATION_GRID_STEP_M",
    "ELEVATION_SMOOTH_CONVERGE_M",
    "ELEVATION_SMOOTH_MAX_ITERS",
    "SHARED_AGREE_TOL_M",
    "SHARED_VERTEX_CLUSTER_TOL_M",
    "SUBDIVIDE_MAX_PAIR_DIST_M",
    "SERVICE_ROAD_MAX_GRADE",
    "SUBDIVIDE_SNAP_RADIUS_M",
    "TAXI_ANCHOR_DIST_M",
    "TAXI_MAX_GRADE",
    "USE_PER_POLYGON_ELEVATION_FIELD",
    "_apply_geometric_finalization",
    "_build_clamp_geom_state",
    "_clamp_junction_free_vertices",
    "_compute_elevations",
    "_corner_elev_map",
    "_corner_elevation_bucket",
    "_drop_overlap_against_fixed_shapes",
    "_enforce_shared_vertex_altitudes",
    "_find_cifp_path",
    "_latlon_to_m_local",
    "_drop_thin_orphan_slivers",
    "_load_airport_dem",
    "_match_elev",
    "_merge_sliver_junctions_into_neighbours",
    "_split_sloped_rects_at_violations",
    "_orient_rect_for_altitude",
    "_planar_fit",
    "_planar_fit_residuals",
    "_re_emit_apron_merged_runway_segments",
    "_rederive_terminal_altitude_from_apron_neighbours",
    "_report_within_shape_violations",
    "_resample_node_altitudes_nn",
    "_sample_dem",
    "_smooth_polygon_grid",
    "_smooth_within_junction_adjacent_pair_grade",
    "_snap_junction_altitudes_to_rect_corners",
    "_solve_pavement_elevations_unified",
    "_subdivide_violating_junctions",
    "_triangulate_junctions",
]


# Grade caps used by the per-surface elevation solver and the audit /
# check_grade pass.  Sourced from ``config`` (single source of truth);
# re-exported here for the many internal callers that import them from
# this module.  Apron == taxiway at 1.5 % (user 2026-05-18): the
# apron-reclassification pass folds apron-territory pavement the old
# solver treated as junction (1.5 %) into ROLE_APRON; the matching cap
# keeps the reclassified shapes feasible without re-solving elevation.
TAXI_ANCHOR_DIST_M = 30.0   # snap taxi rect end to runway segment
                             # elevation when within this distance

# ── Per-shape elevation field (Mode A / Mode B) ────────────────────
# Mode A: 1D smoothing along a rect's axis at this spacing.  Mode B:
# 2D grid smoothing within a non-rect pavement polygon at this
# spacing.  Per-step grade cap is ``ELEVATION_GRID_STEP_M ×
# TAXI_MAX_GRADE`` so two adjacent samples (axis or grid) cannot
# differ by more than that amount.
ELEVATION_GRID_STEP_M = 5.0
ELEVATION_SMOOTH_MAX_ITERS = 50
ELEVATION_SMOOTH_CONVERGE_M = 0.01
# Tolerance for cross-junction shared-bucket reconciliation: when
# two junctions touching the same SOFT (graph/DEM-derived) shared
# bucket end up with smoothed values farther apart than this, we
# overwrite both with the average to restore the shared-vertex
# invariant; below this they stay at their per-junction smoothed
# values (preserves the within-shape grade win).  0.10 m is ≈ 1 %
# grade across a 10 m shared edge — well below the visible-cliff
# threshold and below check_grade.py's CROSS-SHAPE 1.5 % bar.
SHARED_AGREE_TOL_M = 0.10
# Wire ``_smooth_polygon_grid`` (Mode B) into the per-junction flow
# instead of the legacy per-vertex anchor lookup + 1D ring smoothing.
# Default OFF (2026-04-26): Mode B's 2D-Euclidean anchor cones impose
# tighter grade constraints than the elevation graph's network-
# distance smoothing, which can cause regressions when rect corner
# anchors that fit network compliance sit outside the 2D cones,
# forcing wild midpoint fallbacks at free cells.  The helper +
# constants are kept so a future iteration can re-engage Mode B
# once rect-corner derivation is reworked to enforce 2D-Euclidean
# grade compliance.
USE_PER_POLYGON_ELEVATION_FIELD = False

# Per-surface elevation solver (user 2026-05-02 / 2026-05-03 redesign).
# When True, replaces ``_solve_pavement_elevations_unified`` with the
# unified Jacobi solver in ``elevation_per_surface`` that enforces the
# per-axis grade rule (rect axial only; junction multi-directional;
# rect cross-section flatness).  See
# ``docs/elevation_solver.md``.  Default ON now that
# SPJC is the validated baseline; set ``O4_PER_SURFACE_SOLVER=0``
# in the environment to fall back to the legacy unified solver.
USE_PER_SURFACE_SOLVER = (
    os.environ.get("O4_PER_SURFACE_SOLVER", "1") == "1")

# Used by both _build_clamp_geom_state (in this module) and the
# _triangulate_junctions code path (in auto_patch.triangulation).
# Defined here so triangulation can import it without forcing a
# load-order dance.
NEIGHBOUR_CLAMP_RADIUS_M = 5.0

DEM_SUFFIX = ".hgt"
_DEM_CACHE: dict[tuple[int, int], object] = {}

def _load_airport_dem(lat0: float, lon0: float, override_dem=None):
    """Return an ``O4_DEM_Utils.DEM`` covering the 1° tile that
    contains (lat0, lon0).  Auto-downloads via Ortho4XP's standard
    DEM provider chain when no local .hgt file exists.  Falls back
    to None only when the download itself fails.

    When ``override_dem`` is provided (typically Ortho4XP's
    pre-loaded ``tile.dem`` after ``smooth_raster_over_airports``),
    return it directly — avoids a redundant per-airport DEM load
    during the tile pipeline and ensures auto_patch reads the
    SAME smoothed DEM that drives Ortho4XP's airport flattening.
    The standalone path (``tools/build_target_osm.py`` and tests)
    passes ``override_dem=None`` and gets the legacy fresh-load
    behaviour.
    """
    if override_dem is not None:
        return override_dem
    tile_lat = int(math.floor(lat0))
    tile_lon = int(math.floor(lon0))
    key = (tile_lat, tile_lon)
    if key in _DEM_CACHE:
        return _DEM_CACHE[key]
    hem_ns = "S" if tile_lat < 0 else "N"
    hem_ew = "W" if tile_lon < 0 else "E"
    fname = f"{hem_ns}{abs(tile_lat):02d}{hem_ew}{abs(tile_lon):03d}{DEM_SUFFIX}"
    # Standalone path only (tests, tools/build_target_osm): production
    # auto_patch receives Ortho4XP's fully-prepared ``tile.dem`` via
    # ``override_dem`` (returned above), so this branch must produce the
    # SAME surface for tests and lab probes.
    #
    # PRODUCTION-DEM PARITY V2 (owner ruling 2026-07-19, extending the
    # 2026-07-18 probes ruling to the WHOLE standalone loop: "the tests
    # have to use the same DEM as production or they're useless").
    # Parity v1 composed the cached insets but still REPLICATED the
    # airport smoothing (whole-raster PIL blur) and skipped the
    # elevation-level overlay bake — ~1 m residuals vs true production.
    # Now this branch runs the production DEM-prep code itself
    # (``O4_Vector_Map.compose_tile_dem_from_disk``: composite assembly,
    # densification, tile-overlay bake, ``smooth_raster_over_airports``
    # with the real per-airport masks/radii, post-smoothing inset bake)
    # over a real ``CFG.Tile`` — pure disk state, NEVER a network fetch
    # for insets/overlay/airports (the base raster still auto-downloads
    # via the DEM provider chain, the legacy standalone behaviour).  A
    # cold cache degrades to the base surface with a loud warning (warm
    # it with a production build or
    # ``tools/fetch_airport_elevation_insets.py`` before cutting
    # fixtures).
    try:
        import O4_Config_Utils as _CFG
        import O4_File_Names as _FNAMES
        import O4_OSM_Utils as _OSM
        import O4_Vector_Map as _VMAP

        _tile = _CFG.Tile(tile_lat, tile_lon, "")
        try:
            # Per-tile cfg when present, else global cfg — the same
            # settings (apt_smoothing_pix, custom_dem, elevation_level,
            # inset gates) production reads.  A missing cfg leaves the
            # constructor defaults (headless tests).
            _tile.read_from_config()
        except Exception:
            pass
        _dico_airports = {}
        _airports_cache = _FNAMES.osm_cached(tile_lat, tile_lon, "airports")
        if os.path.isfile(_airports_cache):
            # Cached-layer load (the existence gate above keeps this
            # branch network-free) + the production airport-dictionary
            # chain — the smoothing masks and per-airport radii need it.
            _airport_layer = _OSM.OSM_layer()
            _OSM.OSM_queries_to_OSM_layer(
                _VMAP.AIRPORTS_QUERIES,
                _airport_layer,
                tile_lat,
                tile_lon,
                ["all"],
                cached_suffix="airports",
            )
            _dico_airports = _VMAP.build_airports_dico(_tile, _airport_layer)
        else:
            UI.vprint(
                1,
                f"  [pav-builder] WARN: standalone DEM for {fname} has NO "
                "cached airports OSM layer — airport smoothing masks "
                "unavailable, surface stays unsmoothed and diverges from "
                "production.  Warm the cache with a production build.",
            )
        dem = _VMAP.compose_tile_dem_from_disk(
            _tile, _dico_airports, write_alt_file=False
        )
        _baked_insets = getattr(dem, "airport_inset_provenance", None)
        if _baked_insets:
            UI.vprint(
                1,
                f"  [pav-builder] standalone DEM for {fname}: production "
                f"DEM-prep composed from disk ({len(_baked_insets)} cached "
                f"airport elevation inset(s), {len(_dico_airports)} "
                "airport(s) smoothed).",
            )
        else:
            UI.vprint(
                1,
                f"  [pav-builder] WARN: standalone DEM for {fname} baked NO "
                "cached airport elevation insets — base surface only.  If "
                "production uses insets here, warm the cache (production "
                "build or tools/fetch_airport_elevation_insets.py) or this "
                "surface diverges from production.",
            )
    except Exception as exc:
        UI.vprint(
            1,
            f"  [pav-builder] WARN: production-parity DEM prep failed for "
            f"{fname}: {exc!r}",
        )
        _DEM_CACHE[key] = None
        return None
    _DEM_CACHE[key] = dem
    return dem


def _sample_dem(dem, tile_lat: int, tile_lon: int,
                lat: float, lon: float) -> float | None:
    """Sample DEM elevation at (lat, lon).  Returns None if DEM is
    unavailable or out-of-tile.

    IMPORTANT: ``tile_lat``/``tile_lon`` MUST be the integer tile that
    ``dem`` actually covers — the offset ``(lon-tile_lon, lat-tile_lat)``
    is interpreted in that tile's frame.  For a cross-tile airport the
    anchor tile (``floor(layout.anchor)``) and the current build tile
    (``current_tile_lat/lon``) differ; passing the anchor-tile coords
    with the current-tile DEM (or vice-versa) silently reads elevations
    ~1° (≈100 km) away.  That was the MMOX +17 bug (bridge inner edge
    sampling the +16 valley → ~1000 m drop).  Callers must pass the
    tile that matches the DEM object in hand.
    """
    if dem is None:
        return None
    try:
        return float(dem.alt((lon - tile_lon, lat - tile_lat)))
    except _GEOM_EXC:
        return None


def _find_cifp_path(xplane_root: str, icao: str) -> str | None:
    """Locate the CIFP .dat file for an ICAO under the X-Plane
    root.  Returns None if not found."""
    cifp_dir = os.path.join(xplane_root, "Custom Data", "CIFP")
    p = os.path.join(cifp_dir, f"{icao.upper()}.dat")
    if os.path.isfile(p):
        return p
    return None


def _compute_elevations(layout: "PavementLayout", icao: str,
                        xplane_root: str, apt,
                        osm_nodes=None, osm_ways=None,
                        to_m=None,
                        apron_candidates_m: list[Polygon] | None = None,
                        tile_dem=None,
                        current_tile_lat: int | None = None,
                        current_tile_lon: int | None = None) -> None:
    """Phase-2: add altitude tags to runways (segmented), taxi
    rects, and terminal pads.  Junctions / aprons / buildings are
    left un-elevated this iteration.

    Taxi rect elevations come from a grade-compliant elevation
    network built over OSM taxiway centerlines (densified to
    ≤ 30 m edges), anchored at CIFP runway thresholds, and
    post-pass smoothed for rate-of-change compliance
    (FAA 1 %/30 m).  Per user (2026-04-24): real airports
    heavily modify the land, so DEM is a soft preference, not a
    constraint — nodes free from runway anchors follow DEM
    only when no grade-compliance rule forces otherwise.

    ``current_tile_lat`` / ``current_tile_lon`` identify the tile
    being processed by the driver — must match the DEM's tile
    (Ortho4XP's ``tile.dem`` is indexed in current-build-tile
    coords).  ``None`` falls back to ``floor(anchor)`` which is
    correct only for single-tile airports.  See pipeline.py.
    """
    lat0, lon0 = layout.anchor
    # Per user 2026-05-12: keep DEM-tile and indexing-coords in
    # lockstep.  Use current_tile_lat/lon only when ``tile_dem`` is
    # provided (DEM is the current build tile); otherwise load the
    # anchor tile DEM and use anchor coords.  See pipeline.py for the
    # rationale.
    if current_tile_lat is not None:
        tile_lat = current_tile_lat
        tile_lon = current_tile_lon
    else:
        tile_lat = int(math.floor(lat0))
        tile_lon = int(math.floor(lon0))
    if tile_dem is not None:
        dem = tile_dem                      # production: current-tile smoothed DEM
    else:
        # Standalone: load the DEM for the TILE BEING BUILT, not the anchor
        # tile.  A cross-tile airport's non-anchor (sliver) tile must sample
        # its OWN tile's DEM — the anchor-tile DEM clamps at its edge and
        # returns the wrong terrain for the sliver, so standalone/fixture
        # builds didn't match production (SPLP -78 sliver read the -77 edge).
        # current_tile == anchor (single-tile / whole-airport) → unchanged.
        dem = _load_airport_dem(tile_lat + 0.5, tile_lon + 0.5)

    # Meter-space projection (local — the layout's to_m is not
    # exposed, so reconstruct).
    cos0 = math.cos(math.radians(lat0))

    def m_to_ll(x: float, y: float):
        lon = lon0 + math.degrees(x / (R_EARTH * cos0))
        lat = lat0 + math.degrees(y / R_EARTH)
        return lat, lon

    # ── Segmented runway rectangles (legacy CIFP + DEM) ─────────
    cifp_path = _find_cifp_path(xplane_root, icao)
    runway_segment_chain = []
    runway_profile_state: dict = {}
    if cifp_path is not None and dem is not None:
        try:
            from . import driver as _AP
            from . import cifp_reader as _CIFP
            from .pavement import runway_geometry as _RWY
            cifp_runways = _CIFP.parse_cifp_file(cifp_path)
            if cifp_runways:
                pairs = _RWY.pair_runways(cifp_runways)

                # apt.dat runway geometry (sole source of truth for
                # footprint lat/lon + width) — per legacy contract.
                # Key each runway end under its ``RW``-prefixed form AND
                # its canonical (zero-padding-reconciled) form so the
                # segmenter — which iterates CIFP's zero-padded ``RW09``
                # designators — finds apt.dat geometry stored under the
                # bare ``9`` apt.dat spelling (see
                # ``runway_segments.canonical_runway_desig``).  Without
                # this, single-digit runways (TBPB 09/27) fall back to
                # CIFP geometry and never segment at pavement joins.
                from .pavement.runway_segments import (
                    canonical_runway_desig as _canon_desig)
                apt_runway_geom = {}
                for r in apt.runways:
                    geom_a = (r.lat_a, r.lon_a, r.width_m,
                              r.displaced_a_m, r.blast_a_m)
                    geom_b = (r.lat_b, r.lon_b, r.width_m,
                              r.displaced_b_m, r.blast_b_m)
                    for k in (r.desig_a,
                              "RW" + r.desig_a.lstrip("RW"),
                              _canon_desig(r.desig_a)):
                        apt_runway_geom[k] = geom_a
                    for k in (r.desig_b,
                              "RW" + r.desig_b.lstrip("RW"),
                              _canon_desig(r.desig_b)):
                        apt_runway_geom[k] = geom_b
                runway_widths = {}
                for r in apt.runways:
                    for k in (r.desig_a, _canon_desig(r.desig_a)):
                        runway_widths[k] = r.width_m
                    for k in (r.desig_b, _canon_desig(r.desig_b)):
                        runway_widths[k] = r.width_m

                # Reconcile CIFP designators to apt.dat runways by
                # GEOMETRY.  Magnetic-variation drift renumbers runways,
                # so the same physical strip can be ``03/21`` in apt.dat
                # but ``RW04/RW22`` in the CIFP (SSUM Umuarama).  A ±1
                # heading-number change defeats ``canonical_runway_desig``
                # (it only strips ``RW``/zero-padding), so every
                # designator lookup above misses, ``have_apt_geom`` is
                # False, and the runway never segments at its apt.dat
                # pavement joins.  Match by position instead, then register
                # the apt.dat geometry/width under the CIFP spellings too.
                apt_ends = [(r.lat_a, r.lon_a, r.lat_b, r.lon_b)
                            for r in apt.runways]
                cifp_to_apt = {}  # (cifp_a, cifp_b) -> (apt_desig_a, apt_desig_b)
                for desig_a, data_a, desig_b, data_b in pairs:
                    if (desig_b is None or data_a is None or data_b is None
                            or not apt_ends):
                        continue
                    m = _RWY.match_runway_ends_by_geometry(
                        data_a["lat"], data_a["lon"],
                        data_b["lat"], data_b["lon"], apt_ends)
                    if m is None:
                        continue
                    idx, swapped = m
                    r = apt.runways[idx]
                    geom_a = (r.lat_a, r.lon_a, r.width_m,
                              r.displaced_a_m, r.blast_a_m)
                    geom_b = (r.lat_b, r.lon_b, r.width_m,
                              r.displaced_b_m, r.blast_b_m)
                    if swapped:
                        geom_a, geom_b = geom_b, geom_a
                    for k in (desig_a, "RW" + desig_a.lstrip("RW"),
                              _canon_desig(desig_a)):
                        apt_runway_geom.setdefault(k, geom_a)
                        runway_widths.setdefault(k, r.width_m)
                    for k in (desig_b, "RW" + desig_b.lstrip("RW"),
                              _canon_desig(desig_b)):
                        apt_runway_geom.setdefault(k, geom_b)
                        runway_widths.setdefault(k, r.width_m)
                    apt_a = r.desig_b if swapped else r.desig_a
                    apt_b = r.desig_a if swapped else r.desig_b
                    cifp_to_apt[(desig_a, desig_b)] = (apt_a, apt_b)

                class _TileStub:
                    lat: int
                    lon: int | None
                    dem: object
                tile = _TileStub()
                tile.lat = tile_lat
                tile.lon = tile_lon
                tile.dem = dem

                # Per user 2026-05-05: thread the apt.dat-pavement-
                # runway intersection points (collected in pipeline.py
                # at runway-rect build time) into the segmenter so
                # segment seam corners align with apt.dat boundary
                # intersections.  The widening pass then doesn't need
                # to bridge the gap with boundary-trace waypoints.
                pav_intersections = getattr(
                    layout, "_pav_runway_intersections", None)
                # When a runway was renumbered (geometry reconciliation
                # above), the pavement-intersection seams are keyed under
                # the apt.dat designators; mirror them onto the CIFP
                # designators the segmenter iterates so the seams still
                # land (else a geometry-matched runway has correct width
                # but no pavement-join segmentation).
                if pav_intersections and cifp_to_apt:
                    pav_intersections = dict(pav_intersections)
                    for (cifp_a, cifp_b), (apt_a, apt_b) in cifp_to_apt.items():
                        pts = (pav_intersections.get((apt_a, apt_b))
                               or pav_intersections.get((apt_b, apt_a)))
                        if not pts:
                            continue
                        pav_intersections.setdefault((cifp_a, cifp_b), pts)
                        pav_intersections.setdefault((cifp_b, cifp_a), pts)
                _xml, runway_segment_chain, runway_profile_state = (
                    _AP.generate_patch_osm(
                        icao, pairs, runway_widths=runway_widths,
                        tile=tile, apt_runways=apt_runway_geom,
                        pav_intersections=pav_intersections))
        except _GEOM_EXC:
            runway_segment_chain = []
            runway_profile_state = {}

    # Stash the per-pair FAA-profile state on the layout so a
    # downstream redistribute step can fold seam DEM altitudes
    # into the same profile (see ``runway_redistribute``).
    layout._runway_profile_state = runway_profile_state

    new_runway_polys: list[Polygon] = []

    # ── Runway DE-SEGMENTATION (O4_RUNWAY_SINGLE_POLY) ───────────────
    # docs/runway_single_polygon_plan.md: ONE polygon ring per runway
    # ref, built straight from the persisted FAA profile state — no
    # interior cross-edges, so no flat full-width mesh constraint ever
    # cuts across the crown (the part-30i tent hotfix becomes a
    # structural no-op for these rings).  Refs whose rings OVERLAP
    # another runway's ring (a runway-runway crossing) keep the legacy
    # segmented path for now: ``_resolve_runway_crossings`` still
    # carves the crossing junction from member sub-rect pieces (the
    # crossing-carve slice lifts this).
    single_poly_refs: set = set()
    _single_poly_candidates: dict = {}
    _single_poly_crossing_specs: list = []
    _single_poly_ring_pieces: dict = {}
    if RUNWAY_SINGLE_POLY and runway_profile_state:
        def _single_poly_ref(desig_pair) -> str:
            a, b = desig_pair
            a = a[2:] if a and a.startswith("RW") else a
            b = b[2:] if b and b.startswith("RW") else b
            return f"{a}/{b}"
        for _pair, _profile in runway_profile_state.items():
            try:
                _built = _build_single_poly_runway_ring(
                    _profile, lat0, lon0, cos0)
            except _GEOM_EXC:
                _built = None
            if _built is not None:
                _single_poly_candidates[_single_poly_ref(_pair)] = _built
        # Runway-runway CROSSINGS (slice 5): pairs whose PHYSICAL AXES
        # intersect and whose rings overlap non-trivially (the
        # ``_resolve_runway_crossings`` pass-1 discriminator) are
        # carved: the crossing junction = union of both refs' station
        # slabs over the overlap, each ring loses that slab (the cut
        # line passes through existing station vertices), and the
        # junction takes the inverse-distance profile blend.  A
        # close-pass overlap with NO axis meeting stays whole — that
        # is the overlap-clip pass's job, exactly as with segments.
        _candidate_refs = list(_single_poly_candidates)
        _crossing_sites = []
        for _i in range(len(_candidate_refs)):
            _ra = _candidate_refs[_i]
            _ga = _single_poly_candidates[_ra][2]
            _axis_a = LineString([_ga['axis_a'], _ga['axis_b']])
            for _j in range(_i + 1, len(_candidate_refs)):
                _rb = _candidate_refs[_j]
                _gb = _single_poly_candidates[_rb][2]
                try:
                    if not _axis_a.intersects(
                            LineString([_gb['axis_a'], _gb['axis_b']])):
                        continue
                    _overlap = _single_poly_candidates[_ra][0].intersection(
                        _single_poly_candidates[_rb][0])
                except _GEOM_EXC:
                    continue
                if _overlap.is_empty or _overlap.area < 20.0:
                    continue
                _crossing_sites.append((_ra, _rb, _overlap))
        try:
            (_single_poly_crossing_specs,
             _single_poly_ring_pieces) = _carve_single_poly_crossings(
                _single_poly_candidates, _crossing_sites)
        except _GEOM_EXC:
            _single_poly_crossing_specs, _single_poly_ring_pieces = [], {}
        single_poly_refs = set(_single_poly_candidates)

    if runway_segment_chain:
        # Drop the single-rect runway shapes; replace with segments.
        old_runways = [s for s in layout.shapes if s.role == ROLE_RUNWAY]
        layout.shapes = [s for s in layout.shapes if s.role != ROLE_RUNWAY]

        # Single-poly rings first (de-seg refs); the chain loop below
        # skips their per-segment entries.  A fully-flat profile keeps
        # the flat ``altitude=`` form (same class as the MULTI_FLAT
        # consolidation — ``stitch_pavement_to_flat_runways`` keys off
        # it); anything else is per-vertex from birth.  A crossing-
        # carved ref contributes its remainder PIECES instead of the
        # whole ring; the crossing junction itself is appended below.
        for _ring_ref in sorted(single_poly_refs):
            if _ring_ref in _single_poly_ring_pieces:
                _piece_list = _single_poly_ring_pieces[_ring_ref]
            else:
                _rp, _ra, _rg = _single_poly_candidates[_ring_ref]
                _piece_list = [(_rp, _ra)]
            for _ring_poly, _ring_alts in _piece_list:
                _ring_shape = BuiltShape(
                    polygon=_ring_poly, role=ROLE_RUNWAY, ref=_ring_ref,
                    from_single_poly=True)
                if (max(_ring_alts) - min(_ring_alts)
                        < _SINGLE_POLY_FLAT_TOL_M):
                    _ring_shape.altitude = round(
                        sum(_ring_alts[:-1]) / (len(_ring_alts) - 1), 2)
                else:
                    _ring_shape.node_altitudes = list(_ring_alts)
                layout.shapes.append(_ring_shape)
                new_runway_polys.append(_ring_poly)
        for _xing_poly, _xing_alts, _xing_ref in _single_poly_crossing_specs:
            layout.shapes.append(BuiltShape(
                polygon=_xing_poly, role=ROLE_RUNWAY_CROSSING,
                ref=_xing_ref, node_altitudes=list(_xing_alts)))
        if _single_poly_crossing_specs:
            from .pavement.runways import (
                _absorb_crossing_vertices_into_adjacent_rects)
            _absorb_crossing_vertices_into_adjacent_rects(layout)
        if single_poly_refs:
            UI.vprint(1,
                f"  [pav-builder] {icao}: de-seg — single-poly runway "
                f"ring for {len(single_poly_refs)} ref(s)"
                + (f", {len(_single_poly_crossing_specs)} crossing "
                   f"junction(s) carved"
                   if _single_poly_crossing_specs else "")
                + ".")
        # Fallback ref when a segment tuple doesn't carry a desig pair
        # (older chain entries before 2026-05-14 didn't tag the source
        # runway).  Keeps emit safe if a future segmenter path forgets
        # to append the pair.
        ref_fallback = "/".join(sorted(set(
            f"{r.desig_a}/{r.desig_b}" for r in apt.runways)))

        def _ref_from_desig_pair(desig_pair):
            """Per user 2026-05-14: tag each runway segment with the
            ref of the SOURCE runway it was generated for, not the
            airport-wide merged ref.  At CYXY each runway's chain
            now emits with its own pair (e.g. ``14R/32L``) so
            downstream code (junction propagation, OSM consumers,
            audit reports) can identify which runway each segment
            belongs to instead of getting the same opaque
            ``02/20/14L/32R/14R/32L`` string on every segment.

            Strips the ``RW`` prefix that CIFP designators carry so
            the emitted ref matches the apt.dat row-100 convention
            already used at single-runway airports like SPLP
            (``02/20`` rather than ``RW02/RW20``).
            """
            def _strip(d):
                if not d:
                    return d
                return d[2:] if d.startswith("RW") else d
            if desig_pair is None:
                return ref_fallback
            a, b = desig_pair
            a = _strip(a)
            b = _strip(b)
            if a and b:
                return f"{a}/{b}"
            return a or b or ref_fallback
        # Legacy generate_patch_osm pads each side by
        # RUNWAY_MARGIN=3 m for imagery coverage; strip that so
        # the segmented runways match the apt.dat width that our
        # Phase-1 junctions/rects were built against.  Keeps the
        # post-elevation layout overlap-free.
        _LEGACY_RUNWAY_MARGIN = 3.0
        for i, seg in enumerate(runway_segment_chain):
            # Multi-node flat segment (user 2026-05-09): a single
            # consolidated polygon covering N centerline samples at
            # uniform elevation, with intermediate corners at pav_
            # intersection positions.  Tagged tuple shape:
            #   ("MULTI_FLAT", [(lat, lon), ...], elev, width, desig_pair)
            if (len(seg) >= 4 and seg[0] == "MULTI_FLAT"):
                _tag = seg[0]
                samples_ll = seg[1]
                elev_flat = seg[2]
                width_m = seg[3]
                desig_pair = seg[4] if len(seg) >= 5 else None
                seg_ref = _ref_from_desig_pair(desig_pair)
                if seg_ref in single_poly_refs:
                    continue  # de-seg: this ref emitted as ONE ring above
                width_m = max(1.0,
                               width_m - 2.0 * _LEGACY_RUNWAY_MARGIN)
                samples_xy = [
                    _latlon_to_m_local(la, lo, lat0, lon0, cos0)
                    for la, lo in samples_ll]
                if len(samples_xy) < 2:
                    continue
                ax, ay = samples_xy[0]
                bx, by = samples_xy[-1]
                length = math.hypot(bx - ax, by - ay)
                if length < 1.0:
                    continue
                ux = (bx - ax) / length
                uy = (by - ay) / length
                px = -uy * width_m / 2.0
                py = ux * width_m / 2.0
                # Build ring: left side A→B, right side B→A.
                ring = []
                for x, y in samples_xy:
                    ring.append((x + px, y + py))
                for x, y in reversed(samples_xy):
                    ring.append((x - px, y - py))
                poly = Polygon(ring)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.is_empty or poly.geom_type != "Polygon":
                    continue
                shape = BuiltShape(
                    polygon=poly, role=ROLE_RUNWAY, ref=seg_ref)
                shape.altitude = round(float(elev_flat), 1)
                layout.shapes.append(shape)
                new_runway_polys.append(poly)
                continue
            # Legacy 4-corner (sloped or flat).  Tuple shape:
            # (lat_a, lon_a, elev_a, lat_b, lon_b, elev_b, width_m,
            #  desig_pair) — pair optional for backward compat.
            lat_a, lon_a, elev_a, lat_b, lon_b, elev_b, width_m = seg[:7]
            desig_pair = seg[7] if len(seg) >= 8 else None
            seg_ref = _ref_from_desig_pair(desig_pair)
            if seg_ref in single_poly_refs:
                continue  # de-seg: this ref emitted as ONE ring above
            width_m = max(1.0, width_m - 2.0 * _LEGACY_RUNWAY_MARGIN)
            ax, ay = _latlon_to_m_local(lat_a, lon_a, lat0, lon0, cos0)
            bx, by = _latlon_to_m_local(lat_b, lon_b, lat0, lon0, cos0)
            length = math.hypot(bx - ax, by - ay)
            if length < 1.0:
                continue
            # Perpendicular half-width offset — always compute
            # relative to the direction from A to B.
            ux = (bx - ax) / length
            uy = (by - ay) / length
            px = -uy * width_m / 2.0
            py = ux * width_m / 2.0
            # X-Plane patch convention: a way's short edge from
            # the last to the first node (way[-2:]) is interpreted
            # as the ``altitude_high`` side; the short edge from
            # way[1] to way[2] is the ``altitude_low`` side.  So
            # corners 0 and 3 must be at the HIGH-elevation end.
            # If B is the higher end, start the ring from B.
            if float(elev_a) >= float(elev_b):
                # A is HIGH: ring starts at A-side corners.
                corners = [
                    (ax + px, ay + py),   # 0: A-left  (HIGH side)
                    (bx + px, by + py),   # 1: B-left  (LOW side)
                    (bx - px, by - py),   # 2: B-right (LOW side)
                    (ax - px, ay - py),   # 3: A-right (HIGH side)
                ]
                eh, el = float(elev_a), float(elev_b)
            else:
                # B is HIGH: reverse — start the ring at B-side.
                # The perpendicular flips sign when direction
                # reverses, so B-left in the reversed walk is the
                # original B-right (and similarly A).
                corners = [
                    (bx - px, by - py),   # 0: B-left  (HIGH side)
                    (ax - px, ay - py),   # 1: A-left  (LOW side)
                    (ax + px, ay + py),   # 2: A-right (LOW side)
                    (bx + px, by + py),   # 3: B-right (HIGH side)
                ]
                eh, el = float(elev_b), float(elev_a)
            poly = Polygon(corners)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
            shape = BuiltShape(
                polygon=poly, role=ROLE_RUNWAY, ref=seg_ref)
            if abs(eh - el) >= 0.1:
                # Per-vertex from BIRTH (user 2026-07-06): runways
                # never carry the positional [H, L, L, H] hi/lo form —
                # the corner order above is [HIGH, LOW, LOW, HIGH] by
                # construction, so the values map directly.
                corner_values = [round(eh, 2), round(el, 2),
                                 round(el, 2), round(eh, 2)]
                shape.node_altitudes = corner_values + [corner_values[0]]
            else:
                shape.altitude = round((eh + el) / 2.0, 2)
            layout.shapes.append(shape)
            new_runway_polys.append(poly)

        # Drop any newly-emitted runway segment whose footprint is
        # contained inside an apt.dat / DSF pavement polygon that's
        # MUCH LARGER than the segment itself.  Such a polygon is
        # an apron enclosing the runway — the runway physically
        # merges with the surrounding pavement (e.g. CYXY runway 02
        # crosses the south apron at lat 60.7124).  Keeping a
        # separate runway rect there produces a visible rectangular
        # ribbon through the apron.  The junction polygon that
        # fills the apron will smoothly join the LAST surviving
        # runway segment's end at its shared corner.
        #
        # Detection: the segment is ≥ RUNWAY_INSIDE_APRON_FRAC
        # contained inside an apt.dat polygon whose area is
        # ≥ RUNWAY_APRON_AREA_RATIO times the segment area.  A
        # normal runway sits inside a runway-shaped apt.dat
        # polygon that's only marginally larger; an apron-merged
        # runway sits inside a polygon many times its size.
        from .config import ABSORB_RUNWAY_IN_APRON
        if apron_candidates_m and ABSORB_RUNWAY_IN_APRON:
            from shapely.strtree import STRtree
            try:
                index = STRtree(apron_candidates_m)
            except _GEOM_EXC:
                index = None
            kept_shapes: list[BuiltShape] = []
            kept_polys: list[Polygon] = []
            # Track each dropped segment alongside the apron
            # candidate that contained it — used below to clip the
            # hole-fill merge so it can't bleed outside the apron.
            dropped_with_apron: list[tuple[Polygon, Polygon]] = []
            n_dropped = 0
            for sh in layout.shapes:
                if sh.role != ROLE_RUNWAY:
                    kept_shapes.append(sh)
                    continue
                if getattr(sh, "from_single_poly", False):
                    # De-seg ring: the per-segment containment test is
                    # meaningless on a whole-runway ring (an enclosing
                    # apron is never RUNWAY_APRON_AREA_RATIO × the full
                    # runway's area).  Apron-merge CARVING for single
                    # rings lands with the crossing-carve slice; until
                    # then the ring is kept whole.
                    kept_shapes.append(sh)
                    if sh.polygon is not None and not sh.polygon.is_empty:
                        kept_polys.append(sh.polygon)
                    continue
                drop = False
                drop_apron: Polygon | None = None
                if (sh.polygon is not None
                        and not sh.polygon.is_empty):
                    seg_area = sh.polygon.area
                    cand_iter = (index.query(sh.polygon)
                                 if index is not None
                                 else range(len(apron_candidates_m)))
                    for ci in cand_iter:
                        cand = apron_candidates_m[ci]
                        try:
                            if (cand.area
                                    < seg_area
                                    * RUNWAY_APRON_AREA_RATIO):
                                continue
                            inter = sh.polygon.intersection(cand)
                            if (inter.area / seg_area
                                    > RUNWAY_INSIDE_APRON_FRAC):
                                drop = True
                                drop_apron = cand
                                break
                        except _GEOM_EXC:
                            continue
                if drop:
                    n_dropped += 1
                    if (sh.polygon is not None
                            and not sh.polygon.is_empty
                            and drop_apron is not None):
                        dropped_with_apron.append(
                            (sh.polygon, drop_apron))
                    # Per user 2026-04-29 (CYXY runway 32L/16R
                    # ridge): preserve the dropped segment's
                    # altitude info on the layout so a later
                    # pass can imprint the runway's slope onto
                    # any apron-junction polygon that ends up
                    # covering this segment's footprint.  Without
                    # this, the surrounding apron junction's
                    # vertices are 4-5 m higher than the runway
                    # at the same XY, producing a "ridge across
                    # the runway" the user reported.
                    if (sh.polygon is not None
                            and not sh.polygon.is_empty
                            and (sh.altitude_high is not None
                                 or sh.altitude is not None)):
                        if not hasattr(
                                layout,
                                "_apron_merged_runway_drops"):
                            layout._apron_merged_runway_drops = []
                        layout._apron_merged_runway_drops.append(sh)
                    continue
                kept_shapes.append(sh)
                if sh.polygon is not None and not sh.polygon.is_empty:
                    kept_polys.append(sh.polygon)
            if n_dropped:
                try:
                    UI.vprint(1,
                        f"  [pav-builder] {icao}: dropped "
                        f"{n_dropped} runway segment(s) "
                        f"apron-merged.")
                except _GEOM_EXC:
                    pass
                layout.shapes = kept_shapes
                new_runway_polys = kept_polys

                # Per user 2026-04-28: dropping the apron-merged
                # runway segment leaves no hole — the residue
                # computation in ``build_airport_pavement`` already
                # excludes apron-merged regions from the runway-
                # subtraction (see ``_effective_runway_union``), so
                # the surrounding apron junction(s) cover the
                # runway segment's footprint naturally.  Nothing to
                # do here.
                _ = dropped_with_apron  # used only for the log line

        # Resolve runway-runway crossings: when two runway segments
        # overlap significantly (e.g. CYXY's crosswind 02/20 crossing
        # both 14R/32L and 14L/32R), drop both and emit a single
        # junction polygon at the union, with per-vertex altitudes
        # interpolated from the source segments.  Without this, the
        # downstream overlap-clip pass would clip one runway against
        # the other, leaving a 5-vertex shape that still carries
        # ``altitude_high`` / ``altitude_low`` tags — which X-Plane's
        # patch format only renders correctly on 4-corner rects.
        n_crossings = _resolve_runway_crossings(layout)
        if n_crossings:
            try:
                UI.vprint(1,
                    f"  [pav-builder] {icao}: resolved "
                    f"{n_crossings} runway crossing(s) into "
                    f"junction polygon(s).")
            except _GEOM_EXC:
                pass
            new_runway_polys = [
                s.polygon for s in layout.shapes
                if s.role == ROLE_RUNWAY
                and s.polygon is not None
                and not s.polygon.is_empty]
        # Per user 2026-04-30: bridge-segment insertion was
        # tried (option c) but produced worse results — the
        # inserted bridges overlap apron junctions whose
        # altitudes weren't synchronized, creating 10m+ range
        # junctions with 141 % worst-edge grades.  Function
        # ``_insert_runway_chain_bridges`` is left in place but
        # not called pending a different approach.
        if False:
            n_bridges = _insert_runway_chain_bridges(layout)

        # Segmented runway boundaries can drift sub-metre from the
        # original single-rect runway that junctions / rects were
        # built against, leaving tiny overlap slivers.  Subtract
        # the new runway union from junctions (and rects, defensively)
        # to eliminate them.
        if new_runway_polys:
            try:
                new_rwy_union = unary_union(
                    [p.buffer(0) for p in new_runway_polys]
                ).buffer(0)
            except _GEOM_EXC:
                new_rwy_union = None
            if new_rwy_union is not None and not new_rwy_union.is_empty:
                # Two clip regions:
                #
                #   `taxi_clip` — tiny buffer (0.05 m).  For taxi
                #   rects, only kills sub-metre sliver overlap with
                #   the runway while keeping the 4-corner rect
                #   shape intact.
                #
                #   `junction_clip` — exact runway shape, no
                #   outward buffer.  Per user 2026-05-11: a 2 m
                #   outward buffer (used here previously) pushed
                #   every adjacent junction polygon 2 m off the
                #   runway boundary, creating a visible sliver gap
                #   at every taxi-junction-to-runway interface (the
                #   -10178 / V1-throat issue at SPJC).  The original
                #   2026-04-24 rationale ("junction vertices can't
                #   land mid-edge on a runway short edge") is
                #   handled downstream by
                #   ``_snap_polygon_vertices_to_rect_corners`` +
                #   ``widen_junctions_to_runway_corners`` /
                #   ``stitch_pavement_to_flat_runways``; the buffer
                #   was double-protection that broke the seam.
                try:
                    taxi_clip = new_rwy_union.buffer(0.05)
                except _GEOM_EXC:
                    taxi_clip = new_rwy_union
                junction_clip = new_rwy_union
                # Rebuild layout.shapes in-place: when the clip
                # produces a MultiPolygon (e.g. a junction that
                # straddled the old runway ends up as two pieces
                # after the new segmented runway with overruns
                # replaces the old single rect), emit EVERY
                # sub-polygon above MIN_JUNCTION_AREA_M2 so no
                # pavement is lost (fixes the missing F/RW34R
                # gap junction, user 2026-04-24).
                from dataclasses import replace as _dc_replace
                new_shapes: list[BuiltShape] = []
                for shape in layout.shapes:
                    if shape.role in (ROLE_RUNWAY, ROLE_BUILDING):
                        new_shapes.append(shape)
                        continue
                    # Clean sub-polygon before difference — apt.dat
                    # unions can produce polygons with self-kissing
                    # boundaries that trigger "side location" errors
                    # in shapely's overlay.
                    src = shape.polygon
                    if not src.is_valid:
                        try: src = src.buffer(0)
                        except _GEOM_EXC: pass
                    clip_region = (junction_clip
                                   if shape.role == ROLE_JUNCTION
                                   else taxi_clip)
                    try:
                        clipped = src.difference(clip_region)
                    except _GEOM_EXC:
                        try:
                            clipped = src.buffer(0).difference(clip_region)
                        except _GEOM_EXC:
                            new_shapes.append(shape)
                            continue
                    if clipped.is_empty:
                        continue
                    pieces = ([clipped]
                              if clipped.geom_type == "Polygon"
                              else list(getattr(clipped, "geoms", [])))

                    def _keep_piece(p) -> bool:
                        # "No pavement is lost": a COMPACT small piece is
                        # real pavement (user 2026-07-04, CYXY: 20-50 m²
                        # taxi-intersection remainders vanished once the
                        # service-strip carve made their parents smaller
                        # — visible holes at every affected junction).
                        # Only genuine hairline slivers (nothing survives
                        # a 1 m inward buffer) stay dropped below 50 m².
                        if p.geom_type != "Polygon":
                            return False
                        if p.area >= 50.0:
                            return True
                        if p.area < 4.0:
                            return False
                        try:
                            return not p.buffer(-1.0).is_empty
                        except _GEOM_EXC:
                            return False

                    pieces = [p for p in pieces if _keep_piece(p)]
                    if not pieces:
                        continue
                    # Keep the shape metadata on the largest piece,
                    # emit any other pieces as new shapes with the
                    # same role/tags.  For junctions this splits
                    # the residue polygon; for rects this almost
                    # never splits (their snap keeps them whole).
                    pieces.sort(key=lambda g: -g.area)
                    shape.polygon = pieces[0]
                    new_shapes.append(shape)
                    for extra in pieces[1:]:
                        new_shapes.append(_dc_replace(
                            shape, polygon=extra, source_axis=None))
                layout.shapes = new_shapes
                from .geom_guard import coverage_probe as _covpe
                _covpe(layout, "ce-post-runway-clip")

                # Per user 2026-04-28: junction polygon vertices
                # cannot land on a sloping rect's edge interior —
                # only on corners.  Boundary intersection points
                # from the runway difference can land along a
                # runway rect's edge; snap them to the nearest
                # corner.  Same helper used in
                # ``_resolve_runway_crossings``.
                sloping_rect_polys_for_snap = [
                    s.polygon for s in layout.shapes
                    if s.role in (ROLE_RUNWAY,
                                   ROLE_PRIMARY_PARALLEL,
                                   ROLE_SECONDARY_PARALLEL,
                                   ROLE_STUB,
                                   ROLE_CROSS_CONNECTOR)
                    and s.polygon is not None
                    and not s.polygon.is_empty]
                # RECT-ERA PASS — RETIRED UNDER THE GLOBAL SLICE (user
                # 2026-07-03): with no taxi rects, this snapped slice-face
                # vertices up to 5 m onto RUNWAY corners, and the helper
                # rebuilds from ``poly.exterior`` ONLY — a conformant
                # keyhole-cut face wrapping a pav_union hole came back with
                # its cut topology collapsed and the hole PAVED OVER (the
                # SPJC 7,025 m² U-hole between two parallel spines; the
                # slice itself preserved it, measured face-by-face).  The
                # runway seam is owned by planarize_airside + the stitch
                # passes under the slice.  Rect model keeps the snap (its
                # rect-corner altitude convention still needs it).
                from .config import (CURVE_NATIVE_SPINE as _CNS9,
                                     ROUTE_ARC_SPINE as _RAS9)
                if not (_CNS9 or _RAS9):
                    for shape in layout.shapes:
                        if shape.role != ROLE_JUNCTION:
                            continue
                        if (shape.polygon is None
                                or shape.polygon.is_empty):
                            continue
                        # 5 m tolerance is enough to catch overlay-
                        # precision drift after the (no-buffer)
                        # runway difference; tighter than the legacy
                        # 2 m buffer's 5 m margin in cases where the
                        # junction's exact boundary should remain
                        # flush with the runway side.
                        snapped = _snap_polygon_vertices_to_rect_corners(
                            shape.polygon,
                            sloping_rect_polys_for_snap,
                            snap_tol_m=5.0)
                        if snapped is not None and not snapped.is_empty:
                            shape.polygon = snapped

    # ── Terminal pad elevations ─────────────────────────────────
    # Per user 2026-05-03: only runway corners are HARD; terminals
    # may adjust to comply with grade rules.  When the per-surface
    # solver is enabled it handles terminal altitudes itself (soft
    # nodes with a flatness constraint), so skip the legacy
    # pre-pinning here.  Legacy block (preserved while flag is
    # off): per user 2026-04-28: CIFP runway thresholds are the ONLY
    # truly authoritative elevations.  Everything else, including
    # the terminal altitude, should be derived to satisfy FAA
    # grade rules with the propagated runway / taxi / apron
    # values.  The previous DEM-median rule placed terminals on
    # naturally elevated ground (700.8 m at CYXY) when their
    # actual apron-side neighbours were 6-8 m lower; the apron-
    # pin then had to bridge that gap producing 4-7 % grade
    # violations.
    #
    # Rule (per user clarification):
    #   * Terminal altitude = MAX value that respects
    #     APRON_MAX_GRADE with every nearby HARD anchor (runway /
    #     taxi rect corner) at each terminal corner.  Specifically
    #     at each corner C, max allowed = MIN over nearby
    #     anchors A of (A.elev + APRON_MAX_GRADE × dist(C, A));
    #     terminal altitude = MIN over corners.
    #   * Floor at the highest nearby anchor (don't drop below
    #     the local terrain just because grade allows it).
    #   * Ceiling at the DEM-median (don't raise above natural
    #     ground).
    #   * Fall back to DEM-median if no anchors are available.
    runway_corner_pts: list[tuple[float, float, float]] = []
    if USE_PER_SURFACE_SOLVER:
        # Skip the entire legacy terminal pre-pin block.  The
        # per-surface solver treats terminals as SOFT nodes and
        # derives their altitudes from the constrained Laplacian.
        runway_corner_pts = []  # remain empty to no-op the loop below

    for s in layout.shapes:
        if USE_PER_SURFACE_SOLVER:
            break  # legacy terminal pre-pin disabled
        if s.role not in (
                ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
                ROLE_SECONDARY_PARALLEL, ROLE_STUB,
                ROLE_CROSS_CONNECTOR):
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            r_coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if r_coords and r_coords[0] == r_coords[-1]:
            r_coords = r_coords[:-1]
        if len(r_coords) != 4:
            continue
        if (s.altitude_high is not None
                and s.altitude_low is not None):
            per = [s.altitude_high, s.altitude_low,
                   s.altitude_low, s.altitude_high]
        elif s.altitude is not None:
            per = [float(s.altitude)] * 4
        else:
            continue
        for (x, y), a in zip(r_coords, per):
            runway_corner_pts.append(
                (float(x), float(y), float(a)))

    TERMINAL_NEIGHBOUR_RADIUS_M = 250.0
    INF = float("inf")
    for shape in layout.shapes:
        if USE_PER_SURFACE_SOLVER:
            break  # legacy terminal pre-pin disabled
        if shape.role != ROLE_BUILDING:
            continue
        if shape.polygon is None or shape.polygon.is_empty:
            continue
        # DEM-median ceiling (legacy rule).
        dem_samples: list[float] = []
        try:
            t_corners = list(shape.polygon.exterior.coords)
            if t_corners and t_corners[0] == t_corners[-1]:
                t_corners = t_corners[:-1]
        except _GEOM_EXC:
            t_corners = []
        for x, y in [(shape.polygon.centroid.x,
                      shape.polygon.centroid.y)] + t_corners:
            lat, lon = m_to_ll(x, y)
            e = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
            if e is not None:
                dem_samples.append(e)
        if dem_samples:
            dem_samples.sort()
            dem_median = dem_samples[len(dem_samples) // 2]
        else:
            dem_median = None
        # Anchor-based max-allowable rule.
        new_alt: float | None = None
        if runway_corner_pts and t_corners:
            per_corner_max: list[float] = []
            for cx, cy in t_corners:
                corner_max = INF
                hits = 0
                for ax, ay, ae in runway_corner_pts:
                    d = math.hypot(cx - ax, cy - ay)
                    if d > TERMINAL_NEIGHBOUR_RADIUS_M:
                        continue
                    allowed = ae + APRON_MAX_GRADE * d
                    if allowed < corner_max:
                        corner_max = allowed
                    hits += 1
                if hits > 0 and corner_max != INF:
                    per_corner_max.append(corner_max)
            if per_corner_max:
                # Terminal altitude must satisfy the MOST
                # restrictive corner.
                max_alt_from_anchors = min(per_corner_max)
                # Floor at the highest nearby anchor: don't
                # drop terminal below local pavement just
                # because grade allows it.
                anchor_floor = -INF
                for ax, ay, ae in runway_corner_pts:
                    # Only consider anchors with at least one
                    # corner within radius.
                    for cx, cy in t_corners:
                        if math.hypot(cx - ax,
                                      cy - ay) <= TERMINAL_NEIGHBOUR_RADIUS_M:
                            if ae > anchor_floor:
                                anchor_floor = ae
                            break
                candidate = max_alt_from_anchors
                if anchor_floor != -INF and candidate < anchor_floor:
                    candidate = anchor_floor
                # Ceiling at DEM-median (don't raise above
                # natural ground).
                if (dem_median is not None
                        and candidate > dem_median):
                    candidate = dem_median
                new_alt = round(float(candidate), 1)
        if new_alt is None and dem_median is not None:
            new_alt = round(float(dem_median), 1)
        if new_alt is None:
            continue
        shape.altitude = new_alt
        try:
            import sys as _sys
            dem_str = (f"{dem_median:.1f}" if dem_median is not None
                       else "n/a")
            UI.vprint(1,
                f"  [pav-builder] terminal({shape.ref or '?'}) "
                f"altitude {new_alt} m (max grade-compliant from "
                f"runway corners within "
                f"{TERMINAL_NEIGHBOUR_RADIUS_M:.0f} m, "
                f"DEM-median ceiling = {dem_str}).")
        except _GEOM_EXC:
            pass

    # ── Phase D: Geometric refinement + unified elevation solve ─
    # Run the geometric polygon-refinement passes (junction edge
    # push, triangulation, clamp, subdivide) interleaved with two
    # unified-Laplacian passes — see
    # ``_apply_geometric_finalization`` for the full sequence.
    # This replaces the previous bottom-up DEM-driven pipeline
    # (centerline graph + propagate_bounds + smooth_rate_of_change
    # + plateau snap + apron-pin + post-pin reconciliation).
    _apply_geometric_finalization(
        layout, icao, dem, tile_lat, tile_lon, m_to_ll)

    # ── Phase E: Diagnostics ────────────────────────────────────
    # NOTE: the within-shape grade WARN is intentionally NOT emitted
    # here.  ``build_airport_pavement`` (the caller) runs a chain of
    # post-elevation passes — ``_enforce_shared_vertices``,
    # ``_snap_junction_altitudes_to_rect_corners``,
    # ``_enforce_shared_vertex_altitudes``,
    # ``_smooth_within_junction_adjacent_pair_grade``, and a final
    # snap/agree round — that meaningfully change per-vertex
    # altitudes after this point.  Reporting here would surface the
    # MID-pipeline state (often 10×–100× worse than the final
    # output) and mislead.  The WARN is emitted from
    # ``build_airport_pavement`` after the smoother converges.


def _resample_node_altitudes_nn(
        new_poly: Polygon,
        old_open: list[tuple[float, float]],
        old_alts_closed: list[float] | None,
        interior_edge_project: bool = False,
        ) -> list[float] | None:
    """Given a new polygon (post geometry edit) and the OLD ring's
    open-form coords + closed-form altitudes, return a fresh
    ``node_altitudes`` list (closed) for ``new_poly``.

    For each new vertex, sample its altitude via:
      1. **Edge interpolation (preferred).**  Find the OLD edge that
         contains the new vertex (perpendicular distance ≤
         ``EDGE_TOL_M``).  Compute the parametric position ``t`` along
         that edge and linearly interpolate between the edge's two
         endpoint altitudes.  This is the correct sampling for
         vertices inserted by ``polygon.difference`` / ``buffer(0)``
         / boundary clip — they sit exactly on old edges by shapely's
         geometric guarantee, so the edge's linear gradient is the
         authoritative source.
      2. **Nearest-neighbour (fallback).**  When no old edge contains
         the new vertex (rare; happens for vertices inserted in the
         polygon interior or after a degenerate buffer(0) repair),
         fall back to NN against old-ring vertices.

    Per user 2026-05-13: pure NN historically produced jumpy
    altitude deltas at cut-edge vertices — two adjacent new vertices
    on the same old edge could pick *different* old endpoints as
    their nearest, fabricating a step that didn't exist in the
    pre-cut shape's smooth altitude field.  Edge interpolation
    preserves the original gradient.

    ``interior_edge_project`` (default ``False``) upgrades the pass-2
    fallback: instead of snapping a genuinely-interior new vertex to
    the nearest old *vertex* altitude, it projects the vertex onto the
    nearest old *edge* (perpendicular distance unbounded) and
    interpolates along that edge's endpoint altitudes.  Pure vertex-NN
    quantises an interior cut vertex to one ring corner, which on a
    sloped piece can be off by ``gradient x (distance to nearest
    corner)`` — metre-scale on a strongly-sloped, sparse-ring piece
    (see the tunnel graze-clip reproducer).  Edge projection recovers
    the boundary gradient value.  Off by default so every existing
    caller (boundary ribbon, tile-cut, sliver merge, …) stays
    byte-identical; only the tunnel graze-clip node_altitudes path
    opts in, where the interior vertices are buffered-gate cut points
    inside a possibly-sloped ramp/wall piece.

    Used wherever a polygon edit (boundary clip, buffer(0) repair,
    push-off, sliver merge, tile-cut, etc.) changes the vertex
    count and we would otherwise have to drop ``node_altitudes``.

    Returns None if the inputs are insufficient to resample.
    """
    if not old_alts_closed or not old_open:
        return None
    if new_poly is None or new_poly.is_empty:
        return None
    src_alts_open = (
        old_alts_closed[:-1]
        if (len(old_alts_closed) == len(old_open) + 1
            and old_alts_closed[0] == old_alts_closed[-1])
        else old_alts_closed[:len(old_open)])
    if not src_alts_open:
        return None
    try:
        new_open = list(new_poly.exterior.coords)
    except _GEOM_EXC:
        return None
    if new_open and new_open[0] == new_open[-1]:
        new_open = new_open[:-1]
    if not new_open:
        return None

    n_old = min(len(old_open), len(src_alts_open))
    EDGE_TOL_M = 0.5  # perpendicular distance for "on edge"
    EDGE_TOL_M2 = EDGE_TOL_M * EDGE_TOL_M

    new_alts: list[float] = []
    for nx, ny in new_open:
        # Pass 1: edge interpolation.
        best_edge_d2 = float("inf")
        best_edge_alt: float | None = None
        for k in range(n_old):
            sx, sy = old_open[k]
            tx, ty = old_open[(k + 1) % n_old]
            dx, dy = tx - sx, ty - sy
            seg_len2 = dx * dx + dy * dy
            if seg_len2 < 1e-9:
                continue
            t = ((nx - sx) * dx + (ny - sy) * dy) / seg_len2
            if t < -1e-3 or t > 1.0 + 1e-3:
                continue
            t = max(0.0, min(1.0, t))
            px, py = sx + t * dx, sy + t * dy
            d2 = (nx - px) ** 2 + (ny - py) ** 2
            if d2 > EDGE_TOL_M2 or d2 >= best_edge_d2:
                continue
            a_s = src_alts_open[k]
            a_t = src_alts_open[(k + 1) % n_old]
            best_edge_d2 = d2
            best_edge_alt = a_s + t * (a_t - a_s)
        if best_edge_alt is not None:
            new_alts.append(round(float(best_edge_alt), 1))
            continue

        # Pass 2a: interior edge-projection fallback (opt-in).  Project
        # onto the nearest old EDGE (perpendicular distance unbounded)
        # and interpolate along it, so an interior cut vertex recovers
        # the boundary gradient instead of snapping to one ring corner.
        if interior_edge_project:
            best_seg_d2 = float("inf")
            best_seg_alt: float | None = None
            for k in range(n_old):
                sx, sy = old_open[k]
                tx, ty = old_open[(k + 1) % n_old]
                dx, dy = tx - sx, ty - sy
                seg_len2 = dx * dx + dy * dy
                if seg_len2 < 1e-9:
                    continue
                t = ((nx - sx) * dx + (ny - sy) * dy) / seg_len2
                t = max(0.0, min(1.0, t))
                px, py = sx + t * dx, sy + t * dy
                d2 = (nx - px) ** 2 + (ny - py) ** 2
                if d2 >= best_seg_d2:
                    continue
                a_s = src_alts_open[k]
                a_t = src_alts_open[(k + 1) % n_old]
                best_seg_d2 = d2
                best_seg_alt = a_s + t * (a_t - a_s)
            if best_seg_alt is not None:
                new_alts.append(round(float(best_seg_alt), 1))
                continue

        # Pass 2b: nearest-neighbour fallback (interior vertex / no
        # containing edge).
        best_d2 = float("inf")
        best_a = src_alts_open[0]
        for k in range(n_old):
            sx, sy = old_open[k]
            d2 = (nx - sx) ** 2 + (ny - sy) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_a = src_alts_open[k]
        new_alts.append(round(float(best_a), 1))
    return new_alts + [new_alts[0]]



def _apply_geometric_finalization(
        layout: "PavementLayout",
        icao: str,
        dem,
        tile_lat: int,
        tile_lon: int,
        m_to_ll,
        ) -> None:
    """Run the geometric polygon-refinement passes interleaved
    with two unified-solver passes:

      Phase 1: pre-solve geometry
        * Push junction vertices off taxi-rect edge interiors.
        * Triangulate junctions (each replaced with N-2 ear-clip
          triangles, initial node_altitudes from corner-elev map
          + DEM).

      Phase 2: first unified-solver pass
        Establishes a grade-compliant elevation field on the
        existing geometry.  These are the elevations the
        clamp + subdivide passes use to detect grade violations.

      Phase 3: clamp + subdivide based on real elevations
        * ``_clamp_junction_free_vertices``: tighten free
          boundary vertices to grade-comply with neighbours.
        * ``_subdivide_violating_junctions``: split any junction
          whose worst-pair grade exceeds the subdivision
          threshold along a perpendicular cut.
        * Re-push junction vertices off rect edges (subdivision
          can add new vertices on rect edges).
        * Re-clamp.

      Phase 4: second unified-solver pass
        Re-solves elevations on the refined geometry — produces
        the final FAA-compliant elevation field.
    """
    # Phase 1: pre-solve geometry.
    from .geom_guard import coverage_probe as _covpg
    _covpg(layout, "gf-entry")
    _push_junction_vertices_off_taxi_rect_edges(layout)
    _covpg(layout, "gf-post-push")
    # The push/snap machinery is vertex-based; a junction EDGE grazing
    # past a rect/runway CORNER with no junction vertex nearby never
    # shares a node with the rect (the SPJC/HECA 0.6 m grade-gate
    # steps) — route the edge THROUGH the corner.
    _insert_rect_corners_into_grazing_junction_edges(layout)
    _covpg(layout, "gf-post-graze-insert")
    # Triangulate junctions — initial node_altitudes come from
    # the corner-elev map + DEM fallback.  These are placeholders
    # for the first unified-solver pass below.
    _triangulate_junctions(
        layout, dem, tile_lat, tile_lon, m_to_ll)
    _covpg(layout, "gf-post-triangulate")

    # Phase 2: first solver pass (real elevations on the
    # current geometry — clamp + subdivide need these to detect
    # grade violations, not DEM-noisy fallbacks).  Per user
    # 2026-05-03: when the per-surface solver is on, the legacy
    # clamp + subdivide chain is unnecessary (the unified Jacobi
    # converges to a grade-compliant field directly), and the
    # final solver pass at the END of build_airport_pavement
    # absorbs any geometry changes from junction-rule passes.
    # Skipping these here cuts build time roughly in half.
    if not USE_PER_SURFACE_SOLVER:
        _solve_pavement_elevations(
            layout, icao, dem=dem, tile_lat=tile_lat, tile_lon=tile_lon)

        # Phase 3: clamp + subdivide based on the real elevations.
        clamp_geom = _build_clamp_geom_state(layout)
        for _ in range(8):
            n = _clamp_junction_free_vertices(layout, clamp_geom)
            if n == 0:
                break
        for _ in range(4):
            n = _subdivide_violating_junctions(layout)
            if n == 0:
                break
        # Subdivision may introduce vertices on rect edge interiors.
        _push_junction_vertices_off_taxi_rect_edges(layout)
        clamp_geom = _build_clamp_geom_state(layout)
        for _ in range(4):
            n = _clamp_junction_free_vertices(layout, clamp_geom)
            if n == 0:
                break

        # Phase 4: second solver pass (final elevations on
        # refined geometry).
        _solve_pavement_elevations(
            layout, icao, dem=dem, tile_lat=tile_lat, tile_lon=tile_lon)




def _solve_pavement_elevations(
        layout: "PavementLayout", icao: str,
        dem=None, tile_lat: int = 0, tile_lon: int = 0) -> None:
    """Dispatcher: route to the per-surface or unified solver based
    on ``USE_PER_SURFACE_SOLVER``.  The per-surface solver requires
    a DEM + tile coords (passed via callers in
    ``_apply_geometric_finalization``).  When DEM args are missing,
    falls back to the unified solver.
    """
    if USE_PER_SURFACE_SOLVER and dem is not None:
        from .elevation_per_surface import solve as per_surface_solve
        per_surface_solve(layout, icao, dem, tile_lat, tile_lon)
        return
    _solve_pavement_elevations_unified(layout, icao)


def _solve_pavement_elevations_unified(
        layout: "PavementLayout",
        icao: str,
        max_iters: int = 1500,
        tol_m: float = 0.005,
        ) -> None:
    """Unified constrained-Laplacian elevation solver.

    Per user 2026-04-28: replaces the bottom-up DEM-driven
    pipeline (centerline graph + apron-pin + post-pin
    reconciliation + within-junction smoother) with a single
    top-down propagation:

      1. Hard anchors = every CIFP-derived RUNWAY corner (every
         segment in the runway chain has its altitude_high /
         altitude_low set from the FAA-compliant profile, and
         every corner of every segment counts as HARD).
      2. Build the unified pavement graph: every shape's polygon
         ring vertex becomes a node; ring edges + cross-shape
         shared-bucket edges connect them.
      3. Constrained Laplacian solve via Jacobi iteration with
         per-edge grade-cap projection:
           - Each iteration: every non-anchor node moves to the
             length-weighted average of its neighbours, then each
             edge with |Δelev|/length > max_grade pulls its
             endpoints toward each other (or moves the soft one
             toward the anchored one).
           - Per-shape role-specific grade caps (all 1.5 %; user
             2026-05-18 aligned the apron cap with the taxi cap):
               runway/runway: 1.5 % (already enforced by HARD)
               taxi rect / junction / apron / terminal: 1.5 %
               cross-shape: min of the two roles.
           - Terminal corners constrained to be FLAT (all corners
             of one terminal share a single value at every
             iteration).
      4. After convergence, apply the solved elevations back to
         every shape:
           - ROLE_RUNWAY: skip (HARD, already correct).
           - ROLE_PRIMARY_PARALLEL / SECONDARY_PARALLEL / STUB /
             CROSS_CONNECTOR: derive altitude_high/low from the
             rect's 4 corners (avg of corners 0,3 / corners 1,2).
           - ROLE_BUILDING: avg of all corners → altitude.
           - ROLE_JUNCTION: per-vertex node_altitudes from corner
             elevations.

    The architectural advantage: every step of the previous
    pipeline solves a different subset of the constraints, and
    they sometimes disagree (which is why we kept finding new
    edge cases).  This single solve handles all constraints
    simultaneously.

    Performance: O((V + E) × I) where I = iteration count.
    Typically I ≈ graph_diameter² × log(1/tol) — for CYXY
    (~30-hop diameter) ≈ 900 iterations; for HECA (~150-hop)
    ≈ 22 500.  Each iteration is a single sweep of the edge
    list, ~1 µs/edge; CYXY ≈ 0.5 s, HECA ≈ 5 s.  Compare to the
    old pipeline at ~3.5 s and ~30 s respectively.
    """
    import time as _time
    t_start = _time.time()
    # ── Build node list ─────────────────────────────────────────
    pavement_roles = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB,
        ROLE_CROSS_CONNECTOR, ROLE_BUILDING, ROLE_JUNCTION,
    }
    bucket_to_idx: dict[tuple[int, int], int] = {}
    nodes: list[tuple[float, float]] = []
    for s in layout.shapes:
        if s.role not in pavement_roles:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        for x, y in coords:
            b = _corner_elevation_bucket(x, y)
            if b not in bucket_to_idx:
                bucket_to_idx[b] = len(nodes)
                nodes.append((float(x), float(y)))
    n = len(nodes)
    if n == 0:
        return

    # ── Initial elevations + HARD anchor flags ──────────────────
    elev: list[float] = [0.0] * n
    is_hard: list[bool] = [False] * n
    have_initial: list[bool] = [False] * n

    # CIFP runway corners ⇒ HARD.
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            continue
        if (s.altitude_high is None or s.altitude_low is None):
            continue
        per = [s.altitude_high, s.altitude_low,
               s.altitude_low, s.altitude_high]
        for (x, y), a in zip(coords, per):
            b = _corner_elevation_bucket(x, y)
            if b in bucket_to_idx:
                idx = bucket_to_idx[b]
                # First-writer wins among runway corners (handles
                # adjacent segment shared corners — they should
                # already agree from the FAA profile, but pick
                # one canonically).
                if not is_hard[idx]:
                    elev[idx] = float(a)
                    is_hard[idx] = True
                    have_initial[idx] = True

    if not any(is_hard):
        # No CIFP anchors — can't run the unified solver.
        return

    # Seed soft nodes from their existing layout values when
    # available (gives the solver a warm start).
    for s in layout.shapes:
        if s.role not in pavement_roles or s.role == ROLE_RUNWAY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if (s.altitude_high is not None
                and s.altitude_low is not None
                and len(coords) == 4):
            per = [s.altitude_high, s.altitude_low,
                   s.altitude_low, s.altitude_high]
        elif s.altitude is not None:
            per = [float(s.altitude)] * len(coords)
        elif s.node_altitudes:
            per = [float(a) for a in
                   s.node_altitudes[:len(coords)]]
            if len(per) < len(coords):
                per = list(per) + [per[-1]] * (
                    len(coords) - len(per))
        else:
            continue
        for (x, y), a in zip(coords, per):
            b = _corner_elevation_bucket(x, y)
            if b not in bucket_to_idx:
                continue
            idx = bucket_to_idx[b]
            if is_hard[idx]:
                continue
            if not have_initial[idx]:
                elev[idx] = float(a)
                have_initial[idx] = True

    # Backfill any node still without an initial value via nearest
    # hard anchor's elevation (cheap pass).
    if any(not h for h in have_initial):
        hard_pts: list[tuple[float, float, float]] = [
            (nodes[i][0], nodes[i][1], elev[i])
            for i in range(n) if is_hard[i]]
        for i in range(n):
            if have_initial[i]:
                continue
            x, y = nodes[i]
            best_d2 = float("inf")
            best_e = 0.0
            for hx, hy, he in hard_pts:
                d2 = (hx - x) * (hx - x) + (hy - y) * (hy - y)
                if d2 < best_d2:
                    best_d2 = d2
                    best_e = he
            elev[i] = best_e
            have_initial[i] = True

    # ── Build edge list with per-edge max grade ────────────────
    # Edge identified by sorted (u, v); max_grade = min over
    # contributing shapes' role caps.
    edge_grade: dict[tuple[int, int], float] = {}
    edge_length: dict[tuple[int, int], float] = {}

    def _role_grade(role: str) -> float:
        if role == ROLE_RUNWAY:
            return TAXI_MAX_GRADE  # 1.5 %, never tighter than this
        if role in (ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
                     ROLE_STUB, ROLE_CROSS_CONNECTOR):
            return TAXI_MAX_GRADE
        # Terminal, junction, apron — apron rule.
        return APRON_MAX_GRADE

    # Per user 2026-04-29: in addition to ring-edge connectivity,
    # add a "spatial-pair" edge between every pair of vertices
    # WITHIN THE SAME SHAPE that are ≤ ``WITHIN_SHAPE_VIOLATION_
    # RADIUS_M`` apart in EUCLIDEAN distance.  The audit /
    # smoother both check spatial distance, not graph distance —
    # so without this, a junction with 100 ring vertices can have
    # vertex pair (i, j) that is 5 m apart spatially but 50 ring-
    # hops away.  The Laplacian's ring-edge cap then permits up
    # to 50 × per-edge cap of cumulative drift across the chain,
    # which the audit reports as a 100 % grade cliff.  Adding
    # the spatial pair as a direct edge constrains the pair the
    # same way the audit will check it.
    spatial_radius_m = WITHIN_SHAPE_VIOLATION_RADIUS_M
    spatial_radius2 = spatial_radius_m * spatial_radius_m
    for s in layout.shapes:
        if s.role not in pavement_roles:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) < 2:
            continue
        gr = _role_grade(s.role)
        m = len(coords)
        # Pre-compute this shape's vertex node indices so we can
        # cheaply emit ring + spatial pairs.
        node_idx: list[int | None] = []
        for x, y in coords:
            b = _corner_elevation_bucket(x, y)
            node_idx.append(bucket_to_idx.get(b))
        # Ring edges (i ↔ i+1).
        for i in range(m):
            ui = node_idx[i]
            uj = node_idx[(i + 1) % m]
            if ui is None or uj is None or ui == uj:
                continue
            x1, y1 = coords[i]
            x2, y2 = coords[(i + 1) % m]
            length = math.hypot(x2 - x1, y2 - y1)
            if length < 0.1:
                continue
            key = (ui, uj) if ui < uj else (uj, ui)
            cur_g = edge_grade.get(key, float("inf"))
            if gr < cur_g:
                edge_grade[key] = gr
            cur_l = edge_length.get(key, length)
            edge_length[key] = min(cur_l, length)
        # Spatial pairs (i, j) with j > i + 1 and Euclidean ≤
        # spatial_radius_m.  Skip pairs already connected as ring
        # edges (handled above) — the dict-min logic would just
        # repeat them.
        for i in range(m):
            xi, yi = coords[i]
            ui = node_idx[i]
            if ui is None:
                continue
            # Start at i+2 to skip the ring-adjacent pair that's
            # already added (and the i,i identity).  Treat the
            # ring-wrap pair (m-1, 0) as already covered too.
            for j in range(i + 2, m):
                # Skip the wrap-around ring edge.
                if i == 0 and j == m - 1:
                    continue
                uj = node_idx[j]
                if uj is None or ui == uj:
                    continue
                xj, yj = coords[j]
                dx = xj - xi
                dy = yj - yi
                d2 = dx * dx + dy * dy
                if d2 > spatial_radius2:
                    continue
                length = math.sqrt(d2)
                if length < 0.1:
                    continue
                key = (ui, uj) if ui < uj else (uj, ui)
                cur_g = edge_grade.get(key, float("inf"))
                if gr < cur_g:
                    edge_grade[key] = gr
                cur_l = edge_length.get(key, length)
                edge_length[key] = min(cur_l, length)

    # Adjacency for Jacobi step.
    adj: list[list[tuple[int, float, float]]] = [[] for _ in range(n)]
    for (u, v), gr in edge_grade.items():
        L = edge_length[(u, v)]
        adj[u].append((v, L, gr))
        adj[v].append((u, L, gr))

    # ── Per-shape constraint groups ────────────────────────────
    # Terminal corners — flat constraint (all share the same value).
    terminal_groups: list[list[int]] = []
    for s in layout.shapes:
        if s.role != ROLE_BUILDING:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        idxs: list[int] = []
        for x, y in coords:
            b = _corner_elevation_bucket(x, y)
            if b in bucket_to_idx:
                idxs.append(bucket_to_idx[b])
        if len(idxs) >= 2:
            terminal_groups.append(idxs)

    # ── Constrained Laplacian iteration ────────────────────────
    # Per user 2026-04-28: use a damped Jacobi + multi-sweep cap
    # projection to ensure the per-edge grade-cap wins over the
    # Jacobi neighbour pull.  Without damping, short edges with
    # strong external pull oscillate (Jacobi opens the gap, cap
    # closes it, Jacobi reopens it next iter).  With damping ~0.5
    # and multiple cap sweeps per Jacobi, the equilibrium settles
    # at the cap boundary as required.
    JACOBI_DAMPING = 0.5
    CAP_SWEEPS_PER_ITER = 5
    edge_list = list(edge_grade.keys())
    for it in range(max_iters):
        prev_elev = list(elev)
        # 1) Damped Jacobi neighbour-average step.
        new_elev = list(elev)
        for i in range(n):
            if is_hard[i]:
                continue
            nbrs = adj[i]
            if not nbrs:
                continue
            wsum = 0.0
            wvsum = 0.0
            for v, L, _g in nbrs:
                w = 1.0 / max(L, 0.1)
                wsum += w
                wvsum += w * elev[v]
            if wsum > 0:
                avg = wvsum / wsum
                new_elev[i] = (1.0 - JACOBI_DAMPING) * elev[i] \
                    + JACOBI_DAMPING * avg
        elev = new_elev
        # 2) Multi-sweep edge grade-cap projection.  Repeated
        # until either no edge violates or we hit the per-iter
        # sweep cap; this lets the cap propagate through chains
        # of edges in one outer step.
        for _sweep in range(CAP_SWEEPS_PER_ITER):
            any_proj = False
            for (u, v) in edge_list:
                L = edge_length[(u, v)]
                gr = edge_grade[(u, v)]
                diff = elev[u] - elev[v]
                cap = L * gr
                if abs(diff) <= cap:
                    continue
                excess = abs(diff) - cap
                sign = 1 if diff > 0 else -1
                if is_hard[u] and is_hard[v]:
                    continue
                if is_hard[u]:
                    elev[v] += sign * excess
                    any_proj = True
                elif is_hard[v]:
                    elev[u] -= sign * excess
                    any_proj = True
                else:
                    half = 0.5 * excess * sign
                    elev[u] -= half
                    elev[v] += half
                    any_proj = True
            if not any_proj:
                break
        # 3) Terminal flatness.
        for grp in terminal_groups:
            if not grp:
                continue
            free = [i for i in grp if not is_hard[i]]
            if not free:
                continue
            avg = sum(elev[i] for i in grp) / len(grp)
            for i in free:
                elev[i] = avg
        # 4) Convergence check.
        max_change = 0.0
        for i in range(n):
            if is_hard[i]:
                continue
            d = abs(prev_elev[i] - elev[i])
            if d > max_change:
                max_change = d
        if max_change < tol_m:
            break

    # ── Apply solved elevations back to layout shapes ──────────
    n_terms = 0
    n_rects = 0
    n_junctions = 0
    for s in layout.shapes:
        if s.role not in pavement_roles:
            continue
        if s.role == ROLE_RUNWAY:
            continue  # HARD, already correct
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        ring_closed = (coords and coords[0] == coords[-1])
        coords_open = coords[:-1] if ring_closed else coords
        corner_elevs: list[float] = []
        for x, y in coords_open:
            b = _corner_elevation_bucket(x, y)
            if b in bucket_to_idx:
                corner_elevs.append(elev[bucket_to_idx[b]])
            else:
                corner_elevs.append(float("nan"))
        if any(math.isnan(e) for e in corner_elevs):
            continue
        if s.role == ROLE_BUILDING:
            avg = sum(corner_elevs) / len(corner_elevs)
            s.altitude = round(float(avg), 1)
            n_terms += 1
        elif s.role in (ROLE_PRIMARY_PARALLEL,
                         ROLE_SECONDARY_PARALLEL,
                         ROLE_STUB, ROLE_CROSS_CONNECTOR):
            if len(corner_elevs) == 4:
                # Group the 4 corners into the two short-end pairs by
                # projecting onto source_axis (the rect's centerline).
                # Don't trust polygon vertex index — overlap-clip /
                # shared-vertex collapse can rotate the order, in
                # which case the legacy [0,3]/[1,2] grouping silently
                # averages across the slope direction and produces
                # hi ≈ lo (a sloping rect that looks flat).
                start_pair, end_pair = _short_end_pairs_by_axis(
                    coords_open, s.source_axis)
                if start_pair is None:
                    # Fall back to legacy index pairing.
                    start_pair, end_pair = (0, 3), (1, 2)
                a_avg = (corner_elevs[start_pair[0]]
                         + corner_elevs[start_pair[1]]) / 2
                b_avg = (corner_elevs[end_pair[0]]
                         + corner_elevs[end_pair[1]]) / 2
                hi, lo = (a_avg, b_avg) if a_avg >= b_avg else (b_avg, a_avg)
                s.altitude_high = round(float(hi), 1)
                s.altitude_low = round(float(lo), 1)
                s.altitude = None
                n_rects += 1
        elif s.role == ROLE_JUNCTION:
            alts = [round(float(e), 1) for e in corner_elevs]
            if ring_closed:
                alts.append(alts[0])
            s.node_altitudes = alts
            n_junctions += 1

    elapsed = _time.time() - t_start
    try:
        UI.vprint(1,
            f"  [pav-builder] {icao}: unified Laplacian solver "
            f"converged in {it + 1}/{max_iters} iters "
            f"({elapsed:.2f} s); applied to "
            f"{n_terms} terminal(s), {n_rects} rect(s), "
            f"{n_junctions} junction(s).")
    except _GEOM_EXC:
        pass




def _smooth_within_junction_adjacent_pair_grade(
        layout: "PavementLayout",
        max_grade: float = 0.015,
        max_iters: int = 30,
        convergence_m: float = 0.01,
        pair_radius_m: float = 60.0,
        ) -> int:
    """For each junction polygon, iterate over EVERY vertex pair
    within ``pair_radius_m`` (not just immediate ring neighbours).
    When a pair exceeds ``max_grade`` (default 1.5 %, matching the
    FAA taxiway cap), nudge the un-anchored vertex(es) toward the
    grade band.

    Per user 2026-04-28: corner alignment between junctions and
    sloping rects is already enforced by
    ``_snap_junction_altitudes_to_rect_corners`` and
    ``_enforce_shared_vertex_altitudes``, but interior junction
    vertices can still disagree with their immediate neighbours by
    > 1.5 % over short distances (1217 such pairs at CYXY, worst
    17.6 %).  These come from the multi-source apron pin step
    (DEM plane-fit ↔ taxi-graph ↔ apron-pin DEM-clipped-to-ring-
    grade) where adjacent vertices pull from different sources.
    Iterating an adjacent-pair smoother after all the bucket-
    averaging passes have converged the SHARED-vertex constraints
    is the cleanest way to flatten the remaining within-polygon
    grade humps.

    Vertex anchoring rules:
      * A vertex is HARD if its bucket coincides with a sloping
        rect corner (runway / primary_parallel / secondary_parallel
        / stub / cross_connector) — its altitude must equal the
        rect's tag value and CANNOT be modified.
      * Otherwise the vertex is SOFT and may move.

    Pair handling:
      * Both HARD ⇒ skip (constraint unsolvable here).
      * One HARD, one SOFT ⇒ move the soft one to the boundary
        of the hard one's grade band.
      * Both SOFT ⇒ average (preserves volume).

    Returns the number of altitude entries adjusted (cumulative
    across iterations).
    """
    sloping_rect_roles = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB,
        ROLE_CROSS_CONNECTOR,
    }
    # Hard-anchored buckets = sloping rect corner buckets.
    hard_buckets: set = set()
    for s in layout.shapes:
        if s.role not in sloping_rect_roles:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        for cx, cy in coords:
            hard_buckets.add(_corner_elevation_bucket(cx, cy))

    n_changed_total = 0
    for s in layout.shapes:
        if s.role != ROLE_JUNCTION:
            continue
        if not s.node_altitudes:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        n = len(coords)
        if n < 3 or len(s.node_altitudes) < n:
            continue
        alts = [float(a) for a in s.node_altitudes[:n]]
        # Pre-flag hard vertices.
        is_hard = [
            _corner_elevation_bucket(cx, cy) in hard_buckets
            for cx, cy in coords]
        # Pre-build pairs (i, j, distance) for every pair within
        # pair_radius_m.  For ring-adjacent pairs we always include
        # them (zero-distance edges between the same point are
        # filtered).  For non-adjacent pairs we filter by Euclidean
        # distance — distant pairs in the same polygon don't have
        # a meaningful grade constraint at airport scale.
        pair_radius2 = pair_radius_m * pair_radius_m
        pairs: list[tuple[int, int, float]] = []
        for i in range(n):
            for j in range(i + 1, n):
                ax, ay = coords[i]
                bx, by = coords[j]
                dx = bx - ax
                dy = by - ay
                d2 = dx * dx + dy * dy
                if d2 > pair_radius2:
                    continue
                d = math.sqrt(d2)
                if d < 0.5:
                    continue
                pairs.append((i, j, d))
        if not pairs:
            continue
        for _it in range(max_iters):
            max_change = 0.0
            for i, j, d in pairs:
                de = abs(alts[i] - alts[j])
                grade = de / d
                if grade <= max_grade:
                    continue
                # Compute the maximum permitted |Δalt| at this
                # separation.
                max_de = max_grade * d
                hi = max(alts[i], alts[j])
                lo = min(alts[i], alts[j])
                hi_idx = i if alts[i] >= alts[j] else j
                lo_idx = j if hi_idx == i else i
                if is_hard[i] and is_hard[j]:
                    # Both anchored — leave alone (the constraint
                    # is unresolvable without moving runway/rect
                    # tags).
                    continue
                if is_hard[hi_idx] and not is_hard[lo_idx]:
                    # Lift the soft (low) vertex up to the band's
                    # lower edge.
                    new_lo = hi - max_de
                    if abs(alts[lo_idx] - new_lo) > convergence_m:
                        max_change = max(
                            max_change,
                            abs(alts[lo_idx] - new_lo))
                        alts[lo_idx] = new_lo
                        n_changed_total += 1
                elif is_hard[lo_idx] and not is_hard[hi_idx]:
                    # Drop the soft (high) vertex down to the
                    # band's upper edge.
                    new_hi = lo + max_de
                    if abs(alts[hi_idx] - new_hi) > convergence_m:
                        max_change = max(
                            max_change,
                            abs(alts[hi_idx] - new_hi))
                        alts[hi_idx] = new_hi
                        n_changed_total += 1
                else:
                    # Both soft — split the violation evenly.
                    avg = (alts[i] + alts[j]) / 2.0
                    excess = (de - max_de) / 2.0
                    new_hi = avg + max_de / 2.0
                    new_lo = avg - max_de / 2.0
                    if abs(alts[hi_idx] - new_hi) > convergence_m:
                        max_change = max(
                            max_change,
                            abs(alts[hi_idx] - new_hi))
                        alts[hi_idx] = new_hi
                        n_changed_total += 1
                    if abs(alts[lo_idx] - new_lo) > convergence_m:
                        max_change = max(
                            max_change,
                            abs(alts[lo_idx] - new_lo))
                        alts[lo_idx] = new_lo
                        n_changed_total += 1
            if max_change < convergence_m:
                break
        # Write back, preserving the closed-ring duplicate at the end.
        s.node_altitudes = [round(a, 1) for a in alts]
        if (len(s.node_altitudes) == n
                and s.polygon.exterior.coords[0]
                == s.polygon.exterior.coords[-1]):
            s.node_altitudes.append(s.node_altitudes[0])
    return n_changed_total


def _rederive_terminal_altitude_from_apron_neighbours(
        layout: "PavementLayout",
        sample_radius_m: float = 250.0,
        ) -> int:
    """Re-derive each terminal's altitude from the HARD-anchored
    sloping rect corners (runway / taxi) within
    ``sample_radius_m`` of the terminal — NOT from DEM under the
    terminal pad and NOT from apron-pinned vertices (which are
    self-referential to the old terminal altitude).

    Per user 2026-04-28: at CYXY the terminal's DEM-median was
    700.8 m, but the apron actually borders runway 02 (694 m) on
    the south and taxiway F (~692 m) on the north.  The terminal
    sits on naturally elevated ground that the apron pavement
    doesn't reach.  Forcing terminal to its DEM altitude forced
    the apron-pin step to bridge a 7 m gap over short distances,
    producing 4-7 % grade violations.

    Sampling only HARD-anchored sloping rect corners (whose
    elevations come from CIFP runway thresholds + chained
    grade-compliant interpolation, NOT from the terminal) breaks
    the self-reference.  The median of those anchors gives a
    terminal altitude consistent with the apron's actual
    elevation range — the apron's grade then flattens naturally
    because all four boundary classes (terminal, runway, taxi,
    junction) end up within a few metres of each other.

    Returns the number of terminal altitudes changed.
    """
    sloping_rect_roles = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB,
        ROLE_CROSS_CONNECTOR,
    }
    hard_pts: list[tuple[float, float, float]] = []
    for s in layout.shapes:
        if s.role not in sloping_rect_roles:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            continue
        if (s.altitude_high is not None
                and s.altitude_low is not None):
            per = [s.altitude_high, s.altitude_low,
                   s.altitude_low, s.altitude_high]
        elif s.altitude is not None:
            per = [float(s.altitude)] * 4
        else:
            continue
        for (x, y), a in zip(coords, per):
            hard_pts.append((float(x), float(y), float(a)))
    if not hard_pts:
        return 0

    n_changed = 0
    radius2 = sample_radius_m * sample_radius_m
    for s in layout.shapes:
        if s.role != ROLE_BUILDING:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            t_boundary = s.polygon.boundary
        except _GEOM_EXC:
            t_boundary = None
        from shapely.geometry import Point as _P
        nearby: list[tuple[float, float]] = []  # (distance, elev)
        for px, py, pa in hard_pts:
            try:
                if t_boundary is not None:
                    d = t_boundary.distance(_P(px, py))
                else:
                    d = math.hypot(
                        px - s.polygon.centroid.x,
                        py - s.polygon.centroid.y)
            except _GEOM_EXC:
                continue
            if d * d > radius2:
                continue
            nearby.append((d, pa))
        if not nearby:
            continue
        # Median of the elevations weighted by inverse distance —
        # closer hard anchors carry more weight.  Equivalent to
        # "what elevation do the closest hard anchors agree on?"
        # Take the median for robustness against outliers (e.g. a
        # nearby runway-32L corner at 706 m on the OTHER side of
        # the airport that happens to fall within the radius via
        # straight-line distance).
        nearby.sort()
        # Use just the closest 6 anchors to anchor on local
        # terrain rather than the airport-wide elevation range.
        closest = nearby[:6] if len(nearby) > 6 else nearby
        elevs = sorted(e for _d, e in closest)
        median = elevs[len(elevs) // 2]
        new_alt = round(float(median), 1)
        old_alt = s.altitude
        if old_alt is None or abs(old_alt - new_alt) >= 0.05:
            try:
                UI.vprint(1,
                    f"  [pav-builder] terminal({s.ref or '?'}) "
                    f"altitude {old_alt} → {new_alt} m "
                    f"(median of {len(closest)} closest hard "
                    f"anchors within {sample_radius_m:.0f} m; "
                    f"replaces DEM-median).")
            except _GEOM_EXC:
                pass
            s.altitude = new_alt
            n_changed += 1
    return n_changed


def _enforce_shared_vertex_altitudes(
        layout: "PavementLayout") -> int:
    """For every vertex bucket shared by ≥ 2 shapes, force every
    polygon's per-vertex altitude at that bucket to a single
    canonical value.

    Per user 2026-04-28: junctions sharing a boundary node MUST
    agree on its altitude, otherwise X-Plane renders a tear / step
    at the seam.  Subdivide / clamp / shared-vertex passes can
    leave neighbouring junctions with sub-metre disagreement at
    shared buckets even when the underlying mesh value is
    consistent.

    Policy: take the AVERAGE of the disagreeing altitudes.  Skip
    sloping rect tags (altitude_high / altitude_low / altitude) —
    those are the authoritative source and were already aligned by
    ``_snap_junction_altitudes_to_rect_corners``.

    Returns the number of altitude entries adjusted.
    """
    # Gather per-bucket altitude votes from junction polygons only.
    # (Sloped rect altitudes are tag-level; junctions emit per-vertex.)
    bucket_to_entries: dict[tuple[int, int],
                            list[tuple[int, int, float]]] = {}
    for si, s in enumerate(layout.shapes):
        if s.role != ROLE_JUNCTION:
            continue
        if not s.node_altitudes:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        for vi, (cx, cy) in enumerate(coords):
            if vi >= len(s.node_altitudes):
                break
            b = _corner_elevation_bucket(cx, cy)
            bucket_to_entries.setdefault(b, []).append(
                (si, vi, float(s.node_altitudes[vi])))
    n_changed = 0
    for b, entries in bucket_to_entries.items():
        if len(entries) < 2:
            continue
        alts = [e[2] for e in entries]
        spread = max(alts) - min(alts)
        if spread < 0.05:
            continue
        avg = round(sum(alts) / len(alts), 1)
        for si, vi, _e in entries:
            shape = layout.shapes[si]
            if abs(shape.node_altitudes[vi] - avg) < 0.05:
                continue
            shape.node_altitudes[vi] = avg
            # Maintain closed-ring invariant: last == first.
            if (vi == 0 and len(shape.node_altitudes) >= 2):
                shape.node_altitudes[-1] = avg
            n_changed += 1
    return n_changed


def _snap_junction_altitudes_to_rect_corners(
        layout: "PavementLayout",
        interior_proximity_m: float = 1.0,
        ) -> int:
    """For every junction polygon, snap any vertex whose bucket
    coincides with a runway / sloping-rect corner to that rect's
    corresponding altitude tag value (``altitude_high`` for HIGH
    corners 0,3; ``altitude_low`` for LOW corners 1,2; ``altitude``
    for flat shapes).

    Per user 2026-04-29 (CYXY runway-32L ridge): also snap
    junction vertices that lie INSIDE a sloping-rect footprint
    (within ``interior_proximity_m`` of the rect's interior or
    edge) to the rect's INTERPOLATED altitude at that point.
    Without this, a runway-crossing junction polygon whose
    vertices land inside a runway rect can override the rect's
    smooth slope with mesh-interpolated values that are 4-5 m
    off — visible as a ridge crossing the runway surface.

    Without this pass, the smoothing / subdivision / clamping
    passes can leave a junction's ``node_altitudes`` entry at a
    value derived from mesh interpolation rather than the rect's
    EMITTED altitude tag — resulting in a vertical step at the
    shared corner where the rect tag and the junction's per-vertex
    altitude disagree.

    Returns the number of altitude entries adjusted.
    """
    # Key the corner-altitude map by canonical-point coordinates
    # (user 2026-05-18): the discrete ``_corner_elevation_bucket``
    # has a known bucket-boundary aliasing bug where two points
    # 0.002 m apart land in adjacent buckets and miss each other.
    # The shared registry's proximity lookup matches by physical
    # distance, identical to the solver's vertex matching.
    rwy_corner_alt: dict[tuple[float, float], float] = {}
    _reg = layout.canonical_points
    sloping_rect_roles_for_snap = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB,
        ROLE_CROSS_CONNECTOR,
        # Per user 2026-04-28: terminal corners also propagate
        # to apron / junction vertices at the same bucket.  The
        # terminal is FLAT at ``s.altitude`` and the apron must
        # match at every shared corner — otherwise X-Plane
        # renders a step where the apron meets the terminal pad.
        ROLE_BUILDING,
    }
    # Also collect the FULL sloping-rect shapes for interior-
    # snap (point-in-polygon + interpolated altitude).
    rect_shapes_for_interior: list[BuiltShape] = []
    for s in layout.shapes:
        if s.role not in sloping_rect_roles_for_snap:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        # Sloping rects (4-corner with altitude_high/low) — apply
        # per-corner altitudes.
        if (s.altitude_high is not None
                and s.altitude_low is not None
                and len(coords) == 4):
            for i, (cx, cy) in enumerate(coords):
                k = _reg.get_or_add(float(cx), float(cy))
                e = (s.altitude_high
                     if i in (0, 3)
                     else s.altitude_low)
                # First-writer wins — avoids different runway
                # segments at a shared corner disagreeing about
                # the canonical altitude.
                rwy_corner_alt.setdefault(k, float(e))
            rect_shapes_for_interior.append(s)
        elif s.altitude is not None:
            # Flat shapes (terminal pads, pre-elevation rects).
            # Any number of vertices; all share a single altitude.
            for (cx, cy) in coords:
                k = _reg.get_or_add(float(cx), float(cy))
                rwy_corner_alt.setdefault(k, float(s.altitude))
            rect_shapes_for_interior.append(s)
    if not rwy_corner_alt and not rect_shapes_for_interior:
        return 0
    # Spatial index for interior-snap probes.
    try:
        from shapely.strtree import STRtree as _STRtree
        if rect_shapes_for_interior:
            interior_tree = _STRtree(
                [s.polygon for s in rect_shapes_for_interior])
        else:
            interior_tree = None
    except _GEOM_EXC:
        interior_tree = None
    n_changed = 0
    for s in layout.shapes:
        if s.role != ROLE_JUNCTION:
            continue
        if not s.node_altitudes:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        # node_altitudes spans the closed ring; coords from
        # ``polygon.exterior.coords`` is also closed.  Walk the open
        # ring (drop closing repeat) and update by index.
        if coords and coords[0] == coords[-1]:
            coords_open = coords[:-1]
        else:
            coords_open = coords
        for i, (cx, cy) in enumerate(coords_open):
            if i >= len(s.node_altitudes):
                break
            # Pass 1: canonical-corner snap (prevents shared-corner
            # disagreement between junction and rect tags).
            k = _reg.get_or_add(float(cx), float(cy))
            target_e = rwy_corner_alt.get(k)
            if target_e is not None:
                if abs(s.node_altitudes[i] - target_e) >= 0.05:
                    s.node_altitudes[i] = round(target_e, 1)
                    n_changed += 1
                continue
            # Pass 2: interior snap — when the junction vertex
            # lies inside a sloping rect's polygon, snap to the
            # rect's interpolated altitude at that location.
            # Only adjust when the disagreement is > 0.5 m so we
            # don't undo the Laplacian solver's small refinements
            # at points that aren't truly inside a rect.
            if interior_tree is None:
                continue
            try:
                _Point = Point  # local alias
                pt = _Point(cx, cy)
                cands = interior_tree.query(pt)
            except _GEOM_EXC:
                cands = []
            best_e: float | None = None
            best_d2 = float("inf")
            for hit in cands:
                ri = int(hit) if hasattr(hit, "__int__") else hit
                if not isinstance(ri, int):
                    continue
                rs = rect_shapes_for_interior[ri]
                try:
                    if rs.polygon.distance(pt) > interior_proximity_m:
                        continue
                except _GEOM_EXC:
                    continue
                e = _sample_runway_segment_elev(rs, cx, cy)
                if e is None:
                    continue
                # Pick the rect whose centroid is closest (deals
                # with overlapping rect candidates — runway +
                # parallel + stub at a complex junction).
                try:
                    rcx = rs.polygon.centroid.x
                    rcy = rs.polygon.centroid.y
                    d2 = (rcx - cx) ** 2 + (rcy - cy) ** 2
                except _GEOM_EXC:
                    d2 = 0.0
                if d2 < best_d2:
                    best_d2 = d2
                    best_e = float(e)
            if best_e is None:
                continue
            if abs(s.node_altitudes[i] - best_e) >= 0.5:
                s.node_altitudes[i] = round(best_e, 1)
                n_changed += 1
        # Maintain closed-ring invariant: last == first.
        if (s.node_altitudes
                and len(s.node_altitudes) >= 2
                and s.node_altitudes[0] != s.node_altitudes[-1]):
            s.node_altitudes[-1] = s.node_altitudes[0]
    return n_changed


def _re_emit_apron_merged_runway_segments(
        layout: "PavementLayout",
        ) -> int:
    """Re-emit each apron-merged-and-dropped runway segment back
    into ``layout.shapes`` AFTER the elevation pipeline has
    finished assigning altitudes to surrounding apron junctions.
    Subtracts the re-emitted footprint from any overlapping
    junction polygon so the two don't conflict, with NN-resample
    of the junction's per-vertex altitudes after the clip.

    Per user 2026-04-29 (CYXY runway 32L/16R ridge): the
    builder drops runway segments that lie inside a much-larger
    apron polygon to avoid emitting a visible rectangular
    ribbon.  The surrounding apron junction then takes over the
    surface in that area — but the junction's altitudes come
    from the Laplacian solver pinning at non-runway corners, so
    the surface at the dropped-segment's footprint can sit 4-5 m
    above the runway's CIFP profile.  Reading along the runway,
    the rendered surface dips into the runway segment, rises
    over the apron-junction-covered void, then dips back into
    the next segment — the user's "ridge across runway".

    Re-emitting the dropped segment with its preserved
    ``altitude``/``altitude_high``/``altitude_low`` puts a real
    runway-altitude plate back into the output.  The
    surrounding apron junction is clipped (subtracted) so it no
    longer covers the runway footprint.  The runway surface is
    now continuous at its profile altitude through the absorbed
    area.

    Returns the number of segments re-emitted.
    """
    drops = list(getattr(
        layout, "_apron_merged_runway_drops", []) or [])
    if not drops:
        return 0
    try:
        from shapely.strtree import STRtree as _STRtree
        # Index the junction polygons so we know which to clip.
        jct_idxs = [i for i, s in enumerate(layout.shapes)
                     if s.role == ROLE_JUNCTION
                     and s.polygon is not None
                     and not s.polygon.is_empty]
        if jct_idxs:
            index = _STRtree([layout.shapes[i].polygon
                               for i in jct_idxs])
        else:
            index = None
    except _GEOM_EXC:
        index = None
        jct_idxs = []
    n_emitted = 0
    for seg in drops:
        if seg.polygon is None or seg.polygon.is_empty:
            continue
        # Subtract the segment's footprint from every junction
        # whose polygon overlaps it.  Use the raw segment polygon
        # (no buffer) so the clip is exactly the runway shape.
        if index is not None:
            try:
                cands = index.query(seg.polygon)
            except _GEOM_EXC:
                cands = []
            for hit in cands:
                ji = int(hit) if hasattr(hit, "__int__") else hit
                if not isinstance(ji, int):
                    continue
                shape_i = jct_idxs[ji]
                target = layout.shapes[shape_i]
                if (target.polygon is None
                        or target.polygon.is_empty):
                    continue
                try:
                    inter_area = target.polygon.intersection(
                        seg.polygon).area
                except _GEOM_EXC:
                    continue
                if inter_area < 1.0:
                    continue
                # Capture old ring + altitudes BEFORE clip.
                try:
                    _old_ring = list(
                        target.polygon.exterior.coords)
                except _GEOM_EXC:
                    _old_ring = []
                if _old_ring and _old_ring[0] == _old_ring[-1]:
                    _old_ring = _old_ring[:-1]
                _old_alts = (list(target.node_altitudes)
                              if target.node_altitudes else None)
                try:
                    new_poly = target.polygon.difference(
                        seg.polygon)
                except _GEOM_EXC:
                    continue
                if (new_poly.is_empty
                        or new_poly.geom_type
                        not in ("Polygon", "MultiPolygon")):
                    continue
                if new_poly.geom_type == "MultiPolygon":
                    new_poly = max(
                        (g for g in new_poly.geoms
                          if g.geom_type == "Polygon"),
                        key=lambda g: g.area, default=None)
                    if new_poly is None or new_poly.is_empty:
                        continue
                target.polygon = new_poly
                resampled = _resample_node_altitudes_nn(
                    new_poly, _old_ring, _old_alts)
                if resampled is not None:
                    target.node_altitudes = resampled
        # Re-add the runway segment.
        layout.shapes.append(seg)
        n_emitted += 1
    return n_emitted


def _latlon_to_m_local(lat: float, lon: float,
                       lat0: float, lon0: float, cos0: float
                       ) -> tuple[float, float]:
    x = math.radians(lon - lon0) * R_EARTH * cos0
    y = math.radians(lat - lat0) * R_EARTH
    return x, y


# Flat-profile tolerance for the single-poly runway ring — the same
# 5 cm FLAT_TOL the segment chain uses to consolidate consecutive flat
# samples into a MULTI_FLAT polygon (runway_segments.py emit loop), so
# a runway the legacy path emits flat also emits flat as one ring.
_SINGLE_POLY_FLAT_TOL_M = 0.05

# Legacy generate_patch_osm pads each runway side by RUNWAY_MARGIN=3 m
# for imagery coverage; both conversion paths (segmented chain + the
# single-poly ring) strip it to match the apt.dat width.
_LEGACY_RUNWAY_MARGIN_M = 3.0

# Runway-crossing slab bracket snap distance (``_single_poly_station_slab``):
# a slab edge snaps outward to a kept profile station only when that station
# is within this axial distance of the overlap edge, otherwise the slab is
# clamped to the overlap itself.  Small so a distant (sparse-profile) station
# never balloons the crossing junction across unstationed runway.
_XING_SLAB_SNAP_M = 5.0


def _build_single_poly_runway_ring(state: dict, lat0: float, lon0: float,
                                   cos0: float):
    """ONE runway polygon ring from the persisted FAA profile state
    (runway de-segmentation, O4_RUNWAY_SINGLE_POLY — docs/
    runway_single_polygon_plan.md).

    The ring runs the left long edge physical-end-A → physical-end-B
    with one vertex per profile sample station, then the right long
    edge back B → A at the same stations, so the two end edges are the
    former end cross-edges and there are NO interior cross-edges at
    all.  Per-vertex altitudes are the profile value at each station
    (both edges carry the profile; the crown drop field lowers edge
    nodes later, in solve space).  Stations are exactly the profile's
    sample list — physical ends + CIFP thresholds + pav_intersections
    + cross-runway / crossing-reconciliation anchors — so junction
    snap targets sit at the SAME positions the segment corners did.

    Returns ``(polygon, node_altitudes)`` with ``node_altitudes``
    including the closing repeat, or ``None`` when the geometry is
    degenerate — the caller then falls back to the legacy segmented
    chain for this ref.
    """
    phys_a = state.get('phys_end_a_ll')
    phys_b = state.get('phys_end_b_ll')
    fractions = state.get('fractions') or []
    elevs = state.get('elevs') or []
    width = state.get('patch_width_m')
    if (phys_a is None or phys_b is None or width is None
            or len(fractions) < 2 or len(fractions) != len(elevs)):
        return None
    ax, ay = _latlon_to_m_local(phys_a[0], phys_a[1], lat0, lon0, cos0)
    bx, by = _latlon_to_m_local(phys_b[0], phys_b[1], lat0, lon0, cos0)
    length = math.hypot(bx - ax, by - ay)
    if length < 1.0:
        return None
    width = max(1.0, float(width) - 2.0 * _LEGACY_RUNWAY_MARGIN_M)
    ux = (bx - ax) / length
    uy = (by - ay) / length
    px = -uy * width / 2.0
    py = ux * width / 2.0
    # Stations in axis order.  A station closer than SHARED_VERTEX_TOL_M
    # to its predecessor would mint coincident ring vertices (the
    # zero-length-edge class the to_osm guard collapses) — first wins,
    # except the final physical end always survives.
    stations: list[tuple[float, float]] = []
    for f, e in sorted(zip(fractions, elevs)):
        if stations and (f - stations[-1][0]) * length < SHARED_VERTEX_TOL_M:
            continue
        stations.append((float(f), float(e)))
    if stations and (fractions[-1] - stations[-1][0]) * length >= 1e-9:
        # The dedup above dropped the physical end B — put it back in
        # place of the too-close predecessor so the ring spans the full
        # physical extent.
        stations[-1] = (float(fractions[-1]), float(elevs[-1]))
    if len(stations) < 2:
        return None
    ring: list[tuple[float, float]] = []
    alts: list[float] = []
    for f, e in stations:
        x = ax + f * (bx - ax)
        y = ay + f * (by - ay)
        ring.append((x + px, y + py))
        alts.append(round(e, 2))
    for f, e in reversed(stations):
        x = ax + f * (bx - ax)
        y = ay + f * (by - ay)
        ring.append((x - px, y - py))
        alts.append(round(e, 2))
    try:
        poly = Polygon(ring)
    except _GEOM_EXC:
        return None
    if (not poly.is_valid or poly.is_empty
            or poly.geom_type != "Polygon"
            or len(poly.exterior.coords) != len(ring) + 1):
        # A repaired/cleaned ring would break the vertex↔altitude
        # correspondence — fall back to the segmented path instead.
        return None
    geometry = {
        'axis_a': (ax, ay), 'axis_b': (bx, by),
        'length_m': length, 'width_m': width,
        'unit': (ux, uy), 'perp': (px, py),
        'stations': stations,          # kept (fraction, elev) pairs
        'fractions': list(fractions),  # full profile sample list
        'elevs': list(elevs),
    }
    return poly, alts + [alts[0]], geometry


def _single_poly_profile_alt(geometry: dict, x: float, y: float) -> float:
    """Profile value at the axis projection of ``(x, y)`` — the same
    linear interpolation ``runway_redistribute._interp_profile`` uses,
    against the ring's full profile sample list."""
    ax, ay = geometry['axis_a']
    ux, uy = geometry['unit']
    length = geometry['length_m']
    t = ((x - ax) * ux + (y - ay) * uy) / length
    fractions = geometry['fractions']
    elevs = geometry['elevs']
    if t <= fractions[0]:
        return float(elevs[0])
    if t >= fractions[-1]:
        return float(elevs[-1])
    for k in range(len(fractions) - 1):
        f0, f1 = fractions[k], fractions[k + 1]
        if f0 <= t <= f1:
            if f1 - f0 < 1e-12:
                return float(elevs[k])
            u = (t - f0) / (f1 - f0)
            return float(elevs[k] + u * (elevs[k + 1] - elevs[k]))
    return float(elevs[-1])


def _single_poly_station_slab(geometry: dict, overlap) -> Polygon | None:
    """The ring's axis-aligned SLAB spanning ``overlap`` — the de-seg
    equivalent of "the sub-rects of this ref that overlap the other
    runway" (the legacy crossing junction is the union of those;
    ``_resolve_runway_crossings`` pass 2).

    The slab is bracketed to the overlap's OWN axial extent, snapping a
    bracket edge OUTWARD to a kept station only when that station is
    within ``_XING_SLAB_SNAP_M`` of the overlap edge (so the cut reuses
    an existing ring vertex when one is right there, no near-duplicate
    sliver).  It must NOT snap to a DISTANT station: the profile sample
    list is sparse (physical ends + a few crossing anchors), so a runway
    whose only stations below the crossing is the physical end ``0.0``
    would balloon the slab across the ENTIRE unstationed runway span —
    the crossing junction then inherited each runway's full profile
    range (KBNA 02L/20R+13/31: the slab reached the 182.6 m 02L
    threshold and the 169.5 m 20R end, 1.4 km from the crossing, and the
    junction's concave-corner vertices jumped 8.6 m / 731 % between the
    two runways' extremes).  A cut at a non-station fraction is safe:
    ``poly.difference(slab)`` mints a coincident vertex on the remainder
    ring at the same coordinate, and both sides re-sample
    ``_single_poly_profile_alt`` there → identical value, no step and no
    T-vert.  Coverage is preserved because the overlap projects entirely
    inside ``[t_lo, t_hi]`` on this axis, so the slab still contains the
    whole physical crossing."""
    ax, ay = geometry['axis_a']
    bx, by = geometry['axis_b']
    ux, uy = geometry['unit']
    px, py = geometry['perp']
    length = geometry['length_m']
    ts = []
    try:
        coords = list(overlap.exterior.coords)
    except _GEOM_EXC:
        return None
    for x, y in coords:
        ts.append(((x - ax) * ux + (y - ay) * uy) / length)
    if not ts:
        return None
    t_lo, t_hi = min(ts), max(ts)
    station_fractions = [f for f, _e in geometry['stations']]
    snap = _XING_SLAB_SNAP_M / length if length > 0 else 0.0
    # Snap the low edge DOWN to a station only if one sits within ``snap``
    # just below the overlap; otherwise clamp to the overlap edge itself.
    below = [f for f in station_fractions if f <= t_lo + 1e-9]
    f_lo = max(below) if below and (t_lo - max(below)) <= snap else t_lo
    above = [f for f in station_fractions if f >= t_hi - 1e-9]
    f_hi = min(above) if above and (min(above) - t_hi) <= snap else t_hi
    # Never exceed the ring's physical extent.
    f_lo = max(f_lo, station_fractions[0])
    f_hi = min(f_hi, station_fractions[-1])
    if f_hi - f_lo < 1e-9:
        return None

    def _at(f):
        return (ax + f * (bx - ax), ay + f * (by - ay))

    lo_x, lo_y = _at(f_lo)
    hi_x, hi_y = _at(f_hi)
    try:
        slab = Polygon([
            (lo_x + px, lo_y + py), (hi_x + px, hi_y + py),
            (hi_x - px, hi_y - py), (lo_x - px, lo_y - py)])
    except _GEOM_EXC:
        return None
    if slab.is_empty or not slab.is_valid:
        return None
    return slab


def _carve_single_poly_crossings(candidates: dict, sites: list):
    """Carve runway-runway crossings out of single-poly rings.

    ``sites`` = [(ref_a, ref_b, overlap_polygon)] for pairs whose
    physical axes intersect.  Returns ``(crossing_specs, ring_pieces)``:
    crossing_specs = [(polygon, closed_alts, combined_ref)] to emit as
    ROLE_RUNWAY_CROSSING; ring_pieces = {ref: [(polygon, closed_alts)]}
    replacing the carved refs' whole rings.  A ref not in ring_pieces
    keeps its original ring.  Mirrors the legacy resolution: crossing
    polygon = union of both refs' station slabs over the overlap;
    per-vertex altitudes = inverse-distance blend of the member
    profiles; member surface between the bracketing stations is
    REPLACED by the crossing junction."""
    if not sites:
        return [], {}
    merged: list[tuple[set, Polygon]] = []
    for ref_a, ref_b, overlap in sites:
        slab_a = _single_poly_station_slab(candidates[ref_a][2], overlap)
        slab_b = _single_poly_station_slab(candidates[ref_b][2], overlap)
        if slab_a is None or slab_b is None:
            continue
        try:
            site_poly = unary_union([slab_a, slab_b])
        except _GEOM_EXC:
            continue
        refs = {ref_a, ref_b}
        placed = False
        for k, (mrefs, mpoly) in enumerate(merged):
            try:
                touches = site_poly.intersects(mpoly)
            except _GEOM_EXC:
                touches = False
            if touches:
                merged[k] = (mrefs | refs,
                             unary_union([mpoly, site_poly]))
                placed = True
                break
        if not placed:
            merged.append((refs, site_poly))

    crossing_specs = []
    carve_by_ref: dict = {}
    for mrefs, mpoly in merged:
        if mpoly.geom_type != "Polygon":
            parts = [g for g in getattr(mpoly, "geoms", [])
                     if g.geom_type == "Polygon"]
            parts.sort(key=lambda p: -p.area)
            if not parts:
                continue
            mpoly = parts[0]
        try:
            coords = list(mpoly.exterior.coords)[:-1]
        except _GEOM_EXC:
            continue
        if len(coords) < 3:
            continue
        # Inverse-distance blend of member profiles (legacy parity:
        # the vertex on one runway's edge takes ~100% of that
        # runway's profile; the seam between regimes is smooth).
        ring_alts: list[float] = []
        ok = True
        for cx, cy in coords:
            from shapely.geometry import Point as _Pt
            pt = _Pt(cx, cy)
            weighted_sum = 0.0
            weight_sum = 0.0
            for ref in mrefs:
                poly_ref, _alts, geom_ref = candidates[ref]
                try:
                    d = poly_ref.distance(pt)
                except _GEOM_EXC:
                    continue
                e = _single_poly_profile_alt(geom_ref, cx, cy)
                d_eff = max(d, 0.1)
                w = 1.0 / (d_eff ** 2)
                weighted_sum += e * w
                weight_sum += w
            if weight_sum <= 0:
                ok = False
                break
            ring_alts.append(round(weighted_sum / weight_sum, 1))
        if not ok:
            continue
        crossing_specs.append(
            (mpoly, ring_alts + [ring_alts[0]],
             "+".join(sorted(mrefs))))
        for ref in mrefs:
            carve_by_ref.setdefault(ref, []).append(mpoly)

    ring_pieces: dict = {}
    for ref, carve_polys in carve_by_ref.items():
        poly_ref, _alts, geom_ref = candidates[ref]
        try:
            remainder = poly_ref.difference(unary_union(carve_polys))
        except _GEOM_EXC:
            continue
        parts = ([remainder] if remainder.geom_type == "Polygon"
                 else [g for g in getattr(remainder, "geoms", [])
                       if g.geom_type == "Polygon"])
        pieces = []
        for part in parts:
            if part.is_empty or part.area < 5.0:
                continue
            try:
                part_coords = list(part.exterior.coords)[:-1]
            except _GEOM_EXC:
                continue
            alts = [round(_single_poly_profile_alt(geom_ref, x, y), 2)
                    for (x, y) in part_coords]
            if len(alts) < 3:
                continue
            pieces.append((part, alts + [alts[0]]))
        if pieces:
            ring_pieces[ref] = pieces
    return crossing_specs, ring_pieces


def _orient_rect_for_altitude(shape: "BuiltShape",
                              p1: tuple[float, float],
                              p2: tuple[float, float],
                              e1: float, e2: float) -> None:
    """Rewrite a 4-corner rect polygon's ring in the X-Plane
    patch convention:

        [n0 high-left, n1 low-left, n2 low-right, n3 high-right]

    where "high" is whichever of ``p1`` / ``p2`` has the larger
    elevation (``e1`` / ``e2``) and "left" / "right" are
    relative to the high→low axis direction.  The way's short
    edges are then:

        way[-2:] = [n3, n0]  = altitude_high short edge
        way[1:3] = [n1, n2]  = altitude_low  short edge

    Earlier pipeline stages (corner snap to pav vertices,
    shared-vertex enforcement) may have permuted the polygon's
    ring order, so this function re-derives the ordering from
    the 4 raw corner positions by classifying each by nearest
    axis endpoint and by left/right of the axis perpendicular.
    Non-4-corner polygons are left alone.
    """
    try:
        coords = list(shape.polygon.exterior.coords)
    except _GEOM_EXC:
        return
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) != 4:
        return
    # Classify each corner by nearest axis endpoint.
    p1_corners: list[tuple[float, float]] = []
    p2_corners: list[tuple[float, float]] = []
    for c in coords:
        d1 = (c[0] - p1[0]) ** 2 + (c[1] - p1[1]) ** 2
        d2 = (c[0] - p2[0]) ** 2 + (c[1] - p2[1]) ** 2
        (p1_corners if d1 <= d2 else p2_corners).append(c)
    if len(p1_corners) != 2 or len(p2_corners) != 2:
        return
    # Perpendicular for the HIGH→LOW walk direction.
    if e1 >= e2:
        hi_p, lo_p = p1, p2
        hi_corners, lo_corners = p1_corners, p2_corners
    else:
        hi_p, lo_p = p2, p1
        hi_corners, lo_corners = p2_corners, p1_corners
    dx = lo_p[0] - hi_p[0]
    dy = lo_p[1] - hi_p[1]
    ax_len = math.hypot(dx, dy)
    if ax_len < 0.1:
        return
    ux, uy = dx / ax_len, dy / ax_len
    # Left perp when walking high→low = (-uy, ux).
    def _side(c, ref):
        """Positive = left of axis from HIGH end looking LOW."""
        rx, ry = c[0] - ref[0], c[1] - ref[1]
        return rx * (-uy) + ry * ux
    # Sort each endpoint's 2 corners: left first.
    hi_corners = sorted(hi_corners, key=lambda c: -_side(c, hi_p))
    lo_corners = sorted(lo_corners, key=lambda c: -_side(c, lo_p))
    hi_left, hi_right = hi_corners[0], hi_corners[1]
    lo_left, lo_right = lo_corners[0], lo_corners[1]
    # Build ring in legacy convention: high-left → low-left → low-right → high-right.
    new_ring = [hi_left, lo_left, lo_right, hi_right, hi_left]
    try:
        new_poly = Polygon(new_ring, list(shape.polygon.interiors))
        if not new_poly.is_valid:
            new_poly = new_poly.buffer(0)
        if (new_poly.geom_type == "Polygon"
                and not new_poly.is_empty):
            shape.polygon = new_poly
    except _GEOM_EXC:
        pass



# ──────────────────────────────────────────────────────────────────
# Junction-polygon decomposition + densification
# (re-exported from O4_Pavement_Junctions)
# ──────────────────────────────────────────────────────────────────
from .pavement.junctions import (
    _decompose_polygon_with_holes,
    _drop_sliver_corners,
    _merge_thin_decomposed_pieces,
    _polygon_area,
    _polygon_min_thickness,
    _splice_holes,
    _splice_one_hole,
)



def _planar_fit(ring: list[tuple[float, float]],
                elev: list[float]
                ) -> tuple[float, float, float, list[float]] | None:
    """Fit a plane ``z = a*x + b*y + c`` to (x, y, z) by least
    squares and return ``(a, b, c, per-vertex residuals)``.  The
    slope magnitude is ``sqrt(a² + b²)`` (rise per metre of horizontal
    travel — directly comparable to ``TAXI_MAX_GRADE``).
    Returns None if the fit is degenerate (colinear xy).
    """
    n = len(ring)
    if n < 3 or len(elev) != n:
        return None
    sxx = sxy = sxc = syy = syc = scc = 0.0
    sxz = syz = szc = 0.0
    for (x, y), z in zip(ring, elev):
        sxx += x * x
        sxy += x * y
        sxc += x
        syy += y * y
        syc += y
        scc += 1.0
        sxz += x * z
        syz += y * z
        szc += z
    det = (sxx * (syy * scc - syc * syc)
           - sxy * (sxy * scc - syc * sxc)
           + sxc * (sxy * syc - syy * sxc))
    if abs(det) < 1e-9:
        return None
    det_a = (sxz * (syy * scc - syc * syc)
             - sxy * (syz * scc - syc * szc)
             + sxc * (syz * syc - syy * szc))
    det_b = (sxx * (syz * scc - syc * szc)
             - sxz * (sxy * scc - syc * sxc)
             + sxc * (sxy * szc - syz * sxc))
    det_c = (sxx * (syy * szc - syz * syc)
             - sxy * (sxy * szc - syz * sxc)
             + sxz * (sxy * syc - syy * sxc))
    a = det_a / det
    b = det_b / det
    c = det_c / det
    residuals = [abs(z - (a * x + b * y + c))
                 for (x, y), z in zip(ring, elev)]
    return (a, b, c, residuals)


def _planar_fit_residuals(ring: list[tuple[float, float]],
                          elev: list[float]
                          ) -> list[float] | None:
    """Backwards-compat wrapper: residuals only."""
    f = _planar_fit(ring, elev)
    return None if f is None else f[3]


def _match_elev(rx: float, ry: float,
                ring: list[tuple[float, float]],
                elev: list[float]) -> float:
    """Find the elevation in ``elev`` whose corresponding ring
    vertex is closest to (rx, ry).  Used to map shapely-emitted
    closed-ring coords back to our smoothed elevation array."""
    best_e = elev[0]
    best_d2 = float("inf")
    for (x, y), e in zip(ring, elev):
        d2 = (rx - x) * (rx - x) + (ry - y) * (ry - y)
        if d2 < best_d2:
            best_d2 = d2
            best_e = e
    return best_e




# ── Mode B: per-polygon 2D elevation grid ─────────────────────────
#
# Build a 5 m grid covering a non-rect pavement polygon's bbox,
# pin cells nearest each "hard anchor" (rect/runway/terminal corner
# elevations) to those anchor values, initialize free cells to DEM
# clipped into the local feasibility cone (anchor ± dist × 1.5 %),
# then Laplacian-smooth + grade-cap every adjacent cell pair until
# the field is stable.  Returns a sampler that bilinearly
# interpolates the smoothed grid at any (x, y) the caller asks
# about.  The polygon's boundary vertices then take their
# elevations from this single shared field — which is what
# guarantees within-shape grade compliance for the polygon.
#
# Per the elevation-field plan (2026-04-26) this replaces per-
# vertex independent elevation derivation.  Rect / runway / terminal
# corner anchors stay immutable across smoothing iterations.



# ──────────────────────────────────────────────────────────────────
# 2D polygon-grid smoothing (extracted to auto_patch.elevation_smoothing)
# ──────────────────────────────────────────────────────────────────
from .elevation_smoothing import _smooth_polygon_grid

def _short_end_pairs_by_axis(
        coords_open: Sequence[tuple[float, float]],
        source_axis,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Group a 4-corner rect ring into its two short-end vertex pairs
    by projecting each corner onto ``source_axis``.

    The two corners with the smallest parametric position form one
    short end; the two largest form the other.  Returns ``(start_pair,
    end_pair)`` as 0-based index tuples into ``coords_open``, or
    ``(None, None)`` if ``source_axis`` is unusable (missing or
    zero-length).

    Used by the Laplacian-solver writeback to set
    ``altitude_high``/``altitude_low`` from the actual rect geometry,
    independent of polygon vertex index order — which can be rotated
    by overlap-clip / shared-vertex collapse.
    """
    if source_axis is None or source_axis.is_empty:
        return None, None
    if len(coords_open) != 4:
        return None, None
    ax_pts = list(source_axis.coords)
    if len(ax_pts) < 2:
        return None, None
    ax_start = ax_pts[0]
    ax_end = ax_pts[-1]
    axdx = ax_end[0] - ax_start[0]
    axdy = ax_end[1] - ax_start[1]
    ax_len2 = axdx * axdx + axdy * axdy
    if ax_len2 < 1e-9:
        return None, None
    ts = []
    for x, y in coords_open:
        t = ((x - ax_start[0]) * axdx
             + (y - ax_start[1]) * axdy) / ax_len2
        ts.append(t)
    order = sorted(range(4), key=lambda i: ts[i])
    return (order[0], order[1]), (order[2], order[3])


def _corner_elevation_bucket(x: float, y: float,
                             tol: float = SHARED_VERTEX_TOL_M
                             ) -> tuple[int, int]:
    """Quantize a meter-space point to a vertex-bucket key.

    Thin wrapper over ``layout.vertex_bucket`` (the single source of
    truth for discrete vertex bucketing).  Kept as a named alias so
    the ~20 existing call sites don't churn.
    """
    return vertex_bucket(x, y, tol)


def _corner_elev_map(layout: "PavementLayout"
                     ) -> dict[tuple[float, float], float]:
    """Return a canonical-point-keyed elevation lookup for every
    corner of every elevation-bearing non-junction shape.  Per
    user 2026-05-18: route through ``layout.canonical_points`` so
    junction vertex lookups use the same proximity-based matching
    as the solver, eliminating the bucket-boundary aliasing where
    two points 0.002 m apart land in adjacent buckets and miss.

    For sloped rect/runway shapes (altitude_high+altitude_low),
    ring indices 0,3 are the HIGH short edge and 1,2 are the LOW
    short edge — see ``_orient_rect_for_altitude``.  For flat
    polygons (altitude only), every corner gets the single value.
    """
    rect_like_roles = {ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
                       ROLE_SECONDARY_PARALLEL,
                       ROLE_STUB, ROLE_CROSS_CONNECTOR}
    out: dict[tuple[float, float], float] = {}
    reg = layout.canonical_points
    for s in layout.shapes:
        if s.role == ROLE_JUNCTION:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if not coords:
            continue
        # Sloped 4-corner rect/runway in patch convention.
        if (s.role in rect_like_roles
                and s.altitude_high is not None
                and s.altitude_low is not None
                and len(coords) == 4):
            elevs = [s.altitude_high, s.altitude_low,
                     s.altitude_low, s.altitude_high]
            for (cx, cy), e in zip(coords, elevs):
                out.setdefault(
                    reg.get_or_add(float(cx), float(cy)),
                    float(e))
            continue
        # Flat polygon (terminal, flat rect, or flat runway).
        if s.altitude is not None:
            for (cx, cy) in coords:
                out.setdefault(
                    reg.get_or_add(float(cx), float(cy)),
                    float(s.altitude))
    return out



# ──────────────────────────────────────────────────────────────────
# Per-vertex junction altitude assignment
# (extracted to auto_patch.triangulation)
# ──────────────────────────────────────────────────────────────────
from .triangulation import _triangulate_junctions


# ──────────────────────────────────────────────────────────────────
# Junction-polygon elevation repair
# (extracted to auto_patch.junction_repair)
# ──────────────────────────────────────────────────────────────────
from .junction_repair import (
    SUBDIVIDE_MAX_PAIR_DIST_M,
    SUBDIVIDE_MIN_AREA_M2,
    SUBDIVIDE_SNAP_RADIUS_M,
    SUBDIVIDE_VIOLATION_GRADE,
    _build_clamp_geom_state,
    _clamp_junction_free_vertices,
    _drop_thin_orphan_slivers,
    _merge_sliver_junctions_into_neighbours,
    _split_sloped_rects_at_violations,
    _subdivide_violating_junctions,
)

def _report_within_shape_violations(
        layout: "PavementLayout", icao: str) -> None:
    """Audit + WARN summary for within-shape grade violations.

    Per user 2026-05-03: the old audit used ``TAXI_MAX_GRADE``
    (1.5 %) for every shape and a 60 m Euclidean radius cap.  Both
    were wrong for the per-surface elevation pipeline:

    * The user's rule is "any direction", not "within 60 m" —
      drop the radius cap.
    * Audit EVERY role with a non-``None`` ``ROLE_GRADE_LIMITS``
      cap (taxi / apron / junction / runway-class rects = 1.5 %),
      not just apron + junction.  RECTS are *supposed* to slope
      only along their source_axis at ≤ cap, but the solver can
      assign an ``altitude_high`` / ``altitude_low`` pair whose
      slope across the rect exceeds the cap (e.g. a short rect
      shedding the full Δ over its narrow span).  That is a real
      grade violation the test validator (``tools/check_grade.py``
      ``_check_within_shape``) counts — so the WARN must count it
      too, or the runtime self-report silently disagrees with the
      gate.  A *compliant* rect produces no false positive: the
      slope-axis pairs sit exactly at the cap and the diagonals
      come in UNDER it (longer chord, same Δ).

    Apply a 0.10 m absolute rounding allowance (altitudes are
    stored to 0.1 m precision; the per-pair noise envelope is
    twice that worst-case).
    """
    if not layout.shapes:
        return
    # UNIFIED grade-graph audit of apron/junction (spine + body) — the single
    # source the solver used.  Reported separately so the spine (taxi route)
    # smoothness is visible directly, no parallel probe needed.
    try:
        from .grade_graph_validate import within_violations as _gg_within
        gg_viol = _gg_within(layout)
    except Exception:
        gg_viol = []
    if gg_viol:
        spine_n = sum(1 for v in gg_viol if v[4])
        body_n = len(gg_viol) - spine_n
        UI.vprint(1,
                  f"  [pav-builder] WARN: {icao}: {len(gg_viol)} apron/junction "
                  f"within-grade violation(s) [unified grade graph] — "
                  f"SPINE(taxi-route)={spine_n}, BODY(apron)={body_n}.")
        for (pct, cap, d, role, is_spine, x, y) in gg_viol[:8]:
            UI.vprint(1,
                      f"  [pav-builder]   {'SPINE' if is_spine else 'body '} "
                      f"{pct:.1f}% on {role} cap={cap:.1f}% d={d:.1f}m "
                      f"@({x:.0f},{y:.0f})")
    # ROUTE-REACH: a no-building apron whose feeding taxiways arrive at mutually
    # unreachable elevations (so it cannot get a single reachable base level).
    try:
        from .grade_graph_validate import route_reach_violations as _gg_reach
        reach_viol = _gg_reach(layout)
    except Exception:
        reach_viol = []
    if reach_viol:
        UI.vprint(1,
                  f"  [pav-builder] WARN: {icao}: {len(reach_viol)} ROUTE-REACH "
                  f"violation(s) — a no-building apron's feeder taxiways arrive "
                  f"at mutually unreachable elevations (no single reachable base).")
        for (pct, cap, d, role, _sp, x, y) in reach_viol[:8]:
            UI.vprint(1,
                      f"  [pav-builder]   route-reach {pct:.2f}% (cap {cap:.0f}%) "
                      f"over {d:.0f}m @({x:.0f},{y:.0f})")
    # ROUTE-BAND: every airside vertex must sit inside the runway-reach band on
    # THE unified graph G (``reach_band_unified``) — the AS-BUILT confirmation of
    # the bound the solver enforces, on the SAME graph (replaces the retired
    # route_field per-vertex band on a separate centerline graph).
    try:
        from .grade_graph_validate import route_band_violations as _gg_band
        band_viol = _gg_band(layout)
    except Exception:
        band_viol = []
    if band_viol:
        from collections import Counter as _Counter
        cls = _Counter(v[1] for v in band_viol)
        UI.vprint(1,
                  f"  [pav-builder] WARN: {icao}: {len(band_viol)} ROUTE-BAND "
                  f"violation(s) — airside vertex outside the runway-reach band "
                  f"[unified graph G] (ceil/too-high={cls['ceil']}, "
                  f"floor/too-low={cls['floor']}, "
                  f"pinned/no-feasible-band={cls['pinned']}).")
        for (ex, side, role, x, y, e, lo, hi) in band_viol[:8]:
            UI.vprint(1,
                      f"  [pav-builder]   route-band {side} {ex:.1f}m on {role} "
                      f"elev={e:.1f} band=[{lo:.1f},{hi:.1f}] @({x:.0f},{y:.0f})")




WITHIN_SHAPE_VIOLATION_RADIUS_M = 60.0   # spatial-pair edge radius for the
                                          # LEGACY Laplacian solver only
                                          # (inactive under the per-surface
                                          # solver).  NOT the within-shape
                                          # audit, which is now uncapped +
                                          # visibility-gated (see
                                          # _report_within_shape_violations).


SHARED_VERTEX_CLUSTER_TOL_M = 1.5


def _drop_overlap_against_fixed_shapes(
        layout: "PavementLayout",
        icao: str = "",
        include_aprons: bool = False) -> None:
    """Enforce the no-overlap invariant on the layout.

    ``include_aprons``: also resolve APRON ∩ apron / apron ∩ junction
    overlaps (apron joins the junction residue tier).  Off by default so
    the early call sites (junction_emit / mid-finalize) keep their
    original behaviour; the post-neck-split call passes it True, since
    reclassify-to-apron + neck-split run AFTER the mid-finalize clip and
    can leave aprons overlapping junctions / each other (HECA dense
    S/T/W/J/R cluster).  The final solver re-derives node_altitudes for
    the clipped pieces, so this is a pure geometry pass.

    Walks every shape and, where it overlaps another shape, modifies
    or drops it so no two shapes overlap.  Order of priority (the
    LATER a role appears in this list, the more it "yields"):

    1. RUNWAY corners (CIFP-anchored, immutable footprint).
    2. TAXI rect (one per OSM centerline; rect dedup ran upstream).
    3. TERMINAL pad (OSM building).
    4. JUNCTION (residue — must clip to fit around all of the above).

    Strategy:

    * Drop duplicate TERMINAL polygons (a terminal entirely inside
      another terminal is a duplicate from OSM relation parsing).
    * Drop duplicate or heavily-overlapping RUNWAY segments
      (apron-merged runway segmentation can produce overlap with
      the original single-rect runway).
    * Clip each JUNCTION against every fixed shape (rect / runway /
      terminal) and against larger junctions.  Iterate up to 4
      passes so chained clips converge.

    Mutates ``layout.shapes`` in place.
    """
    from shapely.strtree import STRtree
    MIN_KEEP_AREA_M2 = 0.5
    # ``test_no_self_overlap`` enforces SELF_OVERLAP_CAP_M2 = 0.0 —
    # zero tolerance.  Threshold 0 means we clip on ANY non-empty
    # intersection, including sub-meter overlaps (KPHX terminal/
    # terminal 0.226 m²) AND pure float-noise sliver overlaps
    # (KPHX apron/apron ≈ 4e-14 m², SPLP terminal/apron ≈ 4e-14
    # m²) that arise when adjacent shapes share an edge whose
    # coords differ by floating-point epsilon.  Clipping these
    # near-zero overlaps shifts a boundary by ε with no
    # measurable area change, but removes the residual sliver
    # so shapely.intersection returns truly empty afterwards.
    NOISE_OVERLAP_M2 = 0.0

    def _valid_poly(p: Polygon | None) -> Polygon | None:
        if p is None or p.is_empty:
            return None
        if p.geom_type != "Polygon":
            return None
        if not p.is_valid:
            try:
                p = p.buffer(0)
            except _GEOM_EXC:
                return None
            if p.is_empty or p.geom_type != "Polygon":
                return None
        return p

    # A clip whose difference is a MultiPolygon used to keep ONLY the
    # largest piece — silently deleting every other fragment.  Around
    # runways a fixed rect/runway CROSSES a junction, so "keep largest"
    # erased the entire far side of the crossing (KCLT 2026-07-06 user
    # in-sim: disconnected runway stubs, spines with a junction one side
    # and nothing on the other; measured 0.5-1.0 coverage loss at the
    # reported spots in THIS pass).  All pieces above the floor now
    # survive: the primary continues the clip chain, the rest re-enter
    # the fixed-point loop as their own shapes and get clipped/kept on
    # their own merits.
    EXTRA_PIECE_MIN_AREA_M2 = 5.0

    def _clip_pieces(p: Polygon, c: Polygon) -> list[Polygon]:
        """``p.difference(c)`` as polygons sorted by area DESC (largest
        first).  Empty list = nothing usable survives the clip."""
        try:
            d = p.difference(c)
        except _GEOM_EXC:
            return [p]
        if d.is_empty:
            return []
        if d.geom_type == "Polygon":
            return [d] if d.area >= MIN_KEEP_AREA_M2 else []
        if d.geom_type in ("MultiPolygon", "GeometryCollection"):
            pieces = [g for g in d.geoms
                      if g.geom_type == "Polygon"
                      and g.area >= MIN_KEEP_AREA_M2]
            pieces.sort(key=lambda g: -g.area)
            return pieces
        return []

    # (source shape, polygon) fragments to append after the current
    # pass — appended shapes re-enter the next fixed-point iteration.
    extra_fragments: list = []

    def _stash_extras(source_shape, pieces: list) -> None:
        for piece in pieces[1:]:
            if piece.area >= EXTRA_PIECE_MIN_AREA_M2:
                extra_fragments.append((source_shape, piece))

    def _flush_extras() -> None:
        from .layout import BuiltShape as _BuiltShape
        while extra_fragments:
            source_shape, piece = extra_fragments.pop()
            layout.shapes.append(_BuiltShape(
                polygon=piece, role=source_shape.role,
                ref=source_shape.ref,
                reclassified_from_junction=getattr(
                    source_shape, "reclassified_from_junction", False),
                from_route_proximity_cut=getattr(
                    source_shape, "from_route_proximity_cut", False),
                adopts_apron_grade=getattr(
                    source_shape, "adopts_apron_grade", False),
                adopts_taxi_grade=getattr(
                    source_shape, "adopts_taxi_grade", False),
                adopted_taxi_letter=getattr(
                    source_shape, "adopted_taxi_letter", None)))

    n_dropped = 0
    n_clipped = 0
    DUPLICATE_FRAC = 0.80

    # ── Step 1: enforce same-role no-overlap.  Two shapes of the
    # same role (two terminals, two runway segments, two taxi
    # rects) must never overlap.  Three behaviours:
    #
    #   * If one shape is mostly inside the other (≥ DUPLICATE_FRAC
    #     of its area), drop it as a duplicate.
    #   * Otherwise, clip the smaller shape against the larger so
    #     the overlap region is removed from the smaller (the
    #     larger is "the more authoritative" footprint).
    #   * If the clip leaves no usable polygon, drop it.
    same_role_sets = [
            {ROLE_BUILDING},
            {ROLE_RUNWAY},
            {ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
             ROLE_STUB, ROLE_CROSS_CONNECTOR}]
    if include_aprons:
        same_role_sets.append({ROLE_APRON})
    for role_set in same_role_sets:
        # Iterate to a fixed point in case clipping creates new
        # adjacencies that need further clipping.
        for _ in range(4):
            candidates: list[int] = [
                i for i, s in enumerate(layout.shapes)
                if s.role in role_set
                and _valid_poly(s.polygon) is not None]
            # Sort by area DESC so smaller shapes are clipped
            # against larger ones (we walk pairs (i, j) with i<j
            # and clip the SMALLER of the pair).
            candidates.sort(
                key=lambda i: -layout.shapes[i].polygon.area)
            any_change = False
            for ai in range(len(candidates)):
                i = candidates[ai]
                if layout.shapes[i].polygon is None:
                    continue
                pi = layout.shapes[i].polygon
                for bi in range(ai + 1, len(candidates)):
                    j = candidates[bi]
                    if layout.shapes[j].polygon is None:
                        continue
                    pj = layout.shapes[j].polygon
                    try:
                        if not pi.intersects(pj):
                            continue
                        inter = pi.intersection(pj)
                        if (inter.is_empty
                                or inter.area <= NOISE_OVERLAP_M2):
                            continue
                        # Duplicate test.
                        a_min = min(pi.area, pj.area)
                        if (a_min > 0
                                and inter.area / a_min
                                >= DUPLICATE_FRAC):
                            # j is the smaller (sorted desc) —
                            # drop it.
                            layout.shapes[j].polygon = None
                            n_dropped += 1
                            any_change = True
                            continue
                        # Partial overlap — clip j against i.
                        pieces = _clip_pieces(pj, pi)
                        if not pieces:
                            layout.shapes[j].polygon = None
                            n_dropped += 1
                        else:
                            layout.shapes[j].polygon = pieces[0]
                            _stash_extras(layout.shapes[j], pieces)
                            n_clipped += 1
                        any_change = True
                    except _GEOM_EXC:
                        continue
            _flush_extras()
            if not any_change:
                break
    layout.shapes = [s for s in layout.shapes
                     if s.polygon is not None]

    # ── Step 2: priority-ordered clip pass.  Each role yields to
    # everything LISTED ABOVE it in the ``priority`` list:
    #   * RUNWAY (CIFP-anchored) — never modified.
    #   * TERMINAL — yields to runway only.
    #   * TAXI rects — yield to runway + terminal.
    #   * JUNCTION (residue) — yields to everything.
    # Each shape is clipped against every higher-priority shape it
    # overlaps; the result keeps only the largest piece if the clip
    # produces multiple disjoint fragments.  Same-priority shapes
    # of the JUNCTION class additionally yield to LARGER junctions
    # so two junctions can't both claim the same residue area.
    # The residue tier: junctions always; aprons too when requested (so
    # an apron clips against a larger apron/junction and vice versa).
    residue_tier = ({ROLE_JUNCTION, ROLE_APRON} if include_aprons
                    else {ROLE_JUNCTION})
    priority: list[set] = [
        # ROLE_RUNWAY_CROSSING is runway-derived geometry that
        # replaced its source runway segments — same tier as
        # ROLE_RUNWAY so adjacent rects/junctions/aprons clip
        # AGAINST it instead of overlapping into its footprint.
        {ROLE_RUNWAY, ROLE_RUNWAY_CROSSING},
        {ROLE_BUILDING},
        # (s79) SVC road rects are fixed geometry the residue must
        # fit around, exactly like taxi rects — without this an apron
        # overlapped the CYXY pav[1] ramp by 13.5 m2 (zero-tolerance
        # self-overlap test).
        {ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
         ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_SERVICE_ROAD},
        residue_tier,
        {ROLE_BOUNDARY},
    ]
    for outer in range(4):
        any_change = False
        # For each tier (after the first), clip its shapes against
        # all higher-priority shapes.
        for tier_idx in range(1, len(priority)):
            tier_roles = priority[tier_idx]
            higher_polys: list[Polygon] = []
            for s in layout.shapes:
                if s.polygon is None:
                    continue
                role_tier = next(
                    (ti for ti, rs in enumerate(priority)
                     if s.role in rs), -1)
                if 0 <= role_tier < tier_idx:
                    p = _valid_poly(s.polygon)
                    if p is not None:
                        higher_polys.append(p)
            higher_tree = (STRtree(higher_polys)
                           if higher_polys else None)
            # Targets in this tier, sorted by area (largest first
            # — within the JUNCTION tier this lets smaller junctions
            # later be clipped against the already-finalised
            # larger ones).
            target_idx: list[int] = [
                i for i, s in enumerate(layout.shapes)
                if s.role in tier_roles
                and _valid_poly(s.polygon) is not None]
            target_idx.sort(
                key=lambda i: -layout.shapes[i].polygon.area)
            for k, i in enumerate(target_idx):
                tp = layout.shapes[i].polygon
                if tp is None:
                    continue
                new_p: Polygon | None = tp
                # Clip against higher-priority shapes.
                if higher_tree is not None:
                    for hit in higher_tree.query(new_p):
                        fp = higher_polys[hit]
                        try:
                            if not new_p.intersects(fp):
                                continue
                            inter = new_p.intersection(fp)
                            if (inter.is_empty
                                    or inter.area
                                    <= NOISE_OVERLAP_M2):
                                continue
                            pieces = _clip_pieces(new_p, fp)
                            if not pieces:
                                new_p = None
                                break
                            new_p = pieces[0]
                            _stash_extras(layout.shapes[i], pieces)
                            any_change = True
                            n_clipped += 1
                        except _GEOM_EXC:
                            continue
                if (new_p is not None
                        and ROLE_JUNCTION in tier_roles):
                    # Also clip against LARGER same-tier junctions/aprons.
                    for k2 in range(k):
                        i2 = target_idx[k2]
                        tp2 = layout.shapes[i2].polygon
                        if tp2 is None:
                            continue
                        try:
                            if not new_p.intersects(tp2):
                                continue
                            inter = new_p.intersection(tp2)
                            if (inter.is_empty
                                    or inter.area
                                    <= NOISE_OVERLAP_M2):
                                continue
                            pieces = _clip_pieces(new_p, tp2)
                            if not pieces:
                                new_p = None
                                break
                            new_p = pieces[0]
                            _stash_extras(layout.shapes[i], pieces)
                            any_change = True
                            n_clipped += 1
                        except _GEOM_EXC:
                            continue
                if new_p is None:
                    layout.shapes[i].polygon = None
                    n_dropped += 1
                    continue
                if new_p is not tp:
                    layout.shapes[i].polygon = new_p
        _flush_extras()
        if not any_change:
            break

    layout.shapes = [s for s in layout.shapes
                     if s.polygon is not None]

    if (n_clipped + n_dropped) > 0:
        try:
            UI.vprint(1,
                f"  [pav-builder] {icao}: overlap-clip pass — "
                f"{n_clipped} clip operation(s), "
                f"{n_dropped} shape(s) dropped.")
        except _GEOM_EXC:
            pass
