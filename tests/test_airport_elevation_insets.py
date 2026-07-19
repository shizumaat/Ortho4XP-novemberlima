"""Unit tests for the airport elevation inset feature (Phase A).

No network is used anywhere: the Transactional National Map (TNM) discovery
responses are replaced by dummy strategies, and every raster is a synthetic
in-memory / temporary GeoTIFF built with ``osgeo.gdal``.  Tests that need
GDAL are skipped cleanly when the ``osgeo`` bindings are unavailable.

Covered:
  * ``.elv`` provider-definition parsing (valid, invalid, role field).
  * inset-provider selection excludes ``role=base`` definitions.
  * access-strategy registry dispatch -- a second dummy strategy plugs in
    with zero orchestration change.
  * ``index.json`` negative-result caching (no re-query without refresh).
  * composite-source assembly determinism between the step-1 and step-2
    code paths.
  * the ``.alt`` raster bake with a feathered blend band (the G2 proof).
"""

import json
import os

import numpy
import pytest

import O4_File_Names as FNAMES
import O4_Airport_Elevation_Insets as INSETS

try:
    from osgeo import gdal, osr

    HAS_GDAL = True
except Exception:
    HAS_GDAL = False

requires_gdal = pytest.mark.skipif(
    not HAS_GDAL, reason="osgeo (GDAL python bindings) not available"
)


# =====================================================================
# Helpers
# =====================================================================
def _write_elv(directory, code, lines):
    path = os.path.join(directory, code + ".elv")
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def _write_constant_geotiff(
    path, west, south, east, north, value, columns=40, rows=40
):
    """Write a constant-value EPSG:4326 float32 GeoTIFF with nodata set."""
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, columns, rows, 1, gdal.GDT_Float32)
    pixel_width = (east - west) / columns
    pixel_height = (south - north) / rows  # negative
    dataset.SetGeoTransform((west, pixel_width, 0, north, 0, pixel_height))
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-32768.0)
    band.WriteArray(numpy.full((rows, columns), value, dtype=numpy.float32))
    band.FlushCache()
    dataset = None
    return path


# =====================================================================
# .elv parsing
# =====================================================================
def test_parse_valid_and_invalid_elv(tmp_path):
    providers_directory = tmp_path / "Elevation"
    providers_directory.mkdir()
    _write_elv(
        str(providers_directory),
        "GOOD",
        [
            "# a valid definition",
            "access_strategy=tnm_cog",
            "role=airport_inset",
            "native_resolution_m=1",
            "coverage_bbox=-180.0,15.0,-64.0,72.0  # continental US",
            "priority=100",
            "enabled=True",
            "attribution=Some Agency",
        ],
    )
    # Missing the mandatory access_strategy key -> skipped, not fatal.
    _write_elv(
        str(providers_directory),
        "BROKEN",
        ["role=airport_inset", "priority=5"],
    )
    # A .txt file must be ignored by the extension filter.
    (providers_directory / "NOTES.txt").write_text("access_strategy=tnm_cog\n")

    parsed = INSETS.initialize_elevation_providers_dict(
        str(providers_directory)
    )

    assert "GOOD" in parsed
    assert "BROKEN" not in parsed
    good = parsed["GOOD"]
    assert good["access_strategy"] == "tnm_cog"
    assert good["role"] == "airport_inset"
    assert good["priority"] == 100.0
    assert good["enabled"] is True
    assert good["native_resolution_m"] == 1.0
    # inline comment stripped, parsed to a 4-tuple
    assert good["coverage_bbox"] == (-180.0, 15.0, -64.0, 72.0)
    # unknown-but-present keys are preserved verbatim
    assert good["attribution"] == "Some Agency"


def test_role_defaults_to_airport_inset_when_absent(tmp_path):
    providers_directory = tmp_path / "Elevation"
    providers_directory.mkdir()
    _write_elv(
        str(providers_directory),
        "NOROLE",
        ["access_strategy=tnm_cog", "enabled=True"],
    )
    parsed = INSETS.initialize_elevation_providers_dict(
        str(providers_directory)
    )
    assert parsed["NOROLE"]["role"] == INSETS.ROLE_AIRPORT_INSET


def test_base_role_excluded_from_inset_selection(tmp_path):
    """A role=base definition parses but is never an inset provider."""
    providers_directory = tmp_path / "Elevation"
    providers_directory.mkdir()
    _write_elv(
        str(providers_directory),
        "INSET",
        ["access_strategy=tnm_cog", "role=airport_inset", "priority=100"],
    )
    _write_elv(
        str(providers_directory),
        "BASEONLY",
        ["access_strategy=viewfinder_zip", "role=base", "priority=999"],
    )
    parsed = INSETS.initialize_elevation_providers_dict(
        str(providers_directory)
    )
    # Both parse into the registry...
    assert set(parsed) == {"INSET", "BASEONLY"}
    # ...but auto inset selection returns only the airport_inset one,
    # despite BASEONLY's higher priority, and with no warning.
    selected = INSETS.select_provider_definitions("auto")
    assert [definition["code"] for definition in selected] == ["INSET"]
    # Explicitly naming the base provider on the inset path drops it silently.
    assert INSETS.select_provider_definitions("BASEONLY") == []


# =====================================================================
# Strategy registry dispatch
# =====================================================================
def test_second_strategy_plugs_in_without_orchestration_change(tmp_path):
    """A dummy strategy registers and is dispatched by fetch_inset.

    Orchestration (ensure_airport_insets / fetch_inset) is untouched -- it
    only looks the strategy up in the registry -- proving new strategies
    are additive.
    """
    calls = {"discover": 0, "fetch": 0}

    @INSETS.register_access_strategy("dummy_test_strategy")
    class _DummyStrategy:
        def discover(self, definition, bounding_box_wgs84):
            calls["discover"] += 1
            return [{"note": "always covers"}]

        def fetch(
            self,
            definition,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
        ):
            calls["fetch"] += 1
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            with open(destination_path, "wb") as handle:
                handle.write(b"synthetic-raster")
            return {"provider": definition["code"], "strategy": "dummy"}

    try:
        definition = {
            "code": "DUMMY",
            "access_strategy": "dummy_test_strategy",
            "role": INSETS.ROLE_AIRPORT_INSET,
            "enabled": True,
            "priority": 1.0,
        }
        destination = str(tmp_path / "dummy.tif")
        provenance = INSETS.fetch_inset(
            definition, (-1.0, -1.0, 1.0, 1.0), 3.0, destination
        )
        assert provenance == {"provider": "DUMMY", "strategy": "dummy"}
        assert calls["fetch"] == 1
        assert os.path.isfile(destination)
        # discover is also dispatched through the registry
        assert INSETS.discover_inset(definition, (-1.0, -1.0, 1.0, 1.0)) == [
            {"note": "always covers"}
        ]
        assert calls["discover"] == 1
    finally:
        INSETS.ACCESS_STRATEGIES.pop("dummy_test_strategy", None)


# =====================================================================
# index.json negative-result caching
# =====================================================================
def test_negative_result_is_cached_and_not_requeried(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    discover_calls = {"count": 0}

    @INSETS.register_access_strategy("no_coverage_strategy")
    class _NoCoverageStrategy:
        def discover(self, definition, bounding_box_wgs84):
            discover_calls["count"] += 1
            return None

        def fetch(self, definition, bbox, resolution_m, destination_path):
            discover_calls["count"] += 1
            return None

    try:
        definition = {
            "code": "NOCOV",
            "access_strategy": "no_coverage_strategy",
            "role": INSETS.ROLE_AIRPORT_INSET,
            "enabled": True,
            "priority": 1.0,
        }
        boxes = {"KJFK": (-73.82, 40.62, -73.75, 40.68)}

        first = INSETS.ensure_airport_insets(
            36, -87, boxes, [definition], 3.0
        )
        assert first["KJFK"]["NOCOV"] == INSETS.NO_COVERAGE
        assert "checked" in first["KJFK"]
        calls_after_first = discover_calls["count"]
        assert calls_after_first >= 1

        # A second run without refresh must NOT re-query the strategy.
        INSETS.ensure_airport_insets(36, -87, boxes, [definition], 3.0)
        assert discover_calls["count"] == calls_after_first

        # ...but refresh=True does re-query.
        INSETS.ensure_airport_insets(
            36, -87, boxes, [definition], 3.0, refresh=True
        )
        assert discover_calls["count"] > calls_after_first

        # index.json is on disk at the tile's inset directory.
        assert os.path.isfile(FNAMES.airport_inset_index(36, -87))
    finally:
        INSETS.ACCESS_STRATEGIES.pop("no_coverage_strategy", None)


# =====================================================================
# Margin-aware cache invalidation (bounding-box staleness)
# =====================================================================
_SMALL_BOX = (-135.06, 60.69, -135.04, 60.72)
_LARGE_BOX = (-135.10, 60.65, -135.00, 60.76)


def _register_box_recording_strategy(name, calls, fail_when=None):
    """Register a dummy strategy whose fetches record the requested box.

    ``calls`` collects one ``(west, south, east, north)`` tuple per fetch.
    ``fail_when(bounding_box)`` returning True makes that fetch report no
    coverage (after possibly leaving a partial file behind, like a broken
    ``gdal.Warp`` would).
    """

    @INSETS.register_access_strategy(name)
    class _BoxRecordingStrategy:
        def discover(self, definition, bounding_box_wgs84):
            if fail_when is not None and fail_when(bounding_box_wgs84):
                return None
            return [{"note": "covers"}]

        def fetch(
            self,
            definition,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
        ):
            calls.append(tuple(bounding_box_wgs84))
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            if fail_when is not None and fail_when(bounding_box_wgs84):
                with open(destination_path, "wb") as handle:
                    handle.write(b"partial-garbage")
                return None
            with open(destination_path, "wb") as handle:
                handle.write(repr(tuple(bounding_box_wgs84)).encode())
            return {
                "provider": definition["code"],
                "bounding_box_wgs84": list(bounding_box_wgs84),
            }

    return _BoxRecordingStrategy


def _box_definition(code, strategy_name):
    return {
        "code": code,
        "access_strategy": strategy_name,
        "role": INSETS.ROLE_AIRPORT_INSET,
        "enabled": True,
        "priority": 1.0,
    }


def test_margin_growth_refetches_cached_inset(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    fetch_calls = []
    _register_box_recording_strategy("box_growth_strategy", fetch_calls)
    try:
        definition = _box_definition("BOXGROW", "box_growth_strategy")
        destination = FNAMES.airport_inset_dem(60, -136, "CYXY", "BOXGROW")

        INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _SMALL_BOX}, [definition], 3.0
        )
        assert fetch_calls == [_SMALL_BOX]

        # Same box again: the cache holds, nothing is refetched.
        INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _SMALL_BOX}, [definition], 3.0
        )
        assert fetch_calls == [_SMALL_BOX]

        # A larger box (margin grew) outreaches the recorded fetch:
        # the inset is refetched and the raster now covers the large box.
        INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _LARGE_BOX}, [definition], 3.0
        )
        assert fetch_calls == [_SMALL_BOX, _LARGE_BOX]
        with open(destination, "rb") as handle:
            assert handle.read() == repr(_LARGE_BOX).encode()
        assert not os.path.isfile(destination + ".refetch")

        # The enlarged cache is fresh in its turn.
        INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _LARGE_BOX}, [definition], 3.0
        )
        assert fetch_calls == [_SMALL_BOX, _LARGE_BOX]
    finally:
        INSETS.ACCESS_STRATEGIES.pop("box_growth_strategy", None)


def test_margin_shrink_reuses_superset_inset(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    fetch_calls = []
    _register_box_recording_strategy("box_shrink_strategy", fetch_calls)
    try:
        definition = _box_definition("BOXSHRINK", "box_shrink_strategy")
        INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _LARGE_BOX}, [definition], 3.0
        )
        # A smaller request is inside the cached raster: no refetch.
        INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _SMALL_BOX}, [definition], 3.0
        )
        assert fetch_calls == [_LARGE_BOX]
    finally:
        INSETS.ACCESS_STRATEGIES.pop("box_shrink_strategy", None)


def test_failed_refetch_keeps_previous_inset(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    fetch_calls = []
    _register_box_recording_strategy(
        "box_fail_large_strategy",
        fetch_calls,
        fail_when=lambda box: tuple(box) == _LARGE_BOX,
    )
    try:
        definition = _box_definition("BOXFAIL", "box_fail_large_strategy")
        destination = FNAMES.airport_inset_dem(60, -136, "CYXY", "BOXFAIL")

        INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _SMALL_BOX}, [definition], 3.0
        )
        index = INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _LARGE_BOX}, [definition], 3.0
        )
        assert fetch_calls == [_SMALL_BOX, _LARGE_BOX]
        # The enlargement failed: the smaller raster survives untouched,
        # stays recorded as usable, and no scratch file is left behind.
        with open(destination, "rb") as handle:
            assert handle.read() == repr(_SMALL_BOX).encode()
        assert index["CYXY"]["BOXFAIL"] == "ok"
        assert not os.path.isfile(destination + ".refetch")

        # The surviving cache is still smaller than requested, so the next
        # run tries the enlargement again (self-healing after outages).
        INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _LARGE_BOX}, [definition], 3.0
        )
        assert fetch_calls == [_SMALL_BOX, _LARGE_BOX, _LARGE_BOX]
    finally:
        INSETS.ACCESS_STRATEGIES.pop("box_fail_large_strategy", None)


def test_margin_growth_rechecks_negative_results(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    fetch_calls = []
    _register_box_recording_strategy(
        "box_small_no_coverage_strategy",
        fetch_calls,
        fail_when=lambda box: tuple(box) == _SMALL_BOX,
    )
    try:
        definition = _box_definition(
            "BOXNEG", "box_small_no_coverage_strategy"
        )
        first = INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _SMALL_BOX}, [definition], 3.0
        )
        assert first["CYXY"]["BOXNEG"] == INSETS.NO_COVERAGE
        assert first["CYXY"]["bounding_box"] == list(_SMALL_BOX)

        # Same box: the negative result caches, no re-query.
        INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _SMALL_BOX}, [definition], 3.0
        )
        assert fetch_calls == [_SMALL_BOX]

        # A larger box outgrows the box the negative was evaluated
        # against, so the provider is re-checked and now delivers.
        second = INSETS.ensure_airport_insets(
            60, -136, {"CYXY": _LARGE_BOX}, [definition], 3.0
        )
        assert fetch_calls == [_SMALL_BOX, _LARGE_BOX]
        assert second["CYXY"]["BOXNEG"] == "ok"
    finally:
        INSETS.ACCESS_STRATEGIES.pop(
            "box_small_no_coverage_strategy", None
        )


def test_legacy_caches_without_recorded_box_are_reused(
    tmp_path, monkeypatch
):
    """Pre-margin-aware caches carry no box anywhere: never refetched.

    A cached GeoTIFF without a provenance sidecar and an index record
    without ``"bounding_box"`` predate this bookkeeping; both are
    grandfathered as covering whatever is requested (only ``refresh``
    renews them), and the record heals by gaining the current box.
    """
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    fetch_calls = []
    _register_box_recording_strategy("box_legacy_strategy", fetch_calls)
    try:
        definition = _box_definition("BOXLEGACY", "box_legacy_strategy")
        destination = FNAMES.airport_inset_dem(
            60, -136, "CYXY", "BOXLEGACY"
        )
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as handle:
            handle.write(b"legacy-raster")
        INSETS._write_index(
            60, -136, {"CYXY": {"BOXLEGACY": "ok"}, "CYYY": {
                "BOXLEGACY": INSETS.NO_COVERAGE}}
        )

        index = INSETS.ensure_airport_insets(
            60,
            -136,
            {"CYXY": _LARGE_BOX, "CYYY": _LARGE_BOX},
            [definition],
            3.0,
        )
        assert fetch_calls == []
        with open(destination, "rb") as handle:
            assert handle.read() == b"legacy-raster"
        assert index["CYXY"]["bounding_box"] == list(_LARGE_BOX)
        assert index["CYYY"]["BOXLEGACY"] == INSETS.NO_COVERAGE
    finally:
        INSETS.ACCESS_STRATEGIES.pop("box_legacy_strategy", None)


# =====================================================================
# Composite-source assembly determinism (step 1 == step 2)
# =====================================================================
class _FakeTile:
    def __init__(self, lat, lon, custom_dem=""):
        self.lat = lat
        self.lon = lon
        self.dem = None
        self.custom_dem = custom_dem
        self.airport_elevation_insets = True
        self.airport_elevation_providers = "auto"
        self.airport_elevation_inset_resolution_m = 3.0
        self.airport_elevation_inset_margin_m = 1000.0
        self.airport_elevation_inset_feather_m = 60.0
        self.working_grid_arc_seconds = "auto"


def test_composite_assembly_is_deterministic_across_steps(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    # Reload the shipped USGS3DEP definition for a real provider code.
    INSETS.initialize_elevation_providers_dict()

    # Two cached inset files on disk (as if a prior fetch had run).
    inset_directory = FNAMES.airport_inset_directory(36, -87)
    os.makedirs(inset_directory, exist_ok=True)
    for icao in ("KBNA", "KJWN"):
        open(
            FNAMES.airport_inset_dem(36, -87, icao, "USGS3DEP"), "wb"
        ).close()

    tile = _FakeTile(36, -87, custom_dem="")

    # The step-1 hook and the step-2 hook call the SAME helper on the SAME
    # disk state -> byte-identical composite string.
    step_one = INSETS.assemble_inset_composite_source(tile, tile.custom_dem)
    step_two = INSETS.assemble_inset_composite_source(tile, tile.custom_dem)
    assert step_one == step_two

    tokens = step_one.split(";")
    # base is the first token (so step 2's split[0] yields the base dims)...
    assert tokens[0] == ""
    # ...and both cached insets are appended, deterministically sorted.
    assert tokens[1:] == sorted(tokens[1:])
    assert len(tokens) == 3
    assert all(token.endswith("_usgs3dep.tif") for token in tokens[1:])


def test_composite_assembly_is_noop_when_gate_off(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    tile = _FakeTile(36, -87, custom_dem="SRTM;/some/local.tif")
    tile.airport_elevation_insets = False
    # Gate off -> the source is returned untouched (byte-identical build).
    assert (
        INSETS.assemble_inset_composite_source(tile, tile.custom_dem)
        == "SRTM;/some/local.tif"
    )


# =====================================================================
# The .alt raster bake with feathering (G2 proof)
# =====================================================================
@requires_gdal
def test_alt_bake_applies_inset_with_feather(tmp_path, monkeypatch):
    import O4_DEM_Utils as DEM

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    INSETS.initialize_elevation_providers_dict()

    tile_latitude, tile_longitude = 0, 0

    # Flat 0 m base raster covering the whole tile-relative unit square.
    base_path = str(tmp_path / "base.tif")
    _write_constant_geotiff(
        base_path, 0.0, 0.0, 1.0, 1.0, 0.0, columns=301, rows=301
    )
    base_dem = DEM.DEM(
        tile_latitude, tile_longitude, base_path, fill_nodata=False
    )

    # Flat 100 m inset over an inner window, placed in the tile cache dir.
    inset_directory = FNAMES.airport_inset_directory(
        tile_latitude, tile_longitude
    )
    os.makedirs(inset_directory, exist_ok=True)
    inset_path = FNAMES.airport_inset_dem(
        tile_latitude, tile_longitude, "TEST", "USGS3DEP"
    )
    _write_constant_geotiff(
        inset_path, 0.35, 0.35, 0.65, 0.65, 100.0, columns=120, rows=120
    )

    tile = _FakeTile(tile_latitude, tile_longitude)
    tile.dem = base_dem
    tile.airport_elevation_inset_feather_m = 2000.0

    INSETS.bake_airport_insets_into_alt_dem(tile)

    # Write the .alt exactly as the pipeline does, then read it back raw so
    # we prove the values reach the WRITTEN raster (the mesher's input).
    alt_path = str(tmp_path / "baked.alt")
    base_dem.write_to_file(alt_path)
    written = numpy.fromfile(alt_path, dtype=numpy.float32).reshape(
        (base_dem.nydem, base_dem.nxdem)
    )

    number_of_columns = base_dem.nxdem
    number_of_rows = base_dem.nydem

    def cell(longitude_fraction, latitude_fraction):
        column = int(
            round(
                (longitude_fraction - base_dem.x0)
                / (base_dem.x1 - base_dem.x0)
                * (number_of_columns - 1)
            )
        )
        row = int(
            round(
                (base_dem.y1 - latitude_fraction)
                / (base_dem.y1 - base_dem.y0)
                * (number_of_rows - 1)
            )
        )
        return written[row, column]

    # Interior of the inset -> full inset value.
    assert cell(0.50, 0.50) == pytest.approx(100.0, abs=0.5)
    # Well outside the inset -> untouched base.
    assert cell(0.10, 0.10) == pytest.approx(0.0, abs=0.001)
    assert cell(0.90, 0.90) == pytest.approx(0.0, abs=0.001)
    # A cell just inside the inset edge sits in the feather ramp:
    # strictly between base and inset value.
    ramp_value = cell(0.355, 0.50)
    assert 0.0 < ramp_value < 100.0

    # The whole raster stays within [base, inset] -- no overshoot / cliff.
    assert written.min() >= -0.001
    assert written.max() <= 100.001


def _bake_tile_with_insets(tmp_path, monkeypatch, insets):
    """Bake constant-value insets over a flat 0 m base; return the tile.

    ``insets`` is a list of ``(icao, provider_code, west, south, east,
    north, value_m)`` tuples written into the tile's inset cache
    directory before the bake.
    """
    import O4_DEM_Utils as DEM

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    INSETS.initialize_elevation_providers_dict()

    base_path = str(tmp_path / "base.tif")
    _write_constant_geotiff(
        base_path, 0.0, 0.0, 1.0, 1.0, 0.0, columns=301, rows=301
    )
    tile = _FakeTile(0, 0)
    tile.dem = DEM.DEM(0, 0, base_path, fill_nodata=False)
    tile.airport_elevation_inset_feather_m = 2000.0

    inset_directory = FNAMES.airport_inset_directory(0, 0)
    os.makedirs(inset_directory, exist_ok=True)
    for (icao, provider_code, west, south, east, north, value) in insets:
        _write_constant_geotiff(
            FNAMES.airport_inset_dem(0, 0, icao, provider_code),
            west, south, east, north, value, columns=60, rows=60,
        )
    INSETS.bake_airport_insets_into_alt_dem(tile)
    return tile


@requires_gdal
def test_canopy_scale_offset_bakes_without_warning(
    tmp_path, monkeypatch, capsys
):
    # A ~5 m offset is the normal surface-vs-bare-earth gap (the coarse
    # base reads canopy): it must bake silently.  Two insets from one
    # provider stay below the systematic minimum of three.
    _bake_tile_with_insets(
        tmp_path,
        monkeypatch,
        [
            ("AAAA", "USGS3DEP", 0.10, 0.10, 0.25, 0.25, -5.0),
            ("BBBB", "USGS3DEP", 0.60, 0.60, 0.75, 0.75, -5.0),
        ],
    )
    assert "WARNING" not in capsys.readouterr().out


@requires_gdal
def test_datum_scale_offset_warns_per_inset(tmp_path, monkeypatch, capsys):
    _bake_tile_with_insets(
        tmp_path,
        monkeypatch,
        [("AAAA", "USGS3DEP", 0.10, 0.10, 0.25, 0.25, 25.0)],
    )
    output = capsys.readouterr().out
    assert "check vertical datum" in output


@requires_gdal
def test_systematic_provider_offset_warns_once(tmp_path, monkeypatch, capsys):
    # Three insets from ONE provider, all ~5 m below the base: each is
    # under the per-inset threshold, but the agreement in sign across
    # airports is the datum-bug signature and warns once for the provider.
    _bake_tile_with_insets(
        tmp_path,
        monkeypatch,
        [
            ("AAAA", "USGS3DEP", 0.10, 0.10, 0.22, 0.22, -5.0),
            ("BBBB", "USGS3DEP", 0.40, 0.40, 0.52, 0.52, -5.0),
            ("CCCC", "USGS3DEP", 0.70, 0.70, 0.82, 0.82, -5.0),
        ],
    )
    output = capsys.readouterr().out
    assert output.count("provider-wide vertical-datum problem") == 1
    assert "usgs3dep" in output
    # ...while the per-inset datum warning stays quiet at this magnitude.
    assert "check vertical datum" not in output


@requires_gdal
def test_alt_bake_is_noop_without_cached_insets(tmp_path, monkeypatch):
    import O4_DEM_Utils as DEM

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    INSETS.initialize_elevation_providers_dict()

    base_path = str(tmp_path / "base.tif")
    _write_constant_geotiff(
        base_path, 0.0, 0.0, 1.0, 1.0, 42.0, columns=50, rows=50
    )
    base_dem = DEM.DEM(0, 0, base_path, fill_nodata=False)
    before = base_dem.alt_dem.copy()

    tile = _FakeTile(0, 0)
    tile.dem = base_dem
    # No inset files cached -> bake must leave the raster untouched.
    INSETS.bake_airport_insets_into_alt_dem(tile)
    assert numpy.array_equal(base_dem.alt_dem, before)


# =====================================================================
# Automatic per-airport smoothing radius (spec section 3.4)
# =====================================================================
def test_smoothing_radius_rule_arithmetic():
    rule = INSETS.smoothing_radius_pixels_for_source
    working = 30.9
    # 30 m-class source (or the capped base path) -> unchanged.
    assert rule(8, 30.9, working) == 8
    assert rule(8, 30.0, working) == 8
    # 10 m source -> 3 pixels of 8.
    assert rule(8, 10.0, working) == 3
    # 3 m inset -> 1 pixel.
    assert rule(8, 3.0, working) == 1
    # 1 m inset -> 0 pixels (no blur -- the case measured to be harmful).
    assert rule(8, 1.0, working) == 0
    # Never exceeds today's radius, whatever the source claims.
    assert rule(8, 300.0, working) == 8
    # Degenerate inputs stay sane.
    assert rule(0, 3.0, working) == 0
    assert rule(8, 3.0, 0.0) == 8


class _RadiusTile(_FakeTile):
    def __init__(self, lat, lon):
        super().__init__(lat, lon)
        self.apt_smoothing_pix = 8
        self.apt_smoothing_auto = True


def test_override_precedence_beats_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    tile = _RadiusTile(0, 0)
    # The explicit per-airport override wins over the automatic rule...
    (radius, source_pixel, coverage) = INSETS.resolve_airport_smoothing_radius(
        tile, {"smoothing_pix": 13}, 30.9, None
    )
    assert (radius, source_pixel, coverage) == (13, None, None)
    # ...including an override of zero (explicitly no smoothing).
    assert INSETS.resolve_airport_smoothing_radius(
        tile, {"smoothing_pix": 0}, 30.9, None
    ) == (0, None, None)


def test_auto_gate_off_gives_legacy_radius(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    tile = _RadiusTile(0, 0)
    tile.apt_smoothing_auto = False
    assert INSETS.resolve_airport_smoothing_radius(
        tile, {}, 30.9, None
    ) == (8, None, None)
    # Insets gated off -> also the legacy radius, even with auto on.
    tile.apt_smoothing_auto = True
    tile.airport_elevation_insets = False
    assert INSETS.resolve_airport_smoothing_radius(
        tile, {}, 30.9, None
    ) == (8, None, None)


@requires_gdal
def test_coverage_threshold_behaviour(tmp_path, monkeypatch):
    from shapely import geometry as shapely_geometry

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    INSETS.initialize_elevation_providers_dict()
    tile = _RadiusTile(0, 0)
    working_pixel_m = 30.9

    # A 3 m-pixel inset covering [0.40, 0.60]^2 (tile-relative degrees).
    inset_directory = FNAMES.airport_inset_directory(0, 0)
    os.makedirs(inset_directory, exist_ok=True)
    inset_path = FNAMES.airport_inset_dem(0, 0, "COVR", "USGS3DEP")
    three_metres_in_degrees = 3.0 / 111120.0
    columns = int(round(0.2 / three_metres_in_degrees))
    _write_constant_geotiff(
        inset_path, 0.40, 0.40, 0.60, 0.60, 100.0,
        columns=columns, rows=columns,
    )

    # Mask fully inside the inset -> coverage 100 % -> 3 m rule -> 1 pixel.
    inner_mask = shapely_geometry.box(0.45, 0.45, 0.55, 0.55)
    (radius, source_pixel, coverage) = INSETS.resolve_airport_smoothing_radius(
        tile, {}, working_pixel_m, inner_mask
    )
    assert coverage == pytest.approx(1.0, abs=0.01)
    assert source_pixel == pytest.approx(3.0, abs=0.2)
    assert radius == 1

    # Mask half inside (50 % < the 80 % threshold) -> base path -> 8.
    straddling_mask = shapely_geometry.box(0.50, 0.45, 0.70, 0.55)
    (radius, source_pixel, coverage) = INSETS.resolve_airport_smoothing_radius(
        tile, {}, working_pixel_m, straddling_mask
    )
    assert coverage == pytest.approx(0.5, abs=0.02)
    assert source_pixel == pytest.approx(working_pixel_m)
    assert radius == 8

    # Mask 90 % inside (>= threshold) -> the inset rule applies.
    mostly_inside_mask = shapely_geometry.box(0.42, 0.45, 0.62, 0.55)
    (radius, source_pixel, coverage) = INSETS.resolve_airport_smoothing_radius(
        tile, {}, working_pixel_m, mostly_inside_mask
    )
    assert coverage == pytest.approx(0.9, abs=0.02)
    assert radius == 1


# =====================================================================
# Regression: empty base token in a composite resolves the default base
# (caught by the KBNA acceptance run: custom_dem="" plus inset
# augmentation produced ";inset1;..." whose empty first token fell
# through to read_elevation_from_file("") and an ALL-ZERO base raster).
# =====================================================================
@requires_gdal
def test_composite_with_empty_base_token_resolves_default_base(
    tmp_path, monkeypatch
):
    import O4_DEM_Utils as DEM

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    # The default base resolves to a synthetic 42 m raster.
    base_path = str(tmp_path / "base.tif")
    _write_constant_geotiff(
        base_path, 0.0, 0.0, 1.0, 1.0, 42.0, columns=60, rows=60
    )
    monkeypatch.setattr(
        DEM, "resolve_default_base_source", lambda lat, lon: base_path
    )
    # One cached inset at 100 m.
    inset_path = str(tmp_path / "inset.tif")
    _write_constant_geotiff(
        inset_path, 0.4, 0.4, 0.6, 0.6, 100.0, columns=40, rows=40
    )

    # The composite the step hooks assemble when custom_dem is "".
    dem = DEM.DEM(0, 0, ";" + inset_path, fill_nodata=False)
    # The BASE grid must be the resolved default, not zeros.
    assert dem.alt_dem.max() == pytest.approx(42.0)
    assert dem.alt_dem.min() == pytest.approx(42.0)
    # And the composite query path overlays the inset.
    assert dem.alt((0.5, 0.5)) == pytest.approx(100.0, abs=0.5)
    assert dem.alt((0.1, 0.1)) == pytest.approx(42.0, abs=0.5)


# =====================================================================
# Phase C1: densified working grid over inset tiles
# =====================================================================
def test_parse_working_grid_arc_seconds():
    parse = INSETS.parse_working_grid_arc_seconds
    assert parse("auto") == "auto"
    assert parse("") == "auto"
    assert parse("garbage") == "auto"
    assert parse("1") == 1
    assert parse("1/2") == 2
    assert parse("0.5") == 2
    assert parse("1/3") == 3
    assert parse("3") == 3


def test_resample_grid_by_factor_preserves_nodes_and_shape():
    grid = numpy.array(
        [[0.0, 3.0, 6.0], [9.0, 12.0, 15.0], [18.0, 21.0, 24.0]],
        dtype=numpy.float32,
    )
    # factor 1 is an exact identity (byte-path safety).
    assert numpy.array_equal(INSETS.resample_grid_by_factor(grid, 1), grid)
    dense = INSETS.resample_grid_by_factor(grid, 2)
    assert dense.shape == (5, 5)  # (n-1)*factor + 1
    # Every original node survives at its densified position...
    assert dense[0, 0] == 0.0 and dense[0, 4] == 6.0
    assert dense[4, 0] == 18.0 and dense[4, 4] == 24.0
    assert dense[2, 2] == pytest.approx(12.0)  # original centre node
    # ...and interpolated points are the linear midpoints (no new relief).
    assert dense[0, 1] == pytest.approx(1.5)
    assert dense[1, 0] == pytest.approx(4.5)
    dense3 = INSETS.resample_grid_by_factor(grid, 3)
    assert dense3.shape == (7, 7)


def test_smoothing_radius_preserves_physical_footprint_when_densified():
    """The densified path keeps the physical blur footprint via the
    reference pixel: a base-covered airport gets factor x apt_smoothing_pix
    pixels at the finer grid (same metres), an inset airport is unchanged."""
    rule = INSETS.smoothing_radius_pixels_for_source
    reference = 30.9  # one 1 arc-second pixel
    # Non-densified: reference defaults to working -> byte-identical.
    assert rule(8, 30.9, 30.9) == 8
    assert rule(8, 300.0, 30.9) == 8  # cap via min(source, reference)
    # Densified 1/3 (working 10.3 m) base source: the resolver passes the
    # reference pixel as the source for a base-covered airport, so the
    # radius is 24 pixels == 8 * 30.9 m (same physical footprint).
    working_dense = 30.9 / 3
    assert rule(8, reference, working_dense, reference) == 24
    # An inset source stays a small physical footprint at either grid.
    assert rule(8, 3.0, 30.9, 30.9) == 1
    assert rule(8, 3.0, working_dense, reference) == 2


def _fake_inset_tile(lat, lon, tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    INSETS.initialize_elevation_providers_dict()
    return _FakeTile(lat, lon)


class _GeometryDem:
    """Minimal stand-in for a loaded base DEM (geometry only)."""

    def __init__(self, alt_dem=None, combined=True):
        if combined:
            self.x0 = self.y0 = -0.01
            self.x1 = self.y1 = 1.01
            self.nxdem = self.nydem = 3673
        else:
            self.x0 = self.y0 = 0.0
            self.x1 = self.y1 = 1.0
            self.nxdem = self.nydem = 3601
        self.alt_dem = alt_dem


def test_working_grid_factor_is_one_without_insets(tmp_path, monkeypatch):
    """Byte-path posture: auto resolves to 1 arc-second with no insets."""
    tile = _fake_inset_tile(0, 0, tmp_path, monkeypatch)
    # No cached inset directory at all -> factor 1 (byte-identical path).
    assert INSETS.resolve_working_grid_factor(tile, _GeometryDem()) == 1
    # Gate off -> factor 1 even if a stray inset were present.
    tile.airport_elevation_insets = False
    assert INSETS.resolve_working_grid_factor(tile, _GeometryDem()) == 1


@requires_gdal
def test_working_grid_factor_auto_picks_coarsest_passing(tmp_path, monkeypatch):
    """A seeded probe over a modelled scarp: auto picks the coarsest grid
    whose ideal-bake error is within tolerance, and an explicit pin wins."""
    tile = _fake_inset_tile(36, -87, tmp_path, monkeypatch)
    inset_directory = FNAMES.airport_inset_directory(36, -87)
    os.makedirs(inset_directory, exist_ok=True)
    inset_path = FNAMES.airport_inset_dem(36, -87, "KBNA", "USGS3DEP")
    # A step scarp inside the KBNA seed-probe footprint: west half low,
    # east half high, so a coarse grid straddling it smears the probes.
    driver = gdal.GetDriverByName("GTiff")
    columns = rows = 400
    west, south, east, north = -86.72, 36.10, -86.62, 36.16
    dataset = driver.Create(inset_path, columns, rows, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform(
        (west, (east - west) / columns, 0, north, 0, (south - north) / rows)
    )
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-32768.0)
    values = numpy.full((rows, columns), 150.0, dtype=numpy.float32)
    scarp_column = int((-86.676 - west) / (east - west) * columns)
    values[:, scarp_column:] = 167.0
    band.WriteArray(values)
    band.FlushCache()
    dataset = None

    factor = INSETS.resolve_working_grid_factor(tile, _GeometryDem())
    assert factor in (2, 3)  # insets present -> always densified
    # Explicit pins bypass the ideal check entirely.
    tile.working_grid_arc_seconds = "1"
    assert INSETS.resolve_working_grid_factor(tile, _GeometryDem()) == 1
    tile.working_grid_arc_seconds = "1/3"
    assert INSETS.resolve_working_grid_factor(tile, _GeometryDem()) == 3


@requires_gdal
def test_ideal_bake_error_decreases_with_finer_grid(tmp_path, monkeypatch):
    """The modelled error is monotone in the grid: finer never worse."""
    _fake_inset_tile(0, 0, tmp_path, monkeypatch)
    inset_path = str(tmp_path / "scarp.tif")
    driver = gdal.GetDriverByName("GTiff")
    columns = rows = 300
    _make = _write_constant_geotiff  # reuse extent conventions
    _make(inset_path, 0.30, 0.30, 0.70, 0.70, 150.0, columns=columns, rows=rows)
    dataset = gdal.Open(inset_path, gdal.GA_Update)
    array = dataset.GetRasterBand(1).ReadAsArray()
    array[:, columns // 2 :] = 170.0  # a 20 m scarp down the middle
    dataset.GetRasterBand(1).WriteArray(array)
    dataset.FlushCache()
    dataset = None
    probe_lat, probe_lon = 0.50, 0.5003  # a few metres east of the scarp
    probes = [(probe_lon, probe_lat, probe_lon, probe_lat)]
    geometry = (-0.01, 1.01, -0.01, 1.01, 3673, 3673)
    error1 = INSETS.ideal_bake_error_at_probes(inset_path, probes, 1, geometry)
    error2 = INSETS.ideal_bake_error_at_probes(inset_path, probes, 2, geometry)
    error3 = INSETS.ideal_bake_error_at_probes(inset_path, probes, 3, geometry)
    assert error1 >= error2 >= error3


@requires_gdal
def test_densify_tile_dem_noop_and_active(tmp_path, monkeypatch):
    """densify_tile_dem_for_insets is a byte-identity no-op without insets
    and resamples to the dense grid (updating nxdem/nydem) with one."""
    import O4_DEM_Utils as DEM

    tile = _fake_inset_tile(0, 0, tmp_path, monkeypatch)
    base_path = str(tmp_path / "base.tif")
    _write_constant_geotiff(
        base_path, 0.0, 0.0, 1.0, 1.0, 10.0, columns=101, rows=101
    )
    # No inset -> factor 1, grid and array untouched (byte-path posture).
    tile.dem = DEM.DEM(0, 0, base_path, fill_nodata=False)
    original_columns = tile.dem.nxdem
    original_rows = tile.dem.nydem
    before = tile.dem.alt_dem.copy()
    assert INSETS.densify_tile_dem_for_insets(tile) == 1
    assert tile.dem.nxdem == original_columns
    assert tile.dem.nydem == original_rows
    assert numpy.array_equal(tile.dem.alt_dem, before)

    # With a cached inset the grid is densified to the pinned factor.
    inset_directory = FNAMES.airport_inset_directory(0, 0)
    os.makedirs(inset_directory, exist_ok=True)
    _write_constant_geotiff(
        FNAMES.airport_inset_dem(0, 0, "TEST", "USGS3DEP"),
        0.4, 0.4, 0.6, 0.6, 100.0, columns=60, rows=60,
    )
    tile.working_grid_arc_seconds = "1/2"
    tile.dem = DEM.DEM(0, 0, base_path, fill_nodata=False)
    factor = INSETS.densify_tile_dem_for_insets(tile)
    assert factor == 2
    assert tile.dem.nxdem == (original_columns - 1) * 2 + 1
    assert tile.dem.nydem == (original_rows - 1) * 2 + 1
    assert tile.dem.alt_dem.shape == (tile.dem.nydem, tile.dem.nxdem)
    # The upsampled base still reads its flat 10 m value at the corners.
    assert tile.dem.alt_dem[0, 0] == pytest.approx(10.0)


# =====================================================================
# Phase C2: second provider family (STAC), no network
# =====================================================================
# A fixture STAC ItemCollection like the Natural Resources Canada HRDEM
# datacube returns for a bbox search: two items, the first exposing both a
# bare-earth DTM and a surface DSM, the second only a generically-named
# GeoTIFF data asset.
_STAC_ITEMCOLLECTION_FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {
            "id": "hrdem-lidar-item-a",
            "properties": {"gsd": 1.0},
            "assets": {
                "dsm": {
                    "href": "https://example.ca/tile_a_dsm.tif",
                    "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                    "roles": ["data", "dsm"],
                },
                "dtm": {
                    "href": "https://example.ca/tile_a_dtm.tif",
                    "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                    "roles": ["data", "dtm"],
                },
            },
        },
        {
            "id": "hrdem-lidar-item-b",
            "properties": {"resolution": 2.0},
            "assets": {
                "data": {
                    "href": "s3://hrdem-bucket/tile_b.tif",
                    "type": "image/tiff; application=geotiff",
                    "roles": ["data"],
                }
            },
        },
    ],
}


def test_stac_search_payload_parsing():
    parse = INSETS.StacCloudOptimizedGeoTiffStrategy._parse_search_payload
    items = parse(_STAC_ITEMCOLLECTION_FIXTURE)
    assert [item["id"] for item in items] == [
        "hrdem-lidar-item-a",
        "hrdem-lidar-item-b",
    ]
    # An empty / malformed response yields no items (no coverage).
    assert parse({"type": "FeatureCollection", "features": []}) is None
    assert parse({}) is None
    assert parse("not a dict") is None
    # A server that keys the list "items" instead of "features" still parses.
    assert (
        parse({"items": _STAC_ITEMCOLLECTION_FIXTURE["features"]})[0]["id"]
        == "hrdem-lidar-item-a"
    )


def test_stac_asset_selection_prefers_dtm():
    items = _STAC_ITEMCOLLECTION_FIXTURE["features"]
    selected = INSETS._select_stac_dtm_assets(items, ["dtm"])
    # Item A: the DTM wins over the DSM by explicit preference.
    assert selected[0][0] == "https://example.ca/tile_a_dtm.tif"
    assert selected[0][1] == 1.0  # gsd carried through
    # Item B: no dtm key -> falls back to the generic GeoTIFF data asset.
    assert selected[1][0] == "s3://hrdem-bucket/tile_b.tif"
    assert selected[1][1] == 2.0  # resolution property carried through
    # With no preference given, a DTM-roled/keyed asset is still chosen.
    fallback = INSETS._select_stac_dtm_assets(items, [])
    assert fallback[0][0] == "https://example.ca/tile_a_dtm.tif"


def test_stac_asset_href_to_vsicurl():
    convert = INSETS._stac_asset_href_to_vsicurl
    assert convert("https://x/y.tif") == "/vsicurl/https://x/y.tif"
    assert convert("http://x/y.tif") == "/vsicurl/http://x/y.tif"
    assert convert("s3://bucket/key.tif") == "/vsis3/bucket/key.tif"
    assert convert("/vsicurl/https://x/y.tif") == "/vsicurl/https://x/y.tif"


def test_stac_strategy_registered_and_dispatches(tmp_path, monkeypatch):
    """The REAL second strategy is in the registry and is dispatched by the
    orchestration's fetch_inset with zero orchestration change -- discovery
    and the warp core are stubbed so no network / GDAL is touched."""
    assert "stac" in INSETS.ACCESS_STRATEGIES

    monkeypatch.setattr(
        INSETS.StacCloudOptimizedGeoTiffStrategy,
        "discover",
        lambda self, definition, bbox: _STAC_ITEMCOLLECTION_FIXTURE[
            "features"
        ],
    )
    monkeypatch.setattr(INSETS, "has_gdal", True)

    warp_calls = {}

    def _fake_warp(
        vsicurl_inputs, bbox, resolution_m, destination_path, **keyword_arguments
    ):
        warp_calls["inputs"] = list(vsicurl_inputs)
        with open(destination_path, "wb") as handle:
            handle.write(b"stub-geotiff")
        return True

    monkeypatch.setattr(
        INSETS, "warp_vsicurl_sources_to_geotiff", _fake_warp
    )

    definition = {
        "code": "HRDEM",
        "access_strategy": "stac",
        "role": INSETS.ROLE_AIRPORT_INSET,
        "enabled": True,
        "priority": 90.0,
        "collections": "hrdem-lidar",
        "dtm_asset_keys": "dtm",
        "license": "Open Government Licence - Canada",
        "attribution": "Natural Resources Canada",
        "vertical_datum": "CGVD2013",
    }
    destination = str(tmp_path / "CYVR_hrdem.tif")
    provenance = INSETS.fetch_inset(
        definition, (-123.20, 49.18, -123.16, 49.21), 3.0, destination
    )
    assert provenance is not None
    assert provenance["provider"] == "HRDEM"
    assert provenance["access_strategy"] == "stac"
    assert provenance["attribution"] == "Natural Resources Canada"
    assert provenance["vertical_datum"] == "CGVD2013"
    assert provenance["native_resolution_m"] == 1.0  # finest selected asset
    assert os.path.isfile(destination)
    # The DTM (item A) and the fallback data asset (item B) were mosaicked,
    # the DTM's vsicurl and the s3 vsis3 path both present.
    assert "/vsicurl/https://example.ca/tile_a_dtm.tif" in warp_calls["inputs"]
    assert "/vsis3/hrdem-bucket/tile_b.tif" in warp_calls["inputs"]


def test_hrdem_definition_ships_and_is_selectable():
    """The shipped HRDEM.elv parses with role=airport_inset + stac strategy
    and is picked up by auto inset selection alongside USGS3DEP."""
    INSETS.initialize_elevation_providers_dict()
    assert "HRDEM" in INSETS.elevation_providers_dict
    hrdem = INSETS.elevation_providers_dict["HRDEM"]
    assert hrdem["access_strategy"] == "stac"
    assert hrdem["role"] == INSETS.ROLE_AIRPORT_INSET
    assert hrdem["collections"] == "hrdem-lidar"
    codes = [d["code"] for d in INSETS.select_provider_definitions("auto")]
    assert "HRDEM" in codes and "USGS3DEP" in codes
    # USGS3DEP (100) outranks HRDEM (90) in the auto ordering.
    assert codes.index("USGS3DEP") < codes.index("HRDEM")


# =====================================================================
# Inset-derived water supplement (hydro-flat basins)
# =====================================================================


def _write_terrain_geotiff(path, west, south, east, north, values):
    """Write an arbitrary float32 terrain array as an EPSG:4326 GeoTIFF."""
    rows, columns = values.shape
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, columns, rows, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform((
        west, (east - west) / columns, 0,
        north, 0, (south - north) / rows,
    ))
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-32768.0)
    band.WriteArray(values.astype(numpy.float32))
    band.FlushCache()
    dataset = None
    return path


def _basin_terrain(pond_value=95.0, ground_value=100.0, noise_amplitude=0.0):
    """200x200 cell terrain at ``ground_value`` with a 60x60 basin whose
    floor sits at ``pond_value`` (optionally with deterministic noise),
    plus a same-size flat PAD at ground level in the opposite corner
    (level with its surroundings — must never become water)."""
    values = numpy.full((200, 200), ground_value, dtype=numpy.float64)
    values[40:100, 40:100] = pond_value
    if noise_amplitude:
        rng = numpy.random.default_rng(20260714)
        values[40:100, 40:100] += rng.uniform(
            -noise_amplitude, noise_amplitude, (60, 60))
    values[140:190, 140:190] = ground_value  # the pad (a no-op by value)
    return values


@requires_gdal
def test_strict_tier_detects_basin_and_ignores_level_pad(tmp_path):
    # ~1.1 m cells: 200 cells over 0.002 degrees.
    path = str(tmp_path / "airport_provider.tif")
    _write_terrain_geotiff(
        path, -87.001, 36.099, -86.999, 36.101, _basin_terrain())
    rings = INSETS.detect_hydro_flat_water_rings(path)
    assert len(rings) == 1
    ring, water_elevation = rings[0]
    assert water_elevation == pytest.approx(95.0, abs=0.01)
    longitudes = [point[0] for point in ring]
    latitudes = [point[1] for point in ring]
    # The ring sits inside the basin (drawn one cell inside the shore).
    assert min(longitudes) >= -87.001 + 40 * 1e-5 - 1e-6
    assert max(longitudes) <= -87.001 + 100 * 1e-5 + 1e-6
    assert min(latitudes) >= 36.101 - 100 * 1e-5 - 1e-6
    assert max(latitudes) <= 36.101 - 40 * 1e-5 + 1e-6


@requires_gdal
def test_noisy_basin_needs_the_facility_scope(tmp_path):
    from shapely.geometry import box as shapely_box

    path = str(tmp_path / "airport_provider.tif")
    _write_terrain_geotiff(
        path, -87.001, 36.099, -86.999, 36.101,
        _basin_terrain(noise_amplitude=0.2))
    # The noisy working pond fails the strict tier...
    assert INSETS.detect_hydro_flat_water_rings(path) == []
    # ...and is traced by the facility-scoped tier inside its outline.
    loaded = INSETS._load_inset_raster(path)
    values, valid, geotransform = loaded
    facility = shapely_box(-87.0008, 36.0997, -86.9993, 36.1008)
    mask = INSETS._facility_restrict_mask(
        [facility], values.shape, geotransform)
    assert mask is not None and mask.any()
    rings = INSETS._detect_water_components(
        values, valid, geotransform,
        minimum_area_m2=INSETS.INSET_WATER_FACILITY_MINIMUM_AREA_M2,
        local_flatness_m=INSETS.INSET_WATER_FACILITY_LOCAL_FLATNESS_M,
        component_range_m=INSETS.INSET_WATER_FACILITY_COMPONENT_RANGE_M,
        rim_rise_m=INSETS.INSET_WATER_FACILITY_RIM_RISE_M,
        plausibility_band_m=INSETS.INSET_WATER_PLAUSIBILITY_BAND_M,
        restrict_mask=mask)
    assert len(rings) == 1
    assert rings[0][1] == pytest.approx(95.0, abs=0.3)


@requires_gdal
def test_void_fill_plateau_is_rejected(tmp_path):
    # An exact-flat plateau 60 m below everything (the KBNA 40 m void
    # artefact class) must never become water.
    path = str(tmp_path / "airport_provider.tif")
    _write_terrain_geotiff(
        path, -87.001, 36.099, -86.999, 36.101,
        _basin_terrain(pond_value=40.0))
    assert INSETS.detect_hydro_flat_water_rings(path) == []


@requires_gdal
def test_supplement_written_and_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    # No network in tests: the facility fetch is stubbed empty.
    monkeypatch.setattr(
        INSETS, "_facility_outline_polygons", lambda lat, lon: [])
    directory = FNAMES.airport_inset_directory(36, -87)
    os.makedirs(directory)
    _write_terrain_geotiff(
        os.path.join(directory, "KTST_provider.tif"),
        -87.001, 36.099, -86.999, 36.101, _basin_terrain())
    supplement = INSETS.ensure_inset_water_supplement(36, -87)
    assert supplement and os.path.isfile(supplement)
    import bz2

    content = bz2.open(supplement, "rt").read()
    assert content.count("<way") == 1
    assert 'k="natural" v="water"' in content
    # Cached: a second call reuses the file (same mtime).
    first_mtime = os.path.getmtime(supplement)
    assert INSETS.ensure_inset_water_supplement(36, -87) == supplement
    assert os.path.getmtime(supplement) == first_mtime


@requires_gdal
def test_supplement_refuses_surface_model_and_upsampled_rasters(
        tmp_path, monkeypatch):
    """The SPJC live regression (2026-07-18): Copernicus GLO-30 insets —
    a 30 m surface model fetched at 3 m with building footprints
    interpolated away — produced 1060 phantom water basins over urban
    Lima and Callao.  A raster whose provenance sidecar declares
    surface-model building masking, or an upsampled fetch (native
    resolution coarser than the fetched one), is excluded from water
    detection outright; a downsampled lidar raster keeps it."""
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(
        INSETS, "_facility_outline_polygons", lambda lat, lon: [])
    directory = FNAMES.airport_inset_directory(36, -87)
    os.makedirs(directory)
    tif_path = os.path.join(directory, "KTST_provider.tif")
    sidecar_path = os.path.join(directory, "KTST_provider.json")
    _write_terrain_geotiff(
        tif_path, -87.001, 36.099, -86.999, 36.101, _basin_terrain())

    # Surface-model building masking declared: refused.
    with open(sidecar_path, "w") as handle:
        json.dump({INSETS.SURFACE_MODEL_BUILDING_MASKING: {
            "masked_fraction": 0.29}}, handle)
    assert not INSETS._water_detection_trusts_inset_raster(tif_path)
    assert INSETS.ensure_inset_water_supplement(36, -87) is None
    assert not os.path.isfile(FNAMES.inset_water(36, -87))

    # Upsampled fetch (30 m native at 3 m): refused.
    with open(sidecar_path, "w") as handle:
        json.dump({"native_resolution_m": 30.0, "resolution_m": 3.0},
                  handle)
    assert not INSETS._water_detection_trusts_inset_raster(tif_path)
    assert INSETS.ensure_inset_water_supplement(36, -87) is None

    # Downsampled lidar (1 m native at 3 m): trusted, basin detected.
    with open(sidecar_path, "w") as handle:
        json.dump({"native_resolution_m": 1.0, "resolution_m": 3.0},
                  handle)
    assert INSETS._water_detection_trusts_inset_raster(tif_path)
    assert INSETS.ensure_inset_water_supplement(36, -87) is not None


@requires_gdal
def test_old_schema_supplement_regenerates_despite_fresh_mtime(
        tmp_path, monkeypatch):
    """A poisoned supplement written UNDER OLD RULES can be newer than
    every raster (it is written after them in the same build) — the
    schema stamp in the generator attribute must force regeneration."""
    import bz2

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(
        INSETS, "_facility_outline_polygons", lambda lat, lon: [])
    directory = FNAMES.airport_inset_directory(36, -87)
    os.makedirs(directory)
    tif_path = os.path.join(directory, "KTST_provider.tif")
    _write_terrain_geotiff(
        tif_path, -87.001, 36.099, -86.999, 36.101, _basin_terrain())
    # A pre-schema supplement carrying a phantom ring, newer than the
    # raster.
    supplement_path = FNAMES.inset_water(36, -87)
    with bz2.open(supplement_path, "wt", encoding="utf-8") as handle:
        handle.write(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<osm version="0.6" generator="O4_Airport_Elevation_Insets">\n'
            "</osm>\n")
    assert not INSETS._inset_water_supplement_schema_current(
        supplement_path)
    regenerated = INSETS.ensure_inset_water_supplement(36, -87)
    assert regenerated == supplement_path
    content = bz2.open(supplement_path, "rt").read()
    assert INSETS.INSET_WATER_SUPPLEMENT_SCHEMA in content
    assert content.count("<way") == 1
    # And the freshly written schema-current supplement is reused as-is.
    first_mtime = os.path.getmtime(supplement_path)
    assert INSETS.ensure_inset_water_supplement(36, -87) == supplement_path
    assert os.path.getmtime(supplement_path) == first_mtime


@requires_gdal
def test_supplement_removed_when_nothing_qualifies(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(
        INSETS, "_facility_outline_polygons", lambda lat, lon: [])
    directory = FNAMES.airport_inset_directory(36, -87)
    os.makedirs(directory)
    tif_path = os.path.join(directory, "KTST_provider.tif")
    _write_terrain_geotiff(
        tif_path, -87.001, 36.099, -86.999, 36.101, _basin_terrain())
    supplement = INSETS.ensure_inset_water_supplement(36, -87)
    assert supplement is not None
    # Flatten the basin away and touch the raster: the stale supplement
    # is regenerated to nothing and removed.
    _write_terrain_geotiff(
        tif_path, -87.001, 36.099, -86.999, 36.101,
        numpy.full((200, 200), 100.0))
    os.utime(tif_path, None)
    assert INSETS.ensure_inset_water_supplement(36, -87) is None
    assert not os.path.isfile(FNAMES.inset_water(36, -87))


# =====================================================================
# The wcs access strategy (OGC Web Coverage Service national coverages)
# =====================================================================
def _wcs_definition(**overrides):
    definition = {
        "code": "TESTWCS",
        "access_strategy": "wcs",
        "wcs_service_url": "https://example.test/wcs",
        "wcs_version": "2.0.1",
        "wcs_coverage": "national__DTM_1m",
        "native_resolution_m": "1",
        "license": "test licence",
        "attribution": "test agency",
        "vertical_datum": "TESTDATUM",
    }
    definition.update(overrides)
    return definition


def test_wcs_dataset_name_construction():
    strategy = INSETS.ACCESS_STRATEGIES["wcs"]()
    assert strategy.dataset_name(_wcs_definition()) == (
        "WCS:https://example.test/wcs"
        "?version=2.0.1&coverage=national__DTM_1m"
    )
    # A service URL already carrying a query string continues with '&'
    # (the MapServer style, e.g. the Geonorge endpoints).
    assert strategy.dataset_name(
        _wcs_definition(wcs_service_url="https://example.test/wcs?map=dtm")
    ) == (
        "WCS:https://example.test/wcs?map=dtm"
        "&version=2.0.1&coverage=national__DTM_1m"
    )


def test_wcs_discover_honours_coverage_bbox():
    strategy = INSETS.ACCESS_STRATEGIES["wcs"]()
    definition = _wcs_definition(coverage_bbox=(-6.5, 49.8, 1.9, 55.9))
    heathrow = (-0.49, 51.44, -0.41, 51.49)
    doha = (51.55, 25.24, 51.65, 25.29)
    assert strategy.discover(definition, heathrow) == [
        {
            "dataset": "WCS:https://example.test/wcs"
            "?version=2.0.1&coverage=national__DTM_1m"
        }
    ]
    assert strategy.discover(definition, doha) is None


def _stub_wcs_open(monkeypatch, open_calls=None):
    """Replace ``gdal.OpenEx`` with a network-free stub for WCS tests.

    Returns the sentinel object the stub hands back, so tests can assert
    the opened dataset (not the connection string) reaches the warp.
    """
    opened_dataset = object()

    def _fake_open(dataset_name, flags=0, open_options=None, **_keywords):
        if open_calls is not None:
            open_calls.append(
                {"dataset_name": dataset_name, "open_options": open_options}
            )
        return opened_dataset

    monkeypatch.setattr(INSETS.gdal, "OpenEx", _fake_open)
    return opened_dataset


@requires_gdal
def test_wcs_fetch_writes_inset_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(INSETS, "has_gdal", True)
    open_calls = []
    opened_dataset = _stub_wcs_open(monkeypatch, open_calls)
    warp_calls = {}

    def _fake_warp(inputs, bounding_box, resolution, destination, **keyword_arguments):
        warp_calls["inputs"] = list(inputs)
        (west, south, east, north) = bounding_box
        _write_constant_geotiff(
            destination, west, south, east, north, 42.0
        )
        return True

    monkeypatch.setattr(
        INSETS, "warp_vsicurl_sources_to_geotiff", _fake_warp
    )
    definition = _wcs_definition()
    destination = str(tmp_path / "EGLL_testwcs.tif")
    provenance = INSETS.fetch_inset(
        definition, (-0.49, 51.44, -0.41, 51.49), 1.0, destination
    )
    assert provenance is not None
    assert os.path.isfile(destination)
    # The strategy opens the coverage itself (to pass the request-timeout
    # open option) and hands the OPENED dataset to the warp.
    assert open_calls[0]["dataset_name"] == (
        "WCS:https://example.test/wcs"
        "?version=2.0.1&coverage=national__DTM_1m"
    )
    assert open_calls[0]["open_options"] == [
        "TIMEOUT=%d" % INSETS.WCS_REQUEST_TIMEOUT_SECONDS
    ]
    assert warp_calls["inputs"] == [opened_dataset]
    assert provenance["provider"] == "TESTWCS"
    assert provenance["access_strategy"] == "wcs"
    assert provenance["wcs_coverage"] == "national__DTM_1m"
    assert provenance["vertical_datum"] == "TESTDATUM"


@requires_gdal
def test_wcs_all_nodata_window_is_no_coverage(tmp_path, monkeypatch):
    # An airport inside the coverage_bbox but outside the national data
    # extent warps to all nodata: the strategy must delete the file and
    # report no coverage (so the orchestration caches the negative).
    monkeypatch.setattr(INSETS, "has_gdal", True)
    _stub_wcs_open(monkeypatch)

    def _fake_warp(inputs, bounding_box, resolution, destination, **keyword_arguments):
        (west, south, east, north) = bounding_box
        _write_constant_geotiff(
            destination, west, south, east, north, -32768.0
        )
        return True

    monkeypatch.setattr(
        INSETS, "warp_vsicurl_sources_to_geotiff", _fake_warp
    )
    destination = str(tmp_path / "EGXX_testwcs.tif")
    provenance = INSETS.fetch_inset(
        _wcs_definition(), (-3.0, 52.0, -2.9, 52.1), 1.0, destination
    )
    assert provenance is None
    assert not os.path.exists(destination)


@requires_gdal
def test_wcs_failed_warp_is_no_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(INSETS, "has_gdal", True)
    _stub_wcs_open(monkeypatch)
    monkeypatch.setattr(
        INSETS,
        "warp_vsicurl_sources_to_geotiff",
        lambda *arguments, **keyword_arguments: False,
    )
    destination = str(tmp_path / "EGLL_testwcs.tif")
    assert (
        INSETS.fetch_inset(
            _wcs_definition(), (-0.49, 51.44, -0.41, 51.49), 1.0, destination
        )
        is None
    )


@requires_gdal
def test_wcs_open_timeout_raises_transient_fetch_error(tmp_path, monkeypatch):
    # The exact libcurl total-transfer timeout message from the EGLL /
    # ENGLAND1M failure: a transient network answer, never a durable
    # no-coverage one.
    monkeypatch.setattr(INSETS, "has_gdal", True)

    def _timeout_open(*arguments, **keyword_arguments):
        raise RuntimeError(
            "HTTP error: Operation timed out after 30000 milliseconds"
            " with 20607784 bytes received"
        )

    monkeypatch.setattr(INSETS.gdal, "OpenEx", _timeout_open)
    with pytest.raises(INSETS.TransientFetchError):
        INSETS.fetch_inset(
            _wcs_definition(),
            (-0.49, 51.44, -0.41, 51.49),
            1.0,
            str(tmp_path / "EGLL_testwcs.tif"),
        )


@requires_gdal
def test_wcs_durable_open_failure_is_no_coverage(tmp_path, monkeypatch):
    monkeypatch.setattr(INSETS, "has_gdal", True)

    def _broken_open(*arguments, **keyword_arguments):
        raise RuntimeError("Unable to parse coverage description")

    monkeypatch.setattr(INSETS.gdal, "OpenEx", _broken_open)
    assert (
        INSETS.fetch_inset(
            _wcs_definition(),
            (-0.49, 51.44, -0.41, 51.49),
            1.0,
            str(tmp_path / "EGLL_testwcs.tif"),
        )
        is None
    )


@requires_gdal
def test_warp_curl_timeout_raises_transient_fetch_error(tmp_path, monkeypatch):
    def _timeout_warp(*arguments, **keyword_arguments):
        raise RuntimeError(
            "Operation timed out after 30000 milliseconds with"
            " 20607784 bytes received"
        )

    monkeypatch.setattr(INSETS.gdal, "Warp", _timeout_warp)
    with pytest.raises(INSETS.TransientFetchError):
        INSETS.warp_vsicurl_sources_to_geotiff(
            ["/vsicurl/https://example.test/tile.tif"],
            (-0.49, 51.44, -0.41, 51.49),
            1.0,
            str(tmp_path / "out.tif"),
        )


@requires_gdal
def test_warp_durable_failure_still_returns_false(tmp_path, monkeypatch):
    def _broken_warp(*arguments, **keyword_arguments):
        raise RuntimeError("Unsupported band data type")

    monkeypatch.setattr(INSETS.gdal, "Warp", _broken_warp)
    assert (
        INSETS.warp_vsicurl_sources_to_geotiff(
            ["/vsicurl/https://example.test/tile.tif"],
            (-0.49, 51.44, -0.41, 51.49),
            1.0,
            str(tmp_path / "out.tif"),
        )
        is False
    )


def test_transient_fetch_failure_is_not_cached_as_negative(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    fetch_calls = {"count": 0}

    @INSETS.register_access_strategy("transient_failure_strategy")
    class _TransientFailureStrategy:
        def discover(self, definition, bounding_box_wgs84):
            return [{}]

        def fetch(self, definition, bbox, resolution_m, destination_path):
            fetch_calls["count"] += 1
            raise INSETS.TransientFetchError(
                "Operation timed out after 30000 milliseconds"
            )

    try:
        definition = {
            "code": "FLAKY",
            "access_strategy": "transient_failure_strategy",
            "role": INSETS.ROLE_AIRPORT_INSET,
            "enabled": True,
            "priority": 1.0,
        }
        boxes = {"EGLL": (-0.49, 51.44, -0.41, 51.49)}

        first = INSETS.ensure_airport_insets(51, -1, boxes, [definition], 3.0)
        # No durable record: neither "ok" nor a no-coverage negative.
        assert first["EGLL"].get("FLAKY") is None
        assert fetch_calls["count"] == 1

        # A later run retries the fetch (a no-coverage negative would
        # have blocked it, as test_negative_result_is_cached... proves).
        INSETS.ensure_airport_insets(51, -1, boxes, [definition], 3.0)
        assert fetch_calls["count"] == 2
    finally:
        INSETS.ACCESS_STRATEGIES.pop("transient_failure_strategy", None)


# =====================================================================
# STAC asset selection: filename-keyed multi-resolution assets
# =====================================================================
def test_stac_asset_selection_picks_finest_geotiff():
    # swisstopo-shaped item: no "dtm" asset key, two GeoTIFF assets of
    # the same tile at different ground sample distances, keyed by file
    # name and carrying their own eo:gsd.  The 2 m asset deliberately
    # comes FIRST so dictionary order alone would pick the wrong one.
    items = [
        {
            "id": "swissalti3d_2020_2683-1257",
            "properties": {},
            "assets": {
                "tile_2_2056.tif": {
                    "href": "https://example.test/tile_2.tif",
                    "type": (
                        "image/tiff; application=geotiff; "
                        "profile=cloud-optimized"
                    ),
                    "eo:gsd": 2.0,
                },
                "tile_0.5_2056.tif": {
                    "href": "https://example.test/tile_05.tif",
                    "type": (
                        "image/tiff; application=geotiff; "
                        "profile=cloud-optimized"
                    ),
                    "eo:gsd": 0.5,
                },
                "tile_0.5_2056.xyz.zip": {
                    "href": "https://example.test/tile_05.xyz.zip",
                    "type": "application/x.ascii-xyz+zip",
                    "eo:gsd": 0.5,
                },
            },
        }
    ]
    chosen = INSETS._select_stac_dtm_assets(items, prefer_asset_keys=[])
    assert chosen == [("https://example.test/tile_05.tif", 0.5)]


def test_stac_asset_selection_prefers_named_dtm_key():
    # The HRDEM shape is untouched: an explicit "dtm" key wins even when
    # a finer GeoTIFF asset exists under another key.
    items = [
        {
            "id": "hrdem-item",
            "properties": {"gsd": 1.0},
            "assets": {
                "dsm-finer": {
                    "href": "https://example.test/dsm.tif",
                    "type": "image/tiff; application=geotiff",
                    "eo:gsd": 0.5,
                },
                "dtm": {
                    "href": "https://example.test/dtm.tif",
                    "type": "image/tiff; application=geotiff",
                },
            },
        }
    ]
    chosen = INSETS._select_stac_dtm_assets(items, prefer_asset_keys=["dtm"])
    assert chosen == [("https://example.test/dtm.tif", 1.0)]


# =====================================================================
# The direct_cog strategy (fixed country-wide GeoTIFF URLs)
# =====================================================================
@requires_gdal
def test_direct_cog_fetch_and_bbox_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(INSETS, "has_gdal", True)
    warp_calls = {}

    def _fake_warp(inputs, bounding_box, resolution, destination, **keyword_arguments):
        warp_calls["inputs"] = list(inputs)
        (west, south, east, north) = bounding_box
        _write_constant_geotiff(destination, west, south, east, north, 60.0)
        return True

    monkeypatch.setattr(
        INSETS, "warp_vsicurl_sources_to_geotiff", _fake_warp
    )
    definition = {
        "code": "TESTCOG",
        "access_strategy": "direct_cog",
        "cog_urls": "https://example.test/a.tif, s3://bucket/b.tif",
        "coverage_bbox": (-5.5, 51.3, -2.6, 53.5),
        "vertical_datum": "ODN",
    }
    cardiff = (-3.35, 51.39, -3.33, 51.40)
    doha = (51.55, 25.24, 51.65, 25.29)
    strategy = INSETS.ACCESS_STRATEGIES["direct_cog"]()
    assert strategy.discover(definition, doha) is None
    destination = str(tmp_path / "EGFF_testcog.tif")
    provenance = INSETS.fetch_inset(definition, cardiff, 1.0, destination)
    assert provenance is not None
    # Both URL forms map onto GDAL virtual paths, order preserved.
    assert warp_calls["inputs"] == [
        "/vsicurl/https://example.test/a.tif",
        "/vsis3/bucket/b.tif",
    ]


# =====================================================================
# The static_stac strategy (catalog walking + persistent index)
# =====================================================================
_STATIC_ROOT = "https://static.test/catalog.json"
_STATIC_TREE = {
    _STATIC_ROOT: {
        "links": [
            {"rel": "child", "href": "./north/dem_1m/collection.json"},
            {"rel": "child", "href": "./north/dsm_1m/collection.json"},
            {"rel": "child", "href": "./south/dem_1m/collection.json"},
        ]
    },
    "https://static.test/north/dem_1m/collection.json": {
        "extent": {"spatial": {"bbox": [[174.0, -37.2, 175.0, -36.0]]}},
        "links": [
            {"rel": "item", "href": "./tile_a.json"},
            {"rel": "item", "href": "./tile_b.json"},
        ],
    },
    "https://static.test/south/dem_1m/collection.json": {
        "extent": {"spatial": {"bbox": [[167.0, -47.0, 169.0, -44.0]]}},
        "links": [{"rel": "item", "href": "./far.json"}],
    },
    "https://static.test/north/dem_1m/tile_a.json": {
        "bbox": [174.7, -37.1, 174.9, -36.9],
        "assets": {
            "visual": {
                "href": "./tile_a.tiff",
                "type": "image/tiff; application=geotiff",
            }
        },
    },
    "https://static.test/north/dem_1m/tile_b.json": {
        "bbox": [174.0, -36.5, 174.2, -36.3],
        "assets": {
            "visual": {
                "href": "./tile_b.tiff",
                "type": "image/tiff; application=geotiff",
            }
        },
    },
}


def _static_stac_definition():
    return {
        "code": "TESTSTATIC",
        "access_strategy": "static_stac",
        "catalog_url": _STATIC_ROOT,
        "collection_filter": "/dem_1m/",
        "dtm_asset_keys": "",
        "coverage_bbox": (166.0, -48.0, 179.0, -34.0),
    }


def test_static_stac_walks_and_memoises(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    fetch_log = []

    def _fake_fetch_json(self, session, url):
        fetch_log.append(url)
        return _STATIC_TREE.get(url)

    monkeypatch.setattr(
        INSETS.StaticStacCatalogStrategy, "_fetch_json", _fake_fetch_json
    )
    strategy = INSETS.ACCESS_STRATEGIES["static_stac"]()
    definition = _static_stac_definition()
    auckland = (174.78, -37.01, 174.80, -36.99)
    sources = strategy.discover(definition, auckland)
    assert sources == [
        {
            "bbox": [174.7, -37.1, 174.9, -36.9],
            "href": "https://static.test/north/dem_1m/tile_a.tiff",
            "resolution": None,
        }
    ]
    # The dsm_1m sibling was filtered out; the south survey's items were
    # never fetched (its collection box misses the request).
    assert (
        "https://static.test/north/dsm_1m/collection.json" not in fetch_log
    )
    assert "https://static.test/south/dem_1m/far.json" not in fetch_log
    assert os.path.isfile(strategy.index_path(definition))
    # Second discovery answers ENTIRELY from the persisted index.
    def _forbidden(self, session, url):
        raise AssertionError("catalog re-walked despite the index")

    monkeypatch.setattr(
        INSETS.StaticStacCatalogStrategy, "_fetch_json", _forbidden
    )
    strategy_two = INSETS.ACCESS_STRATEGIES["static_stac"]()
    assert strategy_two.discover(definition, auckland) == sources


# =====================================================================
# The xyz_text_tiles strategy (Japan GSI slippy text tiles)
# =====================================================================
class _FakeTileSession:
    """Serves synthetic 256x256 elevation text tiles per URL prefix."""

    def __init__(self, responses):
        # responses: {url_substring: (status, body)} — first match wins.
        self._responses = responses

    def get(self, url, timeout=None):
        import types

        for (token, (status, body)) in self._responses.items():
            if token in url:
                return types.SimpleNamespace(status_code=status, text=body)
        return types.SimpleNamespace(status_code=404, text="")


def _text_tile(value):
    row = ",".join([str(value)] * 256)
    return "\n".join([row] * 256)


def _xyz_definition(**overrides):
    definition = {
        "code": "TESTXYZ",
        "access_strategy": "xyz_text_tiles",
        "tile_url_template": "https://tiles.test/primary/{zoom}/{x}/{y}.txt",
        "tile_zoom": "15",
        "fallback_url_template": (
            "https://tiles.test/fallback/{zoom}/{x}/{y}.txt"
        ),
        "fallback_zoom": "14",
        "native_resolution_m": "5",
        "vertical_datum": "TESTDATUM",
    }
    definition.update(overrides)
    return definition


@requires_gdal
def test_xyz_tiles_primary_layer_serves(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(INSETS, "has_gdal", True)
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _FakeTileSession({"/primary/": (200, _text_tile(42.5))}),
    )
    destination = str(tmp_path / "RJXX_testxyz.tif")
    provenance = INSETS.fetch_inset(
        _xyz_definition(), (139.77, 35.545, 139.79, 35.56), 5.0, destination
    )
    assert provenance is not None
    dataset = gdal.Open(destination)
    values = dataset.GetRasterBand(1).ReadAsArray()
    valid = values[values > -32768]
    assert valid.size and abs(float(valid.mean()) - 42.5) < 0.01


@requires_gdal
def test_xyz_tiles_fall_back_to_composite(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(INSETS, "has_gdal", True)
    # The 5 m layer has no tiles here; the nationwide composite does.
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: _FakeTileSession(
            {
                "/primary/": (404, ""),
                "/fallback/": (200, _text_tile(7.25)),
            }
        ),
    )
    destination = str(tmp_path / "RJYY_testxyz.tif")
    provenance = INSETS.fetch_inset(
        _xyz_definition(), (139.77, 35.545, 139.79, 35.56), 5.0, destination
    )
    assert provenance is not None
    dataset = gdal.Open(destination)
    values = dataset.GetRasterBand(1).ReadAsArray()
    valid = values[values > -32768]
    assert valid.size and abs(float(valid.mean()) - 7.25) < 0.01
    # Nothing anywhere: honest no-coverage.
    monkeypatch.setattr(
        requests, "Session", lambda: _FakeTileSession({})
    )
    assert (
        INSETS.fetch_inset(
            _xyz_definition(),
            (139.77, 35.545, 139.79, 35.56),
            5.0,
            str(tmp_path / "RJZZ_testxyz.tif"),
        )
        is None
    )


# =====================================================================
# The xyz_archive_drop strategy (Taiwan manual archives)
# =====================================================================
@requires_gdal
def test_xyz_archive_drop_converts_and_serves(tmp_path, monkeypatch):
    import zipfile

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    definition = {
        "code": "TESTTWN",
        "access_strategy": "xyz_archive_drop",
        "drop_directory_name": "Taiwan_test_drop",
        "source_epsg": "3826",
        "xyz_column_order": "YXZ",
        "native_resolution_m": "20",
        "vertical_datum": "TWVD2001",
    }
    # A sheet of N,E,H points (northing first) on a 20 m TWD97 grid
    # covering the requested WGS84 window.
    bbox = (121.226, 25.076, 121.238, 25.084)
    strategy = INSETS.ACCESS_STRATEGIES["xyz_archive_drop"]()
    (x_min, y_min, x_max, y_max) = strategy._bounding_box_in_source_crs(
        definition, bbox
    )
    x0 = (int(x_min) // 20 - 4) * 20
    y0 = (int(y_min) // 20 - 4) * 20
    columns = int((x_max - x0) / 20) + 8
    rows = int((y_max - y0) / 20) + 8
    lines = []
    for row in range(rows):
        for column in range(columns):
            lines.append(
                "%d,%d,%s" % (y0 + row * 20, x0 + column * 20, "77.0")
            )
    drop_directory = strategy.drop_directory(definition)
    os.makedirs(drop_directory)
    with zipfile.ZipFile(
        os.path.join(drop_directory, "county.zip"), "w"
    ) as archive:
        archive.writestr("sheets/94191001dem.grd", "\n".join(lines))
        archive.writestr("sheets/94191001dem.hdr", "metadata, skipped")
    destination = str(tmp_path / "RCXX_testtwn.tif")
    provenance = INSETS.fetch_inset(definition, bbox, 20.0, destination)
    assert provenance is not None
    dataset = gdal.Open(destination)
    values = dataset.GetRasterBand(1).ReadAsArray()
    valid = values[values > -32768]
    assert valid.size and abs(float(valid.mean()) - 77.0) < 0.01
    # The conversion is memoised: the sheet GeoTIFF and index exist.
    assert os.path.isfile(strategy.index_path(definition))
    # A second airport far away on the sheet's county still resolves
    # without re-conversion, and an uncovered window is no-coverage.
    assert (
        INSETS.fetch_inset(
            definition,
            (121.5, 25.2, 121.51, 25.21),
            20.0,
            str(tmp_path / "RCYY_testtwn.tif"),
        )
        is None
    )


def test_xyz_archive_drop_instructions_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    definition = {
        "code": "TESTTWN",
        "access_strategy": "xyz_archive_drop",
        "drop_directory_name": "Taiwan_test_drop",
        "source_epsg": "3826",
    }
    strategy = INSETS.ACCESS_STRATEGIES["xyz_archive_drop"]()
    assert strategy.discover(definition, (121.2, 25.0, 121.3, 25.1)) is None


# =====================================================================
# The wfs_tile_index strategy (France LiDAR HD)
# =====================================================================
@requires_gdal
def test_wfs_tile_index_discovers_and_fetches(tmp_path, monkeypatch):
    import types
    import requests

    monkeypatch.setattr(INSETS, "has_gdal", True)
    # A real (tiny) GeoTIFF is served as the tile payload.
    tile_path = str(tmp_path / "payload.tif")
    _write_constant_geotiff(tile_path, 1.35, 43.62, 1.38, 43.64, 150.0)
    with open(tile_path, "rb") as handle:
        tile_bytes = handle.read()
    calls = []

    def _fake_get(url, timeout=None):
        calls.append(url)
        if "GetFeature" in url:
            assert "43.62" in url  # latitude-first bbox
            return types.SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "features": [
                        {
                            "properties": {
                                "name": "LHD_TEST_TILE",
                                "url": "https://tiles.test/one.tif",
                            }
                        }
                    ]
                },
            )
        return types.SimpleNamespace(status_code=200, content=tile_bytes)

    monkeypatch.setattr(requests, "get", _fake_get)
    definition = {
        "code": "TESTWFS",
        "access_strategy": "wfs_tile_index",
        "wfs_service_url": "https://wfs.test/ows",
        "wfs_type_name": "TEST:dalle",
        "native_resolution_m": "0.5",
        "vertical_datum": "NGF-IGN69",
    }
    destination = str(tmp_path / "LFXX_testwfs.tif")
    provenance = INSETS.fetch_inset(
        definition, (1.358, 43.625, 1.372, 43.635), 1.0, destination
    )
    assert provenance is not None
    assert provenance["source_urls"] == ["LHD_TEST_TILE"]
    dataset = gdal.Open(destination)
    values = dataset.GetRasterBand(1).ReadAsArray()
    valid = values[values > -32768]
    assert valid.size and abs(float(valid.mean()) - 150.0) < 0.01


def test_wfs_tile_index_empty_featureset_is_no_coverage(monkeypatch):
    import types
    import requests

    monkeypatch.setattr(
        requests,
        "get",
        lambda url, timeout=None: types.SimpleNamespace(
            status_code=200, json=lambda: {"features": []}
        ),
    )
    strategy = INSETS.ACCESS_STRATEGIES["wfs_tile_index"]()
    definition = {
        "code": "TESTWFS",
        "access_strategy": "wfs_tile_index",
        "wfs_service_url": "https://wfs.test/ows",
        "wfs_type_name": "TEST:dalle",
    }
    assert strategy.discover(definition, (1.0, 43.0, 1.1, 43.1)) is None


# =====================================================================
# The wcs_kvp strategy (Hesse-style spelled-out GetCoverage)
# =====================================================================
@requires_gdal
def test_wcs_kvp_instantiates_bbox_and_fetches(tmp_path, monkeypatch):
    import requests

    monkeypatch.setattr(INSETS, "has_gdal", True)
    payload_path = str(tmp_path / "payload.tif")
    _write_constant_geotiff(payload_path, 8.56, 50.02, 8.59, 50.04, 105.0)
    with open(payload_path, "rb") as handle:
        payload = handle.read()
    seen = {}

    def _fake_get(url, timeout=None, headers=None):
        import types

        seen["url"] = url
        return types.SimpleNamespace(status_code=200, content=payload)

    monkeypatch.setattr(requests, "get", _fake_get)
    definition = {
        "code": "TESTKVP",
        "access_strategy": "wcs_kvp",
        "wcs_getcoverage_template": (
            "https://wcs.test/ows?REQUEST=GetCoverage"
            "&SUBSET=E({xmin},{xmax})&SUBSET=N({ymin},{ymax})"
        ),
        "source_epsg": "25832",
        "vertical_datum": "DHHN2016",
    }
    destination = str(tmp_path / "EDDF_testkvp.tif")
    provenance = INSETS.fetch_inset(
        definition, (8.565, 50.028, 8.580, 50.037), 1.0, destination
    )
    assert provenance is not None
    # The placeholders were filled with padded EPSG:25832 metres.
    assert "SUBSET=E(4" in seen["url"] and "SUBSET=N(55" in seen["url"]
    dataset = gdal.Open(destination)
    values = dataset.GetRasterBand(1).ReadAsArray()
    valid = values[values > -32768]
    assert valid.size and abs(float(valid.mean()) - 105.0) < 0.01


# =====================================================================
# tile_grid_http refinements (offsets, HTML index)
# =====================================================================
@requires_gdal
def test_tile_grid_odd_easting_offset(monkeypatch):
    # Baden-Wuerttemberg's 2 km tiles anchor at ODD easting km.
    strategy = INSETS.ACCESS_STRATEGIES["tile_grid_http"]()
    definition = {
        "code": "TESTBW",
        "access_strategy": "tile_grid_http",
        "tile_url_template": "https://bw.test/dgm1_32_{easting_km}_{northing_km}_2_bw.zip",
        "tile_size_km": "2",
        "grid_easting_offset_km": "1",
        "source_epsg": "25832",
        "probe_mode": "none",
    }
    sources = strategy.discover(definition, (9.215, 48.687, 9.228, 48.695))
    names = [entry["url"].rsplit("/", 1)[-1] for entry in sources]
    # Stuttgart (516200, 5392900): odd-anchored easting 515, even 5392.
    assert "dgm1_32_515_5392_2_bw.zip" in names
    for name in names:
        easting = int(name.split("_")[2])
        northing = int(name.split("_")[3])
        assert easting % 2 == 1 and northing % 2 == 0


def test_tile_grid_html_index_resolution(tmp_path, monkeypatch):
    import types
    import requests

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    listing = (
        '<html><a href="dgm1_32_375_5534_1_rp_2023.tif">x</a>'
        '<a href="dgm1_32_375_5535_1_rp_2021.tif">y</a></html>'
    )

    def _fake_get(url, timeout=None, headers=None):
        response = types.SimpleNamespace(status_code=200, text=listing)
        response.json = lambda: (_ for _ in ()).throw(ValueError())
        return response

    monkeypatch.setattr(requests, "get", _fake_get)
    strategy = INSETS.ACCESS_STRATEGIES["tile_grid_http"]()
    definition = {
        "code": "TESTRLP",
        "access_strategy": "tile_grid_http",
        "tile_url_template": "https://rlp.test/tif/{file_name}",
        "index_url": "https://rlp.test/tif/",
        "tile_size_km": "1",
        "source_epsg": "25832",
    }
    names = strategy._tile_names_from_index(definition)
    assert "dgm1_32_375_5534_1_rp_2023.tif" in names
    # Cached: a second call must not need the network.
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-fetched")),
    )
    assert strategy._tile_names_from_index(definition) == names


# =====================================================================
# xyz_archive_drop: loose (non-zip) dropped files (Hamburg)
# =====================================================================
@requires_gdal
def test_xyz_archive_drop_converts_loose_file(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    definition = {
        "code": "TESTHH",
        "access_strategy": "xyz_archive_drop",
        "drop_directory_name": "Hamburg_test_drop",
        "source_epsg": "25832",
        "xyz_column_order": "AUTO",
        "native_resolution_m": "1",
    }
    strategy = INSETS.ACCESS_STRATEGIES["xyz_archive_drop"]()
    bbox = (9.985, 53.628, 9.995, 53.635)
    (x_min, y_min, x_max, y_max) = strategy._bounding_box_in_source_crs(
        definition, bbox
    )
    x0 = (int(x_min) // 10 - 4) * 10
    y0 = (int(y_min) // 10 - 4) * 10
    columns = int((x_max - x0) / 10) + 8
    rows = int((y_max - y0) / 10) + 8
    lines = [
        "%d %d 11.5" % (x0 + column * 10, y0 + row * 10)
        for row in range(rows)
        for column in range(columns)
    ]
    drop_directory = strategy.drop_directory(definition)
    os.makedirs(drop_directory)
    with open(os.path.join(drop_directory, "dgm1_city.xyz"), "w") as f:
        f.write("\n".join(lines))
    destination = str(tmp_path / "EDDH_testhh.tif")
    provenance = INSETS.fetch_inset(definition, bbox, 10.0, destination)
    assert provenance is not None
    dataset = gdal.Open(destination)
    values = dataset.GetRasterBand(1).ReadAsArray()
    valid = values[values > -32768]
    assert valid.size and abs(float(valid.mean()) - 11.5) < 0.01


# =====================================================================
# Warp sentinel sanitization (undeclared float-max nodata, PDOK case)
# =====================================================================
@requires_gdal
def test_warp_sanitizes_undeclared_sentinel_values(tmp_path):
    import numpy as numpy_module

    source_path = str(tmp_path / "sentinel_source.tif")
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(source_path, 40, 40, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform((4.75, 0.001, 0, 52.32, 0, -0.001))
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(4326)
    dataset.SetProjection(reference.ExportToWkt())
    values = numpy_module.full((40, 40), -3.5, dtype=numpy_module.float32)
    values[:10, :] = 3.4028235e38  # float-max fill, NO nodata declared
    dataset.GetRasterBand(1).WriteArray(values)
    dataset = None
    destination = str(tmp_path / "sanitized.tif")
    assert INSETS.warp_vsicurl_sources_to_geotiff(
        [source_path], (4.75, 52.28, 4.79, 52.32), 100.0, destination
    )
    # Hold the dataset reference: chaining Open().GetRasterBand() lets
    # the dataset be garbage-collected mid-expression and orphans the
    # band (the classic GDAL Python pitfall).
    dataset = gdal.Open(destination)
    result = dataset.GetRasterBand(1).ReadAsArray()
    valid = result[result > -32768]
    assert valid.size and float(valid.max()) < 12000.0
    assert float(result.min()) == -32768.0  # the garbage became nodata


# =====================================================================
# Warp vertical-datum passthrough (compound-CRS source, Sweden case)
# =====================================================================
@requires_gdal
def test_warp_keeps_compound_crs_heights_unshifted(tmp_path):
    """A source declaring a compound CRS must NOT get a geoid shift.

    Lantmateriet's Cloud-Optimized GeoTIFFs declare EPSG:5845 (SWEREF99
    TM + RH2000 height); without -novshift GDAL converted their
    orthometric heights to ellipsoidal during the warp, lifting every
    Swedish airport by the 23-36 m geoid separation.  The provenance
    datum_note promises heights pass through in the source vertical
    datum, so a constant-10 raster must still read 10 after the warp.
    """
    import numpy as numpy_module

    source_path = str(tmp_path / "compound_crs_source.tif")
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(source_path, 100, 100, 1, gdal.GDT_Float32)
    # The real m658_66 national-grid tile window (SWEREF99 TM metres,
    # Stockholm area) at 100 m pixels.
    dataset.SetGeoTransform((660000.0, 100.0, 0, 6590000.0, 0, -100.0))
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(5845)  # SWEREF99 TM + RH2000 height
    dataset.SetProjection(reference.ExportToWkt())
    dataset.GetRasterBand(1).WriteArray(
        numpy_module.full((100, 100), 10.0, dtype=numpy_module.float32)
    )
    dataset = None
    destination = str(tmp_path / "unshifted.tif")
    assert INSETS.warp_vsicurl_sources_to_geotiff(
        [source_path], (17.93, 59.35, 17.95, 59.36), 100.0, destination
    )
    dataset = gdal.Open(destination)
    result = dataset.GetRasterBand(1).ReadAsArray()
    valid = result[result > -32768]
    # ~33.2 here would mean the RH2000 -> ellipsoid shift was applied.
    assert valid.size and abs(float(valid.mean()) - 10.0) < 0.01


# =====================================================================
# The geojson_tile_index strategy (Uruguay's national catalog)
# =====================================================================
@requires_gdal
def test_geojson_tile_index_caches_and_fetches(tmp_path, monkeypatch):
    import types
    import requests

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    catalog = {
        "features": [
            {
                "properties": {"MDT_geoT": "https://tiles.test/J29C3.tif"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-56.06, -34.86], [-56.00, -34.86],
                        [-56.00, -34.82], [-56.06, -34.82],
                        [-56.06, -34.86],
                    ]],
                },
            },
            {
                "properties": {"MDT_geoT": "https://tiles.test/FAR.tif"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [-54.0, -33.0], [-53.9, -33.0],
                        [-53.9, -32.9], [-54.0, -32.9], [-54.0, -33.0],
                    ]],
                },
            },
        ]
    }
    monkeypatch.setattr(
        requests,
        "get",
        lambda url, timeout=None: types.SimpleNamespace(
            status_code=200, json=lambda: catalog
        ),
    )
    definition = {
        "code": "TESTUY",
        "access_strategy": "geojson_tile_index",
        "index_url": "https://catalog.test/grid.geojson",
        "url_property": "MDT_geoT",
    }
    strategy = INSETS.ACCESS_STRATEGIES["geojson_tile_index"]()
    montevideo = (-56.038, -34.845, -56.022, -34.833)
    sources = strategy.discover(definition, montevideo)
    assert [entry["url"] for entry in sources] == [
        "https://tiles.test/J29C3.tif"
    ]
    # Memoised: a second discovery never touches the network.
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-fetch")),
    )
    assert strategy.discover(definition, montevideo) == sources
    assert (
        strategy.discover(definition, (-58.0, -34.0, -57.9, -33.9)) is None
    )


# =====================================================================
# The arcgis_lerc_tiles strategy (Rio's tiles-only image service)
# =====================================================================
# imagecodecs' LERC codec and the osgeo libraries abort a shared
# process (the reason the strategy decodes in a subprocess) -- so the
# TEST must generate its LERC fixture in a subprocess as well.
def _lerc_blob_via_subprocess(tmp_path):
    import subprocess
    import sys

    blob_path = str(tmp_path / "fixture.lerc")
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "import numpy\n"
                "import imagecodecs\n"
                "values = numpy.full((257, 257), 12.5,"
                " dtype=numpy.float32)\n"
                "open(sys.argv[1], 'wb').write("
                "imagecodecs.lerc_encode(values))\n"
            ),
            blob_path,
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        return None
    with open(blob_path, "rb") as handle:
        return handle.read()


@requires_gdal
def test_arcgis_lerc_tiles_decodes_and_serves(tmp_path, monkeypatch):
    import types
    import requests

    blob = _lerc_blob_via_subprocess(tmp_path)
    if blob is None:
        pytest.skip("imagecodecs with LERC not available")
    monkeypatch.setattr(INSETS, "has_gdal", True)
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: types.SimpleNamespace(
            get=lambda url, timeout=None: types.SimpleNamespace(
                status_code=200, content=blob
            )
        ),
    )
    definition = {
        "code": "TESTRIO",
        "access_strategy": "arcgis_lerc_tiles",
        "tile_url_template": (
            "https://tiles.test/ImageServer/tile/{level}/{row}/{col}"
        ),
        "tile_level": "15",
        "native_resolution_m": "5",
        "vertical_datum": "Imbituba",
    }
    destination = str(tmp_path / "SBGL_testrio.tif")
    provenance = INSETS.fetch_inset(
        definition, (-43.255, -22.820, -43.245, -22.812), 5.0, destination
    )
    assert provenance is not None
    dataset = gdal.Open(destination)
    values = dataset.GetRasterBand(1).ReadAsArray()
    valid = values[values > -32768]
    assert valid.size and abs(float(valid.mean()) - 12.5) < 0.01


def test_arcgis_lerc_tiles_missing_tiles_are_no_coverage(
    tmp_path, monkeypatch
):
    import types
    import requests

    monkeypatch.setattr(INSETS, "has_gdal", True)
    monkeypatch.setattr(
        requests,
        "Session",
        lambda: types.SimpleNamespace(
            get=lambda url, timeout=None: types.SimpleNamespace(
                status_code=404, content=b""
            )
        ),
    )
    definition = {
        "code": "TESTRIO",
        "access_strategy": "arcgis_lerc_tiles",
        "tile_url_template": (
            "https://tiles.test/ImageServer/tile/{level}/{row}/{col}"
        ),
        "tile_level": "15",
    }
    assert (
        INSETS.fetch_inset(
            definition,
            (-43.255, -22.820, -43.245, -22.812),
            5.0,
            str(tmp_path / "SBXX_testrio.tif"),
        )
        is None
    )


@requires_gdal
def test_arcgis_lerc_tiles_projected_pyramid_grid(tmp_path, monkeypatch):
    # Estonia-style cache: a projected CRS with its own origin and
    # per-level resolution instead of the global Web Mercator grid.
    import types
    import requests

    blob = _lerc_blob_via_subprocess(tmp_path)
    if blob is None:
        pytest.skip("imagecodecs with LERC not available")
    monkeypatch.setattr(INSETS, "has_gdal", True)
    requested = []

    def _get(url, timeout=None):
        requested.append(url)
        return types.SimpleNamespace(status_code=200, content=blob)

    monkeypatch.setattr(
        requests, "Session", lambda: types.SimpleNamespace(get=_get)
    )
    definition = {
        "code": "TESTEE",
        "access_strategy": "arcgis_lerc_tiles",
        "tile_url_template": (
            "https://tiles.test/ImageServer/tile/{level}/{row}/{col}"
        ),
        "tile_level": "12",
        "tile_epsg": "3301",
        "tile_origin_x": "40500.0",
        "tile_origin_y": "7017000.0",
        "tile_resolution": "0.9765625",
        "native_resolution_m": "1",
    }
    destination = str(tmp_path / "EETN_testee.tif")
    provenance = INSETS.fetch_inset(
        definition, (24.955, 59.408, 24.960, 59.412), 1.0, destination
    )
    assert provenance is not None
    # Tallinn in EPSG:3301 is roughly (542000, 6588000): with a 250 m
    # tile span from origin (40500, 7017000) the columns sit near 2000
    # and the rows near 1700 -- prove the projected grid math was used
    # (the global-mercator indices would be vastly different).
    first = requested[0]
    column = int(first.rsplit("/", 1)[-1])
    row = int(first.rsplit("/", 2)[-2])
    assert 1900 < column < 2200
    assert 1600 < row < 1900
    dataset = gdal.Open(destination)
    values = dataset.GetRasterBand(1).ReadAsArray()
    valid = values[values > -32768]
    assert valid.size and abs(float(valid.mean()) - 12.5) < 0.01


# =====================================================================
# Ordnance Survey grid-square arithmetic (Scotland's lidar bucket)
# =====================================================================
def test_ordnance_survey_square_extents():
    # 100 km anchors: NS (Glasgow) and HY (Orkney).
    assert INSETS._ordnance_survey_square_extent("NS16") == (
        210000, 660000, 220000, 670000
    )
    assert INSETS._ordnance_survey_square_extent("HY20") == (
        320000, 1000000, 330000, 1010000
    )
    # Quadrants halve to 5 km.
    assert INSETS._ordnance_survey_square_extent("NS16NE") == (
        215000, 665000, 220000, 670000
    )
    assert INSETS._ordnance_survey_square_extent("NS16SW") == (
        210000, 660000, 215000, 665000
    )
    # Four digits address a 1 km square.
    assert INSETS._ordnance_survey_square_extent("NR5712") == (
        157000, 612000, 158000, 613000
    )
    # Garbage returns None.
    assert INSETS._ordnance_survey_square_extent("1234") is None
    assert INSETS._ordnance_survey_square_extent("NSXY") is None


# =====================================================================
# The arcgis_feature_tiles strategy (Ireland's DATA_URL catalogs)
# =====================================================================
@requires_gdal
def test_arcgis_feature_tiles_catalog_and_fills(tmp_path, monkeypatch):
    import io
    import types
    import zipfile
    import requests

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    # Archive payload: one DTM GeoTIFF whose fill is -99 but whose
    # band DECLARES nodata 0.0 (the broken Irish campaign shape).
    import numpy as numpy_module

    tif_path = str(tmp_path / "TII_TEST_DTM.tif")
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(tif_path, 40, 40, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform((-6.262, 0.0005, 0, 53.429, 0, -0.0004))
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(4326)
    dataset.SetProjection(reference.ExportToWkt())
    values = numpy_module.full((40, 40), 62.0, dtype=numpy_module.float32)
    values[:8, :] = -99.0
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(0.0)
    band.WriteArray(values)
    dataset = None
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.write(tif_path, "TII_TEST/TII_TEST_DTM.tif")
    archive_bytes = buffer.getvalue()

    def _fake_get(url, timeout=None):
        if url.endswith("/Lidar?f=json"):
            payload = {
                "services": [
                    {"name": "Lidar/IE_Coverage_TEST", "type": "MapServer"}
                ]
            }
        elif url.endswith("/MapServer?f=json"):
            payload = {"layers": [{"id": 4}]}
        elif url.endswith("/MapServer/4?f=json"):
            payload = {"fields": [{"name": "DATA_URL"}]}
        elif "/query?" in url:
            payload = {
                "features": [
                    {"attributes": {"DATA_URL": "https://dl.test/a.zip"}}
                ]
            }
        elif url.endswith(".zip"):
            return types.SimpleNamespace(
                status_code=200, content=archive_bytes
            )
        else:
            return types.SimpleNamespace(status_code=404, content=b"")
        return types.SimpleNamespace(
            status_code=200, json=lambda: payload, content=b""
        )

    monkeypatch.setattr(
        requests,
        "Session",
        lambda: types.SimpleNamespace(get=_fake_get),
    )
    monkeypatch.setattr(requests, "get", _fake_get)
    definition = {
        "code": "TESTIE",
        "access_strategy": "arcgis_feature_tiles",
        "catalog_folder_url": "https://gsi.test/server/rest/services/Lidar",
        "url_field": "DATA_URL",
        "member_filter": "dtm",
        "source_nodata": "-99",
    }
    destination = str(tmp_path / "EIDW_testie.tif")
    provenance = INSETS.fetch_inset(
        definition, (-6.260, 53.418, -6.245, 53.428), 100.0, destination
    )
    assert provenance is not None
    result_dataset = gdal.Open(destination)
    values_out = result_dataset.GetRasterBand(1).ReadAsArray()
    valid = values_out[values_out > -32768]
    # The -99 fill was excluded BEFORE interpolation despite the wrong
    # declared nodata; real values survive untouched.
    assert valid.size and float(valid.min()) > 0
    assert abs(float(valid.max()) - 62.0) < 0.5
    # The layer catalog was cached.
    strategy = INSETS.ACCESS_STRATEGIES["arcgis_feature_tiles"]()
    assert os.path.isfile(strategy.index_path(definition))


# =====================================================================
# xyz_archive_drop: in-place indexing of GeoTIFF members (Wallonia)
# =====================================================================
@requires_gdal
def test_xyz_archive_drop_indexes_geotiffs_in_place(tmp_path, monkeypatch):
    import zipfile
    import numpy as numpy_module

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    definition = {
        "code": "TESTWAL",
        "access_strategy": "xyz_archive_drop",
        "drop_directory_name": "Wallonia_test_drop",
        "source_epsg": "3812",
        "native_resolution_m": "1",
    }
    strategy = INSETS.ACCESS_STRATEGIES["xyz_archive_drop"]()
    # A georeferenced GeoTIFF member (carries its own CRS): must be
    # indexed THROUGH the zip, without an extracted converted copy.
    bbox = (4.44, 50.455, 4.46, 50.468)
    (x_min, y_min, x_max, y_max) = strategy._bounding_box_in_source_crs(
        definition, bbox
    )
    tif_path = str(tmp_path / "province_sheet.tif")
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(tif_path, 50, 50, 1, gdal.GDT_Float32)
    span_x = (x_max - x_min) * 1.5
    span_y = (y_max - y_min) * 1.5
    dataset.SetGeoTransform(
        (x_min - span_x * 0.2, span_x / 50, 0,
         y_max + span_y * 0.2, 0, -span_y / 50)
    )
    reference = osr.SpatialReference()
    reference.ImportFromEPSG(3812)
    dataset.SetProjection(reference.ExportToWkt())
    dataset.GetRasterBand(1).WriteArray(
        numpy_module.full((50, 50), 185.0, dtype=numpy_module.float32)
    )
    dataset = None
    drop_directory = strategy.drop_directory(definition)
    os.makedirs(drop_directory)
    with zipfile.ZipFile(
        os.path.join(drop_directory, "PROVINCE.zip"), "w"
    ) as archive:
        archive.write(tif_path, "MNT/province_sheet.tif")
    destination = str(tmp_path / "EBCI_testwal.tif")
    provenance = INSETS.fetch_inset(definition, bbox, 5.0, destination)
    assert provenance is not None
    assert provenance["source_urls"][0].startswith("/vsizip/")
    result_dataset = gdal.Open(destination)
    result = result_dataset.GetRasterBand(1).ReadAsArray()
    valid = result[result > -32768]
    assert valid.size and abs(float(valid.mean()) - 185.0) < 0.01
    # No extracted per-sheet copy was created.
    converted = os.path.join(drop_directory, "converted")
    copies = [
        name for name in os.listdir(converted) if name.endswith(".tif")
    ]
    assert copies == []


# =====================================================================
# The degree_named_cog access strategy (deterministic per-degree COGs,
# e.g. the Copernicus GLO-30 global surface model on AWS Open Data)
# =====================================================================
class _FakeHeadResponse:
    """Minimal stand-in for a ``requests`` HEAD response."""

    def __init__(self, status_code):
        self.status_code = status_code


def _fake_head_from_status_map(status_by_url, recorded_urls):
    """Build a ``requests.head`` replacement over a URL->status map.

    Each call appends the probed URL to ``recorded_urls`` (so the test can
    count real probes vs. memo hits) and returns the mapped status,
    defaulting unmapped URLs to 404.
    """

    def _fake_head(url, timeout=None):
        recorded_urls.append(url)
        return _FakeHeadResponse(status_by_url.get(url, 404))

    return _fake_head


_DEGREE_URL_TEMPLATE = (
    "https://example.test/cog/{latitude_token}_{longitude_token}.tif"
)


def _degree_cell_url(cell_latitude, cell_longitude):
    latitude_token, longitude_token = (
        INSETS.DegreeNamedCogStrategy.degree_cell_tokens(
            cell_latitude, cell_longitude
        )
    )
    return _DEGREE_URL_TEMPLATE.format(
        latitude_token=latitude_token, longitude_token=longitude_token
    )


def _degree_definition(**overrides):
    definition = {
        "code": "COPERTEST",
        "access_strategy": "degree_named_cog",
        "role": INSETS.ROLE_AIRPORT_INSET,
        "enabled": True,
        "priority": 1.0,
        "url_template": _DEGREE_URL_TEMPLATE,
        "coverage_bbox": (-180.0, -90.0, 180.0, 90.0),
        "native_resolution_m": 30.0,
    }
    definition.update(overrides)
    return definition


def test_degree_cell_tokens_encode_hemispheres_and_zero_padding():
    tokens = INSETS.DegreeNamedCogStrategy.degree_cell_tokens
    # Northern / eastern positives, zero-padded to 2 and 3 digits.
    assert tokens(25, 51) == ("N25", "E051")
    assert tokens(5, 7) == ("N05", "E007")
    # Southern / western negatives take S / W on the magnitude.
    assert tokens(-14, -29) == ("S14", "W029")
    assert tokens(-5, -7) == ("S05", "W007")
    # The origin cell is N/E (zero is non-negative).
    assert tokens(0, 0) == ("N00", "E000")


def test_degree_cells_of_bounding_box_enumeration():
    cells = INSETS.DegreeNamedCogStrategy.degree_cells_of_bounding_box
    # A box wholly inside one degree cell -> exactly that cell.
    assert cells((51.1, 25.1, 51.9, 25.9)) == [(25, 51)]
    # A box straddling both integer boundaries -> the four cells it spans.
    assert sorted(cells((50.5, 24.5, 51.5, 25.5))) == [
        (24, 50),
        (24, 51),
        (25, 50),
        (25, 51),
    ]
    # A box whose edges land exactly on integer degrees does NOT pull in
    # the cell beyond the top/right edge -> a single containing cell.
    assert cells((51.0, 25.0, 52.0, 26.0)) == [(25, 51)]
    # A degenerate (zero-area) box still yields at least its cell.
    assert cells((51.5, 25.5, 51.5, 25.5)) == [(25, 51)]


def test_degree_discover_filters_by_head_probe_and_memoises(monkeypatch):
    import requests

    # A fresh per-test memo dict (monkeypatch restores the class attribute).
    monkeypatch.setattr(
        INSETS.DegreeNamedCogStrategy, "_cell_exists_by_url", {}
    )
    # Four cells span the box; two objects exist, two are absent (ocean).
    present = {
        _degree_cell_url(25, 51): 200,
        _degree_cell_url(24, 50): 200,
    }
    probed = []
    monkeypatch.setattr(
        requests, "head", _fake_head_from_status_map(present, probed)
    )

    strategy = INSETS.DegreeNamedCogStrategy()
    definition = _degree_definition()
    box = (50.5, 24.5, 51.5, 25.5)
    sources = strategy.discover(definition, box)

    # Only the two existing cells survive, each prefixed for /vsicurl/ warp.
    survivor_urls = {entry["source"] for entry in sources}
    assert survivor_urls == {
        "/vsicurl/" + _degree_cell_url(25, 51),
        "/vsicurl/" + _degree_cell_url(24, 50),
    }
    # Every source carries its integer SW-corner cell.
    cells = {tuple(entry["cell"]) for entry in sources}
    assert cells == {(25, 51), (24, 50)}

    # All four definitive 200/404 answers are memoised on the class dict.
    memo = INSETS.DegreeNamedCogStrategy._cell_exists_by_url
    assert memo[_degree_cell_url(25, 51)] is True
    assert memo[_degree_cell_url(24, 50)] is True
    assert memo[_degree_cell_url(24, 51)] is False
    assert memo[_degree_cell_url(25, 50)] is False

    # A second discover over the same box is served entirely from the memo:
    # no further HEAD probes are issued.
    probes_after_first = len(probed)
    strategy.discover(definition, box)
    assert len(probed) == probes_after_first


def test_degree_discover_all_absent_returns_none(monkeypatch):
    import requests

    monkeypatch.setattr(
        INSETS.DegreeNamedCogStrategy, "_cell_exists_by_url", {}
    )
    probed = []
    # Empty status map -> every cell probes 404 (absent from the bucket).
    monkeypatch.setattr(
        requests, "head", _fake_head_from_status_map({}, probed)
    )
    strategy = INSETS.DegreeNamedCogStrategy()
    assert (
        strategy.discover(_degree_definition(), (51.1, 25.1, 51.9, 25.9))
        is None
    )


def test_degree_discover_transient_status_is_not_memoised(monkeypatch):
    import requests

    monkeypatch.setattr(
        INSETS.DegreeNamedCogStrategy, "_cell_exists_by_url", {}
    )
    probed = []
    # A 5xx is transient: treated as absent for this call but NOT memoised,
    # so a network blip cannot poison later airports of the run.
    monkeypatch.setattr(
        requests,
        "head",
        _fake_head_from_status_map({_degree_cell_url(25, 51): 500}, probed),
    )
    strategy = INSETS.DegreeNamedCogStrategy()
    definition = _degree_definition()
    box = (51.1, 25.1, 51.9, 25.9)

    assert strategy.discover(definition, box) is None
    memo = INSETS.DegreeNamedCogStrategy._cell_exists_by_url
    assert _degree_cell_url(25, 51) not in memo
    # A second run re-probes (the 500 left nothing cached).
    probes_after_first = len(probed)
    strategy.discover(definition, box)
    assert len(probed) > probes_after_first


def test_degree_discover_empty_url_template_returns_none(monkeypatch):
    import requests

    monkeypatch.setattr(
        INSETS.DegreeNamedCogStrategy, "_cell_exists_by_url", {}
    )
    probed = []
    monkeypatch.setattr(
        requests, "head", _fake_head_from_status_map({}, probed)
    )
    strategy = INSETS.DegreeNamedCogStrategy()
    assert (
        strategy.discover(
            _degree_definition(url_template=""), (51.1, 25.1, 51.9, 25.9)
        )
        is None
    )
    # No probe was even attempted without a URL template.
    assert probed == []


def test_degree_discover_coverage_miss_returns_none(monkeypatch):
    import requests

    monkeypatch.setattr(
        INSETS.DegreeNamedCogStrategy, "_cell_exists_by_url", {}
    )
    probed = []
    monkeypatch.setattr(
        requests, "head", _fake_head_from_status_map({}, probed)
    )
    strategy = INSETS.DegreeNamedCogStrategy()
    # A coverage_bbox that does not intersect the requested box short-circuits.
    definition = _degree_definition(coverage_bbox=(0.0, 0.0, 10.0, 10.0))
    assert (
        strategy.discover(definition, (51.1, 25.1, 51.9, 25.9)) is None
    )
    assert probed == []


@requires_gdal
def test_degree_fetch_provenance_strips_vsicurl_and_records_warp_inputs(
    tmp_path, monkeypatch
):
    import requests

    monkeypatch.setattr(INSETS, "has_gdal", True)
    monkeypatch.setattr(
        INSETS.DegreeNamedCogStrategy, "_cell_exists_by_url", {}
    )
    # Two of the four spanned cells exist; the other two are ocean (absent).
    present = {
        _degree_cell_url(25, 51): 200,
        _degree_cell_url(24, 50): 200,
    }
    probed = []
    monkeypatch.setattr(
        requests, "head", _fake_head_from_status_map(present, probed)
    )

    warp_calls = {}

    def _fake_warp(
        vsicurl_inputs,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
        **keyword_arguments,
    ):
        warp_calls["inputs"] = list(vsicurl_inputs)
        (west, south, east, north) = bounding_box_wgs84
        _write_constant_geotiff(
            destination_path, west, south, east, north, 10.0
        )
        return True

    monkeypatch.setattr(
        INSETS, "warp_vsicurl_sources_to_geotiff", _fake_warp
    )

    strategy = INSETS.DegreeNamedCogStrategy()
    definition = _degree_definition()
    box = (50.5, 24.5, 51.5, 25.5)
    destination = str(tmp_path / "copernicus.tif")
    provenance = strategy.fetch(definition, box, 30.0, destination)

    assert provenance is not None
    assert provenance["provider"] == "COPERTEST"
    # source_urls are the surviving cells with the /vsicurl/ prefix stripped.
    assert set(provenance["source_urls"]) == {
        _degree_cell_url(25, 51),
        _degree_cell_url(24, 50),
    }
    assert all(
        not url.startswith("/vsicurl/") for url in provenance["source_urls"]
    )
    assert provenance["bounding_box_wgs84"] == list(box)
    # The warp core received exactly the surviving /vsicurl/ inputs.
    assert set(warp_calls["inputs"]) == {
        "/vsicurl/" + _degree_cell_url(25, 51),
        "/vsicurl/" + _degree_cell_url(24, 50),
    }
    assert os.path.isfile(destination)


# =====================================================================
# Surface-model building-footprint masking (post-fetch pass)
# =====================================================================
_MASK_PIXEL_DEGREES = 0.00027  # ~30 m at the equator; exact value unimportant
_MASK_WEST = -87.0
_MASK_NORTH = 36.0
_MASK_COLUMNS = 60
_MASK_ROWS = 60
_MASK_EAST = _MASK_WEST + _MASK_COLUMNS * _MASK_PIXEL_DEGREES
_MASK_SOUTH = _MASK_NORTH - _MASK_ROWS * _MASK_PIXEL_DEGREES
_MASK_BOX = (_MASK_WEST, _MASK_SOUTH, _MASK_EAST, _MASK_NORTH)


def _write_surface_model_with_building_bump(path):
    """~60x60, ~30 m flat 10 m ground with a 45 m building bump and a
    nodata corner far from the bump.  Returns the footprint polygon (in
    absolute lon/lat) covering the bump for the masking test to inject."""
    from shapely import geometry as shapely_geometry

    values = numpy.full((_MASK_ROWS, _MASK_COLUMNS), 10.0, dtype=numpy.float32)
    # A rectangular rooftop bump in the interior (rows/cols 25..34).
    values[25:35, 25:35] = 45.0
    # A genuine-nodata corner far from the building (rows/cols 0..7).
    values[0:8, 0:8] = -32768.0
    _write_terrain_geotiff(
        path, _MASK_WEST, _MASK_SOUTH, _MASK_EAST, _MASK_NORTH, values
    )
    longitude_min = _MASK_WEST + 25 * _MASK_PIXEL_DEGREES
    longitude_max = _MASK_WEST + 35 * _MASK_PIXEL_DEGREES
    latitude_max = _MASK_NORTH - 25 * _MASK_PIXEL_DEGREES
    latitude_min = _MASK_NORTH - 35 * _MASK_PIXEL_DEGREES
    footprint = shapely_geometry.box(
        longitude_min, latitude_min, longitude_max, latitude_max
    )
    return footprint


@requires_gdal
def test_masking_replaces_building_bump_and_preserves_ground_and_nodata(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(INSETS, "has_gdal", True)
    path = str(tmp_path / "surface_model.tif")
    footprint = _write_surface_model_with_building_bump(path)
    monkeypatch.setattr(
        INSETS,
        "openstreetmap_building_footprints",
        lambda bounding_box_wgs84, footprint_prefetch=None: [footprint],
    )
    # Hermetic: never scan a real X-Plane install for package footprints.
    monkeypatch.setattr(
        INSETS, "package_object_footprints", lambda box, defn: []
    )

    definition = {"code": "COPERTEST", "footprint_mask_buffer_m": 35}
    summary = INSETS.mask_building_footprints_in_surface_model(
        path, _MASK_BOX, definition
    )

    # The pass reports its work, not a skip.
    assert "skipped" not in summary
    assert summary["footprint_source"] == "OpenStreetMap building footprints"
    assert summary["footprint_count"] == 1
    assert summary["masked_pixel_count"] > 0
    assert summary["footprint_mask_buffer_m"] == 35

    dataset = gdal.Open(path)
    result = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    # The rooftop bump was interpolated back down to the surrounding ground.
    bump = result[27:33, 27:33]
    assert numpy.all(numpy.abs(bump - 10.0) < 0.5)
    # A pixel far from the footprint is byte-for-byte the original ground.
    assert result[50, 50] == numpy.float32(10.0)
    # The genuine-nodata corner is restored verbatim (never a fill source).
    assert numpy.all(result[0:8, 0:8] == numpy.float32(-32768.0))


@requires_gdal
def test_masking_skips_when_no_footprints_and_leaves_raster_unchanged(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(INSETS, "has_gdal", True)
    path = str(tmp_path / "surface_model.tif")
    _write_surface_model_with_building_bump(path)
    monkeypatch.setattr(
        INSETS,
        "openstreetmap_building_footprints",
        lambda bounding_box_wgs84, footprint_prefetch=None: [],
    )
    # Hermetic: never scan a real X-Plane install for package footprints.
    monkeypatch.setattr(
        INSETS, "package_object_footprints", lambda box, defn: []
    )

    dataset = gdal.Open(path)
    before = dataset.GetRasterBand(1).ReadAsArray().copy()
    dataset = None

    # Residual structure masking OFF: with no footprints either, the
    # pass must skip and leave the raster untouched (the pre-2026-07-18
    # contract; residual masking default-ON handles the no-footprint
    # case separately — see tests/test_dsm_residual_mask.py).
    summary = INSETS.mask_building_footprints_in_surface_model(
        path, _MASK_BOX,
        {"code": "COPERTEST", INSETS.RESIDUAL_STRUCTURE_MASKING: False}
    )
    # No footprints -> an explicit skip carrying a zero count...
    assert "skipped" in summary
    assert summary["footprint_count"] == 0

    dataset = gdal.Open(path)
    after = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    # ...and the raster is left exactly as it was fetched.
    assert numpy.array_equal(after, before)


@requires_gdal
def test_fetch_inset_runs_masking_only_when_flag_true(tmp_path, monkeypatch):
    """The dispatcher runs the masking pass and stores its summary iff the
    definition opts in, and never calls it otherwise."""
    monkeypatch.setattr(INSETS, "has_gdal", True)
    mask_calls = {"count": 0}
    mask_summary = {"masked_pixel_count": 7, "footprint_count": 1}

    def _fake_mask(
        inset_path, bounding_box_wgs84, definition, footprint_prefetch=None
    ):
        mask_calls["count"] += 1
        return mask_summary

    monkeypatch.setattr(
        INSETS, "mask_building_footprints_in_surface_model", _fake_mask
    )

    class _FlagStrategy:
        def discover(self, definition, bounding_box_wgs84):
            return [{"note": "covers"}]

        def fetch(
            self,
            definition,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
        ):
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            with open(destination_path, "wb") as handle:
                handle.write(b"synthetic-surface-model")
            return {"provider": definition["code"]}

    monkeypatch.setitem(
        INSETS.ACCESS_STRATEGIES, "flag_test_strategy", _FlagStrategy
    )
    box = (-1.0, -1.0, 1.0, 1.0)

    # Flag ON -> masking runs and its summary lands under the flag key.
    definition_on = {
        "code": "MASKON",
        "access_strategy": "flag_test_strategy",
        "role": INSETS.ROLE_AIRPORT_INSET,
        "enabled": True,
        "priority": 1.0,
        INSETS.SURFACE_MODEL_BUILDING_MASKING: True,
    }
    provenance_on = INSETS.fetch_inset(
        definition_on, box, 30.0, str(tmp_path / "on.tif")
    )
    assert provenance_on[INSETS.SURFACE_MODEL_BUILDING_MASKING] is mask_summary
    assert mask_calls["count"] == 1

    # Flag OFF -> no masking, no key, no call.
    definition_off = dict(definition_on)
    definition_off["code"] = "MASKOFF"
    definition_off[INSETS.SURFACE_MODEL_BUILDING_MASKING] = False
    provenance_off = INSETS.fetch_inset(
        definition_off, box, 30.0, str(tmp_path / "off.tif")
    )
    assert INSETS.SURFACE_MODEL_BUILDING_MASKING not in provenance_off
    assert mask_calls["count"] == 1


# =====================================================================
# The shipped COPERNICUSGLO30 definition (global surface-model fallback)
# =====================================================================
def test_copernicus_glo30_ships_with_masking_flag_and_ranks_last():
    INSETS.initialize_elevation_providers_dict()
    assert "COPERNICUSGLO30" in INSETS.elevation_providers_dict
    copernicus = INSETS.elevation_providers_dict["COPERNICUSGLO30"]
    # The surface-model masking flag parses to a real boolean True.
    assert copernicus[INSETS.SURFACE_MODEL_BUILDING_MASKING] is True
    assert copernicus["access_strategy"] == "degree_named_cog"
    assert copernicus["role"] == INSETS.ROLE_AIRPORT_INSET
    assert copernicus["priority"] == 1.0

    # It is the global fallback: LAST among the enabled airport_inset
    # providers in the auto ordering (every real source outranks it).
    selected = INSETS.select_provider_definitions("auto")
    codes = [definition["code"] for definition in selected]
    assert "COPERNICUSGLO30" in codes
    assert codes[-1] == "COPERNICUSGLO30"


# =====================================================================
# Distance-transform masked-hole fill (vectorized inpaint replacement)
# =====================================================================
def test_distance_transform_fill_fills_holes_from_nearest_ground():
    """Every masked (non-source) cell takes a nearest-ground value; the
    trusted-ground (source) cells stay byte-identical."""
    values = numpy.array(
        [
            [10.0, 10.0, 10.0, 10.0, 10.0],
            [10.0, 999.0, 999.0, 999.0, 10.0],
            [10.0, 999.0, 999.0, 999.0, 10.0],
            [10.0, 10.0, 10.0, 10.0, 10.0],
        ]
    )
    source_mask = values == 10.0  # the ring of ground; the 999 block is holes
    filled = INSETS._fill_masked_by_distance_transform(
        values, source_mask, smoothing_iterations=0
    )
    # No sentinel/rooftop value survives anywhere.
    assert not numpy.any(filled == 999.0)
    # Holes are filled from the surrounding ground (all 10.0 here).
    assert numpy.allclose(filled[~source_mask], 10.0)
    # Source cells are untouched, exactly.
    assert numpy.array_equal(filled[source_mask], values[source_mask])


def test_distance_transform_fill_is_deterministic():
    rng = numpy.random.default_rng(1234)
    values = rng.normal(size=(37, 41)) * 5.0 + 100.0
    source_mask = rng.random((37, 41)) > 0.3  # ~70% ground, 30% holes
    first = INSETS._fill_masked_by_distance_transform(
        values, source_mask, smoothing_iterations=2
    )
    second = INSETS._fill_masked_by_distance_transform(
        values, source_mask, smoothing_iterations=2
    )
    # Bit-for-bit identical across repeated runs (no order/thread dependence).
    assert numpy.array_equal(first, second)


def test_distance_transform_fill_preserves_source_cells_with_smoothing():
    """Smoothing passes never modify a single source (unmasked) cell."""
    rng = numpy.random.default_rng(7)
    values = rng.normal(size=(25, 25)) + 50.0
    source_mask = numpy.ones((25, 25), dtype=bool)
    source_mask[8:16, 8:16] = False  # a central hole
    filled = INSETS._fill_masked_by_distance_transform(
        values, source_mask, smoothing_iterations=5
    )
    assert numpy.array_equal(filled[source_mask], values[source_mask])


def test_distance_transform_fill_stays_within_ground_range():
    """A nearest-ground fill never overshoots the surrounding ground values
    (no ringing / no rooftop leakage), even on a sloped ground."""
    # Ground is a smooth ramp; a rectangular hole sits in the middle.
    yy, xx = numpy.mgrid[0:30, 0:30]
    values = 100.0 + 0.5 * xx + 0.3 * yy
    values_with_holes = values.copy()
    source_mask = numpy.ones((30, 30), dtype=bool)
    source_mask[10:20, 10:20] = False
    values_with_holes[~source_mask] = 5000.0  # rooftop contamination
    filled = INSETS._fill_masked_by_distance_transform(
        values_with_holes, source_mask, smoothing_iterations=2
    )
    ground_min = values[source_mask].min()
    ground_max = values[source_mask].max()
    filled_holes = filled[~source_mask]
    assert filled_holes.min() >= ground_min - 1e-9
    assert filled_holes.max() <= ground_max + 1e-9


def test_distance_transform_fill_no_sources_returns_unchanged():
    values = numpy.array([[1.0, 2.0], [3.0, 4.0]])
    source_mask = numpy.zeros((2, 2), dtype=bool)  # nothing trusted
    filled = INSETS._fill_masked_by_distance_transform(
        values, source_mask, smoothing_iterations=2
    )
    assert numpy.array_equal(filled, values)


def test_inset_fill_method_env_gate(monkeypatch):
    monkeypatch.delenv("O4_INSET_FILL_METHOD", raising=False)
    assert (
        INSETS._inset_fill_method()
        == INSETS.INSET_FILL_METHOD_DISTANCE_TRANSFORM
    )
    monkeypatch.setenv("O4_INSET_FILL_METHOD", "gdal_fillnodata")
    assert INSETS._inset_fill_method() == INSETS.INSET_FILL_METHOD_LEGACY
    monkeypatch.setenv("O4_INSET_FILL_METHOD", "distance_transform")
    assert (
        INSETS._inset_fill_method()
        == INSETS.INSET_FILL_METHOD_DISTANCE_TRANSFORM
    )


def test_collect_footprints_is_source_agnostic_union(monkeypatch):
    """The mask sourcing is a package + OSM union; today package is empty
    so the union is exactly the OSM set (behaviour preserved), but the
    collector shape supports the union without touching the fill."""
    sentinel_osm = ["osm-polygon-a", "osm-polygon-b"]
    monkeypatch.setattr(
        INSETS, "openstreetmap_building_footprints",
        lambda box, footprint_prefetch=None: list(sentinel_osm),
    )
    monkeypatch.setattr(
        INSETS, "package_object_footprints", lambda box, defn: [],
    )
    footprints, label = INSETS._collect_inset_building_footprints(
        (0.0, 0.0, 1.0, 1.0), {"code": "X"}
    )
    assert footprints == sentinel_osm
    assert "OpenStreetMap" in label

    # With a package source present, the union carries both and labels it.
    monkeypatch.setattr(
        INSETS, "package_object_footprints",
        lambda box, defn: ["pkg-1"],
    )
    footprints, label = INSETS._collect_inset_building_footprints(
        (0.0, 0.0, 1.0, 1.0), {"code": "X"}
    )
    assert footprints == ["pkg-1"] + sentinel_osm
    assert "package" in label and "OpenStreetMap" in label


# =====================================================================
# Package (installed airport scenery) object-footprint sourcing
# =====================================================================
# A box strictly inside tile (+35, -087): group +30-090.
_PACK_BOX = (-86.98, 35.90, -86.94, 35.94)
_PACK_TILE_DSF = os.path.join("+30-090", "+35-087.dsf")


def _write_fake_custom_scenery(root):
    """A fake X-Plane root exercising every pack-scan filter.

    ``Test Airport``: an enabled airport pack (apt.dat + tile DSF) -- the
    one the scan must select.  ``Disabled Airport``: identical but marked
    SCENERY_PACK_DISABLED.  ``Ortho Tiles``: a tile DSF but no apt.dat
    (not an airport pack).  ``Global Airports``: excluded by name.
    """
    custom_scenery = os.path.join(root, "Custom Scenery")
    for pack_name in (
        "Test Airport",
        "Disabled Airport",
        "Ortho Tiles",
        "Global Airports",
    ):
        nav_data = os.path.join(custom_scenery, pack_name, "Earth nav data")
        os.makedirs(os.path.dirname(os.path.join(nav_data, _PACK_TILE_DSF)))
        with open(os.path.join(nav_data, _PACK_TILE_DSF), "w") as handle:
            handle.write("")
        if pack_name != "Ortho Tiles":
            with open(os.path.join(nav_data, "apt.dat"), "w") as handle:
                handle.write("")
    with open(
        os.path.join(custom_scenery, "scenery_packs.ini"), "w"
    ) as handle:
        handle.write(
            "I\n1000 Version\nSCENERY\n\n"
            "SCENERY_PACK Custom Scenery/Test Airport/\n"
            "SCENERY_PACK_DISABLED Custom Scenery/Disabled Airport/\n"
        )
    return custom_scenery


def _configuration_module(monkeypatch, **attributes):
    """Install a stand-in ``O4_Config_Utils`` for the sys.modules idiom."""
    import sys
    import types

    values = {"cifp_data_path": "", "custom_scenery_dir": ""}
    values.update(attributes)
    module = types.SimpleNamespace(**values)
    monkeypatch.setitem(sys.modules, "O4_Config_Utils", module)
    return module


def test_airport_pack_scan_selects_enabled_airport_packs(tmp_path):
    root = str(tmp_path / "X-Plane 12")
    _write_fake_custom_scenery(root)
    dsf_paths = INSETS._airport_pack_dsf_paths(root, _PACK_BOX)
    assert dsf_paths == [
        os.path.join(
            root, "Custom Scenery", "Test Airport", "Earth nav data",
            _PACK_TILE_DSF,
        )
    ]


def test_dsf_tile_enumeration_covers_corner_straddling_boxes():
    assert INSETS._dsf_tile_coordinates_for_bounding_box(_PACK_BOX) == [
        (35, -87)
    ]
    # A box across a tile corner touches all four tiles.
    corner_box = (-87.01, 35.99, -86.99, 36.01)
    assert sorted(
        INSETS._dsf_tile_coordinates_for_bounding_box(corner_box)
    ) == [(35, -88), (35, -87), (36, -88), (36, -87)]


def test_xplane_root_resolution_prefers_cifp_then_custom_scenery(
    tmp_path, monkeypatch
):
    root = str(tmp_path / "X-Plane 12")
    os.makedirs(os.path.join(root, "Custom Scenery"))
    cifp_directory = os.path.join(root, "Custom Data", "CIFP")
    os.makedirs(cifp_directory)
    # CIFP inside the install wins.
    _configuration_module(monkeypatch, cifp_data_path=cifp_directory)
    assert INSETS._xplane_root_for_package_footprints() == root
    # A CIFP folder outside any install (no Custom Scenery two levels up,
    # e.g. a Navigraph download) falls through to custom_scenery_dir.
    navigraph_directory = str(tmp_path / "Navigraph" / "CIFP")
    os.makedirs(navigraph_directory)
    _configuration_module(
        monkeypatch,
        cifp_data_path=navigraph_directory,
        custom_scenery_dir=os.path.join(root, "Custom Scenery"),
    )
    assert INSETS._xplane_root_for_package_footprints() == root
    # No configuration at all: no root.
    _configuration_module(monkeypatch)
    assert INSETS._xplane_root_for_package_footprints() is None


def test_package_footprints_read_pack_objects_and_clip_to_box(
    tmp_path, monkeypatch
):
    from shapely.geometry import Polygon

    from auto_patch import dsf_reader

    root = str(tmp_path / "X-Plane 12")
    _write_fake_custom_scenery(root)
    _configuration_module(
        monkeypatch, custom_scenery_dir=os.path.join(root, "Custom Scenery")
    )
    inside_ring = [
        (-86.961, 35.920), (-86.959, 35.920),
        (-86.959, 35.922), (-86.961, 35.922),
    ]
    outside_ring = [
        (-87.500, 35.500), (-87.499, 35.500),
        (-87.499, 35.501), (-87.500, 35.501),
    ]
    read_paths = []

    def _fake_read(dsf_path, cache_dir=None, xplane_root=None):
        read_paths.append((dsf_path, xplane_root))
        return [
            (inside_ring, [], "object"),
            (outside_ring, [], "object"),
            ([], [], "object"),  # degenerate ring: skipped, never raises
        ]

    monkeypatch.setattr(dsf_reader, "read_dsf_object_buildings", _fake_read)
    footprints = INSETS.package_object_footprints(_PACK_BOX, {"code": "X"})
    # Only the enabled airport pack was read, with the resolved root.
    assert read_paths == [
        (
            os.path.join(
                root, "Custom Scenery", "Test Airport", "Earth nav data",
                _PACK_TILE_DSF,
            ),
            root,
        )
    ]
    # Only the in-box footprint survives, as a plain lon/lat polygon.
    assert len(footprints) == 1
    assert footprints[0].equals(Polygon(inside_ring))


def test_package_footprints_disabled_by_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("O4_INSET_PACKAGE_FOOTPRINTS", "0")

    def _must_not_run():
        raise AssertionError("root resolution ran despite the kill switch")

    monkeypatch.setattr(
        INSETS, "_xplane_root_for_package_footprints", _must_not_run
    )
    assert INSETS.package_object_footprints(_PACK_BOX, {"code": "X"}) == []


def test_package_footprints_never_fail_the_fetch(tmp_path, monkeypatch):
    # No configured root: empty, no exception.
    _configuration_module(monkeypatch)
    assert INSETS.package_object_footprints(_PACK_BOX, {"code": "X"}) == []
    # Configured root but the DSF reader blows up: the pack is skipped.
    from auto_patch import dsf_reader

    root = str(tmp_path / "X-Plane 12")
    _write_fake_custom_scenery(root)
    _configuration_module(
        monkeypatch, custom_scenery_dir=os.path.join(root, "Custom Scenery")
    )

    def _broken_read(dsf_path, cache_dir=None, xplane_root=None):
        raise RuntimeError("corrupt DSF")

    monkeypatch.setattr(dsf_reader, "read_dsf_object_buildings", _broken_read)
    assert INSETS.package_object_footprints(_PACK_BOX, {"code": "X"}) == []
    # Even the pack scan itself failing degrades to empty.
    def _broken_scan(xplane_root, bounding_box_wgs84):
        raise OSError("unreadable Custom Scenery")

    monkeypatch.setattr(INSETS, "_airport_pack_dsf_paths", _broken_scan)
    assert INSETS.package_object_footprints(_PACK_BOX, {"code": "X"}) == []


# =====================================================================
# Tile-level building-footprint prefetch (one extract pass per tile)
# =====================================================================
def _shapely_box(west, south, east, north):
    from shapely.geometry import box as box_geometry

    return box_geometry(west, south, east, north)


def test_prefetch_loads_once_and_clips_per_airport(monkeypatch):
    """Two airports' queries share ONE extract pass carrying BOTH boxes
    (never their bounding rectangle), and each query gets only its own
    box's footprints back."""
    load_calls = {"count": 0, "boxes": None}
    building_a = _shapely_box(0.40, 10.40, 0.45, 10.45)  # inside box A
    building_b = _shapely_box(2.40, 12.40, 2.45, 12.45)  # inside box B
    between = _shapely_box(1.50, 11.50, 1.55, 11.55)  # in neither box

    def _fake_load(osm_layer, boxes):
        load_calls["count"] += 1
        load_calls["boxes"] = boxes
        return True

    monkeypatch.setattr(
        INSETS, "_load_building_layer_from_extracts", _fake_load
    )
    monkeypatch.setattr(
        INSETS,
        "_building_footprint_polygons_from_layer",
        lambda osm_layer: [building_a, building_b, between],
    )
    box_a = (0.0, 10.0, 1.0, 11.0)  # (west, south, east, north)
    box_b = (2.0, 12.0, 3.0, 13.0)
    prefetch = INSETS.TileBuildingFootprintPrefetch([box_a, box_b])
    # Lazy: constructing the prefetch reads nothing.
    assert load_calls["count"] == 0

    assert prefetch.footprints_intersecting_box(box_a) == [building_a]
    assert prefetch.footprints_intersecting_box(box_b) == [building_b]
    # ONE extract pass served both airports...
    assert load_calls["count"] == 1
    # ...and it carried BOTH boxes, in the extracts backend's
    # (south, west, north, east) order.
    assert load_calls["boxes"] == [
        (10.0, 0.0, 11.0, 1.0),
        (12.0, 2.0, 13.0, 3.0),
    ]


def test_prefetch_uncovered_box_answers_none_without_loading(monkeypatch):
    def _must_not_load(osm_layer, boxes):
        raise AssertionError("an uncovered box must never trigger a load")

    monkeypatch.setattr(
        INSETS, "_load_building_layer_from_extracts", _must_not_load
    )
    prefetch = INSETS.TileBuildingFootprintPrefetch([(0.0, 10.0, 1.0, 11.0)])
    assert prefetch.footprints_intersecting_box((5.0, 5.0, 6.0, 6.0)) is None


def test_prefetch_failure_falls_back_to_per_box_path(monkeypatch):
    """When the extracts cannot serve the batched request, the per-box
    path (extracts, then Overpass) runs exactly as without a prefetch."""
    import O4_OSM_Utils as OSM

    monkeypatch.setattr(
        INSETS,
        "_load_building_layer_from_extracts",
        lambda osm_layer, boxes: False,
    )
    per_box_queries = {"count": 0}

    def _fake_overpass_query(statements, bbox, osm_layer):
        per_box_queries["count"] += 1
        return True

    monkeypatch.setattr(OSM, "OSM_query_to_OSM_layer", _fake_overpass_query)
    sentinel = [_shapely_box(0.4, 10.4, 0.5, 10.5)]
    monkeypatch.setattr(
        INSETS,
        "_building_footprint_polygons_from_layer",
        lambda osm_layer: list(sentinel),
    )
    box = (0.0, 10.0, 1.0, 11.0)
    prefetch = INSETS.TileBuildingFootprintPrefetch([box])
    result = INSETS.openstreetmap_building_footprints(
        box, footprint_prefetch=prefetch
    )
    assert result == sentinel
    assert per_box_queries["count"] == 1


def test_prefetch_serves_and_per_box_path_never_runs(monkeypatch):
    """A served prefetch answer bypasses the per-box fetch entirely."""
    import O4_OSM_Utils as OSM

    building = _shapely_box(0.40, 10.40, 0.45, 10.45)
    monkeypatch.setattr(
        INSETS,
        "_load_building_layer_from_extracts",
        lambda osm_layer, boxes: True,
    )
    monkeypatch.setattr(
        INSETS,
        "_building_footprint_polygons_from_layer",
        lambda osm_layer: [building],
    )

    def _must_not_query(statements, bbox, osm_layer):
        raise AssertionError("per-box Overpass must not run")

    monkeypatch.setattr(OSM, "OSM_query_to_OSM_layer", _must_not_query)
    box = (0.0, 10.0, 1.0, 11.0)
    prefetch = INSETS.TileBuildingFootprintPrefetch([box])
    result = INSETS.openstreetmap_building_footprints(
        box, footprint_prefetch=prefetch
    )
    assert result == [building]


def test_ensure_airport_insets_threads_one_prefetch_to_masking(
    tmp_path, monkeypatch
):
    """The orchestration hands the SAME tile-level prefetch (carrying
    every airport's box) to each airport's masking pass."""
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    seen_prefetches = []

    def _fake_mask(
        inset_path, bounding_box_wgs84, definition, footprint_prefetch=None
    ):
        seen_prefetches.append(footprint_prefetch)
        return {"masked_pixel_count": 0, "footprint_count": 0}

    monkeypatch.setattr(
        INSETS, "mask_building_footprints_in_surface_model", _fake_mask
    )

    @INSETS.register_access_strategy("prefetch_thread_test_strategy")
    class _Strategy:
        def discover(self, definition, bounding_box_wgs84):
            return [{"note": "covers"}]

        def fetch(
            self,
            definition,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
        ):
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            with open(destination_path, "wb") as handle:
                handle.write(b"synthetic-surface-model")
            return {"provider": definition["code"]}

    try:
        definition = {
            "code": "PREFTEST",
            "access_strategy": "prefetch_thread_test_strategy",
            "role": INSETS.ROLE_AIRPORT_INSET,
            "enabled": True,
            "priority": 1.0,
            INSETS.SURFACE_MODEL_BUILDING_MASKING: True,
        }
        boxes = {
            "AAAA": (0.0, 10.0, 1.0, 11.0),
            "BBBB": (2.0, 12.0, 3.0, 13.0),
        }
        INSETS.ensure_airport_insets(10, 0, boxes, [definition], 3.0)
    finally:
        INSETS.ACCESS_STRATEGIES.pop("prefetch_thread_test_strategy", None)

    assert len(seen_prefetches) == 2
    assert all(
        isinstance(prefetch, INSETS.TileBuildingFootprintPrefetch)
        for prefetch in seen_prefetches
    )
    # ONE shared prefetch, carrying both airports' boxes.
    assert seen_prefetches[0] is seen_prefetches[1]
    assert sorted(seen_prefetches[0]._boxes) == [
        (0.0, 10.0, 1.0, 11.0),
        (2.0, 12.0, 3.0, 13.0),
    ]
