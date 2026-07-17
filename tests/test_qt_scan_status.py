"""Headless tests for the map's scenery-scan status overlay.

The overlay (label + slim progress bar, bottom-right of the map viewport)
shows that installed scenery is being read while tiles stream onto the map.
Contract:
- hidden until a scan reports progress, hidden again when the scan ends
- determinate when the entry total is known, busy (range 0,0) when not
- pinned to the bottom-right corner across resizes and pans (viewport
  children get dragged by QAbstractScrollArea scrolling — the same trap
  the legend re-pin covers)
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import O4_Qt_Map as QTMAP  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def view(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        QTMAP, "livemap_cache_dir", lambda: str(tmp_path / "livemap")
    )
    v = QTMAP.MapView()
    v.resize(800, 600)
    v._start_workers = lambda: None
    v._update_timer.stop()
    yield v
    v.deleteLater()


def test_hidden_until_scan_starts(view):
    assert not view.scan_status.isVisible()


def test_set_scan_status_shows_determinate_progress(view):
    view.show()
    view.set_scan_status("Reading installed scenery…", 120, 4196)
    assert view.scan_status.isVisible()
    assert view._scan_status_bar.maximum() == 4196
    assert view._scan_status_bar.value() == 120
    assert "installed scenery" in view._scan_status_label.text()


def test_unknown_total_goes_busy(view):
    view.show()
    view.set_scan_status("Reading built tiles…", 0, 0)
    # Qt busy indicator == range (0, 0).
    assert view._scan_status_bar.minimum() == 0
    assert view._scan_status_bar.maximum() == 0


def test_clear_scan_status_hides(view):
    view.show()
    view.set_scan_status("Reading built tiles…", 5, 10)
    assert view.scan_status.isVisible()
    view.clear_scan_status()
    assert not view.scan_status.isVisible()


def _bottom_right_gap(view):
    geo = view.scan_status.geometry()
    return (
        view.viewport().width() - geo.right() - 1,
        view.viewport().height() - geo.bottom() - 1,
    )


def test_pinned_bottom_right_across_resize_and_pan(view):
    view.show()
    view.set_scan_status("Reading installed scenery…", 1, 2)
    assert _bottom_right_gap(view) == (10, 10)
    view.resize(600, 400)
    QApplication.processEvents()
    assert _bottom_right_gap(view) == (10, 10)
    # A pan scrolls the viewport, which drags child widgets — the overlay
    # must be re-pinned by scrollContentsBy.
    view.scrollContentsBy(37, -18)
    assert _bottom_right_gap(view) == (10, 10)


def test_done_clamped_to_total(view):
    view.show()
    view.set_scan_status("Reading built tiles…", 99, 10)
    assert view._scan_status_bar.value() == 10
