"""Tests for the ``coordinate_named_url_list`` elevation access strategy.

The strategy (``CoordinateNamedUrlListStrategy`` in
``src/O4_Airport_Elevation_Insets.py``) indexes a NOAA NCEI CUDEM region
that publishes NO STAC catalog -- only a plain-text list of Cloud-Optimized
GeoTIFF URLs whose filenames encode the tile location.  It fetches that list
once, parses every filename into a bounding box, memoises the index under
``Elevation_data/``, and serves windowed reads of the intersecting tiles.

All headless: the URL-list download is monkeypatched (no test ever hits the
network) and ``tmp_path`` receives the memoised index.  The filename-parser
truth table is hard-coded from the live NCEI STAC oracle validation recorded
in the strategy docstring:

  * ``ncei19_n22x00_w159x50`` (Hawaii)  -> bbox north 22.00, west -159.50
  * ``ncei19_s14x25_w169x75`` (Am.Sam.) -> bbox north -14.25 (south hemi)
  * ``ncei19_n13x25_e144x50`` (Guam)    -> bbox west +144.50 (east hemi)
  * ``ncei19_n25X75_w080X25`` (Florida) -> uppercase ``X`` separator variant
"""

import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

import O4_Airport_Elevation_Insets as INSETS
import O4_File_Names as FNAMES

_HERE = os.path.dirname(os.path.abspath(__file__))
SHIPPED_PROVIDERS_DIRECTORY = os.path.normpath(
    os.path.join(_HERE, "..", "Providers", "Elevation")
)

STRATEGY = INSETS.CoordinateNamedUrlListStrategy

# A tiny CUDEM URL list: four coordinate-named tiles plus the kind of
# non-tile sidecar lines the real lists carry (a shapefile, a metadata
# archive, and the list file itself) -- all of which must be skipped.
_BASE = "https://example.test/dem/NCEI_ninth_Topobathy_2014_8483"
CANNED_URL_LIST = "\n".join(
    [
        _BASE + "/southeast/ncei19_n25x75_w080x25_2016v1.tif",
        _BASE + "/southeast/ncei19_n25x75_w080x50_2016v1.tif",
        _BASE + "/southeast/ncei19_n26x00_w080x25_2016v1.tif",
        _BASE + "/northeast/ncei19_n40x75_w074x00_2015v1.tif",
        _BASE + "/southeast_topobathy_19.shp",
        _BASE + "/ninth_spatial_meta.zip",
        _BASE + "/urllist8483.txt",
        "",
    ]
)


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


def _install_fake_url_list(monkeypatch, tmp_path, text=CANNED_URL_LIST):
    """Point the index at tmp_path and count URL-list downloads."""
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    import requests

    counter = {"calls": 0}

    def _fake_get(url, timeout=None):
        counter["calls"] += 1
        return _FakeResponse(text)

    monkeypatch.setattr(requests, "get", _fake_get)
    return counter


def _definition(code="CUDEMCONUS"):
    return {
        "code": code,
        "access_strategy": "coordinate_named_url_list",
        "role": INSETS.ROLE_BATHYMETRY,
        "enabled": True,
        "priority": 100.0,
        "native_resolution_m": 3.4,
        "coverage_bbox": (-126.0, 23.0, -64.0, 50.0),
        "value_floor_m": "-11100.0",
        "url_list_url": _BASE + "/urllist8483.txt",
    }


# =====================================================================
# Filename parser truth table (hard-coded from the live oracle)
# =====================================================================
@pytest.mark.parametrize(
    "filename, expected",
    [
        # Northern + western hemisphere (Hawaii oracle).
        (
            "ncei19_n22x00_w159x50_2021v1.tif",
            [-159.50, 21.75, -159.25, 22.00],
        ),
        # Southern + western hemisphere (American Samoa oracle): the
        # latitude token gives the NORTH edge at -14.25, not the south.
        (
            "ncei19_s14x25_w169x75_2021v1.tif",
            [-169.75, -14.50, -169.50, -14.25],
        ),
        # Northern + eastern hemisphere (Guam oracle): the longitude token
        # gives the WEST edge at +144.50.
        (
            "ncei19_n13x25_e144x50_2022v1.tif",
            [144.50, 13.00, 144.75, 13.25],
        ),
        # The uppercase-X separator variant (2018 Florida campaign).
        (
            "ncei19_n25X75_w080X25_2018v1.tif",
            [-80.25, 25.50, -80.00, 25.75],
        ),
    ],
)
def test_filename_parser_truth_table(filename, expected):
    box = STRATEGY.parse_filename_bbox(filename)
    assert box is not None
    assert box == pytest.approx(expected, abs=1e-9)


def test_filename_parser_rejects_non_tile_lines():
    for filename in (
        "southeast_topobathy_19.shp",
        "ninth_spatial_meta.zip",
        "urllist8483.txt",
        "catalog.json",
        "ncei19_n25_w080_2018v1.tif",  # no hundredths token
    ):
        assert STRATEGY.parse_filename_bbox(filename) is None


# =====================================================================
# discover(): index build, intersection, memoisation, skip
# =====================================================================
def test_discover_indexes_and_matches(monkeypatch, tmp_path):
    counter = _install_fake_url_list(monkeypatch, tmp_path)
    definition = _definition()
    # Miami box: overlaps the three south-east Florida tiles (the two
    # n25x75 tiles and the n26x00 tile whose 25.75-26.00 band the box's
    # top edge reaches) but not the distant north-east n40 tile.
    sources = STRATEGY().discover(definition, (-80.30, 25.70, -80.20, 25.80))
    assert sources is not None
    stems = sorted(
        entry["href"].rsplit("/", 1)[-1].replace(".tif", "")
        for entry in sources
    )
    assert stems == [
        "ncei19_n25x75_w080x25_2016v1",
        "ncei19_n25x75_w080x50_2016v1",
        "ncei19_n26x00_w080x25_2016v1",
    ]
    assert counter["calls"] == 1


def test_index_is_memoised_zero_second_fetch(monkeypatch, tmp_path):
    counter = _install_fake_url_list(monkeypatch, tmp_path)
    definition = _definition()
    box = (-80.30, 25.70, -80.20, 25.80)
    first = STRATEGY().discover(definition, box)
    assert first is not None
    assert counter["calls"] == 1
    # A fresh strategy instance reads the saved index -- zero more HTTP.
    second = STRATEGY().discover(definition, box)
    assert second is not None
    assert counter["calls"] == 1
    assert [entry["href"] for entry in first] == [
        entry["href"] for entry in second
    ]


def test_discover_none_off_coverage(monkeypatch, tmp_path):
    _install_fake_url_list(monkeypatch, tmp_path)
    definition = _definition()
    # A tile over France -- outside coverage_bbox: the cheap pre-filter
    # returns None before any download.
    assert STRATEGY().discover(definition, (2.0, 48.0, 2.1, 48.1)) is None


def test_discover_none_covered_but_no_tile(monkeypatch, tmp_path):
    _install_fake_url_list(monkeypatch, tmp_path)
    definition = _definition()
    # Inside coverage_bbox (a Gulf-coast box) but no indexed tile overlaps.
    assert STRATEGY().discover(definition, (-95.0, 29.0, -94.9, 29.1)) is None


def test_unparseable_lines_are_skipped(monkeypatch, tmp_path):
    _install_fake_url_list(monkeypatch, tmp_path)
    definition = _definition()
    STRATEGY().discover(definition, (-80.30, 25.70, -80.20, 25.80))
    import json

    with open(STRATEGY().index_path(definition)) as handle:
        index = json.load(handle)
    # Only the four coordinate-named .tif lines survive; the shapefile,
    # the archive and the list file itself are skipped.
    assert len(index["entries"]) == 4
    for entry in index["entries"]:
        assert entry["href"].endswith(".tif")


# =====================================================================
# Provider registration: the three shipped .elv files parse (role filter)
# =====================================================================
def test_cudem_url_list_providers_registered():
    registry = INSETS.initialize_elevation_providers_dict(
        SHIPPED_PROVIDERS_DIRECTORY
    )
    for code in ("CUDEMCONUS", "CUDEMCONUSTHIRD", "CUDEMGUAM"):
        assert code in registry, code
        definition = registry[code]
        assert definition["role"] == INSETS.ROLE_BATHYMETRY, code
        assert (
            definition["access_strategy"] == "coordinate_named_url_list"
        ), code
        assert definition["url_list_url"].startswith("http"), code
        assert float(definition["value_floor_m"]) == -11100.0, code
        # coverage_bbox is normalised to a 4-tuple by the parser.
        assert len(definition["coverage_bbox"]) == 4, code
    # Priority ordering: the ninth arc-second CONUS release outranks the
    # third arc-second gap filler beneath it.
    assert (
        registry["CUDEMCONUS"]["priority"]
        > registry["CUDEMCONUSTHIRD"]["priority"]
    )
    assert registry["CUDEMCONUS"]["access_strategy"] in INSETS.ACCESS_STRATEGIES
