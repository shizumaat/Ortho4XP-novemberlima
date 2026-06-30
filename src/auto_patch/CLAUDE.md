# auto_patch — airport pavement & terrain-grading builder

## Purpose
`auto_patch` brings **true FAA / EASA-compliant terrain grading to every airport in
X-Plane**. Stock X-Plane (and base Ortho4XP) drapes airport pavement over raw DEM
terrain, so runways, taxiways, and aprons inherit whatever lumps and slopes the
elevation data has. `auto_patch` instead reconstructs each airport's paved surfaces
as explicit geometry and solves an elevation field for them that obeys the real
aerodrome design standards — longitudinal grade caps, vertical-curve limits,
runway-end safety areas (RESA), lateral wingtip clearance — so the result is a flyable,
spec-compliant surface rather than a terrain tracing. Output is an OSM patch that
Ortho4XP bakes into the tile mesh.

This is an orientation map, not a full manual. It documents what's *durable*. For
current in-flight work and handover state, read the repo-root `STATUS.md` (that file is
ephemeral; this one is not — do not track current task state here).

## Pipeline at a glance
Public entry point: `pipeline.build_airport_pavement(icao, xplane_root, *,
compute_elevations=True, tile_dem=None, airport_boundary=None)` → `PavementLayout`;
`layout.to_osm(path)` writes the patch.

Two phases:
- **Phase 1 — geometry & role.** Load OSM + apt.dat (smart best-of selector), build
  runway/taxiway rects, centerlines, stubs, junctions, terminals, aprons. Every shape
  gets a *role* (`runway`, `primary_parallel`, `stub`, `junction`, `apron`,
  `boundary`, …) that drives which grade rule applies.
- **Phase 2 — elevation.** Solve a compliant altitude for every vertex. Runways get an
  FAA vertical profile (CIFP threshold alts → regrade → redistribute). Taxiways/aprons/
  junctions are solved by the per-surface field solver. Then boundary ribbon, groundside
  pavement, bridges/tunnels, and lateral/RESA clearance cuts are emitted.

Shape coords are LOCAL METERS relative to `layout.anchor`.

## Standards implemented
The aerodrome-design rules (FAA AC 150/5300-13, EASA CS-ADR-DSN, ICAO Annex 14) are
enforced via **constants in `config.py`** — that file is the source of truth for the rule
*values*; never hard-code a rule number at a call site. The full rule→citation→constant
index lives in **`docs/STANDARDS.md`**; read it before changing any grade/clearance number.

Quick orientation: within-shape grade caps → `config.py` `ROLE_GRADE_LIMITS` (taxi/apron/
junction 1.5%, tunnel/groundside 4%, boundary/wall/clearance = none); runway profile →
`pavement/runway_segments.py` (`MAX_RUNWAY_GRADE` 1.5%, `RUNWAY_END_GRADE` 0.8%) +
`runway_regrade.py`/`runway_redistribute.py` (FAA vertical-curve K-factor); RESA / strip /
wingtip clearance → `config.py` (`*_BY_CODE` tables, `WINGSPAN_BY_CODE_LETTER`). The
within-shape grade *validator* is `tools/check_grade.py`, which reads `ROLE_GRADE_LIMITS`.

**Note:** every grade / vertical-curve rule value is defined once in `config.py`; the
solver modules (`elevation.py`, `pavement/runway_segments.py`, `runway_regrade.py`,
`groundside.py`) import those values and re-export them under their existing local names,
so there is no second copy to keep in sync. Change the number in `config.py` only.

## Key modules
- `pipeline.py` — orchestration; start here to follow the build end-to-end.
- `config.py` — all standards constants + tuning knobs (read first when touching rules).
- `osm_load.py`, `apt_dat_reader.py`, `dsf_reader.py`, `cifp_reader.py` — inputs.
- `pavement/` — phase-1 geometry: `runway_segments.py`, `runway_geometry.py`,
  `centerlines.py`, `rects.py` (taxi-rect builder), `strips.py`.
- `junction_emit.py`, `junction_rules.py`, `junction_repair.py` — junction build/repair.
- `terminals.py`, `groundside.py`, `boundary.py`, `bridges.py`, `clearance.py` — features.
- `elevation_per_surface/` — **the active elevation solver is the
  `elevation_per_surface/route_profile/` package** (`solve_route_profile`: one
  elevation profile solved on the single unified grade graph). Its
  elevation-neutral primitives (node list, DEM seed/sample, within-shape
  constraint + level-coupling graph, runway node/edge sets, writeback) live in
  `elevation_per_surface/solver_primitives.py`. `elevation.py` holds shared
  solver caps + standalone DEM loading.
- `runway_regrade.py`, `runway_redistribute.py` — runway FAA profile reconciliation.
- `tile_cut.py` — clips shapes at integer lat/lon tile boundaries (seam handling).
- `layout.py` — `PavementLayout`, `to_osm`.

## Build & test workflow
- Repo: `/Users/noah/Ortho4XP-novemberlima`. Use the venv: `venv/bin/python` (there is no
  system `python`). `venv/bin/pip` is broken — use `venv/bin/python -m pip`.
- Single-airport build in a script (needs `src/`, repo root, and `tests/` on `sys.path`):
  ```python
  from conftest import xplane_root
  from auto_patch.pipeline import build_airport_pavement
  layout = build_airport_pavement("CYXY", xplane_root(), compute_elevations=True)
  layout.to_osm("/tmp/CYXY.osm")
  ```
  A build takes ~60–90 s.
- Tests: `venv/bin/python -m pytest tests/ -q` (~3–7 min). Fixtures: SPJC, SPLP, CYXY,
  HECA/HEAZ, MMOX. The suite encodes the geometry/grade invariants — treat it as the
  guardrail when changing rules or solver behavior.
- Validate grade on an emitted patch: `tools/check_grade.py`. Other tools of note:
  `tools/build_target_osm.py` (re-cut compare-target fixtures), `tools/mesh_region_tris.py`
  (triangle-count / load-time measurement).

## Gotchas (these bite everyone)
- **DEM smoothing.** Production gets Ortho4XP's airport-SMOOTHED `tile.dem` via
  `override_dem` (smoothed *before* patch generation). The standalone path (tests, tools,
  probes) replicates that `apt_smoothing_pix=8` blur in `elevation.py`. Do NOT add
  smoothing in production. Probes that pass a raw `tile_dem=DEM(...)` use UNsmoothed
  elevations — geometry/connectivity is DEM-independent, but grade/altitude will differ
  from production.
- **Ortho4XP caches `auto_patch` imports.** The long-running Ortho4XP GUI lazily imports
  `auto_patch.*` into `sys.modules` and never reloads. After editing source, a running GUI
  keeps the OLD modules (symptom: unexpected-keyword errors from a new+stale module mix).
  **Fix = restart Ortho4XP.** A fresh `venv/bin/python` build always reflects HEAD.
- **Import cycle:** `junction_repair` ↔ `elevation`. Never `import auto_patch.junction_repair`
  first — go through `auto_patch.pipeline` (normal order) or the cycle errors.
- **Temp/debug scripts and generated OSM dumps go in `/tmp`**, not the working tree.

## Other resources
- `docs/STANDARDS.md` — the FAA/EASA/ICAO rule index (rule → citation → code constant).
- `ONBOARDING.md` (repo root) — walkthrough-style onboarding for new engineers.
- `STATUS.md` (repo root) — current handover / in-flight work (ephemeral).
- `docs/auto_patch_design_requirements.docx` — original design requirements.
- `docs/elevation_solver.md` — **the elevation solver reference** (cascade +
  stiffness-weighted relief; the model, the rules, and the approaches rejected).
- `docs/OPEN_ITEMS.md` — distilled backlog of planned-but-unbuilt work (2026-06-30 audit).
- `docs/archive/README.md` — index of superseded/retired plan docs.
- `docs/auto_patch_tier2_plan.md` — tier-2 design notes (⚠ historical: pre-rewrite module layout).
- `docs/TEST_PLAN_SPJC.md` — SPJC test plan (⚠ obsolete: SPJC now covered by pytest fixtures).
