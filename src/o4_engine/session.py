"""EngineSession — the Controller of the one-engine / many-views design
(docs/specs/engine-protocol-multi-gui.md).

Views (the Qt application, the JSON-lines transport, the command line)
invoke COMMANDS on a session and receive typed EVENTS (o4_engine.events)
through :meth:`EngineSession.subscribe`.  The session owns worker threads,
cancellation, whole-tile percent/label math, event batching cadence, and
the run clock estimate — so every view behaves identically and none can
flood a pipe.  Subscriber callbacks run on UNSPECIFIED worker threads;
marshaling onto a user-interface thread is the view's job (the Qt view
uses one QObject bridge with a single cross-thread Signal).

Core pipeline modules never import this package: they keep calling the
``O4_UI_Utils`` module functions, whose bodies route here through the
``O4_UI_Utils.engine_session`` attribute (set on session construction) —
no import cycle, and the legacy Tkinter application keeps working through
the old ``gui`` attribute when no session exists.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Callable, Optional

import O4_File_Names as FNAMES
import O4_UI_Utils as UI

from .events import (
    AutoPatchBegin, AutoPatchProgress, BuildDone, EngineEvent, EngineHello,
    RunDone, RunEta, ScanBatch, ScanDone, ScanProgress, StepProgress,
    TileState,
)

# ---------------------------------------------------------------------------
# Whole-tile step plan (moved from O4_Qt_GUI 2026-07-15: percent math is
# Controller work — the Doctor must see the same numbers as Qt).  The static
# weights are only the FALLBACK display scale; the run clock uses learned
# per-step second estimates (tile_time_model) and live in-step rates.
# ---------------------------------------------------------------------------
STEP_WEIGHTS = {
    "vector": 0.10,
    "mesh": 0.15,
    "masks": 0.10,
    "imagery": 0.60,
    "overlays": 0.05,
}
STEP_LABELS = {
    "vector": "vector data",
    "mesh": "triangulating",
    "masks": "water masks",
    "imagery": "imagery & DSF",
    "overlays": "overlays",
}
SCAN_FLUSH_SECONDS = 0.1     # scan streaming cadence (~10 Hz)
ETA_EMIT_SECONDS = 1.0       # RunEta cadence
RATE_WINDOW_SECONDS = 20.0   # in-step live-rate sliding window
RATE_MIN_SPAN_SECONDS = 3.0  # need this much window before trusting a rate


def plan_steps(do_vector, do_imagery, do_overlays, steps=None):
    """Ordered ``(key, base_fraction, slice_fraction)`` plan for the selected
    steps, slices normalized so a full tile is exactly 1.0.

    ``steps`` (additive, spec §3.8) selects exact step keys and overrides
    the three booleans — the phase-aware orchestrator uses it to hand a
    worker child one step at a time.
    """
    if steps:
        keys = list(steps)
    else:
        keys = []
        if do_vector:
            keys += ["vector", "mesh", "masks"]
        if do_imagery:
            keys.append("imagery")
        if do_overlays:
            keys.append("overlays")
    total = sum(STEP_WEIGHTS[k] for k in keys)
    plan, base = [], 0.0
    for k in keys:
        weight = STEP_WEIGHTS[k] / total
        plan.append((k, base, weight))
        base += weight
    return plan


def step_progress(step_key, bars):
    """Percent (0-100) inside one step from the three legacy progress bars,
    or None when the step reports no usable percentage (mesh triangulation,
    overlay extraction)."""
    if step_key in ("vector", "masks"):
        return bars.get(1, 0)
    if step_key == "imagery":
        return (
            0.55 * bars.get(2, 0)
            + 0.25 * bars.get(3, 0)
            + 0.20 * bars.get(1, 0)
        )
    return None


def _predict_step_seconds(lat, lon, features, steps):
    """Learned per-step estimates, or static-weight-scaled defaults when the
    time model is unavailable (module optional during bring-up)."""
    try:
        from . import tile_time_model
        return tile_time_model.predict_step_seconds(lat, lon, features, steps)
    except Exception:
        return {k: 600.0 * STEP_WEIGHTS[k] for k in steps}


def prediction_features(lat, lon, provider, zoomlevel, custom_build_dir):
    """The feature dictionary a build-time prediction runs on.

    Provider and zoom identify the imagery bucket; the texture counts
    (estimated from this tile's history plus the ``.dds`` files already
    on disk) carry the cold/warm cache state, so a fully cached rebuild
    predicts materially cheaper than a cold one.  Never raises.
    """
    features = {"zoomlevel": zoomlevel, "provider": provider}
    try:
        from . import tile_time_model
        textures_directory = os.path.join(
            FNAMES.build_dir(lat, lon, custom_build_dir), "textures")
        features.update(tile_time_model.estimate_texture_features(
            lat, lon, zoomlevel, provider, textures_directory))
    except Exception:
        pass
    return features


def reweight_plan_by_seconds(plan, estimated_seconds):
    """The plan's step windows re-scaled by predicted per-step seconds.

    The static :data:`STEP_WEIGHTS` say imagery is 60 % of every build —
    wildly wrong for a cached rebuild or an auto-patch-heavy tile.  With
    a per-step second estimate in hand, the whole-tile percent scale is
    proportional to predicted time instead, so the bar moves at a
    steady pace through the build.  Steps missing an estimate keep a
    tiny floor so their window never vanishes.
    """
    keys = [key for (key, _base, _width) in plan]
    seconds = {}
    for key in keys:
        estimate = (estimated_seconds or {}).get(key)
        if not isinstance(estimate, (int, float)) or estimate <= 0:
            estimate = None
        seconds[key] = estimate if estimate is not None else 1.0
    total = sum(seconds.values())
    if total <= 0:
        return list(plan)
    reweighted, base = [], 0.0
    for key in keys:
        width = seconds[key] / total
        reweighted.append((key, base, width))
        base += width
    return reweighted


def _record_tile_build(lat, lon, features, step_seconds):
    try:
        from . import tile_time_model
        tile_time_model.record_build(lat, lon, features, step_seconds)
    except Exception:
        pass


def _configured_build_slots():
    """The resolved ``max_build_slots`` value (1 when unavailable).

    0 means Auto: :func:`O4_Parallel_Utils.effective_build_slots` scales
    to this machine's cores AND memory.  A missing configuration module
    (headless harnesses) stays at the conservative 1.
    """
    try:
        import O4_Config_Utils as CFG
        import O4_Parallel_Utils as PARALLEL_UTILS
        return PARALLEL_UTILS.effective_build_slots(
            getattr(CFG, "max_build_slots", 1))
    except Exception:
        return 1


class _EtaTracker:
    """The run clock: elapsed + a defensible remaining-time estimate.

    Design (2026-07-15, replacing the naive whole-run extrapolation whose
    static step weights made the estimate wildly wrong on auto-patch-heavy
    or imagery-cached tiles):

    * FUTURE work (queued tiles, this tile's not-yet-started steps) is
      priced by the learned per-step model (same-tile history first — the
      dominant rebuild case — then cross-tile rates).
    * The CURRENT step is priced by its own live progress rate over a
      sliding window, so a fully-cached imagery step collapses the
      estimate within seconds instead of at 60 % weight; while the rate
      is unwarmed, the model estimate minus elapsed (floored at zero) is
      used instead.
    * While auto-patch owns the vector step, the auto-patch time model's
      own per-airport ``eta_total_seconds`` (already blended against
      elapsed by that model) replaces the in-step extrapolation — the
      strongest signal available and previously discarded by the Qt view.
    * No estimate is reported at all until SOME basis exists (views show
      a dash, never a wild number).
    """

    def __init__(self, tiles, plan, per_tile_estimates):
        self.t0 = time.time()
        self.tiles = list(tiles)
        self.plan = plan
        self.estimates = per_tile_estimates    # {(lat,lon): {step: seconds}}
        self.tile_index = 0
        self.step_key = None
        self.step_started_at = None
        self.samples = deque()                 # (t, percent) in current step
        self.autopatch = None                  # {"icao": (t_begin, eta_total)}
        self.finished_steps = {}               # (lat,lon) -> set(step)

    # -- feed ------------------------------------------------------------
    def step_started(self, tile, key):
        self.tile_index = max(self.tile_index, self.tiles.index(tile))
        self.step_key = key
        self.step_started_at = time.time()
        self.samples.clear()

    def step_finished(self, tile, key):
        self.finished_steps.setdefault(tile, set()).add(key)
        self.step_key = None
        self.autopatch = None

    def percent_sample(self, percent):
        now = time.time()
        self.samples.append((now, float(percent)))
        cutoff = now - RATE_WINDOW_SECONDS
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()

    def autopatch_begin(self, airports):
        now = time.time()
        self.autopatch = {a: [now, None, False] for a in airports}

    def autopatch_progress(self, airport, status, eta_total_seconds):
        if self.autopatch is None or airport not in self.autopatch:
            return
        entry = self.autopatch[airport]
        if eta_total_seconds:
            entry[1] = float(eta_total_seconds)
        entry[2] = status in ("done", "fail")

    # -- read ------------------------------------------------------------
    def _current_step_remaining(self):
        from .tile_time_model import remaining_step_seconds

        if self.step_key is None or self.step_started_at is None:
            return 0.0
        tile = self.tiles[self.tile_index]
        estimate = self.estimates.get(tile, {}).get(self.step_key)
        elapsed = time.time() - self.step_started_at
        # Auto-patch live model beats everything while it runs.
        if self.step_key == "vector" and self.autopatch:
            now = time.time()
            remaining = 0.0
            have_any = False
            for t_begin, eta_total, finished in self.autopatch.values():
                if finished:
                    continue
                if eta_total is not None:
                    have_any = True
                    remaining += remaining_step_seconds(
                        eta_total, now - t_begin)
            if have_any:
                return remaining
        # Live in-step rate once the window has substance.
        if len(self.samples) >= 2:
            (t_a, p_a), (t_b, p_b) = self.samples[0], self.samples[-1]
            span, gained = t_b - t_a, p_b - p_a
            if span >= RATE_MIN_SPAN_SECONDS and gained > 0.5:
                rate = gained / span
                return max((100.0 - p_b) / rate, 0.0)
        # No live signal: the model estimate, degrading into
        # overrun-proportional remaining once outlived (a None estimate
        # prices the running step by pure elapsed extrapolation rather
        # than as free — "free" made the whole-run figure absurdly low
        # exactly when the current step was the expensive one).
        return remaining_step_seconds(estimate, elapsed)

    def remaining(self):
        """Whole-run remaining seconds, or None with no basis at all."""
        if not self.tiles:
            return None
        total = self._current_step_remaining()
        current_tile = self.tiles[min(self.tile_index, len(self.tiles) - 1)]
        planned = [k for (k, _b, _w) in self.plan]
        for i, tile in enumerate(self.tiles):
            if i < self.tile_index:
                continue
            done = self.finished_steps.get(tile, set())
            for key in planned:
                if key in done:
                    continue
                if tile == current_tile and key == self.step_key:
                    continue      # current step already priced above
                estimate = self.estimates.get(tile, {}).get(key)
                if estimate is not None:
                    total += estimate
        return total if total > 0 else None


class EngineSession:
    """One engine, many views: commands in, typed events out."""

    def __init__(self, version: str = ""):
        self._subscribers: list = []
        self._lock = threading.Lock()
        self._seq = 0
        self._scan_generation = 0
        self._building = False
        # Build display state (moved from the Qt view): current step plus
        # the three legacy bar values plus the auto-patch fold.
        self._current_step = None          # (tile, key, base, width)
        self._bar_values = {1: 0, 2: 0, 3: 0}
        self._autopatch_state = None
        self._eta: Optional[_EtaTracker] = None
        self._eta_last_emit = 0.0
        # Per-tile cancellation (docs/specs/parallel-tile-builds.md §3.4):
        # tiles the user cancelled individually, the tile currently being
        # built by the in-process worker, and whether the WHOLE run was
        # cancelled (the historic red_flag semantics).
        self._cancelled_tiles: set = set()
        self._active_tile = None
        self._cancel_all = False
        # The live parallel run (subprocess scheduler), when one exists.
        self._parallel = None
        UI.engine_session = self
        self._emit(EngineHello(ortho4xp_version=version,
                               capabilities=("scan", "build", "cancel",
                                             "cancel_tile", "tile_info",
                                             "config", "links")))

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------
    def subscribe(self, callback: Callable[[EngineEvent], None]):
        """Register a view callback.  Called on unspecified worker threads;
        exceptions in callbacks are swallowed (a broken view must never
        break the build)."""
        self._subscribers.append(callback)

    def _emit(self, event: EngineEvent):
        with self._lock:
            self._seq += 1
            object.__setattr__(event, "seq", self._seq)
            object.__setattr__(event, "ts", time.time())
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Command: scan (streamed; moved from the Qt view 2026-07-15)
    # ------------------------------------------------------------------
    def scan(self, working_dir: str, custom_scenery_dir: Optional[str]):
        """Scan built tiles + installed scenery, streaming ScanBatch /
        ScanProgress at ~10 Hz.  A newer scan supersedes an in-flight one:
        the superseded worker stops emitting (its generation is stale)."""
        self._scan_generation += 1
        generation = self._scan_generation

        def work():
            import O4_Scenery_Links as LINKS
            import O4_Tile_Info as TINFO

            built_total = 0
            installed_total = 0
            pending_built = {}
            pending_installed = []
            last_flush = 0.0

            def live():
                return generation == self._scan_generation

            def flush(phase, done, total, force=False):
                nonlocal pending_built, pending_installed, last_flush
                now = time.monotonic()
                if not force and now - last_flush < SCAN_FLUSH_SECONDS:
                    return
                last_flush = now
                if pending_built or pending_installed:
                    self._emit(ScanBatch(built=pending_built,
                                         installed=tuple(pending_installed)))
                    pending_built, pending_installed = {}, []
                self._emit(ScanProgress(phase=phase, done=done, total=total))

            try:
                if os.path.isdir(working_dir):
                    phase = "Reading built tiles…"
                    flush(phase, 0, 0, force=True)
                    for done, total, key, info in TINFO.iter_scan_tiles(
                            working_dir):
                        if not live():
                            return
                        if key is not None:
                            built_total += 1
                            pending_built[key] = info
                        flush(phase, done, total)
            except Exception as exc:
                print("Tile scan failed:", exc)
            try:
                if custom_scenery_dir:
                    phase = "Reading installed scenery…"
                    flush(phase, 0, 0, force=True)
                    for done, total, key, _target in (
                            LINKS.iter_installed_tiles(custom_scenery_dir)):
                        if not live():
                            return
                        if key is not None:
                            installed_total += 1
                            pending_installed.append(key)
                        flush(phase, done, total)
            except Exception as exc:
                print("Custom Scenery scan failed:", exc)
            if live():
                flush("", 0, 0, force=True)
                self._emit(ScanDone(built_count=built_total,
                                    installed_count=installed_total))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    # Command: build (worker moved from the Qt view 2026-07-15)
    # ------------------------------------------------------------------
    def build(self, tiles, provider, zoomlevel, custom_build_dir,
              do_vector=True, do_imagery=True, do_overlays=False,
              slots=None, steps=None):
        """Build the given (lat, lon) tiles.  Returns immediately; progress
        arrives as events.  Only one run at a time.

        ``slots`` (default: the ``max_build_slots`` configuration value)
        chooses the run mode (docs/specs/parallel-tile-builds.md §3.1):
        1 keeps the historic in-process worker; more than 1 builds tiles
        concurrently in worker subprocesses, falling back loudly to the
        in-process worker if a worker child cannot be spawned.

        ``steps`` (additive, spec §3.8) selects exact step keys,
        overriding the three booleans — the parent orchestrator sends a
        worker child one step at a time through this parameter.
        """
        if self._building:
            return False
        self._building = True
        self._cancel_all = False
        self._cancelled_tiles = set()
        self._active_tile = None
        UI.red_flag = False
        plan = plan_steps(do_vector, do_imagery, do_overlays, steps=steps)
        if slots is None:
            slots = _configured_build_slots()
        slots = max(1, int(slots))
        if slots > 1 and len(tiles) > 1:
            from . import parallel
            run = parallel.ParallelBuildRun(
                self, list(tiles), provider, zoomlevel, custom_build_dir,
                (do_vector, do_imagery, do_overlays),
                min(slots, len(tiles)))
            # Registered BEFORE start so a cancel arriving during the
            # worker handshake window already routes to the run; start
            # runs off-thread because the handshake blocks for seconds.
            self._parallel = run
            self._eta = None

            def _start_parallel_or_fall_back():
                if run.start():
                    return
                self._parallel = None
                print("Parallel build workers could not be started; "
                      "building tiles one at a time in this process "
                      "instead.")
                self._prepare_in_process_eta(tiles, plan, provider,
                                             zoomlevel, custom_build_dir)
                self._build_worker(list(tiles), provider, zoomlevel,
                                   custom_build_dir, plan)

            threading.Thread(target=_start_parallel_or_fall_back,
                             daemon=True).start()
            return True
        self._prepare_in_process_eta(tiles, plan, provider, zoomlevel,
                                     custom_build_dir)
        threading.Thread(
            target=self._build_worker,
            args=(list(tiles), provider, zoomlevel, custom_build_dir, plan),
            daemon=True,
        ).start()
        return True

    def _prepare_in_process_eta(self, tiles, plan, provider, zoomlevel,
                                custom_build_dir):
        planned_keys = [k for (k, _b, _w) in plan]
        estimates = {
            t: _predict_step_seconds(
                t[0], t[1],
                prediction_features(
                    t[0], t[1], provider, zoomlevel, custom_build_dir),
                planned_keys)
            for t in tiles
        }
        self._eta = _EtaTracker(tiles, plan, estimates)
        self._eta_last_emit = 0.0

    def cancel(self):
        self._cancel_all = True
        parallel_run = self._parallel
        if parallel_run is not None:
            parallel_run.cancel_all()
            return
        UI.red_flag = True

    def shutdown(self):
        """Front-end exit: stop everything promptly, without blocking.

        :meth:`cancel` is the in-run graceful stop (a parallel run's
        children finish their current step before retiring); ``shutdown``
        is the application going away — worker subprocesses are also
        retired and terminated NOW, so none outlives the front end (each
        child turns the end-of-file/terminate into its own red-flagged,
        bounded wind-down).
        """
        self.cancel()
        parallel_run = self._parallel
        if parallel_run is not None:
            parallel_run.shutdown_workers()

    def cancel_tile(self, lat, lon):
        """Cancel one tile of the current run (spec §3.4).

        A queued tile never starts; an active tile stops after its current
        step while every other tile continues.  Returns True when the tile
        was part of the run and the cancellation was accepted.
        """
        tile = (int(lat), int(lon))
        parallel_run = self._parallel
        if parallel_run is not None:
            return parallel_run.cancel_tile(tile)
        if not self._building:
            return False
        self._cancelled_tiles.add(tile)
        if self._active_tile == tile:
            UI.red_flag = True
        return True

    def _run_finished(self):
        """Bookkeeping shared by both run modes when a run ends."""
        self._building = False
        self._current_step = None
        self._active_tile = None
        self._eta = None
        self._parallel = None

    def _build_worker(self, todo, provider, zoomlevel, custom_build_dir,
                      plan):
        # Heavy pipeline imports stay off the caller's startup path.
        import O4_Config_Utils as CFG
        import O4_Vector_Map as VMAP
        import O4_Mesh_Utils as MESH
        import O4_Mask_Utils as MASK
        import O4_Tile_Utils as TILE
        import O4_Overlay_Utils as OVL

        step_fn = {
            "vector": VMAP.build_poly_file,
            "mesh": MESH.build_mesh,
            "masks": MASK.build_masks,
            "imagery": TILE.build_tile,
            "overlays": lambda t: OVL.build_overlay(t.lat, t.lon),
        }
        done = errors = 0
        for (lat, lon) in todo:
            if UI.red_flag:
                break
            if (lat, lon) in self._cancelled_tiles:
                # Cancelled while queued: never started, reported stopped.
                self._emit(TileState(lat=lat, lon=lon, state="queued",
                                     label="stopped"))
                continue
            self._active_tile = (lat, lon)
            step_seconds = {}
            autopatch_airports = 0
            try:
                tile = CFG.Tile(lat, lon, custom_build_dir)
                tile.read_from_config()
                # The build command's imagery selection is explicit user
                # intent and must win over whatever default_website the
                # tile config recorded last time: stale configs (empty or
                # different provider) otherwise build silently with the
                # wrong — or no — imagery source.
                if provider:
                    if (tile.default_website
                            and tile.default_website != provider):
                        UI.vprint(
                            1,
                            "Tile config for",
                            FNAMES.short_latlon(lat, lon),
                            "recorded imagery source",
                            tile.default_website,
                            "— building with the selected",
                            provider,
                            "instead.")
                    tile.default_website = provider
                if zoomlevel:
                    tile.default_zl = zoomlevel
                UI.reset_total_elapsed()
                if any(k != "overlays" for k, _, _ in plan):
                    tile.make_dirs()
                failed = False
                # Percent windows proportional to this tile's predicted
                # step seconds (static weights are only the fallback).
                tile_plan = plan
                if self._eta is not None:
                    estimates = self._eta.estimates.get((lat, lon))
                    if estimates:
                        tile_plan = reweight_plan_by_seconds(plan, estimates)
                for key, base, width in tile_plan:
                    if UI.red_flag:
                        break
                    self._start_step((lat, lon), key, base, width)
                    step_t0 = time.time()
                    result = step_fn[key](tile)
                    step_seconds[key] = time.time() - step_t0
                    if self._autopatch_state is not None:
                        autopatch_airports = self._autopatch_state["total"]
                    self._finish_step((lat, lon), key)
                    if result == 0 and UI.red_flag:
                        break
                    if result == 0:
                        failed = True
                if UI.red_flag:
                    self._emit(TileState(lat=lat, lon=lon, state="queued",
                                         label="stopped"))
                    if (not self._cancel_all
                            and (lat, lon) in self._cancelled_tiles):
                        # Only THIS tile was cancelled (spec §3.4): clear
                        # the flag and continue with the remaining queue.
                        self._active_tile = None
                        UI.red_flag = False
                        continue
                    break
                if failed:
                    errors += 1
                    self._emit(TileState(lat=lat, lon=lon, state="error",
                                         label="failed"))
                    self._emit(BuildDone(lat=lat, lon=lon, ok=False,
                                         error="a build step failed"))
                else:
                    done += 1
                    self._emit(TileState(lat=lat, lon=lon, state="done",
                                         percent=100.0))
                    self._emit(BuildDone(lat=lat, lon=lon, ok=True))
                    _record_tile_build(
                        lat, lon,
                        {"zoomlevel": zoomlevel, "provider": provider,
                         "airports": autopatch_airports,
                         "autopatch_seconds":
                             self._autopatch_elapsed_seconds(),
                         # Cold/warm cache state, measured by build_dsf
                         # (zero when the imagery step did not run).
                         "textures_total": int(getattr(
                             tile, "textures_total_last_build", 0) or 0),
                         "textures_missing": int(getattr(
                             tile, "textures_missing_last_build", 0) or 0)},
                        step_seconds)
            except Exception:
                import traceback
                traceback.print_exc()
                errors += 1
                self._emit(TileState(lat=lat, lon=lon, state="error",
                                     label="failed"))
                self._emit(BuildDone(lat=lat, lon=lon, ok=False,
                                     error="exception (see log)"))
        cancelled = bool(UI.red_flag)
        self._run_finished()
        self._emit(RunDone(done_count=done, error_count=errors,
                           cancelled=cancelled))

    # ------------------------------------------------------------------
    # Whole-tile percent/label state machine (moved from the Qt view)
    # ------------------------------------------------------------------
    def _start_step(self, tile, key, base, width):
        self._current_step = (tile, key, base, width)
        self._autopatch_state = None
        self._autopatch_t0 = None
        self._bar_values = {1: 0, 2: 0, 3: 0}
        if self._eta:
            self._eta.step_started(tile, key)
        indeterminate = step_progress(key, {}) is None
        self._emit(StepProgress(
            lat=tile[0], lon=tile[1], step_key=key,
            label=STEP_LABELS.get(key, key),
            percent=base * 100.0, indeterminate=indeterminate))
        self._emit_eta(force=True)

    def _finish_step(self, tile, key):
        if self._eta:
            self._eta.step_finished(tile, key)

    def _autopatch_elapsed_seconds(self):
        t0 = getattr(self, "_autopatch_t0", None)
        return (time.time() - t0) if t0 else 0.0

    def _emit_eta(self, force=False):
        if self._eta is None:
            return
        now = time.monotonic()
        if not force and now - self._eta_last_emit < ETA_EMIT_SECONDS:
            return
        self._eta_last_emit = now
        finished = sum(
            1 for t in self._eta.tiles
            if len(self._eta.finished_steps.get(t, set()))
            == len(self._eta.plan))
        self._emit(RunEta(
            elapsed_seconds=time.time() - self._eta.t0,
            remaining_seconds=self._eta.remaining(),
            done_tiles=finished,
            total_tiles=len(self._eta.tiles)))

    # -- legacy channel entry points (called via O4_UI_Utils from the
    #    build worker thread; see that module) -------------------------
    def legacy_progress(self, nbr, percentage):
        self._bar_values[nbr] = percentage
        if self._current_step is None:
            return
        if self._autopatch_running():
            # Auto-patch owns the display until its last airport finishes;
            # stray legacy-bar updates must not flicker the label back.
            self._emit_eta()
            return
        tile, key, base, width = self._current_step
        inside = step_progress(key, self._bar_values)
        if inside is None:
            self._emit_eta()
            return
        percent = min(100.0, (base + width * min(inside, 100) / 100.0) * 100)
        if self._eta:
            self._eta.percent_sample(min(inside, 100.0))
        self._emit(StepProgress(
            lat=tile[0], lon=tile[1], step_key=key,
            label=STEP_LABELS.get(key, key), percent=percent))
        self._emit_eta()

    def _autopatch_running(self):
        state = self._autopatch_state
        return (state is not None
                and len(state["finished"]) < state["total"])

    def autopatch_begin(self, airports):
        airports = list(airports)
        self._autopatch_state = {
            "total": max(len(airports), 1),
            "finished": set(),
            "frac": {},
            "current": "",
        }
        self._autopatch_t0 = time.time()
        if self._eta:
            self._eta.autopatch_begin(airports)
        tile = self._current_step[0] if self._current_step else (0, 0)
        self._emit(AutoPatchBegin(airports=tuple(airports),
                                  lat=tile[0], lon=tile[1]))
        self._render_autopatch()

    def autopatch_event(self, airport, done, total, label, status="run",
                        eta_total_seconds=None):
        state = self._autopatch_state
        if state is None:
            return
        fraction = min(float(done) / float(total), 1.0) if total else 0.0
        if status in ("done", "fail"):
            state["finished"].add(airport)
            state["frac"][airport] = 1.0
            if state["current"] == airport:
                state["current"] = ""
        else:
            state["frac"][airport] = fraction
            state["current"] = airport
        if self._eta:
            self._eta.autopatch_progress(airport, status, eta_total_seconds)
        tile = self._current_step[0] if self._current_step else (0, 0)
        self._emit(AutoPatchProgress(
            airport=str(airport), done=float(done), total=float(total),
            label=str(label), status=str(status),
            eta_total_seconds=eta_total_seconds,
            lat=tile[0], lon=tile[1]))
        self._render_autopatch()

    def _render_autopatch(self):
        if self._current_step is None:
            return
        state = self._autopatch_state
        if state is None:
            return
        tile, key, base, width = self._current_step
        overall = sum(state["frac"].values()) / state["total"]
        percent = min(100.0, (base + width * min(overall, 1.0)) * 100)
        label = "auto-patch %d/%d" % (len(state["finished"]), state["total"])
        if state["current"]:
            label += " · " + state["current"]
        self._emit(StepProgress(
            lat=tile[0], lon=tile[1], step_key=key,
            label=label, percent=percent))
        self._emit_eta()

    # ------------------------------------------------------------------
    # Thin command wrappers (single source: the underlying modules)
    # ------------------------------------------------------------------
    def tile_info(self, lat, lon, working_dir):
        import O4_Tile_Info as TINFO
        return TINFO.tile_info(lat, lon, working_dir)

    def config_describe(self):
        import O4_Cfg_Vars as CFG_VARS
        return CFG_VARS.cfg_vars

    def links_status(self, lat, lon, build_dir, scenery_dir):
        import O4_Scenery_Links as LINKS
        return LINKS.link_status(lat, lon, build_dir, scenery_dir)

    def links_install(self, lat, lon, build_dir, scenery_dir):
        import O4_Scenery_Links as LINKS
        return LINKS.install(lat, lon, build_dir, scenery_dir)

    def links_uninstall(self, lat, lon, build_dir, scenery_dir):
        import O4_Scenery_Links as LINKS
        return LINKS.uninstall(lat, lon, build_dir, scenery_dir)
