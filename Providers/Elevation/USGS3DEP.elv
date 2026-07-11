# United States Geological Survey 3D Elevation Program (3DEP) 1 metre lidar.
#
# Declarative elevation-inset provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# Same comment and key=value syntax as the imagery Providers/<Region>/<CODE>.lay
# files. Unknown keys are preserved; edit freely.

# Detail tier (spec section 3.6): meter-class data fetched only inside
# airport bounding boxes. Tile-wide "base" sources are the Phase A2 refactor.
role=airport_inset

# Named fetch strategy implemented in code (strategy registry in the module).
access_strategy=tnm_cog

# The National Map (TNM) Access API product-discovery endpoint. The
# {west},{south},{east},{north} placeholders are substituted with the
# airport bounding box in EPSG:4326 (degrees) at discovery time.
discovery_url_template=https://tnmaccess.nationalmap.gov/api/v1/products?datasets=Digital Elevation Model (DEM) 1 meter&bbox={west},{south},{east},{north}&outputFormat=JSON

# Native ground resolution of the source rasters, in metres.
native_resolution_m=1

# Cheap pre-filter before the discovery request is issued; discovery is
# authoritative. Continental United States plus a generous buffer for
# Alaska/Hawaii/territories where 3DEP 1 metre coverage also exists.
coverage_bbox=-180.0,15.0,-64.0,72.0

# Vertical datum of the delivered elevations. NAVD88 is within ~1 m of
# EGM96 across the continental United States; the lidar is treated as
# truth and never shifted toward the base DEM.
vertical_datum=NAVD88

license=Public Domain (U.S. Geological Survey)
attribution=U.S. Geological Survey 3D Elevation Program (3DEP)

# Higher priority wins when several providers cover the same airport.
priority=100

enabled=True
