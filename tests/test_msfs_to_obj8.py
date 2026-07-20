"""Headless tests for the MSFS glTF/GLB -> X-Plane OBJ8 converter.

Constructs a minimal valid ``.glb`` in-memory (a two-triangle quad, one
material with a tiny embedded PNG texture, a node with a translation and a
90-degree rotation, plus a second primitive marked with JOINTS_0 that must be
skipped) and exercises the reader and the converter.  No network, no
X-Plane install, ``tmp_path``-based only.
"""
from __future__ import annotations

import io
import json
import math
import struct
from pathlib import Path
from typing import List, Tuple

import pytest

from tools.msfs_to_obj8 import convert as convert_module
from tools.msfs_to_obj8 import gltf_reader
from tools.msfs_to_obj8 import material_fidelity


# --------------------------------------------------------------------------
# GLB construction helpers.
# --------------------------------------------------------------------------
def _tiny_png_bytes() -> bytes:
    from PIL import Image

    image = Image.new("RGBA", (2, 2), (10, 20, 30, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_test_glb() -> bytes:
    """Assemble a minimal GLB with one quad + one skinned primitive."""
    parts: List[bytes] = []
    views: List[Tuple[int, int]] = []

    def add_view(data: bytes) -> int:
        while sum(len(part) for part in parts) % 4 != 0:
            parts.append(b"\x00")
        offset = sum(len(part) for part in parts)
        parts.append(data)
        views.append((offset, len(data)))
        return len(views) - 1

    positions_main = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                      (1.0, 0.0, 1.0), (0.0, 0.0, 1.0)]
    normals_main = [(0.0, 1.0, 0.0)] * 4
    uvs_main = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    indices_main = [0, 1, 2, 0, 2, 3]

    positions_joints = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)]
    joints_values = [(0, 0, 0, 0)] * 3
    indices_joints = [0, 1, 2]

    view_position_main = add_view(
        b"".join(struct.pack("<3f", *p) for p in positions_main)
    )
    view_normal_main = add_view(
        b"".join(struct.pack("<3f", *n) for n in normals_main)
    )
    view_uv_main = add_view(
        b"".join(struct.pack("<2f", *t) for t in uvs_main)
    )
    view_indices_main = add_view(
        b"".join(struct.pack("<H", i) for i in indices_main)
    )
    view_position_joints = add_view(
        b"".join(struct.pack("<3f", *p) for p in positions_joints)
    )
    view_joints = add_view(
        b"".join(struct.pack("<4H", *j) for j in joints_values)
    )
    view_indices_joints = add_view(
        b"".join(struct.pack("<H", i) for i in indices_joints)
    )
    png_bytes = _tiny_png_bytes()
    view_image = add_view(png_bytes)

    binary_blob = b"".join(parts)

    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{
            "mesh": 0,
            "translation": [10.0, 20.0, 30.0],
            # 90-degree rotation about the +Y axis (x, y, z, w).
            "rotation": [0.0, math.sin(math.pi / 4), 0.0, math.cos(math.pi / 4)],
        }],
        "meshes": [{
            "primitives": [
                {
                    "attributes": {
                        "POSITION": 0,
                        "NORMAL": 1,
                        "TEXCOORD_0": 2,
                    },
                    "indices": 3,
                    "material": 0,
                },
                {
                    "attributes": {
                        "POSITION": 4,
                        "JOINTS_0": 5,
                    },
                    "indices": 6,
                    "material": 0,
                },
            ]
        }],
        "materials": [{
            "name": "TerminalWall",
            "pbrMetallicRoughness": {
                "baseColorTexture": {"index": 0},
            },
        }],
        "textures": [{"source": 0}],
        "images": [{"name": "wall.png", "bufferView": view_image,
                    "mimeType": "image/png"}],
        "accessors": [
            {"bufferView": view_position_main, "componentType": 5126,
             "count": 4, "type": "VEC3"},
            {"bufferView": view_normal_main, "componentType": 5126,
             "count": 4, "type": "VEC3"},
            {"bufferView": view_uv_main, "componentType": 5126,
             "count": 4, "type": "VEC2"},
            {"bufferView": view_indices_main, "componentType": 5123,
             "count": 6, "type": "SCALAR"},
            {"bufferView": view_position_joints, "componentType": 5126,
             "count": 3, "type": "VEC3"},
            {"bufferView": view_joints, "componentType": 5123,
             "count": 3, "type": "VEC4"},
            {"bufferView": view_indices_joints, "componentType": 5123,
             "count": 3, "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": offset, "byteLength": length}
            for offset, length in views
        ],
        "buffers": [{"byteLength": len(binary_blob)}],
    }

    json_bytes = json.dumps(document).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "
    padded_binary = binary_blob
    while len(padded_binary) % 4 != 0:
        padded_binary += b"\x00"

    total_length = 12 + 8 + len(json_bytes) + 8 + len(padded_binary)
    glb = bytearray()
    glb += struct.pack("<III", 0x46546C67, 2, total_length)
    glb += struct.pack("<II", len(json_bytes), 0x4E4F534A)
    glb += json_bytes
    glb += struct.pack("<II", len(padded_binary), 0x004E4942)
    glb += padded_binary
    return bytes(glb)


def _expected_world_position(
    point: Tuple[float, float, float]
) -> Tuple[float, float, float]:
    """Apply the same T*R (90 deg about Y, then translate) as the test node."""
    x, y, z = point
    # 90-degree rotation about Y: x' = z, z' = -x.
    rotated = (z, y, -x)
    return (rotated[0] + 10.0, rotated[1] + 20.0, rotated[2] + 30.0)


# --------------------------------------------------------------------------
# OBJ8 parsing helpers.
# --------------------------------------------------------------------------
def _parse_obj_vt(obj_text: str) -> List[Tuple[float, ...]]:
    vertices: List[Tuple[float, ...]] = []
    for line in obj_text.splitlines():
        if line.startswith("VT "):
            values = [float(token) for token in line.split()[1:]]
            vertices.append(tuple(values))
    return vertices


def _parse_obj_indices(obj_text: str) -> List[int]:
    indices: List[int] = []
    for line in obj_text.splitlines():
        if line.startswith("IDX10 "):
            indices.extend(int(token) for token in line.split()[1:])
        elif line.startswith("IDX "):
            indices.append(int(line.split()[1]))
    return indices


# --------------------------------------------------------------------------
# Tests.
# --------------------------------------------------------------------------
def test_parser_decodes_geometry_under_transform(tmp_path: Path) -> None:
    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_test_glb())

    parsed = gltf_reader.parse_gltf(glb_path)

    # The skinned primitive is dropped, so only the quad remains.
    renderable = [p for p in parsed["primitives"]]
    assert len(renderable) == 1
    primitive = renderable[0]

    assert primitive["indices"] == [0, 1, 2, 0, 2, 3]
    assert primitive["material"] == 0

    expected_positions = [
        _expected_world_position(p)
        for p in [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)]
    ]
    for got, expected in zip(primitive["positions"], expected_positions):
        assert got == pytest.approx(list(expected), abs=1e-5)

    # UVs are decoded verbatim (v-flip happens in the converter, not here).
    expected_uvs = [[0, 0], [1, 0], [1, 1], [0, 1]]
    for got_uv, expected_uv in zip(primitive["texcoords"], expected_uvs):
        assert got_uv == pytest.approx(expected_uv, abs=1e-6)

    # A skinned-primitive warning was recorded.
    assert any("skinned mesh skipped" in w for w in parsed["warnings"])


def test_converter_emits_obj_and_png(tmp_path: Path) -> None:
    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_test_glb())
    output_directory = tmp_path / "out"

    manifest = convert_module.convert(glb_path, output_directory, base_name="krdm")

    obj_files = list(output_directory.glob("*.obj"))
    png_files = list(output_directory.glob("*.png"))
    assert len(obj_files) == 1
    assert len(png_files) == 1

    assert len(manifest["objects"]) == 1
    entry = manifest["objects"][0]
    # Objects group by TEXTURE (one obj per texture = X-Plane batching
    # optimum); the group is named after the texture, not the material.
    assert entry["material"] == "wall"
    assert entry["triangles"] == 2
    assert entry["texture"] == png_files[0].name

    obj_text = obj_files[0].read_text()
    assert "TEXTURE " in obj_text
    assert "POINT_COUNTS 4 0 0 6" in obj_text


def test_converter_rotates_axes_and_flips_v(tmp_path: Path) -> None:
    # MSFS -> X-Plane is a 180-degree rotation about Y (fit to the KRDM
    # OSM footprint at 2.8 m mean error, 2026-07-19): negate x and z.
    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_test_glb())
    output_directory = tmp_path / "out"
    convert_module.convert(glb_path, output_directory, base_name="krdm")

    obj_text = next(output_directory.glob("*.obj")).read_text()
    vertices = _parse_obj_vt(obj_text)
    assert len(vertices) == 4

    original_uvs = [(0, 0), (1, 0), (1, 1), (0, 1)]
    original_points = [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)]
    for index, vertex in enumerate(vertices):
        px, py, pz, nx, ny, nz, u, v = vertex
        world = _expected_world_position(original_points[index])
        assert px == pytest.approx(-world[0], abs=1e-4)
        assert py == pytest.approx(world[1], abs=1e-4)
        assert pz == pytest.approx(-world[2], abs=1e-4)
        # V is flipped: v_obj8 = 1 - v_gltf.
        assert u == pytest.approx(original_uvs[index][0], abs=1e-4)
        assert v == pytest.approx(1.0 - original_uvs[index][1], abs=1e-4)
        assert (nx, ny, nz) == pytest.approx((0.0, 1.0, 0.0), abs=1e-4)


def test_manifest_records_horizontal_bounds(tmp_path: Path) -> None:
    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_test_glb())
    output_directory = tmp_path / "out"
    manifest = convert_module.convert(glb_path, output_directory, base_name="krdm")

    entry = manifest["objects"][0]
    # World quad corners map to OBJ8 x in [-11, -10], z in [-30, -29]
    # (the test node's rotation + translation, then the axis map).
    assert entry["bounds_xz"] == pytest.approx([-11.0, -30.0, -10.0, -29.0],
                                               abs=1e-3)


def test_index_order_is_preserved_for_spec_gltf_sources(tmp_path: Path) -> None:
    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_test_glb())
    output_directory = tmp_path / "out"
    convert_module.convert(
        glb_path, output_directory, base_name="krdm", winding="gltf"
    )

    obj_text = next(output_directory.glob("*.obj")).read_text()
    indices = _parse_obj_indices(obj_text)
    # Identity axes preserve winding, so a (declared) spec-CCW source
    # must be reversed to reach OBJ8's CW-front convention.
    assert indices == [0, 2, 1, 0, 3, 2]


def test_auto_winding_reverses_directx_wound_sources(tmp_path: Path) -> None:
    # The synthetic fixture is authored CW-front relative to its normals
    # (DirectX convention, like glTF blobs carved from compiled MSFS
    # BGLs), so auto-detection must classify it as directx and reverse
    # each triangle to keep the OBJ8 output CW-front.
    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_test_glb())
    output_directory = tmp_path / "out"
    manifest = convert_module.convert(glb_path, output_directory, base_name="krdm")

    obj_text = next(output_directory.glob("*.obj")).read_text()
    indices = _parse_obj_indices(obj_text)
    # CW-front (DirectX) sources already match OBJ8 CW-front under the
    # identity axis map: index order is preserved.
    assert indices == [0, 1, 2, 0, 2, 3]
    assert any("winding auto-detected: directx" in w for w in manifest["warnings"])


def _build_mirrored_instances_glb() -> bytes:
    """One CW-front (DirectX) quad mesh instanced by three nodes: one
    identity, two mirrored (scale [-1, 1, 1]) — the mirrored instances are
    the MAJORITY, which defeats a whole-file winding vote that ignores the
    node determinant sign."""
    positions = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                 (1.0, 0.0, 1.0), (0.0, 0.0, 1.0)]
    normals = [(0.0, 1.0, 0.0)] * 4
    indices = [0, 1, 2, 0, 2, 3]

    parts: List[bytes] = []
    views: List[Tuple[int, int]] = []

    def add_view(data: bytes) -> int:
        while sum(len(part) for part in parts) % 4 != 0:
            parts.append(b"\x00")
        offset = sum(len(part) for part in parts)
        parts.append(data)
        views.append((offset, len(data)))
        return len(views) - 1

    view_positions = add_view(
        b"".join(struct.pack("<3f", *p) for p in positions)
    )
    view_normals = add_view(
        b"".join(struct.pack("<3f", *n) for n in normals)
    )
    view_indices = add_view(
        b"".join(struct.pack("<H", i) for i in indices)
    )
    binary_blob = b"".join(parts)

    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 1, 2]}],
        "nodes": [
            {"mesh": 0},
            {"mesh": 0, "scale": [-1.0, 1.0, 1.0],
             "translation": [5.0, 0.0, 0.0]},
            {"mesh": 0, "scale": [-1.0, 1.0, 1.0],
             "translation": [10.0, 0.0, 0.0]},
        ],
        "meshes": [{
            "primitives": [{
                "attributes": {"POSITION": 0, "NORMAL": 1},
                "indices": 2,
            }]
        }],
        "accessors": [
            {"bufferView": view_positions, "componentType": 5126,
             "count": 4, "type": "VEC3"},
            {"bufferView": view_normals, "componentType": 5126,
             "count": 4, "type": "VEC3"},
            {"bufferView": view_indices, "componentType": 5123,
             "count": 6, "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": offset, "byteLength": length}
            for offset, length in views
        ],
        "buffers": [{"byteLength": len(binary_blob)}],
    }

    json_bytes = json.dumps(document).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "
    padded_binary = binary_blob
    while len(padded_binary) % 4 != 0:
        padded_binary += b"\x00"

    total_length = 12 + 8 + len(json_bytes) + 8 + len(padded_binary)
    glb = bytearray()
    glb += struct.pack("<III", 0x46546C67, 2, total_length)
    glb += struct.pack("<II", len(json_bytes), 0x4E4F534A)
    glb += json_bytes
    glb += struct.pack("<II", len(padded_binary), 0x004E4942)
    glb += padded_binary
    return bytes(glb)


def test_parser_flags_mirrored_nodes(tmp_path: Path) -> None:
    glb_path = tmp_path / "mirrored.glb"
    glb_path.write_bytes(_build_mirrored_instances_glb())

    parsed = gltf_reader.parse_gltf(glb_path)

    assert [p["mirrored"] for p in parsed["primitives"]] == [False, True, True]


def test_mirrored_nodes_get_per_primitive_winding(tmp_path: Path) -> None:
    # Detection must not be outvoted by the two mirrored instances (the
    # authored convention is DirectX CW-front), and each mirrored
    # primitive — whose world-space winding the negative-determinant
    # transform reversed — must be flipped back individually.
    glb_path = tmp_path / "mirrored.glb"
    glb_path.write_bytes(_build_mirrored_instances_glb())
    output_directory = tmp_path / "out"
    manifest = convert_module.convert(glb_path, output_directory, base_name="krdm")

    assert any("winding auto-detected: directx" in w for w in manifest["warnings"])
    obj_text = next(output_directory.glob("*.obj")).read_text()
    indices = _parse_obj_indices(obj_text)
    # Instance 1 (identity): kept.  Instances 2-3 (mirrored): reversed.
    assert indices == [
        0, 1, 2, 0, 2, 3,
        4, 6, 5, 4, 7, 6,
        8, 10, 9, 8, 11, 10,
    ]


def test_skinned_primitive_is_skipped_with_warning(tmp_path: Path) -> None:
    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_test_glb())
    output_directory = tmp_path / "out"
    manifest = convert_module.convert(glb_path, output_directory)

    # Only the non-skinned quad produced an object.
    assert len(manifest["objects"]) == 1
    assert any("skinned mesh skipped" in w for w in manifest["warnings"])


# --------------------------------------------------------------------------
# DDS placeholder path.
# --------------------------------------------------------------------------
def _build_bc7_dds_header() -> bytes:
    """Hand-craft a DDS header with FourCC 'DX10' and dxgiFormat BC7."""
    header = bytearray(148)
    header[0:4] = b"DDS "
    struct.pack_into("<I", header, 4, 124)      # dwSize.
    struct.pack_into("<I", header, 8, 0x1007)   # dwFlags (caps|height|width|pf).
    struct.pack_into("<I", header, 12, 4)       # dwHeight.
    struct.pack_into("<I", header, 16, 4)       # dwWidth.
    struct.pack_into("<I", header, 76, 32)      # pixelformat dwSize.
    struct.pack_into("<I", header, 80, 0x4)     # DDPF_FOURCC.
    header[84:88] = b"DX10"                      # FourCC.
    struct.pack_into("<I", header, 128, 98)     # dxgiFormat = BC7_UNORM.
    struct.pack_into("<I", header, 132, 3)      # resourceDimension = TEXTURE2D.
    struct.pack_into("<I", header, 140, 1)      # arraySize.
    return bytes(header)


def test_dds_bc7_writes_placeholder_and_warning(tmp_path: Path) -> None:
    dds_bytes = _build_bc7_dds_header()
    # describe_dds_compression should name BC7 from the DX10 dxgiFormat.
    assert "BC7" in convert_module.describe_dds_compression(dds_bytes)

    image_entry = {
        "name": "terminal_albedo.dds",
        "mime_type": "image/vnd-ms-dds",
        "data": dds_bytes,
        "is_dds": True,
    }
    output_path = tmp_path / "terminal_albedo.png"
    warnings: List[str] = []
    convert_module.convert_texture(image_entry, output_path, warnings)

    # A placeholder PNG was written.
    assert output_path.exists()
    from PIL import Image
    with Image.open(output_path) as placeholder:
        assert placeholder.size == (64, 64)

    # The warning names the file and the BC7 compression.
    assert len(warnings) == 1
    assert "terminal_albedo.dds" in warnings[0]
    assert "BC7" in warnings[0]


# --------------------------------------------------------------------------
# Texture atlasing.
# --------------------------------------------------------------------------
def _solid_png_bytes(color: Tuple[int, int, int]) -> bytes:
    from PIL import Image

    image = Image.new("RGB", (4, 4), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


_ATLAS_RED = (200, 30, 40)
_ATLAS_GREEN = (30, 200, 40)
_ATLAS_BLUE = (30, 40, 200)


def _build_multi_texture_glb() -> bytes:
    """Assemble a GLB with three textured quads.

    Two quads have UVs in ``[0, 1]`` (distinct solid-color textures, so they
    are atlasable); the third has UVs spanning ``[0, 4]`` (tiling, so it must
    keep its own texture).
    """
    parts: List[bytes] = []
    views: List[Tuple[int, int]] = []

    def add_view(data: bytes) -> int:
        while sum(len(part) for part in parts) % 4 != 0:
            parts.append(b"\x00")
        offset = sum(len(part) for part in parts)
        parts.append(data)
        views.append((offset, len(data)))
        return len(views) - 1

    normals = [(0.0, 1.0, 0.0)] * 4
    indices_quad = [0, 1, 2, 0, 2, 3]
    unit_uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    tiling_uvs = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]

    quads = [
        ([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 0.0, 1.0), (0.0, 0.0, 1.0)],
         unit_uvs),
        ([(2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (3.0, 0.0, 1.0), (2.0, 0.0, 1.0)],
         unit_uvs),
        ([(4.0, 0.0, 0.0), (5.0, 0.0, 0.0), (5.0, 0.0, 1.0), (4.0, 0.0, 1.0)],
         tiling_uvs),
    ]

    primitives = []
    accessors: List[dict] = []
    for positions, uvs in quads:
        view_position = add_view(
            b"".join(struct.pack("<3f", *p) for p in positions)
        )
        view_normal = add_view(
            b"".join(struct.pack("<3f", *n) for n in normals)
        )
        view_uv = add_view(b"".join(struct.pack("<2f", *t) for t in uvs))
        view_indices = add_view(
            b"".join(struct.pack("<H", i) for i in indices_quad)
        )
        material_index = len(primitives)
        base = len(accessors)
        accessors.extend([
            {"bufferView": view_position, "componentType": 5126,
             "count": 4, "type": "VEC3"},
            {"bufferView": view_normal, "componentType": 5126,
             "count": 4, "type": "VEC3"},
            {"bufferView": view_uv, "componentType": 5126,
             "count": 4, "type": "VEC2"},
            {"bufferView": view_indices, "componentType": 5123,
             "count": 6, "type": "SCALAR"},
        ])
        primitives.append({
            "attributes": {
                "POSITION": base, "NORMAL": base + 1, "TEXCOORD_0": base + 2,
            },
            "indices": base + 3,
            "material": material_index,
        })

    image_views = [
        add_view(_solid_png_bytes(color))
        for color in (_ATLAS_RED, _ATLAS_GREEN, _ATLAS_BLUE)
    ]

    binary_blob = b"".join(parts)

    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": primitives}],
        "materials": [
            {"name": f"quad_{i}",
             "pbrMetallicRoughness": {"baseColorTexture": {"index": i}}}
            for i in range(3)
        ],
        "textures": [{"source": i} for i in range(3)],
        "images": [
            {"name": f"tex_{i}.png", "bufferView": image_views[i],
             "mimeType": "image/png"}
            for i in range(3)
        ],
        "accessors": accessors,
        "bufferViews": [
            {"buffer": 0, "byteOffset": offset, "byteLength": length}
            for offset, length in views
        ],
        "buffers": [{"byteLength": len(binary_blob)}],
    }

    json_bytes = json.dumps(document).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "
    padded_binary = binary_blob
    while len(padded_binary) % 4 != 0:
        padded_binary += b"\x00"

    total_length = 12 + 8 + len(json_bytes) + 8 + len(padded_binary)
    glb = bytearray()
    glb += struct.pack("<III", 0x46546C67, 2, total_length)
    glb += struct.pack("<II", len(json_bytes), 0x4E4F534A)
    glb += json_bytes
    glb += struct.pack("<II", len(padded_binary), 0x004E4942)
    glb += padded_binary
    return bytes(glb)


def _nearest_reference_color(
    sample: Tuple[int, int, int],
    references: List[Tuple[int, int, int]],
) -> Tuple[int, int, int]:
    def distance(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> int:
        return sum((a[k] - b[k]) ** 2 for k in range(3))

    return min(references, key=lambda reference: distance(sample, reference))


def test_atlas_merges_unit_square_textures_and_keeps_tiling(tmp_path: Path) -> None:
    from PIL import Image

    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_multi_texture_glb())
    output_directory = tmp_path / "out"

    manifest = convert_module.convert(
        glb_path, output_directory, base_name="krdm", atlas=True
    )

    obj_files = sorted(p.name for p in output_directory.glob("*.obj"))
    # Exactly two objects: one atlas (the two unit-square quads merged) plus
    # one tiling object that kept its own texture.
    assert len(obj_files) == 2
    assert "krdm_atlas.obj" in obj_files
    assert manifest["atlas"] == {"textures_packed": 2, "tiling_kept": 1}

    atlas_entries = [o for o in manifest["objects"] if o["material"] == "atlas"]
    tiling_entries = [o for o in manifest["objects"] if o["material"] != "atlas"]
    assert len(atlas_entries) == 1
    assert len(tiling_entries) == 1
    assert atlas_entries[0]["texture"] == "krdm_atlas.png"
    # The two unit-square quads (2 triangles each) merged into one object.
    assert atlas_entries[0]["triangles"] == 4

    # Parse the atlas PNG and confirm each merged quad's remapped UV centroid
    # samples its correct source color.
    atlas_png = output_directory / "krdm_atlas.png"
    assert atlas_png.exists()
    with Image.open(atlas_png) as atlas_image:
        atlas_rgb = atlas_image.convert("RGB")
        width, height = atlas_rgb.size
        atlas_obj_text = (output_directory / "krdm_atlas.obj").read_text()
        vertices = _parse_obj_vt(atlas_obj_text)
        assert len(vertices) == 8  # two quads, four vertices each.

        sampled_colors = []
        for quad_start in (0, 4):
            quad = vertices[quad_start:quad_start + 4]
            mean_u = sum(vertex[6] for vertex in quad) / 4.0
            mean_v = sum(vertex[7] for vertex in quad) / 4.0
            # OBJ8 v is bottom-origin; PNG rows are top-origin.
            pixel_x = min(width - 1, max(0, int(round(mean_u * width))))
            pixel_y = min(height - 1, max(0, int(round((1.0 - mean_v) * height))))
            sampled = atlas_rgb.getpixel((pixel_x, pixel_y))
            sampled_colors.append(
                _nearest_reference_color(sampled, [_ATLAS_RED, _ATLAS_GREEN])
            )
        # Each unit-square texture is represented exactly once in the atlas.
        assert set(sampled_colors) == {_ATLAS_RED, _ATLAS_GREEN}

    # The tiling object's UVs are untouched (still span the [0, 4] range).
    tiling_obj = output_directory / tiling_entries[0]["file"]
    tiling_vertices = _parse_obj_vt(tiling_obj.read_text())
    expected_tiling_uvs = {
        (round(u, 4), round(1.0 - v, 4)) for u, v in
        [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    }
    actual_tiling_uvs = {
        (round(vertex[6], 4), round(vertex[7], 4)) for vertex in tiling_vertices
    }
    assert actual_tiling_uvs == expected_tiling_uvs


def test_atlas_disabled_emits_one_object_per_texture(tmp_path: Path) -> None:
    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_multi_texture_glb())
    output_directory = tmp_path / "out"

    manifest = convert_module.convert(
        glb_path, output_directory, base_name="krdm", atlas=False
    )

    # Legacy behavior: one OBJ8 per texture (three quads, three textures).
    assert len(list(output_directory.glob("*.obj"))) == 3
    assert len(manifest["objects"]) == 3
    assert "atlas" not in manifest
    assert not any(entry["material"] == "atlas" for entry in manifest["objects"])


def test_atlas_spills_into_multiple_atlases_when_full() -> None:
    from PIL import Image

    from tools.msfs_to_obj8 import atlas_pack

    # Two 8x8 textures cannot share an 8x8 atlas, so packing must spill into
    # a second atlas.
    sources = [
        atlas_pack.SourceTexture("a", Image.new("RGB", (8, 8), (255, 0, 0))),
        atlas_pack.SourceTexture("b", Image.new("RGB", (8, 8), (0, 0, 255))),
    ]
    result = atlas_pack.pack_textures(sources, max_atlas_size=8, gutter=4)

    assert len(result.atlas_images) == 2
    assert {p.atlas_index for p in result.placements.values()} == {0, 1}
    for placed in result.placements.values():
        assert placed.atlas_size == 8
        assert placed.width == 8 and placed.height == 8
    for atlas_image in result.atlas_images:
        assert atlas_image.size == (8, 8)


# --------------------------------------------------------------------------
# Material fidelity: factor palette, LIT night maps, glass, normal maps.
# --------------------------------------------------------------------------
# Distinct factor colours (linear), chosen so their sRGB byte forms are far
# from a raw linear copy (0.53 -> ~0.755 sRGB, i.e. byte ~192 not 135).
_FACTOR_GRAY = (0.53, 0.53, 0.53, 1.0)
_FACTOR_BLUE = (0.10, 0.20, 0.80, 1.0)
_FACTOR_TRANSLUCENT = (0.90, 0.10, 0.10, 0.40)   # alpha < 0.95 -> glass
_FACTOR_EMISSIVE_BASE = (0.30, 0.30, 0.30, 1.0)
_FACTOR_EMISSIVE = (0.50, 0.50, 0.50)            # -> LIT cell = base * this
# Distinctive normal-map source: green != 128 so a flip is observable.
_NORMAL_SOURCE_RGBA = (128, 200, 255, 255)
_NORMAL_ROUGHNESS = 0.3                            # -> gloss 0.7 in alpha


def _png_bytes(color, size=(2, 2)):
    from PIL import Image

    image = Image.new("RGBA", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_fidelity_test_glb() -> bytes:
    """Assemble a GLB exercising every material-fidelity path.

    Five quads: two opaque factor materials, one translucent factor, one
    emissive factor (day/night switch), and one textured material carrying a
    normalTexture and roughnessFactor 0.3, under ASOBO_normal_map_convention.
    """
    parts: List[bytes] = []
    views: List[Tuple[int, int]] = []

    def add_view(data: bytes) -> int:
        while sum(len(part) for part in parts) % 4 != 0:
            parts.append(b"\x00")
        offset = sum(len(part) for part in parts)
        parts.append(data)
        views.append((offset, len(data)))
        return len(views) - 1

    normals = [(0.0, 1.0, 0.0)] * 4
    uvs = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    indices_quad = [0, 1, 2, 0, 2, 3]

    primitives = []
    accessors: List[dict] = []
    for slot in range(5):
        base_x = 2.0 * slot
        positions = [
            (base_x, 0.0, 0.0), (base_x + 1.0, 0.0, 0.0),
            (base_x + 1.0, 0.0, 1.0), (base_x, 0.0, 1.0),
        ]
        view_position = add_view(
            b"".join(struct.pack("<3f", *p) for p in positions)
        )
        view_normal = add_view(b"".join(struct.pack("<3f", *n) for n in normals))
        view_uv = add_view(b"".join(struct.pack("<2f", *t) for t in uvs))
        view_indices = add_view(
            b"".join(struct.pack("<H", i) for i in indices_quad)
        )
        base = len(accessors)
        accessors.extend([
            {"bufferView": view_position, "componentType": 5126,
             "count": 4, "type": "VEC3"},
            {"bufferView": view_normal, "componentType": 5126,
             "count": 4, "type": "VEC3"},
            {"bufferView": view_uv, "componentType": 5126,
             "count": 4, "type": "VEC2"},
            {"bufferView": view_indices, "componentType": 5123,
             "count": 6, "type": "SCALAR"},
        ])
        primitives.append({
            "attributes": {
                "POSITION": base, "NORMAL": base + 1, "TEXCOORD_0": base + 2,
            },
            "indices": base + 3,
            "material": slot,
        })

    view_base_image = add_view(_png_bytes((100, 150, 200, 255)))
    view_normal_image = add_view(_png_bytes(_NORMAL_SOURCE_RGBA))

    binary_blob = b"".join(parts)

    materials = [
        {"name": "gray", "pbrMetallicRoughness": {
            "baseColorFactor": list(_FACTOR_GRAY),
            "roughnessFactor": 0.9, "metallicFactor": 0.0}},
        {"name": "blue", "pbrMetallicRoughness": {
            "baseColorFactor": list(_FACTOR_BLUE),
            "roughnessFactor": 0.9, "metallicFactor": 0.0}},
        {"name": "canopy", "pbrMetallicRoughness": {
            "baseColorFactor": list(_FACTOR_TRANSLUCENT),
            "roughnessFactor": 0.9, "metallicFactor": 0.0}},
        {"name": "window", "emissiveFactor": list(_FACTOR_EMISSIVE),
         "extensions": {"ASOBO_material_day_night_switch": {}},
         "pbrMetallicRoughness": {
            "baseColorFactor": list(_FACTOR_EMISSIVE_BASE),
            "roughnessFactor": 0.9, "metallicFactor": 0.0}},
        {"name": "wall", "normalTexture": {"index": 1},
         "pbrMetallicRoughness": {
            "baseColorTexture": {"index": 0},
            "roughnessFactor": _NORMAL_ROUGHNESS, "metallicFactor": 0.0}},
    ]

    document = {
        "asset": {"version": "2.0"},
        "extensionsUsed": ["ASOBO_normal_map_convention"],
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": primitives}],
        "materials": materials,
        "textures": [{"source": 0}, {"source": 1}],
        "images": [
            {"name": "wall_albedo.png", "bufferView": view_base_image,
             "mimeType": "image/png"},
            {"name": "wall_normal.png", "bufferView": view_normal_image,
             "mimeType": "image/png"},
        ],
        "accessors": accessors,
        "bufferViews": [
            {"buffer": 0, "byteOffset": offset, "byteLength": length}
            for offset, length in views
        ],
        "buffers": [{"byteLength": len(binary_blob)}],
    }

    json_bytes = json.dumps(document).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "
    padded_binary = binary_blob
    while len(padded_binary) % 4 != 0:
        padded_binary += b"\x00"

    total_length = 12 + 8 + len(json_bytes) + 8 + len(padded_binary)
    glb = bytearray()
    glb += struct.pack("<III", 0x46546C67, 2, total_length)
    glb += struct.pack("<II", len(json_bytes), 0x4E4F534A)
    glb += json_bytes
    glb += struct.pack("<II", len(padded_binary), 0x004E4942)
    glb += padded_binary
    return bytes(glb)


def _cell_center_colors(image) -> set:
    """Sample the centre pixel of every 16x16 cell in a palette PNG."""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    colors = set()
    for top in range(0, height, material_fidelity.CELL_SIZE):
        for left in range(0, width, material_fidelity.CELL_SIZE):
            colors.add(rgba.getpixel(
                (left + material_fidelity.CELL_SIZE // 2,
                 top + material_fidelity.CELL_SIZE // 2)
            ))
    return colors


def _find_object(manifest, predicate):
    for entry in manifest["objects"]:
        if predicate(entry):
            return entry
    return None


def test_factor_palette_recovers_colors_in_srgb(tmp_path: Path) -> None:
    from PIL import Image

    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_fidelity_test_glb())
    output_directory = tmp_path / "out"
    manifest = convert_module.convert(glb_path, output_directory, base_name="krdm")

    # A single per-model palette PNG carries the recovered factor colours.
    palette_png = output_directory / "krdm_palette.png"
    assert palette_png.exists()
    with Image.open(palette_png) as palette:
        cell_colors = _cell_center_colors(palette)

    # The 0.53 grey is painted in sRGB (~192), NOT a raw linear copy (135).
    gray_byte = material_fidelity.srgb_byte(_FACTOR_GRAY[0])
    assert gray_byte != material_fidelity.to_byte(_FACTOR_GRAY[0])
    assert 185 <= gray_byte <= 200
    assert (gray_byte, gray_byte, gray_byte, 255) in cell_colors

    # The blue factor's three channels each pass through the sRGB transfer.
    blue_cell = (
        material_fidelity.srgb_byte(_FACTOR_BLUE[0]),
        material_fidelity.srgb_byte(_FACTOR_BLUE[1]),
        material_fidelity.srgb_byte(_FACTOR_BLUE[2]),
        255,
    )
    assert blue_cell in cell_colors


def test_translucent_factor_lands_in_glass_palette(tmp_path: Path) -> None:
    from PIL import Image

    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_fidelity_test_glb())
    output_directory = tmp_path / "out"
    manifest = convert_module.convert(glb_path, output_directory, base_name="krdm")

    glass = _find_object(manifest, lambda e: e["file"].endswith("_palette_glass.obj"))
    assert glass is not None
    assert glass.get("blend_glass") is True
    glass_text = (output_directory / glass["file"]).read_text()
    assert "BLEND_GLASS" in glass_text
    # BLEND_GLASS sits in the texture block, before POINT_COUNTS.
    assert glass_text.index("BLEND_GLASS") < glass_text.index("POINT_COUNTS")

    # The translucent factor's cell keeps its alpha (~102 for 0.40).
    with Image.open(output_directory / "krdm_palette.png") as palette:
        alphas = {color[3] for color in _cell_center_colors(palette)}
    assert material_fidelity.to_byte(_FACTOR_TRANSLUCENT[3]) in alphas

    # The opaque palette object must NOT blend.
    opaque = _find_object(manifest, lambda e: e["file"].endswith("_palette.obj"))
    assert opaque is not None
    assert "BLEND_GLASS" not in (output_directory / opaque["file"]).read_text()


def test_emissive_factor_group_emits_texture_lit(tmp_path: Path) -> None:
    from PIL import Image

    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_fidelity_test_glb())
    output_directory = tmp_path / "out"
    manifest = convert_module.convert(glb_path, output_directory, base_name="krdm")

    emissive = _find_object(
        manifest, lambda e: e["file"].endswith("_palette_lit.obj")
    )
    assert emissive is not None
    assert emissive.get("texture_lit") == "krdm_palette_LIT.png"
    obj_text = (output_directory / emissive["file"]).read_text()
    assert "TEXTURE_LIT krdm_palette_LIT.png" in obj_text
    # TEXTURE_LIT follows TEXTURE in the header block.
    assert obj_text.index("TEXTURE ") < obj_text.index("TEXTURE_LIT")

    # The LIT cell equals sRGB(baseColorFactor * emissiveFactor).
    lit_png = output_directory / "krdm_palette_LIT.png"
    assert lit_png.exists()
    with Image.open(lit_png) as lit:
        lit_colors = _cell_center_colors(lit)
    lit_value = material_fidelity.srgb_byte(
        _FACTOR_EMISSIVE_BASE[0] * _FACTOR_EMISSIVE[0]
    )
    assert (lit_value, lit_value, lit_value, 255) in lit_colors
    # It is genuinely dimmed below the daytime albedo (0.3 -> sRGB ~149).
    day_value = material_fidelity.srgb_byte(_FACTOR_EMISSIVE_BASE[0])
    assert lit_value < day_value


def test_normal_map_flips_green_and_bakes_gloss_alpha(tmp_path: Path) -> None:
    from PIL import Image

    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_fidelity_test_glb())
    output_directory = tmp_path / "out"
    manifest = convert_module.convert(glb_path, output_directory, base_name="krdm")

    textured = _find_object(manifest, lambda e: e.get("texture_normal"))
    assert textured is not None
    obj_text = (output_directory / textured["file"]).read_text()
    normal_file = textured["texture_normal"]
    assert f"TEXTURE_NORMAL {normal_file}" in obj_text
    # TEXTURE_NORMAL follows TEXTURE.
    assert obj_text.index("TEXTURE ") < obj_text.index("TEXTURE_NORMAL")

    with Image.open(output_directory / normal_file) as normal:
        red, green, blue, alpha = normal.convert("RGBA").getpixel((0, 0))
    # Green channel flipped (DirectX -> OpenGL) under ASOBO_normal_map_convention.
    assert green == 255 - _NORMAL_SOURCE_RGBA[1]
    assert red == _NORMAL_SOURCE_RGBA[0]      # red/blue pass through
    assert blue == _NORMAL_SOURCE_RGBA[2]
    # Gloss (1 - roughness) lives in the alpha channel: 1 - 0.3 = 0.7.
    assert abs(alpha / 255.0 - (1.0 - _NORMAL_ROUGHNESS)) <= 1.0 / 255.0

    # ATTR_shiny_rat 0.700 (= 1 - 0.3) is present, just before TRIS.
    assert "ATTR_shiny_rat 0.700" in obj_text
    assert obj_text.index("ATTR_shiny_rat") < obj_text.index("TRIS")


def test_palette_uvs_sit_at_cell_centers(tmp_path: Path) -> None:
    from PIL import Image

    glb_path = tmp_path / "model.glb"
    glb_path.write_bytes(_build_fidelity_test_glb())
    output_directory = tmp_path / "out"
    manifest = convert_module.convert(glb_path, output_directory, base_name="krdm")

    with Image.open(output_directory / "krdm_palette.png") as palette:
        canvas_width, canvas_height = palette.size

    opaque = _find_object(manifest, lambda e: e["file"].endswith("_palette.obj"))
    assert opaque is not None
    vertices = _parse_obj_vt((output_directory / opaque["file"]).read_text())
    assert vertices

    half = material_fidelity.CELL_SIZE / 2.0  # 8 px centre offset in a 16 px cell
    for vertex in vertices:
        u, v = vertex[6], vertex[7]
        # Every palette UV addresses a cell centre: (col*16 + 8) in pixels on
        # both axes (v is bottom-origin, so measure 1 - v against height).
        u_pixel = u * canvas_width
        v_pixel = (1.0 - v) * canvas_height
        assert abs((u_pixel % material_fidelity.CELL_SIZE) - half) < 1e-3
        assert abs((v_pixel % material_fidelity.CELL_SIZE) - half) < 1e-3
