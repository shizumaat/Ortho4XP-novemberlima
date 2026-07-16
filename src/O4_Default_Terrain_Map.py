"""Terrain-type lookup built from a default Global Scenery base-mesh DSF.

X-Plane's default Global Scenery textures every 1&deg; tile with landclass
*terrains* (asphalt, grass, rock, water, ...) baked into the tile's
base-mesh DSF.  The ``texture_mode`` feature (see
``docs/specs/texture-mode-spec.md``) reuses those terrain assignments so a
custom Ortho4XP mesh can be textured with X-Plane default terrain instead
of (or beside) orthophotos.

This module reads terrain-type-per-location out of the default DSF via the
bundled ``DSFTool --dsf2text`` dump (the same tool
``src/auto_patch/dsf_reader.py`` uses; 7z decompression is handled by
DSFTool).  No binary DSF parser is written.

Contract (frozen, spec section 4.1):

  * stream the text dump line by line (dumps run to hundreds of megabytes);
  * keep only patches whose flag has the physical bit (value 1) set;
  * expand primitive types 0 (triangles), 1 (strip), 2 (fan) into triangles
    tagged with the patch's terrain index;
  * store triangle vertices as ``(longitude, latitude)``;
  * spatial lookup via a shapely ``STRtree`` with nearest-triangle fallback
    for floating-point misses outside coverage;
  * ``terrain_paths`` preserves ``TERRAIN_DEF`` declaration order.

Water terrains are kept like any other terrain (callers filter if needed).

This is a core-pipeline module: it must never import a GUI toolkit.  It may
import ``O4_UI_Utils`` for prints and reuse ``auto_patch.dsf_reader``'s
DSFTool location + text-dump caching helpers.
"""
from __future__ import annotations

import os

from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

import O4_File_Names as FNAMES
import O4_Overlay_Utils as OVL
import O4_UI_Utils as UI

# Reuse the shared DSFTool location + text-dump caching helpers rather than
# duplicating the binary-location / conversion-cache logic.
from auto_patch.dsf_reader import ensure_dsf_text_path


# A triangle vertex is (longitude, latitude).
_Vertex = "tuple[float, float]"
_Triangle = "tuple[tuple[float, float], tuple[float, float], tuple[float, float]]"

# Patch-flag bit that marks a physical (base-mesh) patch.  Overlay patches
# (flag 2) draw above the physical layer and are ignored: their terrain is a
# decorative border blend, not the exactly-once base coverage this map needs.
_PHYSICAL_FLAG_BIT = 1


def _dump_cache_dir() -> str:
    """Directory for DSFTool text dumps of default Global Scenery DSFs.

    Dumps run to hundreds of megabytes and the source DSF lives inside the
    X-Plane install, so they are cached under the Ortho4XP root
    (``FNAMES.Default_dsf_cache_dir``) — never next to the source DSF and
    never inside a scenery pack.
    """
    os.makedirs(FNAMES.Default_dsf_cache_dir, exist_ok=True)
    return FNAMES.Default_dsf_cache_dir


class DefaultTerrainMap:
    """Terrain-type lookup built from a default Global Scenery base-mesh DSF."""

    def __init__(
        self,
        terrain_paths: list[str],
        triangles: list[tuple[
            tuple[float, float], tuple[float, float], tuple[float, float]]],
        triangle_terrain_index: list[int],
        dsf_path: str | None = None,
    ) -> None:
        """Low-level constructor.  Prefer :meth:`from_dsf` / :meth:`from_tile`.

        ``triangles`` and ``triangle_terrain_index`` are parallel lists: the
        i-th triangle is textured with terrain ``terrain_paths[
        triangle_terrain_index[i]]``.
        """
        self._terrain_paths = list(terrain_paths)
        self._triangles = triangles
        self._triangle_terrain_index = triangle_terrain_index
        self._dsf_path = dsf_path
        self._xplane_root = (
            _derive_xplane_root(dsf_path) if dsf_path else None)
        self._pack_root = _pack_root_for_dsf(dsf_path) if dsf_path else None
        # Cache of resolved-and-inspected .ter projection state per index.
        self._projected_cache: dict[int, bool | None] = {}

        # Spatial index over the triangle polygons.  ``STRtree.query`` /
        # ``.nearest`` return positional indices into this geometry list,
        # which are exactly the indices into ``_triangle_terrain_index``.
        self._polygons = [Polygon(tri) for tri in triangles]
        self._tree = STRtree(self._polygons) if self._polygons else None

    # ── construction ────────────────────────────────────────────────────

    @classmethod
    def from_dsf(cls, dsf_path: str) -> "DefaultTerrainMap":
        """Parse the DSFTool text dump of ``dsf_path`` (7z handled by
        DSFTool) into a terrain-type lookup.

        Raises ``FileNotFoundError`` if the DSF (or its DSFTool text dump)
        cannot be produced; callers wanting a soft failure use
        :meth:`from_tile`, which returns ``None`` instead.

        The text dump is cached under ``FNAMES.Default_dsf_cache_dir`` —
        never next to the source DSF, which may live inside the X-Plane
        install.
        """
        text_path = ensure_dsf_text_path(dsf_path, cache_dir=_dump_cache_dir())
        if text_path is None:
            raise FileNotFoundError(
                f"Could not produce a DSFTool text dump for {dsf_path!r} "
                "(missing DSF, missing DSFTool binary, or conversion error)."
            )
        return cls._from_text_path(text_path, dsf_path=dsf_path)

    @classmethod
    def from_tile(cls, lat: int, lon: int) -> "DefaultTerrainMap | None":
        """Locate the default DSF for tile ``(lat, lon)`` under
        ``O4_Overlay_Utils.custom_overlay_src`` and parse it.

        Resolution mirrors
        ``O4_DSF_Utils.extract_elevation_and_bathymetry_data``
        (``src/O4_DSF_Utils.py:363-379``): the primary source is
        ``custom_overlay_src/Earth nav data/<grp>/<tile>.dsf`` with a
        fallback to ``custom_overlay_src_alternate``.  Returns ``None`` with
        a clear printed error if no DSF is found.
        """
        relative = os.path.join(
            "Earth nav data", FNAMES.long_latlon(lat, lon) + ".dsf")
        candidates = []
        if OVL.custom_overlay_src:
            candidates.append(os.path.join(OVL.custom_overlay_src, relative))
        if OVL.custom_overlay_src_alternate:
            candidates.append(
                os.path.join(OVL.custom_overlay_src_alternate, relative))

        dsf_path = next(
            (c for c in candidates if os.path.isfile(c)), None)
        if dsf_path is None:
            UI.lvprint(
                1,
                "   ERROR: no default Global Scenery DSF found for tile "
                f"{FNAMES.short_latlon(lat, lon)} under custom_overlay_src"
                f" ({OVL.custom_overlay_src!r}) or custom_overlay_src_alternate"
                f" ({OVL.custom_overlay_src_alternate!r}); set the Global"
                " Scenery directory in the config window first.")
            return None

        text_path = ensure_dsf_text_path(dsf_path, cache_dir=_dump_cache_dir())
        if text_path is None:
            UI.lvprint(
                1,
                "   ERROR: could not produce a DSFTool text dump for the "
                f"default DSF {dsf_path!r} (DSFTool binary missing or "
                "conversion failed).")
            return None
        return cls._from_text_path(text_path, dsf_path=dsf_path)

    @classmethod
    def _from_text_path(
        cls, text_path: str, dsf_path: str | None = None,
    ) -> "DefaultTerrainMap":
        """Build a map by streaming a DSFTool ``--dsf2text`` file from disk,
        one line at a time (the dump may be hundreds of megabytes)."""
        with open(text_path, "r", encoding="utf-8", errors="replace") as handle:
            return cls._from_text_lines(handle, dsf_path=dsf_path)

    @classmethod
    def _from_text_lines(
        cls, lines, dsf_path: str | None = None,
    ) -> "DefaultTerrainMap":
        """Build a map from an iterable of DSFTool text-dump lines.

        ``lines`` is consumed lazily, so a file handle is streamed rather
        than read fully into memory.  Grammar::

            TERRAIN_DEF <path>
            BEGIN_PATCH <terrainIdx> <nearLOD> <farLOD> <flags> <coordDepth>
            BEGIN_PRIMITIVE <0|1|2>
            PATCH_VERTEX <lon> <lat> <elev> <nx> <nz> [s t [s2 t2]]
            END_PRIMITIVE
            END_PATCH
        """
        terrain_paths: list[str] = []
        triangles: list = []
        triangle_terrain_index: list[int] = []

        # Per-patch parse state.
        patch_terrain_index = -1
        patch_is_physical = False
        # Per-primitive parse state.
        primitive_type = -1
        primitive_vertices: list[tuple[float, float]] = []

        def _flush_primitive() -> None:
            if not patch_is_physical:
                primitive_vertices.clear()
                return
            for tri in _primitive_triangles(primitive_type, primitive_vertices):
                triangles.append(tri)
                triangle_terrain_index.append(patch_terrain_index)
            primitive_vertices.clear()

        for raw in lines:
            # Fast reject of the common vertex-line case first.
            if raw.startswith("PATCH_VERTEX"):
                if not patch_is_physical or primitive_type < 0:
                    continue
                tokens = raw.split()
                try:
                    lon = float(tokens[1])
                    lat = float(tokens[2])
                except (IndexError, ValueError):
                    continue
                primitive_vertices.append((lon, lat))
                continue
            if raw.startswith("TERRAIN_DEF"):
                tokens = raw.strip().split(maxsplit=1)
                terrain_paths.append(tokens[1].strip() if len(tokens) > 1 else "")
                continue
            if raw.startswith("BEGIN_PATCH"):
                tokens = raw.split()
                try:
                    patch_terrain_index = int(tokens[1])
                    flags = int(tokens[4])
                except (IndexError, ValueError):
                    patch_terrain_index = -1
                    patch_is_physical = False
                else:
                    patch_is_physical = bool(flags & _PHYSICAL_FLAG_BIT)
                primitive_type = -1
                primitive_vertices.clear()
                continue
            if raw.startswith("BEGIN_PRIMITIVE"):
                tokens = raw.split()
                try:
                    primitive_type = int(tokens[1])
                except (IndexError, ValueError):
                    primitive_type = -1
                primitive_vertices.clear()
                continue
            if raw.startswith("END_PRIMITIVE"):
                _flush_primitive()
                primitive_type = -1
                continue
            if raw.startswith("END_PATCH"):
                # Defensive: emit any primitive left unterminated.
                if primitive_type >= 0 and primitive_vertices:
                    _flush_primitive()
                patch_terrain_index = -1
                patch_is_physical = False
                primitive_type = -1
                primitive_vertices.clear()
                continue

        return cls(
            terrain_paths=terrain_paths,
            triangles=triangles,
            triangle_terrain_index=triangle_terrain_index,
            dsf_path=dsf_path,
        )

    # ── public interface ────────────────────────────────────────────────

    @property
    def terrain_paths(self) -> list[str]:
        """TERRAIN_DEF table, index order preserved."""
        return list(self._terrain_paths)

    def terrain_index_at(self, lon: float, lat: float) -> int:
        """Terrain index of the physical triangle containing ``(lon, lat)``.

        Uses a shapely ``STRtree`` for the containment query; on a
        floating-point miss (point just outside every triangle, e.g. a
        centroid exactly on the tile boundary), falls back to the nearest
        triangle.  Returns ``-1`` only when the map holds no triangles.
        """
        if self._tree is None:
            return -1
        point = Point(lon, lat)
        hits = self._tree.query(point, predicate="intersects")
        if len(hits):
            # Any incident triangle is acceptable on a shared edge/vertex.
            return self._triangle_terrain_index[int(hits[0])]
        # Nearest-triangle fallback for out-of-coverage misses.
        nearest_index = int(self._tree.nearest(point))
        return self._triangle_terrain_index[nearest_index]

    def terrain_path_at(self, lon: float, lat: float) -> str:
        """Convenience: ``terrain_paths[terrain_index_at(lon, lat)]``."""
        index = self.terrain_index_at(lon, lat)
        if index < 0 or index >= len(self._terrain_paths):
            return ""
        return self._terrain_paths[index]

    def is_projected(self, terrain_index: int) -> bool | None:
        """Whether terrain ``terrain_index`` is a ``PROJECTED`` terrain.

        Returns ``True``/``False`` when the ``.ter`` file could be resolved
        and read under the X-Plane install; ``None`` when the terrain
        resource was not resolvable (no install root derivable, path is not
        a ``.ter``, or the file could not be located).
        """
        if terrain_index in self._projected_cache:
            return self._projected_cache[terrain_index]
        result = self._inspect_projection(terrain_index)
        self._projected_cache[terrain_index] = result
        return result

    def _inspect_projection(self, terrain_index: int) -> bool | None:
        if terrain_index < 0 or terrain_index >= len(self._terrain_paths):
            return None
        virtual_path = self._terrain_paths[terrain_index]
        if not virtual_path.lower().endswith(".ter"):
            return None

        physical = self._resolve_terrain_file(virtual_path)
        if physical is None:
            return None
        try:
            with open(physical, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.split()[:1] == ["PROJECTED"]:
                        return True
        except OSError:
            return None
        return False

    def _resolve_terrain_file(self, virtual_path: str) -> str | None:
        """Resolve a terrain resource path to an absolute ``.ter`` file.

        Pack-relative wins (the resource sits inside the scenery pack the
        DSF belongs to), else the X-Plane ``library.txt`` virtual->physical
        map, mirroring how the rest of the reader resolves library paths.
        """
        if self._pack_root:
            candidate = os.path.join(self._pack_root, virtual_path)
            if os.path.isfile(candidate):
                return candidate
        if self._xplane_root:
            try:
                from auto_patch.agp_reader import resolve_library_path
                physical = resolve_library_path(virtual_path, self._xplane_root)
            except (OSError, ValueError):
                physical = None
            if physical is not None and os.path.isfile(physical):
                return physical
        return None


# ── primitive expansion ────────────────────────────────────────────────

def _primitive_triangles(
    primitive_type: int, vertices: list[tuple[float, float]],
) -> list:
    """Expand a DSF primitive into a list of ``(v0, v1, v2)`` triangles.

    Winding is preserved consistently: ``0`` (independent triangles) keeps
    each triple as authored; ``1`` (triangle strip) flips every other
    triangle's first two vertices so all triangles wind the same way; ``2``
    (triangle fan) pivots on the first vertex.  Only triangles with three
    distinct vertices are emitted (degenerate slivers are dropped).
    """
    result = []
    count = len(vertices)
    if primitive_type == 0:  # independent triangles
        for i in range(0, count - count % 3, 3):
            _append_triangle(result, vertices[i], vertices[i + 1],
                             vertices[i + 2])
    elif primitive_type == 1:  # triangle strip
        for i in range(count - 2):
            if i % 2 == 0:
                _append_triangle(result, vertices[i], vertices[i + 1],
                                 vertices[i + 2])
            else:
                _append_triangle(result, vertices[i + 1], vertices[i],
                                 vertices[i + 2])
    elif primitive_type == 2:  # triangle fan
        for i in range(1, count - 1):
            _append_triangle(result, vertices[0], vertices[i],
                             vertices[i + 1])
    return result


def _append_triangle(result: list, a, b, c) -> None:
    """Append ``(a, b, c)`` unless two vertices coincide (degenerate)."""
    if a == b or b == c or a == c:
        return
    result.append((a, b, c))


# ── X-Plane install resolution ──────────────────────────────────────────

def _pack_root_for_dsf(dsf_path: str | None) -> str | None:
    """Scenery-pack directory a DSF belongs to
    (``<pack>/Earth nav data/<grp>/<tile>.dsf`` -> ``<pack>``)."""
    if not dsf_path:
        return None
    try:
        pack = os.path.dirname(os.path.dirname(os.path.dirname(dsf_path)))
        return pack if os.path.isdir(pack) else None
    except (OSError, ValueError):
        return None


def _derive_xplane_root(dsf_path: str | None) -> str | None:
    """Best-effort X-Plane install root for a default-scenery DSF path.

    Default Global Scenery lives at
    ``<xplane>/Global Scenery/<pack>/Earth nav data/<grp>/<tile>.dsf`` (and
    Custom Scenery packs mirror the layout under ``Custom Scenery``).  The
    pack directory is three levels up; the install root is two levels above
    the pack when the pack's parent is a recognised scenery container.
    Returns ``None`` when the layout does not match, in which case
    ``is_projected`` reports ``None``.
    """
    pack = _pack_root_for_dsf(dsf_path)
    if not pack:
        return None
    container = os.path.dirname(pack)
    if os.path.basename(container) in ("Global Scenery", "Custom Scenery"):
        root = os.path.dirname(container)
        return root if os.path.isdir(root) else None
    return None
