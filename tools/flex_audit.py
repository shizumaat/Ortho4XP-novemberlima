"""FLEX AUDIT — where the runway flexed, are the feeding taxiways at max
grade?  (FLEX-LAST law: the runway moves only when taxiways are at max
cap, by the minimum — docs/runway_flex_plan.md.)

Compares a flex-ON patch against a flex-OFF patch of the same airport:

1. Flex displacement map: runway-family node values ON vs OFF, matched
   by rounded lat/lon (geometry is flex-independent).
2. For each flexed cluster (|d| >= 0.10 m, coarse ~100 m bins): the taxi
   axes passing within 60 m (from the ON patch's exact-axes sidecar);
   for each, the max emitted grade along the axis within 200 m of the
   spot vs the axis cap.  The least-slack axis is the binding one — real
   positive slack at a big flex means the runway moved before the taxi
   network was exhausted (a FLEX-LAST violation).

Usage:
    venv/bin/python tools/flex_audit.py FLEX_ON.osm FLEX_OFF.osm

The ON patch needs its ``.axes.json`` sidecar (built with
O4_LOG_VERBOSITY=1 — tools/full_airport_build.py does this).

Found the 2026-07-06 HECA over-flex: 17.8 m one-sided profile drops
(sequential rounds let the first runway absorb the whole inter-runway
deficit) and a 16.6 m flex whose binding taxi axis had +0.45% slack.
"""
import json
import math
import re
import sys
from collections import defaultdict

RUNWAY_ROLES = {"runway", "runway_crossing"}
METERS_PER_DEG_LAT = 111320.0


def load(path):
    """(runway node values keyed by rounded lat/lon, all nodes)."""
    nodes = {}
    current_node = None
    node_re = re.compile(
        r"<node id='([-\d]+)'[^>]*lat='([-\d.]+)' lon='([-\d.]+)'")
    alt_re = re.compile(r"<tag k='alt_abs' v='([-\d.]+)'")
    with open(path) as fh:
        for line in fh:
            m = node_re.search(line)
            if m:
                nodes[m.group(1)] = (float(m.group(2)),
                                     float(m.group(3)), None)
                current_node = m.group(1) if "/>" not in line else None
                continue
            if current_node is not None:
                a = alt_re.search(line)
                if a:
                    lat, lon, _ = nodes[current_node]
                    nodes[current_node] = (lat, lon, a.group(1))
                if "</node>" in line:
                    current_node = None
    text = open(path).read()
    runway_values = {}
    for m in re.finditer(r"<way id='([-\d]+)'.*?</way>", text, re.S):
        tags = dict(re.findall(r"<tag k='([^']*)' v='([^']*)'", m.group(0)))
        if tags.get("role") not in RUNWAY_ROLES:
            continue
        for nid in re.findall(r"<nd ref='([-\d]+)'", m.group(0)):
            lat, lon, alt = nodes.get(nid, (None, None, None))
            if lat is None or alt is None:
                continue
            runway_values[(round(lat, 6), round(lon, 6))] = float(alt)
    return runway_values, nodes


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    on_path, off_path = sys.argv[1], sys.argv[2]
    on_values, on_nodes = load(on_path)
    off_values, _ = load(off_path)

    moved = []
    for key, value in on_values.items():
        baseline = off_values.get(key)
        if baseline is not None and abs(value - baseline) >= 0.10:
            moved.append((key[0], key[1], value - baseline))
    print(f"flex displacement: {len(moved)} runway node(s) moved "
          f">= 0.10 m (of {len(on_values)} matched)")
    if moved:
        worst = max(moved, key=lambda t: abs(t[2]))
        print(f"worst displacement: {worst[2]:+.2f} m at "
              f"({worst[0]:.6f},{worst[1]:.6f})")

    sidecar = json.load(open(on_path + ".axes.json"))
    axes = sidecar.get("axes") or sidecar.get("taxi_axes") or []
    mean_lat = (sum(k[0] for k in on_values) / len(on_values)
                if on_values else 0.0)
    meters_per_deg_lon = (METERS_PER_DEG_LAT
                          * math.cos(math.radians(mean_lat)))

    grid = defaultdict(list)
    for nid, (lat, lon, alt) in on_nodes.items():
        if alt is None:
            continue
        grid[(int(lat * 2000), int(lon * 2000))].append(
            (lat, lon, float(alt)))

    def value_at(lat, lon):
        best = None
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                for (nlat, nlon, alt) in grid[(int(lat * 2000) + ox,
                                               int(lon * 2000) + oy)]:
                    d = math.hypot((nlat - lat) * METERS_PER_DEG_LAT,
                                   (nlon - lon) * meters_per_deg_lon)
                    if d < 1.0 and (best is None or d < best[0]):
                        best = (d, alt)
        return best[1] if best else None

    clusters = {}
    for (lat, lon, d) in moved:
        key = (int(lat * 1000), int(lon * 1000))
        if key not in clusters or abs(d) > abs(clusters[key][2]):
            clusters[key] = (lat, lon, d)
    print(f"{len(clusters)} flexed cluster(s); auditing worst 12 by |d|:")

    def audit_spot(spot_lat, spot_lon):
        findings = []
        for entry in axes:
            points, caps = entry[0], entry[1]
            near = any(
                math.hypot((p[0] - spot_lat) * METERS_PER_DEG_LAT,
                           (p[1] - spot_lon) * meters_per_deg_lon) < 60
                for p in points[::max(1, len(points) // 20)])
            if not near:
                continue
            worst_grade = 0.0
            cap_at_worst = None
            for i in range(len(points) - 1):
                (a_lat, a_lon), (b_lat, b_lon) = points[i], points[i + 1]
                mid_d = math.hypot(
                    ((a_lat + b_lat) / 2 - spot_lat) * METERS_PER_DEG_LAT,
                    ((a_lon + b_lon) / 2 - spot_lon) * meters_per_deg_lon)
                if mid_d > 200:
                    continue
                va, vb = value_at(a_lat, a_lon), value_at(b_lat, b_lon)
                if va is None or vb is None:
                    continue
                seg = math.hypot((a_lat - b_lat) * METERS_PER_DEG_LAT,
                                 (a_lon - b_lon) * meters_per_deg_lon)
                if seg < 2:
                    continue
                grade = abs(va - vb) / seg
                if isinstance(caps, (int, float)):
                    cap = float(caps)
                elif caps:
                    cap = caps[min(i, len(caps) - 1)]
                else:
                    cap = None
                if grade > worst_grade:
                    worst_grade = grade
                    cap_at_worst = cap
            if cap_at_worst is not None:
                findings.append((worst_grade, cap_at_worst))
        return findings

    for (lat, lon, d) in sorted(clusters.values(),
                                key=lambda t: -abs(t[2]))[:12]:
        findings = audit_spot(lat, lon)
        if not findings:
            print(f"  ({lat:.6f},{lon:.6f}) flex={d:+.2f} m — "
                  f"no taxi axis nearby")
            continue
        findings.sort(key=lambda t: (t[1] - t[0]))
        grade, cap = findings[0]
        slack = cap - grade
        print(f"  ({lat:.6f},{lon:.6f}) flex={d:+.2f} m — binding taxi "
              f"axis max grade {grade*100:.2f}% of cap {cap*100:.2f}% "
              f"(slack {slack*100:+.2f}%)"
              + ("   << TAXI NOT AT CAP" if slack > 0.003 else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
