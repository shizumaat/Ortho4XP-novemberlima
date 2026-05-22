"""Build an airport and dump the pipeline's real ``pav_union`` to OSM.

``pav_union`` is the merged + seam-cleaned + simplified pavement coverage
the pipeline uses to build rects/junctions, captured BEFORE any runway /
groundside subtraction — the geometry a reviewer looks at to judge "is the
union clean".  See ``_diag.build_capturing_union`` for the capture point.

Usage:
    venv/bin/python tools/dump_pav_union.py HECA
    venv/bin/python tools/dump_pav_union.py CYXY --out /tmp/cyxy_union.osm
"""
from __future__ import annotations

import argparse

import _diag


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _diag.add_common_args(ap)
    args = ap.parse_args(argv)
    out = args.out or f"/tmp/{args.icao}_pav_union.osm"

    print(f"Building {args.icao} ...", flush=True)
    layout, pav_union = _diag.build_capturing_union(args.icao, args.xplane)
    if pav_union is None:
        print("ERROR: pav_union was not captured "
              "(union step may have been renamed — see _diag).")
        return 1

    print(f"pav_union (post close+open + simplify): "
          f"type={pav_union.geom_type} area={pav_union.area:,.0f} m²")
    npoly, nrings = _diag.geom_to_osm(
        pav_union, layout.m_to_ll, out, tag_k="area", tag_v="yes")
    print(f"Wrote {out}: {npoly} polygon(s), {nrings} ring(s).")
    print(f"  anchor={layout.anchor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
