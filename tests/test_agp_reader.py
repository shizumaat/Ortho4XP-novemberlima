"""Unit tests for ``auto_patch.agp_reader`` and the DSF ``.agp`` OBJECT
walker.

Pure hermetic tests — they build tiny ``.agp`` files and ``library.txt``
maps under ``tmp_path`` and feed synthetic DSF text to the OBJECT walker,
so they need no X-Plane install and always run.  They pin the encoded
``.agp`` footprint math (TILE/CROP_POLY × TEXTURE_WIDTH/HEIGHT ÷
TEXTURE_SCALE, anchored at ANCHOR_PT), the ``library.txt`` resolution +
priority, and the placement→lon/lat transform.
"""
from __future__ import annotations

import math

import pytest

from auto_patch import agp_reader as A
from auto_patch.dsf_reader import _read_dsf_object_placements


# ── .agp footprint parsing ───────────────────────────────────────────
# A 128px tile spanning 60 m (mpp = 60/128 = 0.46875), anchored low so
# the parsed footprint is asymmetric in y — exactly the stock
# hangar_40x26_1 layout.
_AGP_60x60 = """A
1000
AG_POINT

TEXTURE ../../textures/transparent.png
TEXTURE_SCALE 128 128

HIDE_TILES

OBJECT app_hangar2_ext1.obj

#id: 1 / tile name: TILE.016
TEXTURE_WIDTH 60.0
TEXTURE_HEIGHT 60.0

TILE 0.0 0.0 128.0 128.0
ROTATION 0
CROP_POLY 0.0 128.0 0.0 0.0 128.0 0.0 128.0 128.0
ANCHOR_PT 64.0 29.867
"""

_MPP = 60.0 / 128.0


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


def test_parse_agp_tile_meters(tmp_path):
    fp = A.parse_agp(_write(tmp_path, "h.agp", _AGP_60x60))
    assert fp is not None
    xs = [x for x, _ in fp.local_poly]
    ys = [y for _, y in fp.local_poly]
    # Full 128px tile → 60 m on each axis.
    assert (max(xs) - min(xs)) == pytest.approx(60.0, abs=1e-6)
    assert (max(ys) - min(ys)) == pytest.approx(60.0, abs=1e-6)
    # Anchor at pixel (64, 29.867) → x centred, y offset low.
    assert min(xs) == pytest.approx(-64.0 * _MPP, abs=1e-6)
    assert min(ys) == pytest.approx(-29.867 * _MPP, abs=1e-6)
    assert max(ys) == pytest.approx((128.0 - 29.867) * _MPP, abs=1e-6)
    assert fp.rotation == 0.0


def test_parse_agp_default_anchor_is_tile_centre(tmp_path):
    text = _AGP_60x60.replace("ANCHOR_PT 64.0 29.867\n", "")
    fp = A.parse_agp(_write(tmp_path, "noanchor.agp", text))
    assert fp is not None
    xs = [x for x, _ in fp.local_poly]
    ys = [y for _, y in fp.local_poly]
    # No ANCHOR_PT → tile centre (64,64) → symmetric ±30 m on both axes.
    assert min(xs) == pytest.approx(-30.0, abs=1e-6)
    assert max(xs) == pytest.approx(30.0, abs=1e-6)
    assert min(ys) == pytest.approx(-30.0, abs=1e-6)
    assert max(ys) == pytest.approx(30.0, abs=1e-6)


def test_parse_agp_crop_tighter_than_tile(tmp_path):
    # CROP_POLY covering only pixels 32..96 (64 px → 30 m) inside the
    # 128px tile: the footprint follows the CROP_POLY, not the TILE.
    text = _AGP_60x60.replace(
        "CROP_POLY 0.0 128.0 0.0 0.0 128.0 0.0 128.0 128.0",
        "CROP_POLY 32.0 96.0 32.0 32.0 96.0 32.0 96.0 96.0")
    fp = A.parse_agp(_write(tmp_path, "crop.agp", text))
    xs = [x for x, _ in fp.local_poly]
    ys = [y for _, y in fp.local_poly]
    assert (max(xs) - min(xs)) == pytest.approx(64.0 * _MPP, abs=1e-6)
    assert (max(ys) - min(ys)) == pytest.approx(64.0 * _MPP, abs=1e-6)


def test_parse_agp_memoized(tmp_path):
    p = _write(tmp_path, "memo.agp", _AGP_60x60)
    assert A.parse_agp(p) is A.parse_agp(p)


def test_parse_agp_missing_scale_returns_none(tmp_path):
    text = _AGP_60x60.replace("TEXTURE_WIDTH 60.0\n", "")
    assert A.parse_agp(_write(tmp_path, "bad.agp", text)) is None


# ── library.txt resolution ───────────────────────────────────────────
def _make_xplane_root(tmp_path):
    root = tmp_path / "XP"
    (root / "Custom Scenery").mkdir(parents=True)
    (root / "Resources" / "default scenery" / "airport scenery").mkdir(
        parents=True)
    return root


def test_library_resolve_and_memoization(tmp_path):
    root = _make_xplane_root(tmp_path)
    pack = root / "Custom Scenery" / "MyPack"
    pack.mkdir()
    (pack / "h.agp").write_text(_AGP_60x60)
    (pack / "library.txt").write_text(
        "EXPORT lib/airport/Common_Elements/Hangars/Test.agp\th.agp\n")

    virtual = "lib/airport/Common_Elements/Hangars/Test.agp"
    phys = A.resolve_library_path(virtual, str(root))
    assert phys is not None
    assert phys.endswith("MyPack/h.agp")
    # Built once and shared.
    assert (A.get_library_index(str(root))
            is A.get_library_index(str(root)))


def test_custom_scenery_overrides_default(tmp_path):
    root = _make_xplane_root(tmp_path)
    virtual = "lib/airport/Common_Elements/Hangars/Test.agp"

    default_dir = root / "Resources" / "default scenery" / "airport scenery"
    (default_dir / "from_default.agp").write_text(_AGP_60x60)
    (default_dir / "library.txt").write_text(
        f"EXPORT {virtual}\tfrom_default.agp\n")

    pack = root / "Custom Scenery" / "Override"
    pack.mkdir()
    (pack / "from_custom.agp").write_text(_AGP_60x60)
    (pack / "library.txt").write_text(
        f"EXPORT {virtual}\tfrom_custom.agp\n")

    phys = A.resolve_library_path(virtual, str(root))
    assert phys.endswith("Override/from_custom.agp")


def test_resolve_missing_returns_none(tmp_path):
    root = _make_xplane_root(tmp_path)
    assert A.resolve_library_path(
        "lib/airport/Common_Elements/Hangars/Nope.agp", str(root)) is None


# ── placement → lon/lat transform ────────────────────────────────────
def test_footprint_lonlat_heading_rotation(tmp_path):
    root = _make_xplane_root(tmp_path)
    pack = root / "Custom Scenery" / "P"
    pack.mkdir()
    (pack / "h.agp").write_text(_AGP_60x60)
    virtual = "lib/airport/Common_Elements/Hangars/T.agp"
    (pack / "library.txt").write_text(f"EXPORT {virtual}\th.agp\n")

    lon0, lat0 = -81.0, 28.0
    ring0 = A.agp_footprint_lonlat(virtual, lon0, lat0, 0.0, str(root))
    ring90 = A.agp_footprint_lonlat(virtual, lon0, lat0, 90.0, str(root))
    assert ring0 and len(ring0) == 4
    assert ring90 and len(ring90) == 4

    # The local east extent at heading 0 should become a (south-going)
    # north extent at heading 90: the lon-span and lat-span swap roughly.
    def span(ring):
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        m_per_deg = A._LAT_M_PER_DEG
        lon_m = (max(lons) - min(lons)) * m_per_deg * math.cos(
            math.radians(lat0))
        lat_m = (max(lats) - min(lats)) * m_per_deg
        return lon_m, lat_m

    lon_m0, lat_m0 = span(ring0)
    lon_m90, lat_m90 = span(ring90)
    assert lon_m0 == pytest.approx(lat_m90, abs=0.5)
    assert lat_m0 == pytest.approx(lon_m90, abs=0.5)


# ── DSF OBJECT walker ────────────────────────────────────────────────
_DSF_LINES = [
    "OBJECT_DEF lib/airport/Common_Elements/Hangars/Lg_Maint_Gray.agp\n",
    "OBJECT_DEF lib/cars/car_static.obj\n",
    "OBJECT_DEF lib/airport/Common_Elements/Hangars/Med_Gray_Hangar.agp\n",
    "OBJECT 0 -81.10 28.20 12.5\n",          # accepted (idx 0, agp)
    "OBJECT 1 -81.11 28.21 0.0\n",           # rejected (not agp)
    "OBJECT_MSL 2 -81.12 28.22 30.0 270.0\n",  # accepted, heading at tok[5]
    "OBJECT 0 -81.13 28.23 90.0\n",          # second placement of idx 0
]


def test_object_walker_filters_and_parses_heading():
    placements = _read_dsf_object_placements(
        _DSF_LINES, A.is_agp_building_def)
    # idx 1 (the .obj) is filtered out; 3 agp placements remain.
    assert len(placements) == 3
    paths = {p[0].split("/")[-1] for p in placements}
    assert paths == {"Lg_Maint_Gray.agp", "Med_Gray_Hangar.agp"}
    # Plain OBJECT heading = tok[4].
    first = placements[0]
    assert first[1:] == pytest.approx((-81.10, 28.20, 12.5))
    # OBJECT_MSL heading = tok[5] (after the elevation field).
    msl = [p for p in placements if p[3] == 270.0]
    assert msl and msl[0][1] == pytest.approx(-81.12)


def test_object_walker_no_accepted_defs():
    lines = ["OBJECT_DEF lib/cars/car_static.obj\n",
             "OBJECT 0 -81.0 28.0 0.0\n"]
    assert _read_dsf_object_placements(lines, A.is_agp_building_def) == []


def test_is_agp_building_def_prefix_and_ext():
    assert A.is_agp_building_def(
        "lib/airport/Common_Elements/Hangars/Foo.agp")
    # wrong extension
    assert not A.is_agp_building_def(
        "lib/airport/Common_Elements/Hangars/Foo.obj")
    # right ext, wrong prefix (not the hangars scope)
    assert not A.is_agp_building_def(
        "lib/airport/Common_Elements/Parking_Items/Row_of_Cars_2.agp")
