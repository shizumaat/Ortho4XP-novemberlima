# Landesvermessung Sachsen (Saxony, Germany) digital terrain model
# DGM1, 1 metre.
#
# Bare-earth lidar, Datenlizenz Deutschland Namensnennung 2.0,
# published as 2 km zips (GeoTIFF inside) on the state's PUBLIC
# Nextcloud share (verified live 2026-07-16 at Leipzig).  The share
# path expects the public share token -- the same one that is in the
# public URL, not a personal credential -- as a Basic-authorization
# user, and the host rejects HEAD, so tiles are probed with ranged
# requests and downloaded whole (the GeoTIFFs are striped, ranged
# window reads would not help anyway).

role=airport_inset
access_strategy=tile_grid_http

tile_url_template=https://geocloud.landesvermessung.sachsen.de/public.php/dav/files/JCcXyifaNdLDnxZ/dgm1_33{easting_km}_{northing_km}_2_sn_tiff.zip
zip_inner_suffix=.tif
zip_inner_strip=_tiff
tile_size_km=2
source_epsg=25833
http_headers=Authorization: Basic SkNjWHlpZmFOZExEbnhaOg==
probe_mode=ranged_get
fetch_mode=download

native_resolution_m=1
# Saxony.
coverage_bbox=11.8,50.1,15.1,51.7

vertical_datum=DHHN2016
license=Datenlizenz Deutschland Namensnennung 2.0 (attribution required)
attribution=GeoSN, Landesvermessung Sachsen

priority=85
enabled=True
