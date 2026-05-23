"""Build SPLP, write the patch OSM, and run the grade audit. Reports the
taxi-profile retarget count + worst within-shape grades.

Run: venv/bin/python tools/verify_splp_grade.py [ICAO]
"""
from __future__ import annotations
import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from pathlib import Path  # noqa: E402
from conftest import xplane_root  # noqa: E402
from auto_patch.pipeline import build_airport_pavement  # noqa: E402
import check_grade  # noqa: E402

ICAO = sys.argv[1] if len(sys.argv) > 1 else "SPLP"
layout = build_airport_pavement(ICAO, xplane_root(), compute_elevations=True)

out = os.path.join(tempfile.gettempdir(), f"{ICAO}_verify.osm")
layout.to_osm(out)
print(f"\nwrote {out}")

within, cross, steps = check_grade.run_checks(
    Path(out), max_grade_pct=1.5, proximity_m=1.0,
    edge_search_m=5.0, edge_step_m=0.5, top_n=8)
print("\n=== check_grade summary ===")
print(f"within-shape violations: {len(within)}")
print(f"cross-shape violations:  {len(cross)}")
print(f"edge/mid-edge steps>0.5m: {len(steps)}")
if within:
    print("\nworst within-shape:")
    for v in within[:8]:
        print(f"  {v.grade_pct:6.2f}% over {v.distance_m:5.1f}m at {v.pt_a}")
