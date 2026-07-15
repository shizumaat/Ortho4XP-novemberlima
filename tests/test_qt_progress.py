"""Tests for the whole-tile build progress model (owner feedback: the ring
climbs once per tile, never restarts per step)."""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("PySide6")

import O4_Qt_GUI as GUI  # noqa: E402


def test_plan_all_steps_covers_unit_interval():
    plan = GUI.plan_steps(True, True, True)
    assert [k for k, _, _ in plan] == [
        "vector", "mesh", "masks", "imagery", "overlays",
    ]
    total = sum(w for _, _, w in plan)
    assert abs(total - 1.0) < 1e-9
    base = 0.0
    for _, b, w in plan:
        assert abs(b - base) < 1e-9, "slices must be contiguous"
        base += w


def test_plan_normalizes_subsets():
    plan = GUI.plan_steps(False, True, False)
    assert plan == [("imagery", 0.0, 1.0)]
    plan = GUI.plan_steps(True, False, False)
    assert [k for k, _, _ in plan] == ["vector", "mesh", "masks"]
    assert abs(sum(w for _, _, w in plan) - 1.0) < 1e-9


def test_step_progress_monotonic_within_imagery():
    early = GUI.step_progress("imagery", {1: 0, 2: 10, 3: 0})
    later = GUI.step_progress("imagery", {1: 50, 2: 80, 3: 40})
    done = GUI.step_progress("imagery", {1: 100, 2: 100, 3: 100})
    assert 0 < early < later < done
    assert abs(done - 100.0) < 1e-9


def test_step_progress_unmeasurable_steps_return_none():
    assert GUI.step_progress("mesh", {1: 50, 2: 50, 3: 50}) is None
    assert GUI.step_progress("overlays", {1: 50, 2: 50, 3: 50}) is None


def test_whole_tile_percentage_never_restarts():
    """Simulate a full tile: at every step boundary the whole-tile pct must
    be >= the pct reached at the end of the previous step."""
    plan = GUI.plan_steps(True, True, False)
    reached = 0.0
    for key, base, width in plan:
        sp = GUI.step_progress(key, {1: 100, 2: 100, 3: 100})
        start_pct = base * 100
        assert start_pct >= reached - 1e-6, (
            "step %s would drop the tile pct from %.1f to %.1f"
            % (key, reached, start_pct)
        )
        if sp is not None:
            reached = (base + width * sp / 100.0) * 100
        else:
            reached = start_pct
    assert reached > 99.9


def test_fmt_duration():
    assert GUI._fmt_duration(42) == "42 s"
    assert GUI._fmt_duration(125) == "2 m 05 s"
    assert GUI._fmt_duration(3700) == "1 h 01 m"


def test_fmt_date_includes_time():
    import datetime

    stamp = datetime.datetime(2026, 7, 15, 14, 30).timestamp()
    assert GUI._fmt_date(stamp) == "15 Jul 2026 14:30"
    assert GUI._fmt_date(None) == "—"
