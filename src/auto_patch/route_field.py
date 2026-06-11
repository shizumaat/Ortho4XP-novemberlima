"""Route-field long-range grade law (#3) — the SHARED violation engine.

Model (user-approved s73-p3, evidence s73-p10g; docs/route_field_model.md):
a pavement vertex's feasible elevation band is

    intersect over anchors a of
        [ E_a - cap*route_d(a, v)*(1 + ROUTE_NOISE_FRAC),
          E_a + cap*route_d(a, v)*(1 + ROUTE_NOISE_FRAC) ]

where ``route_d`` is the shortest path over the taxi-route centerline graph
(+ straight endpoint gaps on both ends), NOT the visibility-chord geodesic —
km-scale chords systematically UNDER-measure the real taxi route and
manufacture infeasibility (HECA: 2.5 km chord chain vs 3.08 km route = 8.5 m
of false demand smeared over ~70 apron "violations").

This module is the ONE engine used by BOTH the validator
(``tools/check_grade.py``) and the runtime WARN
(``elevation._report_within_shape_violations``) so the two can never drift
(the s64 lesson).  It is deliberately layout-free: callers adapt their data
(an emitted OSM, or live layout shapes) into plain coordinate lists.

A vertex far from any centerline gets a WEAK band (the endpoint gap is added
at cap as a straight line) — that is CORRECT behaviour: less long-range
constraint, the local visibility window still applies.  Do NOT add a chord
fallback for coverage holes; that reintroduces the bug this model removes.
"""
from __future__ import annotations

import heapq
import math
from typing import Dict, List, Optional, Sequence, Tuple

from auto_patch.config import ELEV_ROUNDING_NOISE_M, ROUTE_NOISE_FRAC

__all__ = ["route_band_violations", "RouteBandViolation"]

# Same node-snap tolerance as taxi_routing._SNAP_TOL_M (abutting centerline
# segments join) and the same runway-midpoint bridge reach as
# taxi_routing.augment_with_runway_centerlines.
_SNAP_TOL_M = 3.0
_BRIDGE_M = 40.0
# Spatial-grid cell for nearest-node queries (performance only — results are
# exact; the expanding-ring search stops once no closer cell can exist).
_GRID_CELL_M = 60.0


class RouteBandViolation:
    """One vertex outside its anchor route band."""
    __slots__ = ("index", "elev", "lo", "hi", "excess_m", "route_d_m",
                 "anchor_xy", "anchor_elev")

    def __init__(self, index, elev, lo, hi, excess_m, route_d_m,
                 anchor_xy, anchor_elev):
        self.index = index            # position in the caller's check_pts
        self.elev = elev
        self.lo = lo                  # margined band (incl. rounding noise)
        self.hi = hi
        self.excess_m = excess_m      # metres outside the band
        self.route_d_m = route_d_m    # route distance to the binding anchor
        self.anchor_xy = anchor_xy    # binding anchor position (m frame)
        self.anchor_elev = anchor_elev


class _NearestGrid:
    """Exact nearest-node lookup over the graph coords via a uniform grid
    with expanding-ring search (the linear scan in TaxiRouteGraph.nearest_key
    is O(|coord|) per query — too slow for a per-vertex audit)."""

    def __init__(self, coord: Dict, cell: float = _GRID_CELL_M):
        self.cell = cell
        self.buckets: Dict[Tuple[int, int], List] = {}
        kxs: List[int] = []
        kys: List[int] = []
        for k, (x, y) in coord.items():
            ck = (int(math.floor(x / cell)), int(math.floor(y / cell)))
            self.buckets.setdefault(ck, []).append((x, y, k))
            kxs.append(ck[0])
            kys.append(ck[1])
        if kxs:
            self.max_r = max(max(kxs) - min(kxs), max(kys) - min(kys)) + 1
        else:
            self.max_r = 0

    def nearest(self, x: float, y: float):
        """(key, distance) of the nearest graph node, or (None, inf)."""
        if not self.buckets:
            return None, float("inf")
        cell = self.cell
        cx = int(math.floor(x / cell))
        cy = int(math.floor(y / cell))
        best_k = None
        best_d = float("inf")
        for r in range(self.max_r + 1):
            # Cells at Chebyshev radius r around (cx, cy).
            if r == 0:
                ring = [(cx, cy)]
            else:
                ring = []
                for dx in range(-r, r + 1):
                    ring.append((cx + dx, cy - r))
                    ring.append((cx + dx, cy + r))
                for dy in range(-r + 1, r):
                    ring.append((cx - r, cy + dy))
                    ring.append((cx + r, cy + dy))
            for ck in ring:
                bucket = self.buckets.get(ck)
                if not bucket:
                    continue
                for (bx, by, bk) in bucket:
                    d = math.hypot(bx - x, by - y)
                    if d < best_d:
                        best_d = d
                        best_k = bk
            # Any node in a cell at Chebyshev radius >= r+1 is at least
            # r*cell away from the query point — safe to stop.
            if best_k is not None and best_d <= r * cell:
                break
        return best_k, best_d


def _build_graph(centerlines_xy: Sequence[Sequence[Tuple[float, float]]],
                 runway_rings: Sequence[Tuple[Sequence[Tuple[float, float]],
                                              Sequence[float]]]):
    """Centerline route graph (same snapping as taxi_routing) AUGMENTED with
    runway-piece centerline segments (cross-end midpoints; the apt.dat taxi
    rows stop at the runway edge, so threshold/runway-end anchors would
    otherwise be unreachable — s68) bridged to the nearest pre-existing taxi
    node within ``_BRIDGE_M``."""
    tol = _SNAP_TOL_M
    adj: Dict[Tuple[int, int], List[Tuple[Tuple[int, int], float]]] = {}
    coord: Dict[Tuple[int, int], Tuple[float, float]] = {}

    aug: set = set()
    in_aug = [False]

    def _key(x, y):
        return (int(round(x / tol)), int(round(y / tol)))

    def _edge(pa, pb):
        ka, kb = _key(*pa), _key(*pb)
        if ka not in coord:
            coord[ka] = pa
            if in_aug[0]:
                aug.add(ka)
        if kb not in coord:
            coord[kb] = pb
            if in_aug[0]:
                aug.add(kb)
        if ka == kb:
            return
        w = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
        adj.setdefault(ka, []).append((kb, w))
        adj.setdefault(kb, []).append((ka, w))

    for line in centerlines_xy:
        for pa, pb in zip(line, line[1:]):
            _edge(pa, pb)
    taxi_nodes_snapshot = list(coord.values())
    in_aug[0] = True          # everything below is runway augmentation
    mids: List[Tuple[float, float]] = []
    for ring, _elevs in runway_rings:
        pts = list(ring)
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        if len(pts) != 4:
            continue          # only clean 4-corner pieces carry a midline
        edges4 = [(pts[k], pts[(k + 1) % 4]) for k in range(4)]
        edges4.sort(key=lambda ab: math.hypot(
            ab[1][0] - ab[0][0], ab[1][1] - ab[0][1]))
        m0 = ((edges4[0][0][0] + edges4[0][1][0]) / 2.0,
              (edges4[0][0][1] + edges4[0][1][1]) / 2.0)
        m1 = ((edges4[1][0][0] + edges4[1][1][0]) / 2.0,
              (edges4[1][0][1] + edges4[1][1][1]) / 2.0)
        _edge(m0, m1)
        mids.append(m0)
        mids.append(m1)
    for mp in mids:
        best_pt = None
        best_d = _BRIDGE_M
        for (tx, ty) in taxi_nodes_snapshot:
            d = math.hypot(tx - mp[0], ty - mp[1])
            if d < best_d:
                best_d = d
                best_pt = (tx, ty)
        if best_pt is not None:
            _edge(mp, best_pt)
    return adj, coord, aug


def route_band_violations(
        centerlines_xy: Sequence[Sequence[Tuple[float, float]]],
        runway_rings: Sequence[Tuple[Sequence[Tuple[float, float]],
                                     Sequence[float]]],
        check_pts: Sequence[Tuple[float, float, float]],
        cap: float,
        noise_frac: float = ROUTE_NOISE_FRAC,
        rounding_noise_m: float = ELEV_ROUNDING_NOISE_M,
) -> List[RouteBandViolation]:
    """Validate ``check_pts`` (``(x, y, elev)`` in one consistent meter frame)
    against the route bands from the RUNWAY anchors (every ``runway_rings``
    vertex with an elevation).  ``cap`` is the single long-range grade cap
    (decimal — every pavement role caps at 1.5 % today, per the design's
    single-capL statement).  Returns one entry per out-of-band vertex."""
    adj, coord, aug = _build_graph(centerlines_xy, runway_rings)
    if not coord:
        return []
    # Two entry grids: ANCHORS (runway vertices — on runway pavement) may
    # enter through the augmented midline nodes; CHECK VERTICES must enter
    # through PLAIN taxi-row nodes only, or a vertex in a route-graph
    # coverage hole near a runway gets a tight fictitious band through a
    # straight hop across non-pavement instead of the hole's weak band
    # (CYXY TX1, s76 — mirrors TaxiRouteGraph.nearest_key(plain_only)).
    grid = _NearestGrid(coord)
    plain_coord = {k: v for k, v in coord.items() if k not in aug}
    if not plain_coord:
        return []
    plain_grid = _NearestGrid(plain_coord)
    mult = cap * (1.0 + noise_frac)

    # Anchor seeds: each runway vertex enters the graph at its nearest node
    # plus the straight endpoint gap at cap.
    anchors: List[Tuple[Tuple[int, int], float, float,
                        Tuple[float, float]]] = []
    for ring, elevs in runway_rings:
        pts = list(ring)
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        for k, (x, y) in enumerate(pts):
            if k >= len(elevs) or elevs[k] is None:
                continue
            key, gap = grid.nearest(x, y)
            if key is None:
                continue
            anchors.append((key, gap, float(elevs[k]), (x, y)))
    if not anchors:
        return []

    def _propagate(sign):
        """Multi-source Dijkstra: dist[k] = min over anchors a of
        (sign*E_a + mult*(gap_a + route)) — with the binding anchor kept for
        provenance."""
        dist: Dict = {}
        src: Dict = {}
        pq: List = []
        for (key, gap, ae, axy) in anchors:
            v0 = sign * ae + mult * gap
            if v0 < dist.get(key, float("inf")):
                dist[key] = v0
                src[key] = (axy, ae)
                heapq.heappush(pq, (v0, key))
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, float("inf")):
                continue
            for v, w in adj.get(u, ()):
                nd = d + mult * w
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    src[v] = src[u]
                    heapq.heappush(pq, (nd, v))
        return dist, src

    ceil_d, ceil_src = _propagate(1.0)
    floor_d, floor_src = _propagate(-1.0)

    out: List[RouteBandViolation] = []
    near_cache: Dict[Tuple[float, float], Tuple] = {}
    for idx, (x, y, elev) in enumerate(check_pts):
        if elev is None:
            continue
        ck = (round(x, 1), round(y, 1))
        hit = near_cache.get(ck)
        if hit is None:
            hit = plain_grid.nearest(x, y)
            near_cache[ck] = hit
        key, gap = hit
        if key is None:
            continue
        hi = lo = None
        c = ceil_d.get(key)
        if c is not None:
            hi = c + mult * gap
        f = floor_d.get(key)
        if f is not None:
            lo = -(f + mult * gap)
        if hi is not None and elev > hi + rounding_noise_m:
            axy, ae = ceil_src[key]
            route_d = (hi - ae) / mult if mult > 0 else 0.0
            out.append(RouteBandViolation(
                idx, elev, lo if lo is not None else float("-inf"),
                hi, elev - hi, route_d, axy, ae))
        elif lo is not None and elev < lo - rounding_noise_m:
            axy, ae = floor_src[key]
            route_d = (ae - lo) / mult if mult > 0 else 0.0
            out.append(RouteBandViolation(
                idx, elev, lo, hi if hi is not None else float("inf"),
                lo - elev, route_d, axy, ae))
    return out
