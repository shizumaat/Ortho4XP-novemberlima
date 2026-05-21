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

from auto_patch.canonical_points import CanonicalPointRegistry


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
