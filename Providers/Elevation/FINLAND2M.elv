# National Land Survey of Finland (Maanmittauslaitos) elevation model,
# 2 metre, via the CSC Paituli open mirror.
#
# Bare-earth 2 m national terrain model from laser scanning, CC BY 4.0.
# The National Land Survey's own services are API-key-gated, but CSC's
# Paituli research-data service mirrors the complete, current dataset
# with a keyless STAC API and anonymous range-readable GeoTIFFs on
# Funet (verified live 2026-07-15) -- so this is a pure definition on
# the existing stac strategy.

role=airport_inset

access_strategy=stac

discovery_url_template=https://paituli.csc.fi/geoserver/ogc/stac/v1/search

collections=nls_digital_elevation_model_2m_at_paituli

# Each item carries two assets: "..._at_paituli_tiff" (public HTTPS on
# Funet) and "..._at_puhti_tiff" (a local path on CSC's Puhti computing
# cluster, useless off-cluster).  The preference token matches by
# substring and must select the Paituli one.
dtm_asset_keys=paituli_tiff

# Native ground resolution of the source rasters, in metres.
native_resolution_m=2

# Cheap pre-filter: Finland.
coverage_bbox=19.0,59.7,31.6,70.1

# N2000, the Finnish national height datum (EPSG:3900).
vertical_datum=N2000
license=Creative Commons Attribution 4.0 (CC BY 4.0)
attribution=National Land Survey of Finland; mirror hosted by CSC (Paituli)

priority=80

enabled=True
