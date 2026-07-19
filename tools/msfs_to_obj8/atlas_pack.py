"""Per-model texture atlasing for the MSFS glTF/GLB -> X-Plane OBJ8 converter.

X-Plane draws one batch per texture bind, and each OBJ8 object binds exactly
one albedo texture.  Packing a model's small, in-unit-square textures into a
single power-of-two atlas therefore collapses many objects into one: fewer
texture binds, fewer draw batches, and less per-object cull overhead in the
simulator.

This module is pure geometry/image packing; it has no knowledge of glTF or
OBJ8.  The converter (``convert.py``) decides which primitive groups are
atlasable, hands their source images here as :class:`SourceTexture` values,
receives a :class:`PackResult`, and remaps its own UVs with
:func:`remap_uv`.

Packing method (frozen spec)
----------------------------
* Shelf packing, textures sorted by descending height.
* A 4-pixel gutter surrounds every cell; the gutter is filled by replicating
  the cell's own edge pixels (mip-bleed protection), so at coarse mip levels
  a texel near a cell boundary still samples that cell.
* Each atlas is a square power of two, at most 4096x4096.  Cells that do not
  fit spill into additional atlases (atlas index 1, 2, ...).
* A single source larger than the atlas maximum in either dimension is
  downscaled with Lanczos resampling and a warning; nothing else is resized.

Coordinate note
---------------
Cell placements are reported with ``left`` measured from the atlas left edge
and ``bottom`` measured from the atlas BOTTOM edge, so :func:`remap_uv`
consumes OBJ8-convention UVs (v origin bottom-left) directly.  The atlas PNG
itself is written top-left origin (Pillow's convention); the converter's
existing v-flip already reconciles the two.

Dependencies: Pillow plus the Python standard library only.  No new
dependency is introduced.

Build-time impact: none -- this module is part of a stand-alone offline tool
and is never imported or invoked by the tile build pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, List, Tuple

# Frozen spec constants.
MAX_ATLAS_SIZE = 4096
DEFAULT_GUTTER = 4
# A textured group is atlasable only if every UV lies within the unit square
# widened by this tolerance; the tolerance also bounds the clamp error in
# :func:`remap_uv`.
UNIT_SQUARE_TOLERANCE = 0.01


@dataclass
class SourceTexture:
    """One cell to place in an atlas.

    ``key`` uniquely identifies the cell (the converter uses the glTF image
    index, or ``None`` for the shared untextured cell).  ``image`` is a
    Pillow image; ``is_untextured`` marks the solid-gray filler cell so it is
    ignored when deciding whether the atlas needs an alpha channel.
    """

    key: Hashable
    image: Any  # PIL.Image.Image (typed loosely to avoid a hard import here)
    is_untextured: bool = False


@dataclass(frozen=True)
class PlacedTexture:
    """Where a source texture landed, in atlas pixel coordinates.

    ``left`` is measured from the atlas left edge, ``bottom`` from the atlas
    bottom edge; ``width``/``height`` are the (possibly downscaled) cell size;
    ``atlas_size`` is the square atlas's side length.
    """

    key: Hashable
    atlas_index: int
    left: int
    bottom: int
    width: int
    height: int
    atlas_size: int


@dataclass
class PackResult:
    """Result of :func:`pack_textures`."""

    placements: Dict[Hashable, PlacedTexture]
    atlas_images: List[Any]  # list of PIL.Image.Image, one per atlas
    warnings: List[str] = field(default_factory=list)


def uvs_within_unit_square(
    vertices: List[Tuple[float, ...]],
    tolerance: float = UNIT_SQUARE_TOLERANCE,
) -> bool:
    """Return ``True`` if every vertex UV lies within the unit square.

    ``vertices`` are 8-tuples ``(px, py, pz, nx, ny, nz, u, v)``.  A group
    whose UVs all lie within ``[-tolerance, 1 + tolerance]`` on both axes can
    be atlased; anything beyond that tiles (X-Plane wraps it) and must keep
    its own texture.  The unit-square test is symmetric under the v-flip
    (``v -> 1 - v``), so it gives the same verdict before or after the flip.
    """
    low = -tolerance
    high = 1.0 + tolerance
    for vertex in vertices:
        u, v = vertex[6], vertex[7]
        if u < low or u > high or v < low or v > high:
            return False
    return True


def remap_uv(u: float, v: float, placed: PlacedTexture) -> Tuple[float, float]:
    """Map a source UV into its atlas cell (OBJ8 bottom-left v convention).

    ``u``/``v`` are the converter's already-v-flipped UVs.  Clamping to the
    unit square is safe because admission tolerated only +-tolerance of
    overshoot.
    """
    clamped_u = min(1.0, max(0.0, u))
    clamped_v = min(1.0, max(0.0, v))
    atlas_size = placed.atlas_size
    mapped_u = (placed.left + clamped_u * placed.width) / atlas_size
    mapped_v = (placed.bottom + clamped_v * placed.height) / atlas_size
    return mapped_u, mapped_v


def _next_power_of_two(value: int) -> int:
    """Smallest power of two greater than or equal to ``value`` (>= 1)."""
    power = 1
    while power < value:
        power *= 2
    return power


def _image_has_alpha(image: Any) -> bool:
    """Return ``True`` if ``image`` carries any non-opaque pixel."""
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        alpha = image.convert("RGBA").getchannel("A")
        return alpha.getextrema()[0] < 255
    return False


def _shelf_pack(
    items: List[Tuple[Hashable, int, int]], size: int, gutter: int
) -> Dict[Hashable, Tuple[int, int]]:
    """Greedy shelf packing into a ``size`` x ``size`` square.

    ``items`` are ``(key, width, height)`` sorted by descending height.
    Returns a ``{key: (left, top)}`` map (top-left pixel origin) for the
    cells that fit; every cell is inset by ``gutter`` from its neighbours and
    from the atlas edges so its bleed margin never overlaps another cell.
    """
    placements: Dict[Hashable, Tuple[int, int]] = {}
    shelf_left = gutter
    shelf_top = gutter
    shelf_height = 0
    for key, width, height in items:
        if shelf_left + width + gutter > size:
            # Start a new shelf below the current one.
            shelf_top += shelf_height + gutter
            shelf_left = gutter
            shelf_height = 0
        if shelf_top + height + gutter > size or shelf_left + width + gutter > size:
            # Does not fit this atlas at this size; a later, shorter item may.
            continue
        placements[key] = (shelf_left, shelf_top)
        shelf_left += width + gutter
        shelf_height = max(shelf_height, height)
    return placements


def _pack_one_atlas(
    items: List[Tuple[Hashable, int, int]], max_size: int, gutter: int
) -> Tuple[int, Dict[Hashable, Tuple[int, int]]]:
    """Choose the smallest power-of-two atlas that holds ``items``.

    Returns ``(atlas_size, placements)``.  If not even ``max_size`` holds
    every item, returns ``max_size`` with the greedy subset that did fit
    (the caller spills the remainder into another atlas).
    """
    size = 4
    fallback: Tuple[int, Dict[Hashable, Tuple[int, int]]] = (max_size, {})
    while size <= max_size:
        placements = _shelf_pack(items, size, gutter)
        if len(placements) == len(items):
            return size, placements
        fallback = (size, placements)
        size *= 2
    return fallback


def _fill_gutter(atlas: Any, content: Any, left: int, top: int, gutter: int) -> None:
    """Replicate ``content``'s edge pixels into its ``gutter`` margin.

    Top and bottom rows are extended first, then the left and right columns
    are extended over the taller (content + top/bottom margin) span so the
    four corners are filled from the nearest edge.  All writes are clamped to
    the atlas bounds, so a force-placed full-bleed-less cell is handled too.
    """
    width, height = content.size
    atlas_width, atlas_height = atlas.size

    top_row = content.crop((0, 0, width, 1))
    bottom_row = content.crop((0, height - 1, width, height))
    for step in range(1, gutter + 1):
        if top - step >= 0:
            atlas.paste(top_row, (left, top - step))
        if top + height - 1 + step < atlas_height:
            atlas.paste(bottom_row, (left, top + height - 1 + step))

    band_top = max(0, top - gutter)
    band_bottom = min(atlas_height, top + height + gutter)
    left_strip = atlas.crop((left, band_top, left + 1, band_bottom))
    right_strip = atlas.crop((left + width - 1, band_top, left + width, band_bottom))
    for step in range(1, gutter + 1):
        if left - step >= 0:
            atlas.paste(left_strip, (left - step, band_top))
        if left + width - 1 + step < atlas_width:
            atlas.paste(right_strip, (left + width - 1 + step, band_top))


def pack_textures(
    sources: List[SourceTexture],
    max_atlas_size: int = MAX_ATLAS_SIZE,
    gutter: int = DEFAULT_GUTTER,
) -> PackResult:
    """Pack ``sources`` into one or more square power-of-two atlases.

    Returns a :class:`PackResult` whose ``atlas_images`` are Pillow images
    (RGB, or RGBA when any real texture is non-opaque) and whose
    ``placements`` map each source key to its :class:`PlacedTexture`.
    """
    from PIL import Image

    warnings: List[str] = []

    prepared: List[Tuple[Hashable, Any, int, int, bool]] = []
    for source in sources:
        image = source.image
        width, height = image.size
        if width > max_atlas_size or height > max_atlas_size:
            scale = max_atlas_size / float(max(width, height))
            new_width = max(1, int(round(width * scale)))
            new_height = max(1, int(round(height * scale)))
            image = image.resize((new_width, new_height), Image.LANCZOS)
            warnings.append(
                f"texture cell {source.key!r}: {width}x{height} exceeds atlas "
                f"maximum {max_atlas_size}, downscaled to {new_width}x{new_height}"
            )
            width, height = new_width, new_height
        prepared.append((source.key, image, width, height, source.is_untextured))

    atlas_mode = (
        "RGBA"
        if any(
            _image_has_alpha(image)
            for _key, image, _w, _h, untextured in prepared
            if not untextured
        )
        else "RGB"
    )
    background = (128, 128, 128, 255) if atlas_mode == "RGBA" else (128, 128, 128)

    # Sort by descending height (then width, then a stable key) for shelf
    # packing and deterministic output.
    remaining = sorted(
        prepared, key=lambda item: (-item[3], -item[2], repr(item[0]))
    )

    placements: Dict[Hashable, PlacedTexture] = {}
    atlas_images: List[Any] = []
    atlas_index = 0

    while remaining:
        items = [(key, width, height) for key, _img, width, height, _unt in remaining]
        atlas_size, placed = _pack_one_atlas(items, max_atlas_size, gutter)
        if not placed:
            # A single cell larger than the atlas even alone: give it its own
            # tightly sized atlas (bleed margin is sacrificed by necessity).
            key, _img, width, height, _unt = remaining[0]
            atlas_size = _next_power_of_two(max(width, height))
            placed = {key: (0, 0)}

        item_by_key = {item[0]: item for item in remaining}
        atlas_image = Image.new(atlas_mode, (atlas_size, atlas_size), background)
        for key, (left, top) in placed.items():
            _key, image, width, height, _unt = item_by_key[key]
            cell = image.convert(atlas_mode)
            atlas_image.paste(cell, (left, top))
            _fill_gutter(atlas_image, cell, left, top, gutter)
            bottom = atlas_size - top - height
            placements[key] = PlacedTexture(
                key=key,
                atlas_index=atlas_index,
                left=left,
                bottom=bottom,
                width=width,
                height=height,
                atlas_size=atlas_size,
            )
        atlas_images.append(atlas_image)

        remaining = [item for item in remaining if item[0] not in placed]
        atlas_index += 1

    return PackResult(
        placements=placements, atlas_images=atlas_images, warnings=warnings
    )
