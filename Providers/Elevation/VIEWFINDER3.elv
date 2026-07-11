# Viewfinderpanoramas (J. de Ferranti), 3 arc-second archives.
#
# Base-tier (tile-wide) definition; the global fallback source, matching
# the historic default behaviour of the "View" keyword outside the
# 1 arc-second zone whitelist (see VIEWFINDER1.elv).

role=base
access_strategy=viewfinder_zip
legacy_keyword=View

download_url_template=http://viewfinderpanoramas.org/dem3/{archive_code}.zip

resolution_arc_seconds=3

vertical_datum=EGM96
license=Free for non-commercial use with attribution (viewfinderpanoramas.org)
attribution=Jonathan de Ferranti, viewfinderpanoramas.org

priority=10
enabled=True
