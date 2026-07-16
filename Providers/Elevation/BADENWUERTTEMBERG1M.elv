# Landesamt fuer Geoinformation und Landentwicklung Baden-Wuerttemberg
# (Germany) digital terrain model DGM1, 1 metre.
#
# Bare-earth 1 km XYZ tiles, Datenlizenz Deutschland, packaged four to
# a 2 km zip whose south-west corner sits on ODD easting / EVEN
# northing kilometres (verified live 2026-07-16 at Stuttgart).  Tiles
# are downloaded whole and every XYZ member is warped; the XYZ carries
# no CRS, so the warp assigns EPSG:25832.

role=airport_inset
access_strategy=tile_grid_http

tile_url_template=https://opengeodata.lgl-bw.de/data/dgm/dgm1_32_{easting_km}_{northing_km}_2_bw.zip
fetch_mode=download
zip_member_glob=.xyz
tile_size_km=2
grid_easting_offset_km=1
source_epsg=25832
warp_source_epsg=25832

native_resolution_m=1
# Baden-Wuerttemberg.
coverage_bbox=7.4,47.5,10.6,49.9

vertical_datum=DHHN2016
license=Datenlizenz Deutschland (GovData)
attribution=LGL Baden-Wuerttemberg

priority=85
enabled=True
