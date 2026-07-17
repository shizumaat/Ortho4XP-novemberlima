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
        "HRDEM",
        "COPERNICUSGLO30",
        "VIEWFINDER1",
        "VIEWFINDER3",
        "NED1",
        "NED13",
        "SRTM",
        "ALOS",
        "SONNY1",
        "ENGLAND1M",
        "NORWAY1M",
        "SWISSALTI3D",
        "SPAIN5M",
        "POLAND1M",
        "WALES1M",
        "NEWZEALAND1M",
        "FINLAND2M",
        "JAPAN5M",
        "AUSTRALIA5M",
        "TAIWAN20M",
        "FRANCE50CM",
        "BAVARIA1M",
        "NRW1M",
        "THURINGIA1M",
        "MECKLENBURG1M",
        "LOWERSAXONY1M",
        "BRANDENBURG1M",
        "SAXONY1M",
        "SAXONYANHALT1M",
        "BREMEN1M",
        "SCHLESWIGHOLSTEIN1M",
        "BADENWUERTTEMBERG1M",
        "RHINELANDPALATINATE1M",
        "HESSE1M",
        "SAARLAND18M",
        "HAMBURG1M",
        "AUSTRIA1M",
        "NETHERLANDS50CM",
        "SOUTHTYROL50CM",
        "SARDINIA1M",
        "ITALY10M",
        "URUGUAY2M",
        "ESPIRITOSANTO2M",
        "CURITIBA50CM",
        "PERNAMBUCO1M",
        "RIODEJANEIRO5M",
        "CZECHIA2M",
        "LITHUANIA1M",
        "HONGKONG5M",
        "ZAGREB1M",
        "ESTONIA1M",
        "SCOTLAND30M",
        "NORTHERNIRELAND1M",
        "SCOTLAND50CM",
        "FLANDERS1M",
        "IRELAND1M",
        "WALLONIA1M",
        "PORTUGAL2M",
        "PORTUGAL50CM",
        "DENMARK40CM",
        "SWEDEN1M",
        "CUDEMHAWAII",
        "CUDEMPUERTORICO",
        "CUDEMUSVI",
        "CUDEMCNMI",
        "CUDEMAMERICANSAMOA",
        "CUDEMCONUS",
        "CUDEMCONUSTHIRD",
        "CUDEMGUAM",
        "EMODNETBATHYMETRY",
        "GEBCO2024",
        "CORALATLAS",
        "SCOTLANDTIDAL",
        "LOWERSAXONYTIDAL",
        "SCHLESWIGHOLSTEINTIDAL",
        "HRDEMTIDAL",
        "NEWZEALANDTIDAL",
        "FRANCETIDAL",
        "PORTUGALTIDAL",
    }
    for code in (
        "VIEWFINDER1",
        "VIEWFINDER3",
        "NED1",
        "NED13",
        "SRTM",
        "ALOS",
        "SONNY1",
    ):
        assert shipped_registry[code]["role"] == INSETS.ROLE_BASE, code
    assert shipped_registry["USGS3DEP"]["role"] == INSETS.ROLE_AIRPORT_INSET
    # Phase C2: the second inset provider family (Canada HRDEM via STAC).
    assert shipped_registry["HRDEM"]["role"] == INSETS.ROLE_AIRPORT_INSET
    assert shipped_registry["HRDEM"]["access_strategy"] == "stac"
    # The third inset provider family: national lidar over OGC WCS.
    for code in (
        "ENGLAND1M",
        "NORWAY1M",
        "SPAIN5M",
        "POLAND1M",
        "AUSTRALIA5M",
    ):
        assert shipped_registry[code]["role"] == INSETS.ROLE_AIRPORT_INSET
        assert shipped_registry[code]["access_strategy"] == "wcs"
    # The German Laender wave (2026-07-16): kilometre tile grids, two
    # more WCS endpoints, one KVP WCS, one drop folder.
    for code in (
        "BAVARIA1M",
        "NRW1M",
        "THURINGIA1M",
        "BRANDENBURG1M",
        "SAXONY1M",
        "BREMEN1M",
        "SCHLESWIGHOLSTEIN1M",
        "BADENWUERTTEMBERG1M",
        "RHINELANDPALATINATE1M",
    ):
        assert shipped_registry[code]["access_strategy"] == "tile_grid_http"
    assert shipped_registry["MECKLENBURG1M"]["access_strategy"] == "wcs"
    assert shipped_registry["AUSTRIA1M"]["access_strategy"] == "tile_grid_http"
    assert (
        shipped_registry["NETHERLANDS50CM"]["access_strategy"] == "wcs"
    )
    # Italy: two regional lidar services + the national 10 m fallback,
    # all pure wcs definitions.
    for code in ("SOUTHTYROL50CM", "SARDINIA1M", "ITALY10M"):
        assert shipped_registry[code]["access_strategy"] == "wcs"
    # Latin America (2026-07-16): Uruguay's national catalog, Espirito
    # Santo's grid blocks, Curitiba's image service, Pernambuco's
    # CAPTCHA-gated drop folder.
    assert (
        shipped_registry["URUGUAY2M"]["access_strategy"]
        == "geojson_tile_index"
    )
    assert (
        shipped_registry["ESPIRITOSANTO2M"]["access_strategy"]
        == "tile_grid_http"
    )
    assert shipped_registry["CURITIBA50CM"]["access_strategy"] == "wcs_kvp"
    assert (
        shipped_registry["PERNAMBUCO1M"]["access_strategy"]
        == "xyz_archive_drop"
    )
    assert (
        shipped_registry["RIODEJANEIRO5M"]["access_strategy"]
        == "arcgis_lerc_tiles"
    )
    # The ArcGIS hunt wave (2026-07-16).
    for code in ("HONGKONG5M", "ZAGREB1M", "ESTONIA1M", "SCOTLAND30M"):
        assert (
            shipped_registry[code]["access_strategy"] == "arcgis_lerc_tiles"
        )
    for code in ("CZECHIA2M", "LITHUANIA1M"):
        assert shipped_registry[code]["access_strategy"] == "wcs_kvp"
    # Northern Ireland ships DISABLED: its tile cache serves empty
    # stubs at every NI airport (data verification failed).
    assert shipped_registry["NORTHERNIRELAND1M"]["enabled"] is False
    # The non-ArcGIS-channels wave (2026-07-16).
    assert shipped_registry["FLANDERS1M"]["access_strategy"] == "wcs"
    assert (
        shipped_registry["IRELAND1M"]["access_strategy"]
        == "arcgis_feature_tiles"
    )
    assert (
        shipped_registry["WALLONIA1M"]["access_strategy"]
        == "xyz_archive_drop"
    )
    # The account-session wave (2026-07-16): Portugal's DGT collections
    # download automatically over the shared signed-in session (the 2 m
    # provider's drop folder is superseded), Denmark's national height
    # model needs a Datafordeler API key, Sweden's Lantmateriet pixels
    # need a Geotorget account as HTTP Basic authentication.
    for code in ("PORTUGAL50CM", "PORTUGAL2M"):
        assert (
            shipped_registry[code]["access_strategy"]
            == "authenticated_token_search"
        )
        assert shipped_registry[code]["session_name"] == "dgterritorio"
        assert shipped_registry[code]["registration_url"]
    assert shipped_registry["DENMARK40CM"]["access_strategy"] == "wcs"
    assert shipped_registry["DENMARK40CM"]["credential_kind"] == "api_key"
    assert "{api_key}" in shipped_registry["DENMARK40CM"]["wcs_service_url"]
    assert shipped_registry["DENMARK40CM"]["registration_url"]
    assert shipped_registry["SWEDEN1M"]["access_strategy"] == "stac"
    assert shipped_registry["SWEDEN1M"]["credential_kind"] == "http_basic"
    assert shipped_registry["SWEDEN1M"]["registration_url"]
    # Scotland's campaign lidar bucket (finest-wins overlap ordering).
    assert (
        shipped_registry["SCOTLAND50CM"]["access_strategy"]
        == "os_grid_bucket"
    )
    # The coverage box must reach Shetland: tile +59-002 (Sumburgh,
    # Fair Isle) regressed to the 90 m base when it stopped at 58.7.
    assert INSETS._coverage_bbox_intersects(
        shipped_registry["SCOTLAND50CM"], (-2.0, 59.0, -1.0, 60.0)
    )
    assert shipped_registry["SAXONYANHALT1M"]["access_strategy"] == "wcs"
    assert shipped_registry["HESSE1M"]["access_strategy"] == "wcs_kvp"
    # Saarland's open coverage has undocumented value semantics: OFF.
    assert shipped_registry["SAARLAND18M"]["enabled"] is False
    assert (
        shipped_registry["HAMBURG1M"]["access_strategy"] == "xyz_archive_drop"
    )
    # France's WFS-indexed LiDAR HD tiles.
    assert (
        shipped_registry["FRANCE50CM"]["access_strategy"] == "wfs_tile_index"
    )
    # Japan's slippy text tiles and Taiwan's manual XYZ archives.
    assert shipped_registry["JAPAN5M"]["access_strategy"] == "xyz_text_tiles"
    assert (
        shipped_registry["TAIWAN20M"]["access_strategy"] == "xyz_archive_drop"
    )
    # STAC search providers (swisstopo; Finland's Paituli mirror).
    for code in ("SWISSALTI3D", "FINLAND2M"):
        assert shipped_registry[code]["role"] == INSETS.ROLE_AIRPORT_INSET
        assert shipped_registry[code]["access_strategy"] == "stac"
    # Fixed-URL country-wide Cloud-Optimized GeoTIFF (Wales) and the
    # static catalog walker (New Zealand, LERC-compressed tiles).
    assert shipped_registry["WALES1M"]["access_strategy"] == "direct_cog"
    assert (
        shipped_registry["NEWZEALAND1M"]["access_strategy"] == "static_stac"
    )
    assert shipped_registry["NEWZEALAND1M"]["asset_compression"] == "lerc"
    assert shipped_registry["SRTM"]["enabled"] is False
    assert shipped_registry["ALOS"]["enabled"] is False
    assert shipped_registry["VIEWFINDER1"]["priority"] == 60.0
    assert shipped_registry["VIEWFINDER3"]["priority"] == 10.0
    assert shipped_registry["NED1"]["priority"] == 70.0
    assert shipped_registry["NED13"]["priority"] == 0.0
    assert shipped_registry["SONNY1"]["priority"] == 80.0
    assert shipped_registry["SONNY1"]["access_strategy"] == "hgt_archive_drop"


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
    with open(cache_path, "wb") as handle:
        handle.write(b"\x00\x01")  # non-empty: zero-byte is never recycled
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
    with open(cache_path, "wb") as handle:
        handle.write(b"\x00\x01")  # non-empty: zero-byte is never recycled
    assert DEM.ensure_elevation("SRTM", 36, -87) == 1


def test_shim_unknown_source_returns_zero():
    assert DEM.ensure_elevation("BOGUS", 36, -87) == 0


# =====================================================================
# The WCS inset providers (England / Norway national lidar)
# =====================================================================
def test_wcs_inset_definitions_cover_expected_airports(shipped_registry):
    england = shipped_registry["ENGLAND1M"]
    norway = shipped_registry["NORWAY1M"]
    heathrow = (-0.49, 51.44, -0.41, 51.49)
    gardermoen = (11.05, 60.17, 11.13, 60.22)
    doha = (51.55, 25.24, 51.65, 25.29)
    assert INSETS._coverage_bbox_intersects(england, heathrow)
    assert not INSETS._coverage_bbox_intersects(england, gardermoen)
    assert not INSETS._coverage_bbox_intersects(england, doha)
    assert INSETS._coverage_bbox_intersects(norway, gardermoen)
    assert not INSETS._coverage_bbox_intersects(norway, heathrow)
    assert not INSETS._coverage_bbox_intersects(norway, doha)


# =====================================================================
# Sonny LiDAR Europe (the hgt_archive_drop manual-drop strategy)
# =====================================================================
def _write_zip_with_member(zip_path, member_name, payload=b"\x00\x01"):
    import zipfile

    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(member_name, payload)


def test_sonny_covers_nothing_without_dropped_data(
    tmp_path, monkeypatch, shipped_registry
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    definition = shipped_registry["SONNY1"]
    strategy = _strategy_for(definition)
    # Inside the Europe box but nothing dropped: not an automatic
    # candidate, and the explicit path yields 0 with no network.
    assert strategy.covers(definition, 50, 8) is False
    assert DEM.ensure_elevation("SONNY1", 50, 8) == 0
    # Outside the Europe box: never a candidate.
    assert strategy.covers(definition, 36, -87) is False


def test_sonny_extracts_tile_from_dropped_zip(
    tmp_path, monkeypatch, shipped_registry
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    definition = shipped_registry["SONNY1"]
    strategy = _strategy_for(definition)
    drop_directory = strategy.drop_directory(definition)
    os.makedirs(drop_directory)
    # The archives carry their own inner folders and mixed case.
    _write_zip_with_member(
        os.path.join(drop_directory, "Germany_1s.zip"),
        "Germany/n50e008.HGT",
        b"sonny-bytes",
    )
    assert strategy.covers(definition, 50, 8) is True
    assert DEM.ensure_elevation("SONNY1", 50, 8) == 1
    cache_path = FNAMES.elevation_data("SONNY1", 50, 8)
    assert cache_path.endswith("N50E008_SONNY1.hgt")
    with open(cache_path, "rb") as cached:
        assert cached.read() == b"sonny-bytes"
    # A second call recycles the extracted cache (the archive is gone,
    # so a pass proves no re-extraction was needed).
    os.remove(os.path.join(drop_directory, "Germany_1s.zip"))
    assert DEM.ensure_elevation("SONNY1", 50, 8) == 1


def test_sonny_recycles_bare_hgt_from_drop_folder(
    tmp_path, monkeypatch, shipped_registry
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    definition = shipped_registry["SONNY1"]
    strategy = _strategy_for(definition)
    drop_directory = strategy.drop_directory(definition)
    os.makedirs(drop_directory)
    with open(os.path.join(drop_directory, "n47e011.hgt"), "wb") as dropped:
        dropped.write(b"alpine")
    assert strategy.covers(definition, 47, 11) is True
    assert DEM.ensure_elevation("SONNY1", 47, 11) == 1
    with open(FNAMES.elevation_data("SONNY1", 47, 11), "rb") as cached:
        assert cached.read() == b"alpine"


def test_sonny_auto_selection_requires_dropped_data(
    tmp_path, monkeypatch, shipped_registry
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    # Nothing dropped: the European tile ranks exactly as before Sonny.
    candidates = INSETS.select_base_definitions_auto(50, 8)
    assert [c["code"] for c in candidates] == ["VIEWFINDER3"]
    # Tile dropped: SONNY1 (priority 80) leads the ranking.
    definition = shipped_registry["SONNY1"]
    drop_directory = _strategy_for(definition).drop_directory(definition)
    os.makedirs(drop_directory)
    _write_zip_with_member(
        os.path.join(drop_directory, "Germany_1s.zip"), "N50E008.hgt"
    )
    candidates = INSETS.select_base_definitions_auto(50, 8)
    assert candidates[0]["code"] == "SONNY1"


def test_sonny_default_source_long_name(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(DEM, "base_elevation_source", "SONNY1")
    assert DEM.resolve_default_base_source(50, 8) == (
        'Sonny LiDAR 1" (manual download from sonny.4lima.de) - Europe'
    )


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


# =====================================================================
# The per-tile offline elevation summary (GUI tile-info surface)
# =====================================================================
def test_summary_reports_base_and_inset_availability(
    tmp_path, monkeypatch, shipped_registry
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    # London tile: Viewfinder 3 arc-second base, England lidar reachable.
    summary = INSETS.summarize_tile_elevation_sources(51, -1)
    assert summary["base_code"] == "VIEWFINDER3"
    assert summary["base_resolution_arc_seconds"] == 3.0
    assert summary["base_is_fallback"] is False
    assert ("ENGLAND1M", 1.0) in summary["inset_providers"]
    assert summary["fetched_airports"] is None
    # Doha tile: no national source reaches the Middle East, so the
    # only inset provider is the global surface-model fallback.
    summary = INSETS.summarize_tile_elevation_sources(25, 51)
    assert summary["inset_providers"] == [("COPERNICUSGLO30", 30.0)]
    # Zurich tile: the swisstopo 0.5 m lidar.
    summary = INSETS.summarize_tile_elevation_sources(47, 8)
    assert ("SWISSALTI3D", 0.5) in summary["inset_providers"]
    # United States tile: NED1 base picked automatically, 3DEP insets.
    summary = INSETS.summarize_tile_elevation_sources(36, -87)
    assert summary["base_code"] == "NED1"
    assert ("USGS3DEP", 1.0) in summary["inset_providers"]


def test_summary_unresolvable_selector_reports_fallback(
    tmp_path, monkeypatch, shipped_registry
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    summary = INSETS.summarize_tile_elevation_sources(
        51, -1, base_selector="BOGUS"
    )
    assert summary["base_code"] == "VIEWFINDER3"
    assert summary["base_is_fallback"] is True


def test_summary_reads_cached_inset_index_ground_truth(
    tmp_path, monkeypatch, shipped_registry
):
    import json

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    index_path = FNAMES.airport_inset_index(51, -1)
    os.makedirs(os.path.dirname(index_path))
    with open(index_path, "w") as handle:
        json.dump(
            {
                "EGLL": {
                    "ENGLAND1M": "ok",
                    "checked": "2026-07-15",
                    "probes": [[51.47, -0.46]],
                },
                "EGXX": {"ENGLAND1M": "no-coverage"},
                "EGYY": {"ENGLAND1M": "ok"},
            },
            handle,
        )
    summary = INSETS.summarize_tile_elevation_sources(51, -1)
    assert summary["fetched_airports"] == 2
    assert summary["no_coverage_airports"] == 1


def test_tiles_with_inset_coverage_subset(shipped_registry):
    # The global COPERNICUSGLO30 fallback reaches every tile, so the
    # coverage question is now "which tiles" only in the degenerate
    # sense; the Doha tile (25, 51) is the one this feature added.
    tiles = [(51, -1), (25, 51), (47, 8), (60, 10)]
    covered = INSETS.tiles_with_inset_coverage(tiles)
    assert covered == [(51, -1), (25, 51), (47, 8), (60, 10)]


# =====================================================================
# Corrupt-archive containment (upstream CRC damage, dem1/P32.zip case)
# =====================================================================
def _viewfinder_zip_with_corrupt_member():
    """A dem1-style archive: N60E006.hgt good, N60E007.hgt bad CRC.

    Built with ZIP_STORED so the payload bytes appear verbatim in the
    buffer; rewriting them afterwards breaks the stored CRC exactly the
    way the damaged upstream archive does.
    """
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("P32/N60E006.hgt", b"GOODDATA" * 64)
        archive.writestr("P32/N60E007.hgt", b"BADDATA!" * 64)
    raw = buffer.getvalue().replace(b"BADDATA!", b"XADDATA!")
    return raw


def test_corrupt_archive_member_is_contained(tmp_path, monkeypatch):
    import types

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    zip_bytes = _viewfinder_zip_with_corrupt_member()
    monkeypatch.setattr(
        DEM,
        "http_request",
        lambda url, source, verbose=False: types.SimpleNamespace(
            content=zip_bytes
        ),
    )
    # Requesting the GOOD tile: succeeds, extracts it, and the corrupt
    # neighbour member is skipped without an exception and WITHOUT
    # leaving a zero-byte cache file behind.
    assert DEM.ensure_elevation("View", 60, 6) == 1
    good_path = FNAMES.viewfinderpanorama(60, 6)
    with open(good_path, "rb") as handle:
        assert handle.read() == b"GOODDATA" * 64
    assert not os.path.exists(FNAMES.viewfinderpanorama(60, 7))
    assert not os.path.exists(FNAMES.viewfinderpanorama(60, 7) + ".part")
    # Requesting the CORRUPT tile itself: the download happens, the
    # member cannot be extracted, and the source reports failure (0)
    # instead of crashing or poisoning the cache.
    assert DEM.ensure_elevation("View", 60, 7) == 0
    assert not os.path.exists(FNAMES.viewfinderpanorama(60, 7))


def test_zero_byte_cache_file_is_not_recycled(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    # A poisoned zero-byte cache entry (the pre-fix failure mode) must
    # not be recycled: the source re-attempts and, with the download
    # failing here, honestly reports 0 instead of a zero-altitude tile.
    poisoned = FNAMES.elevation_data("NED1", 36, -87)
    os.makedirs(os.path.dirname(poisoned), exist_ok=True)
    open(poisoned, "wb").close()
    monkeypatch.setattr(
        DEM, "http_request", lambda url, source, verbose=False: None
    )
    assert DEM.ensure_elevation("NED1", 36, -87) == 0
    # The same guard heals the manual drop strategy's covers() test.
    sonny = INSETS.elevation_providers_dict["SONNY1"]
    strategy = INSETS.ACCESS_STRATEGIES["hgt_archive_drop"]()
    zero_byte_cache = FNAMES.elevation_data("SONNY1", 50, 8)
    os.makedirs(os.path.dirname(zero_byte_cache), exist_ok=True)
    open(zero_byte_cache, "wb").close()
    assert strategy.covers(sonny, 50, 8) is False


# =====================================================================
# Manual-setup model query (the GUI info-button affordance)
# =====================================================================
def test_manual_setup_entries_per_region(tmp_path, monkeypatch, shipped_registry):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    # Icelandic tile (in Sonny's coverage, no automatic inset
    # provider): the drop-folder source applies (base role).
    entries = INSETS.manual_elevation_setup_for_tile(64, -20)
    codes = [entry["code"] for entry in entries]
    assert codes == ["SONNY1"]
    entry = entries[0]
    assert entry["role"] == INSETS.ROLE_BASE
    assert entry["download_page"] == "https://sonny.4lima.de"
    assert entry["drop_directory"].endswith("Sonny_LiDAR_Europe")
    assert entry["already_dropped"] is False
    assert len(entry["steps"]) == 3
    # Taiwanese tile: the Ministry of the Interior archives apply.
    entries = INSETS.manual_elevation_setup_for_tile(25, 121)
    assert [entry["code"] for entry in entries] == ["TAIWAN20M"]
    # United States tile: everything is automatic, no affordance.
    assert INSETS.manual_elevation_setup_for_tile(36, -87) == []


def test_manual_setup_suppressed_by_finer_automatic_source(
    tmp_path, monkeypatch, shipped_registry
):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    # Norway: the 1 m Kartverket insets download automatically, so the
    # 30 m Sonny drop folder is NOT an improvement (user ruling).
    assert INSETS.manual_elevation_setup_for_tile(60, 10) == []
    # England (1 m WCS), Switzerland (0.5 m STAC), Spain (5 m WCS):
    # all suppressed the same way.
    assert INSETS.manual_elevation_setup_for_tile(51, -1) == []
    assert INSETS.manual_elevation_setup_for_tile(46, 8) == []
    assert INSETS.manual_elevation_setup_for_tile(40, -4) == []
    # The Alps 1 arc-second Viewfinderpanoramas zone equals Sonny's
    # resolution -- equivalent is not better, so still suppressed.
    assert INSETS.manual_elevation_setup_for_tile(46, 7) == []
    # Iceland: best automatic is the 90 m worldwide base -- Sonny's
    # 1 arc-second lidar genuinely improves it, so it IS offered.
    # Germany, France and Italy are now automatically covered (the
    # Laender, LiDAR HD and TINITALY providers), so the suggestion is
    # gone there.
    assert [
        entry["code"]
        for entry in INSETS.manual_elevation_setup_for_tile(64, -20)
    ] == ["SONNY1"]
    assert INSETS.manual_elevation_setup_for_tile(43, 12) == []
    assert INSETS.manual_elevation_setup_for_tile(47, 2) == []
    assert INSETS.manual_elevation_setup_for_tile(51, 10) == []


def test_manual_setup_reports_dropped_state(tmp_path, monkeypatch, shipped_registry):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    drop_directory = os.path.join(str(tmp_path), "Sonny_LiDAR_Europe")
    os.makedirs(drop_directory)
    # Empty folder still counts as not set up.
    assert (
        INSETS.manual_elevation_setup_for_tile(64, -20)[0]["already_dropped"]
        is False
    )
    with open(os.path.join(drop_directory, "Germany_1s.zip"), "wb") as f:
        f.write(b"x")
    assert (
        INSETS.manual_elevation_setup_for_tile(64, -20)[0]["already_dropped"]
        is True
    )
