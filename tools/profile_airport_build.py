"""Wall-clock sampling profiler for a single-airport auto_patch build.

Runs ``build_airport_pavement(ICAO)`` exactly like
``tools/full_airport_build.py`` while a background thread samples the
main thread's stack every ``--interval`` seconds (default 0.02 s).
Because it samples wall time rather than counting calls, it has none of
cProfile's per-call ``tottime`` inflation on hot small functions and
adds <1% overhead, so the attribution matches the production
``~/.ortho4xp/auto_patch_build_times`` phase numbers.

Reports written to ``--out`` (default /tmp/<ICAO>_profile.txt):
  1. Build phase (pipeline step 1-6) per sample, so phase totals can be
     cross-checked against the build-times JSON.
  2. Top call sites inside pipeline.py per phase — "which task is the
     main thread actually inside", attributed to the innermost
     pipeline.py frame.
  3. Top functions by inclusive (anywhere on stack) and leaf
     (top-of-stack) sample counts, project files only.

Usage:
    venv/bin/python tools/profile_airport_build.py ICAO [--interval 0.02]
        [--out /tmp/ICAO_profile.txt]
"""

import argparse
import collections
import os
import sys
import threading
import time

os.environ.setdefault("O4_LOG_VERBOSITY", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (os.path.join(ROOT, "src"), ROOT, os.path.join(ROOT, "tests"),
             os.path.join(ROOT, "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)

# Phase boundaries = the `_progress.step()` call sites in pipeline.py.
# Samples are bucketed by the innermost pipeline.py line on the stack.
# These line numbers are the ``_progress.step()`` call sites in pipeline.py
# that begin each phase; a sample is attributed to the phase whose step()
# most recently preceded the innermost pipeline.py frame.  Keep them in sync
# with pipeline.py (they drifted from 486/562/1642/2972/3892/5590, which
# mis-attributed late phase-4 taxi-rect construction to the solve phase).
PHASE_STARTS = [
    (619, "1 Loading apt.dat & runway geometry"),
    (695, "2 Assembling pavement & runway shoulders"),
    (1991, "3 Building taxiways & terminals"),
    (3321, "4 Building taxi rects, junctions & service roads"),
    (4241, "5 Solving elevations (FAA grade compliance)"),
    (5951, "6 Emitting terrain features & finalizing"),
]


def _phase_for_line(lineno):
    name = "0 before step 1"
    for start, phase in PHASE_STARTS:
        if lineno >= start:
            name = phase
    return name


class StackSampler(threading.Thread):
    """Samples one target thread's stack until ``stop()`` is called."""

    def __init__(self, target_thread_id, interval):
        super().__init__(daemon=True)
        self.target_thread_id = target_thread_id
        self.interval = interval
        self.samples = 0
        self.leaf_counts = collections.Counter()
        self.inclusive_counts = collections.Counter()
        self.pipeline_site_counts = collections.Counter()  # (phase, "file:line fn")
        self.phase_counts = collections.Counter()
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            frame = sys._current_frames().get(self.target_thread_id)
            if frame is not None:
                self._record(frame)
            time.sleep(self.interval)

    def _record(self, frame):
        self.samples += 1
        seen = set()
        leaf_key = None
        pipeline_site = None
        pipeline_line = None
        depth = 0
        while frame is not None and depth < 200:
            code = frame.f_code
            filename = code.co_filename
            short = os.path.relpath(filename, ROOT) if filename.startswith(ROOT) else filename
            key = f"{short}:{code.co_name}"
            if leaf_key is None:
                leaf_key = f"{short}:{frame.f_lineno} {code.co_name}"
            if key not in seen:
                seen.add(key)
                self.inclusive_counts[key] += 1
            # innermost pipeline.py frame = the pipeline task being executed
            if pipeline_site is None and short.endswith("auto_patch/pipeline.py"):
                pipeline_site = f"pipeline.py:{frame.f_lineno} in {code.co_name}"
                pipeline_line = frame.f_lineno
            frame = frame.f_back
            depth += 1
        self.leaf_counts[leaf_key] += 1
        if pipeline_line is not None:
            phase = _phase_for_line(pipeline_line)
            self.phase_counts[phase] += 1
            self.pipeline_site_counts[(phase, pipeline_site)] += 1
        else:
            self.phase_counts["(no pipeline.py frame)"] += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("icao")
    parser.add_argument("--interval", type=float, default=0.02)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out_path = args.out or f"/tmp/{args.icao}_profile.txt"

    from conftest import xplane_root                        # noqa: E402
    from auto_patch.pipeline import build_airport_pavement  # noqa: E402

    sampler = StackSampler(threading.get_ident(), args.interval)
    sampler.start()
    t0 = time.time()
    build_airport_pavement(args.icao, xplane_root(), compute_elevations=True)
    elapsed = time.time() - t0
    sampler.stop()
    sampler.join(timeout=2.0)

    per_sample = elapsed / max(sampler.samples, 1)

    def secs(n):
        return n * per_sample

    lines = []
    lines.append(f"{args.icao} build: {elapsed:.1f} s wall, "
                 f"{sampler.samples} samples ({per_sample * 1000:.1f} ms/sample)")

    lines.append("\n== Seconds per pipeline phase (sampled) ==")
    for phase, n in sorted(sampler.phase_counts.items()):
        lines.append(f"  {secs(n):8.1f} s  {phase}")

    lines.append("\n== Top pipeline.py call sites per phase ==")
    by_phase = collections.defaultdict(collections.Counter)
    for (phase, site), n in sampler.pipeline_site_counts.items():
        by_phase[phase][site] += n
    for phase in sorted(by_phase):
        lines.append(f"  -- {phase} --")
        for site, n in by_phase[phase].most_common(15):
            if secs(n) < 1.0:
                break
            lines.append(f"     {secs(n):8.1f} s  {site}")

    lines.append("\n== Top 60 functions by inclusive wall time ==")
    for key, n in sampler.inclusive_counts.most_common(60):
        lines.append(f"  {secs(n):8.1f} s  {key}")

    lines.append("\n== Top 40 leaf (self-time) sites ==")
    for key, n in sampler.leaf_counts.most_common(40):
        lines.append(f"  {secs(n):8.1f} s  {key}")

    report = "\n".join(lines) + "\n"
    with open(out_path, "w") as handle:
        handle.write(report)
    print(report)
    print(f"report written to {out_path}")


if __name__ == "__main__":
    main()
