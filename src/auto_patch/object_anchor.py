"""Object pooling, structure partitioning and per-structure offsets.

Contract frozen by workstream W1 (``docs/dsf_object_integration_spec.md``
section 3.3, as amended by A1/A3/A10); implemented in workstream W4.
This module is the heart of the correction and the single most likely
place for a subtle bug — read spec section 2.4 and
``docs/obj8_structure_partition.md`` before writing a line.

The rule that must never be collapsed (spec section 2.4, invariant I-3):

    A structure's ground elevation is a property of the STRUCTURE.
    The y offset applied to a vertex is a property of the
    (structure, object) PAIR, because X-Plane puts each object's
    ``y = 0`` plane at the terrain under THAT object's own anchor::

        delta(S, O) = ground_under(centroid(S)) - ground_under(anchor(O))

When two objects contribute geometry to one structure — the KCLT case,
where walls and roof of one building live in different texture-page
bakes — they receive DIFFERENT deltas, and the walls still meet the roof,
because each delta is measured from its own object's ``y = 0`` plane.
A single per-structure delta is correct only when all contributing
objects share an anchor, and silently tears geometry when they do not.

The pool frame (invariant I-2, partition document section 3 step 1).
All cross-object geometry work happens in ONE local east-north-up frame
per pool, in AUTHORED space:

* Origin: the arithmetic mean of the pool's placement latitudes and
  longitudes.  Axes UNROTATED — the frame is exactly a synthetic
  heading-0 placement at that origin, so
  ``obj8_reader.lonlat_to_local_offset(origin_latitude,
  origin_longitude, 0.0, ...)`` and its inverse ARE the frame maps
  (frame ``x`` = metres east of the origin, frame ``z`` = metres south,
  matching the OBJ8 local convention at heading 0).  A fixed unrotated
  frame is required because workstream W2's audit found axis-aligned
  bounding-box results are not rotation invariant: every pool member
  must be measured against the same axes.
* Horizontal position: each vertex is projected to world
  latitude/longitude through ITS OWN placement
  (``local_offset_to_lonlat``), then into the pool frame.  Two nearby
  world points keep their true separation to well under the weld and
  contact tolerances, whatever their anchors.
* Vertical position: the AUTHORED ``v.y`` — never
  ``terrain(anchor) + v.y``.  The author assembled the parts against a
  common assumed-flat plane; authored space is the frame in which they
  fit.
* Mapping back: a pool-frame centroid ``(x, z)`` returns to
  latitude/longitude through ``local_offset_to_lonlat(origin_latitude,
  origin_longitude, 0.0, x, z)`` — the exact inverse of the frame map.

Everything here is pure: geometry and a sampler in, numbers out, no file
input/output (that is ``object_rebake``'s job).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field as dataclass_field, replace

from . import obj8_partition, obj8_reader
from .mesh_sampler import MeshElevationSampler
from .obj8_reader import ObjectGeometry, ObjectPlacement

Triangle = tuple[int, int, int]

# Amendment A3 do-not-bake tie-break: the single-offset correction is
# "worse than uncorrected" only when its mean ground-part residual
# exceeds the uncorrected mean by more than this.  Without the
# tolerance, a structure sitting exactly at its anchor's elevation
# (corrected residual == uncorrected residual up to float noise) could
# flip to skipped on a nanometre.
RESIDUAL_COMPARISON_TOLERANCE_METRES = 1e-6


@dataclass(frozen=True)
class ObjectPool:
    """Objects whose geometry must be partitioned together because their
    placed world footprints interact — a structure may span several of
    them.

    Pooling is by world axis-aligned-bounding-box overlap (transitively,
    with an epsilon margin), NOT by anchor proximity: contact is a
    world-geometry property, and the 41 KCLT terminal-layer objects share
    buildings across anchors 10 metres apart (invariant I-1, amendment
    A10).  Exactly one placement per resource — multi-placement
    definitions are Phase-2-refused upstream (invariant I-4).
    """

    placements: list[ObjectPlacement]
    resolved_paths: dict[str, str]  # resource_path -> file on disk


@dataclass(frozen=True)
class Structure:
    """One rigid unit: a connected component of the contact graph over the
    pooled parts, possibly spanning several objects."""

    triangles_by_resource: dict[str, list[Triangle]]
    surface_area_square_metres: float
    centroid_latitude: float
    centroid_longitude: float
    minimum_base_y_by_resource: dict[str, float]
    is_ground_touching: bool
    # None until Phase 2 has a mesh to sample (footprints never need it).
    ground_span_metres: float | None
    # Amendment A3: large ground span is bake-and-flag, never refuse/split.
    needs_pad: bool
    skip_reason: str | None
    # Set only for structures with no ground-touching part that inherit a
    # supporter's offset (invariant I-8).
    inherited_from_structure_index: int | None


@dataclass(frozen=True)
class RebakeDecision:
    """Everything ``object_rebake.apply`` needs, and nothing it must
    compute: per-resource, per-vertex y offsets plus the audit trail."""

    structures: list[Structure]
    delta_by_resource_and_vertex: dict[str, dict[int, float]]
    anchor_ground_by_resource: dict[str, float]
    skipped: list[tuple[str, str]]  # (resource_path, reason)
    # Amendment A13: (latitude, longitude, heading_degrees) per resource,
    # so the provenance sidecar can record each object's anchor on fresh
    # bakes (workstream W5's escalation: ``apply`` has no placements).
    anchor_by_resource: dict[str, tuple[float, float, float]] = (
        dataclass_field(default_factory=dict))


@dataclass(frozen=True)
class _PoolFrame:
    """The shared authored-space frame for one pool (module docstring,
    "the pool frame").  ``shared_vertices`` concatenates every included
    object's vertices, each projected through its own placement into the
    unrotated frame with ``y`` = authored ``v.y``;
    ``base_offset_by_resource`` gives each object's slice start, so
    shared index = original index + base offset (the prototype's shared
    index-space idiom)."""

    origin_latitude: float
    origin_longitude: float
    shared_vertices: list[tuple[float, float, float]]
    base_offset_by_resource: dict[str, int]
    resource_of_shared_vertex: list[str]
    included_resources: list[str]
    excluded_resources: list[tuple[str, str]]  # (resource_path, reason)


def _placements_mean_origin(
    placements: list[ObjectPlacement],
) -> tuple[float, float]:
    origin_latitude = sum(
        placement.latitude for placement in placements
    ) / len(placements)
    origin_longitude = sum(
        placement.longitude for placement in placements
    ) / len(placements)
    return origin_latitude, origin_longitude


def _world_point_to_pool_frame(
    origin_latitude: float,
    origin_longitude: float,
    latitude: float,
    longitude: float,
) -> tuple[float, float]:
    """Map a world position into the unrotated pool frame: ``x`` = metres
    east of the origin, ``z`` = metres south (a synthetic heading-0
    placement at the pool origin)."""
    return obj8_reader.lonlat_to_local_offset(
        origin_latitude, origin_longitude, 0.0, latitude, longitude
    )


def _pool_frame_to_world_point(
    origin_latitude: float,
    origin_longitude: float,
    frame_x: float,
    frame_z: float,
) -> tuple[float, float]:
    """Inverse of :func:`_world_point_to_pool_frame`: pool-frame metres
    back to ``(latitude, longitude)``."""
    return obj8_reader.local_offset_to_lonlat(
        origin_latitude, origin_longitude, 0.0, frame_x, frame_z
    )


def _build_pool_frame(
    pool: ObjectPool,
    geometry_by_resource: dict[str, ObjectGeometry],
) -> _PoolFrame:
    """Project every usable object's vertices into the pool frame.

    An object is EXCLUDED (with a reason) when its geometry is missing,
    has no solid triangles, or shares vertices between draped and solid
    triangles — the un-correctable case of invariant I-9.
    ``partition_structures`` simply leaves excluded objects out;
    ``structure_deltas`` records the invariant-I-9 exclusions in
    ``RebakeDecision.skipped``.
    """
    origin_latitude, origin_longitude = _placements_mean_origin(
        pool.placements
    )
    shared_vertices: list[tuple[float, float, float]] = []
    base_offset_by_resource: dict[str, int] = {}
    resource_of_shared_vertex: list[str] = []
    included_resources: list[str] = []
    excluded_resources: list[tuple[str, str]] = []

    for placement in pool.placements:
        resource_path = placement.resource_path
        geometry = geometry_by_resource.get(resource_path)
        if geometry is None:
            excluded_resources.append(
                (resource_path, "no parsed geometry available")
            )
            continue
        if not geometry.solid_triangles:
            excluded_resources.append(
                (resource_path, "no solid triangles")
            )
            continue
        # ``getattr`` keeps this duck-type friendly for test doubles that
        # expose only ``vertices`` / ``solid_triangles``.
        if getattr(geometry, "has_mixed_draped_solid_vertices", False):
            excluded_resources.append(
                (
                    resource_path,
                    "vertices shared between draped and solid triangles "
                    "— un-correctable, refused (invariant I-9)",
                )
            )
            continue
        base_offset_by_resource[resource_path] = len(shared_vertices)
        for local_x, authored_y, local_z in geometry.vertices:
            world_latitude, world_longitude = (
                obj8_reader.local_offset_to_lonlat(
                    placement.latitude,
                    placement.longitude,
                    placement.heading_degrees,
                    local_x,
                    local_z,
                )
            )
            frame_x, frame_z = _world_point_to_pool_frame(
                origin_latitude,
                origin_longitude,
                world_latitude,
                world_longitude,
            )
            shared_vertices.append((frame_x, authored_y, frame_z))
        resource_of_shared_vertex.extend(
            [resource_path] * len(geometry.vertices)
        )
        included_resources.append(resource_path)

    return _PoolFrame(
        origin_latitude=origin_latitude,
        origin_longitude=origin_longitude,
        shared_vertices=shared_vertices,
        base_offset_by_resource=base_offset_by_resource,
        resource_of_shared_vertex=resource_of_shared_vertex,
        included_resources=included_resources,
        excluded_resources=excluded_resources,
    )


def discover_object_pools(
    placements: list[ObjectPlacement],
    resolved_paths: dict[str, str],
    geometry_by_resource: dict[str, ObjectGeometry],
    *,
    epsilon_metres: float,
) -> list[ObjectPool]:
    """Group correction-candidate placements whose placed world
    axis-aligned bounding boxes overlap (transitively, expanded by
    ``epsilon_metres``).

    Candidates only: small, correctly anchored objects — a light mast
    beside a terminal wall — must never be pooled; X-Plane already places
    them right, and correcting the terminal moves it towards the mast,
    not away (partition document, section 3 step 0).  Callers pass only
    correction candidates; reach is NOT re-filtered here.

    The world box of each placement is the horizontal bounding box of
    its SOLID triangles, projected through its own placement.  Because
    the heading rotates the box, all FOUR corners are projected — two
    opposite corners under-cover any rotated box.  Overlap is tested on
    the horizontal (east/south) plane only: an elevated clutter object
    hovering over a ground object must pool with it so the inheritance
    rule (invariant I-8) can see its supporter.

    Pooling coarseness is harmless: parts that never come within the
    contact epsilon stay separate structures regardless of how large
    their pool is.
    """
    if not placements:
        return []
    origin_latitude, origin_longitude = _placements_mean_origin(placements)

    expanded_boxes: list[tuple[float, float, float, float]] = []
    for placement in placements:
        geometry = geometry_by_resource.get(placement.resource_path)
        if geometry is not None and geometry.solid_triangles:
            minimum_x, maximum_x, minimum_z, maximum_z = (
                obj8_reader.horizontal_bounding_box(
                    geometry.vertices, geometry.solid_triangles
                )
            )
            corner_offsets = [
                (minimum_x, minimum_z),
                (minimum_x, maximum_z),
                (maximum_x, minimum_z),
                (maximum_x, maximum_z),
            ]
        else:
            # Degenerate: no solid footprint — a point box at the anchor.
            corner_offsets = [(0.0, 0.0)]
        frame_corner_points = []
        for local_x, local_z in corner_offsets:
            world_latitude, world_longitude = (
                obj8_reader.local_offset_to_lonlat(
                    placement.latitude,
                    placement.longitude,
                    placement.heading_degrees,
                    local_x,
                    local_z,
                )
            )
            frame_corner_points.append(
                _world_point_to_pool_frame(
                    origin_latitude,
                    origin_longitude,
                    world_latitude,
                    world_longitude,
                )
            )
        corner_x_values = [point[0] for point in frame_corner_points]
        corner_z_values = [point[1] for point in frame_corner_points]
        expanded_boxes.append(
            (
                min(corner_x_values) - epsilon_metres,
                max(corner_x_values) + epsilon_metres,
                min(corner_z_values) - epsilon_metres,
                max(corner_z_values) + epsilon_metres,
            )
        )

    parent = list(range(len(placements)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    for first_index in range(len(placements)):
        first_box = expanded_boxes[first_index]
        for second_index in range(first_index + 1, len(placements)):
            second_box = expanded_boxes[second_index]
            boxes_overlap = (
                first_box[0] <= second_box[1]
                and second_box[0] <= first_box[1]
                and first_box[2] <= second_box[3]
                and second_box[2] <= first_box[3]
            )
            if boxes_overlap:
                union(first_index, second_index)

    members_by_root: dict[int, list[int]] = defaultdict(list)
    for placement_index in range(len(placements)):
        members_by_root[find(placement_index)].append(placement_index)

    pools: list[ObjectPool] = []
    # Deterministic pool order: by each group's first placement in the
    # caller's input order.
    for members in sorted(members_by_root.values(), key=lambda group: group[0]):
        pool_placements = [placements[index] for index in members]
        pool_resolved_paths = {
            placement.resource_path: resolved_paths[placement.resource_path]
            for placement in pool_placements
            if placement.resource_path in resolved_paths
        }
        pools.append(
            ObjectPool(
                placements=pool_placements,
                resolved_paths=pool_resolved_paths,
            )
        )
    return pools


def partition_structures(
    pool: ObjectPool,
    geometry_by_resource: dict[str, ObjectGeometry],
    *,
    epsilon_metres: float,
) -> list[Structure]:
    """Partition a pool's solid geometry into structures.

    Thin composition over ``obj8_partition`` (amendment A1 — Phases 1 and
    2 MUST share this partition): project every object's vertices into
    the pool frame using its own placement (module docstring, "the pool
    frame"), offset each object's vertex indices into one shared index
    space, ``weld_parts`` → ``contact_graph`` → ``connected_structures``,
    then map back per-object.  Draped triangles are discarded before
    partitioning (invariant I-9); an object with vertices shared between
    draped and solid triangles is excluded entirely (``structure_deltas``
    records it in ``RebakeDecision.skipped``).

    Per-resource triangles carry the ORIGINAL per-object vertex indices
    — downstream, ``object_footprints.structure_ring`` and the rebake
    writer index into each object's own ``geometry.vertices``.

    ``ATTR_LOD`` copies are spatially coincident, so the contact graph
    merges them into one structure by itself; being coincident copies,
    they do not displace the area-weighted centroid either (invariant
    I-12).  Positional commands and ``ANIM`` handling are workstream
    W5's concern.

    Phase 2 fields (``ground_span_metres``, ``needs_pad``,
    ``skip_reason``, ``inherited_from_structure_index``) are left at
    their pre-mesh defaults here; ``structure_deltas`` fills them via
    ``dataclasses.replace``.
    """
    from .config import DSF_OBJECT_ELEVATED_BASE_M

    frame = _build_pool_frame(pool, geometry_by_resource)

    shared_triangles: list[Triangle] = []
    for resource_path in frame.included_resources:
        base_offset = frame.base_offset_by_resource[resource_path]
        geometry = geometry_by_resource[resource_path]
        shared_triangles.extend(
            (
                first_index + base_offset,
                second_index + base_offset,
                third_index + base_offset,
            )
            for first_index, second_index, third_index in (
                geometry.solid_triangles
            )
        )
    if not shared_triangles:
        return []

    parts = obj8_partition.weld_parts(frame.shared_vertices, shared_triangles)
    contact_edges = obj8_partition.contact_graph(
        frame.shared_vertices, parts, epsilon_metres
    )
    part_index_groups = obj8_partition.connected_structures(
        len(parts), contact_edges
    )

    structures: list[Structure] = []
    for part_indices in part_index_groups:
        structure_shared_triangles = [
            triangle
            for part_index in part_indices
            for triangle in parts[part_index]
        ]
        surface_area_square_metres, centroid_x, centroid_z = (
            obj8_reader.area_weighted_centroid(
                frame.shared_vertices, structure_shared_triangles
            )
        )
        centroid_latitude, centroid_longitude = _pool_frame_to_world_point(
            frame.origin_latitude,
            frame.origin_longitude,
            centroid_x,
            centroid_z,
        )

        triangles_by_resource: dict[str, list[Triangle]] = defaultdict(list)
        minimum_base_y_by_resource: dict[str, float] = {}
        for shared_triangle in structure_shared_triangles:
            # All three indices of one triangle come from one object —
            # index offsets are applied per object.
            resource_path = frame.resource_of_shared_vertex[
                shared_triangle[0]
            ]
            base_offset = frame.base_offset_by_resource[resource_path]
            triangles_by_resource[resource_path].append(
                (
                    shared_triangle[0] - base_offset,
                    shared_triangle[1] - base_offset,
                    shared_triangle[2] - base_offset,
                )
            )
            for shared_index in shared_triangle:
                authored_y = frame.shared_vertices[shared_index][1]
                known_minimum = minimum_base_y_by_resource.get(resource_path)
                if known_minimum is None or authored_y < known_minimum:
                    minimum_base_y_by_resource[resource_path] = authored_y

        is_ground_touching = (
            min(minimum_base_y_by_resource.values())
            <= DSF_OBJECT_ELEVATED_BASE_M
        )
        structures.append(
            Structure(
                triangles_by_resource=dict(triangles_by_resource),
                surface_area_square_metres=surface_area_square_metres,
                centroid_latitude=centroid_latitude,
                centroid_longitude=centroid_longitude,
                minimum_base_y_by_resource=minimum_base_y_by_resource,
                is_ground_touching=is_ground_touching,
                ground_span_metres=None,
                needs_pad=False,
                skip_reason=None,
                inherited_from_structure_index=None,
            )
        )
    return structures


def structure_deltas(
    pool: ObjectPool,
    geometry_by_resource: dict[str, ObjectGeometry],
    structures: list[Structure],
    sampler: MeshElevationSampler,
) -> RebakeDecision:
    """Compute per-(structure, object) y offsets against the built mesh.

    Per structure: sample ``ground_under(centroid)``; on an
    outside-the-mesh sample skip-and-report, never guess (invariant
    I-13).  Each placement's anchor is sampled once; an anchor outside
    the mesh skips every structure touching that object.  A structure
    with no ground-touching part inherits its supporter's ground
    (invariant I-8): the ground-touching structure whose horizontal
    bounding box (pool frame) contains its centroid, else the nearest by
    centroid distance; ``inherited_from_structure_index`` records it
    (an index into the returned ``structures`` list, which preserves the
    caller's order).

    ``ground_span_metres`` is the max−min of ``ground_under`` over the
    structure's ground-touching parts' centroids; a part centroid
    outside the mesh borrows the structure centroid's ground (noted, not
    fatal).  ``needs_pad`` flags a span past
    ``DSF_OBJECT_PAD_FLAG_SPAN_M`` — the structure is STILL baked with
    the best single offset (amendment A3).  The only do-not-bake case is
    arithmetic: over the ground-touching parts, if the mean corrected
    residual ``|ground(anchor) + base_y + delta − ground(part)|``
    exceeds the mean uncorrected residual
    ``|ground(anchor) + base_y − ground(part)|``, correction would
    worsen the seating and the structure is skipped with both numbers in
    ``skip_reason``.

    Positional commands and ``ANIM`` handling are workstream W5's
    concern, not this function's.
    """
    from .config import (
        DSF_OBJECT_ELEVATED_BASE_M,
        DSF_OBJECT_PAD_FLAG_SPAN_M,
    )

    frame = _build_pool_frame(pool, geometry_by_resource)

    skipped: list[tuple[str, str]] = []
    unusable_reason_by_resource: dict[str, str] = {}
    for resource_path, reason in frame.excluded_resources:
        unusable_reason_by_resource[resource_path] = reason
        if "invariant I-9" in reason:
            # The un-correctable mixed draped/solid case is a real
            # refusal and part of the audit trail; geometry that is
            # merely absent or empty never needed correcting.
            skipped.append((resource_path, reason))

    # Anchor grounds: one sample per placement.  An anchor outside the
    # mesh poisons every structure touching that object (invariant I-13).
    placement_by_resource = {
        placement.resource_path: placement for placement in pool.placements
    }
    anchor_ground_by_resource: dict[str, float] = {}
    for resource_path in frame.included_resources:
        placement = placement_by_resource[resource_path]
        anchor_ground = sampler.elevation_at_or_none(
            placement.latitude, placement.longitude
        )
        if anchor_ground is None:
            reason = (
                f"anchor ({placement.latitude:.6f}, "
                f"{placement.longitude:.6f}) lies outside the built mesh "
                "— skipped, never nearest-vertex sampled (invariant I-13)"
            )
            unusable_reason_by_resource[resource_path] = reason
            skipped.append((resource_path, reason))
        else:
            anchor_ground_by_resource[resource_path] = anchor_ground

    # Per-structure pool-frame geometry: shared-index triangles, the
    # horizontal bounding box, and the frame-coordinate centroid.
    shared_triangles_by_structure: list[list[Triangle]] = []
    bounding_box_by_structure: list[
        tuple[float, float, float, float] | None
    ] = []
    frame_centroid_by_structure: list[tuple[float, float]] = []
    for structure in structures:
        structure_shared_triangles: list[Triangle] = []
        for resource_path, triangles in (
            structure.triangles_by_resource.items()
        ):
            base_offset = frame.base_offset_by_resource.get(resource_path)
            if base_offset is None:
                continue  # excluded object; the skip pass below handles it
            structure_shared_triangles.extend(
                (
                    first_index + base_offset,
                    second_index + base_offset,
                    third_index + base_offset,
                )
                for first_index, second_index, third_index in triangles
            )
        shared_triangles_by_structure.append(structure_shared_triangles)
        if structure_shared_triangles:
            bounding_box_by_structure.append(
                obj8_reader.horizontal_bounding_box(
                    frame.shared_vertices, structure_shared_triangles
                )
            )
        else:
            bounding_box_by_structure.append(None)
        frame_centroid_by_structure.append(
            _world_point_to_pool_frame(
                frame.origin_latitude,
                frame.origin_longitude,
                structure.centroid_latitude,
                structure.centroid_longitude,
            )
        )

    # Pass 1 — structure grounds and the unconditional skips.
    skip_reason_by_index: dict[int, str] = {}
    ground_by_index: dict[int, float] = {}
    for structure_index, structure in enumerate(structures):
        blocking_resource = None
        for resource_path in structure.triangles_by_resource:
            if resource_path in unusable_reason_by_resource:
                blocking_resource = resource_path
                break
            if resource_path not in frame.base_offset_by_resource:
                blocking_resource = resource_path
                unusable_reason_by_resource[resource_path] = (
                    "resource is not part of this pool's frame"
                )
                break
        if blocking_resource is not None:
            skip_reason_by_index[structure_index] = (
                f"object {blocking_resource} is unusable: "
                f"{unusable_reason_by_resource[blocking_resource]}"
            )
            continue
        centroid_ground = sampler.elevation_at_or_none(
            structure.centroid_latitude, structure.centroid_longitude
        )
        if centroid_ground is None:
            skip_reason_by_index[structure_index] = (
                f"structure centroid ({structure.centroid_latitude:.6f}, "
                f"{structure.centroid_longitude:.6f}) lies outside the "
                "built mesh — skipped, never nearest-vertex sampled "
                "(invariant I-13)"
            )
            continue
        ground_by_index[structure_index] = centroid_ground

    # Pass 2 — inheritance for structures with no ground-touching part
    # (invariant I-8).  Supporters are ground-touching structures with a
    # valid ground sample; containment wins over distance, first
    # containing supporter in list order for determinism.
    supporter_indices = [
        structure_index
        for structure_index, structure in enumerate(structures)
        if structure.is_ground_touching
        and structure_index in ground_by_index
    ]
    inherited_from_by_index: dict[int, int] = {}
    for structure_index, structure in enumerate(structures):
        if (
            structure_index in skip_reason_by_index
            or structure.is_ground_touching
        ):
            continue
        centroid_x, centroid_z = frame_centroid_by_structure[structure_index]
        supporter_index = None
        for candidate_index in supporter_indices:
            candidate_box = bounding_box_by_structure[candidate_index]
            if candidate_box is None:
                continue
            minimum_x, maximum_x, minimum_z, maximum_z = candidate_box
            if (
                minimum_x <= centroid_x <= maximum_x
                and minimum_z <= centroid_z <= maximum_z
            ):
                supporter_index = candidate_index
                break
        if supporter_index is None and supporter_indices:
            supporter_index = min(
                supporter_indices,
                key=lambda candidate_index: math.hypot(
                    frame_centroid_by_structure[candidate_index][0]
                    - centroid_x,
                    frame_centroid_by_structure[candidate_index][1]
                    - centroid_z,
                ),
            )
        if supporter_index is None:
            skip_reason_by_index[structure_index] = (
                "no ground-touching part, and no ground-touching "
                "supporter structure with a valid mesh sample to inherit "
                "from (invariant I-8)"
            )
            continue
        inherited_from_by_index[structure_index] = supporter_index
        ground_by_index[structure_index] = ground_by_index[supporter_index]

    # Pass 3 — ground span, the amendment-A3 residual arithmetic, and the
    # per-(structure, object) deltas (spec section 2.4, invariant I-3).
    updated_structures: list[Structure] = []
    delta_by_resource_and_vertex: dict[str, dict[int, float]] = {}
    for structure_index, structure in enumerate(structures):
        if structure_index in skip_reason_by_index:
            updated_structures.append(
                replace(
                    structure,
                    skip_reason=skip_reason_by_index[structure_index],
                )
            )
            continue
        structure_ground = ground_by_index[structure_index]

        # Ground-touching parts, re-derived from the SAME welding the
        # partition used (welding is intra-part, so welding a structure's
        # own triangles reproduces exactly its parts).
        structure_shared_triangles = shared_triangles_by_structure[
            structure_index
        ]
        parts = (
            obj8_partition.weld_parts(
                frame.shared_vertices, structure_shared_triangles
            )
            if structure_shared_triangles
            else []
        )
        ground_part_records: list[tuple[float, float, str]] = []
        for part_triangles in parts:
            used_shared_indices = {
                shared_index
                for triangle in part_triangles
                for shared_index in triangle
            }
            base_shared_index = min(
                used_shared_indices,
                key=lambda shared_index: frame.shared_vertices[shared_index][
                    1
                ],
            )
            part_base_y = frame.shared_vertices[base_shared_index][1]
            if part_base_y > DSF_OBJECT_ELEVATED_BASE_M:
                continue
            _part_area, part_x, part_z = obj8_reader.area_weighted_centroid(
                frame.shared_vertices, part_triangles
            )
            part_latitude, part_longitude = _pool_frame_to_world_point(
                frame.origin_latitude, frame.origin_longitude, part_x, part_z
            )
            part_ground = sampler.elevation_at_or_none(
                part_latitude, part_longitude
            )
            if part_ground is None:
                # Noted, not fatal: the structure centroid IS on the
                # mesh; a single part centroid off it borrows that
                # ground rather than poisoning the whole structure.
                import O4_UI_Utils as UI

                UI.vprint(
                    2,
                    "  [object-anchor] ground part centroid "
                    f"({part_latitude:.6f}, {part_longitude:.6f}) lies "
                    "outside the built mesh; using the structure "
                    "centroid's ground for it",
                )
                part_ground = structure_ground
            base_resource = frame.resource_of_shared_vertex[
                base_shared_index
            ]
            ground_part_records.append(
                (part_ground, part_base_y, base_resource)
            )

        if ground_part_records:
            part_grounds = [record[0] for record in ground_part_records]
            ground_span_metres = max(part_grounds) - min(part_grounds)
        else:
            ground_span_metres = 0.0
        needs_pad = ground_span_metres > DSF_OBJECT_PAD_FLAG_SPAN_M

        # Amendment A3: always bake the best single offset; do-not-bake
        # ONLY when the arithmetic says correction worsens the seating.
        a3_skip_reason = None
        if ground_part_records:
            corrected_residuals = [
                abs(structure_ground + part_base_y - part_ground)
                for part_ground, part_base_y, _base_resource in (
                    ground_part_records
                )
            ]
            uncorrected_residuals = [
                abs(
                    anchor_ground_by_resource[base_resource]
                    + part_base_y
                    - part_ground
                )
                for part_ground, part_base_y, base_resource in (
                    ground_part_records
                )
            ]
            corrected_mean = sum(corrected_residuals) / len(
                corrected_residuals
            )
            uncorrected_mean = sum(uncorrected_residuals) / len(
                uncorrected_residuals
            )
            if corrected_mean > (
                uncorrected_mean + RESIDUAL_COMPARISON_TOLERANCE_METRES
            ):
                a3_skip_reason = (
                    "single-offset correction would worsen the seating: "
                    f"mean ground-part residual {corrected_mean:.3f} m "
                    f"corrected vs {uncorrected_mean:.3f} m uncorrected "
                    f"over {len(ground_part_records)} ground-touching "
                    "part(s) — left unbaked (amendment A3)"
                )
        if a3_skip_reason is not None:
            updated_structures.append(
                replace(
                    structure,
                    ground_span_metres=ground_span_metres,
                    needs_pad=needs_pad,
                    skip_reason=a3_skip_reason,
                )
            )
            continue

        # The deltas.  Invariant I-3: per (structure, object) — each
        # resource's offset is measured from ITS OWN anchor's ground.
        for resource_path, triangles in (
            structure.triangles_by_resource.items()
        ):
            delta = (
                structure_ground - anchor_ground_by_resource[resource_path]
            )
            resource_deltas = delta_by_resource_and_vertex.setdefault(
                resource_path, {}
            )
            for triangle in triangles:
                for vertex_index in triangle:
                    resource_deltas[vertex_index] = delta

        updated_structures.append(
            replace(
                structure,
                ground_span_metres=ground_span_metres,
                needs_pad=needs_pad,
                inherited_from_structure_index=inherited_from_by_index.get(
                    structure_index
                ),
            )
        )

    return RebakeDecision(
        structures=updated_structures,
        delta_by_resource_and_vertex=delta_by_resource_and_vertex,
        anchor_ground_by_resource=anchor_ground_by_resource,
        skipped=skipped,
        anchor_by_resource={
            resource_path: (
                placement.latitude,
                placement.longitude,
                placement.heading_degrees,
            )
            for resource_path, placement in placement_by_resource.items()
            if resource_path in anchor_ground_by_resource
        },
    )
