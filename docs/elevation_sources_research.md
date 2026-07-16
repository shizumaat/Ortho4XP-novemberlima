# Open elevation sources for airport insets — research report

Three adversarially-verified web-research rounds (2026-07-15) locating
open, programmatically-fetchable bare-earth digital terrain models
finer than the tile-wide base sources, for use as `role=airport_inset`
providers (see `docs/airport_elevation_insets_spec.md`).  Every claim
below survived a 3-voter adversarial verification pass; "live-probed"
means the endpoint answered an anonymous request on 2026-07-15.

## Already shipped as providers

| Code | Region | Resolution | Strategy |
|---|---|---|---|
| `USGS3DEP` | United States | 1 m | `tnm_cog` |
| `HRDEM` | Canada | 1 m | `stac` |
| `ENGLAND1M` | England (~99%) | 1 m | `wcs` |
| `NORWAY1M` | Norway mainland | 1 m | `wcs` |
| `SWISSALTI3D` | Switzerland + Liechtenstein | 0.5 m | `stac` |
| `SPAIN5M` | Spain (peninsula + Balearics) | 5 m | `wcs` |
| `POLAND1M` | Poland | 1 m | `wcs` |
| `WALES1M` | Wales | 1 m | `direct_cog` |
| `NEWZEALAND1M` | New Zealand (survey coverage) | 1 m | `static_stac` (LERC subprocess decode) |
| `FINLAND2M` | Finland | 2 m | `stac` (keyless CSC Paituli mirror) |
| `JAPAN5M` | Japan (5 m lidar + 10 m composite fallback) | 5 m | `xyz_text_tiles` |
| `AUSTRALIA5M` | Australia (survey patchwork, populated coasts) | 5 m | `wcs` |
| `TAIWAN20M` | Taiwan + Penghu | 20 m | `xyz_archive_drop` (browser-download drop folder) |
| `FRANCE50CM` | France (LiDAR HD, growing to 2026) | 0.5 m | `wfs_tile_index` |
| `BAVARIA1M` `NRW1M` `THURINGIA1M` `BRANDENBURG1M` `SAXONY1M` `BREMEN1M` `SCHLESWIGHOLSTEIN1M` `BADENWUERTTEMBERG1M` `RHINELANDPALATINATE1M` | German Länder (kilometre tile grids) | 1 m | `tile_grid_http` |
| `MECKLENBURG1M` `SAXONYANHALT1M` | German Länder (coverage services) | 1 m | `wcs` |
| `HESSE1M` | Hesse | 1 m | `wcs_kvp` (driver-defeating server) |
| `HAMBURG1M` | Hamburg (whole-city file, browser download) | 1 m | `xyz_archive_drop` |
| `SAARLAND18M` | Saarland — DISABLED (open coverage has undocumented value offset; native 1 m is fee-bound) | ~18.5 m | `wcs_kvp` |
| `AUSTRIA1M` | Austria (50 km EPSG:3035 BigTIFF tiles, ranged reads) | 1 m | `tile_grid_http` |
| `NETHERLANDS50CM` | Netherlands (PDOK AHN, WCS — the OpenTopography login gate was never needed) | 0.5 m | `wcs` |
| `SOUTHTYROL50CM` | South Tyrol, Italy (CC0 valley lidar) | 0.5 m | `wcs` |
| `SARDINIA1M` | Sardinia, Italy | 1 m | `wcs` |
| `ITALY10M` | Italy national fallback (TINITALY WCS — beats the 90 m base everywhere incl. the Alps) | 10 m | `wcs` |
| `AUSTRIA1M` | Austria | 1 m | `tile_grid_http` (50 km metre-named tiles) |
| `URUGUAY2M` | Uruguay — national 2.5 m catalog, the only country-wide open meter-class programme in Latin America | 2.5 m | `geojson_tile_index` |
| `ESPIRITOSANTO2M` | Espirito Santo, Brazil (covers Vitoria) | 2 m | `tile_grid_http` |
| `CURITIBA50CM` | Curitiba municipality, Brazil (covers Bacacheri, NOT Afonso Pena) | 0.5 m | `wcs_kvp` (ArcGIS exportImage) |
| `PERNAMBUCO1M` | Pernambuco, Brazil (covers Recife; CAPTCHA-gated portal → drop folder) | 1 m | `xyz_archive_drop` |
| `RIODEJANEIRO5M` | Rio de Janeiro municipality (Galeao + Santos Dumont) | 5 m | `arcgis_lerc_tiles` (tiles-only pyramid, 257-sample shared-edge LERC blobs, subprocess decode) |
| `CZECHIA2M` | Czechia national DMR 5G | 2 m | `wcs_kvp` (ArcGIS exportImage) |
| `LITHUANIA1M` | Lithuania national DTM-LT 2020 | 1 m | `wcs_kvp` (ArcGIS exportImage) |
| `ESTONIA1M` | Estonia national (EPSG:3301 projected pyramid) | 1 m | `arcgis_lerc_tiles` |
| `HONGKONG5M` | Hong Kong (some elevated roads remain in the grid) | 5 m | `arcgis_lerc_tiles` |
| `SCOTLAND30M` | Scotland mainland fallback (EPSG:27700 pyramid) | 30 m | `arcgis_lerc_tiles` |
| `SCOTLAND50CM` | Scotland lidar campaigns incl. Edinburgh + Glasgow (srsp-open-data S3, OS-grid-named tiles, finest campaign wins) | 0.5-1 m | `os_grid_bucket` |
| `ZAGREB1M` | Zagreb city (the airport lies outside the data mask) | 1 m | `arcgis_lerc_tiles` |
| `NORTHERNIRELAND1M` | Northern Ireland — DISABLED: cache serves empty stubs at every NI airport (licence is fine per the no-redistribution ruling; the DATA failed) | n/a | `arcgis_lerc_tiles` |
| `FLANDERS1M` | Flanders, Belgium (covers Brussels airport) | 1 m | `wcs` |
| `IRELAND1M` | Republic of Ireland (campaign lidar; covers Dublin) | 1 m | `arcgis_feature_tiles` (DATA_URL catalogs → zip/7z; per-member fill declaration defeats wrong nodata metadata) |
| `WALLONIA1M` | Wallonia, Belgium (province GeoTIFF zips → drop folder, members indexed in place) | 1 m | `xyz_archive_drop` |
| `PORTUGAL2M` | Portugal (free-registration DGT downloads → drop folder) | 0.5-2 m | `xyz_archive_drop` |
| `SONNY1` (base tier, not inset) | Europe | 1 arc-second | `hgt_archive_drop` |

## Verified, not yet integrated — ranked by ease of integration

(Switzerland and Spain-WCS from this list shipped as `SWISSALTI3D` /
`SPAIN5M` on 2026-07-15, both verified through the production fetch
path at LSZH and LEMD.)

1. ~~Switzerland — swisstopo swissALTI3D~~ **shipped** (`stac`
   strategy, finest-GeoTIFF asset selection added for its
   filename-keyed multi-resolution assets).
2. **Germany, North Rhine-Westphalia — DGM1** (1 m, bare-earth,
   EPSG:25832 horizontal, EPSG:7837 DHHN2016 vertical, Datenlizenz
   Deutschland Zero = public-domain-equivalent). Deterministic 1 km
   GeoTIFF tiles `dgm1_32_[X]_[Y]_1_nw_[YEAR].tif` under
   `https://www.opengeodata.nrw.de/produkte/geobasis/hm/dgm1_tiff/dgm1_tiff/`;
   range reads work. Caveat: the `[YEAR]` token varies per tile —
   consult the directory's XML index, do not construct blind.
3. **Germany, Bavaria — DGM1** (1 m, bare-earth, EPSG:25832,
   CC BY 4.0). Anonymous deterministic 1 km tiles, e.g.
   `https://download1.bayernwolke.de/a/dgm/dgm1/690_5334.tif`.
4. ~~Spain — IGN INSPIRE WCS~~ **shipped** (`wcs` strategy as-is,
   coverage `Elevacion4258_5` at 5 m; the finer 2 m MDT02 remains
   behind the CNIG download center, see entry 7).
5. ~~Italy — TINITALY~~ **shipped** (`ITALY10M` via INGV's WCS —
   the per-tile zip scheme `w{southing/10km:03d}{westing/10km:02d}_s10`
   is decoded and documented as a backup).  Regional lidar shipped for
   South Tyrol (0.5 m CC0) and Sardinia (1 m); Tuscany 1 m,
   Veneto/Piemonte/Lombardia 5 m, Emilia-Romagna (ArcGIS ImageServer),
   Trentino, Friuli and the national PST 1-2 m lidar (browser-only
   web-app, WAF-blocked WCS) remain portal-bound — future adapters.
6. ~~Austria — BEV ALS DTM~~ **shipped** (`tile_grid_http` with
   metre-named 50 km tiles; no ATOM step needed, the naming is
   deterministic).
7. **Spain — MDT02** (2 m, bare-earth, COG format) — but distributed
   through the CNIG download-center flow; no verified anonymous
   deterministic URL scheme, so practical cost unknown.

## Verified round 1 (Europe north-west) — need work beyond current strategies

- ~~Netherlands — AHN4~~ **shipped** (`NETHERLANDS50CM`): the PDOK
  national platform serves an anonymous WCS (`dtm_05m`) — the
  OpenTopography login gate was never the real path.  Its undeclared
  float-max nodata drove the universal sentinel-sanitization pass in
  the warp core.
- **Denmark — DHM** (`dhm_terraen`, ~0.4 m): OGC WCS at
  `https://wcs.datafordeler.dk/DHMNedboer/dhm_wcs/1.0.0/WCS?apikey=...`
  — free registration + stored API key required (anonymous = 401,
  live-probed). Fits the `wcs` strategy once a credential story exists.
- **France — IGN RGE ALTI** (1 m, Etalab 2.0): Géoplateforme download
  service + Web Map Service; no Web Coverage Service verified; updates
  halted 2024 pending the LiDAR HD successor programme.
- **Copernicus GLO-30** (30 m, near-global): anonymous S3
  Cloud-Optimized GeoTIFFs with deterministic tile names
  (`copernicus-dem-30m.s3.amazonaws.com`) — but a surface model
  (includes buildings/vegetation), NOT bare-earth; unsuitable for
  airport insets, noted only as a possible base-tier alternative.

## Round 3 verified (2026-07-15, live-probed unless noted)

1. **New Zealand — LINZ 1 m lidar DEM**: anonymous Cloud-Optimized
   GeoTIFFs + STAC catalog in the `nz-elevation` S3 bucket
   (ap-southeast-2, CC BY 4.0, no AWS account;
   `https://nz-elevation.s3-ap-southeast-2.amazonaws.com/catalog.json`).
   Static catalog, no /search API — needs either static-STAC walking
   or a region/survey path convention.
2. **Wales — DataMapWales lidar DTM**: whole-country anonymous COGs on
   Azure blob storage, OGL v3
   (`https://dmwproductionblob.blob.core.windows.net/cogs/lidar/wales_dtm_32bit_cog.tif`,
   48 GB single COG — a `/vsicurl/` window read away).
3. **Scotland — Scottish Public Sector LiDAR**: anonymous S3 bucket
   `srsp-open-data` (eu-west-2), DTM COGs, OGL v3 (some phase-2 sets
   non-commercial). Partial coverage (survey campaigns, not national).
4. **Poland — GUGiK NMT 1 m**: anonymous OGC WCS 2.0.1 GetCoverage at
   `https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WCS/DigitalTerrainModelFormatTIFF`,
   coverage `DTM_PL-KRON86-NH_TIFF` — fits the shipped `wcs` strategy
   as-is. (License is statutory-open rather than a named CC tag.)
5. **Estonia — Maa-amet DTM**: anonymous deterministic HTTP downloads,
   1 m per map sheet plus whole-country 5/10/25 m GeoTIFFs.
6. **Sweden — Lantmäteriet Markhöjdmodell Nedladdning**: 1 m GeoTIFF,
   CC0 — but the concrete anonymous fetch endpoint was not pinned.
7. **Finland — National Land Survey 2 m**: CC BY 4.0 confirmed; the
   claimed programmatic path (OGC API / WCS) was REFUTED — endpoint
   still unknown.
8. **Japan — GSI elevation tiles**: attribution-only license
   confirmed; the exact tile URL scheme was REFUTED as claimed and
   needs first-hand probing.

## Round 4 (2026-07-15, targeted live-probe agents)

- **Japan — GSI elevation tiles**: anonymous deterministic XYZ text
  tiles, `https://cyberjapandata.gsi.go.jp/xyz/dem5a/15/{x}/{y}.txt`
  (5 m lidar, 256×256 comma-separated metres, nodata `e`), with the
  `dem` composite at z14 as the nationwide 10 m fallback. Bare-earth,
  Tokyo-Peil orthometric, attribution-only. No COG/WCS/STAC anywhere —
  integration needs a small XYZ-text strategy.
- **Sweden — Lantmäteriet 1 m**: full anonymous STAC API
  (`https://api.lantmateriet.se/stac-hojd/v1/`, collection `dtm-cog`)
  but the COG pixels are credential-gated (free Geotorget account,
  Basic/OAuth). License corrected: **CC BY 4.0, not CC0**. Datum
  RH 2000 (EPSG:5845 compound). Fits `stac` + a stored-credential story.
- **Finland — NLS 2 m**: SHIPPED via the CSC Paituli mirror (above);
  the NLS's own WCS (`korkeusmalli_2m`) and OGC API Processes channels
  work but need a free API key.
- **Australia — Geoscience Australia 5 m lidar DEM**: anonymous OGC WCS
  at `https://services.ga.gov.au/gis/services/DEM_LiDAR_5m_2025/MapServer/WCSServer`
  (CC BY 4.0, GDA94/AHD, float32). Gotchas: WCS 1.0.0 with
  `CRS=EPSG:4283` + `FORMAT=GeoTIFF` is the reliable call; coverage is
  a ~245,000 km² survey patchwork (populated coasts), NOT continental
  — needs the all-nodata guard and an SRTM-service fallback. ELVIS is
  email-gated async delivery (not scriptable); no open lidar-DEM COG
  bucket exists (the `dea-public-data` "bare-earth" prefix is a
  Sentinel-2 spectral product, not terrain).

## Global South picture (round-4 probe agents, 2026-07-15)

Sub-30 m open + automatable finds: **Rwanda 10 m national DTM**
(CC BY 4.0, anonymous signed-URL API on UNDP GeoHub + Titiler COG
endpoint), **Taiwan 20 m DTM** (open-government license, keyless
data.gov.tw API chain), **São Paulo municipality 1 m** (CC BY-SA,
anonymous WFS index + direct zips of ground-classified LAZ — point
clouds, needs rasterizing), **Rio de Janeiro 5 m** (CC BY 4.0 ArcGIS
ImageServer, tiles-only, integration unproven). Argentina's IGN 5 m
photogrammetric model is free but form/portal-bound; Indonesia DEMNAS
(~8 m, DSM-leaning) and India CartoDEM (30 m) are registration-gated;
Philippines lidar needs discretionary human approval; South Korea is
JS-portal-bound; Thailand/Malaysia have nothing open below 30 m;
Africa outside Rwanda has nothing open below 30 m (South Africa's 5 m
SUDEM is commercial). Pan-regionally the automatable floor stays
30 m: Copernicus GLO-30 (DSM), NASADEM, and FABDEM (bare-earth-
corrected but CC BY-NC-SA, non-commercial) — none bare-earth
meter-class. OpenTopography hosts no African or Asian city lidar.

## Latin America (round-5 probe agents, 2026-07-16)

Shipped: Uruguay (national), Espirito Santo, Curitiba, Pernambuco
(drop).  Confirmed but not integrable anonymously: Mexico INEGI 5 m /
1.5 m lidar (JavaScript-driven downloads — headless-browser only),
Argentina IGN 5 m (form-bound, migration outage through late 2025,
partial coverage), Ecuador SIGTIERRAS 3-5 m MDT (2026-07-16 UPDATE:
the sigtierras.gob.ec host is now an info-only shell; the actual data
moved to the MAG geoportal.agricultura.gob.ec "Gestor de Descarga de
Informacion" -- an interactive login-gated download manager, ~88%
national coverage, cantonal-mosaic/grid downloads, NOT a per-airport
API or STAC.  Registration requires an Ecuadorian national ID number,
so no anonymous or foreigner account path -- would be a manual
drop-folder source at best), Bogota 0.5 m (token-gated AND
non-commercial licensed), Costa Rica
SNIT (JS-injected endpoints, server 502 at probe time — worth a
browser follow-up), Brasilia (1 m contour vectors only).  Nothing
open: Chile (12.5 m radar DSM only), Peru, Panama, Guatemala,
Dominican Republic, Paraguay, Bolivia, Sao Paulo state beyond the
capital (Guarulhos and Campinas have no open coverage), Porto Alegre
(state lidar only now being procured post-flood — recheck SEMA-RS),
Salvador, Fortaleza, Belo Horizonte.  Rio de Janeiro's tiles-only
service is now SHIPPED via the arcgis_lerc_tiles strategy.

## ArcGIS hunt (round 6, 2026-07-16)

The anonymous ArcGIS Online search API
(`arcgis.com/sharing/rest/search?q=type:"Image Service"` walked per
country with local-language terrain terms) is the productive
discovery tool.  Shipped from it: Czechia, Lithuania, Estonia, Hong
Kong, Scotland 30 m, Zagreb (+ Northern Ireland disabled).  Colombia's
IGAC catalog (504 per-department image services at
`mapas.igac.gov.co/image/rest/services/md`) looked like the biggest
win but FAILED value verification: the probed "antioquia" service is
a mislabeled ~100 km project patch whose extent decodes to a
different department entirely — a proper integration needs an
extent-enumeration pass over all 504 services (an "arcgis_catalog"
meta-index, same shape as the static STAC walker) with per-service
value checks.  Tier-2 leads with the projected-pyramid math now in
place: Latvia national (EPSG:3059 pyramid), Galicia 1 m (EPSG:25829
pyramid).  Empty: Republic of Ireland (hillshade services only),
Belgium (rendered view services only), Turkey/Middle East, Greece,
Slovakia, Hungary, Slovenia, Portugal national, Africa.

## Non-ArcGIS channels (round 7, 2026-07-16)

Shipped: Flanders (anonymous WCS), Ireland (feature catalogs carrying
DATA_URL archives — one campaign declares nodata 0.0 while filling
with -99, driving per-member fill-sniffing VRTs), Wallonia and
Portugal as drop folders.  Confirmed closed: Greece (cadastre 5 m is
licensed, application-only), Turkey (Istanbul's open-data portal has
no elevation; national mapping is request/commercial), Hungary
(DDM5 sold through geoshop.hu).  Needs an EU-IP re-probe: Slovakia
(free 1 m country-wide with a 400 km² GeoTIFF bbox export — the
probe host was geoblocked) and Slovenia (free 1 km tiles but the old
deterministic URL scheme is dead; capture the Atlas okolja download
call).  Latvia's national pyramid proved mechanically readable but
tops out at ~76 m near Riga — not worth wiring.

## Unresolved after four rounds

Latvia, Lithuania, Slovenia, Northern Ireland, German Länder beyond
North Rhine-Westphalia and Bavaria, and the Middle East. The Middle
East (including OTHH/Doha) specifically surfaced nothing open at
meter class in any round.

## Sonny's provenance (checked 2026-07-15)

Sonny's LiDAR DTMs of Europe are a volunteer compilation of exactly
the national open-data programmes this registry integrates directly:
the published source list
(`_Datasources.txt`, linked from https://sonny.4lima.de via Google
Drive, file id `1rgGA22Ha42ulQORK9Pfp4JPpPAIKFx6Q`) enumerates, per
country, the agency download pages — including ALL 16 German Länder
portals, the Austrian Länder, Czechia, the Nordic and Baltic
agencies, France (RGE ALTI), Benelux, and viewfinderpanoramas /
SRTM as fill where no national lidar exists (Croatia, Hungary,
Cyprus...). Sonny's value-add is merging, void-filling and uniform
1 arc-second HGT packaging — "going direct" means integrating those
per-agency endpoints ourselves, which is what the inset providers
above do at native (finer) resolution. The German Länder list in
that file is the ready-made worklist for a Germany integration
beyond NRW/Bavaria.

## Open questions for a future round

- Whether Spain's CNIG download center exposes stable anonymous
  per-sheet URLs for the 2 m MDT02 (would jump it near the top).
- Whether the other German Länder match the NRW/Bavaria
  deterministic-tile pattern.
- swisstopo STAC v0.9 vs v1 item hrefs (target v1 in any integration —
  the shipped `SWISSALTI3D.elv` already targets v1).
- Concrete fetch endpoints for Sweden and Finland; first-hand probe of
  the Japan GSI tile scheme; Australia ELVIS/AWS hosting.
