# Forestry and Land Scotland mainland digital terrain model, 30 metre.
#
# Mainland-wide terrain grid hosted as a tiles-only ArcGIS LERC
# pyramid cached in the British National Grid (EPSG:27700, custom
# cache origin; verified live 2026-07-16 at Edinburgh).  30 m is
# coarse for an inset but still three times finer than the worldwide
# base -- and the only open Scotland-wide source found (the Scottish
# Remote Sensing Portal's finer lidar is campaign-partial, on S3, and
# a future addition).

role=airport_inset
access_strategy=arcgis_lerc_tiles

tile_url_template=https://tiles.arcgis.com/tiles/JF5BD4Y14a3LnPcJ/arcgis/rest/services/Scene30_WEL/ImageServer/tile/{level}/{row}/{col}
tile_level=11
tile_epsg=27700
tile_origin_x=-9597137.348713825
tile_origin_y=4470073.533885086
tile_resolution=38.13605250821619

native_resolution_m=30
# Scotland.
coverage_bbox=-7.7,54.6,-0.7,58.7

vertical_datum=ODN
license=Open Government Licence v3 (Forestry and Land Scotland)
attribution=Forestry and Land Scotland

priority=70
enabled=True
