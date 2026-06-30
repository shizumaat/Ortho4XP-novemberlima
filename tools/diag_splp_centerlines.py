"""Diagnostic: SPLP taxi-centerline profiles around the -10025/-10026
grade violations. Dumps, for each apt taxi centerline that passes near the
violating junctions, arc-length vs DEM vs solved-elevation, plus the junction
centroids/grades and how much route length is available to spread the descent.

Run: venv/bin/python tools/diag_splp_centerlines.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from conftest import xplane_root  # noqa: E402
from auto_patch.pipeline import build_airport_pavement  # noqa: E402
from auto_patch.layout import ROLE_JUNCTION  # noqa: E402
from auto_patch.elevation import _sample_dem, _load_airport_dem  # noqa: E402
import math  # noqa: E402

ICAO = "SPLP"

layout = build_airport_pavement(ICAO, xplane_root(), compute_elevations=True)

# DEM + tile coords (mirror pipeline standalone path).
tlat = int(math.floor(layout.anchor[0]))
tlon = int(math.floor(layout.anchor[1]))
dem = _load_airport_dem(layout.anchor[0], layout.anchor[1])


def shape_alts(s):
    """Return per-open-vertex altitude list for a shape."""
    ring = list(s.polygon.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if s.node_altitudes:
        na = list(s.node_altitudes)
        if len(na) > len(ring):
            na = na[:len(ring)]
        return ring, na
    if s.altitude_high is not None and s.altitude_low is not None and len(ring) == 4:
        return ring, [s.altitude_high, s.altitude_low, s.altitude_low, s.altitude_high]
    if s.altitude is not None:
        return ring, [s.altitude] * len(ring)
    return ring, [None] * len(ring)


def grade_pairs(ring, alts):
    """Worst within-shape grade (all-pair Euclidean)."""
    worst = 0.0
    worst_info = None
    n = len(ring)
    for i in range(n):
        for j in range(i + 1, n):
            if alts[i] is None or alts[j] is None:
                continue
            d = math.hypot(ring[i][0] - ring[j][0], ring[i][1] - ring[j][1])
            if d < 1.0:
                continue
            g = abs(alts[i] - alts[j]) / d
            if g > worst:
                worst = g
                worst_info = (i, j, d, alts[i], alts[j])
    return worst, worst_info


# ── Junctions: print centroid, vertex count, alt range, worst grade ──
print("=" * 70)
print("JUNCTIONS (worst within-shape grade, sorted):")
juncs = []
for s in layout.shapes:
    if s.role != ROLE_JUNCTION:
        continue
    if s.polygon is None or s.polygon.is_empty:
        continue
    ring, alts = shape_alts(s)
    valid = [a for a in alts if a is not None]
    if not valid:
        continue
    worst, info = grade_pairs(ring, alts)
    c = s.polygon.centroid
    juncs.append((worst, s, ring, alts, info, (c.x, c.y), min(valid), max(valid)))

juncs.sort(key=lambda t: -t[0])
for worst, s, ring, alts, info, ctr, amin, amax in juncs[:10]:
    print(f"\n  ref={s.ref!r} centroid=({ctr[0]:.1f},{ctr[1]:.1f}) "
          f"verts={len(ring)} alt={amin:.1f}..{amax:.1f} area={s.polygon.area:.0f}")
    print(f"    worst within-shape grade = {worst*100:.2f}%")
    if info:
        i, j, d, ai, aj = info
        print(f"    worst pair: v{i}({ring[i][0]:.1f},{ring[i][1]:.1f})={ai:.2f} -> "
              f"v{j}({ring[j][0]:.1f},{ring[j][1]:.1f})={aj:.2f}  d={d:.1f}m  "
              f"de={abs(ai-aj):.2f}")

# ── Centerlines passing near the worst few junctions ──
print("\n" + "=" * 70)
print("CENTERLINES near worst junctions:")
worst_centroids = [j[5] for j in juncs[:3]]
apt_lines = getattr(layout, "apt_taxi_centerlines", []) or []
print(f"  total apt_taxi_centerlines: {len(apt_lines)}")

from shapely.geometry import Point  # noqa: E402

for tcl in apt_lines:
    ln, name = tcl.line, tcl.name
    if ln is None or ln.is_empty:
        continue
    # does it pass within 30m of any worst junction centroid?
    near = None
    for cx, cy in worst_centroids:
        d = ln.distance(Point(cx, cy))
        if d < 30.0:
            near = (cx, cy, d)
            break
    if near is None:
        continue
    letter = tcl.dominant_size() or "?"
    print(f"\n  centerline name={name!r} letter={letter} length={ln.length:.1f}m "
          f"passes {near[2]:.1f}m from junction near ({near[0]:.1f},{near[1]:.1f})")
    # arc-length vs DEM profile
    step = 10.0
    nsteps = max(2, int(ln.length / step) + 1)
    prev_dem = None
    print("    s(m)    x       y       DEM    grade-from-prev")
    for k in range(nsteps + 1):
        s_arc = min(ln.length, k * step)
        pt = ln.interpolate(s_arc)
        lat, lon = layout.m_to_ll(pt.x, pt.y)
        e = _sample_dem(dem, tlat, tlon, lat, lon)
        g = ""
        if prev_dem is not None and e is not None:
            g = f"{(e - prev_dem)/step*100:+.2f}%"
        print(f"    {s_arc:6.1f} {pt.x:7.1f} {pt.y:7.1f}  "
              f"{e if e is None else round(e,2)!s:>6}  {g}")
        prev_dem = e
