"""Workstream W2 acceptance tests for ``auto_patch.obj8_partition``.

The theory is ``docs/obj8_structure_partition.md``: structures are the
connected components of the epsilon-contact graph (invariant I-6), the
narrow phase prunes 3D bounding-box over-merges, and a pair that cannot
be proved apart keeps its edge (invariant I-20).  The trap test — a wall
abutting a roof WITHOUT shared vertices is ONE structure — encodes the
partition document's headline finding: vertex connectivity is not
contact (4,453 torn abutments at KCLT).
"""

from __future__ import annotations

import pytest

from auto_patch import obj8_partition
from auto_patch.obj8_partition import (
    connected_structures,
    contact_graph,
    weld_parts,
)


def box_geometry(
    minimum_x: float,
    minimum_y: float,
    minimum_z: float,
    size: float,
    vertices: list[tuple[float, float, float]],
) -> list[tuple[int, int, int]]:
    """Append one closed box to ``vertices`` and return its triangles."""
    base = len(vertices)
    for bit_z in (0, 1):
        for bit_y in (0, 1):
            for bit_x in (0, 1):
                vertices.append(
                    (
                        minimum_x + bit_x * size,
                        minimum_y + bit_y * size,
                        minimum_z + bit_z * size,
                    )
                )
    corner_triangles = [
        (0, 1, 3), (0, 3, 2),
        (4, 7, 5), (4, 6, 7),
        (0, 5, 1), (0, 4, 5),
        (2, 3, 7), (2, 7, 6),
        (0, 2, 6), (0, 6, 4),
        (1, 5, 7), (1, 7, 3),
    ]
    return [tuple(base + index for index in triangle) for triangle in corner_triangles]


def quad_triangles(
    corner_a: tuple[float, float, float],
    corner_b: tuple[float, float, float],
    corner_c: tuple[float, float, float],
    corner_d: tuple[float, float, float],
    vertices: list[tuple[float, float, float]],
) -> list[tuple[int, int, int]]:
    """Append one quad (two triangles, four fresh vertices)."""
    base = len(vertices)
    vertices.extend([corner_a, corner_b, corner_c, corner_d])
    return [(base, base + 1, base + 2), (base, base + 2, base + 3)]


def structures_of(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int]],
    epsilon_metres: float,
) -> list[list[int]]:
    parts = weld_parts(vertices, triangles)
    edges = contact_graph(vertices, parts, epsilon_metres)
    return connected_structures(len(parts), edges)


# ---------------------------------------------------------------------------
# weld_parts — level 1 of the hierarchy
# ---------------------------------------------------------------------------

def test_weld_parts_merges_duplicated_seam_positions():
    """Exporters duplicate a position once per texture seam; without the
    positional weld one wall shatters into two parts."""
    vertices: list[tuple[float, float, float]] = []
    # Two triangles forming one quad, sharing an edge only by POSITION:
    # six vertex entries, four unique positions.
    left = quad_triangles(
        (0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (4.0, 3.0, 0.0), (0.0, 3.0, 0.0),
        vertices,
    )[:1]
    right_base = len(vertices)
    vertices.extend(
        [(4.0, 0.0, 0.0), (4.0, 3.0, 0.0), (0.0, 3.0, 0.0)]
    )
    right = [(right_base, right_base + 1, right_base + 2)]
    parts = weld_parts(vertices, left + right)
    assert len(parts) == 1
    assert sorted(len(part) for part in parts) == [2]


def test_weld_parts_keeps_disjoint_boxes_apart():
    vertices: list[tuple[float, float, float]] = []
    first = box_geometry(0.0, 0.0, 0.0, 10.0, vertices)
    second = box_geometry(13.0, 0.0, 0.0, 10.0, vertices)
    parts = weld_parts(vertices, first + second)
    assert len(parts) == 2
    assert sorted(len(part) for part in parts) == [12, 12]


# ---------------------------------------------------------------------------
# contact graph — the trap test and the separations
# ---------------------------------------------------------------------------

def test_wall_abutting_roof_without_shared_vertices_is_one_structure():
    """THE trap (partition document section 1): triangle soup abuts
    without sharing vertices.  The wall's top edge sits 0.1 m beneath the
    roof plane, with no coincident vertex positions — vertex
    connectivity says two parts, contact says one structure."""
    vertices: list[tuple[float, float, float]] = []
    # Vertical wall: x 0..4, y 0..3, z = 0.
    wall = quad_triangles(
        (0.0, 0.0, 0.0), (4.0, 0.0, 0.0), (4.0, 3.0, 0.0), (0.0, 3.0, 0.0),
        vertices,
    )
    # Horizontal roof slab 0.1 m above the wall top, overhanging it on
    # every side so the wall's top vertices project into the roof's
    # interior.  No vertex is shared, no positions coincide.
    roof = quad_triangles(
        (-1.0, 3.1, -1.0), (5.0, 3.1, -1.0), (5.0, 3.1, 1.0), (-1.0, 3.1, 1.0),
        vertices,
    )
    parts = weld_parts(vertices, wall + roof)
    assert len(parts) == 2  # vertex connectivity alone would tear here
    structures = structures_of(vertices, wall + roof, epsilon_metres=0.25)
    assert len(structures) == 1


def test_two_boxes_three_metres_apart_are_two_structures():
    vertices: list[tuple[float, float, float]] = []
    triangles = box_geometry(0.0, 0.0, 0.0, 10.0, vertices)
    triangles += box_geometry(13.0, 0.0, 0.0, 10.0, vertices)
    structures = structures_of(vertices, triangles, epsilon_metres=0.25)
    assert len(structures) == 2


def test_two_boxes_within_epsilon_merge():
    vertices: list[tuple[float, float, float]] = []
    triangles = box_geometry(0.0, 0.0, 0.0, 10.0, vertices)
    triangles += box_geometry(10.2, 0.0, 0.0, 10.0, vertices)
    structures = structures_of(vertices, triangles, epsilon_metres=0.25)
    assert len(structures) == 1


def test_jetbridge_above_shed_stays_separate_in_three_dimensions():
    """The 2D bounding box (the prototype's) merges a slab at y = 6 m
    with the shed beneath it; the 3D box must not."""
    vertices: list[tuple[float, float, float]] = []
    shed = box_geometry(0.0, 0.0, 0.0, 4.0, vertices)
    elevated_slab = quad_triangles(
        (0.0, 6.0, 0.0), (4.0, 6.0, 0.0), (4.0, 6.0, 4.0), (0.0, 6.0, 4.0),
        vertices,
    )
    structures = structures_of(
        vertices, shed + elevated_slab, epsilon_metres=0.25
    )
    assert len(structures) == 2


def test_interlocking_parts_with_overlapping_boxes_stay_separate():
    """Narrow-phase acceptance: two parts whose 3D bounding boxes overlap
    while their surfaces stay well apart (an interlocking L arrangement)
    must remain separate structures — the broad phase alone would fuse
    them."""
    vertices: list[tuple[float, float, float]] = []
    # An L: one arm along x (z 0..1), one arm along z (x 0..1).
    arm_along_x = box_geometry(0.0, 0.0, 0.0, 1.0, vertices)
    arm_along_x += box_geometry(1.0, 0.0, 0.0, 1.0, vertices)
    arm_along_x += box_geometry(2.0, 0.0, 0.0, 1.0, vertices)
    arm_along_z = box_geometry(0.0, 0.0, 1.0, 1.0, vertices)
    arm_along_z += box_geometry(0.0, 0.0, 2.0, 1.0, vertices)
    l_shape = arm_along_x + arm_along_z
    # A box nested in the L's notch corner: its 3D bounding box overlaps
    # the L's (which spans x 0..3, z 0..3), but its surfaces stay 1 m
    # from both arms.
    nested_box = box_geometry(2.0, 0.0, 2.0, 1.0, vertices)

    parts = weld_parts(vertices, l_shape + nested_box)
    assert len(parts) == 2

    # The L's bounding box contains the nested box's entirely.
    edges = contact_graph(vertices, parts, epsilon_metres=0.25)
    assert edges == set()
    structures = connected_structures(len(parts), edges)
    assert len(structures) == 2


def _diagonal_triangle_and_distant_speck() -> tuple[
    list[tuple[float, float, float]], list[tuple[int, int, int]]
]:
    """A large diagonal triangle plus a small triangle floating deep
    inside its bounding box but ~2 m from its surface.  The box
    restriction cannot empty the candidate sets here (the speck sits
    inside the big triangle's inflated bounding box), so the narrow
    phase must actually spend point-times-triangle work to prove the
    pair apart."""
    vertices: list[tuple[float, float, float]] = [
        (0.0, 0.0, 0.0),
        (4.0, 4.0, 0.0),
        (4.0, 0.0, 4.0),
        (3.5, 3.5, 3.5),
        (3.6, 3.5, 3.5),
        (3.5, 3.6, 3.5),
    ]
    triangles = [(0, 1, 2), (3, 4, 5)]
    return vertices, triangles


def test_distant_speck_inside_bounding_box_is_proved_apart():
    vertices, triangles = _diagonal_triangle_and_distant_speck()
    structures = structures_of(vertices, triangles, epsilon_metres=0.25)
    assert len(structures) == 2


def test_budget_exhaustion_keeps_the_edge(monkeypatch):
    """Invariant I-20: a pair the narrow phase cannot afford to test is
    kept, never pruned — over-merging costs centimetres, tearing is
    unrecoverable.  With the budget forced to zero, the same pair the
    previous test proved apart must now merge on doubt."""
    monkeypatch.setattr(
        obj8_partition, "NARROW_PHASE_POINT_TRIANGLE_BUDGET", 0
    )
    vertices, triangles = _diagonal_triangle_and_distant_speck()
    structures = structures_of(vertices, triangles, epsilon_metres=0.25)
    assert len(structures) == 1


# ---------------------------------------------------------------------------
# connected_structures
# ---------------------------------------------------------------------------

def test_connected_structures_groups_and_isolates():
    structures = connected_structures(5, {(0, 1), (2, 3)})
    assert structures == [[0, 1], [2, 3], [4]]


def test_connected_structures_transitive_chain():
    structures = connected_structures(4, {(0, 1), (1, 2), (2, 3)})
    assert structures == [[0, 1, 2, 3]]


def test_connected_structures_no_edges():
    assert connected_structures(3, set()) == [[0], [1], [2]]


def test_degenerate_edge_not_a_number_is_contact_not_separation():
    """Found live at HECA: a triangle with a duplicated corner makes the
    edge branches of the point-triangle test divide 0/0, and the
    resulting not-a-number used to poison ``ndarray.min()`` for the whole
    pair — flipping genuine contact to "proved apart" (the tear
    invariant I-20 forbids).  A degenerate sliver whose spine passes
    through a neighbouring part's vertex must merge with it."""
    from auto_patch.obj8_partition import contact_graph, weld_parts

    vertices = [
        # Part 1: degenerate sliver — corners 0 and 1 are the SAME
        # position, so edge_ab has zero length.
        (0.0, 0.0, 0.0),
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
        # Part 2: a real triangle with one vertex exactly on the
        # sliver's spine midpoint (true distance 0).
        (1.0, 0.0, 0.0),
        (1.0, 5.0, 0.0),
        (1.0, 0.0, 5.0),
    ]
    triangles = [(0, 1, 2), (3, 4, 5)]
    parts = weld_parts(vertices, triangles)
    assert len(parts) == 2
    edges = contact_graph(vertices, parts, epsilon_metres=0.25)
    assert edges, (
        "degenerate-edge not-a-number separated two parts in true contact"
    )
