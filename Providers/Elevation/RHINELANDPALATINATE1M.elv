# Landesamt fuer Vermessung und Geobasisinformation Rheinland-Pfalz
# (Rhineland-Palatinate, Germany) digital terrain model DGM1, 1 metre.
#
# Bare-earth 1 km GeoTIFF tiles, Datenlizenz Deutschland Namensnennung
# 2.0, anonymous deterministic downloads with a per-tile acquisition
# year resolved through the browsable directory index (fetched once
# and cached; verified live 2026-07-16 at Hahn).

role=airport_inset
access_strategy=tile_grid_http

tile_url_template=https://geobasis-rlp.de/data/dgm1/current/tif/{file_name}
index_url=https://geobasis-rlp.de/data/dgm1/current/tif/
tile_size_km=1
source_epsg=25832

native_resolution_m=1
# Rhineland-Palatinate.
coverage_bbox=6.1,48.9,8.6,51.0

vertical_datum=DHHN2016
license=Datenlizenz Deutschland Namensnennung 2.0 (attribution required)
attribution=LVermGeo Rheinland-Pfalz

priority=85
enabled=True
