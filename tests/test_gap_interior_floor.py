"""Enclosed-pocket interior depth floor (owner ruling 2026-07-19).

Synthetic fixtures only (no airport build), mirroring
``test_gap_fill_spine``'s frame pattern: a rectangular pavement frame
encloses one hole; a fake DEM plants a pit inside it.
``emit_gap_interior_floor`` must clamp the pit to (lip − depth) with a
flat ``gap_pit_floor`` patch, and must emit NOTHING when terrain is
lawful (no-op economy), when the pass is disabled, or when the pocket
is already covered by an emitted gap face.
"""
import math

import pytest
from shapely.geometry import Point, Polygon

from auto_patch import gap_fill as GF
from auto_patch import elevation as ELEV
from auto_patch.gap_fill import emit_gap_interior_floor, _GAP_PIT_FLOOR_REF
from auto_patch.layout import BuiltShape, ROLE_GRADED_STRIP, ROLE_RUNWAY

EDGE_ALT = 100.0
FLOOR_DEPTH = 2.5          # config default the tests assume
PIT_CENTER = (300.0, 150.0)
PIT_RADIUS = 60.0
PIT_ALT = EDGE_ALT - 8.0   # far below the floor


class _FakeLayout:
    def m_to_ll(self, x, y):
        return (y / 111320.0, x / 111320.0)

    def __init__(self, shapes):
        self.shapes = shapes
        self.airport_boundary = None
        self.anchor = (0.0, 0.0)


def _rect(x0, y0, x1, y1, role=ROLE_RUNWAY):
    poly = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    coords = list(poly.exterior.coords)
    return BuiltShape(polygon=poly, role=role,
                      node_altitudes=[EDGE_ALT] * len(coords))


def _frame_layout():
    """Pavement frame around the hole (60, 60)–(540, 240)."""
    return _FakeLayout([
        _rect(0, 0, 600, 60),        # south
        _rect(0, 240, 600, 300),     # north
        _rect(0, 60, 60, 240),       # west
        _rect(540, 60, 600, 240),    # east
    ])


def _fake_sample_dem_with_pit(dem, tile_lat, tile_lon, lat, lon):
    x = lon * 111320.0
    y = lat * 111320.0
    if math.hypot(x - PIT_CENTER[0], y - PIT_CENTER[1]) <= PIT_RADIUS:
        return PIT_ALT
    return EDGE_ALT - 0.5     # lawful gentle terrain


def _fake_sample_dem_flat(dem, tile_lat, tile_lon, lat, lon):
    return EDGE_ALT - 0.5


def _pit_patches(layout):
    return [s for s in layout.shapes
            if getattr(s, "ref", None) == _GAP_PIT_FLOOR_REF]


def test_pit_is_clamped_to_floor(monkeypatch):
    monkeypatch.setattr(ELEV, "_sample_dem", _fake_sample_dem_with_pit)
    layout = _frame_layout()
    n = emit_gap_interior_floor(layout, dem=object(),
                                tile_lat=0, tile_lon=0)
    patches = _pit_patches(layout)
    assert n == len(patches) >= 1
    floor = EDGE_ALT - FLOOR_DEPTH
    covered = any(p.polygon.contains(Point(*PIT_CENTER)) for p in patches)
    assert covered, "the pit center must be inside a floor patch"
    for p in patches:
        assert p.role == ROLE_GRADED_STRIP
        assert all(abs(a - floor) < 0.05 for a in p.node_altitudes)


def test_lawful_terrain_emits_nothing(monkeypatch):
    monkeypatch.setattr(ELEV, "_sample_dem", _fake_sample_dem_flat)
    layout = _frame_layout()
    assert emit_gap_interior_floor(layout, dem=object(),
                                   tile_lat=0, tile_lon=0) == 0
    assert not _pit_patches(layout)


def test_depth_zero_disables(monkeypatch):
    monkeypatch.setattr(ELEV, "_sample_dem", _fake_sample_dem_with_pit)
    monkeypatch.setattr(GF, "GAP_FILL_INTERIOR_FLOOR_DEPTH_M", 0.0)
    layout = _frame_layout()
    assert emit_gap_interior_floor(layout, dem=object(),
                                   tile_lat=0, tile_lon=0) == 0


def test_covered_pocket_is_skipped(monkeypatch):
    """A pocket already covered by an emitted gap face (a graded_strip
    over the whole hole) must not receive pit patches."""
    monkeypatch.setattr(ELEV, "_sample_dem", _fake_sample_dem_with_pit)
    layout = _frame_layout()
    hole = Polygon([(60, 60), (540, 60), (540, 240), (60, 240)])
    coords = list(hole.exterior.coords)
    layout.shapes.append(BuiltShape(
        polygon=hole, role=ROLE_GRADED_STRIP, ref="gap_fill_spine",
        node_altitudes=[EDGE_ALT] * len(coords)))
    assert emit_gap_interior_floor(layout, dem=object(),
                                   tile_lat=0, tile_lon=0) == 0
