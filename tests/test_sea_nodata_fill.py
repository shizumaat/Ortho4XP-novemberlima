"""Tests for ``O4_Sea_Nodata_Fill`` — provider nodata repair over sea.

All tests are headless and in-memory: synthetic noisy-sea images with
saturated white/black rectangles standing in for provider nodata holes.  They
exercise the detection lock (saturated AND flat AND large AND mostly-water),
the byte-identity guarantee outside the hole, colour plausibility of the fill,
and determinism under a fixed seed.
"""

import os
import sys

import numpy
import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from O4_Sea_Nodata_Fill import detect_sea_nodata, fill_sea_nodata  # noqa: E402


IMAGE_SIZE = 800


def make_sea_base(mean, size=IMAGE_SIZE, sigma=6.0, seed=1234):
    """Return a uint8 (size, size, 3) noisy-sea image around ``mean``."""
    generator = numpy.random.default_rng(seed)
    base = generator.normal(
        loc=numpy.array(mean, dtype=numpy.float32),
        scale=sigma,
        size=(size, size, 3),
    )
    return numpy.clip(base, 0, 255).astype(numpy.uint8)


def rectangle_slice(size=IMAGE_SIZE, fraction=0.20):
    """Return (row_slice, column_slice) for a centred square whose area is
    ``fraction`` of the image (comfortably above the 2% size floor)."""
    side = int(round(size * (fraction ** 0.5)))
    start = (size - side) // 2
    stop = start + side
    return slice(start, stop), slice(start, stop)


def test_clean_sea_returns_none():
    array = make_sea_base((40, 70, 110))
    assert fill_sea_nodata(Image.fromarray(array)) is None
    assert detect_sea_nodata(array) is None


def test_white_rectangle_is_filled():
    array = make_sea_base((40, 70, 110))
    rows, columns = rectangle_slice()
    array[rows, columns] = 255
    original = array.copy()

    nodata = detect_sea_nodata(array)
    assert nodata is not None
    # The mask covers the rectangle and extends past it (halo).
    assert nodata[rows, columns].all()
    assert int(nodata.sum()) > int(
        (rows.stop - rows.start) * (columns.stop - columns.start)
    )

    result = fill_sea_nodata(Image.fromarray(array))
    assert result is not None
    filled = numpy.asarray(result)

    # No pixel inside the original rectangle stays saturated white.
    interior = filled[rows, columns]
    assert not (
        (interior[:, :, 0] > 240)
        & (interior[:, :, 1] > 240)
        & (interior[:, :, 2] > 240)
    ).any()

    # Pixels outside the halo-expanded mask are byte-identical to the input.
    outside = ~nodata
    assert numpy.array_equal(filled[outside], original[outside])

    # Filled colour resembles the surrounding sea.
    surrounding = original[outside & _luminance_below(original, 190)]
    surrounding_mean = surrounding.reshape(-1, 3).mean(axis=0)
    fill_mean = filled[rows, columns].reshape(-1, 3).mean(axis=0)
    assert numpy.all(numpy.abs(fill_mean - surrounding_mean) < 30)


def test_black_rectangle_is_filled():
    # Base clearly above the dark ceiling in every channel so genuine sea never
    # registers as black nodata.
    array = make_sea_base((90, 110, 140))
    rows, columns = rectangle_slice()
    array[rows, columns] = 0
    original = array.copy()

    nodata = detect_sea_nodata(array)
    assert nodata is not None
    assert nodata[rows, columns].all()

    result = fill_sea_nodata(Image.fromarray(array))
    assert result is not None
    filled = numpy.asarray(result)

    interior = filled[rows, columns]
    assert not (
        (interior[:, :, 0] < 70)
        & (interior[:, :, 1] < 70)
        & (interior[:, :, 2] < 70)
    ).any()

    outside = ~nodata
    assert numpy.array_equal(filled[outside], original[outside])

    surrounding = original[outside & _luminance_below(original, 190)]
    surrounding_mean = surrounding.reshape(-1, 3).mean(axis=0)
    fill_mean = filled[rows, columns].reshape(-1, 3).mean(axis=0)
    assert numpy.all(numpy.abs(fill_mean - surrounding_mean) < 30)


def test_small_white_square_is_ignored():
    array = make_sea_base((40, 70, 110))
    # 40x40 = 1600 px, well under max(5000, 2% of 640000) = 12800.
    array[100:140, 100:140] = 255
    assert detect_sea_nodata(array) is None
    assert fill_sea_nodata(Image.fromarray(array)) is None


def test_bright_but_textured_region_is_not_detected():
    array = make_sea_base((40, 70, 110))
    rows, columns = rectangle_slice()
    generator = numpy.random.default_rng(99)
    # Mean ~245 but per-pixel noise sigma ~6 so local std stays above 1.5.
    patch = generator.normal(loc=245.0, scale=6.0, size=(rows.stop - rows.start, columns.stop - columns.start, 3))
    array[rows, columns] = numpy.clip(patch, 0, 255).astype(numpy.uint8)
    assert detect_sea_nodata(array) is None
    assert fill_sea_nodata(Image.fromarray(array)) is None


def test_water_mask_guard():
    array = make_sea_base((40, 70, 110))
    rows, columns = rectangle_slice()
    array[rows, columns] = 255

    # Mask says the rectangle sits on land (255 = opaque land): drop it.
    land_mask = numpy.full((IMAGE_SIZE, IMAGE_SIZE), 255, dtype=numpy.uint8)
    assert detect_sea_nodata(array, water_mask=land_mask) is None
    assert fill_sea_nodata(Image.fromarray(array), water_mask=land_mask) is None

    # Mask says water (0 = transparent water) under the rectangle: fill it.
    water_mask = numpy.zeros((IMAGE_SIZE, IMAGE_SIZE), dtype=numpy.uint8)
    assert detect_sea_nodata(array, water_mask=water_mask) is not None
    assert fill_sea_nodata(Image.fromarray(array), water_mask=water_mask) is not None


def test_determinism_same_seed_byte_identical():
    array = make_sea_base((40, 70, 110))
    rows, columns = rectangle_slice()
    array[rows, columns] = 255
    image = Image.fromarray(array)

    first = numpy.asarray(fill_sea_nodata(image, random_seed=7))
    second = numpy.asarray(fill_sea_nodata(image, random_seed=7))
    assert numpy.array_equal(first, second)


def test_inputs_are_not_mutated():
    array = make_sea_base((40, 70, 110))
    rows, columns = rectangle_slice()
    array[rows, columns] = 255
    array_copy = array.copy()
    mask = numpy.zeros((IMAGE_SIZE, IMAGE_SIZE), dtype=numpy.uint8)
    mask_copy = mask.copy()

    image = Image.fromarray(array)
    fill_sea_nodata(image, water_mask=mask)

    assert numpy.array_equal(array, array_copy)
    assert numpy.array_equal(mask, mask_copy)


def _luminance_below(image_array, ceiling):
    luminance = (
        0.299 * image_array[:, :, 0]
        + 0.587 * image_array[:, :, 1]
        + 0.114 * image_array[:, :, 2]
    )
    return luminance < ceiling
