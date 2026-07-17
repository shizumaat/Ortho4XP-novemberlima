"""Tests for the coastal-bathymetry PROVIDER-REGISTRY side.

Covers the additive changes in ``src/O4_Airport_Elevation_Insets.py`` and the
role filter in ``src/O4_Elevation_Level.py`` frozen by
``docs/specs/coastal-bathymetry-spec.md`` sections 2 and 7:

  * ``StaticStacCatalogStrategy`` root-item discovery -- NOAA NCEI CUDEM
    catalogs link tiles directly off the root (``rel="item"``, no child
    collections), which the strategy handles by synthesizing one
    pseudo-collection whose bounding box is the provider ``coverage_bbox``;
    the New Zealand-style child-collection tree must still work unchanged.
  * ``warp_vsicurl_sources_to_geotiff`` ``value_floor_m`` threading -- the
    post-warp sanitizer floor is per-call, so a bathymetry provider's deep
    seabed values survive while the terrestrial default still discards them.
  * ``select_bathymetry_definition`` and the terrain-path role filters --
    a ``role=bathymetry`` provider is returned only by the dedicated entry
    point, never by airport-inset selection or the wide-area overlay
    enumeration (its vertical datum is local tidal, spec section 2.1).

All headless: synthetic in-memory registries, canned JSON in place of HTTP,
``tmp_path`` for any file the code writes.  The value-floor test is skipped
cleanly when the ``osgeo`` GDAL bindings are unavailable.
"""

import os
import sys

import numpy
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

import O4_Airport_Elevation_Insets as INSETS
import O4_Elevation_Level as ELEVATION_LEVEL
import O4_File_Names as FNAMES

try:
    from osgeo import gdal, osr

    HAS_GDAL = True
except Exception:
    HAS_GDAL = False

requires_gdal = pytest.mark.skipif(
    not HAS_GDAL, reason="osgeo (GDAL python bindings) not available"
)


# The main Hawaiian-islands coverage box shared by the fixtures, mirroring
# Providers/Elevation/CUDEMHAWAII.elv.
HAWAII_COVERAGE_BBOX = (-160.5, 18.5, -154.5, 22.5)


# =====================================================================
# Synthetic registry helpers (mirroring test_elevation_level_providers)
# =====================================================================
class _WideAreaStrategy:
    """A windowed reader -- eligible for whole-tile overlays if role allows."""

    supports_wide_area = True


def _install_registry(monkeypatch, definitions, strategies=None):
    """Install a synthetic provider registry + strategy table on INSETS."""
    registry = {definition["code"]: definition for definition in definitions}
    monkeypatch.setattr(
        INSETS,
        "ACCESS_STRATEGIES",
        dict(strategies or {"wide": _WideAreaStrategy}),
    )
    monkeypatch.setattr(INSETS, "elevation_providers_dict", registry)
    monkeypatch.setattr(
        INSETS,
        "initialize_elevation_providers_dict",
        lambda *args, **kwargs: registry,
    )
    return registry


def _bathymetry_definition(code="CUDEMHAWAII", access_strategy="wide"):
    return {
        "code": code,
        "access_strategy": access_strategy,
        "role": INSETS.ROLE_BATHYMETRY,
        "enabled": True,
        "priority": 100.0,
        "native_resolution_m": 3.4,
        "coverage_bbox": HAWAII_COVERAGE_BBOX,
        "value_floor_m": "-11100.0",
    }


def _airport_inset_definition(code="LOCALLIDAR", access_strategy="wide"):
    return {
        "code": code,
        "access_strategy": access_strategy,
        "role": INSETS.ROLE_AIRPORT_INSET,
        "enabled": True,
        "priority": 50.0,
        "native_resolution_m": 1.0,
        "coverage_bbox": HAWAII_COVERAGE_BBOX,
    }


# =====================================================================
# static_stac root-item discovery (spec section 2.3)
# =====================================================================
def _install_canned_static_stac(monkeypatch, tmp_path, documents):
    """Point the index at tmp_path and serve canned JSON by URL."""
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)

    def _fake_fetch_json(self, session, url):
        return documents.get(url)

    monkeypatch.setattr(
        INSETS.StaticStacCatalogStrategy, "_fetch_json", _fake_fetch_json
    )


def test_static_stac_discovers_root_items(monkeypatch, tmp_path):
    """A catalog with only rel=item links yields the intersecting assets."""
    catalog_url = "https://example.test/cudem/catalog.json"
    near_kauai = "https://example.test/cudem/items/tile_near.json"
    far_oahu = "https://example.test/cudem/items/tile_far.json"
    documents = {
        # Root catalog: NO child links, two item links (the NCEI shape).
        catalog_url: {
            "links": [
                {"rel": "item", "href": near_kauai},
                {"rel": "item", "href": far_oahu},
            ]
        },
        # CUDEM keys its single asset by tile name, not "dtm"; the
        # finest-GeoTIFF fallback in _select_stac_dtm_assets handles it.
        near_kauai: {
            "bbox": [-159.6, 21.8, -159.4, 22.0],
            "assets": {
                "ncei19_n22x00_w159x50": {
                    "href": "https://example.test/data/tile_near.tif",
                    "type": "image/tiff; application=geotiff",
                }
            },
        },
        far_oahu: {
            "bbox": [-158.1, 21.2, -157.9, 21.4],
            "assets": {
                "ncei19_n21x25_w158x00": {
                    "href": "https://example.test/data/tile_far.tif",
                    "type": "image/tiff; application=geotiff",
                }
            },
        },
    }
    _install_canned_static_stac(monkeypatch, tmp_path, documents)
    definition = {
        "code": "CUDEMHAWAII",
        "access_strategy": "static_stac",
        "catalog_url": catalog_url,
        "coverage_bbox": HAWAII_COVERAGE_BBOX,
    }
    strategy = INSETS.StaticStacCatalogStrategy()
    # Query a box overlapping the near tile only.
    sources = strategy.discover(
        definition, (-159.55, 21.85, -159.45, 21.95)
    )
    assert sources is not None
    hrefs = [entry["href"] for entry in sources]
    assert hrefs == ["https://example.test/data/tile_near.tif"]


def test_static_stac_child_catalog_still_works(monkeypatch, tmp_path):
    """The New Zealand-style child-collection tree is byte-identical."""
    catalog_url = "https://example.test/nz/catalog.json"
    collection_url = "https://example.test/nz/collections/survey1.json"
    item_url = "https://example.test/nz/collections/items/tile.json"
    documents = {
        catalog_url: {
            "links": [{"rel": "child", "href": collection_url}]
        },
        collection_url: {
            "extent": {
                "spatial": {"bbox": [[-159.6, 21.8, -159.4, 22.0]]}
            },
            "links": [{"rel": "item", "href": item_url}],
        },
        item_url: {
            "bbox": [-159.6, 21.8, -159.4, 22.0],
            "assets": {
                "dtm": {
                    "href": "https://example.test/data/child_tile.tif",
                    "type": "image/tiff; application=geotiff",
                }
            },
        },
    }
    _install_canned_static_stac(monkeypatch, tmp_path, documents)
    definition = {
        "code": "NZSURVEY",
        "access_strategy": "static_stac",
        "catalog_url": catalog_url,
        # A child-tree catalog needs no coverage_bbox for its collections;
        # the pre-filter still admits the query.
        "coverage_bbox": HAWAII_COVERAGE_BBOX,
    }
    strategy = INSETS.StaticStacCatalogStrategy()
    sources = strategy.discover(
        definition, (-159.55, 21.85, -159.45, 21.95)
    )
    assert sources is not None
    hrefs = [entry["href"] for entry in sources]
    assert hrefs == ["https://example.test/data/child_tile.tif"]


# =====================================================================
# value_floor_m threading (spec section 2.3)
# =====================================================================
def _write_block_geotiff(path, west, south, east, north):
    """Three horizontal value blocks: 5, -1000, -20000 (left to right)."""
    columns = 30
    rows = 12
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, columns, rows, 1, gdal.GDT_Float32)
    pixel_width = (east - west) / columns
    pixel_height = (south - north) / rows  # negative (north-up)
    dataset.SetGeoTransform((west, pixel_width, 0, north, 0, pixel_height))
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    values = numpy.empty((rows, columns), dtype=numpy.float32)
    values[:, : columns // 3] = 5.0
    values[:, columns // 3 : 2 * columns // 3] = -1000.0
    values[:, 2 * columns // 3 :] = -20000.0
    band = dataset.GetRasterBand(1)
    band.WriteArray(values)
    band.FlushCache()
    dataset = None
    return path


def _valid_values(path):
    dataset = gdal.Open(path)
    values = dataset.GetRasterBand(1).ReadAsArray()
    dataset = None
    return values[values != -32768.0]


@requires_gdal
def test_value_floor_default_discards_deep_values(monkeypatch, tmp_path):
    """The terrestrial default floor (-600 m) rejects -1000 and -20000."""
    monkeypatch.setattr(INSETS, "has_gdal", True)
    source = str(tmp_path / "source.tif")
    _write_block_geotiff(source, -159.6, 21.8, -159.4, 22.0)
    destination = str(tmp_path / "warp_default.tif")
    ok = INSETS.warp_vsicurl_sources_to_geotiff(
        [source], (-159.6, 21.8, -159.4, 22.0), 15.0, destination
    )
    assert ok
    valid = _valid_values(destination)
    assert valid.size > 0
    # Everything that survived the default floor is above -600 m; the deep
    # blocks became nodata.
    assert float(valid.min()) > -600.0
    assert numpy.any(numpy.abs(valid - 5.0) < 1.0)


@requires_gdal
def test_value_floor_bathymetry_preserves_depths(monkeypatch, tmp_path):
    """A -11100 m floor keeps -1000 while still rejecting -20000."""
    monkeypatch.setattr(INSETS, "has_gdal", True)
    source = str(tmp_path / "source.tif")
    _write_block_geotiff(source, -159.6, 21.8, -159.4, 22.0)
    destination = str(tmp_path / "warp_bathy.tif")
    ok = INSETS.warp_vsicurl_sources_to_geotiff(
        [source],
        (-159.6, 21.8, -159.4, 22.0),
        15.0,
        destination,
        value_floor_m=-11100.0,
    )
    assert ok
    valid = _valid_values(destination)
    assert valid.size > 0
    # -1000 survives; -20000 is below the floor and gone.
    assert numpy.any(numpy.abs(valid - (-1000.0)) < 1.0)
    assert not numpy.any(valid < -11100.0)
    assert not numpy.any(numpy.abs(valid - (-20000.0)) < 1.0)


# =====================================================================
# select_bathymetry_definition + terrain-path role filters
# =====================================================================
def test_intertidal_flag_parses_from_real_registry():
    """The shipped .elv files carry the intertidal flag: the *TIDAL
    twins (exposed-flats lidar) parse True, real seabed bathymetry
    (CUDEM) parses False, and providers without the key default False.
    Reads the real Providers/Elevation directory — no network."""
    saved = INSETS.elevation_providers_dict
    try:
        INSETS.elevation_providers_dict = {}
        definitions = INSETS.initialize_elevation_providers_dict()
        assert definitions["PORTUGALTIDAL"]["intertidal"] is True
        assert definitions["SCOTLANDTIDAL"]["intertidal"] is True
        assert definitions["CUDEMHAWAII"]["intertidal"] is False
        assert definitions["GEBCO2024"]["intertidal"] is False
    finally:
        INSETS.elevation_providers_dict = saved


def test_select_bathymetry_returns_only_bathymetry_role(monkeypatch):
    """The dedicated entry point returns the covering bathymetry provider."""
    _install_registry(
        monkeypatch,
        [_bathymetry_definition(), _airport_inset_definition()],
    )
    # Kauai tile (+22-160): both providers' coverage boxes reach it.
    winner = INSETS.select_bathymetry_definition(22, -160)
    assert winner is not None
    assert winner["code"] == "CUDEMHAWAII"
    assert winner["role"] == INSETS.ROLE_BATHYMETRY


def test_select_bathymetry_none_off_coverage(monkeypatch):
    """No bathymetry provider covers a mainland-Europe tile."""
    _install_registry(
        monkeypatch,
        [_bathymetry_definition(), _airport_inset_definition()],
    )
    # A tile over France (+48+002) -- far outside the Hawaii box.
    assert INSETS.select_bathymetry_definition(48, 2) is None


def test_bathymetry_never_selected_as_airport_inset(monkeypatch):
    """Airport-inset selection excludes the bathymetry provider."""
    _install_registry(
        monkeypatch,
        [_bathymetry_definition(), _airport_inset_definition()],
    )
    selected = INSETS.select_provider_definitions(
        "auto", role=INSETS.ROLE_AIRPORT_INSET
    )
    codes = [definition["code"] for definition in selected]
    assert "CUDEMHAWAII" not in codes
    assert "LOCALLIDAR" in codes


def test_bathymetry_never_selected_by_wide_area_overlay(monkeypatch):
    """The wide-area enumeration filters out the bathymetry provider.

    The bathymetry provider here uses a wide-area-capable strategy, so its
    exclusion proves the ROLE filter (spec section 2.1), not merely the
    ``supports_wide_area`` gate.
    """
    _install_registry(
        monkeypatch,
        [_bathymetry_definition(), _airport_inset_definition()],
    )
    candidates = ELEVATION_LEVEL._wide_area_candidate_definitions(
        22, -160, "auto"
    )
    codes = [definition["code"] for definition in candidates]
    assert "CUDEMHAWAII" not in codes
    # The airport-inset provider (also wide-area here) is still admitted --
    # only the bathymetry role is filtered.
    assert "LOCALLIDAR" in codes
