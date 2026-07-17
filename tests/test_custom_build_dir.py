"""Tests for ``FNAMES.normalize_custom_build_dir``.

``Tile.__init__`` flags any custom_build_dir WITHOUT a trailing
separator as ``grouped=True``, which makes the 3x3 neighbor-mesh
lookups (``select_neighbor_meshes``, ``record_water_tris``) search the
SAME directory instead of the sibling ``zOrtho4XP_...`` directories.
Headless tools (run_tile_build.py, profile_tile_build.py) accept the
tile's own build directory and must normalize it to the parent with a
trailing separator — the per-tile-subdirectory mode the Qt GUI uses —
or cross-tile neighbor data is silently lost (verified live
2026-07-16: Data+37-008.mesh in the sibling directory was ignored).
"""
import os

import pytest

import O4_File_Names as FNAMES  # noqa: E402


def test_empty_passes_through():
    assert FNAMES.normalize_custom_build_dir(36, -8, "") == ""


def test_own_tile_dir_rewritten_to_parent_with_separator():
    path = os.path.join("/Custom Scenery", "zOrtho4XP_+36-008")
    normalized = FNAMES.normalize_custom_build_dir(36, -8, path)
    assert normalized == "/Custom Scenery" + os.sep
    # Round trip: build_dir() must land back in the tile's own directory.
    assert FNAMES.build_dir(36, -8, normalized) == path


def test_own_tile_dir_with_trailing_separator_also_rewritten():
    # A trailing separator on the tile's OWN directory would nest
    # zOrtho4XP_+36-008/zOrtho4XP_+36-008 — normalize that shape too.
    path = os.path.join("/Custom Scenery", "zOrtho4XP_+36-008") + os.sep
    normalized = FNAMES.normalize_custom_build_dir(36, -8, path)
    assert normalized == "/Custom Scenery" + os.sep


def test_parent_dir_with_separator_passes_through():
    path = "/Custom Scenery" + os.sep
    assert FNAMES.normalize_custom_build_dir(36, -8, path) == path


def test_grouped_dir_passes_through():
    # A bare directory that is not a zOrtho4XP_ tile dir is an
    # intentional grouped build and must not be touched.
    path = os.path.join("/Custom Scenery", "my_grouped_builds")
    assert FNAMES.normalize_custom_build_dir(36, -8, path) == path


def test_zortho_prefixed_group_name_passes_through():
    # zOrtho4XP_ prefix alone is not a tile dir — users name grouped
    # directories like this.
    path = os.path.join("/Custom Scenery", "zOrtho4XP_Iberia")
    assert FNAMES.normalize_custom_build_dir(36, -8, path) == path


def test_other_tiles_dir_raises():
    path = os.path.join("/Custom Scenery", "zOrtho4XP_+37-008")
    with pytest.raises(ValueError):
        FNAMES.normalize_custom_build_dir(36, -8, path)


def test_relative_tile_dir_rewritten_to_dot_parent():
    normalized = FNAMES.normalize_custom_build_dir(
        36, -8, "zOrtho4XP_+36-008")
    assert normalized == "." + os.sep
    assert FNAMES.build_dir(36, -8, normalized) == os.path.join(
        ".", "zOrtho4XP_+36-008")


def test_normalized_path_yields_ungrouped_tile(tmp_path):
    # The point of the whole exercise: Tile built from the normalized
    # path must NOT be grouped, so neighbor lookups walk sibling dirs
    # (verified failure mode: grouped=True made select_neighbor_meshes
    # return only the tile's own mesh despite Data+37-008.mesh existing
    # in the sibling directory).
    import O4_Config_Utils as CFG
    import O4_Mask_Utils as MASK

    path = str(tmp_path / "zOrtho4XP_+36-008")
    tile = CFG.Tile(
        36, -8, FNAMES.normalize_custom_build_dir(36, -8, path))
    assert tile.grouped is False
    assert tile.build_dir == path

    # The un-normalized path is exactly the reported failure mode.
    naive = CFG.Tile(36, -8, path)
    assert naive.grouped is True

    for name in ("zOrtho4XP_+36-008", "zOrtho4XP_+37-008"):
        (tmp_path / name).mkdir()
    (tmp_path / "zOrtho4XP_+36-008" / "Data+36-008.mesh").write_text("")
    (tmp_path / "zOrtho4XP_+37-008" / "Data+37-008.mesh").write_text("")

    assert sorted(MASK.select_neighbor_meshes(tile)) == [
        str(tmp_path / "zOrtho4XP_+36-008" / "Data+36-008.mesh"),
        str(tmp_path / "zOrtho4XP_+37-008" / "Data+37-008.mesh"),
    ]
    # Whereas the naive (grouped) tile misses the sibling's mesh.
    assert MASK.select_neighbor_meshes(naive) == [
        str(tmp_path / "zOrtho4XP_+36-008" / "Data+36-008.mesh"),
    ]
