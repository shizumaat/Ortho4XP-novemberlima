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


class TestMinimalEndZoneCapEscalation:
    """``solve_profile_with_minimal_end_zone_cap`` — the end-zone cap
    (0.8% preference) yields MINIMALLY to the main-cap LAW (user ruling
    2026-07-08): when hard anchors make the 0.8%-end-capped solve leave
    a segment over the 1.5% main cap, the end-zone cap escalates to the
    smallest law-compliant value in (0.8%, 1.5%]; a runway feasible at
    0.8% keeps 0.8% verbatim."""

    AXIS_LENGTH_M = 1000.0

    def _profile(self, threshold_a: float, threshold_b: float):
        """11 samples over [0, 1]: anchored thresholds at both ends,
        free interior samples seeded on the straight line between."""
        fractions = [k / 10.0 for k in range(11)]
        elevs = [threshold_a
                 + (threshold_b - threshold_a) * t for t in fractions]
        anchored = [True] + [False] * 9 + [True]
        return fractions, elevs, anchored

    def _max_segment_grade(self, fractions, elevs):
        return max(
            abs(elevs[k] - elevs[k - 1])
            / ((fractions[k] - fractions[k - 1]) * self.AXIS_LENGTH_M)
            for k in range(1, len(fractions)))

    def test_feasible_at_end_zone_preference_keeps_it_verbatim(self):
        from auto_patch.runway_redistribute import (
            RUNWAY_END_GRADE, solve_profile_with_minimal_end_zone_cap)
        # 5 m over 1000 m = 0.5% uniform — comfortably inside 0.8%.
        fractions, elevs, anchored = self._profile(0.0, 5.0)
        cap = solve_profile_with_minimal_end_zone_cap(
            fractions, elevs, anchored, self.AXIS_LENGTH_M)
        assert cap == RUNWAY_END_GRADE
        assert self._max_segment_grade(fractions, elevs) <= 0.008 + 1e-6

    def test_escalates_minimally_when_preference_infeasible(self):
        from auto_patch.runway_redistribute import (
            MAX_RUNWAY_GRADE, RUNWAY_END_GRADE,
            solve_profile_with_minimal_end_zone_cap)
        # 13 m over 1000 m: feasible at the uniform 1.5% law (1.3%) but
        # NOT at the 0.8% end zones (max rise 0.008*500 + 0.015*500 =
        # 11.5 m < 13 m).  Closed-form minimal end cap ignoring the
        # K-factor: 500*c + 0.015*500 = 13 → c = 1.1%; the vertical
        # curve transitions push it slightly higher.
        fractions, elevs, anchored = self._profile(0.0, 13.0)
        cap = solve_profile_with_minimal_end_zone_cap(
            fractions, elevs, anchored, self.AXIS_LENGTH_M)
        # Escalated — but MINIMALLY (well below full relaxation to 1.5%).
        assert cap > RUNWAY_END_GRADE
        assert 0.0105 <= cap <= 0.0135
        assert cap < MAX_RUNWAY_GRADE
        # The accepted solve is law-compliant everywhere and keeps the
        # hard anchors verbatim.
        assert self._max_segment_grade(fractions, elevs) \
            <= MAX_RUNWAY_GRADE + 1e-4 + 1e-9
        assert elevs[0] == 0.0
        assert elevs[-1] == 13.0

    def test_infeasible_even_at_law_cap_keeps_main_cap_solve(self):
        from auto_patch.runway_redistribute import (
            MAX_RUNWAY_GRADE, solve_profile_with_minimal_end_zone_cap)
        # 20 m over 1000 m = 2% between hard anchors: no end-zone cap
        # can satisfy the law — the uniform main-cap solve is kept
        # (best-effort; the validator is the backstop).
        fractions, elevs, anchored = self._profile(0.0, 20.0)
        cap = solve_profile_with_minimal_end_zone_cap(
            fractions, elevs, anchored, self.AXIS_LENGTH_M)
        assert cap == MAX_RUNWAY_GRADE
        assert elevs[0] == 0.0
        assert elevs[-1] == 20.0
