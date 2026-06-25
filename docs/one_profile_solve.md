# The One-Profile Solve (next-gen elevation solver)

> **2026-06-25 PM — current state & handover live in
> [`docs/route_profile_solver_status.md`](route_profile_solver_status.md) (top
> section).** The model has consolidated onto ONE graph
> (`taxi_routing.shared_taxi_route_graph`): `route_profile/route_graph.py`
> solves the route profile on it with ZERO residual, band byte-identical. Live
> solver is still `one_solve` (469/spine 0, spine climbs to serve buildings);
> the route-graph solve is proven but not yet wired into emission. NEXT = wire
> read-by-index emission, but FIRST decide the open question: solve the body
> nodes in the graph too vs grade the body shapes after applying the skeleton.

**Authoritative user spec, 2026-06-24.** This replaces the legacy multi-pass
elevation solver in `elevation_per_surface/unified_jacobi.py`. The central rule:

> **The new solve is the ONLY thing that sets elevations.** Every legacy pass that
> modifies elevations must be DISCONNECTED and marked for deletion, or it will
> corrupt the solve. Single source of elevation truth.

## The model

Inputs / hard anchors (KEEP):
- **Runways** — the FAA vertical profile (immutable).
- **Tile seams** — DEM-pinned for cross-tile continuity.
- **Reach band** — `building_feasibility.reach_band_sampler`: the feasibility
  ENVELOPE `[floor, ceil]` per point (reachable within grade from every
  taxiway↔runway contact; contact-anchor model already landed). NOTE: this is a
  band, not a single value — the SOLVE picks the value.

Elevation assignment (the one solve):
- **Buildings:** closest to DEM within the band (heaviest anchor). Already in
  `building_feasibility.building_feasible_levels`.
- **Aprons:** closest to DEM within the band, AND kept within their grade cap:
  - apron WITH a building → grade from the building via the visibility graph (≤cap);
  - apron with NO building → set its closest-to-DEM-feasible level via its shortest
    taxi / visible route, then grade the rest to its cap (so it is NOT left flat).
- **The route — junctions + rects (the taxi network):** solve for the SMOOTHEST,
  least-grade profile achievable BETWEEN the anchors. Rects read NO DEM — a sloping
  rect is a tilted plane defined by its two ends (`altitude_high/low`); set the
  ends from the profile, it tilts between (≤cap). Flat across the width; shared
  vertices resolve to one elevation.
- **One solve, cap-projected** → every node within grade BY CONSTRUCTION.

Geometry note: a rect never touches a runway directly — a junction always sits
between (if a rect abuts a runway, that's a geometry bug to flag, not handle).

## Pipeline inventory (what writes elevations today)

Active solve order (`unified_jacobi.py`, `_mark` labels): seed → phase1-cascade →
relief-1 → bldg-flex → runway-flex-step3 → corridor-pass-1 → flex-relief →
corridor-pass-flex → relief-post-corridor → enforce → polish+snap →
spine-climb-solve → (min-grade-network, off under SGG) → reconcile → writeback.

### KEEP (anchor inputs + output)
- `_seed_elevations` (~10849, `seed`) — DEM seed / initialization.
- Runway FAA profile (CIFP thresholds; runway_regrade / runway_redistribute).
- Tile-seam DEM pins (`_seam_pinned_runway_nodes`, seam_anchors, tile_cut).
- `building_feasibility.building_feasible_levels` + `reach_band_sampler` +
  `_sample_node_dem` — the band + building levels.
- `_writeback` (~11354, `writeback`) — elev[] → node_altitudes/altitude_high/low.
- Supporting (no elevation write): `_build_node_list`, `_build_shape_constraints`,
  `_grade_graph_context`/`grade_graph` (the unified within-shape graph),
  `_build_level_coupling` (rect flat-across-width may still be needed).

### BUILD NEW (the one solve)
- Extend/replace `grade_graph_solve.spine_carries_climb_solve` +
  `_build_route_layer` into the one-profile solve with PER-ROLE targets:
  - apron/building nodes → closest-to-DEM within band (+ apron grade-to-cap via
    visibility, incl. the no-building shortest-route seed);
  - route nodes (rect ends + junction spine) → minimise grade (smoothest);
  - cap-projected.
- Put taxi RECTS in the one solve (their two ends as graph nodes connected to
  adjacent apron/junction/building nodes); rect `altitude_high/low` from the
  solved ends.
- Extend `grade_graph_validate` to cover rects (today only apron/junction → the
  A2 cliff went unseen).

### LEGACY — DISCONNECT + mark for deletion (these modify elevations)
All default-ON and ACTIVE today unless noted; under the new model the one solve
owns elevations, so every one of these is removed:
- `_phase1_hop_priority` (~4162, `phase1-cascade`).
- `_directional_relief` (~4212, `relief-1`/`flex-relief`/`relief-post-corridor`)
  + `_project_shape`/`_project_within_bands` as relief drivers.
- `_relax_buildings_and_resolve` (~10470, `bldg-flex`; gates default OFF).
- `_relax_runway_and_resolve` (~5778, `runway-flex-step3`).
- `_taxi_corridor_profiles` (~6589, `corridor-pass-1`/`-flex`; TAXI_CORRIDOR_PROFILE
  default ON) + `_resmooth_runways_in_elev` (~5268).
- `_enforce_within_shape_grade` (~1977, `enforce`) + its band machinery
  (`_grade_bands`, `_runway_reach_bands`, `_lipschitz_tighten_bands`,
  `_fair_surface_ripples`) where only used by enforce.
- Polish+snap (all default ON): `SPREAD_APRON_GRADE`, `SEAM_APRON_COMPLEX_POLISH`,
  `TERMINAL_PADS_SLOPE` pad polish, `_snap_junction_verts_to_rect_edge_plane`
  (~5553).
- CAP_PLANAR rect-cap planar extension + rect flat-end axial machinery in
  `_build_shape_constraints` (the legacy rect plane model).
- `_spine_climb_seats` seat/lock + the `lb`/`ub` building-frontage hack +
  `_rect_end_levels` (the reverted band-aid) — replaced by the one solve.
- `_min_grade_network_solve` (~9614, off under SGG) — superseded.
- `_anchor_buildings_at_feasible_dem` / `_anchor_aprons_at_feasible_high` (gated
  OFF) — superseded.
- `_reconcile_level_coupling` (~9504, `reconcile`) — re-evaluate: keep ONLY the
  flat-across-width coupling the new rects need; drop the rest.

## Execution order (incremental, re-verify spine=0 + b16=708 + no-bowl each step)
1. Build the one solve alongside (gated), feeding it the anchors + band; get it
   producing the route (rects climb) + apron (closest-DEM, capped) correctly on
   CYXY (A2 climbs to ~707, no cliff), then SPJC/HECA/SPLP.
2. Switch the pipeline to the one solve; DISCONNECT the legacy passes above (gate
   off → then delete) one group at a time, re-verifying after each removal.
3. Extend `grade_graph_validate` to rects; retire legacy `tools/check_grade.py`
   per-role grading.
4. Re-cut fixtures; full suite green.

## Why (the A2 evidence)
A2 (a `primary_parallel` rect) emitted FLAT (`altitude_high=low=696.4`) because its
ends were never set from the profile, while building16 sat at its 708 band-ceiling
on the adjacent apron → a 12 m cliff that `grade_graph_validate` never saw
(primary_parallel isn't a validated role). Setting A2's ends from the profile makes
it climb 696→707 (proven). The legacy per-consumer band collapses + pre-locked
spine are why it was flat — hence the one-profile-solve rebuild.

---

# PROGRESS & HANDOVER (2026-06-25)

A2 climbs, no bowl, the new solver is the live default in dev. The model
DIVERGED from the original "cap-budget envelope" sketch above — read this section,
not the sketch, for the model as it actually landed and why.

## Where the package lives
`src/auto_patch/elevation_per_surface/route_profile/` — `solve.py` (orchestration),
`anchors.py` (band + building seats + role split + building-frontage spine floor),
`one_solve.py` (the projected Gauss-Seidel), `spine.py` (centerline chain from
grade_graph). Dispatched from `solver.py` on `O4_ROUTE_PROFILE_SOLVE` (**default
ON**). `unified_jacobi.py` is UNTOUCHED (its 15 passes are bypassed when the gate
is on, not yet deleted). `envelope.py` was built then DELETED (see below).

Other files changed: `config.py` (`APRON_TAXI_BLEND` default ON,
`APRON_TAXI_TRANSITION_M=30`), `grade_graph.py` (anisotropic apron↔taxi blend),
`tile_cut.py` + `pipeline.py` (post-solve airside freeze), `junction_repair.py`
(groundside chain excludes buildings).

## THE MODEL AS IT LANDED (supersedes "BUILD NEW" above)
ONE reachability graph: the taxi-route reach band
(`building_feasibility.reach_band_sampler` / `shared_taxi_route_graph`). It sets
the building levels AND bounds the spine. **A 2-D `cap_budget_envelope` (a second
reachability graph) was built then REMOVED** — it routed over the shape graph, not
the taxi route, so it disagreed with the buildings (user: "ONE GRAPH").

- **Spine** (centerline nodes): bounded by the band; clamps ONLY to its
  centerline-consecutive chain (`spine.py`) → spine ≤cap.
- **Apron body**: grades FROM the spine/buildings via the neighbour cap slabs
  (NOT band-bounded directly — the band tracks the spine's climb, which would
  over-cap the apron along-route).
- **Buildings**: flat at the MEDIAN band ceiling over their frontage ring.
- **Apron↔taxi blend** (shared grade_graph, so solver+validator agree): an apron
  edge earns the route's cap on its ALONG-route component near a taxiway; at a
  building frontage the blend is isotropic toward the 4% back-edge cap (the
  apron WARPS to blend a flat pad into the climbing route — the twist).
- **Building-frontage spine floor** (`anchors.building_spine_floor`): a spine node
  a building fronts rises to within an apron grade of the pad, using the SAME
  visible-centerline route the band used. Makes an ARM climb to serve its pads
  (CYXY G arm 701.7→705.6 for building6/8). Clamped to the band ceil.
- **Service roads / road-only aprons** follow DEM (not the taxi band / runway
  chain): `anchors._DEM_BODY_ROLES`; `junction_repair` groundside chain excludes
  buildings + SVC.

## Status vs the 4 execution steps
1. **Build the one solve (gated)** — ✅ DONE. A2 climbs; works on all 4 fixtures.
2. **Switch pipeline + disconnect legacy** — ◐ PARTIAL. Gate default-ON (pipeline
   switched). The 15 legacy passes are BYPASSED (gate on → `unified_jacobi.solve`
   never runs) but NOT deleted. Fixed the one post-solve elevation modifier (the
   tile-cut airside re-sample, now frozen under the gate). To do: delete the dead
   passes once verified.
3. **Extend grade_graph_validate to rects; retire check_grade per-role** — ❌ NOT
   STARTED. (Rects still aren't validated by the single graph — A2-class cliffs
   would still go unseen by `within_violations`.)
4. **Re-cut fixtures; full suite green** — ❌ NOT STARTED. Suite not run under the
   gate; fixtures encode legacy output so they'll diff.

## Current numbers (PYTHONHASHSEED=0, grade_graph_validate within-viol / spine)
CYXY 860 / 0 · HECA 4507 / 0 · SPJC 679 / **2** · SPLP 128 / 0. b16=709.4,
b19=700.2 (no bowl). `test_cyxy_spine_zero_no_bowl` passes under the gate.

## WHAT'S NEXT — one connected theme: spine smoothness + band/spine consistency
X-Plane review shows the spine is grade-LEGAL (spine=0) but not visually smooth or
consistent. All of it traces to: the spine is not solved as ONE smooth continuous
profile, and a building level can out-run what its serving spine can actually
reach. Priority order:

1. **★ Band-Lipschitz consistency (the key structural fix).** Make the per-point
   band grade-consistent on the grade graph — `ceil_i = min_j(ceil_j + cap·dist)`
   SEEDED FROM THE BAND (not the runway, so it can't disagree with buildings). This
   stops a building level exceeding what its through-spine supports (CYXY
   building19 = 700.2 while its serving through-taxiway ~U12 sits flat at 694.6,
   106 m away — the band assumed ~U12 climbs along its route; the spine solve
   keeps it flat). Forcing the spine up (a ceil-override) just manufactured
   violations — reverted. This is the one that unblocks building19-class pads.
2. **Spine continuity/smoothness** (spine=0 ≠ smooth — the validator checks grade
   magnitude, NOT curvature): (a) stitch same-taxiway centerline SEGMENTS across
   gaps (CYXY G has a ~48 m centerline gap → a flat dip); (b) fold rect ends + their
   caps onto the spine chain (A2: threshold→cap 207→rect kinks instead of one
   plane); (c) minimise CURVATURE along the chain so it's a smooth monotone climb.
3. **SPJC spine 0→2** — small regression introduced during the blend/seating work.
4. Then the original steps 3 (validate rects) + 4 (recut fixtures, full suite) and
   delete the dead legacy passes.

## Gotchas for the next session
- PYTHONHASHSEED=0 (indexing nondeterministic); builds 60-90 s; probes in `/tmp`.
- DEM = `elevation._load_airport_dem` (apt_smoothing_pix=8 from repo cfg).
- spine metric = `grade_graph_validate.within_violations` filtered to `v[4]`. The
  legacy `tools/check_grade.py` does NOT understand the apron↔taxi blend (flat 1%);
  don't trust it for apron numbers.
- The apron↔taxi blend lives INSIDE `grade_graph.shape_constraints`, so the
  validator and solver share it automatically (one graph).
- `O4_ROUTE_PROFILE_SOLVE=0` reverts to the legacy solver (byte-identical).
