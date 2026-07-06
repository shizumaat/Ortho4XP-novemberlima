"""Cut shapes along integer lat/lon tile boundaries.

X-Plane / Ortho4XP renders each 1° × 1° lat-lon tile as a separate
DSF file.  A single shape that spans two tiles is awkwardly bisected
at the seam, producing visual artifacts.  Per user 2026-05-10: for
each integer lat or lon line passing through the airport's pavement
footprint, build a buffered line (``half_width_m`` each side, so a
10 m strip by default) and subtract it from every shape.  Shapes
that split into multiple pieces are replaced with separate
``BuiltShape`` entries; sloped 4-corner rects convert to per-vertex
``node_altitudes`` (cut pieces are non-rectangular and the legacy
``[H, L, L, H]`` 4-corner convention no longer applies).

The mechanism mirrors how the pavement builder clips runway shapes
out of ``pav_union`` — just at a different geometric target.  Ortho4XP
and X-Plane stitch the resulting tile seams together at render time.

Public API:
    cut_layout_at_tile_boundaries
"""
from __future__ import annotations

import copy
import math
from collections.abc import Callable

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .layout import (
    BuiltShape, PavementLayout, R_EARTH,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_RUNWAY, ROLE_JUNCTION,
    ROLE_APRON, ROLE_BUILDING, ROLE_SERVICE_ROAD, ROLE_TUNNEL_RAMP,
    ROLE_RETAINING_WALL, vertex_bucket,
)
from .config import TUNNEL_RAMP_MAX_GRADE, RUNWAY_SEAM_DEM_PIN


# Taxi rects whose elevation slopes ALONG ``source_axis`` only — their
# cross-section is flat (enforced by the solver, but only while they
# stay 4-corner ``altitude_high``/``altitude_low`` rects).  When the
# tile slice crosses one we clip it back to a clean perpendicular end
# so the bulk keeps that flat-cross-section invariant; see
# ``_clip_sloping_rect_piece``.
_SLOPING_RECT_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_SERVICE_ROAD,
})

# Roles whose slice-edge vertices are DEM-pinned at the seam so adjacent
# tile builds agree there (taxi rects + junctions + aprons).  Runways
# follow the FAA profile (not terrain) and terminals stay flat, so they
# are excluded.
_PIN_SLICE_ROLES = _SLOPING_RECT_ROLES | frozenset({
    ROLE_JUNCTION, ROLE_APRON,
})

# POST-solve feature roles carrying SYNTHETIC (non-DEM) elevations —
# tunnel ramps / depressed-road plates at apt_elev−8 m and their
# retaining walls at deck level.  When the tile slice cuts one, its
# seam edge would otherwise sit metres above/below the neighbouring
# tile's raw terrain.  Per user 2026-06-10 these must MATCH the DEM at
# the seam and GRADE back to their design elevation inside the tile
# (``_grade_feature_piece_to_seam_dem``).  Groundside / boundary /
# clearance features already follow the DEM, so they agree at the seam
# naturally.
_SEAM_GRADE_FEATURE_ROLES = frozenset({
    ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
})

# Narrow exception set — covers real shapely degeneracy without
# masking programming errors (KeyError/TypeError/IndexError propagate).
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


__all__ = ["cut_layout_at_tile_boundaries",
           "nudge_runway_corners_at_seam_junctions"]

# A vertex this close (m) to an integer tile line is a tile-cut seam
# boundary vertex (tile_cut offsets them ``half_width_m`` = 5 m off the
# line) — terrain-pinned and effectively immutable.
_SEAM_LINE_TOL_M = 6.0
# Within-junction grade cap (matches ROLE_GRADE_LIMITS[junction] = 1.5%).
_RUNWAY_SEAM_GRADE_CAP = 0.015
# Pavement roles that can be a tile-cut seam stub abutting a junction.
_SEAM_PIECE_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_APRON, ROLE_BUILDING,
})


def cut_layout_at_tile_boundaries(
        layout: PavementLayout,
        half_width_m: float = 5.0,
        min_piece_area_m2: float = 1.0,
        current_tile_lat: int | None = None,
        current_tile_lon: int | None = None,
        dem=None,
        skip_roles: frozenset = frozenset()) -> int:
    """Cut every shape crossing an integer lat or lon tile boundary,
    leaving a ``2 * half_width_m`` wide gap (default 10 m).

    Then DROP every shape piece whose representative point falls
    outside the current tile.  The neighbour-tile auto_patch run
    will generate the patch covering its portion.

    ``current_tile_lat`` / ``current_tile_lon`` identify the tile
    being processed by the driver — these can differ from the
    airport's anchor tile when a cross-tile airport is being
    processed during a NEIGHBOUR-tile build (e.g. Ortho4XP
    generates tile -13/-78, which includes SPLP because the
    airport extends into it, but SPLP's anchor is in -13/-77).
    When None, fall back to ``floor(layout.anchor)`` (the
    airport-anchor tile) — backward-compatible default for tests
    and direct ``build_airport_pavement`` calls.

    Mutates ``layout.shapes`` in place.  Returns the net change in
    shape count (positive when shapes split, negative when slivers
    fall below ``min_piece_area_m2`` and get dropped).

    ``skip_roles`` shapes are passed through UNTOUCHED — neither cut nor
    altitude-resampled.  Used by the POST-solve feature cut to freeze AIRSIDE
    pavement: it was already cut by the PRE-solve call and graded by the solver
    against the seam DEM anchors, so re-touching it here only clobbers those
    solved altitudes (the gapped airside edge grazes the cut buffer and triggers
    a spurious re-sample).  The single source of airside elevation truth is the
    solve.
    """
    if not layout.shapes or layout.anchor is None:
        return 0
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))

    polys = [s.polygon for s in layout.shapes
             if s.polygon is not None and not s.polygon.is_empty]
    if not polys:
        return 0
    try:
        union = unary_union(polys)
    except _GEOM_EXC:
        return 0
    minx, miny, maxx, maxy = union.bounds

    # Footprint bounds in lat/lon.
    min_lat = lat0 + math.degrees(miny / R_EARTH)
    max_lat = lat0 + math.degrees(maxy / R_EARTH)
    min_lon = lon0 + math.degrees(minx / (R_EARTH * cos0))
    max_lon = lon0 + math.degrees(maxx / (R_EARTH * cos0))

    # Integer lat lines strictly inside the airport's lat range.
    cut_lines: list[LineString] = []
    for lat_int in range(
            int(math.ceil(min_lat)), int(math.floor(max_lat)) + 1):
        if min_lat < lat_int < max_lat:
            y_int = math.radians(lat_int - lat0) * R_EARTH
            cut_lines.append(LineString([
                (minx - 100.0, y_int), (maxx + 100.0, y_int)]))
    # Integer lon lines strictly inside the airport's lon range.
    for lon_int in range(
            int(math.ceil(min_lon)), int(math.floor(max_lon)) + 1):
        if min_lon < lon_int < max_lon:
            x_int = math.radians(lon_int - lon0) * R_EARTH * cos0
            cut_lines.append(LineString([
                (x_int, miny - 100.0), (x_int, maxy + 100.0)]))

    if not cut_lines:
        return 0

    try:
        cut_polys = [line.buffer(half_width_m, cap_style=2)
                     for line in cut_lines]
        cut_union = unary_union(cut_polys)
    except _GEOM_EXC:
        return 0

    # NOTE (user 2026-05-28): do NOT cut ``layout.airport_boundary`` here.
    # Cutting the outline polygon with the seam band inserted a straight
    # edge ALONG the tile line into the perimeter, and the boundary ribbon
    # (_emit_airport_boundary_shape traces the perimeter) then ran a ribbon
    # ALONG the seam.  The boundary must be sliced like every other shape:
    # the ribbon emits from the FULL outline and its emitted ROLE_BOUNDARY
    # rects are sliced + neighbour-tile pieces dropped by the post-emit
    # cut_layout_at_tile_boundaries call — so the ribbon ends AT the seam
    # instead of following it.

    # The CURRENT tile (the one this auto_patch run is generating)
    # is the airport-anchor tile.  Per user 2026-05-12: after the
    # cut, drop any shape (or shape piece) that's not inside the
    # current tile — when the neighbour tile is processed in its own
    # auto_patch run, IT generates the patch covering its portion of
    # the airport.  Without this drop, ``_runway_clamped_alt`` etc.
    # would try to sample the neighbour-tile DEM (which isn't
    # loaded) and substitute 0 m, producing altitude-0 boundary
    # rects in X-Plane.
    cur_tile_lat = (current_tile_lat if current_tile_lat is not None
                    else int(math.floor(lat0)))
    cur_tile_lon = (current_tile_lon if current_tile_lon is not None
                    else int(math.floor(lon0)))

    def _in_current_tile(poly: Polygon) -> bool:
        try:
            c = poly.representative_point()
        except _GEOM_EXC:
            try:
                c = poly.centroid
            except _GEOM_EXC:
                return True  # fail open
        lat = lat0 + math.degrees(c.y / R_EARTH)
        lon = lon0 + math.degrees(c.x / (R_EARTH * cos0))
        return (cur_tile_lat <= lat < cur_tile_lat + 1
                and cur_tile_lon <= lon < cur_tile_lon + 1)

    # (airport_boundary is intentionally left UNCUT — see note above; the
    # ribbon it generates is sliced as ordinary ROLE_BOUNDARY shapes below.)

    n_before = len(layout.shapes)
    new_shapes: list[BuiltShape] = []
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            new_shapes.append(s)
            continue
        if skip_roles and s.role in skip_roles:
            # Frozen (already cut pre-solve + solved) — pass through untouched.
            new_shapes.append(s)
            continue
        try:
            if not s.polygon.intersects(cut_union):
                # No cut — keep iff the shape is in the current tile.
                if _in_current_tile(s.polygon):
                    new_shapes.append(s)
                continue
            diff = s.polygon.difference(cut_union)
        except _GEOM_EXC:
            new_shapes.append(s)
            continue
        if diff.is_empty:
            # Source polygon entirely inside the cut buffer.  No
            # pavement pieces but a bridge will be emitted below.
            pieces: list[Polygon] = []
        elif diff.geom_type == "Polygon":
            pieces = [diff]
        elif diff.geom_type == "MultiPolygon":
            pieces = [g for g in diff.geoms
                      if g.geom_type == "Polygon" and not g.is_empty]
        else:
            # Unexpected result (e.g. non-empty GeometryCollection);
            # keep the original shape and skip the cut + bridge.
            if _in_current_tile(s.polygon):
                new_shapes.append(s)
            continue
        pieces = [p for p in pieces if p.area >= min_piece_area_m2]
        pieces = [p for p in pieces if _in_current_tile(p)]

        slope_sampler = _make_slope_sampler(s)
        is_sloping_rect = (
            s.role in _SLOPING_RECT_ROLES
            and slope_sampler is not None)
        for piece in pieces:
            # Sloping taxi rect crossed by the slice: keep the bulk as a
            # clean 4-corner sloped rect (so the solver's flat-cross-
            # section constraint survives) and fill the slice-side gap
            # with a small node_altitudes piece.  Converting the WHOLE
            # oblique cut piece to node_altitudes (the default below)
            # drops that constraint and lets the taxiway tilt
            # perpendicular to its axis (user 2026-05-20).
            if is_sloping_rect:
                clipped = _clip_sloping_rect_piece(
                    s, piece, cut_union, slope_sampler,
                    layout, dem, cur_tile_lat, cur_tile_lon)
                if clipped is not None:
                    new_shapes.extend(clipped)
                    continue
                # Clip-back wasn't applicable (e.g. the slice grazes a
                # short/oblique stub and would yield a degenerate clean
                # rect).  Fall through to the default node_altitudes
                # piece; its slice nodes are pinned to the seam DEM below
                # like every other cut piece.
            new_s = _build_piece_shape(s, piece, slope_sampler)
            if new_s is not None:
                # Seam DEM is the top-priority anchor: pin this piece's
                # slice-edge vertices to the (Ortho4XP-smoothed) terrain
                # so the solver grades the surface down to the seam
                # (user 2026-05-20).  Session 51: extended to junctions
                # /aprons too (not just sloping rects) — in the single-
                # solve order their slice-edge vertices would otherwise be
                # fully soft and diverge between adjacent-tile builds
                # (test_cross_tile_cut_edge_elevations_consistent).  The
                # pin makes both tiles compute the same DEM value there.
                if RUNWAY_SEAM_DEM_PIN and s.role == ROLE_RUNWAY:
                    # Runway pieces take the REDISTRIBUTED FAA PROFILE at
                    # every vertex (user SPLP seam-dip report 2026-07-03).
                    # The profile already folds the seam DEM at the
                    # centerline-boundary crossing (the one point where
                    # "seam = threshold at DEM" is well-defined — user
                    # 2026-06-20), so per-vertex DEM pins added nothing at
                    # benign seams and carved a terrain notch into the
                    # runway at oblique ones: SPLP's seam crosses RW02/20
                    # at 18°, its two band-edge corners span 141 m of
                    # station, and their raw-DEM values (55.5 / 59.7 —
                    # a ravine wall) violate the 1.5 % cap by 2×, so the
                    # emitted surface V-notched 4.2 m and every
                    # ``runway_clamp_floor`` computed from it was poisoned
                    # (the mirrored junction dips).  Profile evaluation is
                    # deterministic across adjacent tile builds (same CIFP
                    # + same boundary HGT pixels), which is all the
                    # 2026-06-20 pin actually needed.  Falls back to the
                    # DEM pin when no CIFP profile exists for the ref.
                    if not _pin_runway_piece_to_profile(
                            new_s, cut_union, layout):
                        _terrain_pin_slice_nodes(
                            new_s, cut_union, (), layout, dem,
                            cur_tile_lat, cur_tile_lon)
                elif s.role in _PIN_SLICE_ROLES:
                    _terrain_pin_slice_nodes(
                        new_s, cut_union, (), layout, dem,
                        cur_tile_lat, cur_tile_lon)
                elif s.role in _SEAM_GRADE_FEATURE_ROLES:
                    _grade_feature_piece_to_seam_dem(
                        new_s, cut_union, layout, dem,
                        cur_tile_lat, cur_tile_lon)
                new_shapes.append(new_s)
    layout.shapes = new_shapes

    # Absorb the tiny wedges the slice leaves on either side back into
    # their adjacent shape, so the cut doesn't leave an extra sliver
    # (user 2026-05-23).  The wedge's seam-edge vertices carry the
    # terrain-pinned (critical) seam altitude into the merged shape.
    _absorb_seam_slivers(layout, cut_lines, half_width_m)

    return len(layout.shapes) - n_before


# Roles that participate in the airside pavement partition — only these
# absorb / are absorbed by the seam-sliver pass (boundary ribbon, DEM
# bridge, runway and groundside have their own seam handling).
_ABSORB_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR, ROLE_JUNCTION, ROLE_APRON, ROLE_BUILDING,
})


def _absorb_seam_slivers(layout: PavementLayout,
                         cut_lines: list,
                         half_width_m: float) -> int:
    """Merge each tiny slice-created seam wedge into its adjacent
    (larger) shape so the tile cut leaves no extra sliver on either
    side of the slice.

    A wedge is a SMALL piece that (a) has ≥ 2 vertices on a cut line
    (a seam edge) and (b) hugs ONE larger neighbour — it shares ≥ 40 %
    of its own perimeter with that neighbour (i.e. it is a fragment of,
    or flush against, that shape).  It is unioned into the neighbour and
    dropped.

    Elevation is reconciled per-vertex from BOTH shapes' existing
    altitudes (the wedge's terrain-pinned seam-edge altitudes win for
    the seam vertices, which is the whole point — the slice-edge
    altitude is what X-Plane stitches neighbouring tiles against).  The
    merged shape becomes ``node_altitudes`` (it is no longer a clean
    4-corner rect).  Returns the number of wedges absorbed.
    """
    if not cut_lines or not layout.shapes:
        return 0
    SLIVER_MAX_AREA_M2 = 150.0
    NEIGHBOUR_MIN_RATIO = 1.5
    SHARE_TOL_M = 0.5            # near-adjacency band for shared length
    SHARE_MIN_M = 3.0           # min real contact with the neighbour
    SEAM_VERTEX_TOL_M = half_width_m + 2.0

    def _near_seam(x: float, y: float) -> bool:
        p = Point(x, y)
        return any(line.distance(p) <= SEAM_VERTEX_TOL_M
                   for line in cut_lines)

    def _seam_vertex_count(poly: Polygon) -> int:
        ring = list(poly.exterior.coords)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        return sum(1 for (x, y) in ring if _near_seam(x, y))

    def _near_shared_len(a: Polygon, b: Polygon) -> float:
        # Length of a's boundary lying within SHARE_TOL_M of b — robust
        # to the small float offsets the slice leaves between touching
        # pieces (exact boundary∩boundary reads ~0 there).
        try:
            return a.exterior.intersection(
                b.buffer(SHARE_TOL_M)).length
        except _GEOM_EXC:
            return 0.0

    absorbed = 0
    dropped: set = set()
    # Iterate: absorbing one wedge can make an adjacent wedge flush with
    # the now-larger neighbour (e.g. a junction triangle beside a
    # connector slice that itself merges into the connector bulk).
    for _pass in range(6):
        changed = False
        shapes = layout.shapes
        for sv in list(shapes):
            if id(sv) in dropped:
                continue
            if (sv.role not in _ABSORB_ROLES
                    or sv.polygon is None or sv.polygon.is_empty
                    or sv.polygon.geom_type != "Polygon"):
                continue
            try:
                sv_area = sv.polygon.area
            except _GEOM_EXC:
                continue
            if sv_area >= SLIVER_MAX_AREA_M2:
                continue
            if _seam_vertex_count(sv.polygon) < 2:
                continue
            # Neighbour sharing the most boundary (near-adjacency), at
            # least NEIGHBOUR_MIN_RATIO larger than the wedge.
            best = None
            best_share = 0.0
            for nb in shapes:
                if nb is sv or id(nb) in dropped:
                    continue
                if (nb.role not in _ABSORB_ROLES
                        or nb.polygon is None or nb.polygon.is_empty
                        or nb.polygon.geom_type != "Polygon"):
                    continue
                if nb.polygon.area < NEIGHBOUR_MIN_RATIO * sv_area:
                    continue
                shlen = _near_shared_len(sv.polygon, nb.polygon)
                if shlen > best_share:
                    best_share = shlen
                    best = nb
            if best is None or best_share < SHARE_MIN_M:
                continue
            # PREFER a sloping rect at a seam: EXTEND the rect's seam end
            # out to the wedge's seam edge, keeping a clean 4-corner
            # altitude_high/low rect.  That only works when the two
            # seam-end corners can share one elevation (a perpendicular /
            # flat seam end).  When the SEAM genuinely needs two DIFFERENT
            # corner elevations (an oblique / varying-DEM seam), the shape
            # cannot be a planar rect there, so fall back to node_altitudes
            # via the union path rather than orphaning the wedge (user
            # 2026-05-23: "if the seam requires different elevations at the
            # corners we switch to node_altitudes").
            nb_is_rect = (best.node_altitudes is None
                          and best.altitude_high is not None
                          and best.altitude_low is not None
                          and best.role in _SLOPING_RECT_ROLES
                          and best.polygon.geom_type == "Polygon"
                          and len(best.polygon.exterior.coords) - 1 == 4)
            if nb_is_rect:
                ok = _extend_rect_over_sliver(best, sv, cut_lines)
                if not ok:
                    # Seam needs per-corner elevations → can't stay a rect;
                    # merge as node_altitudes instead of leaving an orphan.
                    ok = _absorb_one_sliver(sv, best)
            else:
                ok = _absorb_one_sliver(sv, best)
            if ok:
                dropped.add(id(sv))
                absorbed += 1
                changed = True
        if not changed:
            break

    if dropped:
        layout.shapes = [s for s in layout.shapes if id(s) not in dropped]
    if absorbed:
        try:
            import O4_UI_Utils as UI
            UI.vprint(1,
                f"  [pav-builder] absorbed {absorbed} tile-slice seam "
                f"wedge(s) into adjacent shape(s).")
        except Exception:
            pass
    return absorbed


def _absorb_one_sliver(sv: BuiltShape, nb: BuiltShape) -> bool:
    """Union wedge ``sv`` into neighbour ``nb`` in place, reconciling
    per-vertex altitudes (the wedge's terrain-pinned seam-edge altitudes
    are carried in).  ``nb`` becomes a ``node_altitudes`` shape.  Returns
    True on success (caller drops ``sv``)."""
    try:
        merged = unary_union([nb.polygon, sv.polygon])
        if merged.geom_type == "MultiPolygon":
            # Tiny float gap between touching pieces — bridge it.
            merged = merged.buffer(0.05).buffer(-0.05)
    except _GEOM_EXC:
        return False
    if (merged.is_empty or merged.geom_type != "Polygon"
            or not merged.is_valid):
        return False
    # Altitude lookup from BOTH source shapes (seam-pinned wedge
    # vertices included — they win for the seam edge).
    src: dict = {}
    for sh in (nb, sv):
        coords, alts = _shape_corner_alts(sh)
        if alts is None:
            continue
        for (x, y), a in zip(coords, alts):
            if a is not None:
                src[vertex_bucket(x, y)] = float(a)
    if not src:
        return False
    ring = list(merged.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        return False
    merged_alts: list = []
    for (x, y) in ring:
        b = vertex_bucket(x, y)
        if b in src:
            merged_alts.append(src[b])
            continue
        # Vertex introduced by the union (rare) — nearest source alt.
        best_a = None
        best_d2 = 4.0  # within 2 m
        for sh in (nb, sv):
            coords, alts = _shape_corner_alts(sh)
            if alts is None:
                continue
            for (cx, cy), a in zip(coords, alts):
                if a is None:
                    continue
                d2 = (cx - x) ** 2 + (cy - y) ** 2
                if d2 < best_d2:
                    best_d2 = d2
                    best_a = float(a)
        if best_a is None:
            return False
        merged_alts.append(best_a)
    if len(merged_alts) != len(ring):
        return False
    nb.polygon = merged
    nb.node_altitudes = merged_alts + [merged_alts[0]]
    nb.altitude_high = None
    nb.altitude_low = None
    return True


def _extend_rect_over_sliver(nb: BuiltShape, sv: BuiltShape,
                             cut_lines: list) -> bool:
    """Extend sloping-rect ``nb``'s seam-end short edge out to the slice
    line so it covers wedge ``sv``, KEEPING ``nb`` a 4-corner
    ``altitude_high``/``altitude_low`` rect (never node_altitudes).

    The new seam-end edge follows the slice (may be an oblique trapezoid
    end).  This is only valid when the two new seam-end corners can share
    ONE elevation — i.e. the slice DEM is ~flat across the rect there.
    When the two corners would need different elevations (an oblique seam
    through a sloping rect) the rect cannot represent it, so we return
    False and leave the wedge unmerged (user 2026-05-23).
    """
    ELEV_SAME_TOL_M = 0.30
    MAX_EXTEND_M = 20.0
    SEAM_VTX_TOL_M = 8.0
    try:
        ring = list(nb.polygon.exterior.coords)
    except _GEOM_EXC:
        return False
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) != 4:
        return False
    # _rect_from_axis_extended corner order: 0,3 = end1 (altitude_high);
    # 1,2 = end2 (altitude_low).  Long edges pair 0-1 and 3-2.
    c0, c1, c2, c3 = ring

    # Nearest slice line to the wedge, and its orientation.
    svc = sv.polygon.centroid
    line = min(cut_lines, key=lambda L: L.distance(svc))
    lc = list(line.coords)
    vertical = abs(lc[0][0] - lc[-1][0]) <= abs(lc[0][1] - lc[-1][1])
    # Extend to the WEDGE's seam edge (the cut EDGE = ``half_width`` off the
    # cut LINE), NOT the line itself.  The current-tile geometry stops at
    # the cut edge; extending all the way to the line overshoots into the
    # removed cut strip, and a later re-clip then converts the rect back to
    # node_altitudes (SPLP stub/A #324 lost its slope this way).  Use the
    # wedge vertices closest to the cut line as the target coordinate.
    try:
        sv_ring = list(sv.polygon.exterior.coords)
        if sv_ring and sv_ring[0] == sv_ring[-1]:
            sv_ring = sv_ring[:-1]
    except _GEOM_EXC:
        sv_ring = []
    line_coord = lc[0][0] if vertical else lc[0][1]
    if sv_ring:
        idx = 0 if vertical else 1
        d_min = min(line.distance(Point(px, py)) for (px, py) in sv_ring)
        edge_pts = [p for p in sv_ring
                    if line.distance(Point(p[0], p[1])) <= d_min + 0.5]
        seam_coord = sum(p[idx] for p in edge_pts) / len(edge_pts)
    else:
        seam_coord = line_coord

    def _extend(pfar, pnear):
        dx, dy = pnear[0] - pfar[0], pnear[1] - pfar[1]
        denom = dx if vertical else dy
        if abs(denom) < 1e-9:
            return None
        s = ((seam_coord - pfar[0]) / dx if vertical
             else (seam_coord - pfar[1]) / dy)
        # Seam must be just BEYOND the near corner (s > 1) by a little.
        if s <= 1.0:
            return None
        np = (pfar[0] + s * dx, pfar[1] + s * dy)
        if math.hypot(np[0] - pnear[0], np[1] - pnear[1]) > MAX_EXTEND_M:
            return None
        return np

    # Which short edge is the seam end?
    mid1 = Point((c0[0] + c3[0]) / 2, (c0[1] + c3[1]) / 2)
    mid2 = Point((c1[0] + c2[0]) / 2, (c1[1] + c2[1]) / 2)
    if line.distance(mid1) <= line.distance(mid2):
        # end1 (high) is the seam end: extend c0 (via c1) and c3 (via c2)
        n_a = _extend(c1, c0)
        n_b = _extend(c2, c3)
        if n_a is None or n_b is None:
            return False
        new_quad = [n_a, c1, c2, n_b]
        set_high = True
    else:
        # end2 (low) is the seam end: extend c1 (via c0) and c2 (via c3)
        n_a = _extend(c0, c1)
        n_b = _extend(c3, c2)
        if n_a is None or n_b is None:
            return False
        new_quad = [c0, n_a, n_b, c3]
        set_high = False

    # Slice DEM elevation at the two new corners — from the wedge's
    # terrain-pinned seam vertices.
    sv_coords, sv_alts = _shape_corner_alts(sv)
    if sv_alts is None:
        return False
    seam_pts = [(x, y, a) for (x, y), a in zip(sv_coords, sv_alts)
                if a is not None
                and line.distance(Point(x, y)) <= SEAM_VTX_TOL_M]
    if not seam_pts:
        return False

    def _seam_elev(pt):
        best_a = None
        best_d2 = float("inf")
        for (x, y, a) in seam_pts:
            d2 = (x - pt[0]) ** 2 + (y - pt[1]) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_a = a
        return best_a

    e_a = _seam_elev(n_a)
    e_b = _seam_elev(n_b)
    if e_a is None or e_b is None:
        return False
    # +1e-6 absorbs float noise: 1-decimal elevations 62.6-62.3 compute to
    # 0.30000000000000004, which a bare ``> 0.30`` would wrongly reject as
    # an oblique seam, orphaning a flat seam wedge (SPLP stub/A #325).
    if abs(e_a - e_b) > ELEV_SAME_TOL_M + 1e-6:
        return False  # oblique seam → cannot keep a flat-end rect

    try:
        new_poly = Polygon(new_quad)
        if not new_poly.is_valid:
            new_poly = new_poly.buffer(0)
    except _GEOM_EXC:
        return False
    if (new_poly.is_empty or new_poly.geom_type != "Polygon"
            or new_poly.area < nb.polygon.area):
        return False
    # The extended rect should cover (most of) the wedge.
    try:
        if new_poly.intersection(sv.polygon).area < 0.5 * sv.polygon.area:
            return False
    except _GEOM_EXC:
        return False

    e_seam = 0.5 * (e_a + e_b)
    nb.polygon = new_poly
    if set_high:
        nb.altitude_high = e_seam
    else:
        nb.altitude_low = e_seam
    return True


def _shape_corner_alts(s: BuiltShape):
    """Return ``(open_coords, open_alts)`` — the shape's exterior ring
    (closing repeat dropped) and a per-vertex elevation list, or
    ``(open_coords, None)`` when no elevation is known."""
    if s.polygon is None or s.polygon.is_empty:
        return [], None
    try:
        coords = list(s.polygon.exterior.coords)
    except _GEOM_EXC:
        return [], None
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    n = len(coords)
    if n == 0:
        return coords, None
    if s.node_altitudes and len(s.node_altitudes) >= n:
        return coords, [float(s.node_altitudes[k]) for k in range(n)]
    if s.altitude_high is not None and s.altitude_low is not None:
        sampler = _make_slope_sampler(s)
        if sampler is not None:
            return coords, [sampler(x, y) for x, y in coords]
    if s.altitude is not None:
        return coords, [float(s.altitude)] * n
    return coords, None


def nudge_runway_corners_at_seam_junctions(layout: PavementLayout) -> int:
    """Nudge runway corners that abut a terrain-pinned tile-seam stub
    through a junction so the junction can hold grade (user 2026-05-20).

    Runs AFTER ``cut_layout_at_tile_boundaries`` (which creates the seam
    stubs) and BEFORE the final per-surface solve.  By this point a
    junction that bridges the runway and a seam stub contains BOTH the
    runway corner and the seam vertex, so the bridge is detectable here
    (it is NOT at runway-redistribute time — the stub doesn't exist
    yet).

    For each junction holding both a runway corner and a seam vertex,
    if the runway corner is more than the grade cap × (corner-to-seam
    distance) from the seam's immutable altitude, the runway corner is
    moved (UP or DOWN) just into grade.  The change is written to every
    runway sub-rect AND junction sharing that corner (canonical sloped
    rects convert to ``node_altitudes``); the final solver then grades
    the junction body against the adjusted, hard-anchored runway corner.

    Returns the number of runway sub-rects modified.
    """
    if not layout.shapes or layout.anchor is None:
        return 0

    # 1. Terrain-pinned seam vertices.  A pavement piece is a tile-cut
    #    seam stub when one of its vertices sits within _SEAM_LINE_TOL_M
    #    of an integer tile line (tile_cut offsets the cut edge 5 m off
    #    the line).  That whole piece is pinned to the immutable seam
    #    DEM, so ALL its vertices count — including the interface
    #    vertices it shares with an abutting junction (those sit further
    #    than 5 m from the line but carry the pinned elevation).
    #    bucket -> (x, y, elev).
    #    The pinned elevation lives on the cut-EDGE vertices (≤ 5 m off
    #    the line); a piece's interface vertices (shared with a
    #    junction, further from the line) have not yet been pulled to
    #    that value at this point in the pipeline — the final solver
    #    does that.  So each non-edge vertex is assigned the nearest
    #    cut-edge vertex's pinned elevation, i.e. the value it WILL
    #    take, so the runway corner is graded against it correctly.
    seam_pts: dict = {}
    for s in layout.shapes:
        if s.role not in _SEAM_PIECE_ROLES:
            continue
        coords, alts = _shape_corner_alts(s)
        if alts is None:
            continue
        edge_vs = []
        for k, (x, y) in enumerate(coords):
            lat, lon = layout.m_to_ll(x, y)
            cos0 = math.cos(math.radians(lat))
            m_lat = abs(lat - round(lat)) * R_EARTH * math.pi / 180.0
            m_lon = abs(lon - round(lon)) * R_EARTH * cos0 * math.pi / 180.0
            if min(m_lat, m_lon) <= _SEAM_LINE_TOL_M:
                edge_vs.append((x, y, alts[k]))
        if not edge_vs:
            continue  # not a tile-cut seam piece
        for x, y in coords:
            ex, ey, ee = min(
                edge_vs, key=lambda e: (e[0] - x) ** 2 + (e[1] - y) ** 2)
            seam_pts[vertex_bucket(x, y)] = (x, y, ee)
    if not seam_pts:
        return 0

    # 2. Runway corners: bucket -> (x, y, elev).
    runway_corner: dict = {}
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY:
            continue
        coords, alts = _shape_corner_alts(s)
        if alts is None:
            continue
        for k, (x, y) in enumerate(coords):
            runway_corner[vertex_bucket(x, y)] = (x, y, alts[k])
    if not runway_corner:
        return 0

    # 3. Junctions bridging a runway corner and a seam vertex → target.
    targets: dict = {}  # runway corner bucket -> target elevation
    for s in layout.shapes:
        if s.role != ROLE_JUNCTION:
            continue
        coords, _ = _shape_corner_alts(s)
        buckets = [vertex_bucket(x, y) for x, y in coords]
        rw = [(b, runway_corner[b]) for b in buckets if b in runway_corner]
        sm = [seam_pts[b] for b in buckets if b in seam_pts]
        if not rw or not sm:
            continue
        for rb, (rx, ry, relev) in rw:
            # The runway corner must stay within grade of EVERY seam
            # vertex in this junction — intersect their feasible bands.
            lo = -float("inf")
            hi = float("inf")
            for sx, sy, selev in sm:
                d = math.hypot(sx - rx, sy - ry)
                if d < 1.0:
                    continue
                budget = _RUNWAY_SEAM_GRADE_CAP * d
                lo = max(lo, selev - budget)
                hi = min(hi, selev + budget)
            if lo > hi:
                continue  # seam vertices conflict — can't satisfy both
            if relev < lo:
                tgt = lo
            elif relev > hi:
                tgt = hi
            else:
                continue  # already within grade of every seam vertex
            # If several junctions touch the same corner, keep the most
            # restrictive (largest required move).
            prev = targets.get(rb)
            if prev is None or abs(tgt - relev) > abs(prev - relev):
                targets[rb] = round(tgt, 2)
    if not targets:
        return 0

    # 4. Apply: rewrite every runway sub-rect and junction sharing a
    #    target bucket (canonical rects → node_altitudes).
    n_runway = 0
    for s in layout.shapes:
        if s.role not in (ROLE_RUNWAY, ROLE_JUNCTION):
            continue
        coords, alts = _shape_corner_alts(s)
        if alts is None:
            continue
        new_alts = list(alts)
        touched = False
        for k, (x, y) in enumerate(coords):
            b = vertex_bucket(x, y)
            if b in targets and abs(new_alts[k] - targets[b]) > 1e-6:
                new_alts[k] = targets[b]
                touched = True
        if touched:
            s.node_altitudes = new_alts + [new_alts[0]]
            s.altitude = None
            s.altitude_high = None
            s.altitude_low = None
            if s.role == ROLE_RUNWAY:
                n_runway += 1
    return n_runway


def _make_slope_sampler(
        s: BuiltShape) -> Callable[[float, float], float] | None:
    """Build a closure that samples a sloped 4-corner rect's
    elevation at any (x, y) by projecting onto the high-mid → low-mid
    axis.  Returns None when ``s`` isn't a 4-corner sloped rect.
    """
    if s.altitude_high is None or s.altitude_low is None:
        return None
    if s.polygon is None or s.polygon.is_empty:
        return None
    try:
        coords = list(s.polygon.exterior.coords)
    except _GEOM_EXC:
        return None
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) != 4:
        return None
    high_mid_x = 0.5 * (coords[0][0] + coords[3][0])
    high_mid_y = 0.5 * (coords[0][1] + coords[3][1])
    low_mid_x = 0.5 * (coords[1][0] + coords[2][0])
    low_mid_y = 0.5 * (coords[1][1] + coords[2][1])
    ax = low_mid_x - high_mid_x
    ay = low_mid_y - high_mid_y
    L2 = ax * ax + ay * ay
    H = float(s.altitude_high)
    L = float(s.altitude_low)
    if L2 < 1e-6:
        avg = 0.5 * (H + L)
        return lambda x, y: avg

    def sample(x: float, y: float) -> float:
        t = ((x - high_mid_x) * ax + (y - high_mid_y) * ay) / L2
        if t < 0.0:
            t = 0.0
        elif t > 1.0:
            t = 1.0
        return H + t * (L - H)
    return sample


def _clip_sloping_rect_piece(
        orig: BuiltShape,
        piece: Polygon,
        cut_union,
        slope_sampler: Callable[[float, float], float] | None,
        layout=None,
        dem=None,
        tile_lat: int = 0,
        tile_lon: int = 0,
) -> list[BuiltShape] | None:
    """Split a sliced sloping taxi rect into a clean 4-corner sloped
    rect (the bulk) plus a small ``node_altitudes`` filler at the slice.

    A taxi rect slopes only along its ``source_axis`` and is flat across
    its width — an invariant the solver enforces, but ONLY for 4-corner
    ``altitude_high``/``altitude_low`` rects.  When the tile slice crosses
    such a rect (especially obliquely, e.g. SPLP taxiway A at a shallow
    angle to lon=-77) the default cut converts the whole non-rectangular
    piece to ``node_altitudes``, dropping the constraint and letting the
    surface tilt sideways.

    Instead, clip the rect back along its axis to a clean perpendicular
    end positioned just clear of the slice, keep that bulk as a 4-corner
    sloped rect, and emit the remaining wedge (between the clean end and
    the slice) as a ``node_altitudes`` filler so there's no gap.  The
    filler is bounded by the rect's flat clean end and the (separately
    flattened) seam edge, so it stays effectively flat across too.

    Returns ``[clean_rect, filler...]`` or ``None`` to fall back to the
    default per-vertex conversion (non-trivial geometry: far end also
    cut, multi-crossing, degenerate clip, etc.).
    """
    if orig.altitude_high is None or orig.altitude_low is None:
        return None
    try:
        oc = list(orig.polygon.exterior.coords)
    except _GEOM_EXC:
        return None
    if oc and oc[0] == oc[-1]:
        oc = oc[:-1]
    if len(oc) != 4:
        return None
    c0, c1, c2, c3 = oc  # [H, L, L, H] convention
    H = float(orig.altitude_high)
    L = float(orig.altitude_low)
    # Axis = high-edge midpoint → low-edge midpoint.
    hx, hy = 0.5 * (c0[0] + c3[0]), 0.5 * (c0[1] + c3[1])
    lx, ly = 0.5 * (c1[0] + c2[0]), 0.5 * (c1[1] + c2[1])
    avx, avy = lx - hx, ly - hy
    axis_len2 = avx * avx + avy * avy
    if axis_len2 < 1.0:
        return None

    def _t(px: float, py: float) -> float:
        """Axis projection: 0 at the high-edge midpoint, 1 at the low."""
        return ((px - hx) * avx + (py - hy) * avy) / axis_len2

    t0, t1, t2, t3 = (_t(*c0), _t(*c1), _t(*c2), _t(*c3))
    t_min, t_max = min(t0, t1, t2, t3), max(t0, t1, t2, t3)

    # Axis-projections of the piece's vertices that sit on the cut edge.
    try:
        cut_boundary = cut_union.boundary
        pc = list(piece.exterior.coords)
    except _GEOM_EXC:
        return None
    if pc and pc[0] == pc[-1]:
        pc = pc[:-1]
    cut_ts: list[float] = []
    for px, py in pc:
        try:
            if Point(px, py).distance(cut_boundary) < 0.75:
                cut_ts.append(_t(px, py))
        except _GEOM_EXC:
            continue
    if not cut_ts:
        return None
    mean_cut = sum(cut_ts) / len(cut_ts)
    margin_t = 2.0 / math.sqrt(axis_len2)

    def _pt_at_proj(pa, ta, pb, tb, s):
        """Point on segment ``pa``→``pb`` at axis-projection ``s``.

        Solving for the projection (not the raw edge parameter) is what
        makes the clipped edge truly PERPENDICULAR to the axis even when
        the input rect has an oblique seam edge (from the upstream
        seam-split), so the two long edges don't share a parameter scale.
        """
        denom = tb - ta
        if abs(denom) < 1e-9:
            return None
        u = (s - ta) / denom
        if u < -0.05 or u > 1.05:
            return None
        u = min(1.0, max(0.0, u))
        return (pa[0] + u * (pb[0] - pa[0]),
                pa[1] + u * (pb[1] - pa[1]))

    # The two long edges (parallel to the axis): c0→c1 and c3→c2.
    if mean_cut > 0.5 * (t_min + t_max):
        # Cut at the HIGH-t (low) end; keep the LOW-t (high) side.
        s_clip = min(cut_ts) - margin_t
        if s_clip <= t_min + 1e-3:
            return None
        far0, far3 = c0, c3
    else:
        # Cut at the LOW-t (high) end; keep the HIGH-t (low) side.
        s_clip = max(cut_ts) + margin_t
        if s_clip >= t_max - 1e-3:
            return None
        far0, far3 = c1, c2
    P1 = _pt_at_proj(c0, t0, c1, t1, s_clip)  # on long edge c0→c1
    P2 = _pt_at_proj(c3, t3, c2, t2, s_clip)  # on long edge c3→c2
    if P1 is None or P2 is None:
        return None

    # Degeneracy guard: the clean rect's two long edges (far0→P1 and
    # far3→P2) should be roughly parallel and similar in length — that's
    # what makes it a clean sloped rect.  When the slice grazes a short
    # or oblique stub the clip produces a lop-sided trapezoid (e.g. SPLP
    # taxiway-stub: 11 m vs 3 m long edges → the H→L drop falls over just
    # 3 m = a ~16 % grade the solver can't honour).  Bail so the caller
    # falls back to a single node_altitudes piece the solver can grade.
    e1 = math.hypot(P1[0] - far0[0], P1[1] - far0[1])
    e2 = math.hypot(P2[0] - far3[0], P2[1] - far3[1])
    if min(e1, e2) < 5.0 or max(e1, e2) > 2.0 * max(min(e1, e2), 1e-6):
        return None

    # Clean rect ring: the far short edge (far0,far3) + the new
    # perpendicular edge (P1,P2).  Order [far0, P1, P2, far3] keeps the
    # two long edges intact (far0–P1 and far3–P2) and the new edge P1–P2
    # perpendicular to the axis.
    clean_ring = [far0, P1, P2, far3]
    e_far = 0.5 * (slope_sampler(*far0) + slope_sampler(*far3))
    e_clip = 0.5 * (slope_sampler(*P1) + slope_sampler(*P2))
    # [H, L, L, H] needs the higher pair at ring positions 0 & 3.
    if e_far >= e_clip:
        alt_hi, alt_lo = e_far, e_clip
    else:
        clean_ring = [P1, far0, far3, P2]
        alt_hi, alt_lo = e_clip, e_far

    try:
        clean_poly = Polygon(clean_ring)
        if not clean_poly.is_valid or clean_poly.is_empty:
            return None
        # Must be clear of the slice and stay within the kept piece
        # (the latter fails when the far end was also cut → fall back).
        if clean_poly.intersection(cut_union).area > 1.0:
            return None
        if clean_poly.difference(piece).area > 1.0:
            return None
    except _GEOM_EXC:
        return None

    clean_s = copy.copy(orig)
    clean_s.polygon = clean_poly
    if orig.role == ROLE_RUNWAY:
        # Runways are per-vertex from birth (user 2026-07-06); the
        # clean ring is built in the canonical [H, L, L, H] corner
        # order (see the _rect_from_axis_extended note below), so the
        # values map directly.
        _corner_values = [round(alt_hi, 2), round(alt_lo, 2),
                          round(alt_lo, 2), round(alt_hi, 2)]
        clean_s.node_altitudes = _corner_values + [_corner_values[0]]
        clean_s.altitude_high = None
        clean_s.altitude_low = None
        clean_s.altitude = None
    else:
        clean_s.altitude_high = round(alt_hi, 2)
        clean_s.altitude_low = round(alt_lo, 2)
        clean_s.altitude = None
        clean_s.node_altitudes = None
    out: list[BuiltShape] = [clean_s]

    # Filler = the slice-side remainder of the kept piece — the wedge
    # between the perpendicular clip edge and the actual (oblique) slice.
    # Built by intersecting ``piece`` with the NEAR half-plane of the
    # clip line (toward the cut), NOT ``piece.difference(clean_poly)``:
    # the explicit clean ring's far edge need not bit-match ``piece``'s
    # boundary, and the boolean difference then wraps around the far end
    # into a ring instead of yielding the small wedge.
    ax_norm = math.sqrt(axis_len2)
    ux, uy = avx / ax_norm, avy / ax_norm           # unit axis (t↑)
    nx, ny = -uy, ux                                 # unit perpendicular
    qx, qy = hx + s_clip * avx, hy + s_clip * avy    # point on clip line
    # Near (cut-ward) axis direction: +u when the cut is at high t,
    # else -u.
    if mean_cut > 0.5 * (t_min + t_max):
        ndx, ndy = ux, uy
    else:
        ndx, ndy = -ux, -uy
    big = 100000.0
    try:
        near_hp = Polygon([
            (qx - nx * big, qy - ny * big),
            (qx + nx * big, qy + ny * big),
            (qx + nx * big + ndx * big, qy + ny * big + ndy * big),
            (qx - nx * big + ndx * big, qy - ny * big + ndy * big),
        ])
        fdiff = piece.intersection(near_hp)
    except _GEOM_EXC:
        fdiff = None
    if fdiff is not None and not fdiff.is_empty:
        if fdiff.geom_type == "Polygon":
            fpieces = [fdiff]
        elif fdiff.geom_type == "MultiPolygon":
            fpieces = [g for g in fdiff.geoms
                       if g.geom_type == "Polygon" and not g.is_empty]
        else:
            fpieces = []
        for fp in fpieces:
            if fp.area < 0.5:
                continue
            fs = _build_piece_shape(orig, fp, slope_sampler)
            if fs is None:
                continue
            # The filler's nodes on the slice edge follow TERRAIN, not
            # the rect's (clamped) slope: at a steep crossing the slice
            # spans tilted terrain, so these must differ from one another
            # rather than collapse to the rect's flat end value.  Nodes
            # shared with the clean rect's perpendicular edge (P1/P2)
            # keep that flat value so the rect↔filler join stays seamless.
            _terrain_pin_slice_nodes(
                fs, cut_union, (P1, P2), layout, dem, tile_lat, tile_lon)
            out.append(fs)
    return out


def _pin_runway_piece_to_profile(fs, cut_union, layout) -> bool:
    """Rewrite a cut runway piece's ``node_altitudes`` from the runway's
    REDISTRIBUTED FAA profile and record its slice-edge vertex buckets as
    seam anchors.

    The profile (``runway_redistribute.sample_redistributed_profile``) is
    laterally flat and already anchored to the seam DEM at the
    centerline-boundary crossing, so every piece vertex — including the
    band-edge corners an oblique seam fans across the runway's width —
    lands on one FAA-compliant, cross-tile-deterministic surface.
    Replaces both the nearest-neighbour resample from the parent ring
    (which grabbed whichever vertex was closest — the SPLP 4.6 m
    cross-seam step of 2026-06-20) and the per-vertex raw-DEM pin that
    fixed it (which carved the terrain into the runway — the SPLP 4.2 m
    seam V-notch of 2026-07-03).

    Returns ``False`` (caller falls back to the DEM pin) when no
    redistributed profile exists for the piece's ref.
    """
    from .runway_redistribute import sample_redistributed_profile
    if (fs.ref is None or fs.polygon is None or fs.polygon.is_empty
            or layout is None):
        return False
    try:
        coords = list(fs.polygon.exterior.coords)
        cut_boundary = cut_union.boundary
    except _GEOM_EXC:
        return False
    if sample_redistributed_profile(layout, fs.ref,
                                    *coords[0][:2]) is None:
        return False
    seam_keys = getattr(layout, "_seam_anchor_keys", None)
    if seam_keys is None:
        seam_keys = set()
        layout._seam_anchor_keys = seam_keys  # type: ignore[attr-defined]
    alts = []
    for (x, y) in coords:
        v = sample_redistributed_profile(layout, fs.ref, x, y)
        if v is None:
            return False
        alts.append(round(float(v), 2))
        try:
            if Point(x, y).distance(cut_boundary) < 0.75:
                seam_keys.add(vertex_bucket(float(x), float(y)))
        except _GEOM_EXC:
            continue
    fs.node_altitudes = alts
    fs.altitude = None
    fs.altitude_high = None
    fs.altitude_low = None
    return True


def _terrain_pin_slice_nodes(fs, cut_union, clip_pts, layout,
                             dem, tile_lat, tile_lon) -> None:
    """Overwrite a filler's slice-edge ``node_altitudes`` with the DEM
    terrain altitude (so they follow the tilted terrain at a steep
    crossing instead of the rect's flat end value), leaving nodes shared
    with the clean rect's perpendicular clip edge (``clip_pts``)
    untouched.  The pinned buckets are recorded on
    ``layout._seam_anchor_keys`` so the per-surface solver HARD-anchors
    them to these terrain altitudes (otherwise the final solve grades
    them back toward the flat rect)."""
    if (dem is None or layout is None
            or fs.polygon is None or fs.polygon.is_empty):
        return
    try:
        cut_boundary = cut_union.boundary
        coords = list(fs.polygon.exterior.coords)
    except _GEOM_EXC:
        return
    nodata = getattr(dem, "nodata", -32768)

    def _dem_at(x: float, y: float) -> float | None:
        try:
            lat, lon = layout.m_to_ll(x, y)
            v = float(dem.alt((lon - tile_lon, lat - tile_lat)))
        except _GEOM_EXC:
            return None
        if v != v or v == nodata:  # NaN / no-data
            return None
        return v

    # Single-solve order (session 51): tile_cut runs PRE-solve, so a
    # cut piece typically has NO altitude data yet (node_altitudes /
    # altitude / altitude_high all None).  DEM-seed every vertex so the
    # slice-edge HARD pins below have a backing array and the solver
    # warm-starts the soft (interior) vertices from terrain.  Guarded on
    # "no altitude data at all" so it never clobbers a solved flat
    # ``altitude`` (post-solve feature pieces keep their tags).
    created_from_dem = False
    if fs.node_altitudes and len(fs.node_altitudes) >= len(coords):
        alts = list(fs.node_altitudes)
    elif (fs.node_altitudes is None and fs.altitude is None
          and fs.altitude_high is None and fs.altitude_low is None):
        alts = [round(_dem_at(x, y) or 0.0, 2) for (x, y) in coords]
        created_from_dem = True
    else:
        return
    seam_keys = getattr(layout, "_seam_anchor_keys", None)
    if seam_keys is None:
        seam_keys = set()
        layout._seam_anchor_keys = seam_keys  # type: ignore[attr-defined]
    changed = False
    # AIRSIDE pins are RUNWAY-CLAMPED (user SPLP report 2026-07-03): the
    # raw-DEM pin sat metres below the design surface next to a runway,
    # making the pin↔runway chain infeasible — the solve split the
    # violation into a V-notch at the seam (mirrored on both tiles).  The
    # clamp floor comes from CIFP-profiled runways, identical on both
    # tiles, so cross-tile continuity is preserved.  Runway pieces keep
    # their own gated pin path (profile authority; see RUNWAY_SEAM_DEM_PIN).
    from .seam_anchors import runway_clamp_floor
    _clamp = (fs.role in _PIN_SLICE_ROLES)
    for i, (x, y) in enumerate(coords):
        # Skip the corners shared with the clean rect (the flat join).
        if any(math.hypot(x - cp[0], y - cp[1]) < 0.5 for cp in clip_pts):
            continue
        try:
            if Point(x, y).distance(cut_boundary) >= 0.75:
                continue  # not a slice-edge node
        except _GEOM_EXC:
            continue
        v = _dem_at(x, y)
        if v is None:
            continue
        if _clamp:
            try:
                f = runway_clamp_floor(layout, x, y)
            except _GEOM_EXC:
                f = None
            if f is not None and f > v:
                v = f
        alts[i] = round(v, 2)
        seam_keys.add(vertex_bucket(float(x), float(y)))
        changed = True
    if changed or created_from_dem:
        fs.node_altitudes = alts
        if created_from_dem:
            # The piece is now a DEM-seeded node_altitudes shape; clear
            # the (None) rect tags so the solver treats it consistently.
            fs.altitude_high = None
            fs.altitude_low = None
            fs.altitude = None


def _grade_feature_piece_to_seam_dem(
        fs: BuiltShape,
        cut_union,
        layout,
        dem,
        tile_lat: int,
        tile_lon: int,
        grade_cap: float = TUNNEL_RAMP_MAX_GRADE,
) -> None:
    """Post-solve feature piece cut at a tile seam: pin its slice-edge
    vertices to the seam DEM and grade every other vertex from the seam
    toward the piece's design elevation at ≤ ``grade_cap``.

    Per user 2026-06-10: every shape must MATCH the terrain at the tile
    boundary — the neighbouring tile renders raw DEM there — and ramp
    back to its own level inside the tile.  Without this, a depressed-
    road plate (apt_elev−8 m) or a deck-level retaining wall cut at the
    seam presents an 8 m vertical face against the next tile's terrain.

    Mutates ``fs`` in place: converts to ``node_altitudes`` (seam
    vertices at DEM, interior clamped to the cap-feasible band toward
    the design elevation).  No-op when the piece has no slice-edge
    vertex or no elevation data.
    """
    if (dem is None or layout is None
            or fs.polygon is None or fs.polygon.is_empty):
        return
    try:
        cut_boundary = cut_union.boundary
        coords = list(fs.polygon.exterior.coords)
    except _GEOM_EXC:
        return
    if len(coords) < 4:
        return
    # Per-vertex DESIGN elevations from the piece's existing tags
    # (``_build_piece_shape`` has already converted sloped quads to
    # node_altitudes; flat plates carry ``altitude``).
    if fs.node_altitudes and len(fs.node_altitudes) >= len(coords):
        design = [float(a) for a in fs.node_altitudes[:len(coords)]]
    elif fs.altitude is not None:
        design = [float(fs.altitude)] * len(coords)
    else:
        return
    nodata = getattr(dem, "nodata", -32768)

    def _dem_at(x: float, y: float) -> float | None:
        try:
            lat, lon = layout.m_to_ll(x, y)
            v = float(dem.alt((lon - tile_lon, lat - tile_lat)))
        except _GEOM_EXC:
            return None
        if v != v or v == nodata:
            return None
        return v

    seam: list[tuple[float, float, float]] = []   # (x, y, dem_alt)
    seam_alt_by_idx: dict[int, float] = {}
    for i, (x, y) in enumerate(coords):
        try:
            if Point(x, y).distance(cut_boundary) >= 0.75:
                continue
        except _GEOM_EXC:
            continue
        v = _dem_at(x, y)
        if v is None:
            continue
        seam.append((float(x), float(y), v))
        seam_alt_by_idx[i] = v
    if not seam:
        return
    new_alts: list[float] = []
    for i, (x, y) in enumerate(coords):
        if i in seam_alt_by_idx:
            new_alts.append(round(seam_alt_by_idx[i], 2))
            continue
        lo = float("-inf")
        hi = float("inf")
        for sx, sy, sv in seam:
            d = math.hypot(x - sx, y - sy)
            hi = min(hi, sv + grade_cap * d)
            lo = max(lo, sv - grade_cap * d)
        if lo > hi:                      # conflicting seam anchors
            new_alts.append(round(0.5 * (lo + hi), 2))
            continue
        new_alts.append(round(min(max(design[i], lo), hi), 2))
    fs.node_altitudes = new_alts
    fs.altitude = None
    fs.altitude_high = None
    fs.altitude_low = None


def _build_piece_shape(
        orig: BuiltShape,
        piece: Polygon,
        slope_sampler: Callable[[float, float], float] | None,
) -> BuiltShape | None:
    """Construct a BuiltShape for one cut piece, copying tags from
    ``orig`` and resampling altitudes for the new polygon vertices.

    * Flat shape (``altitude`` set, ``altitude_high`` None):
      keeps ``altitude`` unchanged; ``node_altitudes`` cleared.
    * Sloped 4-corner rect: convert to ``node_altitudes`` by
      projecting each new vertex onto the original H→L axis.  The
      polygon's vertex count typically differs from 4 post-cut so
      the legacy [H, L, L, H] convention no longer applies.
    * Per-vertex ``node_altitudes``: resample via nearest-neighbour
      against the original ring.
    """
    new_s = copy.copy(orig)
    new_s.polygon = piece

    # Flat with single altitude — corner count irrelevant.
    if orig.altitude is not None and orig.altitude_high is None:
        new_s.altitude = orig.altitude
        new_s.altitude_high = None
        new_s.altitude_low = None
        new_s.node_altitudes = None
        return new_s

    # Sloped 4-corner rect → per-vertex node_altitudes.
    if slope_sampler is not None:
        try:
            coords = list(piece.exterior.coords)
        except _GEOM_EXC:
            return None
        alts = [round(float(slope_sampler(x, y)), 2)
                for (x, y) in coords]
        new_s.node_altitudes = alts
        new_s.altitude = None
        new_s.altitude_high = None
        new_s.altitude_low = None
        return new_s

    # Per-vertex node_altitudes → resample via NN.
    if orig.node_altitudes:
        try:
            old_coords = list(orig.polygon.exterior.coords)
        except _GEOM_EXC:
            return None
        if old_coords and old_coords[0] == old_coords[-1]:
            old_open = old_coords[:-1]
        else:
            old_open = old_coords
        from .elevation import _resample_node_altitudes_nn
        new_alts = _resample_node_altitudes_nn(
            piece, old_open, orig.node_altitudes)
        if new_alts is not None:
            new_s.node_altitudes = new_alts
        return new_s

    # No elevation data — keep as-is with the new polygon.
    return new_s
