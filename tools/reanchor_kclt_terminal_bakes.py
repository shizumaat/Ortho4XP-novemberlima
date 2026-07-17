"""PROTOTYPE: sit each KCLT terminal/hangar structure on the ground.

Hard-coded to the eight ``Charlotte_Airport_00X_ALB.obj`` bakes in the
Nimbus KCLT pack.  This is an experiment for in-sim visual verification,
not a general facility.

The problem
-----------
All eight objects share ONE ``OBJECT`` placement anchor::

    OBJECT 30 -80.935041390 35.207360571 86.095674

The nearest solid vertex to that anchor is 693 m away — it lands in open
grass.  X-Plane samples the terrain there and places every structure in
all eight objects at that one elevation.  Pooled across the eight files
the geometry resolves into ~50 real buildings spread over 2.2 km, so
they float or sink by whatever the ground varies across the airport.

Why all eight files, and why not just move the anchor
-----------------------------------------------------
The eight bakes are split by texture page, not by building: four of the
five structures in ``..._007_ALB.obj`` share vertices (0.00 m apart) with
``001``/``002``/``004``/``006``/``008``.  They are the same buildings.
Re-anchoring one file in isolation tears its walls away from its roof.
So the structure partition must be computed over the pooled geometry of
all eight — legitimate because they share a placement, hence a common
local frame — and every file's contribution to a structure must receive
the same correction.

Moving the anchor to the geometry centroid does not work either.  A
placement carries exactly one elevation, and these objects span terrain
that varies by tens of metres; for ``007`` the centroid lands in the gap
between two building groups and is worse than the status quo.

What this does instead
----------------------
Rather than splitting the objects (which would rewrite the DSF and turn
one draw call into fifty), it bakes the correction into the geometry:
for each structure, add ``ground_under_structure - ground_under_anchor``
to the ``y`` coordinate of that structure's vertices.  X-Plane still
places the object at the anchor's elevation, but each structure now
carries an offset that lands it on its own terrain.

Only ``.obj`` files change — no DSF edit, no new objects, no extra draw
calls.  Elevations come from the built Ortho4XP mesh, so the correction
matches the terrain auto_patch actually produced.

Safety
------
Each file is copied to ``<name>.anchor_bak`` on first run.  Geometry is
always read from that backup and written to the live file, so repeated
runs re-derive the same result rather than stacking offsets.  ``--restore``
puts the originals back.

Choosing --gap
--------------
Components are merged into structures by single-link bounding-box
proximity.  Chaining makes this sensitive: at a 20 m gap the pooled
geometry collapses into 50 structures, one of which sprawls 2.2 km, and a
single elevation for it leaves sub-buildings up to 11 m off the ground.
At 2 m — "physically touching" — it resolves into 105 structures with an
area-weighted residual of 0.19 m.  Measured on this pack::

    gap    structures   worst residual   p95    area-weighted
    0.5m      116           7.93 m      1.09 m      0.18 m
    2.0m      105           7.93 m      1.13 m      0.19 m
    20.0m      50           7.53 m      2.34 m      0.40 m

The worst residual is stubborn because it belongs to a single genuinely
connected 513 m terminal complex sitting on 8 m of mesh slope.  A rigid
object cannot follow that, so it is irreducible here; auto_patch would
have to flatten a building pad beneath it (which is precisely what the
footprints this tool extracts would let it do).

Caveats
-------
* The correction is baked against one specific built mesh.  Rebuild the
  tile with different grading and it goes stale.
* One elevation per structure: a large building on a slope still meets
  the ground at a single height, exactly as a correctly anchored
  standalone object would.
* Draped (``ATTR_draped``) vertices are left alone — they conform to the
  terrain already.  These bakes contain none.

Staleness
---------
The offsets encode one specific built mesh.  Rebuild the tile — auto_patch
re-grades, the terrain moves — and every offset is wrong by however much
the ground shifted.  This is not hypothetical: the KCLT mesh was rebuilt
30 minutes after the first bake and the anchor's ground moved 1.19 m.

So applying records the mesh's identity in a provenance sidecar, and
``--check`` compares it against the mesh on disk.  Re-running is always
safe: geometry is read from the backup, never from the live file.

Usage:
    python3 tools/reanchor_kclt_terminal_bakes.py [--dry-run] [--gap 2]
    python3 tools/reanchor_kclt_terminal_bakes.py --check
    python3 tools/reanchor_kclt_terminal_bakes.py --restore
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC_DIR = os.path.join(os.path.dirname(_TOOLS_DIR), "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from auto_patch.dsf_reader import _load_dsf_text
from mesh_elevation_sampler import MeshElevationSampler
from obj8_geometry import (
    area_weighted_centroid,
    connected_components,
    group_components_into_structures,
    horizontal_bounding_box,
    load_object_file,
    local_offset_to_lonlat,
    read_dsf_object_placements,
)

XPLANE_ROOT = "/Users/noah/X-Plane 12"
PACK_ROOT = os.path.join(
    XPLANE_ROOT,
    "Custom Scenery",
    "Nimbus Simulation - KCLT V1.4 - Charlotte XP12",
)
DSF_PATH = os.path.join(PACK_ROOT, "Earth nav data", "+30-090", "+35-081.dsf")
MESH_PATH = os.path.join(
    XPLANE_ROOT, "Custom Scenery", "zOrtho4XP_+35-081", "Data+35-081.mesh"
)
CO_ANCHORED_RESOURCES = [
    f"Terminals/Hangar/Charlotte_Airport_00{index}_ALB.obj"
    for index in range(1, 9)
]
BACKUP_SUFFIX = ".anchor_bak"

# Anchors are considered shared below this separation, in degrees.
ANCHOR_AGREEMENT_TOLERANCE_DEGREES = 1e-6

# Merge components whose bounding boxes lie within this distance.  See the
# module docstring for the measurement behind the default.
DEFAULT_GAP_METRES = 2.0

# A structure whose lowest vertex sits above this height never touches the
# ground: it is rooftop clutter, a canopy, a jetbridge.  Re-grounding it on
# the terrain beneath it would detach it from whatever it rests on, so it
# inherits the correction of the ground-touching structure below it.
ELEVATED_BASE_THRESHOLD_METRES = 0.5


PROVENANCE_PATH = os.path.join(PACK_ROOT, ".o4_reanchor_provenance.json")


def _mesh_signature(mesh_path: str) -> dict:
    stat = os.stat(mesh_path)
    return {
        "mesh": mesh_path,
        "size": stat.st_size,
        "mtime": int(stat.st_mtime),
    }


def _check_provenance() -> int:
    """Report whether the baked offsets still match the mesh on disk."""
    if not os.path.isfile(PROVENANCE_PATH):
        print("no bake recorded (objects are original, or were restored)")
        return 0
    with open(PROVENANCE_PATH) as handle:
        recorded = json.load(handle)
    if not os.path.isfile(MESH_PATH):
        print(f"STALE: recorded mesh is gone: {recorded['mesh']}")
        return 1
    current = _mesh_signature(MESH_PATH)
    fields = ("size", "mtime")
    if all(recorded.get(field) == current[field] for field in fields):
        print(
            f"CURRENT: bake matches the mesh on disk "
            f"(gap {recorded.get('gap')} m, {recorded.get('structures')} structures)"
        )
        return 0
    print(
        "STALE: the mesh has been rebuilt since the bake.\n"
        f"  baked against size={recorded['size']} mtime={recorded['mtime']}\n"
        f"  on disk now     size={current['size']} mtime={current['mtime']}\n"
        "Re-run this tool (it reads from the backups) to re-bake."
    )
    return 1


def _backup_path(object_path: str) -> str:
    return object_path + BACKUP_SUFFIX


def _ensure_backup(object_path: str) -> str:
    """Copy the original aside once; thereafter it is the source of truth."""
    backup = _backup_path(object_path)
    if not os.path.exists(backup):
        shutil.copy2(object_path, backup)
    return backup


def _restore_all() -> int:
    restored = 0
    for resource in CO_ANCHORED_RESOURCES:
        object_path = os.path.join(PACK_ROOT, resource)
        backup = _backup_path(object_path)
        if os.path.exists(backup):
            shutil.copy2(backup, object_path)
            restored += 1
            print(f"  restored {resource}")
    if os.path.exists(PROVENANCE_PATH):
        os.remove(PROVENANCE_PATH)
    print(f"{restored} file(s) restored from {BACKUP_SUFFIX}")
    return 0


def _rewrite_vertex_elevations(
    source_path: str,
    destination_path: str,
    elevation_delta_by_vertex: dict[int, float],
) -> int:
    """Rewrite ``VT`` lines, adding a per-vertex offset to the y column.

    Every other line, and each ``VT`` line's whitespace and decimal
    precision, is preserved verbatim so the diff stays minimal.
    """
    output: list[str] = []
    vertex_index = 0
    moved = 0
    with open(source_path, errors="replace") as handle:
        for line in handle:
            if not line.split() or line.split()[0] != "VT":
                output.append(line)
                continue
            delta = elevation_delta_by_vertex.get(vertex_index)
            vertex_index += 1
            if delta is None or delta == 0.0:
                output.append(line)
                continue

            had_newline = line.endswith("\n")
            parts = re.split(r"([ \t]+)", line.rstrip("\n"))
            value_positions = [
                position
                for position, part in enumerate(parts)
                if position % 2 == 0 and part != ""
            ]
            # value_positions[0] is the "VT" keyword; then x, y, z.
            y_position = value_positions[2]
            original = parts[y_position]
            decimals = (
                len(original.split(".", 1)[1]) if "." in original else 6
            )
            parts[y_position] = f"{float(original) + delta:.{decimals}f}"
            output.append("".join(parts) + ("\n" if had_newline else ""))
            moved += 1

    with open(destination_path, "w") as handle:
        handle.writelines(output)
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gap", type=float, default=DEFAULT_GAP_METRES)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the baked offsets still match the mesh on disk",
    )
    args = parser.parse_args()

    if args.restore:
        return _restore_all()
    if args.check:
        return _check_provenance()

    for path in (DSF_PATH, MESH_PATH):
        if not os.path.isfile(path):
            print(f"missing: {path}", file=sys.stderr)
            return 2

    lines = _load_dsf_text(DSF_PATH, cache_dir=tempfile.gettempdir())
    if lines is None:
        print("DSFTool could not read the DSF", file=sys.stderr)
        return 2

    wanted = set(CO_ANCHORED_RESOURCES)
    placements = [
        placement
        for placement in read_dsf_object_placements(lines)
        if placement.resource_path in wanted
    ]
    found = {placement.resource_path for placement in placements}
    if found != wanted:
        print(f"expected 8 placements, found {sorted(found)}", file=sys.stderr)
        return 2

    anchor = placements[0]
    for placement in placements[1:]:
        if (
            abs(placement.latitude - anchor.latitude)
            > ANCHOR_AGREEMENT_TOLERANCE_DEGREES
            or abs(placement.longitude - anchor.longitude)
            > ANCHOR_AGREEMENT_TOLERANCE_DEGREES
            or abs(placement.heading_degrees - anchor.heading_degrees) > 1e-4
        ):
            print(
                "placements do not share an anchor — the pooled local frame "
                "assumption fails, refusing to edit",
                file=sys.stderr,
            )
            return 2
    print(
        f"shared anchor: {anchor.latitude:.9f}, {anchor.longitude:.9f} "
        f"heading {anchor.heading_degrees:.6f}\n"
    )

    # Pool every file's components into the shared local frame.
    geometry_by_resource = {}
    pooled: list[tuple[str, list[tuple[int, int, int]]]] = []
    for resource in CO_ANCHORED_RESOURCES:
        backup = _ensure_backup(os.path.join(PACK_ROOT, resource))
        geometry = load_object_file(backup)
        geometry_by_resource[resource] = geometry
        for component in connected_components(
            geometry.vertices, geometry.solid_triangles
        ):
            pooled.append((resource, component))
    print(f"pooled {len(pooled)} components from {len(wanted)} objects")

    # Group across files.  Offset each file's vertex indices into one
    # shared index space so the existing grouper can be reused verbatim.
    shared_vertices: list[tuple[float, float, float]] = []
    base_offset_by_resource: dict[str, int] = {}
    resource_of_shared_vertex: list[str] = []
    for resource in CO_ANCHORED_RESOURCES:
        base_offset_by_resource[resource] = len(shared_vertices)
        shared_vertices.extend(geometry_by_resource[resource].vertices)
        resource_of_shared_vertex.extend(
            [resource] * len(geometry_by_resource[resource].vertices)
        )

    shared_components = [
        [
            tuple(index + base_offset_by_resource[resource] for index in triangle)
            for triangle in component
        ]
        for resource, component in pooled
    ]
    structures = group_components_into_structures(
        shared_vertices, shared_components, args.gap
    )
    structures.sort(
        key=lambda structure: -area_weighted_centroid(
            shared_vertices, structure
        )[0]
    )
    print(f"grouped into {len(structures)} structures (gap < {args.gap:g} m)\n")

    reach = max(
        math.hypot(shared_vertices[index][0], shared_vertices[index][2])
        for structure in structures
        for triangle in structure
        for index in triangle
    )
    margin = (reach + 200.0) / 111320.0
    sampler = MeshElevationSampler(
        MESH_PATH,
        (
            anchor.longitude - margin,
            anchor.latitude - margin,
            anchor.longitude + margin,
            anchor.latitude + margin,
        ),
        margin_degrees=0.0,
    )
    anchor_elevation = sampler.elevation_at(anchor.latitude, anchor.longitude)
    print(f"ground under the anchor: {anchor_elevation:.2f} m\n")

    # Measure every structure before deciding any correction.
    profiles = []
    for structure in structures:
        area, centroid_x, centroid_z = area_weighted_centroid(
            shared_vertices, structure
        )
        latitude, longitude = local_offset_to_lonlat(
            anchor.latitude,
            anchor.longitude,
            anchor.heading_degrees,
            centroid_x,
            centroid_z,
        )
        used = {index for triangle in structure for index in triangle}
        base_y = min(shared_vertices[index][1] for index in used)
        profiles.append(
            {
                "structure": structure,
                "area": area,
                "centroid": (centroid_x, centroid_z),
                "box": horizontal_bounding_box(shared_vertices, structure),
                "ground": sampler.elevation_at(latitude, longitude),
                "latitude": latitude,
                "longitude": longitude,
                "grounded": base_y <= ELEVATED_BASE_THRESHOLD_METRES,
            }
        )

    grounded = [profile for profile in profiles if profile["grounded"]]
    if not grounded:
        print("no ground-touching structure found — refusing to edit", file=sys.stderr)
        return 2

    def _inherit_from_supporting_structure(elevated: dict) -> dict:
        """An elevated structure follows whatever it rests on: the
        ground-touching structure containing its centroid, else the nearest."""
        centroid_x, centroid_z = elevated["centroid"]
        for candidate in grounded:
            min_x, max_x, min_z, max_z = candidate["box"]
            if min_x <= centroid_x <= max_x and min_z <= centroid_z <= max_z:
                return candidate
        return min(
            grounded,
            key=lambda candidate: math.hypot(
                candidate["centroid"][0] - centroid_x,
                candidate["centroid"][1] - centroid_z,
            ),
        )

    # Map every shared vertex to its structure's correction.
    delta_by_shared_vertex: dict[int, float] = {}
    elevated_count = 0
    print(
        f"{'#':>3} {'area m2':>9} {'files':>6} {'ground m':>9} "
        f"{'offset':>8}  structure centroid"
    )
    for number, profile in enumerate(profiles):
        if profile["grounded"]:
            source = profile
        else:
            source = _inherit_from_supporting_structure(profile)
            elevated_count += 1
        delta = source["ground"] - anchor_elevation
        for triangle in profile["structure"]:
            for index in triangle:
                delta_by_shared_vertex[index] = delta

        contributing = {
            resource_of_shared_vertex[index]
            for triangle in profile["structure"]
            for index in triangle
        }
        if number < 12:
            print(
                f"{number:3d} {profile['area']:9.0f} {len(contributing):6d} "
                f"{profile['ground']:9.2f} {delta:+7.2f}m  "
                f"{profile['latitude']:.6f},{profile['longitude']:.6f}"
            )
    if len(structures) > 12:
        print(f"    ... {len(structures) - 12} more")
    if elevated_count:
        print(
            f"\n{elevated_count} elevated structure(s) inherited the offset of "
            "the structure supporting them."
        )

    offsets = [
        delta_by_shared_vertex[index] for index in delta_by_shared_vertex
    ]
    print(
        f"\noffsets: min {min(offsets):+.2f} m  max {max(offsets):+.2f} m  "
        f"span {max(offsets) - min(offsets):.2f} m"
    )

    if args.dry_run:
        print("\n--dry-run: no files written")
        return 0

    print()
    total_moved = 0
    for resource in CO_ANCHORED_RESOURCES:
        object_path = os.path.join(PACK_ROOT, resource)
        base = base_offset_by_resource[resource]
        count = len(geometry_by_resource[resource].vertices)
        local_deltas = {
            index - base: delta
            for index, delta in delta_by_shared_vertex.items()
            if base <= index < base + count
        }
        moved = _rewrite_vertex_elevations(
            _backup_path(object_path), object_path, local_deltas
        )
        total_moved += moved
        print(f"  {os.path.basename(resource):32} {moved:6d} vertices offset")
    provenance = _mesh_signature(MESH_PATH)
    provenance.update(
        {
            "gap": args.gap,
            "structures": len(structures),
            "vertices_offset": total_moved,
            "anchor": [anchor.latitude, anchor.longitude, anchor.heading_degrees],
            "anchor_ground": anchor_elevation,
            "objects": CO_ANCHORED_RESOURCES,
        }
    )
    with open(PROVENANCE_PATH, "w") as handle:
        json.dump(provenance, handle, indent=2)

    print(
        f"\n{total_moved} vertices rewritten across {len(wanted)} objects.\n"
        f"Originals kept as *{BACKUP_SUFFIX}; undo with --restore.\n"
        "Restart X-Plane (objects are cached) and look at KCLT."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
