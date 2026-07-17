# NOAA NCEI Continuously Updated DEM (CUDEM), ninth arc-second American Samoa
# topobathymetric tiles.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial providers
# (USGS3DEP.elv, HRDEM.elv). Unknown keys are preserved; edit freely.

# BATHYMETRY role (spec section 2.1): this is a SEABED-DEPTH source, not a
# terrain source. It is the coastal analogue of the airport-inset pattern --
# measured depth fetched only along a tile's coastline -- consumed by
# O4_Bathymetry.ensure_bathymetry_band for the depth-graded water masks and
# the X-Plane 12 sea_level raster. select_bathymetry_definition() is its ONLY
# entry point; the terrain-selection helpers (airport insets, base sources,
# the elevation_level wide-area overlay) filter this role out.
role=bathymetry

# Named fetch strategy implemented in code (strategy registry in the module).
# The NCEI catalogs are static STAC 1.0 JSON on public object storage with no
# /search API -- discovery walks the catalog. Like the other NCEI ninth
# arc-second datasets, this catalog links its tiles directly from the root
# (rel="item", no child collections); the static_stac strategy synthesizes one
# pseudo-collection whose bounding box is coverage_bbox below.
access_strategy=static_stac

# Root STAC catalog for the NCEI ninth arc-second topobathy American Samoa
# dataset (7 root items, verified live 2026-07-16).
catalog_url=https://noaa-nos-coastal-lidar-pds.s3.amazonaws.com/dem/NCEI_ninth_Topobathy_AmSam_9460/stac/catalog.json

# Native ground resolution of the source rasters, in metres. Ninth arc-second
# is ~3.4 m at these latitudes.
native_resolution_m=3.4

# Cheap pre-filter and, for a root-item catalog, the synthesized
# pseudo-collection's bounding box. Derived from the union of the 7 catalog
# item bboxes padded by 0.1 degree (raw union -171.00,-14.50,-169.25,-14.00;
# verified live 2026-07-16). Covers Tutuila and the Manu'a group with their
# shelves, south of the equator.
coverage_bbox=-171.1,-14.6,-169.15,-13.9

# Vertical datum. CUDEM American Samoa is referenced to a LOCAL TIDAL datum
# (approximately mean sea level); it carries NO NAVD88 orthometric height.
# This is exactly why role=bathymetry must never feed terrain grading: its
# topo side (positive "land" values) is untrustworthy against the base DEM,
# and the values here are depths below a tidal surface, not orthometric
# elevations. The depths are used only for water rendering, never for the
# .alt raster the mesher reads.
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
