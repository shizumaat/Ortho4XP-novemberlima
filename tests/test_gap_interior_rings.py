"""GAP INTERIOR RINGS (ratified design 2026-07-11, gate
O4_GAP_FILL_INTERIOR_RINGS, default OFF, chained under O4_GAP_FILL_SPINE).

Synthetic fixtures only (the test_gap_fill_spine frame pattern): a
rectangular pavement frame enclosing one hole, plus a stub DEM whose
``alt`` drives the violation trigger.

Pins:
  * gate OFF (the default) emits no rings and leaves the plain gap-fill
    path untouched;
  * ring gate ON + spine gate OFF is a HARD configuration error
    (RuntimeError — the fail-loudly chaining);
  * a violating gap emits floor-PINNED rings: every violating ring node
    value equals the solved pavement edge altitude plus the envelope
    floor offset at the node's emitted offset distance (ratified
    answer 2 — exterior fill-band parity, fill exactly TO the floor);
  * a compliant gap (terrain at/above the band floor) emits NO rings
    and its spine values are byte-equal to the gate-OFF run (the
    violation-driven trigger);
  * a partially violating gap tapers: the ring lands AT terrain past
    the violating run (benched taper-out, ratified answer 3), never a
    hard appear/disappear;
  * a narrow gap collapses (ladder rung 4): no rings, today's
    spine-only behavior;
  * runway-bounded ring widths key the TRUE ICAO code from the runway
    AXES (ratified answer 1), not the tile-cut segment chord;
  * zero-lens guards: every ring node inside the gap, >= 1.4 m off the
    boundary and the spine, consecutive nodes >= 2 m apart;
  * spine re-coupling: where a violating ring stands, spine nodes
    INSIDE the ring-2 core never exceed the ring-2 floor (the ring is
    the spine's ceiling there).
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


def _frame_layout(gap_half_width_m, length=1300.0):
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


class _FakeRunway:
    """apt.dat runway row stand-in: just the two threshold lat/lons."""
    def __init__(self, x_a, y_a, x_b, y_b):
        self.lat_a, self.lon_a = y_a / 111320.0, x_a / 111320.0
        self.lat_b, self.lon_b = y_b / 111320.0, x_b / 111320.0


def _rings(layout):
    return getattr(layout, "gap_interior_rings", None) or []


def _ring_nodes_m(layout):
    out = []
    for pts_ll, alts in _rings(layout):
        for (lat, lon), a in zip(pts_ll, alts):
            out.append((lon * 111320.0, lat * 111320.0, a))
    return out


def _gap_polygon(layout, gap_half_width_m, length=1300.0):
    return Polygon([(30.0, 30.0), (length - 30.0, 30.0),
                    (length - 30.0, 30.0 + 2 * gap_half_width_m),
                    (30.0, 30.0 + 2 * gap_half_width_m)])


_LOW = _StubDem(lambda x, y: 90.0)          # deep dip everywhere
_HIGH = _StubDem(lambda x, y: 99.9)         # compliant terrain


@pytest.fixture
def rings_on(monkeypatch):
    monkeypatch.setattr(GF, "GAP_FILL_INTERIOR_RINGS_ENABLED", True)


def test_gate_off_emits_no_rings():
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


def test_violating_gap_emits_floor_pinned_rings(rings_on):
    layout = _frame_layout(30.0)
    n = emit_gap_fill_spines(layout, _LOW, 0, 0)
    assert n == 1
    nodes = _ring_nodes_m(layout)
    assert nodes, "a fully violating gap must emit interior rings"
    # Floor-pin encoding (ratified answer 2): every violating node
    # (value above terrain — the ring holding ground UP) carries
    # EXACTLY the point-law floor: for the node's TWO nearest parents,
    # each parent's envelope floor at the node's TRUE distance to that
    # parent, combined as max(floors) — independently recomputed here.
    from auto_patch.layout import taxi_shape_code_letter
    pav = []
    for s in layout.shapes:
        if s.role == ROLE_RUNWAY:          # 1300 m chord -> code 3
            pav.append((s.polygon.exterior, ("runway", 3, None)))
        elif s.role == ROLE_STUB:          # letter from shape geometry
            letter = taxi_shape_code_letter(layout, s)
            pav.append((s.polygon.exterior, ("stub", None, letter)))
    checked = 0
    for x, y, v in nodes:
        if v <= 90.0 + 0.2:
            continue                       # taper landing at terrain
        dists = sorted((ext.distance(Point(x, y)), key)
                       for ext, key in pav)
        floors = []
        for d, (role, cn, cl) in dists[:2]:
            lo, _hi = adjacent_ground_envelope(role, cn, cl, d)
            if lo is not None:
                floors.append(EDGE_ALT + lo)
        assert floors, f"node at ({x:.0f},{y:.0f}) has no governing floor"
        expected = max(floors)
        assert abs(v - expected) <= 0.05, (
            f"ring node at ({x:.0f},{y:.0f}) value={v} is not the "
            f"point-law floor pin {expected:.2f}")
        checked += 1
    assert checked >= 10


def test_compliant_gap_emits_no_rings_and_identical_spine(rings_on):
    layout_on = _frame_layout(30.0)
    emit_gap_fill_spines(layout_on, _HIGH, 0, 0)
    assert not _rings(layout_on)
    # Trigger philosophy: where terrain complies the face stays
    # boundary + spine exactly as today.
    layout_off = _frame_layout(30.0)
    orig_gate = GF.GAP_FILL_INTERIOR_RINGS_ENABLED
    GF.GAP_FILL_INTERIOR_RINGS_ENABLED = False
    try:
        emit_gap_fill_spines(layout_off, _HIGH, 0, 0)
    finally:
        GF.GAP_FILL_INTERIOR_RINGS_ENABLED = orig_gate
    assert layout_on.gap_spines == layout_off.gap_spines


def test_partial_violation_tapers_to_terrain(rings_on):
    # Violating only for x < 500: the ring must run there, bridge no
    # more than briefly, and LAND at terrain past the run end.
    dem = _StubDem(lambda x, y: 90.0 if x < 500.0 else 99.9)
    layout = _frame_layout(30.0)
    emit_gap_fill_spines(layout, dem, 0, 0)
    nodes = _ring_nodes_m(layout)
    assert nodes
    xs = [x for x, y, v in nodes]
    assert min(xs) < 500.0
    # No ring wanders deep into the compliant east half (landing
    # stations reach at most a couple of station steps past the edge).
    assert max(xs) < 500.0 + 3.0 * GF.GAP_FILL_SPINE_STEP_M
    # Taper-out: the eastmost node of each chain sits AT terrain
    # (99.9), not at the floor pin (~99.1) — the daylight landing.
    for pts_ll, alts in _rings(layout):
        pts = [(lon * 111320.0, lat * 111320.0)
               for lat, lon in pts_ll]
        east_i = max(range(len(pts)), key=lambda i: pts[i][0])
        if pts[east_i][0] > 500.0:
            assert alts[east_i] > 99.5, (
                "run end must land at terrain, not stay pinned")


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
    # axis is 2000 m -> code 4 (band edge 75 m).  In a 170 m gap the
    # cross-fraction cap (0.45 * 170 = 76.5) binds neither, so the
    # emitted ring-2 offset distance directly reads the code source.
    length = 900.0
    layout = _frame_layout(85.0, length=length)
    axis = _FakeRunway(-600.0, 15.0, 1400.0, 15.0)   # 2000 m axis
    emit_gap_fill_spines(layout, _LOW, 0, 0, source_runways=[axis])
    nodes = _ring_nodes_m(layout)
    assert nodes
    # The gap spans y in [30, 230]; the spine rides mid-gap (~y=115 of
    # the 170 m hole).  Ring 2 off the BOTTOM edge sits at y = 30 + w2:
    # 105 for code 4 (75 m), 70 for code 2 (40 m) — filter below the
    # spine and away from the stub ends.
    bottom_edge = LineString([(0.0, 30.0), (length, 30.0)])
    offs = sorted(bottom_edge.distance(Point(x, y))
                  for x, y, v in nodes if y < 110.0 and 200 < x < 700)
    assert offs, "expected ring nodes off the bottom runway edge"
    deepest = offs[-1]
    assert 70.0 <= deepest <= 76.5, (
        f"axis code 4 must place ring 2 near 75 m, got {deepest:.1f}")
    # Chord fallback control: without axes the same frame keys code 2.
    layout2 = _frame_layout(85.0, length=length)
    emit_gap_fill_spines(layout2, _LOW, 0, 0)
    offs2 = sorted(bottom_edge.distance(Point(x, y))
                   for x, y, v in _ring_nodes_m(layout2)
                   if y < 110.0 and 200 < x < 700)
    assert offs2
    assert offs2[-1] <= 45.0, (
        f"chord code 2 must cap ring 2 near 40 m, got {offs2[-1]:.1f}")


def test_ring_zero_lens_guards(rings_on):
    layout = _frame_layout(30.0)
    emit_gap_fill_spines(layout, _LOW, 0, 0)
    gap = _gap_polygon(layout, 30.0)
    chains = []
    for pts_ll, alts in _rings(layout):
        chains.append([(lon * 111320.0, lat * 111320.0)
                       for lat, lon in pts_ll])
    assert chains
    spine_pts = [( lon * 111320.0, lat * 111320.0)
                 for lat, lon in layout.gap_spines[0][0]]
    spine_ls = LineString(spine_pts)
    for pts in chains:
        open_pts = pts[:-1] if pts[0] == pts[-1] else pts
        for i, p in enumerate(open_pts):
            assert gap.buffer(1e-6).contains(Point(p))
            assert gap.exterior.distance(Point(p)) >= 1.4
            assert spine_ls.distance(Point(p)) >= 1.4
            if i:
                assert math.hypot(p[0] - open_pts[i - 1][0],
                                  p[1] - open_pts[i - 1][1]) >= 1.9


def test_spine_recouples_to_ring_two_ceiling(rings_on):
    # With a deep dip the analytic spine target sits ABOVE the ring-2
    # floor (the drain target hangs a quarter below the ceiling); the
    # re-coupling must cap every spine node inside the ring-2 core at
    # the ring-2 floor.
    layout = _frame_layout(30.0)
    emit_gap_fill_spines(layout, _LOW, 0, 0)
    assert _rings(layout)
    # ring-2 floor for this frame: nearest parent is a code-3 runway
    # (1300 m chord), cross width 60 -> ring-2 offset 27 m.
    lo, _hi = adjacent_ground_envelope("runway", 3, None, 27.0)
    ring2_floor = EDGE_ALT + lo
    pts_ll, vals = layout.gap_spines[0]
    gap = _gap_polygon(layout, 30.0)
    for (lat, lon), v in zip(pts_ll, vals):
        x, y = lon * 111320.0, lat * 111320.0
        if gap.exterior.distance(Point(x, y)) > 27.5:
            assert v <= ring2_floor + 0.1, (
                f"spine node inside the ring-2 core above the ring "
                f"ceiling: {v} > {ring2_floor:.2f}")
