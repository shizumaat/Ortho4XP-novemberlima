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


class TestTieredThresholdBand:
    """TIERED end-zone relaxation (user 2026-07-16, KBNA 13/31 defect G):
    the last ``threshold_strict_m`` before each threshold holds the strict
    0.8% cap even when the OUTER end zone escalates; it relaxes only when
    the profile is genuinely infeasible even with the outer end zone at the
    1.5% law."""

    AXIS = 1000.0
    STRICT_M = 90.0

    def _profile(self, hb: float):
        """Threshold A at 0, threshold B at ``hb``; free interior samples,
        with samples placed at the 90 m strict-band boundaries."""
        fr = [0.0, 0.045, 0.09, 0.17, 0.25, 0.375, 0.5, 0.625, 0.75,
              0.83, 0.91, 0.955, 1.0]
        elevs = [hb * t for t in fr]
        anchored = [False] * len(fr)
        anchored[0] = anchored[-1] = True
        return fr, elevs, anchored

    def _seg_grade(self, fr, elevs, k):
        run = (fr[k] - fr[k - 1]) * self.AXIS
        return abs(elevs[k] - elevs[k - 1]) / run

    def _tsf(self):
        return self.STRICT_M / self.AXIS

    def test_feasible_keeps_strict_everywhere(self):
        from auto_patch.runway_redistribute import (
            RUNWAY_END_GRADE, solve_profile_with_minimal_end_zone_cap)
        fr, elevs, anchored = self._profile(5.0)     # 0.5% uniform
        report = {}
        cap = solve_profile_with_minimal_end_zone_cap(
            fr, elevs, anchored, self.AXIS,
            threshold_strict_m=self.STRICT_M, report=report)
        assert cap == RUNWAY_END_GRADE
        assert report['threshold_cap'] == RUNWAY_END_GRADE
        assert report['binding'] == []          # instrument: nothing bound

    def test_outer_escalates_threshold_band_held(self):
        from auto_patch.runway_redistribute import (
            RUNWAY_END_GRADE, MAX_RUNWAY_GRADE,
            solve_profile_with_minimal_end_zone_cap)
        # 13 m: infeasible at strict 0.8% end zone, feasible when the OUTER
        # end zone escalates while the 90 m threshold band stays at 0.8%.
        fr, elevs, anchored = self._profile(13.0)
        report = {}
        cap = solve_profile_with_minimal_end_zone_cap(
            fr, elevs, anchored, self.AXIS,
            threshold_strict_m=self.STRICT_M, report=report)
        # The OUTER end zone escalated (above 0.8%) …
        assert RUNWAY_END_GRADE < cap <= MAX_RUNWAY_GRADE
        # … but the threshold band cap was NOT relaxed.
        assert report['threshold_cap'] == RUNWAY_END_GRADE
        # Instrument-first: the reason is recorded.
        assert report['binding'], "binding report should explain the escalation"
        # Overall law-compliant everywhere.
        for k in range(1, len(fr)):
            assert self._seg_grade(fr, elevs, k) <= MAX_RUNWAY_GRADE + 2e-4

    def test_genuine_infeasibility_relaxes_threshold_band(self):
        from auto_patch.runway_redistribute import (
            RUNWAY_END_GRADE, MAX_RUNWAY_GRADE,
            solve_profile_with_minimal_end_zone_cap)
        # 14.5 m: infeasible even with the outer end zone at the 1.5% law
        # and the threshold band strict — so the threshold band must relax.
        fr, elevs, anchored = self._profile(14.5)
        report = {}
        solve_profile_with_minimal_end_zone_cap(
            fr, elevs, anchored, self.AXIS,
            threshold_strict_m=self.STRICT_M, report=report)
        assert report['threshold_cap'] > RUNWAY_END_GRADE + 1e-9
        assert report['threshold_cap'] <= MAX_RUNWAY_GRADE + 1e-9
        # Still main-cap compliant (the LAW).
        for k in range(1, len(fr)):
            assert self._seg_grade(fr, elevs, k) <= MAX_RUNWAY_GRADE + 2e-4

    def test_tiered_vs_untiered_threshold_band_is_gentler(self):
        """The tiered solve makes the near-threshold grade STRICTLY gentler
        than the untiered solve for the same infeasible anchors (the whole
        point of defect G — the deficit is pushed deeper into the end zone).
        K-factor smoothing means the band is not strictly ≤0.8%, but it is
        materially gentler than the uniform escalation."""
        from auto_patch.runway_redistribute import (
            solve_profile_with_minimal_end_zone_cap)
        # Untiered (historical): the whole end zone escalates uniformly.
        fr_u, ev_u, an_u = self._profile(13.0)
        solve_profile_with_minimal_end_zone_cap(
            fr_u, ev_u, an_u, self.AXIS, threshold_strict_m=0.0)
        # Tiered: threshold band held.
        fr_t, ev_t, an_t = self._profile(13.0)
        solve_profile_with_minimal_end_zone_cap(
            fr_t, ev_t, an_t, self.AXIS, threshold_strict_m=self.STRICT_M)
        # First segment off the threshold: tiered strictly gentler.
        g_u = self._seg_grade(fr_u, ev_u, 1)
        g_t = self._seg_grade(fr_t, ev_t, 1)
        assert g_t < g_u - 1e-4, (g_t, g_u)


class TestFlexThresholdBandClamp:
    """The RUNWAY FLEX must not drag the runway steeper than 0.8% within the
    strict threshold band (KBNA 13/31 defect G — the flex, not the
    redistribute solve, is what steepened the 31 end).  ``flex_slack_at``
    bounds a near-threshold contact against the pinned threshold at
    ``RUNWAY_END_GRADE`` rather than ``MAX_RUNWAY_GRADE``."""

    AXIS = 3368.0

    def _profile(self, strict_frac):
        # threshold A @ 0 (164.6), threshold B @ 1 (177.0), gentle interior.
        fractions = [0.0, 0.5, 1.0]
        elevs = [164.6, 170.8, 177.0]
        return {
            'fractions': fractions,
            'elevs': elevs,
            'anchored': [True, False, True],
            'seam_t': [],
            'axis_len2': self.AXIS ** 2,
            'threshold_strict_fraction': strict_frac,
        }

    def test_near_threshold_downslack_uses_strict_cap(self):
        from auto_patch.runway_redistribute import (
            flex_slack_at, RUNWAY_END_GRADE, MAX_RUNWAY_GRADE)
        strict_frac = 90.0 / self.AXIS
        # A contact 13 m before the B threshold wants to move DOWN.
        t = 1.0 - 13.0 / self.AXIS
        prof_tiered = self._profile(strict_frac)
        prof_untiered = self._profile(0.0)
        slack_tiered = flex_slack_at(prof_tiered, t, -1.0)
        slack_untiered = flex_slack_at(prof_untiered, t, -1.0)
        # The tiered down-slack is much smaller (0.8% vs 1.5% × 13 m from
        # the pinned threshold), so the flex cannot steepen the last 13 m.
        assert slack_tiered < slack_untiered
        # The absolute cap: a flex to (current − slack) sits within 0.8% of
        # the B threshold.
        from auto_patch.runway_redistribute import _interp_profile
        current = _interp_profile(prof_tiered['fractions'],
                                  prof_tiered['elevs'], t)
        flexed = current - slack_tiered
        grade = abs(177.0 - flexed) / 13.0
        assert grade <= RUNWAY_END_GRADE + 1e-6
        assert grade > 0.5 * MAX_RUNWAY_GRADE  # sanity: it IS near the cap

    def test_deep_contact_keeps_main_cap(self):
        """A contact well outside the band still flexes at the 1.5% cap."""
        from auto_patch.runway_redistribute import flex_slack_at
        strict_frac = 90.0 / self.AXIS
        t = 0.5  # mid-runway
        s_tiered = flex_slack_at(self._profile(strict_frac), t, -1.0)
        s_untiered = flex_slack_at(self._profile(0.0), t, -1.0)
        assert abs(s_tiered - s_untiered) < 1e-6
