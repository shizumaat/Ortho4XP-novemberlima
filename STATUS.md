# Auto-Patch Status — clearance matches surface profile; long taxiways follow terrain

## TL;DR / current state

Suite = **9 failures** (`venv/bin/python -m pytest tests/ -q`, ~4.5 min),
down from the **10** baseline at session start. Remaining 9 are all
pre-existing: compare_target ×3 (need re-cut), the SPJC cluster ×3
(`have_source`, `taxi_rects_not_alongside_apron`,
`no_vertex_on_sloping_rect_flat_edge`), and grade ×3 (CYXY/SPLP/SPJC,
mostly sub-metre data spots). `neighbour_corners` was FIXED this session.

This session reworked **DSF sourcing, profile rendering, lateral
clearance, and long-rect terrain-following**. **Read
`clearance_profile_dsf_session46.md` (memory) first.**

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
