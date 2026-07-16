# Kartverket national height model (Nasjonal hoydemodell) terrain
# model, 1 metre.
#
# Bare-earth digital terrain model from the Norwegian national lidar
# programme (flown 2016-2022), covering the whole mainland, published
# as an OGC Web Coverage Service on Geonorge.  Declarative
# elevation-inset provider using the generic wcs access strategy:
# GDAL's WCS driver negotiates the protocol (this endpoint speaks WCS
# 1.0.0) and reads only the airport window from the national coverage.
# Svalbard is a separate programme (Norwegian Polar Institute) and is
# not included.

role=airport_inset

access_strategy=wcs

# The service endpoint, protocol version and coverage identifier as
# published in the WCS GetCapabilities (verified live 2026-07-15).
wcs_service_url=https://wms.geonorge.no/skwms1/wcs.hoyde-dtm-nhm-25833
wcs_version=1.0.0
wcs_coverage=nhm_dtm_topo_25833

# Native ground resolution of the source raster, in metres.
native_resolution_m=1

# Cheap pre-filter: mainland Norway.  Airports inside this box but
# outside the data extent come back as all-nodata windows, which the
# wcs strategy records as no-coverage.
coverage_bbox=4.0,57.9,31.5,71.3

# NN2000, the Norwegian national height datum.
vertical_datum=NN2000
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Kartverket, Nasjonal hoydemodell

priority=80

enabled=True
