# Instituto Geografico Nacional (Spain) digital terrain model, 5 metre.
#
# Bare-earth national terrain model derived from the PNOA lidar
# programme, orthometric heights (EGM08-REDNAP), served through the
# INSPIRE Web Coverage Service on servicios.idee.es with no key or
# account.  5 metres is the finest grid this endpoint offers (the 2 m
# MDT02 exists but only behind the CNIG download-center flow); still
# ~6x finer than any base source and enough to capture embankments and
# terraces at airport scale.  Uses the generic wcs access strategy
# (verified live 2026-07-15).

role=airport_inset

access_strategy=wcs

wcs_service_url=https://servicios.idee.es/wcs-inspire/mdt
wcs_version=2.0.1
# The EPSG:4258 (ETRS89 geographic) national 5 m coverage: degree-based
# like our request bounding boxes, covering peninsula and Balearics.
wcs_coverage=Elevacion4258_5

# Native ground resolution of the source raster, in metres.
native_resolution_m=5

# Cheap pre-filter: peninsular Spain, the Balearics, Ceuta and
# Melilla.  The Canary Islands are a different horizontal datum
# (REGCAN95) and coverage, not included here.
coverage_bbox=-9.4,35.1,4.4,43.9

vertical_datum=EGM08-REDNAP
license=CC BY 4.0 compatible (attribution scne.es required)
attribution=Instituto Geografico Nacional de Espana / scne.es

# Coarser than the meter-class providers; the value only documents
# intent (no geographic overlap with any other inset provider).
priority=60

enabled=True
