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
# FILL direction (reused clearance._build_filled_skirts) + no zone-3 fill
# ──────────────────────────────────────────────────────────────────────
class TestFillBands:
    def test_falling_terrain_is_filled_within_the_graded_width(self):
        stations, alts, outs = _straight_edge()
        _, floor_depth, width, _ = _taxi_c_fns()
        m = len(stations)
        bands = AG._build_filled_skirts(
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
    fill = AG._build_filled_skirts(
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
