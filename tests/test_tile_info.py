"""Unit tests for :mod:`O4_Tile_Info`, the headless built-tile scanner.

Fixtures build fake tile trees under pytest's ``tmp_path`` so no real
Ortho4XP data is required.  ``src/`` is already on ``sys.path`` via the
project ``conftest.py``; the defensive insert below keeps the module
importable if the test file is ever run in isolation.
"""

import os
import sys

_SRC = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import O4_File_Names as FNAMES  # noqa: E402
import O4_Tile_Info as TI  # noqa: E402


##############################################################################
# Fixture builders
##############################################################################
def _write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _make_dsf(build_dir, lat, lon):
    """Create the tile's ``.dsf`` inside ``build_dir/Earth nav data``."""
    dsf = os.path.join(
        build_dir, "Earth nav data", FNAMES.long_latlon(lat, lon) + ".dsf"
    )
    _write(dsf, "dsf")
    return dsf


def _make_cfg(build_dir, lat, lon, website="BI", zl=16, zone_list="[]"):
    cfg = os.path.join(
        build_dir, "Ortho4XP_" + FNAMES.short_latlon(lat, lon) + ".cfg"
    )
    lines = []
    if website is not None:
        lines.append("default_website=" + website)
    if zl is not None:
        lines.append("default_zl=" + str(zl))
    if zone_list is not None:
        lines.append("zone_list=" + zone_list)
    _write(cfg, "\n".join(lines) + "\n")
    return cfg


def _tile_build_dir(working_dir, lat, lon):
    d = os.path.join(working_dir, "zOrtho4XP_" + FNAMES.short_latlon(lat, lon))
    os.makedirs(d, exist_ok=True)
    return d


##############################################################################
# Per-tile scan
##############################################################################
def test_scan_finds_built_tile(tmp_path):
    wd = str(tmp_path)
    bd = _tile_build_dir(wd, 43, 3)
    _make_dsf(bd, 43, 3)
    _make_cfg(bd, 43, 3, website="BI", zl=16, zone_list="[]")

    tiles = TI.scan_tiles(wd)
    assert set(tiles) == {(43, 3)}
    info = tiles[(43, 3)]
    assert info.dsf_present is True
    assert info.provider == "BI"
    assert info.zl == 16
    assert info.has_zones is False
    assert info.dir_name == "zOrtho4XP_+43+003"
    assert os.path.isabs(info.build_dir)


def test_scan_zone_list_sets_has_zones(tmp_path):
    wd = str(tmp_path)
    bd = _tile_build_dir(wd, 43, 3)
    _make_dsf(bd, 43, 3)
    _make_cfg(
        bd, 43, 3, website="BI", zl=16,
        zone_list="[[[0,0,1,0,1,1],16,'BI']]",
    )
    info = TI.scan_tiles(wd)[(43, 3)]
    assert info.has_zones is True


def test_scan_cfg_only_dir_included_without_dsf(tmp_path):
    wd = str(tmp_path)
    bd = _tile_build_dir(wd, 43, 3)
    _make_cfg(bd, 43, 3)  # no dsf created

    tiles = TI.scan_tiles(wd)
    assert (43, 3) in tiles
    assert tiles[(43, 3)].dsf_present is False


def test_scan_dir_without_cfg_or_dsf_skipped(tmp_path):
    wd = str(tmp_path)
    bd = _tile_build_dir(wd, 43, 3)
    _write(os.path.join(bd, "random.txt"), "junk")

    assert TI.scan_tiles(wd) == {}


def test_scan_duplicate_latlon_first_wins(tmp_path):
    wd = str(tmp_path)
    # Two differently-named dirs for the same (lat, lon).
    d1 = os.path.join(wd, "AXP_+43+003")
    d2 = os.path.join(wd, "BXP_+43+003")
    for d in (d1, d2):
        os.makedirs(d)
        _make_dsf(d, 43, 3)
        _make_cfg(d, 43, 3)

    tiles = TI.scan_tiles(wd)
    assert set(tiles) == {(43, 3)}
    # First in directory-listing order wins; verify it is one of the two and
    # that exactly one survived.
    winner = tiles[(43, 3)].dir_name
    expected = sorted(os.listdir(wd))[0]
    assert winner == expected


def test_scan_negative_latlon(tmp_path):
    wd = str(tmp_path)
    # -34+151 (Sydney-ish): confirms short_latlon-derived names parse back.
    assert FNAMES.short_latlon(-34, 151) == "-34+151"
    bd = _tile_build_dir(wd, -34, 151)
    _make_dsf(bd, -34, 151)
    _make_cfg(bd, -34, 151, website="GO2", zl=17)

    tiles = TI.scan_tiles(wd)
    assert (-34, 151) in tiles
    info = tiles[(-34, 151)]
    assert info.provider == "GO2"
    assert info.zl == 17
    assert info.dsf_present is True


def test_scan_bad_zl_tolerated(tmp_path):
    wd = str(tmp_path)
    bd = _tile_build_dir(wd, 43, 3)
    _make_dsf(bd, 43, 3)
    _make_cfg(bd, 43, 3, website="BI", zl="notanint", zone_list="[]")
    info = TI.scan_tiles(wd)[(43, 3)]
    assert info.zl is None
    assert info.provider == "BI"


##############################################################################
# Grouped scan
##############################################################################
def test_scan_grouped(tmp_path):
    wd = str(tmp_path)
    # working_dir IS the build dir; two tiles under one Earth nav data.
    _make_dsf(wd, 43, 3)
    _make_dsf(wd, 44, 3)
    _make_cfg(wd, 43, 3, website="BI", zl=16)
    _make_cfg(wd, 44, 3, website="BI", zl=18)

    tiles = TI.scan_tiles(wd, grouped=True)
    assert set(tiles) == {(43, 3), (44, 3)}
    assert tiles[(43, 3)].zl == 16
    assert tiles[(44, 3)].zl == 18
    for info in tiles.values():
        assert info.dsf_present is True
        assert info.build_dir == os.path.abspath(wd)
        assert info.dir_name == os.path.basename(os.path.normpath(wd))


def test_scan_grouped_no_earth_nav_data(tmp_path):
    assert TI.scan_tiles(str(tmp_path), grouped=True) == {}


##############################################################################
# mesh_date / imagery_date
##############################################################################
def test_mesh_and_imagery_dates_pick_newest(tmp_path):
    wd = str(tmp_path)
    bd = _tile_build_dir(wd, 43, 3)
    dsf = _make_dsf(bd, 43, 3)
    mesh = os.path.join(bd, "Data+43+003.mesh")
    _write(mesh, "mesh")
    _make_cfg(bd, 43, 3)

    tex_old = os.path.join(bd, "textures", "old.dds")
    tex_new = os.path.join(bd, "textures", "new.dds")
    _write(tex_old, "a")
    _write(tex_new, "b")

    # Control mtimes explicitly.
    os.utime(dsf, (1000, 1000))
    os.utime(mesh, (2000, 2000))  # newest mesh/dsf
    os.utime(tex_old, (3000, 3000))
    os.utime(tex_new, (5000, 5000))  # newest texture

    info = TI.scan_tiles(wd)[(43, 3)]
    assert info.mesh_date == 2000
    assert info.imagery_date == 5000


def test_dates_none_when_absent(tmp_path):
    wd = str(tmp_path)
    bd = _tile_build_dir(wd, 43, 3)
    _make_cfg(bd, 43, 3)  # cfg only, no dsf/mesh/textures
    info = TI.scan_tiles(wd)[(43, 3)]
    assert info.mesh_date is None
    assert info.imagery_date is None


##############################################################################
# compute_size
##############################################################################
def test_compute_size_sums_nested_files(tmp_path):
    wd = str(tmp_path)
    bd = _tile_build_dir(wd, 43, 3)
    _make_dsf(bd, 43, 3)  # "dsf" -> 3 bytes
    _write(os.path.join(bd, "textures", "a.dds"), "12345")  # 5 bytes
    _write(os.path.join(bd, "nested", "deep", "b.bin"), "xy")  # 2 bytes

    info = TI.scan_tiles(wd)[(43, 3)]
    assert info.size_bytes is None
    total = TI.compute_size(info)
    assert total == 3 + 5 + 2
    assert info.size_bytes == total


##############################################################################
# tile_info single lookup
##############################################################################
def test_tile_info_per_tile_default_name(tmp_path):
    wd = str(tmp_path)
    bd = _tile_build_dir(wd, 43, 3)
    _make_dsf(bd, 43, 3)
    _make_cfg(bd, 43, 3, website="BI", zl=16)

    info = TI.tile_info(43, 3, wd)
    assert info is not None
    assert info.provider == "BI"
    assert info.dsf_present is True


def test_tile_info_per_tile_nondefault_name_fallback(tmp_path):
    wd = str(tmp_path)
    # Directory does not use the default zOrtho4XP name.
    bd = os.path.join(wd, "customXP_+43+003")
    os.makedirs(bd)
    _make_dsf(bd, 43, 3)
    _make_cfg(bd, 43, 3, website="BI", zl=16)

    info = TI.tile_info(43, 3, wd)
    assert info is not None
    assert info.dir_name == "customXP_+43+003"


def test_tile_info_grouped(tmp_path):
    wd = str(tmp_path)
    _make_dsf(wd, 43, 3)
    _make_cfg(wd, 43, 3, website="BI", zl=16)

    info = TI.tile_info(43, 3, wd, grouped=True)
    assert info is not None
    assert info.zl == 16
    assert info.build_dir == os.path.abspath(wd)


def test_tile_info_none_when_missing(tmp_path):
    wd = str(tmp_path)
    assert TI.tile_info(43, 3, wd) is None
    assert TI.tile_info(43, 3, wd, grouped=True) is None


##############################################################################
# Symlinked tiles (X-Plane Custom Scenery folders commonly hold links to
# tile folders on external drives — the scanner must follow them)
##############################################################################
def test_scan_follows_symlinked_tile_dirs(tmp_path):
    """A ``zOrtho4XP_*`` entry that is a symlink to a build directory on
    another volume must be reported exactly like a plain directory."""
    real_store = tmp_path / "external_drive" / "Ortho4XP"
    bd = _tile_build_dir(str(real_store), 43, 3)
    _make_dsf(bd, 43, 3)
    _make_cfg(bd, 43, 3, website="BI", zl=17)

    wd = tmp_path / "Custom Scenery"
    wd.mkdir()
    os.symlink(bd, str(wd / "zOrtho4XP_+43+003"))

    tiles = TI.scan_tiles(str(wd))
    assert set(tiles) == {(43, 3)}
    info = tiles[(43, 3)]
    assert info.dsf_present is True
    assert info.zl == 17


def test_scan_skips_broken_symlink(tmp_path):
    wd = tmp_path / "Custom Scenery"
    wd.mkdir()
    os.symlink(str(tmp_path / "gone"), str(wd / "zOrtho4XP_+43+003"))
    assert TI.scan_tiles(str(wd)) == {}


# ---------------------------------------------------------------------------
# Incremental scan (iter_scan_tiles) — drives the live map progress overlay
# ---------------------------------------------------------------------------
def test_iter_scan_matches_scan_and_reports_progress(tmp_path):
    wd = str(tmp_path)
    for lat, lon in ((43, 3), (44, 3), (-13, -77)):
        bd = _tile_build_dir(wd, lat, lon)
        _make_dsf(bd, lat, lon)
        _make_cfg(bd, lat, lon)
    (tmp_path / "not_a_tile.txt").write_text("junk")
    (tmp_path / "SomeOtherFolder").mkdir()

    steps = list(TI.iter_scan_tiles(wd))
    total_entries = len(os.listdir(wd))
    # One yield per directory entry, done counting 1..total.
    assert [d for (d, _t, _k, _i) in steps] == list(
        range(1, total_entries + 1))
    assert all(t == total_entries for (_d, t, _k, _i) in steps)
    # Drained generator == the one-shot scan (same keys AND same infos).
    streamed = {k: i for (_d, _t, k, i) in steps if k is not None}
    full = TI.scan_tiles(wd)
    assert set(streamed) == set(full) == {(43, 3), (44, 3), (-13, -77)}
    for key in full:
        assert streamed[key].build_dir == full[key].build_dir
        assert streamed[key].dir_name == full[key].dir_name


def test_iter_scan_duplicate_latlon_first_wins(tmp_path):
    wd = str(tmp_path)
    first = os.path.join(wd, "aOrtho4XP_+43+003")
    second = os.path.join(wd, "zOrtho4XP_+43+003")
    for bd in (first, second):
        os.makedirs(bd)
        _make_dsf(bd, 43, 3)
        _make_cfg(bd, 43, 3)

    yielded = [
        (k, i) for (_d, _t, k, i) in TI.iter_scan_tiles(wd) if k is not None
    ]
    # The duplicate (lat, lon) is yielded exactly once, from the
    # sorted-first directory — the same winner scan_tiles picks.
    assert len(yielded) == 1
    assert yielded[0][0] == (43, 3)
    assert yielded[0][1].dir_name == "aOrtho4XP_+43+003"


def test_iter_scan_missing_dir_yields_nothing(tmp_path):
    assert list(TI.iter_scan_tiles(str(tmp_path / "absent"))) == []
