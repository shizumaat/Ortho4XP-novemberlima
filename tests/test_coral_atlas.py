"""Contract tests for the Allen Coral Atlas bathymetry provider.

Pins the frozen behaviour of ``src/O4_Coral_Atlas.py`` (the local library +
guided in-app fetch), the ``coral_atlas_library`` access strategy registered
in ``src/O4_Airport_Elevation_Insets.py`` and the provider-fallthrough loop
in ``src/O4_Bathymetry_Band.ensure_bathymetry_band``.  The contracts, from
``docs/specs/coastal-bathymetry-spec.md`` section 8 and the module
docstrings:

* UNIT CONVENTION -- Atlas rasters carry depth as POSITIVE CENTIMETRES where
  the seabed is satellite-visible; the pipeline wants metres, negative below
  the surface.  ``convert_centimeter_depths_to_metres`` negates and scales
  positive centimetres and maps every non-positive value (land / deep /
  nodata) to the ``-32768`` sentinel, always as ``float32``.

* LOCAL LIBRARY -- ``rescan_library`` unpacks freshly dropped zips, indexes
  only ``*.tif`` files whose name contains ``bathymetry`` (with each raster's
  EPSG:4326 bounding box), and persists the index; ``entries_intersecting``
  returns the overlapping rasters and nothing for a disjoint query.

* WINDOW FETCH -- ``fetch_window_to_geotiff`` mosaics library rasters to a
  metres ``float32`` window, negative where the source was positive
  centimetres and ``-32768`` nodata elsewhere.

* ACCESS STRATEGY -- ``coral_atlas_library`` is registered; ``discover``
  reports no coverage off the ``coverage_bbox`` and when the library is
  empty; ``fetch`` (through ``INSETS.fetch_inset``) returns provenance
  carrying the definition's license and attribution.

* WEB API CLIENT -- ``AtlasApiClient`` extracts a nested access token on
  login, sets the bearer header on later calls, surfaces the server
  ``message`` on failure, returns the created area-of-interest id, wraps the
  download request body as ``{"datasets": {...}}``, and ``_find_package_url``
  locates a nested archive link (``None`` when absent).

* BAND FALLTHROUGH -- ``ensure_bathymetry_band`` walks the covering
  providers best-first and falls through a provider that yields nothing to
  the next, returning the second provider's VRT; ``fine_nearshore_only``
  drops a coarse-only provider list without any fetch.

Headless: ``tmp_path`` only, ``requests`` and the library path monkeypatched,
no network.  The GDAL-dependent tests skip cleanly when the ``osgeo`` python
bindings are unavailable.
"""

import os
import sys
import zipfile

import numpy
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

import O4_Airport_Elevation_Insets as INSETS  # noqa: E402
import O4_Bathymetry_Band as BATHYBAND  # noqa: E402
import O4_Coral_Atlas as CORAL  # noqa: E402
import O4_File_Names as FNAMES  # noqa: E402

try:
    from osgeo import gdal, osr

    HAS_GDAL = True
except Exception:
    HAS_GDAL = False

requires_gdal = pytest.mark.skipif(
    not HAS_GDAL, reason="osgeo (GDAL python bindings) not available"
)

NODATA = -32768.0


# =====================================================================
# GeoTIFF fixture helpers
# =====================================================================
def _write_int16_geotiff(path, west, south, east, north, value, columns=8,
                         rows=8):
    """A uniform int16 raster (positive centimetres) over the given bounds."""
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, columns, rows, 1, gdal.GDT_Int16)
    dataset.SetGeoTransform(
        (west, (east - west) / columns, 0, north, 0, (south - north) / rows)
    )
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.WriteArray(numpy.full((rows, columns), value, dtype=numpy.int16))
    band.FlushCache()
    dataset = None
    return path


def _read_geotiff(path):
    dataset = gdal.Open(path)
    band = dataset.GetRasterBand(1)
    values = band.ReadAsArray()
    data_type = band.DataType
    dataset = None
    return values, data_type


# =====================================================================
# 1. Unit conversion (centimetres -> metres)
# =====================================================================
def test_convert_centimeter_depths_to_metres():
    """Positive centimetres negate and scale to metres; everything else is
    the -32768 nodata sentinel, always float32."""
    values = numpy.array([200, 0, -50, 100, 2550], dtype=numpy.int16)
    metres = CORAL.convert_centimeter_depths_to_metres(values)

    assert metres.dtype == numpy.float32
    # 200 cm below the surface -> -2.0 m.
    assert metres[0] == pytest.approx(-2.0)
    # 100 cm -> -1.0 m; 2550 cm -> -25.5 m.
    assert metres[3] == pytest.approx(-1.0)
    assert metres[4] == pytest.approx(-25.5)
    # Zero and negative (land / deep / nodata) -> sentinel.
    assert metres[1] == pytest.approx(NODATA)
    assert metres[2] == pytest.approx(NODATA)


# =====================================================================
# 2. Local library indexing
# =====================================================================
def _install_library(monkeypatch, tmp_path):
    """Point the Atlas library + Elevation_dir at isolated tmp dirs."""
    library = tmp_path / "AllenCoralAtlas"
    library.mkdir()
    elevation = tmp_path / "Elevation_data"
    elevation.mkdir()
    monkeypatch.setattr(CORAL, "library_directory", lambda: str(library))
    # rescan_library sweeps FNAMES.Elevation_dir for band stamps to forget;
    # keep it inside the sandbox so no real cache is touched.
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(elevation))
    return library


@requires_gdal
def test_rescan_library_indexes_only_bathymetry(monkeypatch, tmp_path):
    """rescan_library indexes the *bathymetry* raster (with the right bbox),
    ignores the decoy, and entries_intersecting honours the bbox."""
    library = _install_library(monkeypatch, tmp_path)
    # A bathymetry raster over a known box, plus a decoy non-bathymetry tif.
    bounds = (-159.6, 21.8, -159.4, 22.0)  # (west, south, east, north)
    _write_int16_geotiff(
        str(library / "something_bathymetry_0.tif"), *bounds, value=200
    )
    _write_int16_geotiff(
        str(library / "reef_extent_0.tif"),
        -159.6, 21.8, -159.4, 22.0, value=1,
    )

    entries = CORAL.rescan_library(progress=lambda message: None)

    assert len(entries) == 1
    (entry_west, entry_south, entry_east, entry_north) = entries[0]["bbox"]
    assert entry_west == pytest.approx(bounds[0])
    assert entry_south == pytest.approx(bounds[1])
    assert entry_east == pytest.approx(bounds[2])
    assert entry_north == pytest.approx(bounds[3])
    assert "bathymetry" in entries[0]["path"].lower()

    # Overlapping query returns the raster; disjoint query returns nothing.
    overlapping = CORAL.entries_intersecting((-159.55, 21.85, -159.45, 21.95))
    assert len(overlapping) == 1
    assert os.path.isfile(overlapping[0])
    disjoint = CORAL.entries_intersecting((10.0, 40.0, 11.0, 41.0))
    assert disjoint == []


@requires_gdal
def test_rescan_library_unpacks_and_indexes_zip(monkeypatch, tmp_path):
    """A zip carrying a bathymetry tif dropped in the library is unpacked
    and indexed on the next rescan."""
    library = _install_library(monkeypatch, tmp_path)
    # Start with one loose raster.
    _write_int16_geotiff(
        str(library / "loose_bathymetry_0.tif"),
        -159.6, 21.8, -159.4, 22.0, value=200,
    )
    assert len(CORAL.rescan_library(progress=lambda message: None)) == 1

    # Build a package containing a bathymetry raster and drop it in zipped.
    inner_path = str(tmp_path / "packaged_bathymetry_0.tif")
    _write_int16_geotiff(
        inner_path, -158.6, 21.0, -158.4, 21.2, value=300
    )
    zip_path = str(library / "atlas_package.zip")
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.write(inner_path, arcname="packaged_bathymetry_0.tif")

    entries = CORAL.rescan_library(progress=lambda message: None)

    assert len(entries) == 2
    # The package was extracted into a sibling directory named after the zip.
    assert os.path.isdir(str(library / "atlas_package"))
    assert any("atlas_package" in entry["path"] for entry in entries)


# =====================================================================
# 3. Window fetch (centimetres -> metres float32)
# =====================================================================
@requires_gdal
def test_fetch_window_to_geotiff_metres_and_nodata(monkeypatch, tmp_path):
    """The fetched window is float32 metres: negative where the source was
    positive centimetres, and the -32768 sentinel where it was not."""
    # Left half 200 cm (visible seabed), right half 0 (unmeasured).
    source = str(tmp_path / "source_bathymetry.tif")
    columns, rows = 40, 8
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(source, columns, rows, 1, gdal.GDT_Int16)
    west, south, east, north = -159.6, 21.8, -159.4, 22.0
    dataset.SetGeoTransform(
        (west, (east - west) / columns, 0, north, 0, (south - north) / rows)
    )
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    values = numpy.zeros((rows, columns), dtype=numpy.int16)
    values[:, : columns // 2] = 200  # positive centimetres
    dataset.GetRasterBand(1).WriteArray(values)
    dataset.GetRasterBand(1).FlushCache()
    dataset = None

    destination = str(tmp_path / "window.tif")
    ok = CORAL.fetch_window_to_geotiff(
        [source], (west, south, east, north), 20.0, destination
    )
    assert ok

    output, data_type = _read_geotiff(destination)
    assert data_type == gdal.GDT_Float32
    valid = output[output != NODATA]
    # The measured half is present and below the surface (~ -2.0 m).
    assert valid.size > 0
    assert numpy.all(valid < 0.0)
    assert numpy.any(numpy.abs(valid - (-2.0)) < 0.2)
    # The unmeasured half became nodata.
    assert numpy.any(output == NODATA)


# =====================================================================
# 4. coral_atlas_library access strategy
# =====================================================================
def test_strategy_is_registered():
    assert "coral_atlas_library" in INSETS.ACCESS_STRATEGIES


@requires_gdal
def test_strategy_discover_no_coverage(monkeypatch, tmp_path):
    """discover returns None off the coverage_bbox and when the library is
    empty (covered on paper but no downloaded package overlaps)."""
    _install_library(monkeypatch, tmp_path)
    strategy = INSETS.CoralAtlasLibraryStrategy()
    definition = {
        "code": CORAL.PROVIDER_CODE,
        "access_strategy": "coral_atlas_library",
        "coverage_bbox": (-160.5, 18.5, -154.5, 22.5),
    }
    # Outside the coverage box entirely.
    assert strategy.discover(definition, (10.0, 40.0, 11.0, 41.0)) is None
    # Inside the coverage box but the library holds nothing overlapping.
    assert (
        strategy.discover(definition, (-159.55, 21.85, -159.45, 21.95))
        is None
    )


@requires_gdal
def test_strategy_fetch_provenance_carries_license(monkeypatch, tmp_path):
    """Driven through INSETS.fetch_inset, a covered fetch writes the window
    and returns provenance with the definition's license/attribution."""
    library = _install_library(monkeypatch, tmp_path)
    bounds = (-159.6, 21.8, -159.4, 22.0)
    _write_int16_geotiff(
        str(library / "kauai_bathymetry_0.tif"), *bounds, value=200
    )
    CORAL.rescan_library(progress=lambda message: None)

    definition = {
        "code": CORAL.PROVIDER_CODE,
        "access_strategy": "coral_atlas_library",
        "coverage_bbox": (-160.5, 18.5, -154.5, 22.5),
        "license": "CC-BY 4.0 (Allen Coral Atlas)",
        "attribution": "Allen Coral Atlas / Arizona State University",
        "vertical_datum": "Sea surface (satellite-derived)",
    }
    destination = str(tmp_path / "inset.tif")
    provenance = INSETS.fetch_inset(
        definition, (-159.55, 21.85, -159.45, 21.95), 20.0, destination
    )

    assert provenance is not None
    assert provenance["license"] == "CC-BY 4.0 (Allen Coral Atlas)"
    assert (
        provenance["attribution"]
        == "Allen Coral Atlas / Arizona State University"
    )
    assert provenance["provider"] == CORAL.PROVIDER_CODE
    # The window was actually written as metres float32 below the surface.
    output, data_type = _read_geotiff(destination)
    assert data_type == gdal.GDT_Float32
    valid = output[output != NODATA]
    assert valid.size > 0
    assert numpy.all(valid < 0.0)


# =====================================================================
# 5. AtlasApiClient (all requests through a fake Session)
# =====================================================================
class _FakeResponse:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class _FakeSession:
    """Records every request and dispatches to a per-test handler."""

    def __init__(self):
        self.requests = []
        self.handler = lambda method, url, payload: _FakeResponse()

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.requests.append(
            {
                "method": method,
                "url": url,
                "json": json,
                "headers": dict(headers or {}),
            }
        )
        return self.handler(method, url, json)


def _client_with_fake_session(monkeypatch):
    import requests

    monkeypatch.setattr(requests, "Session", _FakeSession)
    return CORAL.AtlasApiClient()


def test_login_extracts_nested_token_and_sets_bearer(monkeypatch):
    """login digs the access token out of a nested body and every later
    request carries it as a bearer header."""
    client = _client_with_fake_session(monkeypatch)

    def handler(method, url, payload):
        if url.endswith("auth/login"):
            return _FakeResponse(
                body={"data": {"session": {"access_token": "TOKEN-123"}}}
            )
        # A later call -> success, id for create_area_of_interest.
        return _FakeResponse(body={"data": {"id": "aoi-7"}})

    client._session.handler = handler
    client.login("pilot@example.test", "secret")
    assert client._token == "TOKEN-123"

    client.create_area_of_interest("Ortho4XP_N21W160", {"type": "Polygon"})
    # The SECOND request (the AOI creation) carried the bearer header.
    last_request = client._session.requests[-1]
    assert last_request["headers"].get("Authorization") == "Bearer TOKEN-123"


def test_login_failure_surfaces_server_message(monkeypatch):
    """A failed login raises AtlasApiError carrying the server message."""
    client = _client_with_fake_session(monkeypatch)

    def handler(method, url, payload):
        return _FakeResponse(
            status_code=401,
            body={"code": 401, "message": "These credentials are invalid."},
        )

    client._session.handler = handler
    with pytest.raises(CORAL.AtlasApiError) as raised:
        client.login("pilot@example.test", "wrong")
    assert "These credentials are invalid." in str(raised.value)


def test_create_area_of_interest_returns_id(monkeypatch):
    client = _client_with_fake_session(monkeypatch)
    client._session.handler = (
        lambda method, url, payload: _FakeResponse(
            body={"data": {"aoi": {"id": "aoi-42"}}}
        )
    )
    identifier = client.create_area_of_interest("area", {"type": "Polygon"})
    assert identifier == "aoi-42"


def test_request_download_body_shape(monkeypatch):
    """The download request wraps the datasets as {"datasets": {...}}."""
    client = _client_with_fake_session(monkeypatch)
    client._session.handler = (
        lambda method, url, payload: _FakeResponse(body={"status": "queued"})
    )
    client.request_download("aoi-42", {"bathymetry": "tif"})
    sent = client._session.requests[-1]
    assert sent["url"].endswith("download/aois/aoi-42")
    assert sent["json"] == {"datasets": {"bathymetry": "tif"}}


def test_find_package_url_nested_and_absent():
    """_find_package_url pulls a nested archive link out, else returns None."""
    body = {
        "status": "ready",
        "result": {
            "files": [
                {"kind": "metadata", "note": "no link here"},
                {"kind": "package", "url": "https://atlas.test/pkg/aoi.zip"},
            ]
        },
    }
    assert (
        CORAL._find_package_url(body) == "https://atlas.test/pkg/aoi.zip"
    )
    assert (
        CORAL._find_package_url({"status": "pending", "note": "no url yet"})
        is None
    )


# =====================================================================
# 6. Provider fallthrough in ensure_bathymetry_band
# =====================================================================
def _band_tile(monkeypatch, tmp_path):
    """A minimal tile whose band paths and geometry live under tmp_path."""
    import types

    from shapely.geometry import LineString

    elevation = tmp_path / "Elevation_data"
    elevation.mkdir(exist_ok=True)
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(elevation))
    # A shoreline crossing the tile centre in tile-relative degrees, so cell
    # selection succeeds without any OSM download.  _band_geometry returns
    # (coastline, inland_or_None) since the inland-reach split.
    monkeypatch.setattr(
        BATHYBAND,
        "_band_geometry",
        lambda tile: (LineString([(0.45, 0.45), (0.55, 0.55)]), None),
    )
    return types.SimpleNamespace(
        lat=21, lon=-160, bathymetry_band_km=5.0
    )


def _fake_definition(code, native_resolution_m):
    return {
        "code": code,
        "access_strategy": "coral_atlas_library",
        "role": INSETS.ROLE_BATHYMETRY,
        "native_resolution_m": native_resolution_m,
        "coverage_bbox": (-160.5, 18.5, -154.5, 22.5),
    }


@requires_gdal
def test_band_falls_through_to_second_provider(monkeypatch, tmp_path):
    """The first (fine) provider yields nothing; the band falls through to
    the second, whose written cell(s) become the returned VRT."""
    tile = _band_tile(monkeypatch, tmp_path)
    first = _fake_definition("FIRSTFINE", 10.0)
    second = _fake_definition("SECONDFINE", 10.0)
    monkeypatch.setattr(
        INSETS,
        "select_bathymetry_definitions",
        lambda lat, lon: [first, second],
    )

    fetch_calls = []

    def fake_fetch_inset(definition, bbox, resolution_m, destination_path):
        fetch_calls.append(definition["code"])
        if definition["code"] == "FIRSTFINE":
            return None  # covered on paper, no data
        # The second provider writes a real 1-cell GeoTIFF.
        _write_int16_geotiff(
            destination_path, bbox[0], bbox[1], bbox[2], bbox[3],
            value=200, columns=2, rows=2,
        )
        return {"provider": definition["code"]}

    monkeypatch.setattr(INSETS, "fetch_inset", fake_fetch_inset)

    vrt_path = BATHYBAND.ensure_bathymetry_band(tile)

    assert vrt_path is not None
    # The VRT belongs to the SECOND provider (band_<code>.vrt, lowercased).
    assert os.path.basename(vrt_path) == "band_secondfine.vrt"
    assert os.path.isfile(vrt_path)
    # Both providers were consulted, first before second.
    assert "FIRSTFINE" in fetch_calls
    assert "SECONDFINE" in fetch_calls
    assert fetch_calls.index("FIRSTFINE") < fetch_calls.index("SECONDFINE")


@requires_gdal
def test_fine_nearshore_only_drops_coarse_only_list(monkeypatch, tmp_path):
    """fine_nearshore_only filters out a coarse-only provider list and
    returns None without ever fetching a cell."""
    tile = _band_tile(monkeypatch, tmp_path)
    coarse = _fake_definition("GEBCO", 450.0)  # far above the 50 m cutoff
    monkeypatch.setattr(
        INSETS,
        "select_bathymetry_definitions",
        lambda lat, lon: [coarse],
    )
    fetch_calls = []
    monkeypatch.setattr(
        INSETS,
        "fetch_inset",
        lambda *args, **kwargs: fetch_calls.append(args) or None,
    )

    result = BATHYBAND.ensure_bathymetry_band(tile, fine_nearshore_only=True)

    assert result is None
    assert fetch_calls == []
