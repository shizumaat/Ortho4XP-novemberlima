# Direcao-Geral do Territorio national lidar terrain model, 2 metre
# (MDT-2m), mainland Portugal.
#
# ACCOUNT REQUIRED: same free DGT account and shared "dgterritorio"
# session as PORTUGAL50CM -- sign in once and both providers work.
# Formerly a manual drop-folder source (Elevation_data/Portugal_DGT);
# superseded 2026-07-16 by automatic download through the
# authenticated token search, same protocol as the 50 cm collection.
# The 2 m collection is the coarser product of the same 2024-2025
# national lidar campaign and ranks one notch below PORTUGAL50CM.

role=airport_inset
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
license=CC BY 4.0 (registration required)
attribution=Direcao-Geral do Territorio

priority=84
enabled=True
