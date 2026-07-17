# Coastal bathymetry: reef-visible masks and DSF sea_level raster

Status: frozen 2026-07-16 (lead session). Implements the three options
approved by the user plus the `masks_use_DEM_too` auto mode and
depth-driven inland transparency.

## 1. Goal

Fetch measured seabed depth along a tile's coastline (the bathymetry
analogue of the airport-inset / coastline-band pattern: data only where
it matters) and use it three ways:

1. **Masks** — replace the distance-only land→water fade with a
   depth-graded alpha so shallow water (reefs, lagoons, sand flats)
   keeps its imagery visible and deep water fades to X-Plane water.
2. **`masks_use_DEM_too` auto** — the DEM-refined mask coastline turns
   itself on when good data covers the tile.
3. **DSF `sea_level` raster** — synthesize the X-Plane 12 bathymetry
   raster from measured depths when the Global Scenery donor DSF is
   missing (Hawaii today), and optionally splice measured depths into
   the donor raster when it exists. X-Plane 12's water shader reads
   this raster for depth-aware light filtering, so accurate depths give
   depth-correct water color in the sim — including inland water where
   the raster carries data.

## 2. Data source and provider registry

### 2.1 Provider role `bathymetry`

New `.elv` role. Bathymetry providers are **never** selected for
terrain elevation (airport insets, `elevation_level` overlays): their
vertical datum is tidal (Hawaii CUDEM has no NAVD88) and their topo
side is untrustworthy for grading. Selection helpers in
`O4_Airport_Elevation_Insets` / `O4_Elevation_Level` must filter on
role; `select_bathymetry_definition(lat, lon)` is the only entry point
that returns them.

### 2.2 First provider: NOAA NCEI CUDEM (ninth arc-second, Hawaii)

`Providers/Elevation/CUDEMHAWAII.elv`:

```
role=bathymetry
access_strategy=static_stac
catalog_url=https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/NCEI_ninth_Topobathy_Hawaii_9428/stac/catalog.json
native_resolution_m=3.4
coverage_bbox=-160.5,18.5,-154.5,22.5
vertical_datum=Local Tidal (approx MSL)
value_floor_m=-11100.0
license=Public Domain (NOAA NCEI)
attribution=NOAA NCEI Continuously Updated DEM (CUDEM)
priority=100
enabled=True
```

Further CUDEM regions (CONUS, Puerto Rico, USVI, Guam, CNMI, American
Samoa) and non-US sources follow the same shape once the coverage
research lands; the code must treat providers generically.

### 2.3 Strategy extensions (frozen API changes)

* `static_stac` — the NCEI catalogs link items **directly from the
  root** (`rel="item"`, no child collections). When a catalog has no
  `child` links, treat the catalog itself as one pseudo-collection
  whose bbox is the provider's `coverage_bbox` and whose items are the
  root's `rel="item"` links. Existing collection-tree behaviour is
  unchanged.
* `warp_vsicurl_sources_to_geotiff` — the post-warp sanitizer's value
  floor (today hardcoded −600 m) becomes per-call:
  `value_floor_m=-600.0` keyword, threaded from the provider definition
  key of the same name. Bathymetry providers set −11100. The ceiling
  (+12000) stays.
* Asset selection — CUDEM items key their single asset by tile name,
  not `dtm`; `_select_stac_dtm_assets`'s existing finest-resolution
  fallback already handles this. No change, but a test pins it.

## 3. Bathymetry band (fetch + cache)

New module `src/O4_Bathymetry_Band.py` (the name `O4_Bathymetry` is
taken by the legacy water-triangle recut module), mirroring the
coastline elevation band (`O4_Elevation_Level.ensure_coastline_band`):

* `ensure_bathymetry_band(tile) -> str | None` — returns the band VRT
  path or None. Never raises; degrades loudly via `UI.vprint`.
* Cells: the tile's 10×10 grid of 0.1° cells **plus one overhang ring
  into the neighbouring tiles** (indices −1 and 10, added 2026-07-16
  after the 37N seam at the Ria Formosa: mask squares and the DSF post
  grid straddle tile edges, and a band clamped to the tile leaves each
  neighbour's copy of a shared straddling square blind on the other
  side of the line). A cell is fetched when its centre lies within
  `bathymetry_band_km` (default 5.0, tile cfg var) **plus the cell
  half-diagonal** of the tile's OSM coastline (shared
  `cached_suffix="coastline"` query — zero extra downloads; the
  complete ways it holds extend past the tile edge, so overhang cells
  select correctly) OR of any OSM `natural=water` polygon larger than
  `max_pond` — inland reservoirs get depth data where the source has
  it. Overhang cells resolve to the OWNING tile's canonical cell path
  (its band directory, its local indices), so two adjacent builds fetch
  each shared cell once, in whichever order they run; they are stamped
  in the fetching tile's `index.json` under an owner-qualified key
  (`<stem>@<owner_short_latlon>`), and the owner's durable no-coverage
  negatives are honoured before any probe.
* Airport-radius gate (auto mode only; ruling 2026-07-16 after the
  +37-009 first fetch ran multi-hour for coastline nobody approaches
  low): `"auto"` additionally keeps only the cells within
  `bathymetry_airport_radius_km` (default 20.0, tile cfg var, 0 = the
  whole shoreline) plus the cell half-diagonal of an enabled apt.dat
  anchor — measured depth matters on approach and low-level water
  flying. Which anchor types gate is per-type user choice (ruling
  2026-07-16, "Bathymetry" settings category), matching how the user
  flies: `bathymetry_near_icao_airports` (type 1 with an `icao_code`
  metadata row, ~15k worldwide, default True),
  `bathymetry_near_other_airports` (type 1 without one — strips and
  bush fields, ~16k, default True), `bathymetry_near_seaplane_bases`
  (type 16, ~150, default True — you land ON the water),
  `bathymetry_near_heliports` (type 17, ~7k, default **False**:
  hospital pads and platforms are the densest anchor with the weakest
  over-water-approach link; measured on the Portuguese coast they
  alone grew +37-009's kept cells 70 → 90 of 100). All four unchecked
  = no measured bathymetry (the fallback story below still applies).
  Anchors come from the offline airport index (`O4_Airport_Index` TSV
  cache at `FNAMES.airport_index_cache()`, cache format v3 adds the
  per-airport `category` column; pre-v3 caches read as stale and
  rebuild once), so neighbour-tile anchors within the radius count
  too. When the index is unavailable the gate disengages (full band,
  an INFO line says why — conservative: never silently degrade
  quality). Explicit `True` never gates. The gate applies to BOTH
  consumers (masks and the DSF `sea_level` raster — one band
  footprint, one cache); beyond the radius the raster falls back to
  the default depth ramp and the masks to the distance fade plus the
  mapped shallow-water fallback (section 4.4).
* Resolution: one flat tier, `BATHYMETRY_CELL_RESOLUTION_M = 10.0`
  (mask pixels at ZL16 are ~2.4 m; a 10 m depth grid is beyond visually
  sufficient and keeps cells ~4 MB).
* Cache layout (mirrors the coastline band):
  `Elevation_data/<block>/<latlon>_bathymetry_band/cell_<col>_<row>_<code>_<res>m.tif`,
  `band_<code>.vrt`, `index.json` stamp
  `{provider, cells: {stem: ok|no_coverage}, checked}`.
  Durable no-coverage negatives are honoured like the coastline band.
* Consumers: Step 2.5 (masks) and Step 3 (DSF raster) both call
  `ensure_bathymetry_band`; second call is a cache hit.
* Performance (2026-07-16, after the Faro first-fetch measured 26 h):
  cells fetch in parallel (first cell alone warms the authenticated
  session, then a fan-out of `CELL_FETCH_WORKERS=8` divided by the
  parallel-build sibling count — the same shared-machine convention as
  the DDS conversion slots); inland water pulls cells at a fixed
  `INLAND_WATER_BAND_KM=1` reach (terrain lidar has no river depths)
  while the coastline keeps the configurable `bathymetry_band_km`; and
  `prefetch_bathymetry_band(tile)`, called at the start of the vector
  step, runs the whole fetch on a background thread so it overlaps the
  mesh build — consumers join the in-flight future (consumed on use so
  a rebuild re-evaluates). Works in both engine modes: in-process, and
  parallel worker children (one child per tile receives its steps
  sequentially, so module state carries the future from step to step).
  Cancellation: cell fetches check `red_flag` so a cancelled tile
  drains the fan-out. Duplicate-query safety: `O4_OSM_Utils` now holds
  a per-cache-file lock, so the prefetch and the vector step never both
  download the same cached query or interleave writes to its file.
* Concurrency + progress (2026-07-16, after two engine processes raced
  on +37-009's PORTUGALTIDAL band and left a zero-valid-pixel cell):
  * The band directory is guarded by a `fetch.lock` (`O_EXCL`, owner
    `{pid, host}`, mtime refreshed after every cell). A second fetcher
    waits — polling with `red_flag` checks and surfacing the other
    fetch's cells as progress — then rescans and resumes instead of
    refetching. Stale locks (dead same-host pid, or 30 min without a
    refresh for undeterminable owners) are stolen.
  * All writes are atomic: cells fetch into a pid-suffixed temp path
    `os.replace`d into place, `index.json` and the band VRT likewise.
  * Cached cells are never trusted on `index.json` alone: a cell must
    open under GDAL with at least one valid pixel; broken leftovers are
    deleted and refetched. A fresh fetch that is readable but fully
    nodata records a durable `no_coverage`; an unreadable one stays
    transient.
  * Progress: each cell completion emits a cells-done/total `vprint`
    and, when a consumer waits in the foreground (the masks/DSF step,
    or a joined prefetch), `UI.progress_bar(1, …)` — which the engine's
    `step_progress` maps to the step percent, so the GUI no longer sits
    frozen through multi-hour first fetches. A background prefetch
    never touches the bar (the mesh step owns it then).

## 4. Masks (Step 2.5)

### 4.1 `masks_use_DEM_too` auto mode

Type changes from bool to enum `False | True | "auto"`, default
`"auto"`. Legacy cfg files parse unchanged (`True`/`False` strings).
Resolution at `build_masks` start:

* `True` — legacy behaviour (DEM from `custom_dem`), plus the depth
  ramp when a bathymetry band exists.
* `"auto"` — resolves to on **iff** `ensure_bathymetry_band` returns a
  VRT **and** the covering provider's `native_resolution_m` is at most
  50 m (`AUTO_MODE_MAXIMUM_RESOLUTION_M`): the global fallbacks (GEBCO,
  450 m) cover every tile on Earth but barely resolve the shoreline, so
  they must never auto-engage the ramp — they stay available to
  explicit `True` and to the DSF raster synthesis. The band's topo side
  provides the ≥0.5 m land/water refinement; `custom_dem`, when set
  with `True`, keeps the legacy land refinement. Resolves to off
  (byte-identical legacy masks) when no fine provider covers the tile.
* `False` — off; no DEM refinement, no depth ramp.

### 4.2 Depth-graded water alpha

New `build_bathymetry_arrays(til_x, til_y, tile, band_vrt_path)` in
`O4_Mask_Utils` returning a `(land_array 6144², water_alpha 4096²)`
uint8 pair (`None` = no contribution):

* Windowed gdal read of the band VRT over the mask square, warped to
  the square's web-mercator grid (same shape as
  `build_custom_pre_mask`).
* For valid pixels with value `v <= 0` (water):
  `alpha(v) = 255 * spline(1 - min(depth, D) / D)` where `depth = -v`,
  `D = reef_visibility_depth` (tile cfg var, default 25.0 m),
  `spline(r) = 3r² − 2r³` (the existing transition profile). Depth 0 →
  255 (imagery opaque at the waterline), depth ≥ D → 0 (pure X-Plane
  water), smooth in between. The ramp targets 0, not the `ratio_water`
  grey: a non-zero floor would paint constant faint imagery over every
  deep-water pixel the band covers and produce a visible seam at the
  band's outer edge. (Inland water keeps its `ratio_water` grey from
  the pre-mask; the ramp can only add visibility on top of it.)
* The ramp extends up to the land threshold (`v <=
  mask_altitude_above`, 0.5 m), not just to the waterline: the wet
  beach strip a low-tide lidar survey measures at +0.0…+0.5 m is
  opaque imagery, and from the threshold up the land pre-mask takes
  over at the same contour — no alpha hole between them (the
  translucent patches seen among the Culatra houses, 2026-07-16).
  Higher land values and nodata contribute 0. The band's topo side
  additionally refines the land pre-mask (values ≥ 0.5 m are land)
  whenever the band exists — measured islets survive even when OSM
  misses them.
* Coverage-edge feather (2026-07-16, the "jagged squares" defect):
  where the band's data simply ENDS while the alpha is still high — an
  intertidal source that stops at the waterline, the band's outer
  limit over a wide shallow shelf, an airport-radius gate boundary —
  the raw data/nodata cliff is quantized at the band's 10 m pixels
  (bilinear warps cannot interpolate across nodata). The alpha is
  therefore feathered over `BATHYMETRY_COVERAGE_FADE_M` (150 m, the
  fallback's own fade scale): a gaussian 0..1 coverage ramp of the
  validity mask multiplies the field, with the outside first extended
  by normalized convolution so the fade decays from the measured edge
  value. Computed at quarter resolution (the band pixels are coarser
  still). Where the ramp already completed inside the data (Kauai) the
  feather multiplies near-zero values — a visual no-op.
* A mask square whose water pre-mask is empty but whose depth alpha is
  non-zero (an offshore atoll in a full-sea square) is still written —
  the legacy early-return must consult the ramp.
* Composition in `build_mask`, after the blur and land re-max:
  `blured_mask = numpy.maximum(blured_mask, bathymetry_alpha)`.
  Maximum semantics: the depth ramp can only *reveal* imagery beyond
  the distance fade (reefs stay visible past `masks_width`), never cut
  visibility inside it. The custom-extent max stays after it.

### 4.3 Inland water

`build_water_pre_mask` today draws inland water at the constant
`sea_level` grey. Depth modulation happens through the same
`build_bathymetry_alpha` maximum — where the band has valid inland
depths, shallow lake margins keep imagery and deep centres stay at
`sea_level`; where it has none (most inland water) the constant grey is
untouched. Sim-side depth lighting for inland water comes from the DSF
raster (section 5), not the mask.

### 4.4 Mapped shallow-water fallback (`osm_shallow_water_fallback`, default True)

Where `ensure_bathymetry_band` returns nothing for the masks (no fine
provider — the open-data reality for most Pacific atolls and temperate
tidal lagoons, e.g. Funafuti: GEBCO reads its lagoon as −615 m; Faro's
Ria Formosa: nothing at all), mapped OpenStreetMap shallow water is
rasterized per mask square through the same spline ramp, one category
at a time (`SHALLOW_WATER_CATEGORIES`, each with its own cached query):

| category | OSM tags | assumed depth |
|---|---|---|
| reef | `natural=reef` | 2 m |
| tidalflat | `wetland=tidalflat` | 1 m |

Deeper categories draw first (shallower wins on overlap), holes are
respected per polygon, and the whole canvas softens over
`SHALLOW_WATER_EDGE_FADE_M` (150 m) at the polygon edges, then
max-composes exactly like the measured ramp (including the early-return
rule). Measured bathymetry always wins, per mask square: the fallback
runs when no band VRT exists at all, and — since the airport-radius
gate (section 3) made the band deliberately partial in auto mode —
alongside a gated band, where it fills exactly the squares the band
left bare (`build_bathymetry_arrays` returned `(None, None)`). A square
the band touches never composes the fallback. It cannot grade basin
interiors (no depths in OSM) — that remains measured-data scope (Allen
Coral Atlas, national lidar).

The category queries reach `SHALLOW_WATER_QUERY_MARGIN_DEGREES` (0.5°)
beyond the tile (2026-07-16): mask squares straddle tile edges, and the
plain tile bbox never returns a flats polygon lying wholly on the other
side of the line, so the two tiles' copies of a straddling square would
disagree. The per-tile cache files carry
`SHALLOW_WATER_CACHE_SCHEMA = "margin-0.5"`; pre-margin caches
re-download once.

### 4.5 Intertidal-only sources (2026-07-16)

Exposed-flats lidar (`intertidal=True` in the `.elv` definition — the
seven *TIDAL twins) measures down to roughly the low-tide waterline and
no further: visually its mask contribution is a binary "flats" layer,
which the free OSM fallback above matches wherever the flats are
mapped, and the DSF `sea_level` raster's `min(measured, elevation − 2)`
convention makes its centimetre depths a strict no-op. The automatic
paths (`masks_use_DEM_too="auto"` and both DSF raster callers)
therefore skip intertidal definitions entirely — no minutes-per-cell
national-server fetches for a result OSM provides — and only explicit
`masks_use_DEM_too=True` passes `intertidal_ok=True` to
`ensure_bathymetry_band` (for regions whose OSM tidal flats are
unmapped). An intertidal twin ahead of a real bathymetry source in the
priority walk never starves it on the automatic paths.

## 5. DSF `sea_level` raster (Step 3)

`extract_elevation_and_bathymetry_data(lat, lon)` grows a companion
`synthesize_elevation_and_bathymetry_data(tile)` in `O4_DSF_Utils`; the
call site dispatches on the new tile cfg var `dsf_bathymetry`
(enum, default `"auto"`):

* `"auto"` — donor DSF present: current byte-copy behaviour (plus the
  existing `min(bathy, elev−2)` inland guard). Donor missing: if a
  bathymetry band exists, synthesize; else current empty-atom warning.
* `True` — synthesize the `sea_level` raster from the band even when
  the donor exists (donor's other rasters are preserved; the band
  raster is spliced over sea pixels only, `min`-guarded as today).
  Donor missing: full synthesis.
* `False` — legacy behaviour exactly.

Synthesis (oracle: X-Plane 12 Global Scenery DSF, verified 2026-07-16):

* `DEMN` (in DEFN): `b"elevation\0sea_level\0"`.
* Per raster, DEMS sub-atoms: `IMED` = `<BBHIIff>` version=1, bpp=2,
  flags=5, width=height=1201, scale=1.0, offset=0.0; `DMED` =
  1201×1201 int16, row-major, north-up (matches donor layout).
* `elevation` raster: `tile.dem` resampled to the 1201² grid (the
  smoothed base DEM — seam values sample the smoothed DEM, per project
  ruling), clamped to int16.
* `sea_level` raster: band VRT resampled to the same grid;
  nodata / land → `elevation − 2` (the donor's own inland safety
  margin); everywhere `min(value, elevation − 2)`.
* The donor path's returned byte-blobs and the synthesized ones are
  interchangeable at the `build_dsf` call site; no size assumptions
  outside the atom lengths already computed from `len()`.

## 6. New cfg vars (tile scope, `O4_Cfg_Vars`)

| var | type | default | hint theme |
|---|---|---|---|
| `masks_use_DEM_too` | enum False/True/auto | auto | auto engages when a bathymetry source covers the tile |
| `bathymetry_band_km` | float | 5.0 | band width around coastline/large water |
| `reef_visibility_depth` | float | 25.0 | depth (m) where imagery fades fully to `ratio_water` transparency; larger = more visible shallows (Pacific atolls: 30–40) |
| `dsf_bathymetry` | enum False/True/auto | auto | synthesize / splice the XP12 sea_level raster from measured depths |

All four appear in the settings window's "Masks" (first three) and
"DSF" (last) categories with full hints.

## 7. Acceptance

* Headless tests: ramp numerics (0 m → 255, ≥D → sea_level, spline
  midpoint), auto-mode resolution (band / no band / custom_dem),
  static_stac root-item discovery against a canned catalog JSON,
  value-floor threading, IMED/DMED encoding round-trip (parse our own
  bytes with the extraction parser).
* Live: +21-160 and +22-160 (Kauai) masks show reef structure off
  Poipu / Hanalei (mask pixels between `sea_level` and 255 beyond the
  old blur ramp); Step 3 DSF for +22-160 contains an `elevation` and
  `sea_level` raster pair where none was possible before (Global
  Scenery block absent).
* No behaviour change when no bathymetry provider covers a tile and
  `masks_use_DEM_too` is auto (legacy-byte masks).

## 8. Allen Coral Atlas (account-gated 10 m reef bathymetry)

The Atlas (allencoralatlas.org, CC-BY 4.0) is the only open depth
source for most Pacific/Asian reefs; downloads need a free account, so
it is a **local-library provider** (`CORALATLAS.elv`, priority 80,
strategy `coral_atlas_library`), not a live URL:

* Library: `Elevation_data/AllenCoralAtlas/` — user-dropped package
  zips (auto-unpacked) or extracted `*bathymetry*.tif` rasters, indexed
  by `O4_Coral_Atlas.rescan_library`. Source rasters are 16-bit
  POSITIVE CENTIMETERS (verified against Methods-Bathymetry.pdf);
  `convert_centimeter_depths_to_metres` negates/scales, non-positive →
  nodata. A successful rescan drops every tile's bathymetry-band stamp
  so previously-negative tiles re-check coverage.
* Guided fetch (`O4_Coral_Atlas.guided_fetch_for_tile`, Qt dialog under
  Tools → Allen Coral Atlas): drives the Atlas's own web API —
  `POST auth/login` (bearer token; password used once, never stored),
  `POST mapping/aois` (tile polygon), `GET mapping/aois/<id>/products`,
  `POST download/aois/<id>` `{datasets:{...}}` — downloads the package
  when the reply carries a link, else surfaces the server's own
  message (asynchronous email delivery) and points at the manual path.
  Endpoints were reverse-verified from the Atlas frontend 2026-07-16;
  the authenticated leg is exercised the first time a real account
  runs it, so every server message is surfaced verbatim.
* Provider fallthrough: coverage claims may exceed data (the library
  before any download), so `select_bathymetry_definitions` returns ALL
  covering providers priority-sorted and `ensure_bathymetry_band` walks
  them, falling through when one yields no cells — GEBCO stays
  reachable for the DSF raster on undownloaded reef tiles.

## 9. Out of scope (recorded)

* Depth-aware wave/foam styling (`coastal_foam_edge` composes as
  before, after the ramp).
* True 1 m bathymetry in masks (band fetches at 10 m; mask pixels are
  ~2.4 m; revisit only if reefs look blocky).
* Atlas lagoon coverage limits: satellite-derived depth exists only
  where the bottom is visible (~0-20 m in clear water); deeper lagoon
  centres stay unmeasured and keep the plain-water look.
