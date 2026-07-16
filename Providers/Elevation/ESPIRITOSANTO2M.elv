# GEOBASES / IEMA Espirito Santo (Brazil) digital terrain model,
# 2 metre.
#
# Bare-earth state-wide model (2012-2015 mapping programme), open
# state geodata, published as deterministic 10 km blocks on the
# state's anonymous object storage (verified live 2026-07-16 at
# Vitoria).  Block names are grid INDEXES (coordinate / 10000) with
# the northing counted from the block's top edge; the rasters are
# ERDAS Imagine files GDAL range-reads directly.  Integer-metre
# values (UInt16 source).

role=airport_inset
access_strategy=tile_grid_http

tile_url_template=https://one.s3.es.gov.br/pr-geobases-public/MAP_ES_2012_2015/MDT/{easting_index}_{northing_index}.img
tile_size_km=10
grid_northing_index_offset=1
source_epsg=31984

native_resolution_m=2
# Espirito Santo.
coverage_bbox=-41.9,-21.3,-39.6,-17.9

vertical_datum=Imbituba (Brazilian orthometric)
license=Open Espirito Santo state geodata
attribution=GEOBASES / IEMA, Governo do Espirito Santo

priority=85
enabled=True
