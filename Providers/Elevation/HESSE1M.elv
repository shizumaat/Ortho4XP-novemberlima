# Hessische Verwaltung fuer Bodenmanagement und Geoinformation
# (Hesse, Germany) digital terrain model DGM1, 1 metre.
#
# Bare-earth model, Datenlizenz Deutschland Zero 2.0, native 1 m over
# the INSPIRE Web Coverage Service (verified live 2026-07-16 at
# Frankfurt).  The server defeats GDAL's WCS driver (it advertises
# octet-stream as its native format), so this uses the wcs_kvp
# strategy with the GetCoverage request spelled out.

role=airport_inset
access_strategy=wcs_kvp

wcs_getcoverage_template=https://inspire-hessen.de/raster/dgm1/ows?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID=he_dgm1&SUBSET=E({xmin},{xmax})&SUBSET=N({ymin},{ymax})&FORMAT=image/tiff
source_epsg=25832

native_resolution_m=1
# Hesse.
coverage_bbox=7.7,49.3,10.3,51.7

vertical_datum=DHHN2016
license=Datenlizenz Deutschland Zero 2.0 (no restrictions)
attribution=HVBG Hessen

priority=85
enabled=True
