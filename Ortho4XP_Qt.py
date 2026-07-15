#!/usr/bin/env python3
"""Ortho4XP — map-first Qt UI (preview).

Usage:  python3 Ortho4XP_Qt.py

The legacy Tkinter UI and the command line remain available via Ortho4XP.py.
"""
import os
import sys

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

try:
    from PySide6.QtWidgets import QApplication
except ImportError:
    print(
        "The Qt UI needs PySide6. Install it into your Ortho4XP environment:\n"
        "    pip install PySide6\n"
        "or keep using the legacy UI:  python3 Ortho4XP.py"
    )
    sys.exit(1)

import O4_File_Names as FNAMES

sys.path.append(FNAMES.Provider_dir)

import O4_Imagery_Utils as IMG


def main():
    if not os.path.isdir(FNAMES.Utils_dir):
        print(
            "Missing", FNAMES.Utils_dir,
            "directory, check your install. Exiting.",
        )
        sys.exit(1)
    for directory in (
        FNAMES.Preview_dir,
        FNAMES.Provider_dir,
        FNAMES.Extent_dir,
        FNAMES.Filter_dir,
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

    app = QApplication(sys.argv)
    app.setApplicationName("Ortho4XP")
    app.setOrganizationName("Ortho4XP")

    import O4_Qt_GUI

    window = O4_Qt_GUI.MainWindow()
    window.show()
    code = app.exec()
    print("Bon vol!")
    sys.exit(code)


if __name__ == "__main__":
    main()
