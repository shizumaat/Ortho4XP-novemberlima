"""Run the auto-patch pavement builder on an ICAO and dump to OSM.

Usage:
    python3 tools/build_target_osm.py <ICAO> [--xplane PATH] [--out PATH]

Then compare with ``tools/compare_target.py`` against the reference
target OSM.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from auto_patch.pipeline import build_airport_pavement


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("icao")
    ap.add_argument("--xplane", default="/Users/noah/X-Plane 12")
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--stage", default="final",
        choices=["raw", "final"],
        help=("'raw' = dump after junction_emit (residue construction) "
              "but BEFORE Phase 2: densification, runway 1:1 snap, "
              "rect-corner snap, widen, push-outside, per-surface "
              "solver, terminal stitch.  'final' = full pipeline."))
    args = ap.parse_args(argv)

    import math
    from collections import Counter
    compute = (args.stage == "final")
    out_dir = os.path.dirname(args.out) if args.out else "/tmp"
    base = (os.path.basename(args.out).rsplit(".osm", 1)[0]
            if args.out else f"{args.icao}_auto")

    # Footprint tiles (geometry-only build is cheap).  A cross-tile airport
    # MUST be re-cut PER TILE with that tile's OWN smoothed DEM — building
    # the whole airport samples the non-anchor (sliver) tile against the
    # anchor-tile DEM (clamped at its edge → wrong terrain), so the fixture
    # wouldn't match production.  Mirrors tests/conftest.cached_airport_layout.
    foot = build_airport_pavement(
        args.icao, args.xplane, compute_elevations=False)
    lats: list = []
    lons: list = []
    for s in foot.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        for (x, y) in s.polygon.exterior.coords:
            la, lo = foot.m_to_ll(x, y)
            lats.append(la)
            lons.append(lo)
    tiles = []
    if lats:
        for la in range(int(math.floor(min(lats))),
                        int(math.floor(max(lats))) + 1):
            for lo in range(int(math.floor(min(lons))),
                            int(math.floor(max(lons))) + 1):
                tiles.append((la, lo))
    multi = len(tiles) > 1

    def _emit(layout, out):
        layout.to_osm(out)
        roles = Counter(s.role for s in layout.shapes)
        print(f"Wrote {out}")
        print(f"  anchor={layout.anchor}  shapes={len(layout.shapes)}")
        for r in sorted(roles):
            print(f"    {r:<22} {roles[r]}")

    if not multi:
        # Single-tile: anchor tile IS the build tile — whole-airport build
        # already uses the correct DEM (unchanged behaviour).
        out = args.out or f"/tmp/{base}.osm"
        _emit(build_airport_pavement(
            args.icao, args.xplane, compute_elevations=compute), out)
        return

    from auto_patch.elevation import _load_airport_dem
    for (tlat, tlon) in tiles:
        dem = _load_airport_dem(tlat + 0.5, tlon + 0.5)
        layout = build_airport_pavement(
            args.icao, args.xplane, compute_elevations=compute,
            tile_dem=dem, current_tile_lat=tlat, current_tile_lon=tlon)
        if not layout.shapes:
            print(f"  (tile {tlat:+d}{tlon:+d}: no pavement — skipped)")
            continue
        out = os.path.join(out_dir, f"{base}_tile{tlat:+d}{tlon:+d}.osm")
        _emit(layout, out)


if __name__ == "__main__":
    main()
