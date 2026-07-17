"""Tile-adaptive color harmonization (pure leaf module).

Ortho scenery tiles assemble textures from imagery flown on different dates,
by different campaigns, sometimes from different providers, producing a
visible patchwork of color casts and hard steps at texture seams. This module
implements the deterministic per-texture color shift specified in
``docs/specs/color-harmonization-spec.md`` (sections 3.1 - 3.3).

The approach is additive per-channel mean/median shifting toward a *locally
derived* target (the median of each texture's grid neighborhood) with a
zoom-level strength schedule, rather than a single hardcoded global target.
Near-black water and near-white nodata pixels are excluded from the
statistics so they cannot poison the shift.

This module imports only numpy and PIL. It performs no input/output, holds no
state, and contains no randomness: every function is a deterministic transform
of its arguments. Orchestration (statistics collection across a tile, the
JSON sidecar, the download/convert barrier) lives in the pipeline modules and
is wired separately; nothing here reaches back into Ortho4XP.
"""

from __future__ import annotations

import numpy
from PIL import Image

# --- Frozen constants (spec section 3.3) -----------------------------------

# Per-channel shift strength as a function of zoom level. At low zoom one
# texture covers enough ground that neighboring blocks of land normally look
# alike, so most color variance is acquisition (date/sensor/processing) and a
# strong pull toward the local consensus is safe. At high zoom the textures
# are small enough that real content variance dominates, so the pull backs off
# hard. Keys are the exact zoom levels called out by the spec; zoom levels at
# or below 16 use the ZL16 value and zoom levels at or above 19 use the ZL19
# value.
STRENGTH_SCHEDULE_BY_ZOOMLEVEL = {
    16: 0.70,  # ZL <= 16
    17: 0.40,
    18: 0.20,
    19: 0.10,  # ZL >= 19
}

# Absolute per-channel cap on the applied shift, in 0-255 counts. This bounds
# the worst-case damage from any statistics failure mode: no matter how far a
# recorded median lies from its target, a single texture can move by at most
# this many counts per channel.
MAXIMUM_SHIFT_MAGNITUDE = 20.0

# --- Statistics pass constants (spec section 3.1) --------------------------

# Every source image is reduced to this square before its statistics are
# computed, so cost is independent of the assembled texture size.
THUMBNAIL_SIZE = (512, 512)

# Rec. 601 luminance coefficients (0.299 R + 0.587 G + 0.114 B).
_LUMINANCE_COEFFICIENTS = numpy.array([0.299, 0.587, 0.114], dtype=numpy.float64)

# A pixel contributes to the statistics only when its luminance is strictly
# inside this open interval. The lower bound excludes water-black, the upper
# bound excludes nodata-white and cloud cores.
LUMINANCE_VALID_LOWER_BOUND = 10.0
LUMINANCE_VALID_UPPER_BOUND = 248.0

# A texture is excluded entirely (contributes no statistics, receives no shift)
# when fewer than this fraction of the thumbnail's pixels are valid.
MINIMUM_VALID_FRACTION = 0.20

# Default grid spacing (in orthogrid tile numbers) between adjacent textures of
# one zoom level: one texture spans 16 x 16 orthogrid tiles. Used only as a
# fallback when a spacing cannot be inferred from the keys themselves.
_DEFAULT_GRID_STEP = 16


def compute_texture_color_statistics(image: Image.Image) -> dict | None:
    """Compute robust per-channel color statistics for one texture image.

    The image is converted to RGB and downsampled to a 512 x 512 thumbnail
    with a box filter, a validity mask keeps only pixels whose Rec. 601
    luminance is strictly between 10 and 248, and the per-channel *median* of
    the valid pixels is recorded (median, not mean: robust to content outliers
    within the thumbnail).

    Args:
        image: Source texture image of any size and mode.

    Returns:
        ``{"channel_medians": [r, g, b], "valid_fraction": f}`` where the
        medians are floats and ``f`` is the fraction of thumbnail pixels that
        were valid, or ``None`` when fewer than 20% of the thumbnail's pixels
        are valid (the texture is then excluded from harmonization entirely).
    """
    thumbnail = image.convert("RGB").resize(THUMBNAIL_SIZE, Image.BOX)
    pixels = numpy.asarray(thumbnail, dtype=numpy.float64)  # (H, W, 3)

    luminance = pixels @ _LUMINANCE_COEFFICIENTS  # (H, W)
    valid_mask = (luminance > LUMINANCE_VALID_LOWER_BOUND) & (
        luminance < LUMINANCE_VALID_UPPER_BOUND
    )

    total_pixel_count = luminance.size
    valid_pixel_count = int(valid_mask.sum())
    valid_fraction = valid_pixel_count / total_pixel_count

    if valid_fraction < MINIMUM_VALID_FRACTION:
        return None

    valid_pixels = pixels[valid_mask]  # (N, 3)
    channel_medians = numpy.median(valid_pixels, axis=0)  # (3,)

    return {
        "channel_medians": [float(value) for value in channel_medians],
        "valid_fraction": float(valid_fraction),
    }


def _infer_axis_step(sorted_unique_values: numpy.ndarray) -> int:
    """Infer the grid spacing along one axis from its sorted unique keys.

    The step is the median of the consecutive differences of the sorted unique
    coordinate values, which recovers the true texture spacing (16 for
    orthogrid tiles) even when the tile has gaps (excluded textures leave
    holes in the grid). It falls back to the default spacing when fewer than
    two distinct values are present, so a single-row or single-column grid
    still yields a usable step.

    Args:
        sorted_unique_values: Ascending unique coordinate values along one axis.

    Returns:
        The inferred positive integer step for that axis.
    """
    if sorted_unique_values.size < 2:
        return _DEFAULT_GRID_STEP
    differences = numpy.diff(sorted_unique_values)
    step = int(round(float(numpy.median(differences))))
    return step if step > 0 else _DEFAULT_GRID_STEP


def compute_target_field(
    statistics_by_grid_key: dict[tuple[int, int], "numpy.ndarray"],
    neighborhood_radius: int = 2,
) -> dict[tuple[int, int], "numpy.ndarray"]:
    """Compute each texture's per-channel harmonization target.

    Textures of one zoom level form a grid keyed by ``(til_x, til_y)``
    orthogrid tile numbers. The target for a texture is the per-channel median
    of the recorded channel-median vectors over its ``(2r + 1) x (2r + 1)``
    grid neighborhood (including its own vector), clamped to whatever neighbors
    are available at tile borders. Excluded textures are simply absent from the
    input and never counted.

    The grid spacing is *not* assumed: the per-axis step is inferred as the
    median of consecutive differences of the sorted unique coordinate values
    (default 16 when an axis has fewer than two distinct values). Each key is
    mapped to an integer grid index by dividing by that step and rounding, so
    two textures are neighbors when their index distance is within
    ``neighborhood_radius`` on both axes. Inferring the step (rather than
    ranking unique values) preserves real gaps: a hole left by an excluded
    texture keeps its neighbors two index-steps apart, so radius-2 windows do
    not silently jump across it.

    Args:
        statistics_by_grid_key: Mapping from ``(til_x, til_y)`` to a length-3
            numpy array of per-channel medians, for the textures that have
            statistics (excluded textures absent).
        neighborhood_radius: Half-width of the square neighborhood in grid
            steps (default 2, i.e. a 5 x 5 window).

    Returns:
        A mapping from the same grid keys to length-3 numpy arrays of
        per-channel targets. Empty input yields an empty mapping.
    """
    if not statistics_by_grid_key:
        return {}

    keys = list(statistics_by_grid_key.keys())
    x_values = numpy.array(sorted({key[0] for key in keys}))
    y_values = numpy.array(sorted({key[1] for key in keys}))
    x_step = _infer_axis_step(x_values)
    y_step = _infer_axis_step(y_values)

    # Map every key to an integer grid index; keep the index alongside the key
    # so border clamping is just an index-distance test over the populated set.
    index_by_key: dict[tuple[int, int], tuple[int, int]] = {}
    for key in keys:
        index_by_key[key] = (
            int(round(key[0] / x_step)),
            int(round(key[1] / y_step)),
        )

    target_field: dict[tuple[int, int], "numpy.ndarray"] = {}
    for key in keys:
        center_x, center_y = index_by_key[key]
        neighborhood_vectors = [
            statistics_by_grid_key[other_key]
            for other_key in keys
            if abs(index_by_key[other_key][0] - center_x) <= neighborhood_radius
            and abs(index_by_key[other_key][1] - center_y) <= neighborhood_radius
        ]
        stacked = numpy.stack(neighborhood_vectors, axis=0)  # (M, 3)
        target_field[key] = numpy.median(stacked, axis=0)

    return target_field


def _strength_for_zoomlevel(zoomlevel: int) -> float:
    """Return the shift strength for a zoom level per the frozen schedule.

    Zoom levels at or below 16 use the ZL16 strength and zoom levels at or
    above 19 use the ZL19 strength; 17 and 18 use their own values.
    """
    if zoomlevel <= 16:
        return STRENGTH_SCHEDULE_BY_ZOOMLEVEL[16]
    if zoomlevel >= 19:
        return STRENGTH_SCHEDULE_BY_ZOOMLEVEL[19]
    return STRENGTH_SCHEDULE_BY_ZOOMLEVEL[zoomlevel]


def compute_harmonization_shift(
    channel_medians: "numpy.ndarray",
    target: "numpy.ndarray",
    zoomlevel: int,
) -> "numpy.ndarray":
    """Compute the per-channel additive shift for one texture.

    The shift pulls the texture's recorded medians toward its target, scaled by
    the zoom-level strength and capped in magnitude::

        shift = clip(strength(zoomlevel) * (target - channel_medians), -20, +20)

    Args:
        channel_medians: Length-3 array of the texture's recorded per-channel
            medians.
        target: Length-3 array of the texture's per-channel target.
        zoomlevel: The texture's zoom level, selecting the strength.

    Returns:
        A length-3 float array of per-channel shifts, each within
        ``[-20, +20]``.
    """
    strength = _strength_for_zoomlevel(zoomlevel)
    medians = numpy.asarray(channel_medians, dtype=numpy.float64)
    target_array = numpy.asarray(target, dtype=numpy.float64)
    raw_shift = strength * (target_array - medians)
    return numpy.clip(raw_shift, -MAXIMUM_SHIFT_MAGNITUDE, MAXIMUM_SHIFT_MAGNITUDE)


def apply_color_shift(image: Image.Image, shift: "numpy.ndarray") -> Image.Image:
    """Apply a per-channel additive color shift to an image via lookup tables.

    Three 256-entry lookup tables are built (each entry ``clip(index + shift,
    0, 255)``) and applied with ``Image.point`` in a single pass. RGB and RGBA
    inputs are both supported; an alpha channel, when present, passes through
    untouched. The input image is never mutated; a new image of the same mode
    is returned. When every channel's shift rounds to zero the function
    short-circuits and returns a copy unchanged.

    Args:
        image: Source RGB or RGBA image.
        shift: Length-3 array of per-channel shifts (red, green, blue).

    Returns:
        A new image of the same mode with the shift applied.
    """
    shift_array = numpy.asarray(shift, dtype=numpy.float64)
    rounded_shifts = [int(round(float(value))) for value in shift_array]

    if all(value == 0 for value in rounded_shifts):
        return image.copy()

    indices = numpy.arange(256, dtype=numpy.int64)
    lookup_tables = []
    for channel_shift in rounded_shifts:
        table = numpy.clip(indices + channel_shift, 0, 255).astype(numpy.uint8)
        lookup_tables.append(table)

    has_alpha = image.mode == "RGBA"
    if has_alpha:
        red, green, blue, alpha = image.split()
    else:
        red, green, blue = image.convert("RGB").split()

    shifted_red = red.point(lookup_tables[0].tolist())
    shifted_green = green.point(lookup_tables[1].tolist())
    shifted_blue = blue.point(lookup_tables[2].tolist())

    if has_alpha:
        return Image.merge("RGBA", (shifted_red, shifted_green, shifted_blue, alpha))
    return Image.merge("RGB", (shifted_red, shifted_green, shifted_blue))
