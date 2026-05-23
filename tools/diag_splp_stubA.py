"""Inspect the SPLP stub/A shapes that show a within-shape grade violation
under the L2 cascade: corners, alts, DEM, and whether each corner is a HARD
seam anchor (so the solver can't move it).

Run: venv/bin/python tools/diag_splp_stubA.py
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
from auto_patch.layout import ROLE_STUB, SHARED_VERTEX_TOL_M  # noqa: E402
from auto_patch.elevation import _sample_dem, _load_airport_dem  # noqa: E402

layout = build_airport_pavement("SPLP", xplane_root(), compute_elevations=True)
tlat = int(math.floor(layout.anchor[0]))
tlon = int(math.floor(layout.anchor[1]))
dem = _load_airport_dem(layout.anchor[0], layout.anchor[1])
seam_keys = getattr(layout, "_seam_anchor_keys", None) or set()
bk_s = 1.0 / SHARED_VERTEX_TOL_M

def seam_bucket(x, y):
    return (int(round(x * bk_s)), int(round(y * bk_s)))

print(f"seam anchor keys: {len(seam_keys)}")
for s in layout.shapes:
    if s.role != ROLE_STUB or s.ref != "A":
        continue
    if s.polygon is None or s.polygon.is_empty:
        continue
    ring = list(s.polygon.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if s.node_altitudes:
        alts = list(s.node_altitudes)[:len(ring)]
        kind = "node_altitudes"
    elif s.altitude_high is not None:
        alts = [s.altitude_high, s.altitude_low, s.altitude_low, s.altitude_high][:len(ring)]
        kind = f"hi/lo {s.altitude_high}/{s.altitude_low}"
    else:
        alts = [s.altitude] * len(ring)
        kind = f"flat {s.altitude}"
    c = s.polygon.centroid
    # worst sloping-edge / pair grade
    worst = 0.0
    for i in range(len(ring)):
        for j in range(i+1, len(ring)):
            d = math.hypot(ring[i][0]-ring[j][0], ring[i][1]-ring[j][1])
            if d < 1.0: continue
            g = abs(alts[i]-alts[j])/d
            worst = max(worst, g)
    print(f"\nstub/A centroid=({c.x:.1f},{c.y:.1f}) verts={len(ring)} "
          f"{kind} worst={worst*100:.2f}%")
    for i,(pt,a) in enumerate(zip(ring,alts)):
        lat,lon = layout.m_to_ll(pt[0],pt[1])
        e = _sample_dem(dem,tlat,tlon,lat,lon)
        is_seam = seam_bucket(pt[0],pt[1]) in seam_keys
        es = "None" if e is None else f"{e:.2f}"
        print(f"  v{i}: ({pt[0]:8.1f},{pt[1]:8.1f}) alt={a!s:>6} DEM={es:>6} "
              f"{'SEAM-HARD' if is_seam else ''}")

# ── Nearest HARD anchors (runway corners + seam verts) to the broken stub ──
from auto_patch.layout import ROLE_RUNWAY, ROLE_RUNWAY_CROSSING
hard_pts = []  # (x,y,alt,kind)
for s in layout.shapes:
    if s.role in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING) and s.polygon and not s.polygon.is_empty:
        ring = list(s.polygon.exterior.coords)
        if ring and ring[0]==ring[-1]: ring=ring[:-1]
        if s.node_altitudes:
            av=list(s.node_altitudes)[:len(ring)]
        elif s.altitude_high is not None and len(ring)==4:
            av=[s.altitude_high,s.altitude_low,s.altitude_low,s.altitude_high]
        elif s.altitude is not None:
            av=[s.altitude]*len(ring)
        else: continue
        for (x,y),a in zip(ring,av):
            hard_pts.append((x,y,a,f"rwy{s.ref}"))
print(f"\nrunway hard corners: {len(hard_pts)}")
# the broken stub corners
broken=[(64.2,454.4,72.0),(45.4,465.1,70.8),(53.4,485.3,70.8),(73.6,481.3,72.0)]
for (vx,vy,va) in broken:
    near=sorted(((math.hypot(vx-hx,vy-hy),ha,hk) for hx,hy,ha,hk in hard_pts))[:3]
    print(f" stub corner ({vx},{vy}) alt={va}: nearest hard:")
    for d,ha,hk in near:
        print(f"    {hk} alt={ha:.1f} dist={d:.1f}m  implied min-grade={abs(va-ha)/max(d,0.1)*100:.2f}%")
