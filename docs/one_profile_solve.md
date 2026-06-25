# The One-Profile Solve (next-gen elevation solver)

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
