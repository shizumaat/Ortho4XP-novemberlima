"""Visibility-graph router for opening apron holes with clean, rect-aware cuts.

PHASE 1 (pure geometry, NOT yet wired into the pipeline).  Given an apron
polygon that still carries interior holes (pavement islands / groundside
cut-outs / embedded taxi rects), this computes the shortest *in-pavement*
polyline from a hole to the exterior boundary that:

  1. stays inside the pavement (never crosses a hole / void),
  2. never crosses a rect interior, and
  3. touches a rect only at a CORNER — it never runs along a rect edge and
     never plants a vertex on a rect edge interior.

This is the routing primitive that will replace the full-span centroid
guillotine in ``_decompose_polygon_with_holes`` (see the session-61 plan).
The user's "radiate a slice out, stop at the rect, jump to a corner on the far
side, continue" is exactly a shortest path on a visibility graph whose only
nodes that sit on a rect are its corners — so corner-to-corner "jumps" fall out
for free.

Rects are obstacles.  In the real residue most rects are ALREADY subtracted, so
they appear as interior rings (holes) of the apron polygon and are avoided by
the in-pavement test automatically — their corners are already polygon
vertices, hence already graph nodes.  The optional ``obstacles`` argument
carries any rect whose footprint is NOT cleanly subtracted (overlap residue),
adding its corners as nodes and its interior as a hard no-cross region.

Perf note: the graph is all-pairs visibility, O(V^2) prepared-geometry
``contains`` tests (V = exterior verts + every hole vert + obstacle corners).
Fine for a per-apron op; Phase 2 can prune to reflex vertices if needed.
"""
from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Polygon
from shapely.prepared import prep

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

# Numerical slack: segment endpoints sit on ring vertices, so a "stays inside"
# test must tolerate float noise at the boundary, and a 1-D overlap shorter
# than this is treated as a point-touch (corner), not an edge-hug.
_EPS_M = 0.05

__all__ = [
    "HoleRoute",
    "VisibilityGraph",
    "build_graph",
    "build_obstacles",
    "plan_hole_cuts",
    "route_between",
    "route_hole_opening",
]


@dataclass
class HoleRoute:
    """Result of routing a hole-opening cut.

    ``path`` is the ordered polyline in local metres: it starts on the target
    hole's ring and ends on the exterior ring, pivoting at rect corners in
    between.  ``rect_corner_pivots`` are the interior waypoints (corners the cut
    bends around).  ``line`` is the same polyline as a ``LineString``.
    """
    path: list[tuple[float, float]]
    line: LineString
    rect_corner_pivots: list[tuple[float, float]] = field(default_factory=list)


def _ring_pts(ring) -> list[tuple[float, float]]:
    """Open coordinate list (no closing duplicate) for a ring/coords."""
    cs = list(ring.coords) if hasattr(ring, "coords") else list(ring)
    if len(cs) >= 2 and cs[0] == cs[-1]:
        cs = cs[:-1]
    return [(float(x), float(y)) for x, y in cs]


def _max_line_len(geom) -> float:
    """Longest 1-D (LineString) component of ``geom``; 0 for point/empty."""
    if geom is None or geom.is_empty:
        return 0.0
    gt = geom.geom_type
    if gt in ("LineString", "LinearRing"):
        return geom.length
    if gt in ("MultiLineString", "GeometryCollection", "MultiPolygon",
              "MultiPoint"):
        best = 0.0
        for g in geom.geoms:
            best = max(best, _max_line_len(g))
        return best
    if gt == "Polygon":
        return geom.length  # treat a degenerate poly intersection as 1-D
    return 0.0


def build_obstacles(polys: Sequence[Polygon]):
    """Prepare an obstacle list ``[(poly, prepared, corner_pts), ...]`` for
    :func:`route_between` from rect footprints (anything cuts must not cross
    except at corners)."""
    out = []
    for p in polys:
        if p is None or p.is_empty or p.geom_type != "Polygon":
            continue
        out.append((p, prep(p), _ring_pts(p.exterior)))
    return out


def _visible(a: tuple[float, float], b: tuple[float, float],
             ppoly_buf, boundary, obstacles, eps: float) -> bool:
    """True iff the open segment a–b is a legal cut chord: inside the pavement,
    not running along the pavement boundary, and touching every obstacle only
    at a corner (point), never through its interior or along an edge."""
    seg = LineString([a, b])
    if seg.length <= 1e-9:
        return False
    try:
        if not ppoly_buf.contains(seg):
            return False
        # No running ALONG the pavement boundary (exterior OR a hole ring) —
        # that would be a degenerate no-op cut hugging an existing edge.
        if _max_line_len(seg.intersection(boundary)) > eps:
            return False
        # Each obstacle (rect): a >point intersection means the chord either
        # crosses the interior or hugs an edge — both illegal.  A bare corner
        # touch is a Point (length 0) and allowed.
        for ob, pob, _corners in obstacles:
            if pob.intersects(seg):
                if _max_line_len(seg.intersection(ob)) > eps:
                    return False
    except _GEOM_EXC:
        return False
    return True


def _dedupe_nodes(pts: Sequence[tuple[float, float]]):
    """Unique node list + index map, bucketed at 1e-6 m so coincident ring /
    corner points collapse to one graph node."""
    nodes: list[tuple[float, float]] = []
    index: dict[tuple[int, int], int] = {}
    for x, y in pts:
        key = (round(x * 1e6), round(y * 1e6))
        if key in index:
            continue
        index[key] = len(nodes)
        nodes.append((float(x), float(y)))
    return nodes, index


def _node_idx(index, x, y):
    return index.get((round(x * 1e6), round(y * 1e6)))


@dataclass
class VisibilityGraph:
    """Reusable in-pavement visibility graph for one polygon.  Build ONCE per
    apron (the O(V^2) cost), then route many holes against it via
    :func:`_dijkstra_path` (the Phase-1 perf carry-over)."""
    nodes: list[tuple[float, float]]
    index: dict[tuple[int, int], int]
    adj: list[list[tuple[int, float]]]
    ext_idx: set[int]                       # node idxs on the exterior ring
    hole_rings: list[list[int]]             # node idxs per interior ring


def build_graph(polygon: Polygon, *,
                obstacles=(),
                extra_nodes: Sequence[tuple[float, float]] = (),
                eps_m: float = _EPS_M) -> VisibilityGraph | None:
    """All-pairs visibility graph over exterior verts + every hole vert +
    obstacle corners + ``extra_nodes``.  ``None`` if the polygon is degenerate."""
    if polygon is None or polygon.is_empty or polygon.geom_type != "Polygon":
        return None
    ext_pts = _ring_pts(polygon.exterior)
    hole_pts_list = [_ring_pts(r) for r in polygon.interiors]

    pts: list[tuple[float, float]] = list(ext_pts)
    for h in hole_pts_list:
        pts.extend(h)
    for _ob, _pob, corners in obstacles:
        pts.extend(corners)
    pts.extend((float(x), float(y)) for x, y in extra_nodes)

    nodes, index = _dedupe_nodes(pts)
    n = len(nodes)
    if n < 2:
        return None
    try:
        ppoly_buf = prep(polygon.buffer(eps_m))
    except _GEOM_EXC:
        return None
    boundary = polygon.boundary

    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for i in range(n):
        xi, yi = nodes[i]
        for j in range(i + 1, n):
            xj, yj = nodes[j]
            if _visible((xi, yi), (xj, yj), ppoly_buf, boundary,
                        obstacles, eps_m):
                d = math.hypot(xi - xj, yi - yj)
                adj[i].append((j, d))
                adj[j].append((i, d))

    ext_idx = {k for k in (_node_idx(index, x, y) for x, y in ext_pts)
               if k is not None}
    hole_rings = [[k for k in (_node_idx(index, x, y) for x, y in h)
                   if k is not None] for h in hole_pts_list]
    return VisibilityGraph(nodes, index, adj, ext_idx, hole_rings)


def _dijkstra_path(adj, src_idx: set[int],
                   tgt_idx: set[int]) -> list[int] | None:
    """Shortest multi-source→multi-target node path; ``None`` if unreachable.
    A target that coincides with a source is not accepted (no zero path)."""
    n = len(adj)
    INF = float("inf")
    dist = [INF] * n
    prev = [-1] * n
    pq: list[tuple[float, int]] = []
    for s in src_idx:
        dist[s] = 0.0
        heapq.heappush(pq, (0.0, s))
    reached = -1
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        if u in tgt_idx and u not in src_idx:
            reached = u
            break
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if reached < 0:
        return None
    path = []
    u = reached
    while u != -1:
        path.append(u)
        if u in src_idx:
            break
        u = prev[u]
    path.reverse()
    return path if len(path) >= 2 else None


def route_between(polygon: Polygon,
                  sources: Sequence[tuple[float, float]],
                  targets: Sequence[tuple[float, float]],
                  *,
                  obstacles=(),
                  extra_nodes: Sequence[tuple[float, float]] = (),
                  eps_m: float = _EPS_M) -> list[tuple[float, float]] | None:
    """Shortest in-pavement, rect-corner-aware polyline from ANY ``sources``
    point to ANY ``targets`` point, or ``None`` if none exists."""
    g = build_graph(polygon, obstacles=obstacles, eps_m=eps_m,
                    extra_nodes=list(extra_nodes) + list(sources)
                    + list(targets))
    if g is None:
        return None
    src_idx = {i for i in (_node_idx(g.index, x, y) for x, y in sources)
               if i is not None}
    tgt_idx = {i for i in (_node_idx(g.index, x, y) for x, y in targets)
               if i is not None}
    if not src_idx or not tgt_idx:
        return None
    path = _dijkstra_path(g.adj, src_idx, tgt_idx)
    if not path:
        return None
    return [g.nodes[i] for i in path]


def _diameter_pair(pts: Sequence[tuple[float, float]]):
    """The farthest-apart pair in ``pts`` (the hole's diameter endpoints)."""
    best = -1.0
    pair = (pts[0], pts[-1])
    for i in range(len(pts)):
        xi, yi = pts[i]
        for j in range(i + 1, len(pts)):
            d = (pts[j][0] - xi) ** 2 + (pts[j][1] - yi) ** 2
            if d > best:
                best = d
                pair = (pts[i], pts[j])
    return pair


def _extreme_pair_along(pts, ux, uy):
    """The two ``pts`` extreme along direction ``(ux, uy)`` — the endpoints of
    a cut that crosses the hole IN that direction."""
    keyed = [((p[0] * ux + p[1] * uy), p) for p in pts]
    return min(keyed)[1], max(keyed)[1]


def _route_split_cut(g: "VisibilityGraph", ha, hb) -> "LineString | None":
    """The routed cut ``E_a … H_a — H_b … E_b`` (two bridges from hole verts
    ``ha``/``hb`` to the exterior, joined by the across-void segment), or
    ``None`` if either bridge has no visible route."""
    ia = _node_idx(g.index, *ha)
    ib = _node_idx(g.index, *hb)
    if ia is None or ib is None:
        return None
    pa = _dijkstra_path(g.adj, {ia}, g.ext_idx)
    pb = _dijkstra_path(g.adj, {ib}, g.ext_idx)
    if not pa or not pb:
        return None
    pts = [g.nodes[i] for i in reversed(pa)] + [g.nodes[i] for i in pb]
    clean: list[tuple[float, float]] = []
    for p in pts:
        if clean and (abs(p[0] - clean[-1][0]) < 1e-9
                      and abs(p[1] - clean[-1][1]) < 1e-9):
            continue
        clean.append(p)
    return LineString(clean) if len(clean) >= 2 else None


def _min_piece_area_after(polygon: Polygon, cut: LineString) -> float:
    """Smaller-piece area after splitting ``polygon`` by ``cut`` (a balance
    score — larger is better, avoids slivers); 0 if it fails to split in two."""
    from shapely.ops import split as _shp_split
    try:
        res = _shp_split(polygon, cut)
    except _GEOM_EXC:
        return 0.0
    geoms = (list(res.geoms) if res.geom_type != "Polygon" else [res])
    areas = [g.area for g in geoms
             if g.geom_type == "Polygon" and not g.is_empty]
    return min(areas) if len(areas) >= 2 else 0.0


def plan_hole_cuts(polygon: Polygon, *,
                   obstacles=(),
                   eps_m: float = _EPS_M,
                   min_hole_area: float = 50.0,
                   runway_axis_deg: float | None = None) -> list[LineString]:
    """Return one routed SPLIT cut per interior hole ≥ ``min_hole_area``.

    Each cut is ``E_a … H_a — H_b … E_b``: two visibility-routed bridges from
    two hole vertices (``H_a``, ``H_b``) out to the exterior, joined by the
    across-the-void segment ``H_a–H_b``.  Feeding this to ``shapely.split``
    divides the local pavement in two and turns the hole into a boundary notch
    on each — the void is preserved, no interior ring remains, and (because
    every waypoint is a polygon vertex or rect corner) the cut never plants a
    mid-edge node and jumps rects corner-to-corner.

    STRATEGIC direction (Phase 3): when ``runway_axis_deg`` is given, the cut
    crosses the hole along the runway-PARALLEL or runway-PERPENDICULAR axis —
    the directions the terrain is graded along, so the cut runs WITH the grade
    instead of across it — and the more BALANCED of the two (larger smaller-
    piece area, like the legacy guillotine) is kept.  Without an axis it falls
    back to the hole diameter.  The visibility graph is built ONCE.
    """
    g = build_graph(polygon, obstacles=obstacles, eps_m=eps_m)
    if g is None:
        return []

    dirs: list[tuple[float, float]] = []
    if runway_axis_deg is not None:
        ax = math.pi / 2.0 - math.radians(runway_axis_deg)
        a = ax % math.pi
        b = (ax + math.pi / 2.0) % math.pi
        dirs = [(math.cos(a), math.sin(a)), (math.cos(b), math.sin(b))]

    interiors = list(polygon.interiors)
    cuts: list[LineString] = []
    for k, ring_idxs in enumerate(g.hole_rings):
        ring_idxs = [i for i in ring_idxs if i is not None]
        if len(ring_idxs) < 3:
            continue
        try:
            if Polygon(interiors[k]).area < min_hole_area:
                continue
        except _GEOM_EXC:
            continue
        ring_pts = [g.nodes[i] for i in ring_idxs]

        candidates: list[LineString] = []
        for ux, uy in dirs:
            ha, hb = _extreme_pair_along(ring_pts, ux, uy)
            c = _route_split_cut(g, ha, hb)
            if c is not None:
                candidates.append(c)
        if not candidates:                       # no axis, or both axes failed
            ha, hb = _diameter_pair(ring_pts)
            c = _route_split_cut(g, ha, hb)
            if c is not None:
                candidates.append(c)
        if not candidates:
            continue
        # Keep the most BALANCED cut (largest smaller-piece area).
        best = max(candidates, key=lambda c: _min_piece_area_after(polygon, c))
        cuts.append(best)
    return cuts


def route_hole_opening(polygon: Polygon,
                       *,
                       hole_index: int = 0,
                       obstacles=(),
                       eps_m: float = _EPS_M) -> HoleRoute | None:
    """Route the shortest bridge from interior ring ``hole_index`` to the
    exterior boundary, pivoting at rect corners.  Returns ``None`` if the
    polygon has no such hole or no legal route exists (caller falls back)."""
    if (polygon is None or polygon.is_empty
            or polygon.geom_type != "Polygon"):
        return None
    interiors = list(polygon.interiors)
    if hole_index < 0 or hole_index >= len(interiors):
        return None
    hole_pts = _ring_pts(interiors[hole_index])
    ext_pts = _ring_pts(polygon.exterior)
    if len(hole_pts) < 3 or len(ext_pts) < 3:
        return None

    path = route_between(polygon, hole_pts, ext_pts,
                         obstacles=obstacles, eps_m=eps_m)
    if path is None or len(path) < 2:
        return None

    # Interior waypoints (everything between the hole end and the boundary end)
    # are the rect corners the cut bends around.
    pivots = path[1:-1]
    return HoleRoute(path=path, line=LineString(path),
                     rect_corner_pivots=list(pivots))
