#!/usr/bin/env python
"""Trace the BINDING reach route to a pavement shape and emit it as KML.

This is the reusable companion to ``building_feasibility.reach_band_sampler`` —
it answers "which runway, via which taxiways, binds this shape's reachable
ceiling, and what does that route look like on the map?".  It uses the SAME cost
model the band uses (the shared taxi-route graph ``G`` with PER-EDGE caps
``G.edge_cap``, anchored at the ``_runway_route_contacts``), so its numbers match
the band exactly — it only adds path reconstruction (predecessors), which the
sampler doesn't expose.

Use this instead of re-deriving reach distances in a throwaway script (a uniform
cap is the classic mistake — taxiway G is 3%, not 1.5%).

Usage:
    venv/bin/python tools/trace_reach_route.py CYXY --coord -262,-381
    venv/bin/python tools/trace_reach_route.py CYXY --ref building14
    # writes <out> (default /tmp/reach_route.kml) and prints the binding contact,
    # the per-cap segment lengths, and the resulting ceiling/floor.
"""
from __future__ import annotations

import argparse
import heapq
import math
import os
import sys

sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "src"),
                os.path.join(os.path.dirname(__file__), ".."),
                os.path.join(os.path.dirname(__file__), "..", "tests")]


def _binding_route(layout, x, y):
    """Return ``(ceiling, floor, contact_xy, runway_ref, path_xy, cap_lengths)``
    for the point ``(x, y)`` — the runway contact that BINDS its ceiling and the
    cap-weighted shortest route to it (same cost model as ``reach_band_sampler``).
    """
    from shapely.geometry import Point
    from auto_patch.taxi_routing import shared_taxi_route_graph
    from auto_patch.config import TAXI_MAX_GRADE, VISIBLE_CHORD_CONNECT
    from auto_patch.elevation_per_surface.building_feasibility import (
        _runway_route_contacts, _nearest_visible_centerline, _pavement_visibility)
    from auto_patch.elevation_per_surface.route_profile.anchors import (
        reach_band_for)
    from auto_patch.elevation_per_surface.unified_jacobi import (
        _build_node_list, _seed_elevations)
    from auto_patch.layout import ROLE_RUNWAY

    nodes, b2i = _build_node_list(layout)
    elev, _bh, _ = _seed_elevations(layout, nodes, b2i, dem=None,
                                    tile_lat=0, tile_lon=0)
    _b, _d, rwpts = reach_band_for(layout, elev, b2i, None, 0, 0)
    G = shared_taxi_route_graph(layout)

    def cap(u, v):
        return G.edge_cap.get(G._ekey(u, v), TAXI_MAX_GRADE)

    def dijkstra(src):
        dist = {src: 0.0}
        prev: dict = {}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, 1e18):
                continue
            for v, w in G.adj.get(u, ()):
                nd = d + cap(u, v) * w
                if nd < dist.get(v, 1e18):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(pq, (nd, v))
        return dist, prev

    cls = [ln for (ln, n) in (getattr(layout, "apt_taxi_centerlines", None) or [])
           if ln is not None and not ln.is_empty
           and not str(n or "").upper().startswith("SVC")]
    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None
    P = Point(x, y)
    ln = (_nearest_visible_centerline(P, cls, vis) if vis is not None
          else min(cls, key=lambda L: L.distance(P)))
    foot = ln.interpolate(ln.project(P))
    kfoot, _ = G.nearest_key(foot.x, foot.y)

    best = None
    for (k, ae) in _runway_route_contacts(layout, G, rwpts):
        dist, prev = dijkstra(k)
        if kfoot in dist:
            ceil = ae + dist[kfoot]
            if best is None or ceil < best[0]:
                best = (ceil, ae - dist[kfoot], k, ae, dist, prev)
    if best is None:
        return None
    ceil, floor, k, ae, dist, prev = best
    path = [kfoot]
    u = kfoot
    while u != k and u in prev:
        u = prev[u]
        path.append(u)
    path.reverse()
    cap_len: dict = {}
    for a, b in zip(path, path[1:]):
        c = round(cap(a, b) * 100, 1)
        seg = math.hypot(G.coord[a][0] - G.coord[b][0],
                         G.coord[a][1] - G.coord[b][1])
        cap_len[c] = cap_len.get(c, 0.0) + seg
    rwy_ref = "?"
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY and s.polygon is not None
                and not s.polygon.is_empty
                and s.polygon.distance(Point(*G.coord[k])) < 15):
            rwy_ref = s.ref
            break
    return (ceil, floor, G.coord[k], ae, rwy_ref,
            [G.coord[n] for n in path], (foot.x, foot.y), cap_len)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("icao")
    ap.add_argument("--ref", help="shape ref (e.g. building14)")
    ap.add_argument("--coord", help="local meters 'x,y'")
    ap.add_argument("--out", default="/tmp/reach_route.kml")
    args = ap.parse_args()

    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement
    layout = build_airport_pavement(args.icao, xplane_root(),
                                    compute_elevations=True)

    if args.coord:
        x, y = (float(v) for v in args.coord.split(","))
    elif args.ref:
        s = next((s for s in layout.shapes if s.ref == args.ref), None)
        if s is None or s.polygon is None:
            sys.exit(f"ref {args.ref} not found / no polygon")
        x, y = s.polygon.centroid.x, s.polygon.centroid.y
    else:
        sys.exit("give --ref or --coord")

    r = _binding_route(layout, x, y)
    if r is None:
        sys.exit("point is not taxi-reachable from any runway contact")
    ceil, floor, cxy, ae, rwy_ref, path, foot, cap_len = r
    print(f"binding runway: {rwy_ref}  contact=({cxy[0]:.0f},{cxy[1]:.0f}) "
          f"elev={ae:.1f}")
    print(f"route per-cap length (m): "
          f"{{{', '.join(f'{k}%: {v:.0f}' for k, v in sorted(cap_len.items()))}}}")
    print(f"reach band at ({x:.0f},{y:.0f}): floor={floor:.1f} ceiling={ceil:.1f}")

    lat0, lon0 = layout.anchor
    R = 6378137.0
    cos0 = math.cos(math.radians(lat0))

    def ll(px, py):
        return (lon0 + math.degrees(px / (R * cos0)),
                lat0 + math.degrees(py / R))

    line = " ".join(f"{ll(*p)[0]:.7f},{ll(*p)[1]:.7f},0" for p in path)

    def pm(name, px, py):
        lo, la = ll(px, py)
        return (f'<Placemark><name>{name}</name><Point><coordinates>'
                f'{lo:.7f},{la:.7f},0</coordinates></Point></Placemark>')

    kml = (
        '<?xml version="1.0"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'
        '<Style id="r"><LineStyle><color>ff00ffff</color><width>4</width>'
        '</LineStyle></Style>\n'
        f'<Placemark><name>reach route (ceil {ceil:.1f})</name>'
        f'<styleUrl>#r</styleUrl><LineString><coordinates>{line}'
        '</coordinates></LineString></Placemark>\n'
        f'{pm(f"target {args.ref or args.coord}", *foot)}\n'
        f'{pm(f"binding {rwy_ref} {ae:.1f}", cxy[0], cxy[1])}\n'
        '</Document></kml>\n')
    with open(args.out, "w") as f:
        f.write(kml)
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
