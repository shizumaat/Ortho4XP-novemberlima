"""Tests for the tile-wide elevation detail level core module.

Covers the lead-authored parts of ``src/O4_Elevation_Level.py``
(``docs/specs/elevation-level-spec.md``): configuration parsing, the
level-to-grid-factor mapping with its data cap, and the strip-wise
whole-tile overlay bake numerics (tile-edge feather, no-coverage
hand-back, base-nodata sentinel guard, strip equivalence).  Provider
selection and the fetch path are exercised in
``tests/test_elevation_level_providers.py``.

All headless: synthetic rasters in ``tmp_path``, no network.  The bake
tests require the GDAL python bindings and skip cleanly without them.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import numpy
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

import O4_Elevation_Level as ELEVATION_LEVEL

try:
    from osgeo import gdal

    HAS_GDAL = True
except Exception:
    HAS_GDAL = False


# ---------------------------------------------------------------------
# parse_elevation_level
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        ("auto", None),
        ("AUTO", None),
        ("", None),
        (None, None),
        ("30", 30),
        ("10", 10),
        ("5", 5),
        ("1", 1),
        (" 10 ", 10),
        (10, 10),
        (5.0, 5),
        ("7", None),  # not a defined level -> auto with a warning
        ("abc", None),
        ("10.0", 10),
    ],
)
def test_parse_elevation_level(value, expected):
    assert ELEVATION_LEVEL.parse_elevation_level(value) == expected


# ---------------------------------------------------------------------
# grid_factor_for_level (the data cap)
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "level,finest,expected",
    [
        (30, 30.0, 1),
        (10, 1.0, 3),
        (5, 1.0, 6),
        (1, 1.0, 9),
        (1, 0.5, 9),
        # Data cap: a level finer than the finest covering source falls
        # back to the factor that source warrants.
        (1, 10.0, 3),
        (5, 10.0, 3),
        (1, 5.0, 6),
        (10, 30.0, 1),
        # No wide-area source at all -> historic factor 1.
        (10, None, 1),
        (1, None, 1),
    ],
)
def test_grid_factor_for_level(level, finest, expected):
    assert ELEVATION_LEVEL.grid_factor_for_level(level, finest) == expected


def test_grid_posting_metres_matches_one_arc_second():
    posting = ELEVATION_LEVEL.grid_posting_metres(1)
    assert 30.0 < posting < 31.5
    assert ELEVATION_LEVEL.grid_posting_metres(3) == pytest.approx(
        posting / 3.0
    )


# ---------------------------------------------------------------------
# resolve_tile_overlay_plan gating (offline paths only)
# ---------------------------------------------------------------------
def _tile_stub(lat=36, lon=-87, **overrides):
    tile = SimpleNamespace(
        lat=lat,
        lon=lon,
        elevation_level="auto",
        custom_dem="",
        airport_elevation_providers="auto",
        airport_elevation_inset_feather_m=60.0,
        dem=None,
    )
    for key, value in overrides.items():
        setattr(tile, key, value)
    return tile


def test_plan_is_none_on_auto():
    assert ELEVATION_LEVEL.resolve_tile_overlay_plan(_tile_stub()) is None


def test_plan_is_none_with_custom_dem(monkeypatch):
    # The custom_dem guard fires before provider selection, so this holds
    # even while the selection function is still agent-stubbed.
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    tile = _tile_stub(elevation_level="10", custom_dem="/some/raster.tif")
    assert ELEVATION_LEVEL.resolve_tile_overlay_plan(tile) is None


def test_plan_is_none_without_gdal(monkeypatch):
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", False)
    tile = _tile_stub(elevation_level="10")
    assert ELEVATION_LEVEL.resolve_tile_overlay_plan(tile) is None


# ---------------------------------------------------------------------
# bake_tile_overlay_into_alt_dem numerics
# ---------------------------------------------------------------------
GRID_CELLS = 41  # 1 degree / 40 steps: coarse but exercises every path
NODATA = -32768.0
# Wide feather so the tile-edge ramp spans a few of the coarse cells
# (posting is ~2.8 km at 41 cells per degree) while still leaving the
# tile interior at full weight.
FEATHER_M = 10000.0


def _base_dem_stub(fill=0.0):
    alt = numpy.full((GRID_CELLS, GRID_CELLS), fill, dtype=numpy.float32)
    return SimpleNamespace(
        nxdem=GRID_CELLS,
        nydem=GRID_CELLS,
        x0=0.0,
        x1=1.0,
        y0=0.0,
        y1=1.0,
        nodata=NODATA,
        alt_dem=alt,
    )


def _write_overlay(path, lat, lon, values):
    """Write a north-up EPSG:4326 GeoTIFF covering the whole tile."""
    rows, columns = values.shape
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(str(path), columns, rows, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform(
        (lon, 1.0 / columns, 0.0, lat + 1.0, 0.0, -1.0 / rows)
    )
    dataset.SetProjection(
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,'
        '298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",'
        '0.0174532925199433],AUTHORITY["EPSG","4326"]]'
    )
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(values.astype(numpy.float32))
    band.FlushCache()
    dataset = None


def _baked_tile(tmp_path, monkeypatch, overlay_values, base_fill=0.0):
    lat, lon = 36, -87
    overlay_path = tmp_path / "overlay.tif"
    _write_overlay(overlay_path, lat, lon, overlay_values)
    tile = _tile_stub(
        lat=lat,
        lon=lon,
        elevation_level="10",
        airport_elevation_inset_feather_m=FEATHER_M,
        dem=_base_dem_stub(base_fill),
    )
    plan = {
        "definition": {"code": "TESTWIDE"},
        "factor": 3,
        "target_resolution_m": 10.29,
        "path": str(overlay_path),
    }
    monkeypatch.setattr(
        ELEVATION_LEVEL, "resolve_tile_overlay_plan", lambda _tile: plan
    )
    return tile


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_bake_constant_overlay_edge_feather(tmp_path, monkeypatch):
    overlay = numpy.full((100, 100), 100.0, dtype=numpy.float32)
    tile = _baked_tile(tmp_path, monkeypatch, overlay)
    assert ELEVATION_LEVEL.bake_tile_overlay_into_alt_dem(tile) is True
    baked = tile.dem.alt_dem
    centre = GRID_CELLS // 2
    # Deep interior takes the overlay outright.
    assert baked[centre, centre] == pytest.approx(100.0)
    # The outermost ring sits AT the tile edge: weight 0, base preserved,
    # so neighbouring tiles always agree at the border.
    assert numpy.all(baked[0, :] == 0.0)
    assert numpy.all(baked[-1, :] == 0.0)
    assert numpy.all(baked[:, 0] == 0.0)
    assert numpy.all(baked[:, -1] == 0.0)
    # The ramp into the interior is monotonic (a feather, not a cliff).
    column = baked[: centre + 1, centre]
    assert numpy.all(numpy.diff(column) >= -1e-4)
    assert column[1] < 100.0
    # Provenance stamped for downstream attribution.
    assert tile.dem.tile_overlay_provenance["provider"] == "TESTWIDE"


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_bake_no_coverage_region_hands_back_softly(tmp_path, monkeypatch):
    overlay = numpy.full((100, 100), 100.0, dtype=numpy.float32)
    overlay[:, :50] = NODATA  # western half: no coverage
    tile = _baked_tile(tmp_path, monkeypatch, overlay)
    assert ELEVATION_LEVEL.bake_tile_overlay_into_alt_dem(tile) is True
    baked = tile.dem.alt_dem
    centre = GRID_CELLS // 2
    # Deep inside the no-coverage half the base survives untouched.
    assert numpy.all(baked[:, : GRID_CELLS // 4] == 0.0)
    # Deep inside the covered half the overlay wins.
    assert baked[centre, GRID_CELLS - 8] == pytest.approx(100.0)
    # Just east of the coverage boundary the hand-back is a ramp: some
    # cell there carries a strictly intermediate value.
    boundary_band = baked[centre, centre : centre + 4]
    assert numpy.any((boundary_band > 0.0) & (boundary_band < 100.0))


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_bake_base_nodata_takes_overlay_outright(tmp_path, monkeypatch):
    overlay = numpy.full((100, 100), 100.0, dtype=numpy.float32)
    tile = _baked_tile(tmp_path, monkeypatch, overlay)
    # Base sentinel right at the tile edge, where the blend weight is 0:
    # blending against the sentinel would fabricate a huge ramp, so the
    # bake must take the overlay value outright instead.
    tile.dem.alt_dem[0, 5] = NODATA
    assert ELEVATION_LEVEL.bake_tile_overlay_into_alt_dem(tile) is True
    assert tile.dem.alt_dem[0, 5] == pytest.approx(100.0)
    # Ordinary edge cells around it still preserve the base.
    assert tile.dem.alt_dem[0, 4] == 0.0


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_bake_strip_processing_matches_single_strip(tmp_path, monkeypatch):
    rng = numpy.random.default_rng(20260715)
    overlay = rng.uniform(50.0, 150.0, size=(100, 100)).astype(numpy.float32)
    overlay[20:35, 40:70] = NODATA  # an interior no-coverage hole

    tile_single = _baked_tile(tmp_path, monkeypatch, overlay)
    assert ELEVATION_LEVEL.bake_tile_overlay_into_alt_dem(tile_single)
    single = tile_single.dem.alt_dem.copy()

    # Force many small strips (minimum strip height is 8 rows) and re-run.
    monkeypatch.setattr(ELEVATION_LEVEL, "STRIP_CELL_BUDGET", 1)
    tile_stripped = _baked_tile(tmp_path, monkeypatch, overlay)
    assert ELEVATION_LEVEL.bake_tile_overlay_into_alt_dem(tile_stripped)
    numpy.testing.assert_allclose(
        tile_stripped.dem.alt_dem, single, rtol=0, atol=1e-4
    )


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_bake_missing_overlay_file_is_a_noop(tmp_path, monkeypatch):
    tile = _tile_stub(
        elevation_level="10",
        airport_elevation_inset_feather_m=FEATHER_M,
        dem=_base_dem_stub(7.0),
    )
    plan = {
        "definition": {"code": "TESTWIDE"},
        "factor": 3,
        "target_resolution_m": 10.29,
        "path": str(tmp_path / "absent.tif"),
    }
    monkeypatch.setattr(
        ELEVATION_LEVEL, "resolve_tile_overlay_plan", lambda _tile: plan
    )
    assert ELEVATION_LEVEL.bake_tile_overlay_into_alt_dem(tile) is False
    assert numpy.all(tile.dem.alt_dem == 7.0)
