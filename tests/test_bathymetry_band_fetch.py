"""Tests for the bathymetry band FETCH robustness layer.

Covers the 2026-07-16 hardening of ``src/O4_Bathymetry_Band.py`` after
tile +37-009 (PORTUGALTIDAL) exposed two production problems: a
multi-hour first fetch with zero progress feedback, and two engine
processes racing on the same band directory (one cell file ended up
with zero valid pixels):

  * progress -- cell completions reach ``UI.progress_bar`` (bar 1, the
    one the engine's ``step_progress`` maps to the masks step percent)
    when a consumer waits in the foreground, and never from a
    background prefetch;
  * cross-process lock -- the ``fetch.lock`` guard on the band
    directory: a stale lock (dead owner) is stolen, a live lock is
    waited on and the waiter resumes from the other process's cells
    without refetching, and ``UI.red_flag`` cancels the wait promptly;
  * atomic writes -- no partial cell/index/VRT files are ever left
    under the band directory;
  * cache validation -- ``index.json`` "ok" is never trusted without
    the cell file verifying (readable raster, at least one valid
    pixel); broken leftovers are deleted and refetched, and a fresh
    fetch that is readable but fully nodata records a durable
    ``no_coverage`` negative.

All headless: ``tmp_path`` for every file, the cell download
monkeypatched (no network), synthetic provider registry and coastline
geometry.  Skipped cleanly when the GDAL python bindings are absent
(the band code itself is a no-op without them).
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
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

pytestmark = pytest.mark.skipif(
    not HAS_GDAL, reason="osgeo (GDAL python bindings) not available"
)

PROVIDER_CODE = "FAKEBATHY"

# A short coastline segment along longitude 0.15 degrees: with
# ``bathymetry_band_km=0.1`` the selection reach is the cell
# half-diagonal (~7.9 km at the equator) plus 100 m, so exactly the
# four cells (column 1, rows 0..3) of the +00+000 tile are selected.
FOUR_CELL_COASTLINE = MultiLineString([[(0.15, 0.05), (0.15, 0.35)]])
FOUR_CELLS = [(1, 0), (1, 1), (1, 2), (1, 3)]


def _tile():
    return SimpleNamespace(lat=0, lon=0, bathymetry_band_km=0.1)


def _band_definition():
    return {
        "code": PROVIDER_CODE,
        "role": "bathymetry",
        "enabled": True,
        "priority": 100.0,
        "native_resolution_m": 3.0,
    }


def _cell_path(cell_column, cell_row):
    return FNAMES.bathymetry_band_cell(
        0, 0, cell_column, cell_row, PROVIDER_CODE,
        BATHYBAND.BATHYMETRY_CELL_RESOLUTION_M,
    )


def _write_cell_geotiff(path, west=0.1, south=0.0, east=0.2, north=0.1,
                        all_nodata=False):
    """A tiny valid band cell raster (float32, nodata -32768)."""
    columns = rows = 4
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, columns, rows, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform(
        (west, (east - west) / columns, 0, north, 0,
         (south - north) / rows)
    )
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-32768.0)
    values = numpy.full((rows, columns), -32768.0, dtype=numpy.float32)
    if not all_nodata:
        values[1:, :] = -5.0  # shallow sea, one nodata row mixed in
    band.WriteArray(values)
    band.FlushCache()
    dataset = None


def _install_fake_fetch(monkeypatch, fetch_calls, all_nodata=False,
                        on_call=None):
    """Replace the network fetch: writes a synthetic cell raster."""

    def _fake_fetch_inset(definition, bounding_box, resolution,
                          destination_path):
        fetch_calls.append(bounding_box)
        if on_call is not None:
            on_call(len(fetch_calls))
        (west, south, east, north) = bounding_box
        _write_cell_geotiff(destination_path, west, south, east, north,
                            all_nodata=all_nodata)
        return {"provider": definition["code"]}

    monkeypatch.setattr(INSETS, "fetch_inset", _fake_fetch_inset)


@pytest.fixture(autouse=True)
def _band_environment(monkeypatch, tmp_path):
    """Isolated band cache + synthetic registry/geometry, fast polling."""
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(
        INSETS, "select_bathymetry_definitions",
        lambda lat, lon: [_band_definition()],
    )
    monkeypatch.setattr(
        BATHYBAND, "_band_geometry",
        lambda tile: (FOUR_CELL_COASTLINE, None),
    )
    monkeypatch.setattr(BATHYBAND, "BAND_LOCK_POLL_SECONDS", 0.05)
    monkeypatch.setattr(UI, "red_flag", False)
    BATHYBAND._prefetch_futures.clear()
    BATHYBAND._foreground_wait.clear()
    yield
    UI.red_flag = False
    BATHYBAND._prefetch_futures.clear()
    BATHYBAND._foreground_wait.clear()


def _leftover_partials(band_directory):
    return [
        name for name in os.listdir(band_directory) if ".part" in name
    ]


# =====================================================================
# Atomic writes + index
# =====================================================================
def test_fetch_writes_cells_index_and_vrt_with_no_partial_files(
    monkeypatch,
):
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)
    vrt_path = BATHYBAND.ensure_bathymetry_band(_tile())
    assert vrt_path == FNAMES.bathymetry_band_vrt(0, 0, PROVIDER_CODE)
    assert os.path.isfile(vrt_path)
    assert len(fetch_calls) == len(FOUR_CELLS)
    for (cell_column, cell_row) in FOUR_CELLS:
        assert os.path.isfile(_cell_path(cell_column, cell_row))
    with open(FNAMES.bathymetry_band_index(0, 0)) as index_file:
        stamp = json.load(index_file)
    assert stamp["provider"] == PROVIDER_CODE
    assert sorted(stamp["cells"].values()) == ["ok"] * len(FOUR_CELLS)
    band_directory = FNAMES.bathymetry_band_directory(0, 0)
    assert _leftover_partials(band_directory) == []
    assert not os.path.isfile(
        os.path.join(band_directory, BATHYBAND.BAND_LOCK_FILE_NAME)
    )


# =====================================================================
# Progress
# =====================================================================
def test_foreground_fetch_drives_the_step_progress_bar(monkeypatch):
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)
    progress_values = []
    monkeypatch.setattr(
        UI, "progress_bar",
        lambda nbr, percentage, message=None: progress_values.append(
            (nbr, percentage)
        ),
    )
    assert BATHYBAND.ensure_bathymetry_band(_tile()) is not None
    assert progress_values, "no progress at all reached the UI contract"
    assert all(nbr == 1 for (nbr, _) in progress_values)
    percentages = [percentage for (_, percentage) in progress_values]
    assert percentages == sorted(percentages)
    assert percentages[-1] == 100


def test_background_prefetch_never_touches_the_progress_bar(monkeypatch):
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)
    progress_values = []
    monkeypatch.setattr(
        UI, "progress_bar",
        lambda nbr, percentage, message=None: progress_values.append(
            (nbr, percentage)
        ),
    )
    # The prefetch path calls this directly on a background thread.
    assert BATHYBAND._ensure_bathymetry_band_now(_tile()) is not None
    assert progress_values == []


# =====================================================================
# Cancellation
# =====================================================================
def test_red_flag_between_cells_stops_the_fan_out(monkeypatch):
    fetch_calls = []

    def _cancel_after_first(call_count):
        if call_count == 1:
            UI.red_flag = True

    _install_fake_fetch(monkeypatch, fetch_calls,
                        on_call=_cancel_after_first)
    BATHYBAND.ensure_bathymetry_band(_tile())
    # The serial session-warming cell ran; the fan-out never started.
    assert len(fetch_calls) == 1


# =====================================================================
# Cross-process lock
# =====================================================================
def test_stale_lock_from_dead_process_is_stolen(monkeypatch):
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)
    band_directory = FNAMES.bathymetry_band_directory(0, 0)
    os.makedirs(band_directory, exist_ok=True)
    dead_child = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_child.wait()
    lock_path = os.path.join(
        band_directory, BATHYBAND.BAND_LOCK_FILE_NAME
    )
    with open(lock_path, "w") as lock_file:
        json.dump(
            {"pid": dead_child.pid, "host": socket.gethostname()},
            lock_file,
        )
    vrt_path = BATHYBAND.ensure_bathymetry_band(_tile())
    assert vrt_path is not None
    assert len(fetch_calls) == len(FOUR_CELLS)
    assert not os.path.isfile(lock_path)


def test_waiter_resumes_from_the_other_process_cells(monkeypatch):
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)
    band_directory = FNAMES.bathymetry_band_directory(0, 0)
    os.makedirs(band_directory, exist_ok=True)
    lock_path = os.path.join(
        band_directory, BATHYBAND.BAND_LOCK_FILE_NAME
    )
    # A live lock: our own pid is alive, so it is never judged stale.
    with open(lock_path, "w") as lock_file:
        json.dump(
            {"pid": os.getpid(), "host": socket.gethostname()}, lock_file
        )

    result = {}

    def _consumer():
        result["vrt"] = BATHYBAND.ensure_bathymetry_band(_tile())

    consumer_thread = threading.Thread(target=_consumer)
    consumer_thread.start()
    time.sleep(0.3)
    assert consumer_thread.is_alive(), "consumer should be waiting"
    assert fetch_calls == []
    # Play the other process: write every cell + the index, release.
    stems = {}
    for (cell_column, cell_row) in FOUR_CELLS:
        cell_path = _cell_path(cell_column, cell_row)
        _write_cell_geotiff(cell_path)
        stems[os.path.splitext(os.path.basename(cell_path))[0]] = "ok"
    with open(FNAMES.bathymetry_band_index(0, 0), "w") as index_file:
        json.dump(
            {"provider": PROVIDER_CODE, "cells": stems,
             "checked": "2026-07-16"},
            index_file,
        )
    os.remove(lock_path)
    consumer_thread.join(timeout=15)
    assert not consumer_thread.is_alive()
    # It resumed from the other fetch's cells: nothing was refetched.
    assert fetch_calls == []
    assert result["vrt"] == FNAMES.bathymetry_band_vrt(
        0, 0, PROVIDER_CODE
    )
    assert os.path.isfile(result["vrt"])


def test_red_flag_while_waiting_on_a_live_lock_returns_none(monkeypatch):
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)
    band_directory = FNAMES.bathymetry_band_directory(0, 0)
    os.makedirs(band_directory, exist_ok=True)
    lock_path = os.path.join(
        band_directory, BATHYBAND.BAND_LOCK_FILE_NAME
    )
    with open(lock_path, "w") as lock_file:
        json.dump(
            {"pid": os.getpid(), "host": socket.gethostname()}, lock_file
        )

    result = {}

    def _consumer():
        result["vrt"] = BATHYBAND.ensure_bathymetry_band(_tile())

    consumer_thread = threading.Thread(target=_consumer)
    consumer_thread.start()
    time.sleep(0.2)
    UI.red_flag = True
    consumer_thread.join(timeout=15)
    assert not consumer_thread.is_alive()
    assert result["vrt"] is None
    assert fetch_calls == []
    # The lock still belongs to its (simulated) owner.
    assert os.path.isfile(lock_path)


# =====================================================================
# Cache validation
# =====================================================================
def test_broken_cached_cells_are_refetched_despite_index_ok(monkeypatch):
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)
    band_directory = FNAMES.bathymetry_band_directory(0, 0)
    os.makedirs(band_directory, exist_ok=True)
    # Cell (1,0): truncated garbage. Cell (1,1): a readable raster whose
    # every pixel is nodata (the +37-009 race leftover). Both stamped
    # "ok" in the index -- which must not be trusted.
    truncated_path = _cell_path(1, 0)
    with open(truncated_path, "wb") as truncated_file:
        truncated_file.write(b"this is not a raster")
    all_nodata_path = _cell_path(1, 1)
    _write_cell_geotiff(all_nodata_path, all_nodata=True)
    stems = {}
    for (cell_column, cell_row) in FOUR_CELLS:
        cell_path = _cell_path(cell_column, cell_row)
        stems[os.path.splitext(os.path.basename(cell_path))[0]] = "ok"
    with open(FNAMES.bathymetry_band_index(0, 0), "w") as index_file:
        json.dump(
            {"provider": PROVIDER_CODE, "cells": stems,
             "checked": "2026-07-15"},
            index_file,
        )
    vrt_path = BATHYBAND.ensure_bathymetry_band(_tile())
    assert vrt_path is not None
    # All four cells were (re)fetched: the two broken ones plus the two
    # that were never on disk.
    assert len(fetch_calls) == len(FOUR_CELLS)
    for path in (truncated_path, all_nodata_path):
        dataset = gdal.Open(path)
        (minimum, maximum) = dataset.GetRasterBand(
            1
        ).ComputeRasterMinMax(1)
        dataset = None
        assert minimum <= -5.0


def test_all_nodata_fetch_records_durable_no_coverage(monkeypatch):
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls, all_nodata=True)
    assert BATHYBAND.ensure_bathymetry_band(_tile()) is None
    assert len(fetch_calls) == len(FOUR_CELLS)
    with open(FNAMES.bathymetry_band_index(0, 0)) as index_file:
        stamp = json.load(index_file)
    assert sorted(stamp["cells"].values()) == (
        [BATHYBAND.NO_COVERAGE] * len(FOUR_CELLS)
    )
    band_directory = FNAMES.bathymetry_band_directory(0, 0)
    assert _leftover_partials(band_directory) == []
    for (cell_column, cell_row) in FOUR_CELLS:
        assert not os.path.isfile(_cell_path(cell_column, cell_row))
    # The negatives are durable: a second run fetches nothing.
    assert BATHYBAND.ensure_bathymetry_band(_tile()) is None
    assert len(fetch_calls) == len(FOUR_CELLS)
