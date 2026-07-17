#!/usr/bin/env python3
"""Ortho4XP — map-first Qt UI (preview).

Usage:  python3 Ortho4XP_Qt.py

The legacy Tkinter UI and the command line remain available via Ortho4XP.py.
"""
import os
import sys

# Deterministic builds, defense in depth: the pavement builder pins every
# hash-order-sensitive iteration at the source, and the frozen executable
# additionally starts with hash_seed=0 (bootloader OPTION in
# Ortho4XP_Qt.spec).  Exporting the variable here makes every child process
# (engine tile-build workers, elevation-inset fetchers — they inherit
# os.environ) run under the same pinned seed, covering any future
# regression.  setdefault so an explicitly chosen seed is respected.
os.environ.setdefault("PYTHONHASHSEED", "0")

Ortho4XP_dir = ".." if getattr(sys, "frozen", False) else "."

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    _proj_data_path = os.path.join(
        sys._MEIPASS, "pyproj", "proj_dir", "share", "proj"
    )
    _lib_path = os.path.join(sys._MEIPASS, "_internal")
    os.environ["PROJ_DATA"] = _proj_data_path
    os.environ["DYLD_LIBRARY_PATH"] = (
        _lib_path + ":" + os.environ.get("DYLD_LIBRARY_PATH", "")
    )

sys.path.append(os.path.join(Ortho4XP_dir, "src"))

# Build-worker mode BEFORE any Qt import (docs/specs/parallel-tile-builds.md
# §3.2): the frozen application serves as its own worker child, mirroring
# the same early branch in Ortho4XP.py.  Everything after this line is
# unreachable in worker mode.
if "--engine-jsonl" in sys.argv:
    from o4_engine import jsonl

    # owns_process: the transport bounds this process's life — front-end
    # death (stdin EOF, SIGTERM, ppid change) stops any in-flight build
    # and exits, so no orphan engine can keep building headless.
    jsonl.serve(sys.stdin, sys.stdout, owns_process=True)
    sys.exit(0)

try:
    from PySide6.QtWidgets import (
        QApplication,
        QDialog,
        QDialogButtonBox,
        QFileDialog,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QPushButton,
        QVBoxLayout,
    )
except ImportError:
    print(
        "The Qt UI needs PySide6. Install it into your Ortho4XP environment:\n"
        "    pip install PySide6\n"
        "or keep using the legacy UI:  python3 Ortho4XP.py"
    )
    sys.exit(1)


class DataFolderDialog(QDialog):
    """First-launch chooser for the packaged app: where should Ortho4XP keep
    its downloads, caches, settings and built tiles?"""

    def __init__(self, initial_path):
        super().__init__()
        self.setWindowTitle("Welcome to Ortho4XP")
        self.setMinimumWidth(560)

        intro = QLabel(
            "Ortho4XP keeps everything it downloads and builds in one "
            "folder: orthophoto imagery, elevation data, caches, settings "
            "and the finished scenery tiles. This folder can grow to many "
            "gigabytes, so choose a location with plenty of space.\n\n"
            "Already have an Ortho4XP folder? Select it — your existing "
            "downloads, tiles and settings are used as-is; nothing is "
            "overwritten."
        )
        intro.setWordWrap(True)

        self.path_edit = QLineEdit(initial_path)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)

        buttons = QDialogButtonBox()
        use_button = buttons.addButton(
            "Use This Folder", QDialogButtonBox.AcceptRole
        )
        buttons.addButton("Quit", QDialogButtonBox.RejectRole)
        use_button.setDefault(True)
        buttons.accepted.connect(self._accept_if_usable)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(path_row)
        layout.addWidget(buttons)

    def _browse(self):
        start = self.path_edit.text().strip() or os.path.expanduser("~")
        while start and not os.path.isdir(start):
            start = os.path.dirname(start)
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the Ortho4XP data folder", start
        )
        if chosen:
            self.path_edit.setText(chosen)

    def _accept_if_usable(self):
        path = os.path.abspath(os.path.expanduser(self.path_edit.text().strip()))
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".ortho4xp_write_test")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
        except OSError as error:
            QMessageBox.warning(
                self,
                "Folder not usable",
                "Ortho4XP cannot write to this folder:\n\n"
                f"{path}\n\n{error}\n\nPlease choose another location.",
            )
            return
        self.chosen_path = path
        self.accept()


def bootstrap_data_root():
    """Resolve the writable data root before any module captures paths.

    Only the packaged app ever shows the chooser; source checkouts keep
    using the checkout directory. Runs again if the remembered folder is
    missing (e.g. an unplugged external drive) so the user can reconnect
    it or pick a new one.
    """
    import O4_File_Names as FNAMES

    if not FNAMES.is_frozen_app():
        return True
    if os.environ.get("ORTHO4XP_DATA_ROOT"):
        FNAMES.set_data_root(FNAMES.resolve_data_root())
        return True
    remembered = FNAMES.read_data_root_pointer()
    if remembered and os.path.isdir(remembered):
        FNAMES.set_data_root(remembered)
        return True
    dialog = DataFolderDialog(remembered or FNAMES.default_data_root())
    if dialog.exec() != QDialog.Accepted:
        return False
    FNAMES.write_data_root_pointer(dialog.chosen_path)
    FNAMES.set_data_root(dialog.chosen_path)
    return True


def main():
    app = QApplication(sys.argv)
    # QApplication calls C setlocale(LC_ALL, ""), switching LC_NUMERIC to
    # the user's locale.  In-process tile builds then format numbers for
    # external tools with grouping/decimal characters their C parsers
    # misread (atoi("7,345") == 7).  Pin numeric formatting back to C.
    import locale

    locale.setlocale(locale.LC_NUMERIC, "C")
    app.setApplicationName("Ortho4XP")
    app.setOrganizationName("Ortho4XP")

    if not bootstrap_data_root():
        sys.exit(0)

    # Imported only after the data root is settled: these capture paths
    # (and O4_Config_Utils creates a default Ortho4XP.cfg) at import time.
    import O4_File_Names as FNAMES

    # macOS takes the icon from the .app bundle; this covers the window /
    # taskbar icon on Windows and Linux (and source runs everywhere).
    icon_path = os.path.join(FNAMES.Utils_dir, "icons", "Ortho4XP.png")
    if os.path.isfile(icon_path):
        from PySide6.QtGui import QIcon

        app.setWindowIcon(QIcon(icon_path))

    sys.path.append(FNAMES.Provider_dir)

    import O4_Imagery_Utils as IMG

    if not os.path.isdir(FNAMES.Utils_dir):
        print(
            "Missing", FNAMES.Utils_dir,
            "directory, check your install. Exiting.",
        )
        sys.exit(1)
    FNAMES.seed_shipped_patches()
    for directory in (
        FNAMES.Preview_dir,
        FNAMES.OSM_dir,
        FNAMES.Mask_dir,
        FNAMES.Imagery_dir,
        FNAMES.Elevation_dir,
        FNAMES.Geotiff_dir,
        FNAMES.Patch_dir,
        FNAMES.Tile_dir,
        FNAMES.Tmp_dir,
    ):
        if not os.path.isdir(directory):
            try:
                os.makedirs(directory)
                print("Creating missing directory", directory)
            except OSError:
                print("Could not create required directory", directory)
                sys.exit(1)

    IMG.initialize_extents_dict()
    IMG.initialize_color_filters_dict()
    IMG.initialize_providers_dict()
    IMG.initialize_combined_providers_dict()
    # Let tile builds reuse imagery already fetched by the live map.
    IMG.shared_tile_cache_dir = os.path.join(FNAMES.Preview_dir, "livemap")

    import O4_Qt_GUI

    window = O4_Qt_GUI.MainWindow()
    window.show()
    code = app.exec()
    print("Bon vol!")
    sys.exit(code)


if __name__ == "__main__":
    main()
