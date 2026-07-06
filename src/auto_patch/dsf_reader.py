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
