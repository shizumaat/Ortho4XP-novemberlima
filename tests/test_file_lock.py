"""Tests for the cross-process advisory cache lock (``src/O4_File_Lock.py``)
and its application around ``O4_Airport_Elevation_Insets.ensure_base_tile``
(``docs/specs/parallel-tile-builds.md`` section 3.5).

All headless: real ``multiprocessing`` workers for the cross-process
mutual-exclusion proof, in-process threads for the ``ensure_base_tile``
serialisation proof, and ``tmp_path`` for every lock file.  No network.

The synthetic-registry monkeypatch style of
``tests/test_elevation_level_providers.py`` is followed for the
``ensure_base_tile`` test: a fake base definition plus a one-entry
``ACCESS_STRATEGIES`` table whose ``ensure_tile`` observes concurrency.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

import O4_File_Lock as LOCK
import O4_Airport_Elevation_Insets as INSETS
import O4_File_Names as FNAMES


# =====================================================================
# Cross-process mutual exclusion (real processes)
# =====================================================================
def _mutual_exclusion_worker(
    worker_id, lock_target, shared_file, iterations
):
    """Append matched enter/exit markers under the lock, repeatedly.

    Runs in a SEPARATE process.  If the lock genuinely serialises the two
    workers, every ``enter`` marker in the shared file is immediately
    followed by the SAME worker's ``exit`` marker -- the critical sections
    never interleave.
    """
    import O4_File_Lock as WORKER_LOCK

    for _ in range(iterations):
        with WORKER_LOCK.hold_file_lock(lock_target, timeout_seconds=60.0):
            with open(shared_file, "a") as handle:
                handle.write("%d-enter\n" % worker_id)
                handle.flush()
            time.sleep(0.02)
            with open(shared_file, "a") as handle:
                handle.write("%d-exit\n" % worker_id)
                handle.flush()


def test_mutual_exclusion_across_real_processes(tmp_path):
    lock_target = str(tmp_path / "shared_cache_artifact")
    shared_file = str(tmp_path / "critical_section_log.txt")
    iterations = 8

    processes = [
        multiprocessing.Process(
            target=_mutual_exclusion_worker,
            args=(worker_id, lock_target, shared_file, iterations),
        )
        for worker_id in (0, 1)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=120)
        assert process.exitcode == 0

    with open(shared_file) as handle:
        lines = [line.strip() for line in handle if line.strip()]

    # Two workers, ``iterations`` critical sections each, two markers per
    # section: the log must be exactly that many lines and pair up cleanly.
    assert len(lines) == 2 * 2 * iterations
    for index in range(0, len(lines), 2):
        enter_worker, enter_kind = lines[index].split("-")
        exit_worker, exit_kind = lines[index + 1].split("-")
        # Never an enter immediately followed by a DIFFERENT worker's marker:
        # that is the interleaving the lock exists to prevent.
        assert enter_kind == "enter"
        assert exit_kind == "exit"
        assert enter_worker == exit_worker

    # The lock file is cleaned up once every holder has released it.
    assert not os.path.exists(lock_target + ".lock")


# =====================================================================
# Lock released on an exception inside the guarded block
# =====================================================================
def test_lock_released_on_exception(tmp_path):
    lock_target = str(tmp_path / "artifact")

    class GuardedFailure(RuntimeError):
        pass

    try:
        with LOCK.hold_file_lock(lock_target, timeout_seconds=5.0) as acquired:
            assert acquired is True
            assert os.path.exists(lock_target + ".lock")
            raise GuardedFailure("boom inside the critical section")
    except GuardedFailure:
        pass

    # The finally-clause removed the lock even though the block raised, so
    # the next builder can acquire it.
    assert not os.path.exists(lock_target + ".lock")


# =====================================================================
# Stale lock (old mtime) is broken and acquisition proceeds
# =====================================================================
def test_stale_lock_is_broken_and_reacquired(tmp_path, monkeypatch):
    lock_target = str(tmp_path / "artifact")
    lock_path = lock_target + ".lock"

    # A leftover lock file from a crashed holder, aged well past the stale
    # threshold.
    with open(lock_path, "w") as handle:
        handle.write("99999 2000-01-01T00:00:00\n")
    old_time = time.time() - (LOCK.STALE_LOCK_AGE_SECONDS + 600.0)
    os.utime(lock_path, (old_time, old_time))

    warnings = []
    monkeypatch.setattr(
        LOCK.UI, "vprint", lambda level, *args: warnings.append(args)
    )

    with LOCK.hold_file_lock(lock_target, timeout_seconds=5.0) as acquired:
        # The stale lock was broken and freshly re-acquired by us.
        assert acquired is True
        assert os.path.exists(lock_path)

    assert not os.path.exists(lock_path)
    # Exactly the stale-break warning fired, and it named the lock file.
    assert warnings, "a stale-lock break must emit one warning"
    assert any(lock_path in [str(item) for item in args] for args in warnings)


# =====================================================================
# Timeout: proceed WITHOUT the lock, leaving a live foreign lock intact
# =====================================================================
def test_timeout_proceeds_without_lock(tmp_path, monkeypatch):
    lock_target = str(tmp_path / "artifact")
    lock_path = lock_target + ".lock"

    # A foreign holder's lock with a RECENT mtime: not stale, so we must
    # wait, time out, warn, and proceed anyway.
    with open(lock_path, "w") as handle:
        handle.write("12345 recent\n")
    now = time.time()
    os.utime(lock_path, (now, now))

    warnings = []
    monkeypatch.setattr(
        LOCK.UI, "vprint", lambda level, *args: warnings.append(args)
    )

    entered = False
    with LOCK.hold_file_lock(lock_target, timeout_seconds=0.5) as acquired:
        entered = True
        # We proceeded without the lock: the context still yields, but the
        # value reports the miss.
        assert acquired is False

    assert entered, "the context manager must yield even on timeout"
    # We never owned the lock, so the foreign holder's file is untouched.
    assert os.path.exists(lock_path)
    assert warnings, "a timeout must emit one warning"
    assert any(lock_path in [str(item) for item in args] for args in warnings)


# =====================================================================
# ensure_base_tile serialisation (two concurrent threads, one lock key)
# =====================================================================
class _ConcurrencyProbeStrategy:
    """A base strategy whose ensure_tile records overlapping entries.

    Shared class-level state records how many callers are inside
    ``ensure_tile`` at once; a peak above one would prove the lock failed
    to serialise the two tiles.
    """

    state_lock = threading.Lock()
    active = 0
    peak_active = 0
    call_count = 0

    @classmethod
    def reset(cls):
        cls.active = 0
        cls.peak_active = 0
        cls.call_count = 0

    def ensure_tile(self, definition, lat, lon, verbose=True):
        with _ConcurrencyProbeStrategy.state_lock:
            _ConcurrencyProbeStrategy.active += 1
            _ConcurrencyProbeStrategy.call_count += 1
            _ConcurrencyProbeStrategy.peak_active = max(
                _ConcurrencyProbeStrategy.peak_active,
                _ConcurrencyProbeStrategy.active,
            )
        # Hold the critical section open long enough for a second thread to
        # collide were the lock absent.
        time.sleep(0.3)
        with _ConcurrencyProbeStrategy.state_lock:
            _ConcurrencyProbeStrategy.active -= 1
        return 1


def test_ensure_base_tile_serializes_concurrent_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    _ConcurrencyProbeStrategy.reset()

    fake_definition = {"code": "TESTBASE", "access_strategy": "probe"}
    monkeypatch.setattr(
        INSETS,
        "resolve_base_definition",
        lambda lat, lon, selector="auto": fake_definition,
    )
    monkeypatch.setattr(
        INSETS, "ACCESS_STRATEGIES", {"probe": _ConcurrencyProbeStrategy}
    )

    lat, lon = 36, -87
    results = []
    results_lock = threading.Lock()

    def call():
        outcome = INSETS.ensure_base_tile("auto", lat, lon)
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    # Both tiles ran the strategy and each got the normal success value, but
    # they never overlapped: the file lock serialised the shared-cache
    # critical section.
    assert results == [1, 1]
    assert _ConcurrencyProbeStrategy.call_count == 2
    assert _ConcurrencyProbeStrategy.peak_active == 1
    # The per-key lock file was cleaned up afterward.
    assert not any(
        name.startswith(".lock_TESTBASE_")
        for name in os.listdir(
            os.path.join(str(tmp_path), FNAMES.round_latlon(lat, lon))
        )
    )
