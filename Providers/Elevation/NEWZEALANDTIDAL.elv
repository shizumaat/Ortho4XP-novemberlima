# Toitu Te Whenua LINZ 1 m LiDAR, INTERTIDAL BATHYMETRY twin.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial providers.
#
# This is the BATHYMETRY-role sibling of NEWZEALAND1M.elv (the terrain-side
# definition of the very same LINZ open-elevation service). The topographic
# laser surveys the land, but the harbour tiles were flown at low tide and
# carry the exposed INTERTIDAL flats as real negative heights below NZVD2016
# -- measured depths near mean sea level for the depth-graded water masks
# (spec section 2). No sub-tidal bathymetry (topographic laser does not
# penetrate water); sub-tidal channels read as nodata, never as fill.

# BATHYMETRY role: intertidal depth source, never terrain. Consumed only by
# O4_Bathymetry.ensure_bathymetry_band via select_bathymetry_*(); the
# terrain-selection helpers filter this role out.
role=bathymetry

# ---- Access keys copied VERBATIM from NEWZEALAND1M.elv (same service) ----
access_strategy=static_stac
catalog_url=https://nz-elevation.s3-ap-southeast-2.amazonaws.com/catalog.json
collection_filter=/dem_1m/
dtm_asset_keys=
asset_compression=lerc
source_epsg=2193
source_nodata=-9999

# Native ground resolution of the source rasters, in metres.
native_resolution_m=1

# Coverage box copied from the parent (New Zealand).
coverage_bbox=166.3,-47.4,178.6,-34.3

# Vertical datum: NZVD2016, the New Zealand vertical datum -- an MSL-family
# datum, appropriate for water rendering.
vertical_datum=NZVD2016

# Deep-ocean value floor for the post-warp sanitizer (spec section 2.3).
value_floor_m=-11100.0

license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Toitu Te Whenua Land Information New Zealand

# Below CUDEM (100), above the Allen Coral Atlas (80).
priority=95

enabled=True

# VERIFIED 2026-07-16 (empirical intertidal sweep, 10 m probe):
#   Window A -- Manukau Harbour flats (174.70,-37.03,174.75,-37.00):
#     valid 38%, intertidal [-5,+0.5] = 100% of valid, band std 0.546 m
#     (real relief), min -1.949 m, mean -1.073 m, 96% of pixels below 0.
#   Window C -- open water 3-10 km offshore (174.20,-37.10,174.25,-37.05):
#     NO COVERAGE -- passes the zero-fill hard gate.
# Scope: INTERTIDAL ONLY -- exposed low-tide flats; no sub-tidal depths.
# Parent terrain-side definition of the same service: NEWZEALAND1M.elv.

# Exposed-flats lidar: data stops at the waterline, so this source is a
# binary "flats" layer the OpenStreetMap shallow-water fallback matches
# for free.  Automatic paths skip it; only masks_use_DEM_too=True
# fetches it (for regions whose OSM tidal flats are unmapped).
intertidal=True
