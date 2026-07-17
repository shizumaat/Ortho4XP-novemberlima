"""Headless tests for the per-tile cancel button on the build progress rows
(docs/specs/parallel-tile-builds.md §3.6).

Offscreen (``QT_QPA_PLATFORM=offscreen``), no network, no X-Plane install.
The rows are driven directly through ``MainWindow._setup_progress_page`` and
``MainWindow._on_tile_state`` — the same entry points the engine event
dispatch uses — so the padding, the standard close ("X") button, and the
terminal-state disabling are all asserted without a real build.

The PREFS_FILE monkeypatch BEFORE construction is mandatory (see the note
in ``test_qt_texture_mode``): MainWindow loads prefs in ``__init__`` and
``closeEvent`` SAVES them, so without isolation a test window clobbers the
user's real ``.qt_prefs.json``.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QToolButton  # noqa: E402

import O4_Qt_GUI as GUI  # noqa: E402
from o4_engine import events as EV  # noqa: E402


TILES = [(48, -6), (49, -6)]


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    # Isolate prefs BEFORE construction (mandatory — see module docstring).
    monkeypatch.setattr(GUI, "PREFS_FILE", str(tmp_path / "prefs.json"))
    import O4_UI_Utils as UI
    saved_stdout = sys.stdout
    win = GUI.MainWindow()
    # A tile reaching "done" triggers a rescan; keep the test hermetic.
    monkeypatch.setattr(win, "refresh_tiles", lambda: None)
    try:
        yield win
    finally:
        win.close()
        win.deleteLater()
        UI.engine_session = None
        sys.stdout = saved_stdout


def _row_widgets(window, tile):
    """(bar, status, row, cancel) for a tile in the progress page."""
    return window._tile_rows[tile]


def test_rows_have_padding_and_spacing(window):
    window._setup_progress_page(TILES)
    # 6 px spacing between rows (the rows container).
    assert window._rows_layout.spacing() == 6
    for tile in TILES:
        _bar, _status, row, _cancel = _row_widgets(window, tile)
        margins = row.layout().contentsMargins()
        assert (margins.left(), margins.top(),
                margins.right(), margins.bottom()) == (6, 4, 6, 4)


def test_each_row_has_standard_cancel_button(window):
    window._setup_progress_page(TILES)
    for tile in TILES:
        _bar, _status, _row, cancel = _row_widgets(window, tile)
        assert isinstance(cancel, QToolButton)
        assert cancel.isEnabled()
        assert not cancel.icon().isNull(), (
            "the cancel button must carry the platform close icon")
        assert cancel.toolTip() == "Cancel this tile"


def test_clicking_cancel_calls_session_once_and_disables(window, monkeypatch):
    window._setup_progress_page(TILES)
    calls = []
    monkeypatch.setattr(
        window._session, "cancel_tile",
        lambda lat, lon: calls.append((lat, lon)) or True)

    tile = TILES[0]
    _bar, status, _row, cancel = _row_widgets(window, tile)
    cancel.click()

    assert calls == [tile], "cancel_tile must be called exactly once, once"
    assert cancel.isEnabled() is False
    assert status.text() == "stopping…"


def test_done_state_disables_button(window):
    window._setup_progress_page(TILES)
    tile = TILES[0]
    _bar, _status, _row, cancel = _row_widgets(window, tile)
    assert cancel.isEnabled()
    window._on_tile_state(
        EV.TileState(lat=tile[0], lon=tile[1], state="done", percent=100.0))
    assert cancel.isEnabled() is False


def test_error_state_disables_button(window):
    window._setup_progress_page(TILES)
    tile = TILES[0]
    _bar, _status, _row, cancel = _row_widgets(window, tile)
    assert cancel.isEnabled()
    window._on_tile_state(
        EV.TileState(lat=tile[0], lon=tile[1], state="error", label="failed"))
    assert cancel.isEnabled() is False


def test_stopped_label_disables_button(window):
    window._setup_progress_page(TILES)
    tile = TILES[0]
    _bar, _status, _row, cancel = _row_widgets(window, tile)
    assert cancel.isEnabled()
    window._on_tile_state(
        EV.TileState(lat=tile[0], lon=tile[1], state="queued",
                     label="stopped"))
    assert cancel.isEnabled() is False
