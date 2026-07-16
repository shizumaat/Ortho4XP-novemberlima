# Nacionaline zemes tarnyba (Lithuania) digital terrain model
# DTM-LT 2020, 1 metre.
#
# National bare-earth model served anonymously through a public
# ArcGIS proxy; exportImage returns float GeoTIFF windows, spelled
# out through the wcs_kvp strategy (verified live 2026-07-16 at
# Vilnius).  EPSG:3346 is the Lithuanian national grid.

role=airport_inset
access_strategy=wcs_kvp

wcs_getcoverage_template=https://utility.arcgis.com/usrsvcs/servers/fef66dec83c14b0295180ecafa662aa0/rest/services/DTM_LT2020/ImageServer/exportImage?bbox={xmin},{ymin},{xmax},{ymax}&bboxSR=3346&imageSR=3346&format=tiff&pixelType=F32&noData=&size={width},{height}&f=image
source_epsg=3346

native_resolution_m=1
# Lithuania.
coverage_bbox=20.9,53.9,26.9,56.5

vertical_datum=LAS07 (Lithuanian height system)
license=Lithuanian open geodata
attribution=Nacionaline zemes tarnyba, Lithuania

priority=85
enabled=True
