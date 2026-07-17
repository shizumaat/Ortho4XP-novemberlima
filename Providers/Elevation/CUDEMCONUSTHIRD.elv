# NOAA NCEI Continuously Updated DEM (CUDEM), THIRD arc-second topobathy
# for the CONtiguous United States coasts -- the broader gap filler beneath
# the ninth arc-second release (CUDEMCONUS).
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
# Like CUDEMCONUS, this release publishes NO STAC catalog -- only a plain-text
# list of Cloud-Optimized GeoTIFF URLs whose filenames encode the tile
# location (ncei13_<lat>_<lon>_<year>.tif). The coordinate_named_url_list
# strategy fetches that list once, parses each filename into a bounding box,
# memoises the index under Elevation_data/, and serves windowed /vsicurl reads.
access_strategy=coordinate_named_url_list

# The authoritative tile index: one Cloud-Optimized GeoTIFF URL per line.
url_list_url=https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/NCEI_third_Topobathy_2014_8580/urllist8580.txt

# Native ground resolution of the source rasters, in metres. Third arc-second
# is ~10 m at these latitudes -- coarser than the ninth, but broader coverage.
native_resolution_m=10.0

# Cheap pre-filter ONLY -- the parsed URL-list index is authoritative for
# which tiles actually exist. Same CONUS coastal footprint as the ninth.
coverage_bbox=-126.0,23.0,-64.0,50.0

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

# Lower priority than the ninth arc-second CONUS release (priority 100): where
# both cover a tile the finer ninth wins; the third arc-second fills the gaps
# beneath it.
priority=90

enabled=True
