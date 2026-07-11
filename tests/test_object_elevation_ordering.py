"""Regression tests for the OBJECT PLACEMENT ORDERING stream (spec section 7).

These lock in the ordering guarantees the object-moving paths depend on now
that high-resolution airport elevation insets land in the ``.alt``/mesh:

* O1 -- ``smooth_raster_over_airports`` blurs FIRST and bakes the inset
  LAST, so a synthetic inset value dropped inside the inset footprint
  survives an ACTIVE airport blur untouched (bake-after-smooth).
* O2 -- ``auto_patch.elevation._load_airport_dem`` returns the production
  ``override_dem`` object by IDENTITY, so auto_patch samples the very array
  the inset bake mutated in place (no pre-bake copy).
* O3 -- ``post_mesh._mesh_is_newer_than_alt`` is the loud ordering guard:
  a mesh older than the tile's ``.alt`` is refused so objects are never
  seated against a stale surface, and ``rebake_dsf_objects`` skips on it.

No network is used: every raster is a synthetic in-memory GeoTIFF, and the
mesh/.alt files are empty stand-ins whose mtimes are set with ``os.utime``.
GDAL-dependent tests skip cleanly when ``osgeo`` is unavailable.
"""

import json
import os
import types

import numpy
import pytest

import O4_File_Names as FNAMES
import O4_Airport_Elevation_Insets as INSETS
import O4_Airport_Utils as APT

try:
    from osgeo import gdal, osr

    HAS_GDAL = True
except Exception:
    HAS_GDAL = False

requires_gdal = pytest.mark.skipif(
    not HAS_GDAL, reason="osgeo (GDAL python bindings) not available"
)

from shapely.geometry import Polygon, box


def _write_constant_geotiff(
    path, west, south, east, north, value, columns=40, rows=40
):
    """Write a constant-value EPSG:4326 float32 GeoTIFF with nodata set."""
    driver = gdal.GetDriverByName("GTiff")
    dataset = driver.Create(path, columns, rows, 1, gdal.GDT_Float32)
    pixel_width = (east - west) / columns
    pixel_height = (south - north) / rows  # negative
    dataset.SetGeoTransform((west, pixel_width, 0, north, 0, pixel_height))
    spatial_reference = osr.SpatialReference()
    spatial_reference.ImportFromEPSG(4326)
    dataset.SetProjection(spatial_reference.ExportToWkt())
    band = dataset.GetRasterBand(1)
    band.SetNoDataValue(-9999.0)
    band.WriteArray(numpy.full((rows, columns), value, dtype=numpy.float32))
    band.FlushCache()
    dataset = None


class _SmoothTile:
    """Minimal tile carrying just what ``smooth_raster_over_airports`` and
    the inset bake read."""

    def __init__(self, lat, lon, build_dir):
        self.lat = lat
        self.lon = lon
        self.dem = None
        self.build_dir = build_dir
        self.iterate = 0
        self.custom_dem = ""
        self.apt_smoothing_pix = 8
        # Force the LEGACY fixed radius so a real blur is active -- this is
        # what makes the test sensitive to bake-vs-smooth ordering.
        self.apt_smoothing_auto = False
        self.airport_elevation_insets = True
        self.airport_elevation_providers = "auto"
        self.airport_elevation_inset_resolution_m = 3.0
        self.airport_elevation_inset_margin_m = 1000.0
        self.airport_elevation_inset_feather_m = 60.0


# =====================================================================
# O1 -- inset survives the airport smoother (bake runs AFTER smoothing)
# =====================================================================
@requires_gdal
def test_inset_survives_active_airport_smoothing(tmp_path, monkeypatch):
    import O4_DEM_Utils as DEM

    monkeypatch.setattr(FNAMES, "Elevation_dir", str(tmp_path))
    monkeypatch.setattr(INSETS, "has_gdal", True)
    INSETS.initialize_elevation_providers_dict()

    tile_latitude, tile_longitude = 0, 0

    # Flat 0 m base raster over the whole tile-relative unit square.
    base_path = str(tmp_path / "base.tif")
    _write_constant_geotiff(
        base_path, 0.0, 0.0, 1.0, 1.0, 0.0, columns=301, rows=301
    )
    base_dem = DEM.DEM(
        tile_latitude, tile_longitude, base_path, fill_nodata=False
    )

    # A SMALL flat 100 m inset (~4 km) placed in the tile cache dir.  Small
    # relative to the 8-pixel blur (~24 px * ~370 m) so that, were the bake
    # to run BEFORE the smoother, the plateau centre would be pulled well
    # below 100 -- the property this test guards.
    inset_directory = FNAMES.airport_inset_directory(
        tile_latitude, tile_longitude
    )
    os.makedirs(inset_directory, exist_ok=True)
    inset_path = FNAMES.airport_inset_dem(
        tile_latitude, tile_longitude, "TEST", "USGS3DEP"
    )
    _write_constant_geotiff(
        inset_path, 0.48, 0.48, 0.52, 0.52, 100.0, columns=60, rows=60
    )

    build_directory = tmp_path / "build"
    build_directory.mkdir()
    tile = _SmoothTile(tile_latitude, tile_longitude, str(build_directory))
    tile.dem = base_dem

    # One airport whose smoothing mask covers the inset; empty runway/
    # hangar/taxiway/apron geometries are enough for the union.
    dico_airports = {
        "TEST": {
            "name": "TEST",
            "boundary": box(0.30, 0.30, 0.70, 0.70),
            "runway": (Polygon(),),
            "hangar": Polygon(),
            "taxiway": (Polygon(),),
            "apron": (Polygon(),),
        }
    }

    APT.smooth_raster_over_airports(tile, dico_airports)

    # Read back the WRITTEN .alt (the mesher's input), not just the array.
    alt_path = FNAMES.alt_file(tile)
    written = numpy.fromfile(alt_path, dtype=numpy.float32).reshape(
        (base_dem.nydem, base_dem.nxdem)
    )

    number_of_columns = base_dem.nxdem
    number_of_rows = base_dem.nydem

    def cell(longitude_fraction, latitude_fraction):
        column = int(
            round(
                (longitude_fraction - base_dem.x0)
                / (base_dem.x1 - base_dem.x0)
                * (number_of_columns - 1)
            )
        )
        row = int(
            round(
                (base_dem.y1 - latitude_fraction)
                / (base_dem.y1 - base_dem.y0)
                * (number_of_rows - 1)
            )
        )
        return written[row, column]

    # The inset plateau centre is pristine: the blur ran BEFORE the bake, so
    # the 100 m value was stamped after all smoothing and never smeared.
    assert cell(0.50, 0.50) == pytest.approx(100.0, abs=0.5)
    # Base well outside the inset stays 0 (a flat field blurs to itself).
    assert cell(0.10, 0.10) == pytest.approx(0.0, abs=0.01)


# =====================================================================
# O2 -- production override_dem is returned by identity (no pre-bake copy)
# =====================================================================
def test_load_airport_dem_returns_override_by_identity():
    from auto_patch import elevation

    sentinel = object()
    returned = elevation._load_airport_dem(36.1, -86.7, override_dem=sentinel)
    # IDENTITY, not equality: auto_patch samples the very DEM object the
    # inset bake mutated in place -- never a re-loaded / re-smoothed copy.
    assert returned is sentinel


# =====================================================================
# O3 -- the mesh-newer-than-.alt ordering guard
# =====================================================================
def _touch(path, mtime):
    with open(path, "w") as handle:
        handle.write("x")
    os.utime(path, (mtime, mtime))


def _tile_for_alt(tmp_path):
    build_directory = tmp_path / "Tiles"
    build_directory.mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(
        lat=36, lon=-87, build_dir=str(build_directory), iterate=0
    )


def test_mesh_newer_than_alt_true_when_mesh_is_newer(tmp_path):
    from auto_patch import post_mesh

    tile = _tile_for_alt(tmp_path)
    stub = os.path.join(
        tile.build_dir, "Data" + FNAMES.short_latlon(tile.lat, tile.lon)
    )
    _touch(stub + ".alt", mtime=1000.0)
    mesh_path = stub + ".mesh"
    _touch(mesh_path, mtime=2000.0)
    assert post_mesh._mesh_is_newer_than_alt(tile, mesh_path) is True


def test_mesh_newer_than_alt_false_when_mesh_is_stale(tmp_path):
    from auto_patch import post_mesh

    tile = _tile_for_alt(tmp_path)
    stub = os.path.join(
        tile.build_dir, "Data" + FNAMES.short_latlon(tile.lat, tile.lon)
    )
    # .alt rebuilt (insets baked) AFTER the mesh -> mesh is stale.
    mesh_path = stub + ".mesh"
    _touch(mesh_path, mtime=1000.0)
    _touch(stub + ".alt", mtime=2000.0)
    # Also cover the iterative-densify .alt variant being the newest.
    _touch(stub + ".3.alt", mtime=3000.0)
    assert post_mesh._mesh_is_newer_than_alt(tile, mesh_path) is False


def test_mesh_newer_than_alt_true_when_no_alt_present(tmp_path):
    from auto_patch import post_mesh

    tile = _tile_for_alt(tmp_path)
    stub = os.path.join(
        tile.build_dir, "Data" + FNAMES.short_latlon(tile.lat, tile.lon)
    )
    mesh_path = stub + ".mesh"
    _touch(mesh_path, mtime=1000.0)
    # No .alt on disk -> nothing to be stale against -> mesh authoritative.
    assert post_mesh._mesh_is_newer_than_alt(tile, mesh_path) is True


def test_rebake_skips_on_stale_mesh(tmp_path, monkeypatch):
    """A stale mesh must trip the guard and RETURN before touching any
    airport -- proven by a bogus (nonexistent) DSF that would otherwise
    raise and bump ``airports_failed``."""
    from auto_patch import post_mesh
    from auto_patch import config

    monkeypatch.setattr(config, "DSF_OBJECT_REANCHOR", True)

    patches_directory = tmp_path / "Patches"
    patches_directory.mkdir(parents=True)
    monkeypatch.setattr(
        FNAMES, "patch_dir",
        lambda latitude, longitude: str(patches_directory),
    )

    build_directory = tmp_path / "Tiles"
    build_directory.mkdir(parents=True)
    tile = types.SimpleNamespace(
        lat=36, lon=-87, build_dir=str(build_directory), iterate=0
    )
    stub = os.path.join(
        tile.build_dir, "Data" + FNAMES.short_latlon(tile.lat, tile.lon)
    )
    mesh_path = FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)
    _touch(mesh_path, mtime=1000.0)
    _touch(stub + ".alt", mtime=2000.0)  # newer -> mesh is stale

    worklist_path = os.path.join(
        str(patches_directory), post_mesh.OBJECT_ANCHOR_WORKLIST_FILENAME
    )
    with open(worklist_path, "w") as handle:
        json.dump(
            {
                "version": post_mesh.OBJECT_ANCHOR_WORKLIST_VERSION,
                "tile": "+36-087",
                "xplane_root": None,
                "airports": [
                    {
                        "icao": "KTST",
                        "dsf_path": "/nonexistent/never.dsf",
                        "dsf_mtime": None,
                        "pack_root": "/nonexistent/pack",
                        "xplane_root": None,
                    }
                ],
            },
            handle,
        )

    counts = post_mesh.rebake_dsf_objects(tile)
    # Guard fired: the airport was never reached, so no failure was counted.
    assert counts["airports_failed"] == 0
    assert counts["airports_processed"] == 0
