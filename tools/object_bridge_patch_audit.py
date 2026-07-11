"""Patch-level audit of Feature B / portal-pair terrain pieces.

The fast iteration gate for the object-terrain work (user 5-minute rule):
runs on a written ``*_auto.patch.osm`` in seconds — no mesh bake — and
asserts the three invariants the 2026-07-10 KBNA troubleshooting session
established:

1.  **No approach-versus-approach overlap.**  ``object_bridge_approach``
    rects are sloped (two-corner altitude semantics) and can never be
    clipped downstream, so any pairwise overlap above the noise floor is
    an emitter bug (the double-bridge / twin-carriageway class).
2.  **No legacy tunnel pieces on classifier-owned crossings.**  A
    ``ref='tunnel_ramp'`` piece within the ownership radius of a Feature
    B plate (``object_bridge_corridor`` / ``object_bridge_causeway`` /
    ``object_tunnel_portal_mouth``) means the legacy OSM machinery fired
    on a crossing Feature B owns — its DEM-referenced altitudes fight
    the hard-pinned plates (measured KBNA: strays at 173.9-177.2 m over
    the 167.0 m taxiway-L plates).
3.  **Plate report.**  Every plate's ref, elevation and centroid, so a
    reader can eyeball the law values (Donelson taxiway-L expects
    167.00; portal mouths expect the ROAD grade, not embankment top).

Usage:
    venv/bin/python tools/object_bridge_patch_audit.py PATCH.osm

Exit code 0 = all invariants hold; 1 = findings (printed).
"""
from __future__ import annotations

import math
import re
import sys

APPROACH_REF = "object_bridge_approach"
PLATE_REFS = (
    "object_bridge_corridor",
    "object_bridge_causeway",
    "object_tunnel_portal_mouth",
)
LEGACY_RAMP_REF = "tunnel_ramp"
OWNERSHIP_RADIUS_M = 100.0
OVERLAP_NOISE_M2 = 0.5


def _parse_patch(path):
    text = open(path, encoding="utf-8").read()
    nodes = {}
    for match in re.finditer(
            r"<node id='(-?\d+)'(.*?)(?:/>|</node>)", text, re.S):
        node_id, body = match.group(1), match.group(2)
        latitude = re.search(r"lat='([-\d.]+)'", body)
        longitude = re.search(r"lon='([-\d.]+)'", body)
        altitude = re.search(r"<tag k='alt_abs' v='([-\d.]+)'", body)
        nodes[node_id] = (
            float(latitude.group(1)) if latitude else None,
            float(longitude.group(1)) if longitude else None,
            float(altitude.group(1)) if altitude else None,
        )
    ways = []
    for match in re.finditer(r"<way id='(-?\d+)'.*?>(.*?)</way>", text, re.S):
        body = match.group(2)
        tags = dict(re.findall(r"<tag k='([^']+)' v='([^']*)'", body))
        node_refs = re.findall(r"<nd ref='(-?\d+)'", body)
        ways.append((match.group(1), tags, node_refs))
    return nodes, ways


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    patch_path = sys.argv[1]
    nodes, ways = _parse_patch(patch_path)

    from shapely.geometry import Polygon
    from shapely.strtree import STRtree

    latitudes = [v[0] for v in nodes.values() if v[0] is not None]
    origin_latitude = sum(latitudes) / len(latitudes) if latitudes else 0.0
    meters_per_degree_longitude = 111320.0 * math.cos(
        math.radians(origin_latitude))

    def _polygon(node_refs):
        ring = []
        for node_ref in node_refs:
            entry = nodes.get(node_ref)
            if entry is None or entry[0] is None:
                continue
            ring.append((entry[1] * meters_per_degree_longitude,
                         entry[0] * 111132.0))
        if len(ring) < 3:
            return None
        try:
            polygon = Polygon(ring)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            return polygon if (polygon.geom_type == "Polygon"
                               and not polygon.is_empty) else None
        except Exception:
            return None

    approaches = []
    legacy_ramps = []
    plates = []
    for way_id, tags, node_refs in ways:
        reference = tags.get("ref", "")
        polygon = None
        if reference in (APPROACH_REF, LEGACY_RAMP_REF) + PLATE_REFS:
            polygon = _polygon(node_refs)
            if polygon is None:
                continue
        altitudes = [nodes[n][2] for n in node_refs
                     if nodes.get(n) and nodes[n][2] is not None]
        if reference == APPROACH_REF:
            approaches.append((tags.get("shapeID", way_id), polygon))
        elif reference == LEGACY_RAMP_REF:
            legacy_ramps.append((tags.get("shapeID", way_id), polygon))
        elif reference in PLATE_REFS:
            plates.append((tags.get("shapeID", way_id), reference,
                           polygon, altitudes))

    findings = 0

    # ── 3. Plate report (informational) ──
    print(f"plates: {len(plates)}")
    for shape_id, reference, polygon, altitudes in plates:
        centroid = polygon.centroid
        altitude_text = (
            f"{min(altitudes):.2f}..{max(altitudes):.2f}"
            if altitudes else "?")
        print(f"  shape {shape_id:>5} {reference:28s} alt {altitude_text}"
              f"  @{centroid.y / 111132.0:.5f},"
              f"{centroid.x / meters_per_degree_longitude:.5f}")

    # ── 1. Approach-versus-approach overlap ──
    overlap_count = 0
    if len(approaches) >= 2:
        tree = STRtree([polygon for _sid, polygon in approaches])
        for index_a, (shape_a, polygon_a) in enumerate(approaches):
            for tree_index in tree.query(polygon_a):
                if tree_index <= index_a:
                    continue
                shape_b, polygon_b = approaches[tree_index]
                try:
                    area = polygon_a.intersection(polygon_b).area
                except Exception:
                    continue
                if area > OVERLAP_NOISE_M2:
                    overlap_count += 1
                    centroid = polygon_a.intersection(polygon_b).centroid
                    print(f"FINDING approach-overlap {area:.1f} m² "
                          f"shapes {shape_a}+{shape_b} "
                          f"@{centroid.y / 111132.0:.5f},"
                          f"{centroid.x / meters_per_degree_longitude:.5f}")
    print(f"approach pieces: {len(approaches)}, "
          f"approach-overlaps: {overlap_count}")
    findings += overlap_count

    # ── 2. Legacy pieces on owned crossings ──
    owned_count = 0
    for shape_id, polygon in legacy_ramps:
        for _plate_id, plate_reference, plate_polygon, _alt in plates:
            try:
                distance = polygon.distance(plate_polygon)
            except Exception:
                continue
            if distance <= OWNERSHIP_RADIUS_M:
                owned_count += 1
                centroid = polygon.centroid
                print(f"FINDING legacy-ramp-on-owned-crossing shape "
                      f"{shape_id} within {distance:.0f} m of "
                      f"{plate_reference} "
                      f"@{centroid.y / 111132.0:.5f},"
                      f"{centroid.x / meters_per_degree_longitude:.5f}")
                break
    print(f"legacy tunnel_ramp pieces: {len(legacy_ramps)}, "
          f"on owned crossings: {owned_count}")
    findings += owned_count

    print("PASS" if findings == 0 else f"FAIL ({findings} finding(s))")
    return 0 if findings == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
