"""Tests for the bathymetry band's auto-mode airport-radius gate.

Ruling 2026-07-16 (after tile +37-009's PORTUGALTIDAL first fetch ran
multi-hour for shoreline nobody approaches low): ``masks_use_DEM_too=
"auto"`` only fetches measured depth for shoreline cells within
``bathymetry_airport_radius_km`` (default 20, 0 = whole shoreline) of an
apt.dat anchor whose type checkbox is enabled
(``bathymetry_near_icao_airports`` / ``_other_airports`` /
``_seaplane_bases`` / ``_heliports`` — heliports off by default);
explicit ``True`` keeps the whole shoreline band.
Outside the ring the masks keep the distance fade plus the mapped
OpenStreetMap shallow-water fallback, which now loads alongside a gated
(partial) band and fills exactly the squares the band left bare.

Covers (spec ``docs/specs/coastal-bathymetry-spec.md`` sections 3, 4.4):

  * ``_filter_cells_to_airport_reach`` — keeps cells within the radius
    (plus the cell half-diagonal slack), disengages at radius 0 and when
    the offline airport index is unavailable (conservative full band);
  * ``_airports_near_tile`` — index-cache loading, the
    radius-expanded bounding box (neighbour-tile airports count), and
    the unavailable variants (missing file, empty index) returning
    ``None`` — never mistaken for "no airports";
  * end-to-end ``ensure_bathymetry_band`` — auto mode fetches only the
    gated cells, ``True`` fetches the full shoreline, and a tile with no
    airport in reach skips the band entirely (zero network calls);
  * the masks step's shallow-water fallback loading condition — engaged
    alongside a gated band, never for an ungated or ``True`` band.

All headless: ``tmp_path`` for every file, the cell download
monkeypatched (no network), synthetic airport index / provider registry
/ coastline geometry.  The end-to-end fetch tests skip cleanly when the
GDAL python bindings are absent.
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
import O4_Geo_Utils as GEO
import O4_UI_Utils as UI

PROVIDER_CODE = "FAKEBATHY"

# Same synthetic shoreline as test_bathymetry_band_fetch: a segment along
# longitude 0.15 degrees selecting exactly the four cells (column 1,
# rows 0..3) of the +00+000 tile when ``bathymetry_band_km=0.1``.
FOUR_CELL_COASTLINE = MultiLineString([[(0.15, 0.05), (0.15, 0.35)]])
FOUR_CELLS = [(1, 0), (1, 1), (1, 2), (1, 3)]

METRES_PER_DEGREE_LONGITUDE = GEO.lon_to_m(0.5)
METRES_PER_DEGREE_LATITUDE = GEO.lat_to_m
CELL_HALF_DIAGONAL_M = 0.5 * (
    (BATHYBAND.BATHYMETRY_CELL_DEGREES * METRES_PER_DEGREE_LONGITUDE) ** 2
    + (BATHYBAND.BATHYMETRY_CELL_DEGREES * METRES_PER_DEGREE_LATITUDE) ** 2
) ** 0.5

# The centre of cell (1, 0): one airport there gates with zero distance.
CELL_1_0_CENTRE = (0.05, 0.15)  # (lat, lon)


def _tile(**overrides):
    attributes = dict(
        lat=0,
        lon=0,
        bathymetry_band_km=0.1,
        bathymetry_airport_radius_km=1.0,
    )
    attributes.update(overrides)
    return SimpleNamespace(**attributes)


def _band_definition():
    return {
        "code": PROVIDER_CODE,
        "role": "bathymetry",
        "enabled": True,
        "priority": 100.0,
        "native_resolution_m": 3.0,
    }


def _write_airport_index(path, airports):
    """A synthetic O4_Airport_Index v3 TSV cache.

    ``airports``: iterable of ``(code, lat, lon)`` or
    ``(code, lat, lon, category)`` (default category "icao_airport").
    """
    rows = [
        (entry + ("icao_airport",))[:4] for entry in airports
    ]
    lines = ["O4AIRPORTIDX 3 %d" % len(rows)]
    lines.append("#SRC 0 0 /nonexistent/apt.dat")
    for (code, lat, lon, category) in rows:
        lines.append(
            "\t".join((code, code + " field", "", "",
                       repr(lat), repr(lon), category))
        )
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


ALL_ANCHOR_CATEGORIES = frozenset(
    category for (_, category, _) in BATHYBAND.ANCHOR_CATEGORY_SETTINGS
)


@pytest.fixture(autouse=True)
def _gate_environment(monkeypatch, tmp_path):
    """Isolated band cache + airport index path + synthetic registry."""
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    index_path = str(tmp_path / "airport_index.tsv")
    monkeypatch.setattr(
        FNAMES, "airport_index_cache", lambda: index_path
    )
    monkeypatch.setattr(
        BATHYBAND,
        "_airport_index_state",
        {"cache_key": None, "positions": None},
    )
    monkeypatch.setattr(
        INSETS,
        "select_bathymetry_definitions",
        lambda lat, lon: [_band_definition()],
    )
    monkeypatch.setattr(
        BATHYBAND, "_band_geometry", lambda tile: (FOUR_CELL_COASTLINE, None)
    )
    monkeypatch.setattr(UI, "red_flag", False)
    BATHYBAND._prefetch_futures.clear()
    BATHYBAND._foreground_wait.clear()
    yield index_path
    UI.red_flag = False
    BATHYBAND._prefetch_futures.clear()
    BATHYBAND._foreground_wait.clear()


def _filter(tile, anchor_categories=ALL_ANCHOR_CATEGORIES):
    return BATHYBAND._filter_cells_to_airport_reach(
        tile,
        anchor_categories,
        list(FOUR_CELLS),
        METRES_PER_DEGREE_LONGITUDE,
        METRES_PER_DEGREE_LATITUDE,
        CELL_HALF_DIAGONAL_M,
    )


# =====================================================================
# 1. _filter_cells_to_airport_reach
# =====================================================================
def test_gate_keeps_only_cells_near_airport(_gate_environment):
    """One airport at cell (1,0)'s centre with a 1 km radius: the reach
    (1 km + the ~7.9 km cell half-diagonal) keeps that cell alone — the
    next cell centre is ~11.1 km away."""
    _write_airport_index(
        _gate_environment, [("XAAA",) + CELL_1_0_CENTRE]
    )

    (kept, dropped_count, gate_engaged) = _filter(_tile())

    assert gate_engaged
    assert kept == [(1, 0)]
    assert dropped_count == 3


def test_gate_radius_zero_disengages(_gate_environment):
    """Radius 0 is the explicit whole-shoreline choice: no gating, and
    the airport index is not even consulted (none written here)."""
    (kept, dropped_count, gate_engaged) = _filter(
        _tile(bathymetry_airport_radius_km=0.0)
    )

    assert not gate_engaged
    assert kept == FOUR_CELLS
    assert dropped_count == 0


def test_gate_disengages_without_airport_index(_gate_environment):
    """No index cache on disk: the gate cannot be evaluated and must
    disengage (full band, today's behaviour) — never drop everything."""
    assert not os.path.isfile(_gate_environment)

    (kept, dropped_count, gate_engaged) = _filter(_tile())

    assert not gate_engaged
    assert kept == FOUR_CELLS
    assert dropped_count == 0


def test_gate_disengages_on_empty_airport_index(_gate_environment):
    """A header-only (zero airports) index means a failed/foreign build,
    not an airport-free planet: treated as unavailable."""
    _write_airport_index(_gate_environment, [])

    (kept, dropped_count, gate_engaged) = _filter(_tile())

    assert not gate_engaged
    assert kept == FOUR_CELLS


def test_gate_drops_all_cells_when_no_airport_in_reach(_gate_environment):
    """A healthy index whose airports are all far away gates everything
    out (the band is then skipped by the caller)."""
    _write_airport_index(_gate_environment, [("XFAR", 5.0, 5.0)])

    (kept, dropped_count, gate_engaged) = _filter(_tile())

    assert gate_engaged
    assert kept == []
    assert dropped_count == len(FOUR_CELLS)


# =====================================================================
# 2. _airports_near_tile
# =====================================================================
def test_airports_near_tile_includes_neighbour_tile_airports(
    _gate_environment,
):
    """An airport just SOUTH of the tile (lat -0.05) sits inside the
    radius-expanded bounding box and must be returned — coastal airports
    across a tile border still gate this tile's cells."""
    _write_airport_index(
        _gate_environment,
        [("XSOU", -0.05, 0.15), ("XFAR", -3.0, 0.15)],
    )

    airports = BATHYBAND._airports_near_tile(
        0, 0, 20.0, ALL_ANCHOR_CATEGORIES
    )

    assert airports == [(-0.05, 0.15)]


def test_airports_near_tile_reloads_on_cache_change(_gate_environment):
    """The module-level index cache invalidates when the TSV changes
    (the map window may rebuild it mid-session)."""
    _write_airport_index(_gate_environment, [("XAAA",) + CELL_1_0_CENTRE])
    first = BATHYBAND._airports_near_tile(
        0, 0, 20.0, ALL_ANCHOR_CATEGORIES
    )
    _write_airport_index(
        _gate_environment,
        [("XAAA",) + CELL_1_0_CENTRE, ("XBBB", 0.35, 0.15)],
    )
    # A same-mtime rewrite is only distinguished by size; both differ
    # here (one extra row).
    second = BATHYBAND._airports_near_tile(
        0, 0, 20.0, ALL_ANCHOR_CATEGORIES
    )

    assert first == [CELL_1_0_CENTRE]
    assert sorted(second) == sorted([CELL_1_0_CENTRE, (0.35, 0.15)])


def test_airports_near_tile_filters_by_category(_gate_environment):
    """Only anchors of enabled categories are returned: a heliport at
    the same spot as an ICAO field must not gate when heliports are
    unchecked."""
    _write_airport_index(
        _gate_environment,
        [
            ("XICA", 0.05, 0.15, "icao_airport"),
            ("XHEL", 0.35, 0.15, "heliport"),
            ("XSEA", 0.55, 0.15, "seaplane_base"),
        ],
    )

    icao_only = BATHYBAND._airports_near_tile(
        0, 0, 20.0, frozenset({"icao_airport"})
    )
    with_heliports = BATHYBAND._airports_near_tile(
        0, 0, 20.0, frozenset({"icao_airport", "heliport"})
    )

    assert icao_only == [(0.05, 0.15)]
    assert sorted(with_heliports) == [(0.05, 0.15), (0.35, 0.15)]


# =====================================================================
# 3. Anchor checkbox resolution
# =====================================================================
def test_enabled_anchor_categories_defaults():
    """Defaults: ICAO airports, small airfields and seaplane bases ON,
    heliports OFF (they dilute the gate the most for the least gain)."""
    categories = BATHYBAND._enabled_anchor_categories(SimpleNamespace())

    assert categories == frozenset(
        {"icao_airport", "airport", "seaplane_base"}
    )


def test_enabled_anchor_categories_reads_tile_attributes():
    tile = SimpleNamespace(
        bathymetry_near_icao_airports=False,
        bathymetry_near_other_airports="False",
        bathymetry_near_seaplane_bases=True,
        bathymetry_near_heliports="True",
    )

    categories = BATHYBAND._enabled_anchor_categories(tile)

    assert categories == frozenset({"seaplane_base", "heliport"})


# =====================================================================
# 4. End-to-end through ensure_bathymetry_band (needs GDAL for the VRT)
# =====================================================================
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
    values = numpy.full((rows, columns), -5.0, dtype=numpy.float32)
    band.WriteArray(values)
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


@pytest.mark.skipif(not HAS_GDAL, reason="osgeo not available")
def test_auto_mode_fetches_only_gated_cells(_gate_environment, monkeypatch):
    _write_airport_index(_gate_environment, [("XAAA",) + CELL_1_0_CENTRE])
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    band_vrt = BATHYBAND.ensure_bathymetry_band(
        _tile(), fine_nearshore_only=True
    )

    assert band_vrt is not None and os.path.isfile(band_vrt)
    assert len(fetch_calls) == 1
    assert os.path.isfile(
        FNAMES.bathymetry_band_cell(
            0, 0, 1, 0, PROVIDER_CODE,
            BATHYBAND.BATHYMETRY_CELL_RESOLUTION_M,
        )
    )


@pytest.mark.skipif(not HAS_GDAL, reason="osgeo not available")
def test_true_mode_fetches_full_shoreline(_gate_environment, monkeypatch):
    """Explicit True (fine_nearshore_only=False) ignores the gate even
    with a gating-friendly index present."""
    _write_airport_index(_gate_environment, [("XAAA",) + CELL_1_0_CENTRE])
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    band_vrt = BATHYBAND.ensure_bathymetry_band(
        _tile(), fine_nearshore_only=False
    )

    assert band_vrt is not None
    assert len(fetch_calls) == len(FOUR_CELLS)


@pytest.mark.skipif(not HAS_GDAL, reason="osgeo not available")
def test_auto_mode_skips_band_when_no_airport_in_reach(
    _gate_environment, monkeypatch
):
    """No airport within the radius: the band is skipped outright — not
    one network call is made."""
    _write_airport_index(_gate_environment, [("XFAR", 5.0, 5.0)])
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    band_vrt = BATHYBAND.ensure_bathymetry_band(
        _tile(), fine_nearshore_only=True
    )

    assert band_vrt is None
    assert fetch_calls == []


@pytest.mark.skipif(not HAS_GDAL, reason="osgeo not available")
def test_auto_mode_skips_band_when_every_anchor_unchecked(
    _gate_environment, monkeypatch
):
    """All four anchor checkboxes off = "no measured bathymetry, thank
    you": the band is skipped before the airport index is even read."""
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    band_vrt = BATHYBAND.ensure_bathymetry_band(
        _tile(
            bathymetry_near_icao_airports=False,
            bathymetry_near_other_airports=False,
            bathymetry_near_seaplane_bases=False,
            bathymetry_near_heliports=False,
        ),
        fine_nearshore_only=True,
    )

    assert band_vrt is None
    assert fetch_calls == []


@pytest.mark.skipif(not HAS_GDAL, reason="osgeo not available")
def test_auto_mode_anchor_checkboxes_select_cells(
    _gate_environment, monkeypatch
):
    """A heliport is the only anchor near the shoreline: with heliports
    at their default (off) nothing fetches; checked, its cell does."""
    _write_airport_index(
        _gate_environment,
        [("XHEL",) + CELL_1_0_CENTRE + ("heliport",)],
    )
    fetch_calls = []
    _install_fake_fetch(monkeypatch, fetch_calls)

    default_band = BATHYBAND.ensure_bathymetry_band(
        _tile(), fine_nearshore_only=True
    )
    assert default_band is None
    assert fetch_calls == []

    heliport_band = BATHYBAND.ensure_bathymetry_band(
        _tile(bathymetry_near_heliports=True), fine_nearshore_only=True
    )
    assert heliport_band is not None
    assert len(fetch_calls) == 1
