"""Failure-path tests for the imagery step (``O4_Tile_Utils``).

Regression 2026-07-15: a stale tile config with an empty ``default_website``
built a whole tile with ZERO textures while reporting success (and recording
a timing sample) — every download printed "Unknown provider" and was silently
dropped.  These tests pin the guards added for that:

1. ``build_tile`` refuses an unknown or empty imagery provider up front,
   before it rewrites the tile config.
2. ``download_textures`` retries each texture three times, then counts it
   into the caller-supplied ``stats`` dict instead of dropping it silently.

Headless: stub tile objects, ``tmp_path`` build directories, no network.
"""

import os
import queue
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import O4_File_Names as FNAMES  # noqa: E402
import O4_Tile_Utils as TILE  # noqa: E402
import O4_UI_Utils as UI  # noqa: E402


class _StubTile:
    """Just enough of ``O4_Config_Utils.Tile`` for ``build_tile``'s
    pre-flight checks."""

    def __init__(self, build_dir, default_website):
        self.lat = 60
        self.lon = 5
        self.build_dir = build_dir
        self.default_website = default_website
        self.grouped = False
        self.config_writes = 0

    def write_to_config(self):
        self.config_writes += 1


def _make_mesh_file(build_dir):
    os.makedirs(build_dir, exist_ok=True)
    with open(FNAMES.mesh_file(build_dir, 60, 5), "w") as handle:
        handle.write("stub mesh\n")


def test_build_tile_rejects_unknown_provider(tmp_path, monkeypatch):
    """An unknown provider fails the step before the tile config is
    rewritten (so the bad value is never persisted)."""
    monkeypatch.setattr(UI, "is_working", False)
    build_dir = str(tmp_path)
    _make_mesh_file(build_dir)
    tile = _StubTile(build_dir, default_website="NO_SUCH_PROVIDER")

    assert TILE.build_tile(tile) == 0
    assert tile.config_writes == 0
    assert UI.is_working is False  # the step released the working flag


def test_build_tile_rejects_empty_provider(tmp_path, monkeypatch):
    """The empty-string provider from a stale tile config is refused."""
    monkeypatch.setattr(UI, "is_working", False)
    build_dir = str(tmp_path)
    _make_mesh_file(build_dir)
    tile = _StubTile(build_dir, default_website="")

    assert TILE.build_tile(tile) == 0
    assert tile.config_writes == 0


def test_download_textures_counts_permanent_failures(monkeypatch):
    """A texture that fails all three attempts lands in ``stats['failed']``
    and never reaches the convert queue; the queue still drains cleanly."""
    attempts = []
    monkeypatch.setattr(
        TILE.IMG, "build_jpeg_ortho",
        lambda tile, *attrs: attempts.append(attrs) or 0)
    monkeypatch.setattr(UI, "red_flag", False)

    download_queue = queue.Queue()
    convert_queue = queue.Queue()
    download_queue.put((4096, 2725, 16, "BI"))
    stats = {}

    result = TILE.download_textures(
        None, download_queue, convert_queue, workers=2, stats=stats)

    assert result == 1  # not interrupted — the caller judges the counts
    assert stats == {"done": 0, "failed": 1}
    assert len(attempts) == 3
    assert convert_queue.empty()


def test_download_textures_counts_successes(monkeypatch):
    """Successful downloads land in ``stats['done']`` and are queued for
    conversion."""
    monkeypatch.setattr(
        TILE.IMG, "build_jpeg_ortho", lambda tile, *attrs: 1)
    monkeypatch.setattr(UI, "red_flag", False)

    download_queue = queue.Queue()
    convert_queue = queue.Queue()
    download_queue.put((4096, 2725, 16, "BI"))
    download_queue.put((4112, 2725, 16, "BI"))
    stats = {}

    result = TILE.download_textures(
        None, download_queue, convert_queue, workers=2, stats=stats)

    assert result == 1
    assert stats == {"done": 2, "failed": 0}
    assert convert_queue.qsize() == 2


def test_download_textures_full_width_despite_siblings(monkeypatch):
    """Sibling tiles no longer throttle the download pool (2026-07-17
    lean-on-the-operating-system ruling): the queue opens at the full
    Auto width immediately — no mid-step raise machinery — so a cold
    download never runs at a shared rate (the historic live case: a
    17-minute ZL18 download at half throughput while its sibling had
    been done for 16 minutes)."""
    from O4_Parallel_Utils import PARALLEL_SIBLINGS_ENVIRONMENT_KEY

    monkeypatch.setenv(PARALLEL_SIBLINGS_ENVIRONMENT_KEY, "2")
    monkeypatch.setattr(TILE, "max_download_slots", 0)  # Auto
    monkeypatch.setattr(UI, "red_flag", False)

    launches = []
    real_launch = TILE.parallel_launch

    def recording_launch(task, task_queue, count, progress=None):
        launches.append(count)
        return real_launch(task, task_queue, count, progress)

    monkeypatch.setattr(TILE, "parallel_launch", recording_launch)
    monkeypatch.setattr(
        TILE.IMG, "build_jpeg_ortho", lambda tile, *attrs: 1)

    download_queue = queue.Queue()
    convert_queue = queue.Queue()
    for index in range(6):
        download_queue.put((4096 + 16 * index, 2725, 16, "BI"))
    stats = {}
    result = TILE.download_textures(
        None, download_queue, convert_queue, stats=stats)

    assert result == 1
    assert stats == {"done": 6, "failed": 0}
    assert launches == [2], (
        "the pool must open at full Auto width in one launch")
