"""Airport boundary ribbon + boundary→DEM bridge polygons.

Two emitters:

* ``_emit_airport_boundary_shape`` — closed-ring airport boundary
  shape that wraps the union of all emitted airside pavement,
  used by downstream consumers to clip terrain underlay.
* ``_emit_boundary_dem_bridge`` — wedge polygons that bridge the
  airport boundary to the surrounding DEM where the airport sits
  noticeably above or below the natural terrain (CYXY's plateau,
  HECA's berm), preventing visual cliffs.

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    _emit_airport_boundary_shape
    _emit_boundary_dem_bridge
"""
from __future__ import annotations

import math
import os

import O4_UI_Utils as UI
from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import linemerge, nearest_points, unary_union

# Narrow exception tuple for shapely / geometry ops that signal
# degenerate input rather than a programming error.  Replaces the
# blanket ``except Exception`` blocks that previously silently
# swallowed ``NameError`` from a missing import (user 2026-05-10
# — boundary runway-elevation clamp had been broken since the
# slice-5 refactor because the import dance masked a NameError).
#
# Programming errors (``NameError``, ``ImportError``,
# ``AttributeError`` from typos / ``None``-leaks) intentionally
# propagate so they surface immediately during testing rather than
# being silently masked at runtime.  Real shapely degeneracy
# surfaces as ``GEOSException`` / ``TopologicalError`` /
# ``ValueError``.  (DEM sampling clamps out-of-bounds to the tile
# edge rather than raising, so no ``IndexError`` is expected.)
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


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
    ROLE_RUNWAY_CLEARANCE,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL,
    ROLE_STUB,
    ROLE_BUILDING,
    ROLE_RETAINING_WALL,
    SHARED_VERTEX_TOL_M,
    corner_alts_from_high_low,
)
from .pavement.vertices import _snap_polygon_vertices_to_rect_corners
from .pavement.junctions import _decompose_polygon_with_holes
from .pavement.runways import _sample_runway_segment_elev
from .elevation import _resample_node_altitudes_nn, _sample_dem


__all__ = [
    "_emit_airport_boundary_shape",
    "_emit_boundary_dem_bridge",
    "_clip_boundary_bridges_against_pavement",
    "_reconcile_boundary_bridges_with_skirts",
    "_snap_bridge_vertices_to_runway_corners",
    "_insert_bridge_contacts_into_junctions",
    "_flatten_bridge_pinch_necks",
    "_despike_airport_boundary",
]


# Row-130 needle removal (``_despike_airport_boundary``).  A vertex is a
# digitization NEEDLE when its interior wedge is sharper than the apex
# threshold AND dropping it changes the ring area by less than the area
# threshold — a sharp, near-zero-area zigzag no real airport fence has
# (HEAZ @ 30.10017,31.35442: 50.6° apex, 10.4 m / 4.4 m legs, ~18 m²).
# Real acute boundary corners enclose far more area than 150 m²; the
# ribbon's miter at any apex sharper than ~60° exceeds short legs and
# folds the strip over itself (boundary∩boundary overlap).
_BOUNDARY_SPIKE_MAX_APEX_DEG = 60.0
_BOUNDARY_SPIKE_MAX_AREA_M2 = 150.0


def _despike_airport_boundary(
        poly: Polygon,
        max_apex_deg: float = _BOUNDARY_SPIKE_MAX_APEX_DEG,
        max_spike_area_m2: float = _BOUNDARY_SPIKE_MAX_AREA_M2,
        icao: str = "") -> Polygon:
    """Remove digitization needle vertices from a row-130 boundary
    polygon (meter space).  Returns the cleaned polygon, or the input
    unchanged when nothing qualifies or the cleaned ring degenerates.

    The apt.dat row-130 ring is traced by hand and occasionally contains
    a needle — a vertex whose two edges double back at a sharp angle
    enclosing almost no area.  The boundary ribbon (a
    ``BOUNDARY_STRIP_HALF_WIDTH_M`` band following the ring) needs a
    miter longer than the needle's legs at such an apex, so consecutive
    ribbon pieces fold over each other and emit overlapping pavement
    (HEAZ #448∩#449/#450).  Dropping the apex vertex is a faithful
    cleanup: the enclosed area is below mapping resolution.
    """
    if poly is None or poly.is_empty or poly.geom_type != "Polygon":
        return poly
    cos_max = math.cos(math.radians(max_apex_deg))
    n_removed = 0

    def _despike_ring(coords: list) -> list:
        nonlocal n_removed
        pts = list(coords)
        if len(pts) >= 2 and pts[0] == pts[-1]:
            pts = pts[:-1]
        changed = True
        while changed and len(pts) > 3:
            changed = False
            for i in range(len(pts)):
                px, py = pts[i - 1]
                cx, cy = pts[i]
                nx, ny = pts[(i + 1) % len(pts)]
                v1x, v1y = px - cx, py - cy
                v2x, v2y = nx - cx, ny - cy
                n1 = math.hypot(v1x, v1y)
                n2 = math.hypot(v2x, v2y)
                if n1 < 1e-9 or n2 < 1e-9:
                    # Doubled vertex — drop it.
                    del pts[i]
                    n_removed += 1
                    changed = True
                    break
                # cos(apex) > cos(threshold) ⇔ apex < threshold.
                cos_apex = (v1x * v2x + v1y * v2y) / (n1 * n2)
                if cos_apex <= cos_max:
                    continue
                tri = 0.5 * abs((cx - px) * (ny - py)
                                - (cy - py) * (nx - px))
                if tri >= max_spike_area_m2:
                    continue
                del pts[i]
                n_removed += 1
                changed = True
                break
        return pts

    try:
        ext = _despike_ring(poly.exterior.coords)
        if len(ext) < 3:
            return poly
        holes = []
        for ring in poly.interiors:
            h = _despike_ring(ring.coords)
            if len(h) >= 3:
                holes.append(h)
        if not n_removed:
            return poly
        cleaned = Polygon(ext, holes or None)
        if not cleaned.is_valid:
            cleaned = cleaned.buffer(0)
        if (cleaned.is_empty or cleaned.geom_type != "Polygon"
                or cleaned.area < 0.99 * poly.area):
            return poly
    except _GEOM_EXC:
        return poly
    try:
        UI.vprint(1,
            f"  [pav-builder] {icao}: de-spiked {n_removed} needle "
            f"vertex(es) from the row-130 airport boundary.")
    except _GEOM_EXC:
        pass
    return cleaned


# Half-width (m) of the airport-boundary ribbon strip.  Single source
# of truth: the ribbon (``_emit_airport_boundary_shape``) offsets its
# corners by this, and the DEM bridge (``_emit_boundary_dem_bridge``)
# places its OUTER edge on the ribbon's inner edge at the SAME offset
# so the two meet flush (no vertical wall — see the bridge outer-edge
# clamp).  These two MUST agree; do not duplicate the literal.
BOUNDARY_STRIP_HALF_WIDTH_M = 2.5


# Airside-pavement roles that anchor the boundary-ribbon altitude clamp
# (user 2026-05-22).  The clamp pulls the perimeter ribbon UP toward the
# nearest such surface within ``clamp_radius_m`` so the ribbon never falls
# below ``surface_edge − grade·distance``.  It used to consider plain
# ROLE_RUNWAY only, which had TWO blind spots at CYXY:
#   * Large aprons / taxiways >400 m from any runway (east apron: ribbon
#     dropped ~39 m below the 694.7 m apron edge).
#   * RUNWAY pavement that survives only as ``runway_crossing`` polygons.
#     Where runways intersect (CYXY's 02/20 crosses 14L/32R + 14R/32L) the
#     overlap is emitted as ROLE_RUNWAY_CROSSING, NOT ROLE_RUNWAY, and the
#     plain runway segments around it have gaps.  So a node 385 m from the
#     14L/32R centerline read its nearest ROLE_RUNWAY shape at 484 m
#     (>radius) and dropped to DEM, even though runway-elevation crossing
#     pavement sat only 330 m away.  ROLE_RUNWAY_CROSSING is runway
#     pavement (it maps to the "runway" surface type) and MUST be included.
# Groundside (DEM-level by design) and terminal pavement are EXCLUDED.
_CLAMP_PAVEMENT_ROLES = {
    ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR,
    ROLE_JUNCTION,
    ROLE_APRON,
}


# Width (m) of the "release band" just inside ``clamp_radius_m`` over which
# the clamp's UP-lift is tapered to zero, so the ribbon meets the DEM
# CONTINUOUSLY at the radius instead of stepping.
#
# Why (user 2026-05-25, CYXY far-north tear): the clamp lifts the ribbon to
# a floor ``best_e − grade·d`` only within ``clamp_radius_m``; one step
# beyond it returned raw DEM.  Where the perimeter runs far (>400 m) from
# pavement AND the terrain sits well below that floor (CYXY's north
# peninsula: 694 m runway, 670 m terrain), the floor at the radius edge was
# still ~12 m above the DEM — so a single ribbon rect straddling the radius
# dropped 11.6 m (≈59 % grade), a near-vertical wall that read as mesh
# tearing.  Tapering the lift to 0 over the last ``release band`` metres
# removes the discontinuity (lift→0 ⇒ ribbon→DEM at the radius) while
# leaving the near-pavement clamp (d ≤ radius − band) untouched.  Beyond the
# radius the ribbon still follows DEM exactly as before.
_CLAMP_RELEASE_BAND_M = 120.0


def _collect_clamp_pavement(layout: "PavementLayout") -> list[BuiltShape]:
    """Airside-pavement shapes (runway / taxiway / junction / apron) with a
    usable polygon that anchor the boundary-ribbon altitude clamp."""
    return [s for s in layout.shapes
            if s.role in _CLAMP_PAVEMENT_ROLES
            and s.polygon is not None
            and not s.polygon.is_empty]


def _runway_clamped_alt_at(
        x: float, y: float, *,
        dem, tile_lat: int, tile_lon: int,
        pavement_shapes, m_to_ll,
        clamp_radius_m: float, clamp_grade: float) -> float | None:
    """DEM at (x, y) clamped UP toward the nearest airside pavement when
    within ``clamp_radius_m`` and the DEM dips below ``surface_e −
    grade·d``; else raw DEM; else None.

    ``pavement_shapes`` is the set of clamp anchors — runways, taxiways,
    junctions and aprons (see ``_collect_clamp_pavement``).  Originally
    runways only; widened (user 2026-05-22) so the ribbon is also held up
    next to large aprons / taxiways that lie beyond the 400 m radius of any
    runway (CYXY: a perimeter section ~440 m from the runway but only ~65 m
    from the 694.7 m east apron was dropping to ~655 m raw DEM).

    Per user 2026-05-11 the clamp is ASYMMETRIC — only ever pull the
    boundary UP toward the pavement, never DOWN.  If surrounding terrain
    is higher than the pavement band the boundary follows DEM so
    Ortho4XP's ``smooth_raster_over_airports`` doesn't drag the
    rendered terrain into a canyon around the perimeter.

    Single source of truth: both the airport-boundary ribbon
    (``_emit_airport_boundary_shape``) and the DEM bridge
    (``_emit_boundary_dem_bridge``) call this so their shared edge gets
    identical altitudes (they used to be two byte-identical copies
    under two names, ``_runway_clamped_alt`` / ``_clamped_alt``).
    """
    try:
        lat, lon = m_to_ll(x, y)
        dem_e = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
    except _GEOM_EXC:
        dem_e = None
    # Find nearest airside pavement (runway/taxiway/junction/apron) and
    # its elevation at the nearest point.  ``_sample_runway_segment_elev``
    # is general: it reads ``altitude`` (flat apron/junction), per-vertex
    # ``node_altitudes``, or the sloped ``altitude_high``/``low`` quad
    # convention shared by runway and taxiway rects.
    best_d = float('inf')
    best_e = None
    pt = Point(x, y)
    for s in pavement_shapes:
        try:
            d = s.polygon.distance(pt)
        except _GEOM_EXC:
            continue
        if d >= best_d:
            continue
        try:
            if d == 0.0:
                np_x, np_y = x, y
            else:
                np = nearest_points(s.polygon, pt)[0]
                np_x, np_y = np.x, np.y
            e = _sample_runway_segment_elev(s, np_x, np_y)
        except _GEOM_EXC:
            e = None
        if e is None:
            continue
        best_d = d
        best_e = e
    if best_e is None:
        return dem_e
    if best_d > clamp_radius_m:
        return dem_e
    band = best_d * clamp_grade
    lo = best_e - band
    if dem_e is None:
        # No DEM — fall back to the floor (closest to the runway at
        # this distance without violating the grade cap).
        return lo
    # Asymmetric: only pull UP toward pavement; otherwise follow DEM.
    lift = lo - dem_e
    if lift <= 0.0:
        return dem_e
    # Taper the lift to 0 over the last ``_CLAMP_RELEASE_BAND_M`` metres
    # before the radius so the ribbon meets the DEM continuously at the
    # radius edge (no step).  ``taper`` is 1 for d ≤ radius − band (full
    # clamp, unchanged) and ramps linearly to 0 at d = radius.
    release_start = clamp_radius_m - _CLAMP_RELEASE_BAND_M
    if best_d <= release_start:
        taper = 1.0
    else:
        taper = (clamp_radius_m - best_d) / _CLAMP_RELEASE_BAND_M
    return dem_e + lift * taper


def _clip_boundary_bridges_against_pavement(
        layout: "PavementLayout",
        min_area_m2: float = 25.0,
        min_overlap_m2: float = 1.0) -> int:
    """Post-process: re-subtract pavement (junction/terminal/rect/
    runway) AND the airport_boundary ribbon from every
    ``boundary_dem_bridge`` shape.

    ``_emit_boundary_dem_bridge`` subtracts the junctions/terminals
    AND the ribbon present AT EMIT TIME, but downstream passes
    (per_surface_solve, subdivide_violating_junctions,
    stitch_pavement_polygons, _split_sloped_rects_at_violations,
    boundary-interior clip, feature conformance) reshape pavement and
    ribbon polygons — a junction may merge with a neighbour, a
    subdivide may grow a junction across the bridge boundary, the
    boundary-interior clip moves ribbon edges, etc.  Any such growth
    creates a stale overlap because the bridge was clipped against the
    emit-time snapshot (LMML: the ribbon's post-emit reshape left 5
    pieces × 37.8 m² over one DEM bridge).

    This pass runs LAST, after all ribbon / pavement reshaping.  The
    DEM bridge is conformance-exempt (``conformance._OVERLAY_REFS``),
    so re-clipping it here never reintroduces a reported T-junction.
    Per user 2026-05-13 (CYXY way -10483 overlap report): zero
    tolerance for bridge↔pavement overlap.

    Only overlaps larger than ``min_overlap_m2`` are trimmed, so
    bridges that merely TOUCH pavement / ribbon along a flush shared
    edge (sub-m² float slivers) are left byte-identical — this keeps
    every fixture that has no real bridge overlap untouched.

    Returns the number of bridge shapes modified (clipped or dropped).
    """
    bridges = [s for s in layout.shapes
               if s.role == ROLE_BOUNDARY
               and s.ref == "boundary_dem_bridge"
               and s.polygon is not None
               and not s.polygon.is_empty]
    if not bridges:
        return 0

    # Roles that bridges must NOT overlap.  We exclude OTHER DEM
    # bridges (they genuinely share vertices by design where adjacent
    # bridge runs meet at a perimeter corner — mutual subtraction
    # would erode them).  The airport_boundary RIBBON, however, is
    # meant to meet the bridge FLUSH (bridge outer edge on ribbon
    # inner edge); the emit-time trim subtracts ``ribbon_union``, but
    # downstream passes that reshape the ribbon (seam conformance,
    # vertex welding) can leave a stale interior overlap this last
    # pass never cleaned (LMML: 5 ribbon pieces × 37.8 m² over one
    # DEM bridge).  Subtracting the ribbon here removes that real
    # double-cover while leaving a truly flush shared edge untouched
    # (difference() of a merely-touching polygon is a no-op).
    NON_BRIDGE_PAVEMENT = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
        ROLE_STUB, ROLE_CROSS_CONNECTOR,
        ROLE_JUNCTION, ROLE_BUILDING, ROLE_APRON,
    }
    obstacles = [s for s in layout.shapes
                 if (s.role in NON_BRIDGE_PAVEMENT
                     or (s.role == ROLE_BOUNDARY
                         and s.ref == "airport_boundary"))
                 and s.polygon is not None
                 and not s.polygon.is_empty]
    if not obstacles:
        return 0

    n_modified = 0
    new_shapes: list[BuiltShape] = []
    for s in layout.shapes:
        if (s.role != ROLE_BOUNDARY
                or s.ref != "boundary_dem_bridge"
                or s.polygon is None
                or s.polygon.is_empty):
            new_shapes.append(s)
            continue
        bridge_poly = s.polygon
        old_alts = s.node_altitudes
        old_open = list(bridge_poly.exterior.coords)
        if old_open and old_open[0] == old_open[-1]:
            old_open = old_open[:-1]
        modified = False
        for obs in obstacles:
            try:
                if not bridge_poly.intersects(obs.polygon):
                    continue
                inter_area = bridge_poly.intersection(obs.polygon).area
                if inter_area <= min_overlap_m2:
                    continue
                bridge_poly = bridge_poly.difference(obs.polygon)
                modified = True
            except _GEOM_EXC:
                continue
            if bridge_poly.is_empty:
                break
            if bridge_poly.geom_type not in (
                    "Polygon", "MultiPolygon",
                    "GeometryCollection"):
                bridge_poly = None
                break
        if bridge_poly is None or bridge_poly.is_empty:
            n_modified += 1
            continue
        if not modified:
            new_shapes.append(s)
            continue
        # Extract Polygon members.  difference() can yield Polygon,
        # MultiPolygon, or GeometryCollection (when subtracted
        # boundaries touch at points/edges).
        if bridge_poly.geom_type == "Polygon":
            pieces = [bridge_poly]
        elif bridge_poly.geom_type == "MultiPolygon":
            pieces = list(bridge_poly.geoms)
        elif bridge_poly.geom_type == "GeometryCollection":
            pieces = [g for g in bridge_poly.geoms
                      if g.geom_type == "Polygon"]
        else:
            pieces = []
        pieces = [p for p in pieces
                  if p.is_valid and not p.is_empty
                  and p.area >= min_area_m2]
        if not pieces:
            n_modified += 1
            continue
        # Keep the largest piece (consistent with emit-time logic).
        pieces.sort(key=lambda g: -g.area)
        keep = pieces[0]
        new_s = BuiltShape(
            polygon=keep,
            role=s.role,
            ref=s.ref,
            source_axis=s.source_axis,
            altitude=s.altitude,
            altitude_high=s.altitude_high,
            altitude_low=s.altitude_low,
            node_altitudes=None,
            is_bridge=s.is_bridge,
        )
        # Resample node_altitudes via edge interpolation against the
        # ORIGINAL bridge ring's per-vertex altitudes.
        if old_alts is not None and old_open:
            new_alts = _resample_node_altitudes_nn(
                keep, old_open, old_alts)
            if new_alts is not None:
                new_s.node_altitudes = new_alts
        new_shapes.append(new_s)
        n_modified += 1

    layout.shapes = new_shapes
    return n_modified


def _reconcile_boundary_bridges_with_skirts(
        layout: "PavementLayout",
        overlap_min_m2: float = 1.0,
        min_area_m2: float = 25.0,
        reconcile_tol_m: float = 8.0) -> int:
    """Make every ``boundary_dem_bridge`` MATCH the runway-end SKIRT /
    RESA surface (role ``runway_clearance``) where the two meet.

    The boundary→DEM bridge emits in the feature phase, BEFORE the final
    grade projection and the runway-end skirts (the absolute-last
    emission — they bake the law floor from the settled pavement
    profile; see the skirt call site in ``pipeline.py``).  So a bridge
    at a runway end anchors its inner edge to the RAW DEM and cannot
    know the skirt/RESA surface emitted later: where the skirt fills a
    hillside brow up to the law floor, the bridge still descends to bare
    terrain, and the two meet in a step (KCLT 18R: a ~10 m bridge-vs-
    skirt mismatch — the ramp the user sees in-sim).

    Per the user's requirement ("if we determine a bridge is needed, it
    has to match with the skirt or RESA"), and keeping the skirt LAST
    (as it must be), this pass runs AFTER the skirt emits and does two
    things per bridge:

      1. **Subtract** any skirt/RESA area the bridge overlaps (>
         ``overlap_min_m2``): inside a governed runway-end zone the
         skirt/RESA is the authoritative graded surface, so the bridge
         must not re-expose raw DEM there.
      2. **Re-anchor** every surviving bridge vertex that lies within
         ``reconcile_tol_m`` of a skirt/RESA surface to that surface's
         edge-interpolated altitude, so the bridge and skirt meet flush
         at their shared frontier instead of stepping.  (A bridge and a
         skirt are separated by the skirt's pavement-gap / clip buffer,
         so they typically ABUT rather than overlap — the re-anchor,
         not the subtraction, is what closes the visible step.)

    Mirror of ``_clip_boundary_bridges_against_pavement`` for the
    subtraction + largest-piece + altitude-resample machinery.  Returns
    the number of bridge shapes modified.
    """
    from .clearance import _edge_interp_alt as _skirt_edge_alt

    obstacles = [s for s in layout.shapes
                 if s.role == ROLE_RUNWAY_CLEARANCE
                 and s.polygon is not None
                 and not s.polygon.is_empty]
    if not obstacles:
        return 0
    if not any(s.role == ROLE_BOUNDARY and s.ref == "boundary_dem_bridge"
               and s.polygon is not None and not s.polygon.is_empty
               for s in layout.shapes):
        return 0

    def _nearest_skirt_alt(x, y):
        """Skirt/RESA surface altitude at ``(x, y)`` from the nearest
        clearance shape within ``reconcile_tol_m`` (edge-interpolated,
        the same read the skirt emits / the validator samples)."""
        best = None
        pt = Point(x, y)
        for obs in obstacles:
            try:
                d = obs.polygon.distance(pt)
            except _GEOM_EXC:
                continue
            if d <= reconcile_tol_m and (best is None or d < best[0]):
                alt = _skirt_edge_alt(obs, x, y)
                if alt is not None:
                    best = (d, alt)
        return None if best is None else best[1]

    n_modified = 0
    new_shapes: list[BuiltShape] = []
    for s in layout.shapes:
        if (s.role != ROLE_BOUNDARY
                or s.ref != "boundary_dem_bridge"
                or s.polygon is None
                or s.polygon.is_empty):
            new_shapes.append(s)
            continue
        bridge_poly = s.polygon
        old_alts = s.node_altitudes
        old_open = list(bridge_poly.exterior.coords)
        if old_open and old_open[0] == old_open[-1]:
            old_open = old_open[:-1]
        # 1. Subtract overlapped skirt/RESA area.
        modified = False
        for obs in obstacles:
            try:
                if not bridge_poly.intersects(obs.polygon):
                    continue
                if bridge_poly.intersection(obs.polygon).area \
                        <= overlap_min_m2:
                    continue
                bridge_poly = bridge_poly.difference(obs.polygon)
                modified = True
            except _GEOM_EXC:
                continue
            if bridge_poly.is_empty:
                break
            if bridge_poly.geom_type not in (
                    "Polygon", "MultiPolygon", "GeometryCollection"):
                bridge_poly = None
                break
        if bridge_poly is None or bridge_poly.is_empty:
            n_modified += 1
            continue
        if modified:
            if bridge_poly.geom_type == "Polygon":
                pieces = [bridge_poly]
            elif bridge_poly.geom_type == "MultiPolygon":
                pieces = list(bridge_poly.geoms)
            elif bridge_poly.geom_type == "GeometryCollection":
                pieces = [g for g in bridge_poly.geoms
                          if g.geom_type == "Polygon"]
            else:
                pieces = []
            pieces = [p for p in pieces
                      if p.is_valid and not p.is_empty
                      and p.area >= min_area_m2]
            if not pieces:
                n_modified += 1
                continue
            pieces.sort(key=lambda g: -g.area)
            keep = pieces[0]
            # node_altitudes are stored CLOSED (first == last).
            alts_closed = None
            if old_alts is not None and old_open:
                alts_closed = _resample_node_altitudes_nn(
                    keep, old_open, old_alts)
        else:
            keep = bridge_poly
            alts_closed = list(old_alts) if old_alts is not None else None

        # 2. Re-anchor vertices near a skirt/RESA surface to it so the
        #    bridge and skirt meet flush (closed-ring: keep first == last).
        reanchored = False
        if alts_closed is not None:
            ring_closed = list(keep.exterior.coords)
            m = min(len(ring_closed), len(alts_closed))
            for i in range(m):
                vx, vy = ring_closed[i]
                sa = _nearest_skirt_alt(vx, vy)
                if sa is not None and abs(sa - alts_closed[i]) > 0.05:
                    alts_closed[i] = round(float(sa), 1)
                    reanchored = True
            # Preserve the closed-ring invariant if the shared vertex moved.
            if len(alts_closed) >= 2:
                alts_closed[-1] = alts_closed[0]

        if not modified and not reanchored:
            new_shapes.append(s)
            continue
        new_shapes.append(BuiltShape(
            polygon=keep,
            role=s.role,
            ref=s.ref,
            source_axis=s.source_axis,
            altitude=s.altitude,
            altitude_high=s.altitude_high,
            altitude_low=s.altitude_low,
            node_altitudes=alts_closed,
            is_bridge=s.is_bridge,
        ))
        n_modified += 1

    layout.shapes = new_shapes
    return n_modified


def _snap_bridge_vertices_to_runway_corners(
        layout: "PavementLayout", snap_tol_m: float = 2.0) -> int:
    """Snap ``boundary_dem_bridge`` vertices that sit within
    ``snap_tol_m`` of a sloping-rect (runway / parallel / stub /
    cross-connector) CORNER onto that corner; resample per-vertex
    altitudes.  Returns the number of bridges modified.

    The bridge clears sloping rects by ~1 m (``sr_union.buffer(1.0)``
    in ``_emit_boundary_dem_bridge``) to keep its vertices off rect
    EDGES (``test_no_vertex_on_sloping_rect_edge``).  The round buffer
    leaves a ~1 m densified arc around each rect CORNER; where a
    junction shares that corner (runway 1:1 sharing), the arc vertices
    land ~1 m off the junction's corner vertex and trip
    ``test_junction_neighbour_corners_shared``.  A vertex coincident
    with a rect corner is explicitly ALLOWED by the no-vertex invariant
    (only edge-interior coincidence is forbidden), so collapsing the
    arc onto the actual corner node lets the bridge SHARE the
    runway/junction corner — satisfying both invariants.  Edge-clearance
    vertices (>``snap_tol_m`` from any corner) are untouched, so the 1 m
    edge clearance — and the no-mid-edge-vertex guarantee — is preserved.

    ``snap_tol_m`` is 2.0 m (user 2026-05-28): the 1 m edge clearance
    means a bridge vertex on the straight edge-clearance run NEAR a
    corner sits ~1 m perpendicular off the edge AND up to ~1.6 m ALONG
    it, i.e. up to ~1.9 m from the corner — just beyond the old 1.5 m
    tolerance.  Such a vertex was left un-snapped, then
    ``_insert_bridge_contacts_into_junctions`` inserted it onto the
    junction's runway-boundary edge ~1 m off the corner, tripping Rule 1
    (``test_junction_runway_node_sharing`` — CYXY junction near 14L/32R).
    2.0 m collapses it onto the corner instead, satisfying both Rule 1
    and the neighbour-corner share.
    """
    sloping = [s.polygon for s in layout.shapes
               if s.role in (ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
                             ROLE_SECONDARY_PARALLEL, ROLE_STUB,
                             ROLE_CROSS_CONNECTOR)
               and s.polygon is not None
               and not s.polygon.is_empty]
    if not sloping:
        return 0
    n_modified = 0
    for s in layout.shapes:
        if (s.role != ROLE_BOUNDARY
                or s.ref != "boundary_dem_bridge"
                or s.polygon is None
                or s.polygon.is_empty):
            continue
        old_open = list(s.polygon.exterior.coords)
        if old_open and old_open[0] == old_open[-1]:
            old_open = old_open[:-1]
        try:
            snapped = _snap_polygon_vertices_to_rect_corners(
                s.polygon, sloping, snap_tol_m=snap_tol_m)
        except _GEOM_EXC:
            continue
        if (snapped is None or snapped.is_empty
                or snapped.geom_type != "Polygon"):
            continue
        # ``_snap_polygon_vertices_to_rect_corners`` returns the input
        # unchanged when nothing snapped — cheap identity skip.
        if snapped is s.polygon:
            continue
        old_alts = s.node_altitudes
        s.polygon = snapped
        if old_alts is not None and old_open:
            new_alts = _resample_node_altitudes_nn(
                snapped, old_open, old_alts)
            s.node_altitudes = new_alts if new_alts is not None else None
        n_modified += 1
    return n_modified


def _insert_bridge_contacts_into_junctions(
        layout: "PavementLayout",
        edge_tol_m: float = 0.10,
        vertex_tol_m: float = 0.10,
        sloping_guard_m: float = 0.5) -> int:
    """Insert each ``boundary_dem_bridge`` vertex that lies ON a
    junction edge (but is not already a junction vertex) into that
    junction's ring so the two SHARE the node.  Returns the number of
    vertices inserted.

    Complements ``_snap_bridge_vertices_to_runway_corners``: that pass
    handles bridge vertices near a runway CORNER; this one handles
    bridge vertices that land mid-edge on a junction's NON-runway
    boundary (where the bridge abuts the junction directly).  Both
    target ``test_junction_neighbour_corners_shared`` — a bridge vertex
    within 1 m of a junction perimeter must coincide with a junction
    vertex.

    The inserted point is collinear on the junction edge, so the
    junction's footprint and per-vertex grade are unchanged (the new
    altitude is the linear interpolation along the edge).  Points within
    ``sloping_guard_m`` of a sloping-rect edge are skipped so the
    insertion can't create a ``test_no_vertex_on_sloping_rect_edge``
    violation on the junction side.
    """
    bridges = [s for s in layout.shapes
               if s.role == ROLE_BOUNDARY
               and s.ref == "boundary_dem_bridge"
               and s.polygon is not None
               and not s.polygon.is_empty]
    junctions = [s for s in layout.shapes
                 if s.role == ROLE_JUNCTION
                 and s.polygon is not None
                 and not s.polygon.is_empty]
    if not bridges or not junctions:
        return 0
    sloping_edges = []
    for s in layout.shapes:
        if s.role in (ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
                      ROLE_SECONDARY_PARALLEL, ROLE_STUB,
                      ROLE_CROSS_CONNECTOR) and s.polygon is not None \
                and not s.polygon.is_empty:
            sloping_edges.append(s.polygon.boundary)

    bridge_pts = []
    for b in bridges:
        for x, y in list(b.polygon.exterior.coords)[:-1]:
            bridge_pts.append((x, y))
    if not bridge_pts:
        return 0

    n_inserted = 0
    for j in junctions:
        ring = list(j.polygon.exterior.coords)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        n = len(ring)
        if n < 3:
            continue
        alts = j.node_altitudes
        # alts is closed-ring length (n+1); use the open part.
        open_alts = (alts[:n] if alts is not None and len(alts) >= n
                     else None)
        # For each edge collect bridge points lying on it.
        new_ring: list[tuple[float, float]] = []
        new_alts: list[float] = []
        changed = False
        added_here = 0
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            new_ring.append((ax, ay))
            if open_alts is not None:
                new_alts.append(open_alts[i])
            dx, dy = bx - ax, by - ay
            seg_l2 = dx * dx + dy * dy
            if seg_l2 <= 1e-9:
                continue
            on_edge = []
            for px, py in bridge_pts:
                t = ((px - ax) * dx + (py - ay) * dy) / seg_l2
                if t <= 0.01 or t >= 0.99:
                    continue
                projx, projy = ax + t * dx, ay + t * dy
                if math.hypot(px - projx, py - projy) > edge_tol_m:
                    continue
                # Skip if already (nearly) a junction vertex.
                if (math.hypot(px - ax, py - ay) <= vertex_tol_m
                        or math.hypot(px - bx, py - by) <= vertex_tol_m):
                    continue
                # Skip if near a sloping-rect edge (would move the
                # no_vertex violation to the junction).
                pt = Point(px, py)
                if any(se.distance(pt) < sloping_guard_m
                       for se in sloping_edges):
                    continue
                on_edge.append((t, px, py))
            if not on_edge:
                continue
            on_edge.sort(key=lambda e: e[0])
            seen_t = set()
            for t, px, py in on_edge:
                key = round(t, 4)
                if key in seen_t:
                    continue
                seen_t.add(key)
                new_ring.append((px, py))
                if open_alts is not None:
                    new_alts.append(
                        open_alts[i] * (1.0 - t)
                        + open_alts[(i + 1) % n] * t)
                changed = True
                added_here += 1
        if not changed:
            continue
        try:
            new_poly = Polygon(new_ring)
            if not new_poly.is_valid:
                new_poly = new_poly.buffer(0)
        except _GEOM_EXC:
            continue  # leave this junction unchanged
        if new_poly.geom_type != "Polygon" or new_poly.is_empty:
            continue
        n_inserted += added_here
        j.polygon = new_poly
        if open_alts is not None and len(new_alts) == len(new_ring):
            j.node_altitudes = new_alts + [new_alts[0]]
    return n_inserted


def _node_altitudes_from_segment_slope(
        open_ring: List[Tuple[float, float]],
        seg_high: Tuple[float, float],
        seg_low: Tuple[float, float],
        eh: float,
        el: float) -> List[float]:
    """Per-vertex altitudes for a boundary strip whose 4-corner sloped
    quad was reshaped into a non-quad (e.g. trimmed against pavement,
    or repaired by ``buffer(0)``).

    A boundary strip slopes ALONG the perimeter segment — altitude
    ``eh`` at the high end ``seg_high`` down to ``el`` at the low end
    ``seg_low`` — and is flat across its width.  Each vertex altitude is
    therefore the linear ``eh``->``el`` interpolation of the vertex's
    projection onto the high->low axis (clamped to the segment ends).
    Vertices that survive a pavement trim land on shared pavement nodes,
    so the consensus pass in ``to_osm`` reconciles them with the
    abutting shapes' altitudes automatically.

    The returned list is aligned with the CLOSED ring (the closing
    repeat is appended), matching the ``node_altitudes`` contract.
    """
    if not open_ring:
        return []
    ax = seg_low[0] - seg_high[0]
    ay = seg_low[1] - seg_high[1]
    L2 = ax * ax + ay * ay
    alts: List[float] = []
    for x, y in open_ring:
        if L2 < 1e-9:
            t = 0.0
        else:
            t = ((x - seg_high[0]) * ax + (y - seg_high[1]) * ay) / L2
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        alts.append(round(eh + t * (el - eh), 1))
    return alts + [alts[0]]


def _densify_ring_coords(coords: list[tuple[float, float]],
                         step_m: float) -> list[tuple[float, float]]:
    """Insert intermediate points so consecutive vertices are ≤ ``step_m``
    apart; return the CLOSED ring (first == last).  Shared by the boundary
    ribbon geometry below."""
    if not coords:
        return coords
    if coords[0] == coords[-1]:
        coords = coords[:-1]
    out: list[tuple[float, float]] = []
    n = len(coords)
    for i in range(n):
        a = coords[i]
        b = coords[(i + 1) % n]
        out.append(a)
        d = math.hypot(b[0] - a[0], b[1] - a[1])
        if d > step_m:
            steps = max(1, int(d / step_m))
            for k in range(1, steps):
                t = k / steps
                out.append((a[0] + t * (b[0] - a[0]),
                            a[1] + t * (b[1] - a[1])))
    out.append(out[0])
    return out


def _ribbon_segment_geometry(
        boundary_geom,
        strip_half_width_m: float,
        densify_step_m: float,
        ) -> list[tuple[tuple[float, float], tuple[float, float],
                        tuple[float, float], tuple[float, float]]]:
    """Pure-geometry boundary-ribbon construction shared by the ribbon EMIT
    (:func:`_emit_airport_boundary_shape`) and the pre-solve interior
    computation (:func:`_compute_boundary_ribbon_interior`).

    Returns a list of ``(p0, p1, perp0, perp1)`` tuples — one per densified
    boundary segment.  ``p0``/``p1`` are the segment endpoints ON the
    row-130 line (the rect's OUTER long edge); ``perp0``/``perp1`` are the
    inward (interior-pointing) per-vertex offsets already scaled by the full
    strip width, so a rect spans corners
    ``[p0, p1, p1+perp1, p0+perp0]``.

    Altitude-INDEPENDENT (altitudes only set the emitted tags), so both
    callers see identical rect geometry — the pavement clipped to the
    pre-solve interior therefore shares its seam vertices with the
    post-solve-emitted ribbon's inner edge (a conforming seam, no slivers).
    """
    if boundary_geom is None or boundary_geom.is_empty:
        return []
    if boundary_geom.geom_type == "Polygon":
        ext_rings = [boundary_geom.exterior]
    elif boundary_geom.geom_type == "MultiPolygon":
        ext_rings = [g.exterior for g in boundary_geom.geoms]
    else:
        return []
    strip_width_m = 2.0 * strip_half_width_m
    out: list = []
    for ring in ext_rings:
        ring_coords = list(ring.coords)
        if ring_coords and ring_coords[0] == ring_coords[-1]:
            ring_coords = ring_coords[:-1]
        if len(ring_coords) < 3:
            continue
        dense = _densify_ring_coords(ring_coords, densify_step_m)
        if len(dense) < 4:
            continue
        dense_open = dense[:-1] if (dense and dense[0] == dense[-1]) else dense
        N_open = len(dense_open)
        # LEFT normal (-dy, dx) points into the interior iff the ring is CCW
        # (positive shoelace); flip for a CW ring so the ribbon offsets inward.
        _sa = 0.0
        for k in range(N_open):
            x0, y0 = dense_open[k]
            x1, y1 = dense_open[(k + 1) % N_open]
            _sa += x0 * y1 - x1 * y0
        inward_sign = 1.0 if _sa > 0.0 else -1.0
        vertex_perp: list[tuple[float, float]] = []
        for k in range(N_open):
            p_prev = dense_open[(k - 1) % N_open]
            p_cur = dense_open[k]
            p_next = dense_open[(k + 1) % N_open]
            dx_in = p_cur[0] - p_prev[0]
            dy_in = p_cur[1] - p_prev[1]
            Lin = math.hypot(dx_in, dy_in)
            if Lin < 1e-9:
                px_in = py_in = 0.0
            else:
                px_in = -dy_in / Lin
                py_in = dx_in / Lin
            dx_out = p_next[0] - p_cur[0]
            dy_out = p_next[1] - p_cur[1]
            Lout = math.hypot(dx_out, dy_out)
            if Lout < 1e-9:
                px_out = py_out = 0.0
            else:
                px_out = -dy_out / Lout
                py_out = dx_out / Lout
            avg_x = (px_in + px_out) / 2.0
            avg_y = (py_in + py_out) / 2.0
            L_avg = math.hypot(avg_x, avg_y)
            if L_avg < 1e-9:
                avg_x = px_in if Lin > 0 else px_out
                avg_y = py_in if Lin > 0 else py_out
            vertex_perp.append(
                (avg_x * strip_width_m * inward_sign,
                 avg_y * strip_width_m * inward_sign))
        n_pairs = len(dense) - 1
        for i in range(n_pairs):
            out.append((dense[i], dense[i + 1],
                        vertex_perp[i % N_open],
                        vertex_perp[(i + 1) % N_open]))
    return out


def _compute_boundary_ribbon_interior(
        layout: "PavementLayout",
        strip_half_width_m: float = BOUNDARY_STRIP_HALF_WIDTH_M,
        densify_step_m: float = 15.0):
    """Airport-interior region (``airport_boundary`` minus the ribbon band),
    computed PRE-solve without altitudes.

    The ribbon polygon footprint is altitude-independent
    (see :func:`_ribbon_segment_geometry`), so this reproduces exactly the
    band the post-solve ribbon emit will occupy.  Used to clip airside
    pavement to the ribbon inner edge BEFORE the solve (refactor Phase 3)
    so the clipped pavement is graded by the solver and tiles conformingly
    with the ribbon emitted later.  Returns the interior ``Polygon`` /
    ``MultiPolygon``, or ``None`` when there is no boundary."""
    ab = getattr(layout, "airport_boundary", None)
    if ab is None or ab.is_empty:
        return None
    quads: list[Polygon] = []
    for p0, p1, perp0, perp1 in _ribbon_segment_geometry(
            ab, strip_half_width_m, densify_step_m):
        corners = [
            (p0[0], p0[1]),
            (p1[0], p1[1]),
            (p1[0] + perp1[0], p1[1] + perp1[1]),
            (p0[0] + perp0[0], p0[1] + perp0[1]),
        ]
        try:
            poly = Polygon(corners)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
        except _GEOM_EXC:
            continue
        quads.append(poly)
    if not quads:
        return None
    try:
        footprint = unary_union(quads)
        interior = ab.difference(footprint)
    except _GEOM_EXC:
        return None
    if interior is None or interior.is_empty:
        return None
    return interior


def _emit_airport_boundary_shape(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        strip_half_width_m: float = BOUNDARY_STRIP_HALF_WIDTH_M,
        runway_clamp_radius_m: float = 400.0,
        runway_clamp_grade: float = 0.03,
        densify_step_m: float = 15.0,
        ) -> int:
    """Emit a node_altitudes polygon tracing the airport boundary
    (apt.dat row-130) at ``2 × strip_half_width_m`` width.

    Per user 2026-04-28: an airport-perimeter "ribbon" with
    controlled per-vertex altitudes provides the elevation
    transition between the airport's pavement and the surrounding
    DEM.  Vertices within ``runway_clamp_radius_m`` of any runway
    are clamped to the runway elevation ± ``runway_clamp_grade``
    × distance (default 3 % grade); vertices beyond the radius
    follow the DEM directly.

    Implementation:
      1. Buffer the boundary's exterior LineString by
         ``strip_half_width_m`` to produce a closed strip polygon.
         The strip naturally has an interior ring (the airport
         interior shrunk inward by the buffer).
      2. Decompose the holed strip into simple non-holed pieces
         via ``_decompose_polygon_with_holes`` so X-Plane's patch
         parser (which drops interior rings) renders the strip
         correctly.
      3. For each piece, densify boundary segments to
         ``densify_step_m`` so per-vertex altitude clamping
         resolves at a useful spatial frequency.
      4. Compute per-vertex altitudes against the runway-distance
         rule + DEM.
      5. Append each piece as a ``ROLE_BOUNDARY`` BuiltShape.

    Returns the number of boundary shape pieces emitted.
    """
    from .pipeline import _load_osm_airports, _load_osm_big_roads
    if layout.airport_boundary is None or layout.airport_boundary.is_empty:
        return 0
    from shapely.geometry import LineString as _LS, Polygon as _Polygon
    from shapely.geometry import Point as _Point
    from shapely.ops import nearest_points as _nearest_points

    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH
    def m_to_ll(x: float, y: float) -> tuple[float, float]:
        lat = lat0 + math.degrees(y / R)
        lon = lon0 + math.degrees(x / (R * cos0))
        return lat, lon

    # Pre-collect airside-pavement polygons (runway/taxiway/junction/
    # apron) + their elevation samplers for the per-vertex distance /
    # clamp lookup.
    pavement_shapes: list[BuiltShape] = _collect_clamp_pavement(layout)
    if not pavement_shapes:
        return 0

    def _runway_clamped_alt(x: float, y: float) -> float | None:
        # Delegates to the module-level single source of truth so the
        # ribbon and the DEM bridge share identical clamp altitudes.
        return _runway_clamped_alt_at(
            x, y, dem=dem, tile_lat=tile_lat, tile_lon=tile_lon,
            pavement_shapes=pavement_shapes, m_to_ll=m_to_ll,
            clamp_radius_m=runway_clamp_radius_m,
            clamp_grade=runway_clamp_grade)

    # Per user 2026-05-12: emit the boundary as a CHAIN OF 4-corner
    # rectangles (one per densified boundary segment) instead of a
    # single buffered strip polygon.  Each rect is either flat
    # (single ``altitude=`` tag) or sloped (``altitude_high`` /
    # ``altitude_low`` with the [high, low, low, high] corner
    # convention), so debug tools like JOSM can read the altitude
    # profile along the perimeter directly off each rect's tags.
    boundary_geom = layout.airport_boundary

    def _rect_for_segment(
            p0: tuple[float, float],
            p1: tuple[float, float],
            alt0: float, alt1: float,
            perp0: tuple[float, float],
            perp1: tuple[float, float],
            ) -> tuple[Polygon, float | None, float] | None:
        """Build a 4-corner rect spanning the boundary segment
        p0 → p1.  ``perp0`` / ``perp1`` are PER-VERTEX perpendicular
        offsets (already scaled by half-width) so the rect uses the
        SAME perpendicular at p0 as the preceding rect did at its
        p1 — i.e., adjacent rects share their flat (cross) edge
        nodes exactly.

        Convention: corners 0, 3 at the HIGH-altitude end, corners
        1, 2 at the LOW end (matches runway segment emit).
        Returns ``(polygon, altitude_high, altitude_low, seg_high,
        seg_low)`` where ``seg_high``/``seg_low`` are the segment
        endpoints (after any high/low swap) defining the slope axis —
        used to re-derive per-vertex altitudes if the rect is later
        reshaped into a non-quad.  ``altitude_high=None`` for flat
        segments.
        """
        # Order so p0 is the HIGH end (alt0 >= alt1).  When swapping,
        # swap the perpendiculars too so each corner gets its
        # vertex's perp.
        if abs(alt0 - alt1) < 0.1:
            eh: float | None = None
            el = round((alt0 + alt1) / 2.0, 1)
        elif alt0 >= alt1:
            eh = round(alt0, 1)
            el = round(alt1, 1)
        else:
            p0, p1 = p1, p0
            alt0, alt1 = alt1, alt0
            perp0, perp1 = perp1, perp0
            eh = round(alt0, 1)
            el = round(alt1, 1)
        dx = p1[0] - p0[0]
        dy = p1[1] - p0[1]
        L = math.hypot(dx, dy)
        if L < 0.5:
            return None
        # The ribbon lies ENTIRELY INSIDE row-130 (user 2026-05-22): the
        # OUTER long edge sits ON the boundary line (offset 0), the INNER
        # long edge is offset inward by the full strip width.  ``perp0`` /
        # ``perp1`` are the inward (interior-pointing) per-vertex offsets.
        corners = [
            (p0[0], p0[1]),                          # 0 high-outer (on line)
            (p1[0], p1[1]),                          # 1 low-outer  (on line)
            (p1[0] + perp1[0], p1[1] + perp1[1]),    # 2 low-inner
            (p0[0] + perp0[0], p0[1] + perp0[1]),    # 3 high-inner
        ]
        try:
            poly = _Polygon(corners)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.geom_type != "Polygon":
                return None
        except _GEOM_EXC:
            return None
        return poly, eh, el, p0, p1

    # Walk the densified boundary as consecutive segment pairs (geometry
    # via the shared :func:`_ribbon_segment_geometry`), emitting one rect
    # per pair.  The per-vertex perpendiculars make adjacent rects share
    # their flat (cross) edge nodes exactly — and reproduce the SAME band
    # the pre-solve clip (``_compute_boundary_ribbon_interior``) used.
    n_emitted = 0
    n_at_dem = 0
    band_union = None    # running union of emitted rects (overlap guard)
    # AT-DEM SUPPRESSION (user 2026-07-03): the ribbon exists to ADJUST
    # terrain where the airport was graded/excavated/filled (the clamp
    # lifted it off raw DEM).  Where both rect ends sit AT raw DEM the rect
    # merely restates the terrain X-Plane already renders — skip it (the
    # boundary ribbon was 24 % of SPJC's emitted vertices).  Rects near
    # pavement are always kept: the ribbon-seam pass adopts pavement
    # altitudes onto them (the pavement↔terrain interface).
    _skip_at_dem = os.environ.get("O4_BOUNDARY_SKIP_AT_DEM", "1") == "1"
    _AT_DEM_TOL_M = 0.05
    _KEEP_NEAR_PAV_M = 30.0
    _pav_u_prep = None
    if _skip_at_dem:
        try:
            from shapely.prepared import prep as _prep
            _pav_u = unary_union(
                [s.polygon for s in pavement_shapes
                 if s.polygon is not None and not s.polygon.is_empty])
            _pav_u_prep = (_prep(_pav_u.buffer(_KEEP_NEAR_PAV_M))
                           if _pav_u is not None and not _pav_u.is_empty
                           else None)
        except _GEOM_EXC:
            _pav_u_prep = None

    def _raw_dem_at(x: float, y: float):
        try:
            lat, lon = m_to_ll(x, y)
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None

    for p0, p1, perp0, perp1 in _ribbon_segment_geometry(
            boundary_geom, strip_half_width_m, densify_step_m):
        a0 = _runway_clamped_alt(p0[0], p0[1])
        a1 = _runway_clamped_alt(p1[0], p1[1])
        if (_skip_at_dem and a0 is not None and a1 is not None):
            d0 = _raw_dem_at(p0[0], p0[1])
            d1 = _raw_dem_at(p1[0], p1[1])
            if (d0 is not None and d1 is not None
                    and abs(float(a0) - float(d0)) <= _AT_DEM_TOL_M
                    and abs(float(a1) - float(d1)) <= _AT_DEM_TOL_M):
                from shapely.geometry import Point as _MidPt
                mid = _MidPt(0.5 * (p0[0] + p1[0]), 0.5 * (p0[1] + p1[1]))
                if _pav_u_prep is None or not _pav_u_prep.intersects(mid):
                    n_at_dem += 1
                    continue
        if a0 is None or a1 is None:
            # Both DEM and runway-clamp returned None for at least one
            # endpoint — we genuinely don't know the altitude here.  Per
            # ``feedback_boundary_clamp_asymmetric``, the clamp lifts UP
            # toward the runway only, so missing data cannot be silently
            # replaced with sea level; that produces a multi-hundred-metre
            # cliff at any non-coastal airport.  Skip the rect.
            UI.vprint(1,
                "  [pav-builder] boundary rect skipped: altitude "
                f"unresolvable at p0={p0} (a0={a0}) p1={p1} (a1={a1})")
            continue
        built = _rect_for_segment(p0, p1, float(a0), float(a1),
                                   perp0, perp1)
        if built is None:
            continue
        poly, eh, el, seg_high, seg_low = built
        # Overlap guard (HEAZ #448∩#449/#450, 2026-06-09): at a sharp
        # boundary corner — a needle apex, or two near-coincident
        # corners turning sharply within less than the band width — the
        # band's miter is longer than the adjacent segment, so the next
        # rect FOLDS back over the previously emitted one and the ribbon
        # double-covers (boundary∩boundary self-overlap).  Subtract the
        # already-emitted band from each new rect so the ribbon tiles
        # overlap-free by construction; a clipped remainder re-derives
        # its per-vertex altitudes via the non-quad path below.
        pieces = [poly]
        if band_union is not None:
            try:
                if poly.intersection(band_union).area > 0.01:
                    diff = poly.difference(band_union)
                    pieces = [
                        g for g in getattr(diff, "geoms", [diff])
                        if (g.geom_type == "Polygon" and not g.is_empty
                            and g.area >= 0.5)]
                    if not pieces:
                        continue   # fully under the already-emitted band
            except _GEOM_EXC:
                pass
        try:
            band_union = (poly if band_union is None
                          else band_union.union(poly))
        except _GEOM_EXC:
            pass
        # No pavement self-trim here: the ribbon now owns the outer
        # ``strip_width_m`` band exclusively, and pavement is clipped
        # back to the ribbon's inner edge by
        # ``_clip_pavement_to_boundary_interior`` so the two tile
        # conformingly (shared inner-edge nodes, no slivers).
        for piece in pieces:
            shape = BuiltShape(
                polygon=piece,
                role=ROLE_BOUNDARY,
                ref="airport_boundary",
            )
            if eh is None:
                shape.altitude = el
            else:
                # A buffer(0) repair in ``_rect_for_segment`` (or the
                # overlap clip above) can turn the 4-corner sloped quad
                # into a non-quad.  ``altitude_high``/``altitude_low`` is
                # only valid on a closed 4-corner quad — Ortho4XP rejects
                # anything else ("Wrong number of nodes ...
                # altitude_high/altitude_low polygon, skipped").  For a
                # non-quad, preserve the along-perimeter slope as
                # per-vertex ``node_altitudes`` (linear eh->el
                # interpolation along the high->low segment axis) rather
                # than dropping it or flattening.
                ring = list(piece.exterior.coords)
                open_ring = (ring[:-1]
                             if (ring and ring[0] == ring[-1]) else ring)
                if len(open_ring) == 4:
                    shape.altitude_high = eh
                    shape.altitude_low = el
                else:
                    shape.node_altitudes = (
                        _node_altitudes_from_segment_slope(
                            open_ring, seg_high, seg_low, eh, el))
            layout.shapes.append(shape)
            n_emitted += 1
    if n_at_dem:
        UI.vprint(1,
            f"  [pav-builder] boundary ribbon: skipped {n_at_dem} "
            f"at-DEM rect(s) (terrain already there; ±{_AT_DEM_TOL_M} m).")
    return n_emitted


# Roles exempt from the boundary-interior clip: they ARE the perimeter
# band (the ribbon) or its DEM transition (the bridge) and legitimately
# reach the row-130 line / lie within the ribbon band by design.
_BOUNDARY_CLIP_EXEMPT_ROLES = {ROLE_BOUNDARY}


def find_boundary_crossings(
        layout: "PavementLayout",
        tol_area_m2: float = 1.0) -> list[BuiltShape]:
    """Return the non-boundary shapes that STRADDLE the airport boundary
    (apt.dat row-130) — more than ``tol_area_m2`` of footprint on BOTH
    sides of the line.

    The invariant (user 2026-05-22): no emitted shape may cross the
    airport boundary — a shape must be either entirely inside row-130 or
    entirely outside it.  ``_clip_pavement_to_boundary_interior`` enforces
    it for crossing shapes (clipping them back to the ribbon's inner edge,
    which is itself inside row-130); this is the check.  An empty result
    means the invariant holds.

    Shapes lying ENTIRELY outside row-130 are NOT crossings: modeled
    external features (tunnel entrance ramps and their retaining walls)
    legitimately live beyond the boundary and are kept untouched.  The
    boundary ribbon / DEM bridge are exempt — they are the perimeter band
    itself.
    """
    ab = getattr(layout, "airport_boundary", None)
    if ab is None or ab.is_empty:
        return []
    out: list[BuiltShape] = []
    for s in layout.shapes:
        if s.role in _BOUNDARY_CLIP_EXEMPT_ROLES:
            continue
        p = s.polygon
        if p is None or p.is_empty or p.geom_type != "Polygon":
            continue
        try:
            if (p.difference(ab).area > tol_area_m2
                    and p.intersection(ab).area > tol_area_m2):
                out.append(s)
        except _GEOM_EXC:
            continue
    return out


def _largest_polygon(geom):
    """Return the largest-area ``Polygon`` member of ``geom`` (which may
    be a Polygon / MultiPolygon / GeometryCollection), or None."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return geom
    best = None
    best_a = 0.0
    for g in getattr(geom, "geoms", ()):
        if g.geom_type == "Polygon" and not g.is_empty and g.area > best_a:
            best, best_a = g, g.area
    return best


def _shape_open_alts(s: BuiltShape, n: int) -> list[float] | None:
    """Per-(open-ring-)vertex altitudes for ``s`` (length ``n``), or None.

    Mirrors ``conformance._vertex_alts`` so a reshaped shape can be
    re-emitted as ``node_altitudes`` preserving its altitude field."""
    na = s.node_altitudes
    if na is not None:
        a = list(na)
        if len(a) == n + 1 and a[0] == a[-1]:
            a = a[:-1]
        if len(a) == n:
            return a
        return None
    if (s.altitude_high is not None
            and s.altitude_low is not None and n == 4):
        return corner_alts_from_high_low(s.altitude_high, s.altitude_low)
    if s.altitude is not None:
        return [float(s.altitude)] * n
    return None


def _flatten_bridge_pinch_necks(
        layout: "PavementLayout",
        pinch_tol_m: float = 1.6,
        min_dalt_m: float = 0.5,
        *, icao: str = "") -> int:
    """Flatten torn vertical slivers where a DEM-bridge ribbon necks.

    A ``boundary_dem_bridge`` ribbon spans from the airport perimeter strip
    (OUTER edge, boundary-clamped altitude) to the abutting pavement (INNER
    edge, pavement altitude).  Where the pavement crowds right up against
    the perimeter, the ribbon necks to near-zero width and a bridge-only
    inner vertex (pavement altitude) ends up within ~1 m of a perimeter-
    strip vertex (clamped altitude) it does NOT share a node with —
    different canonical points beyond the emit weld tolerance.  The result
    is a sub-metre footprint spanning several metres of altitude, which
    X-Plane renders as a torn vertical sliver that z-fights the flat
    perimeter strip (KGCD bridges #461/#463/#466: 0.7–1.3 m apart,
    1.2–4.2 m tall, around the runway near shapeID 9).

    The pinch vertex is introduced AFTER ``_emit_boundary_dem_bridge`` by
    conformance / contact insertion (an abutting pavement vertex grafted
    onto the bridge ring at the pavement altitude), so this runs as a FINAL
    pass.  At a neck the bridge has no appreciable area, so snapping the
    bridge vertex's altitude to its near-coincident perimeter-strip vertex
    removes the wall with no visible change.  Altitude-only: geometry (and
    the conformance invariant) are untouched; the perimeter strip — which
    owns the shared node by consensus — never moves.

    Returns the number of bridge vertices flattened.
    """
    # Spatial index of perimeter-strip vertices (the authoritative,
    # consensus-shared boundary nodes — NOT other bridges).
    cell = max(pinch_tol_m, 1.0)
    grid: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
    for s in layout.shapes:
        if (s.role != ROLE_BOUNDARY or s.ref != "airport_boundary"
                or s.polygon is None or s.polygon.is_empty
                or not s.polygon.exterior):
            continue
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        alts = _shape_open_alts(s, len(coords))
        if alts is None:
            continue
        for (x, y), a in zip(coords, alts):
            grid.setdefault(
                (int(x // cell), int(y // cell)), []).append(
                    (float(x), float(y), float(a)))
    if not grid:
        return 0

    def _nearest_strip_alt(x: float, y: float) -> float | None:
        gx, gy = int(x // cell), int(y // cell)
        best_d2 = pinch_tol_m * pinch_tol_m
        best_a: float | None = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (sx, sy, sa) in grid.get((gx + dx, gy + dy), ()):  # type: ignore[arg-type]
                    d2 = (x - sx) ** 2 + (y - sy) ** 2
                    if d2 <= best_d2:
                        best_d2 = d2
                        best_a = sa
        return best_a

    n_fixed = 0
    for s in layout.shapes:
        if (s.role != ROLE_BOUNDARY or s.ref != "boundary_dem_bridge"
                or not s.node_altitudes or s.polygon is None
                or s.polygon.is_empty or not s.polygon.exterior):
            continue
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        na = list(s.node_altitudes)
        closed = len(na) == len(coords) + 1 and na[0] == na[-1]
        if len(na) < len(coords):
            continue
        changed = False
        for i, (x, y) in enumerate(coords):
            sa = _nearest_strip_alt(x, y)
            if sa is not None and abs(na[i] - sa) > min_dalt_m:
                na[i] = round(sa, 1)
                changed = True
                n_fixed += 1
        if changed:
            if closed:
                na[len(coords)] = na[0]
            s.node_altitudes = na
    return n_fixed


def _clip_pavement_to_boundary_interior(
        layout: "PavementLayout", *, icao: str = "",
        interior=None,
        roles: frozenset | set | None = None,
        skip_roles: frozenset | set | None = None,
        ) -> tuple[int, int]:
    """Clip pavement shapes that STRADDLE the airport boundary back to the
    airport-interior region bounded by the boundary ribbon's INNER edge
    (user 2026-05-22: only shapes that touch/cross the boundary are
    reshaped; nothing is dropped).

    ``interior`` (refactor Phase 3): when given, clip against this
    precomputed interior region instead of deriving it from the emitted
    ribbon shapes.  The pre-solve airside clip passes the geometric interior
    from :func:`_compute_boundary_ribbon_interior` (the ribbon footprint is
    altitude-independent), so the clipped pavement is graded by the solver
    yet still tiles conformingly with the post-solve-emitted ribbon.  When
    ``None`` (post-solve call), the interior is derived from the emitted
    ribbon shapes as before.

    ``roles`` / ``skip_roles``: restrict the pass to / exclude these roles.
    The pre-solve call clips only airside roles; the post-solve call skips
    airside (already clipped pre-solve) and handles the remaining
    post-solve-emitted features (clearance, etc.).

    The relocated ribbon (entirely inside row-130, see
    ``_emit_airport_boundary_shape``) owns the outer ``strip_width_m``
    band; a shape that crosses into that band is clipped back to the
    ribbon's inner edge.  The clip uses ``intersection(interior)`` where
    ``interior = airport_boundary − ribbon_union``, so the clipped edge
    follows the ribbon inner edge and inherits its vertices EXACTLY — a
    conforming seam, no T-junction slivers (the dominant Triangle4XP
    load-time cost).

    A shape that lies ENTIRELY OUTSIDE the interior is left UNTOUCHED, not
    dropped: modeled features that legitimately live beyond the airport
    boundary — tunnel entrance/portal ramp chains (which descend under and
    past the boundary) and their retaining walls — must survive (SPJC: 15
    tunnel-ramp + 30 retaining-wall polygons).  They sit beyond the ribbon
    so they neither abut the perimeter seam nor reintroduce its slivers.
    (Foreign-airport pavement in closely-spaced tiles is already removed
    upstream at collection time, not here — see the DSF/OSM boundary gate
    in ``pipeline``.)

    Altitudes are re-derived for each clipped ring via
    ``_resample_node_altitudes_nn`` (edge-interpolation along the old
    ring), so pavement keeps its own graded altitude field at the seam
    (the ribbon yields to it — see ``_conform_ribbon_to_pavement_seam``).
    Flat ``altitude=`` shapes keep their constant altitude (valid for any
    node count).

    Returns ``(shapes_clipped, shapes_left_outside)``.
    """
    ab = getattr(layout, "airport_boundary", None)
    if ab is None or ab.is_empty:
        return 0, 0
    if interior is None:
        ribbon = [s.polygon for s in layout.shapes
                  if s.role == ROLE_BOUNDARY and s.ref == "airport_boundary"
                  and s.polygon is not None and not s.polygon.is_empty]
        try:
            ribbon_u = unary_union(ribbon) if ribbon else None
            interior = ab.difference(ribbon_u) if ribbon_u is not None else ab
        except _GEOM_EXC:
            return 0, 0
    if interior is None or interior.is_empty:
        return 0, 0

    AREA_EPS = 0.5  # m^2 — ignore sub-tolerance differences
    clipped = 0
    left_outside = 0
    for s in layout.shapes:
        if roles is not None and s.role not in roles:
            continue
        if skip_roles is not None and s.role in skip_roles:
            continue
        if s.role in _BOUNDARY_CLIP_EXEMPT_ROLES:
            continue
        p = s.polygon
        if p is None or p.is_empty or p.geom_type != "Polygon":
            continue
        try:
            outside = p.difference(interior)
        except _GEOM_EXC:
            continue
        if outside.is_empty or outside.area < AREA_EPS:
            continue  # already inside the seam; no clip needed
        try:
            inter = p.intersection(interior)
        except _GEOM_EXC:
            continue
        new_poly = _largest_polygon(inter)
        if new_poly is None or new_poly.area < AREA_EPS:
            # Entirely outside the boundary — a modeled external feature
            # (tunnel ramp / retaining wall).  Keep it as-is.
            left_outside += 1
            continue
        old_open = list(p.exterior.coords)
        if old_open and old_open[0] == old_open[-1]:
            old_open = old_open[:-1]
        if (s.altitude is not None and s.node_altitudes is None
                and s.altitude_high is None):
            # Flat shape: a constant ``altitude=`` is valid for any node
            # count, so just replace the geometry.
            s.polygon = new_poly
        else:
            alts_open = _shape_open_alts(s, len(old_open))
            old_closed = (alts_open + [alts_open[0]]
                          if alts_open else None)
            new_alts = _resample_node_altitudes_nn(
                new_poly, old_open, old_closed)
            s.polygon = new_poly
            if new_alts is not None:
                s.node_altitudes = new_alts
                s.altitude_high = None
                s.altitude_low = None
        clipped += 1
    return clipped, left_outside


def _conform_pavement_to_ribbon_inner_corners(
        layout: "PavementLayout", *,
        roles: frozenset | set,
        strip_half_width_m: float = BOUNDARY_STRIP_HALF_WIDTH_M,
        densify_step_m: float = 15.0,
        tol: float = SHARED_VERTEX_TOL_M) -> int:
    """Pre-solve seam conformance for pavement that HUGS the boundary
    ribbon's inner edge without straddling it.

    ``_clip_pavement_to_boundary_interior`` only reshapes shapes that
    actually cross into the ribbon band; a shape whose ring runs just
    INSIDE the band's inner edge (closer than ``tol`` but never outside)
    is left untouched, so the ribbon rects emitted post-solve drop their
    inner-corner nodes mid-edge onto it — one residual T-junction per
    densify step (HEAZ apron #29: a 168 m edge hugging the seam at
    0.07–0.5 m collected 7).  Post-solve conformance cannot repair this:
    airside is frozen after the solve (geom-guard), and vertices may only
    be inserted into feature shapes.

    Fix at the source, PRE-solve: re-route any ring edge that passes
    within ``tol`` of a ribbon inner-corner node THROUGH that node.  The
    corners come from the shared :func:`_ribbon_segment_geometry`, so they
    are bit-identical to the corners the post-solve ribbon emit produces;
    between two consecutively adopted corners the re-routed edge IS the
    ribbon's inner edge — the sub-tolerance sliver gap closes and the
    seam shares nodes exactly.  Corners within ``tol`` of an existing
    ring vertex are skipped (vertex-near-endpoint is not a T-junction,
    and moving an existing vertex here would race the weld pass).

    Returns the number of shapes reshaped.
    """
    ab = getattr(layout, "airport_boundary", None)
    if ab is None or ab.is_empty:
        return 0
    corners: list[tuple[float, float]] = []
    seen: set[tuple[float, float]] = set()
    for p0, p1, perp0, perp1 in _ribbon_segment_geometry(
            ab, strip_half_width_m, densify_step_m):
        for c in ((p0[0] + perp0[0], p0[1] + perp0[1]),
                  (p1[0] + perp1[0], p1[1] + perp1[1])):
            if c not in seen:
                seen.add(c)
                corners.append(c)
    if not corners:
        return 0
    from collections import defaultdict
    cell = max(densify_step_m, 4.0 * tol)
    grid: dict = defaultdict(list)
    for c in corners:
        grid[(int(c[0] // cell), int(c[1] // cell))].append(c)

    def _corners_near_edge(ax, ay, bx, by):
        i0 = int((min(ax, bx) - tol) // cell)
        i1 = int((max(ax, bx) + tol) // cell)
        j0 = int((min(ay, by) - tol) // cell)
        j1 = int((max(ay, by) + tol) // cell)
        out = []
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                out.extend(grid.get((i, j), ()))
        return out

    n_shapes = 0
    for s in layout.shapes:
        if s.role not in roles:
            continue
        p = s.polygon
        if p is None or p.is_empty or p.geom_type != "Polygon":
            continue
        ring = list(p.exterior.coords)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) < 3:
            continue
        old_open = list(ring)
        ring_set = set(ring)
        changed = False
        # Fixpoint: adopting one corner shifts the neighbouring sub-edges
        # outward, which can bring the NEXT corner inside ``tol``.
        for _ in range(16):
            inserted = False
            n = len(ring)
            for i in range(n):
                ax, ay = ring[i]
                bx, by = ring[(i + 1) % n]
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy
                if L2 < 1e-12:
                    continue
                adds: list[tuple[float, tuple[float, float]]] = []
                for c in _corners_near_edge(ax, ay, bx, by):
                    if c in ring_set:
                        continue
                    cx, cy = c
                    if (math.hypot(cx - ax, cy - ay) < tol
                            or math.hypot(cx - bx, cy - by) < tol):
                        continue   # near an existing vertex: not a TJ
                    t = ((cx - ax) * dx + (cy - ay) * dy) / L2
                    if t <= 0.0 or t >= 1.0:
                        continue
                    perp = abs((cx - ax) * dy - (cy - ay) * dx) / math.sqrt(L2)
                    if perp >= tol:
                        continue
                    adds.append((t, c))
                if not adds:
                    continue
                adds.sort()
                ring[i + 1:i + 1] = [c for _, c in adds]
                ring_set.update(c for _, c in adds)
                inserted = True
                changed = True
                break   # ring re-indexed; restart the edge scan
            if not inserted:
                break
        if not changed:
            continue
        try:
            new_poly = Polygon(ring)
            if not new_poly.is_valid:
                new_poly = new_poly.buffer(0)
            if (new_poly.is_empty or new_poly.geom_type != "Polygon"
                    or abs(new_poly.area - p.area)
                    > len(ring) * tol * densify_step_m):
                continue   # re-route went wrong; keep the original
        except _GEOM_EXC:
            continue
        if (s.altitude is not None and s.node_altitudes is None
                and s.altitude_high is None):
            s.polygon = new_poly
        else:
            alts_open = _shape_open_alts(s, len(old_open))
            old_closed = (alts_open + [alts_open[0]] if alts_open else None)
            new_alts = _resample_node_altitudes_nn(
                new_poly, old_open, old_closed)
            s.polygon = new_poly
            if new_alts is not None:
                s.node_altitudes = new_alts
                s.altitude_high = None
                s.altitude_low = None
        n_shapes += 1
    return n_shapes


def _collect_shape_nodes(layout, predicate
                         ) -> list[tuple[float, float, float]]:
    """Collect ``(x, y, alt)`` for every exterior vertex of each shape
    matching ``predicate`` that carries a usable altitude model."""
    out: list[tuple[float, float, float]] = []
    for s in layout.shapes:
        if not predicate(s):
            continue
        p = s.polygon
        if p is None or p.is_empty or p.geom_type != "Polygon":
            continue
        ring = list(p.exterior.coords)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        alts = _shape_open_alts(s, len(ring))
        if alts is None:
            continue
        for (x, y), a in zip(ring, alts):
            out.append((x, y, float(a)))
    return out


def _yield_seam_altitude(layout, taker_pred,
                         giver_nodes: list[tuple[float, float, float]],
                         tol: float) -> int:
    """For each ``taker`` shape (matching ``taker_pred``), adopt the
    altitude of the coincident ``giver`` node (within ``tol``) at every
    vertex, leaving non-coincident vertices untouched.  A taker that
    changes is re-emitted as ``node_altitudes``.  Returns the number of
    taker shapes adjusted.

    This is the per-node "yield" used to keep the perimeter seam flush:
    the ribbon yields to pavement, then the DEM bridge yields to the
    (already-yielded) ribbon — a one-way altitude cascade pavement →
    ribbon → bridge so there is no vertical wall anywhere along it.
    """
    if not giver_nodes:
        return 0
    from collections import defaultdict
    cell = 5.0
    grid: dict = defaultdict(list)
    for x, y, a in giver_nodes:
        grid[(int(x / cell), int(y / cell))].append((x, y, a))
    tol2 = tol * tol

    def _nearest(x: float, y: float) -> float | None:
        bi, bj = int(x / cell), int(y / cell)
        best_a: float | None = None
        best_d2 = tol2
        for i in (bi - 1, bi, bi + 1):
            for j in (bj - 1, bj, bj + 1):
                for px, py, pa in grid.get((i, j), ()):
                    d2 = (px - x) * (px - x) + (py - y) * (py - y)
                    if d2 < best_d2:
                        best_d2 = d2
                        best_a = pa
        return best_a

    n = 0
    for s in layout.shapes:
        if not taker_pred(s):
            continue
        p = s.polygon
        if p is None or p.is_empty or p.geom_type != "Polygon":
            continue
        ring = list(p.exterior.coords)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        base = _shape_open_alts(s, len(ring))
        if base is None:
            continue
        new = list(base)
        changed = False
        for idx, (x, y) in enumerate(ring):
            ga = _nearest(x, y)
            if ga is not None and abs(ga - new[idx]) > 0.05:
                new[idx] = ga
                changed = True
        if not changed:
            continue
        s.node_altitudes = new + [new[0]]
        s.altitude_high = None
        s.altitude_low = None
        s.altitude = None
        n += 1
    return n


def _conform_ribbon_to_pavement_seam(
        layout: "PavementLayout",
        tol: float = SHARED_VERTEX_TOL_M) -> int:
    """Make the perimeter seam flush by a one-way altitude cascade
    pavement → ribbon → DEM bridge (user 2026-05-22: pavement and ribbon
    must match node AND elevation 1:1; the ribbon is the transition
    strip, so it bends to the pavement — and the bridge in turn bends to
    the ribbon, since its trimmed outer edge sits on the ribbon's inner
    edge).

    After ``_clip_pavement_to_boundary_interior`` + ``enforce_conformance``
    the pavement edge runs along the ribbon's inner edge sharing its
    nodes bidirectionally; the bridge's outer edge (trimmed against the
    ribbon) shares the ribbon's inner-edge nodes too.  So:
      1. ribbon vertices coincident with a pavement node adopt the
         pavement altitude (vertices on the row-130 line / facing open
         terrain keep their clamped/DEM value);
      2. bridge vertices coincident with a (now-yielded) ribbon node
         adopt the ribbon altitude (the bridge's inner DEM edge, which
         shares no ribbon node, is untouched and still transitions to
         DEM).

    MUST run AFTER ``enforce_conformance`` so it covers the seam vertices
    that pass inserts (those would otherwise carry an interpolated
    altitude, not the neighbour's → a wall).

    Returns the number of boundary shapes (ribbon + bridge) adjusted.
    """
    pav_nodes = _collect_shape_nodes(
        layout, lambda s: s.role != ROLE_BOUNDARY)
    n_rib = _yield_seam_altitude(
        layout,
        lambda s: s.role == ROLE_BOUNDARY and s.ref == "airport_boundary",
        pav_nodes, tol)
    # Cascade: the bridge yields to the (now-final) ribbon inner edge.
    rib_nodes = _collect_shape_nodes(
        layout,
        lambda s: s.role == ROLE_BOUNDARY and s.ref == "airport_boundary")
    n_br = _yield_seam_altitude(
        layout,
        lambda s: s.role == ROLE_BOUNDARY and s.ref == "boundary_dem_bridge",
        rib_nodes, tol)
    return n_rib + n_br


def _emit_boundary_dem_bridge(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        gap_threshold_m: float = 5.0,
        bridge_depth_m: float = 100.0,
        densify_step_m: float = 15.0,
        runway_clamp_radius_m: float = 400.0,
        runway_clamp_grade: float = 0.03,
        ) -> int:
    """Emit a wider "bridge" polygon INSIDE the airport boundary
    where the boundary's clamped altitude differs from the raw DEM
    by more than ``gap_threshold_m``.

    Per user 2026-04-28: when the boundary ribbon is forced (by the
    runway-distance clamp at ≤ 3 % grade) to a value that disagrees
    with the natural terrain DEM by > 5 m, X-Plane renders a
    valley/cliff between the 5 m boundary ribbon and the surrounding
    terrain inside the airport perimeter.  The bridge polygon is a
    larger transition strip whose OUTER edge sits on the airport
    perimeter at the boundary's clamped altitude and whose INNER
    edge sits ``bridge_depth_m`` further inside the airport at the
    raw DEM altitude.  Per-vertex altitudes interpolate linearly
    between the two edges, giving X-Plane a gradual surface to
    descend / ascend over instead of a single hard step.

    OUTSIDE the airport boundary X-Plane keeps falling directly to
    DEM (no bridge needed there) — the user explicitly scoped this
    feature to the interior side only.

    Implementation:
      1. Densify the airport-boundary line to ≤ ``densify_step_m``.
      2. For each densified vertex, sample raw DEM and the
         runway-clamped altitude (same rule as the 5 m ribbon).
         Mark vertex if |gap| > ``gap_threshold_m``.
      3. Group consecutive marked vertices into "bridge runs"
         (with a 1-vertex slack so isolated unmarked vertices in
         the middle of a long gap don't split the run).
      4. For each run, build an inward-offset polygon
         (``bridge_depth_m`` inward from the boundary line) and
         clip it against any existing pavement / boundary ribbon.
      5. Emit per-vertex altitudes: outer edge = clamped, inner
         edge = DEM, with shape vertices on the boundary side
         tagged ``clamped`` and inner-edge vertices tagged DEM.
    """
    from .pipeline import _load_osm_airports, _load_osm_big_roads
    if (layout.airport_boundary is None
            or layout.airport_boundary.is_empty):
        return 0
    from shapely.geometry import LineString as _LS, Point as _Point
    from shapely.geometry import Polygon as _Polygon

    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH

    def m_to_ll(x: float, y: float) -> tuple[float, float]:
        lat = lat0 + math.degrees(y / R)
        lon = lon0 + math.degrees(x / (R * cos0))
        return lat, lon

    pavement_shapes: list[BuiltShape] = _collect_clamp_pavement(layout)
    if not pavement_shapes:
        return 0

    def _clamped_alt(x: float, y: float) -> float | None:
        # Delegates to the module-level single source of truth — the
        # SAME clamp the airport-boundary ribbon uses, so the bridge's
        # outer edge meets the ribbon's inner edge flush.
        return _runway_clamped_alt_at(
            x, y, dem=dem, tile_lat=tile_lat, tile_lon=tile_lon,
            pavement_shapes=pavement_shapes, m_to_ll=m_to_ll,
            clamp_radius_m=runway_clamp_radius_m,
            clamp_grade=runway_clamp_grade)

    def _dem_alt(x: float, y: float) -> float | None:
        try:
            lat, lon = m_to_ll(x, y)
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None

    boundary_geom = layout.airport_boundary
    if boundary_geom.geom_type == "Polygon":
        rings = [boundary_geom]
    elif boundary_geom.geom_type == "MultiPolygon":
        rings = list(boundary_geom.geoms)
    else:
        return 0

    # Compose existing PAVEMENT union (excluding ROLE_BOUNDARY
    # shapes — the just-emitted 5 m ribbon's centerline IS the
    # boundary line, so the ribbon would reject every boundary
    # vertex from the pre-filter below).  The bridge is meant to
    # avoid overlapping real pavement (runways / taxis / aprons /
    # terminals); it's placed alongside the ribbon, not on top of
    # other pavement.
    pavement_polys = [
        s.polygon for s in layout.shapes
        if s.role != ROLE_BOUNDARY
        and s.polygon is not None
        and not s.polygon.is_empty]
    emitted_pav_union: Polygon | None = None
    if pavement_polys:
        try:
            emitted_pav_union = unary_union(pavement_polys)
        except _GEOM_EXC:
            emitted_pav_union = None
    # Separately track the boundary ribbon — its centerline matches
    # the boundary line, so the bridge polygon overlaps the ribbon
    # in its inner 2.5 m by construction.  The bridge must be
    # trimmed against the ribbon to satisfy the no-self-overlap
    # geometry test.
    ribbon_polys = [
        s.polygon for s in layout.shapes
        if s.role == ROLE_BOUNDARY
        and s.ref == "airport_boundary"
        and s.polygon is not None
        and not s.polygon.is_empty]
    ribbon_union: Polygon | None = None
    if ribbon_polys:
        try:
            ribbon_union = unary_union(ribbon_polys)
        except _GEOM_EXC:
            ribbon_union = None

    # Pre-collect pavement EDGE points with altitudes — used for
    # nearest-pavement lookup when assigning per-vertex altitudes
    # to the bridge polygon.  Per user 2026-04-28: bridge vertices
    # adjacent to pavement must match the pavement's altitude (not
    # raw DEM) so the bridge actually FILLS the gap between
    # boundary and pavement instead of creating its own valley.
    pav_edge_pts: list[tuple[float, float, float]] = []
    for s in layout.shapes:
        if s.role == ROLE_BOUNDARY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        # Per-vertex altitudes for junctions / boundary; rect tags
        # for sloping rects.
        if s.role in (ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
                       ROLE_SECONDARY_PARALLEL, ROLE_STUB,
                       ROLE_CROSS_CONNECTOR):
            if (s.altitude_high is not None
                    and s.altitude_low is not None):
                # Sloped 4-corner rect — strict convention.
                if len(coords) != 4:
                    continue
                per = [s.altitude_high, s.altitude_low,
                       s.altitude_low, s.altitude_high]
            elif s.altitude is not None:
                # Flat shape: any number of corners (multi-node flat
                # runway shapes from the segmenter).
                if len(coords) < 4:
                    continue
                per = [float(s.altitude)] * len(coords)
            elif s.node_altitudes:
                # Seam-vertex-inserted runway: rect tags were
                # converted to per-vertex altitudes by
                # ``_insert_seam_vertices``.  Without this branch,
                # cross-tile builds drop the runway from
                # ``pav_edge_pts`` entirely, breaking the bridge's
                # nearest-pavement altitude lookup (root cause of
                # MMOX north-tile bridge dipping to valley DEM).
                per = [float(a) for a in
                       s.node_altitudes[:len(coords)]]
                if len(per) < len(coords):
                    continue
            else:
                continue
            for (x, y), a in zip(coords, per):
                pav_edge_pts.append((float(x), float(y), float(a)))
        elif s.node_altitudes:
            for (x, y), a in zip(coords,
                                  s.node_altitudes[:len(coords)]):
                pav_edge_pts.append((float(x), float(y), float(a)))
        elif s.altitude is not None:
            for x, y in coords:
                pav_edge_pts.append((float(x), float(y),
                                     float(s.altitude)))

    def _nearest_pav_alt(x: float, y: float,
                         max_d_m: float = 500.0
                         ) -> tuple[float, float] | None:
        """Return ``(alt, distance_m)`` for the nearest pavement
        edge point within ``max_d_m`` of ``(x, y)``, or None when
        no pavement is in range."""
        best_d2 = max_d_m * max_d_m
        best_alt: float | None = None
        for px, py, pa in pav_edge_pts:
            d2 = (x - px) * (x - px) + (y - py) * (y - py)
            if d2 < best_d2:
                best_d2 = d2
                best_alt = pa
        if best_alt is None:
            return None
        return (best_alt, math.sqrt(best_d2))

    def _bridge_alt(x: float, y: float) -> float | None:
        """Altitude for a bridge vertex.  Per
        ``feedback_boundary_clamp_asymmetric``: never let the bridge
        dip below the surrounding pavement.

        ``_clamped_alt``'s asymmetric runway clamp only acts within
        ``runway_clamp_radius_m`` (400 m).  For airports whose
        boundary extends well beyond that radius (e.g. MMOX, where
        the +17 tile's bridge geometry reaches ~1.4 km north of the
        runway tip), positions outside the radius fall back to raw
        DEM — which at a plateau airport like MMOX (1520 m) samples
        the surrounding valley (~360 m) and silently produced a
        ~1000 m bridge drop.

        Resolution order:
          1. Nearest pavement edge (linear scan, up to 5 km).  This
             keeps the bridge tied to airport elevation no matter
             how far the boundary extends from the runway.
          2. Asymmetric clamp (DEM lifted UP toward runway band).
          3. Raw DEM.
        Returns None only when none of the three are available.
        """
        near = _nearest_pav_alt(x, y, max_d_m=5000.0)
        if near is not None:
            return float(near[0])
        clamped = _clamped_alt(x, y)
        if clamped is not None:
            return float(clamped)
        return _dem_alt(x, y)

    n_emitted = 0
    for boundary_poly in rings:
        try:
            ext_coords = list(boundary_poly.exterior.coords)
        except _GEOM_EXC:
            continue
        if len(ext_coords) < 4:
            continue
        # Densify the boundary line.
        if ext_coords[0] == ext_coords[-1]:
            ext_coords = ext_coords[:-1]
        dense: list[tuple[float, float]] = []
        n = len(ext_coords)
        for i in range(n):
            ax, ay = ext_coords[i]
            bx, by = ext_coords[(i + 1) % n]
            dense.append((ax, ay))
            d = math.hypot(bx - ax, by - ay)
            if d > densify_step_m:
                steps = max(1, int(d / densify_step_m))
                for k in range(1, steps):
                    t = k / steps
                    dense.append((ax + t * (bx - ax),
                                  ay + t * (by - ay)))
        if len(dense) < 4:
            continue
        # Per-vertex clamped + DEM + gap.
        per_vert: list[tuple[float, float, float, float]] = []
        for x, y in dense:
            ca = _clamped_alt(x, y)
            da = _dem_alt(x, y)
            if ca is None or da is None:
                per_vert.append((x, y, float('nan'), float('nan')))
                continue
            per_vert.append((x, y, float(ca), float(da)))

        # A vertex is "needs-bridge" only if (a) the gap exceeds
        # the threshold AND (b) the boundary line at that vertex
        # is NOT already inside pavement (a runway / taxi rect
        # extending to the perimeter doesn't need a transition —
        # pavement is right there).  Pre-filtering on (b) avoids
        # building bridge polygons that overlap pavement; the
        # subsequent pavement-difference would otherwise leave
        # vertices stranded on sloping-rect edges (test
        # ``test_no_vertex_on_sloping_rect_edge``).
        from shapely.geometry import Point as _P2
        marked = []
        for i, v in enumerate(per_vert):
            if math.isnan(v[2]) or math.isnan(v[3]):
                continue
            if abs(v[2] - v[3]) <= gap_threshold_m:
                continue
            if emitted_pav_union is not None and not emitted_pav_union.is_empty:
                try:
                    if emitted_pav_union.distance(
                            _P2(v[0], v[1])) < 5.0:
                        continue
                except _GEOM_EXC:
                    pass
            marked.append((i, v))
        if not marked:
            continue
        # Group consecutive marked vertices into runs (treat the
        # boundary as cyclic; allow 1-vertex unmarked slack).
        marked_idx = sorted(set(m[0] for m in marked))
        N = len(per_vert)
        runs: list[list[int]] = []
        if marked_idx:
            cur = [marked_idx[0]]
            for idx in marked_idx[1:]:
                # Distance along ring, accounting for wrap.
                gap_idx = idx - cur[-1]
                if gap_idx <= 2:
                    cur.append(idx)
                else:
                    runs.append(cur)
                    cur = [idx]
            runs.append(cur)
            # Wrap merge: last run end-of-ring + first run
            # start-of-ring close ⇒ merge.
            if len(runs) >= 2:
                tail = runs[-1][-1]
                head = runs[0][0]
                if (N - tail) + head <= 2:
                    runs[0] = runs[-1] + runs[0]
                    runs.pop()

        # ── User's sequential-walk algorithm (2026-05-16) ─────────
        # 1. Walk boundary; mark vertices within ``runway_clamp_
        #    radius_m`` of any runway (B19's clamp radius is the
        #    upstream condition that creates altitude gaps).
        # 2. Each maximal contiguous "marked" stretch is a bridge
        #    run: the bridge's outer edge walks those vertices.
        # 3. For each run, snap from the run's end-vertex across to
        #    the nearest emitted_pav_union outer-ring vertex; then walk
        #    emitted_pav_union BACK toward the run start, collecting
        #    canonical pavement vertices along the way.  Close the
        #    polygon by snapping from the last pavement-walk vertex
        #    to the run start.  By construction every bridge node
        #    is either a boundary node (outer side) or a pavement-
        #    union outer-ring node (inner side); no synthesised
        #    intersection vertices; no overlap because both walks
        #    are monotonic along their respective polygon
        #    perimeters.
        from shapely.ops import nearest_points

        # Pre-build emitted_pav_union outer ring (canonical nodes).
        # Include runways so the bridge inner edge wraps around them
        # (touching, not overlapping).
        pav_for_inner = [
            s.polygon for s in layout.shapes
            if s.role != ROLE_BOUNDARY
            and s.polygon is not None
            and not s.polygon.is_empty]
        pav_union_local: Polygon | None = None
        pav_ring_coords: list[tuple[float, float]] = []
        pav_ring_line: LineString | None = None
        if pav_for_inner:
            try:
                pav_union_local = unary_union(pav_for_inner)
                if pav_union_local.geom_type == "Polygon":
                    rc = list(pav_union_local.exterior.coords)
                    if rc and rc[0] == rc[-1]:
                        rc = rc[:-1]
                    pav_ring_coords = [(float(x), float(y))
                                        for (x, y) in rc]
                elif pav_union_local.geom_type == "MultiPolygon":
                    # Pick the largest component — bridges face the
                    # main pavement mass; small disconnected pieces
                    # aren't bridged.
                    largest = max(
                        pav_union_local.geoms,
                        key=lambda g: g.area)
                    rc = list(largest.exterior.coords)
                    if rc and rc[0] == rc[-1]:
                        rc = rc[:-1]
                    pav_ring_coords = [(float(x), float(y))
                                        for (x, y) in rc]
            except _GEOM_EXC:
                pav_ring_coords = []
        if len(pav_ring_coords) >= 3:
            try:
                pav_ring_line = _LS(pav_ring_coords + [pav_ring_coords[0]])
            except _GEOM_EXC:
                pav_ring_line = None

        # Altitude lookup for pav_ring nodes (round to 0.1 m).
        pav_alt_lookup: dict[tuple[int, int], float] = {}
        for (px, py, pa) in pav_edge_pts:
            k = (int(round(px * 10)), int(round(py * 10)))
            pav_alt_lookup[k] = float(pa)
        def _pav_alt(px: float, py: float) -> float:
            k = (int(round(px * 10)), int(round(py * 10)))
            if k in pav_alt_lookup:
                return pav_alt_lookup[k]
            # Bucket missed (typically because the point came from a
            # unary_union intersection that isn't on a canonical
            # pavement vertex).  Use the nearest pavement-edge
            # altitude rather than raw DEM — raw DEM sampled at
            # inner-bridge positions in valley terrain north of an
            # elevated airport (e.g. MMOX 1520 m plateau, valley at
            # ~360 m) silently dropped the bridge by ~1000 m.  Per
            # ``feedback_boundary_clamp_asymmetric`` the bridge
            # altitude must never dip below the surrounding pavement.
            near = _nearest_pav_alt(px, py, max_d_m=2000.0)
            if near is not None:
                return near[0]
            clamped = _clamped_alt(px, py)
            if clamped is not None:
                return float(clamped)
            raise RuntimeError(
                f"_pav_alt: no altitude source for ({px:.2f}, {py:.2f}) "
                f"— bucket miss, no nearest pavement within 2 km, "
                f"no clamped DEM.  Investigate why this point has no "
                f"resolvable altitude.")

        _bdbg = os.environ.get("O4_BRIDGE_DEBUG") == "1"
        _emitted_bridge_union = None
        for run in runs:
            if _bdbg:
                _c0 = per_vert[run[0]]
                _c1 = per_vert[run[-1]]
                print(f"  [bridge-dbg] run len={len(run)} "
                      f"start=({_c0[0]:.0f},{_c0[1]:.0f}) "
                      f"end=({_c1[0]:.0f},{_c1[1]:.0f})")
            if len(run) < 2:
                continue
            # Outer side of the bridge sits at the airport_boundary
            # ribbon's INNER edge — offset inward by
            # ``BOUNDARY_STRIP_HALF_WIDTH_M`` from the boundary line.
            # This places the bridge's outer vertices on the same
            # locus as the ribbon's interior-side nodes, eliminating
            # the ribbon overlap that walking the raw boundary would
            # produce.  MUST use the same constant as the ribbon's
            # ``strip_half_width_m`` default so the two meet flush.
            STRIP_HALF_WIDTH_M = BOUNDARY_STRIP_HALF_WIDTH_M
            raw_outer_pts: list[tuple[float, float]] = []
            raw_outer_alts: list[float] = []
            for ii_in_run, i_dense in enumerate(run):
                vx, vy = per_vert[i_dense][0], per_vert[i_dense][1]
                raw_outer_pts.append((vx, vy))
                # OUTER edge sits on the airport perimeter at the
                # boundary's CLAMPED altitude (per this function's
                # docstring) — the SAME value the airport_boundary
                # ribbon assigns at the co-located vertex (both use
                # the asymmetric runway clamp with identical params).
                # Using ``_bridge_alt`` (nearest-pavement) here
                # instead made the outer edge sit several metres above
                # the DEM-following ribbon at the same XY, so
                # ``_intern`` rendered a vertical wall between them —
                # the CYXY perimeter spike/trench artifact (138 such
                # walls, up to 13.6 m).  ``_clamped_alt`` keeps the
                # bridge flush with the ribbon; the MMOX 1000 m drop
                # it was meant to guard was actually fixed by the
                # cross-tile DEM reuse (MMOX emits no bridge at all
                # after that fix, so the outer-edge source is moot
                # there).  The INNER edge keeps its pavement floor.
                ba = _clamped_alt(vx, vy)
                if ba is None:
                    ba = _bridge_alt(vx, vy)
                if ba is None:
                    raise RuntimeError(
                        f'boundary_dem_bridge outer: no altitude '
                        f'source for ({vx:.2f}, {vy:.2f})')
                raw_outer_alts.append(round(ba, 1))
            # Compute inward-perpendicular offset per vertex from
            # the local boundary tangent (average of the two
            # adjacent segments).  The two perpendiculars are
            # disambiguated by ``boundary_poly.contains()`` on a
            # short probe.
            outer_pts = []
            outer_perps: list[tuple[float, float]] = []
            outer_alts = list(raw_outer_alts)
            n_raw = len(raw_outer_pts)
            for k, (bx, by) in enumerate(raw_outer_pts):
                if 0 < k < n_raw - 1:
                    prev_pt = raw_outer_pts[k - 1]
                    next_pt = raw_outer_pts[k + 1]
                elif k > 0:
                    prev_pt = raw_outer_pts[k - 1]
                    next_pt = (bx, by)
                else:
                    prev_pt = (bx, by)
                    next_pt = (raw_outer_pts[k + 1]
                                if n_raw > 1 else (bx, by))
                tx = next_pt[0] - prev_pt[0]
                ty = next_pt[1] - prev_pt[1]
                tmag = math.hypot(tx, ty)
                if tmag < 1e-6:
                    outer_pts.append((bx, by))
                    outer_perps.append((0.0, 0.0))
                    continue
                ux = tx / tmag
                uy = ty / tmag
                probe = _Point(bx + (-uy) * 0.5, by + ux * 0.5)
                if boundary_poly.contains(probe):
                    perp_x = -uy
                    perp_y = ux
                else:
                    perp_x = uy
                    perp_y = -ux
                outer_pts.append((bx + perp_x * STRIP_HALF_WIDTH_M,
                                   by + perp_y * STRIP_HALF_WIDTH_M))
                outer_perps.append((perp_x, perp_y))
            if len(outer_pts) < 2:
                continue

            def _synth_inner_edge(o_pts, o_perps, o_alts):
                """Build an inner edge by offsetting each outer vertex
                inward along its boundary perpendicular by
                ``bridge_depth_m``.  A parallel-offset ribbon that
                works for runs of any length (unlike the pavement
                walk, whose closure check fails for long runs).
                Inner-edge altitudes come from ``_bridge_alt``
                (nearest-pavement floor, never below surrounding
                pavement per ``feedback_boundary_clamp_asymmetric``).
                Returns ``(inner_pts, inner_alts)``.
                """
                i_pts: list[tuple[float, float]] = []
                i_alts: list[float] = []
                for kk, (ox, oy) in enumerate(o_pts):
                    px, py = o_perps[kk]
                    if px == 0.0 and py == 0.0:
                        # Degenerate perpendicular — inherit outer.
                        i_pts.append((ox, oy))
                        i_alts.append(o_alts[kk])
                        continue
                    sx = ox + px * bridge_depth_m
                    sy = oy + py * bridge_depth_m
                    ba = _bridge_alt(sx, sy)
                    i_pts.append((sx, sy))
                    i_alts.append(round(ba, 1) if ba is not None
                                  else o_alts[kk])
                return i_pts, i_alts

            def _fallback_inner_edge():
                """Inner edge for runs that have no usable pavement
                walk (no pavement at all, or run endpoints too far
                from any pavement-ring vertex).

                Offsets each outer vertex inward by ``bridge_depth_m``
                toward the boundary centroid — the cheap heuristic
                that keeps the inner edge clear of nearby junctions
                where it is valid.  But the centroid direction is only
                inward for convex boundaries; on a concave lobe (e.g.
                CYXY's NW finger, whose north edge faces the interior
                while the global centroid sits far south) it points
                OUTWARD and lands the whole bridge outside the
                perimeter.  Detect that case via the sign of the
                centroid direction against the per-vertex inward
                normal (``outer_perps``, disambiguated by
                ``boundary_poly.contains()``) and fall back to the
                rigorous local-perpendicular offset there.
                Altitudes come from ``_bridge_alt`` either way (never
                below surrounding pavement per
                ``feedback_boundary_clamp_asymmetric``).
                Returns ``(inner_pts, inner_alts)``.
                """
                ctr = boundary_poly.centroid
                dot_sum = 0.0
                for (bx, by), (px, py) in zip(outer_pts, outer_perps):
                    cdx, cdy = ctr.x - bx, ctr.y - by
                    cm = math.hypot(cdx, cdy)
                    if cm > 1e-6:
                        dot_sum += (cdx / cm) * px + (cdy / cm) * py
                if dot_sum < 0.0:
                    # Centroid points outward here — use the local
                    # inward perpendicular instead.
                    return _synth_inner_edge(
                        outer_pts, outer_perps, outer_alts)
                i_pts: list[tuple[float, float]] = []
                i_alts: list[float] = []
                for k_pt, (bx, by) in enumerate(outer_pts):
                    cdx, cdy = ctr.x - bx, ctr.y - by
                    pmag = math.hypot(cdx, cdy)
                    if pmag < 1e-6:
                        # Vertex coincides with centroid — inherit the
                        # outer altitude rather than fabricating 0 m.
                        i_pts.append((bx, by))
                        i_alts.append(outer_alts[k_pt])
                        continue
                    sx = bx + cdx / pmag * bridge_depth_m
                    sy = by + cdy / pmag * bridge_depth_m
                    ba = _bridge_alt(sx, sy)
                    i_pts.append((sx, sy))
                    i_alts.append(round(ba, 1) if ba is not None
                                  else outer_alts[k_pt])
                return i_pts, i_alts

            # When no pavement is available, fall back to an inward
            # offset (centroid where valid, local perpendicular on
            # concave lobes — see ``_fallback_inner_edge``).
            if pav_ring_line is None or not pav_ring_coords:
                inner_pts, inner_alts = _fallback_inner_edge()
                ring_pts = list(outer_pts) + list(reversed(inner_pts))
                ring_alts = list(outer_alts) + list(reversed(inner_alts))
            else:
                # ── Step 1: snap run endpoints onto pav_union ring ──
                start_b = outer_pts[0]
                end_b = outer_pts[-1]
                # Snap to nearest pav_union outer-ring VERTEX
                # (canonical alignment so bridge inner-edge vertices
                # coincide with junction corners, preserving the
                # shared-vertex invariant).
                def _nearest_pav_vertex(x: float, y: float
                                          ) -> tuple[int, float]:
                    best_i = -1
                    best_d = float('inf')
                    for ii, (px, py) in enumerate(pav_ring_coords):
                        d = math.hypot(px - x, py - y)
                        if d < best_d:
                            best_d = d
                            best_i = ii
                    return best_i, best_d
                start_i, start_d = _nearest_pav_vertex(*start_b)
                end_i, end_d = _nearest_pav_vertex(*end_b)
                # Reject runs with no nearby pavement vertex on
                # either end — pav_ring_coords can be sparse
                # (corners only) in cross-tile builds.  Fall back to
                # an inward offset (centroid where valid, local
                # perpendicular on concave lobes — see
                # ``_fallback_inner_edge``).  The earlier
                # always-centroid version emitted a bridge entirely
                # outside the perimeter on CYXY's concave NW finger.
                if (start_i < 0 or end_i < 0
                        or start_d > bridge_depth_m * 2
                        or end_d > bridge_depth_m * 2):
                    inner_pts, inner_alts = _fallback_inner_edge()
                    ring_pts = list(outer_pts) + list(reversed(inner_pts))
                    ring_alts = list(outer_alts) + list(reversed(inner_alts))
                else:
                    # ── Step 2: walk pav_ring from end_i back to start_i ──
                    # Pick the direction whose initial step from
                    # end_i is CLOSER to start_b than the opposite
                    # direction's initial step (so we walk "back
                    # toward the run start").
                    n_ring = len(pav_ring_coords)
                    fwd_first = pav_ring_coords[(end_i + 1) % n_ring]
                    bwd_first = pav_ring_coords[(end_i - 1) % n_ring]
                    d_fwd = math.hypot(fwd_first[0] - start_b[0],
                                        fwd_first[1] - start_b[1])
                    d_bwd = math.hypot(bwd_first[0] - start_b[0],
                                        bwd_first[1] - start_b[1])
                    step = +1 if d_fwd < d_bwd else -1
                    # Walk pav_union from end_i back toward start;
                    # STOP when current vertex is >
                    # runway_clamp_radius_m (400 m) from start_b.
                    CLOSURE_DIST_M = runway_clamp_radius_m
                    inner_pts = []
                    inner_alts = []
                    idx = end_i
                    visited = 0
                    while visited < n_ring:
                        px, py = pav_ring_coords[idx]
                        d_to_start = math.hypot(px - start_b[0],
                                                 py - start_b[1])
                        if d_to_start > CLOSURE_DIST_M:
                            break
                        inner_pts.append((float(px), float(py)))
                        inner_alts.append(round(_pav_alt(px, py), 1))
                        if idx == start_i:
                            break
                        idx = (idx + step) % n_ring
                        visited += 1
                    if len(inner_pts) < 2:
                        # Pavement walk failed to span the run.  This
                        # happens for LONG runs (e.g. CYXY's 120-vertex
                        # ~3 km stretch): the closure check measures
                        # each pavement vertex's distance to the run
                        # START, so the pavement vertex nearest the run
                        # END is inherently > CLOSURE_DIST_M from the
                        # start and the walk breaks on iteration 0.
                        # Don't drop the run — fall back to a
                        # perpendicular inward-offset inner edge (works
                        # for any run length, same construction as the
                        # no-pavement / endpoints-far fallbacks).
                        inner_pts, inner_alts = _synth_inner_edge(
                            outer_pts, outer_perps, outer_alts)
                        ring_pts = (list(outer_pts)
                                    + list(reversed(inner_pts)))
                        ring_alts = (list(outer_alts)
                                     + list(reversed(inner_alts)))
                    else:
                        ring_pts = list(outer_pts) + list(inner_pts)
                        ring_alts = list(outer_alts) + list(inner_alts)

            if len(ring_pts) < 4:
                continue
            try:
                bridge_poly = _Polygon(ring_pts)
                if not bridge_poly.is_valid:
                    fixed = bridge_poly.buffer(0)
                    if fixed.is_empty:
                        continue
                    # ``buffer(0)`` heals a self-intersecting ring.
                    # The parallel-offset inner edge used for long
                    # curved runs (CYXY's 3 km west-side run) can
                    # self-overlap on concave boundary sections; the
                    # heal removes the overlapping lobe and may CHANGE
                    # the vertex count or split into a MultiPolygon.
                    # Both are fine — altitudes are assigned by
                    # POSITION downstream (0.1 m bucket +
                    # nearest-pavement fallback), not by index, so we
                    # don't require the ring to keep its original
                    # vertex count.  (Previously a strict
                    # ``len(fc) != len(ring_pts)`` check dropped the
                    # entire run here — the root cause of CYXY only
                    # bridging 2 of its 3 gap runs.)
                    # Keep EVERY healed part — a run that turns a
                    # sharp corner (CYXY north tip) self-overlaps in
                    # the synth inner edge; buffer(0) splits it into
                    # lobes and keeping only the largest DISCARDED the
                    # whole north wedge (the user-visible terrain hole).
                    # Parts are emitted individually below.
                    if fixed.geom_type not in ("Polygon", "MultiPolygon")                             or fixed.is_empty:
                        continue
                    bridge_poly = fixed
                if bridge_poly.is_empty:
                    continue
                if bridge_poly.geom_type not in ("Polygon", "MultiPolygon"):
                    continue
                if bridge_poly.area < 100.0:
                    continue
            except _GEOM_EXC:
                continue

            # Final cleanup: subtract NON-RUNWAY pavement +
            # ribbon to trim small residual overlaps from
            # per-vertex / per-segment perpendicular mismatch.
            # Runways are excluded (they need the 1m sloping-rect
            # buffer per B12 to avoid creating bridge vertices on
            # runway long edges, which the
            # ``test_no_vertex_on_sloping_rect_edge`` invariant
            # catches).  Runway-vs-bridge overlap is small after
            # the canonical-node construction and stays under the
            # overlap-baseline cap on its own.
            cleanup_subs: list[Polygon] = []
            non_runway_pav = [
                s.polygon for s in layout.shapes
                if s.role not in (ROLE_BOUNDARY, ROLE_RUNWAY)
                and s.polygon is not None
                and not s.polygon.is_empty]
            if non_runway_pav:
                try:
                    nr_union = unary_union(non_runway_pav)
                    if nr_union is not None and not nr_union.is_empty:
                        cleanup_subs.append(nr_union)
                except _GEOM_EXC:
                    pass
            if ribbon_union is not None and not ribbon_union.is_empty:
                cleanup_subs.append(ribbon_union)
            # Sloping-rect (runway / parallel / stub / cross-conn)
            # union buffered by 1m so any intersection vertices the
            # subtraction creates land OUTSIDE the
            # ``EDGE_PROX_M`` test tolerance.
            sloping_polys = [
                s.polygon for s in layout.shapes
                if s.role in (ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
                              ROLE_SECONDARY_PARALLEL, ROLE_STUB,
                              ROLE_CROSS_CONNECTOR)
                and s.polygon is not None
                and not s.polygon.is_empty]
            if sloping_polys:
                try:
                    sr_union = unary_union(sloping_polys)
                    if sr_union is not None and not sr_union.is_empty:
                        cleanup_subs.append(sr_union.buffer(1.0))
                except _GEOM_EXC:
                    pass
            if _bdbg:
                print(f"  [bridge-dbg]   pre-subtract area {bridge_poly.area:,.0f}")
            for sub in cleanup_subs:
                try:
                    trimmed = bridge_poly.difference(sub)
                except _GEOM_EXC:
                    trimmed = None
                if trimmed is None or trimmed.is_empty:
                    continue
                if trimmed.geom_type in ("Polygon", "MultiPolygon"):
                    if _bdbg and trimmed.geom_type == "MultiPolygon":
                        print(f"  [bridge-dbg]   subtract -> MULTI "
                              f"{[round(g.area) for g in sorted(trimmed.geoms, key=lambda g: -g.area)[:5]]}")
                    bridge_poly = trimmed
                else:
                    bridge_poly = None
                    break
            if _bdbg:
                print(f"  [bridge-dbg]   post-subtract "
                      f"{'DROPPED' if bridge_poly is None or bridge_poly.is_empty else f'area {bridge_poly.area:,.0f}'}")
            if bridge_poly is None or bridge_poly.is_empty:
                continue
            if bridge_poly.area < 100.0:
                continue
            # Containment guard (scope invariant): the bridge feature
            # is defined ONLY for the interior side of the perimeter
            # ("OUTSIDE the airport boundary X-Plane keeps falling
            # directly to DEM").  Clip to ``boundary_poly`` so no
            # emitted bridge can ever spill outside the perimeter —
            # belt-and-braces against any inner-edge construction
            # path (or buffer(0) heal) that overshoots a thin /
            # concave lobe.  If the clip leaves nothing of substance
            # inside, the run is dropped rather than emitted outside.
            try:
                contained = bridge_poly.intersection(boundary_poly)
            except _GEOM_EXC as _cx:
                if _bdbg:
                    print(f"  [bridge-dbg]   containment EXC: {_cx!r} "
                          f"(boundary valid={boundary_poly.is_valid})")
                contained = None
            if contained is None or contained.is_empty:
                if _bdbg and contained is not None:
                    print(f"  [bridge-dbg]   containment EMPTY "
                          f"(boundary valid={boundary_poly.is_valid} "
                          f"area={boundary_poly.area:,.0f})")
                continue
            if contained.geom_type == "GeometryCollection":
                # Tangencies between the bridge and the perimeter add stray
                # line/point fragments to the intersection; rejecting the
                # whole collection here silently dropped CYXY's ENTIRE
                # 373 k m² bridge (zero bridges emitted, no debug path —
                # the east/north ribbon↔pavement valleys).  Keep the
                # polygonal parts.
                _polys = [g for g in contained.geoms
                          if g.geom_type in ("Polygon", "MultiPolygon")
                          and not g.is_empty]
                if _bdbg:
                    print(f"  [bridge-dbg]   containment GC -> "
                          f"{len(_polys)} polygonal part(s)")
                if not _polys:
                    continue
                try:
                    contained = unary_union(_polys)
                except _GEOM_EXC:
                    continue
            if contained.geom_type not in ("Polygon", "MultiPolygon") \
                    or contained.is_empty:
                if _bdbg:
                    print(f"  [bridge-dbg]   containment non-poly "
                          f"type={contained.geom_type}")
                continue
            if contained.area < 100.0:
                continue
            bridge_poly = contained
            # Emit EVERY substantial part (a healed/subtracted run can be
            # a MultiPolygon; dropping the non-largest lobes left the CYXY
            # north boundary unbridged over faulty DEM).
            _bridge_parts = [g for g in getattr(bridge_poly, "geoms",
                                                [bridge_poly])
                             if g.geom_type == "Polygon"
                             and g.area >= 100.0]
            if _bdbg:
                print(f"  [bridge-dbg]   emitting {len(_bridge_parts)} "
                      f"part(s) {[round(g.area) for g in _bridge_parts]}")
            for bridge_poly in _bridge_parts:
              # No part may overlap an already-emitted bridge (two runs can
              # both reach a corner) or pavement (small lobes the old
              # keep-largest silently discarded) — zero-tolerance
              # ``test_no_self_overlap``.
              try:
                  _subs2 = list(cleanup_subs)
                  if _emitted_bridge_union is not None:
                      _subs2.append(_emitted_bridge_union)
                  for _s2 in _subs2:
                      bridge_poly = bridge_poly.difference(_s2)
                      if bridge_poly.is_empty:
                          break
                  if bridge_poly.is_empty:
                      continue
                  if bridge_poly.geom_type == "MultiPolygon":
                      bridge_poly = max(bridge_poly.geoms,
                                        key=lambda g: g.area)
                  if (bridge_poly.geom_type != "Polygon"
                          or bridge_poly.area < 100.0):
                      continue
              except _GEOM_EXC:
                  continue
              # Resample altitudes for the (possibly reshaped) ring.
              new_coords = list(bridge_poly.exterior.coords)
              if new_coords and new_coords[0] == new_coords[-1]:
                new_coords_open = new_coords[:-1]
              else:
                new_coords_open = new_coords
              if len(new_coords_open) < 3:
                continue
              canon_alt: dict[tuple[int, int], float] = {}
              for (cx, cy), ca in zip(
                    list(outer_pts) + list(inner_pts),
                    list(outer_alts) + list(inner_alts)):
                ck = (int(round(cx * 10)), int(round(cy * 10)))
                canon_alt[ck] = ca
              ring_alts = []
              for (cx, cy) in new_coords_open:
                ck = (int(round(cx * 10)),
                      int(round(cy * 10)))
                ca = canon_alt.get(ck)
                # Boundary-side vertex: ALWAYS use the SAME clamp the
                # ribbon uses, so the bridge stays flush with the ribbon
                # at shared vertices.  This must override even a canon-
                # bucket hit — at a sharp boundary corner (CYXY north
                # tip) a synthesized INNER vertex from one leg lands on
                # the OTHER leg's ribbon carrying a nearest-pavement
                # altitude 5.5 m above it (vertical-wall regression).
                try:
                    # 10 m: generous outer zone (healed corner vertices
                    # land up to ~7 m off the line); inner vertices sit
                    # ~bridge_depth_m (100 m) in, far outside it.
                    _on_bnd = (boundary_poly.exterior.distance(
                        _Point(cx, cy)) <= 10.0)
                except _GEOM_EXC:
                    _on_bnd = False
                if _on_bnd:
                    _cl = _clamped_alt(cx, cy)
                    if _cl is not None:
                        ca = round(float(_cl), 1)
                if ca is None:
                    # The 0.1 m bucket can miss because the cleanup
                    # subtractions above run buffer(0)/difference and
                    # nudge ring vertices off their original keys.
                    # Falling back to raw ``_dem_alt`` here silently
                    # produced the MMOX north-tile 1000 m drop
                    # (bridge inner edge sampling valley DEM ~360 m
                    # while the airport plateau is at ~1520 m).  Use
                    # nearest-pavement altitude instead so the bridge
                    # inherits surrounding pavement elevation, per
                    # ``feedback_boundary_clamp_asymmetric``.
                    near = _nearest_pav_alt(cx, cy, max_d_m=2000.0)
                    if near is not None:
                        ca = round(near[0], 1)
                    else:
                        clamped = _clamped_alt(cx, cy)
                        if clamped is None:
                            raise RuntimeError(
                                f"boundary_dem_bridge: no altitude "
                                f"source for vertex "
                                f"({cx:.2f}, {cy:.2f}) — bucket miss, "
                                f"no pavement within 2 km, no clamped "
                                f"DEM.  Investigate upstream cause.")
                        ca = round(float(clamped), 1)
                ring_alts.append(ca)

              node_alts = list(ring_alts) + [ring_alts[0]]
              layout.shapes.append(BuiltShape(
                polygon=bridge_poly,
                role=ROLE_BOUNDARY,
                ref="boundary_dem_bridge",
                node_altitudes=node_alts,
              ))
              n_emitted += 1
              try:
                  _emitted_bridge_union = (
                      bridge_poly if _emitted_bridge_union is None
                      else _emitted_bridge_union.union(bridge_poly))
              except _GEOM_EXC:
                  pass

    return n_emitted


