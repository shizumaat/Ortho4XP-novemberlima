"""Diagnose HECA runway-edge shoulder strips lumped into junctions.

Builds the full HECA patch and inspects the emitted shapes, focusing on
junctions that carry a long thin strip running along a runway edge (the
"wings" the user wants absorbed into a widened runway).

Usage:
  diag_heca_shoulders.py [ICAO]            # summary of all shapes
  diag_heca_shoulders.py [ICAO] 254 167    # detail on those way ids
"""
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.normpath(os.path.join(_HERE, "..", "src")),
          os.path.normpath(os.path.join(_HERE, "..")),
          os.path.join(_HERE, "..", "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from shapely.geometry import LineString

from conftest import xplane_root
from auto_patch.pipeline import build_airport_pavement
from auto_patch.layout import ROLE_RUNWAY

ICAO = sys.argv[1] if len(sys.argv) > 1 else "HECA"
WANT = {int(a) for a in sys.argv[2:]}  # JOSM way ids (positive form)

layout = build_airport_pavement(ICAO, xplane_root(), compute_elevations=True)

# Replicate to_osm's way-id assignment: start -10001, one per non-empty
# shape, in shape order.  JOSM shows the abs value; the user says "#254"
# meaning way -10254 (i.e. the 254th emitted way).
runways = [s for s in layout.shapes
           if s.role == ROLE_RUNWAY and s.polygon is not None
           and not s.polygon.is_empty]
rwy_edges = []  # (LineString edge, ref) for the two long edges of each rwy
for s in runways:
    c = list(s.polygon.exterior.coords)
    if c and c[0] == c[-1]:
        c = c[:-1]
    if len(c) != 4:
        continue
    edges = sorted(
        ((math.hypot(c[(i+1) % 4][0]-c[i][0], c[(i+1) % 4][1]-c[i][1]),
          c[i], c[(i+1) % 4]) for i in range(4)),
        key=lambda e: e[0])
    for _ln, a, b in edges[2:]:
        rwy_edges.append((LineString([a, b]), s.ref))


def _runway_edge_contact(poly):
    """(contact_len_m, max_perp_reach_m) of poly's boundary lying within
    2 m of any runway long edge."""
    best_len = 0.0
    for edge, _ref in rwy_edges:
        try:
            inter = poly.exterior.intersection(edge.buffer(2.0))
        except Exception:
            continue
        if not inter.is_empty:
            best_len = max(best_len, inter.length)
    return best_len


from shapely.ops import unary_union as _uu
from shapely.geometry import Point as _Pt
_AIRSIDE = {"primary_parallel", "secondary_parallel", "stub",
            "cross_connector", "junction", "apron", "runway_crossing"}
_airside_polys = [s.polygon for s in layout.shapes
                  if s.role in _AIRSIDE and s.polygon is not None
                  and not s.polygon.is_empty]
_airside_union = _uu(_airside_polys) if _airside_polys else None
print("\n=== FINAL-LAYOUT both-sides pavement coverage per runway ===")
print("(raycast each runway edge outward against ALL non-runway airside "
      "pavement = what's in the patch)")
for s in runways:
    c = list(s.polygon.exterior.coords)
    if c and c[0] == c[-1]:
        c = c[:-1]
    if len(c) != 4 or _airside_union is None:
        continue
    edges = sorted(
        ((math.hypot(c[(i+1) % 4][0]-c[i][0], c[(i+1) % 4][1]-c[i][1]),
          c[i], c[(i+1) % 4]) for i in range(4)), key=lambda e: e[0])
    for label, (_ln, a, b) in zip(("edgeA", "edgeB"), edges[2:]):
        ux, uy = (b[0]-a[0])/_ln, (b[1]-a[1])/_ln
        nx, ny = -uy, ux
        mx = 0.5*(a[0]+b[0]); my = 0.5*(a[1]+b[1])
        # outward = away from runway centroid
        cen = s.polygon.centroid
        if (mx-cen.x)*nx + (my-cen.y)*ny < 0:
            nx, ny = -nx, -ny
        prof = []
        nseg = max(2, int(_ln/10))
        for D in (1, 5, 10, 13, 16, 20):
            cov = 0
            for k in range(nseg+1):
                t = k/nseg
                px = a[0]+(b[0]-a[0])*t + nx*D
                py = a[1]+(b[1]-a[1])*t + ny*D
                if _airside_union.contains(_Pt(px, py)):
                    cov += 1
            prof.append((D, round(cov/(nseg+1), 2)))
        print(f"  {s.ref:8s} {label}: " +
              "  ".join(f"{D}m:{f}" for D, f in prof))
print()

print(f"{ICAO}: {len(layout.shapes)} shapes, {len(runways)} runways\n")
print(f"{'shapeID':>7} {'role':<18} {'area':>9} {'contact':>8}  ref")
for s_idx, s in enumerate(layout.shapes):
    if s.polygon is None or s.polygon.is_empty:
        continue
    contact = _runway_edge_contact(s.polygon)
    interesting = (s_idx in WANT) or (
        s.role == "junction" and contact > 30.0)
    if interesting:
        try:
            area = s.polygon.area
        except Exception:
            area = -1
        c = list(s.polygon.exterior.coords)
        print(f"{s_idx:>7} {s.role:<18} {area:9.0f} {contact:8.1f}  "
              f"{s.ref}  verts={len(c)-1}")
        if WANT and s_idx in WANT:
            mrr = s.polygon.minimum_rotated_rectangle
            print(f"         bounds={tuple(round(b) for b in s.polygon.bounds)}  "
                  f"mrr_area={mrr.area:.0f}")
            # Profile the band adjacent to each runway edge: how much of
            # this shape sits within D metres of a runway long edge, and
            # how thick is that band (area / contact_length)?
            for D in (8.0, 15.0, 25.0):
                tot_a = 0.0
                tot_len = 0.0
                for edge, eref in rwy_edges:
                    try:
                        band = s.polygon.intersection(edge.buffer(D))
                        clen = s.polygon.exterior.intersection(
                            edge.buffer(2.0)).length
                    except Exception:
                        continue
                    if not band.is_empty and clen > 1.0:
                        tot_a += band.area
                        tot_len += clen
                if tot_len > 1.0:
                    print(f"         band<={D:>4.0f}m: area={tot_a:8.0f}  "
                          f"contact_len={tot_len:7.0f}  "
                          f"mean_thick={tot_a/tot_len:5.1f}m")
