# Regione Autonoma della Sardegna (Sardinia, Italy) digital terrain
# model mosaic, 1 metre.
#
# Bare-earth island-wide lidar mosaic, CC BY, served anonymously over
# the region's OGC Web Coverage Service (verified live 2026-07-16 at
# Cagliari via GDAL's WCS driver).

role=airport_inset
access_strategy=wcs

wcs_service_url=http://webgis2.regione.sardegna.it/geoserverraster/ows
wcs_version=2.0.1
wcs_coverage=raster__DTM_1M_MOSAICO_ALTIMETRIA

native_resolution_m=1
# Sardinia.
coverage_bbox=8.1,38.8,9.9,41.3

vertical_datum=Orthometric (Italian height system)
license=Creative Commons Attribution (Regione Sardegna open data)
attribution=Regione Autonoma della Sardegna

priority=90
enabled=True
