"""Resolve and parse X-Plane AGP (AutoGen Point) building footprints.

Unlike ``.fac`` facades — which X-Plane places in the DSF as full
draped POLYGONs whose ring geometry the ``dsf_reader`` walks directly —
an ``.agp`` building is placed as a single ``OBJECT`` point plus a
heading (the "handle").  The footprint is NOT in the DSF at all; it is
encoded inside the ``.agp`` sidecar file, which we must locate (via the
X-Plane ``library.txt`` virtual→physical map) and parse ourselves.

This module owns that work, kept independent of the DSF reader so it is
unit-testable in isolation:

  1. ``get_library_index(xplane_root)`` — build (once, memoized) the
     ``library.txt`` ``EXPORT`` table mapping a virtual library path
     (e.g. ``lib/airport/Common_Elements/Hangars/Lg_Maint_Gray.agp``)
     to the absolute path of the physical resource on disk.
  2. ``parse_agp(path)`` — read (once, memoized) an ``.agp`` file and
     return its footprint as a polygon in LOCAL METERS relative to the
     placement anchor, before any placement heading is applied.
  3. ``agp_footprint_lonlat(...)`` — given a placement (virtual path,
     handle lon/lat, heading) project the local footprint to a
     lon/lat ring ready to hand to the building pipeline.

Footprint geometry (per design decision 2026-06-17): the encoded
``CROP_POLY`` (the draped ground polygon), falling back to the ``TILE``
rectangle, scaled from texture pixels to meters via
``TEXTURE_WIDTH/HEIGHT ÷ TEXTURE_SCALE`` and re-centred on ``ANCHOR_PT``
(the pixel that lands on the DSF handle; defaults to the tile centre
when absent).
"""
from __future__ import annotations

import math
import os
from collections import namedtuple


# Parsed footprint of one .agp: ``local_poly`` is a list of (x, y) in
# meters relative to the anchor (x = texture S / east, y = texture T /
# north) BEFORE the placement heading; ``rotation`` is the tile's own
# internal ROTATION (degrees, almost always 0) which combines with the
# placement heading at projection time.
AgpFootprint = namedtuple("AgpFootprint", ["local_poly", "rotation"])


# Memoized library index, keyed by absolute xplane_root.  Built once per
# process and shared by every airport / caller — scanning the thousands
# of library.txt EXPORT entries is the one genuinely expensive disk step
# and must not repeat per-airport.
_LIB_INDEX_CACHE: dict[str, dict[str, str]] = {}

# Memoized .agp parse results, keyed by absolute physical path.  An .agp
# referenced by many placements / many airports is read and parsed from
# disk exactly once; footprint geometry is immutable so no invalidation.
_AGP_CACHE: dict[str, AgpFootprint | None] = {}


# ── library.txt resolution ───────────────────────────────────────────
def _scenery_pack_order(xplane_root: str) -> list[str]:
    """Custom Scenery pack directory names in LOW→HIGH library priority.

    X-Plane resolves library paths with packs listed FIRST in
    ``Custom Scenery/scenery_packs.ini`` taking priority.  We return the
    packs lowest-priority-first so a caller can overwrite into a dict and
    let the highest-priority provider win.  Packs absent from the ini (or
    when the ini is missing) are appended in sorted order at the lowest
    priority for determinism.
    """
    custom = os.path.join(xplane_root, "Custom Scenery")
    if not os.path.isdir(custom):
        return []
    on_disk = {d for d in os.listdir(custom)
               if os.path.isdir(os.path.join(custom, d))}
    ordered_high_to_low: list[str] = []
    ini = os.path.join(custom, "scenery_packs.ini")
    if os.path.isfile(ini):
        try:
            with open(ini, "r", encoding="utf-8",
                      errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("SCENERY_PACK"):
                        continue
                    rest = line.split(None, 1)[1] if " " in line else ""
                    rest = rest.strip().rstrip("/")
                    # "Custom Scenery/<pack>" → "<pack>"
                    name = os.path.basename(rest)
                    if name in on_disk and name not in ordered_high_to_low:
                        ordered_high_to_low.append(name)
        except OSError:
            pass
    # Append any on-disk packs the ini didn't mention (sorted, lowest).
    for name in sorted(on_disk):
        if name not in ordered_high_to_low:
            ordered_high_to_low.append(name)
    # Reverse → low priority first (so dict-overwrite leaves high last).
    return list(reversed(ordered_high_to_low))


def _parse_library_txt(lib_path: str, index: dict[str, str]) -> None:
    """Merge the ``EXPORT*`` directives of one ``library.txt`` into
    ``index`` (virtual-path → absolute physical-path).  Later calls
    overwrite earlier ones, so callers must visit lower-priority
    libraries first."""
    lib_dir = os.path.dirname(lib_path)
    try:
        with open(lib_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        directive = tok[0]
        # EXPORT / EXPORT_EXTEND / EXPORT_BACKUP: <virtual> <physical...>
        # EXPORT_RATIO: <ratio> <virtual> <physical...>  (skip the ratio)
        if directive in ("EXPORT", "EXPORT_EXTEND", "EXPORT_BACKUP"):
            args = tok[1:]
        elif directive == "EXPORT_RATIO":
            args = tok[2:]
        else:
            continue
        if len(args) < 2:
            continue
        virtual = args[0]
        # The physical path is the remainder; it may contain spaces, so
        # re-split the original line off the virtual token rather than
        # trusting whitespace tokenization beyond it.
        physical = line.split(virtual, 1)[1].strip()
        if not physical:
            continue
        phys_abs = os.path.normpath(
            os.path.join(lib_dir, physical.replace("\\", "/")))
        index[virtual] = phys_abs
        # Case-insensitive fallback key (some packs differ only in case
        # from the DSF reference); the exact key above wins on lookup.
        index.setdefault(virtual.lower(), phys_abs)


def get_library_index(xplane_root: str) -> dict[str, str]:
    """Return the memoized virtual→physical ``library.txt`` map for an
    X-Plane install.  Built once per ``xplane_root`` and shared across
    all callers in the process."""
    key = os.path.abspath(xplane_root)
    cached = _LIB_INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    index: dict[str, str] = {}
    # Default scenery first (lowest priority — custom packs override it).
    default_root = os.path.join(xplane_root, "Resources", "default scenery")
    default_libs: list[str] = []
    if os.path.isdir(default_root):
        for entry in sorted(os.listdir(default_root)):
            cand = os.path.join(default_root, entry, "library.txt")
            if os.path.isfile(cand):
                default_libs.append(cand)
    for lib in default_libs:
        _parse_library_txt(lib, index)
    # Custom packs, lowest priority first so highest wins on overwrite.
    custom_root = os.path.join(xplane_root, "Custom Scenery")
    for pack in _scenery_pack_order(xplane_root):
        cand = os.path.join(custom_root, pack, "library.txt")
        if os.path.isfile(cand):
            _parse_library_txt(cand, index)

    _LIB_INDEX_CACHE[key] = index
    return index


def resolve_library_path(virtual_path: str,
                         xplane_root: str) -> str | None:
    """Resolve a virtual library path to an absolute physical file, or
    None if no ``EXPORT`` provides it / the file is missing."""
    index = get_library_index(xplane_root)
    phys = index.get(virtual_path) or index.get(virtual_path.lower())
    if phys and os.path.isfile(phys):
        return phys
    return None


# ── .agp parsing ─────────────────────────────────────────────────────
def _crop_or_tile_pixels(tile, crop):
    """Return the footprint vertices in texture pixels: the CROP_POLY
    pairs when present, else the four TILE corners.  ``tile`` is
    (left, bottom, right, top); ``crop`` is a flat list of pixel
    coords."""
    if crop and len(crop) >= 6:
        pts = [(crop[i], crop[i + 1])
               for i in range(0, len(crop) - 1, 2)]
        if len(pts) >= 3:
            return pts
    if tile is not None:
        left, bottom, right, top = tile
        return [(left, bottom), (right, bottom),
                (right, top), (left, top)]
    return None


def parse_agp(path: str) -> AgpFootprint | None:
    """Parse an ``.agp`` file into a local-meter footprint polygon
    (memoized by absolute path).  Returns None when the file is missing
    or lacks the fields needed to derive a metric footprint."""
    key = os.path.abspath(path)
    if key in _AGP_CACHE:
        return _AGP_CACHE[key]

    result: AgpFootprint | None = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        _AGP_CACHE[key] = None
        return None

    scale_s = scale_t = None
    tex_w = tex_h = None
    tile = None
    crop = None
    anchor = None
    rotation = 0.0
    # Use the FIRST TILE / CROP_POLY block (an .agp may list several tile
    # variants chosen randomly by the sim; for hangars they share one
    # footprint extent, and the first is representative).
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        tok = line.split()
        kw = tok[0]
        try:
            if kw == "TEXTURE_SCALE" and len(tok) >= 3:
                scale_s, scale_t = float(tok[1]), float(tok[2])
            elif kw == "TEXTURE_WIDTH" and len(tok) >= 2:
                tex_w = float(tok[1])
            elif kw == "TEXTURE_HEIGHT" and len(tok) >= 2:
                tex_h = float(tok[1])
            elif kw == "TILE" and tile is None and len(tok) >= 5:
                tile = (float(tok[1]), float(tok[2]),
                        float(tok[3]), float(tok[4]))
            elif kw == "CROP_POLY" and crop is None and len(tok) >= 7:
                crop = [float(v) for v in tok[1:]]
            elif kw == "ANCHOR_PT" and anchor is None and len(tok) >= 3:
                anchor = (float(tok[1]), float(tok[2]))
            elif kw == "ROTATION" and len(tok) >= 2:
                rotation = float(tok[1])
        except (ValueError, IndexError):
            continue

    # Need a pixel→meter scale and a footprint outline to proceed.
    if not scale_s or tex_w is None:
        _AGP_CACHE[key] = None
        return None
    if tex_h is None:           # assume square pixels when height absent
        tex_h = tex_w
        scale_t = scale_t or scale_s
    if not scale_t:
        scale_t = scale_s
    mpp_x = tex_w / scale_s
    mpp_y = tex_h / scale_t

    pixels = _crop_or_tile_pixels(tile, crop)
    if not pixels:
        _AGP_CACHE[key] = None
        return None
    if anchor is None:
        if tile is not None:
            left, bottom, right, top = tile
            anchor = ((left + right) / 2.0, (bottom + top) / 2.0)
        else:
            xs = [p[0] for p in pixels]
            ys = [p[1] for p in pixels]
            anchor = ((min(xs) + max(xs)) / 2.0,
                      (min(ys) + max(ys)) / 2.0)

    ax, ay = anchor
    local = [((px - ax) * mpp_x, (py - ay) * mpp_y) for px, py in pixels]
    result = AgpFootprint(local_poly=local, rotation=rotation)
    _AGP_CACHE[key] = result
    return result


# ── placement → lon/lat ──────────────────────────────────────────────
_LAT_M_PER_DEG = 111320.0


def _rotate_clockwise(x_east: float, y_north: float,
                      heading_deg: float) -> tuple[float, float]:
    """Rotate a local (east, north) point by an X-Plane heading
    (degrees clockwise from true north)."""
    if heading_deg % 360.0 == 0.0:
        return x_east, y_north
    th = math.radians(heading_deg)
    s, c = math.sin(th), math.cos(th)
    east = x_east * c + y_north * s
    north = -x_east * s + y_north * c
    return east, north


def agp_footprint_lonlat(virtual_path: str,
                         lon: float, lat: float, heading_deg: float,
                         xplane_root: str) -> list[tuple[float, float]] | None:
    """Resolve + parse the ``.agp`` for a placement and return its
    footprint as an (unclosed) list of (lon, lat).  None when the
    resource can't be resolved or parsed."""
    phys = resolve_library_path(virtual_path, xplane_root)
    if phys is None:
        return None
    fp = parse_agp(phys)
    if fp is None or len(fp.local_poly) < 3:
        return None
    # The tile's own ROTATION combines with the DSF placement heading.
    total_heading = heading_deg + fp.rotation
    cos_lat = math.cos(math.radians(lat))
    if abs(cos_lat) < 1e-9:
        return None
    ring: list[tuple[float, float]] = []
    for x_east, y_north in fp.local_poly:
        east, north = _rotate_clockwise(x_east, y_north, total_heading)
        plon = lon + east / (_LAT_M_PER_DEG * cos_lat)
        plat = lat + north / _LAT_M_PER_DEG
        ring.append((plon, plat))
    return ring


def is_agp_building_def(path: str) -> bool:
    """True when an ``OBJECT_DEF`` path is a hangar ``.agp`` we admit as
    a building footprint — restricted (per the initial scope) to the
    ``lib/airport/Common_Elements/Hangars/`` virtual prefix."""
    p = path.lower()
    return (p.endswith(".agp")
            and p.startswith("lib/airport/common_elements/hangars/"))
