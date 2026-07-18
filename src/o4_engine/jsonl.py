"""JSON-lines standard-input/output transport for the engine session
(docs/specs/engine-protocol-multi-gui.md §5, Phase 2).

This is the subprocess View: a front end in any language launches
``Ortho4XP.py --engine-jsonl`` and speaks a line-oriented protocol over the
child's standard streams.

Wire format
-----------
* **Commands** (caller -> engine): one JSON object per input line,
  ``{"cmd": <name>, "id": <caller id>, ...named arguments}``.  The named
  arguments match the corresponding :class:`o4_engine.session.EngineSession`
  method parameters exactly (for example ``tile_info`` takes ``lat``,
  ``lon``, ``working_dir``); the transport calls the method with them as
  keyword arguments.
* **Events** (engine -> caller): every :class:`o4_engine.events.EngineEvent`
  the session emits is written as one JSON object per output line, its
  ``dataclasses`` fields plus an ``"event"`` key naming the type.
* **Replies** (engine -> caller): every command, whatever events it also
  triggers, produces exactly one ``{"reply": <id>, "ok": true/false, ...}``
  line so callers can await completion without heuristics.  A successful
  reply carries ``"result"`` (the command's return value, serialized); a
  failed one carries ``"error"`` (a human string).

Design notes
------------
* **stdout is the protocol; nothing else may touch it.**  The pipeline is
  full of ``print``/``vprint`` calls that would corrupt the stream, so
  :func:`serve` keeps a private handle to the real stdout for protocol
  writes and repoints ``sys.stdout`` at ``sys.stderr`` for its whole
  lifetime — pipeline prints stay visible on stderr but can never land in
  the middle of a JSON line.  ``stderr`` is reserved for that plus uncaught
  crash text, per the spec.
* **Thread-safe, line-buffered writes.**  Session subscriber callbacks fire
  on worker threads while the read loop blocks on stdin, so every protocol
  write (its ``json.dumps`` line and the flush) is serialized under one
  lock.
* **Tuple / non-JSON handling.**  ``dataclasses.asdict`` cannot serialize
  :class:`o4_engine.events.ScanBatch` because its ``built`` mapping is keyed
  by ``(lat, lon)`` tuples, which are not valid JSON object keys.  The
  schema (that event's docstring) says consumers index positionally, so
  :func:`serialize_event` special-cases ``ScanBatch`` into a list of
  ``[lat, lon, info]`` triples.  All other non-JSON values (dataclasses,
  tuples, enums, Python ``type`` objects in the settings registry) are made
  JSON-safe by :func:`_json_safe`, which stringifies anything it does not
  recognize rather than raise.

The transport itself owns the ``EngineHello`` handshake line: the session
emits its own hello during construction, before any subscriber exists, so
the transport writes the handshake as its deliberate first protocol line.
"""

from __future__ import annotations

import dataclasses
import json
import os
import signal
import sys
import threading
import time
from enum import Enum
from typing import Any, Callable, Dict, TextIO

import O4_UI_Utils as UI

from .events import EngineHello, Error, ScanBatch
from .session import EngineSession


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable mirror of ``value``.

    The engine hands the transport values that ``json.dumps`` rejects on its
    own: dataclasses (``TileInfo``), tuples, :class:`enum.Enum` members
    (``LinkStatus``), and Python ``type`` objects (the settings registry's
    ``"type"`` fields).  Rather than let one such value crash a reply, every
    unrecognized object is stringified — the protocol is additive and
    forgiving by rule (spec §5), and a stringified value is far better than a
    dropped line.
    """
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _json_safe(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, dict):
        safe: Dict[Any, Any] = {}
        for key, item in value.items():
            # JSON object keys must be str/number/bool/None; a tuple key
            # (or any other object) is stringified so it never crashes.
            if not (key is None or isinstance(key, (str, int, float, bool))):
                key = str(key)
            safe[key] = _json_safe(item)
        return safe
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, type):
        return value.__name__
    if callable(value):
        return getattr(value, "__name__", repr(value))
    return str(value)


def serialize_event(event) -> Dict[str, Any]:
    """Serialize one :class:`o4_engine.events.EngineEvent` to a JSON-safe dict.

    The result is the event's dataclass fields plus an ``"event"`` key naming
    the type.  :class:`o4_engine.events.ScanBatch` gets a special case: its
    ``built`` mapping is keyed by ``(lat, lon)`` tuples, so it is emitted as a
    list of ``[lat, lon, info]`` triples (consumers index positionally, per
    that event's schema docstring).

    This is exported so the transport-equivalence test (spec §8.2) can
    serialize directly-subscribed events with the very serializer the pipe
    uses — the two paths cannot drift if they share this function.
    """
    payload: Dict[str, Any] = {"event": event.event}
    for f in dataclasses.fields(event):
        payload[f.name] = getattr(event, f.name)
    if isinstance(event, ScanBatch):
        payload["built"] = [
            [key[0], key[1], _json_safe(info)]
            for key, info in event.built.items()
        ]
    return _json_safe(payload)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------
def _ortho4xp_version() -> str:
    """Best-effort Ortho4XP version string for the handshake (optional)."""
    try:
        import O4_Version
        return O4_Version.version
    except Exception:
        return ""


def _build_handlers(session: EngineSession) -> Dict[str, Callable]:
    """Map command names 1:1 onto session methods.

    Kept in one place so an unknown command is a lookup miss (a clean
    ``ok=false`` reply) rather than an attribute error.
    """
    return {
        "scan": session.scan,
        "build": session.build,
        "enqueue_build": session.enqueue_build,
        "cancel": session.cancel,
        "cancel_tile": session.cancel_tile,
        "siblings": session.set_parallel_siblings,
        "tile_info": session.tile_info,
        "config_describe": session.config_describe,
        "links_status": session.links_status,
        "links_install": session.links_install,
        "links_uninstall": session.links_uninstall,
    }


def _initialize_pipeline_registries() -> None:
    """Load the imagery provider/extent/filter registries a build needs.

    Both interactive entry points (``Ortho4XP.py``, ``Ortho4XP_Qt.py``)
    run these initializers before any build; the transport must do the
    same for its own process — the 2026-07-16 parallel-build worker
    children failed every imagery step with "not a known provider"
    because the ``--engine-jsonl`` branch exits BEFORE the entry points'
    initialization.  Mirrors those entry points: ``Providers/`` joins
    ``sys.path`` (provider definition files may ship custom code), the
    four registries load, and the live-map imagery cache is shared so a
    worker child reuses tiles the map already fetched.  Called after the
    stdout repoint in :func:`serve`, so any initialization chatter lands
    on standard error, never inside the protocol stream.  Failures warn
    and continue: non-imagery commands must keep working.
    """
    try:
        import O4_File_Names as FNAMES

        if FNAMES.Provider_dir not in sys.path:
            sys.path.append(FNAMES.Provider_dir)
        import O4_Imagery_Utils as IMG

        IMG.initialize_extents_dict()
        IMG.initialize_color_filters_dict()
        IMG.initialize_providers_dict()
        IMG.initialize_combined_providers_dict()
        IMG.shared_tile_cache_dir = os.path.join(
            FNAMES.Preview_dir, "livemap")
    except Exception as error:
        print(
            "WARNING: imagery provider initialization failed in the"
            " engine transport:",
            error,
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Process lifecycle (owns_process mode)
# ---------------------------------------------------------------------------
# How long a dedicated engine process lets an in-flight build acknowledge
# the red flag before the process hard-exits anyway.  The polled
# cancellation contract stops most steps within a second or two; the cap
# exists for steps that never look at the flag.
SHUTDOWN_GRACE_SECONDS = 15.0
# Cadence of the parent-death watchdog's ``os.getppid()`` poll.
PARENT_WATCH_POLL_SECONDS = 1.0


def _shutdown_grace_seconds() -> float:
    """The bounded wind-down window, overridable through the
    ``O4_ENGINE_SHUTDOWN_GRACE_SECONDS`` environment variable (the
    lifecycle tests shrink it; real runs use the default)."""
    value = os.environ.get("O4_ENGINE_SHUTDOWN_GRACE_SECONDS")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
    return SHUTDOWN_GRACE_SECONDS


def _stop_session_and_exit_process(session: EngineSession,
                                   reason: str) -> None:
    """Stop any in-flight build and end THIS PROCESS within a bounded time.

    The only exit path for a dedicated engine process: the polled
    cancellation contract (``O4_UI_Utils.red_flag``, set through
    :meth:`EngineSession.cancel`) asks the build worker to stop at its
    next checkpoint; once it acknowledges — or the grace window expires —
    ``os._exit`` ends the process outright.  A plain ``sys.exit`` is NOT
    enough here: pipeline steps spawn non-daemon helper threads
    (``concurrent.futures`` pools in the elevation/bathymetry fetchers),
    and interpreter shutdown joins those while the daemon build-worker
    thread keeps launching new work — the 2026-07-16 orphan was still
    fetching a bathymetry band 1.5 h after its GUI closed, racing the
    replacement session's engine on the shared elevation caches.
    """
    try:
        session.cancel()
    except Exception:
        pass
    deadline = time.time() + _shutdown_grace_seconds()
    # _building is the session's own run flag, cleared by both run modes'
    # end paths once the cancel is acknowledged.
    while time.time() < deadline and session._building:
        time.sleep(0.1)
    try:
        print("Engine process exiting (%s)." % reason, file=sys.stderr)
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(0)


def _install_terminate_signal_handler(session: EngineSession) -> None:
    """Route SIGTERM through the bounded stop instead of dying mid-write.

    The parent orchestrator terminates slow-retiring workers and the Qt
    front end terminates them at application exit; giving the signal the
    same red-flag-then-exit path lets a cooperating step finish its
    current cache write instead of being cut mid-file.
    """
    def handle_terminate_signal(signal_number, frame):
        _stop_session_and_exit_process(session, "terminate signal")

    try:
        signal.signal(signal.SIGTERM, handle_terminate_signal)
    except (ValueError, OSError):
        # Not the main thread, or the platform does not support it: the
        # end-of-file and watchdog paths still bound the process's life.
        pass


def _parent_process_is_dead(declared_parent_id, initial_parent_id):
    """Whether the front end this engine belongs to is gone.

    Preferred signal: the spawner declares its pid in
    ``O4_PARENT_PROCESS_ID`` (the parallel orchestrator does) and we
    probe that pid directly — immune to the startup race where the
    parent dies before this process even captures its ppid.  Fallback
    (manual spawns): on POSIX an orphan is reparented, so a changed —
    or already-1 — ``os.getppid()`` means the parent is dead.
    """
    if declared_parent_id is not None:
        try:
            os.kill(declared_parent_id, 0)
            return False
        except ProcessLookupError:
            return True
        except Exception:
            return False
    current_parent_id = os.getppid()
    return (current_parent_id != initial_parent_id
            or (os.name == "posix" and current_parent_id == 1))


def _start_parent_death_watchdog(session: EngineSession) -> None:
    """Exit when the parent front end dies, even if stdin never closes.

    End-of-file on stdin is the primary death signal, but it only arrives
    once every copy of the pipe's write end is gone; a write end inherited
    by another long-lived process (or a parent that dies without the pipe
    collapsing) leaves this engine orphaned mid-build — exactly the
    observed failure shape (an engine with parent pid 1 still building
    headless long after its GUI closed).
    """
    declared = os.environ.get("O4_PARENT_PROCESS_ID")
    try:
        declared_parent_id = int(declared) if declared else None
    except ValueError:
        declared_parent_id = None
    initial_parent_id = os.getppid()

    def watch():
        while True:
            time.sleep(PARENT_WATCH_POLL_SECONDS)
            if _parent_process_is_dead(declared_parent_id,
                                       initial_parent_id):
                _stop_session_and_exit_process(
                    session, "parent process died")

    threading.Thread(target=watch, daemon=True,
                     name="o4-engine-parent-watchdog").start()


def serve(stdin: TextIO, stdout: TextIO, owns_process: bool = False) -> None:
    """Run the JSON-lines transport until ``stdin`` closes or ``shutdown``.

    Reads one command object per input line and writes events and replies to
    ``stdout``.  Returns when the input stream reaches end-of-file or a
    ``{"cmd": "shutdown"}`` command is received (its reply is written first).

    ``stdout`` is captured privately for protocol writes and ``sys.stdout`` is
    repointed at ``sys.stderr`` for the duration, so stray pipeline prints
    cannot corrupt the stream (restored on return).

    ``owns_process=True`` (the ``--engine-jsonl`` entry points) binds the
    whole process's life to the transport: end-of-file, ``shutdown``,
    SIGTERM, and parent death (a ppid watchdog) each red-flag any
    in-flight build and end the process within a bounded time, so a
    worker can never outlive its front end and keep building headless.
    In-process callers (the tests' transport harness) keep the historic
    return-to-caller behavior.
    """
    protocol_stdout = stdout
    write_lock = threading.Lock()

    def write_obj(obj: Dict[str, Any]) -> None:
        line = json.dumps(obj)
        with write_lock:
            protocol_stdout.write(line + "\n")
            protocol_stdout.flush()

    saved_sys_stdout = sys.stdout
    sys.stdout = sys.stderr
    try:
        _initialize_pipeline_registries()
        session = EngineSession(version=_ortho4xp_version())
        if owns_process:
            _install_terminate_signal_handler(session)
            _start_parent_death_watchdog(session)
        session.subscribe(lambda event: write_obj(serialize_event(event)))
        # The session's construction-time hello had no subscriber; the
        # transport owns the handshake framing and writes it first.
        write_obj(serialize_event(EngineHello(
            ortho4xp_version=_ortho4xp_version(),
            capabilities=("scan", "build", "enqueue_build", "cancel",
                          "tile_info", "config", "links", "siblings"))))

        handlers = _build_handlers(session)
        for raw_line in stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except (ValueError, TypeError) as exc:
                # Malformed line: report and keep reading (non-fatal).
                write_obj(serialize_event(
                    Error(fatal=False, text="malformed JSON line: %s" % exc)))
                continue
            if not isinstance(message, dict):
                write_obj(serialize_event(
                    Error(fatal=False,
                          text="command must be a JSON object")))
                continue

            command = message.get("cmd")
            call_id = message.get("id")
            if command == "shutdown":
                write_obj({"reply": call_id, "ok": True})
                if owns_process:
                    _stop_session_and_exit_process(
                        session, "shutdown command")
                return

            handler = handlers.get(command)
            if handler is None:
                write_obj({"reply": call_id, "ok": False,
                           "error": "unknown command: %r" % (command,)})
                continue

            arguments = {
                k: v for k, v in message.items() if k not in ("cmd", "id")
            }
            # build's tiles arrive as JSON arrays; the session keys estimate
            # dicts by tile, so they must be hashable tuples again.
            if (command in ("build", "enqueue_build")
                    and "tiles" in arguments):
                arguments["tiles"] = [
                    tuple(tile) for tile in arguments["tiles"]
                ]
            try:
                result = handler(**arguments)
            except Exception as exc:  # a bad command must not end the session
                write_obj({"reply": call_id, "ok": False,
                           "error": str(exc)})
                continue
            write_obj({"reply": call_id, "ok": True,
                       "result": _json_safe(result)})
        if owns_process:
            # End-of-file: the front end exited, crashed, or retired this
            # worker.  A build command returns immediately (the session
            # runs it on a worker thread), so a build may well be in
            # flight right now — it must not keep building headless.
            _stop_session_and_exit_process(session, "standard input closed")
    finally:
        sys.stdout = saved_sys_stdout
        # The transport owned this session's lifecycle; leave the module
        # routing attribute clean for whatever runs next in-process.
        UI.engine_session = None
        UI.red_flag = False
