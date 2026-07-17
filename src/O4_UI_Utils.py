import os
import sys
import time

import O4_File_Names as FNAMES

verbosity = 1
red_flag = False
is_working = False
cleaning_level = 1
gui = None
# The active o4_engine.EngineSession, set by the session itself on
# construction (docs/specs/engine-protocol-multi-gui.md §6).  When set,
# the module functions below route to it; the legacy ``gui`` attribute
# stays as the Tkinter fallback.  This module never imports o4_engine —
# attribute registration keeps the import graph acyclic and core
# pipeline modules toolkit-free.
engine_session = None
log = True
total_elapsed = 0.0


################################################################################
def subprocess_env():
    """Return a subprocess environment with OBJC_DISABLE_INITIALIZE_FORK_SAFETY
    set on macOS to suppress CoreFoundation fork-safety warnings."""
    env = os.environ.copy()
    if "dar" in sys.platform:
        env["OBJC_DISABLE_INITIALIZE_FORK_SAFETY"] = "YES"
    return env


def external_tool_keyword_arguments():
    """Keyword arguments for every external build-tool launch
    (Triangle4XP, DDSTool, gdal_translate, gdalwarp, 7z, ...).

    ``close_fds=False`` makes CPython take its ``posix_spawn()`` path
    instead of ``fork()`` + ``exec()``.  This is load-bearing on macOS:
    once GDAL has warped anything, its bundled PROJ has registered a
    ``pthread_atfork`` handler that closes the ``proj.db`` sqlite handles
    inside the forked child, and that handler segfaults in ``os_log``
    before ``exec`` ever runs (diagnosed 2026-07-16 from the
    ``Python-*.ips`` crash reports: ``fork`` ->
    ``_pthread_atfork_child_handlers`` ->
    ``SQLiteHandleCache::invalidateHandles`` -> SIGSEGV).  The symptom
    was Triangle4XP and texture conversions "failing" instantly with no
    output, only in engine builds that had performed GDAL warps first.

    Not sweeping file descriptors is safe: Python file descriptors are
    non-inheritable by default (PEP 446).  ``posix_spawn`` is only taken
    when ``cwd`` is None and standard streams are not low file
    descriptors -- true for every pipeline tool call.
    """
    return {"env": subprocess_env(), "close_fds": False}


################################################################################
def progress_bar(nbr, percentage, message=None):
    if engine_session is not None:
        try:
            engine_session.legacy_progress(nbr, int(percentage))
        except Exception:
            pass
        return
    if gui:
        gui.pgrbv[nbr].set(percentage)


################################################################################
def auto_patch_begin(icaos):
    """(Re)open the auto-patch progress window with one row per airport in
    ``icaos``.  No-op without a GUI (command-line builds / the test suite).
    Only enqueues onto the GUI's thread-safe queue — safe to call from the
    build worker thread; the actual widgets are created on the Tk main
    thread."""
    if engine_session is not None:
        try:
            engine_session.autopatch_begin(icaos)
        except Exception:
            pass
        return
    if gui:
        try:
            gui.autopatch_begin(icaos)
        except Exception:
            pass


################################################################################
def auto_patch_progress(icao, done, total, label, status="run",
                        eta_total_s=None):
    """Update one airport's row in the auto-patch progress window.

    ``done``/``total`` drive the progress bar (percent = done/total); ``label``
    is the small detail line under the bar.  ``status`` is ``"run"`` for a
    phase transition, ``"done"`` when the airport finished (bar → 100 %), or
    ``"fail"`` when its build failed (row flagged red).  ``eta_total_s`` is
    the build's current best TOTAL-time estimate in seconds (complexity
    prior refined per phase, ``auto_patch.build_time_model``) — the window
    blends it with its own elapsed-time extrapolation for the "About m:ss
    remaining" label.  No-op without a GUI and never raises — progress is
    cosmetic."""
    if engine_session is not None:
        try:
            engine_session.autopatch_event(icao, done, total, label, status,
                                           eta_total_s)
        except Exception:
            pass
        return
    if gui:
        try:
            gui.autopatch_event(icao, done, total, label, status,
                                eta_total_s)
        except Exception:
            pass


################################################################################
def vprint(min_verbosity, *args):
    if verbosity >= min_verbosity:
        print(*args)


################################################################################
def logprint(*args):
    try:
        f = open(FNAMES.data_path("Ortho4XP.log"), "a")
        f.write(
            time.strftime("%c")
            + " | "
            + " ".join([str(x) for x in args])
            + "\n"
        )
        f.close()
    except:
        pass


################################################################################
def lvprint(min_verbosity, *args):
    if verbosity >= min_verbosity:
        print(*args)
    if log:
        logprint(*args)


################################################################################
def bug_report(*args):
    logprint(
        "An internal error occured. Please file a bug with lat/lon and cfg"
    )
    if args:
        logprint(*args)


################################################################################
def exit_message_and_bottom_line(*args):
    global is_working
    if not args:
        args = ("Process interrupted",)
    if args[0]:
        logprint(*args)
        print(*args)
    print(
        "_____________________________________________________________"
        + "____________________________________"
    )
    is_working = False


################################################################################
def reset_total_elapsed():
    global total_elapsed
    total_elapsed = 0.0


################################################################################
def total_bottom_line(lat, lon):
    print(
        "\nTile "
        + FNAMES.short_latlon(lat, lon)
        + " completed in "
        + nicer_timer(total_elapsed)
        + "."
    )
    print(
        "_____________________________________________________________"
        + "____________________________________"
    )


################################################################################
def timings_and_bottom_line(tinit):
    global is_working, total_elapsed
    elapsed = time.time() - tinit
    total_elapsed += elapsed
    print("\nCompleted in " + nicer_timer(elapsed) + ".")
    print(
        "_____________________________________________________________"
        + "____________________________________"
    )
    is_working = False


################################################################################
def human_print(num, suffix=""):
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < 1024.0:
            return "{:.1f}{}{}".format(num, unit, suffix)
        num /= 1024.0
    return "{:.1f}{}{}".format(num, "Y", suffix)


################################################################################
def nicer_timer(elapsed):
    out_string = ""
    hours = elapsed // 3600
    if hours:
        elapsed -= 3600 * hours
        out_string += str(int(hours)) + "h"
    minutes = elapsed // 60
    if hours or minutes:
        elapsed -= 60 * minutes
        out_string += str(int(minutes)) + "m"
    elapsed = "{:.2f}".format(elapsed) if not out_string else int(elapsed)
    out_string += str(elapsed) + "sec"
    return out_string
