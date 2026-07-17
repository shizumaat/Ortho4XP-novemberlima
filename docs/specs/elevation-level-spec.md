# Tile-wide elevation detail level — specification

Status: approved for implementation 2026-07-15.
Companion to `docs/airport_elevation_insets_spec.md` (whose section 3.6
deliberately capped tile-wide sources at 1 arc-second "in auto mode" and
anticipated explicit finer selection).

## 1. Motivation

The imagery side lets the user choose a zoom level per tile; the
elevation side today has one automatic policy: a base source capped at
1 arc-second (~30 m) plus meter-class lidar insets confined to airport
neighbourhoods. Island tiles, fjords, and mountainous terrain are the
cases where the user wants to spend more download/memory budget and get
tile-wide relief finer than 30 m. This feature adds a per-tile
**elevation level** selector — the elevation analogue of the imagery
zoom level.

## 2. User model

New tile configuration variable `elevation_level` (string):

| value | meaning | working grid | `.alt` size |
|---|---|---|---|
| `auto` (default) | exactly today's behaviour: 1″-capped base + airport insets + probe-driven densification | 1″, densified to 1/2″ or 1/3″ over inset tiles | 52–467 MB |
| `30` | 30 m class | 1 arc-second (3601²) | 52 MB |
| `10` | 10 m class | 1/3 arc-second (10801²) | 467 MB |
| `5`  | 5 m class | 1/6 arc-second (21601²) | 1.87 GB |
| `1`  | meter-class sources | 1/9 arc-second (32401², ≈3.4 m posting) | 4.2 GB |

Honesty rule for the `1` level: a literal 1 m posting over a full tile
is a ~40 GB raster and cannot feed the whole-tile-in-memory mesher; the
level therefore fetches meter-class sources and carries them on a
1/9 arc-second grid (≈3.4 m). The hint text states this plainly, along
with the memory expectation (roughly 20 GB peak during the smoothing
and bake stages at 1/9″).

Interplay rules (all deterministic from the tile configuration, so
step 1 and step 2 always agree):

- A numeric level **raises, never lowers**, the working-grid factor:
  `effective_factor = max(level_factor, auto_inset_factor)`. Forcing a
  coarse grid remains the job of an explicit
  `working_grid_arc_seconds` pin, which continues to win outright.
- The level factor is **capped by available data**: if the finest
  wide-area source covering the tile is 10 m, level `5` or `1` uses the
  factor that 10 m data warrants (1/3″) and logs one info line. No
  memory is spent on postings no source can fill.
- `custom_dem` set: the overlay fetch is skipped (the user pinned their
  raster) but the level factor still applies, so a high-resolution
  custom raster can actually reach the mesh.
- Airport elevation insets remain active on top of the overlay exactly
  as today (they carry the 3 m warp and hydro-flat detection).

## 3. Mechanics

### 3.1 Tile-wide overlay (the data path)

For a numeric level the existing per-airport inset machinery is reused
with the whole tile as the bounding box:

- **Provider selection** — enabled `.elv` definitions (any role) whose
  coverage reaches the tile AND whose access strategy declares
  `supports_wide_area = True`, ranked finest-native-resolution first,
  then priority. Wide-area-capable strategies are the windowed
  `/vsicurl`/overview readers: `tnm_cog`, `stac`, `wcs`, `direct_cog`,
  `geojson_tile_index`, `os_grid_bucket`. Tile-download strategies
  (zip archives, LERC blobs, ASCII sheets, `wcs_kvp`, `wfs_tile_index`)
  stay inset-only: a whole tile through them means downloading an
  entire national campaign at native resolution.
- **Fetch** — one call through the existing `fetch_inset` seam with
  `bounding_box = (lon, lat, lon+1, lat+1)` and
  `target_resolution_m = 30.87 / effective_factor` (the grid posting;
  warping finer than the grid is wasted bytes). COG overview pyramids
  make this cheap: a 10 m warp from a 1 m source reads the decimated
  levels only.
- **Cache** — `Elevation_data/<block>/<HEMlatlon>_tile_overlay/` with
  `<code_lower>_<res>m.tif` + provenance JSON + `index.json`
  no-coverage negatives, mirroring the airport-inset layout. Changing
  the level changes the target resolution and therefore the cache key.
- **Bake** — a new strip-wise bake (`bake_tile_overlay_into_alt_dem`)
  blends the overlay into `tile.dem.alt_dem` after densification and
  BEFORE airport smoothing (the overlay is base terrain, subject to
  smoothing; airport insets keep baking last, after smoothing). Strips
  are windowed GDAL reads with a halo, so bake memory stays bounded at
  any factor. Feather weight = (distance-from-tile-edge ramp) ×
  (box-blurred valid-data mask), so:
  - the outer ~60 m of the tile always returns to the base raster —
    neighbouring tiles built at any level stay continuous, because
    both sides feather to the same shared base source at the border;
  - interior no-coverage regions (ocean, campaign edges) hand back to
    the base softly instead of with a cliff.

### 3.2 Working grid extension

- `parse_working_grid_arc_seconds` accepts `1/6` and `1/9` (factors 6
  and 9); the clamp moves from 3 to 9. The auto candidate set
  `WORKING_GRID_CANDIDATE_FACTORS = (2, 3)` is unchanged — factors 6/9
  are reachable only through a numeric level or an explicit pin.
- `resolve_working_grid_factor` gains the level override implementing
  the max() and data-cap rules above. The step-2 `.alt` size guard is
  already factor-generic.

Level → factor table: `{30: 1, 10: 3, 5: 6, 1: 9}`.

### 3.3 No level-aware base-source change (v1)

The only sub-1″ `role=base` definition (NED 1/3″) is coverage-identical
to the 3DEP overlay path, so lifting the base auto-cap buys nothing and
would require new source-dispatch plumbing in `DEM.load_data`. Where no
wide-area provider covers the tile, the level degrades gracefully: the
data cap drops the factor and one warning names the finest available
source.

## 3.4 "Auto + coastline" mode (added 2026-07-16)

`elevation_level = "coastline"`: auto behaviour PLUS a lidar band along
coastlines, graded by approach visibility.

- **Band**: every 0.1° cell of the tile whose centre lies within
  `elevation_coastline_band_km` (default 5 km) plus the cell
  half-diagonal of the tile's OSM coastline (the same cached
  `way["natural"="coastline"]` layer the pipeline already prefetches —
  shared cache, no extra download when the vector step runs anyway).
- **Approach-altitude ladder** — per-cell warp resolution from the
  distance to the nearest airport bounding box on the tile (the same
  boxes the airport insets use): within 20 km (~11 nautical miles, about
  3,400 feet above ground on a 3° glideslope) the cell warps at the
  1/3 arc-second grid posting (~10.3 m); 20–50 km (~8,500 feet) at 20 m;
  beyond 50 km — the 15,000-feet-and-above regime — at the 1 arc-second
  posting (~30.9 m). Far cells still buy lidar's vertical accuracy
  (global bases are at their worst on coasts: sea bleed, void fill)
  without paying for detail invisible from altitude. Airport insets keep
  baking their 3 m detail on top at the airports themselves.
- **Mechanics**: same wide-area provider selection as numeric levels;
  one `fetch_inset` per cell (cell bounding box, cache
  `Elevation_data/<block>/<latlon>_coastline_band/cell_<i>_<j>_<code>_<res>m.tif`);
  cells mosaic into a single `band_<code>.vrt` (`gdal.BuildVRT`,
  finest-resolution grid) that the EXISTING strip bake consumes as its
  overlay path — the blurred valid-data-mask feather already hands the
  band edge back to the base softly, and the tile-edge feather keeps
  neighbouring tiles continuous.
- **Grid**: factor 3 when at least one near-airport cell exists, else
  the historic factor; recorded in the band's `index.json` stamp so both
  build steps re-derive the same factor from disk state (never from
  fetch success), mirroring the inset convention.
- **Interplay**: numeric-level rules apply unchanged (never coarsen
  auto, explicit `working_grid_arc_seconds` pin wins, `custom_dem`
  skips the band fetch).
- **Recorded refinements, out of scope for v1**: airports in
  neighbouring tiles (an approach crossing the tile border currently
  grades by this tile's airports only); true approach-corridor cones
  from CIFP procedures instead of radial distance.

## 4. Code layout (frozen interfaces)

New core module `src/O4_Elevation_Level.py` (no GUI imports):

```python
LEVEL_GRID_FACTORS = {30: 1, 10: 3, 5: 6, 1: 9}
COASTLINE_MODE_VALUE = "coastline"
def parse_elevation_level(value) -> int | None        # None = auto/coastline/invalid
def is_coastline_mode(value) -> bool
def grid_factor_for_level(level_m, finest_source_resolution_m) -> int
def select_tile_overlay_definition(lat, lon, level_m, providers_config="auto") -> dict | None
def finest_wide_area_resolution_m(lat, lon, providers_config="auto") -> float | None
def ensure_tile_overlay(tile, dico_airports=None) -> str | None   # GeoTIFF or band VRT path
def ensure_coastline_band(tile, dico_airports) -> str | None      # band VRT path
def resolve_coastline_band_plan(tile) -> dict | None  # stamp-driven, plan-shaped
def coastline_grid_factor(tile) -> int                # from the band stamp, 1 if absent
def bake_tile_overlay_into_alt_dem(tile) -> bool
```

Surgical edits elsewhere:

- `O4_Cfg_Vars.py` — `elevation_level` tile var (values +
  value_labels + hint), added to `list_tile_vars`.
- `O4_Airport_Elevation_Insets.py` — `supports_wide_area` class flags;
  `parse_working_grid_arc_seconds` 6/9; level override in
  `resolve_working_grid_factor`.
- `O4_File_Names.py` — `tile_overlay_directory`, `tile_overlay_dem`,
  `tile_overlay_provenance`.
- `O4_Vector_Map.py` step 1 — `ensure_tile_overlay` next to
  `ensure_insets_for_tile`; bake between `densify_tile_dem_for_insets`
  and `smooth_raster_over_airports`.
- `O4_Mesh_Utils.py` — iterate-rewrite branch mirrors the step-1 order
  (densify → overlay bake → inset bake → write `.alt`).
- `O4_Settings_Model.py` — `elevation_level` joins the
  "Elevation & Airport Lidar" category.
- `O4_Qt_GUI.py` — per-tile "Elevation detail" combo in the tile
  panel, mirroring the texture-mode combo pattern
  (`read_tile_raw`/`write_tile`).

## 5. Acceptance

- `elevation_level=auto` is byte-inert: no overlay call sites fire, the
  factor resolution takes the historic path (unit-asserted).
- Headless tests, no network: parse/mapping tables; overlay provider
  selection over synthetic registries; ensure/cache recycle with a
  mocked `fetch_inset`; bake numerics on small synthetic rasters
  (edge feather, nodata hand-back, strip equivalence vs whole-array);
  settings-model round-trip; Qt combo behaviour offscreen.
- Live check (manual, not CI): one island tile at level `5` and one
  CONUS mountain tile at level `10`, inspecting `.alt` statistics and
  the seam row against an `auto` neighbour.

## 6. Out of scope (recorded)

- True 1 m whole-tile postings (needs a tiled/streaming `.alt` and
  mesher memory work).
- Multi-provider mosaics for the overlay (v1 takes the single best
  covering provider; gaps hand back to base).
- Wide-area service through tile-download strategies via server-side
  decimation, where any exists.
- Triangle-budget auto-tuning (`curvature_tol` guidance lives in the
  hint instead).
