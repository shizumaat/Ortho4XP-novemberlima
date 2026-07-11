# Shuttle Radar Topography Mission (SRTM) GL1, 1 arc-second.
#
# Base-tier definition kept for the manual-download workflow: the
# OpenTopography direct downloads are dead upstream, so the strategy only
# recycles a file the user has placed at the legacy cache path
# (Elevation_data/<block>/<tile>_SRTMv3.hgt) by hand. Disabled so the
# automatic base selection never considers it; the legacy "SRTM" keyword
# still selects it explicitly.

role=base
access_strategy=manual_download
legacy_keyword=SRTM

resolution_arc_seconds=1

# Radar mission coverage envelope.
coverage_bbox=-180.0,-60.0,180.0,60.0

vertical_datum=EGM96
license=Public Domain (NASA)
attribution=NASA Shuttle Radar Topography Mission

priority=0
enabled=False
