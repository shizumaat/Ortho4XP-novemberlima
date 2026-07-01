#!/usr/bin/env python3
"""Route-anchored centerline recognizer (prototype).

The keystone the medial-axis test couldn't provide at a dense airport: a real
taxiway CENTERLINE rides a taxi ROUTE (apt.dat 1201/1202), an EDGE line is offset
a half-width off it, a HOLD BAR crosses it perpendicular.  So classify each row-120
painted line by whether a substantial run of it rides a route within tolerance AND
tangent-aligned.

Dumps KML:
  * GREEN  — painted lines RECOGNIZED as centerlines (ride a route) = the real
             curved centerline network;
  * GREY   — painted lines NOT recognized (edge lines / hold bars / stray paint);
  * RED    — routes with NO riding painted line (coverage gaps → keep straight route).

Usage:  venv/bin/python tools/recognize_centerlines_kml.py SPJC [--global] \
            --out /Users/noah/Ortho4XP-troubleshoot/SPJC_recognized.kml
"""
from __future__ import annotations

import argparse
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests")]

_R = 6378137.0
_RIDE_TOL_M = 7.0        # a centerline rides within this of its route
_ALIGN_DOT = 0.8         # |cos| of painted-vs-route tangent to count as aligned
_MIN_RIDE_M = 8.0        # substantial aligned overlap to be "riding" a route


def _to_lonlat(x, y, lat0, lon0):
    return (lon0 + math.degrees(x / (_R * math.cos(math.radians(lat0)))),
            lat0 + math.degrees(y / _R))


def _unit(dx, dy):
    n = math.hypot(dx, dy)
    return (dx/n, dy/n) if n > 1e-9 else (0.0, 0.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--out", default=None)
    ap.add_argument("--global", dest="use_global", action="store_true")
    args = ap.parse_args()

    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    root = xplane_root()
    if args.use_global:
        g = os.path.join(root, "Global Scenery", "Global Airports",
                         "Earth nav data", "apt.dat")
        if os.path.isfile(g):
            os.environ["O4_FORCE_APT_DAT"] = g

    lay = build_airport_pavement(args.icao, root, compute_elevations=True)
    lat0, lon0 = lay.anchor
    rw = getattr(lay, "runway_union", None)
    cls = getattr(lay, "apt_taxi_centerlines", []) or []
    routes = {}
    for t in cls:
        if getattr(t, "is_service", False) or getattr(t, "name", "") in ("fillet", "fillet_paint"):
            continue
        rl = t.chained_line
        if rl is not None and not rl.is_empty and rl.length > 8.0:
            routes[id(rl)] = rl
    routes = list(routes.values())
    rtree = STRtree(routes)
    painted = getattr(lay, "_painted_lines_m", None) or []

    def _ride_len(P, R):
        """aligned length of P within _RIDE_TOL_M of R."""
        pts = list(P.coords)
        acc = 0.0
        for k in range(len(pts) - 1):
            mx, my = (pts[k][0]+pts[k+1][0])/2, (pts[k][1]+pts[k+1][1])/2
            mp = Point(mx, my)
            if R.distance(mp) >= _RIDE_TOL_M:
                continue
            seg = _unit(pts[k+1][0]-pts[k][0], pts[k+1][1]-pts[k][1])
            s = R.project(mp)
            r1 = R.interpolate(max(0.0, s - 1.0)); r2 = R.interpolate(min(R.length, s + 1.0))
            rdir = _unit(r2.x - r1.x, r2.y - r1.y)
            if abs(seg[0]*rdir[0] + seg[1]*rdir[1]) >= _ALIGN_DOT:
                acc += math.hypot(pts[k+1][0]-pts[k][0], pts[k+1][1]-pts[k][1])
        return acc

    def _emit(geom, style, tag):
        parts = ([geom] if geom.geom_type == "LineString"
                 else [g for g in getattr(geom, "geoms", []) if g.geom_type == "LineString"])
        for g in parts:
            if g.is_empty or g.length < 1.0:
                continue
            coords = [_to_lonlat(x, y, lat0, lon0) for x, y in g.coords]
            cs = " ".join(f"{lo:.8f},{la:.8f},0" for lo, la in coords)
            marks.append(f'<Placemark><name>{tag}</name><styleUrl>#{style}</styleUrl>'
                         f'<LineString><tessellate>1</tessellate>'
                         f'<coordinates>{cs}</coordinates></LineString></Placemark>')

    marks = []
    n_cl = n_rej = 0
    routes_hit = set()
    for i, ln in enumerate(painted):
        if ln is None or ln.is_empty or ln.length < 2.0:
            continue
        recognized = False
        for ridx in rtree.query(ln.buffer(_RIDE_TOL_M)):
            R = routes[int(ridx)]
            if _ride_len(ln, R) >= _MIN_RIDE_M:
                recognized = True
                routes_hit.add(int(ridx))
        if recognized:
            n_cl += 1
            g = ln
            if rw is not None and not rw.is_empty:      # clip out runway interiors
                try:
                    g = ln.difference(rw)
                except Exception:
                    g = ln
            _emit(g, "cl", f"cl{i}")
        else:
            n_rej += 1
            _emit(ln, "rej", f"rej{i}")                 # grey, toggle off in GE

    n_gap = len(routes) - len(routes_hit)
    styles = (
        '<Style id="cl"><LineStyle><color>ff00ff00</color><width>3</width></LineStyle></Style>'
        '<Style id="rej"><LineStyle><color>66888888</color><width>1</width></LineStyle></Style>')
    doc = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           f'<name>{args.icao} recognized centerlines</name>'
           + styles + "".join(marks) + "</Document></kml>")
    out = args.out or os.path.join(ROOT, f"{args.icao}_recognized.kml")
    with open(out, "w") as f:
        f.write(doc)
    print(f"WROTE {out}")
    print(f"  routes: {len(routes)}  ({len(routes_hit)} with a riding centerline, "
          f"{n_gap} gaps)")
    print(f"  painted: {len(painted)}  ->  RECOGNIZED centerline={n_cl}  rejected={n_rej}")


if __name__ == "__main__":
    main()
