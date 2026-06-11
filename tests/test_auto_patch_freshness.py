"""Auto-patch freshness — apt.dat provenance stamp + rebuild-skip logic.

Covers:
* PavementLayout.to_osm stamps ``o4_apt_dat`` / ``o4_apt_dat_mtime``
  on the <osm> root; ``read_patch_source`` round-trips them.
* driver._auto_patch_is_current: reuse only when the apt.dat that
  would be selected today is the same file the patch was built from
  AND its mtime is unchanged.
"""
import os
from pathlib import Path

import pytest

import auto_patch.osm_load as osm_load
from auto_patch.driver import _auto_patch_is_current
from auto_patch.layout import PavementLayout, read_patch_source


def _make_apt_dat(tmp_path: Path, name: str = "apt.dat") -> Path:
    p = tmp_path / name
    p.write_text("I\n1000 Version\n1 100 0 0 KFAKE Fake Airport\n")
    return p


def _emit_patch(tmp_path: Path, apt_dat: Path | None) -> Path:
    layout = PavementLayout(
        icao="KFAKE", anchor=(40.0, -100.0),
        apt_dat_path=str(apt_dat) if apt_dat else None)
    patch = tmp_path / "KFAKE_auto.patch.osm"
    layout.to_osm(str(patch))
    return patch


# ──────────────────────────────────────────────────────────────────────
# Provenance stamp round-trip
# ──────────────────────────────────────────────────────────────────────
def test_to_osm_stamps_apt_dat_provenance(tmp_path):
    apt = _make_apt_dat(tmp_path)
    patch = _emit_patch(tmp_path, apt)

    meta = read_patch_source(str(patch))
    assert meta is not None
    assert meta["apt_dat"] == str(apt)
    assert meta["apt_dat_mtime"] == pytest.approx(
        os.path.getmtime(apt), abs=1e-6)


def test_to_osm_stamp_survives_spaces_and_quotes_in_path(tmp_path):
    pack = tmp_path / "Pilot's Custom Scenery"
    pack.mkdir()
    apt = _make_apt_dat(pack)
    patch = _emit_patch(tmp_path, apt)

    # The attribute value must stay quote-free (percent-encoded) so
    # the single-quote-delimited OSM line parsers are unaffected.
    header = patch.read_text().splitlines()[1]
    assert "<osm " in header
    assert header.count("'") % 2 == 0
    meta = read_patch_source(str(patch))
    assert meta["apt_dat"] == str(apt)


def test_read_patch_source_none_without_stamp(tmp_path):
    patch = _emit_patch(tmp_path, None)
    assert read_patch_source(str(patch)) is None
    assert read_patch_source(str(tmp_path / "missing.osm")) is None


# ──────────────────────────────────────────────────────────────────────
# _auto_patch_is_current
# ──────────────────────────────────────────────────────────────────────
@pytest.fixture
def fresh_env(monkeypatch):
    monkeypatch.delenv("O4_AUTO_PATCH_REBUILD", raising=False)


def _select(monkeypatch, path):
    monkeypatch.setattr(
        osm_load, "_pick_best_apt_dat_against_osm",
        lambda xp_root, icao: str(path) if path else None)


def test_current_when_same_apt_and_mtime(tmp_path, monkeypatch, fresh_env):
    apt = _make_apt_dat(tmp_path)
    patch = _emit_patch(tmp_path, apt)
    _select(monkeypatch, apt)
    assert _auto_patch_is_current(str(patch), "xp_root", "KFAKE")


def test_stale_when_apt_dat_touched(tmp_path, monkeypatch, fresh_env):
    apt = _make_apt_dat(tmp_path)
    patch = _emit_patch(tmp_path, apt)
    _select(monkeypatch, apt)
    st = os.stat(apt)
    os.utime(apt, (st.st_atime, st.st_mtime + 10.0))
    assert not _auto_patch_is_current(str(patch), "xp_root", "KFAKE")


def test_stale_when_different_apt_selected(tmp_path, monkeypatch, fresh_env):
    apt = _make_apt_dat(tmp_path)
    patch = _emit_patch(tmp_path, apt)
    newer_pack = tmp_path / "NewPack"
    newer_pack.mkdir()
    other = _make_apt_dat(newer_pack)
    _select(monkeypatch, other)
    assert not _auto_patch_is_current(str(patch), "xp_root", "KFAKE")


def test_stale_when_patch_missing_or_unstamped(
        tmp_path, monkeypatch, fresh_env):
    apt = _make_apt_dat(tmp_path)
    _select(monkeypatch, apt)
    missing = tmp_path / "KFAKE_auto.patch.osm"
    assert not _auto_patch_is_current(str(missing), "xp_root", "KFAKE")
    # Pre-stamp patch (no o4_apt_dat attribute) rebuilds once.
    unstamped = _emit_patch(tmp_path, None)
    assert not _auto_patch_is_current(str(unstamped), "xp_root", "KFAKE")


def test_stale_when_no_apt_selectable(tmp_path, monkeypatch, fresh_env):
    apt = _make_apt_dat(tmp_path)
    patch = _emit_patch(tmp_path, apt)
    _select(monkeypatch, None)
    assert not _auto_patch_is_current(str(patch), "xp_root", "KFAKE")


def test_force_rebuild_env_overrides(tmp_path, monkeypatch):
    apt = _make_apt_dat(tmp_path)
    patch = _emit_patch(tmp_path, apt)
    _select(monkeypatch, apt)
    monkeypatch.setenv("O4_AUTO_PATCH_REBUILD", "1")
    assert not _auto_patch_is_current(str(patch), "xp_root", "KFAKE")
