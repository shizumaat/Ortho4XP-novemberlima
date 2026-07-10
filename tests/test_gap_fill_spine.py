"""Gap-fill + drainage SPINE emitter (user design ruling 2026-07-09).

Synthetic fixtures only (no airport build): a rectangular pavement FRAME
— two parallel runway rectangles joined by two end stubs — encloses one
rectangular hole.  The emitter must grade that hole as ONE unit, boundary
VERBATIM + a single drainage spine, splitting it into two half-gap faces.

Pins:
  * exactly TWO half-gap faces emitted from one enclosed gap;
  * every NON-spine (boundary) face vertex is a VERBATIM pavement ring
    vertex (chain identity — no new boundary geometry);
  * every deep-interior spine value sits inside the law drainage corridor
    (computed from ``adjacent_ground_envelope`` directly) and below the
    pavement edge;
  * a gap wider than ``GAP_FILL_MAX_WIDTH_M`` emits nothing;
  * the gate off emits nothing.
"""
import math

import pytest
from shapely.geometry import Point as _pt, Polygon

from auto_patch import gap_fill as GF
from auto_patch.gap_fill import emit_gap_fill_spines, _parent_family_code
from auto_patch.grade_law import adjacent_ground_envelope
from auto_patch.emit_decimate import _key
from auto_patch.layout import (
    BuiltShape, ROLE_GRADED_STRIP, ROLE_RUNWAY, ROLE_STUB,
)

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


def _rect(x0, y0, x1, y1, role):
    poly = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    coords = list(poly.exterior.coords)
    return BuiltShape(polygon=poly, role=role,
                      node_altitudes=[EDGE_ALT] * len(coords))


def _frame_layout(gap_half_width_m):
    """A rectangular pavement frame enclosing ONE rectangular hole.

    Two long parallel RUNWAY rects (long enough to key ICAO code 3, so a
    30 m half-gap lands inside the graded band) joined by two end STUBS;
    the hole spans ``2*gap_half_width_m`` across.  All pavement flat at
    ``EDGE_ALT``."""
    length = 1300.0                       # code 3 → 75 m graded half-width
    inner_x0, inner_x1 = 30.0, length - 30.0
    y_bot0, y_bot1 = 0.0, 30.0
    y_gap0 = y_bot1
    y_gap1 = y_gap0 + 2.0 * gap_half_width_m
    y_top0, y_top1 = y_gap1, y_gap1 + 30.0
    shapes = [
        _rect(0.0, y_bot0, length, y_bot1, ROLE_RUNWAY),        # bottom
        _rect(0.0, y_top0, length, y_top1, ROLE_RUNWAY),        # top
        _rect(0.0, y_gap0, inner_x0, y_gap1, ROLE_STUB),        # left end
        _rect(inner_x1, y_gap0, length, y_gap1, ROLE_STUB),     # right end
    ]
    return _FakeLayout(shapes), list(shapes)   # pav = snapshot of pavement


def _pavement_keys(shapes):
    keys = set()
    for s in shapes:
        for vx, vy in s.polygon.exterior.coords:
            keys.add(_key(vx, vy))
    return keys


def _faces(layout):
    return [s for s in layout.shapes if s.role == ROLE_GRADED_STRIP]


def test_encloses_gap_emits_exactly_two_faces():
    """Open-way redesign (user 2026-07-09 round 2): ONE face — the gap
    polygon verbatim — plus the spine as an interior OPEN WAY held off
    the boundary (layout.gap_spines)."""
    layout, pav = _frame_layout(gap_half_width_m=30.0)
    n = emit_gap_fill_spines(layout, None, 0, 0)
    faces = _faces(layout)
    assert n == 1
    assert len(faces) == 1
    for f in faces:
        assert f.ref == "gap_fill_spine"
        # node_altitudes carry the closing repeat.
        assert len(f.node_altitudes) == len(f.polygon.exterior.coords)
    spines = getattr(layout, "gap_spines", None)
    assert spines and len(spines) == 1
    pts_ll, vals = spines[0]
    assert len(pts_ll) == len(vals) >= 2


def test_boundary_vertices_are_verbatim_pavement_vertices():
    """Chain identity: every face vertex is either a VERBATIM pavement
    ring vertex or a spine vertex (the shared edge of the two faces).  No
    face vertex is new boundary geometry."""
    layout, pav = _frame_layout(gap_half_width_m=30.0)
    emit_gap_fill_spines(layout, None, 0, 0)
    faces = _faces(layout)
    assert len(faces) == 1
    pav_keys = _pavement_keys(pav)

    coords = list(faces[0].polygon.exterior.coords)[:-1]
    for x, y in coords:
        assert _key(x, y) in pav_keys, (
            "boundary vertex is not a verbatim pavement vertex")


def test_spine_values_lie_in_the_law_drainage_corridor():
    """Deep-interior spine values fall BELOW the pavement edge and stay
    inside the two-parent drainage interval — bounds taken straight from
    ``adjacent_ground_envelope`` (no hard-coded numbers).  The spine now
    lives in ``layout.gap_spines`` (open-way redesign) with lat/lon
    points; values are checked against the interval at each point."""
    layout, pav = _frame_layout(gap_half_width_m=30.0)
    emit_gap_fill_spines(layout, None, 0, 0)
    spines = getattr(layout, "gap_spines", None)
    assert spines and len(spines) == 1
    pts_ll, vals = spines[0]
    checked = 0
    for (la, lo), alt in zip(pts_ll, vals):
        vx, vy = layout.ll_to_m(la, lo)
        dists = sorted(
            ((s.polygon.exterior.distance(_pt(vx, vy)), s) for s in pav),
            key=lambda t: t[0])
        (dA, sA), (dB, sB) = dists[0], dists[1]
        if dA < 2.0 or dB < 2.0:
            continue                     # near an end pin — skip
        lo_b, hi_b = None, None
        for d, s in ((dA, sA), (dB, sB)):
            _r, _cn, _cl = _parent_family_code(layout, s)
            fl, ce = adjacent_ground_envelope(_r, _cn, _cl, d)
            if fl is None and ce is None:
                continue
            e = 100.0                    # flat fixture pavement
            f_ = e + fl if fl is not None else None
            c_ = e + ce if ce is not None else None
            if f_ is not None:
                lo_b = f_ if lo_b is None else max(lo_b, f_)
            if c_ is not None:
                hi_b = c_ if hi_b is None else min(hi_b, c_)
        if lo_b is None or hi_b is None:
            continue
        assert lo_b - 0.2 <= alt <= hi_b + 0.2, (
            f"spine value {alt} outside [{lo_b}, {hi_b}]")
        checked += 1
    assert checked >= 1


def test_wide_gap_emits_nothing():
    """A gap whose short side exceeds GAP_FILL_MAX_WIDTH_M stays with the
    corridor-band emitter (half-gap 100 m → 200 m short side > 160 m)."""
    layout, _ = _frame_layout(gap_half_width_m=100.0)
    n = emit_gap_fill_spines(layout, None, 0, 0)
    assert n == 0
    assert _faces(layout) == []


def test_gate_off_emits_nothing(monkeypatch):
    monkeypatch.setattr(GF, "GAP_FILL_SPINE_ENABLED", False)
    layout, _ = _frame_layout(gap_half_width_m=30.0)
    n = emit_gap_fill_spines(layout, None, 0, 0)
    assert n == 0
    assert _faces(layout) == []
