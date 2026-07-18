# CLAUDE.md

## Project

Ortho4XP — scenery generation tool for X-Plane: builds base mesh, water masks,
and orthophoto textures per 1°×1° tile.

- **Entry points:** `Ortho4XP.py` (legacy Tkinter GUI + CLI), `Ortho4XP_Qt.py`
  (new PySide6 map-first GUI, in development).
- **Pipeline:** `src/O4_Vector_Map` → `O4_Mesh_Utils` → `O4_Mask_Utils` →
  `O4_Tile_Utils`. The GUI↔core contract is `src/O4_UI_Utils.py` only:
  `progress_bar(nbr, pct)`, the polled `red_flag` cancellation flag, and
  stdout prints. Core modules must never import a GUI toolkit.
- **Settings registry:** `src/O4_Cfg_Vars.py` (types, defaults, hints);
  config files are flat `key=value` (`Ortho4XP.cfg`, per-tile
  `Ortho4XP_+XX+YYY.cfg`).
- **Tests:** pytest, `tests/` (`pytest.ini`; use `-n0` for serial debugging).
- **Design docs:** `docs/UI_MODERNIZATION.md` (plan + revisions),
  `docs/specs/` (feature specs), `docs/mockups/` (reviewable HTML mockups).
- The `src/auto_patch/` subsystem has its own `CLAUDE.md`; defer to it there.

## Working style — agent delegation model (token policy)

This repo is worked with a lead-session + subagent model to spend
top-tier-model tokens only where judgment is required:

1. **The lead session (Fable) is architect and reviewer.** It owns
   architecture, public interfaces, UX decisions, integration, and final
   review. It writes the judgment-heavy code itself (core widgets, wiring,
   anything user-facing in copy or behavior).
2. **Delegate mechanical, well-specified work to Opus subagents** (Agent tool
   with `model: "opus"`): leaf-module implementations against a frozen
   interface, test authoring, migrations and sweeps, doc formatting,
   large-file reconnaissance.
3. **Every delegation prompt must include:** exact file paths, the frozen
   public API, acceptance criteria (tests that must pass), and constraints
   (no GUI-toolkit imports in core modules, keep imports light). Agents may
   not change a public interface; if blocked, they report back rather than
   improvise.
4. **Run independent agents in parallel** (single message, multiple Agent
   calls). Verify all agent output by running its tests before integrating.
5. **Never delegate:** interface design, UX copy, destructive operations,
   security-sensitive code, or the final review.
6. **HARD LAW — build-time regressions (owner rulings 2026-07-18; this
   is the canonical text — other documents point here).** Two budgets,
   both COLD and EXCLUDING download time: (a) per-airport auto-patch
   wall ≤ **60 s** (docs/specs/flat-airport-fast-path-spec.md §3.5);
   (b) whole-tile compute ≤ **300 s** (provisional figure, owner may
   revise). Any new code costing ≥ **1 % of the relevant budget**
   (0.6 s / 3 s) must be evaluated by a **Fable 5 optimization agent**
   (lead-session-class model — spawned inheriting a Fable lead's
   session model, never Opus) that considers the WHOLE pipeline and
   whether the increase can be avoided or offset. A change that moves
   a build across its budget — or regresses an already-over-budget
   build by ≥1 % — additionally requires a written explanation and
   **explicit owner approval** before landing. Gated-but-default-on
   code is not exempt. Every implementation-agent brief must include a
   build-time impact statement. Executable check:
   `venv/bin/python tools/check_build_time.py` (baselines:
   `tools/build_time_baselines.json`; owner approvals:
   `tools/build_time_approvals.json`).

## Conventions

- New modules: type hints, docstrings, no `exec`/`eval` (legacy code still
  has them; don't add more).
- New UI work targets the PySide6 app; the Tkinter app is legacy and gets
  fixes only.
- Tests for new modules are mandatory and must run headless
  (`tmp_path`-based, no network, no X-Plane install required).
- **Run ledger (owner 2026-07-18):** correctness verification (pytest,
  airport builds, `check_grade`) goes through `venv/bin/python
  tools/run_with_ledger.py -- <command>`. Results persist across
  sessions in a gitignored ledger keyed by code-tree hash + argv +
  `O4_*` env; an identical already-passing run is reported from the
  ledger instead of re-executed. Check `--history` before repeating an
  expensive run another session may have done. Never wrap wall-time
  benchmarks (`tools/check_build_time.py --run`, profilers) — timing
  must always be measured fresh.
