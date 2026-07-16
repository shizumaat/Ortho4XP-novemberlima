# Programa Pernambuco Tridimensional (PE3D, Brazil) digital terrain
# model, 1 metre.
#
# Bare-earth state-wide lidar (covers Recife), free -- but the portal
# requires an account and a CAPTCHA per session, so downloads are
# manual: fetch the per-municipality terrain-model zips (MDT) from
# the page below (or with the official QGIS PE3D Downloader plugin),
# drop them into Elevation_data/Pernambuco_PE3D/, and builds convert
# and index the GeoTIFF sheets automatically on first use.  Sheets
# carry their own coordinate system and keep it.

role=airport_inset
access_strategy=xyz_archive_drop

download_page=https://pe3d.pe.gov.br

drop_directory_name=Pernambuco_PE3D
source_epsg=31985

native_resolution_m=1
# Pernambuco.
coverage_bbox=-41.4,-9.5,-34.8,-7.2

vertical_datum=Imbituba (Brazilian orthometric)
license=Free with registration (Governo de Pernambuco)
attribution=Programa Pernambuco Tridimensional (PE3D)

priority=85
enabled=True
