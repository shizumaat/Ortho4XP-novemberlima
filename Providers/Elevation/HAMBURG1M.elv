# Landesbetrieb Geoinformation und Vermessung Hamburg (Germany)
# digital terrain model DGM1, 1 metre.
#
# Bare-earth 1 m model, Datenlizenz Deutschland Namensnennung 2.0 --
# but Hamburg publishes it ONLY as one whole-city ~2.3 gigabyte ASCII
# file (no per-tile downloads, no usable Web Coverage Service), so
# this uses the xyz_archive_drop strategy: download the file from the
# page below in a browser, drop it into
# Elevation_data/Hamburg_DGM1/, and the first build converts it once
# (several minutes) into an indexed GeoTIFF used by every later build.

role=airport_inset

access_strategy=xyz_archive_drop

download_page=https://suche.transparenz.hamburg.de/dataset/digitales-hohenmodell-hamburg-dgm-1

drop_directory_name=Hamburg_DGM1

source_epsg=25832

# Native ground resolution of the source grid, in metres.
native_resolution_m=1

# Hamburg.
coverage_bbox=9.6,53.3,10.4,53.8

vertical_datum=DHHN2016
license=Datenlizenz Deutschland Namensnennung 2.0 (attribution required)
attribution=Freie und Hansestadt Hamburg, Landesbetrieb Geoinformation und Vermessung

priority=85

enabled=True
