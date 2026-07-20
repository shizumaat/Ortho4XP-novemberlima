"""Tests for tools/obj8_preview/obj8_to_html.py — the OBJ8 → HTML previewer.

Headless, no network, tmp_path-based.  A minimal valid PNG is provided as
a hardcoded bytes literal so the tests have no conditional dependency on
Pillow.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

# Make the tool importable without installing it as a package.
_TOOL_DIR = Path(__file__).resolve().parent.parent / "tools" / "obj8_preview"
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))

import obj8_to_html  # noqa: E402


# A minimal valid 1x1 red PNG (constructed with stdlib zlib/struct offline).
_MINIMAL_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _textured_quad_obj(texture_name: str = "skin.png") -> str:
    """An OBJ8 textured quad: 4 vertices, 6 indices (IDX10 partial via
    IDX lines), one TRIS range, a TEXTURE directive, and one unknown
    directive (ATTR_shiny_rat) plus a comment and blank lines."""
    return "\n".join(
        [
            "A",
            "800",
            "OBJ",
            "",
            "# a textured quad fixture",
            f"TEXTURE {texture_name}",
            "POINT_COUNTS 4 0 0 6",
            "VT -1.0 0.0 -1.0 0.0 1.0 0.0 0.0 0.0  # corner 0",
            "VT  1.0 0.0 -1.0 0.0 1.0 0.0 1.0 0.0",
            "VT  1.0 0.0  1.0 0.0 1.0 0.0 1.0 1.0",
            "VT -1.0 0.0  1.0 0.0 1.0 0.0 0.0 1.0",
            "IDX10 0 1 2 0 2 3 0 0 0 0",
            "ATTR_shiny_rat 0.5",
            "TRIS 0 6",
            "",
        ]
    )


def test_parse_textured_quad(tmp_path: Path) -> None:
    obj_path = tmp_path / "quad.obj"
    obj_path.write_text(_textured_quad_obj(), encoding="utf-8")

    parsed = obj8_to_html.parse_obj8(obj_path)

    assert len(parsed["vertices"]) == 4
    # The IDX10 line contributed 10 indices to the shared list.
    assert len(parsed["indices"]) == 10
    assert parsed["indices"][:6] == [0, 1, 2, 0, 2, 3]
    assert parsed["tris_ranges"] == [[0, 6]]
    assert parsed["texture"] == "skin.png"
    assert "ATTR_shiny_rat" in parsed["warnings"]
    # First vertex fully parsed as 8 floats.
    assert parsed["vertices"][0] == [-1.0, 0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0]


def test_parse_with_single_idx_lines(tmp_path: Path) -> None:
    """IDX10 + IDX accumulate into one shared list in file order."""
    obj_text = "\n".join(
        [
            "I",
            "800",
            "OBJ",
            "TEXTURE t.png",
            "POINT_COUNTS 4 0 0 6",
            "VT 0 0 0 0 1 0 0 0",
            "VT 1 0 0 0 1 0 1 0",
            "VT 1 0 1 0 1 0 1 1",
            "VT 0 0 1 0 1 0 0 1",
            "IDX 0",
            "IDX 1",
            "IDX 2",
            "IDX 0",
            "IDX 2",
            "IDX 3",
            "TRIS 0 6",
        ]
    )
    obj_path = tmp_path / "quad2.obj"
    obj_path.write_text(obj_text, encoding="utf-8")
    parsed = obj8_to_html.parse_obj8(obj_path)
    assert parsed["indices"] == [0, 1, 2, 0, 2, 3]
    assert parsed["tris_ranges"] == [[0, 6]]


def test_point_counts_mismatch_is_warning_not_error(tmp_path: Path) -> None:
    obj_text = "\n".join(
        [
            "A",
            "800",
            "OBJ",
            "POINT_COUNTS 99 0 0 6",  # 99 declared, 4 actual
            "VT 0 0 0 0 1 0 0 0",
            "VT 1 0 0 0 1 0 1 0",
            "VT 1 0 1 0 1 0 1 1",
            "VT 0 0 1 0 1 0 0 1",
            "IDX10 0 1 2 0 2 3 0 0 0 0",
            "TRIS 0 6",
        ]
    )
    obj_path = tmp_path / "mismatch.obj"
    obj_path.write_text(obj_text, encoding="utf-8")

    parsed = obj8_to_html.parse_obj8(obj_path)  # must NOT raise
    assert any("POINT_COUNTS" in warning for warning in parsed["warnings"])


def test_out_of_range_index_raises(tmp_path: Path) -> None:
    obj_text = "\n".join(
        [
            "A",
            "800",
            "OBJ",
            "POINT_COUNTS 3 0 0 3",
            "VT 0 0 0 0 1 0 0 0",
            "VT 1 0 0 0 1 0 1 0",
            "VT 1 0 1 0 1 0 1 1",
            "IDX10 0 1 5 0 0 0 0 0 0 0",  # index 5 out of range (3 verts)
            "TRIS 0 3",
        ]
    )
    obj_path = tmp_path / "bad.obj"
    obj_path.write_text(obj_text, encoding="utf-8")

    with pytest.raises(ValueError):
        obj8_to_html.parse_obj8(obj_path)


def test_first_lod_block_only(tmp_path: Path) -> None:
    """TRIS from a second ATTR_LOD block are dropped."""
    obj_text = "\n".join(
        [
            "A",
            "800",
            "OBJ",
            "POINT_COUNTS 4 0 0 6",
            "VT 0 0 0 0 1 0 0 0",
            "VT 1 0 0 0 1 0 1 0",
            "VT 1 0 1 0 1 0 1 1",
            "VT 0 0 1 0 1 0 0 1",
            "IDX10 0 1 2 0 2 3 0 0 0 0",
            "ATTR_LOD 0 1000",
            "TRIS 0 3",
            "ATTR_LOD 1000 5000",
            "TRIS 3 3",  # second LOD block — must be dropped
        ]
    )
    obj_path = tmp_path / "lod.obj"
    obj_path.write_text(obj_text, encoding="utf-8")
    parsed = obj8_to_html.parse_obj8(obj_path)
    assert parsed["tris_ranges"] == [[0, 3]]
    # ATTR_LOD is a handled directive, not a warning.
    assert "ATTR_LOD" not in parsed["warnings"]


def test_generate_html_embeds_geometry_and_texture(tmp_path: Path) -> None:
    obj_path = tmp_path / "quad.obj"
    obj_path.write_text(_textured_quad_obj("skin.png"), encoding="utf-8")
    (tmp_path / "skin.png").write_bytes(_MINIMAL_PNG_BYTES)
    output_path = tmp_path / "out.html"

    exit_code = obj8_to_html.main(
        [str(obj_path), "-o", str(output_path)]
    )
    assert exit_code == 0

    html = output_path.read_text(encoding="utf-8")

    # Embedded vertex positions present.
    assert '"positions"' in html
    assert "-1.0" in html
    # Texture embedded as a base64 PNG data URI.
    expected_b64 = base64.b64encode(_MINIMAL_PNG_BYTES).decode("ascii")
    assert f"data:image/png;base64,{expected_b64}" in html
    # three.js CDN reference present.
    assert "three@0.160.0/build/three.min.js" in html
    # Stats line reflects the geometry.
    assert "vertices: 4" in html
    assert "triangles: 2" in html


def test_generate_html_reverses_triangle_winding(tmp_path: Path) -> None:
    """Parsed indices [0,1,2,0,2,3] must appear reversed per triangle in
    the embedded HTML index list: [0,2,1,0,3,2]."""
    obj_path = tmp_path / "quad.obj"
    obj_path.write_text(_textured_quad_obj("skin.png"), encoding="utf-8")
    output_path = tmp_path / "out.html"
    obj8_to_html.main([str(obj_path), "-o", str(output_path)])
    html = output_path.read_text(encoding="utf-8")

    # Parser stays in raw file order.
    parsed = obj8_to_html.parse_obj8(obj_path)
    assert parsed["indices"][:6] == [0, 1, 2, 0, 2, 3]

    # HTML embeds the per-triangle-reversed list.
    assert '"indices": [0, 2, 1, 0, 3, 2]' in html


def test_missing_texture_still_produces_html_with_warning(tmp_path: Path) -> None:
    obj_path = tmp_path / "quad.obj"
    obj_path.write_text(_textured_quad_obj("does_not_exist.png"), encoding="utf-8")
    output_path = tmp_path / "out.html"

    exit_code = obj8_to_html.main([str(obj_path), "-o", str(output_path)])
    assert exit_code == 0

    html = output_path.read_text(encoding="utf-8")
    assert "texture not found" in html
    # No data URI should be embedded for the missing texture.
    assert '"texture": null' in html


def test_texture_override(tmp_path: Path) -> None:
    """--texture overrides the TEXTURE directive path."""
    obj_path = tmp_path / "quad.obj"
    obj_path.write_text(_textured_quad_obj("skin.png"), encoding="utf-8")
    override_path = tmp_path / "override.png"
    override_path.write_bytes(_MINIMAL_PNG_BYTES)
    output_path = tmp_path / "out.html"

    obj8_to_html.main(
        [str(obj_path), "-o", str(output_path), "--texture", str(override_path)]
    )
    html = output_path.read_text(encoding="utf-8")
    expected_b64 = base64.b64encode(_MINIMAL_PNG_BYTES).decode("ascii")
    assert f"data:image/png;base64,{expected_b64}" in html


def test_parse_dsf_text_placements_handles_elevated_rows() -> None:
    """OBJECT_AGL / OBJECT_MSL rows parse with their altitude; a plain
    OBJECT row reads as AGL 0."""
    dsf_text = "\n".join(
        [
            "PROPERTY sim/overlay 1",
            "OBJECT_DEF objects/alpha.obj",
            "OBJECT_DEF objects/bravo.obj",
            "OBJECT 0 -121.161000000 44.254000000 90.000000",
            "OBJECT_AGL 1 -121.160000000 44.255000000 180.000000 16.250",
            "OBJECT_MSL 0 -121.159000000 44.256000000 45.000000 938.000",
        ]
    )
    placements = obj8_to_html.parse_dsf_text_placements(dsf_text)
    assert len(placements) == 3

    plain, agl, msl = placements
    assert plain["object_relative_path"] == "objects/alpha.obj"
    assert plain["altitude_meters"] == 0.0
    assert plain["is_above_ground"] is True

    assert agl["object_relative_path"] == "objects/bravo.obj"
    assert agl["altitude_meters"] == 16.25
    assert agl["is_above_ground"] is True

    assert msl["object_relative_path"] == "objects/alpha.obj"
    assert msl["altitude_meters"] == 938.0
    assert msl["is_above_ground"] is False
