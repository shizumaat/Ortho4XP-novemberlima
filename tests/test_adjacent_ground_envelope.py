"""Adjacent-ground LATERAL grade law — the corridor off a pavement edge.

The lateral generalization of the runway-END skirt: ground beside a paved
surface is a two-zone-plus-ungraded CORRIDOR of lawful height offsets relative
to the pavement-edge elevation.  Regulatory basis, the four Noah rulings and
the slice plan: ``docs/adjacent_ground_grade_law_plan.md``.

These tests pin the corridor's exact bounds at representative distances for
each role/code, the mandatory-DOWN direction (a flat surround is OUTSIDE the
corridor), the zone-3 unbounded floor (cliffs lawful), and — the load-bearing
design detail — CONTINUITY of both bounds across the zone boundaries.
"""
import pytest

from auto_patch.config import (
    APRON_EDGE_WALL_MIN_DROP_M,
    CLEARANCE_MAX_REACH_M,
    RUNWAY_STRIP_HALF_WIDTH_BY_CODE,
    taxiway_strip_graded_half_width_for_letter,
)
from auto_patch.grade_law import adjacent_ground_envelope

EPS = 1e-6


# ──────────────────────────────────────────────────────────────────────
# Zone 1 — drainage lip (shared runway & taxiway strips): 3-5 % DOWN
# ──────────────────────────────────────────────────────────────────────
class TestDrainageLip:
    @pytest.mark.parametrize("role,cn,cl", [
        ("runway", 4, None), ("runway", 2, None),
        ("taxiway", None, "D"), ("taxiway", None, "A"),
    ])
    def test_lip_bounds_are_3_to_5_percent_down(self, role, cn, cl):
        """Within the first 3 m the surface must fall 3-5 %: floor at 5 %,
        ceiling at 3 % — both below the edge."""
        floor, ceiling = adjacent_ground_envelope(role, cn, cl, 1.5)
        assert floor == pytest.approx(-0.05 * 1.5)
        assert ceiling == pytest.approx(-0.03 * 1.5)

    def test_flat_surface_is_outside_the_corridor(self):
        """The lip is a MANDATORY-down band (ruling 1, enforce fully): a flat
        surround (offset 0) sits ABOVE the ceiling, i.e. is unlawful."""
        floor, ceiling = adjacent_ground_envelope("runway", 4, None, 2.0)
        assert ceiling < 0.0
        assert floor < ceiling

    def test_flush_at_the_edge(self):
        assert adjacent_ground_envelope("runway", 4, None, 0.0) == (0.0, 0.0)
        assert adjacent_ground_envelope("apron", None, None, 0.0) == (0.0, 0.0)


# ──────────────────────────────────────────────────────────────────────
# Zone 2 — graded band, exact accumulated bounds
# ──────────────────────────────────────────────────────────────────────
class TestRunwayGradedBand:
    def test_code4_band_values(self):
        # C-E down cap 3 %, min 1.5 %, W = 75 m.
        assert adjacent_ground_envelope("runway", 4, None, 40) == (
            pytest.approx(-1.26), pytest.approx(-0.645))
        assert adjacent_ground_envelope("runway", 4, None, 75) == (
            pytest.approx(-2.31), pytest.approx(-1.17))

    def test_code4_band_falls_at_least_1_1m_over_75m(self):
        """Ruling 1's worked example: a code-4 runway's band falls ≥1.1 m
        over its 75 m half-width (the 1.5 % minimum, no flats)."""
        _, ceiling = adjacent_ground_envelope("runway", 4, None, 75)
        assert -ceiling >= 1.1

    def test_code_number_keys_the_down_cap(self):
        """Code 1/2 (≈ AAC A/B) earns the steeper 5 % down cap; code 3/4
        (≈ AAC C-E) the 3 % cap — so at the same distance the smaller code
        may drop further (lower floor)."""
        floor2, _ = adjacent_ground_envelope("runway", 2, None, 40)
        floor4, _ = adjacent_ground_envelope("runway", 4, None, 40)
        assert floor2 == pytest.approx(-2.0)      # -0.15 - 0.05·37
        assert floor4 == pytest.approx(-1.26)     # -0.15 - 0.03·37
        assert floor2 < floor4


class TestTaxiwayGradedBand:
    def test_letter_D_band_values(self):
        # down 1.5-5 %, W = 18.5 m.
        assert adjacent_ground_envelope("taxiway", None, "D", 10) == (
            pytest.approx(-0.5), pytest.approx(-0.195))
        assert adjacent_ground_envelope("taxiway", None, "D", 18.5) == (
            pytest.approx(-0.925), pytest.approx(-0.3225))

    def test_code_letter_keys_the_graded_width(self):
        """Narrow letters get a narrower graded band (OMGWS table)."""
        assert taxiway_strip_graded_half_width_for_letter("A") == 10.25
        assert taxiway_strip_graded_half_width_for_letter("F") == 22.0
        # Unknown/None letter falls back to code C (12.5 m), never a D-F width.
        assert taxiway_strip_graded_half_width_for_letter(None) == 12.5
        assert taxiway_strip_graded_half_width_for_letter("Z") == 12.5

    def test_taxiway_family_roles_use_the_taxiway_strip(self):
        base = adjacent_ground_envelope("taxiway", None, "C", 8.0)
        for role in ("primary_parallel", "stub", "cross_connector", "junction"):
            assert adjacent_ground_envelope(role, None, "C", 8.0) == base


# ──────────────────────────────────────────────────────────────────────
# Zone 3 — ungraded strip: ceiling ≤5 % UP, floor UNBOUNDED (cliffs lawful)
# ──────────────────────────────────────────────────────────────────────
class TestUngradedStrip:
    def test_floor_is_none_beyond_the_graded_band(self):
        """The boundary-bridge killer: no downward mandate past the graded
        portion — the DEM wins below (floor None)."""
        floor, ceiling = adjacent_ground_envelope("runway", 4, None, 100)
        assert floor is None
        assert ceiling == pytest.approx(0.08)

    def test_ceiling_rises_at_5_percent_up(self):
        _, c100 = adjacent_ground_envelope("runway", 4, None, 100)
        _, c110 = adjacent_ground_envelope("runway", 4, None, 110)
        assert c110 - c100 == pytest.approx(0.05 * 10)

    def test_beyond_reach_is_ungoverned(self):
        assert adjacent_ground_envelope("runway", 4, None, 300) == (None, None)
        assert adjacent_ground_envelope("runway", 4, None, 350) == (None, None)
        assert adjacent_ground_envelope("taxiway", None, "D", 100) == (None, None)
        assert adjacent_ground_envelope("taxiway", None, "D", 150) == (None, None)

    def test_reach_matches_the_shared_clearance_bound(self):
        # Reuses CLEARANCE_MAX_REACH_M, not a duplicated constant.
        just_inside = CLEARANCE_MAX_REACH_M["runway"] - EPS
        floor, ceiling = adjacent_ground_envelope(
            "runway", 4, None, just_inside)
        assert ceiling is not None            # still governed just inside
        assert adjacent_ground_envelope(
            "runway", 4, None, CLEARANCE_MAX_REACH_M["runway"]) == (None, None)


# ──────────────────────────────────────────────────────────────────────
# CONTINUITY — the load-bearing design detail: no envelope STEP at any
# zone boundary; the bounds are continuous functions of d (accumulated).
# ──────────────────────────────────────────────────────────────────────
class TestContinuity:
    @pytest.mark.parametrize("role,cn,cl", [
        ("runway", 4, None), ("runway", 2, None), ("taxiway", None, "D"),
    ])
    def test_no_step_at_the_lip_edge(self, role, cn, cl):
        """At the 3 m lip edge both bounds must match from either side."""
        f_lo, c_lo = adjacent_ground_envelope(role, cn, cl, 3.0 - EPS)
        f_hi, c_hi = adjacent_ground_envelope(role, cn, cl, 3.0 + EPS)
        assert f_lo == pytest.approx(f_hi, abs=1e-4)
        assert c_lo == pytest.approx(c_hi, abs=1e-4)

    @pytest.mark.parametrize("role,cn,cl,width", [
        ("runway", 4, None, RUNWAY_STRIP_HALF_WIDTH_BY_CODE[4]),
        ("runway", 2, None, RUNWAY_STRIP_HALF_WIDTH_BY_CODE[2]),
        ("taxiway", None, "D", 18.5),
    ])
    def test_ceiling_continuous_at_the_band_edge(self, role, cn, cl, width):
        """At the graded-band edge the CEILING must not step (zone-3 ceiling
        continues from the zone-2 endpoint).  The floor deliberately goes
        finite→None there — that only OPENS the corridor downward."""
        _, c_lo = adjacent_ground_envelope(role, cn, cl, width - EPS)
        _, c_hi = adjacent_ground_envelope(role, cn, cl, width + EPS)
        assert c_lo == pytest.approx(c_hi, abs=1e-4)
        f_lo, _ = adjacent_ground_envelope(role, cn, cl, width - EPS)
        f_hi, _ = adjacent_ground_envelope(role, cn, cl, width + EPS)
        assert f_lo is not None and f_hi is None   # cliff opens, no step up

    def test_bounds_are_monotone_down_through_zones_1_2(self):
        """Both bounds fall monotonically through the mandatory-down zones."""
        prev_f, prev_c = 0.0, 0.0
        for d in [0.5, 1.5, 3.0, 20.0, 50.0, 75.0]:
            f, c = adjacent_ground_envelope("runway", 4, None, d)
            assert f <= prev_f + EPS and c <= prev_c + EPS
            assert f <= c
            prev_f, prev_c = f, c


# ──────────────────────────────────────────────────────────────────────
# Apron edges — 3 m shoulder (1-3 % down) then zone-3 immediately
# ──────────────────────────────────────────────────────────────────────
class TestApron:
    def test_shoulder_is_1_to_3_percent_down(self):
        floor, ceiling = adjacent_ground_envelope("apron", None, None, 1.5)
        assert floor == pytest.approx(-0.03 * 1.5)
        assert ceiling == pytest.approx(-0.01 * 1.5)
        assert ceiling < 0.0                       # flat rejected in the shoulder

    def test_zone3_immediately_beyond_the_shoulder(self):
        floor, ceiling = adjacent_ground_envelope("apron", None, None, 10.0)
        assert floor is None                       # free floor beyond shoulder
        assert ceiling == pytest.approx(-0.03 + 0.05 * 7)

    def test_shoulder_edge_is_continuous(self):
        f_lo, c_lo = adjacent_ground_envelope("apron", None, None, 3.0 - EPS)
        _, c_hi = adjacent_ground_envelope("apron", None, None, 3.0 + EPS)
        assert c_lo == pytest.approx(c_hi, abs=1e-4)
        assert f_lo is not None

    def test_apron_family_roles(self):
        base = adjacent_ground_envelope("apron", None, None, 2.0)
        assert adjacent_ground_envelope("stand", None, None, 2.0) == base
        assert adjacent_ground_envelope("terminal", None, None, 2.0) == base

    def test_wall_threshold_constant_exists(self):
        """Ruling 3: a retaining-wall face replaces fill past a deep drop.
        The threshold is a named single-source constant (the emitter, slice 3,
        consumes it)."""
        assert APRON_EDGE_WALL_MIN_DROP_M == pytest.approx(1.5)


# ──────────────────────────────────────────────────────────────────────
# Service roads — UNCHANGED 15 m cut-only flat shadow (design choice)
# ──────────────────────────────────────────────────────────────────────
class TestServiceRoad:
    def test_flat_shadow_cut_only_band(self):
        # ceiling at edge level (flat), floor free (never fill).
        for role in ("service_road", "service_junction"):
            assert adjacent_ground_envelope(role, None, None, 5.0) == (None, 0.0)

    def test_band_ends_at_15m(self):
        assert adjacent_ground_envelope("service_road", None, None, 15.0) == (
            None, None)
        assert adjacent_ground_envelope("service_road", None, None, 20.0) == (
            None, None)


# ──────────────────────────────────────────────────────────────────────
# Contract
# ──────────────────────────────────────────────────────────────────────
class TestContract:
    def test_unknown_role_raises(self):
        with pytest.raises(ValueError):
            adjacent_ground_envelope("boundary", None, None, 5.0)

    def test_runway_needs_code_number(self):
        with pytest.raises(ValueError):
            adjacent_ground_envelope("runway", None, None, 5.0)

    def test_deterministic(self):
        a = adjacent_ground_envelope("taxiway", None, "E", 12.3)
        b = adjacent_ground_envelope("taxiway", None, "E", 12.3)
        assert a == b
