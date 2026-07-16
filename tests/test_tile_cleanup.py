"""Tests for the tile cleanup phase in O4_Tile_Utils.

Covers the terrain-name → texture-name derivation used by
``remove_unwanted_textures`` (including the ``_overlay`` names emitted by
the airport_ortho texture mode) and the DSFTool-dump leftover sweep.
Headless: stub tile objects over ``tmp_path``, no network, no X-Plane.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import O4_Tile_Utils as TILE


class _StubTile:
    def __init__(self, build_dir: str) -> None:
        self.build_dir = build_dir


def _make_tile(tmp_path, terrain_files, texture_files):
    (tmp_path / "terrain").mkdir()
    (tmp_path / "textures").mkdir()
    for name in terrain_files:
        (tmp_path / "terrain" / name).write_text("A\n800\nTERRAIN\n")
    for name in texture_files:
        (tmp_path / "textures" / name).write_bytes(b"x")
    return _StubTile(str(tmp_path))


def test_overlay_terrain_names_keep_their_texture(tmp_path):
    """airport_ortho land overlays (<tex>_overlay.ter) must keep <tex>.dds."""
    tile = _make_tile(
        tmp_path,
        terrain_files=[
            "18720_8144_Arc16.ter",
            "18720_8144_Arc16_overlay.ter",
            "18736_8160_Arc16_water_overlay.ter",
            "18752_8160_Arc16_sea.ter",
        ],
        texture_files=[
            "18720_8144_Arc16.dds",
            "18736_8160_Arc16.dds",
            "18752_8160_Arc16.dds",
            "99999_9999_Arc16.dds",  # orphan: no terrain references it
        ],
    )
    TILE.remove_unwanted_textures(tile)
    kept = sorted(os.listdir(tmp_path / "textures"))
    assert kept == [
        "18720_8144_Arc16.dds",
        "18736_8160_Arc16.dds",
        "18752_8160_Arc16.dds",
    ]


def test_fade_mask_pngs_are_never_removed(tmp_path):
    tile = _make_tile(
        tmp_path,
        terrain_files=["18720_8144_Arc16_overlay.ter"],
        texture_files=["18720_8144_Arc16.dds"],
    )
    fade = tmp_path / "textures" / "18720_8144_Arc16_airport_fade.png"
    fade.write_bytes(b"png")
    TILE.remove_unwanted_textures(tile)
    assert fade.exists()


def test_missing_dirs_are_a_no_op(tmp_path):
    """default_xplane tiles may have no terrain/ or textures/ at all."""
    TILE.remove_unwanted_textures(_StubTile(str(tmp_path)))


def test_dsftool_dump_leftovers_are_swept(tmp_path):
    nav = tmp_path / "Earth nav data" / "+60-140"
    nav.mkdir(parents=True)
    dsf = nav / "+60-136.dsf"
    dsf.write_bytes(b"XPLNEDSF")
    (nav / "+60-136.dsf.text").write_text("dump")
    (nav / "+60-136.dsf.text.elevation.raw").write_bytes(b"r")
    (nav / "+60-136.dsf.text.sea_level.raw").write_bytes(b"r")
    TILE.remove_dsftool_dump_leftovers(_StubTile(str(tmp_path)))
    assert sorted(os.listdir(nav)) == ["+60-136.dsf"]
