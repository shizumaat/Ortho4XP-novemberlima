# Auto-Patch Status — session 55 HANDOVER (★ ENTIRE SUITE GREEN: 284 passed / 0 failed / 2 skipped, compare_target INCLUDED)

## Session 55 — build/test PERFORMANCE pass (committed 6140f46, 6d8900d, fc53573, f6ad4fb) + shared build cache (bd728b5)
Profiled the per-airport build (the dominant suite cost; tests themselves
are ~10s/airport). HECA build 70.7s → 54.6s; full HECA suite 590s (pre-cache)
→ 285s → 204s; default suite ~104s → ~84s. All changes verified
output-identical (no behaviour change); suite stays 284/0/2.
- **Where the build time goes (HECA, real):** elevation solver (Jacobi)
  ~30s, clearance emit ~26s, terrain-transition ~7s, discover-taxiways ~4s,
  apt.dat select ~2.5s. DEM load already cached (`_DEM_CACHE`).
- **#1 clearance vectorize (6140f46):** `clearance._resample_alts_over_strips`
  was O(V·all-edges) 11.7M shapely ops → STRtree `dwithin` query for the few
  candidate edges + same projection. Proven identical: 4000 randomized A/B
  trials, 0 mismatch. 20.9s → 7.9s.
- **#2 apt index persist (6d8900d):** `apt_dat_reader` now pickles its
  header index to a temp file (`_APT_DAT_PERSIST_PATH`, keyed by
  path+mtime+size, self-healing/corruption-tolerant). First scan 2.67s →
  warm 23ms across processes/workers/sessions. Bump the `_v1` filename if
  the cached tuple's meaning changes.
- **#3 solver hoist (fc53573):** `_project_within_bands`/`_project_shape`
  precompute loop-invariant `held`/`members` once instead of per-edge-
  per-sweep. Bit-identical (HECA altitude sha unchanged). 59.7s → 54.6s.
- **#4 grade-reuse (f6ad4fb):** `test_pavement_grade` reuses the cached
  layout for SINGLE-TILE airports (per-tile build is bit-identical when no
  integer line crosses the footprint). Saves a redundant ~55s build per
  single-tile airport. Multi-tile (SPLP) still builds per tile.
- **#5 smoothed migration + SPLP dedup (049a7d3):** compare_target +
  tile_cut_parity built per-tile with a RAW `O4DEM(fill_nodata='to zero')`;
  grade + production use the SMOOTHED `_load_airport_dem`. Raw≠smoothed
  (SPLP tile-77 259 vs 250 shapes; primary_parallel 5→7) — so
  compare_target was gating NON-SHIPPED geometry (correctness gap, not just
  perf). Unified all per-tile builds on smoothed via
  `cached_airport_layout` (tile path raw→`_load_airport_dem`); grade +
  tile_cut now build through the cache; tile_cut tests + compare_target_splp
  pinned `xdist_group("SPLP")`. Re-cut SPLP_target_tile fixtures + floors
  (primary_parallel 5→7/4→7, secondary 3→4, totals 236→238/317→325).
  **SPLP builds 10→4, total suite builds 14→8, default suite ~104s→~73s.**
  Note: with only 3 baseline airports + loadgroup, serial `-n0` (~63s) is
  competitive with parallel (~73s); parallelism wins on larger airport sets.
- **Test infra (bd728b5):** one shared session layout cache in
  `conftest.cached_airport_layout` (lru, keyed icao+compute_elevations+tile)
  replaces the per-module/per-test rebuilds; `pytest.ini` adds
  `--dist loadgroup` + a collection hook tagging each airport-parametrised
  test `xdist_group=<icao>` so an airport builds once per run. NOTE: plain
  `--dist load` is SLOWER (178s vs 102s) — it duplicates the same build
  across workers; loadgroup is correct. Remaining full-suite bottleneck =
  **SPLP's genuinely-distinct per-tile builds** (grade/compare/tile_cut,
  different DEMs) serialized on one worker; only a cross-worker disk cache
  would parallelize them (deferred — high risk, uncertain gain).

## Session 55 — HECA issue catalogue + coverage fix (committed 0ffb8f6)
HECA is NOT in the automated baseline (`_BASELINE_AIRPORTS` = SPJC/SPLP/CYXY);
it's a manual build/X-Plane target. Built standalone (no crash, 2407 shapes,
all valid) and ran the invariant suite via `O4_TEST_AIRPORTS=HECA`:
Started at 9 failures; **5 remain** (fixed: coverage #1, terminal #4, the
170-orphan part of #3, and rect_short_edges TX52). The original HEAZ-over-collection
X-Plane crash appears RESOLVED by the committed boundary gate (build
reports 0 off-airport / 0 overlay dropped). HECA failures (tasks 2-5):
1. **Coverage (#1) — FIXED (0ffb8f6):** `test_coverage_within_source_envelope`
   measured emitted vs apt.dat+runways ONLY, omitting DSF pavement (a
   first-class source). HECA emitted +54.9% vs apt-only but only +7.4% vs
   apt+DSF, 0.3% outside boundary = legitimate DSF, not over-collection.
   `_source_pavement_union` now adds boundary-clipped DSF. Adding source
   area only lowers overage → no airport can newly fail.
2. **Within-shape grade — 74 viol:** giant aprons exceed 1.5% over their
   whole span (apron#273 632m@1.6%, apron#209 255m@1.9%; junction#241/243).
   Apron-decomposition piece.
3. **Junction connectivity cluster:** 2 of 5 FIXED.
   - ✓ 170 orphan vertices (d78e6c4): all within 1.5m of the apt+DSF
     pav_union boundary — junction perimeters following the DSF edge. Test's
     `apt_pavement_boundary` captured row-110 only (built before the DSF
     loop). Fix: union the final pav_union boundary into it (test-only).
   - ✓ rect_short_edges TX52 (5b0d100): discovered lane dead-ending AT the
     pavement tip (both dangling corners 0.03/0.06m from the apt+DSF
     boundary). s54's 25m-isolation exemption missed it (a junction vertex
     18.8m away); added an explicit pavement-tip exemption (dangling edge on
     the boundary). TX15-style interior near-misses still flag.
   - REMAINING (3 tests, diagnosed 2026-05-29): split by tractability —
     * **neighbour_corners (1):** junction #369 v4 is 0.50m from stub J3's
       corner (-1977.4,900.1) — clean near-miss; insert/snap the shared
       corner. Tractable.
     * **vertex-on-sloping-edge (2):** junction vertex 0.50m off a sub-rect
       (W2 t=0.987 / A t=0.997) long edge near its END — sub-rect-split
       residual (`_split_sloped_rects_at_violations` pull left 0.5m).
       Probably tractable (tighten the pull snap).
     * **Rule-2 proximity (6: #213 v4, #250 v1/2, #357 v2/3/4):** MID-
       BOUNDARY junction vertices 6-29m from any rect corner, sitting on the
       apt+DSF pavement boundary BETWEEN two rect-corner-shared vertices,
       2-14m perpendicular to a nearby stub long edge. NOT a near-miss —
       the junction legitimately follows the dense-apron pavement edge past
       a stub. NO clean snap target. **Needs a DESIGN DECISION:** relax the
       20m perpendicular rule for dense layouts, or re-cut the junction.
       Don't snap (distorts the junction). PARKED for a deliberate call.
4. ✓ **terminal#9 (terminal10)** (491ed20): conformance vertex insertion
   converted the flat terminal to uniform node_altitudes (H26 violation).
   Fix: keep single-altitude shapes flat after insertion.
5. **Self-overlap** 3 pairs 1.9 m²; + build warnings (6 T-junctions + 3 edge
   crossings → mesh slivers; 8 dropped sliver/invalid polygons; DEM extrema
   −19/425 vs real ~42-165m).

## Session 55 — dead-code prune in unified_jacobi (committed 701a463)
Suite remained fully green; this session removed superseded solver
machinery only (behaviour-neutral). Removed from
`elevation_per_surface/unified_jacobi.py`:
- `_RELIEF_OUTER_SWEEPS = 60` — referenced ONLY by a comment; the old
  60-sweep relaxation it bounded is gone (replaced by the single reverse
  pass + difference-constraint bands solve).
- `_USE_LEAF_HIERARCHY` + the `parent_held` block in `_directional_relief`
  — built a per-shape parent-interface hold set that the live reverse pass
  NEVER consumes. The live pass (the `for k, sc in enumerate(order)` loop)
  computes `held` inline from `settled` + `terminal_nodes`. `parent_held`
  was assigned and discarded.
- **Retained** (still live): `rank`, `mrank`, `depth`, `node_owners` — they
  feed the `order_idx` hop-depth sort that orders the reverse pass.
- **Verification:** repo-wide grep confirmed all 3 symbols were confined to
  this one file; full suite 284 passed / 2 skipped / 0 failed (unchanged).

**Remaining open / nice-to-have (suite green, none blocking):**
- SPLP -78 taxiway A SW leg trim (geometry quality; LENGTH/TRIM at
  `_split_centerlines_at_points`; off-center + absorption-guard both ruled
  out — see MEMORY).
- Runway seam clip directive 3 (runway-aware, no terrain-pin on slice nodes).
- HECA over-collection / X-Plane crash (boundary-scope HEAZ surface attach).
- Runway-flex Level-2 (seam>CIFP) implemented but unexercised by fixtures.

## Session 54 FINAL — full suite green
`venv/bin/python -m pytest tests/ -q -n auto` → **284 passed / 2 skipped / 0 failed**
(~1:45), compare_target included. This session: runway-flex 3rd pass (all 3 grade
tests), CYXY Rule-1 + SPJC short-edge geometry fixes, SPJC stub-B two-rect fix, and
the SPJC/SPLP compare_target re-cut.

### SPJC stub B two-rect fix (committed e21cbae)
B (long ICAO-F diagonal) emitted as TWO parallel rects sharing a long edge. The
length-independent fixed-30 m diagonal trim left B's apron end in the apron mouth
(two apron pieces meet at a bend vertex) → `_split_sloped_rects_at_violations`
split it lengthwise. **Fix (2 parts):**
- `pavement/centerlines.py`: diagonal-stub end margin `max(30 m, 0.20·gap)` (was flat
  30 m). Long diagonals (gap>150 m: B/C/E) trim back enough to clear the junction
  curve; short ones keep 30 m (V3 unaffected). B → one rect.
- `junction_repair._drop_thin_orphan_slivers`: trimming C left a thin residue hugging
  C's straight long edge vs the CURVED pavement boundary — touching C at ONLY ONE
  corner (chord-vs-arc), so the "≥2 shared corners" drop gate missed it. **Relaxed:**
  also drop a thin junction whose EVERY vertex is within 5 m perpendicular of one
  rect's long (sloping) edge (`_hugs_long_edge`). General fix for any straight-rect-
  against-curved-boundary sliver, not just C.

### compare_target re-cut (user re-cut fixtures; floors refreshed this session)
User replaced `SPJC_target.osm` + `SPLP_target_tile-13-{77,78}.osm` with fuller
re-cut targets (boundary ribbon densified, runways re-cut), and removed stale
`CYXY_guide.osm` / `HECA_guide.osm` / `SPJC_target.osm.zip`. The hardcoded per-role
floors in `test_compare_target.py` were refreshed to `target − round(0.05·target)`
(SPJC total 904→1330 target / 1263 floor; SPLP-77 164→248/236; SPLP-78 213→335/317).
All 3 compare_target tests GREEN. **Re-cut workflow reminder:** after
`tools/build_target_osm.py`, update BOTH the per-role baseline dict AND the
`*_TOTAL` (run the test, read the printed `target=/out=/matched=` table, set
floor = target − round(0.05·target)).

(Earlier session-54 sections below — runway-flex, CYXY Rule-1, SPJC TX20/TX15 — remain accurate.)

# (prior header) Auto-Patch Status — session 54 (runway-flex + geometry fixes; non-compare_target was 281/0)

## Session 54 — geometry fixes after the runway-flex work (committed a5159ae + 62321ff)
The two remaining pre-existing GEOMETRY failures are FIXED; the non-compare_target
suite is now **281 passed / 2 skipped / 0 failed**.
- **CYXY `test_junction_runway_node_sharing`** (a5159ae): junction#51 had an
  orphan vertex 1.0 m off the 14L/32R runway edge, 1.89 m from the corner — a
  `boundary_dem_bridge` edge-clearance vertex (bridge clears rects by
  `buffer(1.0)`) that `_insert_bridge_contacts_into_junctions` planted on the
  junction edge. `_snap_bridge_vertices_to_runway_corners`'s `snap_tol_m` was
  1.5 m < 1.89 m, so it missed it. **Fix = widen `snap_tol_m` to 2.0 m** so the
  vertex collapses onto the runway corner (satisfies Rule 1 + neighbour_corners).
- **SPJC `test_rect_short_edges_connect`** (62321ff): two discovered (medial-axis
  "TX") lanes with a dangling short edge. **TX20** dead-ends ~74 m from anything
  — a genuine isolated dead-end (user confirmed real pavement); the TEST now
  EXEMPTS a discovered lane's dangling end when it connects at the other end AND
  both corners are > 25 m from any vertex. **TX15** ends 9.9 m SHORT of residue
  junction #132 (medial centerline terminates early; connected pre-solve, severed
  by a post-solve reshaping pass) — a MISSING CONNECTION. New
  `junction_repair._connect_discovered_lane_dead_ends_to_junctions` (post-solve,
  pre-weld) bridges the lane's end corners to the junction's nearest EXISTING
  edge (sourced vertices only) + resamples node_altitudes; weld/emit reconcile.

## NEXT: open / nice-to-have (suite is fully green — no blockers)
- compare_target re-cut + floor refresh is DONE (see FINAL section at top).
- Candidate cleanups (none blocking): SPLP stub/A apron-side residual was solved by
  the runway-flex; the old dead-code in `unified_jacobi` (`_RELIEF_OUTER_SWEEPS`,
  `_USE_LEAF_HIERARCHY` Dijkstra `rank`) is a candidate prune once stable. The
  runway-flex Level-2 (seam>CIFP threshold release) is implemented but unexercised
  by fixtures.

## (s54 earlier) runway-flex third pass (all 3 grade tests now PASS; suite 5→2)

> **READ FIRST:**
> 1. `docs/pipeline_invariants.md` — the agreed working spec (8 invariant sections, A1–H28).
> 2. `docs/elevation_solver.md` — solver model (the directional two-pass + difference-constraint solve below supersede the old cascade/relief framing).
> 3. This file — what sessions 52–54 changed and what's next.
>
> **Working tree:** session-54 work committed (15298cb, 89a2b84). Suite:
> **2 failed / 279 passed / 2 skipped** (`venv/bin/python -m pytest tests/ -q -k "not compare_target" -n auto` ≈ 1:31).
> The 2 remaining are PRE-EXISTING GEOMETRY tests (no grade test fails anymore).

## Session 54 — runway-flex third pass (committed 89a2b84 + 15298cb)
The runway profile is DERIVED from the DEM (interpolated between CIFP threshold
anchors); the DEM is the least-accurate input. When a junction/stub can't reach
grade because it's wedged between a soft apron and a runway-anchored node the
DEM dipped (CYXY 14R/32L dips ~3 m to 691.4 at the 02/20 intersection → stub A
8.9 %), the impossible connection has nowhere to go while EVERY runway node is
HARD. **Fix = a gated third pass `_relax_runway_and_resolve` in unified_jacobi
(after the reverse pass):**
- **Level 1 — free the runway INTERIOR** (CIFP thresholds + seam-pinned nodes
  stay HARD), add the runway grade-chain (`_build_runway_constraints`: long
  edges axial @1.5%, short edges flat+coupled; crossings all-pair), re-run the
  difference-constraint band solve. The DEM dip rises toward the junction and
  the gap spreads over the runway's length.
- **Level 2 — seam > CIFP last resort** (user 2026-05-28): ONLY when a
  tile-boundary seam exists and Level 1 didn't help, ALSO release the CIFP
  THRESHOLD endpoints so the whole runway yields to the seam terrain when the
  runway↔seam connection is physically infeasible (band lo>hi). Seam-pinned
  runway nodes never move. **Currently UNEXERCISED by fixtures** (SPLP solves at
  Level 1) — it's the defined safety net, low-risk but untested-by-suite.
- **Commit metric (the SPLP unlock):** accept a level iff it reduces the
  violation COUNT/TOTAL without worsening the worst (`_within_excess_stats`).
  The old "worst must improve" guard let an unrelated stubborn junction VETO a
  real fix. On commit, moved runway/crossing shapes are written back as
  `node_altitudes` (writeback skips clean runway rects / never touches
  crossings) so the runway side agrees with the shared junction.

**Results:** CYXY 14R/32L interior 691.4→~694.5 (thresholds 693.8/706.3 pinned);
SPLP runway interior near stub A 73.3→70.8 (interior, NOT a threshold — Level 1).
grade[CYXY] + grade[SPJC] + grade[SPLP] all PASS. **Suite 5→2**, no regressions.

**Correction to prior STATUS hypotheses:** CYXY and SPLP stub A were NOT "one
shared mechanism." CYXY = runway-DEM-dip (Level-1 fix). SPLP = a runway-interior
node 2.5 m too high vs a seam node (62 m) too close to grade; the blocker was the
commit METRIC, not threshold pinning. Both the "STEP 3 shift runway thresholds"
note and the SPLP-emit-consensus NEXT-ACTION are now resolved (SPLP = 0 cross +
0 within).

## Session 53 — SPJC junction-along-sloping-edge cliff (committed)
User report: SPJC `primary_parallel/V` (shape #15) survived as a sloping rect with
junction #146 running its WHOLE long edge, leaving a small bare-pavement cliff ("past
builds had this as one large junction").

**Root cause (fully traced):** `junction = pav_union − rects` is FLUSH against the rect
edge (raw residue shares 304/332 m at distance 0.00). The cliff is opened later by
`_push_junction_vertices_off_taxi_rect_edges` (`pavement/vertices.py:201`, called inside
`_compute_elevations` ~elevation.py 1198/1229): `edge_gap_m=1.0` shoves each junction
vertex within 0.5 m of a rect sloping-edge INTERIOR 1.0 m outside → ~0.79 m bare strip.
The push is correct for a STRAY vertex (would split the rect hi/lo plane) but wrong when a
junction runs the WHOLE edge. V survived construction-time dropping only because the
corridor heuristic in `_drop_primary_parallels_embedded_in_pavement` preserves any rect
within 160 m of a runway (V is runway-anchored).

**Fix (per user directive "clip/drop the rect, don't push the junction"):** ONE focused
change in `pavement/absorption.py` — added `CORRIDOR_OVERRIDE_FRAC=0.6`; corridor
preservation is overridden when a junction/apron runs flush along ≥60% of a long edge
(longest contiguous `either_adj` run). V is then dropped/clipped at CONSTRUCTION and
`junction = pav − rects` wraps the area cleanly — no merge/bridge/interior-edge artifacts.
Runway-side corridors are unaffected (runway is subtracted from `junction_pav`, never reads
adjacent). Result: cliff gone; **suite 7→5** (also cleared baseline
`vertices_outside_pavement[SPJC]` + `no_long_edge_proximity[SPJC]`); zero new failures.

**Rejected (see memory `junction_along_sloping_edge_cliff.md`):** (1) re-enable
`ABSORB_RECTS_ALONGSIDE_APRONS` — `source_axis` mis-ID premise is STALE (1 rect now, not
14); absorb-ON went 7→11, post-absorb-reclassify recovered to 8, rest = strip-corner float.
(2) post-emit `_absorb_rects_fully_under_junction` merge — fixed cliff but 7→7 (swapped
failures) from interior-short-edge + bridge artifacts. Construction-time override is
strictly better.

**compare_target:** still 3 fails (SPJC + SPLP×2) — PRE-EXISTING (identical on clean HEAD),
but SPJC geometry shifted (V dropped) so they'll need re-cutting once the suite is otherwise
green (`tools/build_target_osm.py`).

## TL;DR / where to start
Session 52 built the elevation solver out and cleaned up spurious discovered
rects; the grade violations have collapsed from dozens to a handful:
1. **Terminal = rigid flat unit** (conform forward, rigid-shift reverse).
2. **Sloped-rect flat ends = rigid coupled level** + grade-checker fixes
   (airside↔groundside wall exemption; only-where-shapes-touch).
3. **Boundary ribbon sliced like every shape** (don't cut `airport_boundary` at
   the seam) — fixed SPLP self-overlap + dropped SPLP grade 20→2.
4. **★ Direct difference-constraint solve** (`_grade_bands` +
   `_project_within_bands`, in `unified_jacobi.py`) replaced the non-converging
   relief relaxation. Grade = a difference-constraint system; multi-source
   shortest-path bands from the HARD anchors give each node's feasible
   `[lo,hi]` (multi-path handled natively, infeasible nodes flagged), then a
   bounded cap-projection over WITHIN-SHAPE edges (terminals held, rect flat
   ends coupled, both-HARD skipped) settles the rest. Per-tile within-shape:
   **CYXY 86→5, SPJC 22→2, SPLP 2→4.**
5. **Drop spurious discovered (TX) rects** (`pavement/discovered_taxiways.py`):
   (a) wider-than-long apron blobs (SPJC #44); (b) runway-parallel apron/runway-
   edge medial artifacts within 15°+25m of a runway (SPJC TX24/25/27). Both
   leave the pavement as a single junction/apron. SPJC cross-shape 6→0.

**Per-tile grade-test status now** (the binding numbers — build per-tile with
SMOOTHED DEM; whole-airport `build_airport_pavement` MIS-SAMPLES the seam DEM on
cross-tile airports and fabricates phantom seam violations — always measure
per-tile via the grade test or `/tmp/grade_detail.py`):
- SPLP: 4 cross @ 0.2 m (emit rounding) + 4 within (stub) + 2 barely-over junctions (1.6–1.9 %).
- SPJC: **0 cross** + 2 within (apron).
- CYXY: **0 cross** + 5 within (stub 4, apron 1).

## NEXT ACTION — the small residuals (no longer architectural)
1. **Cross-shape emit-consensus at shared corners** (SPLP 4 @ 0.2 m).  In the
   SOLVER a shared node has ONE elevation; the disagreement is at EMIT — a flat
   terminal writes one altitude, a sloped rect writes a 2-value plane (hi/lo
   collapse), and at the shared corner those differ from the neighbour's per-node
   value.  Make the rect/terminal emit honour the exact solved node elevation at
   shared corners (emit per-node there, or only collapse when it preserves them).
   Probe: `/tmp/diag_rect.py`.  (SPJC's terminal↔rect 1.8 m case was the spurious
   rect #44 — already gone.)
2. **A few barely-over stubs/aprons** (CYXY 5, SPLP 4 within): bands-solve
   residuals at tight spots; re-triage real vs emit-rounding.  `/tmp/grade_detail.py`.

## Solver knobs (unified_jacobi.py)
`_project_within_bands` cap-projection sweep cap is hard-coded **1000** at the
call site in `_directional_relief` (fast; grade build ≈ 21 s).  `_grade_bands`
returns `(-inf,+inf)` for nodes unreachable from a HARD anchor (left at DEM).
The old `_RELIEF_OUTER_SWEEPS=60` / `_USE_LEAF_HIERARCHY` / Dijkstra-`rank`
machinery is still present but now only feeds the single reverse pass + the
bands convergence — candidate dead-code cleanup once stable.

## The directional two-pass model (user 2026-05-28, CONFIRMED)
Priorities: if a tile seam crosses the pavement union it is highest priority;
else runway + junctions touching it = priority 1, increasing with hop-distance
outward. Terminals/leaves are outermost. Ties by area.

**Forward pass — terminal/leaves → runway** (`_phase1_hop_priority`, descending
hop-depth): START at the terminal (flat, rigid). Aprons CONFORM to the
terminal's vertices; each shape inward follows DEM clamped to its grade cap,
holding the vertices its leaf-ward neighbour settled. NEVER average. The
accumulated violation is pushed into the final junction→runway connection.

**Reverse pass — runway → terminal** (`_directional_relief`, ascending depth,
leaf-hierarchy holds parent-interface): pull the runway-touching junction the
MINIMUM to reach grade, propagate outward (stub→junction→primary→apron), each
pulled the minimum; finally RIGID-SHIFT the whole terminal (staying flat) if
needed. → grade-compliant everywhere.

## What session 52 changed (committed)

### 1. Terminal = RIGID FLAT UNIT — `elevation_per_surface/unified_jacobi.py`
`_directional_relief` (the reverse pass) now treats each terminal as ONE rigid
flat variable (lines ~748–805):
- `terminal_groups` = node-sets of each flat (terminal) shape; `terminal_nodes`
  = their union.
- Every NON-terminal shape HOLDS its terminal-shared vertices (`held |=
  terminal_nodes ∩ nodes`) → aprons CONFORM, never flex the terminal boundary.
- When the terminal shape itself is reached, `_rigid_shift_terminal(gi)`
  translates the WHOLE group to the level closest to its forward-pass DEM
  centroid (`term_level0`) that keeps every connection to a settled
  NON-terminal neighbour within grade (band from `cap_adj` = per-edge grade-cap
  adjacency over `edge_grade`). Feasible band → clamp to it (minimum shift);
  infeasible → midpoint (minimise worst violation).
- **Why this and not the freeze-pin tried first:** freezing all terminal nodes
  enforced conformance but BROKE the "rigid-shift if needed" half of the model
  (an apron squeezed between a frozen terminal and the runway couldn't reach
  grade → spurious within-apron violations). The rigid-shift fixes BOTH the
  shared-vertex consensus AND lets the relief pull the terminal up/down.

### 2. Grade-checker false positives — `tools/check_grade.py`
The two STEP checks (`_check_vertex_to_edge_step`, `_check_edge_midpoint_step`)
asserted vertical continuity between ANY two shapes within 5 m horizontally.
Two corrections (user 2026-05-28):
- **Airside↔groundside skip:** `_is_groundside` / `_airside_groundside_pair`.
  Groundside pavement (`groundside_pavement`/`service_road`/`service_junction`)
  is deliberately separated from airside by a clearance gap + retaining/vertical
  wall, often several metres — NOT meant to be flush. Skip those pairs. (This
  alone cleared ~161 CYXY false `apron↔groundside` steps.)
- **Contact tolerance `_STEP_CONTACT_TOL_M = 1.0`:** only flag a step where the
  two edges actually TOUCH (shared boundary); a gap (no pavement between, 2–5 m
  apart) may legitimately differ in height. Gate `best_d2 > tol²` in both
  checks. (`_pair_grade_limit`'s docstring wrongly claimed groundside was already
  skip-listed — it isn't; `ROLE_GRADE_LIMITS['groundside_pavement']=0.04`.)

## NEXT ACTION — sloped-rect shared-vertex consensus
The remaining grade failures are the sloped-rect EMIT gap. In the SOLVER a
shared vertex is ONE node with ONE elevation (consistent). But a sloped rect is
EMITTED as a 2-value plane (`altitude_high`/`altitude_low`, collapsed within
`_RECT_COLLAPSE_TOL_M`); at a shared corner that plane interpolates to a value
that differs from the adjacent junction/apron's per-node `node_altitudes`. Same
emit-consensus class just solved for terminals.

**Confirm the mechanism first** (don't assume): pick a worst SPJC pair, e.g.
`primary_parallel/-10030 (17.9) ↔ apron/-10112 (16.6)` at d=0.00, and check
whether they share a canonical node, what the SOLVER value at that node is, and
why the rect emits 17.9 vs the apron's 16.6. Reusable probe template:
`/tmp/diag_term_apron.py` (node-sharing + per-vertex emit dump) and
`/tmp/grade_detail.py` (per-tile build + `check_grade.run_checks`, cross/within
by role-pair).

Then make the rect's emitted corner value agree with the shared-node solved
value — either emit rects with per-node altitudes at shared corners, or make the
hi/lo collapse honour the exact solved node elevation at every shared vertex.

After that: CYXY apron/junction INTERNAL over-grade (apron `-10078` spans
703.8→701.8 over its own width; the giant east apron grades flat far from its
edge) — needs apron decomposition, a separate piece.

## Still deferred (from session 51, re-confirm after sloped rects)
- **Phase-1 "no averaging"** at the first leaf (`_project_shape` else-branch
  splits an over-cap edge 50/50). For a rect this only averages the cap-0 CROSS
  edge, which is forced + correct (a taxiway cross-section must be level), so
  it's lower priority than STATUS-51 implied. Revisit if a leaf rect still
  emits flat-at-mean when it should slope along-axis.
- **Seam-priority BFS seeding** (`_runway_node_set` seeds runway only): for
  cross-tile airports (SPLP/MMOX) seam-hard-but-not-runway nodes should ALSO
  seed BFS (`seam ∈ base_hard AND ∉ runway_nodes`). SPJC's seam doesn't cross
  runway, so runway stays top priority there.

## Current test failures: NONE — FULL suite green
`venv/bin/python -m pytest tests/ -q -n auto` → **284 passed / 2 skipped / 0
failed** (~1:45), compare_target INCLUDED. The 2 skips are env-gated
(`test_elevation_terrain_following` needs O4_TEST_TILE; `test_boundary` CYXY
ribbon-share). First fully-green full suite this session.
Build per-tile with smoothed DEM to reproduce grade numbers; whole-airport build
mis-samples the seam — use the grade test or `/tmp/grade_detail.py`.

## User algorithm spec (verbatim, 2026-05-28) — keep within reach
> Terminals must be flat, and aprons must conform to their vertices. The
> terminal should be the starting point of our solver pass which should be
> grading aprons FROM the terminal inward to the runway, then the reverse pass
> comes back enforcing grade from runway to terminal and adjusts the whole
> terminal if needed.

> The first pass working from leaves towards the runway should continue
> following DEM and clamping to grade right up to the runway … push the whole
> violation into that last connection. Then the reverse pass pulls the runway
> junction just the minimum required to be within grade, and works it's way
> back out the leaves … and finally at the very end leaves we force them into
> grade compliance.

> If a tile seam crosses airport pavement union, then it's the highest
> priority, and runway thresholds become second.

## Build & test workflow (unchanged)
- Repo `/Users/noah/Ortho4XP-novemberlima`. Venv `venv/`. **No system Python.**
- Single airport: `from auto_patch.pipeline import build_airport_pavement;
  build_airport_pavement("CYXY", xplane_root(), compute_elevations=True)`
  (needs `src/`, repo root, `tests/` on `sys.path`; `from conftest import
  xplane_root`). Build ≈ 15 s.
- Full suite: `venv/bin/python -m pytest tests/ -q -k "not compare_target" -n auto`
  (~2:18). Skip compare_target during dev; re-cut only when otherwise green
  (`tools/build_target_osm.py`).
- Grade audit on an emitted patch: `tools/check_grade.py`. The grade test builds
  PER-TILE with smoothed DEM (production-like), not the whole-airport build.

## Gotchas (unchanged but bite every session)
- **Ortho4XP caches `auto_patch` imports** — full quit+relaunch after edits.
- **Import cycle:** `junction_repair` ↔ `elevation` — go through
  `auto_patch.pipeline`, never import `junction_repair` first.
- **Bash CWD persists** — run probes from repo root, not `src/auto_patch`.
- **Temp/debug scripts + generated OSMs go in `/tmp`**, never the repo.
- **DEM smoothing:** production gets Ortho4XP's `apt_smoothing_pix=8`-smoothed
  `tile.dem` via `override_dem`; the standalone path replicates it. Raw
  `tile_dem=DEM(...)` probes use UNsmoothed elevations (geometry is
  DEM-independent; altitudes differ from production).
- **`git stash` bit me this session:** an interrupted `git stash && … ; git
  stash pop` left the pop un-run, silently reverting an edit. Prefer an env-gated
  toggle or a scratch copy over stash for baseline A/B comparisons.
