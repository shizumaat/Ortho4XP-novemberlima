"""Bin tile-mesh triangles into ground cells and report the hotspots.

Reads a built Ortho4XP ``.mesh`` (MEDIT format, as
``tools/mesh_region_tris.py``) and bins triangle CENTROIDS inside a
bbox into square cells (default 25 m), printing the densest cells with
lat/lon centers plus each cell's median triangle area — a Ruppert
epsilon-explosion shows up as a cell with tens of thousands of
µm²-to-cm² triangles at one seam, versus lawful refinement which stays
in the m² range.

Usage:
    venv/bin/python tools/mesh_hotspot_cells.py \
        --mesh <Data+60-136.mesh> --patch-osm <CYXY patch> \
        [--cell 25] [--top 15]
"""
from __future__ import annotations

import argparse
import array
import math
from collections import defaultdict

from mesh_region_tris import _patch_bbox


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mesh", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--patch-osm")
    g.add_argument("--bbox")
    ap.add_argument("--cell", type=float, default=25.0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    if args.bbox:
        la0, la1, lo0, lo1 = (float(x) for x in args.bbox.split(","))
    else:
        la0, la1, lo0, lo1 = _patch_bbox(args.patch_osm)

    vlon, vlat = array.array("d"), array.array("d")
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
        mlat = 111320.0
        mlon = 111320.0 * math.cos(math.radians((la0 + la1) / 2))
        cells = defaultdict(int)
        areas = defaultdict(list)
        for _ in range(nt):
            p = f.readline().split()
            a, b, c = int(p[0]) - 1, int(p[1]) - 1, int(p[2]) - 1
            cx = (vlon[a] + vlon[b] + vlon[c]) / 3.0
            cy = (vlat[a] + vlat[b] + vlat[c]) / 3.0
            if not (lo0 <= cx <= lo1 and la0 <= cy <= la1):
                continue
            key = (int(cy * mlat / args.cell),
                   int(cx * mlon / args.cell))
            cells[key] += 1
            ax, ay = vlon[a] * mlon, vlat[a] * mlat
            bx, by = vlon[b] * mlon, vlat[b] * mlat
            cx2, cy2 = vlon[c] * mlon, vlat[c] * mlat
            area = abs((bx - ax) * (cy2 - ay)
                       - (cx2 - ax) * (by - ay)) / 2.0
            if len(areas[key]) < 4000:
                areas[key].append(area)
    total = sum(cells.values())
    print(f"triangles in bbox: {total:,}  cells: {len(cells)} "
          f"({args.cell:.0f} m)")
    for key, n in sorted(cells.items(), key=lambda kv: -kv[1])[:args.top]:
        aa = sorted(areas[key])
        med = aa[len(aa) // 2] if aa else 0.0
        la = key[0] * args.cell / mlat
        lo = key[1] * args.cell / mlon
        print(f"  {n:8,} tris  median {med:12.6f} m2  "
              f"@ {la:.6f},{lo:.6f}")
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, "tools")
    raise SystemExit(main())
