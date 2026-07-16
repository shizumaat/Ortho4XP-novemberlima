# Department of Agriculture, Environment and Rural Affairs (Northern
# Ireland) elevation model, ~1 metre.
#
# Province-wide model hosted as a tiles-only ArcGIS LERC pyramid on
# the global Web Mercator grid (tile reads verified 2026-07-16 at
# Belfast).
#
# DISABLED pending licence confirmation: the service credits Bluesky
# (a commercial lidar supplier) alongside DAERA, and the open-data
# terms for redistribution were not verifiable -- enable once the
# DAERA licence is confirmed.

role=airport_inset
access_strategy=arcgis_lerc_tiles

tile_url_template=https://tiles-eu1.arcgis.com/kswen6BYexuc1SUk/arcgis/rest/services/Full_NI_Elevation/ImageServer/tile/{level}/{row}/{col}
tile_level=17

native_resolution_m=1
# Northern Ireland.
coverage_bbox=-8.2,54.0,-5.4,55.4

vertical_datum=Belfast Ordnance Datum
license=UNCONFIRMED (DAERA / Bluesky)
attribution=DAERA Northern Ireland / Bluesky

priority=80
enabled=False
