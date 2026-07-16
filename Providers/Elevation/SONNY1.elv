# Sonny's LiDAR Digital Terrain Models of Europe, 1 arc-second.
#
# Lidar-derived bare-earth terrain models assembled from the European
# national open-lidar programmes, covering essentially all of Europe
# (plus Iceland, Greenland and the Atlantic islands) as classic
# 1 arc-second .hgt tiles. Same quality class as the national lidar
# sources, far better than the mixed radar/topographic compilations,
# wherever the author has published a country.
#
# Distribution is Google Drive folders (per-country and whole-Europe
# archives) linked from the page below -- there is NO stable per-tile
# download URL to automate -- so this definition uses the
# hgt_archive_drop strategy: download the archives once by hand and
# drop them (the zip files themselves, or already-extracted .hgt
# tiles) into Elevation_data/Sonny_LiDAR_Europe/ under the Ortho4XP
# data folder; each build then extracts the tiles it needs on demand.

role=base
access_strategy=hgt_archive_drop
legacy_keyword=SONNY1

# The human download page; shown in the instructions when a tile is
# requested that is not in the drop folder.
download_page=https://sonny.4lima.de

# Folder under Elevation_data/ scanned for dropped archives and tiles.
drop_directory_name=Sonny_LiDAR_Europe

resolution_arc_seconds=1

# Generous Europe envelope (west, south, east, north) including the
# Canary Islands, Iceland and Greenland. Only the cheap pre-filter:
# the authoritative coverage judge is the drop-folder presence test in
# the strategy, so automatic selection never picks this source for a
# tile the user has not downloaded.
coverage_bbox=-74.0,27.0,45.0,84.0

vertical_datum=EGM96
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=Sonny, sonny.4lima.de - LiDAR Digital Terrain Models of Europe

# Above VIEWFINDER1 (60): lidar-derived 1 arc-second beats the
# de Ferranti compilation wherever the user has dropped the data, and
# the presence test in covers() keeps this priority from ever
# out-ranking a source whose data is actually available.
priority=80

enabled=True
