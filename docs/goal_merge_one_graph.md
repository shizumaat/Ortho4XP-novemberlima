# GOAL (TOP PRIORITY) — Merge the route graph and the grade graph into ONE graph

## STATUS 2026-06-26 — DONE (pending the user's X-Plane sign-off)
All of the Definition of DONE is GREEN:
* `test_cyxy_spine_zero` — **0** spine violations (was 18).
* `test_validator_detects_spine_step` — GREEN.
* `test_solver_and_validator_same_nodes` — GREEN (the unified graph's spine
  nodes/edges == the validator's, in coord space).
* `grep -rn "geo_key" src/` == **0**; ONE context builder (`grade_graph.build_context`).
* `route_graph.py` + `graph_field.py` DELETED; the route-graph/`geo_key`
  read-by-index bridge and its dead helpers are gone.  The spine is solved
  directly on the geometry nodes the validator checks
  (`grade_graph.build_unified_graph` + `route_profile/solve._solve_spine_profile`).
Stable with and without `PYTHONHASHSEED=0`.

How the 18 → 0 happened (each on the ONE graph, no bridge):
1. ONE context builder; ONE graph object (`build_unified_graph`) on geometry
   nodes — connected per-centerline spine chains + rects + caps woven in.
2. Building seat vs spine floor disagreed → anchor seats that are spine nodes at
   their real level DURING the spine solve.
3. `_add_rects_to_spine`/`_flatten_rect_ends` only handled 4-corner rects → a
   5-corner code-D stub solved 2.1 m over its ends.  `_rect_ends()` now splits
   any 4+-corner sloping rect by axis projection.
4. Runway-join: anchor the EXACT node the validator picks (incl. runway_crossing)
   at the LOCAL runway elevation — fixes the runway-intersection compromise.

Remaining = Step 6's X-Plane gate (user) + the "AFTER the goal" body/other-airport
work (regressions there are EXPECTED until the body layer lands — user 2026-06-26).

---


## Why
The route-profile solver SETS spine/rect/cap elevations on the **route graph**
(`taxi_routing.shared_taxi_route_graph`, taxi-centerline network) and the
validator CHECKS on the **grade graph** (`grade_graph.shape_constraints`,
geometry ring vertices), bridged by `geo_key` with **two context builders**
(`unified_jacobi._grade_graph_context`, `grade_graph_validate._context`).  They
were built separately for historical reasons (grade graph = original geometry
validator; route graph = later, for runway-route reachability + smooth spine).
The bridge DRIFTS: the route graph can be ≤cap while the validator reports
violations.  Build and validate must use the **exact same nodes**.

## Definition of DONE (objective — `tests/test_single_graph_acceptance.py`)
All green together:
1. `test_cyxy_spine_zero` — 0 spine violations on the strict validator (RED today: 18).
2. `test_validator_detects_spine_step` — validator still flags an injected step.
3. `test_solver_and_validator_same_nodes` — NEW, added in Step 2: the graph the
   solver builds/sets == the nodes+spine-edges the validator checks.
Plus: `grep -rn "geo_key" src/` == 0 and ONE context builder.

## RULES (these are the hacks that have failed for ~10 sessions — forbidden)
- NO bridging two graphs, NO `geo_key` emission mapping, NO post-solve patch, NO
  weakening the validator.  If you write any of those you are NOT doing this goal.
- ONE graph object, ONE context builder, nodes = geometry vertices.
- Runway is the single hard anchor; the building floor yields to it.
- Diagnose EVERY failing edge with `/tmp/js_root.py` — never assume a common root
  (this session wrongly assumed "all runway"; only 3 of 9 edges were).

## MEASURABLE STEPS

### Step 0 — Baseline (measure start)
- `PYTHONHASHSEED=0 venv/bin/python -m pytest tests/test_single_graph_acceptance.py -q`
- `PYTHONHASHSEED=0 O4_ROUTE_PROFILE_SOLVE=1 venv/bin/python /tmp/spine_v.py`
- DONE WHEN: recorded spine_zero=RED(18), step=GREEN, spine by-role noted.

### Step 1 — ONE context builder
- Collapse `unified_jacobi._grade_graph_context` and
  `grade_graph_validate._context` into a single shared
  `grade_graph.build_context(layout, bucket_to_idx=None)`; make the solver
  (`_build_shape_constraints`), the spine (`route_profile/spine.spine_adjacency`)
  and the validator (`within_violations`) all call it.
- MEASURE: `grep -rn "_grade_graph_context\|def _context" src/` → ONE definition;
  full suite shows NO NEW failures vs Step-0 baseline.
- DONE WHEN: one builder, build runs, no new suite failures.

### Step 2 — ONE graph object on geometry nodes + the structural test
- Add `grade_graph.build_unified_graph(layout, bucket_to_idx)` → nodes = geometry
  vertices (`bucket_to_idx`); edges = within-shape grade edges + spine chains
  (centerline order, per-letter cap) + runway contacts + service-road anchors.
  This is the SINGLE graph the solver sets on and the validator checks.
- Write `test_solver_and_validator_same_nodes` (in the acceptance file): assert
  the unified graph's node set == every node `within_violations` checks, AND its
  spine-edge set == the validator's spine pairs.  (Replaces the removed hollow
  `same_spine_pairs`; this one compares the SETTER graph, not a derivative.)
- MEASURE / DONE WHEN: the new structural test is GREEN.

### Step 3 — Solve the spine ON the unified graph; DELETE geo_key
- Replace `route_graph.solve_route_graph` (centerline route graph + geo_key) with
  a solve on the unified graph: spine smooth in centerline order, runway contacts
  HARD at the LOCAL runway elevation, building floors as anchors; write the result
  directly into `elev[idx]` for geometry nodes.  Remove `geo_key` and the
  `_seed_route_skeleton` read-by-index bridge.
- MEASURE: `grep -rn "geo_key" src/` == 0; a test asserts emitted spine z ==
  solved z at every validator spine node (no second source).
- DONE WHEN: no geo_key; emitted == solved on every validator spine node.

### Step 4 — ONE anchor rule (fixes the 3 runway-adjacent edges)
- Every runway-adjacent geometry node is a runway contact at the LOCAL runway
  elevation; the building floor yields where it conflicts (user hierarchy).
- MEASURE / DONE WHEN: the 3 runway-adjacent spine violations → 0 (verify each
  with `/tmp/js_root.py`).

### Step 5 — Diagnose & fix the 6 non-runway edges
- For EACH of the 6, run `/tmp/js_root.py` (set its coord); fix the real cause
  (now that geo_key is gone, likely the spine solve itself or a building floor
  over cap).  Do NOT assume a shared root.
- MEASURE / DONE WHEN: `test_cyxy_spine_zero` GREEN, `test_validator_detects_spine_step`
  GREEN, `test_solver_and_validator_same_nodes` GREEN.

### Step 6 — Hold through later passes + X-Plane gate
- Assert the emitted spine z is identical from pre-writeback through the final
  layout (no later pass moves it).  Then build CYXY in dev and test in X-Plane
  (the user's gate).
- MEASURE / DONE WHEN: spine z unchanged end-to-end; user X-Plane sign-off.

## AFTER the goal (separate work — not part of "one graph")
Caps as true bridge edges; body layer (reach band from taxi + service routes,
building-less apron seating, graded within band); re-cut compare-target fixtures;
full suite; other airports (HECA/SPJC/SPLP); delete the dead route_graph /
geo_key / ~15 legacy elevation passes.
