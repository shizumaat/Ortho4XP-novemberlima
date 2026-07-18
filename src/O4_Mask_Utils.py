import os
import sys
import time
import queue
from math import atan, ceil, floor
import numpy
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from scipy.ndimage import uniform_filter1d
import skfmm
import O4_Bathymetry_Band as BATHYBAND
import O4_Coastal_Foam_Edge as FOAM
import O4_DEM_Utils as DEM
import O4_File_Names as FNAMES
import O4_UI_Utils as UI
import O4_Geo_Utils as GEO
import O4_Imagery_Utils as IMG
import O4_OSM_Utils as OSM
import O4_Vector_Utils as VECT
import O4_Mesh_Utils as MESH
from O4_Parallel_Utils import parallel_execute

mask_altitude_above = 0.5

# OpenStreetMap shallow-water fallback (spec section 4.4): where no fine
# bathymetry covers a tile, mapped shallow-water polygons are treated as
# water of an assumed constant depth per category, faded over
# SHALLOW_WATER_EDGE_FADE_M at the polygon edge (the drop-off).  Each
# category keeps its own cached Overpass query.  Reef flats are awash to
# a couple of metres; tidal flats (lagoons like the Ria Formosa, the
# Waddenzee) sit shallower still.
SHALLOW_WATER_CATEGORIES = (
    ("reef", ('way["natural"="reef"]', 'relation["natural"="reef"]'), 2.0),
    (
        "tidalflat",
        (
            'way["wetland"="tidalflat"]',
            'relation["wetland"="tidalflat"]',
        ),
        1.0,
    ),
)
SHALLOW_WATER_EDGE_FADE_M = 150.0
# Mask squares straddle tile edges (the orthophoto grid never aligns
# with integer degrees), so the fallback query reaches this far into
# the neighbouring tiles: flats polygons lying wholly beyond the tile
# line still rasterize into the shared straddling squares, and the two
# tiles' copies of such a square agree.  Sized for the worst case (a
# ZL14 mask square is ~0.35 degrees) plus the pre-mask pad and fade.
SHALLOW_WATER_QUERY_MARGIN_DEGREES = 0.5
# Cache schema for the shallow-water categories: bumped when the query
# bbox gained the margin so pre-margin caches re-download once.
SHALLOW_WATER_CACHE_SCHEMA = "margin-0.5"
# Where the measured bathymetry band's coverage simply ENDS while the
# water is still shallow — intertidal lidar that stops at the waterline,
# the band's own outer limit on a wide shallow shelf, an airport-radius
# gate boundary — the depth ramp cannot complete and the alpha would
# fall off a cliff quantized at the band's 10 m pixels (the "jagged
# squares" seen at the Ria Formosa, 2026-07-16).  The alpha is instead
# feathered across the data/nodata boundary over this distance; where
# the ramp does complete inside the data (Kauai) the feather multiplies
# near-zero values and changes nothing.
BATHYMETRY_COVERAGE_FADE_M = 150.0
# Mask workers spend nearly all their time in GIL-releasing numpy/scipy/PIL
# calls (profiled 2026-07-15), so threads scale with cores; capped to keep
# the per-worker image working set (a few hundred MB) in check.  Full width
# even under concurrent tile builds (2026-07-17 ruling: the operating
# system arbitrates processor contention).
masks_build_slots = max(2, min(12, (os.cpu_count() or 4) - 2))

################################################################################
def mask_name_for_texture(tile, til_x_left, til_y_top, zl, *args):
    if int(zl) < tile.mask_zl:
        return ""
    factor = 2 ** (zl - tile.mask_zl)
    m_til_x = (int(til_x_left / factor) // 16) * 16
    m_til_y = (int(til_y_top / factor) // 16) * 16
    rx = int((til_x_left - factor * m_til_x) / 16)
    ry = int((til_y_top - factor * m_til_y) / 16)
    return os.path.join(
        FNAMES.mask_dir(tile.lat, tile.lon),
        FNAMES.legacy_mask(m_til_x, m_til_y)
        )
################################################################################

################################################################################
def needs_mask(tile, til_x_left, til_y_top, zl, *args):
    if int(zl) < tile.mask_zl:
        return False
    factor = 2 ** (zl - tile.mask_zl)
    m_til_x = (int(til_x_left / factor) // 16) * 16
    m_til_y = (int(til_y_top / factor) // 16) * 16
    rx = int((til_x_left - factor * m_til_x) / 16)
    ry = int((til_y_top - factor * m_til_y) / 16)
    mask_file = os.path.join(
        FNAMES.mask_dir(tile.lat, tile.lon),
        FNAMES.legacy_mask(m_til_x, m_til_y)
        )
    if not os.path.isfile(mask_file):
        return False
    big_img = Image.open(mask_file)
    x0 = int(rx * 4096 / factor)
    y0 = int(ry * 4096 / factor)
    small_img = big_img.crop((x0, y0, x0 + 4096 // factor, y0 + 4096 // factor))
    small_array = numpy.array(small_img, dtype=numpy.uint8)
    if small_array.max() <= 30:
        return False
    else:
        return small_img
################################################################################

################################################################################
def build_masks(tile, for_imagery=False):
    
    if UI.is_working:
        return 0
    UI.is_working = 1
    
    # Which grey level for inland water equivalent ?
    im = Image.open(os.path.join(FNAMES.Utils_dir, "water_transition.png"))
    sea_level = im.getpixel((0, 127 * (1 - min(1, 0.1 + tile.ratio_water))))
    del im
    
    UI.red_flag = False
    UI.logprint(
        "Step 2.5 for tile lat=", tile.lat, ", lon=", tile.lon, ": starting."
    )
    UI.vprint(
        0,
        "\nStep 2.5 : Building masks for tile "
        + FNAMES.short_latlon(tile.lat, tile.lon)
        + " : \n--------\n",
    )

    timer = time.time()
    
    # Check we have a mesh for this tile
    if not os.path.exists(FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)):
        UI.lvprint(
            0,
            "ERROR: Mesh file ",
            FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon),
            "absent.",
        )
        UI.exit_message_and_bottom_line("")
        return 0
    
    # Custom extent: fall back to regular masks when the named extent is
    # not installed under Extents/ — a stale tile config must not degrade
    # the whole masks step (it used to warn once per mask square and, before
    # 2026-07-16, crashed every mask worker).
    custom_extent_code = tile.masks_custom_extent
    if custom_extent_code and custom_extent_code not in IMG.extents_dict:
        UI.lvprint(
            0,
            "WARNING: custom mask extent '" + str(custom_extent_code)
            + "' was not found under Extents/; building regular masks"
            + " instead.",
        )
        custom_extent_code = ""

    # Check or create dest dir
    dest_dir = (
        FNAMES.mask_dir(tile.lat, tile.lon)
        if not for_imagery
        else os.path.join(
            FNAMES.mask_dir(tile.lat, tile.lon), "Combined_imagery"
        )
    )
    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir)
    
    # Select nearby meshes
    mesh_list = select_neighbor_meshes(tile)

    # Delete old masks
    UI.vprint(1, "-> Deleting existing masks")
    delete_old_masks_in_tile(tile, dest_dir)
    
    # Record water tris form mesh (and portions of nearby meshes)
    UI.vprint(1, "-> Reading mesh data")
    (dico_sea, dico_inland, coastline_sea_present) = record_water_tris(tile)

    UI.vprint(1, "-> Construction of the masks")

    # Bathymetry (docs/specs/coastal-bathymetry-spec.md sections 3-4):
    # resolve the masks_use_DEM_too tri-state.  "auto" engages exactly
    # when a bathymetry provider covers the tile (the band fetch answers
    # that); legacy "True" keeps the custom_dem land refinement AND gets
    # the depth ramp when a band exists; "False" is the pure vector fade.
    masks_dem_setting = str(tile.masks_use_DEM_too)
    bathymetry_band_vrt = None
    if masks_dem_setting in ("auto", "True"):
        # "auto" only engages on fine nearshore data; explicit True also
        # accepts the coarse global fallbacks (GEBCO and friends) and
        # the intertidal-only twins (exposed-flats lidar the mapped
        # fallback otherwise stands in for).
        bathymetry_band_vrt = BATHYBAND.ensure_bathymetry_band(
            tile,
            fine_nearshore_only=(masks_dem_setting == "auto"),
            intertidal_ok=(masks_dem_setting == "True"),
        )
    legacy_dem_refinement = masks_dem_setting == "True"

    # OpenStreetMap shallow-water fallback (spec section 4.4): only where
    # measured bathymetry is unavailable — atolls, reef coasts and tidal
    # lagoons outside every fine provider's coverage still get their
    # mapped reef flats and tidal flats.  With auto mode's airport-radius
    # gate the band is deliberately partial, so the fallback also loads
    # alongside a gated band and fills the squares beyond the radius
    # (measured data still wins per square, below in build_mask).
    airport_gated_band = (
        bathymetry_band_vrt is not None
        and masks_dem_setting == "auto"
        and float(
            getattr(
                tile,
                "bathymetry_airport_radius_km",
                BATHYBAND.DEFAULT_AIRPORT_RADIUS_KM,
            )
        )
        > 0
    )
    shallow_water_categories = None
    if shallow_water_fallback_wanted(
        tile, dico_sea, coastline_sea_present, bathymetry_band_vrt,
        airport_gated_band
    ):
        shallow_water_categories = load_shallow_water_polygons(tile)

    if legacy_dem_refinement:
        try:
            fill_nodata = tile.fill_nodata or "to zero"
            source = (
                (";" in tile.custom_dem) and tile.custom_dem.split(";")[0]
            ) or tile.custom_dem
            tile.dem = DEM.DEM(
                tile.lat, tile.lon, source, fill_nodata, info_only=False
            )
        except:
            UI.exit_message_and_bottom_line(
                "\nERROR: Could not determine the appropriate elevation source.",
                " Please check your custom_dem entry."
            )
            return 0

    #################################
    def build_mask(til_x, til_y):

        (til_x_min, til_y_min) = GEO.wgs84_to_orthogrid(
            tile.lat + 1, tile.lon, tile.mask_zl)
        (til_x_max, til_y_max) = GEO.wgs84_to_orthogrid(
            tile.lat, tile.lon + 1, tile.mask_zl)
        if (til_x < til_x_min or til_x > til_x_max or til_y < til_y_min or 
            til_y > til_y_max):
            return 1

        pre_mask = build_water_pre_mask(til_x, til_y, mesh_list, dico_sea,
                                         dico_inland, sea_level, tile)
        if legacy_dem_refinement:
            dem_array = build_dem_pre_mask(til_x, til_y, tile)
            pre_mask = numpy.maximum(pre_mask, dem_array)
            del(dem_array)

        (bathymetry_land, bathymetry_alpha) = (None, None)
        if bathymetry_band_vrt:
            (bathymetry_land, bathymetry_alpha) = build_bathymetry_arrays(
                til_x, til_y, tile, bathymetry_band_vrt)
            if bathymetry_land is not None:
                # The band's topo side refines the land pre-mask: measured
                # islets survive even when the OSM coastline misses them.
                pre_mask = numpy.maximum(pre_mask, bathymetry_land)

        shallow_water_alpha = None
        if shallow_water_categories is not None and (
            bathymetry_land is None and bathymetry_alpha is None
        ):
            # Measured bathymetry always wins: the mapped fallback only
            # fills squares the (possibly airport-gated) band left bare.
            shallow_water_alpha = build_shallow_water_alpha(
                til_x, til_y, tile, shallow_water_categories)

        if custom_extent_code:
            custom_array = build_custom_pre_mask(
                til_x, til_y, sea_level, tile, custom_extent_code)

        if (
            (pre_mask.max() == 0)
            and (not custom_extent_code or custom_array.max() == 0)
            and (bathymetry_alpha is None or bathymetry_alpha.max() == 0)
            and (
                shallow_water_alpha is None
                or shallow_water_alpha.max() == 0
            )
        ):
            # Nothing to mask — but an offshore shallow (an atoll in a
            # full-sea square) still deserves a mask via the depth ramp
            # or the mapped shallow-water fallback.
            return 1
        
        
        blured_mask = blur_mask(pre_mask, tile, sea_level)

        # Land back to full 255, inland water feathered from the shore
        # down to its constant grey, sea keeping the blur fade; cropped
        # to final size (custom extent mask maxed in below).
        feather_pixels = int(
            float(getattr(tile, "inland_shore_feather_m", 120.0))
            / GEO.webmercator_pixel_size(tile.lat + 0.5, tile.mask_zl)
        )
        blured_mask = compose_water_mask(
            pre_mask, blured_mask, sea_level, feather_pixels)
        
        if custom_extent_code:
            blured_mask = numpy.maximum(blured_mask, custom_array)

        if tile.coastal_foam_edge and not (
            blured_mask.max() == 0 or blured_mask.min() == 255
        ):
            masks_width_meters = tile.masks_width
            if isinstance(masks_width_meters, list):
                masks_width_meters = sum(masks_width_meters)
            mask_pixel_size_meters = GEO.webmercator_pixel_size(
                tile.lat + 0.5, tile.mask_zl
            )
            foam_width_pixels = max(
                30, int(masks_width_meters / mask_pixel_size_meters / 2)
            )
            foam_mask = FOAM.apply_coastal_foam_edge(
                blured_mask,
                foam_width_pixels=foam_width_pixels,
                sea_transparency_gray=int(sea_level),
                # Seed from the mask identity so rebuilds are reproducible.
                random_seed=(til_x << 16) ^ til_y,
            )
            if foam_mask is not None:
                blured_mask = foam_mask

        if bathymetry_alpha is not None:
            # Depth-graded water alpha (spec section 4.2), applied last:
            # a maximum can only REVEAL imagery over measured shallows —
            # reefs stay visible beyond masks_width and through the foam
            # restyle — never cut visibility inside the distance fade.
            blured_mask = numpy.maximum(blured_mask, bathymetry_alpha)

        if shallow_water_alpha is not None:
            # Mapped shallow-water fallback (spec section 4.4), same
            # maximum semantics as the measured ramp.
            blured_mask = numpy.maximum(blured_mask, shallow_water_alpha)

        if not (blured_mask.max() == 0 or blured_mask.min() == 255):
            mask_im = Image.fromarray(blured_mask)
            mask_im.save(os.path.join(
                dest_dir, FNAMES.legacy_mask(til_x, til_y)))
            del blured_mask
            
            # Distance masks for bathymetry cut-off
            if (tile.distance_masks_too):
                pre_mask = (pre_mask > 0).astype(float) * 2 - 1
                band = 255 / 2**(16 - tile.mask_zl)
                dist_array = skfmm.distance(pre_mask, narrow = band)
                if (isinstance(dist_array, numpy.ma.core.MaskedArray)):
                    dist_array = dist_array.filled(-99999)
                dist_array[pre_mask > 0] = 0
                del(pre_mask)
                dist_array = dist_array[1024 : 4096 + 1024, 1024 : 4096 + 1024]
                dist_array = dist_array * (2**(16 - tile.mask_zl))
                dist_array = numpy.minimum(-numpy.minimum(dist_array, 0), 255)
                dist_array = dist_array.astype(numpy.uint8)
                masks_im = Image.fromarray(dist_array)
                masks_im.save(os.path.join(
                    dest_dir, FNAMES.distance_mask(til_x, til_y)))
                UI.vprint(1, "   Created", FNAMES.legacy_mask(til_x, til_y),
                "and", FNAMES.distance_mask(til_x, til_y))
            else:
                UI.vprint(1, "   Created", FNAMES.legacy_mask(til_x, til_y))
        return 1
    #################################
    
    masks_queue = queue.Queue()
    for key in dico_sea:
        masks_queue.put(key)
    dico_progress = {"done": 0, "bar": 1}

    parallel_execute(build_mask, masks_queue, masks_build_slots,
                     progress=dico_progress)

    UI.progress_bar(1, 100)
    UI.timings_and_bottom_line(timer)
    UI.logprint(
        "Step 2.5 for tile lat=", tile.lat, ", lon=", tile.lon, ": normal exit."
    )
    return
################################################################################
    
################################################################################
def select_neighbor_meshes(tile):
    mesh_list = []
    for close_lat in range(tile.lat - 1, tile.lat + 2):
        for close_lon in range(tile.lon - 1, tile.lon + 2):
            close_build_dir = (tile.build_dir if tile.grouped
                else tile.build_dir.replace(
                    FNAMES.tile_dir(tile.lat, tile.lon),
                    FNAMES.tile_dir(close_lat, close_lon),
                )
            )
            close_mesh_file_name = FNAMES.mesh_file(
                close_build_dir, close_lat, close_lon
            )
            if os.path.isfile(close_mesh_file_name):
                mesh_list.append(close_mesh_file_name)
    return mesh_list
################################################################################

################################################################################
def delete_old_masks_in_tile(tile, dest_dir):

    (til_x_min, til_y_min) = GEO.wgs84_to_orthogrid(
        tile.lat + 1, tile.lon, tile.mask_zl)
    (til_x_max, til_y_max) = GEO.wgs84_to_orthogrid(
        tile.lat, tile.lon + 1, tile.mask_zl)

    for til_x in range(til_x_min, til_x_max + 1, 16):
        for til_y in range(til_y_min, til_y_max + 1, 16):
            try:
                os.remove(
                    os.path.join(dest_dir, FNAMES.legacy_mask(til_x, til_y))
                )
            except:
                pass
################################################################################
    
################################################################################
def build_water_pre_mask(til_x, til_y, mesh_list, dico_sea, dico_inland,
                         sea_level, tile):
    (latm0, lonm0) = GEO.gtile_to_wgs84(til_x, til_y, tile.mask_zl)
    (px0, py0) = GEO.wgs84_to_pix(latm0, lonm0, tile.mask_zl)
    px0 -= 1024
    py0 -= 1024
    # 1) We start with a black mask
    mask_im = Image.new("L", (4096 + 2 * 1024, 4096 + 2 * 1024), "black")
    mask_draw = ImageDraw.Draw(mask_im)
    # 2) We fill it with white over the extent of each tile around for 
    # which we had a mesh available
    for mesh_file_name in mesh_list:
        latlonstr = mesh_file_name.split(".mes")[-2][-7:]
        lathere = int(latlonstr[0:3])
        lonhere = int(latlonstr[3:7])
        (px1, py1) = GEO.wgs84_to_pix(lathere, lonhere, tile.mask_zl)
        (px2, py2) = GEO.wgs84_to_pix(lathere, lonhere + 1, tile.mask_zl)
        (px3, py3) = GEO.wgs84_to_pix(
            lathere + 1, lonhere + 1, tile.mask_zl
        )
        (px4, py4) = GEO.wgs84_to_pix(lathere + 1, lonhere, tile.mask_zl)
        px1 -= px0
        px2 -= px0
        px3 -= px0
        px4 -= px0
        py1 -= py0
        py2 -= py0
        py3 -= py0
        py4 -= py0
        mask_draw.polygon(
            [(px1, py1), (px2, py2), (px3, py3), (px4, py4)], fill="white"
        )
    # 3a)  We overwrite the white part of the mask with grey (ratio_water 
    # dependent) where inland water was detected in the first part above
    if (til_x, til_y) in dico_inland:
        for (lat1, lon1, lat2, lon2, lat3, lon3) in dico_inland[
            (til_x, til_y)
        ]:
            (px1, py1) = GEO.wgs84_to_pix(lat1, lon1, tile.mask_zl)
            (px2, py2) = GEO.wgs84_to_pix(lat2, lon2, tile.mask_zl)
            (px3, py3) = GEO.wgs84_to_pix(lat3, lon3, tile.mask_zl)
            px1 -= px0
            px2 -= px0
            px3 -= px0
            py1 -= py0
            py2 -= py0
            py3 -= py0
            mask_draw.polygon(
                [(px1, py1), (px2, py2), (px3, py3)], fill=sea_level
            )  # int(255*(1-tile.ratio_water)))
    # 3b) We overwrite the white + grey part of the mask with black where 
    # sea water was detected in the first part above
    for (lat1, lon1, lat2, lon2, lat3, lon3) in dico_sea[(til_x, til_y)]:
        (px1, py1) = GEO.wgs84_to_pix(lat1, lon1, tile.mask_zl)
        (px2, py2) = GEO.wgs84_to_pix(lat2, lon2, tile.mask_zl)
        (px3, py3) = GEO.wgs84_to_pix(lat3, lon3, tile.mask_zl)
        px1 -= px0
        px2 -= px0
        px3 -= px0
        py1 -= py0
        py2 -= py0
        py3 -= py0
        mask_draw.polygon(
            [(px1, py1), (px2, py2), (px3, py3)], fill="black"
        )
    del mask_draw
    # mask_im=mask_im.convert("L")
    img_array = numpy.array(mask_im, dtype=numpy.uint8)
    return img_array
################################################################################

################################################################################
def build_dem_pre_mask(til_x, til_y, tile):
    (latm0, lonm0) = GEO.gtile_to_wgs84(til_x, til_y, tile.mask_zl)
    (px0, py0) = GEO.wgs84_to_pix(latm0, lonm0, tile.mask_zl)
    px0 -= 1024
    py0 -= 1024
    # computing the part of the mask coming from the DEM:
    (latmax, lonmin) = GEO.pix_to_wgs84(px0, py0, tile.mask_zl)
    (latmin, lonmax) = GEO.pix_to_wgs84(px0 + 6144, py0 + 6144, tile.mask_zl)
    (x03857, y03857) = GEO.geo_to_webm(lonmin, latmax)
    (x13857, y13857) = GEO.geo_to_webm(lonmax, latmin)
    (
        (lonmin, lonmax, latmin, latmax),
        demarr4326,
    ) = tile.dem.super_level_set(
        mask_altitude_above, (lonmin, lonmax, latmin, latmax)
    )
    if demarr4326.any():
        demim4326 = Image.fromarray(
            demarr4326.astype(numpy.uint8) * 255
        )
        del demarr4326
        s_bbox = (lonmin, latmax, lonmax, latmin)
        t_bbox = (x03857, y03857, x13857, y13857)
        demim3857 = IMG.gdalwarp_alternative(
            s_bbox, "4326", demim4326, t_bbox, "3857", (6144, 6144)
        )
        demim3857 = demim3857.filter(
            ImageFilter.GaussianBlur(0.3 * 2 ** (tile.mask_zl - 14))
        )  # slight increase of area
        dem_array = (
            numpy.array(demim3857, dtype=numpy.uint8) > 0
        ).astype(numpy.uint8) * 255
        del demim3857
        del demim4326
    else:
        dem_array = numpy.zeros((6144, 6144), dtype=numpy.uint8)
    return dem_array
################################################################################

################################################################################
def shallow_water_fallback_wanted(tile, dico_sea, coastline_sea_present,
                                  bathymetry_band_vrt, airport_gated_band):
    """Whether the mapped shallow-water fallback should download at all.

    Masks are only ever built for the squares in ``dico_sea``, so a
    landlocked tile (no sea or sea-equivalent water in the mask region,
    ``dico_sea`` empty) must not spend two download round trips on reef
    and tidal-flat queries whose result could never be rasterized.

    ``coastline_sea_present`` sharpens that to MARINE water (owner
    2026-07-18): reefs and tidal flats are tidal features, and a tile
    whose mask squares come only from sea-EQUIVALENT lakes (bit 4, the
    large-lake routing — the Whitehorse lakes around CYXY) has nothing
    they could ever apply to.  Left open, the queries went to the
    regional-extract filter chain (north-admreg + us + us-pacific +
    us/alaska + yukon) and stalled the masks step for ~8 minutes.

    Beyond that, the fallback loads when no measured band covers the
    tile — or alongside an airport-gated band, whose beyond-the-radius
    squares it fills — and only while the fallback setting is on.
    """
    if not dico_sea:
        return False
    if not coastline_sea_present:
        return False
    if bathymetry_band_vrt is not None and not airport_gated_band:
        return False
    return str(getattr(tile, "osm_shallow_water_fallback", True)) == "True"


################################################################################
def load_shallow_water_polygons(tile):
    """The tile's mapped shallow-water polygons, by category.

    Fallback data source for the depth-graded masks (spec section 4.4):
    coral reefs (``natural=reef`` — Funafuti carries 154 elements) and
    tidal flats (``wetland=tidalflat`` — the Ria Formosa around Faro
    carries 32) are frequently mapped in OpenStreetMap where no open
    bathymetry exists.  Each category keeps its own cached Overpass
    query; a failed category download is skipped loudly, an empty one
    silently.  Returns a list of ``(multipolygon, assumed_depth_m)``
    pairs in tile-relative degree offsets, or ``None`` when nothing is
    mapped.
    """
    categories = []
    for (cached_suffix, queries, assumed_depth_m) in (
        SHALLOW_WATER_CATEGORIES
    ):
        layer = OSM.OSM_layer()
        try:
            if not OSM.OSM_queries_to_OSM_layer(
                list(queries),
                layer,
                tile.lat,
                tile.lon,
                [],
                cached_suffix=cached_suffix,
                cache_schema=SHALLOW_WATER_CACHE_SCHEMA,
                bbox_margin_degrees=SHALLOW_WATER_QUERY_MARGIN_DEGREES,
            ):
                UI.lvprint(
                    0,
                    "   WARNING: the",
                    cached_suffix,
                    "download for the shallow-water mask fallback failed;"
                    " that category is skipped.",
                )
                continue
            area = OSM.OSM_to_MultiPolygon(layer, tile.lat, tile.lon)
        except Exception as error:
            UI.vprint(
                1,
                "   WARNING: shallow-water fallback category",
                cached_suffix,
                "skipped:",
                str(error),
            )
            continue
        if area.is_empty:
            continue
        polygon_count = len(getattr(area, "geoms", []))
        UI.vprint(
            1,
            "   Shallow-water mask fallback:",
            polygon_count,
            "OpenStreetMap",
            cached_suffix,
            "polygon(s).",
        )
        categories.append((area, assumed_depth_m))
    return categories or None


def build_shallow_water_alpha(til_x, til_y, tile, shallow_water_categories):
    """Rasterize the mapped shallow-water alpha for one mask square.

    Each category's polygons are treated as water of its assumed
    constant depth through the same spline ramp as measured depths —
    deeper categories draw first so shallower ones win on overlap —
    then the whole canvas is softened over
    :data:`SHALLOW_WATER_EDGE_FADE_M` at the polygon edges (the
    drop-off).  Returns a 4096² uint8 array, or ``None`` when no polygon
    touches the square.  Never raises.
    """
    from shapely.geometry import box as shapely_box

    (latm0, lonm0) = GEO.gtile_to_wgs84(til_x, til_y, tile.mask_zl)
    (latm1, lonm1) = GEO.gtile_to_wgs84(
        til_x + 16, til_y + 16, tile.mask_zl
    )
    # Tile-relative degree bbox of the square, padded by the edge fade.
    pad_degrees = 2 * SHALLOW_WATER_EDGE_FADE_M / GEO.lat_to_m
    square = shapely_box(
        lonm0 - tile.lon - pad_degrees,
        latm1 - tile.lat - pad_degrees,
        lonm1 - tile.lon + pad_degrees,
        latm0 - tile.lat + pad_degrees,
    )
    depth_ceiling = max(
        float(getattr(tile, "reef_visibility_depth", 25.0)), 0.01
    )
    (px0, py0) = GEO.wgs84_to_pix(latm0, lonm0, tile.mask_zl)
    canvas = Image.new("L", (4096, 4096), "black")
    canvas_draw = ImageDraw.Draw(canvas)
    drawn = False
    # Deeper categories first: on overlap the shallower (brighter) fill
    # painted later wins.
    for (polygons, assumed_depth_m) in sorted(
        shallow_water_categories, key=lambda pair: -pair[1]
    ):
        if not polygons.intersects(square):
            continue
        try:
            local_area = polygons.intersection(square)
        except Exception:
            local_area = polygons.buffer(0).intersection(square)
        if local_area.is_empty:
            continue
        shallowness = max(
            min(1.0 - assumed_depth_m / depth_ceiling, 1.0), 0.0
        )
        fill_alpha = int(
            round(255 * shallowness * shallowness * (3 - 2 * shallowness))
        )
        if fill_alpha == 0:
            continue
        geometries = getattr(local_area, "geoms", [local_area])
        rings = []  # (is_hole, ring)
        for geometry in geometries:
            if geometry.geom_type != "Polygon":
                continue
            rings.append((False, geometry.exterior))
            for interior in geometry.interiors:
                rings.append((True, interior))
        for (is_hole, ring) in rings:
            pixel_ring = [
                tuple(
                    numpy.array(
                        GEO.wgs84_to_pix(
                            tile.lat + y, tile.lon + x, tile.mask_zl
                        )
                    )
                    - (px0, py0)
                )
                for (x, y) in zip(*ring.xy)
            ]
            if len(pixel_ring) >= 3:
                canvas_draw.polygon(
                    pixel_ring, fill=0 if is_hole else fill_alpha
                )
                drawn = True
    del canvas_draw
    if not drawn:
        return None
    fade_pixels = SHALLOW_WATER_EDGE_FADE_M / GEO.webmercator_pixel_size(
        tile.lat + 0.5, tile.mask_zl
    )
    canvas = canvas.filter(ImageFilter.GaussianBlur(fade_pixels / 2))
    shallow_water_alpha = numpy.array(canvas, dtype=numpy.uint8)
    if not shallow_water_alpha.any():
        return None
    return shallow_water_alpha


def _feather_alpha_at_coverage_edge(alpha, valid, mask_pixel_size_m):
    """Fade the depth-graded alpha across the band's coverage boundary.

    ``alpha`` (float32, full 6144² pre-mask geometry) is exact inside
    ``valid``; outside, the band has no data and the value is 0.  When
    the data ends while the alpha is still high (an intertidal source
    that stops at the waterline, the band's outer limit over a shallow
    shelf, an airport-radius gate boundary), that boundary is a cliff
    quantized at the band's native pixels.  This blends it out over
    :data:`BATHYMETRY_COVERAGE_FADE_M`:

    * a smooth 0..1 coverage ramp (gaussian of the validity mask,
      0.5 exactly on the boundary) multiplies the whole field, and
    * outside the data the alpha is first extended by normalized
      convolution (nearby measured values averaged), so the fade decays
      from the measured edge value instead of from 0.

    Where the ramp already completed inside the data the feather
    multiplies near-zero values — visually a no-op (Kauai).  Computed
    at 1/4 resolution: the band's native pixels are coarser still, and
    the fade spans dozens of mask pixels.
    """
    from scipy.ndimage import gaussian_filter

    fade_pixels = BATHYMETRY_COVERAGE_FADE_M / mask_pixel_size_m
    decimation = 4
    sigma = max(fade_pixels / 2.0 / decimation, 0.5)
    valid_small = valid[::decimation, ::decimation].astype(numpy.float32)
    alpha_small = alpha[::decimation, ::decimation]
    coverage_ramp = gaussian_filter(valid_small, sigma)
    extended = gaussian_filter(alpha_small, sigma) / numpy.maximum(
        coverage_ramp, 1e-3
    )
    ramp_image = Image.fromarray(
        numpy.clip(numpy.round(coverage_ramp * 255.0), 0, 255).astype(
            numpy.uint8
        )
    ).resize((alpha.shape[1], alpha.shape[0]), Image.BILINEAR)
    extended_image = Image.fromarray(
        numpy.clip(numpy.round(extended), 0, 255).astype(numpy.uint8)
    ).resize((alpha.shape[1], alpha.shape[0]), Image.BILINEAR)
    coverage_ramp = (
        numpy.asarray(ramp_image, dtype=numpy.float32) / 255.0
    )
    filled = numpy.where(
        valid, alpha, numpy.asarray(extended_image, dtype=numpy.float32)
    )
    return filled * coverage_ramp


################################################################################
def build_bathymetry_arrays(til_x, til_y, tile, band_vrt_path):
    """Windowed read of the bathymetry band over one mask square.

    Warps the band VRT to the square's padded web-mercator grid (6144²,
    the pre-mask geometry) and derives two arrays
    (docs/specs/coastal-bathymetry-spec.md section 4.2):

    * ``land_array`` — 6144² uint8, 255 where the band's topo side is at
      or above ``mask_altitude_above`` (joins the land pre-mask before
      the blur), else 0; ``None`` when the square has no such pixel.
    * ``water_alpha`` — 4096² uint8 (final mask geometry), the
      depth-graded imagery visibility ``255 * spline(1 - depth / D)``
      with ``D = tile.reef_visibility_depth``: opaque at the waterline,
      0 at depth ``D`` and beyond (pure X-Plane water — a non-zero floor
      would seam at the band edge); ``None`` when the square has none.
      The ramp extends up to ``mask_altitude_above`` (the intertidal
      strip between the waterline and the land threshold is opaque, not
      a hole), and it is feathered over
      :data:`BATHYMETRY_COVERAGE_FADE_M` across the band's data/nodata
      boundary — sources that stop while the water is still shallow
      (intertidal lidar, a gated or truncated band) fade out instead of
      falling off a pixel-quantized cliff.

    Returns ``(None, None)`` when the square lies outside the band's
    coverage or the warp fails.  Never raises.
    """
    try:
        from osgeo import gdal
    except ImportError:
        return (None, None)
    (latm0, lonm0) = GEO.gtile_to_wgs84(til_x, til_y, tile.mask_zl)
    (px0, py0) = GEO.wgs84_to_pix(latm0, lonm0, tile.mask_zl)
    px0 -= 1024
    py0 -= 1024
    (latmax, lonmin) = GEO.pix_to_wgs84(px0, py0, tile.mask_zl)
    (latmin, lonmax) = GEO.pix_to_wgs84(px0 + 6144, py0 + 6144, tile.mask_zl)
    (web_x_min, web_y_max) = GEO.geo_to_webm(lonmin, latmax)
    (web_x_max, web_y_min) = GEO.geo_to_webm(lonmax, latmin)
    try:
        gdal.UseExceptions()
        warped = gdal.Warp(
            "",
            band_vrt_path,
            options=gdal.WarpOptions(
                format="MEM",
                outputType=gdal.GDT_Float32,
                dstSRS="EPSG:3857",
                outputBounds=(web_x_min, web_y_min, web_x_max, web_y_max),
                width=6144,
                height=6144,
                resampleAlg="bilinear",
                dstNodata=-32768.0,
            ),
        )
        if warped is None:
            return (None, None)
        values = warped.GetRasterBand(1).ReadAsArray()
        warped = None
    except Exception as error:
        UI.vprint(
            2, "   Bathymetry window read failed for one mask square:",
            str(error),
        )
        return (None, None)
    if values is None:
        return (None, None)
    valid = values > -32000.0
    if not valid.any():
        return (None, None)

    land_array = (
        ((values >= mask_altitude_above) & valid).astype(numpy.uint8) * 255
    )
    reef_depth = max(
        float(getattr(tile, "reef_visibility_depth", 25.0)), 0.01
    )
    shallowness = numpy.clip(1.0 + values / reef_depth, 0.0, 1.0)
    spline = shallowness * shallowness * (3.0 - 2.0 * shallowness)
    # The ramp reaches up to the land threshold, not just to 0: the
    # intertidal strip is opaque imagery (values above 0 clip to full
    # shallowness), and beyond the threshold the land pre-mask takes
    # over at the same contour — no gap, no cliff.
    water = valid & (values <= mask_altitude_above)
    alpha = numpy.where(water, 255.0 * spline, 0.0).astype(numpy.float32)
    if not valid.all():
        alpha = _feather_alpha_at_coverage_edge(
            alpha, valid, GEO.webmercator_pixel_size(latm0, tile.mask_zl)
        )
    water_alpha = numpy.clip(
        numpy.round(alpha[1024 : 4096 + 1024, 1024 : 4096 + 1024]), 0, 255
    ).astype(numpy.uint8)

    if not land_array.any():
        land_array = None
    if not water_alpha.any():
        water_alpha = None
    return (land_array, water_alpha)
################################################################################

################################################################################
def build_custom_pre_mask(til_x, til_y, sea_level, tile, extent_code):
    custom_mask_array = numpy.zeros((4096, 4096), dtype=numpy.uint8)
    (latm0, lonm0) = GEO.gtile_to_wgs84(til_x, til_y, tile.mask_zl)
    (latm1, lonm1) = GEO.gtile_to_wgs84(til_x + 16, til_y + 16, tile.mask_zl)
    bbox_4326 = (lonm0, latm0, lonm1, latm1)
    masks_im = IMG.has_data(
        bbox_4326,
        extent_code,
        True,
        mask_size=(4096, 4096),
        is_sharp_resize=False,
        is_mask_layer=False,
    )
    if masks_im:
        custom_mask_array = (
            numpy.array(masks_im, dtype=numpy.uint8) * (sea_level / 255)
        ).astype(numpy.uint8)

    return custom_mask_array
################################################################################

################################################################################
def compose_water_mask(pre_mask, blured_full, sea_level, feather_pixels,
                       crop_margin=1024):
    """Final mask from the pre-mask classes and the blurred fade.

    * LAND pixels (255 in the pre-mask) return to full opacity — the
      blur must never thin them.
    * SEA pixels (0) keep the blurred distance fade untouched.
    * INLAND-water pixels (the ``sea_level`` grey) get the SHORE
      FEATHER (2026-07-17): opaque at the land shoreline, easing down
      to the constant inland grey over ``feather_pixels``, and FLOORED
      at that grey — the fade can approach the water look but never
      continue toward deep-water transparency inside mapped water.
      The feather profile mirrors sand mode (triangular blur of the
      land indicator, doubled and clipped), so an inland shore reads
      like a narrow beach fade that settles at the ``ratio_water``
      blend instead of at open water.  ``feather_pixels <= 0`` keeps
      the historic hard clamp.

    ``pre_mask`` and ``blured_full`` are full pre-mask geometry
    (``crop_margin`` on each side); the returned array is cropped.
    """
    crop = slice(crop_margin, pre_mask.shape[0] - crop_margin)
    land_full = pre_mask == 255
    composed = numpy.maximum(
        land_full[crop, crop].astype(numpy.uint8) * 255,
        blured_full[crop, crop],
    )
    inland = ((pre_mask > 0) & ~land_full)[crop, crop]
    if not inland.any():
        return composed
    grey = int(sea_level)
    if feather_pixels <= 0 or grey >= 255:
        composed[inland] = 255
        return composed
    shore_ramp = triangular_blur_along_axis(
        land_full.astype(numpy.uint8) * 255, feather_pixels, axis=1)
    shore_ramp = triangular_blur_along_axis(
        shore_ramp, feather_pixels, axis=0)
    shore_ramp = (
        2 * numpy.minimum(shore_ramp[crop, crop], 127)
    ).astype(numpy.uint16)
    feathered = (
        grey + ((255 - grey) * shore_ramp[inland]) // 255
    ).astype(numpy.uint8)
    composed[inland] = numpy.maximum(feathered, grey)
    return composed


################################################################################
def water_type_is_inland(water_bits):
    """Mapped inland water WINS over coastline sea.

    A triangle carrying both the WATER and SEA bits (3) sits inside an
    OpenStreetMap water polygon that the coastline's sea flood also
    reached — the Ria Formosa lagoon behind rings cut at tile edges is
    the canonical case.  The mapper's polygon is the deliberate signal,
    so such triangles take the INLAND treatment (no deep-water fade).
    SEA_EQUIV (bit 4) keeps the sea treatment: that class is itself an
    explicit sea-mask routing (the large-lake rule).
    """
    return bool(water_bits & 1) and not (water_bits & 4)


################################################################################
def record_water_tris(tile):
    mesh_list = []
    for close_lat in range(tile.lat - 1, tile.lat + 2):
        for close_lon in range(tile.lon - 1, tile.lon + 2):
            close_build_dir = (
                tile.build_dir
                if tile.grouped
                else tile.build_dir.replace(
                    FNAMES.tile_dir(tile.lat, tile.lon),
                    FNAMES.tile_dir(close_lat, close_lon),
                )
            )
            close_mesh_file_name = FNAMES.mesh_file(
                close_build_dir, close_lat, close_lon
            )
            if os.path.isfile(close_mesh_file_name):
                mesh_list.append(close_mesh_file_name)
    ####################
    dico_sea = {}
    dico_inland = {}
    # Whether any recorded triangle carries the coastline-flood SEA bit
    # (bit 2) as its ruling class.  Sea-EQUIVALENT lakes (bit 4, the
    # large-lake mask routing) fill dico_sea without setting this: they
    # get mask squares but are not marine water, and marine-only
    # consumers (the reef/tidal-flat shallow-water fallback) gate on it.
    coastline_sea_present = False
    ####################
    [til_x_min, til_y_min] = GEO.wgs84_to_orthogrid(
        tile.lat + 1, tile.lon, tile.mask_zl
    )
    [til_x_max, til_y_max] = GEO.wgs84_to_orthogrid(
        tile.lat, tile.lon + 1, tile.mask_zl
    )
    for mesh_file_name in mesh_list:
        try:
            f_mesh = open(mesh_file_name, "r")
            UI.vprint(1, "   * ", mesh_file_name)
        except:
            UI.lvprint(
                1, "Mesh file ", mesh_file_name, " could not be read. Skipped."
            )
            continue
        mesh_version = float(f_mesh.readline().strip().split()[-1])
        has_water = 7 if mesh_version >= 1.3 else 3
        for i in range(3):
            f_mesh.readline()
        nbr_pt_in = int(f_mesh.readline())
        pt_in = numpy.zeros(5 * nbr_pt_in, "float")
        for i in range(0, nbr_pt_in):
            pt_in[5 * i : 5 * i + 3] = [
                float(x) for x in f_mesh.readline().split()[:3]
            ]
        for i in range(0, 3):
            f_mesh.readline()
        for i in range(0, nbr_pt_in):
            pt_in[5 * i + 3 : 5 * i + 5] = [
                float(x) for x in f_mesh.readline().split()[:2]
            ]
        for i in range(0, 2):  # skip 2 lines
            f_mesh.readline()
        nbr_tri_in = int(f_mesh.readline())  # read nbr of tris
        step_stones = max(1, nbr_tri_in // 100)
        percent = -1
        UI.vprint(
            2,
            " Attribution process of masks buffers to water triangles for "
            + str(mesh_file_name)
            + ".",
        )
        for i in range(0, nbr_tri_in):
            if i % step_stones == 0:
                percent += 1
                UI.progress_bar(1, int(percent * 5 / 10))
                if UI.red_flag:
                    UI.exit_message_and_bottom_line()
                    return 0
            (n1, n2, n3, tri_type) = [
                int(x) - 1 for x in f_mesh.readline().split()[:4]
            ]
            tri_type += 1
            water_bits = tri_type & has_water
            if (not water_bits) or (
                water_type_is_inland(water_bits)
                and not tile.use_masks_for_inland
            ):
                continue
            (lon1, lat1) = pt_in[5 * n1 : 5 * n1 + 2]
            (lon2, lat2) = pt_in[5 * n2 : 5 * n2 + 2]
            (lon3, lat3) = pt_in[5 * n3 : 5 * n3 + 2]
            bary_lat = (lat1 + lat2 + lat3) / 3
            bary_lon = (lon1 + lon2 + lon3) / 3
            (til_x, til_y) = GEO.wgs84_to_orthogrid(
                bary_lat, bary_lon, tile.mask_zl
            )
            if (
                til_x < til_x_min - 16
                or til_x > til_x_max + 16
                or til_y < til_y_min - 16
                or til_y > til_y_max + 16
            ):
                continue
            if (water_bits & 2) and not water_type_is_inland(water_bits):
                coastline_sea_present = True
            (til_x2, til_y2) = GEO.wgs84_to_orthogrid(
                bary_lat, bary_lon, tile.mask_zl + 2
            )
            a = (til_x2 // 16) % 4
            b = (til_y2 // 16) % 4
            if (til_x, til_y) in dico_sea:
                dico_sea[(til_x, til_y)].append(
                    (lat1, lon1, lat2, lon2, lat3, lon3)
                )
            else:
                dico_sea[(til_x, til_y)] = [
                    (lat1, lon1, lat2, lon2, lat3, lon3)
                ]
            if a == 0:
                if (til_x - 16, til_y) in dico_sea:
                    dico_sea[(til_x - 16, til_y)].append(
                        (lat1, lon1, lat2, lon2, lat3, lon3)
                    )
                else:
                    dico_sea[(til_x - 16, til_y)] = [
                        (lat1, lon1, lat2, lon2, lat3, lon3)
                    ]
                if b == 0:
                    if (til_x - 16, til_y - 16) in dico_sea:
                        dico_sea[(til_x - 16, til_y - 16)].append(
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        )
                    else:
                        dico_sea[(til_x - 16, til_y - 16)] = [
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        ]
                elif b == 3:
                    if (til_x - 16, til_y + 16) in dico_sea:
                        dico_sea[(til_x - 16, til_y + 16)].append(
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        )
                    else:
                        dico_sea[(til_x - 16, til_y + 16)] = [
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        ]
            elif a == 3:
                if (til_x + 16, til_y) in dico_sea:
                    dico_sea[(til_x + 16, til_y)].append(
                        (lat1, lon1, lat2, lon2, lat3, lon3)
                    )
                else:
                    dico_sea[(til_x + 16, til_y)] = [
                        (lat1, lon1, lat2, lon2, lat3, lon3)
                    ]
                if b == 0:
                    if (til_x + 16, til_y - 16) in dico_sea:
                        dico_sea[(til_x + 16, til_y - 16)].append(
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        )
                    else:
                        dico_sea[(til_x + 16, til_y - 16)] = [
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        ]
                elif b == 3:
                    if (til_x + 16, til_y + 16) in dico_sea:
                        dico_sea[(til_x + 16, til_y + 16)].append(
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        )
                    else:
                        dico_sea[(til_x + 16, til_y + 16)] = [
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        ]
            if b == 0:
                if (til_x, til_y - 16) in dico_sea:
                    dico_sea[(til_x, til_y - 16)].append(
                        (lat1, lon1, lat2, lon2, lat3, lon3)
                    )
                else:
                    dico_sea[(til_x, til_y - 16)] = [
                        (lat1, lon1, lat2, lon2, lat3, lon3)
                    ]
            elif b == 3:
                if (til_x, til_y + 16) in dico_sea:
                    dico_sea[(til_x, til_y + 16)].append(
                        (lat1, lon1, lat2, lon2, lat3, lon3)
                    )
                else:
                    dico_sea[(til_x, til_y + 16)] = [
                        (lat1, lon1, lat2, lon2, lat3, lon3)
                    ]
        f_mesh.close()
        if not tile.use_masks_for_inland:
            UI.vprint(2, "   Taking care of inland water near shoreline")
            f_mesh = open(mesh_file_name, "r")
            for i in range(0, 4):
                f_mesh.readline()
            nbr_pt_in = int(f_mesh.readline())
            for i in range(0, 2 * nbr_pt_in + 5):
                f_mesh.readline()
            nbr_tri_in = int(f_mesh.readline())  # read nbr of tris
            step_stones = max(1, nbr_tri_in // 100)
            percent = -1
            for i in range(0, nbr_tri_in):
                if i % step_stones == 0:
                    percent += 1
                    UI.progress_bar(1, int(percent * 5 / 10))
                    if UI.red_flag:
                        UI.exit_message_and_bottom_line()
                        return 0
                (n1, n2, n3, tri_type) = [
                    int(x) - 1 for x in f_mesh.readline().split()[:4]
                ]
                tri_type += 1
                if not water_type_is_inland(tri_type & has_water):
                    continue
                (lon1, lat1) = pt_in[5 * n1 : 5 * n1 + 2]
                (lon2, lat2) = pt_in[5 * n2 : 5 * n2 + 2]
                (lon3, lat3) = pt_in[5 * n3 : 5 * n3 + 2]
                bary_lat = (lat1 + lat2 + lat3) / 3
                bary_lon = (lon1 + lon2 + lon3) / 3
                (til_x, til_y) = GEO.wgs84_to_orthogrid(
                    bary_lat, bary_lon, tile.mask_zl
                )
                if (
                    til_x < til_x_min - 16
                    or til_x > til_x_max + 16
                    or til_y < til_y_min - 16
                    or til_y > til_y_max + 16
                ):
                    continue
                (til_x2, til_y2) = GEO.wgs84_to_orthogrid(
                    bary_lat, bary_lon, tile.mask_zl + 2
                )
                a = (til_x2 // 16) % 4
                b = (til_y2 // 16) % 4
                # Here an inland water tri is added ONLY if sea water tri were 
                # already added for this mask extent
                if (til_x, til_y) in dico_sea:
                    if (til_x, til_y) in dico_inland:
                        dico_inland[(til_x, til_y)].append(
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        )
                    else:
                        dico_inland[(til_x, til_y)] = [
                            (lat1, lon1, lat2, lon2, lat3, lon3)
                        ]
            f_mesh.close()
    
    return (dico_sea, dico_inland, coastline_sea_present)
################################################################################
        
################################################################################
def triangular_blur_along_axis(img_array, width, axis, lines_per_chunk=512):
    """Blur every line of ``img_array`` along ``axis`` with the triangular
    kernel [1, 2, .., width, .., 2, 1] / width**2, zero padding at the array
    boundary, floor-truncated to uint8 — the same output the legacy per-row
    ``numpy.convolve(line, kernel, "same")`` assignment produced, computed
    in O(n) per line instead of O(n * width).

    A triangular kernel is a box kernel convolved with itself, so two
    running-mean passes (``uniform_filter1d``) replace the convolution.
    The input is zero-padded by ``width`` along the filtered axis so the
    second pass sees the first pass's true out-of-bounds values (matching
    the one-shot convolution near the boundary); for even ``width`` the two
    passes use origins 0 and -1, whose half-sample offsets cancel.
    Lines are processed in chunks to bound the float64 working set."""
    output = numpy.empty(img_array.shape, dtype=numpy.uint8)
    second_pass_origin = -1 if width % 2 == 0 else 0
    padding = [(0, 0), (0, 0)]
    padding[axis] = (width, width)
    unpad = [slice(None), slice(None)]
    unpad[axis] = slice(width, -width)
    line_count = img_array.shape[1 - axis]
    for start in range(0, line_count, lines_per_chunk):
        chunk = [slice(None), slice(None)]
        chunk[1 - axis] = slice(start, start + lines_per_chunk)
        block = numpy.pad(
            img_array[tuple(chunk)].astype(numpy.float64), padding)
        block = uniform_filter1d(
            block, width, axis=axis, mode="constant", origin=0)
        block = uniform_filter1d(
            block, width, axis=axis, mode="constant",
            origin=second_pass_origin)
        output[tuple(chunk)] = block[tuple(unpad)]
    return output


def blur_mask(img_array, tile, sea_level):
    ##########################################
    def transition_profile(ratio, ttype):
        if ttype == "spline":
            return 3 * ratio ** 2 - 2 * ratio ** 3
        elif ttype == "linear":
            return ratio
        elif ttype == "parabolic":
            return 2 * ratio - ratio ** 2
    ##########################################
    pxscal = GEO.webmercator_pixel_size(tile.lat + 0.5, tile.mask_zl)
    if tile.masking_mode == "sand":
        blur_width = int(tile.masks_width / pxscal)
    elif tile.masking_mode == "rocks":
        blur_width = tile.masks_width / (2 * pxscal)
    elif tile.masking_mode == "3steps":
        blur_width = [L / pxscal for L in tile.masks_width]
    # Sand mode
    if tile.masking_mode == "sand" and blur_width:
        # convolution with a hat function (separable: rows then columns,
        # uint8 truncation between the two axes as in the original)
        b_img_array = triangular_blur_along_axis(img_array, blur_width, axis=1)
        b_img_array = triangular_blur_along_axis(b_img_array, blur_width, axis=0)
        b_img_array = 2 * numpy.minimum(b_img_array, 127)
        b_img_array = numpy.array(b_img_array, dtype=numpy.uint8)
    # Rocks mode
    elif tile.masking_mode == "rocks" and blur_width:
        # slight increase of the mask, then gaussian blur, nonlinear map and
        # a tiny bit of smoothing again on a short scale along the shore
        b_img_array = (
            numpy.array(
                Image.fromarray(img_array)
                .convert("L")
                .filter(ImageFilter.GaussianBlur(blur_width / 1.7)),
                dtype=numpy.uint8,
            )
            > 0
        ).astype(numpy.uint8) * 255
        # blur it
        b_img_array = numpy.array(
            Image.fromarray(b_img_array)
            .convert("L")
            .filter(ImageFilter.GaussianBlur(blur_width)),
            dtype=numpy.uint8,
        )
        # nonlinear transform to make the transition quicker at the shore 
        # (gaussian is too flat)
        gamma = 2.5
        b_img_array = (
            (
                (
                    numpy.tan(
                        (b_img_array.astype(numpy.float32) - 127.5)
                        / 128
                        * atan(3)
                    )
                    - numpy.tan(-127.5 / 128 * atan(3))
                )
                * 254
                / (2 * numpy.tan(127.5 / 128 * atan(3)))
            )
            ** gamma
            / (255 ** (gamma - 1))
        ).astype(numpy.uint8)
        # still some slight smoothing at the shore
        b_img_array = numpy.maximum(
            b_img_array,
            numpy.array(
                Image.fromarray(img_array)
                .convert("L")
                .filter(ImageFilter.GaussianBlur(2 ** (tile.mask_zl - 14))),
                dtype=numpy.uint8,
            ),
        )
    # 3 steps
    elif tile.masking_mode == "3steps":
        # why trying something so complicated...
        transin = blur_width[0]
        midzone = blur_width[1]
        transout = blur_width[2]
        shore_level = 255
        b_img_array = b_mask_array = numpy.array(img_array)
        # First the transition at the shore
        # We go from shore_level to sea_level in transin meters
        stepsin = int(transin / 3)
        for i in range(stepsin):
            value = shore_level + transition_profile(
                (i + 1) / stepsin, "parabolic"
            ) * (sea_level - shore_level)
            b_mask_array = (
                numpy.array(
                    Image.fromarray(b_mask_array)
                    .convert("L")
                    .filter(ImageFilter.GaussianBlur(1)),
                    dtype=numpy.uint8,
                )
                > 0
            ).astype(numpy.uint8) * 255
            b_img_array[(b_img_array == 0) * (b_mask_array != 0)] = value
            UI.vprint(2, value)
        # Next the intermediate zone at constant transparency
        sea_b_radius = midzone / 3
        sea_b_radius_buffered = (midzone + transout) / 3
        b_mask_array = (
            numpy.array(
                Image.fromarray(b_mask_array)
                .convert("L")
                .filter(ImageFilter.GaussianBlur(sea_b_radius_buffered)),
                dtype=numpy.uint8,
            )
            > 0
        ).astype(numpy.uint8) * 255
        b_mask_array = (
            numpy.array(
                Image.fromarray(b_mask_array)
                .convert("L")
                .filter(
                    ImageFilter.GaussianBlur(
                        sea_b_radius_buffered - sea_b_radius
                    )
                ),
                dtype=numpy.uint8,
            )
            == 255
        ).astype(numpy.uint8) * 255
        b_img_array[(b_img_array == 0) * (b_mask_array != 0)] = sea_level
        # Finally the transition to the X-Plane sea
        # We go from sea_level to 0 in transout meters
        stepsout = int(transout / 3)
        for i in range(stepsout):
            value = sea_level * (
                1 - transition_profile((i + 1) / stepsout, "linear")
            )
            b_mask_array = (
                numpy.array(
                    Image.fromarray(b_mask_array)
                    .convert("L")
                    .filter(ImageFilter.GaussianBlur(1)),
                    dtype=numpy.uint8,
                )
                > 0
            ).astype(numpy.uint8) * 255
            b_img_array[(b_img_array == 0) * (b_mask_array != 0)] = value
            UI.vprint(2, value)
        # To smoothen the thresolding introduced above we do a global short 
        # extent gaussian blur
        b_img_array = numpy.array(
            Image.fromarray(b_img_array)
            .convert("L")
            .filter(ImageFilter.GaussianBlur(2)),
            dtype=numpy.uint8,
        )
    else:
        # Just a (futile) copy
        b_img_array = numpy.array(img_array)

    return b_img_array
################################################################################

################################################################################
def triangulation_to_image(name, pixel_size, grid_size_or_bbox):
    f_node = open(name + ".1.node", "r")
    nbr_pt = int(f_node.readline().split()[0])
    vertices = numpy.zeros(2 * nbr_pt)
    for i in range(0, nbr_pt):
        # Triangle .node files have the node number in front
        vertices[2 * i : 2 * i + 2] = [
            float(x) for x in f_node.readline().split()[1:3]
        ]
    f_node.close()
    xmin = vertices[::2].min()
    xmax = vertices[::2].max()
    ymin = vertices[1::2].min()
    ymax = vertices[1::2].max()
    if isinstance(grid_size_or_bbox, tuple):  # bbox
        bbox = grid_size_or_bbox
        (xmin, ymin, xmax, ymax) = bbox
    else:  # float
        grid_size = grid_size_or_bbox
        xmin = floor((xmin - grid_size) / grid_size) * grid_size
        xmax = ceil((xmax + grid_size) / grid_size) * grid_size
        ymin = floor((ymin - grid_size) / grid_size) * grid_size
        ymax = ceil((ymax + grid_size) / grid_size) * grid_size
    mask_im = Image.new(
        "1", (int((xmax - xmin) / pixel_size), int((ymax - ymin) / pixel_size))
    )
    mask_draw = ImageDraw.Draw(mask_im)
    f_ele = open(name + ".1.ele", "r")
    nbr_tri = int(f_ele.readline().split()[0])
    for i in range(nbr_tri):
        (n1, n2, n3, tritype) = [
            int(x) - 1 for x in f_ele.readline().split()[1:5]
        ]
        tritype += 1
        if not tritype:
            continue
        (x1, y1) = vertices[2 * n1 : 2 * n1 + 2]
        (x2, y2) = vertices[2 * n2 : 2 * n2 + 2]
        (x3, y3) = vertices[2 * n3 : 2 * n3 + 2]
        (px1, py1) = [
            round((x1 - xmin) / pixel_size),
            round((y1 - ymin) / pixel_size),
        ]
        (px2, py2) = [
            round((x2 - xmin) / pixel_size),
            round((y2 - ymin) / pixel_size),
        ]
        (px3, py3) = [
            round((x3 - xmin) / pixel_size),
            round((y3 - ymin) / pixel_size),
        ]
        try:
            mask_draw.polygon(
                [(px1, py1), (px2, py2), (px3, py3)], fill="white"
            )
        except:
            pass
    f_ele.close()
    return ((xmin, ymin, xmax, ymax), ImageOps.flip(mask_im).convert("L"))


################################################################################

if __name__ == "__main__":
    UI.log = False
    UI.verbosity = 2
    Syntax = (
        'Syntax :\n',
        '--------\n',
        '(PYTHON) extent_code  pixel_size buffer_size blur_size [OSM query] [EPSG code] [bbox_or_grid_size]\n',
        'All three sizes in meters, buffer_size can be negative too.\n',
        'If OSM query is not used, data must be cached in an ',
        'extent_code.osm.bz2 file. EPSG code defaults to 4326, if it is used ',
        'the OSM query needs to be used too.\n\n',
        'Example :(from a subdirectory of Extents)\n',
        '---------\n',
        'python3 ../../src/O4_Mask_Utils.py Suisse  20 0 400 rel["admin_level"="2"]["name:fr"="Suisse"]'
    )
    nargs = len(sys.argv)
    if not nargs in (5, 6, 7, 8):
        print(Syntax)
        sys.exit(1)
    name = sys.argv[1]
    cached_file_name = name + ".osm.bz2"
    if nargs == 5 and not os.path.exists(cached_file_name):
        print(Syntax)
        sys.exit(1)
    if nargs in (6, 7, 8):
        query_tmp = sys.argv[5]
        query = ""
        for char in query_tmp:
            if char == "[":
                query += '["'
            elif char == "]":
                query += '"]'
            elif char in ["=", "~"]:
                query += '"' + char + '"'
            else:
                query += char
    else:
        query = None
    if nargs in (7, 8):
        epsg_code = sys.argv[6]
    else:
        epsg_code = "4326"
    if nargs == 8:
        grid_size_or_bbox = eval(sys.argv[7])
    else:
        grid_size_or_bbox = 0.02 if epsg_code == "4326" else 2000
    pixel_size = float(sys.argv[2])
    buffer_width = float(sys.argv[3]) / pixel_size
    mask_width = int(int(sys.argv[4]) / pixel_size)
    pixel_size = (
        pixel_size / 111120 if epsg_code == "4326" else pixel_size
    )  # assuming meters if not degrees
    vector_map = VECT.Vector_Map()
    osm_layer = OSM.OSM_layer()
    if not os.path.exists(cached_file_name):
        print("OSM query...")
        if not OSM.OSM_query_to_OSM_layer(
            query, "", osm_layer, "all", cached_file_name=cached_file_name
        ):
            print("OSM query failed. Exiting.")
            del vector_map
            time.sleep(1)
            sys.exit(0)
    else:
        print("Recycling OSM file...")
        osm_layer.update_dicosm(cached_file_name, None)
    print("Transform to multipolygon...")
    multipolygon_area = OSM.OSM_to_MultiPolygon(osm_layer, 0, 0)
    del osm_layer
    if not multipolygon_area.area:
        # try: os.remove(cached_file_name)
        # except: pass
        print(
            "Humm... an empty response. ",
            "Are you sure about the exact OSM tag for your region ?"
        )
        print("Exiting with no extent created.")
        del vector_map
        time.sleep(1)
        sys.exit(0)
    if epsg_code != "4326":
        name += "_" + epsg_code
        print("Changing coordinates to match EPSG code")
        import shapely.ops

        reprojection = GEO.transformer(4326, int(epsg_code))
        multipolygon_area = shapely.ops.transform(
            reprojection, multipolygon_area
        )

    vector_map.encode_MultiPolygon(
        multipolygon_area, VECT.dummy_alt, "WATER", check=True, cut=False
    )
    vector_map.write_node_file(name + ".node")
    vector_map.write_poly_file(name + ".poly")
    print("Triangulate...")
    MESH.triangulate(name, os.path.join(os.path.dirname(sys.argv[0]), ".."))
    ((xmin, ymin, xmax, ymax), mask_im) = triangulation_to_image(
        name, pixel_size, grid_size_or_bbox
    )
    print("Mask size : ", mask_im.size, "pixels.")
    buffer = ""
    try:
        f = open(name + ".ext", "r")
        for line in f.readlines():
            if ("#" not in line) or query:
                continue
            if "Initially" not in line:
                buffer += "# Initially c" + line[3:]
            else:
                buffer += line
        f.close()
    except:
        pass
    buffer += "# Created with : " + " ".join(sys.argv) + "\n"
    buffer += (
        "mask_bounds="
        + str(xmin)
        + ","
        + str(ymin)
        + ","
        + str(xmax)
        + ","
        + str(ymax)
        + "\n"
    )
    f = open(name + ".ext", "w")
    f.write(buffer)
    f.close()
    if buffer_width:
        UI.vprint(1, "Buffer of the mask...")
        mask_im = mask_im.filter(ImageFilter.GaussianBlur(buffer_width / 4))
        if buffer_width > 0:
            mask_im = Image.fromarray(
                (numpy.array(mask_im, dtype=numpy.uint8) > 0).astype(
                    numpy.uint8
                )
                * 255
            )
        else:  # buffer width can be negative
            mask_im = Image.fromarray(
                (numpy.array(mask_im, dtype=numpy.uint8) == 255).astype(
                    numpy.uint8
                )
                * 255
            )
    if mask_width:
        mask_width += 1
        UI.vprint(1, "Blur of the mask...")
        img_array = numpy.array(mask_im, dtype=numpy.uint8)
        kernel = numpy.ones(int(mask_width)) / int(mask_width)
        kernel = numpy.array(range(1, 2 * mask_width))
        kernel[mask_width:] = range(mask_width - 1, 0, -1)
        kernel = kernel / mask_width ** 2
        for i in range(0, len(img_array)):
            img_array[i] = numpy.convolve(img_array[i], kernel, "same")
        img_array = img_array.transpose()
        for i in range(0, len(img_array)):
            img_array[i] = numpy.convolve(img_array[i], kernel, "same")
        img_array = img_array.transpose()
        img_array[img_array >= 128] = 255
        img_array[img_array < 128] *= 2
        img_array = numpy.array(img_array, dtype=numpy.uint8)
        mask_im = Image.fromarray(img_array)
    mask_im.save(name + ".png")
    for f in [
        name + ".poly",
        name + ".node",
        name + ".1.node",
        name + ".1.ele",
    ]:
        try:
            os.remove(f)
        except:
            pass
    print("Done!")
################################################################################
