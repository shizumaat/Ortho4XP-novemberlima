#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Headless, in-memory tests for O4_Coastal_Foam_Edge.

All masks are synthetic and generated in memory (no files, no network, no
X-Plane install). A "straight shoreline" mask has fully opaque land (255) on the
left half and fully transparent water (0) on the right, with a linear ramp of
width ~2*influence centred on the boundary. That ramp mimics the extent of
``blur_mask`` output whose width the production caller matches by construction.
"""

import os
import sys
import time

import numpy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import O4_Coastal_Foam_Edge as COAST  # noqa: E402


def _influence(foam_width_pixels: int) -> int:
    """Return the module's influence half-width for a foam width."""
    return (
        COAST.NOISE_AMPLITUDE_PIXELS
        + foam_width_pixels
        + COAST.EDGE_MARGIN_PIXELS
    )


def _straight_shoreline_mask(
    size: int, foam_width_pixels: int
) -> "numpy.ndarray":
    """Return a size x size uint8 mask: land (255) left, water (0) right.

    A linear ramp of width ~2*influence is centred on the vertical boundary at
    the middle column, so the transition extent matches the module's band.
    """
    influence = _influence(foam_width_pixels)
    boundary_column = size / 2.0
    columns = numpy.arange(size, dtype=float)
    # +influence at the boundary means fully land; -influence means fully water.
    signed = boundary_column - columns
    ramp = numpy.clip((signed + influence) / (2.0 * influence), 0.0, 1.0) * 255.0
    row = ramp.astype(numpy.uint8)
    return numpy.tile(row, (size, 1))


def test_uniform_and_sliver_masks_return_none():
    size = 1024
    all_land = numpy.full((size, size), 255, dtype=numpy.uint8)
    all_water = numpy.zeros((size, size), dtype=numpy.uint8)
    assert COAST.apply_coastal_foam_edge(all_land, 120, 100) is None
    assert COAST.apply_coastal_foam_edge(all_water, 120, 100) is None

    # A mask with only a thin sliver (< 4%) of water is not coastal enough.
    sliver = numpy.full((size, size), 255, dtype=numpy.uint8)
    sliver_columns = int(size * 0.02)  # 2% water, below the 4% floor
    sliver[:, -sliver_columns:] = 0
    assert COAST.apply_coastal_foam_edge(sliver, 120, 100) is None


def test_returns_uint8_same_shape_without_mutating_input():
    size = 1024
    mask = _straight_shoreline_mask(size, 120)
    original = mask.copy()
    result = COAST.apply_coastal_foam_edge(mask, 120, 100)
    assert result is not None
    assert result.dtype == numpy.uint8
    assert result.shape == mask.shape
    # Input untouched, byte for byte.
    assert numpy.array_equal(mask, original)


def test_far_land_and_far_water_are_saturated():
    size = 1024
    foam_width_pixels = 120
    mask = _straight_shoreline_mask(size, foam_width_pixels)
    result = COAST.apply_coastal_foam_edge(mask, foam_width_pixels, 100)
    assert result is not None

    influence = _influence(foam_width_pixels)
    boundary_column = size // 2
    # Well beyond the influence band on either side.
    far_land_column = boundary_column - influence - 40
    far_water_column = boundary_column + influence + 40
    assert numpy.all(result[:, far_land_column] == 255)
    assert numpy.all(result[:, far_water_column] == 0)


def test_foam_plateau_exists_on_water_side():
    size = 1024
    foam_width_pixels = 120
    sea_transparency_gray = 100
    mask = _straight_shoreline_mask(size, foam_width_pixels)
    result = COAST.apply_coastal_foam_edge(
        mask, foam_width_pixels, sea_transparency_gray
    )
    assert result is not None

    # Sample the water side of the band, just inside the shoreline.
    boundary_column = size // 2
    water_side = result[:, boundary_column + 5 : boundary_column + foam_width_pixels]
    foam_pixels = (water_side > 20) & (water_side < sea_transparency_gray + 25)
    assert foam_pixels.any()


def test_shoreline_is_wavy():
    size = 1024
    foam_width_pixels = 120
    mask = _straight_shoreline_mask(size, foam_width_pixels)
    result = COAST.apply_coastal_foam_edge(mask, foam_width_pixels, 100)
    assert result is not None

    crossing_columns = []
    for row in range(size):
        below = numpy.where(result[row] < 128)[0]
        if below.size:
            crossing_columns.append(int(below[0]))
    assert len(crossing_columns) > size // 2
    # The straight input would give a near-zero standard deviation.
    assert numpy.std(crossing_columns) > 3.0


def test_determinism_same_seed_and_variation_across_seeds():
    size = 1024
    foam_width_pixels = 120
    mask = _straight_shoreline_mask(size, foam_width_pixels)

    first = COAST.apply_coastal_foam_edge(mask, foam_width_pixels, 100, random_seed=7)
    second = COAST.apply_coastal_foam_edge(mask, foam_width_pixels, 100, random_seed=7)
    other = COAST.apply_coastal_foam_edge(mask, foam_width_pixels, 100, random_seed=99)
    assert first is not None and second is not None and other is not None
    assert numpy.array_equal(first, second)
    assert not numpy.array_equal(first, other)


def test_monotone_land_to_water_on_center_line():
    size = 1024
    foam_width_pixels = 120
    mask = _straight_shoreline_mask(size, foam_width_pixels)
    result = COAST.apply_coastal_foam_edge(mask, foam_width_pixels, 100)
    assert result is not None

    center_line = result[size // 2].astype(float)
    window = 25
    window_means = numpy.convolve(
        center_line, numpy.ones(window) / window, mode="valid"
    )
    # Sampled window means should generally decrease from land to water.
    step = window
    samples = window_means[::step]
    decreases = numpy.sum(numpy.diff(samples) <= 0)
    assert decreases >= int(0.9 * (len(samples) - 1))


def test_performance_smoke_on_full_size_mask():
    size = 4096
    foam_width_pixels = 300
    mask = _straight_shoreline_mask(size, foam_width_pixels)
    start = time.monotonic()
    result = COAST.apply_coastal_foam_edge(mask, foam_width_pixels, 100)
    elapsed = time.monotonic() - start
    assert result is not None
    assert result.shape == mask.shape
    # Generous bound so CI never flakes on a shared runner.
    assert elapsed < 5.0


def test_inland_water_plateau_far_from_shoreline_is_preserved():
    """The same mask array carries inland-water plateaus at the sea_level
    gray; anything beyond the coastal band must survive byte-identical."""
    size = 1024
    foam_width_pixels = 120
    influence = _influence(foam_width_pixels)
    mask = _straight_shoreline_mask(size, foam_width_pixels)
    # A large inland lake plateau (constant transparency gray) on the land
    # side: its interior extends beyond the influence band of its own shore.
    plateau_gray = 76
    lake_half_side = influence + 40
    lake_center = (240, 240)
    mask[
        lake_center[0] - lake_half_side : lake_center[0] + lake_half_side,
        lake_center[1] - lake_half_side : lake_center[1] + lake_half_side,
    ] = plateau_gray
    result = COAST.apply_coastal_foam_edge(
        mask,
        foam_width_pixels=foam_width_pixels,
        sea_transparency_gray=100,
        random_seed=11,
    )
    assert result is not None
    # Beyond the band of the lake's own shoreline the plateau is untouched.
    deep_interior = result[
        lake_center[0] - 20 : lake_center[0] + 20,
        lake_center[1] - 20 : lake_center[1] + 20,
    ]
    assert (deep_interior == plateau_gray).all()
    # A small lake (entirely inside its own shoreline band) may only shift
    # mildly thanks to the crossfade — never blow out toward transparent.
    small_mask = _straight_shoreline_mask(size, foam_width_pixels)
    small_mask[100:220, 40:160] = plateau_gray
    small_result = COAST.apply_coastal_foam_edge(
        small_mask,
        foam_width_pixels=foam_width_pixels,
        sea_transparency_gray=100,
        random_seed=11,
    )
    small_center = small_result[150:170, 90:110].astype(int)
    assert numpy.abs(small_center - plateau_gray).max() <= 20


def test_values_blend_into_original_at_band_edges():
    """No discontinuity may appear where the styled band meets the original
    profile: values just inside and just outside the influence boundary must
    differ by only a few gray levels."""
    size = 1024
    foam_width_pixels = 120
    influence = _influence(foam_width_pixels)
    mask = _straight_shoreline_mask(size, foam_width_pixels)
    result = COAST.apply_coastal_foam_edge(
        mask,
        foam_width_pixels=foam_width_pixels,
        sea_transparency_gray=100,
        random_seed=11,
    ).astype(int)
    boundary_column = size // 2
    for edge_column in (
        boundary_column - influence,
        boundary_column + influence,
    ):
        near_inside = result[:, edge_column - 3]
        near_outside = result[:, edge_column + 3]
        step = numpy.abs(near_inside - near_outside)
        assert int(step.max()) <= 12, (
            f"gray step of {int(step.max())} at band edge column {edge_column}"
        )
