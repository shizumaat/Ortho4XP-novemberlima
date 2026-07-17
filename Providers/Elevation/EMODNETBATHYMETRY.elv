# EMODnet Bathymetry Digital Terrain Model (DTM), European seas.
#
# Declarative elevation provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the terrestrial WCS providers
# (ITALY10M.elv, NORWAY1M.elv). Unknown keys are preserved; edit freely.

# BATHYMETRY role (spec section 2.1): this is a SEABED-DEPTH source, not a
# terrain source. It is consumed only by O4_Bathymetry.ensure_bathymetry_band
# for the depth-graded water masks and the X-Plane 12 sea_level raster.
# select_bathymetry_definition() is its ONLY entry point; the terrain-selection
# helpers (airport insets, base sources, the elevation_level wide-area overlay)
# filter this role out. Regional CUDEM lidar (priority 100) outranks this
# broad DTM (priority 60) wherever both cover a tile.
role=bathymetry

# EMODnet publishes the harmonized European DTM over an OGC Web Coverage
# Service. GDAL's WCS driver does the protocol work (version negotiation,
# DescribeCoverage, windowed GetCoverage) exactly as for the terrestrial WCS
# terrain providers, so the fetch core is the same warp.
access_strategy=wcs

# WCS endpoint, negotiated version, and the coverage id. Note the DOUBLE
# underscore in the WCS 2.0.1 CoverageId (emodnet__mean, from GetCapabilities)
# -- not the emodnet:mean layer name used by the WMS. The module builds the
# GDAL descriptor "WCS:<url>?version=<v>&coverage=<coverage>".
wcs_service_url=https://ows.emodnet-bathymetry.eu/wcs
wcs_version=2.0.1
wcs_coverage=emodnet__mean

# Native ground resolution, in metres. The DTM is published at 1/16 arc-minute
# (~115 m at mid-European latitudes).
native_resolution_m=115

# Cheap pre-filter: the EMODnet DTM extent (European seas, the Arctic to the
# Canaries / Mediterranean / Black Sea). GetCapabilities reports a
# 108960 x 75840 grid over roughly this box.
coverage_bbox=-36.0,15.0,43.0,90.0

# Vertical datum. EMODnet mean depth is referenced approximately to Lowest
# Astronomical Tide (LAT) / mean sea level, NOT to an orthometric height
# system -- another reason role=bathymetry must never feed terrain grading.
# The raster uses the ELEVATION sign convention: values are NEGATIVE below sea
# level (positive on land), so the value_floor_m below and the water test in
# the mask consumer (v <= 0 is water) both apply directly.
vertical_datum=LAT / approx MSL

# Lowest value the post-warp sanitizer treats as genuine data. The default
# terrestrial floor (-600 m) would discard real seabed depths, so bathymetry
# lowers it below the deepest ocean (Challenger Deep ~ -10935 m).
value_floor_m=-11100.0

license=CC-BY (EMODnet Bathymetry Consortium)
attribution=EMODnet Bathymetry Consortium, EMODnet Digital Bathymetry (DTM)

# Below the regional CUDEM lidar (100); above the global GEBCO fallback (10).
priority=60

# Live verification 2026-07-16: WCS 2.0.1 GetCoverage for
# subset=Lat(39.5,39.6)&subset=Long(12.5,12.6) (deep Tyrrhenian Sea) returned
# a valid GeoTIFF window with depths of -3610 .. -3586 m (negative below sea
# level), confirming the coverage id, axis labels (Lat Long) and sign
# convention. A land-side window (east Sardinia) returned positive elevations,
# as expected for an elevation-convention DTM.
enabled=True
