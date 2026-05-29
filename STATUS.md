# Auto-Patch Status — session 54 HANDOVER (runway-flex third pass: all 3 grade tests now PASS; suite 5→2; only 2 pre-existing GEOMETRY tests left)

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

## Current test failures (2, both PRE-EXISTING GEOMETRY — NEXT SESSION)
```
FAILED tests/test_pavement_geometry.py::test_rect_short_edges_connect[SPJC]
FAILED tests/test_junction_rules.py::test_junction_runway_node_sharing[CYXY]
```
Both were failing at session-54 start (geometry invariants, NOT grade). All three
`test_pavement_grade` tests now PASS (session 54). These two are the next target —
neither is elevation/grade related:
- `rect_short_edges_connect[SPJC]` — a taxi rect's short edges not connecting cleanly.
- `runway_node_sharing[CYXY]` — junction vs runway shared-node mismatch.
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
