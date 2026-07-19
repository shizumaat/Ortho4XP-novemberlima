"""Residual structure masking for surface-model elevation insets.

The footprint mask can only erase MAPPED buildings; unmapped structures
(dense city blocks with no OpenStreetMap coverage — the SPJC east-side
mounds, 2026-07-18) survive it.  The residual mask flags pixels standing
above a morphological-opening ground estimate and heals them through the
same source-agnostic fill.

Hermetic: synthetic rasters only, no network, no airport builds.
"""

import json
import os

import numpy
import pytest

import O4_Airport_Elevation_Insets as INSETS

try:
    from osgeo import gdal

    HAS_GDAL = True
except Exception:
    HAS_GDAL = False

requires_gdal = pytest.mark.skipif(
    not HAS_GDAL, reason="osgeo (GDAL python bindings) not available"
)


PIXEL_SIZE_M = 3.0
NO_EXCLUDE = None


def _mask(values, exclude=None):
    if exclude is None:
        exclude = numpy.zeros(values.shape, dtype=bool)
    return INSETS._residual_structure_mask(values, exclude, PIXEL_SIZE_M)


class TestResidualStructureMask:
    def test_plane_and_slope_are_untouched(self):
        rows, columns = numpy.mgrid[0:120, 0:120]
        sloped = 25.0 + 0.04 * PIXEL_SIZE_M * columns   # a lawful 4 % hill
        assert not _mask(sloped).any()

    def test_narrow_bump_is_masked_surroundings_are_not(self):
        values = numpy.full((120, 120), 25.0)
        values[50:60, 50:60] += 5.0        # 30 m unmapped structure
        mask = _mask(values)
        assert mask[52:58, 52:58].all()
        assert not mask[:30, :30].any()

    def test_bump_on_a_slope_is_still_masked(self):
        rows, columns = numpy.mgrid[0:120, 0:120]
        values = 25.0 + 0.04 * PIXEL_SIZE_M * columns
        values[50:60, 50:60] += 5.0
        mask = _mask(values)
        assert mask[52:58, 52:58].all()
        assert mask.sum() <= 600            # only the bump neighbourhood

    def test_gradual_flank_terrain_is_preserved_regardless_of_width(self):
        # A wide mesa with 6 % ramped flanks: natural terrain (the
        # steepness clause protects flanks up to ~6 %; steeper
        # cliff-flanked mesas accept bounded shoulder nibbling by
        # design).  Nothing masks anywhere.
        values = numpy.full((300, 300), 25.0)
        ramp = numpy.clip((numpy.arange(300) - 30) * 0.18, 0.0, 5.0)
        ramp = numpy.minimum(
            ramp, numpy.clip((270 - numpy.arange(300)) * 0.18, 0.0, 5.0))
        values += ramp[numpy.newaxis, :]
        assert not _mask(values).any()

    def test_cliff_edged_city_plateau_erodes_within_bounded_reach(self):
        # Contiguous vertical-edged plateau far wider than the window (the
        # dense-city rooftop fabric): the iterative passes recede its edge
        # from the true-ground side, but the DEEP interior beyond the pass
        # reach must survive (bounded flattening by design).
        values = numpy.full((300, 500), 25.0)
        values[:, 200:] += 6.0            # 900 m of solid "rooftops"
        mask = _mask(values)
        # Interior rows only: the estimator's border band (half a window,
        # ~17 full-resolution pixels) never masks by design.
        assert mask[30:270, 200:230].all()   # near edge: recovered
        assert not mask[:, 420:].any()       # deep city: out of reach

    def test_sub_threshold_bump_is_not_masked(self):
        values = numpy.full((120, 120), 25.0)
        values[50:60, 50:60] += 1.5        # under the 2 m threshold
        assert not _mask(values).any()

    def test_nodata_cells_never_mask_and_never_poison(self):
        values = numpy.full((120, 120), 25.0)
        exclude = numpy.zeros(values.shape, dtype=bool)
        values[0:20, 0:20] = -32768.0
        exclude[0:20, 0:20] = True
        values[50:60, 50:60] += 5.0
        mask = _mask(values, exclude)
        assert not mask[0:20, 0:20].any()
        assert mask[52:58, 52:58].all()


@requires_gdal
class TestMaskingPassIntegration:
    def _write_geotiff(self, path, values):
        driver = gdal.GetDriverByName("GTiff")
        dataset = driver.Create(
            path, values.shape[1], values.shape[0], 1, gdal.GDT_Float32
        )
        # ~3 m pixels at the equator keeps the maths simple.
        degree_step = 3.0 / 111320.0
        dataset.SetGeoTransform((0.0, degree_step, 0.0, 0.0, 0.0,
                                 -degree_step))
        band = dataset.GetRasterBand(1)
        band.SetNoDataValue(-32768.0)
        band.WriteArray(values.astype(numpy.float32))
        band.FlushCache()
        dataset = None

    def test_unmapped_bump_is_flattened_without_any_footprint(
        self, tmp_path, monkeypatch
    ):
        values = numpy.full((150, 150), 25.0)
        values[60:75, 60:75] += 6.0        # unmapped block, no footprint
        inset_path = str(tmp_path / "inset.tif")
        self._write_geotiff(inset_path, values)
        monkeypatch.setattr(
            INSETS,
            "_collect_inset_building_footprints",
            lambda *args, **kwargs: ([], "none"),
        )
        summary = INSETS.mask_building_footprints_in_surface_model(
            inset_path,
            (0.0, -150 * 3.0 / 111320.0, 150 * 3.0 / 111320.0, 0.0),
            {"code": "TEST", INSETS.RESIDUAL_STRUCTURE_MASKING: True},
        )
        assert summary["residual_masked_pixel_count"] > 0
        assert "skipped" not in summary
        dataset = gdal.Open(inset_path)
        healed = dataset.GetRasterBand(1).ReadAsArray()
        dataset = None
        assert float(abs(healed[60:75, 60:75] - 25.0).max()) < 0.5
        assert float(abs(healed[:40, :40] - 25.0).max()) < 0.01

    def test_gate_off_restores_footprint_only_behavior(
        self, tmp_path, monkeypatch
    ):
        values = numpy.full((150, 150), 25.0)
        values[60:75, 60:75] += 6.0
        inset_path = str(tmp_path / "inset.tif")
        self._write_geotiff(inset_path, values)
        monkeypatch.setattr(
            INSETS,
            "_collect_inset_building_footprints",
            lambda *args, **kwargs: ([], "none"),
        )
        summary = INSETS.mask_building_footprints_in_surface_model(
            inset_path,
            (0.0, -150 * 3.0 / 111320.0, 150 * 3.0 / 111320.0, 0.0),
            {"code": "TEST", INSETS.RESIDUAL_STRUCTURE_MASKING: False},
        )
        assert summary.get("skipped") == "no building footprints in the box"
        dataset = gdal.Open(inset_path)
        untouched = dataset.GetRasterBand(1).ReadAsArray()
        dataset = None
        assert float(untouched[60:75, 60:75].max()) > 30.0


class TestSidecarUpgradeDetection:
    def _write_sidecar(self, tmp_path, monkeypatch, payload):
        sidecar = tmp_path / "sidecar.json"
        sidecar.write_text(json.dumps(payload))
        import O4_File_Names as FNAMES

        monkeypatch.setattr(
            FNAMES,
            "airport_inset_provenance",
            lambda lat, lon, icao, code: str(sidecar),
        )

    def test_gate_on_pre_residual_sidecar_reads_stale(
            self, tmp_path, monkeypatch):
        self._write_sidecar(
            tmp_path, monkeypatch,
            {INSETS.SURFACE_MODEL_BUILDING_MASKING: {
                "masked_pixel_count": 10}},
        )
        assert INSETS._sidecar_residual_masking_mismatch(
            0, 0, "TEST", "P", True)

    def test_gate_on_residual_sidecar_reads_current(
            self, tmp_path, monkeypatch):
        self._write_sidecar(
            tmp_path, monkeypatch,
            {INSETS.SURFACE_MODEL_BUILDING_MASKING: {
                "masked_pixel_count": 10,
                "residual_masked_pixel_count": 0}},
        )
        assert not INSETS._sidecar_residual_masking_mismatch(
            0, 0, "TEST", "P", True)

    def test_gate_off_damaged_sidecar_reads_stale(
            self, tmp_path, monkeypatch):
        # The 2026-07-18 live-regression caches: residual pixels were
        # masked but the gate is now OFF — must regenerate clean.
        self._write_sidecar(
            tmp_path, monkeypatch,
            {INSETS.SURFACE_MODEL_BUILDING_MASKING: {
                "masked_pixel_count": 10,
                "residual_masked_pixel_count": 12345}},
        )
        assert INSETS._sidecar_residual_masking_mismatch(
            0, 0, "TEST", "P", False)

    def test_gate_off_clean_sidecar_reads_current(
            self, tmp_path, monkeypatch):
        self._write_sidecar(
            tmp_path, monkeypatch,
            {INSETS.SURFACE_MODEL_BUILDING_MASKING: {
                "masked_pixel_count": 10}},
        )
        assert not INSETS._sidecar_residual_masking_mismatch(
            0, 0, "TEST", "P", False)

    def test_missing_sidecar_reads_current(self, tmp_path, monkeypatch):
        import O4_File_Names as FNAMES

        monkeypatch.setattr(
            FNAMES,
            "airport_inset_provenance",
            lambda lat, lon, icao, code: str(tmp_path / "absent.json"),
        )
        assert not INSETS._sidecar_residual_masking_mismatch(
            0, 0, "TEST", "P", True)
