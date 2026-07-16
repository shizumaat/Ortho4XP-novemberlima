# Landesamt GeoInformation Bremen (Germany) digital terrain model
# DGM1, 1 metre.
#
# Bare-earth 1 km XYZ tiles, CC BY 4.0, packaged as one state-wide
# zip whose members GDAL reads remotely through ranged requests
# (verified live 2026-07-16 at Bremen airport).  The template IS a
# GDAL virtual path; candidate members are probed by opening them.
# The XYZ carries no CRS of its own, so the warp assigns EPSG:25832.

role=airport_inset
access_strategy=tile_grid_http

tile_url_template=/vsizip//vsicurl/https://gdi2.geo.bremen.de/inspire/download/DGM/data/Gitternetz_DGM1_2017_HB_ASCII_XYZ.zip/Gitternetz_DGM1_2017/dgm1_32{easting_km}_{northing_km}_1_hb.xyz
tile_size_km=1
source_epsg=25832
warp_source_epsg=25832

native_resolution_m=1
# Bremen city (Bremerhaven is a separate 2015 archive without an
# airport of note).
coverage_bbox=8.6,52.9,9.1,53.3

vertical_datum=DHHN2016
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Landesamt GeoInformation Bremen

priority=85
enabled=True
