# Advanced Land Observing Satellite (ALOS) World 3D, 30 metre.
#
# Base-tier definition kept for the manual-download workflow: the
# OpenTopography direct downloads are dead upstream, so the strategy only
# recycles a file the user has placed at the legacy cache path
# (Elevation_data/<block>/<tile>_ALOS3W30.tif) by hand. Disabled so the
# automatic base selection never considers it; the legacy "ALOS" keyword
# still selects it explicitly.

role=base
access_strategy=manual_download
legacy_keyword=ALOS

resolution_arc_seconds=1

vertical_datum=EGM96
license=Free with attribution (JAXA)
attribution=Japan Aerospace Exploration Agency, ALOS World 3D

priority=0
enabled=False
