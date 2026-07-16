# Bayerische Vermessungsverwaltung (Bavaria, Germany) digital terrain
# model DGM1, 1 metre.
#
# Bare-earth lidar tiles, CC BY 4.0, anonymous deterministic 1 km
# GeoTIFF downloads on bayernwolke (verified live 2026-07-16 at
# Munich).  Uses the tile_grid_http strategy: pure kilometre
# arithmetic in EPSG:25832 with a HEAD probe per candidate tile.

role=airport_inset
access_strategy=tile_grid_http

tile_url_template=https://download1.bayernwolke.de/a/dgm/dgm1/{easting_km}_{northing_km}.tif
tile_size_km=1
source_epsg=25832

native_resolution_m=1
# Bavaria.
coverage_bbox=8.9,47.2,13.9,50.6

vertical_datum=DHHN2016
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Bayerische Vermessungsverwaltung - www.geodaten.bayern.de

priority=85
enabled=True
