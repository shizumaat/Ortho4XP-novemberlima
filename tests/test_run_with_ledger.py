"""Hermetic tests for tools/run_with_ledger.py (persistent run ledger).

All tests run in a throwaway git repo under tmp_path with the ledger
redirected via O4_RUN_LEDGER_PATH — no network, no airport builds, no
dependence on the real repo's state.
"""
import json
import os
import subprocess
import sys

import pytest

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "tools", "run_with_ledger.py")


@pytest.fixture()
def ledger_repo(tmp_path, monkeypatch):
    """A minimal git repo shaped like the real one (src/ + tools/) with the
    ledger tool importable against it via O4_RUN_LEDGER_PATH redirect."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tools").mkdir()
    (repo / "src" / "module.py").write_text("VALUE = 1\n")
    env = dict(os.environ,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "seed"]):
        subprocess.run(cmd, cwd=repo, env=env, check=True,
                       capture_output=True)
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setenv("O4_RUN_LEDGER_PATH", str(ledger))
    return repo, ledger


def _run_tool(repo, extra, command, env_extra=None):
    """Invoke the tool with REPO_ROOT patched to the throwaway repo."""
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    code = (
        "import runpy, sys, importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('rwl', {TOOL!r})\n"
        "rwl = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(rwl)\n"
        f"rwl.REPO_ROOT = {str(repo)!r}\n"
        f"sys.exit(rwl.main({extra!r} + ['--'] + {command!r}))\n"
    )
    return subprocess.run([sys.executable, "-c", code], env=env,
                          capture_output=True, text=True)


def test_miss_runs_and_records(ledger_repo):
    repo, ledger = ledger_repo
    result = _run_tool(repo, [], [sys.executable, "-c", "print('ran')"])
    assert result.returncode == 0
    assert "MISS" in result.stdout and "ran" in result.stdout
    records = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(records) == 1 and records[0]["exit_code"] == 0


def test_identical_rerun_is_skipped(ledger_repo):
    repo, ledger = ledger_repo
    command = [sys.executable, "-c", "print('expensive')"]
    _run_tool(repo, [], command)
    second = _run_tool(repo, [], command)
    assert second.returncode == 0
    assert "HIT" in second.stdout
    assert len(ledger.read_text().splitlines()) == 1   # nothing re-recorded


def test_force_reruns(ledger_repo):
    repo, ledger = ledger_repo
    command = [sys.executable, "-c", "print('again')"]
    _run_tool(repo, [], command)
    forced = _run_tool(repo, ["--force"], command)
    assert "MISS" in forced.stdout or "running" in forced.stdout
    assert len(ledger.read_text().splitlines()) == 2


def test_code_change_invalidates(ledger_repo):
    repo, ledger = ledger_repo
    command = [sys.executable, "-c", "print('x')"]
    _run_tool(repo, [], command)
    (repo / "src" / "module.py").write_text("VALUE = 2\n")   # uncommitted
    second = _run_tool(repo, [], command)
    assert "MISS" in second.stdout
    assert len(ledger.read_text().splitlines()) == 2


def test_o4_env_keys_the_run(ledger_repo):
    repo, ledger = ledger_repo
    command = [sys.executable, "-c", "print('x')"]
    _run_tool(repo, [], command, env_extra={"O4_SOME_GATE": "1"})
    second = _run_tool(repo, [], command, env_extra={"O4_SOME_GATE": "0"})
    assert "MISS" in second.stdout


def test_failure_never_cached(ledger_repo):
    repo, ledger = ledger_repo
    command = [sys.executable, "-c", "import sys; sys.exit(3)"]
    first = _run_tool(repo, [], command)
    assert first.returncode == 3
    second = _run_tool(repo, [], command)
    assert second.returncode == 3
    assert "MISS" in second.stdout          # failed run is not reused


def test_artifact_body_hash_ignores_provenance(ledger_repo, tmp_path):
    repo, ledger = ledger_repo
    osm_a = tmp_path / "a.osm"
    osm_b = tmp_path / "b.osm"
    osm_a.write_text("<?xml?>\n<osm o4_provenance_built='T1'>\n<node/>\n")
    osm_b.write_text("<?xml?>\n<osm o4_provenance_built='T2'>\n<node/>\n")
    _run_tool(repo, ["--artifact", str(osm_a)],
              [sys.executable, "-c", "print('build a')"])
    record = json.loads(ledger.read_text().splitlines()[-1])
    art = record["artifacts"][0]
    assert art["sha256"] != art["body_sha256"]
    # Same body, different provenance line ⇒ same body hash.
    import hashlib
    body_b = b"\n".join(line for line in osm_b.read_bytes().split(b"\n")
                        if b"o4_provenance" not in line)
    assert art["body_sha256"] == hashlib.sha256(body_b).hexdigest()
