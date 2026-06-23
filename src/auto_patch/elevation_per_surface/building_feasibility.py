"""Per-building route-feasibility elevation (user model, 2026-06-22).

A building that touches airside pavement is seated FLAT at the elevation
closest to its DEM that keeps it reachable WITHIN GRADE from EVERY runway
threshold along the real taxi route — the "heaviest anchor" the rest of the
network then grades to (docs/taxi_centerline_grading_plan.md §9).

The route to a building (user's definition, validated on CYXY):

  1. a PERPENDICULAR from the building centroid to the NEAREST taxi
     centerline (named or not — geometry only).  The part of that
     perpendicular inside the taxiway-width corridor climbs at the
     taxiway's own cap; the part beyond (real apron) at the apron cap (1%).
  2. from the foot point, the taxi-route to each threshold along the
     centerline graph at the PER-EDGE per-letter caps (narrow A/B = 3 %,
     wide C–F = 1.5 %) — including the partial first edge from the foot
     point to its graph node (the bit that snapping to the node would drop).

The feasibility BAND is the intersection over ALL thresholds:

  ceiling = min_t ( thr_elev_t + climb_t )
  floor   = max_t ( thr_elev_t − climb_t )

and the seated level is ``clamp(DEM, floor, ceiling)`` — closest to DEM,
could be above or below it.  Buildings NOT touching airside pavement are
omitted (the caller leaves them at their DEM).

This routes on the SAME `TaxiRouteGraph.edge_cap` the reach-bands use, so a
narrow code-A/B arm contributes 3 % only when its size is known — which for
unnamed arms requires the P3a `UNNAMED_TAXI_SIZE` recovery to be on.
"""

from __future__ import annotations

import heapq
import math
from typing import Callable, Dict, List, Tuple

from auto_patch.config import TAXI_MAX_GRADE
from auto_patch.layout import (
    ROLE_APRON, ROLE_BUILDING, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
)

__all__ = ["building_feasible_levels"]

# Pavement a building must touch to count as airside-served (else → DEM).
_AIRSIDE_ROLES = frozenset({
    ROLE_APRON, ROLE_JUNCTION, ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
    ROLE_SECONDARY_PARALLEL, ROLE_STUB, ROLE_CROSS_CONNECTOR,
})
_APRON_CAP = 0.01          # apron grade outside the taxiway-width corridor
_ENTRY_CAP = 0.015         # runway-threshold → nearest taxi node stub
_TAXI_HALF_W_M = 7.5       # taxiway half-width corridor (perp split point)
_TOUCH_TOL_M = 2.0         # building↔airside distance to count as "touching"
_INF = float("inf")


def building_feasible_levels(
        layout,
        thresholds_xyz: List[Tuple[float, float, float]],
        dem_sampler: Callable[[float, float], "float | None"],
) -> Dict[int, float]:
    """Return ``{id(building_shape): seated_level_m}`` for every
    ``ROLE_BUILDING`` that touches airside pavement.

    ``thresholds_xyz``: ``[(x_m, y_m, elev_m)]`` runway thresholds (both ends
    of each runway) at their solved elevations.  ``dem_sampler(x, y)``: DEM
    (metres) at a layout-local point, or None.  Buildings not touching
    airside pavement are omitted — the caller keeps them at their DEM.
    """
    from shapely.geometry import LineString
    from shapely.ops import nearest_points, unary_union

    from auto_patch.taxi_routing import shared_taxi_route_graph
    G = shared_taxi_route_graph(layout)
    if not getattr(G, "coord", None) or not thresholds_xyz:
        return {}

    def _cap(u, v):
        return G.edge_cap.get(G._ekey(u, v), TAXI_MAX_GRADE)

    def _capdist_from(src):
        dist = {src: 0.0}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, _INF):
                continue
            for v, w in G.adj.get(u, ()):
                nd = d + _cap(u, v) * w
                if nd < dist.get(v, _INF):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    # one cap-weighted Dijkstra per threshold (amortised over all buildings)
    thr: List[Tuple[float, float, dict]] = []
    for (tx, ty, te) in thresholds_xyz:
        k, gap = G.nearest_key(tx, ty)
        if k is not None:
            thr.append((te, gap, _capdist_from(k)))
    if not thr:
        return {}

    cls = [ln for (ln, n) in (getattr(layout, "apt_taxi_centerlines", None)
                              or [])
           if ln is not None and not ln.is_empty
           and not str(n or "").upper().startswith("SVC")]
    if not cls:
        return {}

    polys = [s.polygon for s in layout.shapes
             if s.role in _AIRSIDE_ROLES and s.polygon is not None
             and not s.polygon.is_empty]
    if not polys:
        return {}
    airside = unary_union(polys)

    out: Dict[int, float] = {}
    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty):
            continue
        if airside.is_empty or s.polygon.distance(airside) > _TOUCH_TOL_M:
            continue                            # not airside-served → DEM
        c = s.polygon.centroid
        # nearest taxi centerline (names irrelevant) + perpendicular foot
        ln = min(cls, key=lambda L: L.distance(c))
        try:
            P = nearest_points(ln, c)[0]
        except Exception:
            continue
        perp = c.distance(ln)
        # the foot's bracketing centerline vertices = its graph edge
        coords = list(ln.coords)
        sp = ln.project(P)
        acc = 0.0
        A = B = None
        for i in range(len(coords) - 1):
            seg_len = LineString([coords[i], coords[i + 1]]).length
            if acc - 1e-6 <= sp <= acc + seg_len + 1e-6:
                A = (coords[i], sp - acc)
                B = (coords[i + 1], (acc + seg_len) - sp)
                break
            acc += seg_len
        if A is None:
            A = (coords[0], 0.0)
            B = (coords[-1], ln.length)
        kA, _ = G.nearest_key(*A[0])
        kB, _ = G.nearest_key(*B[0])
        ecap = G.edge_cap.get(G._ekey(kA, kB), TAXI_MAX_GRADE)
        # perpendicular climb: taxiway-corridor part at the taxiway cap,
        # the rest (apron) at 1 %.
        perp_climb = (ecap * min(perp, _TAXI_HALF_W_M)
                      + _APRON_CAP * max(0.0, perp - _TAXI_HALF_W_M))

        floor, ceil = -_INF, _INF
        for (te, gap, cdm) in thr:
            cands = []
            if kA in cdm:
                cands.append(cdm[kA] + ecap * A[1])   # + partial first edge
            if kB in cdm:
                cands.append(cdm[kB] + ecap * B[1])
            if not cands:
                continue                          # threshold can't reach it
            budget = _ENTRY_CAP * gap + min(cands) + perp_climb
            ceil = min(ceil, te + budget)
            floor = max(floor, te - budget)
        if ceil >= _INF:
            continue
        de = dem_sampler(c.x, c.y)
        if de is None:
            continue
        out[id(s)] = min(max(de, floor), ceil)
    return out
