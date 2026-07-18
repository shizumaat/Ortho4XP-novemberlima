"""Fast snapshot-replay harness for the adjacent-ground graded-strip stage.

The full ``full_airport_build.py`` cycle is ~3-8 minutes, far over the
five-minute iteration ceiling.  The site-2 residual divergence
(60.7208676,-135.0790956) is minted entirely inside
``adjacent_ground.emit_adjacent_ground_bands`` (the LATERAL banded
graded-strip emitter) and its post-emission conformance weld, so this
harness snapshots the pipeline state ONCE at the point just before that
emitter runs, then replays ONLY that stage plus the final
epsilon-wedge weld and the post-weld residual report against the frozen
snapshot.  Iteration on the emitter source is then seconds-fast.

The snapshot-and-replay pattern mirrors ``O4_DUMP_PRE_TUNNEL_LAYOUT``
(finalize.py): a monkeypatch intercepts the emitter, pickles
``(layout, dem, tile_lat, tile_lon, runways)`` before the emitter
mutates anything, and raises a sentinel to abort the (now unneeded) rest
of the build.  The snapshot is code-independent input data, so editing
``adjacent_ground.py`` only changes the REPLAY, never the snapshot — one
snapshot serves any number of replays.

Usage:
    # 1. Capture the snapshot once (~3 min, one full build up to the stage):
    venv/bin/python tools/adjacent_ground_replay.py snapshot [ICAO]

    # 2. Replay the stage in seconds after each source edit:
    venv/bin/python tools/adjacent_ground_replay.py replay [ICAO]

Replay prints the post-weld residual T-junctions/crossings (the same set
the pipeline's residual report prints) and specifically whether the
site-2 divergence survives.
"""
import math
import os
import pickle
import sys

os.environ.setdefault("O4_LOG_VERBOSITY", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests"),
           os.path.join(ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from conftest import xplane_root                              # noqa: E402

# Site 2: the pre-to_osm graded_strip<->graded_strip residual T-junction.
SITE2_LATLON = (60.7208676, -135.0790956)


def _snapshot_path(icao):
    return f"/tmp/{icao}_adjacent_ground_presnap.pkl"


class _StopAfterSnapshot(Exception):
    pass


def do_snapshot(icao):
    import auto_patch.adjacent_ground as ag
    from auto_patch.pipeline import build_airport_pavement

    snap = _snapshot_path(icao)
    real_emit = ag.emit_adjacent_ground_bands

    def _hook(layout, dem, tile_lat, tile_lon, source_runways=None):
        # Some pipeline stages cache unpicklable objects (shapely
        # PREPARED geometries) on the layout.  Drop those attributes
        # from the snapshot — they are derived caches the consumers
        # rebuild on demand — and report what was dropped so a replay
        # divergence can be traced back here.
        dropped = []
        for name in list(vars(layout)):
            try:
                pickle.dumps(getattr(layout, name),
                             protocol=pickle.HIGHEST_PROTOCOL)
            except Exception:
                dropped.append(name)
                delattr(layout, name)
        if dropped:
            print(f"[snapshot] dropped unpicklable layout attribute(s): "
                  f"{dropped}")
        with open(snap, "wb") as fh:
            pickle.dump((layout, dem, tile_lat, tile_lon, source_runways),
                        fh, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"[snapshot] wrote {snap} "
              f"({len(layout.shapes)} shapes, before emission)")
        raise _StopAfterSnapshot()

    ag.emit_adjacent_ground_bands = _hook
    try:
        build_airport_pavement(icao, xplane_root(), compute_elevations=True)
    except _StopAfterSnapshot:
        pass
    finally:
        ag.emit_adjacent_ground_bands = real_emit
    print("[snapshot] done")


def _site2_local_metres(layout):
    """Invert ``layout.m_to_ll`` locally to place site 2 in metres."""
    import numpy as np
    lat0, lon0 = layout.m_to_ll(0.0, 0.0)
    latx, lonx = layout.m_to_ll(100.0, 0.0)
    laty, lony = layout.m_to_ll(0.0, 100.0)
    a = np.array([[(lonx - lon0) / 100.0, (lony - lon0) / 100.0],
                  [(latx - lat0) / 100.0, (laty - lat0) / 100.0]])
    b = np.array([SITE2_LATLON[1] - lon0, SITE2_LATLON[0] - lat0])
    x, y = np.linalg.solve(a, b)
    return float(x), float(y)


def do_replay(icao):
    from auto_patch.adjacent_ground import emit_adjacent_ground_bands
    from auto_patch.conformance import (enforce_conformance,
                                        find_conformance_violations)

    snap = _snapshot_path(icao)
    with open(snap, "rb") as fh:
        layout, dem, tile_lat, tile_lon, runways = pickle.load(fh)

    n_ag = emit_adjacent_ground_bands(
        layout, dem, tile_lat, tile_lon, source_runways=runways)
    print(f"[replay] emitted {n_ag} adjacent-ground polygon(s)")

    # Mirror the pipeline tail: final epsilon-wedge weld, then the
    # post-weld residual report at tol=0.005.
    enforce_conformance(layout, tol=0.01, include_overlay_refs=True)
    tj, cr = find_conformance_violations(layout.shapes, tol=0.005)

    # Optional: write the post-stage patch so downstream probes (coincident
    # audit, per-site node inspection) can run against the interned OSM.
    out = os.environ.get("O4_REPLAY_OSM_OUT")
    if out:
        layout.to_osm(out)
        print(f"[replay] wrote {out}")

    sx, sy = _site2_local_metres(layout)
    print(f"[replay] post-weld residual: {len(tj)} T-junction(s), "
          f"{len(cr)} crossing(s)")
    site2_hit = None
    for x, y in tj + cr:
        la, lo = layout.m_to_ll(x, y)
        d = math.hypot(x - sx, y - sy)
        tag = " <== SITE 2" if d < 0.05 else ""
        print(f"      @ {la:.7f},{lo:.7f}  (d_site2={d * 1000:.1f} mm){tag}")
        if d < 0.05 and site2_hit is None:
            site2_hit = (la, lo)
    print(f"[replay] SITE-2 present: {site2_hit is not None}")
    return site2_hit is not None


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
