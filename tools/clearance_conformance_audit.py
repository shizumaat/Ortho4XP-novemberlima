"""Measure whether clearance CUTS actually protect anything (conformance).

The spike audit (``clearance_spike_audit.py``) measures UNCOVERED terrain:
a spot beside pavement, above it, with no emitted shape over it.  But a
clearance cut that RIDES THE DEM reads as "covered" while protecting
nothing — its surface sits at natural ground, so an obstruction inside
the band still pokes through.  The part-30f outer-edge lift
(``max(ceiling, DEM)`` in ``_build_graded_strips``, reverted part 30k)
tilted whole cut surfaces up to the DEM; the spike audit cannot see that
regression — this audit measures it directly.

For every LATERAL/SURFACE clearance-cut vertex (roles ``*_clearance``,
ref ``surface_clearance`` — NOT the runway-end skirt, which is a FILL and
legitimately rides terrain) it computes the EXPECTED CEILING at that
vertex:

    ceiling = nearest_airside_pavement_edge_alt + threshold

(the lateral strip is a FLAT shadow, ``CLEARANCE_LATERAL_MAX_SLOPE == 0``,
so there is no ramp term for lateral cuts) and reports

    excess = vertex_alt - min(ceiling, DEM)

A vertex whose emitted altitude exceeds ``min(ceiling, DEM)`` by more than
``CONFORM_TOL_M`` (0.5 m) is DEM-RIDING: the cut surface there sits above
the protective ceiling, so it caps nothing.

Two numbers are reported:
  * FLAT-flagged — against the flat ceiling.  The RESA end-caps ramp
    lawfully at ``RUNWAY_END_RESA_MAX_SLOPE`` (5 %) and merge into the
    same ``surface_clearance`` region, so a bounded set of lawful ramp
    vertices flags here (HECA pre-30e: 36 of its 40).  Comparable across
    builds of the same airport — the primary A/B regression metric.
  * RAMP-ALLOWED-flagged — against ``ceiling + 5 % × distance``, the most
    generous lawful surface any cut regime may emit.  A vertex above even
    this is unconditionally ineffective (HECA pre-30e: 4; part-30f HEAD:
    339 — the regression this tool was built to catch).

Usage:
    venv/bin/python tools/clearance_conformance_audit.py /tmp/HECA_x.osm [lat lon]

The optional lat/lon pin the DEM tile (defaults to the OSM centroid tile).
Requires the local Elevation_data for that tile.
"""
import math
import os
import sys
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

CONFORM_TOL_M = 0.5          # excess above min(ceiling, DEM) that flags
THRESHOLD_M = 1.0            # CLEARANCE_OBSTRUCTION_THRESHOLD_M (all roles)
RAMP_SLOPE = 0.05            # RUNWAY_END_RESA_MAX_SLOPE (lawful max ramp)
CLUSTER_M = 25.0

# Roles the clearance builder emits.  We audit only the LATERAL/SURFACE
# cuts, whose ref after finalize is ``surface_clearance``.  The runway-end
# skirt (ref runway_end_skirt) is a FILL — excluded by ref below.
CLEARANCE_ROLES = {"runway_clearance", "taxiway_clearance"}
LATERAL_REF = "surface_clearance"

# Airside pavement whose edge altitude sets the ceiling.
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

    def to_xy(r):
        return ((nodes[r][1] - lon0) * mlon, (nodes[r][0] - lat0) * mlat)

    from shapely.geometry import Point, LineString
    from shapely.strtree import STRtree

    pav_edges = []           # LineString of airside pavement ring edges
    pav_edge_alts = []       # (a0, a1) endpoint alts per edge
    cut_verts = []           # (x, y, alt) for lateral clearance-cut vertices
    for w in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        role = tags.get("role", "")
        ref = tags.get("ref", "")
        refs = [nd.get("ref") for nd in w.findall("nd")]
        pts = [to_xy(r) for r in refs if r in nodes]
        if len(pts) < 2:
            continue
        if role in AIRSIDE_ROLES:
            for i in range(len(pts) - 1):
                r0, r1 = refs[i], refs[i + 1]
                a0, a1 = node_alt.get(r0), node_alt.get(r1)
                if a0 is None or a1 is None:
                    continue
                if math.dist(pts[i], pts[i + 1]) < 0.5:
                    continue
                pav_edges.append(LineString([pts[i], pts[i + 1]]))
                pav_edge_alts.append((a0, a1))
        elif role in CLEARANCE_ROLES and ref == LATERAL_REF:
            seen = set()
            for r, p in zip(refs, pts):
                a = node_alt.get(r)
                if a is None or r in seen:
                    continue
                seen.add(r)
                cut_verts.append((p[0], p[1], a))

    if not pav_edges:
        print("no airside pavement edges found; aborting")
        return 2
    if not cut_verts:
        print("no lateral clearance-cut (surface_clearance) vertices found")
        return 0

    edge_tree = STRtree(pav_edges)

    from auto_patch.elevation import _load_airport_dem, _sample_dem
    tl, tlon = int(math.floor(lat0)), int(math.floor(lon0))
    dem = _load_airport_dem(tl + 0.5, tlon + 0.5)
    if dem is None:
        print("no DEM available; aborting")
        return 2

    def ceiling_at(x, y):
        """(flat ceiling, distance to pavement edge) at a cut vertex."""
        pt = Point(x, y)
        res = edge_tree.nearest(pt)
        # STRtree.nearest returns an index (shapely 2) or a geometry (1.x).
        try:
            j = int(res)
            edge = pav_edges[j]
        except (TypeError, ValueError):
            edge = res
            j = pav_edges.index(edge)
        a0, a1 = pav_edge_alts[j]
        # interpolate the edge altitude at the projection of pt
        L = edge.length
        if L < 1e-6:
            base = a0
        else:
            t = max(0.0, min(1.0, edge.project(pt) / L))
            base = a0 + t * (a1 - a0)
        return base + THRESHOLD_M, edge.distance(pt)

    flags = []               # (excess, lat, lon, alt, ceiling, dem)
    n_flat = 0
    n_ramp = 0
    excesses = []
    for (x, y, alt) in cut_verts:
        lat = lat0 + y / mlat
        lon = lon0 + x / mlon
        dd = _sample_dem(dem, tl, tlon, lat, lon)
        ceil, dist = ceiling_at(x, y)
        target = ceil if dd is None else min(ceil, dd)
        excess = alt - target
        excesses.append(excess)
        ramp_ceil = ceil + RAMP_SLOPE * dist
        ramp_target = ramp_ceil if dd is None else min(ramp_ceil, dd)
        if alt - ramp_target > CONFORM_TOL_M:
            n_ramp += 1
        if excess > CONFORM_TOL_M:
            n_flat += 1
            flags.append((excess, lat, lon, alt, ceil,
                          dd if dd is not None else float("nan")))

    # cluster the flagged (DEM-riding) vertices
    flags.sort(reverse=True)
    clusters = []
    for f in flags:
        for c in clusters:
            if (abs(f[1] - c[0][1]) * mlat <= CLUSTER_M
                    and abs(f[2] - c[0][2]) * mlon <= CLUSTER_M):
                c.append(f)
                break
        else:
            clusters.append([f])

    total = len(cut_verts)
    mean_ex = sum(excesses) / total if total else 0.0
    print(f"{n_flat}/{total} lateral clearance-cut vertices DEM-RIDING "
          f"(alt > min(flat ceiling, DEM) + {CONFORM_TOL_M} m) in "
          f"{len(clusters)} cluster(s).  mean excess {mean_ex:+.2f} m.  "
          f"RAMP-ALLOWED (5% x dist) flagged: {n_ramp}.")
    for c in clusters[:25]:
        e, lat, lon, alt, ceil, dem_v = c[0]
        print(f"  +{e:5.2f} m at {lat:.6f},{lon:.6f}  "
              f"(alt={alt:.1f} ceiling={ceil:.1f} dem={dem_v:.1f}; "
              f"{len(c)} vert(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
