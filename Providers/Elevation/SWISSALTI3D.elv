# swisstopo swissALTI3D, 0.5 metre lidar terrain model.
#
# The Swiss federal bare-earth digital terrain model ("without
# vegetation and development"; the surface model is the separate
# swissSURFACE3D product), covering Switzerland and Liechtenstein.
# Served through swisstopo's STAC API as anonymous Cloud-Optimized
# GeoTIFF assets -- the same shape as HRDEM, so this is a pure
# definition on the existing stac strategy (verified live 2026-07-15).

role=airport_inset

access_strategy=stac

# The STAC API search endpoint (v1; the older /api/stac/v0.9 item
# path also exists but may deprecate).
discovery_url_template=https://data.geo.admin.ch/api/stac/v1/search

# STAC collection to search.
collections=ch.swisstopo.swissalti3d

# Assets are filename-keyed (no "dtm" key), each carrying its own
# eo:gsd; the strategy's finest-GeoTIFF fallback picks the 0.5 m
# asset over the 2 m one.

# Native ground resolution of the finest source rasters, in metres.
native_resolution_m=0.5

# Cheap pre-filter: Switzerland and Liechtenstein.
coverage_bbox=5.9,45.7,10.6,47.9

# Swiss national levelling network heights (LN02).
vertical_datum=LN02

license=swisstopo open geodata (free use with source attribution)
attribution=swisstopo, swissALTI3D

priority=100

enabled=True
