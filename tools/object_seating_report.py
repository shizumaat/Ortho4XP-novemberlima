"""Per-structure seating report: predicted float/sink against the mesh.

For every pool the Phase 2 discovery pipeline would process, partition,
compute the per-(structure, object) offsets, and report — per structure —
the PREDICTED rendered seating error of its ground-touching parts:

    residual(part) = ground(anchor(object)) + base_y(part) + delta
                     − ground_under(part_centroid)

which is exactly what the simulator shows after the bake (up to DSF
elevation quantisation).  Structures are reported worst-first with
latitude/longitude, so an in-sim "that building floats" observation can
be matched to a row and its cause:

* ``span``      — ground variation across the structure (one rigid offset
                  cannot fix it; a Phase 1 pad can — flagged needs_pad)
* ``diameter``  — a huge diameter means transitive contact chaining
                  (for example buildings linked by an elevated railway)
* ``inherited`` — the structure has no ground contact and borrowed its
                  supporter's offset; a wrong supporter shows here
* ``skipped``   — never baked (multi-placement, animation, arithmetic)
* ``feet:N``    — foot-anchored (author-baked vertical offset; N foot
                  clusters); residuals come from the decision's per-foot
                  fit, so baked-offset objects are no longer invisible

The ``--mesh`` you pass MUST be the SAME built ``Data<tile>.mesh`` the
production pipeline seats objects against — i.e. one built from the
inset-corrected ``.alt`` (airport elevation insets baked in).  This tool
samples ONLY that mesh (``MeshElevationSampler``); it never re-samples a
raw DEM/.hgt.  Point it at a mesh built WITHOUT insets and the residuals
it reports will not match what production produces.

Usage:
    venv/bin/python tools/object_seating_report.py \
        --dsf <pack DSF> --mesh <inset-built Data<tile>.mesh> \
        --pack-root <pack> [--threshold 0.5] [--limit 40]
"""
import argparse
import math
import os
import sys

_TOOLS_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS_DIRECTORY)
for path in (os.path.join(_ROOT, "src"), _ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

METRES_PER_DEGREE_LATITUDE = 111320.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsf", required=True)
    parser.add_argument(
        "--mesh", required=True,
        help="built Data<tile>.mesh to seat against — MUST be built from "
             "the inset-corrected .alt so the report matches production; "
             "this tool samples only this mesh, never a raw DEM")
    parser.add_argument("--pack-root", required=True)
    parser.add_argument("--xplane-root", default=None)
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="report structures whose worst part residual "
                             "exceeds this (metres)")
    parser.add_argument("--limit", type=int, default=40)
    arguments = parser.parse_args()

    os.environ.setdefault("O4_DSF_OBJECT_BUILDINGS", "1")
    from auto_patch import obj8_reader, object_anchor, post_mesh
    from auto_patch.mesh_sampler import MeshElevationSampler
    from auto_patch.obj8_partition import weld_parts

    result = post_mesh.discover_and_rebake_airport(
        arguments.dsf, arguments.mesh, arguments.pack_root,
        arguments.xplane_root, write_changes=False)

    print(f"skipped ({len(result['skipped'])}):")
    for resource_or_dsf, reason in result["skipped"]:
        print(f"  {os.path.basename(resource_or_dsf):44} {reason[:90]}")
    print()
    print(f"partially baked ({len(result['partially_baked'])}) — passing "
          "structures bake, skipped structures stay authored:")
    for resource, summary in result["partially_baked"]:
        print(f"  {os.path.basename(resource):44} {summary[:90]}")
    print()

    # Per-STRUCTURE skips (skip_reason set on the structure, not the
    # resource): the rigid-seat span limit leaves a whole chained contact
    # component at its authored elevations.  These never appear in the
    # resource-level "skipped" list above, so surface them here with their
    # centroid and span so a span-limited mega component is visible.
    structure_skips = [
        structure
        for _pool, decision in result["decisions"]
        for structure in decision.structures
        if structure.skip_reason
    ]
    print(f"structures left at authored elevations ({len(structure_skips)}) — "
          "per-structure skip_reason:")
    for structure in structure_skips:
        span = structure.ground_span_metres or 0.0
        print(f"  {structure.centroid_latitude:.6f}, "
              f"{structure.centroid_longitude:.6f}  span {span:6.2f}  "
              f"{(structure.skip_reason or '')[:80]}")
    print()

    rows = []
    for pool, decision in result["decisions"]:
        # Re-load geometry the same way discovery did (backup preferred).
        geometry_by_resource = {}
        for resource, physical in pool.resolved_paths.items():
            backup = physical + ".anchor_bak"
            source = backup if os.path.isfile(backup) else physical
            geometry_by_resource[resource] = (
                post_mesh.dsf_reader._load_object_geometry(source))

        bounds = post_mesh._pool_world_bounds(pool, geometry_by_resource)
        sampler = MeshElevationSampler(arguments.mesh, bounds)
        placement_by_resource = {
            placement.resource_path: placement
            for placement in pool.placements}

        for structure_index, structure in enumerate(decision.structures):
            if structure.skip_reason:
                rows.append((math.inf, 0.0, structure, "skipped", 0.0, 0.0))
                continue
            feet = decision.foot_clusters_by_structure_index.get(
                structure_index)
            if feet:
                # Foot-anchored structure (author-baked vertical offset;
                # every part sits above the absolute elevated threshold,
                # so the per-part sweep below is blind to it).  The
                # decision already carries per-foot residuals.
                worst = 0.0
                for foot in feet:
                    if foot.residual_metres is not None and (
                            abs(foot.residual_metres) > abs(worst)):
                        worst = foot.residual_metres
                latitudes = [foot.latitude for foot in feet]
                longitudes = [foot.longitude for foot in feet]
                diameter = math.hypot(
                    (max(latitudes) - min(latitudes))
                    * METRES_PER_DEGREE_LATITUDE,
                    (max(longitudes) - min(longitudes))
                    * METRES_PER_DEGREE_LATITUDE
                    * math.cos(math.radians(latitudes[0])))
                rows.append((abs(worst), worst, structure,
                             f"feet:{len(feet)}", diameter,
                             structure.ground_span_metres or 0.0))
                continue
            worst = 0.0
            diameter_points = []
            for resource, triangles in (
                    structure.triangles_by_resource.items()):
                geometry = geometry_by_resource.get(resource)
                placement = placement_by_resource.get(resource)
                deltas = decision.delta_by_resource_and_vertex.get(
                    resource, {})
                anchor_ground = decision.anchor_ground_by_resource.get(
                    resource)
                if geometry is None or placement is None \
                        or anchor_ground is None:
                    continue
                # Per PART: weld this structure's triangles, take each
                # part's centroid + base.
                for part in weld_parts(geometry.vertices, triangles):
                    used = sorted({i for t in part for i in t})
                    base_y = min(geometry.vertices[i][1] for i in used)
                    if base_y > 0.5:
                        continue  # elevated part, rests on the structure
                    centroid_x = sum(
                        geometry.vertices[i][0] for i in used) / len(used)
                    centroid_z = sum(
                        geometry.vertices[i][2] for i in used) / len(used)
                    latitude, longitude = obj8_reader.local_offset_to_lonlat(
                        placement.latitude, placement.longitude,
                        placement.heading_degrees, centroid_x, centroid_z)
                    diameter_points.append((latitude, longitude))
                    part_ground = sampler.elevation_at_or_none(
                        latitude, longitude)
                    if part_ground is None:
                        continue
                    delta = deltas.get(used[0], 0.0)
                    residual = (anchor_ground + base_y + delta
                                - part_ground)
                    if abs(residual) > abs(worst):
                        worst = residual
            if not diameter_points:
                continue
            latitudes = [p[0] for p in diameter_points]
            longitudes = [p[1] for p in diameter_points]
            diameter = math.hypot(
                (max(latitudes) - min(latitudes))
                * METRES_PER_DEGREE_LATITUDE,
                (max(longitudes) - min(longitudes))
                * METRES_PER_DEGREE_LATITUDE
                * math.cos(math.radians(latitudes[0])))
            kind = ("inherited"
                    if structure.inherited_from_structure_index is not None
                    else ("needs_pad" if structure.needs_pad else ""))
            rows.append((abs(worst), worst, structure, kind, diameter,
                         structure.ground_span_metres or 0.0))

    rows.sort(key=lambda row: -row[0])
    offenders = [row for row in rows
                 if row[0] != math.inf and row[0] > arguments.threshold]
    baked = [row for row in rows if row[0] != math.inf]
    print(f"structures evaluated: {len(baked)}   "
          f"predicted |seating error| > {arguments.threshold} m: "
          f"{len(offenders)}")
    print(f"{'worst':>7}  {'span':>6} {'diam':>7}  {'kind':10} "
          f"latitude, longitude")
    for magnitude, signed, structure, kind, diameter, span in (
            offenders[: arguments.limit]):
        print(f"{signed:+7.2f}  {span:6.2f} {diameter:7.1f}  {kind:10} "
              f"{structure.centroid_latitude:.6f}, "
              f"{structure.centroid_longitude:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
