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
The v2 planner additionally excludes collinear mid-edge ring vertices from
the pair enumeration (``config.HOLE_ROUTER_MID_EDGE_PRUNE``, track T3c):
it blocks them in every Dijkstra call anyway, so their edges are provably
dead — on residue rings dense with collinear vertices this removes most of
the pair count without changing a single planned cut.
"""
from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.prepared import prep

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

# Chunk size for the vectorized all-pairs visibility pass — bounds the transient
# LineString array to a few hundred MB even on the largest aprons while keeping
# the shapely C-loop batches large enough to amortize call overhead.
_VIS_PAIR_CHUNK = 400_000

# Numerical slack: segment endpoints sit on ring vertices, so a "stays inside"
# test must tolerate float noise at the boundary, and a 1-D overlap shorter
# than this is treated as a point-touch (corner), not an edge-hug.
_EPS_M = 0.05

# Interior parameters (fraction of the way from endpoint A to endpoint B) at
# which the vectorized adjacency pass samples each candidate chord for the
# cheap ``contains_xy`` rejection prefilter.  Any sample falling outside the
# eps-buffered pavement proves ``contains(buffered_pavement, segment)`` must be
# False, so the pair can be rejected before the expensive full-segment
# ``contains`` call.  The prefilter is necessary-only: pairs whose samples all
# land inside still go through the full ``contains`` test.
_PREFILTER_SAMPLE_FRACTIONS = (0.2, 0.4, 0.6, 0.8)

__all__ = [
    "HoleRoute",
    "VisibilityGraph",
    "build_graph",
    "build_obstacles",
    "plan_hole_cuts",
    "plan_hole_cuts_v2",
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


def _coord_key(x: float, y: float) -> tuple[int, int]:
    """The 1e-6 m bucket key shared by node dedupe and index lookups."""
    return (round(x * 1e6), round(y * 1e6))


# Collinearity threshold for mid-edge classification: a ring vertex whose two
# incident edges are within ~1 degree of straight merely SUBDIVIDES an edge.
_COLLINEAR_COS = -math.cos(math.radians(1.0))   # cos(179 deg)


def _collinear_mid_edge_keys(polygon: Polygon,
                             extra_keys: set[tuple[int, int]]
                             ) -> set[tuple[int, int]]:
    """Coordinate keys of ring vertices that merely subdivide a straight edge.

    Along a subtracted rect the residue ring runs flush with the rect side, so
    such a vertex sits on the rect-edge INTERIOR — the v2 planner refuses them
    as attachment points (a cut attaching there decouples the piece from the
    rect downstream; HECA U-connector) and blocks them as waypoints in every
    Dijkstra call.  True corners and global/shared ``extra_keys`` stay legal.

    Returns an empty set on any geometry error (conservative: nothing is
    classified, so nothing is blocked or pruned).
    """
    keys: set[tuple[int, int]] = set()

    def _mark(ring_obj) -> None:
        pts_r = _ring_pts(ring_obj)
        m = len(pts_r)
        if m < 3:
            return
        for ii in range(m):
            ax, ay = pts_r[(ii - 1) % m]
            bx, by = pts_r[ii]
            cx, cy = pts_r[(ii + 1) % m]
            v1x, v1y = ax - bx, ay - by
            v2x, v2y = cx - bx, cy - by
            n1 = math.hypot(v1x, v1y)
            n2 = math.hypot(v2x, v2y)
            if n1 < 1e-9 or n2 < 1e-9:
                continue
            if (v1x * v2x + v1y * v2y) / (n1 * n2) >= _COLLINEAR_COS:
                continue                      # a real corner
            key = _coord_key(bx, by)
            if key in extra_keys:
                continue                      # shared neighbour corner
            keys.add(key)

    try:
        _mark(polygon.exterior)
        for ring_obj in polygon.interiors:
            _mark(ring_obj)
    except _GEOM_EXC:
        return set()
    return keys


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


def _build_adjacency_scalar(nodes, ppoly_buf, boundary, obstacles, eps_m,
                            excluded=frozenset()):
    """Reference O(V^2) adjacency: the original per-pair ``_visible`` double
    loop.  Kept verbatim as the byte-identity oracle for the vectorized path.

    ``excluded`` node indices take part in no pair (T3c mid-edge prune): the
    caller guarantees they can never be traversed, so their edges are dead.
    """
    n = len(nodes)
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    for i in range(n):
        if i in excluded:
            continue
        xi, yi = nodes[i]
        for j in range(i + 1, n):
            if j in excluded:
                continue
            xj, yj = nodes[j]
            if _visible((xi, yi), (xj, yj), ppoly_buf, boundary,
                        obstacles, eps_m):
                d = math.hypot(xi - xj, yi - yj)
                adj[i].append((j, d))
                adj[j].append((i, d))
    return adj


def _build_adjacency_vectorized(nodes, buf_poly, boundary, obstacles, eps_m,
                                excluded=frozenset()):
    """Vectorized equivalent of :func:`_build_adjacency_scalar` using shapely-2
    batch predicates.  Produces a BYTE-IDENTICAL adjacency (same edge set, same
    per-list order): pairs are enumerated in the identical ascending ``(i, j)``
    upper-triangle order, and each surviving pair is appended to ``adj[i]`` /
    ``adj[j]`` in that order — so any equal-cost Dijkstra tie downstream breaks
    the same way.

    The predicate chain is applied in the SAME sequence as ``_visible``
    (length > 0, then ``contains`` on the eps-buffered pavement, then the
    boundary-run rejection, then per-obstacle interior/edge rejection); only the
    GEOS calls are batched.  ``shapely.contains(buf, segs)`` is elementwise
    identical to ``prep(buf).contains(seg)`` (GEOS PreparedContains == Contains),
    verified against the scalar path on real fixture geometry.

    Two sound (verdict-preserving) prunes cut the GEOS work without changing a
    single edge:

    * Sampled ``contains_xy`` prefilter — before any LineString is built,
      each candidate chord is probed at the interior parameters in
      ``_PREFILTER_SAMPLE_FRACTIONS``.  ``contains(buf_poly, seg)`` requires
      EVERY point of the segment to lie in ``buf_poly``, so one sample outside
      proves the pair fails; only pairs with all samples inside pay for the
      LineString build + full ``contains``.

    * ``relate_pattern`` prune for the boundary-run stage —
      ``_max_line_len(seg ∩ boundary) > eps`` needs a 1-dimensional component
      in the intersection, which (both operands being lineal, so the Polygon
      branch of ``_max_line_len`` is unreachable) requires the DE-9IM
      interior/interior entry to have dimension 1.  The vectorized
      ``relate_pattern(segs, boundary, "1********")`` is therefore a sound
      necessary condition; the expensive ``intersection`` + ``_max_line_len``
      run only on its hits.  Point-touches (segment endpoints on ring
      vertices) yield 0-D entries and are skipped for free.
    """
    n = len(nodes)
    adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    if n < 2:
        return adj
    coords = np.asarray(nodes, dtype=float)          # (n, 2)
    xs = coords[:, 0]
    ys = coords[:, 1]
    # Upper-triangle pair indices in row-major (ascending i, then j) order —
    # exactly the scalar double-loop order.
    ii, jj = np.triu_indices(n, k=1)
    if excluded:
        # T3c mid-edge prune: drop pairs with an excluded endpoint before any
        # GEOS work.  Filtering the ascending pair stream preserves the scalar
        # enumeration order of the surviving pairs.
        excluded_mask = np.zeros(n, dtype=bool)
        excluded_mask[list(excluded)] = True
        pair_alive = ~(excluded_mask[ii] | excluded_mask[jj])
        ii = ii[pair_alive]
        jj = jj[pair_alive]
    total = ii.shape[0]
    for start in range(0, total, _VIS_PAIR_CHUNK):
        stop = min(start + _VIS_PAIR_CHUNK, total)
        I = ii[start:stop]
        J = jj[start:stop]
        ax = xs[I]; ay = ys[I]
        bx = xs[J]; by = ys[J]
        length = np.hypot(ax - bx, ay - by)
        keep = length > 1e-9
        if not keep.any():
            continue
        # Sampled contains_xy rejection prefilter (sound, necessary-only):
        # a pair with ANY interior sample outside the eps-buffered pavement
        # cannot satisfy ``contains(buf_poly, seg)`` — reject it before the
        # far more expensive per-segment LineString build + contains call.
        for sample_fraction in _PREFILTER_SAMPLE_FRACTIONS:
            candidate_indices = np.flatnonzero(keep)
            if candidate_indices.size == 0:
                break
            sample_x = (ax[candidate_indices]
                        + sample_fraction
                        * (bx[candidate_indices] - ax[candidate_indices]))
            sample_y = (ay[candidate_indices]
                        + sample_fraction
                        * (by[candidate_indices] - ay[candidate_indices]))
            sample_inside = shapely.contains_xy(buf_poly, sample_x, sample_y)
            keep[candidate_indices[~sample_inside]] = False
        if not keep.any():
            continue
        # Build LineStrings only for the surviving pairs.
        sub = np.flatnonzero(keep)
        seg_coords = np.empty((sub.shape[0], 2, 2), dtype=float)
        seg_coords[:, 0, 0] = ax[sub]
        seg_coords[:, 0, 1] = ay[sub]
        seg_coords[:, 1, 0] = bx[sub]
        seg_coords[:, 1, 1] = by[sub]
        # np.asarray is a no-op passthrough on the geometry ndarray but gives
        # the type checker the array (indexable) type the stubs lack.
        segs = np.asarray(shapely.linestrings(seg_coords))
        # contains on the eps-buffered pavement.
        alive = shapely.contains(buf_poly, segs)
        if not alive.any():
            continue
        # Boundary-run rejection: reject where the longest 1-D component of the
        # seg∩boundary exceeds eps (a chord hugging an existing edge).  A 1-D
        # component requires the DE-9IM interior/interior entry to be
        # 1-dimensional, so the vectorized relate_pattern is a sound necessary
        # condition — the expensive intersection + _max_line_len measurement
        # runs only on its (rare) hits.
        survivor_indices = np.flatnonzero(alive)
        boundary_run_mask = shapely.relate_pattern(
            segs[survivor_indices], boundary, "1********")
        boundary_run_indices = survivor_indices[boundary_run_mask]
        if boundary_run_indices.size:
            boundary_overlaps = shapely.intersection(
                segs[boundary_run_indices], boundary)
            for m, geom in zip(boundary_run_indices, boundary_overlaps):
                if _max_line_len(geom) > eps_m:
                    alive[m] = False
        # Obstacle interior/edge rejection, per obstacle, same order/semantics.
        if obstacles:
            for ob, _pob, _corners in obstacles:
                aidx = np.flatnonzero(alive)
                if aidx.size == 0:
                    break
                hit = shapely.intersects(segs[aidx], ob)
                hidx = aidx[hit]
                if hidx.size == 0:
                    continue
                obinter = shapely.intersection(segs[hidx], ob)
                for m, geom in zip(hidx, obinter):
                    if _max_line_len(geom) > eps_m:
                        alive[m] = False
        # Emit surviving edges in ascending (i, j) order.  The weight is
        # recomputed with math.hypot on the node coords — NOT taken from the
        # numpy ``length`` array — because np.hypot and math.hypot can disagree
        # in the last ULP, and the scalar path uses math.hypot; byte-identity
        # of the edge weight (hence of every downstream Dijkstra tie) requires
        # the identical scalar call.  ``length`` is used only for the coarse
        # >1e-9 degeneracy mask, which is insensitive to a 1-ULP shift.
        surv = sub[np.flatnonzero(alive)]
        for k in surv:
            i = int(I[k]); j = int(J[k])
            xi, yi = nodes[i]
            xj, yj = nodes[j]
            d = math.hypot(xi - xj, yi - yj)
            adj[i].append((j, d))
            adj[j].append((i, d))
    return adj


def _build_adjacency(nodes, buf_poly, ppoly_buf, boundary, obstacles, eps_m,
                     excluded=frozenset()):
    """Adjacency dispatch: vectorized batch predicates when
    ``config.VECTORIZED_GEOMETRY`` is on (byte-identical to the scalar path),
    else the reference double loop.  Any failure in the vectorized path falls
    back to the scalar loop — correctness over speed."""
    try:
        from ..config import VECTORIZED_GEOMETRY
    except Exception:
        VECTORIZED_GEOMETRY = True
    if VECTORIZED_GEOMETRY:
        try:
            return _build_adjacency_vectorized(
                nodes, buf_poly, boundary, obstacles, eps_m, excluded)
        except _GEOM_EXC:
            pass
    return _build_adjacency_scalar(nodes, ppoly_buf, boundary, obstacles,
                                   eps_m, excluded)


def build_graph(polygon: Polygon, *,
                obstacles=(),
                extra_nodes: Sequence[tuple[float, float]] = (),
                eps_m: float = _EPS_M,
                excluded_pair_keys: set[tuple[int, int]] = frozenset(),
                ) -> VisibilityGraph | None:
    """All-pairs visibility graph over exterior verts + every hole vert +
    obstacle corners + ``extra_nodes``.  ``None`` if the polygon is degenerate.

    ``excluded_pair_keys`` (T3c mid-edge prune): coordinate keys (see
    ``_coord_key``) of nodes to leave out of the pair enumeration — their
    adjacency lists come back empty and no other list references them.  Only
    a caller that provably never traverses those nodes (the v2 planner, which
    blocks them in every Dijkstra call) may pass this; v1 paths pass nothing
    and keep the full graph.
    """
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
        buf_poly = polygon.buffer(eps_m)
        ppoly_buf = prep(buf_poly)
    except _GEOM_EXC:
        return None
    boundary = polygon.boundary

    excluded = (
        {i for i, (x, y) in enumerate(nodes)
         if _coord_key(x, y) in excluded_pair_keys}
        if excluded_pair_keys else frozenset()
    )
    adj = _build_adjacency(nodes, buf_poly, ppoly_buf,
                           boundary, obstacles, eps_m, excluded)

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


# ── v2: Prim min-spanning-forest conforming-cuts planner (session 68) ──────
#
# The v1 planner above routes EVERY hole's two bridges independently to the
# nearest exterior node.  On a large apron with many holes the Dijkstra exits
# all converge on the same few reflex corners (HECA: 12 of 50 cut endpoints on
# ONE hub vertex, several cuts degenerate loops with start == end), carving
# needle-thin wedge slices (1–2° apex) between near-parallel bridges.  The
# downstream sliver guards then truncate or drop those needles — uncovering
# source pavement (the HECA 670 m² fan wedge).  v2 instead grows a Prim-style
# spanning forest: each hole connects to the NEAREST point of the already-
# connected boundary network (the exterior ring or a previously-opened hole),
# so bridges are short, chained, and endpoint-shared by construction — no
# parallel duplicate bridges, no fan, no needles.


def _dijkstra_all(adj, sources, blocked=frozenset()):
    """Multi-source Dijkstra over the whole graph.  Returns ``(dist, prev)``
    arrays; ``blocked`` nodes are impassable (and excluded as sources)."""
    n = len(adj)
    INF = float("inf")
    dist = [INF] * n
    prev = [-1] * n
    pq: list[tuple[float, int]] = []
    for s in sources:
        if s in blocked:
            continue
        dist[s] = 0.0
        heapq.heappush(pq, (0.0, s))
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        for v, w in adj[u]:
            if v in blocked:
                continue
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, prev


def _walk_back(prev, end, sources):
    """Node path ``[source, …, end]`` from a ``_dijkstra_all`` prev array."""
    path = [end]
    u = end
    while u not in sources:
        u = prev[u]
        if u < 0:
            return None
        path.append(u)
    path.reverse()
    return path


def _segments_properly_intersect(a1, a2, b1, b2) -> bool:
    """True iff open segments a1–a2 and b1–b2 cross at a point interior to
    BOTH (shared endpoints do not count)."""
    def orient(p, q, r):
        v = ((q[0] - p[0]) * (r[1] - p[1])
             - (q[1] - p[1]) * (r[0] - p[0]))
        if v > 1e-12:
            return 1
        if v < -1e-12:
            return -1
        return 0
    # Shared endpoint → not a proper crossing.
    for p in (a1, a2):
        for q in (b1, b2):
            if abs(p[0] - q[0]) < 1e-9 and abs(p[1] - q[1]) < 1e-9:
                return False
    o1 = orient(a1, a2, b1)
    o2 = orient(a1, a2, b2)
    o3 = orient(b1, b2, a1)
    o4 = orient(b1, b2, a2)
    return o1 != o2 and o3 != o4 and 0 not in (o1, o2, o3, o4)


def _polyline_crosses(pts_a: Sequence[tuple[float, float]],
                      pts_b: Sequence[tuple[float, float]]) -> bool:
    """True iff any segment of polyline A properly crosses one of B."""
    for i in range(len(pts_a) - 1):
        a1, a2 = pts_a[i], pts_a[i + 1]
        lo_x = min(a1[0], a2[0]) - 1e-9
        hi_x = max(a1[0], a2[0]) + 1e-9
        lo_y = min(a1[1], a2[1]) - 1e-9
        hi_y = max(a1[1], a2[1]) + 1e-9
        for j in range(len(pts_b) - 1):
            b1, b2 = pts_b[j], pts_b[j + 1]
            if (max(b1[0], b2[0]) < lo_x or min(b1[0], b2[0]) > hi_x
                    or max(b1[1], b2[1]) < lo_y
                    or min(b1[1], b2[1]) > hi_y):
                continue
            if _segments_properly_intersect(a1, a2, b1, b2):
                return True
    return False


def _prune_edges_crossing(g: "VisibilityGraph",
                          cut_pts: Sequence[tuple[float, float]]) -> None:
    """Drop every visibility edge that properly crosses the accepted cut —
    later bridges must route AROUND planted cuts, not across them (an X
    crossing between two cuts encloses an island ring in the arrangement)."""
    minx = min(p[0] for p in cut_pts) - 1e-9
    maxx = max(p[0] for p in cut_pts) + 1e-9
    miny = min(p[1] for p in cut_pts) - 1e-9
    maxy = max(p[1] for p in cut_pts) + 1e-9
    for i in range(len(g.nodes)):
        xi, yi = g.nodes[i]
        kept = []
        changed = False
        for j, w in g.adj[i]:
            xj, yj = g.nodes[j]
            if (max(xi, xj) < minx or min(xi, xj) > maxx
                    or max(yi, yj) < miny or min(yi, yj) > maxy):
                kept.append((j, w))
                continue
            if _polyline_crosses(((xi, yi), (xj, yj)), cut_pts):
                changed = True
                continue
            kept.append((j, w))
        if changed:
            g.adj[i] = kept


def _void_path(hole_poly: Polygon, a, b,
               eps_m: float) -> list[tuple[float, float]] | None:
    """Polyline from ``a`` to ``b`` staying inside the hole VOID (used when
    the straight chord between the two bridge feet would clip pavement on a
    non-convex hole).  ``None`` if no route exists."""
    try:
        if hole_poly.is_empty or hole_poly.geom_type != "Polygon":
            return None
        return route_between(hole_poly, [a], [b], eps_m=eps_m)
    except _GEOM_EXC:
        return None


def plan_hole_cuts_v2(polygon: Polygon, *,
                      obstacles=(),
                      eps_m: float = _EPS_M,
                      min_hole_area: float = 50.0,
                      extra_nodes: Sequence[tuple[float, float]] = (),
                      ) -> list[LineString]:
    """Plan one conforming SPLIT cut per interior hole ≥ ``min_hole_area`` as
    a Prim-style minimum-spanning-forest of slits.

    One visibility graph is built for the WHOLE polygon (every ring vertex +
    obstacle corner + ``extra_nodes`` — shared/global points, e.g. neighbour-
    shape corners sitting on this polygon's boundary).  Holes are then opened
    nearest-first: the next hole is the one closest to the CONNECTED boundary
    network (initially the exterior ring; thereafter also every opened hole's
    ring and every planted bridge), and its cut is

        ``A …bridge-1… H_a — (void crossing) — H_b …bridge-2… B``

    with bridge-2 node-disjoint from bridge-1 (so the two bridges can never
    pinch a zero-width wedge at a shared endpoint — the v1 fan failure).  The
    void crossing is the straight chord when it stays inside the hole, else a
    route through the hole interior.  Cuts must be APPLIED in the returned
    order: a later cut may end on a ring that an earlier cut exposes.

    Holes with no two disjoint visible bridges are SKIPPED (left in place for
    the caller's legacy-guillotine fallback on that piece alone).
    """
    # Ring vertices that merely SUBDIVIDE a straight edge (collinear within
    # ~1 degree) are not conforming attachment points (see
    # ``_collinear_mid_edge_keys``) and are blocked as waypoints in every
    # Dijkstra call below — so their visibility edges are provably dead.
    # Under ``config.HOLE_ROUTER_MID_EDGE_PRUNE`` (T3c) they are excluded
    # from the O(V^2) pair enumeration outright; the planned cuts are
    # identical either way.
    extra_keys = {_coord_key(float(x), float(y)) for (x, y) in extra_nodes}
    mid_edge_keys = _collinear_mid_edge_keys(polygon, extra_keys)
    try:
        from ..config import HOLE_ROUTER_MID_EDGE_PRUNE
    except Exception:
        HOLE_ROUTER_MID_EDGE_PRUNE = True

    g = build_graph(polygon, obstacles=obstacles, eps_m=eps_m,
                    extra_nodes=extra_nodes,
                    excluded_pair_keys=(mid_edge_keys
                                        if HOLE_ROUTER_MID_EDGE_PRUNE
                                        else frozenset()))
    if g is None:
        return []
    mid_edge = {i for i, (x, y) in enumerate(g.nodes)
                if _coord_key(x, y) in mid_edge_keys}
    interiors = list(polygon.interiors)
    INF = float("inf")

    # Big-hole node sets (graph idx) + hole polygons.
    remaining: dict[int, set[int]] = {}
    hole_polys: dict[int, Polygon] = {}
    for k, ring_idxs in enumerate(g.hole_rings):
        idxs = {i for i in ring_idxs if i is not None}
        if len(idxs) < 3:
            continue
        try:
            hp = Polygon(interiors[k])
            if hp.area < min_hole_area:
                continue
        except _GEOM_EXC:
            continue
        remaining[k] = idxs
        hole_polys[k] = hp

    if not remaining:
        return []

    # Connected boundary network: exterior ring vertices + any extra node
    # sitting ON the exterior ring (a neighbour-shape corner mid-edge).
    # Extra nodes sitting ON a big hole's ring join that hole's node set —
    # they are legal conforming bridge FEET for that hole.
    connected: set[int] = set(g.ext_idx)
    all_hole_idx = set().union(*remaining.values())
    try:
        ext_line = polygon.exterior
        for i, (x, y) in enumerate(g.nodes):
            if i in connected or i in all_hole_idx:
                continue
            pt = Point(x, y)
            if ext_line.distance(pt) <= eps_m:
                connected.add(i)
                continue
            for k in remaining:
                if interiors[k].distance(pt) <= eps_m:
                    remaining[k].add(i)
                    all_hole_idx.add(i)
                    break
    except _GEOM_EXC:
        pass

    def _crossing(ha: int, hb: int, k: int) -> list[tuple[float, float]] | None:
        """The void-crossing polyline from node ``ha`` to ``hb`` of hole
        ``k``: the straight chord if it does not clip pavement (and does not
        run along the ring), else a detour through the hole's interior (its
        representative point — handles triangle holes where every vertex
        chord IS a ring edge), else a routed path through the void.
        ``None`` if nothing works."""
        pa, pb = g.nodes[ha], g.nodes[hb]
        if math.hypot(pa[0] - pb[0], pa[1] - pb[1]) <= 1e-9:
            return None

        def _stays_in_void(pts: list[tuple[float, float]]) -> bool:
            try:
                return _max_line_len(
                    LineString(pts).intersection(polygon)) <= eps_m
            except _GEOM_EXC:
                return False

        if _stays_in_void([pa, pb]):
            return [pa, pb]
        try:
            rp = hole_polys[k].representative_point()
            mid = (float(rp.x), float(rp.y))
            if _stays_in_void([pa, mid, pb]):
                return [pa, mid, pb]
        except _GEOM_EXC:
            pass
        via = _void_path(hole_polys[k], pa, pb, eps_m)
        if via is not None and len(via) >= 2:
            return via
        return None

    cuts: list[LineString] = []
    while remaining:
        # No bridge may pass THROUGH an unopened hole's ring node: a cut
        # polyline merely TOUCHING a still-closed void makes shapely's
        # split merge that void into the cut (HECA: three holes fused
        # into one 85 k m² leftover ring).  Unopened ring nodes are
        # therefore blocked as waypoints and reached only as bridge FEET
        # via their last visibility edge.
        unopened = set().union(*remaining.values())

        # Bridge 1: nearest unopened hole from the connected network.
        dist, prev = _dijkstra_all(g.adj, connected,
                                   blocked=unopened | mid_edge)
        best = None
        for k, idxs in remaining.items():
            for t in idxs:
                if t in mid_edge:
                    continue
                for j, w in g.adj[t]:
                    if j in unopened or dist[j] >= INF:
                        continue
                    c = dist[j] + w
                    if best is None or c < best[0]:
                        best = (c, k, t, j)
        if best is None:
            break                        # nothing reachable — caller falls back
        _, k, ha, j1 = best
        p1 = _walk_back(prev, j1, connected)
        if p1 is None:
            remaining.pop(k)
            continue
        p1 = p1 + [ha]

        # Bridge 2: from the network back to the same hole, node-disjoint
        # from bridge 1 so the two bridges cannot pinch a zero-width wedge
        # at a shared vertex (the v1 fan/needle failure).
        hole_idx = remaining[k]
        p1_set = set(p1)
        blocked2 = (unopened - {ha}) | p1_set | mid_edge
        src2 = connected - p1_set
        dist2, prev2 = _dijkstra_all(g.adj, src2, blocked=blocked2)
        feet: list[tuple[float, int, int]] = []
        for t in hole_idx:
            if t == ha or t in mid_edge:
                continue
            for j, w in g.adj[t]:
                if j in blocked2 or dist2[j] >= INF:
                    continue
                feet.append((dist2[j] + w, t, j))
        feet.sort()
        cut_pts = None
        accepted_p2 = None
        tried_feet: set[int] = set()
        p1_pts = [g.nodes[i] for i in p1]
        for _c, foot, j2 in feet:
            if foot in tried_feet:
                continue
            tried_feet.add(foot)
            mid = _crossing(ha, foot, k)
            if mid is None:
                continue
            p2 = _walk_back(prev2, j2, src2)
            if p2 is None:
                continue
            p2 = p2 + [foot]
            # The two bridges are node-disjoint but may still CROSS each
            # other mid-pavement — the X would enclose an island ring in
            # the arrangement (a piece with a leftover interior ring).
            if _polyline_crosses([g.nodes[i] for i in p2], p1_pts):
                continue
            #   A …bridge-1… H_a  +  void crossing  +  H_b …bridge-2… B
            cut_pts = ([g.nodes[i] for i in p1]
                       + list(mid[1:-1])
                       + [g.nodes[i] for i in reversed(p2)])
            accepted_p2 = p2
            break
        if cut_pts is None:
            remaining.pop(k)             # guillotine fallback handles this one
            continue

        clean: list[tuple[float, float]] = []
        for p in cut_pts:
            if clean and (abs(p[0] - clean[-1][0]) < 1e-9
                          and abs(p[1] - clean[-1][1]) < 1e-9):
                continue
            clean.append(p)
        if len(clean) < 2:
            remaining.pop(k)
            continue
        cuts.append(LineString(clean))
        # Later bridges must route around this cut, never across it.
        _prune_edges_crossing(g, clean)

        # The opened hole's ring + both bridges join the connected network.
        connected |= hole_idx | p1_set | set(accepted_p2)
        remaining.pop(k)

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
