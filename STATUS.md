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
## ★★ THE ROOT CAUSE (user 2026-06-26, must fix FIRST) — THERE ARE TWO GRAPHS

The "single graph" is **NOT implemented**.  Elevations are SET on one graph and
CHECKED on another, bridged by a fragile map.  Every remaining spine error is
this drift.

- **Graph A — route graph** (`taxi_routing.shared_taxi_route_graph` →
  `route_profile/route_graph.solve_route_graph`).  Nodes = apt.dat
  taxi-centerline vertices + synthetics (rect-ends, spine welds, service/TX
  anchors).  **SETS** the spine/rect/cap elevations (`z`, by graph key); feeds the
  reach band (building levels).
- **Graph B — grade graph** (`grade_graph.shape_constraints`).  Nodes = shape
  RING vertices (emitted geometry).  **VALIDATES** (`grade_graph_validate.
  within_violations`) AND grades the body.  Built by **TWO** context builders:
  `unified_jacobi._grade_graph_context` (solver, by index) and
  `grade_graph_validate._context` (validator, by rounded coord).
- Bridge = `geo_key` ({geometry node idx → Graph-A key}).  Graph B has
  adjacencies Graph A never had (a junction's spine↔runway vertex pair), so
  **Graph A is internally ≤cap while Graph B reports violations.**
- PLUS runway-adjacent nodes have **two elevation sources**: Graph A's `z` AND
  the runway seed (`_seed_elevations`, hard).  They disagree (CYXY node 567:
  Graph A 695.63 vs runway 694.89); the protect-guard keeps the runway value and
  leaves the adjacent spine node (568 = 695.79) inconsistent → 8.2 % over 11 m.

### THE FIX (the actual one-graph) — next major work
1. **One node set** = the geometry vertices the validator checks.  The solver
   sets elevations on THOSE nodes/edges, not a parallel route-graph node set
   bridged by `geo_key`.
2. **One context builder** — delete the `_grade_graph_context` / `_context`
   duplication; solver and validator call the SAME function for centerlines,
   caps, spine membership, and edges.  (See `docs/single_grade_graph.md`.)
3. **One anchor rule** — every runway-adjacent node is a runway CONTACT at the
   LOCAL runway elevation (the runway is the single hard truth; the building
   floor yields to it).  This generalises the F/14R fix to ALL runway joins.

Until 1–3 hold, the setter will always miss a pair the checker sees.

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

## USER'S GATE for an X-Plane test
"When the entire graph is read & assigned to geometry, not broken in later
passes, and ZERO spine validation errors — build in dev and test in X-Plane."
We are at 18 spine errors → NOT yet ready.  Note: the 18 are NOT broken in later
passes (verified: `elev` is already wrong pre-writeback) — they are the
two-graph drift at the source.

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
