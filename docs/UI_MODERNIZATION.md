# Ortho4XP UI Modernization — Research Report & Design Recommendations

**Scope:** What is possible and practical to modernize the Ortho4XP GUI so it feels as
close as possible to a native application on macOS, Windows, and Linux, plus concrete
design changes following human-factors and usability best practices, a prioritized
roadmap, and a proposed settings reorganization.

**Codebase reviewed:** `src/O4_GUI_Utils.py` (main window, zone editor, world map —
2,340 lines), `src/O4_Config_Utils.py` (config window — 1,660 lines),
`src/O4_Cfg_Vars.py` (settings registry — ~60 parameters), `src/O4_UI_Utils.py`
(GUI/core bridge), `Ortho4XP.py` (entry point / CLI).

---

## 1. Executive Summary

Ortho4XP's GUI is a single-theme Tkinter application (hard-coded "light green"
background, fixed-width font, `alt` ttk theme) with three windows: the main
build window, a "Custom Zoom Levels" zone editor, and a "Tiles Collection and
Management" world map. It works, but it does not look or behave like a native
application on any platform, and several interactions rely on invisible
modifier-key combinations that new users cannot discover.

**The good news:** the architecture makes modernization unusually cheap. Only two
files import Tkinter. The compute pipeline (vector, mesh, masks, imagery, DSF)
communicates with the GUI through exactly three narrow channels:

1. `UI.progress_bar(n, pct)` — progress updates (`O4_UI_Utils.py:26`)
2. `UI.red_flag` — a polled cancellation flag
3. `print()`/stdout — redirected into the console `Text` widget via a queue
   (`O4_GUI_Utils.py:372-373`)

Work already runs on background threads with queue-based, poll-driven UI updates.
This means **any** GUI toolkit can be swapped in behind a small adapter without
touching the build pipeline, and the CLI mode (`Ortho4XP.py lat lon ...`) is
unaffected.

**Recommendation in one paragraph:** Do this in two tracks. **Track A (near-term,
low risk):** keep Tkinter but fix the worst native-feel and usability offenses —
platform-native ttk themes instead of hard-coded colors, a real menu bar,
HiDPI awareness on Windows, tooltips, labeled buttons, confirmation on
destructive actions, and visible alternatives to hidden modifier-click features.
**Track B (the actual modernization):** port the three windows to **PySide6
(Qt 6, LGPL)**, which is the only mainstream Python option that delivers
system-following dark/light mode, native menus and dialogs, HiDPI, screen-reader
accessibility, and a first-class canvas/map story on all three platforms, while
remaining PyInstaller-friendly. A settings-model refactor (Section 7) should
precede Track B and benefits Track A immediately.

---

## 2. Current State Assessment

### 2.1 Architecture (relevant to UI work)

```
Ortho4XP.py ──► O4_GUI_Utils.Ortho4XP_GUI (tk.Tk)
                 ├── Ortho4XP_Custom_ZL (tk.Toplevel)   zone editor, tk.Canvas
                 ├── Ortho4XP_Earth_Preview (tk.Toplevel) world map, tk.Canvas
                 └── O4_Config_Utils.Ortho4XP_Config (tk.Toplevel) 3-tab settings

Core modules (MESH, MASK, TILE, VMAP, IMG, ...) ──► O4_UI_Utils (UI.*)
        progress_bar() / red_flag / vprint()→stdout   ◄── the ONLY GUI contact surface
```

- GUI state is persisted ad hoc to `.last_gui_params.txt` (lat/lon/provider/ZL and
  base folder only). Window size/position is not persisted.
- Settings are managed through module-level globals created with
  `exec()`/`eval()` from the `cfg_vars` dict (`O4_Config_Utils.py:96-102`,
  `52-74`), mirrored into `global_*`-prefixed twins for the "global" scope, and
  written to flat `key=value` files.
- Long-running work: `threading.Thread` per step; GUI polls queues every 100 ms
  (`console_update`, `pgrb_update`).

### 2.2 Usability and native-feel issues found (with locations)

| # | Issue | Where | Severity |
|---|-------|-------|----------|
| 1 | Hard-coded `"light green"`/`"dark green"` colors and `TkFixedFont` everywhere → looks foreign on every OS, breaks with dark system themes, low-contrast text on colored backgrounds | `O4_GUI_Utils.py:62-93,147-166` and ~40 more sites | High |
| 2 | No menu bar at all — no File/Edit/View/Help, no About, no standard shortcuts (Cmd+Q handling is explicitly noted as not covered, `O4_GUI_Utils.py:98-99`) | all windows | High |
| 3 | Icon-only buttons (Config, Loupe, Earth, Stop, Exit as GIFs) with **no tooltips and no text labels** | `O4_GUI_Utils.py:259-294` | High |
| 4 | Hidden functionality behind undocumented modifier clicks: Shift-click "Triangulate 3D Mesh" = *sort mesh*, Ctrl-click = *download community mesh* (`O4_GUI_Utils.py:310-312`); Shift-click "Draw Water Masks" = masks for imagery (`319-320`); Shift-click the DEM folder button = *append* a DEM (`O4_Config_Utils.py:539`) | main + config windows | High |
| 5 | Map interactions only discoverable via a static label: "Ctrl+B1: add texture, Shift+B1: add zone point, B2 drag: pan, `o`: link overlays…" (`O4_GUI_Utils.py:992-998, 1527-1535`); different bindings on macOS vs others (`1090-1096`) | both canvas windows | High |
| 6 | **Batch Delete erases data (`shutil.rmtree`) with no confirmation dialog** | `O4_GUI_Utils.py:1993-2015` | **Critical** |
| 7 | Settings displayed as raw variable names (`apt_smoothing_pix`, `curvature_tol`); help only via clicking the name, which opens a separate "Hint!" popup window | `O4_Config_Utils.py:466-475, 1640-1660` | High |
| 8 | ~47 tile settings shown at once in a 4-column grid with 7-char entry fields; no search, no basic/advanced separation, no indication of which values differ from defaults | `O4_Config_Utils.py:394-605` | High |
| 9 | Tile/Global/App scope model is implicit; the same parameter appears twice (Tile tab and Global tab) with no visual link, and `global_` prefix bookkeeping is done in code by string munging | `O4_Cfg_Vars.py:354-361` | Medium |
| 10 | No input validation until build time; lat/lon errors surface in the console as text (`O4_GUI_Utils.py:536-559`); typed settings silently reset to defaults on parse failure with a modal error listing variable names | main + config | Medium |
| 11 | Three unlabeled progress bars; cancellation is a red icon that silently sets a flag; no busy/percent state in the title or taskbar | `O4_GUI_Utils.py:333-357` | Medium |
| 12 | `takefocus=False` on most buttons → keyboard navigation is impossible; and Tkinter has **no screen-reader/accessibility bridge at all** on any platform | throughout | Medium (High for a11y) |
| 13 | No HiDPI handling on Windows (blurry text at 125–200 % scaling unless DPI awareness is declared); no dark-mode support or detection | app-wide | Medium |
| 14 | World map is pre-rendered ZL-6 JPEG quadrants with manual pan and **no zoom**; tile status legend (colors, stipple = symlinked) is unexplained in the UI | `O4_GUI_Utils.py:1398-,2267-2336` | Medium |
| 15 | Console is a raw `Text` widget capturing stdout; no log levels/filtering/copy-all/save; verbosity is a setting buried in App Config | `O4_GUI_Utils.py:360-417` | Low |
| 16 | Symlinks created by shelling out to `ln -s` / `MKLINK /J` via `os.system` with string concatenation | `O4_GUI_Utils.py:1616-1620` | Low (robustness) |

---

## 3. What "Native Experience" Means per Platform — and Where Tkinter Caps Out

| Capability | macOS expectation | Windows expectation | Linux expectation | Tkinter today |
|---|---|---|---|---|
| Widget look | Aqua controls, SF font, unified toolbar | Win 11 (Mica/Fluent-ish), Segoe UI | GTK/Qt theme of the desktop | `aqua` ttk theme is decent on macOS; `vista` theme dated but OK on Windows; on Linux ttk `clam/alt` looks like 1995. Hard-coded colors currently defeat even this. |
| Menu bar | Global menu bar, app menu (About/Preferences/Quit with ⌘Q, ⌘,) | In-window menu bar | In-window (or global on GNOME) | Fully supported by Tk (`tk.Menu`) — simply not used by Ortho4XP. |
| Dark mode | Follows system, auto | Follows system | Follows system | No native support; needs `darkdetect` + a manual theme (e.g. sv-ttk). macOS Tk ≥ 8.6.10 partially adapts `aqua` automatically. |
| HiDPI | Automatic | Requires DPI-awareness opt-in + `tk scaling` | Mixed; fractional scaling poor | macOS fine; Windows needs `SetProcessDpiAwareness`; Linux manual. |
| File dialogs | Native | Native | Native only on GTK-ish desktops | Already native via `tkinter.filedialog` (this part is fine). |
| Accessibility (VoiceOver/Narrator/Orca) | Required for "native" claim | Required | Required | **Not possible in Tkinter — no accessibility API bridge exists.** |
| System notifications, taskbar progress, recent-files, drag-and-drop | Expected in modern apps | Expected | Expected | Not available (or third-party hacks only). |

**Conclusion:** With effort, Tkinter can be made *presentable and consistent*
(Track A), and genuinely close to native on macOS specifically. It cannot ever be
made *native* on Linux, accessible anywhere, or system-dark-mode-following without
maintenance-heavy workarounds. Those require a toolkit change (Track B).

---

## 4. Framework Options Analysis

### Option 1 — Tkinter, improved in place (Track A)

Keep the toolkit; remove hard-coded colors; use per-platform ttk themes
(`aqua` on macOS, `vista` on Windows, and a modern third-party theme such as
**sv-ttk** (Sun Valley) or **ttkbootstrap** on Linux/as dark-mode fallback);
add `darkdetect` for light/dark selection; declare DPI awareness on Windows;
add a menu bar, tooltips, and validation.

- **Pros:** zero new heavy dependencies (sv-ttk and darkdetect are tiny, pure
  Python); fully incremental; no packaging changes; preserves all current code.
- **Cons:** ceiling described in §3 — no accessibility, Linux never native,
  dark mode is "our theme" not "the system theme"; canvas remains basic.
- **Effort:** days-to-weeks. **Risk:** minimal.

### Option 2 — CustomTkinter

Drop-in modern-flat look over Tkinter with built-in dark mode.

- **Pros:** quick visual win, small migration (widgets are near-drop-in).
- **Cons:** it is deliberately *non-native* (its own flat design language on all
  platforms); still Tkinter underneath, so the accessibility/HiDPI/Linux issues
  remain; single-maintainer project risk.
- **Verdict:** only worth it if "modern-looking" matters more than "native."
  Given the stated goal (*as close as possible to native*), **not recommended**.

### Option 3 — PySide6 / Qt 6 (recommended for Track B)

Qt Widgets with the platform style: true Aqua-styled controls and the global menu
bar on macOS, Win11-styled controls on Windows (Qt ≥ 6.7 ships a native Windows 11
style), Fusion (theme-color-aware) on Linux with optional GTK color adoption.

- **Pros:**
  - System **dark/light mode followed automatically** (Qt ≥ 6.5 exposes
    `QStyleHints.colorScheme`, and platform styles adapt on all three OSes).
  - **Accessibility**: full bridges to VoiceOver, UIA/Narrator, and AT-SPI/Orca.
  - Native menus, dialogs, standard shortcuts (`QKeySequence.StandardKey`),
    proper macOS app-menu integration (About / Preferences / Quit).
  - `QGraphicsView` is a direct, better `tk.Canvas` replacement for both map
    windows (zoom, item hit-testing, rubber-band selection built in). Optional
    QtLocation/QML slippy map later.
  - `QSettings` for window-state persistence; `QThread`/signals map cleanly onto
    the existing thread+queue design (queues can stay; a QTimer poll reproduces
    today's model exactly, so the port can be mechanical).
  - LGPL licensing is fine for this GPL-adjacent open-source project;
    PyInstaller support is mature; wheels exist for all three platforms
    (including macOS arm64).
- **Cons:** ~100–200 MB added to the bundled app; a real (if mechanical) port of
  ~3,000 GUI lines; contributors must learn some Qt.
- **Effort:** ~3–6 weeks part-time for a faithful port, done window-by-window.
  **Risk:** low-moderate; mitigated by the narrow `UI.*` contact surface.

### Option 4 — wxPython

Wraps genuinely native widgets (Cocoa, Win32, GTK3) — arguably the *most* native
look attainable from Python.

- **Pros:** real native widgets everywhere; native accessibility; permissive license.
- **Cons:** substantially smaller community than Qt; historically slow wheel
  availability for new Python versions (this repo tracks recent Pythons — 3.13
  is referenced in the README); dated API ergonomics; weaker canvas
  (`wx.lib.floatcanvas`) for the map windows; dark-mode support on Windows lagging.
- **Verdict:** a legitimate second choice if bundle size is the top concern, but
  PySide6 is the safer bet for this project's pace and needs.

### Option 5 — Web UI (pywebview / Tauri / Electron / NiceGUI) with Python backend

Wrap an HTML/JS front end over the existing pipeline (pywebview keeps it
in-process; Tauri/Electron split it into a served app).

- **Pros:** the **map experience** becomes best-in-class with almost no work —
  Leaflet/MapLibre gives a real zoomable slippy world map, polygon drawing for ZL
  zones, and tile overlays, which is exactly Ortho4XP's core interaction. Modern
  styling is trivial; one UI for all platforms; could even run headless/remote
  (build server at home, control from a laptop).
- **Cons:** explicitly *not* native (web look, unless significant effort is
  spent mimicking each OS); Electron adds ~200 MB and a second runtime; Tauri
  needs a Rust toolchain in CI; pywebview depends on each OS's WebView (WebView2
  on Windows, WebKit on macOS/Linux) with its own quirks; two-language codebase.
- **Verdict:** not the right answer to a "native experience" brief, **but** the
  Leaflet-style map idea should be stolen: in Qt, embed `QWebEngineView` (or
  QtLocation) for the world-map window only, or reproduce zoom/pan in
  `QGraphicsView`. Keep this option in mind if the project ever wants a
  remote/headless build UI.

### Option 6 — Flet, Kivy, Dear PyGui (brief)

All are actively developed but render their own non-native UI (Flutter, OpenGL).
They fail the "native" requirement on all three platforms. Not recommended here.

### Comparison matrix

| Criterion (weight) | Tk improved | CustomTkinter | **PySide6** | wxPython | Web/pywebview |
|---|---|---|---|---|---|
| Native look macOS | ● ● ◐ | ● ◐ ○ | ● ● ● | ● ● ● | ● ○ ○ |
| Native look Windows | ● ● ○ | ● ◐ ○ | ● ● ◐ | ● ● ● | ● ○ ○ |
| Native look Linux | ● ○ ○ | ● ◐ ○ | ● ● ◐ | ● ● ◐ | ● ○ ○ |
| System dark mode | ◐ | ● (own theme) | ● ● ● | ● ● ◐ | ◐ |
| HiDPI | ◐ | ◐ | ● ● ● | ● ● ◐ | ● ● ● |
| Accessibility | ○ | ○ | ● ● ● | ● ● ◐ | ● ● ◐ |
| Map/canvas power | ◐ | ◐ | ● ● ◐ | ● ○ ○ | ● ● ● |
| Migration effort | ● ● ● (least) | ● ● ◐ | ● ◐ ○ | ● ◐ ○ | ● ○ ○ (most) |
| Packaging/bundle impact | ● ● ● | ● ● ● | ● ◐ ○ | ● ● ◐ | ● ○ ○ |
| Ecosystem/longevity | ● ● ● | ◐ | ● ● ● | ● ◐ ○ | ● ● ● |

**Bottom line: PySide6 is the destination; an improved Tkinter is the bridge.**

---

## 5. Recommended Plan — Two Tracks

### Track A — Modernize in place (ship within one or two releases)

1. **Safety first:** confirmation dialog on Batch Delete (list what will be
   deleted, for which tiles, with counts); make "Stop" give feedback
   ("Stopping after current step…", button shows active state).
2. **De-hardcode the theme.** Remove every `bg="light green"` etc.; select ttk
   theme per platform (`aqua`/`vista`/sv-ttk); adopt `darkdetect` so Linux and
   Windows follow the system light/dark preference at startup.
3. **Menu bar + shortcuts** (see §6.2). Wire ⌘Q/Alt-F4 through the existing
   `exit_prg` unsaved-changes flow.
4. **Windows HiDPI:** call `SetProcessDpiAwareness` (via ctypes) before creating
   the root window and set `tk scaling` accordingly.
5. **Expose the hidden features** as visible controls (see §6.3) and add
   tooltips to every control (a 20-line Tooltip class or `idlelib.tooltip`).
6. **Validation** on lat/lon and numeric settings at input time
   (`validatecommand`), with inline error styling instead of console messages.
7. **Persist window geometry** per window in the existing params file.

Everything in Track A is toolkit-agnostic groundwork or directly portable design
work — nothing is thrown away by Track B.

### Track B — Port to PySide6, window by window

Order of port (lowest risk → highest value):

1. **Adapter layer.** Replace `sys.stdout` redirection with a `UI.post_line()`
   callback (default: print), keep `progress_bar`/`red_flag` signatures. The Tk
   and Qt front ends both consume the same three-channel interface. *(Small,
   do during Track A.)*
2. **Settings window** — after the settings-model refactor (§7), this is a
   data-driven form; build it once with `QFormLayout`/`QTreeView`+search.
3. **Main window** — direct translation; console becomes `QPlainTextEdit`
   (read-only, 10k-line cap), progress bars become labeled `QProgressBar`s +
   taskbar progress (`QWinTaskbarButton` equivalent / unity launcher API).
4. **World map & zone editor** — `QGraphicsScene` port of the canvas logic
   (the coordinate math in `GEO.*` is UI-independent and reusable as-is);
   add wheel-zoom and mode toolbar. Optionally upgrade later to a real slippy
   map (QtWebEngine + MapLibre) once parity is reached.
5. Keep the Tk GUI available behind a `--legacy-gui` flag for one release, then
   remove.

Packaging: PyInstaller specs already exist (`Ortho4XP.spec`); PySide6 has
first-class PyInstaller hooks. Expect the bundle to grow by roughly 100–200 MB;
exclude unused Qt modules (`QtWebEngine` unless used, `Qt3D`, etc.) to control it.

---

## 6. Design Recommendations (Human Factors & Usability)

### 6.1 Main window layout

Restructure the main window into the standard "form → actions → feedback" flow,
top to bottom, and rename the pipeline actions to task language:

```
┌────────────────────────────────────────────────────────────────┐
│ File  Edit  View  Tools  Help                    (menu bar)    │
├────────────────────────────────────────────────────────────────┤
│ Tile:  Lat [ 48▲▼]  Lon [ -6▲▼]   [🌍 Pick on map…]           │
│ Imagery: [Bing (BI)      ▾]   Zoom level: [16 ▾]               │
│ Output folder: [/path/to/Tiles………………]  [Browse…]               │
├────────────────────────────────────────────────────────────────┤
│ Build steps:                                                   │
│ [1. Vector data] [2. Mesh ▾] [3. Water masks ▾] [4. Imagery]   │
│                       [▶ Build All]     [■ Stop]               │
├────────────────────────────────────────────────────────────────┤
│ Vector/DSF   ▓▓▓▓▓▓░░░░░░ 52 %                                 │
│ Imagery      ▓▓░░░░░░░░░░ 17 %                                 │
│ Conversion   ░░░░░░░░░░░░  0 %                                 │
├────────────────────────────────────────────────────────────────┤
│ Log  [level: Normal ▾] [Filter…    ] [Copy] [Save…]            │
│ ┌────────────────────────────────────────────────────────────┐ │
│ │ …                                                          │ │
└────────────────────────────────────────────────────────────────┘
```

- Lat/lon become **spinboxes with range validation** (-85…84 / -180…179,
  matching `get_lat_lon`), with a "Pick on map…" button as the primary path —
  most users think in places, not integers.
- Number the steps (1–4). The current labels don't communicate order; the whole
  product is a 4-stage pipeline and the UI should teach that.
- **Label the progress bars** (they map to fixed roles in `O4_Parallel_Utils`)
  and add percentages. Show overall busy state in the title bar/taskbar.
- Console gains a level selector (maps to existing `verbosity`), filter box,
  and copy/save; keep the queue+timer mechanism.

### 6.2 Menu bar (restores platform conventions almost for free)

- **File:** Save Tile Config, Load Tile Config, Open Tiles Folder, Quit
- **Edit:** standard clipboard items (macOS gets these for free), Preferences…
  (⌘, / Ctrl+,) → settings window
- **View:** Show Map, Show Zone Editor, Log Verbosity, Theme (System/Light/Dark)
- **Tools:** Build steps (with accelerators), Batch Build…, Erase Cached Data…,
  Link Overlays to X-Plane
- **Help:** About Ortho4XP (version — currently only in the title bar),
  Open Documentation, Open Log File, Keyboard & Mouse Reference

On macOS put About/Preferences/Quit in the app menu (Tk supports this via
`tk::mac::` hooks; Qt does it automatically).

### 6.3 Kill invisible modifier-click features

Every hidden binding gets a visible equivalent (keep the shortcuts as
accelerators for experts, but document them in tooltips/menu):

| Today (hidden) | Proposed (visible) |
|---|---|
| Shift-click "Triangulate 3D Mesh" → sort mesh; Ctrl-click → community mesh | Split-button / dropdown on the Mesh button: *Triangulate*, *Sort mesh*, *Download community mesh* |
| Shift-click "Draw Water Masks" → masks for imagery | Dropdown: *Draw masks*, *Draw masks for imagery* |
| Shift-click DEM folder icon → append second DEM | Editable DEM list with [+]/[−] buttons |
| Map: Ctrl+B1 / Shift+B1 / B2-drag / `o`, `p`, `d`, `n`, Backspace | A small **mode toolbar** on the map: `[Pan] [Select tile] [Multi-select] [Draw zone] [Delete zone]` + right-click context menu on tiles (*Set active*, *Link to Custom Scenery*, *Delete cached data…*). Cursor changes per mode. |

Mode toolbars beat modifier chords on every usability axis: discoverable,
touchpad-friendly (B2/B3 don't exist on Mac touchpads without remapping — the
code already needs a platform fork for this today), and self-documenting.

### 6.4 Map windows

- Merge the mental model: one **Map** window with two layers/tabs — *Tiles*
  (status + selection) and *Zones* (per-tile custom ZL polygons) — instead of two
  separate Toplevels with different interaction rules.
- Add **zoom** (wheel/pinch) — the current fixed ZL-6 world image makes tile
  picking at coastlines needlessly imprecise. In Qt, `QGraphicsView` scaling
  over the existing image pyramid is enough; a slippy-map (MapLibre/Leaflet via
  WebEngine, or `tkintermapview` in Track A) is the deluxe version.
- Add a **legend** for tile colors (ZL→color mapping and "hatched = linked into
  Custom Scenery") — currently pure folklore.
- Show a live **status bar**: cursor lat/lon, active tile, selection count,
  estimated download size (the zone editor already computes Gb — surface it here).
- Batch actions operate on the visible selection with an explicit summary
  ("Build 12 tiles: steps 1–4, ~34 GB imagery") before starting.

### 6.5 Feedback, errors, and destructive actions

- Confirmation with consequence summary for: Batch Delete (**critical — currently
  none**), Reset to Global/Defaults, overwrite of tile config.
- Replace console-only errors ("Latitude out of range" printed at build time)
  with inline field validation + disabled action buttons and a reason tooltip.
- Long-running builds: disable step buttons while `UI.is_working`, show elapsed
  time (already computed by `timings_and_bottom_line`) in the status bar, and
  fire a system notification on batch completion (builds take hours; users leave).
- Make Stop a two-state control: *Stop requested…* until the worker thread
  actually checks `red_flag`.

### 6.6 Accessibility & input

- Full keyboard traversal: remove `takefocus=False`, define a logical tab order,
  Enter triggers the default action, Esc closes dialogs.
- Minimum 4.5:1 text contrast in both themes (the current blue-on-white entries
  and black-on-light-green labels fail in several places under macOS dark mode).
- Respect system font and size everywhere (drop global `TkFixedFont`; keep
  monospace only in the log view).
- Screen-reader support arrives with Track B (Qt) — label every control and set
  accessible names on the map scene items ("Tile +48-006, built, ZL16, linked").

---

## 7. Settings: Model Refactor and Reorganization

### 7.1 Replace the exec/eval globals with a typed settings model

This is the single highest-leverage refactor in the codebase, and it is a
prerequisite for any clean settings UI (it also removes ~15 `exec()` call sites
in `O4_Config_Utils.py` / `O4_GUI_Utils.py`):

- One `@dataclass`-based `Settings` object with three layers:
  **defaults → global config → per-tile config**, resolved by simple chain
  lookup. `Tile` already copies resolved values onto itself
  (`O4_Config_Utils.py:156-157`) — keep that contract so the pipeline is untouched.
- Keep the on-disk `key=value` format for backward compatibility (existing
  `Ortho4XP.cfg` and tile cfgs continue to load); the `config_compatibility`
  shim stays.
- Each setting keeps its registry entry (type, default, allowed values, hint) —
  `cfg_vars` already has 90 % of this — and gains: a **human-readable label**, a
  **unit**, a **category**, and an **advanced** flag.
- Benefit for both tracks: the settings UI becomes a loop over the registry, and
  "unsaved changes" detection becomes `dataclass != dataclass` instead of 130
  lines of string comparison (`check_unsaved_changes`).

### 7.2 Scope model made visible (the VS Code pattern)

Keep exactly two scopes the user can edit — **Global (defaults for all tiles)**
and **This tile (override)** — plus read-only app-level settings, but show them
in *one* place instead of duplicate tabs:

```
┌ Settings ────────────────────────────────────────────────────────┐
│ [Search settings…                    ]   Scope: (•) Tile +48-006 │
│                                          ( ) Global defaults     │
│ ▸ General & Paths          ┌─────────────────────────────────────┐
│ ▸ Network & Downloads      │ Mesh                                │
│ ▾ Mesh & Elevation         │                                     │
│ ▸ Imagery & Zoom Levels    │ Curvature tolerance      [2.0   ] ● │
│ ▸ Water & Masks            │   Higher values → fewer triangles.  │
│ ▸ Overlays                 │ Airport curvature tol.   [0.5   ]   │
│ ▸ Advanced                 │   Overrides curvature tolerance     │
│                            │   near airports.                    │
│                            │ Max triangles (millions) [3.0   ]   │
│ [Reset category…]          │ …                                   │
│ [Save]  [Cancel]           └─────────────────────────────────────┘
```

- **● modified indicator** next to any value that differs from the inherited
  layer, with per-setting context menu: *Reset to global / Reset to default /
  Copy to global*.
- When the Tile scope has no config file, show the global values greyed with a
  one-click **"Override for this tile"** — replacing today's disable-all-fields
  + status-string mechanism (`tile_cfg_status`).
- **Hints become inline descriptions** (short form under the field, full text in
  a help panel/tooltip) instead of a click-to-open "Hint!" popup. The hint texts
  in `O4_Cfg_Vars.py` are genuinely good — they're just hidden.
- **Search** filters across labels, keys, and hint text (60 settings is exactly
  the size where search starts to matter).

### 7.3 Proposed category structure (replacing Tile/Global/App tabs)

Organized by user task, basic-before-advanced within each; *(A)* = advanced flag,
hidden until "Show advanced" is toggled:

1. **General & Paths** — `custom_scenery_dir`, base/build folder (promoted from
   main window duplication), `custom_overlay_src`, `custom_overlay_src_alternate`,
   `cifp_data_path` (dev branch; defaults to X-Plane `Custom Data/CIFP/`, so
   onboarding's X-Plane detection covers it), `verbosity`, `cleaning_level` *(A)*
2. **Network & Downloads** — `max_download_slots`, `max_convert_slots`,
   `overpass_server_choice`, `http_timeout` *(A)*, `max_connect_retries` *(A)*,
   `max_baddata_retries` *(A)*, `check_tms_response` *(A)*, `skip_downloads` *(A)*,
   `skip_converts` *(A)*
3. **Imagery & Zoom Levels** — default provider + ZL (same values as main
   window), `cover_airports_with_highres`, `cover_zl`, `cover_extent`,
   `sea_texture_blur` *(A)*, custom ZL zones summary (count + "Edit on map…" link)
4. **Mesh & Elevation** — `custom_dem` (list editor), `fill_nodata`,
   `auto_patch` (dev branch: None/ICAO/All runway slope patches from CIFP —
   registered under vector vars in code, but users will look for it next to
   elevation), `curvature_tol`, `apt_curv_tol`/`apt_curv_ext`,
   `coast_curv_tol`/`coast_curv_ext`, `limit_tris`, `min_angle` *(A)*,
   `iterate` *(A)*, `mesh_zl` *(A)*
5. **Roads & Vector Data** — `road_level`, `road_banking_limit` *(A)*,
   `lane_width` *(A)*, `max_levelled_segs` *(A)*, `apt_smoothing_pix` *(A)*,
   `clean_bad_geometries` *(A)*, `water_simplification` *(A)*, `min_area`/`max_area` *(A)*
6. **Water & Masks** — `water_tech`, `ratio_water`, `ratio_bathy`, `mask_zl`,
   `masks_width`, `masking_mode`, `use_masks_for_inland` *(A)*,
   `imprint_masks_to_dds` *(A)*, `distance_masks_too` *(A)*,
   `masks_use_DEM_too` *(A)*, `masks_custom_extent` *(A)*
7. **Rendering & Overlays** — `overlay_lod`, `terrain_casts_shadows`,
   `use_decal_on_terrain`, `normal_map_strength` *(A)*, `ovl_exclude_pol` *(A)*,
   `ovl_exclude_net` *(A)*

Widget upgrades while reorganizing: booleans → checkboxes/switches (not
True/False comboboxes); enumerations keep comboboxes but show friendly labels
("Sand (smooth fade)" for `masking_mode=sand`); numeric fields get units and
min/max from the registry; `masks_width` gets a proper 1-or-3-values editor
instead of a free-text list.

---

## 8. Prioritized Roadmap

| Priority | Item | Sections | Effort | Impact |
|---|---|---|---|---|
| **P0** | Confirmation on Batch Delete; Stop-button feedback | 6.5 | Hours | Prevents data loss |
| **P0** | Tooltips everywhere; expose hidden modifier-click features as visible controls | 6.3 | Days | Discoverability |
| **P0** | Settings-model refactor (typed registry, no exec/eval), keeping file formats | 7.1 | ~1 wk | Unblocks everything; kills a class of bugs |
| **P1** | De-hardcoded theming: platform ttk themes + darkdetect + contrast pass; Windows DPI awareness | 5.A, 6.6 | Days | Biggest visual jump per effort |
| **P1** | Menu bar with standard shortcuts; About dialog; window-geometry persistence | 6.2 | Days | Native conventions |
| **P1** | Inline validation (lat/lon spinboxes, numeric ranges); labeled progress bars; log level/filter/save | 6.1, 6.5 | Days | Error prevention |
| **P2** | Settings UI reorganization: single window, scope selector, search, categories, inline hints, modified-indicators | 7.2–7.3 | 1–2 wks | Core UX of the product |
| **P2** | Map UX: mode toolbar, legend, status bar, selection summary before batch actions | 6.4 | ~1 wk | Core UX |
| **P3** | **PySide6 port** (adapter → settings → main → maps), `--legacy-gui` fallback for one release | 5.B | 3–6 wks | The actual native experience: dark mode, a11y, HiDPI, Linux |
| **P3** | Map zoom via QGraphicsView; optional slippy-map upgrade; system notifications; taskbar progress | 6.4, 6.5 | 1–2 wks | Polish |

P0–P2 are all achievable in the current Tkinter codebase and none of the work is
discarded by the port: the settings model, the interaction design, the category
structure, and the three-channel UI adapter carry over unchanged.

---

## 9. Risks & Migration Notes

- **Bundle size (Qt):** +100–200 MB in PyInstaller output. Mitigate by excluding
  unused Qt plugins/modules. If this is unacceptable, wxPython is the fallback
  (native + small), accepting its slower wheel cadence for new Python versions.
- **Threading:** keep the existing worker-thread + queue design; in Qt, poll the
  same queues with a `QTimer` (mechanical parity), then optionally migrate to
  signals later. Never touch widgets from worker threads (same rule as Tk today).
- **CLI must stay headless:** `O4_UI_Utils` defaults (`gui=None`, print-based
  vprint) already guarantee this; the adapter in Track B must preserve it.
- **PyInstaller spec** (`Ortho4XP.spec`) needs updating for whichever toolkit
  ships; PySide6 hooks are maintained upstream.
- **macOS Tk quirks** currently patched in code (button-2/3 mapping changes in
  Python 3.13, `OsX` stipple workarounds at `O4_GUI_Utils.py:50-52, 1090-1096`)
  disappear entirely with Qt — worth counting as negative maintenance cost.
- **Community expectations:** Ortho4XP has long-time users with muscle memory.
  Keep all existing keyboard/mouse shortcuts working as accelerators alongside
  the new visible controls, and document them under Help → Keyboard & Mouse
  Reference.

---

## 10. Design Revision (Review Round 2): Map-First, Single Window

Review feedback on the first mockups pivoted the Track B design from
"form window + map windows" to **one map-first window**. Decisions recorded
here; mockups in `docs/mockups/trackb-ui-mockups.html` (rev 2).

### 10.1 Shell

- **The map is the main window.** Search field, imagery/ZL selectors and a
  Zones mode toggle in the toolbar; context-sensitive right panel; collapsible
  console drawer below the map; status bar with cursor lat/lon, zoom, source,
  selection counts. The separate "Custom ZL" and "Earth preview" windows are
  retired — zone editing is a mode of the same map.
- **Live provider imagery, Google-Maps style.** The map renders the currently
  selected imagery source at the resolution of the current zoom. Feasible by
  reusing `O4_Imagery_Utils` provider definitions and request code behind an
  async QGraphicsView tile layer with the existing disk cache; no web engine
  required, works offline from cache, and makes provider coverage/quality
  visible before building. Changing Imagery or Build ZL re-renders live.
- **Search** over airports (indexed from X-Plane Global Airports `apt.dat` at
  onboarding: ICAO, name, city, country) plus a small bundled gazetteer for
  cities/countries. Selecting a result zooms and selects the containing tile.
- **Gestures:** pinch/scroll to zoom, two-finger (secondary) drag to pan;
  click = select tile, Shift-click = contiguous range,
  Cmd/Ctrl-click = non-contiguous toggle.
- **Tile info pane** (right panel, when a built tile is selected): imagery
  source, ZL (+zone count), mesh build date, imagery update date, size on disk
  (scanned once, cached), and an "Installed in X-Plane" toggle replacing
  Ctrl-click symlinking.
- **Everything else** (GeoTIFF creation, mesh extraction, community mesh,
  cache deletion, open tile folder) lives in an Actions menu on the selection
  and in the Zones-mode side panel; overlay linking under Tools.

### 10.2 Build experience

- Clicking Build zooms the map to the selection and locks editing (view-only
  pan/zoom — open question whether to freeze entirely).
- **Per-tile progress on the map:** indeterminate spinner while a tile waits or
  runs non-measurable steps (Triangle4XP reports no %), switching to a
  determinate ring with percentage for download/convert/DSF phases; green check
  when done, dashed outline while queued.
- **Activity panel** replaces the right panel during builds: overall progress +
  ETA, one card per active tile (step, %, thread counts, throughput), Stop.
- **Console drawer** shows tile-prefixed stdout with level/filter/copy/save.
- **Concurrency honesty:** today tiles build sequentially with parallelism
  inside a tile. v1 target is pipeline overlap (tile N downloads while tile
  N+1 triangulates); fully parallel tile builds are a larger backend change
  (RAM/Triangle4XP contention) and staged for later.

### 10.3 Configuration

- **Output folder moves to app config** (Settings → General & Paths); removed
  from the main window.
- **First-launch onboarding** (4 steps, skippable, re-runnable from Help):
  welcome → locate X-Plane install (auto-detected; unlocks Custom Scenery
  target, overlay source, airports search index) → output folder + cache
  location → default imagery provider and ZL.
- Settings scope is now **per-category**: General & Paths and Network are
  application-wide; Mesh, Masks, Imagery, Roads, Water, Rendering keep the
  Tile ↔ Global switch.

### 10.4 Rebase onto `dev`

This branch was originally cut from `master`; after review round 2 it was
rebased onto `origin/dev`, which carries the auto-patch subsystem
(`src/auto_patch/`, runway slope patches from CIFP/AIRAC data). UI-relevant
deltas absorbed into this plan:

- New settings `auto_patch` (tile-scoped, None/ICAO/All) and `cifp_data_path`
  (app-scoped path) — slotted into §7.3 categories 4 and 1 respectively.
  `cifp_data_path` defaults to the X-Plane install, which the onboarding
  wizard already captures.
- `O4_UI_Utils` gained `total_elapsed`/`total_bottom_line` (per-tile total
  build time) — feeds the activity panel's per-tile timing directly.
- The GUI contact surface (three channels, two Tkinter files) is unchanged on
  `dev`, so the Track B port plan is unaffected.

### 10.5 Impact on the roadmap

The P0–P2 Tkinter-track items in §8 are unchanged. Within Track B, the port
order shifts: the QGraphicsView live-tile map engine moves from "P3 polish" to
the core deliverable, and the onboarding wizard is added as a P3 item. The
apt.dat airport index and the tile-info scanner are new, UI-independent modules
that can be built and unit-tested before any Qt code.
