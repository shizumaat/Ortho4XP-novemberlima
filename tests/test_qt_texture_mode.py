"""Headless tests for the build-area texture-mode selector (Work package 4).

Verifies the frozen §3.2 contract of ``docs/specs/texture-mode-spec.md``:
- a ``Textures:`` label + combo box live in the build area *after* the
  existing build checkboxes;
- the combo shows the three labels in order and defaults to ``Full Ortho``;
- selecting ``Default X-Plane`` round-trips ``texture_mode="default_xplane"``
  into the active tile's config file and reads back on reload.

All offscreen (``QT_QPA_PLATFORM=offscreen``), no network, no X-Plane install.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import O4_Qt_GUI as GUI  # noqa: E402
import O4_Settings_Model as SM  # noqa: E402


def test_modules_resolve_to_this_worktree():
    """Guard against sys.path/conftest leakage from another checkout."""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for module in (GUI, SM):
        assert os.path.abspath(module.__file__).startswith(here), (
            "%s resolved to %s, not this worktree" % (module, module.__file__)
        )


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    # Isolate the prefs file BEFORE construction: MainWindow loads the
    # prefs in __init__ and closeEvent SAVES them — without this patch,
    # closing a test window clobbers the user's real .qt_prefs.json with
    # the pytest temp output_dir (which is exactly what once broke the
    # tile scan of a real install).
    monkeypatch.setattr(GUI, "PREFS_FILE", str(tmp_path / "prefs.json"))
    saved_stdout = sys.stdout
    win = GUI.MainWindow()
    # Route all per-tile config reads/writes into the isolated temp dir.
    win.prefs["output_dir"] = str(tmp_path)
    try:
        yield win
    finally:
        win.close()
        win.deleteLater()
        sys.stdout = saved_stdout


def _options_layout(window):
    """The QVBoxLayout hosting the build checkboxes + texture row + button."""
    return window.chk_skip_built.parentWidget().layout()


def test_texture_combo_is_after_checkboxes(window):
    layout = _options_layout(window)
    combo_index = layout.indexOf(window.texture_row)
    button_index = layout.indexOf(window.build_btn)
    for chk in (
        window.chk_vector,
        window.chk_imagery,
        window.chk_overlays,
        window.chk_skip_built,
    ):
        assert layout.indexOf(chk) < combo_index, (
            "texture selector must sit after every build checkbox"
        )
    assert combo_index < button_index, "texture selector must precede Build"
    assert window.texture_label.text() == "Textures:"


def test_combo_shows_three_labels_in_order(window):
    combo = window.texture_combo
    assert combo.count() == 3
    assert [combo.itemText(i) for i in range(3)] == [
        "Full Ortho", "Airport Ortho", "Default X-Plane",
    ]
    assert [combo.itemData(i) for i in range(3)] == [
        "full_ortho", "airport_ortho", "default_xplane",
    ]


def test_defaults_to_full_ortho(window):
    window.map.set_active(48, -6)
    assert window.texture_combo.currentText() == "Full Ortho"
    assert window.texture_combo.currentData() == "full_ortho"


def test_selecting_default_xplane_round_trips(window, tmp_path):
    lat, lon = 48, -6
    window.map.set_active(lat, lon)

    # Select "Default X-Plane" the way a user would.
    index = window.texture_combo.findData("default_xplane")
    assert index >= 0
    window.texture_combo.setCurrentIndex(index)

    # Persisted into the tile config file.
    raw = SM.read_tile_raw(lat, lon, window.output_dir())
    assert raw is not None, "selecting a mode must create the tile config"
    assert raw.get("texture_mode") == "default_xplane"

    # Reload: knock the combo off, then re-read from disk.
    window.texture_combo.blockSignals(True)
    window.texture_combo.setCurrentIndex(0)
    window.texture_combo.blockSignals(False)
    window._refresh_texture_mode((lat, lon))
    assert window.texture_combo.currentData() == "default_xplane"


def test_no_active_tile_write_is_noop(window):
    # With no active tile the change handler must stay silent (no crash).
    original = window.map.active_tile
    window.map.active_tile = lambda: None
    try:
        window._texture_mode_changed(2)  # must not raise
    finally:
        window.map.active_tile = original
