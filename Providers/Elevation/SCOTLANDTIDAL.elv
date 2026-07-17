# Scottish Remote Sensing Portal 0.5 m LiDAR, INTERTIDAL BATHYMETRY twin.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial providers.
#
# This is the BATHYMETRY-role sibling of SCOTLAND50CM.elv (the terrain-side
# definition of the very same Scottish Government open-lidar S3 bucket). The
# topographic laser surveys the land, but Solway Firth tiles flown at low
# tide carry the exposed INTERTIDAL flats and saltmarsh as heights around
# and below the ODN datum, giving measured shoreline depths for the
# depth-graded water masks (spec section 2). No sub-tidal bathymetry; open
# water has no tiles (nodata), never fill.

# BATHYMETRY role: intertidal depth source, never terrain. Consumed only by
# O4_Bathymetry.ensure_bathymetry_band via select_bathymetry_*(); the
# terrain-selection helpers filter this role out.
role=bathymetry

# ---- Access keys copied VERBATIM from SCOTLAND50CM.elv (same service) ----
access_strategy=os_grid_bucket
bucket_url=https://srsp-open-data.s3.eu-west-2.amazonaws.com
bucket_prefixes=lidar/phase-1/dtm/,lidar/phase-2/dtm/,lidar/phase-3/dtm/,lidar/phase-4/dtm/,lidar/phase-5/dtm/,lidar/phase-6/dtm/,lidar/national-lidar-programme/dtm/,lidar/outer-hebrides/dtm/,lidar/orkney-islands-council-23/dtm/

# Native ground resolution of the source rasters, in metres.
native_resolution_m=0.5

# Coverage box copied from the parent (Scotland, reaching Shetland/Orkney).
coverage_bbox=-7.7,54.6,-0.6,61.0

# Vertical datum: ODN (Ordnance Datum Newlyn), the British mainland height
# datum referenced to mean sea level at Newlyn -- an MSL-family datum,
# appropriate for water rendering.
vertical_datum=ODN

# Deep-ocean value floor for the post-warp sanitizer (spec section 2.3).
value_floor_m=-11100.0

license=Open Government Licence v3
attribution=Scottish Remote Sensing Portal (Scottish Government)

# Below CUDEM (100), above the Allen Coral Atlas (80).
priority=95

enabled=True

# VERIFIED 2026-07-16 (empirical intertidal sweep, 10 m probe):
#   Window A -- Solway Firth flats (-3.58,54.94,-3.52,54.97): valid 86%,
#     intertidal [-5,+0.5] = 24% of valid, band std 0.174 m (real relief,
#     just above the 0.15 m floor), band min -0.193 m. Mostly low
#     marsh/flats; genuine near-datum shoreline structure, no zero-fill.
#   Window C -- open water, North Sea (-1.50,56.00,-1.45,56.03): NO COVERAGE
#     -- passes the zero-fill hard gate. (The originally suggested offshore
#     box -3.70,54.85 clipped coastal hills to 179 m, not open water.)
# Scope: INTERTIDAL ONLY -- exposed low-tide flats; no sub-tidal depths.
# Parent terrain-side definition of the same service: SCOTLAND50CM.elv.

# Exposed-flats lidar: data stops at the waterline, so this source is a
# binary "flats" layer the OpenStreetMap shallow-water fallback matches
# for free.  Automatic paths skip it; only masks_use_DEM_too=True
# fetches it (for regions whose OSM tidal flats are unmapped).
intertidal=True
