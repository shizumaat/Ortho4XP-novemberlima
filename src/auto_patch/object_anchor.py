"""Object pooling, structure partitioning and per-structure offsets.

Contract frozen by workstream W1 (``docs/dsf_object_integration_spec.md``
section 3.3, as amended by A1/A3/A10); implementation lands in
workstream W4.  This module is the heart of the correction and the single
most likely place for a subtle bug — read spec section 2.4 and
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

Everything here is pure: geometry and a sampler in, numbers out, no file
input/output (that is ``object_rebake``'s job).
"""

from __future__ import annotations

from dataclasses import dataclass

from .mesh_sampler import MeshElevationSampler
from .obj8_reader import ObjectGeometry, ObjectPlacement

Triangle = tuple[int, int, int]


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
    not away (partition document, section 3 step 0).
    """
    raise NotImplementedError("workstream W4")


def partition_structures(
    pool: ObjectPool,
    geometry_by_resource: dict[str, ObjectGeometry],
    *,
    epsilon_metres: float,
) -> list[Structure]:
    """Partition a pool's solid geometry into structures.

    Thin composition over ``obj8_partition`` (amendment A1 — Phases 1 and
    2 MUST share this partition): project every object's vertices into
    one authored-space frame using its own placement, ``weld_parts``,
    ``contact_graph``, ``connected_structures``, then map back
    per-object.  Draped triangles are discarded before partitioning
    (invariant I-9).
    """
    raise NotImplementedError("workstream W4")


def structure_deltas(
    pool: ObjectPool,
    geometry_by_resource: dict[str, ObjectGeometry],
    structures: list[Structure],
    sampler: MeshElevationSampler,
) -> RebakeDecision:
    """Compute per-(structure, object) y offsets against the built mesh.

    Per structure: sample ``ground_under(centroid)``; on
    ``OutsideMeshError`` skip-and-report, never guess (invariant I-13).
    A structure with no ground-touching part inherits its supporter's
    ground (invariant I-8).  Ground span over the structure's
    ground-touching parts sets ``needs_pad`` past
    ``DSF_OBJECT_PAD_FLAG_SPAN_M``; the structure is still baked with the
    best single offset (amendment A3) unless the single-offset residual
    would exceed the uncorrected residual — the only do-not-bake case,
    decided by arithmetic, not thresholds.
    """
    raise NotImplementedError("workstream W4")
