#!/bin/bash
# Build a double-clickable Ortho4XP.app (Qt UI) on macOS.
# Run from the Ortho4XP folder, inside your working venv:
#     ./build_mac_app.sh
set -e

if [ ! -f "Ortho4XP_Qt.py" ]; then
    echo "Run this from the main Ortho4XP directory."
    exit 1
fi

if [ -d "venv" ] && [ -z "$VIRTUAL_ENV" ]; then
    source venv/bin/activate
fi

pip show pyinstaller >/dev/null 2>&1 || pip install pyinstaller

pyinstaller Ortho4XP_Qt.spec --noconfirm

echo
echo "Done: dist/Ortho4XP.app"
echo "Double-click it, or drag it to /Applications."
echo "Note: tiles, caches and configs live inside the app's folder tree"
echo "unless you set the output folder in Settings — do that on first run."
