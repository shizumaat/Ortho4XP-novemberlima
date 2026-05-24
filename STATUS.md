# Auto-Patch Status — session 47: taxilanes-through-aprons (no-absorption model, EXPERIMENTAL)

## TL;DR / current state (session 47)
**Read memory `apron_grade_standards.md` FIRST** — it has the full session-47
model, the config FLAGS, and the deferred route-graph work.

Suite (excl. compare_target) = **9 failed / 328 passed / 5 skipped**
(`venv/bin/python -m pytest tests/ -q -k "not compare_target"`, ~6 min).
Of the 9: **5 are baseline-comparable** (grade ×3 CYXY/SPLP/SPJC, plus
`have_source[SPJC]`, `no_vertex_on_sloping_rect_flat_edge[SPJC]`), and
**4 are NEW SPJC regressions from this experimental apron-lane work**:
`neighbour_corners_shared[SPJC]`, `no_long_edge_proximity[SPJC]`,
`runway_node_sharing[SPJC]`, `no_vertex_on_sloping_rect_edge[SPJC]` — the
apron-lane chains + junction gaps collide with SPJC's denser junction/rect
geometry. KNOWN; committed as an experimental checkpoint (all flag-gated,
revertible). `test_taxi_rects_not_alongside_apron` is SKIPPED (marked for
deletion — its absorption premise is reversed by the no-absorption model).

**Session-47 model (taxilanes through aprons):** stop absorbing taxi rects
that share a sloping edge with an apron → they persist as directionally-
graded rects; apron = `pav_union − rects` (automatic node parity).  Lane
rects for centerlines crossing OPEN apron (no bounded width from
`_build_taxi_rects`) are built as continuous code-letter-width RIBBON chains
(`pavement/rects.build_apron_lane_rects`), trimmed at BRANCH routing nodes to
leave bounded gaps → residue junctions, run-through at collinear
continuations.  Grade-rule research (stand 1.0% / taxilane 1.5% / 4% car) +
the stand & service-road features are GATED OFF (see flags).

**Config flags (all OFF = experiment / feature gated):**
`ABSORB_RECTS_ALONGSIDE_APRONS=False` (drives the no-absorption + apron-lane
model), `ENABLE_AIRCRAFT_STANDS=False`, `ENABLE_SERVICE_ROADS=False`,
`_PER_AXIS_JUNCTIONS=False` (unified_jacobi).

**DEFERRED (user-accepted "junction connectivity for now"):** direct lane→E/F
*continuation* chains need full ROUTE-GRAPH TRAVERSAL (follow each centerline's
apt.dat node-path to its terminating taxiway rect).  Today apron lanes connect
to the E/F network THROUGH junctions (topologically correct).

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
