"""Tests for the color-harmonization orchestration in O4_Imagery_Utils.

Covers the pipeline-side wiring around the pure O4_Color_Harmonization
module (see docs/specs/color-harmonization-spec.md section 3.4): statistics
collection from cached source JPEGs, target-field computation grouped by
(zoomlevel, provider), and the per-texture shift lookup used by
convert_texture.

All headless: providers and JPEG path helpers are monkeypatched so the
"downloaded" textures are tiny synthetic JPEGs in tmp_path.
"""

import types

import numpy
import pytest
from PIL import Image

import O4_Imagery_Utils as IMG


PROVIDER_CODE = "FAKEPROV"
ZOOMLEVEL = 16


@pytest.fixture()
def harmonization_tile(monkeypatch, tmp_path):
    """A minimal tile plus a fake provider whose JPEGs live in tmp_path."""
    monkeypatch.setitem(
        IMG.providers_dict, PROVIDER_CODE, {"code": PROVIDER_CODE}
    )
    monkeypatch.setattr(
        IMG.FNAMES,
        "jpeg_file_dir_from_attributes",
        lambda lat, lon, zoomlevel, provider: str(tmp_path),
    )
    monkeypatch.setattr(
        IMG.FNAMES,
        "jpeg_file_name_from_attributes",
        lambda til_x, til_y, zoomlevel, provider_code: (
            f"{til_y}_{til_x}_{provider_code}{zoomlevel}.jpg"
        ),
    )
    tile = types.SimpleNamespace(lat=45, lon=-64, color_harmonization=True)
    IMG.initialize_color_harmonization(tile)

    def write_texture_jpeg(til_x, til_y, color):
        image = Image.new("RGB", (64, 64), color)
        image.save(
            tmp_path
            / f"{til_y}_{til_x}_{PROVIDER_CODE}{ZOOMLEVEL}.jpg",
            quality=95,
        )

    return tile, write_texture_jpeg


def test_statistics_collection_and_shift_pull_textures_together(
    harmonization_tile,
):
    tile, write_texture_jpeg = harmonization_tile
    write_texture_jpeg(0, 0, (100, 110, 120))
    write_texture_jpeg(16, 0, (140, 110, 100))
    for til_x in (0, 16):
        IMG.collect_color_statistics_for_harmonization(
            tile, til_x, 0, ZOOMLEVEL, PROVIDER_CODE
        )
    assert len(tile.color_harmonization_statistics) == 2

    IMG.compute_color_harmonization_targets(tile)
    assert len(tile.color_harmonization_targets) == 2

    shift_first = IMG.color_harmonization_shift_for_texture(
        tile, 0, 0, ZOOMLEVEL, PROVIDER_CODE
    )
    shift_second = IMG.color_harmonization_shift_for_texture(
        tile, 16, 0, ZOOMLEVEL, PROVIDER_CODE
    )
    # Both textures share the same neighborhood target (their joint median),
    # so at ZL16 strength 0.70 the red shifts pull toward each other:
    # first texture up by ~0.7 * 20, second down by the same amount.
    assert shift_first is not None and shift_second is not None
    assert 10 <= shift_first[0] <= 18
    assert -18 <= shift_second[0] <= -10
    # Green is identical on both textures: that channel's shift is ~0.
    assert abs(shift_first[1]) <= 2 and abs(shift_second[1]) <= 2


def test_shift_is_none_without_feature_or_statistics(harmonization_tile):
    tile, write_texture_jpeg = harmonization_tile
    write_texture_jpeg(0, 0, (100, 110, 120))
    IMG.collect_color_statistics_for_harmonization(
        tile, 0, 0, ZOOMLEVEL, PROVIDER_CODE
    )
    IMG.compute_color_harmonization_targets(tile)
    # A texture that was never downloaded has no target.
    assert (
        IMG.color_harmonization_shift_for_texture(
            tile, 32, 0, ZOOMLEVEL, PROVIDER_CODE
        )
        is None
    )
    # A tile that never activated the feature has no targets attribute.
    bare_tile = types.SimpleNamespace(lat=45, lon=-64)
    assert (
        IMG.color_harmonization_shift_for_texture(
            bare_tile, 0, 0, ZOOMLEVEL, PROVIDER_CODE
        )
        is None
    )


def test_single_texture_tile_gets_zero_shift(harmonization_tile):
    """With one texture the neighborhood target IS its own median, so the
    shift rounds to zero and the lookup returns None (no PNG round-trip)."""
    tile, write_texture_jpeg = harmonization_tile
    write_texture_jpeg(0, 0, (100, 110, 120))
    IMG.collect_color_statistics_for_harmonization(
        tile, 0, 0, ZOOMLEVEL, PROVIDER_CODE
    )
    IMG.compute_color_harmonization_targets(tile)
    assert (
        IMG.color_harmonization_shift_for_texture(
            tile, 0, 0, ZOOMLEVEL, PROVIDER_CODE
        )
        is None
    )


def test_missing_jpeg_and_unknown_provider_are_skipped(harmonization_tile):
    tile, _ = harmonization_tile
    IMG.collect_color_statistics_for_harmonization(
        tile, 48, 0, ZOOMLEVEL, PROVIDER_CODE
    )
    IMG.collect_color_statistics_for_harmonization(
        tile, 0, 0, ZOOMLEVEL, "NOT_A_PROVIDER"
    )
    assert tile.color_harmonization_statistics == {}
    IMG.compute_color_harmonization_targets(tile)
    assert tile.color_harmonization_targets == {}
