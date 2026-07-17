"""Qt dialog for the Allen Coral Atlas reef bathymetry setup.

The GUI face of :mod:`O4_Coral_Atlas` (which stays GUI-free): account
guidance, the guided in-app fetch for one tile, and the manual
download-package path (open the website, drop the zip into the library,
rescan).  Fetch work runs on a daemon thread; results marshal back to
the GUI thread through a queued signal (never call widgets from worker
threads — see the cross-thread gotchas of 2026-07-15).
"""

import os
import threading

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

import O4_Coral_Atlas as CORAL

INSTRUCTIONS = (
    "<b>Allen Coral Atlas — 10 m reef bathymetry</b><br>"
    "The Atlas maps every shallow reef on Earth (CC-BY 4.0) and is the"
    " only open depth source for most Pacific and Asian reefs. Downloads"
    " need a <i>free</i> Atlas account.<br><br>"
    "<b>1.</b> Create the account in your browser: open the Atlas, click"
    " <i>Sign in → Register</i>, confirm the email they send you."
    " Ortho4XP never sees or stores that password permanently — it is"
    " used once per fetch to sign in, and only the session lasts.<br>"
    "<b>2.</b> Either enter your Atlas sign-in below and fetch the tile"
    " you need, or download any region/area package yourself on the"
    " website and drop the zip into the library folder, then Rescan.<br>"
    "<b>3.</b> Rebuild the tile (Step 2.5 + Step 3): tiles covered by"
    " library data use it automatically."
)


class CoralAtlasDialog(QDialog):
    """Account guidance + guided fetch + manual library management."""

    progress_message = Signal(str)
    fetch_finished = Signal(bool, str)

    def __init__(self, parent=None, initial_lat=0, initial_lon=0):
        super().__init__(parent)
        self.setWindowTitle("Allen Coral Atlas reef bathymetry")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        instructions = QLabel(INSTRUCTIONS)
        instructions.setWordWrap(True)
        instructions.setTextFormat(Qt.RichText)
        layout.addWidget(instructions)

        website_row = QHBoxLayout()
        open_site = QPushButton("Open allencoralatlas.org")
        open_site.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(CORAL.ATLAS_REGISTER_URL)
            )
        )
        website_row.addWidget(open_site)
        open_library = QPushButton("Open library folder")
        open_library.clicked.connect(self._open_library_folder)
        website_row.addWidget(open_library)
        rescan = QPushButton("Rescan library")
        rescan.clicked.connect(self._rescan_library)
        website_row.addWidget(rescan)
        layout.addLayout(website_row)

        form = QFormLayout()
        self.email_field = QLineEdit()
        self.email_field.setPlaceholderText("Atlas account email")
        form.addRow("Email", self.email_field)
        self.password_field = QLineEdit()
        self.password_field.setEchoMode(QLineEdit.Password)
        self.password_field.setPlaceholderText(
            "Used once to sign in; never stored"
        )
        form.addRow("Password", self.password_field)
        self.latitude_field = QSpinBox()
        self.latitude_field.setRange(-89, 89)
        self.latitude_field.setValue(int(initial_lat))
        self.longitude_field = QSpinBox()
        self.longitude_field.setRange(-180, 179)
        self.longitude_field.setValue(int(initial_lon))
        tile_row = QHBoxLayout()
        tile_row.addWidget(QLabel("Tile latitude"))
        tile_row.addWidget(self.latitude_field)
        tile_row.addWidget(QLabel("longitude"))
        tile_row.addWidget(self.longitude_field)
        form.addRow("Fetch for", tile_row)
        layout.addLayout(form)

        self.fetch_button = QPushButton(
            "Sign in and fetch this tile's reef bathymetry"
        )
        self.fetch_button.clicked.connect(self._start_fetch)
        layout.addWidget(self.fetch_button)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(160)
        layout.addWidget(self.log_view)

        self.progress_message.connect(self._append_log)
        self.fetch_finished.connect(self._on_fetch_finished)

    # ------------------------------------------------------------------
    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def _open_library_folder(self) -> None:
        os.makedirs(CORAL.library_directory(), exist_ok=True)
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(CORAL.library_directory())
        )

    def _rescan_library(self) -> None:
        entries = CORAL.rescan_library(self.progress_message.emit)
        self._append_log(
            "Library rescanned: %d bathymetry raster(s) indexed."
            % len(entries)
        )

    def _start_fetch(self) -> None:
        email = self.email_field.text().strip()
        password = self.password_field.text()
        if not email or not password:
            self._append_log(
                "Enter your Atlas account email and password first"
                " (Register on the website if you have no account)."
            )
            return
        latitude = self.latitude_field.value()
        longitude = self.longitude_field.value()
        self.fetch_button.setEnabled(False)
        self._append_log(
            "Fetching Allen Coral Atlas bathymetry for tile"
            " %+03d%+04d..." % (latitude, longitude)
        )

        def work():
            try:
                landed = CORAL.guided_fetch_for_tile(
                    latitude,
                    longitude,
                    email,
                    password,
                    progress=self.progress_message.emit,
                )
                self.fetch_finished.emit(
                    landed,
                    "New reef bathymetry is in the library — rebuild the"
                    " tile's masks (Step 2.5 + Step 3) to use it."
                    if landed
                    else "No package landed yet — follow the message"
                    " above.",
                )
            except Exception as error:
                self.fetch_finished.emit(False, str(error))

        threading.Thread(target=work, daemon=True).start()

    def _on_fetch_finished(self, landed: bool, message: str) -> None:
        self.fetch_button.setEnabled(True)
        self._append_log(("Done: " if landed else "Note: ") + message)
        # The password is only needed for the one sign-in.
        self.password_field.clear()
