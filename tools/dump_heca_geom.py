"""Build HECA once and pickle runway + pavement geometry for offline
shoulder analysis (so we stop paying the 3-4 min build per question).

Run with O4_NO_SHOULDER_WIDEN=1 for the baseline (pre-widen) geometry.
Writes /tmp/heca_geom.pkl: list of (role, ref, wkb) for every shape,
plus the 3 apt.dat runway records (endpoints + width) for clean axes.
"""
import os
import pickle
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.normpath(os.path.join(_HERE, "..", "src")),
          os.path.normpath(os.path.join(_HERE, "..")),
          os.path.join(_HERE, "..", "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from conftest import xplane_root
from auto_patch import apt_dat_reader as APR
from auto_patch.layout import _projection, _airport_anchor
from auto_patch.osm_load import _pick_best_apt_dat_against_osm
from auto_patch.pipeline import build_airport_pavement

ICAO = sys.argv[1] if len(sys.argv) > 1 else "HECA"

layout = build_airport_pavement(ICAO, xplane_root(), compute_elevations=True)

shapes = []
for s in layout.shapes:
    if s.polygon is None or s.polygon.is_empty:
        continue
    shapes.append((s.role, s.ref, s.polygon.wkb))

apt_path = _pick_best_apt_dat_against_osm(xplane_root(), ICAO)
apt = APR.load_airport(apt_path, ICAO)
anchor = _airport_anchor(apt)
to_m = _projection(anchor)
runways = []
for r in apt.runways:
    ax, ay = to_m(r.lon_a, r.lat_a)
    bx, by = to_m(r.lon_b, r.lat_b)
    runways.append((f"{r.desig_a}/{r.desig_b}", ax, ay, bx, by, r.width_m))

with open("/tmp/heca_geom.pkl", "wb") as f:
    pickle.dump({"shapes": shapes, "runways": runways,
                 "widened": not os.environ.get("O4_NO_SHOULDER_WIDEN")}, f)
print(f"{ICAO}: dumped {len(shapes)} shapes, {len(runways)} apt runways "
      f"(widened={not os.environ.get('O4_NO_SHOULDER_WIDEN')}) "
      f"to /tmp/heca_geom.pkl")
layout.to_osm("/tmp/heca_baseline.osm")
print("wrote /tmp/heca_baseline.osm")
