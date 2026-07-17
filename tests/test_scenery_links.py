"""Headless unit tests for :mod:`O4_Scenery_Links` (spec sections 4 & 5).

All tests are tmp_path-based, use no network, and pass on Linux where symlinks
are always available.
"""

import os
import sys

import pytest

# Match the project convention (see tests/conftest.py): put src/ on sys.path so
# the flat O4 modules import directly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import O4_Scenery_Links as SL  # noqa: E402
from O4_Scenery_Links import LinkStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def dirs(tmp_path):
    """Return (scenery_dir, build_dir) as existing, distinct directories."""
    scenery = tmp_path / "Custom Scenery"  # space in path (regression guard)
    build = tmp_path / "Tiles" / "zOrtho4XP_+48-006"
    scenery.mkdir(parents=True)
    build.mkdir(parents=True)
    # Put some content in the build dir to assert it is never touched.
    (build / "Earth nav data").mkdir()
    (build / "Earth nav data" / "data.dsf").write_text("payload")
    return str(scenery), str(build)


LAT, LON = 48, -6


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------
def test_link_name():
    assert SL.link_name(48, -6) == "zOrtho4XP_+48-006"
    assert SL.link_name(-34, 12) == "zOrtho4XP_-34+012"


def test_group_link_name():
    assert SL.group_link_name("/a/b/MyGroup") == "zOrtho4XP_MyGroup"
    assert SL.group_link_name("/a/b/MyGroup/") == "zOrtho4XP_MyGroup"


# ---------------------------------------------------------------------------
# install / status happy path
# ---------------------------------------------------------------------------
def test_install_creates_symlink_and_status_installed(dirs):
    scenery, build = dirs
    assert SL.link_status(LAT, LON, build, scenery) is LinkStatus.NOT_INSTALLED
    SL.install(LAT, LON, build, scenery)
    link = os.path.join(scenery, "zOrtho4XP_+48-006")
    assert os.path.islink(link)
    assert os.path.realpath(link) == os.path.realpath(build)
    # Link resolves to real build content.
    assert os.path.isfile(os.path.join(link, "Earth nav data", "data.dsf"))
    assert SL.link_status(LAT, LON, build, scenery) is LinkStatus.INSTALLED


def test_install_idempotent(dirs):
    scenery, build = dirs
    SL.install(LAT, LON, build, scenery)
    SL.install(LAT, LON, build, scenery)  # no raise, still one link
    assert SL.link_status(LAT, LON, build, scenery) is LinkStatus.INSTALLED


def test_uninstall_removes_only_link(dirs):
    scenery, build = dirs
    SL.install(LAT, LON, build, scenery)
    link = os.path.join(scenery, "zOrtho4XP_+48-006")
    assert os.path.lexists(link)
    SL.uninstall(LAT, LON, build, scenery)
    assert not os.path.lexists(link)
    # Build dir and its contents untouched.
    assert os.path.isfile(os.path.join(build, "Earth nav data", "data.dsf"))
    assert (
        open(os.path.join(build, "Earth nav data", "data.dsf")).read() == "payload"
    )
    assert SL.link_status(LAT, LON, build, scenery) is LinkStatus.NOT_INSTALLED


def test_uninstall_noop_when_not_installed(dirs):
    scenery, build = dirs
    # Should not raise.
    SL.uninstall(LAT, LON, build, scenery)


# ---------------------------------------------------------------------------
# Status: BROKEN / CONFLICT / UNAVAILABLE
# ---------------------------------------------------------------------------
def test_status_broken_after_target_deleted(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    build = tmp_path / "Tiles" / "zOrtho4XP_+48-006"
    scenery.mkdir(parents=True)
    build.mkdir(parents=True)
    SL.install(LAT, LON, str(build), str(scenery))
    # Remove the target -> link dangles.
    import shutil

    shutil.rmtree(build)
    assert SL.link_status(LAT, LON, str(build), str(scenery)) is LinkStatus.BROKEN


def test_status_conflict_real_dir(dirs):
    scenery, build = dirs
    # Hand-made real directory with the expected name.
    os.mkdir(os.path.join(scenery, "zOrtho4XP_+48-006"))
    assert SL.link_status(LAT, LON, build, scenery) is LinkStatus.CONFLICT


def test_status_conflict_foreign_link(dirs, tmp_path):
    scenery, build = dirs
    elsewhere = tmp_path / "somewhere_else"
    elsewhere.mkdir()
    os.symlink(str(elsewhere), os.path.join(scenery, "zOrtho4XP_+48-006"))
    assert SL.link_status(LAT, LON, build, scenery) is LinkStatus.CONFLICT


def test_status_unavailable_missing_scenery(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    assert (
        SL.link_status(LAT, LON, str(build), "") is LinkStatus.UNAVAILABLE
    )
    assert (
        SL.link_status(LAT, LON, str(build), str(tmp_path / "nope"))
        is LinkStatus.UNAVAILABLE
    )


def test_status_unavailable_scenery_equals_build(tmp_path):
    d = tmp_path / "same"
    d.mkdir()
    assert (
        SL.link_status(LAT, LON, str(d), str(d)) is LinkStatus.UNAVAILABLE
    )


# ---------------------------------------------------------------------------
# install refuses to overwrite; replaces BROKEN
# ---------------------------------------------------------------------------
def test_install_on_conflict_raises_and_dir_survives(dirs):
    scenery, build = dirs
    conflict = os.path.join(scenery, "zOrtho4XP_+48-006")
    os.mkdir(conflict)
    with open(os.path.join(conflict, "marker.txt"), "w") as fh:
        fh.write("keep me")
    with pytest.raises(ValueError):
        SL.install(LAT, LON, build, scenery)
    # The foreign directory and its content survive untouched.
    assert os.path.isdir(conflict)
    assert not os.path.islink(conflict)
    assert os.path.isfile(os.path.join(conflict, "marker.txt"))


def test_install_on_unavailable_raises(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    with pytest.raises(ValueError):
        SL.install(LAT, LON, str(build), "")


def test_install_replaces_broken(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    build = tmp_path / "Tiles" / "zOrtho4XP_+48-006"
    scenery.mkdir(parents=True)
    build.mkdir(parents=True)
    # Create a broken link with the expected name, pointing at a missing target.
    link = os.path.join(str(scenery), "zOrtho4XP_+48-006")
    os.symlink(str(tmp_path / "gone"), link)
    assert SL.link_status(LAT, LON, str(build), str(scenery)) is LinkStatus.BROKEN
    SL.install(LAT, LON, str(build), str(scenery))
    assert SL.link_status(LAT, LON, str(build), str(scenery)) is LinkStatus.INSTALLED
    assert os.path.realpath(link) == os.path.realpath(str(build))


# ---------------------------------------------------------------------------
# uninstall behavior on BROKEN / CONFLICT
# ---------------------------------------------------------------------------
def test_uninstall_broken_allowed(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    build = tmp_path / "Tiles" / "zOrtho4XP_+48-006"
    scenery.mkdir(parents=True)
    build.mkdir(parents=True)
    link = os.path.join(str(scenery), "zOrtho4XP_+48-006")
    os.symlink(str(tmp_path / "gone"), link)
    assert SL.link_status(LAT, LON, str(build), str(scenery)) is LinkStatus.BROKEN
    SL.uninstall(LAT, LON, str(build), str(scenery))
    assert not os.path.lexists(link)


def test_uninstall_on_conflict_raises(dirs):
    scenery, build = dirs
    conflict = os.path.join(scenery, "zOrtho4XP_+48-006")
    os.mkdir(conflict)
    with pytest.raises(ValueError):
        SL.uninstall(LAT, LON, build, scenery)
    assert os.path.isdir(conflict)  # not deleted


# ---------------------------------------------------------------------------
# grouped mode
# ---------------------------------------------------------------------------
def test_grouped_install_status_uninstall(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    group = tmp_path / "Tiles" / "MyRegion"
    scenery.mkdir(parents=True)
    group.mkdir(parents=True)
    (group / "content.txt").write_text("x")

    assert (
        SL.link_status(LAT, LON, str(group), str(scenery), grouped=True)
        is LinkStatus.NOT_INSTALLED
    )
    SL.install(LAT, LON, str(group), str(scenery), grouped=True)
    link = os.path.join(str(scenery), "zOrtho4XP_MyRegion")
    assert os.path.islink(link)
    assert os.path.realpath(link) == os.path.realpath(str(group))
    assert (
        SL.link_status(LAT, LON, str(group), str(scenery), grouped=True)
        is LinkStatus.INSTALLED
    )
    # Per-tile link name should not exist in grouped mode.
    assert not os.path.lexists(os.path.join(str(scenery), "zOrtho4XP_+48-006"))

    SL.uninstall(LAT, LON, str(group), str(scenery), grouped=True)
    assert not os.path.lexists(link)
    assert os.path.isfile(os.path.join(str(group), "content.txt"))  # untouched


# ---------------------------------------------------------------------------
# installed_tiles()
# ---------------------------------------------------------------------------
def test_installed_tiles_parses_and_filters(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    scenery.mkdir()
    tiles_root = tmp_path / "Tiles"
    tiles_root.mkdir()

    def make_tile(lat, lon):
        b = tiles_root / SL.link_name(lat, lon)
        b.mkdir()
        SL.install(lat, lon, str(b), str(scenery))
        return str(b)

    t1 = make_tile(48, -6)
    t2 = make_tile(-34, 12)  # negative latitude
    t3 = make_tile(0, 0)

    # A foreign directory and a foreign link that must be ignored.
    (scenery / "SomeOtherScenery").mkdir()
    os.symlink(str(tmp_path), os.path.join(str(scenery), "yOrtho4XP_Overlays"))

    # A broken per-tile link that must be skipped (installed then target gone).
    bshort = tiles_root / SL.link_name(10, 20)
    bshort.mkdir()
    SL.install(10, 20, str(bshort), str(scenery))
    import shutil

    shutil.rmtree(bshort)

    result = SL.installed_tiles(str(scenery))
    assert set(result.keys()) == {(48, -6), (-34, 12), (0, 0)}
    assert result[(48, -6)] == os.path.realpath(t1)
    assert result[(-34, 12)] == os.path.realpath(t2)
    assert result[(0, 0)] == os.path.realpath(t3)


def test_installed_tiles_empty_or_missing(tmp_path):
    assert SL.installed_tiles("") == {}
    assert SL.installed_tiles(str(tmp_path / "missing")) == {}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert SL.installed_tiles(str(empty)) == {}


# ---------------------------------------------------------------------------
# overlay link
# ---------------------------------------------------------------------------
def test_overlay_install_and_uninstall(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    overlays = tmp_path / "yOrtho4XP_Overlays"
    scenery.mkdir()
    overlays.mkdir()
    (overlays / "o.txt").write_text("o")

    SL.install_overlay_link(str(overlays), str(scenery))
    link = os.path.join(str(scenery), "yOrtho4XP_Overlays")
    assert os.path.islink(link)
    assert os.path.realpath(link) == os.path.realpath(str(overlays))
    # Idempotent.
    SL.install_overlay_link(str(overlays), str(scenery))

    SL.uninstall_overlay_link(str(overlays), str(scenery))
    assert not os.path.lexists(link)
    assert os.path.isfile(os.path.join(str(overlays), "o.txt"))  # untouched


def test_overlay_conflict_raises(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    overlays = tmp_path / "yOrtho4XP_Overlays"
    scenery.mkdir()
    overlays.mkdir()
    os.mkdir(os.path.join(str(scenery), "yOrtho4XP_Overlays"))
    with pytest.raises(ValueError):
        SL.install_overlay_link(str(overlays), str(scenery))


# ---------------------------------------------------------------------------
# paths with spaces (regression: old os.system quoting)
# ---------------------------------------------------------------------------
def test_paths_with_spaces(tmp_path):
    scenery = tmp_path / "My Custom Scenery"
    build = tmp_path / "My Tiles" / "zOrtho4XP_+48-006"
    scenery.mkdir(parents=True)
    build.mkdir(parents=True)
    SL.install(LAT, LON, str(build), str(scenery))
    assert SL.link_status(LAT, LON, str(build), str(scenery)) is LinkStatus.INSTALLED
    SL.uninstall(LAT, LON, str(build), str(scenery))
    assert SL.link_status(LAT, LON, str(build), str(scenery)) is LinkStatus.NOT_INSTALLED

# ---------------------------------------------------------------------------
# PHYSICAL: the tile's build dir itself lives inside Custom Scenery
# ---------------------------------------------------------------------------
def test_status_physical_build_dir_inside_scenery(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    scenery.mkdir()
    build = scenery / "zOrtho4XP_+48-006"
    build.mkdir()
    assert (
        SL.link_status(LAT, LON, str(build), str(scenery))
        is LinkStatus.PHYSICAL
    )


def test_install_is_noop_on_physical(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    scenery.mkdir()
    build = scenery / "zOrtho4XP_+48-006"
    build.mkdir()
    SL.install(LAT, LON, str(build), str(scenery))  # must not raise
    # Still the real directory — no link was created anywhere.
    assert not SL._is_link(str(build))


def test_uninstall_refuses_physical_and_data_survives(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    scenery.mkdir()
    build = scenery / "zOrtho4XP_+48-006"
    (build / "Earth nav data").mkdir(parents=True)
    (build / "Earth nav data" / "data.dsf").write_text("payload")
    with pytest.raises(ValueError):
        SL.uninstall(LAT, LON, str(build), str(scenery))
    assert (build / "Earth nav data" / "data.dsf").read_text() == "payload"


def test_installed_tiles_includes_plain_tile_dirs(tmp_path):
    scenery = tmp_path / "Custom Scenery"
    scenery.mkdir()
    # Physically installed tile (plain directory).
    plain = scenery / "zOrtho4XP_+36-087"
    plain.mkdir()
    # Linked tile.
    target = tmp_path / "Tiles" / "zOrtho4XP_+48-006"
    target.mkdir(parents=True)
    SL.install(48, -6, str(target), str(scenery))
    # A plain FILE squatting on a tile name must be skipped.
    (scenery / "zOrtho4XP_+10+010").write_text("not a dir")

    result = SL.installed_tiles(str(scenery))
    assert set(result) == {(36, -87), (48, -6)}
    assert result[(36, -87)] == os.path.realpath(str(plain))


# ---------------------------------------------------------------------------
# Incremental scan (iter_installed_tiles) — drives the live map progress
# overlay
# ---------------------------------------------------------------------------
def test_iter_installed_matches_installed(dirs, tmp_path):
    scenery, build = dirs
    # An installed symlink, a plain tile directory, a broken link, and a
    # foreign entry — the full acceptance mix.
    SL.install(LAT, LON, build, scenery)
    os.mkdir(os.path.join(scenery, "zOrtho4XP_+50+010"))
    os.symlink(str(tmp_path / "gone"),
               os.path.join(scenery, "zOrtho4XP_+51+011"))
    os.mkdir(os.path.join(scenery, "SomeAirportPack"))

    steps = list(SL.iter_installed_tiles(scenery))
    total_entries = len(os.listdir(scenery))
    assert [d for (d, _t, _k, _p) in steps] == list(
        range(1, total_entries + 1))
    assert all(t == total_entries for (_d, t, _k, _p) in steps)
    streamed = {k: p for (_d, _t, k, p) in steps if k is not None}
    assert streamed == SL.installed_tiles(scenery)
    assert set(streamed) == {(LAT, LON), (50, 10)}


def test_iter_installed_missing_dir_yields_nothing(tmp_path):
    assert list(SL.iter_installed_tiles(str(tmp_path / "absent"))) == []
    assert list(SL.iter_installed_tiles("")) == []
