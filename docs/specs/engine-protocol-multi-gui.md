# Spec: One Engine, Many Views — the Ortho4XP Engine Protocol

**Status:** Draft for review · **Owner ask (2026-07-15):** a foundation that
supports multiple graphical front ends on one core engine, with
Model/View/Controller-style separation; the Qt application remains the
working front end now, and an XPScenery Doctor "Builder" module must be
addable later without re-architecting.

## 1. Summary

Ortho4XP's build engine is Python and stays Python — the pipeline
(`O4_Vector_Map` → `O4_Mesh_Utils` → `O4_Mask_Utils` → `O4_Tile_Utils`, plus
`auto_patch`) is not portable and must not be duplicated per front end. What
front ends need from it is narrow and already half-formalized: start work,
stream progress, stream results, cancel, read/write settings, inspect tiles.
This spec turns that half-formal seam into an explicit **engine session
API** (the Controller) with **typed events** (the Model's outputs), consumed
identically by every View:

- the PySide6 application (in-process consumer, today),
- a JSON-lines standard-input/output transport (subprocess consumer:
  XPScenery Doctor's Builder module, automation, continuous integration),
- the command line (a thin consumer of the same events).

One session implementation, two transports, N views. The discipline that
makes this work is the same one the repo already enforces for core modules
("never import a GUI toolkit"): **views may only talk to the session API,
and the session API never knows which view is listening.**

## 2. Model / View / Controller mapping

| Layer | What it is here | Where it lives |
|---|---|---|
| **Model** | The build engine and its domain state: tile pipeline, auto-patch, tile scanning (`O4_Tile_Info`), link management (`O4_Scenery_Links`), imagery providers, the settings registry (`O4_Cfg_Vars`), config files. Headless, GUI-free (already enforced by convention). | `src/` core modules, unchanged |
| **Controller** | `o4_engine`: a session façade exposing *commands* (verbs a view can invoke) and emitting *events* (typed, serializable facts about progress and results). Owns threading, cancellation, event batching/throttling policy. | new package `src/o4_engine/` |
| **View** | Anything that renders events and issues commands: Qt MainWindow, XPScenery Doctor Builder, the CLI, test harnesses. Contains **zero** business logic — a view that needs a new fact asks for a new event, never reaches into the Model. | `O4_Qt_GUI.py` (migrated), Doctor (later), `Ortho4XP.py` CLI |

## 3. Goals / non-goals

**Goals**

1. A view can be written in any language against a documented, versioned
   protocol, without reading engine source.
2. The Qt application migrates onto the session API with no user-visible
   change — it is the proof the boundary is real, and stays the reference
   view while the owner tests.
3. Event stream and command set sufficient for the Doctor Builder module:
   scan working directory, tile info, configure, build N tiles with live
   per-tile/per-step progress, cancel, install/uninstall links.
4. Single source of truth for settings metadata: views render settings
   from the registry (`O4_Cfg_Vars` types/defaults/hints), never hardcode.
5. An engine-only macOS bundle target (no PySide6) a native shell can ship.

**Non-goals**

- Porting any pipeline code to Swift (never).
- Embedding Python in-process in a native shell (subprocess only).
- Replacing the Qt application (it remains the Windows/Linux/many-Mac view).
- A remote/network protocol (local stdio only; no sockets, no auth surface).
- Migrating the legacy Tkinter application (it keeps the legacy adapter
  until retirement; fixes only, per repo convention).

## 4. Current seams this formalizes (inventory)

The engine already talks to front ends through exactly these channels — the
protocol is their typed union, not an invention:

| Existing seam | Where | Becomes |
|---|---|---|
| `progress_bar(nbr, pct)` | `O4_UI_Utils.py:27` | `StepProgress` event |
| `red_flag` polled cancellation | `O4_UI_Utils.py:8` | `cancel` command (same flag underneath) |
| `auto_patch_begin/progress` | `O4_UI_Utils.py:33,47` | `AutoPatchBegin` / `AutoPatchProgress` events |
| `vprint` / `logprint` stdout | `O4_UI_Utils.py:69` | `Log` event (level-tagged) |
| scan generators `iter_scan_tiles`, `iter_installed_tiles` | `O4_Tile_Info.py`, `O4_Scenery_Links.py` | `ScanProgress` / `ScanBatch` events (the 2026-07-15 streaming-scan work is already event-shaped) |
| Qt worker signal classes `_BuildSignals`, `_ScanSignals` | `O4_Qt_GUI.py:193,199` | deleted — Qt consumes session events |
| flat `key=value` config read/write | `O4_Config_Utils` | `config.get/set/validate/describe` commands |
| link install/uninstall/status | `O4_Scenery_Links` | `links.install/uninstall/status` commands |

## 5. The session API (Controller)

New package `src/o4_engine/` (three modules, deliberately small):

- **`events.py`** — frozen dataclasses, the protocol's vocabulary. Every
  event carries `event` (type name), `seq` (monotonic int), and `ts`
  (epoch seconds). Initial set:
  `EngineHello` (version, protocol version, capabilities),
  `Log` (level, text),
  `ScanProgress` (phase, done, total),
  `ScanBatch` (built tiles added, installed tiles added),
  `ScanDone` (built count, installed count),
  `TileState` (lat, lon, state, label, percent),
  `StepProgress` (lat, lon, step key, percent),
  `AutoPatchBegin` (airport identifiers),
  `AutoPatchProgress` (airport, done, total, label, status, eta seconds),
  `BuildDone` (lat, lon, ok, error text),
  `RunDone` (done count, error count, cancelled flag),
  `Error` (fatal, text).
- **`session.py`** — `class EngineSession`: owns worker threads, the
  cancellation flag, and event dispatch. Commands (Phase 1 set):
  `scan(working_dir, custom_scenery_dir)`,
  `tile_info(lat, lon)`,
  `build(tiles, steps)` (tiles = list of lat/lon + per-tile config,
  steps = vector/mesh/masks/imagery subset),
  `cancel()`,
  `config_describe()` / `config_get(scope)` / `config_set(scope, values)`
  (scope = global or per-tile; validation errors are return values,
  not events),
  `links_status(...)` / `links_install(...)` / `links_uninstall(...)`.
  Consumers subscribe with `session.subscribe(callback)`; the callback
  receives event objects on an unspecified thread, documented as such —
  marshaling to a UI thread is the view's job (Qt: one `QObject` bridge
  with a single `Signal(object)`; this replaces today's per-purpose signal
  classes and is the same cross-thread rule the repo already learned the
  hard way — never `QTimer` from workers).
- **`jsonl.py`** — the subprocess transport: reads command objects (one
  JSON object per line) on standard input, writes events (one JSON object
  per line) on standard output. `stderr` is reserved for uncaught crash
  text only. Entry: `Ortho4XP.py --engine-jsonl`. Events serialize by
  `dataclasses.asdict`; commands are `{"cmd": ..., "id": ..., args...}`
  and every command gets a `{"reply": id, "ok": ..., ...}` line in
  addition to whatever events it triggers, so callers can await
  completion without heuristics.

**Batching policy lives in the session, not in views.** The scan flush
cadence (~10 Hz) and imagery progress throttling are session concerns so
every view gets the same feel and no view can accidentally flood a pipe.

**Event compatibility rule:** additive only. New fields and new event types
are minor-version bumps; removing or renaming anything is a new protocol
major version, negotiated via `EngineHello.protocol` at startup. Views must
ignore unknown event types and unknown fields (stated in the schema doc).

## 6. How the engine emits (Model → Controller wiring)

`O4_UI_Utils` keeps its module-level functions as the **only** thing core
code calls (no churn in pipeline modules), but their bodies route to the
active session's emitter when one exists, falling back to the legacy `gui`
attribute so Tkinter keeps working:

```
O4_UI_Utils.progress_bar(nbr, pct)  →  session.emit(StepProgress(...))
O4_UI_Utils.red_flag                ←  session.cancel() sets it (unchanged polling)
```

The step-key mapping for `StepProgress` reuses the Qt window's existing
`(nbr, base, slice)` bookkeeping, moved down into the session so the Doctor
gets whole-tile progress identical to Qt's — that logic is Controller, not
View, and today it lives in the wrong layer (`O4_Qt_GUI.py`).

## 7. Migration plan (each phase shippable; Qt keeps working throughout)

- **Phase 1 — session + events, Qt migrates.** Create `o4_engine`;
  implement `EngineSession` wrapping the existing worker/thread patterns;
  route `O4_UI_Utils` through it; rewrite `refresh_tiles`/build-launch in
  `O4_Qt_GUI.py` to subscribe to the session (deleting `_BuildSignals` /
  `_ScanSignals`, keeping one bridge object). Acceptance: Qt behavior
  byte-for-byte equivalent (offscreen verification pattern + existing
  `tests/test_qt_progress.py`, `tests/test_qt_scan_status.py` migrated);
  no core pipeline module diff beyond `O4_UI_Utils`.
- **Phase 2 — JSON-lines transport + golden transcripts.** `--engine-jsonl`
  entry; headless tests drive scan/config/build/cancel through a pipe and
  assert against golden event transcripts (tmp_path fixtures, no network,
  no X-Plane install — same rules as all repo tests). A `Log`-level CLI
  progress renderer replaces ad-hoc prints when running interactively.
- **Phase 3 — native-shell enablement.** Engine-only PyInstaller target
  (reuse `build_mac_app.sh` scaffolding minus Qt; expected far smaller);
  `EngineHello` handshake documented; a ~100-line reference Swift client
  (`docs/specs/` appendix or the Doctor repo) that launches the bundle,
  performs the handshake, runs a scan, and renders `ScanProgress` — the
  seed of the Doctor Builder module.

Phase 1 is the judgment-heavy piece (interface + Qt migration — lead work).
Phase 2 is largely mechanical against this spec (delegable with the golden
transcripts as acceptance). Phase 3 is packaging plus one reference client.

## 8. Testing strategy

1. **Golden transcripts** (Phase 2): recorded JSON-lines sessions for scan,
   config round-trip, a stub build, and cancel-mid-build; asserted
   field-by-field with volatile fields (`ts`, durations, absolute paths)
   normalized.
2. **Transport equivalence:** one test drives the same scripted scenario
   through the in-process subscription and through the pipe and asserts the
   event sequences are identical after serialization — this is the test
   that prevents the two-transport drift risk.
3. **Cancel semantics:** `cancel` during each phase → `RunDone(cancelled)`
   arrives, no further `TileState` events, engine process exits cleanly.
4. Existing Qt tests keep passing unchanged in intent (probe objects swap
   from signal classes to session subscription).

## 9. Risks and mitigations

- **Two transports drift** → single dispatcher, equivalence test (§8.2).
- **Event flood over the pipe** (imagery fetch loops) → throttling in the
  session (§5), never in views; golden tests pin cadence bounds.
- **Schema rot** → additive-only rule + protocol version in `EngineHello`;
  the dataclasses file is the schema (generated JSON examples in the doc).
- **Qt migration regressions while the owner is actively testing** →
  Phase 1 lands behind nothing new: same widgets, same signals cadence;
  offscreen screenshot comparison before/after.
- **Settings divergence between shells** → `config_describe()` is the only
  source of settings metadata; the Qt settings model (`O4_Settings_Model`)
  already reads the registry, so this is codifying current practice.

## 10. Explicitly deferred

- Doctor Builder user experience (native shell design happens in the
  Doctor repo once Phase 3 lands).
- Multiple concurrent build sessions (today's engine is one-build-at-a-time;
  the protocol reserves room — commands return a `run_id` — but only one
  active run is supported).
- Tile preview imagery over the protocol (the Doctor has its own map
  imagery path; revisit only if the Builder needs Ortho4XP's providers).
