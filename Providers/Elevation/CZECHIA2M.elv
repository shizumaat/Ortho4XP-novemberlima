# Cesky urad zememericky a katastralni (CUZK, Czechia) digital
# terrain model DMR 5G, 2 metre.
#
# National bare-earth lidar model, served anonymously by CUZK's
# ArcGIS image service (Web Mercator variant); exportImage returns
# float GeoTIFF windows, spelled out through the wcs_kvp strategy
# (verified live 2026-07-16 at Prague).

role=airport_inset
access_strategy=wcs_kvp

wcs_getcoverage_template=https://ags.cuzk.gov.cz/arcgis/rest/services/3D/dmr5g_wm/ImageServer/exportImage?bbox={xmin},{ymin},{xmax},{ymax}&bboxSR=3857&imageSR=3857&format=tiff&pixelType=F32&noData=&size={width},{height}&f=image
source_epsg=3857

native_resolution_m=2
# Czechia.
coverage_bbox=12.0,48.5,18.9,51.1

vertical_datum=Balt po vyrovnani (Baltic height system)
license=CUZK open data (attribution required)
attribution=CUZK, Czech Republic

priority=85
enabled=True
