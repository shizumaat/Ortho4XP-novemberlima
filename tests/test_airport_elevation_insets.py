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
