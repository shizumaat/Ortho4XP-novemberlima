"""Audit a classified taxiway bridge against a BUILT Ortho4XP mesh.

Workstream W-V of ``docs/object_terrain_features_spec.md`` (section 4, the
``bridge_deck_audit`` acceptance tool).  Feature B (``O4_OBJECT_BRIDGE_TERRAIN``)
must make the terrain meet the deck at the abutments and clear the girder
line over the depressed road corridor.  This tool classifies the pack's
bridge objects (importing the pure classifier — never editing it), then
samples the built ``Data<tile>.mesh`` where feature B claims to have acted
and reports whether the numbers land on the spec's acceptance values.

The physics being checked (spec sections 2.2, 3.2)
--------------------------------------------------
A deck-carried bridge (KBNA class) has its deck top at an absolute
elevation — pinned by ``OBJECT_MSL`` fixtures where the pack supplies them
(KBNA taxiway-L: twelve fixtures at 166.9994 m -> deck 167.0 m), else the
anchor datum plus the deck-top profile.  The abutment lines are where the
solved pavement network must grade UP to the deck elevation; the road
corridor beneath must be depressed so the girder underside clears it
(KBNA: corridor <= 161.25 m under a +4.2 m girder line).  So the audit
measures three things against the built mesh:

    abutment terrain   mesh elevation along each abutment line, versus the
                       deck-end elevation (MSL-first, else mesh-at-anchor +
                       profile end).  Feature B pins these equal.
    corridor floor     mesh elevation along the deck centerline (the road
                       corridor), versus the clearance-underside line
                       (deck absolute - (deck-top - girder underside)).
                       The corridor must sit BELOW the girder line by the
                       clearance margin.
    acceptance         PASS/FAIL against the spec section 4 numbers
                       (KBNA taxiway-L: abutments 167.0 +/- 0.25, corridor
                       <= 161.25 m).

The datum proxy: with no ``OBJECT_MSL`` fixture, "DEM-at-anchor" is taken
as the built mesh's own elevation at the structure anchor — the same point
the anchor seat fixes in the solve — plus the deck profile value.  This is
reported explicitly so a reader can see which path supplied each number.

Sampling plumbing only, until a gated tile is built
---------------------------------------------------
Feature B is default OFF and no gated tile has been built, so run against
today's UNGRADED Ortho4XP mesh this tool will (correctly) report FAIL —
the terrain has not been graded to the deck yet.  That still exercises the
whole sampling path (sampler load, abutment/centerline sampling, the
comparison arithmetic).  Point ``--mesh`` at a feature-B build to get a
real PASS/FAIL.

Usage:
    venv/bin/python tools/bridge_deck_audit.py <tile.dsf | pack_root>
                                    --mesh Data<tile>.mesh
                                    [--xplane-root DIR]
                                    [--samples-per-line 9]
                                    [--tolerance 0.25]

Example (the Nimbus KBNA pack against its Ortho4XP tile mesh):
    venv/bin/python tools/bridge_deck_audit.py \
        "$XP/Custom Scenery/US-KBNA Nashville Airport/Earth nav data/+30-090/+36-087.dsf" \
        --mesh "$XP/Custom Scenery/zOrtho4XP_+36-087/Data+36-087.mesh"
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from auto_patch import dsf_reader, obj8_reader, object_terrain_features
from auto_patch.object_terrain_features import (
    DECK_CARRIED,
    PROFILE_CARRIED,
)

MAXIMUM_PLACEMENTS_PER_RESOURCE = 50

# Spec section 4 acceptance numbers for KBNA taxiway-L, keyed here so the
# audit prints them alongside the measured values.  They are the reference
# a feature-B build is judged against; other bridges are judged against
# their own derived deck/corridor elevations.
KBNA_ABUTMENT_ELEVATION_M = 167.0
KBNA_CORRIDOR_CEILING_M = 161.25


def _default_xplane_root(dsf_path: str) -> str | None:
    current = os.path.abspath(dsf_path)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if os.path.basename(parent) == "Custom Scenery":
            return os.path.dirname(parent)
        current = parent


def _resolve_dsf_path(argument: str) -> str | None:
    if os.path.isfile(argument):
        return argument
    earth_nav_data = os.path.join(argument, "Earth nav data")
    if os.path.isdir(earth_nav_data):
        for group in sorted(os.listdir(earth_nav_data)):
            group_dir = os.path.join(earth_nav_data, group)
            if not os.path.isdir(group_dir):
                continue
            for name in sorted(os.listdir(group_dir)):
                if name.lower().endswith(".dsf"):
                    return os.path.join(group_dir, name)
    return None


def _classify_pack(dsf_path: str, xplane_root: str | None):
    """Run the shared read -> load -> classify chain (mirrors
    ``object_terrain_assembly``), feeding DSF draped pavement as the
    contract-coverage evidence.  Returns
    ``(ClassificationResult, terrain_placements)``."""
    from shapely.geometry import Polygon

    lines = dsf_reader._load_dsf_text(dsf_path, cache_dir=tempfile.gettempdir())
    if lines is None:
        raise SystemExit("DSFTool could not read the DSF")
    all_placements = obj8_reader.read_dsf_object_placements(
        lines,
        accept_resource=lambda resource: resource.lower().endswith(".obj"),
        include_object_msl=True,
    )
    mean_sea_level_placements = [
        placement for placement in all_placements
        if placement.placement_kind == "OBJECT_MSL"
    ]
    terrain_placements = [
        placement for placement in all_placements
        if placement.placement_kind != "OBJECT_MSL"
    ]
    pack_root = dsf_reader._pack_root_for_dsf(dsf_path) or ""

    placement_count: dict[str, int] = {}
    for placement in terrain_placements:
        placement_count[placement.resource_path] = (
            placement_count.get(placement.resource_path, 0) + 1
        )
    geometry_by_resource: dict = {}
    for resource_path in sorted(
        {placement.resource_path for placement in terrain_placements}
    ):
        if placement_count[resource_path] > MAXIMUM_PLACEMENTS_PER_RESOURCE:
            continue
        physical_path = obj8_reader.resolve_object_resource(
            resource_path, pack_root, xplane_root
        )
        if physical_path is None:
            continue
        geometry = dsf_reader._load_object_geometry(physical_path)
        if geometry is None or not geometry.has_solid_geometry:
            continue
        geometry_by_resource[resource_path] = geometry

    pavement_polygons = None
    try:
        pavements = dsf_reader.read_dsf_pavements(
            dsf_path, cache_dir=tempfile.gettempdir(), xplane_root=xplane_root
        )
        pavement_polygons = [
            Polygon(outer_ring)
            for outer_ring, _holes, _def in pavements
            if len(outer_ring) >= 3
        ] or None
    except (OSError, ValueError):
        pass

    result = object_terrain_features.classify_object_terrain_features(
        terrain_placements,
        geometry_by_resource,
        pavement_polygons_longitude_latitude=pavement_polygons,
        mean_sea_level_placements=mean_sea_level_placements,
        pack_root=pack_root,
    )
    return result, terrain_placements, pavement_polygons


def _bridge_is_road_carried_proxy(bridge, pavement_polygons) -> bool:
    """Road-overpass discriminator, audit-side proxy (stage 2b): no
    draped pavement within ``BRIDGE_ROAD_CARRIED_PAVEMENT_PROXIMITY_M``
    of the deck footprint means the deck carries a ROAD, not a
    taxi/truck route (KBNA Crossing_Bridge: nearest pavement 176 m) —
    terrain must not rise to it, so the abutment check is skipped.  The
    in-pipeline discriminator reads the layout's taxi/truck shapes
    instead; this proxy exists because the audit has only the pack."""
    import math as _math

    from auto_patch.config import BRIDGE_ROAD_CARRIED_PAVEMENT_PROXIMITY_M
    from auto_patch import obj8_reader as _obj8
    from auto_patch.object_terrain_features import (
        frame_polygon_to_longitude_latitude,
    )
    from shapely.geometry import LineString as _LineString
    from shapely.geometry import Polygon as _Polygon

    if bridge.deck_polygon is None or not pavement_polygons:
        return False
    origin_longitude, origin_latitude = (
        bridge.frame_origin_longitude_latitude
    )
    cosine = _math.cos(_math.radians(origin_latitude))
    earth_radius = 6378137.0

    def _to_meters(longitude, latitude):
        return (
            _math.radians(longitude - origin_longitude)
            * earth_radius * cosine,
            _math.radians(latitude - origin_latitude) * earth_radius,
        )

    footprint = frame_polygon_to_longitude_latitude(
        bridge.deck_polygon, bridge.frame_origin_longitude_latitude
    )
    if footprint.geom_type == "MultiPolygon":
        footprint = max(footprint.geoms, key=lambda g: g.area)
    footprint_meters = _Polygon(
        [_to_meters(lon, lat) for lon, lat in footprint.exterior.coords]
    )
    nearest = None
    for polygon in pavement_polygons:
        try:
            ring = [
                _to_meters(lon, lat) for lon, lat in polygon.exterior.coords
            ]
        except (AttributeError, TypeError):
            continue
        if len(ring) < 2:
            continue
        # Cheap locality reject: skip pavement far outside the frame.
        if min(abs(x) + abs(y) for x, y in ring) > 3000.0:
            continue
        distance = footprint_meters.distance(_LineString(ring))
        if nearest is None or distance < nearest:
            nearest = distance
    return (
        nearest is None
        or nearest > BRIDGE_ROAD_CARRIED_PAVEMENT_PROXIMITY_M
    )


def _frame_point_to_latitude_longitude(bridge, frame_x, frame_z):
    """Convert a structure-frame ``(x, z)`` point to ``(latitude,
    longitude)`` using the bridge's frame origin."""
    origin_longitude, origin_latitude = bridge.frame_origin_longitude_latitude
    return obj8_reader.local_offset_to_lonlat(
        origin_latitude, origin_longitude, 0.0, frame_x, frame_z
    )


# Under-girder trim (m) for the corridor-clearance sample line: the
# corridor's clearance-relevant extent is strictly BETWEEN the abutment
# faces — the sample line's endpoints otherwise land exactly on the
# causeway lips (feature B overlaps the lip 0.6 m inward against node
# wobble), reading headwall fill at deck-end elevation instead of
# passage space (round 10: Murfreesboro corridors 'fouled' by their own
# lip samples at -deck-thickness).
CORRIDOR_UNDER_GIRDER_TRIM_M = 2.0


def _sample_line(sampler, bridge, start_xz, end_xz, count):
    """Mesh elevations sampled at ``count`` equally spaced points along a
    frame-space segment; ``None`` entries where the point fell off the
    retained mesh."""
    elevations = []
    for step in range(count):
        fraction = step / (count - 1) if count > 1 else 0.5
        frame_x = start_xz[0] + fraction * (end_xz[0] - start_xz[0])
        frame_z = start_xz[1] + fraction * (end_xz[1] - start_xz[1])
        latitude, longitude = _frame_point_to_latitude_longitude(
            bridge, frame_x, frame_z
        )
        elevations.append(sampler.elevation_at_or_none(latitude, longitude))
    return elevations


def _summary(values):
    present = [value for value in values if value is not None]
    if not present:
        return None
    return (min(present), sum(present) / len(present), max(present),
            len(present), len(values))


def _format_summary(summary) -> str:
    if summary is None:
        return "no samples on mesh"
    minimum, mean, maximum, present, total = summary
    return (f"{minimum:.2f}..{maximum:.2f} m (mean {mean:.2f}, "
            f"{present}/{total} on mesh)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dsf", help="tile DSF file OR pack root directory")
    parser.add_argument("--mesh", required=True,
                        help="built Data<tile>.mesh to sample")
    parser.add_argument("--xplane-root", default=None)
    parser.add_argument("--samples-per-line", type=int, default=9)
    parser.add_argument("--tolerance", type=float, default=0.25,
                        help="metres of abutment error allowed for PASS")
    args = parser.parse_args()

    dsf_path = _resolve_dsf_path(args.dsf)
    if dsf_path is None:
        print(f"no DSF found at or under {args.dsf}", file=sys.stderr)
        return 2
    xplane_root = args.xplane_root or _default_xplane_root(dsf_path)

    result, placements, pavement_polygons = _classify_pack(
        dsf_path, xplane_root)
    print(f"DSF:  {dsf_path}")
    print(f"mesh: {args.mesh}")
    print(f"{len(result.bridges)} bridge(s) classified\n")
    if not result.bridges:
        print("no bridges to audit")
        return 0

    from auto_patch.mesh_sampler import MeshElevationSampler

    latitudes = [placement.latitude for placement in placements]
    longitudes = [placement.longitude for placement in placements]
    sampler = MeshElevationSampler(
        args.mesh,
        (min(longitudes), min(latitudes), max(longitudes), max(latitudes)),
        margin_degrees=0.03,
    )

    for index, bridge in enumerate(result.bridges):
        print(f"=== bridge {index}: {bridge.contract} "
              f"[{', '.join(bridge.object_resources)}] ===")
        anchor_longitude, anchor_latitude = bridge.anchor_longitude_latitude
        mesh_at_anchor = sampler.elevation_at_or_none(
            anchor_latitude, anchor_longitude
        )

        # Deck-end elevation: OBJECT_MSL first, else mesh-at-anchor + profile.
        if bridge.absolute_deck_elevation_m is not None:
            datum_source = "OBJECT_MSL fixtures"
            deck_absolute = bridge.absolute_deck_elevation_m
            deck_end_absolute = (
                deck_absolute
                - bridge.deck_top_y_m
                + bridge.deck_end_elevations_y_m[0],
                deck_absolute
                - bridge.deck_top_y_m
                + bridge.deck_end_elevations_y_m[1],
            )
        elif mesh_at_anchor is not None:
            datum_source = "mesh-at-anchor + deck profile (no MSL)"
            deck_absolute = mesh_at_anchor + bridge.deck_top_y_m
            deck_end_absolute = (
                mesh_at_anchor + bridge.deck_end_elevations_y_m[0],
                mesh_at_anchor + bridge.deck_end_elevations_y_m[1],
            )
        else:
            datum_source = "unavailable (anchor off mesh, no MSL)"
            deck_absolute = None
            deck_end_absolute = (None, None)

        print(f"  datum source: {datum_source}")
        if mesh_at_anchor is not None:
            print(f"  mesh at anchor: {mesh_at_anchor:.2f} m")
        if deck_absolute is not None:
            print(f"  expected deck top: {deck_absolute:.2f} m MSL   "
                  f"deck ends: {deck_end_absolute[0]:.2f} / "
                  f"{deck_end_absolute[1]:.2f} m")

        # Road-carried overpass (stage 2b): the deck carries a road, not
        # a taxi/truck route — terrain must NOT rise to it, so the
        # abutment coupling is not required and its check is skipped.
        road_carried = _bridge_is_road_carried_proxy(
            bridge, pavement_polygons)
        if road_carried:
            print("  road-carried overpass (no pavement within proximity "
                  "of the deck footprint) — abutment check skipped; the "
                  "road machinery owns the corridor")

        # Abutment terrain versus deck-end elevation.
        abutment_pass = True
        for end_index, abutment_line in enumerate(bridge.abutment_lines):
            samples = _sample_line(
                sampler, bridge, abutment_line[0], abutment_line[1],
                args.samples_per_line,
            )
            summary = _summary(samples)
            expected = deck_end_absolute[end_index] if end_index < 2 else None
            label = "start" if end_index == 0 else "far"
            line = f"  abutment {label}: terrain {_format_summary(summary)}"
            if summary is not None and expected is not None:
                # Round 10, road-exit lanes: a road leaving the span
                # THROUGH this abutment end runs at corridor level in
                # its lane (the author-mesh through-corridor), so
                # samples at or below the girder underside are lane
                # ground, not causeway error.  Judge the deck-end match
                # on the remaining (fill) samples.
                girder_line = None
                if (deck_absolute is not None
                        and bridge.clearance_underside_y_m is not None):
                    girder_line = (deck_absolute - bridge.deck_top_y_m
                                   + bridge.clearance_underside_y_m)
                # Sample classes across an abutment with a road-exit
                # lane (round 10, the author-mesh through-corridor):
                #   lane — at/below the girder underside: corridor
                #          ground where the road leaves the span;
                #   wall — between girder and deck-end: the 45-56
                #          degree side wall between lane and fill;
                #   fill — at deck-end (the causeway), judged strictly.
                # ANY sample above deck-end + tolerance is unfinished
                # ground and fails regardless of class.
                fill_values = []
                lane_count = 0
                wall_count = 0
                too_high = 0
                for value in samples:
                    if value > expected + args.tolerance:
                        too_high += 1
                    elif value >= expected - args.tolerance:
                        fill_values.append(value)
                    elif (girder_line is not None
                          and value <= girder_line + 0.25):
                        lane_count += 1
                    else:
                        wall_count += 1
                if lane_count or wall_count:
                    line += (f"   ({lane_count} road-lane, {wall_count} "
                             "wall-face sample(s) excluded)")
                if fill_values:
                    error = (sum(fill_values) / len(fill_values)
                             - expected)
                    line += (f"   vs deck-end {expected:.2f} -> "
                             f"{error:+.2f} m")
                    if abs(error) > args.tolerance or too_high:
                        abutment_pass = False
                else:
                    line += (f"   vs deck-end {expected:.2f} -> NO fill "
                             "sample at deck-end")
                    abutment_pass = False
                if too_high:
                    line += (f"   ({too_high} sample(s) ABOVE "
                             "deck-end!)")
            else:
                abutment_pass = False
            print(line)

        # Corridor floor along the deck centerline versus the girder line.
        axis_midpoints = []
        for abutment_line in bridge.abutment_lines[:2]:
            axis_midpoints.append((
                (abutment_line[0][0] + abutment_line[1][0]) / 2.0,
                (abutment_line[0][1] + abutment_line[1][1]) / 2.0,
            ))
        corridor_pass = None
        girder_absolute = None
        if len(axis_midpoints) == 2 and deck_absolute is not None:
            # Trim to the under-girder extent (see the constant above).
            (sx, sz), (ex, ez) = axis_midpoints
            axis_length = ((ex - sx) ** 2 + (ez - sz) ** 2) ** 0.5
            trim = (CORRIDOR_UNDER_GIRDER_TRIM_M / axis_length
                    if axis_length > 2 * CORRIDOR_UNDER_GIRDER_TRIM_M
                    else 0.0)
            trimmed_start = (sx + (ex - sx) * trim, sz + (ez - sz) * trim)
            trimmed_end = (ex - (ex - sx) * trim, ez - (ez - sz) * trim)
            corridor_samples = _sample_line(
                sampler, bridge, trimmed_start, trimmed_end,
                max(args.samples_per_line, 11),
            )
            corridor_summary = _summary(corridor_samples)
            if bridge.clearance_underside_y_m is not None:
                girder_absolute = (
                    deck_absolute
                    - bridge.deck_top_y_m
                    + bridge.clearance_underside_y_m
                )
            print(f"  corridor centerline: terrain "
                  f"{_format_summary(corridor_summary)}")
            if girder_absolute is not None:
                print(f"  girder underside line: {girder_absolute:.2f} m MSL "
                      f"(deck - {bridge.deck_top_y_m - bridge.clearance_underside_y_m:.2f} m)")
                if corridor_summary is not None:
                    clearance = girder_absolute - corridor_summary[2]
                    print(f"  min corridor clearance under girder: "
                          f"{clearance:+.2f} m "
                          f"({'ok' if clearance >= 0 else 'FOULED'})")
                    corridor_pass = clearance >= 0

        # KBNA-specific acceptance echo (spec section 4).
        if bridge.absolute_deck_elevation_m is not None:
            print(f"  [KBNA acceptance] abutment target "
                  f"{KBNA_ABUTMENT_ELEVATION_M} +/- {args.tolerance} m, "
                  f"corridor ceiling <= {KBNA_CORRIDOR_CEILING_M} m")

        if road_carried:
            verdict = "SKIPPED (road-carried overpass)"
            print(f"  --> {verdict}")
        else:
            verdict = "PASS" if abutment_pass \
                and (corridor_pass in (True, None)) else "FAIL"
            print(f"  --> {verdict}"
                  + ("" if abutment_pass
                     else "  (abutment terrain off deck)")
                  + ("" if corridor_pass in (True, None)
                     else "  (corridor fouls girder)"))
        print()

    if os.environ.get("O4_OBJECT_BRIDGE_TERRAIN", "0") == "1":
        print("NOTE: O4_OBJECT_BRIDGE_TERRAIN is ON — this audit is a "
              "real acceptance check against a feature-B build.")
    else:
        print("NOTE: feature B (O4_OBJECT_BRIDGE_TERRAIN) is OFF in this "
              "environment.  Against an ungraded Ortho4XP mesh a FAIL is "
              "expected — the terrain has not been graded to the deck.  A "
              "feature-B build is the real acceptance target.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
