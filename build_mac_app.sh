#!/bin/bash
# Build a double-clickable Ortho4XP.app (Qt UI) on macOS.
# Run from the Ortho4XP folder:
#     ./build_mac_app.sh
set -e

if [ ! -f "Ortho4XP_Qt.py" ]; then
    echo "Run this from the main Ortho4XP directory."
    exit 1
fi

# Use the project venv's interpreter directly — do not rely on `activate`
# (a venv that was moved or copied has a stale path baked into activate,
# which makes `pip` vanish from PATH even though python itself works).
if [ -n "$VIRTUAL_ENV" ]; then
    PYTHON="$VIRTUAL_ENV/bin/python"
elif [ -x "venv/bin/python" ]; then
    PYTHON="venv/bin/python"
else
    echo "No venv found. Run ./install_mac.sh first."
    exit 1
fi

"$PYTHON" -m pip show pyinstaller >/dev/null 2>&1 || "$PYTHON" -m pip install pyinstaller

"$PYTHON" -m PyInstaller Ortho4XP_Qt.spec --noconfirm

echo
echo "Done: dist/Ortho4XP.app"
echo "Double-click it, or drag it to /Applications."
echo "On first launch the app asks where to keep downloaded data, caches"
echo "and built tiles; picking an existing Ortho4XP folder reuses its data."
