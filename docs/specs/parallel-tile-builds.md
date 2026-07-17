# Parallel tile builds — specification

Status: approved for implementation 2026-07-16.
Extends `docs/specs/engine-protocol-multi-gui.md`, whose section 10
deferred "multiple concurrent build sessions"; this spec implements the
narrower, more useful thing: one build run that processes several tiles
concurrently, each in its own worker process.

## 1. Motivation and constraints

Multi-tile builds run strictly sequentially today
(`o4_engine/session.py::_build_worker` walks the todo list). The core
pipeline cannot run two tiles in one process — configuration is applied
onto module globals, cancellation is one global flag (`UI.red_flag`),
and progress is one channel — so tile-level parallelism requires
process isolation. The existing JSON-lines engine transport
(`Ortho4XP.py --engine-jsonl`, spec section 5) already provides exactly
that: a child process that builds tiles and streams the full typed
event vocabulary over standard output, keeps pipeline prints on
standard error, and answers a `cancel` command by setting its own
`red_flag`.

## 2. User model

- New app setting `max_build_slots` (int, default 0 = **Auto**): how
  many tiles build concurrently. `1` restores the historic in-process
  build exactly. Auto (added 2026-07-16) scales to the machine with the
  formula the bottleneck warrants — NOT bare core count:
  `min(cores // 3, memory_gigabytes // 6)` clamped to 1..6. The memory
  divisor is deliberately soft (owner ruling 2026-07-16: macOS memory
  compression and fast solid-state swap page gracefully — the divisor
  guards the paging performance cliff of actively swept rasters, not
  out-of-memory), and Auto's ceiling of six is politeness to the
  OpenStreetMap and imagery servers, not hardware. EXPLICIT settings up
  to eight are honoured for big-memory machines. Worker children learn
  their sibling count via the `O4_PARALLEL_BUILD_SIBLINGS` environment
  variable so their own Auto conversion slots share the processor
  instead of each claiming all of it.
- The sibling knobs adopt the same "0 = Auto" convention, resolved in
  `O4_Parallel_Utils`: `max_convert_slots` Auto = cores − 2 (floor 2,
  cap 16; CPU-bound), `max_download_slots` Auto = a fixed 2
  (network-bound — cores are irrelevant; external-drive users keep the
  documented explicit 1). The mask stage already auto-scaled
  (`O4_Mask_Utils.masks_build_slots`). Existing configuration files
  with explicit values keep their explicit behaviour.
- Per-tile cancellation: every row in the build progress list gets an
  operating-system-standard close ("X") button. Cancelling a queued
  tile removes it from the queue; cancelling an active tile stops that
  tile after its current step (the same graceful semantics the global
  Stop button has) while every other tile continues. The global Stop
  button keeps its current meaning (stop everything).
- The progress rows themselves gain proper padding (they are currently
  zero-margin) alongside the new button.

## 3. Architecture

### 3.1 Two run modes, one scheduler entry

`EngineSession.build(...)` chooses at call time:

- `max_build_slots == 1` (or a single-tile todo): the historic
  in-process `_build_worker`, unchanged except for per-tile-cancel
  support (section 3.4). Behaviourally identical to today when no
  per-tile cancel is issued — the existing engine-session tests pin
  this.
- `max_build_slots > 1`: the subprocess scheduler. Up to N worker
  children run at once; each child is handed EXACTLY ONE tile per
  `build` command, so a child's run lifecycle equals one tile's
  lifecycle.

### 3.2 Worker children

- Command: from source,
  `[sys.executable, <repo>/Ortho4XP.py, "--engine-jsonl"]`; in the
  frozen app, `[sys.executable, "--engine-jsonl"]` —
  `Ortho4XP_Qt.py` gains the same early argv branch `Ortho4XP.py`
  already has (before any Qt import), so the bundled executable can
  serve as its own worker.
- The parent sends one `build` command per assigned tile (tiles list of
  length 1, plus provider/zoomlevel/custom_build_dir/do_* exactly as
  the in-process call would receive). When a child's run completes
  (its `RunDone` arrives / the command reply returns), the scheduler
  either assigns it the next queued tile (children are REUSED — one
  interpreter start-up per slot, not per tile) or closes its standard
  input, which ends `jsonl.serve` and the process.
- Child standard error (pipeline prints, crash text) is read by a
  drain thread and re-printed to the parent's standard output prefixed
  with the tile, e.g. `[+48-006] …`, so the Qt console tee shows an
  attributable interleaved log.
- Spawn failure or a handshake timeout at run start degrades loudly:
  one warning line, then the whole run falls back to the historic
  in-process worker. A parallel build must never be less reliable than
  the sequential one.

### 3.3 Event merging

A reader thread per child parses event lines back into typed events
(name → dataclass registry over `o4_engine.events`; unknown fields
dropped, unknown types ignored, per the additive protocol rule) and
re-emits them through the parent session's `_emit`, which re-stamps
`seq`/`ts` so subscribers see one coherent stream.

- Forwarded: `TileState`, `StepProgress`, `BuildDone`,
  `AutoPatchBegin`, `AutoPatchProgress`, `Log`, non-fatal `Error`.
- Suppressed: the child's `EngineHello`, `RunEta`, `RunDone`, scan
  events (children never scan). The parent emits its own run-level
  events: `RunEta` with `elapsed_seconds` and
  `remaining_seconds=None` in parallel mode (a dash, never a wild
  number; a parallel-aware time model is recorded follow-up work) and
  one final `RunDone` with aggregate done/error/cancelled counts.
- Additive protocol bump (1.0 → 1.1): `AutoPatchBegin` and
  `AutoPatchProgress` gain `lat`/`lon` fields (default 0). In-process
  emission stamps them from the current tile; the parent stamps them
  when forwarding a child's events. Views that ignore them are
  unaffected.
- Build-time recording (`_record_tile_build`, the tile time model)
  happens INSIDE the child, exactly as in a sequential run.

### 3.4 Cancellation

New API `EngineSession.cancel_tile(lat, lon) -> bool` (and a matching
JSONL command, additive):

- Queued tile (either mode): removed from the queue;
  `TileState(state="queued", label="stopped")` is emitted so the row
  shows "stopped".
- Active tile, subprocess mode: the parent writes a `cancel` command to
  that child only. The child's worker red-flags, ends the tile after
  the current step, emits its own `TileState(..., "stopped")`, and the
  child is then recycled for the next queued tile (a fresh process —
  after a cancel the child is closed and a new one spawned, so a
  red-flagged interpreter never carries state into the next tile).
- Active tile, in-process mode: the session records the tile in a
  cancelled set and raises `UI.red_flag`. When the worker unwinds from
  the aborted tile it finds the tile in the cancelled set with no
  global cancel pending, CLEARS `red_flag`, reports the tile
  "stopped", and continues with the remaining queue.
- `cancel()` (global) keeps today's semantics in both modes: in-process
  it raises the flag and the loop breaks; in subprocess mode it cancels
  every child and drains the queue. The existing
  `test_cancel_mid_run_skips_remaining_tiles` contract is unchanged.

### 3.5 Shared-cache discipline

Per-tile caches (OSM per-tile files, orthophoto directories, airport
insets, tile overlays, coastline bands, build-time records) are keyed
by tile and cannot collide across concurrent tiles. The audited
collision surface is the BASE ELEVATION store: `build_combined_raster`
fetches the 3×3 tile neighbourhood, so two adjacent tiles building
concurrently race to download the same `.hgt`/`.tif`.

New helper `src/O4_File_Lock.py`:
`hold_file_lock(target_path, timeout_seconds)` — a cross-process
advisory lock context manager (`<target>.lock` created with
`O_CREAT | O_EXCL`, holder process id inside, polling wait, locks older
than one hour treated as stale and broken loudly). Applied around the
download-if-missing critical section of
`O4_Airport_Elevation_Insets.ensure_base_tile`, with a cache re-check
after acquisition (double-checked locking). Waiters therefore block
until the first downloader finishes and then hit the cache. No other
call site takes the lock in v1; the audit above is recorded here so a
future cache gains the same treatment knowingly.

Overpass etiquette: concurrency multiplies simultaneous queries; the
`max_build_slots` hint carries the "keep it at 2–3" guidance (the
2026-07-03 no-query-racing ruling is about racing one query against
multiple servers, which this does not do).

### 3.6 Qt build panel

- Row padding: per-row content margins (6, 4, 6, 4) and 2 px internal
  spacing; 6 px spacing between rows (both currently 0/4 with
  zero-margin rows).
- Per-row cancel: a flat `QToolButton` on the right of the row header
  carrying the platform style's standard close icon
  (`QStyle.SP_TitleBarCloseButton` — the operating-system-appropriate
  "X"), tooltip "Cancel this tile". Clicking calls
  `session.cancel_tile(lat, lon)`, disables the button and sets the
  row status to "stopping…"; terminal states (`done`, `error`,
  `stopped`) disable it.

### 3.7 Cross-tile coordination (added 2026-07-16, owner request)

Three mechanisms keep a parallel run efficient AND polite to shared
servers:

- **Staggered first wave.** The first worker starts immediately; each
  further first-wave assignment waits `INITIAL_ASSIGNMENT_STAGGER_SECONDS`
  (15 s), so N children never open N simultaneous Overpass/download
  bursts at second zero. A tile whose OpenStreetMap caches are already
  warm skips its stagger — the delay exists only to space out downloads
  that would actually happen. Later assignments are naturally staggered
  by build completion.
- **Parent-side OpenStreetMap cache warmer.** A warmer thread in the
  parent pre-downloads QUEUED (unassigned) tiles' OSM layer caches —
  airports first, then the same specification list the in-build
  prefetch uses (`O4_Vector_Map.osm_layer_warm_specifications`) — one
  tile at a time, one Overpass request at a time. By the time a worker
  child receives a queued tile, its vector data is a cache hit. The
  no-race guarantee is structural, not lock-based: the warmer only
  touches tiles still in the queue, and `_assign_next_locked` never
  hands out the tile being warmed right now (the warmer dispatches idle
  children the moment it finishes one). Warm failures are per-tile and
  non-fatal — the child downloads for itself, exactly as without a
  warmer. Net server profile: at most one warmer request plus the
  currently building tiles' staggered bursts.
- **Sibling-aware download slots.** Auto `max_download_slots` drops to
  one orthophoto stream per tile when siblings are present (a six-tile
  run opens six streams, not twelve); Auto convert slots already divide
  the processor by the sibling count (section 2).

**Considered and rejected for v1 — multi-tile Overpass requests.**
Bundling several tiles' OSM into one request (a union bounding box,
split client-side into the per-tile caches) is technically exact:
`recurse-down` returns every way's nodes, so per-tile membership ("at
least one node in the tile box") is reproducible offline. It is still
the wrong lever: response size scales with area, and public Overpass
instances punish large responses with timeouts far more readily than
they punish a modest request COUNT — request concurrency, not count, is
what their rate limiting meters. The warmer already reduces concurrency
to the sequential-build profile with zero response-size risk and zero
cache-format change. Recorded as follow-up only if real-world runs show
request-count throttling (revisit alongside the 2026-07-03 batched
union-query work, which operates within one tile).

### 3.8 Phase-aware orchestration (added 2026-07-16, owner request)

The scheduler stops handing a worker child its whole tile and instead
dispatches ONE STEP AT A TIME (`build` gains an additive `steps`
parameter selecting exact step keys; pipeline steps already communicate
through files on disk, so a child between steps holds almost nothing).
The parent therefore knows and controls every tile's phase, which
buys:

- **Resource-class limits.** Each step key belongs to a class, and the
  two network-bound steps get SEPARATE classes because they exhaust
  SEPARATE servers (2026-07-16 makespan ruling: the goal is minimal
  total time for the whole queue, and imagery dominates it — a tile
  downloading OpenStreetMap data must not steal imagery budget):
  `vector` → "osm" (cap 2), `imagery` → "imagery" (cap 2),
  `mesh`/`masks`/`overlays` → "compute" (cap `max(1, slots - 1)`, one
  slot's worth of headroom so a network phase is always feedable). A
  child whose next step's class is full simply WAITS (idle at the
  protocol level, negligible memory) until a completion frees
  capacity. This replaces the first-wave stagger of section 3.7, which
  is RETIRED — the osm-class cap is the same protection, structurally.
- **Memory-aware mesh admission.** Mesh memory varies an order of
  magnitude with the per-tile elevation detail level, so the class cap
  alone is not enough. At run start the parent reads each tile's
  `elevation_level` from its configuration and assigns a peak-memory
  estimate (`MESH_MEMORY_ESTIMATES_GB`: auto 2, coastline 3, 30 m 1.5,
  10 m 3, 5 m 8, 1 m 18); a mesh step is admitted only while the sum
  of running estimates stays within the machine budget (total memory
  minus 4 GB headroom, floor 4). One mesh is ALWAYS admitted — a
  single tile must never deadlock, however big its raster. Estimation
  failures degrade to the default estimate, never to a scheduling
  failure.
- **Recorded follow-up — adaptive backpressure.** Static caps do not
  react to a server actively throttling. Closing that loop needs a
  child→parent "server pushing back" signal (a typed event fed from
  the download retry paths) and real-world throttling data; revisit
  after live multi-tile runs.
- **Finish-first priority.** When capacity frees, blocked children are
  dispatched before new tiles enter the run, later steps before
  earlier ones — work in progress drains before new intermediate files
  pile up, which matters at forty tiles queued.
- **The queue at scale.** Unchanged mechanics (tiles assigned as
  children free up, warmer pre-downloading ahead), now with the
  guarantee that a forty-tile selection cannot stampede any single
  resource: at most two tiles touch the network, at most slots-minus-one
  crunch, everything else waits its turn.

Event-stream consequences (the parent owns tile lifecycle now):

- The child emits `BuildDone`/`TileState(done)`/`RunDone` per STEP
  command; the parent suppresses the intermediate ones and forwards a
  tile-level `BuildDone`/`TileState(done)` only when the tile's LAST
  step completes. A failed step forwards the failure and aborts that
  tile's remaining steps.
- `StepProgress` percent from a single-step child plan spans 0–100 of
  that step; the parent remaps it into the tile's full-plan window
  (`base + width × percent`) before re-emitting, so views see exactly
  the whole-tile percent they saw before.
- Cancelling an active tile BETWEEN steps needs no child cancel at all:
  the parent just stops dispatching and reports the tile stopped (the
  child stays clean and reusable). Mid-step cancel keeps the section
  3.4 semantics (cancel command, retire, respawn).

## 4. Code layout

- `src/o4_engine/events.py` — additive `lat`/`lon` on the auto-patch
  events; `PROTOCOL_VERSION = "1.1"`.
- `src/o4_engine/session.py` — scheduler (slot pool, queue, per-child
  reader/drain threads, event filter/re-emit, fallback), `cancel_tile`,
  in-process cancelled-set handling, run-level `RunEta`/`RunDone` in
  parallel mode.
- `src/o4_engine/jsonl.py` — `cancel_tile` command registration
  (additive).
- `Ortho4XP_Qt.py` — early `--engine-jsonl` branch (frozen-app worker
  path), mirroring `Ortho4XP.py:24-27`.
- `src/O4_File_Lock.py` — the advisory lock helper (no GUI imports).
- `src/O4_Airport_Elevation_Insets.py` — `ensure_base_tile` locking.
- `src/O4_Cfg_Vars.py` / `src/O4_Settings_Model.py` —
  `max_build_slots` (app scope, "Network & Downloads" category).
- `src/O4_Qt_GUI.py` — row padding, per-row cancel button, handler
  wiring.

## 5. Acceptance

- `max_build_slots=1` runs are event-for-event identical to today
  (existing `tests/test_engine_session.py` green, unmodified except
  for added cancel_tile coverage).
- Scheduler tests drive the REAL subprocess path against a stub worker
  script (a tiny stand-in speaking the JSONL protocol, no pipeline):
  two tiles at two slots demonstrably overlap; merged events stay
  per-tile ordered; `RunDone` aggregates; `cancel_tile` on a queued
  tile never starts it; `cancel_tile` on an active tile stops only it;
  global cancel stops everything; a crashing child yields
  `TileState(error)` + `BuildDone(ok=False)` and the run continues;
  spawn failure falls back to in-process and still completes.
- File-lock tests: mutual exclusion across processes, stale-lock
  recovery, double-checked cache path.
- Qt tests: rows carry the padding, the close button exists with a
  standard icon, clicking it calls `cancel_tile` once and disables the
  button, terminal states disable it.
- Live check (manual): a 4-tile coastal build at `max_build_slots=2`,
  cancelling one queued and one active tile mid-run.

## 6. Out of scope (recorded)

- A parallel-aware run ETA model (parallel runs show elapsed + dash).
- Multiple concurrent build RUNS / `run_id` (still deferred, spec
  section 10 of the engine protocol).
- Locking any cache beyond the base elevation store (audit in 3.5).
- Per-step pipelining across tiles inside one process.
