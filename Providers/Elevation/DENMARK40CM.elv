# Danish national height model (DHM/Terraen), ~0.4 metre grid, via
# the Dataforsyningen distribution platform (Klimadatastyrelsen).
#
# ACCOUNT REQUIRED: GetCapabilities is open but every GetCoverage
# answers 403 "User not authorized" without a token (live-probed
# 2026-07-16).  Create a free Dataforsyningen account, copy the token
# from your user profile, and paste it into the Ortho4XP settings
# window (Elevation -> Provider accounts).  The {api_key} placeholder
# is substituted at fetch time by O4_Authenticated_Sessions and never
# written to logs or provenance sidecars.
#
# Deliberately NOT the sibling Datafordeler platform
# (wcs.datafordeler.dk, 401-gated): that one has no single-string
# key -- it requires a separate "tjenestebruger" with its own
# username/password, a poorer fit and a clumsier sign-up.
#
# NOT yet verified end-to-end with a real token (the gate and the 403
# rejection convention are verified; the GetCoverage leg follows the
# same WCS core as England, Norway and the other national services).

role=airport_inset
access_strategy=wcs

wcs_service_url=https://api.dataforsyningen.dk/dhm_wcs_DAF?token={api_key}
wcs_coverage=dhm_terraen
wcs_version=1.0.0

credential_kind=api_key
session_name=dataforsyningen
registration_url=https://dataforsyningen.dk/

# Step-by-step setup shown in the sign-in dialog.
setup_step_1=Create a free account at https://dataforsyningen.dk/ (click "Opret bruger", choose "Borger" for personal use), then verify your email and log in.
setup_step_2=Open your profile (the person icon, top right) and find the "Token" section; generate a token if there is none, then copy it.
setup_step_3=Paste the token in the field below and click Sign in.
# A tiny 100x100 sample GetCoverage: 403 on a bad token (the open
# GetCapabilities cannot validate anything).
api_key_probe_url=https://api.dataforsyningen.dk/dhm_wcs_DAF?service=WCS&version=1.0.0&request=GetCoverage&coverage=dhm_terraen&crs=EPSG:25832&bbox=721000,6174000,721100,6174100&width=100&height=100&format=GTiff&token={api_key}

native_resolution_m=0.4
# Denmark including Bornholm.
coverage_bbox=8.0,54.5,15.3,57.8

vertical_datum=DVR90
license=Danish public data (free registration)
attribution=Klimadatastyrelsen, Denmark

priority=88
enabled=True
