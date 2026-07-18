"""Tests for cross-tile build coordination
(docs/specs/parallel-tile-builds.md §3.7–3.8): the parent-side
OpenStreetMap cache warmer, the assignment gate that keeps a warming
tile off the workers, and the phase-aware orchestrator's resource-class
limits.

Headless, no network: the warmer's download modules are stubbed through
``sys.modules`` (the warmer imports them lazily inside its thread), and
the suite-wide ``O4_DISABLE_OSM_WARMER`` guard from ``conftest.py`` is
lifted only inside the tests that exercise the warmer itself.  Worker
children are the stub protocol speaker from
``tests/fixtures/stub_engine_worker.py``, which writes per-step
``stepstart_/stepend_`` marker files so class-concurrency claims can be
proven from wall-clock intervals.
"""

from __future__ import annotations

import os
import sys
import time
import types
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from o4_engine import parallel
from o4_engine.events import RunDone
from o4_engine.session import EngineSession

STUB_WORKER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "stub_engine_worker.py",
)


@pytest.fixture
def stub_worker_command(monkeypatch):
    monkeypatch.setattr(
        parallel, "tile_worker_command",
        lambda: [sys.executable, STUB_WORKER],
    )


@pytest.fixture
def collector():
    return []


def _run_build(session, events, tiles, slots, timeout=25.0):
    done = []

    def collect(event):
        events.append(event)
        if isinstance(event, RunDone):
            done.append(event)

    session.subscribe(collect)
    assert session.build(
        tiles, "STUBPROVIDER", 16, "", do_vector=True, do_imagery=False,
        slots=slots,
    )
    deadline = time.time() + timeout
    while not done and time.time() < deadline:
        time.sleep(0.02)
    assert done, "RunDone never arrived"
    return done[0]


def _install_warm_stubs(monkeypatch, warm_log, layer_seconds=0.02):
    """Stub the warmer's lazily imported modules through sys.modules."""

    def fake_queries_to_layer(queries, layer, lat, lon, tags,
                              cached_suffix="", node_tags_of_interest=[],
                              cache_schema=""):
        time.sleep(layer_seconds)
        warm_log.append(((lat, lon), cached_suffix, time.time()))
        return 1

    fake_osm = types.ModuleType("O4_OSM_Utils")
    fake_osm.OSM_layer = lambda: object()
    fake_osm.OSM_queries_to_OSM_layer = fake_queries_to_layer

    fake_vmap = types.ModuleType("O4_Vector_Map")
    fake_vmap.osm_layer_warm_specifications = lambda tile: [
        ("airports", ["q"], ["all"], [], ""),
        ("coastline", ["q"], [], [], ""),
    ]

    fake_cfg = types.ModuleType("O4_Config_Utils")

    class _Tile:
        def __init__(self, lat, lon, build_dir):
            self.lat, self.lon = lat, lon

        def read_from_config(self):
            return 1

    fake_cfg.Tile = _Tile
    monkeypatch.setitem(sys.modules, "O4_OSM_Utils", fake_osm)
    monkeypatch.setitem(sys.modules, "O4_Vector_Map", fake_vmap)
    monkeypatch.setitem(sys.modules, "O4_Config_Utils", fake_cfg)


def _step_interval(tmp_path, tile, step):
    start = tmp_path / ("stepstart_%d_%d_%s" % (tile[0], tile[1], step))
    end = tmp_path / ("stepend_%d_%d_%s" % (tile[0], tile[1], step))
    assert start.exists() and end.exists(), (
        "missing markers for %s %s" % (tile, step))
    return float(start.read_text()), float(end.read_text())


def _max_concurrency(intervals):
    """Peak overlap count over a list of (start, end) intervals."""
    boundary_events = sorted(
        [(start, 1) for start, _ in intervals]
        + [(end, -1) for _, end in intervals]
    )
    active = peak = 0
    for _, delta in boundary_events:
        active += delta
        peak = max(peak, active)
    return peak


# ---------------------------------------------------------------------
# The dispatcher gates (unit level)
# ---------------------------------------------------------------------
class _FakeChild:
    def __init__(self):
        self.tile = None
        self.running_step = None
        self.step_failed = False
        self.cancelling = False
        self.retired = False
        self.started = []

    def start_step(self, step_key, build_arguments):
        self.running_step = step_key
        self.started.append((self.tile, step_key))
        return True

    def send(self, payload):
        return True


def _bare_run(tiles, slots):
    return parallel.ParallelBuildRun(
        SimpleNamespace(_emit=lambda e: None, _run_finished=lambda: None),
        tiles, "P", 16, "", (True, False, False), slots,
    )


def test_new_tile_skips_the_tile_being_warmed():
    run = _bare_run([(10, 10), (11, 11)], 2)
    child = _FakeChild()
    run._warming_tile = (10, 10)
    with run._lock:
        assert run._start_new_tile_locked(child) is True
    assert child.tile == (11, 11), "must skip the warming tile"
    # Only the warming tile left: nothing assignable.
    other = _FakeChild()
    with run._lock:
        assert run._start_new_tile_locked(other) is False
    assert (10, 10) in run._queue, "the warming tile stays queued"


def test_step_dispatch_respects_class_capacity():
    run = _bare_run([(10, 10), (11, 11), (12, 12)], 3)
    # program = [vector, mesh, masks]; limits: osm 2, compute 2.
    first, second, third = _FakeChild(), _FakeChild(), _FakeChild()
    with run._lock:
        assert run._start_new_tile_locked(first) is True
        assert run._start_new_tile_locked(second) is True
        # Third tile's first step is osm-class and the cap is 2.
        assert run._start_new_tile_locked(third) is False
    assert third.tile is None
    assert run._class_active["osm"] == 2


def test_blocked_children_dispatch_finish_first():
    run = _bare_run([(10, 10), (11, 11)], 2)
    ahead, behind = _FakeChild(), _FakeChild()
    ahead.tile, behind.tile = (10, 10), (11, 11)
    run._children = [behind, ahead]
    run._next_step_index = {(10, 10): 2, (11, 11): 1}  # masks vs mesh
    run._queue.clear()
    # Compute capacity of exactly one (slots=2): only the LATER step
    # (ahead, masks) may start.
    run._class_limits = {"osm": 2, "imagery": 2, "compute": 1}
    with run._lock:
        run._dispatch_locked()
    assert ahead.running_step == "masks"
    assert behind.running_step is None


def test_osm_and_imagery_budgets_are_independent():
    """A full imagery class must not block a tile's vector step — the
    two classes exhaust SEPARATE servers (spec §3.8, makespan goal)."""
    run = parallel.ParallelBuildRun(
        SimpleNamespace(_emit=lambda e: None, _run_finished=lambda: None),
        [(10, 10), (11, 11), (12, 12)], "P", 16, "",
        (True, True, False), 4,
    )
    # program = [vector, mesh, masks, imagery]
    imagery_one, imagery_two, fresh = (
        _FakeChild(), _FakeChild(), _FakeChild())
    imagery_one.tile, imagery_two.tile = (10, 10), (11, 11)
    run._children = [imagery_one, imagery_two, fresh]
    run._next_step_index = {(10, 10): 3, (11, 11): 3}
    run._queue.clear()
    run._queue.append((12, 12))
    with run._lock:
        assert run._try_start_step_locked(imagery_one) is True
        assert run._try_start_step_locked(imagery_two) is True
        assert run._class_active["imagery"] == 2
        # Imagery is saturated, yet a NEW tile's vector step starts.
        assert run._start_new_tile_locked(fresh) is True
    assert fresh.running_step == "vector"


def test_mesh_memory_admission_gate():
    run = _bare_run([(10, 10), (11, 11)], 3)
    big, other = _FakeChild(), _FakeChild()
    big.tile, other.tile = (10, 10), (11, 11)
    run._children = [big, other]
    run._queue.clear()
    run._next_step_index = {(10, 10): 1, (11, 11): 1}  # both at mesh
    run._mesh_memory_estimates = {(10, 10): 10.0, (11, 11): 10.0}
    run._mesh_memory_budget = 12.0
    with run._lock:
        assert run._try_start_step_locked(big) is True
        # Two 10 GB meshes exceed the 12 GB budget: the second waits.
        assert run._try_start_step_locked(other) is False
        assert run._mesh_memory_in_use == 10.0
        # Releasing the first admits the second.
        run._release_step_resources_locked(big)
        assert run._try_start_step_locked(other) is True
        assert run._mesh_memory_in_use == 10.0


def test_mesh_memory_gate_always_admits_one():
    """A single tile must never deadlock, however big its raster."""
    run = _bare_run([(10, 10)], 2)
    child = _FakeChild()
    child.tile = (10, 10)
    run._children = [child]
    run._queue.clear()
    run._next_step_index = {(10, 10): 1}
    run._mesh_memory_estimates = {(10, 10): 50.0}
    run._mesh_memory_budget = 8.0
    with run._lock:
        assert run._try_start_step_locked(child) is True


def test_mesh_memory_estimates_follow_elevation_level():
    assert parallel.mesh_memory_estimate_gigabytes("1") == 18.0
    assert parallel.mesh_memory_estimate_gigabytes("5") == 8.0
    assert parallel.mesh_memory_estimate_gigabytes("auto") == 2.0
    assert parallel.mesh_memory_estimate_gigabytes(None) == 2.0
    assert parallel.mesh_memory_estimate_gigabytes("garbage") == 2.0


# ---------------------------------------------------------------------
# Class limits end to end (wall-clock proof via per-step markers)
# ---------------------------------------------------------------------
def test_compute_steps_run_concurrently(
    monkeypatch, tmp_path, stub_worker_command, collector
):
    """Compute steps are uncapped (2026-07-17 ruling: the operating
    system arbitrates processor contention) — two sleeper tiles' mesh
    steps overlap in wall-clock time; only the memory admission gate
    and the network classes ever hold a step back."""
    monkeypatch.setenv("STUB_WORKER_MARK_DIR", str(tmp_path))
    session = EngineSession()
    # Sleeper tiles (lat 60) sleep ~0.6 s inside EVERY step, so their
    # mesh intervals overlap solidly when dispatched concurrently.
    tiles = [(60, -100), (60, -101)]
    result = _run_build(session, collector, tiles, slots=2)
    assert (result.done_count, result.error_count) == (2, 0)
    mesh_intervals = [
        _step_interval(tmp_path, tile, "mesh") for tile in tiles
    ]
    assert _max_concurrency(mesh_intervals) == 2, (
        "uncapped compute steps must run concurrently"
    )


def test_network_class_capped_at_two_with_three_slots(
    monkeypatch, tmp_path, stub_worker_command, collector
):
    monkeypatch.setenv("STUB_WORKER_MARK_DIR", str(tmp_path))
    session = EngineSession()
    tiles = [(40, -100), (41, -100), (42, -100)]
    result = _run_build(session, collector, tiles, slots=3)
    assert result.done_count == 3
    vector_intervals = [
        _step_interval(tmp_path, tile, "vector") for tile in tiles
    ]
    assert _max_concurrency(vector_intervals) <= 2, (
        "at most two tiles may occupy the network class"
    )


def test_percent_remaps_into_the_full_plan_window(
    monkeypatch, tmp_path, stub_worker_command, collector
):
    session = EngineSession()
    tiles = [(40, -100), (41, -100)]
    result = _run_build(session, collector, tiles, slots=2)
    assert result.done_count == 2
    # The stub emits 50% inside each single-step build; after remapping,
    # a masks-step 50% must land above where a vector-step 100% would
    # (percent never restarts across steps).
    from o4_engine.events import StepProgress

    per_tile = {}
    for event in collector:
        if isinstance(event, StepProgress):
            per_tile.setdefault((event.lat, event.lon), []).append(
                event.percent)
    for tile, percents in per_tile.items():
        assert percents == sorted(percents), (
            "whole-tile percent must be monotonic, got %s for %s"
            % (percents, tile))
        assert max(percents) <= 100.0


# ---------------------------------------------------------------------
# Warmer end to end: queued tiles warm before their builds start
# ---------------------------------------------------------------------
def test_warmer_warms_queued_tiles_before_their_builds(
    monkeypatch, tmp_path, stub_worker_command, collector
):
    monkeypatch.delenv("O4_DISABLE_OSM_WARMER", raising=False)
    monkeypatch.setenv("STUB_WORKER_MARK_DIR", str(tmp_path))
    warm_log = []
    _install_warm_stubs(monkeypatch, warm_log)

    session = EngineSession()
    tiles = [(40, -100), (41, -100), (42, -100), (43, -100)]
    result = _run_build(session, collector, tiles, slots=2)
    assert (result.done_count, result.error_count) == (4, 0)

    warmed = {entry[0] for entry in warm_log}
    # Only tiles that were QUEUED (not first-wave assigned) get warmed;
    # with two slots the first two tiles start immediately.
    assert warmed <= {(42, -100), (43, -100)}
    # Every warmed tile finished warming BEFORE its build started (the
    # no-race guarantee: assignment skips the tile being warmed).
    for tile in warmed:
        last_warm = max(t for (w, _s, t) in warm_log if w == tile)
        marker = tmp_path / ("start_%d_%d" % tile)
        assert marker.exists()
        build_start = float(marker.read_text())
        assert last_warm <= build_start + 1e-3


def test_warmer_skips_tiles_covered_by_local_extracts(
    monkeypatch, tmp_path, stub_worker_command, collector
):
    """The warmer exists to spare OVERPASS; a tile fully covered by
    local regional extracts never touches Overpass, and warming it
    would run country-sized pbf scans inside the front-end process,
    starving the interface through the interpreter lock (live
    "build appears hung", 2026-07-17)."""
    monkeypatch.delenv("O4_DISABLE_OSM_WARMER", raising=False)
    monkeypatch.setenv("STUB_WORKER_MARK_DIR", str(tmp_path))
    warm_log = []
    _install_warm_stubs(monkeypatch, warm_log)
    fake_extracts = types.ModuleType("O4_OSM_Extracts")
    fake_extracts.local_extracts_cover = lambda bounding_box: True
    monkeypatch.setitem(sys.modules, "O4_OSM_Extracts", fake_extracts)

    session = EngineSession()
    tiles = [(40, -100), (41, -100), (42, -100), (43, -100)]
    result = _run_build(session, collector, tiles, slots=2)
    assert (result.done_count, result.error_count) == (4, 0)
    assert warm_log == [], (
        "locally covered tiles must not be warmed in the front-end "
        "process")


def test_warmer_disabled_by_environment(
    monkeypatch, stub_worker_command, collector
):
    # conftest sets O4_DISABLE_OSM_WARMER=1 suite-wide; leave it in place.
    warm_log = []
    _install_warm_stubs(monkeypatch, warm_log)
    session = EngineSession()
    tiles = [(40, -100), (41, -100), (42, -100)]
    result = _run_build(session, collector, tiles, slots=2)
    assert result.done_count == 3
    assert warm_log == [], "disabled warmer must never download"


def test_step_progress_never_slides_backward():
    """The legacy in-step bars oscillate (they refill per OpenStreetMap
    layer, per download phase); the forwarder ratchets so a per-tile bar
    only ever advances — the 2026-07-16 "bars jump around" report."""
    emitted = []
    run = parallel.ParallelBuildRun(
        SimpleNamespace(_emit=emitted.append, _run_finished=lambda: None),
        [(10, 10)], "P", 16, "", (True, True, False), 2,
    )
    child = _FakeChild()
    child.tile = (10, 10)
    run._next_step_index[(10, 10)] = 3  # imagery, window 36.8..100
    for oscillating in (10.0, 80.0, 20.0, 90.0, 60.0):
        run._on_child_event(child, {
            "event": "StepProgress", "lat": 10, "lon": 10,
            "step_key": "imagery", "label": "imagery & DSF",
            "percent": oscillating, "indeterminate": False,
        })
    percents = [e.percent for e in emitted]
    assert percents == sorted(percents), (
        "ratcheted percent must be monotonic, got %s" % percents)
    assert len(set(percents)) >= 2, "the bar must still advance"
    # A later, genuinely higher value still moves the bar.
    assert percents[-1] >= percents[1]


def test_sibling_count_broadcast_when_a_tile_finishes():
    """When a child runs out of work, survivors get a "siblings"
    message so their Auto slot resolutions stop sharing the machine
    with ghosts (live case: one tile's imagery fully cached and done
    in seconds, the other throttled to half download throughput for
    its entire 17-minute cold download)."""
    run = _bare_run([(10, 10), (11, 11)], 2)

    class _SendingChild(_FakeChild):
        def __init__(self):
            super().__init__()
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)
            return True

    finished, survivor = _SendingChild(), _SendingChild()
    finished.tile, survivor.tile = (10, 10), (11, 11)
    run._children = [finished, survivor]
    run._queue.clear()
    # The finished child's tile is on its last step.
    program = run._programs[(10, 10)]
    run._next_step_index[(10, 10)] = len(program) - 1
    run._next_step_index[(11, 11)] = 0
    finished.running_step = program[-1]
    run._child_step_done(finished)
    assert finished.tile is None
    assert {"cmd": "siblings", "count": 1} in survivor.sent
    assert not finished.sent
    # No further change: no duplicate broadcast.
    survivor.sent.clear()
    run._broadcast_sibling_count()
    assert survivor.sent == []


def test_sibling_count_ignores_queued_tiles():
    """Queued tiles consume nothing until a child picks them up, so
    they must NOT count as siblings: the pre-2026-07-17 formula told
    every child in a 2-slot 6-tile run that six siblings shared the
    machine, throttling each to a sixth of it for the whole run."""
    run = _bare_run([(10, 10), (11, 11), (12, 12), (13, 13),
                     (14, 14), (15, 15)], 2)

    class _SendingChild(_FakeChild):
        def __init__(self):
            super().__init__()
            self.sent = []

        def send(self, payload):
            self.sent.append(payload)
            return True

    first, second = _SendingChild(), _SendingChild()
    first.tile, second.tile = (10, 10), (11, 11)
    run._children = [first, second]
    for tile in [(10, 10), (11, 11)]:
        run._queue.remove(tile)
        run._next_step_index[tile] = 0
    # Four tiles still queued; two children hold tiles.  The broadcast
    # baseline is the slot count (2) — holders match it, so nothing is
    # sent, and above all nothing says "6".
    run._broadcast_sibling_count()
    assert first.sent == [] and second.sent == []
    assert run._sibling_broadcast == 2


def test_enqueue_admits_new_tiles_and_skips_running_ones():
    """enqueue() on a live run admits fresh tiles (per-batch build
    arguments) and refuses duplicates of queued or active tiles."""
    run = _bare_run([(10, 10), (11, 11)], 2)
    # No real worker processes in this unit test.
    run._spawn_child = lambda: None
    child = _FakeChild()
    child.tile = (10, 10)
    run._children = [child]
    run._queue.remove((10, 10))
    run._next_step_index[(10, 10)] = 1

    admitted = run.enqueue([(10, 10), (11, 11), (12, 12)],
                           "OTHERPROVIDER", 17, "/elsewhere/",
                           (True, True, True))
    assert admitted == 1
    assert (12, 12) in run._queue
    assert run._total == 3
    # The new batch keeps its own build arguments and step program.
    assert run._tile_arguments[(12, 12)]["provider"] == "OTHERPROVIDER"
    assert run._tile_arguments[(12, 12)]["zoomlevel"] == 17
    assert run._programs[(12, 12)] == [
        "vector", "mesh", "masks", "imagery", "overlays"]
    # The original batch is untouched.
    assert run._tile_arguments[(11, 11)]["provider"] == "P"
    assert run._programs[(11, 11)] == ["vector", "mesh", "masks"]


def test_enqueue_refused_once_run_finished():
    run = _bare_run([(10, 10)], 2)
    run._finished = True
    assert run.enqueue([(11, 11)], "P", 16, "", (True, False, False)) == 0
