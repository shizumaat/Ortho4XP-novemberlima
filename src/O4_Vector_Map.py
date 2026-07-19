import os
import time
import threading
from math import pi, sin, cos, sqrt, atan, exp
import numpy
from shapely import geometry, ops
from shapely.prepared import prep

# from PIL import Image, ImageDraw, ImageFilter
import O4_DEM_Utils as DEM
import O4_UI_Utils as UI
import O4_OSM_Utils as OSM
import O4_Vector_Utils as VECT
import O4_File_Names as FNAMES
import O4_Geo_Utils as GEO
import O4_Airport_Utils as APT
import O4_Airport_Elevation_Insets as INSETS
import O4_Elevation_Level as ELEVATION_LEVEL
from auto_patch import driver as AUTOPATCH
from auto_patch import osm_aeroway as OSMAERO
import O4_Config_Utils as CFG

good_imagery_list = ()

################################################################################
# OSM layer download specifications, shared between the include_* encoders
# below and the background prefetch: a single source for each layer's
# Overpass statements and tags, so the cache a prefetch writes is exactly
# the cache the encoder would have written itself.
################################################################################
BIG_ROADS_QUERIES = [
    'way["highway"="motorway"]',
    'way["highway"="trunk"]',
    'way["highway"="primary"]',
    'way["highway"="secondary"]',
    'way["railway"="rail"]',
    'way["railway"="narrow_gauge"]',
]
# ``width`` / ``lanes`` size the auto-patch tunnel ramps from the mapped
# carriageway instead of the per-type width table (user 2026-07-16,
# EGPB: the table's 18 m ``primary`` default tripled the A970's width).
ROADS_TAGS_OF_INTEREST = ["bridge", "tunnel", "width", "lanes"]
# Node tags retained on the road ways' child nodes: at-grade
# level-crossing evidence for the implied-crossing-tunnel veto (user
# 2026-07-16, EGPB/Gibraltar — the world's few public roads that cross
# an active runway at grade are mapped with ``aeroway=aircraft_crossing``
# and barrier gates, and must NOT get a synthetic tunnel).
ROAD_NODE_TAGS_OF_INTEREST = ["aeroway", "crossing:aircraft", "barrier"]
# Tag-schema version stamped into the road layer caches.  Bump it when
# the retained-tag whitelists above grow: caches written under an older
# schema are re-downloaded once instead of silently recycled without
# the new tags.
ROAD_CACHE_TAG_SCHEMA = "2026-07-16"
COASTLINE_QUERIES = ['way["natural"="coastline"]']
AIRPORTS_QUERIES = [('node["aeroway"]', 'way["aeroway"]', 'rel["aeroway"]')]
WATER_QUERIES = [
    'rel["natural"="water"]',
    'rel["waterway"="riverbank"]',
    'way["natural"="water"]',
    'way["waterway"="riverbank"]',
    'way["waterway"="dock"]',
]
# "tidal" and "water" feed the SEA_EQUIV routing of tidal ponds and
# lagoons (see water_polygon_is_tidal); bump WATER_CACHE_TAG_SCHEMA
# when this list grows so pre-existing caches re-download with the
# new tags.
WATER_TAGS_OF_INTEREST = ["name", "tidal", "water"]
WATER_CACHE_TAG_SCHEMA = "2026-07-17"


def water_polygon_is_tidal(osmid, dicosmtags):
    """Whether an OpenStreetMap water polygon belongs to the tidal regime.

    True for polygons tagged ``tidal=yes`` (the Ria Formosa salinas and
    esteros) or mapped as coastal lagoons (``water=lagoon`` — the Ria
    Formosa itself is one such relation).  These areas keep the INLAND
    water treatment (orthophoto at the constant ``ratio_water``
    transparency with X-Plane water on top) even where the coastline
    polygon claims them as open sea: :func:`include_sea` refuses to
    seed the SEA attribute inside them, so the deep-water fade begins
    at the true coast, never inside a mapped lagoon.  (Routing these to
    SEA_EQUIV instead was tried on 2026-07-17 and reverted: the sea's
    depth-graded masks read the intertidal lidar as exposed flats and
    printed permanently wet water — marinas, channels — as opaque dark
    imagery with razor polygon-edge seams.)
    """
    tags = dicosmtags.get(osmid, {})
    return tags.get("tidal") == "yes" or tags.get("water") == "lagoon"


def sea_seed_areas(sea_area, tidal_water_area):
    """Where the SEA attribute may be seeded: the sea minus tidal water.

    The coastline's contiguous sea polygon often reaches through inlets
    INTO a mapped lagoon, and its representative point (the flood seed)
    can land there — classifying the whole lagoon as deep sea.  Tidal
    water polygons' boundaries are already encoded mesh constraints, so
    withholding seeds from their interiors is sufficient: the flood
    stops at their rings and the lagoon's own WATER seeds win.  Slivers
    between the coastline and a lagoon ring keep their own seeds (they
    are genuinely sea).  Any geometry failure falls back to the
    undiminished sea area — a mis-seeded lagoon must never cost the
    whole coastline.
    """
    if tidal_water_area is None or tidal_water_area.is_empty:
        return sea_area
    try:
        remainder = VECT.ensure_MultiPolygon(
            sea_area.difference(tidal_water_area)
        )
        if remainder.is_empty:
            return remainder
        return remainder
    except Exception:
        return sea_area


def small_roads_queries(road_level):
    queries = ['way["highway"="tertiary"]']
    if road_level >= 3:
        queries += [
            'way["highway"="unclassified"]',
            'way["highway"="residential"]',
        ]
    if road_level >= 4:
        queries += ['way["highway"="service"]']
    if road_level >= 5:
        queries += ['way["highway"="track"]']
    return queries


def _osm_layer_prefetch_specifications(tile):
    """Every OSM layer this tile build will download besides the airports
    layer, in download order, honouring the same conditions the encoders
    apply (road_level gates; custom coastline/water data replaces the
    Overpass download entirely)."""
    specifications = []
    if tile.road_level:
        specifications.append(
            ("big_roads", BIG_ROADS_QUERIES, ROADS_TAGS_OF_INTEREST,
             ROAD_NODE_TAGS_OF_INTEREST, ROAD_CACHE_TAG_SCHEMA))
    if tile.road_level >= 2:
        specifications.append(
            ("small_roads", small_roads_queries(tile.road_level),
             ROADS_TAGS_OF_INTEREST,
             ROAD_NODE_TAGS_OF_INTEREST, ROAD_CACHE_TAG_SCHEMA))
    if not (os.path.isfile(FNAMES.custom_coastline(tile.lat, tile.lon))
            or os.path.isdir(FNAMES.custom_coastline_dir(tile.lat,
                                                         tile.lon))):
        specifications.append(("coastline", COASTLINE_QUERIES, [], [], ""))
    if not (os.path.isfile(FNAMES.custom_water(tile.lat, tile.lon))
            or os.path.isdir(FNAMES.custom_water_dir(tile.lat, tile.lon))):
        specifications.append(
            ("water", WATER_QUERIES, WATER_TAGS_OF_INTEREST, [],
             WATER_CACHE_TAG_SCHEMA))
    return specifications


def osm_layer_warm_specifications(tile):
    """Every OSM layer a build of this tile downloads, airports first.

    Consumed by the parallel-build OSM warmer
    (docs/specs/parallel-tile-builds.md §3.7), which pre-downloads queued
    tiles' caches one Overpass request at a time while earlier tiles
    compute.  Same 5-tuple shape as the prefetch specifications:
    ``(cached_suffix, queries, tags_of_interest, node_tags_of_interest,
    cache_schema)``.
    """
    return [
        ("airports", AIRPORTS_QUERIES, ["all"], [], "")
    ] + _osm_layer_prefetch_specifications(tile)


_osm_prefetch_thread = None


def start_background_osm_prefetch(tile):
    """Download this tile's remaining OSM layer caches in the background.

    Started as soon as the airports layer (the only data the next
    pipeline stages need immediately) has arrived: the road, coastline
    and water layers then download WHILE airport processing and the
    auto-patch builds compute, instead of after them.  Downloads run
    sequentially — one Overpass request at a time — so the prefetch is
    no harder on the servers than the old inline order, just earlier.
    Consumers call wait_for_background_osm_prefetch() and then read the
    cache exactly as before.
    """
    global _osm_prefetch_thread
    wait_for_background_osm_prefetch()  # never two prefetches at once
    specifications = [
        specification
        for specification in _osm_layer_prefetch_specifications(tile)
        if not (os.path.isfile(
                    FNAMES.osm_cached(tile.lat, tile.lon, specification[0]))
                and OSM._cached_osm_schema_matches(
                    FNAMES.osm_cached(tile.lat, tile.lon, specification[0]),
                    specification[4]))
    ]
    if not specifications:
        return
    UI.vprint(
        1,
        "    * Prefetching OSM data in the background:",
        ", ".join(specification[0] for specification in specifications),
    )

    def download_missing_layer_caches():
        for (cached_suffix, queries, tags_of_interest,
                node_tags_of_interest, cache_schema) in specifications:
            if UI.red_flag:
                return
            # The layer object is discarded: the point is the cache file
            # OSM_queries_to_OSM_layer writes, which the encoder later
            # recycles.
            OSM.OSM_queries_to_OSM_layer(
                queries,
                OSM.OSM_layer(),
                tile.lat,
                tile.lon,
                tags_of_interest,
                cached_suffix=cached_suffix,
                node_tags_of_interest=node_tags_of_interest,
                cache_schema=cache_schema,
            )

    _osm_prefetch_thread = threading.Thread(
        target=download_missing_layer_caches, daemon=True)
    _osm_prefetch_thread.start()


def wait_for_background_osm_prefetch():
    """Block until the background OSM prefetch (if any) has finished.
    Callers that read a layer cache MUST call this first, so they never
    race the prefetch on the same cache file."""
    global _osm_prefetch_thread
    if _osm_prefetch_thread is not None:
        _osm_prefetch_thread.join()
        _osm_prefetch_thread = None


################################################################################
def build_poly_file(tile):
    if UI.is_working:
        return 0
    UI.is_working = 1
    UI.red_flag = 0
    # in case that was forgotten by the user
    tile.iterate = 0
    # update the lat/lon scaling factor in VECT
    VECT.scalx = cos((tile.lat + 0.5) * pi / 180)
    # Let's go !
    UI.logprint(
        "Step 1 for tile lat=", tile.lat, ", lon=", tile.lon, ": starting."
    )
    UI.vprint(
        0,
        "\nStep 1 : Building vector data for tile "
        + FNAMES.short_latlon(tile.lat, tile.lon)
        + " : \n--------\n",
    )
    timer = time.time()

    if not os.path.exists(tile.build_dir):
        os.makedirs(tile.build_dir)
    if not os.path.exists(FNAMES.osm_dir(tile.lat, tile.lon)):
        os.makedirs(FNAMES.osm_dir(tile.lat, tile.lon))

    # Start the coastal bathymetry band fetch in the background when this
    # tile will want it: the network fetch then overlaps the vector and
    # mesh steps instead of serializing in front of the masks step
    # (docs/specs/coastal-bathymetry-spec.md section 3).  A no-op on
    # non-coastal tiles and when the mask settings do not call for it.
    import O4_Bathymetry_Band as BATHYBAND

    BATHYBAND.prefetch_bathymetry_band(tile)

    node_file = FNAMES.input_node_file(tile)
    poly_file = FNAMES.input_poly_file(tile)
    vector_map = VECT.Vector_Map()

    if UI.red_flag:
        UI.exit_message_and_bottom_line()
        return 0

    # Airports
    (apt_array, apt_area) = include_airports(vector_map, tile)
    UI.vprint(
        1, "   Number of edges at this point:", len(vector_map.dico_edges)
    )

    if UI.red_flag:
        UI.exit_message_and_bottom_line()
        return 0

    # Roads
    include_roads(vector_map, tile, apt_array, apt_area)
    if tile.road_level:
        UI.vprint(
            1, "   Number of edges at this point:", len(vector_map.dico_edges)
        )

    if UI.red_flag:
        UI.exit_message_and_bottom_line()
        return 0

    # Sea
    include_sea(vector_map, tile)
    UI.vprint(
        1, "   Number of edges at this point:", len(vector_map.dico_edges)
    )

    if UI.red_flag:
        UI.exit_message_and_bottom_line()
        return 0

    # Water
    include_water(vector_map, tile)
    UI.vprint(
        1, "   Number of edges at this point:", len(vector_map.dico_edges)
    )

    if UI.red_flag:
        UI.exit_message_and_bottom_line()
        return 0

    # Buildings
    # include_buildings(vector_map)
    # if UI.red_flag: UI.exit_message_and_bottom_line(); return 0

    # Orthogrid
    UI.vprint(0, "-> Inserting edges related to the orthophotos grid")
    xgrid = set()  # x coordinates of vertical grid lines
    ygrid = set()  # y coordinates of horizontal grid lines
    (til_xul, til_yul) = GEO.wgs84_to_orthogrid(
        tile.lat + 1, tile.lon, tile.mesh_zl
    )
    (til_xlr, til_ylr) = GEO.wgs84_to_orthogrid(
        tile.lat, tile.lon + 1, tile.mesh_zl
    )
    for til_x in range(til_xul + 16, til_xlr + 1, 16):
        pos_x = til_x / (2 ** (tile.mesh_zl - 1)) - 1
        xgrid.add(pos_x * 180 - tile.lon)
        #print("x", pos_x * 180 - tile.lon)
    for til_y in range(til_yul + 16, til_ylr + 1, 16):
        pos_y = 1 - (til_y) / (2 ** (tile.mesh_zl - 1))
        ygrid.add(360 / pi * atan(exp(pi * pos_y)) - 90 - tile.lat)
        #print("y", (360 / pi * atan(exp(pi * pos_y)) - 90 - tile.lat))

    xgrid.add(0)
    xgrid.add(1)
    ygrid.add(0)
    ygrid.add(1)
    xgrid = list(sorted(xgrid))
    ygrid = list(sorted(ygrid))
    eps = 2 ** -5
    ortho_network = geometry.MultiLineString(
        [geometry.LineString([(x, 0.0 - eps), (x, 1.0 + eps)]) for x in xgrid]
        + [geometry.LineString([(0.0 - eps, y), (1.0 + eps, y)]) for y in ygrid]
    )
    vector_map.encode_MultiLineString(
        ortho_network, tile.dem.alt_vec, "DUMMY", check=True, skip_cut=True
    )

    if UI.red_flag:
        UI.exit_message_and_bottom_line()
        return 0

    # Gluing edges
    UI.vprint(0, "-> Inserting additional boundary edges for gluing")
    segs = 2048
    gluing_network = geometry.MultiLineString(
        [
            geometry.LineString(
                [(x, 0) for x in numpy.arange(0, segs + 1) / segs]
            ),
            geometry.LineString(
                [(x, 1) for x in numpy.arange(0, segs + 1) / segs]
            ),
            geometry.LineString(
                [(0, y) for y in numpy.arange(0, segs + 1) / segs]
            ),
            geometry.LineString(
                [(1, y) for y in numpy.arange(0, segs + 1) / segs]
            ),
        ]
    )
    vector_map.encode_MultiLineString(
        gluing_network, tile.dem.alt_vec, "DUMMY", check=True, skip_cut=True
    )

    if UI.red_flag:
        UI.exit_message_and_bottom_line()
        return 0
    UI.vprint(0, "-> Transcription to the files ", poly_file, "and .node")
    if not vector_map.seeds:
        if tile.dem.alt_dem.max() >= 1:
            vector_map.seeds["SEA"] = [numpy.array([1000, 1000])]
        else:
            vector_map.seeds["SEA"] = [numpy.array([0.5, 0.5])]
    vector_map.snap_to_grid(9) 
    vector_map.write_node_file(node_file)
    vector_map.write_poly_file(poly_file)

    UI.vprint(
        1, "\nFinal number of constrained edges :", len(vector_map.dico_edges)
    )
    UI.timings_and_bottom_line(timer)
    UI.logprint(
        "Step 1 for tile lat=", tile.lat, ", lon=", tile.lon, ": normal exit."
    )
    return 1


################################################################################
def load_airports_and_prepare_dem(tile):
    """Airport OSM layer + ``dico_airports`` + the PRODUCTION tile DEM.

    This is the exact prelude the tile build runs before auto-patch
    generation: airports layer load, airport dictionaries, elevation
    insets, tile-wide overlay, DEM construction, inset densification,
    overlay bake, and airport smoothing — ``tile.dem`` afterwards IS the
    DEM every production ``build_airport_pavement`` call receives via
    ``tile_dem``.  Factored out (2026-07-18, user: standalone probes
    must test with the production DEM) so
    ``tools/production_airport_patch.py`` can run single-airport builds
    against the identical surface.  Returns ``(airport_layer,
    dico_airports)``, or ``(None, None)`` when the airports layer
    cannot be loaded."""
    airport_layer = OSM.OSM_layer()
    queries = AIRPORTS_QUERIES
    tags_of_interest = ["all"]
    if not OSM.OSM_queries_to_OSM_layer(
        queries,
        airport_layer,
        tile.lat,
        tile.lon,
        tags_of_interest,
        cached_suffix="airports",
    ):
        return (None, None)
    # The airports layer was the only download the next stages need
    # right away — fetch every other layer this build will read in the
    # background while airport processing / auto-patch builds compute.
    # (The auto-patch road-aware grading reads the big_roads cache at
    # build time, so without this the roads would arrive too late on a
    # freshly built tile.)
    start_background_osm_prefetch(tile)
    dico_airports = build_airports_dico(tile, airport_layer)
    APT.list_airports_and_runways(dico_airports)
    UI.vprint(1, "   Loading elevation data and smoothing it over airports.")
    # Airport elevation insets (spec section 3.3): fetch meter-class public
    # elevation for each airport neighbourhood, then augment the DEM source
    # in memory with the cached insets (base;inset1;inset2). No-op -- and a
    # byte-identical build -- when the feature is gated off, no provider
    # covers the tile, or GDAL is unavailable. The user's custom_dem config
    # value is never rewritten.
    INSETS.ensure_insets_for_tile(tile, dico_airports)
    # Tile-wide elevation detail level (docs/specs/elevation-level-spec.md):
    # fetch the whole-tile overlay for a numeric elevation_level, or the
    # coastline lidar band for "coastline" (dico_airports feeds its
    # approach-visibility ladder). No-op -- and a byte-identical build --
    # on the default "auto".
    ELEVATION_LEVEL.ensure_tile_overlay(tile, dico_airports)
    compose_tile_dem_from_disk(tile, dico_airports)
    return (airport_layer, dico_airports)


################################################################################
def build_airports_dico(tile, airport_layer):
    """The tile build's airport-dictionary chain, exactly as
    ``load_airports_and_prepare_dem`` runs it (discovery, surfaces,
    runway reconstruction, discards, hangar/apron/taxiway areas,
    boundaries).  Factored out (2026-07-19, production-DEM parity v2)
    so the standalone DEM loader in ``auto_patch.elevation`` can build
    the same ``dico_airports`` the production airport smoothing uses.
    Pure compute over the already-loaded layer — no network."""
    dico_airports = {}
    APT.discover_airport_names(airport_layer, dico_airports)
    APT.attach_surfaces_to_airports(airport_layer, dico_airports)
    APT.sort_and_reconstruct_runways(tile, airport_layer, dico_airports)
    APT.discard_unwanted_airports(tile, dico_airports)
    APT.build_hangar_areas(tile, airport_layer, dico_airports)
    APT.build_apron_areas(tile, airport_layer, dico_airports)
    APT.build_taxiway_areas(tile, airport_layer, dico_airports)
    APT.update_airport_boundaries(tile, dico_airports)
    return dico_airports


################################################################################
def compose_tile_dem_from_disk(tile, dico_airports, write_alt_file=True):
    """DEM construction from CACHED disk state: composite assembly,
    densification, tile-overlay bake, airport smoothing + inset bake.

    This is the tail of the production DEM prelude, after the two
    network ``ensure_*`` fetch steps — everything here reads only what
    is already on disk.  Factored out (2026-07-19, owner ruling: "the
    tests have to use the same DEM as production or they're useless")
    so the standalone loader ``auto_patch.elevation._load_airport_dem``
    runs the IDENTICAL code over the cached state instead of a
    replication.  ``write_alt_file=False`` keeps the result in memory
    (tests/probes must not write tile build state).  Sets ``tile.dem``
    and returns it."""
    dem_source = INSETS.assemble_inset_composite_source(tile, tile.custom_dem)
    tile.dem = DEM.DEM(
        tile.lat,
        tile.lon,
        dem_source,
        tile.fill_nodata or "to zero",
        info_only=False,
    )
    # Densify the working grid over inset tiles (spec Phase C1) BEFORE
    # smoothing and baking, so the finer posting carries the meter-class
    # airport relief through to the mesh. No-op (byte-identical) when no
    # inset covers the tile or the feature is gated off.
    INSETS.densify_tile_dem_for_insets(tile)
    # The tile-wide overlay is base terrain: bake it BEFORE the airport
    # smoothing pass (airport insets keep baking last, after smoothing).
    ELEVATION_LEVEL.bake_tile_overlay_into_alt_dem(tile)
    APT.smooth_raster_over_airports(
        tile, dico_airports, write_alt_file=write_alt_file
    )
    return tile.dem


################################################################################
def run_auto_patch_generation(tile, airport_layer, dico_airports):
    """The tile build's auto-patch generation call, exactly as
    ``include_airports`` runs it (mode resolution, CIFP discovery, lazy
    taxiway/building/road providers).  ``tile.dem`` must already be the
    production DEM (:func:`load_airports_and_prepare_dem`).  Factored
    out with it (2026-07-18) for the single-airport production lab loop
    in ``tools/production_airport_patch.py``."""
    # Auto-generate runway, taxiway, and building patches from CIFP data +
    # OSM geometry (before loading patches so include_patches() picks them up)
    # Backward compat: legacy bool True/False configs map to "All"/"None"
    auto_patch_mode = tile.auto_patch
    if auto_patch_mode is True:
        auto_patch_mode = "All"
    elif auto_patch_mode is False:
        auto_patch_mode = "None"
    if auto_patch_mode != "None":
        cifp_path = CFG.cifp_data_path
        if not cifp_path and CFG.custom_scenery_dir:
            # Try X-Plane's default CIFP location relative to Custom Scenery
            xplane_root = os.path.dirname(
                os.path.normpath(CFG.custom_scenery_dir)
            )
            candidate = os.path.join(xplane_root, "Custom Data", "CIFP")
            if os.path.isdir(candidate):
                cifp_path = candidate
        if cifp_path:
            # The taxiway/building/road extraction below is passed as
            # zero-arg callables: generate_auto_patches invokes them
            # only when at least one airport actually needs a rebuild.
            # A tile whose auto-patches are all up to date (apt.dat
            # unchanged) skips the parsing — and its log output —
            # entirely.

            # Taxiway centerlines from OSM data for patch generation.
            def _taxiway_provider():
                return OSMAERO.extract_taxiway_info(
                    airport_layer, dico_airports, tile
                )

            # Building data: rely solely on aeroway=hangar and
            # aeroway=terminal features that are ALREADY in the
            # per-tile airport_layer cache.  Per user 2026-04-27:
            # the previous per-airport ``way["building"]`` Overpass
            # queries (one per airport, with a 1 km buffer) caused
            # rate-limit cascades and partial failures on tiles
            # with many small airports (e.g. 25+ airports in the
            # Charlotte tile), and the resulting building cache
            # often failed to write entirely.  General building
            # footprints (control towers, fire stations, fuel
            # depots, etc.) catch only edge cases — terminals and
            # hangars dominate the apron-paint cut-outs.  Skipping
            # the extra query trades minor coverage for speed,
            # robustness, and zero rate-limit risk.
            def _building_provider():
                return OSMAERO.extract_building_info(
                    airport_layer, dico_airports, tile,
                    building_layer=None,
                )

            # Cached big roads for tunnel/road-aware terrain modeling.
            def _road_provider():
                # The background prefetch may still be downloading the
                # roads — wait for it, so a freshly built tile gets its
                # road data instead of silently building without it.
                wait_for_background_osm_prefetch()
                cached_roads = FNAMES.osm_cached(
                    tile.lat, tile.lon, "big_roads"
                )
                if not os.path.isfile(cached_roads):
                    return None
                road_osm_layer = OSM.OSM_layer()
                road_osm_layer.update_dicosm(
                    cached_roads,
                    {"n": [], "w": [("highway", ""), ("tunnel", ""),
                                    ("bridge", "")], "r": []},
                    {"n": [], "w": [("highway", ""), ("tunnel", ""),
                                    ("bridge", "")], "r": []},
                )
                return OSMAERO.extract_road_info(
                    dico_airports, tile, road_layer=road_osm_layer)

            AUTOPATCH.generate_auto_patches(
                tile, cifp_path,
                taxiway_data=_taxiway_provider,
                building_data=_building_provider,
                dico_airports=dico_airports,
                road_data=_road_provider,
                mode=auto_patch_mode,
            )


################################################################################
def include_airports(vector_map, tile):
    UI.vprint(0, "-> Dealing with airports")
    (airport_layer, dico_airports) = load_airports_and_prepare_dem(tile)
    if airport_layer is None:
        return (0, 0)
    run_auto_patch_generation(tile, airport_layer, dico_airports)
    (patches_area, patches_list) = include_patches(vector_map, tile)
    runway_taxiway_apron_area = APT.encode_runways_taxiways_and_aprons(
        tile, airport_layer, dico_airports, vector_map, patches_list,
        patches_area=patches_area,
    )
    treated_area = ops.unary_union([patches_area, runway_taxiway_apron_area])
    APT.encode_hangars(tile, dico_airports, vector_map, patches_list,
                       patches_area=patches_area)
    APT.flatten_helipads(airport_layer, vector_map, tile, treated_area)
    # APT.encode_aprons(tile,dico_airports,vector_map)
    apt_array = APT.build_airport_array(tile, dico_airports)
    return (apt_array, treated_area)


################################################################################
def include_roads(vector_map, tile, apt_array, apt_area):
    def road_is_too_much_banked(way, filtered_segs):
        (col, row) = numpy.minimum(
            numpy.maximum(numpy.round(way[0] * 1000), 0), 1000
        )
        if apt_array[int(1000 - row), int(col)]:
            return True
        (col, row) = numpy.minimum(
            numpy.maximum(numpy.round(way[-1] * 1000), 0), 1000
        )
        if apt_array[int(1000 - row), int(col)]:
            return True
        if filtered_segs >= tile.max_levelled_segs:
            return False
        return (
            numpy.abs(
                tile.dem.alt_vec(way)
                - tile.dem.alt_vec(VECT.shift_way(way, tile.lane_width))
            )
            >= tile.road_banking_limit
        ).any()

    def alt_vec_shift(way):
        return tile.dem.alt_vec(VECT.shift_way(way, tile.lane_width))

    if not tile.road_level:
        return
    UI.vprint(0, "-> Dealing with roads")
    wait_for_background_osm_prefetch()
    tags_of_interest = ROADS_TAGS_OF_INTEREST
    # Need to evaluate if including bridges is better or worse
    tags_for_exclusion = set(["bridge", "tunnel"])
    # tags_for_exclusion=set(["tunnel"])
    road_layer = OSM.OSM_layer()
    if not OSM.OSM_queries_to_OSM_layer(
        BIG_ROADS_QUERIES,
        road_layer,
        tile.lat,
        tile.lon,
        tags_of_interest,
        cached_suffix="big_roads",
        node_tags_of_interest=ROAD_NODE_TAGS_OF_INTEREST,
        cache_schema=ROAD_CACHE_TAG_SCHEMA,
    ):
        return 0
    UI.vprint(1, "    * Checking which large roads need leveling.")
    (road_network_banked, road_network_flat) = OSM.OSM_to_MultiLineString(
        road_layer,
        tile.lat,
        tile.lon,
        tags_for_exclusion,
        road_is_too_much_banked,
    )
    if UI.red_flag:
        return 0
    if tile.road_level >= 2:
        road_layer = OSM.OSM_layer()
        if not OSM.OSM_queries_to_OSM_layer(
            small_roads_queries(tile.road_level),
            road_layer,
            tile.lat,
            tile.lon,
            tags_of_interest,
            cached_suffix="small_roads",
            node_tags_of_interest=ROAD_NODE_TAGS_OF_INTEREST,
            cache_schema=ROAD_CACHE_TAG_SCHEMA,
        ):
            return 0
        UI.vprint(1, "    * Checking which smaller roads need leveling.")
        timer = time.time()
        (
            road_network_banked_2,
            road_network_flat_2,
        ) = OSM.OSM_to_MultiLineString(
            road_layer,
            tile.lat,
            tile.lon,
            tags_for_exclusion,
            road_is_too_much_banked,
        )
        UI.vprint(3, "Time for check :", time.time() - timer)
        road_network_banked = geometry.MultiLineString(
            list(road_network_banked.geoms) + list(road_network_banked_2.geoms)
        )
    if not road_network_banked.is_empty:
        UI.vprint(1, "    * Buffering banked road network as multipolygon.")
        timer = time.time()
        road_area = VECT.improved_buffer(
            road_network_banked.difference(
                VECT.improved_buffer(apt_area, tile.lane_width + 2, 0, 0)
            ),
            tile.lane_width,
            2,
            0.5,
            show_progress=True,
        )
        UI.vprint(3, "Time for improved buffering:", time.time() - timer)
        if UI.red_flag:
            return 0
        UI.vprint(1, "      Encoding it.")
        vector_map.encode_MultiPolygon(
            road_area, alt_vec_shift, "INTERP_ALT", check=True, refine=100
        )
        if UI.red_flag:
            return 0
    # Hack (23/02/2024 : seems better without actually, keep it just in case)
    if False and not road_network_flat.is_empty:
        road_network_flat = road_network_flat.difference(road_network_banked)
        road_network_flat = road_network_flat.difference(
            VECT.improved_buffer(apt_area, 15, 0, 0)
        ).simplify(0.00001)
        UI.vprint(
            1,
            "    * Encoding the remaining primary road network as linestrings.",
        )
        vector_map.encode_MultiLineString(
            road_network_flat, tile.dem.alt_vec, "DUMMY", check=True
        )
    return 1


################################################################################
def _tidal_water_area(tile):
    """Union of the tile's tidal / lagoon water polygons (tile-relative).

    Loads the same water layer the water encoder uses — the cached
    Overpass download (a cache hit after the prefetch) or the user's
    custom water files — and polygonizes just the polygons matching
    :func:`water_polygon_is_tidal`.  Returns an empty MultiPolygon on
    any failure: the sea-seed subtraction is an override, never a
    dependency the coastline step can fail on.
    """
    try:
        water_layer = OSM.OSM_layer()
        custom_water = FNAMES.custom_water(tile.lat, tile.lon)
        custom_water_dir = FNAMES.custom_water_dir(tile.lat, tile.lon)
        if os.path.isfile(custom_water):
            water_layer.update_dicosm(
                custom_water, input_tags=None, target_tags=None
            )
        elif os.path.isdir(custom_water_dir):
            for osm_file in os.listdir(custom_water_dir):
                water_layer.update_dicosm(
                    os.path.join(custom_water_dir, osm_file),
                    input_tags=None,
                    target_tags=None,
                )
        elif not OSM.OSM_queries_to_OSM_layer(
            WATER_QUERIES,
            water_layer,
            tile.lat,
            tile.lon,
            WATER_TAGS_OF_INTEREST,
            cached_suffix="water",
            cache_schema=WATER_CACHE_TAG_SCHEMA,
        ):
            return geometry.MultiPolygon()
        (_area, tidal_area) = OSM.OSM_to_MultiPolygon(
            water_layer,
            tile.lat,
            tile.lon,
            lambda pol, osmid, dicosmtags: water_polygon_is_tidal(
                osmid, dicosmtags
            ),
        )
        return tidal_area
    except Exception:
        return geometry.MultiPolygon()


def include_sea(vector_map, tile):
    UI.vprint(0, "-> Dealing with coastline")
    wait_for_background_osm_prefetch()
    sea_layer = OSM.OSM_layer()
    custom_source = False
    custom_coastline = FNAMES.custom_coastline(tile.lat, tile.lon)
    custom_coastline_dir = FNAMES.custom_coastline_dir(tile.lat, tile.lon)
    if os.path.isfile(custom_coastline):
        UI.vprint(1, "    * User defined custom coastline data detected.")
        sea_layer.update_dicosm(
            custom_coastline, input_tags=None, target_tags=None
        )
        custom_source = True
    elif os.path.isdir(custom_coastline_dir):
        UI.vprint(
            1,
            "    * User defined custom coastline data detected ",
            "(multiple files).",
        )
        for osm_file in os.listdir(custom_coastline_dir):
            UI.vprint(2, "      ", osm_file)
            sea_layer.update_dicosm(
                os.path.join(custom_coastline_dir, osm_file),
                input_tags=None,
                target_tags=None,
            )
            sea_layer.write_to_file(custom_coastline)
        custom_source = True
    else:
        if not OSM.OSM_queries_to_OSM_layer(
            COASTLINE_QUERIES,
            sea_layer,
            tile.lat,
            tile.lon,
            [],
            cached_suffix="coastline",
        ):
            return 0
    coastline = OSM.OSM_to_MultiLineString(sea_layer, tile.lat, tile.lon)
    if not coastline.is_empty:
        # 1) encoding the coastline
        UI.vprint(1, "    * Encoding coastline.")
        vector_map.encode_MultiLineString(
            VECT.cut_to_tile(coastline, strictly_inside=True),
            tile.dem.alt_vec,
            "SEA",
            check=True,
            refine=False,
        )
        UI.vprint(3, "...done.")
        # 2) finding seeds (transform multilinestring coastline to polygon
        # coastline linemerge being expensive we first set aside what is
        # already known to be closed loops
        UI.vprint(1, "    * Reconstructing its topology.")
        loops = geometry.MultiLineString(
            [line for line in coastline.geoms if line.is_ring]
        )
        remainder = VECT.ensure_MultiLineString(
            VECT.cut_to_tile(
                geometry.MultiLineString(
                    [line for line in coastline.geoms if not line.is_ring]
                ),
                strictly_inside=True,
            )
        )
        UI.vprint(3, "Linemerge...")
        if not remainder.is_empty:
            remainder = VECT.ensure_MultiLineString(ops.linemerge(remainder))
        UI.vprint(3, "...done.")
        coastline = geometry.MultiLineString(
            list(remainder.geoms) + list(loops.geoms)
        )
        sea_area = VECT.ensure_MultiPolygon(
            VECT.coastline_to_MultiPolygon(
                coastline, tile.lat, tile.lon, custom_source
            )
        )
        if sea_area.geoms:
            UI.vprint(
                1, "      Found ", len(sea_area.geoms), "contiguous patch(es)."
            )
        tidal_water = _tidal_water_area(tile)
        if not tidal_water.is_empty:
            UI.vprint(
                1,
                "      Tidal / lagoon water polygons override the"
                " coastline: keeping their interiors inland.",
            )
        seed_area = sea_seed_areas(sea_area, tidal_water)
        for polygon in seed_area.geoms:
            seed = numpy.array(polygon.representative_point().coords[0])
            if "SEA" in vector_map.seeds:
                vector_map.seeds["SEA"].append(seed)
            else:
                vector_map.seeds["SEA"] = [seed]


################################################################################
def include_water(vector_map, tile):
    large_lake_threshold = (
        tile.max_area * 1e6 / (GEO.lat_to_m * GEO.lon_to_m(tile.lat + 0.5))
    )

    def filter_large_lakes(pol, osmid, dicosmtags):
        if pol.area < large_lake_threshold:
            return False
        area = int(pol.area * GEO.lat_to_m * GEO.lon_to_m(tile.lat + 0.5) / 1e6)
        if (osmid in dicosmtags) and ("name" in dicosmtags[osmid]):
            if dicosmtags[osmid]["name"] in good_imagery_list:
                UI.vprint(
                    1,
                    "      * ",
                    dicosmtags[osmid]["name"],
                    "kept will complete imagery although it is",
                    area,
                    "km^2.",
                )
                return False
            else:
                UI.vprint(
                    1,
                    "      * ",
                    dicosmtags[osmid]["name"],
                    "will be masked like the sea due to its large area of",
                    area,
                    "km^2.",
                )
                return True
        else:
            pt = (
                pol.exterior.coords[0]
                if "Multi" not in pol.geom_type
                else pol.geoms[0].exterior.coords[0]
            )
            UI.vprint(
                1,
                "      * ",
                "Some large OSM water patch close to lat=",
                "{:.2f}".format(pt[1] + tile.lon),
                "lon=",
                "{:.2f}".format(pt[0] + tile.lat),
                "will be masked due to its large area of",
                area,
                "km^2.",
            )
            return True

    UI.vprint(0, "-> Dealing with inland water")
    wait_for_background_osm_prefetch()
    water_layer = OSM.OSM_layer()
    custom_water = FNAMES.custom_water(tile.lat, tile.lon)
    custom_water_dir = FNAMES.custom_water_dir(tile.lat, tile.lon)
    if os.path.isfile(custom_water):
        UI.vprint(1, "    * User defined custom water data detected.")
        water_layer.update_dicosm(
            custom_water, input_tags=None, target_tags=None
        )
    elif os.path.isdir(custom_water_dir):
        UI.vprint(
            1, "    * User defined custom water data detected (multiple files)."
        )
        for osm_file in os.listdir(custom_water_dir):
            UI.vprint(2, "      ", osm_file)
            water_layer.update_dicosm(
                os.path.join(custom_water_dir, osm_file),
                input_tags=None,
                target_tags=None,
            )
            water_layer.write_to_file(custom_water)
    else:
        if not OSM.OSM_queries_to_OSM_layer(
            WATER_QUERIES,
            water_layer,
            tile.lat,
            tile.lon,
            WATER_TAGS_OF_INTEREST,
            cached_suffix="water",
            cache_schema=WATER_CACHE_TAG_SCHEMA,
        ):
            return 0
    # Airport-inset water supplement (additive, never a replacement):
    # hydro-flat basins detected in the lidar insets — standing water
    # OpenStreetMap does not carry (the KBNA wastewater ponds).  The
    # supplement joins whichever base layer loaded above (custom or
    # Overpass) and flows through the normal WATER seed + smoothing.
    if getattr(tile, "airport_inset_water", True):
        import O4_Airport_Elevation_Insets as INSETS

        if INSETS.insets_enabled_for_tile(tile):
            inset_water_path = INSETS.ensure_inset_water_supplement(
                tile.lat, tile.lon
            )
            if inset_water_path:
                UI.vprint(
                    1,
                    "    * Airport-inset water supplement merged "
                    "(hydro-flat basins from the elevation insets).",
                )
                water_layer.update_dicosm(
                    inset_water_path, input_tags=None, target_tags=None
                )
    UI.vprint(1, "    * Building water multipolygon.")
    (water_area, sea_equiv_area) = OSM.OSM_to_MultiPolygon(
        water_layer, tile.lat, tile.lon, filter_large_lakes
    )
    if not water_area.is_empty:
        UI.vprint(1, "      Cleaning it.")
        try:
            (idx_water, dico_water) = VECT.MultiPolygon_to_Indexed_Polygons(
                water_area, merge_overlappings=tile.clean_bad_geometries
            )
        except:
            return 0
        UI.vprint(
            2, "      Number of water Multipolygons : " + str(len(dico_water))
        )
        UI.vprint(1, "      Encoding it.")
        vector_map.encode_MultiPolygon(
            dico_water,
            tile.dem.alt_vec,
            "WATER",
            area_limit=tile.min_area / 10000,
            simplify=tile.water_simplification * GEO.m_to_lat,
            check=True,
        )
    if not sea_equiv_area.is_empty:
        UI.vprint(
            1, "      Separate treatment for larger pieces requiring masks."
        )
        try:
            (idx_water, dico_water) = VECT.MultiPolygon_to_Indexed_Polygons(
                sea_equiv_area, merge_overlappings=tile.clean_bad_geometries
            )
        except:
            return 0
        UI.vprint(
            2, "      Number of water Multipolygons : " + str(len(dico_water))
        )
        UI.vprint(1, "      Encoding them.")
        vector_map.encode_MultiPolygon(
            dico_water,
            tile.dem.alt_vec,
            "SEA_EQUIV",
            area_limit=tile.min_area / 10000,
            simplify=tile.water_simplification * GEO.m_to_lat,
            check=True,
        )
    return 1


################################################################################
# def include_buildings(vector_map, tile):
#     # should be all revisited
#     UI.vprint(0, "-> Dealing with buildings")
#     building_layer = OSM.OSM_layer()
#     queries = []  #'way["building"="yes"]']
#     tags_of_interest = []
#     if not OSM.OSM_queries_to_OSM_layer(
#         queries,
#         building_layer,
#         tile.lat,
#         tile.lon,
#         tags_of_interest,
#         cached_suffix="buildings",
#     ):
#         return 0
#     for (i, j) in itertools.product(range(1), range(1)):
#         print("    Obtaining part ", 4 * i + j, " of OSM data for " + tag)
#         response = get_overpass_data(
#             tag,
#             (lat + i / 4, lon + j / 4, lat + (i + 1) / 4, lon + (j + 1) / 4),
#             "FR",
#         )
#         if UI.red_flag:
#             return 0
#         if response[0] != "ok":
#             print("    Error while trying to obtain ", query, ", exiting.")
#             return 0
#         building_layer.update_dicosm(response[1], tags_of_interest)
#     building_area = OSM.OSM_to_MultiPolygon(building_layer, lat, lon)
#     try:
#         (idx_building, dico_building) = MultiPolygon_to_Indexed_Polygons(
#             building_area, merge_overlappings=True
#         )
#     except:
#         return 0
#     UI.vprint(2, "Number of building Multipolygons :", len(dico_pol_building))
#     vector_map.encode_MultiPolygon(
#         dico_building,
#         dem.alt_vec,
#         "WATER",
#         area_limit=min_area / 10000,
#         check=True,
#     )
#     return 1


################################################################################
def include_patches(vector_map, tile):
    def tanh_profile(alpha, x):
        return (numpy.tanh((x - 0.5) * alpha) / numpy.tanh(0.5 * alpha) + 1) / 2

    def spline_profile(x):
        return 3 * x ** 2 - 2 * x ** 3

    def plane_profile(x):
        return x

    patches_list = []
    patches_area = geometry.Polygon()
    # Closed patch polygons, kept so that INTERP_ALT seeds can be placed
    # per planar FACE after all patch files are read (see below).
    interp_alt_patch_polygons = []
    patch_dir = FNAMES.patch_dir(tile.lat, tile.lon)
    if not os.path.exists(patch_dir):
        return (patches_area, patches_list)
    # Sort patch files so manual patches are processed before auto patches.
    # This ensures manual patches take priority: if a manual patch covers an
    # airport, the corresponding _auto patch is skipped.
    all_patch_files = [
        f for f in os.listdir(patch_dir) if f[-10:] == ".patch.osm"
    ]
    manual_patches = [f for f in all_patch_files if "_auto.patch.osm" not in f]
    auto_patches = [f for f in all_patch_files if "_auto.patch.osm" in f]
    # Track which ICAO codes are covered by manual patches
    manual_icao_codes = set()
    for f in manual_patches:
        base = f[:-10]  # strip .patch.osm
        icao_prefix = base.split("_")[0].upper()
        manual_icao_codes.add(icao_prefix)
    # Honor the auto_patch mode when LOADING, not just when generating
    # (user 2026-05-22): the setting governs which auto-patches are
    # APPLIED, so changing it takes effect even when auto-patch files
    # already exist from a previous run (e.g. set to "None" → no auto
    # patches loaded, even if the .osm files are still on disk).  Manual
    # patches are always applied — the setting only governs auto-patches.
    # Backward compat: legacy bool True/False map to "All"/"None".
    auto_patch_mode = tile.auto_patch
    if auto_patch_mode is True:
        auto_patch_mode = "All"
    elif auto_patch_mode is False:
        auto_patch_mode = "None"
    # Process manual patches first, then auto patches
    ordered_patch_files = manual_patches + auto_patches
    for pfile_name in ordered_patch_files:
        # Skip auto-patches for airports that have a manual patch
        is_auto = "_auto.patch.osm" in pfile_name
        if is_auto:
            auto_icao = pfile_name.replace("_auto.patch.osm", "").upper()
            # Apply the auto_patch mode filter (mirrors generation in
            # driver.generate_auto_patches): None loads nothing; ICAO
            # loads only real 4-letter-alpha ICAO codes; All loads every.
            if auto_patch_mode == "None":
                UI.vprint(
                    1, "   Skipping auto-patch", pfile_name,
                    "(auto_patch=None).")
                continue
            if auto_patch_mode == "ICAO" and not (
                    len(auto_icao) == 4 and auto_icao.isalpha()):
                UI.vprint(
                    1, "   Skipping auto-patch", pfile_name,
                    "(non-ICAO code, auto_patch=ICAO).")
                continue
            if auto_icao in manual_icao_codes:
                UI.vprint(
                    1,
                    "   Skipping auto-patch",
                    pfile_name,
                    "(manual patch exists).",
                )
                continue
        UI.vprint(1, "   Patching", pfile_name)
        patch_layer = OSM.OSM_layer()
        try:
            patch_layer.update_dicosm(
                os.path.join(patch_dir, pfile_name),
                input_tags=None,
                target_tags=None,
            )
        except:
            UI.vprint(1, "     Error in treating", pfile_name, ", skipped.")
        patches_list.append(pfile_name[:-10])
        # For auto-patches, also add the bare ICAO code so that
        # encode_runways_taxiways_and_aprons() skips this airport.
        # The auto-patch must handle the full airport surface because
        # building flattening can create large DEM variances that the
        # normal pipeline's DEM-based polynomial fitting can't account for.
        if is_auto:
            patches_list.append(auto_icao)
        dw = patch_layer.dicosmw
        dn = patch_layer.dicosmn
        df = patch_layer.dicosmfirst
        dt = patch_layer.dicosmtags
        # reorganize them so that untagged dummy ways are treated last (due to
        # altitude being first done kept for all)
        # waylist=list(set(dw).intersection(df['w']).intersection(dt['w']))+
        # list(set(dw).intersection(df['w']).difference(dt['w']))
        # HACK
        waylist = tuple(df["w"].intersection(dt["w"])) + tuple(
            df["w"].difference(dt["w"])
        )
        for wayid in waylist:
            way = numpy.array(
                [dn[nodeid] for nodeid in dw[wayid]], dtype=float
            )
            way = way - numpy.array([[tile.lon, tile.lat]])
            alti_way_orig = tile.dem.alt_vec(way)
            cplx_way = False
            if wayid in dt["w"]:
                wtags = dt["w"][wayid]
                if "cst_alt_abs" in wtags:
                    alti_way = numpy.ones((len(way), 1)) * float(
                        wtags["cst_alt_abs"]
                    )
                elif "cst_alt_rel" in wtags:
                    alti_way = numpy.ones((len(way), 1)) * (
                        numpy.mean(tile.dem.alt_vec(way))
                        + float(wtags["cst_alt_rel"])
                    )
                elif "var_alt_rel" in wtags:
                    alti_way = alti_way_orig + float(wtags["var_alt_rel"])
                elif (
                    "altitude" in wtags
                ):  # deprecated : for backward compatibility only
                    try:
                        alti_way = numpy.ones((len(way), 1)) * float(
                            wtags["altitude"]
                        )
                    except:
                        alti_way = numpy.ones((len(way), 1)) * numpy.mean(
                            tile.dem.alt_vec(way)
                        )
                elif "node_altitudes" in wtags:
                    # Per-node altitude: comma-separated elevation values,
                    # one per node. Supports arbitrary polygon shapes with
                    # individually specified elevations at each vertex.
                    try:
                        alts = [
                            float(x)
                            for x in wtags["node_altitudes"].split(",")
                        ]
                        if len(alts) == len(way):
                            alti_way = numpy.array(alts).reshape(-1, 1)
                        else:
                            UI.vprint(
                                1,
                                "    node_altitudes count ({}) != node"
                                " count ({}), using DEM.".format(
                                    len(alts), len(way)
                                ),
                            )
                            alti_way = alti_way_orig
                    except Exception:
                        alti_way = alti_way_orig
                elif "altitude_high" in wtags:
                    cplx_way = True
                    if len(way) != 5 or (way[0] != way[-1]).all():
                        UI.vprint(
                            1,
                            "    Wrong number of nodes or non closed way for ",
                            "a altitude_high/altitude_low polygon, skipped.",
                        )
                        continue
                    short_high = way[-2:]
                    short_low = way[1:3]
                    try:
                        altitude_high = float(wtags["altitude_high"])
                        altitude_low = float(wtags["altitude_low"])
                    except:
                        altitude_high = tile.dem.alt_vec(short_high).mean()
                        altitude_low = tile.dem.alt_vec(short_low).mean()
                    try:
                        cell_size = float(wtags["cell_size"])
                    except:
                        cell_size = 10
                    try:
                        rnw_profile = wtags["profile"]
                    except:
                        rnw_profile = "plane"
                    try:
                        alpha = float(wtags["steepness"])
                    except:
                        alpha = 2
                    if "tanh" in rnw_profile:
                        rnw_profile = lambda x: tanh_profile(alpha, x)
                    elif rnw_profile == "spline":
                        rnw_profile = spline_profile
                    else:
                        rnw_profile = plane_profile
                    rnw_vect = (
                        short_high[0]
                        + short_high[1]
                        - short_low[0]
                        - short_low[1]
                    ) / 2
                    rnw_length = (
                        sqrt(
                            rnw_vect[0] ** 2 * cos(tile.lat * pi / 180) ** 2
                            + rnw_vect[1] ** 2
                        )
                        * 111120
                    )
                    # Floor at 1 cut: a large ``cell_size`` (the patch
                    # node-density knob) can drive ``int(rnw_length /
                    # cell_size)`` to 0, which skipped the graded-altitude
                    # path and left ``alti_way`` unbound (UnboundLocalError
                    # below).  One cut is negligible for triangle count and
                    # keeps the graded ``altitude_high/low`` profile (the
                    # alt fallback samples the raw DEM, which would drop a
                    # sloped pavement onto terrain).
                    cuts_long = max(1, int(rnw_length / cell_size))
                    if cuts_long:
                        cuts_long += 1
                        way = numpy.array(
                            [
                                way[0] + i / cuts_long * (way[1] - way[0])
                                for i in range(cuts_long)
                            ]
                            + [way[1]]
                            + [
                                way[2] + i / cuts_long * (way[3] - way[2])
                                for i in range(cuts_long)
                            ]
                            + [way[3], way[4]]
                        )
                        alti_way = numpy.array(
                            [
                                altitude_high
                                - rnw_profile(i / cuts_long)
                                * (altitude_high - altitude_low)
                                for i in range(cuts_long + 1)
                            ]
                        )
                        alti_way = numpy.hstack(
                            [alti_way, alti_way[::-1], alti_way[0]]
                        )
                else:
                    alti_way = alti_way_orig
            else:
                alti_way = alti_way_orig
            if not cplx_way:
                for i in range(len(way)):
                    nodeid = dw[wayid][i]
                    if nodeid in dt["n"]:
                        ntags = dt["n"][nodeid]
                        if "alt_abs" in ntags:
                            alti_way[i] = float(ntags["alt_abs"])
                        elif "alt_rel" in ntags:
                            alti_way[i] = alti_way_orig[i] + float(
                                ntags["alt_rel"]
                            )
            alti_way = alti_way.reshape((len(alti_way), 1))
            if (way[0] == way[-1]).all():
                try:
                    pol = geometry.Polygon(way)
                    if pol.is_valid and pol.area:
                        patches_area = patches_area.union(pol)
                        vector_map.insert_way(
                            numpy.hstack([way, alti_way]),
                            "INTERP_ALT",
                            check=True,
                        )
                        interp_alt_patch_polygons.append(pol)
                        if cplx_way and cuts_long:
                            for i in range(1, cuts_long):
                                id0 = vector_map.dico_nodes[tuple(way[i])]
                                id1 = vector_map.dico_nodes[tuple(way[-2 - i])]
                                vector_map.insert_edge(
                                    id0,
                                    id1,
                                    vector_map.dico_attributes["DUMMY"],
                                )
                    else:
                        UI.vprint(2, "     Skipping invalid patch polygon.")
                except:
                    UI.vprint(2, "     Skipping invalid patch polygon.")
            else:
                vector_map.insert_way(
                    numpy.hstack([way, alti_way]), "DUMMY", check=True
                )
    # Seed every planar FACE of the patch coverage, not one point per ring.
    # Triangle4XP spreads a regional attribute by plague, and the flood is
    # blocked by ANY segment carrying the same attribute bit (see the
    # preamble of O4_Vector_Utils and regionplague in Triangle4XP.c).  When
    # closed patch ways overlap — a bridge trench crossing pavement rings, a
    # retaining wall crossing an apron — their boundaries partition each
    # other's interiors into several faces, and a single seed per ring
    # leaves the other faces unmarked: their triangles keep the raw DEM
    # altitude even though every ring vertex carries the intended one.
    if interp_alt_patch_polygons:
        try:
            interp_alt_seeds = []
            covered = prep(patches_area)
            for face in ops.polygonize(
                ops.unary_union(
                    [pol.boundary for pol in interp_alt_patch_polygons]
                )
            ):
                seed_point = face.representative_point()
                if covered.contains(seed_point):
                    interp_alt_seeds.append(
                        numpy.array(seed_point.coords[0])
                    )
            if not interp_alt_seeds:
                raise ValueError("face seeding produced no seeds")
        except Exception:
            # Fall back to the historical one-seed-per-ring placement.
            interp_alt_seeds = [
                numpy.array(pol.representative_point().coords[0])
                for pol in interp_alt_patch_polygons
            ]
        vector_map.seeds.setdefault("INTERP_ALT", []).extend(
            interp_alt_seeds
        )
    for pdir_name in os.listdir(patch_dir):
        if not os.path.isdir(os.path.join(patch_dir, pdir_name)):
            continue
        UI.vprint(1, "   Including OBJ8 objects from", pdir_name)
        patches_list.append(pdir_name)
        for pfile_name in os.listdir(os.path.join(patch_dir, pdir_name)):
            pfile_namelong = os.path.join(patch_dir, pdir_name, pfile_name)
            try:
                pfile = open(pfile_namelong, "r")
            except:
                continue
            firstline = pfile.readline()
            if not "ANCHOR" in firstline:
                UI.vprint(
                    1,
                    "     Object ",
                    pfile_name,
                    " is missing and ANCHOR in first line, skipping it.",
                )
                continue
            pfile.close()
            try:
                (lon_anchor, lat_anchor, alt_anchor, heading_anchor) = [
                    float(x) for x in firstline.split()[1:]
                ]
            except:
                try:
                    (lon_anchor, lat_anchor, heading_anchor) = [
                        float(x) for x in firstline.split()[1:]
                    ]
                    alt_anchor = tile.dem.alt(
                        (lon_anchor - tile.lon, lat_anchor - tile.lat)
                    )
                except:
                    UI.vprint(
                        1,
                        "     Anchor wrongly encode for : ",
                        pfile_name,
                        " skipping that one.",
                    )
                    continue
            patches_area = patches_area.union(
                keep_obj8(
                    lat_anchor,
                    lon_anchor,
                    alt_anchor,
                    heading_anchor,
                    pfile_namelong,
                    vector_map,
                    tile,
                )
            )
    return (patches_area, patches_list)


################################################################################
def keep_obj8(
    lat_anchor,
    lon_anchor,
    alt_anchor,
    heading_anchor,
    objfile_name,
    vector_map,
    tile,
):
    dico_idx_nodes = {}
    idx_node = 0
    dico_index = {}
    index = 0
    latscale = GEO.m_to_lat
    lonscale = latscale / cos(lat_anchor * pi / 180)
    f = open(objfile_name, "r")
    for line in f.readlines():
        if line[0:2] == "VT":
            (xo, yo, zo) = [float(s) for s in line.split()[1:4]]
            Xo = xo * cos(heading_anchor * pi / 180) - zo * sin(
                heading_anchor * pi / 180
            )
            Zo = xo * sin(heading_anchor * pi / 180) + zo * cos(
                heading_anchor * pi / 180
            )
            y = numpy.round(lat_anchor - latscale * float(Zo) - tile.lat, 7)
            x = numpy.round(lon_anchor + lonscale * float(Xo) - tile.lon, 7)
            z = yo + alt_anchor
            dico_idx_nodes[idx_node] = vector_map.insert_node(x, y, z)
            idx_node += 1
        elif line[0:3] == "IDX":
            dico_index[index] = [int(x) for x in line.split()[1:]]
            index += 1
        elif line[0:4] == "TRIS":
            (offset, count) = [int(x) for x in line.split()[1:3]]
            list = []
            count_tmp = 0
            try:
                polist = []
                while count_tmp < count:
                    list += dico_index[offset]
                    count_tmp += len(dico_index[offset])
                    offset += 1
                for j in range(count // 3):
                    (a, b, c) = [
                        dico_idx_nodes[x] for x in list[3 * j : 3 * j + 3]
                    ]
                    if a == b or a == c or b == c:
                        continue
                    for (initp, endp) in ((a, b), (b, c), (c, a)):
                        vector_map.insert_edge(
                            initp,
                            endp,
                            vector_map.dico_attributes["INTERP_ALT"],
                            check=True,
                        )
                    seed = (
                        numpy.array(vector_map.nodes_dico[a])
                        + numpy.array(vector_map.nodes_dico[b])
                        + numpy.array(vector_map.nodes_dico[c])
                    ) / 3
                    if "INTERP_ALT" in vector_map.seeds:
                        vector_map.seeds["INTERP_ALT"].append(seed)
                    else:
                        vector_map.seeds["INTERP_ALT"] = [seed]
                    polist.append(
                        geometry.Polygon(
                            [
                                vector_map.nodes_dico[a],
                                vector_map.nodes_dico[b],
                                vector_map.nodes_dico[c],
                                vector_map.nodes_dico[a],
                            ]
                        )
                    )
                multipol = VECT.ensure_MultiPolygon(ops.unary_union(polist))
            except:
                pass
    f.close()
    return multipol
