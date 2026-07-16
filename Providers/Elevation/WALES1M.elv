# DataMapWales lidar digital terrain model, 1 metre.
#
# The Welsh Government's bare-earth lidar composite, published as ONE
# country-wide Cloud-Optimized GeoTIFF on Azure blob storage with no
# registration -- the direct_cog strategy window-reads the airport
# bounding box straight out of it (verified live 2026-07-15).  Licence
# is the Open Government Licence v3.

role=airport_inset

access_strategy=direct_cog

# The 32-bit country-wide Cloud-Optimized GeoTIFF (float metres).
cog_urls=https://dmwproductionblob.blob.core.windows.net/cogs/lidar/wales_dtm_32bit_cog.tif

# Native ground resolution of the source raster, in metres.
native_resolution_m=1

# Cheap pre-filter: Wales.  Overlaps the ENGLAND1M box along the
# border; the higher priority below means Welsh-side airports try this
# source first and border airports fall through cleanly on the
# all-nodata check.
coverage_bbox=-5.5,51.3,-2.6,53.5

# Ordnance Datum Newlyn, like the England composite.
vertical_datum=ODN
license=Open Government Licence v3 (attribution required)
attribution=Welsh Government / DataMapWales, LiDAR terrain model

# Above ENGLAND1M (80): the boxes overlap along the border and the
# Welsh data is the right first try inside Wales.
priority=85

enabled=True
