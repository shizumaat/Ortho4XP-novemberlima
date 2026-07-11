"""Automatic per-airport high-resolution elevation insets.

This module fetches meter-class public elevation (for example the United
States Geological Survey 3D Elevation Program, "3DEP") for the neighbourhood
of every airport on a tile, caches it under ``Elevation_data/``, overlays it
on the base elevation, and -- crucially -- bakes the overlaid values into the
raster that the Triangle4XP mesher actually reads.  See
``docs/airport_elevation_insets_spec.md`` for the full design.

The provider framework is DECLARATIVE, mirroring Ortho4XP's imagery
providers: a source is described by a ``Providers/Elevation/<CODE>.elv``
``key=value`` file (parsed by :func:`initialize_elevation_providers_dict`),
and the genuinely-logic part -- how bytes are fetched -- lives in a named
ACCESS STRATEGY registered in :data:`ACCESS_STRATEGIES`.  Phase A ships one
definition (``USGS3DEP.elv``) and one strategy (``tnm_cog``).  Adding a
future provider is a new ``.elv`` file plus, only if its fetch differs, one
new strategy class + one registry entry -- with zero changes to the
discovery/cache/composite/bake orchestration below.

--------------------------------------------------------------------------
G2 flow finding -- how a composite source reaches the ``.alt`` raster
--------------------------------------------------------------------------
The mandatory first investigation (spec section 3.3, goal G2) traced how a
composite ``custom_dem`` (the ``base;sub1;sub2`` syntax in
``O4_DEM_Utils.py``) reaches the ``.alt`` file that Triangle4XP consumes.

What the composite mechanism actually does (``O4_DEM_Utils.py``):
  * ``DEM.load_data`` splits ``base;sub1;sub2`` into a BASE raster
    (``self.alt_dem``, an in-memory array) plus a tuple of strict
    ``self.subdems``.
  * The subdems are consulted ONLY at QUERY TIME by ``alt_composite`` /
    ``alt_vec_composite`` (i.e. ``dem.alt(node)`` / ``dem.alt_vec(way)``),
    which return the highest-priority sub-DEM value at a point, else the
    base.  Later tokens win (``subdems[::-1]`` in ``alt_composite``).
  * ``DEM.write_to_file`` writes ONLY ``self.alt_dem`` -- the base array.
    It never consults the subdems.

Who writes and who reads the ``.alt`` file:
  * Step 1 (``O4_Vector_Map.build_poly_file`` ->
    ``O4_Airport_Utils.smooth_raster_over_airports``) smooths
    ``tile.dem.alt_dem`` in place and calls ``write_to_file`` -> ``.alt``.
  * Step 2 (``O4_Mesh_Utils.build_mesh``) loads the DEM ``info_only=True``
    (so ``alt_dem`` is ``None``) and hands Triangle4XP the on-disk ``.alt``
    file directly; the mesh vertex elevations come from that raster.

CONCLUSION: sub-DEM / inset values do NOT reach the ``.alt`` raster through
the composite mechanism.  The composite only feeds the QUERY path
(``alt_vec`` -- used for OSM vector node elevations and, via
``auto_patch.elevation._load_airport_dem(override_dem=tile.dem)``, for the
grading seeds).  So this feature needs BOTH:

  1. Composite-source augmentation (``assemble_inset_composite_source``) so
     inset values reach the vector / grading QUERY path automatically.
  2. An explicit RASTER BAKE (``bake_airport_insets_into_alt_dem``) so inset
     values reach the ``.alt`` file the mesher reads.  The bake samples each
     cached inset into ``tile.dem.alt_dem`` over its footprint with a
     feathered blend band (``airport_elevation_inset_feather_m``, default
     60 m) so the inset->base seam is a ramp, not a cliff.  It runs in
     step 1 just before ``write_to_file``, so both steps see one raster.

The synthetic-inset unit test in ``tests/test_airport_elevation_insets.py``
proves the bake: a flat inset over a flat base appears at inset cells, ramps
across the feather, and leaves the base untouched outside.
"""

import os
import json
import glob
import datetime

import numpy

try:
    from osgeo import gdal

    has_gdal = True
    gdal.UseExceptions()
except Exception:
    has_gdal = False

import O4_UI_Utils as UI
import O4_File_Names as FNAMES
import O4_Geo_Utils as GEO
import O4_DEM_Utils as DEM

# The .elv provider CODE is lower-cased in cache file names so the cache key
# survives access-strategy refactors (spec section 3.2).
NO_COVERAGE = "no-coverage"

# Detail tier (spec section 3.6).  A definition without an explicit ``role``
# is an airport inset; ``role=base`` definitions describe tile-wide sources
# (the Phase A2 legacy refactor) and are ignored by the inset path here.
ROLE_AIRPORT_INSET = "airport_inset"
ROLE_BASE = "base"

# Populated lazily by initialize_elevation_providers_dict(); keyed by the
# .elv file basename (the provider CODE, e.g. "USGS3DEP").
elevation_providers_dict = {}


# =====================================================================
# Declarative provider definition (.elv) parsing
# =====================================================================
def elevation_providers_directory():
    """Return the ``Providers/Elevation`` directory path."""
    return os.path.join(FNAMES.Provider_dir, "Elevation")


def initialize_elevation_providers_dict(providers_directory=None):
    """Parse every ``Providers/Elevation/<CODE>.elv`` file.

    Same tolerant style as ``O4_Imagery_Utils.initialize_providers_dict``:
    comments (``#``) and blank lines are ignored, ``key=value`` pairs are
    kept verbatim (unknown keys preserved), and a file that cannot be read
    or lacks the mandatory ``access_strategy`` key is skipped with one
    warning line rather than aborting the run.

    Returns the populated dictionary and also stores it in the module-level
    :data:`elevation_providers_dict`.  Keyed by file basename (the CODE).
    """
    global elevation_providers_dict
    result = {}
    directory = providers_directory or elevation_providers_directory()
    if not os.path.isdir(directory):
        elevation_providers_dict = result
        return result
    for file_name in sorted(os.listdir(directory)):
        if "." not in file_name or file_name.split(".")[-1] != "elv":
            continue
        provider_code = file_name.split(".")[0]
        definition = {"code": provider_code}
        try:
            with open(os.path.join(directory, file_name), "r") as handle:
                lines = handle.readlines()
        except Exception:
            UI.vprint(
                0,
                "   WARNING: could not read elevation provider file",
                file_name,
                "- skipping it.",
            )
            continue
        for line in lines:
            line = line.strip()
            if "#" in line:
                if line[0] == "#":
                    continue
                line = line.split("#")[0]
            if "=" not in line:
                continue
            items = line.split("=")
            key = items[0].strip()
            value = "=".join(items[1:]).strip()
            definition[key] = value
        if "access_strategy" not in definition:
            UI.vprint(
                0,
                "   WARNING: elevation provider",
                provider_code,
                "has no access_strategy field - skipping it.",
            )
            continue
        # Normalise the handful of typed fields we understand; unknown keys
        # remain untouched strings for future strategies.
        definition["role"] = (
            str(definition.get("role", ROLE_AIRPORT_INSET)).strip().lower()
            or ROLE_AIRPORT_INSET
        )
        definition["enabled"] = _parse_boolean(
            definition.get("enabled", "True")
        )
        definition["priority"] = _parse_float(
            definition.get("priority"), default=0.0
        )
        if "native_resolution_m" in definition:
            definition["native_resolution_m"] = _parse_float(
                definition.get("native_resolution_m"), default=None
            )
        if "coverage_bbox" in definition:
            definition["coverage_bbox"] = _parse_bounding_box(
                definition["coverage_bbox"]
            )
        result[provider_code] = definition
    elevation_providers_dict = result
    return result


def _parse_boolean(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_bounding_box(value):
    """Parse ``W,S,E,N`` into a ``(west, south, east, north)`` tuple."""
    try:
        parts = [float(item) for item in str(value).split(",")]
        if len(parts) == 4:
            return (parts[0], parts[1], parts[2], parts[3])
    except (TypeError, ValueError):
        pass
    return None


def select_provider_definitions(providers_config, role=ROLE_AIRPORT_INSET):
    """Rank the provider definitions to try, honouring the config value.

    ``providers_config`` is the ``airport_elevation_providers`` string:
    ``"auto"`` uses every ``enabled=True`` definition ranked by descending
    ``priority``; an explicit comma-separated list of CODES pins and orders
    the providers exactly as listed (still skipping disabled ones).

    ``role`` filters by detail tier (spec section 3.6): the airport-inset
    path passes ``"airport_inset"`` and never sees ``role=base``
    definitions.  Definitions of another role are dropped silently -- a
    ``role=base`` file living in ``Providers/Elevation`` is simply not an
    inset provider, not a misconfiguration.  Keeping the role a parameter
    lets the Phase A2 base-selection path reuse this function unchanged.
    """
    if not elevation_providers_dict:
        initialize_elevation_providers_dict()
    value = (providers_config or "auto").strip()
    if value.lower() == "auto":
        candidates = [
            definition
            for definition in elevation_providers_dict.values()
            if definition.get("enabled", True)
            and definition.get("role", ROLE_AIRPORT_INSET) == role
        ]
        candidates.sort(
            key=lambda definition: (
                -definition.get("priority", 0.0),
                definition["code"],
            )
        )
        return candidates
    ordered = []
    for token in value.split(","):
        code = token.strip()
        if not code:
            continue
        definition = elevation_providers_dict.get(code)
        if definition is None:
            UI.vprint(
                1,
                "   WARNING: unknown elevation provider code",
                code,
                "- ignoring it.",
            )
            continue
        if definition.get("role", ROLE_AIRPORT_INSET) != role:
            # Wrong tier for this path; ignore silently (not an error).
            continue
        if definition.get("enabled", True):
            ordered.append(definition)
    return ordered


def _coverage_bbox_intersects(definition, bounding_box_wgs84):
    """Cheap pre-filter: does the provider's optional coverage overlap?"""
    coverage = definition.get("coverage_bbox")
    if not coverage:
        return True
    (west, south, east, north) = bounding_box_wgs84
    (cw, cs, ce, cn) = coverage
    return not (east < cw or west > ce or north < cs or south > cn)


# =====================================================================
# Access-strategy registry (the code seam; strategy-agnostic below)
# =====================================================================
ACCESS_STRATEGIES = {}


def register_access_strategy(name):
    """Class/callable decorator that adds an access strategy to the registry."""

    def _register(strategy):
        ACCESS_STRATEGIES[name] = strategy
        return strategy

    return _register


def fetch_inset(
    definition, bounding_box_wgs84, target_resolution_m, destination_path
):
    """Dispatch a fetch to the strategy named by the provider definition.

    This is the strategy-agnostic seam: the orchestration (discovery loop,
    caching, index, provenance, composite assembly, bake) calls only this
    function and never mentions a concrete strategy.  A new strategy plugs
    in by registering itself; nothing here changes.

    Returns the provenance metadata dictionary produced by the strategy, or
    ``None`` when the strategy reports no usable coverage.
    """
    strategy_name = definition.get("access_strategy")
    strategy_factory = ACCESS_STRATEGIES.get(strategy_name)
    if strategy_factory is None:
        UI.vprint(
            1,
            "   WARNING: no access strategy named",
            strategy_name,
            "for elevation provider",
            definition.get("code"),
            "- skipping it.",
        )
        return None
    strategy = strategy_factory()
    return strategy.fetch(
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    )


def discover_inset(definition, bounding_box_wgs84):
    """Ask the provider's strategy whether it covers a bounding box.

    Returns a list of opaque source descriptors, or ``None`` for no
    coverage / not applicable.  Strategy-agnostic, like :func:`fetch_inset`.
    """
    strategy_factory = ACCESS_STRATEGIES.get(definition.get("access_strategy"))
    if strategy_factory is None:
        return None
    return strategy_factory().discover(definition, bounding_box_wgs84)


# =====================================================================
# Strategy 1: tnm_cog (TNM Access API -> /vsicurl COG window -> warp)
# =====================================================================
@register_access_strategy("tnm_cog")
class TnmCloudOptimizedGeoTiffStrategy:
    """Fetch United States Geological Survey 3DEP lidar via the National Map.

    Discovery hits the TNM Access API (no authentication) for products
    intersecting the bounding box.  Fetch performs a ranged window read from
    the Cloud-Optimized GeoTIFF on the ``prd-tnm`` S3 bucket through GDAL's
    ``/vsicurl/`` virtual file system and warps the window to EPSG:4326 at
    the requested resolution -- the full source tile (hundreds of megabytes)
    is never downloaded.
    """

    def discover(self, definition, bounding_box_wgs84):
        import requests

        (west, south, east, north) = bounding_box_wgs84
        template = definition.get("discovery_url_template", "")
        url = (
            template.replace("{west}", repr(west))
            .replace("{south}", repr(south))
            .replace("{east}", repr(east))
            .replace("{north}", repr(north))
        )
        try:
            response = requests.get(url, timeout=30)
        except Exception as error:
            UI.vprint(
                1, "   WARNING: TNM discovery request failed:", str(error)
            )
            return None
        if response.status_code != 200:
            UI.vprint(
                1,
                "   WARNING: TNM discovery returned status",
                response.status_code,
            )
            return None
        try:
            payload = response.json()
        except Exception:
            UI.vprint(1, "   WARNING: TNM discovery returned non-JSON body.")
            return None
        items = payload.get("items") or []
        sources = []
        for item in items:
            download_url = item.get("downloadURL") or (
                item.get("urls", {}) or {}
            ).get("TIFF")
            if not download_url:
                continue
            sources.append(
                {
                    "download_url": download_url,
                    "source_id": item.get("sourceId"),
                    "title": item.get("title"),
                    "publication_date": item.get("publicationDate") or "",
                    "bounding_box": item.get("boundingBox"),
                }
            )
        if not sources:
            return None
        # Newest project first (spec: prefer the newest publicationDate).
        sources.sort(
            key=lambda source: source["publication_date"], reverse=True
        )
        return sources

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        if not has_gdal:
            return None
        sources = self.discover(definition, bounding_box_wgs84)
        if not sources:
            return None
        (west, south, east, north) = bounding_box_wgs84
        centre_latitude = (south + north) / 2.0
        metres_per_degree_latitude = GEO.lat_to_m
        metres_per_degree_longitude = GEO.lon_to_m(centre_latitude)
        x_resolution_deg = target_resolution_m / metres_per_degree_longitude
        y_resolution_deg = target_resolution_m / metres_per_degree_latitude

        # Mosaic the newest-project sources; gdal.Warp accepts several inputs
        # and honours their order (later inputs win on overlap).
        newest_date = sources[0]["publication_date"]
        chosen = [
            source
            for source in sources
            if source["publication_date"] == newest_date
        ] or sources
        vsicurl_inputs = [
            "/vsicurl/" + source["download_url"] for source in chosen
        ]

        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        warp_options = gdal.WarpOptions(
            format="GTiff",
            outputType=gdal.GDT_Float32,
            dstSRS="EPSG:4326",
            outputBounds=(west, south, east, north),
            xRes=x_resolution_deg,
            yRes=y_resolution_deg,
            resampleAlg="bilinear",
            dstNodata=-32768.0,
            creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=3"],
        )
        try:
            dataset = gdal.Warp(
                destination_path, vsicurl_inputs, options=warp_options
            )
        except Exception as error:
            UI.vprint(
                1, "   WARNING: TNM warp failed:", str(error)
            )
            return None
        if dataset is None:
            return None
        dataset = None  # flush to disk

        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [source["download_url"] for source in chosen],
            "source_ids": [source["source_id"] for source in chosen],
            "project_titles": [source["title"] for source in chosen],
            "publication_date": newest_date,
            "license": definition.get("license"),
            "attribution": definition.get("attribution"),
            "vertical_datum": definition.get("vertical_datum"),
            "datum_note": (
                "Elevations are in the source vertical datum; lidar is "
                "treated as truth and is NOT shifted toward the base DEM."
            ),
            "fetch_date": datetime.date.today().isoformat(),
            "bounding_box_wgs84": list(bounding_box_wgs84),
            "resolution_m": target_resolution_m,
        }


# =====================================================================
# Orchestration (strategy-agnostic): discovery loop, cache, index
# =====================================================================
def _read_index(lat, lon):
    index_path = FNAMES.airport_inset_index(lat, lon)
    if not os.path.isfile(index_path):
        return {}
    try:
        with open(index_path, "r") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _write_index(lat, lon, index):
    index_path = FNAMES.airport_inset_index(lat, lon)
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    with open(index_path, "w") as handle:
        json.dump(index, handle, indent=2, sort_keys=True)


def ensure_airport_insets(
    lat,
    lon,
    airport_bounding_boxes,
    provider_definitions,
    target_resolution_m,
    refresh=False,
):
    """Ensure a cached inset exists for each airport, per provider ranking.

    ``airport_bounding_boxes`` maps airport identifier -> ``(west, south,
    east, north)`` in EPSG:4326 degrees.  For each airport the providers are
    tried in order; the first with coverage wins and its GeoTIFF +
    provenance sidecar are written.  ``index.json`` records positives and
    NEGATIVE (``no-coverage``) results so a rebuild never re-queries the
    discovery API; ``refresh`` forces a re-query and re-fetch.

    Returns the updated index dictionary.  Strategy-agnostic: it only calls
    :func:`discover_inset` / :func:`fetch_inset`.
    """
    index = _read_index(lat, lon)
    checked_stamp = datetime.date.today().isoformat()
    for icao in sorted(airport_bounding_boxes):
        bounding_box = airport_bounding_boxes[icao]
        airport_record = index.get(icao, {})
        for definition in provider_definitions:
            code = definition["code"]
            destination = FNAMES.airport_inset_dem(lat, lon, icao, code)
            if os.path.isfile(destination) and not refresh:
                airport_record[code] = airport_record.get(code) or "ok"
                break
            if (
                not refresh
                and airport_record.get(code) == NO_COVERAGE
            ):
                continue
            if not _coverage_bbox_intersects(definition, bounding_box):
                airport_record[code] = NO_COVERAGE
                airport_record["checked"] = checked_stamp
                continue
            UI.vprint(
                1,
                "    Fetching elevation inset for",
                icao,
                "from",
                code,
            )
            provenance = fetch_inset(
                definition, bounding_box, target_resolution_m, destination
            )
            if provenance is None:
                airport_record[code] = NO_COVERAGE
                airport_record["checked"] = checked_stamp
                continue
            provenance_path = FNAMES.airport_inset_provenance(
                lat, lon, icao, code
            )
            with open(provenance_path, "w") as handle:
                json.dump(provenance, handle, indent=2, sort_keys=True)
            airport_record[code] = "ok"
            airport_record["checked"] = checked_stamp
            break
        index[icao] = airport_record
    _write_index(lat, lon, index)
    return index


def list_cached_inset_dems(lat, lon, provider_codes=None):
    """Deterministically list the cached inset GeoTIFFs for a tile.

    Both build steps derive their composite from this same disk state, so
    the composite is idempotent and identical between steps.  Restricted to
    ``provider_codes`` (lower-cased) when given; otherwise every ``*.tif``
    in the inset directory.  Sorted by file name for determinism.
    """
    directory = FNAMES.airport_inset_directory(lat, lon)
    if not os.path.isdir(directory):
        return []
    paths = sorted(glob.glob(os.path.join(directory, "*.tif")))
    if provider_codes is None:
        return paths
    suffixes = tuple("_" + code.lower() + ".tif" for code in provider_codes)
    return [path for path in paths if path.endswith(suffixes)]


# =====================================================================
# Tile-aware wrappers (read tile config attributes)
# =====================================================================
def insets_enabled_for_tile(tile):
    """Master gate for the tile: config on AND GDAL present.

    Emits exactly one clear line when the gate is on but GDAL is missing,
    then disables the feature so the build is byte-identical to gate-off.
    """
    if not getattr(tile, "airport_elevation_insets", False):
        return False
    if not has_gdal:
        UI.vprint(
            1,
            "   INFO: airport elevation insets are enabled but the GDAL "
            "python bindings (osgeo) are unavailable - insets are disabled "
            "for this build.",
        )
        return False
    return True


def _airport_bounding_boxes(tile, dico_airports):
    """Build ``{airport: (west, south, east, north)}`` in EPSG:4326.

    Ortho4XP geometry is in tile-relative degrees; this adds the tile origin
    back and expands by ``airport_elevation_inset_margin_m`` converted to
    degrees at the tile latitude.
    """
    margin_m = getattr(tile, "airport_elevation_inset_margin_m", 1000.0)
    metres_per_degree_latitude = GEO.lat_to_m
    metres_per_degree_longitude = GEO.lon_to_m(tile.lat + 0.5)
    margin_lon = margin_m / metres_per_degree_longitude
    margin_lat = margin_m / metres_per_degree_latitude
    boxes = {}
    for airport in dico_airports:
        record = dico_airports[airport]
        boundary = record.get("boundary")
        if boundary is None or boundary.is_empty:
            continue
        (xmin, ymin, xmax, ymax) = boundary.bounds
        boxes[airport] = (
            tile.lon + xmin - margin_lon,
            tile.lat + ymin - margin_lat,
            tile.lon + xmax + margin_lon,
            tile.lat + ymax + margin_lat,
        )
    return boxes


def ensure_insets_for_tile(tile, dico_airports, refresh=False):
    """Fetch/refresh every airport inset on the tile (step-1 download hook)."""
    if not insets_enabled_for_tile(tile):
        return
    provider_definitions = select_provider_definitions(
        getattr(tile, "airport_elevation_providers", "auto")
    )
    if not provider_definitions:
        return
    boxes = _airport_bounding_boxes(tile, dico_airports)
    if not boxes:
        return
    resolution_m = getattr(
        tile, "airport_elevation_inset_resolution_m", 3.0
    )
    try:
        ensure_airport_insets(
            tile.lat,
            tile.lon,
            boxes,
            provider_definitions,
            resolution_m,
            refresh=refresh,
        )
    except Exception as error:
        # Never let inset fetching abort a build (G4 safety).
        UI.vprint(
            1,
            "   WARNING: airport elevation inset fetch raised",
            str(error),
            "- continuing without insets.",
        )


def assemble_inset_composite_source(tile, base_source):
    """Return ``base_source`` augmented in-memory with cached inset paths.

    The user's ``custom_dem`` config value is never rewritten.  Inset paths
    are APPENDED after the base (and after any user sub-DEMs) so, under the
    composite's last-token-wins priority (``O4_DEM_Utils`` ``alt_composite``
    / ``alt_vec_composite``), the high-resolution insets win at query time,
    while the first token stays the base source so step 2's
    ``split(';')[0]`` still yields the correct raster dimensions.

    (The spec's illustrative ``inset;...;custom_dem`` ordering is written
    highest-priority-first; the concrete code is last-wins, so we append.)

    Deterministic and disk-state-driven: step 1 and step 2 both call this and
    get the identical composite string.
    """
    if not insets_enabled_for_tile(tile):
        return base_source
    provider_definitions = select_provider_definitions(
        getattr(tile, "airport_elevation_providers", "auto")
    )
    codes = [definition["code"] for definition in provider_definitions]
    inset_paths = list_cached_inset_dems(
        tile.lat, tile.lon, provider_codes=codes or None
    )
    if not inset_paths:
        return base_source
    return ";".join([base_source] + inset_paths)


def bake_airport_insets_into_alt_dem(tile):
    """Bake cached insets into ``tile.dem.alt_dem`` with a feather band.

    See the module docstring's G2 note: this is the step that puts inset
    values into the ``.alt`` raster the mesher reads (the composite alone
    only feeds the query path).  Runs in step 1 immediately before
    ``write_to_file``.  A no-op -- byte-identical output -- when the feature
    is gated off, no inset covers the tile, or GDAL is missing.
    """
    if not insets_enabled_for_tile(tile):
        return
    if tile.dem is None or tile.dem.alt_dem is None:
        return
    provider_definitions = select_provider_definitions(
        getattr(tile, "airport_elevation_providers", "auto")
    )
    codes = [definition["code"] for definition in provider_definitions]
    inset_paths = list_cached_inset_dems(
        tile.lat, tile.lon, provider_codes=codes or None
    )
    if not inset_paths:
        return
    feather_m = getattr(tile, "airport_elevation_inset_feather_m", 60.0)
    for inset_path in inset_paths:
        try:
            _bake_one_inset(tile, inset_path, feather_m)
        except Exception as error:
            UI.vprint(
                1,
                "   WARNING: could not bake inset",
                os.path.basename(inset_path),
                ":",
                str(error),
            )


def _bake_one_inset(tile, inset_path, feather_m):
    """Blend a single inset GeoTIFF into the working grid over its footprint.

    The blend weight ramps linearly from 0 at the inset's data edge to 1 at
    ``feather_m`` inside it, so the seam is a ramp not a cliff.  Cells with
    inset nodata keep the base value.
    """
    base_dem = tile.dem
    inset = DEM.DEM(
        tile.lat, tile.lon, inset_path, fill_nodata=False, info_only=False
    )
    if inset.alt_dem is None:
        return

    number_of_columns = base_dem.nxdem
    number_of_rows = base_dem.nydem
    x0 = base_dem.x0
    x1 = base_dem.x1
    y0 = base_dem.y0
    y1 = base_dem.y1
    # Cell-centre coordinates (tile-relative degrees), matching the sampling
    # convention in O4_DEM_Utils.DEM.alt_nostrict.
    x_step = (x1 - x0) / (number_of_columns - 1)
    y_step = (y1 - y0) / (number_of_rows - 1)

    # Working-grid column/row window that overlaps the inset extent.
    column_min = max(int(numpy.floor((inset.x0 - x0) / x_step)), 0)
    column_max = min(
        int(numpy.ceil((inset.x1 - x0) / x_step)), number_of_columns - 1
    )
    # Row 0 is the northern edge (y == y1): row grows as y decreases.
    row_min = max(int(numpy.floor((y1 - inset.y1) / y_step)), 0)
    row_max = min(int(numpy.ceil((y1 - inset.y0) / y_step)), number_of_rows - 1)
    if column_min > column_max or row_min > row_max:
        return

    columns = numpy.arange(column_min, column_max + 1)
    rows = numpy.arange(row_min, row_max + 1)
    x_coordinates = x0 + columns * x_step
    y_coordinates = y1 - rows * y_step
    mesh_x, mesh_y = numpy.meshgrid(x_coordinates, y_coordinates)
    query = numpy.column_stack(
        (mesh_x.ravel(), mesh_y.ravel())
    )

    inset_values = inset.alt_vec_strict(query).reshape(mesh_x.shape)
    valid = inset_values != inset.nodata

    # Distance in metres to the nearest inset-extent edge (rectangular data
    # region), converted from the per-axis degree distances.
    centre_latitude = tile.lat + (y0 + y1) / 2.0
    metres_per_degree_longitude = GEO.lon_to_m(centre_latitude)
    metres_per_degree_latitude = GEO.lat_to_m
    distance_west = (mesh_x - inset.x0) * metres_per_degree_longitude
    distance_east = (inset.x1 - mesh_x) * metres_per_degree_longitude
    distance_south = (mesh_y - inset.y0) * metres_per_degree_latitude
    distance_north = (inset.y1 - mesh_y) * metres_per_degree_latitude
    distance_to_edge = numpy.minimum(
        numpy.minimum(distance_west, distance_east),
        numpy.minimum(distance_south, distance_north),
    )
    if feather_m > 0:
        weight = numpy.clip(distance_to_edge / feather_m, 0.0, 1.0)
    else:
        weight = (distance_to_edge >= 0).astype(numpy.float32)
    weight = numpy.where(valid, weight, 0.0)

    window = base_dem.alt_dem[
        row_min : row_max + 1, column_min : column_max + 1
    ]
    # Datum sanity: median base-vs-inset offset across the feather ring.
    ring = (weight > 0) & (weight < 1) & valid
    if numpy.any(ring):
        offset = float(
            numpy.median(inset_values[ring] - window[ring])
        )
        if abs(offset) > 3.0:
            UI.vprint(
                1,
                "   WARNING: elevation inset",
                os.path.basename(inset_path),
                "differs from the base DEM by a median",
                round(offset, 2),
                "m over the feather ring (>3 m; check vertical datum).",
            )
    blended = weight * inset_values + (1.0 - weight) * window
    base_dem.alt_dem[
        row_min : row_max + 1, column_min : column_max + 1
    ] = blended.astype(base_dem.alt_dem.dtype)
