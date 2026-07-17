"""Stub-build tests for :class:`o4_engine.session.EngineSession`
(docs/specs/engine-protocol-multi-gui.md §7 Phase 2, §8).

These drive a whole build headlessly — no Qt, no network, no X-Plane
install, no real pipeline.  ``EngineSession._build_worker`` imports the six
heavy pipeline modules BY NAME from inside the worker thread, so stub
modules installed into ``sys.modules`` before the build starts win, and the
build exercises the session's real event/percent/ETA machinery against
trivial step functions.

The stub pipeline (Tile + six step functions) is shared with the transport
tests (test_engine_jsonl.py imports it) so both prove the same behaviour.
"""

import os
import sys
import threading
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

import O4_UI_Utils as UI  # noqa: E402
from o4_engine import events as EV  # noqa: E402
from o4_engine import tile_time_model as TTM  # noqa: E402
from o4_engine.session import EngineSession  # noqa: E402


# ---------------------------------------------------------------------------
# The stub pipeline (shared with the transport tests)
# ---------------------------------------------------------------------------
class _StubTile:
    """Minimal stand-in for ``O4_Config_Utils.Tile``.

    Records the constructor arguments and the ``make_dirs`` call so tests can
    assert the worker wired it up, and reports "no per-tile config" so the
    worker takes the provider/zoomlevel default branch.
    """

    def __init__(self, lat, lon, build_dir):
        self.lat = lat
        self.lon = lon
        self.build_dir = build_dir
        self.default_website = None
        self.default_zl = None
        self.made_dirs = False

    def read_from_config(self):
        return False

    def make_dirs(self):
        self.made_dirs = True


def install_stub_pipeline(monkeypatch, results=None, hooks=None):
    """Install stub pipeline modules into ``sys.modules`` for one build.

    ``results`` maps a step key ("vector"/"mesh"/"masks"/"imagery"/"overlays")
    to the integer the step returns (default 1 = success; 0 = failure), or to
    a ``callable(tile) -> int`` when the outcome must depend on the tile.
    ``hooks`` maps a step key to a ``callable(tile)`` run inside the step —
    used to simulate legacy progress-bar traffic or a mid-step cancel.

    Also points the tile-time model at deterministic, side-effect-free stubs
    so the run clock is stable and nothing is written under ``~/.ortho4xp``.
    """
    results = results or {}
    hooks = hooks or {}

    def make_step(step_key):
        def step(tile):
            hook = hooks.get(step_key)
            if hook is not None:
                hook(tile)
            result = results.get(step_key, 1)
            return result(tile) if callable(result) else result
        return step

    config_module = types.ModuleType("O4_Config_Utils")
    config_module.Tile = _StubTile
    vector_module = types.ModuleType("O4_Vector_Map")
    vector_module.build_poly_file = make_step("vector")
    mesh_module = types.ModuleType("O4_Mesh_Utils")
    mesh_module.build_mesh = make_step("mesh")
    mask_module = types.ModuleType("O4_Mask_Utils")
    mask_module.build_masks = make_step("masks")
    tile_module = types.ModuleType("O4_Tile_Utils")
    tile_module.build_tile = make_step("imagery")
    overlay_module = types.ModuleType("O4_Overlay_Utils")
    _overlay_step = make_step("overlays")
    # The session calls OVL.build_overlay(t.lat, t.lon); the stub ignores it.
    overlay_module.build_overlay = lambda lat, lon: _overlay_step(None)

    for module in (config_module, vector_module, mesh_module, mask_module,
                   tile_module, overlay_module):
        monkeypatch.setitem(sys.modules, module.__name__, module)

    monkeypatch.setattr(
        TTM, "predict_step_seconds",
        lambda lat, lon, features, steps: {key: 10.0 for key in steps})
    monkeypatch.setattr(TTM, "record_build", lambda *args, **kwargs: None)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_ui_routing():
    """Always clear the module-level engine routing state (work item 2.5)."""
    yield
    UI.engine_session = None
    UI.red_flag = False


def run_build(session, tiles, tmp_path, **overrides):
    """Start a build, block until ``RunDone``, and return the events list."""
    events = []
    finished = threading.Event()

    def collect(event):
        events.append(event)
        if isinstance(event, EV.RunDone):
            finished.set()

    session.subscribe(collect)
    params = dict(provider="BI", zoomlevel=16, custom_build_dir=str(tmp_path),
                  do_vector=True, do_imagery=True, do_overlays=False)
    params.update(overrides)
    started = session.build(
        tiles, params["provider"], params["zoomlevel"],
        params["custom_build_dir"],
        do_vector=params["do_vector"], do_imagery=params["do_imagery"],
        do_overlays=params["do_overlays"])
    assert started is True
    assert finished.wait(30), "build did not finish within 30 s"
    return events


def _tile_events(events, lat, lon):
    return [e for e in events
            if getattr(e, "lat", None) == lat
            and getattr(e, "lon", None) == lon]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_two_tile_build_emits_ordered_events(monkeypatch, tmp_path):
    """Per tile, one StepProgress(start) per planned step, then
    TileState(done) and BuildDone(ok); the run ends RunDone(2, 0, False)."""
    install_stub_pipeline(monkeypatch)
    session = EngineSession()
    tiles = [(10, 20), (11, 21)]
    events = run_build(session, tiles, tmp_path)

    planned = ["vector", "mesh", "masks", "imagery"]
    for lat, lon in tiles:
        tile_events = _tile_events(events, lat, lon)
        starts = [e for e in tile_events if isinstance(e, EV.StepProgress)]
        # One StepProgress start per step (no progress-bar traffic here).
        ordered_keys = []
        for e in starts:
            if e.step_key not in ordered_keys:
                ordered_keys.append(e.step_key)
        assert ordered_keys == planned

        states = [e for e in tile_events if isinstance(e, EV.TileState)]
        builds = [e for e in tile_events if isinstance(e, EV.BuildDone)]
        assert states and states[-1].state == "done"
        assert len(builds) == 1 and builds[0].ok is True

        # Steps precede the terminal tile events.
        last_step_index = max(i for i, e in enumerate(tile_events)
                              if isinstance(e, EV.StepProgress))
        done_index = next(i for i, e in enumerate(tile_events)
                          if isinstance(e, EV.TileState) and e.state == "done")
        assert last_step_index < done_index

    run_done = [e for e in events if isinstance(e, EV.RunDone)][-1]
    assert (run_done.done_count, run_done.error_count,
            run_done.cancelled) == (2, 0, False)


def test_build_selection_overrides_stale_tile_config(monkeypatch, tmp_path):
    """The provider/zoom selected for the build must win over whatever the
    tile config recorded last time.  Regression 2026-07-15: a stale config
    with an EMPTY default_website was left in place (the worker only applied
    the selection when no config existed), so a whole tile built with no
    imagery source — zero textures — while reporting success."""
    seen = {}
    install_stub_pipeline(
        monkeypatch,
        hooks={"vector": lambda tile: seen.setdefault("tile", tile)})

    class _StaleConfigTile(_StubTile):
        def __init__(self, lat, lon, build_dir):
            super().__init__(lat, lon, build_dir)
            self.default_website = ""   # stale empty provider
            self.default_zl = 12

        def read_from_config(self):
            return True

    sys.modules["O4_Config_Utils"].Tile = _StaleConfigTile
    session = EngineSession()
    run_build(session, [(10, 20)], tmp_path)
    assert seen["tile"].default_website == "BI"
    assert seen["tile"].default_zl == 16

    # A config recording a DIFFERENT provider is overridden the same way.
    class _OtherProviderTile(_StaleConfigTile):
        def __init__(self, lat, lon, build_dir):
            super().__init__(lat, lon, build_dir)
            self.default_website = "USA2"

    seen.clear()
    sys.modules["O4_Config_Utils"].Tile = _OtherProviderTile
    run_build(EngineSession(), [(10, 20)], tmp_path)
    assert seen["tile"].default_website == "BI"
    assert seen["tile"].default_zl == 16


def test_failed_step_marks_error_and_run_continues(monkeypatch, tmp_path):
    """A step returning 0 marks its tile error/BuildDone(ok=False); the run
    proceeds to the next tile."""
    # Fail the mesh step for the first tile only, so the run must continue.
    install_stub_pipeline(
        monkeypatch, results={"mesh": lambda tile: 0 if tile.lat == 10 else 1})
    session = EngineSession()
    tiles = [(10, 20), (11, 21)]
    events = run_build(session, tiles, tmp_path)

    first = _tile_events(events, 10, 20)
    assert any(isinstance(e, EV.TileState) and e.state == "error"
               and e.label == "failed" for e in first)
    first_done = [e for e in first if isinstance(e, EV.BuildDone)][0]
    assert first_done.ok is False

    second = _tile_events(events, 11, 21)
    second_done = [e for e in second if isinstance(e, EV.BuildDone)][0]
    assert second_done.ok is True

    run_done = [e for e in events if isinstance(e, EV.RunDone)][-1]
    assert (run_done.done_count, run_done.error_count,
            run_done.cancelled) == (1, 1, False)


def test_cancel_mid_run_skips_remaining_tiles(monkeypatch, tmp_path):
    """cancel() from inside a step ends with RunDone(cancelled=True) and emits
    nothing for the tiles that never ran."""
    holder = {}

    def cancel_hook(_tile):
        holder["session"].cancel()

    install_stub_pipeline(monkeypatch, hooks={"vector": cancel_hook})
    session = EngineSession()
    holder["session"] = session
    tiles = [(10, 20), (11, 21)]
    events = run_build(session, tiles, tmp_path)

    run_done = [e for e in events if isinstance(e, EV.RunDone)][-1]
    assert run_done.cancelled is True
    # No event of any kind for the second, never-started tile.
    assert not _tile_events(events, 11, 21)
    # The first tile was reported stopped, never done.
    assert any(isinstance(e, EV.TileState) and e.label == "stopped"
               for e in events)
    assert not any(isinstance(e, EV.TileState) and e.state == "done"
                   for e in events)


def test_legacy_progress_bar_drives_stepprogress_and_eta(monkeypatch,
                                                         tmp_path):
    """progress_bar() calls during a step produce StepProgress events with
    rising percent, and RunEta events whose remaining_seconds is populated
    once step estimates exist."""
    def vector_hook(_tile):
        for percentage in (20, 50, 90):
            UI.progress_bar(1, percentage)

    install_stub_pipeline(monkeypatch, hooks={"vector": vector_hook})
    session = EngineSession()
    events = run_build(session, [(10, 20)], tmp_path)

    vector_steps = [e for e in events
                    if isinstance(e, EV.StepProgress) and e.step_key == "vector"]
    percents = [e.percent for e in vector_steps]
    assert percents == sorted(percents)      # never restarts / never dips
    assert percents[-1] > percents[0]        # the bar traffic moved it

    etas = [e for e in events if isinstance(e, EV.RunEta)]
    assert etas
    assert any(e.remaining_seconds is not None for e in etas)
