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
    runway_end_governed_length_beyond_pavement_m,
    runway_end_governed_length_m,
    runway_end_skirt_floor_profile,
    runway_end_skirt_floor_profile_beyond_pavement,
    runway_end_skirt_profile_breakpoints,
    runway_end_skirt_profile_breakpoints_beyond_pavement,
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
# Overrun-pavement anchoring: the governed footprint is measured from
# the RUNWAY END, blast pad / stopway pavement INSIDE it (FAA AC
# 150/5300-13B §3.16), so pavement past the end consumes governed
# length and advances the floor profile.
# ──────────────────────────────────────────────────────────────────────
class TestOverrunPavementAnchoring:
    def test_pavement_consumes_governed_length(self):
        assert runway_end_governed_length_beyond_pavement_m(
            305.0, 61.0) == 244.0

    def test_no_pavement_keeps_full_length(self):
        assert runway_end_governed_length_beyond_pavement_m(
            305.0, 0.0) == 305.0

    def test_pavement_covering_footprint_zeroes_the_skirt(self):
        """A blast pad longer than the governed footprint leaves nothing
        to govern (the KCLT-18L EMAS-end class)."""
        assert runway_end_governed_length_beyond_pavement_m(
            90.0, 148.0) == 0.0

    def test_negative_pavement_never_extends(self):
        assert runway_end_governed_length_beyond_pavement_m(
            240.0, -5.0) == 240.0

    def test_advanced_profile_is_a_translation(self):
        """depth_beyond_exit(d) = depth_from_end(pad + d) −
        depth_from_end(pad): the profile continues where the pavement
        left it, starting flush (depth 0) at the exit."""
        pad = 61.0
        stations = [0.0, 10.0, 50.0, 120.0, 244.0]
        advanced = runway_end_skirt_floor_profile_beyond_pavement(
            stations, 0.0, pad)
        full = runway_end_skirt_floor_profile(
            [pad] + [pad + d for d in stations], 0.0)
        assert advanced[0] == 0.0
        for got, want in zip(advanced, full[1:]):
            assert got == pytest.approx(want - full[0], abs=1e-12)

    def test_advanced_profile_descends_steeper_than_fresh(self):
        """Past a long pad the profile is already at its caps, so the
        first stations fall FASTER than a fresh 0→−3 % easing — the
        pre-fix behavior restarted the easing at the exit and pushed the
        daylight point ~a pad-length too far out."""
        fresh = runway_end_skirt_floor_profile([30.0], 0.0)[0]
        advanced = runway_end_skirt_floor_profile_beyond_pavement(
            [30.0], 0.0, 100.0)[0]
        assert advanced > fresh

    def test_zero_pavement_is_the_identity(self):
        stations = [0.0, 5.0, 61.0, 200.0]
        assert runway_end_skirt_floor_profile_beyond_pavement(
            stations, -0.01, 0.0) == runway_end_skirt_floor_profile(
            stations, -0.01)

    def test_breakpoints_shift_inward_and_drop_consumed(self):
        pad = 100.0
        base = runway_end_skirt_profile_breakpoints(0.0)
        shifted = runway_end_skirt_profile_breakpoints_beyond_pavement(
            0.0, pad)
        assert shifted == sorted(b - pad for b in base if b > pad + 1e-9)
        assert all(b > 0.0 for b in shifted)


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
    def _build(self, sample_dem, cap=_CAP):
        from auto_patch.clearance import _build_filled_skirts
        from auto_patch.grade_law import (
            runway_end_skirt_profile_breakpoints)
        stations, alts, outwards, _caps = _edge()
        return _build_filled_skirts(
            stations, alts, outwards, [cap] * len(stations),
            _law_floor_depth, runway_end_skirt_profile_breakpoints(),
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

    def test_cliff_produces_banded_skirt_on_law_floor(self):
        """A sheer drop 10 m beyond the edge is filled as ABUTTING BANDS
        split at the law's grade breakpoints (a single two-row ring
        would render a straight chord sagging metres below the curved
        floor).  Inner vertices tie to the pavement-end altitude, outer
        vertices sit on the law floor, every altitude stays within the
        floor envelope, and adjacent bands agree at their shared rows."""
        skirts = self._build(lambda x, y: _REF if x <= 10.0 else 60.0)
        assert len(skirts) >= 3   # near-zone splits + the linear tail
        deepest = _law_floor_depth(_CAP)
        shared: dict = {}
        for ring, alts in skirts:
            assert len(ring) == len(alts)
            assert max(alts) <= _REF + 1e-9
            assert min(alts) >= _REF - deepest - 0.1  # emit rounding
            for (x, y), a in zip(ring, alts):
                key = (round(x, 2), round(y, 2))
                assert shared.setdefault(key, a) == a, (
                    f"band boundary altitude mismatch at {key}")
        # Bands tile the full governed cap with no longitudinal gap.
        assert max(x for ring, _a in skirts
                   for x, _y in ring) == pytest.approx(_CAP)
        # The deepest band actually descends well below the reference
        # (the skirt is a ramp, not a shelf).
        assert min(a for _r, alts in skirts
                   for a in alts) < _REF - 0.8 * deepest

    def test_band_chords_stay_near_the_law_floor(self):
        """Within each band the floor is one quadratic, so the ruled
        chord between the band's rows sags at most rate·L²/8 ≈ 0.31 m
        below the true floor — the whole point of banding."""
        from auto_patch.grade_law import (
            RUNWAY_END_SKIRT_MAX_GRADE_CHANGE_PER_M as RATE)
        skirts = self._build(lambda x, y: _REF if x <= 10.0 else 60.0)
        # Reconstruct each band's (d0, d1, alt0, alt1) from its ring on
        # the y=0 centerline row and compare the chord midpoint.
        for ring, alts in skirts:
            xs = [x for x, _y in ring]
            d0, d1 = min(xs), max(xs)
            a0 = _REF - _law_floor_depth(d0)
            a1 = _REF - _law_floor_depth(d1)
            mid_chord = 0.5 * (a0 + a1)
            mid_floor = _REF - _law_floor_depth(0.5 * (d0 + d1))
            sag = mid_floor - mid_chord
            assert sag <= RATE * (d1 - d0) ** 2 / 8.0 + 0.05

    def test_shallow_drop_daylights_before_the_cap(self):
        """Terrain 3 m below the reference: the floor overtakes it
        within ~85 m, so the skirt daylights there instead of running
        the full governed length."""
        skirts = self._build(lambda x, y: _REF if x <= 10.0 else _REF - 3.0)
        assert skirts
        reach = max(x for ring, _a in skirts for x, _y in ring)
        # Stations stop contributing once the floor is within the 1 m
        # trigger of the terrain, i.e. depth(d) ≥ 2 m, which the faired
        # profile reaches near d ≈ 85 m; one station of overshoot is by
        # construction (last + step).
        assert 60.0 < reach < 120.0

    def test_respects_governed_length_cap(self):
        """A shorter cap truncates the same cliff's skirt: beyond the
        governed footprint a drop is lawful and stays untouched."""
        skirts = self._build(
            lambda x, y: _REF if x <= 10.0 else 60.0, cap=90.0)
        assert skirts
        reach = max(x for ring, _a in skirts for x, _y in ring)
        assert reach == pytest.approx(90.0)


# ──────────────────────────────────────────────────────────────────────
# Pass D end-to-end: synthetic-layout harness shared by the emit,
# validator, blast-pad-flank and road-awareness test classes
# ──────────────────────────────────────────────────────────────────────
class SkirtHarness:
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
        """Run the clearance emitter + the Pass D skirt emitter (which
        the pipeline calls separately, after the final grade projection)
        over a synthetic DEM: flat at runway level everywhere except
        sheer 30 m drops starting 10 m beyond BOTH runway ends
        (x < −10 and x > length + 10)."""
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
        n = clearance.emit_surface_clearance_cuts(
            layout, dem=object(), tile_lat=0, tile_lon=0,
            source_runways=[runway])
        n += clearance.emit_runway_end_skirts(
            layout, dem=object(), tile_lat=0, tile_lon=0,
            source_runways=[runway])
        return n

    @staticmethod
    def _clearance_shapes(layout):
        return [s for s in layout.shapes if s.role == "runway_clearance"]

    def _validate(self, monkeypatch, layout, runway):
        import math
        from auto_patch import elevation, verification
        from auto_patch.layout import R_EARTH

        def _fake_sample_dem(dem, tile_lat, tile_lon, lat, lon):
            x = math.radians(lon) * R_EARTH
            if -10.0 <= x <= self._RUNWAY_LEN + 10.0:
                return self._RUNWAY_ALT
            return self._RUNWAY_ALT - 30.0

        monkeypatch.setattr(elevation, "_sample_dem", _fake_sample_dem)
        return verification.check_runway_end_skirt(
            layout, dem=object(), tile_lat=0, tile_lon=0,
            source_runways=[runway])


class TestEmitPassD(SkirtHarness):
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
        # The banded skirt descends as a whole (the near band alone
        # lawfully drops < 1 m; the deep bands carry the ramp).
        for side in (east, west):
            assert min(a for s in side
                       for a in s.node_altitudes) < self._RUNWAY_ALT - 2.0

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


# ──────────────────────────────────────────────────────────────────────
# Validator: verification.check_runway_end_skirt (lockstep with Pass D)
# ──────────────────────────────────────────────────────────────────────
class TestSkirtValidator(SkirtHarness):
    def test_ungoverned_cliffs_are_reported(self, monkeypatch):
        """Gate OFF: nothing fills the drops, so the validator reports
        both ends, worst-first, ~29 m below the law floor (30 m cliff
        minus the shallow floor descent near its start)."""
        layout = self._make_layout()
        runway = self._make_runway()
        self._emit(monkeypatch, layout, runway, gate_on=False)
        findings = self._validate(monkeypatch, layout, runway)
        assert len(findings) == 2
        kinds = {f[0] for f in findings}
        assert kinds == {"end_drop"}
        desigs = {f[1] for f in findings}
        assert desigs == {"09", "27"}
        for _kind, _desig, below, tolerance, _latlon in findings:
            assert tolerance == 1.5
            assert 20.0 < below < 30.0
        # worst-first ordering
        assert findings[0][2] >= findings[1][2]

    def test_emitted_skirts_satisfy_the_validator(self, monkeypatch):
        """Gate ON: Pass D fills both drops up to the law floor, so the
        same validator comes back clean — the emitter and the reader
        share one law (no drift possible)."""
        layout = self._make_layout()
        runway = self._make_runway()
        self._emit(monkeypatch, layout, runway, gate_on=True)
        findings = self._validate(monkeypatch, layout, runway)
        assert findings == []


# ──────────────────────────────────────────────────────────────────────
# Crossing-zone clip: the skirt clears the published crossing influence
# zone (Phase 1, docs/specs/crossing-terrain-ownership.md; supersedes
# the round-8 road-lane clip)
# ──────────────────────────────────────────────────────────────────────
class TestSkirtCrossingZoneClip(SkirtHarness):
    """A runway-end skirt is clipped out of the published crossing
    influence zone (crossings, collar rings, and the depressed-road
    corridor), so it never lays its floor across a depressed public road
    (measured KBNA: 201 m² over an ``object_bridge_approach``).  The
    skirt-airside precedence ruling (2026-07-10) does NOT extend into a
    crossing's influence zone: the skirt clears it, exactly as it
    already clears surface roads."""

    def _zone_over_east_end(self):
        from shapely.geometry import Polygon
        # A band across the east skirt zone, just past the runway end.
        return Polygon([
            (self._RUNWAY_LEN + 15.0, -18.0),
            (self._RUNWAY_LEN + 45.0, -18.0),
            (self._RUNWAY_LEN + 45.0, 18.0),
            (self._RUNWAY_LEN + 15.0, 18.0)])

    def test_east_skirt_covers_zone_without_a_crossing(self, monkeypatch):
        # Control: with nothing published the east skirt fills the region
        # the zone would occupy, so the clip below is what removes it.
        layout = self._make_layout()
        self._emit(monkeypatch, layout, self._make_runway())
        zone = self._zone_over_east_end()
        skirts = [s for s in layout.shapes if s.ref == "runway_end_skirt"]
        covered = sum(s.polygon.intersection(zone).area for s in skirts)
        assert covered > 50.0

    def test_skirt_clipped_out_of_crossing_zone(self, monkeypatch):
        from auto_patch import crossing_terrain
        zone = self._zone_over_east_end()
        layout = self._make_layout()
        setattr(layout,
                crossing_terrain.CROSSING_INFLUENCE_ZONE_UNION_ATTRIBUTE,
                zone)
        self._emit(monkeypatch, layout, self._make_runway())
        skirts = [s for s in layout.shapes if s.ref == "runway_end_skirt"]
        # Skirts still emit (the west end, and the un-clipped east remainder).
        assert skirts
        overlap = sum(s.polygon.intersection(zone).area for s in skirts)
        assert overlap <= 1.0


# ──────────────────────────────────────────────────────────────────────
# Blast-pad flank wrap: lateral drops inside the governed end zone
# ──────────────────────────────────────────────────────────────────────
class TestBlastPadFlanks(SkirtHarness):
    _BLAST_PAD_LEN = 140.0

    def _make_layout(self):
        from shapely.geometry import Polygon
        from auto_patch.layout import BuiltShape
        layout = super()._make_layout()
        half_width = 22.5
        pad = Polygon([
            (self._RUNWAY_LEN, -half_width),
            (self._RUNWAY_LEN + self._BLAST_PAD_LEN, -half_width),
            (self._RUNWAY_LEN + self._BLAST_PAD_LEN, half_width),
            (self._RUNWAY_LEN, half_width)])
        layout.shapes.append(BuiltShape(
            polygon=pad, role="runway", ref="27-blastpad",
            altitude_high=self._RUNWAY_ALT,
            altitude_low=self._RUNWAY_ALT))
        return layout

    def _pad_sample_dem(self):
        """Cliffs west of the runway, along the blast pad FLANKS
        (|y| > 30 between the runway end and the pad end) and beyond
        the pad end; flat at runway level elsewhere."""
        import math
        from auto_patch.layout import R_EARTH
        pad_end = self._RUNWAY_LEN + self._BLAST_PAD_LEN

        def _fake_sample_dem(dem, tile_lat, tile_lon, lat, lon):
            x = math.radians(lon) * R_EARTH
            y = math.radians(lat) * R_EARTH
            if x < -10.0:
                return self._RUNWAY_ALT - 30.0
            if x <= self._RUNWAY_LEN:
                return self._RUNWAY_ALT
            if x <= pad_end + 5.0:
                return (self._RUNWAY_ALT if abs(y) <= 30.0
                        else self._RUNWAY_ALT - 30.0)
            return self._RUNWAY_ALT - 30.0
        return _fake_sample_dem

    def _emit(self, monkeypatch, layout, runway, gate_on=True):
        from auto_patch import clearance, elevation
        fake = self._pad_sample_dem()
        monkeypatch.setattr(clearance, "_sample_dem", fake)
        monkeypatch.setattr(elevation, "_sample_dem", fake)
        monkeypatch.setattr(
            clearance, "RUNWAY_END_SKIRT_ENABLED", gate_on)
        n = clearance.emit_surface_clearance_cuts(
            layout, dem=object(), tile_lat=0, tile_lon=0,
            source_runways=[runway])
        n += clearance.emit_runway_end_skirts(
            layout, dem=object(), tile_lat=0, tile_lon=0,
            source_runways=[runway])
        return n

    def _validate(self, monkeypatch, layout, runway):
        from auto_patch import elevation, verification
        monkeypatch.setattr(
            elevation, "_sample_dem", self._pad_sample_dem())
        return verification.check_runway_end_skirt(
            layout, dem=object(), tile_lat=0, tile_lon=0,
            source_runways=[runway])

    def test_flank_cliffs_get_wrapped(self, monkeypatch):
        layout = self._make_layout()
        self._emit(monkeypatch, layout, self._make_runway())
        pad_zone = (self._RUNWAY_LEN + 5.0,
                    self._RUNWAY_LEN + self._BLAST_PAD_LEN - 5.0)
        flanks = [s for s in self._clearance_shapes(layout)
                  if pad_zone[0] < s.polygon.centroid.x < pad_zone[1]
                  and abs(s.polygon.centroid.y) > 24.0]
        north = [s for s in flanks if s.polygon.centroid.y > 0]
        south = [s for s in flanks if s.polygon.centroid.y < 0]
        assert north and south, (
            f"expected flank skirts both sides of the blast pad, got "
            f"{len(north)} north / {len(south)} south")
        for s in flanks:
            assert s.node_altitudes
            assert max(s.node_altitudes) <= self._RUNWAY_ALT + 0.5

    def test_flank_validator_lockstep(self, monkeypatch):
        """Gate OFF: the validator reports the flank drops
        (end_drop_flank).  Gate ON: the wrap satisfies it."""
        layout = self._make_layout()
        runway = self._make_runway()
        self._emit(monkeypatch, layout, runway, gate_on=False)
        findings = self._validate(monkeypatch, layout, runway)
        assert any(k == "end_drop_flank" for k, *_rest in findings)

        layout_on = self._make_layout()
        self._emit(monkeypatch, layout_on, runway, gate_on=True)
        findings_on = self._validate(monkeypatch, layout_on, runway)
        assert findings_on == []


# ──────────────────────────────────────────────────────────────────────
# Road awareness: surface roads through the governed zone stay unfilled
# ──────────────────────────────────────────────────────────────────────
class TestRoadAwareness(SkirtHarness):
    _ROAD_X = 1700.0   # crosses the east end zone (precision → 305 m)

    def _road_network(self):
        """A surface secondary road crossing the east skirt zone at
        x = 1700, running north–south, plus a TUNNEL way NEARER the
        end (x = 1600) that must neither carve nor constrain."""
        import math
        from auto_patch.layout import R_EARTH

        def _ll(x, y):
            return (math.degrees(y / R_EARTH), math.degrees(x / R_EARTH))

        nodes = {
            "r1": _ll(self._ROAD_X, -400.0),
            "r2": _ll(self._ROAD_X, 400.0),
            "t1": _ll(1600.0, -400.0),
            "t2": _ll(1600.0, 400.0),
        }
        ways = [
            ("road", ["r1", "r2"], {"highway": "secondary"}),
            ("bore", ["t1", "t2"], {"highway": "secondary",
                                    "tunnel": "yes"}),
        ]
        return nodes, ways, {"road", "bore"}, {}

    def _emit(self, monkeypatch, layout, runway, gate_on=True):
        from auto_patch import bridges
        monkeypatch.setattr(bridges, "_load_tunnel_road_network",
                            lambda _layout: self._road_network())
        return super()._emit(monkeypatch, layout, runway, gate_on)

    def test_surface_road_truncates_and_tunnel_does_not(self, monkeypatch):
        """EMAS-inference semantics (user 2026-07-05): a surface road
        across the end zone TRUNCATES the skirt (non-standard end);
        fill runs up to the road and nothing beyond.  A tunnel-tagged
        way neither carves nor constrains — fill covers it."""
        from shapely.geometry import Point
        from auto_patch.grade_law import (
            RUNWAY_END_SKIRT_CONSTRAINT_MARGIN_M)
        layout = self._make_layout()
        # East end precision → 305 m unconstrained footprint.
        self._emit(monkeypatch, layout, self._make_runway(lights_b=2))
        skirts = self._clearance_shapes(layout)
        east = [s for s in skirts
                if s.polygon.centroid.x > self._RUNWAY_LEN]
        assert east
        assert not any(s.polygon.covers(Point(self._ROAD_X, 0.0))
                       for s in east), "skirt filled over a surface road"
        assert not any(s.polygon.covers(Point(self._ROAD_X + 15.0, 0.0))
                       for s in east), "fill beyond the constraining road"
        east_reach = max(s.polygon.bounds[2] for s in east)
        assert east_reach <= (self._ROAD_X
                              - RUNWAY_END_SKIRT_CONSTRAINT_MARGIN_M
                              + 1.0)
        # The TUNNEL way (nearer than the road) did not carve or
        # constrain the fill.
        assert any(s.polygon.covers(Point(1600.0, 0.0)) for s in east)

    def test_validator_exempts_the_same_corridor(self, monkeypatch):
        layout = self._make_layout()
        runway = self._make_runway(lights_b=2)
        # _emit patches the road network; the patch stays active for
        # the validator below (same monkeypatch scope), so both read
        # the same corridors.
        self._emit(monkeypatch, layout, runway, gate_on=True)
        findings = self._validate(monkeypatch, layout, runway)
        assert findings == []


# ──────────────────────────────────────────────────────────────────────
# EMAS inference: a road / water crossing the end zone shortens the
# governed length (no reliable EMAS data — nearby infrastructure IS the
# fingerprint of a non-standard end, user ruling 2026-07-05)
# ──────────────────────────────────────────────────────────────────────
class TestConstraintInference(SkirtHarness):
    _ROAD_X = 1580.0   # 80 m beyond the east end (precision → 305 m)

    def _road_network(self):
        import math
        from auto_patch.layout import R_EARTH

        def _ll(x, y):
            return (math.degrees(y / R_EARTH), math.degrees(x / R_EARTH))

        nodes = {"r1": _ll(self._ROAD_X, -400.0),
                 "r2": _ll(self._ROAD_X, 400.0)}
        return nodes, [("road", ["r1", "r2"],
                        {"highway": "service"})], {"road"}, {}

    def _emit(self, monkeypatch, layout, runway, gate_on=True):
        from auto_patch import bridges
        monkeypatch.setattr(bridges, "_load_tunnel_road_network",
                            lambda _layout: self._road_network())
        return super()._emit(monkeypatch, layout, runway, gate_on)

    def test_road_across_the_end_shortens_the_skirt(self, monkeypatch):
        """Service road 80 m beyond the east end: the east skirt stops
        a margin short of it instead of running the 305 m precision
        footprint (KCLT 18L class); the unconstrained west end keeps
        its full footprint."""
        from auto_patch.grade_law import (
            RUNWAY_END_SKIRT_CONSTRAINT_MARGIN_M)
        layout = self._make_layout()
        self._emit(monkeypatch, layout,
                   self._make_runway(lights_a=2, lights_b=2))
        cuts = self._clearance_shapes(layout)
        east_reach = (max(s.polygon.bounds[2] for s in cuts)
                      - self._RUNWAY_LEN)
        west_reach = -min(s.polygon.bounds[0] for s in cuts)
        road_offset = self._ROAD_X - self._RUNWAY_LEN
        assert east_reach <= (road_offset
                              - RUNWAY_END_SKIRT_CONSTRAINT_MARGIN_M
                              + 1.0)
        assert east_reach > 40.0          # still fills up to the road
        assert west_reach > 290.0         # unconstrained precision end

    def test_validator_agrees_with_the_constrained_end(self, monkeypatch):
        layout = self._make_layout()
        runway = self._make_runway(lights_a=2, lights_b=2)
        self._emit(monkeypatch, layout, runway, gate_on=True)
        findings = self._validate(monkeypatch, layout, runway)
        assert findings == []

    def test_constraint_at_the_pavement_end_suppresses_the_skirt(
            self, monkeypatch):
        """Road right at the end (KCLT 18L ground truth): no skirt at
        all on that end."""
        self._ROAD_X = self._RUNWAY_LEN + 6.0
        try:
            layout = self._make_layout()
            self._emit(monkeypatch, layout,
                       self._make_runway(lights_b=2))
            cuts = [s for s in self._clearance_shapes(layout)
                    if s.polygon.centroid.x > self._RUNWAY_LEN]
            assert cuts == []
        finally:
            self._ROAD_X = type(self)._ROAD_X

    def test_water_across_the_end_shortens_the_skirt(self, monkeypatch):
        """A water polygon 100 m beyond the east end constrains it the
        same way a road does."""
        import math
        from auto_patch import osm_load
        from auto_patch.grade_law import (
            RUNWAY_END_SKIRT_CONSTRAINT_MARGIN_M)
        from auto_patch.layout import R_EARTH

        def _ll(x, y):
            return (math.degrees(y / R_EARTH), math.degrees(x / R_EARTH))

        pond_west = self._RUNWAY_LEN + 100.0
        nodes = {"w1": _ll(pond_west, -200.0),
                 "w2": _ll(pond_west + 300.0, -200.0),
                 "w3": _ll(pond_west + 300.0, 200.0),
                 "w4": _ll(pond_west, 200.0)}
        water_ways = [("pond", ["w1", "w2", "w3", "w4", "w1"],
                       {"natural": "water"})]
        monkeypatch.setattr(
            osm_load, "_load_osm_road_layer",
            lambda layer, lat, lon, radius_deg=0.05:
            (nodes, water_ways, {}) if layer == "water"
            else ({}, [], {}))
        layout = self._make_layout()
        SkirtHarness._emit(self, monkeypatch, layout,
                           self._make_runway(lights_b=2))
        cuts = [s for s in self._clearance_shapes(layout)
                if s.polygon.centroid.x > self._RUNWAY_LEN]
        assert cuts
        east_reach = (max(s.polygon.bounds[2] for s in cuts)
                      - self._RUNWAY_LEN)
        assert east_reach <= (100.0
                              - RUNWAY_END_SKIRT_CONSTRAINT_MARGIN_M
                              + 1.0)


# ──────────────────────────────────────────────────────────────────────
# check_grade DEM-free skirt edge reader
# ──────────────────────────────────────────────────────────────────────
class TestCheckGradeSkirtReader:
    @staticmethod
    def _import_check_grade():
        import os
        import sys
        tools_directory = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools")
        if tools_directory not in sys.path:
            sys.path.insert(0, tools_directory)
        import check_grade
        return check_grade

    def _skirt_way(self, elevations):
        check_grade = self._import_check_grade()
        n = len(elevations)
        nids = [f"n{i}" for i in range(n)] + ["n0"]
        return check_grade.Way(
            wid="w1", role="runway_clearance", ref="runway_end_skirt",
            aeroway="aerodrome", nids=nids,
            elevs=list(elevations) + [elevations[0]], tags={})

    @staticmethod
    def _nodes(points):
        import math
        from auto_patch.layout import R_EARTH
        return {f"n{i}": (math.degrees(y / R_EARTH),
                          math.degrees(x / R_EARTH))
                for i, (x, y) in enumerate(points)}

    @staticmethod
    def _ll_to_m(lat, lon):
        import math
        from auto_patch.layout import R_EARTH
        return (math.radians(lon) * R_EARTH, math.radians(lat) * R_EARTH)

    def test_lawful_skirt_ring_passes(self):
        check_grade = self._import_check_grade()
        # 20 m wide band descending 5 % along +x over 20 m: Δz = 1.0.
        way = self._skirt_way([100.0, 99.0, 99.0, 100.0])
        nodes = self._nodes([(0, 0), (20, 0), (20, 30), (0, 30)])
        assert check_grade._check_runway_end_skirt_edges(
            [way], nodes, self._ll_to_m) == []

    def test_over_steep_edge_flags(self):
        check_grade = self._import_check_grade()
        # 8 % over 20 m — beyond the 5 % law cap + noise.
        way = self._skirt_way([100.0, 98.4, 98.4, 100.0])
        nodes = self._nodes([(0, 0), (20, 0), (20, 30), (0, 30)])
        violations = check_grade._check_runway_end_skirt_edges(
            [way], nodes, self._ll_to_m)
        assert len(violations) == 2   # both long edges
        assert violations[0].grade_pct == pytest.approx(8.0, abs=0.1)

    def test_non_skirt_ways_ignored(self):
        check_grade = self._import_check_grade()
        way = self._skirt_way([100.0, 90.0, 90.0, 100.0])
        way.ref = "surface_clearance"
        nodes = self._nodes([(0, 0), (20, 0), (20, 30), (0, 30)])
        assert check_grade._check_runway_end_skirt_edges(
            [way], nodes, self._ll_to_m) == []


# ──────────────────────────────────────────────────────────────────────
# Fixture airports: validator smoke + gate-off baseline capture
# ──────────────────────────────────────────────────────────────────────
class TestSkirtValidatorAtFixtures:
    @staticmethod
    def _fixture_airports():
        from conftest import baseline_airports, xplane_available
        if not xplane_available():
            return []
        return baseline_airports()

    def test_baseline_report(self):
        """Reporter-only at the fixture airports (gate currently off in
        default builds): the validator must RUN everywhere; its counts
        are the M4 calibration baseline, printed for capture, never
        asserted zero here."""
        import math
        airports = self._fixture_airports()
        if not airports:
            pytest.skip("X-Plane fixtures not available")
        from conftest import cached_airport_layout
        from auto_patch.elevation import _load_airport_dem
        from auto_patch.verification import check_runway_end_skirt
        for icao in airports:
            layout = cached_airport_layout(icao)
            lat0, lon0 = layout.anchor
            dem = _load_airport_dem(lat0, lon0)
            if dem is None:
                continue
            findings = check_runway_end_skirt(
                layout, dem,
                int(math.floor(lat0)), int(math.floor(lon0)))
            assert isinstance(findings, list)
            for kind, desig, below, tolerance, latlon in findings:
                assert kind in ("end_drop", "end_drop_flank")
                assert below > tolerance
            print(f"[skirt-baseline] {icao}: {len(findings)} end(s) "
                  + "; ".join(f"{f[1]} −{f[2]:.1f} m @{f[4]}"
                              for f in findings))
