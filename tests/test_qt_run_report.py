"""Headless tests for the end-of-run console report.

The final console message reports the whole run's wall time and, per
tile, its own build time — or why it failed (the engine names the step
whose build function reported failure).  Single-tile runs collapse to
one line: "Tile <coords> finished in <duration>."

All offscreen (``QT_QPA_PLATFORM=offscreen``), no network, no X-Plane
install — engine events are hand-fed to the window's handlers.
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import O4_Qt_GUI as GUI  # noqa: E402
from o4_engine import events as EV  # noqa: E402
from o4_engine import session as ENGINE_SESSION  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(GUI, "PREFS_FILE", str(tmp_path / "prefs.json"))
    import O4_UI_Utils as UI
    saved_stdout = sys.stdout
    win = GUI.MainWindow()
    win.prefs["output_dir"] = str(tmp_path)
    monkeypatch.setattr(win, "refresh_tiles", lambda: None)
    try:
        yield win
    finally:
        win._building = False
        win.close()
        win.deleteLater()
        UI.engine_session = None
        sys.stdout = saved_stdout


# ---------------------------------------------------------------------
# The report composer (pure function)
# ---------------------------------------------------------------------
def test_single_tile_report_is_one_line_with_its_time():
    (summary, lines) = GUI._compose_run_report(
        [(48, -6)],
        {(48, -6): (True, "", 754.0)},
        {(48, -6): ("done", "", 100)},
        760.0,
        stopped=False,
    )
    assert summary == "Tile +48-006 finished in 12 m 34 s."
    assert lines == []


def test_single_tile_failure_names_the_reason():
    error = "the mesh step failed (see the console log)"
    (summary, lines) = GUI._compose_run_report(
        [(48, -6)],
        {(48, -6): (False, error, 100.0)},
        {(48, -6): ("error", "failed", 40)},
        100.0,
        stopped=False,
    )
    assert summary == "Tile +48-006 failed after 1 m 40 s — %s." % error
    assert lines == []


def test_multi_tile_report_lists_each_tiles_time():
    (summary, lines) = GUI._compose_run_report(
        [(48, -6), (48, -5)],
        {(48, -6): (True, "", 125.0), (48, -5): (True, "", 65.0)},
        {(48, -6): ("done", "", 100), (48, -5): ("done", "", 100)},
        425.0,
        stopped=False,
    )
    assert summary == "Build finished in 7 m 05 s."
    assert lines == [
        "  +48-006: built in 2 m 05 s",
        "  +48-005: built in 1 m 05 s",
    ]


def test_multi_tile_failure_line_carries_the_explanation():
    error = "the imagery/DSF step failed (see the console log)"
    (summary, lines) = GUI._compose_run_report(
        [(48, -6), (48, -5)],
        {(48, -6): (True, "", 125.0), (48, -5): (False, error, 65.0)},
        {(48, -6): ("done", "", 100), (48, -5): ("error", "failed", 60)},
        200.0,
        stopped=False,
    )
    assert summary == "Build finished in 3 m 20 s: 1 ok, 1 failed."
    assert lines[0] == "  +48-006: built in 2 m 05 s"
    assert lines[1] == "  +48-005: failed after 1 m 05 s — %s" % error


def test_stopped_run_reports_unfinished_tiles():
    (summary, lines) = GUI._compose_run_report(
        [(48, -6), (48, -5)],
        {(48, -6): (True, "", 125.0)},
        {(48, -6): ("done", "", 100),
         (48, -5): ("queued", "stopped", 0)},
        150.0,
        stopped=True,
    )
    assert summary == "Build stopped after 2 m 30 s: 1 done, 0 failed."
    assert lines == [
        "  +48-006: built in 2 m 05 s",
        "  +48-005: stopped before finishing",
    ]


# ---------------------------------------------------------------------
# The engine's failure explanation
# ---------------------------------------------------------------------
def test_failed_step_error_text_names_the_step():
    assert (ENGINE_SESSION.failed_steps_error_text(["mesh"])
            == "the mesh step failed (see the console log)")
    assert (ENGINE_SESSION.failed_steps_error_text(["vector", "imagery"])
            == "the vector data and imagery/DSF steps failed "
               "(see the console log)")


# ---------------------------------------------------------------------
# Wiring: events -> report
# ---------------------------------------------------------------------
def test_build_done_records_the_tiles_own_wall_time(window):
    window._tile_started_at[(48, -6)] = time.time() - 130.0
    window._on_build_done(EV.BuildDone(lat=48, lon=-6, ok=True))
    (ok, error, seconds) = window._tile_results[(48, -6)]
    assert ok is True
    assert 125.0 < seconds < 140.0


def test_step_progress_starts_the_tile_stopwatch_once(window):
    event = EV.StepProgress(lat=48, lon=-6, step_key="vector",
                            label="vector data", percent=0.0)
    window._on_step_progress(event)
    first = window._tile_started_at[(48, -6)]
    window._on_step_progress(EV.StepProgress(
        lat=48, lon=-6, step_key="mesh", label="triangulating",
        percent=0.0))
    assert window._tile_started_at[(48, -6)] == first


def test_run_done_puts_the_summary_in_the_status_bar(window):
    window._building = True
    window._build_t0 = time.time() - 200.0
    window._progress_states = {
        (48, -6): ("done", "", 100),
        (48, -5): ("error", "failed", 60),
    }
    window._tile_results = {
        (48, -6): (True, "", 125.0),
        (48, -5): (False, "the mesh step failed (see the console log)",
                   65.0),
    }
    window._on_run_done(EV.RunDone(done_count=1, error_count=1))
    message = window.statusBar().currentMessage()
    assert message.startswith("Build finished in 3 m 2")
    assert "1 ok, 1 failed" in message
