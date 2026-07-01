import os
import sys
import time

import O4_File_Names as FNAMES

verbosity = 1
red_flag = False
is_working = False
cleaning_level = 1
gui = None
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


################################################################################
def progress_bar(nbr, percentage, message=None):
    if gui:
        gui.pgrbv[nbr].set(percentage)


################################################################################
def auto_patch_begin(icaos):
    """(Re)open the auto-patch progress window with one row per airport in
    ``icaos``.  No-op without a GUI (command-line builds / the test suite).
    Only enqueues onto the GUI's thread-safe queue — safe to call from the
    build worker thread; the actual widgets are created on the Tk main
    thread."""
    if gui:
        try:
            gui.autopatch_begin(icaos)
        except Exception:
            pass


################################################################################
def auto_patch_progress(icao, done, total, label, status="run"):
    """Update one airport's row in the auto-patch progress window.

    ``done``/``total`` drive the progress bar (percent = done/total); ``label``
    is the small detail line under the bar.  ``status`` is ``"run"`` for a
    phase transition, ``"done"`` when the airport finished (bar → 100 %), or
    ``"fail"`` when its build failed (row flagged red).  No-op without a GUI
    and never raises — progress is cosmetic."""
    if gui:
        try:
            gui.autopatch_event(icao, done, total, label, status)
        except Exception:
            pass


################################################################################
def vprint(min_verbosity, *args):
    if verbosity >= min_verbosity:
        print(*args)


################################################################################
def logprint(*args):
    try:
        f = open(FNAMES.resource_path("Ortho4XP.log"), "a")
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
