"""Unit tests for :mod:`O4_Airport_Fade_Masks` (texture-mode feature, work
package 3 -- ``docs/specs/texture-mode-spec.md`` section 4.3).

All headless: no network, no X-Plane install.  :class:`AirportOrthoGeometry`
is built directly from a small square polygon (the acceptance criteria's
"construct directly from a small square polygon" path); one thinner test
exercises the ``build_airport_ortho_geometry`` builder wiring against a stub
``.apt`` pickle.
"""
import os
import pickle

import numpy
import pytest
from PIL import Image
from shapely.geometry import MultiPolygon, Polygon

import O4_Airport_Fade_Masks as FADE
import O4_File_Names as FNAMES
import O4_Geo_Utils as GEO


# ── module-resolves-to-worktree guard ───────────────────────────────────

def test_module_resolves_to_worktree():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.abspath(FADE.__file__).startswith(here), (
        f"O4_Airport_Fade_Masks imported from {FADE.__file__!r}, expected "
        f"under the worktree {here!r}")


# ── fade geometry: alpha_at / covers ────────────────────────────────────

def _unit_square_geometry(fade_width=1000.0):
    """A 0.01deg square at ref_lat 0 (so 1deg == 111319 m in both axes),
    tile origin at (0, 0) -- absolute == tile-local for easy arithmetic."""
    square = MultiPolygon(
        [Polygon([(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01)])])
    return FADE.AirportOrthoGeometry(
        square, fade_width, tile_lon=0.0, tile_lat=0.0, ref_lat=0.0)


def test_alpha_inside_is_one():
    geometry = _unit_square_geometry()
    assert geometry.alpha_at(0.005, 0.005) == pytest.approx(1.0)
    # On the boundary edge counts as inside (distance 0).
    assert geometry.alpha_at(0.01, 0.005) == pytest.approx(1.0)


def test_alpha_midpoint_of_fade_band():
    geometry = _unit_square_geometry(fade_width=1000.0)
    # 500 m due east of the eastern edge (lon = 0.01) -> half faded.
    lon = 0.01 + 500.0 / GEO.lon_to_m(0.0)
    assert geometry.alpha_at(lon, 0.005) == pytest.approx(0.5, abs=1e-3)


def test_alpha_beyond_fade_band_is_zero():
    geometry = _unit_square_geometry(fade_width=1000.0)
    lon = 0.01 + 1500.0 / GEO.lon_to_m(0.0)
    assert geometry.alpha_at(lon, 0.005) == pytest.approx(0.0)


def test_covers_edge_behavior():
    geometry = _unit_square_geometry(fade_width=1000.0)
    # Well inside -> covered.
    assert geometry.covers(0.005, 0.005) is True
    # Within the fade band (500 m out) -> covered.
    lon_in = 0.01 + 500.0 / GEO.lon_to_m(0.0)
    assert geometry.covers(lon_in, 0.005) is True
    # Beyond boundary + fade width -> not covered.
    lon_out = 0.01 + 1500.0 / GEO.lon_to_m(0.0)
    assert geometry.covers(lon_out, 0.005) is False


def test_zero_fade_width_is_a_step():
    geometry = _unit_square_geometry(fade_width=0.0)
    assert geometry.alpha_at(0.005, 0.005) == pytest.approx(1.0)
    lon = 0.01 + 1.0 / GEO.lon_to_m(0.0)  # 1 m outside
    assert geometry.alpha_at(lon, 0.005) == pytest.approx(0.0)
    assert geometry.covers(lon, 0.005) is False


def test_empty_geometry_covers_nothing():
    geometry = FADE.AirportOrthoGeometry(None, 1000.0)
    assert geometry.is_empty() is True
    assert geometry.covers(0.5, 0.5) is False
    assert geometry.alpha_at(0.5, 0.5) == 0.0


# ── fade mask PNG rasterisation ─────────────────────────────────────────

def _centered_geometry(tile_lat, tile_lon, zoomlevel, half_m=100.0,
                       fade_width=1000.0):
    """Geometry whose square boundary is centered on the center of the ortho
    texture tile that contains the tile mid-point, returned with that tile's
    ``(til_x, til_y)`` so the ramp sits comfortably inside the 4096 extent."""
    (til_x, til_y) = GEO.wgs84_to_orthogrid(
        tile_lat + 0.5, tile_lon + 0.5, zoomlevel)
    (lat_c, lon_c) = GEO.gtile_to_wgs84(til_x + 8, til_y + 8, zoomlevel)
    d_lon = half_m * GEO.m_to_lon(lat_c)
    d_lat = half_m * GEO.m_to_lat
    cx = lon_c - tile_lon
    cy = lat_c - tile_lat
    square = MultiPolygon([Polygon([
        (cx - d_lon, cy - d_lat), (cx + d_lon, cy - d_lat),
        (cx + d_lon, cy + d_lat), (cx - d_lon, cy + d_lat)])])
    geometry = FADE.AirportOrthoGeometry(
        square, fade_width, tile_lon=tile_lon, tile_lat=tile_lat,
        ref_lat=tile_lat + 0.5)
    return geometry, til_x, til_y, lat_c, lon_c


def test_fade_mask_png_size_georeference_and_ramp(tmp_path):
    tile_lat, tile_lon, zoomlevel = 50, 10, 15
    (geometry, til_x, til_y, lat_c, lon_c) = _centered_geometry(
        tile_lat, tile_lon, zoomlevel)
    mask_path = str(tmp_path / FNAMES.airport_fade_mask_name(
        til_x, til_y, zoomlevel, "BI"))
    geometry.write_fade_mask(til_x, til_y, zoomlevel, "BI", mask_path)

    image = Image.open(mask_path)
    assert image.size == (4096, 4096)
    assert image.mode == "L"
    array = numpy.array(image)

    # Georeferencing: the ortho tile's top-left web-mercator pixel origin.
    (lat0, lon0) = GEO.gtile_to_wgs84(til_x, til_y, zoomlevel)
    (px0, py0) = GEO.wgs84_to_pix(lat0, lon0, zoomlevel)
    # A known interior point maps to a known pixel that must read opaque.
    (pcx, pcy) = GEO.wgs84_to_pix(lat_c, lon_c, zoomlevel)
    col, row = pcx - px0, pcy - py0
    assert 0 <= col < 4096 and 0 <= row < 4096
    assert array[row, col] == 255

    # Monotone ramp along a +x ray leaving the airport centre.
    ray = [int(array[row, min(col + k, 4095)]) for k in range(0, 900, 30)]
    assert ray[0] == 255
    assert all(ray[i] >= ray[i + 1] for i in range(len(ray) - 1))
    assert ray[-1] == 0
    # A far corner of the tile is well past the fade band.
    assert array[0, 0] == 0


def test_fade_mask_empty_geometry_is_black(tmp_path):
    geometry = FADE.AirportOrthoGeometry(None, 1000.0, tile_lon=10, tile_lat=50)
    mask_path = str(tmp_path / "empty_fade.png")
    geometry.write_fade_mask(100, 200, 15, "BI", mask_path)
    array = numpy.array(Image.open(mask_path))
    assert array.shape == (4096, 4096)
    assert not array.any()


# ── builder wiring: build_airport_ortho_geometry ────────────────────────

def _stub_tile(build_dir):
    tile = type("Tile", (), {})()
    tile.lat = 50
    tile.lon = 10
    tile.build_dir = build_dir
    tile.iterate = 0
    tile.airport_ortho_fade_width = 1000.0
    return tile


def test_build_from_apt_pickle(tmp_path):
    """The builder reuses the per-tile ``.apt`` boundary (decision 7)."""
    tile = _stub_tile(str(tmp_path))
    # Tile-local square boundary, as O4_Airport_Utils caches it.
    boundary = MultiPolygon([Polygon([
        (0.40, 0.40), (0.60, 0.40), (0.60, 0.60), (0.40, 0.60)])])
    dico_airports = {"TEST": {"key_type": "icao", "boundary": boundary}}
    with open(FNAMES.apt_file(tile), "wb") as handle:
        pickle.dump(dico_airports, handle)

    geometry = FADE.build_airport_ortho_geometry(tile)
    assert geometry.is_empty() is False
    # A point inside the boundary (absolute coords) is covered/opaque.
    assert geometry.covers(10.5, 50.5) is True
    assert geometry.alpha_at(10.5, 50.5) == pytest.approx(1.0)
    # A point far outside the boundary + fade band is not covered.
    assert geometry.covers(10.9, 50.9) is False


def test_build_missing_apt_is_empty(tmp_path):
    tile = _stub_tile(str(tmp_path))  # no .apt written
    geometry = FADE.build_airport_ortho_geometry(tile)
    assert geometry.is_empty() is True
    assert geometry.covers(10.5, 50.5) is False
