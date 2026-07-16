# Toitu Te Whenua Land Information New Zealand (LINZ) lidar digital
# elevation model, 1 metre.
#
# Bare-earth 1 m tiles from the national lidar programme, published as
# anonymous Cloud-Optimized GeoTIFFs in the nz-elevation bucket on the
# Amazon Web Services Registry of Open Data (CC BY 4.0, no account).
# The bucket carries a STATIC STAC 1.0 catalog (plain JSON files, no
# search endpoint), so this definition uses the static_stac strategy:
# the catalog tree is walked once and memoised to
# Elevation_data/newzealand1m_static_stac_index.json.  Coverage is by
# survey campaign, not yet wall-to-wall -- uncovered airports fall
# back to the base tier via the all-nodata check.

role=airport_inset

access_strategy=static_stac

catalog_url=https://nz-elevation.s3-ap-southeast-2.amazonaws.com/catalog.json

# Index only the bare-earth collections (the bucket pairs each survey
# with a surface model under dsm_1m).
collection_filter=/dem_1m/

# Each item exposes a single Cloud-Optimized GeoTIFF asset (key
# "visual"); the finest-GeoTIFF fallback selects it, no preference
# tokens needed.
dtm_asset_keys=

# The tiles are LERC-compressed, a codec most GDAL builds ship
# without: whole tiles are downloaded and decoded through
# tifffile/imagecodecs instead of window-read.  New Zealand Transverse
# Mercator 2000 grid, -9999 nodata.
asset_compression=lerc
source_epsg=2193
source_nodata=-9999

# Native ground resolution of the source rasters, in metres.
native_resolution_m=1

# Cheap pre-filter: the main islands (the Chatham Islands sit across
# the antimeridian and are not representable in one box).
coverage_bbox=166.3,-47.4,178.6,-34.3

# New Zealand Vertical Datum 2016.
vertical_datum=NZVD2016
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Toitu Te Whenua Land Information New Zealand

priority=90

enabled=True
