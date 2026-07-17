# LVermGeo Schleswig-Holstein DGM1 (1 m LiDAR), INTERTIDAL BATHYMETRY twin.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial providers.
#
# This is the BATHYMETRY-role sibling of SCHLESWIGHOLSTEIN1M.elv (the
# terrain-side definition of the very same Schleswig-Holstein DGM1 mass
# download). The topographic laser surveys the land, but the North Frisian
# Wadden Sea tiles flown at low tide carry the exposed INTERTIDAL flats as
# real negative heights below the DHHN2016 datum, giving measured depths
# near mean sea level for the depth-graded water masks (spec section 2). No
# sub-tidal bathymetry; open water has no tiles (nodata), never fill.

# BATHYMETRY role: intertidal depth source, never terrain. Consumed only by
# O4_Bathymetry.ensure_bathymetry_band via select_bathymetry_*(); the
# terrain-selection helpers filter this role out.
role=bathymetry

# ---- Access keys copied VERBATIM from SCHLESWIGHOLSTEIN1M.elv (same service) ----
access_strategy=tile_grid_http
tile_url_template={file_name}
index_url=https://geodaten.schleswig-holstein.de/gaialight-sh/_apps/dladownload/single.php?file=DGM1_SH__Massendownload.geojson&id=4
fetch_mode=download
download_suffix=.xyz
tile_size_km=1
source_epsg=25832
warp_source_epsg=25832

# Native ground resolution of the source rasters, in metres.
native_resolution_m=1

# Coverage box copied from the parent (Schleswig-Holstein).
coverage_bbox=7.8,53.3,11.4,55.1

# Vertical datum: DHHN2016, the German national height datum -- normal
# heights tied to the Amsterdam Ordnance Datum (NAP), i.e. an MSL-family
# datum, appropriate for water rendering.
vertical_datum=DHHN2016

# Deep-ocean value floor for the post-warp sanitizer (spec section 2.3).
value_floor_m=-11100.0

license=Datenlizenz Deutschland Zero 2.0 (no restrictions)
attribution=GeoBasis-DE/LVermGeo SH

# Below CUDEM (100), above the Allen Coral Atlas (80).
priority=95

enabled=True

# VERIFIED 2026-07-16 (empirical intertidal sweep, 10 m probe):
#   Window A -- Husum Wadden flats (8.75,54.42,8.80,54.45): valid 100%,
#     intertidal [-5,+0.5] = 73% of valid, band std 0.636 m (strong real
#     relief), band min -1.616 m, 60% of valid pixels below 0.
#   Window C -- open water offshore (7.90,54.35,7.95,54.40): NO COVERAGE --
#     passes the zero-fill hard gate.
# Scope: INTERTIDAL ONLY -- exposed low-tide flats; no sub-tidal depths.
# Parent terrain-side definition of the same service: SCHLESWIGHOLSTEIN1M.elv.

# Exposed-flats lidar: data stops at the waterline, so this source is a
# binary "flats" layer the OpenStreetMap shallow-water fallback matches
# for free.  Automatic paths skip it; only masks_use_DEM_too=True
# fetches it (for regions whose OSM tidal flats are unmapped).
intertidal=True
