"""GAP INTERIOR RINGS (ratified 2026-07-11; ROUND-8 revision: complete
closed loops, value-gated; gate O4_GAP_FILL_INTERIOR_RINGS, chained
under O4_GAP_FILL_SPINE).

Synthetic fixtures only (the test_gap_fill_spine frame pattern): a
rectangular pavement frame enclosing one hole, plus a stub DEM whose
``alt`` drives the per-station value clamp.

Round-8 pins:
  * gate OFF emits no rings and leaves the plain gap-fill path
    untouched; ring gate ON + spine gate OFF is a HARD error;
  * rings emit as COMPLETE CLOSED LOOPS (first node repeats at the
    end) — no arcs, no taper stations, no mid-gap chain ends;
  * station values are clamp(terrain, floor, ceiling) at the
    point-law distances: a deep dip pins AT the floor (fill), lawful
    terrain rides invisibly;
  * a gap whose EVERY station of BOTH rings is a value no-op skips
    its rings entirely (per-gap economy gate, all-or-nothing) and its
    spine emits exactly as gate-OFF;
  * ALONG-RING continuity: adjacent-station value steps are bench- or
    terrain-limited, never a pin-to-terrain cliff;
  * the SPINE is trimmed to the ring-2 core (a full-length spine
    would cross the closed loops at the gap ends);
  * narrow gaps collapse (ladder rung 4): no rings, today's
    spine-only behavior;
  * runway-bounded ring widths key the TRUE ICAO code from runway
    AXES, not the tile-cut segment chord;
  * zero-lens guards: nodes inside the gap, clear of boundary/spine/
    other chains, minimum spacing.
"""
import math

import pytest
from shapely.geometry import LineString, Point, Polygon

from auto_patch import gap_fill as GF
from auto_patch.gap_fill import emit_gap_fill_spines
from auto_patch.grade_law import adjacent_ground_envelope
from auto_patch.layout import BuiltShape, ROLE_RUNWAY, ROLE_STUB

EDGE_ALT = 100.0


class _FakeLayout:
    def m_to_ll(self, x, y):
        return (y / 111320.0, x / 111320.0)

    def ll_to_m(self, lat, lon):
        return (lon * 111320.0, lat * 111320.0)

    def __init__(self, shapes):
        self.shapes = shapes
        self.airport_boundary = None
        self.anchor = (0.0, 0.0)


class _StubDem:
    """DEM stub: ``alt((dx, dy))`` in tile-offset degrees; the fake
    layout maps x = lon * 111320, y = lat * 111320."""
    def __init__(self, fn):
        self._fn = fn

    def alt(self, t):
        dx, dy = t
        return self._fn(dx * 111320.0, dy * 111320.0)


def _rect(x0, y0, x1, y1, role):
    poly = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    coords = list(poly.exterior.coords)
    return BuiltShape(polygon=poly, role=role,
                      node_altitudes=[EDGE_ALT] * len(coords))


FRAME_LENGTH = 1300.0


def _frame_layout(gap_half_width_m, length=FRAME_LENGTH):
    """Two parallel RUNWAY rects joined by two end STUBS enclosing one
    hole ``2*gap_half_width_m`` across (the test_gap_fill_spine frame)."""
    inner_x0, inner_x1 = 30.0, length - 30.0
    y_bot1 = 30.0
    y_gap0, y_gap1 = y_bot1, y_bot1 + 2.0 * gap_half_width_m
    y_top1 = y_gap1 + 30.0
    shapes = [
        _rect(0.0, 0.0, length, y_bot1, ROLE_RUNWAY),
        _rect(0.0, y_gap1, length, y_top1, ROLE_RUNWAY),
        _rect(0.0, y_gap0, inner_x0, y_gap1, ROLE_STUB),
        _rect(inner_x1, y_gap0, length, y_gap1, ROLE_STUB),
    ]
    return _FakeLayout(shapes)


def _lawful_terrain(gap_half_width_m, length=FRAME_LENGTH):
    """Terrain INSIDE every parent's corridor: edge − 3 %·d − 1 mm,
    ``d`` = distance to the nearest frame inner edge (lawful for the
    runway, taxiway and lip envelopes alike — every station is a value
    no-op, so the economy gate must skip the gap)."""
    y0, y1 = 30.0, 30.0 + 2.0 * gap_half_width_m
    x0, x1 = 30.0, length - 30.0

    def fn(x, y):
        d = max(0.0, min(y - y0, y1 - y, x - x0, x1 - x))
        return EDGE_ALT - 0.03 * d - 0.001
    return _StubDem(fn)


class _FakeRunway:
    """apt.dat runway row stand-in: just the two threshold lat/lons."""
    def __init__(self, x_a, y_a, x_b, y_b):
        self.lat_a, self.lon_a = y_a / 111320.0, x_a / 111320.0
        self.lat_b, self.lon_b = y_b / 111320.0, x_b / 111320.0


def _rings(layout):
    return getattr(layout, "gap_interior_rings", None) or []


def _ring_chains_m(layout):
    out = []
    for pts_ll, alts in _rings(layout):
        out.append(([(lon * 111320.0, lat * 111320.0)
                     for lat, lon in pts_ll], list(alts)))
    return out


_LOW = _StubDem(lambda x, y: 90.0)          # deep dip everywhere


@pytest.fixture
def rings_on(monkeypatch):
    monkeypatch.setattr(GF, "GAP_FILL_INTERIOR_RINGS_ENABLED", True)


def test_gate_off_emits_no_rings(monkeypatch):
    monkeypatch.setattr(GF, "GAP_FILL_INTERIOR_RINGS_ENABLED", False)
    layout = _frame_layout(30.0)
    n = emit_gap_fill_spines(layout, _LOW, 0, 0)
    assert n == 1
    assert not _rings(layout)


def test_ring_gate_requires_spine_gate(monkeypatch):
    monkeypatch.setattr(GF, "GAP_FILL_INTERIOR_RINGS_ENABLED", True)
    monkeypatch.setattr(GF, "GAP_FILL_SPINE_ENABLED", False)
    layout = _frame_layout(30.0)
    with pytest.raises(RuntimeError):
        emit_gap_fill_spines(layout, _LOW, 0, 0)


def test_rings_are_complete_closed_loops(rings_on):
    """Round-8 mandate: both rings, complete unbroken closed loops —
    the first coordinate repeats at the end, no mid-gap chain ends."""
    layout = _frame_layout(30.0)
    n = emit_gap_fill_spines(layout, _LOW, 0, 0)
    assert n == 1
    chains = _ring_chains_m(layout)
    assert len(chains) == 2, "ring 1 + ring 2, one loop each"
    for pts, alts in chains:
        assert len(pts) >= 20
        assert pts[0] == pts[-1], "every ring chain must be CLOSED"
        assert alts[0] == alts[-1]


def test_deep_dip_pins_at_the_floor(rings_on):
    """Value law: with terrain far below every floor, each node carries
    at least the point-law floor (max over the two nearest parents of
    edge + floor_offset at min(true distance, band width)); most nodes
    sit EXACTLY on it (the along-ring bench may lift seam nodes, never
    lower them)."""
    layout = _frame_layout(30.0)
    emit_gap_fill_spines(layout, _LOW, 0, 0)
    from auto_patch.layout import taxi_shape_code_letter
    from auto_patch.config import taxiway_strip_graded_half_width_for_letter
    pav = []
    for s in layout.shapes:
        if s.role == ROLE_RUNWAY:          # 1300 m chord -> code 3
            pav.append((s.polygon.exterior, ("runway", 3, None), 75.0))
        elif s.role == ROLE_STUB:
            letter = taxi_shape_code_letter(layout, s)
            pav.append((s.polygon.exterior, ("stub", None, letter),
                        taxiway_strip_graded_half_width_for_letter(letter)))
    exact = checked = 0
    for pts, alts in _ring_chains_m(layout):
        open_pts = pts[:-1] if pts[0] == pts[-1] else pts
        for (x, y), v in zip(open_pts, alts):
            dists = sorted((ext.distance(Point(x, y)), key, w)
                           for ext, key, w in pav)
            floors = []
            for d, (role, cn, cl), w in dists[:2]:
                lo, _hi = adjacent_ground_envelope(role, cn, cl, min(d, w))
                if lo is not None:
                    floors.append(EDGE_ALT + lo)
            assert floors
            expected = max(floors)
            assert v >= expected - 0.05, (
                f"node at ({x:.0f},{y:.0f}) value {v} BELOW its floor "
                f"{expected:.2f}")
            checked += 1
            if abs(v - expected) <= 0.05:
                exact += 1
    assert checked >= 40
    assert exact >= checked * 0.6, "most nodes must sit exactly on floor"


def test_lawful_terrain_economy_skips_whole_gap(rings_on):
    """Per-gap economy gate (all-or-nothing): terrain inside the law
    corridor everywhere -> NO rings, and the spine emits equal to the
    gate-OFF run (the ring gate must be value-invisible)."""
    dem = _lawful_terrain(30.0)
    layout_on = _frame_layout(30.0)
    emit_gap_fill_spines(layout_on, dem, 0, 0)
    assert not _rings(layout_on)
    layout_off = _frame_layout(30.0)
    orig = GF.GAP_FILL_INTERIOR_RINGS_ENABLED
    GF.GAP_FILL_INTERIOR_RINGS_ENABLED = False
    try:
        emit_gap_fill_spines(layout_off, dem, 0, 0)
    finally:
        GF.GAP_FILL_INTERIOR_RINGS_ENABLED = orig
    assert layout_on.gap_spines == layout_off.gap_spines


def test_along_ring_continuity(rings_on):
    """Round-8 continuity: adjacent-station value steps are bench- or
    terrain-limited — never a pin-to-terrain cliff.  A hard 9 m step
    in the DEM must NOT appear as a hard step in the ring values."""
    y0 = 30.0

    def fn(x, y):
        if x < 500.0:
            return 90.0                      # deep dip west
        d = max(0.0, min(y - y0, (y0 + 60.0) - y, x - 30.0,
                         (FRAME_LENGTH - 30.0) - x))
        return EDGE_ALT - 0.03 * d - 0.001   # lawful east
    layout = _frame_layout(30.0)
    emit_gap_fill_spines(layout, _StubDem(fn), 0, 0)
    chains = _ring_chains_m(layout)
    assert chains, "engaged west half must emit rings"
    from auto_patch.gap_fill import _RING_ALONG_BENCH_SLOPE
    for pts, alts in chains:
        assert pts[0] == pts[-1], "loops stay closed under partial dip"
        for i in range(1, len(pts)):
            dz = abs(alts[i] - alts[i - 1])
            dd = math.hypot(pts[i][0] - pts[i - 1][0],
                            pts[i][1] - pts[i - 1][1])
            if dd < 0.1:
                continue
            assert dz <= _RING_ALONG_BENCH_SLOPE * dd + 0.75, (
                f"along-ring cliff {dz:.2f} m over {dd:.1f} m at "
                f"({pts[i][0]:.0f},{pts[i][1]:.0f})")


def test_narrow_gap_collapses_to_spine_only(rings_on):
    # 6 m across: not even the lip fits (rung 4) — no rings, and the
    # spine still emits exactly as the plain gap-fill.
    layout = _frame_layout(3.0)
    n = emit_gap_fill_spines(layout, _LOW, 0, 0)
    assert n == 1
    assert not _rings(layout)
    assert getattr(layout, "gap_spines", None)


def test_runway_axes_key_the_ring_width(rings_on):
    # A 900 m frame chord keys code 2 (band edge 40 m); the TRUE runway
    # axis is 2000 m -> code 4 (band edge 75 m).  In a 170 m gap
    # neither the cross-fraction cap (76.5) nor the opposite-side cap
    # (84) binds, so the deepest ring-2 offset reads the code source.
    length = 900.0
    layout = _frame_layout(85.0, length=length)
    axis = _FakeRunway(-600.0, 15.0, 1400.0, 15.0)   # 2000 m axis
    emit_gap_fill_spines(layout, _LOW, 0, 0, source_runways=[axis])
    bottom_edge = LineString([(0.0, 30.0), (length, 30.0)])
    offs = [bottom_edge.distance(Point(x, y))
            for pts, alts in _ring_chains_m(layout)
            for x, y in pts if y < 110.0 and 200 < x < 700]
    assert offs, "expected ring nodes off the bottom runway edge"
    assert 70.0 <= max(offs) <= 76.5, (
        f"axis code 4 must place ring 2 near 75 m, got {max(offs):.1f}")
    layout2 = _frame_layout(85.0, length=length)
    emit_gap_fill_spines(layout2, _LOW, 0, 0)
    offs2 = [bottom_edge.distance(Point(x, y))
             for pts, alts in _ring_chains_m(layout2)
             for x, y in pts if y < 110.0 and 200 < x < 700]
    assert offs2
    assert max(offs2) <= 45.0, (
        f"chord code 2 must cap ring 2 near 40 m, got {max(offs2):.1f}")


def test_ring_zero_lens_guards(rings_on):
    layout = _frame_layout(30.0)
    emit_gap_fill_spines(layout, _LOW, 0, 0)
    gap = Polygon([(30.0, 30.0), (FRAME_LENGTH - 30.0, 30.0),
                   (FRAME_LENGTH - 30.0, 90.0), (30.0, 90.0)])
    chains = _ring_chains_m(layout)
    assert chains
    spine_lines = []
    for pts_ll, vals in layout.gap_spines:
        pts = [(lon * 111320.0, lat * 111320.0) for lat, lon in pts_ll]
        if len(pts) >= 2:
            spine_lines.append(LineString(pts))
    for pts, alts in chains:
        open_pts = pts[:-1] if pts[0] == pts[-1] else pts
        for i, p in enumerate(open_pts):
            assert gap.buffer(1e-6).contains(Point(p))
            assert gap.exterior.distance(Point(p)) >= 1.4
            for sls in spine_lines:
                assert sls.distance(Point(p)) >= 1.4
            if i:
                assert math.hypot(p[0] - open_pts[i - 1][0],
                                  p[1] - open_pts[i - 1][1]) >= 1.9


def test_spine_trimmed_to_ring_core(rings_on):
    """Round-8: the spine is cut back to the ring-2 core — no emitted
    spine node may remain in the band annulus (a full-length spine
    would cross the closed loops at the gap ends)."""
    layout = _frame_layout(30.0)
    emit_gap_fill_spines(layout, _LOW, 0, 0)
    assert _rings(layout)
    gap_boundary = Polygon([(30.0, 30.0), (FRAME_LENGTH - 30.0, 30.0),
                            (FRAME_LENGTH - 30.0, 90.0),
                            (30.0, 90.0)]).exterior
    # Ring-2 offsets in this frame: 27 m off the runway long sides
    # (min(75, 0.45*60, 0.5*60-1)) and 22 m off the stub ends (the
    # letter-F taxiway band) — every surviving spine node clears the
    # SMALLER of the two and keeps 2 m off every ring chain.
    ring_chains = [LineString(pts) for pts, _a in _ring_chains_m(layout)]
    for pts_ll, vals in layout.gap_spines:
        for lat, lon in pts_ll:
            x, y = lon * 111320.0, lat * 111320.0
            assert gap_boundary.distance(Point(x, y)) > 21.4, (
                "spine node left in the band annulus after the trim")
            for rc in ring_chains:
                assert rc.distance(Point(x, y)) >= 1.9


def test_spine_recouples_to_ring_two_ceiling(rings_on):
    # With the deep dip every ring-2 station is floor-ENGAGED; the
    # surviving (trimmed) spine nodes must not exceed their governing
    # ring-2 value.  Mid-frame (away from the stub-end corners, whose
    # point-law floors are runway-governed and higher) the pin is the
    # runway floor at 27 m exactly.
    layout = _frame_layout(30.0)
    emit_gap_fill_spines(layout, _LOW, 0, 0)
    assert _rings(layout)
    lo, _hi = adjacent_ground_envelope("runway", 3, None, 27.0)
    ring2_floor = EDGE_ALT + lo
    _lo_ceiling = EDGE_ALT + adjacent_ground_envelope(
        "runway", 3, None, 27.0)[1]        # zone-2 ceiling at the edge
    checked = 0
    for pts_ll, vals in layout.gap_spines:
        for (lat, lon), v in zip(pts_ll, vals):
            x = lon * 111320.0
            # Everywhere: never above the band-edge CEILING.
            assert v <= _lo_ceiling + 0.15, (
                f"spine node above the band-edge ceiling: {v}")
            if 300.0 < x < 1000.0:
                checked += 1
                assert v <= ring2_floor + 0.15, (
                    f"mid-frame spine node above the ring-2 floor pin: "
                    f"{v} > {ring2_floor:.2f}")
    assert checked >= 5
