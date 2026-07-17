"""Equivalence tests for ``O4_Mask_Utils.triangular_blur_along_axis``.

The sand-mode shoreline blur used to convolve every row and column of a
6144x6144 mask with a ~2*blur_width-tap triangular kernel — 96% of the
masks step's build time on coastal tiles (profiled 2026-07-15).  The
replacement computes the same blur as two O(n) running-mean passes.

Neither the legacy float convolution nor the replacement is bit-stable
where the exact blurred value is an integer (constant plateaus): float
dust puts the result an epsilon either side and the uint8 cast truncates
either way.  The meaningful contract, asserted here against an exact
integer-arithmetic reference, is: every output is the exact floor value
or one below it — the same envelope the legacy code lived in.

Headless: pure numpy, no tile, no network.
"""

import os
import sys

import numpy
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from O4_Mask_Utils import triangular_blur_along_axis  # noqa: E402


def triangular_kernel(blur_width):
    kernel = numpy.array(range(1, 2 * blur_width), dtype=numpy.int64)
    kernel[blur_width:] = range(blur_width - 1, 0, -1)
    return kernel


def exact_blur_along_axis(img_array, blur_width):
    """Floor of the exact rational blur of each ROW (axis=1), computed in
    integer arithmetic — no float error by construction."""
    kernel = triangular_kernel(blur_width)
    out = numpy.empty(img_array.shape, dtype=numpy.int64)
    for i in range(len(img_array)):
        out[i] = (
            numpy.convolve(img_array[i].astype(numpy.int64), kernel, "same")
            // blur_width ** 2
        )
    return out


def legacy_blur_along_axis(img_array, blur_width):
    """One axis of the pre-2026-07-15 sand-mode blur, verbatim."""
    b_img_array = numpy.array(img_array)
    kernel = triangular_kernel(blur_width) / blur_width ** 2
    for i in range(0, len(b_img_array)):
        b_img_array[i] = numpy.convolve(b_img_array[i], kernel, "same")
    return b_img_array


def shoreline_image():
    rng = numpy.random.default_rng(7)
    img = rng.integers(0, 256, size=(200, 300)).astype(numpy.uint8)
    # Hard water/land plateaus, the shape real masks are made of — these
    # produce the exact-integer results that make float truncation flap.
    img[:, :120] = 255
    img[:80, :] = 0
    return img


@pytest.mark.parametrize("blur_width", [30, 31])  # even and odd kernels
def test_within_one_of_exact_result(blur_width):
    img = shoreline_image()
    exact = exact_blur_along_axis(img, blur_width)
    fast = triangular_blur_along_axis(img, blur_width, axis=1).astype(int)
    legacy = legacy_blur_along_axis(img, blur_width).astype(int)

    # Both implementations live in the same {exact-1, exact} envelope.
    assert set(numpy.unique(exact - fast)) <= {0, 1}
    assert set(numpy.unique(exact - legacy)) <= {0, 1}


@pytest.mark.parametrize("blur_width", [30, 31])
def test_composite_blur_matches_legacy_within_truncation(blur_width):
    img = shoreline_image()

    legacy = legacy_blur_along_axis(img, blur_width)
    legacy = legacy_blur_along_axis(legacy.transpose(), blur_width).transpose()

    fast = triangular_blur_along_axis(img, blur_width, axis=1)
    fast = triangular_blur_along_axis(fast, blur_width, axis=0)

    difference = numpy.abs(legacy.astype(int) - fast.astype(int))
    # +/-1 per axis can compound to 2; anything more is a real bug.
    assert difference.max() <= 2
    assert difference.mean() < 0.2


@pytest.mark.parametrize("blur_width", [10, 11])
def test_chunking_does_not_change_the_result(blur_width):
    rng = numpy.random.default_rng(11)
    img = rng.integers(0, 256, size=(130, 90)).astype(numpy.uint8)

    whole = triangular_blur_along_axis(
        img, blur_width, axis=0, lines_per_chunk=1024)
    chunked = triangular_blur_along_axis(
        img, blur_width, axis=0, lines_per_chunk=32)

    assert numpy.array_equal(whole, chunked)


def test_blur_width_one_is_identity():
    rng = numpy.random.default_rng(3)
    img = rng.integers(0, 256, size=(40, 50)).astype(numpy.uint8)
    for axis in (0, 1):
        assert numpy.array_equal(
            triangular_blur_along_axis(img, 1, axis=axis), img)
