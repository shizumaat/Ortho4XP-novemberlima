"""Headless tests for ``O4_Color_Harmonization`` (spec section 5).

All fixtures are built in memory with numpy and PIL; there is no tile, no
network, and no X-Plane install involved. The module under test is a pure
deterministic leaf, so every assertion below is a fixed-value check.
"""

import os
import sys

import numpy
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import O4_Color_Harmonization as harmonization  # noqa: E402


# --- Statistics pass -------------------------------------------------------


def test_statistics_known_medians_on_solid_image():
    """A solid-color image yields exactly that color as the channel medians."""
    color = (140, 96, 60)
    image = Image.new("RGB", (128, 128), color)

    statistics = harmonization.compute_texture_color_statistics(image)

    assert statistics is not None
    assert statistics["valid_fraction"] == pytest.approx(1.0)
    assert statistics["channel_medians"] == pytest.approx(list(color), abs=1.0)


def test_statistics_mostly_black_returns_none():
    """An image that is 90% black (luminance <= 10) is excluded (None)."""
    pixels = numpy.zeros((100, 100, 3), dtype=numpy.uint8)
    # Top 10 rows are mid-gray (valid); the remaining 90 rows are black.
    pixels[:10, :, :] = 120
    image = Image.fromarray(pixels, "RGB")

    assert harmonization.compute_texture_color_statistics(image) is None


def test_statistics_excludes_near_white_nodata():
    """Near-white nodata (luminance >= 248) does not enter the medians."""
    # Left half gray-120 (valid), right half white-255 (excluded as nodata).
    pixels = numpy.empty((128, 128, 3), dtype=numpy.uint8)
    pixels[:, :64, :] = 120
    pixels[:, 64:, :] = 255
    image = Image.fromarray(pixels, "RGB")

    statistics = harmonization.compute_texture_color_statistics(image)

    assert statistics is not None
    # Only the gray half survives, so every channel median is ~120, not ~187.
    assert statistics["channel_medians"] == pytest.approx([120, 120, 120], abs=1.0)
    assert statistics["valid_fraction"] == pytest.approx(0.5, abs=0.02)


# --- Target field ----------------------------------------------------------


def _linear_gradient_grid(size: int, step: int) -> dict:
    """Build a ``size x size`` grid keyed by (til_x, til_y) spaced ``step``.

    Each texture's per-channel median is a smooth linear function of its grid
    position, so a neighborhood median tracks the local value closely.
    """
    grid = {}
    for grid_x in range(size):
        for grid_y in range(size):
            key = (grid_x * step, grid_y * step)
            red = 100.0 + grid_x * 3.0
            green = 90.0 + grid_y * 2.0
            blue = 80.0 + (grid_x + grid_y) * 1.0
            grid[key] = numpy.array([red, green, blue])
    return grid


def test_target_field_outlier_pulled_to_consensus():
    """An interior outlier's target tracks its neighborhood, not itself."""
    step = 16
    grid = _linear_gradient_grid(8, step)
    outlier_key = (4 * step, 4 * step)
    consensus_value = grid[outlier_key].copy()
    grid[outlier_key] = numpy.array([250.0, 250.0, 250.0])

    target_field = harmonization.compute_target_field(grid, neighborhood_radius=2)

    # The 5x5 neighborhood has 25 vectors; the lone outlier cannot move the
    # median, so the target sits at the local gradient consensus.
    assert numpy.allclose(target_field[outlier_key], consensus_value, atol=3.0)


def test_target_field_gradient_endpoints_keep_local_value():
    """Corner (endpoint) textures keep targets close to their own value."""
    step = 16
    grid = _linear_gradient_grid(8, step)

    target_field = harmonization.compute_target_field(grid, neighborhood_radius=2)

    for corner_key in [(0, 0), (7 * step, 7 * step)]:
        assert numpy.allclose(target_field[corner_key], grid[corner_key], atol=6.0)


def test_target_field_border_keys_have_clamped_neighborhoods():
    """Every key gets a target (no KeyError) and borders clamp gracefully."""
    step = 16
    grid = _linear_gradient_grid(8, step)

    target_field = harmonization.compute_target_field(grid, neighborhood_radius=2)

    assert set(target_field.keys()) == set(grid.keys())
    for target in target_field.values():
        assert target.shape == (3,)


def test_target_field_infers_step_across_gaps():
    """A hole from an excluded texture keeps neighbors two index-steps apart."""
    step = 16
    grid = _linear_gradient_grid(5, step)
    # Remove one interior texture (excluded / mostly water).
    del grid[(2 * step, 2 * step)]

    target_field = harmonization.compute_target_field(grid, neighborhood_radius=1)

    # The gap key is absent from the field, and the survivors are unaffected.
    assert (2 * step, 2 * step) not in target_field
    assert set(target_field.keys()) == set(grid.keys())


def test_target_field_empty_input():
    """Empty statistics yield an empty target field."""
    assert harmonization.compute_target_field({}) == {}


# --- Shift -----------------------------------------------------------------


@pytest.mark.parametrize(
    "zoomlevel, expected_strength",
    [(13, 0.70), (16, 0.70), (17, 0.40), (18, 0.20), (19, 0.10), (21, 0.10)],
)
def test_shift_schedule_values_exact(zoomlevel, expected_strength):
    """The strength schedule is exact per zoom level (clamped at the ends)."""
    medians = numpy.array([100.0, 100.0, 100.0])
    target = numpy.array([110.0, 110.0, 110.0])

    shift = harmonization.compute_harmonization_shift(medians, target, zoomlevel)

    expected = expected_strength * 10.0
    assert numpy.allclose(shift, [expected, expected, expected])


def test_shift_capped_at_twenty():
    """A huge target difference is capped at +-20 per channel."""
    medians = numpy.array([10.0, 200.0, 100.0])
    target = numpy.array([250.0, 0.0, 100.0])  # +240, -200, 0 raw differences

    shift = harmonization.compute_harmonization_shift(medians, target, 16)

    assert numpy.allclose(shift, [20.0, -20.0, 0.0])


def test_shift_zero_when_medians_equal_target():
    """No shift when the medians already sit on the target."""
    medians = numpy.array([123.0, 45.0, 67.0])

    shift = harmonization.compute_harmonization_shift(medians, medians.copy(), 16)

    assert numpy.allclose(shift, [0.0, 0.0, 0.0])


# --- Apply -----------------------------------------------------------------


def test_apply_matches_direct_arithmetic_rgb():
    """The LUT result equals clip(array + shift) computed directly."""
    rng = numpy.random.RandomState(0)
    pixels = rng.randint(0, 256, size=(64, 64, 3), dtype=numpy.uint8)
    image = Image.fromarray(pixels, "RGB")
    shift = numpy.array([12.0, -7.0, 20.0])

    shifted = harmonization.apply_color_shift(image, shift)

    rounded = numpy.array([12, -7, 20])
    expected = numpy.clip(pixels.astype(numpy.int64) + rounded, 0, 255).astype(
        numpy.uint8
    )
    assert numpy.array_equal(numpy.asarray(shifted), expected)


def test_apply_preserves_alpha_byte_identical():
    """An alpha channel passes through untouched under a color shift."""
    rng = numpy.random.RandomState(1)
    pixels = rng.randint(0, 256, size=(48, 48, 4), dtype=numpy.uint8)
    image = Image.fromarray(pixels, "RGBA")
    shift = numpy.array([15.0, -15.0, 5.0])

    shifted = harmonization.apply_color_shift(image, shift)

    assert shifted.mode == "RGBA"
    original_alpha = numpy.asarray(image)[:, :, 3]
    shifted_alpha = numpy.asarray(shifted)[:, :, 3]
    assert numpy.array_equal(original_alpha, shifted_alpha)


def test_apply_does_not_mutate_input():
    """The input image is left unchanged after applying a shift."""
    rng = numpy.random.RandomState(2)
    pixels = rng.randint(0, 256, size=(32, 32, 3), dtype=numpy.uint8)
    image = Image.fromarray(pixels, "RGB")
    before = numpy.asarray(image).copy()

    harmonization.apply_color_shift(image, numpy.array([20.0, 20.0, 20.0]))

    assert numpy.array_equal(numpy.asarray(image), before)


def test_apply_zero_shift_short_circuits_to_equal_copy():
    """Shifts that round to zero return an equal, distinct copy."""
    rng = numpy.random.RandomState(3)
    pixels = rng.randint(0, 256, size=(32, 32, 3), dtype=numpy.uint8)
    image = Image.fromarray(pixels, "RGB")

    shifted = harmonization.apply_color_shift(image, numpy.array([0.3, -0.2, 0.4]))

    assert shifted is not image
    assert numpy.array_equal(numpy.asarray(shifted), pixels)


# --- End-to-end micro ------------------------------------------------------


def test_end_to_end_two_textures_converge():
    """Two casts sharing a seam converge to within a few counts at strength 0.70."""
    step = 16
    zoomlevel = 16  # strength 0.70

    texture_a = Image.new("RGB", (64, 64), (138, 120, 112))
    texture_b = Image.new("RGB", (64, 64), (126, 128, 124))
    key_a = (0, 0)
    key_b = (step, 0)

    statistics = {
        key_a: harmonization.compute_texture_color_statistics(texture_a),
        key_b: harmonization.compute_texture_color_statistics(texture_b),
    }
    medians_by_key = {
        key: numpy.array(value["channel_medians"]) for key, value in statistics.items()
    }

    target_field = harmonization.compute_target_field(medians_by_key)

    shifted_a = harmonization.apply_color_shift(
        texture_a,
        harmonization.compute_harmonization_shift(
            medians_by_key[key_a], target_field[key_a], zoomlevel
        ),
    )
    shifted_b = harmonization.apply_color_shift(
        texture_b,
        harmonization.compute_harmonization_shift(
            medians_by_key[key_b], target_field[key_b], zoomlevel
        ),
    )

    medians_a = numpy.array(
        harmonization.compute_texture_color_statistics(shifted_a)["channel_medians"]
    )
    medians_b = numpy.array(
        harmonization.compute_texture_color_statistics(shifted_b)["channel_medians"]
    )

    assert numpy.allclose(medians_a, medians_b, atol=5.0)
