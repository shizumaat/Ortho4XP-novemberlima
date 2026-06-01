# Auto-Patch Status — session 58 CLOSE → session 59 = DEBUG REMAINING HECA ISSUES

## ★★ SESSION 59 TASK: fix the remaining HECA patch issues ★★

The build now SELF-VERIFIES and lists HECA's exact problems with shapeIDs +
taxiway names + lat/lon. Your job: work that list down to zero. Run:

```
venv/bin/python -c "
import sys; sys.path.insert(0,'src'); sys.path.insert(0,'.'); sys.path.insert(0,'tests')
from conftest import cached_airport_layout
from auto_patch.verification import verify_and_log
verify_and_log(cached_airport_layout('HECA'),'HECA')" 2>&1 | grep '\[verify\]'
```
(~55s build, cached after the first call.) Or the pytest gate on HECA:
`O4_TEST_AIRPORTS=HECA venv/bin/python -m pytest tests/test_pavement_geometry.py
tests/test_pavement_grade.py -o addopts="" -q`. HECA grade is now COVERED (no
longer a gap). The shipped patch dump is `/tmp/HECA_grade.osm` (probe
`/tmp/probes/heca_grade.py`). Anchor (30.10895832, 31.43477812). shapeID == the
`shapeID` tag in the patch OSM == index in `layout.shapes`.

### THE CURRENT HECA ISSUE LIST (2026-05-31; total: overlap 4 / off-source 2 /
### flat-edge 1 / short-edge 2 / cross-shape 37 / within-shape 15 / edge-steps 229)

**★ DOMINANT ROOT CAUSE = the terminal-cluster coherent-fill (deferred HECA #2
solver redesign — see session-55 catalogue below + memory).** terminal7 [#6],
terminal10 [#9], terminal6 [#5], terminal2 are flat pads at DIFFERENT levels
sharing corners (3.4–3.8 m apart) → drives most of the 37 cross-shape + 229
edge-steps. The fix is the coherent terminal+apron fill in
`elevation_per_surface/unified_jacobi`: terminal groups must lift as MOVABLE
COUPLED units within their grade band (NOT shape-by-shape — that backfired,
169 viol). Regression-guard: SPJC/SPLP/CYXY grade tests (all green at zero).

1. **OVERLAP (4)** — apron/junction footprint overlaps near the S/T/R/W taxiway
   cluster: 957 m² apron[#309]∩junction[#310] @30.10571,31.40253; 150 m²
   apron[#298]∩apron[#302]; 144 m² apron[#298]∩junction[#300]; 0.1 m²
   groundside∩groundside. Likely junction/apron decomposition over the wide
   blob (see s56 neck-split / apron-reclassify). Pre-existing (the s55 #5
   self-overlap, now LOCATED).
2. **OFF-SOURCE (2)** — apron[#258] 487 m² (8% on source) @30.10723,31.43105;
   apron[#228] 204 m² (8% on source). Aprons emitted where apt.dat/DSF has
   almost no pavement → spurious synthesis OR a non-pavement polygon tagged as
   pavement. NEW finding (the per-shape source-adjacency check located these;
   the old coverage ratio never could). Investigate the apt.dat/DSF near there.
3. **FLAT-EDGE (1)** — apron vertex on stub C[#103] flat (cross) edge
   (t=0.675, d=1.00 m) @30.10482,31.39360. Builder issue (a junction/apron
   vertex on a rect cross-edge interior).
4. **SHORT-EDGE (2)** — stub Exit-2[#94] + Exit-3[#116] end_A connect to nothing
   (rects end mid-air) @ ~30.108,31.433 / 30.110,31.435. Likely a gap in the
   apt.dat taxi network at the runway exits, OR a missing connecting junction.
5. **WITHIN-SHAPE (15)** — steep aprons/stub: apron[#308] 17.5%/3.4 m (tiny neck);
   apron[#296] 13.5%/8.2 m (where J,S,T,W meet); stub S[#126] 6.5%/50 m. Apron
   decomposition (narrow necks) + the coherent-fill issue.
6. **EDGE-STEPS (229)** + **CROSS-SHAPE (37)** — mostly the terminal cluster
   (root cause above).

### Session-58 build-VERIFICATION + diagnostics architecture (use it)
- `auto_patch/verification.py` = the SINGLE home for every invariant check;
  `verify_and_log(layout, icao)` runs them and logs WHAT/WHERE(shapeID+taxiways+
  lat,lon)/CAUSE/FIX. Called per in-tile airport by `driver.generate_auto_patches`
  (production) AND by the pytest gate (tests delegate to the same functions — no
  duplication). Grade reuses `tools/check_grade.py` (the engine).
- Checks shared: grade (cross/within/steps), self-overlap, source-adjacency,
  terminal-flat, vertex-on-sloping-edge, vertex-on-flat-edge, axis-tilt,
  short-edge. NOT yet extracted (pytest-only): junction rules
  (`test_junction_rules.py` / `test_junction_invariants.py`).
- `config.LOG_VERBOSITY` = 0 (Ortho4XP window quiet unless a patch has issues;
  set 2 for debug). ALL per-airport test caps/baselines REMOVED → universal zero
  (they were stale; baselines clean). Commits this session: 024f50b dedup,
  cbcf344 RESA-from-source, c42a340/136bc1b/dcd872c/44bbeea/6c18da6/bcbb9c4/
  7b03bb3/f8e8c90/b965775 verification. Plus 3509845 compare_target re-cut.

### Suite state
Default suite **289 passed / 2 skipped / 0 failed** (SPJC/SPLP/CYXY, compare_target
incl). HECA is NOT in the default suite; its issues above only show via
`O4_TEST_AIRPORTS=HECA` or the build verification. Earlier session-58 work: taxi-rect
dedup keeps R/B sections (commit 024f50b, [[taxi_rect_dedup_sections]]); RESA anchored
on apt.dat geometry (cbcf344, [[heca_resa_nondeterminism]]). Architecture detail:
memory [[build_verification_architecture]].

---

# Auto-Patch Status — session 57 HANDOVER (★ NEW DIRECTION: line-marking centerlines → drop curves → rects)

## ★★ SESSION 57 HANDOVER → NEXT AGENT: build the line-marking centerline extractor ★★

### THE ALGORITHM TO IMPLEMENT (user's formula, 2026-05-31 — authoritative)
Replace the *synthesis* of curves (the `r/tan(α/2)` fillet approach — abandoned,
see below) with **reading the real curves out of the apt.dat line markings**:

1. **Taxi route network → intersections.** A route node with ≥2 distinct taxi
   names OR degree ≥3 is an INTERSECTION; that point is ALWAYS inside a junction.
2. **Line markings → the real curved centerlines.** apt.dat **row-120** linear
   features, type **code 60** ("Single Taxi Wide" = taxiway centerline), carry
   the actual geometry INCLUDING beziers (the curves). Filter: code 60 (HECA);
   cross-airport, confirm centerline by ALIGNMENT to the route net (median dist
   <~6m + parallel). Edges = "Double Solid" (code 53, offset ~21m); holds/broken
   are codes 52/61/62 — all separable.
3. **Identify the junction's curves.** Trace the route OUT from each intersection
   point; the junction is bounded by the **nodes where the bezier curves begin**
   (where a centerline marking leaves the straight route). JUNCTIONS ARE
   DIFFERENT SIZES — do NOT use a fixed radius; the curve-start nodes define the
   extent.
4. **Drop the straight chords BETWEEN curve ends** (the straight bits *inside* the
   junction — these wrongly survive an alignment-only filter; see #3 example).
5. **Drop ALL the bezier curves** (the curved marking segments — a segment is a
   curve iff an endpoint is a 112/114/116 bezier node; this is the reliable,
   heuristic-free curve test the user landed on: "can't you just drop all the
   bezier curves?").
6. **Remainder = the straight centerlines → feed the rect builder.**

### What's built (probes in /tmp/probes/, NOT committed — exploratory)
- `linemark_extract.py` — parses row-120 features (samples beziers via the
  X-Plane mirror convention), filters to code-60 centerlines, route-cross-ref
  align+parallel, drops curves, writes `/tmp/HECA_linemark_straights.osm`.
  STATE: code-60 filter is clean (938 centerlines vs 106 edge/hold/broken).
  Curve-dropping via per-segment alignment got #1 (keep stub middle ✓) and #2
  (drop curve ✓) right but #3 WRONG (a straight chord that is on+parallel to a
  junction-internal route edge survives — needs step-4: drop straights between
  curve ends). **The clean fix per the user = step 5 (drop bezier segments
  directly) + step 4 (drop straight chords between curve ends near an
  intersection), NOT the alignment/deflection heuristics.**
- HECA apt.dat: `/Users/noah/X-Plane 12/Custom Scenery/HECA Cairo/Earth nav
  data/apt.dat`. Row counts: 1044 row-120 features, beziers present (889×112,
  272×116). The reader does NOT parse row-120 line markings yet — add it.
- `/tmp/HECA_target_centerlines.osm` = 125 hand-tuned target spines (validation).
- Example coords (lat,lon): #1 keep stub-middle 30.1199249,31.4477182 →
  30.1197105,31.4479466 ; #2 curve-to-drop 30.1201791,31.4467661 ; #3
  junction-internal straight to drop 30.1190203,31.3825056 → 30.1194462,31.3830776.

### REJECTED this session — the curve SYNTHESIS approach (don't revive)
Spent a long arc trying to SYNTHESISE curves from the straight route network
(`/tmp/probes/curve_mask.py`, `junction_bounds.py`): clip each edge back by the
fillet tangent `t=r/tan(wedge/2)`, r=50 (Code E, ICAO Doc 9157 curve radius),
high-speed exits R_HS≈370. It half-worked (T2 calibrated to the target) but was
fundamentally fragile: acute-gore vs real-turn ambiguity, runway-exit transitions
under/over-masked, multi-vertex curve `arc[j]` bugs, and NO single local signal
(width / through-angle / convergence) separates off-target from real taxiways
because HECA pavement is a merged blob and apt.dat over-labels. The line-marking
data has the REAL curves — use them instead.

### Committed this session (suite GREEN 285/0/2 except compare_target — see below)
- `d46034e` cap cross-connector end margin 50m (HECA L coverage 71→99.7%).
- `02677a3` keep through-taxiways whole in the corridor trim (R 70→99%).
- `c51fa12` off-corridor drop (`_drop_offcorridor_centerlines`: runway-crossing
  >5m + junction-buried median-nearer-edge-halfwidth ≥50m) + bend-hook trim
  (`_trim_short_bend_hooks`) + runway-centering margins (perp 30→25, diag 15→25,
  junction 15→20; tunable module globals `_RWY_JUNCTION_BUFFER_M` etc. in
  pipeline.py, `_CHART_JUNCTION_MARGIN_M`/`_BEND_ENDPOINT_MARGIN_M` in
  centerlines.py).
- `93eab1d` bend trim: split at the corner into two straights, never keep a bent
  piece (drop short hook <45%, else keep both straights).
- HECA named-only centerline coverage 95.5 → 98.4%. These are CENTERLINE-quality
  improvements to the EXISTING route-network path; the line-marking approach
  above will likely SUPERSEDE much of the off-corridor/bend-hook logic once it
  lands — keep them until the new extractor proves out.

### ⚠️ STILL OPEN / GOTCHAS
- **compare_target fixtures (SPJC + SPLP×2) need RE-CUTTING** — the centerline
  geometry shifted this session; they're the only suite failures. User approved
  the re-cut (dropping short rects like SPJC R1/R2 is OK). Use `/tmp/recut.py` /
  `tools/build_target_osm.py`, refresh floors = target−round(5%).
- **CONCURRENT AGENT** owns the uncommitted `unified_jacobi.py` WIP (HECA #2
  grade) + committed `803761b` (runway geometry). LEAVE unified_jacobi.py ALONE;
  `git add` explicit paths only.
- Named-only is the agreed HECA metric (exclude TX/discovered + refless both
  sides). Probe anchor (30.10895832, 31.43477812). HECA build ≈55s.

---

# Auto-Patch Status — session 56 HANDOVER (★ default suite GREEN: 281 passed / 0 failed / 2 skipped, compare_target INCLUDED)

## ★★ SESSION 56 HANDOVER → NEXT AGENT: collinear-fragment MERGE (the remaining rect-quality lever) ★★

### Where we are
Session 56 was a taxi-rect QUALITY pass driven by a hand-verified target.
Guiding model (user): **a taxi rect = its centerline segment, widened** — so
align the centerline segmentation+extent and the rects follow. Junctions only
at real curves/intersections. Rules must be GENERAL (geometric, all airports),
not HECA-tuned.

Committed this session (all suite-green, no regressions):
- `e9889a4` source_axis-aware sloping-edge split (stop two-parallel-lane rects)
  + `56fc95e` no-overlap guards on junction-vertex-moving passes (replaced a
  bad flat-end trim that broke junctions). [memory/two_parallel_rects_rotated_ring.md]
- `f205856` **width-aware endpoint trim** (`_trim_axis_to_narrow_corridor` in
  pavement/rects.py): trims a rect's axis back where pavement widens past
  1.3×strip half-width → rect ends at the junction mouth. CAP GOTCHA: the
  half-width probe caps at 40m and saturates at wide airports; the trim
  re-measures the strip with a HIGH cap (120m).
- `077b17a` apron-blob rejection: drop a rect whose mean width > 1.7×(2*narrow_hw)
  AND > 50m (corner-snap inflated it into apron — HECA U1 570×162).
- `674d5e8` corridor trim keeps through-taxi crossings (was discarding R's
  670m middle section).
Cumulative HECA vs target: rect IoU 0.704→0.749, matched 98→108/125, ~half
the over-length gap closed. FULL DETAIL: **memory/rect_centerline_quality.md**.

### THE NEXT PHASE (your task): collinear-fragment MERGE, replacing the stub-dedup
**Goal:** close the remaining gap. Current centerline-level score (rect axes vs
target): **153 axes / 29,730 m vs target 125 / 25,375 m** — over-segmented
(+28) and ~17% too long overall (T/S/G over-fragmented), while R/B are *under*-
built. One coherent lever fixes BOTH directions.

**Diagnosis (verified, do not re-litigate):**
- apt.dat is FINE — R = 1918 m (7 edges), B = 883 m (5 edges), full length.
  `apt_dat_reader.taxi_centerlines` preserves them.
- The builder `_build_taxi_rects` EMITS all of R's segments (instrumented: 5
  "R EMITTED" stubs). They are then COLLAPSED to 1 by the **stub-ref dedup**
  post-pass: `src/auto_patch/pavement/rects.py` ~lines 351-457
  (`_should_dedup` + the overlap/proximity cluster dedup + the "diagonal-parent
  → exactly one rect" rule). That logic keeps only the LONGEST fragment per
  ref, assuming letter-only stubs are single short stubs (correct for SPJC
  B/C/E/G ≤580 m; WRONG for a long 2-section taxiway like R).
- R's true shape (user): TWO straight sections + a bend. Its fragments are
  section-1 (db≈30° to runway: 447+194 m) and section-2 (db≈89°: 271+129+94 m).
  Correct output ≈ 2 rects (one per section), target has 3.

**The fix to build:** replace the keep-longest stub-dedup with a **MERGE of
adjacent COLLINEAR same-ref rect axes** into one rect:
- MERGE when fragments are same-ref + roughly collinear (bearing within ~15°)
  + adjacent/end-to-end (touching, ~0 area overlap). → R section-1's 447+194
  merge to ~641 m; section-2's 271+129+94 merge to ~494 m → R = 2 rects.
- Do NOT merge across a real bend (different bearing → R's 2 sections stay
  separate).
- STILL drop genuine DUPLICATES (same footprint, real area overlap — SPJC's
  fragmented OSM ways / V2's 3 pieces). Keep that behaviour; only stop
  collapsing distinct collinear-adjacent SECTIONS.
- This same merge also fixes the over-fragmentation (T 14→11, S 7→4, G 9→7).

### How to measure (scoring harness — reuse it, don't rebuild it)
- `/tmp/HECA_target_centerlines.osm` — 125 user-verified target spines (one per
  intended rect). `/tmp/HECA_initial_rects_target.osm` — the 125 target rects.
- `venv/bin/python /tmp/probes/clscore.py` — builds HECA, captures rect AXES
  (entry[1] of `_build_taxi_rects` output), reports per-ref count+length vs
  target + TOTAL. PRIMARY metric (1-D, robust).
- `/tmp/probes/score.py` — rect IoU vs target (NOISY: target hand-drawn ~approx
  coords, ~0.7 even when right; secondary trend only).
- Anchor for /tmp probes: HECA = (30.10895832, 31.43477812).
- Score per-ref after EACH change; converge cur→target.

### Risks / guardrails
- **Do NOT regress SPJC's short stubs** (B/C/E/G, V2) — they rely on the dedup.
  Run the full suite + `O4_TEST_AIRPORTS=SPJC,SPLP,CYXY` checks at each step.
- Broad geometry shift → **re-cut compare_target fixtures + refresh floors**
  when done. Workflow: `venv/bin/python /tmp/recut.py` (re-cuts SPJC + both SPLP
  tiles, prints floors = count−round(5%)); update `tests/test_compare_target.py`
  baselines + totals. (Done twice this session — see git log.)
- The `_rect_long_edges_at_pavement_boundary` both-embedded gate (rects.py ~479)
  currently LIMITS over-fragmentation; the merge interacts with it — measure.
- Earlier REJECTED dead-end (don't repeat): tightening `split_merged_centerline`
  bend-split (more splitting) REGRESSED — target wants FEWER rects, not more.
  The lever is MERGE (post-build), not more centerline bend-splitting.

### ⚠️ Pre-existing WIP in the tree — LEAVE IT ALONE
`src/auto_patch/elevation_per_surface/unified_jacobi.py` has ~29 lines of
UNCOMMITTED WIP (a `_directional_relief` terminal grade-band "FILL terminals up
to their grade-feasible band" block) belonging to ANOTHER agent's HECA #2
grade work. Do NOT stage/commit it. **Avoid `git stash`** with it in the tree
(it was accidentally reverted+reconstructed once this session — see
memory/two_parallel_rects_rotated_ring.md incident note). Use
`git add <explicit paths>` only.

### Key file map
- `pavement/rects.py`: `_build_taxi_rects` (gates + the stub-dedup at ~351-457),
  `_trim_axis_to_narrow_corridor`, `_natural_half_width`, apron-blob gate.
- `pavement/centerlines.py`: `split_merged_centerline` (bend-split),
  `_split_centerlines_at_points` (intersection split + margins).
- `pipeline.py` ~1846: diagonal-stub corridor trim (just fixed). ~1930: split call.
- `apt_dat_reader.py:1209` `taxi_centerlines` (source — confirmed clean).

---

# Auto-Patch Status — session 55 HANDOVER (★ default suite GREEN: 281 passed / 0 failed / 2 skipped, compare_target INCLUDED)

## Session 55 CLOSE (2026-05-29)
Two threads ran: (A) build/test PERF + (B) HECA correctness. Net:
- **HECA invariant failures 9 → 2** (`O4_TEST_AIRPORTS=HECA`): fixed coverage,
  terminal flatness, 170 orphans, rect-short-edge TX52, retired Rule-2
  proximity, vertex-on-sloping-edge snap, neighbour-corner insert. The
  junction-connectivity cluster (#3) is fully closed.
- **Remaining HECA: #2 within-shape grade (~79, DIAGNOSED — needs a solver
  redesign, SPAWNED as a separate task) and #5 self-overlap (3 pairs).**
- **Perf:** build 70.7→54.6 s; suite builds 14→8; default suite ~104→~73 s.
- Default-suite count dropped 284→281 only because the retired Rule-2 test
  had 3 parametrizations (SPJC/SPLP/CYXY); nothing regressed.
- ⚠️ ANOTHER AGENT has uncommitted WIP in `junction_repair.py`
  (`_orient_rect_sloping_edge_first`). LEAVE IT ALONE.
- The #2 solver work is handed to a fresh session (see the "PLAN for the
  solver session" under HECA failure #2 below). Do NOT rush it into a
  mixed session — the naive terminal-lift backfired (169 viol); it needs a
  coherent-fill redesign with the grade tests as the regression guard.

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
Started at 9 failures; **2 remain**. The junction-connectivity cluster
(#3) is FULLY resolved: coverage #1; terminal #4; 170 orphans (DSF
boundary as source); Rule-2 proximity retired; rect_short_edges TX52
(pavement-tip exemption); vertex-on-sloping-edge (post-conformance
near-corner snap onto rect corners); neighbour_corners (post-conformance
insert of unshared neighbour corners into junction edges). Remaining:
within-junction grade (#2, ~79 — DIAGNOSED; needs a coherent-fill solver
redesign, deferred to a spawned solver session — see #2 below), self-
overlap (#5, 3 pairs 1.9 m²). The original HEAZ-over-collection
X-Plane crash appears RESOLVED by the committed boundary gate (build
reports 0 off-airport / 0 overlay dropped). HECA failures (tasks 2-5):
1. **Coverage (#1) — FIXED (0ffb8f6):** `test_coverage_within_source_envelope`
   measured emitted vs apt.dat+runways ONLY, omitting DSF pavement (a
   first-class source). HECA emitted +54.9% vs apt-only but only +7.4% vs
   apt+DSF, 0.3% outside boundary = legitimate DSF, not over-collection.
   `_source_pavement_union` now adds boundary-clipped DSF. Adding source
   area only lowers overage → no airport can newly fail.
2. **Within-shape grade — ~79 viol — DIAGNOSED, NOT FIXED (s55, deferred to
   a solver session — see spawned task).** NOT apron decomposition, NOT
   neighbour-holding. Full diagnosis 2026-05-29:
   - Cap = ≤1.5% between ANY two vertices of a junction/apron (all-pair
     Euclidean). `_PER_AXIS_JUNCTIONS=False` so aprons use pure Euclidean
     (matches the test); junctions get arc-length relaxation but the worst
     pairs are too short for that to matter — so the violations are genuine.
   - SPLIT (83 pairs / 12 shapes): **31 "below-floor"** (node seeded at the
     too-low DEM, below its grade-feasible band — FILL fixes) + **52
     "infeasible" (band lo>hi)**, mostly the ~1 km² apron. ALL infeasibility
     gaps are SMALL (≤2.60 m; many exactly 2.60 m).
   - ROOT: `_grade_bands` is seeded ONLY from the 15 CIFP runway THRESHOLDS
     (the 171 interior runway nodes are NOT hard). HECA's thresholds span
     58-142 m, so a node squeezed between a CLOSE high threshold (e.g.
     136.5 m, ~200 m away → forces ≥133.5) and a FAR low one (60.7 m,
     ~4700 m → ≤131.0) gets an infeasible band — the two extreme runways are
     ~1.55% apart over their connecting pavement path (~2.6 m over 1.5%).
   - USER FRAMING (authoritative, 2026-05-29): the DEM is the LEAST-accurate
     input (low-res + smoothed); CIFP thresholds are CORRECT; real taxiways
     follow grade; fill/cut are normal. So a ≤1.5% surface ALWAYS exists and
     "infeasible" just means the DEM is wrong there. The band's all-`lo`
     assignment IS grade-compliant (triangle ineq) — so the fix is to FILL
     toward the band, treating DEM as a within-band preference only.
   - ATTEMPT THAT BACKFIRED (reverted): lifting each terminal group to its
     band floor in isolation → 169 viol, worst 62.2%. Lifting a terminal's
     shared edge ~7 m while its far edge stays at terrain makes a cliff
     INSIDE the shape. LESSON: **fill must be COHERENT across the whole
     connected sub-network** (terminal + abutting aprons + connecting
     taxiways lift together), not shape-by-shape.
   - PLAN for the solver session: a coherent global fill in the final
     difference-constraint pass (`unified_jacobi._project_within_bands` /
     `_directional_relief`) — make terminal groups MOVABLE coupled units
     within the band-projection and alternate cap-projection ∩ band-clamp
     over ALL soft nodes (incl. terminal units), so below-floor nodes lift
     to their floor and infeasible nodes resolve to midpoint, with the whole
     region moving together. Regression-guard: SPJC/SPLP/CYXY grade tests.
   ⚠️ An OTHER AGENT has uncommitted WIP in `junction_repair.py`
   (`_orient_rect_sloping_edge_first` — fixes a rotated-rect mis-split,
   HECA taxiway A #446/447). LEAVE IT ALONE; coordinate before touching
   `_split_sloped_rects_at_violations`.
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
   - ✓ Rule-2 proximity (6) — TEST RETIRED (32a7718, user design call
     2026-05-29). `test_junction_no_long_edge_proximity` flagged junction
     vertices within 20m PERPENDICULAR of a sloping rect edge — a proximity
     proxy. The REAL invariant is "a node ON the sloping edge breaks the
     rect; proximity is fine as long as only CORNER nodes are shared," which
     is tested directly by `test_no_vertex_on_sloping_rect_edge` (geometry)
     + grade/step (elevation). HECA 213/250/357 were thin connectors
     (5.8-9.4m wide; 213 is a SERVICE ROAD, not a taxiway) running 2-14m
     alongside a stub = false positives. Builder snap + SLOPING_EDGE_SNAP_M
     kept. NOTE: general rule — pavement is a taxiway only with a centerline,
     else undesignated.
   - ✓ vertex-on-sloping-edge (2) — FIXED (3046d8e). ROOT CAUSE: a junction
     vertex left ~0.5m off a sloped rect corner, ON the edge interior
     (un-splittable near-corner; nudged there by weld/conformance AFTER the
     split passes). NEW pass `_snap_near_corner_vertices_to_rect_corners`
     runs LAST (post-conformance, on emitted geometry): snaps any non-rect
     vertex on a sloped 4-corner rect's edge within 1.5m of a corner ONTO
     that corner (all 4 edges). General — prevents at all airports.
   - ✓ neighbour_corners (1) — FIXED (ffd18c4). stub J3's corner sat on
     junction #369's edge 0.52m from vertex v4; conformance's endpoint
     guard (0.5m along-edge) skipped it (t*L≈0.4999) though it's >0.10m
     (test tol) from v4. NEW post-conformance pass
     `_share_neighbour_corners_into_junctions` INSERTS an unshared
     neighbour corner on a junction edge into that junction (test's
     tolerances; junction-scoped; INSERT not snap → +0.49m² vs -20m²).
     CLUSTER #3 NOW FULLY RESOLVED.
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
