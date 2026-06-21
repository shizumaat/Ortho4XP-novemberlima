# STATUS — handover (2026-06-20)

Branch `dev`. **Committed** (the whole seam/grade WIP — latest commit). Full suite:
**2 failed / 362 passed** (was 9 failed). SPLP + SPJC now FULLY GREEN after the
fixture recut + test adjustments below. The 2 remaining reds are OUT OF SCOPE
and untouched: `test_pavement_rests_on_source[CYXY]` and
`test_pavement_grade[HECA]`. Run `venv/bin/python -m pytest tests/ -q`.

## LATEST-3 — SPLP+SPJC fixtures recut + tests adjusted to GREEN (2026-06-20)
User: SPLP & SPJC look good in X-Plane; recut fixtures + adjust tests so both
fully pass. Done:
- **Recut fixtures** from the current build: `tests/fixtures/SPJC_target.osm`,
  `SPLP_target_tile-13-77.osm`, `SPLP_target_tile-13-78.osm`. Updated all
  `*_BASELINE` dicts + totals in `test_compare_target.py` (floors = current −5%).
  ⚠⚠ **FLAGGED REGRESSION**: SPJC `retaining_wall` 68→3 (tunnel_ramp still 41) —
  blessed per the recut directive but is a real drop worth investigating (likely
  this branch's bridges/wall work). Also SPJC airside fragmented a lot
  (apron 19→86, junction 24→180 from spine-slice/caps/decompose) — renders fine.
- **SPLP grade**: refined `tools/check_grade.py` GENERICALLY (not SPLP-special):
  `_seam_nids` now returns the **seam terrain-matching zone** — nids within
  `_SEAM_ZONE_M=400 m` of a tile boundary the airport actually CROSSES. Within-
  shape + route-band laws yield there (pavement must match the neighbour mesh).
  Single-tile airports → empty zone → byte-identical (HECA/CYXY/SPJC unaffected).
- **SPJC grade**: building↔building edge-steps exempt in `test_pavement_grade`
  (adjacent terminal pads may sit at different flat levels — building16 @30.9 vs
  building30 @29.5 = a legit 1.4 m terminal step).
- **SPJC geometry nits** (sub-meter, single-tile, NOT seam — ⚠ candidates to
  fix, accepted as visually fine): baselines bumped — `boundary_near_centerline`
  SPJC {1 offender, 60 m} (#192 @59.4 m); `A4` SPJC=2 (#229/#230 v 0.79 m
  outside); `RULE1` SPJC=3 (orphan runway-kiss nodes); `no_vertex_on_sloping_
  rect_edge` SPJC=1 (#98 runway edge t=0.911).
Result: SPLP + SPJC fully GREEN. Remaining suite reds = CYXY rests_on_source +
HECA grade (out of scope, untouched).

## LATEST-2 — near-seam apron jump (SPLP −77 #16/#17/#19) + seam→setback
**Apron complex polish** (gate `SEAM_APRON_COMPLEX_POLISH`, default ON;
unified_jacobi SPREAD block): the per-apron isolated polish skips near-seam
slivers (every vertex is seam/shared → no free interior to ramp), and the seam
DEM pin lands AFTER the grade → cliff. FIX: polish each CONNECTED apron complex
that touches a seam AS ONE — re-assert seam DEM (from `dem_elev`), hold seam +
non-apron-shared verts, free the apron↔apron interior, project via the
visibility edges. **#16/#17 7.1%→1.8%, #19 18.1%→4.7%** (residual = terrain on
the oblique seam edge + an interior visibility pinch on the real 72→76 climb;
minor collateral #23 1.6→3.2%). Suite clean (9f/355p, baseline).
**#1 seam→setback** (`_seam_lines_at_setback`): the taxi-route field now offsets
each `_seam_cut_lines` line 5 m into the current tile before `build_and_solve`,
so the seam hard-anchor + DEM sample land at the setback (where pavement ends),
not the integer boundary. Confirmed taxi seam still grades to DEM; suite clean.
**#2 field apron-to-seam — ATTEMPTED, REVERTED**: seeding the apron geodesic band
field from seam verts had ZERO effect (best-effort band overridden by legal route
bands). The working apron-to-seam is the complex polish. Residual + the user's
spine model = NEXT (see below + memory `apron_spine_grade_model.md`).

### NEXT — apron spine grade model (user ruling 2026-06-20)
The taxi CENTERLINE feasibility graph ALREADY treats seam crossings as hard
anchors and grades the route between them and runway contacts (confirmed:
cross_connector at the SPLP seam = seam DEM, rising inland). Goal: (a) a
centerline THROUGH an apron = the SPINE, its nodes graded at TAXIWAY cap (1.5%)
even inside the apron; (b) apron body grades 1% from edges to that spine.
★ FIRST ATTEMPT FAILED (reverted): a blunt cap-loosening (apron spine edges →
1.5% in `_visible_grade_edges`) DESTABILIZED the solve — build-internal within
43→57, junctions #32/#33 inherited 5.3%. The spine must be a CONTROLLED SMOOTH
ROUTE (grade the spine polyline as a taxiway profile, then grade the apron body
1% to that fixed ridge). check_grade already does per-axis apron grading
(`_per_axis_allowance` cL/cT) so the validator side is partly there. Build on
`_spine_apron_points`, `junction_spine.py`.
★ CORRECTION (the "corridor must descend to the seam" note was WRONG — it came
from the reverted spine experiment): in the SHIPPED build #16/#17 are FLAT at the
seam (72.6–72.8 = DEM, 54 m inland) and the corridor #4 right there IS graded to
the seam (69.7–72.0). The route-band seam anchoring WORKS where a centerline
crosses. The remaining residual is ONLY #19, which is a different problem: it has
NO centerline crossing it (B/#4 cross ~150 m south), so the per-crossing field
anchor never applies; #19 is a pure APRON draped over a real terrain climb
(72→76 m over ~350 m ≈ 1.4% — already > the 1% apron cap) with no taxiway through
it to shed the grade. Its 4.7% is that ~1.4% climb CONCENTRATING at a ~15 m neck
(visibility-projection distribution limit), not a corridor issue. Real #19 next
steps: (a) spread the climb evenly; (b) POLICY — is an apron over a >1% hill
allowed to sit at the terrain slope (~1.4%)? (user call).

## LATEST — runway cross-seam STEP fixed (gate `RUNWAY_SEAM_DEM_PIN`, default ON)
SPLP RW02/20 crosses the lon=−77 seam; the two tiles' setback corners diverged
**4.6 m** (−78 corner 52.7 vs −77 corner 57.3) and neither matched its own
setback DEM. **ROOT (probe-confirmed, not what the old NEXT section guessed):**
the redistribute FAA profile is FINE (anchors the centerline boundary crossing at
DEM; axis-projected corner values ~56 in BOTH tiles). The corruption is in
**tile_cut** — runways were EXCLUDED from `_PIN_SLICE_ROLES`, so the cut's
`_resample_node_altitudes_nn` grabbed a nearby flat blast-region value (52.9) and
picked different sources per tile. `faa_joint_solve` does NOT override anchored
seam samples (they're immutable) — the "gates override the seam" hypothesis was
WRONG. **FIX:** include ROLE_RUNWAY in tile_cut's post-cut terrain-pin
(`tile_cut.py` ~L278, gate `RUNWAY_SEAM_DEM_PIN`/`O4_RUNWAY_SEAM_PIN`) — each
runway setback node pinned to its own `dem.alt`, recorded on `_seam_anchor_keys`,
solver HARD-honors. EXACTLY the working apron/taxi/junction model.
**RESULT:** −78 corners 54.8/60.6, −77 corners 55.5/59.7 (all = their own setback
DEM); cross-seam south-pair step **4.6 m → 0.70 m**.
`test_runway_longitudinal_grade[SPLP]` / `test_tile_cut_parity` /
`test_runway_redistribute` all still PASS. Memory: `runway_seam_dem_pin.md`.
★ ACCEPTED COST: a within-shape grade red on the OBLIQUE seam sub-rect
(~2.8 %, 55.5→59.7 over the 148 m diagonal seam edge) — RW02/20 is 18° off the
seam, terrain rises 4.2 m along the diagonal slice, so the last setback sliver
MUST tilt to match terrain (the point — no cliff vs neighbour mesh). Terrain-
dictated, same class as the standing taxi/junction seam reds; SPLP grade test was
ALREADY red on junctions so this is NOT a net-new failing test. Not exempted in
the validator (user decision pending).

---

## What we accomplished this session

All gated, all default ON, all gate-OFF byte-identical (single-tile / non-seam
airports unaffected). Per-topic detail in the memory files cited.

1. **Width-dependent taxiway grade caps** — gate `TAXI_GRADE_BY_WIDTH`
   (`O4_TAXI_GRADE_BY_WIDTH`). ICAO code A/B (width <15 m) grade at 3 %, C–F at
   1.5 %. Single fn `config.taxi_grade_cap_for_letter`. Wired into within-shape
   (`_shape_grade`), per-corridor (`cd["cap"]`), the field (SVC-road-style
   length-scaling `narrow_lines`/`narrow_cap` in `network_profile.build_and_solve`),
   and the validator (`check_grade.py` reads a new `code_letter` tag emitted by
   `layout.to_osm`). Files: config.py, layout.py, unified_jacobi.py,
   network_profile.py, tools/check_grade.py. → memory `width_dependent_taxi_grade.md`

2. **Square taxi-rect ends** — gate `RECT_SQUARE_ENDS`. Root of CYXY cross_connector
   G's off-axis node: `_snap_corners_to_pavement` snaps each corner independently +
   `_snap_rect_sloping_edges_to_holes` re-snaps the long edge to a hole boundary →
   slanted trapezoid. FIX = `rects._square_taxi_rect_ends` post-pass (after
   hole-snap + split, before emit): collapse a slanted end's corners to the
   axis-endpoint perpendicular line, snap back to boundary, keep only if the rect
   stays within pavement. **Also fixed CYXY grade #35.** → memory `rect_square_ends.md`

3. **`PAVEMENT_INSIDE_TOL_M` 0.1 → 0.5** (junction_rules.py) — clears the CYXY
   outside-pavement vertices (0.11–0.14 m float drift from squaring).

4. **Multi-tile DEM correctness** — `elevation._compute_elevations` loads the
   CURRENT tile's DEM (not the anchor) when `current_tile_lat` is given;
   `tools/build_target_osm.py` REWRITTEN to recut PER-TILE with each tile's DEM
   (`{icao}_target_tile{+lat}{+lon}.osm`). ★ `apt_smoothing_pix` is read from the
   repo `Ortho4XP.cfg` = **4**, but DEV is **8** — FLAGGED (bumping recuts every DEM
   baseline; not changed).

5. **Seam field anchors** — gate `SEAM_FIELD_ANCHORS`. Feed every centerline×seam
   crossing into the field as a HARD anchor at the DEM value. ⚠ Samples at the tile
   BOUNDARY, NOT the setback — see NEXT.

6. **Apron isolated polish** — gate `SPREAD_APRON_GRADE`. After the within-shape
   enforce, polish each APRON in isolation (`_project_shape`): hold its HARD +
   SHARED vertices, grade only its private interior. The global projection does NOT
   converge on a frustrated welded apron belt (oscillates 2000 sweeps, falls back to
   the DEM seed → dumps the seam→interior climb into one edge); an isolated shape
   DOES converge → smooth ramp. **SPLP apron #62: 8.35% → 1.72%, validator-clean.**
   ★ APRON ONLY — junctions grade PER-AXIS; the all-pair `_project_shape`
   over-constrains them. → memory `splp_seam_apron_polish.md`

### Net test delta vs HEAD
- CYXY grade now PASSES (width #35 + square-ends); CYXY outside-pavement PASSES.
- SPLP apron #62 spot FIXED; SPLP grade still RED on JUNCTIONS (separate).
- SPJC / HECA grade + SPJC geometry reds + CYXY rests_on_source = unchanged
  standing reds.

---

## Key learnings (traps that cost hours)
- `NETWORK_PROFILE_MODEL=True` (default) ⇒ the tie-layer `else` branch in
  `_taxi_corridor_profiles` (~L7600–8400) is DEAD CODE. Active = `_network_field_stations`
  (the `network_profile` field) + per-chain `faa_joint_solve`.
- The apron/junction concentration is **projection non-convergence (oscillation)**,
  NOT infeasibility. Bands are wide; isolation converges.
- ★★ DEM PROBE TRAP: for a multi-tile airport, sampling the sliver tile in the
  ANCHOR tile's DEM (wrong offset) returns garbage (~4666 m). ALWAYS
  `_load_airport_dem(currenttile+0.5)` and sample with the CURRENT tile offset.
- The boundary DEM (lon at the integer line) IS cross-tile consistent
  (preserve_boundary / SRTM overlap). The seam is currently sampled at the SETBACK
  position, slightly off-boundary.

---

## NEXT — the SPLP seam profile model (user-specified, NOT yet coded)

**User's model (authoritative):** Ortho4XP already smooths the tile-to-tile mesh
across the setback gap — that's WHY we set the pavement back from the boundary. So:
- **Every node at our setback must be EXACTLY at DEM at its OWN setback position**
  (a hard anchor, like a runway threshold), then each profile grades smoothly from
  that anchor inward.
- The two tiles do **NOT** need to match each other — Ortho4XP bridges the gap.
- **Runway**: where it crosses into another tile, grade it like TWO separate
  runways; the seam (+ setback) is just another THRESHOLD at DEM.
- **Taxi route profile**: SAME model — treat the setback node as a hard anchor like
  a runway threshold and grade the profile between the setback and runway anchors.

### Evidence (SPLP runway across the lon=−77 seam, tiles −13/−78 vs −13/−77)
Cross-seam runway pair is **4.6 m apart** and neither matches DEM:
| node | tile | solved | boundary DEM | setback DEM |
|---|---|---|---|---|
| −12.164713, −77.0000459 | −78 | **52.70** | 54.69 | 54.84 |
| −12.164436, −76.9999541 | −77 | **57.30** | 55.35 | 55.52 |
The runway seam node is the FAA-profile value, ~2 m off DEM in OPPOSITE directions
per tile → the cross-seam step ("−78 way too low, −77 fine").

### Per-component state vs the model
- **Apron** — CORRECT per model: `apply_seam_dem_anchors` (seam_anchors.py ~L579)
  samples `dem.alt_strict` at each seam VERTEX's own setback position → pins it. Only
  acts on `node_altitudes` shapes. Remaining apron "jump" is the interior seed/polish.
- **Runway** — ✅ FIXED (see LATEST above). The diagnosis in this section was
  WRONG: the seam was NOT lost in `faa_joint_solve` (anchored samples are
  immutable there) — it was the tile_cut NN-resample corrupting the setback
  corners (runways excluded from the post-cut terrain-pin). Fix = pin runway
  setback nodes to their own DEM like aprons/taxis (gate `RUNWAY_SEAM_DEM_PIN`).
  `redistribute_runway_profile` is left as-is (its profile is sound).
- **Taxi route** — `SEAM_FIELD_ANCHORS` anchors at the boundary crossing; per the
  model it should anchor at the SETBACK node at the setback DEM.

### Where to look
- `runway_redistribute.py` — `redistribute_runway_profile`,
  `_find_centerline_boundary_crossings` (change sample point → setback),
  `_shift_thresholds_for_seams`, `_insert_seam_anchors`, `faa_joint_solve` (seam =
  hard threshold).
- `seam_anchors.py` — `apply_seam_dem_anchors` is the working apron model the runway
  should mirror (sample at the node's OWN setback position).
- `elevation_per_surface/unified_jacobi.py` — `_network_field_stations` +
  `narrow_lines9`/`seam_lines` feed for SEAM_FIELD_ANCHORS.
- Probe pattern: `_load_airport_dem(tile+0.5, tile+0.5)`, then
  `_sample_dem(dem, tile_lat, tile_lon, lat, lon)`. SPLP setback nodes: lon=−77.0000459
  (−78) / −76.9999541 (−77), lat range −12.163…−12.165. Working probes:
  `/tmp/splp_seamcmp.py`, `/tmp/splp_boundary.py`.

### Also pending (lower priority)
- SPLP junctions #26/#27 (2.38–3.26 %) — local concentrations near the 68 m seam;
  need a PER-AXIS junction polish (the apron all-pair polish over-constrains
  junctions). #70/#71 (1.64 % over 341 m) = the ROUTE-BAND law, terrain-dictated.
- Decide whether to bump repo `Ortho4XP.cfg` apt_smoothing_pix 4→8 to match dev
  (recuts all DEM baselines + fixtures).
- Recut SPJC + SPLP target fixtures (the original goal) once the seam profile is
  fixed — `tools/build_target_osm.py SPLP` now emits per-tile correctly.

---

## New gates (all default ON; `O4_*` env = `0` to disable)
`TAXI_GRADE_BY_WIDTH`, `RECT_SQUARE_ENDS`, `SEAM_FIELD_ANCHORS`,
`SPREAD_APRON_GRADE`, `RUNWAY_SEAM_DEM_PIN`, `SEAM_APRON_COMPLEX_POLISH`.
Constants: `TAXI_MAX_GRADE_NARROW=0.030`, `RECT_END_SQUARE_TOL_M=1.0`,
`PAVEMENT_INSIDE_TOL_M=0.5`.
