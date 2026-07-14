# Spec: "Installed in X-Plane" Switch

**Status:** Draft for review · **Scope decision:** links only —
`scenery_packs.ini` management is explicitly **out of scope** (owner decision,
review round 3).

## 1. Summary

A switch in the tile info pane (and a matching batch action) that installs or
uninstalls a built Ortho4XP tile in X-Plane by creating or removing a link in
the Custom Scenery folder. It replaces two hidden mechanisms in the current UI:

- Ctrl-click on a tile in the Tiles Collection window
  (`toggle_to_custom`, `O4_GUI_Utils.py:2156`), and
- the shell-out link creation in `add_symlink`/`remove_symlink`
  (`O4_GUI_Utils.py:1594-1698`), which builds `ln -s` / `MKLINK /J` command
  strings via `os.system`.

The overlays link (`yOrtho4XP_Overlays`, currently the `o` key) is a separate
Tools-menu command and is **not** governed by this switch, though it uses the
same link engine (§5).

## 2. Goals / non-goals

**Goals**

1. One visible, truthful control per built tile: on = X-Plane will load it.
2. Never move, copy, or delete tile data. Links only.
3. Correct, quote-safe link creation on macOS, Windows, and Linux via Python
   APIs — no `os.system` string concatenation.
4. State is always derived from disk, never from cached app state.
5. The link engine is a UI-independent module usable from both the Tk (Track A)
   and Qt (Track B) front ends, and unit-testable headless.

**Non-goals**

- `scenery_packs.ini` ordering or entries (out of scope by decision).
- Managing links owned by other tools or hand-made links that do not resolve
  to an Ortho4XP tile (never touched).
- Copying tiles into Custom Scenery.

## 3. UX specification

### 3.1 Placement and states

- **Tile info pane** (single built tile selected): row
  `Installed in X-Plane [switch]`.
- The switch reflects `link_status(tile)` (§4.3) at the moment the pane is
  shown and after every toggle:

| Status | Switch renders | Notes |
|---|---|---|
| `INSTALLED` | on | Link exists and resolves to this tile's build dir |
| `NOT_INSTALLED` | off | No link with the expected name |
| `BROKEN` | off + warning glyph, tooltip "Broken link found — will be replaced on install" | Expected name exists but does not resolve (moved output folder, deleted tile) |
| `CONFLICT` | disabled + warning glyph, tooltip "A folder named zOrtho4XP_+48-006 already exists in Custom Scenery and is not managed by Ortho4XP" | Real directory or foreign link occupies the name; we never overwrite |
| `UNAVAILABLE` | disabled, tooltip "Set your X-Plane folder in Settings to install tiles" | `custom_scenery_dir` unset/invalid, or scenery dir == build dir |

- **Multi-tile selection:** the build panel shows
  `Installed: 3 of 12 — [Install all] [Remove all]` buttons instead of a
  tri-state switch (mixed-state switches are ambiguous; explicit verbs are not).
- **Settings → General & Paths:** `Install finished tiles automatically`
  (default **on**). When on, each tile is installed at the moment its build
  completes successfully. Batch builds install per-tile as they finish, not at
  the end of the batch.
- **Failure feedback:** a failed operation shows a non-modal toast/status-bar
  message with the OS error (e.g. Windows symlink privilege), plus a log line.
  The switch snaps back to the disk-derived state — it never shows a state that
  isn't real.

### 3.2 Interactions with other features

- **Delete cached data → Tile (whole):** uninstall first, then delete
  (preserves current behavior at `O4_GUI_Utils.py:2084-2088`), and say so in
  the confirmation dialog ("The tile will also be removed from X-Plane").
- **Output folder changed in Settings:** rescan; links into the old location
  report `BROKEN` and the rescan prompt offers "Repair N links" (recreate
  against the new path) — repair is still just link deletion + creation.
- **Grouped build directory** (all tiles in one folder, current `grouped`
  mode): one link `zOrtho4XP_<basename>` covers every tile in the group. The
  per-tile switch then acts on the group link and the info pane labels it
  "Installed in X-Plane (whole group)". Install/uninstall from any member tile
  affects the group; the map hatches all member tiles together.

## 4. Behavior specification

### 4.1 Naming and targets (unchanged from today)

- Per-tile link name: `zOrtho4XP_` + short lat/lon (e.g. `zOrtho4XP_+48-006`),
  created in `custom_scenery_dir`, pointing at the tile's build directory
  (resolved via `FNAMES.build_dir(lat, lon, custom_build_dir)`).
- Grouped link name: `zOrtho4XP_` + basename of the grouped build dir,
  pointing at that directory.

### 4.2 Link creation (replaces `os.system` calls)

- **macOS / Linux:** `os.symlink(target, link, target_is_directory=True)`.
- **Windows:** try `os.symlink(..., target_is_directory=True)` first — this
  succeeds without elevation when Developer Mode is on (Windows 10 1703+,
  Python passes `SYMLINK_FLAG_ALLOW_UNPRIVILEGED_CREATE`). On `OSError`
  (privilege not held), fall back to a **directory junction** — the current
  `MKLINK /J` behavior — via `_winapi.CreateJunction(target, link)`, guarded so
  a future stdlib change degrades to a clear error rather than a crash.
  Junctions require no privileges and X-Plane follows both.
- Targets are stored **absolute and resolved** (`os.path.realpath`), matching
  current behavior.
- All failures raise; the caller maps them to the §3.1 feedback. No silent
  `except: pass`.

### 4.3 Status detection (truth from disk)

```
def link_status(tile) -> LinkStatus:
    UNAVAILABLE  if custom_scenery_dir unset, missing, or == build dir
    name = expected link name (per-tile or grouped)
    if name does not exist            -> NOT_INSTALLED
    if islink/isjunction(name):
        if realpath(name) == realpath(build_dir) -> INSTALLED
        if target missing                        -> BROKEN
        else                                     -> CONFLICT   # foreign link
    else                                         -> CONFLICT   # real folder
```

- Junction detection on Windows: `os.path.islink` is False for junctions; use
  `os.stat(..., follow_symlinks=False)` reparse-point attributes.
- A full-scenery scan (`installed_tiles()`) runs at startup and on Refresh to
  hatch installed tiles on the map; per-tile status checks run on selection.

### 4.4 Uninstall

- Delete the link only, and only after re-verifying it resolves to the
  expected tile (or is `BROKEN` with the expected name). `CONFLICT` names are
  never deleted.
- Removing a `BROKEN` link with the expected name is allowed without
  confirmation (it points at nothing).

## 5. Module design

New UI-independent module `src/O4_Scenery_Links.py`:

```
class LinkStatus(Enum): INSTALLED, NOT_INSTALLED, BROKEN, CONFLICT, UNAVAILABLE

def link_status(lat, lon, build_dir, scenery_dir, grouped=False) -> LinkStatus
def install(lat, lon, build_dir, scenery_dir, grouped=False) -> None   # raises
def uninstall(lat, lon, build_dir, scenery_dir, grouped=False) -> None # raises
def installed_tiles(scenery_dir) -> dict[(lat, lon) | str, Path]
def install_overlay_link(overlay_dir, scenery_dir) / uninstall_overlay_link(...)
```

- No Tkinter/Qt imports; no `UI.vprint` — returns/raises, callers log.
- `O4_GUI_Utils.add_symlink/remove_symlink/add_overlay_symlink` become thin
  wrappers during Track A; Track B calls the module directly.

## 6. Acceptance criteria

1. Toggling on creates a working link on all three OSes; X-Plane loads the
   tile on next start. Paths containing spaces and quotes work (regression:
   current `os.system` quoting).
2. Toggling off removes only the link; tile data is untouched (verified by
   checksum/mtime of build dir before/after).
3. On Windows without Developer Mode, install falls back to a junction and
   succeeds without elevation.
4. A hand-created folder named like a link is reported `CONFLICT`, the switch
   disables, and nothing is deleted or overwritten.
5. Moving the output folder flags affected tiles `BROKEN`; repair recreates
   links; no stale hatching remains on the map after Refresh.
6. Grouped mode: one link governs the group; per-tile switches reflect and
   control the group link consistently.
7. "Install finished tiles automatically" installs each tile within a second
   of its build completing, including during batch builds.
8. All module functions covered by headless unit tests (tmp dirs standing in
   for scenery/build folders) on the three platforms in CI.
9. Every install/uninstall/repair writes one log line:
   `link installed: zOrtho4XP_+48-006 -> /path/to/tile` (and equivalent).

## 7. Out of scope (recorded for later)

- `scenery_packs.ini` ordering — the known "overlays above ortho" pitfall.
  Deliberately excluded; revisit only if user reports justify it.
- Copy-install for users whose Custom Scenery must be self-contained.
