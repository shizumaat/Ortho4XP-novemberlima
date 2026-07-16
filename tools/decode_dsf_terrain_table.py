#!/usr/bin/env python3
"""Decode an emitted DSF's terrain-definition table and per-patch summary.

Runs the bundled ``DSFTool --dsf2text`` on a ``.dsf`` file (7z handled by
DSFTool) and prints:

  * the ``TERRAIN_DEF`` table in index order, and
  * a per-patch line of ``(terrain index, terrain path, flags, plane count,
    triangle count)``.

It is the verification companion to the ``texture_mode`` writer work
(``docs/specs/texture-mode-spec.md``, work package 2): tests use
:func:`decode_dsf` to assert an emitted DSF's terrain table and patch
attributes; a human can run it from the command line on any DSF.

The DSFTool location and the ``--dsf2text`` conversion cache are reused from
``src/auto_patch/dsf_reader.py`` (``_dsftool_path`` / ``ensure_dsf_text_path``)
so this tool honours the same binary discovery and mtime-keyed text cache as
the rest of the pipeline.

Usage::

    python tools/decode_dsf_terrain_table.py <path/to/tile.dsf>

Exit status is non-zero when DSFTool is unavailable or the DSF cannot be
converted.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import NamedTuple

# Make ``src`` importable when run as a standalone script.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import O4_File_Names as FNAMES
from auto_patch.dsf_reader import _dsftool_path, ensure_dsf_text_path


class PatchSummary(NamedTuple):
    """One physical/overlay terrain patch decoded from a DSF text dump."""

    terrain_index: int
    terrain_path: str
    flags: int
    plane_count: int
    triangle_count: int


class DsfTerrainDump(NamedTuple):
    """Decoded terrain table and patch list for a single DSF."""

    terrain_paths: list
    patches: list  # list[PatchSummary]


def _primitive_triangle_count(primitive_type: int, vertex_count: int) -> int:
    """Number of triangles a primitive of ``vertex_count`` vertices yields.

    Type 0 = independent triangles (``n // 3``); types 1 (strip) and 2 (fan)
    both yield ``n - 2`` triangles (0 when fewer than three vertices).
    """
    if primitive_type == 0:
        return vertex_count // 3
    if primitive_type in (1, 2):
        return max(0, vertex_count - 2)
    return 0


def decode_text_lines(lines) -> DsfTerrainDump:
    """Decode an iterable of DSFTool ``--dsf2text`` lines.

    Grammar (mirrors ``O4_Default_Terrain_Map``)::

        TERRAIN_DEF <path>
        BEGIN_PATCH <terrainIdx> <nearLOD> <farLOD> <flags> <coordDepth>
        BEGIN_PRIMITIVE <0|1|2>
        PATCH_VERTEX <lon> <lat> ...
        END_PRIMITIVE
        END_PATCH
    """
    terrain_paths: list = []
    patches: list = []

    patch_terrain_index = -1
    patch_flags = 0
    patch_plane_count = 0
    patch_triangle_count = 0
    primitive_type = -1
    primitive_vertex_count = 0

    def _flush_primitive() -> None:
        nonlocal patch_triangle_count
        if primitive_type >= 0:
            patch_triangle_count += _primitive_triangle_count(
                primitive_type, primitive_vertex_count)

    for raw in lines:
        if raw.startswith("PATCH_VERTEX"):
            primitive_vertex_count += 1
            continue
        if raw.startswith("TERRAIN_DEF"):
            tokens = raw.strip().split(maxsplit=1)
            terrain_paths.append(
                tokens[1].strip() if len(tokens) > 1 else "")
            continue
        if raw.startswith("BEGIN_PATCH"):
            tokens = raw.split()
            try:
                patch_terrain_index = int(tokens[1])
                patch_flags = int(tokens[4])
                patch_plane_count = int(tokens[5])
            except (IndexError, ValueError):
                patch_terrain_index = -1
                patch_flags = 0
                patch_plane_count = 0
            patch_triangle_count = 0
            primitive_type = -1
            primitive_vertex_count = 0
            continue
        if raw.startswith("BEGIN_PRIMITIVE"):
            tokens = raw.split()
            try:
                primitive_type = int(tokens[1])
            except (IndexError, ValueError):
                primitive_type = -1
            primitive_vertex_count = 0
            continue
        if raw.startswith("END_PRIMITIVE"):
            _flush_primitive()
            primitive_type = -1
            primitive_vertex_count = 0
            continue
        if raw.startswith("END_PATCH"):
            if primitive_type >= 0 and primitive_vertex_count:
                _flush_primitive()
            path = (
                terrain_paths[patch_terrain_index]
                if 0 <= patch_terrain_index < len(terrain_paths)
                else "")
            patches.append(PatchSummary(
                terrain_index=patch_terrain_index,
                terrain_path=path,
                flags=patch_flags,
                plane_count=patch_plane_count,
                triangle_count=patch_triangle_count,
            ))
            patch_terrain_index = -1
            patch_flags = 0
            patch_plane_count = 0
            patch_triangle_count = 0
            primitive_type = -1
            primitive_vertex_count = 0
            continue

    return DsfTerrainDump(terrain_paths=terrain_paths, patches=patches)


def decode_dsf(dsf_path: str) -> DsfTerrainDump:
    """Run DSFTool on ``dsf_path`` and decode its terrain table + patches.

    Raises ``FileNotFoundError`` if DSFTool is unavailable or the DSF cannot
    be converted to text.  The text dump (and DSFTool's ``.raw`` raster
    sidecars) go to ``FNAMES.Default_dsf_cache_dir`` — never next to the
    DSF, which may live inside a scenery pack that ships to X-Plane.
    """
    os.makedirs(FNAMES.Default_dsf_cache_dir, exist_ok=True)
    text_path = ensure_dsf_text_path(
        dsf_path, cache_dir=FNAMES.Default_dsf_cache_dir)
    if text_path is None:
        raise FileNotFoundError(
            f"Could not produce a DSFTool text dump for {dsf_path!r} "
            "(missing DSF, missing DSFTool binary, or conversion error).")
    with open(text_path, "r", encoding="utf-8", errors="replace") as handle:
        return decode_text_lines(handle)


def dsftool_available() -> bool:
    """True when the bundled DSFTool binary is present (for test skips)."""
    return _dsftool_path() is not None


def _format_report(dump: DsfTerrainDump) -> str:
    out = ["TERRAIN_DEF table ({} entries):".format(len(dump.terrain_paths))]
    for index, path in enumerate(dump.terrain_paths):
        out.append("  [{:>3}] {}".format(index, path))
    out.append("")
    out.append("Patches ({} total):".format(len(dump.patches)))
    out.append("  {:>5}  {:>5}  {:>5}  {:>6}  {}".format(
        "idx", "flag", "plane", "tris", "terrain"))
    for patch in dump.patches:
        out.append("  {:>5}  {:>5}  {:>5}  {:>6}  {}".format(
            patch.terrain_index, patch.flags, patch.plane_count,
            patch.triangle_count, patch.terrain_path))
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dsf_path", help="path to a .dsf file to decode")
    args = parser.parse_args(argv)

    if not dsftool_available():
        print("ERROR: bundled DSFTool binary not found; cannot decode.",
              file=sys.stderr)
        return 2
    try:
        dump = decode_dsf(args.dsf_path)
    except FileNotFoundError as exc:
        print("ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print(_format_report(dump))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
