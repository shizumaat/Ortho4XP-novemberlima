"""First-launch onboarding wizard for the Ortho4XP Qt UI.

Four steps (Welcome → X-Plane → Folders → Imagery), skippable at any point,
re-runnable from Help → "Run setup assistant…". Finishing (or skipping)
returns the collected preferences; the caller persists them and kicks off
airport indexing.
"""

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

STEPS = ["Welcome", "X-Plane", "Folders", "Imagery"]


def detect_xplane_installs():
    """Best-effort discovery of X-Plane installs on this machine."""
    candidates = []
    home = os.path.expanduser("~")
    # X-Plane's own installer breadcrumbs
    if sys.platform == "darwin":
        pref_dir = os.path.join(home, "Library", "Preferences")
    elif sys.platform.startswith("win"):
        pref_dir = os.environ.get("LOCALAPPDATA", "")
    else:
        pref_dir = os.path.join(home, ".x-plane")
    for fname in ("x-plane_install_12.txt", "x-plane_install_11.txt"):
        path = os.path.join(pref_dir, fname) if pref_dir else ""
        try:
            with open(path, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        candidates.append(line)
        except OSError:
            pass
    # Common locations
    for base in (
        "/Applications",
        home,
        os.path.join(home, "Desktop"),
        os.path.join(home, "Applications"),
    ):
        for name in ("X-Plane 12", "X-Plane 11"):
            candidates.append(os.path.join(base, name))
    seen, found = set(), []
    for c in candidates:
        c = os.path.normpath(c)
        if c in seen:
            continue
        seen.add(c)
        if os.path.isdir(os.path.join(c, "Custom Scenery")):
            found.append(c)
    return found


def looks_like_xplane(path):
    return bool(path) and os.path.isdir(os.path.join(path, "Custom Scenery"))


class OnboardingWizard(QDialog):
    """Mockup-faithful wizard: step rail on the left, content right."""

    def __init__(self, prefs, provider_codes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Welcome to Ortho4XP")
        self.setModal(True)
        self.resize(640, 400)
        self.prefs = dict(prefs)
        self.skipped = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Step rail
        rail = QFrame()
        rail.setFixedWidth(160)
        rail.setStyleSheet(
            "QFrame { background: palette(alternate-base); }"
        )
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(14, 18, 14, 14)
        self._step_labels = []
        for i, step in enumerate(STEPS):
            lbl = QLabel("%d.  %s" % (i + 1, step))
            rail_layout.addWidget(lbl)
            self._step_labels.append(lbl)
        rail_layout.addStretch(1)
        skip_btn = QPushButton("Skip setup")
        skip_btn.setFlat(True)
        skip_btn.clicked.connect(self._skip)
        rail_layout.addWidget(skip_btn)
        root.addWidget(rail)

        # Content
        right = QVBoxLayout()
        right.setContentsMargins(22, 18, 22, 16)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_welcome())
        self.stack.addWidget(self._page_xplane())
        self.stack.addWidget(self._page_folders())
        self.stack.addWidget(self._page_imagery(provider_codes))
        right.addWidget(self.stack, 1)

        nav = QHBoxLayout()
        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(self._back)
        nav.addWidget(self.back_btn)
        nav.addStretch(1)
        self.next_btn = QPushButton("Continue")
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self._next)
        nav.addWidget(self.next_btn)
        right.addLayout(nav)
        root.addLayout(right, 1)

        self._set_step(0)

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------
    def _page_welcome(self):
        page, lay = self._page("Let's get you building scenery")
        lay.addWidget(self._body(
            "Ortho4XP turns public imagery and elevation data into\n"
            "photo-real X-Plane scenery, one 1°×1° tile at a time.\n\n"
            "This takes under a minute and sets three things:\n"
            "  •  where X-Plane lives (so tiles install themselves),\n"
            "  •  where finished tiles are stored,\n"
            "  •  your default imagery source and zoom level.\n\n"
            "Everything can be changed later in Settings."
        ))
        lay.addStretch(1)
        return page

    def _page_xplane(self):
        page, lay = self._page("Where is X-Plane installed?")
        lay.addWidget(self._body(
            "Used to install finished tiles and to read data X-Plane\n"
            "already ships with. Optional — skip if you copy tiles by hand."
        ))
        row = QHBoxLayout()
        self.xplane_edit = QLineEdit(self.prefs.get("xplane_dir", ""))
        self.xplane_edit.textChanged.connect(self._xplane_changed)
        row.addWidget(self.xplane_edit, 1)
        self.detect_tag = QLabel("")
        row.addWidget(self.detect_tag)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_xplane)
        row.addWidget(browse)
        lay.addLayout(row)

        self.unlock_label = QLabel("")
        self.unlock_label.setTextFormat(Qt.RichText)
        lay.addWidget(self.unlock_label)
        lay.addStretch(1)

        if not self.xplane_edit.text():
            detected = detect_xplane_installs()
            if detected:
                self.xplane_edit.setText(detected[0])
                self.detect_tag.setText("detected")
        self._xplane_changed(self.xplane_edit.text())
        return page

    def _page_folders(self):
        page, lay = self._page("Where should finished tiles go?")
        import O4_File_Names as FNAMES

        lay.addWidget(self._body(
            "Each built tile is a folder of a few GB. Pick a drive with\n"
            "room; leave empty to use the classic location below."
        ))
        row = QHBoxLayout()
        self.output_edit = QLineEdit(self.prefs.get("output_dir", ""))
        self.output_edit.setPlaceholderText(FNAMES.Tile_dir)
        row.addWidget(self.output_edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_output)
        row.addWidget(browse)
        lay.addLayout(row)
        lay.addWidget(self._body(
            "Downloaded imagery and the live-map cache stay inside the\n"
            "Ortho4XP folder and can be cleared anytime."
        ))
        lay.addStretch(1)
        return page

    def _page_imagery(self, provider_codes):
        page, lay = self._page("Default imagery")
        lay.addWidget(self._body(
            "The imagery source and zoom level every new tile starts\n"
            "with. ZL16 is the sweet spot for most areas; each +1 ZL\n"
            "quadruples download size."
        ))
        row = QHBoxLayout()
        row.addWidget(QLabel("Imagery:"))
        self.imagery_combo = QComboBox()
        self.imagery_combo.addItems(provider_codes)
        self.imagery_combo.setCurrentText(self.prefs.get("imagery", "BI"))
        row.addWidget(self.imagery_combo)
        row.addSpacing(16)
        row.addWidget(QLabel("Zoom level:"))
        self.zl_combo = QComboBox()
        self.zl_combo.addItems([str(z) for z in range(12, 19)])
        self.zl_combo.setCurrentText(str(self.prefs.get("zl", 16)))
        row.addWidget(self.zl_combo)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    # ------------------------------------------------------------------
    def _page(self, title):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        heading = QLabel("<b>%s</b>" % title)
        heading.setTextFormat(Qt.RichText)
        lay.addWidget(heading)
        return page, lay

    def _body(self, text):
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        return lbl

    def _set_step(self, index):
        self.stack.setCurrentIndex(index)
        for i, lbl in enumerate(self._step_labels):
            lbl.setStyleSheet(
                "font-weight: bold;" if i == index else "color: gray;"
            )
        self.back_btn.setVisible(index > 0)
        self.next_btn.setText(
            "Finish" if index == len(STEPS) - 1 else "Continue"
        )

    def _back(self):
        self._set_step(max(0, self.stack.currentIndex() - 1))

    def _next(self):
        i = self.stack.currentIndex()
        if i == len(STEPS) - 1:
            self._finish()
        else:
            self._set_step(i + 1)

    def _skip(self):
        self.skipped = True
        self._collect()
        self.accept()

    def _finish(self):
        self._collect()
        self.accept()

    def _collect(self):
        self.prefs["xplane_dir"] = self.xplane_edit.text().strip()
        self.prefs["output_dir"] = self.output_edit.text().strip()
        self.prefs["imagery"] = self.imagery_combo.currentText()
        self.prefs["zl"] = int(self.zl_combo.currentText())

    # ------------------------------------------------------------------
    def _browse_xplane(self):
        path = QFileDialog.getExistingDirectory(self, "X-Plane folder")
        if path:
            self.xplane_edit.setText(path)
            self.detect_tag.setText("")

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(self, "Output folder")
        if path:
            self.output_edit.setText(path)

    def _xplane_changed(self, text):
        ok = looks_like_xplane(text.strip())
        if ok:
            self.unlock_label.setText(
                "<span style='color:green'>✓</span> Custom Scenery — tiles "
                "install with one click<br>"
                "<span style='color:green'>✓</span> Global Airports — map "
                "search by ICAO, name, city, country<br>"
                "<span style='color:green'>✓</span> Global Scenery — overlay "
                "source for roads and forests"
            )
        elif text.strip():
            self.unlock_label.setText(
                "<span style='color:#b00'>This folder has no Custom Scenery "
                "subfolder — is it really an X-Plane install?</span>"
            )
        else:
            self.unlock_label.setText(
                "<i>No folder set — installing and airport search stay "
                "off until you set one in Settings.</i>"
            )
