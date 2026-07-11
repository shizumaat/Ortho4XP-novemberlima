# Airport elevation insets — specification

Status: APPROVED for implementation (2026-07-11).
Branch: `feature/airport-elevation-insets` (from `dev`).
Owner: auto_patch / core elevation.

## 1. Motivation (measured, KBNA 2026-07-11)

The pipeline builds airport terrain from coarse global elevation and then
blurs it. Measured truth chain at the KBNA water-treatment shelf
(45 m gantry: south-west foot / anchor / north-east foot / taxiway M
plateau), all metres:

| source                                   | SW foot | anchor | NE foot | twy M plateau |
|------------------------------------------|---------|--------|---------|----------------|
| 2022 USGS lidar (truth)                  | 156.8   | 155.7  | 156.0   | 166.7          |
| raw 90 m SRTM (`N36W087.hgt`, 1201²)     | 154.6   | 153.0  | 151.1   | ~164           |
| smoothed `.alt` (`apt_smoothing_pix=8`)  | 154.0   | 152.5  | 151.2   | **157.2**      |
| built mesh                               | **162.2** | 157.7 | 153.6  | 167.3          |

Two compounding defects:

1. `apt_smoothing_pix=8` is a fixed 8-PIXEL tent blur on the ~31 m/px
   working raster ≈ 250 m footprint. It erases engineered relief: the
   taxiway M plateau drops 9.5 m in the smoothed raster.
2. Grading correctly restores pavement by grade law (167.3 ≈ lidar), but
   the clearance band must then bridge from the restored pavement edge
   down to smoothing-depressed terrain — a ~17 % false ramp more than
   100 m long across off-airport ground where custom objects sit.

Fix both at the data level: fetch meter-class public elevation for the
airport neighbourhood where it exists, overlay it on the tile DEM
(Digital Elevation Model), and scale the airport smoothing radius to the
quality of the data actually covering each airport — no over-smoothing.

## 2. Goals

- G1: per-airport high-resolution elevation insets, fetched automatically
  during tile build, cached under `Elevation_data/` following existing
  Ortho4XP download/cache conventions (file-exists = cache hit).
- G2: inset values must actually reach the raster Triangle4XP consumes
  (the `.alt` file) and the auto_patch grading inputs.
- G3: `apt_smoothing_pix` adjusted automatically per airport from the
  finest source covering that airport; identical behaviour to today for
  coarse-data airports.
- G4: byte-identical output when the feature is gated off, when no
  provider covers the airport, or when GDAL is unavailable.
- G5: redistribution-safe sources only (public domain / attribution);
  write provenance sidecars.

Non-goals (this feature):
- Seating the KBNA gantry itself. Correct terrain is necessary but not
  sufficient — the object has a baked +6.5 m base and needs the
  multi-ground-cluster re-anchor work (separate feature; see
  project memory `kbna-gantry-pond-multi-foot-objects`). Acceptance here
  is TERRAIN accuracy only.
- Raising the mesh refinement floor beyond the working-grid pixel
  (Phase C, below).
- Non-US providers beyond the registry scaffolding (Phase C).

## 3. Architecture

### 3.1 Provider framework — new module `src/O4_Airport_Elevation_Insets.py`

A provider registry, ordered by preference, config
`airport_elevation_providers` (default `"usgs3dep"`). Provider contract:

```python
class AirportElevationProvider:
    name: str            # cache-key token, e.g. "usgs3dep"
    def discover(self, bbox_wgs84) -> list[SourceTile] | None
        # None = no coverage / not applicable (e.g. outside the US)
    def fetch(self, bbox_wgs84, target_resolution_m, destination_path) -> InsetResult
        # writes EPSG:4326 float32 GeoTIFF with nodata; returns metadata
```

Provider 1 — `usgs3dep` (this feature):
- Discovery: TNM Access API,
  `https://tnmaccess.nationalmap.gov/api/v1/products?datasets=Digital Elevation Model (DEM) 1 meter&bbox=W,S,E,N&outputFormat=JSON`
  (no auth). Prefer the newest `publicationDate` project.
- Fetch: ranged window read from the Cloud-Optimized GeoTIFF on
  `prd-tnm` S3 via GDAL `/vsicurl/` (`gdal.Translate` with
  `projWin`/`projWinSRS=EPSG:4326`), then `gdal.Warp` to EPSG:4326 at
  `airport_elevation_inset_resolution_m` (default **3.0 m**; the working
  mesh grid is ~31 m, so 3 m keeps headroom for Phase C at ~1/10 the bytes
  of native 1 m). Verified interactively 2026-07-11: a 453×446 window
  reads in seconds without downloading the 345 MB tile.
- Vertical datum: NAVD88 ≈ EGM96 within ~1 m in CONUS. Do NOT shift the
  lidar toward the base DEM (lidar is truth). DO compute the median
  base-vs-inset offset over the feather ring and WARN above 3 m.
- Multiple source tiles intersecting one bbox: mosaic within the fetch
  (gdal.Warp accepts several inputs).

### 3.2 Cache layout (follows `Elevation_data/<block>/` conventions)

```
Elevation_data/+30-090/N36W087_airport_insets/
    index.json                 # per-airport discovery results incl. negatives
    KBNA_usgs3dep.tif          # EPSG:4326 float32 + nodata
    KBNA_usgs3dep.json         # provenance: provider, project id, source URLs,
                               # publication date, license, fetch date, bbox,
                               # resolution, datum note
```

- Path helpers live in `O4_File_Names.py` (`airport_inset_directory`,
  `airport_inset_dem`, …) like every other cached artefact.
- Cache hit = file exists (standard Ortho4XP behaviour). `index.json`
  also records NEGATIVE results (`"KJFK": {"usgs3dep": "no-coverage",
  "checked": "2026-07-11"}`) so builds don't re-query the discovery API;
  refreshed only via the CLI tool's `--refresh`.
- Airports keyed by ICAO from the same airport collection the smoothing
  driver iterates (`smooth_raster_over_airports`,
  `src/O4_Airport_Utils.py:924`); bbox = that airport's mask union
  expanded by `airport_elevation_inset_margin_m` (default **1000 m** —
  the clearance band and object neighbourhoods extend well beyond the
  boundary polygon; KBNA's pond is ~100 m outside it).

### 3.3 Build integration

- Hook: in step 1 (`O4_Vector_Map.build_poly_file`) immediately before
  `DEM.DEM(...)` — ensure insets for every airport on the tile
  (download if missing), then hand the DEM constructor an augmented
  composite source: `inset1;inset2;…;<user custom_dem or default>`,
  reusing the EXISTING composite mechanism
  (`O4_DEM_Utils.py:42-47,157-165,285-290`). The user's `custom_dem`
  config value is never rewritten; augmentation is in-memory.
- Step 2 (`O4_Mesh_Utils.build_mesh`) reconstructs the DEM independently:
  derive the same inset list deterministically from the cache directory
  so both steps see one composite (idempotent, disk-state-driven).
- **G2 verification (mandatory, FIRST implementation task):** trace how a
  composite source reaches the `.alt` file consumed by Triangle4XP. If
  sub-DEM values are not already baked into the written raster, add a
  bake: sample each inset into the base working grid over its footprint
  **with a feathered blend band** (default 60 m) from inset to base at
  the inset edge — the composite is a hard priority overlay today and a
  registration step at the seam would show as a cliff. Prove the bake
  with a synthetic-inset unit test (flat 100 m inset over a 0 m base
  must appear in the written `.alt` at inset cells, ramp in the feather,
  base outside).
- auto_patch consumes the same `tile.dem` via `override_dem`
  (`src/auto_patch/elevation.py:239-255`) — no auto_patch change needed
  for values; grading seeds improve automatically.

### 3.4 Automatic smoothing radius (per airport)

Semantics change from "pixels of the working grid" to "pixels of the
finest source covering this airport", never exceeding today's value:

```
working_pixel_m  = tile working-grid pixel size (~30.9 m at 3601/°)
source_pixel_m(a) = finest inset pixel size if insets cover ≥ 80 % of
                    airport a's smoothing mask, else the base source's
                    TRUE pixel size capped at working_pixel_m
radius_pixels(a) = min(apt_smoothing_pix,
                       round(apt_smoothing_pix * source_pixel_m(a) / working_pixel_m))
```

Consequences: 30 m-class sources → 8 px (byte-identical to today);
10 m NED 1/3″ → 3 px; 3 m inset → 1 px; 1 m inset → 0 px (no blur —
the case measured to be harmful). Config gate `apt_smoothing_auto`
(default True); the existing per-airport `smoothing_pix` apt.dat/config
override still wins over auto. Implementation lives where the per-airport
radius is already resolved (`O4_Airport_Utils.py:951-959`); express the
radius in metres internally so the upscale-to-10 m mask step doesn't
change the physical footprint.

### 3.5 Config (all in `O4_Cfg_Vars.py`, env-overridable per fork norms)

| variable | default | meaning |
|---|---|---|
| `airport_elevation_insets` | True | master gate (G4 fallback paths) |
| `airport_elevation_providers` | "usgs3dep" | ordered provider tokens |
| `airport_elevation_inset_resolution_m` | 3.0 | warp target resolution |
| `airport_elevation_inset_margin_m` | 1000.0 | bbox margin beyond airport mask |
| `airport_elevation_inset_feather_m` | 60.0 | inset→base blend band |
| `apt_smoothing_auto` | True | per-airport radius rule (§3.4) |

### 3.6 GDAL dependency policy

GDAL python bindings (`osgeo`) are already an optional core dependency
(`has_gdal`). This feature requires them AND network access; absence of
either logs one clear line and disables insets for the build (G4).
Update `ONBOARDING.md` and both install scripts with the optional GDAL
install guidance (brew/apt system lib + `pip install gdal`), same change
(fork rule: new dependency ⇒ installers + onboarding together).

## 4. Phases

- **Phase A (this branch, agent 1):** provider framework, usgs3dep,
  cache + index + provenance, composite assembly in steps 1 and 2,
  `.alt` bake with feathering + proof test, config vars, CLI tool
  `tools/fetch_airport_elevation_insets.py` (docstring per fork rule),
  unit tests (no network in pytest — fixtures/mocks).
- **Phase B (this branch, agent 2):** automatic smoothing radius (§3.4),
  KBNA acceptance (§5), byte-identity guard runs, ONBOARDING/installer
  updates, docs.
- **Phase C (future, out of scope):** national providers (UK/France/
  Netherlands/Switzerland/Canada per research report), densified working
  grid over inset airports (Triangle4XP refinement floor = one working
  pixel, `Triangle4XP.c:7297`), vertical datum transforms.

## 5. Acceptance (Phase B, KBNA tile +36-087)

Fast-harness rule applies (>5 min ⇒ stop and use/build a harness in
`tools/`). Steps 1+2 only (no ortho/DSF needed) for the tile, then probe
the written `.alt` and `Data+36-087.mesh`:

| probe (lat, lon) | lidar truth | required |
|---|---|---|
| 45 m SW foot (36.1374844, -86.6760939) | 156.8 | `.alt` within ±1.5 m |
| 45 m anchor (36.1376421, -86.6759065)  | 155.7 | `.alt` within ±1.5 m |
| 45 m NE foot (36.1377853, -86.6757619) | 156.0 | `.alt` within ±1.5 m |
| taxiway M plateau (36.13715, -86.67650) | 166.7 | `.alt` within ±1.5 m (no plateau melt) |
| mesh transect (36.13715,-86.67650)→(36.13815,-86.67525) | staircase | shelf segment (45–100 m along) mean within ±2 m of 155.7; no monotone ramp |

Guardrails:
- `airport_elevation_insets=False` ⇒ byte-identical `.alt` and `.mesh`
  (same-path stash A/B, `PYTHONHASHSEED` pinned — fork verification rules).
- Gate ON, tile with no US coverage ⇒ byte-identical.
- Full test suite: no NEW failures (19 pre-existing failures are known;
  bisect before blaming — see project memory).

## 6. Out-of-scope follow-ups recorded

- Multi-ground-cluster object re-anchor (gantry feet) — separate feature.
- Water/shelf polygon flattening from pack-shipped author meshes.
- `apt_smoothing_pix=0` global experiment for coarse-data airports.
