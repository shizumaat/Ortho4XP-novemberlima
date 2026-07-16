# Maa- ja Ruumiamet (Estonian Land and Spatial Board) digital terrain
# model, 1 metre.
#
# National bare-earth lidar hosted as a tiles-only ArcGIS LERC
# pyramid cached in the Estonian national grid (EPSG:3301) -- the
# arcgis_lerc_tiles strategy's projected-pyramid keys carry the cache
# origin and per-level resolution (verified live 2026-07-16 at
# Tallinn).

role=airport_inset
access_strategy=arcgis_lerc_tiles

tile_url_template=https://tiles.arcgis.com/tiles/ZYGCYltwz5ExeoGm/arcgis/rest/services/DTM_3301/ImageServer/tile/{level}/{row}/{col}
tile_level=12
tile_epsg=3301
tile_origin_x=40500.0
tile_origin_y=7017000.0
tile_resolution=0.9765625000001229

native_resolution_m=1
# Estonia.
coverage_bbox=21.7,57.5,28.3,59.8

vertical_datum=EH2000
license=Estonian open geodata (Maa- ja Ruumiamet)
attribution=Maa- ja Ruumiamet, Estonia

priority=85
enabled=True
