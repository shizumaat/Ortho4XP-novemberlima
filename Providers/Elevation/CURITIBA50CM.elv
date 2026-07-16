# Instituto de Pesquisa e Planejamento Urbano de Curitiba (IPPUC,
# Brazil) digital terrain model, 0.5 metre.
#
# Bare-earth 2019 lidar of Curitiba MUNICIPALITY (covers Bacacheri
# airport; Afonso Pena lies outside the city and outside the data),
# served anonymously by an ArcGIS image service.  Uses the wcs_kvp
# strategy with the exportImage request spelled out (verified live
# 2026-07-16); windows outside the data mask come back all-nodata and
# fall through to the base tier.

role=airport_inset
access_strategy=wcs_kvp

wcs_getcoverage_template=https://geocuritiba.ippuc.org.br/server/rest/services/GeoCuritiba/MDT_2019/ImageServer/exportImage?bbox={xmin},{ymin},{xmax},{ymax}&bboxSR=31982&imageSR=31982&format=tiff&pixelType=F32&noData=&size={width},{height}&f=image
source_epsg=31982

native_resolution_m=0.5
# Curitiba municipality.
coverage_bbox=-49.42,-25.65,-49.15,-25.30

vertical_datum=Imbituba (Brazilian orthometric)
license=Open municipal geodata (IPPUC)
attribution=IPPUC, Prefeitura de Curitiba

priority=85
enabled=True
