import os
import threading
import O4_UI_Utils as UI


# ---------------------------------------------------------------------------
# Machine-aware slot resolution ("0 = Auto" in the slot settings,
# docs/specs/parallel-tile-builds.md §2).  Each knob gets the formula its
# bottleneck warrants: tile builds bind on MEMORY as much as cores, DDS
# conversion is CPU-bound, downloads are network-bound (cores irrelevant).
#
# Owner ruling 2026-07-17: MACHINE resources (processor, memory pressure
# short of the mesh cliff) are the operating system's to arbitrate — six
# hand-launched Ortho4XP copies plus X-Plane always time-sliced fine.
# Per-tile pools therefore run at FULL machine width regardless of how
# many tiles build concurrently.  Deliberate throttles remain only where
# the operating system cannot help: REMOTE SERVER goodwill (the
# orchestrator's OpenStreetMap/imagery request caps, the bathymetry cell
# fetch below) and the multi-gigabyte mesh working-set cliff (the
# orchestrator's memory admission gate).
# ---------------------------------------------------------------------------
def machine_core_count() -> int:
    """Logical processor count (4 when the platform will not say)."""
    return os.cpu_count() or 4


def machine_memory_gigabytes() -> float:
    """Total physical memory in gigabytes (8.0 when undeterminable).

    Standard-library only: ``os.sysconf`` on macOS/Linux, the Windows
    ``GlobalMemoryStatusEx`` call through ``ctypes`` elsewhere.
    """
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        if page_size > 0 and page_count > 0:
            return page_size * page_count / (1024.0 ** 3)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        import ctypes

        class _MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32),
                ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys / (1024.0 ** 3)
    except Exception:
        pass
    return 8.0


# Set by the parallel-build scheduler in every worker child's
# environment (and refreshed by the "siblings" broadcast as tiles
# finish): how many sibling tiles are building at once.  Since the
# 2026-07-17 lean-on-the-operating-system ruling, only NETWORK fetchers
# that hit small remote hosts consult it (the bathymetry cell fetch) —
# processor-bound pools run at full width regardless.
PARALLEL_SIBLINGS_ENVIRONMENT_KEY = "O4_PARALLEL_BUILD_SIBLINGS"


def parallel_sibling_count() -> int:
    """How many tiles are building concurrently (1 outside a parallel run)."""
    try:
        return max(1, int(os.environ.get(
            PARALLEL_SIBLINGS_ENVIRONMENT_KEY, "1")))
    except ValueError:
        return 1


def effective_build_slots(configured) -> int:
    """Concurrent tile builds for a ``max_build_slots`` value (0 = Auto).

    Auto weighs BOTH constraints: each concurrent tile runs its own
    multi-threaded downloads and conversions (cores divide by three) and
    carries its own working memory (gigabytes divide by six).  The
    memory divisor is soft on purpose — modern systems (macOS memory
    compression, fast solid-state swap) degrade gracefully rather than
    fail, so it guards against the paging performance cliff of actively
    swept rasters, not out-of-memory.  Auto's ceiling of six keeps the
    default a good citizen toward the OpenStreetMap and imagery servers;
    an EXPLICIT setting may go higher — big-memory machines can carry
    more, at the price of occasional server throttling (downloads retry).
    """
    configured = int(configured or 0)
    if configured > 0:
        return configured
    by_cores = machine_core_count() // 3
    by_memory = int(machine_memory_gigabytes() // 6)
    return max(1, min(6, by_cores, by_memory))


def effective_convert_slots(configured) -> int:
    """Parallel DDS conversions for a ``max_convert_slots`` value (0 = Auto).

    Conversion is CPU-bound: Auto uses every core but two (floor two,
    cap sixteen) — at FULL width even when several tiles build
    concurrently (2026-07-17 ruling: the operating system time-slices
    competing pools fine, and the last running tile inherits the whole
    machine with no hand-off machinery).
    """
    configured = int(configured or 0)
    if configured > 0:
        return configured
    return max(2, min(16, machine_core_count() - 2))


def effective_download_slots(configured) -> int:
    """Parallel orthophoto constructions for ``max_download_slots``
    (0 = Auto).

    Downloads are network-bound, so the core count is irrelevant: Auto
    is two (each orthophoto already runs sixteen request threads),
    whether or not sibling tiles build concurrently — commercial
    imagery hosts handle a handful of parallel streams comfortably, and
    the orchestrator's imagery class cap already bounds how many tiles
    download at once.  Users on external drives may prefer an explicit
    one — the historic default — per the long-standing warning in the
    setting's hint.
    """
    configured = int(configured or 0)
    if configured > 0:
        return configured
    return 2

################################################################################
class parallel_worker(threading.Thread):
    def __init__(self, task, queue, progress=None, success=[1]):
        threading.Thread.__init__(self)
        self._task = task
        self._queue = queue
        self._progress = progress
        self._success = success

    def run(self):
        while True:
            args = self._queue.get()
            if isinstance(args, str) and args == "quit":
                try:
                    UI.progress_bar(self._progress["bar"], 100)
                except:
                    pass
                return 1
            try:
                self._success[0] = self._task(*args) and self._success[0]
            except Exception:
                # A worker crash must fail the step loudly: swallowing it
                # here made Step 2.5 report "normal exit" with zero masks
                # built (custom-extent NameError, 2026-07-16).
                import traceback
                UI.lvprint(0, "ERROR: a worker task crashed:\n"
                           + traceback.format_exc())
                self._success[0] = 0
            if self._progress:
                self._progress["done"] += 1
                UI.progress_bar(
                    self._progress["bar"],
                    int(
                        100
                        * self._progress["done"]
                        / (self._progress["done"] + self._queue.qsize())
                    ),
                )
            if UI.red_flag:
                return 0

################################################################################
def parallel_execute(task, queue, nbr_workers, progress=None):
    workers = []
    success = [1]
    for _ in range(nbr_workers):
        queue.put("quit")
        worker = parallel_worker(task, queue, progress, success)
        worker.start()
        workers.append(worker)
    for worker in workers:
        worker.join()
    if UI.red_flag:
        return 0
    return success[0]


################################################################################
def parallel_launch(task, queue, nbr_workers, progress=None):
    workers = []
    for _ in range(nbr_workers):
        worker = parallel_worker(task, queue, progress)
        worker.start()
        workers.append(worker)
    return workers

################################################################################
def parallel_join(workers):
    for worker in workers:
        worker.join()