"""Find DSF objects whose anchor sits away from their geometry.

X-Plane drapes a plain ``OBJECT`` placement at the terrain elevation
under its anchor (the placement lon/lat), then places the whole mesh
rigidly relative to that.  When an author bakes many buildings into one
``.obj`` — or shares one anchor across a family of objects — the anchor
frequently lands hundreds of metres from any geometry.  On flat terrain
nobody notices.  On real terrain the structures float or sink.

This probe reports, per object definition:

    reach       greatest horizontal distance from the local origin to
                any solid vertex.  A correctly anchored compact object
                measures a few metres.
    structures  how many separate buildings the object contains, after
                welding seams and merging meshes closer than --gap.
    anchored    whether the anchor falls inside (or within --gap of) any
                structure's footprint.  False means the elevation is
                being sampled where nothing exists.
    separation  greatest distance between two structures in the object.
                Non-zero means no single anchor can be correct.

With --mesh, each structure's rendered base is compared against the built
Ortho4XP terrain beneath it.  ``base err`` is the signed distance the
structure floats (positive) or sinks (negative).  Because it measures the
*rendered* base — the anchor's ground elevation plus the structure's own
local y — a corrective y offset baked into the geometry shows up here as a
restored zero, so this doubles as a check on such a fix.

Limitations
-----------
``base err`` reports the *worst* structure in a definition, so a single
building on a slope dominates a row that is otherwise correct.  It also
assumes local y=0 is the object's ground plane, which holds for authored
geometry but not once a corrective y offset has been baked in — after
such a fix the elevated-structure test misclassifies, and a per-component
comparison against the mesh is the reliable check.

Definitions sharing an anchor are analysed independently here, which they
are not: see the warning this prints, and
``tools/reanchor_kclt_terminal_bakes.py``.

Usage:
    python3 tools/dsf_object_anchor_audit.py <path/to/tile.dsf>
                                    [--xplane-root DIR]
                                    [--mesh Data+35-081.mesh]
                                    [--min-reach 25]
                                    [--gap 2]
                                    [--error-threshold 0.25]
                                    [--max-components 2500]

Example (the Nimbus KCLT pack):
    python3 tools/dsf_object_anchor_audit.py \
        "$XP/Custom Scenery/Nimbus Simulation - KCLT V1.4 - Charlotte XP12/Earth nav data/+30-090/+35-081.dsf" \
        --mesh "$XP/Custom Scenery/zOrtho4XP_+35-081/Data+35-081.mesh"
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
from collections import defaultdict

# A structure whose lowest vertex sits above this height rests on another
# structure, not on the terrain.
ELEVATED_BASE_THRESHOLD_METRES = 0.5

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from auto_patch.dsf_reader import _load_dsf_text, _pack_root_for_dsf
from obj8_geometry import (
    area_weighted_centroid,
    connected_components,
    group_components_into_structures,
    horizontal_bounding_box,
    load_object_file,
    local_offset_to_lonlat,
    read_dsf_object_placements,
    resolve_object_resource,
)


def _default_xplane_root(dsf_path: str) -> str | None:
    """Walk up from the DSF looking for the X-Plane root above
    ``Custom Scenery``."""
    current = os.path.abspath(dsf_path)
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if os.path.basename(parent) == "Custom Scenery":
            return os.path.dirname(parent)
        current = parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dsf", help="DSF file to audit")
    parser.add_argument("--xplane-root", default=None)
    parser.add_argument(
        "--mesh", default=None, help="built Data<tile>.mesh for elevation error"
    )
    parser.add_argument("--min-reach", type=float, default=25.0)
    parser.add_argument(
        "--gap",
        type=float,
        default=2.0,
        help="merge components closer than this into one structure; "
        "chaining makes large values collapse distinct buildings",
    )
    parser.add_argument(
        "--error-threshold",
        type=float,
        default=0.25,
        help="metres of float/sink before a definition is flagged (needs --mesh)",
    )
    parser.add_argument(
        "--max-components",
        type=int,
        default=2500,
        help="skip structure grouping above this component count",
    )
    args = parser.parse_args()

    xplane_root = args.xplane_root or _default_xplane_root(args.dsf)
    pack_root = _pack_root_for_dsf(args.dsf)
    lines = _load_dsf_text(args.dsf, cache_dir=tempfile.gettempdir())
    if lines is None:
        print("DSFTool could not read the DSF", file=sys.stderr)
        return 2

    placements = read_dsf_object_placements(lines)
    if not placements:
        print("no terrain-draped OBJECT placements in this DSF")
        return 0

    placement_count: dict[str, int] = {}
    first_placement = {}
    for placement in placements:
        placement_count[placement.resource_path] = (
            placement_count.get(placement.resource_path, 0) + 1
        )
        first_placement.setdefault(placement.resource_path, placement)

    sampler = None
    if args.mesh:
        from mesh_elevation_sampler import MeshElevationSampler

        latitudes = [p.latitude for p in placements]
        longitudes = [p.longitude for p in placements]
        sampler = MeshElevationSampler(
            args.mesh,
            (min(longitudes), min(latitudes), max(longitudes), max(latitudes)),
            margin_degrees=0.03,
        )

    print(
        f"{len(placement_count)} object definitions, "
        f"{len(placements)} terrain-draped placements\n"
    )
    header = (
        f"{'reach':>8} {'structs':>8} {'comps':>7} {'plc':>5} "
        f"{'anchored':>9} {'separation':>11}"
    )
    if sampler:
        header += f" {'base err':>10}"
    print(header + "  resource")

    findings = []
    for resource, count in placement_count.items():
        placement = first_placement[resource]
        path = resolve_object_resource(resource, pack_root, xplane_root)
        if not path:
            continue
        geometry = load_object_file(path)
        if not geometry.has_solid_geometry:
            continue
        reach = geometry.solid_reach_metres()
        if reach < args.min_reach:
            continue

        components = connected_components(
            geometry.vertices, geometry.solid_triangles
        )
        if len(components) > args.max_components:
            print(
                f"{reach:7.0f}m {'-':>8} {len(components):7d} {count:5d} "
                f"{'-':>9} {'-':>11}"
                + (f" {'-':>10}" if sampler else "")
                + f"  {resource}   (skipped: >{args.max_components} components)"
            )
            continue

        structures = group_components_into_structures(
            geometry.vertices, components, args.gap
        )
        centroids = [
            area_weighted_centroid(geometry.vertices, structure)
            for structure in structures
        ]
        anchored = any(
            min_x - args.gap <= 0.0 <= max_x + args.gap
            and min_z - args.gap <= 0.0 <= max_z + args.gap
            for min_x, max_x, min_z, max_z in (
                horizontal_bounding_box(geometry.vertices, structure)
                for structure in structures
            )
        )
        separation = max(
            (
                math.hypot(
                    centroids[left][1] - centroids[right][1],
                    centroids[left][2] - centroids[right][2],
                )
                for left in range(len(centroids))
                for right in range(left + 1, len(centroids))
            ),
            default=0.0,
        )

        worst_error = None
        if sampler:
            # X-Plane renders the object's local y=0 plane at the terrain
            # under the *anchor*.  A structure whose lowest vertex sits at
            # local y therefore renders at anchor_elevation + y, and the
            # error is how far that lands from the ground beneath it.
            # Measuring the rendered base — rather than ground-vs-ground —
            # means a corrective y offset baked into the geometry shows up
            # here as a restored zero.
            anchor_elevation = sampler.elevation_at(
                placement.latitude, placement.longitude
            )
            errors = []
            for structure, (_, centroid_x, centroid_z) in zip(
                structures, centroids
            ):
                latitude, longitude = local_offset_to_lonlat(
                    placement.latitude,
                    placement.longitude,
                    placement.heading_degrees,
                    centroid_x,
                    centroid_z,
                )
                base_y = min(
                    geometry.vertices[index][1]
                    for triangle in structure
                    for index in triangle
                )
                if base_y > ELEVATED_BASE_THRESHOLD_METRES:
                    # Rooftop clutter, canopies, jetbridges: they rest on
                    # something else, not on the terrain.  Judging them
                    # against the ground would report every roof as floating.
                    continue
                rendered_base = anchor_elevation + base_y
                errors.append(
                    rendered_base - sampler.elevation_at(latitude, longitude)
                )
            # No ground-touching structure at all.  Objects split by texture
            # page routinely contain nothing but roof or glass faces, and
            # scoring those against the terrain is meaningless.  Report it
            # rather than defaulting to a zero that reads as "correct".
            worst_error = max(errors, key=abs) if errors else None

        if sampler and worst_error is None:
            needs_fix = True  # undecidable alone; must be pooled with siblings
        elif worst_error is not None:
            needs_fix = abs(worst_error) > args.error_threshold
        else:
            needs_fix = (not anchored) or len(structures) > 1
        row = (
            f"{reach:7.0f}m {len(structures):8d} {len(components):7d} "
            f"{count:5d} {str(anchored):>9} {separation:10.0f}m"
        )
        if sampler:
            row += (
                f" {worst_error:+9.2f}m"
                if worst_error is not None
                else f" {'no base':>10}"
            )
        print(row + f"  {resource}" + ("   <-- FIX" if needs_fix else ""))
        if needs_fix:
            findings.append((resource, placement))

    print(f"\n{len(findings)} definitions need re-anchoring.")

    # Definitions sharing an anchor share a local frame, and in practice
    # share buildings: a family split by texture page holds one building's
    # walls in one file and its roof in another.  Correcting any of them
    # alone tears the building apart.
    by_anchor: dict[tuple[float, float, float], list[str]] = defaultdict(list)
    for resource, placement in findings:
        by_anchor[
            (
                round(placement.latitude, 6),
                round(placement.longitude, 6),
                round(placement.heading_degrees, 4),
            )
        ].append(resource)
    families = {
        anchor: resources
        for anchor, resources in by_anchor.items()
        if len(resources) > 1
    }
    if families:
        print(
            "\nWARNING: these definitions share an anchor, so they probably "
            "share buildings.\nThey must be pooled into one structure "
            "partition and corrected together —\nsee "
            "tools/reanchor_kclt_terminal_bakes.py.  A 'no base' row is a "
            "file holding no\nground-level geometry at all, which only makes "
            "sense pooled with its siblings."
        )
        for (latitude, longitude, heading), resources in families.items():
            print(
                f"  {len(resources):3d} objects @ {latitude:.6f},"
                f"{longitude:.6f} heading {heading:.4f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
