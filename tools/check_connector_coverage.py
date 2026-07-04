"""Task-4 invariant probe: every apt.dat truck route must be COVERED by
emitted pavement (any role) along its whole length — a gap = a severed
airside<->groundside connector.  Samples each truck-route polyline every
5 m and reports uncovered runs > 15 m.

Usage: venv/bin/python connector_coverage_probe.py ICAO
"""
import os, sys, math

os.environ.setdefault("O4_LOG_VERBOSITY", "0")
ROOT = "/Users/noah/Ortho4XP-novemberlima"
for p in (ROOT + "/src", ROOT, ROOT + "/tests", ROOT + "/tools"):
    if p not in sys.path:
        sys.path.insert(0, p)

from conftest import xplane_root
from auto_patch.pipeline import build_airport_pavement

icao = sys.argv[1] if len(sys.argv) > 1 else "CYXY"
layout = build_airport_pavement(icao, xplane_root(), compute_elevations=True)

from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from shapely import prepare, contains_xy

pav = unary_union([s.polygon for s in layout.shapes
                   if s.polygon is not None and not s.polygon.is_empty
                   and s.role not in ("boundary", "clearance",
                                      "taxiway_clearance")])
pav = pav.buffer(2.0)     # tolerance: road half-width
prepare(pav)

routes = getattr(layout, "apt_truck_centerlines", None) or []
if not routes:
    # fall back to any centerline collection tagged service
    routes = [c for c in (getattr(layout, "apt_taxi_centerlines", None) or [])
              if getattr(c, "is_service", False)]
print(f"{icao}: {len(routes)} truck/service centerline(s)")
n_gap = 0
for ri, r in enumerate(routes):
    pts = getattr(r, "points_m", None) or getattr(r, "pts", None)
    if pts is None and isinstance(r, (list, tuple)):
        pts = r
    if not pts or len(pts) < 2:
        continue
    line = LineString(pts)
    L = line.length
    step = 5.0
    gap_start = None
    k = 0.0
    while k <= L:
        p = line.interpolate(k)
        inside = contains_xy(pav, p.x, p.y)
        if not inside and gap_start is None:
            gap_start = k
        elif inside and gap_start is not None:
            if k - gap_start > 15.0:
                n_gap += 1
                mid = line.interpolate(0.5 * (gap_start + k))
                la, lo = layout.m_to_ll(mid.x, mid.y)
                print(f"  GAP {k-gap_start:5.0f}m on route#{ri} "
                      f"@({la:.6f},{lo:.6f})")
            gap_start = None
        k += step
    if gap_start is not None and L - gap_start > 15.0:
        n_gap += 1
        mid = line.interpolate(0.5 * (gap_start + L))
        la, lo = layout.m_to_ll(mid.x, mid.y)
        print(f"  GAP {L-gap_start:5.0f}m (to end) on route#{ri} "
              f"@({la:.6f},{lo:.6f})")
print(f"uncovered runs >15m: {n_gap}")
