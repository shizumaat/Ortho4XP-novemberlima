"""Analyze Triangle4XP mesh triangle SIZES + locate degenerate hotspots.

X-Plane load time tracks the triangle count, and pathological load times
come from DEGENERATE micro-triangulation — millions of sub-1 m² (often
sub-cm²) triangles — not from honest curvature refinement.  This tool
reads a built ``.mesh`` (MEDIT), restricts to an airport bbox, and reports:

  * the triangle-area histogram (how many are sub-1 m² etc.)
  * the ~100 m cells where the micro-triangles concentrate (hotspots)

A high sub-1 m² fraction concentrated in a few cells = a geometry/DEM
defect at those spots (overlapping/near-coincident constraint edges, or
sharp DEM-curvature steps from building/terminal flattening) — which no
``cell_size`` / ``curvature_tol`` / ``apt_curv_tol`` knob can fix.  A flat,
meter-scale distribution = honest curvature refinement (tune curvature_tol).

Usage:
    venv/bin/python tools/mesh_triangle_quality.py \
        --mesh "/Users/noah/X-Plane 12/Custom Scenery/zOrtho4XP_+30+031/Data+30+031.mesh" \
        --patch-osm /tmp/HECA_auto.patch.osm
    # or --bbox lat0,lat1,lon0,lon1
"""
from __future__ import annotations

import argparse
import array
import math
import re
import time
from collections import defaultdict


def _patch_bbox(path, margin=0.002):
    txt = open(path).read()
    lats = [float(m) for m in re.findall(r"lat=['\"](-?[\d.]+)['\"]", txt)]
    lons = [float(m) for m in re.findall(r"lon=['\"](-?[\d.]+)['\"]", txt)]
    if not lats:
        raise SystemExit(f"no lat/lon nodes in {path}")
    return (min(lats) - margin, max(lats) + margin,
            min(lons) - margin, max(lons) + margin)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--patch-osm")
    g.add_argument("--bbox")
    ap.add_argument("--micro", type=float, default=1.0,
                    help="micro-triangle area threshold m² (default 1.0)")
    ap.add_argument("--cell-deg", type=float, default=0.001,
                    help="hotspot cell size in degrees (~100 m, default)")
    args = ap.parse_args(argv)

    if args.bbox:
        la0, la1, lo0, lo1 = (float(x) for x in args.bbox.split(","))
    else:
        la0, la1, lo0, lo1 = _patch_bbox(args.patch_osm)
    latm = (la0 + la1) / 2
    mlat, mlon = 111320.0, 111320.0 * math.cos(math.radians(latm))

    t = time.time()
    vlon, vlat = array.array("d"), array.array("d")
    buckets = [("<1", 1.0), ("1-5", 5.0), ("5-25", 25.0),
               ("25-100", 100.0), ("100-1k", 1000.0), (">1k", float("inf"))]
    hist = defaultdict(int)
    grid = defaultdict(int)
    areas = []
    apt_n = 0
    with open(args.mesh) as f:
        line = f.readline()
        while line and not line.startswith("Vertices"):
            line = f.readline()
        nv = int(f.readline())
        for _ in range(nv):
            p = f.readline().split()
            vlon.append(float(p[0]))
            vlat.append(float(p[1]))
        line = f.readline()
        while line and not line.startswith("Triangles"):
            line = f.readline()
        nt = int(f.readline())
        for _ in range(nt):
            p = f.readline().split()
            a, b, c = int(p[0]) - 1, int(p[1]) - 1, int(p[2]) - 1
            cx = (vlon[a] + vlon[b] + vlon[c]) / 3
            cy = (vlat[a] + vlat[b] + vlat[c]) / 3
            if not (lo0 <= cx <= lo1 and la0 <= cy <= la1):
                continue
            apt_n += 1
            x1 = (vlon[a] - cx) * mlon; y1 = (vlat[a] - cy) * mlat
            x2 = (vlon[b] - cx) * mlon; y2 = (vlat[b] - cy) * mlat
            x3 = (vlon[c] - cx) * mlon; y3 = (vlat[c] - cy) * mlat
            ar = abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1)) / 2
            areas.append(ar)
            for label, hi in buckets:
                if ar < hi:
                    hist[label] += 1
                    break
            if ar < args.micro:
                grid[(round(cx / args.cell_deg),
                      round(cy / args.cell_deg))] += 1

    if not apt_n:
        print("no triangles in bbox")
        return 1
    areas.sort()
    n = len(areas)
    print(f"airport triangles: {apt_n:,}   (parsed {time.time() - t:.0f}s)")
    print(f"area m²: min={areas[0]:.3f} p50={areas[n//2]:.3f} "
          f"p90={areas[9*n//10]:.3f} max={areas[-1]:,.0f}")
    print("area histogram:")
    for label, _ in buckets:
        v = hist[label]
        print(f"   {label:>8} m²: {v:>10,}  ({100*v/apt_n:5.1f}%)")
    micro = sum(grid.values())
    top = sorted(grid.items(), key=lambda kv: -kv[1])[:12]
    print(f"\nmicro-triangles (<{args.micro:g} m²): {micro:,} "
          f"in {len(grid)} cells")
    sh = sum(n_ for _, n_ in top)
    print(f"top-12 cells hold {100*sh/max(1,micro):.1f}%  (count @ lat,lon):")
    for (gx, gy), c in top:
        print(f"   {c:>9,}  @ {gy*args.cell_deg:.4f},{gx*args.cell_deg:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
