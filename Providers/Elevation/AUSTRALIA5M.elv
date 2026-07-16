# Geoscience Australia lidar digital elevation model, 5 metre.
#
# Bare-earth 5 m composite of ~236 lidar surveys (2001-2015, about
# 245,000 square kilometres: the populated coasts, Murray-Darling
# floodplains and town surveys -- NOT continental), served anonymously
# over an OGC Web Coverage Service on services.ga.gov.au.  Uses the
# generic wcs access strategy; GDAL's WCS driver handles this ArcGIS
# endpoint at protocol version 1.0.0 (verified live 2026-07-15).
# Airports outside the survey patchwork come back as all-nodata
# windows, which the strategy records as no-coverage -- they keep the
# base tier.

role=airport_inset

access_strategy=wcs

wcs_service_url=https://services.ga.gov.au/gis/services/DEM_LiDAR_5m_2025/MapServer/WCSServer
wcs_version=1.0.0
wcs_coverage=1

# Native ground resolution of the source raster, in metres.
native_resolution_m=5

# Cheap pre-filter: continental Australia and Tasmania.
coverage_bbox=112.9,-43.7,153.7,-9.0

# Australian Height Datum (via AUSGeoid); horizontal GDA94.
vertical_datum=AHD
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Commonwealth of Australia (Geoscience Australia)

priority=80

enabled=True
