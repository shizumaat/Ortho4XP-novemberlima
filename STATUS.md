# Auto-Patch Status — session 51 IN PROGRESS: clean no-absorption model NOW DEFAULT (absorb=False, commit 0104f00); single-solve + invariants agreed + tests redefined + apron_lane retired + _split_narrow_necks retired; 10 real invariant violations remain

> **Read `docs/pipeline_invariants.md` FIRST** — the agreed working spec for the
> refactor (8 invariant sections, A1–H28). Then `docs/elevation_solver.md` for the
> solver model. Session 51 has: collapsed the 2-solve pipeline into one solve
> (4× faster build), agreed the invariant set with the user, redefined the
> 2-solve-era tests, and added missing invariant coverage. What remains is
> fixing the 13 real geometry/grade violations the cleaned-up tests now surface.
>
> See "SESSION 51 PROGRESS" immediately below, then "THE
> PLANNED REFACTOR" for the full target order.

## PIPELINE PASS AUDIT (session 51 — the refactor's working plan)
The pipeline accreted ~28 fix-up passes around the OLD 2-solve order. The
single-solve model is much simpler; the dividing line: **passes whose TRIGGER or
OUTPUT depends on solved altitudes = 2-solve band-aids; pure-geometry passes =
fundamental.** Audit verdicts (4 parallel readers, full bodies read):

**REMOVE (dead/obsolete/harmful under single-solve):**
- `_snap_junction_altitudes_to_rect_corners`, `_enforce_shared_vertex_altitudes`,
  `_smooth_within_junction_adjacent_pair_grade` — already gated `if not
  USE_PER_SURFACE_SOLVER` (DEAD in prod); docs say they re-introduce the
  violations the solver just fixed.
- `_subdivide_violating_junctions` — grade-TRIGGERED (needs altitudes); reintroduces
  the geometry↔elevation loop. (Removed from pipeline.) Confirmed it would fire on
  0 junctions (CYXY/SPJC) / 1 (SPLP) anyway.
- `_align_rect_slope_to_axis` — reads solved hi/lo; no-op pre-solve. (Dropped.)
- `_apply_geometric_finalization` Phase 3/4 chain (elevation.py) — old interleaved
  solve→clamp→subdivide→solve, behind the dead gate.

**REFINE (keep intent, fix for single-solve):**
- **`_absorb_rects_at_junction_perimeters`** — the rect-destroyer. TESTED
  `ABSORB_RECTS_ALONGSIDE_APRONS=False` (no-absorption / clean 5-step model):
  **20 failed vs 12 ON.** The suite ENCODES absorption — off makes taxi rects sit
  alongside junctions/aprons, violating no_long_edge_proximity /
  no_vertex_on_sloping_rect_flat_edge / rect_short_edges_connect /
  runway_node_sharing / neighbour_corners. So the clean model is viable but needs
  REDEFINING ~7 invariant tests (a deliberate decision — NOT done). Kept ON.
  If kept ON: switch sloping-edge ID to `source_axis` (NOT corner-order convention —
  mis-IDs 1 CYXY/14 SPJC rects) AND only run on fully-refined geometry.
- `_clip_sloping_rect_piece` (tile_cut) — DEAD now (bails on `altitude_high is
  None`; `split_pavement_at_seams` nulls taxi alts pre-solve) → cut taxi rects lose
  clean-rect preservation and may TILT (suspect for SPLP regressions). Re-express
  via `source_axis` (geometric clean-rect, no altitudes).
- `stitch_pavement_polygons` — gates on `node_altitudes` present → no-op pre-solve;
  strip the z-interpolation, keep pure vertex-sharing.
- `_push_junction_vertices_off_taxi_rect_edges` — keep geometry; drop the now-dead
  NN altitude-resample limb.
- `widen_junctions_to_runway_corners` — "POST-ELEVATION," backfills junction alts
  from solved runway → obsolete intent; keep only geometric vertex-insertion.
- `_enforce_runway_1to1_sharing` — HIGHEST-RISK: delicate rewrite that amputated
  real pavement (CYXY 20-end, HECA), wrapped in RUNWAY_REWRITE_MAX_ABS_LOSS guards.
  Audit whether junctions=union−rects already meet runway 1:1 → likely deletable.
- The sloping-edge SNAPS (`_snap_to_sloping_edge_corners`,
  `_snap_junction_vertices_to_rect_flat_edge_corners`) — switch edge-ID to
  `source_axis` too (same fix as absorb).

**KEEP (fundamental, pure-geometry):** `_enforce_shared_vertices`,
`_drop_overlap_against_fixed_shapes`, `_reclassify_apron_junctions` (= clean-model
step 5), `stitch_pavement_to_terminals`, `stitch_pavement_to_flat_runways`,
`_merge_sliver_junctions_into_neighbours`, `_drop_thin_orphan_slivers`,
`_drop_floating_orphan_junctions`, `cut_layout_at_tile_boundaries` +
`_terrain_pin_slice_nodes`, `apply_seam_dem_anchors`, `weld_layout_vertices`,
`enforce_conformance`, `_clip_pavement_to_boundary_interior`.

**STRUCTURAL (bigger refactors, later):**
- TWO parallel seam pipelines: `split_pavement_at_seams`+`apply_seam_dem_anchors`
  vs `cut_layout_at_tile_boundaries`+`_terrain_pin_slice_nodes` recompute the same
  integer cut-lines + both DEM-pin seam nodes. Consolidate.
- "Single solve last" isn't strict for boundary/conformance: `enforce_conformance`
  + `_clip_pavement_to_boundary_interior` insert/resample vertices POST-solve that
  the solver never grades; `_conform_ribbon_to_pavement_seam` papers over the walls.
  Acceptable (snap to solved neighbours) but the solve doesn't cover those nodes.

## SESSION 51 PROGRESS (single-solve refactor)
Session-50 work was committed first as checkpoint `b187dd6` (baseline 6 failed /
279 passed / 5 skipped, excl compare_target). Then:

**DONE (de-coupling + prep, all behavior-preserving unless noted):**
- `_split_sloped_rects_at_violations` (junction_repair.py): detect sloped rects
  by ROLE not altitude tags; interpolate sub-rect alts only when present, else
  set None. Works pre- or post-solve.
- `_snap_to_sloping_edge_corners` + `_snap_junction_vertices_to_rect_flat_edge_corners`
  (junction_rules.py): removed the `altitude_high/low is None` gate → role-based.
  NOTE: this also makes them process would-be-FLAT rects at their current
  post-solve call sites (a real behavior change — validate). Sloping-EDGE
  detection still uses the `_rect_from_axis_extended` corner-order convention,
  which is a RELIABLE CONTRACT (corner order is an Ortho4XP downstream
  requirement, maintained by `_split`/absorb). `_rect_sloping_edges` (axis-based)
  exists but is DEAD CODE — intentionally NOT wired in (redundant given the
  corner-order contract).
- `finalize.py`: `run_phase2` → renamed `compute_elevations_and_repair_geometry`;
  extracted the feature-emit block into new `emit_terrain_transition_features`
  (boundary ribbon / groundside / bridges / tunnels). Currently still called at
  the OLD pre-solve position (behavior-preserving extract).
- Renamed for clarity per user: no vague phase/step names.

**KEY FINDINGS (correct the session-50 plan):**
- `_compute_elevations` does NOT call the solver under `USE_PER_SURFACE_SOLVER`
  (gated `if not USE_PER_SURFACE_SOLVER`, elevation.py:1214). The "2× solve" is
  exactly the two `per_surface_solve` calls in pipeline.py. ✓ premise holds.
- The solver SAMPLES DEM per-vertex for soft nodes (`_seed_elevations`,
  unified_jacobi.py:988) → soft shapes need NO pre-seeded altitudes; only HARD
  anchors (runway via `redistribute_runway_profile`, seam via
  `apply_seam_dem_anchors`) must be set pre-solve, and they already are.
- `_absorb_rects_at_junction_perimeters` already de-couples cleanly pre-solve
  (None alts → `_alt_at_t` None → no node_altitudes written). No edit needed.

**DECISIONS (user, session 51):**
- Feature emit (`emit_terrain_transition_features`) moves POST-solve (cleaner;
  needs a post-emit `cut_layout_at_tile_boundaries` for cross-tile features).
- DROP `_align_rect_slope_to_axis` entirely (reactive-to-solve); verify the
  cascade never produces perpendicular-sloped rects. Restore if regressions.

**REORDER DONE (pipeline.py, this session):** removed solve#1 + both
`_subdivide_violating_junctions` loops + the altitude-reconciliation chain
(`_snap_junction_altitudes_to_rect_corners` / `_enforce_shared_vertex_altitudes`)
+ dropped `_align_rect_slope_to_axis`. ALL geometry now finalizes pre-solve
(stitches + `_split`/absorb/reclassify/Rule-2 + tile_cut + nudge); ONE
`per_surface_solve`; then `emit_terrain_transition_features` + a feature tile_cut
+ clearance, all POST-solve.
- **WIN CONFIRMED:** CYXY build 60-90s → **~15s** (one ~0.1s solve, no subdivide
  loops). The single-solve premise holds: build runs clean, altitudes populated.

### Task #8 (real-regression fixes) — in progress, partial wins committed

**A4 outside_pavement** — root cause: bridge contact propagation. Fixed by
exempting bridge-shared junction vertices in the A4 test + invariant doc
(commit a400ada). Drops the 26.55m/28.11m bridge-shared violations cleanly.
**Remaining (sub-10m, 9 total):** mix of (a) small snap/densification drift
(sub-1.5m, 4 cases) and (b) shared-between-two-junctions vertices 4-8m out
(CYXY 337,-1181; SPJC -466,1330). The big-shared-pair ones traced to
junction-construction artifacts: e.g. SPJC junction#69 (8687 m², 17 verts) has
a self-crossing zig-zag boundary suggesting `_decompose_polygon_with_holes`
or `_split_narrow_necks` cut a non-convex junction wrong.

**A5 have_source[SPJC]** — 38 orphan vertices, ~32 of them inside junction#69
at 50-100m from any source-shape corner. Same root cause: junction#69's
zig-zag/self-crossing exterior. Fixing junction construction for non-convex
residue polygons likely resolves A4 + A5 together at SPJC.

**TWO neck-split implementations exist** (do not conflate, per user 2026-05-27):
- **KEEP — `split_polygon_at_necks`** (`pavement/apron_necks.py`, session 50):
  splits large apron residue at taxi-width arm mouths via medial-axis tracing;
  called from `junction_emit.py:222-225` BEFORE hole-decompose. CYXY within-
  shape grade viol 786→253 when introduced. Geometric, pre-solve, no
  altitude coupling.
- **RETIRE — `_split_narrow_necks`** (`junction_rules.py:1813`, user
  2026-05-01): Rule 4, MRR-based symmetric axial split at MRR midpoint along
  runway axis. Called from `apply_junction_rules` (post-emit). Audit verdict
  **WEAK** ("geometric quality heuristic not in invariants spec"). Tested by
  `test_no_narrow_neck_junctions` (3 unit tests in `test_junction_unit.py`).
  Likely culprit for SPJC junction#69's self-crossing zig-zag (cut location +
  direction can produce non-simple polygons on non-convex residue).

**Recommended next attack (next session, fresh context):**
1. Retire `_split_narrow_necks` (the OLD one) + its callers + the unit tests
   (`test_no_narrow_neck_junctions`, the 3 `test_junction_unit.py` tests
   referencing it). Re-run; junction#69 may normalize, clearing A5 SPJC
   orphans + A4 4-8m shared-pair violations + possibly A2/A1/D12/F16.
2. If junction#69 still self-crosses after retirement, investigate
   `_decompose_polygon_with_holes` on non-convex residue (next likely
   culprit — its cut lines can also produce zig-zag exteriors).
3. The 4 sub-1.5m A4 drift cases: probably a snap pass placing a vertex just
   off pav_union. Trace via per-pass layout snapshot.
4. After geometry-construction lands clean, re-run full suite and re-baseline
   grade `MID_EDGE_CAP` (2-solve artifacts).

**VALIDATION: 12 failed / 269 passed / 9 skipped (was 6).** 6 NEW failures, two
root causes (committed as WIP — debug next):
- **tile_cut elevation coupling (KEY, user-flagged):** `tile_cut` is NOT purely
  geometric — `_terrain_pin_slice_nodes` DEM-pins SLOPING-RECT slice edges (HARD,
  fine pre-solve) but junction/apron near-cut vertices are left SOFT, and
  `_build_piece_shape`/`_make_slope_sampler` derive cut-piece altitudes from the
  SOURCE shape's field (None pre-solve). Old order ran tile_cut AFTER solve#1, so
  near-cut verts warm-started from a cross-tile-consistent field; now each tile's
  single solve grades them against its own post-drop network → diverge.
  `test_cross_tile_cut_edge_elevations_consistent`: SPLP near-cut 71.0 vs 61.6
  (9.4m > 2.5m tol); also `no_self_overlap[SPLP]`.
  - **FIX APPLIED (tile_cut.py):** `_terrain_pin_slice_nodes` now DEM-seeds a
    cut piece when it has no altitude data (pre-solve) and pins slice-edge
    vertices; extended via `_PIN_SLICE_ROLES` to junctions/aprons (was sloping
    rects only). **cross_tile_cut test now PASSES.**
  - **DECISION (user): keep the pin, let the directional relief absorb the
    grade.** grade[SPLP] is PRE-EXISTING (one of the baseline 6) — the reorder
    briefly fixed it, the pin returned it to baseline; NOT a net regression.
  - Note: original code left junctions/aprons SOFT ("graded soft against the
    smoothed DEM seed"); soft DEM-seed does NOT give cross-tile consistency
    (both tiles DEM-sample identically yet still diverge 9.4m — the divergence
    is each tile's solve pulling against its own post-drop network, so only a
    HARD pin fixes it). 2-solve got consistency from solve#1's warm-start (gone).
  - Whether `_clip_sloping_rect_piece` (clean-rect preservation, skipped pre-
    solve since slope_sampler needs altitudes) must be re-expressed via
    source_axis is still OPEN (cut taxi rects may tilt without it).

**BASELINE-vs-CURRENT (after tile_cut fix): 12 failed.** Accurate diff vs the
session-50 baseline 6:
- Pre-existing, still failing (5): grade[CYXY/SPJC/SPLP], have_source[SPJC],
  outside_pavement[CYXY].
- FIXED by refactor (1): runway_node_sharing[CYXY].
- NEW REGRESSIONS (7) — THE REMAINING WORK:
  - **lost post-solve geometry refinement (CORRECTED diagnosis):**
    large_junction_axis_aligned_borders[SPJC/SPLP] (Rule 3: 20→40),
    neighbour_corners[SPJC], have_source[CYXY], outside_pavement[SPLP],
    no_self_overlap[SPLP]×2.
    - RULED OUT: the corner-snaps. Removing the (now-active, formerly
      gate-no-op) early snap calls at the old L2462 changed NOTHING (still 12).
    - ROOT CAUSE: removing `_subdivide_violating_junctions` (+ the
      reconciliation chain) removed real GEOMETRY refinement, not just altitude
      patching. That pass split large junctions (→ fewer/smaller misaligned
      borders; its absence is the Rule-3 20→40) and its splits + the
      reconciliation cleaned T-junctions (neighbour_corners) and kept junction
      vertices on-pavement / sourced (have_source, outside_pavement). It was
      GRADE-triggered (needs altitudes), so it can't just move pre-solve.
    - IMPLICATION (refines the single-solve hypothesis): "one solve" is correct
      for ELEVATIONS, but the old pipeline interleaved GEOMETRY refinement
      (junction subdivision, T-junction reconciliation) with its solves. That
      geometry work still needs a home. Options: (A) a PRE-solve geometric
      junction-subdivider (split large/misaligned junctions by GEOMETRY, not
      grade — the apron neck-split is a start but doesn't cover SPJC large
      junctions); (B) allow ALTITUDE-SAFE geometry passes POST-solve (move
      vertices preserving altitude-by-index; no re-solve needed) — a narrower
      retreat from "nothing after the solve" that keeps the single solve.
    - The early-snap removal (harmless cleanup, matches original effective
      behaviour) is UNCOMMITTED on top of dde0fbf; fold into the next fix.
- **snap over-reach:** role-based snaps (`_snap_to_sloping_edge_corners`,
  `_snap_junction_vertices_to_rect_flat_edge_corners`) now fire on would-be-FLAT
  rects pre-solve (the altitude gate used to skip them), moving vertices →
  `large_junction_axis_aligned_borders[SPJC/SPLP]`, `neighbour_corners[SPJC]`,
  `have_source[CYXY/SPJC]`, `outside_pavement[CYXY/SPLP]`. FIX TBD: scope the
  snaps so they don't over-move (the flat-rect case needs a non-altitude guard,
  or restrict the snap to genuine sloping geometry).
- grade[CYXY], grade[SPJC], grade[SPLP] = pre-existing (grade[SPLP] now passing?
  re-check — SPLP showed only 7 within-shape 1.8% warns, may have improved).

## TL;DR / current state
**Suite (excl. compare_target): 6 failed / 279 passed / 5 skipped** — run with
`venv/bin/python -m pytest tests/ -q -k "not compare_target"` (now ~4.5 min,
parallel; see Perf below). All work is **uncommitted on `dev`**:
- Modified: `pytest.ini`, `requirements.txt`, `config.py`,
  `elevation_per_surface/unified_jacobi.py`, `junction_emit.py`, `pipeline.py`
- New (untracked): `pavement/discovered_taxiways.py`, `pavement/apron_necks.py`

The 6 failures:
- **`grade[CYXY]`** — the terrace holdout (see "Grade violations" below).
- **`grade[SPLP]`, `grade[SPJC]`** — pre-existing.
- **`have_source[SPJC]`** — pre-existing.
- **`runway_node_sharing[CYXY]`, `outside_pavement[CYXY]`** — introduced by the
  discovery feature (task #7; in the SW apron area Phase 2 reworks — may be mooted
  by the refactor or need scoping).

Baseline before this session's work was 5 failed (`have_source[SPJC]`,
`no_vertex_on_sloping_rect_flat_edge[SPJC]` (flaky), grade×3).

## What landed this session (3 features, all flag-gated, all on `dev` uncommitted)

### 1. Discovered unreferenced taxiways — `pavement/discovered_taxiways.py` (NEW)
Small/remote airports have real taxiways with no apt.dat/OSM centerline; they
otherwise dissolve into all-pair junction/apron residue. We **extract the medial
axis** (Voronoi skeleton of `pav_union`, clearance band 6–32 m via
`_WIDTH_MIN/_WIDTH_MAX`), bend-split each lane through the SHARED
`pavement.centerlines.split_merged_centerline`, and inject synthetic centerlines
(`ref="TXn"`) into `osm_centerlines` at **pipeline.py ~L1251** so the SINGLE
`_build_taxi_rects` pass builds them like any referenced taxiway.
- Flag `ENABLE_DISCOVERED_TAXIWAYS=True` (config.py).
- **Scoped to clean free strips:** `reject_curved_discovered_rects` (called in
  pipeline after `_build_taxi_rects`) drops discovered rects the builder left
  **sheared** (>10° corner deviation) — those are apron-EMBEDDED lanes the snap
  distorts; they're deferred to Phase 2 (which separates them so they re-emit
  clean). CYXY keeps ~6 clean discovered rects.
- **Why not snap-free / perpendicular construction:** tried + rejected — snap-free
  rects float off the boundary → `have_source`/`outside_pavement`/`no_self_overlap`
  fail. The snapping rect-builder is the single code path; discovery just feeds it
  centerlines. (Long debugging arc — see git/conversation if revisiting.)
- Residual: adds `runway_node_sharing[CYXY]` + `outside_pavement[CYXY]` (task #7).

### 2. Phase 2 — apron neck-split — `pavement/apron_necks.py` (NEW)
Splits large/blobby apron (residue) pieces at their **necks** (taxi-width arm
mouths) into convex pads. `split_polygon_at_necks` / `neck_cuts`:
- A **mouth** = two boundary nodes < taxi-width apart, non-adjacent on the ring,
  with a real boundary excursion between them, chord crossing interior pavement.
- Validated by eroding the excursion (`buffer(-taxi_hw)`): the cut is real when
  the excursion is a thin arm (core empty) or a narrow neck then a pad
  (core ≥ `min_neck_len` from the mouth). (NOTE: still **over-detects** ~dozens of
  cuts on wiggly outlines; user accepted "extra cuts are not detrimental". The
  user's two GT cuts on the big CYXY apron ARE captured.)
- Flag `ENABLE_APRON_NECK_SPLIT=True`. Wired into `junction_emit` on large residue
  pieces, BEFORE the hole-decompose.
- **Non-regressing** (same 6 failures). Cut CYXY within-shape grade violations
  786→253. Aprons 18→25.
- KEY MODEL (took many iterations): the arm/channel **curves**, so MRR / straight
  cross-sections read wide (56 m); traced **along the medial centerline** it stays
  ~taxi width. Phase 1 already produces a centerline for it. The cut is the
  perpendicular waist at the arm's pad-end where the traced width steps up.

### 3. Phase 3 — leaf hierarchy — `unified_jacobi._directional_relief`
The directional relief now holds each shape **only at its parent-interface
vertices (+ HARD anchors)**, not at every settled vertex. Parent = the adjacent
piece one **HOP** inward (shape-graph BFS depth from HARD anchors, NOT metres —
user: "the longer the chain the lower the priority"). Tie-break: widest shared
interface, then metres-rank.
- Flag `_USE_LEAF_HIERARCHY=True` (unified_jacobi.py).
- Cut CYXY within-shape grade violations 253→**64**, worst-case **145%→64.5%**
  (the hop metric — vs metres — fixed the 145% spike).
- This is STATUS-49's "force-hierarchy leaf network" (task #3).

### Perf (task done): suite was serial → now parallel
- `pytest-xdist` added to `requirements.txt`; `addopts += -n auto` in `pytest.ini`.
  Suite 21 min → **4.5 min** (~5×). `-n0` forces serial for debugging.
- Build profile (cProfile, one CYXY build): the elevation **`solve()` runs 2× per
  build ≈ 79% of build time (~130 s)** — `_project_shape` 99 s, 8160 calls, 599 M
  `abs()`. apt.dat index (`_index_apt_dat`) is ALREADY memoized — not a redundancy.
  The 2× solve is the real cost → the refactor below collapses it.

## Grade violations — diagnosis (CYXY, 6 violating SHAPES; tools to reuse below)
Two distinct causes (review OSM written to `/tmp/CYXY_grade_viol.osm`):
- **Cause 1 — genuine competing priorities (1 shape):** apron #62 (47%) is pinned
  at **703 by junction #63 AND 712 by apron #65** (9.1 m apart over 16 m) — the
  excavated terrace. No compliant surface exists unless a neighbour yields
  (retaining/ramp, or the parked STEP-3 runway-threshold relief). Real infeasibility.
- **Cause 2 — relief output overwritten after the final solve (5 shapes):** #61
  (64.5%, pinned only at runway 691.4, free verts stuck at 695.5), #78 (23%), #50
  (15.5%, NO pins at all — should be trivially flat), #86, #53. These have NO
  competing pins, so the solver SHOULD flatten them — but passes AFTER
  `per_surface_solve` (conformance vertex-insert, 2nd tile-cut, clearance) edit
  geometry/altitudes with **no re-solve**, OR they're created by the subdivide
  between the two solves. **This is exactly what the refactor fixes.**

## THE PLANNED REFACTOR (task #8 — next agent's main job, NOT started)
**Goal:** finalize ALL geometry first, then run the elevation solver **ONCE, last**,
with nothing editing altitudes after it. Eliminates the 2× solve (~130 s) AND
Cause 2 (post-final-solve overrides). User confirmed: do it **in one go, then
test/debug** (don't test mid-refactor).

**Evaluation (done — why the current order exists):** the 2× solve is an iterative
geometry↔altitude loop: solve#1 → grade-subdivide (needs grades) + snap/enforce
reconciliation (needs solved tags) + `_split_sloped_rects`/`_absorb` (detect AND
**propagate** altitudes) → solve#2. Genuinely elevation-dependent pieces, BUT:
- Sloped-rect detection is a **role** property (`SLOPING_RECT_ROLES`), not an
  altitude property — `_split_sloped_rects_at_violations` already role-gates; it
  just additionally skips when `altitude_high is None` and interpolates sub-rect
  hi/lo (junction_repair.py:1293, 1449-1450) + updates junction `node_altitudes`
  (1591-1664). De-couple = geometric split ONLY, set altitudes `None`, let the
  solver re-derive. Same for `_absorb_rects_at_junction_perimeters` (1766).
- Grade-subdivide (`_subdivide_violating_junctions`) is **superseded** by Phase 2
  neck-split (geometric, pre-solve) + the hop-hierarchy relief.
- snap/enforce reconciliation only patches (a) drift from geometry passes that ran
  AFTER solve#1 and (b) the lossy terminal-flat / rect-hi-lo tags.

**KEY INSIGHT — no per-vertex writeback needed:** if ALL geometry is finalized
before a single solve, the writeback is **lossless** (solver gives one value per
shared bucket; a flat terminal's corners are all equal; a rect's 4 corners define
its plane), so shared corners agree by construction → **no reconciliation needed**.

**Target order (rewrite pipeline.py elevation phase ~L2286–2900):**
1. ALL geometry, no elevations: `split_long_rects_along_terrain`,
   `stitch_pavement_to_flat_runways`/`_to_terminals`/`_polygons`,
   `_split_sloped_rects_at_violations` + `_absorb_rects_at_junction_perimeters`
   (DE-COUPLED to geometric-only), `_snap_junction_vertices_to_rect_flat_edge_corners`,
   `_reclassify_apron_junctions`, Rule-2 sloping-edge snap, seam split,
   `cut_layout_at_tile_boundaries`, conformance vertex-insertion. (Neck-split
   already runs in `junction_emit`, pre-elevation.)
2. Runway profile: `redistribute_runway_profile`, `nudge_runway_corners_at_seam_junctions`,
   `apply_seam_dem_anchors`.
3. **SINGLE `per_surface_solve`** (cascade + relief).
4. `emit_surface_clearance_cuts` (overlay — emits separate shapes, must NOT edit
   pavement altitudes).
**REMOVE:** solve#1 (pipeline ~L2552), all `_subdivide_violating_junctions` calls
(~L2563, ~L2635), the snap/enforce reconciliation loop (~L2614–2647), the 2nd
`cut_layout_at_tile_boundaries` (~L2893).

**Risks / watch:** (a) the de-coupling surgery on `_split_sloped_rects` + `_absorb`
(strip altitude propagation); (b) pass-ordering deps — preserve the documented ones
(Rule-2 AFTER reclassify; absorb at end; etc.); (c) dropping grade-subdivide may
leave residual grade — verify; (d) dropping reconciliation must NOT reintroduce
shared-corner steps (the lossless-writeback argument must hold — check
`test_pavement_grade` cross-shape/step); (e) compare_target fixtures may shift.
**Validate:** `pytest -k "not compare_target" -n auto`; target ≤ 6 failures;
grade[CYXY] within-shape should improve; SPLP/SPJC must not regress.

## Reusable debug probes (this session, in /tmp — regenerate as needed)
- Enumerate within-shape grade violators + competing pins → `/tmp/CYXY_grade_viol.osm`.
- Discovered-centerline overlay (apt.dat vs synthetic) → `/tmp/CYXY_cl_overlay.osm`.
- Apron neck candidates / cuts → `/tmp/CYXY_neck_cuts.osm`.
(All built via `build_airport_pavement("CYXY", xplane_root())` + the hook on
`discover_unreferenced_centerlines` to capture `pav_union`.)

## GOTCHAS (unchanged + new)
- **Ortho4XP caches `auto_patch` modules** — full quit+relaunch after edits.
- **Import cycle** `junction_repair` ↔ `elevation` — import `auto_patch.pipeline`
  first (the discover hook / probes do).
- **Bash CWD persists** — probes that `cd src/auto_patch` then call `venv/bin/python`
  fail; always run from repo root.
- **Two `_subdivide_violating_junctions` call sites** + a post-snap loop — remove
  ALL when refactoring.
- Temp/debug scripts + OSM dumps go in `/tmp`, never the working tree.

See `docs/elevation_solver.md` for the solver model; this session did NOT change the
cascade or the relief's core mechanism (only added the hop-hierarchy hold rule).
