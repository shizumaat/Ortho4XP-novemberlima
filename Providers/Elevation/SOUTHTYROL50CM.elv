# Autonome Provinz Bozen - Suedtirol (South Tyrol, Italy) digital
# terrain model, 0.5 metre.
#
# Bare-earth lidar of the valley floors (the province-wide 2.5 m model
# covers the rest), PUBLIC DOMAIN (CC0), served anonymously over the
# province's OGC Web Coverage Service (verified live 2026-07-16 at
# Bolzano via GDAL's WCS driver).  The finest Alpine source in the
# registry alongside Switzerland's.

role=airport_inset
access_strategy=wcs

wcs_service_url=https://geoservices9.civis.bz.it/geoserver/ows
wcs_version=2.0.1
wcs_coverage=p_bz-Elevation__DigitalTerrainModel-0.5m

native_resolution_m=0.5
# South Tyrol.
coverage_bbox=10.4,46.2,12.5,47.1

vertical_datum=Orthometric (Italian height system)
license=Creative Commons Zero (public domain)
attribution=Autonome Provinz Bozen - Suedtirol

priority=95
enabled=True
