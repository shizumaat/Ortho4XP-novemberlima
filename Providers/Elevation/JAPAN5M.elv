# Geospatial Information Authority of Japan (GSI) elevation tiles,
# 5 metre lidar with the nationwide composite fallback.
#
# Japan's national bare-earth elevation model, published ONLY as
# anonymous slippy-map text tiles (256x256 comma-separated metres, the
# letter "e" for nodata) -- no GeoTIFF, Web Coverage Service or STAC
# exists in the public GSI stack.  Uses the xyz_text_tiles strategy:
# the 5 m lidar layer (dem5a, zoom 15) first, and wherever it has no
# tile the server-side priority-merged nationwide composite (dem,
# zoom 14, ~10 m) fills in, so all of Japan is covered.
# Verified live 2026-07-15.  Attribution required: display
# "Geospatial Information Authority of Japan" per the GSI content
# terms; bulk redistribution of RAW tile values can fall under the
# Survey Act -- baked scenery is a derived work.

role=airport_inset

access_strategy=xyz_text_tiles

tile_url_template=https://cyberjapandata.gsi.go.jp/xyz/dem5a/{zoom}/{x}/{y}.txt
tile_zoom=15

fallback_url_template=https://cyberjapandata.gsi.go.jp/xyz/dem/{zoom}/{x}/{y}.txt
fallback_zoom=14

# Native ground resolution of the primary layer, in metres (the
# fallback composite is ~10 m).
native_resolution_m=5

# Cheap pre-filter: the Japanese archipelago.
coverage_bbox=122.5,24.0,146.5,45.8

# Orthometric heights above Tokyo Bay mean sea level.
vertical_datum=Tokyo Peil (Japanese geoid)
license=GSI Content Usage Terms (free with attribution)
attribution=Geospatial Information Authority of Japan (GSI)

priority=80

enabled=True
