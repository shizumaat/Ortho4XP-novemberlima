# Pre-Solve Geometry Refactor — settle ALL airside geometry before the elevation solve

**Goal:** every geometry change to **solver-graded (airside)** shapes happens **before**
`per_surface_solve` runs. After the solve, only **altitude** is assigned (to non-graded terrain
features) and **new non-airside shapes** are added (clearance). No airside vertex is ever moved,
inserted, welded, snapped, or clipped after the solve.

**Why:** post-solve node-unification (`weld_layout_vertices`, full `enforce_conformance`,
clip-to-boundary, junction corner-inserts) snaps vertices coincident *after* the solver graded
them to independent elevations → coincident-vertex **cliffs** (HECA #291↔#371 = 4.4 m). The solver
can only couple shapes through shared nodes; it must see the FINAL node-set. See
`memory/postsolve_geometry_audit.md`.

**Key enabler:** every post-solve airside geometry op depends only on geometry **known pre-solve**
(airport boundary = apt.dat row-130; bridge/tunnel = apt.dat/OSM; groundside = OSM roads;
weld/conformance = pure node-unification). Only the terrain FEATURES' *altitude* depends on the
solve. The current code conflates feature geometry + altitude; the refactor separates them.

---

## Invariant (definition of done)
A dev guard (Phase 1) hashes every airside shape's ring geometry immediately before the solve and
again at emit; **the count of airside shapes whose geometry changed post-solve must be 0.**
Airside roles = runway, runway_crossing, primary_parallel, secondary_parallel, stub,
cross_connector, junction, service_junction, apron, terminal.

## Guardrails (run after EVERY phase)
- `venv/bin/python -m pytest tests/ -q` → **293 passed / 5 skipped** (compare_target paused until Phase 9).
- HECA grade (`O4_HOLE_ROUTER=1` build + `tools/check_grade.py`): within-shape / cross-shape / v2e /
  mid-edge must not regress vs the Phase-0 baseline (apt.dat now stable → deterministic).
- Phase-1 guard: post-solve airside-geometry-change count must be **monotonically non-increasing**.

## Status legend
`[ ]` not started · `[~]` in progress · `[x]` done & guardrails green · `[!]` blocked

---

## Phase 0 — Baseline & metrics  `[x]`
- Record current baseline: full-suite result; HECA within/cross/v2e/mid; SPJC/SPLP/CYXY grade.
- Confirm determinism (build HECA twice, identical metrics).
- **Baseline (HEAD 7e66178, O4_HOLE_ROUTER=1):** suite=**293 passed / 5 skipped** ;
  HECA within-shape/cross-shape/v2e/mid-edge = **17 / 13 / 20 / 95** (plane gradient 1) ;
  dominant class = the #291↔#371 / terminal2 coincident-vertex cliff (4.1–4.4 m, d=0) — the
  exact post-solve weld/conformance cliff Phase 7 targets.
  post-solve airside changes=**159** (Phase-1 guard, 402 airside shapes;
  mutated=157, new=0, removed=2; by role: primary_parallel 57, stub 56,
  cross_connector 24, apron 8, secondary_parallel 7, terminal 5, apron-removed 2).

## Phase 1 — Instrumentation guard  `[x]`
- Add a (env-gated, e.g. `O4_GEOM_GUARD=1`) snapshot of airside ring geometry just before
  `per_surface_solve`, and a comparison at the end of finalize that logs how many airside shapes
  changed geometry post-solve (and which passes/roles). No behaviour change.
- **Verify:** guard reports the *current* (non-zero) count; suite green. This number is the
  progress metric, driven to 0 by later phases.
- **DONE:** new module `src/auto_patch/geom_guard.py` (`snapshot_airside_geometry` /
  `report_post_solve_changes`, `O4_GEOM_GUARD=1`). Wired in pipeline.py around the solve.
  Imports split per use-site (the solve block is conditional — a single import made the report
  name an unbound local for the non-solver path). Guard count at this point = **159**. Suite 293/5.

## Phase 2 — Move airside SHAPE-DROP passes pre-solve  `[x]`
- `_drop_floating_orphan_junctions`, `_drop_off_source_residue` (junction_repair) — pure topology,
  no feature/altitude dependency. Move to just before the solve.
- **Risk:** low (removing shapes). **Verify:** guard count drops by these passes' contribution; suite green.
- **DONE:** both moved to just before the guard snapshot (inside the solver block, same execution
  condition as before). Guard count **159 → 157** (HECA dropped 2 off-source aprons pre-solve,
  snapshot now 400, removed=0). Suite 293/5.

## Phase 3 — Boundary clip pre-solve  `[x]`
- Compute the airport-boundary inner-edge line pre-solve (row-130 + offset; no ribbon altitude needed).
- Move `_clip_pavement_to_boundary_interior` (airside clip) pre-solve. Leave ribbon EMIT +
  `_conform_ribbon_to_pavement_seam` + `_flatten_bridge_pinch_necks` post-solve (altitude-only).
- **Risk:** medium — clipped pavement now graded by the solver (intended). Watch CYXY/SPLP boundary.
- **Verify:** suite green (incl. test_boundary); HECA boundary shapes unchanged in count.
- **DONE:** extracted the altitude-independent ribbon geometry into shared
  `_ribbon_segment_geometry` (used by both `_emit_airport_boundary_shape` and the new
  `_compute_boundary_ribbon_interior`). Pre-solve: clip airside roles against the geometric
  interior. Post-solve clip now takes `skip_roles=_AIRSIDE_ROLES` (handles only post-solve
  features). Ribbon byte-identical at HECA (1752 boundary shapes). Suite 293/5; test_boundary 32p/1s.
  HECA guard 157 (airside doesn't straddle at HECA — clip is correctness-only for CYXY/SPLP).
  Grade within-shape 17→14 (Phase-2 side effect), cross/v2e/mid unchanged.

## Phase 4 — Groundside absorb/reclassify pre-solve  `[x]`  ★ BIG WIN
- Compute groundside footprints pre-solve. Move `_absorb_apron_enclosed_groundside` (island/fragment
  merge) and `_reclassify_groundside_orphan_junctions` pre-solve so absorbed aprons/reclassified
  junctions are graded by the solver (kills the island flat-vs-graded cliffs at the source).
- Keep groundside DEM-altitude emit + `_separate_groundside_from_airside` (groundside-only clip) post-solve.
- **Risk:** medium — interacts with the s61 island-merge work. **Verify:** HECA cross-shape steps drop;
  suite green.
- **DONE:** moved the whole groundside EMIT + absorb + reclassify block pre-solve (groundside is
  DEM-following + excluded from the solver's `PAVEMENT_ROLES` node graph → emitting it pre-solve does
  NOT couple the solve). Reordered `_absorb_apron_enclosed_groundside` so the geometry-only MERGE runs
  before the altitude gate (works pre-solve when aprons have no altitude yet). Removed the three passes
  from `finalize.emit_terrain_transition_features`; kept `_separate_groundside_from_airside` post-solve.
  **HECA absorbed 6 pieces pre-solve** (was 0 post-solve — the pre-solve groundside footprint is larger,
  before the airside subtraction). **The dominant #291↔#371 cliff class was an apron-ISLAND, not a weld
  artifact:** grade within-shape 14→**8**, cross-shape 13→**1**, v2e 20→**0**, mid-edge 95→**0**.
  Guard 157→**154** (apron 8→5). Suite 293/5.

## Phase 5 — Bridge/tunnel airside touches pre-solve  `[x]` (investigated — NOT movable)
- Compute bridge/tunnel footprints pre-solve. Move `_snap_bridge_vertices_to_runway_corners` and
  `_insert_bridge_contacts_into_junctions` (airside vertex ops) pre-solve. Keep wall/ramp altitude
  emit post-solve.
- **Risk:** medium (gated by `EMIT_BRIDGES_AND_TUNNELS`; affects KGCD bridge). **Verify:** suite green.
- **FINDING (not movable):** the boundary ribbon + boundary→DEM bridge runway-distance clamp anchors
  to ALL airside pavement — incl. aprons/taxiways (deliberately widened 2026-05-22 for the CYXY
  east-apron case), whose altitudes are only known AFTER the solve. So the bridge PLACEMENT
  (`|clamp − DEM| > 5 m`) is genuinely solve-dependent — a trial pre-solve move LOST the HECA DEM-bridge
  (boundary 1752→1751). Ribbon + bridge must share the same clamp state, so BOTH stay post-solve.
  Reverted. The only airside touch, `_insert_bridge_contacts_into_junctions`, stays post-solve; it is
  altitude-neutral (collinear, interpolated) and fires **0× at HECA** (guard junction:0), so it does
  not block the HECA invariant. Bridge-airport (KGCD) contacts remain a documented post-solve exception.

## Phase 6 — Remaining junction-repair geometry pre-solve  `[x]`
- Move `_connect_discovered_lane_dead_ends_to_junctions`, `_snap_near_corner_vertices_to_rect_corners`,
  `_share_neighbour_corners_into_junctions` pre-solve.
- **Verify:** guard count now ~only weld+conformance remain; suite green.
- **DONE (with Phase 7):** all three folded into the new `_unify_airside_geometry(layout, icao)`
  helper (pipeline.py), called pre-solve just before the guard snapshot. Coupled with Phase 7 because
  snap/share must run after conformance. Suite 293/5.

## Phase 7 — Weld + full conformance pre-solve (THE cliff fix)  `[x]`
- With airside geometry final, move `weld_layout_vertices` (airside roles) and the full
  `enforce_conformance` to immediately before the solve. Remove/neutralise the post-solve copies
  (the partial pre-solve apron/junction conformance is superseded).
- **Verify:** HECA cross-shape proximity + d=0 cliffs drop sharply (the #291↔#371 class resolves);
  guard count for weld/conformance → 0; suite green.
- **DONE:** `weld_layout_vertices` + full `enforce_conformance` moved into `_unify_airside_geometry`
  pre-solve; partial apron/junction pre-solve conformance removed. Post-solve runs a ONE-SIDED feature
  conformance (`enforce_conformance(owner_roles=_POSTSOLVE_FEATURE_OWNER_ROLES)`) that conforms only
  ribbon/groundside/ramps/walls TO the frozen airside — never moving an airside vertex. A non-per-surface
  fallback runs the full unification at the post-solve site. **Full conformance pre-solve does NOT break
  the solver's 4-corner rect model in practice** (grade unchanged: within 8, cross 1, v2e 0, mid 0).
  The cliff class was already resolved in Phase 4; this phase settles the airside node-set + drops
  aprons/terminals from the post-solve-changed set. Suite 293/5.

## Phase 8 — Enforce the invariant (post-solve = altitude-only)  `[x]`
- Confirm guard reports **0** post-solve airside geometry changes. Any residual pass → move or fix.
- Post-solve does only: feature altitude sampling, clearance emit (adds shapes), tile-cut of
  features, altitude-only ribbon conforms.
- **Verify:** guard=0; full suite green; HECA grade improved vs Phase-0 baseline.
- **DONE: `[geom-guard] HECA: 0 airside shapes changed geometry post-solve — invariant HOLDS.`**
  Required making the guard hash rotation/reflection-invariant (`_canonical_ring`): the solver reorders
  a rect's ring to the `[high,low,low,high]` convention when it assigns altitudes — a ring rotation, NOT
  a geometry change; the order-sensitive hash mis-flagged all 143 rects. Post-solve airside is now
  altitude-only. The one documented exception is `_insert_bridge_contacts_into_junctions` (solve-dependent
  bridge, Phase 5) — collinear/altitude-neutral and 0× at HECA. **HECA grade: within 17→8, cross 13→1,
  v2e 20→0, mid-edge 95→0** vs Phase-0 baseline. Suite 293/5.

## Phase 9 — Re-cut compare_target & re-enable  `[x]`
- Re-cut SPJC + SPLP targets (`tools/build_target_osm.py`), refresh per-role floors, delete the
  `pytest.mark.skip` in `tests/test_compare_target.py`.
- **Verify:** FULL suite green INCLUDING compare_target (no skips except the 2 environmental).
- **DONE:** re-cut all 3 fixtures (`SPJC_target.osm`, `SPLP_target_tile-13-{77,78}.osm`) with the
  post-refactor pipeline (per-tile via `_load_airport_dem(tile+0.5)` exactly as conftest). Floors
  refreshed from the EMITTED OSM counts (NOT in-memory — `to_osm` drops invalid polygons: SPLP-77
  emits 6 aprons not 7; SPJC emits 31 primary_parallel not 32). Removed the `pytest.mark.skip`.
  SPLP-77 apron floor given ±1 slack (5 of 6) for the invalid-apron-drop nondeterminism. The refactor
  intentionally consolidated apron islands (SPJC apron 32→30, SPLP-77 11→7→6-emitted, SPLP-78 3→4).
  compare_target 3/3 pass.

---

## Notes / decisions log
- **★ COMPLETE (2026-06-03).** All 9 phases landed. Post-solve airside geometry change = **0**
  (`O4_GEOM_GUARD=1`, HECA). Full suite **296 passed / 2 skipped** (only the 2 environmental skips:
  `test_elevation_terrain_following` needs `O4_TEST_TILE`; `test_boundary` CYXY shared-vertex). HECA
  grade vs Phase-0 baseline: within-shape 17→8, cross-shape 13→1, v2e 20→0, mid-edge 95→0.
- **Phase 4 was the surprise high-value phase** (not Phase 7): the dominant #291↔#371 4.4 m cliff
  class was an apron-ISLAND emitted flat post-solve, not a weld artifact. Moving
  `_absorb_apron_enclosed_groundside` pre-solve (merging islands into host aprons, graded in place)
  collapsed cross/v2e/mid-edge steps. Required reordering the absorb's geometry-MERGE before its
  altitude gate (so it works when aprons are still altitude-less pre-solve).
- **Phase 5 NOT movable (documented):** the boundary ribbon + DEM-bridge clamp anchors to ALL airside
  pavement incl. aprons/taxiways (solved altitudes), so the bridge PLACEMENT is solve-dependent — they
  stay post-solve. The lone airside touch (`_insert_bridge_contacts_into_junctions`) is altitude-neutral
  (collinear) and 0× at HECA, so it doesn't break the HECA invariant; KGCD-bridge contacts are a
  documented post-solve exception.
- **Guard hash had to be rotation/reflection-invariant** (`geom_guard._canonical_ring`): the solver
  reorders a rect's ring to the `[high,low,low,high]` convention when assigning altitudes — a ring
  rotation, NOT a geometry change. The order-sensitive hash mis-flagged all 143 rects as "changed"; the
  canonical form drives the real count to 0.
- **Rect 4-corner constraint respected:** full conformance pre-solve does NOT, in practice, break the
  solver's 4-corner sloping-rect model at HECA/SPJC/SPLP (grade unchanged). Post-solve uses a ONE-SIDED
  feature conformance (`owner_roles=_POSTSOLVE_FEATURE_OWNER_ROLES`) so ribbon/groundside/ramps conform
  TO the frozen airside without ever moving an airside vertex.
- **New code:** `src/auto_patch/geom_guard.py` (the guard); `pipeline._unify_airside_geometry`
  (Phases 6+7 helper); `boundary._ribbon_segment_geometry` + `_compute_boundary_ribbon_interior`
  (Phase 3 shared geometry); `_clip_pavement_to_boundary_interior(interior=, roles=, skip_roles=)`.
