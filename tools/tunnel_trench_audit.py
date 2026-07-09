"""Audit a classified tunnel trench against a BUILT Ortho4XP mesh.

Workstream W-V of ``docs/object_terrain_features_spec.md`` (section 4, the
``tunnel_trench_audit`` acceptance tool).  Feature A
(``O4_OBJECT_TUNNEL_TERRAIN``) cuts an open trench over a tunnel's whole
deck footprint: a flat floor strictly below the object's road deck, a rim
at the anchor datum, near-vertical node-split walls, and NO sub-datum
terrain outside the footprint (spec sections 2.4, 3.3).  This tool
classifies the pack's tunnel objects (importing the pure classifier — never
editing it), then grid-samples the built ``Data<tile>.mesh`` over each
tunnel footprint and a rim ring and checks the numbers against the
object-implied expectation.

The expectation, and why it is computed here rather than imported
-----------------------------------------------------------------
The lockstep grade law ``grade_law.tunnel_trench`` that the emitter and
validator will both import does NOT exist yet (workstream W-T is pending),
so the OBJECT-implied expectation is computed directly in this tool, in one
clearly-marked function (:func:`object_implied_trench_floor`) carrying a
TODO-W-T note.  Its physics (spec section 2.4 point 3, amendment A1):

    rim   = datum (the anchor seat elevation)
    floor = datum - body_depth - TUNNEL_FLOOR_BELOW_OBJECT_DECK_M

The mesh floor sits STRICTLY below the OBJ8 road deck — the deck carries
the visible road, the mesh only clears it (the author's own mesh runs
~1 m below the deck; the project constant is 0.5 m).  The audit reports:

    floor      built-mesh minimum over the footprint versus the expected
               floor (spec KBNA-analogue tolerance +/- 0.25 m).
    rim        built-mesh elevation on a ring just outside the footprint
               versus datum (+/- 0.25 m).
    spill      count of ring/margin samples that dip a metre below datum —
               sub-datum terrain OUTSIDE the footprint, which must be zero.

With ``--author-mesh`` the tool also parses the correlating author base
mesh (via the R11 oracle's parser) and reports the datum delta (our solved
datum versus the author's flattened platform) and the floor delta — open
question 1's quantification, per amendment A1 / R11.

Sampling plumbing only, until a gated tile is built
---------------------------------------------------
Feature A is default OFF and no gated tile has been built.  Against an
UNGRADED Ortho4XP mesh the trench has not been cut, so floor==rim==terrain
and the audit reports FAIL — expected, and it still exercises the whole
sampling path.  Point ``--mesh`` at a feature-A build for a real verdict.

Usage:
    venv/bin/python tools/tunnel_trench_audit.py <tile.dsf | pack_root>
                                    --mesh Data<tile>.mesh
                                    [--author-mesh author_base_mesh.dsf]
                                    [--xplane-root DIR]
                                    [--grid 24] [--tolerance 0.25]

Example (EGLL, with the correlating author mesh for the datum delta):
    venv/bin/python tools/tunnel_trench_audit.py \
        "$XP/Custom Scenery/c_GBR - 100_airport - EGLL_LONDON_TAIMODELS/Earth nav data/+50-010/+51-001.dsf" \
        --mesh "$XP/Custom Scenery/zOrtho4XP_+51-001/Data+51-001.mesh" \
        --author-mesh "/Volumes/.../c_GBR - 100_airport - EGLL_MESH/Earth nav data/+50-010/+51-001.dsf"
"""

from __future__ import annotations

import argparse
import math
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
    frame_polygon_to_longitude_latitude,
)
# The R11 oracle owns the author base-mesh parser; reuse it (spec: "the
# datum delta versus an author mesh ... see tool 4's parser").
from custom_mesh_cutout_oracle import (
    AuthorMesh,
    parse_author_mesh_triangles,
    _dump_mesh_text,
)

MAXIMUM_PLACEMENTS_PER_RESOURCE = 50

# TODO-W-T: this constant is the project value the spec assigns to
# ``grade_law.TUNNEL_FLOOR_BELOW_OBJECT_DECK_M`` (spec section 3.3 point 3 /
# amendment A1).  When W-T lands the lockstep law
# ``grade_law.tunnel_trench(datum, trench_polygon, deck_depth_samples)`` and
# its constant, import BOTH from ``grade_law`` and delete this local copy so
# the audit and the emitter can never drift.  Kept local now only because
# ``grade_law`` does not yet carry the function.
TUNNEL_FLOOR_BELOW_OBJECT_DECK_M = 0.5

# A ring sample dipping this far below datum is "spill" — sub-datum terrain
# outside the footprint, which the trench must not create.
SPILL_BELOW_DATUM_M = 1.0


def object_implied_trench_floor(datum_m: float, body_depth_m: float) -> float:
    """OBJECT-implied trench floor elevation (spec section 2.4 point 3).

    TODO-W-T: replace the whole function with a call into
    ``grade_law.tunnel_trench`` once workstream W-T lands that lockstep law
    — do NOT guess its final signature here.  Until then the expectation is
    the measured contract directly: the mesh floor sits
    :data:`TUNNEL_FLOOR_BELOW_OBJECT_DECK_M` below the object's road deck,
    which itself sits ``body_depth`` below the datum."""
    return datum_m - body_depth_m - TUNNEL_FLOOR_BELOW_OBJECT_DECK_M


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


def _classify_tunnels(dsf_path: str, xplane_root: str | None):
    lines = dsf_reader._load_dsf_text(dsf_path, cache_dir=tempfile.gettempdir())
    if lines is None:
        raise SystemExit("DSFTool could not read the DSF")
    all_placements = obj8_reader.read_dsf_object_placements(
        lines,
        accept_resource=lambda resource: resource.lower().endswith(".obj"),
        include_object_msl=True,
    )
    terrain_placements = [
        placement for placement in all_placements
        if placement.placement_kind != "OBJECT_MSL"]
    mean_sea_level_placements = [
        placement for placement in all_placements
        if placement.placement_kind == "OBJECT_MSL"]
    pack_root = dsf_reader._pack_root_for_dsf(dsf_path) or ""

    placement_count: dict[str, int] = {}
    for placement in terrain_placements:
        placement_count[placement.resource_path] = (
            placement_count.get(placement.resource_path, 0) + 1)
    geometry_by_resource: dict = {}
    for resource_path in sorted(
            {placement.resource_path for placement in terrain_placements}):
        if placement_count[resource_path] > MAXIMUM_PLACEMENTS_PER_RESOURCE:
            continue
        physical_path = obj8_reader.resolve_object_resource(
            resource_path, pack_root, xplane_root)
        if physical_path is None:
            continue
        geometry = dsf_reader._load_object_geometry(physical_path)
        if geometry is None or not geometry.has_solid_geometry:
            continue
        geometry_by_resource[resource_path] = geometry

    result = object_terrain_features.classify_object_terrain_features(
        terrain_placements, geometry_by_resource,
        mean_sea_level_placements=mean_sea_level_placements,
        pack_root=pack_root)
    return result, terrain_placements


def _footprint_longitude_latitude(tunnel):
    if tunnel.deck_footprint is None or tunnel.deck_footprint.is_empty:
        return None
    polygon = frame_polygon_to_longitude_latitude(
        tunnel.deck_footprint, tunnel.frame_origin_longitude_latitude)
    if polygon.geom_type == "MultiPolygon":
        polygon = max(polygon.geoms, key=lambda part: part.area)
    return polygon


def _grid_sample(sampler, polygon, grid):
    from shapely.geometry import Point

    minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = (
        polygon.bounds)
    values = []
    interior = 0
    for row in range(grid):
        for column in range(grid):
            longitude = minimum_longitude + (column + 0.5) / grid * (
                maximum_longitude - minimum_longitude)
            latitude = minimum_latitude + (row + 0.5) / grid * (
                maximum_latitude - minimum_latitude)
            if not polygon.contains(Point(longitude, latitude)):
                continue
            interior += 1
            elevation = sampler.elevation_at_or_none(latitude, longitude)
            if elevation is not None:
                values.append(elevation)
    return values, interior


def _ring_sample(sampler, polygon, margin_metres):
    """Elevations sampled on a ring ``margin_metres`` outside the footprint
    bounding box."""
    minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = (
        polygon.bounds)
    center_latitude = (minimum_latitude + maximum_latitude) / 2.0
    margin_latitude = margin_metres / 111320.0
    margin_longitude = margin_metres / (
        111320.0 * max(math.cos(math.radians(center_latitude)), 1e-6))
    values = []
    steps = 16
    for step in range(steps):
        fraction = step / (steps - 1)
        longitude_at = minimum_longitude + fraction * (
            maximum_longitude - minimum_longitude)
        latitude_at = minimum_latitude + fraction * (
            maximum_latitude - minimum_latitude)
        for latitude, longitude in (
            (minimum_latitude - margin_latitude, longitude_at),
            (maximum_latitude + margin_latitude, longitude_at),
            (latitude_at, minimum_longitude - margin_longitude),
            (latitude_at, maximum_longitude + margin_longitude),
        ):
            elevation = sampler.elevation_at_or_none(latitude, longitude)
            if elevation is not None:
                values.append(elevation)
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dsf", help="tile DSF file OR pack root directory")
    parser.add_argument("--mesh", required=True,
                        help="built Data<tile>.mesh to sample")
    parser.add_argument("--author-mesh", default=None,
                        help="correlating author base-mesh DSF (or .txt) for "
                        "the datum delta")
    parser.add_argument("--xplane-root", default=None)
    parser.add_argument("--grid", type=int, default=24)
    parser.add_argument("--tolerance", type=float, default=0.25)
    parser.add_argument("--margin-metres", type=float, default=15.0)
    args = parser.parse_args()

    dsf_path = _resolve_dsf_path(args.dsf)
    if dsf_path is None:
        print(f"no DSF found at or under {args.dsf}", file=sys.stderr)
        return 2
    xplane_root = args.xplane_root or _default_xplane_root(dsf_path)

    result, terrain_placements = _classify_tunnels(dsf_path, xplane_root)
    print(f"DSF:  {dsf_path}")
    print(f"mesh: {args.mesh}")
    print(f"{len(result.tunnels)} tunnel(s) classified\n")
    if not result.tunnels:
        print("no tunnels to audit")
        return 0

    from auto_patch.mesh_sampler import MeshElevationSampler

    longitudes = [placement.longitude for placement in terrain_placements]
    latitudes = [placement.latitude for placement in terrain_placements]
    sampler = MeshElevationSampler(
        args.mesh,
        (min(longitudes), min(latitudes), max(longitudes), max(latitudes)),
        margin_degrees=0.03)

    author_mesh = None
    if args.author_mesh:
        text_path = _dump_mesh_text(args.author_mesh)
        bounds = (min(longitudes) - 0.02, min(latitudes) - 0.02,
                  max(longitudes) + 0.02, max(latitudes) + 0.02)
        author_triangles = parse_author_mesh_triangles(text_path, bounds)
        author_mesh = AuthorMesh(author_triangles)
        print(f"author mesh: {author_mesh.triangle_count} physical triangles "
              f"in bbox\n")

    for index, tunnel in enumerate(result.tunnels):
        polygon = _footprint_longitude_latitude(tunnel)
        print(f"=== tunnel {index}: body depth {tunnel.body_depth_m:.2f} m, "
              f"{len(tunnel.mouth_polygons)} mouth(s) ===")
        if polygon is None:
            print("  no deck footprint to sample\n")
            continue

        anchor_longitude, anchor_latitude = tunnel.anchor_longitude_latitude
        datum = sampler.elevation_at_or_none(anchor_latitude, anchor_longitude)
        if datum is None:
            # Fall back to the ring modal elevation as the datum proxy.
            ring_values = _ring_sample(sampler, polygon, args.margin_metres)
            datum = (sum(ring_values) / len(ring_values)
                     if ring_values else None)
            datum_source = "ring mean (anchor off mesh)"
        else:
            datum_source = "built mesh at anchor"
        if datum is None:
            print("  datum unavailable (anchor and ring off mesh)\n")
            continue

        expected_floor = object_implied_trench_floor(
            datum, tunnel.body_depth_m)
        floor_values, interior = _grid_sample(sampler, polygon, args.grid)
        ring_values = _ring_sample(sampler, polygon, args.margin_metres)

        print(f"  datum: {datum:.2f} m ({datum_source})")
        print(f"  expected floor: {expected_floor:.2f} m "
              f"(datum - body_depth {tunnel.body_depth_m:.2f} - "
              f"{TUNNEL_FLOOR_BELOW_OBJECT_DECK_M} [TODO-W-T])")

        floor_pass = rim_pass = spill_pass = None
        if floor_values:
            measured_floor = min(floor_values)
            floor_error = measured_floor - expected_floor
            floor_pass = abs(floor_error) <= args.tolerance
            print(f"  measured floor: {measured_floor:.2f} m over "
                  f"{len(floor_values)}/{interior} interior points "
                  f"-> {floor_error:+.2f} m vs expected "
                  f"({'ok' if floor_pass else 'OFF'})")
        else:
            print("  measured floor: no interior samples on mesh")

        if ring_values:
            rim_mean = sum(ring_values) / len(ring_values)
            rim_error = rim_mean - datum
            rim_pass = abs(rim_error) <= args.tolerance
            spill = sum(1 for value in ring_values
                        if value <= datum - SPILL_BELOW_DATUM_M)
            spill_pass = spill == 0
            print(f"  rim: {rim_mean:.2f} m ({len(ring_values)} ring samples) "
                  f"-> {rim_error:+.2f} m vs datum "
                  f"({'ok' if rim_pass else 'OFF'})")
            print(f"  spill (sub-datum ring samples outside footprint): "
                  f"{spill} ({'ok' if spill_pass else 'LEAK'})")

        if author_mesh is not None:
            author_datum = author_mesh.local_datum(polygon, 20.0)
            author_floor_values, _author_interior = _grid_sample_author(
                author_mesh, polygon, args.grid)
            author_floor = (min(author_floor_values)
                            if author_floor_values else None)
            print(f"  [author mesh] datum {('%.0f m' % author_datum) if author_datum is not None else 'n/a'}"
                  f"  floor {('%.1f m' % author_floor) if author_floor is not None else 'n/a'}"
                  + (f"   datum delta (ours - author) {datum - author_datum:+.2f} m"
                     if author_datum is not None else ""))

        checks = [check for check in (floor_pass, rim_pass, spill_pass)
                  if check is not None]
        verdict = "PASS" if checks and all(checks) else "FAIL"
        print(f"  --> {verdict}\n")

    print("NOTE: feature A is default OFF.  Against an ungraded Ortho4XP mesh "
          "a FAIL is expected — no trench has been cut.  A feature-A build is "
          "the real acceptance target.")
    return 0


def _grid_sample_author(author_mesh, polygon, grid):
    """Grid-sample the author mesh (its sampler takes (lon, lat), unlike the
    built-mesh sampler which takes (lat, lon))."""
    from shapely.geometry import Point

    minimum_longitude, minimum_latitude, maximum_longitude, maximum_latitude = (
        polygon.bounds)
    values = []
    interior = 0
    for row in range(grid):
        for column in range(grid):
            longitude = minimum_longitude + (column + 0.5) / grid * (
                maximum_longitude - minimum_longitude)
            latitude = minimum_latitude + (row + 0.5) / grid * (
                maximum_latitude - minimum_latitude)
            if not polygon.contains(Point(longitude, latitude)):
                continue
            interior += 1
            elevation = author_mesh.sample(longitude, latitude)
            if elevation is not None:
                values.append(elevation)
    return values, interior


if __name__ == "__main__":
    raise SystemExit(main())
