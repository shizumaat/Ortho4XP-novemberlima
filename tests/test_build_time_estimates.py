"""Prediction-driven progress windows and the parallel run clock
(2026-07-17).

Pure-function tests: :func:`o4_engine.session.reweight_plan_by_seconds`
(percent windows proportional to predicted step seconds) and
:func:`o4_engine.parallel.estimate_remaining_wall_seconds` (the learned
remaining-time estimate that replaced the parallel ticker's dash).
Headless, no subprocesses, no store access.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from o4_engine.parallel import estimate_remaining_wall_seconds  # noqa: E402
from o4_engine.session import plan_steps, reweight_plan_by_seconds  # noqa: E402


class TestReweightPlanBySeconds:
    def test_windows_proportional_to_seconds(self):
        plan = plan_steps(True, True, False)   # vector, mesh, masks, imagery
        estimates = {"vector": 10.0, "mesh": 20.0, "masks": 10.0,
                     "imagery": 60.0}
        reweighted = dict(
            (key, (base, width))
            for (key, base, width) in reweight_plan_by_seconds(plan, estimates)
        )
        assert reweighted["vector"][1] == pytest.approx(0.10)
        assert reweighted["imagery"][1] == pytest.approx(0.60)
        # Bases accumulate in order and the windows tile [0, 1).
        assert reweighted["vector"][0] == pytest.approx(0.0)
        assert reweighted["mesh"][0] == pytest.approx(0.10)
        assert sum(width for (_b, width) in reweighted.values()) \
            == pytest.approx(1.0)

    def test_cached_imagery_shrinks_its_window(self):
        """The whole point: a warm rebuild's imagery window collapses
        instead of occupying the static 60 percent."""
        plan = plan_steps(True, True, False)
        estimates = {"vector": 30.0, "mesh": 60.0, "masks": 30.0,
                     "imagery": 5.0}
        reweighted = dict(
            (key, (base, width))
            for (key, base, width) in reweight_plan_by_seconds(plan, estimates)
        )
        assert reweighted["imagery"][1] < 0.1

    def test_missing_estimates_keep_a_floor(self):
        plan = plan_steps(True, True, True)
        reweighted = reweight_plan_by_seconds(plan, {})
        widths = [width for (_k, _b, width) in reweighted]
        assert all(width > 0 for width in widths)
        assert sum(widths) == pytest.approx(1.0)


class TestEstimateRemainingWallSeconds:
    PROGRAM = ("vector", "mesh")

    def test_queued_tiles_count_their_full_plan(self):
        estimates = {(1, 1): {"vector": 10.0, "mesh": 30.0},
                     (2, 2): {"vector": 10.0, "mesh": 30.0}}
        remaining = estimate_remaining_wall_seconds(
            estimates, self.PROGRAM,
            queued_tiles=[(1, 1), (2, 2)], next_step_index={},
            in_flight_steps={}, now=1000.0, slots=1)
        assert remaining == pytest.approx(80.0)

    def test_parallelism_divides_the_work(self):
        estimates = {(1, 1): {"vector": 10.0, "mesh": 30.0},
                     (2, 2): {"vector": 10.0, "mesh": 30.0}}
        remaining = estimate_remaining_wall_seconds(
            estimates, self.PROGRAM,
            queued_tiles=[(1, 1), (2, 2)], next_step_index={},
            in_flight_steps={}, now=1000.0, slots=2)
        assert remaining == pytest.approx(40.0)
        # Parallelism never exceeds the tiles actually holding work.
        remaining = estimate_remaining_wall_seconds(
            estimates, self.PROGRAM,
            queued_tiles=[(1, 1), (2, 2)], next_step_index={},
            in_flight_steps={}, now=1000.0, slots=8)
        assert remaining == pytest.approx(40.0)

    def test_in_flight_step_credits_elapsed_time(self):
        estimates = {(1, 1): {"vector": 10.0, "mesh": 30.0}}
        remaining = estimate_remaining_wall_seconds(
            estimates, self.PROGRAM,
            queued_tiles=[], next_step_index={(1, 1): 0},
            in_flight_steps={((1, 1), "vector"): 994.0},
            now=1000.0, slots=1)
        # vector: 10 - 6 elapsed = 4; mesh: full 30.
        assert remaining == pytest.approx(34.0)

    def test_overrun_step_floors_at_zero(self):
        estimates = {(1, 1): {"vector": 10.0, "mesh": 30.0}}
        remaining = estimate_remaining_wall_seconds(
            estimates, self.PROGRAM,
            queued_tiles=[], next_step_index={(1, 1): 0},
            in_flight_steps={((1, 1), "vector"): 900.0},
            now=1000.0, slots=1)
        assert remaining == pytest.approx(30.0)

    def test_finished_steps_no_longer_count(self):
        estimates = {(1, 1): {"vector": 10.0, "mesh": 30.0}}
        remaining = estimate_remaining_wall_seconds(
            estimates, self.PROGRAM,
            queued_tiles=[], next_step_index={(1, 1): 1},
            in_flight_steps={}, now=1000.0, slots=1)
        assert remaining == pytest.approx(30.0)

    def test_no_work_left_yields_none(self):
        remaining = estimate_remaining_wall_seconds(
            {}, self.PROGRAM, queued_tiles=[], next_step_index={},
            in_flight_steps={}, now=1000.0, slots=2)
        assert remaining is None
