"""Adjacent-ground LATERAL emitter — band math (slice 3).

The pure geometry of the banded emitter: a synthetic straight pavement
edge + a synthetic DEM feed the corridor builders and we pin the emitted
bands / values.  The corridor VALUES themselves are pinned in
``test_adjacent_ground_envelope.py``; here we pin the EMISSION:

  * CUT fires where the DEM rises above the sloped ceiling, and the band
    rides that ceiling (below the pavement edge in the graded zones).
  * FILL fires where the DEM falls below the floor, bounded by the
    graded width (zone 3's free floor emits nothing).
  * DEM inside the corridor emits NOTHING.
  * the apron retaining-wall face fires only past the drop threshold.
  * the nearest-vertex resampler is continuous across a band seam.
"""
import math

import pytest

from auto_patch.config import (
    ADJACENT_GROUND_LIP_WIDTH_M,
    APRON_EDGE_WALL_MIN_DROP_M,
    APRON_SHOULDER_WIDTH_M,
    CLEARANCE_MAX_REACH_M,
    CLEARANCE_STATION_STEP_M,
    taxiway_strip_graded_half_width_for_letter,
)
from auto_patch.grade_law import adjacent_ground_envelope
from auto_patch import adjacent_ground as AG

STEP = CLEARANCE_STATION_STEP_M
TRIGGER = 1.0
EDGE_ALT = 100.0


def _straight_edge(length_m=120.0):
    """Stations along the x-axis at y=0, outward normal +y, flat edge."""
    n = int(length_m // STEP) + 1
    stations = [(k * STEP, 0.0) for k in range(n)]
    alts = [EDGE_ALT] * n
    outs = [(0.0, 1.0)] * n
    return stations, alts, outs


def _taxi_c_fns():
    def ceil_off(d):
        return adjacent_ground_envelope("taxiway", None, "C", d)[1]

    def floor_depth(d):
        f = adjacent_ground_envelope("taxiway", None, "C", d)[0]
        return None if f is None else -f
    width = taxiway_strip_graded_half_width_for_letter("C")
    reach = CLEARANCE_MAX_REACH_M["taxiway"]
    return ceil_off, floor_depth, width, reach


# ──────────────────────────────────────────────────────────────────────
# CUT direction (_build_cut_bands)
# ──────────────────────────────────────────────────────────────────────
class TestCutBands:
    def test_rising_terrain_is_cut_to_the_ceiling(self):
        """DEM 5 m above the edge everywhere → cut bands ride the sloped
        ceiling, which sits BELOW the edge in the graded zones."""
        stations, alts, outs = _straight_edge()
        ceil_off, _, _, reach = _taxi_c_fns()
        m = len(stations)

        def dem(x, y):
            return EDGE_ALT + 5.0

        bands = AG._build_cut_bands(
            stations, alts, outs, [reach] * m, ceil_off,
            {ADJACENT_GROUND_LIP_WIDTH_M,
             taxiway_strip_graded_half_width_for_letter("C")},
            TRIGGER, STEP, dem)
        assert bands, "rising terrain must produce cut bands"
        width = taxiway_strip_graded_half_width_for_letter("C")
        for ring, ralts in bands:
            assert len(ring) == len(ralts) >= 4
            # The cut never rises above the DEM (it removes material).
            assert max(ralts) <= EDGE_ALT + 5.0 + 1e-6
            # Inside the graded zones (d ≤ W) the mandatory-down ceiling
            # is at or below the pavement edge.
            for (vx, vy), a in zip(ring, ralts):
                if vy <= width + 1e-6:
                    assert a <= EDGE_ALT + 1e-6

    def test_flat_surround_within_trigger_emits_no_cut(self):
        """A flat surround at the edge altitude deviates from the shallow
        taxiway ceiling by < the 1 m trigger, so nothing is cut (the
        reused clearance trigger gates the sub-metre mandate)."""
        stations, alts, outs = _straight_edge()
        ceil_off, _, _, reach = _taxi_c_fns()
        m = len(stations)
        bands = AG._build_cut_bands(
            stations, alts, outs, [reach] * m, ceil_off,
            {ADJACENT_GROUND_LIP_WIDTH_M}, TRIGGER, STEP,
            lambda x, y: EDGE_ALT)
        assert bands == []


# ──────────────────────────────────────────────────────────────────────
# FILL direction (_build_fill_bands, the skirt fill builder's lateral twin)
# + no zone-3 fill
# ──────────────────────────────────────────────────────────────────────
class TestFillBands:
    def test_falling_terrain_is_filled_within_the_graded_width(self):
        stations, alts, outs = _straight_edge()
        _, floor_depth, width, _ = _taxi_c_fns()
        m = len(stations)
        bands = AG._build_fill_bands(
            stations, alts, outs, [width] * m, floor_depth,
            {ADJACENT_GROUND_LIP_WIDTH_M}, TRIGGER, STEP,
            lambda x, y: EDGE_ALT - 8.0)
        assert bands, "falling terrain inside the band must fill"
        # No filled vertex reaches beyond the graded half-width (zone-3
        # cliffs are lawful — the fill is bounded by W).
        for ring, _ in bands:
            assert max(vy for _, vy in ring) <= width + 1e-6

    def test_deep_drop_beyond_width_is_not_filled(self):
        """The floor is None beyond the graded width, so a ravine outside
        the band leaves the DEM untouched (boundary-bridge killer)."""
        _, floor_depth, width, _ = _taxi_c_fns()
        assert floor_depth(width + 20.0) is None


# ──────────────────────────────────────────────────────────────────────
# Weld to pavement (user ruling 2026-07-09): the first band's inner row
# sits AT the pavement edge (d = 0), and a vertex on the ring carries
# the pavement edge value verbatim — no standoff groove, no rounding.
# ──────────────────────────────────────────────────────────────────────
class TestWeldToPavement:
    def test_fill_band_inner_row_sits_on_the_ring(self):
        stations, alts, outs = _straight_edge()
        _, floor_depth, width, _ = _taxi_c_fns()
        m = len(stations)
        bands = AG._build_fill_bands(
            stations, alts, outs, [width] * m, floor_depth,
            {ADJACENT_GROUND_LIP_WIDTH_M}, TRIGGER, STEP,
            lambda x, y: EDGE_ALT - 8.0)
        assert bands
        inner_ys = [vy for ring, _ in bands for _, vy in ring
                    if abs(vy) < 1e-9]
        assert inner_ys, "expected inner-row vertices AT the pavement edge"

    def test_cut_band_inner_row_sits_on_the_ring(self):
        stations, alts, outs = _straight_edge()
        ceil_off, _, width, reach = _taxi_c_fns()
        m = len(stations)
        bands = AG._build_cut_bands(
            stations, alts, outs, [reach] * m, ceil_off,
            {ADJACENT_GROUND_LIP_WIDTH_M, width}, TRIGGER, STEP,
            lambda x, y: EDGE_ALT + 6.0)
        assert bands
        at_edge = [(vy, a) for ring, ralts in bands
                   for (_, vy), a in zip(ring, ralts) if abs(vy) < 1e-9]
        assert at_edge, "expected inner-row vertices AT the pavement edge"
        # The weld row carries the edge value (corridor at d=0 is [0,0]).
        for _, a in at_edge:
            assert a == pytest.approx(EDGE_ALT, abs=1e-6)

    def test_weld_row_value_is_exact_and_flagged(self):
        """A resampled vertex ON the ring returns the UNROUNDED edge
        value with the weld flag set (emit consensus must be a no-op)."""
        edge_alt = 99.87   # would round to 99.9 under the 0.1 band rule
        ring = [(0.0, 0.0), (40.0, 0.0), (40.0, -40.0), (0.0, -40.0),
                (0.0, 0.0)]
        ring_alts = [edge_alt] * 5
        width = taxiway_strip_graded_half_width_for_letter("C")

        def envelope_at(d):
            return adjacent_ground_envelope("taxiway", None, "C", d)

        resample = AG._make_edge_projection_resampler(
            ring, ring_alts, envelope_at, width,
            lambda x, y: edge_alt - 8.0)
        value, is_weld = resample(20.0, 0.0, "fill")
        assert is_weld is True
        assert value == pytest.approx(edge_alt, abs=1e-9)
        off_value, off_weld = resample(20.0, 8.0, "fill")
        assert off_weld is False

    def test_dedup_ring_collapses_fan_corner_duplicates(self):
        ring = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0),
                (5.0, 3.0), (3.0, 5.0)]
        alts = [1.0, 1.0, 1.0, 2.0, 3.0]
        kept_ring, kept_alts = AG._dedup_ring(ring, alts)
        assert kept_ring == [(0.0, 0.0), (5.0, 3.0), (3.0, 5.0)]
        assert kept_alts == [1.0, 2.0, 3.0]


# ──────────────────────────────────────────────────────────────────────
# DEM inside the corridor → nothing
# ──────────────────────────────────────────────────────────────────────
def test_dem_inside_corridor_emits_nothing():
    """A DEM that falls at the mid-band drainage rate sits INSIDE the
    corridor at every distance → neither cut nor fill fires."""
    stations, alts, outs = _straight_edge()
    ceil_off, floor_depth, width, reach = _taxi_c_fns()
    m = len(stations)

    def dem(x, y):
        # -4 % is between the 3 % ceiling and 5 % floor of the lip and
        # inside the zone-2 band corridor.
        return EDGE_ALT - 0.04 * y

    cut = AG._build_cut_bands(
        stations, alts, outs, [reach] * m, ceil_off,
        {ADJACENT_GROUND_LIP_WIDTH_M, width}, TRIGGER, STEP, dem)
    fill = AG._build_fill_bands(
        stations, alts, outs, [width] * m, floor_depth,
        {ADJACENT_GROUND_LIP_WIDTH_M}, TRIGGER, STEP, dem)
    assert cut == []
    assert fill == []


# ──────────────────────────────────────────────────────────────────────
# Corner weld: adjacent bands share their boundary row exactly
# ──────────────────────────────────────────────────────────────────────
def test_adjacent_cut_bands_share_boundary_row_values():
    """The lip band and the zone-2 band split at d=3 m must agree on the
    shared row's altitude (so the surface welds, no tear)."""
    stations, alts, outs = _straight_edge()
    ceil_off, _, width, reach = _taxi_c_fns()
    m = len(stations)
    # Rising terrain so both bands emit across the whole edge.
    bands = AG._build_cut_bands(
        stations, alts, outs, [reach] * m, ceil_off,
        {ADJACENT_GROUND_LIP_WIDTH_M, width}, TRIGGER, STEP,
        lambda x, y: EDGE_ALT + 6.0)
    # Collect the altitude emitted at the y == lip boundary row.
    lip = ADJACENT_GROUND_LIP_WIDTH_M
    at_lip = {}
    for ring, ralts in bands:
        for (vx, vy), a in zip(ring, ralts):
            if abs(vy - lip) < 1e-6:
                at_lip.setdefault(round(vx, 3), set()).add(a)
    assert at_lip, "expected vertices on the lip boundary row"
    for vx, vals in at_lip.items():
        assert len(vals) == 1, f"tear at x={vx}: {vals}"


# ──────────────────────────────────────────────────────────────────────
# CLAMP-INTO-CORRIDOR value rule (round 2 — the law-alignment fix): an
# emitted band vertex is the DEM clamped into [edge+floor(d),
# edge+ceiling(d)]; unlike the skirt FLOOR law, the corridor bounds BOTH
# sides, so a DEM bump above the ceiling is cut to it.
# ──────────────────────────────────────────────────────────────────────
class TestClampIntoCorridor:
    def _resampler(self, dem_value):
        # A 40 m square pavement ring, flat at EDGE_ALT.
        ring = [(0.0, 0.0), (40.0, 0.0), (40.0, -40.0), (0.0, -40.0),
                (0.0, 0.0)]
        alts = [EDGE_ALT] * 5
        width = taxiway_strip_graded_half_width_for_letter("C")

        def envelope_at(d):
            return adjacent_ground_envelope("taxiway", None, "C", d)

        return AG._make_edge_projection_resampler(
            ring, alts, envelope_at, width, lambda x, y: dem_value)

    def test_dem_above_ceiling_is_cut_to_the_ceiling(self):
        resample = self._resampler(EDGE_ALT + 10.0)
        d = 8.0     # zone 2, off the top edge (outward normal +y)
        _, ceiling_offset = adjacent_ground_envelope(
            "taxiway", None, "C", d)
        assert resample(20.0, d, "cut")[0] == pytest.approx(
            EDGE_ALT + ceiling_offset, abs=0.06)

    def test_dem_below_floor_is_filled_to_the_floor(self):
        resample = self._resampler(EDGE_ALT - 10.0)
        d = 8.0
        floor_offset, _ = adjacent_ground_envelope("taxiway", None, "C", d)
        assert resample(20.0, d, "fill")[0] == pytest.approx(
            EDGE_ALT + floor_offset, abs=0.06)

    def test_dem_inside_corridor_passes_through(self):
        # d = 12 m: the taxi-C corridor spread there (~0.38 m) exceeds
        # twice the snap-to-bound band, so a mid-corridor DEM is far
        # enough from both bounds to pass through unsnapped.  (In the
        # narrower near-edge corridor a mid value lawfully snaps to the
        # nearest bound — the triangle-diet rule.)
        d = 12.0
        floor_offset, ceiling_offset = adjacent_ground_envelope(
            "taxiway", None, "C", d)
        assert (ceiling_offset - floor_offset) > 2 * AG._CORRIDOR_SNAP_TOL_M
        mid = EDGE_ALT + 0.5 * (floor_offset + ceiling_offset)
        resample = self._resampler(mid)
        assert resample(20.0, d, "fill")[0] == pytest.approx(mid, abs=0.06)
        assert resample(20.0, d, "cut")[0] == pytest.approx(mid, abs=0.06)

    def test_near_bound_dem_snaps_to_the_bound(self):
        """Triangle diet: a DEM within the snap band of a corridor bound
        emits the bound itself (piecewise-linear, decimates away)."""
        d = 12.0
        _, ceiling_offset = adjacent_ground_envelope(
            "taxiway", None, "C", d)
        near_ceiling = EDGE_ALT + ceiling_offset - 0.05
        resample = self._resampler(near_ceiling)
        assert resample(20.0, d, "cut")[0] == pytest.approx(
            EDGE_ALT + ceiling_offset, abs=0.06)

    def test_zone3_free_floor_keeps_deep_dem(self):
        """Beyond the graded width the floor is None — a deep DEM under a
        cut piece stays (never filled); only the rising ceiling clips."""
        width = taxiway_strip_graded_half_width_for_letter("C")
        d = width + 10.0
        resample = self._resampler(EDGE_ALT - 30.0)
        assert resample(20.0, d, "cut")[0] == pytest.approx(
            EDGE_ALT - 30.0, abs=0.06)

    def test_fill_piece_never_crosses_the_width_discontinuity(self):
        """A fill vertex whose projection jitters past W stays on the
        shelf edge (floor at W), not 30 m down on the DEM — the round-2
        CYXY in-piece cliff class."""
        width = taxiway_strip_graded_half_width_for_letter("C")
        floor_at_width, _ = adjacent_ground_envelope(
            "taxiway", None, "C", width)
        resample = self._resampler(EDGE_ALT - 30.0)
        just_past = width + 0.05
        assert resample(20.0, just_past, "fill")[0] == pytest.approx(
            EDGE_ALT + floor_at_width, abs=0.06)


# ──────────────────────────────────────────────────────────────────────
# Nearest resampler continuity
# ──────────────────────────────────────────────────────────────────────
def test_nearest_alt_picks_closest_sample():
    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    alts = [100.0, 101.0, 102.0, 103.0]
    assert AG._nearest_alt(pts, alts, 0.5, 0.5) == 100.0
    assert AG._nearest_alt(pts, alts, 9.5, 0.5) == 101.0
    assert AG._nearest_alt(pts, alts, 9.5, 9.5) == 102.0


# ──────────────────────────────────────────────────────────────────────
# Apron retaining-wall threshold (_emit_apron_walls)
# ──────────────────────────────────────────────────────────────────────
class _FakeLayout:
    def __init__(self):
        self.shapes = []
        self.airport_boundary = None


def _apron_ceil_off(d):
    return adjacent_ground_envelope("apron", None, None, d)[1]


def test_apron_wall_fires_only_past_the_drop_threshold():
    stations, alts, outs = _straight_edge(length_m=60.0)
    m = len(stations)
    # Shoulder outer edge altitude ≈ EDGE_ALT + ceil_off(3) (just below).
    shoulder_edge = EDGE_ALT + _apron_ceil_off(APRON_SHOULDER_WIDTH_M)

    # A drop just UNDER the threshold → no wall.
    shallow = _FakeLayout()
    n0, _ = AG._emit_apron_walls(
        shallow, stations, alts, outs, _apron_ceil_off, STEP,
        lambda x, y: shoulder_edge - (APRON_EDGE_WALL_MIN_DROP_M - 0.3),
        None, None)
    assert n0 == 0
    assert not shallow.shapes

    # A drop well OVER the threshold → a retaining-wall face.
    deep = _FakeLayout()
    n1, _ = AG._emit_apron_walls(
        deep, stations, alts, outs, _apron_ceil_off, STEP,
        lambda x, y: shoulder_edge - (APRON_EDGE_WALL_MIN_DROP_M + 4.0),
        None, None)
    assert n1 >= 1
    assert deep.shapes
    from auto_patch.layout import ROLE_RETAINING_WALL
    assert all(s.role == ROLE_RETAINING_WALL for s in deep.shapes)


# ──────────────────────────────────────────────────────────────────────
# DAYLIGHT slope-limit law (grade_law.adjacent_ground_supported_depths)
# ──────────────────────────────────────────────────────────────────────
class TestDaylightSupportedDepths:
    """The along-frontage benching law that kills the isolated-ray knife
    slot (CYXY 417).  Stations march at STEP (5 m) along y=0."""

    @staticmethod
    def _line_positions(n, spacing=STEP):
        return [(k * spacing, 0.0) for k in range(n)]

    def test_isolated_deep_spike_clamps_to_slope_limit_scale(self):
        """A single 156 m ray among 0-depth neighbours one station (5 m)
        away is benched to ~LIMIT * spacing — a shallow entry, not a
        knife-slot blade."""
        from auto_patch.grade_law import adjacent_ground_supported_depths
        from auto_patch.config import ADJACENT_GROUND_DAYLIGHT_SLOPE_LIMIT
        depths = [0.0, 0.0, 156.0, 0.0, 0.0]
        pos = self._line_positions(len(depths))
        out = adjacent_ground_supported_depths(depths, pos)
        # The spike is clamped from BOTH flanking zeros at 5 m spacing.
        expected = ADJACENT_GROUND_DAYLIGHT_SLOPE_LIMIT * STEP
        assert out[2] == pytest.approx(expected)          # 2.0 * 5 = 10 m
        # The 156 m blade is gone; the neighbours are untouched (0).
        assert out[2] < 156.0
        assert out[0] == out[1] == out[3] == out[4] == 0.0

    def test_two_station_spike_benches_in_from_both_sides(self):
        """Two adjacent deep stations among zeros: each is benched from its
        nearer flank, so the pair ramps in at the slope limit rather than
        standing as a 156 m twin blade."""
        from auto_patch.grade_law import adjacent_ground_supported_depths
        from auto_patch.config import ADJACENT_GROUND_DAYLIGHT_SLOPE_LIMIT
        depths = [0.0, 156.0, 156.0, 0.0]
        pos = self._line_positions(len(depths))
        out = adjacent_ground_supported_depths(depths, pos)
        step_allow = ADJACENT_GROUND_DAYLIGHT_SLOPE_LIMIT * STEP
        # Station 1 benches up from the left zero, station 2 from the right.
        assert out[1] == pytest.approx(step_allow)
        assert out[2] == pytest.approx(step_allow)
        assert max(out) < 156.0

    def test_wide_hill_is_unchanged(self):
        """A genuine hill rising 10 m of governed depth per 5 m station over
        10 stations is a supported daylight line (slope 2.0 = the limit),
        so the law leaves every depth untouched."""
        from auto_patch.grade_law import adjacent_ground_supported_depths
        depths = [10.0 * k for k in range(10)]        # 0, 10, 20, … 90
        pos = self._line_positions(len(depths))       # 5 m apart → slope 2.0
        out = adjacent_ground_supported_depths(depths, pos)
        assert out == pytest.approx(depths)

    def test_fan_stations_share_position_gain_no_allowance(self):
        """Corner-fan stations share ONE coordinate (dist 0), so a fan ray
        earns NO extra allowance over its corner — a deep fan flanked by a
        0-depth corner is clamped straight to 0 (the CYXY 417 fan blade)."""
        from auto_patch.grade_law import adjacent_ground_supported_depths
        # Station 0 unobstructed corner; stations 1-2 fan rays at the SAME
        # corner coordinate marching deep; station 3 back on the next edge.
        depths = [0.0, 120.0, 120.0, 0.0]
        pos = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (STEP, 0.0)]
        out = adjacent_ground_supported_depths(depths, pos)
        # Zero-distance to the 0-depth corner ⇒ the fans clamp to 0.
        assert out[1] == pytest.approx(0.0)
        assert out[2] == pytest.approx(0.0)

    def test_symmetric_under_reversal(self):
        """The forward+backward sweep is reversal-invariant: limiting a
        sequence then reversing equals reversing then limiting."""
        from auto_patch.grade_law import adjacent_ground_supported_depths
        depths = [0.0, 3.0, 156.0, 12.0, 0.0, 40.0]
        pos = self._line_positions(len(depths))
        fwd = adjacent_ground_supported_depths(depths, pos)
        rev = adjacent_ground_supported_depths(
            depths[::-1], pos[::-1])
        assert fwd == pytest.approx(rev[::-1])

    def test_continuation_seam_terminal_holds_raw_depth(self):
        """A deep terminal station at a pavement-PARTITION seam (the run ends
        because an abutting airside shape continues the frontage, not because
        the frontage ends) is pinned to its raw depth — it must NOT bench in
        toward its own locally-unobstructed interior neighbour, so it agrees
        with the abutting shape's full-depth terminal (no seam notch)."""
        from auto_patch.grade_law import adjacent_ground_supported_depths
        # Stations march inward → seam.  The interior shallows to 0; the
        # terminal (index 3) sits at the seam at full depth 12.5.
        depths = [0.0, 4.0, 8.0, 12.5]
        pos = self._line_positions(len(depths))
        seam = [False, False, False, True]
        out = adjacent_ground_supported_depths(depths, pos, seam)
        # Pinned terminal holds full depth (unpinned it would bench toward the
        # interior ramp).
        assert out[3] == pytest.approx(12.5)
        # The interior stays supported (the deep seam seeds the ramp), not
        # dragged below its raw values.
        assert out == pytest.approx(depths)

    def test_continuation_seam_default_off_still_benches(self):
        """With no seam flags (the default / a TRUE frontage end) the deep
        terminal benches exactly as the daylight law — the pin is opt-in."""
        from auto_patch.grade_law import adjacent_ground_supported_depths
        from auto_patch.config import ADJACENT_GROUND_DAYLIGHT_SLOPE_LIMIT
        depths = [0.0, 0.0, 0.0, 12.5]
        pos = self._line_positions(len(depths))
        unpinned = adjacent_ground_supported_depths(depths, pos)
        # The isolated deep terminal benches toward its 0-depth neighbour.
        assert unpinned[3] == pytest.approx(
            ADJACENT_GROUND_DAYLIGHT_SLOPE_LIMIT * STEP)
        # A None flag list is identical to omitting it.
        assert adjacent_ground_supported_depths(
            depths, pos, None) == pytest.approx(unpinned)
