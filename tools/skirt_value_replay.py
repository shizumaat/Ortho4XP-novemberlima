"""Fast replay harness for runway-end skirt edge-grade valuation.

The elevation VALUES written at skirt weld-row corners are decided in
``PavementLayout.to_osm`` (the shared-node altitude consensus), NOT in
the ~3-minute ``build_airport_pavement`` geometry/solve.  So iterating on
a to_osm value fix by rebuilding the whole airport each time wastes
minutes per cycle.

This harness snapshots the fully built ``PavementLayout`` to a pickle
once (~3 min), then replays ONLY ``to_osm`` + the runway-end skirt
edge-grade law check (``check_grade._check_runway_end_skirt_edges``) on
every subsequent run (seconds).  Because ``to_osm`` is a method defined
in ``auto_patch.layout``, each fresh interpreter picks up the current
source, so edits to the consensus code are exercised on replay without a
rebuild.

Usage:
    # First run (or after a geometry/solver change) builds + caches:
    venv/bin/python tools/skirt_value_replay.py CYXY --rebuild

    # Subsequent runs replay the cached layout through to_osm (seconds):
    venv/bin/python tools/skirt_value_replay.py CYXY

The exit code is the number of skirt edge-grade violations (0 = pass),
so it doubles as a patch-level assert.
"""
from __future__ import annotations

import os
import pickle
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests"),
           os.path.join(ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("O4_LOG_VERBOSITY", "1")


def _build_layout(icao: str):
    os.environ["O4_AUTO_PATCH_REBUILD"] = "1"
    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement
    return build_airport_pavement(icao, xplane_root(),
                                  compute_elevations=True)


def main() -> int:
    icao = sys.argv[1] if len(sys.argv) > 1 else "CYXY"
    rebuild = "--rebuild" in sys.argv[2:]
    cache = Path(f"/tmp/{icao}_layout.pkl")

    if rebuild or not cache.exists():
        layout = _build_layout(icao)
        with cache.open("wb") as handle:
            pickle.dump(layout, handle)
        print(f"[replay] built + cached layout -> {cache}")
    else:
        with cache.open("rb") as handle:
            layout = pickle.load(handle)
        print(f"[replay] loaded cached layout <- {cache}")

    out = f"/tmp/{icao}_replay.osm"
    layout.to_osm(out)

    import math
    import check_grade as cg
    nodes, ways = cg._parse_osm(Path(out))
    lat0 = min(lat for lat, lon in nodes.values())
    # Reuse check_grade's own metric projection helper via a closure that
    # matches its internal ll_to_m (equirectangular about the patch).
    R = 6_378_137.0
    lats = [lat for lat, lon in nodes.values()]
    lons = [lon for lat, lon in nodes.values()]
    clat = math.radians(sum(lats) / len(lats))
    cos0 = math.cos(clat)

    def ll_to_m(lat, lon):
        return (math.radians(lon) * R * cos0, math.radians(lat) * R)

    vios = cg._check_runway_end_skirt_edges(ways, nodes, ll_to_m)
    print(f"[replay] RUNWAY-END SKIRT edge-grade violations: {len(vios)}")
    for v in vios:
        sid_a = v.way_a.tags.get("shapeID")
        print(f"    {v.grade_pct:6.2f}% (excess {v.excess_pct:+5.2f}%) "
              f"d={v.distance_m:6.2f}m |de|={v.de_m:5.2f}m  "
              f"skirt #{sid_a}  {v.elev_a:.1f} -> {v.elev_b:.1f}")
    return len(vios)


if __name__ == "__main__":
    sys.exit(main())
