"""Wall-clock sampling profiler for a whole tile build (steps 1-4:
vector, mesh, masks, imagery/DSF).

Companion to ``tools/profile_airport_build.py`` (which profiles the
auto_patch pipeline only).  Runs the same initialisation as
``tools/run_tile_build.py`` — provider dictionaries, per-tile config —
then executes the four build steps while a background thread samples
EVERY Python thread's stack through ``sys._current_frames()`` each
``--interval`` seconds (default 0.05 s).  Because it samples wall time,
step totals match the production ``~/.ortho4xp/tile_build_times``
numbers; parallel stages (imagery download and convert workers, the
DSF writer thread, the vector-step OSM prefetch thread) report
THREAD-seconds, which can exceed wall seconds.

Time spent inside external binaries (Triangle4XP, the mesh sorter)
appears as the main thread blocked reading the subprocess pipe — it is
reported under that call site, not lost.

Report sections, written to ``--out``
(default /tmp/tile_<short_latlon>_profile.txt):
  1. Exact wall seconds per step (timed around each step call).
  2. Per step: top functions by inclusive and by leaf (self-time)
     thread-seconds, main thread and worker threads listed separately.

Usage:
    venv/bin/python tools/profile_tile_build.py <lat> <lon>
        [--build-dir DIR] [--provider CODE] [--zl N]
        [--steps vector,mesh,masks,imagery] [--interval 0.05]
        [--out PATH]
"""

import argparse
import collections
import os
import sys
import threading
import time

os.environ.setdefault("O4_LOG_VERBOSITY", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "src"))

STEP_ORDER = ("vector", "mesh", "masks", "imagery")


def _short_path(filename):
    if filename.startswith(ROOT):
        return os.path.relpath(filename, ROOT)
    marker = os.sep + "site-packages" + os.sep
    if marker in filename:
        return "site-packages/" + filename.split(marker, 1)[1]
    return filename


class AllThreadSampler(threading.Thread):
    """Samples every live Python thread's stack until ``stop()``.

    Counters are keyed by (step, role, function) where role is "main"
    or "worker"; one count = one thread observed there for one sample
    tick, so counts convert to THREAD-seconds via the per-sample
    duration.
    """

    def __init__(self, main_thread_id, current_step, interval):
        super().__init__(daemon=True)
        self.main_thread_id = main_thread_id
        self.current_step = current_step  # one-element mutable list
        self.interval = interval
        self.ticks = 0
        self.inclusive = collections.Counter()  # (step, role, fn) -> n
        self.leaf = collections.Counter()       # (step, role, site) -> n
        self.thread_ticks = collections.Counter()  # (step, role) -> n
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        own_id = threading.get_ident()
        while not self._stop_event.is_set():
            step = self.current_step[0]
            if step is not None:
                for thread_id, frame in sys._current_frames().items():
                    if thread_id == own_id:
                        continue
                    role = ("main" if thread_id == self.main_thread_id
                            else "worker")
                    self._record(step, role, frame)
                self.ticks += 1
            time.sleep(self.interval)

    def _record(self, step, role, frame):
        self.thread_ticks[(step, role)] += 1
        seen = set()
        leaf_site = None
        depth = 0
        while frame is not None and depth < 200:
            code = frame.f_code
            short = _short_path(code.co_filename)
            key = f"{short}:{code.co_name}"
            if leaf_site is None:
                leaf_site = f"{short}:{frame.f_lineno} {code.co_name}"
            if key not in seen:
                seen.add(key)
                self.inclusive[(step, role, key)] += 1
            frame = frame.f_back
            depth += 1
        if leaf_site is not None:
            self.leaf[(step, role, leaf_site)] += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lat", type=int)
    parser.add_argument("lon", type=int)
    parser.add_argument("--build-dir", default="",
                        help="custom build directory (as in run_tile_build; "
                             "the tile's own zOrtho4XP_... dir is normalized "
                             "to its parent so neighbor lookups keep working)")
    parser.add_argument("--provider", default="",
                        help="override default_website (else tile config)")
    parser.add_argument("--zl", type=int, default=0,
                        help="override default_zl (else tile config)")
    parser.add_argument("--steps", default="vector,mesh,masks,imagery",
                        help="comma list, subset of vector,mesh,masks,imagery")
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = [s for s in steps if s not in STEP_ORDER]
    if unknown:
        raise SystemExit(f"unknown steps: {unknown}")

    import O4_File_Names as FNAMES
    import O4_UI_Utils as UI  # noqa: F401  (imported for side effects)

    sys.path.append(FNAMES.Provider_dir)
    import O4_Imagery_Utils as IMG
    import O4_Vector_Map as VMAP
    import O4_Mesh_Utils as MESH
    import O4_Mask_Utils as MASK
    import O4_Tile_Utils as TILE
    import O4_Config_Utils as CFG

    out_path = args.out or "/tmp/tile_%s_profile.txt" % FNAMES.short_latlon(
        args.lat, args.lon)

    IMG.initialize_extents_dict()
    IMG.initialize_color_filters_dict()
    IMG.initialize_providers_dict()
    IMG.initialize_combined_providers_dict()

    tile = CFG.Tile(
        args.lat, args.lon,
        FNAMES.normalize_custom_build_dir(args.lat, args.lon,
                                          args.build_dir),
    )
    tile.read_from_config()
    if args.provider:
        tile.default_website = args.provider
    if args.zl:
        tile.default_zl = args.zl
    print("build directory:", tile.build_dir)
    print("default_website:", tile.default_website,
          "default_zl:", tile.default_zl)
    if not tile.default_website:
        raise SystemExit("empty default_website — pass --provider")

    step_fn = {
        "vector": VMAP.build_poly_file,
        "mesh": MESH.build_mesh,
        "masks": MASK.build_masks,
        "imagery": TILE.build_tile,
    }

    current_step = [None]
    sampler = AllThreadSampler(
        threading.get_ident(), current_step, args.interval)
    sampler.start()

    step_wall = {}
    t_run = time.time()
    for step in STEP_ORDER:
        if step not in steps:
            continue
        print(f"=== step {step} ===", flush=True)
        current_step[0] = step
        t0 = time.time()
        result = step_fn[step](tile)
        step_wall[step] = time.time() - t0
        current_step[0] = None
        print(f"=== step {step} done in {step_wall[step]:.1f} s "
              f"(result {result}) ===", flush=True)
        if result == 0:
            print("step failed — stopping here")
            break
    total_wall = time.time() - t_run
    sampler.stop()
    sampler.join(timeout=2.0)

    # One sampler tick observed every live thread once; convert counts to
    # thread-seconds through the measured tick duration.
    per_tick = total_wall / max(sampler.ticks, 1)

    def secs(n):
        return n * per_tick

    lines = [
        "tile %s build profile: %.1f s wall, %d sample ticks "
        "(%.1f ms/tick)" % (
            FNAMES.short_latlon(args.lat, args.lon), total_wall,
            sampler.ticks, per_tick * 1000.0),
        "provider %s ZL%s, build dir %s" % (
            tile.default_website, tile.default_zl, tile.build_dir),
        "",
        "== Wall seconds per step (exact) ==",
    ]
    for step in STEP_ORDER:
        if step in step_wall:
            lines.append("  %8.1f s  %s" % (step_wall[step], step))

    for step in STEP_ORDER:
        if step not in step_wall:
            continue
        for role in ("main", "worker"):
            ticks = sampler.thread_ticks.get((step, role), 0)
            if not ticks:
                continue
            lines.append("")
            lines.append("== %s / %s threads: %.1f thread-seconds ==" % (
                step, role, secs(ticks)))
            lines.append("  -- top 30 inclusive --")
            rows = [(n, key) for (s, r, key), n in sampler.inclusive.items()
                    if s == step and r == role]
            for n, key in sorted(rows, reverse=True)[:30]:
                if secs(n) < 1.0:
                    break
                lines.append("  %8.1f s  %s" % (secs(n), key))
            lines.append("  -- top 20 leaf (self-time) --")
            rows = [(n, site) for (s, r, site), n in sampler.leaf.items()
                    if s == step and r == role]
            for n, site in sorted(rows, reverse=True)[:20]:
                if secs(n) < 1.0:
                    break
                lines.append("  %8.1f s  %s" % (secs(n), site))

    report = "\n".join(lines) + "\n"
    with open(out_path, "w") as handle:
        handle.write(report)
    print(report)
    print("report written to", out_path)


if __name__ == "__main__":
    main()
