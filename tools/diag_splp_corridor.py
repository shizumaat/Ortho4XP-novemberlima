"""Prototype + validate the taxi-profile fit on the SPLP 'A' corridor.

Concatenates the north A leg (672m) + south A leg (642m) into one continuous
route through the junction, samples DEM along it, runs a 1-D grade-compliant
closest-to-DEM relaxation (the algorithm intended for taxi_redistribute), and
reports max deviation from DEM + resulting worst grade. Also reports what shapes
the south leg's far endpoint touches (runway anchor?).

Run: venv/bin/python tools/diag_splp_corridor.py
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
from auto_patch.layout import ROLE_RUNWAY  # noqa: E402
from auto_patch.elevation import _sample_dem, _load_airport_dem  # noqa: E402
from shapely.geometry import LineString, Point  # noqa: E402

ICAO = "SPLP"
layout = build_airport_pavement(ICAO, xplane_root(), compute_elevations=True)
tlat = int(math.floor(layout.anchor[0]))
tlon = int(math.floor(layout.anchor[1]))
dem = _load_airport_dem(layout.anchor[0], layout.anchor[1])

apt_lines = getattr(layout, "apt_taxi_centerlines", []) or []

# Identify the two A legs by endpoint near the junction (3.3,500.9).
JUNC = (3.3, 500.9)
def near_junc(pt):
    return math.hypot(pt[0]-JUNC[0], pt[1]-JUNC[1]) < 2.0

north = south = None
for tcl in apt_lines:
    ln, name = tcl.line, tcl.name
    if name != "A":
        continue
    a, b = ln.coords[0], ln.coords[-1]
    if abs(ln.length - 672.2) < 1.0:
        north = ln
    elif abs(ln.length - 642.9) < 1.0:
        south = ln
print(f"north leg: {None if north is None else round(north.length,1)}  "
      f"south leg: {None if south is None else round(south.length,1)}")

# Build one continuous corridor: north (from far end -> junction) + south
# (junction -> far end). north.coords already go (211,1140)->(3.3,500.9),
# i.e. far->junction. south goes junction->far. Concatenate.
coords = list(north.coords) + list(south.coords)[1:]
corridor = LineString(coords)
L = corridor.length
print(f"corridor length = {L:.1f}m  junction at arc≈{north.length:.1f}")

# Sample DEM along corridor at fine step.
STEP = 5.0
nseg = max(2, int(L / STEP))
arcs = [i * L / nseg for i in range(nseg + 1)]
dems = []
for s_arc in arcs:
    pt = corridor.interpolate(s_arc)
    lat, lon = layout.m_to_ll(pt.x, pt.y)
    e = _sample_dem(dem, tlat, tlon, lat, lon)
    dems.append(e)

# South far endpoint: what shapes touch it?
far = Point(south.coords[-1])
print(f"\nSouth leg far end ({far.x:.1f},{far.y:.1f}) — runway corners within 20m:")
for sh in layout.shapes:
    if sh.role != ROLE_RUNWAY or sh.polygon is None or sh.polygon.is_empty:
        continue
    if sh.polygon.distance(far) < 20.0:
        print(f"  RUNWAY ref={sh.ref!r} dist={sh.polygon.distance(far):.1f}")

# ── 1-D grade-compliant closest-to-DEM relaxation ──
CAP = 0.015
import os as _os
BUDGET_DOWN = float(_os.environ.get("BUDGET_DOWN", "1.5"))  # dip below terrain
BUDGET_UP = float(_os.environ.get("BUDGET_UP", "0.5"))      # rise above terrain
prof = [e if e is not None else 0.0 for e in dems]   # seed at DEM
has = [e is not None for e in dems]
ds = [arcs[i+1]-arcs[i] for i in range(len(arcs)-1)]
for it in range(50000):
    prev = list(prof)
    # cap projection along the chain (multi-sweep) — grade constraint
    for _sw in range(5):
        moved = False
        for i in range(len(prof)-1):
            cap = CAP * ds[i]
            diff = prof[i] - prof[i+1]
            if abs(diff) > cap + 1e-9:
                ex = (abs(diff) - cap) * (1 if diff > 0 else -1)
                prof[i] -= 0.5*ex
                prof[i+1] += 0.5*ex
                moved = True
        if not moved:
            break
    # deviation budget clamp — bound how far the profile leaves terrain.
    # Asymmetric: dip readily (spread descents), rise reluctantly.
    for i in range(len(prof)):
        if not has[i]:
            continue
        lo = dems[i] - BUDGET_DOWN
        hi = dems[i] + BUDGET_UP
        if prof[i] < lo:
            prof[i] = lo
        elif prof[i] > hi:
            prof[i] = hi
    maxch = max(abs(prof[i]-prev[i]) for i in range(len(prof)))
    if maxch < 1e-5:
        print(f"(converged at iter {it})")
        break
# report
worst_g = 0.0
for i in range(len(prof)-1):
    g = abs(prof[i]-prof[i+1])/ds[i]
    worst_g = max(worst_g, g)
maxdev = max((abs(prof[i]-dems[i]) for i in range(len(prof)) if has[i]), default=0.0)
print(f"\nFITTED PROFILE: worst grade={worst_g*100:.2f}%  max|prof-DEM|={maxdev:.2f}m")
print("\narc     DEM    prof   dev    grade")
for i in range(len(arcs)):
    e = dems[i]
    es = "None" if e is None else f"{e:6.2f}"
    dev = "" if not has[i] else f"{prof[i]-dems[i]:+5.2f}"
    g = "" if i == 0 else f"{(prof[i]-prof[i-1])/ds[i-1]*100:+.2f}%"
    mark = " <-- junction" if abs(arcs[i]-north.length) < STEP else ""
    # only print every other to keep it short, plus junction zone
    if i % 4 == 0 or abs(arcs[i]-north.length) < 70:
        print(f"{arcs[i]:6.1f} {es} {prof[i]:6.2f} {dev:>5} {g:>7}{mark}")
