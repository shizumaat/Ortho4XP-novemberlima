# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Qt (map-first) UI.
# On macOS this produces a double-clickable dist/Ortho4XP.app with a real
# application menu bar (no more "Python"). Build with:
#     ./build_mac_app.sh          (or: pyinstaller Ortho4XP_Qt.spec)
import os
import subprocess
import sys

import pyproj
from PyInstaller.utils.hooks import collect_submodules


# Same system-proj.db resolution as the legacy Ortho4XP.spec.
def get_system_proj_db():
    try:
        result = (
            subprocess.check_output(
                ["projinfo", "--searchpaths"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
        for path in result.splitlines():
            db = os.path.join(path.strip(), "proj.db")
            if os.path.isfile(db):
                return os.path.dirname(db)
    except Exception:
        pass
    if os.name == "nt":
        osgeo = os.environ.get("OSGEO4W_ROOT", r"C:\OSGeo4W")
        conda = os.environ.get("CONDA_PREFIX", "")
        candidates = [
            os.path.join(osgeo, "share", "proj"),
            os.path.join(conda, "Library", "share", "proj"),
            r"C:\Program Files\PROJ\share\proj",
        ]
    else:
        candidates = [
            "/opt/homebrew/share/proj",
            "/usr/local/share/proj",
            "/usr/share/proj",
        ]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, "proj.db")):
            return candidate
    print("WARNING: falling back to pyproj's bundled proj.db")
    return pyproj.datadir.get_data_dir()


system_proj_dir = get_system_proj_db()
proj_dest = os.path.join("pyproj", "proj_dir", "share", "proj")

a = Analysis(
    ['Ortho4XP_Qt.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        ('./Utils',               './Ortho4XP_Data/Utils'),
        ('./Extents',             './Ortho4XP_Data/Extents'),
        ('./Filters',             './Ortho4XP_Data/Filters'),
        ('./Licence',             './Ortho4XP_Data/Licence'),
        ('./Patches',             './Ortho4XP_Data/Patches'),
        ('./Previews',            './Ortho4XP_Data/Previews'),
        ('./Providers',           './Ortho4XP_Data/Providers'),
        ('community_server.txt',  './Ortho4XP_Data/'),
        ('overpass_servers.txt',  './Ortho4XP_Data/'),
        (os.path.join(system_proj_dir, "proj.db"), proj_dest),
    ],
    hiddenimports=collect_submodules('PIL'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Qt modules the app does not use — keeps the bundle smaller.
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtCharts',
        'PySide6.QtMultimedia', 'PySide6.QtQuick', 'PySide6.QtQml',
        'PySide6.QtPdf', 'PySide6.QtDesigner', 'PySide6.QtTest',
        'tkinter',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Ortho4XP_Qt',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # windowed app: proper menu bar, no terminal
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Ortho4XP_Qt',
)

if sys.platform == 'darwin':
    app = BUNDLE(
        coll,
        name='Ortho4XP.app',
        icon=None,
        bundle_identifier='org.ortho4xp.qt',
        info_plist={
            'CFBundleName': 'Ortho4XP',
            'CFBundleDisplayName': 'Ortho4XP',
            'CFBundleShortVersionString': '1.40',
            'NSHighResolutionCapable': True,
            'NSRequiresAquaSystemAppearance': False,  # follow dark mode
        },
    )
