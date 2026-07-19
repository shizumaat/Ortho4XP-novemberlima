"""Material-fidelity helpers for the MSFS glTF/GLB -> X-Plane OBJ8 converter.

The base converter (``convert.py``) groups primitives by albedo texture and
emits one OBJ8 per group.  Many MSFS materials, however, carry no texture at
all -- they are solid ``baseColorFactor`` colors (trim, mullions, window
frames) -- plus scalar PBR (roughness/metallic), emissive night-window
switches, and, in other packages, normal maps.  Collapsing all of those into
one flat gray object throws the information away.

This module holds the pure colour/image helpers that let the converter keep
that fidelity without a per-material object explosion:

* the linear->sRGB transfer (glTF factors are LINEAR; X-Plane textures are
  sRGB, so a raw copy would render everything too dark);
* a *factor palette*: one small PNG per model whose 16x16-pixel cells each
  hold one distinct factor colour, so hundreds of untextured materials share
  a single texture bind and their primitives merely point at a cell centre;
* the matching night-lighting (``TEXTURE_LIT``) palette (cell = factor x
  emissive) and textured-albedo LIT scaling;
* normal-map preparation (ASOBO DirectX->OpenGL green-channel flip, gloss in
  the alpha channel).

It knows nothing about glTF or OBJ8 file structure; the converter owns that.

Dependencies: Pillow plus the Python standard library only.  No new
dependency is introduced.

Build-time impact: none -- this module is part of a stand-alone offline tool
and is never imported or invoked by the tile build pipeline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, List, Sequence, Tuple

# Side length, in pixels, of one palette cell.  A cell is a flat colour, so a
# small square is plenty; 16 leaves a comfortable margin against bilinear
# sampling bleeding a neighbour in at the cell centre.
CELL_SIZE = 16
# Emissive is "on" when any channel exceeds this (guards against float noise).
EMISSIVE_EPSILON = 1e-6


# --------------------------------------------------------------------------
# Colour transfer.
# --------------------------------------------------------------------------
def linear_to_srgb(value: float) -> float:
    """Convert one LINEAR colour component to sRGB (IEC 61966-2-1 transfer).

    glTF ``baseColorFactor`` / ``emissiveFactor`` are linear; X-Plane samples
    albedo/LIT textures as sRGB, so a factor must pass through this transfer
    before it is painted (a raw copy renders every factor colour too dark).
    """
    clamped = 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)
    if clamped <= 0.0031308:
        return 12.92 * clamped
    return 1.055 * (clamped ** (1.0 / 2.4)) - 0.055


def to_byte(value: float) -> int:
    """Clamp a 0..1 value and quantise it to an 8-bit channel (0..255)."""
    scaled = int(round(value * 255.0))
    return 0 if scaled < 0 else (255 if scaled > 255 else scaled)


def srgb_byte(linear_value: float) -> int:
    """Linear component -> sRGB -> 8-bit byte (the palette painting path)."""
    return to_byte(linear_to_srgb(linear_value))


# --------------------------------------------------------------------------
# Factor-cell identity and bucketing.
# --------------------------------------------------------------------------
def roughness_bucket(roughness: float) -> float:
    """Quantise roughness so near-identical materials share one palette cell."""
    return round(roughness, 2)


def emissive_bucket(emissive: Sequence[float]) -> Tuple[float, float, float]:
    """Quantise an emissive RGB triple for palette-cell de-duplication."""
    return (round(emissive[0], 3), round(emissive[1], 3), round(emissive[2], 3))


def is_emissive(emissive: Sequence[float]) -> bool:
    """Return ``True`` if any emissive channel is meaningfully above zero."""
    return any(component > EMISSIVE_EPSILON for component in emissive)


@dataclass(frozen=True)
class FactorCell:
    """One distinct factor material occupying a single palette cell.

    ``base_color`` is linear RGBA (alpha carries translucency), ``roughness``
    the scalar roughness factor, and ``emissive`` the linear emissive RGB.
    Two materials with the same :meth:`key` share a cell.
    """

    base_color: Tuple[float, float, float, float]
    roughness: float
    emissive: Tuple[float, float, float]

    def key(self) -> Tuple[Any, ...]:
        """Hashable identity used to de-duplicate cells across a model."""
        rgba = tuple(round(component, 4) for component in self.base_color)
        return (rgba, roughness_bucket(self.roughness),
                emissive_bucket(self.emissive))


# --------------------------------------------------------------------------
# Palette layout and UV mapping.
# --------------------------------------------------------------------------
def _next_power_of_two(value: int) -> int:
    """Smallest power of two greater than or equal to ``value`` (>= 1)."""
    power = 1
    while power < value:
        power *= 2
    return power


@dataclass(frozen=True)
class PaletteLayout:
    """Grid geometry of a factor palette PNG.

    ``columns``/``rows`` count cells; ``width``/``height`` are the power-of-two
    canvas size in pixels; ``cell_size`` is a cell's pixel side length.
    """

    columns: int
    rows: int
    width: int
    height: int
    cell_size: int


def palette_layout(cell_count: int, cell_size: int = CELL_SIZE) -> PaletteLayout:
    """Choose a near-square power-of-two canvas holding ``cell_count`` cells."""
    count = max(1, cell_count)
    columns = max(1, math.ceil(math.sqrt(count)))
    rows = max(1, math.ceil(count / columns))
    width = _next_power_of_two(columns * cell_size)
    height = _next_power_of_two(rows * cell_size)
    return PaletteLayout(columns, rows, width, height, cell_size)


def cell_center_uv(cell_index: int, layout: PaletteLayout) -> Tuple[float, float]:
    """Return the OBJ8-space (u, v) of cell ``cell_index``'s centre.

    The v axis is returned already flipped to OBJ8's bottom-left origin
    (``v_obj8 = 1 - v_gltf``), so the converter uses it verbatim without its
    usual per-vertex flip.
    """
    column = cell_index % layout.columns
    row = cell_index // layout.columns
    center_x = column * layout.cell_size + layout.cell_size / 2.0
    center_y_top = row * layout.cell_size + layout.cell_size / 2.0
    u = center_x / layout.width
    v = 1.0 - center_y_top / layout.height
    return u, v


def _paint_cell(image: Any, cell_index: int, layout: PaletteLayout,
                rgba: Tuple[int, int, int, int]) -> None:
    """Paste one solid ``rgba`` cell into ``image`` at ``cell_index``."""
    from PIL import Image

    column = cell_index % layout.columns
    row = cell_index // layout.columns
    left = column * layout.cell_size
    top = row * layout.cell_size
    cell = Image.new("RGBA", (layout.cell_size, layout.cell_size), rgba)
    image.paste(cell, (left, top))


def bake_palette_image(cells: List[FactorCell], layout: PaletteLayout) -> Any:
    """Bake the day/albedo palette: each cell = sRGB(base colour), with alpha.

    Returns an RGBA Pillow image (translucent factors keep their alpha so the
    glass palette object can blend them).
    """
    from PIL import Image

    image = Image.new("RGBA", (layout.width, layout.height), (0, 0, 0, 0))
    for index, cell in enumerate(cells):
        red = srgb_byte(cell.base_color[0])
        green = srgb_byte(cell.base_color[1])
        blue = srgb_byte(cell.base_color[2])
        # X-Plane has no global-illumination ambient lift: an authored
        # pure-black factor renders as a featureless void (MSFS's own
        # ambient keeps such trim readable).  Floor near-black albedo at
        # charcoal so dark trim stays visible in shadow.
        if max(red, green, blue) < 24:
            red, green, blue = max(red, 22), max(green, 24), max(blue, 26)
        alpha = to_byte(cell.base_color[3])
        _paint_cell(image, index, layout, (red, green, blue, alpha))
    return image


def bake_palette_lit_image(cells: List[FactorCell], layout: PaletteLayout) -> Any:
    """Bake the night/LIT palette: each cell = sRGB(base colour x emissive).

    Non-emissive cells stay black so an object bound to this LIT texture only
    glows where its own emissive cells sit.  The cell layout matches
    :func:`bake_palette_image` so the same UVs address both.
    """
    from PIL import Image

    image = Image.new("RGBA", (layout.width, layout.height), (0, 0, 0, 255))
    for index, cell in enumerate(cells):
        if not is_emissive(cell.emissive):
            continue
        red = srgb_byte(cell.base_color[0] * cell.emissive[0])
        green = srgb_byte(cell.base_color[1] * cell.emissive[1])
        blue = srgb_byte(cell.base_color[2] * cell.emissive[2])
        _paint_cell(image, index, layout, (red, green, blue, 255))
    return image


# --------------------------------------------------------------------------
# Textured LIT and normal-map preparation.
# --------------------------------------------------------------------------
def bake_textured_lit_image(
    albedo_image: Any, emissive: Tuple[float, float, float]
) -> Any:
    """Scale an albedo texture by ``emissive`` per channel for a LIT map.

    Albedo pixels are already sRGB, so this multiplies the stored bytes by the
    (0..1) emissive factor and clamps -- the specified behaviour for MSFS
    day/night windows whose lit appearance is the daytime albedo dimmed to the
    emissive strength.
    """
    from PIL import Image

    rgba = albedo_image.convert("RGBA")
    red, green, blue, _alpha = rgba.split()
    red = red.point(lambda value: to_byte(value / 255.0 * emissive[0]))
    green = green.point(lambda value: to_byte(value / 255.0 * emissive[1]))
    blue = blue.point(lambda value: to_byte(value / 255.0 * emissive[2]))
    opaque = Image.new("L", rgba.size, 255)
    return Image.merge("RGBA", (red, green, blue, opaque))


def build_normal_image(
    source_image: Any, flip_green: bool, gloss: float
) -> Any:
    """Prepare a normal map for X-Plane: optional green flip, gloss in alpha.

    X-Plane reads OpenGL-convention normals (green = +Y) and takes gloss
    (``1 - roughness``) from the normal texture's alpha channel.  MSFS ships
    DirectX-convention normals (green = -Y) flagged by
    ``ASOBO_normal_map_convention``; ``flip_green`` inverts the green channel
    to reconcile the two.
    """
    from PIL import Image

    rgba = source_image.convert("RGBA")
    red, green, blue, _alpha = rgba.split()
    if flip_green:
        green = green.point(lambda value: 255 - value)
    gloss_alpha = Image.new("L", rgba.size, to_byte(gloss))
    return Image.merge("RGBA", (red, green, blue, gloss_alpha))


# --------------------------------------------------------------------------
# Geometry weighting.
# --------------------------------------------------------------------------
def triangle_area(
    point_a: Sequence[float],
    point_b: Sequence[float],
    point_c: Sequence[float],
) -> float:
    """Return the area of the triangle spanned by three 3-D points."""
    edge_a = (point_b[0] - point_a[0], point_b[1] - point_a[1],
              point_b[2] - point_a[2])
    edge_b = (point_c[0] - point_a[0], point_c[1] - point_a[1],
              point_c[2] - point_a[2])
    cross = (
        edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
        edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
        edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
    )
    return 0.5 * math.sqrt(
        cross[0] * cross[0] + cross[1] * cross[1] + cross[2] * cross[2]
    )
