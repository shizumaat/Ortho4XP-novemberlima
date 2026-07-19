"""Texture-atlas layout primitives for the OBJ8 building generator.

One building uses a single square texture atlas. The atlas is organized as:

* **Bands** — full-width horizontal strips that tile seamlessly in U
  (OpenGL repeat). Used for anything of unbounded horizontal extent:
  wall runs, glazing ribbons, roof seams. A band declares the physical
  width of one U repeat (``tile_width_meters``) and the physical height
  mapped across the band (``height_meters``); geometry converts meters
  to UV through it. U may leave [0, 1] freely (it wraps back into the
  same band because V is unchanged); V is clamped inside the band with
  a small inset to prevent bleeding into neighboring bands.
* **Rects** — arbitrary sub-rectangles addressed by unit fractions,
  stretched to fit the face they are applied to. Used for bounded
  features: roof caps, gable-end infills, doors, signage.

The texture painter uses the same objects (via ``pixel_rect``) so the
painted pixels and the UV mapping can never drift apart.

Build-time impact: none — not part of the tile build pipeline.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class AtlasBand:
    """A full-width horizontal atlas strip that tiles in U."""

    name: str
    v_bottom: float
    v_top: float
    tile_width_meters: float
    height_meters: float
    inset: float = 0.004

    def u_for(self, distance_meters: float) -> float:
        """U coordinate for a horizontal distance along the surface (wraps)."""
        return distance_meters / self.tile_width_meters

    def v_for(self, height_from_base_meters: float) -> float:
        """V coordinate for a height above the surface base (clamped)."""
        fraction = height_from_base_meters / self.height_meters
        fraction = min(1.0, max(0.0, fraction))
        v_low = self.v_bottom + self.inset
        v_high = self.v_top - self.inset
        return v_low + fraction * (v_high - v_low)

    def pixel_rect(self, atlas_size: int) -> tuple[int, int, int, int]:
        """(left, top, right, bottom) in image coordinates (y down)."""
        return (
            0,
            round((1.0 - self.v_top) * atlas_size),
            atlas_size,
            round((1.0 - self.v_bottom) * atlas_size),
        )


@dataclass(frozen=True)
class AtlasRect:
    """A bounded atlas rectangle, stretched to fit the face using it."""

    name: str
    u0: float
    v0: float
    u1: float
    v1: float
    inset: float = 0.004

    def uv_for(self, fraction_u: float, fraction_v: float) -> tuple[float, float]:
        """UV for unit fractions across the rectangle (0,0 = bottom-left)."""
        u_low, u_high = self.u0 + self.inset, self.u1 - self.inset
        v_low, v_high = self.v0 + self.inset, self.v1 - self.inset
        return (
            u_low + min(1.0, max(0.0, fraction_u)) * (u_high - u_low),
            v_low + min(1.0, max(0.0, fraction_v)) * (v_high - v_low),
        )

    def pixel_rect(self, atlas_size: int) -> tuple[int, int, int, int]:
        """(left, top, right, bottom) in image coordinates (y down)."""
        return (
            round(self.u0 * atlas_size),
            round((1.0 - self.v1) * atlas_size),
            round(self.u1 * atlas_size),
            round((1.0 - self.v0) * atlas_size),
        )


def stack_bands(
    specifications: Sequence[tuple[str, float, float]],
    v_bottom: float = 0.0,
    v_top: float = 1.0,
) -> dict[str, AtlasBand]:
    """Stack bands into [v_bottom, v_top], sized proportionally.

    Each specification is (name, tile_width_meters, height_meters); the
    vertical share of each band is proportional to its height_meters, so
    all bands get the same meters-per-texel vertical density.
    """
    total_height = sum(height for _, _, height in specifications)
    bands: dict[str, AtlasBand] = {}
    cursor = v_bottom
    for name, tile_width_meters, height_meters in specifications:
        share = (v_top - v_bottom) * height_meters / total_height
        bands[name] = AtlasBand(
            name=name,
            v_bottom=cursor,
            v_top=cursor + share,
            tile_width_meters=tile_width_meters,
            height_meters=height_meters,
        )
        cursor += share
    return bands
