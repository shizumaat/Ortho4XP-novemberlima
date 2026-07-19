"""Headless tests for toolbar Imagery / Build ZL sync with the selection.

When tiles are selected on the map, the imagery-source combo and the Build
ZL combo must reflect the ``default_website`` / ``default_zl`` recorded in
the selected tiles' per-tile configs.  When the selected tiles disagree,
both combos unresolve to the "--" placeholder (``currentIndex() == -1``)
and ``start_build`` refuses to run until the user picks a value.  Tiles
with no recorded value impose nothing.

All offscreen (``QT_QPA_PLATFORM=offscreen``), no network, no X-Plane
install.
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
    # prefs in __init__ and closeEvent SAVES them.
    monkeypatch.setattr(GUI, "PREFS_FILE", str(tmp_path / "prefs.json"))
    saved_stdout = sys.stdout
    win = GUI.MainWindow()
    # Route all per-tile config reads/writes into the isolated temp dir.
    win.prefs["output_dir"] = str(tmp_path)
    # Headless runs skip the entry point's initialize_providers_dict(),
    # leaving the imagery combo empty; seed two stand-in codes.
    win.imagery_combo.addItems(["TEST_PROVIDER_A", "TEST_PROVIDER_B"])
    # Startup may have synced the combos from whatever tile the default
    # prefs activate; pin a known state for the assertions below.
    win.imagery_combo.setCurrentIndex(0)
    win.zl_combo.setCurrentText("16")
    try:
        yield win
    finally:
        win.close()
        win.deleteLater()
        sys.stdout = saved_stdout


def _write_tile_cfg(window, lat, lon, website=None, zoomlevel=None):
    """Seed a per-tile config file with build provenance keys."""
    path = SM._tile_cfg_path(lat, lon, window.output_dir())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    if website is not None:
        lines.append("default_website=%s" % website)
    if zoomlevel is not None:
        lines.append("default_zl=%s" % zoomlevel)
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")


def _two_provider_codes(window):
    combo = window.imagery_combo
    codes = [combo.itemText(i) for i in range(combo.count())]
    assert len(codes) >= 2, "need at least two GUI providers for the test"
    return codes[0], codes[1]


def test_single_tile_config_drives_both_combos(window):
    code, _ = _two_provider_codes(window)
    _write_tile_cfg(window, 48, -6, website=code, zoomlevel=17)
    window.map.set_selection({(48, -6)})
    assert window.imagery_combo.currentText() == code
    assert window.zl_combo.currentText() == "17"


def test_agreeing_tiles_select_the_shared_value(window):
    code, _ = _two_provider_codes(window)
    _write_tile_cfg(window, 48, -6, website=code, zoomlevel=18)
    _write_tile_cfg(window, 48, -5, website=code, zoomlevel=18)
    window.map.set_selection({(48, -6), (48, -5)})
    assert window.imagery_combo.currentText() == code
    assert window.zl_combo.currentText() == "18"


def test_disagreeing_tiles_unresolve_and_block_build(window):
    first, second = _two_provider_codes(window)
    _write_tile_cfg(window, 48, -6, website=first, zoomlevel=16)
    _write_tile_cfg(window, 48, -5, website=second, zoomlevel=17)
    window.map.set_selection({(48, -6), (48, -5)})
    assert window.imagery_combo.currentIndex() == -1
    assert window.zl_combo.currentIndex() == -1

    window.start_build()  # must refuse: unresolved toolbar combos
    assert window._building is False
    assert "disagree" in window.statusBar().currentMessage()


def test_unconfigured_tiles_leave_combos_alone(window):
    before_imagery = window.imagery_combo.currentText()
    before_zoomlevel = window.zl_combo.currentText()
    window.map.set_selection({(41, 12), (41, 13)})
    assert window.imagery_combo.currentText() == before_imagery
    assert window.zl_combo.currentText() == before_zoomlevel


def test_unconfigured_tile_does_not_dilute_agreement(window):
    _, code = _two_provider_codes(window)
    _write_tile_cfg(window, 48, -6, website=code, zoomlevel=12)
    window.map.set_selection({(48, -6), (48, -5)})
    assert window.imagery_combo.currentText() == code
    assert window.zl_combo.currentText() == "12"


def test_unknown_provider_code_unresolves_the_combo(window):
    _write_tile_cfg(window, 48, -6, website="NO_SUCH_PROVIDER", zoomlevel=16)
    window.map.set_selection({(48, -6)})
    assert window.imagery_combo.currentIndex() == -1
    assert window.zl_combo.currentText() == "16"


def test_picking_a_value_resolves_the_block(window):
    first, second = _two_provider_codes(window)
    _write_tile_cfg(window, 48, -6, website=first, zoomlevel=16)
    _write_tile_cfg(window, 48, -5, website=second, zoomlevel=17)
    window.map.set_selection({(48, -6), (48, -5)})
    assert window.imagery_combo.currentIndex() == -1

    # The user picks values in the toolbar: the "--" state clears and the
    # unresolved-combo guard in start_build no longer applies.
    window.imagery_combo.setCurrentText(first)
    window.zl_combo.setCurrentText("17")
    assert window.imagery_combo.currentIndex() >= 0
    assert window.zl_combo.currentIndex() >= 0


def test_user_pick_survives_panel_refresh_on_same_selection(window):
    """Async panel refreshes (scan results streaming in, build
    bookkeeping) re-run the sync on an unchanged selection; they must
    not snap the combos back to the tiles' recorded provenance after
    the user deliberately picked different values."""
    first, second = _two_provider_codes(window)
    _write_tile_cfg(window, 48, -6, website=first, zoomlevel=17)
    window.map.set_selection({(48, -6)})
    assert window.imagery_combo.currentText() == first

    window.imagery_combo.setCurrentText(second)
    window.zl_combo.setCurrentText("16")
    window._active_changed(window.map.active_tile())
    assert window.imagery_combo.currentText() == second
    assert window.zl_combo.currentText() == "16"


def test_reselecting_tiles_resyncs_the_combos(window):
    first, second = _two_provider_codes(window)
    _write_tile_cfg(window, 48, -6, website=first, zoomlevel=17)
    window.map.set_selection({(48, -6)})
    window.imagery_combo.setCurrentText(second)

    # A GENUINE selection change re-arms the sync: dropping and
    # re-taking the selection points the combos back at the config.
    window.map.set_selection(set())
    window.map.set_selection({(48, -6)})
    assert window.imagery_combo.currentText() == first
    assert window.zl_combo.currentText() == "17"


def test_start_build_uses_the_user_picked_values(window, monkeypatch):
    """start_build must hand the engine the values showing in the
    toolbar at click time — its own panel refresh used to reset the
    combos to the tiles' recorded provenance BEFORE they were read,
    silently rebuilding with the old imagery source."""
    first, second = _two_provider_codes(window)
    _write_tile_cfg(window, 48, -6, website=first, zoomlevel=17)
    window.map.set_selection({(48, -6)})
    assert window.imagery_combo.currentText() == first

    window.imagery_combo.setCurrentText(second)
    window.zl_combo.setCurrentText("16")

    calls = []

    def fake_enqueue_build(tiles, **kwargs):
        calls.append((list(tiles), kwargs))
        # "Not started": start_build unwinds its run bookkeeping, so the
        # window closes cleanly without a live build to stop.
        return False

    monkeypatch.setattr(window._session, "enqueue_build",
                        fake_enqueue_build)
    window.start_build()
    assert calls, "start_build never reached enqueue_build"
    kwargs = calls[0][1]
    assert kwargs["provider"] == second
    assert kwargs["zoomlevel"] == 16
