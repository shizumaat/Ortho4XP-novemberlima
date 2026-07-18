"""Headless tests for the three-box right panel (Selection / Build /
Activity) and for queueing builds into a run in progress.

The map stays LIVE during builds (no view-only lock): tiles can be
selected, reviewed, and queued into the running build; the Build box
keeps its options visible while the Activity box shows per-tile
progress.  All offscreen (``QT_QPA_PLATFORM=offscreen``), no network,
no X-Plane install — the engine session's ``enqueue_build`` is
monkeypatched so no real build starts.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import O4_Qt_GUI as GUI  # noqa: E402
from o4_engine import events as EV  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    # Isolate prefs BEFORE construction (MainWindow loads them in
    # __init__ and closeEvent SAVES them).
    monkeypatch.setattr(GUI, "PREFS_FILE", str(tmp_path / "prefs.json"))
    import O4_UI_Utils as UI
    saved_stdout = sys.stdout
    win = GUI.MainWindow()
    win.prefs["output_dir"] = str(tmp_path)
    # Headless runs skip provider initialization; seed a stand-in code.
    win.imagery_combo.addItems(["TEST_PROVIDER"])
    win.imagery_combo.setCurrentText("TEST_PROVIDER")
    win.zl_combo.setCurrentText("16")
    monkeypatch.setattr(win, "refresh_tiles", lambda: None)
    try:
        yield win
    finally:
        # Tests leave the window mid-"build"; closeEvent would pop the
        # modal "stop and quit?" question, which blocks forever
        # offscreen.  The stub run has nothing real to stop.
        win._building = False
        win.close()
        win.deleteLater()
        UI.engine_session = None
        sys.stdout = saved_stdout


@pytest.fixture
def enqueue_calls(window, monkeypatch):
    """Capture enqueue_build invocations; every call is accepted."""
    calls = []

    def fake_enqueue_build(tiles, **kwargs):
        calls.append((list(tiles), kwargs))
        return True

    monkeypatch.setattr(window._session, "enqueue_build",
                        fake_enqueue_build)
    return calls


# ---------------------------------------------------------------------
# Panel structure
# ---------------------------------------------------------------------
def test_panel_has_selection_build_and_activity_boxes(window):
    assert window.info_group.title() == "Selection"
    assert window.activity_group.title() == "Activity"
    # Idle: the Activity box hides; the Build options are present.
    # (isHidden: the window itself is never shown in offscreen tests,
    # so isVisible() would be False regardless.)
    assert window.activity_group.isHidden()
    assert window.build_btn is not None
    assert window.stop_btn is not None


def test_map_is_never_locked_by_a_build(window, enqueue_calls):
    window.map.set_selection({(48, -6)})
    window.start_build()
    assert window._building is True
    # The map keeps taking selection clicks during the run.
    assert window.map._locked is False
    window.map.set_selection({(50, 10)})
    assert window.map.selection() == {(50, 10)}


# ---------------------------------------------------------------------
# Starting and queueing
# ---------------------------------------------------------------------
def test_start_build_shows_activity_and_keeps_build_options(window,
                                                            enqueue_calls):
    window.map.set_selection({(48, -6), (49, -6)})
    window.start_build()
    assert window._building is True
    assert len(enqueue_calls) == 1
    assert sorted(enqueue_calls[0][0]) == [(48, -6), (49, -6)]
    assert enqueue_calls[0][1]["provider"] == "TEST_PROVIDER"
    assert enqueue_calls[0][1]["zoomlevel"] == 16
    assert not window.activity_group.isHidden()
    # The Build box did not morph away: its options stay actionable.
    assert window.build_btn.isEnabled()
    assert window.chk_vector.isEnabled()
    assert set(window._tile_rows) == {(48, -6), (49, -6)}


def test_second_build_click_queues_into_the_running_run(window,
                                                        enqueue_calls):
    window.map.set_selection({(48, -6)})
    window.start_build()
    first_rows = dict(window._tile_rows)

    window.map.set_selection({(50, 10), (51, 10)})
    window.start_build()

    assert len(enqueue_calls) == 2
    assert sorted(enqueue_calls[1][0]) == [(50, 10), (51, 10)]
    # Existing rows survive; the new tiles append to the Activity list.
    assert set(window._tile_rows) == {(48, -6), (50, 10), (51, 10)}
    assert window._tile_rows[(48, -6)] == first_rows[(48, -6)]
    assert window._ntiles == 3
    # The queued tiles join the map progress overlay as "queued".
    assert window._progress_states[(50, 10)][0] == "queued"
    assert "Queued 2 tiles" in window.statusBar().currentMessage()


def test_queue_skips_tiles_already_in_the_run(window, enqueue_calls):
    window.map.set_selection({(48, -6)})
    window.start_build()
    window.start_build()  # same selection again: nothing to queue
    assert len(enqueue_calls) == 1
    assert "already building or queued" in (
        window.statusBar().currentMessage())


def test_build_button_relabels_while_building(window, enqueue_calls):
    window.map.set_selection({(48, -6)})
    assert "Build" in window.build_btn.text()
    window.start_build()
    window.map.set_selection({(50, 10)})
    assert "Queue" in window.build_btn.text()


def test_tile_in_active_run_gates_only_live_tiles(window, enqueue_calls):
    window.map.set_selection({(48, -6)})
    window.start_build()
    assert window._tile_in_active_run((48, -6)) is True
    assert window._tile_in_active_run((50, 10)) is False
    # A terminal state releases the tile.
    window._on_tile_state(EV.TileState(lat=48, lon=-6, state="done",
                                       percent=100.0))
    assert window._tile_in_active_run((48, -6)) is False


def test_requeue_of_finished_tile_resets_its_row(window, enqueue_calls):
    window.map.set_selection({(48, -6), (49, -6)})
    window.start_build()
    window._on_tile_state(EV.TileState(lat=48, lon=-6, state="done",
                                       percent=100.0))
    bar, status, _row, cancel = window._tile_rows[(48, -6)]
    assert status.text() == "done ✓"
    # Re-queue the finished tile while the run continues.
    window.map.set_selection({(48, -6)})
    window.start_build()
    assert len(enqueue_calls) == 2
    assert status.text() == "queued"
    assert bar.value() == 0
    assert cancel.isEnabled()


def test_activity_box_absorbs_all_free_height_while_visible(window,
                                                            enqueue_calls):
    """While the Activity box shows, the panel's idle bottom spacer is
    zeroed so the box grows 1:1 with the window; hiding it restores the
    spacer (otherwise the spacer keeps a share of every resize and the
    box appears to stop at a maximum height)."""
    layout = window._panel_layout
    spacer_index = window._panel_spacer_index
    assert layout.stretch(spacer_index) == 1  # idle: spacer top-aligns
    window.map.set_selection({(48, -6)})
    window.start_build()
    assert layout.stretch(spacer_index) == 0
    window._set_activity_box_visible(False)
    assert layout.stretch(spacer_index) == 1


def test_refused_enqueue_reports_and_keeps_state(window, monkeypatch):
    monkeypatch.setattr(window._session, "enqueue_build",
                        lambda tiles, **kwargs: False)
    window.map.set_selection({(48, -6)})
    window.start_build()
    # The fresh run could not start: the window returns to idle.
    assert window._building is False
    assert window.activity_group.isHidden()
