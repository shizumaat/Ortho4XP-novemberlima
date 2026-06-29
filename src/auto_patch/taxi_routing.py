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

__all__ = ["TaxiRouteGraph", "build_taxi_route_graph", "taxi_route_distance",
           "shared_taxi_route_graph", "augment_with_runway_centerlines",
           "runway_augmented_route_graph"]

# Centerline vertices within this distance (m) are treated as the same graph
# node, so abutting taxiway segments join.  Small relative to taxiway spacing.
_SNAP_TOL_M = 3.0


class TaxiRouteGraph:
    """Undirected graph of taxiway-centerline segments joined at shared
    endpoints.  ``adj[key] = [(other_key, length_m), ...]``; ``coord[key] =
    (x, y)`` in layout-local metres.

    ``aug`` holds the keys ADDED by runway-centerline augmentation (see
    ``augment_with_runway_centerlines``).  Augmented nodes are ROUTE
    segments and ANCHOR entry points only — a pavement vertex must never
    take an augmented node as its nearest graph entry, or a vertex sitting
    in a route-graph COVERAGE HOLE near a runway gets a tight fictitious
    band through a straight perpendicular hop across non-pavement instead
    of the weak band the hole should produce (measured at CYXY TX1, s76:
    nearest plain node 345 m, nearest midline node ~60 m → 11 false
    route-band violations on a corridor profile the curve-aware model had
    legally written)."""

    __slots__ = ("adj", "coord", "tol", "aug", "edge_cap")

    def __init__(self, adj, coord, tol, aug=None, edge_cap=None):
        self.adj = adj
        self.coord = coord
        self.tol = tol
        self.aug = aug if aug is not None else set()
        # Per-edge grade cap keyed by the SORTED node-pair (ka, kb) with
        # ka <= kb — the rise/run a route is allowed to climb along that
        # taxiway segment, from its ICAO code letter (narrow A/B = 3 %,
        # C–F = 1.5 %).  Empty/absent edge → caller's uniform fallback cap.
        # Used by ``_runway_reach_bands`` to compute width-aware band
        # ceilings (config ``TAXI_REACH_BAND_BY_WIDTH``).
        self.edge_cap = edge_cap if edge_cap is not None else {}

    @staticmethod
    def _ekey(ka, kb):
        return (ka, kb) if ka <= kb else (kb, ka)

    def _key(self, x: float, y: float) -> Tuple[int, int]:
        return (int(round(x / self.tol)), int(round(y / self.tol)))

    def copy(self) -> "TaxiRouteGraph":
        """Independent copy (adjacency lists + coord dict are duplicated) so a
        caller may AUGMENT it (e.g. with runway centerlines) without mutating a
        shared cached instance."""
        return TaxiRouteGraph({k: list(v) for k, v in self.adj.items()},
                              dict(self.coord), self.tol, set(self.aug),
                              dict(self.edge_cap))

    def nearest_key(self, x: float, y: float, plain_only: bool = False
                    ) -> Tuple[Optional[Tuple[int, int]], float]:
        """The graph node nearest ``(x, y)`` and its distance (m).
        ``plain_only`` skips augmentation-added nodes (see class doc —
        required for pavement-VERTEX queries on an augmented graph)."""
        best = None
        bd = float("inf")
        aug = self.aug if plain_only else None
        for k, (cx, cy) in self.coord.items():
            if aug is not None and k in aug:
                continue
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
    from .config import taxi_grade_cap_for_letter
    adj: Dict[Tuple[int, int], List[Tuple[Tuple[int, int], float]]] = {}
    coord: Dict[Tuple[int, int], Tuple[float, float]] = {}
    edge_cap: Dict[Tuple[Tuple[int, int], Tuple[int, int]], float] = {}
    g = TaxiRouteGraph(adj, coord, tol_m, edge_cap=edge_cap)
    centerlines = getattr(layout, "apt_taxi_centerlines", None) or []
    for entry in centerlines:
        ls = entry.line if hasattr(entry, "line") else (entry[0] if isinstance(entry, (tuple, list)) else entry)
        ref = entry[1] if (isinstance(entry, (tuple, list))
                           and len(entry) > 1) else None
        # Per-segment grade cap from the taxiway's ICAO code letter (gate
        # TAXI_GRADE_BY_WIDTH off → uniform TAXI_MAX_GRADE → byte-identical).
        cap = float(taxi_grade_cap_for_letter(
            entry.dominant_size() if hasattr(entry, "dominant_size") else None))
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
            # A shared node between a narrow and a wider taxiway: keep the
            # LOOSER (larger) cap on the joint edge so the climb the narrow
            # taxiway is entitled to is never clipped by an abutting wide one.
            ek = g._ekey(ka, kb)
            prev = edge_cap.get(ek)
            edge_cap[ek] = cap if prev is None else max(prev, cap)
    return g


def shared_taxi_route_graph(layout) -> TaxiRouteGraph:
    """The per-layout CACHED centerline route graph (built once per solve and
    shared by the enforce bands, the corridor pass, the terminal seed and the
    flex path — they used to each rebuild it).  Callers must NOT mutate the
    returned instance; augmenting callers take ``.copy()`` first (see
    ``augment_with_runway_centerlines``)."""
    g = getattr(layout, "_taxi_route_graph_cache", None)
    if g is None:
        g = build_taxi_route_graph(layout)
        try:
            layout._taxi_route_graph_cache = g
        except (AttributeError, TypeError):
            pass
    return g


def augment_with_runway_centerlines(G: TaxiRouteGraph, layout,
                                    skip_ref: Optional[str] = None,
                                    bridge_m: float = 40.0) -> None:
    """AUGMENT ``G`` (in place — pass a copy of a shared graph) with RUNWAY
    centerline segments: the apt.dat taxi-route rows stop at/near the runway
    edge, so runway-end/threshold anchors are otherwise unreachable through the
    graph (s68 — the legitimate other-runway demand never formed).  Each
    4-corner runway piece contributes its cross-end midpoint pair as an edge
    (pieces share cross-ends, so the chain connects along the runway); each
    midpoint also bridges to the nearest PRE-EXISTING taxi node within
    ``bridge_m`` (the taxi rows' on-runway endpoints).

    ``skip_ref``: exclude that runway's own pieces — the flex demand path must
    not let a flexing runway's threshold ride its own interior as a fictitious
    1.5 % rise-corridor (the s68 false 102.09 ceiling)."""
    taxi_nodes_snapshot = list(G.coord.values())

    def _aug_edge(pa, pb):
        ka, kb = G._key(*pa), G._key(*pb)
        if ka not in G.coord:
            G.coord[ka] = pa
            G.aug.add(ka)          # added by augmentation, not a taxi row
        if kb not in G.coord:
            G.coord[kb] = pb
            G.aug.add(kb)
        if ka == kb:
            return
        w = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        G.adj.setdefault(ka, []).append((kb, w))
        G.adj.setdefault(kb, []).append((ka, w))

    mids: List[Tuple[float, float]] = []
    for s in layout.shapes:
        if s.role != "runway" or s.polygon is None or s.polygon.is_empty:
            continue
        if skip_ref is not None and (s.ref or "") == skip_ref:
            continue        # no rise-corridor along the flexing runway
        ring = list(s.polygon.exterior.coords)
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) != 4:
            continue
        edges4 = [(ring[k], ring[(k + 1) % 4]) for k in range(4)]
        edges4.sort(key=lambda ab: math.hypot(
            ab[1][0] - ab[0][0], ab[1][1] - ab[0][1]))
        m0 = ((edges4[0][0][0] + edges4[0][1][0]) / 2.0,
              (edges4[0][0][1] + edges4[0][1][1]) / 2.0)
        m1 = ((edges4[1][0][0] + edges4[1][1][0]) / 2.0,
              (edges4[1][0][1] + edges4[1][1][1]) / 2.0)
        _aug_edge(m0, m1)
        mids.append(m0)
        mids.append(m1)
    for mp in mids:
        best_pt = None
        best_d = bridge_m
        for (tx, ty) in taxi_nodes_snapshot:
            d = math.hypot(tx - mp[0], ty - mp[1])
            if d < best_d:
                best_d = d
                best_pt = (tx, ty)
        if best_pt is not None:
            _aug_edge(mp, best_pt)


def runway_augmented_route_graph(layout) -> TaxiRouteGraph:
    """Per-layout CACHED copy of the shared route graph augmented with ALL
    runway centerlines — the graph for the route-field long-range law (the
    enforce's reach bands), where runways are HARD at solved values and a route
    along a runway is a physically real path."""
    g = getattr(layout, "_taxi_route_graph_rwy_cache", None)
    if g is None:
        g = shared_taxi_route_graph(layout).copy()
        augment_with_runway_centerlines(g, layout)
        try:
            layout._taxi_route_graph_rwy_cache = g
        except (AttributeError, TypeError):
            pass
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
