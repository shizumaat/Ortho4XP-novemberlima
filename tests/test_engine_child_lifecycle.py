"""Engine child process-lifecycle tests (the 2026-07-16 orphan regression).

An ``--engine-jsonl`` child whose front end died used to keep building
headless: stdin end-of-file made ``serve`` return, but ``sys.exit``
blocked in interpreter shutdown joining non-daemon pipeline helper
threads while the daemon build-worker thread kept launching new work —
one orphan (parent pid 1) was still fetching a bathymetry band 1.5 h
after its GUI closed, racing the replacement session's engine on the
shared elevation caches.

These tests spawn REAL engine child processes (the genuine transport and
session, with the pipeline stubbed to a slow step — see
``tests/fixtures/slow_build_engine_child.py``) and assert every death
signal ends the process within a bounded time:

* stdin end-of-file with a build in flight whose step honors the polled
  ``red_flag`` contract (the cooperative wind-down);
* stdin end-of-file with a step that IGNORES the flag (the hard exit
  deadline, ``O4_ENGINE_SHUTDOWN_GRACE_SECONDS``);
* parent death while stdin is held open by a third process (the
  ``os.getppid()`` watchdog — end-of-file alone would never arrive).

All headless: no network, no GUI toolkit, no X-Plane install.
"""

import json
import os
import selectors
import subprocess
import sys
import textwrap
import time

import pytest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
CHILD_RUNNER = os.path.join(TESTS_DIR, "fixtures",
                            "slow_build_engine_child.py")

# The stubbed step runs this long unless stopped — every bound asserted
# below is far under it, so a pass can never mean "the step just ended".
SLOW_STEP_SECONDS = 120.0


class _ProtocolReader:
    """Incremental line reader over a child's stdout pipe with timeouts."""

    def __init__(self, process):
        self._file_descriptor = process.stdout.fileno()
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._file_descriptor,
                                selectors.EVENT_READ)
        self._buffer = b""

    def wait_for(self, predicate, timeout):
        """Return the first JSON object line matching ``predicate``, or
        None if the stream ends or the timeout expires first."""
        deadline = time.time() + timeout
        while True:
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and predicate(message):
                    return message
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            if not self._selector.select(min(remaining, 0.2)):
                continue
            chunk = os.read(self._file_descriptor, 65536)
            if not chunk:
                return None
            self._buffer += chunk


def _spawn_engine_child(tmp_path, extra_environment=None):
    environment = dict(
        os.environ,
        O4_ENGINE_SHUTDOWN_GRACE_SECONDS="8.0",
        SLOW_STEP_SECONDS=str(SLOW_STEP_SECONDS),
    )
    environment.update(extra_environment or {})
    stderr_file = open(os.path.join(str(tmp_path), "child_stderr.txt"), "wb")
    process = subprocess.Popen(
        [sys.executable, CHILD_RUNNER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_file,
        env=environment,
    )
    stderr_file.close()
    return process


def _start_slow_build(process, reader, tmp_path):
    """Handshake, send one build, and wait until its slow step is running."""
    assert reader.wait_for(
        lambda message: message.get("event") == "EngineHello",
        timeout=30.0), "engine child never said hello"
    command = {
        "cmd": "build", "id": 1, "tiles": [[10, 20]],
        "provider": "BI", "zoomlevel": 16,
        "custom_build_dir": str(tmp_path),
        "do_vector": True, "do_imagery": False, "do_overlays": False,
    }
    process.stdin.write((json.dumps(command) + "\n").encode())
    process.stdin.flush()
    assert reader.wait_for(
        lambda message: (message.get("event") == "StepProgress"
                         and message.get("step_key") == "vector"),
        timeout=30.0), "the slow vector step never started"


def _assert_exits_within(process, bound_seconds):
    started = time.time()
    try:
        process.wait(timeout=bound_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        pytest.fail("engine child still alive %.0f s after its death "
                    "signal — the orphan regression" % bound_seconds)
    return time.time() - started


def test_stdin_eof_stops_in_flight_build_and_exits(tmp_path):
    """Closing the child's stdin mid-build (what a dead or exiting front
    end looks like) must red-flag the running step and end the process
    promptly — far sooner than the step would have run."""
    process = _spawn_engine_child(tmp_path)
    try:
        reader = _ProtocolReader(process)
        _start_slow_build(process, reader, tmp_path)
        process.stdin.close()
        elapsed = _assert_exits_within(process, 20.0)
        assert elapsed < SLOW_STEP_SECONDS / 2
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()


def test_grace_deadline_bounds_a_step_that_ignores_red_flag(tmp_path):
    """A step deaf to the polled cancellation contract must still be
    bounded: the grace window expires and the process hard-exits."""
    process = _spawn_engine_child(
        tmp_path,
        extra_environment={
            "SLOW_STEP_IGNORES_RED_FLAG": "1",
            "O4_ENGINE_SHUTDOWN_GRACE_SECONDS": "2.0",
        })
    try:
        reader = _ProtocolReader(process)
        _start_slow_build(process, reader, tmp_path)
        process.stdin.close()
        _assert_exits_within(process, 15.0)
    finally:
        if process.poll() is None:
            process.kill()


def _process_is_gone(process_id):
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


@pytest.mark.skipif(os.name != "posix",
                    reason="orphan reparenting is a POSIX signal")
def test_parent_death_watchdog_exits_even_with_stdin_held_open(tmp_path):
    """The watchdog leg: the engine's parent dies but a THIRD process
    still holds the stdin pipe's write end, so end-of-file never arrives
    (the shape a leaked file descriptor produces).  The ``os.getppid()``
    watchdog must end the orphan anyway."""
    intermediate_source = textwrap.dedent("""
        import json, os, subprocess, sys
        runner = os.environ["ENGINE_CHILD_RUNNER"]
        # Declare ourselves the way the parallel orchestrator does, so the
        # child's watchdog probes this pid (robust against this process
        # dying before the child finishes starting up).
        os.environ["O4_PARENT_PROCESS_ID"] = str(os.getpid())
        read_end, write_end = os.pipe()
        holder = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            pass_fds=(write_end,),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child = subprocess.Popen(
            [sys.executable, runner], stdin=read_end,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(read_end)
        os.close(write_end)
        print(json.dumps({"child": child.pid, "holder": holder.pid}),
              flush=True)
        # Exit immediately: the engine child is now an orphan whose stdin
        # is still open (the holder keeps the write end alive).
    """)
    environment = dict(
        os.environ,
        ENGINE_CHILD_RUNNER=CHILD_RUNNER,
        O4_ENGINE_SHUTDOWN_GRACE_SECONDS="2.0",
        SLOW_STEP_SECONDS=str(SLOW_STEP_SECONDS),
    )
    output = subprocess.run(
        [sys.executable, "-c", intermediate_source],
        env=environment, capture_output=True, text=True, timeout=60,
    )
    assert output.returncode == 0, output.stderr
    process_ids = json.loads(output.stdout)
    child_process_id = process_ids["child"]
    holder_process_id = process_ids["holder"]
    try:
        deadline = time.time() + 20.0
        while time.time() < deadline:
            if _process_is_gone(child_process_id):
                break
            time.sleep(0.2)
        else:
            pytest.fail("orphaned engine child (pid %d) outlived its "
                        "parent despite the watchdog" % child_process_id)
    finally:
        for process_id in (child_process_id, holder_process_id):
            try:
                os.kill(process_id, 9)
            except (ProcessLookupError, PermissionError):
                pass
