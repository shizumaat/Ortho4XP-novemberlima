"""Headless tests for the X-Plane output side of MSFS airport conversion.

Exercises apt.dat extraction, airport lookup, pack apt.dat writing, and
exclusion-rectangle clustering against synthetic in-memory fixtures, and
round-trips a real overlay DSF through the bundled Laminar DSFTool binary
(skipped when the binary is absent or the platform is not macOS-arm64).

``tmp_path``-based only: no network, no X-Plane install required for the
non-DSFTool tests.
"""
from __future__ import annotations

import math
import platform
import subprocess
from pathlib import Path

import pytest

import O4_MSFS_XPlane_Pack as PACK


# --------------------------------------------------------------------------
# Synthetic apt.dat fixture: two airports with header, 1302 datum rows,
# a runway (100) and a helipad (102), and a trailing 99.
# --------------------------------------------------------------------------
_AIRPORT_ALPHA_BLOCK = "\n".join(
    [
        "1   1000 0 0 AAAA Alpha Field",
        "1302 icao_code AAAA",
        "1302 datum_lat 44.250000000",
        "1302 datum_lon -121.150000000",
        "100 45.72 1 1 0.25 0 3 1   5 44.2495634 -121.1617736 0 0 3 0 0 1 "
        "23 44.2591032 -121.1384471 0 0 3 8 0 1",
        "102 H1 44.2500 -121.1500 0.0 15.0 15.0 1 0 0.25 0",
    ]
)

_AIRPORT_BRAVO_BLOCK = "\n".join(
    [
        "1   500 0 0 BBBB Bravo Field",
        "1302 icao_code BBBB",
        "1302 datum_lat 40.000000000",
        "1302 datum_lon -105.000000000",
        "100 45.72 1 1 0.25 0 3 1   9 39.9990000 -105.0010000 0 0 3 0 0 1 "
        "27 40.0010000 -104.9990000 0 0 3 8 0 1",
    ]
)


def _write_apt_dat(directory: Path) -> Path:
    """Write a two-airport synthetic apt.dat and return its path."""
    text = "\n".join(
        [
            "I",
            "1100 synthetic fixture",
            _AIRPORT_ALPHA_BLOCK,
            _AIRPORT_BRAVO_BLOCK,
            "99",
            "",
        ]
    )
    apt_dat_path = directory / "apt.dat"
    apt_dat_path.write_text(text, encoding="utf-8")
    return apt_dat_path


# --------------------------------------------------------------------------
# extract_airport_from_global_apt_dat
# --------------------------------------------------------------------------
def test_extract_returns_exact_block_for_first_airport(tmp_path):
    apt_dat_path = _write_apt_dat(tmp_path)
    block = PACK.extract_airport_from_global_apt_dat(apt_dat_path, "AAAA")
    assert block == _AIRPORT_ALPHA_BLOCK


def test_extract_returns_exact_block_for_last_airport(tmp_path):
    apt_dat_path = _write_apt_dat(tmp_path)
    block = PACK.extract_airport_from_global_apt_dat(apt_dat_path, "BBBB")
    # Bravo runs up to (not including) the trailing 99.
    assert block == _AIRPORT_BRAVO_BLOCK


def test_extract_missing_airport_returns_empty(tmp_path):
    apt_dat_path = _write_apt_dat(tmp_path)
    assert PACK.extract_airport_from_global_apt_dat(apt_dat_path, "ZZZZ") == ""


def test_extract_missing_file_returns_empty(tmp_path):
    assert (
        PACK.extract_airport_from_global_apt_dat(tmp_path / "nope.dat", "AAAA") == ""
    )


# --------------------------------------------------------------------------
# find_airport_near
# --------------------------------------------------------------------------
def test_find_airport_near_picks_closest(tmp_path):
    apt_dat_path = _write_apt_dat(tmp_path)
    # Very close to Alpha's datum.
    assert (
        PACK.find_airport_near(apt_dat_path, 44.2501, -121.1501, max_kilometers=5.0)
        == "AAAA"
    )
    # Very close to Bravo's datum.
    assert (
        PACK.find_airport_near(apt_dat_path, 40.0001, -105.0001, max_kilometers=5.0)
        == "BBBB"
    )


def test_find_airport_near_out_of_range_returns_none(tmp_path):
    apt_dat_path = _write_apt_dat(tmp_path)
    # Mid-ocean, far from both airports.
    assert PACK.find_airport_near(apt_dat_path, 0.0, 0.0, max_kilometers=5.0) is None


def test_find_airport_near_respects_range_boundary(tmp_path):
    apt_dat_path = _write_apt_dat(tmp_path)
    # ~2 km north of Alpha's datum (1 degree lat ~= 111 km, so 0.018 deg
    # ~= 2 km): inside a 5 km radius, outside a 1 km radius.
    latitude = 44.25 + 2.0 / 111.32
    assert (
        PACK.find_airport_near(apt_dat_path, latitude, -121.15, max_kilometers=5.0)
        == "AAAA"
    )
    assert (
        PACK.find_airport_near(apt_dat_path, latitude, -121.15, max_kilometers=1.0)
        is None
    )


def test_find_airport_near_falls_back_to_runway(tmp_path):
    # An airport with no 1302 datum rows: position must come from the
    # runway (row 100) midpoint.
    block = "\n".join(
        [
            "1   200 0 0 CCCC Charlie",
            "100 30.0 1 1 0.25 0 3 1   9 10.0000000 20.0000000 0 0 3 0 0 1 "
            "27 10.0100000 20.0100000 0 0 3 8 0 1",
        ]
    )
    apt_dat_path = tmp_path / "apt.dat"
    apt_dat_path.write_text("I\n1100 x\n" + block + "\n99\n", encoding="utf-8")
    midpoint_latitude = (10.0 + 10.01) / 2.0
    midpoint_longitude = (20.0 + 20.01) / 2.0
    assert (
        PACK.find_airport_near(
            apt_dat_path, midpoint_latitude, midpoint_longitude, max_kilometers=5.0
        )
        == "CCCC"
    )


def test_find_airport_near_falls_back_to_helipad(tmp_path):
    # No datum, no runway: position must come from the helipad (row 102).
    block = "\n".join(
        [
            "17  50 0 0 DDDD Delta Heliport",
            "102 H1 51.5000000 -0.1000000 0.0 15.0 15.0 1 0 0.25 0",
        ]
    )
    apt_dat_path = tmp_path / "apt.dat"
    apt_dat_path.write_text("I\n1100 x\n" + block + "\n99\n", encoding="utf-8")
    assert (
        PACK.find_airport_near(apt_dat_path, 51.5001, -0.1001, max_kilometers=5.0)
        == "DDDD"
    )


# --------------------------------------------------------------------------
# write_pack_apt_dat
# --------------------------------------------------------------------------
def test_write_pack_apt_dat_header_footer_exact(tmp_path):
    PACK.write_pack_apt_dat(tmp_path, _AIRPORT_ALPHA_BLOCK)
    apt_dat_path = tmp_path / "Earth nav data" / "apt.dat"
    assert apt_dat_path.is_file()
    content = apt_dat_path.read_text(encoding="utf-8")
    expected = (
        "I\n"
        "1100 Generated by Ortho4XP MSFS airport converter\n"
        + _AIRPORT_ALPHA_BLOCK
        + "\n99\n"
    )
    assert content == expected
    # Newline-terminated.
    assert content.endswith("\n")
    lines = content.splitlines()
    assert lines[0] == "I"
    assert lines[1] == "1100 Generated by Ortho4XP MSFS airport converter"
    assert lines[-1] == "99"


def test_write_pack_apt_dat_strips_block_edge_newlines(tmp_path):
    PACK.write_pack_apt_dat(tmp_path, "\n" + _AIRPORT_ALPHA_BLOCK + "\n\n")
    content = (tmp_path / "Earth nav data" / "apt.dat").read_text(encoding="utf-8")
    # No blank line should sneak in before the 99.
    assert "\n\n99\n" not in content
    assert content.endswith(_AIRPORT_ALPHA_BLOCK + "\n99\n")


# --------------------------------------------------------------------------
# compute_exclusion_rectangles
# --------------------------------------------------------------------------
def test_compute_exclusion_empty_returns_empty():
    assert PACK.compute_exclusion_rectangles([]) == []


def test_compute_exclusion_two_nearby_objects_merge_with_padding():
    latitude = 44.25
    padding_meters = 20.0
    objects = [
        PACK.PlacedObject("objects/a.obj", -121.150, latitude, 0.0),
        PACK.PlacedObject("objects/b.obj", -121.149, latitude, 90.0),
    ]
    rectangles = PACK.compute_exclusion_rectangles(objects, padding_meters)
    # Two objects ~80 m apart collapse to a single bounding rectangle.
    assert len(rectangles) == 1
    west, south, east, north = rectangles[0]

    latitude_padding_degrees = padding_meters / 111320.0
    longitude_padding_degrees = padding_meters / (
        111320.0 * math.cos(math.radians(latitude))
    )
    # Latitude bounds: both objects share the latitude, so the box is the
    # single latitude +/- the metre-based padding.
    assert south == pytest.approx(latitude - latitude_padding_degrees, abs=1e-9)
    assert north == pytest.approx(latitude + latitude_padding_degrees, abs=1e-9)
    # Longitude bounds: extremes padded, longitude padding scaled by
    # cos(latitude) (verifies the degrees-vs-metres conversion).
    assert west == pytest.approx(-121.150 - longitude_padding_degrees, abs=1e-9)
    assert east == pytest.approx(-121.149 + longitude_padding_degrees, abs=1e-9)
    # Longitude padding must exceed latitude padding at this latitude
    # (cos(44.25 deg) < 1).
    assert longitude_padding_degrees > latitude_padding_degrees


def test_compute_exclusion_covers_rotated_model_footprint():
    # A 200 m x 10 m model (x in [-100, 100], z in [-5, 5]) placed at
    # heading 0 spans east-west; at heading 90 the same footprint spans
    # north-south. The exclusion box must follow the rotation.
    latitude = 44.25
    padding_meters = 20.0
    bounds = (-100.0, -5.0, 100.0, 5.0)
    metres_per_degree_latitude = 111320.0
    metres_per_degree_longitude = metres_per_degree_latitude * math.cos(
        math.radians(latitude)
    )

    def box_for(heading):
        objects = [
            PACK.PlacedObject(
                "objects/terminal.obj", -121.150, latitude, heading,
                bounds_xz=bounds,
            )
        ]
        rectangles = PACK.compute_exclusion_rectangles(objects, padding_meters)
        assert len(rectangles) == 1
        return rectangles[0]

    west, south, east, north = box_for(0.0)
    assert (east - west) * metres_per_degree_longitude == pytest.approx(
        2 * (100.0 + padding_meters), rel=1e-6
    )
    assert (north - south) * metres_per_degree_latitude == pytest.approx(
        2 * (5.0 + padding_meters), rel=1e-6
    )

    west, south, east, north = box_for(90.0)
    assert (east - west) * metres_per_degree_longitude == pytest.approx(
        2 * (5.0 + padding_meters), rel=1e-6
    )
    assert (north - south) * metres_per_degree_latitude == pytest.approx(
        2 * (100.0 + padding_meters), rel=1e-6
    )


def test_compute_exclusion_point_placements_unchanged_without_bounds():
    # No bounds: behaves exactly like the old point-plus-padding rule.
    latitude = 44.25
    objects = [PACK.PlacedObject("objects/a.obj", -121.150, latitude, 0.0)]
    rectangles = PACK.compute_exclusion_rectangles(objects, 20.0)
    assert len(rectangles) == 1
    west, south, east, north = rectangles[0]
    latitude_padding = 20.0 / 111320.0
    longitude_padding = 20.0 / (111320.0 * math.cos(math.radians(latitude)))
    assert west == pytest.approx(-121.150 - longitude_padding, abs=1e-12)
    assert east == pytest.approx(-121.150 + longitude_padding, abs=1e-12)
    assert south == pytest.approx(latitude - latitude_padding, abs=1e-12)
    assert north == pytest.approx(latitude + latitude_padding, abs=1e-12)


def test_compute_exclusion_far_apart_objects_stay_separate():
    # Two objects ~4 km apart (span > 2 km) with small padding must not
    # collapse into one bounding rectangle.
    objects = [
        PACK.PlacedObject("objects/a.obj", -121.200, 44.25, 0.0),
        PACK.PlacedObject("objects/b.obj", -121.150, 44.25, 0.0),
    ]
    rectangles = PACK.compute_exclusion_rectangles(objects, padding_meters=20.0)
    assert len(rectangles) == 2


# --------------------------------------------------------------------------
# _build_tile_dsf_text placement rows (headless; no DSFTool needed)
# --------------------------------------------------------------------------
def test_dsf_text_places_altitude_as_agl_msl_or_draped():
    placements = [
        # Altitude 0: ground-draped OBJECT regardless of the AGL flag.
        PACK.PlacedObject("objects/a.obj", -121.5, 44.5, 0.0),
        PACK.PlacedObject("objects/a.obj", -121.4, 44.4, 10.0,
                          altitude_meters=0.0, is_above_ground=False),
        # Non-zero + above-ground flag: OBJECT_AGL.
        PACK.PlacedObject("objects/a.obj", -121.3, 44.3, 20.0,
                          altitude_meters=16.25, is_above_ground=True),
        # Non-zero + absolute: OBJECT_MSL.
        PACK.PlacedObject("objects/a.obj", -121.2, 44.2, 30.0,
                          altitude_meters=-2.5, is_above_ground=False),
    ]
    text = PACK._build_tile_dsf_text(-122, 44, placements, [])
    object_rows = [
        line for line in text.splitlines() if line.startswith("OBJECT ")
        or line.startswith("OBJECT_AGL ") or line.startswith("OBJECT_MSL ")
    ]
    assert len(object_rows) == 4
    assert object_rows[0].startswith("OBJECT 0 -121.5")
    assert object_rows[1].startswith("OBJECT 0 -121.4")
    assert object_rows[2].startswith("OBJECT_AGL 0 -121.3")
    assert object_rows[2].endswith("16.250")
    assert object_rows[3].startswith("OBJECT_MSL 0 -121.2")
    assert object_rows[3].endswith("-2.500")


# --------------------------------------------------------------------------
# write_overlay_dsf (real DSFTool round-trip)
# --------------------------------------------------------------------------
_DSFTOOL_PATH = (
    Path(__file__).resolve().parent.parent / "Utils" / "mac" / "DSFTool"
)
_IS_MACOS_ARM = platform.system() == "Darwin" and platform.machine() == "arm64"

_dsftool_reason = "requires the bundled macOS-arm64 DSFTool binary"
requires_dsftool = pytest.mark.skipif(
    not (_IS_MACOS_ARM and _DSFTOOL_PATH.is_file()), reason=_dsftool_reason
)


@requires_dsftool
def test_write_overlay_dsf_round_trips_through_dsftool(tmp_path):
    pack_directory = tmp_path / "MSFS Convert - TEST"
    # Two placements in the lat 44, lon -122 tile (folder +40-130).
    placements = [
        PACK.PlacedObject("objects/alpha.obj", -121.161, 44.254, 90.0),
        PACK.PlacedObject("objects/bravo.obj", -121.160, 44.255, 180.0),
        # Elevated placements: OBJECT_AGL and OBJECT_MSL rows must survive
        # the text2dsf/dsf2text round trip.
        PACK.PlacedObject("objects/alpha.obj", -121.159, 44.256, 0.0,
                          altitude_meters=16.25, is_above_ground=True),
        PACK.PlacedObject("objects/bravo.obj", -121.158, 44.257, 45.0,
                          altitude_meters=938.0, is_above_ground=False),
    ]
    exclusions = PACK.compute_exclusion_rectangles(placements)
    assert len(exclusions) == 1

    earth_nav_data = PACK.write_overlay_dsf(
        pack_directory, placements, exclusions, _DSFTOOL_PATH
    )
    assert earth_nav_data == pack_directory / "Earth nav data"

    dsf_path = earth_nav_data / "+40-130" / "+44-122.dsf"
    assert dsf_path.is_file()

    # Round-trip back to text via DSFTool and assert the round-tripped
    # source carries our OBJECT / OBJECT_DEF / exclusion lines.
    text_out = tmp_path / "roundtrip.txt"
    completed = subprocess.run(
        [str(_DSFTOOL_PATH), "--dsf2text", str(dsf_path), str(text_out)],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    round_tripped = text_out.read_text(encoding="utf-8")

    assert "OBJECT_DEF objects/alpha.obj" in round_tripped
    assert "OBJECT_DEF objects/bravo.obj" in round_tripped
    # One OBJECT line per ground-draped placement, plus the elevated rows.
    object_lines = [
        line
        for line in round_tripped.splitlines()
        if line.startswith("OBJECT ")
    ]
    assert len(object_lines) == 2
    agl_lines = [
        line
        for line in round_tripped.splitlines()
        if line.startswith("OBJECT_AGL ")
    ]
    msl_lines = [
        line
        for line in round_tripped.splitlines()
        if line.startswith("OBJECT_MSL ")
    ]
    assert len(agl_lines) == 1 and "16.25" in agl_lines[0]
    assert len(msl_lines) == 1 and "938" in msl_lines[0]

    # The exclusion properties survive with the slash-separated value.
    west, south, east, north = exclusions[0]
    expected_value = PACK._format_exclusion_value(exclusions[0])
    assert (
        "PROPERTY sim/exclude_obj " + expected_value in round_tripped
    )
    assert (
        "PROPERTY sim/exclude_fac " + expected_value in round_tripped
    )
    assert (
        "PROPERTY sim/exclude_agp " + expected_value in round_tripped
    )


@requires_dsftool
def test_write_overlay_dsf_multiple_tiles(tmp_path):
    pack_directory = tmp_path / "MSFS Convert - MULTI"
    # One placement in each of two 1x1 tiles.
    placements = [
        PACK.PlacedObject("objects/a.obj", -121.5, 44.5, 0.0),
        PACK.PlacedObject("objects/b.obj", -120.5, 45.5, 0.0),
    ]
    exclusions = PACK.compute_exclusion_rectangles(placements)
    earth_nav_data = PACK.write_overlay_dsf(
        pack_directory, placements, exclusions, _DSFTOOL_PATH
    )
    # lat 44 lon -122 -> +40-130/+44-122.dsf ; lat 45 lon -121 -> same
    # 10-degree folder +40-130 but tile +45-121.
    assert (earth_nav_data / "+40-130" / "+44-122.dsf").is_file()
    assert (earth_nav_data / "+40-130" / "+45-121.dsf").is_file()


def test_write_overlay_dsf_no_placements_returns_nav_dir(tmp_path):
    pack_directory = tmp_path / "empty"
    result = PACK.write_overlay_dsf(pack_directory, [], [], _DSFTOOL_PATH)
    assert result == pack_directory / "Earth nav data"
    assert result.is_dir()


def test_write_overlay_dsf_bad_dsftool_raises(tmp_path):
    pack_directory = tmp_path / "MSFS Convert - BAD"
    placements = [PACK.PlacedObject("objects/a.obj", -121.5, 44.5, 0.0)]
    with pytest.raises(RuntimeError):
        PACK.write_overlay_dsf(
            pack_directory, placements, [], tmp_path / "does-not-exist-dsftool"
        )
