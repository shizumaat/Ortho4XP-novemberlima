"""Tests for the bathymetry band's one-cell overhang into neighbour tiles.

Diagnosed 2026-07-16 at the Ria Formosa (tiles +36-008 / +37-008): mask
squares straddle tile edges, but the band was clamped to the 1 degree
tile, so each tile's copy of a shared straddling square carried the
measured blend only on its own side of the line — a razor-straight seam
at 37N in the sim.  The band now enumerates one overhang ring (cell
indices -1 and 10); overhang cells resolve to the OWNING tile's
canonical cell path so adjacent builds share each physical cell file.

Covers:

  * overhang cells fetch into the owner tile's band directory and are
    stamped under a key qualified by the owner tile (no collision with
    this tile's own cell of the same local indices);
  * an owner cell already on disk is reused without a fetch;
  * an owner tile's durable no-coverage negative is honoured (no
    refetch of a cell the neighbour already probed empty).

All headless: ``tmp_path`` for every file, the cell download
monkeypatched, synthetic provider registry / coastline geometry.
Everything skips without the GDAL python bindings (the VRT build).
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

try:
    from osgeo import gdal, osr

    HAS_GDAL = True
except Exception:
    HAS_GDAL = False

import numpy
from shapely.geometry import MultiLineString

import O4_Airport_Elevation_Insets as INSETS
import O4_Bathymetry_Band as BATHYBAND
import O4_File_Names as FNAMES
import O4_UI_Utils as UI

pytestmark = pytest.mark.skipif(not HAS_GDAL, reason="osgeo not available")

PROVIDER_CODE = "FAKEBATHY"

# A shoreline segment crossing the +00+000 tile's NORTH edge along
# longitude 0.15: with ``bathymetry_band_km=0.1`` it selects exactly the
# in-tile cell (1, 9) and the overhang cell (1, 10) — owned by tile
# (+01+000).
NORTH_EDGE_COASTLINE = MultiLineString([[(0.15, 0.95), (0.15, 1.05)]])


def _tile(**overrides):
    attributes = dict(
        lat=0,
        lon=0,
        bathymetry_band_km=0.1,
        bathymetry_airport_radius_km=1.0,
    )
    attributes.update(overrides)
    return SimpleNamespace(**attributes)


def _band_definition(code=PROVIDER_CODE, intertidal=False, priority=100.0):
    return {
        "code": code,
        "role": "bathymetry",
        "enabled": True,
        "priority": priority,
        "native_resolution_m": 3.0,
        "intertidal": intertidal,
    }


@pytest.fixture(autouse=True)
def _overhang_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(
        INSETS,
        "select_bathymetry_definitions",
        lambda lat, lon: [_band_definition()],
    )
    monkeypatch.setattr(
        BATHYBAND,
        "_band_geometry",
        lambda tile: (NORTH_EDGE_COASTLINE, None),
    )
    monkeypatch.setattr(UI, "red_flag", False)
    BATHYBAND._prefetch_futures.clear()
    BATHYBAND._foreground_wait.clear()
    yield
    UI.red_flag = False
    BATHYBAND._prefetch_futures.clear()
    BATHYBAND._foreground_wait.clear()


def _write_cell_geotiff(path, west, south, east, north):
    """A tiny valid band cell raster (float32, nodata -32768)."""
    columns = rows = 4
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, columns, rows, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform(
        (west, (east - west) / columns, 0, north, 0, (south - north) / rows)
    )
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-32768.0)
    band.WriteArray(numpy.full((rows, columns), -5.0, dtype=numpy.float32))
    band.FlushCache()
    dataset = None


def _install_fake_fetch(monkeypatch, fetch_calls):
    def _fake_fetch_inset(definition, bounding_box, resolution,
                          destination_path):
        fetch_calls.append(bounding_box)
        (west, south, east, north) = bounding_box
        _write_cell_geotiff(destination_path, west, south, east, north)
        return {"provider": definition["code"]}

    monkeypatch.setattr(INSETS, "fetch_inset", _fake_fetch_inset)


def _in_tile_cell_path():
    return FNAMES.bathymetry_band_cell(
        0, 0, 1, 9, PROVIDER_CODE, BATHYBAND.BATHYMETRY_CELL_RESOLUTION_M
    )


def _owner_cell_path():
    """The overhang cell (1, 10) canonically belongs to tile (+01+000)
    as its local cell (1, 0)."""
    return FNAMES.bathymetry_band_cell(
        1, 0, 1, 0, PROVIDER_CODE, BATHYBAND.BATHYMETRY_CELL_RESOLUTION_M
    )


def test_overhang_cell_fetches_into_owner_directory(monkeypatch):
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    band_vrt = BATHYBAND.ensure_bathymetry_band(_tile())

    assert band_vrt is not None and os.path.isfile(band_vrt)
    assert len(fetch_calls) == 2
    assert os.path.isfile(_in_tile_cell_path())
    # The overhang cell landed at the OWNER tile's canonical path.
    assert os.path.isfile(_owner_cell_path())
    assert "N01E000" in _owner_cell_path().replace(os.sep, "/")
    # The fetch bbox of the overhang cell lies in the neighbour tile.
    overhang_bboxes = [
        bbox for bbox in fetch_calls if bbox[1] >= 1.0
    ]
    assert len(overhang_bboxes) == 1
    # Our stamp records it under an owner-qualified key; the in-tile
    # stem is untouched (existing durable negatives stay valid).
    stamp = BATHYBAND._read_band_stamp(
        FNAMES.bathymetry_band_index(0, 0)
    )
    stems = set(stamp.get("cells", {}))
    assert any(stem.endswith("@+01+000") for stem in stems)
    assert any("@" not in stem for stem in stems)
    # The VRT mosaics both cells (the overhang one lives in another
    # directory).
    with open(band_vrt) as handle:
        vrt_text = handle.read()
    assert os.path.basename(_in_tile_cell_path()) in vrt_text
    assert "N01E000_bathymetry_band" in vrt_text


def test_overhang_reuses_owner_cell_without_fetch(monkeypatch):
    """The neighbour tile already fetched the shared cell: this build
    references it in place — one fetch for the in-tile cell only."""
    owner_path = _owner_cell_path()
    os.makedirs(os.path.dirname(owner_path), exist_ok=True)
    _write_cell_geotiff(owner_path, 0.1, 1.0, 0.2, 1.1)
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    band_vrt = BATHYBAND.ensure_bathymetry_band(_tile())

    assert band_vrt is not None
    assert len(fetch_calls) == 1
    assert fetch_calls[0][1] < 1.0  # the in-tile cell
    with open(band_vrt) as handle:
        assert "N01E000_bathymetry_band" in handle.read()


def test_overhang_honours_owner_no_coverage_negative(monkeypatch):
    """The neighbour tile durably recorded the shared cell as
    no-coverage: this build neither refetches nor probes it."""
    owner_stamp_path = FNAMES.bathymetry_band_index(1, 0)
    os.makedirs(os.path.dirname(owner_stamp_path), exist_ok=True)
    owner_stem = os.path.splitext(os.path.basename(_owner_cell_path()))[0]
    BATHYBAND._write_band_stamp(
        owner_stamp_path,
        {
            "provider": PROVIDER_CODE,
            "cells": {owner_stem: BATHYBAND.NO_COVERAGE},
            "checked": "2026-07-16",
        },
    )
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    band_vrt = BATHYBAND.ensure_bathymetry_band(_tile())

    assert band_vrt is not None
    assert len(fetch_calls) == 1
    assert fetch_calls[0][1] < 1.0
    # The negative is now durable locally too, under the qualified key.
    stamp = BATHYBAND._read_band_stamp(FNAMES.bathymetry_band_index(0, 0))
    assert (
        stamp["cells"].get(owner_stem + "@+01+000")
        == BATHYBAND.NO_COVERAGE
    )


# =====================================================================
# Intertidal-only sources: automatic paths skip them (the OpenStreetMap
# shallow-water fallback matches their binary "flats" result for free,
# and the DSF sea_level raster's min(measured, elevation - 2) makes
# their centimetre depths a no-op); only masks_use_DEM_too=True passes
# intertidal_ok and fetches them.
# =====================================================================
def test_intertidal_definition_skipped_without_opt_in(monkeypatch):
    monkeypatch.setattr(
        INSETS,
        "select_bathymetry_definitions",
        lambda lat, lon: [_band_definition(intertidal=True)],
    )
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    assert BATHYBAND.ensure_bathymetry_band(_tile()) is None
    assert fetch_calls == []


def test_intertidal_definition_fetched_with_opt_in(monkeypatch):
    monkeypatch.setattr(
        INSETS,
        "select_bathymetry_definitions",
        lambda lat, lon: [_band_definition(intertidal=True)],
    )
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    band_vrt = BATHYBAND.ensure_bathymetry_band(
        _tile(), intertidal_ok=True
    )

    assert band_vrt is not None
    assert len(fetch_calls) == 2


def test_intertidal_skipped_in_favour_of_next_provider(monkeypatch):
    """A higher-priority intertidal twin never starves a real
    bathymetry source behind it on the automatic paths."""
    monkeypatch.setattr(
        INSETS,
        "select_bathymetry_definitions",
        lambda lat, lon: [
            _band_definition(code="TIDALTWIN", intertidal=True,
                             priority=95.0),
            _band_definition(code=PROVIDER_CODE, priority=50.0),
        ],
    )
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    band_vrt = BATHYBAND.ensure_bathymetry_band(_tile())

    assert band_vrt is not None
    assert "fakebathy" in os.path.basename(band_vrt)
    assert len(fetch_calls) == 2
