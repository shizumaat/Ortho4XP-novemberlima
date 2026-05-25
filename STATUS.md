# Auto-Patch Status — session 49 END: directional shape-cascade relief; CYXY terrace = holdout

## TL;DR / current state (session 49 end)
**Read `docs/elevation_solver.md` FIRST** — THE core component (elevation
solver/relief), the user's model, every approach tried+rejected.

**Relief is now a DIRECTIONAL SHAPE-CASCADE** (commit `81c76a0`,
`unified_jacobi._directional_relief`) — it REPLACES the session-48 symmetric
stiffness cap-projection (now superseded; the `_RELIEF_APRON_STIFFNESS`/
stiffness machinery is gone).
- **Phase 1** = the inverted cascade (terminal→apron→taxi→runway, DEM-
  following, cap to grade) — the warm start.
- **Phase 2** = propagate compliance OUTWARD from the runway/seam HARD
  anchors, building ON phase 1 (**NO reseed** — never throw the DEM-following
  surface away). Shapes are processed by network distance from the runway;
  each is solved as a UNIT (`_project_shape`) holding the vertices already set
  by inward shapes, so a violation is pushed OUT to the free terminal/apron
  end. Terminals translate as a rigid flat plane; aprons/junctions flex as
  compliant all-pair surfaces (slope-to-cap-then-drag), never sheared.

**Why the rewrite:** the symmetric relief reseeded to DEM and over-dropped
compliant nodes to the LOWEST feasible surface — at SPLP it dropped junction
21 to 70.7 (~1 m below its feasible band [71.65, 71.97], verified by Dijkstra
band check), breaking stub A that phase 1 had already solved (0.43%). The
directional cascade keeps phase 1's surface and only pushes residual outward.

**Result:**
- **SPLP — SOLVED.** Tile −13/−77 within-shape = 0 (stub A relieved with NO
  runway move — it was never infeasible, just over-dropped by the old relief).
  Tile −13/−78: 16.3% primary-A sliver GONE (one marginal 1.9% apron + three
  cross-shape 0.2 m shared-vertex hits remain — the latter a separate
  emit-rounding issue, not the relief).
- **CYXY — KNOWN REGRESSION (within-shape ~77), the holdout.** The excavated-
  terrace cluster (big "leaf" apron #48 + sub-leaf junctions/aprons
  #49/#51/#50, the 705–715 m hillside) SHEARS — the cascade's strict
  inward-held ordering can't reconcile a densely-interconnected non-convex
  2-D cluster the way the old symmetric relief (global reconciliation) did.
  apron #51 sheared 119% (two ADJACENT verts 4.6 m apart, chord INSIDE — a
  real cliff, not a phantom). Measured facts: apron #48 convexity **0.32**
  (super-convoluted); its taxi connections are TIGHT (702.4–704.4, worst
  1.67%) — so CYXY is a flatten/ordering problem, **NOT** global infeasibility.

**STEP 3 (runway-threshold yield) is PARKED** (commit `849265e`,
`runway_redistribute.relieve_grade_via_runway_thresholds`,
`ENABLE_RUNWAY_THRESHOLD_RELIEF=False`): the last-resort for a GENUINE
multi-runway infeasibility (SPJC competing-terminal). Functional but
UNVALIDATED on that target, and it can't tell a fixable residual from an
unfixable one — so it's off by default. Enable + validate when a real
multi-runway squeeze is hit (NOT needed for SPLP/CYXY).

## PLAN for next session — fix CYXY's terrace (the holdout)
1. **Cut non-convex transition aprons** (the non-convex all-pair problem).
   apron #48 is a BLOB (convexity 0.32). The directional cascade works on
   CHAINS (SPLP ✓) and chokes on BLOBS. Detect big LOW-CONVEXITY shapes that
   also span grade, and CUT them PERPENDICULAR to their taxi routes into
   CONVEX pieces — turning the terrace blob into a chain the cascade grades
   smoothly (each piece compliant, joined at shared edges, grade following the
   real pavement path, not Euclidean chords through non-pavement). CAVEAT:
   apron *grid* subdivision was tried+rejected (`docs/elevation_solver.md`) —
   pieces shared nodes (still one network) + added slivers; the new cut must
   yield genuinely convex, non-sliver pieces.
2. **Force-hierarchy "leaf network"** for the cascade's parent/held logic:
   trunk (runway) → branches (taxiways, largest→smallest) → leaves (aprons
   touching a taxiway) → sub-leaves (aprons touching only aprons). Give each
   shape ONE parent (highest-priority inward neighbour); a sub-leaf is held
   only by its parent, never by a same/lower-tier sibling — so it can drag its
   siblings/children down instead of being clamped at incompatible levels (the
   #51 119% shear was being held at both a #48-side 707 and a #50-side 713).
3. Re-check SPJC; if a genuine multi-runway/terminal squeeze remains, enable +
   validate STEP 3.

## Session-49 commits on `dev`
- `fb0cf43` clearance edge-interp (fix phantom apron-edge bumps — was
  nearest-node Voronoi sampling; now matches Triangle4XP linear render).
- `c4aa796` relief step-2 framing (terminals rigid, aprons/taxi flex).
- `81c76a0` directional shape-cascade relief (replaces symmetric).
- `849265e` parked step-3 runway-threshold relief (gated off).
- (session-48 base) `6c6726e`/`5cb2e0a`/`60db68d`/`1f2ba5a`/`8f2b0e9` —
  module/stands cleanup, docs, and the stiffness relief (now SUPERSEDED).

**Suite (excl. compare_target):** **UNMEASURED after the directional cascade
(`81c76a0`)** — committed as a checkpoint without a full re-run.  Known from
per-tile probes: **CYXY grade REGRESSED** (within-shape ~77 — the terrace
shear above; was within=0 / step-cap-only under the prior symmetric relief),
**SPLP grade improved** (−13/−77 within=0).  SPJC unmeasured.  **First action
next session: run `venv/bin/python -m pytest tests/ -q -k "not compare_target"`
(~20 min)** to get the true count before touching anything.  Pre-directional
baseline (`c4aa796`) was **6 failed / 279 passed / 5 skipped** = junction
`have_source`[SPJC]+[CYXY] + `outside_pavement`[CYXY] (the last is FLAKY) +
`no_vertex_on_sloping_rect_flat_edge`[SPJC] + grade ×3 (CYXY/SPLP/SPJC); the
session-48 "5"-fail count UNDERCOUNTED the two CYXY junction-invariant fails
(pre-existing / flaky).

**Session-47 model REVERTED (user 2026-05-24):** `ABSORB_RECTS_ALONGSIDE_APRONS
=True` — the no-absorption + apron-lane-chain model is gone; taxilanes through
aprons dissolve into the apron again (no tilting fixed-width ribbon chains).
CYXY now emits **0 apron-lane shapes**.

**Removed dead/abandoned code (this session):**
- Solver experiments: `pavement/apron_subdivide.py` (150 m grid — didn't fix
  the over-drop, pieces share nodes; added slivers), `_ceiling_spread` +
  `floor_tol`, the `largest_rwy_bearing` block.
- Old superseded modules + their tests: `pavement/{taxiway_rects,classifier,
  taxiway_decompose}.py` (replaced by `rects.py`) + `apron_split.py`.
- **Aircraft stands** end-to-end: `ENABLE_AIRCRAFT_STANDS`, `STAND_MAX_GRADE`,
  `ROLE_STAND`, `apt_stand_zones`, the `RampStart` apt.dat parser (+ tests),
  `AIRCRAFT_LENGTH_BY_CODE_LETTER`.  (`WINGSPAN_BY_CODE_LETTER` kept — wingtip
  clearance.)
- **Service roads** (`ENABLE_SERVICE_ROADS=False`): code kept, but the OSM
  small-roads lookup (already gated) + apt.dat 1206 centerline parse are now
  both skipped while disabled — no wasted load cycles.
See `docs/elevation_solver.md` for why the solver experiments were rejected.

**Remaining flags:** `ABSORB_RECTS_ALONGSIDE_APRONS=True` (reverted),
`ENABLE_SERVICE_ROADS=False` (gated, code kept), `_PER_AXIS_JUNCTIONS=False`.
The session-47 details below are history (its apron-lane / stands experiments
have been reverted/removed per the above).

## DONE this session (committed on `dev`)
1. **`98f8ad0`** — (a) **same-pack DSF**: read DSF only from the chosen
   apt.dat's pack, so the stock Global Airports DSF no longer re-imports
   shapes a custom pack removed (CYXY wide A2); (b) **clearance shadows
   the FINAL profile**: emit clearance AFTER the last per-surface solve +
   flat lateral shadow (slope 0) — fixed CYXY taxiway-E trench; (c) **apron
   absorb**: assign each absorbed rect strip to ONE junction, not every
   bordering one — fixed the 3344 m² SPJC apron/apron overlap.
2. **`1d85d86`** — `PATCH_SLOPE_PROFILE = "plane"` (was spline). **Fixed
   the taxiway-A2 dip**: the mesh rendered the spline curve while the
   clearance samples the surface LINEARLY, so they diverged; plane =
   constant grade = matches. Multi-airport DEM fit confirmed spline is
   never meaningfully the best fit.
3. **`fbeaa05`** — `split_long_rects_along_terrain`: split taxi rects
   >200 m at interior DEM extrema so the solver places control points
   where terrain curves and each piece follows it (seam between plane
   pieces is a <3% grade-capped fold — invisible). CYXY +1, SPJC +2,
   HECA +13.
4. **`6a7bb3a`** — **prong #2 grade-relief: terminal-free aprons yield.**
   The inverted cascade froze every apron before solving taxi, so a
   taxiway bridging a low runway and a high terminal-free apron over-
   graded with no recourse. New relief phase in `unified_jacobi.solve`:
   after the cascade, terminal-free apron nodes go SOFT with the taxi
   network (terminals / terminal-anchored aprons / runway-seam stay
   HARD); cap projection lets the apron yield within its all-pair grade
   (no steep-middle apron). Grade-driven + unbounded (DEM unreliable at
   excavated terraces — confirmed by ground truth: real terrace 705 m
   while DEM reads it via the cut-face). **CYXY taxiway E#2 3.92% → 1.48%
   (compliant), E#1 → 0.57%.** Suite unchanged. `_runway_nodes` + the
   `node_bounds` clamp are PARKED in the file for the last-resort
   runway-yield (prong #1), currently unused.

## Grade-relief 3-prong plan (user 2026-05-23) — status
The CYXY E grade was a real infeasibility: ~20 m rise from the runway
valley (693) to the upper hillside aprons (705–715) over short taxiways.
Fix = spread the relief across three prongs so no one surface absorbs it:
- **#2 terminal-free apron yield — DONE (`6a7bb3a`)**, fixes E#2.
- **#1 runway-threshold yield — PARKED, last resort.** Only engage when
  the taxi network can't meet grade after #2/#3 (NOT always-on — perturbing
  published runways is a last resort). Helper + node_bounds clamp already
  in `unified_jacobi`.
- **#3 width-dependent apron grade — ATTEMPTED + REVERTED.** Model (user):
  grade stiffness ∝ local width — a WIDE area is all-pair (flat, free
  maneuvering, can't terrace); a NARROW arm (access road / taxiway neck)
  flexes ALONG its axis like a taxiway. Correct model, but the
  implementation (morphological-opening classification, `buffer(-W/2).
  buffer(+W/2)` + contains) **misclassified wide-apron PERIMETER vertices
  as narrow** → solver ramped wide aprons → 1608 grade violations, 14.7%
  apron steps. **Fix for next time:** classify by distance to the eroded
  core — `wide[v] = poly.buffer(-W/2).distance(v) <= W/2 + tol` (a wide
  perimeter vertex is ~W/2 from the eroded core; a narrow-arm vertex is
  far). Needs the SAME gate in `tools/check_grade` (else it flags what the
  solver builds). SEPARATE blocker: CYXY's access road is swallowed into
  the giant apron #41 blob, so it isn't a thin arm in our geometry — the
  apron needs de-blobbing for the access-road case to show.

## APRON GRADE STANDARDS — RESEARCHED (session 47, 2026-05-24) ✓
Verbatim-sourced from FAA AC 150/5300-13B §5.9, EASA CS ADR-DSN.E.360, ICAO
Annex 14 §3.13. Full writeup in memory `apron_grade_standards.md`. The standards
distinguish **parking positions (aircraft stands)** from **apron taxilanes** —
our single all-pair `APRON_MAX_GRADE=0.015` conflates them. Answers:
- **All-pair vs local?** BOTH, by FUNCTION. Stand bodies = **all-pair**
  (EASA/ICAO: "max 1% in any direction") — our Euclidean all-pair cap is RIGHT
  there. Apron **taxilanes = directional/along-axis** travel paths (flex like a
  taxiway), NOT all-pair. = the width/torsion model, keyed on function.
- **1.0% vs 1.5%?** Both: **stands/parking = 1.0%** (our 0.015 is TOO LOOSE);
  **heavy apron taxilane = 1.5%** (0.015 correct); light (<30,000 lb) = 2.0%.
- **Curvature?** Yes, simplified: FAA **max grade CHANGE = 2%** (not a runway
  K-curve) + smooth longitudinal changes >1% on taxilanes; 0.5% drainage floor.
  Grade is a LOCAL max that fluctuates — NOT a flat plane end-to-end (user right).

**Reframes prong #3:** key the cap on stand-vs-taxilane function (width as proxy
when apt.dat doesn't say). Wide stand → all-pair 1.0%; narrow taxilane → along-
axis 1.5%; add a ~2% apron grade-change cap (new, currently unmodeled). The prior
#3 classification bug + CYXY access-road-in-blob#41 still apply (memory note).
**Implementation gated on user green-light** (real compare_target/grade impact;
`tools/check_grade` needs the SAME gate).

## OPEN / next
- ~~**Junction clearance consolidation**~~ — DONE (commits `509648d` +
  `f027912`): `clearance._finalize` unions all raw strips, subtracts
  pavement once, and emits ONE `node_altitudes` shape per connected
  region (1:1 shared-vertex adoption across pavement-hole splits). CYXY
  emits 45 clearance shapes; near-pairs are pavement-separated (correct —
  no cut over pavement), not fragments. The old stale OPEN bullet was
  carried forward unstruck from the session-46 handover.
- **compare_target ×3** — re-cut (gated on suite being otherwise green).
- **SPJC cluster ×3** + **grade ×3** — pre-existing; grade mostly data.
- Profile dynamic per-rect FIT: evaluated + DROPPED (marginal vs plane).

## Design facts locked this session
- O4 profile curves all pin endpoints (`plane/spline/tanh(0)=0,(1)=1`) →
  per-rect profile choice never creates a seam; only shapes the interior.
- Grade-capped plane-segment seam crease ≤ ~3% (≈1.7°), <1% typical →
  splitting at extrema is visually safe (KTEX concern resolved).
- Profile = graded DESIGN, NOT the DEM (man-made fill: CYXY 02/20, BGGH;
  ribbon/bridge own the terrain transition).

## GOTCHAS
- **Ortho4XP GUI caches `auto_patch` modules** — after editing source you
  MUST fully quit + relaunch (kill the python procs); rebuilding in the
  same GUI reuses stale modules. The user hit this ("global DSF back").
- Per-tile build = production DEM: `_load_airport_dem(lat0,lon0,
  override_dem=tile_dem)`; `_sample_dem(dem, tile_lat, tile_lon, lat, lon)`
  — args in THAT order.
- User edits files in parallel (config.py consolidation, docs, tools/*heca*)
  — re-check `git status`/`git log`; commit ONLY your own files.
