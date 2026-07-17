"""
O4_Sea_Nodata_Fill.py — repair provider "nodata" defects in sea orthophotos
============================================================================

Some imagery providers return saturated white or black rectangles where they
have no coverage, most often over coastal open sea.  When such a texture is
baked into a scenery tile those holes appear as glaring flat patches next to
otherwise convincing water.  This module detects those holes and synthesises
plausible water into them by cloning genuine sea pixels that lie nearby, so the
repaired texture blends into the surrounding sea.

This is a clean-room reimplementation, to this repository's conventions, of the
detection-and-fill algorithm from another GPL Ortho4XP fork.  Only the pixel
algorithm is ported; none of that fork's mesh/tile selection, patch-directory
plumbing or GUI is reproduced here.  The module is a pure leaf: it imports only
numpy, PIL and scipy.ndimage, never a GUI toolkit and never another O4_* module.

Detection lock (a pixel region is treated as fillable nodata only when ALL of
these hold — the guard exists because real photographed water must never be
touched):
  * saturated: every channel < 70 (black) or every channel > 240 (white);
  * flat: local 9x9 standard deviation < 1.5 (photographed water is never
    perfectly flat, so any texture disqualifies the region);
  * large: the connected saturated-and-flat component covers at least
    max(5000, 2% of the image) pixels (small specular glints are ignored);
  * mostly water: when a water mask is supplied, at least 70% of the component
    must overlap water, so a large flat white roof or a land-side nodata patch
    can never trigger a sea clone.

All randomness is drawn from a single ``numpy.random.default_rng(random_seed)``
so that identical inputs and seed produce byte-identical output; the module
never consults ``hash()``, wall-clock time or the global random state.
"""

import numpy
from PIL import Image
from scipy.ndimage import (
    binary_erosion,
    distance_transform_edt,
    gaussian_filter,
    label,
    uniform_filter,
    zoom,
)


# Saturation thresholds and the flatness ceiling are field-validated constants
# from the reference fork; they are the anti-false-positive lock and must not be
# loosened without re-validating on real 4096 px coastal tiles.
_DARK_CHANNEL_CEILING = 70
_WHITE_CHANNEL_FLOOR = 240
_LOCAL_FLATNESS_CEILING = 1.5
_FLATNESS_WINDOW = 9

# Margin (pixels) grown around the raw saturated bounding box before the
# expensive flatness filter and labelling run.  Restricting those passes to the
# defect neighbourhood instead of the whole frame is the ~3x speed-up on tiles
# whose nodata is a partial corner.  The margin must comfortably exceed the
# flatness window and the halo so edge effects never reach a real component.
_ANALYSIS_MARGIN = 150

# A component must be at least 70% over water (mask value < 128) to survive when
# a water mask is supplied.
_WATER_MASK_MIDPOINT = 128
_MINIMUM_WATER_OVERLAP_FRACTION = 0.70

# Pixels below this luminance are candidate sea; brighter valid pixels (beach,
# breaking surf, boats) are excluded from the sea pool.
_SEA_LUMINANCE_CEILING = 190


def _bounding_box_of_true(mask: "numpy.ndarray") -> "tuple[int, int, int, int] | None":
    """Return ``(row_min, row_max, column_min, column_max)`` inclusive for the
    True pixels of ``mask``, or None when the mask is empty."""
    rows_with_true = numpy.where(mask.any(axis=1))[0]
    columns_with_true = numpy.where(mask.any(axis=0))[0]
    if rows_with_true.size == 0 or columns_with_true.size == 0:
        return None
    return (
        int(rows_with_true.min()),
        int(rows_with_true.max()),
        int(columns_with_true.min()),
        int(columns_with_true.max()),
    )


def _compute_local_standard_deviation(grayscale: "numpy.ndarray") -> "numpy.ndarray":
    """Return the per-pixel standard deviation of ``grayscale`` over a
    ``_FLATNESS_WINDOW`` square window, via the mean/mean-of-squares identity."""
    local_mean = uniform_filter(grayscale, size=_FLATNESS_WINDOW)
    local_mean_of_squares = uniform_filter(grayscale ** 2, size=_FLATNESS_WINDOW)
    variance = numpy.clip(local_mean_of_squares - local_mean ** 2, 0, None)
    return numpy.sqrt(variance)


def _compute_luminance(image_array: "numpy.ndarray") -> "numpy.ndarray":
    """Return the Rec. 601 luminance of an (H, W, 3) RGB array."""
    return (
        0.299 * image_array[:, :, 0]
        + 0.587 * image_array[:, :, 1]
        + 0.114 * image_array[:, :, 2]
    )


def detect_sea_nodata(
    image_array: "numpy.ndarray",
    water_mask: "numpy.ndarray | None" = None,
) -> "numpy.ndarray | None":
    """Return a boolean nodata mask (halo-expanded) or None when the image has
    no fillable nodata.

    ``image_array`` is a float32 or uint8 RGB array of shape (H, W, 3).
    ``water_mask``, when given, is a uint8 array of shape (H, W) following the
    repository's convention: 255 = fully land (opaque orthophoto), 0 = fully
    water (transparent).  Components whose water overlap is below 70% are
    dropped, so land-side nodata never registers as sea.

    The returned mask marks every saturated-flat-large(-and-mostly-water)
    component grown by the provider-halo distance so the blend fringe around a
    hole is repaired together with the hole itself.
    """
    array = numpy.asarray(image_array, dtype=numpy.float32)
    height, width = array.shape[:2]
    red = array[:, :, 0]
    green = array[:, :, 1]
    blue = array[:, :, 2]

    # Fast reject FIRST: the overwhelmingly common case is a clean texture with
    # no saturated pixel at all, and it must cost only three comparisons.
    raw_dark = (
        (red < _DARK_CHANNEL_CEILING)
        & (green < _DARK_CHANNEL_CEILING)
        & (blue < _DARK_CHANNEL_CEILING)
    )
    raw_white = (
        (red > _WHITE_CHANNEL_FLOOR)
        & (green > _WHITE_CHANNEL_FLOOR)
        & (blue > _WHITE_CHANNEL_FLOOR)
    )
    if not raw_dark.any() and not raw_white.any():
        return None
    raw_nodata = raw_dark | raw_white

    # Restrict the flatness filter and labelling to the raw-defect bounding box
    # grown by a margin, then reinject into a full-size mask.
    bounding_box = _bounding_box_of_true(raw_nodata)
    if bounding_box is None:
        return None
    row_min, row_max, column_min, column_max = bounding_box
    crop_row_start = max(0, row_min - _ANALYSIS_MARGIN)
    crop_row_stop = min(height, row_max + 1 + _ANALYSIS_MARGIN)
    crop_column_start = max(0, column_min - _ANALYSIS_MARGIN)
    crop_column_stop = min(width, column_max + 1 + _ANALYSIS_MARGIN)

    cropped_array = array[
        crop_row_start:crop_row_stop, crop_column_start:crop_column_stop
    ]
    cropped_grayscale = cropped_array.mean(axis=2)
    cropped_flat = _compute_local_standard_deviation(cropped_grayscale) < _LOCAL_FLATNESS_CEILING
    cropped_candidate = (
        raw_nodata[crop_row_start:crop_row_stop, crop_column_start:crop_column_stop]
        & cropped_flat
    )
    if not cropped_candidate.any():
        return None

    minimum_component_size = max(5000, int(0.02 * height * width))
    labelled, component_count = label(cropped_candidate)
    if component_count == 0:
        return None
    component_sizes = numpy.bincount(labelled.ravel())

    cropped_water = None
    if water_mask is not None:
        cropped_water = (
            numpy.asarray(water_mask)[
                crop_row_start:crop_row_stop, crop_column_start:crop_column_stop
            ]
            < _WATER_MASK_MIDPOINT
        )

    kept_nodata = numpy.zeros(cropped_candidate.shape, dtype=bool)
    for component_index in range(1, component_count + 1):
        size = int(component_sizes[component_index])
        if size < minimum_component_size:
            continue
        component = labelled == component_index
        if cropped_water is not None:
            water_overlap = int((component & cropped_water).sum())
            if water_overlap < _MINIMUM_WATER_OVERLAP_FRACTION * size:
                # Mostly over land: a flat white roof or land-side hole, never a
                # place where cloning sea would be correct.
                continue
        kept_nodata |= component

    if not kept_nodata.any():
        return None

    full_nodata = numpy.zeros((height, width), dtype=bool)
    full_nodata[
        crop_row_start:crop_row_stop, crop_column_start:crop_column_stop
    ] = kept_nodata

    # Grow the mask to swallow the provider's blend halo around each hole.  The
    # halo scales with resolution (~1.6% of the long side) because a fixed pixel
    # count leaves a dark straight seam on large tiles.
    halo_pixels = max(12, int(0.016 * max(height, width)))
    distance_to_nodata = distance_transform_edt(~full_nodata)
    return distance_to_nodata <= halo_pixels


def fill_sea_nodata(
    image: "Image.Image",
    water_mask: "numpy.ndarray | None" = None,
    random_seed: int = 0,
) -> "Image.Image | None":
    """Return a new RGB image with nodata regions synthesised from neighbouring
    sea pixels, or None when there is nothing to fill.

    The synthesis clones genuine sea pixels into each hole with an aligned
    clone-stamp gesture: the hole is split into wavy slabs, each slab is filled
    by one constant translation offset chosen to land mostly inside a
    locally-derived sea pool, and seams between strokes are smudged so the join
    is invisible.  Pixels outside the halo-expanded nodata mask are left exactly
    as they were in ``image``; the inputs are never mutated.
    """
    source_image = image.convert("RGB")
    array = numpy.asarray(source_image, dtype=numpy.float32)
    height, width = array.shape[:2]
    long_side = max(height, width)

    nodata = detect_sea_nodata(array, water_mask)
    if nodata is None:
        return None
    valid = ~nodata
    distance_to_nodata = distance_transform_edt(~nodata)

    random_generator = numpy.random.default_rng(random_seed)

    # ── Adaptive sea pool: median/MAD statistics of the valid ring hugging the
    # hole, so the pool tracks the local water colour rather than a global one.
    luminance = _compute_luminance(array)
    ring_width = max(25, int(0.01 * long_side))
    ring = valid & (distance_to_nodata < 2 * ring_width) & (luminance < _SEA_LUMINANCE_CEILING)
    if int(ring.sum()) < 200:
        ring = valid & (luminance < _SEA_LUMINANCE_CEILING)

    median_color = numpy.array(
        [float(numpy.median(array[:, :, channel][ring])) for channel in range(3)],
        dtype=numpy.float32,
    )
    # 1.4826 rescales the median absolute deviation to a Gaussian sigma; the 6.0
    # floor keeps the pool from collapsing on very uniform water.
    channel_sigma = numpy.array(
        [
            max(
                6.0,
                1.4826
                * float(numpy.median(numpy.abs(array[:, :, channel][ring] - median_color[channel]))),
            )
            for channel in range(3)
        ],
        dtype=numpy.float32,
    )
    deviation = numpy.max(
        numpy.abs(array - median_color[None, None, :]) / channel_sigma[None, None, :],
        axis=2,
    )
    sea_pool = valid & (deviation < 4.0) & (luminance < _SEA_LUMINANCE_CEILING)
    sea_pool = binary_erosion(sea_pool, iterations=2)
    if int(sea_pool.sum()) < 5000:
        # Relaxed fallback when the strict pool is too small to clone from.
        sea_pool = valid & (deviation < 6.0) & (luminance < _SEA_LUMINANCE_CEILING)

    pool_rows, pool_columns = numpy.where(sea_pool)
    if pool_rows.size < 50:
        return None

    filled = array.copy()
    remaining = nodata.copy()
    stroke_id = numpy.zeros((height, width), dtype=numpy.int32)
    stroke_counter = 0

    hole_rows, hole_columns = numpy.where(nodata)
    hole_row_min, hole_row_max = int(hole_rows.min()), int(hole_rows.max())
    hole_column_min, hole_column_max = int(hole_columns.min()), int(hole_columns.max())
    along_vertical_axis = (hole_row_max - hole_row_min) >= (hole_column_max - hole_column_min)
    hole_extent = (
        (hole_row_max - hole_row_min + 1)
        if along_vertical_axis
        else (hole_column_max - hole_column_min + 1)
    )

    slab_thickness = max(int(0.10 * long_side), 80)
    slab_count = max(1, int(numpy.ceil(hole_extent / slab_thickness)))
    perpendicular_length = width if along_vertical_axis else height

    # Wavy slab boundaries: cubic-zoom a handful of random samples across the
    # perpendicular extent so slab edges undulate like a real waterline instead
    # of drawing straight rectangular seams.
    wave_offsets = []
    for _ in range(slab_count + 1):
        samples = random_generator.random(9) - 0.5
        stretched = zoom(samples, perpendicular_length / 9.0, order=3)[:perpendicular_length]
        wave_offsets.append((stretched * 0.5 * slab_thickness).astype(numpy.int32))

    axis_index = (
        numpy.arange(height)[:, None]
        if along_vertical_axis
        else numpy.arange(width)[None, :]
    )
    local_anchor_window = max(40, int(0.05 * long_side))
    adjacent_sea_window = max(60, int(0.08 * long_side))

    def apply_aligned_stroke(target_rows, target_columns, anchors, minimum_coverage):
        """Fill one stroke by an aligned clone stamp: find a single translation
        offset whose translate of the target pixels lands mostly in the sea
        pool, copy the raw source pixels for those that do, and tone-shift the
        stroke towards the adjacent sea.  Return the number of pixels written."""
        nonlocal stroke_counter
        anchor_rows, anchor_columns = anchors
        if anchor_rows.size == 0:
            return 0

        probe_count = min(2500, target_rows.size)
        probe_selection = random_generator.choice(target_rows.size, size=probe_count, replace=False)
        probe_rows = target_rows[probe_selection]
        probe_columns = target_columns[probe_selection]

        best_coverage = 0.0
        best_offset = None
        for _ in range(40):
            anchor_index = int(random_generator.integers(anchor_rows.size))
            source_row = int(anchor_rows[anchor_index])
            source_column = int(anchor_columns[anchor_index])
            pairing_index = int(random_generator.integers(target_rows.size))
            row_offset = source_row - int(target_rows[pairing_index])
            column_offset = source_column - int(target_columns[pairing_index])

            translated_rows = probe_rows + row_offset
            translated_columns = probe_columns + column_offset
            inside = (
                (translated_rows >= 0)
                & (translated_rows < height)
                & (translated_columns >= 0)
                & (translated_columns < width)
            )
            if not inside.any():
                continue
            coverage = float(
                sea_pool[translated_rows[inside], translated_columns[inside]].sum()
            ) / probe_count
            if coverage > best_coverage:
                best_coverage = coverage
                best_offset = (row_offset, column_offset)
            if coverage > 0.92:
                break

        if best_offset is None or best_coverage < minimum_coverage:
            return 0

        row_offset, column_offset = best_offset
        translated_rows = target_rows + row_offset
        translated_columns = target_columns + column_offset
        inside = (
            (translated_rows >= 0)
            & (translated_rows < height)
            & (translated_columns >= 0)
            & (translated_columns < width)
        )
        destination_rows = target_rows[inside]
        destination_columns = target_columns[inside]
        translated_rows = translated_rows[inside]
        translated_columns = translated_columns[inside]

        source_in_pool = sea_pool[translated_rows, translated_columns]
        if not source_in_pool.any():
            return 0
        destination_rows = destination_rows[source_in_pool]
        destination_columns = destination_columns[source_in_pool]
        translated_rows = translated_rows[source_in_pool]
        translated_columns = translated_columns[source_in_pool]

        # Tone-shift the whole stroke by one clipped constant towards the sea
        # adjacent to this slab, so the copied patch matches the local water
        # brightness without averaging away its texture.
        if along_vertical_axis:
            adjacent_start = max(0, int(destination_rows.min()) - adjacent_sea_window)
            adjacent_stop = min(height, int(destination_rows.max()) + adjacent_sea_window + 1)
            adjacent = (
                sea_pool[adjacent_start:adjacent_stop, :]
                & (distance_to_nodata[adjacent_start:adjacent_stop, :] < adjacent_sea_window)
            )
            adjacent_pixels = array[adjacent_start:adjacent_stop, :][adjacent]
        else:
            adjacent_start = max(0, int(destination_columns.min()) - adjacent_sea_window)
            adjacent_stop = min(width, int(destination_columns.max()) + adjacent_sea_window + 1)
            adjacent = (
                sea_pool[:, adjacent_start:adjacent_stop]
                & (distance_to_nodata[:, adjacent_start:adjacent_stop] < adjacent_sea_window)
            )
            adjacent_pixels = array[:, adjacent_start:adjacent_stop][adjacent]

        source_pixels = array[translated_rows, translated_columns]
        if adjacent_pixels.shape[0] > 100:
            tone_shift = numpy.clip(
                adjacent_pixels.mean(axis=0) - source_pixels.mean(axis=0), -40, 40
            )
        else:
            tone_shift = numpy.zeros(3, dtype=numpy.float32)

        stroke_counter += 1
        filled[destination_rows, destination_columns] = source_pixels + tone_shift[None, :]
        stroke_id[destination_rows, destination_columns] = stroke_counter
        remaining[destination_rows, destination_columns] = False
        return destination_rows.size

    def collect_slab_anchors(slab_rows, slab_columns):
        """Return sea-pool pixels in a window around a slab, grown until the
        anchor set is large enough for a meaningful offset search."""
        for growth in (1, 2, 4):
            if along_vertical_axis:
                window_start = max(0, int(slab_rows.min()) - local_anchor_window * growth)
                window_stop = min(height, int(slab_rows.max()) + local_anchor_window * growth + 1)
                window = sea_pool[window_start:window_stop, :]
                local_rows, local_columns = numpy.where(window)
                if local_rows.size > 2000:
                    return (local_rows + window_start, local_columns)
            else:
                window_start = max(0, int(slab_columns.min()) - local_anchor_window * growth)
                window_stop = min(width, int(slab_columns.max()) + local_anchor_window * growth + 1)
                window = sea_pool[:, window_start:window_stop]
                local_rows, local_columns = numpy.where(window)
                if local_rows.size > 2000:
                    return (local_rows, local_columns + window_start)
        return (numpy.array([], dtype=numpy.int64), numpy.array([], dtype=numpy.int64))

    for slab_index in range(slab_count):
        slab_low = (hole_row_min if along_vertical_axis else hole_column_min) + slab_index * slab_thickness
        slab_high = slab_low + slab_thickness
        if along_vertical_axis:
            lower_boundary = slab_low + wave_offsets[slab_index][None, :]
            upper_boundary = slab_high + wave_offsets[slab_index + 1][None, :]
        else:
            lower_boundary = slab_low + wave_offsets[slab_index][:, None]
            upper_boundary = slab_high + wave_offsets[slab_index + 1][:, None]
        slab_mask = remaining & (axis_index >= lower_boundary) & (axis_index < upper_boundary)
        if slab_index == 0:
            slab_mask |= remaining & (axis_index < lower_boundary)
        if slab_index == slab_count - 1:
            slab_mask |= remaining & (axis_index >= upper_boundary)
        if not slab_mask.any():
            continue
        slab_rows, slab_columns = numpy.where(slab_mask)
        anchors = collect_slab_anchors(slab_rows, slab_columns)
        apply_aligned_stroke(slab_rows, slab_columns, anchors, 0.5)

    # Catch-up: fill stragglers with global-anchor strokes at a low coverage
    # floor, so isolated leftover pixels still receive real cloned water.
    catch_up_rounds = 0
    while remaining.any() and catch_up_rounds < 10:
        catch_up_rounds += 1
        leftover_rows, leftover_columns = numpy.where(remaining)
        written = apply_aligned_stroke(
            leftover_rows, leftover_columns, (pool_rows, pool_columns), 0.15
        )
        if written == 0:
            break

    # Last resort for anything still unfilled: ring median plus light gaussian
    # noise.  Nearest-neighbour / Voronoi fill is deliberately avoided — it
    # produces triangular clone artifacts.
    if remaining.any():
        leftover_rows, leftover_columns = numpy.where(remaining)
        base_color = median_color
        noise = random_generator.standard_normal((leftover_rows.size, 3)).astype(numpy.float32) * 1.5
        filled[leftover_rows, leftover_columns] = base_color[None, :] + noise
        remaining[:] = False

    _smudge_stroke_seams(
        filled, array, nodata, stroke_id, height, width, long_side, random_generator
    )

    # Guarantee: pixels outside the nodata mask are byte-identical to the input.
    filled[valid] = array[valid]

    # Final safety pass: replace any pixel still saturated white/black inside the
    # hole (a stroke may have copied a straggler) with ring median plus noise, so
    # no original defect survives.
    filled_red = filled[:, :, 0]
    filled_green = filled[:, :, 1]
    filled_blue = filled[:, :, 2]
    still_dark = (
        (filled_red < _DARK_CHANNEL_CEILING)
        & (filled_green < _DARK_CHANNEL_CEILING)
        & (filled_blue < _DARK_CHANNEL_CEILING)
        & (numpy.abs(filled_red - median_color[0]) > 3 * channel_sigma[0])
    )
    still_white = (
        (filled_red > _WHITE_CHANNEL_FLOOR)
        & (filled_green > _WHITE_CHANNEL_FLOOR)
        & (filled_blue > _WHITE_CHANNEL_FLOOR)
    )
    still_saturated = nodata & (still_dark | still_white)
    if still_saturated.any():
        bad_rows, bad_columns = numpy.where(still_saturated)
        noise = random_generator.standard_normal((bad_rows.size, 3)).astype(numpy.float32) * 1.5
        filled[bad_rows, bad_columns] = median_color[None, :] + noise

    return Image.fromarray(numpy.clip(filled, 0, 255).astype(numpy.uint8))


def _smudge_stroke_seams(
    filled: "numpy.ndarray",
    array: "numpy.ndarray",
    nodata: "numpy.ndarray",
    stroke_id: "numpy.ndarray",
    height: int,
    width: int,
    long_side: int,
    random_generator: "numpy.random.Generator",
) -> None:
    """Blur and displace pixels along the seams between neighbouring strokes and
    along the hole frontier, so the aligned-stamp joins do not read as hard
    edges.  Mutates ``filled`` in place; only nodata pixels are altered."""
    # Seam pixels: nodata pixels where neighbouring stroke ids disagree, plus the
    # one-pixel-deep frontier of the hole itself.
    maximum_neighbour = numpy.zeros((height, width), dtype=numpy.int32)
    minimum_neighbour = numpy.full((height, width), 2 ** 30, dtype=numpy.int32)
    for shift_row in (-1, 0, 1):
        for shift_column in (-1, 0, 1):
            shifted = numpy.roll(numpy.roll(stroke_id, shift_row, axis=0), shift_column, axis=1)
            maximum_neighbour = numpy.maximum(maximum_neighbour, shifted)
            has_stroke = shifted > 0
            minimum_neighbour = numpy.where(
                has_stroke, numpy.minimum(minimum_neighbour, shifted), minimum_neighbour
            )
    seam_lines = (
        nodata
        & (stroke_id > 0)
        & (maximum_neighbour != minimum_neighbour)
        & (minimum_neighbour < 2 ** 30)
    )
    depth_into_hole = distance_transform_edt(nodata)
    seam_lines |= nodata & (depth_into_hole <= 1.5)

    if not seam_lines.any():
        return

    distance_to_seam = distance_transform_edt(~seam_lines).astype(numpy.float32)
    band_width = max(8.0, 0.004 * long_side)
    band = nodata & (distance_to_seam < band_width)
    if not band.any():
        return

    # Displace band pixels perpendicular to the seam (the normalised gradient of
    # the distance field) by a smooth random amount that is strongest on the
    # seam and fades to zero at the band edge — the directional "smudge".
    gradient_row, gradient_column = numpy.gradient(distance_to_seam)
    gradient_norm = numpy.maximum(numpy.sqrt(gradient_row ** 2 + gradient_column ** 2), 1e-3)
    gradient_row /= gradient_norm
    gradient_column /= gradient_norm

    noise_seed = random_generator.random((17, 17)) - 0.5
    noise_field = zoom(noise_seed, (height / 17.0, width / 17.0), order=3)[:height, :width]
    amplitude = (
        numpy.clip(1.0 - distance_to_seam / band_width, 0, 1)
        * band_width
        * 1.6
        * noise_field.astype(numpy.float32)
    )

    band_rows, band_columns = numpy.where(band)
    sample_rows = numpy.clip(
        (band_rows + gradient_row[band_rows, band_columns] * amplitude[band_rows, band_columns])
        .round()
        .astype(numpy.int64),
        0,
        height - 1,
    )
    sample_columns = numpy.clip(
        (band_columns + gradient_column[band_rows, band_columns] * amplitude[band_rows, band_columns])
        .round()
        .astype(numpy.int64),
        0,
        width - 1,
    )
    filled[band_rows, band_columns] = filled[sample_rows, sample_columns]

    # Blend a light gaussian blur weighted towards the seam line, hole pixels
    # only, to soften the displaced band.
    blend_weight = numpy.clip(1.0 - distance_to_seam / band_width, 0.0, 1.0) ** 1.5
    blend_weight = numpy.where(nodata, blend_weight, 0.0).astype(numpy.float32)
    for channel in range(3):
        blurred = gaussian_filter(filled[:, :, channel], sigma=1.5)
        filled[:, :, channel] = (
            blend_weight * blurred + (1.0 - blend_weight) * filled[:, :, channel]
        )
