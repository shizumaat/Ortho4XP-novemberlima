"""Scheduler tests for the parallel tile-build subprocess run
(docs/specs/parallel-tile-builds.md §3, §5).

These drive the REAL subprocess path of
``o4_engine.parallel.ParallelBuildRun`` against the pure-stdlib stub
worker in ``tests/fixtures/stub_engine_worker.py`` — no pipeline, no
network, no X-Plane install.  ``tile_worker_command`` is monkeypatched to
launch the stub, and the stub's script is chosen by tile COORDINATES, so
each test scripts the run through the tiles it builds:

* happy tiles (any lat not 60/61/62) complete done,
* ``lat == 60`` sleeps mid-build and honours a per-tile / global cancel,
* ``lat == 61`` fails its build,
* ``lat == 62`` crashes the worker mid-build.

The in-process cancel-semantics block at the end reuses the sequential
stub pipeline from ``test_engine_session`` (slots=1, no subprocess).
"""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

import O4_UI_Utils as UI  # noqa: E402
from o4_engine import events as EV  # noqa: E402
from o4_engine import parallel  # noqa: E402
from o4_engine import tile_time_model as TTM  # noqa: E402
from o4_engine.session import EngineSession  # noqa: E402

from test_engine_session import install_stub_pipeline  # noqa: E402


STUB_WORKER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures", "stub_engine_worker.py")


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_ui_routing():
    yield
    UI.engine_session = None
    UI.red_flag = False


@pytest.fixture
def stub_worker(monkeypatch):
    """Point the scheduler at the stub worker and stub the time model.

    The time model is only consulted on the in-process path, but stubbing
    it (as ``test_engine_session`` does) keeps every run deterministic and
    off the real ``~/.ortho4xp`` store.
    """
    monkeypatch.setattr(
        parallel, "tile_worker_command",
        lambda: [sys.executable, STUB_WORKER])
    monkeypatch.setattr(
        TTM, "predict_step_seconds",
        lambda lat, lon, features, steps: {key: 10.0 for key in steps})
    monkeypatch.setattr(TTM, "record_build", lambda *a, **k: None)
    return monkeypatch


class Collector:
    """Subscribe to a session and let a test block until RunDone."""

    def __init__(self, session):
        self.events = []
        self._done = threading.Event()
        session.subscribe(self._on_event)

    def _on_event(self, event):
        self.events.append(event)
        if isinstance(event, EV.RunDone):
            self._done.set()

    def wait_run_done(self, timeout=10.0):
        assert self._done.wait(timeout), "run did not finish in time"
        return [e for e in self.events if isinstance(e, EV.RunDone)][-1]

    # -- convenience accessors ------------------------------------------
    def tile_events(self, lat, lon):
        return [e for e in self.events
                if getattr(e, "lat", None) == lat
                and getattr(e, "lon", None) == lon]

    def of_type(self, cls):
        return [e for e in self.events if isinstance(e, cls)]


def _start_parallel(session, tiles, slots=2, **overrides):
    params = dict(provider="BI", zoomlevel=16, custom_build_dir="",
                  do_vector=True, do_imagery=True, do_overlays=False)
    params.update(overrides)
    return session.build(
        list(tiles), params["provider"], params["zoomlevel"],
        params["custom_build_dir"],
        do_vector=params["do_vector"], do_imagery=params["do_imagery"],
        do_overlays=params["do_overlays"], slots=slots)


def _wait_for(predicate, timeout=5.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Subprocess-path tests
# ---------------------------------------------------------------------------
def test_two_tiles_overlap_and_complete(stub_worker, tmp_path):
    """Two happy tiles at slots=2 build CONCURRENTLY (marker files: both
    starts precede either end), all four terminal events arrive, RunDone
    aggregates 2/0/False, and per-tile step order holds."""
    stub_worker.setenv("STUB_WORKER_MARK_DIR", str(tmp_path))
    session = EngineSession()
    collector = Collector(session)
    tiles = [(48, -6), (49, -6)]
    assert _start_parallel(session, tiles, slots=2) is True
    run_done = collector.wait_run_done()

    # Every tile's terminal events arrived.
    for lat, lon in tiles:
        events = collector.tile_events(lat, lon)
        states = [e for e in events if isinstance(e, EV.TileState)]
        builds = [e for e in events if isinstance(e, EV.BuildDone)]
        steps = [e for e in events if isinstance(e, EV.StepProgress)]
        assert states and states[-1].state == "done"
        assert len(builds) == 1 and builds[0].ok is True
        # Per-tile order: every StepProgress precedes the BuildDone.
        last_step = max(i for i, e in enumerate(events)
                        if isinstance(e, EV.StepProgress))
        build_index = next(i for i, e in enumerate(events)
                           if isinstance(e, EV.BuildDone))
        assert last_step < build_index
        assert steps  # forwarded, attributed to this tile

    assert (run_done.done_count, run_done.error_count,
            run_done.cancelled) == (2, 0, False)

    # Overlap proof: the LAST start precedes the FIRST end.
    def _mark(kind, lat, lon):
        with open(os.path.join(tmp_path, "%s_%d_%d" % (kind, lat, lon))) as h:
            return float(h.read())

    starts = [_mark("start", *t) for t in tiles]
    ends = [_mark("end", *t) for t in tiles]
    assert max(starts) < min(ends), (
        "tiles did not overlap in wall-clock time: starts=%s ends=%s"
        % (starts, ends))


def test_three_tiles_two_slots_all_complete(stub_worker, tmp_path):
    """Three tiles, two slots: the third is queued behind the first two and
    still completes; RunDone counts all three."""
    session = EngineSession()
    collector = Collector(session)
    tiles = [(10, 20), (11, 21), (12, 22)]
    assert _start_parallel(session, tiles, slots=2) is True
    run_done = collector.wait_run_done()

    for lat, lon in tiles:
        builds = [e for e in collector.tile_events(lat, lon)
                  if isinstance(e, EV.BuildDone)]
        assert len(builds) == 1 and builds[0].ok is True
    assert (run_done.done_count, run_done.error_count,
            run_done.cancelled) == (3, 0, False)


def test_failing_tile_reports_error_others_succeed(stub_worker, tmp_path):
    """A failing tile (lat 61) yields BuildDone ok=false and RunDone
    errors=1 while the other tile succeeds."""
    session = EngineSession()
    collector = Collector(session)
    tiles = [(61, 20), (10, 20)]
    assert _start_parallel(session, tiles, slots=2) is True
    run_done = collector.wait_run_done()

    failing = [e for e in collector.tile_events(61, 20)
               if isinstance(e, EV.BuildDone)]
    assert len(failing) == 1 and failing[0].ok is False
    assert any(isinstance(e, EV.TileState) and e.state == "error"
               for e in collector.tile_events(61, 20))

    other = [e for e in collector.tile_events(10, 20)
             if isinstance(e, EV.BuildDone)]
    assert len(other) == 1 and other[0].ok is True

    assert (run_done.done_count, run_done.error_count,
            run_done.cancelled) == (1, 1, False)


def test_crashing_worker_synthesizes_failure(stub_worker, tmp_path):
    """A crashing tile (lat 62): the parent synthesizes TileState(error) +
    BuildDone(ok=False, error mentions the worker); the OTHER tile still
    completes; RunDone errors=1."""
    session = EngineSession()
    collector = Collector(session)
    tiles = [(62, 20), (10, 20)]
    assert _start_parallel(session, tiles, slots=2) is True
    run_done = collector.wait_run_done()

    crashed = collector.tile_events(62, 20)
    builds = [e for e in crashed if isinstance(e, EV.BuildDone)]
    assert len(builds) == 1 and builds[0].ok is False
    assert "worker" in builds[0].error.lower()
    assert any(isinstance(e, EV.TileState) and e.state == "error"
               for e in crashed)

    other = [e for e in collector.tile_events(10, 20)
             if isinstance(e, EV.BuildDone)]
    assert len(other) == 1 and other[0].ok is True

    assert (run_done.done_count, run_done.error_count,
            run_done.cancelled) == (1, 1, False)


def test_cancel_queued_tile_never_starts(stub_worker, tmp_path):
    """cancel_tile on a QUEUED tile (3 tiles, slots=2, third queued behind
    two sleepers): the third emits TileState 'stopped', NEVER a
    StepProgress, and RunDone done=2."""
    session = EngineSession()
    collector = Collector(session)
    # Two slow sleepers keep both slots busy so the third stays queued.
    tiles = [(60, 1), (60, 2), (12, 3)]
    assert _start_parallel(session, tiles, slots=2) is True
    # Cancel the queued tile immediately (before either sleeper frees a slot).
    assert session.cancel_tile(12, 3) is True
    run_done = collector.wait_run_done()

    queued = collector.tile_events(12, 3)
    assert any(isinstance(e, EV.TileState) and e.label == "stopped"
               for e in queued)
    assert not any(isinstance(e, EV.StepProgress) for e in queued), (
        "a cancelled-while-queued tile must never emit progress")
    # The two sleepers completed; the queued one was never counted.
    assert (run_done.done_count, run_done.error_count,
            run_done.cancelled) == (2, 0, False)


def test_cancel_active_tile_stops_only_it(stub_worker, tmp_path):
    """cancel_tile on an ACTIVE tile (lat 60 sleeper) stops only that tile;
    the other completes 'done'; RunDone(done=1, cancelled=False)."""
    session = EngineSession()
    collector = Collector(session)
    tiles = [(60, 1), (10, 20)]
    assert _start_parallel(session, tiles, slots=2) is True
    # The sleeper is active for ~0.6 s — cancel it while it sleeps.
    assert _wait_for(
        lambda: any(isinstance(e, EV.StepProgress) and e.lat == 60
                    for e in collector.events))
    assert session.cancel_tile(60, 1) is True
    run_done = collector.wait_run_done()

    sleeper = collector.tile_events(60, 1)
    assert any(isinstance(e, EV.TileState) and e.label == "stopped"
               for e in sleeper)
    assert not any(isinstance(e, EV.TileState) and e.state == "done"
                   for e in sleeper)

    other = collector.tile_events(10, 20)
    assert any(isinstance(e, EV.TileState) and e.state == "done"
               for e in other)
    assert (run_done.done_count, run_done.error_count,
            run_done.cancelled) == (1, 0, False)


def test_global_cancel_stops_everything(stub_worker, tmp_path):
    """cancel() (global) mid-run ends RunDone(cancelled=True); queued tiles
    report 'stopped'."""
    session = EngineSession()
    collector = Collector(session)
    tiles = [(60, 1), (60, 2), (60, 3)]
    assert _start_parallel(session, tiles, slots=2) is True
    # Both slots active on sleepers, the third queued — cancel everything.
    assert _wait_for(
        lambda: sum(1 for e in collector.events
                    if isinstance(e, EV.StepProgress)) >= 2)
    session.cancel()
    run_done = collector.wait_run_done()

    assert run_done.cancelled is True
    # The queued tile was drained with a 'stopped' state and never ran.
    queued = collector.tile_events(60, 3)
    assert any(isinstance(e, EV.TileState) and e.label == "stopped"
               for e in queued)
    assert not any(isinstance(e, EV.StepProgress) for e in queued)


def test_spawn_failure_falls_back_to_in_process(stub_worker, monkeypatch,
                                                 tmp_path, capsys):
    """A worker child that dies before the handshake makes the whole run
    fall back to the in-process worker: build() still returns True, RunDone
    arrives with done == number of tiles, and a warning is printed."""
    # A child that exits before EngineHello — the handshake never lands.
    monkeypatch.setattr(
        parallel, "tile_worker_command",
        lambda: [sys.executable, "-c", "import sys; sys.exit(3)"])
    # Keep the handshake wait short so the fallback is prompt (<10 s test).
    monkeypatch.setattr(parallel, "HANDSHAKE_TIMEOUT_SECONDS", 2.0)
    # The fallback runs the in-process worker, which imports the pipeline.
    install_stub_pipeline(monkeypatch)

    session = EngineSession()
    collector = Collector(session)
    tiles = [(10, 20), (11, 21)]
    assert _start_parallel(session, tiles, slots=2) is True
    run_done = collector.wait_run_done()

    assert run_done.done_count == len(tiles)
    assert run_done.cancelled is False
    output = capsys.readouterr().out
    assert "one at a time" in output or "could not be started" in output


# ---------------------------------------------------------------------------
# In-process (slots=1) per-tile cancel semantics — spec §3.4
# ---------------------------------------------------------------------------
def _run_in_process(session, tiles, tmp_path, collector=None):
    collector = collector or Collector(session)
    started = session.build(
        list(tiles), "BI", 16, str(tmp_path),
        do_vector=True, do_imagery=True, do_overlays=False, slots=1)
    assert started is True
    return collector


def test_in_process_cancel_queued_tile(monkeypatch, tmp_path):
    """(a) Cancelling a QUEUED tile before the in-process worker reaches it:
    that tile is 'stopped', the others complete, RunDone cancelled=False."""
    holder = {}

    def cancel_second(_tile):
        # Runs inside the first tile's vector step — the second tile is
        # still queued in the worker's todo walk.
        holder["session"].cancel_tile(11, 21)

    install_stub_pipeline(monkeypatch, hooks={"vector": cancel_second})
    session = EngineSession()
    holder["session"] = session
    collector = _run_in_process(session, [(10, 20), (11, 21), (12, 22)],
                                tmp_path)
    run_done = collector.wait_run_done(30.0)

    stopped = collector.tile_events(11, 21)
    assert any(isinstance(e, EV.TileState) and e.label == "stopped"
               for e in stopped)
    assert not any(isinstance(e, EV.StepProgress) for e in stopped)
    # The other two tiles built.
    for lat, lon in [(10, 20), (12, 22)]:
        builds = [e for e in collector.tile_events(lat, lon)
                  if isinstance(e, EV.BuildDone)]
        assert len(builds) == 1 and builds[0].ok is True
    assert run_done.cancelled is False
    assert run_done.done_count == 2


def test_in_process_cancel_active_tile_continues(monkeypatch, tmp_path):
    """(b) Cancelling the ACTIVE tile from a step hook stops only it; the
    NEXT tile still builds; RunDone includes the rest, cancelled=False."""
    holder = {}
    fired = {"done": False}

    def cancel_self(_tile):
        # Cancel the currently-building tile exactly once.
        if not fired["done"]:
            fired["done"] = True
            holder["session"].cancel_tile(10, 20)

    install_stub_pipeline(monkeypatch, hooks={"vector": cancel_self})
    session = EngineSession()
    holder["session"] = session
    collector = _run_in_process(session, [(10, 20), (11, 21)], tmp_path)
    run_done = collector.wait_run_done(30.0)

    first = collector.tile_events(10, 20)
    assert any(isinstance(e, EV.TileState) and e.label == "stopped"
               for e in first)
    assert not any(isinstance(e, EV.TileState) and e.state == "done"
                   for e in first)
    # The next tile built normally.
    second = [e for e in collector.tile_events(11, 21)
              if isinstance(e, EV.BuildDone)]
    assert len(second) == 1 and second[0].ok is True
    assert run_done.cancelled is False
    assert run_done.done_count == 1


def test_in_process_global_cancel_stops_run(monkeypatch, tmp_path):
    """(c) One integration assertion that session.cancel() still cancels the
    whole in-process run (the detailed version lives in
    test_engine_session)."""
    holder = {}

    def cancel_all(_tile):
        holder["session"].cancel()

    install_stub_pipeline(monkeypatch, hooks={"vector": cancel_all})
    session = EngineSession()
    holder["session"] = session
    collector = _run_in_process(session, [(10, 20), (11, 21)], tmp_path)
    run_done = collector.wait_run_done(30.0)

    assert run_done.cancelled is True
    assert not collector.tile_events(11, 21)
