# Ministry of the Interior (Taiwan) 20 metre grid digital terrain
# model (numerical terrain model, 數值地形模型).
#
# Bare-earth photogrammetric national terrain model under the Taiwan
# Open Government Data License (attribution required, commercial use
# permitted).  The dataset INDEX is a keyless API on data.gov.tw, but
# the file host (tgos.tw) blocks every non-browser client -- so this
# uses the xyz_archive_drop strategy: download the county archives (or
# the whole-country one) from the page below IN A BROWSER, drop the
# zip files into Elevation_data/Taiwan_MOI_DEM/, and builds convert
# and index the TWD97 XYZ sheets automatically on first use.
#
# NOTE: if airports on a tile were already checked before the archives
# were dropped, rebuild with the inset refresh option (the negative
# results are cached).

role=airport_inset

access_strategy=xyz_archive_drop

# 2025 sheet-divided edition (per county):
#   https://data.gov.tw/dataset/176927
# Older whole-country edition (one zip):
#   https://data.gov.tw/dataset/35430
download_page=https://data.gov.tw/dataset/176927

drop_directory_name=Taiwan_MOI_DEM

# Sheets are TWD97 Transverse Mercator (2-degree zone 121), listed as
# N,E,H points -- northing first.
source_epsg=3826
xyz_column_order=YXZ

# Native ground resolution of the source grid, in metres.
native_resolution_m=20

# Cheap pre-filter: Taiwan and Penghu.
coverage_bbox=118.1,21.8,122.1,25.4

vertical_datum=TWVD2001
license=Taiwan Open Government Data License v1.0 (attribution required)
attribution=Ministry of the Interior, Taiwan (內政部)

priority=80

enabled=True
