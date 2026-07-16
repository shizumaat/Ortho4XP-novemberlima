"""Headless scanner for built Ortho4XP tiles.

This module ports the tile-detection logic of
``O4_GUI_Utils.OsInterface._preview_existing_tiles_inner`` without any
tkinter / UI dependency.  It is consumed by the new Qt front-end for its
map overlays and tile-info pane, but has no knowledge of any UI toolkit.

A "build directory" is the folder that holds a tile's ``Earth nav data``
tree (the ``.dsf``), its mesh/data files, its ``textures`` folder and the
per-tile ``Ortho4XP_<short_latlon>.cfg``.  Two on-disk layouts exist:

* *per-tile* (``grouped=False``) — ``working_dir`` is the parent of many
  ``*XP_<short_latlon>`` sub-directories, one build dir per tile.
* *grouped* (``grouped=True``) — ``working_dir`` **is** a single build dir
  that holds several tiles' ``.dsf`` files under one ``Earth nav data``.

The scanner never executes or ``eval``s configuration files; it parses at
most three keys line-wise, exactly mirroring the legacy detection code.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Iterator, Optional

import O4_File_Names as FNAMES


@dataclass
class TileInfo:
    """Metadata describing one built (or partially built) Ortho4XP tile."""

    lat: int
    lon: int
    build_dir: str            # absolute path of the tile's build directory
    dir_name: str             # basename of the build directory
    dsf_present: bool
    provider: str = ""        # default_website from cfg, "" if unknown
    zl: Optional[int] = None  # default_zl from cfg
    has_zones: bool = False   # non-trivial zone_list in cfg
    custom_dem: str = ""      # pinned elevation source from cfg, "" if unset
    mesh_date: Optional[float] = None     # unix mtime of newest dsf/mesh
    imagery_date: Optional[float] = None  # unix mtime of newest texture
    size_bytes: Optional[int] = None      # None until compute_size()


##############################################################################
# Internal helpers
##############################################################################
def _cfg_candidates(build_dir: str, lat: int, lon: int) -> list[str]:
    """Return the config paths to try, in priority order.

    The short-latlon named config is preferred over the legacy generic
    ``Ortho4XP.cfg`` name, matching the legacy detection code.
    """
    return [
        os.path.join(
            build_dir, "Ortho4XP_" + FNAMES.short_latlon(lat, lon) + ".cfg"
        ),
        os.path.join(build_dir, "Ortho4XP.cfg"),
    ]


def _parse_cfg(
    cfg_path: str,
) -> tuple[str, Optional[int], bool, str]:
    """Parse the four keys of interest from a tile config file.

    Returns ``(provider, zl, has_zones, custom_dem)``.  ``provider`` is
    ``""`` when the ``default_website`` line is absent, ``zl`` is ``None``
    when ``default_zl`` is missing or non-integer, ``has_zones`` mirrors
    the legacy ``len(line[10:]) > 3`` test on the ``zone_list`` line, and
    ``custom_dem`` is the tile's pinned elevation source (``""`` when
    unset) for the info pane's elevation row.

    Only these keys are inspected; the file is never executed.
    """
    provider = ""
    zl: Optional[int] = None
    has_zones = False
    custom_dem = ""
    try:
        with open(cfg_path, "r") as f:
            for line in f.readlines():
                if line[:15] == "default_website":
                    provider = line.strip().split("=", 1)[1].strip()
                elif line[:10] == "default_zl":
                    try:
                        zl = int(line.strip().split("=", 1)[1])
                    except (ValueError, IndexError):
                        zl = None
                elif line[:10] == "custom_dem":
                    custom_dem = line.strip().split("=", 1)[1].strip()
                elif line[:9] == "zone_list" and len(line[10:]) > 3:
                    has_zones = True
    except OSError:
        pass
    return provider, zl, has_zones, custom_dem


def _mesh_date(build_dir: str) -> Optional[float]:
    """Newest mtime among the tile's ``.dsf`` and ``Data*.mesh`` files."""
    candidates = glob.glob(
        os.path.join(build_dir, "Earth nav data", "**", "*.dsf"),
        recursive=True,
    )
    candidates += glob.glob(os.path.join(build_dir, "Data*.mesh"))
    mtimes = []
    for path in candidates:
        try:
            mtimes.append(os.path.getmtime(path))
        except OSError:
            pass
    return max(mtimes) if mtimes else None


def _imagery_date(build_dir: str) -> Optional[float]:
    """Newest mtime among files directly under ``<build_dir>/textures/``."""
    tex_dir = os.path.join(build_dir, "textures")
    if not os.path.isdir(tex_dir):
        return None
    mtimes = []
    for entry in os.listdir(tex_dir):
        path = os.path.join(tex_dir, entry)
        if os.path.isfile(path):
            try:
                mtimes.append(os.path.getmtime(path))
            except OSError:
                pass
    return max(mtimes) if mtimes else None


def _build_tile_info(
    build_dir: str, lat: int, lon: int, dir_name: str
) -> Optional[TileInfo]:
    """Assemble a :class:`TileInfo` for one build directory, or ``None``.

    A tile is reported when its ``.dsf`` exists (``dsf_present=True``) or,
    failing that, when a tile config file exists (``dsf_present=False``).
    When neither is present ``None`` is returned so the caller skips it.
    """
    dsf_path = os.path.join(
        build_dir, "Earth nav data", FNAMES.long_latlon(lat, lon) + ".dsf"
    )
    dsf_present = os.path.isfile(dsf_path)

    cfg_path = next(
        (p for p in _cfg_candidates(build_dir, lat, lon) if os.path.isfile(p)),
        None,
    )

    if not dsf_present and cfg_path is None:
        return None

    provider, zl, has_zones, custom_dem = ("", None, False, "")
    if cfg_path is not None:
        provider, zl, has_zones, custom_dem = _parse_cfg(cfg_path)

    return TileInfo(
        lat=lat,
        lon=lon,
        build_dir=os.path.abspath(build_dir),
        dir_name=dir_name,
        dsf_present=dsf_present,
        provider=provider,
        zl=zl,
        has_zones=has_zones,
        custom_dem=custom_dem,
        mesh_date=_mesh_date(build_dir),
        imagery_date=_imagery_date(build_dir),
        size_bytes=None,
    )


def iter_scan_tiles(
    working_dir: str,
) -> "Iterator[tuple[int, int, Optional[tuple[int, int]], Optional[TileInfo]]]":
    """Incremental form of :func:`scan_tiles` (per-tile mode) for live UIs.

    Yields ``(done, total, key, info)`` after EVERY directory entry
    examined — ``done`` counts entries processed so far, ``total`` is the
    entry count, and ``key``/``info`` carry the ``(lat, lon)`` and
    :class:`TileInfo` when that entry produced a new tile (``None``/``None``
    otherwise), so a consumer can both drive a progress bar and surface
    tiles as they are read.  Acceptance, "sorted, first wins" duplicate
    handling, and symlink traversal are identical to
    ``scan_tiles(working_dir)`` — that function is this generator drained.
    """
    try:
        # sorted: "first wins" for duplicate lat/lon must be deterministic
        # across filesystems (raw listdir order is creation-dependent on
        # APFS and arbitrary elsewhere; the duplicate-latlon test pins the
        # sorted-first winner).
        names = sorted(os.listdir(working_dir))
    except OSError:
        return
    total = len(names)
    seen: set[tuple[int, int]] = set()
    for done, dir_name in enumerate(names, start=1):
        key: Optional[tuple[int, int]] = None
        info: Optional[TileInfo] = None
        if "XP_" in dir_name:
            try:
                lat = int(dir_name.split("XP_")[1][:3])
                lon = int(dir_name.split("XP_")[1][3:7])
            except (ValueError, IndexError):
                lat = None
            # Enlarged directory-name acceptance can yield more than one dir
            # for the same (lat, lon); keep the first encountered, like the
            # legacy.
            if lat is not None and (lat, lon) not in seen:
                build_dir = os.path.join(working_dir, dir_name)
                if os.path.isdir(build_dir):
                    built = _build_tile_info(build_dir, lat, lon, dir_name)
                    if built is not None:
                        seen.add((lat, lon))
                        key, info = (lat, lon), built
        yield done, total, key, info


def _scan_per_tile(working_dir: str) -> dict[tuple[int, int], TileInfo]:
    """Scan a parent directory of per-tile ``*XP_*`` build directories."""
    tiles: dict[tuple[int, int], TileInfo] = {}
    for _done, _total, key, info in iter_scan_tiles(working_dir):
        if key is not None:
            tiles[key] = info
    return tiles


def _scan_grouped(working_dir: str) -> dict[tuple[int, int], TileInfo]:
    """Scan a single grouped build directory holding several tiles."""
    tiles: dict[tuple[int, int], TileInfo] = {}
    end_dir = os.path.join(working_dir, "Earth nav data")
    if not os.path.isdir(end_dir):
        return tiles
    dir_name = os.path.basename(os.path.normpath(working_dir))
    for sub in os.listdir(end_dir):
        sub_path = os.path.join(end_dir, sub)
        if not os.path.isdir(sub_path):
            continue
        for file_name in os.listdir(sub_path):
            if not file_name.endswith(".dsf"):
                continue
            try:
                lat = int(file_name[0:3])
                lon = int(file_name[3:7])
            except ValueError:
                continue
            if (lat, lon) in tiles:
                continue
            info = _build_tile_info(working_dir, lat, lon, dir_name)
            if info is not None:
                tiles[(lat, lon)] = info
    return tiles


##############################################################################
# Public API
##############################################################################
def scan_tiles(
    working_dir: str, grouped: bool = False
) -> dict[tuple[int, int], TileInfo]:
    """Scan ``working_dir`` for built tiles, keyed by ``(lat, lon)``.

    When ``grouped`` is ``False`` (the default) ``working_dir`` is treated
    as the parent of many per-tile ``*XP_<short_latlon>`` build directories.
    When ``grouped`` is ``True`` ``working_dir`` is itself a single build
    directory whose ``Earth nav data`` tree holds several tiles' ``.dsf``
    files.
    """
    if grouped:
        return _scan_grouped(working_dir)
    return _scan_per_tile(working_dir)


def tile_info(
    lat: int, lon: int, working_dir: str, grouped: bool = False
) -> Optional[TileInfo]:
    """Return the :class:`TileInfo` for a single tile, or ``None``.

    In grouped mode ``working_dir`` is the build directory itself.  In
    per-tile mode the expected build directory is derived from
    :func:`O4_File_Names.build_dir` (default ``zOrtho4XP_<short_latlon>``
    naming); if that exact directory is absent the whole ``working_dir`` is
    scanned for any ``*XP_*`` directory matching ``(lat, lon)``.
    """
    if grouped:
        dir_name = os.path.basename(os.path.normpath(working_dir))
        return _build_tile_info(working_dir, lat, lon, dir_name)

    # Per-tile: FNAMES.build_dir treats a trailing-slash custom dir as the
    # parent of per-tile dirs and returns "<working_dir>/zOrtho4XP_<short>".
    custom = working_dir
    if not custom.endswith(("/", "\\")):
        custom = custom + "/"
    expected = FNAMES.build_dir(lat, lon, custom)
    if os.path.isdir(expected):
        info = _build_tile_info(
            expected, lat, lon, os.path.basename(os.path.normpath(expected))
        )
        if info is not None:
            return info

    # Fall back to a full scan (handles non-default directory names).
    return scan_tiles(working_dir, grouped=False).get((lat, lon))


def compute_size(info: TileInfo) -> int:
    """Sum the byte size of every file under ``info.build_dir``.

    Symlinks are not followed.  The result is stored on ``info.size_bytes``
    and also returned.
    """
    total = 0
    for root, _dirs, files in os.walk(info.build_dir, followlinks=False):
        for name in files:
            path = os.path.join(root, name)
            try:
                total += os.stat(path, follow_symlinks=False).st_size
            except OSError:
                pass
    info.size_bytes = total
    return total
