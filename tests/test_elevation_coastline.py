"""Tests for the "Auto + coastline" elevation band (spec section 3.4).

Covers the agent-authored coastline portion of
``src/O4_Elevation_Level.py`` (``ensure_coastline_band``,
``resolve_coastline_band_plan``, ``coastline_grid_factor``) and the
``resolve_working_grid_factor`` coastline branch in
``src/O4_Airport_Elevation_Insets.py``.

All headless: the OpenStreetMap coastline download and the geometry
conversion are monkeypatched to return synthetic shapely geometry (in the
tile-relative degree frame ``OSM_to_MultiLineString`` produces), provider
selection returns a synthetic definition, and ``fetch_inset`` is faked to
write tiny GeoTIFFs.  No network; every cache path is routed into
``tmp_path`` by monkeypatching ``FNAMES.Elevation_dir``.
"""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest
from shapely import geometry

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

import O4_Airport_Elevation_Insets as INSETS
import O4_Elevation_Level as ELEVATION_LEVEL
import O4_File_Names as FNAMES

try:
    from osgeo import gdal

    HAS_GDAL = True
except Exception:  # pragma: no cover - GDAL is required for the band
    HAS_GDAL = False


TILE_LAT = 36
TILE_LON = -87
CELL_DEGREES = ELEVATION_LEVEL.COASTLINE_CELL_DEGREES
NODATA = -32768.0

NEAR_RESOLUTION_M = round(ELEVATION_LEVEL.grid_posting_metres(3), 2)  # 10.31
MID_RESOLUTION_M = float(ELEVATION_LEVEL.COASTLINE_MID_RESOLUTION_M)  # 20.0
FAR_RESOLUTION_M = round(ELEVATION_LEVEL.grid_posting_metres(1), 2)  # 30.92


# ---------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------
def _coastline_tile(**overrides):
    """A tile stub in "Auto + coastline" mode."""
    tile = SimpleNamespace(
        lat=TILE_LAT,
        lon=TILE_LON,
        elevation_level="coastline",
        custom_dem="",
        airport_elevation_providers="auto",
        elevation_coastline_band_km=5.0,
    )
    for key, value in overrides.items():
        setattr(tile, key, value)
    return tile


def _install_coastline(monkeypatch, coastline_geometry):
    """Route the OSM coastline path to a synthetic tile-relative geometry.

    ``OSM_to_MultiLineString`` returns coordinates as tile-relative degree
    offsets ``(longitude - lon, latitude - lat)`` (each roughly 0..1), so
    the fixtures are expressed in that same frame.
    """
    monkeypatch.setattr(
        "O4_OSM_Utils.OSM_queries_to_OSM_layer",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "O4_OSM_Utils.OSM_to_MultiLineString",
        lambda *args, **kwargs: coastline_geometry,
    )


def _install_provider(monkeypatch, code="TESTLIDAR"):
    """Make provider selection return a synthetic wide-area definition."""
    definition = {"code": code}
    monkeypatch.setattr(
        ELEVATION_LEVEL,
        "select_tile_overlay_definition",
        lambda lat, lon, level, providers_config="auto": definition,
    )
    return definition


def _write_cell_geotiff(
    path, west, south, east, north, value=100.0, pixels=4, array=None
):
    """Write a tiny north-up EPSG:4326 GeoTIFF covering a cell bounding box.

    By default the raster carries ``value`` plus a small per-pixel ramp:
    genuine warped lidar is never bit-for-bit constant, and a constant
    synthetic cell would (rightly) trip the constant-value plausibility
    guard in ``ensure_coastline_band``.  Pass ``array`` to control the
    samples exactly (the guard's own regression tests do).
    """
    import numpy

    if array is None:
        ramp = 0.01 * numpy.arange(
            pixels * pixels, dtype="float32"
        ).reshape(pixels, pixels)
        array = numpy.full((pixels, pixels), value, dtype="float32") + ramp
    else:
        array = numpy.asarray(array, dtype="float32")
        pixels = array.shape[0]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(
        str(path), array.shape[1], array.shape[0], 1, gdal.GDT_Float32
    )
    dataset.SetGeoTransform(
        (
            west,
            (east - west) / array.shape[1],
            0.0,
            north,
            0.0,
            -(north - south) / array.shape[0],
        )
    )
    dataset.SetProjection(
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,'
        '298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",'
        '0.0174532925199433],AUTHORITY["EPSG","4326"]]'
    )
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(array)
    band.FlushCache()
    dataset = None


def _bbox_to_cell(bounding_box):
    """Recover ``(column, row)`` from a fetch bounding box."""
    west, south = bounding_box[0], bounding_box[1]
    column = int(round((west - TILE_LON) / CELL_DEGREES))
    row = int(round((south - TILE_LAT) / CELL_DEGREES))
    return column, row


def _writing_fetch(records):
    """A fake ``fetch_inset`` that writes a valid GeoTIFF and records the call."""

    def fetch(definition, bounding_box, resolution_m, destination):
        records.append(
            {
                "cell": _bbox_to_cell(bounding_box),
                "bbox": bounding_box,
                "resolution_m": resolution_m,
                "path": destination,
            }
        )
        _write_cell_geotiff(destination, *bounding_box)
        return {"provider": definition["code"], "source": "synthetic"}

    return fetch


# A full-height vertical coastline near tile-relative longitude 0.15 (so
# columns 0-2 are coastal at the default 5 km band) spanning every row.
FULL_HEIGHT_COASTLINE = geometry.MultiLineString(
    [[(0.15, 0.0), (0.15, 1.0)]]
)
# A short vertical segment (columns 0-2, rows ~2-5 only).
SHORT_COASTLINE = geometry.MultiLineString(
    [[(0.15, 0.35), (0.15, 0.45)]]
)


# =====================================================================
# Gates
# =====================================================================
def test_ensure_returns_none_on_auto_tile(monkeypatch):
    tile = _coastline_tile(elevation_level="auto")
    assert ELEVATION_LEVEL.ensure_coastline_band(tile, {}) is None


def test_ensure_returns_none_with_custom_dem(monkeypatch):
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    tile = _coastline_tile(custom_dem="/pinned/raster.tif")
    assert ELEVATION_LEVEL.ensure_coastline_band(tile, {}) is None


def test_ensure_returns_none_without_provider(monkeypatch):
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    monkeypatch.setattr(
        ELEVATION_LEVEL,
        "select_tile_overlay_definition",
        lambda *args, **kwargs: None,
    )
    tile = _coastline_tile()
    assert ELEVATION_LEVEL.ensure_coastline_band(tile, {}) is None


def test_ensure_returns_none_on_empty_coastline(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, geometry.MultiLineString([]))
    tile = _coastline_tile()
    assert ELEVATION_LEVEL.ensure_coastline_band(tile, {}) is None


def test_ensure_returns_none_when_coastline_download_fails(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    _install_provider(monkeypatch)
    monkeypatch.setattr(
        "O4_OSM_Utils.OSM_queries_to_OSM_layer",
        lambda *args, **kwargs: 0,
    )
    tile = _coastline_tile()
    assert ELEVATION_LEVEL.ensure_coastline_band(tile, {}) is None


# =====================================================================
# Cell selection + band width
# =====================================================================
@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_coastal_cells_selected_inland_cells_not(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, SHORT_COASTLINE)
    records = []
    monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))

    result = ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    assert result is not None

    selected = {record["cell"] for record in records}
    # Cells straddling the short coastline (column 1, rows 2-5) are in.
    assert (1, 3) in selected
    assert (1, 4) in selected
    # A clearly inland cell (far corner) is excluded.
    assert (9, 9) not in selected
    # No cell east of column 2 qualifies at the 5 km band.
    assert all(column <= 2 for column, _row in selected)


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_wider_band_selects_more_cells(monkeypatch, tmp_path):
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, SHORT_COASTLINE)

    def count_for_band(band_km, cache_dir):
        monkeypatch.setattr(FNAMES, "Elevation_dir", str(cache_dir))
        records = []
        monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))
        tile = _coastline_tile(elevation_coastline_band_km=band_km)
        ELEVATION_LEVEL.ensure_coastline_band(tile, None)
        return len(records)

    narrow = count_for_band(5.0, tmp_path / "narrow")
    wide = count_for_band(50.0, tmp_path / "wide")
    assert wide > narrow


# =====================================================================
# Approach-visibility ladder
# =====================================================================
@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_ladder_near_mid_far_by_airport_distance(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, FULL_HEIGHT_COASTLINE)
    # A single synthetic airport box centred near tile-relative (0.15, 0.15).
    box = (
        TILE_LON + 0.14,
        TILE_LAT + 0.14,
        TILE_LON + 0.16,
        TILE_LAT + 0.16,
    )
    monkeypatch.setattr(
        INSETS,
        "_airport_bounding_boxes",
        lambda tile, dico_airports: {"KTEST": box},
    )
    records = []
    monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))

    ELEVATION_LEVEL.ensure_coastline_band(
        _coastline_tile(), {"KTEST": object()}
    )
    resolution_by_cell = {
        record["cell"]: record["resolution_m"] for record in records
    }
    # Column 1 straddles the airport; the ladder grades purely by row.
    assert resolution_by_cell[(1, 1)] == NEAR_RESOLUTION_M  # ~0 km
    assert resolution_by_cell[(1, 2)] == NEAR_RESOLUTION_M  # ~11 km
    assert resolution_by_cell[(1, 4)] == MID_RESOLUTION_M   # ~33 km
    assert resolution_by_cell[(1, 9)] == FAR_RESOLUTION_M   # ~89 km


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_ladder_far_everywhere_without_airports(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, FULL_HEIGHT_COASTLINE)
    records = []
    monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))

    ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    assert records  # some cells were selected
    assert all(
        record["resolution_m"] == FAR_RESOLUTION_M for record in records
    )


# =====================================================================
# Fetch orchestration + stamp
# =====================================================================
@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_fetch_called_once_per_missing_cell(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, SHORT_COASTLINE)
    records = []
    monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))

    ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    cells = [record["cell"] for record in records]
    assert len(cells) == len(set(cells))  # exactly one fetch per cell


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_recycle_skips_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    definition = _install_provider(monkeypatch)
    _install_coastline(monkeypatch, SHORT_COASTLINE)

    # First run fetches every cell.
    records = []
    monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))
    ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    first_run_cells = len(records)
    assert first_run_cells > 0

    # Second run recycles the cached cells: no fetch call at all.
    def fail_if_called(*args, **kwargs):
        raise AssertionError("cached cells must not be re-fetched")

    monkeypatch.setattr(INSETS, "fetch_inset", fail_if_called)
    result = ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    assert result is not None


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_clean_none_records_negative_honoured_on_rerun(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    definition = _install_provider(monkeypatch)
    _install_coastline(monkeypatch, SHORT_COASTLINE)

    # One target cell returns a clean None (no coverage); the rest succeed.
    target_cell = (1, 3)

    def selective_fetch(definition, bounding_box, resolution_m, destination):
        if _bbox_to_cell(bounding_box) == target_cell:
            return None
        _write_cell_geotiff(destination, *bounding_box)
        return {"provider": definition["code"]}

    monkeypatch.setattr(INSETS, "fetch_inset", selective_fetch)
    ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)

    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    with open(stamp_path) as handle:
        stamp = json.load(handle)
    target_stem = os.path.splitext(
        os.path.basename(
            FNAMES.coastline_band_cell_dem(
                TILE_LAT, TILE_LON, 1, 3, "TESTLIDAR", FAR_RESOLUTION_M
            )
        )
    )[0]
    assert stamp["cells"][target_stem] == INSETS.NO_COVERAGE

    # A re-run honours the recorded negative: fetch is never called for it.
    seen = []

    def rerun_fetch(definition, bounding_box, resolution_m, destination):
        seen.append(_bbox_to_cell(bounding_box))
        _write_cell_geotiff(destination, *bounding_box)
        return {"provider": definition["code"]}

    monkeypatch.setattr(INSETS, "fetch_inset", rerun_fetch)
    ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    assert target_cell not in seen
    # And the negative is preserved in the rewritten stamp.
    with open(stamp_path) as handle:
        stamp = json.load(handle)
    assert stamp["cells"][target_stem] == INSETS.NO_COVERAGE


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_fetch_exception_does_not_poison_or_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, SHORT_COASTLINE)
    target_cell = (1, 3)

    def raising_fetch(definition, bounding_box, resolution_m, destination):
        if _bbox_to_cell(bounding_box) == target_cell:
            raise RuntimeError("simulated network/GDAL failure")
        _write_cell_geotiff(destination, *bounding_box)
        return {"provider": definition["code"]}

    monkeypatch.setattr(INSETS, "fetch_inset", raising_fetch)
    # Must not raise; the band still builds from the surviving cells.
    result = ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    assert result is not None

    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    with open(stamp_path) as handle:
        stamp = json.load(handle)
    target_stem = os.path.splitext(
        os.path.basename(
            FNAMES.coastline_band_cell_dem(
                TILE_LAT, TILE_LON, 1, 3, "TESTLIDAR", FAR_RESOLUTION_M
            )
        )
    )[0]
    # A raised failure is NOT a durable negative.
    assert stamp["cells"].get(target_stem) != INSETS.NO_COVERAGE


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_stamp_factor_three_when_a_near_cell_exists(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, FULL_HEIGHT_COASTLINE)
    box = (TILE_LON + 0.14, TILE_LAT + 0.14, TILE_LON + 0.16, TILE_LAT + 0.16)
    monkeypatch.setattr(
        INSETS,
        "_airport_bounding_boxes",
        lambda tile, dico_airports: {"KTEST": box},
    )
    records = []
    monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))
    ELEVATION_LEVEL.ensure_coastline_band(
        _coastline_tile(), {"KTEST": object()}
    )
    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    with open(stamp_path) as handle:
        stamp = json.load(handle)
    assert stamp["factor"] == 3
    # finest_resolution_m is the finest (near) cell resolution present.
    assert stamp["finest_resolution_m"] == pytest.approx(NEAR_RESOLUTION_M)
    assert stamp["vrt"] == os.path.basename(
        FNAMES.coastline_band_vrt(TILE_LAT, TILE_LON, "TESTLIDAR")
    )
    assert os.path.isfile(
        FNAMES.coastline_band_vrt(TILE_LAT, TILE_LON, "TESTLIDAR")
    )


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_stamp_factor_one_without_near_cells(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, FULL_HEIGHT_COASTLINE)
    records = []
    monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))
    ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    with open(stamp_path) as handle:
        stamp = json.load(handle)
    assert stamp["factor"] == 1
    # Every cell is a far cell, so the finest resolution is the far posting.
    assert stamp["finest_resolution_m"] == pytest.approx(FAR_RESOLUTION_M)


# =====================================================================
# resolve_coastline_band_plan + coastline_grid_factor
# =====================================================================
@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_resolve_plan_round_trips_the_stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, FULL_HEIGHT_COASTLINE)
    box = (TILE_LON + 0.14, TILE_LAT + 0.14, TILE_LON + 0.16, TILE_LAT + 0.16)
    monkeypatch.setattr(
        INSETS,
        "_airport_bounding_boxes",
        lambda tile, dico_airports: {"KTEST": box},
    )
    records = []
    monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))
    tile = _coastline_tile()
    vrt_path = ELEVATION_LEVEL.ensure_coastline_band(
        tile, {"KTEST": object()}
    )

    plan = ELEVATION_LEVEL.resolve_coastline_band_plan(tile)
    assert plan is not None
    assert plan["definition"] == {"code": "TESTLIDAR"}
    assert plan["factor"] == 3
    assert plan["target_resolution_m"] == pytest.approx(NEAR_RESOLUTION_M)
    assert plan["path"] == vrt_path
    # coastline_grid_factor reads the same stamp.
    assert ELEVATION_LEVEL.coastline_grid_factor(tile) == 3


def test_resolve_plan_none_without_stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    assert (
        ELEVATION_LEVEL.resolve_coastline_band_plan(_coastline_tile())
        is None
    )


def test_resolve_plan_none_when_vrt_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    # A stamp naming a VRT that does not exist on disk -> None.
    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    os.makedirs(os.path.dirname(stamp_path), exist_ok=True)
    with open(stamp_path, "w") as handle:
        json.dump(
            {
                "provider": "TESTLIDAR",
                "factor": 3,
                "finest_resolution_m": NEAR_RESOLUTION_M,
                "vrt": "band_testlidar.vrt",
                "cells": {},
            },
            handle,
        )
    assert (
        ELEVATION_LEVEL.resolve_coastline_band_plan(_coastline_tile())
        is None
    )


def test_coastline_grid_factor_absent_stamp_is_one(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    assert ELEVATION_LEVEL.coastline_grid_factor(_coastline_tile()) == 1


# =====================================================================
# resolve_working_grid_factor coastline branch (INSETS)
# =====================================================================
def _grid_tile(**overrides):
    tile = SimpleNamespace(
        lat=TILE_LAT,
        lon=TILE_LON,
        elevation_level="coastline",
        custom_dem="",
        working_grid_arc_seconds="auto",
        airport_elevation_providers="auto",
    )
    for key, value in overrides.items():
        setattr(tile, key, value)
    return tile


def _write_factor_stamp(factor):
    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    os.makedirs(os.path.dirname(stamp_path), exist_ok=True)
    with open(stamp_path, "w") as handle:
        json.dump(
            {"provider": "TESTLIDAR", "factor": factor, "cells": {}},
            handle,
        )


def test_grid_factor_coastline_stamp_raises_historic(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 1
    )
    _write_factor_stamp(3)
    assert INSETS.resolve_working_grid_factor(_grid_tile(), None) == 3


def test_grid_factor_coastline_absent_stamp_is_historic(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 2
    )
    # No stamp -> coastline_grid_factor 1 -> max(historic 2, 1) = 2.
    assert INSETS.resolve_working_grid_factor(_grid_tile(), None) == 2


def test_grid_factor_coastline_explicit_pin_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(ELEVATION_LEVEL, "has_gdal", True)
    monkeypatch.setattr(
        INSETS, "_historic_working_grid_factor", lambda tile, base_dem: 2
    )
    _write_factor_stamp(3)
    tile = _grid_tile(working_grid_arc_seconds="1/2")
    # An explicit working-grid pin governs the grid outright.
    assert INSETS.resolve_working_grid_factor(tile, None) == 2


# =====================================================================
# Bake integration through the genuine dispatch path
# =====================================================================
@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_bake_blends_band_and_leaves_uncovered_at_base(monkeypatch, tmp_path):
    import numpy

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    # Two central cells at DIFFERENT pixel resolutions, both value 100.
    cell_paths = []
    for column, pixels in ((4, 4), (5, 6)):
        west = TILE_LON + column * CELL_DEGREES
        south = TILE_LAT + 4 * CELL_DEGREES
        east = west + CELL_DEGREES
        north = south + CELL_DEGREES
        path = FNAMES.coastline_band_cell_dem(
            TILE_LAT, TILE_LON, column, 4, "TESTLIDAR", MID_RESOLUTION_M
        )
        _write_cell_geotiff(path, west, south, east, north, value=100.0, pixels=pixels)
        cell_paths.append(path)

    vrt_path = FNAMES.coastline_band_vrt(TILE_LAT, TILE_LON, "TESTLIDAR")
    mosaic = gdal.BuildVRT(
        vrt_path,
        cell_paths,
        options=gdal.BuildVRTOptions(
            resolution="highest", resampleAlg="bilinear",
            srcNodata=-32768, VRTNodata=-32768,
        ),
    )
    mosaic = None

    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    with open(stamp_path, "w") as handle:
        json.dump(
            {
                "provider": "TESTLIDAR",
                "factor": 1,
                "finest_resolution_m": MID_RESOLUTION_M,
                "vrt": os.path.basename(vrt_path),
                "cells": {},
            },
            handle,
        )

    grid_cells = 41
    alt = numpy.zeros((grid_cells, grid_cells), dtype=numpy.float32)
    dem = SimpleNamespace(
        nxdem=grid_cells,
        nydem=grid_cells,
        x0=0.0,
        x1=1.0,
        y0=0.0,
        y1=1.0,
        nodata=NODATA,
        alt_dem=alt,
    )
    tile = _coastline_tile(
        dem=dem, airport_elevation_inset_feather_m=2000.0
    )

    # The genuine dispatch: resolve_tile_overlay_plan -> coastline plan.
    assert ELEVATION_LEVEL.bake_tile_overlay_into_alt_dem(tile) is True
    baked = tile.dem.alt_dem
    # A grid cell inside the covered central block blends toward the band.
    assert baked[22, 20] > 50.0
    # Grid regions the band never covers stay exactly at the base value.
    assert baked[0, 0] == 0.0
    assert numpy.all(baked[:, 35:] == 0.0)


# =====================================================================
# Constant-value plausibility guard (regression: the Spanish PNOA Web
# Coverage Service zero-fills requests over Portugal instead of
# answering nodata, so tile +37-008 cached all-0.0 cells stamped "ok"
# and baked the Portuguese coast flat to sea level)
# =====================================================================
def _cell_stem(column, row, resolution_m=FAR_RESOLUTION_M):
    return os.path.splitext(
        os.path.basename(
            FNAMES.coastline_band_cell_dem(
                TILE_LAT, TILE_LON, column, row, "TESTLIDAR", resolution_m
            )
        )
    )[0]


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_geotiff_is_constant_value_probe(tmp_path):
    import numpy

    box = (0.0, 0.0, 0.1, 0.1)

    constant_zero = str(tmp_path / "constant_zero.tif")
    _write_cell_geotiff(
        constant_zero, *box, array=numpy.zeros((4, 4))
    )
    assert INSETS.geotiff_is_constant_value(constant_zero) is True
    # ... and the pre-guard validity check is exactly what let it through.
    assert INSETS._geotiff_has_valid_data(constant_zero) is True

    constant_nonzero = str(tmp_path / "constant_nonzero.tif")
    _write_cell_geotiff(
        constant_nonzero, *box, array=numpy.full((4, 4), 37.5)
    )
    assert INSETS.geotiff_is_constant_value(constant_nonzero) is True

    varied = str(tmp_path / "varied.tif")
    _write_cell_geotiff(varied, *box)
    assert INSETS.geotiff_is_constant_value(varied) is False

    # Constant valid samples next to a nodata margin are still constant.
    constant_with_nodata = str(tmp_path / "constant_with_nodata.tif")
    mixed = numpy.zeros((4, 4))
    mixed[0, :] = NODATA
    _write_cell_geotiff(constant_with_nodata, *box, array=mixed)
    assert INSETS.geotiff_is_constant_value(constant_with_nodata) is True

    # All-nodata and unreadable rasters belong to _geotiff_has_valid_data.
    all_nodata = str(tmp_path / "all_nodata.tif")
    _write_cell_geotiff(all_nodata, *box, array=numpy.full((4, 4), NODATA))
    assert INSETS.geotiff_is_constant_value(all_nodata) is False
    assert (
        INSETS.geotiff_is_constant_value(str(tmp_path / "missing.tif"))
        is False
    )


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_constant_zero_fetch_recorded_no_coverage(monkeypatch, tmp_path):
    """A fetch that lands an all-0.0 cell becomes a durable negative."""
    import numpy

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, SHORT_COASTLINE)
    target_cell = (1, 3)

    def zero_filling_fetch(definition, bounding_box, resolution_m, destination):
        if _bbox_to_cell(bounding_box) == target_cell:
            _write_cell_geotiff(
                destination, *bounding_box, array=numpy.zeros((4, 4))
            )
        else:
            _write_cell_geotiff(destination, *bounding_box)
        return {"provider": definition["code"]}

    monkeypatch.setattr(INSETS, "fetch_inset", zero_filling_fetch)
    result = ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    # The band still builds from the genuine cells.
    assert result is not None

    target_stem = _cell_stem(*target_cell)
    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    with open(stamp_path) as handle:
        stamp = json.load(handle)
    assert stamp["cells"][target_stem] == INSETS.NO_COVERAGE
    # The implausible raster is deleted, never mosaicked.
    assert not os.path.isfile(
        FNAMES.coastline_band_cell_dem(
            TILE_LAT, TILE_LON, *target_cell, "TESTLIDAR", FAR_RESOLUTION_M
        )
    )

    # A re-run honours the negative without re-fetching the cell.
    seen = []

    def rerun_fetch(definition, bounding_box, resolution_m, destination):
        seen.append(_bbox_to_cell(bounding_box))
        _write_cell_geotiff(destination, *bounding_box)
        return {"provider": definition["code"]}

    monkeypatch.setattr(INSETS, "fetch_inset", rerun_fetch)
    ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    assert target_cell not in seen


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_every_cell_constant_zero_yields_no_band(monkeypatch, tmp_path):
    """The +37-008 Portuguese-half shape: all fetches zero-filled -> no band."""
    import numpy

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, SHORT_COASTLINE)

    def zero_filling_fetch(definition, bounding_box, resolution_m, destination):
        _write_cell_geotiff(
            destination, *bounding_box, array=numpy.zeros((4, 4))
        )
        return {"provider": definition["code"]}

    monkeypatch.setattr(INSETS, "fetch_inset", zero_filling_fetch)
    assert (
        ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
        is None
    )
    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    with open(stamp_path) as handle:
        stamp = json.load(handle)
    assert stamp["cells"]
    assert all(
        outcome == INSETS.NO_COVERAGE
        for outcome in stamp["cells"].values()
    )
    # No implausible raster survives to be recycled by a later run.
    band_directory = FNAMES.coastline_band_directory(TILE_LAT, TILE_LON)
    assert not [
        name
        for name in os.listdir(band_directory)
        if name.endswith(".tif")
    ]


@pytest.mark.skipif(not HAS_GDAL, reason="requires the GDAL bindings")
def test_cached_constant_cell_purged_not_recycled(monkeypatch, tmp_path):
    """A cache poisoned before the guard existed heals on the next run."""
    import numpy

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _install_provider(monkeypatch)
    _install_coastline(monkeypatch, SHORT_COASTLINE)
    poisoned_cell = (1, 3)
    poisoned_path = FNAMES.coastline_band_cell_dem(
        TILE_LAT, TILE_LON, *poisoned_cell, "TESTLIDAR", FAR_RESOLUTION_M
    )
    west = TILE_LON + poisoned_cell[0] * CELL_DEGREES
    south = TILE_LAT + poisoned_cell[1] * CELL_DEGREES
    _write_cell_geotiff(
        poisoned_path,
        west,
        south,
        west + CELL_DEGREES,
        south + CELL_DEGREES,
        array=numpy.zeros((4, 4)),
    )

    records = []
    monkeypatch.setattr(INSETS, "fetch_inset", _writing_fetch(records))
    result = ELEVATION_LEVEL.ensure_coastline_band(_coastline_tile(), None)
    assert result is not None
    assert not os.path.isfile(poisoned_path)
    stamp_path = FNAMES.coastline_band_index(TILE_LAT, TILE_LON)
    with open(stamp_path) as handle:
        stamp = json.load(handle)
    assert stamp["cells"][_cell_stem(*poisoned_cell)] == INSETS.NO_COVERAGE
    # Healthy cached cells (none here) aside, the other cells fetched fine.
    assert poisoned_cell not in [record["cell"] for record in records]
