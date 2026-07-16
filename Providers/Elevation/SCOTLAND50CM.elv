# Scottish Remote Sensing Portal lidar digital terrain model,
# 0.5-1 metre by campaign.
#
# The national lidar programme's bare-earth tiles (Open Government
# Licence v3) on the anonymous srsp-open-data bucket: six campaign
# phases at 1 m or 50 cm plus the rolling national programme at
# 50 cm, named by their Ordnance Survey grid squares.  The
# os_grid_bucket strategy paginates the listings once, computes each
# square's footprint arithmetically, and window-reads intersecting
# tiles; where campaigns overlap the finest wins (verified live
# 2026-07-16 at Edinburgh and Glasgow).  Coverage is campaign-based,
# not wall-to-wall -- uncovered airports fall to the 30 m mainland
# provider, then the base tier.

role=airport_inset

access_strategy=os_grid_bucket

bucket_url=https://srsp-open-data.s3.eu-west-2.amazonaws.com
bucket_prefixes=lidar/phase-1/dtm/,lidar/phase-2/dtm/,lidar/phase-3/dtm/,lidar/phase-4/dtm/,lidar/phase-5/dtm/,lidar/phase-6/dtm/,lidar/national-lidar-programme/dtm/,lidar/outer-hebrides/dtm/,lidar/orkney-islands-council-23/dtm/

# Native ground resolution: 0.5 m in the newer campaigns, 1 m in
# phases 1-2.
native_resolution_m=0.5

# Scotland INCLUDING Orkney, Fair Isle and Shetland (the bucket's
# HU/HY/HP squares -- phase 2 covers Shetland at 1 m).
coverage_bbox=-7.7,54.6,-0.6,61.0

vertical_datum=ODN
license=Open Government Licence v3
attribution=Scottish Remote Sensing Portal (Scottish Government)

priority=90

enabled=True
