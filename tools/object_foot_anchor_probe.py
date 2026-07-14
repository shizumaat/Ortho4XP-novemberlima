"""Targeted foot re-anchor probe for named DSF objects.

Runs the Phase 2 discovery chain (placements → pools → partition →
``structure_deltas``) for ONLY the resources matching a substring, then
prints — per structure — the detected foot clusters, the fitted rigid
offset, and each foot's predicted rendered residual against the mesh:

    residual(foot) = ground(anchor) + base_y(foot) + delta
                     − ground_under(foot_centroid)

This is the fast-harness tier for the multi-ground-cluster re-anchor
work (project memory kbna-gantry-pond-multi-foot-objects): it answers
"do the KBNA stairs' two feet seat?" in seconds, without a full-airport
``discover_and_rebake_airport`` sweep.  Read-only: nothing on disk is
touched; geometry is read from ``.anchor_bak`` originals when present.

The ``--mesh`` MUST be the same built ``Data<tile>.mesh`` production
seats against (inset-corrected ``.alt``); see
``tools/object_seating_report.py`` for the full-airport audit.

Usage:
    venv/bin/python tools/object_foot_anchor_probe.py \
        --dsf <pack DSF> --mesh <built Data<tile>.mesh> \
        --pack-root <pack> --resource-substring Stair_45m \
        [--xplane-root <root>]
"""
import argparse
import os
import sys

_TOOLS_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS_DIRECTORY)
for path in (os.path.join(_ROOT, "src"), _ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsf", required=True)
    parser.add_argument("--mesh", required=True)
    parser.add_argument("--pack-root", required=True)
    parser.add_argument("--xplane-root", default=None)
    parser.add_argument(
        "--resource-substring", action="append", required=True,
        help="probe resources whose path contains this (repeatable)")
    arguments = parser.parse_args()

    from auto_patch import dsf_reader, obj8_reader, object_anchor
    from auto_patch import object_rebake, post_mesh
    from auto_patch.config import DSF_OBJECT_CONTACT_EPSILON_M
    from auto_patch.mesh_sampler import MeshElevationSampler

    lines = dsf_reader._load_dsf_text(arguments.dsf)
    if not lines:
        print("DSF text unavailable (missing file or DSFTool)")
        return 1
    wanted = arguments.resource_substring
    placements = obj8_reader.read_dsf_object_placements(
        lines,
        accept_resource=lambda resource: any(
            substring in resource for substring in wanted),
    )
    if not placements:
        print("no matching placements in the DSF")
        return 1
    placement_count: dict[str, int] = {}
    for placement in placements:
        placement_count[placement.resource_path] = (
            placement_count.get(placement.resource_path, 0) + 1)
    for resource_path, count in sorted(placement_count.items()):
        note = "" if count == 1 else "  (MULTI-PLACEMENT: invariant I-4 " \
            "excludes this resource from Phase 2)"
        print(f"placements {count}  {resource_path}{note}")
    placements = [placement for placement in placements
                  if placement_count[placement.resource_path] == 1]
    if not placements:
        return 1

    resolved_paths: dict[str, str] = {}
    geometry_by_resource: dict = {}
    for resource_path in sorted({p.resource_path for p in placements}):
        physical_path = obj8_reader.resolve_object_resource(
            resource_path, arguments.pack_root, arguments.xplane_root)
        if physical_path is None:
            print(f"unresolved: {resource_path}")
            continue
        backup_path = physical_path + object_rebake.BACKUP_SUFFIX
        source = backup_path if os.path.isfile(backup_path) else physical_path
        geometry = dsf_reader._load_object_geometry(source)
        if geometry is None or not geometry.has_solid_geometry:
            print(f"no solid geometry: {resource_path}")
            continue
        resolved_paths[resource_path] = physical_path
        geometry_by_resource[resource_path] = geometry
    placements = [placement for placement in placements
                  if placement.resource_path in resolved_paths]
    if not placements:
        return 1

    pools = object_anchor.discover_object_pools(
        placements, resolved_paths, geometry_by_resource,
        epsilon_metres=DSF_OBJECT_CONTACT_EPSILON_M)
    for pool in pools:
        pool_geometry = {resource: geometry_by_resource[resource]
                         for resource in pool.resolved_paths}
        bounds = post_mesh._pool_world_bounds(pool, pool_geometry)
        sampler = MeshElevationSampler(arguments.mesh, bounds)
        structures = object_anchor.partition_structures(
            pool, pool_geometry,
            epsilon_metres=DSF_OBJECT_CONTACT_EPSILON_M)
        decision = object_anchor.structure_deltas(
            pool, pool_geometry, structures, sampler)

        print(f"\npool: {sorted(pool.resolved_paths)}")
        for resource_path, reason in decision.skipped:
            print(f"  skipped {resource_path}: {reason}")
        for structure_index, structure in enumerate(decision.structures):
            resources = sorted(structure.triangles_by_resource)
            print(f"  structure {structure_index}: {resources}, "
                  f"ground_touching={structure.is_ground_touching}, "
                  f"span={structure.ground_span_metres}, "
                  f"needs_pad={structure.needs_pad}, "
                  f"inherited_from="
                  f"{structure.inherited_from_structure_index}, "
                  f"skip={structure.skip_reason}")
            feet = decision.foot_clusters_by_structure_index.get(
                structure_index)
            if not feet:
                continue
            for foot in feet:
                residual = ("None" if foot.residual_metres is None
                            else f"{foot.residual_metres:+.3f}")
                print(f"    foot base_y={foot.base_y:+8.3f} "
                      f"contacts={len(foot.contact_points):3d} "
                      f"({foot.latitude:.7f}, {foot.longitude:.7f}) "
                      f"ground={foot.ground_metres:.3f} "
                      f"kept={foot.kept_for_fit} residual={residual} m")
        for request in decision.foot_pad_requests:
            print(f"  PAD REQUEST structure {request.structure_index} "
                  f"foot at ({request.latitude:.7f}, "
                  f"{request.longitude:.7f}): residual "
                  f"{request.residual_metres:+.3f} m, target ground "
                  f"{request.target_ground_metres:.3f} m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
