"""Focused anatomy of the SPLP junction near (4.1,503.3) (the -10025 2.87%
violation): every vertex (x,y,alt,DEM), which apt centerlines pass through it,
and where each vertex projects onto each centerline (perp dist + arc pos).
Determines whether the worst pair v6->v7 lies ALONG one centerline.

Run: venv/bin/python tools/diag_splp_junction.py
"""
from __future__ import annotations
import os, sys, math

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from conftest import xplane_root  # noqa: E402
from auto_patch.pipeline import build_airport_pavement  # noqa: E402
from auto_patch.layout import ROLE_JUNCTION  # noqa: E402
from auto_patch.elevation import _sample_dem, _load_airport_dem  # noqa: E402
from shapely.geometry import Point  # noqa: E402

ICAO = "SPLP"
TARGET = (4.1, 503.3)

layout = build_airport_pavement(ICAO, xplane_root(), compute_elevations=True)
tlat = int(math.floor(layout.anchor[0]))
tlon = int(math.floor(layout.anchor[1]))
dem = _load_airport_dem(layout.anchor[0], layout.anchor[1])


def shape_alts(s):
    ring = list(s.polygon.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if s.node_altitudes:
        na = list(s.node_altitudes)[:len(ring)]
        return ring, na
    if s.altitude_high is not None and s.altitude_low is not None and len(ring) == 4:
        return ring, [s.altitude_high, s.altitude_low, s.altitude_low, s.altitude_high]
    if s.altitude is not None:
        return ring, [s.altitude] * len(ring)
    return ring, [None] * len(ring)


# Find the target junction (closest centroid).
best = None
for s in layout.shapes:
    if s.role != ROLE_JUNCTION or s.polygon is None or s.polygon.is_empty:
        continue
    c = s.polygon.centroid
    d = math.hypot(c.x - TARGET[0], c.y - TARGET[1])
    if best is None or d < best[0]:
        best = (d, s)

s = best[1]
ring, alts = shape_alts(s)
c = s.polygon.centroid
print(f"Junction centroid=({c.x:.1f},{c.y:.1f}) verts={len(ring)} area={s.polygon.area:.0f}")
print("\nVERTICES (idx: x, y, alt, DEM, alt-DEM):")
for i, (pt, a) in enumerate(zip(ring, alts)):
    lat, lon = layout.m_to_ll(pt[0], pt[1])
    e = _sample_dem(dem, tlat, tlon, lat, lon)
    dd = "" if (e is None or a is None) else f"{a-e:+.2f}"
    es = "None" if e is None else f"{e:.2f}"
    print(f"  v{i:2d}: ({pt[0]:7.1f},{pt[1]:7.1f})  alt={a!s:>6}  DEM={es:>6}  d={dd}")

# Centerlines through this junction.
print("\nCENTERLINES intersecting the junction polygon:")
apt_lines = getattr(layout, "apt_taxi_centerlines", []) or []
PERP = 15.0
through = []
for tcl in apt_lines:
    ln, name = tcl.line, tcl.name
    if ln is None or ln.is_empty:
        continue
    if not s.polygon.intersects(ln):
        continue
    through.append((ln, name))
    print(f"\n  name={name!r} len={ln.length:.1f}m  "
          f"ends=({ln.coords[0][0]:.1f},{ln.coords[0][1]:.1f})->"
          f"({ln.coords[-1][0]:.1f},{ln.coords[-1][1]:.1f})")
    print("    vertices within %.0fm perp (idx: perp, arc):" % PERP)
    for i, pt in enumerate(ring):
        p = Point(pt)
        perp = ln.distance(p)
        if perp <= PERP:
            arc = ln.project(p)
            print(f"      v{i:2d}: perp={perp:5.1f}  arc={arc:6.1f}  alt={alts[i]}")

# Specifically: do v6 and v7 lie along ANY common centerline?
print("\nWORST PAIR v6,v7 along a common centerline?")
for ln, name in through:
    p6, p7 = Point(ring[6]), Point(ring[7])
    d6, d7 = ln.distance(p6), ln.distance(p7)
    if d6 <= PERP and d7 <= PERP:
        a6, a7 = ln.project(p6), ln.project(p7)
        arc = abs(a6 - a7)
        chord = math.hypot(ring[6][0]-ring[7][0], ring[6][1]-ring[7][1])
        print(f"  name={name!r}: v6 perp={d6:.1f} arc={a6:.1f}; "
              f"v7 perp={d7:.1f} arc={a7:.1f}; along-arc={arc:.1f} chord={chord:.1f}")
    else:
        print(f"  name={name!r}: NOT both within perp (d6={d6:.1f}, d7={d7:.1f})")
