"""Fast snapshot-replay harness for the interval-edge reach envelope.

Slice B stage B3 prerequisite: the gates-ON gap-fill spine build makes the
main-yield ``feasibility_project`` exhaust its 2400-iteration visit budget
(~27.7 M worklist visits) instead of draining (~30 k gate-OFF).  A full CYXY
build is ~1-3 min, far over the five-minute iteration ceiling, so this harness
snapshots the EXACT inputs of that projection call ONCE (reusing the existing
``O4_DUMP_SOLVE_STATE`` hook in ``route_profile/solve.py``, which pickles
``elev`` / ``joint_edges`` / ``yield_hard`` / ``pad_groups`` right before the
call) and then drives ``feasibility_project`` standalone in seconds.

The snapshot is code-independent input data, so editing ``one_solve.py`` only
changes the REPLAY, never the snapshot — one snapshot serves any number of
replays of the reach-envelope / break-detection fix.

Mirrors the ``tools/adjacent_ground_replay.py`` convention.

Usage:
    # 1. Capture the snapshot once (one gates-ON CYXY build):
    venv/bin/python tools/interval_reach_replay.py snapshot [ICAO]

    # 2. Replay the main-yield projection in seconds after each source edit:
    venv/bin/python tools/interval_reach_replay.py replay [ICAO]

The main-yield call the pipeline makes (route_profile/solve.py) is:
    feasibility_project(elev, joint, yield_hard, force_scalar=True,
                        max_iters=2400, flat_groups=pad_groups or None,
                        broken_out=...)
so the replay reconstructs it verbatim (max_iters/force_scalar are the pinned
pipeline constants).  O4_FP_REENTRY_DEBUG=1 is set for the replay so the
per-kind pop / re-entry counters print.
"""
import os
import pickle
import sys
import time

os.environ.setdefault("O4_LOG_VERBOSITY", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests"),
           os.path.join(ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# The pinned main-yield projection constants (route_profile/solve.py).
_MAX_ITERS = 2400


def _snapshot_path(icao):
    return f"/tmp/{icao}_interval_reach_state.pkl"


def do_snapshot(icao):
    # Gates ON (skirt + gap) so the interval edges are produced, and dump the
    # main-yield projection inputs.  Pinned hash seed for a reproducible build.
    os.environ["O4_ONE_SOLVE_TERRAIN"] = "1"
    os.environ["O4_ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT"] = "1"
    os.environ["O4_ONE_SOLVE_TERRAIN_GAP_FILL_SPINE"] = "1"
    os.environ["O4_DUMP_SOLVE_STATE"] = _snapshot_path(icao)
    os.environ.setdefault("PYTHONHASHSEED", "0")
    # Ground-truth counters from the REAL build (not just the replay).
    os.environ["O4_FP_REENTRY_DEBUG"] = "1"
    os.environ["O4_STEP_DEBUG"] = "1"

    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement

    t0 = time.time()
    build_airport_pavement(icao, xplane_root(), compute_elevations=True)
    print(f"[snapshot] build done in {time.time() - t0:.1f}s; "
          f"dump at {_snapshot_path(icao)}")


def _drive(state, *, label):
    """Drive ``feasibility_project`` on the pickled main-yield inputs and
    report visits / residuals.  Returns ``(rem, bh)``."""
    from auto_patch.elevation_per_surface.route_profile.one_solve import (
        feasibility_project)
    elev = list(state["elev"])
    joint_edges = [tuple(e) for e in state["joint_edges"]]
    yield_hard = set(state["yield_hard"])
    pad_groups = [set(g) for g in state.get("pad_groups", [])] or None
    n_int = sum(1 for e in joint_edges if len(e) >= 4)
    print(f"[{label}] nodes={len(elev)} edges={len(joint_edges)} "
          f"interval_edges={n_int} hard={len(yield_hard)} "
          f"pad_groups={len(pad_groups) if pad_groups else 0}")
    broken: set = set()
    t0 = time.time()
    rem, bh = feasibility_project(
        elev, [{"edges": joint_edges}], yield_hard,
        force_scalar=True, max_iters=_MAX_ITERS,
        flat_groups=pad_groups, broken_out=broken)
    dt = time.time() - t0
    print(f"[{label}] projection {dt:.2f}s  rem={rem} bh={bh} "
          f"broken={len(broken)}")
    return rem, bh


def do_replay(icao):
    snap = _snapshot_path(icao)
    with open(snap, "rb") as fh:
        state = pickle.load(fh)
    os.environ["O4_FP_REENTRY_DEBUG"] = "1"
    os.environ["O4_STEP_DEBUG"] = "1"
    _drive(state, label="replay")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "replay"
    icao = sys.argv[2] if len(sys.argv) > 2 else "CYXY"
    if mode == "snapshot":
        do_snapshot(icao)
    elif mode == "replay":
        do_replay(icao)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
