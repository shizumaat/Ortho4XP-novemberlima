"""Runway end skirt (inverse RESA) — approach classification and law.

The skirt governs terrain that DROPS beyond a runway end, mirroring the
Pass C RESA cut that governs terrain that rises.  Regulatory basis and
plan: ``docs/runway_end_skirt_plan.md``.
"""
import pytest

from auto_patch.config import runway_end_approach_class
from auto_patch.grade_law import (
    RUNWAY_END_SKIRT_MAX_DOWN_GRADE,
    RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M,
    RUNWAY_END_SKIRT_NEAR_MAX_DOWN_GRADE,
    RUNWAY_END_SKIRT_NEAR_ZONE_M,
    runway_end_governed_length_m,
    runway_end_skirt_floor_profile,
)


# ──────────────────────────────────────────────────────────────────────
# Approach classification (apt.dat row-100 per-end markings + lights)
# ──────────────────────────────────────────────────────────────────────
class TestRunwayEndApproachClass:
    @pytest.mark.parametrize("markings", [3, 5])
    def test_precision_markings(self, markings):
        assert runway_end_approach_class(markings, 0) == "precision"

    @pytest.mark.parametrize("lights", [1, 2, 3, 4, 5, 8])
    def test_precision_approach_lights_upgrade_blank_markings(self, lights):
        """ALSF/Calvert/SSALR/MALSR imply a precision approach even when
        the markings field was left 0 (common in gateway data)."""
        assert runway_end_approach_class(0, lights) == "precision"

    def test_precision_lights_win_over_visual_paint(self):
        assert runway_end_approach_class(1, 8) == "precision"

    @pytest.mark.parametrize("markings", [2, 4])
    def test_non_precision_markings(self, markings):
        assert runway_end_approach_class(markings, 0) == "non_precision"

    @pytest.mark.parametrize("lights", [6, 7, 9, 10, 11, 12])
    def test_lesser_lighting_does_not_upgrade(self, lights):
        """SSALF/SALS/MALSF/MALS/ODALS/RAIL also serve non-precision
        approaches — they never upgrade the class on their own."""
        assert runway_end_approach_class(2, lights) == "non_precision"
        assert runway_end_approach_class(1, lights) == "visual"

    def test_explicit_visual_markings(self):
        assert runway_end_approach_class(1, 0) == "visual"

    def test_blank_row_defaults_long(self):
        """Missing data must never pick the SHORT skirt footprint."""
        assert runway_end_approach_class(0, 0) == "non_precision"


# ──────────────────────────────────────────────────────────────────────
# Governed length (footprint by ICAO code number × approach class)
# ──────────────────────────────────────────────────────────────────────
class TestGovernedLength:
    @pytest.mark.parametrize("length_m,expected", [
        (700.0, 60.0),      # code 1
        (1000.0, 90.0),     # code 2
        (1500.0, 150.0),    # code 3
        (3000.0, 240.0),    # code 4
    ])
    def test_non_precision_uses_by_code_base(self, length_m, expected):
        assert runway_end_governed_length_m(
            length_m, "non_precision") == expected

    def test_visual_clamps_long_runways_to_90(self):
        assert runway_end_governed_length_m(3000.0, "visual") == 90.0
        assert runway_end_governed_length_m(1500.0, "visual") == 90.0

    def test_visual_keeps_short_runway_base(self):
        assert runway_end_governed_length_m(700.0, "visual") == 60.0

    def test_precision_extends_code_3_and_4_to_305(self):
        assert runway_end_governed_length_m(1500.0, "precision") == 305.0
        assert runway_end_governed_length_m(3000.0, "precision") == 305.0

    def test_precision_extends_small_codes_to_240(self):
        assert runway_end_governed_length_m(700.0, "precision") == 240.0
        assert runway_end_governed_length_m(1000.0, "precision") == 240.0


# ──────────────────────────────────────────────────────────────────────
# Floor profile (lowest lawful surface beyond the runway end)
# ──────────────────────────────────────────────────────────────────────
_STATIONS = [float(d) for d in range(0, 306, 5)]


def _grades_between_stations(depths, stations):
    """Down-grade (positive = descending) between consecutive stations."""
    return [(depths[i + 1] - depths[i]) / (stations[i + 1] - stations[i])
            for i in range(len(stations) - 1)]


class TestFloorProfile:
    def test_depths_start_at_zero_and_never_recover(self):
        depths = runway_end_skirt_floor_profile(_STATIONS)
        assert depths[0] == 0.0
        assert all(b >= a for a, b in zip(depths, depths[1:]))

    def test_near_zone_respects_three_percent(self):
        depths = runway_end_skirt_floor_profile(_STATIONS)
        for i, grade in enumerate(_grades_between_stations(depths, _STATIONS)):
            if _STATIONS[i + 1] <= RUNWAY_END_SKIRT_NEAR_ZONE_M:
                assert grade <= RUNWAY_END_SKIRT_NEAR_MAX_DOWN_GRADE + 1e-9

    def test_far_zone_respects_five_percent(self):
        depths = runway_end_skirt_floor_profile(_STATIONS)
        for grade in _grades_between_stations(depths, _STATIONS):
            assert grade <= RUNWAY_END_SKIRT_MAX_DOWN_GRADE + 1e-9

    def test_far_zone_reaches_five_percent(self):
        """The floor is the LOWEST lawful surface — far out it must
        actually descend at the full 5 %, not something shallower."""
        depths = runway_end_skirt_floor_profile(_STATIONS)
        grades = _grades_between_stations(depths, _STATIONS)
        assert grades[-1] == pytest.approx(
            RUNWAY_END_SKIRT_MAX_DOWN_GRADE, abs=1e-9)

    def test_grade_change_rate_limited_everywhere(self):
        depths = runway_end_skirt_floor_profile(_STATIONS)
        grades = _grades_between_stations(depths, _STATIONS)
        step = _STATIONS[1] - _STATIONS[0]
        for a, b in zip(grades, grades[1:]):
            assert abs(b - a) / step <= (
                RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M + 1e-9)

    def test_descending_runway_enters_at_its_own_grade(self):
        """A runway descending at 1.5 % toward its end hands that grade
        to the skirt with no discontinuity at the joint: the grade over
        the first metre is the entry grade plus at most one metre of
        lawful steepening."""
        depths = runway_end_skirt_floor_profile(
            [0.0, 1.0], start_grade=-0.015)
        first_grade = depths[1] - depths[0]
        steepening = 0.5 * RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M
        assert first_grade == pytest.approx(0.015 + steepening, abs=1e-9)

    def test_climbing_runway_clamps_to_flat_start(self):
        """FAA near zone allows only downward slopes — a crest ends AT
        the pavement end, so a climbing end grade starts the floor flat,
        never above the reference elevation."""
        depths = runway_end_skirt_floor_profile(_STATIONS, start_grade=0.02)
        assert depths[0] == 0.0
        assert all(d >= 0.0 for d in depths)
        first_grade = _grades_between_stations(depths, _STATIONS)[0]
        assert first_grade <= RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M * \
            _STATIONS[1] + 1e-9

    def test_deeper_with_steeper_entry(self):
        """Entering already descending reaches the caps sooner, so the
        floor is everywhere at least as deep as the flat-entry floor."""
        flat = runway_end_skirt_floor_profile(_STATIONS, start_grade=0.0)
        descending = runway_end_skirt_floor_profile(
            _STATIONS, start_grade=-0.015)
        assert all(d >= f - 1e-12 for f, d in zip(flat, descending))

    def test_arbitrary_station_order_is_pointwise(self):
        """Each depth is a pure function of its distance — callers may
        pass stations in any order."""
        forward = runway_end_skirt_floor_profile([50.0, 100.0, 200.0])
        backward = runway_end_skirt_floor_profile([200.0, 100.0, 50.0])
        assert forward == list(reversed(backward))
