#!/usr/bin/env python3
"""Medial-axis centerline classifier (codes-free) — prototype.

Classifies every row-120 painted line as CENTERLINE / EDGE / SHORT using PAVEMENT
GEOMETRY only, not paint codes:

  * a CENTERLINE runs down the middle of a taxi corridor — its distance to the
    nearest pavement EDGE is ≈ the local half-width (metres of clearance);
  * an EDGE line hugs a boundary — distance-to-edge ≈ 0;
  * SHORT lines (hold bars / fragments) are flagged separately.

The pavement corridor is the union of the built airside/apron/runway shapes.
Dumps KML: centerlines GREEN, edge lines RED, short GREY — plus agreement stats
against the (unreliable) paint-code test.

Usage:  venv/bin/python tools/classify_centerlines_kml.py SPJC [--global] \
            --out /Users/noah/Ortho4XP-troubleshoot/SPJC_centerlines.kml
"""
from __future__ import annotations

import argparse
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests")]

_R = 6378137.0
# Per-side perpendicular probe: a CENTERLINE has pavement clearance on BOTH sides
# (≈ half-width); an EDGE line hits a boundary immediately on ONE side.
_CENTER_CLEAR_M = 2.5     # both-side clearance ≥ this ⇒ a centered (interior) point
_EDGE_CLEAR_M = 1.5       # one side ≤ this ⇒ a boundary-hugging (edge) point
_MARCH_MAX_M = 24.0
_MARCH_STEP_M = 1.5
_SHORT_M = 8.0            # shorter than this ⇒ fragment (set aside)
_ONPAV_FRAC = 0.6         # must be at least this fraction on pavement
_SAMPLE_M = 8.0           # sample spacing along each line
_MAX_SAMPLES = 10

# pavement roles that form the taxi corridor (exclude boundary/clearance/feature)
_PAV_ROLES = {"primary_parallel", "secondary_parallel", "stub", "cross_connector",
              "junction", "service_junction", "apron", "terminal", "runway",
              "runway_shoulder", "service_road"}


def _to_lonlat(x, y, lat0, lon0):
    return (lon0 + math.degrees(x / (_R * math.cos(math.radians(lat0)))),
            lat0 + math.degrees(y / _R))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--out", default=None)
    ap.add_argument("--global", dest="use_global", action="store_true")
    args = ap.parse_args()

    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement
    from shapely.geometry import Point
    from shapely.ops import unary_union
    from shapely.prepared import prep

    root = xplane_root()
    if args.use_global:
        g = os.path.join(root, "Global Scenery", "Global Airports",
                         "Earth nav data", "apt.dat")
        if os.path.isfile(g):
            os.environ["O4_FORCE_APT_DAT"] = g

    lay = build_airport_pavement(args.icao, root, compute_elevations=True)
    lat0, lon0 = lay.anchor

    pav = unary_union([s.polygon for s in lay.shapes
                       if getattr(s, "role", "") in _PAV_ROLES
                       and s.polygon is not None and not s.polygon.is_empty])
    pav_prep = prep(pav)
    painted = getattr(lay, "_painted_lines_m", None) or []

    def _march(px, py, nx, ny):
        """Distance pavement extends from (px,py) in direction (nx,ny), capped."""
        s = _MARCH_STEP_M
        while s <= _MARCH_MAX_M:
            if not pav_prep.contains(Point(px + s*nx, py + s*ny)):
                return s
            s += _MARCH_STEP_M
        return _MARCH_MAX_M

    marks = []
    n_cl = n_edge = n_amb = n_short = n_off = 0
    for i, ln in enumerate(painted):
        if ln is None or ln.is_empty or ln.length < 2.0:
            continue
        L = ln.length
        coords_m = list(ln.coords)
        ns = max(3, min(_MAX_SAMPLES, int(L / _SAMPLE_M) + 1))
        ts = [k / (ns - 1) for k in range(ns)]
        pts = [ln.interpolate(t, normalized=True) for t in ts]
        onpav = sum(1 for p in pts if pav_prep.contains(p)) / len(pts)
        if onpav < _ONPAV_FRAC:
            n_off += 1
            continue
        if L < _SHORT_M:
            cls = "short"; n_short += 1
        else:
            center = edge = 0
            for k in range(1, ns - 1):
                p = pts[k]
                if not pav_prep.contains(p):
                    continue
                a = ln.interpolate(ts[k-1], normalized=True)
                b = ln.interpolate(ts[k+1], normalized=True)
                tx, ty = b.x - a.x, b.y - a.y
                tl = math.hypot(tx, ty) or 1.0
                nx, ny = -ty/tl, tx/tl                    # unit perpendicular
                left = _march(p.x, p.y, nx, ny)
                right = _march(p.x, p.y, -nx, -ny)
                lo = min(left, right)
                if lo <= _EDGE_CLEAR_M:
                    edge += 1
                elif lo >= _CENTER_CLEAR_M:
                    center += 1
            tot = max(1, center + edge)
            if edge / tot > 0.4:
                cls = "edge"; n_edge += 1
            elif center / tot > 0.5:
                cls = "centerline"; n_cl += 1
            else:
                cls = "ambiguous"; n_amb += 1
        coords = [_to_lonlat(x, y, lat0, lon0) for x, y in coords_m]
        cs = " ".join(f"{lo:.8f},{la:.8f},0" for lo, la in coords)
        marks.append(f'<Placemark><name>{cls}{i}</name><styleUrl>#{cls}</styleUrl>'
                     f'<LineString><tessellate>1</tessellate>'
                     f'<coordinates>{cs}</coordinates></LineString></Placemark>')

    styles = (
        '<Style id="centerline"><LineStyle><color>ff00ff00</color><width>3</width></LineStyle></Style>'
        '<Style id="edge"><LineStyle><color>ff0000ff</color><width>2</width></LineStyle></Style>'
        '<Style id="ambiguous"><LineStyle><color>ff00ffff</color><width>2</width></LineStyle></Style>'
        '<Style id="short"><LineStyle><color>ff888888</color><width>1</width></LineStyle></Style>')
    doc = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           f'<name>{args.icao} painted-line classification</name>'
           + styles + "".join(marks) + "</Document></kml>")
    out = args.out or os.path.join(ROOT, f"{args.icao}_centerlines.kml")
    with open(out, "w") as f:
        f.write(doc)
    print(f"WROTE {out}")
    print(f"  painted lines: {len(painted)}")
    print(f"  CENTERLINE={n_cl}  EDGE={n_edge}  AMBIGUOUS={n_amb}  "
          f"SHORT={n_short}  off-pavement={n_off}")


if __name__ == "__main__":
    main()
