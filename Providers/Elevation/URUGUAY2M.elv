# Infraestructura de Datos Espaciales de Uruguay (IDEuy) national
# terrain model, 2.5 metre.
#
# Bare-earth model from the 2017-2019 national survey -- the only
# country-wide open meter-class terrain programme in Latin America --
# under Uruguay's open data licence.  A single national GeoJSON
# catalog lists all ~6600 tiles with direct, range-readable GeoTIFF
# URLs; the geojson_tile_index strategy caches it once and window-
# reads the intersecting tiles (verified live 2026-07-16 at
# Montevideo).

role=airport_inset

access_strategy=geojson_tile_index

index_url=https://catalogodatos.gub.uy/dataset/cd4b81ab-3943-4bc5-8075-bb1fab4bef9c/resource/e33f0906-3b82-4a98-ad83-c9862eb5e86e/download/grilla_mdt_nacional_epsg4326.geojson
url_property=MDT_geoT

# Native ground resolution of the source tiles, in metres.
native_resolution_m=2.5

# Uruguay.
coverage_bbox=-58.5,-35.1,-53.0,-30.0

vertical_datum=Uruguayan official height datum (orthometric)
license=Licencia de Datos Abiertos de Uruguay
attribution=Infraestructura de Datos Espaciales de Uruguay (IDEuy)

priority=85

enabled=True
