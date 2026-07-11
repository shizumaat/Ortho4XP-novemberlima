"""Fast snapshot-replay harness for the adjacent-ground COVERAGE closure
(Slice B stage B3 order 3 — coverage-gap closure).

The coverage gap spans three stages — the PRE-SOLVE band construct march
(``construct_adjacent_ground_presolve``), ``per_surface_solve`` (which
values the admitted zone nodes), and the post-solve emit
(``emit_adjacent_ground_bands``, whose resampler reads those solved
values).  The existing ``adjacent_ground_replay.py`` snapshots AFTER the
solve (right before the emitter), so it cannot exercise a CONSTRUCT change.
This harness snapshots ONE step earlier — right before
``construct_adjacent_ground_presolve`` — then replays
construct + solve + emit against the frozen snapshot, so iteration on the
construct march (and its worst-case reach-band coverage) is seconds-fast
(the solve is ~2.4 s; the expensive legacy clearance emit + bridge
classifier that dominate a full build are skipped).

The snapshot is code-independent input data captured after the B1
pre-solve skirts and the B2 gap-fill spine store already exist on the
layout, so editing ``adjacent_ground.py`` only changes the REPLAY.

Run with the FULL-ON gate set exported in the shell:
    O4_ONE_SOLVE_TERRAIN=1
    O4_ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT=1
    O4_ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT=1
    O4_ONE_SOLVE_TERRAIN_GAP_FILL_SPINE=1
    O4_ONE_SOLVE_TERRAIN_GRADED_STRIP=1
    O4_ADJACENT_GROUND_END_WRAP=1

Usage:
    venv/bin/python tools/adjacent_ground_coverage_replay.py snapshot [ICAO]
    venv/bin/python tools/adjacent_ground_coverage_replay.py replay   [ICAO]
"""
import math
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

from conftest import xplane_root                              # noqa: E402

# Taxiway-end WRAP ruling site (Noah 2026-07-10).
WRAP_SITE_LATLON = (60.6972471, -135.0608669)


def _snapshot_path(icao):
    return f"/tmp/{icao}_adjacent_ground_preconstruct.pkl"


class _StopAfterSnapshot(Exception):
    pass


def do_snapshot(icao):
    import auto_patch.adjacent_ground as ag
    from auto_patch.pipeline import build_airport_pavement

    snap = _snapshot_path(icao)
    real = ag.construct_adjacent_ground_presolve

    def _hook(layout, dem, tile_lat, tile_lon, source_runways=None):
        with open(snap, "wb") as fh:
            pickle.dump((layout, dem, tile_lat, tile_lon, source_runways),
                        fh, protocol=pickle.HIGHEST_PROTOCOL)
        print(f"[snapshot] wrote {snap} "
              f"({len(layout.shapes)} shapes, before construct)")
        raise _StopAfterSnapshot()

    ag.construct_adjacent_ground_presolve = _hook
    try:
        build_airport_pavement(icao, xplane_root(), compute_elevations=True)
    except _StopAfterSnapshot:
        pass
    finally:
        ag.construct_adjacent_ground_presolve = real
    print("[snapshot] done")


def _site_local_metres(layout, latlon):
    import numpy as np
    lat0, lon0 = layout.m_to_ll(0.0, 0.0)
    latx, lonx = layout.m_to_ll(100.0, 0.0)
    laty, lony = layout.m_to_ll(0.0, 100.0)
    a = np.array([[(lonx - lon0) / 100.0, (lony - lon0) / 100.0],
                  [(latx - lat0) / 100.0, (laty - lat0) / 100.0]])
    b = np.array([latlon[1] - lon0, latlon[0] - lat0])
    x, y = np.linalg.solve(a, b)
    return float(x), float(y)


def do_replay(icao):
    import auto_patch.adjacent_ground as ag
    from shapely.geometry import Point
    from auto_patch.adjacent_ground import (
        construct_adjacent_ground_presolve, emit_adjacent_ground_bands)
    from auto_patch.elevation_per_surface import solve as per_surface_solve
    from auto_patch.layout import ROLE_GRADED_STRIP

    snap = _snapshot_path(icao)
    with open(snap, "rb") as fh:
        layout, dem, tile_lat_i, tile_lon_i, runways = pickle.load(fh)

    t0 = time.time()
    n_shapes = construct_adjacent_ground_presolve(
        layout, dem, tile_lat_i, tile_lon_i, source_runways=runways)
    t_construct = time.time() - t0
    store = getattr(layout, "adjacent_ground_presolve", []) or []
    n_zone_nodes = sum(len(e.get("zone_nodes") or []) for e in store)
    n_bands = sum(len(e["fill"]) + len(e["cut"]) for e in store)
    print(f"[replay] construct: {n_shapes} shape(s), {n_bands} raw band(s), "
          f"{n_zone_nodes} zone node(s), {t_construct:.2f}s")

    t0 = time.time()
    per_surface_solve(layout, icao, dem=dem,
                      tile_lat=tile_lat_i, tile_lon=tile_lon_i)
    t_solve = time.time() - t0
    n_solved = sum(len(e.get("zone_values") or {}) for e in store)
    print(f"[replay] solve: {t_solve:.2f}s, {n_solved} zone value(s) written")

    n_before = len(layout.shapes)
    n_ag = emit_adjacent_ground_bands(
        layout, dem, tile_lat_i, tile_lon_i, source_runways=runways)
    hits = dict(ag._APPARATUS_HITS)
    print(f"[replay] emitted {n_ag} adjacent-ground polygon(s)")
    print("[replay] apparatus hits: "
          + " ".join(f"{k}={hits.get(k, 0)}" for k in ag._APPARATUS_KEYS))

    # Wrap evidence: graded_strip pieces near the ruling site.
    wx, wy = _site_local_metres(layout, WRAP_SITE_LATLON)
    near = []
    for s in layout.shapes[n_before:]:
        if s.role != ROLE_GRADED_STRIP or s.polygon is None:
            continue
        try:
            d = s.polygon.distance(Point(wx, wy))
        except Exception:
            continue
        if d <= 20.0:
            near.append((d, s.polygon.area))
    near.sort()
    print(f"[replay] WRAP site ({wx:.1f},{wy:.1f}) graded_strip pieces "
          f"within 20 m: {len(near)}")
    for d, a in near[:8]:
        print(f"      d={d:.2f} m  area={a:.1f} m2")
    return hits


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
