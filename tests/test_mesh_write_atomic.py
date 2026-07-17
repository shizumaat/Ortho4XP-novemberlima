"""``O4_Mesh_Utils.write_mesh_file`` atomicity (parallel-build race,
fixed 2026-07-16).

During parallel tile builds (``o4_engine.parallel``, one subprocess per
tile) a sibling tile's step 2.5 (``O4_Mask_Utils.build_masks`` →
``select_neighbor_meshes`` / ``record_water_tris``) reads NEIGHBOR
tiles' ``Data+XX+YYY.mesh`` files.  If the neighbor's build is still
writing that file, the reader used to see a half-written mesh and
crash (observed live: tile +36-009 step 2.5 failed while +36-008's
mesh was being written).  ``write_mesh_file`` must therefore write to
a temporary name in the same directory and ``os.replace`` it into
place, so the final path only ever holds either nothing or a complete
mesh.

Everything is hermetic under ``tmp_path``: the tile is a
``types.SimpleNamespace`` and the ``.ele`` input is a tiny hand-written
two-triangle file (same harness pattern as ``tests/test_post_mesh.py``).
"""

from __future__ import annotations

import os
import types

import O4_File_Names as FNAMES
import O4_Mesh_Utils

TILE_LATITUDE = 36
TILE_LONGITUDE = -9


def _make_tile(build_directory) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        build_dir=str(build_directory),
        lat=TILE_LATITUDE,
        lon=TILE_LONGITUDE,
        iterate=0,
    )


def _seed_ele_file(tile) -> None:
    """Write the Triangle4XP ``.ele`` output ``write_mesh_file`` reads:
    a count header, then one ``index v1 v2 v3 type`` row per triangle."""
    with open(FNAMES.output_ele_file(tile), "w") as handle:
        handle.write("2 3 1\n")
        handle.write("1 1 2 3 0\n")
        handle.write("2 1 3 4 0\n")


def _square_vertices() -> list[float]:
    """Four vertices, six floats each (longitude offset, latitude
    offset, elevation * 100000, normal x, normal y, interpolated
    elevation) — one unit square split into two triangles."""
    corners = [(0.0, 0.0), (0.001, 0.0), (0.001, 0.001), (0.0, 0.001)]
    vertices: list[float] = []
    for longitude_offset, latitude_offset in corners:
        vertices += [
            longitude_offset,
            latitude_offset,
            12345.0,
            0.0,
            0.0,
            12345.0,
        ]
    return vertices


def _assert_mesh_file_complete(mesh_path: str) -> None:
    with open(mesh_path, "r") as handle:
        lines = handle.read().splitlines()
    assert lines[0] == "MeshVersionFormatted 2"
    vertex_header_index = lines.index("Vertices")
    assert int(lines[vertex_header_index + 1]) == 4
    triangle_header_index = lines.index("Triangles")
    assert int(lines[triangle_header_index + 1]) == 2
    triangle_rows = lines[
        triangle_header_index + 2 : triangle_header_index + 4
    ]
    assert triangle_rows == ["1 2 3 0", "1 3 4 0"]


def test_write_mesh_file_renames_into_place(tmp_path, monkeypatch):
    """The mesh is written to a temporary sibling file and
    ``os.replace``d into place — the final path never receives direct
    writes, so a concurrent neighbor-tile reader can never observe a
    partial mesh there."""
    tile = _make_tile(tmp_path)
    _seed_ele_file(tile)
    final_mesh_path = FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)

    observed = {}
    real_replace = os.replace

    def spying_replace(source, destination):
        observed["source"] = source
        observed["destination"] = destination
        # The rename is the FIRST time anything appears at the final
        # path: in this fresh directory it must not exist yet.
        observed["final_path_existed_before_replace"] = os.path.exists(
            destination
        )
        # The temporary file must already be a complete mesh.
        _assert_mesh_file_complete(source)
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", spying_replace)

    O4_Mesh_Utils.write_mesh_file(tile, _square_vertices())

    assert observed, "write_mesh_file never called os.replace"
    assert observed["destination"] == final_mesh_path
    assert not observed["final_path_existed_before_replace"]
    # Same directory, different name: the rename is atomic (no
    # cross-filesystem copy) and never targets the final path directly.
    assert os.path.dirname(observed["source"]) == os.path.dirname(
        final_mesh_path
    )
    assert observed["source"] != final_mesh_path
    _assert_mesh_file_complete(final_mesh_path)
    # No temporary file left behind.
    assert not os.path.exists(observed["source"])


def test_write_mesh_file_overwrite_keeps_old_mesh_readable(
    tmp_path, monkeypatch
):
    """On a rebuild the final path holds the previous complete mesh for
    the whole duration of the write — a neighbor tile reading it at any
    moment sees either the old mesh or the new one, never a mixture."""
    tile = _make_tile(tmp_path)
    _seed_ele_file(tile)
    final_mesh_path = FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)
    stale_content = "MeshVersionFormatted 2\nstale but complete\n"
    with open(final_mesh_path, "w") as handle:
        handle.write(stale_content)

    observed = {}
    real_replace = os.replace

    def spying_replace(source, destination):
        # Right up to the swap, a reader of the final path still gets
        # the intact previous mesh.
        with open(destination, "r") as handle:
            observed["content_at_replace_time"] = handle.read()
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", spying_replace)

    O4_Mesh_Utils.write_mesh_file(tile, _square_vertices())

    assert observed["content_at_replace_time"] == stale_content
    _assert_mesh_file_complete(final_mesh_path)
