#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cosmetic post-pass that gives coastal water masks an organic shoreline.

Ortho4XP stores per-tile water masks as uint8 alpha arrays following the
repository convention:

    255 = fully land  (opaque orthophoto is shown)
      0 = fully water (fully transparent, X-Plane water shows through)

The regular ``blur_mask`` transition between those two extremes is a
mathematically smooth hat-convolution, so the land-to-water boundary reads as
a machine-straight edge. This module replaces that boundary *inside a narrow
band* with two cosmetic effects:

  1. an organically wavy shoreline (fractal displacement of the boundary), and
  2. a semi-transparent "foam" band on the water side that ramps up to a caller
     supplied gray value at the shoreline.

The pass therefore restyles the ``masking_mode`` transition *shape* near the
shoreline and crossfades back into the original mask values toward the band
edges, so deep water, interior land, inland-water plateaus (drawn at the
sea_level gray in the same array) and fades wider than the band all survive
untouched.

Performance redesign
--------------------
This is a performance-oriented reimplementation of an algorithm from another
GPL Ortho4XP fork (``O4_Coastal_Manager.post_process_coastal_mask``). The
reference computes, per 4096x4096 mask, two *full-frame* exact Euclidean
distance transforms and twelve *full-frame* sine evaluations (four fractal
octaves x three directional sine terms). Both scale with the whole frame even
though only a thin coastal band is ever modified.

This variant instead:

  * computes a *single* signed distance with ``skfmm.distance(..., narrow=...)``
    so only the coastal band is evaluated (see ``src/O4_Mask_Utils.py`` for the
    same narrow-band idiom used for distance masks), and
  * synthesises the displacement noise from two *low-resolution* random grids
    upscaled with a cubic ``scipy.ndimage.zoom`` instead of full-resolution
    trigonometry.

Every subsequent step runs on the band's bounding-box crop only, then the
result is reinjected into a copy of the input.

Byte reproducibility
--------------------
This repository pins byte-for-byte reproducibility of builds, so the noise is
drawn exclusively from a single ``numpy.random.default_rng(random_seed)``. The
module never uses ``hash()``, wall-clock time, or global random state.

This is a pure leaf module: it imports only ``numpy``, ``scipy.ndimage`` and
``skfmm``. It performs no input/output and imports no ``O4_*`` module or GUI
toolkit.
"""

from __future__ import annotations

import math

import numpy
import scipy.ndimage
import skfmm

# Maximum lateral displacement, in pixels, of the wavy shoreline.
NOISE_AMPLITUDE_PIXELS = 20

# Extra pixels added to the influence band beyond the noise and foam widths, so
# the noised boundary can never reach the hard-protected saturated region.
EDGE_MARGIN_PIXELS = 20

# Minimum fraction of clearly-water and clearly-land pixels a mask must contain
# to be treated as a genuine mixed coastal mask worth processing.
COASTAL_MINIMUM_FRACTION = 0.04

# Wavelengths, in pixels, of the two smooth random noise layers, together with
# their relative blend weights. The long wavelength contributes broad bays and
# coves; the short wavelength contributes finer wobble.
_NOISE_LAYER_WAVELENGTHS_PIXELS = (180.0, 60.0)
_NOISE_LAYER_WEIGHTS = (0.7, 0.3)


def _fill_narrow_band_signed_distance(
    signed_distance: "numpy.ndarray",
    land: "numpy.ndarray",
    influence: float,
) -> "numpy.ndarray":
    """Return a plain float array of signed distances with masked cells filled.

    ``skfmm.distance(..., narrow=influence)`` returns a masked array whose
    entries outside the narrow band are masked. Those entries are filled with
    ``+influence`` on the land side and ``-influence`` on the water side (the
    side is read from ``land``), so the returned array is monotone in sign:
    positive = land, negative = water.
    """
    if isinstance(signed_distance, numpy.ma.MaskedArray):
        outside_band = numpy.ma.getmaskarray(signed_distance)
        filled = numpy.asarray(signed_distance.filled(0.0), dtype=float)
        filled[outside_band & land] = influence
        filled[outside_band & (~land)] = -influence
        return filled
    return numpy.asarray(signed_distance, dtype=float)


def _smooth_random_layer(
    random_generator: "numpy.random.Generator",
    crop_height: int,
    crop_width: int,
    wavelength_pixels: float,
) -> "numpy.ndarray":
    """Return a smooth random field of shape ``(crop_height, crop_width)``.

    A low-resolution uniform grid (values in ``[-0.5, 0.5]``) with roughly one
    cell per ``wavelength_pixels`` is drawn from ``random_generator`` and
    upscaled to the crop shape with a cubic ``scipy.ndimage.zoom``.
    """
    low_height = int(math.ceil(crop_height / wavelength_pixels)) + 2
    low_width = int(math.ceil(crop_width / wavelength_pixels)) + 2
    low_grid = random_generator.uniform(-0.5, 0.5, size=(low_height, low_width))
    upscaled = scipy.ndimage.zoom(
        low_grid,
        (crop_height / low_height, crop_width / low_width),
        order=3,
    )
    # Cubic zoom output can round to one pixel short of the target; slice to the
    # exact crop shape, padding with edge values in the unlikely short case.
    upscaled = upscaled[:crop_height, :crop_width]
    if upscaled.shape != (crop_height, crop_width):
        upscaled = numpy.pad(
            upscaled,
            (
                (0, crop_height - upscaled.shape[0]),
                (0, crop_width - upscaled.shape[1]),
            ),
            mode="edge",
        )
    return upscaled


def _build_displacement_noise(
    random_generator: "numpy.random.Generator",
    crop_height: int,
    crop_width: int,
) -> "numpy.ndarray":
    """Return a displacement field spanning roughly +/-NOISE_AMPLITUDE_PIXELS.

    Two smooth random layers are blended with ``_NOISE_LAYER_WEIGHTS`` and
    rescaled so the combined field spans about +/-``NOISE_AMPLITUDE_PIXELS``.
    """
    combined = numpy.zeros((crop_height, crop_width), dtype=float)
    for wavelength_pixels, weight in zip(
        _NOISE_LAYER_WAVELENGTHS_PIXELS, _NOISE_LAYER_WEIGHTS
    ):
        combined += weight * _smooth_random_layer(
            random_generator, crop_height, crop_width, wavelength_pixels
        )
    # The blended layers each span [-0.5, 0.5] and the weights sum to 1, so the
    # combined field spans about [-0.5, 0.5]. Scale it to +/-amplitude.
    return combined * (NOISE_AMPLITUDE_PIXELS / 0.5)


def apply_coastal_foam_edge(
    mask_array: "numpy.ndarray",
    foam_width_pixels: int,
    sea_transparency_gray: int,
    random_seed: int = 0,
) -> "numpy.ndarray | None":
    """Return a new uint8 mask with a wavy shoreline and foam band, or None.

    ``mask_array`` is a ``(height, width)`` uint8 water mask using the repository
    alpha convention (255 = fully land, 0 = fully water). ``foam_width_pixels``
    is the width, in pixels, of the semi-transparent foam band on the water
    side. ``sea_transparency_gray`` (0-255) is the gray value the foam band ramps
    up to at the shoreline. ``random_seed`` seeds the reproducible noise.

    ``None`` is returned when the mask is not a genuine mixed coastal mask (its
    water or land fraction is below ``COASTAL_MINIMUM_FRACTION``), so uniform or
    near-uniform masks are skipped before any expensive work.

    The input array is never mutated; a new uint8 array of the same shape is
    returned.
    """
    source = numpy.asarray(mask_array)
    height, width = source.shape

    # 1. Guard: skip masks that are not a genuine mixed coast.
    total_pixels = source.size
    water_fraction = float((source < 64).sum()) / total_pixels
    land_fraction = float((source > 200).sum()) / total_pixels
    if (
        water_fraction < COASTAL_MINIMUM_FRACTION
        or land_fraction < COASTAL_MINIMUM_FRACTION
    ):
        return None

    # 2. Signed distance to the shoreline, narrow band only. Positive = land.
    land = source >= 128
    influence = float(
        NOISE_AMPLITUDE_PIXELS + foam_width_pixels + EDGE_MARGIN_PIXELS
    )
    phi = land.astype(float) * 2.0 - 1.0
    signed_distance = _fill_narrow_band_signed_distance(
        skfmm.distance(phi, narrow=influence), land, influence
    )

    # 3. Restrict all further work to the band's padded bounding box.
    in_band = numpy.abs(signed_distance) < influence
    band_rows = numpy.where(in_band.any(axis=1))[0]
    band_columns = numpy.where(in_band.any(axis=0))[0]
    if band_rows.size == 0 or band_columns.size == 0:
        # No transition band at all; nothing cosmetic to do.
        return source.astype(numpy.uint8, copy=True)
    pad = 4
    row_start = max(int(band_rows[0]) - pad, 0)
    row_stop = min(int(band_rows[-1]) + 1 + pad, height)
    column_start = max(int(band_columns[0]) - pad, 0)
    column_stop = min(int(band_columns[-1]) + 1 + pad, width)

    signed_crop = signed_distance[row_start:row_stop, column_start:column_stop]
    crop_height, crop_width = signed_crop.shape

    # 4. Low-resolution fractal displacement noise on the crop.
    random_generator = numpy.random.default_rng(random_seed)
    noise = _build_displacement_noise(
        random_generator, crop_height, crop_width
    )

    # 5. Wavy shoreline: displace the distance, tapering the noise to zero at
    #    the band edges so the interior land and deep water stay put.
    attenuation = numpy.clip(1.0 - numpy.abs(signed_crop) / influence, 0.0, 1.0)
    noisy_distance = signed_crop + noise * attenuation

    # 6. Sigmoid transition from water (0) to land (255) across the foam width.
    steepness = math.log(19.0) / max(foam_width_pixels, 1)
    new_value = 255.0 / (1.0 + numpy.exp(-steepness * noisy_distance))

    # 7. Foam band on the water side: linear ramp from 0 (deep water) up to
    #    sea_transparency_gray at the shoreline.
    foam_band = (noisy_distance >= -foam_width_pixels) & (noisy_distance < 0.0)
    if foam_band.any():
        foam_ramp = (
            numpy.clip(
                (noisy_distance + foam_width_pixels) / max(foam_width_pixels, 1),
                0.0,
                1.0,
            )
            * sea_transparency_gray
        )
        new_value = numpy.where(foam_band, foam_ramp, new_value)

    # 8. Smooth the styled profile on the crop before blending.
    smoothing_sigma = max(4.0, foam_width_pixels / 20.0)
    new_value = scipy.ndimage.gaussian_filter(new_value, sigma=smoothing_sigma)

    # 9. Crossfade the styled profile back into the ORIGINAL mask values as the
    #    UN-noised distance approaches the band edge.  A hard saturation beyond
    #    the band (as in the reference implementation) is wrong for this
    #    repository's masks: the same array also carries inland-water plateaus
    #    at the sea_level gray and masking_mode fades wider than the band, and
    #    both must survive untouched.  The crossfade guarantees continuity with
    #    whatever profile surrounds the band, with no assumption about it.
    original_crop = source[
        row_start:row_stop, column_start:column_stop
    ].astype(float)
    blend_weight = (
        numpy.clip(1.0 - numpy.abs(signed_crop) / influence, 0.0, 1.0) ** 1.5
    )
    composed = blend_weight * new_value + (1.0 - blend_weight) * original_crop
    composed = numpy.clip(numpy.round(composed), 0, 255).astype(numpy.uint8)

    # 10. Reinject the crop into a fresh copy of the input and return it.
    result = source.astype(numpy.uint8, copy=True)
    result[row_start:row_stop, column_start:column_stop] = composed
    return result
