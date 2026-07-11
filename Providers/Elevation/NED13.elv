# United States Geological Survey national elevation dataset, 1/3 arc-second.
#
# Base-tier (tile-wide) definition. Finer than the 1 arc-second working
# mesh grid, so the automatic base selection NEVER picks it (the auto cap
# excludes resolution_arc_seconds < 1 -- roughly 450 megabytes per tile
# for no mesh benefit); it remains selectable explicitly via
# base_elevation_source=NED13 or the legacy "NED1/3" keyword.

role=base
access_strategy=usgs_seamless
legacy_keyword=NED1/3

download_url_template=https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/{dataset}/TIFF/current/{tile_identifier}/USGS_{dataset}_{tile_identifier}.tif
usgs_dataset=13

resolution_arc_seconds=0.3333

coverage_bbox=-125.1,24.4,-66.8,49.5

vertical_datum=NAVD88
license=Public Domain (U.S. Geological Survey)
attribution=U.S. Geological Survey

priority=0
enabled=True
