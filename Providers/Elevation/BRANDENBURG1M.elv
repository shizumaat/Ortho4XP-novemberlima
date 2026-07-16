# Landesvermessung und Geobasisinformation Brandenburg (Germany)
# digital terrain model DGM1, 1 metre -- covers Berlin too.
#
# Bare-earth lidar tiles, Datenlizenz Deutschland Namensnennung 2.0,
# anonymous deterministic 1 km zips (GeoTIFF inside), read remotely
# through /vsizip//vsicurl (verified live 2026-07-16 at Berlin
# Brandenburg airport).  The archive is "Brandenburg mit Berlin", so
# no separate Berlin provider is needed (Berlin's own product is
# ASCII-only anyway).

role=airport_inset
access_strategy=tile_grid_http

tile_url_template=https://data.geobasis-bb.de/geobasis/daten/dgm/tif/dgm_33{easting_km}-{northing_km}.zip
zip_inner_suffix=.tif
tile_size_km=1
source_epsg=25833

native_resolution_m=1
# Brandenburg including Berlin.
coverage_bbox=11.2,51.3,14.8,53.6

vertical_datum=DHHN2016
license=Datenlizenz Deutschland Namensnennung 2.0 (attribution required)
attribution=GeoBasis-DE/LGB

priority=85
enabled=True
