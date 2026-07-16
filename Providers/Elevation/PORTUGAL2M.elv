# Direcao-Geral do Territorio (Portugal) national lidar terrain
# model, 0.5-2 metre.
#
# Bare-earth tiles from the 2024-2025 national lidar programme
# (~90% coverage), CC BY 4.0 -- downloads require a FREE registered
# account at the DGT data centre, so this is a drop-folder source:
# create the account, download the terrain-model tiles for your area
# from the page below (about 200 square kilometres per session),
# drop the zips or GeoTIFFs into Elevation_data/Portugal_DGT/, and
# builds index them automatically.

role=airport_inset
access_strategy=xyz_archive_drop

download_page=https://cdd.dgterritorio.gov.pt/

drop_directory_name=Portugal_DGT
source_epsg=3763

native_resolution_m=2
# Portugal mainland.
coverage_bbox=-9.6,36.9,-6.2,42.2

vertical_datum=Cascais 1938
license=Creative Commons Attribution 4.0 (CC BY 4.0; free registration to download)
attribution=Direcao-Geral do Territorio, Portugal

priority=85
enabled=True
