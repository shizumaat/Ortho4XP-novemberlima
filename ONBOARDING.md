# Onboarding — `auto_patch` (X-Plane airport terrain grading)

Welcome. This walks a new engineer (human or AI) from zero to a first build and a first
change. For the terse reference version that Claude auto-loads, see
`src/auto_patch/CLAUDE.md`; for the design-standards index see `docs/STANDARDS.md`.

## What this project does, and why
X-Plane (and the base Ortho4XP scenery generator this repo is built on) drapes airport
pavement straight over the raw elevation model. Real terrain is lumpy, so runways end up
with dips and slopes that no real airport would ever be built with — sometimes unflyable.

`auto_patch` fixes this. For any airport it:
1. Reconstructs the paved surfaces (runways, taxiways, aprons, terminals) as explicit
   geometry from OSM + the X-Plane `apt.dat` / `.dsf` data and CIFP runway data.
2. Solves an **elevation** for every vertex that obeys real aerodrome design standards —
   FAA AC 150/5300-13, EASA CS-ADR-DSN, ICAO Annex 14 — for longitudinal grade, runway
   vertical curves, runway-end safety areas (RESA), and lateral wingtip clearance.
3. Writes an OSM "patch" that Ortho4XP bakes into the tile mesh.

The goal in one line: **true FAA/EASA-compliant terrain grading for every airport in
X-Plane.**

## Mental model
- **Phase 1 = geometry + role.** Figure out where the pavement is and label each shape
  with a *role* (`runway`, `primary_parallel`, `stub`, `junction`, `apron`, `terminal`,
  `boundary`, …). The role decides which grade rule applies later.
- **Phase 2 = elevation.** Decide the altitude of every vertex. Runways follow a real FAA
  vertical profile; everything else is solved by a per-surface elevation field that keeps
  grades within spec while staying close to the terrain.
- **Coordinates** inside the builder are LOCAL METERS relative to `layout.anchor`, not
  lat/lon.
- **Tiles** are 1°×1°. A runway can cross an integer lat/lon line ("seam"); a lot of the
  tricky code exists to keep grade continuous across that seam.

## First setup
- The project lives at `/Users/noah/Ortho4XP-novemberlima`.
- Use the bundled virtualenv: **`venv/bin/python`** (there's no system `python`).
- Python **3.11+ supported, 3.13+ recommended** (~5-10% faster builds; verified
  2026-07-05 — same outputs, all dependency wheels present).  Newer Python is a
  performance gain, never a requirement.
- Inner-loop dev: `tools/fast_suite.sh` runs the suite on the cheap airports
  only (~80s vs ~3.5min) — the FULL suite remains the gate before committing.
- `venv/bin/pip` is broken — if you need to install, use `venv/bin/python -m pip`.
- Dependencies are in `requirements.txt` (the platform install scripts
  `install_mac.sh` / `install_windows.bat` both install from it); shapely is the
  core geometry library, scipy (Voronoi/splines) + networkx (skeleton graphs)
  serve the pavement-skeleton spine (`pavement/pav_skeleton.py`), and osmium
  (pyosmium) filters the Geofabrik regional OSM extracts that stand in for
  Overpass downloads (`O4_OSM_Extracts` / `O4_OSM_Extract_Filter`,
  `docs/specs/osm-regional-extracts-spec.md`).
- **PySide6 (Qt UI only).** `Ortho4XP_Qt.py` launches the modernized Qt
  interface (live map, settings window, onboarding wizard — see
  `docs/UI_MODERNIZATION.md`); it needs `PySide6` from `requirements.txt`.
  The legacy Tk interface (`Ortho4XP.py`) does not, and headless builds
  import neither (tkinter is optional in `O4_Config_Utils`).
- **GDAL (optional at runtime).** The GDAL python bindings (`osgeo`) power the
  airport elevation insets (automatic meter-class lidar over airports, see
  `docs/airport_elevation_insets_spec.md`) and GeoTIFF `custom_dem` reading.
  Both installers set it up (macOS: `brew install proj gdal` then the pinned
  `gdal` wheel from `requirements.txt`; Windows: the bundled wheel in
  `Utils/win/`). If `osgeo` cannot be imported the build still works: the
  inset feature logs one line and disables itself for that build, and
  raster `custom_dem` files fall back to zero altitude with a warning.
  Manual install on a bare system: `brew install gdal` / `apt install
  libgdal-dev`, then `venv/bin/python -m pip install gdal`.

## Your first build
Build one airport and write its patch. CYXY (Whitehorse) and SPLP are good small fixtures.

```python
import sys
sys.path[:0] = ["src", ".", "tests"]   # src/, repo root, tests/ on the path
from conftest import xplane_root
from auto_patch.pipeline import build_airport_pavement

layout = build_airport_pavement("CYXY", xplane_root(), compute_elevations=True)
layout.to_osm("/tmp/CYXY.osm")
print(len(layout.shapes), "shapes")
```

A build takes ~60–90 s. Open the resulting `/tmp/CYXY.osm` in JOSM to see the geometry.

## Running the tests
```
venv/bin/python -m pytest tests/ -q
```
~3–7 minutes. The suite is the **guardrail** — it encodes the geometry and grade
invariants for the fixture airports (SPJC, SPLP, CYXY, HECA/HEAZ, MMOX). If you change a
rule or the solver, the suite is how you know what you broke. The baseline is not fully
green; check `STATUS.md` for the currently-expected failures before assuming you caused one.

## Making your first change
1. **Read `src/auto_patch/CLAUDE.md`** for the module map and gotchas.
2. Find the right module (`pipeline.py` orchestrates; `config.py` holds the rules; the
   `pavement/` package builds geometry; `elevation_per_surface/unified_jacobi.py` is the
   active elevation solver).
3. If you're touching a **standard** (a grade cap, a clearance width), change the constant
   in `config.py` — never a magic number at a call site — and check `docs/STANDARDS.md`.
4. Build an affected airport, eyeball it in JOSM, then run the suite.
5. Put any throwaway scripts and generated OSM in `/tmp`, not the working tree.

## Gotchas that bite everyone
- **Restart Ortho4XP after editing source.** The running GUI imports `auto_patch.*` once
  and never reloads, so a live GUI runs stale code (mysterious keyword-argument errors are
  the usual symptom). A fresh `venv/bin/python` build always reflects your edits.
- **DEM smoothing.** Production feeds the solver Ortho4XP's *smoothed* airport DEM; the
  standalone path (tests/tools) replicates that blur. Don't add smoothing in production.
  Probes that pass a raw DEM use unsmoothed elevations, so their grades won't exactly
  match production.
- **Import order.** `junction_repair` and `elevation` form an import cycle — always import
  through `auto_patch.pipeline`, never `import auto_patch.junction_repair` first.

## Where to look next
- `src/auto_patch/CLAUDE.md` — module map, build/test, gotchas (the reference card).
- `docs/STANDARDS.md` — every FAA/EASA/ICAO rule → citation → code constant.
- `docs/elevation_solver.md` — the elevation solver reference (core component).
- `docs/auto_patch_design_requirements.docx`, `docs/auto_patch_tier2_plan.md` — design docs.
- `STATUS.md` — what's currently being worked on and known-failing tests.
- `tools/` — `check_grade.py` (grade validator), `build_target_osm.py` (re-cut test
  fixtures), `mesh_region_tris.py` (mesh/load-time measurement).
