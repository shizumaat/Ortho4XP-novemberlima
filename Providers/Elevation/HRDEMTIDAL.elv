# Natural Resources Canada HRDEM (1 m LiDAR), INTERTIDAL BATHYMETRY twin.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial providers.
#
# This is the BATHYMETRY-role sibling of HRDEM.elv (the terrain-side
# definition of the very same NRCan High Resolution DEM STAC service). The
# topographic laser surveys the land, but Bay of Fundy campaigns -- flown at
# low tide over the world's largest tidal range -- carry the exposed
# INTERTIDAL mudflats as real negative heights below CGVD2013, giving
# measured depths near mean sea level for the depth-graded water masks
# (spec section 2). No sub-tidal bathymetry; open water reads as nodata.

# BATHYMETRY role: intertidal depth source, never terrain. Consumed only by
# O4_Bathymetry.ensure_bathymetry_band via select_bathymetry_*(); the
# terrain-selection helpers filter this role out.
role=bathymetry

# ---- Access keys copied VERBATIM from HRDEM.elv (same service) ----
access_strategy=stac
discovery_url_template=https://datacube.services.geo.ca/stac/api/search
collections=hrdem-lidar
dtm_asset_keys=dtm

# Native ground resolution of the source rasters, in metres.
native_resolution_m=1

# Coverage box copied from the parent (Canada-wide lidar). The claim is
# broad; intertidal depth exists only where a campaign was flown at low tide
# (Bay of Fundy verified). select_bathymetry_definitions() returns every
# covering provider priority-sorted and the band fetch falls through where a
# provider yields no cells, so this broad claim never starves the fallbacks.
coverage_bbox=-141.0,41.0,-52.0,84.0

# Vertical datum: CGVD2013, the Canadian geodetic vertical datum -- an
# MSL-family datum, appropriate for water rendering.
vertical_datum=CGVD2013

# Deep-ocean value floor for the post-warp sanitizer (spec section 2.3).
value_floor_m=-11100.0

license=Open Government Licence - Canada
attribution=Natural Resources Canada

# Below CUDEM (100), above the Allen Coral Atlas (80).
priority=95

enabled=True

# VERIFIED 2026-07-16 (empirical intertidal sweep, 10 m probe):
#   Window A -- Minas Basin flats, Bay of Fundy (-64.35,45.35,-64.30,45.38):
#     valid 28%, intertidal [-5,+0.5] = 26% of valid, band std 1.371 m
#     (strong real relief), band min -4.999 m, 54% of valid pixels below 0.
#   Window C -- open water mid-basin (-64.60,45.25,-64.55,45.28): 0 valid
#     pixels (all nodata) -- passes the zero-fill hard gate. (The originally
#     suggested C box -65.20,44.85 clipped Nova Scotia land, not open water.)
# Scope: INTERTIDAL ONLY -- exposed low-tide flats; no sub-tidal depths.
# Parent terrain-side definition of the same service: HRDEM.elv.

# Exposed-flats lidar: data stops at the waterline, so this source is a
# binary "flats" layer the OpenStreetMap shallow-water fallback matches
# for free.  Automatic paths skip it; only masks_use_DEM_too=True
# fetches it (for regions whose OSM tidal flats are unmapped).
intertidal=True
