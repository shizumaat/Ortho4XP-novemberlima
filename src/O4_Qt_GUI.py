"""Main window of the Ortho4XP Qt UI (map-first shell).

Layout: toolbar (search, imagery, build ZL) / live map / context panel on the
right / collapsible console drawer below / status bar.

The build pipeline is untouched: this window talks to it exactly the way the
legacy Tk GUI does — a worker thread runs the step functions, progress arrives
via UI.progress_bar (adapted to Qt signals), cancellation via UI.red_flag,
console output via a stdout tee.
"""

import json
import math
import os
import queue
import sys
import threading
import traceback

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import O4_File_Names as FNAMES
import O4_Imagery_Utils as IMG
import O4_UI_Utils as UI
import O4_Version
import O4_Airport_Index as APT
import O4_Scenery_Links as LINKS
import O4_Tile_Info as TINFO
import O4_Qt_Map as QTMAP
from o4_engine import EngineSession
from o4_engine import events as EV
import O4_Qt_Settings as QTSET
import O4_Qt_Wizard as QTWIZ

PREFS_FILE = FNAMES.data_path(".qt_prefs.json")
AIRPORT_CACHE = FNAMES.data_path(".airport_index.tsv")
MAX_CONSOLE_LINES = 5000

# Build-area texture-mode selector: user-visible label -> tile config value.
# Order is significant (it is the popup-menu item order).
TEXTURE_MODE_CHOICES = (
    ("Full Ortho", "full_ortho"),
    ("Airport Ortho", "airport_ortho"),
    ("Default X-Plane", "default_xplane"),
)


def load_prefs():
    try:
        with open(PREFS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_prefs(prefs):
    try:
        with open(PREFS_FILE, "w") as f:
            json.dump(prefs, f, indent=2)
    except Exception:
        pass


class _StdoutTee:
    """Duplicates pipeline stdout into a queue for the console drawer."""

    def __init__(self, original, line_queue):
        self._original = original
        self._queue = line_queue

    def write(self, text):
        try:
            self._original.write(text)
        except Exception:
            pass
        self._queue.put(text)

    def flush(self):
        try:
            self._original.flush()
        except Exception:
            pass


class _EngineBridge(QObject):
    """Marshals engine-session events onto the GUI thread.

    The session invokes subscriber callbacks on its worker threads;
    cross-thread Signal emission is the one supported hand-off (QTimer
    from a plain worker thread silently never fires)."""

    event = Signal(object)
    size_computed = Signal()


def gui_provider_codes():
    codes = sorted(
        code
        for code in set(IMG.providers_dict)
        if IMG.providers_dict[code].get("in_GUI")
    )
    codes += sorted(set(IMG.combined_providers_dict))
    return [c for c in codes if c not in ("SEA",)]


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ortho4XP " + O4_Version.version)
        self.resize(1180, 780)

        self.prefs = load_prefs()
        self._first_run = not os.path.isfile(PREFS_FILE)
        self._airports = []
        self._built = {}
        self._installed = set()
        self._progress_states = {}
        self._building = False
        self._stop_requested = False

        # --- pipeline adapters -----------------------------------------
        UI.verbosity = int(self.prefs.get("verbosity", 1))
        # Console feed: pipeline prints (worker threads included) are teed
        # into a queue and drained onto the console drawer by a GUI-thread
        # timer.  This stays view-side plumbing even under the engine
        # session — stdout is process-global, and the JSON-lines transport
        # has its own stdout discipline instead.
        self._console_queue = queue.Queue()
        sys.stdout = _StdoutTee(sys.stdout, self._console_queue)
        self._console_timer = QTimer(self)
        self._console_timer.setInterval(120)
        self._console_timer.timeout.connect(self._drain_console)
        self._console_timer.start()
        self._session = EngineSession()
        self._bridge = _EngineBridge()
        self._bridge.event.connect(self._on_engine_event)
        self._session.subscribe(self._bridge.event.emit)
        self._event_handlers = {
            EV.ScanProgress: self._on_scan_progress,
            EV.ScanBatch: self._on_scan_batch,
            EV.ScanDone: self._on_scan_done,
            EV.StepProgress: self._on_step_progress,
            EV.TileState: self._on_tile_state,
            EV.RunEta: self._on_run_eta,
            EV.RunDone: self._on_run_done,
        }
        self._scan_built = {}
        self._scan_installed = set()
        self._last_run_eta = None
        self._build_t0 = None
        self._done_count = 0
        self._ntiles = 0

        self._make_widgets()
        self._make_menus()
        self._apply_prefs(initial=True)

        if self._first_run:
            QTimer.singleShot(200, self.run_wizard)
        QTimer.singleShot(300, self.refresh_tiles)
        QTimer.singleShot(400, self._load_airports_async)

        lat = int(self.prefs.get("last_lat", 48))
        lon = int(self.prefs.get("last_lon", -6))
        self.map.center_on_tile(lat, lon, zoom=7)
        self.map.set_active(lat, lon)

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------
    def _make_widgets(self):
        toolbar = QToolBar()
        toolbar.setMovable(False)
        # Breathing room: keep controls off the window edges and apart.
        toolbar.setStyleSheet(
            "QToolBar { padding: 6px 10px; spacing: 6px; }"
        )
        self.addToolBar(toolbar)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Search airport, city, country — or tile like +48-006"
        )
        self.search_edit.setFixedWidth(320)
        self.search_edit.textEdited.connect(self._update_search_popup)
        self.search_edit.returnPressed.connect(self._search_accept_first)
        toolbar.addWidget(self.search_edit)
        self.search_popup = QListWidget(self)
        self.search_popup.setWindowFlags(Qt.ToolTip)
        self.search_popup.itemClicked.connect(self._search_accept_item)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" Imagery "))
        self.imagery_combo = QComboBox()
        self.imagery_combo.addItems(gui_provider_codes())
        self.imagery_combo.currentTextChanged.connect(self._imagery_changed)
        toolbar.addWidget(self.imagery_combo)
        toolbar.addWidget(QLabel(" Build ZL "))
        self.zl_combo = QComboBox()
        self.zl_combo.addItems([str(z) for z in range(12, 19)])
        toolbar.addWidget(self.zl_combo)
        toolbar.addSeparator()
        self.zones_btn = QPushButton("✏ Zones")
        self.zones_btn.setEnabled(False)
        self.zones_btn.setToolTip(
            "Zone editing arrives in the next iteration — "
            "use the legacy UI (Ortho4XP.py) for zones meanwhile."
        )
        toolbar.addWidget(self.zones_btn)
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        toolbar.addWidget(spacer)
        settings_btn = QPushButton("⚙ Settings")
        settings_btn.clicked.connect(self.open_settings)
        toolbar.addWidget(settings_btn)

        # Map + right panel
        self.map = QTMAP.MapView()
        self.map.selection_changed.connect(self._selection_changed)
        self.map.active_changed.connect(self._active_changed)
        self.map.hover_ll.connect(self._hover)
        self.map.status_message.connect(self._status)

        panel = QWidget()
        panel.setFixedWidth(280)
        pv = QVBoxLayout(panel)
        pv.setContentsMargins(10, 10, 10, 10)

        self.info_group = QGroupBox("Tile")
        ig = QFormLayout(self.info_group)
        self.info_title = QLabel("—")
        ig.addRow(self.info_title)
        self.info_provider = QLabel("—")
        ig.addRow("Imagery:", self.info_provider)
        self.info_zl = QLabel("—")
        ig.addRow("Zoom level:", self.info_zl)
        self.info_mesh = QLabel("—")
        ig.addRow("Mesh built:", self.info_mesh)
        self.info_imagery = QLabel("—")
        ig.addRow("Imagery updated:", self.info_imagery)
        self.info_elevation = QLabel("—")
        self.info_elevation.setWordWrap(True)
        ig.addRow("Elevation:", self.info_elevation)
        self.info_airport_lidar = QLabel("—")
        self.info_airport_lidar.setWordWrap(True)
        ig.addRow("Airport lidar:", self.info_airport_lidar)
        # Manual-setup affordance (VIEW): shown by the controller when
        # the model reports manual-download sources that could serve
        # the active tile but are not set up yet.
        self.manual_elevation_btn = QPushButton(
            "ⓘ Better elevation data available…"
        )
        self.manual_elevation_btn.setFlat(True)
        self.manual_elevation_btn.setStyleSheet(
            "QPushButton { text-align: left; color: palette(link); }"
        )
        self.manual_elevation_btn.clicked.connect(
            self._show_manual_elevation_dialog
        )
        self.manual_elevation_btn.setVisible(False)
        ig.addRow(self.manual_elevation_btn)
        self._manual_elevation_entries = []
        self.info_size = QLabel("—")
        ig.addRow("Size on disk:", self.info_size)
        self.install_check = QCheckBox("Installed in X-Plane")
        self.install_check.clicked.connect(self._toggle_install)
        ig.addRow(self.install_check)
        pv.addWidget(self.info_group)

        build_group = QGroupBox("Build")
        bgl = QVBoxLayout(build_group)
        self.build_stack = QStackedWidget()
        bgl.addWidget(self.build_stack)

        # Page 0 — build options
        options_page = QWidget()
        bg = QVBoxLayout(options_page)
        bg.setContentsMargins(0, 0, 0, 0)
        self.build_summary = QLabel("No tiles selected")
        bg.addWidget(self.build_summary)
        self.chk_vector = QCheckBox("Vector, mesh && masks")
        self.chk_vector.setChecked(True)
        self.chk_imagery = QCheckBox("Imagery && DSF")
        self.chk_imagery.setChecked(True)
        self.chk_overlays = QCheckBox("Extract overlays")
        self.chk_skip_built = QCheckBox("Skip already-built tiles")
        self.chk_skip_built.setChecked(True)
        for c in (
            self.chk_vector,
            self.chk_imagery,
            self.chk_overlays,
            self.chk_skip_built,
        ):
            bg.addWidget(c)

        # Texture mode: what the base mesh is textured with (per-tile config).
        self.texture_row = QWidget()
        trl = QHBoxLayout(self.texture_row)
        trl.setContentsMargins(0, 0, 0, 0)
        self.texture_label = QLabel("Textures:")
        trl.addWidget(self.texture_label)
        self.texture_combo = QComboBox()
        for label, value in TEXTURE_MODE_CHOICES:
            self.texture_combo.addItem(label, value)
        self.texture_combo.currentIndexChanged.connect(
            self._texture_mode_changed
        )
        trl.addWidget(self.texture_combo, 1)
        bg.addWidget(self.texture_row)

        self.build_btn = QPushButton("▶ Build")
        self.build_btn.clicked.connect(self.start_build)
        bg.addWidget(self.build_btn)
        self.build_stack.addWidget(options_page)

        # Page 1 — live per-tile progress (shown while building)
        progress_page = QWidget()
        pg = QVBoxLayout(progress_page)
        pg.setContentsMargins(0, 0, 0, 0)
        self.progress_title = QLabel("")
        pg.addWidget(self.progress_title)
        rows_scroll = QScrollArea()
        rows_scroll.setWidgetResizable(True)
        rows_host = QWidget()
        self._rows_layout = QVBoxLayout(rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        self._rows_layout.addStretch(1)
        rows_scroll.setWidget(rows_host)
        pg.addWidget(rows_scroll, 1)
        self.elapsed_label = QLabel("Elapsed —")
        pg.addWidget(self.elapsed_label)
        self.eta_label = QLabel("Remaining —")
        pg.addWidget(self.eta_label)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.clicked.connect(self.request_stop)
        pg.addWidget(self.stop_btn)
        self.build_stack.addWidget(progress_page)

        pv.addWidget(build_group, 1)
        self._tile_rows = {}
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._update_build_clock)

        center = QWidget()
        ch = QHBoxLayout(center)
        ch.setContentsMargins(0, 0, 0, 0)
        ch.setSpacing(0)
        ch.addWidget(self.map, 1)
        ch.addWidget(panel)

        # Console drawer
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(MAX_CONSOLE_LINES)
        font = self.console.font()
        font.setFamily("Menlo" if sys.platform == "darwin" else "Monospace")
        font.setPointSize(11)
        self.console.setFont(font)

        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(center)
        self.splitter.addWidget(self.console)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.setCentralWidget(self.splitter)

        status = QStatusBar()
        self.setStatusBar(status)
        self.coords_label = QLabel("—")
        status.addWidget(self.coords_label)
        self.zoom_label = QLabel("")
        status.addWidget(self.zoom_label)
        self.selection_label = QLabel("")
        status.addPermanentWidget(self.selection_label)
        self.console_btn = QPushButton("Console ▾")
        self.console_btn.setFlat(True)
        self.console_btn.clicked.connect(self.toggle_console)
        status.addPermanentWidget(self.console_btn)
        self.map.view_changed.connect(self._update_zoom_label)

    def _make_menus(self):
        file_menu = self.menuBar().addMenu("&File")
        settings_action = QAction("Settings…", self)
        settings_action.setShortcut(QKeySequence.Preferences)
        settings_action.triggered.connect(self.open_settings)
        file_menu.addAction(settings_action)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")
        refresh_action = QAction("Refresh tiles", self)
        refresh_action.setShortcut(QKeySequence.Refresh)
        refresh_action.triggered.connect(self.refresh_tiles)
        view_menu.addAction(refresh_action)
        console_action = QAction("Toggle console", self)
        console_action.triggered.connect(self.toggle_console)
        view_menu.addAction(console_action)
        legend_action = QAction("Show map legend", self)
        legend_action.setCheckable(True)
        legend_action.setChecked(bool(self.prefs.get("legend", True)))
        self.map.set_legend_visible(legend_action.isChecked())

        def toggle_legend(checked):
            self.map.set_legend_visible(checked)
            self.prefs["legend"] = bool(checked)

        legend_action.toggled.connect(toggle_legend)
        view_menu.addAction(legend_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        overlay_action = QAction("Link overlays folder in X-Plane", self)
        overlay_action.triggered.connect(self._toggle_overlay_link)
        tools_menu.addAction(overlay_action)

        help_menu = self.menuBar().addMenu("&Help")
        wizard_action = QAction("Run setup assistant…", self)
        wizard_action.triggered.connect(self.run_wizard)
        help_menu.addAction(wizard_action)
        about_action = QAction("About Ortho4XP", self)
        about_action.triggered.connect(
            lambda: QMessageBox.about(
                self,
                "Ortho4XP",
                "Ortho4XP %s\nMap-first Qt UI (preview build)."
                % O4_Version.version,
            )
        )
        help_menu.addAction(about_action)

    # ------------------------------------------------------------------
    # Prefs / settings
    # ------------------------------------------------------------------
    def _apply_prefs(self, initial=False):
        import O4_Config_Utils as CFG

        imagery = self.prefs.get("imagery", "BI")
        if imagery in [
            self.imagery_combo.itemText(i)
            for i in range(self.imagery_combo.count())
        ]:
            self.imagery_combo.setCurrentText(imagery)
        self.zl_combo.setCurrentText(str(self.prefs.get("zl", 16)))
        xplane = self.prefs.get("xplane_dir", "")
        if xplane and not CFG.custom_scenery_dir:
            candidate = os.path.join(xplane, "Custom Scenery")
            if os.path.isdir(candidate):
                CFG.custom_scenery_dir = candidate
        self.map.set_provider(self.imagery_combo.currentText())

    def run_wizard(self):
        wizard = QTWIZ.OnboardingWizard(
            self.prefs, gui_provider_codes(), self
        )
        wizard.exec()
        self.prefs = dict(wizard.prefs)
        save_prefs(self.prefs)
        self._seed_paths_from_xplane()
        self._apply_prefs()
        self._load_airports_async()
        self.refresh_tiles()

    def _seed_paths_from_xplane(self):
        """Fill empty scenery/overlay paths from the X-Plane folder."""
        import O4_Settings_Model as SM

        xplane = self.prefs.get("xplane_dir", "")
        if not xplane:
            return
        seed = {}
        current = SM.read_global_raw()
        scenery = os.path.join(xplane, "Custom Scenery")
        if not current.get("custom_scenery_dir") and os.path.isdir(scenery):
            seed["custom_scenery_dir"] = scenery
        overlays = os.path.join(xplane, "Global Scenery")
        if not current.get("custom_overlay_src") and os.path.isdir(overlays):
            seed["custom_overlay_src"] = overlays
        if seed:
            try:
                SM.write_global(seed)
                SM.apply_runtime(seed)
                for key, value in seed.items():
                    print("Derived %s from X-Plane folder: %s" % (key, value))
            except OSError as exc:
                print("Could not save derived paths:", exc)

    def open_settings(self):
        dialog = QTSET.SettingsWindow(
            self.prefs,
            self.map.active_tile(),
            self.output_dir(),
            self,
        )
        if dialog.exec() == QDialog.Accepted:
            old_xplane = self.prefs.get("xplane_dir", "")
            self.prefs = dialog.result_prefs()
            save_prefs(self.prefs)
            self._apply_prefs()
            if self.prefs.get("xplane_dir", "") != old_xplane:
                self._seed_paths_from_xplane()
                self._load_airports_async()
            self.refresh_tiles()

    def output_dir(self):
        """Custom build dir semantics: '' = default Tiles dir; a path with a
        trailing separator = per-tile subdirectories inside it."""
        out = self.prefs.get("output_dir", "")
        if out and not out.endswith(("/", "\\")):
            out += "/"
        return out

    def working_dir(self):
        out = self.prefs.get("output_dir", "")
        return out if out else FNAMES.Tile_dir

    # ------------------------------------------------------------------
    # Airport search
    # ------------------------------------------------------------------
    def _load_airports_async(self):
        """Load the airport search index, rebuilding it when the X-Plane
        apt.dat sources changed since the cache was written (mtime/size)."""
        xplane = self.prefs.get("xplane_dir", "")

        def work():
            try:
                paths = APT.find_apt_dats(xplane) if xplane else []
                if paths and APT.index_is_stale(paths, AIRPORT_CACHE):
                    if os.path.isfile(AIRPORT_CACHE):
                        print(
                            "X-Plane airport data changed — refreshing the "
                            "search index…"
                        )
                    else:
                        print("Building the airport search index…")
                    count = APT.build_index(paths, AIRPORT_CACHE)
                    print(
                        "Airport search index ready: %d airports." % count
                    )
                if os.path.isfile(AIRPORT_CACHE):
                    self._airports = APT.load_index(AIRPORT_CACHE)
            except Exception as exc:
                print("Airport index unavailable:", exc)

        threading.Thread(target=work, daemon=True).start()

    def _update_search_popup(self, text):
        self.search_popup.clear()
        text = text.strip()
        if len(text) < 2:
            self.search_popup.hide()
            return
        coord = APT.parse_coordinate_query(text)
        if coord:
            item = QListWidgetItem("Go to tile %+03d%+04d" % coord)
            item.setData(Qt.UserRole, ("tile", coord))
            self.search_popup.addItem(item)
        for entry in APT.search(self._airports, text, limit=8):
            label = "%s — %s" % (entry.code, entry.name)
            if entry.city:
                label += " (%s)" % entry.city
            item = QListWidgetItem(label)
            item.setData(
                Qt.UserRole,
                ("apt", (math.floor(entry.lat), math.floor(entry.lon))),
            )
            self.search_popup.addItem(item)
        if not self.search_popup.count():
            self.search_popup.hide()
            return
        pos = self.search_edit.mapToGlobal(
            self.search_edit.rect().bottomLeft()
        )
        self.search_popup.move(pos)
        self.search_popup.resize(self.search_edit.width(), 180)
        self.search_popup.show()

    def _search_accept_first(self):
        if self.search_popup.count():
            self._search_accept_item(self.search_popup.item(0))

    def _search_accept_item(self, item):
        kind, (lat, lon) = item.data(Qt.UserRole)
        self.search_popup.hide()
        self.search_edit.clear()
        self.map.center_on_tile(lat, lon, zoom=9 if kind == "apt" else 8)
        self.map.set_active(lat, lon)

    # ------------------------------------------------------------------
    # Selection / tile info
    # ------------------------------------------------------------------
    def refresh_tiles(self):
        import O4_Config_Utils as CFG

        # Fresh accumulators per scan: the display keeps showing the old
        # overlay until ScanDone swaps the authoritative result in, so
        # deleted tiles vanish exactly when the scan completes (the same
        # contract the one-shot scan had).
        self._scan_built = {}
        self._scan_installed = set()
        self._session.scan(self.working_dir(), CFG.custom_scenery_dir)

    # ------------------------------------------------------------------
    # Engine event dispatch (the view renders; the session computes)
    # ------------------------------------------------------------------
    def _on_engine_event(self, event):
        handler = self._event_handlers.get(type(event))
        if handler is not None:
            handler(event)

    def _on_scan_progress(self, event):
        self.map.set_scan_status(event.phase, event.done, event.total)

    def _on_scan_batch(self, event):
        self._scan_built.update(event.built)
        self._scan_installed.update(event.installed)
        self._built.update(event.built)
        self._installed.update(event.installed)
        self.map.set_built(self._built)
        self.map.set_installed(self._installed)

    def _on_scan_done(self, event):
        self._built = dict(self._scan_built)
        self._installed = set(self._scan_installed)
        self.map.clear_scan_status()
        self._push_overlays()

    def _push_overlays(self):
        self.map.set_built(self._built)
        self.map.set_installed(self._installed)
        self._active_changed(self.map.active_tile())

    def _selection_changed(self):
        sel = self.map.selection()
        n = len(sel)
        if not n:
            self.build_summary.setText("No tiles selected")
        else:
            try:
                zl = int(self.zl_combo.currentText())
            except ValueError:
                zl = 16
            est = n * 3.0 * 4 ** (zl - 16)
            summary_text = "%d tile%s selected · rough est. %.1f GB" % (
                n,
                "s" if n > 1 else "",
                est,
            )
            try:
                import O4_Airport_Elevation_Insets as ELEVATION_PROVIDERS

                covered = ELEVATION_PROVIDERS.tiles_with_inset_coverage(sel)
                if covered and n == 1:
                    summary_text += " · airport lidar available"
                elif covered and len(covered) == n:
                    summary_text += " · airport lidar on all"
                elif covered:
                    summary_text += " · airport lidar on %d" % len(covered)
            except Exception:
                pass
            self.build_summary.setText(summary_text)
        self.selection_label.setText("%d selected" % n if n else "")
        self.build_btn.setText("▶ Build %d tile%s" % (n, "s" if n > 1 else "")
                               if n else "▶ Build")

    def _active_changed(self, tile):
        self._selection_changed()
        self._refresh_texture_mode(tile)
        if tile is None:
            self.info_group.setVisible(False)
            return
        lat, lon = tile
        self.info_group.setVisible(True)
        self.info_title.setText(
            "<b>Tile %s</b>" % FNAMES.short_latlon(lat, lon)
        )
        info = self._built.get(tile)
        import O4_Config_Utils as CFG

        # The elevation rows populate for built AND unbuilt tiles: what
        # data a build WOULD use matters most before building.
        try:
            (base_text, lidar_text) = _elevation_row_texts(
                lat, lon, info.custom_dem if info else ""
            )
        except Exception:
            (base_text, lidar_text) = ("?", "?")
        self.info_elevation.setText(base_text)
        self.info_airport_lidar.setText(lidar_text)
        # Manual-setup affordance (CONTROLLER): ask the model which
        # manual-download sources could serve this tile and are not set
        # up yet; the view only renders what it is handed.
        try:
            import O4_Airport_Elevation_Insets as ELEVATION_PROVIDERS

            self._manual_elevation_entries = [
                entry
                for entry in (
                    ELEVATION_PROVIDERS.manual_elevation_setup_for_tile(
                        lat, lon
                    )
                )
                if not entry["already_dropped"]
            ]
        except Exception:
            self._manual_elevation_entries = []
        self.manual_elevation_btn.setVisible(
            bool(self._manual_elevation_entries)
        )

        if info is None:
            for w in (
                self.info_provider,
                self.info_zl,
                self.info_mesh,
                self.info_imagery,
                self.info_size,
            ):
                w.setText("—")
            self.info_title.setText(
                self.info_title.text() + "  (not built)"
            )
            self.install_check.setEnabled(False)
            self.install_check.setChecked(False)
            return
        self.info_provider.setText(info.provider or "?")
        zl_text = str(info.zl) if info.zl else "?"
        if info.has_zones:
            zl_text += " + zones"
        self.info_zl.setText(zl_text)
        self.info_mesh.setText(_fmt_date(info.mesh_date))
        self.info_imagery.setText(_fmt_date(info.imagery_date))
        if info.size_bytes is None:
            self.info_size.setText("computing…")
            threading.Thread(
                target=self._compute_size_async, args=(info,), daemon=True
            ).start()
        else:
            self.info_size.setText(_fmt_size(info.size_bytes))
        can_link = bool(CFG.custom_scenery_dir)
        self.install_check.setChecked(tile in self._installed)
        physical = False
        if can_link:
            try:
                physical = (
                    LINKS.link_status(
                        lat, lon, info.build_dir, CFG.custom_scenery_dir
                    )
                    is LINKS.LinkStatus.PHYSICAL
                )
            except OSError:
                pass
        self.install_check.setEnabled(
            can_link and not self._building and not physical
        )
        if not can_link:
            self.install_check.setToolTip(
                "Set your X-Plane folder in Settings to install tiles."
            )
        elif physical:
            self.install_check.setToolTip(
                "This tile's folder lives directly in Custom Scenery, so it "
                "is always installed. To manage it as a link, move the "
                "folder elsewhere first."
            )
        else:
            self.install_check.setToolTip("")

    def _refresh_texture_mode(self, tile):
        """Load the active tile's ``texture_mode`` into the build-area combo.

        Reads the per-tile config via :mod:`O4_Settings_Model`; falls back to
        the registry default (``full_ortho``) when the tile has no config or
        the value is unknown.  Signals are blocked so the programmatic update
        does not trigger a write back to disk.
        """
        import O4_Settings_Model as SM

        value = "full_ortho"
        if tile is not None:
            raw = SM.read_tile_raw(tile[0], tile[1], self.output_dir())
            if raw and raw.get("texture_mode"):
                value = raw["texture_mode"]
        index = self.texture_combo.findData(value)
        if index < 0:
            index = 0
        self.texture_combo.blockSignals(True)
        self.texture_combo.setCurrentIndex(index)
        self.texture_combo.blockSignals(False)

    def _texture_mode_changed(self, index):
        """Persist the chosen texture mode to the active tile's config.

        No-ops when no tile is active.  Writes only the ``texture_mode`` key;
        :func:`O4_Settings_Model.write_tile` preserves every other tile var.
        """
        tile = self.map.active_tile()
        if tile is None:
            return
        value = self.texture_combo.itemData(index)
        if value is None:
            return
        import O4_Settings_Model as SM

        try:
            SM.write_tile(
                tile[0], tile[1], self.output_dir(), {"texture_mode": value}
            )
        except OSError as exc:
            print("Could not save texture mode:", exc)

    def _compute_size_async(self, info):
        try:
            TINFO.compute_size(info)
        except Exception:
            return
        self._bridge.size_computed.emit()

    def _show_manual_elevation_dialog(self):
        """Render the model's manual-setup entries (VIEW only).

        One section per provider: what it is, a clickable download
        page, the numbered steps, and the drop folder with an opener --
        every string comes from the model entry verbatim.
        """
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        entries = self._manual_elevation_entries
        if not entries:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Better elevation data for this tile")
        layout = QVBoxLayout(dialog)
        introduction = QLabel(
            "These sources cover this tile but must be downloaded once "
            "by hand (their file hosts do not allow automatic "
            "downloads). After the files are in place, every build "
            "uses them automatically."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        for entry in entries:
            group = QGroupBox(
                "%s — %s %s"
                % (
                    entry["code"],
                    entry["native_resolution"],
                    "tile-wide" if entry["role"] == "base" else
                    "airport lidar",
                )
            )
            group_layout = QVBoxLayout(group)
            link = QLabel(
                '1. Download from <a href="%s">%s</a>'
                % (entry["download_page"], entry["download_page"])
            )
            link.setOpenExternalLinks(True)
            link.setWordWrap(True)
            group_layout.addWidget(link)
            for (number, step) in enumerate(entry["steps"][1:], start=2):
                step_label = QLabel("%d. %s" % (number, step))
                step_label.setWordWrap(True)
                group_layout.addWidget(step_label)
            folder_row = QHBoxLayout()
            folder_label = QLabel(entry["drop_directory"])
            folder_label.setWordWrap(True)
            folder_label.setTextInteractionFlags(
                Qt.TextSelectableByMouse
            )
            folder_row.addWidget(folder_label, 1)
            open_button = QPushButton("Open folder")

            def _open_drop_folder(_checked=False, path=entry["drop_directory"]):
                os.makedirs(path, exist_ok=True)
                QDesktopServices.openUrl(QUrl.fromLocalFile(path))

            open_button.clicked.connect(_open_drop_folder)
            folder_row.addWidget(open_button)
            group_layout.addLayout(folder_row)
            if entry.get("license"):
                license_label = QLabel(
                    "Licence: %s" % entry["license"]
                )
                license_label.setWordWrap(True)
                license_label.setStyleSheet("color: palette(mid);")
                group_layout.addWidget(license_label)
            layout.addWidget(group)
        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button, alignment=Qt.AlignRight)
        dialog.resize(560, min(220 + 200 * len(entries), 640))
        dialog.exec()

    def _toggle_install(self, checked):
        import O4_Config_Utils as CFG

        tile = self.map.active_tile()
        if tile is None:
            return
        lat, lon = tile
        info = self._built.get(tile)
        if info is None:
            self.install_check.setChecked(False)
            return
        try:
            if checked:
                LINKS.install(lat, lon, info.build_dir, CFG.custom_scenery_dir)
                self._installed.add(tile)
                print(
                    "Installed %s in X-Plane." % FNAMES.short_latlon(lat, lon)
                )
            else:
                LINKS.uninstall(
                    lat, lon, info.build_dir, CFG.custom_scenery_dir
                )
                self._installed.discard(tile)
                print(
                    "Removed %s from X-Plane."
                    % FNAMES.short_latlon(lat, lon)
                )
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Install in X-Plane", str(exc))
            self.install_check.setChecked(tile in self._installed)
        self.map.set_installed(self._installed)

    def _toggle_overlay_link(self):
        import O4_Config_Utils as CFG

        if not CFG.custom_scenery_dir:
            QMessageBox.information(
                self,
                "Overlays",
                "Set your X-Plane folder in Settings first.",
            )
            return
        try:
            status = os.path.isdir(
                os.path.join(CFG.custom_scenery_dir, "yOrtho4XP_Overlays")
            )
            if status:
                LINKS.uninstall_overlay_link(
                    FNAMES.Overlay_dir, CFG.custom_scenery_dir
                )
                print("Overlay link removed from Custom Scenery.")
            else:
                LINKS.install_overlay_link(
                    FNAMES.Overlay_dir, CFG.custom_scenery_dir
                )
                print("Overlay link added to Custom Scenery.")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Overlays", str(exc))

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------
    def _imagery_changed(self, code):
        self.map.set_provider(code)
        self._update_zoom_label()

    def start_build(self):
        if self._building:
            return
        selection = sorted(self.map.selection())
        if not selection:
            self._status("Select at least one tile to build.")
            return
        if self.chk_skip_built.isChecked():
            todo = [t for t in selection if t not in self._built]
            skipped = len(selection) - len(todo)
            if skipped:
                print(
                    "Skipping %d already-built tile%s "
                    "(uncheck 'Skip already-built tiles' to rebuild)."
                    % (skipped, "s" if skipped > 1 else "")
                )
        else:
            todo = selection
        if not todo:
            self._status("All selected tiles are already built.")
            return
        do_vector = self.chk_vector.isChecked()
        do_imagery = self.chk_imagery.isChecked()
        do_overlays = self.chk_overlays.isChecked()
        if not (do_vector or do_imagery or do_overlays):
            self._status("Choose at least one build step.")
            return

        self._building = True
        self._stop_requested = False
        self.stop_btn.setEnabled(True)
        self.stop_btn.setText("■ Stop")
        self.install_check.setEnabled(False)
        self.map.set_locked(True)
        self.map.zoom_to_tiles(todo)
        self._progress_states = {
            t: ("queued", "queued", 0) for t in todo
        }
        self.map.set_progress(self._progress_states)
        self._setup_progress_page(todo)
        if not self.console.isVisible():
            self.toggle_console()

        self._session.build(
            todo,
            provider=self.imagery_combo.currentText(),
            zoomlevel=int(self.zl_combo.currentText()),
            custom_build_dir=self.output_dir(),
            do_vector=do_vector,
            do_imagery=do_imagery,
            do_overlays=do_overlays,
        )

    def _setup_progress_page(self, todo):
        """Morph the Build box into the per-tile progress list."""
        for bar, status, row in self._tile_rows.values():
            row.deleteLater()
        self._tile_rows = {}
        for tile in todo:
            row = QWidget()
            rl = QVBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(1)
            head = QHBoxLayout()
            head.addWidget(QLabel(FNAMES.short_latlon(*tile)))
            head.addStretch(1)
            status = QLabel("queued")
            status.setStyleSheet("color: gray; font-size: 11px;")
            head.addWidget(status)
            rl.addLayout(head)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            rl.addWidget(bar)
            self._rows_layout.insertWidget(
                self._rows_layout.count() - 1, row
            )
            self._tile_rows[tile] = (bar, status, row)
        self._done_count = 0
        self._ntiles = len(todo)
        self._build_t0 = __import__("time").time()
        self.progress_title.setText(
            "<b>Building %d tile%s</b>"
            % (len(todo), "s" if len(todo) > 1 else "")
        )
        self.elapsed_label.setText("Elapsed 0 s")
        self.eta_label.setText("Remaining —")
        self._elapsed_timer.start()
        self.build_stack.setCurrentIndex(1)

    def _on_step_progress(self, event):
        tile = (event.lat, event.lon)
        state = "indeterminate" if event.indeterminate else "active"
        self._progress_states[tile] = (state, event.label, event.percent)
        self.map.set_progress(self._progress_states)
        self._update_tile_row(tile, event.percent, event.label)
        self.setWindowTitle(
            "Ortho4XP — building %s · %s · %d%%"
            % (FNAMES.short_latlon(*tile), event.label, event.percent)
        )

    def _on_tile_state(self, event):
        tile = (event.lat, event.lon)
        state, label, pct = event.state, event.label, event.percent
        if state == "done":
            self._done_count += 1
        self._progress_states[tile] = (state, label, pct)
        self.map.set_progress(self._progress_states)
        if tile in self._tile_rows:
            bar, status, _ = self._tile_rows[tile]
            if state == "done":
                bar.setValue(100)
                status.setText("done ✓")
                status.setStyleSheet("color: green; font-size: 11px;")
            elif state == "error":
                status.setText("failed")
                status.setStyleSheet("color: red; font-size: 11px;")
            elif label == "stopped":
                status.setText("stopped")
        if state == "done":
            self.refresh_tiles()

    def _on_run_eta(self, event):
        self._last_run_eta = event

    def _update_tile_row(self, tile, pct, label):
        if tile in self._tile_rows:
            bar, status, _ = self._tile_rows[tile]
            bar.setValue(int(pct))
            status.setText("%s · %d%%" % (label, pct))

    def _update_build_clock(self):
        import time as _time

        if self._build_t0 is None:
            return
        elapsed = _time.time() - self._build_t0
        self.elapsed_label.setText("Elapsed %s" % _fmt_duration(elapsed))
        # The engine session owns the estimate (learned per-step model +
        # live in-step rate + the auto-patch model); the view only renders.
        eta = self._last_run_eta
        if eta is not None and eta.remaining_seconds is not None:
            self.eta_label.setText(
                "Remaining ≈ %s" % _fmt_duration(eta.remaining_seconds)
            )
        else:
            self.eta_label.setText("Remaining —")

    def _on_run_done(self, event):
        done, errors = event.done_count, event.error_count
        self._building = False
        self._last_run_eta = None
        self._elapsed_timer.stop()
        self.stop_btn.setEnabled(False)
        self.stop_btn.setText("■ Stop")
        self.map.set_locked(False)
        self.setWindowTitle("Ortho4XP " + O4_Version.version)
        summary = "Build finished: %d ok, %d failed." % (done, errors)
        if self._stop_requested:
            summary = "Build stopped: %d done, %d failed." % (done, errors)
        print(summary)
        self._status(summary)
        self.progress_title.setText("<b>%s</b>" % summary)
        UI.is_working = False

        def revert():
            self.map.set_progress({})
            self.build_stack.setCurrentIndex(0)
            self._selection_changed()

        QTimer.singleShot(5000, revert)
        self.refresh_tiles()
        QApplication.beep()

    def request_stop(self):
        self._stop_requested = True
        self._session.cancel()
        self.stop_btn.setText("Stopping after current step…")
        self.stop_btn.setEnabled(False)

    # ------------------------------------------------------------------
    # Console / status
    # ------------------------------------------------------------------
    def _drain_console(self):
        chunks = []
        try:
            while True:
                chunks.append(self._console_queue.get_nowait())
        except queue.Empty:
            pass
        if chunks:
            text = "".join(chunks)
            self.console.moveCursor(QTextCursor.End)
            self.console.insertPlainText(text)
            self.console.moveCursor(QTextCursor.End)

    def toggle_console(self):
        visible = not self.console.isVisible()
        self.console.setVisible(visible)
        self.console_btn.setText("Console ▴" if visible else "Console ▾")

    def _hover(self, lat, lon):
        self.coords_label.setText("%.3f°, %.3f°" % (lat, lon))

    def _update_zoom_label(self):
        self.zoom_label.setText(
            "z%.0f · %s" % (self.map.zoom_level(), self.map.display_code())
        )

    def _status(self, message):
        self.statusBar().showMessage(message, 8000)

    # ------------------------------------------------------------------
    def closeEvent(self, event):
        if self._building:
            answer = QMessageBox.question(
                self,
                "Build in progress",
                "A build is running. Stop it and quit?",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            UI.red_flag = True
        tile = self.map.active_tile()
        if tile:
            self.prefs["last_lat"], self.prefs["last_lon"] = tile
        self.prefs["imagery"] = self.imagery_combo.currentText()
        self.prefs["zl"] = int(self.zl_combo.currentText())
        save_prefs(self.prefs)
        event.accept()


def _fmt_date(mtime):
    if not mtime:
        return "—"
    import datetime

    return datetime.datetime.fromtimestamp(mtime).strftime("%d %b %Y %H:%M")


def _fmt_arc_seconds(resolution_arc_seconds):
    """Human text for a base-source posting, e.g. '1 arc-second (~30 m)'."""
    if not resolution_arc_seconds:
        return "unknown resolution"
    value = float(resolution_arc_seconds)
    if value < 1.0:
        text = "1/%d arc-second" % round(1.0 / value)
    elif value == int(value):
        text = "%d arc-second" % int(value)
    else:
        text = "%.2f arc-second" % value
    return "%s (~%d m)" % (text, round(value * 30))


def _elevation_row_texts(lat, lon, tile_custom_dem=""):
    """The (base, airport lidar) strings for the tile-info elevation rows.

    Everything behind this is offline (registry, local files, the
    cached inset index) — see summarize_tile_elevation_sources — so it
    runs on every selection change without stalling the UI.
    """
    import O4_DEM_Utils as DEM
    import O4_Airport_Elevation_Insets as ELEVATION_PROVIDERS

    summary = ELEVATION_PROVIDERS.summarize_tile_elevation_sources(
        lat, lon, DEM.base_elevation_source
    )
    if tile_custom_dem:
        # The tile config pins its own source; the first ";"-token is
        # the base, the rest are local insets.
        base_text = "custom: " + (
            os.path.basename(tile_custom_dem.split(";")[0])
            or tile_custom_dem
        )
    else:
        base_text = "%s, %s" % (
            summary["base_code"],
            _fmt_arc_seconds(summary["base_resolution_arc_seconds"]),
        )
        if summary["base_is_fallback"]:
            base_text += " (default)"
    if summary["fetched_airports"] is not None:
        pieces = []
        if summary["fetched_airports"]:
            pieces.append(
                "%d airport%s fetched"
                % (
                    summary["fetched_airports"],
                    "s" if summary["fetched_airports"] > 1 else "",
                )
            )
        if summary["no_coverage_airports"]:
            pieces.append(
                "%d without coverage" % summary["no_coverage_airports"]
            )
        lidar_text = " · ".join(pieces) if pieces else "no airports found"
    elif summary["inset_providers"]:
        lidar_text = "available: " + ", ".join(
            "%s (%s m)"
            % (code, ("%g" % resolution) if resolution else "?")
            for (code, resolution) in summary["inset_providers"]
        )
    else:
        lidar_text = "none for this region"
    return (base_text, lidar_text)


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return "%d s" % seconds
    if seconds < 3600:
        return "%d m %02d s" % (seconds // 60, seconds % 60)
    return "%d h %02d m" % (seconds // 3600, (seconds % 3600) // 60)


def _fmt_size(nbytes):
    value = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return "%.1f %s" % (value, unit)
        value /= 1024
