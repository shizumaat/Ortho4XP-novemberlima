#!/usr/bin/env python
"""Trace the BINDING reach route to a pavement point and emit it as KML.

The companion to ``building_feasibility.reach_band_unified`` (THE one unified
grade graph G, post route_field retirement): it answers "which runway, via which
spine, binds this point's reachable ceiling/floor, and what does that route look
like on the map?".  It replays the EXACT band computation — nearest visible
centerline, perpendicular foot, the two spine nodes (kA/kB) bracketing the foot,
the per-edge cap, the perp climb — over ``G.spine_adj`` from ``G.runway_anchor``,
and adds the predecessor path the band sampler doesn't expose.  Its ceiling/floor
match ``reach_band_unified`` exactly.

Use this instead of a throwaway script (the stale uniform-cap mistake, or the old
``shared_taxi_route_graph`` route graph that no longer drives the band).

It also prints whether the perpendicular chord to the binding centerline stays on
pavement (a real apron path vs a phantom connection across grass) and the band via
the SECOND-nearest visible centerline (is a higher route available the band's
nearest-only rule skipped?), and draws the apron/building/centerline context.

Usage:
    venv/bin/python tools/trace_reach_route.py CYXY --coord -334,-30
    venv/bin/python tools/trace_reach_route.py CYXY --ref building5
    # writes <out> (default /tmp/reach_route.kml) and prints the binding contact,
    # the per-cap segment lengths, the on-pavement check, and ceiling/floor.
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

_INF = float("inf")


def _capdist_prev(spine_adj, src):
    """Cap-Dijkstra over ``spine_adj`` from ``src`` with predecessors."""
    dist = {src: 0.0}
    prev: dict = {}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, _INF):
            continue
        for (v, budget) in spine_adj.get(u, ()):
            nd = d + budget
            if nd < dist.get(v, _INF):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    return dist, prev


def _foot_bracket(ln, c):
    """Replay the band's foot geometry: project ``c`` onto ``ln`` and return the
    two segment endpoints A,B bracketing the foot as ``(coord, along_dist)``."""
    coords = list(ln.coords)
    sp = ln.project(c)
    acc = 0.0
    for i in range(len(coords) - 1):
        seg = math.hypot(coords[i + 1][0] - coords[i][0],
                         coords[i + 1][1] - coords[i][1])
        if acc - 1e-6 <= sp <= acc + seg + 1e-6:
            return (coords[i], sp - acc), (coords[i + 1], (acc + seg) - sp)
        acc += seg
    return (coords[0], 0.0), (coords[-1], ln.length)


def _binding_route(layout, x, y):
    """``(ceil, floor, contact_xy, ae, rwy_ref, path_xy, foot_xy, cap_len,
    serving_name, perp, on_pav, second)`` for ``(x, y)`` on the UNIFIED graph —
    the runway anchor that BINDS the ceiling and the cap-route to it."""
    from shapely.geometry import Point, LineString
    from shapely.strtree import STRtree
    from auto_patch import grade_graph as GG
    from auto_patch.config import TAXI_MAX_GRADE, VISIBLE_CHORD_CONNECT
    from auto_patch.elevation_per_surface.solver_primitives import _build_node_list
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_unified, _pavement_visibility, _nearest_visible_centerline,
        _TAXI_HALF_W_M)
    from auto_patch.grade_law import APRON_MAX_GRADE as _APRON_CAP
    from auto_patch.layout import ROLE_RUNWAY

    nodes, b2i = _build_node_list(layout)
    G = GG.build_unified_graph(layout, b2i)
    # sanity: the band we replay IS reach_band_unified on this G.
    _ = reach_band_unified(layout, G)
    if not getattr(G, "runway_anchor", None) or not getattr(G, "spine_adj", None):
        return None

    anchors = {k: (float(ae), *_capdist_prev(G.spine_adj, k))
               for (k, ae) in G.runway_anchor.items()}
    sidx = [i for i in G.spine_adj if i in G.pos]
    if not sidx:
        return None
    tree = STRtree([Point(*G.pos[i]) for i in sidx])

    def nn(pt):
        return sidx[int(tree.nearest(Point(pt[0], pt[1])))]

    def ecap(a, b):
        for (j, budget) in G.spine_adj.get(a, ()):
            if j == b:
                d = math.hypot(G.pos[a][0] - G.pos[b][0],
                               G.pos[a][1] - G.pos[b][1])
                return budget / d if d > 1e-9 else TAXI_MAX_GRADE
        return TAXI_MAX_GRADE

    cls_named = [(tcl.line, str(tcl.name or "?"))
                 for tcl in (getattr(layout, "apt_taxi_centerlines", None)
                             or [])
                 if tcl.line is not None and not tcl.line.is_empty
                 and not tcl.is_service
                 and not str(tcl.name or "").upper().startswith("SVC")]
    cls = [ln for (ln, _n) in cls_named]
    if not cls:
        return None
    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None
    c = Point(x, y)

    def _ceil_via(ln):
        """Replay the band ceiling/floor + path for a GIVEN centerline."""
        perp = c.distance(ln)
        A, B = _foot_bracket(ln, c)
        kA, kB = nn(A[0]), nn(B[0])
        ec = ecap(kA, kB)
        perp_climb = (ec * min(perp, _TAXI_HALF_W_M)
                      + _APRON_CAP * max(0.0, perp - _TAXI_HALF_W_M))
        best = None
        for (k, (ae, cdm, prev)) in anchors.items():
            cands = []
            if kA in cdm:
                cands.append((cdm[kA] + ec * A[1], kA))
            if kB in cdm:
                cands.append((cdm[kB] + ec * B[1], kB))
            if not cands:
                continue
            bud, kbind = min(cands)
            ceil = ae + bud + perp_climb
            if best is None or ceil < best[0]:
                best = (ceil, ae - bud - perp_climb, k, ae, kbind, prev, perp)
        return best

    # binding (nearest visible) centerline — exactly as the band picks it.
    serving = (_nearest_visible_centerline(c, cls, vis) if vis is not None
               else min(cls, key=lambda L: L.distance(c)))
    serving_name = next((n for (ln, n) in cls_named if ln is serving), "?")
    b0 = _ceil_via(serving)
    if b0 is None:
        return None
    ceil, floor, k, ae, kbind, prev, perp = b0

    # on-pavement check of the perp chord (phantom-across-grass detector)
    foot = serving.interpolate(serving.project(c))
    chord = LineString([(x, y), (foot.x, foot.y)])
    on_pav = None
    if vis is not None and chord.length > 1e-6:
        try:
            on_pav = chord.intersection(vis.context).length / chord.length
        except Exception:
            on_pav = None

    # second-nearest visible centerline — would a higher route serve this point?
    second = None
    for ln in sorted(cls, key=lambda L: L.distance(c)):
        if ln is serving:
            continue
        b2 = _ceil_via(ln)
        if b2 is not None:
            nm = next((n for (lk, n) in cls_named if lk is ln), "?")
            second = (nm, b2[0], ln.distance(c))
            break

    # reconstruct path kbind -> k (runway anchor)
    path = [kbind]
    u = kbind
    while u != k and u in prev:
        u = prev[u]
        path.append(u)
    path.reverse()
    cap_len: dict = {}
    for a, b in zip(path, path[1:]):
        cc = round(ecap(a, b) * 100, 1)
        seg = math.hypot(G.pos[a][0] - G.pos[b][0], G.pos[a][1] - G.pos[b][1])
        cap_len[cc] = cap_len.get(cc, 0.0) + seg

    rwy_ref = "?"
    for s in layout.shapes:
        if (s.role == ROLE_RUNWAY and s.polygon is not None
                and not s.polygon.is_empty
                and s.polygon.distance(Point(*G.pos[k])) < 15):
            rwy_ref = str(s.ref)
            break
    return (ceil, floor, G.pos[k], ae, rwy_ref, [G.pos[n] for n in path],
            (foot.x, foot.y), cap_len, serving_name, perp, on_pav, second)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("icao")
    ap.add_argument("--ref", help="shape ref (e.g. building5)")
    ap.add_argument("--coord", help="local meters 'x,y'")
    ap.add_argument("--out", default="/tmp/reach_route.kml")
    args = ap.parse_args()

    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement
    from auto_patch.grade_graph import _open_ring
    from auto_patch.layout import ROLE_APRON, ROLE_BUILDING
    layout = build_airport_pavement(args.icao, xplane_root(),
                                    compute_elevations=True)

    if args.coord:
        x, y = (float(v) for v in args.coord.split(","))
    elif args.ref:
        s = next((s for s in layout.shapes if str(s.ref) == args.ref), None)
        if s is None or s.polygon is None:
            sys.exit(f"ref {args.ref} not found / no polygon")
        x, y = s.polygon.centroid.x, s.polygon.centroid.y
    else:
        sys.exit("give --ref or --coord")

    r = _binding_route(layout, x, y)
    if r is None:
        sys.exit("point is not taxi-reachable from any runway contact")
    (ceil, floor, cxy, ae, rwy_ref, path, foot, cap_len, serving_name,
     perp, on_pav, second) = r
    print(f"target ({x:.0f},{y:.0f}) — serving centerline {serving_name} "
          f"perp={perp:.1f}m")
    print(f"binding runway: {rwy_ref}  contact=({cxy[0]:.0f},{cxy[1]:.0f}) "
          f"elev={ae:.1f}")
    print(f"route per-cap length (m): "
          f"{{{', '.join(f'{k}%: {v:.0f}' for k, v in sorted(cap_len.items()))}}}")
    print(f"reach band: floor={floor:.1f} ceiling={ceil:.1f}")
    if on_pav is not None:
        print(f"perp chord on-pavement fraction: {on_pav*100:.0f}%"
              f"  ({'REAL apron path' if on_pav >= 0.97 else 'PHANTOM — crosses grass'})")
    if second is not None:
        nm, c2, d2 = second
        print(f"2nd-nearest visible centerline {nm} ({d2:.0f}m): ceiling={c2:.1f}"
              f"  ({'HIGHER route available (band uses nearest-only)' if c2 > ceil + 0.3 else 'not higher'})")

    lat0, lon0 = layout.anchor
    R = 6378137.0
    cos0 = math.cos(math.radians(lat0))

    def ll(px, py):
        return (lon0 + math.degrees(px / (R * cos0)),
                lat0 + math.degrees(py / R))

    def line(pts):
        return " ".join(f"{ll(*p)[0]:.7f},{ll(*p)[1]:.7f},0" for p in pts)

    def pm(name, px, py):
        lo, la = ll(px, py)
        return (f'<Placemark><name>{name}</name><Point><coordinates>'
                f'{lo:.7f},{la:.7f},0</coordinates></Point></Placemark>')

    parts = [
        '<?xml version="1.0"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        '<Style id="r"><LineStyle><color>ff00ffff</color><width>5</width></LineStyle></Style>',
        '<Style id="cl"><LineStyle><color>ffffffff</color><width>2</width></LineStyle></Style>',
        '<Style id="ap"><LineStyle><color>ffff8800</color><width>2</width></LineStyle>'
        '<PolyStyle><color>20ff8800</color></PolyStyle></Style>',
        '<Style id="bl"><LineStyle><color>ff0000ff</color><width>2</width></LineStyle>'
        '<PolyStyle><color>300000ff</color></PolyStyle></Style>',
        f'<Placemark><name>reach route {rwy_ref} {ae:.1f} -> ceil {ceil:.1f}</name>'
        f'<styleUrl>#r</styleUrl><LineString><coordinates>{line(path)}'
        '</coordinates></LineString></Placemark>',
        pm(f"target {args.ref or args.coord} (e?/ceil {ceil:.1f})", x, y),
        pm(f"foot {serving_name} perp {perp:.0f}m", *foot),
        pm(f"binding {rwy_ref} {ae:.1f}", cxy[0], cxy[1]),
    ]
    # apron + building context within 120 m, and nearby centerlines
    from shapely.geometry import Point as _P
    near = _P(x, y)
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty or s.polygon.distance(near) > 120:
            continue
        if s.role == ROLE_APRON:
            ring = _open_ring(list(s.polygon.exterior.coords))
            parts.append(f'<Placemark><name>apron {s.polygon.area:.0f}m2</name>'
                         f'<styleUrl>#ap</styleUrl><Polygon><outerBoundaryIs>'
                         f'<LinearRing><coordinates>{line(ring+[ring[0]])}'
                         '</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>')
        elif s.role == ROLE_BUILDING:
            ring = _open_ring(list(s.polygon.exterior.coords))
            parts.append(f'<Placemark><name>{s.ref}</name><styleUrl>#bl</styleUrl>'
                         f'<Polygon><outerBoundaryIs><LinearRing><coordinates>'
                         f'{line(ring+[ring[0]])}'
                         '</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>')
    for entry in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln, nm = entry.line, str(entry.name or "?")
        if (ln is None or ln.is_empty or ln.distance(near) > 120
                or entry.is_service or nm.upper().startswith("SVC")):
            continue
        parts.append(f'<Placemark><name>{nm}</name><styleUrl>#cl</styleUrl>'
                     f'<LineString><coordinates>{line(list(ln.coords))}'
                     '</coordinates></LineString></Placemark>')
    parts.append('</Document></kml>')
    with open(args.out, "w") as f:
        f.write("\n".join(parts) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
