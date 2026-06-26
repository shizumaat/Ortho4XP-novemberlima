# STATUS — handover (2026-06-26) — ROUTE-GRAPH REDESIGN; ROOT = TWO GRAPHS, MUST UNIFY

Branch `dev`.  Committed this session (newest first):
- `f6b6b73` diagnostic stash (elev pre-writeback) — pinned the remaining errors.
- `01df682` hold rects + caps at route-graph z (read-by-index) → rect/cap
  violations = 0.
- `e95fb1f` full anchor coverage (service-road spines + catch-all) → no z=0.
- `8db18ab` rect graph coverage + lateral nodes + extended validator + F/14R fix
  + TX16 cull + min-curv body.

Build/probe: `PYTHONHASHSEED=0 O4_ROUTE_PROFILE_SOLVE=1 venv/bin/python …`
(route-graph emission is default ON, `O4_RP_ROUTE_GRAPH=1`).  Pin
PYTHONHASHSEED=0.  Only CYXY is being worked right now (user: don't chase other
airports / baselines until the plan is done, tested, debugged).

---
## ★★ THE ROOT CAUSE (user 2026-06-26) — TWO GRAPH STRUCTURES; SPINE ROOT NOT FULLY DIAGNOSED

⚠ HONESTY NOTE: earlier claims in this session ("two graphs, different nodes",
"all 18 are spine↔runway") were IMPRECISE and only partly verified.  The verified
facts below supersede them.  DO NOT assume a single root — diagnose each failing
edge with `/tmp/js_root.py`.

**Verified facts (CYXY):**
- There ARE two graph STRUCTURES + two context builders: Graph A = route graph
  (`taxi_routing.shared_taxi_route_graph` → `route_graph.solve_route_graph`,
  nodes = apt.dat centerline vertices + synthetics) SETS the spine/rect/cap z;
  Graph B = grade graph (`grade_graph.shape_constraints`, nodes = geometry ring
  vertices) VALIDATES + grades the body, built by `unified_jacobi.
  _grade_graph_context` AND `grade_graph_validate._context` (two builders).
- BUT the spine NODES are in fact shared via `geo_key` — node 567/568 are both
  in `geo_key` and the route-graph values there are compliant (0.16 m).  So
  "different nodes" was WRONG for the spine; the pair definitions agree too
  (`spine_adjacency` ⊇ validator spine pairs).
- The 18 spine violations = **9 distinct edges**: **3 runway-adjacent, 6 NOT**.
  Only ONE has been root-caused: a runway-adjacent dual-source — node 567 has
  TWO elevation sources (route-graph z 695.63 AND the hard runway seed 694.89);
  the protect-guard keeps the runway value, leaving its route-graph-set neighbor
  (568 = 695.79) inconsistent → 8.2 % over 11 m.
- The other **6 non-runway edges are UNVERIFIED** — could be `geo_key` coverage
  gaps, a building-floor pushing the spine over cap, the two context builders
  diverging on caps, or genuine geometry.  NEXT SESSION: run `/tmp/js_root.py`
  per failing edge and find the real cause for EACH before changing code.

### THE FIX (toward the genuine one graph)
1. **One context builder** — delete the `_grade_graph_context` / `_context`
   duplication; solver and validator call the SAME function for centerlines,
   caps, spine membership, and edges (`docs/single_grade_graph.md`).
2. **One anchor rule** — every runway-adjacent node is a runway CONTACT at the
   LOCAL runway elevation (runway is the single hard truth; building floor
   yields).  Fixes the 3 runway-adjacent edges (generalises the F/14R fix).
3. **Diagnose the 6 non-runway edges** with `js_root.py` and fix the real cause
   (likely `geo_key` coverage and/or building-floor-vs-cap).
4. Ideally collapse Graph A into Graph B so elevations are SET on the exact nodes
   the validator checks (no `geo_key` bridge, no second source).

---
## What is DONE (committed, working on CYXY)
- **Rects + caps read the route graph and are CLEAN** (0 within-grade): held hard
  at route-graph z (`route_profile/solve._seed_route_skeleton`, hard=True).
- **Full anchor coverage** (no z=0 fragments): runway contacts (taxi),
  service-road spines (4 %, terrain + airside contacts,
  `route_graph._service_road_anchors`), catch-all band/contact anchor for
  isolated discovered-TX (`_anchor_remaining_components`).
- **Validator EXTENDED** to the full spine: `grade_graph_validate` now checks
  sloping rects, rect end-caps, and spine→runway joins at the width-based
  per-letter cap, all flagged `is_spine` (+ `_shape_elevs` handles
  altitude_high/low).  THIS is the spine test now.
- **Lateral corridor nodes** (`lateral_spine_nodes.py`, pre-solve, gated
  `O4_LATERAL_SPINE_NODES=1`): a vertex on each apron/junction edge within
  ±half-taxi-width of a spine → the lateral drop (CYXY building19 runway-side,
  17.2 %) is now solved AND validated.
- **F/14R valley fixed**: `_threshold_anchors` skips the marker for BUILT runway
  ends; route seed never overwrites a hard runway/seam node.
- **Discovered building-cull**: TX centerlines crossing a (final) building are
  dropped (`pipeline.py`, post-merge); TX16 gone, 47 valid discovered remain.
- **Every rect is a graph segment** (`enrich_route_graph` rect-end nodes + axis).

## What is NOT done (the remaining work, in order)
1. **Unify the graph (the ROOT above)** — THE prerequisite for zero spine errors.
2. CYXY spine validator = **18 errors**, all the spine↔runway / junction-spine
   drift from the two-graph root (worst 8.2 % at a runway-adjacent junction).
   rect / cross_connector / stub / rect_cap = 0.
3. Caps as true BRIDGE edges (rect-end z one side, junction z other) in the ONE
   graph — currently caps are co-planar rect-continuation (clean on the validator
   but can cliff at the junction in X-Plane).
4. BODY layer (regressed under the hard skeleton — expected, body-next):
   reach band sourced from taxi + service routes; building-less aprons seated at
   their reachable level (graded within band, visible-chord fallback).
5. Then: re-cut compare-target fixtures, run the full suite, set new baselines,
   check the other airports (HECA/SPJC/SPLP), retire the ~15 legacy passes.

## ★ DEFINITION OF DONE — `tests/test_single_graph_acceptance.py` (DO NOT bypass)
Done is NOT a claim or an eyeballed number.  Done = these tests GREEN:
- `test_cyxy_spine_zero` — zero spine violations on the strict extended
  validator.  RED today (18).  THIS is the load-bearing gate: whatever graph sets
  the elevations, if the surface doesn't satisfy the validator's pairs, it's red.
- `test_validator_detects_spine_step` — a 3 m injected step MUST be flagged.
  GREEN now; must STAY green (DO NOT weaken the validator to fake spine=0).

(A `same_spine_pairs` test was REMOVED — it compared two grade-graph derivatives,
not the route graph that sets the values, so it was trivially green and proved
nothing.  False assurance is exactly the failure mode this file prevents.)

A `geo_key` emission mapping, a post-solve patch, or a relaxed validator cannot
make `test_cyxy_spine_zero` + `test_validator_detects_spine_step` green together.
If you are writing a bridge / second graph / post-solve patch, stop — that is the
hack.  ⚠ A genuine STRUCTURAL test still wants writing: compare the ELEVATION-
SETTING graph (route graph / emitted z) to the validator's pairs — add it.

Immediate target: the 3 runway-adjacent edges via the one-anchor rule; diagnose
the 6 non-runway edges with `js_root.py` (root unknown — do not guess).

## USER'S GATE for an X-Plane test
"When the entire graph is read & assigned to geometry, not broken in later
passes, and ZERO spine validation errors — build in dev and test in X-Plane."
= `test_cyxy_spine_zero` green (with the other two still green).  NOT yet ready
(18).  Note: the 18 are NOT broken in later passes (verified: `elev` is already
wrong pre-writeback) — they are the dual-source drift at runway-adjacent nodes.

## Probes (/tmp, rebuild as needed; gated O4_RP_DEBUG_STASH=1 exposes layout._rg_debug)
- `/tmp/spine_v.py` — within_violations total + spine(is_spine) + by-role.
- `/tmp/js_root.py` — nail a junction-spine pair: in spine_adj? geo_key? emitted
  vs route-graph z (this is what proved the two-graph drift).
- `/tmp/ab.py ICAO` — within-viol total/spine + bowl depth.
- `/tmp/connect.py`, `/tmp/frag_id.py` — route-graph component anchoring.
- `/tmp/rect_src.py` — per-rect route-graph grade vs emitted.

## Gates (defaults)
`O4_ROUTE_PROFILE_SOLVE=1` (route-profile solver live), `O4_RP_ROUTE_GRAPH=1`
(route-graph emission), `O4_LATERAL_SPINE_NODES=1`, `O4_RP_APRON_SMOOTH=1`
(min-curv body, no DEM), `O4_RP_BRIDGE_COMPONENTS=0` (component bridge OFF — wrong
for service roads), `O4_RP_GRAPH_FIELD`/`O4_RP_RECT_BRIDGE`=0 (dead, deletable).

## ⚠ Traps
- The reach band uses Graph A; building levels come from it.  Changing the
  contact/anchor model shifts the band → buildings → expect fixture churn.
- `geo_key` only covers spine nodes woven within 3 m of a route-graph edge; nodes
  on centerlines not in the cached graph are missed (part of the root).
- Runway nodes are `protected` from the spine seed (keep CIFP truth) — correct,
  but it exposes the Graph-A-vs-runway disagreement.
- Build with the venv (`venv/bin/python`); macOS tempdir `/var/folders`; restart
  any running Ortho4XP GUI to pick up source edits.
