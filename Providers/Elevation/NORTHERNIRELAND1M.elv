# Department of Agriculture, Environment and Rural Affairs (Northern
# Ireland) elevation model, ~1 metre.
#
# Province-wide model hosted as a tiles-only ArcGIS LERC pyramid on
# the global Web Mercator grid (tile reads verified 2026-07-16 at
# Belfast).
#
# Licence is fine per project policy (Ortho4XP never redistributes;
# users fetch from the agency during their own builds).  DISABLED
# anyway (2026-07-16) because the service fails DATA verification:
# its cache serves 70-byte empty stubs at both Belfast airports and
# at Derry at every level, with real payloads only over Belfast city
# at the ~10 m level -- an inconsistently cached 3D-scene layer, not
# a province terrain model.  Northern Ireland needs a different
# source (OpenDataNI's raw DTM downloads are the lead to probe).

role=airport_inset
access_strategy=arcgis_lerc_tiles

tile_url_template=https://tiles-eu1.arcgis.com/kswen6BYexuc1SUk/arcgis/rest/services/Full_NI_Elevation/ImageServer/tile/{level}/{row}/{col}
tile_level=17

native_resolution_m=1
# Northern Ireland.
coverage_bbox=-8.2,54.0,-5.4,55.4

vertical_datum=Belfast Ordnance Datum
license=DAERA open data (fetch-time use; no redistribution by this tool)
attribution=DAERA Northern Ireland / Bluesky

priority=80
enabled=False
