# Civil Engineering and Development Department (Hong Kong) digital
# terrain model, 5 metre.
#
# Territory-wide terrain grid via DATA.GOV.HK, hosted as a tiles-only
# ArcGIS LERC pyramid on the global Web Mercator grid (verified live
# 2026-07-16 at Chek Lap Kok).  Mostly bare-earth, but the source
# documents that some elevated roads and bridges remain in the grid
# (about +/-5 m class) -- still far better than the 90 m base.

role=airport_inset
access_strategy=arcgis_lerc_tiles

tile_url_template=https://tiles.arcgis.com/tiles/6j1KwZfY2fZrfNMR/arcgis/rest/services/HK_DTM/ImageServer/tile/{level}/{row}/{col}
tile_level=15

native_resolution_m=5
# Hong Kong.
coverage_bbox=113.80,22.13,114.45,22.58

vertical_datum=Hong Kong Principal Datum
license=DATA.GOV.HK open data terms
attribution=CEDD / LandsD, Government of Hong Kong SAR (via DATA.GOV.HK)

priority=80
enabled=True
