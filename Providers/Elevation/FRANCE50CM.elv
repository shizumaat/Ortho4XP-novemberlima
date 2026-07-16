# Institut national de l'information geographique et forestiere (IGN,
# France) LiDAR HD terrain model, 0.5 metre.
#
# Bare-earth 50 cm tiles from the national LiDAR HD programme (flying
# 2021-2026), open under the Etalab 2.0 licence and served anonymously
# by the Geoplateforme: a WFS layer indexes the 1 km tiles and each
# feature carries a ready-made GeoTIFF URL (float32, Lambert-93,
# NGF-IGN69 heights).  Uses the wfs_tile_index strategy (verified live
# 2026-07-15 near Toulouse).  Coverage grows with the campaign;
# not-yet-flown areas return no tiles and honestly fall back to the
# base tier (the older RGE ALTI product, frozen since 2024, is NOT
# integrated -- LiDAR HD supersedes it).

role=airport_inset

access_strategy=wfs_tile_index

wfs_service_url=https://data.geopf.fr/wfs/ows
wfs_type_name=IGNF_MNT-LIDAR-HD:dalle
url_property=url

# Native ground resolution of the source tiles, in metres.
native_resolution_m=0.5

# Cheap pre-filter: metropolitan France including Corsica (the LiDAR
# HD FXX blocks; overseas territories are separate campaigns).
coverage_bbox=-5.2,41.3,9.6,51.1

# NGF-IGN69 normal heights (IGN78 in Corsica).
vertical_datum=NGF-IGN69
license=Licence Ouverte / Etalab 2.0
attribution=IGN France, LiDAR HD

priority=90

enabled=True
