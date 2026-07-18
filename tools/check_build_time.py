"""Executable enforcement for the build-time HARD LAW (CLAUDE.md item 6).

The canonical law text lives in CLAUDE.md, working-style item 6 (owner
rulings 2026-07-18).  This tool makes it executable: two budgets, both
COLD and EXCLUDING download time —

  * per-airport auto-patch wall  <= 60 s
  * whole-tile compute           <= 300 s (provisional)

A measured metric (total or any phase) that regresses by >= 1 % of the
relevant budget (0.6 s airport / 3.0 s tile) against the committed
baseline FAILS the check, as does a build that crosses its budget, unless
a matching owner approval is committed in
``tools/build_time_approvals.json``.  A failure means: spawn the Fable 5
whole-pipeline optimization review; a budget crossing additionally needs
recorded owner approval (see CLAUDE.md item 6).

Baselines are committed in ``tools/build_time_baselines.json`` (reference
airports: at least OTHH + CYXY per the enforcement brief; CYXY is the
ruled first test airport).

Measurement sources
-------------------
* Airport builds: the production per-phase store
  ``~/.ortho4xp/auto_patch_build_times/<ICAO>.json`` written by
  ``build_airport_pavement`` at the end of every successful full build.
  ``--run`` performs fresh cold-equivalent builds (one fresh interpreter
  per run, exactly like ``tools/full_airport_build.py`` /
  ``tools/profile_airport_build.py``) and then consumes the records those
  runs appended.  Cold-equivalent means: fresh process (no in-process
  memoization) with OSM/DEM downloads already cached, so no download
  time is included — run each airport once by hand first if its caches
  are cold.
* Whole-tile builds: the engine store
  ``~/.ortho4xp/tile_build_times/<short_latlon>.json`` written by
  ``o4_engine.session``.  Only records with
  ``features.textures_missing == 0`` qualify (a record with missing
  textures spent wall time downloading, which the budget excludes); the
  tile compute total is the sum of its recorded step seconds.  There is
  no ``--run`` mode for tiles — build the tile in the app/engine, then
  run this tool.

Usage
-----
    venv/bin/python tools/check_build_time.py
        Check every subject in the committed baselines against the
        newest qualifying store record.  Exit 0 = pass, 1 = unapproved
        regression or budget crossing, 2 = missing data / usage error.

    venv/bin/python tools/check_build_time.py --run OTHH CYXY
        Run fresh cold-equivalent benchmark builds for the named
        airports (``--runs N`` repeats each and compares the per-metric
        median), then check as above.

    venv/bin/python tools/check_build_time.py --run --update-baselines OTHH CYXY
        Benchmark, then rewrite those subjects' baselines from the
        measurements (with git provenance) instead of failing on them.
        Commit the baseline file together with the change that paid for
        the new numbers — and the approval entry when the law requires
        one.

Approvals file format (``tools/build_time_approvals.json``)::

    {"approvals": [
        {"subject": "airport:OTHH",
         "metric": "total",                # or "phase:<label>" or "*"
         "allowed_seconds": 400.0,         # approved ceiling, measured must stay under
         "reason": "why the owner accepted this cost",
         "approved_by": "owner", "date": "2026-07-18"}]}

Entries missing a non-empty ``reason`` or ``approved_by`` are ignored
(with a warning) — a hollow approval cannot pass the check.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The budgets and the review fraction mirror CLAUDE.md working-style
# item 6 (the canonical text); change them there first.
AIRPORT_BUDGET_SECONDS = 60.0
TILE_BUDGET_SECONDS = 300.0
REVIEW_FRACTION = 0.01

DEFAULT_BASELINES_PATH = os.path.join(
    REPOSITORY_ROOT, "tools", "build_time_baselines.json")
DEFAULT_APPROVALS_PATH = os.path.join(
    REPOSITORY_ROOT, "tools", "build_time_approvals.json")
DEFAULT_AIRPORT_STORE_DIRECTORY = os.path.join(
    os.path.expanduser("~"), ".ortho4xp", "auto_patch_build_times")
DEFAULT_TILE_STORE_DIRECTORY = os.path.join(
    os.path.expanduser("~"), ".ortho4xp", "tile_build_times")

TOTAL_METRIC = "total"

STATUS_OK = "OK"
STATUS_IMPROVED = "IMPROVED"
STATUS_REGRESSION = "REGRESSION"
STATUS_APPROVED = "APPROVED"
STATUS_BUDGET_CROSSED = "BUDGET-CROSSED"
FAILING_STATUSES = (STATUS_REGRESSION, STATUS_BUDGET_CROSSED)


def budget_for_subject(subject: str) -> float:
    """Budget seconds for a ``airport:<ICAO>`` / ``tile:<latlon>`` subject."""
    if subject.startswith("tile:"):
        return TILE_BUDGET_SECONDS
    return AIRPORT_BUDGET_SECONDS


def load_json_file(path: str) -> dict:
    with open(path) as json_file:
        loaded = json.load(json_file)
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return loaded


def valid_approvals(approvals_document: dict, warn=print) -> list:
    """The usable approval entries; hollow ones are dropped with a warning."""
    entries = approvals_document.get("approvals", [])
    usable = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        reason = str(entry.get("reason", "") or "").strip()
        approved_by = str(entry.get("approved_by", "") or "").strip()
        allowed = entry.get("allowed_seconds")
        if not reason or not approved_by \
                or not isinstance(allowed, (int, float)):
            warn(f"WARNING: ignoring hollow approval entry {entry!r} "
                 "(needs non-empty reason, approved_by, "
                 "numeric allowed_seconds)")
            continue
        usable.append(entry)
    return usable


def matching_approval_ceiling(approvals: list, subject: str,
                              metric: str):
    """Highest approved ceiling covering (subject, metric), or None."""
    ceilings = [
        float(entry["allowed_seconds"]) for entry in approvals
        if entry.get("subject") in (subject, "*")
        and entry.get("metric") in (metric, "*")
    ]
    return max(ceilings) if ceilings else None


def evaluate_subject(subject: str, baseline: dict, measured: dict,
                     approvals: list) -> list:
    """Compare one subject's measurement to its baseline.

    Returns one finding dict per metric (total first, then each phase in
    baseline-then-measured order).  A metric fails when it regresses by
    at least ``REVIEW_FRACTION`` of the subject's budget, or when the
    total crosses the budget, unless an approval ceiling covers the
    measured value.
    """
    budget = budget_for_subject(subject)
    threshold = REVIEW_FRACTION * budget
    baseline_phases = dict(baseline.get("phase_seconds") or {})
    measured_phases = dict(measured.get("phase_seconds") or {})
    metric_names = [TOTAL_METRIC]
    for label in list(baseline_phases) + list(measured_phases):
        name = f"phase:{label}"
        if name not in metric_names:
            metric_names.append(name)

    findings = []
    for metric in metric_names:
        if metric == TOTAL_METRIC:
            baseline_seconds = float(baseline.get("total_seconds") or 0.0)
            measured_seconds = float(measured.get("total_seconds") or 0.0)
        else:
            label = metric[len("phase:"):]
            baseline_seconds = float(baseline_phases.get(label) or 0.0)
            measured_seconds = float(measured_phases.get(label) or 0.0)
        delta = measured_seconds - baseline_seconds
        crossed = (metric == TOTAL_METRIC
                   and baseline_seconds <= budget < measured_seconds)
        if delta >= threshold or crossed:
            ceiling = matching_approval_ceiling(approvals, subject, metric)
            if ceiling is not None and measured_seconds <= ceiling:
                status = STATUS_APPROVED
            elif crossed:
                status = STATUS_BUDGET_CROSSED
            else:
                status = STATUS_REGRESSION
        elif delta <= -threshold:
            status = STATUS_IMPROVED
        else:
            status = STATUS_OK
        findings.append({
            "subject": subject,
            "metric": metric,
            "budget_seconds": budget,
            "threshold_seconds": threshold,
            "baseline_seconds": baseline_seconds,
            "measured_seconds": measured_seconds,
            "delta_seconds": delta,
            "over_budget": (metric == TOTAL_METRIC
                            and measured_seconds > budget),
            "status": status,
        })
    return findings


def evaluate_all(baselines_document: dict, measurements: dict,
                 approvals: list) -> list:
    """Findings for every measured subject, in baseline order."""
    findings = []
    for subject in measurements:
        baseline = subject_baseline(baselines_document, subject) or {}
        findings.extend(evaluate_subject(
            subject, baseline, measurements[subject], approvals))
    return findings


def subject_baseline(baselines_document: dict, subject: str):
    kind, _, name = subject.partition(":")
    section = baselines_document.get(
        "tiles" if kind == "tile" else "airports", {})
    return section.get(name) if isinstance(section, dict) else None


def baseline_subjects(baselines_document: dict) -> list:
    subjects = []
    for name in (baselines_document.get("airports") or {}):
        subjects.append(f"airport:{name}")
    for name in (baselines_document.get("tiles") or {}):
        subjects.append(f"tile:{name}")
    return subjects


def format_budget_table(findings: list) -> str:
    """The human-readable budget table plus the pass/fail verdict."""
    lines = [
        "Build-time hard law check — canonical text: CLAUDE.md "
        "working-style item 6",
        f"Budgets (cold, excluding downloads): airport "
        f"{AIRPORT_BUDGET_SECONDS:.0f} s, tile compute "
        f"{TILE_BUDGET_SECONDS:.0f} s   (review trigger = "
        f"{REVIEW_FRACTION:.0%} of budget: "
        f"{REVIEW_FRACTION * AIRPORT_BUDGET_SECONDS:.2f} s / "
        f"{REVIEW_FRACTION * TILE_BUDGET_SECONDS:.2f} s)",
        "",
    ]
    header = (f"{'SUBJECT':<16} {'METRIC':<48} {'BASELINE':>9} "
              f"{'MEASURED':>9} {'DELTA':>8}  STATUS")
    lines.append(header)
    lines.append("-" * len(header))
    for finding in findings:
        metric = finding["metric"]
        if len(metric) > 48:
            metric = metric[:45] + "..."
        status = finding["status"]
        if finding["over_budget"]:
            status += " (over budget)"
        lines.append(
            f"{finding['subject']:<16} {metric:<48} "
            f"{finding['baseline_seconds']:>9.2f} "
            f"{finding['measured_seconds']:>9.2f} "
            f"{finding['delta_seconds']:>+8.2f}  {status}")
    failing = [finding for finding in findings
               if finding["status"] in FAILING_STATUSES]
    lines.append("")
    if failing:
        lines.append(
            f"RESULT: FAIL — {len(failing)} unapproved regression(s). "
            "Per CLAUDE.md item 6: spawn a Fable 5 whole-pipeline "
            "optimization review; a budget crossing (or a >=1% "
            "regression while already over budget) needs owner approval "
            "recorded in tools/build_time_approvals.json.")
    else:
        lines.append("RESULT: PASS")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Measurement acquisition
# ---------------------------------------------------------------------------

def load_store_records(store_directory: str, name: str) -> list:
    path = os.path.join(store_directory, f"{name}.json")
    try:
        with open(path) as record_file:
            records = json.load(record_file)
        return records if isinstance(records, list) else []
    except (OSError, ValueError):
        return []


def newest_airport_measurement(icao: str, store_directory: str,
                               finished_after: float = 0.0):
    """Newest airport store record as a measurement dict, or None."""
    for record in reversed(load_store_records(store_directory, icao)):
        if float(record.get("finished_at") or 0.0) >= finished_after:
            return {
                "total_seconds": float(record.get("total_seconds") or 0.0),
                "phase_seconds": dict(record.get("phase_seconds") or {}),
                "finished_at": record.get("finished_at"),
            }
    return None


def newest_tile_measurement(tile_name: str, store_directory: str):
    """Newest download-free tile record (textures_missing == 0), or None.

    Tile compute total = sum of recorded step seconds; a record that had
    to download textures spent unbudgeted wall time and is skipped.
    """
    for record in reversed(load_store_records(store_directory, tile_name)):
        features = record.get("features") or {}
        if float(features.get("textures_missing") or 0.0) != 0.0:
            continue
        step_seconds = {
            str(step): float(seconds)
            for step, seconds in (record.get("step_seconds") or {}).items()}
        if not step_seconds:
            continue
        return {
            "total_seconds": round(sum(step_seconds.values()), 2),
            "phase_seconds": step_seconds,
            "finished_at": record.get("finished_at"),
        }
    return None


def median_measurement(measurements: list) -> dict:
    """Per-metric median across repeated runs of one subject."""
    totals = [m["total_seconds"] for m in measurements]
    phase_labels = []
    for measurement in measurements:
        for label in measurement["phase_seconds"]:
            if label not in phase_labels:
                phase_labels.append(label)
    return {
        "total_seconds": round(statistics.median(totals), 2),
        "phase_seconds": {
            label: round(statistics.median(
                [m["phase_seconds"].get(label, 0.0)
                 for m in measurements]), 2)
            for label in phase_labels},
    }


def run_airport_benchmark(icao: str, repetition_count: int,
                          store_directory: str, runner=None) -> dict:
    """Fresh cold-equivalent build(s) of one airport; median measurement.

    ``runner(icao)`` performs one build whose record lands in the store;
    the default spawns ``check_build_time.py --run-one ICAO`` in a fresh
    interpreter (cold-equivalent by construction).  Raises RuntimeError
    when a run leaves no new store record.
    """
    if runner is None:
        def default_subprocess_runner(one_icao):
            completed = subprocess.run(
                [sys.executable, os.path.abspath(__file__),
                 "--run-one", one_icao],
                cwd=REPOSITORY_ROOT)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"benchmark build of {one_icao} failed "
                    f"(exit {completed.returncode})")
        runner = default_subprocess_runner
    measurements = []
    for repetition in range(repetition_count):
        started_at = time.time()
        runner(icao)
        measurement = newest_airport_measurement(
            icao, store_directory, finished_after=started_at)
        if measurement is None:
            raise RuntimeError(
                f"benchmark build of {icao} produced no new record in "
                f"{store_directory} — did the build fail before phase "
                "recording?")
        print(f"  {icao} run {repetition + 1}/{repetition_count}: "
              f"{measurement['total_seconds']:.1f} s")
        measurements.append(measurement)
    return median_measurement(measurements)


def run_one_airport_build(icao: str) -> None:
    """One in-process full build (the ``--run-one`` subprocess body).

    Mirrors ``tools/full_airport_build.py``: the pipeline itself records
    the per-phase wall times into the production store on success.
    """
    os.environ.setdefault("O4_LOG_VERBOSITY", "1")
    for path in (os.path.join(REPOSITORY_ROOT, "src"), REPOSITORY_ROOT,
                 os.path.join(REPOSITORY_ROOT, "tests")):
        if path not in sys.path:
            sys.path.insert(0, path)
    from conftest import xplane_root                        # type: ignore
    from auto_patch.pipeline import build_airport_pavement  # type: ignore
    started_at = time.time()
    build_airport_pavement(icao, xplane_root(), compute_elevations=True)
    print(f"RUN-ONE {icao} {time.time() - started_at:.1f}s")


# ---------------------------------------------------------------------------
# Baseline updates
# ---------------------------------------------------------------------------

def git_provenance() -> dict:
    """Current commit + dirty state, for auditable baseline metadata."""
    def git_output(*arguments):
        try:
            return subprocess.run(
                ["git", *arguments], cwd=REPOSITORY_ROOT,
                capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            return ""
    dirty_files = [line for line in
                   git_output("status", "--porcelain").splitlines() if line]
    return {
        "git_commit": git_output("rev-parse", "--short", "HEAD"),
        "git_dirty_file_count": len(dirty_files),
    }


def update_baselines(baselines_document: dict, measurements: dict,
                     provenance: dict) -> dict:
    """New baselines document with the measured subjects replaced."""
    updated = json.loads(json.dumps(baselines_document))  # deep copy
    stamp = {"recorded_at": time.strftime("%Y-%m-%d"), **provenance}
    for subject, measurement in measurements.items():
        kind, _, name = subject.partition(":")
        section_name = "tiles" if kind == "tile" else "airports"
        section = updated.setdefault(section_name, {})
        section[name] = {
            **stamp,
            "total_seconds": measurement["total_seconds"],
            "phase_seconds": measurement["phase_seconds"],
        }
    return updated


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build-time hard law check (CLAUDE.md item 6).")
    parser.add_argument("subjects", nargs="*", metavar="SUBJECT",
                        help="ICAO, airport:ICAO, or tile:<short_latlon>; "
                             "default = every subject in the baselines")
    parser.add_argument("--run", action="store_true",
                        help="run fresh cold-equivalent airport builds "
                             "instead of consuming existing store records")
    parser.add_argument("--runs", type=int, default=1, metavar="N",
                        help="repetitions per airport with --run; the "
                             "per-metric median is compared (default 1)")
    parser.add_argument("--update-baselines", action="store_true",
                        help="rewrite the checked subjects' baselines "
                             "from these measurements")
    parser.add_argument("--baselines", default=DEFAULT_BASELINES_PATH)
    parser.add_argument("--approvals", default=DEFAULT_APPROVALS_PATH)
    parser.add_argument("--airport-store",
                        default=DEFAULT_AIRPORT_STORE_DIRECTORY)
    parser.add_argument("--tile-store",
                        default=DEFAULT_TILE_STORE_DIRECTORY)
    parser.add_argument("--run-one", metavar="ICAO",
                        help=argparse.SUPPRESS)  # internal subprocess body
    arguments = parser.parse_args(argv)

    if arguments.run_one:
        run_one_airport_build(arguments.run_one)
        return 0

    try:
        baselines_document = load_json_file(arguments.baselines)
    except (OSError, ValueError) as problem:
        print(f"ERROR: cannot load baselines: {problem}")
        return 2
    try:
        approvals = valid_approvals(load_json_file(arguments.approvals))
    except (OSError, ValueError) as problem:
        print(f"ERROR: cannot load approvals: {problem}")
        return 2

    subjects = [subject if ":" in subject else f"airport:{subject}"
                for subject in arguments.subjects]
    if not subjects:
        subjects = baseline_subjects(baselines_document)
    if not subjects:
        print("ERROR: no subjects — baselines file is empty and none "
              "were named on the command line")
        return 2

    measurements = {}
    for subject in subjects:
        kind, _, name = subject.partition(":")
        if kind == "airport" and arguments.run:
            try:
                measurements[subject] = run_airport_benchmark(
                    name, max(1, arguments.runs), arguments.airport_store)
            except RuntimeError as problem:
                print(f"ERROR: {problem}")
                return 2
        elif kind == "airport":
            measurement = newest_airport_measurement(
                name, arguments.airport_store)
            if measurement is None:
                print(f"ERROR: no store record for {subject} in "
                      f"{arguments.airport_store} — build it first or "
                      "use --run")
                return 2
            measurements[subject] = measurement
        elif kind == "tile":
            measurement = newest_tile_measurement(name, arguments.tile_store)
            if measurement is None:
                print(f"ERROR: no download-free store record for "
                      f"{subject} in {arguments.tile_store} — build the "
                      "tile with textures already cached first")
                return 2
            measurements[subject] = measurement
        else:
            print(f"ERROR: unknown subject kind {subject!r}")
            return 2

    if not arguments.update_baselines:
        missing = [subject for subject in measurements
                   if subject_baseline(baselines_document, subject) is None]
        if missing:
            print(f"ERROR: no committed baseline for {', '.join(missing)} "
                  "— record one with --update-baselines")
            return 2

    if arguments.update_baselines:
        updated = update_baselines(
            baselines_document, measurements, git_provenance())
        with open(arguments.baselines, "w") as baselines_file:
            json.dump(updated, baselines_file, indent=1, sort_keys=False)
            baselines_file.write("\n")
        print(f"Baselines updated: {arguments.baselines} "
              f"({', '.join(measurements)})")
        baselines_document = updated

    findings = evaluate_all(baselines_document, measurements, approvals)
    print(format_budget_table(findings))
    failing = any(finding["status"] in FAILING_STATUSES
                  for finding in findings)
    return 1 if failing and not arguments.update_baselines else 0


if __name__ == "__main__":
    sys.exit(main())
