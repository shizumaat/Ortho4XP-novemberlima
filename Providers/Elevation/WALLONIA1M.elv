# Service public de Wallonie (Belgium) terrain model MNT 2021-2022,
# 1 metre.
#
# Bare-earth regional lidar, CC BY 4.0 -- but Wallonia publishes no
# float raster service: the open downloads are PROVINCE-level GeoTIFF
# zips (2.6-11 GB each) behind an interactive order front.  Drop
# folder workflow: download the province zip(s) covering your area
# from the page below, drop them into Elevation_data/Wallonia_MNT/,
# and builds index the GeoTIFF members IN PLACE inside the zip (no
# extraction) and window-read them directly.

role=airport_inset
access_strategy=xyz_archive_drop

download_page=https://geoportail.wallonie.be/catalogue/fe13bc84-e7d7-4a1b-9adc-c3d5cbc350ee.html

drop_directory_name=Wallonia_MNT
source_epsg=3812

native_resolution_m=1
# Wallonia.
coverage_bbox=2.75,49.45,6.5,50.85

vertical_datum=DNG/TAW
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Service public de Wallonie

priority=85
enabled=True
