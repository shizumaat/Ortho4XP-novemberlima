# Environment Agency LIDAR Composite Digital Terrain Model, 1 metre.
#
# Bare-earth lidar composite covering ~99% of England (Scotland, Wales
# and Northern Ireland run separate programmes and are NOT included),
# published as an OGC Web Coverage Service on the Defra spatial-data
# platform.  Declarative elevation-inset provider using the generic wcs
# access strategy: GDAL's WCS driver negotiates the protocol and reads
# only the airport window from the national coverage.

# Detail tier: meter-class data fetched only inside airport bounding
# boxes.
role=airport_inset

access_strategy=wcs

# The service endpoint, protocol version and coverage identifier as
# published in the WCS GetCapabilities (verified live 2026-07-15).
wcs_service_url=https://environment.data.gov.uk/spatialdata/lidar-composite-digital-terrain-model-dtm-1m/wcs
wcs_version=2.0.1
wcs_coverage=13787b9a-26a4-4775-8523-806d13af58fc__Lidar_Composite_Elevation_DTM_1m

# Native ground resolution of the source raster, in metres.
native_resolution_m=1

# Cheap pre-filter: England including the Isles of Scilly, stopping at
# the Scottish border.  Airports inside this box but outside the
# English data (the Welsh borders, southern Scotland) come back as
# all-nodata windows, which the wcs strategy records as no-coverage.
coverage_bbox=-6.5,49.8,1.9,55.9

# Ordnance Datum Newlyn, the British mainland height datum.
vertical_datum=ODN
license=Open Government Licence v3 (attribution required)
attribution=Environment Agency, LIDAR Composite DTM 1m, Crown copyright

# Never overlaps the North-American inset providers; the value only
# documents intent alongside USGS3DEP (100) and HRDEM (90).
priority=80

enabled=True
