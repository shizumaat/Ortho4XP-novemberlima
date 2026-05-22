"""Count Triangle4XP mesh triangles overall and inside an airport region.

X-Plane load/render cost scales with the TILE MESH triangle count produced
by Triangle4XP — NOT with the patch's node count (the airport mesh is
AREA-refined, so it has far more triangles than the patch has vertices).
This tool reads a built Ortho4XP ``.mesh`` (MEDIT format) and reports the
total triangle count plus how many fall in the airport's bounding box —
the patch's real triangle footprint, the number to optimize against.

Pair each measurement with a measured X-Plane load time to find the
triangle↔load-time curve and the right density compromises.

Usage:
    # bbox from an emitted patch OSM (single- or double-quoted):
    venv/bin/python tools/mesh_region_tris.py \
        --mesh "/Users/noah/X-Plane 12/Custom Scenery/zOrtho4XP_+30+031/Data+30+031.mesh" \
        --patch-osm /tmp/HECA_auto.patch.osm

    # or an explicit bbox lat0,lat1,lon0,lon1:
    venv/bin/python tools/mesh_region_tris.py --mesh <file> --bbox 30.08,30.15,31.37,31.46
"""
from __future__ import annotations

import argparse
import array
import re
import time


def _patch_bbox(path, margin=0.002):
    txt = open(path).read()
    lats = [float(m) for m in re.findall(r"lat=['\"](-?[\d.]+)['\"]", txt)]
    lons = [float(m) for m in re.findall(r"lon=['\"](-?[\d.]+)['\"]", txt)]
    if not lats or not lons:
        raise SystemExit(f"no lat/lon nodes found in {path}")
    return (min(lats) - margin, max(lats) + margin,
            min(lons) - margin, max(lons) + margin)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mesh", required=True, help="path to a .mesh file")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--patch-osm", help="derive airport bbox from this patch OSM")
    g.add_argument("--bbox", help="lat0,lat1,lon0,lon1")
    args = ap.parse_args(argv)

    if args.bbox:
        la0, la1, lo0, lo1 = (float(x) for x in args.bbox.split(","))
    else:
        la0, la1, lo0, lo1 = _patch_bbox(args.patch_osm)
    print(f"airport bbox: lat {la0:.4f}..{la1:.4f}  lon {lo0:.4f}..{lo1:.4f}")

    t = time.time()
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
        in_box = 0
        for _ in range(nt):
            p = f.readline().split()
            a, b, c = int(p[0]) - 1, int(p[1]) - 1, int(p[2]) - 1
            cx = (vlon[a] + vlon[b] + vlon[c]) / 3.0
            cy = (vlat[a] + vlat[b] + vlat[c]) / 3.0
            if lo0 <= cx <= lo1 and la0 <= cy <= la1:
                in_box += 1
    print(f"vertices: {nv:,}")
    print(f"total tile triangles: {nt:,}")
    print(f"triangles in airport bbox: {in_box:,}  "
          f"({100.0 * in_box / nt:.1f}% of tile)  ← patch footprint")
    print(f"(parsed in {time.time() - t:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
