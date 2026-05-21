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
    ROLE_APRON, ROLE_TERMINAL, vertex_bucket,
)


# Taxi rects whose elevation slopes ALONG ``source_axis`` only — their
# cross-section is flat (enforced by the solver, but only while they
# stay 4-corner ``altitude_high``/``altitude_low`` rects).  When the
# tile slice crosses one we clip it back to a clean perpendicular end
# so the bulk keeps that flat-cross-section invariant; see
# ``_clip_sloping_rect_piece``.
_SLOPING_RECT_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR,
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
    ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_APRON, ROLE_TERMINAL,
})


def cut_layout_at_tile_boundaries(
        layout: PavementLayout,
        half_width_m: float = 5.0,
        min_piece_area_m2: float = 1.0,
        current_tile_lat: int | None = None,
        current_tile_lon: int | None = None,
        dem=None) -> int:
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

    # Cut the airport-boundary polygon itself.  Downstream
    # consumers (Ortho4XP encoder / boundary ribbon emit / DEM
    # bridge) see the cut MultiPolygon and naturally produce
    # per-tile geometry instead of spanning-tile artifacts.
    if (layout.airport_boundary is not None
            and not layout.airport_boundary.is_empty):
        try:
            ab_cut = layout.airport_boundary.difference(cut_union)
        except _GEOM_EXC:
            ab_cut = None
        if ab_cut is not None and not ab_cut.is_empty:
            if ab_cut.geom_type in ("Polygon", "MultiPolygon"):
                layout.airport_boundary = ab_cut

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

    # Also clip ``layout.airport_boundary`` to the current tile.
    if (layout.airport_boundary is not None
            and not layout.airport_boundary.is_empty):
        ab = layout.airport_boundary
        if ab.geom_type == "MultiPolygon":
            kept = [g for g in ab.geoms
                    if g.geom_type == "Polygon" and not g.is_empty
                    and _in_current_tile(g)]
            if not kept:
                layout.airport_boundary = None
            elif len(kept) == 1:
                layout.airport_boundary = kept[0]
            else:
                from shapely.geometry import MultiPolygon
                layout.airport_boundary = MultiPolygon(kept)
        elif ab.geom_type == "Polygon":
            if not _in_current_tile(ab):
                layout.airport_boundary = None

    n_before = len(layout.shapes)
    new_shapes: list[BuiltShape] = []
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
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
                # (user 2026-05-20).  Taxi rects only for now — junctions
                # /aprons are graded soft against the smoothed DEM seed.
                if s.role in _SLOPING_RECT_ROLES:
                    _terrain_pin_slice_nodes(
                        new_s, cut_union, (), layout, dem,
                        cur_tile_lat, cur_tile_lon)
                new_shapes.append(new_s)
    layout.shapes = new_shapes
    return len(layout.shapes) - n_before


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
                targets[rb] = round(tgt, 1)
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
    clean_s.altitude_high = round(alt_hi, 1)
    clean_s.altitude_low = round(alt_lo, 1)
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
    if (dem is None or layout is None or not fs.node_altitudes
            or fs.polygon is None or fs.polygon.is_empty):
        return
    try:
        cut_boundary = cut_union.boundary
        coords = list(fs.polygon.exterior.coords)
    except _GEOM_EXC:
        return
    nodata = getattr(dem, "nodata", -32768)
    alts = list(fs.node_altitudes)
    if len(alts) < len(coords):
        return
    seam_keys = getattr(layout, "_seam_anchor_keys", None)
    if seam_keys is None:
        seam_keys = set()
        layout._seam_anchor_keys = seam_keys  # type: ignore[attr-defined]
    changed = False
    for i, (x, y) in enumerate(coords):
        # Skip the corners shared with the clean rect (the flat join).
        if any(math.hypot(x - cp[0], y - cp[1]) < 0.5 for cp in clip_pts):
            continue
        try:
            if Point(x, y).distance(cut_boundary) >= 0.75:
                continue  # not a slice-edge node
            lat, lon = layout.m_to_ll(x, y)
            v = float(dem.alt((lon - tile_lon, lat - tile_lat)))
        except _GEOM_EXC:
            continue
        if v != v or v == nodata:  # NaN / no-data
            continue
        alts[i] = round(v, 1)
        seam_keys.add(vertex_bucket(float(x), float(y)))
        changed = True
    if changed:
        fs.node_altitudes = alts


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
        alts = [round(float(slope_sampler(x, y)), 1)
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
