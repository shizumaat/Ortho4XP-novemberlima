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

On first launch a small settings dialog asks for your **X-Plane folder** and
**output folder** (both optional — skip and it uses the classic
`Ortho4XP/Tiles`). Setting the X-Plane folder unlocks the airport search and
the "Installed in X-Plane" switch.

## What's implemented in this preview

- **Live map** — the map renders the selected imagery source at the current
  zoom (change Imagery in the toolbar and watch it re-render). Providers that
  can't be live-mapped (combined/WMS sources) fall back to OSM with a note in
  the status bar. Tiles cache under `Previews/livemap/`.
- **Gestures** — pinch or two-finger scroll to zoom, two-finger click
  (right/middle button) drag to pan; click selects a tile, ⇧-click selects a
  contiguous block, ⌘/Ctrl-click toggles tiles in and out.
- **Search** — type an ICAO, airport, or city in the search box (index built
  from your X-Plane Global Airports on first launch — takes a minute, watch
  the console), or a tile like `+48-006` / `48 -6`. Enter jumps to the top
  result.
- **Tile info pane** — select a built tile: imagery source, ZL (+zones),
  mesh date, imagery date, size on disk, and the **Installed in X-Plane**
  switch (creates/removes the `zOrtho4XP_*` link in Custom Scenery; links
  only, per the spec — never touches your tile data).
- **Building** — select tiles, choose steps, Build. The map zooms to the
  selection and locks; each tile shows queued → spinner → progress → ✓/!
  badges; the console drawer opens with the familiar pipeline output; Stop
  requests a halt after the current step. Tiles build sequentially (same as
  the legacy batch build — pipeline overlap comes later).
- **Console drawer** — toggle from the status bar; verbosity set in Settings.
- **Tools → Link overlays folder** — the old `o`-key overlay link.

## Known gaps (deliberate, this round)

- **Zones mode** is stubbed (button disabled) — draw custom ZL zones in the
  legacy UI for now; both UIs read the same configs.
- Settings dialog is minimal (paths + defaults); the full categorized
  settings window comes with the settings-model refactor.
- No onboarding wizard yet — first-run shows the settings dialog instead.
- Download-size estimate on the build panel is a rough order-of-magnitude.
- macOS: if pinch feels off or panning fights scrolling, say so — gesture
  tuning needs real trackpad feedback, which the dev container can't provide.

## Feedback

Pin-style comments welcome, e.g. "map: zoom steps too coarse",
"info pane: add X", "build badges: too small at low zoom". Console output and
`Ortho4XP.log` are unchanged if something breaks — paste the traceback.
