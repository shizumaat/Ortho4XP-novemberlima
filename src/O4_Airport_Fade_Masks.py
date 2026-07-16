"""Airport orthophoto fade geometry and grayscale fade-mask rasterisation.

This is the ``airport_ortho`` half of the ``texture_mode`` feature (see
``docs/specs/texture-mode-spec.md``, work package 3).  In that mode every land
triangle is textured with X-Plane default landclass terrain (the physical
base, produced by :mod:`O4_Default_Terrain_Map`), and orthophoto is drawn on
top -- as a non-physical overlay patch -- only on and around airports.  The
overlay fades out over a configurable band beyond the airport boundary, the
fade carried by a grayscale alpha mask that X-Plane blends through
``BORDER_TEX``.

This module supplies:

  * :func:`build_airport_ortho_geometry` -- reads the airport boundary polygons
    Ortho4XP already computed for the tile (the very geometry
    ``cover_airports_with_highres`` / ``cover_zl`` consume, cached in the
    per-tile ``.apt`` pickle, decision 7) and wraps them together with the fade
    band into an :class:`AirportOrthoGeometry`;
  * :class:`AirportOrthoGeometry` -- point queries (:meth:`~AirportOrthoGeometry.covers`,
    :meth:`~AirportOrthoGeometry.alpha_at`) used per triangle centroid by the
    DSF writer, and :meth:`~AirportOrthoGeometry.write_fade_mask`, which
    rasterises the alpha into a 4096x4096 grayscale PNG georeferenced exactly
    like the orthophoto texture tile it accompanies.

Fade law (decision 6): alpha = 1 (opaque ortho) everywhere inside the airport
boundary polygon; alpha ramps linearly 1 -> 0 from the boundary outward over
``airport_ortho_fade_width`` meters; 0 beyond.  ``covers`` is True inside the
boundary + fade band (i.e. the centroid gets an ortho overlay).

This is a core-pipeline module: it must never import a GUI toolkit.
"""
from __future__ import annotations

import math
import os
import pickle
from typing import Iterator

import numpy
from PIL import Image, ImageDraw
from shapely import affinity, ops
from shapely.geometry import MultiPolygon, Point, Polygon

import O4_File_Names as FNAMES
import O4_Geo_Utils as GEO
import O4_UI_Utils as UI

# Texture tiles are 4096x4096, the same size as the orthophoto DDS and the sea
# masks (``O4_Mask_Utils``).
_TEXTURE_SIZE = 4096

# Cap on the off-tile padding used when rasterising the fade mask.  It only
# matters for airport boundaries that lie outside the texture tile yet whose
# fade band still reaches into it; capping bounds the working-canvas memory.
_MAX_MASK_MARGIN = 2048


class AirportOrthoGeometry:
    """Airport boundary polygons plus the fade band, for one tile.

    The boundary is stored in *tile-local* longitude/latitude (i.e. absolute
    lon/lat minus the tile's south-west corner), matching how Ortho4XP caches
    airport boundaries in the per-tile ``.apt`` pickle.  Public point queries
    accept *absolute* WGS84 lon/lat -- what the DSF writer holds as a triangle
    centroid -- and translate internally.

    Distances (for the fade ramp) are measured in meters: the boundary is
    projected once into a local metric frame (degrees scaled by meters-per-
    degree at the tile's reference latitude), so :meth:`alpha_at` and
    :meth:`covers` reason directly in the ``airport_ortho_fade_width`` units.
    """

    def __init__(
        self,
        boundary,
        fade_width: float,
        tile_lon: float = 0.0,
        tile_lat: float = 0.0,
        ref_lat: float | None = None,
    ) -> None:
        """Wrap a boundary ``(Multi)Polygon`` (tile-local lon/lat) and a fade.

        ``boundary`` may be ``None`` or empty, in which case the geometry
        covers nothing (no airports on the tile).  ``fade_width`` is the fade
        band width in meters (clamped to >= 0).  ``tile_lon`` / ``tile_lat``
        are the tile's south-west corner used to translate absolute queries;
        ``ref_lat`` is the latitude at which meters-per-degree is evaluated
        (defaults to the tile center).
        """
        self.fade_width = max(0.0, float(fade_width))
        self.tile_lon = float(tile_lon)
        self.tile_lat = float(tile_lat)
        self.ref_lat = float(
            ref_lat if ref_lat is not None else self.tile_lat + 0.5)
        # Meters per degree at the reference latitude.
        self._lon_scale = GEO.lon_to_m(self.ref_lat)
        self._lat_scale = GEO.lat_to_m

        if boundary is not None and not boundary.is_empty:
            self._boundary = boundary
            # Metric-frame copy for meter-accurate distance queries.
            self._boundary_m = affinity.scale(
                boundary,
                xfact=self._lon_scale,
                yfact=self._lat_scale,
                origin=(0.0, 0.0),
            )
        else:
            self._boundary = None
            self._boundary_m = None

    # -- point queries -------------------------------------------------------

    def is_empty(self) -> bool:
        """True when there is no airport geometry on this tile."""
        return self._boundary_m is None

    def _distance_m(self, lon: float, lat: float) -> float:
        """Meters from the metric-frame boundary to absolute ``(lon, lat)``
        (0.0 for points inside the boundary)."""
        point = Point(
            (lon - self.tile_lon) * self._lon_scale,
            (lat - self.tile_lat) * self._lat_scale,
        )
        return self._boundary_m.distance(point)

    def covers(self, lon: float, lat: float) -> bool:
        """True inside boundary + fade width (this centroid gets an overlay).

        ``lon`` / ``lat`` are absolute WGS84 degrees.
        """
        if self._boundary_m is None:
            return False
        return self._distance_m(lon, lat) <= self.fade_width

    def alpha_at(self, lon: float, lat: float) -> float:
        """Overlay opacity at absolute ``(lon, lat)``.

        1.0 inside the boundary; a linear ramp down to 0.0 across the fade
        band; 0.0 beyond it (and everywhere when the tile has no airports).
        """
        if self._boundary_m is None:
            return 0.0
        distance = self._distance_m(lon, lat)
        if distance <= 0.0:
            return 1.0
        if self.fade_width <= 0.0:
            return 0.0
        return max(0.0, min(1.0, 1.0 - distance / self.fade_width))

    # -- rasterisation -------------------------------------------------------

    def write_fade_mask(
        self,
        til_x: int,
        til_y: int,
        zoomlevel: int,
        provider_code: str,
        mask_path: str,
    ) -> None:
        """Rasterise the fade alpha into a grayscale PNG at ``mask_path``.

        The mask is 4096x4096 and georeferenced exactly like the orthophoto
        DDS for texture tile ``(til_x, til_y)`` at ``zoomlevel``: mask pixel
        ``(col, row)`` maps to web-mercator pixel
        ``(til_x * 256 + col, til_y * 256 + row)``, so the fade mask shares the
        ortho tile's projection and can reuse its texture coordinates.  Pixel
        value ``round(alpha * 255)``: 255 opaque ortho, 0 fully faded to
        default terrain.
        """
        size = _TEXTURE_SIZE
        if self._boundary is None:
            # No airports -> a fully transparent (black) mask.
            Image.new("L", (size, size), 0).save(mask_path)
            return

        # Web-mercator pixel origin of the texture tile's top-left corner.
        (lat0, lon0) = GEO.gtile_to_wgs84(til_x, til_y, zoomlevel)
        (px0, py0) = GEO.wgs84_to_pix(lat0, lon0, zoomlevel)

        # Meters-per-pixel at the tile center (web mercator is locally
        # conformal, so a single scale for the ~km-scale fade is accurate).
        (lat_center, _lon_center) = GEO.pix_to_wgs84(
            px0 + size // 2, py0 + size // 2, zoomlevel)
        pixel_size_m = GEO.webmercator_pixel_size(lat_center, zoomlevel)
        if pixel_size_m <= 0:
            Image.new("L", (size, size), 0).save(mask_path)
            return

        # Pad the working canvas so a boundary that sits outside the tile can
        # still cast its fade band inward.  Distances beyond the fade band do
        # not influence in-tile alpha, so the reach is exactly the fade width.
        if self.fade_width > 0.0:
            margin = min(
                int(math.ceil(self.fade_width / pixel_size_m)) + 2,
                _MAX_MASK_MARGIN,
            )
        else:
            margin = 0

        canvas_size = size + 2 * margin
        inside_im = Image.new("L", (canvas_size, canvas_size), 0)
        draw = ImageDraw.Draw(inside_im)
        for polygon in _iter_polygons(self._boundary):
            exterior = self._ring_to_pixels(
                polygon.exterior.coords, zoomlevel, px0, py0, margin)
            if len(exterior) >= 3:
                draw.polygon(exterior, fill=255)
            for interior in polygon.interiors:
                hole = self._ring_to_pixels(
                    interior.coords, zoomlevel, px0, py0, margin)
                if len(hole) >= 3:
                    draw.polygon(hole, fill=0)
        del draw

        inside = numpy.array(inside_im, dtype=numpy.uint8) > 0
        del inside_im

        if self.fade_width <= 0.0:
            alpha = inside.astype(numpy.float32)
        elif inside.any():
            from scipy import ndimage

            # Distance (in pixels) from every outside pixel to the nearest
            # inside pixel; 0 inside.  Linear ramp over ``fade_px`` pixels.
            distance_px = ndimage.distance_transform_edt(~inside)
            fade_px = self.fade_width / pixel_size_m
            alpha = numpy.clip(1.0 - distance_px / fade_px, 0.0, 1.0)
            alpha = alpha.astype(numpy.float32)
        else:
            # Boundary present but nothing rasterised within reach.
            alpha = numpy.zeros((canvas_size, canvas_size), dtype=numpy.float32)

        # Crop the padding back off so the result is the texture-tile extent.
        if margin:
            alpha = alpha[margin:margin + size, margin:margin + size]

        mask = numpy.round(alpha * 255).astype(numpy.uint8)
        Image.fromarray(mask, "L").save(mask_path)

    def _ring_to_pixels(self, coords, zoomlevel, px0, py0, margin):
        """Convert a tile-local ring to canvas pixel ``(col, row)`` tuples."""
        pixels = []
        for (lon_local, lat_local) in coords:
            (pix_x, pix_y) = GEO.wgs84_to_pix(
                lat_local + self.tile_lat,
                lon_local + self.tile_lon,
                zoomlevel,
            )
            pixels.append((pix_x - px0 + margin, pix_y - py0 + margin))
        return pixels


def build_airport_ortho_geometry(tile) -> AirportOrthoGeometry:
    """Airport boundary polygons for ``tile`` plus its fade band.

    Reuses the very airport-area geometry ``cover_airports_with_highres`` /
    ``cover_zl`` consume (decision 7): the per-tile ``.apt`` pickle written by
    ``O4_Airport_Utils.update_airport_boundaries``, whose ``"boundary"`` entry
    is a tile-local shapely ``(Multi)Polygon`` per airport.  All airport
    boundaries on the tile are unioned; the fade width comes from
    ``tile.airport_ortho_fade_width``.  A missing/unreadable pickle or a tile
    with no airport boundaries yields an empty geometry (no ortho overlays).
    """
    fade_width = float(getattr(tile, "airport_ortho_fade_width", 1000.0))
    apt_path = FNAMES.apt_file(tile)
    boundaries = []
    try:
        with open(apt_path, "rb") as handle:
            dico_airports = pickle.load(handle)
    except OSError:
        UI.vprint(
            1,
            "   WARNING: airport info file",
            apt_path,
            "is missing (erased after Step 1?); airport_ortho mode will emit "
            "default terrain only for this tile.",
        )
        dico_airports = {}
    except (pickle.UnpicklingError, EOFError, ValueError):
        UI.vprint(
            1,
            "   WARNING: airport info file",
            apt_path,
            "is unreadable; airport_ortho mode will emit default terrain only "
            "for this tile.",
        )
        dico_airports = {}

    for airport in dico_airports:
        boundary = dico_airports[airport].get("boundary")
        if boundary is not None and not boundary.is_empty:
            boundaries.append(boundary)

    if not boundaries:
        merged = None
    else:
        merged = ops.unary_union(boundaries)
        if merged.is_empty:
            merged = None
        elif isinstance(merged, Polygon):
            merged = MultiPolygon([merged])

    return AirportOrthoGeometry(
        merged,
        fade_width,
        tile_lon=tile.lon,
        tile_lat=tile.lat,
        ref_lat=tile.lat + 0.5,
    )


def _iter_polygons(geometry) -> Iterator[Polygon]:
    """Yield the ``Polygon`` parts of a ``Polygon`` / ``MultiPolygon``."""
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        for polygon in geometry.geoms:
            if not polygon.is_empty:
                yield polygon
