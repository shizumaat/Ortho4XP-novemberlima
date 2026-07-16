# Actueel Hoogtebestand Nederland (AHN, Netherlands) digital terrain
# model, 0.5 metre, via the national PDOK platform.
#
# Bare-earth national lidar (the AHN programme), public domain / CC0,
# served anonymously over PDOK's OGC Web Coverage Service (verified
# live 2026-07-16 at Schiphol via GDAL's WCS driver).  The dtm_05m
# coverage is the ground model; the sibling dsm_05m is the surface
# model and is not used.

role=airport_inset
access_strategy=wcs

wcs_service_url=https://service.pdok.nl/rws/ahn/wcs/v1_0
wcs_version=2.0.1
wcs_coverage=dtm_05m

native_resolution_m=0.5
# The Netherlands.
coverage_bbox=3.2,50.7,7.3,53.6

# Normaal Amsterdams Peil.
vertical_datum=NAP
license=Creative Commons Zero (public domain)
attribution=Actueel Hoogtebestand Nederland (AHN) / PDOK

priority=90
enabled=True
