# Freistaat Thueringen (Germany) digital terrain model DGM1, 1 metre.
#
# Bare-earth lidar tiles, Datenlizenz Deutschland Namensnennung 2.0,
# anonymous 1 km zip downloads (each zip carries the GeoTIFF), read
# remotely through /vsizip//vsicurl (verified live 2026-07-16 at
# Erfurt).  IMPORTANT: served from geoportal.geoportal-th.de -- the
# geoportal.thueringen.de host sits behind a browser challenge.  The
# 2020-2025 epoch is the newest naming scheme.

role=airport_inset
access_strategy=tile_grid_http

tile_url_template=https://geoportal.geoportal-th.de/hoehendaten/DGM/dgm_2020-2025/dgm1_32_{easting_km}_{northing_km}_1_th_2020-2025.zip
zip_inner_suffix=.tif
tile_size_km=1
source_epsg=25832

native_resolution_m=1
# Thuringia.
coverage_bbox=9.8,50.2,12.7,51.7

vertical_datum=DHHN2016
license=Datenlizenz Deutschland Namensnennung 2.0 (attribution required)
attribution=GDI-Th, Freistaat Thueringen

priority=85
enabled=True
