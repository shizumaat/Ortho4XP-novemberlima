# STATUS — handover (2026-07-02, session 2) — **V14: route-arc spine → GLOBAL SLICE (no rects); SPJC beats baseline; default still OFF**

> Everything committed on `dev` (HEAD `2f828e1`), tree clean. Suite: **the same 19
> pre-existing failures** (list-diff identical to `/tmp/suite_failures_20260702.txt`;
> this session added 0).
> ⚠ Ortho4XP caches `auto_patch.*` — restart Ortho4XP after any commit.

## What happened this session

1. **Audit of the 1242→2533 doubling** (the old rect-path A/B): NOT worse grading —
   the violation *rate* went DOWN (5.64%→5.11%); the count doubled because the arcs +
   their corner legs were both sliced per-junction → 2.25× constrained pairs
   (sliver faces, dense cut nodes). The metric itself was also off: the CLI
   `check_grade` ran context-free (no axes/routes → no spine/blend/aniso credit).
2. **USER RULING mid-session: with the full spine, disable taxi-RECT creation — the
   spine runs everywhere.** Implemented: `O4_ROUTE_ARC_SPINE=1` now implies the
   curve-native **global slice** (`apply_route_arc_spine` runs at the slice stage;
   pav_union cut once by route+arc ways; rect emit / junction_emit / fillet /
   synthetic-spine / junction_spine all bypassed). `2f828e1`.
3. **Axes sidecar** (`10eb088`): `layout.to_osm` writes `<patch>.axes.json`;
   `tools/check_grade.py` auto-loads it → the standalone CLI now applies the SAME
   within-shape law as the solver/suite. Context-free numbers (1242 etc.) are
   obsolete; compare law-true only.
4. **Solver adaptations** (found via `tools/grade_feasibility_audit.py` — all
   violations were 0-fundamental/all-unenforced, POCS→0):
   * **No dedup for route-arc slice input** — the 3.5 m paint-dedup ate short
     junction connector fragments (481→399), disconnecting spine chains: PHASE A
     froze adjacent route chains up to 2.6 m apart (frozen-spine walls).
   * **`classify_faces` v2** — corridor by geometry (width = area/shared-edge), not
     centerline count; big multi-CL faces are JUNCTION when ≥55% of area is within
     25 m of their centerlines; only true stand/terminal pavement keeps 1 % apron law.
   * **SPINE-YIELD projection** (`route_profile/solve.py`, global-slice only, LAST
     before writeback): most nodes are spine under the slice, so "both-hard =
     genuine step" is wrong; re-project with only truth anchors hard (runway/CIFP,
     tile-seam pins, building seats, groundside pins).

## Scoreboard (law-true `tools/check_grade.py <patch>` with sidecar, within-shape)

| fixture | rect baseline (gate OFF) | route-arc global slice (ON) |
|---|---|---|
| SPJC | 198 | **185 ✓ below baseline** |
| CYXY | 138 | 300 ✗ |
| SPLP | 0 | 24 ✗ |
| HECA | 4138 | 5275 ✗ (+4 cross-shape desyncs) |

**Default stays OFF** (project bar: new ≤ old ×4 fixtures). SPJC (the mission
target) is below baseline; the other three have *named, diagnosed* residuals:

- **CYXY 300**: building-frontage seat conflicts — pairs between/next to building
  pads seated at incompatible levels 1–2 m apart (production pins seats; the audit
  proves a compliant field exists if seats could move → the building-FEASIBILITY
  seat solver must pick frontage-compatible levels). Worst: apron/-10061 (88,-399),
  apron/-10045 + building-10002 (117,-533), (-243,914).
- **SPLP 24**: ONE ~5 m wall on runway 02/20 + apron -10004 at the tile seam —
  seam DEM pins vs FAA runway profile disagree under the new face geometry
  (seam-anchor keys land differently without rects). All 24 pairs are that wall.
- **HECA 5275 vs 4138**: not yet dissected (dense-junction monster; builds clean,
  deterministic pipeline held). Suspect same building-seat class as CYXY + scale.
- **Service roads have NO shapes under the slice** (`road_zone`/`service_road`
  dead → 4 % road-carve relaxation lost; truck-route lots may not reclassify
  groundside). Phase-5-class work; likely part of CYXY's delta.
- `node_altitudes` are written at 0.1 m resolution — at sub-metre pair distances the
  rounding alone can eat the budget; part of the <0.5%-over tail is noise
  (`ELEV_ROUNDING_NOISE_M` covers half of it).

## Where things are

- Wiring: `pipeline.py` (`_global_slice_spine = CURVE_NATIVE_SPINE or
  ROUTE_ARC_SPINE`, slice branch ~line 3360; the old pre-slice hook is gone).
- Gate: `config.ROUTE_ARC_SPINE` (env `O4_ROUTE_ARC_SPINE`, default OFF).
- Solver: `route_profile/solve.py` — `truth_hard` captured pre-freeze; SPINE-YIELD
  block right before `_writeback`.
- Faces: `pavement/global_slice.py::classify_faces` (route-territory rule).
- Sidecar: `layout._write_axes_sidecar` + `tools/check_grade.py::main`.
- Iteration tools (session scratchpad patterns worth recreating): full-build script
  (`build_airport_pavement` → `to_osm` → CLI check); law-true probe = build →
  `verification.taxi_axes_ll/taxi_routes_ll` → `check_grade._check_within_shape`;
  `tools/grade_feasibility_audit.py <ICAO>` classifies fundamental vs unenforced
  (env gates apply — run with `O4_ROUTE_ARC_SPINE=1`).
- Debug: `O4_STEP_DEBUG=1` prints one_solve residuals by node type ("seam" there
  = any base_hard node incl. frozen spine, NOT just tile seams).

## NEXT SESSION

1. **CYXY building-seat frontage coupling**: make `building_feasibility` seat
   neighbouring pads/frontage at within-cap-compatible levels (or let the yield
   projection treat each building as a movable FLAT group like the audit does).
   Target: CYXY ≤ 138.
2. **SPLP runway seam wall**: reconcile tile-seam DEM pins with the runway FAA
   profile under the global slice (find where the 74.4 pin lands on the 79.8
   profile; likely `_seed_elevations` seam override vs `runway_regrade`).
3. **HECA dissection** (rate + audit + forensics — same playbook as this session).
4. Service-road shapes under the slice (slice service routes too, or emit their
   corridors as `service_road` faces) → restores road_zone + groundside reclassify.
5. Then flip `O4_ROUTE_ARC_SPINE` default ON + re-baseline the suite (recut
   compare-targets where geometry legitimately shifted).

Suggested kickoff:
> "Continue V14 (STATUS.md + memory pav_skeleton_medial_axis_spine.md): route-arc
> global slice is wired, SPJC 185<198 ✓. Drive CYXY (300 vs 138, building-seat
> frontage conflicts), SPLP (one runway seam wall, 24 vs 0) and HECA (5275 vs 4138)
> to ≤ baseline, then flip the gate default ON."

## Pre-existing suite reds (unchanged)
19 at `dev@2f828e1` — identical list to `/tmp/suite_failures_20260702.txt`.
