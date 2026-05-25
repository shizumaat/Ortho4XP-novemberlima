# Auto-Patch Status — session 48: elevation relief = stiffness-weighted cap projection

## TL;DR / current state (session 48)
**Read `docs/elevation_solver.md` FIRST** — it documents THE core
component (the elevation solver/relief), the user's model, the final design,
and every approach tried+rejected. Do not re-enter that rabbit hole blind.

**The win:** the relief "bounce" now uses a **stiffness-weighted cap
projection**. Every pavement node is soft (runway/seam HARD), but the
**terminal is a STIFF soft anchor** (`_RELIEF_TERMINAL_STIFFNESS=20`,
`_RELIEF_MAX_ITERS=12000` in `unified_jacobi.py`): it holds its DEM-centroid
and yields only the MINIMUM, only where no grade-compliant path exists. The
flexible aprons/taxi absorb the grade and the cap **overwrites the false-low
DEM** (CYXY runways sit ~693 m on the plateau while the raw DEM reads the
~670–688 m valley). This is the user's model made literal: anchor the truth,
grade away from it, overwrite DEM where the slope rule requires.

**CYXY result:** terminal sits near its DEM-centroid (**~703 m** under the
restored absorption model; was a wrongly-dragged 692 before the stiffness
fix), **within-shape grade = 0** (fully compliant).  The terminal yields only
the minimum for a compliant path — the exact value depends on the surrounding
apron geometry (the relief mechanism is the constant).  South aprons sit at
their highest-compliant level (their ~722 DEM is genuinely infeasible — well
above the SE runway ends they connect to — NOT an over-drop).

**Net change set (small + clean):**
1. **Stiffness-weighted relief** — `_compliant_spread_fit(..., stiffness=)` +
   the relief block in `solve()` (terminal stiff, all pavement soft).
2. **SPJC degenerate sloping-rect guard** — `_rect_short_ends_perpendicular`
   in `_writeback`: a tapering-wedge sub-rect falls back to `node_altitudes`
   instead of a bogus `altitude_high/low` that tilts across a perpendicular
   edge. Fixes `test_sloping_rect_slopes_only_along_axis[SPJC]`.

**Suite (excl. compare_target):** **5 failed / 280 passed / 5 skipped** —
the genuine PRE-session-47 baseline (restoring absorption removed the 4 NEW
SPJC apron-lane regressions session 47 had introduced).  The 5 = `have_source`
[SPJC] + `no_vertex_on_sloping_rect_flat_edge`[SPJC] + grade ×3 (CYXY/SPLP/
SPJC).  CYXY grade fails ONLY on the step cap — worst steps are the real
~10 m excavated terrace (apron #60 ↔ #50); CYXY **within-shape grade = 0**.
(280 vs the old 328 passed = the deleted dead-module/stands/ramp tests.)
Re-run: `venv/bin/python -m pytest tests/ -q -k "not compare_target"` (~9 min).

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
- **Junction clearance consolidation** — clearance near junctions emits
  many short node_altitudes runs with gaps → visible elevation variation
  in X-Plane. The centerline-trace fragments it. Contained to
  `clearance.py`. **The user's remaining requested item.**
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
