"""Whole-tile progress math + auto-patch folding tests.

The percent/label state machine moved from the Qt view into
``o4_engine.session`` (2026-07-15, the one-engine/many-views migration:
views render, the Controller computes) — these tests now pin the session,
and a couple of pure formatting helpers still pin the Qt view.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

import O4_UI_Utils as UI  # noqa: E402
from o4_engine import events as EV  # noqa: E402
from o4_engine import session as SESSION  # noqa: E402
from o4_engine.session import EngineSession  # noqa: E402


# ---------------------------------------------------------------------------
# Step plan math (moved from O4_Qt_GUI)
# ---------------------------------------------------------------------------
def test_plan_covers_unit_interval():
    plan = SESSION.plan_steps(True, True, True)
    total = sum(w for _, _, w in plan)
    assert abs(total - 1.0) < 1e-9
    base = 0.0
    for _, b, w in plan:
        assert abs(b - base) < 1e-9, "slices must be contiguous"
        base += w


def test_plan_normalizes_subsets():
    plan = SESSION.plan_steps(False, True, False)
    assert plan == [("imagery", 0.0, 1.0)]
    plan = SESSION.plan_steps(True, False, False)
    assert [k for k, _, _ in plan] == ["vector", "mesh", "masks"]
    assert abs(sum(w for _, _, w in plan) - 1.0) < 1e-9


def test_step_progress_monotonic_within_imagery():
    early = SESSION.step_progress("imagery", {1: 0, 2: 10, 3: 0})
    later = SESSION.step_progress("imagery", {1: 50, 2: 80, 3: 40})
    done = SESSION.step_progress("imagery", {1: 100, 2: 100, 3: 100})
    assert 0 < early < later < done
    assert abs(done - 100.0) < 1e-9


def test_step_progress_unmeasurable_steps_return_none():
    assert SESSION.step_progress("mesh", {1: 50, 2: 50, 3: 50}) is None
    assert SESSION.step_progress("overlays", {1: 50, 2: 50, 3: 50}) is None


def test_whole_tile_percentage_never_restarts():
    """Simulate a full tile: at every step boundary the whole-tile pct must
    be >= the pct reached at the end of the previous step."""
    plan = SESSION.plan_steps(True, True, False)
    reached = 0.0
    for key, base, width in plan:
        sp = SESSION.step_progress(key, {1: 100, 2: 100, 3: 100})
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


# ---------------------------------------------------------------------------
# Formatting helpers (still the Qt view's)
# ---------------------------------------------------------------------------
def test_fmt_duration():
    pytest.importorskip("PySide6")
    import O4_Qt_GUI as GUI

    assert GUI._fmt_duration(42) == "42 s"
    assert GUI._fmt_duration(125) == "2 m 05 s"
    assert GUI._fmt_duration(3700) == "1 h 01 m"


def test_fmt_date_includes_time():
    pytest.importorskip("PySide6")
    import datetime

    import O4_Qt_GUI as GUI

    stamp = datetime.datetime(2026, 7, 15, 14, 30).timestamp()
    assert GUI._fmt_date(stamp) == "15 Jul 2026 14:30"
    assert GUI._fmt_date(None) == "—"


# ---------------------------------------------------------------------------
# Auto-patch folding (owner feedback: long airport-pavement construction
# inside the vector step must move the tile progress, not look stalled).
# Now pinned at the session: no Qt required at all.
# ---------------------------------------------------------------------------
@pytest.fixture
def session():
    s = EngineSession()
    events = []
    s.subscribe(events.append)
    # Put the session mid-build in the vector step of tile (48, -6), the
    # same scenario the old view-level probe used.
    s._start_step((48, -6), "vector", 0.0, 0.286)
    events.clear()
    yield s, events
    UI.engine_session = None
    UI.red_flag = False


def _step_events(events):
    return [e for e in events if isinstance(e, EV.StepProgress)]


def test_autopatch_progress_advances_tile_percentage(session):
    s, events = session
    s.autopatch_begin(["KBNA", "KJWN", "KMQY"])
    assert s._autopatch_running()

    s.autopatch_event("KBNA", 0.5, 1.0, "solving", "run")
    e1 = _step_events(events)[-1]
    assert (e1.lat, e1.lon) == (48, -6)
    assert 0 < e1.percent < 28.6
    assert "KBNA" in e1.label and "0/3" in e1.label

    s.autopatch_event("KBNA", 1.0, 1.0, "done", "done")
    s.autopatch_event("KJWN", 0.5, 1.0, "solving", "run")
    e2 = _step_events(events)[-1]
    assert e2.percent > e1.percent
    assert "1/3" in e2.label and "KJWN" in e2.label


def test_autopatch_suppresses_legacy_bar_until_finished(session):
    s, events = session
    s.autopatch_begin(["KBNA", "KJWN"])
    s.autopatch_event("KBNA", 0.25, 1.0, "grading", "run")
    assert _step_events(events)[-1].label.startswith("auto-patch")
    assert s._autopatch_running()

    # While running, stray legacy-bar events must not repaint the label.
    n_before = len(_step_events(events))
    s.legacy_progress(1, 77)
    assert len(_step_events(events)) == n_before

    # After the last airport finishes, the legacy bar drives again.
    s.autopatch_event("KBNA", 1.0, 1.0, "done", "done")
    s.autopatch_event("KJWN", 1.0, 1.0, "done", "fail")
    assert not s._autopatch_running()
    s.legacy_progress(1, 77)
    assert _step_events(events)[-1].label.startswith("vector data")


def test_autopatch_event_clamps_and_forwards(session):
    s, events = session
    s.autopatch_begin(["KBNA"])
    s.autopatch_event("KBNA", 3, 4, "solving", "run")
    s.autopatch_event("KBNA", 9, 4, "over", "run")
    s.autopatch_event("KBNA", 1, 0, "zero-total", "run")
    ap = [e for e in events if isinstance(e, EV.AutoPatchProgress)]
    assert (ap[0].airport, ap[0].done, ap[0].total) == ("KBNA", 3.0, 4.0)
    assert s._autopatch_state["frac"]["KBNA"] == 0.0   # zero total tolerated
    assert all(f <= 1.0 for f in s._autopatch_state["frac"].values())


def test_ui_utils_routes_to_session(session):
    """O4_UI_Utils module functions reach the active session — the seam
    every pipeline module already uses (spec §6)."""
    s, events = session
    UI.auto_patch_begin(["KBNA"])
    assert any(isinstance(e, EV.AutoPatchBegin) for e in events)
    UI.auto_patch_progress("KBNA", 1, 2, "solving", "run", eta_total_s=120.0)
    ap = [e for e in events if isinstance(e, EV.AutoPatchProgress)][-1]
    assert ap.eta_total_seconds == 120.0


def test_run_eta_uses_autopatch_model(session):
    """The auto-patch model's live total estimate drives the run clock
    while auto-patch owns the vector step (the previously-discarded
    signal the 2026-07-15 estimation rework leverages)."""
    s, events = session
    s._eta = SESSION._EtaTracker(
        [(48, -6)], SESSION.plan_steps(True, True, False),
        {(48, -6): {"vector": 30.0, "mesh": 10.0, "masks": 5.0,
                    "imagery": 20.0}})
    s._eta.step_started((48, -6), "vector")
    s.autopatch_begin(["KBNA", "KJWN"])
    s.autopatch_event("KBNA", 0.1, 1.0, "solving", "run",
                      eta_total_seconds=300.0)
    s.autopatch_event("KJWN", 0.1, 1.0, "solving", "run",
                      eta_total_seconds=100.0)
    remaining = s._eta.remaining()
    # ~400 s of auto-patch remain + the 35 s of future steps; far above
    # what the static vector estimate (30 s) would have said.
    assert remaining > 300.0
    # An airport finishing removes its share.
    s.autopatch_event("KBNA", 1.0, 1.0, "done", "done")
    assert s._eta.remaining() < remaining


def test_run_eta_autopatch_overrun_keeps_receding(session):
    """An airport that outlives its predicted total must not pin the
    run clock at "almost done" — the live HECA defect (2026-07-17):
    the display sat under a minute for the five minutes the overrun
    lasted.  Overrun remaining grows with elapsed instead."""
    s, events = session
    s._eta = SESSION._EtaTracker(
        [(30, 31)], SESSION.plan_steps(True, True, False),
        {(30, 31): {"vector": 30.0, "mesh": 10.0, "masks": 5.0,
                    "imagery": 20.0}})
    s._eta.step_started((30, 31), "vector")
    s.autopatch_begin(["HECA"])
    s.autopatch_event("HECA", 0.5, 1.0, "solving", "run",
                      eta_total_seconds=60.0)
    # Simulate the airport running 360 s against its 60 s prediction.
    s._eta.autopatch["HECA"][0] -= 360.0
    remaining = s._eta.remaining()
    # 0.5 × 300 s overrun + 35 s of future steps — nowhere near zero.
    assert 175.0 < remaining < 195.0


def test_set_parallel_siblings_updates_environment(session, monkeypatch):
    """The jsonl "siblings" command lands here: the child's Auto slot
    resolutions read the count from the environment."""
    from O4_Parallel_Utils import PARALLEL_SIBLINGS_ENVIRONMENT_KEY
    import os

    s, events = session
    monkeypatch.setenv(PARALLEL_SIBLINGS_ENVIRONMENT_KEY, "2")
    assert s.set_parallel_siblings(1) is True
    assert os.environ[PARALLEL_SIBLINGS_ENVIRONMENT_KEY] == "1"
    s.set_parallel_siblings(0)   # floor at one
    assert os.environ[PARALLEL_SIBLINGS_ENVIRONMENT_KEY] == "1"


def test_run_eta_live_rate_engages_on_slow_steps(session, monkeypatch):
    """The old fixed 20 s sample window could never accumulate the
    0.5 % gain an hours-long download step needs, so the live rate
    NEVER engaged there and the estimate fell back to the (broken)
    model figure — the "inaccurate while downloading" defect.  The
    window now widens until it carries a measurable rate."""
    s, events = session
    s._eta = SESSION._EtaTracker(
        [(30, 31)], SESSION.plan_steps(True, True, False),
        {(30, 31): {"imagery": 300.0}})
    clock = [1000.0]
    monkeypatch.setattr(SESSION.time, "time", lambda: clock[0])
    s._eta.step_started((30, 31), "imagery")
    # Download bar: 0.1 % every 10 s — a near-three-hour step.
    percent = 0.0
    for _ in range(12):
        clock[0] += 10.0
        percent += 0.1
        s._eta.percent_sample(2, percent)
    remaining = s._eta._current_step_remaining()
    # Live rate 0.01 %/s: (100 − 1.2) / 0.01 ≈ 9880 s — nowhere near
    # the 300 s model estimate the old window fell back to.
    assert remaining == pytest.approx((100.0 - percent) / 0.01, rel=0.01)


def test_run_eta_slowest_concurrent_bar_wins(session, monkeypatch):
    """The imagery step's activities run concurrently, and the FAST
    early DSF-render motion must not drown out the slow download rate
    — extrapolating the blended step percent did exactly that, and
    the display then climbed for minutes ("counting up") as the
    transient aged out.  Each bar rates itself; the slowest wins,
    and a finished bar prices as zero."""
    s, events = session
    s._eta = SESSION._EtaTracker(
        [(30, 31)], SESSION.plan_steps(True, True, False),
        {(30, 31): {"imagery": 300.0}})
    clock = [1000.0]
    monkeypatch.setattr(SESSION.time, "time", lambda: clock[0])
    s._eta.step_started((30, 31), "imagery")
    # DSF render (bar 1) races to done while downloads (bar 2) crawl.
    render = downloads = 0.0
    for _ in range(20):
        clock[0] += 5.0
        render = min(render + 10.0, 100.0)
        downloads += 0.1
        s._eta.percent_sample(1, render)
        s._eta.percent_sample(2, downloads)
    remaining = s._eta._current_step_remaining()
    # Downloads gained 2 % in 100 s → (100 − 2) / 0.02 = 4900 s; the
    # finished render bar votes 0 and must not shrink the figure.
    assert remaining == pytest.approx(4900.0, rel=0.05)


def test_run_eta_overrun_step_estimate_keeps_receding(session):
    """Same guarantee on the plain per-step estimate path (no
    auto-patch signal): a step 500 s into a 30 s prediction reads as
    receding overrun, not as finished."""
    s, events = session
    s._eta = SESSION._EtaTracker(
        [(30, 31)], SESSION.plan_steps(True, True, False),
        {(30, 31): {"mesh": 30.0, "masks": 5.0, "imagery": 20.0}})
    s._eta.step_started((30, 31), "mesh")
    s._eta.step_started_at -= 500.0
    remaining = s._eta.remaining()
    # 0.5 × 470 s overrun + the 25 s of future steps.
    assert 255.0 < remaining < 265.0
