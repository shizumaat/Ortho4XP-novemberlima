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
import O4_Qt_Settings as QTSET
import O4_Qt_Wizard as QTWIZ

PREFS_FILE = FNAMES.resource_path(".qt_prefs.json")
AIRPORT_CACHE = FNAMES.resource_path(".airport_index.tsv")
MAX_CONSOLE_LINES = 5000

# Build-area texture-mode selector: user-visible label -> tile config value.
# Order is significant (it is the popup-menu item order).
TEXTURE_MODE_CHOICES = (
    ("Full Ortho", "full_ortho"),
    ("Airport Ortho", "airport_ortho"),
    ("Default X-Plane", "default_xplane"),
)

# ---------------------------------------------------------------------------
# Whole-tile progress model: each pipeline step owns a weighted slice of the
# tile's 0-100%, so the per-tile ring/bar climbs once and never restarts.
# ---------------------------------------------------------------------------
STEP_WEIGHTS = {
    "vector": 0.10,
    "mesh": 0.15,
    "masks": 0.10,
    "imagery": 0.60,
    "overlays": 0.05,
}
STEP_LABELS = {
    "vector": "vector data",
    "mesh": "triangulating",
    "masks": "water masks",
    "imagery": "imagery & DSF",
    "overlays": "overlays",
}


def plan_steps(do_vector, do_imagery, do_overlays):
    """Ordered (key, base_fraction, slice_fraction) plan for the selected
    steps, with slices normalized so a full tile is exactly 1.0."""
    keys = []
    if do_vector:
        keys += ["vector", "mesh", "masks"]
    if do_imagery:
        keys.append("imagery")
    if do_overlays:
        keys.append("overlays")
    total = sum(STEP_WEIGHTS[k] for k in keys)
    plan, base = [], 0.0
    for k in keys:
        weight = STEP_WEIGHTS[k] / total
        plan.append((k, base, weight))
        base += weight
    return plan


def step_progress(step_key, bars):
    """Progress (0-100) inside one step from the three legacy progress
    bars, or None when the step reports no usable percentage (mesh
    triangulation, overlay extraction)."""
    if step_key in ("vector", "masks"):
        return bars.get(1, 0)
    if step_key == "imagery":
        return (
            0.55 * bars.get(2, 0)
            + 0.25 * bars.get(3, 0)
            + 0.20 * bars.get(1, 0)
        )
    return None


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


class _ProgressVar:
    """Mimics a Tk IntVar's .set() — the UI.progress_bar contract."""

    def __init__(self, nbr, signal):
        self._nbr = nbr
        self._signal = signal

    def set(self, value):
        self._signal.emit(self._nbr, int(value))


class _UiAdapter(QObject):
    """Stands in for the Tk window as UI.gui (progress channel only)."""

    progress = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.pgrbv = {n: _ProgressVar(n, self.progress) for n in (1, 2, 3)}


class _BuildSignals(QObject):
    tile_state = Signal(int, int, str, str, int)  # lat, lon, state, label, pct
    step_started = Signal(int, int, str, float, float)  # lat, lon, key, base, slice
    finished = Signal(int, int)  # done_count, error_count


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
        self._adapter = _UiAdapter()
        self._adapter.progress.connect(self._on_progress_bar)
        UI.gui = self._adapter
        UI.verbosity = int(self.prefs.get("verbosity", 1))
        self._console_queue = queue.Queue()
        sys.stdout = _StdoutTee(sys.stdout, self._console_queue)
        self._console_timer = QTimer(self)
        self._console_timer.setInterval(120)
        self._console_timer.timeout.connect(self._drain_console)
        self._console_timer.start()
        self._build_signals = _BuildSignals()
        self._build_signals.tile_state.connect(self._on_tile_state)
        self._build_signals.step_started.connect(self._on_step_started)
        self._build_signals.finished.connect(self._on_build_finished)
        self._bar_values = {1: 0, 2: 0, 3: 0}
        self._cur_step = None  # (tile, key, base, slice) while building
        self._build_t0 = None
        self._done_count = 0
        self._ntiles = 0
        self._tile_frac = 0.0

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
        wd = self.working_dir()

        def work():
            import O4_Config_Utils as CFG

            built = {}
            installed = set()
            try:
                if os.path.isdir(wd):
                    built = TINFO.scan_tiles(wd)
            except Exception as exc:
                print("Tile scan failed:", exc)
            try:
                if CFG.custom_scenery_dir:
                    installed = set(
                        LINKS.installed_tiles(CFG.custom_scenery_dir)
                    )
            except Exception as exc:
                print("Custom Scenery scan failed:", exc)
            self._built = built
            self._installed = installed
            QTimer.singleShot(0, self._push_overlays)

        threading.Thread(target=work, daemon=True).start()

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
            self.build_summary.setText(
                "%d tile%s selected · rough est. %.1f GB"
                % (n, "s" if n > 1 else "", est)
            )
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
        self.install_check.setEnabled(can_link and not self._building)
        self.install_check.setChecked(tile in self._installed)
        if not can_link:
            self.install_check.setToolTip(
                "Set your X-Plane folder in Settings to install tiles."
            )

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
        QTimer.singleShot(0, lambda: self._active_changed(
            self.map.active_tile()
        ))

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
        UI.red_flag = False
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

        provider = self.imagery_combo.currentText()
        zl = int(self.zl_combo.currentText())
        custom_build_dir = self.output_dir()
        plan = plan_steps(do_vector, do_imagery, do_overlays)
        threading.Thread(
            target=self._build_worker,
            args=(todo, provider, zl, custom_build_dir, plan),
            daemon=True,
        ).start()

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
        self._tile_frac = 0.0
        self._build_t0 = __import__("time").time()
        self.progress_title.setText(
            "<b>Building %d tile%s</b>"
            % (len(todo), "s" if len(todo) > 1 else "")
        )
        self.elapsed_label.setText("Elapsed 0 s")
        self.eta_label.setText("Remaining —")
        self._elapsed_timer.start()
        self.build_stack.setCurrentIndex(1)

    def _build_worker(self, todo, provider, zl, custom_build_dir, plan):
        # Heavy pipeline imports stay off the GUI startup path.
        import O4_Config_Utils as CFG
        import O4_Vector_Map as VMAP
        import O4_Mesh_Utils as MESH
        import O4_Mask_Utils as MASK
        import O4_Tile_Utils as TILE
        import O4_Overlay_Utils as OVL

        step_fn = {
            "vector": VMAP.build_poly_file,
            "mesh": MESH.build_mesh,
            "masks": MASK.build_masks,
            "imagery": TILE.build_tile,
            "overlays": lambda t: OVL.build_overlay(t.lat, t.lon),
        }
        done = errors = 0
        emit_state = self._build_signals.tile_state.emit
        emit_step = self._build_signals.step_started.emit
        for (lat, lon) in todo:
            if UI.red_flag:
                break
            try:
                tile = CFG.Tile(lat, lon, custom_build_dir)
                if not tile.read_from_config():
                    tile.default_website = provider
                    tile.default_zl = zl
                UI.reset_total_elapsed()
                if any(k != "overlays" for k, _, _ in plan):
                    tile.make_dirs()
                failed = False
                for key, base, width in plan:
                    if UI.red_flag:
                        break
                    emit_step(lat, lon, key, base, width)
                    result = step_fn[key](tile)
                    if result == 0 and UI.red_flag:
                        break
                    if result == 0:
                        failed = True
                if UI.red_flag:
                    emit_state(lat, lon, "queued", "stopped", 0)
                    break
                if failed:
                    errors += 1
                    emit_state(lat, lon, "error", "failed", 0)
                else:
                    done += 1
                    emit_state(lat, lon, "done", "", 100)
            except Exception:
                traceback.print_exc()
                errors += 1
                emit_state(lat, lon, "error", "failed", 0)
        self._build_signals.finished.emit(done, errors)

    def _on_step_started(self, lat, lon, key, base, width):
        tile = (lat, lon)
        self._cur_step = (tile, key, base, width)
        self._bar_values = {1: 0, 2: 0, 3: 0}
        label = STEP_LABELS.get(key, key)
        pct = base * 100
        self._tile_frac = base
        # Steps without a usable percentage show a spinner on the map but
        # hold the whole-tile value already earned.
        state = (
            "indeterminate" if step_progress(key, {}) is None else "active"
        )
        self._progress_states[tile] = (state, label, pct)
        self.map.set_progress(self._progress_states)
        self._update_tile_row(tile, pct, label)

    def _on_tile_state(self, lat, lon, state, label, pct):
        tile = (lat, lon)
        if state in ("done", "error"):
            self._cur_step = None
            self._tile_frac = 0.0
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

    def _on_progress_bar(self, nbr, value):
        self._bar_values[nbr] = value
        if self._cur_step is None or not self._building:
            return
        tile, key, base, width = self._cur_step
        sp = step_progress(key, self._bar_values)
        if sp is None:
            return
        pct = min(100.0, (base + width * min(sp, 100) / 100.0) * 100)
        self._tile_frac = pct / 100.0
        label = STEP_LABELS.get(key, key)
        self._progress_states[tile] = ("active", label, pct)
        self.map.set_progress(self._progress_states)
        self._update_tile_row(tile, pct, label)
        self.setWindowTitle(
            "Ortho4XP — building %s · %s · %d%%"
            % (FNAMES.short_latlon(*tile), label, pct)
        )

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
        frac = (
            (self._done_count + self._tile_frac) / self._ntiles
            if self._ntiles
            else 0
        )
        if frac > 0.02:
            remaining = elapsed * (1 - frac) / frac
            self.eta_label.setText(
                "Remaining ≈ %s" % _fmt_duration(remaining)
            )
        else:
            self.eta_label.setText("Remaining —")

    def _on_build_finished(self, done, errors):
        self._building = False
        self._cur_step = None
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
        UI.red_flag = True
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
