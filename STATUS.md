# STATUS — handover (2026-06-23, single path landed)

Branch `dev`. Tree CLEAN (latest `392d3a4`). **THE AUTHORITATIVE PLAN is
`docs/single_grade_graph.md` §4b** (read first); memory `p5_lockstep_diagnosis.md`.

## ►► SPINE GRADING TUNED TO FIELD FEEDBACK (2026-06-23, latest) ◄◄
The spine seating (`_spine_climb_seats` + `building_feasibility.reach_band_sampler`)
now matches the user's X-Plane review:
- **ONE shared feasibility band** for buildings + spine (`reach_band_sampler`):
  measured along taxi routes with centerline **edge projection** (smooth between
  coarse graph vertices — the graph only measures distance, doesn't need many
  nodes), anchored on **runway-edge taxi connections** (densified runway boundary)
  **+ threshold MARKERS** (runway profile extrapolated to the marker — for runway
  ends absorbed into an apron, e.g. CYXY 02). Intersected over every runway.
- **Buildings** seat at clamp(DEM, band) → at DEM where reachable (CYXY/SPJC 0
  below DEM; HECA 6 in its canyon).
- **Spine** = building-frontage nodes held within apron-reach band
  `[lvl−1%·d, lvl+1%·d]` of their frontage buildings (so the apron grades ≤1% to
  them, hard floor) + runway/threshold anchors; between them solved for
  **smoothest grade** (neighbour-mean min-curvature) with a **mild DEM pull**
  (`_DEM_PULL=0.15`), NOT closest-to-DEM.
- RESULT: pure-spine **0** on CYXY/SPJC/HECA; CYXY body 2405→~870 (the spine
  rising to buildings lets aprons grade to them). Commits `4727c65` `bf16896`
  `e1dcd40` `76153d8` `92ae9d4` `68334ff`.
- **OPEN field items:** (2a) pin spine nodes that touch a runway edge to the
  runway surface (no peak/valley) — not currently triggering but add the guard;
  (4) E and some ~B route centerlines cross APRON INTERIORS with 0 nodes on them
  → no spine: `junction_spine` must slice the apron along EVERY crossing route
  centerline (Phase-1 geometry). Item 3's residual steepness funnels into item 4
  (coarse 02/20→A2 centerline). BODY/apron grading is still the deferred phase.

## ►► (prior) ONE PATH, SPINE CLEAN, BODY/CANYON IS NEXT ◄◄
**Collapsed to a SINGLE apron/junction grading path** (commit `392d3a4`): the
bowling `connecting_solve` is DELETED; `grade_graph_solve.spine_carries_climb_solve`
is THE solve under `SINGLE_GRADE_GRAPH` (default ON, no sub-gate). Build a plain
dev patch (no env) to fly it.
- **Bowl FIXED**: buildings lock at route-feasible level (`building_feasible_levels`)
  → 0 CYXY buildings materially below route level (was 16).
- **SPINE PERFECT (commit `e1c5857`)**: `_spine_climb_seats` (unified_jacobi) locks
  the taxi centerlines at their route-traced CLIMBING profile (in-band assignment
  that climbs at the per-letter cap; `_runway_reach_bands`). ★ The per-node band is
  reachability NOT a pairwise grade constraint — only the climbing profile is
  pairwise-compliant, so the spine must be SET to it + locked, not free-solved.
  ★★ The locked seats are marked `base_hard` so the post-solve
  `_reconcile_level_coupling` (snaps rect flat-end groups to the rect plane) HOLDS
  them instead of raising rect-coupled spine nodes ~0.5m and stranding neighbours.
  **PURE taxi-route spine violations (grade_graph_validate @ 0.15m noise): CYXY 0,
  SPJC 0, HECA 0.** All remaining spine residuals are building-frontage (canyon).
- **As-built validation UNIFIED**: `grade_graph_validate.within_violations(layout)` =
  the same grade_graph the solver used; the build WARN prints it split
  **SPINE(taxi-route) vs BODY(apron)**. (⚠ `tools/check_grade.py` still legacy →
  Phase-1 wire + fixture re-cut pending.)
- **NEXT = BODY / CANYON** (CYXY body ≈ 1284, HECA 11922, SPJC 1133): wide aprons
  between stepped pads can't grade ≤1% up to the high spine/buildings. §3b: spine-
  slice wide aprons / joint pad feasibility so the body grades ≤1% to its LOCAL
  spine, building↔building steps where pads can't co-level. Visuals:
  `/tmp/viz_violations_png.py` (inline), `/tmp/viz_violations_kml.py` (Google Earth),
  `/tmp/probe_buildings.py` (bowl check), `/tmp/probe_spine_profile.py` (DEPRECATED —
  use grade_graph_validate, not a parallel metric).

## (historical — the original spine-climb implementation note, superseded by §4b)
### ►► PRIOR PLAN: IMPLEMENT SPINE-CARRIES-CLIMB ◄◄
**The default build is ON but NOT usable as-is (user): it BOWLS the buildings.**
The connecting solve hits within=0 by locking buildings on the CONNECTING band,
which seats them ~5 m below DEM (building5 707.7 vs DEM 713). **A within=0 reached
by bowling is a FAILURE against the central pillar (buildings at DEM + taxiways
carrying the climb).** Locking at ROUTE levels instead regresses (462 floor>ceiling)
because the connecting-graph Dijkstra routes a building's reach through the
cheapest/shortest path (decisive: `bld:building8 712.4 ↔ runway 694` short path),
under-measuring the real long-taxi-route reach.
**THE FIX TO BUILD = spine-carries-climb (design in `docs/single_grade_graph.md`
★ SPINE-CARRIES-CLIMB, two graphs):** (1) ROUTE graph carries the climb — building
levels = `building_feasibility.building_feasible_levels`; the SPINE (taxi rects +
centerline spines) gets a climbing profile from a cap-weighted band on the ROUTE
graph, imposed as soft anchors; (2) CONNECTING graph grades the apron body ≤1% from
its LOCAL SPINE (not from spurious short/global paths to far-low anchors); (3) a
spurious short path (high-terrain building near low runway) is NOT a real grade
path. ★ DONE-CRITERION: every building emits at ≈ min(DEM, route-band-ceiling) — a
build with a building materially below that is a FAILURE even if within=0 (add a
test). Then Phase 3b joint-feasibility, Phase 1 validator + re-cut, Phase 4 retire
legacy. Helper `_building_route_levels` (unified_jacobi) computes the route levels.

## ⚙ State of the gates (all committed)
`SINGLE_GRADE_GRAPH`, `UNNAMED_TAXI_SIZE`, `FIELD_ROUTE_BAND_BY_WIDTH` default ON
(commit 42a03c9). `O4_SINGLE_GRADE_GRAPH=0` (+ the other two) restores the legacy
path. ⚠ The full grade + compare_target suite is RED by design (airside is mid-build
+ building set changed by the pad fix — fixtures need re-cut once spine-climb lands).

## ✓ Corrections this session (don't repeat my mistakes)
- **The build IS DETERMINISTIC** (3 separate processes byte-identical). My earlier
  "nondeterminism / partition coin-flip" claim was WRONG — an artifact of my own
  buggy serving-weld code. The older MEMORY "PYTHONHASHSEED partition-nondeterministic"
  claims are SUSPECT; re-verify before trusting. Identify buildings by CENTROID
  (refs renumber when the building set changes).
- **HANGAR PADS FIXED (commit 4b50ee1):** scattered facade pieces (pier_wooden-style
  ~0.6 m² panels) were dropped (sub-min-area, unmerged) → buildings got no pad.
  `_cluster_dsf_building_facades` now bridges gaps up to `DSF_FACADE_MERGE_GAP_M`
  (2.0 m) + repairs invalid rings + min area `DSF_MIN_BUILDING_AREA_M2` (20 m²).
  CYXY 13→24 buildings; SPJC 31 / HECA 30 (no explosion).
- **Temp files → /tmp, never the project dir** (user). Probes:
  `/tmp/probe_sgg_within.py` (O4_PROBE_ICAO=<icao>; unified-graph within count),
  `/tmp/diag_infeasible.py` (the route-lock infeasibility classifier).

## (historical context below — superseded by the spine-climb design above)
### ⚡ prior note (commit 42a03c9)
The single-grade-graph system is **ON by default** (`SINGLE_GRADE_GRAPH`,
`UNNAMED_TAXI_SIZE`, `FIELD_ROUTE_BAND_BY_WIDTH` all default ON). A plain build
uses the Phase-3 connecting solve. ⚠ "CYXY within 0" was APRON/JUNCTION-ONLY +
BOWLED — see the correction above; it is NOT flight-clean.

## ★★ CURRENT GENERATION (2026-06-23) — the SINGLE GRADE GRAPH ★★
**Authoritative plan: `docs/single_grade_graph.md`.** Memory:
`p5_lockstep_diagnosis.md`. This SUPERSEDES the P5/P6 sketch below for the
connecting solve. The pivot this session:

- **Root cause of the residual 481 CYXY within-violations = TWO within-shape
  graphs.** The solver (`unified_jacobi._visible_grade_edges`) and the validator
  (`check_grade.iter_shape_grade_constraints`) derive grade pairs from two
  different functions; on identical geometry they disagree by ~9k pairs (146
  *violated*). The solver cannot fix what it does not grade → 481 can't reach 0.
- **User model (authoritative, in the doc + memory):** (1) keep the TAXI ROUTE
  graph as the feasibility-band / building-elevation layer; (2) ONE within-shape
  grading graph for solver+validator; (3) buildings = closest-to-DEM in band then
  LOCKED; everything else = **min grade + curvature** spread on the one graph;
  (4) **NO genuine infeasibility — anything infeasible is a BUG** (no P6).
- **Junction = apron with spine+body** (user 2026-06-23): spine graded smooth like
  a crossing runway at the taxiway per-letter cap (shares elevation at crossings,
  grades into the adjacent corridor); body = visibility/geodesic at the taxiway cap
  (NOT 1%); spine-less junction inherits the cap from the nearest connected
  taxiway. The old per-axis diagonal-skip (`check_grade.py:949`) leaves wide
  junction bodies UNGRADED — that goes away.
- **BUILT THIS SESSION (uncommitted, clean-room):**
  `src/auto_patch/grade_graph.py` = THE single graph (apron/junction spine+body,
  unified per-edge cap rule, visibility, seam-drop, cap-inheritance) +
  `tests/test_grade_graph.py` (7 hermetic tests, GREEN). Proof
  (`/tmp/probe_lockstep_module.py`): same module fed SOLVER shapes vs EMITTED OSM →
  **0 cap disagreements**, 94% identical pairs; the ~6% residual is geometry
  non-identity (`to_osm` weld + the 5 post-solve-drift shapes + vis-buffer flutter)
  = Phase 0.
- **PHASE 0 DONE (this session):** moved `_dedup_coincident_ring_vertices` +
  `drop_flatedge_nodes` PRE-solve (gate `O4_PRESOLVE_CLEAN`, default ON;
  pipeline.py after `_unify_airside_geometry`, idempotent post-solve copies kept).
  CYXY geom-guard **5→2**; the 2 residual = `_insert_bridge_contacts_into_junctions`
  (Phase-5 solve-dependent exception, collinear → grade-neutral). ★ Insight: the
  grade graph is ALTITUDE-INDEPENDENT, so altitude-only post-solve passes
  (`debulge_cap_centre_nodes`, `_smooth_junction_ring_curvature`) are lockstep-safe
  and were never the issue. Grade suite: 1 failed (HECA standing) / 14 passed = NO
  regression. test_grade_graph.py 7/7 green.
- **PHASE 2 DONE (this session):** solver consumes `grade_graph` for apron/junction
  (gate `O4_SINGLE_GRADE_GRAPH`, default OFF; helpers `_grade_graph_context` +
  `_grade_graph_edges` in unified_jacobi; ROLE_BUILDING + service_junction stay
  legacy). Gate-off byte-identical (new elif requires `_gg_ctx is not None`).
  **CYXY apron/junction within = 348** under the unified graph
  (`/tmp/probe_sgg_within.py`, solver+validator both grade_graph) — the OLD solve's
  quality gap (aprons 10%/4–15m, junctions ~9%), NOT infeasibility. → Phase 3.
- **PHASE 3 DONE (this session) — CYXY within 481 → 4.** NEW
  `grade_graph_solve.connecting_solve` (commits 92db144, e355e61), wired in place of
  `_min_grade_network_solve` under `O4_SINGLE_GRADE_GRAPH`. (1) Feasibility bands =
  direct multi-source Dijkstra over the cap-weighted graph (no POCS). (2) ★ Buildings
  LOCKED on the CONNECTING graph's OWN bands (closest-to-DEM in pad band-
  intersection), NOT pre-pinned from the route graph — pre-pin → 443 false-
  infeasible bands; connecting-lock → 443→1. (3) Projected Gauss-Seidel smoothing
  (lands in cap-feasible interval; converges 102 sweeps). (4) Auto-disabled the
  legacy post-solve altitude band-aids (`_smooth_junction_ring_curvature`,
  `debulge_cap_centre_nodes`) under the gate — they fought the solve and re-added 66
  junction violations. RESULT: every apron resolved; **within = 4** (mild ~4% apron
  spots by locked buildings) + 7 in-solve both-hard edges = final cleanup. Stack =
  `O4_SINGLE_GRADE_GRAPH=1 O4_UNNAMED_TAXI_SIZE=1 O4_FIELD_ROUTE_BAND_BY_WIDTH=1`
  (P4 BUILDING_ROUTE_FEASIBILITY no longer needed). Gate-off byte-identical.
- **INTER-PAD FRONTAGE EXEMPT (commit 4470971) — CYXY within 481 → 0.** An
  apron/junction edge with BOTH endpoints on building pads = a building↔building
  step (allowed), exempt in grade_graph (`GradeContext.building_keys`); also
  un-tightens bands. CYXY 4→0 (in-solve both-hard 7→0).
- **MULTI-AIRPORT STATUS (unified graph, full single-graph stack):** CYXY **0** ✓;
  **SPJC 211→134**, **HECA 1034→928**. The dominant remaining issue = **INFEASIBLE
  BANDS** (HECA 2593, SPJC 148 nodes with floor>ceiling): building pads are locked
  INDEPENDENTLY (each closest-to-DEM in its runway-reach band), so on a canyon
  (HECA terminal spans 82–88 m) two adjacent pads sit at mutually-incompatible
  levels and the free apron node between them can't grade ≤1% to BOTH → empty band.
- **★ NEXT = JOINT BUILDING FEASIBILITY** (the canyon case): lock pads at
  mutually-consistent levels, not pad-by-pad — the connecting surface between two
  pads must be gradeable (apron ≤1% to its local spine; the SPINE carries the climb
  at taxi cap; large aprons are spine-sliced so each piece grades ≤1% locally; where
  a pad genuinely can't co-level, it steps and the apron follows it). Then Phase 1
  (validator→grade_graph + re-cut fixtures), Phase 4 (verify all airports, retire
  legacy per-axis/`_visible_grade_edges`/`_min_grade_network`). Probe:
  `O4_PROBE_ICAO=<icao> /tmp/probe_sgg_within.py`. (Older Phase-1 note:) wire
  validator to
  grade_graph (+ re-cut fixtures for the junction change); Phase 2 wire solver
  (gated, A/B `probe_constraint_diff`); Phase 3 the connecting solve (lock
  buildings, min grade+curvature — NEW, replaces `_min_grade_network_solve`, NO
  60k-iter POCS — bands are a direct Dijkstra); Phase 4 verify + RETIRE the old
  per-axis / `_visible_grade_edges` / `_min_grade_network_solve` scaffolding.
- ⚠ **Clean-room rule (user):** build in NEW files, wire in, DELETE old. Do not
  extend the legacy graph/solve code.

## (PRIOR GENERATION — context only; the building/route measurement is REUSED)
The default build is unchanged except **P2 is ON**. P3/P3a/P4/P5 are built + gated
default-OFF; P4 (route-feasibility building elevations) is VALIDATED and is REUSED
as the band/building layer. The old plan's NEXT STEP ("P5 clears wide aprons") is
SUPERSEDED by the single-graph generation above.

## ★ START HERE (older plan, still useful for the route/band + P3a/P4 detail)
1. **`docs/taxi_centerline_grading_plan.md`** §1 (model) + §9. The building
   feasibility metric there is the route/band layer the new generation keeps.
2. Memory: `taxi_grading_final_plan_field_target.md`, `corridor_spine_chains_p2.md`.
3. ⚠ **Two traps that cost real time:**
   - **Pin `PYTHONHASHSEED=0`** for ANY A/B — the apron/junction partition is
     hashseed-nondeterministic (CYXY within-shape flakes on the same config).
   - **Judge against the SMOOTHED DEM**, never the emitted (bowled) levels:
     `_load_airport_dem(lat, lon)` with `override_dem=None` (it applies the same
     `apt_smoothing_pix` blur the build uses). Reading emitted levels led to a
     wrong "no 718 m terminal" conclusion mid-session.

## The root cause (verified) and the model
The terminal sits on terrain that rises faster than taxi grade allows; the bowl
came from **a dropped ICAO size code**: CYXY's gate "arms" from taxiway G to the
terminal are apt.dat `taxiway_A` (3 %) **but unnamed**, and
`apt_dat_reader.taxi_size_letters` was keyed by name and skipped unnamed edges —
so the feasibility band to the terminal was computed at the uniform 1.5 % (half
the legal climb), clamping the buildings ~9 m below DEM and bowling G + the aprons
with them.

**Model (user, authoritative).** Buildings are the heaviest anchor, seated FLAT at
the elevation closest to DEM that keeps them reachable WITHIN GRADE from **every**
runway threshold along the real taxi route; the taxi network carries the climb at
per-letter caps (narrow A/B 3 %, wide C–F 1.5 %); aprons ≤1 %; any steepness the
terrain forces beyond that goes in an **explicit transition (ramp/wall), never the
apron/taxi interior**. Minimal-deviation: everything as close to DEM as grade
allows.

**The building-feasibility metric (LOCKED, validated to the user's hand-calcs)** —
`elevation_per_surface/building_feasibility.py::building_feasible_levels`: for each
building touching airside pavement, perpendicular from the centroid to the nearest
taxi centerline (named or not) — corridor part of the perp at the taxiway cap,
apron part at 1 % — then the per-edge per-letter cap-weighted centerline route
(incl. the partial first edge from the foot point to its graph node) to **every**
runway threshold; band = intersection over thresholds (`ceil=min(thr+climb)`,
`floor=max(thr−climb)`); seat at `clamp(DEM, floor, ceiling)`. CYXY result matches
the user: building9 (terminal) 700.4, building3 (hangar) 715.7, building5 709.1,
building10 (not touching) stays DEM.

## Gate stack (all in `config.py`; env override in parens)
Default-ON keepers (the variable-grade primitives, zero net-new reds):
- `TAXI_REACH_BAND_BY_WIDTH` — per-edge caps in `_runway_reach_bands`.
- `JUNCTION_NARROW_GRADE` (per-axis) — narrow junction axis earns 3 %.
- `SPINE_PIECE_ROLE_REEVAL` — promote narrow apron-corridor pieces → junction.
- **`CORRIDOR_SPINE_CHAINS` (P2, default ON)** — `_taxi_corridor_profiles` adds
  station-only chains over the spine nodes of any centerline a rect chain misses
  (CYXY G's promoted-apron pieces) so the field value is written + held along the
  whole route. Flipped `grade[CYXY]` RED→GREEN; off-target airports byte-identical.

Built + banked default-OFF (the §9 chain; flip ON together to test the airside):
- **`UNNAMED_TAXI_SIZE` (P3a)** — recover the ICAO size of unnamed `taxiway_A/B`
  arms via geometry → synthetic ref `~A`/`~B` in `apt_taxi_letters`
  (`apt_dat_reader.coded_taxi_edge_segments` + pipeline resolver). THE unlock;
  tags 19 CYXY arms. Regresses standalone (within 0→8) → needs P4/P5.
- **`FIELD_ROUTE_BAND_BY_WIDTH` (P3)** — `network_profile._runway_route_band` uses
  `edge_cap` (was uniform 1.5 %). Correct band fix; regresses standalone → P4/P5.
- **`BUILDING_ROUTE_FEASIBILITY` (P4)** — seat buildings at the validated metric
  levels as hard anchors (`_seat_buildings_route_feasible`; thresholds on
  `layout.runway_thresholds`). VALIDATED. Requires P3a. Anchors alone + old solve
  → within 563 (network can't reach them) → needs P5.
- **`MIN_GRADE_NETWORK` (P5, PROTOTYPE)** — `_min_grade_network_solve`: re-solve
  free airside nodes as the smoothest (min Σgrade²) cap-bounded surface connecting
  the hard anchors (buildings + runway + seams). Holds the anchors + grades the
  taxi network, but within-shape only 563→481, **apron-dominated (328)** — see
  NEXT STEP. Final override before writeback.

Superseded / to retire (proved mechanisms, NOT the architecture):
`FIELD_TARGET_CONFORMANCE` (half-measure lift, superseded by P5),
`APRON_FEASIBLE_LIFT`, `O4_TAXI_SPINE`, `BUILDING_DEM_ANCHOR` (uniform-cap →
bowled; superseded by P4's edge-cap metric), `O4_DEM_ATTR`/`O4_DEM_FLOOR_ATTR`.

**Run the full new airside pipeline:**
`O4_UNNAMED_TAXI_SIZE=1 O4_FIELD_ROUTE_BAND_BY_WIDTH=1 O4_BUILDING_ROUTE_FEASIBILITY=1 O4_MIN_GRADE_NETWORK=1`
→ buildings land on their metric levels; within-shape 481 (apron-dominated).
Default (no env) = clean P2 baseline.

## ► NEXT STEP (where a new session picks up)
P5 holds the anchors but does NOT clear the **wide terminal aprons** (481 within,
328 apron). This is NOT solver-tuning: the existing enforce (a proven projector)
is also stuck at 563 with the anchors, so it's genuine — two parts:
1. **Builder-vs-validator graph mismatch.** `_min_grade_network_solve` solves the
   `shape_constraints` edges; `tools/check_grade.py` (and the build WARN) use their
   own geodesic per-axis graph. FIRST diagnose how many of the 481 are real ≤1 %
   apron infeasibilities vs edges the solver never sees, and reconcile the solve
   graph with the validator's. (Probe: dump the within-violations by shape +
   whether each endpoint is hard — see below.)
2. **P6 explicit transitions.** Where a wide apron genuinely cannot grade ≤1 %
   from its low taxiway edge up to the high anchored building across its width,
   emit an explicit ramp/retaining edge at the building frontage and let the apron
   interior sit flat at the building level (the user's "steepness in the
   transition, never the apron interior"). The P4 metric only credits ONE
   perpendicular apron crossing at 1 %, so a wide apron's far interior nodes are
   the ones that can't reach.
Also consider a true constrained-QP for the min-grade solve rather than the
alternating harmonic/projection prototype.
Then: flip P3a+P3+P4+P5 (+P6) ON by default; user re-cuts SPJC/SPLP fixtures;
add a centerline-smoothness + closest-to-DEM test (P7); retire the scaffolding.

## Test suite
`PYTHONHASHSEED=0 venv/bin/python -m pytest tests/ -q` (seed 0; ~5.5 min) →
**5 failed / 359 passed** with defaults (P2 on). The 5:
- `rests_on_source[CYXY]`, `grade[HECA]` — pre-existing standing reds.
- `compare_target_splp[-13--77]`, `compare_target_splp[-13--78]`,
  `compare_target_spjc` — EXPECTED: keeper changes shifted junction values; the
  user re-cuts these once CYXY is right. Do NOT chase them.
`grade[CYXY]` is GREEN (P2 fixed it). A/B: `O4_CORRIDOR_SPINE_CHAINS=0` → 6 failed
(grade[CYXY] red) = the exact pre-session baseline. (Total tests dropped ~19 vs
mid-session: an unrelated commit `8107519` removed `tests/test_surface_mesh.py`.)

## Probes (⚠ `/tmp` is periodically CLEARED — recreate as needed)
Build a single airport (cwd = repo root, venv): see `auto_patch/CLAUDE.md`. Key
patterns used this session (re-create in `/tmp`):
- within/cross/steps mirroring `test_pavement_grade`: build CYXY, `to_osm`, then
  `tools.check_grade.run_checks(out, max_grade_pct=1.5, proximity_m=1.0,
  edge_search_m=5.0, edge_step_m=0.5, taxi_axes_ll=<per-letter axes>,
  route_ctx=route_ctx_from_layout(layout))`.
- building levels vs DEM: build, sample `_load_airport_dem`/`_sample_dem` at each
  ROLE_BUILDING centroid vs `mean(node_altitudes)`.
- the route-feasibility table / KMLs (building5 etc.): call
  `building_feasibility.building_feasible_levels(layout, thresholds_xyz,
  dem_sampler)` — thresholds from `apt.runways` (both ends) at nearest-runway-node
  elev. (KMLs of building + nearby centerlines + the route path were very useful
  for confirming geometry with the user in Google Earth.)
- env debug: `O4_STEP_DEBUG=1` (per-pass counts incl. "route-feasible buildings
  seated", "min-grade network"), `O4_CORRIDOR_DEBUG=1`, `O4_NPF_DEBUG=1`.

## New code this session (all committed)
- `elevation_per_surface/building_feasibility.py` (NEW) — P4 metric.
- `elevation_per_surface/unified_jacobi.py` — P2 spine chains in
  `_taxi_corridor_profiles`; `_seat_buildings_route_feasible` (P4);
  `_min_grade_network_solve` (P5); `_enforce_within_shape_grade` gained a
  `dem_elev` param + the (superseded) `FIELD_TARGET_CONFORMANCE` lift block.
- `apt_dat_reader.py` — `coded_taxi_edge_segments` (P3a).
- `pipeline.py` — P3a unnamed-arm size recovery; stash `layout.runway_thresholds`.
- `network_profile.py` — `_runway_route_band` per-edge cap (P3).
- `config.py` — gates P2/P3/P3a/P4/P5 (above) + `FIELD_TARGET_CONFORMANCE`.
- `docs/taxi_centerline_grading_plan.md` §9 — the authoritative plan.

## Commit trail (this session, branch dev)
`6cd8922` P2 + variable-grade keepers (grade[CYXY] →green) · `6779767` P3 banked ·
`138555c` P4 investigation (docs) · `5627712`+`c83bf10` §9 plan · `4b8fb41` P3a ·
`8fe80aa` P4 finding · `f9d3875` P4 building driver · `686e7c7` P5 prototype.
