"""Phase-1 unit tests for the visibility-graph hole-opening router
(``auto_patch.pavement.hole_router``).  Pure synthetic geometry — no airport
build.  Validates: shortest in-pavement bridge, rect-corner "jump" routing,
and the touch-rect-only-at-a-corner invariant (both when the rect is a separate
obstacle and when it is a subtracted hole)."""
from shapely.geometry import LineString, Polygon

from shapely.ops import split as shp_split

from auto_patch.pavement.hole_router import (
    HoleRoute,
    build_obstacles,
    plan_hole_cuts,
    route_between,
    route_hole_opening,
)
from auto_patch.pavement.hole_router import _max_line_len

EPS = 0.05


def _densified_square(side: float, step: float = 10.0) -> list[tuple[float, float]]:
    """Square ring [0,side]^2 with a vertex every ``step`` so the router has
    edge nodes to aim at / pivot through (a real residue boundary is dense)."""
    pts: list[tuple[float, float]] = []
    n = int(round(side / step))
    for i in range(n):           # bottom →
        pts.append((i * step, 0.0))
    for i in range(n):           # right ↑
        pts.append((side, i * step))
    for i in range(n):           # top ←
        pts.append((side - i * step, side))
    for i in range(n):           # left ↓
        pts.append((0.0, side - i * step))
    return pts


def _inside(line: LineString, polygon: Polygon) -> bool:
    return polygon.buffer(EPS).contains(line)


def _no_filled_overlap(line: LineString, rect: Polygon) -> bool:
    """No 1-D overlap with the FILLED rect → neither crosses its interior nor
    hugs an edge."""
    return _max_line_len(line.intersection(rect)) <= EPS


def _touches_only_at_corners(line: LineString, rect: Polygon) -> bool:
    inter = line.intersection(rect.boundary)
    if _max_line_len(inter) > EPS:
        return False                       # a 1-D overlap = edge hug
    corners = list(rect.exterior.coords)[:-1]

    def _is_corner(px, py):
        return any(abs(px - cx) <= EPS and abs(py - cy) <= EPS
                   for cx, cy in corners)

    geoms = [inter] if inter.geom_type == "Point" else list(
        getattr(inter, "geoms", []))
    for g in geoms:
        if g.geom_type == "Point" and not _is_corner(g.x, g.y):
            return False
    return True


# ── 1. straight shortest bridge, no obstacle ────────────────────────────────
def test_straight_bridge_no_obstacle():
    ext = _densified_square(100.0)
    hole = [(45, 45), (55, 45), (55, 55), (45, 55)]
    poly = Polygon(ext, [hole])

    route = route_hole_opening(poly)
    assert isinstance(route, HoleRoute)
    assert len(route.path) == 2                      # single straight chord
    assert route.rect_corner_pivots == []
    assert _inside(route.line, poly)
    # ends: one vertex on the hole ring, one on the exterior ring.
    a, b = route.path[0], route.path[-1]
    assert Polygon(hole).exterior.distance(LineString([a, a]).centroid) <= EPS
    assert poly.exterior.distance(LineString([b, b]).centroid) <= EPS
    assert route.line.length < 60.0


# ── 2. jump around a separate rect obstacle ─────────────────────────────────
def test_route_jumps_around_rect_obstacle():
    poly = Polygon(_densified_square(120.0))
    rect = Polygon([(40, 40), (80, 40), (80, 80), (40, 80)])
    obs = build_obstacles([rect])

    path = route_between(poly, [(10, 60)], [(110, 60)], obstacles=obs)
    assert path is not None
    line = LineString(path)

    assert _inside(line, poly)
    assert _no_filled_overlap(line, rect)            # never through interior
    assert _touches_only_at_corners(line, rect)      # corner contact only
    # the cut must actually bend around a rect corner (the "jump").
    corners = list(rect.exterior.coords)[:-1]
    interior = path[1:-1]
    assert any(abs(px - cx) <= EPS and abs(py - cy) <= EPS
               for (px, py) in interior for (cx, cy) in corners)


# ── 3. the same, but the rect is a SUBTRACTED HOLE (real-residue case) ───────
def test_embedded_rect_hole_is_avoided_without_obstacle_arg():
    rect = [(40, 40), (80, 40), (80, 80), (40, 80)]
    poly = Polygon(_densified_square(120.0), [rect])
    rect_poly = Polygon(rect)

    # No obstacles arg: crossing the hole-void is rejected by the in-pavement
    # test alone, and the rect corners are already polygon vertices.
    path = route_between(poly, [(10, 60)], [(110, 60)])
    assert path is not None
    line = LineString(path)

    assert _inside(line, poly)
    assert _no_filled_overlap(line, rect_poly)
    assert _touches_only_at_corners(line, rect_poly)
    corners = rect
    interior = path[1:-1]
    assert any(abs(px - cx) <= EPS and abs(py - cy) <= EPS
               for (px, py) in interior for (cx, cy) in corners)


# ── 4. no path vertex lands on a rect EDGE interior ─────────────────────────
def test_no_vertex_on_rect_edge_interior():
    poly = Polygon(_densified_square(120.0))
    rect = Polygon([(40, 40), (80, 40), (80, 80), (40, 80)])
    obs = build_obstacles([rect])
    path = route_between(poly, [(10, 60)], [(110, 60)], obstacles=obs)
    assert path is not None
    corners = list(rect.exterior.coords)[:-1]
    for (px, py) in path:
        on_boundary = rect.exterior.distance(LineString(
            [(px, py), (px, py)]).centroid) <= EPS
        if on_boundary:                     # if it touches the rect at all…
            assert any(abs(px - cx) <= EPS and abs(py - cy) <= EPS
                       for cx, cy in corners)   # …it must be a corner


# ── 5. degenerate inputs return None (caller falls back) ────────────────────
def test_none_on_no_hole_or_bad_index():
    poly = Polygon(_densified_square(100.0))          # no holes
    assert route_hole_opening(poly) is None
    hole = [(45, 45), (55, 45), (55, 55), (45, 55)]
    poly_h = Polygon(_densified_square(100.0), [hole])
    assert route_hole_opening(poly_h, hole_index=5) is None


def _apply_cuts(poly: Polygon, cuts):
    pieces = [poly]
    for cut in cuts:
        nxt = []
        for p in pieces:
            if p.geom_type != "Polygon" or not cut.intersects(p):
                nxt.append(p)
                continue
            res = shp_split(p, cut)
            nxt.extend(g for g in getattr(res, "geoms", [res])
                       if g.geom_type == "Polygon" and not g.is_empty)
        pieces = nxt
    return pieces


# ── 6. plan_hole_cuts splits the apron and removes the hole (void preserved) ─
def test_plan_hole_cuts_opens_hole_via_split():
    ext = _densified_square(100.0)
    hole = [(40, 40), (60, 40), (60, 60), (40, 60)]   # 400 m² island
    poly = Polygon(ext, [hole])

    cuts = plan_hole_cuts(poly, min_hole_area=50.0)
    assert len(cuts) == 1

    pieces = _apply_cuts(poly, cuts)
    assert len(pieces) >= 2                             # apron was split
    # no piece keeps an interior ring (hole opened)…
    assert all(len(list(p.interiors)) == 0 for p in pieces)
    # …area conserved (void preserved as notches, nothing paved over)
    assert abs(sum(p.area for p in pieces) - poly.area) < 1.0


# ── 7. multi-hole: one cut per hole, all opened, graph built once ───────────
def test_plan_hole_cuts_multi_hole():
    ext = _densified_square(120.0)
    holes = [
        [(20, 20), (35, 20), (35, 35), (20, 35)],
        [(85, 85), (100, 85), (100, 100), (85, 100)],
    ]
    poly = Polygon(ext, holes)
    cuts = plan_hole_cuts(poly, min_hole_area=50.0)
    assert len(cuts) == 2
    pieces = _apply_cuts(poly, cuts)
    assert all(len(list(p.interiors)) == 0 for p in pieces)
    assert abs(sum(p.area for p in pieces) - poly.area) < 1.0


# ── 8. v2 Prim forest planner: conforming, chained, needle-free ──────────────
def _piece_min_angle_deg(p):
    import math
    ring = list(p.exterior.coords)[:-1]
    n = len(ring)
    worst = 180.0
    for i in range(n):
        ax, ay = ring[(i - 1) % n]
        bx, by = ring[i]
        cx, cy = ring[(i + 1) % n]
        v1 = (ax - bx, ay - by)
        v2 = (cx - bx, cy - by)
        n1 = math.hypot(*v1)
        n2 = math.hypot(*v2)
        if n1 < 1e-9 or n2 < 1e-9:
            continue
        c = max(-1.0, min(1.0, (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
        worst = min(worst, math.degrees(math.acos(c)))
    return worst


def test_plan_hole_cuts_v2_opens_all_holes_exact_tiling():
    from shapely.ops import polygonize, unary_union
    from auto_patch.pavement.hole_router import plan_hole_cuts_v2

    ext = _densified_square(200.0)
    holes = [
        [(20, 20), (40, 20), (40, 40), (20, 40)],
        [(60, 20), (80, 20), (80, 40), (60, 40)],     # neighbour → chaining
        [(150, 150), (170, 150), (170, 170), (150, 170)],
        [(90, 90), (100, 95), (95, 105)],             # triangle hole
    ]
    poly = Polygon(ext, holes)
    cuts = plan_hole_cuts_v2(poly, min_hole_area=50.0)
    assert len(cuts) == 4                              # one cut per hole

    # apply as the production code does: one global arrangement.
    noded = unary_union([poly.boundary] + list(cuts))
    pieces = []
    for f in polygonize(noded):
        c = f.intersection(poly)
        for g in getattr(c, "geoms", [c]):
            if g.geom_type == "Polygon" and not g.is_empty and g.area > 1e-6:
                pieces.append(g)
    # every hole opened (no piece keeps a big interior ring)
    assert all(Polygon(h).area < 50.0
               for p in pieces for h in p.interiors)
    # EXACT tiling: nothing uncovered, nothing paved over the voids
    assert poly.difference(unary_union(pieces)).area < 1e-6
    assert unary_union(pieces).difference(poly).area < 1e-6
    # no needle wedge pieces (the v1 fan failure mode)
    assert all(_piece_min_angle_deg(p) >= 2.0 for p in pieces)


def test_plan_hole_cuts_v2_no_shared_endpoint_fan():
    """Two bridges of one cut must not share an endpoint, and no graph node
    may serve as an endpoint for a pile of cuts (the v1 hub-fan failure)."""
    from collections import Counter
    from auto_patch.pavement.hole_router import plan_hole_cuts_v2

    ext = _densified_square(200.0)
    # a row of holes all nearest to the same boundary region — v1 fanned
    # every bridge into the same exterior vertex here.
    holes = [
        [(30 + dx, 60), (45 + dx, 60), (45 + dx, 75), (30 + dx, 75)]
        for dx in (0, 40, 80, 120)
    ]
    poly = Polygon(ext, holes)
    cuts = plan_hole_cuts_v2(poly, min_hole_area=50.0)
    assert len(cuts) == 4
    ends = Counter()
    for c in cuts:
        cs = list(c.coords)
        assert tuple(cs[0]) != tuple(cs[-1])           # no degenerate loop
        ends[tuple(cs[0])] += 1
        ends[tuple(cs[-1])] += 1
    assert max(ends.values()) <= 2                     # no hub fan
