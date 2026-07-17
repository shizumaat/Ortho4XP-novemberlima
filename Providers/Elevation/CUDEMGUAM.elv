# NOAA NCEI Continuously Updated DEM (CUDEM), ninth arc-second topobathy
# for Guam.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial providers
# (USGS3DEP.elv, HRDEM.elv). Unknown keys are preserved; edit freely.

# BATHYMETRY role (spec section 2.1): a SEABED-DEPTH source, not terrain.
# The coastal analogue of the airport-inset pattern -- measured depth fetched
# only along a tile's coastline -- consumed by ensure_bathymetry_band for the
# depth-graded water masks and the X-Plane 12 sea_level raster.
# select_bathymetry_definition() is its ONLY entry point; the terrain paths
# (airport insets, base sources, the elevation_level overlay) filter it out.
role=bathymetry

# Named fetch strategy implemented in code (strategy registry in the module).
# Guam publishes NO STAC catalog -- only a plain-text list of Cloud-Optimized
# GeoTIFF URLs whose filenames encode the tile location. Guam sits in the
# EASTERN hemisphere, so the longitude token uses 'e' (e.g.
# ncei19_n13x25_e144x50_2022v1.tif -> west edge +144.50). The
# coordinate_named_url_list strategy handles both hemispheres.
access_strategy=coordinate_named_url_list

# The authoritative tile index: one Cloud-Optimized GeoTIFF URL per line.
url_list_url=https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/NCEI_ninth_Topobathy_Guam_9462/urllist9462.txt

# Native ground resolution of the source rasters, in metres. Ninth arc-second
# is ~3.4 m at these latitudes.
native_resolution_m=3.4

# Cheap pre-filter ONLY -- the parsed URL-list index is authoritative for
# which tiles actually exist. Derived by parsing the live Guam URL list
# (4 tiles spanning 144.50..145.00 E, 13.00..13.75 N) and padding 0.1 deg.
coverage_bbox=144.40,12.90,145.10,13.85

# Vertical datum. CUDEM is referenced to a LOCAL TIDAL datum (approximately
# mean sea level); it carries NO NAVD88 orthometric height. This is exactly
# why role=bathymetry must never feed terrain grading -- the depths are used
# only for water rendering, never for the .alt raster the mesher reads.
vertical_datum=Local Tidal (approx MSL)

# Lowest value the post-warp sanitizer treats as genuine data. The default
# terrestrial floor (-600 m) would discard real seabed depths, so bathymetry
# lowers it below the deepest ocean (Challenger Deep ~ -10935 m).
value_floor_m=-11100.0

license=Public Domain (NOAA NCEI)
attribution=NOAA NCEI Continuously Updated DEM (CUDEM)

# Higher priority wins when several bathymetry providers cover the same tile.
priority=100

enabled=True
