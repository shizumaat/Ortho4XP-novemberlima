#!/usr/bin/env python3
"""Probe X-Plane default Global Scenery base-mesh terrain assignments.

This is the Work-package-0 reconnaissance tool for the *texture modes*
feature (``docs/specs/texture-mode-spec.md``).  It answers the format
questions the ``DefaultTerrainMap`` implementation (work package 1) and the
``default_xplane`` writer (work package 2) are built against:

  * which terrain-library namespace X-Plane 12 default DSFs reference in
    ``TERRAIN_DEF`` (``lib/g10/terrain10`` vs anything newer);
  * how many land terrains are *non*-projected (decision 9's fallback only
    fires if any exist);
  * the water terrain path(s) and their per-vertex plane counts;
  * the plane counts of land patches (the 5-plane projected assumption).

Given an X-Plane install (discovered the same way the app does) and a tile,
it runs the bundled ``DSFTool --dsf2text`` on the default Global Scenery DSF
for that tile and streams the (potentially hundreds-of-megabytes) text dump
line by line -- it never loads the whole dump into memory.

Usage
-----
Report on a tile (X-Plane root / overlay source auto-discovered)::

    python tools/probe_default_terrain.py 60 -136

Explicit install / overlay source / DSF::

    python tools/probe_default_terrain.py --xplane-root "/path/X-Plane 12" 60 -136
    python tools/probe_default_terrain.py --overlay-src "/path/Global Scenery/..." 60 -136
    python tools/probe_default_terrain.py --dsf /path/+60-136.dsf 60 -136

Write a truncated pytest fixture (a few patches of each primitive type,
physical and overlay, with their ``TERRAIN_DEF`` header lines)::

    python tools/probe_default_terrain.py 60 -136 \
        --dump-fixture tests/fixtures/default_dsf_excerpt.txt

Discovery order for the X-Plane install mirrors the app:

  1. ``--xplane-root`` / ``--overlay-src`` / ``--dsf`` command-line values;
  2. ``custom_overlay_src`` in the repo ``Ortho4XP.cfg`` (points straight at
     the Global Scenery folder that holds ``Earth nav data``);
  3. the ``xplane_dir`` preference in ``.qt_prefs.json`` (see
     ``src/O4_Qt_GUI.py``);
  4. ``detect_xplane_installs()`` best-effort discovery (see
     ``src/O4_Qt_Wizard.py``).

The DSFTool binary is located through the same helper the DSF reader uses
(``src/auto_patch/dsf_reader.py::_dsftool_path``).

DSFTool text-dump facts this tool relies on (X-Plane 12, verified on
``+60-136`` / CYXY)::

    TERRAIN_DEF <path>                 # index-ordered terrain table; index 0
                                       # is the name-only ``terrain_Water``
    BEGIN_PATCH <terr> <n_lod> <f_lod> <flags> <planes>
                                       # flags bit: 1 = physical, 2 = overlay
    BEGIN_PRIMITIVE <type>             # 0 = triangles, 1 = strip, 2 = fan
    PATCH_VERTEX <lon> <lat> <elev> <nx> <ny> [<s> <t>]
    END_PRIMITIVE / END_PATCH

Triangle counts per primitive: type 0 -> n_vertices // 3; type 1 (strip)
and type 2 (fan) -> n_vertices - 2.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from math import floor
from typing import Iterator, Optional

# --------------------------------------------------------------------------
# Repo wiring: make src/ importable and reuse the DSFTool locator by symbol.
# --------------------------------------------------------------------------
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC_DIR = os.path.join(_REPO_ROOT, "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import O4_File_Names as FNAMES  # noqa: E402

# ``_dsftool_path`` reads ``FNAMES.Utils_dir`` (``resource_path("Utils")``),
# which is anchored on the current working directory.  Pin it to this repo's
# Utils/ so the tool works regardless of where it is invoked from.
FNAMES.Utils_dir = os.path.join(_REPO_ROOT, "Utils")

from auto_patch.dsf_reader import _dsftool_path  # noqa: E402


# --------------------------------------------------------------------------
# Tile / path helpers (same DSF-resolution logic as
# O4_DSF_Utils.extract_elevation_and_bathymetry_data -> FNAMES.long_latlon).
# --------------------------------------------------------------------------
def _long_latlon(lat: int, lon: int) -> str:
    """``<group>/<tile>`` DSF stem, e.g. ``+60-140/+60-136`` (matches
    ``O4_File_Names.long_latlon``)."""
    strlat = "{:+.0f}".format(lat).zfill(3)
    strlon = "{:+.0f}".format(lon).zfill(4)
    strlatround = "{:+.0f}".format(floor(lat / 10) * 10).zfill(3)
    strlonround = "{:+.0f}".format(floor(lon / 10) * 10).zfill(4)
    return os.path.join(strlatround + strlonround, strlat + strlon)


def _read_cfg_value(cfg_path: str, key: str) -> Optional[str]:
    """Return the value of ``key=value`` in a flat Ortho4XP config file."""
    try:
        with open(cfg_path, "r", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith(key + "="):
                    return line[len(key) + 1:].strip()
    except OSError:
        return None
    return None


def _read_pref_value(prefs_path: str, key: str) -> Optional[str]:
    """Return ``prefs[key]`` from a JSON prefs file, or None."""
    import json
    try:
        with open(prefs_path, "r", errors="replace") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    value = data.get(key)
    return str(value) if value else None


def _detect_xplane_installs() -> list[str]:
    """Best-effort X-Plane discovery, mirroring
    ``O4_Qt_Wizard.detect_xplane_installs`` (kept self-contained so this tool
    never needs a GUI import)."""
    candidates: list[str] = []
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        pref_dir = os.path.join(home, "Library", "Preferences")
    elif sys.platform.startswith("win"):
        pref_dir = os.environ.get("LOCALAPPDATA", "")
    else:
        pref_dir = os.path.join(home, ".x-plane")
    for fname in ("x-plane_install_12.txt", "x-plane_install_11.txt"):
        path = os.path.join(pref_dir, fname) if pref_dir else ""
        try:
            with open(path, "r", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        candidates.append(line)
        except OSError:
            pass
    for base in (
        "/Applications", home,
        os.path.join(home, "Desktop"), os.path.join(home, "Applications"),
    ):
        for name in ("X-Plane 12", "X-Plane 11"):
            candidates.append(os.path.join(base, name))
    seen: set[str] = set()
    found: list[str] = []
    for cand in candidates:
        cand = os.path.normpath(cand)
        if cand in seen:
            continue
        seen.add(cand)
        if os.path.isdir(os.path.join(cand, "Custom Scenery")):
            found.append(cand)
    return found


def _overlay_src_from_xplane_root(xplane_root: str) -> Optional[str]:
    """The Global Scenery folder holding ``Earth nav data`` under an
    X-Plane root (X-Plane 12 or 11 default naming)."""
    for name in (
        "X-Plane 12 Global Scenery",
        "X-Plane 11 Global Scenery",
        "X-Plane Global Scenery",
    ):
        cand = os.path.join(xplane_root, "Global Scenery", name)
        if os.path.isdir(os.path.join(cand, "Earth nav data")):
            return cand
    return None


def _xplane_root_from_overlay_src(overlay_src: str) -> Optional[str]:
    """Walk up from a Global Scenery overlay folder to the X-Plane root that
    holds a ``Resources`` tree (needed to resolve ``.ter`` resources)."""
    current = os.path.abspath(overlay_src)
    for _ in range(4):
        parent = os.path.dirname(current)
        if parent == current:
            break
        if os.path.isdir(os.path.join(parent, "Resources")):
            return parent
        current = parent
    return None


@dataclass
class InstallPaths:
    """Resolved locations for one probe run."""

    dsf_path: str
    overlay_src: Optional[str]
    xplane_root: Optional[str]
    source: str  # human description of how we found the DSF


def resolve_install_paths(
    lat: int,
    lon: int,
    xplane_root_arg: Optional[str],
    overlay_src_arg: Optional[str],
    dsf_arg: Optional[str],
    cfg_path: Optional[str],
    prefs_path: Optional[str],
) -> Optional[InstallPaths]:
    """Discover the default-scenery DSF path and the X-Plane root, following
    the same precedence the app uses.  Returns None (after printing why) if
    no DSF can be located."""
    if dsf_arg:
        overlay = overlay_src_arg
        root = xplane_root_arg
        if overlay is None:
            # <overlay>/Earth nav data/<group>/<tile>.dsf -> climb three up.
            end = os.path.dirname(os.path.dirname(os.path.dirname(dsf_arg)))
            if os.path.basename(end) == "Earth nav data":
                overlay = os.path.dirname(end)
        if root is None and overlay:
            root = _xplane_root_from_overlay_src(overlay)
        if not os.path.isfile(dsf_arg):
            print(f"ERROR: --dsf path does not exist: {dsf_arg}")
            return None
        return InstallPaths(dsf_arg, overlay, root, "explicit --dsf")

    overlay_src = overlay_src_arg
    source = "explicit --overlay-src"

    if overlay_src is None and cfg_path and os.path.isfile(cfg_path):
        value = _read_cfg_value(cfg_path, "custom_overlay_src")
        if value:
            overlay_src = value
            source = f"custom_overlay_src in {cfg_path}"
    if overlay_src is None and cfg_path and os.path.isfile(cfg_path):
        value = _read_cfg_value(cfg_path, "custom_overlay_src_alternate")
        if value:
            overlay_src = value
            source = f"custom_overlay_src_alternate in {cfg_path}"

    xplane_root = xplane_root_arg
    if overlay_src is None:
        if xplane_root is None and prefs_path and os.path.isfile(prefs_path):
            xplane_root = _read_pref_value(prefs_path, "xplane_dir")
            if xplane_root:
                source = f"xplane_dir pref in {prefs_path}"
        if xplane_root is None:
            installs = _detect_xplane_installs()
            if installs:
                xplane_root = installs[0]
                source = f"detect_xplane_installs() -> {xplane_root}"
        if xplane_root:
            overlay_src = _overlay_src_from_xplane_root(xplane_root)

    if overlay_src is None:
        print("ERROR: could not locate a Global Scenery overlay source. "
              "Pass --overlay-src, --xplane-root, or --dsf, or set "
              "custom_overlay_src in Ortho4XP.cfg.")
        return None

    if xplane_root is None:
        xplane_root = xplane_root_arg or _xplane_root_from_overlay_src(
            overlay_src)

    dsf_path = os.path.join(
        overlay_src, "Earth nav data", _long_latlon(lat, lon) + ".dsf")
    if not os.path.isfile(dsf_path):
        print(f"ERROR: default DSF for tile ({lat}, {lon}) not found at "
              f"{dsf_path}\n       (overlay source: {overlay_src})")
        return None
    return InstallPaths(dsf_path, overlay_src, xplane_root, source)


# --------------------------------------------------------------------------
# DSFTool invocation + streaming line reader.
# --------------------------------------------------------------------------
def run_dsftool_to_text(dsf_path: str) -> Optional[str]:
    """Run ``DSFTool --dsf2text`` on ``dsf_path``, writing to a temp file
    that the caller streams and then deletes.  Returns the text-file path,
    or None on failure."""
    tool = _dsftool_path()
    if tool is None:
        print("ERROR: bundled DSFTool binary not found under Utils/.")
        return None
    handle = tempfile.NamedTemporaryFile(
        suffix=".dsf.text", delete=False)
    text_path = handle.name
    handle.close()
    try:
        subprocess.run(
            [tool, "--dsf2text", dsf_path, text_path],
            check=True, capture_output=True, timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: DSFTool failed on {dsf_path}: {exc}")
        try:
            os.unlink(text_path)
        except OSError:
            pass
        return None
    return text_path


def stream_lines(text_path: str) -> Iterator[str]:
    """Yield the text dump line by line without loading it all into
    memory (dumps run to hundreds of megabytes)."""
    with open(text_path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line


# --------------------------------------------------------------------------
# Streaming report aggregation.
# --------------------------------------------------------------------------
def _triangles_in_primitive(primitive_type: int, vertex_count: int) -> int:
    """Triangle count for a primitive given its vertex count."""
    if vertex_count < 3:
        return 0
    if primitive_type == 0:          # independent triangles
        return vertex_count // 3
    return vertex_count - 2          # strip (1) or fan (2)


@dataclass
class TerrainStats:
    """Accumulated counts for one terrain-table index."""

    path: str
    patch_count: int = 0
    triangle_count: int = 0
    physical_patches: int = 0
    overlay_patches: int = 0
    plane_counts: Counter = field(default_factory=Counter)


@dataclass
class ProbeReport:
    """Everything the streaming pass gathered from a DSF text dump."""

    terrains: list[TerrainStats]
    flag_plane_histogram: Counter          # (flags, planes) -> patch count
    primitive_type_histogram: Counter      # primitive type -> count
    total_patches: int
    total_triangles: int


def build_report(lines: Iterator[str]) -> ProbeReport:
    """Single streaming pass over the DSFTool text dump."""
    terrains: list[TerrainStats] = []
    flag_plane_histogram: Counter = Counter()
    primitive_type_histogram: Counter = Counter()
    total_patches = 0
    total_triangles = 0

    cur_terrain = -1
    cur_flags = 0
    cur_planes = 0
    cur_triangles = 0
    in_primitive = False
    prim_type = 0
    prim_vertices = 0

    for line in lines:
        if line.startswith("TERRAIN_DEF"):
            parts = line.split(maxsplit=1)
            path = parts[1].strip() if len(parts) > 1 else ""
            terrains.append(TerrainStats(path=path))
            continue
        if line.startswith("BEGIN_PATCH"):
            tok = line.split()
            try:
                cur_terrain = int(tok[1])
                cur_flags = int(tok[4])
                cur_planes = int(tok[5])
            except (ValueError, IndexError):
                cur_terrain, cur_flags, cur_planes = -1, 0, 0
            cur_triangles = 0
            flag_plane_histogram[(cur_flags, cur_planes)] += 1
            total_patches += 1
            continue
        if line.startswith("BEGIN_PRIMITIVE"):
            tok = line.split()
            try:
                prim_type = int(tok[1])
            except (ValueError, IndexError):
                prim_type = -1
            primitive_type_histogram[prim_type] += 1
            in_primitive = True
            prim_vertices = 0
            continue
        if line.startswith("PATCH_VERTEX"):
            if in_primitive:
                prim_vertices += 1
            continue
        if line.startswith("END_PRIMITIVE"):
            tris = _triangles_in_primitive(prim_type, prim_vertices)
            cur_triangles += tris
            total_triangles += tris
            in_primitive = False
            continue
        if line.startswith("END_PATCH"):
            if 0 <= cur_terrain < len(terrains):
                stats = terrains[cur_terrain]
                stats.patch_count += 1
                stats.triangle_count += cur_triangles
                stats.plane_counts[cur_planes] += 1
                if cur_flags & 2:
                    stats.overlay_patches += 1
                if cur_flags & 1:
                    stats.physical_patches += 1
            cur_terrain = -1
            continue

    return ProbeReport(
        terrains=terrains,
        flag_plane_histogram=flag_plane_histogram,
        primitive_type_histogram=primitive_type_histogram,
        total_patches=total_patches,
        total_triangles=total_triangles,
    )


# --------------------------------------------------------------------------
# .ter resource resolution (PROJECTED? WET?).
# --------------------------------------------------------------------------
@dataclass
class TerInfo:
    """Result of inspecting a ``.ter`` resource."""

    resolvable: bool
    projected: Optional[bool]
    wet: Optional[bool]
    physical_path: Optional[str]


def _build_library_index(xplane_root: str) -> dict[str, str]:
    """Map default-scenery library virtual paths (``lib/...``) to absolute
    physical ``.ter`` paths, parsing every ``library.txt`` under
    ``Resources/default scenery`` (EXPORT* lines).  First mapping wins."""
    index: dict[str, str] = {}
    default_scenery = os.path.join(
        xplane_root, "Resources", "default scenery")
    if not os.path.isdir(default_scenery):
        return index
    for entry in sorted(os.listdir(default_scenery)):
        lib_txt = os.path.join(default_scenery, entry, "library.txt")
        if not os.path.isfile(lib_txt):
            continue
        base = os.path.dirname(lib_txt)
        try:
            with open(lib_txt, "r", errors="replace") as handle:
                for line in handle:
                    if not line.startswith("EXPORT"):
                        continue
                    tok = line.split()
                    virtual = physical = None
                    for i, part in enumerate(tok):
                        if part.startswith("lib/") and i + 1 < len(tok):
                            virtual = part
                            physical = tok[i + 1]
                            break
                    if virtual and physical and virtual not in index:
                        index[virtual] = os.path.normpath(
                            os.path.join(base, physical))
        except OSError:
            continue
    return index


def _resolve_ter_physical(
    virtual_path: str,
    xplane_root: str,
    library_index: dict[str, str],
) -> Optional[str]:
    """Resolve a ``lib/...`` terrain virtual path to a physical ``.ter``
    file: library.txt mapping first, then a direct ``lib/g10`` ->
    ``1000 world terrain`` fallback."""
    mapped = library_index.get(virtual_path)
    if mapped and os.path.isfile(mapped):
        return mapped
    if virtual_path.startswith("lib/g10/"):
        cand = os.path.join(
            xplane_root, "Resources", "default scenery",
            "1000 world terrain", virtual_path[len("lib/g10/"):])
        if os.path.isfile(cand):
            return cand
    return None


def inspect_ter(
    virtual_path: str,
    xplane_root: Optional[str],
    library_index: dict[str, str],
) -> TerInfo:
    """Inspect a referenced terrain: whether its ``.ter`` resolves under the
    install and, if so, whether it declares ``PROJECTED`` / ``WET``.  The
    name-only ``terrain_Water`` (no ``.ter``) reports unresolvable."""
    if not xplane_root or not virtual_path.endswith(".ter"):
        return TerInfo(False, None, None, None)
    physical = _resolve_ter_physical(
        virtual_path, xplane_root, library_index)
    if physical is None:
        return TerInfo(False, None, None, None)
    projected = False
    wet = False
    try:
        with open(physical, "r", errors="replace") as handle:
            for line in handle:
                token = line.strip().split(None, 1)
                if not token:
                    continue
                head = token[0].upper()
                if head == "PROJECTED":
                    projected = True
                elif head == "WET":
                    wet = True
    except OSError:
        return TerInfo(False, None, None, physical)
    return TerInfo(True, projected, wet, physical)


def _is_water_terrain(path: str, info: TerInfo) -> bool:
    """A terrain is water if it is the name-only ``terrain_Water`` or its
    ``.ter`` declares ``WET``."""
    if path == "terrain_Water" or path.endswith("/terrain_Water"):
        return True
    return bool(info.wet)


# --------------------------------------------------------------------------
# Report rendering.
# --------------------------------------------------------------------------
def render_report(
    paths: InstallPaths,
    report: ProbeReport,
    ter_infos: dict[int, TerInfo],
) -> str:
    """Format the full human-readable report."""
    out: list[str] = []
    out.append("=" * 72)
    out.append("Default-terrain probe report")
    out.append("=" * 72)
    out.append(f"DSF:          {paths.dsf_path}")
    out.append(f"Overlay src:  {paths.overlay_src}")
    out.append(f"X-Plane root: {paths.xplane_root}")
    out.append(f"Discovery:    {paths.source}")
    out.append("")

    # Namespaces used.
    namespaces: Counter = Counter()
    for stats in report.terrains:
        if stats.path == "terrain_Water":
            namespaces["terrain_Water (name-only)"] += 1
        else:
            namespaces[os.path.dirname(stats.path)] += 1
    out.append(f"Terrain table: {len(report.terrains)} entries")
    out.append("TERRAIN_DEF namespaces:")
    for namespace, count in namespaces.most_common():
        out.append(f"    {count:5d}  {namespace}")
    out.append("")

    out.append(f"Total patches:   {report.total_patches}")
    out.append(f"Total triangles: {report.total_triangles}")
    out.append("")

    out.append("Per-patch (flags, planes) histogram:")
    out.append("    flags  planes   patches   meaning")
    for (flags, planes), count in sorted(report.flag_plane_histogram.items()):
        meaning = []
        meaning.append("physical" if flags & 1 else "")
        meaning.append("overlay" if flags & 2 else "")
        label = "+".join(m for m in meaning if m) or f"flags={flags}"
        out.append(f"    {flags:5d}  {planes:6d}   {count:7d}   {label}")
    out.append("")

    out.append("Primitive-type histogram (0=tri, 1=strip, 2=fan):")
    for ptype, count in sorted(report.primitive_type_histogram.items()):
        name = {0: "triangles", 1: "strip", 2: "fan"}.get(ptype, "?")
        out.append(f"    type {ptype} ({name:9s}): {count}")
    out.append("")

    # Referenced terrains (patch_count > 0), with projection/water status.
    referenced = [(i, s) for i, s in enumerate(report.terrains)
                  if s.patch_count > 0]
    out.append(f"Referenced terrains (patch_count > 0): {len(referenced)} "
               f"of {len(report.terrains)}")
    out.append("")

    # Projection summary over referenced non-water land terrains.
    land_projected = 0
    land_nonprojected: list[str] = []
    land_unresolvable: list[str] = []
    for i, stats in referenced:
        info = ter_infos.get(i, TerInfo(False, None, None, None))
        if _is_water_terrain(stats.path, info):
            continue
        if not info.resolvable:
            land_unresolvable.append(stats.path)
        elif info.projected:
            land_projected += 1
        else:
            land_nonprojected.append(stats.path)

    out.append("Land-terrain projection summary (referenced only):")
    out.append(f"    PROJECTED land terrains:     {land_projected}")
    out.append(f"    NON-projected land terrains: {len(land_nonprojected)}")
    for path in land_nonprojected:
        out.append(f"        {path}")
    out.append(f"    Unresolvable land terrains:  {len(land_unresolvable)}")
    for path in land_unresolvable[:20]:
        out.append(f"        {path}")
    if len(land_unresolvable) > 20:
        out.append(f"        ... and {len(land_unresolvable) - 20} more")
    out.append("")

    # Water terrains.
    out.append("Water terrains (referenced):")
    water_found = False
    for i, stats in referenced:
        info = ter_infos.get(i, TerInfo(False, None, None, None))
        if not _is_water_terrain(stats.path, info):
            continue
        water_found = True
        planes = ", ".join(
            f"{p}-plane x{c}" for p, c in sorted(stats.plane_counts.items()))
        out.append(f"    [{i}] {stats.path}")
        out.append(f"        patches={stats.patch_count} "
                   f"triangles={stats.triangle_count} planes: {planes}")
    if not water_found:
        out.append("    (none)")
    out.append("")

    # Full terrain table with counts + status.
    out.append("Full terrain table (index, patches, tris, planes, status):")
    for i, stats in enumerate(report.terrains):
        info = ter_infos.get(i, TerInfo(False, None, None, None))
        if stats.patch_count == 0:
            status = "unused"
        elif _is_water_terrain(stats.path, info):
            status = "WATER"
        elif not info.resolvable:
            status = "land/unresolvable"
        elif info.projected:
            status = "land/PROJECTED"
        else:
            status = "land/non-projected"
        planes = ",".join(
            f"{p}p:{c}" for p, c in sorted(stats.plane_counts.items())) or "-"
        out.append(
            f"    [{i:3d}] patches={stats.patch_count:5d} "
            f"tris={stats.triangle_count:7d} planes={planes:12s} "
            f"{status:20s} {stats.path}")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Fixture dump: a few patches of each primitive type, physical + overlay.
# --------------------------------------------------------------------------
@dataclass
class _PatchCapture:
    """A patch buffered verbatim during the fixture-dump streaming pass."""

    terrain_index: int
    flags: int
    planes: int
    primitive_types: set[int]
    lines: list[str]


def _fixture_category(flags: int, primitive_types: set[int]) -> list[str]:
    """The (overlay/physical) x (primitive) categories a patch satisfies."""
    layer = "overlay" if flags & 2 else "physical"
    return [f"{layer}:{ptype}" for ptype in primitive_types]


def dump_fixture(
    lines: Iterator[str],
    out_path: str,
    per_category: int = 2,
    max_vertices: int = 40,
) -> dict[str, int]:
    """Stream the dump and write a truncated excerpt: up to ``per_category``
    patches for each (physical/overlay) x (triangle/strip/fan) category, plus
    the ``TERRAIN_DEF`` header lines for the terrains those patches reference
    (re-indexed so the excerpt is internally consistent).  Only small patches
    (at most ``max_vertices`` vertices) are captured so the fixture stays
    compact and readable.  A synthetic fan patch is appended if the DSF
    contains none, so the fixture exercises all three primitive types.
    Returns the per-category capture counts."""
    terrain_paths: list[str] = []
    captured: list[_PatchCapture] = []
    category_counts: Counter = Counter()

    # Streaming buffer for the current patch.
    in_patch = False
    buf: list[str] = []
    cur_terrain = -1
    cur_flags = 0
    cur_planes = 0
    cur_vertices = 0
    cur_prim_types: set[int] = set()
    # Cap total capture work so we can stop scanning early.
    want_categories = {f"{layer}:{ptype}"
                       for layer in ("physical", "overlay")
                       for ptype in (0, 1, 2)}

    def _is_complete() -> bool:
        return all(category_counts[c] >= per_category
                   for c in want_categories)

    for line in lines:
        if line.startswith("TERRAIN_DEF"):
            parts = line.split(maxsplit=1)
            terrain_paths.append(parts[1].strip() if len(parts) > 1 else "")
            continue
        if line.startswith("BEGIN_PATCH"):
            tok = line.split()
            try:
                cur_terrain = int(tok[1])
                cur_flags = int(tok[4])
                cur_planes = int(tok[5])
            except (ValueError, IndexError):
                cur_terrain, cur_flags, cur_planes = -1, 0, 0
            in_patch = True
            buf = [line]
            cur_vertices = 0
            cur_prim_types = set()
            continue
        if in_patch:
            buf.append(line)
            if line.startswith("BEGIN_PRIMITIVE"):
                tok = line.split()
                try:
                    cur_prim_types.add(int(tok[1]))
                except (ValueError, IndexError):
                    pass
            elif line.startswith("PATCH_VERTEX"):
                cur_vertices += 1
            elif line.startswith("END_PATCH"):
                in_patch = False
                if cur_vertices > max_vertices:
                    continue
                cats = _fixture_category(cur_flags, cur_prim_types)
                needed = [c for c in cats
                          if category_counts[c] < per_category]
                if needed:
                    for c in cats:
                        category_counts[c] += 1
                    captured.append(_PatchCapture(
                        terrain_index=cur_terrain,
                        flags=cur_flags,
                        planes=cur_planes,
                        primitive_types=set(cur_prim_types),
                        lines=list(buf),
                    ))
                if _is_complete():
                    break
    # We rely on all TERRAIN_DEFs preceding the first patch (true for
    # DSFTool output); terrain_paths is fully populated by now.

    _write_fixture_file(out_path, terrain_paths, captured, category_counts)
    return dict(category_counts)


def _remap_patch_header(line: str, new_index: int) -> str:
    """Rewrite a ``BEGIN_PATCH`` line's terrain index."""
    tok = line.rstrip("\n").split()
    if len(tok) >= 2:
        tok[1] = str(new_index)
    return " ".join(tok) + "\n"


def _write_fixture_file(
    out_path: str,
    terrain_paths: list[str],
    captured: list[_PatchCapture],
    category_counts: Counter,
) -> None:
    """Emit the DSFTool-text fixture: a minimal header, the re-indexed
    ``TERRAIN_DEF`` table for the referenced terrains, then each captured
    patch with its terrain reference remapped."""
    used_indices = sorted({p.terrain_index for p in captured
                           if 0 <= p.terrain_index < len(terrain_paths)})
    remap = {old: new for new, old in enumerate(used_indices)}

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write("A\n800\nDSF2TEXT\n\n")
        handle.write(
            "# Truncated DSFTool --dsf2text excerpt from an X-Plane 12\n"
            "# default Global Scenery base-mesh DSF, produced by\n"
            "# tools/probe_default_terrain.py --dump-fixture.  A few patches\n"
            "# of each (physical/overlay) x (triangle/strip/fan) category,\n"
            "# with their TERRAIN_DEF table re-indexed to this excerpt.\n\n")
        handle.write("PROPERTY sim/planet earth\n")
        handle.write("DIVISIONS 8\n\n")
        for old in used_indices:
            handle.write(f"TERRAIN_DEF {terrain_paths[old]}\n")
        handle.write("\n")
        for patch in captured:
            new_index = remap.get(patch.terrain_index, 0)
            layer = "overlay" if patch.flags & 2 else "physical"
            prims = ",".join(str(p) for p in sorted(patch.primitive_types))
            handle.write(
                f"# {layer} patch, planes={patch.planes}, "
                f"primitive types={prims}\n")
            for line in patch.lines:
                if line.startswith("BEGIN_PATCH"):
                    handle.write(_remap_patch_header(line, new_index))
                else:
                    handle.write(line if line.endswith("\n") else line + "\n")
            handle.write("\n")

        # Append a synthetic fan patch if the source had none, so the
        # fixture exercises all three primitive types.
        have_fan = any(2 in p.primitive_types for p in captured)
        if not have_fan and used_indices:
            handle.write(
                "# synthetic physical fan patch (source DSF had no fan "
                "primitives); exercises primitive type 2 for parser tests\n")
            handle.write("BEGIN_PATCH 0 0.000000 -1.000000 1 5\n")
            handle.write("BEGIN_PRIMITIVE 2\n")
            handle.write(
                "PATCH_VERTEX -135.500000 60.500000 1000.000000 "
                "0.000000 0.000000\n")
            handle.write(
                "PATCH_VERTEX -135.490000 60.500000 1000.000000 "
                "0.000000 0.000000\n")
            handle.write(
                "PATCH_VERTEX -135.490000 60.510000 1000.000000 "
                "0.000000 0.000000\n")
            handle.write(
                "PATCH_VERTEX -135.500000 60.510000 1000.000000 "
                "0.000000 0.000000\n")
            handle.write("END_PRIMITIVE\n")
            handle.write("END_PATCH\n\n")


# --------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------
def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe X-Plane default Global Scenery base-mesh terrain "
                    "assignments for a tile.")
    parser.add_argument("lat", type=int, help="tile latitude (integer)")
    parser.add_argument("lon", type=int, help="tile longitude (integer)")
    parser.add_argument("--xplane-root", default=None,
                        help="X-Plane install root (default: discover)")
    parser.add_argument("--overlay-src", default=None,
                        help="Global Scenery folder holding 'Earth nav data' "
                             "(default: custom_overlay_src / discover)")
    parser.add_argument("--dsf", default=None,
                        help="explicit path to the default DSF to probe")
    parser.add_argument(
        "--cfg", default=os.path.join(_REPO_ROOT, "Ortho4XP.cfg"),
        help="Ortho4XP.cfg to read custom_overlay_src from")
    parser.add_argument(
        "--prefs", default=os.path.join(_REPO_ROOT, ".qt_prefs.json"),
        help="Qt prefs JSON to read xplane_dir from")
    parser.add_argument("--dump-fixture", default=None, metavar="PATH",
                        help="write a truncated pytest fixture to PATH "
                             "instead of (or in addition to) reporting")
    parser.add_argument("--fixture-per-category", type=int, default=2,
                        help="patches per (layer x primitive) category in the "
                             "fixture (default 2)")
    parser.add_argument("--fixture-max-vertices", type=int, default=40,
                        help="skip patches larger than this many vertices "
                             "when building the fixture (default 40)")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    paths = resolve_install_paths(
        args.lat, args.lon,
        args.xplane_root, args.overlay_src, args.dsf,
        args.cfg, args.prefs)
    if paths is None:
        return 2

    print(f"Probing {paths.dsf_path}")
    print(f"  ({paths.source})")
    text_path = run_dsftool_to_text(paths.dsf_path)
    if text_path is None:
        return 3

    try:
        if args.dump_fixture:
            counts = dump_fixture(
                stream_lines(text_path), args.dump_fixture,
                per_category=args.fixture_per_category,
                max_vertices=args.fixture_max_vertices)
            print(f"Wrote fixture to {args.dump_fixture}")
            print(f"  captured categories (layer:primitive): {counts}")

        report = build_report(stream_lines(text_path))
        library_index = (
            _build_library_index(paths.xplane_root)
            if paths.xplane_root else {})
        ter_infos: dict[int, TerInfo] = {}
        for i, stats in enumerate(report.terrains):
            if stats.patch_count > 0:
                ter_infos[i] = inspect_ter(
                    stats.path, paths.xplane_root, library_index)
        print(render_report(paths, report, ter_infos))
    finally:
        try:
            os.unlink(text_path)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
