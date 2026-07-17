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

### 3.1 Provider framework — declarative, following the imagery pattern

Ortho4XP's imagery providers are DECLARATIVE: `Providers/<Region>/<CODE>.lay`
flat `key=value` files parsed generically by
`O4_Imagery_Utils.initialize_providers_dict` (`src/O4_Imagery_Utils.py:209`),
with `Extents/` coverage polygons referenced by an `extent=` key and
`Providers/O4_Custom_URL.py` as the code escape-hatch for providers whose
requests need logic (session tokens, signing). Elevation sources today are
the OPPOSITE — a hardcoded tuple + if/elif chain in `O4_DEM_Utils.py:21-32`.
This feature follows the imagery pattern so future high-resolution sources
are added by dropping in a definition file, not editing core code.

**Definition files** — `Providers/Elevation/<CODE>.elv`, same comment and
`key=value` syntax as `.lay` (the `.lay` scanner filters by extension, so
coexistence is safe). Parsed at startup by
`initialize_elevation_providers_dict()` in the new module
`src/O4_Airport_Elevation_Insets.py` into `elevation_providers_dict`
keyed by file basename. Fields:

```
# Providers/Elevation/USGS3DEP.elv
role=airport_inset            # detail tier, see section 3.6 (base | airport_inset)
access_strategy=tnm_cog       # named fetch strategy implemented in code
discovery_url_template=https://tnmaccess.nationalmap.gov/api/v1/products?datasets=Digital Elevation Model (DEM) 1 meter&bbox={west},{south},{east},{north}&outputFormat=JSON
native_resolution_m=1
extent=USA                    # optional Extents/ code OR coverage_bbox=W,S,E,N
                              # — cheap pre-filter; discovery is authoritative
vertical_datum=NAVD88
license=Public Domain (U.S. Geological Survey)
attribution=U.S. Geological Survey 3D Elevation Program
priority=100                  # higher wins when several providers cover an airport
enabled=True
```

**Access strategies** (code, small closed set — the seam where variation
is genuinely logic, mirroring `request_type` in `.lay`): a strategy takes
the parsed definition plus `(bbox_wgs84, target_resolution_m,
destination_path)` and produces the EPSG:4326 float32 GeoTIFF with nodata,
plus discovery metadata. Phase A implements ONE strategy, `tnm_cog`
(TNM Access API discovery → ranged Cloud-Optimized GeoTIFF `/vsicurl/`
window read → `gdal.Warp`). The strategy registry is a plain dict so
Phase C additions (`wcs`, `stac`, `tile_rest` — for the national lidar
services in the research report) are one class + one dict entry each,
with NO change to discovery/fetch orchestration, caching, or composite
assembly. Shipped so far: `stac` (Phase C2, `HRDEM.elv`; also
`SWISSALTI3D.elv`, whose filename-keyed multi-resolution assets drove
the finest-GeoTIFF fallback in `_select_stac_dtm_assets`, and
`FINLAND2M.elv` via the keyless CSC Paituli mirror), `wcs`
(2026-07-15, `ENGLAND1M.elv` + `NORWAY1M.elv` + `SPAIN5M.elv` +
`POLAND1M.elv` — GDAL's WCS driver negotiates the protocol version
per endpoint, and an all-nodata post-warp check turns
inside-the-box-but-outside-the-data airports into cached no-coverage
negatives), `direct_cog` (`WALES1M.elv`, one fixed country-wide
Cloud-Optimized GeoTIFF), `static_stac` (`NEWZEALAND1M.elv` —
walks a catalog.json tree on object storage, memoising every bounding
box in one per-provider index file under `Elevation_data/`; its
LERC-compressed tiles are downloaded whole and decoded in a
subprocess through tifffile/imagecodecs, because the imagecodecs LERC
decoder and the osgeo shared libraries abort the process when loaded
together), `xyz_text_tiles` (`JAPAN5M.elv` — GSI's slippy-map text
tiles mosaicked in EPSG:3857, 5 m lidar over the nationwide 10 m
composite), and `xyz_archive_drop` (`TAIWAN20M.elv` —
browser-downloaded county archives of TWD97 XYZ sheets, converted
once to indexed GeoTIFFs under the drop folder; the tgos.tw file host
blocks non-browser clients, so the download stays manual).
2026-07-16 additions: `wfs_tile_index` (`FRANCE50CM.elv` — the
Geoplateforme's WFS tile catalog carries ready-made GeoTIFF URLs),
`tile_grid_http` (nine German Länder — deterministic kilometre tiles
in the UTM CRS, with optional directory indexes for year-stamped
names, grid anchor offsets, per-tile zips, download mode with
headers), and `wcs_kvp` (Hesse — spelled-out GetCoverage for servers
that defeat GDAL's WCS driver). `Providers/O4_Custom_Elevation.py` (mirroring
`O4_Custom_URL.py`) is the escape hatch for sources that defy the
declarative fields; absent file = no-op.

Provider selection: config `airport_elevation_providers` (default
`"auto"` = every `enabled=True` definition, ranked by `priority`;
or an explicit comma-separated list of codes for pinning/testing).

Provider 1 — `USGS3DEP.elv` + `tnm_cog` strategy (this feature):
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
  expanded by `airport_elevation_inset_margin_m` (default **2000 m** —
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
| `base_elevation_source` | "auto" | base-tier pick (§3.6): auto = best covering `role=base` ≤ 1″; legacy keywords still valid |
| `airport_elevation_providers` | "auto" | "auto" = enabled `.elv` files by priority; or explicit comma list |
| `airport_elevation_inset_resolution_m` | 3.0 | warp target resolution |
| `airport_elevation_inset_margin_m` | 2000.0 | bbox margin beyond airport mask |
| `airport_elevation_inset_feather_m` | 60.0 | inset→base blend band |
| `apt_smoothing_auto` | True | per-airport radius rule (§3.4) |

### 3.6 Unified elevation provider model — legacy refactor + detail tiers

The `.elv` registry is not only for airport insets: the LEGACY base
sources (`available_sources` tuple + if/elif download chain,
`O4_DEM_Utils.py:21-32,591-800`) are refactored onto the same registry so
every elevation source — tile-wide or airport-local — is one definition
file. Measured motivation: `ensure_elevation("View", ...)` picks the 1″
de Ferranti archive only for a HARDCODED zone whitelist
(`O4_DEM_Utils.py:606-645` — Alps/Scandinavia/New Zealand) and falls back
to 3″/90 m everywhere else, including the whole US — while a working USGS
NED 1″ downloader sits unused in the next branch of the same function.
KBNA was built from 90 m data with two 30 m sources a keyword away.

**Detail tiers** — each `.elv` declares `role=`:

| role | scope | resolution class | examples |
|---|---|---|---|
| `base` | whole 1°×1° tile | capped at 1″ (~30 m) in auto mode | VIEWFINDER1, VIEWFINDER3, NED1 |
| `airport_inset` | airport bbox + margin only | meter-class | USGS3DEP |

The cap and the bbox confinement are the performance guardrails: the
working mesh grid is 3601/° (1″), so tile-wide data finer than 1″ is
wasted download and memory (NED 1/3″ is ~450 MB/tile vs ~50 MB for 1″);
meter-class data is fetched ONLY inside airport bboxes where graded
terrain and parked objects make it visible from the air.

**Base definitions shipped** (all `role=base`):
- `VIEWFINDER1.elv` — strategy `viewfinder_zip`, `resolution=1"`,
  priority 60; the de Ferranti 1″ zone whitelist moves out of code into
  a `dem1_zones=` field (comma list of letter+number archive codes) so
  coverage updates are file edits. The Wellington (-42,174) missing-data
  exception moves to an `exclude_tiles=` field.
- `VIEWFINDER3.elv` — strategy `viewfinder_zip`, `resolution=3"`,
  priority 10, global fallback.
- `NED1.elv` — strategy `usgs_seamless` (the existing
  `prd-tnm .../Elevation/1/TIFF/current/` URL scheme), `extent=USA`,
  priority 70 (beats VIEWFINDER1 where both cover).
- `NED13.elv` — same strategy, 1/3″, `enabled=True` but priority 0:
  never auto-picked (exceeds the 1″ auto cap), selectable explicitly.
- `SRTM.elv` / `ALOS.elv` — `enabled=False`, definitions kept for the
  manual-download workflow the current code half-supports (downloads
  dead upstream, `O4_DEM_Utils.py:709-720`).

**Base selection**: new config `base_elevation_source` (default
`"auto"`): rank enabled `role=base` definitions covering the tile by
priority, capped at 1″ — for KBNA this yields NED1 (30 m) instead of
today's 90 m, tile-wide. The legacy keywords (`View`, `SRTM`, `NED1`,
`NED1/3`, `ALOS`) remain valid as ALIASES resolving into the registry
(`View` → VIEWFINDER1-with-VIEWFINDER3-fallback, exactly today's
behaviour), so existing configs and the GUI dropdown keep working.

**Compatibility invariants:**
- Cache paths unchanged: base downloads keep landing at
  `FNAMES.elevation_data(...)` / `FNAMES.viewfinderpanorama(...)` names
  (`N36W087.hgt`, `..._NED1.tif`, …) so existing `Elevation_data/`
  caches are reused byte-for-byte.
- With `base_elevation_source` explicitly set to a legacy keyword, the
  chosen URL and written file must be IDENTICAL to the pre-refactor
  code (unit-test the URL construction for a 1″-whitelist tile, a 3″
  tile, and a NED1 tile against fixed expected strings).
- The de Ferranti "don't overwrite a 1″ file with a 3″ neighbour"
  zip-extraction guard (`O4_DEM_Utils.py:698-708`) must be preserved.

### 3.7 Global surface-model fallback + building masking (2026-07-17)

`COPERNICUSGLO30.elv` closes the "no inset provider covers this
airport" gap worldwide: Copernicus DEM GLO-30 (30 m TanDEM-X radar),
one Cloud-Optimized GeoTIFF per 1° cell on the registration-free AWS
Open Data bucket, cell coordinates encoded in the object name.  The
`degree_named_cog` strategy computes each cell URL outright (no
discovery API), HEAD-probes existence (ocean-only cells are absent
from the bucket; definitive 200/404 answers are memoised per process,
transient failures are not) and reuses the shared windowed `/vsicurl/`
warp core.  `priority=1` so every national source outranks it.

GLO-30 is a SURFACE model — rooftops and canopy are baked into the
heights, which is exactly what an airport grading solver must not see.
Two consequences are load-bearing:

- **Inset-only.** The strategy sets `supports_wide_area = False`, so
  the tile-wide `elevation_level` overlay can never select it: outside
  airports (cities, forests) the corrected footprint set does not
  exist and uncorrected rooftop heights would be worse than the 90 m
  base they replaced.
- **Building masking, not height subtraction.** A post-fetch pass
  (`mask_building_footprints_in_surface_model`, gated by the
  definition flag `surface_model_building_masking`) queries Overpass
  for every OpenStreetMap `building` way/relation in the inset box,
  buffers each footprint by `footprint_mask_buffer_m` (default 35 m ≈
  one 30 m pixel + radar-layover smear), rasterizes the union onto the
  inset grid and re-interpolates every covered pixel from surrounding
  ground with `gdal.FillNodata`.  Height subtraction was rejected:
  partial pixels hold roof/ground mixtures, layover displaces returns
  beyond the walls, and mapped heights rarely match what the radar
  saw — whereas the ground under a terminal is nearly planar, so
  interpolation from its surroundings is accurate and needs footprints
  only (no height data).  Genuine nodata cells are excluded as
  interpolation sources and restored verbatim afterwards.  On any
  failure (Overpass down, zero footprints) the pass records a
  `skipped` reason in the provenance sidecar and keeps the uncorrected
  raster — an uncorrected inset still beats no inset.  The correction
  runs inside `fetch_inset`, so the cached GeoTIFF is the corrected
  one and every consumer (composite source, bake, acceptance probes)
  sees corrected values; a margin enlargement refetches and re-queries
  footprints for the grown box (no stale-footprint cache class).

Live-verified 2026-07-17 at OTHH (871 footprints, 9.4 % of pixels
masked, terminal concourse 30.9 m → 5.9 m with runways/aprons
byte-unchanged) and HECA (343 footprints, corrections to −24 m).

### 3.8 GDAL dependency policy

GDAL python bindings (`osgeo`) are already an optional core dependency
(`has_gdal`). This feature requires them AND network access; absence of
either logs one clear line and disables insets for the build (G4).
Update `ONBOARDING.md` and both install scripts with the optional GDAL
install guidance (brew/apt system lib + `pip install gdal`), same change
(fork rule: new dependency ⇒ installers + onboarding together).

## 4. Phases

- **Phase A (this branch, agent 1):** declarative provider framework
  (`.elv` parser + strategy registry + custom hook), `USGS3DEP.elv` with
  the `tnm_cog` strategy, cache + index + provenance, composite assembly
  in steps 1 and 2, `.alt` bake with feathering + proof test, config
  vars, CLI tool `tools/fetch_airport_elevation_insets.py` (docstring
  per fork rule), unit tests (no network in pytest — fixtures/mocks;
  include a parser test over a temp `.elv` file and a strategy-registry
  dispatch test proving a second strategy plugs in without orchestration
  changes).
- **Phase A2 (this branch, agent 2, after A lands):** legacy base-source
  refactor onto the registry (§3.6): `role=` tiers, base `.elv`
  definitions, `viewfinder_zip` + `usgs_seamless` strategies extracted
  from `ensure_elevation`, `base_elevation_source=auto` selection with
  the 1″ cap, legacy-keyword aliases, URL-compatibility unit tests,
  cache-path invariants.
- **Phase B (this branch, agent 3):** automatic smoothing radius (§3.4),
  KBNA acceptance (§5) — now with NED1 as auto base — byte-identity
  guard runs, ONBOARDING/installer updates, docs.
- **Phase C (approved 2026-07-11, same branch):**
  - **C1 — densified working grid over inset tiles.** The Phase B
    acceptance proved the last residual is the grid, not the bake: at the
    KBNA SW-foot probe an IDEAL bake reads +1.62 m at 30.9 m posting
    (25 m from a 10 m scarp); Triangle4XP cannot refine below one working
    pixel (`Triangle4XP.c:7297`). Fix: when any airport inset is cached
    for the tile, build the combined working raster (and `.alt`) on a
    denser grid — base upsampled bilinearly, insets baked at the denser
    posting. New config `working_grid_arc_seconds` default `"auto"`:
    1″ when no insets (byte-path identical to today); otherwise the
    COARSEST of {1/2″, 1/3″} whose IDEAL-bake error at the stored
    acceptance probes passes ±1.0 m — measured empirically from the
    cached inset BEFORE building (cheap numpy check), so we never pay
    for more grid than the data needs. Guardrails: report `.alt` size,
    Triangle4XP peak memory, triangle count, and step-2 wall time at
    KBNA vs the 1″ baseline; step-2 time must stay under 3× baseline.
    The `upsample()` 1201→3601 special case is superseded by a general
    grid-target resample for this path only.
  - **C2 — second provider family to prove extensibility.** One new
    strategy + definition, chosen for test-airport relevance: `stac` +
    `HRDEM.elv` (Canada NRCan HRDEM lidar, Open Government Licence,
    STAC API `https://datacube.services.geo.ca/stac/api/search?collections=hrdem-lidar`,
    Cloud-Optimized GeoTIFF assets). CYXY (Whitehorse) is the fork's
    primary test airport — if HRDEM covers it, this directly attacks the
    CYXY invalid-DEM dip; if not, demonstrate with a covered Canadian
    airport and record the coverage result. Vertical datum CGVD2013:
    keep the warn-don't-shift policy, log the feather-ring offset.
  - **C3 (still future):** `wcs`/`tile_rest` strategies (UK/France/
    Netherlands/Switzerland), vertical datum transforms via PROJ.

## 7. Object placement ordering (approved 2026-07-11, companion stream)

Objects are MOVED (Phase 2 y-bake: `object_anchor.structure_deltas` →
`object_rebake.apply`) and terrain is SHAPED under them (Phase 1 pads,
object terrain features) using sampled ground elevations. With insets
landing in the `.alt`/mesh, every one of those samples must read the
inset-corrected surface, and the pipeline order must guarantee the
high-resolution elevations are set BEFORE any object delta is computed.
Branch `feature/object-elevation-ordering` (from `bccf026`), merged into
the feature branch when green.

Audit + enforce (each with a file:line finding, a fix if wrong, and a
regression test or assertion):
- O1: step-1 order must be smooth THEN bake — the inset bake must not be
  blurred by the airport smoother, and the smoother's auto radius must
  not be computed from pre-bake data. Document the verified order in
  `smooth_raster_over_airports`.
- O2: auto_patch grading seeds and Phase 1 object pads
  (`object_footprints` → layout) sample `override_dem = tile.dem` —
  verify that DEM object carries the baked composite at sampling time
  (not a pre-bake copy), at both entry points (production `override_dem`
  and the standalone `_load_airport_dem` path).
- O3: Phase 2 y-bake and the object-terrain-features classifier must
  sample the FINAL built mesh / final `.alt`, and must run strictly
  after the mesh carries the insets. Add an explicit ordering assertion
  (fail loud, not silent stale sampling). Check the pack-sidecar
  classification cache fingerprint (DSF+apt.dat+obj mtimes) does NOT
  need to also key on the elevation state — if object deltas depend on
  mesh elevations, a rebuilt mesh with unchanged pack files must NOT
  reuse stale deltas (the `O4_AUTO_PATCH_REBUILD=1` gotcha class).
- O4: `tools/object_seating_report.py` and `tools/reanchor_dsf_objects.py`
  ground-truth paths — same audit, so the report tools measure the same
  surface the pipeline moves objects against.
- Acceptance: rerun the seating audit at KBNA against the inset mesh from
  the feature branch and report the float distribution shift for the
  water-treatment pool (pre: stairs invisible/floating 3.2–11.5 m).

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

Base-tier check (Phase A2): with `base_elevation_source=auto` on
+36-087 the base becomes NED1 (1″, `USGS_1_n37w087.tif`-class file) —
verify a probe well OUTSIDE the inset bbox (e.g. 36.20, -86.50) tracks
the NED1 value, and that `base_elevation_source=View` still reproduces
today's 90 m file byte-for-byte at its legacy cache path.

Guardrails:
- `airport_elevation_insets=False` AND `base_elevation_source=View` ⇒
  byte-identical `.alt` and `.mesh` (same-path stash A/B,
  `PYTHONHASHSEED` pinned — fork verification rules).
- Gate ON, tile with no US coverage ⇒ byte-identical.
- Full test suite: no NEW failures (19 pre-existing failures are known;
  bisect before blaming — see project memory).

## 6. Out-of-scope follow-ups recorded

- Multi-ground-cluster object re-anchor (gantry feet) — separate feature.
- Water/shelf polygon flattening from pack-shipped author meshes.
- `apt_smoothing_pix=0` global experiment for coarse-data airports.
