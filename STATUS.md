# STATUS — handover (2026-06-30) — ANISOTROPIC EDGES default-ON, clearance pockets, legacy retired

> All three work-streams below are **committed on `dev`** and the tree is clean.
> The anisotropic within-shape grade law is now the **default** build behaviour.
> What's left is solver-quality polish + pre-existing suite reds — NOT new feature
> work. Read `docs/anisotropic_edge_handling_plan.md` (marked DONE) for the model.

Build/verify (no system python): `PYTHONHASHSEED=0 venv/bin/python …`; single build
`build_airport_pavement("CYXY", xplane_root())` (~60–90 s). Suite
`venv/bin/python -m pytest tests/ -q` (~7 min). Fixtures: CYXY, SPJC, SPLP, HECA.
⚠ shapeIDs are build-specific — identify shapes by **ref/coord**, not index.

---
## ✅ SHIPPED THIS SESSION (committed on `dev`, newest first)

- `69c087e` **cleanup: retire `O4_SINGLE_GRADE_GRAPH` + per-axis legacy.**
  `unified_jacobi.py` was already gone; this deletes the dead `=0` path + the
  per-axis machinery it gated (`_PER_AXIS_JUNCTIONS`, `_per_axis_allowance`,
  `_project_to_polyline`, `_collect_junction_axes`, `_smooth_junction_ring_curvature`,
  cap-debulge, the old per-axis WARN audit). −645 lines, 9 files. **CYXY
  BYTE-IDENTICAL** under default. `solver_primitives._build_shape_constraints` now
  always takes the shared `_grade_graph_edges` branch; the legacy
  `_visible_grade_edges` arm is BUILDING-only. `taxi_axes_ll` kept = check_grade's
  CENTERLINE source. (A subagent did the solver-side deletes but missed the
  pipeline.py dangling ripple caller → its byte-identity loop never converged; I
  took over and finished.)
- `8d53837` **clearance: ring enclosed wingtip POCKETS (Pass A2).** A taxi network
  can fully enclose a small non-pavement pocket (a HOLE in the airside union);
  centerline-perpendicular Pass A misses its oblique far edges so sharp terrain
  slipped through (CYXY jct134/148 throat, 9 m rise). Pass A2 rings the full
  perimeter of every hole that is a true wingtip pocket (`hole.buffer(-reach)`
  core ≈ empty → excludes the giant infield). Edge-alt = NEAREST shape's
  `_edge_interp_alt` (the union hole boundary falls in hairline inter-shape gaps
  that `_pav_alt` containment misses). Gate `O4_POCKET_CLEARANCE` (default on).
- `5d2e66d` **aniso-edges P7: `O4_ANISO_EDGES` default ON** (user-approved). Plus
  P0–P6 (`e463b02 bfeb099 0e6a3f0 38ccf36 541c70a 3124a2a c80cd1b`): route chaining
  → `ds_decompose` → cT table → anisotropy BAKED into the per-edge `Allowance` in
  `grade_graph.shape_constraints` (0 site edits; all consumers get it via their
  existing `cap.at(d,0)`) → reach-band agreement → audit oracle → standalone
  check_grade wired with chained routes → default-on. Lockstep test
  `test_solver_validator_same_edge_budgets` green.

## The anisotropic law in one paragraph (for the new session)
A spine / junction-body / apron-blend pair's grade budget is `cL·Δs∥ + cT·Δs⊥`
decomposed against the pair's whole chained ROUTE (`grade_graph.ds_decompose`;
Δs∥ = spine arc). It is computed ONCE in `shape_constraints` and BAKED into the
`Allowance` (`grade_law.Allowance.baked`), so the solver, the in-build validator
(`within_violations`), the standalone `check_grade`, and the feasibility audit all
share one decomposition (proven by the lockstep test). `O4_ANISO_EDGES=0` reverts
to the isotropic `cap·dist` law, byte-identical to pre-feature.

---
## ⚠ OUTSTANDING (follow-ups, NOT plan work)

1. **Residual solver-miss cliffs from default-on.** Default-on is a net win on
   every fixture (within-shape viols + total >8% cliffs DOWN; audit **0
   fundamental**), but the solver redistributes under the more-correct L1 law and
   leaves a FEW new >8% cliffs — **HECA `(-2522,1936)` apron 6.3%→15.8%**, **SPJC
   `(1629,-268)` jct 3.5%→8.3%**, **CYXY `(-325,-429)` jct 7.8%→8.6%**. These are
   solver MISSES (audit says feasible), not law errors — drive them down with
   solver work, not by loosening the law. Measure with
   `grade_graph_validate.within_violations` filtered to >8%; the anisotropic L1
   budget (`cL·Δalong+cT·Δacross`) is the CORRECT max-Δz on a tilted plane.
2. **Pocket-clearance partial coverage.** The user's pocket is fixed, but a few
   OTHER pockets with high terrain still get no cut — `_build_graded_strips` needs
   ≥2 consecutive obstructed stations, so isolated/central obstructions slip (e.g.
   CYXY hole area 2050 @`(-124,-280)`, 9.6 m rise, strips=0). Consider clipping
   high terrain to the whole pocket vs per-edge perpendicular strips.
3. **`O4_POCKET_CLEARANCE` reach** uses the nearest-centerline code letter per
   pocket (over-reaches a code-B pocket bordered near a wider route; clipped to
   non-pavement so harmless, but per-edge reach would be cleaner).

## Pre-existing reds (predate this work — NOT regressions)
`pytest tests/ -q` ≈ 22 failed (gate-on AND gate-off the same set, modulo counts):
`test_apron_with_spine_taxi_on_spine_one_percent_body` (apron body-cap 0.01→0.0133
re-baseline owed), `test_pavement_grade[*]` (cap=0, the solver-miss cliffs above),
`test_route_band_zero`, and the connectivity-refactor geometry-baseline shifts
(`test_junction_invariants`, `test_pavement_geometry::test_pavement_rests_on_source`,
`test_compare_target`). Anisotropy IMPROVES their numbers but can't zero them; they
need their own re-baseline pass. The acceptance gate
`test_single_graph_acceptance.py` (spine=0, lockstep, anti-gaming) is GREEN.

## Optional / not done (deliberate)
- `crosses_spine_fn` cross-crotch skip kept (plan P6 said drop only "if proven
  safe" — the nearest-route model makes it redundant but it was left in).
- The single true X junction (#154) 4-cell behaviour not separately eyeballed (the
  nearest-route Voronoi model + clean audit covered it implicitly).
