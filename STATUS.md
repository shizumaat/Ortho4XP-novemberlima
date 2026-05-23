# Auto-Patch Status — solver inverted + bounce-back ON; grade residuals are mostly DATA

## TL;DR / current state

Suite = **10 failures** (`venv/bin/python -m pytest tests/ -q`, ~5 min):
down from **15** at the start of this session. The remaining 10 are
compare_target ×3 (re-cut), the SPJC junction-drift cluster ×4, and
grade ×3 (now mostly sub-metre data-mismatch spots, not solver
infeasibility).

This session **inverted the elevation cascade** (terminal→apron→taxi)
and **enabled the bounce-back** (`_USE_L2_FIT`), which together fixed the
terminal-flatness problem and made previously-"infeasible" grade
holdouts (SPLP stub/A) compliant by consuming network slack — exactly
the user's design. Big realisation: the leftover CYXY grade violations
were **bad apt.dat data** (a ~2 m runway-vs-DEM mismatch the user fixed
live), not a solver limitation — so **Step 2 (runway-yield) may not be
needed at all**; fix the data instead.

**Read `solver_inversion_and_bounceback.md` first** — it has every
mechanism + tunable from this session.

---

## DONE this session (all in the uncommitted commit below)

1. **Cascade INVERTED** (`unified_jacobi.py`): tiers flipped to
   `_TIER_TERMINAL=3 > _TIER_APRON=2 > _TIER_TAXI=1`; cascade solves
   terminal→apron→taxi (seam/runway still base-HARD). Terminal is now a
   flat anchor at the DEM-mean of its footprint; a terminal↔apron shared
   node is terminal-owned, so the apron yields to the flat floor →
   whole-flat terminals + 1:1 aprons fall out natively (the old
   final-flatten hack was removed).
2. **Bounce-back ENABLED**: `_USE_L2_FIT=True`, `_L2_MAX_ITERS` 40000→**2000**
   (40000 was the suite-hang cause; 2000 converges fine, builds 4–6 s).
   `_compliant_spread_fit` = the user's "flow apron→runway capping at
   grade, bounce back to consume slack" — it already stops on full
   compliance (line ~394); when it can't, the residual marks a genuinely
   stuck (usually DATA) edge. SPLP stub/A: 4.07% → **1.43%**.
3. **Terminal weld + emit guards** (committed via pipeline weld at
   pipeline.py:2733 + `layout.to_osm`): `ROLE_TERMINAL` in `_weld_roles`;
   flat shapes emit `altitude=`; **rect-role 4-corner shapes whose
   `[H,L,L,H]` pairs are within `_RECT_COLLAPSE_TOL_M=0.5 m` collapse to
   `altitude_high/low`** instead of demoting to node_altitudes (fixed
   CYXY taxiway G + the two CYXY sloping-rect invariants). node_altitudes
   is now reserved for genuinely compound sloping polygons.
4. **`shapeID` OSM tag** = index in `layout.shapes` (= the `#N` in
   test/debug messages). Stable cross-file handle for JOSM; `check_grade`
   labels now show it. Ortho4XP's patch parser ignores unknown tags.
5. **Per-tile grade audit** (`test_pavement_grade.py`): builds **each
   tile** the airport spans with the **smoothed** DEM (`_load_airport_dem`),
   not the pre-cut whole-airport superset — so it grades what ships. New
   helper `tests/conftest.is_tile_seam_vertex` exempts tile-seam vertices
   in the junction invariants (cleared 2 SPLP failures).
6. **SPLP seam geometry**: `#325` seam-wedge now absorbed
   (`tile_cut._extend_rect_over_sliver` flat-end tol got a `+1e-6` float
   guard, and now extends to the wedge's **cut edge** not the cut LINE, so
   it stays a sloping rect; a varying-seam wedge falls back to
   node_altitudes via union rather than orphaning). `absorption.py`
   embed-test now probes `half_w + 4 m` PAST the edge so it only clips a
   taxiway bounded by a real **apron**, not by the taxiway's own pavement
   (un-clipped SPLP taxi A's seam suffix).

---

## OPEN / next

- **compare_target ×3 (re-cut)** — gated on the suite being otherwise
  green; everything else is close. Re-cut tool: `tools/build_target_osm.py`.
- **SPJC junction-drift cluster ×4** — `have_source`, `neighbour_corners`,
  `taxi_rects_not_alongside_apron`, `no_vertex_on_sloping_rect_flat_edge`
  (all SPJC). Pre-existing; CYXY equivalents already cleared this session.
- **grade ×3** — now mostly **sub-metre data spots** (CYXY apron/junction
  micro-relief; SPLP/SPJC). After the #67 apt.dat fix the worst CYXY
  dropped 13%→~8% (0.4 m). Treat these as DATA first (stale/over-smoothed
  DEM, mis-anchored thresholds) — inspect with `shapeID` in JOSM.
- **CYXY #49 missing taxi rects (DEFERRED, needs user read)** — junction
  #49 sits inside large aprons (#50 ≈ 60k m², #64 ≈ 14k m²) that swallow
  G's east arm + the E centerline, so they never emit as taxi rects. Real
  apron extent or another data error?
- **SPLP `#20` seam-arm (DEFERRED)** — centerline-less seam pavement →
  junction residue; needs cross-tile centerline extension. Cosmetic.
- **#9 seam-crossing terminal (DEFERRED)** — KPHX has a seam through a
  terminal; lock V to a deterministic seam-DEM mean so both tile builds
  agree. No baseline coverage yet.
- **Step 2 runway-yield** — likely **unnecessary**: the stuck cases are
  data, and the bounce-back consumes real network slack. Revisit only if a
  genuinely-infeasible (clean-data) spot appears.

## Tunables (all in `unified_jacobi.py` / `layout.py`)
- `_USE_L2_FIT=True` (bounce-back), `_L2_MAX_ITERS=2000`.
- `_RECT_COLLAPSE_TOL_M=0.5` (rect-vs-node_altitudes at emit).
- `_PER_AXIS_JUNCTIONS=False` (still gated; pairs with
  `check_grade.run_checks(taxi_axes_ll=…)`).
- DEM attraction UNCHANGED (`DEM_ATTRACTION=0.3`, `DECAY=1.0`, `FLOOR=0.85`).

## GOTCHAS
- Per-tile build = production: `_load_airport_dem(tlat+.5,tlon+.5)` for the
  SMOOTHED DEM + `current_tile_lat/lon`; a raw `O4DEM` adds spurious grade
  noise. `_sample_dem(dem, tile_lat, tile_lon, lat, lon)` — args in THAT
  order (easy to reverse → reads ~100 km away → None).
- `timeout` binary is NOT on macOS (use the Bash tool's own timeout).
- Ortho4XP GUI caches `auto_patch` modules — restart it to pick up edits.
- User edits runway-shoulder code in parallel (`runways.py`,
  `apt_dat_reader.py`, `pipeline.py`, `clearance.py`, `tools/*heca*`) —
  commit ONLY your own files; re-check `git status`/`git log` first.
