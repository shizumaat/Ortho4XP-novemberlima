# Landesamt fuer Vermessung und Geoinformation Sachsen-Anhalt
# (Germany) digital terrain model DGM1, 1 metre.
#
# Bare-earth model, Datenlizenz Deutschland Namensnennung 2.0, served
# anonymously over an OGC Web Coverage Service (verified live
# 2026-07-16 at Magdeburg via GDAL's WCS driver).  The endpoint's
# 2.0.1 GetCoverage is broken server-side; protocol 1.0.0 works.

role=airport_inset
access_strategy=wcs

wcs_service_url=https://www.geodatenportal.sachsen-anhalt.de/wss/service/ST_LVermGeo_DGM1_WCS_OpenData/guest
wcs_version=1.0.0
wcs_coverage=1

native_resolution_m=1
# Saxony-Anhalt.
coverage_bbox=10.5,50.9,13.2,53.1

vertical_datum=DHHN2016
license=Datenlizenz Deutschland Namensnennung 2.0 (attribution required)
attribution=LVermGeo Sachsen-Anhalt

priority=85
enabled=True
