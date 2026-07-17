"""Data-root resolution for the packaged app (O4_File_Names).

The packaged (PyInstaller-frozen) app keeps all writable data — downloads,
caches, built tiles, config — under a user-chosen "data root", while
read-only resources stay bundled. Source checkouts keep today's behavior:
everything resolves to the checkout directory. Selecting an existing
Ortho4XP folder as the data root must adopt its contents untouched.
"""
import importlib
import os

import pytest

import O4_File_Names as FNAMES


@pytest.fixture(autouse=True)
def restore_file_names_module():
    """Every test may mutate module-level path state; reload to restore."""
    yield
    importlib.reload(FNAMES)


def test_source_run_uses_checkout_directory():
    assert FNAMES.current_data_root() == os.path.abspath(".")
    assert FNAMES.Tile_dir == os.path.join(os.path.abspath("."), "Tiles")
    assert FNAMES.data_path("Ortho4XP.cfg") == os.path.join(
        os.path.abspath("."), "Ortho4XP.cfg"
    )


def test_source_run_data_path_follows_working_directory(tmp_path, monkeypatch):
    """Legacy behavior kept: in a source run, call-time paths follow the
    current working directory (several tests and CLI flows chdir first)."""
    monkeypatch.chdir(tmp_path)
    assert FNAMES.data_path("Ortho4XP.cfg") == str(tmp_path / "Ortho4XP.cfg")


def test_environment_variable_overrides_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("ORTHO4XP_DATA_ROOT", str(tmp_path))
    assert FNAMES.resolve_data_root() == str(tmp_path)


def test_set_data_root_repoints_writable_directories_only(tmp_path):
    provider_dir_before = FNAMES.Provider_dir
    FNAMES.set_data_root(str(tmp_path))
    assert FNAMES.Tile_dir == str(tmp_path / "Tiles")
    assert FNAMES.Imagery_dir == str(tmp_path / "Orthophotos")
    assert FNAMES.Elevation_dir == str(tmp_path / "Elevation_data")
    assert FNAMES.Auto_extent_dir == str(tmp_path / "Extents" / "Auto")
    # Read-only bundled resources must not move.
    assert FNAMES.Provider_dir == provider_dir_before


def test_data_root_pointer_round_trip(tmp_path, monkeypatch):
    pointer = tmp_path / "config" / "data_root.txt"
    monkeypatch.setattr(FNAMES, "data_root_pointer_file", str(pointer))
    assert FNAMES.read_data_root_pointer() is None
    FNAMES.write_data_root_pointer(str(tmp_path / "MyOrtho4XP"))
    assert FNAMES.read_data_root_pointer() == str(tmp_path / "MyOrtho4XP")


def test_frozen_app_resolves_pointer(tmp_path, monkeypatch):
    pointer = tmp_path / "data_root.txt"
    monkeypatch.setattr(FNAMES, "data_root_pointer_file", str(pointer))
    monkeypatch.setattr(FNAMES, "is_frozen_app", lambda: True)
    monkeypatch.delenv("ORTHO4XP_DATA_ROOT", raising=False)
    FNAMES.write_data_root_pointer(str(tmp_path / "chosen"))
    assert FNAMES.resolve_data_root() == str(tmp_path / "chosen")


def test_seed_shipped_patches_copies_into_empty_data_root(
    tmp_path, monkeypatch
):
    shipped = tmp_path / "bundle" / "Patches"
    (shipped / "+30+030").mkdir(parents=True)
    (shipped / "+30+030" / "patch.txt").write_text("shipped")
    monkeypatch.setattr(
        FNAMES, "resource_path", lambda rel: str(tmp_path / "bundle" / rel)
    )
    FNAMES.set_data_root(str(tmp_path / "data"))
    FNAMES.seed_shipped_patches()
    assert (
        tmp_path / "data" / "Patches" / "+30+030" / "patch.txt"
    ).read_text() == "shipped"


def test_seed_shipped_patches_never_touches_adopted_folder(
    tmp_path, monkeypatch
):
    """Selecting an existing Ortho4XP folder adopts it as-is: the user's own
    Patches folder must survive byte-for-byte, gaining nothing."""
    shipped = tmp_path / "bundle" / "Patches"
    shipped.mkdir(parents=True)
    (shipped / "shipped_only.txt").write_text("shipped")
    existing = tmp_path / "data" / "Patches"
    existing.mkdir(parents=True)
    (existing / "user_patch.txt").write_text("mine")
    monkeypatch.setattr(
        FNAMES, "resource_path", lambda rel: str(tmp_path / "bundle" / rel)
    )
    FNAMES.set_data_root(str(tmp_path / "data"))
    FNAMES.seed_shipped_patches()
    assert (existing / "user_patch.txt").read_text() == "mine"
    assert sorted(os.listdir(existing)) == ["user_patch.txt"]


def test_seed_shipped_patches_is_noop_in_source_checkout():
    """In a source run, shipped and writable Patches are the same folder —
    seeding must do nothing rather than copy a tree onto itself."""
    before = sorted(os.listdir(FNAMES.Patch_dir))
    FNAMES.seed_shipped_patches()
    assert sorted(os.listdir(FNAMES.Patch_dir)) == before
