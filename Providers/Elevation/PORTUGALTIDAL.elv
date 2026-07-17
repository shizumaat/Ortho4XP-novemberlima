# Direcao-Geral do Territorio national lidar, INTERTIDAL twin of
# PORTUGAL2M (the terrain-side definitions of the same service are
# PORTUGAL50CM and PORTUGAL2M).  The twin reads the 2 m collection:
# the bathymetry band warps to a 10 m grid, and the 50 cm sheets made
# each 0.1 degree cell a ~27 minute fetch (16x the data for no mask
# benefit; measured 2026-07-16).
#
# The 2024-2025 national lidar campaign surveyed the tidal lagoons at
# low tide: the Ria Formosa flats carry real negative Cascais
# elevations. Verified 2026-07-16 over the lagoon around Faro airport
# at 10 m: flats south of the airport 99% valid, minimum -0.37 m, 46%
# of valid pixels in the intertidal band; Olhao flats 100% valid,
# minimum -0.66 m, 85% intertidal; open Atlantic 10 km offshore =
# NO COVERAGE (no zero-fill hazard — the safety property England,
# Spain and Australia FAILED in the 2026-07-16 sweep).
#
# INTERTIDAL ONLY: topographic laser does not penetrate water. Exposed
# flats are measured; sub-tidal channels are NOT (the ~6 m dredged
# Olhao channel reads about -0.5 m), so channels render as shallow
# imagery rather than deep water — visually acceptable, and strictly
# better than the mapped-tidalflat fallback's 1 m assumption.
#
# role=bathymetry keeps this out of every terrain path; it feeds the
# depth-graded water masks and the DSF sea_level raster only
# (docs/specs/coastal-bathymetry-spec.md sections 2-5).
role=bathymetry

# Same authenticated service and shared "dgterritorio" session as
# PORTUGAL50CM / PORTUGAL2M -- sign in once and all three work.
access_strategy=authenticated_token_search

search_url=https://cdd.dgterritorio.gov.pt/dgt-be/v1/search
collections=MDT-2m

session_name=dgterritorio
login_flow=keycloak_password
login_url=https://cdd.dgterritorio.gov.pt/auth/login
registration_url=https://cdd.dgterritorio.gov.pt/auth/login
session_probe_url=https://cdd.dgterritorio.gov.pt/dgt-be/v1/?f=json

native_resolution_m=2
# Portugal mainland.
coverage_bbox=-9.6,36.9,-6.1,42.2

source_nodata=-999

vertical_datum=Cascais (mainland Portugal orthometric)
# Consistent with the other bathymetry providers; intertidal values
# never approach it.
value_floor_m=-11100.0

license=CC BY 4.0 (registration required)
attribution=Direcao-Geral do Territorio

# Below the measured-topobathy CUDEMs (100), above the Allen Coral
# Atlas (80), EMODnet (60) and GEBCO (10) — the standard intertidal
# twin rank from the 2026-07-16 sweep.
priority=95

enabled=True

# Exposed-flats lidar: data stops at the waterline, so this source is a
# binary "flats" layer the OpenStreetMap shallow-water fallback matches
# for free.  Automatic paths skip it; only masks_use_DEM_too=True
# fetches it (for regions whose OSM tidal flats are unmapped).
intertidal=True
