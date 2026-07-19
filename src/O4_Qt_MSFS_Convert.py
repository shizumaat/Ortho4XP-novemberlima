"""Qt dialog for the Tools menu "Convert MSFS airport" command.

Thin GUI wrapper around O4_MSFS_Airport_Convert.convert_msfs_airport:
pick an MSFS package folder, choose the new pack name, run the
conversion on a background thread with live progress, and summarize the
result. Cancellation goes through the standard O4_UI_Utils.red_flag.

The core conversion logic lives in src/O4_MSFS_Airport_Convert.py (no
GUI imports there); this module owns only widgets and thread plumbing.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

import O4_File_Names as FNAMES
import O4_UI_Utils as UI


def default_dsftool_path() -> Path:
    """The bundled DSFTool for this platform (same logic as overlays)."""
    if os.name == "posix" and os.uname().sysname == "Darwin":
        return Path(FNAMES.Utils_dir) / "mac" / "DSFTool"
    if os.name == "nt":
        return Path(FNAMES.Utils_dir) / "win" / "DSFTool.exe"
    return Path(FNAMES.Utils_dir) / "lin" / "DSFTool"


class MSFSConvertDialog(QDialog):
    """Convert an MSFS airport scenery package into a Custom Scenery pack."""

    progress_update = Signal(int, str)
    conversion_finished = Signal(bool, str, object)  # ok, message, report|None

    def __init__(self, parent, xplane_directory: str):
        super().__init__(parent)
        self.setWindowTitle("Convert MSFS airport")
        self.setMinimumWidth(620)
        self._xplane_directory = Path(xplane_directory) if xplane_directory else None
        self._worker: threading.Thread | None = None

        layout = QVBoxLayout(self)
        introduction = QLabel(
            "Converts an MSFS airport package into a new X-Plane Custom "
            "Scenery pack: models are converted to OBJ8, placed in an "
            "overlay DSF, the airport's default apt.dat is copied from "
            "Global Airports, and the default gateway 3-D underneath the "
            "converted objects is suppressed with exclusion zones "
            "(Global Airports itself is never modified).\n\n"
            "Converted third-party scenery is for personal use unless the "
            "original author grants redistribution rights."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)

        form = QFormLayout()
        self.package_edit = QLineEdit()
        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self._browse_package)
        package_row = QHBoxLayout()
        package_row.addWidget(self.package_edit)
        package_row.addWidget(browse_button)
        form.addRow("MSFS package folder:", package_row)

        self.pack_name_edit = QLineEdit()
        self.pack_name_edit.setPlaceholderText(
            "Leave empty to name it after the detected airport"
        )
        form.addRow("New pack name:", self.pack_name_edit)

        custom_scenery = self._custom_scenery_directory()
        self.target_label = QLabel(
            str(custom_scenery) if custom_scenery else
            "X-Plane folder not configured (set it in Settings first)"
        )
        self.target_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        form.addRow("Installs into:", self.target_label)
        layout.addLayout(form)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        layout.addWidget(self.progress)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(160)
        layout.addWidget(self.log_view)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.convert_button = QPushButton("Convert")
        self.convert_button.clicked.connect(self._start_conversion)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self.convert_button)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self.progress_update.connect(self._on_progress)
        self.conversion_finished.connect(self._on_finished)

    # ------------------------------------------------------------------
    def _custom_scenery_directory(self) -> Path | None:
        if self._xplane_directory is None:
            return None
        return self._xplane_directory / "Custom Scenery"

    def _global_airports_directory(self) -> Path | None:
        if self._xplane_directory is None:
            return None
        return self._xplane_directory / "Global Scenery" / "Global Airports"

    def _browse_package(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select the MSFS airport package folder"
        )
        if chosen:
            self.package_edit.setText(chosen)

    # ------------------------------------------------------------------
    def _start_conversion(self) -> None:
        package_directory = Path(self.package_edit.text().strip())
        custom_scenery = self._custom_scenery_directory()
        global_airports = self._global_airports_directory()
        if not package_directory.is_dir():
            QMessageBox.warning(
                self, "Convert MSFS airport",
                "Select an MSFS package folder first."
            )
            return
        if custom_scenery is None or not custom_scenery.is_dir():
            QMessageBox.warning(
                self, "Convert MSFS airport",
                "The X-Plane Custom Scenery folder is not available; set "
                "the X-Plane folder in Settings first."
            )
            return
        dsftool = default_dsftool_path()
        if not dsftool.is_file():
            QMessageBox.warning(
                self, "Convert MSFS airport",
                f"DSFTool not found at {dsftool}."
            )
            return
        pack_name = self.pack_name_edit.text().strip() or None

        self.convert_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.log_view.clear()
        UI.red_flag = False

        def work() -> None:
            import O4_MSFS_Airport_Convert as CONVERT

            try:
                report = CONVERT.convert_msfs_airport(
                    package_directory,
                    custom_scenery,
                    global_airports,
                    dsftool,
                    package_name=pack_name,
                    progress_callback=(
                        lambda percent, message:
                        self.progress_update.emit(percent, message)
                    ),
                )
            except InterruptedError:
                self.conversion_finished.emit(False, "Cancelled.", None)
            except Exception as error:  # surfaced verbatim in the dialog
                self.conversion_finished.emit(False, str(error), None)
            else:
                self.conversion_finished.emit(True, "", report)

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def _cancel(self) -> None:
        UI.red_flag = True
        self.cancel_button.setEnabled(False)

    # ------------------------------------------------------------------
    def _on_progress(self, percent: int, message: str) -> None:
        self.progress.setValue(percent)
        self.log_view.appendPlainText(message)

    def _on_finished(self, ok: bool, message: str, report) -> None:
        self.convert_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        if not ok:
            self.log_view.appendPlainText(f"Failed: {message}")
            QMessageBox.critical(self, "Convert MSFS airport", message)
            return
        summary_lines = [
            f"Created: {report.package_path}",
            f"Airport: {report.airport_icao or 'not identified'}"
            f" (apt.dat {'copied' if report.apt_dat_copied else 'NOT copied'})",
            f"Models converted: {report.models_converted}"
            f" -> {report.objects_written} OBJ8 objects",
            f"Placements written: {report.placements_written}"
            f" ({report.placements_skipped} skipped)",
            f"Exclusion zones: {report.exclusion_rectangles}",
        ]
        if report.warnings:
            summary_lines.append("")
            summary_lines.append("Warnings:")
            summary_lines.extend(f"  - {w}" for w in report.warnings[:12])
            if len(report.warnings) > 12:
                summary_lines.append(
                    f"  … and {len(report.warnings) - 12} more"
                )
        summary = "\n".join(summary_lines)
        self.log_view.appendPlainText(summary)
        QMessageBox.information(self, "Convert MSFS airport", summary)
