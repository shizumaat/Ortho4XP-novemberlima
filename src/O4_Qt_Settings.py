"""Categorized settings window for the Ortho4XP Qt UI.

A category sidebar that filters the page to the selected category, live
search over labels/keys/hints (searching matches across every category and
overrides the sidebar selection until cleared), a Global-defaults ↔
This-tile scope switch for tile-scoped settings, modified-value dots with
right-click reset, and a Show-advanced toggle.

All file/value semantics live in O4_Settings_Model (headless, tested); this
module is presentation and interaction only.
"""

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import O4_File_Names as FNAMES
import O4_Settings_Model as SM

DOT_STYLE = "color: #C7861B; font-weight: bold;"
PATH_SUFFIXES = ("_dir", "_src", "_path", "_src_alternate")


def _is_path_setting(setting):
    return setting.name.endswith(PATH_SUFFIXES) or setting.name in (
        "xplane_dir",
        "output_dir",
        "custom_overlay_src",
    )


class _SettingRow(QWidget):
    """One label + control + description row with a modified indicator."""

    changed = Signal()
    reset_requested = Signal(object, str)  # setting, "default"|"global"|"copy"

    def __init__(self, setting, parent=None):
        super().__init__(parent)
        self.setting = setting
        self._baseline = setting.default
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(1)

        top = QHBoxLayout()
        self.dot = QLabel("●")
        self.dot.setStyleSheet(DOT_STYLE)
        self.dot.setVisible(False)
        self.dot.setToolTip("Differs from the inherited value")
        top.addWidget(self.dot)
        self.name_label = QLabel(setting.label)
        self.name_label.setToolTip(setting.hint or setting.name)
        top.addWidget(self.name_label)
        self.scope_tag = QLabel("app-wide")
        self.scope_tag.setStyleSheet("color: gray; font-size: 10px;")
        self.scope_tag.setVisible(False)
        top.addWidget(self.scope_tag)
        top.addStretch(1)

        if setting.vtype is bool:
            self.control = QCheckBox()
            self.control.toggled.connect(self._emit_changed)
        elif setting.values:
            # Menu shows the human-readable title; the raw config value
            # rides along as item data so storage never sees the label.
            self.control = QComboBox()
            for raw in setting.values:
                self.control.addItem(setting.label_for(raw), raw)
            self.control.currentIndexChanged.connect(self._emit_changed)
        else:
            self.control = QLineEdit()
            self.control.setFixedWidth(
                280 if _is_path_setting(setting) else 110
            )
            self.control.textChanged.connect(self._emit_changed)
        top.addWidget(self.control)
        if isinstance(self.control, QLineEdit) and _is_path_setting(setting):
            browse = QPushButton("…")
            browse.setFixedWidth(28)
            browse.clicked.connect(self._browse)
            top.addWidget(browse)
        lay.addLayout(top)

        if setting.hint:
            # Full hint, word-wrapped — the sidebar filters the page down to
            # one category at a time, so row height is no longer at a
            # premium and truncated descriptions were routinely clipped
            # mid-sentence.
            desc = QLabel(setting.hint)
            desc.setWordWrap(True)
            desc.setStyleSheet("color: gray; font-size: 11px;")
            desc.setContentsMargins(16, 0, 0, 0)
            lay.addWidget(desc)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self._search_blob = (
            "%s %s %s %s" % (
                setting.label,
                setting.name,
                setting.hint,
                " ".join(title for _, title in setting.value_labels),
            )
        ).lower()

    # -- value plumbing -------------------------------------------------
    def value(self):
        if isinstance(self.control, QCheckBox):
            return "True" if self.control.isChecked() else "False"
        if isinstance(self.control, QComboBox):
            return self.control.currentData()
        return self.control.text().strip()

    def set_value(self, text):
        self.control.blockSignals(True)
        if isinstance(self.control, QCheckBox):
            self.control.setChecked(str(text).strip() in ("True", "true", "1"))
        elif isinstance(self.control, QComboBox):
            index = self.control.findData(str(text))
            if index >= 0:
                self.control.setCurrentIndex(index)
        else:
            self.control.setText(str(text))
        self.control.blockSignals(False)
        self.refresh_dot()

    def set_baseline(self, text, tooltip):
        self._baseline = str(text)
        self.dot.setToolTip(tooltip)
        self.refresh_dot()

    def refresh_dot(self):
        self.dot.setVisible(self.value() != self._baseline)

    def matches(self, query):
        return query in self._search_blob

    def _emit_changed(self, *_):
        self.refresh_dot()
        self.changed.emit()

    # -- menus / browse -------------------------------------------------
    def _context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction(
            "Reset to default (%s)" % (self.setting.default or "empty"),
            lambda: self.reset_requested.emit(self.setting, "default"),
        )
        if self.setting.scope == "tile":
            menu.addAction(
                "Reset to global (%s)" % self._baseline,
                lambda: self.reset_requested.emit(self.setting, "global"),
            )
            menu.addAction(
                "Copy value to global",
                lambda: self.reset_requested.emit(self.setting, "copy"),
            )
        menu.exec(self.mapToGlobal(pos))

    def _browse(self):
        if self.setting.name == "custom_dem":
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Choose DEM file",
                "",
                "DEM files (*.tif *.hgt *.raw *.img);;All files (*)",
            )
            if path:
                current = self.control.text().strip()
                self.control.setText(
                    current + ";" + path if current else path
                )
        else:
            path = QFileDialog.getExistingDirectory(self, "Choose folder")
            if path:
                self.control.setText(path)


class SettingsWindow(QDialog):
    def __init__(self, prefs, active_tile, custom_build_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ortho4XP Settings")
        self.resize(880, 620)
        self.prefs = dict(prefs)
        self.active_tile = active_tile
        self.custom_build_dir = custom_build_dir
        self.tile_written = False

        # Value stores per scope (strings, keyed by setting name)
        defaults = {s.name: s.default for s in SM.settings()}
        self._global_file_vals = SM.read_global_raw()
        self._store_global = dict(defaults)
        self._store_global.update(
            {
                k: v
                for k, v in self._global_file_vals.items()
                if k in defaults
            }
        )
        for s in SM.settings():
            if s.scope == "pref":
                self._store_global[s.name] = str(
                    self.prefs.get(s.name, "") or ""
                )
        self._tile_raw = None
        if active_tile:
            self._tile_raw = SM.read_tile_raw(
                active_tile[0], active_tile[1], custom_build_dir
            )
        self._store_tile = dict(self._store_global)
        if self._tile_raw:
            self._store_tile.update(
                {k: v for k, v in self._tile_raw.items() if k in defaults}
            )
        self._scope = "global"

        self._build_ui()
        self._load_scope_values()

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)

        # Top bar: search + scope
        top = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search settings…")
        self.search_edit.setFixedWidth(240)
        self.search_edit.textChanged.connect(self._apply_filter)
        top.addWidget(self.search_edit)
        top.addStretch(1)
        self.scope_global_btn = QPushButton("Global defaults")
        self.scope_global_btn.setCheckable(True)
        self.scope_global_btn.setChecked(True)
        self.scope_global_btn.clicked.connect(
            lambda: self._switch_scope("global")
        )
        top.addWidget(self.scope_global_btn)
        tile_text = (
            "This tile  %s" % FNAMES.short_latlon(*self.active_tile)
            if self.active_tile
            else "This tile"
        )
        self.scope_tile_btn = QPushButton(tile_text)
        self.scope_tile_btn.setCheckable(True)
        self.scope_tile_btn.setEnabled(self.active_tile is not None)
        if not self.active_tile:
            self.scope_tile_btn.setToolTip(
                "Select a tile on the map to edit its settings."
            )
        self.scope_tile_btn.clicked.connect(
            lambda: self._switch_scope("tile")
        )
        top.addWidget(self.scope_tile_btn)
        root.addLayout(top)

        self.banner = QLabel("")
        self.banner.setStyleSheet(
            "background: palette(alternate-base); padding: 5px;"
            "border-radius: 4px;"
        )
        self.banner.setVisible(False)
        root.addWidget(self.banner)

        # Sidebar + content
        middle = QHBoxLayout()
        side = QVBoxLayout()
        self.category_list = QListWidget()
        self.category_list.setFixedWidth(190)
        for _, title in SM.CATEGORIES:
            self.category_list.addItem(title)
        self.category_list.currentRowChanged.connect(self._category_changed)
        side.addWidget(self.category_list, 1)
        self.advanced_check = QCheckBox("Show advanced")
        self.advanced_check.toggled.connect(self._apply_filter)
        side.addWidget(self.advanced_check)
        middle.addLayout(side)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        content = QWidget()
        self.content_layout = QVBoxLayout(content)
        self.content_layout.setSpacing(2)

        self.rows = {}          # name -> _SettingRow
        self._headers = {}      # category key -> QLabel
        self._lines = {}        # category key -> QFrame separator
        self._advanced_notes = {}
        for key, title in SM.CATEGORIES:
            header = QLabel("<b>%s</b>" % title)
            header.setTextFormat(Qt.RichText)
            header.setContentsMargins(0, 14, 0, 2)
            self.content_layout.addWidget(header)
            self._headers[key] = header
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setStyleSheet("color: palette(mid);")
            self.content_layout.addWidget(line)
            self._lines[key] = line
            hidden_count = 0
            for setting in SM.settings_for(key):
                row = _SettingRow(setting)
                row.changed.connect(self._row_changed)
                row.reset_requested.connect(self._row_reset)
                self.content_layout.addWidget(row)
                self.rows[setting.name] = row
                if setting.advanced:
                    hidden_count += 1
            if hidden_count:
                note = QLabel(
                    "<i>%d advanced setting%s hidden — enable "
                    "“Show advanced”.</i>"
                    % (hidden_count, "s" if hidden_count > 1 else "")
                )
                note.setStyleSheet("color: gray; font-size: 11px;")
                note.setContentsMargins(8, 0, 0, 4)
                self.content_layout.addWidget(note)
                self._advanced_notes[key] = note
        self.content_layout.addStretch(1)
        self.scroll.setWidget(content)
        middle.addWidget(self.scroll, 1)
        root.addLayout(middle, 1)

        # Footer
        footer = QHBoxLayout()
        reset_btn = QPushButton("Reset category…")
        reset_btn.clicked.connect(self._reset_category)
        footer.addWidget(reset_btn)
        footer.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        footer.addWidget(cancel_btn)
        save_btn = QPushButton("Save")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        footer.addWidget(save_btn)
        root.addLayout(footer)

        self.category_list.setCurrentRow(0)
        self._apply_filter()

    # ------------------------------------------------------------------
    # Scope handling
    # ------------------------------------------------------------------
    def _current_store(self):
        return self._store_tile if self._scope == "tile" else \
            self._store_global

    def _capture_scope_values(self):
        store = self._current_store()
        for name, row in self.rows.items():
            if row.setting.scope == "tile" or self._scope == "global":
                store[name] = row.value()
            else:
                # app/pref rows always edit the global store
                self._store_global[name] = row.value()

    def _load_scope_values(self):
        tile_scope = self._scope == "tile"
        store = self._current_store()
        for name, row in self.rows.items():
            s = row.setting
            if s.scope == "tile":
                row.set_value(store.get(name, s.default))
                if tile_scope:
                    row.set_baseline(
                        self._store_global.get(name, s.default),
                        "Overrides the global default "
                        "(right-click to reset)",
                    )
                else:
                    row.set_baseline(
                        s.default, "Differs from the built-in default"
                    )
                row.setEnabled(True)
            else:
                row.set_value(self._store_global.get(name, s.default))
                row.set_baseline(
                    s.default, "Differs from the built-in default"
                )
                row.scope_tag.setVisible(tile_scope and s.scope == "app")
        self.scope_global_btn.setChecked(not tile_scope)
        self.scope_tile_btn.setChecked(tile_scope)
        if tile_scope and self._tile_raw is None:
            self.banner.setText(
                "This tile has no config yet — Save will create one from "
                "the values below."
            )
            self.banner.setVisible(True)
        else:
            self.banner.setVisible(False)

    def _switch_scope(self, scope):
        if scope == self._scope:
            self._load_scope_values()  # re-sync toggle buttons
            return
        self._capture_scope_values()
        self._scope = scope
        self._load_scope_values()

    # ------------------------------------------------------------------
    # Filtering / navigation
    # ------------------------------------------------------------------
    def _apply_filter(self, *_):
        """Show only the sidebar-selected category's settings.

        A non-empty search query overrides the category selection and
        matches across every category (so search never comes up empty just
        because another category is selected); clearing the query returns
        to the selected-category view.
        """
        query = self.search_edit.text().strip().lower()
        advanced = self.advanced_check.isChecked()
        selected = self._selected_category_key()
        visible_by_cat = {key: 0 for key, _ in SM.CATEGORIES}
        for name, row in self.rows.items():
            s = row.setting
            in_category = query or selected is None or s.category == selected
            show = bool(in_category) and (advanced or not s.advanced) and (
                not query or row.matches(query)
            )
            row.setVisible(show)
            if show:
                visible_by_cat[s.category] += 1
        for key, _ in SM.CATEGORIES:
            self._headers[key].setVisible(visible_by_cat[key] > 0)
            self._lines[key].setVisible(visible_by_cat[key] > 0)
            note = self._advanced_notes.get(key)
            if note:
                note.setVisible(
                    not advanced and not query and visible_by_cat[key] > 0
                )

    def _selected_category_key(self):
        """Category key for the sidebar selection, or None if nothing is."""
        index = self.category_list.currentRow()
        if index < 0:
            return None
        return SM.CATEGORIES[index][0]

    def _category_changed(self, index):
        if index < 0:
            return
        self._apply_filter()
        self.scroll.verticalScrollBar().setValue(0)

    # ------------------------------------------------------------------
    # Row actions
    # ------------------------------------------------------------------
    def _row_changed(self):
        pass  # dots update themselves; hook kept for future dirty tracking

    def _row_reset(self, setting, action):
        row = self.rows[setting.name]
        if action == "default":
            row.set_value(setting.default)
        elif action == "global":
            row.set_value(self._store_global.get(setting.name,
                                                 setting.default))
        elif action == "copy":
            self._store_global[setting.name] = row.value()
            if self._scope == "tile":
                row.set_baseline(
                    row.value(),
                    "Overrides the global default (right-click to reset)",
                )

    def _reset_category(self):
        index = self.category_list.currentRow()
        if index < 0:
            return
        key, title = SM.CATEGORIES[index]
        target = "global values" if self._scope == "tile" else "defaults"
        answer = QMessageBox.question(
            self,
            "Reset category",
            "Reset all “%s” settings to %s?" % (title, target),
        )
        if answer != QMessageBox.Yes:
            return
        for setting in SM.settings_for(key):
            row = self.rows[setting.name]
            if self._scope == "tile" and setting.scope == "tile":
                row.set_value(
                    self._store_global.get(setting.name, setting.default)
                )
            else:
                row.set_value(setting.default)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    def _save(self):
        self._capture_scope_values()
        errors = []
        for name, row in self.rows.items():
            s = row.setting
            for store in (self._store_global, self._store_tile):
                ok, normalized, error = SM.coerce(s.name, store.get(
                    name, s.default
                ))
                if not ok:
                    errors.append("%s: %s" % (s.label, error))
                    break
                store[name] = normalized
        if errors:
            QMessageBox.warning(
                self,
                "Invalid values",
                "Fix these before saving:\n\n• " + "\n• ".join(
                    sorted(set(errors))
                ),
            )
            return

        # App + global-tile values that changed vs the file
        changed = {}
        for s in SM.settings():
            if s.scope == "pref":
                continue
            new = self._store_global[s.name]
            old = self._global_file_vals.get(s.name)
            if old is None and new == s.default:
                continue
            if old != new:
                changed[s.name] = new
        try:
            if changed:
                SM.write_global(changed)
                failed = SM.apply_runtime(changed)
                if failed:
                    QMessageBox.warning(
                        self,
                        "Settings",
                        "Saved, but these could not be applied to the "
                        "running app (restart to pick them up): "
                        + ", ".join(failed),
                    )
            # Tile scope: write the tile cfg when the user was editing it
            if self._scope == "tile" and self.active_tile:
                tile_values = {
                    name: self._store_tile[name]
                    for name, row in self.rows.items()
                    if row.setting.scope == "tile"
                }
                SM.write_tile(
                    self.active_tile[0],
                    self.active_tile[1],
                    self.custom_build_dir,
                    tile_values,
                )
                self.tile_written = True
        except OSError as exc:
            QMessageBox.critical(self, "Settings", "Could not save: %s" % exc)
            return

        for s in SM.settings():
            if s.scope == "pref":
                self.prefs[s.name] = self._store_global.get(s.name, "")
        self.accept()

    def result_prefs(self):
        return self.prefs
