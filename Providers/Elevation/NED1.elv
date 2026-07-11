# United States Geological Survey national elevation dataset, 1 arc-second.
#
# Base-tier (tile-wide) definition using the existing staged-products URL
# scheme. Coverage is declared with coverage_bbox (rather than an Extents/
# polygon) deliberately: automatic selection judges coverage at the tile
# CENTRE, and a wrong automatic pick means a zero-altitude tile, so the
# box is kept to the CONTINENTAL United States where 1 arc-second
# availability is complete. Alaska / Hawaii / territories users can still
# select this source explicitly (explicit selection bypasses the
# coverage test, like the legacy keyword did).

role=base
access_strategy=usgs_seamless
legacy_keyword=NED1

download_url_template=https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/{dataset}/TIFF/current/{tile_identifier}/USGS_{dataset}_{tile_identifier}.tif
usgs_dataset=1

resolution_arc_seconds=1

# Continental United States (automatic-selection pre-filter; see above).
coverage_bbox=-125.1,24.4,-66.8,49.5

vertical_datum=NAVD88
license=Public Domain (U.S. Geological Survey)
attribution=U.S. Geological Survey

# Beats VIEWFINDER1 (60) where both cover.
priority=70
enabled=True
