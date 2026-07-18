"""Contract tests for the masks-step half of coastal bathymetry.

Pins ``docs/specs/coastal-bathymetry-spec.md`` sections 3-4 as they land
in ``O4_Mask_Utils``:

* ``build_bathymetry_arrays`` — the depth-graded water alpha and the
  band's land refinement.  What is pinned and WHY: the mask must keep
  imagery visible over shallow reefs (alpha 255 at the waterline) and
  fade fully to X-Plane water at ``reef_visibility_depth`` (alpha 0, not
  a grey floor — a floor would seam at the band edge, spec 4.2).  The
  spline midpoint (depth D/2) must land mid-scale.  The band's topo side
  additionally re-lands measured islets (values >= 0.5 m).  Numbers are
  asserted through a *real* warp of a small GeoTIFF because the function
  warps a VRT — there is nothing meaningful to monkeypatch.

* ``masks_use_DEM_too`` tri-state resolution in ``build_masks`` — "auto"
  fetches the band exactly once (and only then), "False" never touches
  it, "True" both fetches the band AND loads the legacy custom-DEM
  refinement.  Pinned because "auto" must be byte-identical to legacy
  when no provider covers the tile, and the mesh-existence guard must
  short-circuit BEFORE any band fetch so a broken tile never hits the
  network.

* Offshore-shallow early-return (spec 4.2) — a full-sea mask square whose
  vector pre-mask is empty is still written when the depth ramp is
  non-zero (an atoll in open water).

Headless: ``tmp_path`` only, GDAL rasters built locally, the network
band fetch always monkeypatched.  The whole module skips when the GDAL
Python bindings are unavailable.
"""

import os
import sys
import types

import numpy
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

pytest.importorskip("osgeo")
from osgeo import gdal, osr  # noqa: E402

import O4_Bathymetry_Band as BATHYBAND  # noqa: E402
import O4_File_Names as FNAMES  # noqa: E402
import O4_Geo_Utils as GEO  # noqa: E402
import O4_Mask_Utils as MASK  # noqa: E402
import O4_UI_Utils as UI  # noqa: E402


REEF_DEPTH = 25.0  # reef_visibility_depth default (spec section 6)
MASK_ZL = 16


# ---------------------------------------------------------------------------
# Geometry helpers shared by the numerics test
# ---------------------------------------------------------------------------
def _mask_square_window(til_x, til_y, mask_zl):
    """The padded 6144-pixel warp window build_bathymetry_arrays reads,
    returned as the WGS84 bbox (lon_min, lat_min, lon_max, lat_max)."""
    (latm0, lonm0) = GEO.gtile_to_wgs84(til_x, til_y, mask_zl)
    (px0, py0) = GEO.wgs84_to_pix(latm0, lonm0, mask_zl)
    px0 -= 1024
    py0 -= 1024
    (latmax, lonmin) = GEO.pix_to_wgs84(px0, py0, mask_zl)
    (latmin, lonmax) = GEO.pix_to_wgs84(px0 + 6144, py0 + 6144, mask_zl)
    return (lonmin, latmin, lonmax, latmax)


def _column_longitudes(window, columns):
    """Longitude of each output column centre after the web-mercator warp
    build_bathymetry_arrays performs (values depend on longitude only, so
    this classifies every column into its source value band)."""
    from pyproj import Transformer

    (lonmin, latmin, lonmax, latmax) = window
    (web_x_min, web_y_max) = GEO.geo_to_webm(lonmin, latmax)
    (web_x_max, web_y_min) = GEO.geo_to_webm(lonmax, latmin)
    indices = numpy.arange(columns)
    web_x = web_x_min + (indices + 0.5) * (web_x_max - web_x_min) / columns
    to_wgs84 = Transformer.from_crs(3857, 4326, always_xy=True)
    longitudes, _ = to_wgs84.transform(
        web_x, numpy.full(columns, (web_y_min + web_y_max) / 2)
    )
    return numpy.asarray(longitudes)


# Six longitude bands across the middle of the mask square, each a
# constant depth/height (spec section 4.2 value cases).
_VALUE_BANDS = [
    (0.20, 0.30, -32768.0),  # nodata
    (0.30, 0.40, 10.0),      # land +10 m
    (0.40, 0.50, 0.0),       # waterline
    (0.50, 0.60, -REEF_DEPTH / 2),   # spline midpoint
    (0.60, 0.70, -REEF_DEPTH),       # exactly D -> pure water
    (0.70, 0.80, -100.0),    # far deeper than D
]


def _write_banded_geotiff(path, window, bands=None):
    """A 100 m-posting 4326 raster over ``window`` (+margin) whose value
    depends only on longitude, split into the six ``_VALUE_BANDS`` (or
    the caller's ``bands``, same ``(low, high, value)`` shape; fractions
    not covered by any band stay nodata)."""
    (lonmin, latmin, lonmax, latmax) = window
    span = lonmax - lonmin
    margin = 0.02
    west, east = lonmin - margin, lonmax + margin
    south, north = latmin - margin, latmax + margin
    degrees_per_100m = 100.0 / 111320.0
    columns = int((east - west) / degrees_per_100m) + 1
    rows = int((north - south) / degrees_per_100m) + 1

    values = numpy.full((rows, columns), -32768.0, dtype=numpy.float32)
    for column in range(columns):
        longitude = west + (column + 0.5) * degrees_per_100m
        fraction = (longitude - lonmin) / span
        for low, high, value in (bands or _VALUE_BANDS):
            if low <= fraction < high:
                values[:, column] = value
                break

    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, columns, rows, 1, gdal.GDT_Float32)
    dataset.SetGeoTransform(
        (west, degrees_per_100m, 0, north, 0, -degrees_per_100m)
    )
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.WriteArray(values)
    band.SetNoDataValue(-32768.0)
    band.FlushCache()
    dataset = None
    return path


# =====================================================================
# 1. Depth ramp numerics (spec section 4.2)
# =====================================================================
def test_build_bathymetry_arrays_depth_ramp_numerics(tmp_path):
    location_lat, location_lon = 21.35, -159.5  # off Kauai (CUDEM Hawaii)
    (til_x, til_y) = GEO.wgs84_to_orthogrid(
        location_lat, location_lon, MASK_ZL
    )
    window = _mask_square_window(til_x, til_y, MASK_ZL)
    raster_path = str(tmp_path / "band.tif")
    _write_banded_geotiff(raster_path, window)

    tile = types.SimpleNamespace(
        mask_zl=MASK_ZL, reef_visibility_depth=REEF_DEPTH
    )
    (land_array, water_alpha) = MASK.build_bathymetry_arrays(
        til_x, til_y, tile, raster_path
    )

    # Both arrays exist and carry the spec's fixed geometry.
    assert land_array is not None and water_alpha is not None
    assert land_array.shape == (6144, 6144)
    assert water_alpha.shape == (4096, 4096)

    (lonmin, _latmin, lonmax, _latmax) = window
    span = lonmax - lonmin
    land_longitudes = _column_longitudes(window, 6144)
    land_fraction = (land_longitudes - lonmin) / span
    # water_alpha is the central crop [1024:5120] of the 6144 window.
    alpha_fraction = land_fraction[1024 : 4096 + 1024]

    def _core(fraction, low, high):
        # Central slice of a band, one band-margin in from each edge so
        # bilinear smear at the boundaries never contaminates the read.
        return (fraction > low + 0.03) & (fraction < high - 0.03)

    def _alpha_band(low, high):
        return water_alpha[:, _core(alpha_fraction, low, high)]

    def _land_band(low, high):
        return land_array[:, _core(land_fraction, low, high)]

    # Waterline (depth 0) -> imagery fully opaque.
    assert numpy.all(_alpha_band(0.40, 0.50) == 255)
    # Spline midpoint (depth D/2) -> mid-scale, strictly interior.
    midpoint = _alpha_band(0.50, 0.60)
    assert midpoint.size
    assert midpoint.min() > 60 and midpoint.max() < 195
    # At and beyond depth D -> pure X-Plane water (0, no grey floor).
    assert numpy.all(_alpha_band(0.60, 0.70) == 0)
    assert numpy.all(_alpha_band(0.70, 0.80) == 0)
    # Land (+10 m) -> re-landed in land_array, contributes no water alpha.
    assert numpy.all(_land_band(0.30, 0.40) == 255)
    assert numpy.all(_alpha_band(0.30, 0.40) == 0)
    # Nodata -> neither array.
    assert numpy.all(_land_band(0.20, 0.30) == 0)
    assert numpy.all(_alpha_band(0.20, 0.30) == 0)


def test_build_bathymetry_arrays_returns_none_off_coverage(tmp_path):
    """A window with no overlap onto the band returns (None, None) rather
    than raising — the guard the build_mask early-return relies on."""
    location_lat, location_lon = 21.35, -159.5
    (til_x, til_y) = GEO.wgs84_to_orthogrid(
        location_lat, location_lon, MASK_ZL
    )
    window = _mask_square_window(til_x, til_y, MASK_ZL)
    raster_path = str(tmp_path / "band.tif")
    _write_banded_geotiff(raster_path, window)

    tile = types.SimpleNamespace(
        mask_zl=MASK_ZL, reef_visibility_depth=REEF_DEPTH
    )
    # A square many tiles away — the raster does not cover it at all.
    (land_array, water_alpha) = MASK.build_bathymetry_arrays(
        til_x + 16 * 200, til_y, tile, raster_path
    )
    assert land_array is None and water_alpha is None


def test_coverage_edge_feather_smooths_shallow_data_edge(tmp_path):
    """Intertidal-class data that simply STOPS while still shallow (the
    Ria Formosa flats end at -0.5 m against nodata) must fade out over
    BATHYMETRY_COVERAGE_FADE_M instead of falling off a cliff quantized
    at the band's pixels — the 2026-07-16 'jagged squares' defect."""
    location_lat, location_lon = 21.35, -159.5
    (til_x, til_y) = GEO.wgs84_to_orthogrid(
        location_lat, location_lon, MASK_ZL
    )
    window = _mask_square_window(til_x, til_y, MASK_ZL)
    raster_path = str(tmp_path / "band.tif")
    # One shallow flats band surrounded by nodata on both sides.
    _write_banded_geotiff(raster_path, window, bands=[(0.30, 0.60, -0.5)])

    tile = types.SimpleNamespace(
        mask_zl=MASK_ZL, reef_visibility_depth=REEF_DEPTH
    )
    (_land_array, water_alpha) = MASK.build_bathymetry_arrays(
        til_x, til_y, tile, raster_path
    )
    assert water_alpha is not None

    (lonmin, _latmin, lonmax, _latmax) = window
    span = lonmax - lonmin
    alpha_fraction = (
        (_column_longitudes(window, 6144) - lonmin) / span
    )[1024 : 4096 + 1024]
    row = water_alpha[2048].astype(int)

    # Deep inside the data: the flats are essentially opaque.
    interior = row[(alpha_fraction > 0.40) & (alpha_fraction < 0.50)]
    assert interior.size and interior.min() >= 250
    # Far outside the data: pure water, the feather fully decays.
    outside = row[(alpha_fraction < 0.22) | (alpha_fraction > 0.68)]
    assert outside.size and outside.max() == 0
    # The transition is a fade, not a cliff: no adjacent-pixel jump
    # anywhere near the old ~250-level step, and a real population of
    # intermediate values on the way down.
    assert numpy.max(numpy.abs(numpy.diff(row))) < 25
    assert numpy.count_nonzero((row > 20) & (row < 235)) >= 40


def test_intertidal_strip_up_to_land_threshold_is_opaque(tmp_path):
    """Values between the waterline and mask_altitude_above (the wet
    beach a low-tide lidar survey measures) are opaque imagery, not the
    alpha hole they used to be; from the threshold up the land array
    takes over at the same contour."""
    location_lat, location_lon = 21.35, -159.5
    (til_x, til_y) = GEO.wgs84_to_orthogrid(
        location_lat, location_lon, MASK_ZL
    )
    window = _mask_square_window(til_x, til_y, MASK_ZL)
    raster_path = str(tmp_path / "band.tif")
    _write_banded_geotiff(
        raster_path,
        window,
        bands=[(0.30, 0.45, 0.3), (0.45, 0.60, 10.0)],
    )

    tile = types.SimpleNamespace(
        mask_zl=MASK_ZL, reef_visibility_depth=REEF_DEPTH
    )
    (land_array, water_alpha) = MASK.build_bathymetry_arrays(
        til_x, til_y, tile, raster_path
    )
    assert land_array is not None and water_alpha is not None

    (lonmin, _latmin, lonmax, _latmax) = window
    span = lonmax - lonmin
    land_fraction = (_column_longitudes(window, 6144) - lonmin) / span
    alpha_fraction = land_fraction[1024 : 4096 + 1024]

    def _core(fraction, low, high):
        return (fraction > low + 0.03) & (fraction < high - 0.03)

    # The +0.3 m strip: opaque alpha, below the land threshold.
    strip_alpha = water_alpha[:, _core(alpha_fraction, 0.30, 0.45)]
    assert strip_alpha.size and numpy.all(strip_alpha >= 250)
    strip_land = land_array[:, _core(land_fraction, 0.30, 0.45)]
    assert numpy.all(strip_land == 0)
    # The +10 m band: land array, no water alpha.
    high_alpha = water_alpha[:, _core(alpha_fraction, 0.45, 0.60)]
    assert numpy.all(high_alpha == 0)
    high_land = land_array[:, _core(land_fraction, 0.45, 0.60)]
    assert numpy.all(high_land == 255)


def test_shallow_water_fallback_query_uses_margin(monkeypatch):
    """The fallback's Overpass queries reach beyond the tile (straddling
    mask squares need the flats polygons wholly across the line) and
    carry the schema bump that invalidates pre-margin caches."""
    captured = {}

    monkeypatch.setattr(MASK.OSM, "OSM_layer", lambda: object())

    def _capture(queries, layer, lat, lon, tags, cached_suffix="",
                 **keyword_arguments):
        captured[cached_suffix] = keyword_arguments
        return 0  # a failed download skips the category loudly

    monkeypatch.setattr(MASK.OSM, "OSM_queries_to_OSM_layer", _capture)

    tile = types.SimpleNamespace(lat=0, lon=0)
    assert MASK.load_shallow_water_polygons(tile) is None
    assert set(captured) == {"reef", "tidalflat"}
    for keyword_arguments in captured.values():
        assert (
            keyword_arguments["bbox_margin_degrees"]
            == MASK.SHALLOW_WATER_QUERY_MARGIN_DEGREES
        )
        assert (
            keyword_arguments["cache_schema"]
            == MASK.SHALLOW_WATER_CACHE_SCHEMA
        )


def test_shallow_water_fallback_skipped_on_landlocked_tile():
    """A tile with no sea in the mask region (``dico_sea`` empty) never
    downloads the reef/tidal-flat fallback: masks are only built for
    ``dico_sea`` squares, so the data could never be rasterized."""
    tile = types.SimpleNamespace(lat=48, lon=8,
                                 osm_shallow_water_fallback=True)
    assert not MASK.shallow_water_fallback_wanted(
        tile, dico_sea={}, coastline_sea_present=False,
        bathymetry_band_vrt=None, airport_gated_band=False)


def test_shallow_water_fallback_skipped_on_sea_equivalent_lakes():
    """The CYXY 8-minute stall (owner 2026-07-18): sea-EQUIVALENT lakes
    fill ``dico_sea`` with mask squares, but reefs and tidal flats are
    marine features — without a coastline-flood SEA triangle anywhere in
    the mask region the fallback must not run its downloads (they were
    going to the regional-extract filter chain, 8 minutes at CYXY)."""
    lake_sea = {(0, 0): [(0.0,) * 6]}
    tile = types.SimpleNamespace(lat=60, lon=-136,
                                 osm_shallow_water_fallback=True)
    assert not MASK.shallow_water_fallback_wanted(
        tile, lake_sea, coastline_sea_present=False,
        bathymetry_band_vrt=None, airport_gated_band=False)


def test_shallow_water_fallback_gating_matrix():
    """Coastal tiles keep the historic behavior: load with no band,
    load alongside an airport-gated band, skip under a full band, and
    honour the fallback setting."""
    coastal_sea = {(0, 0): [(0.0,) * 6]}
    tile = types.SimpleNamespace(lat=37, lon=-8,
                                 osm_shallow_water_fallback=True)
    assert MASK.shallow_water_fallback_wanted(
        tile, coastal_sea, coastline_sea_present=True,
        bathymetry_band_vrt=None, airport_gated_band=False)
    assert MASK.shallow_water_fallback_wanted(
        tile, coastal_sea, coastline_sea_present=True,
        bathymetry_band_vrt="band.vrt", airport_gated_band=True)
    assert not MASK.shallow_water_fallback_wanted(
        tile, coastal_sea, coastline_sea_present=True,
        bathymetry_band_vrt="band.vrt", airport_gated_band=False)
    tile_fallback_off = types.SimpleNamespace(
        lat=37, lon=-8, osm_shallow_water_fallback=False)
    assert not MASK.shallow_water_fallback_wanted(
        tile_fallback_off, coastal_sea, coastline_sea_present=True,
        bathymetry_band_vrt=None, airport_gated_band=False)


def _install_band_geometry_osm(monkeypatch, tmp_path, coastline,
                               water_polygons):
    """Route ``_band_geometry``'s OSM traffic to synthetic data.

    The coastline query succeeds and yields ``coastline``; a ``water``
    cache file exists on disk and, if parsed, yields
    ``water_polygons``.  Returns the list of issued cache suffixes so a
    test can assert which queries actually ran."""
    import O4_OSM_Utils as OSM
    from shapely.geometry import MultiPolygon

    queries_issued = []

    def _record_query(queries, layer, lat, lon, tags, cached_suffix="",
                      **keyword_arguments):
        queries_issued.append(cached_suffix)
        return 1

    monkeypatch.setattr(OSM, "OSM_layer", lambda: object())
    monkeypatch.setattr(OSM, "OSM_queries_to_OSM_layer", _record_query)
    monkeypatch.setattr(
        OSM, "OSM_to_MultiLineString", lambda layer, lat, lon: coastline
    )
    monkeypatch.setattr(
        OSM,
        "OSM_to_MultiPolygon",
        lambda layer, lat, lon: MultiPolygon(water_polygons),
    )
    water_cache = tmp_path / "water.osm.bz2"
    water_cache.write_bytes(b"")
    monkeypatch.setattr(
        FNAMES, "osm_cached", lambda lat, lon, suffix: str(water_cache)
    )
    return queries_issued


def test_landlocked_tile_skips_the_band_outright(monkeypatch, tmp_path):
    """A tile with big inland lakes but no coastline ways never fetches
    the bathymetry band (owner direction 2026-07-18): the inland reach
    only serves lagoons adjoining a coast, and no shipped provider has
    lake bathymetry — CYXY was probing 46 wasted cells around the
    Whitehorse lakes.  The cached water query must not even be parsed."""
    import O4_Airport_Elevation_Insets as INSETS
    from shapely.geometry import MultiLineString

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    queries_issued = _install_band_geometry_osm(
        monkeypatch, tmp_path, MultiLineString([]), []
    )
    monkeypatch.setattr(
        INSETS,
        "select_bathymetry_definitions",
        lambda lat, lon: [{
            "code": "FAKEBATHY", "role": "bathymetry", "enabled": True,
            "priority": 100.0, "native_resolution_m": 3.0,
        }],
    )

    def _never_fetch(*args, **keyword_arguments):
        raise AssertionError(
            "a landlocked tile must not fetch bathymetry band cells"
        )

    monkeypatch.setattr(INSETS, "fetch_inset", _never_fetch)

    tile = types.SimpleNamespace(lat=60, lon=-136,
                                 bathymetry_band_km=5.0)
    assert BATHYBAND._ensure_bathymetry_band_now(
        tile, fine_nearshore_only=True) is None
    assert queries_issued == ["coastline"]


def test_coastal_tile_keeps_the_inland_lagoon_reach(monkeypatch,
                                                    tmp_path):
    """The reason the inland reach EXISTS: a coastal tile whose lagoon
    is inland-classed water (the Ria Formosa under the water-class
    rulings) still collects the lagoon geometry for cell selection —
    the landlocked guard must never eat this case."""
    from shapely.geometry import MultiLineString, Polygon

    queries_issued = _install_band_geometry_osm(
        monkeypatch,
        tmp_path,
        MultiLineString([[(0.1, 0.05), (0.1, 0.35)]]),
        # ~25 km2 at the equator: over MINIMUM_INLAND_WATER_KM2.
        [Polygon([(0.2, 0.2), (0.25, 0.2), (0.25, 0.25), (0.2, 0.25)])],
    )

    tile = types.SimpleNamespace(lat=0, lon=0, bathymetry_band_km=5.0)
    (coastline, inland) = BATHYBAND._band_geometry(tile)
    assert not coastline.is_empty
    assert inland is not None and not inland.is_empty
    assert queries_issued == ["coastline", "water"]


# =====================================================================
# Shared fixtures for the build_masks integration tests
# =====================================================================
@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """Redirect every writable O4 directory under tmp_path and reset the
    step's global is_working latch around the test."""
    original_override = FNAMES._data_root_override
    FNAMES.set_data_root(str(tmp_path))
    UI.is_working = 0
    try:
        yield tmp_path
    finally:
        UI.is_working = 0
        FNAMES._data_root_override = original_override
        FNAMES._apply_data_root()


def _make_tile(tmp_path, **overrides):
    """A minimal tile object carrying every attribute the masks step
    reads before (and during) the build_mask closure."""
    build_dir = str(tmp_path / "build")
    os.makedirs(build_dir, exist_ok=True)
    attributes = dict(
        lat=21,
        lon=-160,
        mask_zl=MASK_ZL,
        build_dir=build_dir,
        grouped=True,
        ratio_water=0.3,
        masks_custom_extent="",
        masks_use_DEM_too="auto",
        custom_dem="",
        fill_nodata="",
        masking_mode="sand",
        masks_width=100.0,
        distance_masks_too=False,
        coastal_foam_edge=False,
        use_masks_for_inland=False,
        reef_visibility_depth=REEF_DEPTH,
    )
    attributes.update(overrides)
    return types.SimpleNamespace(**attributes)


def _write_minimal_mesh(tile, filler_count=150):
    """A MeshVersionFormatted-2 mesh with one giant sea triangle covering
    the tile (so the square holding its barycentre is FULL sea — an empty
    vector pre-mask) plus a tiny land triangle.  Layout mirrors
    tests/fixtures/mesh/synthetic_fan_three_triangles.mesh exactly."""
    lat, lon = tile.lat, tile.lon
    # A big triangle spanning most of the 1-degree tile, tag 2 = sea.
    vertices = [
        (lon + 0.05, lat + 0.05),
        (lon + 0.95, lat + 0.05),
        (lon + 0.50, lat + 0.95),
    ]
    lines = ["MeshVersionFormatted 2", "Dimension 3", "", "Vertices", "3"]
    for (vertex_lon, vertex_lat) in vertices:
        lines.append("%.9f %.9f 0.000000000 0" % (vertex_lon, vertex_lat))
    lines += ["", "Normals", "3", "0.00 0.00 0", "0.00 0.00 0",
              "0.00 0.00 0"]
    # One giant sea triangle (tag 2) plus land-tag filler triangles
    # keeping the default mesh realistically sized (real meshes carry
    # thousands of triangles).  The fillers are tag 0 (land), skipped
    # before any geometry is read.  The sub-100 regression test below
    # passes a small filler_count on purpose.
    triangle_lines = ["1 2 3 2"] + ["1 1 1 0"] * filler_count
    lines += ["", "Triangles", str(len(triangle_lines))] + triangle_lines
    mesh_path = FNAMES.mesh_file(tile.build_dir, lat, lon)
    with open(mesh_path, "w") as mesh_file:
        mesh_file.write("\n".join(lines) + "\n")
    return mesh_path


def test_record_water_tris_handles_sub_100_triangle_mesh(data_root):
    """Regression: record_water_tris computes its progress step as
    nbr_tri_in // 100 — a mesh with fewer than 100 triangles used to
    raise ZeroDivisionError in the ``i % step_stones`` modulo (both the
    sea pass and, with use_masks_for_inland False, the inland pass).
    The clamp is now ``max(1, nbr_tri_in // 100)`` at both sites."""
    UI.red_flag = False
    tile = _make_tile(data_root, use_masks_for_inland=False)
    _write_minimal_mesh(tile, filler_count=2)  # 3 triangles total

    (dico_sea, dico_inland, coastline_sea_present) = (
        MASK.record_water_tris(tile)
    )

    assert dico_sea  # the sea triangle was still attributed
    assert dico_inland == {}
    assert coastline_sea_present  # pure SEA drives the marine flag


# =====================================================================
# 2. masks_use_DEM_too tri-state resolution (spec section 4.1)
# =====================================================================
def test_build_masks_returns_zero_before_band_fetch_when_mesh_missing(
    data_root, monkeypatch
):
    """The mesh-existence guard short-circuits BEFORE the band fetch: a
    broken tile must never trigger a bathymetry download."""
    calls = []
    monkeypatch.setattr(
        BATHYBAND, "ensure_bathymetry_band",
        lambda tile, **keyword_arguments: calls.append(tile) or None,
    )
    tile = _make_tile(data_root, masks_use_DEM_too="auto")
    # No mesh written -> mesh_file() does not exist.
    assert not os.path.exists(
        FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)
    )

    result = MASK.build_masks(tile)

    assert result == 0
    assert calls == []  # the band fetch was never reached


class _RecordingDEM:
    """Stand-in for O4_DEM_Utils.DEM: records construction and offers the
    build_dem_pre_mask contract (super_level_set -> empty array)."""

    instances = []

    def __init__(self, *args, **kwargs):
        _RecordingDEM.instances.append((args, kwargs))

    def super_level_set(self, level, bbox):
        (lonmin, lonmax, latmin, latmax) = bbox
        return (
            (lonmin, lonmax, latmin, latmax),
            numpy.zeros((4, 4), dtype=bool),
        )


@pytest.mark.parametrize(
    "setting, expect_band_fetch, expect_dem_load",
    [
        ("auto", True, False),   # engages iff a band exists; no legacy DEM
        ("False", False, False),  # pure vector fade, nothing fetched
        ("True", True, True),     # legacy custom-DEM refinement + band
    ],
)
def test_build_masks_dem_too_tristate_resolution(
    data_root, monkeypatch, setting, expect_band_fetch, expect_dem_load
):
    """"auto"/"True"/"False" map to (band-fetch?, custom-DEM-load?) per
    spec 4.1.  Exercised through build_masks with the water triangles
    stubbed out so only the resolution logic runs."""
    band_calls = []
    monkeypatch.setattr(
        BATHYBAND, "ensure_bathymetry_band",
        lambda tile, **keyword_arguments: band_calls.append(
            keyword_arguments
        ) or None,  # no band available
    )
    _RecordingDEM.instances = []
    monkeypatch.setattr(MASK.DEM, "DEM", _RecordingDEM)
    # No mesh squares to process: the tri-state logic runs, the parallel
    # build immediately drains an empty queue.
    monkeypatch.setattr(
        MASK, "record_water_tris", lambda tile: ({}, {}, False))

    tile = _make_tile(data_root, masks_use_DEM_too=setting)
    _write_minimal_mesh(tile)  # mesh must exist to pass the guard

    MASK.build_masks(tile)

    assert (len(band_calls) == 1) is expect_band_fetch
    assert (len(_RecordingDEM.instances) == 1) is expect_dem_load
    if expect_band_fetch:
        # "auto" applies the fine-nearshore gate and never fetches
        # intertidal-only sources; explicit "True" is the only opt-in.
        assert band_calls[0]["fine_nearshore_only"] is (setting == "auto")
        assert band_calls[0]["intertidal_ok"] is (setting == "True")


# =====================================================================
# 3. Offshore-shallow full-sea square is still written (spec section 4.2)
# =====================================================================
def test_full_sea_square_written_when_depth_ramp_nonzero(
    data_root, monkeypatch
):
    """A mask square whose vector pre-mask is entirely sea (empty) but
    whose bathymetry alpha is non-zero must still produce a mask PNG —
    the legacy early-return has to consult the depth ramp."""
    tile = _make_tile(data_root, masks_use_DEM_too="auto")
    _write_minimal_mesh(tile)

    # The square holding the giant sea triangle's barycentre.
    barycentre_lat = tile.lat + (0.05 + 0.05 + 0.95) / 3
    barycentre_lon = tile.lon + (0.05 + 0.95 + 0.50) / 3
    (til_x, til_y) = GEO.wgs84_to_orthogrid(
        barycentre_lat, barycentre_lon, MASK_ZL
    )

    # A uniform shallow (-5 m -> alpha well above zero) band covering just
    # that square's window.
    window = _mask_square_window(til_x, til_y, MASK_ZL)
    (lonmin, latmin, lonmax, latmax) = window
    margin = 0.02
    columns, rows = 64, 64
    raster_path = str(data_root / "shallow_band.tif")
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(raster_path, columns, rows, 1, gdal.GDT_Float32)
    west, east = lonmin - margin, lonmax + margin
    north, south = latmax + margin, latmin - margin
    dataset.SetGeoTransform(
        (west, (east - west) / columns, 0, north, 0, (south - north) / rows)
    )
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.WriteArray(numpy.full((rows, columns), -5.0, dtype=numpy.float32))
    band.SetNoDataValue(-32768.0)
    band.FlushCache()
    dataset = None

    monkeypatch.setattr(
        BATHYBAND, "ensure_bathymetry_band", lambda tile, **keyword_arguments: raster_path
    )

    MASK.build_masks(tile)

    mask_png = os.path.join(
        FNAMES.mask_dir(tile.lat, tile.lon),
        FNAMES.legacy_mask(til_x, til_y),
    )
    assert os.path.isfile(mask_png), (
        "the full-sea square carrying the shallow depth ramp must be "
        "written despite an empty vector pre-mask"
    )


# =====================================================================
# 4. Shallow-water fallback loading alongside the airport-gated band
#    (spec sections 3 + 4.4, ruling 2026-07-16)
# =====================================================================
@pytest.mark.parametrize(
    "setting, band_available, radius, expect_fallback_load",
    [
        # Auto + gated (partial) band: the fallback loads and fills the
        # squares beyond the airport ring.
        ("auto", True, 20.0, True),
        # Auto + radius 0 = ungated full band: measured data covers the
        # whole shoreline, nothing to fill.
        ("auto", True, 0.0, False),
        # Explicit True never gates, so never needs the fill.
        ("True", True, 20.0, False),
        # No band at all: the pre-existing fallback rule, unchanged.
        ("auto", False, 20.0, True),
    ],
)
def test_shallow_water_fallback_loads_alongside_gated_band(
    data_root, monkeypatch, setting, band_available, radius,
    expect_fallback_load,
):
    """The mapped shallow-water fallback loads exactly when squares can
    exist that measured bathymetry deliberately left bare: alongside an
    airport-gated band, or with no band at all."""
    band_vrt = str(data_root / "band.vrt") if band_available else None
    monkeypatch.setattr(
        BATHYBAND,
        "ensure_bathymetry_band",
        lambda tile, **keyword_arguments: band_vrt,
    )
    monkeypatch.setattr(MASK.DEM, "DEM", _RecordingDEM)
    _RecordingDEM.instances = []
    fallback_loads = []
    monkeypatch.setattr(
        MASK,
        "load_shallow_water_polygons",
        lambda tile: fallback_loads.append(tile) or None,
    )
    # One out-of-range sea square: the tile counts as coastal for the
    # landlocked gate (an empty dico_sea skips the fallback entirely),
    # but build_mask returns immediately on it, so only the resolution
    # logic actually runs.
    monkeypatch.setattr(
        MASK, "record_water_tris", lambda tile: ({(0, 0): []}, {}, True))

    tile = _make_tile(
        data_root,
        masks_use_DEM_too=setting,
        bathymetry_airport_radius_km=radius,
    )
    _write_minimal_mesh(tile)

    MASK.build_masks(tile)

    assert (len(fallback_loads) == 1) is expect_fallback_load
