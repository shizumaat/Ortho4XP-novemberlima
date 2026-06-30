# Implementation Plan — Fully Anisotropic Edge Handling

> **STATUS 2026-06-29 (gate `O4_ANISO_EDGES`, default OFF):**
> - **P0** ✅ `e463b02` — route chaining (`TaxiCenterline.route_line`, `RouteChain`,
>   `GradeContext.routes`, `Centerline.route_idx`); `tools/probe_route_chains.py`.
> - **P1** ✅ `e463b02` — `grade_graph.ds_decompose`; unit tests.
> - **P2** ✅ `bfeb099` — `config.taxi_transverse_cap_for_letter` (A/B 2 %), STANDARDS.
> - **P3** ✅ `0e6a3f0` — anisotropy BAKED into the per-edge `Allowance` inside
>   `shape_constraints` (all 5 sites get it via their existing `cap.at(d,0)`, 0 site
>   edits); lockstep budget test. CYXY gate-on within body 319→268, >8% cliffs 28→24
>   ZERO new (4 resolved).
> - **P4** ✅ `38ccf36` — reach band agrees with the law (junction full-cap perp +
>   multi-route ceiling); CYXY route_band 159→145. Gate-off byte-identical.
> - **P5** ✅ `541c70a` — feasibility-audit edge weight = the law's `c.allowance`;
>   CYXY 0 fundamental, POCS resid ≈ 0.
> - **P6** ⚠️ functional core ✅ `3124a2a` — standalone `check_grade` wired with the
>   chained routes (gate-on within 362→303, agrees with the solver). **DEFERRED:**
>   the legacy per-axis CODE DELETION (`_per_axis_allowance`, `taxi_axes_ll`,
>   `_PER_AXIS_JUNCTIONS`, `_project_to_polyline`, cT-discard, `flatten_pairs`) —
>   `_PER_AXIS_JUNCTIONS` still gates active solver code (`solver_primitives.py:832`)
>   + the retired `unified_jacobi` path (`elevation.py:3020-3157`); needs dead-code
>   verification, not a byte-identical trivial delete.
> - **P7** 🚧 docs corrected (`grade_law` docstring, `elevation_solver.md`). Default-on
>   pending all-fixture gate-on cliff validation; note "full suite green" is gated by
>   PRE-EXISTING failures (`test_pavement_grade` cap=0, `test_route_band_zero`) that
>   the anisotropy IMPROVES but doesn't resolve — out of this plan's scope.



**Goal:** make the within-shape grade law genuinely anisotropic — longitudinal cap
`cL` along a taxi route, transverse cap `cT` across it — instead of the current
isotropic shortcut where every evaluation passes `Δs⊥ = 0` and uses straight-line
(chord) distance. The motivating failure is the **rising curve at junctions**: a
route turns and climbs through a junction, the spine arc ≫ its chord, and the
chord-based budget false-flags the climb.

This doc is written to be executed phase-by-phase by an implementing agent. Each
phase has an explicit **Done-when** checklist with verification commands. Verify
everything with:

```
cd /Users/noah/Ortho4XP-novemberlima
PYTHONHASHSEED=0 PYTHONPATH=src:.:tests venv/bin/python ...
```

Build a fixture once via `tools/_diag.build("CYXY")`; full suite is
`venv/bin/python -m pytest tests/ -q`. "Byte-identical" = same-path stash A/B
(memory `byte_verify_same_path_ab`): stash the change, build the four fixtures to
OSM, pop, rebuild, `diff` — NOT a worktree baseline.

---

## 0. Reality corrections (start here)
- `docs/elevation_solver.md` still names the retired `unified_jacobi.py::solve()`.
  The active solver is `elevation_per_surface/route_profile/solve.py::
  solve_route_profile` (+ `one_solve.py`, `building_feasibility.py`). Fix this doc
  in the final phase.
- `grade_law.shape_constraints`/`classify_pair` already thread an `Allowance`
  object per edge (`grade_graph.py:538`, `grade_law.py:209`); every consumer just
  collapses it via `cap.at(d, 0.0)` / `cap.flat_cap()`. **Only `Allowance.flat`
  is ever constructed** — `cT == cL` always today.

## 1. Current state — the isotropic shortcut (cite file:line)
- `grade_law.Allowance(cL, cT)` (`grade_law.py:87-110`): `at(Δs∥, Δs⊥) = cL·Δs∥ +
  cT·Δs⊥`. Note: with `cT==cL` this is `cL·(Δs∥+Δs⊥) ≥ cL·d`, so **passing a real
  decomposition is NOT behaviour-neutral** even when flat — it loosens on curves.
  That is the intended fix; plan the phases accordingly (decomposition lands as a
  measured behaviour change, not a byte-identical refactor).
- Five evaluation sites collapse to `cL·d` via `.at(d, 0.0)`:
  1. `solver_primitives.py:598` `_grade_graph_edges` — apron/junction body solve
  2. `building_feasibility.py:171` `_build_skeleton_band` — skeleton reach band
  3. `grade_graph_validate.py:152` `within_violations` — as-built validator
  4. `route_profile/solve.py:198` — solver own-graph feasibility projection
  5. `check_grade.py:941/947` `iter_shape_grade_constraints` — OSM test + audit
- Downstream, `budget = cap·d` is baked into Dijkstra reach envelopes
  (`one_solve.py:_reach`, `building_feasibility.reach_band_unified`) and
  Gauss-Seidel/POCS clamps — all assume one scalar per edge (fine: still static).

## 2. Groundwork already present (~80%)
- `Allowance` type + `PairContext` model `cL·Δs∥ + cT·Δs⊥` and thread an object end
  to end.
- `_project(cl, x, y)` (`grade_graph.py:291`) already returns `(arc_pos, perp_dist)`
  — the exact decomposition primitive.
- `_spine_membership` (`grade_graph.py:313`) maps each ring vertex → the
  centerline(s) it lies on; `_nearest_centerline` (`:397`) returns the nearest
  centerline's `(dist, cap, unit_tangent)`.
- `_apron_edge_cap` (`:419-445`) already projects an apron edge onto the centerline
  tangent (along/perp) — proves the decomposition works in this code.
- The CANONICAL anisotropic allowance already exists in the LEGACY path:
  `verification._per_axis_allowance` / `check_grade.py:549-601` compute
  `Δs∥ = long_arc`, `Δs⊥ = √(sep² − long_chord²)`, `allow = cL·Δs∥ + cT·Δs⊥`. Port
  its math; it is gated off (`_PER_AXIS_JUNCTIONS`) and its `cT` is discarded
  (`check_grade.py:786`).
- `verification.taxi_axes_ll` (`:868`) is the only place a non-equal `(cL,cT)` is
  authored: A/B → `(0.03, 0.02)`, else `(0.015, 0.015)` — the de-facto cT spec.
- Per-segment caps live: `Centerline.seg_caps` + `cap_at(arc)`.

## 3. The junction decomposition model (CYXY-validated 2026-06-29)

### 3a. Junctions are the PRIMARY target, not an isotropic exclusion
A junction has **no spine through its middle** — `junction_spine` slices it so each
taxi centerline runs **along an edge**, with spine nodes ON that edge
(`grade_graph.py` docstring). Everything else (the opposite/outer edge, the
interior) is **body**, today graded **all-pair, flat, isotropic** at the junction
cap. The curve lives here: a straight rect is a flat plane and never curves; the
route turns and climbs *through the junction*, so the sliced spine edge is an arc
and the **other edge has to climb with it**. Leaving junction body isotropic
(chord-distance) defeats the fix at the one place it is for.

### 3b. CYXY measured junction degree (by distinct route NAME)
`tools` probe over `layout.shapes` junctions vs `apt_taxi_centerlines` names:

| routes | count | shape |
|---|---|---|
| 0 | 17 | inherit cap from neighbour |
| 1 | 60 | one route curving through |
| 2 | 23 | T / simple crossing |
| 3 | 6 | **Y** |
| 4 | 1 | **X** |

**True X/Y is rare: 7 of ~124 junctions.** The naive per-*piece* spine count
inflated this to "up to 13 spines" because bend-split children of ONE route count
separately (see 3d). Reference Y for tests: junction `#108`
(`60.717807,-135.076265`), routes `A`,`F`,`?` (the U12 lane), converging at local
`(-477,861)`; its entire outer arc (ring v1–v16) is `body`, F along one edge, A
along another. The single X: junction `#154` (`60.701086,-135.062189`, area 2567).

### 3c. Crotch = nearest-route Voronoi cell (no crotch geometry to build)
Grade each junction body vertex by decomposing it against its **NEAREST route**.
On #108 the outer-arc vertices nearest the top project onto F, those nearest the
west onto A — the nearest-route cells ARE the crotches (3 for a Y, 4 for an X), with
no explicit crotch-splitter. So:
- body pair, **same** nearest route → anisotropic `cL·Δs∥ + cT·Δs⊥` (Δs∥ = arc).
- body pair, **different** nearest routes (spans the convergence) → keep the
  existing `crosses_spine_fn` SKIP (`grade_law.py:171`): the climb is carried via
  the spine through the convergence, not the direct diagonal.
`_nearest_centerline` already finds the cell; it must additionally return `_project`
arc/perp, not only the tangent.

### 3d. NEW required infra — chain bend-split pieces into whole routes
`ctx.centerlines` are bend-split *pieces* (~20–30 m). The fix is `Δs∥ = spine arc`,
but a junction body↔spine pair that straddles two pieces of the SAME route sees
different centerline indices → `_shared_centerline` returns False → flat body, and
arc-length **resets at every bend** — exactly where the route curves (the junction).
So the decomposition must project against the **chained route polyline** (group
pieces by `TaxiCenterline.name` + shared-endpoint connectivity). This also fixes the
spine-pair arc credit generally.

### 3e. Solver impact stays benign
Each edge budget is still a constant scalar computed once from static geometry, so
Dijkstra reach + Gauss-Seidel/POCS remain valid — **no new solver**. The pure spine
is 1-D (Δs⊥≈0) so cT is a no-op there; cT only bites off-spine junction-body / apron-
blend edges.

## 4. What retires once anisotropy lands
- Legacy per-axis validator path: `verification._per_axis_allowance`,
  `_project_to_polyline`, `taxi_axes`/`taxi_axes_ll`, `_PER_AXIS_JUNCTIONS`,
  `_grade_context_from_osm`'s cT-discard (`check_grade.py:786`).
- The `crosses_spine_fn` cross-crotch skip MAY simplify (now well-defined per
  nearest route) — but keep it until P6 proves it's safe to drop.
- `grade_graph.flatten_pairs` (`:1032`) — appears dead; confirm + remove.

---

## Phased plan with measurable milestones

Gate every behaviour change behind an `O4_*` env flag (convention: `config.py`,
`O4_SINGLE_GRADE_GRAPH` pattern), default OFF until its phase's Done-when passes.
Fixtures: CYXY, SPJC, SPLP, HECA.

### Phase 0 — Route-chaining infrastructure
**Goal:** expose whole routes (chained bend-split pieces) without using them yet.
**Changes:** in `apt_dat_reader`/`grade_graph.build_context`, group
`apt_taxi_centerlines` pieces by `.name` + shared endpoints into a chained polyline;
attach to each `Centerline` a reference to its chained-route arc (or build a
`ctx.routes` list + per-centerline route index). Do NOT change any caller.
**Done when (all measurable):**
- [ ] New `tools/probe_route_chains.py` (or a unit test) prints, for CYXY junction
      #108, route `F` as ONE polyline whose arc length == Σ of its bend-split piece
      lengths (±1e-6).
- [ ] At CYXY, the count of distinct chained routes touching each junction matches
      the degree table in §3b (0→17, 1→60, 2→23, 3→6, 4→1).
- [ ] CYXY/SPJC/SPLP/HECA build to OSM **byte-identical** vs pre-phase (chaining
      unused). Gate: none needed (pure addition).

### Phase 1 — `ds_decompose` helper + unit coverage (unused)
**Goal:** the shared decomposition primitive, tested, not yet wired.
**Changes:** add `grade_graph.ds_decompose(pa, pb, route) -> (Δs∥, Δs⊥)` using
`_project` against a chained route: `Δs∥ = |arc_a − arc_b|`, `Δs⊥ =
√(max(0, dist² − long_chord²))`. Extend `_nearest_centerline` (or add a sibling) to
return the nearest **chained route** + arc/perp, not only the tangent.
**Done when:**
- [ ] Unit test `tests/test_grade_graph.py`: a STRAIGHT route pair → `(d, 0.0)`
      within 1e-6 (so straight surfaces will be unaffected later).
- [ ] Unit test: a CURVED route pair (synthetic arc, or CYXY #108 F across two bend
      pieces) → `Δs∥ == arc > chord` and `Δs⊥ < 0.5 m`.
- [ ] `ds_decompose` is the ONLY decomposition implementation referenced by the
      five sites in later phases (no per-site copy). Not yet called → all four
      fixtures byte-identical.

### Phase 2 — cT table in config + STANDARDS
**Goal:** author the transverse caps; unused.
**Changes:** `config.py` `taxi_transverse_cap_for_letter(letter)` → A/B `0.02`,
C–F = `taxi_grade_cap_for_letter` (isotropic). Add the cT rows to `docs/STANDARDS.md`
with citations (ICAO Annex 14 §3.9 transverse slope; EASA CS-ADR-DSN.D.265/.280 —
confirm exact values with the user, see §7).
**Done when:**
- [ ] `taxi_transverse_cap_for_letter('C') == taxi_grade_cap_for_letter('C')` and
      `('A') == 0.02`. Unit-tested.
- [ ] STANDARDS.md has a cT column with a citation per code letter.
- [ ] Unused → four fixtures byte-identical.

### Phase 3 — wire anisotropy: classify_pair + the five sites (the behaviour change)
**Goal:** spine + junction-body + apron-blend pairs evaluate `cL·Δs∥ + cT·Δs⊥`
against the nearest chained route; cross-crotch pairs stay skipped.
**Changes:** `classify_pair` returns `Allowance(cL, cT)` for spine/junction-body/
apron-blend (cT from the table; junction body decomposes against the body vertex's
nearest chained route — §3c). All five sites call `ds_decompose` and pass
`(Δs∥, Δs⊥)`. Gate `O4_ANISO_EDGES`.
**Done when (measurable):**
- [ ] STRAIGHT-ONLY invariant: a fixture/synthetic airport with only straight
      taxiways + aprons (or assert per-pair) builds byte-identical (decomposition
      returns `(d,0)` there).
- [ ] CYXY: within-shape body violations AT THE 7 Y/X JUNCTIONS (#108 + the other
      5 Y + #154 X) **drop** — record before/after with
      `grade_graph_validate.within_violations` filtered to those junction polygons;
      target = the curve-climb false-flags removed (expect the rising-curve cluster
      at #108 → 0). Report the exact numbers.
- [ ] No NEW within-shape cliffs (>8% bucket) anywhere on the four fixtures vs the
      P2 baseline (use the severity-bucket probe).
- [ ] LOCKSTEP: extend `tests/` so the solver-graph and validator-graph produce
      **identical per-edge budgets** `cap.at(Δs∥,Δs⊥)` for every shared edge (not
      just identical nodes) — the p5_lockstep guard.
- [ ] Suite: re-baseline genuinely-changed fixtures; suite green after re-baseline.

### Phase 4 — reach band + skeleton consistency (atomic with P3's cT)
**Goal:** `reach_band_unified` / `_build_skeleton_band` foot-climb derive cL/cT from
the SAME table, so `route_band_violations` doesn't false-flag against the new law.
**Changes:** replace the band foot-climb's hardcoded `ecap` / `APRON_MAX_GRADE`
split (`building_feasibility.py:355-358`, `:171`) with the table-driven cL/cT.
**Done when:**
- [ ] CYXY `route_band_violations` (ceil/floor/pinned) ≤ the pre-P3 count (no new
      band false-flags introduced by the law/band disagreeing).
- [ ] `tools/trace_reach_route.py` on a CYXY Y-junction foot point reports a band
      whose floor/ceil match the new edge law within 0.1 m.
- [ ] Suite `test_route_band` / `test_route_reach` green (re-baseline if improved).

### Phase 5 — feasibility-audit oracle
**Goal:** the oracle uses the anisotropic allowance so it can't report false
infeasibilities.
**Changes:** `tools/grade_feasibility_audit.py:260` `w = c.cap*c.dist` →
`w = c.allowance` from `iter_shape_grade_constraints` (`check_grade.py:942`).
**Done when:**
- [ ] `grade_feasibility_audit CYXY` reports **0 FUNDAMENTAL** at the 7 Y/X junctions
      (they are feasible-but-enforced now), and POCS resid ≈ 0.
- [ ] Audit runs clean on all four fixtures (no crash, no false-infeasible spike).

### Phase 6 — retire the legacy per-axis path
**Goal:** delete the now-redundant machinery (§4).
**Changes:** remove `_per_axis_allowance`, `_project_to_polyline`, `taxi_axes_ll`,
`_PER_AXIS_JUNCTIONS`, the `taxi_axes` args, the cT-discard. Confirm + remove
`flatten_pairs` if dead. Evaluate dropping `crosses_spine_fn` (only if P3 left it
unused or proven safe).
**Done when:**
- [ ] `grep -rn "_per_axis_allowance\|taxi_axes_ll\|_PER_AXIS_JUNCTIONS\|flatten_pairs"
      src tools tests` returns nothing (or only the removal commit).
- [ ] Suite green; four fixtures byte-identical vs end-of-P5 (pure dead-code).

### Phase 7 — default on + docs
**Done when:**
- [ ] `O4_ANISO_EDGES` defaults on; full suite green (re-baselined).
- [ ] `docs/elevation_solver.md` corrected (drop `unified_jacobi`; name
      `route_profile`); `grade_law.py` module docstring no longer says "every rule
      is ISOTROPIC"; this plan marked DONE.

## 5. Acceptance dashboard (hand-off summary)
A new agent has fully implemented this when, on a clean `dev`:
- [ ] `O4_ANISO_EDGES` default on, full `pytest tests/ -q` green.
- [ ] CYXY rising-curve violations at the 7 Y/X junctions = 0; no new >8% cliffs on
      any fixture; `grade_feasibility_audit` 0 fundamental at those junctions.
- [ ] Legacy per-axis path deleted; lockstep budget test passing.
- [ ] Docs corrected.

## 6. Risks & invariants
- **Lockstep** (memory p5_lockstep_diagnosis): one shared `ds_decompose`; budget-
  equality test (P3).
- **Band/law agreement** is the highest-coupling point — P3+P4 are effectively one
  atomic step; don't ship P3 default-on without P4.
- C–F taxiways + all aprons (cT=cL) stay isotropic; only A/B and curved spines
  change. A fixture with no A/B taxiway and no curve = clean no-change isolation.
- Δs∥ MUST be spine ARC (not chord) and over the CHAINED route, or the curve credit
  breaks at bend boundaries (§3d).

## 7. Open — needs user confirmation before P2/P3
- Authoritative cT-per-letter values + citations (only source today is the
  `(0.03,0.02)`/`(0.015,0.015)` map in `verification.py:868`).
- The single X junction (#154) 4-cell behaviour — dump + eyeball before trusting the
  nearest-route crotch on a true 4-way.
- Whether `crosses_spine_fn` can be dropped (P6) or must stay for multi-route
  diagonals.
- HECA `O4_CAP_PLANAR` fragility (`solver_primitives.py:648`) — confirm no
  interaction before default-on (P7).
