"""Partition pooled OBJ8 geometry into structures via the contact graph.

Contract frozen by workstream W1 (amendment A1 of
``docs/dsf_object_integration_spec.md``); implemented in workstream W2.
The theory, the measurements, and the traps are in
``docs/obj8_structure_partition.md`` — read it in full before touching
this module.

The one-paragraph version.  With the correction
``delta(S, O) = ground_under(centroid(S)) - ground_under(anchor(O))``
applied to every vertex of structure ``S`` contributed by object ``O``,
the anchor term cancels at render time, so every structure moves as a
rigid body under pure vertical translation and its internal assembly is
preserved exactly.  All distortion therefore lives on structure
boundaries, which makes the optimal partition exactly the connected
components of the epsilon-contact graph: any coarser partition pays
residual for nothing, any finer partition tears contacting parts apart.
The only parameter is epsilon — a modelling tolerance ("how large a gap
did the author leave between a wall and its roof"), measured on a plateau
of 0.02–0.25 m at KCLT (``DSF_OBJECT_CONTACT_EPSILON_M``).

The trap (do not "fix" this): vertex-connectivity is NOT contact.  These
bakes are triangle soup — a wall abuts the roof it holds up without
sharing a vertex.  Partitioning on vertex-connectivity alone measured
4,453 torn abutments up to 11.39 m at KCLT.  Contact needs a broad phase
(3D axis-aligned bounding boxes, which is sound: box gap never exceeds
surface gap, so it can over-merge but never tear) and a narrow phase
(surface distance, pruning 43% of the broad-phase edges at KCLT).  A pair
whose contact cannot be PROVED absent keeps its edge — tearing is the
unrecoverable failure, over-merging costs centimetres (invariant I-20).

Narrow-phase honesty note: the surface-distance test is vertex-to-triangle
in both directions.  A pure edge-edge crossing with no vertex proximity is
missed by it — but for abutting building parts that configuration implies
interpenetration, which the vertex tests catch in practice (partition
document, section 3 step 3).

Frames: callers pool geometry across objects in AUTHORED space — world
XZ through each object's own placement, projected into one local
east-north-up frame centred on the pool, with y left as the authored
``v.y`` (never ``terrain(anchor) + v.y``).  The author assembled the
parts against a common assumed-flat plane; authored space is the frame
in which they fit (partition document, section 3 step 1).

Exactness caveat (amendment A7): the anchor cancellation equates our
mesh sample at the anchor with X-Plane's render-time terrain sample.
Those agree only up to DSF elevation-pool quantisation — a per-object
constant of roughly centimetres.  Within one object the assembly is
exact regardless.  Do not chase a 2 cm cross-object "tear".
"""

from __future__ import annotations

import math
from collections import defaultdict

import numpy
from scipy.spatial import cKDTree

from .obj8_reader import VERTEX_WELD_DECIMALS

Triangle = tuple[int, int, int]

# Broad-phase uniform-grid cell size (metres) on the horizontal plane.
# Purely a performance knob: every axis-aligned-bounding-box pair within
# a bucket is still tested exactly.
BROAD_PHASE_GRID_CELL_METRES = 6.0

# Narrow-phase work ceiling per part pair, in point-times-triangle
# operations after candidate restriction.  A pair that would exceed the
# budget in BOTH test directions cannot be proved apart and therefore
# KEEPS its contact edge (invariant I-20: merge on doubt; over-merging
# costs centimetres, tearing is unrecoverable).
NARROW_PHASE_POINT_TRIANGLE_BUDGET = 400_000


def weld_parts(
    vertices: list[tuple[float, float, float]],
    triangles: list[Triangle],
) -> list[list[Triangle]]:
    """Split a triangle soup into position-welded connectivity classes
    ("parts") — level 1 of the part/structure/inherited hierarchy.

    Vertices at the same position (``VERTEX_WELD_DECIMALS``) are welded
    first: exporters duplicate a position once per texture seam or
    smoothing group, which would otherwise shatter a single wall into
    dozens of parts.  Callers pre-merge ``ANIM_begin`` blocks and fold
    ``ATTR_LOD`` copies before welding (partition document, section 3
    step 2); draped triangles never enter (invariant I-9).

    Subsumes the prototype's ``connected_components`` (amendment A10);
    ``group_components_into_structures`` is deliberately not ported.
    """
    parent = list(range(len(vertices)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    position_to_vertex: dict[tuple[float, float, float], int] = {}
    for triangle in triangles:
        for index in triangle:
            vertex = vertices[index]
            key = (
                round(vertex[0], VERTEX_WELD_DECIMALS),
                round(vertex[1], VERTEX_WELD_DECIMALS),
                round(vertex[2], VERTEX_WELD_DECIMALS),
            )
            if key in position_to_vertex:
                union(index, position_to_vertex[key])
            else:
                position_to_vertex[key] = index

    for first, second, third in triangles:
        union(first, second)
        union(second, third)

    grouped: dict[int, list[Triangle]] = defaultdict(list)
    for triangle in triangles:
        grouped[find(triangle[0])].append(triangle)
    return list(grouped.values())


def _point_triangle_minimum_distances(
    points: numpy.ndarray,
    triangle_corner_a: numpy.ndarray,
    triangle_corner_b: numpy.ndarray,
    triangle_corner_c: numpy.ndarray,
) -> numpy.ndarray:
    """Minimum distance from each point to each triangle, vectorised over
    the points AND the triangles (the standard Voronoi-region
    point-triangle test).

    Corner arrays of shape ``(3,)`` describe one triangle and produce the
    historical ``(P,)`` result; shape ``(T, 3)`` produces ``(P, T)`` in
    one broadcast.  The batched form is the cold-build hot path: the
    2026-07-15 profile showed the narrow phase spending its time in a
    Python loop calling the one-triangle form per candidate triangle
    (the ``NARROW_PHASE_POINT_TRIANGLE_BUDGET`` cap keeps one batch
    under ~400k point-triangle pairs, ~10 MB per broadcast array).

    A degenerate (zero-area) triangle takes the final plane-projection
    branch with distance 0.0 — deliberately erring toward CONTACT, per
    invariant I-20 (numerical doubt merges, never tears).
    """
    single_triangle = triangle_corner_a.ndim == 1
    corner_a = numpy.atleast_2d(triangle_corner_a)
    corner_b = numpy.atleast_2d(triangle_corner_b)
    corner_c = numpy.atleast_2d(triangle_corner_c)

    edge_ab = corner_b - corner_a                          # (T, 3)
    edge_ac = corner_c - corner_a
    from_a = points[:, None, :] - corner_a[None, :, :]     # (P, T, 3)
    from_b = points[:, None, :] - corner_b[None, :, :]
    from_c = points[:, None, :] - corner_c[None, :, :]
    dot_1 = numpy.einsum("ptk,tk->pt", from_a, edge_ab)    # (P, T)
    dot_2 = numpy.einsum("ptk,tk->pt", from_a, edge_ac)
    dot_3 = numpy.einsum("ptk,tk->pt", from_b, edge_ab)
    dot_4 = numpy.einsum("ptk,tk->pt", from_b, edge_ac)
    dot_5 = numpy.einsum("ptk,tk->pt", from_c, edge_ab)
    dot_6 = numpy.einsum("ptk,tk->pt", from_c, edge_ac)
    distances = numpy.full(dot_1.shape, numpy.inf)

    # Per-element edge/corner vectors for masked (point, triangle) pairs:
    # broadcast views cost nothing until a boolean mask materialises the
    # selected rows.
    edge_ab_at = numpy.broadcast_to(edge_ab[None, :, :], from_a.shape)
    edge_ac_at = numpy.broadcast_to(edge_ac[None, :, :], from_a.shape)
    corner_b_at = numpy.broadcast_to(corner_b[None, :, :], from_a.shape)
    corner_c_at = numpy.broadcast_to(corner_c[None, :, :], from_a.shape)
    points_at = numpy.broadcast_to(points[:, None, :], from_a.shape)

    # Degenerate-edge guard (found live at HECA): the edge branches below
    # divide by |edge| squared, so a zero-length edge (duplicate triangle
    # corners) yields 0/0 = not-a-number, and ndarray.min() PROPAGATES it —
    # one degenerate triangle would poison the whole pair's minimum and
    # flip "in contact" to "proved apart", the exact tear invariant I-20
    # forbids.  Sanitised to 0.0 (contact) before returning; the caller
    # silences the noise-only warning.
    in_corner_a = (dot_1 <= 0) & (dot_2 <= 0)
    distances[in_corner_a] = numpy.linalg.norm(from_a[in_corner_a], axis=1)
    in_corner_b = (dot_3 >= 0) & (dot_4 <= dot_3) & ~in_corner_a
    distances[in_corner_b] = numpy.linalg.norm(from_b[in_corner_b], axis=1)
    in_corner_c = (
        (dot_6 >= 0) & (dot_5 <= dot_6) & ~in_corner_a & ~in_corner_b
    )
    distances[in_corner_c] = numpy.linalg.norm(from_c[in_corner_c], axis=1)
    remaining = ~in_corner_a & ~in_corner_b & ~in_corner_c
    if remaining.any():
        barycentric_c = dot_1 * dot_4 - dot_3 * dot_2
        barycentric_a = dot_3 * dot_6 - dot_5 * dot_4
        barycentric_b = dot_5 * dot_2 - dot_1 * dot_6
        on_edge_ab = remaining & (barycentric_c <= 0) & (dot_1 >= 0) & (dot_3 <= 0)
        if on_edge_ab.any():
            parameter = (dot_1[on_edge_ab] / (dot_1[on_edge_ab] - dot_3[on_edge_ab]))[:, None]
            distances[on_edge_ab] = numpy.linalg.norm(
                from_a[on_edge_ab] - parameter * edge_ab_at[on_edge_ab],
                axis=1,
            )
        on_edge_ac = (
            remaining
            & (barycentric_b <= 0)
            & (dot_2 >= 0)
            & (dot_6 <= 0)
            & numpy.isinf(distances)
        )
        if on_edge_ac.any():
            parameter = (dot_2[on_edge_ac] / (dot_2[on_edge_ac] - dot_6[on_edge_ac]))[:, None]
            distances[on_edge_ac] = numpy.linalg.norm(
                from_a[on_edge_ac] - parameter * edge_ac_at[on_edge_ac],
                axis=1,
            )
        on_edge_bc = (
            remaining
            & (barycentric_a <= 0)
            & ((dot_4 - dot_3) >= 0)
            & ((dot_5 - dot_6) >= 0)
            & numpy.isinf(distances)
        )
        if on_edge_bc.any():
            parameter = (
                (dot_4[on_edge_bc] - dot_3[on_edge_bc])
                / (
                    (dot_4[on_edge_bc] - dot_3[on_edge_bc])
                    + (dot_5[on_edge_bc] - dot_6[on_edge_bc])
                )
            )[:, None]
            distances[on_edge_bc] = numpy.linalg.norm(
                points_at[on_edge_bc]
                - (
                    corner_b_at[on_edge_bc]
                    + parameter
                    * (corner_c_at[on_edge_bc] - corner_b_at[on_edge_bc])
                ),
                axis=1,
            )
        interior = remaining & numpy.isinf(distances)
        if interior.any():
            normal = numpy.cross(edge_ab, edge_ac)             # (T, 3)
            normal_length = numpy.linalg.norm(normal, axis=1)  # (T,)
            point_rows, triangle_columns = numpy.nonzero(interior)
            plane_offsets = numpy.abs(
                numpy.einsum(
                    "kj,kj->k",
                    from_a[point_rows, triangle_columns],
                    normal[triangle_columns],
                )
            )
            lengths = normal_length[triangle_columns]
            interior_distances = numpy.zeros(len(point_rows))
            usable = lengths > 1e-12
            interior_distances[usable] = (
                plane_offsets[usable] / lengths[usable]
            )
            distances[point_rows, triangle_columns] = interior_distances
    # Degenerate-edge guard: 0/0 in the edge branches yields not-a-number,
    # which would otherwise poison ndarray.min() for the whole pair and
    # flip contact to proved-apart.  Numerical doubt is CONTACT (I-20).
    non_finite = ~numpy.isfinite(distances)
    if non_finite.any():
        distances[non_finite] = 0.0
    return distances[:, 0] if single_triangle else distances


# Below this many points/triangles a part's full-array box mask is
# cheaper than cell-index bookkeeping — small parts skip the index.
_SPATIAL_INDEX_MINIMUM = 512

# A query box covering more cells than this falls back to the full-array
# mask (large-vs-large pairs; gathering most of the index would only add
# overhead).
_SPATIAL_QUERY_CELL_CAP = 256


class _PartGeometry:
    """Per-part cached arrays for the contact graph: vertex positions,
    triangle corner arrays, 3D axis-aligned bounding boxes (one per part
    and one per triangle), a lazily built vertex k-d tree, and lazily
    built horizontal (x, z) cell indexes over points and triangle boxes.

    The cell indexes are pure prefilters: a query gathers the cells its
    box overlaps (a superset) and the caller's EXACT mask then filters,
    so results are identical to the full-array scan they replace — the
    per-pair full scans were the cold-build hot spot once the distance
    kernel was batched (2026-07-15)."""

    def __init__(
        self, vertex_array: numpy.ndarray, triangles: list[Triangle]
    ) -> None:
        used_vertex_indices = numpy.array(
            sorted({index for triangle in triangles for index in triangle}),
            dtype=numpy.int64,
        )
        self.points = vertex_array[used_vertex_indices]
        triangle_array = numpy.array(triangles, dtype=numpy.int64)
        self.corner_a = vertex_array[triangle_array[:, 0]]
        self.corner_b = vertex_array[triangle_array[:, 1]]
        self.corner_c = vertex_array[triangle_array[:, 2]]
        corners = numpy.stack(
            (self.corner_a, self.corner_b, self.corner_c), axis=1
        )
        self.triangle_minimum = corners.min(axis=1)
        self.triangle_maximum = corners.max(axis=1)
        self.box_minimum = self.points.min(axis=0)
        self.box_maximum = self.points.max(axis=0)
        self._vertex_tree: cKDTree | None = None
        self._point_cells: dict[tuple[int, int], numpy.ndarray] | None = None
        self._triangle_cells: (
            dict[tuple[int, int], numpy.ndarray] | None
        ) = None

    @property
    def vertex_tree(self) -> cKDTree:
        if self._vertex_tree is None:
            self._vertex_tree = cKDTree(self.points)
        return self._vertex_tree

    @staticmethod
    def _cell_range(
        low: float, high: float
    ) -> tuple[int, int]:
        cell = BROAD_PHASE_GRID_CELL_METRES
        return int(math.floor(low / cell)), int(math.floor(high / cell))

    def _gather(
        self,
        cells: dict[tuple[int, int], numpy.ndarray],
        box_minimum: numpy.ndarray,
        box_maximum: numpy.ndarray,
        epsilon_metres: float,
    ) -> numpy.ndarray | None:
        """Indices found in the cells the inflated box overlaps, or
        ``None`` when the box covers too many cells to be worth it."""
        x_low, x_high = self._cell_range(
            box_minimum[0] - epsilon_metres, box_maximum[0] + epsilon_metres
        )
        z_low, z_high = self._cell_range(
            box_minimum[2] - epsilon_metres, box_maximum[2] + epsilon_metres
        )
        if (x_high - x_low + 1) * (z_high - z_low + 1) > (
            _SPATIAL_QUERY_CELL_CAP
        ):
            return None
        found = []
        for cell_x in range(x_low, x_high + 1):
            for cell_z in range(z_low, z_high + 1):
                bucket = cells.get((cell_x, cell_z))
                if bucket is not None:
                    found.append(bucket)
        return (
            numpy.concatenate(found)
            if found
            else numpy.empty(0, dtype=numpy.int64)
        )

    def points_inside_box(
        self,
        box_minimum: numpy.ndarray,
        box_maximum: numpy.ndarray,
        epsilon_metres: float,
    ) -> numpy.ndarray:
        """``self.points`` inside the inflated box — exactly the set the
        historical full-array mask produced."""
        if len(self.points) < _SPATIAL_INDEX_MINIMUM:
            return _points_inside_box(
                self.points, box_minimum, box_maximum, epsilon_metres
            )
        if self._point_cells is None:
            self._point_cells = _build_cell_index(
                self.points[:, 0], self.points[:, 2],
                self.points[:, 0], self.points[:, 2],
            )
        indices = self._gather(
            self._point_cells, box_minimum, box_maximum, epsilon_metres
        )
        if indices is None:
            return _points_inside_box(
                self.points, box_minimum, box_maximum, epsilon_metres
            )
        return _points_inside_box(
            self.points[indices], box_minimum, box_maximum, epsilon_metres
        )

    def candidate_triangles_inside_box(
        self,
        box_minimum: numpy.ndarray,
        box_maximum: numpy.ndarray,
        epsilon_metres: float,
    ) -> numpy.ndarray:
        """Indices of triangles whose bounding box overlaps the inflated
        box — exactly the historical full-array candidate set (sorted
        ascending)."""
        candidate_indices = None
        if len(self.corner_a) >= _SPATIAL_INDEX_MINIMUM:
            if self._triangle_cells is None:
                self._triangle_cells = _build_cell_index(
                    self.triangle_minimum[:, 0],
                    self.triangle_minimum[:, 2],
                    self.triangle_maximum[:, 0],
                    self.triangle_maximum[:, 2],
                )
            gathered = self._gather(
                self._triangle_cells, box_minimum, box_maximum,
                epsilon_metres,
            )
            if gathered is not None:
                candidate_indices = numpy.unique(gathered)
        if candidate_indices is None:
            candidate_indices = numpy.arange(
                len(self.corner_a), dtype=numpy.int64
            )
        overlaps = (
            (
                self.triangle_minimum[candidate_indices]
                <= box_maximum + epsilon_metres
            )
            & (
                self.triangle_maximum[candidate_indices]
                >= box_minimum - epsilon_metres
            )
        ).all(axis=1)
        return candidate_indices[overlaps]


def _build_cell_index(
    minimum_x: numpy.ndarray,
    minimum_z: numpy.ndarray,
    maximum_x: numpy.ndarray,
    maximum_z: numpy.ndarray,
) -> dict[tuple[int, int], numpy.ndarray]:
    """Horizontal cell index: each element index lands in every cell its
    (x, z) extent overlaps (points pass identical minimum/maximum)."""
    cell = BROAD_PHASE_GRID_CELL_METRES
    x_low = numpy.floor(minimum_x / cell).astype(numpy.int64)
    x_high = numpy.floor(maximum_x / cell).astype(numpy.int64)
    z_low = numpy.floor(minimum_z / cell).astype(numpy.int64)
    z_high = numpy.floor(maximum_z / cell).astype(numpy.int64)
    cells: dict[tuple[int, int], list[int]] = {}
    for index in range(len(x_low)):
        for cell_x in range(x_low[index], x_high[index] + 1):
            for cell_z in range(z_low[index], z_high[index] + 1):
                cells.setdefault((cell_x, cell_z), []).append(index)
    return {
        key: numpy.array(indices, dtype=numpy.int64)
        for key, indices in cells.items()
    }


def _points_inside_box(
    points: numpy.ndarray,
    box_minimum: numpy.ndarray,
    box_maximum: numpy.ndarray,
    epsilon_metres: float,
) -> numpy.ndarray:
    inside = (
        (points >= box_minimum - epsilon_metres)
        & (points <= box_maximum + epsilon_metres)
    ).all(axis=1)
    return points[inside]


def _vertex_to_triangle_proof(
    points: numpy.ndarray,
    other: _PartGeometry,
    epsilon_metres: float,
) -> tuple[bool, bool]:
    """One direction of the narrow phase: are any of ``points`` within
    ``epsilon_metres`` of ``other``'s triangle surfaces?

    Returns ``(contact_found, proof_complete)``.  ``proof_complete`` is
    False when the point-times-triangle budget was exhausted, in which
    case the caller must keep the edge (invariant I-20).

    Both candidate sets are restricted losslessly first: a point within
    epsilon of a triangle necessarily lies inside that triangle's
    bounding box inflated by epsilon, and vice versa.
    """
    if len(points) == 0:
        return False, True
    points_minimum = points.min(axis=0)
    points_maximum = points.max(axis=0)
    candidate_triangles = other.candidate_triangles_inside_box(
        points_minimum, points_maximum, epsilon_metres
    )
    if len(candidate_triangles) == 0:
        return False, True
    if len(points) * len(candidate_triangles) > (
        NARROW_PHASE_POINT_TRIANGLE_BUDGET
    ):
        return False, False
    # One batched call over every candidate triangle: the budget above
    # caps the (point, triangle) broadcast at ~400k pairs, so the whole
    # test runs in C instead of a Python loop per triangle (the loop was
    # the cold-build hot spot, 2026-07-15 profile).
    # errstate: a degenerate edge divides 0/0 inside; the function
    # sanitises the resulting not-a-number to 0.0 (contact), so the
    # warning is noise.
    with numpy.errstate(invalid="ignore", divide="ignore"):
        distances = _point_triangle_minimum_distances(
            points,
            other.corner_a[candidate_triangles],
            other.corner_b[candidate_triangles],
            other.corner_c[candidate_triangles],
        )
    return bool(distances.min() <= epsilon_metres), True


def _surfaces_in_contact(
    first: _PartGeometry,
    second: _PartGeometry,
    epsilon_metres: float,
) -> bool:
    """Narrow phase for one broad-phase pair.  True unless the pair is
    PROVED apart (invariant I-20)."""
    first_candidates = first.points_inside_box(
        second.box_minimum, second.box_maximum, epsilon_metres
    )
    second_candidates = second.points_inside_box(
        first.box_minimum, first.box_maximum, epsilon_metres
    )

    # Quick accept: any vertex-vertex pair within epsilon is a contact.
    if len(first_candidates) and len(second_candidates):
        nearest, _ = second.vertex_tree.query(
            first_candidates, k=1, distance_upper_bound=epsilon_metres
        )
        if numpy.isfinite(nearest).any():
            return True

    contact, proved = _vertex_to_triangle_proof(
        first_candidates, second, epsilon_metres
    )
    if contact:
        return True
    contact_reverse, proved_reverse = _vertex_to_triangle_proof(
        second_candidates, first, epsilon_metres
    )
    if contact_reverse:
        return True
    if proved and proved_reverse:
        return False
    # Budget exhausted in at least one direction: cannot prove the pair
    # apart, so the edge stays (merge on doubt).
    return True


def _broad_phase_pairs(
    part_geometries: list[_PartGeometry],
    epsilon_metres: float,
) -> set[tuple[int, int]]:
    """All part pairs whose 3D axis-aligned bounding boxes come within
    ``epsilon_metres`` on every axis — a superset of true surface contact
    (box gap never exceeds surface gap), so merging on it alone is sound;
    the narrow phase merely prunes."""
    cell_size = max(BROAD_PHASE_GRID_CELL_METRES, 2.0 * epsilon_metres)
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for part_index, geometry in enumerate(part_geometries):
        first_cell_x = int(
            (geometry.box_minimum[0] - epsilon_metres) // cell_size
        )
        last_cell_x = int(
            (geometry.box_maximum[0] + epsilon_metres) // cell_size
        )
        first_cell_z = int(
            (geometry.box_minimum[2] - epsilon_metres) // cell_size
        )
        last_cell_z = int(
            (geometry.box_maximum[2] + epsilon_metres) // cell_size
        )
        for cell_x in range(first_cell_x, last_cell_x + 1):
            for cell_z in range(first_cell_z, last_cell_z + 1):
                buckets[(cell_x, cell_z)].append(part_index)

    pairs: set[tuple[int, int]] = set()
    for bucket in buckets.values():
        for position, left in enumerate(bucket):
            left_geometry = part_geometries[left]
            for right in bucket[position + 1 :]:
                key = (left, right) if left < right else (right, left)
                if key in pairs:
                    continue
                right_geometry = part_geometries[right]
                if (
                    (
                        left_geometry.box_minimum - epsilon_metres
                        <= right_geometry.box_maximum
                    ).all()
                    and (
                        right_geometry.box_minimum - epsilon_metres
                        <= left_geometry.box_maximum
                    ).all()
                ):
                    pairs.add(key)
    return pairs


def contact_graph(
    vertices: list[tuple[float, float, float]],
    parts: list[list[Triangle]],
    epsilon_metres: float,
) -> set[tuple[int, int]]:
    """Return a CONNECTIVITY-EQUIVALENT set of in-contact part-index
    pairs: every returned edge is a true surface contact within
    ``epsilon_metres``, and the connected components equal those of the
    full contact graph — but pairs already joined transitively by
    earlier contacts are skipped untested, so the set is a spanning
    subset, not every in-contact pair.  The sole consumer
    (``connected_structures``) reads only connectivity; skipping
    redundant pairs cut the dominant cold-build cost (2026-07-15
    profile: dense terminal clusters have near-quadratic in-contact
    pairs, of which a spanning handful suffices).

    Broad phase: 3D axis-aligned bounding-box gap over a uniform grid
    (sound — box gap never exceeds surface gap).  Narrow phase:
    vertex-to-triangle surface distance in both directions with early
    exit at epsilon, pruning the broad-phase superset.  Any pair the
    narrow phase cannot prove apart (budget exhaustion, degenerate
    triangles, numerical doubt) KEEPS its edge (invariant I-20 — the
    skip only ever removes REDUNDANT tests, never a component-joining
    one, so I-20's merge-on-doubt reach is unchanged).

    Uses the 3D box, not the prototype's 2D box: the 2D box merges a
    jetbridge at y = 6 m with the shed beneath it.
    """
    vertex_array = numpy.asarray(vertices, dtype=numpy.float64)
    part_geometries = [_PartGeometry(vertex_array, part) for part in parts]
    edges: set[tuple[int, int]] = set()

    parent = list(range(len(part_geometries)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in _broad_phase_pairs(part_geometries, epsilon_metres):
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            continue  # already one structure — the edge would be redundant
        if _surfaces_in_contact(
            part_geometries[left], part_geometries[right], epsilon_metres
        ):
            edges.add((left, right))
            parent[left_root] = right_root
    return edges


def connected_structures(
    part_count: int,
    contact_edges: set[tuple[int, int]],
) -> list[list[int]]:
    """Connected components of the contact graph: lists of part indices,
    one list per structure.  This IS the optimal partition (partition
    document, section 2.2) — there is no gap parameter to tune, and no
    post-hoc merging or splitting belongs here (large-ground-span
    handling is bake-and-flag in workstream W4, amendment A3, never a
    split)."""
    parent = list(range(part_count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in contact_edges:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    grouped: dict[int, list[int]] = defaultdict(list)
    for part_index in range(part_count):
        grouped[find(part_index)].append(part_index)
    structures = [sorted(members) for members in grouped.values()]
    structures.sort(key=lambda members: members[0])
    return structures
