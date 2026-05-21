# Auto-Patch Status — 2026-05-20 session: SPJC + SPLP baselines solid (8 → 1 failures)

## TL;DR

**Test failures went 8 → 1 this session, all genuine code fixes (zero
regressions), then SPJC + SPLP `compare_target` fixtures were re-cut and
the commit tagged `spjc-splp-baselines-solid`.**  The only remaining
failure is `neighbour_corners[CYXY]` (a CYXY DEM-bridge issue, unrelated
to SPJC/SPLP).

The production "`cut_layout_at_tile_boundaries() got an unexpected keyword
argument 'dem'`" report was **not a code bug** — it's a stale in-memory
import in a long-running Ortho4XP GUI (it lazily imports `auto_patch.*`
and never reloads).  **Fix = restart Ortho4XP.**  HEAD source is correct.

## Commits this session (on `dev`, after `15d72ae`)

| Hash | What |
|---|---|
| `3da37d9` | **`grade[SPLP]`** — `nudge_runway_corners_at_seam_junctions` (tile_cut.py + pipeline.py). Junction bridging a runway corner (FAA profile) and a terrain-pinned seam stub was ungradeable (1.89% > 1.5%); the stub is created by tile_cut AFTER redistribute, so the runway corner is nudged post-tile_cut into the junction's feasible band. |
| `ad2080b` | **`have_source[SPLP]` + `outside_pavement[SPLP]`** — `_drop_floating_orphan_junctions` (junction_repair.py + pipeline.py). SPLP #33 was a 19 m² triangle sharing 0 vertices with any shape (pav_union.difference residue past a rect end). Drop junctions with area < 50 m² that share 0 vertices. |
| `580c70b`, `9cc03ac` | **`no_long_edge_proximity[SPJC]`** — re-run `_snap_to_sloping_edge_corners` after absorption. MUST run AFTER `_reclassify_apron_junctions` (else it yanks a soon-to-be-apron junction's vertex ~28 m across grass to a rect corner — the SPLP -10190 over-snap the user caught). |
| `a2652ef` | **Re-cut SPJC + SPLP `compare_target` fixtures + refresh baselines** (tagged `spjc-splp-baselines-solid`). |

(`15d72ae` = prior session's SPLP tile-boundary seam dip fix.  `dd04d8e`
runway threshold reconciliation is fine to keep — the old claim that it
"owns" grade[SPLP]/neighbour_corners[CYXY] was measured before the seam
fix; in the current tree reverting `dd04d8e` is a no-op for tests.)

## Current test failures (1)

`test_junction_neighbour_corners_shared[CYXY]` — junction #103 vs
`boundary_dem_bridge` #466: 18 bridge vertices sit exactly 1.00 m off the
junction perimeter but aren't shared nodes (the densified DEM-bridge
ribbon doesn't share the junction's coarser vertices).  NOT yet
investigated in depth.

Full suite: **289 passed / 1 failed / 1 skipped.**

## Open quality items (NOT failing tests — deferred)

### 1. SPLP -78 way `-10027`: taxiway-A SW leg is a junction, not a rect

The SW leg comes out as a long junction + triangle residue (`-10027`)
with a gap to stub A, instead of a sloping rect + clean junction.  The
rect IS built (raw `primary_parallel/A`) but **absorbed** by
`_absorb_rects_at_junction_perimeters` because a junction runs along its
sloping edge.

**Two fixes ruled out:**
* **Guard absorption to keep the rect** — CANNOT work.
  `test_taxi_rects_not_alongside_apron` is a universal invariant: no
  sloping rect may have a junction/apron along its sloping edge (it
  over-constrains the junction's elevation → cliff).  A both-edge
  guard broke that test at SPJC + CYXY.
* **Off-center rect** — based on a mis-measurement.  The ~16.5 m "right"
  width is the `-10029` junction WIDENING, not the taxiway; at
  construction the taxiway's narrowest is ~symmetric (L≈12, R≈10).  Built
  `_natural_half_widths_lr` + off-center `_rect_from_axis_extended` —
  gate didn't fire and it wouldn't cover the widening.  Reverted.

**Actual lever = TRIM:** the taxiway is a ~22 m near-symmetric strip that
widens into the `-10029` intersection; the rect is absorbed because it
extends INTO that widening.  Trim the rect's north end to stop before the
widening so the junction meets it at the SHORT edge only.  Trimming is
meant to come from `_split_centerlines_at_points` (~70 % between
intersections) — the `-10029` widening isn't a recognized split point.
NOT attempted.

### 2. Directive 3 — clip runway rects at the tile seam like taxiways

Seam-crossing runway sub-rects (SPLP `-10006` triangle, `-10008`
pentagon at lon=-77) become large irregular `node_altitudes` ways.
Adding `ROLE_RUNWAY` to `tile_cut._SLOPING_RECT_ROLES` made it WORSE (the
clip bails on the oblique 45 m-wide crossing and the terrain-pin tilts
the runway cross-section).  Needs a runway-aware seam clip that does NOT
terrain-pin the slice nodes (runways follow the FAA profile, already
seam-reconciled) and handles the wide oblique crossing.

### 3. SPLP `-159`→`-160` etc. — runway corners #154/#174 at SPJC

The Rule-2 re-snap (commit above) DOES snap SPJC junction #154 (near
stub G, ~10.95 m) and #174 (near parallel U, ~21.84 m) to rect corners.
Worth a JOSM eyeball that those don't also cross grass (they're genuine
taxiway-adjacent junctions, likely on-pavement).

## Re-cut workflow (for future fixtures)

```bash
# Per-airport target (SPLP is per-tile):
venv/bin/python tools/build_target_osm.py SPJC --out tests/fixtures/SPJC_target.osm
# SPLP per tile: build with tile_dem=DEM(lat,lon) + current_tile_lat/lon,
# write to SPLP_target_tile-13-77.osm / -78.osm.
```

Then update the per-role floors in `tests/test_compare_target.py`:
`floor = target - round(0.05 * target)` (small counts stay exact); update
BOTH each `*_BASELINE` dict AND its `*_TOTAL`.

## How to verify / reproduce

```bash
# Full suite (~3 min):
venv/bin/python -m pytest tests/ -q

# SPLP -78 build (inspect -10027 / SW-leg junction in JOSM):
venv/bin/python - <<'PY'
import sys; sys.path.insert(0,'src')
from auto_patch.pipeline import build_airport_pavement
from O4_DEM_Utils import DEM
dem=DEM(-13,-78,fill_nodata="to zero")
lay=build_airport_pavement('SPLP','/Users/noah/X-Plane 12',compute_elevations=True,
                           tile_dem=dem,current_tile_lat=-13,current_tile_lon=-78)
lay.to_osm('/tmp/SPLP_78.osm')
PY
```

Standalone `_load_airport_dem` (no `tile_dem`) replicates Ortho4XP's
`apt_smoothing_pix=8` blur (commit `15d72ae`); production gets the
smoothed DEM via `override_dem` — do NOT add smoothing in production.

## Key memory files

* `memory/feedback_general_solutions.md` — every fix must work at all baseline airports.
* `memory/feedback_root_cause_only.md` — fix root causes; ASK before band-aid post-process clean-up.
* `memory/feedback_shape_rules.md` — authoritative rect + junction construction rules.
* `memory/feedback_grade_rules.md` — grade rule (all pavement roles share 1.5 %, all-pair within-shape).
