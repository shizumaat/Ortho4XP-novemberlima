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

# Amendment A19: the A3 do-not-bake guard applies only to structures
# smaller than this diameter.  A mega-structure (HECA's kilometre-wide
# chained terminal web) always bakes with its best single offset and
# flags ``needs_pad`` — judging it by mean residual and silently
# skipping left 49 resources floating at anchor minus local ground.
A3_GUARD_MAXIMUM_DIAMETER_METRES = 100.0

# Stable phrase carried in the ``skip_reason`` of a structure left at its
# authored elevations because its ground-contact terrain span exceeds
# ``DSF_OBJECT_BAKE_MAX_GROUND_SPAN_M``.  ``post_mesh`` matches on it to
# count the per-airport "left at authored elevations" summary, so the
# reason text and this phrase must stay in lockstep.
GROUND_SPAN_SKIP_REASON_PHRASE = "exceeds the rigid-seat limit"


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
class FootCluster:
    """One ground-contact FOOT of a structure: a cluster of solid
    vertices at the structure's own lowest band (project memory
    kbna-gantry-pond-multi-foot-objects).

    Detected in the pool frame by :func:`detect_foot_clusters`;
    ``structure_deltas`` fills the world/mesh fields
    (``latitude``/``longitude``/``ground_metres``/``kept_for_fit``/
    ``residual_metres``) via ``dataclasses.replace`` when the structure
    is foot-anchored."""

    centroid_x: float  # pool frame metres east
    centroid_z: float  # pool frame metres south
    base_y: float  # authored y of the cluster's lowest solid vertex
    base_resource: str  # resource owning that lowest vertex
    contact_points: tuple[tuple[float, float], ...]  # frame (x, z)
    latitude: float | None = None
    longitude: float | None = None
    ground_metres: float | None = None
    # False when the foot's seat target fell more than
    # DSF_OBJECT_FOOT_CONTACT_TOLERANCE_M below the topmost target and
    # was excluded from the rigid fit.
    kept_for_fit: bool = True
    # ``rendered base − ground`` after the fitted rigid offset
    # (positive floats, negative sinks); None until fitted.
    residual_metres: float | None = None


@dataclass(frozen=True)
class FootPadRequest:
    """A per-foot terrain-pad request: after the best rigid offset this
    foot still misses the mesh by more than
    ``DSF_OBJECT_FOOT_PAD_RESIDUAL_M`` — a rigid body cannot seat it,
    only terrain shaped to ``target_ground_metres`` under the foot can.
    Recorded on the decision and written to the post-mesh sidecar; the
    ring itself is built downstream by
    ``object_footprints.foot_pad_ring`` from ``contact_points_lonlat``.
    """

    structure_index: int
    resource_path: str
    latitude: float
    longitude: float
    base_y: float
    residual_metres: float
    target_ground_metres: float
    contact_points_lonlat: tuple[tuple[float, float], ...]  # (lon, lat)


@dataclass(frozen=True)
class RebakeDecision:
    """Everything ``object_rebake.apply`` needs, and nothing it must
    compute: per-resource, per-vertex y offsets plus the audit trail."""

    structures: list[Structure]
    delta_by_resource_and_vertex: dict[str, dict[int, float]]
    anchor_ground_by_resource: dict[str, float]
    # (resource_path, reason) for resources that must not be baked AT
    # ALL: ``object_rebake.apply`` refuses every resource listed here.
    # A resource where only SOME structures were skipped is not listed —
    # its passing structures' deltas are in
    # ``delta_by_resource_and_vertex`` and bake normally (amendment
    # A21); the skipped structures keep their ``skip_reason`` in
    # ``structures``, which is where per-structure detail comes from.
    skipped: list[tuple[str, str]]
    # Amendment A13: (latitude, longitude, heading_degrees) per resource,
    # so the provenance sidecar can record each object's anchor on fresh
    # bakes (workstream W5's escalation: ``apply`` has no placements).
    anchor_by_resource: dict[str, tuple[float, float, float]] = (
        dataclass_field(default_factory=dict))
    # Foot re-anchor audit trail: every foot-anchored structure's
    # detected feet (world/mesh fields filled), keyed by index into
    # ``structures``.  Present even when the structure was later
    # A3-skipped, so the seating audit is never blind to a
    # baked-offset object again.
    foot_clusters_by_structure_index: dict[int, tuple[FootCluster, ...]] = (
        dataclass_field(default_factory=dict))
    # Feet the rigid offset could not seat (see FootPadRequest).
    foot_pad_requests: list[FootPadRequest] = (
        dataclass_field(default_factory=list))


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


def detect_foot_clusters(
    points: list[tuple[float, float, float]],
    resources: list[str],
    *,
    band_metres: float,
    cluster_gap_metres: float,
    maximum_base_spread_metres: float,
) -> list[FootCluster]:
    """Detect a structure's ground-contact FEET relative to its own
    lowest band (never the absolute ``y <= DSF_OBJECT_ELEVATED_BASE_M``
    test, which an author-baked vertical offset defeats).

    ``points`` are the structure's solid vertices ``(x, y, z)`` in the
    pool frame (``y`` = authored vertical); ``resources`` is parallel.
    Three stages, each doing one job (constants documented in
    ``config.py``):

    1. CONTACT BAND — a vertex qualifies when it lies within
       ``band_metres`` of the lowest vertex in its own horizontal
       neighbourhood (radius ``cluster_gap_metres``).  A local band,
       not a global one: the 45 m KBNA stair's feet sit 1.17 m apart
       in authored y, and near each foot the band must exclude the
       stair stringers right above it.
    2. CLUSTERING — band vertices chain into one foot when within
       ``cluster_gap_metres`` horizontally AND ``band_metres``
       vertically per link.  The vertical constraint keeps a foot from
       chaining up a staircase onto the deck underside.
    3. FOOT GATE — a cluster is a foot only when its base lies within
       ``maximum_base_spread_metres`` of the structure's overall lowest
       vertex.  Mid-span deck-underside clusters (their own local
       minima, stage 1 cannot see the feet from there) start ~1.9 m up
       on the measured KBNA stairs and are dropped here.

    Returns feet ordered by ``(centroid_x, centroid_z)`` for
    determinism.  A single-foot result is normal (most objects); the
    caller decides what to do with it.
    """
    if not points:
        return []
    minimum_y_overall = min(point[1] for point in points)

    # Stage 1 — grid-bucketed local-minimum band.
    cell_size = cluster_gap_metres if cluster_gap_metres > 0.0 else 1.0
    indices_by_cell: dict[tuple[int, int], list[int]] = defaultdict(list)
    for point_index, (x, _y, z) in enumerate(points):
        indices_by_cell[
            (int(math.floor(x / cell_size)), int(math.floor(z / cell_size)))
        ].append(point_index)

    def _neighbour_indices(x: float, z: float):
        cell_x = int(math.floor(x / cell_size))
        cell_z = int(math.floor(z / cell_size))
        for offset_x in (-1, 0, 1):
            for offset_z in (-1, 0, 1):
                yield from indices_by_cell.get(
                    (cell_x + offset_x, cell_z + offset_z), ()
                )

    candidate_indices: list[int] = []
    for point_index, (x, y, z) in enumerate(points):
        local_minimum_y = y
        for other_index in _neighbour_indices(x, z):
            other_x, other_y, other_z = points[other_index]
            if other_y < local_minimum_y and (
                math.hypot(other_x - x, other_z - z) <= cluster_gap_metres
            ):
                local_minimum_y = other_y
        if y <= local_minimum_y + band_metres:
            candidate_indices.append(point_index)
    if not candidate_indices:
        return []

    # Stage 2 — single-linkage union-find over the candidates.
    position_in_candidates = {
        point_index: candidate_position
        for candidate_position, point_index in enumerate(candidate_indices)
    }
    parent = list(range(len(candidate_indices)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    for candidate_position, point_index in enumerate(candidate_indices):
        x, y, z = points[point_index]
        for other_index in _neighbour_indices(x, z):
            other_position = position_in_candidates.get(other_index)
            if other_position is None or other_position <= candidate_position:
                continue
            other_x, other_y, other_z = points[other_index]
            if (
                abs(other_y - y) <= band_metres
                and math.hypot(other_x - x, other_z - z)
                <= cluster_gap_metres
            ):
                union(candidate_position, other_position)

    members_by_root: dict[int, list[int]] = defaultdict(list)
    for candidate_position, point_index in enumerate(candidate_indices):
        members_by_root[find(candidate_position)].append(point_index)

    # Stage 3 — the foot gate, then one FootCluster per surviving group.
    feet: list[FootCluster] = []
    for member_indices in members_by_root.values():
        base_index = min(
            member_indices, key=lambda point_index: points[point_index][1]
        )
        base_y = points[base_index][1]
        if base_y > minimum_y_overall + maximum_base_spread_metres:
            continue
        contact_indices = [
            point_index
            for point_index in member_indices
            if points[point_index][1] <= base_y + band_metres
        ]
        centroid_x = sum(
            points[point_index][0] for point_index in contact_indices
        ) / len(contact_indices)
        centroid_z = sum(
            points[point_index][2] for point_index in contact_indices
        ) / len(contact_indices)
        feet.append(
            FootCluster(
                centroid_x=centroid_x,
                centroid_z=centroid_z,
                base_y=base_y,
                base_resource=resources[base_index],
                contact_points=tuple(
                    (points[point_index][0], points[point_index][2])
                    for point_index in contact_indices
                ),
            )
        )
    feet.sort(key=lambda foot: (foot.centroid_x, foot.centroid_z))
    return feet


@dataclass(frozen=True)
class ConnectorMetrics:
    """Footprint metrics that recognise a CONNECTOR object.

    Defect 2026-07-17 (UK payware co-baked airports): scenery packs bake
    a whole airport as many ``.obj`` files sharing one anchor, and among
    them are CONNECTOR meshes — perimeter fences, road/rail networks,
    whole-complex ground slabs — whose base geometry physically touches
    (within the contact epsilon) every real building.  Left in the pool
    they chain all the buildings into one connected structure whose
    convex hull fills the field, burying the real buildings and the
    below-grade tunnels under one airport-sized pad (EGGW building1 was
    2,814,841 m²; EGLL's T5 web 537,939 m²).

    A connector is long AND sparse: a fence or branching road covers only
    a thin sliver of the convex hull it stretches across; a solid terminal
    slab, however large, fills most of its hull.  Both must hold to flag,
    so a genuine large filled terminal is never mistaken for a connector.

    * ``span_metres`` — the larger side of the solid footprint's
      axis-aligned bounding box (the object's own authored horizontal
      frame; span is invariant to translation and, for the elongated
      connectors this targets, dominated by the long axis regardless of
      heading — measuring in the tight authored frame is the conservative
      choice against false positives on a rotated compact building).
    * ``hull_fill_ratio`` — horizontal solid-triangle area ÷ convex-hull
      area of the footprint (0 when the hull is degenerate).
    """

    span_metres: float
    hull_fill_ratio: float
    footprint_area_square_metres: float
    hull_area_square_metres: float


def _convex_hull_area_square_metres(
    points: list[tuple[float, float]],
) -> float:
    """Area of the convex hull of 2-D ``points`` (Andrew's monotone chain
    followed by the shoelace formula).  Zero when the points do not span
    a two-dimensional area (fewer than three, or all collinear)."""
    unique_points = sorted(set(points))
    if len(unique_points) < 3:
        return 0.0

    def cross(
        origin: tuple[float, float],
        first: tuple[float, float],
        second: tuple[float, float],
    ) -> float:
        return (first[0] - origin[0]) * (second[1] - origin[1]) - (
            first[1] - origin[1]
        ) * (second[0] - origin[0])

    lower: list[tuple[float, float]] = []
    for point in unique_points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0.0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(unique_points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0.0:
            upper.pop()
        upper.append(point)
    hull = lower[:-1] + upper[:-1]
    if len(hull) < 3:
        return 0.0
    twice_area = 0.0
    for index in range(len(hull)):
        x1, y1 = hull[index]
        x2, y2 = hull[(index + 1) % len(hull)]
        twice_area += x1 * y2 - x2 * y1
    return abs(twice_area) / 2.0


def resource_connector_metrics(
    geometry: ObjectGeometry,
) -> ConnectorMetrics:
    """Measure one object's solid footprint span and hull-fill ratio.

    Pure: geometry in, numbers out (see :class:`ConnectorMetrics` for the
    defect and the metric definitions).  Works on the object's own
    authored ``(x, z)`` horizontal coordinates — both the footprint area
    and the hull area are rotation-invariant, and the bounding-box span is
    measured in the tight authored frame."""
    solid_triangles = geometry.solid_triangles
    if not solid_triangles:
        return ConnectorMetrics(0.0, 0.0, 0.0, 0.0)
    used_indices = {
        index for triangle in solid_triangles for index in triangle
    }
    x_values = [geometry.vertices[index][0] for index in used_indices]
    z_values = [geometry.vertices[index][2] for index in used_indices]
    span_metres = max(
        max(x_values) - min(x_values), max(z_values) - min(z_values)
    )
    footprint_area = 0.0
    for first, second, third in solid_triangles:
        ax, az = geometry.vertices[first][0], geometry.vertices[first][2]
        bx, bz = geometry.vertices[second][0], geometry.vertices[second][2]
        cx, cz = geometry.vertices[third][0], geometry.vertices[third][2]
        footprint_area += abs(
            (bx - ax) * (cz - az) - (cx - ax) * (bz - az)
        ) / 2.0
    hull_area = _convex_hull_area_square_metres(
        [
            (geometry.vertices[index][0], geometry.vertices[index][2])
            for index in used_indices
        ]
    )
    hull_fill_ratio = (
        footprint_area / hull_area if hull_area > 0.0 else 0.0
    )
    return ConnectorMetrics(
        span_metres=span_metres,
        hull_fill_ratio=hull_fill_ratio,
        footprint_area_square_metres=footprint_area,
        hull_area_square_metres=hull_area,
    )


def is_connector_resource(
    geometry: ObjectGeometry,
    *,
    connector_span_metres: float,
    connector_maximum_fill: float,
) -> tuple[bool, ConnectorMetrics]:
    """Return ``(is_connector, metrics)`` for one object.

    A resource is a CONNECTOR — excluded from building pooling and
    partitioning before weld/contact so it cannot chain real buildings
    into one field-spanning structure — only when BOTH conditions hold:
    its footprint span exceeds ``connector_span_metres`` AND its hull-fill
    ratio is below ``connector_maximum_fill``.  A large but FILLED
    footprint (a real mega-terminal) fails the fill test and is kept."""
    metrics = resource_connector_metrics(geometry)
    is_connector = (
        metrics.span_metres > connector_span_metres
        and metrics.hull_fill_ratio < connector_maximum_fill
    )
    return is_connector, metrics


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
    # Connector split (2026-07-18, EGGW floating buildings): an
    # airport-scale chained component can never be seated by one rigid
    # offset — re-partition it at its linear connectors so each real
    # building bakes on its own (see the CONNECTOR_SPLIT constants in
    # obj8_partition for the design and the accepted fence-joint cost).
    part_index_groups, connector_splits = (
        obj8_partition.split_oversized_components(
            frame.shared_vertices, parts, part_index_groups, epsilon_metres
        )
    )
    if connector_splits:
        import O4_UI_Utils as UI

        UI.vprint(
            1,
            f"   [object-anchor] connector split: {connector_splits} "
            "oversized chained component(s) re-partitioned at their "
            "linear connectors (fences/barriers) so member buildings "
            "seat individually",
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
    ``skip_reason``.  A skip is per STRUCTURE, never per resource
    (amendment A21): a resource whose other structures pass still bakes
    those structures' deltas, and only lands in ``skipped`` when every
    structure carrying it was skipped.

    Foot re-anchor (``DSF_OBJECT_FOOT_ANCHOR``, project memory
    kbna-gantry-pond-multi-foot-objects): a structure the absolute
    elevated test classifies as clutter, but whose lowest band IS its
    own object's lowest band, carries an author-BAKED vertical offset.
    Unless every detected foot sits over a ground-touching supporter
    (genuine baked rooftop clutter — inheritance stands), the structure
    is FOOT-ANCHORED: its ground records are its feet
    (:func:`detect_foot_clusters`), and the seating elevation is the
    midpoint of the kept per-foot seat targets ``ground(foot) −
    base_y(foot)`` — the rigid offset minimising the worst foot
    residual across feet whose authored bases differ.  Detected feet
    land in ``RebakeDecision.foot_clusters_by_structure_index`` (the
    audit trail); feet the rigid offset cannot seat raise
    ``RebakeDecision.foot_pad_requests``.

    Positional commands and ``ANIM`` handling are workstream W5's
    concern, not this function's.
    """
    from .config import (
        DSF_OBJECT_BAKE_MAX_GROUND_SPAN_M,
        DSF_OBJECT_ELEVATED_BASE_M,
        DSF_OBJECT_FOOT_ANCHOR,
        DSF_OBJECT_FOOT_BAND_M,
        DSF_OBJECT_FOOT_CLUSTER_GAP_M,
        DSF_OBJECT_FOOT_CONTACT_TOLERANCE_M,
        DSF_OBJECT_FOOT_MAX_BASE_SPREAD_M,
        DSF_OBJECT_FOOT_PAD_RESIDUAL_M,
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
            # Amendment A18: an OBJECT_AGL placement puts the object's
            # ``y = 0`` plane at ``terrain(anchor) + elevation``, so the
            # effective anchor elevation carries the offset (zero for a
            # plain ``OBJECT``).  Everything downstream — deltas, the
            # rendered-elevation identity, provenance — uses this sum.
            anchor_ground_by_resource[resource_path] = (
                anchor_ground + placement.above_ground_level_metres
            )

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

    # Foot re-anchor pre-pass (project memory
    # kbna-gantry-pond-multi-foot-objects): a structure classified as
    # elevated whose lowest band IS its own object's lowest band carries
    # an author-BAKED vertical offset — the KBNA 45 m stair's lowest
    # solid vertex sits at authored y = +6.5 m.  Such a structure never
    # rests on a sibling structure the way rooftop clutter does; its
    # feet were authored for TERRAIN.  Detect the feet here; pass 2
    # decides between inheritance (all feet over a supporter — genuine
    # baked rooftop clutter) and foot-anchoring (pass 3 seats the best
    # rigid offset across the feet).
    foot_candidate_by_index: dict[int, list[FootCluster]] = {}
    if DSF_OBJECT_FOOT_ANCHOR:
        resource_minimum_solid_y: dict[str, float] = {}
        for resource_path in frame.included_resources:
            geometry = geometry_by_resource[resource_path]
            resource_minimum_solid_y[resource_path] = min(
                geometry.vertices[vertex_index][1]
                for triangle in geometry.solid_triangles
                for vertex_index in triangle
            )
        for structure_index, structure in enumerate(structures):
            if (
                structure_index in skip_reason_by_index
                or structure.is_ground_touching
            ):
                continue
            sits_at_own_lowest_band = any(
                resource_path in resource_minimum_solid_y
                and structure_minimum_base_y
                <= resource_minimum_solid_y[resource_path]
                + DSF_OBJECT_ELEVATED_BASE_M
                for resource_path, structure_minimum_base_y in (
                    structure.minimum_base_y_by_resource.items()
                )
            )
            if not sits_at_own_lowest_band:
                continue
            structure_shared_triangles = shared_triangles_by_structure[
                structure_index
            ]
            used_shared_indices = sorted({
                shared_index
                for triangle in structure_shared_triangles
                for shared_index in triangle
            })
            feet = detect_foot_clusters(
                [
                    frame.shared_vertices[shared_index]
                    for shared_index in used_shared_indices
                ],
                [
                    frame.resource_of_shared_vertex[shared_index]
                    for shared_index in used_shared_indices
                ],
                band_metres=DSF_OBJECT_FOOT_BAND_M,
                cluster_gap_metres=DSF_OBJECT_FOOT_CLUSTER_GAP_M,
                maximum_base_spread_metres=(
                    DSF_OBJECT_FOOT_MAX_BASE_SPREAD_M
                ),
            )
            if feet:
                foot_candidate_by_index[structure_index] = feet

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
    foot_anchored_by_index: dict[int, list[FootCluster]] = {}
    for structure_index, structure in enumerate(structures):
        if (
            structure_index in skip_reason_by_index
            or structure.is_ground_touching
        ):
            continue
        feet = foot_candidate_by_index.get(structure_index)
        if feet:
            # Baked rooftop clutter rests ON a sibling: every foot sits
            # over one ground-touching supporter's box, and inheritance
            # (below) remains the correct seating.  Feet over open
            # terrain mean the author baked the offset against THEIR
            # mesh — the structure is foot-anchored and pass 3 fits the
            # rigid offset across its feet instead.
            supported = any(
                bounding_box_by_structure[candidate_index] is not None
                and all(
                    bounding_box_by_structure[candidate_index][0]
                    <= foot.centroid_x
                    <= bounding_box_by_structure[candidate_index][1]
                    and bounding_box_by_structure[candidate_index][2]
                    <= foot.centroid_z
                    <= bounding_box_by_structure[candidate_index][3]
                    for foot in feet
                )
                for candidate_index in supporter_indices
            )
            if not supported:
                foot_anchored_by_index[structure_index] = feet
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
    foot_clusters_by_structure_index: dict[int, tuple[FootCluster, ...]] = {}
    foot_pad_requests: list[FootPadRequest] = []
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

        anchored_feet = foot_anchored_by_index.get(structure_index)
        if anchored_feet is not None:
            # Foot-anchored: one record per FOOT, sampled under the
            # foot's own contact centroid.  A foot centroid off the
            # mesh borrows the structure centroid's ground — noted,
            # not fatal, exactly like a part centroid below.
            enriched_feet: list[FootCluster] = []
            ground_part_records = []
            for foot in anchored_feet:
                foot_latitude, foot_longitude = _pool_frame_to_world_point(
                    frame.origin_latitude,
                    frame.origin_longitude,
                    foot.centroid_x,
                    foot.centroid_z,
                )
                foot_ground = sampler.elevation_at_or_none(
                    foot_latitude, foot_longitude
                )
                if foot_ground is None:
                    import O4_UI_Utils as UI

                    UI.vprint(
                        2,
                        "  [object-anchor] foot centroid "
                        f"({foot_latitude:.6f}, {foot_longitude:.6f}) "
                        "lies outside the built mesh; using the "
                        "structure centroid's ground for it",
                    )
                    foot_ground = structure_ground
                enriched_feet.append(
                    replace(
                        foot,
                        latitude=foot_latitude,
                        longitude=foot_longitude,
                        ground_metres=foot_ground,
                    )
                )
                ground_part_records.append(
                    (foot_ground, foot.base_y, foot.base_resource)
                )
        else:
            enriched_feet = []
            ground_part_records = []

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
            if structure_shared_triangles and anchored_feet is None
            else []
        )
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

        if anchored_feet is not None:
            part_grounds = [record[0] for record in ground_part_records]
            ground_span_metres = max(part_grounds) - min(part_grounds)
            # Foot-anchored seating: each foot's SEAT TARGET is the
            # world elevation of the object's y = 0 plane that lands
            # that foot exactly on the mesh (its ground minus its
            # authored base — feet with different authored bases are
            # the whole point).  The rigid offset that minimises the
            # WORST foot residual is the midpoint of the kept targets.
            # A foot whose target fell more than the contact tolerance
            # below the topmost target is excluded from the fit — the
            # body rests on its highest contacts; a cluster hanging
            # over a pond must never drag the true feet down.
            seat_targets = [
                foot_ground - foot_base_y
                for foot_ground, foot_base_y, _resource in (
                    ground_part_records
                )
            ]
            topmost_target = max(seat_targets)
            kept_targets = [
                target
                for target in seat_targets
                if target
                >= topmost_target - DSF_OBJECT_FOOT_CONTACT_TOLERANCE_M
            ]
            structure_ground = (
                min(kept_targets) + max(kept_targets)
            ) / 2.0
            enriched_feet = [
                replace(
                    foot,
                    kept_for_fit=(
                        target
                        >= topmost_target
                        - DSF_OBJECT_FOOT_CONTACT_TOLERANCE_M
                    ),
                    residual_metres=(
                        structure_ground + foot.base_y - foot.ground_metres
                    ),
                )
                for foot, target in zip(enriched_feet, seat_targets)
            ]
            foot_clusters_by_structure_index[structure_index] = tuple(
                enriched_feet
            )
        elif ground_part_records:
            part_grounds = [record[0] for record in ground_part_records]
            ground_span_metres = max(part_grounds) - min(part_grounds)
            # Amendment A19: the seating elevation of a structure with
            # ground-touching parts is the MEDIAN of those parts'
            # grounds — the best single rigid offset — not the ground at
            # the area-weighted centroid, which is one unrepresentative
            # sample for a large structure (a kilometre-wide chained web
            # at HECA was judged, and wrongly skipped, by it).
            sorted_grounds = sorted(part_grounds)
            middle = len(sorted_grounds) // 2
            structure_ground = (
                sorted_grounds[middle]
                if len(sorted_grounds) % 2 == 1
                else (sorted_grounds[middle - 1] + sorted_grounds[middle])
                / 2.0
            )
        else:
            ground_span_metres = 0.0
        needs_pad = ground_span_metres > DSF_OBJECT_PAD_FLAG_SPAN_M

        # Rigid-seat span limit (design 2026-07-17, EGGW UK2000 pack): a
        # structure whose ground-contact terrain span exceeds
        # ``DSF_OBJECT_BAKE_MAX_GROUND_SPAN_M`` cannot be seated by one
        # rigid vertical offset — whatever offset the best fit picks,
        # one end floats or sinks past the seating tolerance.  Co-baked
        # payware packs chain real buildings into airport-scale contact
        # components via connector objects; baking one offset for such a
        # component floated the EGGW chains by +33 m and +20 m.  Leave
        # the whole structure at its AUTHORED elevations; its real
        # buildings are carried by their own Phase-1 pads instead.  Feet
        # keep the per-foot machinery below — each foot seats
        # independently, so a large inter-foot span is exactly what that
        # path is for, never a reason to refuse.  A skip is per
        # STRUCTURE: the resource carries no delta, so the byte-idempotent
        # rewrite (and the reversion pass) leave it at its authored y.
        if (
            anchored_feet is None
            and ground_span_metres > DSF_OBJECT_BAKE_MAX_GROUND_SPAN_M
        ):
            updated_structures.append(
                replace(
                    structure,
                    ground_span_metres=ground_span_metres,
                    needs_pad=needs_pad,
                    skip_reason=(
                        f"ground span {ground_span_metres:.1f} m "
                        f"{GROUND_SPAN_SKIP_REASON_PHRASE} — left at "
                        "authored elevations"
                    ),
                )
            )
            continue

        # Amendment A3, bounded by amendment A19: always bake the best
        # single offset; do-not-bake ONLY when the arithmetic says
        # correction worsens the seating AND the structure is small
        # enough for that judgment to be meaningful.  A mega-structure
        # (a chained web) always bakes and flags ``needs_pad`` — its
        # real fix is the hinge cut, never a silent skip.
        a3_skip_reason = None
        bounding_box = bounding_box_by_structure[structure_index]
        structure_diameter_metres = (
            math.hypot(
                bounding_box[1] - bounding_box[0],
                bounding_box[3] - bounding_box[2],
            )
            if bounding_box is not None
            else 0.0
        )
        if ground_part_records and (
            structure_diameter_metres <= A3_GUARD_MAXIMUM_DIAMETER_METRES
        ):
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

        # A baked foot-anchored structure whose rigid offset still
        # leaves a foot off the mesh past the residual threshold gets a
        # per-foot terrain-pad REQUEST — the ground under that foot,
        # not the object, is what needs to move (target recorded).
        if anchored_feet is not None:
            for foot in foot_clusters_by_structure_index[structure_index]:
                if (
                    foot.residual_metres is None
                    or abs(foot.residual_metres)
                    <= DSF_OBJECT_FOOT_PAD_RESIDUAL_M
                ):
                    continue
                foot_pad_requests.append(
                    FootPadRequest(
                        structure_index=structure_index,
                        resource_path=foot.base_resource,
                        latitude=foot.latitude,
                        longitude=foot.longitude,
                        base_y=foot.base_y,
                        residual_metres=foot.residual_metres,
                        target_ground_metres=(
                            structure_ground + foot.base_y
                        ),
                        contact_points_lonlat=tuple(
                            _pool_frame_to_world_point(
                                frame.origin_latitude,
                                frame.origin_longitude,
                                contact_x,
                                contact_z,
                            )[::-1]
                            for contact_x, contact_z in (
                                foot.contact_points
                            )
                        ),
                    )
                )

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

    # Amendment A19: structure-level skips must be VISIBLE at the
    # resource level.  Forty-nine HECA resources vanished from the bake
    # because every structure carrying their geometry was skipped and
    # nothing said so.  One aggregated entry per affected resource — but
    # ONLY for resources left with no delta at all (amendment A21): a
    # resource whose OTHER structures baked stays out of ``skipped``
    # (``object_rebake.apply`` refuses everything listed there), bakes
    # the passing structures' deltas, and surfaces its per-structure
    # skips through the report and the provenance sidecar instead.
    skip_count_by_resource: dict[str, int] = {}
    first_skip_reason_by_resource: dict[str, str] = {}
    for updated_structure in updated_structures:
        if not updated_structure.skip_reason:
            continue
        for resource_path in updated_structure.triangles_by_resource:
            skip_count_by_resource[resource_path] = (
                skip_count_by_resource.get(resource_path, 0) + 1
            )
            first_skip_reason_by_resource.setdefault(
                resource_path, updated_structure.skip_reason
            )
    for resource_path in sorted(skip_count_by_resource):
        if resource_path in delta_by_resource_and_vertex:
            continue
        count = skip_count_by_resource[resource_path]
        skipped.append(
            (
                resource_path,
                f"ALL {count} structure(s) carrying this resource "
                "were skipped — resource left unbaked; first reason: "
                + first_skip_reason_by_resource[resource_path],
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
        foot_clusters_by_structure_index=foot_clusters_by_structure_index,
        foot_pad_requests=foot_pad_requests,
    )
