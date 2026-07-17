"""Regression tests for ``O4_Parallel_Utils.parallel_execute`` crash handling.

A worker task that raised used to kill its thread silently: the step
finished with "normal exit", zero output, and success still reported.
That is exactly how the masks step (Step 2.5) shipped zero masks on
2026-07-16 — every ``build_mask`` call died on a NameError (the
``masks_custom_extent`` branch referenced the undefined ``custom_mask``
instead of ``custom_array``) and nothing surfaced anywhere.

The contract asserted here: a crashing task marks the run failed
(``parallel_execute`` returns 0) while the remaining queue items still
run, and a healthy queue still succeeds.

Headless: pure threading, no tile, no network.
"""

import os
import queue
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from O4_Parallel_Utils import parallel_execute  # noqa: E402


def _filled_queue(items):
    q = queue.Queue()
    for item in items:
        q.put(item)
    return q


def test_crashing_task_fails_the_run_but_processes_remaining_items():
    processed = []

    def task(n):
        if n == 2:
            raise NameError("name 'custom_mask' is not defined")
        processed.append(n)
        return 1

    result = parallel_execute(task, _filled_queue([(1,), (2,), (3,)]), 1)

    assert result == 0
    assert processed == [1, 3]


def test_healthy_queue_still_reports_success():
    processed = []

    def task(n):
        processed.append(n)
        return 1

    result = parallel_execute(task, _filled_queue([(1,), (2,), (3,)]), 2)

    assert result == 1
    assert sorted(processed) == [1, 2, 3]
