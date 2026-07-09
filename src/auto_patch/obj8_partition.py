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
    """Minimum distance from each point to one triangle, vectorised over
    the points (the standard Voronoi-region point-triangle test).

    A degenerate (zero-area) triangle takes the final plane-projection
    branch with distance 0.0 — deliberately erring toward CONTACT, per
    invariant I-20 (numerical doubt merges, never tears).
    """
    edge_ab = triangle_corner_b - triangle_corner_a
    edge_ac = triangle_corner_c - triangle_corner_a
    from_a = points - triangle_corner_a
    dot_1 = from_a @ edge_ab
    dot_2 = from_a @ edge_ac
    from_b = points - triangle_corner_b
    dot_3 = from_b @ edge_ab
    dot_4 = from_b @ edge_ac
    from_c = points - triangle_corner_c
    dot_5 = from_c @ edge_ab
    dot_6 = from_c @ edge_ac
    distances = numpy.full(len(points), numpy.inf)

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
                from_a[on_edge_ab] - parameter * edge_ab, axis=1
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
                from_a[on_edge_ac] - parameter * edge_ac, axis=1
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
                points[on_edge_bc]
                - (triangle_corner_b + parameter * (triangle_corner_c - triangle_corner_b)),
                axis=1,
            )
        interior = remaining & numpy.isinf(distances)
        if interior.any():
            normal = numpy.cross(edge_ab, edge_ac)
            normal_length = numpy.linalg.norm(normal)
            distances[interior] = (
                numpy.abs(from_a[interior] @ normal) / normal_length
                if normal_length > 1e-12
                else 0.0
            )
    # Degenerate-edge guard: 0/0 in the edge branches yields not-a-number,
    # which would otherwise poison ndarray.min() for the whole pair and
    # flip contact to proved-apart.  Numerical doubt is CONTACT (I-20).
    non_finite = ~numpy.isfinite(distances)
    if non_finite.any():
        distances[non_finite] = 0.0
    return distances


class _PartGeometry:
    """Per-part cached arrays for the contact graph: vertex positions,
    triangle corner arrays, 3D axis-aligned bounding boxes (one per part
    and one per triangle), and a lazily built vertex k-d tree."""

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

    @property
    def vertex_tree(self) -> cKDTree:
        if self._vertex_tree is None:
            self._vertex_tree = cKDTree(self.points)
        return self._vertex_tree


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
    candidate_triangles = numpy.nonzero(
        (
            (other.triangle_minimum <= points_maximum + epsilon_metres)
            & (other.triangle_maximum >= points_minimum - epsilon_metres)
        ).all(axis=1)
    )[0]
    if len(candidate_triangles) == 0:
        return False, True
    if len(points) * len(candidate_triangles) > (
        NARROW_PHASE_POINT_TRIANGLE_BUDGET
    ):
        return False, False
    for triangle_index in candidate_triangles:
        # errstate: a degenerate edge divides 0/0 inside; the function
        # sanitises the resulting not-a-number to 0.0 (contact), so the
        # warning is noise.
        with numpy.errstate(invalid="ignore", divide="ignore"):
            distances = _point_triangle_minimum_distances(
                points,
                other.corner_a[triangle_index],
                other.corner_b[triangle_index],
                other.corner_c[triangle_index],
            )
        if distances.min() <= epsilon_metres:
            return True, True
    return False, True


def _surfaces_in_contact(
    first: _PartGeometry,
    second: _PartGeometry,
    epsilon_metres: float,
) -> bool:
    """Narrow phase for one broad-phase pair.  True unless the pair is
    PROVED apart (invariant I-20)."""
    first_candidates = _points_inside_box(
        first.points, second.box_minimum, second.box_maximum, epsilon_metres
    )
    second_candidates = _points_inside_box(
        second.points, first.box_minimum, first.box_maximum, epsilon_metres
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
    """Return the set of part-index pairs whose surfaces come within
    ``epsilon_metres`` of each other.

    Broad phase: 3D axis-aligned bounding-box gap over a uniform grid
    (sound — box gap never exceeds surface gap).  Narrow phase:
    vertex-to-triangle surface distance in both directions with early
    exit at epsilon, pruning the broad-phase superset.  Any pair the
    narrow phase cannot prove apart (budget exhaustion, degenerate
    triangles, numerical doubt) KEEPS its edge (invariant I-20).

    Uses the 3D box, not the prototype's 2D box: the 2D box merges a
    jetbridge at y = 6 m with the shed beneath it.
    """
    vertex_array = numpy.asarray(vertices, dtype=numpy.float64)
    part_geometries = [_PartGeometry(vertex_array, part) for part in parts]
    edges: set[tuple[int, int]] = set()
    for left, right in _broad_phase_pairs(part_geometries, epsilon_metres):
        if _surfaces_in_contact(
            part_geometries[left], part_geometries[right], epsilon_metres
        ):
            edges.add((left, right))
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
