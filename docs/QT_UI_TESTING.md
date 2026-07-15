# Testing the New Qt UI (preview build)

The map-first UI from the rev-2 mockups, wired to the real build pipeline.
The legacy Tkinter UI is untouched — `python3 Ortho4XP.py` still works, and
both UIs share the same config files, tiles, and caches.

## Run it

From your existing Ortho4XP environment (the one that already builds tiles):

```bash
source venv/bin/activate        # or however you activate your env
pip install PySide6
python3 Ortho4XP_Qt.py
```

On first launch a four-step **setup assistant** runs (Welcome → X-Plane →
Folders → Imagery). It auto-detects X-Plane installs, shows what setting the
folder unlocks (one-click install, airport search, overlay source), and is
skippable — rerun it anytime from Help → "Run setup assistant…". Finishing
seeds `custom_scenery_dir`/`custom_overlay_src` in your global config when
they're empty.

## What's implemented in this preview

- **Live map** — the map renders the selected imagery source at the current
  zoom (change Imagery in the toolbar and watch it re-render). Providers that
  can't be live-mapped (combined/WMS sources) fall back to OSM with a note in
  the status bar. Tiles cache under `Previews/livemap/`.
  The map is merged with the build pipeline's own imagery cache: view tiles
  covered by an assembled orthophoto in `Orthophotos/` are cropped from it
  (areas you've built render instantly, offline), and raw tiles the map
  downloads at ZL17+ are stored build-grade and reused by later builds, so
  browsing an airport closely pre-seeds its build.
  Loading is progressive and never blocks the view, Google/Apple-Maps style:
  a low-res world base layer is always resident, coarse levels fill in first
  and sharpen to the actual zoom from the screen center outward, downloads
  start only after the view settles for ~¼ s (flinging through zoom levels
  costs nothing), and anything that pans or zooms out of view is cancelled
  immediately. Six download workers max; already-seen areas come straight
  from the disk cache.
- **Gestures** — pinch or two-finger scroll to zoom, two-finger click
  (right/middle button) drag to pan; click selects a tile, ⇧-click selects a
  contiguous block, ⌘/Ctrl-click toggles tiles in and out.
- **Search** — type an ICAO, airport, or city in the search box (index built
  from your X-Plane Global Airports on first launch — takes a minute, watch
  the console), or a tile like `+48-006` / `48 -6`. Enter jumps to the top
  result.
- **Tile info pane** — select a built tile: imagery source, ZL (+zones),
  mesh and imagery dates (with time), size on disk, and the **Installed in
  X-Plane** switch (creates/removes the `zOrtho4XP_*` link in Custom
  Scenery; links only, per the spec — never touches your tile data).
- **Built-tile overlays + legend** — built tiles show a colored fill and
  border (color = ZL, per the legacy color code), the provider + ZL label
  from moderate zoom in, a doubled border when installed in X-Plane, and
  `*` for custom zones. The legend (bottom-left) explains all of it;
  toggle it via View → Show map legend. Tiles are detected in the output
  folder from Settings — if yours live elsewhere, point the output folder
  there and View → Refresh tiles.
- **Building** — select tiles, choose steps, Build. The map zooms to the
  selection and locks; each tile's ring shows **whole-tile completion**
  (steps own weighted slices of 0-100 %, so the ring climbs once and never
  restarts; it holds steady with a spinner during mesh triangulation, which
  reports no percentage). The Build box morphs into a live progress list —
  one bar per tile with its current step, elapsed time, estimated time
  remaining, and Stop at the bottom — and reverts a few seconds after the
  build ends. Tiles build sequentially (pipeline overlap comes later).
- **macOS app bundle** — `./build_mac_app.sh` produces a double-clickable
  `dist/Ortho4XP.app` with a real "Ortho4XP" menu bar (PyInstaller,
  windowed). Untested from the dev container — please report how the first
  build goes. When running from source, the menu bar will still say
  "Python"; that's a macOS bundle-name rule only an .app can fix.
- **Console drawer** — toggle from the status bar; verbosity set in Settings.
- **Tools → Link overlays folder** — the old `o`-key overlay link.
- **Full settings window** (⚙ or Cmd/Ctrl+,) — the categorized window from
  the mockups: sidebar categories, search over names/keys/hint text,
  **Global defaults ↔ This tile** scope switch (tile scope creates the tile
  config on Save if it doesn't exist yet), amber modified-value dots with
  right-click *Reset to default / Reset to global / Copy to global*, inline
  descriptions (full hint on hover), and a Show-advanced toggle. Values are
  written to the same `Ortho4XP.cfg` / per-tile cfg files the legacy UI
  uses — the two UIs stay interchangeable.
- **Airport index freshness** — the search index is cached
  (`.airport_index.tsv`) with the source `apt.dat` modification times and
  sizes recorded; every launch compares them and rebuilds only when
  X-Plane's airport data actually changed (e.g. after an X-Plane update).

## Known gaps (deliberate, this round)

- **Zones mode** is stubbed (button disabled) — draw custom ZL zones in the
  legacy UI for now; both UIs read the same configs.
- Download-size estimate on the build panel is a rough order-of-magnitude.
- macOS: if pinch feels off or panning fights scrolling, say so — gesture
  tuning needs real trackpad feedback, which the dev container can't provide.

## Feedback

Pin-style comments welcome, e.g. "map: zoom steps too coarse",
"info pane: add X", "build badges: too small at low zoom". Console output and
`Ortho4XP.log` are unchanged if something breaks — paste the traceback.
