"""Find terrain SPIKES next to pavement that no clearance cut covers.

User report (2026-07-07): several spots at HECA show small terrain
spikes right beside pavement — the lateral/surface clearance passes
miss them.  This audit turns "several spots" into coordinates:

For every airside pavement ring node in an emitted patch, sample the
smoothed DEM at a few outward offsets inside the clearance-relevant
band.  A sample that sits more than ``SPIKE_MIN_M`` above the pavement
altitude AND is not covered by ANY emitted shape (clearance cuts,
boundary ribbon, other pavement, groundside...) is a spike candidate.
Candidates are clustered (~25 m) and reported worst-first.

Usage:
    venv/bin/python tools/clearance_spike_audit.py /tmp/HECA_x.osm [lat lon]

The optional lat/lon pin the DEM tile (defaults to the OSM's centroid
tile).  Requires the local Elevation_data for that tile.
"""
import math
import os
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

SPIKE_MIN_M = 0.8       # DEM this far above pavement = spike
PROBE_OFFSETS_M = (3.0, 6.0, 10.0, 15.0)
CLUSTER_M = 25.0
AIRSIDE_ROLES = {
    "runway", "runway_crossing", "primary_parallel", "secondary_parallel",
    "stub", "cross_connector", "junction", "apron", "service_road",
    "service_junction", "taxiway",
}


def main() -> int:
    path = sys.argv[1]
    tree = ET.parse(path)
    root = tree.getroot()
    nodes = {}
    node_alt = {}
    for n in root.iter("node"):
        nid = n.get("id")
        nodes[nid] = (float(n.get("lat")), float(n.get("lon")))
        for t in n.findall("tag"):
            if t.get("k") == "alt_abs":
                node_alt[nid] = float(t.get("v"))
    lat0 = sum(v[0] for v in nodes.values()) / len(nodes)
    lon0 = sum(v[1] for v in nodes.values()) / len(nodes)
    if len(sys.argv) >= 4:
        lat0, lon0 = float(sys.argv[2]), float(sys.argv[3])
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))

    from shapely.geometry import Point, Polygon
    from shapely.ops import unary_union
    from shapely.prepared import prep

    all_polys = []          # every emitted shape (coverage = "handled")
    airside = []            # (role, ring_xy, per-node alts)
    for w in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        role = tags.get("role", "")
        refs = [nd.get("ref") for nd in w.findall("nd")]
        pts = [((nodes[r][1] - lon0) * mlon, (nodes[r][0] - lat0) * mlat)
               for r in refs if r in nodes]
        if len(pts) < 4:
            continue
        try:
            poly = Polygon(pts).buffer(0)
        except Exception:
            continue
        if poly.is_empty:
            continue
        all_polys.append(poly)
        if role in AIRSIDE_ROLES:
            alts = [node_alt.get(r) for r in refs if r in nodes]
            airside.append((role, pts, alts))
    covered = prep(unary_union(all_polys).buffer(0.5))

    from auto_patch.elevation import _load_airport_dem, _sample_dem
    tl, tlon = int(math.floor(lat0)), int(math.floor(lon0))
    dem = _load_airport_dem(tl + 0.5, tlon + 0.5)
    if dem is None:
        print("no DEM available; aborting")
        return 2

    spikes = []             # (excess, lat, lon, role, pav_alt, dem_alt)
    for role, pts, alts in airside:
        n = len(pts)
        for i in range(n - 1):
            a = alts[i] if i < len(alts) else None
            if a is None:
                continue
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % (n - 1)]
            ex, ey = x2 - x1, y2 - y1
            el = math.hypot(ex, ey)
            if el < 1.0:
                continue
            # outward normal (either side; coverage test filters inward)
            nx, ny = ey / el, -ex / el
            for sign in (1.0, -1.0):
                for off in PROBE_OFFSETS_M:
                    px = x1 + sign * nx * off
                    py = y1 + sign * ny * off
                    if covered.contains(Point(px, py)):
                        continue
                    lat = lat0 + py / mlat
                    lon = lon0 + px / mlon
                    v = _sample_dem(dem, tl, tlon, lat, lon)
                    if v is None:
                        continue
                    excess = v - a
                    if excess >= SPIKE_MIN_M:
                        spikes.append((excess, lat, lon, role, a, v))
                    break       # first uncovered offset only, per side

    # cluster
    spikes.sort(reverse=True)
    clusters = []
    for s in spikes:
        for c in clusters:
            if (abs(s[1] - c[0][1]) * mlat <= CLUSTER_M
                    and abs(s[2] - c[0][2]) * mlon <= CLUSTER_M):
                c.append(s)
                break
        else:
            clusters.append([s])
    print(f"{len(spikes)} spike sample(s) in {len(clusters)} cluster(s) "
          f"(DEM {SPIKE_MIN_M}+ m above adjacent pavement, uncovered by "
          f"any emitted shape):")
    for c in clusters[:25]:
        e, lat, lon, role, a, v = c[0]
        print(f"  +{e:5.2f} m at {lat:.6f},{lon:.6f}  ({role} pav={a:.1f} "
              f"dem={v:.1f}; {len(c)} sample(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
