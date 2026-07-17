# IGN France LiDAR HD (0.5 m), INTERTIDAL BATHYMETRY twin.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial providers.
#
# This is the BATHYMETRY-role sibling of FRANCE50CM.elv (the terrain-side
# definition of the very same IGN LiDAR HD service). France's topographic
# laser is flown to survey the land, but along the Atlantic coast the tiles
# were surveyed at low tide and carry the exposed INTERTIDAL flats as real
# negative heights below the NGF-IGN69 datum -- usable measured depths near
# mean sea level for the depth-graded water masks (spec section 2). The
# service carries NO sub-tidal bathymetry (topographic laser does not
# penetrate water); sub-tidal channels read as nodata, never as fill.

# BATHYMETRY role: seabed/intertidal depth source, never terrain. Consumed
# only by O4_Bathymetry.ensure_bathymetry_band via select_bathymetry_*();
# the terrain-selection helpers filter this role out.
role=bathymetry

# ---- Access keys copied VERBATIM from FRANCE50CM.elv (same service) ----
access_strategy=wfs_tile_index
wfs_service_url=https://data.geopf.fr/wfs/ows
wfs_type_name=IGNF_MNT-LIDAR-HD:dalle
url_property=url

# Native ground resolution of the source rasters, in metres.
native_resolution_m=0.5

# Coverage box copied from the parent (metropolitan France).
coverage_bbox=-5.2,41.3,9.6,51.1

# Vertical datum: NGF-IGN69, the French national height system referenced to
# the Marseille tide gauge mean sea level -- an MSL-family datum, appropriate
# for water rendering (depths are measured relative to a tidal surface).
vertical_datum=NGF-IGN69

# Deep-ocean value floor for the post-warp sanitizer (spec section 2.3), so
# genuine seabed depths are not discarded as leaked fill.
value_floor_m=-11100.0

license=Licence Ouverte / Etalab 2.0
attribution=IGN France, LiDAR HD

# Below CUDEM (100), above the Allen Coral Atlas (80).
priority=95

enabled=True

# VERIFIED 2026-07-16 (empirical intertidal sweep, 10 m probe):
#   Window A -- Arcachon Bay flats (-1.15,44.68,-1.10,44.71): valid 100%,
#     intertidal [-5,+0.5] = 100% of valid, band std 0.465 m (real relief,
#     not a constant), min -1.572 m, mean -0.685 m, 95% of pixels below 0.
#   Window C -- open water 3-10 km offshore (-1.60,44.60,-1.55,44.65):
#     NO COVERAGE (no tiles over open sea) -- passes the zero-fill hard gate.
#   (Mont-Saint-Michel returned NO COVERAGE and is not represented here.)
# Scope: INTERTIDAL ONLY -- exposed low-tide flats; no sub-tidal depths.
# Parent terrain-side definition of the same service: FRANCE50CM.elv.

# Exposed-flats lidar: data stops at the waterline, so this source is a
# binary "flats" layer the OpenStreetMap shallow-water fallback matches
# for free.  Automatic paths skip it; only masks_use_DEM_too=True
# fetches it (for regions whose OSM tidal flats are unmapped).
intertidal=True
