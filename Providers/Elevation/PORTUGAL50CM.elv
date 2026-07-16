# Direcao-Geral do Territorio national lidar digital terrain model,
# 50 centimetre (MDT-50cm), mainland Portugal.
#
# ACCOUNT REQUIRED: downloads are gated behind a free registration at
# https://cdd.dgterritorio.gov.pt/ -- sign in once from the Ortho4XP
# settings window and the session is kept alive automatically (see
# O4_Authenticated_Sessions).  The catalog search itself is public and
# mints short-lived tokenized download URLs; redeeming one with the
# signed-in session answers a presigned object-storage URL that GDAL
# reads windowed through /vsicurl (verified live 2026-07-16 at Lisbon).
#
# Items are 2 km EPSG:3763 (ETRS89 / Portugal TM06) Float32 GeoTIFF
# tiles, declared nodata -999, ~16 MB each.

role=airport_inset
access_strategy=authenticated_token_search

search_url=https://cdd.dgterritorio.gov.pt/dgt-be/v1/search
collections=MDT-50cm

# Shared account session (PORTUGAL2M reuses it).
session_name=dgterritorio
login_flow=keycloak_password
login_url=https://cdd.dgterritorio.gov.pt/auth/login
# The login page carries the account-creation ("Registo") flow.
registration_url=https://cdd.dgterritorio.gov.pt/auth/login

# Step-by-step setup shown in the sign-in dialog.
setup_step_1=Create a free account at https://cdd.dgterritorio.gov.pt/ (follow the "Registo" link on the sign-in page) and verify your email.
setup_step_2=Sign in below with that username and password.  One sign-in covers both the 50 cm and 2 m Portugal collections.
# Signed out: HTTP redirect to the login page.  Signed in: a plain JSON
# 404 from the backend router -- anything but a redirect/401/403 counts
# as signed in (see O4_Authenticated_Sessions.probe_signed_in).
session_probe_url=https://cdd.dgterritorio.gov.pt/dgt-be/v1/?f=json

native_resolution_m=0.5
# Mainland Portugal (the lidar campaign does not cover the islands).
coverage_bbox=-9.6,36.9,-6.1,42.2

source_nodata=-999

vertical_datum=Cascais (mainland Portugal orthometric)
license=CC BY 4.0 (registration required)
attribution=Direcao-Geral do Territorio

priority=88
enabled=True
