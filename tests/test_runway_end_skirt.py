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


# ──────────────────────────────────────────────────────────────────────
# _build_filled_skirts — the fill-direction twin of the cut builder
# ──────────────────────────────────────────────────────────────────────
_STEP = 5.0
_REF = 100.0
_CAP = 240.0


def _edge(n_stations=9):
    """A straight pavement-end edge along y, filling outward along +x."""
    stations = [(0.0, float(i) * _STEP) for i in range(n_stations)]
    outwards = [(1.0, 0.0)] * n_stations
    alts = [_REF] * n_stations
    caps = [_CAP] * n_stations
    return stations, alts, outwards, caps


def _law_floor_depth(distance_m):
    return runway_end_skirt_floor_profile([distance_m])[0]


class TestBuildFilledSkirts:
    def _build(self, sample_dem):
        from auto_patch.clearance import _build_filled_skirts
        stations, alts, outwards, caps = _edge()
        return _build_filled_skirts(
            stations, alts, outwards, caps, _law_floor_depth,
            1.0, _STEP, sample_dem)

    def test_flat_terrain_leaves_no_skirt(self):
        assert self._build(lambda x, y: _REF) == []

    def test_rising_terrain_leaves_no_skirt(self):
        """Rising terrain is the CUT passes' domain."""
        assert self._build(lambda x, y: _REF + 0.02 * x) == []

    def test_lawful_gentle_descent_leaves_no_skirt(self):
        """Terrain already descending within the law floor (2 % ≤ the
        3 %/5 % caps) needs no fill."""
        assert self._build(lambda x, y: _REF - 0.02 * x) == []

    def test_cliff_produces_skirt_on_law_floor(self):
        """A sheer drop 10 m beyond the edge is filled: inner ring
        vertices tie to the pavement-end altitude, outer vertices sit on
        the law floor, and every altitude stays within the floor
        envelope (never below the deepest lawful point, never above the
        reference)."""
        skirts = self._build(lambda x, y: _REF if x <= 10.0 else 60.0)
        assert len(skirts) == 1
        ring, alts = skirts[0]
        assert len(ring) == len(alts)
        deepest = _law_floor_depth(_CAP)
        assert max(alts) <= _REF + 1e-9
        assert min(alts) >= _REF - deepest - 0.1   # 0.1 m emit rounding
        # The floor never reaches the 60 m terrain within the governed
        # cap, so the skirt runs the full cap length.
        assert max(x for x, _y in ring) == pytest.approx(_CAP)
        # Outer-edge altitudes actually descend well below the reference
        # (the skirt is a ramp, not a shelf).
        assert min(alts) < _REF - 0.8 * deepest

    def test_shallow_drop_daylights_before_the_cap(self):
        """Terrain 3 m below the reference: the floor overtakes it
        within ~150 m, so the skirt daylights there instead of running
        the full governed length."""
        skirts = self._build(lambda x, y: _REF if x <= 10.0 else _REF - 3.0)
        assert len(skirts) == 1
        ring, _alts = skirts[0]
        reach = max(x for x, _y in ring)
        # Stations stop contributing once the floor is within the 1 m
        # trigger of the terrain, i.e. depth(d) ≥ 2 m, which the faired
        # profile reaches near d ≈ 85 m; one station of overshoot is by
        # construction (last + step).
        assert 60.0 < reach < 120.0

    def test_respects_governed_length_cap(self):
        """A shorter cap truncates the same cliff's skirt: beyond the
        governed footprint a drop is lawful and stays untouched."""
        from auto_patch.clearance import _build_filled_skirts
        stations, alts, outwards, _caps = _edge()
        skirts = _build_filled_skirts(
            stations, alts, outwards, [90.0] * len(stations),
            _law_floor_depth, 1.0, _STEP,
            lambda x, y: _REF if x <= 10.0 else 60.0)
        assert len(skirts) == 1
        ring, _alts = skirts[0]
        assert max(x for x, _y in ring) == pytest.approx(90.0)


# ──────────────────────────────────────────────────────────────────────
# Pass D end-to-end: emit_surface_clearance_cuts with a synthetic layout
# ──────────────────────────────────────────────────────────────────────
class TestEmitPassD:
    _RUNWAY_LEN = 1500.0   # ICAO code 3
    _RUNWAY_ALT = 100.0

    def _make_layout(self):
        from shapely.geometry import Polygon
        from auto_patch.layout import BuiltShape, PavementLayout
        half_width = 22.5
        rect = Polygon([
            (0.0, -half_width), (self._RUNWAY_LEN, -half_width),
            (self._RUNWAY_LEN, half_width), (0.0, half_width)])
        layout = PavementLayout(icao="ZZZZ", anchor=(0.0, 0.0))
        layout.shapes.append(BuiltShape(
            polygon=rect, role="runway", ref="09-27",
            altitude_high=self._RUNWAY_ALT,
            altitude_low=self._RUNWAY_ALT))
        return layout

    def _make_runway(self, markings_a=0, lights_a=0,
                     markings_b=0, lights_b=0):
        import math
        from auto_patch.apt_dat_reader import Runway
        from auto_patch.layout import R_EARTH
        lon_b = math.degrees(self._RUNWAY_LEN / R_EARTH)
        return Runway(
            desig_a="09", desig_b="27",
            lat_a=0.0, lon_a=0.0, lat_b=0.0, lon_b=lon_b,
            width_m=45.0, surface_code=1,
            displaced_a_m=0.0, displaced_b_m=0.0,
            markings_a=markings_a, approach_lights_a=lights_a,
            markings_b=markings_b, approach_lights_b=lights_b)

    def _emit(self, monkeypatch, layout, runway, gate_on=True):
        """Run the clearance emitter over a synthetic DEM: flat at
        runway level everywhere except sheer 30 m drops starting 10 m
        beyond BOTH runway ends (x < −10 and x > length + 10)."""
        import math
        from auto_patch import clearance
        from auto_patch.layout import R_EARTH

        def _fake_sample_dem(dem, tile_lat, tile_lon, lat, lon):
            x = math.radians(lon) * R_EARTH
            if -10.0 <= x <= self._RUNWAY_LEN + 10.0:
                return self._RUNWAY_ALT
            return self._RUNWAY_ALT - 30.0

        monkeypatch.setattr(clearance, "_sample_dem", _fake_sample_dem)
        monkeypatch.setattr(
            clearance, "RUNWAY_END_SKIRT_ENABLED", gate_on)
        return clearance.emit_surface_clearance_cuts(
            layout, dem=object(), tile_lat=0, tile_lon=0,
            source_runways=[runway])

    @staticmethod
    def _clearance_shapes(layout):
        return [s for s in layout.shapes if s.role == "runway_clearance"]

    def test_gate_off_emits_nothing(self, monkeypatch):
        layout = self._make_layout()
        n = self._emit(monkeypatch, layout, self._make_runway(),
                       gate_on=False)
        assert n == 0
        assert self._clearance_shapes(layout) == []

    def test_cliffs_beyond_both_ends_get_skirts(self, monkeypatch):
        layout = self._make_layout()
        n = self._emit(monkeypatch, layout, self._make_runway())
        assert n >= 2
        cuts = self._clearance_shapes(layout)
        east = [s for s in cuts
                if s.polygon.centroid.x > self._RUNWAY_LEN]
        west = [s for s in cuts if s.polygon.centroid.x < 0.0]
        assert east and west
        for s in cuts:
            assert s.node_altitudes
            assert max(s.node_altitudes) <= self._RUNWAY_ALT + 0.5
            assert min(s.node_altitudes) < self._RUNWAY_ALT - 2.0

    def test_governed_length_scales_with_approach_class(self, monkeypatch):
        """Same cliff on both ends; end a (west) is explicitly VISUAL,
        end b (east) is PRECISION (ALSF-II) — the west skirt stops at
        the 90 m visual clamp while the east one runs to the 305 m
        precision footprint."""
        layout = self._make_layout()
        self._emit(monkeypatch, layout,
                   self._make_runway(markings_a=1, lights_b=2))
        cuts = self._clearance_shapes(layout)
        west_reach = -min(s.polygon.bounds[0] for s in cuts)
        east_reach = (max(s.polygon.bounds[2] for s in cuts)
                      - self._RUNWAY_LEN)
        assert west_reach <= 90.0 + _STEP + 1.0
        assert west_reach > 60.0
        assert east_reach > 290.0
        assert east_reach <= 305.0 + _STEP + 1.0
