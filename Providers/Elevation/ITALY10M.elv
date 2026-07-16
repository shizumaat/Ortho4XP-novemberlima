# TINITALY v1.1 (INGV) national digital terrain model, 10 metre.
#
# Bare-earth national model (heterogeneous provenance: regional
# models, largely contour-derived -- not uniform lidar), CC BY 4.0
# with citation of Tarquini et al. (doi:10.13127/tinitaly/1.1),
# served anonymously over INGV's OGC Web Coverage Service (verified
# live 2026-07-16 via GDAL's WCS driver).  The national fallback:
# regional lidar (South Tyrol, Sardinia) outranks it where present,
# and 10 m still beats the 90 m worldwide base everywhere else in
# Italy -- including the whole Alps and Apennines.

role=airport_inset
access_strategy=wcs

wcs_service_url=https://tinitaly.pi.ingv.it/TINItaly_1_1/wcs
wcs_version=2.0.1
wcs_coverage=TINItaly_1_1__tinitaly_dem

native_resolution_m=10
# Italy.
coverage_bbox=6.6,35.4,18.6,47.2

vertical_datum=Orthometric (Italian height system)
license=Creative Commons Attribution 4.0 (cite Tarquini et al., doi:10.13127/tinitaly/1.1)
attribution=Istituto Nazionale di Geofisica e Vulcanologia (INGV), TINITALY

priority=60
enabled=True
