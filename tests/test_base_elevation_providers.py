"""Compatibility tests for the base-tier elevation provider refactor.

Phase A2 of ``docs/airport_elevation_insets_spec.md`` (section 3.6) moved
the legacy base elevation sources (the ``available_sources`` tuple +
if/elif download chain in ``O4_DEM_Utils.ensure_elevation``) onto the
declarative ``Providers/Elevation/<CODE>.elv`` registry.  These tests pin
the refactor to the historic behaviour WITHOUT any network access:

  * download URL construction equals fixed expected strings,
  * legacy keyword aliases (View / SRTM / NED1 / NED1/3 / ALOS) resolve,
  * automatic base selection ranks by priority under the 1 arc-second cap,
  * cache paths are byte-equal to the legacy ``O4_File_Names`` results,
  * the ``ensure_elevation`` shim recycles cached files and preserves the
    legacy 0/1 return convention -- all with no request ever issued.
"""

import os

import pytest

import O4_File_Names as FNAMES
import O4_DEM_Utils as DEM
import O4_Airport_Elevation_Insets as INSETS

_HERE = os.path.dirname(os.path.abspath(__file__))
SHIPPED_PROVIDERS_DIRECTORY = os.path.normpath(
    os.path.join(_HERE, "..", "Providers", "Elevation")
)


@pytest.fixture(autouse=True)
def shipped_registry():
    """Load the SHIPPED definitions before each test (cwd-independent)."""
    INSETS.initialize_elevation_providers_dict(SHIPPED_PROVIDERS_DIRECTORY)
    yield INSETS.elevation_providers_dict


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any http request in these tests is a failure."""

    def _forbidden(*arguments, **keyword_arguments):
        raise AssertionError(
            "network request attempted in a no-network unit test"
        )

    monkeypatch.setattr(DEM, "http_request", _forbidden)


def _strategy_for(definition):
    return INSETS.ACCESS_STRATEGIES[definition["access_strategy"]]()


# =====================================================================
# Shipped definitions
# =====================================================================
def test_shipped_definitions_parse(shipped_registry):
    assert set(shipped_registry) == {
        "USGS3DEP",
        "VIEWFINDER1",
        "VIEWFINDER3",
        "NED1",
        "NED13",
        "SRTM",
        "ALOS",
    }
    for code in ("VIEWFINDER1", "VIEWFINDER3", "NED1", "NED13", "SRTM", "ALOS"):
        assert shipped_registry[code]["role"] == INSETS.ROLE_BASE, code
    assert shipped_registry["USGS3DEP"]["role"] == INSETS.ROLE_AIRPORT_INSET
    assert shipped_registry["SRTM"]["enabled"] is False
    assert shipped_registry["ALOS"]["enabled"] is False
    assert shipped_registry["VIEWFINDER1"]["priority"] == 60.0
    assert shipped_registry["VIEWFINDER3"]["priority"] == 10.0
    assert shipped_registry["NED1"]["priority"] == 70.0
    assert shipped_registry["NED13"]["priority"] == 0.0


# =====================================================================
# URL construction (fixed expected strings)
# =====================================================================
def test_viewfinder_url_for_dem1_whitelist_tile(shipped_registry):
    definition = shipped_registry["VIEWFINDER1"]
    strategy = _strategy_for(definition)
    # (46, 7) is in the Alps: archive code L32, on the dem1 whitelist.
    assert INSETS.deferranti_archive_code(46, 7) == "L32"
    assert strategy.covers(definition, 46, 7)
    assert (
        strategy.download_url(definition, 46, 7)
        == "http://viewfinderpanoramas.org/dem1/L32.zip"
    )


def test_viewfinder_url_for_dem3_tile(shipped_registry):
    # (36, -87) -- the KBNA tile -- is NOT on the dem1 whitelist.
    assert INSETS.deferranti_archive_code(36, -87) == "J16"
    assert not _strategy_for(shipped_registry["VIEWFINDER1"]).covers(
        shipped_registry["VIEWFINDER1"], 36, -87
    )
    definition = shipped_registry["VIEWFINDER3"]
    strategy = _strategy_for(definition)
    assert strategy.covers(definition, 36, -87)
    assert (
        strategy.download_url(definition, 36, -87)
        == "http://viewfinderpanoramas.org/dem3/J16.zip"
    )


def test_usgs_seamless_urls(shipped_registry):
    assert INSETS.usgs_seamless_tile_identifier(36, -87) == "n37w087"
    ned_one = shipped_registry["NED1"]
    assert _strategy_for(ned_one).download_url(ned_one, 36, -87) == (
        "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/1/TIFF/"
        "current/n37w087/USGS_1_n37w087.tif"
    )
    ned_third = shipped_registry["NED13"]
    assert _strategy_for(ned_third).download_url(ned_third, 36, -87) == (
        "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/"
        "current/n37w087/USGS_13_n37w087.tif"
    )


# =====================================================================
# Legacy keyword aliases
# =====================================================================
def test_view_alias_resolves_per_tile():
    # Whitelist tile -> the 1 arc-second definition.
    assert INSETS.resolve_base_definition(46, 7, "View")["code"] == (
        "VIEWFINDER1"
    )
    # Non-whitelist tile -> the 3 arc-second fallback.
    assert INSETS.resolve_base_definition(36, -87, "View")["code"] == (
        "VIEWFINDER3"
    )
    # Wellington: SK60 IS on the whitelist but the tile is excluded
    # (missing 1 arc-second data), so the alias falls to 3 arc-second --
    # the historic hardcoded exception, now the exclude_tiles field.
    assert INSETS.deferranti_archive_code(-42, 174) == "SK60"
    assert INSETS.resolve_base_definition(-42, 174, "View")["code"] == (
        "VIEWFINDER3"
    )


def test_direct_legacy_aliases():
    assert INSETS.resolve_base_definition(36, -87, "NED1")["code"] == "NED1"
    assert (
        INSETS.resolve_base_definition(36, -87, "NED1/3")["code"] == "NED13"
    )
    # Disabled sources stay explicitly selectable (manual-download flow).
    assert INSETS.resolve_base_definition(36, -87, "SRTM")["code"] == "SRTM"
    assert INSETS.resolve_base_definition(36, -87, "ALOS")["code"] == "ALOS"
    # Registry codes are accepted directly too.
    assert (
        INSETS.resolve_base_definition(46, 7, "VIEWFINDER3")["code"]
        == "VIEWFINDER3"
    )
    # Unknown selector -> None (the shim maps this to the legacy error).
    assert INSETS.resolve_base_definition(36, -87, "BOGUS") is None
    # An inset-role code is not a base source.
    assert INSETS.resolve_base_definition(36, -87, "USGS3DEP") is None


# =====================================================================
# Automatic selection + the 1 arc-second cap
# =====================================================================
def test_auto_selection_decision_table():
    # Continental United States tile -> NED1 (priority 70 beats all).
    assert INSETS.resolve_base_definition(36, -87, "auto")["code"] == "NED1"
    # Whitelisted Alps tile -> VIEWFINDER1 (60; NED1 does not cover).
    assert (
        INSETS.resolve_base_definition(46, 7, "auto")["code"] == "VIEWFINDER1"
    )
    # Anywhere else -> the global VIEWFINDER3 fallback (10).
    assert (
        INSETS.resolve_base_definition(10, 10, "auto")["code"] == "VIEWFINDER3"
    )


def test_auto_never_picks_finer_than_one_arc_second():
    # NED13 covers (36, -87) and is enabled, but its resolution (1/3
    # arc-second) is finer than the working mesh grid, so the cap excludes
    # it from auto EVEN with the highest priority in the registry.
    INSETS.elevation_providers_dict["NED13"]["priority"] = 999.0
    try:
        candidates = INSETS.select_base_definitions_auto(36, -87)
        assert "NED13" not in [
            definition["code"] for definition in candidates
        ]
        assert INSETS.resolve_base_definition(36, -87, "auto")["code"] == (
            "NED1"
        )
    finally:
        INSETS.initialize_elevation_providers_dict(
            SHIPPED_PROVIDERS_DIRECTORY
        )
    # Explicit selection still works for the capped source.
    assert (
        INSETS.resolve_base_definition(36, -87, "NED13")["code"] == "NED13"
    )


def test_disabled_sources_never_auto_picked():
    candidates = INSETS.select_base_definitions_auto(36, -87)
    codes = [definition["code"] for definition in candidates]
    assert "SRTM" not in codes
    assert "ALOS" not in codes


# =====================================================================
# Cache-path invariance (byte-equal to the legacy FNAMES results)
# =====================================================================
def test_cache_paths_equal_legacy_paths(shipped_registry):
    lat, lon = 36, -87
    for code in ("VIEWFINDER1", "VIEWFINDER3"):
        definition = shipped_registry[code]
        assert _strategy_for(definition).tile_cache_path(
            definition, lat, lon
        ) == FNAMES.viewfinderpanorama(lat, lon)
        assert _strategy_for(definition).tile_cache_path(
            definition, lat, lon
        ) == FNAMES.elevation_data("View", lat, lon)
    for code, keyword in (
        ("NED1", "NED1"),
        ("NED13", "NED1/3"),
        ("SRTM", "SRTM"),
        ("ALOS", "ALOS"),
    ):
        definition = shipped_registry[code]
        assert _strategy_for(definition).tile_cache_path(
            definition, lat, lon
        ) == FNAMES.elevation_data(keyword, lat, lon)
    # Southern/western tile too (sign handling in the block naming).
    assert _strategy_for(shipped_registry["VIEWFINDER3"]).tile_cache_path(
        shipped_registry["VIEWFINDER3"], -12, -77
    ) == FNAMES.viewfinderpanorama(-12, -77)


# =====================================================================
# The ensure_elevation shim (recycle + return convention, no network)
# =====================================================================
def test_shim_recycles_cached_file_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    cache_path = FNAMES.elevation_data("NED1", 36, -87)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    open(cache_path, "wb").close()
    # The no_network fixture makes any request raise, so a pass proves the
    # recycle happened purely from the cache.
    assert DEM.ensure_elevation("NED1", 36, -87) == 1


def test_shim_manual_download_source_missing_returns_zero(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    # SRTM downloads are dead upstream: without a manually placed file the
    # source yields 0 (and, per the no_network fixture, never requests).
    assert DEM.ensure_elevation("SRTM", 36, -87) == 0
    # With the file placed by hand it recycles.
    cache_path = FNAMES.elevation_data("SRTM", 36, -87)
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    open(cache_path, "wb").close()
    assert DEM.ensure_elevation("SRTM", 36, -87) == 1


def test_shim_unknown_source_returns_zero():
    assert DEM.ensure_elevation("BOGUS", 36, -87) == 0


# =====================================================================
# Default-source resolution (the load_data hook)
# =====================================================================
def test_default_source_resolution_long_names(monkeypatch):
    monkeypatch.setattr(DEM, "base_elevation_source", "auto")
    assert DEM.resolve_default_base_source(36, -87) == (
        'NED 1" (from USGS) - USA, Canada, Mexico'
    )
    assert DEM.resolve_default_base_source(10, 10) == (
        "Viewfinderpanoramas (J. de Ferranti) - mostly worldwide"
    )
    # Legacy keyword pin reproduces the historic default exactly.
    monkeypatch.setattr(DEM, "base_elevation_source", "View")
    assert DEM.resolve_default_base_source(36, -87) == (
        "Viewfinderpanoramas (J. de Ferranti) - mostly worldwide"
    )
    # Unresolvable value falls back to the historic default.
    monkeypatch.setattr(DEM, "base_elevation_source", "BOGUS")
    assert DEM.resolve_default_base_source(36, -87) == (
        "Viewfinderpanoramas (J. de Ferranti) - mostly worldwide"
    )
