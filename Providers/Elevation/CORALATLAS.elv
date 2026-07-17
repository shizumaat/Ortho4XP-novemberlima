# Allen Coral Atlas satellite-derived reef bathymetry (10 m).
#
# Declarative elevation-inset provider, parsed by
# src/O4_Airport_Elevation_Insets.py:initialize_elevation_providers_dict.
#
# The Atlas (allencoralatlas.org, Arizona State University) maps every
# shallow reef on Earth at 10 m from Sentinel-2/Landsat-8/Planet imagery
# where the bottom is visible — the 0-20 m zone that drives the
# depth-graded water masks. Coverage includes the reef regions no open
# government source reaches (Pacific island nations, Southeast Asia,
# the non-US Caribbean, the Red Sea).
#
# Downloads are FREE but account-gated, so this provider serves a LOCAL
# LIBRARY instead of a live URL: packages the user downloads (in-app
# guided fetch under Tools > Allen Coral Atlas, or manually from the
# website) land in Elevation_data/AllenCoralAtlas/ and are indexed from
# there. A tile has coverage exactly when a downloaded package overlaps
# it; until then discovery reports no coverage and the other bathymetry
# providers (or the OpenStreetMap reef fallback) apply.
role=bathymetry

# Named fetch strategy implemented in code (strategy registry in the
# module); delegates to src/O4_Coral_Atlas.py.
access_strategy=coral_atlas_library

# Native ground resolution of the source rasters, in metres. Finer than
# the 50 m masks-auto gate, so downloaded coverage engages the depth
# ramp automatically.
native_resolution_m=10.0

# Cheap pre-filter; the local library index is authoritative. The
# world's photic reef belt.
coverage_bbox=-180.0,-35.0,180.0,35.0

# Satellite-derived depth below the sea surface at acquisition time.
# Source rasters carry POSITIVE CENTIMETERS (16-bit integers); the
# strategy converts to the pipeline's negative metres. Never used for
# terrain grading (role=bathymetry).
vertical_datum=Sea surface (satellite-derived)

# Reef depths only; the floor is irrelevant but kept consistent with
# the other bathymetry providers.
value_floor_m=-11100.0

# CC-BY 4.0: reuse requires attribution — the scenery distribution note
# should cite the Atlas as below.
license=CC-BY 4.0 (Allen Coral Atlas)
attribution=Allen Coral Atlas (2022). Imagery, maps and monitoring of the world's tropical coral reefs. doi.org/10.5281/zenodo.3833242

# Below the measured-lidar CUDEM providers (100): where both cover a
# tile the lidar-derived depths win; above EMODnet (60) and GEBCO (10).
priority=80

enabled=True
