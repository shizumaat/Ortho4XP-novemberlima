# grade_law consolidation — handover

> ⚠ **SUPERSEDED (2026-06-30 audit) by `grade_law_consolidation_handover_4.md`.**
> Items 1–3,5 landed in later handovers. Still-open carry-forwards (route-band-on-OSM
> check; "audit every check against principle #2" / `_check_plane_gradient`) → **`OPEN_ITEMS.md`**.

**Goal (user-set, 2026-06-28):** one canonical ruleset that BOTH the solver and
the validators use, so output is tuned by editing *rules*, not by chasing three
implementations. The principles, verbatim:

1. The solver is responsible for solving the elevation of **every node** in the
   patch in compliance with **every rule**.
2. We must **not test anything that is not defined in the canonical ruleset**
   (`grade_law`). Every check must map to a rule the solver enforces.
3. Where the solver *should* guarantee something (e.g. every feeder to an apron
   can reach it within grade), **define the rule**, have the solver apply it, and
   make the test **confirm the rule was applied in the patch**.
4. Byte-identical output is the guardrail for *dead-code cleanup only*; for
   *architecture/rule* work, pick the cleanest design and verify against the
   SUITE, re-baselining tests on genuine improvements (see memory
   `byte_identity_vs_clean_architecture`).

## Architecture (target end state)

ONE unified grade graph `G` (`grade_graph.build_unified_graph`), built from the
same `grade_law` rules by every reader. Edges carry a `grade_law.Allowance`
(`cL·Δs∥ + cT·Δs⊥`). Two derived checks on that one graph:

- **per-edge ≤ its Allowance** = the within-shape grade law.
- **transitive cap-distance closure from the runway anchors** = the route-band /
  reach law.

The spine is the backbone of the route graph; the anisotropic `Allowance` carries
curves in BOTH checks. `grade_law.classify_pair` is the single rule for which
pairs are constrained and at what budget.

## DONE (committed on `dev`)

| commit | what |
|---|---|
| `0fddabc` | `grade_law.py` — `PairContext` + `Allowance` + `classify_pair` (the law); `grade_graph.shape_constraints` is the solver-side reader. |
| `e22e39e` | `check_grade` reads the law (soft shapes) — second reader. |
| `e73b4db` | road-frontage relaxation moved INTO the law (`GradeContext.road_zone`); solver+WARN+test apply it. |
| `54bd5d2` | back-edge-ramp relaxation DELETED from check_grade (superseded by `TAXI_SLACK_TERMINALS`) — check_grade has zero test-only relaxations. |
| `28d56ee`,`0bbb097` | `plane_constraints` — rects/runway/terminal on the law too; solver (`build_unified_graph`), in-memory validator, and OSM test all use it. |
| `8feb477` | build-time OSM-patch validation (`verify_and_log`→`run_grade_checks`) is DEBUG-ONLY behind `O4_VERIFY_OSM_GRADE` (default off). |
| `9e16aaa` | **anisotropic Allowance plumbing** — grade-graph edges carry `Allowance`; consumers eval `allow.at(Δs∥,Δs⊥)` / `.flat_cap()`. Behaviorally neutral (flat today). |
| `f41e309` | **`route_field` RETIRED** (the duplicate per-vertex band on a separate centerline graph) — deleted + consumers removed. |

State: within-shape grade and plane shapes are fully one-law (solver = WARN =
test). Suite failure set steady at 21 throughout (the 4 red grade-tested airports
are the genuine M6 solver work). Allowance plumbing is in, so the curve fix is now
a rule change.

## REMAINING (do these, roughly in order)

### 1. Restore the route-band CONFIRMATION on the one graph `G`  ⟵ top priority
`route_field` is gone, so the route-band rule is **enforced** by the solver
(`building_feasibility.reach_band_unified` bounds nodes) but **no longer confirmed
on the patch**. Add the confirmation as a per-vertex band check on `G`:
- `reach_band_unified(layout, G)` gives `band(x,y) → (floor, ceiling)`. Check
  every airside pavement vertex's solved elev against it.
- Build `G` via `solver_primitives._build_node_list` + `grade_graph.build_unified_graph`.
- Match `route_field`'s coverage (it flagged building-frontage points too — high
  terminal vs low runway; those are largely M6 *fundamental* cases).
- Two homes possible: in-memory in `grade_graph_validate` (has the layout), or
  rebuild `G` from OSM in `check_grade` (the purist "confirm on the shipped
  patch"). The spine point (user) argues for the full graph; reach_band needs the
  global spine, which only `G` has — so the OSM path means reconstructing `G` from
  OSM. In-memory is the pragmatic first step; flag the OSM-path as a follow-up.
- Re-baseline `test_pavement_grade` route-band counts (old route_field counts are
  no longer produced) and add/repoint a route-band test to the new check.

### 2. Define the apron FEEDER-REACH rule in `grade_law` (user directive #3)
The solver should guarantee **every feeder taxiway to an apron can reach it within
grade**. Today this is only *surfaced* by `grade_graph_validate.route_reach_violations`
(an apron feeder-contact heuristic), which the per-vertex band does NOT subsume
(an apron can sit at a compromise level inside its intersected band while its
feeders are mutually incompatible — proven this session by a reverted fold-in).
- DEFINE the rule (feeders converge to a shared reachable level) as part of the
  canonical ruleset, have the SOLVER apply it (currently a gap — `test_cyxy_route_reach_zero`
  is xfail), and have the test CONFIRM it on the patch.
- Split feasible (solver should fix → gate it) from fundamental (hard-anchored
  terrain → documented explicit transition, not a gate) — same fundamental-vs-
  feasible split `grade_feasibility_audit` makes.

### 3. The anisotropic CURVE FIX (now purely a rule change)
Plumbing is done (`9e16aaa`). To land: supply real `Δs∥`/`Δs⊥` per edge (project
onto the local spine; the hard part is the longitudinal-reference assignment in
multi-branch junctions) and flip junction/curve edges to anisotropic
`Allowance(cL, cT)`. The route-band closure reads the same Allowance, so curves
compose automatically. See `docs/m4_constraint_graph_findings.md` for the model.

### 4. Audit EVERY remaining check against principle #2 (canonical ruleset)
Each must map to a `grade_law` rule or be retired:
- `check_grade._check_plane_gradient` (triangle perpendicular plane gradient) —
  a TEST-ONLY rule with no solver counterpart. Either add it to `grade_law` (so
  the solver constrains it) or retire it.
- `check_grade` cross-shape proximity / vertex-to-edge / edge-midpoint step —
  continuity checks; the solver guarantees continuity by node-welding. Decide:
  keep as weld-invariant confirmations, or express as rules.
- runway longitudinal grade + vertical curve — caps are config-single-source, but
  the build profile (`runway_regrade`/`redistribute`) and the check are separate
  code. Bring onto the law or document as deliberately separate.

### 5. Cleanup left from the retirements (M5-style)
- `route_ctx` plumbing is now dead: `check_grade.run_checks` `route_ctx` param,
  `verification.route_ctx_from_layout`, the `route_ctx=` arg in
  `test_pavement_grade` and `verification.run_grade_checks`. Remove.
- `elevation.py` `_rf_runway_rings/_rf_check_pts/_rf_check_src` collection is dead.
- config gates likely dead after route_field: `ROUTE_FIELD_MODEL`,
  `ROUTE_FIELD_LOCAL_WINDOW_M`, `ROUTE_NOISE_FRAC` (verify, then retire).
- `grade_feasibility_audit._route_band_intervals` degrades to `{}` now (guarded
  import) — repoint to the band-on-`G` from item 1.
- `Allowance.flat_cap()` asserts `is_flat`; once item 3 makes rules anisotropic,
  the `.flat_cap()` reporting sites (within_violations %, check_grade
  `ShapePairConstraint.cap`) need the anisotropic-aware %-report.

## Verification recipe (this codebase)
- venv only; `PYTHONHASHSEED=0 PYTHONPATH=src:.:tests`. Suite:
  `venv/bin/python -m pytest tests/ -q` (~6–9 min). Baseline failure SET is the
  21 in `/tmp/suite_ab/clean.set` (capture fresh if stale).
- Byte-diff (only for dead-code steps): same-path `git stash` A/B in the MAIN repo
  — SPJC/SPLP output is absolute-path-dependent (memory `byte_verify_same_path_ab`).
- Tools: `tools/diff_constraint_graphs.py ICAO` (rulers agree?),
  `tools/grade_feasibility_audit.py ICAO` (fundamental vs feasible),
  `tools/check_grade.py ICAO`, `tools/junction_repair_impact.py`.
- Relevant memory: `grade_law_single_source`, `byte_identity_vs_clean_architecture`,
  `byte_verify_same_path_ab`, `cleanup_consolidation_plan`.
