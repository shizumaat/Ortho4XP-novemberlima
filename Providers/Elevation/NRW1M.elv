# Geobasis Nordrhein-Westfalen (Germany) digital terrain model DGM1,
# 1 metre.
#
# Bare-earth lidar tiles, Datenlizenz Deutschland Zero 2.0
# (public-domain-equivalent), anonymous 1 km GeoTIFF downloads on
# opengeodata.nrw.de (verified live 2026-07-16 at Duesseldorf).  File
# names carry a per-tile acquisition year, resolved through the
# machine-readable directory index (fetched once and cached).

role=airport_inset
access_strategy=tile_grid_http

tile_url_template=https://www.opengeodata.nrw.de/produkte/geobasis/hm/dgm1_tiff/dgm1_tiff/{file_name}
index_url=https://www.opengeodata.nrw.de/produkte/geobasis/hm/dgm1_tiff/dgm1_tiff/index.json
tile_size_km=1
source_epsg=25832

native_resolution_m=1
# North Rhine-Westphalia.
coverage_bbox=5.8,50.3,9.5,52.6

vertical_datum=DHHN2016
license=Datenlizenz Deutschland Zero 2.0 (no restrictions)
attribution=Geobasis NRW

priority=85
enabled=True
