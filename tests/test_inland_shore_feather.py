"""Inland shore feather (2026-07-17).

Inland water used to be clamped fully opaque in the masks and blended
at one constant alpha in the DSF — razor shorelines on every lagoon and
lake.  The feather: mask pixels over inland water ease from opaque at
the land shoreline down to the constant ``ratio_water`` grey, FLOORED
at that grey (no deep-water transparency can ever appear inside mapped
water), and inland-water terrains whose square carries a mask blend
through it like the masked sea overlay.

Headless: synthetic arrays for the compositor, a stub tile writing a
real ``.ter`` for the border routing.
"""

from __future__ import annotations

import os
import sys

import numpy
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import O4_DSF_Utils as DSF  # noqa: E402
import O4_Mask_Utils as MASK  # noqa: E402

GREY = 200


def _scene(size=96, margin=16):
    """Land block left, inland water middle, sea right; a blurred fade
    standing in for blur_mask output (values only matter over sea)."""
    pre_mask = numpy.zeros((size, size), dtype=numpy.uint8)
    pre_mask[:, : size // 3] = 255              # land
    pre_mask[:, size // 3: 2 * size // 3] = GREY  # inland water
    blured = numpy.full((size, size), 40, dtype=numpy.uint8)  # sea fade
    return (pre_mask, blured, margin)


class TestComposeWaterMask:
    def test_land_is_opaque_and_sea_keeps_the_fade(self):
        (pre_mask, blured, margin) = _scene()
        composed = MASK.compose_water_mask(
            pre_mask, blured, GREY, feather_pixels=8, crop_margin=margin)
        crop = slice(margin, pre_mask.shape[0] - margin)
        land = (pre_mask == 255)[crop, crop]
        sea = (pre_mask == 0)[crop, crop]
        assert (composed[land] == 255).all()
        assert (composed[sea] == 40).all()

    def test_inland_feathers_from_shore_down_to_grey(self):
        (pre_mask, blured, margin) = _scene()
        composed = MASK.compose_water_mask(
            pre_mask, blured, GREY, feather_pixels=8, crop_margin=margin)
        row = composed[32]
        inland_start = 96 // 3 - margin        # first inland column
        inland_end = 2 * 96 // 3 - margin
        shore_value = row[inland_start]
        deep_inland_value = row[inland_end - 4]
        assert shore_value > GREY + 30          # near-opaque at the shore
        assert deep_inland_value == GREY        # settled at the constant
        # Monotone easing between the two.
        segment = row[inland_start:inland_end - 4].astype(int)
        assert (numpy.diff(segment) <= 0).all()

    def test_inland_never_below_the_grey_floor(self):
        (pre_mask, blured, margin) = _scene()
        blured[:] = 0                            # worst case: full fade
        composed = MASK.compose_water_mask(
            pre_mask, blured, GREY, feather_pixels=8, crop_margin=margin)
        crop = slice(margin, pre_mask.shape[0] - margin)
        inland = ((pre_mask > 0) & (pre_mask != 255))[crop, crop]
        assert composed[inland].min() >= GREY

    def test_zero_feather_restores_the_hard_clamp(self):
        (pre_mask, blured, margin) = _scene()
        composed = MASK.compose_water_mask(
            pre_mask, blured, GREY, feather_pixels=0, crop_margin=margin)
        crop = slice(margin, pre_mask.shape[0] - margin)
        inland = ((pre_mask > 0) & (pre_mask != 255))[crop, crop]
        assert (composed[inland] == 255).all()


class _TerrainTile:
    def __init__(self, build_dir):
        self.build_dir = str(build_dir)
        self.imprint_masks_to_dds = False
        self.use_decal_on_terrain = False
        self.terrain_casts_shadows = True
        self.mask_zl = 16


class TestInlandTerrainBorder:
    def _terrain_text(self, tmp_path, mask_border):
        tile = _TerrainTile(tmp_path)
        os.makedirs(os.path.join(str(tmp_path), "textures"), exist_ok=True)
        name = DSF.create_terrain_file(
            tile, "24000_31000_Arc16.dds", 24000, 31000, 16, "Arc",
            1, True, mask_border=mask_border)
        with open(os.path.join(str(tmp_path), "terrain", name)) as ter:
            return ter.read()

    def test_mask_border_references_the_tile_mask(self, tmp_path):
        import O4_File_Names as FNAMES

        text = self._terrain_text(tmp_path, mask_border=True)
        assert "water_transition.png" not in text
        mask_name = FNAMES.mask_file(24000, 31000, 16, "Arc")
        assert "BORDER_TEX ../textures/" + mask_name in text

    def test_default_keeps_the_constant_blend(self, tmp_path):
        text = self._terrain_text(tmp_path, mask_border=False)
        assert "water_transition.png" in text
