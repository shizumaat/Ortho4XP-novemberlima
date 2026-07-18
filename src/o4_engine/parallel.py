"""Parallel tile builds: the phase-aware subprocess orchestrator
(docs/specs/parallel-tile-builds.md).

One :class:`ParallelBuildRun` drives a run in which up to N tiles build
concurrently, each inside its own worker child process running the
existing JSON-lines engine transport (``Ortho4XP.py --engine-jsonl``).

The parent dispatches ONE STEP AT A TIME (spec §3.8): every ``build``
command a child receives selects a single step key, so the parent knows
and controls each tile's phase.  Steps belong to resource classes; the
two NETWORK classes ("osm" for vector, "imagery") carry concurrency
caps so a forty-tile queue can never stampede the OpenStreetMap or
imagery servers, and the memory admission gate keeps concurrent mesh
steps within the machine's budget.  Compute concurrency is otherwise
uncapped — processor arbitration is the operating system's job
(2026-07-17 owner ruling).  A child whose next step's class is full
waits idle (a between-steps child holds almost nothing — the pipeline
communicates through files).  When capacity frees, blocked children
are dispatched finish-first (later steps before earlier ones, work in
progress drains before new tiles enter).

Children are reused across steps and tiles (one interpreter start-up
per slot), except after a mid-step cancel, where the child is retired
and a fresh one spawned so a red-flagged interpreter never carries
state forward.  The child's per-step ``BuildDone`` / ``TileState(done)``
/ ``RunDone`` events are consumed as scheduling signals; the parent
forwards tile-level terminals only when a tile's LAST step completes,
and remaps ``StepProgress`` percent from the child's single-step window
into the tile's full-plan window, so views see exactly the stream a
whole-tile build produced.

No GUI-toolkit imports (core-module rule).  The session owns run
lifecycle bookkeeping; this module reports back through
``session._emit`` and ``session._run_finished``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Optional

from . import events as EVENTS
from . import tile_time_model
from .events import BuildDone, RunDone, RunEta, StepProgress, TileState
from .session import (
    _predict_step_seconds, plan_steps, prediction_features,
    reweight_plan_by_seconds,
)


def estimate_remaining_wall_seconds(estimates, programs, queued_tiles,
                                    next_step_index, in_flight_steps,
                                    now, slots):
    """Advisory wall-clock remaining estimate for a parallel run.

    Work model: every queued tile contributes its full predicted plan;
    every active tile contributes its remaining steps, the in-flight
    step credited for its elapsed time (degrading into
    overrun-proportional remaining once it outlives its prediction —
    see :func:`tile_time_model.remaining_step_seconds`).  The total
    work is divided by the effective parallelism (``slots`` capped by
    the tiles still holding work) — coarse (class limits and the
    memory gate are not modelled) but a defensible estimate where
    there was previously an honest dash.  Returns ``None`` when no
    work remains.

    ``estimates``: ``{tile: {step: seconds}}``; ``programs``:
    ``{tile: ordered step keys}`` (batches enqueued into a live run may
    select different steps); ``next_step_index``:
    ``{tile: index of the running-or-next step}``; ``in_flight_steps``:
    ``{(tile, step): started_at}``.
    """
    total_work = 0.0
    tiles_with_work = 0

    def step_estimate(tile, key):
        value = (estimates.get(tile) or {}).get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
        return None

    for tile in queued_tiles:
        tiles_with_work += 1
        for key in programs.get(tile, ()):
            estimate = step_estimate(tile, key)
            if estimate is not None:
                total_work += estimate
    for tile, index in next_step_index.items():
        tiles_with_work += 1
        for position, key in enumerate(programs.get(tile, ())):
            if position < index:
                continue
            estimate = step_estimate(tile, key)
            started_at = in_flight_steps.get((tile, key))
            if started_at is not None:
                estimate = tile_time_model.remaining_step_seconds(
                    estimate, now - started_at)
            elif estimate is None:
                continue
            total_work += estimate
    if not tiles_with_work:
        return None
    parallelism = max(1, min(int(slots), tiles_with_work))
    return total_work / parallelism

# Seconds to wait for a freshly spawned worker's EngineHello line before
# declaring the spawn failed (interpreter start + light imports only —
# the heavy pipeline imports happen later, inside the build command).
HANDSHAKE_TIMEOUT_SECONDS = 30.0

# Resource classes (spec §3.8): which shared resource each step leans
# on, and how many tiles may occupy a class at once.  The two network
# classes are SEPARATE because they exhaust separate servers — a tile
# downloading OpenStreetMap data must not steal budget from the imagery
# phase, which dominates total build time (the goal is minimal makespan
# for the whole queue).  Compute steps are UNCAPPED (the class exists
# only for bookkeeping): the 2026-07-17 owner ruling leaves processor
# arbitration to the operating system — the caps that remain guard
# REMOTE SERVERS, plus the mesh memory admission gate below for the
# one machine cliff the operating system handles badly.
STEP_CLASSES = {
    "vector": "osm",
    "imagery": "imagery",
    "mesh": "compute",
    "masks": "compute",
    "overlays": "compute",
}
OSM_CLASS_LIMIT = 2
IMAGERY_CLASS_LIMIT = 2


def class_limits(slots):
    """Per-class concurrency caps for a run of ``slots`` children."""
    return {
        "osm": min(OSM_CLASS_LIMIT, slots),
        "imagery": min(IMAGERY_CLASS_LIMIT, slots),
        "compute": slots,
    }


# Rough peak mesh-step memory per tile (gigabytes) by elevation detail
# level (docs/specs/elevation-level-spec.md sizing: working raster plus
# smoothing/bake copies).  Drives the mesh admission gate below, so one
# 1 m island tile in a forty-tile queue is scheduled around, not
# alongside, other big rasters.
MESH_MEMORY_ESTIMATES_GB = {
    "auto": 2.0,
    "coastline": 3.0,
    "30": 1.5,
    "10": 3.0,
    "5": 8.0,
    "1": 18.0,
}
MESH_MEMORY_DEFAULT_GB = 2.0
# Gigabytes kept free for the operating system, the application, and the
# non-mesh phases of concurrently building tiles.
MESH_MEMORY_HEADROOM_GB = 4.0


def mesh_memory_estimate_gigabytes(elevation_level_value):
    """Estimated peak mesh memory for a tile's ``elevation_level``."""
    if elevation_level_value is None:
        return MESH_MEMORY_DEFAULT_GB
    key = str(elevation_level_value).strip().lower()
    return MESH_MEMORY_ESTIMATES_GB.get(key, MESH_MEMORY_DEFAULT_GB)


def mesh_memory_budget_gigabytes():
    """Total gigabytes the run may commit to concurrent mesh steps."""
    import O4_Parallel_Utils as PARALLEL_UTILS

    return max(
        4.0,
        PARALLEL_UTILS.machine_memory_gigabytes() - MESH_MEMORY_HEADROOM_GB,
    )


# Event types forwarded verbatim (modulo remapping/suppression, spec
# §3.8) from a child stream into the parent session.  Everything else is
# child-run-level (hello, run clock, run end) or impossible from a
# worker (scan events) and is suppressed; the parent emits its own.
_FORWARDED_EVENT_TYPES = (
    "TileState",
    "StepProgress",
    "BuildDone",
    "AutoPatchBegin",
    "AutoPatchProgress",
    "Log",
)

_EVENT_CLASSES = {
    name: value
    for name, value in vars(EVENTS).items()
    if isinstance(value, type)
    and issubclass(value, EVENTS.EngineEvent)
    and value is not EVENTS.EngineEvent
}


def tile_worker_command():
    """The argv launching one worker child.

    From source: ``[python, <repo>/Ortho4XP.py, --engine-jsonl]``.  In
    the frozen application the executable serves as its own worker via
    the same early argv branch in ``Ortho4XP_Qt.py``.  Module-level so
    tests can substitute a stub worker script.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--engine-jsonl"]
    repository_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return [
        sys.executable,
        os.path.join(repository_root, "Ortho4XP.py"),
        "--engine-jsonl",
    ]


def _short_latlon(tile):
    lat, lon = tile
    return "%+03d%+04d" % (lat, lon)


def _rebuild_event(payload):
    """Parse one child event line back into a typed event, or ``None``.

    Additive-protocol rules: unknown event types and unknown fields are
    dropped silently.  ``seq``/``ts`` are discarded — the parent session
    re-stamps both at re-emission.
    """
    event_name = payload.get("event")
    event_class = _EVENT_CLASSES.get(event_name)
    if event_class is None:
        return None
    field_names = {
        f.name for f in dataclasses.fields(event_class)
    } - {"seq", "ts"}
    kwargs = {k: v for k, v in payload.items() if k in field_names}
    # JSON turns tuples into lists; the tuple-typed forwarded field is
    # normalized back so in-process and merged streams look alike.
    if "airports" in kwargs and isinstance(kwargs["airports"], list):
        kwargs["airports"] = tuple(
            tuple(a) if isinstance(a, list) else a
            for a in kwargs["airports"]
        )
    try:
        return event_class(**kwargs)
    except Exception:
        return None


class _WorkerChild:
    """One worker subprocess: process handle, stream threads, state."""

    def __init__(self, run, index):
        self.run = run
        self.index = index
        self.process: Optional[subprocess.Popen] = None
        self.tile = None               # tile this child is carrying
        self.running_step = None       # step key in flight, None = waiting
        self.step_failed = False       # current tile had a failing step
        self.cancelling = False        # a mid-step cancel is in flight
        self.retired = False
        self.hello = threading.Event()
        self._stdin_lock = threading.Lock()
        self._command_id = 0

    # -- lifecycle -----------------------------------------------------
    def spawn(self):
        """Start the process and its stream threads; await the handshake.

        Returns True when the child produced its EngineHello in time.
        """
        try:
            # Children learn their sibling count so their own Auto slot
            # resolutions (DDS conversion, downloads) share the machine.
            child_environment = dict(os.environ)
            child_environment["O4_PARALLEL_BUILD_SIBLINGS"] = str(
                self.run._slots)
            # The child's parent-death watchdog probes this pid directly
            # (jsonl owns_process mode), so a front end that dies without
            # collapsing the stdin pipe still takes its workers with it.
            child_environment["O4_PARENT_PROCESS_ID"] = str(os.getpid())
            self.process = subprocess.Popen(
                tile_worker_command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=child_environment,
                cwd=os.path.dirname(
                    os.path.dirname(
                        os.path.dirname(os.path.abspath(__file__)))),
            )
        except Exception as error:
            print("Could not start a build worker process:", error)
            return False
        threading.Thread(target=self._read_events, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        if not self.hello.wait(HANDSHAKE_TIMEOUT_SECONDS):
            print("Build worker", self.index,
                  "did not answer in time; giving up on it.")
            self.terminate()
            return False
        return True

    def send(self, command):
        try:
            with self._stdin_lock:
                self._command_id += 1
                command = dict(command, id=self._command_id)
                self.process.stdin.write(json.dumps(command) + "\n")
                self.process.stdin.flush()
            return True
        except Exception:
            return False

    def start_step(self, step_key, build_arguments):
        """Send one single-step build command for this child's tile.

        ``slots=1`` is explicit: a worker child must run its step
        in-process, never orchestrate worker grandchildren of its own,
        whatever its own configuration would resolve to.
        """
        self.running_step = step_key
        return self.send(dict(
            build_arguments,
            cmd="build",
            tiles=[[self.tile[0], self.tile[1]]],
            steps=[step_key],
            slots=1,
        ))

    def retire(self):
        """Close the child down gracefully (EOF ends jsonl.serve)."""
        self.retired = True
        try:
            self.process.stdin.close()
        except Exception:
            pass

    def terminate(self):
        self.retired = True
        try:
            self.process.terminate()
        except Exception:
            pass

    # -- stream threads --------------------------------------------------
    def _read_events(self):
        try:
            for line in self.process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if payload.get("event") == "EngineHello":
                    self.hello.set()
                    continue
                if "event" in payload:
                    self.run._on_child_event(self, payload)
        except Exception:
            pass
        self.run._on_child_exit(self)

    def _drain_stderr(self):
        """Pipeline prints and crash text, re-printed with attribution so
        the interleaved console stays readable."""
        try:
            for line in self.process.stderr:
                tile = self.tile
                prefix = ("[%s] " % _short_latlon(tile)) if tile else (
                    "[worker %d] " % self.index)
                print(prefix + line.rstrip("\n"))
        except Exception:
            pass


class ParallelBuildRun:
    """The phase-aware orchestrator for one parallel run (spec §3.1–3.8)."""

    def __init__(self, session, tiles, provider, zoomlevel,
                 custom_build_dir, step_flags, slots):
        self._session = session
        self._queue = deque()
        self._total = 0
        self._slots = slots
        # Per-tile build state: a batch enqueued into a LIVE run may
        # carry different build arguments (imagery source, zoom level,
        # output folder, step selection) than the batch that started it,
        # so everything the dispatcher needs is keyed by tile.
        self._tile_arguments: dict = {}   # tile -> child build kwargs
        self._programs: dict = {}         # tile -> ordered step keys
        self._static_windows: dict = {}   # tile -> {step: (base, width)}
        # Learned per-tile step estimates (tile_time_model): drive the
        # run clock (spec §3.3 upgraded from the honest dash) and
        # re-scale each tile's percent windows to predicted seconds.
        # Purely advisory — failure keeps the static windows and the
        # dash.
        self._estimates: dict = {}
        self._tile_step_windows: dict = {}
        self._step_started_at: dict = {}      # (tile, step) -> monotonic t
        self._class_limits = class_limits(slots)
        self._class_active = {name: 0 for name in self._class_limits}
        self._next_step_index: dict = {}
        # Children spawn believing `slots` siblings share the machine;
        # broadcasts below shrink that as tiles finish (see
        # _broadcast_sibling_count).
        self._sibling_broadcast = slots
        # Memory-aware mesh admission (spec §3.8): per-tile estimates
        # from each tile's configured elevation detail level, admitted
        # against the machine's budget (at least one mesh always runs).
        self._mesh_memory_estimates: dict = {}
        self._mesh_memory_budget = mesh_memory_budget_gigabytes()
        self._mesh_memory_in_use = 0.0
        self._meshing_tiles: set = set()
        self._lock = threading.Lock()
        self._children: list = []
        self._next_child_index = 0
        self._done = 0
        self._errors = 0
        self._cancel_all = False
        self._finished = False
        self._t0 = time.time()
        # OpenStreetMap cache warmer state (spec §3.7): the tile whose
        # caches are being downloaded right now (assignment waits for
        # it), the tiles already warmed, and whether the warmer thread
        # is alive (an enqueue may need to revive it).
        self._warming_tile = None
        self._warmed_tiles: set = set()
        self._warmer_running = False
        # Per-tile progress high-water marks: the legacy in-step bars
        # oscillate within a step (they refill per OpenStreetMap layer,
        # per download phase, ...), which the historic single-bar view
        # hid but a per-tile bar shows as jumping.  The forwarder
        # ratchets: a tile's displayed percent only ever advances.
        self._percent_high_water: dict = {}
        with self._lock:
            self._admit_batch_locked(tiles, provider, zoomlevel,
                                     custom_build_dir, step_flags)

    def _admit_batch_locked(self, tiles, provider, zoomlevel,
                            custom_build_dir, step_flags):
        """Register a batch of tiles with the run (caller holds the lock).

        Tiles already part of the run (queued or on a child) are skipped
        — re-queueing a tile that already FINISHED is allowed and treats
        it as new work.  Returns the tiles actually admitted, already
        appended to the queue.
        """
        do_vector, do_imagery, do_overlays = step_flags
        full_plan = plan_steps(do_vector, do_imagery, do_overlays)
        program = [key for (key, _base, _width) in full_plan]
        if not program:
            return []
        admitted = []
        for tile in tiles:
            if (tile in self._queue or tile in self._next_step_index
                    or any(child.tile == tile
                           for child in self._children)):
                continue
            admitted.append(tile)
            self._tile_arguments[tile] = {
                "provider": provider,
                "zoomlevel": zoomlevel,
                "custom_build_dir": custom_build_dir,
            }
            self._programs[tile] = list(program)
            self._static_windows[tile] = {
                key: (base, width) for (key, base, width) in full_plan
            }
            try:
                features = prediction_features(
                    tile[0], tile[1], provider, zoomlevel,
                    custom_build_dir)
                estimates = _predict_step_seconds(
                    tile[0], tile[1], features, program)
                self._estimates[tile] = estimates
                self._tile_step_windows[tile] = {
                    key: (base, width)
                    for (key, base, width) in reweight_plan_by_seconds(
                        full_plan, estimates)
                }
            except Exception:
                self._estimates.pop(tile, None)
                self._tile_step_windows.pop(tile, None)
            self._mesh_memory_estimates[tile] = (
                self._estimate_mesh_memory(tile, custom_build_dir))
            self._percent_high_water.pop(tile, None)
            self._queue.append(tile)
        self._total += len(admitted)
        return admitted

    # -- lifecycle -----------------------------------------------------
    def start(self):
        """Spawn the slot pool and dispatch the first steps.

        The FIRST child is the canary: if it cannot be spawned, the whole
        parallel mode is declared unavailable and the session falls back
        to the in-process worker (spec §3.2).  Later spawn failures just
        shrink the pool.
        """
        # Spawn one child per tile up to the slot cap — never more
        # children than there is work (the cap itself may exceed the
        # starting batch: enqueue grows the pool later).
        with self._lock:
            wanted_children = min(self._slots, len(self._queue))
        if not wanted_children:
            return False
        first = self._spawn_child()
        if first is None:
            return False
        for _ in range(wanted_children - 1):
            if not self._queue:
                # A cancel during the handshake window drained the queue.
                break
            if self._spawn_child() is None:
                print("Fewer build workers than requested could be "
                      "started; continuing with", len(self._children))
                break
        with self._lock:
            self._dispatch_locked()
            self._warmer_running = True
        threading.Thread(target=self._osm_cache_warmer, daemon=True).start()
        threading.Thread(target=self._ticker, daemon=True).start()
        # Children spawned believing `slots` siblings share the machine;
        # tell them the real count right away (a two-tile run on four
        # slots must not throttle itself for two ghosts).
        self._broadcast_sibling_count()
        # A cancel that landed during the handshake window may have
        # drained the queue already; settle immediately in that case.
        self._maybe_finish()
        return True

    def enqueue(self, tiles, provider, zoomlevel, custom_build_dir,
                step_flags):
        """Append a batch of tiles to the LIVE run.

        The batch keeps its own build arguments and step selection —
        per-tile state throughout the dispatcher makes mixed batches
        first-class.  Idle children pick the new tiles up immediately;
        if the pool has shrunk (crashed or retired-after-cancel
        children), replacements are spawned off-thread up to the slot
        count.  Returns the number of tiles actually admitted: 0 when
        the run is finishing or cancelled (the caller should start a
        fresh run instead) or when every tile is already part of it.
        """
        with self._lock:
            if self._finished or self._cancel_all:
                return 0
            admitted = self._admit_batch_locked(
                tiles, provider, zoomlevel, custom_build_dir, step_flags)
            if not admitted:
                return 0
            self._dispatch_locked()
            alive = [child for child in self._children
                     if not child.retired]
            missing = max(0, min(self._slots - len(alive),
                                 len(self._queue)))
            revive_warmer = not self._warmer_running
            if revive_warmer:
                self._warmer_running = True

        def _grow_pool_and_dispatch():
            for _ in range(missing):
                if self._spawn_child() is None:
                    break
            with self._lock:
                self._dispatch_locked()
            self._broadcast_sibling_count()

        # Spawning blocks on the worker handshake — never on the
        # caller's (GUI) thread.
        threading.Thread(target=_grow_pool_and_dispatch,
                         daemon=True).start()
        if revive_warmer:
            threading.Thread(target=self._osm_cache_warmer,
                             daemon=True).start()
        self._broadcast_sibling_count()
        return len(admitted)

    def _spawn_child(self):
        """Spawn one worker (blocks on the handshake; never called while
        holding the scheduler lock — a slow spawn must not block
        cancellation or event routing)."""
        with self._lock:
            child = _WorkerChild(self, self._next_child_index)
            self._next_child_index += 1
        if not child.spawn():
            return None
        with self._lock:
            self._children.append(child)
        return child

    # -- the dispatcher (spec §3.8) --------------------------------------
    def _dispatch_locked(self):
        """Start every step that capacity allows (caller holds the lock).

        Blocked children first, finish-first (later steps before earlier
        ones), then idle children pick up new tiles from the queue.
        """
        if self._finished or self._cancel_all:
            return
        blocked = sorted(
            (child for child in self._children
             if child.tile is not None and child.running_step is None
             and not child.retired and not child.cancelling),
            key=lambda child: -self._next_step_index.get(child.tile, 0),
        )
        for child in blocked:
            self._try_start_step_locked(child)
        for child in list(self._children):
            if (child.tile is None and not child.retired
                    and not child.cancelling):
                self._start_new_tile_locked(child)

    @staticmethod
    def _estimate_mesh_memory(tile, custom_build_dir):
        """One tile's mesh memory estimate from its configuration.

        Reads the tile's ``elevation_level`` (per-tile config file, or
        the default when absent); any failure degrades to the default
        estimate — scheduling must never fail on a config hiccup.
        """
        level_value = None
        try:
            import O4_Settings_Model as SETTINGS_MODEL

            raw = SETTINGS_MODEL.read_tile_raw(
                tile[0], tile[1], custom_build_dir)
            level_value = (raw or {}).get("elevation_level")
            if level_value is None:
                # Sparse tile configs (blended model) omit inherited
                # settings: the tile builds with the GLOBAL value.
                level_value = SETTINGS_MODEL.global_effective_value(
                    "elevation_level")
        except Exception:
            level_value = None
        return mesh_memory_estimate_gigabytes(level_value)

    def _mesh_memory_admits_locked(self, tile):
        """True when the tile's mesh step fits the memory budget now.

        One mesh is always admitted (a single tile must never deadlock,
        however big its raster); beyond that, the sum of running
        estimates stays within the budget.
        """
        if not self._meshing_tiles:
            return True
        estimate = self._mesh_memory_estimates.get(
            tile, MESH_MEMORY_DEFAULT_GB)
        return (self._mesh_memory_in_use + estimate
                <= self._mesh_memory_budget)

    def _try_start_step_locked(self, child):
        """Dispatch the child's tile's next step if its class has room
        (and, for mesh steps, the memory budget admits it)."""
        step_index = self._next_step_index.get(child.tile, 0)
        step_key = self._programs[child.tile][step_index]
        step_class = STEP_CLASSES.get(step_key, "compute")
        if self._class_active[step_class] >= self._class_limits[step_class]:
            return False
        if step_key == "mesh" and not self._mesh_memory_admits_locked(
                child.tile):
            return False
        if child.start_step(step_key, self._tile_arguments[child.tile]):
            self._step_started_at[(child.tile, step_key)] = time.time()
            self._class_active[step_class] += 1
            if step_key == "mesh":
                self._meshing_tiles.add(child.tile)
                self._mesh_memory_in_use += self._mesh_memory_estimates.get(
                    child.tile, MESH_MEMORY_DEFAULT_GB)
            return True
        return False

    def _release_step_resources_locked(self, child):
        """Return a finished/aborted step's class and memory budget."""
        step_key = child.running_step
        if step_key is None:
            return
        self._step_started_at.pop((child.tile, step_key), None)
        step_class = STEP_CLASSES.get(step_key, "compute")
        self._class_active[step_class] = max(
            0, self._class_active[step_class] - 1)
        if step_key == "mesh" and child.tile in self._meshing_tiles:
            self._meshing_tiles.discard(child.tile)
            self._mesh_memory_in_use = max(
                0.0,
                self._mesh_memory_in_use - self._mesh_memory_estimates.get(
                    child.tile, MESH_MEMORY_DEFAULT_GB))
        child.running_step = None

    def _start_new_tile_locked(self, child):
        """Hand an idle child a queued tile whose first step has room.

        A tile whose OpenStreetMap caches are being warmed RIGHT NOW is
        skipped (never assigned mid-warm — the no-race guarantee of spec
        §3.7); the warmer re-dispatches the moment it finishes a tile.
        A tile whose FIRST step's class is full is also skipped in favour
        of a later queued tile whose first step has room (mixed-batch
        programs may lead with different classes).
        """
        tile = None
        for candidate in self._queue:
            if candidate == self._warming_tile:
                continue
            first_class = STEP_CLASSES.get(
                self._programs[candidate][0], "compute")
            if (self._class_active[first_class]
                    >= self._class_limits[first_class]):
                continue
            tile = candidate
            break
        if tile is None:
            return False
        self._queue.remove(tile)
        child.tile = tile
        child.step_failed = False
        self._next_step_index[tile] = 0
        if not self._try_start_step_locked(child):
            # Dead pipe: put the tile back for another child; the exit
            # path cleans the child up.
            child.tile = None
            self._queue.appendleft(tile)
            return False
        return True

    # -- cancellation ----------------------------------------------------
    def cancel_tile(self, tile):
        emit_stopped = False
        with self._lock:
            if tile in self._queue:
                self._queue.remove(tile)
                emit_stopped = True
                accepted = True
            else:
                accepted = False
                for child in self._children:
                    if child.tile != tile or child.retired:
                        continue
                    if child.running_step is None:
                        # Between steps: no child cancel needed at all —
                        # stop dispatching and report stopped (spec §3.8);
                        # the child stays clean and reusable.
                        child.tile = None
                        self._next_step_index.pop(tile, None)
                        self._percent_high_water.pop(tile, None)
                        emit_stopped = True
                        accepted = True
                        self._dispatch_locked()
                    else:
                        child.cancelling = True
                        accepted = child.send({"cmd": "cancel"})
                    break
        if emit_stopped:
            self._session._emit(TileState(lat=tile[0], lon=tile[1],
                                          state="queued", label="stopped"))
            self._maybe_finish()
        return accepted

    def cancel_all(self):
        with self._lock:
            self._cancel_all = True
            drained = list(self._queue)
            self._queue.clear()
            waiting = []
            for child in self._children:
                if child.retired or child.tile is None:
                    continue
                if child.running_step is None:
                    waiting.append(child.tile)
                    child.tile = None
                else:
                    child.cancelling = True
                    child.send({"cmd": "cancel"})
        for tile in drained + waiting:
            self._session._emit(TileState(lat=tile[0], lon=tile[1],
                                          state="queued", label="stopped"))
        self._maybe_finish()

    def shutdown_workers(self):
        """Hard-stop every worker child because the front end is exiting.

        ``cancel_all`` is the graceful in-run stop (children finish their
        current step before retiring); this is the application going away
        NOW.  Each child gets its stdin closed (end-of-file) AND a
        terminate signal — its transport turns both into a red-flagged,
        bounded wind-down (jsonl.serve owns_process mode) — so no worker
        can outlive the front end and keep building headless.  Never
        blocks the caller.
        """
        with self._lock:
            self._cancel_all = True
            self._queue.clear()
            children = list(self._children)
        for child in children:
            child.retire()
            child.terminate()

    # -- child callbacks (reader threads) --------------------------------
    def _on_child_event(self, child, payload):
        event_name = payload.get("event")
        if event_name == "RunDone":
            self._child_step_done(child)
            return
        if event_name == "Error" and payload.get("fatal"):
            # A fatal child error ends that CHILD, never the parent
            # session; the crash path in _on_child_exit accounts for it.
            print("[worker %d] fatal: %s"
                  % (child.index, payload.get("text", "")))
            return
        if event_name not in _FORWARDED_EVENT_TYPES:
            return
        event = _rebuild_event(payload)
        if event is None:
            return
        tile = child.tile
        step_index = self._next_step_index.get(tile, 0)
        program = self._programs.get(tile, ())
        last_step = step_index >= len(program) - 1
        if event_name == "BuildDone":
            if event.ok and not last_step:
                # Intermediate step completion: a scheduling signal, not
                # a tile terminal — swallowed (spec §3.8).
                return
            with self._lock:
                if event.ok:
                    self._done += 1
                else:
                    child.step_failed = True
                    self._errors += 1
        elif event_name == "TileState":
            if event.state == "done" and not last_step:
                return
        elif event_name == "StepProgress" and tile is not None:
            # Remap the child's single-step percent window into the
            # tile's full-plan window so views see whole-tile percent,
            # then ratchet against the tile's high-water mark: the
            # legacy bars oscillate within a step, and a per-tile bar
            # must fill smoothly, never slide back (each tile has one
            # child, so the per-key update is single-writer).
            windows = (self._tile_step_windows.get(tile)
                       or self._static_windows.get(tile) or {})
            base, width = windows.get(event.step_key, (0.0, 1.0))
            remapped = min(
                100.0, (base + width * event.percent / 100.0) * 100.0)
            remapped = max(
                remapped, self._percent_high_water.get(tile, 0.0))
            self._percent_high_water[tile] = remapped
            event = dataclasses.replace(event, percent=remapped)
        if event_name in ("AutoPatchBegin", "AutoPatchProgress"):
            if tile is not None and (event.lat, event.lon) == (0, 0):
                event = dataclasses.replace(event, lat=tile[0], lon=tile[1])
        self._session._emit(event)

    def _child_step_done(self, child):
        """The child finished (or aborted) one single-step build."""
        respawn = False
        with self._lock:
            self._release_step_resources_locked(child)
            if child.cancelling:
                # Spec §3.4: never reuse a red-flagged interpreter.
                self._next_step_index.pop(child.tile, None)
                self._percent_high_water.pop(child.tile, None)
                child.tile = None
                child.retire()
                respawn = bool(self._queue) and not self._cancel_all
            elif child.step_failed:
                # The failure was forwarded at BuildDone time; abort the
                # tile's remaining steps.
                self._next_step_index.pop(child.tile, None)
                self._percent_high_water.pop(child.tile, None)
                child.tile = None
                child.step_failed = False
            elif child.tile is not None:
                self._next_step_index[child.tile] = (
                    self._next_step_index.get(child.tile, 0) + 1)
                if (self._next_step_index[child.tile]
                        >= len(self._programs.get(child.tile, ()))):
                    # Tile complete (its final BuildDone was forwarded).
                    self._next_step_index.pop(child.tile, None)
                    self._percent_high_water.pop(child.tile, None)
                    child.tile = None
            self._dispatch_locked()
        if respawn:
            replacement = self._spawn_child()
            if replacement is not None:
                with self._lock:
                    self._dispatch_locked()
        self._broadcast_sibling_count()
        self._maybe_finish()

    def _broadcast_sibling_count(self):
        """Tell surviving children how many siblings still hold work.

        Since the 2026-07-17 lean-on-the-operating-system ruling,
        processor-bound pools no longer divide by this count — its one
        remaining consumer is the network fetchers that hit small
        remote hosts (the bathymetry cell fetch), which re-read it from
        the environment (set_parallel_siblings) at their next step.

        The count is the children actively HOLDING tiles (measured
        after dispatch, so a freed slot the queue instantly refills
        never reads as spare capacity).  Queued tiles do not count —
        they consume nothing until a child picks them up; counting them
        (the pre-2026-07-17 formula) told every child in a deep-queue
        run that the whole queue was concurrent.
        """
        with self._lock:
            holders = [child for child in self._children
                       if child.tile is not None and not child.retired]
            count = max(1, len(holders))
            if count == self._sibling_broadcast:
                return
            self._sibling_broadcast = count
        for child in holders:
            child.send({"cmd": "siblings", "count": count})

    def _on_child_exit(self, child):
        """The child's stdout closed: normal retirement or a crash."""
        crashed_tile = None
        respawn = False
        with self._lock:
            if child in self._children:
                self._children.remove(child)
            self._release_step_resources_locked(child)
            if child.tile is not None and not child.retired:
                crashed_tile = child.tile
                self._next_step_index.pop(child.tile, None)
                self._percent_high_water.pop(child.tile, None)
                child.tile = None
                self._errors += 1
                respawn = bool(self._queue) and not self._cancel_all
            self._dispatch_locked()
        if crashed_tile is not None:
            lat, lon = crashed_tile
            self._session._emit(TileState(lat=lat, lon=lon, state="error",
                                          label="failed"))
            self._session._emit(BuildDone(
                lat=lat, lon=lon, ok=False,
                error="build worker exited unexpectedly"))
            print("Build worker for", _short_latlon(crashed_tile),
                  "exited unexpectedly; its tile is marked failed.")
        if respawn:
            replacement = self._spawn_child()
            if replacement is not None:
                with self._lock:
                    self._dispatch_locked()
        self._broadcast_sibling_count()
        self._maybe_finish()

    # -- OpenStreetMap cache warmer (spec §3.7) ---------------------------
    def _osm_cache_warmer(self):
        """Warmer thread body: run the warm loop, then mark the thread
        dead so a later enqueue knows to revive it (the loop returns
        the moment no unwarmed queued tiles remain)."""
        try:
            self._warm_queued_tiles()
        finally:
            with self._lock:
                self._warmer_running = False

    def _warm_queued_tiles(self):
        """Pre-download queued tiles' OpenStreetMap layer caches.

        One tile at a time, one Overpass request at a time — combined
        with the network class cap, the server sees a bounded, polite
        request profile however many tiles are queued.  Only UNASSIGNED
        tiles are touched, and assignment skips the tile being warmed
        (_start_new_tile_locked), so the warmer can never race a worker
        child on the same cache files.  Failures are per-tile and
        non-fatal: the child simply downloads for itself, exactly as
        without a warmer.

        ``O4_DISABLE_OSM_WARMER`` in the environment disables the warmer
        entirely (the automated test suite sets it globally so no test
        can reach the network; real runs never set it).
        """
        if os.environ.get("O4_DISABLE_OSM_WARMER"):
            return
        try:
            import O4_Config_Utils as CFG
            import O4_OSM_Utils as OSM
            import O4_Vector_Map as VMAP
        except Exception as error:
            print("OpenStreetMap cache warmer unavailable:", error)
            return
        try:
            import O4_OSM_Extracts as EXTRACTS
        except Exception:
            EXTRACTS = None
        while True:
            with self._lock:
                if self._finished or self._cancel_all:
                    return
                tile = next(
                    (t for t in self._queue if t not in self._warmed_tiles),
                    None,
                )
                if tile is None:
                    return
                self._warming_tile = tile
            # The warmer exists to spare the OVERPASS servers.  A tile
            # fully covered by locally stored regional extracts never
            # touches Overpass — and warming it would run country-sized
            # pbf scans INSIDE the front-end process, starving the
            # interface through the interpreter lock (the 2026-07-17
            # live "build appears hung": the interface sat at 100 %
            # processor parsing great-britain.osm.pbf while the worker
            # children built fine).  Its worker child filters the same
            # extracts in its OWN process instead.
            locally_covered = False
            if EXTRACTS is not None:
                try:
                    locally_covered = EXTRACTS.local_extracts_cover(
                        (tile[0], tile[1], tile[0] + 1, tile[1] + 1))
                except Exception:
                    locally_covered = False
            if locally_covered:
                print("[warm]", _short_latlon(tile),
                      "is covered by local OpenStreetMap extracts;"
                      " its build filters them directly.")
                with self._lock:
                    self._warming_tile = None
                    self._warmed_tiles.add(tile)
                    self._dispatch_locked()
                self._maybe_finish()
                continue
            try:
                with self._lock:
                    tile_build_dir = self._tile_arguments.get(
                        tile, {}).get("custom_build_dir", "")
                tile_configuration = CFG.Tile(
                    tile[0], tile[1], tile_build_dir)
                tile_configuration.read_from_config()
                specifications = VMAP.osm_layer_warm_specifications(
                    tile_configuration)
                warmed_layers = 0
                for (cached_suffix, queries, tags_of_interest,
                        node_tags_of_interest,
                        cache_schema) in specifications:
                    with self._lock:
                        still_queued = tile in self._queue
                        stopping = self._finished or self._cancel_all
                    if stopping or not still_queued:
                        break
                    OSM.OSM_queries_to_OSM_layer(
                        queries,
                        OSM.OSM_layer(),
                        tile[0],
                        tile[1],
                        tags_of_interest,
                        cached_suffix=cached_suffix,
                        node_tags_of_interest=node_tags_of_interest,
                        cache_schema=cache_schema,
                    )
                    warmed_layers += 1
                if warmed_layers:
                    print("[warm] OpenStreetMap cache ready for",
                          _short_latlon(tile),
                          "(%d layer(s))" % warmed_layers)
            except Exception as error:
                print("[warm] OpenStreetMap warm failed for",
                      _short_latlon(tile), ":", error,
                      "- its build will download for itself.")
            with self._lock:
                self._warming_tile = None
                self._warmed_tiles.add(tile)
                self._dispatch_locked()
            self._maybe_finish()

    # -- run end ----------------------------------------------------------
    def _maybe_finish(self):
        with self._lock:
            if self._finished:
                return
            busy = any(
                child.tile is not None
                for child in self._children
                if not child.retired
            )
            if self._queue or busy:
                return
            self._finished = True
            children = list(self._children)
            done, errors = self._done, self._errors
            cancelled = self._cancel_all
        # The session leaves its "building" state BEFORE the bounded
        # child reaping below: a Build click landing the moment the last
        # tile finishes must start a fresh run immediately, not wait out
        # slow-exiting workers.
        self._session._run_finished()
        self._session._emit(RunDone(done_count=done, error_count=errors,
                                    cancelled=cancelled))
        for child in children:
            child.retire()
        deadline = time.time() + 5.0
        for child in children:
            try:
                child.process.wait(timeout=max(0.1, deadline - time.time()))
            except Exception:
                child.terminate()

    def _ticker(self):
        """Run-level clock: elapsed + the learned remaining estimate
        (spec §3.3; a dash only when the time model has no basis)."""
        while True:
            with self._lock:
                if self._finished:
                    return
                completed = self._done + self._errors
                queued_tiles = list(self._queue)
                next_step_index = dict(self._next_step_index)
                in_flight_steps = dict(self._step_started_at)
                programs = dict(self._programs)
                total = self._total
            remaining = None
            try:
                remaining = estimate_remaining_wall_seconds(
                    self._estimates, programs, queued_tiles,
                    next_step_index, in_flight_steps, time.time(),
                    self._slots)
            except Exception:
                remaining = None
            self._session._emit(RunEta(
                elapsed_seconds=time.time() - self._t0,
                remaining_seconds=remaining,
                done_tiles=completed,
                total_tiles=total))
            time.sleep(1.0)
