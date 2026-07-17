"""Re-anchor DSF scenery objects against a built Ortho4XP mesh (command line).

Generalises — and replaces — the KCLT-hard-coded prototype
``tools/reanchor_kclt_terminal_bakes.py``.  The library implementation
lives in ``src/auto_patch/`` (``post_mesh``, ``object_anchor``,
``object_rebake``); this tool is a thin argument parser over the exact
discovery pipeline the post-mesh build hook runs, so the two can never
drift (``post_mesh.discover_and_rebake_airport`` is shared, not copied).

The problem it fixes: X-Plane elevates a scenery object at its placement
ANCHOR, not at its geometry.  Scenery authors bake many buildings into
one ``.obj`` whose anchor may sit hundreds of metres from any vertex
(693 m at KCLT), so every structure inherits the terrain elevation under
that one distant point and floats or sinks by however much the ground
varies.  The fix bakes ``ground_under(structure) − ground_under(anchor)``
into each structure's ``y`` coordinates, per (structure, object) pair.

Writes are IN PLACE into the scenery pack with ``<name>.anchor_bak``
originals (ruling R1); geometry is always re-read from the backup, so
re-running is byte-idempotent and cannot stack.  Corrected packs MUST
NOT be redistributed.  X-Plane caches objects — restart it after a bake.

The mesh this tool seats against (``--mesh``, or the tile-derived default)
MUST be the inset-built ``Data<tile>.mesh`` — the one produced from the
inset-corrected ``.alt`` (airport elevation insets baked in).  Discovery
samples ONLY that mesh (``post_mesh.discover_and_rebake_airport`` ->
``MeshElevationSampler``); it never re-samples a raw DEM/.hgt, so the tool
moves objects against exactly the surface the pipeline moves them against.

Two input modes:

* ``--worklist PATH`` — the per-tile sidecar the auto_patch driver
  writes (``Patches/<tile>/o4_object_anchor_worklist.json``); every
  airport in it is processed.  The mesh path is derived from the
  worklist's tile name (``Tiles/zOrtho4XP_<tile>/Data<tile>.mesh``)
  unless ``--mesh`` overrides it.
* ``--dsf PATH --mesh PATH --pack-root PATH [--xplane-root PATH]`` —
  one airport named explicitly, no worklist required.

Actions (default is to apply the bake):

* ``--dry-run``  — run discovery and report the offsets; write nothing.
* ``--check``    — compare the provenance sidecar against the mesh on
  disk per pack: CURRENT, STALE (mesh rebuilt since the bake — re-run)
  or NONE.
* ``--restore``  — put the ``.anchor_bak`` originals back byte-identically
  and remove the provenance sidecar, per pack.

Usage examples (the KCLT paths are EXAMPLES only — any pack works)::

    venv/bin/python tools/reanchor_dsf_objects.py \\
        --worklist "Patches/+30-090/+35-081/o4_object_anchor_worklist.json"

    venv/bin/python tools/reanchor_dsf_objects.py \\
        --dsf "/Users/noah/X-Plane 12/Custom Scenery/Nimbus Simulation - KCLT V1.4 - Charlotte XP12/Earth nav data/+30-090/+35-081.dsf" \\
        --mesh "Tiles/zOrtho4XP_+35-081/Data+35-081.mesh" \\
        --pack-root "/Users/noah/X-Plane 12/Custom Scenery/Nimbus Simulation - KCLT V1.4 - Charlotte XP12" \\
        --xplane-root "/Users/noah/X-Plane 12" \\
        --dry-run

    venv/bin/python tools/reanchor_dsf_objects.py \\
        --dsf ... --mesh ... --pack-root ... --check

    venv/bin/python tools/reanchor_dsf_objects.py \\
        --dsf ... --mesh ... --pack-root ... --restore
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_TOOLS_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
_SOURCE_DIRECTORY = os.path.join(os.path.dirname(_TOOLS_DIRECTORY), "src")
if _SOURCE_DIRECTORY not in sys.path:
    sys.path.insert(0, _SOURCE_DIRECTORY)

from auto_patch import object_rebake, post_mesh  # noqa: E402


def _mesh_path_for_tile_name(tile_name: str) -> str:
    """``+35-081`` → ``Tiles/zOrtho4XP_+35-081/Data+35-081.mesh`` (the
    default build directory; pass ``--mesh`` for a custom one)."""
    import O4_File_Names as FNAMES

    latitude = int(tile_name[:3])
    longitude = int(tile_name[3:])
    return FNAMES.mesh_file(
        FNAMES.build_dir(latitude, longitude, ""), latitude, longitude
    )


def _collect_targets(arguments) -> list[dict]:
    """Normalise both input modes into
    ``[{label, dsf_path, mesh_path, pack_root, xplane_root}]``."""
    if arguments.worklist:
        with open(arguments.worklist) as handle:
            worklist = json.load(handle)
        if arguments.mesh:
            mesh_path = arguments.mesh
        else:
            try:
                mesh_path = _mesh_path_for_tile_name(
                    worklist.get("tile", ""))
            except (TypeError, ValueError, IndexError):
                mesh_path = None  # --restore never needs it
        targets = []
        for airport in worklist.get("airports", []):
            targets.append(
                {
                    "label": airport.get("icao", "?"),
                    "dsf_path": airport["dsf_path"],
                    "mesh_path": mesh_path,
                    "pack_root": airport["pack_root"],
                    "xplane_root": airport.get("xplane_root")
                    or worklist.get("xplane_root"),
                }
            )
        return targets
    return [
        {
            "label": os.path.basename(arguments.pack_root.rstrip("/"))
            or arguments.pack_root,
            "dsf_path": arguments.dsf,
            "mesh_path": arguments.mesh,
            "pack_root": arguments.pack_root,
            "xplane_root": arguments.xplane_root,
        }
    ]


def _report_dry_run_decisions(target: dict, result: dict) -> None:
    for _pool, decision in result["decisions"]:
        for resource_path, deltas in sorted(
            decision.delta_by_resource_and_vertex.items()
        ):
            if not deltas:
                continue
            distinct_offsets = sorted({round(delta, 3) for delta in deltas.values()})
            preview = ", ".join(f"{offset:+.3f}" for offset in distinct_offsets[:8])
            if len(distinct_offsets) > 8:
                preview += ", ..."
            print(
                f"    {resource_path}: {len(deltas)} vertices, "
                f"{len(distinct_offsets)} distinct offset(s) [{preview}] m"
            )
        for structure in decision.structures:
            if structure.needs_pad:
                print(
                    "    needs pad: structure at "
                    f"({structure.centroid_latitude:.6f}, "
                    f"{structure.centroid_longitude:.6f}), ground span "
                    f"{structure.ground_span_metres:.2f} m"
                )
            if structure.skip_reason:
                print(
                    "    structure skipped: "
                    f"({structure.centroid_latitude:.6f}, "
                    f"{structure.centroid_longitude:.6f}): "
                    f"{structure.skip_reason}"
                )


def main(argument_list: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bake per-structure terrain offsets into DSF scenery-object "
            ".obj files against a built Ortho4XP mesh (in place, "
            "reversible; see the module docstring)."
        )
    )
    parser.add_argument(
        "--worklist",
        help="per-tile worklist sidecar written by the auto_patch driver "
        "(mode 1; processes every airport in it)",
    )
    parser.add_argument("--dsf", help="DSF file to discover placements from "
                        "(mode 2)")
    parser.add_argument(
        "--mesh",
        help="built Data<tile>.mesh (required in mode 2; optional "
        "override of the tile-derived default in mode 1).  MUST be the "
        "inset-built mesh (from the inset-corrected .alt) so the bake "
        "seats objects against the same surface production uses; the "
        "shared discovery pipeline samples only this mesh, never a raw DEM",
    )
    parser.add_argument("--pack-root", help="scenery-pack directory whose "
                        ".obj files are rewritten (mode 2)")
    parser.add_argument("--xplane-root", help="X-Plane root, for "
                        "library.txt resource resolution (mode 2, optional)")
    parser.add_argument(
        "--epsilon",
        type=float,
        default=None,
        help="contact-graph epsilon in metres (default: "
        "config.DSF_OBJECT_CONTACT_EPSILON_M)",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="report the offsets; write nothing")
    parser.add_argument("--check", action="store_true",
                        help="report provenance freshness per pack "
                        "(CURRENT / STALE / NONE)")
    parser.add_argument("--restore", action="store_true",
                        help="put the .anchor_bak originals back and "
                        "remove the provenance sidecar, per pack")
    arguments = parser.parse_args(argument_list)

    if bool(arguments.worklist) == bool(arguments.dsf):
        parser.error("exactly one of --worklist or --dsf is required")
    if arguments.dsf and not arguments.pack_root:
        parser.error("--dsf mode requires --pack-root")
    if arguments.dsf and not arguments.mesh and not arguments.restore:
        parser.error("--dsf mode requires --mesh (except with --restore)")
    if sum(map(bool, (arguments.dry_run, arguments.check,
                      arguments.restore))) > 1:
        parser.error("--dry-run, --check and --restore are mutually "
                     "exclusive")

    targets = _collect_targets(arguments)
    if not targets:
        print("nothing to do: the worklist lists no airports")
        return 0

    if arguments.restore:
        restored_pack_roots = set()
        for target in targets:
            pack_root = target["pack_root"]
            if pack_root in restored_pack_roots:
                continue
            restored_pack_roots.add(pack_root)
            restored_count = object_rebake.restore(pack_root)
            print(f"{target['label']}: restored {restored_count} file(s) "
                  f"in {pack_root}")
        return 0

    if arguments.check:
        for target in targets:
            if not target["mesh_path"]:
                print(f"{target['label']}: mesh path unknown — pass --mesh")
                continue
            status = object_rebake.check(
                target["pack_root"], target["mesh_path"]
            )
            print(f"{target['label']}: {status} ({target['pack_root']})")
        return 0

    exit_code = 0
    for target in targets:
        if not target["mesh_path"] or not os.path.isfile(
                target["mesh_path"]):
            print(f"{target['label']}: mesh not found: "
                  f"{target['mesh_path']}")
            exit_code = 1
            continue
        if not os.path.isfile(target["dsf_path"]):
            print(f"{target['label']}: DSF not found: {target['dsf_path']}")
            exit_code = 1
            continue
        result = post_mesh.discover_and_rebake_airport(
            target["dsf_path"],
            target["mesh_path"],
            target["pack_root"],
            target["xplane_root"],
            epsilon_metres=arguments.epsilon,
            write_changes=not arguments.dry_run,
        )
        if arguments.dry_run:
            # Nothing was written; count what WOULD be from the decisions.
            resources_with_offsets = set()
            vertices_with_offsets = 0
            for _pool, decision in result["decisions"]:
                for resource_path, deltas in (
                    decision.delta_by_resource_and_vertex.items()
                ):
                    if deltas:
                        resources_with_offsets.add(resource_path)
                        vertices_with_offsets += len(deltas)
            object_file_count = len(resources_with_offsets)
            vertex_count = vertices_with_offsets
            action = "would bake"
        else:
            object_file_count = len(result["objects_written"])
            vertex_count = result["vertices_offset"]
            action = "baked"
        print(
            f"{target['label']}: {action} "
            f"{result['structures_baked']} structure(s) across "
            f"{object_file_count} object file(s), "
            f"{vertex_count} vertices offset, "
            f"{result['structures_needing_pad']} needing a pad, "
            f"{len(result['skipped'])} skipped"
        )
        for resource_path, reason in result["skipped"]:
            print(f"    skipped {resource_path}: {reason}")
        for resource_path, summary in result["partially_baked"]:
            print(f"    partially baked {resource_path}: {summary}")
        if arguments.dry_run:
            _report_dry_run_decisions(target, result)
        elif result["objects_written"]:
            print(f"    restart X-Plane (objects are cached): "
                  f"{target['pack_root']}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
