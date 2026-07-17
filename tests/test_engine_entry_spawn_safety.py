"""The engine transport dispatch must be spawn-safe (2026-07-17).

Auto-patch builds airports through a spawn-context ProcessPoolExecutor
(plus a Manager).  Spawn helpers re-import ``Ortho4XP.py`` as
``__mp_main__`` with the parent's ``sys.argv`` restored — inside a
parallel-build engine child that argv contains ``--engine-jsonl``, and
an unguarded module-level dispatch turned the would-be Manager into a
second engine server blocked on its pipe: the Manager handshake never
completed and a live 3-tile build wedged at zero CPU with no progress.

Two guards here: a fast AST check pinning the ``__name__`` condition,
and a subprocess re-import reproducing exactly what multiprocessing
spawn does.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys

ORTHO4XP_PATH = os.path.join(
    os.path.dirname(__file__), "..", "Ortho4XP.py")


def test_engine_dispatch_condition_carries_the_name_guard():
    with open(ORTHO4XP_PATH) as script:
        source = script.read()
    tree = ast.parse(source)
    dispatch_conditions = [
        ast.get_source_segment(source, node.test)
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "--engine-jsonl" in ast.get_source_segment(source, node.test)
    ]
    assert dispatch_conditions, "engine transport dispatch not found"
    for condition in dispatch_conditions:
        assert "__name__" in condition, (
            "the --engine-jsonl dispatch must be __name__-guarded: "
            "multiprocessing spawn helpers re-import Ortho4XP.py as "
            "__mp_main__ with --engine-jsonl still in sys.argv"
        )


def test_mp_main_reimport_completes_instead_of_serving(tmp_path):
    """Re-import Ortho4XP.py the way a spawn helper does; the module
    must finish importing (sentinel printed) rather than entering
    jsonl.serve (which would consume stdin and sys.exit first)."""
    probe = tmp_path / "spawn_probe.py"
    probe.write_text(
        "import sys, runpy\n"
        "sys.argv = ['Ortho4XP.py', '--engine-jsonl']\n"
        "runpy.run_path(%r, run_name='__mp_main__')\n"
        "print('IMPORT-COMPLETED')\n" % os.path.abspath(ORTHO4XP_PATH)
    )
    completed = subprocess.run(
        [sys.executable, str(probe)],
        input=b"", capture_output=True, timeout=180,
        cwd=os.path.dirname(os.path.abspath(ORTHO4XP_PATH)),
    )
    assert b"IMPORT-COMPLETED" in completed.stdout, (
        "spawn-style re-import entered the engine transport instead of "
        "completing the module import; stderr:\n"
        + completed.stderr.decode(errors="replace")[-2000:]
    )
