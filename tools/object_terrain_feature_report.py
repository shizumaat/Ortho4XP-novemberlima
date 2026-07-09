"""Dump the object-derived terrain-feature classifier for one airport pack.

Workstream W-V of ``docs/object_terrain_features_spec.md`` (section 4, the
per-pack classifier dump).  This is the standalone command-line front end to
the pure classifier
``auto_patch.object_terrain_features.classify_object_terrain_features``: it
runs exactly the read -> load -> classify chain that
``auto_patch.object_terrain_assembly`` performs inside the build (this tool
IMPORTS both modules, never edits them), and prints every record the
classifier emits so a human can read what feature A (tunnel cutouts) and
feature B (bridge adaptation) would see BEFORE either gated feature is
switched on.

The physics
-----------
X-Plane drapes each ``OBJECT`` at ``terrain(anchor) + offset`` (0 for a
plain ``OBJECT``, the signed metres for ``OBJECT_AGL``, absolute for
``OBJECT_MSL``).  The classifier reasons in anchor-independent EFFECTIVE
HEIGHT (``above_ground_level_metres + authored_local_y``), so the plane
``effective_y = 0`` is grade.  A tunnel is a below-grade DRIVABLE
(``ATTR_hard`` / ``ATTR_hard_deck``) deck (or a negative ``OBJECT_AGL``
placement); a bridge is a near-horizontal hard deck whose contract
(deck-carried / terrain-carried / profile-carried) is read from its deck
profile and — when pavement is supplied — the pavement coverage of the
mid-deck box.  See the classifier module docstring for the full model.

What this tool reads, and one honest limitation
-----------------------------------------------
The bridge terrain CONTRACT depends on how much draped pavement crosses the
mid-deck box (spec section 2.3).  Inside the build the pipeline hands the
classifier its OWN solved pavement union; a standalone tool has no solved
pavement, so this tool reads the pack's DSF draped pavement polygons
(``dsf_reader.read_dsf_pavements``) and feeds those as the coverage
evidence.  That matches the KBNA reality (all pavement is DSF-draped there
and cut at the abutments), but a pack whose drivable pavement lives only in
its ``apt.dat`` (not the DSF) will show ``coverage=None`` and the contract
falls back to the deck-crest height rule — the report says so per bridge.

Usage:
    venv/bin/python tools/object_terrain_feature_report.py <tile.dsf | pack_root>
                                    [--xplane-root DIR]
                                    [--no-pavement]

Examples (the three-pack investigation set):
    venv/bin/python tools/object_terrain_feature_report.py \
        "$XP/Custom Scenery/c_GBR - 100_airport - EGLL_LONDON_TAIMODELS/Earth nav data/+50-010/+51-001.dsf"
    venv/bin/python tools/object_terrain_feature_report.py \
        "$XP/Custom Scenery/US-KBNA Nashville Airport/Earth nav data/+30-090/+36-087.dsf"
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

# The clutter cutoff object_terrain_assembly applies: a resource placed
# more than this many times is trees/lamps/fences, never a tunnel/bridge.
MAXIMUM_PLACEMENTS_PER_RESOURCE = 50


def _default_xplane_root(dsf_path: str) -> str | None:
    """Walk up from the DSF to the X-Plane root above ``Custom Scenery``."""
    current = os.path.abspath(dsf_path)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if os.path.basename(parent) == "Custom Scenery":
            return os.path.dirname(parent)
        current = parent


def _resolve_dsf_path(argument: str) -> str | None:
    """Accept either a DSF file or a pack root; return the tile DSF path.

    A pack root is resolved to the single ``.dsf`` under its ``Earth nav
    data`` tree (the airport overlay tile)."""
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


def _load_geometry_by_resource(placements, pack_root, xplane_root):
    """Resolve and load OBJ8 geometry for each terrain-relative placement
    resource, skipping light-only objects and mass-placed clutter — the
    same two skips ``object_terrain_assembly._load_object_geometry_by_resource``
    applies."""
    placement_count: dict[str, int] = {}
    for placement in placements:
        placement_count[placement.resource_path] = (
            placement_count.get(placement.resource_path, 0) + 1
        )
    geometry_by_resource: dict = {}
    for resource_path in sorted(
        {placement.resource_path for placement in placements}
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
    return geometry_by_resource


def _draped_pavement_polygons(dsf_path: str, xplane_root: str | None):
    """DSF draped pavement outer rings as shapely polygons in
    ``(longitude, latitude)`` — the classifier's contract-coverage
    evidence.  ``None`` when the pack has no DSF pavement (contract then
    falls back to the deck-crest height rule)."""
    from shapely.geometry import Polygon

    try:
        pavements = dsf_reader.read_dsf_pavements(
            dsf_path,
            cache_dir=tempfile.gettempdir(),
            xplane_root=xplane_root,
        )
    except (OSError, ValueError):
        return None
    polygons = []
    for outer_ring, _holes, _def_path in pavements:
        if len(outer_ring) >= 3:
            polygons.append(Polygon(outer_ring))
    return polygons or None


def _format_polygon_area_list(polygons) -> str:
    if not polygons:
        return "none"
    return ", ".join(f"{polygon.area:.0f} m2" for polygon in polygons)


def _print_tunnel(index: int, tunnel) -> None:
    longitude, latitude = tunnel.anchor_longitude_latitude
    print(f"  [tunnel {index}] anchor {latitude:.6f},{longitude:.6f}  "
          f"heading {tunnel.heading_degrees:.1f}  {tunnel.placement_kind}"
          + (f"  offset {tunnel.above_ground_offset_m:+.1f} m"
             if tunnel.above_ground_offset_m else ""))
    print(f"      resources: {', '.join(tunnel.object_resources)}")
    roof_area = (
        f"{tunnel.roof_footprint.area:.0f} m2"
        if tunnel.roof_footprint is not None else "none"
    )
    deck_area = (
        f"{tunnel.deck_footprint.area:.0f} m2"
        if tunnel.deck_footprint is not None else "none"
    )
    print(f"      body depth {tunnel.body_depth_m:.2f} m below grade   "
          f"roof footprint {roof_area}   deck footprint {deck_area}")
    print(f"      mouths ({len(tunnel.mouth_polygons)}): "
          f"{_format_polygon_area_list(tunnel.mouth_polygons)}")
    for mouth_index, depth in enumerate(tunnel.mouth_depth_samples):
        print(f"        mouth {mouth_index}: depth "
              f"{depth.minimum_depth_m:.2f}..{depth.maximum_depth_m:.2f} m "
              f"(mean {depth.mean_depth_m:.2f}, n={depth.sample_count})")


def _print_bridge(index: int, bridge) -> None:
    longitude, latitude = bridge.anchor_longitude_latitude
    end_start, end_far = bridge.deck_end_elevations_y_m
    ceiling = (
        f"{bridge.ceiling_y_m:+.2f} m" if bridge.ceiling_y_m is not None
        else "none"
    )
    clearance = (
        f"{bridge.clearance_underside_y_m:+.2f} m"
        if bridge.clearance_underside_y_m is not None else "none"
    )
    absolute = (
        f"{bridge.absolute_deck_elevation_m:.4f} m MSL"
        if bridge.absolute_deck_elevation_m is not None
        else "none (no OBJECT_MSL on deck)"
    )
    print(f"  [bridge {index}] contract {bridge.contract}   "
          f"anchor {latitude:.6f},{longitude:.6f}  "
          f"heading {bridge.heading_degrees:.1f}")
    print(f"      resources: {', '.join(bridge.object_resources)}")
    print(f"      deck {bridge.deck_length_m:.1f} x {bridge.deck_width_m:.1f} m   "
          f"hardness {bridge.deck_hardness} (hard_deck={bridge.hard_deck})")
    print(f"      deck-top profile: ends {end_start:+.2f} / {end_far:+.2f} m,  "
          f"crest {bridge.deck_top_y_m:+.2f} m  "
          f"({len(bridge.deck_top_profile)} bins)")
    print(f"      clearance underside {clearance}   slab ceiling {ceiling}")
    print(f"      absolute deck elevation: {absolute}")
    print(f"      abutment reaches grade: {bridge.abutment_reaches_grade}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dsf", help="tile DSF file OR pack root directory")
    parser.add_argument("--xplane-root", default=None)
    parser.add_argument(
        "--no-pavement",
        action="store_true",
        help="skip the DSF pavement read; contract falls back to deck-crest "
        "height",
    )
    args = parser.parse_args()

    dsf_path = _resolve_dsf_path(args.dsf)
    if dsf_path is None:
        print(f"no DSF found at or under {args.dsf}", file=sys.stderr)
        return 2
    xplane_root = args.xplane_root or _default_xplane_root(dsf_path)

    lines = dsf_reader._load_dsf_text(dsf_path, cache_dir=tempfile.gettempdir())
    if lines is None:
        print("DSFTool could not read the DSF", file=sys.stderr)
        return 2

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
    geometry_by_resource = _load_geometry_by_resource(
        terrain_placements, pack_root, xplane_root
    )

    pavement_polygons = None
    if not args.no_pavement:
        pavement_polygons = _draped_pavement_polygons(dsf_path, xplane_root)

    print(f"pack:  {pack_root or '(unknown)'}")
    print(f"DSF:   {dsf_path}")
    print(f"placements: {len(terrain_placements)} terrain-relative "
          f"({len({p.resource_path for p in terrain_placements})} definitions), "
          f"{len(mean_sea_level_placements)} OBJECT_MSL")
    print(f"loaded geometry for {len(geometry_by_resource)} object definitions "
          f"(clutter cutoff {MAXIMUM_PLACEMENTS_PER_RESOURCE} placements)")
    print(f"pavement coverage evidence: "
          f"{'none (crest-height fallback)' if pavement_polygons is None else str(len(pavement_polygons)) + ' DSF draped polygons'}")
    print()

    result = object_terrain_features.classify_object_terrain_features(
        terrain_placements,
        geometry_by_resource,
        pavement_polygons_longitude_latitude=pavement_polygons,
        mean_sea_level_placements=mean_sea_level_placements,
        pack_root=pack_root,
    )

    print(f"=== TUNNELS ({len(result.tunnels)}) ===")
    for index, tunnel in enumerate(result.tunnels):
        _print_tunnel(index, tunnel)
    if not result.tunnels:
        print("  none")

    print(f"\n=== BRIDGES ({len(result.bridges)}) ===")
    for index, bridge in enumerate(result.bridges):
        _print_bridge(index, bridge)
    if not result.bridges:
        print("  none")

    print(f"\n=== REFUSALS ({len(result.refusals)}) ===")
    for refusal in result.refusals:
        print(f"  {', '.join(refusal.object_resources)}")
        print(f"      reason: {refusal.reason}")
    if not result.refusals:
        print("  none")

    print(f"\n=== EXCLUSIONS (R4 Phase-2 y-bake feed): "
          f"{len(result.exclusions)} object(s) ===")
    for _pack_root, resource in result.exclusions:
        print(f"  {resource}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
