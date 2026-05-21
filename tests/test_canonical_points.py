"""Property-based tests for auto_patch.canonical_points.CanonicalPointRegistry.

The registry's contract (canonical_points.py docstring): every corner
near (x, y) resolves to ONE canonical point — the first registered
within ``tol_m``, returned at exact equality so adjacent shapes share
coordinates.  These properties check that contract holds for arbitrary
insertion sequences.
"""
from __future__ import annotations

import math

from hypothesis import given, settings
from hypothesis import strategies as st

from strategies import (
    merge_tol, point, point_and_near_point, point_sequence,
    well_separated_points,
)

from shapely.geometry import Polygon

from auto_patch.canonical_points import (
    CanonicalPointRegistry,
    snap_polygon_through_registry,
)


class TestGetOrAdd:
    """Properties of CanonicalPointRegistry.get_or_add."""

    @given(p=point, tol=merge_tol)
    def test_self_return_is_idempotent(self, p, tol):
        # Querying the same point twice returns the identical canonical
        # point, and re-querying with the returned point is a fixed point.
        r = CanonicalPointRegistry(tol_m=tol)
        first = r.get_or_add(*p)
        assert r.get_or_add(*p) == first
        assert r.get_or_add(*first) == first

    @given(seq=point_sequence, tol=merge_tol)
    def test_result_within_tol_of_input(self, seq, tol):
        # Every returned canonical point is within tol of the query
        # (it's either the query itself, or a prior entry < tol away).
        r = CanonicalPointRegistry(tol_m=tol)
        for p in seq:
            cp = r.get_or_add(*p)
            assert math.hypot(cp[0] - p[0], cp[1] - p[1]) <= tol + 1e-9

    @given(seq=point_sequence, tol=merge_tol)
    def test_result_is_registered(self, seq, tol):
        # Whatever get_or_add returns is an actual registry entry.
        r = CanonicalPointRegistry(tol_m=tol)
        for p in seq:
            cp = r.get_or_add(*p)
            assert cp in r.points()

    @given(near=point_and_near_point())
    def test_points_within_tol_merge(self, near):
        # A second point strictly within tol of the first resolves to
        # the first (the sole prior entry) — they share one canonical.
        tol, base, second = near
        r = CanonicalPointRegistry(tol_m=tol)
        canon = r.get_or_add(*base)
        assert r.get_or_add(*second) == canon
        assert r.size == 1

    @given(sep=well_separated_points())
    def test_separated_points_stay_distinct(self, sep):
        # Points that are all pairwise ≥ 3·tol apart never merge: the
        # registry keeps exactly one entry per input point.
        tol, pts = sep
        r = CanonicalPointRegistry(tol_m=tol)
        for p in pts:
            r.get_or_add(*p)
        assert r.size == len(pts)

    @given(seq=point_sequence, tol=merge_tol)
    def test_size_never_exceeds_calls(self, seq, tol):
        # Each call adds at most one entry; size is bounded by call count
        # and is monotonic non-decreasing.
        r = CanonicalPointRegistry(tol_m=tol)
        prev = 0
        for p in seq:
            r.get_or_add(*p)
            assert prev <= r.size <= prev + 1
            prev = r.size
        assert r.size <= len(seq)

    @given(seq=point_sequence, tol=merge_tol)
    def test_deterministic_replay(self, seq, tol):
        # The same insertion sequence on two fresh registries yields the
        # identical canonical-point set in the identical order.
        r1 = CanonicalPointRegistry(tol_m=tol)
        r2 = CanonicalPointRegistry(tol_m=tol)
        out1 = [r1.get_or_add(*p) for p in seq]
        out2 = [r2.get_or_add(*p) for p in seq]
        assert out1 == out2
        assert r1.points() == r2.points()


class TestSeed:
    """Properties of CanonicalPointRegistry.seed."""

    @given(sep=well_separated_points())
    def test_seed_separated_adds_all(self, sep):
        # Seeding well-separated anchors registers each as a new point;
        # the reported count equals the number seeded.
        tol, pts = sep
        r = CanonicalPointRegistry(tol_m=tol)
        added = r.seed(pts)
        assert added == len(pts)
        assert r.size == len(pts)

    @given(near=point_and_near_point())
    def test_seed_collapses_duplicates(self, near):
        # Seeding two within-tol points collapses them to one entry.
        tol, base, second = near
        r = CanonicalPointRegistry(tol_m=tol)
        added = r.seed([base, second])
        assert added == 1
        assert r.size == 1

    def test_seed_into_non_empty_returns_only_new_count(self):
        # seed reports NEW points added, not the total — a second seed
        # batch with one duplicate (near a prior entry) and one fresh
        # point must report 1, with size growing by exactly 1.
        r = CanonicalPointRegistry(tol_m=0.5)
        r.seed([(0.0, 0.0), (100.0, 100.0)])
        added = r.seed([(0.1, 0.0), (200.0, 200.0)])
        assert added == 1
        assert r.size == 3


class TestFindNearest:
    """Properties of CanonicalPointRegistry.find_nearest (no-add lookup)."""

    def test_returns_nearest_within_max_d(self):
        r = CanonicalPointRegistry(tol_m=0.5)
        r.seed([(0.0, 0.0), (10.0, 10.0)])
        assert r.find_nearest(0.2, 0.1, 0.5) == (0.0, 0.0)

    def test_returns_none_beyond_max_d(self):
        r = CanonicalPointRegistry(tol_m=0.5)
        r.seed([(0.0, 0.0), (10.0, 10.0)])
        assert r.find_nearest(5.0, 5.0, 1.0) is None

    def test_does_not_add(self):
        r = CanonicalPointRegistry(tol_m=0.5)
        r.seed([(0.0, 0.0)])
        r.find_nearest(0.2, 0.1, 0.5)
        r.find_nearest(50.0, 50.0, 0.5)
        assert r.size == 1

    def test_finds_point_several_cells_away(self):
        # A match well beyond one cell width (max_d > tol) must still be
        # found — exercises the multi-cell scan radius.
        r = CanonicalPointRegistry(tol_m=0.5)
        r.seed([(0.0, 0.0)])
        assert r.find_nearest(1.8, 0.0, 2.0) == (0.0, 0.0)


class TestSnapPolygonThroughRegistry:
    """``snap_polygon_through_registry`` routes ring vertices through the
    registry so neighbouring shapes share exact coordinates."""

    def test_vertices_snap_to_seeded_canonical_points(self):
        r = CanonicalPointRegistry(tol_m=0.5)
        r.seed([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])
        # Slightly-off vertices (each within tol of a seeded corner).
        poly = Polygon([(0.1, 0.1), (10.1, -0.1),
                        (9.9, 10.1), (-0.1, 9.9)])
        snapped = snap_polygon_through_registry(poly, r)
        ring = set(list(snapped.exterior.coords)[:-1])
        assert ring == {(0.0, 0.0), (10.0, 0.0),
                        (10.0, 10.0), (0.0, 10.0)}
        # No new canonical points created — all resolved to seeds.
        assert r.size == 4

    def test_adjacent_polygons_share_canonical_corner(self):
        # Two polygons whose near-corners are within tol must end up with
        # the IDENTICAL coordinate there (the registry's whole purpose).
        r = CanonicalPointRegistry(tol_m=0.5)
        poly_a = Polygon([(0.0, 0.0), (5.0, 0.0), (5.0, 5.0), (0.0, 5.0)])
        poly_b = Polygon([(5.2, 5.1), (10.0, 5.0),
                          (10.0, 10.0), (5.0, 10.0)])
        snap_polygon_through_registry(poly_a, r)   # registers (5.0, 5.0)
        snapped_b = snap_polygon_through_registry(poly_b, r)
        b_ring = set(list(snapped_b.exterior.coords)[:-1])
        assert (5.0, 5.0) in b_ring
        assert (5.2, 5.1) not in b_ring

    def test_degenerate_ring_returns_none(self):
        # All three corners collapse to one canonical point → < 3 unique
        # vertices → the snap yields a degenerate ring → None.
        r = CanonicalPointRegistry(tol_m=0.5)
        poly = Polygon([(0.0, 0.0), (0.1, 0.0), (0.0, 0.1)])
        assert snap_polygon_through_registry(poly, r) is None

    def test_none_inputs_pass_through(self):
        r = CanonicalPointRegistry(tol_m=0.5)
        assert snap_polygon_through_registry(None, r) is None
        poly = Polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)])
        # No registry → returned unchanged.
        assert snap_polygon_through_registry(poly, None) is poly
