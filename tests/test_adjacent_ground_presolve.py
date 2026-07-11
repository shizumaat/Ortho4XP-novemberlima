"""Slice B stage B3 ORDER 1 — adjacent-ground band CONSTRUCTION MOVE.

The construction move stages the band FOOTPRINT march PRE-SOLVE
(``construct_adjacent_ground_presolve``) and the post-solve emitter
CONSUMES the frozen footprints instead of re-marching, while still valuing
every vertex analytically off the (unchanged here) solved altitudes.

PARITY (the order's item 5): on a synthetic layout with NO late features and
node altitudes already set (so the DEM-seeded pre-solve estimate EQUALS the
solved edge references), the gate-ON pre-built footprints must be identical to
the gate-OFF inline-marched footprints — same polygons, same values.  Any
delta in production comes only from seed-vs-solved edge altitudes and
post-solve-only clip features, none of which exist in this hermetic fixture.
"""
import pytest
from shapely.geometry import Polygon

import auto_patch.config as cfg
from auto_patch import adjacent_ground as AG
from auto_patch.layout import BuiltShape, ROLE_APRON, ROLE_GRADED_STRIP

EDGE_ALT = 100.0
DEM_RISE = 6.0                                # terrain 6 m above the edge


class _FakeLayout:
    def m_to_ll(self, x, y):
        return (y / 111320.0, x / 111320.0)

    def __init__(self, shapes):
        self.shapes = shapes
        self.airport_boundary = None
        self.anchor = (0.0, 0.0)


def _apron(x0, y0, x1, y1):
    poly = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    coords = list(poly.exterior.coords)
    return BuiltShape(polygon=poly, role=ROLE_APRON,
                      ref="apron",
                      node_altitudes=[EDGE_ALT] * len(coords))


def _mk_layout():
    # One isolated apron; every edge faces terrain (no neighbour to cover the
    # outward probe), and the flat 6 m-high surround triggers CUT bands on the
    # whole perimeter.
    return _FakeLayout([_apron(0.0, 0.0, 200.0, 80.0)])


class _FakeRunway:
    # Far-away runway so ``rw_axes`` is non-empty (the emitter takes its local
    # LineString import path, as in production) without affecting the APRON
    # family (which ignores runway axes).
    lat_a, lon_a = 0.1, 0.1
    lat_b, lon_b = 0.1, 0.11


_RUNWAYS = [_FakeRunway()]


@pytest.fixture(autouse=True)
def _synthetic_dem(monkeypatch):
    # Deterministic constant surface 6 m above the pavement everywhere — the
    # emitter and constructor both sample it through ``AG._sample_dem``.
    monkeypatch.setattr(AG, "_sample_dem",
                        lambda dem, tl, tn, lat, lon: EDGE_ALT + DEM_RISE)
    yield


def _emitted_footprints(layout):
    """The emitted ``graded_strip`` polygons as (rounded WKT, alt tuple)."""
    out = []
    for s in layout.shapes:
        if s.role != ROLE_GRADED_STRIP:
            continue
        out.append((
            s.polygon.wkt,
            tuple(round(a, 3) for a in (s.node_altitudes or [])),
        ))
    return sorted(out)


def test_construct_stages_raw_bands():
    layout = _mk_layout()
    n = AG.construct_adjacent_ground_presolve(
        layout, dem=object(), tile_lat=0, tile_lon=0, source_runways=_RUNWAYS)
    assert n >= 1
    store = layout.adjacent_ground_presolve
    assert store and store[0]["shape"] is layout.shapes[0]
    # Raw cut bands were marched for the rising surround.
    assert store[0]["cut"], "constant 6 m rise must produce raw cut bands"


def test_gate_on_footprints_equal_gate_off(monkeypatch):
    # ── Path A: gate-OFF inline march ──
    layout_off = _mk_layout()
    n_off = AG.emit_adjacent_ground_bands(
        layout_off, dem=object(), tile_lat=0, tile_lon=0, source_runways=_RUNWAYS)
    foot_off = _emitted_footprints(layout_off)
    assert n_off >= 1 and foot_off

    # ── Path B: gate-ON, consume pre-built footprints ──
    layout_on = _mk_layout()
    AG.construct_adjacent_ground_presolve(
        layout_on, dem=object(), tile_lat=0, tile_lon=0, source_runways=_RUNWAYS)
    assert getattr(layout_on, "adjacent_ground_presolve", None)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", True)
    n_on = AG.emit_adjacent_ground_bands(
        layout_on, dem=object(), tile_lat=0, tile_lon=0, source_runways=_RUNWAYS)
    foot_on = _emitted_footprints(layout_on)

    # Seed == solved and no late features → byte-for-byte identical footprints.
    assert n_on == n_off
    assert foot_on == foot_off


def test_gate_on_without_store_falls_back_to_inline(monkeypatch):
    # Gate flags on but NO pre-solve store (construct never ran) → the emitter
    # marches inline exactly as gate-OFF (the ``_presolve_bands is None`` path).
    layout_ref = _mk_layout()
    n_ref = AG.emit_adjacent_ground_bands(
        layout_ref, dem=object(), tile_lat=0, tile_lon=0, source_runways=_RUNWAYS)

    layout = _mk_layout()
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", True)
    n = AG.emit_adjacent_ground_bands(
        layout, dem=object(), tile_lat=0, tile_lon=0, source_runways=_RUNWAYS)
    assert n == n_ref
    assert _emitted_footprints(layout) == _emitted_footprints(layout_ref)
