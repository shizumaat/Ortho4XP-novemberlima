"""Read draped pavement polygons from a binary X-Plane DSF file.

DSF (Distribution Scenery File) is X-Plane's binary scenery format.
Beyond apt.dat row-110 polygons, scenery packs commonly add airport
pavement as DRAPED POLYGONS in the adjacent DSF — entries that
reference a ``.pol`` definition (typical paths under
``lib/airport/pavement/`` or ``lib/airport/ground/pavement/``).

We don't bother reimplementing DSF binary parsing; X-Plane ships
``DSFTool`` which converts DSF→text losslessly.  Ortho4XP bundles
``DSFTool`` for all three platforms under ``Utils/{lin,mac,win}/``.
This module:

  1. Locates the platform's ``DSFTool`` binary.
  2. Runs ``DSFTool --dsf2text`` on the requested DSF (caching the
     text output so repeated reads are free).
  3. Parses the text for ``POLYGON_DEF`` blocks and ``BEGIN_POLYGON``
     / ``POLYGON_POINT`` instances; filters to entries whose
     ``POLYGON_DEF`` path looks like pavement.
  4. Returns each pavement polygon as a list of (lon, lat) coords.

Returns are in lat/lon (EPSG:4326).  The caller is responsible for
projecting to its local meter coordinate system.
"""
from __future__ import annotations

import math
import os
import platform
import subprocess
import sys
import tempfile

import O4_File_Names as FNAMES
import O4_UI_Utils as UI

# Reuse the X-Plane bezier convention + flattening from the apt.dat
# reader so DSF curves are sampled IDENTICALLY to apt.dat curves.
# When the same WED-authored curve is exported to both apt.dat and the
# DSF, sampling both with the same control-point math + segment count
# makes their shared boundaries land on the same vertices — so the
# union dissolves cleanly instead of leaving lens-shaped residue.
from .apt_dat_reader import (
    BEZIER_FLATTEN_DEV_DEG,
    DEFAULT_BEZIER_SEGMENTS,
    _cubic_bezier,
    _mirror,
    _quadratic_bezier,
)
from . import agp_reader as _AGPR
from .config import AGP_BUILDINGS


# Pavement-detector patterns: a POLYGON_DEF must START with one of
# these prefixes to be admitted as pavement geometry.  These are
# the X-Plane STOCK pavement library paths — bulk taxiway / apron /
# runway base pavement.  Third-party libraries (zannespol/, custom
# .pol files in scenery packs, etc.) are NOT trusted by this filter
# because their .pol entries are usually visual OVERLAYS painted on
# top of base pavement (e.g. ``LAYER_GROUP taxiways +1``,
# ``LAYER_GROUP runways +3``) — emitting them as pavement footprint
# duplicates apt.dat row-110 coverage and pulls non-pavement
# (grass-tinted, grunge, decorative) areas into the layout.
#
# Confirmed at SPJC where ``zannespol/Asphalt_2_Green_T80.pol`` has
# ``LAYER_GROUP taxiways +1`` and contributes 1.45 M m² of "pavement"
# that's actually a green-tinted overlay on the apron — driving the
# overlap + non-pavement coverage regression the user observed in
# JOSM.  Restricting to stock paths drops 1.86 M m² of decorative
# overlay; CYXY (which legitimately uses DSF as its sole pavement
# source) survives because every CYXY DSF pavement def is under
# ``lib/airport/pavement/`` or ``lib/airport/ground/pavement/``.
_PAVEMENT_PREFIXES = (
    "lib/airport/pavement/",
    "lib/airport/ground/pavement/",
)
# Skip patterns inside the accepted prefixes — line markings,
# direction signs, etc. that share path namespace with bulk
# pavement.  NOTE: "shoulder" is intentionally NOT skipped — runway
# / taxiway shoulders are real paved surface the patch should keep
# (user 2026-05-21); only paint / signage / decals are excluded.
_PAVEMENT_SKIP = (
    "/lines/",
    "/markings/",
    "/lights/",
    "/decals/",
    "DirSigns",
)
# Second tier (user 2026-06-10, KPHX south apron): a third-party
# ``.pol`` IS sometimes the BASE pavement, not an overlay —
# ``ZDP_Library/ground_textures/concrete/flat/Flat_New_Uniform.pol``
# carries KPHX's south aprons with NO apt.dat row-110 beneath them.
# Admit third-party ``.pol`` defs by MATERIAL DESCRIPTOR in the path
# (the common library naming convention; token list in config —
# "asphalt"/"concrete" + FR/DE/ES/IT/PT equivalents per user), with
# nothing decorative in the path; the pipeline's geometric overlay
# gate (a polygon ≥ 80 % inside the apt.dat union is dropped)
# additionally keeps overlays painted ON apt.dat pavement out of the
# layout.
from .config import DSF_PAVEMENT_MATERIAL_TOKENS
_THIRD_PARTY_SKIP_TOKENS = _PAVEMENT_SKIP + (
    "grass", "terrain", "dirt", "gravel", "soil", "mud", "snow",
    "paint", "line", "marking", "light", "decal", "sign", "logo",
    "grunge", "stain", "skid", "crack_line",
)


def is_stock_pavement_def(path: str) -> bool:
    """True when the POLYGON_DEF path is X-Plane STOCK pavement
    (``lib/airport/pavement/…``).  Third-party admissions (tier 2 in
    ``_is_pavement_def``) return False — the pipeline counts them as
    pavement COVERAGE but excludes them from apron-merge semantics
    (a full-airport base-texture ``.pol`` under the runways must not
    read as "an apron enclosing the runway")."""
    p = path.lower()
    return any(p.startswith(prefix) for prefix in _PAVEMENT_PREFIXES)


def _dsftool_path() -> str | None:
    """Return the platform's bundled DSFTool binary, or None."""
    # Mirror the layout O4_Mesh_Utils uses for Triangle4XP.
    base = FNAMES.Utils_dir
    sysname = platform.system().lower()
    if sysname == "darwin":
        cand = os.path.join(base, "mac", "DSFTool")
    elif sysname == "windows":
        cand = os.path.join(base, "win", "DSFTool.exe")
    else:
        cand = os.path.join(base, "lin", "DSFTool")
    if os.path.isfile(cand):
        return cand
    return None


def _is_pavement_def(path: str) -> bool:
    """True if the POLYGON_DEF path is bulk pavement geometry.

    Tier 1 — X-Plane stock pavement library paths, always admitted.

    Tier 2 — third-party ``.pol`` defs (``ZDP_Library/``,
    ``zannespol/``, pack-local files, …) whose path names a pavement
    MATERIAL (concrete/asphalt/…) and nothing decorative.  These are
    often layered visual overlays painted ON apt.dat pavement — but
    sometimes they ARE the base pavement (KPHX south aprons ship
    solely as ``ZDP_Library/.../concrete/flat/Flat_New_Uniform.pol``
    with no row-110 beneath).  Admit them here; the pipeline's
    geometric overlay gate drops any polygon ≥ 80 % inside the
    apt.dat union, so true overlays (SPJC zannespol tinted asphalt)
    still never reach the layout.
    """
    p = path.lower()
    if any(p.startswith(prefix) for prefix in _PAVEMENT_PREFIXES):
        return not any(s in p for s in _PAVEMENT_SKIP)
    if (p.endswith(".pol")
            and any(t in p for t in DSF_PAVEMENT_MATERIAL_TOKENS)):
        return not any(s.lower() in p for s in _THIRD_PARTY_SKIP_TOKENS)
    return False


# ── SURFACE-attribute classification (user 2026-07-05) ───────────────
# The NAME heuristics above miss real pavement whose resource path
# carries no material token — but the ``.pol`` resource ITSELF declares
# what it is: a draped polygon with ``SURFACE asphalt`` / ``SURFACE
# concrete`` is hard pavement to X-Plane's own physics, and one with
# ``SURFACE grass|dirt|gravel|…`` is ground texture no matter how its
# path reads.  So the classifier now resolves the POLYGON_DEF resource
# (pack-relative file, else the ``library.txt`` virtual→physical map)
# and reads its SURFACE attribute:
#
#   * SURFACE asphalt/concrete  → pavement (regardless of the name);
#   * SURFACE anything-else    → NOT pavement (declared soft — vetoes
#     even a material-token name like ``.../concrete_edge_grass.pol``);
#   * no SURFACE / unresolvable → fall back to the name heuristics.
#
# The pipeline's geometric overlay gate (≥ 80 % inside the apt.dat
# union → dropped) still applies afterwards, so a tinted overlay ON
# apt.dat pavement that declares SURFACE asphalt (they often do) never
# duplicates the layout.  Gate: O4_DSF_SURFACE_POLYGONS (default on).
_HARD_SURFACE_VALUES = frozenset({"asphalt", "concrete"})

# Decorative namespaces/tokens veto BEFORE the SURFACE attribute is
# consulted: painted overlays (runway signs, safety-area stripes,
# taxi lines) routinely declare ``SURFACE asphalt|concrete`` because
# their authors match the pavement they sit on — admitting them mints
# 4 m² "pavement" sign patches on grass shoulders (KCLT ships 56
# DrapedRwySigns placements with SURFACE concrete).  These are the
# decorative tokens of the name filter WITHOUT the terrain words —
# a terrain-worded path with a declared hard surface is trusted.
_DECORATIVE_SKIP_TOKENS = _PAVEMENT_SKIP + (
    "sign", "line", "marking", "decal", "paint", "logo",
    "grunge", "stain", "skid", "crack_line",
)

# Memoized per (def_path, pack_root, xplane_root): parsing the same
# ``.pol`` once per process, not once per POLYGON_DEF reference.
_surface_attribute_cache: dict[tuple[str, str, str], str | None] = {}


def _resource_surface_attribute(def_path: str,
                                pack_root: str | None,
                                xplane_root: str | None) -> str | None:
    """The ``SURFACE`` value declared by a draped-polygon ``.pol``
    resource, lower-cased — or ``None`` when the resource is not a
    ``.pol``, cannot be resolved to a file, or declares no SURFACE.

    Resolution order mirrors X-Plane: a pack-relative file wins, else
    the ``library.txt`` virtual→physical map (``agp_reader``'s memoized
    index)."""
    key = (def_path, pack_root or "", xplane_root or "")
    if key in _surface_attribute_cache:
        return _surface_attribute_cache[key]
    value: str | None = None
    if def_path.lower().endswith(".pol"):
        physical = None
        if pack_root:
            candidate = os.path.join(pack_root, def_path)
            if os.path.isfile(candidate):
                physical = candidate
        if physical is None and xplane_root:
            try:
                from .agp_reader import resolve_library_path
                physical = resolve_library_path(def_path, xplane_root)
            except (OSError, ValueError):
                physical = None
        if physical is not None and os.path.isfile(physical):
            try:
                with open(physical, "r", errors="ignore") as handle:
                    for line in handle:
                        tokens = line.split()
                        if tokens and tokens[0].upper() == "SURFACE" \
                                and len(tokens) > 1:
                            value = tokens[1].lower()
                            break
            except OSError:
                value = None
    _surface_attribute_cache[key] = value
    return value


def _classify_pavement_def(def_path: str,
                           pack_root: str | None = None,
                           xplane_root: str | None = None) -> bool:
    """SURFACE-attribute-first pavement classification (falls back to
    the ``_is_pavement_def`` name heuristics — see the section comment
    above).  Decorative-namespace defs are vetoed before SURFACE is
    consulted (painted overlays declare the surface they sit ON)."""
    from .config import DSF_SURFACE_POLYGONS
    if DSF_SURFACE_POLYGONS:
        p = def_path.lower()
        if any(t in p for t in _DECORATIVE_SKIP_TOKENS):
            return False
        surface = _resource_surface_attribute(
            def_path, pack_root, xplane_root)
        if surface is not None:
            return surface in _HARD_SURFACE_VALUES
    return _is_pavement_def(def_path)


def _pack_root_for_dsf(dsf_path: str) -> str | None:
    """Scenery-pack directory a DSF belongs to
    (``<pack>/Earth nav data/<subdir>/<tile>.dsf`` → ``<pack>``)."""
    try:
        pack = os.path.dirname(os.path.dirname(os.path.dirname(dsf_path)))
        return pack if os.path.isdir(pack) else None
    except (OSError, ValueError):
        return None


def _interpolate_dsf_ring(
    nodes: list[tuple[tuple[float, float], tuple[float, float] | None]],
    bezier_segments: int,
) -> list[tuple[float, float]]:
    """Flatten a DSF polygon winding into (lon, lat) vertices,
    sampling bezier curves the SAME way the apt.dat reader does.

    ``nodes`` is the closed ring as ``[(anchor_xy, ctrl_xy_or_None),
    ...]`` (not repeating the first vertex).  Each node's control
    point is its bezier handle (absolute coords); ``None`` means a
    plain corner.  Convention matches ``apt_dat_reader
    ._interpolate_contour``: for A→B, cubic with ``ctrl_a`` and
    ``mirror(ctrl_b, B)`` when both have handles, quadratic when one
    does, straight otherwise; sub-``BEZIER_FLATTEN_DEV_DEG`` curves
    collapse to a straight edge.

    SPLIT bezier handles: WED supports SPLIT handles (independent in/out
    length & direction), which the DSF encodes as a RUN of same-anchor
    points — the point BEFORE the zero-length break carries the INCOMING
    handle, the point AFTER carries the OUTGOING handle (either may be a
    plain corner-marker, ``ctrl == anchor``).  We do NOT merge the run:
    the per-segment convention below (C1 = ``a_ctrl`` used directly, C2 =
    ``mirror(b_ctrl)``) already routes each duplicate's handle to the
    correct side — the incoming segment mirrors the leading point's
    handle as its end control, the outgoing segment uses the trailing
    point's handle directly as its start control.  The zero-length span
    between the duplicates is skipped so it cannot form a self-intersecting
    spike.  Empirically this makes every HECA bezier ring valid (vs the
    old merge-into-one-mirrored-handle approximation, which left ~7 rings
    self-intersecting and bowed split-handle tips the wrong way — the
    source of the phantom notch near HECA 30.11735/31.41601).  The only
    remaining invalid rings are PLAIN (depth-2) polygons that are
    self-intersecting in the authored DSF itself (repaired downstream).
    """
    n = len(nodes)
    if n < 2:
        return [a for a, _ in nodes]
    out: list[tuple[float, float]] = []
    for i in range(n):
        a_xy, a_ctrl = nodes[i]
        b_xy, b_ctrl = nodes[(i + 1) % n]
        if not out or out[-1] != a_xy:
            out.append(a_xy)
        # Zero-length span between split-handle duplicates: no curve to
        # draw (the duplicates' handles serve the adjacent real segments).
        if a_xy == b_xy:
            continue
        if a_ctrl is None and b_ctrl is None:
            continue
        if a_ctrl is not None and b_ctrl is None:
            ctrl_eff = a_ctrl
        elif a_ctrl is None and b_ctrl is not None:
            ctrl_eff = _mirror(b_ctrl, b_xy)
        else:
            mirrored = _mirror(b_ctrl, b_xy)
            mid = (0.5 * (a_xy[0] + b_xy[0]), 0.5 * (a_xy[1] + b_xy[1]))
            d1 = math.hypot(a_ctrl[0] - mid[0], a_ctrl[1] - mid[1])
            d2 = math.hypot(mirrored[0] - mid[0], mirrored[1] - mid[1])
            if 0.5 * max(d1, d2) < BEZIER_FLATTEN_DEV_DEG:
                continue
            for pt in _cubic_bezier(a_xy, a_ctrl, mirrored, b_xy,
                                    bezier_segments)[1:-1]:
                if not out or out[-1] != pt:
                    out.append(pt)
            continue
        mid = (0.5 * (a_xy[0] + b_xy[0]), 0.5 * (a_xy[1] + b_xy[1]))
        if 0.5 * math.hypot(ctrl_eff[0] - mid[0],
                            ctrl_eff[1] - mid[1]) < BEZIER_FLATTEN_DEV_DEG:
            continue
        for pt in _quadratic_bezier(a_xy, ctrl_eff, b_xy,
                                    bezier_segments)[1:-1]:
            if not out or out[-1] != pt:
                out.append(pt)
    return out


# Memoized DSFTool text dumps, keyed by (abspath, mtime).  Both
# ``read_dsf_pavements`` and ``read_dsf_buildings`` — and the ``.agp``
# OBJECT walk — run on the SAME DSF; this keeps the conversion AND the
# (potentially tens-of-MB) ``readlines`` to ONCE per DSF per process
# instead of once per caller.  Keyed on mtime so a rebuilt DSF
# re-converts and re-reads.
_DSF_LINES_CACHE: dict[tuple[str, float], list[str]] = {}


def ensure_dsf_text_path(dsf_path: str,
                         cache_dir: str | None = None) -> str | None:
    """Return the path to the DSFTool ``--dsf2text`` output for a DSF,
    running the conversion only when the cached ``<dsf>.text`` is
    missing or stale.

    Shared helper (extracted so ``O4_Default_Terrain_Map`` can stream
    the same cached text dump line by line without loading it fully into
    memory the way ``_load_dsf_text`` does).  The conversion is the
    expensive step — DSFTool transparently decompresses 7z DSFs — and it
    is keyed on the ``<dsf>.text`` mtime versus the source DSF mtime, so
    a rebuilt DSF re-converts.  Returns None on any failure (missing
    file/tool, conversion error) and prints the same warnings
    ``_load_dsf_text`` historically printed.
    """
    if not dsf_path or not os.path.isfile(dsf_path):
        return None
    try:
        mtime = os.path.getmtime(dsf_path)
    except OSError:
        return None

    tool = _dsftool_path()
    if tool is None:
        UI.vprint(1,
            "  [dsf-reader] WARN: DSFTool binary not found at "
            f"{os.path.join(FNAMES.Utils_dir, platform.system().lower())}; "
            "DSF data will not be loaded.")
        return None

    # Cache the converted text alongside the DSF (or in cache_dir).
    if cache_dir is None:
        cache_dir = os.path.dirname(dsf_path)
    text_path = os.path.join(
        cache_dir,
        os.path.basename(dsf_path) + ".text",
    )
    # Re-convert if text is missing or older than the DSF.
    needs_convert = (not os.path.isfile(text_path)
                     or (os.path.getmtime(text_path) < mtime))
    if needs_convert:
        try:
            # Some platforms don't allow writing into Custom Scenery;
            # fall back to a temp file in /tmp if the cache write fails.
            try:
                subprocess.run(
                    [tool, "--dsf2text", dsf_path, text_path],
                    check=True, capture_output=True, timeout=120,
                )
            except (PermissionError, subprocess.CalledProcessError):
                fallback = tempfile.NamedTemporaryFile(
                    suffix=".dsf.text", delete=False)
                text_path = fallback.name
                fallback.close()
                subprocess.run(
                    [tool, "--dsf2text", dsf_path, text_path],
                    check=True, capture_output=True, timeout=120,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            UI.vprint(1,
                f"  [dsf-reader] WARN: DSFTool failed on "
                f"{os.path.basename(dsf_path)}: {exc}")
            return None
    return text_path


def _load_dsf_text(dsf_path: str,
                   cache_dir: str | None = None) -> list[str] | None:
    """Return the DSFTool ``--dsf2text`` lines for a DSF (memoized).

    Runs DSFTool only when the cached ``<dsf>.text`` is missing/stale,
    and reads the text from disk only ONCE per DSF per process (shared
    across every reader that walks the same DSF).  Returns None on any
    failure (missing file/tool, conversion error).
    """
    if not dsf_path or not os.path.isfile(dsf_path):
        return None
    try:
        mtime = os.path.getmtime(dsf_path)
    except OSError:
        return None
    ckey = (os.path.abspath(dsf_path), mtime)
    cached = _DSF_LINES_CACHE.get(ckey)
    if cached is not None:
        return cached

    text_path = ensure_dsf_text_path(dsf_path, cache_dir)
    if text_path is None:
        return None

    try:
        with open(text_path, "r", encoding="utf-8",
                  errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None
    _DSF_LINES_CACHE[ckey] = lines
    return lines


def _read_dsf_polys(
    dsf_path: str,
    accept_fn,
    cache_dir: str | None = None,
    bezier_segments: int = DEFAULT_BEZIER_SEGMENTS,
) -> list[tuple[list[tuple[float, float]],
                list[list[tuple[float, float]]],
                str]]:
    """Extract draped polygons from a DSF file, keeping only those
    whose ``POLYGON_DEF`` path satisfies ``accept_fn(path) -> bool``.

    This is the shared walker behind both ``read_dsf_pavements``
    (``accept_fn = _is_pavement_def``) and ``read_dsf_buildings``
    (``accept_fn`` = "is a terminal/hangar facade").  Pavement and
    building facades are BOTH draped POLYGON placements in the DSF —
    they differ only in which ``POLYGON_DEF`` resource the placement
    references — so the bezier/winding/hole machinery is identical.

    Args:
        dsf_path: path to a binary ``.dsf`` file.
        accept_fn: predicate on the POLYGON_DEF resource path; only
            polygons whose def path passes are returned.
        cache_dir: directory to store the converted text file
            (saves a re-run of DSFTool on subsequent reads).
            Defaults to a per-DSF temp file alongside the source.

    Returns:
        A list of polygons, each as ``(outer_ring, holes, def_path)``
        where ``outer_ring`` is a list of ``(lon, lat)`` tuples,
        ``holes`` is a list of inner rings (each also a list of
        ``(lon, lat)``), and ``def_path`` is the POLYGON_DEF resource
        path that produced the polygon.  Rings are NOT closed (first
        vertex isn't repeated).  Returns ``[]`` on any failure
        (DSFTool missing, DSF unreadable, no accepted defs, etc.).
    """
    lines = _load_dsf_text(dsf_path, cache_dir)
    if not lines:
        return []

    # Pass 1: collect POLYGON_DEFs in order; track which indices the
    # caller accepts (and their resource paths, returned per polygon).
    accepted_def_idx: dict[int, str] = {}
    def_idx = 0
    for line in lines:
        if line.startswith("POLYGON_DEF"):
            tok = line.strip().split(maxsplit=1)
            path = tok[1] if len(tok) > 1 else ""
            if accept_fn(path):
                accepted_def_idx[def_idx] = path.strip()
            def_idx += 1
    if not accepted_def_idx:
        return []

    # Pass 2: walk BEGIN_POLYGON / END_POLYGON / BEGIN_WINDING /
    # END_WINDING / POLYGON_POINT to build per-instance rings.
    # A polygon may have multiple windings: the FIRST is the outer
    # ring, any SUBSEQUENT windings are HOLES.  Holes MUST be kept —
    # ignoring them turns a perforated pavement ring into a solid
    # blob covering the whole airport (HECA's ground/pavement
    # patched.pol / damaged.pol instances have a 3.5 M / 1.25 M m²
    # outer winding but only ~75 k / ~60 k m² of actual pavement once
    # their holes are subtracted).
    # The BEGIN_POLYGON header's 3rd field is the coordinate depth:
    # 2 = plain (lon, lat); 4 = BEZIER (lon, lat, ctrl_lon, ctrl_lat).
    # X-Plane stock pavement (e.g. asphalt/patched.pol) is authored as
    # bezier polygons; reading only the anchor (lon, lat) collapses
    # smooth curves into coarse straight segments ("pentagrams"),
    # which then leave residue against the apt.dat bezier curves on
    # union.  Capture the control points and tessellate.
    polys: list[tuple[list[tuple[float, float]],
                      list[list[tuple[float, float]]],
                      str]] = []
    in_accepted = False
    cur_def_path = ""
    in_winding = False
    cur_depth = 2
    cur_uv_mode = False
    # Each winding node is (anchor_xy, ctrl_xy_or_None).
    current_ring: list[tuple[tuple[float, float],
                             tuple[float, float] | None]] | None = None
    cur_outer: list[tuple[float, float]] | None = None
    cur_holes: list[list[tuple[float, float]]] = []

    def _finish_ring(ring_nodes):
        flat = _interpolate_dsf_ring(ring_nodes, bezier_segments)
        return flat if len(flat) >= 3 else None

    for line in lines:
        if line.startswith("BEGIN_POLYGON"):
            tok = line.split()
            try:
                idx = int(tok[1])
            except (ValueError, IndexError):
                idx = -1
            try:
                cur_depth = int(tok[3])
            except (ValueError, IndexError):
                cur_depth = 2
            # Draped-polygon param 65535 = explicit per-vertex UV mode:
            # depth 4 is (lon, lat, u, v) — planes 3-4 are TEXTURE
            # coords in [0,1], not bezier handles.  UV-mode bezier is
            # depth 8 (lon, lat, ctrl_lon, ctrl_lat, u, v, ctrl_u,
            # ctrl_v), where planes 3-4 ARE the handles again.  Reading
            # UVs as handles turned 4-corner road quads in the stock
            # Global Airports +39-076.dsf into continental-scale bezier
            # rings (lon −97…−54) that wedged the KOQN hole router.
            try:
                cur_uv_mode = int(tok[2]) == 65535
            except (ValueError, IndexError):
                cur_uv_mode = False
            in_accepted = idx in accepted_def_idx
            cur_def_path = accepted_def_idx.get(idx, "")
            in_winding = False
            current_ring = None
            cur_outer = None
            cur_holes = []
            continue
        if line.startswith("END_POLYGON"):
            if in_accepted and cur_outer and len(cur_outer) >= 3:
                polys.append((cur_outer, cur_holes, cur_def_path))
            in_accepted = False
            in_winding = False
            current_ring = None
            cur_outer = None
            cur_holes = []
            continue
        if not in_accepted:
            continue
        if line.startswith("BEGIN_WINDING"):
            in_winding = True
            current_ring = []
            continue
        if line.startswith("END_WINDING"):
            if (in_winding and current_ring
                    and len(current_ring) >= 3):
                flat = _finish_ring(current_ring)
                if flat is not None:
                    if cur_outer is None:
                        cur_outer = flat
                    else:
                        cur_holes.append(flat)
            in_winding = False
            current_ring = None
            continue
        if in_winding and line.startswith("POLYGON_POINT"):
            tok = line.split()
            try:
                lon = float(tok[1])
                lat = float(tok[2])
            except (ValueError, IndexError):
                continue
            ctrl = None
            # Where the bezier control point lives depends on the plane
            # layout:
            #  • UV mode (param 65535): planes are (lon, lat, [ctrl_lon,
            #    ctrl_lat,] u, v[, ctrl_u, ctrl_v]).  A geographic handle
            #    exists only at depth>=8 and sits at tok[3],tok[4]; depth-4
            #    is (lon, lat, u, v) with no handle.
            #  • Plain / param mode: the geographic handle, when present, is
            #    the LAST TWO coordinate planes — tok[3],tok[4] for a depth-4
            #    pavement bezier (lon, lat, ctrl_lon, ctrl_lat) AND
            #    tok[4],tok[5] for a depth-5 FACADE bezier (lon, lat,
            #    wall_param, ctrl_lon, ctrl_lat).  Reading a fixed tok[3],
            #    tok[4] for a depth-5 facade grabbed the wall param as the
            #    control lon (≈3), exploding the ring to continental scale
            #    (HECA's curved term_building_* facades → ~880 km blobs that
            #    the boundary gate then silently dropped).
            if cur_uv_mode:
                ci, cj, has_bez = 3, 4, cur_depth >= 8
            else:
                ci, cj, has_bez = cur_depth - 1, cur_depth, cur_depth >= 4
            if has_bez:
                try:
                    cx = float(tok[ci])
                    cy = float(tok[cj])
                    # A control == anchor means "no handle" (corner).
                    if cx != lon or cy != lat:
                        ctrl = (cx, cy)
                except (ValueError, IndexError):
                    ctrl = None
            current_ring.append(((lon, lat), ctrl))
    return polys


def read_dsf_pavements(
    dsf_path: str,
    cache_dir: str | None = None,
    bezier_segments: int = DEFAULT_BEZIER_SEGMENTS,
    xplane_root: str | None = None,
) -> list[tuple[list[tuple[float, float]],
                list[list[tuple[float, float]]],
                str]]:
    """Extract draped pavement polygons from a DSF file.

    Thin wrapper over ``_read_dsf_polys`` admitting only pavement
    ``POLYGON_DEF`` paths — classified SURFACE-attribute-first when
    ``xplane_root`` is given (``_classify_pavement_def``), by the name
    heuristics alone otherwise (``_is_pavement_def``).  Return shape
    and semantics are unchanged from before the building reader was
    added: ``(outer_ring, holes, def_path)`` per polygon, rings
    unclosed.
    """
    pack_root = _pack_root_for_dsf(dsf_path)

    def _accept(def_path: str) -> bool:
        return _classify_pavement_def(def_path, pack_root, xplane_root)

    return _read_dsf_polys(dsf_path, _accept, cache_dir, bezier_segments)


# ── Pavement BORDER-LINE strips (user 2026-07-16, KBNA) ─────────────
# Some packs draw their pavement as a ``.pol`` polygon PLUS a wide
# draped ``.lin`` "border" strip traced along the polygon outline
# (KBNA: ``Lines/BordaTaxiway_*.lin``, 4-31 m wide).  X-Plane centers
# the line texture on its path, so HALF the strip is rendered pavement
# OUTSIDE the ``.pol`` — real surface a ``.pol``-only union misses
# (the KBNA Donelson taxiway hole: a 10 m junction gap that the 27 m
# concrete border fills in the sim).  The ``.lin`` resource declares
# its drawn width:
#
#     width_m = SCALE_s × (s2 − s1) / TEX_WIDTH
#
# (``S_OFFSET <layer> <s1> <s_mid> <s2>`` gives the texture columns,
# ``TEX_WIDTH`` the texture's total columns, ``SCALE <s> <t>`` the
# real-world meters those columns span).  Only STRIP-class lines are
# pavement candidates — painted markings are ~0.15-1 m wide, so the
# width floor alone separates them; whether a candidate actually
# borders pavement is the CALLER's geometric test (the pipeline checks
# the path runs along the pavement-union boundary).
_LIN_STRIP_MIN_WIDTH_M = 2.0

# Memoized per (def_path, pack_root, xplane_root), like the SURFACE
# attribute cache above.
_lin_strip_width_cache: dict[tuple[str, str, str], float | None] = {}


def _lin_strip_width_m(def_path: str,
                       pack_root: str | None,
                       xplane_root: str | None) -> float | None:
    """Drawn width in meters a ``.lin`` resource declares, or ``None``
    when the resource is not a ``.lin``, cannot be resolved to a file,
    or lacks the SCALE / TEX_WIDTH / S_OFFSET triple."""
    key = (def_path, pack_root or "", xplane_root or "")
    if key in _lin_strip_width_cache:
        return _lin_strip_width_cache[key]
    width: float | None = None
    if def_path.lower().endswith(".lin"):
        physical = None
        if pack_root:
            candidate = os.path.join(pack_root, def_path)
            if os.path.isfile(candidate):
                physical = candidate
        if physical is None and xplane_root:
            try:
                from .agp_reader import resolve_library_path
                physical = resolve_library_path(def_path, xplane_root)
            except (OSError, ValueError):
                physical = None
        if physical is not None and os.path.isfile(physical):
            texture_columns = None
            scale_s = None
            s_low = s_high = None
            try:
                with open(physical, "r", errors="ignore") as handle:
                    for line in handle:
                        tokens = line.split()
                        if not tokens:
                            continue
                        keyword = tokens[0].upper()
                        try:
                            if keyword == "TEX_WIDTH":
                                texture_columns = float(tokens[1])
                            elif keyword == "SCALE":
                                scale_s = float(tokens[1])
                            elif (keyword == "S_OFFSET"
                                    and len(tokens) >= 5):
                                s_low = float(tokens[2])
                                s_high = float(tokens[4])
                        except (ValueError, IndexError):
                            continue
            except OSError:
                texture_columns = None
            if (texture_columns and scale_s is not None
                    and s_low is not None and s_high is not None):
                width = scale_s * (s_high - s_low) / texture_columns
    _lin_strip_width_cache[key] = width
    return width


def _interpolate_dsf_polyline(
    nodes: list[tuple[tuple[float, float], tuple[float, float] | None]],
    bezier_segments: int,
) -> list[tuple[float, float]]:
    """Open-polyline twin of ``_interpolate_dsf_ring``: same per-segment
    bezier convention, but NO wraparound segment from the last node back
    to the first (a line placement's winding is a path, not a ring)."""
    n = len(nodes)
    if n < 2:
        return [a for a, _ in nodes]
    out: list[tuple[float, float]] = []
    for i in range(n - 1):
        a_xy, a_ctrl = nodes[i]
        b_xy, b_ctrl = nodes[i + 1]
        if not out or out[-1] != a_xy:
            out.append(a_xy)
        if a_xy == b_xy:
            continue
        if a_ctrl is None and b_ctrl is None:
            continue
        if a_ctrl is not None and b_ctrl is None:
            ctrl_eff = a_ctrl
        elif a_ctrl is None and b_ctrl is not None:
            ctrl_eff = _mirror(b_ctrl, b_xy)
        else:
            mirrored = _mirror(b_ctrl, b_xy)
            mid = (0.5 * (a_xy[0] + b_xy[0]), 0.5 * (a_xy[1] + b_xy[1]))
            d1 = math.hypot(a_ctrl[0] - mid[0], a_ctrl[1] - mid[1])
            d2 = math.hypot(mirrored[0] - mid[0], mirrored[1] - mid[1])
            if 0.5 * max(d1, d2) < BEZIER_FLATTEN_DEV_DEG:
                continue
            for pt in _cubic_bezier(a_xy, a_ctrl, mirrored, b_xy,
                                    bezier_segments)[1:-1]:
                if not out or out[-1] != pt:
                    out.append(pt)
            continue
        mid = (0.5 * (a_xy[0] + b_xy[0]), 0.5 * (a_xy[1] + b_xy[1]))
        if 0.5 * math.hypot(ctrl_eff[0] - mid[0],
                            ctrl_eff[1] - mid[1]) < BEZIER_FLATTEN_DEV_DEG:
            continue
        for pt in _quadratic_bezier(a_xy, ctrl_eff, b_xy,
                                    bezier_segments)[1:-1]:
            if not out or out[-1] != pt:
                out.append(pt)
    last_xy = nodes[-1][0]
    if not out or out[-1] != last_xy:
        out.append(last_xy)
    return out


def read_dsf_pavement_border_lines(
    dsf_path: str,
    cache_dir: str | None = None,
    bezier_segments: int = DEFAULT_BEZIER_SEGMENTS,
    xplane_root: str | None = None,
) -> list[tuple[list[tuple[float, float]], float, bool, str]]:
    """Extract STRIP-class draped line placements from a DSF file.

    Returns one ``(path_points, width_m, closed, def_path)`` per line
    placement whose ``.lin`` def declares a drawn width ≥
    ``_LIN_STRIP_MIN_WIDTH_M`` — ``path_points`` is the flattened
    ``(lon, lat)`` polyline (bezier-tessellated, NOT closed), ``closed``
    is the placement's closed-ring flag.  Whether the strip is actually
    pavement (it borders the pavement union) is the caller's geometric
    decision; this reader only separates strips from painted markings
    by width.
    """
    lines = _load_dsf_text(dsf_path, cache_dir)
    if not lines:
        return []
    pack_root = _pack_root_for_dsf(dsf_path)

    strip_width_by_def_idx: dict[int, tuple[float, str]] = {}
    def_idx = 0
    for line in lines:
        if line.startswith("POLYGON_DEF"):
            tok = line.strip().split(maxsplit=1)
            path = tok[1].strip() if len(tok) > 1 else ""
            if path.lower().endswith(".lin"):
                width = _lin_strip_width_m(path, pack_root, xplane_root)
                if width is not None and width >= _LIN_STRIP_MIN_WIDTH_M:
                    strip_width_by_def_idx[def_idx] = (width, path)
            def_idx += 1
    if not strip_width_by_def_idx:
        return []

    out: list[tuple[list[tuple[float, float]], float, bool, str]] = []
    in_strip = False
    cur_width = 0.0
    cur_def_path = ""
    cur_closed = False
    cur_depth = 2
    current_path: list[tuple[tuple[float, float],
                             tuple[float, float] | None]] | None = None
    for line in lines:
        if line.startswith("BEGIN_POLYGON"):
            tok = line.split()
            try:
                idx = int(tok[1])
            except (ValueError, IndexError):
                idx = -1
            in_strip = idx in strip_width_by_def_idx
            if in_strip:
                cur_width, cur_def_path = strip_width_by_def_idx[idx]
                # For a ``.lin`` placement the BEGIN_POLYGON param is
                # the closed-ring flag (0 open path / 1 closed loop).
                try:
                    cur_closed = int(tok[2]) == 1
                except (ValueError, IndexError):
                    cur_closed = False
                try:
                    cur_depth = int(tok[3])
                except (ValueError, IndexError):
                    cur_depth = 2
            current_path = None
            continue
        if not in_strip:
            continue
        if line.startswith("BEGIN_WINDING"):
            current_path = []
            continue
        if line.startswith("END_WINDING"):
            if current_path and len(current_path) >= 2:
                flat = _interpolate_dsf_polyline(current_path,
                                                 bezier_segments)
                if len(flat) >= 2:
                    out.append((flat, cur_width, cur_closed,
                                cur_def_path))
            current_path = None
            continue
        if line.startswith("END_POLYGON"):
            in_strip = False
            current_path = None
            continue
        if current_path is not None and line.startswith("POLYGON_POINT"):
            tok = line.split()
            try:
                lon = float(tok[1])
                lat = float(tok[2])
            except (ValueError, IndexError):
                continue
            ctrl = None
            if cur_depth >= 4:
                try:
                    cx = float(tok[cur_depth - 1])
                    cy = float(tok[cur_depth])
                    if cx != lon or cy != lat:
                        ctrl = (cx, cy)
                except (ValueError, IndexError):
                    ctrl = None
            current_path.append(((lon, lat), ctrl))
    return out


# Building-facade detector: X-Plane places airport TERMINAL and HANGAR
# buildings as draped FACADE polygons (``.fac``) in the DSF.  The
# library virtual paths name the building class:
#   terminals → lib/airport/Modern_Airports/Terminal_kit/term_building_*.fac
#   hangars   → lib/airport/Common_Elements/Hangars/*Hangar.fac,
#               lib/airport/hangars/.../*.fac
# We classify by substring on the lowercased def path: "term_building"
# → terminal, "hangar" → hangar.  Restricted to ``.fac`` so a pavement
# ``.pol`` or object ``.obj`` that merely happens to contain "hangar"
# in its name can never be mistaken for a building footprint.
#
# The Terminal_kit also ships term_roof_* decorative pieces that stack
# ON the footprint (no new outline → dropped) and term_bridge_* CONNECTOR
# facades — enclosed skybridges / link spans that physically join two
# ``term_building_*`` facades.  Bridges carry the ``"bridge"`` role so the
# caller can feed them into the building clustering as CONNECTORS (a
# building + bridge + building run unions into ONE flat pad) without
# treating a stray bridge as a standalone building.
def _building_role_for_def(path: str) -> str | None:
    """Return ``"terminal"`` / ``"hangar"`` / ``"bridge"`` if the
    POLYGON_DEF path is a terminal, hangar, or terminal-bridge facade,
    else ``None``."""
    p = path.lower()
    if not p.endswith(".fac"):
        return None
    if "term_bridge" in p:
        return "bridge"
    if "term_building" in p:
        return "terminal"
    if "hangar" in p:
        return "hangar"
    return None


def _read_dsf_object_placements(
        lines: list[str], accept_fn,
) -> list[tuple[str, float, float, float]]:
    """Walk ``OBJECT_DEF`` / ``OBJECT`` placements over an already-loaded
    DSF text dump.

    Returns ``(def_path, lon, lat, heading_deg)`` for each placement
    whose ``OBJECT_DEF`` path satisfies ``accept_fn``.  Mirrors the
    POLYGON_DEF index-table pattern: ``OBJECT_DEF``\\ s are numbered
    0..N in declaration order; an ``OBJECT`` / ``OBJECT_MSL`` /
    ``OBJECT_AGL`` instruction references one by index, followed by
    lon, lat and (for MSL/AGL, after the elevation field) the heading
    in degrees clockwise from true north.
    """
    accepted: dict[int, str] = {}
    idx = 0
    for line in lines:
        if line.startswith("OBJECT_DEF"):
            tok = line.strip().split(maxsplit=1)
            path = tok[1].strip() if len(tok) > 1 else ""
            if accept_fn(path):
                accepted[idx] = path
            idx += 1
    if not accepted:
        return []
    out: list[tuple[str, float, float, float]] = []
    for line in lines:
        if not line.startswith("OBJECT"):
            continue
        tok = line.split()
        kw = tok[0]
        if kw == "OBJECT":
            hi = 4                       # idx lon lat HEADING
        elif kw in ("OBJECT_MSL", "OBJECT_AGL"):
            hi = 5                       # idx lon lat ELEV HEADING
        else:                            # OBJECT_DEF and unknowns
            continue
        try:
            oi = int(tok[1])
            lon = float(tok[2])
            lat = float(tok[3])
            heading = float(tok[hi]) if len(tok) > hi else 0.0
        except (ValueError, IndexError):
            continue
        p = accepted.get(oi)
        if p is not None:
            out.append((p, lon, lat, heading))
    return out


def read_dsf_buildings(
    dsf_path: str,
    cache_dir: str | None = None,
    bezier_segments: int = DEFAULT_BEZIER_SEGMENTS,
    xplane_root: str | None = None,
) -> list[tuple[list[tuple[float, float]],
                list[list[tuple[float, float]]],
                str]]:
    """Extract terminal/hangar building footprints from a DSF file.

    Returns a list of ``(outer_ring, holes, role)`` where ``role`` is
    ``"terminal"``, ``"hangar"``, or ``"bridge"`` (a term_bridge_*
    connector facade; mapped from the facade's POLYGON_DEF path via
    ``_building_role_for_def``), ``outer_ring`` is
    a list of ``(lon, lat)`` tuples and ``holes`` its inner rings.
    Rings are NOT closed.  Returns ``[]`` on any failure.

    A single building is often placed as SEVERAL stacked facade pieces
    sharing one footprint (e.g. ``term_building_Ground`` +
    ``term_building_Levels`` on the same corners); de-duplicating /
    unioning coincident footprints is the caller's responsibility.

    Two sources feed the same list:
      * ``.fac`` facades — full draped POLYGONs whose ring geometry is
        read straight from the DSF (terminal / hangar / bridge roles).
      * ``.agp`` autogen-point hangars — placed as a single ``OBJECT``
        handle + heading; their footprint is resolved from the ``.agp``
        sidecar via ``library.txt`` and projected onto the handle
        (role ``"hangar"``).  This source is gated by ``AGP_BUILDINGS``
        and requires ``xplane_root`` to resolve the library; when the
        gate is off (or no root is supplied) the result is exactly the
        prior ``.fac``-only behaviour.
    """
    polys = _read_dsf_polys(
        dsf_path,
        lambda pth: _building_role_for_def(pth) is not None,
        cache_dir, bezier_segments)
    out: list[tuple[list[tuple[float, float]],
                    list[list[tuple[float, float]]],
                    str]] = []
    for outer, holes, def_path in polys:
        role = _building_role_for_def(def_path)
        if role is not None:
            out.append((outer, holes, role))

    # ── .agp point-placed hangars (just another building source) ────
    if AGP_BUILDINGS and xplane_root:
        lines = _load_dsf_text(dsf_path, cache_dir)   # memoized: no re-read
        if lines:
            for vpath, lon, lat, heading in _read_dsf_object_placements(
                    lines, _AGPR.is_agp_building_def):
                ring = _AGPR.agp_footprint_lonlat(
                    vpath, lon, lat, heading, xplane_root)
                if ring and len(ring) >= 3:
                    out.append((ring, [], "hangar"))
    return out


# ── OBJ8 scenery objects as buildings (Phase 1 of the DSF object
# integration — docs/dsf_object_integration_spec.md section 4-W6) ────
#
# Resolved OBJ8 geometry, keyed on (absolute path, mtime).  Unlike the
# legacy ``_surface_attribute_cache`` / ``_LIB_INDEX_CACHE`` / ``_AGP_CACHE``
# — which have NO invalidation — a replaced ``.obj`` file re-parses here,
# matching the ``_DSF_LINES_CACHE`` idiom above.
_OBJECT_GEOMETRY_CACHE: dict = {}


def _load_object_geometry(physical_path: str):
    """Parse an OBJ8 file through ``obj8_reader.load_object_file``,
    memoized on ``(absolute path, mtime)``.  Returns ``None`` when the
    file is missing or unparsable."""
    from . import obj8_reader as _OBJ8
    try:
        cache_key = (os.path.abspath(physical_path),
                     os.path.getmtime(physical_path))
    except OSError:
        return None
    cached = _OBJECT_GEOMETRY_CACHE.get(cache_key)
    if cached is not None:
        return cached
    try:
        geometry = _OBJ8.load_object_file(physical_path)
    except (OSError, ValueError):
        return None
    _OBJECT_GEOMETRY_CACHE[cache_key] = geometry
    return geometry


def airport_mod_cache_dir(pack_root: str) -> str | None:
    """Directory for Ortho4XP-only sidecar caches of one scenery pack.

    USER RULING (Noah, 2026-07-15): cache files used only by Ortho4XP
    must NOT clutter airport scenery pack folders — they live under the
    Ortho4XP data root at ``Airport_mod_cache/<pack folder name>/``.
    (Backups such as ``.anchor_bak`` explicitly STAY in-pack next to the
    files they back up; this helper is for caches only.)

    The directory is NOT created here — writers ``os.makedirs(...,
    exist_ok=True)`` right before writing, readers just probe with
    ``isfile``.  In a source checkout ``O4_File_Names.data_path`` follows
    the current working directory at call time (load-bearing legacy
    behavior elsewhere — never cache this result at import time); in the
    packaged app it is the user-chosen data root.

    Returns ``None`` when ``pack_root`` is falsy or not a directory."""
    if not pack_root or not os.path.isdir(pack_root):
        return None
    import O4_File_Names as _FNAMES
    pack_name = os.path.basename(os.path.abspath(pack_root))
    return _FNAMES.data_path(os.path.join("Airport_mod_cache", pack_name))


# ── Pack-sidecar footprint cache (mirrors the object-terrain
# classification cache in ``object_terrain_assembly``) ────────────────
#
# Bump when the partition / footprint logic changes shape in a way that
# would make an old cached ring set wrong — invalidates every footprint
# sidecar.
# 2: portal-face exclusions (EGGW); 3: terrain classifier refuses stock
# library (lib/...) resources, changing the terrain-exclusion pass output
_OBJECT_FOOTPRINT_CACHE_VERSION = 3

# Sidecar file name prefix; the full name carries the DSF stem
# (``o4_object_footprints_<dsf-stem>.cache``) so two DSFs of one pack
# never collide.  Lives under ``airport_mod_cache_dir`` — NOT in the
# pack (user ruling 2026-07-15, no Ortho4XP clutter in scenery packs).
_OBJECT_FOOTPRINT_SIDECAR_PREFIX = "o4_object_footprints"

# Pre-ruling in-pack sidecar name, removed on sight (legacy cleanup).
_OBJECT_FOOTPRINT_LEGACY_SIDECAR_NAME = "o4_object_footprints.cache"


def _object_footprint_sidecar(
    dsf_path: str,
    pack_root: str | None,
    contact_epsilon_metres: float,
    minimum_reach_metres: float,
    gate_constants: tuple[float, ...] = (),
    sidecar_prefix: str = _OBJECT_FOOTPRINT_SIDECAR_PREFIX,
    cache_version: int = _OBJECT_FOOTPRINT_CACHE_VERSION,
) -> tuple[str | None, str | None]:
    """Sidecar path + input fingerprint for the pack footprint cache.

    ``sidecar_prefix`` / ``cache_version`` let the object-PAVEMENT reader
    (:func:`read_dsf_object_pavements`) keep its own sidecar file and
    version stream while sharing the fingerprint machinery — its result
    depends on the same inputs (the overlay DSF, every pack-local
    ``.obj``, its gate constants).

    The return value of :func:`read_dsf_object_buildings` is a pure
    function of everything hashed here, so a fingerprint match makes the
    cached ring set exactly reproducible.  The fingerprint (sha1, same
    style as ``object_terrain_assembly._classification_sidecar``) covers:

    * the overlay DSF (basename, size, mtime) — the placement list is
      read from it;
    * every ``.obj`` under the pack root (relative path, size, mtime) —
      the geometry that is parsed and partitioned; a Phase 2 y-bake that
      rewrites a live ``.obj`` invalidates automatically.  ``.anchor_bak``
      backups are not ``.obj`` files and stay out of it (same rule and
      rationale as the classification fingerprint);
    * the two config constants that drive partitioning,
      ``DSF_OBJECT_CONTACT_EPSILON_M`` and ``DSF_OBJECT_MIN_REACH_M``
      (their float values enter the digest);
    * ``gate_constants`` — the building-pad footprint gates the ring set
      also depends on (defect 2026-07-17: the connector pre-filter flag /
      span / fill, the structure span gate, the area backstop, and the
      ``OBJECT_BRIDGE_TERRAIN`` terrain-feature exclusion that drops
      tunnel/bridge/deck resources from the pool); a change to any of
      them invalidates the cache, since the cached rings are computed
      under them;
    * :data:`_OBJECT_FOOTPRINT_CACHE_VERSION`.

    ACCEPTED RISK (identical to the classification cache): out-of-pack,
    ``library.txt``-resolved ``.obj`` resources are NOT fingerprinted —
    only files physically under the pack root are walked — so an edit to
    a shared library object elsewhere in the X-Plane install will not
    invalidate this cache.  Pack-local resources (the common case for the
    co-baked terminal ``.obj`` files this reader targets) are covered.

    The sidecar lives under :func:`airport_mod_cache_dir`, never in the
    pack (user ruling 2026-07-15); any pre-ruling in-pack sidecar found
    at the pack root is removed here so the pack stays clean.

    Returns ``(None, None)`` when no pack root is known (nowhere to key a
    sidecar on) or fingerprinting fails."""
    cache_directory = airport_mod_cache_dir(pack_root)
    if cache_directory is None:
        return None, None
    # Legacy cleanup (the point of the ruling): the old in-pack sidecar
    # would keep cluttering the pack — remove exactly that one filename
    # at the pack root, swallowing every OSError.
    try:
        os.remove(os.path.join(pack_root,
                               _OBJECT_FOOTPRINT_LEGACY_SIDECAR_NAME))
    except OSError:
        pass
    import hashlib
    digest = hashlib.sha1()
    try:
        digest.update(str(cache_version).encode())
        dsf_stat = os.stat(dsf_path)
        digest.update(
            f"{os.path.basename(dsf_path)}:{dsf_stat.st_size}"
            f":{dsf_stat.st_mtime}".encode()
        )
        object_entries = []
        for directory, _subdirectories, file_names in os.walk(pack_root):
            for file_name in file_names:
                if not file_name.lower().endswith(".obj"):
                    continue
                full_path = os.path.join(directory, file_name)
                try:
                    file_stat = os.stat(full_path)
                except OSError:
                    continue
                object_entries.append(
                    f"{os.path.relpath(full_path, pack_root)}"
                    f":{file_stat.st_size}:{file_stat.st_mtime}"
                )
        for entry in sorted(object_entries):
            digest.update(entry.encode())
        digest.update(
            f"epsilon:{float(contact_epsilon_metres)!r}"
            f":reach:{float(minimum_reach_metres)!r}".encode()
        )
        digest.update(
            (
                "gates:"
                + ":".join(repr(float(value)) for value in gate_constants)
            ).encode()
        )
    except OSError:
        return None, None
    dsf_stem = os.path.splitext(os.path.basename(dsf_path))[0]
    return (
        os.path.join(
            cache_directory,
            f"{sidecar_prefix}_{dsf_stem}.cache",
        ),
        digest.hexdigest(),
    )


def read_dsf_object_buildings(
    dsf_path: str,
    cache_dir: str | None = None,
    xplane_root: str | None = None,
) -> list[tuple[list[tuple[float, float]],
                list[list[tuple[float, float]]],
                str]]:
    """Extract OBJ8 scenery-object structure footprints from a DSF file.

    Scenery authors bake many buildings into one ``.obj`` whose DSF
    placement anchor may sit hundreds of metres from any geometry, so
    ``read_dsf_buildings`` (which requires ``.fac`` facades or ``.agp``
    hangars) cannot see them at all — roughly 105 buildings at KCLT.
    This reader walks the terrain-draped ``OBJECT`` placements, resolves
    and parses each ``.obj`` resource, partitions the solid geometry
    into structures (the SAME contact-graph partition Phase 2 bakes
    against — amendment A1), and emits one footprint ring per structure.

    Returns the same building-tuple shape as ``read_dsf_buildings``:
    ``(outer_ring, holes, role)`` with ``role = "object"`` and
    ``holes = []`` (the hull has none; the union ring drops interiors in
    v1).  Rings are unclosed, in ``(longitude, latitude)``.  Returns
    ``[]`` on any failure to load the DSF text.

    Multi-placement definitions are ACCEPTED here — N placements of one
    ``.obj`` are N buildings, each with its own footprint (invariant
    I-5).  The refusal of multi-placement definitions (invariant I-4)
    applies only to the Phase 2 y-bake, where a correction differs per
    placement and cannot be baked into one shared file.

    Resource resolution is pack-relative-wins, then ``library.txt``
    (``obj8_reader.resolve_object_resource``) — the order X-Plane itself
    uses; ``read_dsf_buildings`` never needed ``_pack_root_for_dsf``
    but pack-local resources such as
    ``Terminals/Hangar/Charlotte_Airport_007_ALB.obj`` do.

    The whole result is cached in a sidecar under the data root's
    ``Airport_mod_cache/<pack>/``
    (``o4_object_footprints_<dsf-stem>.cache``, user ruling 2026-07-15:
    never inside the pack) keyed on a fingerprint of the DSF,
    every pack-local ``.obj``, and the two partition config constants —
    the O(n^2) contact-graph partition re-runs only when an input
    actually changes (see :func:`_object_footprint_sidecar` for the
    fingerprint, including the accepted out-of-pack library-object risk).
    ``O4_OBJECT_FOOTPRINT_CACHE=0`` disables the cache entirely (no read,
    no write); with no resolvable pack root the reader behaves as it did
    before the cache existed.
    """
    # Function-local config imports so tests can monkeypatch the values
    # (the module-level idiom at the top of this file freezes them —
    # spec section 4-W1, "one trap").
    from .config import (
        DSF_OBJECT_CONNECTOR_MAX_FILL,
        DSF_OBJECT_CONNECTOR_PREFILTER,
        DSF_OBJECT_CONNECTOR_SPAN_M,
        DSF_OBJECT_CONTACT_EPSILON_M,
        DSF_OBJECT_MAX_FOOTPRINT_AREA_M2,
        DSF_OBJECT_MAX_STRUCTURE_SPAN_M,
        DSF_OBJECT_MIN_REACH_M,
        OBJECT_BRIDGE_TERRAIN,
    )
    from . import obj8_reader as _OBJ8
    from . import object_anchor as _ANCHOR
    from . import object_footprints as _FOOTPRINTS

    # ── Pack-sidecar footprint cache (default ON) ──  A hit skips ALL
    # ``.obj`` parsing and the contact-graph partition and returns the
    # cached ring set.  ``O4_OBJECT_FOOTPRINT_CACHE=0`` disables it.
    sidecar_path: str | None = None
    fingerprint: str | None = None
    if os.environ.get("O4_OBJECT_FOOTPRINT_CACHE", "1") == "1":
        import pickle
        sidecar_path, fingerprint = _object_footprint_sidecar(
            dsf_path, _pack_root_for_dsf(dsf_path),
            DSF_OBJECT_CONTACT_EPSILON_M, DSF_OBJECT_MIN_REACH_M,
            gate_constants=(
                # Building-pad footprint gates the cached ring set depends
                # on (defect 2026-07-17) — a change invalidates the cache.
                DSF_OBJECT_MAX_FOOTPRINT_AREA_M2,
                DSF_OBJECT_MAX_STRUCTURE_SPAN_M,
                float(DSF_OBJECT_CONNECTOR_PREFILTER),
                DSF_OBJECT_CONNECTOR_SPAN_M,
                DSF_OBJECT_CONNECTOR_MAX_FILL,
                # Terrain-feature exclusion (defect 2026-07-17, EGLL
                # Building36): tunnel/bridge/deck resources drop from the
                # building pool when this feature is on, so the cached
                # ring set depends on it — a toggle invalidates the cache.
                float(OBJECT_BRIDGE_TERRAIN),
            ),
        )
        if sidecar_path and fingerprint and os.path.isfile(sidecar_path):
            try:
                with open(sidecar_path, "rb") as sidecar_file:
                    payload = pickle.load(sidecar_file)
                if payload.get("fingerprint") == fingerprint:
                    UI.vprint(
                        1,
                        "   [dsf-object] footprints read from the pack "
                        "sidecar cache (fingerprint match)",
                    )
                    return payload["result"]
                UI.vprint(
                    1,
                    "   [dsf-object] footprint pack sidecar cache STALE "
                    "(pack edited since it was written) - recomputing",
                )
            except Exception:
                pass

    lines = _load_dsf_text(dsf_path, cache_dir)
    if not lines:
        return []
    # Read every OBJECT/OBJECT_AGL/OBJECT_MSL placement.  The building
    # pool uses only the terrain-relative ones (the historical set — MSL
    # rows were skipped before), so building formation is unchanged; the
    # MSL rows feed the Feature-B terrain classifier below (absolute deck
    # elevations, invariant to the anchor-family read).
    all_placements = _OBJ8.read_dsf_object_placements(
        lines,
        accept_resource=lambda resource: resource.lower().endswith(".obj"),
        include_object_msl=True,
    )
    mean_sea_level_placements = [
        p for p in all_placements if p.placement_kind == "OBJECT_MSL"]
    placements = [
        p for p in all_placements if p.placement_kind != "OBJECT_MSL"]
    if not placements:
        return []
    pack_root = _pack_root_for_dsf(dsf_path)

    # Resolve and parse each distinct resource once; keep only the
    # correction candidates (solid geometry whose reach exceeds the
    # detector floor — a compact, correctly anchored object is X-Plane's
    # business, not ours).
    resolved_paths: dict[str, str] = {}
    geometry_by_resource: dict = {}
    for resource_path in sorted({p.resource_path for p in placements}):
        physical_path = _OBJ8.resolve_object_resource(
            resource_path, pack_root, xplane_root)
        if physical_path is None:
            continue
        # Ruling R1 (same choice as Phase 2 discovery in
        # ``post_mesh.discover_and_rebake_airport``): geometry is ALWAYS
        # read from the ``.anchor_bak`` original when one exists.  After
        # a Phase 2 y-bake the LIVE file carries per-vertex offsets, so
        # its base y is no longer ~0 and every rebaked structure would
        # read as elevated — on a rebaked pack this loop then produces
        # ZERO building rings (found at KBNA 2026-07-14: the whole
        # terminal complex vanished from the building pool after the
        # first rebake).
        from .object_rebake import BACKUP_SUFFIX

        backup_path = physical_path + BACKUP_SUFFIX
        geometry_source_path = (
            backup_path if os.path.isfile(backup_path) else physical_path)
        geometry = _load_object_geometry(geometry_source_path)
        if geometry is None or not geometry.has_solid_geometry:
            continue
        if geometry.solid_reach_metres() < DSF_OBJECT_MIN_REACH_M:
            continue
        # CONNECTOR PRE-FILTER (defect 2026-07-17, UK payware co-baked
        # airports): a perimeter fence, road/rail network or whole-complex
        # ground slab spans the field and touches every real building; left
        # in the pool it chains them all into one convex-hull mega-pad that
        # buries the real buildings and the below-grade tunnels.  Drop it
        # here — BEFORE ``discover_object_pools`` and the weld/contact
        # partition — so it can never chain components; report through the
        # skip path (config DSF_OBJECT_CONNECTOR_SPAN_M / _MAX_FILL).  A
        # large but FILLED terminal fails the fill test and is kept.
        # DEFAULT OFF (verification finding — a per-object span+fill test
        # cannot separate a co-baked building texture-page from a true
        # bridging connector, so it gutted EGGW/EGLL buildings; the
        # STRUCTURE span gate in ``object_footprints.structure_ring`` is
        # the sound per-structure fix).  Owner ruling pending.
        is_connector, connector_metrics = (
            _ANCHOR.is_connector_resource(
                geometry,
                connector_span_metres=DSF_OBJECT_CONNECTOR_SPAN_M,
                connector_maximum_fill=DSF_OBJECT_CONNECTOR_MAX_FILL,
            )
            if DSF_OBJECT_CONNECTOR_PREFILTER
            else (False, None)
        )
        if is_connector:
            UI.vprint(
                1,
                f"   [dsf-object] {resource_path} is a CONNECTOR (span "
                f"{connector_metrics.span_metres:.0f} m > "
                f"{DSF_OBJECT_CONNECTOR_SPAN_M:.0f} m, hull-fill "
                f"{connector_metrics.hull_fill_ratio:.3f} < "
                f"{DSF_OBJECT_CONNECTOR_MAX_FILL:.2f}) — excluded from "
                "building pooling (O4_DSF_OBJECT_CONNECTOR_SPAN_M).",
            )
            continue
        resolved_paths[resource_path] = physical_path
        geometry_by_resource[resource_path] = geometry
    if not resolved_paths:
        return []

    # TERRAIN-FEATURE EXCLUSION (defect 2026-07-17, EGLL Building36): a
    # tunnel is authored as a shell + deck pair (``N.obj`` + ``Na.obj``)
    # that share an anchor and weld into one rigid ground-touching
    # structure at the contact epsilon.  Its convex-hull footprint (EGLL
    # 9/9a: 88,414 m² over 654 m; 2/2a: 87,148 m² over 1,076 m) falls
    # under the area backstop, so it emitted AS a building pad — a
    # phantom flat pad burying the below-grade deck.  Tunnels, bridges
    # and interior deck cutouts are Feature-B object-TERRAIN material,
    # not building pads; the shared Feature-B classifier
    # (``object_terrain_features.classify_object_terrain_features``) is a
    # geometric recognizer — below-grade drivable enclosure for tunnels,
    # deck/abutment signature for bridges, with a building-likeness gate
    # that protects real terminals — and it needs only placements and
    # geometry, both already in hand here.  Excluding the resources it
    # consumes BEFORE pooling/weld means they can never chain into a
    # pad.  Gated on ``OBJECT_BRIDGE_TERRAIN`` (the terrain feature that
    # then owns them): a tunnel leaves the building pool exactly when the
    # feature that adapts it is active.  Pavement polygons are not
    # available at Phase-1 building extraction, so bridge classification
    # here falls back to the deck-crest contract — a strict subset of
    # what the elevation-phase classifier consumes, so a bridge missed
    # here simply stays a building pad as it did before (no regression),
    # while the below-grade tunnel signature (the reported defect) needs
    # no pavement.
    from .config import OBJECT_BRIDGE_TERRAIN
    if OBJECT_BRIDGE_TERRAIN:
        from . import object_terrain_features as _TERRAIN
        classified_placements = [
            p for p in placements if p.resource_path in geometry_by_resource]
        try:
            classification = _TERRAIN.classify_object_terrain_features(
                classified_placements,
                geometry_by_resource,
                pavement_polygons_longitude_latitude=None,
                mean_sea_level_placements=mean_sea_level_placements,
                pack_root=pack_root or "",
            )
        except Exception:
            # A classifier failure must never break building extraction;
            # fall back to the un-excluded pool (the pre-fix behaviour).
            classification = None
        if classification is not None:
            terrain_resources = {
                resource for _root, resource in classification.exclusions
                if resource in resolved_paths}
            if terrain_resources:
                UI.vprint(
                    1,
                    f"   [dsf-object] {len(terrain_resources)} resource(s) "
                    "classified as tunnel/bridge/deck terrain "
                    "(O4_OBJECT_BRIDGE_TERRAIN) — excluded from building "
                    "pooling so they cannot chain into a pad: "
                    f"{sorted(os.path.basename(r) for r in terrain_resources)}",
                )
                for resource in terrain_resources:
                    resolved_paths.pop(resource, None)
                    geometry_by_resource.pop(resource, None)
    if not resolved_paths:
        return []

    kept_placements = [p for p in placements
                       if p.resource_path in resolved_paths]

    # An ObjectPool carries exactly one placement per resource, so a
    # multi-placement definition cannot enter the shared pooling — each
    # of its placements becomes its own single-object pool (N placements
    # = N buildings, invariant I-5).  Single-placement resources pool
    # together by world bounding-box overlap so structures spanning
    # several co-baked objects merge (invariant I-1).
    placement_count_by_resource: dict[str, int] = {}
    for placement in kept_placements:
        placement_count_by_resource[placement.resource_path] = (
            placement_count_by_resource.get(placement.resource_path, 0) + 1)
    single_placements = [
        p for p in kept_placements
        if placement_count_by_resource[p.resource_path] == 1]
    multi_placements = [
        p for p in kept_placements
        if placement_count_by_resource[p.resource_path] > 1]

    pools = []
    if single_placements:
        single_resources = {p.resource_path for p in single_placements}
        pools.extend(_ANCHOR.discover_object_pools(
            single_placements,
            {resource: resolved_paths[resource]
             for resource in single_resources},
            {resource: geometry_by_resource[resource]
             for resource in single_resources},
            epsilon_metres=DSF_OBJECT_CONTACT_EPSILON_M,
        ))
    for placement in multi_placements:
        pools.append(_ANCHOR.ObjectPool(
            placements=[placement],
            resolved_paths={
                placement.resource_path:
                    resolved_paths[placement.resource_path]},
        ))

    out: list[tuple[list[tuple[float, float]],
                    list[list[tuple[float, float]]],
                    str]] = []
    for pool in pools:
        pool_geometry_by_resource = {
            resource: geometry_by_resource[resource]
            for resource in pool.resolved_paths}
        structures = _ANCHOR.partition_structures(
            pool,
            pool_geometry_by_resource,
            epsilon_metres=DSF_OBJECT_CONTACT_EPSILON_M,
        )
        for structure in structures:
            ring = _FOOTPRINTS.structure_ring(
                structure, pool_geometry_by_resource, pool.placements)
            if ring is not None and len(ring) >= 3:
                out.append((ring, [], "object"))

    # Persist the finished ring set for the next build of this unchanged
    # pack.  A write failure must never break a build (out of space, a
    # read-only pack) — swallow it and let the next run recompute.
    if sidecar_path is not None and fingerprint is not None:
        import pickle
        try:
            os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
            with open(sidecar_path, "wb") as sidecar_file:
                pickle.dump(
                    {"fingerprint": fingerprint, "result": out},
                    sidecar_file,
                )
            UI.vprint(
                1,
                "   [dsf-object] footprints written to the pack sidecar "
                f"cache ({os.path.basename(sidecar_path)})",
            )
        except Exception:
            pass

    return out


# ── OBJ8 scenery objects as PAVEMENT (user 2026-07-17, HECA Tai
# Models) ──  Ground-paint packs draw base pavement as DRAPED-ONLY
# ``.obj`` texture pages: one OBJECT placement carries the whole
# airport's geometry for one texture (HECA ``Airport/ground/asphalt.obj``
# = 31k draped vertices, zero solid triangles).  The building reader
# skips them at ``has_solid_geometry``; the ``.pol`` pavement reader
# never sees them.  The base-vs-decal discriminator is the DECLARED DRAW
# LAYER: base pavement stacks UNDER markings via
# ``ATTR_layer_group_draped runways/taxiways <small offset>`` while taxi
# lines, ramp decals and gate signs sit in group ``markings`` or at
# offsets 2..5 (HECA survey 2026-07-17 — every base asphalt/concrete
# page at ``runways 1``, every decal above it).
_PAVEMENT_OBJECT_LAYER_GROUPS = frozenset({"runways", "taxiways"})
_OBJECT_PAVEMENT_SIDECAR_PREFIX = "o4_object_pavements"
_OBJECT_PAVEMENT_CACHE_VERSION = 1


def _is_pavement_object(def_path: str, geometry) -> bool:
    """True when an OBJ8 resource is a base ground-pavement texture page.

    Conjunctive, conservative:

    * draped-only geometry (any solid triangle → it is a 3-D object,
      the building path's business);
    * declares ``ATTR_layer_group_draped`` in a pavement group at an
      offset no greater than ``DSF_OBJECT_PAVEMENT_MAX_LAYER_OFFSET``
      (an object declaring NO draped layer group is refused — the
      base-vs-decal ordering signal is the whole classification);
    * no decorative token in the file's basename (same veto vocabulary
      as the ``.pol`` SURFACE classifier; basename only, so a
      directory named e.g. ``Flightline/`` cannot false-veto).
    """
    from .config import DSF_OBJECT_PAVEMENT_MAX_LAYER_OFFSET

    if geometry.solid_triangles or not geometry.draped_triangles:
        return False
    if geometry.draped_layer_group is None:
        return False
    layer_group_name, layer_offset = geometry.draped_layer_group
    if layer_group_name not in _PAVEMENT_OBJECT_LAYER_GROUPS:
        return False
    if layer_offset > DSF_OBJECT_PAVEMENT_MAX_LAYER_OFFSET:
        return False
    basename = os.path.basename(def_path).lower()
    if any(token.lower() in basename
           for token in _DECORATIVE_SKIP_TOKENS):
        return False
    return True


def read_dsf_object_pavements(
    dsf_path: str,
    cache_dir: str | None = None,
    xplane_root: str | None = None,
) -> list[tuple[list[tuple[float, float]],
                list[list[tuple[float, float]]],
                str]]:
    """Extract base-pavement OBJ8 ground-paint patches from a DSF file.

    Walks the ``OBJECT`` placements, resolves and parses each ``.obj``
    resource, keeps those :func:`_is_pavement_object` classifies as base
    pavement, and unions each accepted placement's draped triangles into
    pavement patches (:func:`object_footprints.draped_pavement_patches`
    — ALL disjoint patches kept, interior holes honoured, patches under
    ``DSF_OBJECT_PAVEMENT_MIN_PATCH_M2`` dropped).

    Returns the ``read_dsf_pavements`` tuple shape — ``(outer_ring,
    holes, def_path)``, rings unclosed in ``(longitude, latitude)`` — so
    the pipeline's DSF pavement sweep (distance gate, boundary gate,
    overlay drop, third-party marking) applies to both sources through
    one path.  Returns ``[]`` on any failure to load the DSF text.

    The result is sidecar-cached per pack
    (``o4_object_pavements_<dsf-stem>.cache`` under
    :func:`airport_mod_cache_dir`) on the same fingerprint machinery as
    the object-building footprints, with the pavement gate constants in
    the digest; ``O4_OBJECT_FOOTPRINT_CACHE=0`` disables this cache too
    (one switch for both object sidecars).
    """
    from .config import (
        DSF_OBJECT_PAVEMENT_MAX_LAYER_OFFSET,
        DSF_OBJECT_PAVEMENT_MIN_PATCH_M2,
    )
    from . import obj8_reader as _OBJ8
    from . import object_footprints as _FOOTPRINTS

    sidecar_path: str | None = None
    fingerprint: str | None = None
    if os.environ.get("O4_OBJECT_FOOTPRINT_CACHE", "1") == "1":
        import pickle
        sidecar_path, fingerprint = _object_footprint_sidecar(
            dsf_path, _pack_root_for_dsf(dsf_path),
            0.0, 0.0,  # no contact partition in the pavement path
            gate_constants=(
                float(DSF_OBJECT_PAVEMENT_MAX_LAYER_OFFSET),
                DSF_OBJECT_PAVEMENT_MIN_PATCH_M2,
            ),
            sidecar_prefix=_OBJECT_PAVEMENT_SIDECAR_PREFIX,
            cache_version=_OBJECT_PAVEMENT_CACHE_VERSION,
        )
        if sidecar_path and fingerprint and os.path.isfile(sidecar_path):
            try:
                with open(sidecar_path, "rb") as sidecar_file:
                    payload = pickle.load(sidecar_file)
                if payload.get("fingerprint") == fingerprint:
                    UI.vprint(
                        1,
                        "   [dsf-object] pavement patches read from the "
                        "pack sidecar cache (fingerprint match)",
                    )
                    return payload["result"]
                UI.vprint(
                    1,
                    "   [dsf-object] pavement pack sidecar cache STALE "
                    "(pack edited since it was written) - recomputing",
                )
            except Exception:
                pass

    lines = _load_dsf_text(dsf_path, cache_dir)
    if not lines:
        return []
    placements = _OBJ8.read_dsf_object_placements(
        lines,
        accept_resource=lambda resource: resource.lower().endswith(".obj"),
    )
    if not placements:
        return []
    pack_root = _pack_root_for_dsf(dsf_path)

    out: list[tuple[list[tuple[float, float]],
                    list[list[tuple[float, float]]],
                    str]] = []
    accepted_resource_count = 0
    for resource_path in sorted({p.resource_path for p in placements}):
        physical_path = _OBJ8.resolve_object_resource(
            resource_path, pack_root, xplane_root)
        if physical_path is None:
            continue
        geometry = _load_object_geometry(physical_path)
        if geometry is None:
            continue
        if not _is_pavement_object(resource_path, geometry):
            continue
        resource_patch_count = 0
        for placement in placements:
            if placement.resource_path != resource_path:
                continue
            for outer_ring, hole_rings in (
                    _FOOTPRINTS.draped_pavement_patches(
                        geometry, placement,
                        DSF_OBJECT_PAVEMENT_MIN_PATCH_M2)):
                out.append((outer_ring, hole_rings, resource_path))
                resource_patch_count += 1
        if resource_patch_count:
            accepted_resource_count += 1
            UI.vprint(
                2,
                f"  [dsf-object] pavement object {resource_path}: "
                f"{resource_patch_count} patch(es)",
            )
    if out:
        UI.vprint(
            1,
            f"   [dsf-object] {len(out)} ground-paint pavement patches "
            f"from {accepted_resource_count} draped base-layer object(s)",
        )

    if sidecar_path is not None and fingerprint is not None:
        import pickle
        try:
            os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
            with open(sidecar_path, "wb") as sidecar_file:
                pickle.dump(
                    {"fingerprint": fingerprint, "result": out},
                    sidecar_file,
                )
        except Exception:
            pass

    return out


def find_associated_dsf(apt_dat_path: str,
                        apt_lat: float,
                        apt_lon: float) -> str | None:
    """Locate the DSF file in the same scenery pack as ``apt_dat_path``
    that covers ``(apt_lat, apt_lon)``.

    DSFs are organized by tile (1° × 1°) inside an ``Earth nav data``
    tree.  The apt.dat sits next to a per-tile subtree like
    ``Earth nav data/+60-140/+60-136.dsf``.

    Returns the absolute DSF path or None.
    """
    if not apt_dat_path or not os.path.isfile(apt_dat_path):
        return None
    # apt.dat lives at <pack>/Earth nav data/apt.dat
    end_dir = os.path.dirname(apt_dat_path)
    if os.path.basename(end_dir) != "Earth nav data":
        return None
    # Tile + group dir naming.
    import math
    tile_lat = int(math.floor(apt_lat))
    tile_lon = int(math.floor(apt_lon))
    grp_lat = (tile_lat // 10) * 10
    grp_lon = (tile_lon // 10) * 10

    def _fmt(v: int, pad: int) -> str:
        sign = "+" if v >= 0 else "-"
        return f"{sign}{abs(v):0{pad}d}"

    grp = f"{_fmt(grp_lat, 2)}{_fmt(grp_lon, 3)}"
    tile = f"{_fmt(tile_lat, 2)}{_fmt(tile_lon, 3)}"
    cand = os.path.join(end_dir, grp, tile + ".dsf")
    if os.path.isfile(cand):
        return cand
    return None
