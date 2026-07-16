# GeoBasis Mecklenburg-Vorpommern (Germany) digital terrain model
# DGM1, 1 metre.
#
# Bare-earth model, CC BY 4.0, served anonymously over an OGC Web
# Coverage Service (verified live 2026-07-16 at Rostock-Laage via
# GDAL's WCS driver).  The state's per-tile download wrapper ignores
# ranged requests, so the WCS is the right channel.

role=airport_inset
access_strategy=wcs

wcs_service_url=https://www.geodaten-mv.de/dienste/dgm_wcs
wcs_version=2.0.1
wcs_coverage=mv_dgm

native_resolution_m=1
# Mecklenburg-Vorpommern.
coverage_bbox=10.5,53.0,14.5,54.8

vertical_datum=DHHN2016
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=GeoBasis-DE/M-V

priority=85
enabled=True
