"""Taxi-route distance along apt.dat taxiway centerlines.

The grade-feasibility band for a runway/pavement point — "how high can this
point be while a connecting point reaches it within grade" — depends on the
distance BETWEEN them, and that distance must be measured *along the taxi route
the aircraft (and the grade) actually follow*, i.e. the taxiway centerlines.

A shortest path over the elevation solver's within-shape grade graph is the
WRONG distance for this: it takes straight node-to-node chords, so it cuts
corners through wide junctions and shortcuts straight across large aprons that a
taxiway merely borders.  At HECA the 05C/23C(T4-join) ↔ 05L/23R(23R-threshold)
route measured 3014 m that way versus ~3236 m along the centerlines (user-
confirmed ~3200 m) — a ~6 % under-count that, at 1.5 %, is ~3 m of elevation
budget and flips the feasibility verdict.  So this module measures distance over
the centerline network instead.

Public API:
    build_taxi_route_graph(layout, tol_m=...) -> TaxiRouteGraph
    taxi_route_distance(layout_or_graph, a_xy, b_xy) -> float | None
"""
from __future__ import annotations

import heapq
import math
from typing import Dict, List, Optional, Tuple

__all__ = ["TaxiRouteGraph", "build_taxi_route_graph", "taxi_route_distance"]

# Centerline vertices within this distance (m) are treated as the same graph
# node, so abutting taxiway segments join.  Small relative to taxiway spacing.
_SNAP_TOL_M = 3.0


class TaxiRouteGraph:
    """Undirected graph of taxiway-centerline segments joined at shared
    endpoints.  ``adj[key] = [(other_key, length_m), ...]``; ``coord[key] =
    (x, y)`` in layout-local metres."""

    __slots__ = ("adj", "coord", "tol")

    def __init__(self, adj, coord, tol):
        self.adj = adj
        self.coord = coord
        self.tol = tol

    def _key(self, x: float, y: float) -> Tuple[int, int]:
        return (int(round(x / self.tol)), int(round(y / self.tol)))

    def nearest_key(self, x: float, y: float
                    ) -> Tuple[Optional[Tuple[int, int]], float]:
        """The graph node nearest ``(x, y)`` and its distance (m)."""
        best = None
        bd = float("inf")
        for k, (cx, cy) in self.coord.items():
            d = math.hypot(cx - x, cy - y)
            if d < bd:
                bd, best = d, k
        return best, bd

    def distances_from(self, src_xy: Tuple[float, float]
                       ) -> Tuple[Dict[Tuple[int, int], float], float]:
        """One Dijkstra from the graph node nearest ``src_xy``: returns
        ``(dist_by_node, src_gap)`` where ``dist_by_node[k]`` is the centerline
        distance from that source node to graph node ``k`` and ``src_gap`` is the
        straight stub from ``src_xy`` to its nearest node.  Add ``src_gap`` (and
        the target's own gap) for an edge-to-edge value.  Amortises many queries
        from one source."""
        src, sd = self.nearest_key(*src_xy)
        if src is None:
            return {}, float("inf")
        dist: Dict[Tuple[int, int], float] = {src: 0.0}
        pq: List[Tuple[float, Tuple[int, int]]] = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            for v, w in self.adj.get(u, ()):  # type: ignore[union-attr]
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist, sd

    def distance(self, a_xy: Tuple[float, float],
                 b_xy: Tuple[float, float],
                 include_endpoint_gaps: bool = True
                 ) -> Optional[float]:
        """Shortest centerline-route distance (m) between the two points, snapped
        to the nearest centerline nodes.  ``include_endpoint_gaps`` adds the
        straight stub from each point to its nearest centerline node (the
        centerline typically stops short of the runway edge), so the result is
        edge-to-edge.  Returns None if the points are not connected."""
        src, sd = self.nearest_key(*a_xy)
        dst, dd = self.nearest_key(*b_xy)
        if src is None or dst is None:
            return None
        if src == dst:
            return (sd + dd) if include_endpoint_gaps else 0.0
        dist: Dict[Tuple[int, int], float] = {src: 0.0}
        pq: List[Tuple[float, Tuple[int, int]]] = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            if u == dst:
                break
            for v, w in self.adj.get(u, ()):  # type: ignore[union-attr]
                nd = d + w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        if dst not in dist:
            return None
        out = dist[dst]
        if include_endpoint_gaps:
            out += sd + dd
        return out


def build_taxi_route_graph(layout, tol_m: float = _SNAP_TOL_M
                           ) -> TaxiRouteGraph:
    """Build the centerline-route graph from ``layout.apt_taxi_centerlines``
    (a list of ``(LineString, ref)``).  Each consecutive centerline-vertex pair
    is an edge weighted by its length; vertices within ``tol_m`` coincide."""
    adj: Dict[Tuple[int, int], List[Tuple[Tuple[int, int], float]]] = {}
    coord: Dict[Tuple[int, int], Tuple[float, float]] = {}
    g = TaxiRouteGraph(adj, coord, tol_m)
    centerlines = getattr(layout, "apt_taxi_centerlines", None) or []
    for entry in centerlines:
        ls = entry[0] if isinstance(entry, (tuple, list)) else entry
        try:
            cs = list(ls.coords)
        except (AttributeError, TypeError):
            continue
        for (x0, y0), (x1, y1) in zip(cs, cs[1:]):
            ka, kb = g._key(x0, y0), g._key(x1, y1)
            coord[ka] = (x0, y0)
            coord[kb] = (x1, y1)
            if ka == kb:
                continue
            w = math.hypot(x1 - x0, y1 - y0)
            adj.setdefault(ka, []).append((kb, w))
            adj.setdefault(kb, []).append((ka, w))
    return g


def taxi_route_distance(layout_or_graph,
                        a_xy: Tuple[float, float],
                        b_xy: Tuple[float, float],
                        include_endpoint_gaps: bool = True
                        ) -> Optional[float]:
    """Convenience wrapper: centerline-route distance (m) between two
    layout-local points.  Accepts a prebuilt ``TaxiRouteGraph`` (reuse it across
    many queries) or a layout (graph built on the fly)."""
    graph = (layout_or_graph if isinstance(layout_or_graph, TaxiRouteGraph)
             else build_taxi_route_graph(layout_or_graph))
    return graph.distance(a_xy, b_xy, include_endpoint_gaps=include_endpoint_gaps)
