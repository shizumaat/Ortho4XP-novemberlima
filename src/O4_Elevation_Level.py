"""Tile-wide elevation detail level (the elevation analogue of imagery zoom).

Implements ``docs/specs/elevation-level-spec.md``: the per-tile
``elevation_level`` configuration value ("auto", "30", "10", "5", "1")
selects a tile-wide elevation overlay fetched through the declarative
provider registry of :mod:`O4_Airport_Elevation_Insets`, and drives the
working-grid densification factor.  ``auto`` is byte-inert: every entry
point returns immediately and the historic behaviour is untouched.

Import discipline: :mod:`O4_Airport_Elevation_Insets` is imported lazily
inside functions (it lazily imports this module from
``resolve_working_grid_factor``; keeping both directions lazy avoids any
import-order trap).  No GUI toolkit imports, per the core-module rule.
"""

import datetime
import json
import os

import numpy

try:
    from osgeo import gdal

    has_gdal = True
    gdal.UseExceptions()
except Exception:
    has_gdal = False

import O4_File_Names as FNAMES
import O4_Geo_Utils as GEO
import O4_UI_Utils as UI

# Level (metres) -> working-grid densification factor (grid spacing is
# 1/factor arc-seconds).  The "1" level carries meter-class sources on a
# 1/9 arc-second grid (~3.4 m posting), the practical whole-tile ceiling
# for the in-memory raster architecture (spec section 2, honesty rule).
LEVEL_GRID_FACTORS = {30: 1, 10: 3, 5: 6, 1: 9}

# Factors a numeric level may select, coarsest first, for the data cap.
_CAP_CANDIDATE_FACTORS = (1, 3, 6, 9)

# Tolerated posting-vs-source slack in the data cap: a 1/3 arc-second
# grid (10.3 m posting) is allowed to carry a 10 m source.
_POSTING_SLACK = 1.15

# Cells of blend payload per bake strip (bounds bake memory at any grid
# factor); module-level so tests can force multi-strip processing on
# small synthetic rasters.
STRIP_CELL_BUDGET = 4_000_000

# --- "Auto + coastline" mode (spec section 3.4) -----------------------
COASTLINE_MODE_VALUE = "coastline"

# Band decomposition: the coastline band is fetched as 0.1 degree cells
# of the tile whose centre lies within the configured band width (plus
# the cell half-diagonal) of the OpenStreetMap coastline.
COASTLINE_CELL_DEGREES = 0.1

# Approach-visibility ladder: per-cell warp resolution from the distance
# to the nearest airport bounding box on the tile.  On a 3 degree
# glideslope, 20 km out is roughly 11 nautical miles / 3,400 feet above
# ground (10 m detail plainly visible), 50 km out is roughly 8,500 feet,
# and beyond that sits the 15,000-feet-and-above regime where 1
# arc-second posting suffices -- far cells still buy lidar's vertical
# accuracy on the shoreline, where global bases are at their worst.
COASTLINE_NEAR_AIRPORT_KM = 20.0
COASTLINE_MID_AIRPORT_KM = 50.0
COASTLINE_MID_RESOLUTION_M = 20.0


def is_coastline_mode(value):
    """True when an ``elevation_level`` value selects "Auto + coastline"."""
    return (
        isinstance(value, str)
        and value.strip().lower() == COASTLINE_MODE_VALUE
    )


def parse_elevation_level(value):
    """Parse an ``elevation_level`` configuration value.

    Returns the level in metres (one of ``LEVEL_GRID_FACTORS``' keys) or
    ``None`` for "auto" (the default), empty, or anything unrecognised
    (with one warning, so a typo degrades to today's behaviour instead of
    failing the build).
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().lower()
        # "coastline" is a mode of its own (is_coastline_mode), not a
        # numeric level -- the numeric machinery treats it like auto.
        if value in ("", "auto", COASTLINE_MODE_VALUE):
            return None
    try:
        level = int(float(value))
    except (TypeError, ValueError):
        level = None
    if level not in LEVEL_GRID_FACTORS:
        UI.vprint(
            1,
            "   WARNING: unrecognised elevation_level",
            repr(value),
            "- using auto.",
        )
        return None
    return level


def grid_posting_metres(factor):
    """North-south posting of the working grid at a densification factor."""
    return GEO.lat_to_m / (3600.0 * factor)


def grid_factor_for_level(level_m, finest_source_resolution_m):
    """Working-grid factor for a level, capped by the available data.

    The cap keeps memory honest: densifying the grid finer than the
    finest source covering the tile buys nothing (spec section 2).  A
    ``None`` resolution means no wide-area source covers the tile at all,
    in which case the level cannot help and the historic factor 1 grid is
    kept (the caller still applies the max() rule against the airport
    inset auto factor).
    """
    if level_m not in LEVEL_GRID_FACTORS:
        return 1
    if finest_source_resolution_m is None:
        return 1
    effective_resolution_m = max(
        float(level_m), float(finest_source_resolution_m)
    )
    level_factor = LEVEL_GRID_FACTORS[level_m]
    for factor in _CAP_CANDIDATE_FACTORS:
        if grid_posting_metres(factor) <= effective_resolution_m * (
            _POSTING_SLACK
        ):
            return min(factor, level_factor)
    return level_factor


def select_tile_overlay_definition(lat, lon, level_m, providers_config="auto"):
    """Pick the provider definition serving the tile-wide overlay.

    Candidates are the enabled ``.elv`` definitions (any role) whose
    coverage reaches the tile AND whose access strategy declares
    ``supports_wide_area = True`` (the windowed ``/vsicurl``/overview
    readers -- fetching a whole tile through a tile-download strategy
    would pull an entire national campaign at native resolution).
    ``providers_config`` follows the ``airport_elevation_providers``
    convention ("auto" or an explicit comma list of codes).

    Ranking: finest native resolution first, then priority (descending),
    then code.  Returns the winning definition dict or ``None``.
    """
    import O4_Airport_Elevation_Insets as INSETS

    candidates = _wide_area_candidate_definitions(
        lat, lon, providers_config
    )
    if not candidates:
        return None

    def _ranking_key(definition):
        resolution_m = INSETS._definition_resolution_m(definition)
        if resolution_m is None:
            # A wide-area source that does not declare its resolution ranks
            # last (finite resolutions are always preferred as finer).
            resolution_m = float("inf")
        return (
            resolution_m,
            -float(definition.get("priority", 0.0)),
            definition["code"],
        )

    return min(candidates, key=_ranking_key)


def _wide_area_candidate_definitions(lat, lon, providers_config="auto"):
    """Enabled wide-area definitions (any role) whose coverage reaches a tile.

    Shared candidate set for :func:`select_tile_overlay_definition` and
    :func:`finest_wide_area_resolution_m`.  Mirrors the filtering conventions
    of :func:`O4_Airport_Elevation_Insets.select_provider_definitions`:
    ``providers_config`` "auto" admits every enabled definition, while an
    explicit comma-separated list of codes restricts the set to those codes
    (the list ORDER is irrelevant here -- the resolution ranking of the
    caller decides the winner, so the config only filters).  A definition
    qualifies only when its access strategy class declares a truthy
    ``supports_wide_area`` attribute (the windowed readers) and its coverage
    reaches the tile: role=base definitions use the full
    :func:`O4_Airport_Elevation_Insets.base_definition_covers_tile` test,
    all others the cheap coverage-box intersection.  Bathymetry providers
    (role=bathymetry) are excluded outright -- their tidal-datum depths are
    never terrain (spec section 2.1).
    """
    import O4_Airport_Elevation_Insets as INSETS

    if not INSETS.elevation_providers_dict:
        INSETS.initialize_elevation_providers_dict()

    config_value = (providers_config or "auto").strip()
    if config_value.lower() == "auto":
        allowed_codes = None
    else:
        allowed_codes = {
            token.strip()
            for token in config_value.split(",")
            if token.strip()
        }

    tile_bounding_box = (lon, lat, lon + 1, lat + 1)
    candidates = []
    for definition in INSETS.elevation_providers_dict.values():
        if not definition.get("enabled", True):
            continue
        if (
            allowed_codes is not None
            and definition["code"] not in allowed_codes
        ):
            continue
        strategy_factory = INSETS.ACCESS_STRATEGIES.get(
            definition.get("access_strategy")
        )
        if strategy_factory is None:
            continue
        if not getattr(strategy_factory, "supports_wide_area", False):
            continue
        role = definition.get("role", INSETS.ROLE_AIRPORT_INSET)
        if role == INSETS.ROLE_BATHYMETRY:
            # Bathymetry providers deliver tidal-datum seabed depth and are
            # never terrain sources (spec section 2.1); they must never feed
            # the elevation_level wide-area overlay even if a future one uses
            # a wide-area access strategy.
            continue
        if role == INSETS.ROLE_BASE:
            if not INSETS.base_definition_covers_tile(definition, lat, lon):
                continue
        elif not INSETS._coverage_bbox_intersects(
            definition, tile_bounding_box
        ):
            continue
        candidates.append(definition)
    return candidates


def finest_wide_area_resolution_m(lat, lon, providers_config="auto"):
    """Finest resolution (metres) among wide-area sources covering the tile.

    Same candidate set as :func:`select_tile_overlay_definition`; used by
    the working-grid data cap.  Returns ``None`` when nothing covers.
    """
    import O4_Airport_Elevation_Insets as INSETS

    resolutions = [
        INSETS._definition_resolution_m(definition)
        for definition in _wide_area_candidate_definitions(
            lat, lon, providers_config
        )
    ]
    resolutions = [
        resolution_m
        for resolution_m in resolutions
        if resolution_m is not None
    ]
    if not resolutions:
        return None
    return min(resolutions)


def resolve_tile_overlay_plan(tile):
    """The single source of truth mapping a tile to its overlay artefact.

    Purely offline and deterministic from the tile configuration and the
    provider registry, so the fetch (step 1) and the bake (steps 1 and 2)
    always agree on the same cache path.  Returns ``None`` when the level
    is auto, GDAL is unavailable, a ``custom_dem`` pins the raster, or no
    wide-area provider covers the tile; otherwise a dict with keys
    ``definition``, ``factor``, ``target_resolution_m``, ``path``.
    """
    raw_value = getattr(tile, "elevation_level", "auto")
    if is_coastline_mode(raw_value):
        return resolve_coastline_band_plan(tile)
    level = parse_elevation_level(raw_value)
    if level is None:
        return None
    if not has_gdal:
        UI.vprint(
            1,
            "   INFO: elevation_level",
            level,
            "requires the GDAL python bindings; keeping auto behaviour.",
        )
        return None
    if getattr(tile, "custom_dem", ""):
        UI.vprint(
            1,
            "   INFO: custom_dem is set; elevation_level",
            level,
            "keeps the custom raster and only densifies the grid.",
        )
        return None
    providers_config = getattr(tile, "airport_elevation_providers", "auto")
    definition = select_tile_overlay_definition(
        tile.lat, tile.lon, level, providers_config
    )
    if definition is None:
        UI.vprint(
            1,
            "   INFO: no wide-area elevation source finer than the base",
            "covers this tile; elevation_level",
            level,
            "has no effect.",
        )
        return None
    import O4_Airport_Elevation_Insets as INSETS

    finest = INSETS._definition_resolution_m(definition)
    factor = grid_factor_for_level(level, finest)
    if factor < LEVEL_GRID_FACTORS[level]:
        UI.vprint(
            1,
            "   INFO: finest source covering this tile is",
            definition["code"],
            "(",
            finest,
            "m ); elevation_level",
            level,
            "capped to a 1/%d arc-second grid." % factor,
        )
    target_resolution_m = round(grid_posting_metres(factor), 2)
    path = FNAMES.tile_overlay_dem(
        tile.lat, tile.lon, definition["code"], target_resolution_m
    )
    return {
        "definition": definition,
        "factor": factor,
        "target_resolution_m": target_resolution_m,
        "path": path,
    }


def ensure_tile_overlay(tile, dico_airports=None):
    """Fetch (or recycle) the tile-wide overlay artefact for the tile.

    Numeric levels: follows the airport-inset orchestration conventions —
    recycle an existing artefact, honour a cached no-coverage negative in
    the overlay ``index.json``, otherwise fetch through
    :func:`O4_Airport_Elevation_Insets.fetch_inset` with the whole-tile
    bounding box ``(lon, lat, lon + 1, lat + 1)``, write the provenance
    sidecar, and record the outcome in the index.  Logs the estimated
    raster size before fetching.  Returns the cached path or ``None``.

    "Auto + coastline" mode dispatches to :func:`ensure_coastline_band`
    instead; ``dico_airports`` (the step-1 airport dictionary) feeds its
    approach-visibility ladder and is unused by numeric levels.
    """
    if is_coastline_mode(getattr(tile, "elevation_level", "auto")):
        return ensure_coastline_band(tile, dico_airports)
    plan = resolve_tile_overlay_plan(tile)
    if plan is None:
        return None
    overlay_path = plan["path"]
    if os.path.isfile(overlay_path):
        UI.vprint(
            2,
            "   Recycling cached tile elevation overlay",
            os.path.basename(overlay_path),
        )
        return overlay_path

    import O4_Airport_Elevation_Insets as INSETS

    lat = tile.lat
    lon = tile.lon
    definition = plan["definition"]
    target_resolution_m = plan["target_resolution_m"]
    # Key the index by the artefact's basename stem so different levels
    # (different target resolutions -> different cache files) each carry
    # their own positive/negative record.
    index_key = os.path.splitext(os.path.basename(overlay_path))[0]
    index_path = FNAMES.tile_overlay_index(lat, lon)
    index = _read_overlay_index(index_path)
    if index.get(index_key) == INSETS.NO_COVERAGE:
        UI.vprint(
            2,
            "   No wide-area elevation coverage recorded for",
            definition["code"],
            "over this tile; skipping the overlay fetch.",
        )
        return None

    level = parse_elevation_level(
        getattr(tile, "elevation_level", "auto")
    )
    centre_latitude = lat + 0.5
    estimated_columns = max(
        int(GEO.lon_to_m(centre_latitude) / target_resolution_m), 1
    )
    estimated_rows = max(int(GEO.lat_to_m / target_resolution_m), 1)
    estimated_megabytes = (
        estimated_columns * estimated_rows * 4 / (1024.0 * 1024.0)
    )
    UI.vprint(
        1,
        "   Fetching tile elevation overlay from",
        definition["code"],
        "at",
        target_resolution_m,
        "m (about",
        round(estimated_megabytes, 1),
        "MB uncompressed float32 raster).",
    )

    bounding_box = (lon, lat, lon + 1, lat + 1)
    fetch_raised = False
    try:
        provenance = INSETS.fetch_inset(
            definition, bounding_box, target_resolution_m, overlay_path
        )
    except Exception as error:
        # Network/GDAL failures must never abort the build; degrade to the
        # base elevation source instead.  A raised failure is NOT recorded
        # as a no-coverage negative (it may be a transient outage).
        provenance = None
        fetch_raised = True
        UI.vprint(2, "   Tile elevation overlay fetch error:", str(error))

    if provenance is None:
        if not fetch_raised:
            # A clean "no usable coverage" answer is a durable fact; cache
            # it so a rebuild never re-queries the discovery service.
            index[index_key] = INSETS.NO_COVERAGE
            _write_overlay_index(index_path, index)
        UI.lvprint(
            0,
            "   WARNING: elevation_level",
            level,
            "was requested but the tile-wide elevation overlay fetch"
            " failed; the build continues on the base elevation source.",
        )
        return None

    provenance = dict(provenance)
    provenance["fetch_date"] = datetime.date.today().isoformat()
    provenance_path = FNAMES.tile_overlay_provenance(
        lat, lon, definition["code"], target_resolution_m
    )
    os.makedirs(os.path.dirname(provenance_path), exist_ok=True)
    with open(provenance_path, "w") as handle:
        json.dump(provenance, handle, indent=2, sort_keys=True)
    index[index_key] = "ok"
    _write_overlay_index(index_path, index)
    return overlay_path


def ensure_coastline_band(tile, dico_airports):
    """Fetch (or recycle) the coastline lidar band for the tile.

    Spec section 3.4.  Loads the tile's OpenStreetMap coastline through
    the pipeline's shared cache (``cached_suffix="coastline"``), selects
    the 0.1 degree cells whose centre lies within
    ``tile.elevation_coastline_band_km`` (plus the cell half-diagonal) of
    the coastline, grades each cell's warp resolution by the
    approach-visibility ladder (distance from the cell centre to the
    nearest airport bounding box from ``dico_airports``), fetches each
    missing cell through the wide-area provider machinery, mosaics the
    cells into a single band VRT, and writes the ``index.json`` stamp
    recording the chosen working-grid factor (3 when any near-airport
    cell exists, else 1) plus per-cell outcomes.  A cell whose raster is
    one constant value everywhere (a fill some providers serve beyond
    their true extent instead of nodata) fails the plausibility probe in
    :func:`O4_Airport_Elevation_Insets.geotiff_is_constant_value` and is
    recorded as no-coverage -- cached and freshly fetched cells alike --
    so the bake keeps the base elevation source there.  Returns the band VRT
    path, or ``None`` when the mode is inactive, GDAL is missing,
    ``custom_dem`` is set, no wide-area provider covers the tile, the
    tile has no coastline, or nothing could be fetched.  Failures never
    raise; they degrade loudly like the numeric-level fetch.
    """
    if not is_coastline_mode(getattr(tile, "elevation_level", "auto")):
        return None
    if not has_gdal:
        UI.vprint(
            1,
            "   INFO: elevation_level coastline requires the GDAL python"
            " bindings; keeping auto behaviour.",
        )
        return None
    if getattr(tile, "custom_dem", ""):
        UI.vprint(
            1,
            "   INFO: custom_dem is set; the coastline elevation band is"
            " skipped (the custom raster is kept).",
        )
        return None

    lat = tile.lat
    lon = tile.lon
    providers_config = getattr(tile, "airport_elevation_providers", "auto")
    definition = select_tile_overlay_definition(
        lat, lon, None, providers_config
    )
    if definition is None:
        UI.vprint(
            1,
            "   INFO: no wide-area elevation source covers this tile; the"
            " coastline elevation band has no effect.",
        )
        return None
    code = definition["code"]

    # Coastline geometry through the pipeline's shared cache (the vector
    # step prefetches the very same query, so no extra download here).
    import O4_OSM_Utils as OSM

    coastline_layer = OSM.OSM_layer()
    if not OSM.OSM_queries_to_OSM_layer(
        ['way["natural"="coastline"]'],
        coastline_layer,
        lat,
        lon,
        [],
        cached_suffix="coastline",
    ):
        UI.lvprint(
            0,
            "   WARNING: the coastline download for the elevation band"
            " failed; the band is skipped and the build continues.",
        )
        return None
    coastline = OSM.OSM_to_MultiLineString(coastline_layer, lat, lon)
    # OSM_to_MultiLineString subtracts (lon, lat) from every node, so the
    # geometry is in TILE-RELATIVE degree offsets (x = longitude offset,
    # y = latitude offset, each roughly 0..1 within the tile).
    if coastline.is_empty:
        UI.vprint(
            1,
            "   INFO: this tile has no coastline; the elevation band is"
            " skipped.",
        )
        return None

    from shapely.affinity import scale as _scale_geometry
    from shapely.geometry import Point

    metres_per_degree_longitude = GEO.lon_to_m(lat + 0.5)
    metres_per_degree_latitude = GEO.lat_to_m
    # Work in a metre-scaled plane so shapely's .distance is metric.
    scaled_coastline = _scale_geometry(
        coastline,
        xfact=metres_per_degree_longitude,
        yfact=metres_per_degree_latitude,
        origin=(0.0, 0.0),
    )
    cell_half_diagonal_m = 0.5 * (
        (COASTLINE_CELL_DEGREES * metres_per_degree_longitude) ** 2
        + (COASTLINE_CELL_DEGREES * metres_per_degree_latitude) ** 2
    ) ** 0.5
    band_reach_m = (
        float(getattr(tile, "elevation_coastline_band_km", 5.0)) * 1000.0
        + cell_half_diagonal_m
    )

    # Airport bounding boxes (west, south, east, north) drive the
    # approach-visibility ladder; none -> every cell is a far-tier cell.
    import O4_Airport_Elevation_Insets as INSETS

    if dico_airports:
        airport_boxes = list(
            INSETS._airport_bounding_boxes(tile, dico_airports).values()
        )
    else:
        airport_boxes = []

    near_resolution_m = round(grid_posting_metres(3), 2)
    mid_resolution_m = float(COASTLINE_MID_RESOLUTION_M)
    far_resolution_m = round(grid_posting_metres(1), 2)

    def _nearest_airport_distance_m(cell_centre_lon, cell_centre_lat):
        """Metre distance to the nearest airport box (0 inside), inf if none.

        True point-to-rectangle distance under axis-aligned metre scaling:
        the nearest point on an axis-aligned box is the per-axis clamp, and
        independent axis scaling preserves that, so each axis gap is scaled
        by its own metres-per-degree factor before combining.
        """
        best = float("inf")
        for west, south, east, north in airport_boxes:
            gap_longitude_deg = max(
                west - cell_centre_lon, 0.0, cell_centre_lon - east
            )
            gap_latitude_deg = max(
                south - cell_centre_lat, 0.0, cell_centre_lat - north
            )
            distance_m = (
                (gap_longitude_deg * metres_per_degree_longitude) ** 2
                + (gap_latitude_deg * metres_per_degree_latitude) ** 2
            ) ** 0.5
            if distance_m < best:
                best = distance_m
        return best

    # Select coastal cells and grade each by the approach ladder.
    cells = []
    cell_count = 10  # a 1 degree tile is a 10 x 10 grid of 0.1 degree cells
    for cell_column in range(cell_count):
        for cell_row in range(cell_count):
            centre_offset_longitude = (
                cell_column + 0.5
            ) * COASTLINE_CELL_DEGREES
            centre_offset_latitude = (
                cell_row + 0.5
            ) * COASTLINE_CELL_DEGREES
            distance_to_coast_m = scaled_coastline.distance(
                Point(
                    centre_offset_longitude * metres_per_degree_longitude,
                    centre_offset_latitude * metres_per_degree_latitude,
                )
            )
            if distance_to_coast_m > band_reach_m:
                continue
            centre_longitude = lon + centre_offset_longitude
            centre_latitude = lat + centre_offset_latitude
            airport_distance_m = _nearest_airport_distance_m(
                centre_longitude, centre_latitude
            )
            if airport_distance_m <= COASTLINE_NEAR_AIRPORT_KM * 1000.0:
                tier = "near"
                resolution_m = near_resolution_m
            elif airport_distance_m <= COASTLINE_MID_AIRPORT_KM * 1000.0:
                tier = "mid"
                resolution_m = mid_resolution_m
            else:
                tier = "far"
                resolution_m = far_resolution_m
            cell_path = FNAMES.coastline_band_cell_dem(
                lat, lon, cell_column, cell_row, code, resolution_m
            )
            cells.append(
                {
                    "column": cell_column,
                    "row": cell_row,
                    "tier": tier,
                    "resolution_m": resolution_m,
                    "path": cell_path,
                    "stem": os.path.splitext(
                        os.path.basename(cell_path)
                    )[0],
                }
            )

    if not cells:
        UI.vprint(
            1,
            "   INFO: no tile cell lies within the coastline band; the"
            " elevation band is skipped.",
        )
        return None

    near_tier_cells = sum(1 for cell in cells if cell["tier"] == "near")
    mid_tier_cells = sum(1 for cell in cells if cell["tier"] == "mid")
    far_tier_cells = sum(1 for cell in cells if cell["tier"] == "far")
    UI.vprint(
        1,
        "   Coastline elevation band from",
        code,
        "-",
        len(cells),
        "cell(s) (",
        near_tier_cells,
        "near /",
        mid_tier_cells,
        "mid /",
        far_tier_cells,
        "far ).",
    )

    band_directory = FNAMES.coastline_band_directory(lat, lon)
    os.makedirs(band_directory, exist_ok=True)
    stamp_path = FNAMES.coastline_band_index(lat, lon)
    previous_stamp = _read_coastline_band_stamp(stamp_path)
    # Preserve every previously recorded per-cell negative when rewriting.
    cell_outcomes = {
        stem: outcome
        for stem, outcome in previous_stamp.get("cells", {}).items()
        if outcome == INSETS.NO_COVERAGE
    }

    def _discard_implausible_cell(cell_path, stem):
        """Delete a constant-value cell and record a durable negative.

        Some providers fill windows beyond their true data extent with a
        constant value instead of nodata (the Spanish PNOA Web Coverage
        Service serves 0.0 over Portugal), which would bake the coast
        flat to that constant; recording no-coverage makes the bake fall
        back to the base elevation source there.
        """
        try:
            os.remove(cell_path)
        except OSError:
            pass
        cell_outcomes[stem] = INSETS.NO_COVERAGE
        UI.vprint(
            1,
            "   INFO: coastline band cell",
            stem,
            "is one constant value everywhere (a fill served beyond the"
            " provider's true extent); recording no coverage so the base"
            " elevation source is kept there.",
        )

    for cell in cells:
        cell_path = cell["path"]
        stem = cell["stem"]
        if os.path.isfile(cell_path):
            # A cached cell is recycled, but only after the same
            # plausibility probe a fresh fetch gets: caches poisoned
            # before the guard existed heal here.
            if INSETS.geotiff_is_constant_value(cell_path):
                _discard_implausible_cell(cell_path, stem)
            else:
                cell_outcomes[stem] = "ok"
            continue
        if cell_outcomes.get(stem) == INSETS.NO_COVERAGE:
            # A durable no-coverage negative from a previous run: honour it
            # without re-querying the discovery service.
            continue
        try:
            provenance = INSETS.fetch_inset(
                definition,
                (
                    lon + cell["column"] * COASTLINE_CELL_DEGREES,
                    lat + cell["row"] * COASTLINE_CELL_DEGREES,
                    lon
                    + (cell["column"] + 1) * COASTLINE_CELL_DEGREES,
                    lat + (cell["row"] + 1) * COASTLINE_CELL_DEGREES,
                ),
                cell["resolution_m"],
                cell_path,
            )
        except Exception as error:
            # A raised failure (network/GDAL) is skipped and NOT recorded as
            # a durable negative -- it may be a transient outage.
            UI.vprint(
                2,
                "   Coastline band cell fetch error at",
                stem,
                ":",
                str(error),
            )
            continue
        if provenance is None:
            # A clean "no usable coverage" answer is durable; record it.
            cell_outcomes[stem] = INSETS.NO_COVERAGE
        elif INSETS.geotiff_is_constant_value(cell_path):
            # Fetched, but implausible: a constant raster is a fill served
            # beyond the provider's true extent, never genuine lidar.
            _discard_implausible_cell(cell_path, stem)
        else:
            cell_outcomes[stem] = "ok"

    existing_cells = [
        cell for cell in cells if os.path.isfile(cell["path"])
    ]
    if not existing_cells:
        stamp = {
            "provider": code,
            "factor": 1,
            "cells": cell_outcomes,
            "checked": datetime.date.today().isoformat(),
        }
        _write_coastline_band_stamp(stamp_path, stamp)
        UI.lvprint(
            0,
            "   WARNING: no coastline band cell could be fetched for this"
            " tile; the build continues on the base elevation source.",
        )
        return None

    vrt_path = FNAMES.coastline_band_vrt(lat, lon, code)
    mosaic = gdal.BuildVRT(
        vrt_path,
        [cell["path"] for cell in existing_cells],
        options=gdal.BuildVRTOptions(
            resolution="highest",
            resampleAlg="bilinear",
            srcNodata=-32768,
            VRTNodata=-32768,
        ),
    )
    # Release the handle so the virtual mosaic is flushed to disk.
    mosaic = None

    band_factor = (
        3 if any(cell["tier"] == "near" for cell in existing_cells) else 1
    )
    stamp = {
        "provider": code,
        "factor": band_factor,
        "finest_resolution_m": min(
            cell["resolution_m"] for cell in existing_cells
        ),
        "vrt": os.path.basename(vrt_path),
        "cells": cell_outcomes,
        "checked": datetime.date.today().isoformat(),
    }
    _write_coastline_band_stamp(stamp_path, stamp)
    return vrt_path


def resolve_coastline_band_plan(tile):
    """Plan-shaped view of the cached coastline band (stamp-driven).

    The coastline analogue of :func:`resolve_tile_overlay_plan`: purely
    disk-state-driven (the ``index.json`` stamp written by
    :func:`ensure_coastline_band`), so both build steps agree without a
    fetch.  Returns ``None`` when the mode is inactive, GDAL is missing,
    ``custom_dem`` is set, or no stamp/VRT exists; otherwise a dict with
    the same keys the bake consumes: ``definition`` (a minimal
    ``{"code": ...}`` from the stamp), ``factor``,
    ``target_resolution_m`` (the finest cell resolution in the band) and
    ``path`` (the band VRT).
    """
    if not is_coastline_mode(getattr(tile, "elevation_level", "auto")):
        return None
    if not has_gdal:
        return None
    if getattr(tile, "custom_dem", ""):
        return None
    stamp_path = FNAMES.coastline_band_index(tile.lat, tile.lon)
    stamp = _read_coastline_band_stamp(stamp_path)
    if not stamp:
        return None
    vrt_name = stamp.get("vrt")
    if not vrt_name:
        return None
    vrt_path = os.path.join(
        FNAMES.coastline_band_directory(tile.lat, tile.lon), vrt_name
    )
    if not os.path.isfile(vrt_path):
        return None
    return {
        "definition": {"code": stamp["provider"]},
        "factor": int(stamp["factor"]),
        "target_resolution_m": stamp["finest_resolution_m"],
        "path": vrt_path,
    }


def coastline_grid_factor(tile):
    """Working-grid factor recorded in the coastline band stamp.

    Returns 1 when no stamp exists (band never fetched -- the grid stays
    historic, keeping the mode inert until step 1 has run).
    """
    stamp = _read_coastline_band_stamp(
        FNAMES.coastline_band_index(tile.lat, tile.lon)
    )
    try:
        return int(stamp["factor"])
    except (KeyError, TypeError, ValueError):
        return 1


def _read_coastline_band_stamp(stamp_path):
    """Read the coastline band ``index.json`` stamp (factor + cell outcomes).

    Returns an empty dictionary when the stamp does not exist yet or cannot
    be parsed, mirroring :func:`_read_overlay_index`.
    """
    if not os.path.isfile(stamp_path):
        return {}
    try:
        with open(stamp_path, "r") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _write_coastline_band_stamp(stamp_path, stamp):
    """Write the coastline band stamp, creating its directory."""
    os.makedirs(os.path.dirname(stamp_path), exist_ok=True)
    with open(stamp_path, "w") as handle:
        json.dump(stamp, handle, indent=2, sort_keys=True)


def _read_overlay_index(index_path):
    """Read the tile-overlay discovery index (positives and negatives).

    Returns an empty dictionary when the index does not exist yet or cannot
    be parsed, mirroring the airport-inset ``_read_index`` conventions.
    """
    if not os.path.isfile(index_path):
        return {}
    try:
        with open(index_path, "r") as handle:
            return json.load(handle)
    except Exception:
        return {}


def _write_overlay_index(index_path, index):
    """Write the tile-overlay discovery index, creating its directory."""
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    with open(index_path, "w") as handle:
        json.dump(index, handle, indent=2, sort_keys=True)


def bake_tile_overlay_into_alt_dem(tile):
    """Blend the cached tile-wide overlay into ``tile.dem.alt_dem``.

    Runs after :func:`O4_Airport_Elevation_Insets.
    densify_tile_dem_for_insets` and BEFORE the airport smoothing pass
    (the overlay is base terrain and is smoothed like base terrain;
    airport insets keep baking last, after smoothing).  Strip-wise
    windowed GDAL reads keep memory bounded at any grid factor.

    Blend weight per cell = (distance-to-tile-edge ramp over the feather
    width) x (box-blurred valid-data mask), so the outer feather band
    always returns to the shared base raster (cross-tile continuity) and
    interior no-coverage regions (ocean, campaign edges) hand back to the
    base softly.  Cells whose bilinear support touches overlay nodata
    keep the base value outright; base-nodata cells take the overlay
    outright (mirroring the airport-inset bake's sentinel guard).

    Returns ``True`` when at least one strip blended overlay data.
    """
    import O4_Airport_Elevation_Insets as INSETS

    plan = resolve_tile_overlay_plan(tile)
    if plan is None:
        return False
    if tile.dem is None or tile.dem.alt_dem is None:
        return False
    overlay_path = plan["path"]
    if not os.path.isfile(overlay_path):
        return False

    base_dem = tile.dem
    dataset = gdal.Open(overlay_path)
    try:
        band = dataset.GetRasterBand(1)
        overlay_columns = dataset.RasterXSize
        overlay_rows = dataset.RasterYSize
        geotransform = dataset.GetGeoTransform()
        overlay_nodata = band.GetNoDataValue()
        if overlay_nodata is None:
            overlay_nodata = -32768.0
        overlay_nodata = numpy.float32(overlay_nodata)
        # Pixel-centre origin, matching read_elevation_from_file's
        # AREA_OR_POINT=Area convention.
        overlay_x_origin = geotransform[0] + 0.5 * geotransform[1]
        overlay_x_step = geotransform[1]
        overlay_y_origin = geotransform[3] + 0.5 * geotransform[5]
        overlay_y_step = -geotransform[5]  # positive, rows go north->south
        if overlay_x_step <= 0 or overlay_y_step <= 0:
            UI.vprint(
                1,
                "   WARNING: unexpected overlay geotransform in",
                os.path.basename(overlay_path),
                "- skipping the tile overlay bake.",
            )
            return False

        grid_columns = base_dem.nxdem
        grid_rows = base_dem.nydem
        x_step = (base_dem.x1 - base_dem.x0) / (grid_columns - 1)
        y_step = (base_dem.y1 - base_dem.y0) / (grid_rows - 1)
        # Absolute cell-centre coordinates of the working grid.
        grid_x = (
            tile.lon + base_dem.x0 + numpy.arange(grid_columns) * x_step
        )
        grid_y = (
            tile.lat + base_dem.y1 - numpy.arange(grid_rows) * y_step
        )

        centre_latitude = tile.lat + (base_dem.y0 + base_dem.y1) / 2.0
        metres_per_degree_longitude = GEO.lon_to_m(centre_latitude)
        metres_per_degree_latitude = GEO.lat_to_m
        feather_m = float(
            getattr(tile, "airport_elevation_inset_feather_m", 60.0)
        )

        # Fractional overlay pixel coordinates of every grid column (the
        # grid and the overlay are both axis-aligned in EPSG:4326, so the
        # bilinear sample separates into per-column and per-row terms).
        # The overlay pixels are areas: coordinates up to half a pixel
        # outside the outermost pixel CENTRE are still covered (this is
        # where the tile-edge grid rows land), sampled by clamping the
        # bilinear support to the edge pixel.
        # (A millionth of a pixel of slack absorbs degree-arithmetic
        # rounding at the exact half-pixel boundary.)
        half_pixel_slack = 0.5 + 1e-6
        column_pixel = (grid_x - overlay_x_origin) / overlay_x_step
        column_inside = (column_pixel >= -half_pixel_slack) & (
            column_pixel <= overlay_columns - 1 + half_pixel_slack
        )
        column_pixel = numpy.clip(column_pixel, 0.0, overlay_columns - 1)
        column_index = numpy.clip(
            numpy.floor(column_pixel).astype(numpy.int64),
            0,
            overlay_columns - 2,
        )
        column_fraction = (column_pixel - column_index).astype(numpy.float32)

        # Distance-to-tile-edge ramp along columns (metres); the overlay
        # bounding box is exactly the tile, so the ramp doubles as the
        # outside-the-tile guard for combined base rasters whose grid
        # extends past the 1 degree square.
        edge_west = (grid_x - tile.lon) * metres_per_degree_longitude
        edge_east = (tile.lon + 1.0 - grid_x) * metres_per_degree_longitude
        column_edge_m = numpy.minimum(edge_west, edge_east)

        # Feather radius in grid cells for the valid-mask blur.
        posting_m = y_step * metres_per_degree_latitude
        feather_cells = max(int(numpy.ceil(feather_m / posting_m)), 1)

        # Strip sizing: bounded payload per strip plus the blur halo.
        strip_rows = max(int(STRIP_CELL_BUDGET // max(grid_columns, 1)), 8)

        blended_any = False
        ring_samples = []

        def _read_overlay_rows(row_start, row_stop):
            """Read overlay rows [row_start, row_stop) clipped to the file."""
            row_start = max(row_start, 0)
            row_stop = min(row_stop, overlay_rows)
            if row_start >= row_stop:
                return row_start, None
            window = band.ReadAsArray(
                0, row_start, overlay_columns, row_stop - row_start
            )
            if window is None:
                return row_start, None
            return row_start, window.astype(numpy.float32, copy=False)

        def _strip_values_and_valid(row_indices):
            """Bilinear overlay values + validity for the given grid rows."""
            y_values = grid_y[row_indices]
            row_pixel = (overlay_y_origin - y_values) / overlay_y_step
            row_inside = (row_pixel >= -half_pixel_slack) & (
                row_pixel <= overlay_rows - 1 + half_pixel_slack
            )
            row_pixel = numpy.clip(row_pixel, 0.0, overlay_rows - 1)
            row_index = numpy.clip(
                numpy.floor(row_pixel).astype(numpy.int64),
                0,
                overlay_rows - 2,
            )
            row_fraction = (row_pixel - row_index).astype(numpy.float32)
            read_start, window = _read_overlay_rows(
                int(row_index.min()), int(row_index.max()) + 2
            )
            if window is None:
                shape = (len(row_indices), grid_columns)
                return (
                    numpy.zeros(shape, dtype=numpy.float32),
                    numpy.zeros(shape, dtype=bool),
                )
            local_row = row_index - read_start
            v00 = window[numpy.ix_(local_row, column_index)]
            v01 = window[numpy.ix_(local_row, column_index + 1)]
            v10 = window[numpy.ix_(local_row + 1, column_index)]
            v11 = window[numpy.ix_(local_row + 1, column_index + 1)]
            ry = row_fraction[:, None]
            rx = column_fraction[None, :]
            values = (1.0 - ry) * ((1.0 - rx) * v00 + rx * v01) + ry * (
                (1.0 - rx) * v10 + rx * v11
            )
            support_valid = (
                (v00 != overlay_nodata)
                & (v01 != overlay_nodata)
                & (v10 != overlay_nodata)
                & (v11 != overlay_nodata)
            )
            valid = (
                support_valid
                & row_inside[:, None]
                & column_inside[None, :]
            )
            return values.astype(numpy.float32), valid

        def _box_blur_mask(mask_float, radius):
            """Zero-padded separable box blur (both axes, same radius)."""
            window_length = 2 * radius + 1
            for axis in (0, 1):
                padded = numpy.zeros(
                    (
                        mask_float.shape[0]
                        + (window_length - 1) * (axis == 0),
                        mask_float.shape[1]
                        + (window_length - 1) * (axis == 1),
                    ),
                    dtype=numpy.float32,
                )
                offset = radius
                if axis == 0:
                    padded[offset : offset + mask_float.shape[0], :] = (
                        mask_float
                    )
                    summed = numpy.cumsum(padded, axis=0)
                    mask_float = (
                        summed[window_length - 1 :, :]
                        - numpy.vstack(
                            (
                                numpy.zeros(
                                    (1, padded.shape[1]), dtype=numpy.float32
                                ),
                                summed[: -window_length, :],
                            )
                        )
                    ) / window_length
                else:
                    padded[:, offset : offset + mask_float.shape[1]] = (
                        mask_float
                    )
                    summed = numpy.cumsum(padded, axis=1)
                    mask_float = (
                        summed[:, window_length - 1 :]
                        - numpy.hstack(
                            (
                                numpy.zeros(
                                    (padded.shape[0], 1), dtype=numpy.float32
                                ),
                                summed[:, : -window_length],
                            )
                        )
                    ) / window_length
            return mask_float

        for strip_start in range(0, grid_rows, strip_rows):
            strip_stop = min(strip_start + strip_rows, grid_rows)
            halo_start = max(strip_start - feather_cells, 0)
            halo_stop = min(strip_stop + feather_cells, grid_rows)
            extended_rows = numpy.arange(halo_start, halo_stop)
            values_ext, valid_ext = _strip_values_and_valid(extended_rows)
            if not valid_ext.any():
                continue
            # Soft hand-back near no-coverage regions: blur the validity
            # and rescale so cells deep inside data keep weight 1 while
            # the boundary ramps to 0 over ~the feather width.
            data_weight_ext = _box_blur_mask(
                valid_ext.astype(numpy.float32), feather_cells
            )
            data_weight_ext = numpy.clip(
                2.0 * data_weight_ext - 1.0, 0.0, 1.0
            )
            trim_lo = strip_start - halo_start
            trim_hi = trim_lo + (strip_stop - strip_start)
            values = values_ext[trim_lo:trim_hi]
            valid = valid_ext[trim_lo:trim_hi]
            data_weight = data_weight_ext[trim_lo:trim_hi]

            strip_y = grid_y[strip_start:strip_stop]
            edge_south = (strip_y - tile.lat) * metres_per_degree_latitude
            edge_north = (
                tile.lat + 1.0 - strip_y
            ) * metres_per_degree_latitude
            row_edge_m = numpy.minimum(edge_south, edge_north)
            edge_m = numpy.minimum(row_edge_m[:, None], column_edge_m[None, :])
            if feather_m > 0:
                edge_weight = numpy.clip(edge_m / feather_m, 0.0, 1.0)
            else:
                edge_weight = (edge_m >= 0).astype(numpy.float32)

            weight = edge_weight.astype(numpy.float32) * data_weight
            weight[~valid] = 0.0
            if not (weight > 0).any():
                continue

            window = base_dem.alt_dem[strip_start:strip_stop]
            base_nodata = window == base_dem.nodata
            ring = (weight > 0) & (weight < 1) & valid & ~base_nodata
            if ring.any() and len(ring_samples) < 40:
                differences = (values[ring] - window[ring]).ravel()
                ring_samples.append(differences[:10000])
            blended = weight * values + (1.0 - weight) * window
            if base_nodata.any():
                blended = numpy.where(
                    base_nodata,
                    numpy.where(valid, values, window),
                    blended,
                )
            base_dem.alt_dem[strip_start:strip_stop] = blended.astype(
                base_dem.alt_dem.dtype
            )
            blended_any = True
    finally:
        dataset = None

    if ring_samples:
        offset = float(numpy.median(numpy.concatenate(ring_samples)))
        # A few metres is the normal surface-vs-bare-earth gap along the
        # tile-edge feather band; only datum-class magnitudes are
        # actionable (see INSETS.INSET_DATUM_WARNING_THRESHOLD_M).
        if abs(offset) > INSETS.INSET_DATUM_WARNING_THRESHOLD_M:
            UI.vprint(
                1,
                "   WARNING: tile elevation overlay",
                os.path.basename(overlay_path),
                "differs from the base DEM by a median",
                round(offset, 2),
                "m over the feather band (>%d m; check vertical datum)."
                % int(INSETS.INSET_DATUM_WARNING_THRESHOLD_M),
            )
    if blended_any:
        base_dem.tile_overlay_provenance = {
            "provider": plan["definition"]["code"],
            "path": overlay_path,
            "target_resolution_m": plan["target_resolution_m"],
        }
        UI.vprint(
            1,
            "   Tile elevation overlay baked:",
            plan["definition"]["code"],
            "at",
            plan["target_resolution_m"],
            "m.",
        )
    return blended_any
