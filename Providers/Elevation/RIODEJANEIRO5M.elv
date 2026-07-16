# Instituto Pereira Passos (IPP, Prefeitura do Rio de Janeiro,
# Brazil) digital terrain model, 5 metre.
#
# Bare-earth 2019 lidar of Rio de Janeiro MUNICIPALITY (resampled to
# 5 m; covers Galeao and Santos Dumont), CC BY 4.0, hosted as an
# ArcGIS image service whose only data channel is a tiles-only LERC
# pyramid on the standard Web Mercator grid -- the arcgis_lerc_tiles
# strategy downloads the level-15 tiles (~4.8 m) and decodes them in
# a subprocess (verified live 2026-07-16 at Galeao and Santos
# Dumont).  Windows outside the municipal data mask come back
# all-nodata and fall through to the base tier.

role=airport_inset

access_strategy=arcgis_lerc_tiles

tile_url_template=https://tiles.arcgis.com/tiles/OlP4dGNtIcnD3RYf/arcgis/rest/services/Painel_de_Monitoramento_3D_Reviver_Centro__23032022__WEL/ImageServer/tile/{level}/{row}/{col}
tile_level=15

native_resolution_m=5
# Rio de Janeiro municipality.
coverage_bbox=-43.80,-23.10,-43.09,-22.74

vertical_datum=Imbituba (Brazilian orthometric)
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Instituto Pereira Passos, Prefeitura do Rio de Janeiro

priority=85

enabled=True
