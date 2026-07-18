"""Persistent cross-session run ledger — skip repeat verification runs.

Runs a command and records (code-tree hash, command, O4_* env, exit code,
duration, output tail) in an append-only JSONL ledger.  If the EXACT same
command already ran at the EXACT same code state and PASSED, the run is
skipped and the prior result is reported instead — so sessions stop paying
for test suites and airport builds that were already green on an identical
tree (owner directive 2026-07-18).

Usage:
    venv/bin/python tools/run_with_ledger.py [options] -- <command> [args...]

Options:
    --force            run even if a passing identical run is recorded
    --artifact PATH    (repeatable) after a successful run, record PATH's
                       sha256; for .osm patches also a body sha256 that
                       excludes the volatile provenance header line, so
                       byte-identity A/Bs can compare against the ledger
                       instead of rebuilding the reference side
    --history N        show the last N ledger records for this command
                       (any tree state) and exit without running
    --label TEXT       free-text note stored with the record

What keys a run (any difference ⇒ cache miss):
    * a git tree hash over the code-relevant paths (src/, tests/, tools/,
      conftest.py, pytest.ini, Ortho4XP*.py, requirements*.txt) including
      uncommitted changes — computed with a TEMPORARY git index, never the
      shared one (parallel sessions share the real index);
    * the full argv;
    * every O4_* environment variable plus PYTEST_ADDOPTS.

Only PASSING (exit 0) runs are ever reused; failures always re-run.

Do NOT wrap wall-time benchmarks (tools/check_build_time.py --run,
tools/profile_airport_build.py): a timing number from last week is not a
timing number for this change.  Correctness runs only.

Ledger location: tools/run_ledger.jsonl (gitignored, machine-local;
override with O4_RUN_LEDGER_PATH).  Concurrent appends are flock-guarded.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Paths whose content can change test/build outcomes.  Docs, scratch and
#: memory churn must NOT invalidate recorded results.
CODE_PATHS = ("src", "tests", "tools", "conftest.py", "pytest.ini",
              "Ortho4XP.py", "Ortho4XP_Qt.py", "requirements.txt")

#: The one file that must never key the tree hash: the ledger itself
#: (it lives under tools/ and grows on every run).
LEDGER_BASENAME = "run_ledger.jsonl"


def default_ledger_path() -> str:
    return os.environ.get(
        "O4_RUN_LEDGER_PATH",
        os.path.join(REPO_ROOT, "tools", LEDGER_BASENAME))


def code_tree_hash(repo_root: str | None = None) -> str:
    """Content hash of the code-relevant working tree, uncommitted changes
    included.  Uses a temporary GIT_INDEX_FILE so the shared real index is
    never touched (parallel sessions share it — memory ruling).  Gitignored
    files (__pycache__, the ledger itself) are excluded by ``git add``'s
    normal ignore rules, keeping the hash stable across imports and runs."""
    if repo_root is None:
        repo_root = REPO_ROOT
    existing = [p for p in CODE_PATHS
                if os.path.exists(os.path.join(repo_root, p))]
    with tempfile.NamedTemporaryFile(prefix="o4_ledger_index_") as tf:
        env = dict(os.environ, GIT_INDEX_FILE=tf.name)
        subprocess.run(["git", "read-tree", "HEAD"], cwd=repo_root, env=env,
                       check=True, capture_output=True)
        subprocess.run(
            ["git", "add", "-A", "--"] + existing,
            cwd=repo_root, env=env, check=True, capture_output=True)
        tree = subprocess.run(
            ["git", "write-tree"], cwd=repo_root, env=env, check=True,
            capture_output=True, text=True).stdout.strip()
    return tree


def relevant_env() -> dict:
    keys = sorted(k for k in os.environ
                  if k.startswith("O4_") or k == "PYTEST_ADDOPTS")
    return {k: os.environ[k] for k in keys
            if k != "O4_RUN_LEDGER_PATH"}


def run_key(tree: str, argv: list, env: dict) -> str:
    payload = json.dumps({"tree": tree, "argv": argv, "env": env},
                         sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def artifact_hashes(path: str) -> dict:
    entry = {"path": path}
    with open(path, "rb") as f:
        data = f.read()
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    if path.endswith(".osm"):
        # Body hash excluding the provenance header line (build timestamp
        # and dirty-diff provenance legitimately differ run to run).
        lines = data.split(b"\n")
        body = b"\n".join(line for line in lines
                          if b"o4_provenance" not in line)
        entry["body_sha256"] = hashlib.sha256(body).hexdigest()
    return entry


def load_records(ledger_path: str) -> list:
    if not os.path.exists(ledger_path):
        return []
    records = []
    with open(ledger_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue        # torn concurrent write: skip, never crash
    return records


def append_record(ledger_path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    with open(ledger_path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(record, sort_keys=True) + "\n")
        fcntl.flock(f, fcntl.LOCK_UN)


def describe(record: dict) -> str:
    when = record.get("finished_at_iso", "?")
    dur = record.get("duration_s", 0.0)
    exit_code = record.get("exit_code")
    label = record.get("label") or ""
    lines = [f"  ran {when}, exit {exit_code}, {dur:.1f}s"
             + (f"  [{label}]" if label else "")]
    for art in record.get("artifacts", ()):
        body = art.get("body_sha256")
        lines.append(f"  artifact {art['path']} sha256={art['sha256'][:16]}…"
                     + (f" body={body[:16]}…" if body else ""))
    tail = record.get("output_tail", "")
    if tail:
        lines.append("  --- recorded output tail ---")
        lines.extend("  " + line for line in tail.splitlines()[-8:])
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        usage="run_with_ledger.py [options] -- command [args...]")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--history", type=int, default=0)
    parser.add_argument("--label", default="")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command and not args.history:
        parser.error("no command given (separate with --)")

    ledger_path = default_ledger_path()
    records = load_records(ledger_path)

    if args.history:
        matches = [r for r in records if r.get("argv") == command] \
            if command else records
        for record in matches[-args.history:]:
            print(f"[ledger] tree={record.get('tree', '?')[:12]}")
            print(describe(record))
        if not matches:
            print("[ledger] no prior records for this command")
        return 0

    tree = code_tree_hash()
    env = relevant_env()
    key = run_key(tree, command, env)

    if not args.force:
        passing = [r for r in records
                   if r.get("key") == key and r.get("exit_code") == 0]
        if passing:
            latest = passing[-1]
            print(f"[ledger] HIT — identical command already PASSED at this "
                  f"exact code state (tree {tree[:12]}); skipping "
                  f"(--force to rerun).")
            print(describe(latest))
            return 0

    print(f"[ledger] MISS (tree {tree[:12]}) — running: "
          + " ".join(command))
    started = time.time()
    proc = subprocess.Popen(command, cwd=REPO_ROOT,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    tail: list = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        tail.append(line)
        if len(tail) > 200:
            del tail[:100]
    exit_code = proc.wait()
    finished = time.time()

    record = {
        "key": key,
        "tree": tree,
        "argv": command,
        "env": env,
        "label": args.label,
        "exit_code": exit_code,
        "duration_s": round(finished - started, 2),
        "started_at": started,
        "finished_at_iso": time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.localtime(finished)),
        "output_tail": "".join(tail)[-4000:],
    }
    if exit_code == 0 and args.artifact:
        arts = []
        for path in args.artifact:
            try:
                arts.append(artifact_hashes(path))
            except OSError as exc:
                print(f"[ledger] WARN: artifact {path} unreadable ({exc})")
        record["artifacts"] = arts
    append_record(ledger_path, record)
    print(f"[ledger] recorded exit {exit_code} "
          f"({record['duration_s']:.1f}s) → {ledger_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
