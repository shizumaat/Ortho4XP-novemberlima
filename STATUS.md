# STATUS — handover (2026-06-26 late) — ONE-GRAPH DONE; now on the CYXY BODY LAYER

Branch `dev`. Build a single airport + probe with the venv:
```
PYTHONHASHSEED=0 venv/bin/python -c "import sys;sys.path[:0]=['src','.','tests'];\
from conftest import xplane_root; from auto_patch.pipeline import build_airport_pavement;\
L=build_airport_pavement('CYXY', xplane_root(), compute_elevations=True)"
```
⚠ PIN `PYTHONHASHSEED=0` (apron/junction partition is nondeterministic → shape
indices/`shapeID` shift between builds — identify shapes by **ref/coord**, never
raw index). A build is ~60–90 s.

★ STANDING RULE (user, emphatic): use the EXISTING checks/validators/tests, NOT
ad-hoc `/tmp` scripts that re-derive logic and produce wrong numbers (a uniform
1.5 % cap in a /tmp script cost hours — the real `reach_band_sampler` uses
per-edge caps, taxiway G = 3 %). If something isn't covered, ASK to add it to the
main code or `tools/` (not `/tmp`). Real validators:
`grade_graph_validate.within_violations` (spine + body) / `.route_reach_violations`,
`building_feasibility.reach_band_sampler`, `tools/check_grade.py`,
`tools/trace_reach_route.py` (NEW: reach-route → KML), the pytest suite.

---
## ✅ DONE THIS SESSION (committed on dev, newest first)

1. **ONE-GRAPH MERGE — COMPLETE** (`docs/goal_merge_one_graph.md`, the original
   goal). Spine solved DIRECTLY on the geometry nodes the validator checks
   (`grade_graph.build_unified_graph`); `route_graph.py`/`geo_key` DELETED; ONE
   `grade_graph.build_context`. `grep geo_key src/` == 0. CYXY spine 18→0.
   Acceptance `test_validator_detects_spine_step` + `test_solver_and_validator_
   same_nodes` GREEN. (`test_cyxy_spine_zero` was GREEN at the merge; RED now — see
   below.)
2. **route_reach validator** — `route_reach_violations`: a no-building apron whose
   feeder taxiways arrive at mutually unreachable elevations. `tests/test_route_
   reach.py`.
3. **Apron #86 / west apron** — CLOSEST-DEM-FEASIBLE model: every surface
   (buildings, aprons, spine) = DEM clamped into its per-node reach band
   [floor,ceil]. West apron filled out of its wrong-low DEM (677→693). Aprons grade
   **1 % visibility/geodesic** (apron_smooth=True), NOT DEM-draped.
4. **Synthetic junction spines** — `synthetic_junction_spine.py`: every spineless
   junction (45 of 112 at CYXY) gets a route through its taxi-network mouths
   (2→straight, 3+→star). Junction #88 (TX2 drop) 4.2 m→0.5 m; TX2 spreads its
   climb along its length.
5. **Junction-edge densification** — `lateral_spine_nodes.densify_junction_edges`
   (gate `O4_DENSIFY_JUNCTION_EDGES` default ON): subdivide every junction
   exterior edge to ~12 m (SPINE_STEP_M). Junction #97's 500 m far edge now TILTS
   695.7→698.8 following the spine (was flat 695.6).
6. `tools/trace_reach_route.py` — reusable reach-route tracer (KML + per-cap
   segment lengths), uses the real `reach_band_sampler` cost model.

★ USER MODEL (authoritative, 2026-06-26): **junctions FOLLOW their spine, graded
≤cap laterally from it; aprons grade 1 % visibility/geodesic; nothing drapes raw
DEM.** Building pads + no-building-apron feasible levels are set FIRST (closest-DEM
within the reach band), THEN the spine is smoothed between them.

---
## ⚠ WHERE WE'RE AT — `test_cyxy_spine_zero` is RED(1), AWAITING USER X-PLANE REVIEW

The junction-edge densification (#5, committed DEFAULT-ON at user request) surfaces
grade the flat edges hid. REFINED so it only densifies OFF-SPINE, off-runway edges
(densifying a spine/runway-adjacent edge added near-centerline nodes that became
NEW spine nodes / perturbed the runway anchor):
- spine went 4→**1** with the refinement. The remaining 1 = a MARGINAL
  **runway_join 5.4 %** at runway 14L/32R near (-33,576) — it was 4.5 % even in the
  ORIGINAL 18, got ≤cap by the one-graph work, and the densification perturbed the
  global solve enough to flip it back over (the node there is pre-existing, NOT a
  densified node — it's solve-convergence sensitivity, not geometry).
- **body 671→793** (grade now visible at junction edges that follow the spine).
- route-reach 2; structural + anti-gaming tests GREEN. Junction #97 far edge TILTS
  695.7→698.8 (goal met).

USER (last instruction): "Keep it and ensure it's on in dev so I can build and
review in X-Plane before deciding next steps." So restart Ortho4XP (it caches
`auto_patch` imports), rebuild CYXY, look in X-Plane. DO NOT revert the
densification. NEXT after review = the 1 marginal runway_join @(-33,576) (stabilise
the solve there / anchor the runway-join node) to restore spine=0, then the body
grade now visible at junction edges.

---
## OUTSTANDING — CYXY review batch (memory `cyxy_review_items.md`)

- **stub/A 3.2 % + body** at junction↔taxiway joins (from densification, above).
- **Crossing #82** (02/20+14L/32R runway crossing): 693.7..695.9 / 6.8 % within-
  shape — needs smoothing.
- **Shed buildings**: add DSF OBJECTs whose path starts `lib/g10/US/industrial/`
  and contains "shed" as building sources (`dsf_reader.read_dsf_buildings` only
  reads `.fac` facades today; objects need footprint resolution).
- **SVC7** (service_road, 715.2..715.6) disconnected from its parking-lot
  groundside — find the clearance/separation that drops it, keep it.
- **Dead-end road loop** at 60.7102606,-135.072653 (local ≈ -290,79; currently
  SVC10/SVC9/service_junction 708.5) → classify as apron (closest-DEM-feasible).
- **Shape "154"** = service_junction next to SVC11 → user: a shape reachable ONLY
  via an SVC road → treat as GROUNDSIDE, level = that SVC's max-grade reach.
- **SVC ramp** at 60.7131156,-135.0752334: connect apron (≈694–700) to groundside
  (≈701) as a smooth SVC ramp, not a projection off the apron.

★ DEAD ENDS that thrashed this session (don't repeat): projecting the COARSE
3-vertex spine onto edges; a hard "junction-follows-spine" grading pass (rejected);
`apron_smooth=False` closest-DEM apron body (reverted — aprons = 1 % visibility);
gating the whole junction body as is_spine (rejected — instead give each junction
a spine). The WORKING approach = densify junction edges + let the solver grade.

---
## AFTER the CYXY body layer (deferred)
Body/apron grade regressed vs the route-graph baseline (EXPECTED until the body
layer is done — user: CYXY is the sole focus). Then: re-cut compare-target
fixtures, run the full suite, other airports (HECA/SPJC/SPLP), delete ~15 legacy
elevation passes. See `docs/one_profile_solve.md` and the memory index.

## Gates / env (CYXY)
`O4_ROUTE_PROFILE_SOLVE` (the next-gen solver), `O4_DENSIFY_JUNCTION_EDGES`,
`O4_SYNTH_JUNCTION_SPINE`, `O4_LATERAL_SPINE_NODES` — all default ON. The
`O4_RP_ROUTE_GRAPH`/`geo_key` path is GONE.
