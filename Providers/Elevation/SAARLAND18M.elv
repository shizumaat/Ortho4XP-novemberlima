# Landesamt fuer Vermessung, Geoinformation und Landentwicklung
# Saarland (Germany) INSPIRE elevation coverage, ~18.5 metre.
#
# The only OPEN Saarland elevation service: the anonymous INSPIRE Web
# Coverage Service serves a DOWNSAMPLED (~18.5 m) version of the state
# terrain model -- the native 1 m DGM1 is fee/contract-bound (checked
# 2026-07-16).  Still ~5x finer than the 90 m worldwide base, so worth
# fetching; like Hesse the server needs the KVP request spelled out.
#
# DISABLED: the returned TIFF carries NO projection and Float32 values
# around 31768-32158 -- an undocumented offset encoding.  Baking a
# guessed offset would corrupt terrain silently; leave off until the
# LVGL documents the value semantics (or opens the native DGM1).

role=airport_inset
access_strategy=wcs_kvp

wcs_getcoverage_template=https://geoportal.saarland.de/gdi-sl/inspireraster/inspirewcsel?service=WCS&version=2.0.1&request=GetCoverage&coverageId=EL.GridCoverage&subset=x({xmin},{xmax})&subset=y({ymin},{ymax})&format=image/tiff
source_epsg=25832

native_resolution_m=18.5
# Saarland.
coverage_bbox=6.3,49.1,7.4,49.7

vertical_datum=DHHN2016
license=INSPIRE open view/coverage service
attribution=LVGL Saarland

priority=70
enabled=False
