"""The console drawer must receive pipeline prints during a build.

Regression guard for the 2026-07-15 engine-session migration, which
accidentally removed the stdout tee installation along with the retired
signal-adapter block — builds then ran with a silent console.  The
contract: MainWindow construction installs ``_StdoutTee`` on
``sys.stdout``, worker prints land in the console queue, and the drain
timer moves them into the console widget.

Note on capture: pytest reassigns ``sys.stdout`` between test phases, so
the installation check must run at construction time inside
``capsys.disabled()`` — asserting on ``sys.stdout`` later observes
pytest's capture object, not the application's wiring.
"""

import os
import queue
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch, capsys):
    import O4_Qt_GUI as GUI
    import O4_UI_Utils as UI

    monkeypatch.setattr(GUI, "PREFS_FILE", str(tmp_path / "prefs.json"))
    with capsys.disabled():
        original_stdout = sys.stdout
        w = GUI.MainWindow()
        w._tee_installed_at_construction = isinstance(
            sys.stdout, GUI._StdoutTee)
        sys.stdout = original_stdout
    try:
        yield w
    finally:
        w.deleteLater()
        UI.engine_session = None


def test_stdout_tee_installed_at_construction(window):
    assert window._tee_installed_at_construction, (
        "MainWindow must tee sys.stdout into the console queue — a build "
        "with no tee runs with a silent console"
    )


def test_console_queue_drains_into_widget(window):
    assert isinstance(window._console_queue, queue.Queue)
    assert window._console_timer.isActive(), (
        "the drain timer must run, or queued text never reaches the widget"
    )
    marker = "console-regression-marker-4711\n"
    window._console_queue.put(marker)
    window._drain_console()
    assert "console-regression-marker-4711" in window.console.toPlainText()


def test_verbosity_preference_applied(window):
    import O4_UI_Utils as UI

    assert UI.verbosity == int(window.prefs.get("verbosity", 1))
