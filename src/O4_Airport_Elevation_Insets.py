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
ACCESS STRATEGY registered in :data:`ACCESS_STRATEGIES`.  Phase A shipped one
definition (``USGS3DEP.elv``) and one strategy (``tnm_cog``); Phase C2 added
a second family (``HRDEM.elv`` + the ``stac`` strategy) as the extensibility
proof -- one class + one definition, no orchestration change.  Adding a
future provider is a new ``.elv`` file plus, only if its fetch differs, one
new strategy class + one registry entry -- with zero changes to the
discovery/cache/composite/bake orchestration below.

Phase C1 additionally densifies the working grid over inset tiles (the
``densify_tile_dem_for_insets`` / ``resolve_working_grid_factor`` section
below): the ``.alt`` raster the mesher reads is built on a finer grid so the
meter-class inset relief survives the Triangle4XP one-working-pixel
refinement floor, chosen per tile by a cheap numpy ideal-bake check on the
cached inset before any build.

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
    from osgeo import gdal, ogr, osr

    has_gdal = True
    gdal.UseExceptions()
except Exception:
    has_gdal = False

import O4_UI_Utils as UI
import O4_File_Names as FNAMES
import O4_File_Lock as O4_File_Lock
import O4_Geo_Utils as GEO
import O4_DEM_Utils as DEM

# The .elv provider CODE is lower-cased in cache file names so the cache key
# survives access-strategy refactors (spec section 3.2).
NO_COVERAGE = "no-coverage"


class TransientFetchError(Exception):
    """A network-shaped fetch failure that may succeed on a later run.

    Raised (instead of a no-coverage answer) when a remote read dies in a
    way that says nothing about whether the provider has data: curl
    timeouts, connection failures, 5xx server responses.  The module-wide
    convention every :func:`fetch_inset` caller honours: a RAISED failure
    is never recorded as a durable no-coverage negative, while a returned
    ``None`` is.
    """


# Substrings (lower-cased) of libcurl / GDAL HTTP error messages that mean
# "the network or the server had a bad moment", not "there is no data
# here".  Matched against the stringified GDAL exception; anything else is
# treated as a durable answer as before.
_TRANSIENT_NETWORK_ERROR_FRAGMENTS = (
    # libcurl CURLE_OPERATION_TIMEDOUT ("Operation timed out after 30000
    # milliseconds with 20607784 bytes received") and connect timeouts.
    "timed out",
    "timeout was reached",
    # Connection-level failures.
    "connection reset",
    "connection was reset",
    "failed to connect",
    "could not resolve host",
    "recv failure",
    "transfer closed",
    "empty reply from server",
    # Server-side conditions worth retrying; GDAL formats these as
    # "HTTP error code : 503".
    "http error code : 5",
    "http error code: 5",
    "service unavailable",
)


def error_message_indicates_transient_network_failure(message):
    """Does an error message describe a retryable network/server failure?"""
    lowered = str(message).lower()
    return any(
        fragment in lowered
        for fragment in _TRANSIENT_NETWORK_ERROR_FRAGMENTS
    )

# Detail tier (spec section 3.6).  A definition without an explicit ``role``
# is an airport inset; ``role=base`` definitions describe tile-wide sources
# (the Phase A2 legacy refactor) and are ignored by the inset path here.
ROLE_AIRPORT_INSET = "airport_inset"
ROLE_BASE = "base"
# Coastal bathymetry (spec section 2.1).  Bathymetry providers deliver
# measured seabed depth on a LOCAL TIDAL vertical datum and are NEVER
# eligible for terrain grading (airport insets, base sources, the
# elevation_level wide-area overlay); :func:`select_bathymetry_definition`
# is their only entry point.  Every terrain-selection path filters this
# role out explicitly.
ROLE_BATHYMETRY = "bathymetry"

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
        # role=bathymetry sources whose data stops at the waterline
        # (exposed-flats lidar): visually a binary "flats" layer, so the
        # automatic paths prefer the free OpenStreetMap fallback and only
        # masks_use_DEM_too=True fetches them (spec section 4.5).
        definition["intertidal"] = _parse_boolean(
            definition.get("intertidal", "False")
        )
        # Surface-model (DSM) providers opt into the post-fetch building
        # masking pass (see mask_building_footprints_in_surface_model).
        definition[SURFACE_MODEL_BUILDING_MASKING] = _parse_boolean(
            definition.get(SURFACE_MODEL_BUILDING_MASKING, "False")
        )
        # Residual structure masking rides the same pass.  DEFAULT OFF
        # (2026-07-18 late: live SPJC regression — the airfield itself
        # fits the estimator's bite profile, a flat plateau with lower
        # coastal land in the wide window and a scarp at its edge, and
        # sea reads as exact 0.0 ground; the mask pulled the runway-mid
        # DEM from ~28 m to ~19.6 m while runways anchor to CIFP/apt.dat
        # elevations, tearing every taxiway between the two — plus
        # coastal city substituted toward sea level).  Re-enable per
        # provider with residual_structure_masking=True once the
        # airport-region protection and water exclusion are designed.
        definition[RESIDUAL_STRUCTURE_MASKING] = _parse_boolean(
            definition.get(RESIDUAL_STRUCTURE_MASKING, "False")
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


def select_bathymetry_definition(lat, lon):
    """Return the bathymetry provider serving a tile, or ``None``.

    The single entry point for coastal bathymetry (spec section 2.1): the
    highest-``priority`` ENABLED definition whose ``role`` is
    :data:`ROLE_BATHYMETRY` and whose ``coverage_bbox`` intersects the
    one-degree tile ``(lon, lat, lon + 1, lat + 1)``, or ``None`` when no
    bathymetry provider covers the tile.  Ties on priority break on the
    provider CODE so the choice is deterministic.

    Bathymetry providers are deliberately invisible to every terrain path
    (their vertical datum is local tidal); this function is the only place
    that returns them.  ``O4_Bathymetry.ensure_bathymetry_band`` calls it.
    """
    if not elevation_providers_dict:
        initialize_elevation_providers_dict()
    tile_bounding_box = (lon, lat, lon + 1, lat + 1)
    candidates = [
        definition
        for definition in elevation_providers_dict.values()
        if definition.get("enabled", True)
        and definition.get("role") == ROLE_BATHYMETRY
        and _coverage_bbox_intersects(definition, tile_bounding_box)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda definition: (
            -definition.get("priority", 0.0),
            definition["code"],
        )
    )
    return candidates[0]


def select_bathymetry_definitions(lat, lon):
    """Every bathymetry provider covering a tile, priority-sorted.

    Like :func:`select_bathymetry_definition` but returning the full
    ordered candidate list: a provider whose coverage claim is broader
    than its actual data (the Allen Coral Atlas local library covers the
    whole reef belt on paper but only holds what the user downloaded)
    must not starve the ones behind it — the band fetch walks this list
    and falls through when a provider yields nothing.
    """
    if not elevation_providers_dict:
        initialize_elevation_providers_dict()
    tile_bounding_box = (lon, lat, lon + 1, lat + 1)
    candidates = [
        definition
        for definition in elevation_providers_dict.values()
        if definition.get("enabled", True)
        and definition.get("role") == ROLE_BATHYMETRY
        and _coverage_bbox_intersects(definition, tile_bounding_box)
    ]
    candidates.sort(
        key=lambda definition: (
            -definition.get("priority", 0.0),
            definition["code"],
        )
    )
    return candidates


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
    definition,
    bounding_box_wgs84,
    target_resolution_m,
    destination_path,
    footprint_prefetch=None,
):
    """Dispatch a fetch to the strategy named by the provider definition.

    This is the strategy-agnostic seam: the orchestration (discovery loop,
    caching, index, provenance, composite assembly, bake) calls only this
    function and never mentions a concrete strategy.  A new strategy plugs
    in by registering itself; nothing here changes.

    ``footprint_prefetch`` (optional :class:`TileBuildingFootprintPrefetch`)
    is handed to the surface-model masking pass so a multi-airport tile
    shares one building-footprint extract pass instead of one per airport.

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
    provenance = strategy.fetch(
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    )
    # Surface-model providers (radar DSMs) opt into a post-fetch pass that
    # replaces building-contaminated pixels by interpolated ground; the
    # pass and its summary live with the fetch so every consumer of the
    # cached inset (composite source, bake, probes) sees corrected values.
    if provenance is not None and definition.get(
        SURFACE_MODEL_BUILDING_MASKING
    ):
        provenance[SURFACE_MODEL_BUILDING_MASKING] = (
            mask_building_footprints_in_surface_model(
                destination_path,
                bounding_box_wgs84,
                definition,
                footprint_prefetch=footprint_prefetch,
            )
        )
    return provenance


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
    vsicurl_inputs,
    bounding_box_wgs84,
    target_resolution_m,
    destination_path,
    source_srs=None,
    source_nodata=None,
    value_floor_m=-600.0,
    gdal_configuration_options=None,
):
    """Mosaic + warp remote rasters to an EPSG:4326 float32 GeoTIFF window.

    The genuinely shared core of every Cloud-Optimized GeoTIFF strategy:
    ``gdal.Warp`` reads only the requested window from each ``/vsicurl/``
    source (the full source tiles, hundreds of megabytes each, are never
    downloaded), mosaics them (later inputs win on overlap), reprojects to
    EPSG:4326 and resamples to ``target_resolution_m`` at the bounding
    box's centre latitude.  Returns ``True`` on success, ``False`` on a
    durable GDAL failure (the caller records no-coverage), and raises
    :class:`TransientFetchError` when the failure is network-shaped (a
    curl timeout, a connection failure, a 5xx) so callers can skip the
    provider WITHOUT caching a no-coverage negative.  A no-op returning
    ``False`` when GDAL is unavailable.

    ``value_floor_m`` is the lowest value the post-warp sanitizer treats as
    genuine data (spec section 2.3): terrestrial elevation providers keep
    the default -600 m, while a bathymetry provider passes its own
    ``value_floor_m`` (CUDEM Hawaii uses -11100 m) so measured seabed
    depths are not mistaken for leaked fill values and discarded.

    ``gdal_configuration_options`` are applied around the warp only (via
    ``gdal.config_options``): credential-gated sources use it to pass
    e.g. ``GDAL_HTTP_USERPWD`` without leaking it into global state.
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
        # Some sources (plain XYZ grids) carry no CRS of their own.
        srcSRS=source_srs,
        # ... and some carry an UNDECLARED fill value (Ireland's -99).
        srcNodata=source_nodata,
        dstSRS="EPSG:4326",
        # Heights must pass through UNCHANGED in the source vertical
        # datum (the provenance datum_note promises exactly that).
        # Without -novshift, GDAL applies a geoid shift whenever a
        # source declares a compound CRS -- Lantmateriet's COGs
        # (EPSG:5845, SWEREF99 TM + RH2000 height) came out 23-36 m
        # too high that way, ellipsoidal instead of orthometric.
        options=["-novshift"],
        outputBounds=(west, south, east, north),
        xRes=x_resolution_deg,
        yRes=y_resolution_deg,
        resampleAlg="bilinear",
        dstNodata=-32768.0,
        creationOptions=["COMPRESS=DEFLATE", "PREDICTOR=3"],
    )
    try:
        if gdal_configuration_options:
            with gdal.config_options(gdal_configuration_options):
                dataset = gdal.Warp(
                    destination_path,
                    list(vsicurl_inputs),
                    options=warp_options,
                )
        else:
            dataset = gdal.Warp(
                destination_path, list(vsicurl_inputs), options=warp_options
            )
    except Exception as error:
        if error_message_indicates_transient_network_failure(error):
            raise TransientFetchError(
                "elevation warp died on a network timeout or outage: "
                + str(error)
            ) from error
        UI.vprint(1, "   WARNING: elevation warp failed:", str(error))
        return False
    if dataset is None:
        return False
    dataset = None  # flush to disk before reopening
    # Sentinel sanitization: sources with UNDECLARED nodata leak their
    # fill values straight through the warp as "valid elevation" (the
    # Dutch national service fills with float-max, some Irish campaign
    # tiles with -9999).  Terrestrial elevations live within
    # -430..+8850 m; anything above +12000 or below ``value_floor_m``, or
    # not finite, is garbage and becomes nodata here so it can never reach
    # a bake.  The floor is per-call: terrestrial providers keep the -600 m
    # default (small negative fills like Ireland's -99 are PLAUSIBLE land
    # heights and need the per-provider source_nodata key instead), while
    # bathymetry providers lower it (CUDEM Hawaii to -11100 m) so real
    # seabed depths survive.  Done on a fresh update handle after the warp
    # result is flushed.
    try:
        dataset = gdal.Open(destination_path, gdal.GA_Update)
        band = dataset.GetRasterBand(1)
        values = band.ReadAsArray()
        if values is not None:
            garbage = (
                ~numpy.isfinite(values)
                | (values > 12000.0)
                | (values < value_floor_m)
            )
            if garbage.any():
                values[garbage] = -32768.0
                band.WriteArray(values)
                band.FlushCache()
        dataset = None
    except Exception as error:
        UI.vprint(
            1, "   WARNING: sentinel sanitization skipped:", str(error)
        )
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

    # Windowed /vsicurl reader: gates whole-tile elevation-level overlay use
    # (a whole tile costs only a decimated overview read, not a full campaign).
    supports_wide_area = True

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
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
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
        asset_resolution = None
        # 1. Explicit preference order (e.g. "dtm", "dtm-1m").  A
        #    preference token matches an exact asset key first, else any
        #    key CONTAINING it -- catalogs like Finland's Paituli mirror
        #    key their assets "<dataset>_at_paituli_tiff", so exact keys
        #    cannot be written into a definition file.
        for preference in prefer_asset_keys:
            matched = None
            if preference in assets:
                matched = assets[preference]
            else:
                for (key, asset) in assets.items():
                    if preference in key:
                        matched = asset
                        break
            if matched is not None and matched.get("href"):
                href = matched["href"]
                asset_resolution = _stac_asset_resolution(matched)
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
                    asset_resolution = _stac_asset_resolution(asset)
                    break
        # 3. Fall back to the FINEST GeoTIFF-typed asset: some catalogs
        #    (swisstopo's, for one) publish several resolutions of the
        #    same tile as filename-keyed assets carrying their own
        #    eo:gsd, so "first GeoTIFF" would be dictionary-order luck.
        if href is None:
            geotiff_assets = []
            for asset in assets.values():
                media_type = str(asset.get("type", "")).lower()
                if ("tiff" in media_type or "geotiff" in media_type) and asset.get(
                    "href"
                ):
                    geotiff_assets.append(asset)
            if geotiff_assets:
                finest = min(
                    geotiff_assets,
                    key=lambda asset: (
                        _stac_asset_resolution(asset)
                        if _stac_asset_resolution(asset) is not None
                        else float("inf")
                    ),
                )
                href = finest["href"]
                asset_resolution = _stac_asset_resolution(finest)
        if href is None:
            continue
        resolution = (
            asset_resolution
            or properties.get("gsd")
            or properties.get("resolution")
            or None
        )
        chosen.append((href, resolution))
    return chosen


def _stac_asset_resolution(asset):
    """The asset-level ground sample distance in metres, if declared."""
    for key in ("eo:gsd", "gsd", "resolution"):
        value = asset.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


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

    # Windowed /vsicurl reader: eligible for whole-tile overlay fetches.
    supports_wide_area = True

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
        import O4_Authenticated_Sessions as SESSIONS

        if not has_gdal:
            return None
        # Credential-gated pixels (Sweden's Geotorget: the STAC search
        # is anonymous but every Cloud-Optimized GeoTIFF read needs the
        # account as HTTP Basic authentication, carried to GDAL through
        # a warp-scoped configuration option).
        gdal_configuration_options = None
        if (
            SESSIONS.credential_kind(definition)
            == SESSIONS.CREDENTIAL_KIND_HTTP_BASIC
            and definition.get("session_name")
        ):
            try:
                session = SESSIONS.ensure_session(definition)
            except SESSIONS.LoginError as error:
                _warn_sign_in_needed_once(definition, error)
                return None
            gdal_configuration_options = {
                "GDAL_HTTP_USERPWD": "%s:%s" % tuple(session.auth)
            }
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
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
            gdal_configuration_options=gdal_configuration_options,
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
# Strategy: authenticated_token_search (signed-in token redemption)
# =====================================================================
# Providers whose ensure-session failed already this run: warn ONCE per
# provider, not once per airport.
_SIGN_IN_WARNED_PROVIDERS = set()


def _warn_sign_in_needed_once(definition, error):
    """Surface a LoginError loudly, once per provider per run."""
    provider_code = definition.get("code")
    if provider_code not in _SIGN_IN_WARNED_PROVIDERS:
        _SIGN_IN_WARNED_PROVIDERS.add(provider_code)
        UI.vprint(0, "   WARNING:", str(error))


@register_access_strategy("authenticated_token_search")
class AuthenticatedTokenSearchStrategy:
    """OGC/STAC search whose asset downloads need a signed-in session.

    The pattern (first seen at Portugal's Direcao-Geral do Territorio
    download centre, live-verified 2026-07-16): the ``search_url``
    endpoint is PUBLIC and its items' asset hrefs are short-lived
    tokenized download URLs -- but redeeming one requires the account
    session cookie, and answers an HTTP redirect to a presigned object-
    storage URL.  The presigned URL itself is public and range-capable,
    so the fetch ends in the same windowed ``/vsicurl`` warp as every
    other Cloud-Optimized GeoTIFF strategy; whole source tiles are never
    downloaded.

    Sessions, cookies, and stored accounts are owned by
    O4_Authenticated_Sessions (definition keys ``session_name``,
    ``login_flow``, ``login_url``, ``session_probe_url``).  Without a
    working sign-in the provider degrades to no-coverage with ONE loud
    instruction, never a silent skip and never a failed build.

    Tokens are minted fresh by the search and redeemed immediately --
    tokens observed at the pilot service expire quickly and may be
    single-use, so nothing tokenized is ever cached or reused.
    """

    # NOT wide-area eligible: a whole 1-degree tile over the pilot
    # service would mean thousands of token redemptions per fetch (and
    # the search limit would truncate the item list).  Airport insets
    # need a few dozen items at most.
    supports_wide_area = False

    def discover(self, definition, bounding_box_wgs84):
        import requests

        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        search_url = definition.get("search_url", "")
        if not search_url:
            return None
        (west, south, east, north) = bounding_box_wgs84
        body = {
            "bbox": [west, south, east, north],
            "limit": int(float(definition.get("search_limit", 200))),
        }
        collections = [
            token.strip()
            for token in str(definition.get("collections", "")).split(",")
            if token.strip()
        ]
        if collections:
            body["collections"] = collections
        try:
            response = requests.post(search_url, json=body, timeout=30)
            if response.status_code != 200:
                UI.vprint(
                    1,
                    "   WARNING: token search returned status",
                    response.status_code,
                    "for provider",
                    definition.get("code"),
                )
                return None
            payload = response.json()
        except Exception as error:
            UI.vprint(1, "   WARNING: token search failed:", str(error))
            return None
        items = StacCloudOptimizedGeoTiffStrategy._parse_search_payload(
            payload
        )
        if items is not None and len(items) >= body["limit"]:
            # No silent caps: a full page means the search may have
            # truncated the item list for this window.
            UI.vprint(
                1,
                "   WARNING: token search returned the full page of",
                body["limit"],
                "items for provider",
                definition.get("code"),
                "- coverage of this window may be incomplete.",
            )
        return items

    @staticmethod
    def _item_download_hrefs(items):
        """Tokenized download hrefs of the items' data assets."""
        hrefs = []
        for item in items:
            assets = item.get("assets")
            if not isinstance(assets, dict):
                continue
            for asset in assets.values():
                if not isinstance(asset, dict):
                    continue
                href = asset.get("href")
                roles = asset.get("roles") or []
                # A declared data role wins; the fallback (services that
                # do not declare roles at all) must not grab a lone
                # thumbnail or metadata asset by accident.
                if href and (
                    "data" in roles or (not roles and len(assets) == 1)
                ):
                    hrefs.append(href)
                    break
        return hrefs

    @staticmethod
    def _redeem_download_href(session, href):
        """Turn one tokenized download URL into its presigned target.

        The signed-in GET answers a redirect whose Location is the
        presigned object-storage URL.  Anything else (200 would mean the
        service streamed the file itself; an error status means the
        token aged out) returns None and the item is skipped with a
        warning by the caller.
        """
        try:
            response = session.get(href, timeout=30, allow_redirects=False)
        except Exception:
            return None
        if response.status_code not in (301, 302, 303, 307, 308):
            return None
        location = response.headers.get("Location", "")
        if not location.lower().startswith(("http://", "https://")):
            return None
        return location

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        import O4_Authenticated_Sessions as SESSIONS

        if not has_gdal:
            return None
        items = self.discover(definition, bounding_box_wgs84)
        if not items:
            return None
        try:
            session = SESSIONS.ensure_session(definition)
        except SESSIONS.LoginError as error:
            _warn_sign_in_needed_once(definition, error)
            return None
        hrefs = self._item_download_hrefs(items)
        presigned_urls = []
        for href in hrefs:
            presigned = self._redeem_download_href(session, href)
            if presigned is None:
                UI.vprint(
                    1,
                    "   WARNING: could not redeem a download token for",
                    definition.get("code"),
                    "- skipping one source tile.",
                )
                continue
            presigned_urls.append(presigned)
        if not presigned_urls:
            return None
        vsicurl_inputs = [
            _stac_asset_href_to_vsicurl(url) for url in presigned_urls
        ]
        source_nodata = definition.get("source_nodata")
        if not warp_vsicurl_sources_to_geotiff(
            vsicurl_inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            source_nodata=(
                float(source_nodata) if source_nodata is not None else None
            ),
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        ):
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            # Provenance records the item ids, NOT the redeemed URLs:
            # presigned URLs carry signature query parameters and expire.
            "source_ids": [
                item.get("id") for item in items if item.get("id")
            ],
            "collections": [
                token.strip()
                for token in str(definition.get("collections", "")).split(",")
                if token.strip()
            ],
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 3: wcs (OGC Web Coverage Service GetCoverage -> warp)
# =====================================================================
def _geotiff_has_valid_data(geotiff_path):
    """Does the raster contain at least one non-nodata sample?

    A Web Coverage Service window requested inside the definition's
    coverage_bbox but outside the national data extent (a Welsh airport
    against the England-only composite, say) warps successfully to an
    all-nodata raster; treating that as a fetched inset would bake a
    nodata hole into the airport.  Callers delete the file and record
    no-coverage instead.
    """
    if not has_gdal:
        return False
    try:
        dataset = gdal.Open(geotiff_path)
        band = dataset.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        values = band.ReadAsArray()
    except Exception:
        return False
    if values is None:
        return False
    if nodata is None:
        return True
    return bool((values != nodata).any())


def geotiff_is_constant_value(geotiff_path):
    """Are ALL valid samples in the raster one single constant value?

    A plausibility probe for coastline band cells.  Some national Web
    Coverage Services answer requests beyond their true data extent with
    a constant FILL instead of nodata (the Spanish PNOA endpoint serves
    0.0 across the border over Portugal), so the raster passes both the
    post-warp sentinel sanitizer (0.0 is a plausible height) and
    :func:`_geotiff_has_valid_data` (nothing equals the nodata marker)
    and would bake a sea-level plateau over foreign land.  Genuine warped
    lidar is never bit-for-bit constant across a whole 0.1 degree cell,
    while an all-water cell that IS legitimately constant loses nothing
    by falling back to the base elevation source (which models the sea
    anyway) -- so callers may safely treat a constant cell as
    no-coverage.  Note the check is deliberately NOT "mostly zero":
    genuine coastal cells hold large exactly-0.0 sea areas next to varied
    land, so any zero-share threshold would reject real data.  Returns
    False when the raster cannot be read or holds no valid sample (those
    cases belong to :func:`_geotiff_has_valid_data`).
    """
    if not has_gdal:
        return False
    try:
        dataset = gdal.Open(geotiff_path)
        band = dataset.GetRasterBand(1)
        nodata = band.GetNoDataValue()
        values = band.ReadAsArray()
    except Exception:
        return False
    if values is None:
        return False
    values = values.ravel()
    if nodata is not None:
        values = values[values != nodata]
    if values.size == 0:
        return False
    return bool((values == values[0]).all())


# GDAL's WCS driver defaults its curl TOTAL-transfer timeout to 30
# seconds (frmts/wcs/wcsdataset.cpp falls back to Timeout "30"), which a
# windowed GetCoverage for a large airport at meter-class resolution
# cannot honour: Heathrow's 1 m window from the Environment Agency
# service died mid-stream at 20 MB.  The driver-level open option is the
# ONLY override -- the driver always passes its own TIMEOUT to curl, so
# the GDAL_HTTP_TIMEOUT configuration option never applies to WCS reads.
WCS_REQUEST_TIMEOUT_SECONDS = 600


@register_access_strategy("wcs")
class WcsStrategy:
    """National lidar terrain models served over OGC Web Coverage Service.

    The European national programmes (England's Environment Agency
    composite, Norway's Kartverket national height model, Denmark's
    DHM, ...) publish meter-class bare-earth models as WCS endpoints
    rather than catalogs of Cloud-Optimized GeoTIFFs.  GDAL's WCS driver
    does the protocol work -- version negotiation from 1.0.0 through
    2.0.1, DescribeCoverage, and windowed GetCoverage requests -- so the
    fetch core is the same warp every other strategy uses, reading only
    the airport window from the national coverage.

    Unlike the catalog strategies there is no per-item discovery API:
    one definition names ONE national coverage, and the post-warp
    validity check in :func:`_geotiff_has_valid_data` is what turns
    inside-the-box-but-outside-the-data airports into cached
    no-coverage negatives.
    """

    # Windowed GetCoverage reader: eligible for whole-tile overlay fetches.
    supports_wide_area = True

    def dataset_name(self, definition, api_key=None):
        """The GDAL WCS driver dataset name for the definition.

        A ``{api_key}`` placeholder in the service URL (credential-gated
        services like Denmark's Datafordeler) is substituted when a key
        is supplied and left literal otherwise -- discovery results and
        provenance records use the literal form so the secret never
        lands in a log or a sidecar file.
        """
        service_url = definition["wcs_service_url"]
        if api_key is not None:
            service_url = service_url.replace("{api_key}", api_key)
        separator = "&" if "?" in service_url else "?"
        return (
            "WCS:"
            + service_url
            + separator
            + "version="
            + str(definition.get("wcs_version", "2.0.1"))
            + "&coverage="
            + definition["wcs_coverage"]
        )

    def discover(self, definition, bounding_box_wgs84):
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        return [{"dataset": self.dataset_name(definition)}]

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        import O4_Authenticated_Sessions as SESSIONS

        if not has_gdal:
            return None
        api_key = None
        if "{api_key}" in str(definition.get("wcs_service_url", "")):
            try:
                api_key = SESSIONS.ensure_api_key(definition)
            except SESSIONS.LoginError as error:
                _warn_sign_in_needed_once(definition, error)
                return None
        dataset_name = self.dataset_name(definition, api_key)
        # Open with an explicit request timeout: the driver's own 30 s
        # default (see WCS_REQUEST_TIMEOUT_SECONDS above) kills large
        # windowed GetCoverage responses mid-stream.  The option also
        # covers the GetCapabilities/DescribeCoverage handshake and is
        # folded into the driver's cached service description.
        try:
            wcs_dataset = gdal.OpenEx(
                dataset_name,
                gdal.OF_RASTER,
                open_options=[
                    "TIMEOUT=%d" % WCS_REQUEST_TIMEOUT_SECONDS
                ],
            )
        except Exception as error:
            if error_message_indicates_transient_network_failure(error):
                raise TransientFetchError(
                    "WCS coverage open died on a network timeout or "
                    "outage: " + str(error)
                ) from error
            UI.vprint(
                1,
                "   WARNING: could not open WCS coverage:",
                str(error),
            )
            return None
        if wcs_dataset is None:
            return None
        if not warp_vsicurl_sources_to_geotiff(
            [wcs_dataset],
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        ):
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            # The literal form: any {api_key} placeholder stays a
            # placeholder so the secret never reaches the provenance
            # sidecar.
            "source_urls": [self.dataset_name(definition)],
            "wcs_coverage": definition.get("wcs_coverage"),
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 4: static_stac (catalog.json trees on object storage)
# =====================================================================
def _bounding_boxes_intersect(box_a, box_b):
    """Do two (west, south, east, north) boxes overlap?"""
    (west_a, south_a, east_a, north_a) = box_a[:4]
    (west_b, south_b, east_b, north_b) = box_b[:4]
    return not (
        east_a < west_b
        or east_b < west_a
        or north_a < south_b
        or north_b < south_a
    )


@register_access_strategy("static_stac")
class StaticStacCatalogStrategy:
    """Static STAC catalog trees on object storage (no /search API).

    New Zealand's national lidar (the ``nz-elevation`` bucket) publishes
    STAC 1.0 as plain JSON files: a root catalog linking ~200 survey
    collections, each linking hundreds of items, each carrying a
    bounding box and one Cloud-Optimized GeoTIFF asset.  There is no
    search endpoint, so discovery WALKS the tree -- and because that
    walk is thousands of small requests for a whole country, every
    fetched bounding box is memoised in ONE per-provider index file
    under ``Elevation_data/``: the first airport in a region pays the
    walk, every later airport (and every rebuild) reads the index.
    """

    def index_path(self, definition):
        return os.path.join(
            FNAMES.Elevation_dir,
            definition["code"].lower() + "_static_stac_index.json",
        )

    def _load_index(self, definition):
        try:
            with open(self.index_path(definition), "r") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _save_index(self, definition, index):
        index_file = self.index_path(definition)
        os.makedirs(os.path.dirname(index_file), exist_ok=True)
        with open(index_file, "w") as handle:
            json.dump(index, handle)

    def _fetch_json(self, session, url):
        try:
            response = session.get(url, timeout=60)
        except Exception as error:
            UI.vprint(
                1, "   WARNING: static catalog request failed:", str(error)
            )
            return None
        if response.status_code != 200:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _prefer_asset_keys(self, definition):
        return [
            token.strip()
            for token in str(
                definition.get("dtm_asset_keys", "dtm")
            ).split(",")
            if token.strip()
        ]

    def _ensure_collections(self, definition, index, session):
        """Fill index["collections"] = {url: {bbox, items}} once.

        Two catalog shapes are handled.  The New Zealand shape is a tree:
        the root links ~200 survey COLLECTIONS (``rel="child"``), each of
        which carries its own spatial extent and a list of item links.
        The NOAA NCEI CUDEM shape (spec section 2.3) has NO child links --
        its tiles hang directly off the root as ``rel="item"`` links -- so
        when the catalog has zero child links but at least one item link we
        synthesize a single pseudo-collection keyed by the catalog URL,
        whose bounding box is the provider's ``coverage_bbox`` (there is no
        per-collection extent to read) and whose items are the root item
        hrefs.  The tree path below is left byte-identical.
        """
        import urllib.parse

        if index.get("collections") is not None:
            return
        catalog_url = definition["catalog_url"]
        catalog = self._fetch_json(session, catalog_url)
        if catalog is None:
            return
        children = [
            link
            for link in catalog.get("links", [])
            if link.get("rel") == "child" and link.get("href")
        ]
        if not children:
            root_item_hrefs = [
                link["href"]
                for link in catalog.get("links", [])
                if link.get("rel") == "item" and link.get("href")
            ]
            if root_item_hrefs:
                # Reuse the coverage_bbox parser -- a definition parsed from
                # a .elv file already holds a (west, south, east, north)
                # tuple, but one assembled by hand in a test may still be a
                # raw "W,S,E,N" string.
                coverage = definition.get("coverage_bbox")
                if not isinstance(coverage, (list, tuple)):
                    coverage = _parse_bounding_box(coverage)
                if coverage:
                    UI.vprint(
                        1,
                        "    Indexing the",
                        definition["code"],
                        "elevation catalog (root-item catalog,",
                        len(root_item_hrefs),
                        "tiles, once per install).",
                    )
                    index["collections"] = {
                        catalog_url: {
                            "bbox": list(coverage),
                            "items": root_item_hrefs,
                        }
                    }
                    return
        must_contain = str(definition.get("collection_filter", ""))
        if must_contain:
            children = [
                link for link in children if must_contain in link["href"]
            ]
        UI.vprint(
            1,
            "    Indexing the",
            definition["code"],
            "elevation catalog (",
            len(children),
            "collections, once per install).",
        )
        collections = {}
        for link in children:
            collection_url = urllib.parse.urljoin(catalog_url, link["href"])
            collection = self._fetch_json(session, collection_url)
            if not collection:
                continue
            boxes = (
                (collection.get("extent") or {})
                .get("spatial", {})
                .get("bbox")
            ) or []
            if not boxes:
                continue
            collections[collection_url] = {
                "bbox": boxes[0],
                "items": [
                    item_link["href"]
                    for item_link in collection.get("links", [])
                    if item_link.get("rel") == "item"
                    and item_link.get("href")
                ],
            }
        index["collections"] = collections

    def _ensure_items(self, definition, index, session, collection_url):
        """Fill and return the item entries of one collection."""
        import urllib.parse

        items_by_collection = index.setdefault("items", {})
        if collection_url in items_by_collection:
            return items_by_collection[collection_url]
        item_hrefs = index["collections"][collection_url]["items"]
        UI.vprint(
            1,
            "    Indexing",
            len(item_hrefs),
            "elevation tiles of one survey (once).",
        )
        prefer = self._prefer_asset_keys(definition)
        entries = []
        for href in item_hrefs:
            item_url = urllib.parse.urljoin(collection_url, href)
            item = self._fetch_json(session, item_url)
            if not item or not item.get("bbox"):
                continue
            selected = _select_stac_dtm_assets([item], prefer)
            if not selected:
                continue
            (asset_href, resolution) = selected[0]
            entries.append(
                {
                    "bbox": item["bbox"],
                    "href": urllib.parse.urljoin(item_url, asset_href),
                    "resolution": resolution,
                }
            )
        items_by_collection[collection_url] = entries
        return entries

    def discover(self, definition, bounding_box_wgs84):
        import requests

        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        index = self._load_index(definition)
        session = requests.Session()
        self._ensure_collections(definition, index, session)
        if not index.get("collections"):
            return None
        sources = []
        for (collection_url, record) in index["collections"].items():
            if not _bounding_boxes_intersect(
                record["bbox"], bounding_box_wgs84
            ):
                continue
            for entry in self._ensure_items(
                definition, index, session, collection_url
            ):
                if _bounding_boxes_intersect(
                    entry["bbox"], bounding_box_wgs84
                ):
                    sources.append(entry)
        self._save_index(definition, index)
        return sources or None

    # Runs in a THROWAWAY interpreter: imagecodecs' LERC decoder and the
    # osgeo shared libraries abort the process when both are loaded (a
    # native symbol clash, reproduced on macOS with the Homebrew GDAL and
    # the imagecodecs wheels), so the decode must never share a process
    # with GDAL.  argv: <input tiff> <output npy>; tags go to stdout.
    _LERC_DECODE_SNIPPET = (
        "import json, sys\n"
        "import numpy\n"
        "import tifffile\n"
        "with tifffile.TiffFile(sys.argv[1]) as tif:\n"
        "    page = tif.pages[0]\n"
        "    numpy.save(sys.argv[2], page.asarray())\n"
        "    print(json.dumps({\n"
        "        'scale': list(page.tags['ModelPixelScaleTag'].value),\n"
        "        'tiepoint': list(page.tags['ModelTiepointTag'].value),\n"
        "    }))\n"
    )

    def _decode_lerc_sources(self, definition, sources, destination_path):
        """Download + decode LERC-compressed assets to local GeoTIFFs.

        Some catalogs (New Zealand's) compress their Cloud-Optimized
        GeoTIFFs with LERC, a codec most GDAL builds (Homebrew, the
        official wheels) ship WITHOUT -- a ``/vsicurl`` warp then fails
        with "missing codec LERC".  When the definition declares
        ``asset_compression=lerc`` the whole tile is downloaded instead
        and decoded (in a subprocess, see ``_LERC_DECODE_SNIPPET``) into
        a temporary plain GeoTIFF beside ``destination_path``,
        georeferenced from the embedded ModelPixelScale/ModelTiepoint
        tags and the definition's ``source_epsg``.  Returns the
        temporary paths (caller deletes).
        """
        import subprocess
        import sys

        import requests

        if getattr(sys, "frozen", False):
            # The packaged application cannot spawn a bare interpreter;
            # LERC sources degrade to the base tier there.
            UI.vprint(
                1,
                "   WARNING: LERC-compressed elevation sources are not "
                "available in the packaged application - skipping "
                + str(definition.get("code"))
                + ".",
            )
            return []
        source_epsg = int(float(definition.get("source_epsg", 4326)))
        temporary_paths = []
        for (number, entry) in enumerate(sources):
            tiff_path = destination_path + ".lerc%d.download" % number
            npy_path = destination_path + ".lerc%d.npy" % number
            try:
                response = requests.get(entry["href"], timeout=300)
                if response.status_code != 200:
                    continue
                with open(tiff_path, "wb") as handle:
                    handle.write(response.content)
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        self._LERC_DECODE_SNIPPET,
                        tiff_path,
                        npy_path,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                if completed.returncode != 0:
                    raise RuntimeError(
                        completed.stderr.strip()[-200:] or "decode failed"
                    )
                tags = json.loads(completed.stdout)
                scale = tags["scale"]
                tiepoint = tags["tiepoint"]
                values = numpy.load(npy_path)
            except Exception as error:
                UI.vprint(
                    1,
                    "   WARNING: could not decode elevation tile",
                    entry["href"],
                    ":",
                    str(error),
                )
                continue
            finally:
                for scratch in (tiff_path, npy_path):
                    try:
                        os.remove(scratch)
                    except OSError:
                        pass
            temporary_path = destination_path + ".lerc%d.tif" % number
            driver = gdal.GetDriverByName("GTiff")
            dataset = driver.Create(
                temporary_path,
                values.shape[1],
                values.shape[0],
                1,
                gdal.GDT_Float32,
            )
            dataset.SetGeoTransform(
                (tiepoint[3], scale[0], 0.0, tiepoint[4], 0.0, -scale[1])
            )
            spatial_reference = osr.SpatialReference()
            spatial_reference.ImportFromEPSG(source_epsg)
            dataset.SetProjection(spatial_reference.ExportToWkt())
            band = dataset.GetRasterBand(1)
            nodata = _parse_float(definition.get("source_nodata"), -9999.0)
            band.SetNoDataValue(nodata)
            band.WriteArray(values)
            band.FlushCache()
            dataset = None
            temporary_paths.append(temporary_path)
        return temporary_paths

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
        temporary_paths = []
        if str(definition.get("asset_compression", "")).lower() == "lerc":
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            temporary_paths = self._decode_lerc_sources(
                definition, sources, destination_path
            )
            warp_inputs = temporary_paths
        else:
            warp_inputs = [
                _stac_asset_href_to_vsicurl(entry["href"])
                for entry in sources
            ]
        warped = bool(warp_inputs) and warp_vsicurl_sources_to_geotiff(
            warp_inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        )
        for temporary_path in temporary_paths:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
        if not warped:
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        resolutions = [
            entry["resolution"]
            for entry in sources
            if entry.get("resolution")
        ]
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [entry["href"] for entry in sources],
            "native_resolution_m": (
                min(resolutions)
                if resolutions
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
# Strategy: coordinate_named_url_list (plain URL list, filename = location)
# =====================================================================
import re as _re

# The NOAA NCEI CUDEM filename tile-locator token.  Grammar (spec / this
# file's docstring): a filename contains one pair
# ``_<lat><sep><lon>_`` where the LATITUDE token is
# ``[ns]DDxQQ`` and the LONGITUDE token is ``[ew]DDDxQQ`` -- two-digit
# whole degrees of latitude, three-digit whole degrees of longitude, a
# decimal separator, and a two-digit hundredths part in {00,25,50,75}.
# The separator is written both lowercase ``x`` (n39x00) AND uppercase
# ``X`` (n25X75, the 2018 Florida campaign): the live CONUS list mixes
# both, so the pattern accepts either (a spec-reality note recorded in the
# strategy docstring).
_CUDEM_TILE_TOKEN = _re.compile(
    r"_([ns])(\d{2})[xX](\d{2})_([ew])(\d{3})[xX](\d{2})_"
)


@register_access_strategy("coral_atlas_library")
class CoralAtlasLibraryStrategy:
    """Locally downloaded Allen Coral Atlas packages as a provider.

    The Atlas's 10 m reef bathymetry is account-gated (free), so it is
    served from the user-populated library under
    ``Elevation_data/AllenCoralAtlas/`` instead of a live URL — see
    ``O4_Coral_Atlas`` for the library, the unit conversion (positive
    centimetres -> negative metres) and the guided in-app fetch.
    Discovery consults only the local index: a tile has coverage exactly
    when the user has downloaded a package that overlaps it.
    """

    supports_wide_area = True

    def discover(self, definition, bounding_box_wgs84):
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        import O4_Coral_Atlas as CORAL

        return CORAL.entries_intersecting(bounding_box_wgs84) or None

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
        import O4_Coral_Atlas as CORAL

        if not CORAL.fetch_window_to_geotiff(
            sources,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
        ):
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": sources,
            "source_ids": [os.path.basename(s) for s in sources],
            "project_titles": ["Allen Coral Atlas bathymetry"],
            "publication_date": "",
            "license": definition.get("license"),
            "attribution": definition.get("attribution"),
            "vertical_datum": definition.get("vertical_datum"),
            "datum_note": (
                "Satellite-derived depths below the sea surface;"
                " converted from positive centimetres to negative metres."
            ),
            "fetch_date": datetime.date.today().isoformat(),
            "bounding_box_wgs84": list(bounding_box_wgs84),
            "resolution_m": target_resolution_m,
        }


@register_access_strategy("coordinate_named_url_list")
class CoordinateNamedUrlListStrategy:
    """Cloud-Optimized GeoTIFFs indexed by a plain-text list of URLs.

    NOAA NCEI publishes several CUDEM regions (CONUS coasts, Guam) NOT as a
    STAC catalog but as a single ``urllist<id>.txt`` file: one Cloud-
    Optimized GeoTIFF URL per line, the tile's location encoded in the
    filename.  There is no per-tile JSON to walk (unlike ``static_stac``),
    so discovery fetches that ONE text file once, parses every filename's
    coordinate token into a bounding box, and memoises the result in a
    per-provider index under ``Elevation_data/`` -- the first airport in a
    region pays the single small download, every later airport and every
    rebuild reads the index.  Fetch is the shared windowed ``/vsicurl``
    warp core (only the requested window of each remote tile is read).

    Filename grammar and its coordinate semantics (validated 2026-07-16
    against the NCEI STAC oracles -- do NOT change without re-validating):

      * The token pair is ``_[ns]DDxQQ_[ew]DDDxQQ_`` where ``x`` (or its
        uppercase ``X`` variant, both live in the CONUS list) is the
        decimal separator and ``QQ`` is hundredths of a degree in
        {00,25,50,75}.
      * The LATITUDE token gives the tile's NORTH edge: ``n`` = positive
        latitude, ``s`` = negative.  Verified against Hawaii item
        ``ncei19_n22x00_w159x50`` (bbox north 22.0002) and, for the
        southern hemisphere, American Samoa items
        ``ncei19_s14x25_w169x75`` (bbox north -14.2498) and
        ``ncei19_s14x00_w169x50`` (bbox north -13.9998): ``s14x25`` is the
        NORTH edge at -14.25, not the south edge.
      * The LONGITUDE token gives the tile's WEST edge: ``w`` = negative
        longitude, ``e`` = positive (Guam ``ncei19_n13x25_e144x50`` ->
        west 144.50).
      * Every tile is 0.25 deg x 0.25 deg, so ``east = west + 0.25`` and
        ``south = north - 0.25``.

    Lines whose filename carries no such token (the list also holds
    shapefile sidecars, metadata ``.xml``/``.json``, ``.zip`` archives and
    the list file itself) are skipped, reported once as a summary count --
    never one warning per line.
    """

    # Windowed /vsicurl reader: eligible for whole-tile overlay fetches.
    supports_wide_area = True

    # 0.25 deg tile edge, and the padding applied to every stored bbox so a
    # query that grazes a tile boundary still matches (mirrors the oracle
    # bboxes' own ~0.0002 deg overlap).
    TILE_DEGREES = 0.25
    BBOX_PADDING_DEGREES = 0.001

    @staticmethod
    def parse_filename_bbox(filename):
        """Parse a CUDEM tile filename into ``[west, south, east, north]``.

        Returns the UNPADDED tile bounding box, or ``None`` when the
        filename carries no ``_[ns]DDxQQ_[ew]DDDxQQ_`` locator token.  See
        the class docstring for the validated north-edge / west-edge
        coordinate convention.
        """
        match = _CUDEM_TILE_TOKEN.search(filename)
        if match is None:
            return None
        (
            latitude_hemisphere,
            latitude_degrees,
            latitude_hundredths,
            longitude_hemisphere,
            longitude_degrees,
            longitude_hundredths,
        ) = match.groups()
        latitude = int(latitude_degrees) + int(latitude_hundredths) / 100.0
        longitude = int(longitude_degrees) + int(longitude_hundredths) / 100.0
        north = latitude if latitude_hemisphere == "n" else -latitude
        west = longitude if longitude_hemisphere == "e" else -longitude
        tile = CoordinateNamedUrlListStrategy.TILE_DEGREES
        return [west, north - tile, west + tile, north]

    def index_path(self, definition):
        return os.path.join(
            FNAMES.Elevation_dir,
            definition["code"].lower() + "_url_list_index.json",
        )

    def _load_index(self, definition):
        try:
            with open(self.index_path(definition), "r") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _save_index(self, definition, index):
        index_file = self.index_path(definition)
        os.makedirs(os.path.dirname(index_file), exist_ok=True)
        with open(index_file, "w") as handle:
            json.dump(index, handle)

    def _ensure_entries(self, definition, index):
        """Fill ``index["entries"]`` from the URL list once (memoised).

        ``entries`` is a list of ``{"href": url, "bbox": padded box}``.
        The single text-file download happens only when the index has no
        ``entries`` key yet; a later call (this run or a rebuild) reads the
        saved index and performs ZERO HTTP.
        """
        import requests

        if index.get("entries") is not None:
            return
        url_list_url = definition.get("url_list_url")
        if not url_list_url:
            return
        try:
            response = requests.get(url_list_url, timeout=60)
        except Exception as error:
            UI.vprint(
                1, "   WARNING: URL-list request failed:", str(error)
            )
            return
        if response.status_code != 200:
            UI.vprint(
                1,
                "   WARNING: URL-list request returned status",
                response.status_code,
            )
            return
        entries = []
        skipped = 0
        padding = self.BBOX_PADDING_DEGREES
        for line in response.text.splitlines():
            url = line.strip()
            if not url:
                continue
            filename = url.rsplit("/", 1)[-1]
            box = self.parse_filename_bbox(filename)
            if box is None:
                skipped += 1
                continue
            entries.append(
                {
                    "href": url,
                    "bbox": [
                        box[0] - padding,
                        box[1] - padding,
                        box[2] + padding,
                        box[3] + padding,
                    ],
                }
            )
        UI.vprint(
            1,
            "    Indexing the",
            definition.get("code"),
            "elevation URL list (",
            len(entries),
            "coordinate-named tiles,",
            skipped,
            "non-tile lines skipped, once per install).",
        )
        index["entries"] = entries

    def discover(self, definition, bounding_box_wgs84):
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        index = self._load_index(definition)
        self._ensure_entries(definition, index)
        entries = index.get("entries")
        if not entries:
            return None
        self._save_index(definition, index)
        matched = [
            entry
            for entry in entries
            if _bounding_boxes_intersect(entry["bbox"], bounding_box_wgs84)
        ]
        return matched or None

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
        vsicurl_inputs = ["/vsicurl/" + entry["href"] for entry in sources]
        if not warp_vsicurl_sources_to_geotiff(
            vsicurl_inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        ):
            return None
        tile_stems = [
            entry["href"].rsplit("/", 1)[-1].rsplit(".", 1)[0]
            for entry in sources
        ]
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [entry["href"] for entry in sources],
            "source_ids": tile_stems,
            "project_titles": tile_stems,
            "license": definition.get("license"),
            "attribution": definition.get("attribution"),
            "vertical_datum": definition.get("vertical_datum"),
            "datum_note": (
                "Depths are in the source local tidal datum; a bathymetry "
                "source is used only for water rendering, never for terrain "
                "grading."
            ),
            "fetch_date": datetime.date.today().isoformat(),
            "bounding_box_wgs84": list(bounding_box_wgs84),
            "resolution_m": target_resolution_m,
        }


# =====================================================================
# Strategy 5: xyz_text_tiles (slippy-map elevation text tiles -> warp)
# =====================================================================
_WEB_MERCATOR_HALF_CIRCUMFERENCE = 20037508.342789244


def _slippy_tile_of(latitude, longitude, zoom):
    """The (x, y) slippy-map tile containing a WGS84 point at ``zoom``."""
    import math

    n = 2 ** zoom
    x = int((longitude + 180.0) / 360.0 * n)
    latitude_radians = math.radians(latitude)
    y = int(
        (
            1.0
            - math.log(
                math.tan(latitude_radians) + 1.0 / math.cos(latitude_radians)
            )
            / math.pi
        )
        / 2.0
        * n
    )
    return (min(max(x, 0), n - 1), min(max(y, 0), n - 1))


@register_access_strategy("xyz_text_tiles")
class XyzTextTileStrategy:
    """Slippy-map elevation tiles carrying comma-separated metre values.

    Japan's Geospatial Information Authority publishes its national
    elevation model this way and ONLY this way (no GeoTIFF, WCS or
    STAC anywhere in its stack): anonymous 256x256 text tiles in Web
    Mercator, one elevation per cell, the letter ``e`` for nodata.
    Fetch computes the covering tiles at ``tile_zoom``, assembles them
    into an EPSG:3857 mosaic, and (when any primary tile is missing --
    the 5 m lidar is not wall-to-wall) underlays a second mosaic from
    ``fallback_url_template`` at ``fallback_zoom``, the server-side
    priority-merged nationwide composite.  Both land as temporary
    GeoTIFFs beside the destination and go through the same warp core
    as every other strategy (mosaic order makes the primary win where
    it has data).
    """

    MAXIMUM_TILES_PER_MOSAIC = 4096

    def discover(self, definition, bounding_box_wgs84):
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        return [{"template": definition.get("tile_url_template")}]

    def _tile_range(self, bounding_box_wgs84, zoom):
        (west, south, east, north) = bounding_box_wgs84
        (x_min, y_min) = _slippy_tile_of(north, west, zoom)
        (x_max, y_max) = _slippy_tile_of(south, east, zoom)
        return (x_min, y_min, x_max, y_max)

    def _mosaic_to_geotiff(
        self, session, template, zoom, bounding_box_wgs84, temporary_path
    ):
        """Fetch all covering tiles at ``zoom`` into one local GeoTIFF.

        Returns ``(path, missing_tile_count)`` or ``(None, 0)`` when
        nothing at all was retrieved (outside the dataset).
        """
        (x_min, y_min, x_max, y_max) = self._tile_range(
            bounding_box_wgs84, zoom
        )
        columns = x_max - x_min + 1
        rows = y_max - y_min + 1
        if columns * rows > self.MAXIMUM_TILES_PER_MOSAIC:
            UI.vprint(
                1,
                "   WARNING: elevation tile mosaic of",
                columns * rows,
                "tiles exceeds the cap - skipping this source.",
            )
            return (None, 0)
        values = numpy.full(
            (rows * 256, columns * 256), -32768.0, dtype=numpy.float32
        )
        fetched = 0
        missing = 0
        for tile_y in range(y_min, y_max + 1):
            for tile_x in range(x_min, x_max + 1):
                url = (
                    template.replace("{zoom}", str(zoom))
                    .replace("{x}", str(tile_x))
                    .replace("{y}", str(tile_y))
                )
                try:
                    response = session.get(url, timeout=60)
                except Exception:
                    missing += 1
                    continue
                if response.status_code != 200:
                    missing += 1
                    continue
                try:
                    tile_values = numpy.array(
                        [
                            [
                                -32768.0 if token == "e" else float(token)
                                for token in line.split(",")
                            ]
                            for line in response.text.strip().split("\n")
                        ],
                        dtype=numpy.float32,
                    )
                    if tile_values.shape != (256, 256):
                        raise ValueError(str(tile_values.shape))
                except Exception:
                    missing += 1
                    continue
                row0 = (tile_y - y_min) * 256
                column0 = (tile_x - x_min) * 256
                values[row0 : row0 + 256, column0 : column0 + 256] = (
                    tile_values
                )
                fetched += 1
        if not fetched:
            return (None, missing)
        tile_size_m = 2.0 * _WEB_MERCATOR_HALF_CIRCUMFERENCE / (2 ** zoom)
        origin_x = x_min * tile_size_m - _WEB_MERCATOR_HALF_CIRCUMFERENCE
        origin_y = _WEB_MERCATOR_HALF_CIRCUMFERENCE - y_min * tile_size_m
        pixel_m = tile_size_m / 256.0
        driver = gdal.GetDriverByName("GTiff")
        dataset = driver.Create(
            temporary_path,
            values.shape[1],
            values.shape[0],
            1,
            gdal.GDT_Float32,
        )
        dataset.SetGeoTransform(
            (origin_x, pixel_m, 0.0, origin_y, 0.0, -pixel_m)
        )
        spatial_reference = osr.SpatialReference()
        spatial_reference.ImportFromEPSG(3857)
        dataset.SetProjection(spatial_reference.ExportToWkt())
        band = dataset.GetRasterBand(1)
        band.SetNoDataValue(-32768.0)
        band.WriteArray(values)
        band.FlushCache()
        dataset = None
        return (temporary_path, missing)

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        import requests

        if not has_gdal:
            return None
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        session = requests.Session()
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        primary_zoom = int(float(definition.get("tile_zoom", 15)))
        (primary_path, missing) = self._mosaic_to_geotiff(
            session,
            definition["tile_url_template"],
            primary_zoom,
            bounding_box_wgs84,
            destination_path + ".primary.tif",
        )
        fallback_path = None
        fallback_template = definition.get("fallback_url_template")
        if fallback_template and (primary_path is None or missing):
            (fallback_path, _fallback_missing) = self._mosaic_to_geotiff(
                session,
                fallback_template,
                int(float(definition.get("fallback_zoom", 14))),
                bounding_box_wgs84,
                destination_path + ".fallback.tif",
            )
        # Later inputs win where they carry data: the primary (finer)
        # mosaic overlays the fallback composite.
        warp_inputs = [
            path for path in (fallback_path, primary_path) if path
        ]
        warped = bool(warp_inputs) and warp_vsicurl_sources_to_geotiff(
            warp_inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        )
        for path in (primary_path, fallback_path):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
        if not warped:
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [definition.get("tile_url_template")],
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 12: arcgis_lerc_tiles (tiles-only ArcGIS elevation services)
# =====================================================================
@register_access_strategy("arcgis_lerc_tiles")
class ArcgisLercTileStrategy:
    """Tiles-only ArcGIS elevation services (LERC blobs in Web Mercator).

    Some open city terrain models (Rio de Janeiro's lidar) are hosted
    as ArcGIS image services whose ``exportImage`` is disabled: the
    only data channel is a pre-rendered tile pyramid of single-band
    float LERC blobs on the STANDARD global Web Mercator grid, so a
    tile's (row, column) at a level are ordinary slippy-map
    coordinates.  Fetch computes the covering tiles at ``tile_level``,
    downloads the blobs, decodes them ALL in one subprocess (the
    imagecodecs LERC decoder and the osgeo libraries abort a shared
    process -- the same isolation the New Zealand provider uses),
    assembles an EPSG:3857 mosaic and warps it through the shared
    core.  Missing tiles (outside the service's data mask) are simply
    absent from the mosaic.
    """

    MAXIMUM_TILES_PER_MOSAIC = 1024

    # argv: <blob_directory> <output_directory>; decodes every *.lerc
    # file to a .npy beside-named file, marking invalid samples -32768.
    _LERC_BLOB_DECODE_SNIPPET = (
        "import os, sys\n"
        "import numpy\n"
        "import imagecodecs\n"
        "for name in os.listdir(sys.argv[1]):\n"
        "    if not name.endswith('.lerc'):\n"
        "        continue\n"
        "    blob = open(os.path.join(sys.argv[1], name), 'rb').read()\n"
        "    mask = None\n"
        "    try:\n"
        "        decoded = imagecodecs.lerc_decode(blob, masks=True)\n"
        "        if isinstance(decoded, tuple):\n"
        "            (values, mask) = decoded\n"
        "        else:\n"
        "            values = decoded\n"
        "    except TypeError:\n"
        "        values = imagecodecs.lerc_decode(blob)\n"
        "    values = numpy.asarray(values, dtype=numpy.float32)\n"
        "    values = values.reshape(values.shape[-2], values.shape[-1])\n"
        "    if mask is not None:\n"
        "        mask = numpy.asarray(mask, dtype=bool).reshape(values.shape)\n"
        "        values[~mask] = -32768.0\n"
        "    # ArcGIS elevation tiles carry a one-sample shared edge\n"
        "    # (257x257 for a 256 grid): crop to the tile proper.\n"
        "    values = values[:256, :256]\n"
        "    numpy.save(\n"
        "        os.path.join(sys.argv[2], name[:-5] + '.npy'), values\n"
        "    )\n"
    )

    def discover(self, definition, bounding_box_wgs84):
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        return [{"template": definition.get("tile_url_template")}]

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        import shutil
        import subprocess
        import sys

        import requests

        if not has_gdal:
            return None
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        if getattr(sys, "frozen", False):
            UI.vprint(
                1,
                "   WARNING: LERC-tile elevation sources are not "
                "available in the packaged application - skipping "
                + str(definition.get("code"))
                + ".",
            )
            return None
        level = int(float(definition.get("tile_level", 15)))
        # The pyramid grid: Web Mercator with the global origin by
        # default (Rio, Hong Kong, Zagreb); services caching in a
        # projected CRS (Estonia's EPSG:3301, Scotland's EPSG:27700)
        # declare tile_epsg / tile_origin_x / tile_origin_y /
        # tile_resolution (metres per pixel AT tile_level) instead.
        tile_epsg = int(float(definition.get("tile_epsg", 3857)))
        origin_x = _parse_float(
            definition.get("tile_origin_x"),
            -_WEB_MERCATOR_HALF_CIRCUMFERENCE,
        )
        origin_y = _parse_float(
            definition.get("tile_origin_y"),
            _WEB_MERCATOR_HALF_CIRCUMFERENCE,
        )
        resolution = _parse_float(
            definition.get("tile_resolution"),
            2.0
            * _WEB_MERCATOR_HALF_CIRCUMFERENCE
            / (2 ** level)
            / 256.0,
        )
        tile_span = resolution * 256.0
        (grid_x_min, grid_y_min, grid_x_max, grid_y_max) = (
            transform_bounding_box_to_epsg(bounding_box_wgs84, tile_epsg)
        )
        x_min = int((grid_x_min - origin_x) // tile_span)
        x_max = int((grid_x_max - origin_x) // tile_span)
        y_min = int((origin_y - grid_y_max) // tile_span)
        y_max = int((origin_y - grid_y_min) // tile_span)
        columns = x_max - x_min + 1
        rows = y_max - y_min + 1
        if columns * rows > self.MAXIMUM_TILES_PER_MOSAIC:
            UI.vprint(
                1,
                "   WARNING: LERC tile mosaic of",
                columns * rows,
                "tiles exceeds the cap - skipping this source.",
            )
            return None
        template = definition["tile_url_template"]
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        blob_directory = destination_path + ".lercblobs"
        decoded_directory = destination_path + ".lercnpy"
        os.makedirs(blob_directory, exist_ok=True)
        os.makedirs(decoded_directory, exist_ok=True)
        session = requests.Session()
        fetched = []
        try:
            for tile_y in range(y_min, y_max + 1):
                for tile_x in range(x_min, x_max + 1):
                    url = (
                        template.replace("{level}", str(level))
                        .replace("{row}", str(tile_y))
                        .replace("{col}", str(tile_x))
                    )
                    try:
                        response = session.get(url, timeout=60)
                    except Exception:
                        continue
                    if (
                        response.status_code != 200
                        or not response.content
                    ):
                        continue
                    name = "%d_%d" % (tile_x, tile_y)
                    with open(
                        os.path.join(blob_directory, name + ".lerc"), "wb"
                    ) as handle:
                        handle.write(response.content)
                    fetched.append((tile_x, tile_y, name))
            if not fetched:
                return None
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    self._LERC_BLOB_DECODE_SNIPPET,
                    blob_directory,
                    decoded_directory,
                ],
                capture_output=True,
                text=True,
                timeout=600,
            )
            if completed.returncode != 0:
                UI.vprint(
                    1,
                    "   WARNING: LERC decode failed:",
                    completed.stderr.strip()[-200:],
                )
                return None
            values = numpy.full(
                (rows * 256, columns * 256), -32768.0, dtype=numpy.float32
            )
            decoded_any = False
            for (tile_x, tile_y, name) in fetched:
                npy_path = os.path.join(decoded_directory, name + ".npy")
                try:
                    tile_values = numpy.load(npy_path)
                except (OSError, ValueError):
                    continue
                if tile_values.shape != (256, 256):
                    continue
                row0 = (tile_y - y_min) * 256
                column0 = (tile_x - x_min) * 256
                values[row0 : row0 + 256, column0 : column0 + 256] = (
                    tile_values
                )
                decoded_any = True
            if not decoded_any:
                return None
        finally:
            shutil.rmtree(blob_directory, ignore_errors=True)
            shutil.rmtree(decoded_directory, ignore_errors=True)
        mosaic_path = destination_path + ".mosaic.tif"
        driver = gdal.GetDriverByName("GTiff")
        dataset = driver.Create(
            mosaic_path,
            values.shape[1],
            values.shape[0],
            1,
            gdal.GDT_Float32,
        )
        dataset.SetGeoTransform(
            (
                origin_x + x_min * tile_span,
                resolution,
                0.0,
                origin_y - y_min * tile_span,
                0.0,
                -resolution,
            )
        )
        spatial_reference = osr.SpatialReference()
        spatial_reference.ImportFromEPSG(tile_epsg)
        dataset.SetProjection(spatial_reference.ExportToWkt())
        band = dataset.GetRasterBand(1)
        band.SetNoDataValue(-32768.0)
        band.WriteArray(values)
        band.FlushCache()
        dataset = None
        warped = warp_vsicurl_sources_to_geotiff(
            [mosaic_path],
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        )
        try:
            os.remove(mosaic_path)
        except OSError:
            pass
        if not warped:
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [definition.get("tile_url_template")],
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 6: direct_cog (fixed Cloud-Optimized GeoTIFF URLs -> warp)
# =====================================================================
@register_access_strategy("direct_cog")
class DirectCogStrategy:
    """National models published as a few fixed Cloud-Optimized GeoTIFFs.

    The simplest provider family of all: no discovery API, no tiling
    scheme -- the definition lists the COG URL(s) outright (Wales
    publishes its whole 1 m lidar terrain model as ONE country-wide
    Cloud-Optimized GeoTIFF on Azure blob storage) and the fetch is a
    windowed ``/vsicurl/`` read straight out of them, exactly the warp
    core every other strategy uses.  The same all-nodata post-warp
    check as the wcs strategy turns inside-the-box-but-outside-the-data
    airports into cached no-coverage negatives.
    """

    # Windowed /vsicurl reader: eligible for whole-tile overlay fetches.
    supports_wide_area = True

    def _vsicurl_inputs(self, definition):
        return [
            _stac_asset_href_to_vsicurl(url.strip())
            for url in str(definition.get("cog_urls", "")).split(",")
            if url.strip()
        ]

    def discover(self, definition, bounding_box_wgs84):
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        inputs = self._vsicurl_inputs(definition)
        return [{"source": path} for path in inputs] or None

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        if not has_gdal:
            return None
        inputs = self._vsicurl_inputs(definition)
        if not inputs:
            return None
        if not warp_vsicurl_sources_to_geotiff(
            inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        ):
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": inputs,
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 10: wcs_kvp (hand-built GetCoverage for non-standard WCS)
# =====================================================================
@register_access_strategy("wcs_kvp")
class WcsKvpStrategy:
    """GetCoverage by explicit key-value URL for quirky WCS servers.

    Some INSPIRE deployments defeat GDAL's WCS driver (Hesse's
    advertises octet-stream as its native format and the driver's
    negotiation returns empty rasters), yet answer a plain KVP
    GetCoverage perfectly.  The definition spells the WHOLE request
    out as ``wcs_getcoverage_template`` with ``{xmin}/{ymin}/{xmax}/
    {ymax}`` placeholders in ``source_epsg`` coordinates; fetch pads
    the airport box, downloads the returned GeoTIFF and warps it
    through the shared core.
    """

    PAD_M = 60.0

    def _request_url(self, definition, bounding_box_wgs84):
        source_epsg = int(float(definition.get("source_epsg", 25832)))
        (x_min, y_min, x_max, y_max) = transform_bounding_box_to_epsg(
            bounding_box_wgs84, source_epsg
        )
        native = _parse_float(
            definition.get("native_resolution_m"), 1.0
        )
        width = max(
            1, int(round((x_max - x_min + 2 * self.PAD_M) / native))
        )
        height = max(
            1, int(round((y_max - y_min + 2 * self.PAD_M) / native))
        )
        return (
            definition["wcs_getcoverage_template"]
            .replace("{xmin}", repr(round(x_min - self.PAD_M, 2)))
            .replace("{ymin}", repr(round(y_min - self.PAD_M, 2)))
            .replace("{xmax}", repr(round(x_max + self.PAD_M, 2)))
            .replace("{ymax}", repr(round(y_max + self.PAD_M, 2)))
            # ArcGIS exportImage endpoints want an explicit pixel size.
            .replace("{width}", str(min(width, 8000)))
            .replace("{height}", str(min(height, 8000)))
        )

    def discover(self, definition, bounding_box_wgs84):
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        return [{"url": self._request_url(definition, bounding_box_wgs84)}]

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        import requests

        if not has_gdal:
            return None
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        url = self._request_url(definition, bounding_box_wgs84)
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        scratch_path = destination_path + ".getcoverage.tif"
        try:
            response = requests.get(url, timeout=300)
            if response.status_code != 200 or not response.content[
                :4
            ].startswith((b"II*\x00", b"MM\x00*")):
                return None
            with open(scratch_path, "wb") as handle:
                handle.write(response.content)
        except Exception as error:
            UI.vprint(
                1, "   WARNING: WCS GetCoverage failed:", str(error)
            )
            return None
        warped = warp_vsicurl_sources_to_geotiff(
            [scratch_path],
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        )
        try:
            os.remove(scratch_path)
        except OSError:
            pass
        if not warped:
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [url],
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 9: tile_grid_http (deterministic projected kilometre tiles)
# =====================================================================
@register_access_strategy("tile_grid_http")
class TileGridHttpStrategy:
    """Deterministic per-kilometre tile downloads on a projected grid.

    The German Länder pattern: bare-earth GeoTIFF tiles named by their
    lower-left kilometre coordinate in the UTM CRS (Bavaria's
    ``{easting_km}_{northing_km}.tif``, Thuringia's zipped epochs, the
    NRW year-stamped files).  Discovery is pure arithmetic --
    transform the airport box to ``source_epsg``, floor to the
    ``tile_size_km`` grid -- refined by either a HEAD probe per
    candidate (missing water/border tiles must not fail the mosaic) or
    a one-time cached directory index (``index_url``) where filenames
    carry unpredictable tokens like NRW's per-tile acquisition year.
    Fetch reads the tiles remotely through ``/vsicurl/`` (wrapped in
    ``/vsizip/`` when ``zip_inner_suffix`` says the GeoTIFF sits
    inside a per-tile zip) and warps through the shared core.
    """

    MAXIMUM_TILES_PER_FETCH = 120

    def index_path(self, definition):
        return os.path.join(
            FNAMES.Elevation_dir,
            definition["code"].lower() + "_tile_grid_index.json",
        )

    def _tile_names_from_index(self, definition):
        """The cached filename list of the provider's directory index."""
        import requests

        index_url = definition.get("index_url")
        if not index_url:
            return None
        try:
            with open(self.index_path(definition), "r") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
        try:
            response = requests.get(index_url, timeout=120)
        except Exception as error:
            UI.vprint(
                1, "   WARNING: tile index request failed:", str(error)
            )
            return None
        if response.status_code != 200:
            return None
        names = []

        def _collect(node):
            if isinstance(node, dict):
                for value in node.values():
                    _collect(value)
            elif isinstance(node, list):
                for value in node:
                    _collect(value)
            elif isinstance(node, str) and any(
                token in node.lower() for token in (".tif", ".zip", ".xyz")
            ):
                names.append(node)

        try:
            _collect(response.json())
        except ValueError:
            # Not JSON: scrape filename-looking tokens out of an HTML
            # or plain-text directory listing (the Apache index case).
            import re

            names.extend(
                re.findall(
                    r"[\w\-\.]+\.(?:tif|zip|xyz)", response.text
                )
            )
            names = sorted(set(names))
        if not names:
            return None
        os.makedirs(
            os.path.dirname(self.index_path(definition)), exist_ok=True
        )
        with open(self.index_path(definition), "w") as handle:
            json.dump(names, handle)
        return names

    def discover(self, definition, bounding_box_wgs84):
        import requests

        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        source_epsg = int(float(definition.get("source_epsg", 25832)))
        tile_size_km = int(float(definition.get("tile_size_km", 1)))
        (x_min, y_min, x_max, y_max) = transform_bounding_box_to_epsg(
            bounding_box_wgs84, source_epsg
        )
        # Grid anchor offsets, in km: Baden-Wuerttemberg's 2 km tiles
        # are anchored at ODD easting kilometres.
        offset_e = int(float(definition.get("grid_easting_offset_km", 0)))
        offset_n = int(float(definition.get("grid_northing_offset_km", 0)))

        def _grid_range(minimum_m, maximum_m, offset_km):
            first = (
                int((minimum_m / 1000.0 - offset_km) // tile_size_km)
                * tile_size_km
                + offset_km
            )
            last = (
                int((maximum_m / 1000.0 - offset_km) // tile_size_km)
                * tile_size_km
                + offset_km
            )
            return range(first, last + tile_size_km, tile_size_km)

        eastings = _grid_range(x_min, x_max, offset_e)
        northings = _grid_range(y_min, y_max, offset_n)
        candidates = [
            (easting, northing)
            for easting in eastings
            for northing in northings
        ]
        if len(candidates) > self.MAXIMUM_TILES_PER_FETCH:
            UI.vprint(
                1,
                "   WARNING: tile-grid fetch of",
                len(candidates),
                "tiles exceeds the cap - skipping this source.",
            )
            return None
        index_names = self._tile_names_from_index(definition)
        template = definition["tile_url_template"]
        headers = self._http_headers(definition)
        sources = []
        session = requests.Session()
        for (easting, northing) in candidates:
            if index_names is not None:
                token = definition.get(
                    "index_token_template", "_{easting_km}_{northing_km}_"
                ).replace("{easting_km}", str(easting)).replace(
                    "{northing_km}", str(northing)
                )
                matches = [
                    name for name in index_names if token in name
                ]
                if not matches:
                    continue
                best_match = sorted(matches)[-1]
                # Index entries that are already full URLs (the
                # Schleswig-Holstein GeoJSON) need no template at all.
                if best_match.startswith("http"):
                    url = best_match
                else:
                    url = template.replace("{file_name}", best_match)
            else:
                northing_index_offset = int(
                    float(definition.get("grid_northing_index_offset", 0))
                )
                url = (
                    template.replace("{easting_km}", str(easting))
                    .replace("{northing_km}", str(northing))
                    # Austria's tiles are named in full metres.
                    .replace("{easting_m}", str(easting * 1000))
                    .replace("{northing_m}", str(northing * 1000))
                    # Espirito Santo's blocks are named by grid INDEX
                    # (coordinate // tile size), northing from the top.
                    .replace(
                        "{easting_index}",
                        str(easting // tile_size_km),
                    )
                    .replace(
                        "{northing_index}",
                        str(
                            northing // tile_size_km
                            + northing_index_offset
                        ),
                    )
                )
                if not self._tile_exists(
                    definition, session, headers, url
                ):
                    continue
            sources.append({"url": url})
        return sources or None

    def _http_headers(self, definition):
        """Optional per-provider request headers, ``Name: value;;...``.

        Saxony's geocloud is a PUBLIC Nextcloud share whose WebDAV path
        expects the (public) share token as a Basic-authorization user
        -- not a personal credential, just the same token that is in
        the public URL, so it may live in the definition file.
        """
        headers = {}
        for pair in str(definition.get("http_headers", "")).split(";;"):
            if ":" in pair:
                (name, value) = pair.split(":", 1)
                headers[name.strip()] = value.strip()
        return headers or None

    def _tile_exists(self, definition, session, headers, url):
        """Does a candidate tile URL exist?  Water/border tiles do not.

        ``probe_mode``: ``head`` (default), ``ranged_get`` (hosts that
        reject HEAD -- Saxony's answers 401 to it), ``gdal_open``
        (templates that are GDAL virtual paths rather than plain URLs,
        e.g. members inside one big remote zip), or ``none``.
        """
        probe_mode = str(definition.get("probe_mode", "head")).lower()
        if url.startswith("/vsi") or probe_mode == "gdal_open":
            try:
                dataset = gdal.Open(url)
                return dataset is not None
            except Exception:
                return False
        if probe_mode == "none":
            return True
        try:
            if probe_mode == "ranged_get":
                request_headers = dict(headers or {})
                request_headers["Range"] = "bytes=0-0"
                probe = session.get(
                    url,
                    timeout=30,
                    headers=request_headers,
                    stream=True,
                )
                probe.close()
                return probe.status_code in (200, 206)
            probe = session.head(
                url, timeout=30, headers=headers, allow_redirects=True
            )
            return probe.status_code == 200
        except Exception:
            return False

    def _zip_inner_name(self, definition, url):
        """The GeoTIFF member name inside a per-tile zip.

        Zip stem + ``zip_inner_suffix``, minus an optional trailing
        ``zip_inner_strip`` token (Saxony's ``..._sn_tiff.zip`` holds
        ``..._sn.tif``).
        """
        stem = os.path.basename(url.split("?")[0])[: -len(".zip")]
        strip = definition.get("zip_inner_strip")
        if strip and stem.endswith(strip):
            stem = stem[: -len(strip)]
        return stem + definition.get("zip_inner_suffix", ".tif")

    def _warp_input_for(self, definition, url):
        if url.startswith("/vsi"):
            return url
        if definition.get("zip_inner_suffix") and url.lower().endswith(
            ".zip"
        ):
            return (
                "/vsizip//vsicurl/"
                + url
                + "/"
                + self._zip_inner_name(definition, url)
            )
        return "/vsicurl/" + url

    def _download_inputs(
        self, definition, sources, destination_path
    ):
        """``fetch_mode=download``: whole-tile pulls to local scratch.

        For hosts that ignore ranged requests or gate reads behind
        headers GDAL cannot easily carry (Saxony's share, the
        Schleswig-Holstein download script): each tile is downloaded
        with the definition's headers, and the warp input is the local
        file (or the GeoTIFF member inside the local zip).  Returns
        ``(warp_inputs, scratch_paths)``.
        """
        import requests

        headers = self._http_headers(definition)
        zip_inner_suffix = definition.get("zip_inner_suffix")
        warp_inputs = []
        scratch_paths = []
        for (number, entry) in enumerate(sources):
            suffix = definition.get("download_suffix") or (
                ".zip"
                if ".zip" in entry["url"].lower()
                else os.path.splitext(entry["url"].split("?")[0])[1]
                or ".dat"
            )
            scratch_path = destination_path + ".tile%d%s" % (number, suffix)
            try:
                response = requests.get(
                    entry["url"], timeout=300, headers=headers
                )
                if response.status_code != 200:
                    continue
                with open(scratch_path, "wb") as handle:
                    handle.write(response.content)
            except Exception as error:
                UI.vprint(
                    1,
                    "   WARNING: could not download elevation tile",
                    entry["url"][:90],
                    ":",
                    str(error),
                )
                continue
            scratch_paths.append(scratch_path)
            member_glob = definition.get("zip_member_glob")
            if member_glob and suffix == ".zip":
                # Members may sit under an inner folder (the
                # Baden-Wuerttemberg zips do) -- walk two levels.
                def _matching_members(root, depth=0):
                    for member in gdal.ReadDir(root) or []:
                        entry = root + "/" + member
                        if member.lower().endswith(member_glob):
                            yield entry
                        elif depth < 2 and "." not in member:
                            yield from _matching_members(entry, depth + 1)

                warp_inputs.extend(
                    _matching_members("/vsizip/" + scratch_path)
                )
            elif zip_inner_suffix and suffix == ".zip":
                warp_inputs.append(
                    "/vsizip/"
                    + scratch_path
                    + "/"
                    + self._zip_inner_name(definition, entry["url"])
                )
            else:
                warp_inputs.append(scratch_path)
        return (warp_inputs, scratch_paths)

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
        scratch_paths = []
        if str(definition.get("fetch_mode", "")).lower() == "download":
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
            (warp_inputs, scratch_paths) = self._download_inputs(
                definition, sources, destination_path
            )
        else:
            warp_inputs = [
                self._warp_input_for(definition, entry["url"])
                for entry in sources
            ]
        source_srs = definition.get("warp_source_epsg")
        warped = bool(warp_inputs) and warp_vsicurl_sources_to_geotiff(
            warp_inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
            source_srs=(
                "EPSG:" + str(int(float(source_srs))) if source_srs else None
            ),
        )
        for scratch_path in scratch_paths:
            try:
                os.remove(scratch_path)
            except OSError:
                pass
        if not warped:
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [entry["url"] for entry in sources],
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 8: geojson_tile_index (one tile catalog file, direct URLs)
# =====================================================================
def _geojson_geometry_bounding_box(geometry):
    """The (west, south, east, north) envelope of a GeoJSON geometry."""
    longitudes = []
    latitudes = []

    def _walk(node):
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(value, (int, float)) for value in node[:2])
        ):
            longitudes.append(float(node[0]))
            latitudes.append(float(node[1]))
        elif isinstance(node, (list, tuple)):
            for child in node:
                _walk(child)

    _walk((geometry or {}).get("coordinates") or [])
    if not longitudes:
        return None
    return (
        min(longitudes),
        min(latitudes),
        max(longitudes),
        max(latitudes),
    )


@register_access_strategy("geojson_tile_index")
class GeojsonTileIndexStrategy:
    """One national GeoJSON tile catalog whose features carry file URLs.

    Uruguay's national terrain model publishes exactly this: a single
    (few-megabyte) GeoJSON of ~6600 tile footprints in WGS84, each
    feature holding the direct GeoTIFF URL.  The catalog is fetched
    once, reduced to ``(bounding_box, url)`` pairs and cached in a
    per-provider index file; fetches then warp the intersecting tiles
    straight off the server through ``/vsicurl/`` (the host honours
    ranged reads).
    """

    # Windowed /vsicurl reader (inherited by os_grid_bucket): eligible for
    # whole-tile overlay fetches.
    supports_wide_area = True

    def index_path(self, definition):
        return os.path.join(
            FNAMES.Elevation_dir,
            definition["code"].lower() + "_geojson_tile_index.json",
        )

    def _tile_entries(self, definition):
        import requests

        try:
            with open(self.index_path(definition), "r") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
        try:
            response = requests.get(definition["index_url"], timeout=180)
        except Exception as error:
            UI.vprint(
                1, "   WARNING: tile catalog request failed:", str(error)
            )
            return None
        if response.status_code != 200:
            return None
        try:
            features = response.json().get("features") or []
        except ValueError:
            return None
        url_property = definition.get("url_property", "url")
        entries = []
        for feature in features:
            url = (feature.get("properties") or {}).get(url_property)
            box = _geojson_geometry_bounding_box(feature.get("geometry"))
            if url and box:
                entries.append({"bbox": list(box), "url": url})
        if not entries:
            return None
        os.makedirs(
            os.path.dirname(self.index_path(definition)), exist_ok=True
        )
        with open(self.index_path(definition), "w") as handle:
            json.dump(entries, handle)
        return entries

    def discover(self, definition, bounding_box_wgs84):
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        entries = self._tile_entries(definition)
        if not entries:
            return None
        hits = [
            entry
            for entry in entries
            if _bounding_boxes_intersect(entry["bbox"], bounding_box_wgs84)
        ]
        return hits or None

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
        if not warp_vsicurl_sources_to_geotiff(
            ["/vsicurl/" + entry["url"] for entry in sources],
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        ):
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [entry["url"] for entry in sources],
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 14: arcgis_feature_tiles (feature catalogs with DATA_URL)
# =====================================================================
@register_access_strategy("arcgis_feature_tiles")
class ArcgisFeatureTileStrategy:
    """ArcGIS feature layers whose attributes carry archive URLs.

    Ireland's national lidar is published this way: a folder of
    "Coverage" map services whose polygon layers hold a ``DATA_URL``
    field pointing at zip/7z archives of GeoTIFF tiles.  Discovery
    enumerates the folder ONCE (cached: every layer carrying the URL
    field becomes a query endpoint), then each fetch spatially queries
    those layers for the airport box, downloads the referenced
    archives and warps their terrain-model members through GDAL's
    ``/vsizip`` / ``/vsi7z`` handlers.
    """

    MAXIMUM_ARCHIVES_PER_FETCH = 8

    def index_path(self, definition):
        return os.path.join(
            FNAMES.Elevation_dir,
            definition["code"].lower() + "_feature_layers.json",
        )

    def _query_endpoints(self, definition):
        import requests

        try:
            with open(self.index_path(definition), "r") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
        folder_url = definition["catalog_folder_url"].rstrip("/")
        url_field = definition.get("url_field", "DATA_URL")
        session = requests.Session()
        try:
            folder = session.get(folder_url + "?f=json", timeout=60).json()
        except Exception as error:
            UI.vprint(
                1, "   WARNING: catalog folder query failed:", str(error)
            )
            return None
        service_names = sorted(
            {
                service.get("name")
                for service in folder.get("services", [])
                if service.get("name")
                and "coverage" in service.get("name", "").lower()
            }
        )
        UI.vprint(
            1,
            "    Indexing",
            len(service_names),
            "lidar coverage catalogs (once per install).",
        )
        root = folder_url.rsplit("/", 1)[0]
        endpoints = []
        for name in service_names:
            service_url = root + "/" + name + "/MapServer"
            try:
                service = session.get(
                    service_url + "?f=json", timeout=60
                ).json()
            except Exception:
                continue
            for layer in service.get("layers", []):
                layer_url = service_url + "/" + str(layer.get("id"))
                try:
                    layer_meta = session.get(
                        layer_url + "?f=json", timeout=60
                    ).json()
                except Exception:
                    continue
                fields = [
                    field.get("name", "")
                    for field in layer_meta.get("fields", []) or []
                ]
                if url_field in fields:
                    endpoints.append(layer_url)
        if not endpoints:
            return None
        os.makedirs(
            os.path.dirname(self.index_path(definition)), exist_ok=True
        )
        with open(self.index_path(definition), "w") as handle:
            json.dump(endpoints, handle)
        return endpoints

    def discover(self, definition, bounding_box_wgs84):
        import requests

        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        endpoints = self._query_endpoints(definition)
        if not endpoints:
            return None
        url_field = definition.get("url_field", "DATA_URL")
        (west, south, east, north) = bounding_box_wgs84
        session = requests.Session()
        archives = []
        seen = set()
        for layer_url in endpoints:
            query = (
                layer_url
                + "/query?geometry=%s,%s,%s,%s" % (west, south, east, north)
                + "&geometryType=esriGeometryEnvelope&inSR=4326"
                + "&spatialRel=esriSpatialRelIntersects&outFields="
                + url_field
                + "&returnGeometry=false&f=json"
            )
            try:
                payload = session.get(query, timeout=60).json()
            except Exception:
                continue
            for feature in payload.get("features", []) or []:
                archive_url = (feature.get("attributes") or {}).get(
                    url_field
                )
                if archive_url and archive_url not in seen:
                    seen.add(archive_url)
                    archives.append({"url": archive_url})
        return archives[: self.MAXIMUM_ARCHIVES_PER_FETCH] or None

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        import requests

        if not has_gdal:
            return None
        sources = self.discover(definition, bounding_box_wgs84)
        if not sources:
            return None
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        scratch_paths = []
        warp_inputs = []
        member_filter = str(
            definition.get("member_filter", "dtm")
        ).lower()
        for (number, entry) in enumerate(sources):
            suffix = os.path.splitext(entry["url"].split("?")[0])[1] or ".zip"
            scratch_path = destination_path + ".arch%d%s" % (number, suffix)
            try:
                response = requests.get(entry["url"], timeout=600)
                if response.status_code != 200:
                    continue
                with open(scratch_path, "wb") as handle:
                    handle.write(response.content)
            except Exception as error:
                UI.vprint(
                    1,
                    "   WARNING: could not download lidar archive",
                    entry["url"][:90],
                    ":",
                    str(error),
                )
                continue
            scratch_paths.append(scratch_path)
            handler = "/vsi7z/" if suffix.lower() == ".7z" else "/vsizip/"

            def _tif_members(root, depth=0):
                for member in gdal.ReadDir(root) or []:
                    entry_path = root + "/" + member
                    if member.lower().endswith((".tif", ".tiff")):
                        yield entry_path
                    elif depth < 2 and "." not in member:
                        yield from _tif_members(entry_path, depth + 1)

            members = list(_tif_members(handler + scratch_path))
            preferred = [
                member
                for member in members
                if member_filter in os.path.basename(member).lower()
            ]
            # Members with UNDECLARED fill values would contaminate the
            # warp's interpolation: sniff each member and declare the
            # fill on a VRT wrapper so resampling never blends across
            # it.  A minimum below any real land elevation is a fill;
            # otherwise the definition's source_nodata (Ireland: -99)
            # is declared when the band carries none of its own.
            fallback_nodata = _parse_float(
                definition.get("source_nodata")
            )
            for member in preferred or members:
                declared_input = member
                try:
                    member_dataset = gdal.Open(member)
                    band = member_dataset.GetRasterBand(1)
                    declared = band.GetNoDataValue()
                    (minimum, _maximum) = band.ComputeRasterMinMax(True)
                    fill = None
                    if minimum < -430.0:
                        # Below any land on Earth: the minimum IS the fill.
                        fill = minimum
                    elif (
                        fallback_nodata is not None
                        and abs(minimum - fallback_nodata) < 0.5
                    ):
                        # The definition's known fill is present -- even
                        # when the band DECLARES something else (one
                        # Irish campaign declares 0.0 while filling with
                        # -99, which would also void real sea-level
                        # pixels).
                        fill = fallback_nodata
                    if fill is not None and (
                        declared is None or abs(declared - fill) > 0.5
                    ):
                        vrt_path = destination_path + ".m%d.vrt" % len(
                            scratch_paths
                        )
                        gdal.Translate(
                            vrt_path,
                            member_dataset,
                            format="VRT",
                            noData=fill,
                        )
                        scratch_paths.append(vrt_path)
                        declared_input = vrt_path
                    member_dataset = None
                except Exception:
                    pass
                warp_inputs.append(declared_input)
        warped = bool(warp_inputs) and warp_vsicurl_sources_to_geotiff(
            warp_inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        )
        for scratch_path in scratch_paths:
            try:
                os.remove(scratch_path)
            except OSError:
                pass
        if not warped:
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [entry["url"] for entry in sources],
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 13: os_grid_bucket (S3 buckets of OS-grid-named GeoTIFFs)
# =====================================================================
_OS_GRID_LETTERS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"  # no I, the OS way


def _ordnance_survey_square_extent(square_name):
    """The (x0, y0, x1, y1) EPSG:27700 extent of an OS grid square name.

    Handles the three forms in Scotland's lidar bucket: two letters +
    two digits (10 km, ``NS16``), the same + a quadrant (5 km,
    ``NS16NE``), and two letters + four digits (1 km, ``NR5712``).
    Returns ``None`` for anything else.
    """
    name = square_name.upper()
    if (
        len(name) < 4
        or name[0] not in _OS_GRID_LETTERS
        or name[1] not in _OS_GRID_LETTERS
    ):
        return None
    first = _OS_GRID_LETTERS.index(name[0])
    second = _OS_GRID_LETTERS.index(name[1])
    easting_100km = ((first - 2) % 5) * 500000 + (second % 5) * 100000
    northing_100km = (19 - (first // 5) * 5) * 100000 - (
        second // 5
    ) * 100000
    rest = name[2:]
    if len(rest) == 4 and rest.isdigit():
        x0 = easting_100km + int(rest[:2]) * 1000
        y0 = northing_100km + int(rest[2:]) * 1000
        return (x0, y0, x0 + 1000, y0 + 1000)
    if len(rest) >= 2 and rest[:2].isdigit():
        x0 = easting_100km + int(rest[0]) * 10000
        y0 = northing_100km + int(rest[1]) * 10000
        quadrant = rest[2:4]
        if quadrant in ("NE", "NW", "SE", "SW"):
            if quadrant[1] == "E":
                x0 += 5000
            if quadrant[0] == "N":
                y0 += 5000
            return (x0, y0, x0 + 5000, y0 + 5000)
        if not rest[2:]:
            return (x0, y0, x0 + 10000, y0 + 10000)
    return None


@register_access_strategy("os_grid_bucket")
class OsGridBucketStrategy(GeojsonTileIndexStrategy):
    """Anonymous S3 buckets of Ordnance-Survey-grid-named GeoTIFFs.

    Scotland's Remote Sensing Portal bucket lays its lidar campaigns
    out as ``lidar/<campaign>/dtm/27700/gridded/<OSSQUARE>_<RES>_...tif``
    -- the square name IS the footprint, so the index is built by
    paginating the bucket listings once, computing each name's extent
    arithmetically, and caching ``(bounding_box, url, resolution)``
    exactly like the GeoJSON-catalog strategy this extends (discover
    and fetch are inherited).  Overlapping campaigns are ordered
    coarse-first so the warp's later-wins mosaic keeps the finest data.
    """

    def _tile_entries(self, definition):
        import re
        import urllib.parse

        import requests

        try:
            with open(self.index_path(definition), "r") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
        bucket_url = definition["bucket_url"].rstrip("/")
        wgs84 = osr.SpatialReference()
        wgs84.ImportFromEPSG(4326)
        wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        projected = osr.SpatialReference()
        projected.ImportFromEPSG(27700)
        projected.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        to_wgs84 = osr.CoordinateTransformation(projected, wgs84)
        entries = []
        UI.vprint(
            1,
            "    Indexing the",
            definition["code"],
            "lidar bucket (once per install).",
        )
        for prefix in str(definition.get("bucket_prefixes", "")).split(","):
            prefix = prefix.strip()
            if not prefix:
                continue
            continuation = None
            while True:
                url = (
                    bucket_url
                    + "/?list-type=2&max-keys=1000&prefix="
                    + urllib.parse.quote(prefix)
                )
                if continuation:
                    url += "&continuation-token=" + urllib.parse.quote(
                        continuation
                    )
                try:
                    response = requests.get(url, timeout=120)
                except Exception as error:
                    UI.vprint(
                        1,
                        "   WARNING: bucket listing failed:",
                        str(error),
                    )
                    return entries or None
                if response.status_code != 200:
                    break
                keys = re.findall(r"<Key>([^<]+)</Key>", response.text)
                for key in keys:
                    if not key.lower().endswith(".tif"):
                        continue
                    base = os.path.basename(key)
                    square = base.split("_")[0]
                    extent = _ordnance_survey_square_extent(square)
                    if extent is None:
                        continue
                    (x0, y0, x1, y1) = extent
                    corners = [
                        to_wgs84.TransformPoint(x, y)
                        for (x, y) in (
                            (x0, y0),
                            (x0, y1),
                            (x1, y0),
                            (x1, y1),
                        )
                    ]
                    resolution = (
                        0.5 if "50CM" in base.upper() else 1.0
                    )
                    entries.append(
                        {
                            "bbox": [
                                min(c[0] for c in corners),
                                min(c[1] for c in corners),
                                max(c[0] for c in corners),
                                max(c[1] for c in corners),
                            ],
                            "url": bucket_url + "/" + key,
                            "resolution": resolution,
                        }
                    )
                token_match = re.search(
                    r"<NextContinuationToken>([^<]+)"
                    r"</NextContinuationToken>",
                    response.text,
                )
                if token_match:
                    continuation = token_match.group(1)
                else:
                    break
        if not entries:
            return None
        # Coarse first: the warp keeps the LAST valid sample, so the
        # finest campaign wins wherever several overlap.
        entries.sort(key=lambda entry: -entry["resolution"])
        os.makedirs(
            os.path.dirname(self.index_path(definition)), exist_ok=True
        )
        with open(self.index_path(definition), "w") as handle:
            json.dump(entries, handle)
        return entries


# =====================================================================
# Strategy 8: wfs_tile_index (WFS tile catalog carrying download URLs)
# =====================================================================
@register_access_strategy("wfs_tile_index")
class WfsTileIndexStrategy:
    """Tile catalogs served over WFS whose features carry download URLs.

    France's LiDAR HD terrain model (IGN Geoplateforme) indexes its
    1 km tiles as WFS features whose ``url`` property is a ready-made
    GeoTIFF request: discovery is one anonymous WFS GetFeature bbox
    query, fetch downloads each returned tile (they are dynamically
    rendered, so no range reads) to temporary files and warps them
    through the shared core.  Coverage grows as the national lidar
    campaign progresses; an empty feature set is an honest no-coverage.
    """

    MAXIMUM_TILES_PER_FETCH = 120

    def _feature_query_url(self, definition, bounding_box_wgs84):
        (west, south, east, north) = bounding_box_wgs84
        return (
            definition["wfs_service_url"]
            + ("&" if "?" in definition["wfs_service_url"] else "?")
            + "SERVICE=WFS&REQUEST=GetFeature&VERSION=2.0.0&TYPENAMES="
            + definition["wfs_type_name"]
            + "&outputFormat=application/json&count="
            + str(self.MAXIMUM_TILES_PER_FETCH)
            # WFS 2.0 with the urn CRS is latitude-first.
            + "&bbox=%s,%s,%s,%s,urn:ogc:def:crs:EPSG::4326"
            % (repr(south), repr(west), repr(north), repr(east))
        )

    def discover(self, definition, bounding_box_wgs84):
        import requests

        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        url_property = definition.get("url_property", "url")
        try:
            response = requests.get(
                self._feature_query_url(definition, bounding_box_wgs84),
                timeout=60,
            )
        except Exception as error:
            UI.vprint(
                1, "   WARNING: WFS tile-index query failed:", str(error)
            )
            return None
        if response.status_code != 200:
            return None
        try:
            features = response.json().get("features") or []
        except ValueError:
            return None
        sources = []
        for feature in features:
            properties = feature.get("properties") or {}
            tile_url = properties.get(url_property)
            if tile_url:
                sources.append(
                    {
                        "url": tile_url,
                        "name": properties.get("name", ""),
                    }
                )
        return sources or None

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        import requests

        if not has_gdal:
            return None
        sources = self.discover(definition, bounding_box_wgs84)
        if not sources:
            return None
        os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        temporary_paths = []
        for (number, entry) in enumerate(sources):
            temporary_path = destination_path + ".tile%d.tif" % number
            try:
                response = requests.get(entry["url"], timeout=300)
                if response.status_code != 200:
                    continue
                with open(temporary_path, "wb") as handle:
                    handle.write(response.content)
                temporary_paths.append(temporary_path)
            except Exception as error:
                UI.vprint(
                    1,
                    "   WARNING: could not download elevation tile",
                    entry.get("name") or entry["url"][:80],
                    ":",
                    str(error),
                )
        warped = bool(temporary_paths) and warp_vsicurl_sources_to_geotiff(
            temporary_paths,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        )
        for temporary_path in temporary_paths:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
        if not warped:
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [entry["name"] or entry["url"] for entry in sources],
            "native_resolution_m": definition.get("native_resolution_m"),
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


def transform_bounding_box_to_epsg(bounding_box_wgs84, source_epsg):
    """A WGS84 (west, south, east, north) box in another projected CRS.

    The envelope of the four transformed corners -- shared by every
    strategy that must pick projected-grid tiles (Taiwan's TWD97
    sheets, the German kilometre tile grids) from a geographic
    request box.
    """
    if source_epsg == 4326:
        return bounding_box_wgs84
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    target = osr.SpatialReference()
    target.ImportFromEPSG(source_epsg)
    target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(wgs84, target)
    (west, south, east, north) = bounding_box_wgs84
    xs = []
    ys = []
    for (longitude, latitude) in (
        (west, south),
        (west, north),
        (east, south),
        (east, north),
    ):
        (x, y, _z) = transform.TransformPoint(longitude, latitude)
        xs.append(x)
        ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


# =====================================================================
# Strategy 7: xyz_archive_drop (manual archives of ASCII grid sheets)
# =====================================================================
@register_access_strategy("xyz_archive_drop")
class XyzArchiveDropStrategy:
    """Manually downloaded archives of ASCII-grid elevation sheets.

    Taiwan's Ministry of the Interior 20 m terrain model is genuinely
    open (Taiwan Open Government Data License) and its INDEX is a
    keyless API -- but the file host (tgos.tw) hard-blocks every
    non-browser client, so the archives must be fetched once by hand
    (the .elv definition's download_page lists them) and dropped into
    ``Elevation_data/<drop_directory_name>/``.

    On first use each dropped zip is extracted and every sheet is
    parsed ONCE: GDAL's XYZ driver reads the ASCII grid (the
    ``xyz_column_order`` definition key handles northing-first files),
    and the sheet is converted to a small GeoTIFF stamped with
    ``source_epsg`` under ``<drop>/converted/``; sheet extents are
    memoised in a per-provider index file.  Airport fetches then warp
    only the intersecting converted sheets -- the ASCII is never
    parsed again.
    """

    def drop_directory(self, definition):
        return os.path.join(
            FNAMES.Elevation_dir,
            definition.get("drop_directory_name", definition["code"]),
        )

    def index_path(self, definition):
        return os.path.join(
            self.drop_directory(definition), "converted", "index.json"
        )

    def manual_setup_information(self, definition):
        """Model data for the GUI's manual-setup affordance."""
        return _manual_drop_setup_information(
            definition,
            self.drop_directory(definition),
            "county or whole-country archives (zip)",
        )

    def _load_index(self, definition):
        try:
            with open(self.index_path(definition), "r") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    def _save_index(self, definition, index):
        os.makedirs(os.path.dirname(self.index_path(definition)), exist_ok=True)
        with open(self.index_path(definition), "w") as handle:
            json.dump(index, handle)

    def _open_ascii_grid(self, definition, sheet_path):
        """Open one extracted sheet through GDAL's XYZ (or native) driver."""
        column_order = str(
            definition.get("xyz_column_order", "AUTO")
        ).upper()
        try:
            return gdal.OpenEx(
                sheet_path,
                allowed_drivers=["XYZ"],
                open_options=["COLUMN_ORDER=" + column_order],
            )
        except Exception:
            pass
        try:
            return gdal.Open(sheet_path)
        except Exception:
            return None

    def _convert_new_archives(self, definition, index):
        """Extract + convert any dropped archive not yet in the index."""
        import zipfile

        drop_directory = self.drop_directory(definition)
        if not os.path.isdir(drop_directory):
            return
        converted_directory = os.path.join(drop_directory, "converted")
        source_epsg = int(float(definition.get("source_epsg", 4326)))
        spatial_reference = osr.SpatialReference()
        spatial_reference.ImportFromEPSG(source_epsg)
        done_archives = index.setdefault("archives", [])
        sheets = index.setdefault("sheets", {})
        # Loose (non-zip) dropped grid files convert the same way --
        # Hamburg publishes its whole-city model as ONE big ASCII file.
        for entry in sorted(os.listdir(drop_directory)):
            if entry in done_archives or not entry.lower().endswith(
                (".xyz", ".txt", ".asc", ".ascii", ".grd", ".tif")
            ):
                continue
            loose_path = os.path.join(drop_directory, entry)
            UI.vprint(
                1,
                "    Converting the dropped elevation file",
                entry,
                "(once; large files take a while).",
            )
            dataset = self._open_ascii_grid(definition, loose_path)
            if dataset is None:
                done_archives.append(entry)
                continue
            converted_directory = os.path.join(drop_directory, "converted")
            os.makedirs(converted_directory, exist_ok=True)
            converted_path = os.path.join(
                converted_directory, entry + ".tif"
            )
            spatial_reference = osr.SpatialReference()
            spatial_reference.ImportFromEPSG(source_epsg)
            try:
                translated = gdal.Translate(
                    converted_path,
                    dataset,
                    format="GTiff",
                    outputType=gdal.GDT_Float32,
                    outputSRS=spatial_reference.ExportToWkt(),
                    creationOptions=["COMPRESS=DEFLATE", "BIGTIFF=IF_SAFER"],
                )
            except Exception as error:
                UI.vprint(
                    1, "   WARNING:", entry, "did not convert:", str(error)
                )
                translated = None
            if translated is not None:
                geotransform = translated.GetGeoTransform()
                x0 = geotransform[0]
                y0 = geotransform[3]
                x1 = x0 + geotransform[1] * translated.RasterXSize
                y1 = y0 + geotransform[5] * translated.RasterYSize
                sheets[converted_path] = [
                    min(x0, x1),
                    min(y0, y1),
                    max(x0, x1),
                    max(y0, y1),
                ]
                translated = None
            dataset = None
            done_archives.append(entry)
        for entry in sorted(os.listdir(drop_directory)):
            if not entry.lower().endswith(".zip") or entry in done_archives:
                continue
            archive_path = os.path.join(drop_directory, entry)
            UI.vprint(
                1,
                "    Converting the dropped elevation archive",
                entry,
                "(once).",
            )
            try:
                archive = zipfile.ZipFile(archive_path, "r")
            except zipfile.BadZipFile:
                UI.vprint(1, "   WARNING: unreadable archive", archive_path)
                done_archives.append(entry)
                continue
            with archive:
                for member in archive.filelist:
                    member_name = os.path.basename(member.filename)
                    if not member_name or member_name.lower().endswith(
                        (".hdr", ".xml", ".pdf", ".doc")
                    ):
                        continue
                    # GeoTIFF members carrying their own CRS (Wallonia's
                    # multi-gigabyte province zips) are indexed IN PLACE
                    # through /vsizip -- no extraction, no conversion.
                    if member_name.lower().endswith((".tif", ".tiff")):
                        vsizip_path = (
                            "/vsizip/" + archive_path + "/" + member.filename
                        )
                        try:
                            in_place = gdal.Open(vsizip_path)
                        except Exception:
                            in_place = None
                        if in_place is not None and in_place.GetProjection():
                            geotransform = in_place.GetGeoTransform()
                            x0 = geotransform[0]
                            y0 = geotransform[3]
                            x1 = x0 + geotransform[1] * in_place.RasterXSize
                            y1 = y0 + geotransform[5] * in_place.RasterYSize
                            sheets[vsizip_path] = [
                                min(x0, x1),
                                min(y0, y1),
                                max(x0, x1),
                                max(y0, y1),
                            ]
                            in_place = None
                            continue
                    scratch_path = os.path.join(
                        converted_directory, member_name + ".scratch"
                    )
                    os.makedirs(converted_directory, exist_ok=True)
                    try:
                        with open(scratch_path, "wb") as out:
                            out.write(archive.open(member, "r").read())
                    except Exception:
                        continue
                    dataset = self._open_ascii_grid(definition, scratch_path)
                    if dataset is None:
                        os.remove(scratch_path)
                        continue
                    converted_path = os.path.join(
                        converted_directory, member_name + ".tif"
                    )
                    try:
                        translated = gdal.Translate(
                            converted_path,
                            dataset,
                            format="GTiff",
                            outputType=gdal.GDT_Float32,
                            # Only ASSIGN a CRS when the sheet has none
                            # of its own (Taiwan's bare grids); sheets
                            # carrying one (Pernambuco spans two UTM
                            # zones) keep it.
                            outputSRS=(
                                None
                                if dataset.GetProjection()
                                else spatial_reference.ExportToWkt()
                            ),
                            creationOptions=["COMPRESS=DEFLATE"],
                        )
                    except Exception as error:
                        UI.vprint(
                            2,
                            "      Sheet",
                            member_name,
                            "did not convert:",
                            str(error),
                        )
                        translated = None
                    if translated is not None:
                        geotransform = translated.GetGeoTransform()
                        x0 = geotransform[0]
                        y0 = geotransform[3]
                        x1 = x0 + geotransform[1] * translated.RasterXSize
                        y1 = y0 + geotransform[5] * translated.RasterYSize
                        sheets[converted_path] = [
                            min(x0, x1),
                            min(y0, y1),
                            max(x0, x1),
                            max(y0, y1),
                        ]
                        translated = None
                    dataset = None
                    os.remove(scratch_path)
            done_archives.append(entry)

    def _bounding_box_in_source_crs(self, definition, bounding_box_wgs84):
        return transform_bounding_box_to_epsg(
            bounding_box_wgs84,
            int(float(definition.get("source_epsg", 4326))),
        )

    def discover(self, definition, bounding_box_wgs84):
        if not has_gdal:
            return None
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        index = self._load_index(definition)
        self._convert_new_archives(definition, index)
        self._save_index(definition, index)
        sheets = index.get("sheets") or {}
        if not sheets:
            UI.vprint(
                1,
                "    "
                + definition["code"]
                + " is a manual-download source: fetch the archives from "
                + definition.get("download_page", "its download page")
                + " in a browser and drop the zip files into "
                + self.drop_directory(definition)
                + " .",
            )
            return None
        source_box = self._bounding_box_in_source_crs(
            definition, bounding_box_wgs84
        )
        hits = [
            path
            for (path, extent) in sheets.items()
            if _bounding_boxes_intersect(extent, source_box)
            and (path.startswith("/vsi") or os.path.isfile(path))
        ]
        return [{"path": path} for path in sorted(hits)] or None

    def fetch(
        self,
        definition,
        bounding_box_wgs84,
        target_resolution_m,
        destination_path,
    ):
        sources = self.discover(definition, bounding_box_wgs84)
        if not sources:
            return None
        if not warp_vsicurl_sources_to_geotiff(
            [entry["path"] for entry in sources],
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        ):
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [entry["path"] for entry in sources],
            "native_resolution_m": definition.get("native_resolution_m"),
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
# Strategy 15: degree_named_cog (deterministic per-degree COG names)
# =====================================================================
@register_access_strategy("degree_named_cog")
class DegreeNamedCogStrategy:
    """Cloud-Optimized GeoTIFFs named by their 1-degree cell coordinates.

    The Copernicus GLO-30 mirror on AWS Open Data publishes one COG per
    1-degree cell with the cell's south-west corner encoded in the object
    name as hemisphere tokens (``N25``/``E051`` style) -- no discovery
    API, no index file: every cell's URL is computable.  Discovery
    enumerates the cells touching the requested box and keeps those whose
    object actually exists (ocean-only cells are simply absent from the
    bucket; one cheap HEAD request per cell separates the two, memoised
    for the process so neighbouring airports in the same cell never
    re-ask).  The fetch is the shared windowed ``/vsicurl/`` warp.

    Deliberately NOT eligible for whole-tile overlay fetches
    (``supports_wide_area = False``): the only shipped user is a global
    SURFACE model (buildings and canopy baked into the heights) whose
    building artifacts are corrected by the airport-scoped footprint
    masking pass -- a tile-wide use would spread uncorrected rooftop
    elevations across every city in the tile.
    """

    supports_wide_area = False

    # Process-lifetime memo of definitive existence answers (HTTP 200 /
    # 404) keyed by URL.  Transient failures (timeouts, 5xx) are NOT
    # memoised: one network blip must not poison every later airport of
    # the run with a false "absent".
    _cell_exists_by_url = {}

    @staticmethod
    def degree_cell_tokens(cell_latitude, cell_longitude):
        """Hemisphere-coded name tokens of a 1-degree cell's SW corner.

        ``(25, 51) -> ("N25", "E051")``; ``(-14, -29) -> ("S14", "W029")``.
        Latitude is zero-padded to 2 digits, longitude to 3, matching the
        Copernicus DEM object-name grammar.
        """
        latitude_token = "%s%02d" % (
            "N" if cell_latitude >= 0 else "S",
            abs(cell_latitude),
        )
        longitude_token = "%s%03d" % (
            "E" if cell_longitude >= 0 else "W",
            abs(cell_longitude),
        )
        return latitude_token, longitude_token

    @staticmethod
    def degree_cells_of_bounding_box(bounding_box_wgs84):
        """The integer SW corners of every 1-degree cell a box touches.

        A box edge lying exactly on an integer degree does not pull in the
        cell beyond it (``ceil`` on the top/right edges).
        """
        import math

        (west, south, east, north) = bounding_box_wgs84
        return [
            (cell_latitude, cell_longitude)
            for cell_latitude in range(
                int(math.floor(south)), max(int(math.ceil(north)),
                                            int(math.floor(south)) + 1)
            )
            for cell_longitude in range(
                int(math.floor(west)), max(int(math.ceil(east)),
                                           int(math.floor(west)) + 1)
            )
        ]

    def _cell_url(self, definition, cell_latitude, cell_longitude):
        latitude_token, longitude_token = self.degree_cell_tokens(
            cell_latitude, cell_longitude
        )
        return str(definition.get("url_template", "")).format(
            latitude_token=latitude_token, longitude_token=longitude_token
        )

    def _url_exists(self, url):
        memo = DegreeNamedCogStrategy._cell_exists_by_url
        if url in memo:
            return memo[url]
        import requests

        try:
            response = requests.head(url, timeout=30)
        except Exception as error:
            UI.vprint(
                1,
                "   WARNING: existence probe failed for",
                url,
                ":",
                str(error),
            )
            return False
        if response.status_code == 200:
            memo[url] = True
        elif response.status_code == 404:
            memo[url] = False
        else:
            UI.vprint(
                1,
                "   WARNING: existence probe for",
                url,
                "returned status",
                response.status_code,
            )
            return False
        return memo[url]

    def discover(self, definition, bounding_box_wgs84):
        if not _coverage_bbox_intersects(definition, bounding_box_wgs84):
            return None
        if not str(definition.get("url_template", "")).strip():
            return None
        sources = [
            {"source": "/vsicurl/" + url, "cell": [cell_latitude, cell_longitude]}
            for (cell_latitude, cell_longitude) in (
                self.degree_cells_of_bounding_box(bounding_box_wgs84)
            )
            for url in [
                self._cell_url(definition, cell_latitude, cell_longitude)
            ]
            if self._url_exists(url)
        ]
        return sources or None

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
        vsicurl_inputs = [entry["source"] for entry in sources]
        if not warp_vsicurl_sources_to_geotiff(
            vsicurl_inputs,
            bounding_box_wgs84,
            target_resolution_m,
            destination_path,
            value_floor_m=float(definition.get("value_floor_m", -600.0)),
        ):
            return None
        if not _geotiff_has_valid_data(destination_path):
            try:
                os.remove(destination_path)
            except OSError:
                pass
            return None
        return {
            "provider": definition.get("code"),
            "access_strategy": definition.get("access_strategy"),
            "source_urls": [
                entry["source"].replace("/vsicurl/", "", 1)
                for entry in sources
            ],
            "native_resolution_m": definition.get("native_resolution_m"),
            "license": definition.get("license"),
            "attribution": definition.get("attribution"),
            "vertical_datum": definition.get("vertical_datum"),
            "datum_note": (
                "Elevations are in the source vertical datum; the source "
                "is treated as truth and is NOT shifted toward the base "
                "DEM."
            ),
            "fetch_date": datetime.date.today().isoformat(),
            "bounding_box_wgs84": list(bounding_box_wgs84),
            "resolution_m": target_resolution_m,
        }


# =====================================================================
# Surface-model building masking (strategy-agnostic post-fetch pass)
# =====================================================================
# Definition flag naming the pass; parsed to bool at registry load.
SURFACE_MODEL_BUILDING_MASKING = "surface_model_building_masking"

# One buffered pixel of a 30 m grid plus a margin for radar layover: the
# X-band return of a building smears roughly one resolution cell beyond
# its walls, so the mask must reach past the mapped footprint.
DEFAULT_FOOTPRINT_MASK_BUFFER_M = 35.0
# Residual structure masking (2026-07-18, SPJC east-side mounds): surface
# models bake UNMAPPED structures into the terrain too — dense city blocks
# outside any OpenStreetMap/package footprint (measured SPJC: a +4-6 m
# plateau of unmapped Lima rooftops right behind the terminal, zero
# footprints within 90 m).  A morphological-opening ground estimate flags
# pixels standing more than the threshold above it; the opening window
# must exceed the widest unmapped structure while staying invariant on
# planar slopes (opening of a plane is the plane, so genuine hillsides
# never mask).  Broad ridges wider than the window are preserved.
DEFAULT_RESIDUAL_MASK_THRESHOLD_M = 2.0
DEFAULT_RESIDUAL_MASK_OPENING_WINDOW_M = 105.0
# Dense-city DSM fabric is CONTIGUOUS rooftop plateau for hundreds of
# metres (measured SPJC east wall: +5-6 m with no ground pixel inside any
# window — 30 m native resolution never sees the streets), so a single
# opening cannot recover ground there.  Iterate: each mask+fill round
# recedes the plateau's SHARP edge by ~half a window from every recovered-
# ground side; gradual (natural) terrain edges produce no opening residual
# and never start eroding, so real sloped-flank mesas are preserved.  The
# pass count bounds the reach (~5 x 52 m = ~260 m) — deep-city plateau
# beyond it keeps its DSM height, which only matters past the graded
# strips' reach where the sim draws the buildings anyway.
DEFAULT_RESIDUAL_MASK_EROSION_PASSES = 12
RESIDUAL_STRUCTURE_MASKING = "residual_structure_masking"


def _residual_structure_mask(
    values,
    exclude_mask,
    pixel_size_m,
    opening_window_m=DEFAULT_RESIDUAL_MASK_OPENING_WINDOW_M,
    threshold_m=DEFAULT_RESIDUAL_MASK_THRESHOLD_M,
    erosion_passes=DEFAULT_RESIDUAL_MASK_EROSION_PASSES,
):
    """Boolean mask of pixels standing above the local ground estimate.

    The ground estimate is a grey (morphological) OPENING of the raster:
    erosion then dilation with a square window of ``opening_window_m``.
    Structures narrower than the window are levelled out of the estimate,
    so their pixels show a positive residual; a planar slope is invariant
    under opening, so genuine hillsides show none; terrain features with
    GRADUAL flanks survive into the estimate and are preserved regardless
    of width.  Contiguous cliff-edged plateau wider than the window
    (dense-city rooftop fabric) is recovered ITERATIVELY: each pass fills
    the masked cells from the nearest trusted ground and re-runs the
    opening, so the plateau's sharp edge recedes ~half a window per pass;
    ``erosion_passes`` bounds the total reach.  ``exclude_mask`` cells
    (genuine nodata) are replaced by the finite median for the estimate —
    they can neither poison the estimate with sentinel lows nor appear in
    the returned mask.

    ESTIMATOR (2026-07-18, second iteration): the ground reference is a
    LOCAL MEDIAN, not a morphological opening — an opening is exactly
    invariant on a half-plane step (erosion shifts the cliff edge, the
    dilation shifts it back), so it can never start biting into wide
    city-plateau fabric.  The median over a symmetric window of a plane
    is its centre value (slopes and gradual flanks are invariant), while
    any pixel whose window is MAJORITY ground gets pulled down to
    ground, which both levels isolated structures and lets the fill
    iteration recede a plateau edge pass by pass.  Computed on a grid
    decimated to ~15 m cells: the interesting sources are 30 m-native
    DSMs oversampled to 3 m, so decimation loses nothing and keeps the
    median filter fast."""
    import numpy
    from scipy import ndimage

    finite = ~exclude_mask
    if not finite.any():
        return numpy.zeros(values.shape, dtype=bool)
    decimation = max(1, int(round(15.0 / max(pixel_size_m, 0.5))))
    small_values = values[::decimation, ::decimation].astype(
        numpy.float64, copy=True)
    small_finite = finite[::decimation, ::decimation]
    if not small_finite.any():
        return numpy.zeros(values.shape, dtype=bool)
    small_pixel_m = pixel_size_m * decimation
    window_pixels = int(round(opening_window_m / small_pixel_m))
    window_pixels = max(3, window_pixels)
    if window_pixels % 2 == 0:
        window_pixels += 1
    border = window_pixels // 2
    small_values[~small_finite] = float(
        numpy.median(small_values[small_finite]))
    small_mask = numpy.zeros(small_values.shape, dtype=bool)
    for _erosion_pass in range(max(1, erosion_passes)):
        # Slope-safe criteria per pass, evaluated over a WINDOW LADDER
        # (1x, 2x, 4x the base window): one window cannot fit every
        # structure scale — a 105 m window around a 150 m warehouse band
        # or inside wide city fabric sees under a quarter of true
        # ground, so its low quartile carries no signal, while the
        # airport's abundant ground IS visible at 420 m.  The uphill and
        # steepness gates below are scale-independent, so growing the
        # quartile's reach never unprotects slopes.
        #   BUMP — the pixel stands above the base-window MEDIAN
        #   (features covering under half the window; the median of a
        #   plane is its centre, so slopes never fire);
        #   EDGE BITE — the pixel stands above SOME window's LOW
        #   QUARTILE (ground visible at that scale), has nothing
        #   significantly higher within ~30 m uphill (plateau top, not
        #   mid-slope — pixel-relative, so staircase levels recede
        #   top-down across passes), and drops by the threshold within
        #   ~30 m somewhere nearby (a real structure edge; natural
        #   flanks up to ~6 % never do — steeper cliff-flanked terrain
        #   accepts a bounded shoulder band being re-interpolated,
        #   which inside an airport-neighbourhood inset is the right
        #   trade).
        window_median = ndimage.median_filter(
            small_values, size=window_pixels, mode="nearest"
        )
        ground_visible_below = numpy.zeros(small_values.shape, dtype=bool)
        believed_ground = None
        for scale in (1, 2, 4):
            ladder_window = window_pixels * scale
            if ladder_window % 2 == 0:
                ladder_window += 1
            low_quartile = ndimage.percentile_filter(
                small_values, 25, size=ladder_window, mode="nearest"
            )
            ground_visible_below |= (
                (small_values - low_quartile) > threshold_m
            )
            believed_ground = (low_quartile if believed_ground is None
                               else numpy.minimum(believed_ground,
                                                  low_quartile))
        uphill_is_flat = (
            (ndimage.grey_dilation(small_values, size=5, mode="nearest")
             - small_values) < threshold_m / 2.0
        )
        local_drop = small_values - ndimage.grey_erosion(
            small_values, size=5, mode="nearest"
        )
        pass_mask = ((
            ((small_values - window_median) > threshold_m)
            | (ground_visible_below
               & uphill_is_flat
               & (local_drop > threshold_m))
        ) & small_finite & ~small_mask)
        # Nearest-padding makes the window asymmetric at the raster
        # border (a slope reads high there), which would mask and
        # re-fill a border strip and mint an inset-boundary seam; the
        # estimate is untrustworthy there, so the border band never
        # masks.
        if border > 0:
            pass_mask[:border, :] = False
            pass_mask[-border:, :] = False
            pass_mask[:, :border] = False
            pass_mask[:, -border:] = False
        if not pass_mask.any():
            break
        small_mask |= pass_mask
        # Substitute the BELIEVED GROUND at the newly masked cells (a
        # nearest-source fill would copy plateau values back in from the
        # plateau side and stall the recession); the next pass then sees
        # the edge receded.  The real raster fill happens once, in the
        # caller, over the accumulated mask.
        small_values = small_values.copy()
        small_values[pass_mask] = believed_ground[pass_mask]
    if not small_mask.any():
        return numpy.zeros(values.shape, dtype=bool)
    full_mask = numpy.kron(
        small_mask,
        numpy.ones((decimation, decimation), dtype=bool),
    )[: values.shape[0], : values.shape[1]]
    if full_mask.shape != values.shape:
        padded = numpy.zeros(values.shape, dtype=bool)
        padded[: full_mask.shape[0], : full_mask.shape[1]] = full_mask
        full_mask = padded
    return full_mask & finite

# Upper bound, in pixels, on how far the legacy gdal.FillNodata inpainting
# looks for valid ground values.  100 pixels at a 30 m grid is 3 km --
# beyond any single terminal complex while keeping gdal.FillNodata cheap.
DEFAULT_FOOTPRINT_FILL_SEARCH_PIXELS = 100

# The masked-hole fill algorithm.  The default vectorized distance-transform
# fill replaces every masked (building) cell with the value of its nearest
# trusted-ground cell in one O(N) exact-Euclidean pass, then applies a fixed
# number of deterministic masked smoothing passes -- reproducing what
# gdal.FillNodata does (interpolate holes from surrounding ground) as a
# single deterministic array operation instead of an iterative search whose
# cost grows with maxSearchDist.  The result is exactly reproducible (no
# floating iteration-order dependence) and never touches unmasked cells.
INSET_FILL_METHOD_DISTANCE_TRANSFORM = "distance_transform"
INSET_FILL_METHOD_LEGACY = "gdal_fillnodata"

# Smoothing passes applied to the filled cells only, matching the legacy
# gdal.FillNodata(smoothingIterations=2) so the two paths stay comparable.
DEFAULT_FILL_SMOOTHING_ITERATIONS = 2


def _inset_fill_method():
    """The masked-hole fill method, environment-overridable (default-on).

    ``O4_INSET_FILL_METHOD=gdal_fillnodata`` restores the legacy in-place
    ``gdal.FillNodata`` inpaint (the byte-for-byte fallback); any other or
    absent value selects the vectorized distance-transform fill.
    """
    value = os.environ.get("O4_INSET_FILL_METHOD", "").strip().lower()
    if value == INSET_FILL_METHOD_LEGACY:
        return INSET_FILL_METHOD_LEGACY
    return INSET_FILL_METHOD_DISTANCE_TRANSFORM


def _fill_masked_by_distance_transform(
    values, source_mask, smoothing_iterations=DEFAULT_FILL_SMOOTHING_ITERATIONS
):
    """Fill every non-source cell from its nearest trusted-ground cell.

    SOURCE-AGNOSTIC by contract: it takes a boolean ``source_mask`` marking
    the cells whose values are trusted ground, and knows nothing about where
    the holes came from (building footprints, package objects, both).  Every
    cell outside ``source_mask`` is overwritten with the value of the nearest
    source cell in exact Euclidean distance (``scipy.ndimage`` distance
    transform with ``return_indices``); the ground under an airport terminal
    is nearly planar, so a nearest-ground value is an excellent estimate --
    the same assumption the legacy fill relied on.  ``smoothing_iterations``
    deterministic 3x3 masked-mean passes then soften the piecewise-constant
    Voronoi seams; each pass only ever writes non-source cells, so every
    source (unmasked) cell stays byte-identical.

    Returns a new float64 array; ``values`` is not mutated.  The caller
    restores genuine nodata afterwards, exactly as with gdal.FillNodata
    (which also fills every non-source cell and lets the caller re-stamp the
    sentinel).  Determinism: the distance transform and the fixed-count
    box-mean passes have no iteration-order or thread dependence.
    """
    from scipy import ndimage

    filled = numpy.asarray(values, dtype=numpy.float64).copy()
    source_mask = numpy.asarray(source_mask, dtype=bool)
    fill_mask = ~source_mask
    if not source_mask.any() or not fill_mask.any():
        # No trusted ground to source from, or nothing to fill: unchanged.
        return filled
    # Nearest source-cell index for every cell (exact Euclidean).  The EDT
    # runs on the complement of the source mask, so each cell's returned
    # index is that of the closest source cell.
    nearest_indices = ndimage.distance_transform_edt(
        fill_mask, return_distances=False, return_indices=True
    )
    nearest_values = filled[tuple(nearest_indices)]
    filled[fill_mask] = nearest_values[fill_mask]
    # Masked smoothing: average over a 3x3 window but write only the filled
    # cells, so source cells (and thus every unmasked pixel) are untouched.
    for _ in range(int(smoothing_iterations)):
        blurred = ndimage.uniform_filter(filled, size=3, mode="nearest")
        filled[fill_mask] = blurred[fill_mask]
    return filled


_BUILDING_QUERY_STATEMENTS = ['way["building"]', 'rel["building"]']


def _load_building_layer_from_extracts(osm_layer, bbox_south_west_north_east):
    """Populate ``osm_layer`` with buildings from local Geofabrik extracts.

    ``bbox_south_west_north_east`` may also be a LIST of such boxes; the
    extracts backend then serves all of them in one filtering pass (the
    tile-level footprint prefetch batches every airport's box this way).

    The airport-inset footprint query previously went straight to Overpass,
    bypassing the regional-extract accelerator the tile vector pipeline
    already uses -- and for a large airport box that meant multi-minute
    Overpass queue waits (the profiled dominant cost of a cold inset build).
    When the downloaded extracts cover the box (the common airport case),
    the ``building`` ways/relations are filtered out of the local pbf
    instead, which is bounded by local disk I/O.  Returns ``True`` when the
    layer was populated locally, ``False`` to fall through to Overpass --
    the accelerator is never a dependency (missing index, region not stored
    yet, or any failure all read as ``False``).
    """
    try:
        import O4_OSM_Extracts as EXTRACTS
    except Exception:
        return False
    try:
        xml_bytes = EXTRACTS.osm_xml_from_local_extracts(
            _BUILDING_QUERY_STATEMENTS,
            bbox_south_west_north_east,
            request_description="inset_buildings",
        )
    except Exception:
        return False
    if not xml_bytes:
        return False
    # Mirror OSM_query_to_OSM_layer's tag setup for this fixed query so
    # update_dicosm keeps every building way/relation and its child nodes.
    building_tags = {"n": [], "w": [("building", "")], "r": [("building", "")]}
    try:
        osm_layer.update_dicosm(xml_bytes, building_tags, building_tags)
    except Exception:
        return False
    return True


def _building_footprint_polygons_from_layer(osm_layer):
    """Convert a populated building OSM layer to shapely polygons.

    Plain (longitude, latitude) coordinates (``OSM_to_MultiPolygon`` with
    a zero origin, so nothing here is tile-relative); ``[]`` on failure.
    """
    import O4_OSM_Utils as OSM

    try:
        footprints = OSM.OSM_to_MultiPolygon(osm_layer, 0, 0)
    except Exception as error:
        UI.vprint(
            1,
            "   WARNING: OpenStreetMap building polygons unreadable:",
            str(error),
        )
        return []
    return [polygon for polygon in getattr(footprints, "geoms", []) if polygon.area]


def openstreetmap_building_footprints(
    bounding_box_wgs84, footprint_prefetch=None
):
    """Absolute-WGS84 building footprint polygons from OpenStreetMap.

    Returns a list of shapely polygons in plain (longitude, latitude)
    coordinates for every ``building`` way and relation in the box
    (``OSM_to_MultiPolygon`` with a zero origin, so nothing here is
    tile-relative).  The data is served from the local Geofabrik regional
    extracts when they cover the box, and from Overpass otherwise; returns
    ``[]`` on any failure -- the caller then skips the masking pass rather
    than failing the fetch: an uncorrected surface-model inset is still
    better than no inset.

    When ``footprint_prefetch`` (a :class:`TileBuildingFootprintPrefetch`)
    can serve the box, the polygons come from its one shared extract pass
    instead of a fresh per-box pass -- the per-box pbf filtering cost is
    box-size-INDEPENDENT (three full osmium reads of the regional
    extract), so a tile with N airports otherwise pays that read N times.
    A prefetch answer of ``None`` (box not covered, extracts unable to
    serve) falls through to the unchanged per-box path.

    Deliberately NOT cached on disk: an inset fetch is already a rare,
    cached event, and reusing a footprint file fetched for a smaller
    margin would silently miss buildings in the enlarged ring (the exact
    staleness class the margin-aware inset cache invalidation fixed).
    """
    if footprint_prefetch is not None:
        prefetched = footprint_prefetch.footprints_intersecting_box(
            bounding_box_wgs84
        )
        if prefetched is not None:
            return prefetched
    import O4_OSM_Utils as OSM

    (west, south, east, north) = bounding_box_wgs84
    bbox = (south, west, north, east)
    osm_layer = OSM.OSM_layer()
    if not _load_building_layer_from_extracts(osm_layer, bbox):
        try:
            queried = OSM.OSM_query_to_OSM_layer(
                _BUILDING_QUERY_STATEMENTS, bbox, osm_layer,
            )
        except Exception as error:
            UI.vprint(
                1,
                "   WARNING: OpenStreetMap building query failed:",
                str(error),
            )
            return []
        if not queried:
            return []
    return _building_footprint_polygons_from_layer(osm_layer)


class TileBuildingFootprintPrefetch:
    """ONE regional-extract pass serving every airport's footprint query.

    Each per-airport OpenStreetMap building query independently filters
    the ENTIRE regional pbf (three osmium passes over hundreds of
    megabytes), and that cost does not depend on the box size -- so a
    tile with N airports paid the full read N times (93% of the profiled
    cold +25+051 inset build).  This prefetch runs the filter once with
    the full LIST of airport boxes -- never their bounding rectangle,
    which would sweep up every building between airports in a metro tile
    -- and answers each airport's query by clipping in memory.

    Lazy: nothing is read until the first query, so builds that never
    reach a masking pass (warm caches, no surface-model provider) never
    pay the extract pass.  Failure-neutral: when the extracts cannot
    serve (backend disabled, region not downloaded, osmium missing),
    every query returns ``None`` and the caller falls back to the
    unchanged per-box path (extracts, then Overpass).  Margin semantics
    are preserved because the boxes given here are the same margin-grown
    boxes each per-airport fetch would have queried with.
    """

    def __init__(self, bounding_boxes_wgs84):
        """``bounding_boxes_wgs84``: iterable of (west, south, east,
        north) boxes, one per airport."""
        self._boxes = [
            tuple(float(value) for value in box)
            for box in bounding_boxes_wgs84
        ]
        self._load_attempted = False
        # None until a successful load; a list afterwards.
        self._footprints = None

    def _box_is_covered(self, bounding_box_wgs84):
        """True when the request sits inside one of the construction
        boxes (tolerance for float round-trips)."""
        (west, south, east, north) = (
            float(value) for value in bounding_box_wgs84
        )
        epsilon = 1e-9
        for (p_west, p_south, p_east, p_north) in self._boxes:
            if (
                west >= p_west - epsilon
                and south >= p_south - epsilon
                and east <= p_east + epsilon
                and north <= p_north + epsilon
            ):
                return True
        return False

    def _load_once(self):
        if self._load_attempted:
            return
        self._load_attempted = True
        import O4_OSM_Utils as OSM

        osm_layer = OSM.OSM_layer()
        boxes_south_west_north_east = [
            (south, west, north, east)
            for (west, south, east, north) in self._boxes
        ]
        if not _load_building_layer_from_extracts(
            osm_layer, boxes_south_west_north_east
        ):
            return
        self._footprints = _building_footprint_polygons_from_layer(osm_layer)

    def footprints_intersecting_box(self, bounding_box_wgs84):
        """Prefetched footprints intersecting the box, or ``None`` when
        the prefetch cannot serve it (caller falls back per-box)."""
        if not self._boxes or not self._box_is_covered(bounding_box_wgs84):
            return None
        self._load_once()
        if self._footprints is None:
            return None
        from shapely.geometry import box as shapely_box

        (west, south, east, north) = bounding_box_wgs84
        clip_box = shapely_box(west, south, east, north)
        selected = []
        for polygon in self._footprints:
            (b_west, b_south, b_east, b_north) = polygon.bounds
            if (
                b_east < west
                or b_west > east
                or b_north < south
                or b_south > north
            ):
                continue
            if polygon.intersects(clip_box):
                selected.append(polygon)
        return selected


def _xplane_root_for_package_footprints():
    """X-Plane installation root from download-time configuration.

    The inset fetch runs at step 1, before any DSF is read, so there is no
    build context to hand a pack root in -- the root has to come from plain
    configuration.  The resolution order mirrors what ``O4_Vector_Map``
    already does to locate CIFP data (the reverse direction of the same
    derivation): ``cifp_data_path`` walked up two levels via
    ``auto_patch.cifp_reader.xplane_root_from_cifp_path``, then the parent
    of ``custom_scenery_dir``.  A CIFP path pointing outside an X-Plane
    install (a Navigraph folder) is rejected by requiring ``Custom
    Scenery`` under the derived root, and falls through to the next source.

    The config module is fetched through ``sys.modules`` (the established
    core-module idiom, see ``O4_OSM_Extracts``): this module must not
    import ``O4_Config_Utils`` at top level, and when it was never loaded
    (unit tests, library use) there is simply no root.  Returns ``None``
    when no root is resolvable.
    """
    import sys

    configuration = sys.modules.get("O4_Config_Utils")
    if configuration is None:
        return None
    cifp_path = getattr(configuration, "cifp_data_path", "") or ""
    if cifp_path:
        try:
            from auto_patch.cifp_reader import xplane_root_from_cifp_path

            root = xplane_root_from_cifp_path(cifp_path)
        except Exception:
            root = None
        if root and os.path.isdir(os.path.join(root, "Custom Scenery")):
            return root
    custom_scenery_directory = (
        getattr(configuration, "custom_scenery_dir", "") or ""
    )
    if custom_scenery_directory and os.path.isdir(custom_scenery_directory):
        return os.path.dirname(os.path.normpath(custom_scenery_directory))
    return None


def _dsf_tile_coordinates_for_bounding_box(bounding_box_wgs84):
    """Integer (latitude, longitude) of every 1x1 degree DSF tile the box
    touches -- usually one, up to four when an airport straddles a tile
    corner (the margin ring routinely crosses a tile edge)."""
    import math

    (west, south, east, north) = bounding_box_wgs84
    return [
        (tile_latitude, tile_longitude)
        for tile_latitude in range(
            int(math.floor(south)), int(math.floor(north)) + 1
        )
        for tile_longitude in range(
            int(math.floor(west)), int(math.floor(east)) + 1
        )
    ]


def _disabled_custom_scenery_pack_names(custom_scenery_directory):
    """Pack directory names marked SCENERY_PACK_DISABLED in
    ``scenery_packs.ini``.  A disabled pack does not render, so its object
    footprints are not authoritative for the mask (the whole point of the
    package source is matching what renders in the simulator)."""
    disabled = set()
    ini_path = os.path.join(custom_scenery_directory, "scenery_packs.ini")
    try:
        with open(
            ini_path, "r", encoding="utf-8", errors="replace"
        ) as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("SCENERY_PACK_DISABLED"):
                    continue
                rest = line.split(None, 1)[1] if " " in line else ""
                if rest:
                    disabled.add(
                        os.path.basename(rest.strip().rstrip("/"))
                    )
    except OSError:
        pass
    return disabled


def _airport_pack_dsf_paths(xplane_root, bounding_box_wgs84):
    """Overlay DSFs of installed AIRPORT packs covering the box's tile(s).

    Candidate packs are ``Custom Scenery`` entries that carry an ``Earth
    nav data/apt.dat`` -- the marker of an airport pack, which keeps ortho
    tiles, mesh packs and object libraries out of the scan -- and a tile
    DSF for a 1x1 degree tile the box touches (a couple of ``os.path``
    checks per pack, no file is parsed here).  ``Global Airports`` is
    excluded: its per-tile DSFs cover everywhere, its buildings are
    library-resolved by the thousand (an expensive cold parse at download
    time), and the OpenStreetMap side of the union already covers those
    real-world buildings; per-airport custom packs are the placements this
    source exists for.  Disabled packs (``scenery_packs.ini``) do not
    render and are skipped.
    """
    custom_scenery_directory = os.path.join(xplane_root, "Custom Scenery")
    if not os.path.isdir(custom_scenery_directory):
        return []
    disabled = _disabled_custom_scenery_pack_names(custom_scenery_directory)
    tile_relative_paths = [
        FNAMES.long_latlon(tile_latitude, tile_longitude) + ".dsf"
        for (tile_latitude, tile_longitude) in (
            _dsf_tile_coordinates_for_bounding_box(bounding_box_wgs84)
        )
    ]
    dsf_paths = []
    for pack_name in sorted(os.listdir(custom_scenery_directory)):
        if pack_name == "Global Airports" or pack_name in disabled:
            continue
        nav_data_directory = os.path.join(
            custom_scenery_directory, pack_name, "Earth nav data"
        )
        if not os.path.isfile(os.path.join(nav_data_directory, "apt.dat")):
            continue
        for tile_relative_path in tile_relative_paths:
            candidate = os.path.join(nav_data_directory, tile_relative_path)
            if os.path.isfile(candidate):
                dsf_paths.append(candidate)
    return dsf_paths


def package_object_footprints(bounding_box_wgs84, definition):
    """Authoritative building footprints from installed airport packages.

    The installed airport scenery package's DSF/OBJ8 object placements are
    the footprints that actually render in the simulator, so they are the
    PRIMARY footprint source for the inset mask (owner ruling 2026-07-18;
    OpenStreetMap supplements them, and since the mask is a boolean union
    precedence never has to be arbitrated).

    Pack resolution is bbox-driven and needs no airport identifier and no
    build context: the X-Plane root comes from download-time configuration
    (:func:`_xplane_root_for_package_footprints`), candidate packs from a
    cheap directory scan (:func:`_airport_pack_dsf_paths`), and each
    candidate DSF goes through
    ``auto_patch.dsf_reader.read_dsf_object_buildings`` -- the same reader
    the build pipeline uses, so the expensive OBJ8 parse + partition is
    served from (and primes) the shared ``o4_object_footprints_*`` sidecar
    caches under ``Airport_mod_cache/``.  Rings come back in plain
    (longitude, latitude); they are repaired like the pipeline's building
    pool (``buffer(0)``) and clipped to the box.

    Defensive throughout: no configured root, no candidate pack, an
    unreadable DSF, or any exception all degrade to ``[]`` -- the mask then
    falls back to OpenStreetMap alone, and an uncorrected inset still beats
    a failed fetch.  ``O4_INSET_PACKAGE_FOOTPRINTS=0`` disables the source
    (debug: attribute a bad mask to one side of the union).
    """
    if os.environ.get("O4_INSET_PACKAGE_FOOTPRINTS", "1") != "1":
        return []
    try:
        from shapely.geometry import Polygon
        from shapely.geometry import box as shapely_box

        xplane_root = _xplane_root_for_package_footprints()
        if not xplane_root:
            return []
        dsf_paths = _airport_pack_dsf_paths(xplane_root, bounding_box_wgs84)
        if not dsf_paths:
            return []
        from auto_patch.dsf_reader import read_dsf_object_buildings
    except Exception as error:
        UI.vprint(
            2,
            "   Package footprint sourcing unavailable:",
            str(error),
        )
        return []
    (west, south, east, north) = bounding_box_wgs84
    bounding_geometry = shapely_box(west, south, east, north)
    footprints = []
    for dsf_path in dsf_paths:
        try:
            buildings = read_dsf_object_buildings(
                dsf_path, xplane_root=xplane_root
            )
        except Exception as error:
            UI.vprint(
                2,
                "   Package footprint read failed for",
                dsf_path,
                ":",
                str(error),
            )
            continue
        for (outer_ring, hole_rings, _role) in buildings:
            if len(outer_ring) < 3:
                continue
            try:
                polygon = Polygon(
                    outer_ring,
                    [ring for ring in hole_rings if len(ring) >= 3],
                )
                if not polygon.is_valid:
                    polygon = polygon.buffer(0)
                if (
                    polygon.is_empty
                    or not polygon.area
                    or not polygon.intersects(bounding_geometry)
                ):
                    continue
            except Exception:
                continue
            footprints.append(polygon)
    if footprints:
        UI.vprint(
            2,
            "   ",
            len(footprints),
            "building footprints from installed airport package(s).",
        )
    return footprints


def _collect_inset_building_footprints(
    bounding_box_wgs84, definition, footprint_prefetch=None
):
    """UNION of building footprints for the inset mask (package + OSM).

    Package (installed airport scenery) object footprints are authoritative
    where present; OpenStreetMap footprints supplement them.  The mask built
    from these is a boolean UNION, so precedence is moot -- a cell is masked
    if ANY source covers it, and the downstream fill is completely agnostic
    to which source contributed a polygon (requirement: sourcing rule and
    fill algorithm stay decoupled).  Either source may be empty.

    Returns ``(footprints, source_label)``.
    """
    package = package_object_footprints(bounding_box_wgs84, definition)
    osm = openstreetmap_building_footprints(
        bounding_box_wgs84, footprint_prefetch=footprint_prefetch
    )
    footprints = list(package) + list(osm)
    if package and osm:
        label = "installed package objects + OpenStreetMap footprints"
    elif package:
        label = "installed package objects"
    else:
        label = "OpenStreetMap building footprints"
    return footprints, label


def _buffer_footprints_in_metres(footprints, buffer_m, centre_latitude):
    """Buffer WGS84 polygons by ``buffer_m`` true metres.

    Degrees are anisotropic away from the equator, so each polygon is
    scaled into a local equirectangular metre frame at the box's centre
    latitude, buffered there, and scaled back.
    """
    from shapely.ops import transform as shapely_transform

    metres_per_degree_longitude = GEO.lon_to_m(centre_latitude)
    metres_per_degree_latitude = GEO.lat_to_m
    buffered = []
    for polygon in footprints:
        in_metres = shapely_transform(
            lambda x, y: (
                x * metres_per_degree_longitude,
                y * metres_per_degree_latitude,
            ),
            polygon,
        )
        back_in_degrees = shapely_transform(
            lambda x, y: (
                x / metres_per_degree_longitude,
                y / metres_per_degree_latitude,
            ),
            in_metres.buffer(buffer_m),
        )
        if back_in_degrees.is_valid and not back_in_degrees.is_empty:
            buffered.append(back_in_degrees)
    return buffered


def _rasterize_footprint_mask(footprints, reference_dataset):
    """Boolean array marking ``reference_dataset`` pixels under a footprint."""
    memory_raster = gdal.GetDriverByName("MEM").Create(
        "",
        reference_dataset.RasterXSize,
        reference_dataset.RasterYSize,
        1,
        gdal.GDT_Byte,
    )
    memory_raster.SetGeoTransform(reference_dataset.GetGeoTransform())
    memory_raster.SetProjection(reference_dataset.GetProjection())
    # GDAL 3.11 renamed the in-memory OGR driver "Memory" -> "MEM";
    # accept both so older installs keep working.
    vector_driver = ogr.GetDriverByName("MEM") or ogr.GetDriverByName(
        "Memory"
    )
    vector_dataset = vector_driver.CreateDataSource("")
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    vector_layer = vector_dataset.CreateLayer(
        "footprints", spatial_reference, ogr.wkbPolygon
    )
    for polygon in footprints:
        feature = ogr.Feature(vector_layer.GetLayerDefn())
        feature.SetGeometry(ogr.CreateGeometryFromWkb(polygon.wkb))
        vector_layer.CreateFeature(feature)
        feature = None
    gdal.RasterizeLayer(memory_raster, [1], vector_layer, burn_values=[1])
    mask = memory_raster.GetRasterBand(1).ReadAsArray().astype(bool)
    memory_raster = None
    vector_dataset = None
    return mask


def mask_building_footprints_in_surface_model(
    inset_path, bounding_box_wgs84, definition, footprint_prefetch=None
):
    """Replace building-contaminated surface-model pixels by ground.

    Surface models (radar DSMs like Copernicus GLO-30) bake rooftop
    heights into the terrain.  Height subtraction cannot repair that (a
    30 m pixel straddling a wall holds a roof/ground mixture, radar
    layover smears the return past the footprint, and mapped heights
    rarely match what the radar saw), so contaminated pixels are simply
    NOT TRUSTED: every pixel within ``footprint_mask_buffer_m`` of an
    OpenStreetMap building footprint is masked and re-interpolated from
    the surrounding ground with ``gdal.FillNodata``.  Under an airport
    terminal the true ground is nearly planar, which makes the fill an
    excellent estimate.  Genuine nodata cells are excluded from the
    interpolation sources and restored verbatim afterwards.

    Returns a summary dictionary for the provenance sidecar; on any
    failure the summary carries a ``skipped`` reason and the raster is
    left as fetched (an uncorrected inset beats a failed fetch).
    """
    if not has_gdal:
        return {"skipped": "GDAL unavailable"}
    (footprints, footprint_source) = _collect_inset_building_footprints(
        bounding_box_wgs84, definition, footprint_prefetch=footprint_prefetch
    )
    buffer_m = _parse_float(
        definition.get("footprint_mask_buffer_m"),
        default=DEFAULT_FOOTPRINT_MASK_BUFFER_M,
    )
    residual_masking_on = definition.get(RESIDUAL_STRUCTURE_MASKING, True)
    if not footprints and not residual_masking_on:
        return {
            "skipped": "no building footprints in the box",
            "footprint_count": 0,
        }
    (west, south, east, north) = bounding_box_wgs84
    centre_latitude = (south + north) / 2.0
    try:
        buffered = _buffer_footprints_in_metres(
            footprints, buffer_m, centre_latitude
        ) if footprints else []
        dataset = gdal.Open(inset_path, gdal.GA_Update)
        band = dataset.GetRasterBand(1)
        values = band.ReadAsArray()
        nodata_value = band.GetNoDataValue()
        if nodata_value is None:
            nodata_value = -32768.0
        if buffered:
            building_mask = _rasterize_footprint_mask(buffered, dataset)
        else:
            building_mask = numpy.zeros(values.shape, dtype=bool)
        genuine_nodata = (values == nodata_value) | ~numpy.isfinite(values)
        # RESIDUAL STRUCTURE MASK (2026-07-18): footprint masking can only
        # erase what is MAPPED; unmapped structures (dense city blocks with
        # no OpenStreetMap coverage — the SPJC east-side mounds) survive it.
        # Flag every pixel standing above the morphological ground estimate
        # and heal it through the SAME source-agnostic fill.
        residual_mask = numpy.zeros(values.shape, dtype=bool)
        residual_masked_pixel_count = 0
        if residual_masking_on:
            import math
            geotransform = dataset.GetGeoTransform()
            pixel_size_m = abs(geotransform[1]) * 111320.0 * math.cos(
                math.radians(centre_latitude))
            residual_mask = _residual_structure_mask(
                values, genuine_nodata, pixel_size_m
            )
            residual_masked_pixel_count = int(residual_mask.sum())
        combined_mask = building_mask | residual_mask
        pixels_to_fill = combined_mask & ~genuine_nodata
        # Trusted-ground sources: every cell that is neither under a
        # footprint nor genuine nodata (so sentinels are never fill
        # sources).  Both fill methods fill every non-source cell and let
        # the caller restore genuine nodata, so only building pixels change.
        interpolation_sources = ~combined_mask & ~genuine_nodata
        if not pixels_to_fill.any() or not interpolation_sources.any():
            dataset = None
            return {
                "footprint_source": footprint_source,
                "footprint_count": len(footprints),
                "masked_pixel_count": 0,
                "residual_masked_pixel_count": residual_masked_pixel_count,
                "residual_mask_threshold_m":
                    DEFAULT_RESIDUAL_MASK_THRESHOLD_M,
                "residual_mask_opening_window_m":
                    DEFAULT_RESIDUAL_MASK_OPENING_WINDOW_M,
                "footprint_mask_buffer_m": buffer_m,
                "fill_method": _inset_fill_method(),
            }
        fill_method = _inset_fill_method()
        if fill_method == INSET_FILL_METHOD_DISTANCE_TRANSFORM:
            filled_values = _fill_masked_by_distance_transform(
                values,
                interpolation_sources,
                smoothing_iterations=DEFAULT_FILL_SMOOTHING_ITERATIONS,
            )
            filled_values[genuine_nodata] = nodata_value
            band.WriteArray(filled_values)
            band.FlushCache()
            dataset = None
        else:
            source_mask_raster = gdal.GetDriverByName("MEM").Create(
                "", dataset.RasterXSize, dataset.RasterYSize, 1,
                gdal.GDT_Byte,
            )
            source_mask_raster.SetGeoTransform(dataset.GetGeoTransform())
            source_mask_band = source_mask_raster.GetRasterBand(1)
            source_mask_band.WriteArray(
                interpolation_sources.astype(numpy.uint8) * 255
            )
            search_pixels = _parse_float(
                definition.get("footprint_fill_search_pixels"),
                default=DEFAULT_FOOTPRINT_FILL_SEARCH_PIXELS,
            )
            gdal.FillNodata(
                targetBand=band,
                maskBand=source_mask_band,
                maxSearchDist=float(search_pixels),
                smoothingIterations=DEFAULT_FILL_SMOOTHING_ITERATIONS,
            )
            filled_values = band.ReadAsArray()
            filled_values[genuine_nodata] = nodata_value
            band.WriteArray(filled_values)
            band.FlushCache()
            dataset = None
            source_mask_raster = None
    except Exception as error:
        UI.vprint(
            1,
            "   WARNING: building-footprint masking failed:",
            str(error),
        )
        return {"skipped": str(error), "footprint_count": len(footprints)}
    return {
        "footprint_source": footprint_source,
        "footprint_count": len(footprints),
        "masked_pixel_count": int(pixels_to_fill.sum()),
        "masked_fraction": round(
            float(pixels_to_fill.sum()) / float(values.size), 4
        ),
        "residual_masked_pixel_count": residual_masked_pixel_count,
        "residual_mask_threshold_m": DEFAULT_RESIDUAL_MASK_THRESHOLD_M,
        "residual_mask_opening_window_m":
            DEFAULT_RESIDUAL_MASK_OPENING_WINDOW_M,
        "footprint_mask_buffer_m": buffer_m,
        "fill_method": fill_method,
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


# ~0.1 m at the equator: a real margin change (metres) always exceeds it,
# while float noise from recomputing the same box never does.
INSET_BOUNDING_BOX_TOLERANCE_DEGREES = 1e-6


def _bounding_box_extends_beyond(
    requested_box, recorded_box,
    tolerance=INSET_BOUNDING_BOX_TOLERANCE_DEGREES,
):
    """True when ``requested_box`` reaches outside ``recorded_box`` anywhere.

    Both are ``(west, south, east, north)`` in EPSG:4326 degrees.  A
    requested box fully inside the recorded one (margin shrunk or equal)
    is NOT beyond it: a superset raster stays valid.
    """
    (requested_west, requested_south, requested_east, requested_north) = (
        requested_box
    )
    (recorded_west, recorded_south, recorded_east, recorded_north) = (
        recorded_box
    )
    return (
        requested_west < recorded_west - tolerance
        or requested_south < recorded_south - tolerance
        or requested_east > recorded_east + tolerance
        or requested_north > recorded_north + tolerance
    )


def _sidecar_residual_masking_mismatch(lat, lon, icao, provider_code,
                                       residual_masking_wanted):
    """True when the cached inset was produced with a DIFFERENT residual
    structure-masking configuration than the provider now wants — both
    directions: an inset built without it while the gate is ON, and an
    inset carrying residual-masked pixels while the gate is OFF (the
    2026-07-18 live regression left damaged caches that must regenerate
    clean).  A missing or unreadable sidecar reads as False —
    pre-sidecar caches keep the established leave-alone policy."""
    provenance_path = FNAMES.airport_inset_provenance(
        lat, lon, icao, provider_code
    )
    try:
        with open(provenance_path, "r") as handle:
            provenance = json.load(handle)
    except (OSError, ValueError):
        return False
    summary = provenance.get(SURFACE_MODEL_BUILDING_MASKING)
    if not isinstance(summary, dict):
        return False
    if residual_masking_wanted:
        return "residual_masked_pixel_count" not in summary
    return bool(summary.get("residual_masked_pixel_count"))


def _fetched_bounding_box(lat, lon, icao, provider_code):
    """The bounding box a cached inset was actually fetched with.

    Read from the provenance sidecar's ``bounding_box_wgs84`` (every access
    strategy records the requested box verbatim).  ``None`` when the sidecar
    is missing or unreadable — pre-sidecar caches cannot be judged and are
    treated as covering whatever is requested.
    """
    provenance_path = FNAMES.airport_inset_provenance(
        lat, lon, icao, provider_code
    )
    try:
        with open(provenance_path, "r") as handle:
            provenance = json.load(handle)
        recorded_box = provenance.get("bounding_box_wgs84")
        if isinstance(recorded_box, (list, tuple)) and len(recorded_box) == 4:
            return tuple(float(value) for value in recorded_box)
    except (OSError, ValueError, TypeError):
        pass
    return None


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
    discovery API; ``refresh`` forces a re-query and re-fetch.  A fetch
    that RAISES (:class:`TransientFetchError` or any strategy crash) is
    treated as transient: nothing is recorded for that provider and the
    next run retries it.

    The caches are margin-aware: a cached GeoTIFF whose provenance sidecar
    records a smaller bounding box than requested (the user enlarged
    ``airport_elevation_inset_margin_m``) is refetched, and negative results
    are re-checked when the request outgrows the box they were evaluated
    against (stored per airport under ``"bounding_box"``).  A request equal
    to or inside what is cached never refetches.  Records written before
    this key existed keep their negative results until a ``refresh``.

    Returns the updated index dictionary.  Strategy-agnostic: it only calls
    :func:`discover_inset` / :func:`fetch_inset`.
    """
    index = _read_index(lat, lon)
    checked_stamp = datetime.date.today().isoformat()
    # One shared, LAZY footprint prefetch for the whole tile: the first
    # surface-model masking pass triggers a single extract read covering
    # every airport's box; later airports clip from it in memory.  Tiles
    # that never reach a masking fetch never pay the read.
    footprint_prefetch = TileBuildingFootprintPrefetch(
        airport_bounding_boxes.values()
    )
    # key=str: callers pass string airport codes, but a mixed-type dict must
    # never abort the whole tile's fetches with an unorderable-keys
    # TypeError (defense in depth behind _airport_bounding_boxes' filter).
    for icao in sorted(airport_bounding_boxes, key=str):
        bounding_box = airport_bounding_boxes[icao]
        airport_record = index.get(icao, {})
        recorded_box = airport_record.get("bounding_box")
        negatives_are_stale = (
            recorded_box is not None
            and _bounding_box_extends_beyond(bounding_box, recorded_box)
        )
        for definition in provider_definitions:
            code = definition["code"]
            destination = FNAMES.airport_inset_dem(lat, lon, icao, code)
            cached_inset_is_stale = False
            if os.path.isfile(destination) and not refresh:
                fetched_box = _fetched_bounding_box(lat, lon, icao, code)
                cached_inset_is_stale = (
                    fetched_box is not None
                    and _bounding_box_extends_beyond(
                        bounding_box, fetched_box
                    )
                )
                if (not cached_inset_is_stale
                        and definition.get(SURFACE_MODEL_BUILDING_MASKING)
                        and _sidecar_residual_masking_mismatch(
                            lat, lon, icao, code,
                            definition.get(RESIDUAL_STRUCTURE_MASKING))):
                    # One-time reconcile (2026-07-18): the cached inset was
                    # produced under the OTHER residual-masking setting —
                    # either it predates the feature while the gate is on,
                    # or it carries residual-masked pixels while the gate
                    # is off (the live-regression caches) — refetch once.
                    cached_inset_is_stale = True
                    UI.vprint(
                        1,
                        "    Cached elevation inset for",
                        icao,
                        "from",
                        code,
                        "was built with a different residual-masking"
                        " setting - refetching.",
                    )
                if not cached_inset_is_stale:
                    airport_record[code] = airport_record.get(code) or "ok"
                    _store_acceptance_probes_in_record(
                        airport_record, destination
                    )
                    break
                UI.vprint(
                    1,
                    "    Cached elevation inset for",
                    icao,
                    "from",
                    code,
                    "covers a smaller area than the requested margin"
                    " - refetching.",
                )
            if (
                not refresh
                and not negatives_are_stale
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
            fetch_destination = destination
            if cached_inset_is_stale:
                # A failed warp can leave a partial file behind; the
                # still-valid smaller inset must survive a failed
                # enlargement, so fetch beside it and replace on success.
                fetch_destination = destination + ".refetch"
            fetch_raised = False
            try:
                provenance = fetch_inset(
                    definition,
                    bounding_box,
                    target_resolution_m,
                    fetch_destination,
                    footprint_prefetch=footprint_prefetch,
                )
            except Exception as error:
                # A raised failure (a network timeout, a server outage, a
                # strategy crash) says nothing about coverage: skip the
                # provider for THIS run without caching a no-coverage
                # negative, so the next run retries the fetch.
                provenance = None
                fetch_raised = True
                UI.vprint(
                    1,
                    "   WARNING: elevation inset fetch for",
                    icao,
                    "from",
                    code,
                    "failed without a durable answer:",
                    str(error),
                    "- it will be retried on the next run.",
                )
            if provenance is None:
                if cached_inset_is_stale:
                    if os.path.isfile(fetch_destination):
                        os.remove(fetch_destination)
                    UI.vprint(
                        1,
                        "   WARNING: could not refetch a larger inset for",
                        icao,
                        "from",
                        code,
                        "- keeping the previous smaller one.",
                    )
                    airport_record[code] = "ok"
                    airport_record["checked"] = checked_stamp
                    break
                if os.path.isfile(destination):
                    # No cache existed before this fetch (that branch breaks
                    # or refetches beside it), so this is a partial file
                    # from a broken warp; left in place it would pass the
                    # cache check and bake garbage on the next run.
                    os.remove(destination)
                if fetch_raised:
                    # Transient: no durable record for this provider; a
                    # lower-ranked provider may still cover the airport
                    # this run, and this one retries next run.
                    continue
                airport_record[code] = NO_COVERAGE
                airport_record["checked"] = checked_stamp
                continue
            if cached_inset_is_stale:
                os.replace(fetch_destination, destination)
            provenance_path = FNAMES.airport_inset_provenance(
                lat, lon, icao, code
            )
            with open(provenance_path, "w") as handle:
                json.dump(provenance, handle, indent=2, sort_keys=True)
            airport_record[code] = "ok"
            airport_record["checked"] = checked_stamp
            # refresh=True: the raster on disk is new, so cached acceptance
            # probes from a previous (smaller) fetch must recompute.
            _store_acceptance_probes_in_record(
                airport_record, destination, refresh=True
            )
            break
        airport_record["bounding_box"] = [
            float(value) for value in bounding_box
        ]
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
# Inset-derived water supplement (hydro-flat basins)
# =====================================================================
# Lidar reads water surfaces as (near-)constant elevation, so real
# basins appear in the inset rasters as large flat plateaus sitting
# BELOW their rims — the KBNA wastewater ponds measure a 0.02 m
# internal range against rims 10+ m higher.  Such basins are usually
# absent from OpenStreetMap (no ``natural=water`` way exists over the
# KBNA ponds), so the mesh keeps raw noisy terrain there instead of the
# flat water the ``WATER`` seed + ``water_smoothing`` pipeline would
# produce.  These functions derive the missing polygons from the inset
# rasters themselves and write a per-tile OSM fragment which
# ``include_water`` merges ADDITIVELY into the water layer.

# Detection is TWO-TIER (measured on the real KBNA 3DEP inset,
# 2026-07-14):
#
# * STRICT, whole raster — exact hydro-flat plateaus (providers
#   hydro-flatten sizeable water bodies; KBNA's south-east lake is a
#   16,000 m2 plateau with 0.000 m internal range).  At the strict
#   thresholds exactly one basin qualifies at KBNA and every apron,
#   pavement plateau and void-fill artefact is rejected.
# * FACILITY-SCOPED, loose — INSIDE OpenStreetMap water-facility
#   outlines only (man_made=wastewater_plant, landuse=basin/reservoir).
#   Working ponds are NOT hydro-flat (the KBNA aeration ponds carry
#   0.3-0.5 m of lidar surface texture), so the loose thresholds would
#   over-detect hollows tile-wide (109 candidates measured) — but
#   inside a mapped water facility the outline itself authorises the
#   looser read; the lidar only traces the geometry.
#
# Strict tier:
INSET_WATER_LOCAL_FLATNESS_M = 0.05
INSET_WATER_COMPONENT_RANGE_M = 0.15
# The surrounding rim (75th percentile over an ~8-cell collar) must
# rise at least this above the water — flat PAVEMENT sits level with
# its surroundings and must never become water.
INSET_WATER_RIM_RISE_M = 0.3
# Plateaus further than this from the raster's median elevation are
# void-fill artefacts (a 40 m plateau inside the 150-180 m KBNA
# raster), never water.
INSET_WATER_PLAUSIBILITY_BAND_M = 30.0
INSET_WATER_MINIMUM_AREA_M2 = 500.0
# Facility-scoped tier (loose).  A WORKING pond's lidar surface is
# rough: the KBNA aeration ponds measure 0.25-0.55 m of 3-by-3 relief
# and a 1.8 m whole-pond range (foam, aerators, shore transition
# cells) — the mapped outline is the authorisation; these thresholds
# only trace the geometry within it.
INSET_WATER_FACILITY_LOCAL_FLATNESS_M = 0.6
INSET_WATER_FACILITY_COMPONENT_RANGE_M = 2.5
INSET_WATER_FACILITY_RIM_RISE_M = 0.3
INSET_WATER_FACILITY_MINIMUM_AREA_M2 = 300.0
# The OpenStreetMap outlines that authorise the loose tier.
INSET_WATER_FACILITY_QUERIES = (
    'way["man_made"="wastewater_plant"]',
    'way["landuse"="basin"]',
    'way["landuse"="reservoir"]',
)
# Schema stamp written into the supplement's ``generator`` attribute.
# Bump it whenever the detection rules change: a cached supplement can
# be NEWER than its rasters yet written under wrong rules (the
# 2026-07-18 SPJC regression left supplements with 1060 phantom urban
# basins), and mtime comparison alone would keep it forever.
INSET_WATER_SUPPLEMENT_SCHEMA = "hydro-flat-2026-07-18"


def detect_hydro_flat_water_rings(
    inset_tif_path,
    *,
    minimum_area_m2=None,
    local_flatness_m=None,
    component_range_m=None,
    rim_rise_m=None,
    plausibility_band_m=None,
):
    """Detect hydro-flat basins in one inset GeoTIFF.

    Returns a list of ``(ring, water_elevation_m)`` where ``ring`` is an
    unclosed ``[(longitude, latitude), ...]`` exterior; empty list when
    nothing qualifies (or GDAL/scipy are unavailable).  Pure read — no
    files written.
    """
    if minimum_area_m2 is None:
        minimum_area_m2 = INSET_WATER_MINIMUM_AREA_M2
    if local_flatness_m is None:
        local_flatness_m = INSET_WATER_LOCAL_FLATNESS_M
    if component_range_m is None:
        component_range_m = INSET_WATER_COMPONENT_RANGE_M
    if rim_rise_m is None:
        rim_rise_m = INSET_WATER_RIM_RISE_M
    if plausibility_band_m is None:
        plausibility_band_m = INSET_WATER_PLAUSIBILITY_BAND_M
    loaded = _load_inset_raster(inset_tif_path)
    if loaded is None:
        return []
    values, valid, geotransform = loaded
    return _detect_water_components(
        values, valid, geotransform,
        minimum_area_m2=minimum_area_m2,
        local_flatness_m=local_flatness_m,
        component_range_m=component_range_m,
        rim_rise_m=rim_rise_m,
        plausibility_band_m=plausibility_band_m,
    )


def _load_inset_raster(inset_tif_path):
    """``(median_filtered_values, valid_mask, geotransform)`` or ``None``
    (GDAL or scipy unavailable, unreadable file).  The 3-by-3 median
    pre-filter repairs isolated lidar dropouts (11 m pits inside the
    KBNA ponds) that would otherwise fragment flat components."""
    if not has_gdal:
        return None
    try:
        from scipy import ndimage
    except ImportError:
        UI.vprint(
            1,
            "   INFO: scipy is unavailable - inset water detection "
            "skipped.",
        )
        return None
    dataset = gdal.Open(inset_tif_path)
    if dataset is None:
        return None
    geotransform = dataset.GetGeoTransform()
    band = dataset.GetRasterBand(1)
    raw = band.ReadAsArray().astype(numpy.float64)
    nodata = band.GetNoDataValue()
    valid = numpy.isfinite(raw)
    if nodata is not None:
        valid &= raw != nodata
    values = ndimage.median_filter(raw, size=3)
    return values, valid, geotransform


def _detect_water_components(
    values,
    valid,
    geotransform,
    *,
    minimum_area_m2,
    local_flatness_m,
    component_range_m,
    rim_rise_m,
    plausibility_band_m,
    restrict_mask=None,
):
    """The shared component scan behind both detection tiers.

    ``restrict_mask`` (boolean, raster-shaped) limits candidate cells —
    the facility-scoped tier passes the rasterized OpenStreetMap
    water-facility outlines.  Returns ``[(ring, water_elevation_m)]``.
    """
    from scipy import ndimage
    from shapely.errors import GEOSException
    from shapely.geometry import box as shapely_box
    from shapely.ops import unary_union

    # Metric cell size at the raster's own latitude.
    centre_latitude = geotransform[3] + (
        geotransform[5] * values.shape[0] / 2.0
    )
    metres_per_degree_longitude = GEO.lat_to_m * numpy.cos(
        numpy.radians(centre_latitude)
    )
    cell_area_m2 = abs(
        geotransform[1] * metres_per_degree_longitude
        * geotransform[5] * GEO.lat_to_m
    )
    if cell_area_m2 <= 0.0:
        return []
    minimum_cells = max(9, int(minimum_area_m2 / cell_area_m2))

    # 3-by-3 local relief (max minus min over the neighbourhood).
    local_maximum = ndimage.maximum_filter(values, size=3)
    local_minimum = ndimage.minimum_filter(values, size=3)
    flat = (local_maximum - local_minimum) <= local_flatness_m
    flat &= valid
    if restrict_mask is not None:
        flat &= restrict_mask
    flat[0, :] = flat[-1, :] = False
    flat[:, 0] = flat[:, -1] = False

    labels, label_count = ndimage.label(flat)
    if label_count == 0:
        return []
    sizes = numpy.bincount(labels.ravel())
    overall_median = float(numpy.median(values[valid])) if valid.any() \
        else 0.0

    rings = []
    for label_index in range(1, label_count + 1):
        if sizes[label_index] < minimum_cells:
            continue
        component = labels == label_index
        component_values = values[component]
        if (float(component_values.max())
                - float(component_values.min())) > component_range_m:
            continue
        water_elevation = float(numpy.median(component_values))
        if abs(water_elevation - overall_median) > plausibility_band_m:
            continue
        # The rim must RISE above the water (a flat apron sits level
        # with its surroundings and must never become water).  75th
        # percentile over an 8-cell collar: shores shelve gently, so a
        # thin median under-reads real rims (measured KBNA lake:
        # 3-cell median rim +0.12 m, 8-cell 75th percentile +5.7 m).
        dilated = ndimage.binary_dilation(component, iterations=8)
        rim = dilated & ~component & valid
        if not rim.any():
            continue
        if float(numpy.percentile(values[rim], 75)) < (
                water_elevation + rim_rise_m):
            continue
        # Draw the polygon one cell INSIDE the shore so every enclosed
        # mesh triangle converges cleanly under water smoothing.
        eroded = ndimage.binary_erosion(component)
        if not eroded.any():
            eroded = component
        # Row-run rectangles -> union -> simplify (runs, not cells,
        # keep the union cheap).
        boxes = []
        for row_index in numpy.flatnonzero(eroded.any(axis=1)):
            row = eroded[row_index]
            occupied_columns = numpy.flatnonzero(row)
            runs = numpy.split(
                occupied_columns, numpy.flatnonzero(
                    numpy.diff(occupied_columns) > 1) + 1)
            for run in runs:
                if run.size == 0:
                    continue
                column_start, column_end = int(run[0]), int(run[-1])
                longitude_west = geotransform[0] + (
                    column_start * geotransform[1])
                longitude_east = geotransform[0] + (
                    (column_end + 1) * geotransform[1])
                latitude_north = geotransform[3] + (
                    row_index * geotransform[5])
                latitude_south = geotransform[3] + (
                    (row_index + 1) * geotransform[5])
                boxes.append(shapely_box(
                    min(longitude_west, longitude_east),
                    min(latitude_north, latitude_south),
                    max(longitude_west, longitude_east),
                    max(latitude_north, latitude_south),
                ))
        if not boxes:
            continue
        try:
            union = unary_union(boxes)
        except (ValueError, GEOSException):
            continue
        polygons = ([union] if union.geom_type == "Polygon"
                    else [geometry for geometry in getattr(
                        union, "geoms", [])
                        if geometry.geom_type == "Polygon"])
        for polygon in polygons:
            # Metric area floor PER POLYGON: a 55-cell component that is
            # a 2-cell-wide creek thread erodes to slivers — a linear
            # water body is OpenStreetMap's business, not a basin.
            polygon_area_m2 = (polygon.area * GEO.lat_to_m
                               * metres_per_degree_longitude)
            if polygon_area_m2 < minimum_area_m2:
                continue
            simplified = polygon.simplify(
                abs(geotransform[1]) * 2.0, preserve_topology=True)
            if simplified.is_empty \
                    or simplified.geom_type != "Polygon":
                simplified = polygon
            ring = [(float(x), float(y))
                    for x, y in simplified.exterior.coords[:-1]]
            if len(ring) >= 3:
                rings.append((ring, water_elevation))
    return rings


def _facility_outline_polygons(lat, lon):
    """Closed OpenStreetMap water-facility outlines for the tile
    (absolute longitude/latitude shapely polygons), fetched through the
    normal cached Overpass machinery (``cached_suffix="water_basins"``).
    Empty list when the fetch fails or nothing is mapped."""
    import O4_OSM_Utils as OSM
    from shapely.geometry import Polygon as ShapelyPolygon

    facility_layer = OSM.OSM_layer()
    if not OSM.OSM_queries_to_OSM_layer(
        list(INSET_WATER_FACILITY_QUERIES),
        facility_layer,
        lat,
        lon,
        tags_of_interest=["man_made", "landuse"],
        cached_suffix="water_basins",
    ):
        return []
    polygons = []
    for way_identifier in (facility_layer.dicosmfirst["w"]
                           or facility_layer.dicosmw):
        node_references = facility_layer.dicosmw.get(way_identifier, [])
        if len(node_references) < 4 \
                or node_references[0] != node_references[-1]:
            continue
        ring = [facility_layer.dicosmn[reference]
                for reference in node_references[:-1]]
        try:
            polygon = ShapelyPolygon(ring)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if not polygon.is_empty and polygon.area > 0.0:
                polygons.append(polygon)
        except ValueError:
            continue
    return polygons


def _facility_restrict_mask(polygons, values_shape, geotransform):
    """Boolean raster mask of the cells whose centres fall inside any
    facility polygon, or ``None`` when no polygon overlaps the raster."""
    import shapely

    if not polygons:
        return None
    rows, columns = values_shape
    mask = numpy.zeros(values_shape, dtype=bool)
    for polygon in polygons:
        minimum_x, minimum_y, maximum_x, maximum_y = polygon.bounds
        column_start = int((minimum_x - geotransform[0])
                           / geotransform[1]) - 1
        column_end = int((maximum_x - geotransform[0])
                         / geotransform[1]) + 2
        row_start = int((maximum_y - geotransform[3])
                        / geotransform[5]) - 1
        row_end = int((minimum_y - geotransform[3])
                      / geotransform[5]) + 2
        column_start = max(0, column_start)
        column_end = min(columns, column_end)
        row_start = max(0, row_start)
        row_end = min(rows, row_end)
        if column_start >= column_end or row_start >= row_end:
            continue
        window_rows = numpy.arange(row_start, row_end)
        window_columns = numpy.arange(column_start, column_end)
        latitudes = geotransform[3] + (
            (window_rows + 0.5) * geotransform[5])
        longitudes = geotransform[0] + (
            (window_columns + 0.5) * geotransform[1])
        longitude_grid, latitude_grid = numpy.meshgrid(
            longitudes, latitudes)
        inside = shapely.contains_xy(
            polygon, longitude_grid.ravel(), latitude_grid.ravel()
        ).reshape(longitude_grid.shape)
        mask[row_start:row_end, column_start:column_end] |= inside
    return mask if mask.any() else None


def _water_detection_trusts_inset_raster(inset_path):
    """Whether hydro-flat water detection may read this inset raster.

    The detector's physical premise is a MEASURED surface at the
    working resolution: lidar reads real water as hydro-flat plateaus
    while dry ground keeps centimetre texture in every 3-by-3
    neighbourhood.  Two raster classes break that premise and are
    skipped outright (live SPJC regression 2026-07-18: 1060 phantom
    basins over urban Lima and Callao):

    * surface models with building-footprint masking — the masked
      pixels are ``gdal.FillNodata`` interpolation, synthetic smooth
      "ground" that reads as hydro-flat across whole city blocks
      (the SPJC GLO-30 inset had 29% of its pixels interpolated);
    * rasters upsampled beyond their native resolution (Copernicus
      GLO-30's 30 m cells fetched at 3 m) — every 3-by-3 window then
      samples one native cell's resampling ramp, so the flatness
      measure reads interpolation smoothness, not surface texture.

    A missing or unreadable provenance sidecar reads as trusted:
    pre-sidecar caches are the established lidar providers.
    """
    provenance_path = os.path.splitext(inset_path)[0] + ".json"
    try:
        with open(provenance_path, "r") as handle:
            provenance = json.load(handle)
    except (OSError, ValueError):
        return True
    if isinstance(provenance.get(SURFACE_MODEL_BUILDING_MASKING), dict):
        return False
    native_resolution_m = _parse_float(
        provenance.get("native_resolution_m"), default=None
    )
    fetched_resolution_m = _parse_float(
        provenance.get("resolution_m"), default=None
    )
    if (native_resolution_m is not None
            and fetched_resolution_m is not None
            and native_resolution_m > fetched_resolution_m):
        return False
    return True


def _inset_water_supplement_schema_current(supplement_path):
    """Whether a cached supplement was written under the current
    detection schema (the stamp lives in its ``generator`` attribute).
    Old-schema files regenerate even when newer than their rasters —
    their RULES were wrong when they were written, which mtime
    comparison cannot see.  Unreadable files read as stale."""
    import bz2

    try:
        with bz2.open(supplement_path, "rt", encoding="utf-8") as handle:
            handle.readline()
            return INSET_WATER_SUPPLEMENT_SCHEMA in handle.readline()
    except OSError:
        return False


def ensure_inset_water_supplement(lat, lon):
    """Write (or refresh) the per-tile inset-water OSM supplement and
    return its path, or ``None`` when no basin qualifies.

    Derived from every cached inset GeoTIFF for the tile
    (``list_cached_inset_dems``) whose provenance passes
    :func:`_water_detection_trusts_inset_raster`; regenerated when
    missing, older than any raster, or written under an older
    detection schema; removed when the qualifying rasters leave
    nothing behind.  The fragment is standard OSM XML (closed
    ``natural=water`` ways with negative identifiers), consumed
    additively by ``include_water``.
    """
    import bz2

    supplement_path = FNAMES.inset_water(lat, lon)
    inset_paths = list_cached_inset_dems(lat, lon)
    if not inset_paths:
        if os.path.isfile(supplement_path):
            os.remove(supplement_path)
        return None
    newest_raster = max(os.path.getmtime(path) for path in inset_paths)
    if (os.path.isfile(supplement_path)
            and os.path.getmtime(supplement_path) >= newest_raster
            and _inset_water_supplement_schema_current(supplement_path)):
        return supplement_path

    facility_polygons = _facility_outline_polygons(lat, lon)
    all_rings = []
    for inset_path in inset_paths:
        if not _water_detection_trusts_inset_raster(inset_path):
            UI.vprint(
                2,
                "   Inset water detection skips "
                + os.path.basename(inset_path)
                + " (surface model or upsampled raster).",
            )
            continue
        loaded = _load_inset_raster(inset_path)
        if loaded is None:
            continue
        values, valid, geotransform = loaded
        # Strict tier: exact hydro-flat plateaus, whole raster.
        for ring, water_elevation in _detect_water_components(
                values, valid, geotransform,
                minimum_area_m2=INSET_WATER_MINIMUM_AREA_M2,
                local_flatness_m=INSET_WATER_LOCAL_FLATNESS_M,
                component_range_m=INSET_WATER_COMPONENT_RANGE_M,
                rim_rise_m=INSET_WATER_RIM_RISE_M,
                plausibility_band_m=INSET_WATER_PLAUSIBILITY_BAND_M):
            all_rings.append((ring, water_elevation, inset_path))
        # Facility-scoped tier: loose thresholds, only inside mapped
        # water-facility outlines (working ponds carry real surface
        # texture and are never exactly flat).
        restrict_mask = _facility_restrict_mask(
            facility_polygons, values.shape, geotransform)
        if restrict_mask is not None:
            for ring, water_elevation in _detect_water_components(
                    values, valid, geotransform,
                    minimum_area_m2=INSET_WATER_FACILITY_MINIMUM_AREA_M2,
                    local_flatness_m=INSET_WATER_FACILITY_LOCAL_FLATNESS_M,
                    component_range_m=(
                        INSET_WATER_FACILITY_COMPONENT_RANGE_M),
                    rim_rise_m=INSET_WATER_FACILITY_RIM_RISE_M,
                    plausibility_band_m=INSET_WATER_PLAUSIBILITY_BAND_M,
                    restrict_mask=restrict_mask):
                all_rings.append((ring, water_elevation, inset_path))
    if not all_rings:
        if os.path.isfile(supplement_path):
            os.remove(supplement_path)
        return None

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<osm version="0.6" generator="O4_Airport_Elevation_Insets '
             + INSET_WATER_SUPPLEMENT_SCHEMA + '">']
    node_identifier = -1
    way_identifier = -1
    for ring, water_elevation, inset_path in all_rings:
        node_identifiers = []
        for longitude, latitude in ring:
            lines.append(
                f'  <node id="{node_identifier}" lat="{latitude:.8f}" '
                f'lon="{longitude:.8f}" version="1"/>')
            node_identifiers.append(node_identifier)
            node_identifier -= 1
        lines.append(f'  <way id="{way_identifier}" version="1">')
        for reference in node_identifiers + [node_identifiers[0]]:
            lines.append(f'    <nd ref="{reference}"/>')
        lines.append('    <tag k="natural" v="water"/>')
        lines.append(
            '    <tag k="source" v="airport elevation inset '
            'hydro-flat detection"/>')
        lines.append(f'  </way>')
        way_identifier -= 1
        UI.vprint(
            1,
            "   Inset water basin detected at "
            f"{water_elevation:.2f} m "
            f"({os.path.basename(inset_path)}).",
        )
    lines.append("</osm>")
    with bz2.open(supplement_path, "wt", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return supplement_path


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
    margin_m = getattr(tile, "airport_elevation_inset_margin_m", 2000.0)
    metres_per_degree_latitude = GEO.lat_to_m
    metres_per_degree_longitude = GEO.lon_to_m(tile.lat + 0.5)
    margin_lon = margin_m / metres_per_degree_longitude
    margin_lat = margin_m / metres_per_degree_latitude
    boxes = {}
    skipped_without_code = 0
    for airport in dico_airports:
        # dico_airports keys are ICAO/IATA/local_ref/name STRINGS for real
        # airports but REPRESENTATIVE-NODE TUPLES for unnamed strips
        # (O4_Airport_Utils key_type "repr_node").  Tuple keys cannot name a
        # cache file (FNAMES.airport_inset_dem concatenates the key) and made
        # ensure_airport_insets' sorted() raise "'<' not supported between
        # instances of 'str' and 'tuple'" — the 2026-07-14 CYXY fetch abort.
        # Unnamed strips do not get elevation insets; skip them loudly.
        if not isinstance(airport, str):
            skipped_without_code += 1
            continue
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
    if skipped_without_code:
        UI.vprint(
            1,
            "   INFO: airport elevation insets skipped",
            skipped_without_code,
            "unnamed airport(s) (no code to cache under).",
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


# Feather-ring offset sanity thresholds.  A few metres of median offset
# between an inset and the base DEM is the NORMAL surface-vs-bare-earth
# gap (the coarse base reads canopy, hedgerows and buildings; lidar reads
# ground) and warrants no action, so a single inset only warns at the
# magnitude a genuine datum/height-system mistake produces (the SWEDEN1M
# compound-CRS shift measured +23..36 m).  The OTHER actionable signature
# is systematic: many insets from ONE provider offset in the same
# direction, which vegetation cannot explain -- that fires at a lower
# per-inset magnitude but only across several airports agreeing in sign.
INSET_DATUM_WARNING_THRESHOLD_M = 10.0
SYSTEMATIC_OFFSET_MINIMUM_INSETS = 3
SYSTEMATIC_OFFSET_THRESHOLD_M = 3.0
SYSTEMATIC_OFFSET_SIGN_FRACTION = 0.8


def _warn_if_provider_offsets_systematic(provider_ring_offsets):
    """One warning per provider whose insets share a consistent DEM offset.

    ``provider_ring_offsets`` maps a provider code (the cache-file suffix)
    to the median feather-ring offsets of its baked insets.  A provider
    with at least SYSTEMATIC_OFFSET_MINIMUM_INSETS measured insets whose
    overall median exceeds SYSTEMATIC_OFFSET_THRESHOLD_M and whose
    per-inset offsets agree in sign (SYSTEMATIC_OFFSET_SIGN_FRACTION)
    looks datum-shifted, not vegetated, and is worth reporting.
    """
    for (provider_code, offsets) in sorted(provider_ring_offsets.items()):
        if len(offsets) < SYSTEMATIC_OFFSET_MINIMUM_INSETS:
            continue
        median_offset = float(numpy.median(offsets))
        if abs(median_offset) <= SYSTEMATIC_OFFSET_THRESHOLD_M:
            continue
        agreeing = sum(
            1
            for offset in offsets
            if (offset > 0) == (median_offset > 0)
        )
        if agreeing / len(offsets) < SYSTEMATIC_OFFSET_SIGN_FRACTION:
            continue
        UI.vprint(
            1,
            "   WARNING:",
            len(offsets),
            "airport insets from",
            provider_code,
            "differ from the base DEM in the same direction (median",
            str(round(median_offset, 2)) + " m)",
            "- a provider-wide vertical-datum problem is likely.",
        )


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
    # Record which insets actually bake into this DEM so the auto_patch
    # provenance stamp can report the true elevation source per airport (see
    # auto_patch.provenance).  An EMPTY list means the bake step ran but found
    # no cached inset -- the silent-raw-DEM case the provenance exists to make
    # loud.  Absence of the attribute (never set) means the bake never ran.
    baked_provenance = []
    tile.dem.airport_inset_provenance = baked_provenance
    if not inset_paths:
        return
    feather_m = getattr(tile, "airport_elevation_inset_feather_m", 60.0)
    provider_ring_offsets = {}
    for inset_path in inset_paths:
        try:
            ring_offset_m = _bake_one_inset(tile, inset_path, feather_m)
        except Exception as error:
            UI.vprint(
                1,
                "   WARNING: could not bake inset",
                os.path.basename(inset_path),
                ":",
                str(error),
            )
            continue
        if ring_offset_m is not None:
            stem = os.path.basename(inset_path)
            stem = stem[:-4] if stem.endswith(".tif") else stem
            provider_code = stem.rsplit("_", 1)[-1]
            provider_ring_offsets.setdefault(provider_code, []).append(
                ring_offset_m
            )
        baked_provenance.append(_inset_bake_provenance_entry(inset_path))
    _warn_if_provider_offsets_systematic(provider_ring_offsets)


def _inset_bake_provenance_entry(inset_path):
    """One baked-inset provenance record for the DEM stamp.

    Reads the provenance sidecar (``<icao>_<code>.json``) the fetch wrote —
    provider, source_ids, fetch_date — and derives the airport ICAO from the
    cache file name so ``auto_patch`` can attribute the inset to its airport.
    Always returns a dict; missing/unreadable sidecars degrade to just the
    parsed name fields.
    """
    basename = os.path.basename(inset_path)
    stem = basename[:-4] if basename.endswith(".tif") else basename
    icao = stem.rsplit("_", 1)[0] if "_" in stem else stem
    entry = {"icao": icao, "path": inset_path}
    sidecar = inset_path[:-4] + ".json" if inset_path.endswith(".tif") else None
    if sidecar and os.path.isfile(sidecar):
        try:
            with open(sidecar, "r") as handle:
                meta = json.load(handle)
            entry["provider"] = meta.get("provider")
            entry["source_ids"] = meta.get("source_ids") or []
            entry["fetch_date"] = meta.get("fetch_date")
            entry["native_resolution_m"] = meta.get("native_resolution_m")
        except Exception:
            pass
    return entry


def _bake_one_inset(tile, inset_path, feather_m):
    """Blend a single inset GeoTIFF into the working grid over its footprint.

    The blend weight ramps linearly from 0 at the inset's data edge to 1 at
    ``feather_m`` inside it, so the seam is a ramp not a cliff.  Cells with
    inset nodata keep the base value.

    Returns the median inset-vs-base offset (metres) measured over the
    feather ring, or ``None`` when nothing was measured -- the caller
    aggregates these per provider for the systematic-offset warning.
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
    ring_offset_m = None
    if numpy.any(ring):
        ring_offset_m = float(
            numpy.median(inset_values[ring] - window[ring])
        )
        # A few metres is the normal surface-vs-bare-earth gap (canopy in
        # the coarse base); only datum-class magnitudes warrant a warning.
        if abs(ring_offset_m) > INSET_DATUM_WARNING_THRESHOLD_M:
            UI.vprint(
                1,
                "   WARNING: elevation inset",
                os.path.basename(inset_path),
                "differs from the base DEM by a median",
                round(ring_offset_m, 2),
                "m over the feather ring (>%d m; check vertical datum)."
                % int(INSET_DATUM_WARNING_THRESHOLD_M),
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
    return ring_offset_m


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
    FACTOR (1, 2, 3, 6 or 9) for an explicit pin.  Accepts ``"1"``, ``"1/2"``,
    ``"0.5"``, ``"1/3"``, the finer ``"1/6"``/``"6"`` and ``"1/9"``/``"9"``
    pins (reachable only through an explicit pin or a numeric
    ``elevation_level``), and friends; an unrecognised value falls back to
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
    if text in ("1/6", "6"):
        return 6
    if text in ("1/9", "9"):
        return 9
    # A bare fraction "a/b" -> snap b/a to the nearest allowed factor.
    if "/" in text:
        try:
            numerator, denominator = text.split("/", 1)
            arc_seconds = float(numerator) / float(denominator)
            if arc_seconds > 0:
                factor = 1.0 / arc_seconds
                return min(
                    (1, 2, 3, 6, 9),
                    key=lambda candidate: (abs(candidate - factor), candidate),
                )
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
    """Choose the working-grid densification factor for a tile.

    Returns the historic airport-inset decision (1, 2 or 3) RAISED, never
    lowered, by any numeric ``elevation_level`` override:
    ``max(historic_factor, level_factor)`` (spec section 3.2).  An explicit
    non-auto ``working_grid_arc_seconds`` pin still wins outright, and when
    ``elevation_level`` is auto/invalid the return is behaviourally
    IDENTICAL to the historic decision (the byte-inert auto path).
    """
    historic = _historic_working_grid_factor(tile, base_dem)
    configured = parse_working_grid_arc_seconds(
        getattr(tile, "working_grid_arc_seconds", "auto")
    )
    if configured != "auto":
        # An explicit working-grid pin governs the grid outright; the
        # elevation-level override never overrules a user pin.
        return historic
    # Lazy import keeps both directions of the O4_Elevation_Level <-> this
    # module dependency lazy (that module lazily imports this one too).
    import O4_Elevation_Level as ELEVATION_LEVEL

    raw_level_value = getattr(tile, "elevation_level", "auto")
    if ELEVATION_LEVEL.is_coastline_mode(raw_level_value):
        # "Auto + coastline": the factor comes from the band stamp written
        # by ensure_coastline_band (disk-state-driven, so both build steps
        # agree; 1 while no band has been fetched -> historic unchanged).
        if not ELEVATION_LEVEL.has_gdal:
            return historic
        return max(historic, ELEVATION_LEVEL.coastline_grid_factor(tile))
    level = ELEVATION_LEVEL.parse_elevation_level(raw_level_value)
    if level is None or not ELEVATION_LEVEL.has_gdal:
        return historic
    if getattr(tile, "custom_dem", ""):
        # The user's pinned raster is trusted to carry the finer detail, so
        # the grid is densified to the full level factor, uncapped.
        level_factor = ELEVATION_LEVEL.LEVEL_GRID_FACTORS[level]
    else:
        level_factor = ELEVATION_LEVEL.grid_factor_for_level(
            level,
            ELEVATION_LEVEL.finest_wide_area_resolution_m(
                tile.lat,
                tile.lon,
                getattr(tile, "airport_elevation_providers", "auto"),
            ),
        )
    return max(historic, level_factor)


def _historic_working_grid_factor(tile, base_dem):
    """The pre-elevation-level working-grid decision (1, 2 or 3).

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


def cached_elevation_file_is_valid(cache_path):
    """Is a cached whole-tile elevation file present AND non-empty?

    A bare ``os.path.exists`` recycle check is not enough: an archive
    extraction that died mid-member (upstream CRC corruption, disk
    full, a kill) can leave a zero-byte file, and recycling it silently
    yields a zero-altitude tile.  Every base strategy's recycle test
    goes through here so poisoned caches self-heal on the next build.
    """
    try:
        return os.path.getsize(cache_path) > 0
    except OSError:
        return False

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
        import zlib

        cache_path = self.tile_cache_path(definition, lat, lon)
        if cached_elevation_file_is_valid(cache_path):
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
                    # Decompress BEFORE touching the destination, and land
                    # atomically: a corrupt member (upstream CRC damage --
                    # seen live in dem1/P32.zip) must neither abort the
                    # other members nor leave a zero-byte cache file behind.
                    try:
                        member_bytes = zip_ref.open(zipped_file, "r").read()
                    except (zipfile.BadZipFile, zlib.error, EOFError) as error:
                        UI.vprint(
                            1,
                            "    WARNING : skipping the corrupt archive "
                            "member",
                            zipped_file.filename,
                            "(" + str(error) + ") -- the archive on "
                            "viewfinderpanoramas.org is damaged for that "
                            "tile.",
                        )
                        continue
                    UI.vprint(2, "      Extracting", out_file_name)
                    temporary_path = out_file_name + ".part"
                    with open(temporary_path, "wb") as out:
                        out.write(member_bytes)
                    os.replace(temporary_path, out_file_name)
        # Success is judged on the tile actually requested: a corrupt
        # OTHER member in the same archive costs a warning, nothing more.
        return 1 if cached_elevation_file_is_valid(cache_path) else 0


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
        if cached_elevation_file_is_valid(cache_path):
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
        if cached_elevation_file_is_valid(cache_path):
            UI.vprint(2, "   Recycling ", cache_path)
            return 1
        UI.vprint(
            1,
            "    WARNING : This elevation source has no longer direct downloads !"
        )
        return 0


@register_access_strategy("hgt_archive_drop")
class HgtArchiveDropStrategy:
    """Manually downloaded .hgt tile archives recycled from a drop folder.

    For sources distributed through file-sharing folders with no stable
    per-tile URL scheme (Sonny's LiDAR Digital Terrain Models of Europe,
    published as Google Drive country archives): the user downloads the
    archives once and drops them -- the zip files themselves, or
    already-extracted ``.hgt`` tiles -- into
    ``Elevation_data/<drop_directory_name>/``; ``ensure_tile`` then
    extracts (or copies) the one NxxEyyy.hgt tile it needs to the legacy
    cache path on demand.

    ``covers`` additionally REQUIRES the tile to be locally present
    (cached, dropped bare, or found inside a dropped archive): automatic
    base selection must never pick a manual source whose data the user
    has not downloaded, since a wrong automatic pick means a
    zero-altitude tile.  Explicit selection bypasses coverage as usual
    and prints download instructions when the tile is missing.
    """

    def covers(self, definition, lat, lon):
        if not base_definition_covers_tile(definition, lat, lon):
            return False
        if cached_elevation_file_is_valid(
            self.tile_cache_path(definition, lat, lon)
        ):
            return True
        return self._locate_dropped_tile(definition, lat, lon) is not None

    def download_url(self, definition, lat, lon):
        return None

    def tile_cache_path(self, definition, lat, lon):
        return FNAMES.elevation_data(
            definition["legacy_keyword"], lat, lon
        )

    def drop_directory(self, definition):
        return os.path.join(
            FNAMES.Elevation_dir,
            definition.get("drop_directory_name", definition["code"]),
        )

    def manual_setup_information(self, definition):
        """Model data for the GUI's manual-setup affordance."""
        return _manual_drop_setup_information(
            definition,
            self.drop_directory(definition),
            "archives (zip) or extracted .hgt tiles",
        )

    def _locate_dropped_tile(self, definition, lat, lon):
        """Find one tile among the dropped files.

        Returns ``("hgt", path)`` for a bare .hgt file, ``("zip",
        archive_path, member_name)`` for a member of a dropped zip
        archive, or ``None``.  The match is case-insensitive on the
        ``NxxEyyy.hgt`` basename, whatever folder structure the archive
        carries inside.
        """
        import zipfile

        drop_directory = self.drop_directory(definition)
        if not os.path.isdir(drop_directory):
            return None
        wanted = (FNAMES.hem_latlon(lat, lon) + ".hgt").lower()
        entries = sorted(os.listdir(drop_directory))
        for entry in entries:
            if entry.lower() == wanted:
                return ("hgt", os.path.join(drop_directory, entry))
        for entry in entries:
            if not entry.lower().endswith(".zip"):
                continue
            archive_path = os.path.join(drop_directory, entry)
            try:
                with zipfile.ZipFile(archive_path, "r") as archive:
                    for member_name in archive.namelist():
                        if os.path.basename(member_name).lower() == wanted:
                            return ("zip", archive_path, member_name)
            except zipfile.BadZipFile:
                UI.vprint(
                    2, "      Skipping the unreadable archive", archive_path
                )
        return None

    def ensure_tile(self, definition, lat, lon, verbose=True):
        import shutil
        import zipfile

        cache_path = self.tile_cache_path(definition, lat, lon)
        if cached_elevation_file_is_valid(cache_path):
            UI.vprint(2, "   Recycling ", cache_path)
            return 1
        located = self._locate_dropped_tile(definition, lat, lon)
        if located is None:
            UI.vprint(
                1,
                "    WARNING : "
                + definition["code"]
                + " is a manual-download source: fetch the archives from "
                + definition.get("download_page", "its download page")
                + " and drop them (zip files or extracted .hgt tiles) into "
                + self.drop_directory(definition)
                + " .",
            )
            return 0
        if not os.path.isdir(os.path.dirname(cache_path)):
            os.makedirs(os.path.dirname(cache_path))
        if located[0] == "hgt":
            UI.vprint(
                1, "    Recycling", located[1], "from the drop folder."
            )
            shutil.copyfile(located[1], cache_path)
            return 1
        (_, archive_path, member_name) = located
        UI.vprint(
            1,
            "    Extracting",
            os.path.basename(member_name),
            "from the dropped archive",
            archive_path,
            ".",
        )
        with zipfile.ZipFile(archive_path, "r") as archive:
            with open(cache_path, "wb") as out:
                out.write(archive.open(member_name, "r").read())
        return 1


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
    # Serialise the download-if-missing critical section across concurrent
    # tile-build processes: adjacent tiles fetch an overlapping three-by-three
    # neighbourhood and would otherwise race to download the SAME base file
    # into this shared cache (docs/specs/parallel-tile-builds.md section 3.5).
    # The lock is keyed per (base definition code, latitude, longitude) inside
    # the tile's Elevation_data block directory; only the ``.lock`` sibling is
    # created, so the lock target file itself need not exist.
    lock_target = os.path.join(
        FNAMES.Elevation_dir,
        FNAMES.round_latlon(lat, lon),
        ".lock_" + definition["code"] + "_" + FNAMES.hem_latlon(lat, lon),
    )
    os.makedirs(os.path.dirname(lock_target), exist_ok=True)
    # The double-checked-locking re-check is inherent: every strategy's
    # ensure_tile is a no-op that recycles the cached tile when it is already
    # present, so a waiter that blocked until the first downloader finished
    # simply finds the file cached and returns without re-downloading.
    with O4_File_Lock.hold_file_lock(lock_target):
        return strategy_factory().ensure_tile(definition, lat, lon, verbose)


# =====================================================================
# Offline per-tile source summary (for the GUI tile-info surface)
# =====================================================================
def summarize_tile_elevation_sources(
    lat, lon, base_selector="auto", inset_providers_config="auto"
):
    """One offline snapshot of the elevation sources a build would use.

    Feeds the GUI's tile-info surface, so it is offline BY DESIGN:
    registry lookups, local file checks (including the manual
    drop-folder state) and the tile's cached inset ``index.json`` only
    -- never a discovery request -- making it safe to call on every
    selection change.  Returns a dictionary:

    * ``base_code`` / ``base_resolution_arc_seconds`` -- what the
      ``base_selector`` (the ``base_elevation_source`` configuration)
      resolves to for this tile right now.  When it resolves nothing
      the historic fallback (Viewfinderpanoramas 3 arc-second) is
      reported and ``base_is_fallback`` is True.
    * ``inset_providers`` -- ordered ``(code, native_resolution_m)``
      for every enabled airport-inset definition whose coverage box
      reaches this tile.
    * ``fetched_airports`` / ``no_coverage_airports`` -- ground truth
      from the cached inset index when the tile has been built or
      fetched before (both ``None`` when no index exists): airports
      with a fetched inset, and airports checked against every
      provider without coverage.
    """
    if not elevation_providers_dict:
        initialize_elevation_providers_dict()
    base_definition = resolve_base_definition(lat, lon, base_selector)
    base_is_fallback = base_definition is None
    if base_definition is None:
        base_definition = elevation_providers_dict.get("VIEWFINDER3")
    tile_bounding_box = (lon, lat, lon + 1, lat + 1)
    inset_providers = [
        (
            definition["code"],
            _parse_float(definition.get("native_resolution_m")),
        )
        for definition in select_provider_definitions(
            inset_providers_config, role=ROLE_AIRPORT_INSET
        )
        if _coverage_bbox_intersects(definition, tile_bounding_box)
    ]
    fetched_airports = None
    no_coverage_airports = None
    if os.path.isfile(FNAMES.airport_inset_index(lat, lon)):
        index = _read_index(lat, lon)
        fetched_airports = 0
        no_coverage_airports = 0
        for airport_record in index.values():
            statuses = [
                value
                for (key, value) in airport_record.items()
                if key not in ("checked", "probes", "bounding_box")
            ]
            if "ok" in statuses:
                fetched_airports += 1
            elif statuses:
                no_coverage_airports += 1
    return {
        "base_code": (
            base_definition["code"] if base_definition else None
        ),
        "base_resolution_arc_seconds": (
            _parse_float(base_definition.get("resolution_arc_seconds"))
            if base_definition
            else None
        ),
        "base_is_fallback": base_is_fallback,
        "inset_providers": inset_providers,
        "fetched_airports": fetched_airports,
        "no_coverage_airports": no_coverage_airports,
    }


def _definition_resolution_m(definition):
    """A definition's ground resolution in metres (arc-seconds ~ x30)."""
    meters = _parse_float(definition.get("native_resolution_m"))
    if meters is not None:
        return meters
    arc_seconds = _parse_float(definition.get("resolution_arc_seconds"))
    if arc_seconds is not None:
        return arc_seconds * 30.0
    return None


def _finest_automatic_resolution_m(lat, lon):
    """The finest resolution any AUTOMATIC provider offers at a tile.

    "Automatic" = enabled definitions whose strategy has no manual
    workflow; base definitions count only where their real coverage
    test passes (zone whitelists included), inset definitions where
    their coverage box reaches the tile.  Returns metres, or ``None``
    when nothing automatic covers (never in practice -- the worldwide
    Viewfinderpanoramas base always does).
    """
    tile_bounding_box = (lon, lat, lon + 1, lat + 1)
    finest = None
    for definition in elevation_providers_dict.values():
        if not definition.get("enabled", True):
            continue
        # Bathymetry providers are not terrain sources (spec section 2.1);
        # their resolution must never inform the "better elevation is
        # available" comparison.
        if definition.get("role") == ROLE_BATHYMETRY:
            continue
        # Surface models (radar DSMs, building-masked only at airport
        # footprints) are a FALLBACK quality class: whatever their grid
        # size says, they never make a genuine terrain model "not
        # better", so they must not veto the affordance (the global
        # 30 m GLO-30 would otherwise suppress the ~30.9 m lidar-derived
        # Sonny drop folder over all of Europe).
        if definition.get(SURFACE_MODEL_BUILDING_MASKING):
            continue
        strategy_factory = ACCESS_STRATEGIES.get(
            definition.get("access_strategy")
        )
        if strategy_factory is None:
            continue
        if hasattr(strategy_factory(), "manual_setup_information"):
            continue
        if definition.get("role") == ROLE_BASE:
            if not base_definition_covers_tile(definition, lat, lon):
                continue
        elif not _coverage_bbox_intersects(definition, tile_bounding_box):
            continue
        meters = _definition_resolution_m(definition)
        if meters is None:
            continue
        finest = meters if finest is None else min(finest, meters)
    return finest


def manual_elevation_setup_for_tile(lat, lon):
    """The manual-download providers WORTH setting up for a tile (MODEL).

    The model half of the GUI's "better elevation is available -- here
    is how" affordance: for every ENABLED definition (base or inset
    role) whose coverage box reaches the tile and whose access
    strategy declares a manual workflow (a
    ``manual_setup_information`` method), return one entry::

        {"code", "role", "native_resolution", "download_page",
         "drop_directory", "steps": [str, ...], "already_dropped": bool}

    A manual source is offered ONLY when it is strictly FINER than the
    best automatic source covering the tile (user ruling 2026-07-15):
    a Norwegian tile already gets 1 m airport lidar automatically, so
    the 30 m Sonny drop folder is not "better" and is not suggested
    there -- while a German tile, whose best automatic source is the
    90 m worldwide base, is exactly where the suggestion belongs.

    Pure data, computed offline (registry + a directory listing) --
    the view renders it verbatim and the controller only decides WHEN
    to ask.  ``already_dropped`` lets the view drop the affordance for
    providers the user has set up (their drop folder has content).
    """
    if not elevation_providers_dict:
        initialize_elevation_providers_dict()
    tile_bounding_box = (lon, lat, lon + 1, lat + 1)
    finest_automatic = _finest_automatic_resolution_m(lat, lon)
    entries = []
    for definition in sorted(
        elevation_providers_dict.values(),
        key=lambda entry: entry["code"],
    ):
        if not definition.get("enabled", True):
            continue
        # The coverage BOX is deliberately the only geographic test: a
        # manual source's covers() also requires dropped data, and this
        # affordance exists precisely for tiles where that data is
        # still missing.
        if not _coverage_bbox_intersects(definition, tile_bounding_box):
            continue
        strategy_factory = ACCESS_STRATEGIES.get(
            definition.get("access_strategy")
        )
        if strategy_factory is None:
            continue
        strategy = strategy_factory()
        if not hasattr(strategy, "manual_setup_information"):
            continue
        meters = _definition_resolution_m(definition)
        if (
            finest_automatic is not None
            and meters is not None
            and finest_automatic <= meters
        ):
            continue
        information = strategy.manual_setup_information(definition)
        if information is not None:
            entries.append(information)
    return entries


def _manual_drop_setup_information(definition, drop_directory, file_kinds):
    """Shared manual-setup entry builder for the drop-folder strategies."""
    already = False
    try:
        already = any(
            entry
            for entry in os.listdir(drop_directory)
            if not entry.startswith(".") and entry != "converted"
        )
    except OSError:
        pass
    resolution = definition.get("native_resolution_m")
    if resolution is None:
        resolution = definition.get("resolution_arc_seconds")
        resolution_text = (
            str(resolution) + " arc-second" if resolution else "unknown"
        )
    else:
        resolution_text = "%g m" % _parse_float(resolution, 0.0)
    return {
        "code": definition["code"],
        "role": definition.get("role", ROLE_AIRPORT_INSET),
        "native_resolution": resolution_text,
        "download_page": definition.get("download_page", ""),
        "drop_directory": drop_directory,
        "already_dropped": already,
        "steps": [
            "Open the download page in your browser and download the "
            + file_kinds
            + " covering your region.",
            "Drop the downloaded files into the folder below "
            "(no unpacking needed).",
            "Rebuild the tile; the data is picked up automatically. "
            "If the tile was already built once, refresh its elevation "
            "insets so cached no-coverage results are re-checked.",
        ],
        "attribution": definition.get("attribution", ""),
        "license": definition.get("license", ""),
    }


def tiles_with_inset_coverage(tiles, inset_providers_config="auto"):
    """The subset of ``(lat, lon)`` tiles any inset provider reaches.

    Pure bounding-box arithmetic against the enabled airport-inset
    definitions -- no file or network access -- so the GUI can call it
    for arbitrarily large selections.
    """
    if not elevation_providers_dict:
        initialize_elevation_providers_dict()
    definitions = select_provider_definitions(
        inset_providers_config, role=ROLE_AIRPORT_INSET
    )
    covered = []
    for (lat, lon) in tiles:
        tile_bounding_box = (lon, lat, lon + 1, lat + 1)
        if any(
            _coverage_bbox_intersects(definition, tile_bounding_box)
            for definition in definitions
        ):
            covered.append((lat, lon))
    return covered
