"""Round-9 acceptance: INTERP_ALT (attr >= 8) coverage of the J/R trench
polygon and both abutment bands in the freshly baked Data+36-087.mesh,
plus the z landing check."""
import math
import re
import sys

sys.path.insert(0, "/Users/noah/Ortho4XP-object-terrain/src")
from shapely.geometry import LineString, Point, Polygon
from shapely.prepared import prep

ROOT = "/Users/noah/Ortho4XP-object-terrain"
patch_text = open(
    f"{ROOT}/Patches/+30-090/+36-087/KBNA_auto.patch.osm").read()
nodes = {m.group(1): (float(m.group(2)), float(m.group(3)))
         for m in re.finditer(
             r"<node id='(-?\d+)'[^>]*lat='([-0-9.]+)' lon='([-0-9.]+)'",
             patch_text)}
node_alt = {m.group(1): float(m.group(2)) for m in re.finditer(
    r"<node id='(-?\d+)'[^>]*>\s*<tag k='alt_abs' v='([-0-9.]+)'",
    patch_text)}

# The J/R trench ring (161.x) and the two J/R causeway rings (167.x).
trench_ring = None
causeway_rings = []
for way in re.findall(r"<way .*?</way>", patch_text, re.S):
    nds = re.findall(r"<nd ref='(-?\d+)'", way)
    values = [node_alt.get(n) for n in nds if n in node_alt]
    values = [v for v in values if v is not None]
    if not values:
        continue
    ring = [(nodes[n][1], nodes[n][0]) for n in nds if n in nodes]
    if "v='bridge_trench'" in way and abs(values[0] - 161.01) < 0.1:
        trench_ring = Polygon(ring)
    elif "v='bridge_causeway'" in way and abs(values[0] - 167.0) < 0.1:
        causeway_rings.append(Polygon(ring))
# Round 10: the road-exit cut splits each J/R causeway into flanks.
assert trench_ring is not None and len(causeway_rings) >= 2, \
    (trench_ring is not None, len(causeway_rings))

# Abutment bands: the trench-facing causeway lip strips — approximate as
# each causeway polygon intersected with the trench polygon buffered by
# the lip gap + capture band (covers the audit's 9-sample lines).
bands = [c.intersection(trench_ring.buffer(14.0 / 111320.0))
         for c in causeway_rings]

zones = [("trench", trench_ring)] + [
    (f"abutment-band-{i}", band) for i, band in enumerate(bands)]

with open(f"{ROOT}/Tiles/zOrtho4XP_+36-087/Data+36-087.mesh") as f:
    lines = iter(f)
    for line in lines:
        if line.startswith("Vertices"):
            break
    n_v = int(next(lines))
    verts = []
    for _ in range(n_v):
        p = next(lines).split()
        verts.append((float(p[0]), float(p[1]), float(p[2]) * 1e5))
    for line in lines:
        if line.strip().startswith("Triangles"):
            break
    n_t = int(next(lines))
    tris = []
    for _ in range(n_t):
        p = next(lines).split()
        tris.append((int(p[0]) - 1, int(p[1]) - 1, int(p[2]) - 1,
                     int(p[3])))

overall_pass = True
for name, zone in zones:
    if zone.is_empty:
        print(f"### {name}: EMPTY zone (geometry degenerated) — FAIL")
        overall_pass = False
        continue
    covered = prep(zone.buffer(-2e-6))  # stay off shared borders
    total = marked = 0
    z_bad = []
    for v1, v2, v3, attr in tris:
        cx = (verts[v1][0] + verts[v2][0] + verts[v3][0]) / 3
        cy = (verts[v1][1] + verts[v2][1] + verts[v3][1]) / 3
        if not covered.contains(Point(cx, cy)):
            continue
        total += 1
        if attr >= 8:
            marked += 1
        else:
            zmean = (verts[v1][2] + verts[v2][2] + verts[v3][2]) / 3
            z_bad.append(round(zmean, 2))
    fraction = marked / total if total else 0.0
    status = "PASS" if total and marked == total else "FAIL"
    if status == "FAIL":
        overall_pass = False
    print(f"### {name}: {marked}/{total} INTERP_ALT faces "
          f"({fraction * 100:.1f}%) -> {status}"
          + (f"  unmarked z: {z_bad[:8]}" if z_bad else ""))

# z landing: minimum z inside the trench must be the law floor.
z_in_trench = []
covered = prep(trench_ring.buffer(-2e-6))
for lon, lat, z in verts:
    if covered.contains(Point(lon, lat)):
        z_in_trench.append(z)
if z_in_trench:
    print(f"### trench z range in mesh: {min(z_in_trench):.2f}"
          f"..{max(z_in_trench):.2f} (law floor 161.01)")
print(f"### ROUND-9 ACCEPTANCE: {'PASS' if overall_pass else 'FAIL'}")
