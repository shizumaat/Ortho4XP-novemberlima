"""Vectorized RUNWAY-BOUNDARY freeze scan in ``final_grade_projection``
(build-time track T1c, 2026-07-18).

The late pipeline-end projection freezes every node lying on a runway
boundary edge interior (see the WHY comment on
``_runway_boundary_freeze_indexes``).  The scan used to loop over all
131k nodes at OTHH-class airports, building a ``shapely.geometry.Point``
and calling ``prep(zone).contains(point)`` per node.  It is now one
C-vectorized ``shapely.contains_xy`` call over the node coordinates.

These hermetic tests assert the vectorized helper returns EXACTLY the
index set a scalar prepared-``contains`` reference produces on the same
zone geometry — no fixtures, no DEM, no network.  ``-n0`` in well under
a second.
"""
from shapely.geometry import LinearRing, Point
from shapely.ops import unary_union
from shapely.prepared import prep

from auto_patch.elevation_per_surface.route_profile.solve import (
    _runway_boundary_freeze_indexes)


def _scalar_reference(nodes, node_count, already_hard,
                      runway_boundary_lines, freeze_tolerance_m):
    """The pre-vectorization scalar scan, verbatim in intent: prepared
    ``contains`` per candidate node, skipping already-hard indexes and
    indexes ``>= node_count``."""
    if not runway_boundary_lines:
        return set()
    zone = prep(unary_union(runway_boundary_lines).buffer(freeze_tolerance_m))
    result = set()
    for index, (x, y) in enumerate(nodes):
        if index in already_hard or index >= node_count:
            continue
        if zone.contains(Point(x, y)):
            result.add(index)
    return result


def _rectangle_ring(x0, y0, x1, y1):
    """Closed rectangular runway ring (exterior LinearRing)."""
    return LinearRing([(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)])


def test_vectorized_matches_scalar_reference_on_grid():
    tol = 0.5
    # Two rectangular "runways": one crossing the other, so the union
    # boundary has interior junction geometry (the case the freeze
    # protects).
    rings = [
        _rectangle_ring(0.0, 0.0, 40.0, 8.0),
        _rectangle_ring(15.0, -20.0, 23.0, 28.0),
    ]

    # A grid of candidate nodes: some sit exactly on a boundary edge
    # (inside the tol buffer), some well inside a rectangle interior
    # (NOT on the boundary → outside the thin buffered zone), some far
    # outside everything.
    nodes = []
    for gx in range(-10, 50, 3):
        for gy in range(-30, 40, 3):
            nodes.append((float(gx), float(gy)))
    # Force a handful of nodes to land right on boundary edges.
    nodes.append((20.0, 0.0))     # on the horizontal runway's bottom edge
    nodes.append((40.0, 4.0))     # on the horizontal runway's right edge
    nodes.append((15.0, 10.0))    # on the vertical runway's left edge
    nodes.append((19.0, 28.0))    # on the vertical runway's top edge
    nodes.append((-100.0, -100.0))  # far outside

    n = len(nodes)
    already_hard = set()

    expected = _scalar_reference(nodes, n, already_hard, rings, tol)
    actual = _runway_boundary_freeze_indexes(nodes, n, already_hard, rings, tol)

    assert actual == expected
    # Sanity: the scene is designed to have boundary-hits AND misses.
    assert 0 < len(expected) < n


def test_already_hard_indexes_are_excluded():
    tol = 0.5
    rings = [_rectangle_ring(0.0, 0.0, 30.0, 6.0)]
    nodes = [(0.0, 0.0), (10.0, 0.0), (30.0, 3.0), (15.0, 3.0), (99.0, 99.0)]
    n = len(nodes)

    # Without any pre-hard nodes, the on-boundary vertices are picked up.
    baseline = _runway_boundary_freeze_indexes(nodes, n, set(), rings, tol)
    assert baseline  # at least one boundary hit exists

    # Pre-marking one of the hit indexes must drop exactly that index.
    victim = next(iter(baseline))
    already_hard = {victim}
    expected = _scalar_reference(nodes, n, already_hard, rings, tol)
    actual = _runway_boundary_freeze_indexes(nodes, n, already_hard, rings, tol)
    assert actual == expected
    assert victim not in actual


def test_index_at_or_above_node_count_is_guarded():
    tol = 0.5
    rings = [_rectangle_ring(0.0, 0.0, 30.0, 6.0)]
    # Node 0 is on the boundary; nodes 3 and 4 also sit on the boundary
    # but live at indexes >= node_count and must be ignored.
    nodes = [(0.0, 0.0), (15.0, 3.0), (99.0, 99.0), (30.0, 3.0), (0.0, 6.0)]
    node_count = 3  # only indexes 0, 1, 2 are real solver variables
    already_hard = set()

    expected = _scalar_reference(
        nodes, node_count, already_hard, rings, tol)
    actual = _runway_boundary_freeze_indexes(
        nodes, node_count, already_hard, rings, tol)
    assert actual == expected
    assert all(index < node_count for index in actual)
    assert 3 not in actual and 4 not in actual


def test_no_runway_lines_returns_empty_set():
    nodes = [(0.0, 0.0), (1.0, 1.0)]
    assert _runway_boundary_freeze_indexes(nodes, 2, set(), [], 0.5) == set()


def test_empty_node_list_returns_empty_set():
    rings = [_rectangle_ring(0.0, 0.0, 30.0, 6.0)]
    assert _runway_boundary_freeze_indexes([], 0, set(), rings, 0.5) == set()
