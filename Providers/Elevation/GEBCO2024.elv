# GEBCO 2024 global bathymetry / topography grid.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial direct_cog provider
# (WALES1M.elv). Unknown keys are preserved; edit freely.

# BATHYMETRY role (spec section 2.1): this is a SEABED-DEPTH source, not a
# terrain source. It is consumed only by O4_Bathymetry.ensure_bathymetry_band
# for the depth-graded water masks and the X-Plane 12 sea_level raster.
# select_bathymetry_definition() is its ONLY entry point; the terrain-selection
# helpers (airport insets, base sources, the elevation_level wide-area overlay)
# filter this role out. This is the GLOBAL LAST-RESORT depth source: the
# regional CUDEM lidar (priority 100) and the European EMODnet DTM (priority
# 60) both outrank it wherever they cover a tile.
role=bathymetry

# The whole GEBCO grid is published as ONE global Cloud-Optimized GeoTIFF, so
# the direct_cog strategy window-reads the tile's coastal bounding box straight
# out of it via /vsicurl/ (no per-item discovery, no catalog).
access_strategy=direct_cog

# Global GEBCO 2024 15 arc-second grid as a single COG. This is a THIRD-PARTY
# MIRROR (Source Cooperative / alexgleith) of the official GEBCO 2024 release;
# the data are identical to the GEBCO Compilation Group (2024) grid, only the
# hosting differs. Verified live 2026-07-16: HEAD returns 200 and GDAL opens it
# as an 86400 x 43200 single-band COG with 8 overview levels, geotransform
# origin (-180, 90), 15 arc-second (~450 m) pixels.
cog_urls=https://data.source.coop/alexgleith/gebco-2024/GEBCO_2024.tif

# Native ground resolution of the source raster, in metres. 15 arc-second is
# ~450 m at the equator.
native_resolution_m=450

# Cheap pre-filter: the whole planet, minus the polar caps the grid does not
# extend to at full 15 arc-second (the COG spans latitudes to +/-90, but the
# bathymetry band only ever samples near coastlines).
coverage_bbox=-180.0,-85.0,180.0,85.0

# Vertical datum. GEBCO is referenced to MEAN SEA LEVEL and uses the ELEVATION
# sign convention: values are NEGATIVE below sea level (positive on land), so
# the value_floor_m below and the water test in the mask consumer (v <= 0 is
# water) both apply directly. As a tidal/MSL global model it must never feed
# terrain grading -- role=bathymetry keeps it out of every terrain selector.
vertical_datum=MSL

# Lowest value the post-warp sanitizer treats as genuine data. The default
# terrestrial floor (-600 m) would discard real seabed depths; this floor must
# admit FULL OCEAN DEPTH, so it sits below the deepest ocean (Challenger Deep
# ~ -10935 m) to preserve genuine abyssal and hadal depths.
value_floor_m=-11100.0

license=GEBCO grid, freely available (cite GEBCO Compilation Group 2024)
attribution=GEBCO Compilation Group (2024); hosted via Source Cooperative mirror

# Global last resort: below regional CUDEM (100) and European EMODnet (60).
priority=10

enabled=True
