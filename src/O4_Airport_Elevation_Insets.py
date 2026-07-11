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
     step 1 just before ``write_to_file``, so both steps see one raster,
     and again on step 2's ITERATIVE-refinement branch, which rewrites the
     ``.alt`` from the ``tile.iterate``-th user sub-DEM (that load keeps
     nodata, so the bake takes the inset outright over base nodata cells
     instead of blending against the sentinel).

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
        # Base-tier (role=base) fields, spec section 3.6.
        if "resolution_arc_seconds" in definition:
            definition["resolution_arc_seconds"] = _parse_float(
                definition.get("resolution_arc_seconds"), default=None
            )
        if "dem1_zones" in definition:
            definition["dem1_zones"] = frozenset(
                token.strip()
                for token in str(definition["dem1_zones"]).split(",")
                if token.strip()
            )
        if "exclude_tiles" in definition:
            definition["exclude_tiles"] = _parse_tile_list(
                definition["exclude_tiles"]
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


def _parse_tile_list(value):
    """Parse ``lat,lon;lat,lon;...`` into a tuple of integer tile corners."""
    tiles = []
    for pair in str(value).split(";"):
        parts = pair.split(",")
        if len(parts) != 2:
            continue
        try:
            tiles.append((int(parts[0].strip()), int(parts[1].strip())))
        except ValueError:
            continue
    return tuple(tiles)


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
# Shared fetch helpers (strategy-agnostic; reused by tnm_cog and stac)
# =====================================================================
def warp_vsicurl_sources_to_geotiff(
    vsicurl_inputs, bounding_box_wgs84, target_resolution_m, destination_path
):
    """Mosaic + warp remote rasters to an EPSG:4326 float32 GeoTIFF window.

    The genuinely shared core of every Cloud-Optimized GeoTIFF strategy:
    ``gdal.Warp`` reads only the requested window from each ``/vsicurl/``
    source (the full source tiles, hundreds of megabytes each, are never
    downloaded), mosaics them (later inputs win on overlap), reprojects to
    EPSG:4326 and resamples to ``target_resolution_m`` at the bounding
    box's centre latitude.  Returns ``True`` on success, ``False`` on any
    GDAL failure (the caller records no-coverage).  A no-op returning
    ``False`` when GDAL is unavailable.
    """
    if not has_gdal:
        return False
    (west, south, east, north) = bounding_box_wgs84
    centre_latitude = (south + north) / 2.0
    metres_per_degree_latitude = GEO.lat_to_m
    metres_per_degree_longitude = GEO.lon_to_m(centre_latitude)
    x_resolution_deg = target_resolution_m / metres_per_degree_longitude
    y_resolution_deg = target_resolution_m / metres_per_degree_latitude
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
            destination_path, list(vsicurl_inputs), options=warp_options
        )
    except Exception as error:
        UI.vprint(1, "   WARNING: elevation warp failed:", str(error))
        return False
    if dataset is None:
        return False
    dataset = None  # flush to disk
    return True


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

        if not warp_vsicurl_sources_to_geotiff(
            vsicurl_inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
        ):
            return None

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
# Strategy 2: stac (SpatioTemporal Asset Catalog search -> COG -> warp)
# =====================================================================
# The extensibility proof (spec Phase C2): a whole new provider family --
# a STAC API search endpoint serving Cloud-Optimized GeoTIFF assets --
# plugs into the SAME orchestration (discovery loop, cache, index,
# provenance, composite assembly, bake, and the Phase C1 grid decision)
# with only this one class + one Providers/Elevation/*.elv definition,
# reusing warp_vsicurl_sources_to_geotiff for the fetch core.  Shipped
# with HRDEM.elv (Natural Resources Canada high-resolution lidar).
def _select_stac_dtm_assets(items, prefer_asset_keys):
    """Pick one Cloud-Optimized GeoTIFF DTM asset href from each STAC item.

    STAC items expose named assets; elevation collections publish a Digital
    Terrain Model (bare earth) and often a Digital Surface Model (canopy /
    buildings) too.  We prefer the DTM: an asset whose key matches one of
    ``prefer_asset_keys`` (in order) wins; otherwise the first asset whose
    key or roles suggest a DTM; otherwise the first GeoTIFF-typed asset.
    Returns a list of ``(href, native_resolution_m_or_None)`` for the
    chosen assets, skipping items with no usable asset.
    """
    chosen = []
    for item in items:
        assets = item.get("assets") or {}
        properties = item.get("properties") or {}
        href = None
        # 1. Explicit preference order (e.g. "dtm", "dtm-1m").
        for key in prefer_asset_keys:
            if key in assets and assets[key].get("href"):
                href = assets[key]["href"]
                break
        # 2. Any asset that looks like a DTM by key or declared role.
        if href is None:
            for key, asset in assets.items():
                roles = [str(role).lower() for role in asset.get("roles", [])]
                if (
                    "dtm" in key.lower()
                    or "data" in roles
                    and "dtm" in " ".join(roles)
                ) and asset.get("href"):
                    href = asset["href"]
                    break
        # 3. Fall back to the first GeoTIFF-typed asset.
        if href is None:
            for asset in assets.values():
                media_type = str(asset.get("type", "")).lower()
                if ("tiff" in media_type or "geotiff" in media_type) and asset.get(
                    "href"
                ):
                    href = asset["href"]
                    break
        if href is None:
            continue
        resolution = (
            properties.get("gsd")
            or properties.get("resolution")
            or None
        )
        chosen.append((href, resolution))
    return chosen


def _stac_asset_href_to_vsicurl(href):
    """Turn a STAC asset href into a GDAL virtual path for a window read.

    ``https://`` / ``http://`` hrefs become ``/vsicurl/<url>``; an ``s3://``
    href becomes ``/vsis3/<bucket/key>``; an already-virtual path is left
    untouched.  Only the requested window is read regardless.
    """
    if href.startswith("/vsi"):
        return href
    if href.startswith("s3://"):
        return "/vsis3/" + href[len("s3://") :]
    return "/vsicurl/" + href


@register_access_strategy("stac")
class StacCloudOptimizedGeoTiffStrategy:
    """Fetch lidar elevation via a STAC API search + Cloud-Optimized GeoTIFF.

    Discovery POSTs (falling back to GET) a bounding-box + collections
    query to the STAC ``/search`` endpoint named by the definition's
    ``discovery_url_template`` and returns the intersecting items.  Fetch
    selects the highest-resolution Digital Terrain Model asset of each
    item, mosaics their Cloud-Optimized GeoTIFFs through GDAL's virtual
    file system (window reads only) and warps to EPSG:4326 at the target
    resolution -- reusing warp_vsicurl_sources_to_geotiff, exactly like
    tnm_cog, with zero change to the orchestration around it.
    """

    def _search_url_and_body(self, definition, bounding_box_wgs84):
        (west, south, east, north) = bounding_box_wgs84
        template = definition.get("discovery_url_template", "")
        # The endpoint may be a bare .../search URL or one already carrying
        # ?collections=...; keep any query the definition supplied.
        url = template
        collections = [
            token.strip()
            for token in str(definition.get("collections", "")).split(",")
            if token.strip()
        ]
        body = {
            "bbox": [west, south, east, north],
            "limit": int(float(definition.get("search_limit", 50))),
        }
        if collections:
            body["collections"] = collections
        return (url, body, collections, (west, south, east, north))

    def discover(self, definition, bounding_box_wgs84):
        import requests

        (url, body, collections, bbox) = self._search_url_and_body(
            definition, bounding_box_wgs84
        )
        if not url:
            return None
        payload = None
        try:
            response = requests.post(url, json=body, timeout=30)
            if response.status_code == 200:
                payload = response.json()
        except Exception as error:
            UI.vprint(
                1, "   WARNING: STAC POST search failed:", str(error)
            )
        if payload is None:
            # Fall back to a GET query-string search (some STAC servers).
            (west, south, east, north) = bbox
            get_url = url + (
                ("&" if "?" in url else "?")
                + "bbox="
                + ",".join(repr(value) for value in (west, south, east, north))
            )
            if collections:
                get_url = get_url + "&collections=" + ",".join(collections)
            try:
                response = requests.get(get_url, timeout=30)
                if response.status_code != 200:
                    UI.vprint(
                        1,
                        "   WARNING: STAC GET search returned status",
                        response.status_code,
                    )
                    return None
                payload = response.json()
            except Exception as error:
                UI.vprint(
                    1, "   WARNING: STAC GET search failed:", str(error)
                )
                return None
        return self._parse_search_payload(payload)

    @staticmethod
    def _parse_search_payload(payload):
        """Extract the item list from a STAC ItemCollection response."""
        if not isinstance(payload, dict):
            return None
        features = payload.get("features")
        if features is None and "items" in payload:
            features = payload.get("items")
        if not features:
            return None
        return list(features)

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        if not has_gdal:
            return None
        items = self.discover(definition, bounding_box_wgs84)
        if not items:
            return None
        prefer_asset_keys = [
            token.strip()
            for token in str(
                definition.get("dtm_asset_keys", "dtm")
            ).split(",")
            if token.strip()
        ]
        selected = _select_stac_dtm_assets(items, prefer_asset_keys)
        if not selected:
            return None
        # Highest resolution first so the finest asset WINS on overlap
        # (gdal.Warp lets later inputs win, so sort coarsest-to-finest).
        selected.sort(
            key=lambda pair: (pair[1] is None, -(pair[1] or 0.0))
        )
        vsicurl_inputs = [
            _stac_asset_href_to_vsicurl(href) for (href, _resolution) in selected
        ]
        if not warp_vsicurl_sources_to_geotiff(
            vsicurl_inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
        ):
            return None
        native_resolutions = [
            resolution for (_href, resolution) in selected if resolution
        ]
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [href for (href, _resolution) in selected],
            "source_ids": [
                item.get("id") for item in items if item.get("id")
            ],
            "collections": [
                token.strip()
                for token in str(definition.get("collections", "")).split(",")
                if token.strip()
            ],
            "native_resolution_m": (
                min(native_resolutions)
                if native_resolutions
                else definition.get("native_resolution_m")
            ),
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
                _store_acceptance_probes_in_record(airport_record, destination)
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
            _store_acceptance_probes_in_record(
                airport_record, destination, refresh=refresh
            )
            break
        index[icao] = airport_record
    _write_index(lat, lon, index)
    return index


def _store_acceptance_probes_in_record(airport_record, inset_path, refresh=False):
    """Record the Phase C1 acceptance probes for an inset in its index entry.

    Stores ``[[latitude, longitude], ...]`` under ``"probes"`` so the
    working-grid decision is transparent and inspectable per airport (spec
    section 4, C1: "store the probe list with the tile's inset index").
    Computed once and cached in the index; ``refresh`` recomputes.  A no-op
    without GDAL or when the probes cannot be derived.
    """
    if "probes" in airport_record and not refresh:
        return
    try:
        probes = acceptance_probes_for_inset(inset_path)
    except Exception:
        return
    if probes:
        airport_record["probes"] = [
            [float(latitude), float(longitude)]
            for (latitude, longitude) in probes
        ]


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
    base_nodata = window == base_dem.nodata
    # Datum sanity: median base-vs-inset offset across the feather ring
    # (base nodata cells carry the sentinel, not terrain -- exclude them).
    ring = (weight > 0) & (weight < 1) & valid & ~base_nodata
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
    # Where the base holds its nodata sentinel (possible on the step-2
    # iterative-refinement path, which loads with fill_nodata=False),
    # blending against the sentinel would fabricate huge negative ramps:
    # take the inset outright where it has data, keep the sentinel where
    # neither has data.
    if numpy.any(base_nodata):
        blended = numpy.where(
            base_nodata,
            numpy.where(valid, inset_values, window),
            blended,
        )
    base_dem.alt_dem[
        row_min : row_max + 1, column_min : column_max + 1
    ] = blended.astype(base_dem.alt_dem.dtype)


# =====================================================================
# Automatic per-airport smoothing radius (spec section 3.4)
# =====================================================================
# The airport smoothing blur exists to hide the pixel staircase of the
# elevation SOURCE, so its radius should scale with the source's pixel
# size, not sit fixed at apt_smoothing_pix working-grid pixels (a fixed
# 8-pixel tent blur is ~250 m and was measured to erase engineered
# relief -- the KBNA taxiway M plateau dropped 9.5 m).  The rule, in
# metres so the mask upscaling step never changes the physical footprint:
#
#   blur_radius_m    = apt_smoothing_pix * source_pixel_m,
#                      capped at apt_smoothing_pix * working_pixel_m
#   radius_pixels(a) = round(blur_radius_m / working_pixel_m)
#                    = min(apt_smoothing_pix,
#                          round(apt_smoothing_pix
#                                * source_pixel_m / working_pixel_m))
#
# where source_pixel_m is the finest cached inset pixel when insets cover
# at least INSET_COVERAGE_THRESHOLD of the airport's smoothing mask, else
# the base source's TRUE pixel size capped at the working pixel.  The
# base loader either reads a source at its native grid or UPSAMPLES
# coarser data onto the working grid, so no base source is ever finer
# than the working grid: the cap makes the base path's ratio exactly 1
# and the radius exactly apt_smoothing_pix -- identical to today (goal
# G3).  Consequences: 30 m-class base -> apt_smoothing_pix unchanged;
# a 10 m source -> 3 pixels (of 8); 3 m inset -> 1; 1 m inset -> 0.

INSET_COVERAGE_THRESHOLD = 0.8


def smoothing_radius_pixels_for_source(
    apt_smoothing_pix, source_pixel_m, working_pixel_m, reference_pixel_m=None
):
    """The spec section 3.4 radius rule (pure arithmetic).

    Half-up rounding (not banker's) so the boundary cases are
    deterministic and monotone in ``source_pixel_m``; floored at 0 (no
    blur).

    The radius expresses a PHYSICAL blur footprint of
    ``apt_smoothing_pix * min(source_pixel_m, reference_pixel_m)`` metres,
    divided by the working-grid pixel to yield a pixel count.
    ``reference_pixel_m`` is the 1 arc-second (~30.9 m) pixel that the
    historic ``apt_smoothing_pix`` was expressed in; it defaults to
    ``working_pixel_m`` so the non-densified path is byte-identical to
    before.  On the Phase C densified path the caller passes the true
    1 arc-second pixel so that halving the grid does not silently halve
    the physical smoothing footprint (the section 3.4 "radius in metres"
    principle).  ``min(source_pixel_m, reference_pixel_m)`` also supplies
    the historic "never exceed today" cap: no base source is finer than
    the reference pixel, so a coarse source yields exactly
    ``apt_smoothing_pix`` reference-pixels of blur.
    """
    if apt_smoothing_pix <= 0 or working_pixel_m <= 0:
        return max(int(apt_smoothing_pix), 0)
    if reference_pixel_m is None:
        reference_pixel_m = working_pixel_m
    effective_source_pixel_m = min(source_pixel_m, reference_pixel_m)
    scaled = apt_smoothing_pix * effective_source_pixel_m / working_pixel_m
    return int(scaled + 0.5)


def inset_coverage_of_airport_mask(tile, mask_geometry):
    """Coverage of an airport's smoothing mask by the cached insets.

    Returns ``(coverage_fraction, finest_intersecting_inset_pixel_m)``.
    Coverage is judged by the insets' raster EXTENTS (rectangles in
    tile-relative degrees) -- interior nodata is not subtracted, which
    matches how the bake applies them (nodata cells fall back to base).
    ``(0.0, None)`` when no cached inset touches the mask.
    """
    if (
        not has_gdal
        or mask_geometry is None
        or mask_geometry.is_empty
        or mask_geometry.area == 0
    ):
        return (0.0, None)
    from shapely import geometry as shapely_geometry
    from shapely import ops as shapely_ops

    provider_definitions = select_provider_definitions(
        getattr(tile, "airport_elevation_providers", "auto")
    )
    codes = [definition["code"] for definition in provider_definitions]
    inset_paths = list_cached_inset_dems(
        tile.lat, tile.lon, provider_codes=codes or None
    )
    boxes = []
    finest_pixel_m = None
    for inset_path in inset_paths:
        try:
            dataset = gdal.Open(inset_path)
            geotransform = dataset.GetGeoTransform()
            columns = dataset.RasterXSize
            rows = dataset.RasterYSize
        except Exception:
            continue
        west = geotransform[0]
        north = geotransform[3]
        east = west + columns * geotransform[1]
        south = north + rows * geotransform[5]
        extent_box = shapely_geometry.box(
            west - tile.lon, south - tile.lat, east - tile.lon, north - tile.lat
        )
        if not extent_box.intersects(mask_geometry):
            continue
        boxes.append(extent_box)
        pixel_m = abs(geotransform[5]) * GEO.lat_to_m
        finest_pixel_m = (
            pixel_m
            if finest_pixel_m is None
            else min(finest_pixel_m, pixel_m)
        )
    if not boxes:
        return (0.0, None)
    covered_area = (
        shapely_ops.unary_union(boxes).intersection(mask_geometry).area
    )
    return (covered_area / mask_geometry.area, finest_pixel_m)


def resolve_airport_smoothing_radius(
    tile, airport_record, working_pixel_m, mask_geometry=None,
    reference_pixel_m=None,
):
    """Resolve the smoothing radius (in working-grid pixels) for one airport.

    Returns ``(radius_pixels, source_pixel_m, coverage_fraction)``; the
    last two are ``None`` whenever the LEGACY fixed radius applies (so a
    caller can log only the automatic decisions).  Precedence:

    1. The per-airport ``smoothing_pix`` apt.dat/config override always
       wins (unchanged from the historic behaviour).  An unparseable
       override falls through to the rules below (historically it fell to
       the tile default; with the automatic gate off that is still exactly
       what happens).
    2. ``apt_smoothing_auto`` off, insets gated off, or GDAL absent ->
       the fixed ``tile.apt_smoothing_pix``.
    3. Otherwise the section 3.4 rule above.

    ``reference_pixel_m`` is the physical size of one 1 arc-second working
    pixel (~30.9 m).  On the Phase C densified path it differs from
    ``working_pixel_m`` (the dense pixel) so the physical blur footprint
    is preserved across densification; when omitted it defaults to
    ``working_pixel_m`` and the behaviour is byte-identical to before.
    Note the explicit ``smoothing_pix`` override stays a PIXEL count of the
    working grid (its historic meaning), so densifying scales its physical
    footprint -- an override is a deliberate manual value and is left
    literal.
    """
    if "smoothing_pix" in airport_record:
        try:
            return (int(airport_record["smoothing_pix"]), None, None)
        except (TypeError, ValueError):
            pass
    default_radius = tile.apt_smoothing_pix
    if not getattr(tile, "apt_smoothing_auto", False):
        return (default_radius, None, None)
    if not getattr(tile, "airport_elevation_insets", False) or not has_gdal:
        return (default_radius, None, None)
    (coverage_fraction, finest_pixel_m) = inset_coverage_of_airport_mask(
        tile, mask_geometry
    )
    if reference_pixel_m is None:
        reference_pixel_m = working_pixel_m
    if coverage_fraction >= INSET_COVERAGE_THRESHOLD and finest_pixel_m:
        source_pixel_m = finest_pixel_m
    else:
        # Base source: TRUE pixel capped at the reference pixel (see the
        # section comment -- the cap makes this the reference pixel, and
        # the radius identical to today on the non-densified path).
        source_pixel_m = reference_pixel_m
    radius_pixels = smoothing_radius_pixels_for_source(
        default_radius, source_pixel_m, working_pixel_m, reference_pixel_m
    )
    return (radius_pixels, source_pixel_m, coverage_fraction)


# =====================================================================
# Densified working grid over inset tiles (spec section 4, Phase C1)
# =====================================================================
# The Phase B acceptance proved the last KBNA residual is the GRID, not
# the bake: Triangle4XP cannot refine a mesh below one working pixel
# (Utils/src/Triangle4XP.c:7297), so a meter-class scarp captured in a
# 3 m inset is still resolved to +/-1.6 m when the working grid posts at
# ~30.9 m (1 arc-second).  When any airport inset is cached for the tile,
# the combined working raster (and the .alt the mesher reads) is built on
# a denser grid: the base is upsampled bilinearly to the target posting
# and the insets are baked at that denser posting, so their relief
# survives to the mesh.  No-inset tiles keep the 1 arc-second grid and are
# byte-identical to before.
#
# The target spacing is chosen BEFORE any tile build with a cheap numpy
# check on the cached inset GeoTIFF (no mesh, no Triangle4XP): for a small
# set of acceptance PROBES, the "ideal bake" value the probe would read
# from a working raster at a candidate grid is modelled and compared to
# the inset's own bilinear value there.  We pick the COARSEST candidate of
# {1/2, 1/3} arc-second whose worst-probe error stays within
# WORKING_GRID_IDEAL_TOLERANCE_M, so we never pay for more grid (bytes,
# memory, mesh time) than the data actually needs.

# Candidate densification FACTORS relative to the 1 arc-second base grid
# (factor f => spacing 1/f arc-second => (n-1)*f + 1 samples).  The
# candidate set is the coarsest-first {1/2, 1/3} arc-second of the spec.
WORKING_GRID_CANDIDATE_FACTORS = (2, 3)

# Worst-probe ideal-bake tolerance for the automatic grid decision.
# Deliberately tighter than the +/-1.5 m mesh acceptance so the modelled
# .alt error leaves headroom for the mesh-floor gap the model omits.
WORKING_GRID_IDEAL_TOLERANCE_M = 1.0

# Number of steepest-gradient probes derived per inset for tiles/airports
# without a hand-seeded probe list.
DERIVED_PROBE_COUNT = 8

# Hand-seeded acceptance probes keyed by ICAO (spec section 5 seed set).
# Each probe is (latitude, longitude) in EPSG:4326 degrees.  Airports not
# listed here derive probes generically from the steepest-gradient cells
# of their inset footprint (see derive_acceptance_probes).
SEED_ACCEPTANCE_PROBES = {
    "KBNA": (
        (36.1374844, -86.6760939),  # 45 m gantry south-west foot
        (36.1376421, -86.6759065),  # gantry anchor
        (36.1377853, -86.6757619),  # 45 m gantry north-east foot
        (36.13715, -86.67650),      # taxiway M plateau
    ),
}


def parse_working_grid_arc_seconds(value):
    """Parse the ``working_grid_arc_seconds`` config into a decision.

    Returns ``"auto"`` for the automatic rule, or an integer densification
    FACTOR (1, 2 or 3) for an explicit pin.  Accepts ``"1"``, ``"1/2"``,
    ``"0.5"``, ``"1/3"`` and friends; an unrecognised value falls back to
    ``"auto"`` (conservative -- the automatic rule keeps 1 arc-second when
    no inset covers the tile).
    """
    text = str(value or "auto").strip().lower()
    if text == "auto":
        return "auto"
    if text in ("1", "1/1", "1.0", "1\"", "1''"):
        return 1
    if text in ("1/2", "0.5", ".5", "2"):
        return 2
    if text in ("1/3", "3"):
        return 3
    # A bare fraction "a/b" -> round(b/a) as the factor (1 arc-second / n).
    if "/" in text:
        try:
            numerator, denominator = text.split("/", 1)
            arc_seconds = float(numerator) / float(denominator)
            if arc_seconds > 0:
                return max(1, min(3, int(round(1.0 / arc_seconds))))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return "auto"


def _inset_icao_from_path(inset_path):
    """The ICAO/airport key encoded in a cached inset file name.

    Cache files are ``<airport>_<code>.tif`` (the code lower-cased); strip
    the trailing ``_<code>`` to recover the airport key used for probe
    seeding.  Returns the whole stem if there is no underscore.
    """
    stem = os.path.splitext(os.path.basename(inset_path))[0]
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


def _open_inset_array(inset_path):
    """Read a cached inset GeoTIFF into ``(array, geotransform, nodata)``.

    Returns ``(None, None, None)`` when GDAL is unavailable or the file
    cannot be read -- callers treat that inset as contributing no probes /
    no error (the grid decision then rests on the other insets, or falls
    back to the finest candidate).
    """
    if not has_gdal:
        return (None, None, None)
    try:
        dataset = gdal.Open(inset_path)
        band = dataset.GetRasterBand(1)
        array = band.ReadAsArray().astype(numpy.float64)
        geotransform = dataset.GetGeoTransform()
        nodata = band.GetNoDataValue()
    except Exception:
        return (None, None, None)
    return (array, geotransform, nodata)


def _bilinear_sample_raster(array, geotransform, longitudes, latitudes):
    """Bilinearly sample a north-up GeoTIFF at arrays of lon/lat points.

    ``geotransform`` is the standard GDAL 6-tuple; pixel (0, 0) covers the
    top-left corner and its CENTRE sits at ``(west + 0.5 dx, north + 0.5
    dy)``.  Samples are clamped to the valid interior so edge points read
    the nearest in-bounds bilinear cell rather than raising.
    """
    west = geotransform[0]
    north = geotransform[3]
    pixel_width = geotransform[1]
    pixel_height = geotransform[5]  # negative for north-up
    rows, columns = array.shape
    fractional_x = (numpy.asarray(longitudes) - (west + 0.5 * pixel_width)) / pixel_width
    fractional_y = (numpy.asarray(latitudes) - (north + 0.5 * pixel_height)) / pixel_height
    column0 = numpy.clip(numpy.floor(fractional_x).astype(int), 0, columns - 2)
    row0 = numpy.clip(numpy.floor(fractional_y).astype(int), 0, rows - 2)
    tx = numpy.clip(fractional_x - column0, 0.0, 1.0)
    ty = numpy.clip(fractional_y - row0, 0.0, 1.0)
    top_left = array[row0, column0]
    top_right = array[row0, column0 + 1]
    bottom_left = array[row0 + 1, column0]
    bottom_right = array[row0 + 1, column0 + 1]
    return (
        top_left * (1 - tx) * (1 - ty)
        + top_right * tx * (1 - ty)
        + bottom_left * (1 - tx) * ty
        + bottom_right * tx * ty
    )


# The generic probe derivation targets TERRAIN-SCALE relief -- engineered
# embankments, plateaus and shelves a few metres tall over tens of metres,
# the class the KBNA seed probes represent -- NOT pixel-scale vertical
# discontinuities (building walls, trees) that NO working grid can resolve
# to +/-1 m and that would otherwise force every tile to the finest grid.
# So the gradient is computed on the inset BLOCK-AVERAGED to roughly the
# 1 arc-second working-pixel scale, where a resolvable embankment shows a
# strong slope and a vertical wall averages out.
PROBE_TERRAIN_SCALE_M = 30.0


def derive_acceptance_probes(inset_path, count=DERIVED_PROBE_COUNT):
    """Generic acceptance probes: the steepest TERRAIN-SCALE cells of an inset.

    Airports without a hand-seeded probe list (every non-KBNA tile) still
    need a sensible grid decision, so we probe where the inset's
    terrain-scale relief is steepest -- exactly the engineered embankments
    a coarse working grid smears worst, and the cells densifying actually
    helps.  The inset is block-averaged to roughly the working-pixel scale
    (:data:`PROBE_TERRAIN_SCALE_M`) before the gradient is taken, so
    unresolvable pixel-scale walls do not dominate.  The ``count`` strongest
    coarse cells, spread at least a tenth of the footprint apart, are
    returned as ``(latitude, longitude)`` cell-centre pairs.
    """
    (array, geotransform, nodata) = _open_inset_array(inset_path)
    if array is None:
        return []
    pixel_height_m = abs(geotransform[5]) * GEO.lat_to_m
    block = max(1, int(round(PROBE_TERRAIN_SCALE_M / max(pixel_height_m, 1e-6))))
    rows, columns = array.shape
    valid = numpy.ones(array.shape, dtype=bool)
    if nodata is not None:
        valid &= array != nodata
    # Block-mean the inset (ignoring nodata) to ~working-pixel posting.
    coarse_rows = rows // block
    coarse_columns = columns // block
    if coarse_rows < 3 or coarse_columns < 3:
        block = 1
        coarse_rows, coarse_columns = rows, columns
        coarse = numpy.where(valid, array, numpy.nan)
    else:
        trimmed = array[: coarse_rows * block, : coarse_columns * block]
        trimmed_valid = valid[: coarse_rows * block, : coarse_columns * block]
        blocks = trimmed.reshape(
            coarse_rows, block, coarse_columns, block
        )
        blocks_valid = trimmed_valid.reshape(
            coarse_rows, block, coarse_columns, block
        )
        with numpy.errstate(invalid="ignore"):
            summed = numpy.where(blocks_valid, blocks, 0.0).sum(axis=(1, 3))
            counted = blocks_valid.sum(axis=(1, 3))
            coarse = numpy.where(counted > 0, summed / numpy.maximum(counted, 1), numpy.nan)
    gradient_y, gradient_x = numpy.gradient(numpy.nan_to_num(coarse, nan=0.0))
    magnitude = numpy.hypot(gradient_x, gradient_y)
    magnitude[numpy.isnan(coarse)] = -1.0
    minimum_separation = max(1, int(0.1 * min(coarse_rows, coarse_columns)))
    order = numpy.argsort(magnitude, axis=None)[::-1]
    west = geotransform[0]
    north = geotransform[3]
    pixel_width = geotransform[1]
    pixel_height = geotransform[5]
    chosen_cells = []
    probes = []
    for flat_index in order:
        if len(probes) >= count:
            break
        coarse_row = int(flat_index // coarse_columns)
        coarse_column = int(flat_index % coarse_columns)
        if magnitude[coarse_row, coarse_column] < 0:
            break
        too_close = any(
            abs(coarse_row - r) < minimum_separation
            and abs(coarse_column - c) < minimum_separation
            for (r, c) in chosen_cells
        )
        if too_close:
            continue
        chosen_cells.append((coarse_row, coarse_column))
        # Cell centre of the coarse block, back in inset pixel coordinates.
        column = (coarse_column + 0.5) * block
        row = (coarse_row + 0.5) * block
        longitude = west + column * pixel_width
        latitude = north + row * pixel_height
        probes.append((latitude, longitude))
    return probes


def acceptance_probes_with_source(inset_path):
    """The acceptance probes for one inset plus whether they are seeded.

    Returns ``(probes, is_seeded)``.  A hand-seeded ICAO probe set (spec
    section 5) is used when the file's airport key matches AND the probes
    fall inside the inset footprint (``is_seeded=True``); otherwise probes
    are derived from the steepest terrain-scale cells (``is_seeded=False``).
    Seeded probes are the airport's acceptance requirement and always drive
    the grid decision; derived probes drive it only where densification can
    actually bring them within tolerance (see resolve_working_grid_factor).
    """
    icao = _inset_icao_from_path(inset_path)
    seeded = SEED_ACCEPTANCE_PROBES.get(icao)
    if seeded:
        (array, geotransform, nodata) = _open_inset_array(inset_path)
        if array is not None:
            rows, columns = array.shape
            west = geotransform[0]
            north = geotransform[3]
            east = west + columns * geotransform[1]
            south = north + rows * geotransform[5]
            inside = [
                (latitude, longitude)
                for (latitude, longitude) in seeded
                if west <= longitude <= east and south <= latitude <= north
            ]
            if inside:
                return (inside, True)
    return (derive_acceptance_probes(inset_path), False)


def acceptance_probes_for_inset(inset_path):
    """The acceptance probes for one cached inset (seeded or derived)."""
    return acceptance_probes_with_source(inset_path)[0]


def ideal_bake_errors_per_probe(inset_path, probes, factor, base_geometry):
    """Modelled .alt error of an inset baked at a densified grid, per probe.

    For each probe, ``truth`` is the inset's own bilinear value there.
    ``built`` models the value the probe would read from a working raster
    posting at ``factor`` x the base grid: each surrounding working-grid
    NODE takes the inset's bilinear value, and the probe is interpolated
    across the cell with the SAME two-triangle split the pipeline's
    ``DEM.alt_nostrict`` (and hence the built mesh) uses -- so the number
    is the grid-quantisation error the mesh will actually carry, not an
    optimistic full-bilinear estimate.  Returns a list of ``|built -
    truth|`` aligned with ``probes`` (empty when the inset is unreadable).

    ``base_geometry`` is ``(x0, x1, y0, y1, nxdem, nydem)`` of the base
    working grid in tile-relative degrees; the densified node spacing is
    that grid refined by ``factor``.  Probes carry both tile-relative and
    absolute coordinates (the geometry math and the inset sampling each get
    the frame they need -- see resolve_working_grid_factor).
    """
    (array, geotransform, nodata) = _open_inset_array(inset_path)
    if array is None or not probes:
        return []
    (x0, x1, y0, y1, nxdem, nydem) = base_geometry
    dense_columns = (nxdem - 1) * factor + 1
    dense_rows = (nydem - 1) * factor + 1
    x_step = (x1 - x0) / (dense_columns - 1)
    y_step = (y1 - y0) / (dense_rows - 1)

    errors = []
    for (relative_x, relative_y, longitude, latitude) in probes:
        truth = float(
            _bilinear_sample_raster(
                array, geotransform, [longitude], [latitude]
            )[0]
        )
        # Working-grid cell containing the probe (row grows southward).
        column_index = (relative_x - x0) / x_step
        row_index = (y1 - relative_y) / y_step
        column0 = int(numpy.floor(column_index))
        row0 = int(numpy.floor(row_index))
        rx = column_index - column0
        ry = row_index - row0

        def node_value(column, row):
            node_longitude = longitude + (
                (x0 + column * x_step) - relative_x
            )
            node_latitude = latitude + (
                (y1 - row * y_step) - relative_y
            )
            return float(
                _bilinear_sample_raster(
                    array, geotransform, [node_longitude], [node_latitude]
                )[0]
            )

        top_left = node_value(column0, row0)
        top_right = node_value(column0 + 1, row0)
        bottom_left = node_value(column0, row0 + 1)
        bottom_right = node_value(column0 + 1, row0 + 1)
        # Two-triangle split identical to DEM.alt_nostrict (rx vs ry).
        if rx >= ry:
            built = (
                (1 - rx) * top_left
                + ry * bottom_right
                + (rx - ry) * top_right
            )
        else:
            built = (
                (1 - ry) * top_left
                + rx * bottom_right
                + (ry - rx) * bottom_left
            )
        errors.append(abs(built - truth))
    return errors


def ideal_bake_error_at_probes(inset_path, probes, factor, base_geometry):
    """Worst modelled .alt error over the probes (see per-probe variant)."""
    errors = ideal_bake_errors_per_probe(
        inset_path, probes, factor, base_geometry
    )
    return max(errors) if errors else 0.0


def _base_geometry_of_dem(dem):
    """Extract ``(x0, x1, y0, y1, nxdem, nydem)`` from a loaded base DEM."""
    return (dem.x0, dem.x1, dem.y0, dem.y1, dem.nxdem, dem.nydem)


def resolve_working_grid_factor(tile, base_dem):
    """Choose the working-grid densification factor for a tile (1, 2 or 3).

    The decision is deterministic and disk-state-driven (both build steps
    call it on the same cached insets and the same base geometry), so
    steps 1 and 2 always agree on the grid.  Returns ``1`` -- the
    byte-identical 1 arc-second path -- whenever the feature is gated off,
    GDAL is missing, no inset is cached, or the config pins ``"1"``.  An
    explicit ``"1/2"`` / ``"1/3"`` pin is honoured outright.  In ``"auto"``
    mode with insets present it evaluates the ideal-bake error over every
    cached inset's acceptance probes and returns the COARSEST candidate
    factor whose worst error is within WORKING_GRID_IDEAL_TOLERANCE_M,
    falling back to the finest candidate if none qualifies.
    """
    if not insets_enabled_for_tile(tile):
        return 1
    configured = parse_working_grid_arc_seconds(
        getattr(tile, "working_grid_arc_seconds", "auto")
    )
    provider_definitions = select_provider_definitions(
        getattr(tile, "airport_elevation_providers", "auto")
    )
    codes = [definition["code"] for definition in provider_definitions]
    inset_paths = list_cached_inset_dems(
        tile.lat, tile.lon, provider_codes=codes or None
    )
    if not inset_paths:
        return 1
    if configured != "auto":
        return configured
    base_geometry = _base_geometry_of_dem(base_dem)
    finest_factor = WORKING_GRID_CANDIDATE_FACTORS[-1]
    tolerance = WORKING_GRID_IDEAL_TOLERANCE_M

    # Assemble every probe once, carrying its tile-relative and absolute
    # coordinates (the geometry math and the inset sampling each need one
    # frame) plus whether it is a hand-seeded acceptance probe.
    all_probes = []  # (inset_path, adjusted_probe, is_seeded)
    for inset_path in inset_paths:
        (probes, is_seeded) = acceptance_probes_with_source(inset_path)
        for (latitude, longitude) in probes:
            all_probes.append(
                (
                    inset_path,
                    (
                        longitude - tile.lon,
                        latitude - tile.lat,
                        longitude,
                        latitude,
                    ),
                    is_seeded,
                )
            )
    if not all_probes:
        # Insets present but no probes derivable -> take the finest grid
        # (the data is there; be safe rather than leave it on the floor).
        return finest_factor

    # A tile with a CURATED seed set (KBNA, spec section 5) is decided by
    # those probes ALONE: the seed set is the acceptance requirement for
    # the tile, and letting an arbitrary co-tile rural strip's steepest
    # slope override it would ignore the curation.  DERIVED probes are the
    # generic fallback for tiles nobody has seeded.
    if any(is_seeded for (_, _, is_seeded) in all_probes):
        all_probes = [
            probe for probe in all_probes if probe[2]  # is_seeded
        ]

    # Per-probe error at every candidate factor.  A DERIVED probe that
    # stays above tolerance even at the FINEST candidate models relief no
    # working grid resolves to +/-1 m (a natural cliff, a data spike); it
    # must NOT drive the grid finer, since no candidate would satisfy it,
    # and letting it would force every steep tile to the max grid.  Seeded
    # acceptance probes are the airport's requirement and always count.
    errors_by_factor = {
        factor: [
            errs
            for (inset_path, probes) in _group_probes_by_inset(all_probes)
            for errs in ideal_bake_errors_per_probe(
                inset_path, probes, factor, base_geometry
            )
        ]
        for factor in WORKING_GRID_CANDIDATE_FACTORS
    }
    finest_errors = errors_by_factor[finest_factor]
    seeded_flags = [is_seeded for (_, _, is_seeded) in all_probes]
    actionable = [
        seeded or (finest_error <= tolerance)
        for (seeded, finest_error) in zip(seeded_flags, finest_errors)
    ]
    if not any(actionable):
        return finest_factor
    for factor in WORKING_GRID_CANDIDATE_FACTORS:  # coarsest first
        worst = max(
            error
            for (error, keep) in zip(errors_by_factor[factor], actionable)
            if keep
        )
        if worst <= tolerance:
            UI.vprint(
                1,
                "   Airport elevation insets: working grid densified to 1/"
                + str(factor)
                + " arc-second (worst actionable ideal-bake error "
                + str(round(worst, 3))
                + " m).",
            )
            return factor
    UI.vprint(
        1,
        "   Airport elevation insets: working grid densified to the finest "
        "1/"
        + str(finest_factor)
        + " arc-second (no coarser candidate met the "
        + str(tolerance)
        + " m tolerance).",
    )
    return finest_factor


def _group_probes_by_inset(all_probes):
    """Group ``(inset_path, adjusted_probe, is_seeded)`` by inset path.

    Preserves order so the flattened per-probe error lists stay aligned
    with ``all_probes`` (both iterate insets then probes in the same
    order).
    """
    grouped = []
    for (inset_path, adjusted_probe, _is_seeded) in all_probes:
        if grouped and grouped[-1][0] == inset_path:
            grouped[-1][1].append(adjusted_probe)
        else:
            grouped.append((inset_path, [adjusted_probe]))
    return [(path, probes) for (path, probes) in grouped]


def resample_grid_by_factor(array, factor):
    """Bilinearly resample a 2-D grid to ``factor`` x its resolution.

    A new grid of ``(rows - 1) * factor + 1`` by ``(columns - 1) * factor
    + 1`` samples over the SAME extent; the four corners and every original
    node are preserved exactly (integer-factor, endpoint-anchored bilinear),
    so the densified base carries no new invented relief -- the added
    detail comes only from the insets baked at the finer posting.  Done as
    two separable 1-D passes to keep the peak memory to one intermediate.
    """
    if factor == 1:
        return array
    array = numpy.ascontiguousarray(array, dtype=numpy.float32)

    def _upsample_axis(source, axis):
        length = source.shape[axis]
        new_length = (length - 1) * factor + 1
        target_indices = numpy.arange(new_length)
        lower = numpy.minimum(target_indices // factor, length - 2)
        fraction = (target_indices - lower * factor) / float(factor)
        lower_slice = numpy.take(source, lower, axis=axis)
        upper_slice = numpy.take(source, lower + 1, axis=axis)
        shape = [1] * source.ndim
        shape[axis] = new_length
        fraction = fraction.reshape(shape).astype(numpy.float32)
        return lower_slice * (1.0 - fraction) + upper_slice * fraction

    densified = _upsample_axis(array, 1)
    densified = _upsample_axis(densified, 0)
    return densified.astype(numpy.float32)


def densify_tile_dem_for_insets(tile):
    """Densify ``tile.dem`` onto the Phase C1 working grid, in place.

    Called immediately after the base DEM is loaded in BOTH build steps.
    A no-op -- and a byte-identical build -- when the resolved factor is 1
    (feature gated off, GDAL missing, no inset cached, or the grid pinned
    to 1 arc-second).  With a factor of 2 or 3 it rewrites ``nxdem`` /
    ``nydem`` (and, for a full load, resamples ``alt_dem``) so the extent
    is unchanged but the posting is finer; the subsequent inset bake and
    ``.alt`` write then land at that finer posting, and the info-only step
    2 load sees matching dimensions for its raster-size check.  Returns the
    factor applied.
    """
    if tile.dem is None:
        return 1
    factor = resolve_working_grid_factor(tile, tile.dem)
    if factor == 1:
        return 1
    tile.dem.nxdem = (tile.dem.nxdem - 1) * factor + 1
    tile.dem.nydem = (tile.dem.nydem - 1) * factor + 1
    if tile.dem.alt_dem is not None:
        tile.dem.alt_dem = resample_grid_by_factor(tile.dem.alt_dem, factor)
    tile.working_grid_factor = factor
    return factor


# =====================================================================
# Base-tier (role=base) sources -- legacy refactor (spec section 3.6)
# =====================================================================
# The tile-wide "base" elevation sources -- historically a hardcoded
# tuple + if/elif download chain in O4_DEM_Utils.ensure_elevation -- are
# described by the same Providers/Elevation/<CODE>.elv registry, with
# role=base.  Unlike airport insets, base strategies download WHOLE-TILE
# files to the LEGACY cache paths (FNAMES.viewfinderpanorama /
# FNAMES.elevation_data), never to the airport_insets directory, so the
# on-disk cache layout is byte-identical to the historic behaviour.
#
# O4_DEM_Utils.ensure_elevation is now a thin shim over
# ensure_base_tile() below (its signature is unchanged -- the DEM loader,
# the 3x3 combined-raster assembly and the GUI keep calling it with the
# legacy short keywords).

DEFERRANTI_ALPHABET = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

# Legacy short keywords (the O4_DEM_Utils.available_sources tokens and
# hence every existing tile config) resolving onto registry codes.
# "View" is special-cased in resolve_base_definition: it picks the
# 1 arc-second archive where its zone list covers, else 3 arc-second --
# exactly the choice the legacy code made inside ensure_elevation.
LEGACY_BASE_KEYWORD_ALIASES = {
    "SRTM": "SRTM",
    "ALOS": "ALOS",
    "NED1": "NED1",
    "NED1/3": "NED13",
}


def deferranti_archive_code(lat, lon):
    """The Viewfinderpanoramas letter+number archive code for a tile.

    Exact transliteration of the legacy math (previously inline in
    O4_DEM_Utils.ensure_elevation): column number ``31 + lon // 6``
    zero-padded under 10, row letter from ``lat // 4`` (mirrored and
    prefixed ``S`` south of the equator).
    """
    deferranti_number = 31 + lon // 6
    if deferranti_number < 10:
        deferranti_number = "0" + str(deferranti_number)
    else:
        deferranti_number = str(deferranti_number)
    deferranti_letter = (
        DEFERRANTI_ALPHABET[lat // 4]
        if lat >= 0
        else DEFERRANTI_ALPHABET[(-1 - lat) // 4]
    )
    if lat < 0:
        deferranti_letter = "S" + deferranti_letter
    return deferranti_letter + deferranti_number


def usgs_seamless_tile_identifier(lat, lon):
    """The USGS staged-products tile identifier, e.g. ``n37w087``.

    Exact transliteration of the legacy construction, INCLUDING its
    operator-precedence quirk on the third line: for ``lon >= 0`` the
    conditional expression evaluates to just ``"e"``, discarding the
    north/south prefix.  The USGS national elevation datasets live at
    western longitudes so the quirk was never reachable in practice; it
    is preserved verbatim because this refactor is behaviour-preserving
    (the compatibility tests pin the western-hemisphere URLs).
    """
    tile_identifier = "n" if lat >= 0 else "s"
    tile_identifier = tile_identifier + str(abs(lat + 1)).zfill(2)
    tile_identifier = tile_identifier + "w" if lon < 0 else "e"
    tile_identifier = tile_identifier + str(abs(lon)).zfill(3)
    return tile_identifier


def _tile_centre_in_coverage(definition, lat, lon):
    """Does the tile CENTRE fall inside the definition's coverage_bbox?

    Base sources are whole-tile files, so coverage is judged at the tile
    centre (a tile straddling the coverage edge is not a safe automatic
    pick -- the un-covered part would read as nodata/zero).
    """
    coverage = definition.get("coverage_bbox")
    if not coverage:
        return True
    (west, south, east, north) = coverage
    centre_longitude = lon + 0.5
    centre_latitude = lat + 0.5
    return (
        west <= centre_longitude <= east
        and south <= centre_latitude <= north
    )


def base_definition_covers_tile(definition, lat, lon):
    """Full coverage test for a role=base definition at one tile."""
    if not _tile_centre_in_coverage(definition, lat, lon):
        return False
    if (lat, lon) in definition.get("exclude_tiles", ()):
        return False
    zones = definition.get("dem1_zones")
    if zones is not None and deferranti_archive_code(lat, lon) not in zones:
        return False
    return True


@register_access_strategy("viewfinder_zip")
class ViewfinderZipStrategy:
    """Viewfinderpanoramas (J. de Ferranti) zip archives, whole tiles.

    One archive covers several 1x1 degree tiles; download extracts every
    ``.hgt`` member to its own legacy cache path with the historic size
    guard (never overwrite a larger -- i.e. 1 arc-second -- file with a
    smaller 3 arc-second neighbour from a nearby archive).
    """

    def covers(self, definition, lat, lon):
        return base_definition_covers_tile(definition, lat, lon)

    def download_url(self, definition, lat, lon):
        return definition["download_url_template"].replace(
            "{archive_code}", deferranti_archive_code(lat, lon)
        )

    def tile_cache_path(self, definition, lat, lon):
        return FNAMES.viewfinderpanorama(lat, lon)

    def ensure_tile(self, definition, lat, lon, verbose=True):
        import io
        import zipfile

        cache_path = self.tile_cache_path(definition, lat, lon)
        if os.path.exists(cache_path):
            UI.vprint(2, "   Recycling ", cache_path)
            return 1
        UI.vprint(
            1,
            "    Downloading ",
            cache_path,
            "from Viewfinderpanoramas (J. de Ferranti).",
        )
        url = self.download_url(definition, lat, lon)
        response = DEM.http_request(
            url, definition.get("legacy_keyword", definition["code"]), verbose
        )
        if not response:
            return 0
        with zipfile.ZipFile(io.BytesIO(response.content), "r") as zip_ref:
            for zipped_file in zip_ref.filelist:
                file_name = os.path.basename(zipped_file.filename)
                if not file_name:
                    continue
                try:
                    lat0 = int(file_name[1:3])
                    lon0 = int(file_name[4:7])
                except (ValueError, IndexError):
                    UI.vprint(
                        2,
                        "      Archive contains the unknown file name",
                        file_name,
                        "which is skipped.",
                    )
                    continue
                if ("S" in file_name) or ("s" in file_name):
                    lat0 *= -1
                if ("W" in file_name) or ("w" in file_name):
                    lon0 *= -1
                out_file_name = FNAMES.viewfinderpanorama(lat0, lon0)
                # we don't wish to overwrite a 1 arc-second version by
                # downloading the whole archive of a nearby 3 arc-second one
                if (
                    not os.path.exists(out_file_name)
                    or os.path.getsize(out_file_name) <= zipped_file.file_size
                ):
                    if not os.path.isdir(os.path.dirname(out_file_name)):
                        os.makedirs(os.path.dirname(out_file_name))
                    with open(out_file_name, "wb") as out:
                        UI.vprint(2, "      Extracting", out_file_name)
                        out.write(zip_ref.open(zipped_file, "r").read())
        return 1


@register_access_strategy("usgs_seamless")
class UsgsSeamlessStrategy:
    """USGS national elevation dataset staged GeoTIFF products.

    The existing ``prd-tnm .../StagedProducts/Elevation/{1,13}/TIFF/
    current/`` whole-tile URL scheme, downloading to the legacy
    ``FNAMES.elevation_data`` cache path.
    """

    def covers(self, definition, lat, lon):
        return base_definition_covers_tile(definition, lat, lon)

    def download_url(self, definition, lat, lon):
        return (
            definition["download_url_template"]
            .replace("{dataset}", str(definition.get("usgs_dataset", "1")))
            .replace(
                "{tile_identifier}", usgs_seamless_tile_identifier(lat, lon)
            )
        )

    def tile_cache_path(self, definition, lat, lon):
        return FNAMES.elevation_data(
            definition["legacy_keyword"], lat, lon
        )

    def ensure_tile(self, definition, lat, lon, verbose=True):
        cache_path = self.tile_cache_path(definition, lat, lon)
        if os.path.exists(cache_path):
            UI.vprint(2, "   Recycling ", cache_path)
            return 1
        UI.vprint(1, "    Downloading ", cache_path, "from USGS.")
        url = self.download_url(definition, lat, lon)
        response = DEM.http_request(
            url, definition.get("legacy_keyword", definition["code"]), verbose
        )
        if not response:
            return 0
        if not os.path.isdir(os.path.dirname(cache_path)):
            os.makedirs(os.path.dirname(cache_path))
        with open(cache_path, "wb") as out:
            try:
                out.write(response.content)
            except Exception:
                return 0
        return 1


@register_access_strategy("manual_download")
class ManualDownloadStrategy:
    """Sources whose direct downloads are dead upstream (SRTM, ALOS).

    The legacy code half-supports a manual workflow: a user places the
    file at the legacy cache path by hand and the build recycles it;
    otherwise one warning line and the source yields nothing.
    """

    def covers(self, definition, lat, lon):
        return base_definition_covers_tile(definition, lat, lon)

    def download_url(self, definition, lat, lon):
        return None

    def tile_cache_path(self, definition, lat, lon):
        return FNAMES.elevation_data(
            definition["legacy_keyword"], lat, lon
        )

    def ensure_tile(self, definition, lat, lon, verbose=True):
        cache_path = self.tile_cache_path(definition, lat, lon)
        if os.path.exists(cache_path):
            UI.vprint(2, "   Recycling ", cache_path)
            return 1
        UI.vprint(
            1,
            "    WARNING : This elevation source has no longer direct downloads !"
        )
        return 0


def select_base_definitions_auto(lat, lon):
    """Rank the automatic base-source candidates for one tile.

    Enabled ``role=base`` definitions COVERING the tile, ranked by
    descending priority, CAPPED at 1 arc-second: the working mesh grid is
    3601 per degree (1 arc-second), so tile-wide data finer than that
    (e.g. the 1/3 arc-second national dataset) is wasted download and
    memory and is never auto-picked -- it stays selectable explicitly.
    A definition without a declared ``resolution_arc_seconds`` is
    excluded from auto (conservative), still selectable explicitly.
    """
    if not elevation_providers_dict:
        initialize_elevation_providers_dict()
    candidates = []
    for definition in elevation_providers_dict.values():
        if definition.get("role") != ROLE_BASE:
            continue
        if not definition.get("enabled", True):
            continue
        resolution = definition.get("resolution_arc_seconds")
        if resolution is None or resolution < 1.0:
            continue
        strategy_factory = ACCESS_STRATEGIES.get(
            definition.get("access_strategy")
        )
        if strategy_factory is None:
            continue
        if not strategy_factory().covers(definition, lat, lon):
            continue
        candidates.append(definition)
    candidates.sort(
        key=lambda definition: (
            -definition.get("priority", 0.0),
            definition["code"],
        )
    )
    return candidates


def resolve_base_definition(lat, lon, selector="auto"):
    """Resolve the base-source selector to one role=base definition.

    ``selector`` is the ``base_elevation_source`` config value, a registry
    CODE, or a legacy short keyword:

    * ``"auto"`` -- best automatic candidate (see
      :func:`select_base_definitions_auto`), or ``None`` when nothing
      covers the tile.
    * ``"View"`` -- the 1 arc-second Viewfinderpanoramas definition where
      its zone list covers this tile, else the 3 arc-second one: exactly
      the per-tile choice the legacy ensure_elevation made (including the
      Wellington exclusion, now the ``exclude_tiles`` field).
    * ``"SRTM"`` / ``"ALOS"`` / ``"NED1"`` / ``"NED1/3"`` -- direct legacy
      aliases onto their registry codes.
    * any registry CODE with ``role=base`` -- returned unconditionally
      (explicit selection bypasses both the ``enabled`` flag and the
      coverage test, matching the legacy behaviour where an explicit
      keyword always attempted its download / cache read).

    Returns ``None`` for an unknown selector.
    """
    if not elevation_providers_dict:
        initialize_elevation_providers_dict()
    selector = (selector or "auto").strip()
    if selector.lower() == "auto":
        candidates = select_base_definitions_auto(lat, lon)
        return candidates[0] if candidates else None
    if selector == "View":
        one_arc_second = elevation_providers_dict.get("VIEWFINDER1")
        three_arc_second = elevation_providers_dict.get("VIEWFINDER3")
        if (
            one_arc_second is not None
            and one_arc_second.get("enabled", True)
            and base_definition_covers_tile(one_arc_second, lat, lon)
        ):
            return one_arc_second
        return three_arc_second
    alias_code = LEGACY_BASE_KEYWORD_ALIASES.get(selector)
    if alias_code is not None:
        return elevation_providers_dict.get(alias_code)
    definition = elevation_providers_dict.get(selector)
    if definition is not None and definition.get("role") == ROLE_BASE:
        return definition
    return None


def ensure_base_tile(source, lat, lon, verbose=True):
    """Ensure the whole-tile base file for a legacy keyword or CODE.

    The target of the ``O4_DEM_Utils.ensure_elevation`` shim: resolves
    the selector, dispatches the strategy's ``ensure_tile``, and preserves
    the legacy unknown-source error line and 0/1 return convention.
    """
    definition = resolve_base_definition(lat, lon, source)
    if definition is None:
        UI.vprint(1, "   ERROR: Unknown elevation source.")
        return 0
    strategy_factory = ACCESS_STRATEGIES.get(definition.get("access_strategy"))
    if strategy_factory is None:
        UI.vprint(1, "   ERROR: Unknown elevation source.")
        return 0
    return strategy_factory().ensure_tile(definition, lat, lon, verbose)
