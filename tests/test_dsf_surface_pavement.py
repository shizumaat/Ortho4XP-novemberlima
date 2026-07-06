"""SURFACE-attribute classification of DSF draped pavement polygons.

A ``.pol`` resource declaring ``SURFACE asphalt|concrete`` is pavement
regardless of its NAME; a declared soft surface vetoes a material-token
name; no SURFACE falls back to the name heuristics.
"""
import os

import pytest

from auto_patch import dsf_reader as DSFR


def _write_pol(root, relative_path, surface=None):
    path = os.path.join(root, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = ["A", "850", "DRAPED_POLYGON", "",
             "TEXTURE_NOWRAP tex.png", "SCALE 25 25"]
    if surface is not None:
        lines.append(f"SURFACE {surface}")
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


@pytest.fixture(autouse=True)
def _clear_surface_cache():
    DSFR._surface_attribute_cache.clear()
    yield
    DSFR._surface_attribute_cache.clear()


class TestSurfaceClassification:
    def test_hard_surface_admits_neutral_name(self, tmp_path):
        """SURFACE asphalt makes a neutrally-named .pol pavement — the
        exact class the name filter misses."""
        _write_pol(str(tmp_path), "textures/tarmac_dark.pol", "asphalt")
        assert not DSFR._is_pavement_def("textures/tarmac_dark.pol")
        assert DSFR._classify_pavement_def(
            "textures/tarmac_dark.pol", str(tmp_path), None)

    def test_concrete_surface_admits(self, tmp_path):
        _write_pol(str(tmp_path), "ground/slab_a.pol", "concrete")
        assert DSFR._classify_pavement_def(
            "ground/slab_a.pol", str(tmp_path), None)

    def test_soft_surface_vetoes_material_name(self, tmp_path):
        """A declared soft surface beats a pavement-sounding name."""
        _write_pol(str(tmp_path), "ground/concrete_apron.pol", "grass")
        assert DSFR._is_pavement_def("ground/concrete_apron.pol")
        assert not DSFR._classify_pavement_def(
            "ground/concrete_apron.pol", str(tmp_path), None)

    @pytest.mark.parametrize("soft", ["dirt", "gravel", "lakebed",
                                      "snow", "water"])
    def test_soft_surfaces_all_veto(self, tmp_path, soft):
        _write_pol(str(tmp_path), f"ground/asphalt_{soft}.pol", soft)
        assert not DSFR._classify_pavement_def(
            f"ground/asphalt_{soft}.pol", str(tmp_path), None)

    def test_no_surface_falls_back_to_name(self, tmp_path):
        _write_pol(str(tmp_path), "ground/concrete_pad.pol")
        _write_pol(str(tmp_path), "ground/mystery_pad.pol")
        assert DSFR._classify_pavement_def(
            "ground/concrete_pad.pol", str(tmp_path), None)
        assert not DSFR._classify_pavement_def(
            "ground/mystery_pad.pol", str(tmp_path), None)

    def test_unresolvable_falls_back_to_name(self):
        """No pack root, no library — stock names still classify."""
        assert DSFR._classify_pavement_def(
            "lib/airport/pavement/asphalt_1D.pol", None, None)
        assert not DSFR._classify_pavement_def(
            "lib/airport/markings/DrapedRwySigns.pol", None, None)

    def test_library_virtual_path_resolution(self, tmp_path, monkeypatch):
        """lib/… virtual paths resolve through the library.txt map."""
        physical = _write_pol(str(tmp_path), "phys/surfaced.pol",
                              "concrete")
        from auto_patch import agp_reader
        monkeypatch.setattr(
            agp_reader, "resolve_library_path",
            lambda virtual, root: physical
            if virtual == "lib/somepack/neutral_name.pol" else None)
        assert DSFR._classify_pavement_def(
            "lib/somepack/neutral_name.pol", None, "/fake/xplane")

    def test_gate_off_restores_name_behavior(self, tmp_path, monkeypatch):
        from auto_patch import config
        monkeypatch.setattr(config, "DSF_SURFACE_POLYGONS", False)
        _write_pol(str(tmp_path), "textures/tarmac_dark.pol", "asphalt")
        assert not DSFR._classify_pavement_def(
            "textures/tarmac_dark.pol", str(tmp_path), None)

    def test_decorative_overlay_with_hard_surface_stays_rejected(
            self, tmp_path):
        """Painted overlays (signs, safety-area stripes, lines) declare
        the surface they sit ON — SURFACE concrete on a sign patch must
        not mint pavement islands (KCLT DrapedRwySigns class)."""
        for name in ("lib/airport/markings/DrapedRwySigns.pol",
                     "lib/airport/lines/safety_area_red.pol",
                     "objectfede/lines/red_grid.pol"):
            _write_pol(str(tmp_path), name, "concrete")
            assert not DSFR._classify_pavement_def(
                name, str(tmp_path), None), name

    def test_non_pol_resources_never_surface_classified(self, tmp_path):
        """.for/.fac/.lin resources have no SURFACE semantics — always
        name-filtered (and thus rejected)."""
        assert not DSFR._classify_pavement_def(
            "lib/vegetation/forests/broadleaves/warm.for",
            str(tmp_path), None)
        assert not DSFR._classify_pavement_def(
            "lib/airport/Common_Elements/Fence_Facades/Fence.fac",
            str(tmp_path), None)

    def test_pack_root_derivation(self, tmp_path):
        dsf = os.path.join(str(tmp_path), "Earth nav data",
                           "+30-090", "+35-081.dsf")
        os.makedirs(os.path.dirname(dsf), exist_ok=True)
        open(dsf, "w").close()
        assert DSFR._pack_root_for_dsf(dsf) == str(tmp_path)
