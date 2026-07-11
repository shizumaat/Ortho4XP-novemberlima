# Viewfinderpanoramas (J. de Ferranti), 1 arc-second archives.
#
# Base-tier (tile-wide) definition, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
# The legacy "View" keyword resolves to this definition wherever
# dem1_zones covers the tile, else to VIEWFINDER3 -- exactly the choice
# the historic O4_DEM_Utils.ensure_elevation made inline.

role=base
access_strategy=viewfinder_zip

# Legacy short keyword: the cache file path (FNAMES.viewfinderpanorama)
# and the download log/retry labels stay byte-identical to the historic
# behaviour.
legacy_keyword=View

download_url_template=http://viewfinderpanoramas.org/dem1/{archive_code}.zip

resolution_arc_seconds=1

# The letter+number archive codes for which de Ferranti publishes
# 1 arc-second data (Alps, Scandinavia-adjacent zones, New Zealand).
# Moved verbatim out of the historic hardcoded whitelist so coverage
# updates are file edits, not code edits.
dem1_zones=L31,L32,L33,K32,O31,P31,N32,O32,P32,Q32,N33,O33,P33,Q33,R33,O34,P34,Q34,R34,O35,P35,Q35,R35,P36,Q36,R36,SL58,SI59,SJ59,SK59,SL59,SI60,SJ60,SK60,SL60

# Wellington International has missing elevation data in the 1 arc-second
# resolution (historic hardcoded exception, now declarative).
exclude_tiles=-42,174

vertical_datum=EGM96
license=Free for non-commercial use with attribution (viewfinderpanoramas.org)
attribution=Jonathan de Ferranti, viewfinderpanoramas.org

priority=60
enabled=True
