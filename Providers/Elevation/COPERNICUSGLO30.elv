# Copernicus DEM GLO-30 (ESA), 30 metre global surface model.
#
# The global fallback INSET tier: TanDEM-X radar heights resampled to
# 1 arc-second, published as one Cloud-Optimized GeoTIFF per 1-degree
# cell on the registration-free AWS Open Data mirror, the cell encoded
# in the object name (degree_named_cog strategy).  Ocean-only cells are
# simply absent from the bucket; the strategy's existence probes turn
# them into cached no-coverage negatives.
#
# This is a SURFACE model: buildings and canopy are baked into the
# heights.  The surface_model_building_masking pass therefore replaces
# every pixel under (or within footprint_mask_buffer_m of) an
# OpenStreetMap building footprint with ground interpolated from its
# surroundings before the inset is cached -- see
# mask_building_footprints_in_surface_model in
# src/O4_Airport_Elevation_Insets.py.  Masking is the reason this
# source is inset-only (airports, where footprints are mapped and the
# ground under terminals is nearly planar): a tile-wide overlay would
# spread uncorrected rooftop and canopy heights across whole cities,
# so the strategy deliberately refuses wide-area use.

role=airport_inset

access_strategy=degree_named_cog

url_template=https://copernicus-dem-30m.s3.amazonaws.com/Copernicus_DSM_COG_10_{latitude_token}_00_{longitude_token}_00_DEM/Copernicus_DSM_COG_10_{latitude_token}_00_{longitude_token}_00_DEM.tif

native_resolution_m=30

# Global coverage; per-cell existence is probed against the bucket.
coverage_bbox=-180.0,-90.0,180.0,90.0

surface_model_building_masking=True
footprint_mask_buffer_m=35

vertical_datum=EGM2008
license=ESA Copernicus DEM GLO-30 licence (free use, modification and distribution with attribution)
attribution=Produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and (c) Airbus Defence and Space GmbH 2014-2018, provided under COPERNICUS by the European Union and ESA

# The whole point of this definition is to be LAST: a genuine national
# lidar or photogrammetric source is always preferable, so every other
# enabled inset provider outranks the global radar fallback.
priority=1

enabled=True
