# Landesamt fuer Geoinformation und Landesvermessung Niedersachsen
# (Lower Saxony, Germany) digital terrain model DGM1, 1 metre.
#
# Bare-earth Cloud-Optimized GeoTIFF tiles, Datenlizenz Deutschland
# Zero 2.0, discovered through the state's anonymous STAC API and
# window-read from object storage -- a pure definition on the stac
# strategy (verified live 2026-07-16 at Hanover).

role=airport_inset
access_strategy=stac

discovery_url_template=https://dgm.stac.lgln.niedersachsen.de/search
collections=dgm1
dtm_asset_keys=dgm1-tif

native_resolution_m=1
# Lower Saxony.
coverage_bbox=6.6,51.2,11.6,54.0

vertical_datum=DHHN2016
license=Datenlizenz Deutschland Zero 2.0 (no restrictions)
attribution=LGLN Niedersachsen

priority=85
enabled=True
