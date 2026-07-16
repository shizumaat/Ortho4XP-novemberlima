# Lantmateriet national lidar digital terrain model (Markhojdmodell
# Nedladdning, grid 1 m), Sweden.
#
# ACCOUNT REQUIRED for the pixels only: the STAC catalog search is
# anonymous, but every Cloud-Optimized GeoTIFF read answers 401 with
# WWW-Authenticate: Basic (live-probed 2026-07-16) -- a Geotorget
# "systemkonto" sent as HTTP Basic authentication unlocks them.  Sign
# in from the Ortho4XP settings window (Elevation -> Provider
# accounts); credentials are stored in the platform secret store and
# carried to GDAL through a warp-scoped GDAL_HTTP_USERPWD option.
#
# GOTCHA (this is what "access denied" almost always means): the
# account credentials do not grant file access until the (free)
# "Markhojdmodell Nedladdning" product has been ORDERED and attached
# to the account -- until then every file answers 401, even with a
# correct username and password.  Order the product first; the SAME
# Geotorget web-login username and password then work here (for a
# private individual on open data, no separate system account is
# issued -- verified live 2026-07-16).
#
# License CC BY 4.0 (NOT CC0 -- corrected in the 2026-07 research
# round).  Verified end-to-end with a real account 2026-07-16: ESSB,
# ESSA, ESMS and ESPA all read within a few metres of their published
# field elevations, and the Skanor sand spit reads 0-3 m.
#
# DATUM GOTCHA (found and fixed 2026-07-16): the Cloud-Optimized
# GeoTIFFs declare the COMPOUND CRS EPSG:5845 (SWEREF99 TM + RH2000
# height).  gdal.Warp sees the vertical component and helpfully
# applies the RH2000 -> ellipsoid geoid shift during reprojection,
# lifting the whole country by the 23-36 m geoid separation.  The
# shared warp core (warp_vsicurl_sources_to_geotiff) now passes
# -novshift so heights stay orthometric RH2000, matching the
# vertical_datum declared below.

role=airport_inset
access_strategy=stac

discovery_url_template=https://api.lantmateriet.se/stac-hojd/v1/search
collections=dtm-cog
# The DTM asset is keyed "data" (exact match wins before the
# "metadata" substring could).
dtm_asset_keys=data

credential_kind=http_basic
session_name=lantmateriet_geotorget
registration_url=https://geotorget.lantmateriet.se/

# Step-by-step setup shown in the sign-in dialog.  Step 2 is the one
# everyone misses: the account cannot read files until the free
# product is attached to it.
setup_step_1=Create a free Geotorget account at https://geotorget.lantmateriet.se/ and verify your email.
setup_step_2=Order the free product "Markhojdmodell Nedladdning" (the plain 1 metre one, NOT "grid 50+" and NOT "Visning").  Downloads stay locked until this product is attached to your account.
setup_step_3=Wait for the order to become active (you will get a confirmation from Lantmateriet).
setup_step_4=Sign in below with your Geotorget username and password (the same web login).
# A known 2.5 km national-grid tile (Stockholm area): 401 without
# credentials, 200/206 with them; the probe sends a tiny ranged read.
session_probe_url=https://dl1.lantmateriet.se/hojd/data/grid/mhm/66_6/m662_67.tif

native_resolution_m=1
# Sweden.
coverage_bbox=10.9,55.2,24.2,69.1

vertical_datum=RH 2000
license=Creative Commons Attribution 4.0 (CC BY 4.0; free registration)
attribution=Lantmateriet, Sweden

priority=85
enabled=True
