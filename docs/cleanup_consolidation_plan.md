# auto_patch cleanup & consolidation plan

**Goal:** remove the dead/duplicate code paths left by ~a dozen partially-abandoned
approaches, and collapse the cases where we build "the same thing" two+ ways, so that
the live solver runs on **one** graph, **one** band, **one** validator — with no
behaviour change to the default build until we deliberately choose one.

**Author context (2026-06-27 session):** the route_profile solver is the live path
(`O4_ROUTE_PROFILE_SOLVE=1`); the legacy `unified_jacobi.solve` cascade is bypassed.
We confirmed: two within-shape constraint-pair generators that disagree (OEMA 610 vs
976), two reach-band impls, ~14k lines of bypassed legacy solver, and ~20 junction
"repair" passes patching emission. See [[one_graph_merge_diagnosis]],
[[p5_lockstep_diagnosis]], [[route_profile_one_solve]].

---

## Operating rules (READ FIRST — these are the guardrails)

1. **Builds are deterministic.** Run-to-run output differences are NOT flake — they
   mean something changed (source data or a parallel edit). See [[nondeterminism-cause]].
   So a **byte-identical OSM** before/after is a *valid, strong* verification.
2. **Dead-code deletions must be output-neutral.** Anything reachable only via a
   gate-off branch CANNOT change the default build. Prove it: build the fixtures to OSM
   before the change, again after, `diff` must be empty.
3. **One commit per milestone**, message records the evidence (byte-diff result + suite
   delta). This makes every step independently revertible (`git revert`/`git checkout`).
4. **Never widen the suite's failure set.** Compare failure *sets*, not counts.
5. **Use existing tools — `ls tools/` before writing any probe.** See
   [[use-existing-checks-not-tmp-scripts]]. New reusable probes go in `tools/`, not /tmp.
6. **Do not attempt M6 (solver correctness) autonomously** — it is the open research
   problem, not a cleanup. Stop the autonomous run at the end of M5/M7-mechanical.

## Verification toolkit (the exact commands)

```bash
# venv only; there is no system python. PYTHONHASHSEED=0 is harmless (not required).
BUILD () { PYTHONHASHSEED=0 PYTHONPATH=src:.:tests venv/bin/python -c \
  "from conftest import xplane_root; from auto_patch.pipeline import build_airport_pavement as B; \
   B('$1', xplane_root(), compute_elevations=True).to_osm('$2')"; }     # ~60-90s/airport

# Fixtures to check (representative set): CYXY OEMA HECA SPJC SPLP
# Authoritative within-shape grade validator (THE ruler; tests use it):
venv/bin/python tools/check_grade.py <ICAO>
# Classify violations fundamental(infeasible) vs feasible-but-unenforced:
venv/bin/python tools/grade_feasibility_audit.py <ICAO> [ICAO ...]
# Full suite (baseline = capture failure SET before starting):
venv/bin/python -m pytest tests/ -q
```

**M0 — capture the baseline (do this once, first).** Build all five fixtures to
`/tmp/baseline/<ICAO>.osm`, record the pytest failure set to `/tmp/baseline/suite.txt`,
and record `check_grade`/`grade_feasibility_audit` counts per fixture. Every later
milestone diffs against these.

---

## Milestones

Each milestone: **objective → steps → ACCEPTANCE (measurable) → rollback.**

### M1 — Delete zero-caller orphan modules ✅ DONE 2026-06-27
Deleted `route_profile/{profile,caps,spine}.py` (~397 lines, 0 importers); fixed a
stale doc ref in `grade_graph.py`. ACCEPTANCE met: package imports clean.

### M2 — Delete the legacy `unified_jacobi` solver + its satellites  [AUTONOMOUS]
**Objective:** remove ~14k lines of bypassed legacy solver; route_profile becomes the
only solver.
**Steps:**
1. Identify the elevation-NEUTRAL helpers the live path still imports from
   `unified_jacobi` (currently: `_build_node_list, _seed_elevations, _sample_node_dem,
   _runway_node_set, _build_shape_constraints, _build_level_coupling, _writeback,
   _report` in solve.py; `_runway_edge_pts` in anchors.py; `_open_ring`) + constants
   `_PER_AXIS_JUNCTIONS`, `PAVEMENT_ROLES` (used by verification.py / interior_path.py).
2. Move those helpers+constants into a new `elevation_per_surface/route_profile/primitives.py`
   (or `solver_primitives.py`). Repoint all live imports to it.
3. Delete `unified_jacobi.solve` + its private cascade (`_phase1_hop_priority`,
   `_directional_relief`, `_taxi_corridor_profiles`, `_enforce_within_shape_grade`,
   `_reconcile_level_coupling`, the polish passes, the `O4_CAP_PLANAR` block, etc.),
   then `network_profile.py` and `grade_graph_solve.py` (reachable only from that cascade).
4. In `elevation_per_surface/solver.py`, remove the `O4_ROUTE_PROFILE_SOLVE` gate and the
   `else: unified_jacobi.solve` fallback — route_profile is unconditional.
5. Keep `route_field.route_band_violations` (it's a LIVE validator/WARN, not solver).
**ACCEPTANCE:**
- Every fixture OSM **byte-identical** to `/tmp/baseline/<ICAO>.osm`.
- pytest failure set ⊆ baseline (update any test that imported the deleted
  helpers to the new module — those are *test* edits, allowed).
- `grep -rn "unified_jacobi\|network_profile\|grade_graph_solve\|O4_ROUTE_PROFILE_SOLVE" src/`
  returns only the new primitives module (or nothing).
- `wc -l` of src/auto_patch drops by ~13–14k.
**Rollback:** `git revert` the milestone commit.

### M3 — Retire the legacy reach-band path  [AUTONOMOUS]
**Objective:** one band. `reach_band_unified` (on the unified graph) is already the
default; remove the route-graph band and its bridge.
**Steps:**
1. Delete `building_feasibility.reach_band_sampler` + `_runway_route_contacts` (its
   helpers), `_cap_consistent_band` (solve.py), and the `O4_BAND_ON_UNIFIED_GRAPH`
   gate-off branch in `anchors.reach_band_for` (make unified unconditional).
2. Repoint/remove any remaining `reach_band_sampler` references.
3. Demote `taxi_routing.shared_taxi_route_graph` to a distance-only utility, or delete
   if it has no remaining callers after M2.
**ACCEPTANCE:** fixtures byte-identical to baseline; suite ⊆ baseline;
`grep -rn "reach_band_sampler\|_cap_consistent_band\|O4_BAND_ON_UNIFIED_GRAPH" src/` empty.

### M4 — Unify the two within-shape pair generators  [SEMI-AUTONOMOUS — see note]
**Objective:** the solver enforces EXACTLY the pairs the validator checks. Today
`tools/check_grade.iter_shape_grade_constraints` is an independent reimplementation of
`grade_graph.shape_constraints`; they cross-disagree (OEMA |A|=27396, |B|=72153,
A∩B=11338). This is the root of "956 feasible-but-unenforced".
**Decision (made 2026-06-27): NARROW check_grade to the model.** The B-only excess is
long cross-apron diagonals (median 360m) the solver decouples on purpose
(`_APRON_BODY_CHORD_MAX_M`); per [[apron_spine_grade_model]] the apron grades *locally*
to its edges/spine, so those long diagonals are not regulated. Make `check_grade`
consume one shared pair generator (lift `grade_graph.shape_constraints`' pair-selection
into a module both `grade_graph_validate` and `check_grade` import), incl. the distance
window, per-axis junction rule, and chord-decoupling.
**ACCEPTANCE (the rulers agree):**
- For every fixture: `check_grade` within-shape violation count == `grade_graph_validate`
  within-shape count (build's own WARN). (They differ today: OEMA 976 vs 610.)
- The A△B edge-set diff restricted to soft-airside ≤60m pairs == 0 (write
  `tools/diff_constraint_graphs.py ICAO` to measure — REUSABLE, goes in tools/).
- Suite ⊆ baseline (test_pavement_grade thresholds may need re-baselining ONCE, with the
  new agreed counts — record old/new in the commit).
**NOTE for autonomous run:** this changes *what is counted*, so it is the one milestone
that re-baselines a test. Safe to execute, but flag the count changes clearly in the
commit so a human can sanity-check the modeling choice. If unsure, STOP and leave a
written summary instead of guessing.

### M5 — Retire banked / superseded gates  [AUTONOMOUS]
**Objective:** delete gates whose other branch is dead, and their dead branches.
**Targets (verify each is still default-off / legacy-only first):**
`O4_BUILDING_DEM_ANCHOR` (retired), `O4_APRON_FEASIBLE_LIFT` (RETIRED), `O4_SPLIT_LONG_RECTS`
(not needed), `O4_CONSISTENT_CEILING` (legacy-only after M3), `O4_FRONTAGE_SPINE_RISE`
(banked — confirm with audit numbers before removing; it may still be wanted).
**KEEP:** `O4_ABSORB_RUNWAY_IN_APRON` (blocks KPHX — needs per-airport work, NOT dead).
**ACCEPTANCE:** fixtures byte-identical; suite ⊆ baseline; each removed gate grep-clean.

### M6 — Reach an acceptable solver baseline  [NOT AUTONOMOUS — human/research]
**Objective:** the open problem we've been working — get within-shape violations to an
acceptable level on the single unified graph (the "956 unenforced" after M4 should
collapse since the test stops over-checking; the residual FUNDAMENTAL cases are genuine
terrain (high-terminal-vs-low-runway) → explicit transitions).
**Measurable targets (to confirm with the user, not invent):** spine violations == 0 on
all fixtures (`grade_graph_validate` is_spine); `grade_feasibility_audit` FUNDAMENTAL
count only at documented terrain cases; within-shape (post-M4 agreed ruler) ≤ an
agreed per-airport budget. **Tag the commit `baseline-clean` once met** — this is the
reference all later refactors must match.
**Do NOT attempt this autonomously.** It needs design judgment (which residuals are
explicit-transition vs solver gaps). Stop the overnight run here and hand back.

### M7 — junction_repair: measure, then modularize  [M7a AUTONOMOUS; M7b after M6]
**M7a (diagnostic, anytime):** write `tools/junction_repair_impact.py ICAO` that wraps
each repair pass and reports (#shapes, #nodes, role-counts) delta per pass on CYXY/OEMA/
HECA/SPJC. Output: a table of load-bearing vs no-op passes. ACCEPTANCE: tool exists +
runs; report committed to docs.
**M7b (refactor, needs the M6 baseline):** split `junction_repair.py` (3861 lines, 34
passes) into cohesive submodules (`junction_repair/{absorb,drop,reclassify,vertex_fix,
split}.py`) behind ONE ordered entry `repair_junctions(layout)`; pipeline.py makes ONE
call instead of ~12 scattered imports; consolidate the overlapping families (4 absorb,
4 drop, 3 reclassify) where M7a shows redundancy. ACCEPTANCE: output matches
`baseline-clean` (byte-identical or within a recorded tolerance); `grep -c "import" `
junction_repair in pipeline == 1; no junction_repair submodule > 1500 lines; suite ⊆ baseline.

### M8 — Modularize the remaining giants  [after M6, optional]
`pipeline.py` (4675 — pull inline logic into modules so it's pure orchestration),
`elevation.py` (3607 — separate DEM-loading / solver-caps / validator-glue). Same
output-match + suite acceptance as M7b. Lowest priority.

---

## What an overnight `/goal` run can realistically finish

| milestone | autonomous? | gate |
|---|---|---|
| M0 baseline capture | yes | — |
| M2 delete unified_jacobi | **yes** | byte-identical + suite |
| M3 retire legacy band | **yes** | byte-identical + suite |
| M5 retire banked gates | **yes** | byte-identical + suite |
| M4 unify pair generators | **yes, with care** | rulers agree; flag count changes |
| M7a repair impact tool | **yes** | tool runs |
| M6 acceptable solver output | **NO** — research/judgment | hand back |
| M7b / M8 modularize live code | after M6 only | output-match baseline |

**Realistic overnight outcome:** M2+M3+M5 (≈14k lines of dead code gone, default build
provably unchanged), M4 (one ruler — the build's own check finally equals the grade
test), and M7a (the repair-impact data). That removes essentially all the
*duplicate/dead-path* cruft and gives one graph / one band / one validator — without
touching solver behaviour. M6 (making the output actually good) and the live-code
modularization (M7b/M8) wait for a human-confirmed `baseline-clean` tag.
