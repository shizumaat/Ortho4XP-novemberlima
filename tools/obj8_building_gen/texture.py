"""Procedural texture-atlas painter for the OBJ8 building generator.

Paints into the same AtlasBand / AtlasRect objects the geometry maps UVs
through, so painted pixels and UV mapping cannot drift apart. Band
painters are horizontally tile-safe: any repeating feature is drawn an
integer number of times per U tile (spacings are rounded to divisors of
the band's ``tile_width_meters``).

All randomness is seeded — the same inputs always paint the same atlas.

Build-time impact: none — not part of the tile build pipeline.
"""
from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw

from .atlas import AtlasBand, AtlasRect

Color = tuple[int, int, int]


def _blend(color_a: Color, color_b: Color, t: float) -> Color:
    return tuple(round(a + (b - a) * t) for a, b in zip(color_a, color_b))  # type: ignore[return-value]


class AtlasPainter:
    """Paints one square RGB atlas image."""

    def __init__(self, size: int = 2048, background: Color = (128, 128, 128)) -> None:
        self.size = size
        self.image = Image.new("RGB", (size, size), background)
        self.draw = ImageDraw.Draw(self.image)

    # -- shared helpers ---------------------------------------------------

    def _rect(self, target: AtlasBand | AtlasRect) -> tuple[int, int, int, int]:
        return target.pixel_rect(self.size)

    def _pixels_per_meter_u(self, band: AtlasBand) -> float:
        return self.size / band.tile_width_meters

    def _pixels_per_meter_v(self, band: AtlasBand) -> float:
        left, top, right, bottom = self._rect(band)
        return (bottom - top) / band.height_meters

    def _y_for_height(self, band: AtlasBand, height_meters: float) -> int:
        """Image y for a height above the band's base (V grows upward)."""
        left, top, right, bottom = self._rect(band)
        return bottom - round(height_meters * self._pixels_per_meter_v(band))

    # -- generic fills ----------------------------------------------------

    def flat(
        self,
        target: AtlasBand | AtlasRect,
        color: Color,
        noise_amplitude: int = 0,
        seed: int = 0,
    ) -> None:
        """Solid fill, optionally with per-column brightness noise
        (columns keep horizontal tileability)."""
        left, top, right, bottom = self._rect(target)
        self.draw.rectangle((left, top, right - 1, bottom - 1), fill=color)
        if noise_amplitude > 0:
            generator = random.Random(seed)
            for x in range(left, right):
                delta = generator.randint(-noise_amplitude, noise_amplitude)
                shaded = tuple(min(255, max(0, c + delta)) for c in color)
                self.draw.line((x, top, x, bottom - 1), fill=shaded)

    def vertical_gradient(
        self, target: AtlasBand | AtlasRect, bottom_color: Color, top_color: Color
    ) -> None:
        left, top, right, bottom = self._rect(target)
        span = max(bottom - top - 1, 1)
        for y in range(top, bottom):
            t = (bottom - 1 - y) / span
            self.draw.line((left, y, right - 1, y), fill=_blend(bottom_color, top_color, t))

    # -- band patterns (tile-safe in U) -----------------------------------

    def standing_seam(
        self,
        band: AtlasBand,
        panel_color: Color,
        seam_color: Color,
        seam_spacing_meters: float,
        noise_amplitude: int = 6,
        seed: int = 1,
    ) -> None:
        """Standing-seam metal roofing: vertical seams at even spacing."""
        self.flat(band, panel_color, noise_amplitude=noise_amplitude, seed=seed)
        left, top, right, bottom = self._rect(band)
        seam_count = max(1, round(band.tile_width_meters / seam_spacing_meters))
        for k in range(seam_count):
            x = left + round(k * self.size / seam_count)
            self.draw.line((x, top, x, bottom - 1), fill=seam_color)
            highlight = _blend(panel_color, (255, 255, 255), 0.18)
            self.draw.line((x + 1, top, x + 1, bottom - 1), fill=highlight)

    def lap_siding(
        self,
        band: AtlasBand,
        board_color: Color,
        shadow_color: Color,
        board_height_meters: float,
        band_bottom_meters: float = 0.0,
        band_top_meters: float | None = None,
        noise_amplitude: int = 5,
        seed: int = 2,
    ) -> None:
        """Horizontal lap boards between two heights within the band."""
        if band_top_meters is None:
            band_top_meters = band.height_meters
        left, top, right, bottom = self._rect(band)
        self.flat(band, board_color, noise_amplitude=noise_amplitude, seed=seed)
        height = band_bottom_meters
        while height <= band_top_meters:
            y = self._y_for_height(band, height)
            if top <= y < bottom:
                self.draw.line((left, y, right - 1, y), fill=shadow_color)
            height += board_height_meters

    def masonry(
        self,
        band: AtlasBand,
        stone_color: Color,
        mortar_color: Color,
        course_height_meters: float,
        stone_width_meters: float,
        top_height_meters: float | None = None,
        color_variation: int = 14,
        seed: int = 3,
    ) -> None:
        """Coursed masonry (stone or block) up to ``top_height_meters``.

        Stones per course are an integer per U tile; alternate courses
        offset half a stone, so the pattern tiles horizontally.
        """
        if top_height_meters is None:
            top_height_meters = band.height_meters
        left, top, right, bottom = self._rect(band)
        generator = random.Random(seed)
        stones_per_tile = max(1, round(band.tile_width_meters / stone_width_meters))
        stone_pixels = self.size / stones_per_tile
        course = 0
        height = 0.0
        while height < top_height_meters:
            y_low = self._y_for_height(band, height)
            y_high = self._y_for_height(band, min(height + course_height_meters, top_height_meters))
            offset = 0.5 * stone_pixels if course % 2 else 0.0
            for k in range(stones_per_tile + 1):
                x0 = left + round(k * stone_pixels - offset)
                x1 = left + round((k + 1) * stone_pixels - offset)
                shade = generator.randint(-color_variation, color_variation)
                fill = tuple(min(255, max(0, c + shade)) for c in stone_color)
                x_left = max(left, x0)
                x_right = min(right - 1, x1 - 2)
                y_top = max(top, y_high)
                y_bottom = min(bottom - 1, y_low)
                if x_right < x_left or y_bottom < y_top:
                    continue
                self.draw.rectangle((x_left, y_top, x_right, y_bottom), fill=fill)
            if top <= y_low < bottom:
                self.draw.line((left, y_low, right - 1, y_low), fill=mortar_color)
            course += 1
            height += course_height_meters

    def window_row(
        self,
        band: AtlasBand,
        sill_meters: float,
        head_meters: float,
        window_width_meters: float,
        spacing_meters: float,
        glass_color: Color,
        frame_color: Color,
        frame_pixels: int = 2,
        mullions_per_window: int = 0,
    ) -> None:
        """A repeating row of windows between two heights in a wall band."""
        left, top, right, bottom = self._rect(band)
        windows_per_tile = max(1, round(band.tile_width_meters / spacing_meters))
        spacing_pixels = self.size / windows_per_tile
        width_pixels = round(window_width_meters * self._pixels_per_meter_u(band))
        y_head = max(top, self._y_for_height(band, head_meters))
        y_sill = min(bottom - 1, self._y_for_height(band, sill_meters))
        for k in range(windows_per_tile):
            center = left + round((k + 0.5) * spacing_pixels)
            x0 = center - width_pixels // 2
            x1 = center + width_pixels // 2
            self.draw.rectangle((x0, y_head, x1, y_sill), fill=frame_color)
            self.draw.rectangle(
                (x0 + frame_pixels, y_head + frame_pixels, x1 - frame_pixels, y_sill - frame_pixels),
                fill=glass_color,
            )
            for m in range(1, mullions_per_window + 1):
                x_m = x0 + round(m * (x1 - x0) / (mullions_per_window + 1))
                self.draw.line((x_m, y_head, x_m, y_sill), fill=frame_color, width=frame_pixels)

    def glazing_grid(
        self,
        band: AtlasBand,
        glass_color: Color,
        mullion_color: Color,
        mullion_spacing_meters: float,
        transom_heights_meters: tuple[float, ...] = (),
        mullion_pixels: int = 3,
        glass_gradient_top: Color | None = None,
    ) -> None:
        """Full-height curtain wall: vertical mullions plus transoms."""
        left, top, right, bottom = self._rect(band)
        if glass_gradient_top is not None:
            self.vertical_gradient(band, glass_color, glass_gradient_top)
        else:
            self.flat(band, glass_color)
        mullion_count = max(1, round(band.tile_width_meters / mullion_spacing_meters))
        for k in range(mullion_count):
            x = left + round(k * self.size / mullion_count)
            self.draw.line((x, top, x, bottom - 1), fill=mullion_color, width=mullion_pixels)
        for height in transom_heights_meters:
            y = self._y_for_height(band, height)
            if top <= y < bottom:
                self.draw.line((left, y, right - 1, y), fill=mullion_color, width=mullion_pixels)
        self.draw.line((left, bottom - 2, right - 1, bottom - 2), fill=mullion_color, width=mullion_pixels)
        self.draw.line((left, top + 1, right - 1, top + 1), fill=mullion_color, width=mullion_pixels)

    # -- output -----------------------------------------------------------

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.image.save(path)
