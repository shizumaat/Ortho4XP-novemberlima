"""Patch-level crossing-influence-zone conformance check (Phase 1 of
docs/specs/crossing-terrain-ownership.md).

THE CONTRACT IT CHECKS (spec section 2 point 4, Phase-1 slice): nothing
OUTSIDE the crossing assembly may intersect the published influence zone.
Every consumer terrain writer — adjacent-ground bands (``graded_strip``),
runway-end skirts, legacy clearance strips, gap-fill spine faces — must
have been clipped/skipped against the zone at build time; this tool
re-verifies that on the EMITTED patch, so a regression in any consumer's
zone consult is caught at the patch level without a sim session (the
fast-iteration gate pattern of ``object_bridge_patch_audit.py``).

Crossing-owned pieces (``object_bridge_*`` / ``object_tunnel_portal_*`` /
``bridge_trench`` / ``bridge_causeway`` / ``tunnel_ramp`` refs and roles)
are exempt — the zone is exactly where they live.  Airside pavement,
buildings, boundary and groundside surfaces are also exempt: the zone
does not evict pavement or structures, only the terrain-drape writers.

INPUT: the patch ``.osm`` plus the zone dump the build wrote with
``O4_CROSSING_ZONE_DUMP=<path>`` (JSON ``{"anchor": [lat, lon],
"wkt": ...}`` from ``crossing_terrain.publish_crossing_influence_zones``).
The dump is in layout meters anchored at the layout anchor; patch nodes
are lat/lon and are projected with the same equirectangular formula
(``layout.ll_to_m``).

Usage:
    venv/bin/python tools/crossing_zone_conformance.py PATCH.osm ZONE.json
        [--tolerance-m2 1.0]

Exit code 0 = no writer intersects the zone beyond the noise tolerance;
1 = findings (printed per way, largest first); 2 = bad invocation.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys

_SRC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# Terrain-drape writers the zone evicts (way ref= prefixes / role tags).
CHECKED_REFS = (
    "adjacent_ground",       # graded_strip pieces carry ref adjacent_ground
    "graded_strip",
    "runway_end_skirt",
    "surface_clearance",
    "gap_fill",
)
# Pieces that legitimately live inside the zone (the crossing's own).
EXEMPT_REF_PREFIXES = (
    "object_bridge", "object_tunnel_portal", "tunnel_ramp",
    "bridge_trench", "bridge_causeway",
)


def _parse_patch(path):
    text = open(path, encoding="utf-8").read()
    nodes = {}
    for match in re.finditer(
            r"<node id='(-?\d+)'(.*?)(?:/>|</node>)", text, re.S):
        node_id, body = match.group(1), match.group(2)
        latitude = re.search(r"lat='([-\d.]+)'", body)
        longitude = re.search(r"lon='([-\d.]+)'", body)
        if latitude and longitude:
            nodes[node_id] = (float(latitude.group(1)),
                              float(longitude.group(1)))
    ways = []
    for match in re.finditer(r"<way id='(-?\d+)'.*?>(.*?)</way>", text, re.S):
        body = match.group(2)
        tags = dict(re.findall(r"<tag k='([^']+)' v='([^']*)'", body))
        node_refs = re.findall(r"<nd ref='(-?\d+)'", body)
        ways.append((match.group(1), tags, node_refs))
    return nodes, ways


def main() -> int:
    arguments = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(arguments) != 2:
        print(__doc__)
        return 2
    tolerance_m2 = 1.0
    for argument in sys.argv[1:]:
        if argument.startswith("--tolerance-m2"):
            tolerance_m2 = float(argument.split("=", 1)[1])
    patch_path, zone_path = arguments

    from shapely import wkt as shapely_wkt
    from shapely.geometry import Polygon

    dump = json.load(open(zone_path, encoding="utf-8"))
    anchor_latitude, anchor_longitude = dump["anchor"]
    zone = shapely_wkt.loads(dump["wkt"])
    if zone.is_empty:
        print("crossing-zone conformance: zone is EMPTY — nothing to check.")
        return 0

    # Same equirectangular projection as ``layout.ll_to_m``.
    from O4_Geo_Utils import earth_radius as R_EARTH
    cos0 = math.cos(math.radians(anchor_latitude))

    def _to_meters(latitude, longitude):
        return (math.radians(longitude - anchor_longitude) * R_EARTH * cos0,
                math.radians(latitude - anchor_latitude) * R_EARTH)

    nodes, ways = _parse_patch(patch_path)
    findings = []
    checked = 0
    for way_id, tags, node_refs in ways:
        ref = tags.get("ref", "")
        role = tags.get("role", "")
        marker = f"{ref} {role}"
        if any(marker.startswith(p) or ref.startswith(p) or
               role.startswith(p) for p in EXEMPT_REF_PREFIXES):
            continue
        if not any(k in marker for k in CHECKED_REFS):
            continue
        ring = [_to_meters(*nodes[n]) for n in node_refs if n in nodes]
        if len(ring) < 3:
            continue
        try:
            polygon = Polygon(ring)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            overlap = polygon.intersection(zone).area
        except Exception:
            continue
        checked += 1
        if overlap > tolerance_m2:
            centroid = polygon.centroid
            latitude = anchor_latitude + math.degrees(centroid.y / R_EARTH)
            longitude = anchor_longitude + math.degrees(
                centroid.x / (R_EARTH * cos0))
            findings.append((overlap, way_id, ref or role,
                             latitude, longitude))

    print(f"crossing-zone conformance: {checked} writer way(s) checked "
          f"against a {zone.area:.0f} m2 zone "
          f"(tolerance {tolerance_m2} m2).")
    if not findings:
        print("PASS — no terrain writer intersects the published zone.")
        return 0
    findings.sort(reverse=True)
    print(f"FAIL — {len(findings)} way(s) intersect the zone:")
    for overlap, way_id, kind, latitude, longitude in findings[:25]:
        print(f"  way {way_id} ({kind}): {overlap:.1f} m2 in-zone "
              f"near {latitude:.7f},{longitude:.7f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
