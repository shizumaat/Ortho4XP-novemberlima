# LGLN Niedersachsen DGM1 (1 m LiDAR), INTERTIDAL BATHYMETRY twin.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial providers.
#
# This is the BATHYMETRY-role sibling of LOWERSAXONY1M.elv (the terrain-side
# definition of the very same LGLN Lower Saxony DGM1 STAC service). The
# topographic laser surveys the land, but the East Frisian Wadden Sea tiles
# flown at low tide carry the exposed INTERTIDAL flats as real negative
# heights below the DHHN2016 datum, giving measured depths near mean sea
# level for the depth-graded water masks (spec section 2). No sub-tidal
# bathymetry; open water has no tiles (nodata), never fill.

# BATHYMETRY role: intertidal depth source, never terrain. Consumed only by
# O4_Bathymetry.ensure_bathymetry_band via select_bathymetry_*(); the
# terrain-selection helpers filter this role out.
role=bathymetry

# ---- Access keys copied VERBATIM from LOWERSAXONY1M.elv (same service) ----
access_strategy=stac
discovery_url_template=https://dgm.stac.lgln.niedersachsen.de/search
collections=dgm1
dtm_asset_keys=dgm1-tif

# Native ground resolution of the source rasters, in metres.
native_resolution_m=1

# Coverage box copied from the parent (Lower Saxony).
coverage_bbox=6.6,51.2,11.6,54.0

# Vertical datum: DHHN2016, the German national height datum -- normal
# heights tied to the Amsterdam Ordnance Datum (NAP), i.e. an MSL-family
# datum, appropriate for water rendering.
vertical_datum=DHHN2016

# Deep-ocean value floor for the post-warp sanitizer (spec section 2.3).
value_floor_m=-11100.0

license=Datenlizenz Deutschland Zero 2.0 (no restrictions)
attribution=LGLN Niedersachsen

# Below CUDEM (100), above the Allen Coral Atlas (80).
priority=95

enabled=True

# VERIFIED 2026-07-16 (empirical intertidal sweep, 10 m probe):
#   Window A -- Norddeich Wadden flats (7.15,53.63,7.20,53.66): valid 40%,
#     intertidal [-5,+0.5] = 33% of valid, band std 0.226 m (real relief),
#     band min -1.219 m. Genuine exposed-flat structure, no zero-fill.
#   Window C -- open water offshore (6.50,53.75,6.55,53.80): NO COVERAGE --
#     passes the zero-fill hard gate.
# Scope: INTERTIDAL ONLY -- exposed low-tide flats; no sub-tidal depths.
# Parent terrain-side definition of the same service: LOWERSAXONY1M.elv.

# Exposed-flats lidar: data stops at the waterline, so this source is a
# binary "flats" layer the OpenStreetMap shallow-water fallback matches
# for free.  Automatic paths skip it; only masks_use_DEM_too=True
# fetches it (for regions whose OSM tidal flats are unmapped).
intertidal=True
