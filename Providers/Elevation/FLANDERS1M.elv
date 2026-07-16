# Digitaal Hoogtemodel Vlaanderen II (Flanders, Belgium) digital
# terrain model, 1 metre.
#
# Bare-earth regional lidar, Flanders open data, served anonymously
# over the region's OGC Web Coverage Service (verified live
# 2026-07-16 at Brussels airport via GDAL's WCS driver -- EBBR lies
# in Flanders).  Heights are TAW (Tweede Algemene Waterpassing).

role=airport_inset
access_strategy=wcs

wcs_service_url=https://geo.api.vlaanderen.be/DHMV/wcs
wcs_version=2.0.1
wcs_coverage=DHMVII_DTM_1m

native_resolution_m=1
# Flanders (including Brussels' surroundings).
coverage_bbox=2.5,50.6,5.95,51.55

vertical_datum=TAW
license=Flanders open data (Vlaanderen)
attribution=Digitaal Vlaanderen, DHMV II

priority=85
enabled=True
