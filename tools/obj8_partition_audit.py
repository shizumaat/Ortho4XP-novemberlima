"""Audit the OBJ8 structure partition of a scenery pack against a built mesh.

The oracle for workstream W2/W4 of ``docs/dsf_object_integration_spec.md``
(amendment A9), promoting the throwaway probes behind
``docs/obj8_structure_partition.md`` section 1 into a repeatable tool.
For every pool of interacting object definitions it reports:

* **The Pareto table** — fidelity (per-part residual against the mesh)
  versus tearing (vertical separation opened between touching parts),
  for each candidate partition: no correction, the superseded 2D
  bounding-box gap heuristic at 0.5/2/5/20 m, vertex-contact at 5 cm
  (the trap: beautiful residual, thousands of tears), 3D axis-aligned
  bounding-box contact at epsilon (broad phase only), and the shipped
  broad-plus-narrow contact graph from ``auto_patch.obj8_partition``.
* **The epsilon-plateau scan** — contact-graph structure count as a
  function of epsilon, to confirm the chosen tolerance sits on the
  measured plateau (partition document section 2.3).
* **The hard-tear check** in its amended form (invariant I-21): any two
  world-coincident vertices must land at the same post-bake RENDERED
  elevation ``ground(anchor(object)) + y_after`` — never compared by
  delta equality, which multi-anchor structures legitimately violate
  (specification section 2.4).

Correctness rules honoured here:

* Geometry is read from ``<name>.anchor_bak`` originals when present, so
  the audit stays truthful on a live-baked pack (the prototype bake is
  live at KCLT today).
* Residuals are measured per PART, never per structure centroid — the
  centroid check is tautological (plan section 8.3).
* Vertices are pooled in AUTHORED space: horizontal position through
  each object's own placement into one shared east-north-up frame,
  vertical position left as the authored y (partition document,
  section 3 step 1).
* Definitions with more than one placement are skipped and reported
  (plan section 8.2), as are objects mixing draped and solid vertex use
  (invariant I-9).

Usage::

    venv/bin/python tools/obj8_partition_audit.py \
        --dsf PATH --mesh PATH --pack-root PATH [--epsilon 0.25]
        [--min-reach 25] [--resource-filter SUBSTRING]...

Example — reproduce the partition document's section-1 table on the
Nimbus KCLT pack (the ``--resource-filter`` restricts the audit to the
eight co-anchored terminal bakes the document measured)::

    venv/bin/python tools/obj8_partition_audit.py \
        --dsf "/Users/noah/X-Plane 12/Custom Scenery/Nimbus Simulation - KCLT V1.4 - Charlotte XP12/Earth nav data/+30-090/+35-081.dsf" \
        --mesh "/Users/noah/X-Plane 12/Custom Scenery/zOrtho4XP_+35-081/Data+35-081.mesh" \
        --pack-root "/Users/noah/X-Plane 12/Custom Scenery/Nimbus Simulation - KCLT V1.4 - Charlotte XP12" \
        --resource-filter Charlotte_Airport_00
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
from collections import defaultdict

import numpy
from scipy.spatial import cKDTree

_TOOLS_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
_SOURCE_DIRECTORY = os.path.join(os.path.dirname(_TOOLS_DIRECTORY), "src")
if _SOURCE_DIRECTORY not in sys.path:
    sys.path.insert(0, _SOURCE_DIRECTORY)

from auto_patch.dsf_reader import _load_dsf_text  # noqa: E402
from auto_patch.mesh_sampler import MeshElevationSampler  # noqa: E402
from auto_patch.obj8_partition import (  # noqa: E402
    connected_structures,
    contact_graph,
    weld_parts,
)
from auto_patch.obj8_reader import (  # noqa: E402
    METRES_PER_DEGREE_LATITUDE,
    VERTEX_WELD_DECIMALS,
    load_object_file,
    local_offset_to_lonlat,
    lonlat_to_local_offset,
    read_dsf_object_placements,
    resolve_object_resource,
)

# A part whose lowest vertex sits above this rests on another structure,
# not on the terrain, and is excluded from the residual metric
# (mirrors config.DSF_OBJECT_ELEVATED_BASE_M).
ELEVATED_BASE_METRES = 0.5

# Two vertices closer than this in the pooled world frame count as
# world-coincident for the hard-tear relation (the partition document's
# vertex-contact distance).
COINCIDENT_VERTEX_METRES = 0.05

# Rendered-elevation spread above which a coincident-vertex bucket
# counts as torn.  Comfortably above float noise, far below anything
# visible.
HARD_TEAR_TOLERANCE_METRES = 1e-6

# Epsilon values for the plateau scan (partition document section 2.3
# asks for the plateau to be re-derived under the narrow phase).
PLATEAU_EPSILON_VALUES_METRES = [0.02, 0.05, 0.10, 0.25, 0.50, 1.00]

# 2D bounding-box gaps of the superseded heuristic, kept in the Pareto
# table so its coarseness stays measurable on every pack.
SUPERSEDED_GAP_VALUES_METRES = [0.5, 2.0, 5.0, 20.0]


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


def _union_labels(part_count: int, pairs) -> numpy.ndarray:
    parent = list(range(part_count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in pairs:
        left_root, right_root = find(int(left)), find(int(right))
        if left_root != right_root:
            parent[left_root] = right_root
    return numpy.array([find(index) for index in range(part_count)])


class _PooledGeometry:
    """One pool of interacting objects, projected into a shared local
    frame in authored space, split into welded parts.

    The pool frame is a virtual placement at the mean anchor position
    carrying the FIRST member's heading, so that when a family shares
    one heading (the common case — texture-page bakes of one model) the
    pooled coordinates coincide with the authored local axes.  That
    matters only for the axis-aligned-bounding-box candidate rows of
    the Pareto table, which are not rotation invariant; the contact
    graph's narrow phase is pure surface distance and does not care.
    """

    def __init__(self, members, sampler: MeshElevationSampler) -> None:
        self.members = members  # list of (placement, geometry)
        self.sampler = sampler

        anchor_latitudes = [placement.latitude for placement, _ in members]
        anchor_longitudes = [placement.longitude for placement, _ in members]
        self.centre_latitude = sum(anchor_latitudes) / len(anchor_latitudes)
        self.centre_longitude = sum(anchor_longitudes) / len(anchor_longitudes)
        self.frame_heading_degrees = members[0][0].heading_degrees

        pooled_positions: list[tuple[float, float, float]] = []
        self.parts: list[list[tuple[int, int, int]]] = []
        self.part_member_index: list[int] = []
        for member_index, (placement, geometry) in enumerate(members):
            base = len(pooled_positions)
            for local_x, local_y, local_z in geometry.vertices:
                latitude, longitude = local_offset_to_lonlat(
                    placement.latitude,
                    placement.longitude,
                    placement.heading_degrees,
                    local_x,
                    local_z,
                )
                pool_x, pool_z = lonlat_to_local_offset(
                    self.centre_latitude,
                    self.centre_longitude,
                    self.frame_heading_degrees,
                    latitude,
                    longitude,
                )
                pooled_positions.append((pool_x, local_y, pool_z))
            # Weld in each object's own (noise-free) local frame, then
            # re-index the part triangles into the pooled vertex space.
            for part in weld_parts(geometry.vertices, geometry.solid_triangles):
                self.parts.append(
                    [tuple(base + index for index in triangle) for triangle in part]
                )
                self.part_member_index.append(member_index)

        self.vertices = numpy.array(pooled_positions, dtype=numpy.float64)
        self.part_count = len(self.parts)

        self.part_vertex_indices = [
            numpy.array(
                sorted({index for triangle in part for index in triangle}),
                dtype=numpy.int64,
            )
            for part in self.parts
        ]
        self.part_base_y = numpy.array(
            [self.vertices[indices, 1].min() for indices in self.part_vertex_indices]
        )
        self.part_ground_touching = self.part_base_y <= ELEVATED_BASE_METRES

        # Per-part surface area and area-weighted centroid, in the
        # pooled frame (matching the probes behind the partition
        # document's section-1 table).
        self.part_area = numpy.zeros(self.part_count)
        self.part_centroid = numpy.zeros((self.part_count, 2))  # (pool_x, pool_z)
        for part_index, part in enumerate(self.parts):
            area, centroid_x, centroid_z = self._area_weighted_centroid(part)
            self.part_area[part_index] = area
            self.part_centroid[part_index] = (centroid_x, centroid_z)

        self._ground_cache: dict[tuple[float, float], float] = {}
        self.part_ground = numpy.array(
            [
                self.ground_at_pool_frame(
                    self.part_centroid[part_index, 0],
                    self.part_centroid[part_index, 1],
                )
                for part_index in range(self.part_count)
            ]
        )
        self.anchor_ground_by_member = numpy.array(
            [
                self.ground_at_latitude_longitude(
                    placement.latitude, placement.longitude
                )
                for placement, _ in members
            ]
        )

        # 2D and 3D bounding boxes per part, for the candidate
        # partitions in the Pareto table.
        self.part_box = numpy.zeros((self.part_count, 6))
        for part_index, indices in enumerate(self.part_vertex_indices):
            points = self.vertices[indices]
            self.part_box[part_index] = (
                points[:, 0].min(), points[:, 1].min(), points[:, 2].min(),
                points[:, 0].max(), points[:, 1].max(), points[:, 2].max(),
            )

    def _area_weighted_centroid(self, part) -> tuple[float, float, float]:
        total_area = 0.0
        weighted_x = 0.0
        weighted_z = 0.0
        for first, second, third in part:
            a = self.vertices[first]
            b = self.vertices[second]
            c = self.vertices[third]
            cross = numpy.cross(b - a, c - a)
            area = 0.5 * float(numpy.linalg.norm(cross))
            total_area += area
            weighted_x += area * (a[0] + b[0] + c[0]) / 3.0
            weighted_z += area * (a[2] + b[2] + c[2]) / 3.0
        if total_area <= 0.0:
            # Degenerate part: fall back to the vertex mean.
            indices = sorted({index for triangle in part for index in triangle})
            points = self.vertices[indices]
            return 0.0, float(points[:, 0].mean()), float(points[:, 2].mean())
        return (
            total_area,
            weighted_x / total_area,
            weighted_z / total_area,
        )

    def ground_at_latitude_longitude(
        self, latitude: float, longitude: float
    ) -> float:
        key = (round(latitude, 9), round(longitude, 9))
        if key not in self._ground_cache:
            self._ground_cache[key] = self.sampler.elevation_at(
                latitude, longitude
            )
        return self._ground_cache[key]

    def ground_at_pool_frame(self, pool_x: float, pool_z: float) -> float:
        latitude, longitude = local_offset_to_lonlat(
            self.centre_latitude,
            self.centre_longitude,
            self.frame_heading_degrees,
            pool_x,
            pool_z,
        )
        return self.ground_at_latitude_longitude(latitude, longitude)

    # -- candidate adjacency relations for the tear columns ------------

    def coincident_vertex_pairs(self) -> set[tuple[int, int]]:
        """Cross-part pairs holding at least one world-coincident vertex
        pair — the HARD relation: separating such a pair is a visible
        tear at a shared wall/roof seam."""
        owner = numpy.concatenate(
            [
                numpy.full(len(indices), part_index, dtype=numpy.int64)
                for part_index, indices in enumerate(self.part_vertex_indices)
            ]
        )
        points = self.vertices[numpy.concatenate(self.part_vertex_indices)]
        tree = cKDTree(points)
        pairs = tree.query_pairs(COINCIDENT_VERTEX_METRES, output_type="ndarray")
        left_owner = owner[pairs[:, 0]]
        right_owner = owner[pairs[:, 1]]
        differs = left_owner != right_owner
        stacked = numpy.sort(
            numpy.stack([left_owner[differs], right_owner[differs]], axis=1),
            axis=1,
        )
        return {(int(left), int(right)) for left, right in stacked}

    def bounding_box_abutment_pairs(
        self, epsilon_metres: float
    ) -> set[tuple[int, int]]:
        """Cross-part pairs whose 3D axis-aligned bounding boxes come
        within epsilon on every axis (the sound broad-phase relation)."""
        cell_size = 6.0
        buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        box = self.part_box
        for part_index in range(self.part_count):
            first_cell_x = int((box[part_index, 0] - epsilon_metres) // cell_size)
            last_cell_x = int((box[part_index, 3] + epsilon_metres) // cell_size)
            first_cell_z = int((box[part_index, 2] - epsilon_metres) // cell_size)
            last_cell_z = int((box[part_index, 5] + epsilon_metres) // cell_size)
            for cell_x in range(first_cell_x, last_cell_x + 1):
                for cell_z in range(first_cell_z, last_cell_z + 1):
                    buckets[(cell_x, cell_z)].append(part_index)
        pairs: set[tuple[int, int]] = set()
        for bucket in buckets.values():
            for position, left in enumerate(bucket):
                for right in bucket[position + 1 :]:
                    key = (left, right) if left < right else (right, left)
                    if key in pairs:
                        continue
                    if (
                        box[left, 0] - epsilon_metres <= box[right, 3]
                        and box[right, 0] - epsilon_metres <= box[left, 3]
                        and box[left, 1] - epsilon_metres <= box[right, 4]
                        and box[right, 1] - epsilon_metres <= box[left, 4]
                        and box[left, 2] - epsilon_metres <= box[right, 5]
                        and box[right, 2] - epsilon_metres <= box[left, 5]
                    ):
                        pairs.add(key)
        return pairs

    def two_dimensional_gap_labels(self, gap_metres: float) -> numpy.ndarray:
        """The superseded heuristic: single-link merge on horizontal
        bounding-box gap (Euclidean over the per-axis gaps)."""
        cell_size = max(gap_metres * 3.0, 20.0)
        buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        box = self.part_box
        for part_index in range(self.part_count):
            first_cell_x = int((box[part_index, 0] - gap_metres) // cell_size)
            last_cell_x = int((box[part_index, 3] + gap_metres) // cell_size)
            first_cell_z = int((box[part_index, 2] - gap_metres) // cell_size)
            last_cell_z = int((box[part_index, 5] + gap_metres) // cell_size)
            for cell_x in range(first_cell_x, last_cell_x + 1):
                for cell_z in range(first_cell_z, last_cell_z + 1):
                    buckets[(cell_x, cell_z)].append(part_index)
        pairs: set[tuple[int, int]] = set()
        for bucket in buckets.values():
            for position, left in enumerate(bucket):
                for right in bucket[position + 1 :]:
                    key = (left, right) if left < right else (right, left)
                    if key in pairs:
                        continue
                    gap_x = max(
                        0.0,
                        max(
                            box[right, 0] - box[left, 3],
                            box[left, 0] - box[right, 3],
                        ),
                    )
                    gap_z = max(
                        0.0,
                        max(
                            box[right, 2] - box[left, 5],
                            box[left, 2] - box[right, 5],
                        ),
                    )
                    if math.hypot(gap_x, gap_z) < gap_metres:
                        pairs.add(key)
        return _union_labels(self.part_count, pairs)

    # -- evaluation ----------------------------------------------------

    def structure_ground_per_part(self, labels: numpy.ndarray) -> numpy.ndarray:
        """Ground elevation at each part's structure centroid (area
        weighted across the structure's parts)."""
        ground_per_part = numpy.zeros(self.part_count)
        groups: dict[int, list[int]] = defaultdict(list)
        for part_index, label in enumerate(labels):
            groups[int(label)].append(part_index)
        for members in groups.values():
            weights = self.part_area[members]
            if weights.sum() > 0.0:
                centroid_x = float(
                    (weights * self.part_centroid[members, 0]).sum()
                    / weights.sum()
                )
                centroid_z = float(
                    (weights * self.part_centroid[members, 1]).sum()
                    / weights.sum()
                )
                ground = self.ground_at_pool_frame(centroid_x, centroid_z)
            else:
                ground = float(self.part_ground[members].mean())
            for member in members:
                ground_per_part[member] = ground
        return ground_per_part

    def evaluate_partition(
        self,
        name: str,
        labels: numpy.ndarray,
        hard_pairs: set[tuple[int, int]],
        abutment_pairs: set[tuple[int, int]],
    ) -> None:
        """One Pareto-table row: residual quantiles plus tear counts.

        Tears are computed through the amended rendered-elevation form
        (invariant I-21): the rendered elevation of a vertex of part
        ``p`` from member ``m`` is ``ground(anchor(m)) + y +
        delta(structure(p), m)``, and the anchor term cancels to
        ``ground(structure_centroid) + y`` — so two touching parts in
        different structures separate by exactly the difference of
        their structures' centroid grounds, regardless of how many
        anchors contribute.  Delta equality is never compared.
        """
        structure_ground = self.structure_ground_per_part(labels)
        grounded = self.part_ground_touching
        residuals = numpy.abs(
            structure_ground[grounded]
            + self.part_base_y[grounded]
            - self.part_ground[grounded]
        )

        def separations(pairs: set[tuple[int, int]]) -> numpy.ndarray:
            values = [
                abs(structure_ground[left] - structure_ground[right])
                for left, right in pairs
                if labels[left] != labels[right]
            ]
            return numpy.array(values) if values else numpy.array([0.0])

        hard_separations = separations(hard_pairs)
        abutment_separations = separations(abutment_pairs)
        structure_count = len(set(labels.tolist()))
        print(
            f"{name:34} structures={structure_count:5d} | residual "
            f"p50={numpy.median(residuals):5.2f} "
            f"p90={numpy.percentile(residuals, 90):5.2f} "
            f"max={residuals.max():6.2f} "
            f">0.5m={int((residuals > 0.5).sum()):4d}/{len(residuals)} | "
            f"hard tears n={int((hard_separations > HARD_TEAR_TOLERANCE_METRES).sum()):5d} "
            f"max={hard_separations.max():5.2f} | "
            f"abutment tears n={int((abutment_separations > HARD_TEAR_TOLERANCE_METRES).sum()):5d} "
            f"max={abutment_separations.max():5.2f}"
        )

    def hard_tear_check(self, labels: numpy.ndarray) -> tuple[int, int, float]:
        """Invariant I-21: bucket pooled vertices by world position
        quantized at the weld tolerance; within each bucket every vertex
        must land at the same post-bake rendered elevation
        ``ground(anchor(object)) + y_after``.

        Two exactly coincident vertices share their authored y, so their
        rendered elevations must be equal; to keep quantization noise
        (vertices up to a millimetre apart sharing a bucket) out of the
        comparison, each member's rendered elevation is referenced to the
        bucket by subtracting its own authored y.  The compared quantity
        is therefore ``ground(anchor(object)) + delta(structure, object)``
        — the rendered elevation of the object's y = 0 plane — in which
        the anchor ground stays present, so this is NOT a delta-equality
        check: objects with different anchors in one structure pass, and
        the collapsed per-structure-delta bug (specification section 2.4)
        fails.  Uses only the outputs (structure grounds and authored y),
        independent of how the partition was derived."""
        structure_ground = self.structure_ground_per_part(labels)
        buckets: dict[tuple[int, int, int], list[float]] = defaultdict(list)
        weld_scale = 10.0 ** VERTEX_WELD_DECIMALS
        for part_index, indices in enumerate(self.part_vertex_indices):
            for pool_x, authored_y, pool_z in self.vertices[indices]:
                key = (
                    int(round(pool_x * weld_scale)),
                    int(round(authored_y * weld_scale)),
                    int(round(pool_z * weld_scale)),
                )
                rendered = structure_ground[part_index] + authored_y
                buckets[key].append(rendered - authored_y)
        torn = 0
        shared = 0
        worst_spread = 0.0
        for rendered_values in buckets.values():
            if len(rendered_values) < 2:
                continue
            shared += 1
            spread = max(rendered_values) - min(rendered_values)
            worst_spread = max(worst_spread, spread)
            if spread > HARD_TEAR_TOLERANCE_METRES:
                torn += 1
        return shared, torn, worst_spread


def _discover_pools(candidates, epsilon_metres: float):
    """Group candidate (placement, geometry) members by transitive world
    axis-aligned bounding-box overlap (invariant I-1: pooling is a world
    geometry property, not an anchor property)."""
    boxes = []
    for placement, geometry in candidates:
        used = {
            index
            for triangle in geometry.solid_triangles
            for index in triangle
        }
        latitudes = []
        longitudes = []
        for index in used:
            local_x, _, local_z = geometry.vertices[index]
            latitude, longitude = local_offset_to_lonlat(
                placement.latitude,
                placement.longitude,
                placement.heading_degrees,
                local_x,
                local_z,
            )
            latitudes.append(latitude)
            longitudes.append(longitude)
        margin_latitude = epsilon_metres / METRES_PER_DEGREE_LATITUDE
        margin_longitude = epsilon_metres / (
            METRES_PER_DEGREE_LATITUDE
            * math.cos(math.radians(placement.latitude))
        )
        boxes.append(
            (
                min(longitudes) - margin_longitude,
                min(latitudes) - margin_latitude,
                max(longitudes) + margin_longitude,
                max(latitudes) + margin_latitude,
            )
        )

    pairs = []
    for left in range(len(candidates)):
        for right in range(left + 1, len(candidates)):
            if (
                boxes[left][0] <= boxes[right][2]
                and boxes[right][0] <= boxes[left][2]
                and boxes[left][1] <= boxes[right][3]
                and boxes[right][1] <= boxes[left][3]
            ):
                pairs.append((left, right))
    labels = _union_labels(len(candidates), pairs)
    pools: dict[int, list] = defaultdict(list)
    for candidate_index, label in enumerate(labels):
        pools[int(label)].append(candidates[candidate_index])
    return sorted(
        pools.values(),
        key=lambda members: -sum(len(g.vertices) for _, g in members),
    )


def _audit_pool(pool_number: int, members, sampler, epsilon_metres: float) -> int:
    pool = _PooledGeometry(members, sampler)
    distinct_anchors = {
        (
            round(placement.latitude, 9),
            round(placement.longitude, 9),
            round(placement.heading_degrees, 6),
        )
        for placement, _ in members
    }
    print(
        f"\n== pool {pool_number}: {len(members)} objects, "
        f"{len(pool.vertices)} vertices, {pool.part_count} parts "
        f"({int(pool.part_ground_touching.sum())} ground-touching), "
        f"{len(distinct_anchors)} distinct anchor(s) =="
    )
    for placement, _ in members:
        print(f"   {placement.resource_path}")

    hard_pairs = pool.coincident_vertex_pairs()
    abutment_pairs = pool.bounding_box_abutment_pairs(epsilon_metres)
    print(
        f"\nadjacency: {len(hard_pairs)} coincident-vertex pairs "
        f"({COINCIDENT_VERTEX_METRES:g} m), {len(abutment_pairs)} "
        f"bounding-box abutment pairs ({epsilon_metres:g} m)"
    )

    # -- Pareto table ----------------------------------------------------
    print("\n[Pareto: fidelity versus tearing, per candidate partition]")
    grounded = pool.part_ground_touching
    uncorrected_rendered = (
        pool.anchor_ground_by_member[
            numpy.array(pool.part_member_index)[grounded]
        ]
        + pool.part_base_y[grounded]
    )
    uncorrected_residuals = numpy.abs(uncorrected_rendered - pool.part_ground[grounded])
    print(
        f"{'(no correction)':34} structures={'-':>5} | residual "
        f"p50={numpy.median(uncorrected_residuals):5.2f} "
        f"p90={numpy.percentile(uncorrected_residuals, 90):5.2f} "
        f"max={uncorrected_residuals.max():6.2f} "
        f">0.5m={int((uncorrected_residuals > 0.5).sum()):4d}"
        f"/{len(uncorrected_residuals)}"
    )
    for gap_metres in SUPERSEDED_GAP_VALUES_METRES:
        pool.evaluate_partition(
            f"2D bounding-box gap {gap_metres:g} m",
            pool.two_dimensional_gap_labels(gap_metres),
            hard_pairs,
            abutment_pairs,
        )
    pool.evaluate_partition(
        f"vertex-contact {COINCIDENT_VERTEX_METRES:g} m (the trap)",
        _union_labels(pool.part_count, hard_pairs),
        hard_pairs,
        abutment_pairs,
    )
    pool.evaluate_partition(
        f"3D bounding-box contact {epsilon_metres:g} m",
        _union_labels(pool.part_count, abutment_pairs),
        hard_pairs,
        abutment_pairs,
    )
    contact_edges = contact_graph(
        [tuple(position) for position in pool.vertices],
        pool.parts,
        epsilon_metres,
    )
    contact_labels = _union_labels(pool.part_count, contact_edges)
    pool.evaluate_partition(
        f"contact graph {epsilon_metres:g} m (shipped)",
        contact_labels,
        hard_pairs,
        abutment_pairs,
    )

    # -- epsilon plateau ---------------------------------------------------
    print("\n[epsilon plateau: contact-graph structure count per epsilon]")
    pooled_vertex_tuples = [tuple(position) for position in pool.vertices]
    for plateau_epsilon in PLATEAU_EPSILON_VALUES_METRES:
        if plateau_epsilon == epsilon_metres:
            edges = contact_edges
        else:
            edges = contact_graph(
                pooled_vertex_tuples, pool.parts, plateau_epsilon
            )
        structures = connected_structures(pool.part_count, edges)
        print(f"   epsilon {plateau_epsilon:5.2f} m -> {len(structures):5d} structures")

    # -- hard-tear check (invariant I-21) -----------------------------------
    shared, torn, worst_spread = pool.hard_tear_check(contact_labels)
    print(
        f"\n[hard-tear check @ epsilon {epsilon_metres:g} m] "
        f"{shared} shared world positions, {torn} torn, "
        f"worst rendered-elevation spread {worst_spread:.9f} m"
    )
    if torn:
        print("   *** HARD TEARS PRESENT — the partition is not admissible ***")
    return torn


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dsf", required=True, help="DSF file to audit")
    parser.add_argument(
        "--mesh", required=True, help="built Data<tile>.mesh to sample"
    )
    parser.add_argument(
        "--pack-root", required=True, help="scenery pack root directory"
    )
    parser.add_argument(
        "--epsilon",
        type=float,
        default=0.25,
        help="contact tolerance in metres (default 0.25, the measured plateau)",
    )
    parser.add_argument("--xplane-root", default=None)
    parser.add_argument(
        "--min-reach",
        type=float,
        default=25.0,
        help="solid-reach floor in metres for candidate definitions",
    )
    parser.add_argument(
        "--resource-filter",
        action="append",
        default=[],
        help="only audit definitions whose resource path contains this "
        "substring (repeatable)",
    )
    arguments = parser.parse_args()

    xplane_root = arguments.xplane_root or _default_xplane_root(arguments.dsf)
    lines = _load_dsf_text(arguments.dsf, cache_dir=tempfile.gettempdir())
    if lines is None:
        print("DSFTool could not read the DSF", file=sys.stderr)
        return 2

    placements = read_dsf_object_placements(lines)
    placements_by_resource: dict[str, list] = defaultdict(list)
    for placement in placements:
        placements_by_resource[placement.resource_path].append(placement)

    candidates = []
    skipped: list[tuple[str, str]] = []
    for resource, resource_placements in placements_by_resource.items():
        if arguments.resource_filter and not any(
            substring in resource for substring in arguments.resource_filter
        ):
            continue
        if len(resource_placements) > 1:
            skipped.append(
                (resource, f"{len(resource_placements)} placements")
            )
            continue
        resolved = resolve_object_resource(
            resource, arguments.pack_root, xplane_root
        )
        if not resolved:
            skipped.append((resource, "unresolved"))
            continue
        # Stay truthful on a live-baked pack: the original geometry is
        # in the backup, the live file already carries the offsets.
        backup = resolved + ".anchor_bak"
        geometry = load_object_file(
            backup if os.path.isfile(backup) else resolved
        )
        if not geometry.has_solid_geometry:
            continue
        if geometry.has_mixed_draped_solid_vertices:
            skipped.append((resource, "mixed draped/solid vertices"))
            continue
        if geometry.solid_reach_metres() < arguments.min_reach:
            continue
        candidates.append((resource_placements[0], geometry))

    print(
        f"{len(placements_by_resource)} definitions, "
        f"{len(placements)} terrain-draped placements, "
        f"{len(candidates)} candidates "
        f"(reach >= {arguments.min_reach:g} m, single placement)"
    )
    for resource, reason in skipped:
        print(f"   skipped: {resource} ({reason})")
    if not candidates:
        print("nothing to audit")
        return 0

    all_latitudes = [p.latitude for p, _ in candidates]
    all_longitudes = [p.longitude for p, _ in candidates]
    greatest_reach = max(g.solid_reach_metres() for _, g in candidates)
    margin_degrees = (greatest_reach + 300.0) / METRES_PER_DEGREE_LATITUDE
    sampler = MeshElevationSampler(
        arguments.mesh,
        (
            min(all_longitudes) - margin_degrees,
            min(all_latitudes) - margin_degrees,
            max(all_longitudes) + margin_degrees,
            max(all_latitudes) + margin_degrees,
        ),
        margin_degrees=0.0,
    )

    pools = _discover_pools(candidates, arguments.epsilon)
    print(f"{len(pools)} pool(s) by transitive world bounding-box overlap")
    torn_total = 0
    for pool_number, members in enumerate(pools, start=1):
        torn_total += _audit_pool(
            pool_number, members, sampler, arguments.epsilon
        )
    return 1 if torn_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
