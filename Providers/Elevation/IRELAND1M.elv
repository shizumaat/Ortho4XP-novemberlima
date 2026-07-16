# Geological Survey Ireland national lidar digital terrain model,
# 1 metre.
#
# Bare-earth campaign lidar, CC BY 4.0, published through ArcGIS
# coverage catalogs whose polygon features carry DATA_URL links to
# zip/7z archives of GeoTIFF tiles -- the arcgis_feature_tiles
# strategy enumerates the catalogs once, spatially queries them per
# airport and reads the archives through /vsizip and /vsi7z (verified
# live 2026-07-16 at Dublin).  Coverage is campaign-based, not
# wall-to-wall; uncovered airports keep the base tier.

role=airport_inset
access_strategy=arcgis_feature_tiles

catalog_folder_url=https://gsi.geodata.gov.ie/server/rest/services/Lidar
url_field=DATA_URL
member_filter=dtm

native_resolution_m=1
# Republic of Ireland.
coverage_bbox=-10.7,51.3,-5.9,55.5

vertical_datum=Malin Head
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Geological Survey Ireland

priority=85
enabled=True

# Some campaign tiles carry an UNDECLARED -99 fill value.
source_nodata=-99
