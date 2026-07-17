"""JSON-lines transport tests for :mod:`o4_engine.jsonl`
(docs/specs/engine-protocol-multi-gui.md §5, §8).

Covers reply framing, a golden transcript shape for a stubbed one-tile
build, the transport-equivalence guard against two-transport drift (§8.2),
and error handling for malformed / unknown commands.  All headless, no
network, no X-Plane install; the stubbed build reuses the pipeline stubs
from test_engine_session.py.
"""

import io
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

import O4_UI_Utils as UI  # noqa: E402
from o4_engine import events as EV  # noqa: E402
from o4_engine import jsonl  # noqa: E402
from o4_engine.session import EngineSession  # noqa: E402

from test_engine_session import install_stub_pipeline  # noqa: E402


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def reset_ui_routing():
    yield
    UI.engine_session = None
    UI.red_flag = False


class _CaptureStream:
    """A thread-safe writable that splits protocol writes into lines.

    serve() writes from the read-loop thread and from worker threads (under
    its own lock); this collector locks too and lets a test block until a
    line matching a predicate appears (the asynchronous build path).
    """

    def __init__(self):
        self._buffer = ""
        self.lines = []
        self._condition = threading.Condition()

    def write(self, text):
        with self._condition:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                self.lines.append(line)
            self._condition.notify_all()
        return len(text)

    def flush(self):
        pass

    def wait_for(self, predicate, timeout=30.0):
        deadline = time.time() + timeout
        with self._condition:
            while True:
                if any(predicate(line) for line in self.lines):
                    return True
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    def objects(self):
        return [json.loads(line) for line in self.lines if line.strip()]


def _is_event(line, name):
    try:
        message = json.loads(line)
    except (ValueError, TypeError):
        return False
    return isinstance(message, dict) and message.get("event") == name


def _run_transport_build(monkeypatch, tmp_path, do_imagery):
    """Drive one stubbed build through serve() over an OS pipe.

    Returns the parsed output objects once RunDone has been observed and the
    session has been shut down.
    """
    install_stub_pipeline(monkeypatch)
    read_fd, write_fd = os.pipe()
    stdin = os.fdopen(read_fd, "r")
    writer = os.fdopen(write_fd, "w")
    capture = _CaptureStream()
    thread = threading.Thread(target=jsonl.serve, args=(stdin, capture))
    thread.start()
    try:
        writer.write(json.dumps({
            "cmd": "build", "id": 1, "tiles": [[10, 20]],
            "provider": "BI", "zoomlevel": 16,
            "custom_build_dir": str(tmp_path),
            "do_vector": True, "do_imagery": do_imagery,
            "do_overlays": False}) + "\n")
        writer.flush()
        assert capture.wait_for(lambda line: _is_event(line, "RunDone")), \
            "build never produced RunDone"
        writer.write(json.dumps({"cmd": "shutdown", "id": 2}) + "\n")
        writer.flush()
        thread.join(30)
        assert not thread.is_alive()
    finally:
        writer.close()
        stdin.close()
    return capture.objects()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_every_command_gets_one_reply(tmp_path):
    """Reply framing: hello first, then exactly one ok=true reply per
    command carrying its id."""
    commands = "\n".join([
        json.dumps({"cmd": "config_describe", "id": 1}),
        json.dumps({"cmd": "tile_info", "id": 2, "lat": 10, "lon": 20,
                    "working_dir": str(tmp_path)}),
        json.dumps({"cmd": "shutdown", "id": 3}),
    ]) + "\n"
    capture = _CaptureStream()
    jsonl.serve(io.StringIO(commands), capture)
    objects = capture.objects()

    assert objects[0].get("event") == "EngineHello"
    reply_ids = [o["reply"] for o in objects if "reply" in o]
    assert sorted(reply_ids) == [1, 2, 3]           # exactly one each
    replies = {o["reply"]: o for o in objects if "reply" in o}
    assert all(reply["ok"] is True for reply in replies.values())
    # config_describe returns the settings registry as a dict.
    assert isinstance(replies[1]["result"], dict) and replies[1]["result"]


def test_golden_transcript_single_tile_build(monkeypatch, tmp_path):
    """Golden shape (event-type sequence + key fields, volatile fields
    ignored) for a stubbed one-tile vector/mesh/masks build."""
    objects = _run_transport_build(monkeypatch, tmp_path, do_imagery=False)
    events = [o for o in objects if "event" in o]
    event_types = [o["event"] for o in events]

    assert event_types[0] == "EngineHello"     # handshake framing
    # The tile lifecycle sequence, ignoring RunEta cadence noise.
    lifecycle = [et for et in event_types
                 if et in ("StepProgress", "TileState", "BuildDone", "RunDone")]
    assert lifecycle == ["StepProgress", "StepProgress", "StepProgress",
                         "TileState", "BuildDone", "RunDone"]

    steps = [o["step_key"] for o in events if o["event"] == "StepProgress"]
    assert steps == ["vector", "mesh", "masks"]

    build_done = [o for o in events if o["event"] == "BuildDone"][0]
    assert (build_done["lat"], build_done["lon"], build_done["ok"]) == \
        (10, 20, True)
    tile_state = [o for o in events if o["event"] == "TileState"][-1]
    assert tile_state["state"] == "done"
    run_done = [o for o in events if o["event"] == "RunDone"][0]
    assert (run_done["done_count"], run_done["error_count"],
            run_done["cancelled"]) == (1, 0, False)


_VOLATILE_FIELDS = {"ts", "elapsed_seconds", "remaining_seconds"}


def _normalize(value):
    """Blank out wall-clock volatile fields so two runs compare equal."""
    if isinstance(value, dict):
        return {key: (None if key in _VOLATILE_FIELDS else _normalize(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def test_transport_matches_direct_subscription(monkeypatch, tmp_path):
    """Spec §8.2 drift guard: the same stubbed build serialized from a direct
    subscription and read off the pipe must be identical apart from the
    handshake and wall-clock fields."""
    # Direct in-process subscription.
    install_stub_pipeline(monkeypatch)
    direct_events = []
    finished = threading.Event()
    session = EngineSession()

    def collect(event):
        direct_events.append(event)
        if isinstance(event, EV.RunDone):
            finished.set()

    session.subscribe(collect)
    session.build([(10, 20)], "BI", 16, str(tmp_path),
                  do_vector=True, do_imagery=False, do_overlays=False)
    assert finished.wait(30)
    UI.engine_session = None
    UI.red_flag = False
    direct_serialized = [jsonl.serialize_event(e) for e in direct_events]

    # Same build over the transport.
    objects = _run_transport_build(monkeypatch, tmp_path, do_imagery=False)
    # Drop the handshake hello (transport-owned) and every reply line; the
    # direct path had neither.
    transport_events = [o for o in objects
                        if "event" in o and o["event"] != "EngineHello"]

    assert [_normalize(o) for o in transport_events] == \
        [_normalize(o) for o in direct_serialized]


def test_malformed_line_and_unknown_command(tmp_path):
    """A malformed JSON line yields a non-fatal Error event and the loop
    continues; an unknown command yields ok=false."""
    commands = "\n".join([
        "{ this is not valid json",
        json.dumps({"cmd": "no_such_command", "id": 7}),
        json.dumps({"cmd": "shutdown", "id": 8}),
    ]) + "\n"
    capture = _CaptureStream()
    jsonl.serve(io.StringIO(commands), capture)
    objects = capture.objects()

    assert any(o.get("event") == "Error" and o.get("fatal") is False
               for o in objects)
    unknown = [o for o in objects if o.get("reply") == 7]
    assert len(unknown) == 1
    assert unknown[0]["ok"] is False and "error" in unknown[0]
    assert any(o.get("reply") == 8 and o.get("ok") is True for o in objects)


def test_transport_initializes_imagery_providers():
    """The 2026-07-16 parallel-build regression, pinned: worker children
    run ``--engine-jsonl`` which exits BEFORE the interactive entry
    points' provider initialization, so the transport must load the
    imagery registries itself — otherwise every imagery step fails with
    "not a known provider" in the child."""
    from o4_engine import jsonl
    import O4_Imagery_Utils as IMG

    jsonl._initialize_pipeline_registries()
    assert IMG.providers_dict, "providers must load inside the transport"
    assert IMG.shared_tile_cache_dir, (
        "worker children must reuse the live-map imagery cache"
    )
