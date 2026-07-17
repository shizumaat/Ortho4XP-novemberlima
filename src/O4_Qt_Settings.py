"""Categorized settings window for the Ortho4XP Qt UI — the BLENDED view.

One sheet, no scope toggle (docs: Option C, 2026-07-16).  With a tile
active every tile-scope row shows its EFFECTIVE value: inherited rows
carry the global (or default) value quietly; customized rows carry the
orange dot, and hovering any modified row reveals a revert button.  A
pinned "This tile" sidebar section surfaces the settings most commonly
adjusted per tile plus everything customized on this one, and a
"Customized (n)" filter chip collapses the sheet to the overrides alone.

Changes apply IMMEDIATELY (Apple Human Interface Guidelines style):
tile-row edits write sparse overrides through
:func:`O4_Settings_Model.write_tile` (a value equal to the inherited one
REMOVES the override), app/global edits write the global config and
apply to the running app, and the single Done button just closes.  All
file/value semantics live in O4_Settings_Model (headless, tested); this
module is presentation and interaction only.
"""

import re

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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import O4_File_Names as FNAMES
import O4_Settings_Model as SM

DOT_STYLE = "color: #C7861B; font-weight: bold;"
INVALID_STYLE = "border: 1px solid #C03030;"
PATH_SUFFIXES = ("_dir", "_src", "_path", "_src_alternate")
TILE_SIDEBAR_TITLE = "★ This tile"


def _is_path_setting(setting):
    return setting.name.endswith(PATH_SUFFIXES) or setting.name in (
        "xplane_dir",
        "output_dir",
        "custom_overlay_src",
    )


def html_escape(text):
    """Escape &, <, > so provider-supplied text is safe as rich text."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


_URL_IN_TEXT = re.compile(r"(https?://[^\s<>]+)")


def _linkify_urls(escaped_text):
    """Turn bare http(s) URLs in already-escaped text into <a> links."""
    return _URL_IN_TEXT.sub(
        lambda match: '<a href="%s">%s</a>'
        % (match.group(1), match.group(1)),
        escaped_text,
    )


class _SettingRow(QWidget):
    """One label + control + description row with a modified indicator,
    a hover revert button, and immediate-apply commit signalling."""

    committed = Signal(object)               # row (value already validated)
    reset_requested = Signal(object, str)    # setting, "inherit"|"default"|"copy"

    def __init__(self, setting, parent=None):
        super().__init__(parent)
        self.setting = setting
        self._baseline = setting.default
        self._mixed = False
        self._any_override = False
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
            self.control.toggled.connect(self._commit)
        elif setting.values:
            # Menu shows the human-readable title; the raw config value
            # rides along as item data so storage never sees the label.
            self.control = QComboBox()
            for raw in setting.values:
                self.control.addItem(setting.label_for(raw), raw)
            self.control.currentIndexChanged.connect(self._commit)
        else:
            self.control = QLineEdit()
            self.control.setFixedWidth(
                280 if _is_path_setting(setting) else 110
            )
            # Free-text commits when editing FINISHES (Enter / focus out),
            # so half-typed numbers are never written; the dot still
            # tracks live keystrokes.
            self.control.editingFinished.connect(self._commit)
            self.control.textEdited.connect(lambda *_: self.refresh_dot())
        top.addWidget(self.control)
        if isinstance(self.control, QLineEdit) and _is_path_setting(setting):
            browse = QPushButton("…")
            browse.setFixedWidth(28)
            browse.clicked.connect(self._browse)
            top.addWidget(browse)
        # Per-setting revert, ALWAYS VISIBLE on the right for
        # discoverability (the undo for an immediately-applied change
        # lives where the change was made); enabled only when there is
        # something to revert.
        self.revert_button = QToolButton()
        self.revert_button.setText("↺")
        self.revert_button.setAutoRaise(True)
        self.revert_button.setFixedSize(20, 20)
        self.revert_button.setEnabled(False)
        self.revert_button.setToolTip("Revert to the inherited value")
        self.revert_button.clicked.connect(
            lambda: self.reset_requested.emit(self.setting, "inherit")
        )
        top.addWidget(self.revert_button)
        lay.addLayout(top)

        if setting.hint:
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
        self._mixed = False
        self.control.blockSignals(True)
        if isinstance(self.control, QCheckBox):
            self.control.setTristate(False)
            self.control.setChecked(str(text).strip() in ("True", "true", "1"))
        elif isinstance(self.control, QComboBox):
            index = self.control.findData(str(text))
            if index >= 0:
                self.control.setCurrentIndex(index)
        else:
            self.control.setPlaceholderText("")
            self.control.setText(str(text))
        self.control.blockSignals(False)
        self.mark_valid()
        self.refresh_dot()

    def set_mixed(self, any_override):
        """Display the multi-tile MIXED state: the selected tiles disagree.

        The control goes value-less (blank text / dash menu / partial
        checkmark); the first user edit replaces the mixed state with one
        value for every selected tile.  ``any_override`` drives the dot:
        at least one selected tile overrides the global value.
        """
        self._mixed = True
        self._any_override = bool(any_override)
        self.control.blockSignals(True)
        if isinstance(self.control, QCheckBox):
            self.control.setTristate(True)
            self.control.setCheckState(Qt.PartiallyChecked)
        elif isinstance(self.control, QComboBox):
            self.control.setPlaceholderText("— Mixed —")
            self.control.setCurrentIndex(-1)
        else:
            self.control.setText("")
            self.control.setPlaceholderText("Mixed")
        self.control.blockSignals(False)
        self.mark_valid()
        self.refresh_dot()

    def is_mixed(self):
        return self._mixed

    def set_baseline(self, text, tooltip):
        self._baseline = str(text)
        self.dot.setToolTip(tooltip)
        self.refresh_dot()

    def baseline(self):
        return self._baseline

    def is_modified(self):
        if self._mixed:
            return self._any_override
        return self.value() != self._baseline

    def refresh_dot(self):
        modified = self.is_modified()
        self.dot.setVisible(modified)
        self.revert_button.setEnabled(modified)

    def mark_invalid(self, message):
        self.control.setStyleSheet(INVALID_STYLE)
        self.control.setToolTip(message)

    def mark_valid(self):
        self.control.setStyleSheet("")
        self.control.setToolTip("")

    def matches(self, query):
        return query in self._search_blob

    def _commit(self, *_):
        if (self._mixed and isinstance(self.control, QLineEdit)
                and not self.control.isModified()):
            # Focus merely passed through a mixed field: nothing typed,
            # nothing to apply — the mixed state stands.
            return
        if self._mixed:
            # The user picked one value for every selected tile: the
            # mixed display resolves (a tristate checkbox goes two-state
            # again so it can never be cycled back to partial).
            self._mixed = False
            if isinstance(self.control, QCheckBox):
                self.control.setTristate(False)
        self.refresh_dot()
        self.committed.emit(self)

    # -- menus / browse -------------------------------------------------
    def _context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction(
            "Reset to default (%s)" % (self.setting.default or "empty"),
            lambda: self.reset_requested.emit(self.setting, "default"),
        )
        if self.setting.scope == "tile":
            menu.addAction(
                "Revert to global (%s)" % self._baseline,
                lambda: self.reset_requested.emit(self.setting, "inherit"),
            )
            menu.addAction(
                "Set as global default",
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
                self._commit()
        else:
            path = QFileDialog.getExistingDirectory(self, "Choose folder")
            if path:
                self.control.setText(path)
                self._commit()


class _SignInDialog(QDialog):
    """Credentials prompt for one provider account session.

    Adapts to the definition's credential kind: username + password for
    cookie sessions and HTTP Basic providers, a single key field for
    API-key providers.  The sign-in itself (network) runs on a worker
    thread; the result is marshalled back through a signal (never touch
    widgets from the thread, never QTimer.singleShot from a Python
    thread).
    """

    _sign_in_finished = Signal(object)  # None on success, else LoginError

    def __init__(self, definition, parent=None):
        super().__init__(parent)
        import O4_Authenticated_Sessions as SESSIONS

        self._sessions = SESSIONS
        self.definition = definition
        self._api_key_kind = (
            SESSIONS.credential_kind(definition)
            == SESSIONS.CREDENTIAL_KIND_API_KEY
        )
        reference_url = str(
            definition.get("login_url")
            or definition.get("registration_url")
            or ""
        )
        service_host = (
            reference_url.split("/")[2] if "://" in reference_url else ""
        )
        self.setWindowTitle(
            "Sign in — %s" % (definition.get("attribution") or service_host)
        )
        layout = QVBoxLayout(self)
        if self._api_key_kind:
            introduction = QLabel(
                "This provider requires a (free) account at %s and an "
                "API key generated there.  Paste the key below; it is "
                "stored in the system keychain." % service_host
            )
        else:
            introduction = QLabel(
                "This provider requires a (free) account at %s.  Your "
                "password is sent only to that service." % service_host
            )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)

        registration_url = definition.get("registration_url")
        if registration_url:
            registration_link = QLabel(
                '<a href="%s">No account yet?  Create one here.</a>'
                % registration_url
            )
            registration_link.setTextFormat(Qt.RichText)
            registration_link.setOpenExternalLinks(True)
            layout.addWidget(registration_link)

        steps = SESSIONS.setup_steps(definition)
        if steps:
            # Some accounts need work before credentials will work at all
            # (Sweden: order the free product; Denmark: copy a token).
            # Show the checklist right here so the user does it first.
            # Any http(s) URL in a step becomes a clickable link.
            items = "".join(
                "<li>%s</li>" % _linkify_urls(html_escape(step))
                for step in steps
            )
            steps_label = QLabel(
                "<b>Setup</b><ol style='margin-left:-20px;'>%s</ol>" % items
            )
            steps_label.setTextFormat(Qt.RichText)
            steps_label.setWordWrap(True)
            steps_label.setOpenExternalLinks(True)
            layout.addWidget(steps_label)

        self.username_edit = QLineEdit()
        self.username_edit.setPlaceholderText("Username or email address")
        layout.addWidget(self.username_edit)
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText(
            "API key" if self._api_key_kind else "Password"
        )
        self.password_edit.setEchoMode(QLineEdit.Password)
        layout.addWidget(self.password_edit)
        if self._api_key_kind:
            # One secret string IS the whole credential.
            self.username_edit.setVisible(False)

        self.remember_check = QCheckBox(
            "Remember on this device (stored in the system keychain)"
        )
        if SESSIONS.credential_store_available():
            self.remember_check.setChecked(True)
        else:
            self.remember_check.setChecked(False)
            self.remember_check.setEnabled(False)
            self.remember_check.setToolTip(
                "No system keychain is available on this machine; the "
                "session lasts until it expires, then sign in again."
            )
        if self._api_key_kind:
            # An API key only works stored: it is read back at build
            # time, unlike a session which persists as cookies.
            self.remember_check.setChecked(True)
            self.remember_check.setVisible(False)
        layout.addWidget(self.remember_check)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #C03030;")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addWidget(cancel_button)
        self.sign_in_button = QPushButton("Sign in")
        self.sign_in_button.setDefault(True)
        self.sign_in_button.clicked.connect(self._start_sign_in)
        buttons.addWidget(self.sign_in_button)
        layout.addLayout(buttons)

        self._sign_in_finished.connect(self._finish_sign_in)

    def _start_sign_in(self):
        import threading

        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if self._api_key_kind:
            if not password.strip():
                self.error_label.setText("Paste an API key.")
                self.error_label.setVisible(True)
                return
        elif not username or not password:
            self.error_label.setText("Enter both a username and a password.")
            self.error_label.setVisible(True)
            return
        self.error_label.setVisible(False)
        self.sign_in_button.setEnabled(False)
        self.sign_in_button.setText("Signing in…")
        for widget in (self.username_edit, self.password_edit,
                       self.remember_check):
            widget.setEnabled(False)
        remember = self.remember_check.isChecked()
        api_key_kind = self._api_key_kind

        def _worker():
            try:
                if api_key_kind:
                    self._sessions.sign_in_api_key(
                        self.definition, password, remember=remember
                    )
                else:
                    self._sessions.sign_in(
                        self.definition, username, password,
                        remember=remember,
                    )
            except self._sessions.LoginError as error:
                self._sign_in_finished.emit(error)
                return
            except Exception as error:  # network hiccups, anything odd
                self._sign_in_finished.emit(
                    self._sessions.LoginError(str(error))
                )
                return
            self._sign_in_finished.emit(None)

        threading.Thread(target=_worker, daemon=True).start()

    def _finish_sign_in(self, error):
        if error is None:
            self.accept()
            return
        self.error_label.setText(str(error))
        self.error_label.setVisible(True)
        self.sign_in_button.setEnabled(True)
        self.sign_in_button.setText("Sign in")
        for widget in (self.username_edit, self.password_edit,
                       self.remember_check):
            widget.setEnabled(True)
        self.password_edit.setFocus()


class _ProviderSignInSection(QWidget):
    """Account sign-ins for providers that require one (app-level).

    Lives at the end of the Elevation category.  Status is derived from
    LOCAL state only (saved session cookies / stored account) — building
    the settings window never touches the network; the build pipeline
    verifies and re-logs sessions on its own (O4_Authenticated_Sessions.
    ensure_session).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        import O4_Authenticated_Sessions as SESSIONS

        self._sessions = SESSIONS
        self._search_haystack = "sign in account provider "
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QLabel("<b>Provider accounts</b>")
        header.setTextFormat(Qt.RichText)
        header.setContentsMargins(0, 14, 0, 2)
        layout.addWidget(header)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet("color: palette(mid);")
        layout.addWidget(line)
        note = QLabel(
            "These data sources need a signed-in account.  Sessions are "
            "kept alive automatically once you sign in."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(note)
        self._rows = {}  # session_name -> (definition, status QLabel, out btn)
        for definition in self._session_definitions():
            self._add_row(layout, definition)
        self._refresh_statuses()

    @staticmethod
    def _all_account_definitions():
        """Every enabled definition that declares an account session."""
        import O4_Airport_Elevation_Insets as ELEVATION_PROVIDERS

        if not ELEVATION_PROVIDERS.elevation_providers_dict:
            ELEVATION_PROVIDERS.initialize_elevation_providers_dict()
        result = []
        for definition in sorted(
            ELEVATION_PROVIDERS.elevation_providers_dict.values(),
            key=lambda d: str(d.get("code")),
        ):
            if not definition.get("session_name"):
                continue
            if not definition.get("enabled", True):
                continue
            result.append(definition)
        return result

    @classmethod
    def _session_definitions(cls):
        """One representative definition per account session."""
        by_session = {}
        for definition in cls._all_account_definitions():
            by_session.setdefault(definition["session_name"], definition)
        return list(by_session.values())

    @classmethod
    def _definitions_sharing_session(cls, session_name):
        """All enabled definitions using one account session."""
        return [
            definition
            for definition in cls._all_account_definitions()
            if definition["session_name"] == session_name
        ]

    def _add_row(self, layout, definition):
        session_name = definition["session_name"]
        row = QHBoxLayout()
        codes = ", ".join(
            sorted(
                other.get("code", "")
                for other in self._definitions_sharing_session(session_name)
            )
        )
        title = QLabel(
            "%s  <span style='color: gray;'>(%s)</span>"
            % (definition.get("attribution") or session_name, codes)
        )
        title.setTextFormat(Qt.RichText)
        # Long attributions wrap rather than forcing the row (and with
        # it the whole settings window) wider than the viewport.
        title.setWordWrap(True)
        if definition.get("registration_url"):
            title.setToolTip(
                "Create an account: %s" % definition["registration_url"]
            )
        row.addWidget(title, 1)
        status = QLabel("")
        row.addWidget(status)
        sign_in_button = QPushButton("Sign in…")
        sign_in_button.clicked.connect(
            lambda _=False, d=definition: self._sign_in(d)
        )
        row.addWidget(sign_in_button)
        sign_out_button = QPushButton("Sign out")
        sign_out_button.clicked.connect(
            lambda _=False, n=session_name: self._sign_out(n)
        )
        row.addWidget(sign_out_button)
        container = QWidget()
        container.setLayout(row)
        layout.addWidget(container)
        self._rows[session_name] = (definition, status, sign_out_button)
        self._search_haystack += " ".join(
            str(definition.get(key, ""))
            for key in ("code", "attribution", "session_name")
        ).lower()

    def matches(self, query):
        return query in self._search_haystack

    def _refresh_statuses(self):
        import os

        for session_name, (definition, status, sign_out_button) in (
            self._rows.items()
        ):
            if (
                self._sessions.credential_kind(definition)
                == self._sessions.CREDENTIAL_KIND_API_KEY
            ):
                has_api_key = bool(
                    self._sessions.load_api_key(session_name)
                )
                status.setText(
                    "<span style='color: #2E7D32;'>API key stored</span>"
                    if has_api_key
                    else "<span style='color: gray;'>No API key</span>"
                )
                status.setTextFormat(Qt.RichText)
                sign_out_button.setEnabled(has_api_key)
                continue
            credentials = self._sessions.load_credentials(session_name)
            has_cookies = os.path.isfile(
                self._sessions.cookie_file_path(session_name)
            )
            if credentials is not None:
                status.setText(
                    "<span style='color: #2E7D32;'>Signed in as %s</span>"
                    % credentials[0]
                )
            elif has_cookies:
                status.setText(
                    "<span style='color: #2E7D32;'>Session saved</span>"
                )
            else:
                status.setText(
                    "<span style='color: gray;'>Not signed in</span>"
                )
            status.setTextFormat(Qt.RichText)
            sign_out_button.setEnabled(credentials is not None or has_cookies)

    def _sign_in(self, definition):
        dialog = _SignInDialog(definition, self)
        if dialog.exec():
            self._refresh_statuses()

    def _sign_out(self, session_name):
        self._sessions.sign_out(session_name)
        self._refresh_statuses()


class SettingsWindow(QDialog):
    """The blended settings sheet (Option C), multi-tile aware.

    ``tiles`` is the map selection: zero tiles edits global defaults;
    one tile blends it over global; several tiles edit them TOGETHER —
    settings the tiles disagree on display a MIXED state, and entering a
    value applies it to every selected tile.
    """

    def __init__(self, prefs, tiles, custom_build_dir, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ortho4XP Settings")
        self.resize(880, 620)
        self.prefs = dict(prefs)
        if tiles and isinstance(tiles, tuple) and isinstance(tiles[0], int):
            tiles = [tiles]  # a bare (lat, lon) is one tile
        self.tiles = sorted(tiles or [])
        self.custom_build_dir = custom_build_dir
        self.tile_written = False

        # Global-effective values (registry default overlaid by the global
        # config file), plus preference values from the caller.
        self._global_file_vals = SM.read_global_raw()
        self._global_vals = {}
        for setting in SM.settings():
            if setting.scope == "pref":
                self._global_vals[setting.name] = str(
                    self.prefs.get(setting.name, "") or ""
                )
            else:
                self._global_vals[setting.name] = SM.global_effective_value(
                    setting.name, self._global_file_vals
                )
        # Sparse overrides per selected tile (blended at display time).
        self._overrides_by_tile = {}
        for tile in self.tiles:
            blended = SM.effective_tile_settings(
                tile[0], tile[1], custom_build_dir
            )
            self._overrides_by_tile[tile] = {
                name: value
                for name, (value, origin) in blended.items()
                if origin == "tile"
            }

        self._width_clamped = False
        self._build_ui()
        self._load_values()

    def showEvent(self, event):
        super().showEvent(event)
        self._clamp_width_to_content()

    def _clamp_width_to_content(self):
        """Keep every description wrapped inside the window width.

        The scroll area re-wraps description labels only down to the
        content widget's minimum width; narrower than that it scrolls
        horizontally — which macOS draws as an invisible overlay
        scrollbar, so long descriptions simply look clipped at the
        right edge.  The dialog's own minimum does not cover this
        because QScrollArea never propagates its content's minimum.
        Clamp the dialog minimum so the viewport always fits the
        widest row of ANY category (hidden categories count: switching
        category must not start clipping).  Needs the window shown
        once, to measure the chrome between window and viewport.
        """
        if self._width_clamped:
            return
        self._width_clamped = True
        widest_row = 0
        for index in range(self.content_layout.count()):
            widget = self.content_layout.itemAt(index).widget()
            if widget is None:
                continue
            width = widget.minimumSizeHint().width()
            scope_tag = getattr(widget, "scope_tag", None)
            if (
                scope_tag is not None
                and scope_tag.isVisibleTo(widget)
                and not scope_tag.isVisible()
            ):
                # A hidden row measures without its pending "app-wide"
                # tag; count the tag so switching to that row's
                # category never starts clipping.
                width += scope_tag.sizeHint().width() + 8
            widest_row = max(widest_row, width)
        margins = self.content_layout.contentsMargins()
        chrome = self.width() - self.scroll.viewport().width()
        scrollbar_allowance = (
            self.scroll.verticalScrollBar().sizeHint().width()
        )
        self.setMinimumWidth(
            widest_row + margins.left() + margins.right()
            + chrome + scrollbar_allowance
        )

    @property
    def blended(self):
        return bool(self.tiles)

    def _override_union(self):
        """Names overridden on AT LEAST one selected tile."""
        union = set()
        for overrides in self._overrides_by_tile.values():
            union.update(overrides)
        return union

    def _tiles_label(self):
        if len(self.tiles) == 1:
            return FNAMES.short_latlon(*self.tiles[0])
        return "%d tiles" % len(self.tiles)

    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)

        # Top bar: search + editing context + customized filter
        top = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search settings…")
        self.search_edit.setFixedWidth(240)
        self.search_edit.textChanged.connect(self._apply_filter)
        top.addWidget(self.search_edit)
        top.addStretch(1)
        self.context_label = QLabel()
        self.context_label.setStyleSheet("color: gray;")
        top.addWidget(self.context_label)
        self.customized_chip = QPushButton()
        self.customized_chip.setCheckable(True)
        self.customized_chip.setVisible(self.blended)
        self.customized_chip.toggled.connect(self._apply_filter)
        top.addWidget(self.customized_chip)
        root.addLayout(top)

        # Sidebar + content
        middle = QHBoxLayout()
        side = QVBoxLayout()
        self.category_list = QListWidget()
        self.category_list.setFixedWidth(190)
        self._sidebar_offset = 0
        if self.blended:
            self.category_list.addItem(
                "★ These tiles (%d)" % len(self.tiles)
                if len(self.tiles) > 1
                else "%s  %s" % (TILE_SIDEBAR_TITLE,
                                 FNAMES.short_latlon(*self.tiles[0]))
            )
            self._sidebar_offset = 1
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

        # The pinned This-tile view has its own header line.
        self.tile_header = QLabel()
        self.tile_header.setTextFormat(Qt.RichText)
        self.tile_header.setContentsMargins(0, 14, 0, 2)
        self.tile_header.setVisible(False)
        self.content_layout.addWidget(self.tile_header)

        self.rows = {}          # name -> _SettingRow
        self._headers = {}      # category key -> QLabel
        self._lines = {}        # category key -> QFrame separator
        self._advanced_notes = {}
        self._provider_signin_section = None
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
                row.committed.connect(self._row_committed)
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
            if key == "elevation":
                # Account sign-ins for providers that need one; only
                # present when at least one such provider is installed.
                section = _ProviderSignInSection()
                if section._rows:
                    self._provider_signin_section = section
                    self.content_layout.addWidget(section)
                else:
                    section.deleteLater()
        self.content_layout.addStretch(1)
        self.scroll.setWidget(content)
        middle.addWidget(self.scroll, 1)
        root.addLayout(middle, 1)

        # Footer: modified-value key, reset actions, Done.
        footer = QHBoxLayout()
        self.legend = QLabel("")
        self.legend.setTextFormat(Qt.RichText)
        self.legend.setStyleSheet("color: gray; font-size: 11px;")
        footer.addWidget(self.legend)
        footer.addStretch(1)
        self.reset_category_btn = QPushButton()
        self.reset_category_btn.clicked.connect(self._reset_category)
        footer.addWidget(self.reset_category_btn)
        self.reset_all_btn = QPushButton()
        self.reset_all_btn.clicked.connect(self._reset_all)
        footer.addWidget(self.reset_all_btn)
        footer.addSpacing(16)
        done_btn = QPushButton("Done")
        done_btn.setDefault(True)
        done_btn.clicked.connect(self.accept)
        footer.addWidget(done_btn)
        root.addLayout(footer)

        if self.blended:
            self.context_label.setText(
                ("Tile %s" % FNAMES.short_latlon(*self.tiles[0])
                 if len(self.tiles) == 1
                 else "%d tiles" % len(self.tiles))
                + " · blended with global"
            )
            legend = (
                '<span style="%s">●</span> overrides the global value'
                " &nbsp;·&nbsp; ↺ reverts a setting" % DOT_STYLE
            )
            if len(self.tiles) > 1:
                legend += " &nbsp;·&nbsp; blank = mixed across tiles"
            self.legend.setText(legend)
        else:
            self.context_label.setText("Editing global defaults")
            self.legend.setText(
                '<span style="%s">●</span> changed from default'
                " &nbsp;·&nbsp; ↺ reverts a setting" % DOT_STYLE
            )

        self.category_list.setCurrentRow(0)
        self._update_footer_actions()
        self._apply_filter()

    # ------------------------------------------------------------------
    # Value display
    # ------------------------------------------------------------------
    def _load_values(self):
        for name, row in self.rows.items():
            setting = row.setting
            if self.blended and setting.scope == "tile":
                row.revert_button.setToolTip("Revert to the global value")
                inherited = self._global_vals[name]
                row.set_baseline(
                    inherited,
                    "Overrides the global value"
                    if len(self.tiles) == 1
                    else "Overrides the global value on at least one"
                         " selected tile",
                )
                effective = {
                    overrides.get(name, inherited)
                    for overrides in self._overrides_by_tile.values()
                }
                if len(effective) > 1:
                    row.set_mixed(name in self._override_union())
                else:
                    row.set_value(effective.pop())
            else:
                row.revert_button.setToolTip(
                    "Revert to the built-in default")
                row.set_value(self._global_vals.get(name, setting.default))
                row.set_baseline(
                    setting.default, "Differs from the built-in default"
                )
                row.scope_tag.setVisible(
                    self.blended and setting.scope in ("app", "pref")
                )
        self._refresh_customized_chip()

    def _refresh_customized_chip(self):
        self.customized_chip.setText(
            "Customized (%d)" % len(self._override_union())
        )

    # ------------------------------------------------------------------
    # Immediate apply
    # ------------------------------------------------------------------
    def _row_committed(self, row):
        setting = row.setting
        ok, normalized, error = SM.coerce(setting.name, row.value())
        if not ok:
            row.mark_invalid("Not saved: %s" % error)
            return
        row.mark_valid()
        if self.blended and setting.scope == "tile":
            self._write_tile_value(setting.name, normalized)
        else:
            self._write_global_value(setting, normalized)

    def _write_tile_value(self, name, normalized):
        """Apply one value to EVERY selected tile (sparse per tile)."""
        for tile in self.tiles:
            try:
                SM.write_tile(
                    tile[0], tile[1], self.custom_build_dir,
                    {name: normalized},
                )
            except OSError as exc:
                QMessageBox.critical(
                    self, "Settings", "Could not save: %s" % exc
                )
                return
            if SM.values_equivalent(
                name, normalized, self._global_vals[name]
            ):
                self._overrides_by_tile[tile].pop(name, None)
            else:
                self._overrides_by_tile[tile][name] = normalized
        self.tile_written = True
        row = self.rows[name]
        row.set_value(normalized)  # uniform across tiles now
        self._refresh_customized_chip()
        # The This-tile view and the Customized chip both derive their
        # row sets from the override set, which just changed.
        if (self.customized_chip.isChecked()
                or self._selected_category_key() == "__tile__"):
            self._apply_filter()

    def _write_global_value(self, setting, normalized):
        if setting.scope == "pref":
            self.prefs[setting.name] = normalized
            self._global_vals[setting.name] = normalized
            self.rows[setting.name].set_value(normalized)
            return
        try:
            SM.write_global({setting.name: normalized})
        except OSError as exc:
            QMessageBox.critical(
                self, "Settings", "Could not save: %s" % exc
            )
            return
        SM.apply_runtime({setting.name: normalized})
        self._global_file_vals[setting.name] = normalized
        self._global_vals[setting.name] = normalized
        row = self.rows[setting.name]
        row.set_value(normalized)
        # Inherited tile rows track the global value live.
        if self.blended and setting.scope == "tile":
            if setting.name not in self._override_union():
                row.set_baseline(normalized, "Overrides the global value")
                row.set_value(normalized)

    # ------------------------------------------------------------------
    # Filtering / navigation
    # ------------------------------------------------------------------
    def _apply_filter(self, *_):
        """Show the sidebar-selected view.

        A non-empty search query overrides the selection and matches
        across every category; the Customized chip narrows further to
        this tile's overrides; the pinned This-tile view shows the
        curated commonly-adjusted settings plus every override.
        """
        query = self.search_edit.text().strip().lower()
        advanced = self.advanced_check.isChecked()
        selected = self._selected_category_key()
        tile_view = self.blended and selected == "__tile__"
        customized_only = (
            self.blended and self.customized_chip.isChecked()
        )
        override_union = self._override_union() if self.blended else set()
        visible_by_cat = {key: 0 for key, _ in SM.CATEGORIES}
        for name, row in self.rows.items():
            s = row.setting
            if tile_view and not query:
                show = s.name in SM.CURATED_TILE_SETTINGS or (
                    s.name in override_union
                )
            else:
                in_category = (
                    query or selected is None or s.category == selected
                )
                show = bool(in_category) and (
                    advanced or not s.advanced or s.name in override_union
                ) and (not query or row.matches(query))
            if customized_only:
                show = show and s.name in override_union
            row.setVisible(show)
            if show and not tile_view:
                visible_by_cat[s.category] += 1
        for key, _ in SM.CATEGORIES:
            self._headers[key].setVisible(
                not tile_view and visible_by_cat[key] > 0
            )
            self._lines[key].setVisible(
                not tile_view and visible_by_cat[key] > 0
            )
            note = self._advanced_notes.get(key)
            if note:
                note.setVisible(
                    not tile_view and not advanced and not query
                    and visible_by_cat[key] > 0
                )
        if self._provider_signin_section is not None:
            # App-level, so it hides in the tile-focused views; a search
            # query matches it by provider name / "sign in".
            self._provider_signin_section.setVisible(
                not tile_view
                and not customized_only
                and (
                    self._provider_signin_section.matches(query)
                    if query
                    else selected in (None, "elevation")
                )
            )
        if tile_view:
            count = len(override_union)
            subject = (
                "This tile" if len(self.tiles) == 1
                else "These %d tiles" % len(self.tiles)
            )
            self.tile_header.setText(
                "<b>%s — commonly adjusted, plus %s %d customization%s</b>"
                % (subject,
                   "its" if len(self.tiles) == 1 else "their",
                   count, "" if count == 1 else "s")
            )
        self.tile_header.setVisible(tile_view and not query)

    def _selected_category_key(self):
        """Category key for the sidebar selection ("__tile__" for the
        pinned This-tile view), or None when nothing is selected."""
        index = self.category_list.currentRow()
        if index < 0:
            return None
        if self.blended and index == 0:
            return "__tile__"
        return SM.CATEGORIES[index - self._sidebar_offset][0]

    def _category_changed(self, index):
        if index < 0:
            return
        self._update_footer_actions()
        self._apply_filter()
        self.scroll.verticalScrollBar().setValue(0)

    def _update_footer_actions(self):
        """Defaults-resets act on the GLOBAL layer everywhere; the tile
        view swaps the All button for the tile-override reset and the
        category button (a global-layer concept) goes quiet."""
        tile_view = self._selected_category_key() == "__tile__"
        self.reset_category_btn.setText("Reset Category to Defaults")
        self.reset_category_btn.setEnabled(not tile_view)
        self.reset_all_btn.setText(
            "Reset to Global" if tile_view else "Reset All to Defaults"
        )

    # ------------------------------------------------------------------
    # Row actions
    # ------------------------------------------------------------------
    def _row_reset(self, setting, action):
        row = self.rows[setting.name]
        if action == "default":
            row.set_value(setting.default)
            self._row_committed(row)
        elif action == "inherit":
            if self.blended and setting.scope == "tile":
                row.set_value(self._global_vals[setting.name])
            else:
                row.set_value(setting.default)
            self._row_committed(row)
        elif action == "copy":
            self._write_global_value(setting, row.value())
            if self.blended and setting.scope == "tile":
                # The row now equals the global value: drop the redundant
                # tile override so the file reflects the blend.
                self._write_tile_value(setting.name, row.value())

    def _reset_global_layer(self, settings):
        """Return the GLOBAL layer to built-in defaults for *settings*.

        Tile overrides are untouched (defaults are a global-layer
        concept; dropping a tile's overrides is the This-tile view's
        "Reset to Global").  Preference rows (machine paths like the
        X-Plane and output folders) are never reset by a bulk action.
        """
        for setting in settings:
            if setting.scope == "pref":
                continue
            self._write_global_value(setting, setting.default)

    def _reset_tiles_to_global(self):
        """Drop every override on every selected tile."""
        for name in sorted(self._override_union()):
            self._write_tile_value(name, self._global_vals[name])
        self._load_values()

    def _reset_category(self):
        key = self._selected_category_key()
        if key is None or key == "__tile__":
            return  # the button is disabled on the This-tile view
        title = dict(SM.CATEGORIES)[key]
        answer = QMessageBox.question(
            self,
            "Reset category",
            "Reset every “%s” setting to its built-in default?"
            "\n\nThis changes the GLOBAL values%s." % (
                title,
                "; per-tile customizations are kept"
                if self.blended else ""),
        )
        if answer != QMessageBox.Yes:
            return
        self._reset_global_layer(SM.settings_for(key))

    def _reset_all(self):
        if self._selected_category_key() == "__tile__":
            question = (
                "Return ALL settings of the selected tile%s to the global"
                " values?\n\nEvery per-tile customization is removed;"
                " global and app-wide settings are not touched."
                % ("s" if len(self.tiles) > 1 else ""))
            answer = QMessageBox.question(
                self, "Reset to global", question)
            if answer != QMessageBox.Yes:
                return
            self._reset_tiles_to_global()
            return
        question = (
            "Reset ALL settings to their built-in defaults?"
            "\n\nThis changes the GLOBAL values%s. Folder locations under"
            " General (X-Plane, output) are kept." % (
                "; per-tile customizations are kept"
                if self.blended else ""))
        answer = QMessageBox.question(self, "Reset all settings", question)
        if answer != QMessageBox.Yes:
            return
        self._reset_global_layer(SM.settings())

    # ------------------------------------------------------------------
    # Closing (changes are already applied; every close path accepts)
    # ------------------------------------------------------------------
    def reject(self):
        self.accept()

    def result_prefs(self):
        return self.prefs
