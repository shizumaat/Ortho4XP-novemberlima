"""Mapped inland water WINS over coastline sea (2026-07-17).

The Ria Formosa rebuild showed the coastline's sea flood leaking into
the lagoon through polygon rings cut at tile edges, producing WATER|SEA
triangles whose SEA bit previously won everywhere — deep-water fades
inside a mapped lagoon.  The ruling: a triangle carrying both bits
belongs to the mapper's water polygon and takes the INLAND treatment;
SEA_EQUIV (the explicit large-lake sea routing) keeps the sea class.
Guards the three consumers: the masks recorder, the DSF class remap,
and (by the shared predicate) the mesh smoothing split.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import O4_DSF_Utils as DSF  # noqa: E402
import O4_File_Names as FNAMES  # noqa: E402
import O4_Mask_Utils as MASK  # noqa: E402


class TestInlandPredicate:
    def test_pure_inland(self):
        assert MASK.water_type_is_inland(1)

    def test_water_and_sea_is_inland(self):
        assert MASK.water_type_is_inland(3)

    def test_pure_sea_is_not(self):
        assert not MASK.water_type_is_inland(2)

    def test_sea_equivalent_keeps_sea(self):
        assert not MASK.water_type_is_inland(4)
        assert not MASK.water_type_is_inland(5)  # WATER|SEA_EQUIV
        assert not MASK.water_type_is_inland(7)

    def test_land(self):
        assert not MASK.water_type_is_inland(0)


class TestDsfRemap:
    def test_classes(self):
        assert DSF.remap_water_tri_type(0, False) == 0
        assert DSF.remap_water_tri_type(1, False) == 1
        assert DSF.remap_water_tri_type(2, False) == 2
        assert DSF.remap_water_tri_type(3, False) == 1   # the ruling
        assert DSF.remap_water_tri_type(4, False) == 2
        assert DSF.remap_water_tri_type(5, False) == 2
        assert DSF.remap_water_tri_type(7, False) == 2

    def test_use_masks_for_inland_still_promotes(self):
        assert DSF.remap_water_tri_type(1, True) == 2
        assert DSF.remap_water_tri_type(3, True) == 2

    def test_pre_1_3_mesh_mask(self):
        # has_water=3: bit 4 is a non-water attribute and must not leak.
        assert DSF.remap_water_tri_type(4, False, has_water=3) == 0
        assert DSF.remap_water_tri_type(7, False, has_water=3) == 1


class _FakeTile:
    def __init__(self, build_dir):
        self.lat, self.lon = 40, 5
        self.build_dir = str(build_dir)
        self.grouped = True
        self.mask_zl = 14
        self.use_masks_for_inland = False


def _write_mesh(tile, triangle_attributes):
    """Three adjacent tiny triangles at tile centre, given attributes."""
    lat, lon = tile.lat + 0.5, tile.lon + 0.5
    vertices = [
        (lon, lat), (lon + 0.001, lat), (lon + 0.001, lat + 0.001),
        (lon, lat + 0.001), (lon + 0.002, lat),
    ]
    triangles = [(1, 2, 3), (1, 3, 4), (2, 5, 3)]
    lines = ["MeshVersionFormatted 2", "Dimension 3", "", "Vertices",
             str(len(vertices))]
    for (v_lon, v_lat) in vertices:
        lines.append("%.15f %.15f 0.001 0" % (v_lon, v_lat))
    lines += ["", "Normals", str(len(vertices))]
    lines += ["0.00 0.00 0"] * len(vertices)
    lines += ["", "Triangles", str(len(triangles))]
    for (corners, attribute) in zip(triangles, triangle_attributes):
        lines.append("%d %d %d %d" % (*corners, attribute))
    mesh_path = FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)
    os.makedirs(os.path.dirname(mesh_path), exist_ok=True)
    with open(mesh_path, "w") as mesh:
        mesh.write("\n".join(lines) + "\n")


def test_record_water_tris_routes_water_sea_to_inland(tmp_path):
    """A WATER|SEA triangle joins dico_inland (grey paint), never
    dico_sea — while pure SEA still drives the mask squares."""
    import O4_UI_Utils as UI

    UI.red_flag = False
    tile = _FakeTile(tmp_path)
    _write_mesh(tile, triangle_attributes=(2, 3, 1))
    (dico_sea, dico_inland, coastline_sea_present) = (
        MASK.record_water_tris(tile)
    )
    sea_count = sum(len(tris) for tris in dico_sea.values())
    inland_count = sum(len(tris) for tris in dico_inland.values())
    assert sea_count >= 1          # the pure-sea triangle
    assert inland_count == 2       # WATER|SEA and pure WATER
    assert coastline_sea_present   # bit 2 rules that pure-sea triangle


def test_record_water_tris_sea_equivalent_lakes_are_not_marine(tmp_path):
    """The CYXY shape (owner 2026-07-18): a mesh whose only mask-water
    is sea-EQUIVALENT lakes (bit 4, the large-lake routing) fills
    ``dico_sea`` — the lakes DO get mask squares — but reports no
    marine coastline sea, so marine-only consumers (the reef/tidal-flat
    shallow-water fallback) know to stay quiet."""
    import O4_UI_Utils as UI

    UI.red_flag = False
    tile = _FakeTile(tmp_path)
    _write_mesh(tile, triangle_attributes=(4, 4, 4))
    (dico_sea, dico_inland, coastline_sea_present) = (
        MASK.record_water_tris(tile)
    )
    assert dico_sea                       # lake squares still masked
    assert not coastline_sea_present      # ... but nothing marine
