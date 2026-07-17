import os
import re
import shutil
import sys
from math import floor

import O4_UI_Utils as UI

g2xpl_16_prefix = ""
g2xpl_16_suffix = ""


def is_frozen_app():
    """True when running from a PyInstaller bundle (the packaged app)."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def resource_path(relative_path):
    """Absolute path to a READ-ONLY bundled resource (Providers, Extents,
    Filters, Utils, ...): inside the PyInstaller bundle when frozen, the
    checkout directory otherwise. Never use this for anything the app
    writes — writes inside a macOS .app bundle break its code signature,
    and a quarantined bundle may be mounted read-only."""
    if is_frozen_app():
        base_path = os.path.join(sys._MEIPASS, 'Ortho4XP_Data')
    else:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


# ---------------------------------------------------------------------------
# Writable data root: where downloads, caches, built tiles and config live.
#
# Running from a source checkout, this is the checkout directory itself, so
# nothing changes for developers. The packaged app instead asks the user on
# first launch and remembers the answer in a small per-user pointer file
# (the data itself lives wherever the user chose, possibly a big external
# drive; only the pointer lives under the home directory).
# ---------------------------------------------------------------------------

data_root_pointer_file = os.path.join(
    os.path.expanduser("~"), ".ortho4xp", "data_root.txt"
)


def default_data_root():
    """Default offered by the packaged app's first-launch folder chooser."""
    if sys.platform == "darwin":
        # "Next to the app" is unreliable on macOS (Gatekeeper translocation
        # runs freshly downloaded apps from a randomized read-only mount).
        return os.path.join(os.path.expanduser("~"), "Ortho4XP")
    # Windows / Linux: portable layout — data folders next to the executable.
    return os.path.dirname(os.path.abspath(sys.executable))


def read_data_root_pointer():
    """The remembered data-root choice, or None if never chosen."""
    try:
        with open(data_root_pointer_file, "r", encoding="utf-8") as f:
            path = f.read().strip()
        return path or None
    except OSError:
        return None


def write_data_root_pointer(path):
    """Remember the user's data-root choice for future launches."""
    os.makedirs(os.path.dirname(data_root_pointer_file), exist_ok=True)
    with open(data_root_pointer_file, "w", encoding="utf-8") as f:
        f.write(os.path.abspath(path) + "\n")


def resolve_data_root():
    """Precedence: ORTHO4XP_DATA_ROOT environment variable, then (packaged
    app only) the remembered first-launch choice, then the default."""
    env_root = os.environ.get("ORTHO4XP_DATA_ROOT")
    if env_root:
        return os.path.abspath(env_root)
    if is_frozen_app():
        pointed = read_data_root_pointer()
        if pointed:
            return pointed
        return default_data_root()
    return os.path.abspath(".")


# Explicit choice made at runtime (the packaged app's first-launch chooser);
# None means "resolve on each use", which keeps the legacy source-checkout
# behavior of following the current working directory at call time.
_data_root_override = None


def current_data_root():
    if _data_root_override is not None:
        return _data_root_override
    return resolve_data_root()


def data_path(relative_path):
    """Absolute path for WRITABLE app data (downloads, tiles, caches,
    config, logs). Everything lands under the user-chosen data root."""
    return os.path.join(current_data_root(), relative_path)


def airport_index_cache():
    """The offline airport search index (O4_Airport_Index TSV cache).

    Built by the map window from X-Plane's Global Airports apt.dat;
    also read by the bathymetry band's airport-radius gate."""
    return data_path(".airport_index.tsv")


# Read-only, shipped with the app.
Provider_dir = resource_path("Providers")
Extent_dir = resource_path("Extents")
Filter_dir = resource_path("Filters")
Utils_dir = resource_path("Utils")

# Writable, live under the data root (values assigned by _apply_data_root).
# Extents/Filters/Providers are read-only EXCEPT the generated layer-mask
# cache, which gets its own writable home below.
Auto_extent_dir = ""
Preview_dir = ""
OSM_dir = ""
Mask_dir = ""
Imagery_dir = ""
Elevation_dir = ""
Geotiff_dir = ""
Patch_dir = ""
Tile_dir = ""
Tmp_dir = ""
Overlay_dir = ""
# DSFTool text dumps of default Global Scenery DSFs (used by the
# default-landclass texture modes).  Writable cache — lives under the data
# root so we never write into the X-Plane install or a scenery pack.
Default_dsf_cache_dir = ""


def _apply_data_root():
    global Auto_extent_dir, Preview_dir, OSM_dir, Mask_dir, Imagery_dir
    global Elevation_dir, Geotiff_dir, Patch_dir, Tile_dir, Tmp_dir
    global Overlay_dir, Default_dsf_cache_dir
    Auto_extent_dir = data_path(os.path.join("Extents", "Auto"))
    Preview_dir = data_path("Previews")
    OSM_dir = data_path("OSM_data")
    Mask_dir = data_path("Masks")
    Imagery_dir = data_path("Orthophotos")
    Elevation_dir = data_path("Elevation_data")
    Geotiff_dir = data_path("Geotiffs")
    Patch_dir = data_path("Patches")
    Tile_dir = data_path("Tiles")
    Tmp_dir = data_path("tmp")
    Overlay_dir = data_path("yOrtho4XP_Overlays")
    Default_dsf_cache_dir = data_path("Default_DSF_cache")


_apply_data_root()


def set_data_root(path):
    """Point all writable directories at a new data root.

    Must be called before any module that captures these paths at import
    time (O4_Config_Utils, the GUIs) is imported — the packaged app's
    first-launch chooser runs before those imports for exactly this reason.
    """
    global _data_root_override
    _data_root_override = os.path.abspath(path)
    _apply_data_root()


def seed_shipped_patches():
    """Copy the patches shipped with the app into a data root that has no
    Patches folder yet. A data root that already has one — e.g. the user
    selected their existing Ortho4XP folder — is left completely untouched."""
    shipped = resource_path("Patches")
    if os.path.normpath(shipped) == os.path.normpath(Patch_dir):
        return
    if os.path.isdir(Patch_dir) or not os.path.isdir(shipped):
        return
    shutil.copytree(shipped, Patch_dir)

##############################################################################
def short_latlon(lat, lon):
    strlat = "{:+.0f}".format(lat).zfill(3)
    strlon = "{:+.0f}".format(lon).zfill(4)
    return strlat + strlon


def round_latlon(lat, lon):
    strlatround = "{:+.0f}".format(floor(lat / 10) * 10).zfill(3)
    strlonround = "{:+.0f}".format(floor(lon / 10) * 10).zfill(4)
    return strlatround + strlonround


def long_latlon(lat, lon):
    strlat = "{:+.0f}".format(lat).zfill(3)
    strlon = "{:+.0f}".format(lon).zfill(4)
    strlatround = "{:+.0f}".format(floor(lat / 10) * 10).zfill(3)
    strlonround = "{:+.0f}".format(floor(lon / 10) * 10).zfill(4)
    return os.path.join(strlatround + strlonround, strlat + strlon)


def hem_latlon(lat, lon):
    hemisphere = "N" if lat >= 0 else "S"
    greenwichside = "E" if lon >= 0 else "W"
    return (
        hemisphere
        + "{:.0f}".format(abs(lat)).zfill(2)
        + greenwichside
        + "{:.0f}".format(abs(lon)).zfill(3)
    )


##############################################################################
def tile_dir(lat, lon):
    return "zOrtho4XP_" + short_latlon(lat, lon)


def build_dir(lat, lon, custom_build_dir):
    if not custom_build_dir:
        return os.path.join(Tile_dir, tile_dir(lat, lon))
    elif custom_build_dir.endswith(("/", "\\")):
        return os.path.join(custom_build_dir[:-1], tile_dir(lat, lon))
    else:
        return custom_build_dir


def normalize_custom_build_dir(lat: int, lon: int,
                               custom_build_dir: str) -> str:
    """Return ``custom_build_dir`` in the form ``Tile.__init__`` expects.

    ``Tile`` treats a non-empty ``custom_build_dir`` WITHOUT a trailing
    separator as ``grouped=True``: the path is used verbatim as the build
    directory and every 3x3-neighbor lookup (``select_neighbor_meshes``,
    ``record_water_tris``) then searches that SAME directory for neighbor
    meshes.  Headless callers naturally pass the tile's own build
    directory (".../zOrtho4XP_+36-008"), which flips them into grouped
    mode and silently loses all cross-tile neighbor data — mask seams at
    every tile edge.  The intended per-tile-subdirectory mode is the
    PARENT directory with a trailing separator (what the Qt GUI passes).

    Normalization rules:

    * empty stays empty (default ``Tiles/`` layout);
    * a path whose last component is exactly ``tile_dir(lat, lon)``
      (with or without a trailing separator) is rewritten to its parent
      with a trailing ``os.sep``;
    * a path whose last component names a DIFFERENT tile's
      ``zOrtho4XP_+XX+YYY`` directory raises ``ValueError`` — that is
      always a caller mix-up, never a grouped directory name;
    * anything else is returned unchanged (intentional grouped mode, or
      an already-correct parent directory with trailing separator).
    """
    if not custom_build_dir:
        return custom_build_dir
    stripped = custom_build_dir.rstrip("/\\")
    basename = os.path.basename(stripped)
    if basename == tile_dir(lat, lon):
        parent = os.path.dirname(stripped)
        return (parent if parent else ".") + os.sep
    if re.fullmatch(r"zOrtho4XP_[+-]\d{2}[+-]\d{3}", basename):
        raise ValueError(
            "custom_build_dir %r is the build directory of tile %s, not "
            "of the requested tile %s (%s); pass the parent directory "
            "with a trailing separator, or the requested tile's own "
            "directory" % (
                custom_build_dir, basename[len("zOrtho4XP_"):],
                short_latlon(lat, lon), tile_dir(lat, lon),
            )
        )
    return custom_build_dir


def osm_dir(lat, lon):
    return os.path.join(OSM_dir, long_latlon(lat, lon))


def mask_dir(lat, lon):
    return os.path.join(Mask_dir, long_latlon(lat, lon))


def patch_dir(lat, lon):
    return os.path.join(Patch_dir, long_latlon(lat, lon))


def input_node_file(tile):
    if tile.iterate:
        return os.path.join(
            tile.build_dir,
            "Data"
            + short_latlon(tile.lat, tile.lon)
            + "."
            + str(tile.iterate)
            + ".node",
        )
    else:
        return os.path.join(
            tile.build_dir, "Data" + short_latlon(tile.lat, tile.lon) + ".node"
        )


def input_poly_file(tile):
    if tile.iterate:
        return os.path.join(
            tile.build_dir,
            "Data"
            + short_latlon(tile.lat, tile.lon)
            + "."
            + str(tile.iterate)
            + ".poly",
        )
    else:
        return os.path.join(
            tile.build_dir, "Data" + short_latlon(tile.lat, tile.lon) + ".poly"
        )


def input_ele_file(tile):
    if tile.iterate:
        return os.path.join(
            tile.build_dir,
            "Data"
            + short_latlon(tile.lat, tile.lon)
            + "."
            + str(tile.iterate)
            + ".ele",
        )
    else:
        return os.path.join(
            tile.build_dir, "Data" + short_latlon(tile.lat, tile.lon) + ".ele"
        )


def output_node_file(tile):
    return os.path.join(
        tile.build_dir,
        "Data"
        + short_latlon(tile.lat, tile.lon)
        + "."
        + str(tile.iterate + 1)
        + ".node",
    )


def output_poly_file(tile):
    return os.path.join(
        tile.build_dir,
        "Data"
        + short_latlon(tile.lat, tile.lon)
        + "."
        + str(tile.iterate + 1)
        + ".poly",
    )


def output_ele_file(tile):
    return os.path.join(
        tile.build_dir,
        "Data"
        + short_latlon(tile.lat, tile.lon)
        + "."
        + str(tile.iterate + 1)
        + ".ele",
    )


def alt_file(tile):
    if tile.iterate:
        return os.path.join(
            tile.build_dir,
            "Data"
            + short_latlon(tile.lat, tile.lon)
            + "."
            + str(tile.iterate)
            + ".alt",
        )
    else:
        return os.path.join(
            tile.build_dir, "Data" + short_latlon(tile.lat, tile.lon) + ".alt"
        )


def apt_file(tile):
    return os.path.join(
        tile.build_dir, "Data" + short_latlon(tile.lat, tile.lon) + ".apt"
    )


def weight_file(tile):
    return os.path.join(
        tile.build_dir, "Data" + short_latlon(tile.lat, tile.lon) + ".weight"
    )


def mesh_file(build_dir, lat, lon):
    return os.path.join(build_dir, "Data" + short_latlon(lat, lon) + ".mesh")


def dsf_file(build_dir, lat, lon):
    return os.path.join(
        build_dir, "Earth nav data", long_latlon(lat, lon) + ".dsf"
    )


def obj_file(til_x_left, til_y_top, zoomlevel, provider_code):
    return os.path.join(
        Geotiff_dir,
        str(til_y_top)
        + "_"
        + str(til_x_left)
        + "_"
        + provider_code
        + str(zoomlevel)
        + ".obj",
    )


def mtl_file(til_x_left, til_y_top, zoomlevel, provider_code):
    return os.path.join(
        Geotiff_dir,
        str(til_y_top)
        + "_"
        + str(til_x_left)
        + "_"
        + provider_code
        + str(zoomlevel)
        + ".mtl",
    )


##############################################################################

##############################################################################
def preview(lat, lon, zoomlevel, provider_code):
    return os.path.join(
        Preview_dir,
        short_latlon(lat, lon) + "_" + provider_code + str(zoomlevel) + ".jpg",
    )


##############################################################################

##############################################################################
def custom_coastline(lat, lon):
    return os.path.join(
        OSM_dir,
        long_latlon(lat, lon),
        short_latlon(lat, lon) + "_custom_coastline.osm.bz2",
    )


def custom_coastline_dir(lat, lon):
    return os.path.join(OSM_dir, long_latlon(lat, lon), "custom_coastline")


def custom_water(lat, lon):
    return os.path.join(
        OSM_dir,
        long_latlon(lat, lon),
        short_latlon(lat, lon) + "_custom_water.osm.bz2",
    )


def custom_water_dir(lat, lon):
    return os.path.join(OSM_dir, long_latlon(lat, lon), "custom_water")


def osm_cached(lat, lon, cached_suffix):
    return os.path.join(
        OSM_dir,
        long_latlon(lat, lon),
        short_latlon(lat, lon) + "_" + cached_suffix + ".osm.bz2",
    )


def osm_old_cached(lat, lon, query):
    subtags = query.split('"')
    return os.path.join(
        OSM_dir,
        long_latlon(lat, lon),
        short_latlon(lat, lon)
        + "_"
        + subtags[0][0:-1]
        + "_"
        + subtags[1]
        + "_"
        + subtags[3]
        + ".osm",
    )


##############################################################################
def base_file_name(lat, lon):
    return os.path.join(
        Elevation_dir, round_latlon(lat, lon), hem_latlon(lat, lon)
    )


##############################################################################

##############################################################################
def elevation_data(source, lat, lon):
    if source == "View":
        return base_file_name(lat, lon) + ".hgt"
    elif source == "SRTM":
        return base_file_name(lat, lon) + "_SRTMv3.hgt"
    elif source == "ALOS":
        return base_file_name(lat, lon) + "_ALOS3W30.tif"
    elif source == "NED1/3":
        return base_file_name(lat, lon) + "_NED13.tif"
    elif source == "NED1":
        return base_file_name(lat, lon) + "_NED1.tif"
    elif source == "SONNY1":
        return base_file_name(lat, lon) + "_SONNY1.hgt"
##############################################################################

##############################################################################
def airport_inset_directory(lat, lon):
    """Directory holding the fetched airport elevation insets for a tile.

    Lives under the same ``Elevation_data/<block>/`` tree as every other
    cached elevation artefact, e.g.
    ``Elevation_data/+30-090/N36W087_airport_insets/``.
    """
    return os.path.join(
        Elevation_dir,
        round_latlon(lat, lon),
        hem_latlon(lat, lon) + "_airport_insets",
    )


def airport_inset_index(lat, lon):
    """The per-tile discovery index (including negative results)."""
    return os.path.join(airport_inset_directory(lat, lon), "index.json")


def airport_inset_dem(lat, lon, icao, provider_code):
    """The warped EPSG:4326 float32 GeoTIFF for one airport and provider.

    ``provider_code`` is the lower-cased ``.elv`` definition code so cache
    keys survive access-strategy refactors, e.g. ``KBNA_usgs3dep.tif``.
    """
    return os.path.join(
        airport_inset_directory(lat, lon),
        icao + "_" + provider_code.lower() + ".tif",
    )


def airport_inset_provenance(lat, lon, icao, provider_code):
    """The provenance sidecar accompanying an airport inset GeoTIFF."""
    return os.path.join(
        airport_inset_directory(lat, lon),
        icao + "_" + provider_code.lower() + ".json",
    )


def tile_overlay_directory(lat, lon):
    """Directory holding the tile-wide elevation-level overlays for a tile.

    Sibling of the airport-inset cache, e.g.
    ``Elevation_data/+30-090/N36W087_tile_overlay/``.
    """
    return os.path.join(
        Elevation_dir,
        round_latlon(lat, lon),
        hem_latlon(lat, lon) + "_tile_overlay",
    )


def tile_overlay_index(lat, lon):
    """The per-tile overlay discovery index (including negative results)."""
    return os.path.join(tile_overlay_directory(lat, lon), "index.json")


def _tile_overlay_stem(provider_code, target_resolution_m):
    """Cache stem keyed by provider and warp resolution, e.g.
    ``usgs3dep_10.29m`` — changing the elevation level changes the target
    resolution and therefore the cache key."""
    resolution_token = ("%.2f" % float(target_resolution_m)).rstrip(
        "0"
    ).rstrip(".")
    return provider_code.lower() + "_" + resolution_token + "m"


def tile_overlay_dem(lat, lon, provider_code, target_resolution_m):
    """The warped EPSG:4326 float32 GeoTIFF covering the whole tile."""
    return os.path.join(
        tile_overlay_directory(lat, lon),
        _tile_overlay_stem(provider_code, target_resolution_m) + ".tif",
    )


def tile_overlay_provenance(lat, lon, provider_code, target_resolution_m):
    """The provenance sidecar accompanying a tile-wide overlay GeoTIFF."""
    return os.path.join(
        tile_overlay_directory(lat, lon),
        _tile_overlay_stem(provider_code, target_resolution_m) + ".json",
    )


def coastline_band_directory(lat, lon):
    """Directory holding the coastline lidar band cells for a tile, e.g.
    ``Elevation_data/+30-090/N36W087_coastline_band/``."""
    return os.path.join(
        Elevation_dir,
        round_latlon(lat, lon),
        hem_latlon(lat, lon) + "_coastline_band",
    )


def coastline_band_index(lat, lon):
    """The band stamp: chosen grid factor + per-cell fetch outcomes."""
    return os.path.join(coastline_band_directory(lat, lon), "index.json")


def coastline_band_cell_dem(
    lat, lon, cell_column, cell_row, provider_code, target_resolution_m
):
    """One warped band cell, keyed by cell indices, provider and warp
    resolution, e.g. ``cell_03_07_usgs3dep_10.29m.tif``."""
    return os.path.join(
        coastline_band_directory(lat, lon),
        "cell_%02d_%02d_" % (cell_column, cell_row)
        + _tile_overlay_stem(provider_code, target_resolution_m)
        + ".tif",
    )


def coastline_band_vrt(lat, lon, provider_code):
    """The virtual mosaic of every fetched band cell (the bake input)."""
    return os.path.join(
        coastline_band_directory(lat, lon),
        "band_" + provider_code.lower() + ".vrt",
    )


def bathymetry_band_directory(lat, lon):
    """Directory holding the coastal bathymetry band cells for a tile, e.g.
    ``Elevation_data/+20-160/N21W160_bathymetry_band/``."""
    return os.path.join(
        Elevation_dir,
        round_latlon(lat, lon),
        hem_latlon(lat, lon) + "_bathymetry_band",
    )


def bathymetry_band_index(lat, lon):
    """The bathymetry band stamp: provider + per-cell fetch outcomes."""
    return os.path.join(bathymetry_band_directory(lat, lon), "index.json")


def bathymetry_band_cell(
    lat, lon, cell_column, cell_row, provider_code, target_resolution_m
):
    """One warped bathymetry band cell, keyed like the coastline band's,
    e.g. ``cell_03_07_cudemhawaii_10.0m.tif``."""
    return os.path.join(
        bathymetry_band_directory(lat, lon),
        "cell_%02d_%02d_" % (cell_column, cell_row)
        + _tile_overlay_stem(provider_code, target_resolution_m)
        + ".tif",
    )


def bathymetry_band_vrt(lat, lon, provider_code):
    """The virtual mosaic of every fetched bathymetry band cell."""
    return os.path.join(
        bathymetry_band_directory(lat, lon),
        "band_" + provider_code.lower() + ".vrt",
    )


def inset_water(lat, lon):
    """The per-tile airport-inset water supplement: hydro-flat basins
    detected in the lidar insets, written as an OSM fragment and merged
    ADDITIVELY into the water layer by ``include_water`` (it never
    replaces the Overpass/custom water — unlike ``custom_water``).
    Lives beside the inset GeoTIFFs it is derived from, so cache
    invalidation follows the rasters."""
    return os.path.join(
        airport_inset_directory(lat, lon),
        short_latlon(lat, lon) + "_inset_water.osm.bz2",
    )


##############################################################################

##############################################################################
def generic_tif(lat, lon):
    return base_file_name(lat, lon) + ".tif"


##############################################################################

##############################################################################
def viewfinderpanorama(lat, lon):
    return base_file_name(lat, lon) + ".hgt"


##############################################################################

##############################################################################
def SRTM_1sec(lat, lon):
    return base_file_name(lat, lon) + "_SRTM_1sec.hgt"


##############################################################################

##############################################################################
def legacy_mask(m_til_x_left, m_til_y_top):
    return str(m_til_y_top) + "_" + str(m_til_x_left) + ".png"

def distance_mask(m_til_x_left, m_til_y_top):
    return str(m_til_y_top) + "_" + str(m_til_x_left) + "_dist.png"


def mask_file(til_x_left, til_y_top, zoomlevel, provider_code):
    return (
        str(til_y_top) + "_" + str(til_x_left) + "_ZL" + str(zoomlevel) + ".png"
    )


def airport_fade_mask_name(til_x_left, til_y_top, zoomlevel, provider_code):
    """Grayscale fade mask accompanying an orthophoto texture tile in
    ``airport_ortho`` texture mode (see ``docs/specs/texture-mode-spec.md``).

    Georeferenced identically to the DDS
    (``dds_file_name_from_attributes``) it fades; the ``_airport_fade`` suffix
    keeps it distinct from the sea/distance masks (``mask_file`` /
    ``distance_mask``), which share the same texture directory.
    """
    return (
        str(til_y_top)
        + "_"
        + str(til_x_left)
        + "_"
        + provider_code
        + str(zoomlevel)
        + "_airport_fade.png"
    )


##############################################################################

##############################################################################
def jpeg_file_name_from_attributes(
    til_x_left, til_y_top, zoomlevel, provider_code
):
    if provider_code == "g2xpl_16":
        file_name = (
            g2xpl_16_prefix
            + str(zoomlevel)
            + "_"
            + str(til_x_left)
            + "_"
            + str(2 ** zoomlevel - 16 - til_y_top)
            + g2xpl_16_suffix
            + ".jpg"
        )
    else:
        file_name = (
            str(til_y_top)
            + "_"
            + str(til_x_left)
            + "_"
            + provider_code
            + str(zoomlevel)
            + ".jpg"
        )
    return file_name


##############################################################################

##############################################################################
def jpeg_file_dir_from_attributes(lat, lon, zoomlevel, provider):
    if not provider:
        file_dir = "."
    elif provider["imagery_dir"] == "normal":
        file_dir = os.path.join(
            Imagery_dir,
            short_latlon(lat, lon),
            provider["code"] + "_" + str(zoomlevel),
        )
    elif provider["imagery_dir"] == "grouped":
        file_dir = os.path.join(
            Imagery_dir,
            long_latlon(lat, lon),
            provider["code"] + "_" + str(zoomlevel),
        )
    elif provider["imagery_dir"] == "code":
        file_dir = os.path.join(
            Imagery_dir,
            provider["code"],
            provider["code"] + "_" + str(zoomlevel),
        )
    else:
        file_dir = os.path.join(
            Imagery_dir,
            provider["imagery_dir"],
            provider["code"] + "_" + str(zoomlevel),
        )
    return file_dir


##############################################################################

##############################################################################
def dds_file_name_from_attributes(
    til_x_left, til_y_top, zoomlevel, provider_code, file_ext="dds"
):
    if provider_code == "g2xpl_16":
        file_name = (
            g2xpl_16_prefix
            + str(zoomlevel)
            + "_"
            + str(til_x_left)
            + "_"
            + str(2 ** zoomlevel - 16 - til_y_top)
            + g2xpl_16_suffix
            + "."
            + file_ext
        )
    else:
        file_name = (
            str(til_y_top)
            + "_"
            + str(til_x_left)
            + "_"
            + provider_code
            + str(zoomlevel)
            + "."
            + file_ext
        )
    return file_name


##############################################################################

##############################################################################
def geotiff_file_name_from_attributes(
    til_x_left, til_y_top, zoomlevel, provider_code
):
    return (
        str(til_y_top)
        + "_"
        + str(til_x_left)
        + "_"
        + provider_code
        + str(zoomlevel)
        + "-WGS84.tif"
    )


##############################################################################
