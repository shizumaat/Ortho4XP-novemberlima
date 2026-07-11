"""Full airport build → patch OSM + axes sidecar → law-true check_grade run.

The standard lab loop: build one airport exactly the way the pipeline
ships it, emit ``/tmp/<ICAO>_<suffix>.osm`` (with the axes sidecar the
law-true validator needs), then run ``tools/check_grade.py`` on it.

Usage:
    venv/bin/python tools/full_airport_build.py ICAO [outsuffix]

Env:
    O4_LOG_VERBOSITY=1 is set here so ``to_osm`` writes the axes sidecar.
    Grade-law gates (O4_RUNWAY_FLEX=0, O4_STEP_DEBUG=1, ...) pass through.

(Promoted from the /tmp/spjc_lab scratch script, user 2026-07-06:
persistent tools live in tools/, /tmp is temporary by nature.)
"""
import os
import sys
import time

os.environ.setdefault("O4_LOG_VERBOSITY", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests"),
             os.path.join(ROOT, "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)

icao = sys.argv[1] if len(sys.argv) > 1 else "SPJC"
suffix = sys.argv[2] if len(sys.argv) > 2 else "dev"

from conftest import xplane_root                          # noqa: E402
from auto_patch.pipeline import build_airport_pavement    # noqa: E402

t0 = time.time()
layout = build_airport_pavement(icao, xplane_root(), compute_elevations=True)
t1 = time.time()
out = f"/tmp/{icao}_{suffix}.osm"
layout.to_osm(out)
source_note = ""
try:
    import re
    match = re.search(r"o4_apt_dat_mtime='([^']*)'", open(out).read(20000))
    source_note = f" [apt_mtime={match.group(1)}]" if match else ""
except OSError:
    pass
print(f"BUILD {icao} {t1 - t0:.1f}s -> {out}{source_note}")

import subprocess                                          # noqa: E402
result = subprocess.run(
    [sys.executable,
     os.path.join(ROOT, "tools/check_grade.py"), out],
    capture_output=True, text=True)
print(result.stdout)
if result.returncode not in (0, 1):
    print(result.stderr[-2000:])
