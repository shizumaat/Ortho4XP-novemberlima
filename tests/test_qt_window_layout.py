"""Main-window layout contracts (2026-07-17).

Regressions guarded here:

* the tile-details Elevation / Airport lidar values are capped at two
  wrapped lines with MIDDLE elision (long provider lists used to
  overflow their boxes), and the labels are fixed at two lines tall so
  the form always shows both lines;
* the right panel's content never demands more width than its
  fixed-width scroll viewport (wider content clipped silently — the
  horizontal scrollbar is off — and a width-dependent label height
  crashed via scroll-area relayout oscillation);
* the tile-info form pins AllNonFixedFieldsGrow (macOS's native
  FieldsStayAtSizeHint style collapses Ignored-policy fields to zero
  width — the values vanished on Macs only);
* while a scenery scan streams in, an unknown tile reads
  "(scanning…)" — never a premature "(not built)" — and flips to its
  real info the moment its batch arrives;
* the console drawer defaults to six lines, keeps its size when toggled
  (re-opening restores the height it had), and window growth goes to
  the map, not the console;
* window geometry, console visibility and the splitter split persist
  across launches through the prefs file.

Headless (offscreen platform); the prefs file is monkeypatched BEFORE
MainWindow construction (see qt_tests_prefs_file_clobber).
"""

import os
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
def make_window(qapp, tmp_path, monkeypatch, capsys):
    """Factory building MainWindows that share one prefs file."""
    import O4_Qt_GUI as GUI
    import O4_UI_Utils as UI

    monkeypatch.setattr(GUI, "PREFS_FILE", str(tmp_path / "prefs.json"))
    windows = []

    def build():
        with capsys.disabled():
            original_stdout = sys.stdout
            window = GUI.MainWindow()
            sys.stdout = original_stdout
        windows.append(window)
        return window

    try:
        yield build
    finally:
        for window in windows:
            window.deleteLater()
        UI.engine_session = None


LONG_TEXT = (
    "1/9 arc-second (~3 m) lidar: USGS_3DEP_1M, HRDEM_QUEBEC_1M, "
    "PT_DGT_LIDAR_COASTAL, SWEDEN_LM_1M, DK_SDFE_DTM and 12 more sources"
)


class TestTwoLineElidedLabel:
    def _label(self, qapp, width):
        import O4_Qt_GUI as GUI

        label = GUI.TwoLineElidedLabel()
        # Shown so later resize() delivers resizeEvent synchronously
        # (hidden widgets defer it to show time).
        label.show()
        label.resize(width, 60)
        qapp.processEvents()
        return label

    def test_short_text_untouched(self, qapp):
        label = self._label(qapp, 170)
        label.setText("auto (1 arc-second)")
        assert label.text() == "auto (1 arc-second)"
        assert label.toolTip() == ""

    def test_long_text_elides_middle_and_keeps_two_lines(self, qapp):
        label = self._label(qapp, 170)
        label.setText(LONG_TEXT)
        shown = label.text()
        assert shown != LONG_TEXT
        assert "…" in shown
        # Head and tail both survive (middle elision).
        assert shown.startswith(LONG_TEXT[:8])
        assert shown.endswith(LONG_TEXT[-8:])
        metrics = label.fontMetrics()
        from PySide6.QtCore import Qt

        rect = metrics.boundingRect(
            0, 0, label.contentsRect().width(), 100000,
            Qt.TextWordWrap, shown,
        )
        assert rect.height() <= 2 * metrics.lineSpacing() + 2
        assert label.toolTip() == LONG_TEXT

    def test_re_elides_when_narrowed(self, qapp):
        label = self._label(qapp, 600)
        label.setText(LONG_TEXT)
        wide = label.text()
        label.resize(150, 60)
        assert len(label.text()) < len(wide)

    def test_tile_info_labels_use_the_elided_class(self, make_window):
        import O4_Qt_GUI as GUI

        window = make_window()
        assert isinstance(window.info_elevation, GUI.TwoLineElidedLabel)
        assert isinstance(window.info_airport_lidar, GUI.TwoLineElidedLabel)

    def test_labels_are_fixed_at_two_lines_tall(self, qapp):
        """Layouts must grant BOTH lines — and exactly two, so height
        never depends on width (the scroll-area oscillation guard)."""
        label = self._label(qapp, 170)
        two_lines = 2 * label.fontMetrics().lineSpacing() + 2
        assert label.minimumHeight() == two_lines
        assert label.maximumHeight() == two_lines


class TestPanelFitsItsViewport:
    """The right panel is a fixed-width scroll area with the horizontal
    scrollbar off: content wider than the viewport silently clips, so
    no child may demand more width than the viewport offers."""

    def test_panel_minimum_width_fits(self, qapp, make_window):
        window = make_window()
        window.show()
        qapp.processEvents()
        # Long dynamic values, as after a scan of a lidar-covered tile.
        window.build_summary.setText(
            "12 tiles selected · rough est. 48.0 GB · airport lidar on 12"
        )
        window.info_elevation.setText(LONG_TEXT)
        window.info_airport_lidar.setText(LONG_TEXT)
        qapp.processEvents()
        panel = window.info_group.parentWidget()
        scroll = panel.parentWidget()
        while not hasattr(scroll, "viewport"):
            scroll = scroll.parentWidget()
        assert panel.minimumSizeHint().width() <= scroll.viewport().width()
        assert panel.width() <= scroll.viewport().width()

    def test_info_rows_get_two_lines_in_the_form(self, qapp, make_window):
        window = make_window()
        window.show()
        qapp.processEvents()
        window.info_group.setVisible(True)
        window.info_elevation.setText(LONG_TEXT)
        qapp.processEvents()
        metrics = window.info_elevation.fontMetrics()
        assert window.info_elevation.height() >= 2 * metrics.lineSpacing()
        assert window.info_airport_lidar.height() >= 2 * metrics.lineSpacing()

    def test_info_form_grows_ignored_policy_fields(self, make_window):
        """macOS's native form style (FieldsStayAtSizeHint) gives the
        Ignored-policy elided labels ZERO width — the values vanish.
        The layout must pin the growing policy explicitly."""
        from PySide6.QtWidgets import QFormLayout

        window = make_window()
        layout = window.info_group.layout()
        assert (
            layout.fieldGrowthPolicy()
            == QFormLayout.AllNonFixedFieldsGrow
        )


class TestScanPendingState:
    """While a scenery scan is streaming in, a tile absent from the
    results is merely "not scanned yet": the info panel must say
    "(scanning…)", not flash a wrong "(not built)" verdict."""

    def _scan_done(self, window):
        from o4_engine import events as EV

        window._on_scan_done(EV.ScanDone())

    def test_starts_in_scanning_state(self, qapp, make_window):
        window = make_window()
        window.show()
        qapp.processEvents()
        window._active_changed((36, -87))
        assert "(scanning…)" in window.info_title.text()
        assert "(not built)" not in window.info_title.text()
        assert window.info_provider.text() == "…"

    def test_scan_done_reveals_not_built(self, qapp, make_window):
        window = make_window()
        window.show()
        qapp.processEvents()
        self._scan_done(window)
        window._active_changed((36, -87))
        assert "(not built)" in window.info_title.text()
        assert window.info_provider.text() == "—"

    def test_active_tile_updates_when_its_batch_arrives(
        self, qapp, make_window
    ):
        import inspect

        import O4_Tile_Info as TINFO
        from o4_engine import events as EV

        window = make_window()
        window.show()
        qapp.processEvents()
        window.map.set_active(36, -87)
        window._active_changed((36, -87))
        assert "(scanning…)" in window.info_title.text()
        required = {
            "lat": 36,
            "lon": -87,
            "build_dir": "/nonexistent",
            "dir_name": "zOrtho4XP_+36-087",
            "dsf_present": True,
        }
        signature = inspect.signature(TINFO.TileInfo)
        kwargs = {
            name: required.get(name)
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect.Parameter.empty
        }
        info = TINFO.TileInfo(**kwargs, provider="BI", zl=16)
        window._on_scan_batch(EV.ScanBatch(built={(36, -87): info}))
        assert "(scanning…)" not in window.info_title.text()
        assert window.info_provider.text() == "BI"

    def test_rescan_returns_to_scanning_state(self, qapp, make_window):
        window = make_window()
        window.show()
        qapp.processEvents()
        self._scan_done(window)
        window._session.scan = lambda *args, **kwargs: None
        window.map.set_active(36, -87)
        window.refresh_tiles()
        assert "(scanning…)" in window.info_title.text()


class TestConsoleDrawer:
    def test_defaults_to_six_lines(self, qapp, make_window):
        window = make_window()
        window.show()
        qapp.processEvents()
        assert window.console.isVisible()
        sizes = window.splitter.sizes()
        assert abs(sizes[1] - window._console_default_height()) <= 2

    def test_window_growth_goes_to_the_map(self, qapp, make_window):
        window = make_window()
        window.show()
        qapp.processEvents()
        before = window.splitter.sizes()[1]
        window.resize(window.width(), window.height() + 300)
        qapp.processEvents()
        assert abs(window.splitter.sizes()[1] - before) <= 2

    def test_toggle_restores_previous_height(self, qapp, make_window):
        window = make_window()
        window.show()
        qapp.processEvents()
        total = sum(window.splitter.sizes())
        window._apply_console_height(120)
        assert window.splitter.sizes()[1] == 120
        window.toggle_console()
        assert not window.console.isVisible()
        window.toggle_console()
        qapp.processEvents()
        assert window.console.isVisible()
        assert abs(window.splitter.sizes()[1] - 120) <= 2
        assert total == sum(window.splitter.sizes())


class TestLayoutPersistence:
    def test_round_trip(self, qapp, make_window):
        # Sizes chosen to fit the offscreen platform's 800x600 virtual
        # screen, so restoreGeometry does not clamp them.
        first = make_window()
        first.show()
        qapp.processEvents()
        first.resize(760, 560)
        qapp.processEvents()
        # The layout minimum may override the requested height; what
        # must round-trip is the ACTUAL size.
        first_size = (first.width(), first.height())
        first._apply_console_height(120)
        first.close()
        qapp.processEvents()

        second = make_window()
        second.show()
        qapp.processEvents()
        assert (second.width(), second.height()) == first_size
        assert abs(second.splitter.sizes()[1] - 120) <= 2
        assert second.console.isVisible()

    def test_hidden_console_stays_hidden(self, qapp, make_window):
        first = make_window()
        first.show()
        qapp.processEvents()
        first.set_console_visible(False)
        first.close()
        qapp.processEvents()

        second = make_window()
        second.show()
        qapp.processEvents()
        assert not second.console.isVisible()
        assert second.console_btn.text() == "Console ▾"
