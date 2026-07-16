# Glowny Urzad Geodezji i Kartografii (Poland) national digital
# terrain model (NMT GRID1), 1 metre.
#
# Bare-earth national terrain model from the Polish lidar programme,
# served anonymously (no key, no fees, no access constraints) through
# the geoportal's OGC Web Coverage Service.  Reuse is free under the
# Polish geodesy statute (open by law rather than a named Creative
# Commons tag).  Uses the generic wcs access strategy.

role=airport_inset

access_strategy=wcs

wcs_service_url=https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF
wcs_version=2.0.1
# Kronstadt-1986 normal-height coverage (a PL-EVRF2007-NH variant also
# exists on the same endpoint).
wcs_coverage=DTM_PL-KRON86-NH_TIFF

# Native ground resolution of the source raster, in metres.
native_resolution_m=1

# Cheap pre-filter: Poland.
coverage_bbox=14.1,49.0,24.2,55.0

vertical_datum=PL-KRON86-NH
license=Open by statute (Polish geodetic and cartographic law)
attribution=Glowny Urzad Geodezji i Kartografii (GUGiK), NMT

priority=80

enabled=True
