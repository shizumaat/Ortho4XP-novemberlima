"""The graph-first taxi-route PROFILE (user 2026-06-25).

The taxi-route graph already sets the building elevations and the spine-climb
floors.  This solves ONE smooth elevation for every graph node — the actual
route profile, not a band — so that geometry (rect ends, caps, junction spine)
can READ its elevation from the profile and line up BY CONSTRUCTION instead of
each shape solving its own nodes and pulling on its neighbours.

The solve is a small, stable 1-D projected Gauss-Seidel on the route graph:

  * every node is bounded by the reach band ``[floor, ceil]`` (runway-route
    reachability — this pins runway CONTACTS, where ``floor == ceil ==`` the
    runway elevation, with no separate anchor needed);
  * a building's serving foot raises a floor (``seat − apron-climb``), propagated
    along the graph edges as a cap-Lipschitz ramp so the route CLIMBS to serve;
  * the free target is closest-to-DEM (the route tracks terrain where it may);
  * each node is clamped ≤cap to its graph neighbours and smoothed (min
    curvature) → a continuous grade-legal profile.

``solve_graph_profile`` returns ``(node_elev, sample)`` where ``node_elev`` maps a
graph key to its solved elevation and ``sample(x, y)`` interpolates the profile
along the nearest graph edge (the value geometry reads).
"""
from __future__ import annotations

import heapq
import math

_INF = float("inf")


def solve_graph_profile(layout, runway_pts, dem_fn, building_seats,
                        bucket_to_idx, *, max_sweeps=2000, tol=1e-3,
                        curvature=0.25):
    """Solve one elevation per taxi-route graph node.  See module docstring."""
    from auto_patch.config import APRON_MAX_GRADE, TAXI_MAX_GRADE, \
        VISIBLE_CHORD_CONNECT
    from auto_patch.layout import ROLE_BUILDING
    from auto_patch.taxi_routing import shared_taxi_route_graph
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_sampler, _nearest_visible_centerline, _pavement_visibility)
    from shapely.geometry import Point

    G = shared_taxi_route_graph(layout)
    if not getattr(G, "coord", None):
        return {}, (lambda x, y: None)
    band = reach_band_sampler(layout, runway_pts)

    keys = [k for k in G.coord if k not in G.aug]      # plain taxi nodes only
    if not keys:
        return {}, (lambda x, y: None)
    kset = set(keys)

    def cap(a, b):
        return G.edge_cap.get(G._ekey(a, b), TAXI_MAX_GRADE)

    # Per-node reach-band bounds + DEM target.
    floor: dict = {}
    ceil: dict = {}
    dem: dict = {}
    for k in keys:
        cx, cy = G.coord[k]
        b = band(cx, cy)
        if b is not None:
            lo, hi = b
            if lo > hi:
                lo = hi = 0.5 * (lo + hi)
            floor[k], ceil[k] = lo, hi
        dem[k] = dem_fn(cx, cy)

    # BUILDING-SERVING floors at the foot graph node, propagated along the graph
    # as a cap-Lipschitz ramp (so the route climbs smoothly to serve the pad).
    clines = [ln for (ln, n) in (getattr(layout, "apt_taxi_centerlines", None)
                                 or [])
              if ln is not None and not ln.is_empty
              and not str(n or "").upper().startswith("SVC")]
    vis = _pavement_visibility(layout) if (VISIBLE_CHORD_CONNECT and clines) else None
    cps = layout.canonical_points
    src: dict = {}
    if clines:
        for s in layout.shapes:
            if (s.role != ROLE_BUILDING or s.polygon is None
                    or s.polygon.is_empty):
                continue
            lv = None
            r = list(s.polygon.exterior.coords)
            r = r[:-1] if len(r) > 1 and r[0] == r[-1] else r
            for (x, y) in r:
                i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                if i in building_seats:
                    lv = building_seats[i]
                    break
            if lv is None:
                continue
            c = s.polygon.centroid
            ln = (_nearest_visible_centerline(c, clines, vis) if vis is not None
                  else min(clines, key=lambda L: L.distance(c)))
            foot = ln.interpolate(ln.project(c))
            fk, _ = G.nearest_key(foot.x, foot.y)
            if fk is None or fk not in kset:
                continue
            perp = c.distance(ln)
            t = lv - APRON_MAX_GRADE * perp           # 1 % apron climb spine→pad
            if t > src.get(fk, -_INF):
                src[fk] = t
    bfloor: dict = {}
    if src:
        pq = [(-t, k) for k, t in src.items()]
        heapq.heapify(pq)
        while pq:
            negt, k = heapq.heappop(pq)
            t = -negt
            if t <= bfloor.get(k, -_INF):
                continue
            bfloor[k] = t
            for (j, w) in G.adj.get(k, ()):
                if j not in kset:
                    continue
                nt = t - cap(k, j) * w
                if nt > bfloor.get(j, -_INF):
                    heapq.heappush(pq, (-nt, j))
        for k, f in bfloor.items():                   # clamp to reachable ceil
            hi = ceil.get(k, _INF)
            floor[k] = max(floor.get(k, -_INF), min(f, hi))

    def _target(k):
        lo, hi = floor.get(k, -_INF), ceil.get(k, _INF)
        d = dem.get(k)
        if lo > hi:
            return 0.5 * (lo + hi)
        if d is None:
            return 0.5 * (lo + hi) if lo > -_INF and hi < _INF else 0.0
        return min(max(d, lo), hi)

    elev = {k: _target(k) for k in keys}

    # Projected Gauss-Seidel: min-curvature target, clamped ≤cap to graph
    # neighbours and into [floor, ceil].
    for _ in range(max_sweeps):
        moved = 0.0
        for k in keys:
            nbrs = [(j, cap(k, j) * w) for (j, w) in G.adj.get(k, ())
                    if j in kset and w > 1e-9]
            if not nbrs:
                continue
            sw = acc = 0.0
            for (j, budget) in nbrs:
                wt = 1.0 / max(budget, 1e-3) ** 2
                sw += wt
                acc += elev[j] * wt
            harm = acc / sw if sw > 0 else elev[k]
            pm = sum(elev[j] for (j, _b) in nbrs) / len(nbrs)
            tgt = (1.0 - curvature) * harm + curvature * pm
            # blend toward closest-DEM so the route tracks terrain where free
            tgt = 0.5 * tgt + 0.5 * _target(k)
            n_lo, n_hi = -_INF, _INF
            for (j, budget) in nbrs:
                ej = elev[j]
                if ej - budget > n_lo:
                    n_lo = ej - budget
                if ej + budget < n_hi:
                    n_hi = ej + budget
            lo_e = max(n_lo, floor.get(k, -_INF))
            hi_e = min(n_hi, ceil.get(k, _INF))
            if lo_e <= hi_e:
                tgt = min(max(tgt, lo_e), hi_e)
            else:
                tgt = 0.5 * (lo_e + hi_e)
            d = tgt - elev[k]
            if d:
                elev[k] = tgt
                if abs(d) > moved:
                    moved = abs(d)
        if moved < tol:
            break

    # Build an edge index for the sampler (nearest-edge projection).
    edges = []
    seen_e = set()
    for k in keys:
        for (j, _w) in G.adj.get(k, ()):
            if j not in kset:
                continue
            e = G._ekey(k, j)
            if e in seen_e:
                continue
            seen_e.add(e)
            edges.append((k, j))

    def sample(x, y):
        """Profile elevation at (x, y): project onto the nearest graph edge and
        linearly interpolate its two endpoints (continuous along centerlines)."""
        best_d = _INF
        best_z = None
        for (a, b) in edges:
            ax, ay = G.coord[a]
            bx, by = G.coord[b]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            if L2 < 1e-12:
                t = 0.0
            else:
                t = ((x - ax) * dx + (y - ay) * dy) / L2
                t = max(0.0, min(1.0, t))
            px, py = ax + t * dx, ay + t * dy
            dd = (x - px) ** 2 + (y - py) ** 2
            if dd < best_d:
                best_d = dd
                best_z = elev[a] + t * (elev[b] - elev[a])
        return best_z

    return elev, sample
