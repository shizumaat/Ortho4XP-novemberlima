"""Render the Ortho4XP app icon from its SVG source to all platform formats.

Reads  Utils/icons/Ortho4XP_icon.svg  and writes, next to it:
  Ortho4XP.icns  (macOS bundle icon, via iconutil — macOS only)
  Ortho4XP.ico   (Windows executable icon)
  Ortho4XP.png   (512px, set as the runtime window icon on Windows/Linux)

Run from the repository root, inside the project venv:
    python tools/render_app_icons.py

Note: Qt's SVG renderer ignores <clipPath>, so the rounded-rect shape is
applied here as a QPainter clip instead of inside the SVG. The geometry
must match the SVG's background rect (x=60 y=60 w=904 h=904 rx=202 in the
1024-unit viewBox).
"""
import os
import shutil
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPainterPath
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = os.path.join("Utils", "icons")
SVG_PATH = os.path.join(ICON_DIR, "Ortho4XP_icon.svg")

# Rounded-rect of the icon plate, in the SVG's 1024-unit coordinates.
PLATE_X, PLATE_Y, PLATE_SIZE, PLATE_RADIUS = 60.0, 60.0, 904.0, 202.0


def render_png(renderer: QSvgRenderer, size: int, path: str) -> None:
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    scale = size / 1024.0
    clip = QPainterPath()
    clip.addRoundedRect(
        QRectF(
            PLATE_X * scale,
            PLATE_Y * scale,
            PLATE_SIZE * scale,
            PLATE_SIZE * scale,
        ),
        PLATE_RADIUS * scale,
        PLATE_RADIUS * scale,
    )
    painter.setClipPath(clip)
    renderer.render(painter)
    painter.end()
    image.save(path)


def main() -> None:
    app = QGuiApplication([])  # noqa: F841 — Qt needs an application object
    renderer = QSvgRenderer(SVG_PATH)
    if not renderer.isValid():
        sys.exit(f"Could not parse {SVG_PATH}")

    render_png(renderer, 512, os.path.join(ICON_DIR, "Ortho4XP.png"))

    with tempfile.TemporaryDirectory() as tmp:
        # Windows .ico
        from PIL import Image

        base = os.path.join(tmp, "ico_256.png")
        render_png(renderer, 256, base)
        Image.open(base).save(
            os.path.join(ICON_DIR, "Ortho4XP.ico"),
            sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)],
        )

        # macOS .icns
        if sys.platform == "darwin" and shutil.which("iconutil"):
            iconset = os.path.join(tmp, "Ortho4XP.iconset")
            os.makedirs(iconset)
            for pts in (16, 32, 128, 256, 512):
                render_png(
                    renderer, pts, f"{iconset}/icon_{pts}x{pts}.png"
                )
                render_png(
                    renderer, pts * 2, f"{iconset}/icon_{pts}x{pts}@2x.png"
                )
            subprocess.run(
                ["iconutil", "-c", "icns", iconset,
                 "-o", os.path.join(ICON_DIR, "Ortho4XP.icns")],
                check=True,
            )
        else:
            print("Skipping .icns (not on macOS or iconutil missing)")

    print("Icons written to", ICON_DIR)


if __name__ == "__main__":
    main()
