"""SPLP runway 02/20: CIFP vs post-regrade threshold elevations, seam
crossings, and where taxiway A connects to the runway (at what runway
elevation). Clarifies whether the seam-wins/threshold-adjusts rule is firing
and whether the taxi network anchors to the adjusted threshold.

Run: venv/bin/python tools/diag_splp_runway.py
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
from auto_patch.layout import ROLE_RUNWAY, SHARED_VERTEX_TOL_M  # noqa: E402

layout = build_airport_pavement("SPLP", xplane_root(), compute_elevations=True)

seam_keys = getattr(layout, "_seam_anchor_keys", None) or set()
bk_s = 1.0 / SHARED_VERTEX_TOL_M
def seam_bk(x, y):
    return (int(round(x*bk_s)), int(round(y*bk_s)))

# Runway profile state (CIFP + seam samples, pre-regrade).
ps = getattr(layout, "_runway_profile_state", None)
print("=== _runway_profile_state ===")
if ps:
    for k, st in ps.items():
        print(f" pair {k}: phys_dist={st.get('phys_dist_m'):.0f}m")
        fr = st.get('fractions'); el = st.get('elevs'); an = st.get('anchored')
        for f, e, a in zip(fr, el, an):
            print(f"   t={f:.3f} e={e:.2f} {'ANCHOR' if a else ''}")
else:
    print(" (none)")

# Final runway segment elevations + which corners are seam-hard.
print("\n=== runway 02/20 segment corners (final) ===")
allc = []
for s in layout.shapes:
    if s.role != ROLE_RUNWAY or s.ref != "02/20":
        continue
    if s.polygon is None or s.polygon.is_empty:
        continue
    ring = list(s.polygon.exterior.coords)
    if ring and ring[0]==ring[-1]: ring=ring[:-1]
    if s.node_altitudes:
        av = list(s.node_altitudes)[:len(ring)]
    elif s.altitude_high is not None and len(ring)==4:
        av = [s.altitude_high,s.altitude_low,s.altitude_low,s.altitude_high]
    elif s.altitude is not None:
        av = [s.altitude]*len(ring)
    else:
        continue
    for (x,y),a in zip(ring,av):
        allc.append((x,y,a, seam_bk(x,y) in seam_keys))
# dedup + sort by y (south->north)
seen=set(); uniq=[]
for x,y,a,sm in allc:
    key=(round(x,1),round(y,1))
    if key in seen: continue
    seen.add(key); uniq.append((x,y,a,sm))
uniq.sort(key=lambda t:t[1])
for x,y,a,sm in uniq:
    print(f"  ({x:8.1f},{y:8.1f}) alt={a:6.2f} {'SEAM-HARD' if sm else ''}")

# Where does taxiway A connect to the runway? For each apt 'A' centerline,
# report endpoints near the runway polygon + runway alt there.
print("\n=== taxiway A endpoints vs runway ===")
from shapely.geometry import Point
rwys = [s for s in layout.shapes if s.role==ROLE_RUNWAY and s.ref=="02/20"
        and s.polygon and not s.polygon.is_empty]
def rwy_alt_near(pt):
    best=None
    for x,y,a,sm in uniq:
        d=math.hypot(pt[0]-x,pt[1]-y)
        if best is None or d<best[0]: best=(d,a)
    return best
for tcl in (getattr(layout,'apt_taxi_centerlines',[]) or []):
    ln, name = tcl.line, tcl.name
    if name!="A" or ln is None or ln.is_empty: continue
    for end in (ln.coords[0], ln.coords[-1]):
        p=Point(end)
        dmin=min((r.polygon.distance(p) for r in rwys), default=9e9)
        if dmin < 30:
            d,a = rwy_alt_near(end)
            print(f"  A end ({end[0]:.1f},{end[1]:.1f}) {dmin:.1f}m from rwy; "
                  f"nearest rwy corner alt={a:.2f} (d={d:.1f}m)")
