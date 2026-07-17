"""Patch-level audit of Feature B / portal-pair terrain pieces.

The fast iteration gate for the object-terrain work (user 5-minute rule):
runs on a written ``*_auto.patch.osm`` in seconds — no mesh bake — and
asserts the six invariants the 2026-07-10 and 2026-07-15 KBNA
troubleshooting sessions established:

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
4.  **Chain continuity.**  ``object_bridge_approach`` pieces grouped
    into chains (proximity + heading) must be contiguous: the maximum
    facing-edge gap between consecutive pieces of a chain is < 1.0 m.
    Measured KBNA 2026-07-15: every-other-step holes at ~39 m spacing
    (4 NE / 2 SW) were filled by adjacent-ground bands, tearing the
    terrain along Donelson Pike.
5.  **Plate weld.**  Every ``object_bridge_corridor`` plate end that
    has an approach chain must SHARE at least two exact nodes
    (tolerance 0.05 m) with the nearest approach piece — verbatim
    shared coordinates are the repo's weld mechanism; anything else
    leaves an open seam (measured KBNA: 1.09 m seam at
    36.12202,-86.66612).
6.  **Portal collar coverage.**  For every
    ``object_tunnel_portal_mouth`` with a crown, the union of its
    ``object_tunnel_portal_collar`` pieces must span >= 95 % of the
    portal's back-side width — the extent of mouth+crown projected
    on the CROWN's long axis (the crown hugs the portal back, so its
    long axis follows the back edge; the crown→mouth centroid
    direction is skewed on diagonal-band portals).  Measured KBNA
    2026-07-15 defect C: one collar side truncated by up to 8 m,
    bare back-edge slivers at 36.11005,-86.68272 and
    36.11124,-86.68596.
7.  **Portal collar transition (no cliff).**  A
    ``object_tunnel_portal_collar`` is a TRANSITION ring, crown-high
    at the object-hidden inner face and DEM-low at the exposed rim,
    NOT a flat plate at crown altitude.  For every collar vertex, the
    terrain step to its nearest NON-crown emitted neighbour (mouth,
    approach, adjacent_ground, plate, or another collar part) within
    ``PORTAL_STEP_XY_M`` must not exceed ``PORTAL_STEP_MAX_M``, EXCEPT
    where the collar vertex lies on the object-hidden inner face —
    within ``PORTAL_HIDDEN_FACE_TOLERANCE_M`` of the crown polygon
    (the buried half of the object footprint), the face the portal
    facade covers, whose vertical meeting with the mouth is
    by-design.  Measured KBNA 2026-07-15: the flat collar plate
    stepped 7.76/7.09 m to the approach and 4.44 m to the
    adjacent_ground band along near-vertical stretched-texture walls
    flanking the portal objects.
8.  **Deck plate <-> pavement weld overlap.**  Aircraft must taxi
    SMOOTHLY onto the decks: for every ``object_bridge_corridor`` /
    ``object_bridge_causeway`` plate, each adjacent airside-pavement
    way (role junction/apron/runway/... within
    ``PLATE_PAVEMENT_ADJACENCY_M``) must OVERLAP the deck-weld system
    (the plates plus the ``object_bridge_deck_weld`` strips) by at
    least ``WELD_OVERLAP_MIN_DEPTH_M`` ring-depth along its fronting
    edge, and inside the overlap the pavement node altitudes must
    agree with the nearest deck-elevation piece (causeway or weld
    strip) to ``WELD_ALT_TOLERANCE_M``.  Measured KBNA 2026-07-15:
    the R8 pavement cut at the deck box against the trench plate
    inset 1.2 m left a 0.8-1.2 m ring of raw mesh diving 6 m to the
    trench floor at the taxi path (pavement 167.0 pinned, plate edge
    161.01 0.8-1.2 m away — the owner-visible gap onto the
    taxiway-L deck).
9.  **Portal outward-ramp width.**  A tunnel-portal object whose sides
    slant up into retaining walls has a footprint far wider than the
    DRIVABLE road that emerges from it, so its outward approach ramp
    must match the road, never the full footprint (user ruling
    2026-07-15, supersedes the 2026-07-14c "as wide as the mouth face"
    rule).  Every ``object_bridge_approach`` piece assigned to the
    nearest ``object_tunnel_portal_mouth`` (within
    ``PORTAL_ASSOCIATION_RADIUS_M``) must be no wider — measured
    PERPENDICULAR to travel (mouth→approach centroid) — than that
    mouth's face width (the long rotated-rectangle dimension of the
    mouth plate) PLUS ``PORTAL_RAMP_WIDTH_TOLERANCE_M``, AND, where the
    mapped OSM carriageway is resolvable (re-associated to the big-road
    cache by geometry, so a pre-fix build with no width plumbing is
    still judged), no wider than ``PORTAL_RAMP_MAPPED_WIDTH_FACTOR`` ×
    the mapped width.  Measured KBNA 2026-07-15 portal 1: an 84.3 m
    ramp over a 6-lane, 21 m road (84.3 > 21 x 1.5).
10. **Portal collar no front remnant.**  No
    ``object_tunnel_portal_collar`` part may sit on the ROAD side of
    the mouth face — the plane through the portal mouth's forward-most
    extent, perpendicular to the outward (mouth->road) direction
    inferred from the crown->mouth centroids.  Everything ahead of that
    face belongs to the mouth plate and the road approaches; a collar
    lobe out there is a free-standing vertical face (measured KBNA
    2026-07-16 round-8 baseline: a 34 m2 collar lobe 46.7 m in front of
    the west mouth @36.11196,-86.68564, flat at the crown elevation).
11. **Portal collar flank transition.**  A collar vertex that meets the
    low mouth plate vertically (terrain step > ``PORTAL_STEP_MAX_M`` to
    a mouth-plate vertex within ``PORTAL_STEP_XY_M``) is allowed ONLY
    within the object-hidden span — the mouth face's lateral half-width,
    which the portal facade covers.  A flank sticking out laterally
    BEYOND that span (its projection perpendicular to outward is outside
    the mouth's lateral extent) must transition per-vertex like the
    outer rim; a vertical meeting there is an exposed cliff (measured
    KBNA 2026-07-15 round-8: east 2.1-3.7 m and west 5.79-5.98 m steps
    onto the mouth plate beside the opening).

The invariants are numbered here, in ``_CHECK_HEADERS`` (printed as the
audit header), and in each check's section banner, so every check is
discoverable from either the source or a run.

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

# Invariant 4 (chain continuity): grouping and gap thresholds.  Pieces
# within CHAIN_PROXIMITY_M with aligned headings (within
# CHAIN_HEADING_TOLERANCE_DEG, connection vector along the axis) are one
# chain; consecutive pieces of a chain may face-gap at most
# CHAIN_MAX_FACING_GAP_M (measured KBNA 2026-07-15: ~39 m every-other-
# step holes, filled by adjacent-ground bands).
CHAIN_PROXIMITY_M = 45.0
CHAIN_HEADING_TOLERANCE_DEG = 30.0
CHAIN_MAX_FACING_GAP_M = 1.0

# Invariant 5 (plate weld): an approach chain ending within
# WELD_CHAIN_ATTACH_RADIUS_M of a corridor plate END must share at
# least WELD_MIN_SHARED_NODES exact nodes (WELD_NODE_TOLERANCE_M) with
# the plate — verbatim shared coordinates are the repo's weld
# mechanism (measured KBNA: a 1.09 m open seam at the trench inset).
WELD_CHAIN_ATTACH_RADIUS_M = 10.0
WELD_MIN_SHARED_NODES = 2
WELD_NODE_TOLERANCE_M = 0.05

# Invariant 6 (portal collar coverage): a mouth's crown/collar system
# is associated within this radius; the collar union must span at
# least this fraction of the portal (mouth+crown) back-side width.
PORTAL_ASSOCIATION_RADIUS_M = 60.0
PORTAL_COLLAR_MIN_COVERAGE = 0.95

CROWN_REF = "object_tunnel_portal_crown"
COLLAR_REF = "object_tunnel_portal_collar"

# Invariant 9 (portal outward-ramp width, user ruling 2026-07-15): a
# portal OUTWARD approach ramp must match the DRIVABLE road width, never
# the full portal footprint — a portal whose sides slant up into
# retaining walls flares far wider than the carriageway (measured KBNA
# 2026-07-15 portal 1: an 84.3 m ramp over a 6-lane, 21 m road).  Every
# ``object_bridge_approach`` piece associated with an
# ``object_tunnel_portal_mouth`` must be no wider (perpendicular to
# travel) than that mouth's face width AND, where the mapped OSM
# carriageway is resolvable, no wider than
# ``PORTAL_RAMP_MAPPED_WIDTH_FACTOR`` × the mapped width.
PORTAL_RAMP_WIDTH_TOLERANCE_M = 1.5    # weld overhang / curvature slack
PORTAL_RAMP_MAPPED_WIDTH_FACTOR = 1.5
# A big-road highway way within this distance of a portal mouth is the
# road the ramp follows (mirror of ``bridges.PORTAL_ROAD_ASSOCIATION_M``).
PORTAL_MAPPED_ROAD_ASSOCIATION_M = 15.0

# Invariant 8 (deck plate <-> pavement weld overlap): an airside
# pavement way within PLATE_PAVEMENT_ADJACENCY_M of a corridor/causeway
# plate must overlap the deck-weld system by WELD_OVERLAP_MIN_DEPTH_M
# ring-depth along at least WELD_FRONTAGE_COVERAGE_MIN of its fronting
# edge (the pavement-edge portion within WELD_FRONTAGE_BAND_M of the
# plate), with pavement node altitudes inside the overlap agreeing with
# the nearest deck-elevation piece to WELD_ALT_TOLERANCE_M.
DECK_WELD_REF = "object_bridge_deck_weld"
PLATE_PAVEMENT_ADJACENCY_M = 3.0
WELD_FRONTAGE_BAND_M = 2.0
WELD_FRONTAGE_MIN_LENGTH_M = 1.5
WELD_FRONTAGE_COVERAGE_MIN = 0.75
WELD_OVERLAP_MIN_DEPTH_M = 0.2
WELD_ALT_TOLERANCE_M = 0.05
WELD_ALT_NEIGHBOUR_RADIUS_M = 2.5
AIRSIDE_ROLE_TAGS = frozenset({
    "junction", "apron", "runway", "runway_crossing", "taxiway",
    "stub", "primary_parallel", "secondary_parallel", "cross_connector",
})

# Invariant 7 (portal collar transition): a collar vertex may not step
# more than PORTAL_STEP_MAX_M to its nearest non-crown neighbour within
# PORTAL_STEP_XY_M, unless it sits on the object-hidden inner face —
# within PORTAL_HIDDEN_FACE_TOLERANCE_M of the crown polygon (the buried
# half of the footprint the facade covers).  The collar-vs-CROWN meeting
# is never checked (both hold the crown elevation there by design).
PORTAL_STEP_MAX_M = 2.5
PORTAL_STEP_XY_M = 1.5
PORTAL_HIDDEN_FACE_TOLERANCE_M = 2.0
# Emitted pieces a collar must transition smoothly TO (crown excluded —
# it is the object-hidden inner face's own value).  Other collar parts
# are added dynamically so an inconsistent split is caught too.
PORTAL_COLLAR_NEIGHBOUR_REFS = (
    "object_tunnel_portal_mouth",
    "object_bridge_approach",
    "object_bridge_corridor",
    "object_bridge_causeway",
    "adjacent_ground",
)

# Invariant 10 (portal collar no front remnant): a collar part whose
# centroid projects forward of the mouth's front face by more than
# PORTAL_FRONT_REMNANT_TOL_M (outward = crown->mouth centroids) is on the
# road side of the mouth plane and must not exist.
PORTAL_FRONT_REMNANT_TOL_M = 2.0
# Invariant 11 (portal collar flank transition): a collar vertex outside
# the mouth's lateral span by more than PORTAL_FLANK_SPAN_TOL_M that
# steps > PORTAL_STEP_MAX_M to a mouth-plate vertex within
# PORTAL_STEP_XY_M is an exposed cliff (the object hides only the span).
PORTAL_FLANK_SPAN_TOL_M = 1.0

# Numbered index of every check, printed as the audit header so a run is
# self-documenting (round-8 discoverability requirement).
_CHECK_HEADERS = (
    "1. approach-vs-approach overlap",
    "2. legacy tunnel_ramp on classifier-owned crossings",
    "3. plate report (informational)",
    "4. approach chain continuity (no every-other-step holes)",
    "5. corridor plate <-> approach chain weld (shared nodes)",
    "6. portal collar coverage (>= 95% of back width)",
    "7. portal collar transition (no cliff to non-crown neighbour)",
    "8. deck plate <-> pavement weld overlap + coplanarity",
    "9. portal outward-ramp width (matches the drivable road)",
    "10. portal collar no front remnant (road side of the mouth face)",
    "11. portal collar flank transition (steps only within object span)",
)


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
    print("object-bridge patch audit — checks:")
    for header in _CHECK_HEADERS:
        print(f"  {header}")
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
    crowns = []
    collars = []
    for way_id, tags, node_refs in ways:
        reference = tags.get("ref", "")
        polygon = None
        if reference in ((APPROACH_REF, LEGACY_RAMP_REF, CROWN_REF,
                          COLLAR_REF) + PLATE_REFS):
            polygon = _polygon(node_refs)
            if polygon is None:
                continue
        altitudes = [nodes[n][2] for n in node_refs
                     if nodes.get(n) and nodes[n][2] is not None]
        if reference == APPROACH_REF:
            approaches.append((tags.get("shapeID", way_id), polygon,
                               node_refs))
        elif reference == LEGACY_RAMP_REF:
            legacy_ramps.append((tags.get("shapeID", way_id), polygon))
        elif reference == CROWN_REF:
            crowns.append((tags.get("shapeID", way_id), polygon))
        elif reference == COLLAR_REF:
            collars.append((tags.get("shapeID", way_id), polygon))
        elif reference in PLATE_REFS:
            plates.append((tags.get("shapeID", way_id), reference,
                           polygon, altitudes, node_refs))

    findings = 0

    # ── 3. Plate report (informational) ──
    print(f"plates: {len(plates)}")
    for shape_id, reference, polygon, altitudes, _refs in plates:
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
        tree = STRtree([polygon for _sid, polygon, _r in approaches])
        for index_a, (shape_a, polygon_a, _refs_a) in enumerate(approaches):
            for tree_index in tree.query(polygon_a):
                if tree_index <= index_a:
                    continue
                shape_b, polygon_b, _refs_b = approaches[tree_index]
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
        for _plate_id, plate_reference, plate_polygon, _alt, _refs in plates:
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

    # ── 4. Chain continuity (2026-07-15, defect B) ──
    # Group approach pieces into chains: proximity within
    # CHAIN_PROXIMITY_M and rectangle-frame alignment (headings compared
    # modulo 90° — a chain's quads are wider than they are long, so the
    # travel axis may be either rectangle axis).  Within each chain,
    # pieces must form ONE connected component at
    # CHAIN_MAX_FACING_GAP_M; every split is a terrain hole.
    def _frame_heading(polygon):
        try:
            corners = list(
                polygon.minimum_rotated_rectangle.exterior.coords)[:4]
        except Exception:
            return None
        if len(corners) < 4:
            return None
        return math.atan2(corners[1][1] - corners[0][1],
                          corners[1][0] - corners[0][0]) % (math.pi / 2.0)

    def _frame_difference_degrees(angle_a, angle_b):
        quarter = math.pi / 2.0
        difference = abs(angle_a - angle_b) % quarter
        return math.degrees(min(difference, quarter - difference))

    headings = [_frame_heading(polygon)
                for _sid, polygon, _r in approaches]
    parent = list(range(len(approaches)))

    def _find_root(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def _union(index_a, index_b):
        parent[_find_root(index_a)] = _find_root(index_b)

    for index_a in range(len(approaches)):
        for index_b in range(index_a + 1, len(approaches)):
            heading_a = headings[index_a]
            heading_b = headings[index_b]
            if heading_a is None or heading_b is None:
                continue
            try:
                if (approaches[index_a][1].distance(approaches[index_b][1])
                        > CHAIN_PROXIMITY_M):
                    continue
            except Exception:
                continue
            if (_frame_difference_degrees(heading_a, heading_b)
                    > CHAIN_HEADING_TOLERANCE_DEG):
                continue
            _union(index_a, index_b)

    chains: dict = {}
    for index in range(len(approaches)):
        chains.setdefault(_find_root(index), []).append(index)
    gap_count = 0
    chain_count = 0
    for members in chains.values():
        if len(members) < 2:
            continue
        chain_count += 1
        # Sub-components under the facing-gap threshold.
        sub_parent = {index: index for index in members}

        def _sub_root(index):
            while sub_parent[index] != index:
                sub_parent[index] = sub_parent[sub_parent[index]]
                index = sub_parent[index]
            return index

        for position_a, member_a in enumerate(members):
            for member_b in members[position_a + 1:]:
                try:
                    if (approaches[member_a][1].distance(
                            approaches[member_b][1])
                            <= CHAIN_MAX_FACING_GAP_M):
                        sub_parent[_sub_root(member_a)] = (
                            _sub_root(member_b))
                except Exception:
                    continue
        components: dict = {}
        for member in members:
            components.setdefault(_sub_root(member), []).append(member)
        component_lists = list(components.values())
        if len(component_lists) < 2:
            continue
        # Every split is a hole: report each component's nearest gap to
        # another component, once per split.
        for position, component in enumerate(component_lists[:-1]):
            best = None
            for other in component_lists[position + 1:]:
                for member_a in component:
                    for member_b in other:
                        try:
                            gap = approaches[member_a][1].distance(
                                approaches[member_b][1])
                        except Exception:
                            continue
                        if best is None or gap < best[0]:
                            best = (gap, member_a, member_b)
            if best is None:
                continue
            gap, member_a, member_b = best
            gap_count += 1
            midpoint_x = (approaches[member_a][1].centroid.x
                          + approaches[member_b][1].centroid.x) / 2.0
            midpoint_y = (approaches[member_a][1].centroid.y
                          + approaches[member_b][1].centroid.y) / 2.0
            print(f"FINDING chain-gap {gap:.2f} m between shapes "
                  f"{approaches[member_a][0]}+{approaches[member_b][0]} "
                  f"@{midpoint_y / 111132.0:.5f},"
                  f"{midpoint_x / meters_per_degree_longitude:.5f}")
    print(f"approach chains (>=2 pieces): {chain_count}, "
          f"chain gaps > {CHAIN_MAX_FACING_GAP_M} m: {gap_count}")
    findings += gap_count

    # ── 5. Plate weld (2026-07-15, defect A) ──
    from shapely.geometry import LineString

    def _node_points_meters(node_refs):
        points = []
        for node_ref in node_refs:
            entry = nodes.get(node_ref)
            if entry is None or entry[0] is None:
                continue
            points.append((entry[1] * meters_per_degree_longitude,
                           entry[0] * 111132.0))
        return points

    weld_missing = 0
    plate_end_count = 0
    for plate_id, reference, plate_polygon, _alt, plate_refs in plates:
        if reference != "object_bridge_corridor":
            continue
        try:
            corners = list(
                plate_polygon.minimum_rotated_rectangle.exterior.coords)[:4]
        except Exception:
            continue
        if len(corners) < 4:
            continue
        edges = [(corners[i], corners[(i + 1) % 4]) for i in range(4)]
        edges.sort(key=lambda e: math.hypot(
            e[1][0] - e[0][0], e[1][1] - e[0][1]))
        plate_nodes = _node_points_meters(plate_refs)
        for end_edge in edges[:2]:
            try:
                end_line = LineString(end_edge)
            except Exception:
                continue
            candidates = []
            for shape_id, polygon, node_refs in approaches:
                try:
                    distance = polygon.distance(end_line)
                except Exception:
                    continue
                if distance <= WELD_CHAIN_ATTACH_RADIUS_M:
                    candidates.append((distance, shape_id, node_refs))
            if not candidates:
                continue  # this plate end has no approach chain
            plate_end_count += 1
            candidates.sort(key=lambda entry: entry[0])
            _distance, near_id, near_refs = candidates[0]
            shared = 0
            for point_x, point_y in _node_points_meters(near_refs):
                for plate_x, plate_y in plate_nodes:
                    if math.hypot(point_x - plate_x,
                                  point_y - plate_y) <= WELD_NODE_TOLERANCE_M:
                        shared += 1
                        break
            if shared < WELD_MIN_SHARED_NODES:
                weld_missing += 1
                midpoint = end_line.interpolate(0.5, normalized=True)
                print(f"FINDING plate-weld-missing: approach {near_id} "
                      f"shares {shared} node(s) with corridor plate "
                      f"{plate_id} end "
                      f"@{midpoint.y / 111132.0:.5f},"
                      f"{midpoint.x / meters_per_degree_longitude:.5f}")
    print(f"corridor plate ends with chains: {plate_end_count}, "
          f"missing welds: {weld_missing}")
    findings += weld_missing

    # ── 6. Portal collar coverage (2026-07-15, defect C) ──
    collar_low_count = 0
    portals_with_crown = 0
    mouths = [(shape_id, polygon)
              for shape_id, reference, polygon, _alt, _refs in plates
              if reference == "object_tunnel_portal_mouth"]
    for mouth_id, mouth_polygon in mouths:
        mouth_centroid = mouth_polygon.centroid
        near_crowns = [
            (shape_id, polygon) for shape_id, polygon in crowns
            if polygon.distance(mouth_polygon)
            <= PORTAL_ASSOCIATION_RADIUS_M
        ]
        if not near_crowns:
            continue  # no crown/collar system for this mouth
        portals_with_crown += 1
        crown_id, crown_polygon = min(
            near_crowns,
            key=lambda entry: entry[1].centroid.distance(mouth_centroid))
        # Back-edge axis = the CROWN's long axis (the crown hugs the
        # portal back, so its long axis follows the back edge).  The
        # crown→mouth centroid direction is NOT usable: the measured
        # KBNA portals are diagonal bands whose crown sits laterally
        # one-sided, skewing that inference by ~30-60° and folding
        # outward depth into the "width".
        try:
            crown_corners = list(
                crown_polygon.minimum_rotated_rectangle.exterior.coords)[:4]
        except Exception:
            continue
        if len(crown_corners) < 4:
            continue
        crown_edges = [
            (crown_corners[i], crown_corners[(i + 1) % 4])
            for i in range(4)
        ]
        longest_edge = max(crown_edges, key=lambda edge: math.hypot(
            edge[1][0] - edge[0][0], edge[1][1] - edge[0][1]))
        lateral_x = longest_edge[1][0] - longest_edge[0][0]
        lateral_y = longest_edge[1][1] - longest_edge[0][1]
        lateral_norm = math.hypot(lateral_x, lateral_y)
        if lateral_norm < 0.5:
            continue
        lateral_x /= lateral_norm
        lateral_y /= lateral_norm
        portal_union = mouth_polygon.union(crown_polygon)
        if portal_union.geom_type != "Polygon":
            portal_union = max(
                portal_union.geoms, key=lambda geometry: geometry.area)
        anchor_point = portal_union.centroid

        def _lateral_span(polygon):
            values = [
                ((x - anchor_point.x) * lateral_x
                 + (y - anchor_point.y) * lateral_y)
                for x, y in polygon.exterior.coords
            ]
            return min(values), max(values)

        span_low, span_high = _lateral_span(portal_union)
        width = span_high - span_low
        if width < 1.0:
            continue
        intervals = sorted(
            _lateral_span(polygon)
            for _shape_id, polygon in collars
            if polygon.distance(mouth_polygon)
            <= PORTAL_ASSOCIATION_RADIUS_M
        )
        merged = []
        for interval_low, interval_high in intervals:
            if merged and interval_low <= merged[-1][1]:
                merged[-1] = (merged[-1][0],
                              max(merged[-1][1], interval_high))
            else:
                merged.append((interval_low, interval_high))
        covered = sum(
            min(interval_high, span_high) - max(interval_low, span_low)
            for interval_low, interval_high in merged
            if interval_high > span_low and interval_low < span_high
        )
        fraction = covered / width
        if fraction < PORTAL_COLLAR_MIN_COVERAGE:
            collar_low_count += 1
            print(f"FINDING portal-collar-coverage {fraction:.0%} "
                  f"(< {PORTAL_COLLAR_MIN_COVERAGE:.0%}) of "
                  f"{width:.1f} m back width, mouth {mouth_id} crown "
                  f"{crown_id} "
                  f"@{mouth_centroid.y / 111132.0:.5f},"
                  f"{mouth_centroid.x / meters_per_degree_longitude:.5f}")
    print(f"portal mouths with crowns: {portals_with_crown}, "
          f"collar coverage below "
          f"{PORTAL_COLLAR_MIN_COVERAGE:.0%}: {collar_low_count}")
    findings += collar_low_count

    # ── 7. Portal collar transition (2026-07-15, defect — flat collar
    # cliff).  Every collar vertex must transition (step <=
    # PORTAL_STEP_MAX_M within PORTAL_STEP_XY_M) to its nearest non-crown
    # neighbour, EXCEPT on the object-hidden inner face (within
    # PORTAL_HIDDEN_FACE_TOLERANCE_M of a crown polygon).
    from shapely.geometry import Point

    def _vertices_with_altitude(node_refs):
        points = []
        for node_ref in node_refs:
            entry = nodes.get(node_ref)
            if (entry is None or entry[0] is None or entry[1] is None
                    or entry[2] is None):
                continue
            points.append((
                entry[1] * meters_per_degree_longitude,
                entry[0] * 111132.0,
                entry[2],
            ))
        return points

    collar_ways = []          # (way_id, [(x, y, alt), ...])
    neighbour_points = []     # (x, y, alt, ref, way_id)
    for way_id, tags, node_refs in ways:
        reference = tags.get("ref", "")
        if reference == COLLAR_REF:
            vertices = _vertices_with_altitude(node_refs)
            if vertices:
                collar_ways.append((way_id, vertices))
                for x, y, altitude in vertices:
                    neighbour_points.append(
                        (x, y, altitude, reference, way_id))
        elif reference in PORTAL_COLLAR_NEIGHBOUR_REFS:
            for x, y, altitude in _vertices_with_altitude(node_refs):
                neighbour_points.append(
                    (x, y, altitude, reference, way_id))

    crown_polygons = [polygon for _shape_id, polygon in crowns]
    collar_step_findings = 0
    if collar_ways and neighbour_points:
        neighbour_tree = STRtree(
            [Point(x, y) for x, y, _a, _r, _w in neighbour_points])
        for collar_way_id, vertices in collar_ways:
            worst = None
            for vertex_x, vertex_y, vertex_altitude in vertices:
                vertex_point = Point(vertex_x, vertex_y)
                if any(polygon.distance(vertex_point)
                       <= PORTAL_HIDDEN_FACE_TOLERANCE_M
                       for polygon in crown_polygons):
                    continue  # object-hidden inner face — step is by design
                for tree_index in neighbour_tree.query(
                        vertex_point.buffer(PORTAL_STEP_XY_M)):
                    other_x, other_y, other_altitude, other_ref, other_way = (
                        neighbour_points[tree_index])
                    if other_way == collar_way_id:
                        continue  # same collar part
                    if math.hypot(vertex_x - other_x,
                                  vertex_y - other_y) > PORTAL_STEP_XY_M:
                        continue
                    step = abs(vertex_altitude - other_altitude)
                    if step > PORTAL_STEP_MAX_M and (
                            worst is None or step > worst[0]):
                        worst = (step, other_ref, vertex_x, vertex_y)
            if worst is not None:
                collar_step_findings += 1
                step, other_ref, vertex_x, vertex_y = worst
                print(f"FINDING portal-collar-cliff {step:.2f} m step "
                      f"(> {PORTAL_STEP_MAX_M:.1f} m) collar {collar_way_id} "
                      f"vs {other_ref} "
                      f"@{vertex_y / 111132.0:.5f},"
                      f"{vertex_x / meters_per_degree_longitude:.5f}")
    print(f"portal collar parts: {len(collar_ways)}, "
          f"with a cliff step > {PORTAL_STEP_MAX_M:.1f} m: "
          f"{collar_step_findings}")
    findings += collar_step_findings

    # ── 8. Deck plate <-> pavement weld overlap (2026-07-15) ──
    from shapely.ops import unary_union

    deck_plates = [
        (shape_id, reference, polygon)
        for shape_id, reference, polygon, _alt, _refs in plates
        if reference in ("object_bridge_corridor",
                         "object_bridge_causeway")
    ]
    deck_weld_ways = []   # (way_id, polygon, node_refs)
    airside_ways = []     # (way_id, role, polygon, node_refs)
    causeway_way_refs = []  # node_refs of deck-elevation plates
    for way_id, tags, node_refs in ways:
        reference = tags.get("ref", "")
        role = tags.get("role", "")
        if reference == DECK_WELD_REF:
            polygon = _polygon(node_refs)
            if polygon is not None:
                deck_weld_ways.append((way_id, polygon, node_refs))
        elif reference == "object_bridge_causeway":
            causeway_way_refs.append(node_refs)
        elif role in AIRSIDE_ROLE_TAGS:
            polygon = _polygon(node_refs)
            if polygon is not None:
                airside_ways.append((way_id, role, polygon, node_refs))
    weld_gap_findings = 0
    adjacency_pairs = 0
    if deck_plates:
        system_polygons = (
            [polygon for _sid, _ref, polygon in deck_plates]
            + [polygon for _wid, polygon, _r in deck_weld_ways])
        try:
            system_union = unary_union(system_polygons)
            eroded_system = system_union.buffer(
                -(WELD_OVERLAP_MIN_DEPTH_M - 0.01))
        except Exception:
            system_union = eroded_system = None
        # Deck-ELEVATION node points (causeway plates + weld strips):
        # the coplanarity comparator.  The corridor trench floor is
        # deliberately excluded — it sits a storey below by design.
        deck_level_points = []
        for node_refs in ([refs for _w, _p, refs in deck_weld_ways]
                          + causeway_way_refs):
            for node_ref in node_refs:
                entry = nodes.get(node_ref)
                if (entry is None or entry[0] is None
                        or entry[2] is None):
                    continue
                deck_level_points.append((
                    entry[1] * meters_per_degree_longitude,
                    entry[0] * 111132.0, entry[2]))
        for plate_id, plate_reference, plate_polygon in deck_plates:
            for way_id, role, pavement_polygon, pavement_refs \
                    in airside_ways:
                try:
                    distance = plate_polygon.distance(pavement_polygon)
                except Exception:
                    continue
                if distance > PLATE_PAVEMENT_ADJACENCY_M:
                    continue
                try:
                    frontage = pavement_polygon.exterior.intersection(
                        plate_polygon.buffer(WELD_FRONTAGE_BAND_M))
                except Exception:
                    continue
                frontage_length = frontage.length
                if frontage_length < WELD_FRONTAGE_MIN_LENGTH_M:
                    continue
                adjacency_pairs += 1
                if eroded_system is None:
                    continue
                try:
                    covered_length = frontage.intersection(
                        eroded_system).length
                except Exception:
                    covered_length = 0.0
                centroid = (frontage.centroid
                            if not frontage.is_empty
                            else pavement_polygon.centroid)
                if (covered_length
                        < WELD_FRONTAGE_COVERAGE_MIN * frontage_length):
                    weld_gap_findings += 1
                    print(
                        "FINDING deck-weld-gap: pavement "
                        f"{way_id} ({role}) fronts plate {plate_id} "
                        f"({plate_reference}) for "
                        f"{frontage_length:.1f} m but only "
                        f"{covered_length:.1f} m overlaps the deck-weld "
                        f"system by >= {WELD_OVERLAP_MIN_DEPTH_M} m "
                        f"@{centroid.y / 111132.0:.5f},"
                        f"{centroid.x / meters_per_degree_longitude:.5f}")
                    continue
                # Coplanarity inside the overlap: pavement nodes within
                # the system must agree with the nearest deck-elevation
                # node.
                worst_step = None
                for node_ref in pavement_refs:
                    entry = nodes.get(node_ref)
                    if (entry is None or entry[0] is None
                            or entry[2] is None):
                        continue
                    node_x = entry[1] * meters_per_degree_longitude
                    node_y = entry[0] * 111132.0
                    from shapely.geometry import Point as _Point
                    if not system_union.buffer(0.05).covers(
                            _Point(node_x, node_y)):
                        continue
                    nearest = None
                    for other_x, other_y, other_alt in deck_level_points:
                        separation = math.hypot(node_x - other_x,
                                                node_y - other_y)
                        if separation > WELD_ALT_NEIGHBOUR_RADIUS_M:
                            continue
                        if nearest is None or separation < nearest[0]:
                            nearest = (separation, other_alt)
                    if nearest is None:
                        continue
                    step = abs(entry[2] - nearest[1])
                    if step > WELD_ALT_TOLERANCE_M and (
                            worst_step is None or step > worst_step[0]):
                        worst_step = (step, node_x, node_y)
                if worst_step is not None:
                    weld_gap_findings += 1
                    step, node_x, node_y = worst_step
                    print(
                        "FINDING deck-weld-step: pavement "
                        f"{way_id} ({role}) differs {step:.2f} m "
                        f"(> {WELD_ALT_TOLERANCE_M}) from the deck-weld "
                        "system inside the overlap "
                        f"@{node_y / 111132.0:.5f},"
                        f"{node_x / meters_per_degree_longitude:.5f}")
    print(f"deck plate/pavement adjacencies: {adjacency_pairs}, "
          f"weld-gap/step findings: {weld_gap_findings}")
    findings += weld_gap_findings

    # ── 9. Portal outward-ramp width (2026-07-15) ──
    # Mouth-face width = the LONG rotated-rectangle dimension of the
    # mouth plate (the wide face perpendicular to travel; the short
    # dimension is the outward depth).  Each approach is assigned to the
    # nearest mouth and its width measured PERPENDICULAR to travel
    # (mouth-centroid → approach-centroid).  The mapped OSM carriageway
    # is resolved by geometry from the big-road cache (independent of any
    # patch tag, so a pre-fix build with no width plumbing is still
    # judged); when unavailable, only the mouth-face cap is enforced.
    def _rotated_rect_dims(polygon):
        try:
            corners = list(
                polygon.minimum_rotated_rectangle.exterior.coords)[:4]
        except Exception:
            return None
        if len(corners) < 4:
            return None
        edge_a = math.hypot(corners[1][0] - corners[0][0],
                            corners[1][1] - corners[0][1])
        edge_b = math.hypot(corners[2][0] - corners[1][0],
                            corners[2][1] - corners[1][1])
        return min(edge_a, edge_b), max(edge_a, edge_b)

    def _perp_travel_width(approach_polygon, mouth_centroid):
        approach_centroid = approach_polygon.centroid
        travel_x = approach_centroid.x - mouth_centroid.x
        travel_y = approach_centroid.y - mouth_centroid.y
        norm = math.hypot(travel_x, travel_y)
        if norm < 1e-6:
            dims = _rotated_rect_dims(approach_polygon)
            return dims[1] if dims else 0.0
        perp_x, perp_y = -travel_y / norm, travel_x / norm
        values = [
            (x - approach_centroid.x) * perp_x
            + (y - approach_centroid.y) * perp_y
            for x, y in approach_polygon.exterior.coords
        ]
        return max(values) - min(values)

    def _mapped_road_width_near(mouth_polygon):
        """Widest mapped OSM carriageway crossing near the mouth, resolved
        by geometry from the big-road cache — ``None`` when the cache /
        auto_patch package is unavailable or no highway is near."""
        try:
            import os as _os
            import sys as _sys
            _tool_root = _os.path.dirname(
                _os.path.dirname(_os.path.abspath(__file__)))
            for _p in (_os.path.join(_tool_root, "src"), _tool_root):
                if _p not in _sys.path:
                    _sys.path.insert(0, _p)
            from auto_patch.osm_load import _load_osm_big_roads
            from auto_patch.bridges import _carriageway_width_from_tags
            longitudes = [v[1] for v in nodes.values() if v[1] is not None]
            mean_longitude = (sum(longitudes) / len(longitudes)
                              if longitudes else 0.0)
            nodes_raw, ways_raw = _load_osm_big_roads(
                origin_latitude, mean_longitude)
        except Exception:
            return None
        if not ways_raw:
            return None
        node_xy = {
            nid: (lon * meters_per_degree_longitude, lat * 111132.0)
            for nid, (lat, lon) in nodes_raw.items()
        }
        best = None
        for _way_id, node_refs, tags in ways_raw:
            if tags.get("highway") is None:
                continue
            points = [node_xy[n] for n in node_refs if n in node_xy]
            if len(points) < 2:
                continue
            try:
                line = LineString(points)
                if (line.is_empty or line.distance(mouth_polygon)
                        > PORTAL_MAPPED_ROAD_ASSOCIATION_M):
                    continue
            except Exception:
                continue
            width = _carriageway_width_from_tags(
                tags.get("highway"), tags, 0.0)
            if width > 0.0 and (best is None or width > best):
                best = width
        return best

    portal_width_findings = 0
    mouth_shapes = [
        (shape_id, polygon)
        for shape_id, reference, polygon, _alt, _refs in plates
        if reference == "object_tunnel_portal_mouth"
    ]
    if mouth_shapes and approaches:
        for mouth_id, mouth_polygon in mouth_shapes:
            dims = _rotated_rect_dims(mouth_polygon)
            if dims is None:
                continue
            mouth_face_width = dims[1]
            mapped_width = _mapped_road_width_near(mouth_polygon)
            mouth_centroid = mouth_polygon.centroid
            for shape_id, approach_polygon, _refs in approaches:
                # nearest-mouth assignment within the association radius
                try:
                    if (approach_polygon.distance(mouth_polygon)
                            > PORTAL_ASSOCIATION_RADIUS_M):
                        continue
                    nearest_other = min(
                        (mp.distance(approach_polygon)
                         for _mid, mp in mouth_shapes),
                        default=None)
                    if (nearest_other is not None
                            and approach_polygon.distance(mouth_polygon)
                            > nearest_other + 1e-6):
                        continue  # belongs to a closer mouth
                except Exception:
                    continue
                width = _perp_travel_width(approach_polygon, mouth_centroid)
                centroid = approach_polygon.centroid
                location = (
                    f"@{centroid.y / 111132.0:.5f},"
                    f"{centroid.x / meters_per_degree_longitude:.5f}")
                if width > mouth_face_width + PORTAL_RAMP_WIDTH_TOLERANCE_M:
                    portal_width_findings += 1
                    print(f"FINDING portal-ramp-over-mouth-face "
                          f"{width:.1f} m approach {shape_id} wider than "
                          f"mouth {mouth_id} face {mouth_face_width:.1f} m "
                          f"{location}")
                    continue
                if (mapped_width is not None
                        and width > mapped_width
                        * PORTAL_RAMP_MAPPED_WIDTH_FACTOR
                        + PORTAL_RAMP_WIDTH_TOLERANCE_M):
                    portal_width_findings += 1
                    print(f"FINDING portal-ramp-over-mapped-width "
                          f"{width:.1f} m approach {shape_id} exceeds "
                          f"{PORTAL_RAMP_MAPPED_WIDTH_FACTOR:.1f}× the "
                          f"mapped OSM road {mapped_width:.1f} m at mouth "
                          f"{mouth_id} {location}")
    print(f"portal mouths (width-checked): {len(mouth_shapes)}, "
          f"over-wide approach findings: {portal_width_findings}")
    findings += portal_width_findings

    # ── 10 & 11. Portal collar front-remnant + flank transition ──
    # Both key off the outward (mouth->road) direction inferred from the
    # crown->mouth centroids (the patch carries no ``outward`` tag).
    collar_vertices_by_way = {}   # way_id -> [(x, y, alt), ...]
    collar_polygons_by_way = {}   # way_id -> Polygon
    for way_id, tags, node_refs in ways:
        if tags.get("ref", "") != COLLAR_REF:
            continue
        vertices = _vertices_with_altitude(node_refs)
        if vertices:
            collar_vertices_by_way[way_id] = vertices
        polygon = _polygon(node_refs)
        if polygon is not None:
            collar_polygons_by_way[way_id] = polygon

    mouth_records = []  # (id, polygon, centroid, [(x,y,alt)], outward)
    for shape_id, reference, polygon, _alt, refs in plates:
        if reference != "object_tunnel_portal_mouth":
            continue
        mouth_centroid = polygon.centroid
        near_crowns = [
            (crown_polygon.centroid.distance(mouth_centroid), crown_polygon)
            for _crown_id, crown_polygon in crowns
            if crown_polygon.distance(polygon) <= PORTAL_ASSOCIATION_RADIUS_M
        ]
        outward = None
        if near_crowns:
            _distance, crown_polygon = min(near_crowns, key=lambda e: e[0])
            crown_centroid = crown_polygon.centroid
            outward_x = mouth_centroid.x - crown_centroid.x
            outward_y = mouth_centroid.y - crown_centroid.y
            norm = math.hypot(outward_x, outward_y)
            if norm >= 0.5:
                outward = (outward_x / norm, outward_y / norm)
        mouth_records.append((
            shape_id, polygon, mouth_centroid,
            _vertices_with_altitude(refs), outward))

    front_remnant_findings = 0
    flank_findings = 0
    if mouth_records and collar_polygons_by_way:
        for collar_way, collar_polygon in collar_polygons_by_way.items():
            # Assign each collar to its NEAREST mouth: paired portals
            # face each other, so a part behind mouth A is ahead of B.
            nearest = min(
                mouth_records,
                key=lambda record: record[1].distance(collar_polygon))
            (mouth_id, mouth_polygon, mouth_centroid,
             mouth_vertices, outward) = nearest
            if outward is None or not mouth_vertices:
                continue
            perpendicular = (-outward[1], outward[0])
            # ── 10. front remnant: collar centroid ahead of the face ──
            front_extent = max(
                vertex_x * outward[0] + vertex_y * outward[1]
                for vertex_x, vertex_y, _alt in mouth_vertices)
            collar_centroid = collar_polygon.centroid
            forward = (collar_centroid.x * outward[0]
                       + collar_centroid.y * outward[1])
            if forward > front_extent + PORTAL_FRONT_REMNANT_TOL_M:
                front_remnant_findings += 1
                print(
                    "FINDING portal-collar-front-remnant: collar "
                    f"{collar_way} ({collar_polygon.area:.0f} m2) sits "
                    f"{forward - front_extent:.1f} m FORWARD of mouth "
                    f"{mouth_id} face "
                    f"@{collar_centroid.y / 111132.0:.5f},"
                    f"{collar_centroid.x / meters_per_degree_longitude:.5f}")
            # ── 11. flank transition: step onto the mouth beyond span ──
            mouth_lateral = [
                (vertex_x - mouth_centroid.x) * perpendicular[0]
                + (vertex_y - mouth_centroid.y) * perpendicular[1]
                for vertex_x, vertex_y, _alt in mouth_vertices]
            span_low, span_high = min(mouth_lateral), max(mouth_lateral)
            worst = None
            for collar_x, collar_y, collar_alt in \
                    collar_vertices_by_way.get(collar_way, []):
                lateral = (
                    (collar_x - mouth_centroid.x) * perpendicular[0]
                    + (collar_y - mouth_centroid.y) * perpendicular[1])
                if (span_low - PORTAL_FLANK_SPAN_TOL_M
                        <= lateral
                        <= span_high + PORTAL_FLANK_SPAN_TOL_M):
                    continue  # object-hidden span — vertical meeting is fine
                for mouth_x, mouth_y, mouth_alt in mouth_vertices:
                    if math.hypot(collar_x - mouth_x,
                                  collar_y - mouth_y) > PORTAL_STEP_XY_M:
                        continue
                    step = abs(collar_alt - mouth_alt)
                    if step > PORTAL_STEP_MAX_M and (
                            worst is None or step > worst[0]):
                        worst = (step, collar_x, collar_y, lateral)
            if worst is not None:
                flank_findings += 1
                step, collar_x, collar_y, lateral = worst
                print(
                    f"FINDING portal-collar-flank-cliff {step:.2f} m step "
                    f"(> {PORTAL_STEP_MAX_M:.1f} m) collar {collar_way} "
                    f"onto mouth {mouth_id} beside the opening "
                    f"(lateral {lateral:.1f} m outside "
                    f"[{span_low:.1f},{span_high:.1f}]) "
                    f"@{collar_y / 111132.0:.5f},"
                    f"{collar_x / meters_per_degree_longitude:.5f}")
    print(f"portal collar parts: {len(collar_polygons_by_way)}, "
          f"front remnants: {front_remnant_findings}, "
          f"flank cliffs beyond the object span: {flank_findings}")
    findings += front_remnant_findings + flank_findings

    print("PASS" if findings == 0 else f"FAIL ({findings} finding(s))")
    return 0 if findings == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
