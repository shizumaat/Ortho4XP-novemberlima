"""Headless tests for tools/check_build_time.py (build-time hard law).

Synthetic timing inputs only — no real builds, no network, no store
outside ``tmp_path``.  The law under test is CLAUDE.md working-style
item 6: 60 s airport / 300 s tile budgets, review trigger at 1 % of the
relevant budget, approvals required for budget crossings.
"""

import importlib.util
import json
import os

import pytest

_TOOL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools", "check_build_time.py")
_specification = importlib.util.spec_from_file_location(
    "check_build_time", _TOOL_PATH)
assert _specification is not None and _specification.loader is not None
check_build_time = importlib.util.module_from_spec(_specification)
_specification.loader.exec_module(check_build_time)


def airport_measurement(total, phases=None):
    return {"total_seconds": total, "phase_seconds": dict(phases or {})}


def finding_for(findings, metric):
    matches = [f for f in findings if f["metric"] == metric]
    assert len(matches) == 1, f"expected one {metric} finding"
    return matches[0]


# ---------------------------------------------------------------------------
# evaluate_subject — threshold semantics
# ---------------------------------------------------------------------------

def test_airport_regression_below_one_percent_of_budget_passes():
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(45.0),
        airport_measurement(45.59), [])
    assert finding_for(findings, "total")["status"] == "OK"


def test_airport_regression_at_one_percent_of_budget_fails():
    # 1 % of the 60 s airport budget = 0.60 s exactly.
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(45.0),
        airport_measurement(45.60), [])
    assert finding_for(findings, "total")["status"] == "REGRESSION"


def test_airport_improvement_reported_as_improved():
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(45.0),
        airport_measurement(44.0), [])
    assert finding_for(findings, "total")["status"] == "IMPROVED"


def test_phase_regression_fails_even_when_total_holds():
    # A phase that pays for another phase's win still triggers review.
    baseline = airport_measurement(45.0, {"solve": 30.0, "emit": 10.0})
    measured = airport_measurement(45.0, {"solve": 31.0, "emit": 9.0})
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", baseline, measured, [])
    assert finding_for(findings, "total")["status"] == "OK"
    assert finding_for(findings, "phase:solve")["status"] == "REGRESSION"
    assert finding_for(findings, "phase:emit")["status"] == "IMPROVED"


def test_new_phase_counts_fully_against_the_budget():
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(45.0, {}),
        airport_measurement(45.0, {"new feature pass": 0.7}), [])
    assert finding_for(
        findings, "phase:new feature pass")["status"] == "REGRESSION"


def test_tile_threshold_is_three_seconds():
    # 1 % of the 300 s tile budget = 3.0 s: 2.9 s passes, 3.0 s fails.
    passing = check_build_time.evaluate_subject(
        "tile:+30+031", airport_measurement(200.0),
        airport_measurement(202.9), [])
    failing = check_build_time.evaluate_subject(
        "tile:+30+031", airport_measurement(200.0),
        airport_measurement(203.0), [])
    assert finding_for(passing, "total")["status"] == "OK"
    assert finding_for(failing, "total")["status"] == "REGRESSION"


def test_budget_crossing_fails_even_below_review_threshold():
    # 59.8 -> 60.3 s regresses only 0.5 s (< 0.6) but crosses the 60 s
    # budget — the law requires owner approval for any crossing.
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(59.8),
        airport_measurement(60.3), [])
    total = finding_for(findings, "total")
    assert total["status"] == "BUDGET-CROSSED"
    assert total["over_budget"]


def test_already_over_budget_regression_still_fails():
    findings = check_build_time.evaluate_subject(
        "airport:OTHH", airport_measurement(340.0),
        airport_measurement(341.0), [])
    total = finding_for(findings, "total")
    assert total["status"] == "REGRESSION"
    assert total["over_budget"]


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------

def approval(subject="airport:CYXY", metric="total", allowed=100.0,
             reason="owner accepted the cost", approved_by="owner"):
    return {"subject": subject, "metric": metric,
            "allowed_seconds": allowed, "reason": reason,
            "approved_by": approved_by, "date": "2026-07-18"}


def test_matching_approval_turns_regression_into_approved():
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(45.0),
        airport_measurement(47.0), [approval(allowed=48.0)])
    assert finding_for(findings, "total")["status"] == "APPROVED"


def test_approval_ceiling_below_measurement_does_not_cover():
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(45.0),
        airport_measurement(47.0), [approval(allowed=46.0)])
    assert finding_for(findings, "total")["status"] == "REGRESSION"


def test_approval_subject_and_metric_must_match():
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(45.0),
        airport_measurement(47.0),
        [approval(subject="airport:OTHH"),
         approval(metric="phase:solve")])
    assert finding_for(findings, "total")["status"] == "REGRESSION"


def test_wildcard_approval_covers_any_subject_metric():
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(45.0, {"solve": 30.0}),
        airport_measurement(47.0, {"solve": 32.0}),
        [approval(subject="*", metric="*", allowed=100.0)])
    assert finding_for(findings, "total")["status"] == "APPROVED"
    assert finding_for(findings, "phase:solve")["status"] == "APPROVED"


def test_budget_crossing_covered_by_approval():
    findings = check_build_time.evaluate_subject(
        "airport:CYXY", airport_measurement(59.8),
        airport_measurement(60.3), [approval(allowed=61.0)])
    assert finding_for(findings, "total")["status"] == "APPROVED"


def test_hollow_approvals_are_ignored_with_warning():
    warnings = []
    usable = check_build_time.valid_approvals(
        {"approvals": [approval(reason="  "),
                       approval(approved_by=""),
                       {"subject": "airport:CYXY", "metric": "total",
                        "reason": "no ceiling", "approved_by": "owner"},
                       approval()]},
        warn=warnings.append)
    assert len(usable) == 1
    assert len(warnings) == 3


# ---------------------------------------------------------------------------
# Store consumption
# ---------------------------------------------------------------------------

def write_store(directory, name, records):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, f"{name}.json"), "w") as store_file:
        json.dump(records, store_file)


def test_newest_airport_measurement_takes_last_record(tmp_path):
    store = str(tmp_path / "airports")
    write_store(store, "CYXY", [
        {"finished_at": 100.0, "total_seconds": 50.0,
         "phase_seconds": {"solve": 40.0}},
        {"finished_at": 200.0, "total_seconds": 45.0,
         "phase_seconds": {"solve": 35.0}},
    ])
    measurement = check_build_time.newest_airport_measurement("CYXY", store)
    assert measurement["total_seconds"] == 45.0
    assert measurement["phase_seconds"] == {"solve": 35.0}
    assert check_build_time.newest_airport_measurement(
        "CYXY", store, finished_after=250.0) is None
    assert check_build_time.newest_airport_measurement("ZZZZ", store) is None


def test_newest_tile_measurement_skips_download_records(tmp_path):
    store = str(tmp_path / "tiles")
    write_store(store, "+30+031", [
        {"finished_at": 100.0,
         "features": {"textures_missing": 0},
         "step_seconds": {"vector": 10.0, "mesh": 20.0}},
        {"finished_at": 200.0,
         "features": {"textures_missing": 12},
         "step_seconds": {"imagery": 500.0}},
    ])
    measurement = check_build_time.newest_tile_measurement("+30+031", store)
    assert measurement["total_seconds"] == 30.0
    assert measurement["phase_seconds"] == {"vector": 10.0, "mesh": 20.0}


def test_median_measurement_is_per_metric():
    median = check_build_time.median_measurement([
        airport_measurement(50.0, {"solve": 40.0}),
        airport_measurement(44.0, {"solve": 30.0}),
        airport_measurement(46.0, {"solve": 35.0, "emit": 5.0}),
    ])
    assert median["total_seconds"] == 46.0
    assert median["phase_seconds"]["solve"] == 35.0
    assert median["phase_seconds"]["emit"] == 0.0  # median of 0, 0, 5


def test_run_airport_benchmark_with_injected_runner(tmp_path):
    store = str(tmp_path / "airports")
    finished_at = [0.0]

    def fake_runner(icao):
        finished_at[0] += 1000.0
        records = []
        path = os.path.join(store, f"{icao}.json")
        if os.path.exists(path):
            records = json.load(open(path))
        records.append({"finished_at": finished_at[0] + 1e12,
                        "total_seconds": 40.0 + len(records),
                        "phase_seconds": {"solve": 30.0}})
        write_store(store, icao, records)

    measurement = check_build_time.run_airport_benchmark(
        "CYXY", 3, store, runner=fake_runner)
    assert measurement["total_seconds"] == 41.0  # median of 40, 41, 42


def test_run_airport_benchmark_without_new_record_raises(tmp_path):
    with pytest.raises(RuntimeError):
        check_build_time.run_airport_benchmark(
            "CYXY", 1, str(tmp_path), runner=lambda icao: None)


# ---------------------------------------------------------------------------
# main() end-to-end on synthetic files
# ---------------------------------------------------------------------------

def synthetic_setup(tmp_path, baseline_total=45.0, measured_total=45.0,
                    approvals=None):
    baselines_path = tmp_path / "baselines.json"
    approvals_path = tmp_path / "approvals.json"
    airport_store = tmp_path / "airport_store"
    tile_store = tmp_path / "tile_store"
    baselines_path.write_text(json.dumps({
        "airports": {"CYXY": {"total_seconds": baseline_total,
                              "phase_seconds": {"solve": 30.0}}},
        "tiles": {}}))
    approvals_path.write_text(json.dumps({"approvals": approvals or []}))
    write_store(str(airport_store), "CYXY", [
        {"finished_at": 1.0, "total_seconds": measured_total,
         "phase_seconds": {"solve": 30.0}}])
    return ["--baselines", str(baselines_path),
            "--approvals", str(approvals_path),
            "--airport-store", str(airport_store),
            "--tile-store", str(tile_store)], baselines_path


def test_main_passes_and_prints_budget_table(tmp_path, capsys):
    argv, _ = synthetic_setup(tmp_path)
    assert check_build_time.main(argv) == 0
    output = capsys.readouterr().out
    assert "CLAUDE.md" in output
    assert "airport:CYXY" in output
    assert "RESULT: PASS" in output


def test_main_fails_on_unapproved_regression(tmp_path, capsys):
    argv, _ = synthetic_setup(tmp_path, measured_total=47.0)
    assert check_build_time.main(argv) == 1
    assert "RESULT: FAIL" in capsys.readouterr().out


def test_main_passes_with_committed_approval(tmp_path, capsys):
    argv, _ = synthetic_setup(
        tmp_path, measured_total=47.0, approvals=[approval(allowed=48.0)])
    assert check_build_time.main(argv) == 0
    assert "APPROVED" in capsys.readouterr().out


def test_main_errors_without_store_record(tmp_path):
    argv, _ = synthetic_setup(tmp_path)
    assert check_build_time.main(argv + ["OTHH"]) == 2


def test_main_errors_on_measured_subject_without_baseline(tmp_path):
    argv, baselines_path = synthetic_setup(tmp_path)
    baselines_path.write_text(json.dumps({"airports": {}, "tiles": {}}))
    assert check_build_time.main(argv + ["CYXY"]) == 2


def test_main_update_baselines_writes_measurement(tmp_path, capsys):
    argv, baselines_path = synthetic_setup(tmp_path, measured_total=47.0)
    assert check_build_time.main(argv + ["--update-baselines"]) == 0
    updated = json.loads(baselines_path.read_text())
    entry = updated["airports"]["CYXY"]
    assert entry["total_seconds"] == 47.0
    assert entry["recorded_at"]
    # A fresh check against the new baseline now passes.
    capsys.readouterr()
    assert check_build_time.main(argv) == 0
    assert "RESULT: PASS" in capsys.readouterr().out


def test_main_checks_tile_subject_from_store(tmp_path, capsys):
    argv, baselines_path = synthetic_setup(tmp_path)
    baselines_path.write_text(json.dumps({
        "airports": {},
        "tiles": {"+30+031": {"total_seconds": 30.0,
                              "phase_seconds": {"vector": 10.0,
                                                "mesh": 20.0}}}}))
    write_store(str(tmp_path / "tile_store"), "+30+031", [
        {"finished_at": 1.0, "features": {"textures_missing": 0},
         "step_seconds": {"vector": 10.0, "mesh": 25.0}}])
    assert check_build_time.main(argv) == 1
    output = capsys.readouterr().out
    assert "tile:+30+031" in output
    assert "phase:mesh" in output


def test_repository_baselines_and_approvals_files_are_valid():
    baselines = check_build_time.load_json_file(
        check_build_time.DEFAULT_BASELINES_PATH)
    approvals_document = check_build_time.load_json_file(
        check_build_time.DEFAULT_APPROVALS_PATH)
    assert isinstance(baselines.get("airports"), dict)
    assert isinstance(approvals_document.get("approvals"), list)
    # Every committed approval must be usable (non-hollow).
    warnings = []
    usable = check_build_time.valid_approvals(
        approvals_document, warn=warnings.append)
    assert not warnings
    assert len(usable) == len(approvals_document["approvals"])
