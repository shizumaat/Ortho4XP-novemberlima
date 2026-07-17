#!/usr/bin/env python3
"""A stand-in worker child for the parallel-tile-build scheduler tests.

Pure standard library.  Speaks the JSON-lines engine protocol
(``src/o4_engine/jsonl.py`` docstring) well enough for
``o4_engine.parallel.ParallelBuildRun`` to drive it as a real worker
subprocess, but runs NO pipeline: it emits a scripted event stream whose
shape is chosen by the tile COORDINATES the parent hands it, so a test
controls the script purely through the tiles it builds.

Protocol, briefly:

* On start we print one ``{"event": "EngineHello", ...}`` line to stdout
  and flush (the parent's handshake gate), then read commands, one JSON
  object per stdin line.
* ``build`` (``tiles=[[lat, lon]]``): reply ``{"reply": id, "ok": true}``
  then emit a per-tile scripted stream ending in the child-run-level
  ``RunDone`` the parent expects (and suppresses).
* ``cancel``: set a flag the active build polls.
* End-of-file on stdin: exit 0 (the parent closed our stdin to retire us).

Tile-coordinate scripts (switch on ``lat``):

* ``lat == 60`` — a cancellable sleeper: emit progress, then sleep ~0.6 s
  polling stdin (via the reader thread's cancel flag).  If cancelled,
  emit ``TileState`` label ``"stopped"`` + ``RunDone``; otherwise finish
  the happy path.
* ``lat == 61`` — a failing tile: ``BuildDone`` ``ok=false`` +
  ``TileState`` state ``"error"`` then ``RunDone``.
* ``lat == 62`` — a crashing worker: emit one progress line then
  ``os._exit(1)`` mid-build (no ``RunDone`` — the parent synthesizes the
  failure from the unexpected exit).
* anything else — the happy path: two ``StepProgress`` lines, then
  ``TileState`` done + ``BuildDone`` ok + ``RunDone``.

If ``STUB_WORKER_MARK_DIR`` is set, ``start_<lat>_<lon>`` and
``end_<lat>_<lon>`` marker files (each containing ``time.time()``) are
written at build start / end so a test can PROVE two tiles overlapped in
wall-clock time.

Every emitted event is one JSON object per line on stdout, flushed;
nothing else touches stdout (any chatter goes to stderr).
"""

import json
import os
import queue
import sys
import threading
import time

MARK_DIR = os.environ.get("STUB_WORKER_MARK_DIR")

# Small inter-event pauses so concurrent tiles overlap measurably in
# wall-clock time (the two-slot overlap test asserts on marker files).
_STEP_PAUSE = 0.06
_TERMINAL_PAUSE = 0.08
_SLEEPER_SECONDS = 0.6
_SLEEPER_POLL = 0.02

_cancel_flag = threading.Event()
_build_queue: "queue.Queue" = queue.Queue()
_eof = threading.Event()


def _emit(obj):
    """Write one protocol line to stdout and flush."""
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _chatter(*parts):
    """Non-protocol text goes to stderr only (the parent tees it)."""
    print(*parts, file=sys.stderr, flush=True)


def _write_marker(kind, lat, lon, step=None, only_if_absent=False):
    if not MARK_DIR:
        return
    name = "%s_%d_%d" % (kind, lat, lon)
    if step:
        name += "_" + step
    path = os.path.join(MARK_DIR, name)
    if only_if_absent and os.path.exists(path):
        return
    try:
        with open(path, "w") as handle:
            handle.write(repr(time.time()))
    except OSError as error:
        _chatter("stub worker could not write marker", path, error)


def _step(lat, lon, key, percent, indeterminate=False):
    _emit({
        "event": "StepProgress",
        "lat": lat, "lon": lon,
        "step_key": key, "label": key,
        "percent": percent, "indeterminate": indeterminate,
    })


def _tile_state(lat, lon, state, label="", percent=0.0):
    _emit({
        "event": "TileState",
        "lat": lat, "lon": lon,
        "state": state, "label": label, "percent": percent,
    })


def _build_done(lat, lon, ok, error=""):
    _emit({
        "event": "BuildDone",
        "lat": lat, "lon": lon, "ok": ok, "error": error,
    })


def _run_done():
    # The parent suppresses the child's RunDone (it is child-run-level) and
    # uses it only as the "this tile's run finished" signal.
    _emit({"event": "RunDone", "done_count": 0, "error_count": 0,
           "cancelled": False})


def _reader():
    """Background stdin reader: routes builds to a queue, sets the cancel
    flag, and signals end-of-file."""
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        command = message.get("cmd")
        if command == "cancel":
            _cancel_flag.set()
        elif command == "build":
            _build_queue.put(message)
        # Any other command (shutdown, etc.) is ignored by this stub.
    _eof.set()
    _build_queue.put(None)   # unblock the main loop


def _run_one_build(message):
    tiles = message.get("tiles") or [[0, 0]]
    lat, lon = int(tiles[0][0]), int(tiles[0][1])
    # The phase-aware orchestrator (spec 3.8) sends one step per build
    # command; whole-build commands (no "steps") keep the legacy shape.
    steps = message.get("steps") or []
    step_key = steps[0] if steps else "vector"
    _emit({"reply": message.get("id"), "ok": True})
    # A child is a fresh process after a per-tile cancel, but reuse across
    # tiles is allowed for the non-cancel path — clear any stale flag.
    _cancel_flag.clear()
    # Tile-level markers: "start" is the FIRST command's instant (never
    # overwritten), "end" the latest command's end (last step wins).
    _write_marker("start", lat, lon, only_if_absent=True)
    if steps:
        _write_marker("stepstart", lat, lon, step=step_key)

    if lat == 60:
        _sleeper_tile(lat, lon, step_key)
    elif lat == 61:
        _failing_tile(lat, lon, step_key)
    elif lat == 62:
        _crashing_tile(lat, lon, step_key)
    else:
        _happy_tile(lat, lon, step_key)

    if steps:
        _write_marker("stepend", lat, lon, step=step_key)
    _write_marker("end", lat, lon)


def _happy_tile(lat, lon, step_key="vector"):
    _step(lat, lon, step_key, 0.0)
    time.sleep(_STEP_PAUSE)
    _step(lat, lon, step_key, 50.0)
    time.sleep(_STEP_PAUSE)
    _tile_state(lat, lon, "done", percent=100.0)
    time.sleep(_TERMINAL_PAUSE)
    _build_done(lat, lon, True)
    time.sleep(_TERMINAL_PAUSE)
    _run_done()


def _sleeper_tile(lat, lon, step_key="vector"):
    _step(lat, lon, step_key, 0.0)
    time.sleep(_STEP_PAUSE)
    _step(lat, lon, step_key, 30.0)
    deadline = time.time() + _SLEEPER_SECONDS
    while time.time() < deadline:
        if _cancel_flag.is_set():
            _tile_state(lat, lon, "queued", label="stopped")
            time.sleep(_TERMINAL_PAUSE)
            _run_done()
            return
        time.sleep(_SLEEPER_POLL)
    # Never cancelled: finish the happy way.
    _tile_state(lat, lon, "done", percent=100.0)
    time.sleep(_TERMINAL_PAUSE)
    _build_done(lat, lon, True)
    time.sleep(_TERMINAL_PAUSE)
    _run_done()


def _failing_tile(lat, lon, step_key="vector"):
    _step(lat, lon, step_key, 0.0)
    time.sleep(_STEP_PAUSE)
    _step(lat, lon, step_key, 50.0, indeterminate=True)
    time.sleep(_STEP_PAUSE)
    _build_done(lat, lon, False, error="a build step failed")
    time.sleep(_TERMINAL_PAUSE)
    _tile_state(lat, lon, "error", label="failed")
    time.sleep(_TERMINAL_PAUSE)
    _run_done()


def _crashing_tile(lat, lon, step_key="vector"):
    _step(lat, lon, step_key, 0.0)
    time.sleep(_STEP_PAUSE)
    _chatter("stub worker crashing on purpose for", lat, lon)
    os._exit(1)


def main():
    _emit({
        "event": "EngineHello",
        "ortho4xp_version": "stub-worker",
        "protocol": "1.1",
        "capabilities": ["build", "cancel", "cancel_tile"],
    })
    threading.Thread(target=_reader, daemon=True).start()
    while True:
        message = _build_queue.get()
        if message is None:      # stdin closed
            break
        _run_one_build(message)
    sys.exit(0)


if __name__ == "__main__":
    main()
