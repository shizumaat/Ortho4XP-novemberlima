# OSM regional extracts — spec (2026-07-17)

## 1. Problem

Every OSM layer a tile build needs (airports, roads, coastline, water,
shallow-water fallback) is fetched from public Overpass servers.  The
client is already polite — batched union queries, per-tile caching with
schema markers, sticky server rotation, 429 backoff — but bulk tile
building is a bulk-extraction workload, and shared query servers are
not sized for it.  Cold batch builds and tag-schema bumps produce
exactly the request storms Overpass instances throttle.

## 2. Design

Serve the same per-tile layer caches from **Geofabrik regional
extracts** — daily `.osm.pbf` snapshots per country/region on a plain
HTTPS CDN with no rate limits — and keep Overpass as the fallback.

* **Same caches, same downstream.**  The extract backend produces OSM
  XML bytes equivalent to the Overpass response for the layer's
  statements over the tile bbox, substituted at the single
  `get_overpass_data` call site in `_OSM_queries_to_OSM_layer_serialized`.
  Layer parsing, tag whitelists, the per-tile cache files and their
  `o4_tag_schema` markers are untouched.
* **Lazy region acquisition.**  Nothing is downloaded up front.  The
  first build touching a region RECORDS the region as wanted and falls
  back to Overpass for that build; the background maintenance thread
  downloads the extract; subsequent builds in the region are served
  locally.
* **Background freshness.**  On every application start (Qt or CLI),
  a maintenance thread refreshes the Geofabrik region index when it is
  older than `INDEX_REFRESH_DAYS` and re-downloads any stored extract
  older than the `osm_extract_refresh_days` setting.  Users never
  manage this by hand.  Existing per-tile caches keep their historic
  lifetime (they never expired under Overpass either); extract
  freshness governs future filtering only.

## 3. Region model

* Index: `https://download.geofabrik.de/index-v1.json` (GeoJSON;
  feature properties carry `id`, `parent`, `urls.pbf`; geometries are
  region polygons).  Cached in the store; refreshed by maintenance.
* **Leaves only**: regions that are no other region's parent (states
  for the US/Germany/France, countries elsewhere).  A tile's covering
  set is every leaf whose polygon intersects the tile bbox plus the
  caller's query margin.  Border tiles legitimately map to several
  leaves; the filter reads all of them and deduplicates by element id.
* If the covering set is empty, or any covering polygon set fails to
  contain the bbox (open ocean, index gaps), the tile is not
  extract-servable: permanent Overpass fallback, no queuing.

## 4. Store

`<OSM_dir>/_regional_extracts/`:

* `index-v1.json` — the Geofabrik index.
* `<region_id>.osm.pbf` — extracts, atomic download (`.tmp` +
  `os.replace`).
* `state.json` — `{region_id: {"downloaded_at": epoch, "url": ...}}`,
  atomic rewrite.
* `wanted.json` — region ids requested by build processes, atomic
  rewrite under `wanted.lock`.  **Only the maintenance thread
  downloads.**  Parallel-build worker children merely append wants, so
  N children can never race on a multi-hundred-megabyte download; the
  maintenance thread rescans the wanted list every
  `WANTED_RESCAN_SECONDS`.

## 5. Filter (O4_OSM_Extract_Filter)

`filter_extracts_to_osm_xml(extract_paths, statements, bounding_box)`
reproduces `(statement1(bbox);statement2(bbox);...);(._;>>;);out meta;`:

* an element is SELECTED when its type ('node'/'way'/'rel') and tags
  match any statement (key present; value equal when the statement has
  one) AND its geometry touches the bbox (node inside; way with any
  node inside; relation with any member node/way touching);
* the output additionally carries the full downward closure of every
  selected element — relation member ways and nodes, way nodes — even
  when those lie outside the bbox (Overpass recursion semantics);
* output is OSM XML bytes, nodes then ways then relations, elements
  deduplicated across extract files, parseable by
  `OSM_layer.update_dicosm` (acceptance: byte stream feeds the layer
  identically to an Overpass response).

## 6. Settings

* `osm_regional_extracts` (app, bool, default True) — serve OSM layers
  from local regional extracts when available, and download extracts
  in the background for regions the user builds in.
* `osm_extract_refresh_days` (app, float, default 14) — background
  re-download age threshold.

## 7. Failure discipline

The backend is an accelerator, never a dependency: every entry point
catches everything and returns None / no-ops, leaving the historic
Overpass path exactly as it was.  A corrupt extract or index is
deleted and re-queued, never fatal.

## 8. Dependency

`osmium` (pyosmium, wheels on all three platforms) — added to
requirements.txt, both installers and ONBOARDING.md in the same
change, per the repository dependency rule.
