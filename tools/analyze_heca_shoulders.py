"""Offline analysis of HECA shoulder geometry from /tmp/heca_geom.pkl.

For each of the 3 apt.dat runways, profile BOTH sides: march outward
from the runway edge (at the apt.dat half-width) against the union of
all non-runway airside pavement, reporting coverage vs distance, so we
can see where ~13 m shoulder pieces sit and on which sides.  Also lists
the individual pavement pieces that abut each runway edge (role, area,
along-runway length, perpendicular offset+thickness) = the "shoulders".
"""
import math
import pickle

from shapely import wkb as _wkb
from shapely.geometry import Point
from shapely.ops import unary_union

with open("/tmp/heca_geom.pkl", "rb") as f:
    data = pickle.load(f)

print(f"widened={data['widened']}  shapes={len(data['shapes'])}\n")

AIRSIDE = {"primary_parallel", "secondary_parallel", "stub",
           "cross_connector", "junction", "apron", "runway_crossing"}
shapes = [(role, ref, _wkb.loads(w)) for role, ref, w in data["shapes"]]
airside = [(role, ref, g) for role, ref, g in shapes
           if role in AIRSIDE and not g.is_empty]
airside_union = unary_union([g for _r, _f, g in airside])

for ref, ax, ay, bx, by, width in data["runways"]:
    udx, udy = bx - ax, by - ay
    L = math.hypot(udx, udy)
    ux, uy = udx / L, udy / L
    nx, ny = -uy, ux
    half = width / 2.0
    print(f"=== {ref}  L={L:.0f}m  width={width:.1f}m (half={half:.1f}) ===")
    n_st = max(2, int(L / 10))
    for side, sname in ((1.0, "+side"), (-1.0, "-side")):
        prof = []
        for D in (1, 3, 5, 8, 11, 13, 16, 20, 30):
            cov = 0
            for k in range(n_st + 1):
                s = L * k / n_st
                ex = ax + ux * s + nx * side * (half + D)
                ey = ay + uy * s + ny * side * (half + D)
                if airside_union.contains(Point(ex, ey)):
                    cov += 1
            prof.append((D, round(cov / (n_st + 1), 2)))
        print(f"  {sname}: " + "  ".join(f"{D}:{f}" for D, f in prof))
    # Per-piece: which airside shapes abut this runway's edges within 13m?
    edgeA = [(ax + nx * half, ay + ny * half),
             (bx + nx * half, by + ny * half)]
    edgeB = [(ax - nx * half, ay - ny * half),
             (bx - nx * half, by - ny * half)]
    from shapely.geometry import LineString
    for elabel, epts in (("+edge", edgeA), ("-edge", edgeB)):
        eline = LineString(epts)
        hits = []
        for role, sref, g in airside:
            try:
                contact = g.exterior.intersection(eline.buffer(2.0)).length
            except Exception:
                continue
            if contact < 5.0:
                continue
            # thickness = area within 13m band / contact
            try:
                band = g.intersection(eline.buffer(13.0))
                thick = band.area / contact if contact > 0 else 0
            except Exception:
                thick = -1
            hits.append((role, g.area, contact, thick))
        hits.sort(key=lambda h: -h[2])
        if hits:
            print(f"  {elabel} abutting pieces (contact>=5m):")
            for role, area, contact, thick in hits[:8]:
                print(f"      {role:16s} area={area:8.0f} "
                      f"contact={contact:6.0f}m thick<=13m={thick:5.1f}m")
    print()
