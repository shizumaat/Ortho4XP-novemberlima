"""Property-based tests for runway_redistribute._interp_profile.

``_interp_profile(fractions, elevs, t)`` linearly interpolates ``elevs``
over a sorted ``fractions`` grid, clamping to the endpoints outside the
range.  Properties verify that contract: bounded output, exact at the
endpoints and at every node, and convex between adjacent nodes.
"""
from __future__ import annotations

import math

from hypothesis import given

from strategies import profile, profile_t

from auto_patch.runway_redistribute import (
    _insert_seam_anchors,
    _interp_profile,
)


class TestInterpProfile:
    """Properties of _interp_profile."""

    @given(prof=profile(), t=profile_t)
    def test_result_within_elev_range(self, prof, t):
        # Interpolating/clamping can never escape the elevation range.
        fractions, elevs = prof
        v = _interp_profile(fractions, elevs, t)
        assert min(elevs) - 1e-6 <= v <= max(elevs) + 1e-6

    @given(prof=profile())
    def test_clamps_below_first_node(self, prof):
        # Any t at or below the first fraction returns the first elev.
        fractions, elevs = prof
        assert _interp_profile(fractions, elevs, fractions[0]) == elevs[0]
        assert _interp_profile(
            fractions, elevs, fractions[0] - 5.0) == elevs[0]

    @given(prof=profile())
    def test_clamps_above_last_node(self, prof):
        # Any t at or above the last fraction returns the last elev.
        fractions, elevs = prof
        assert _interp_profile(fractions, elevs, fractions[-1]) == elevs[-1]
        assert _interp_profile(
            fractions, elevs, fractions[-1] + 5.0) == elevs[-1]

    @given(prof=profile())
    def test_exact_at_every_node(self, prof):
        # Evaluating exactly at a node returns that node's elevation.
        fractions, elevs = prof
        for f, e in zip(fractions, elevs):
            assert math.isclose(
                _interp_profile(fractions, elevs, f), e,
                rel_tol=1e-9, abs_tol=1e-6)

    @given(prof=profile())
    def test_convex_between_adjacent_nodes(self, prof):
        # Midway between two nodes, the value lies between their elevs.
        fractions, elevs = prof
        for i in range(len(fractions) - 1):
            t_mid = 0.5 * (fractions[i] + fractions[i + 1])
            v = _interp_profile(fractions, elevs, t_mid)
            lo, hi = sorted((elevs[i], elevs[i + 1]))
            assert lo - 1e-6 <= v <= hi + 1e-6


class TestInsertSeamAnchors:
    """``_insert_seam_anchors`` folds seam (t, elev) samples into the
    parallel fractions/elevs/anchored arrays.  A seam in (0, 1) is
    inserted (or overrides a coincident sample) as an ANCHORED point;
    seams outside [0, 1] are ignored.
    """

    def test_inserts_in_sorted_position(self):
        fractions = [0.0, 0.5, 1.0]
        elevs = [10.0, 12.0, 14.0]
        anchored = [True, False, True]
        _insert_seam_anchors(fractions, elevs, anchored, [(0.25, 11.5)])
        assert fractions == [0.0, 0.25, 0.5, 1.0]
        assert elevs == [10.0, 11.5, 12.0, 14.0]
        assert anchored == [True, True, False, True]

    def test_override_coincident_sample(self):
        # A seam coinciding (within 1e-3) with an existing sample takes
        # it over: no new entry, anchored set, elevation replaced.
        fractions = [0.0, 0.5, 1.0]
        elevs = [10.0, 12.0, 14.0]
        anchored = [True, False, True]
        _insert_seam_anchors(fractions, elevs, anchored, [(0.5, 99.0)])
        assert fractions == [0.0, 0.5, 1.0]
        assert elevs == [10.0, 99.0, 14.0]
        assert anchored == [True, True, True]

    def test_out_of_range_seams_ignored(self):
        fractions = [0.0, 1.0]
        elevs = [10.0, 14.0]
        anchored = [True, True]
        _insert_seam_anchors(
            fractions, elevs, anchored, [(-0.1, 5.0), (1.5, 20.0)])
        assert fractions == [0.0, 1.0]
        assert elevs == [10.0, 14.0]
        assert anchored == [True, True]
