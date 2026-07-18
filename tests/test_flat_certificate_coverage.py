"""Flat-airport fast path — Tier 1 certificate coverage (WP1).

Hermetic unit tests (no fixtures, no network, ``tmp_path``-free) for the
taxi-rect and building-seat certificates added by
docs/specs/flat-airport-fast-path-spec.md §3.2:

  * a taxi RECT whose DEM is provably flat certifies (returns its seed +
    shortest axial span) and one over the axial budget / with a cross-fall /
    on a sampling gap refuses (``None``);
  * a building SEAT whose whole footprint DEM relief fits the seat tolerance
    certifies, SKIPS its reach band, and records the footprint DEM MEAN as the
    seated level through the same ``{id(shape): level}`` structure the band
    path fills — while a footprint over the relief budget, a tight reach band,
    or the gate turned off all fall back to the normal band clamp.

The certificate helpers sample the DEM through ``auto_patch.elevation.
_sample_dem`` (rects) / a caller-supplied ``dem_sampler`` (seats); both are
stubbed here so each case drives a controlled elevation field.
"""

import auto_patch.elevation as elevation_module
from auto_patch.elevation_per_surface import solver_primitives as SP
from auto_patch.elevation_per_surface.building_feasibility import (
    building_feasible_levels)
from auto_patch.config import (
    BUILDING_SEAT_FLATNESS_TOLERANCE_M, RECT_CROSS_FLATNESS_TOLERANCE_M,
    TAXI_MAX_GRADE)
from auto_patch.layout import ROLE_APRON, ROLE_BUILDING

from shapely.geometry import Polygon


# ── shared fakes ─────────────────────────────────────────────────────────────
class _FakeShape:
    def __init__(self, role, polygon):
        self.role = role
        self.polygon = polygon


class _FakeLayout:
    """Minimal layout: identity ``m_to_ll`` (so DEM samples key on the local
    metre coords), a shapes list, and an ICAO for the summary line."""

    def __init__(self, shapes, icao="TEST"):
        self.shapes = shapes
        self.icao = icao

    def m_to_ll(self, x, y):
        return (x, y)


def _install_plane_dem(monkeypatch, base, axial_rate, cross_rate):
    """Stub ``_sample_dem`` with a tilted plane ``base + axial_rate·lat +
    cross_rate·lon`` (identity ``m_to_ll`` ⇒ lat = x, lon = y)."""

    def _fake(dem, tile_lat, tile_lon, lat, lon):
        return base + axial_rate * lat + cross_rate * lon

    monkeypatch.setattr(elevation_module, "_sample_dem", _fake)


# A 100 m (axial, along x) × 20 m (cross, along y) taxi rect.
_RECT_COORDS = [(0.0, 0.0), (100.0, 0.0), (100.0, 20.0), (0.0, 20.0)]
_RECT_CROSS = [(1, 2), (3, 0)]                       # the two 20 m short edges
_RECT_AXIAL = [(0, 1, 100.0), (2, 3, 100.0)]         # the two 100 m long edges


def _rect_shape():
    return _FakeShape("primary_parallel", Polygon(_RECT_COORDS))


# ── rect certificate ─────────────────────────────────────────────────────────
def test_rect_certifies_when_flat(monkeypatch):
    # 0.5 % axial slope (< 0.6·1.5 % = 0.9 % budget) and 0.2 % cross slope
    # (0.04 m over 20 m < 0.10 m reserve): certifies.
    _install_plane_dem(monkeypatch, base=100.0, axial_rate=0.005,
                       cross_rate=0.002)
    layout = _FakeLayout([])
    result = SP._certify_flat_rect(
        layout, _rect_shape(), _RECT_COORDS, _RECT_CROSS, _RECT_AXIAL,
        TAXI_MAX_GRADE, dem=object(), tile_lat=0, tile_lon=0,
        rate_factor=0.6)
    assert result is not None
    ring_seed, minimum_axial_length = result
    assert len(ring_seed) == 4
    assert minimum_axial_length == 100.0
    # Seed values are the plane sampled at each corner (bit-identical seeds).
    assert ring_seed[0] == 100.0
    assert abs(ring_seed[1] - (100.0 + 0.005 * 100.0)) < 1e-9


def test_rect_refuses_axial_relief_over_budget(monkeypatch):
    # 1.2 % axial slope > 0.9 % budget → the axial edge check refuses.
    _install_plane_dem(monkeypatch, base=100.0, axial_rate=0.012,
                       cross_rate=0.0)
    layout = _FakeLayout([])
    result = SP._certify_flat_rect(
        layout, _rect_shape(), _RECT_COORDS, _RECT_CROSS, _RECT_AXIAL,
        TAXI_MAX_GRADE, dem=object(), tile_lat=0, tile_lon=0,
        rate_factor=0.6)
    assert result is None


def test_rect_refuses_cross_fall(monkeypatch):
    # 1 % cross slope → 0.2 m relief over 20 m > 0.10 m flat-cross reserve.
    assert 0.01 * 20.0 > RECT_CROSS_FLATNESS_TOLERANCE_M
    _install_plane_dem(monkeypatch, base=100.0, axial_rate=0.0,
                       cross_rate=0.01)
    layout = _FakeLayout([])
    result = SP._certify_flat_rect(
        layout, _rect_shape(), _RECT_COORDS, _RECT_CROSS, _RECT_AXIAL,
        TAXI_MAX_GRADE, dem=object(), tile_lat=0, tile_lon=0,
        rate_factor=0.6)
    assert result is None


def test_rect_refuses_on_sampling_gap(monkeypatch):
    # Any DEM gap (None) refuses — fail toward correctness.
    monkeypatch.setattr(elevation_module, "_sample_dem",
                        lambda *a, **k: None)
    layout = _FakeLayout([])
    result = SP._certify_flat_rect(
        layout, _rect_shape(), _RECT_COORDS, _RECT_CROSS, _RECT_AXIAL,
        TAXI_MAX_GRADE, dem=object(), tile_lat=0, tile_lon=0,
        rate_factor=0.6)
    assert result is None


# ── seat certificate (building_feasible_levels) ──────────────────────────────
_APRON = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])       # 10 000 m²
_BUILDING = Polygon([(40, 101), (70, 101), (70, 131), (40, 131)])  # 900 m², 1 m off
_HIGH_CORNER = (70.0, 131.0)


def _seat_layout():
    building = _FakeShape(ROLE_BUILDING, _BUILDING)
    layout = _FakeLayout([_FakeShape(ROLE_APRON, _APRON), building])
    return layout, building


def _flat_footprint_sampler():
    """DEM 100.0 everywhere on the footprint except one raised corner, so the
    footprint MEAN (100.041667) differs from the centroid sample (100.0) — this
    is how a certified seat (records the mean) is told apart from the normal
    band clamp (records the centroid-derived level)."""
    def _sampler(x, y):
        return 100.25 if (x, y) == _HIGH_CORNER else 100.0
    return _sampler


_EXPECTED_MEAN = (100.0 * 5 + 100.25) / 6      # ring(5, closed) + centroid


def _wide_band(x, y):
    return (0.0, 1000.0)


def test_seat_certifies_records_dem_mean_and_skips_band(monkeypatch):
    monkeypatch.delenv("O4_FLAT_CERTIFICATE_COVERAGE", raising=False)
    layout, building = _seat_layout()
    band_calls = []

    def _counting_band(x, y):
        band_calls.append((x, y))
        return (0.0, 1000.0)

    out = building_feasible_levels(layout, [], _flat_footprint_sampler(),
                                   band=_counting_band)
    assert id(building) in out
    assert abs(out[id(building)] - _EXPECTED_MEAN) < 1e-9
    # SKIP proof: the certified seat consults the band exactly once (the
    # soundness guard) — never the per-frontage / clamp queries the normal
    # path would issue.
    assert len(band_calls) == 1
    counts = layout._flat_certificate_counts["seat"]
    assert counts["certified"] == 1
    assert counts["refused"] == 0


def test_seat_refuses_when_footprint_not_flat(monkeypatch):
    monkeypatch.delenv("O4_FLAT_CERTIFICATE_COVERAGE", raising=False)
    layout, building = _seat_layout()

    def _bumpy(x, y):
        return 101.0 if (x, y) == _HIGH_CORNER else 100.0   # 1 m relief > 0.30

    out = building_feasible_levels(layout, [], _bumpy, band=_wide_band)
    # Normal band clamp: DEM at centroid (100.0) inside the wide band.
    assert abs(out[id(building)] - 100.0) < 1e-9
    counts = layout._flat_certificate_counts["seat"]
    assert counts["certified"] == 0
    assert counts["refused"] == 1


def test_seat_refuses_when_band_too_tight(monkeypatch):
    monkeypatch.delenv("O4_FLAT_CERTIFICATE_COVERAGE", raising=False)
    layout, building = _seat_layout()

    def _tight_band(x, y):
        return (100.0, 100.1)          # width 0.1 m < footprint reach margin

    out = building_feasible_levels(layout, [], _flat_footprint_sampler(),
                                   band=_tight_band)
    # DEM 100.0 clamps up to the tight floor (still not the DEM mean).
    assert abs(out[id(building)] - 100.0) < 1e-9
    counts = layout._flat_certificate_counts["seat"]
    assert counts["certified"] == 0
    assert counts["refused"] == 1


def test_seat_gate_off_uses_band(monkeypatch):
    monkeypatch.setenv("O4_FLAT_CERTIFICATE_COVERAGE", "0")
    layout, building = _seat_layout()
    out = building_feasible_levels(layout, [], _flat_footprint_sampler(),
                                   band=_wide_band)
    # Gate OFF: normal band clamp records the centroid-derived level, NOT the
    # footprint mean, and nothing is certified.
    assert abs(out[id(building)] - 100.0) < 1e-9
    counts = getattr(layout, "_flat_certificate_counts", None)
    assert counts is None or counts["seat"]["certified"] == 0


def test_seat_tolerance_boundary():
    # Documents the tolerance the seat certificate is wired to.
    assert BUILDING_SEAT_FLATNESS_TOLERANCE_M == 0.30
