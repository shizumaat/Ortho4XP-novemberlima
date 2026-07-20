"""glTF 2.0 reader (standard-library only) for the MSFS -> OBJ8 converter.

Parses ``.gltf`` (JSON, with external or ``data:`` embedded ``.bin`` and
image resources) and ``.glb`` (binary container) files into plain Python
structures: world-transformed geometry per primitive, materials, and image
byte payloads.  Nothing here imports a GUI toolkit or any third-party
package; only ``json``, ``struct``, ``base64`` and ``pathlib`` are used.

Scope (prototype): meshes and their base-color textures.  Skinned meshes
and morph-target primitives are skipped with a warning rather than
converted.  Animations and BGL placement are out of scope entirely.

Build-time impact: none -- this module is a stand-alone tool and is never
imported by the tile build pipeline.
"""
from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# glTF component types -> (struct format character, byte size).
_COMPONENT_TYPES: Dict[int, Tuple[str, int]] = {
    5120: ("b", 1),  # BYTE
    5121: ("B", 1),  # UNSIGNED_BYTE
    5122: ("h", 2),  # SHORT
    5123: ("H", 2),  # UNSIGNED_SHORT
    5125: ("I", 4),  # UNSIGNED_INT
    5126: ("f", 4),  # FLOAT
}

# glTF accessor types -> number of components.
_TYPE_COMPONENT_COUNTS: Dict[str, int] = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}

_GLB_MAGIC = 0x46546C67  # "glTF" little-endian.
_GLB_CHUNK_JSON = 0x4E4F534A  # "JSON".
_GLB_CHUNK_BIN = 0x004E4942  # "BIN\0".


# --------------------------------------------------------------------------
# 4x4 matrix helpers.  Matrices are held row-major as a flat list of 16
# floats: element at row r, column c is ``matrix[r * 4 + c]``.  glTF stores
# node matrices column-major per the specification, so ``_matrix_from_columns``
# transposes on the way in.
# --------------------------------------------------------------------------
def _identity_matrix() -> List[float]:
    return [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _matrix_from_columns(column_major: List[float]) -> List[float]:
    """Convert a glTF column-major 16-float matrix to our row-major layout."""
    return [column_major[column * 4 + row]
            for row in range(4) for column in range(4)]


def _matrix_from_translation_rotation_scale(
    translation: Tuple[float, float, float],
    rotation: Tuple[float, float, float, float],
    scale: Tuple[float, float, float],
) -> List[float]:
    """Compose T * R * S (row-major).  Rotation is a quaternion (x, y, z, w)."""
    x, y, z, w = rotation
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    wx, wy, wz = w * x, w * y, w * z
    # Rotation matrix (row-major) from the unit quaternion.
    rotation_matrix = [
        1 - 2 * (yy + zz), 2 * (xy - wz),     2 * (xz + wy),     0.0,
        2 * (xy + wz),     1 - 2 * (xx + zz), 2 * (yz - wx),     0.0,
        2 * (xz - wy),     2 * (yz + wx),     1 - 2 * (xx + yy), 0.0,
        0.0,               0.0,               0.0,               1.0,
    ]
    scale_x, scale_y, scale_z = scale
    scale_matrix = [
        scale_x, 0.0,     0.0,     0.0,
        0.0,     scale_y, 0.0,     0.0,
        0.0,     0.0,     scale_z, 0.0,
        0.0,     0.0,     0.0,     1.0,
    ]
    translation_matrix = _identity_matrix()
    translation_matrix[3] = translation[0]
    translation_matrix[7] = translation[1]
    translation_matrix[11] = translation[2]
    return _matrix_multiply(
        translation_matrix, _matrix_multiply(rotation_matrix, scale_matrix)
    )


def _matrix_multiply(a: List[float], b: List[float]) -> List[float]:
    """Row-major 4x4 matrix product ``a @ b``."""
    result = [0.0] * 16
    for row in range(4):
        for column in range(4):
            total = 0.0
            for k in range(4):
                total += a[row * 4 + k] * b[k * 4 + column]
            result[row * 4 + column] = total
    return result


def _transform_point(
    matrix: List[float], point: Tuple[float, float, float]
) -> Tuple[float, float, float]:
    """Apply a row-major affine matrix to a position (w = 1)."""
    x, y, z = point
    return (
        matrix[0] * x + matrix[1] * y + matrix[2] * z + matrix[3],
        matrix[4] * x + matrix[5] * y + matrix[6] * z + matrix[7],
        matrix[8] * x + matrix[9] * y + matrix[10] * z + matrix[11],
    )


def _determinant3(matrix: List[float]) -> float:
    """Determinant of the upper-left 3x3 of a row-major 4x4 matrix.

    Its sign tells whether the transform is orientation-preserving; a
    negative determinant (mirrored node) reverses the winding of the
    node's world-space triangles relative to their index order.
    """
    a, b, c = matrix[0], matrix[1], matrix[2]
    d, e, f = matrix[4], matrix[5], matrix[6]
    g, h, i = matrix[8], matrix[9], matrix[10]
    return (
        a * (e * i - f * h)
        - b * (d * i - f * g)
        + c * (d * h - e * g)
    )


def _normal_matrix(matrix: List[float]) -> List[float]:
    """Inverse-transpose of the upper-left 3x3, returned row-major 3x3.

    Normals transform by the inverse-transpose so that non-uniform scale and
    shear do not skew them off the surface.  Falls back to the plain 3x3 if
    the block is singular.
    """
    a, b, c = matrix[0], matrix[1], matrix[2]
    d, e, f = matrix[4], matrix[5], matrix[6]
    g, h, i = matrix[8], matrix[9], matrix[10]
    determinant = _determinant3(matrix)
    if abs(determinant) < 1e-12:
        # Singular upper 3x3: use it directly (best effort).
        return [a, b, c, d, e, f, g, h, i]
    inverse_determinant = 1.0 / determinant
    # Cofactor / inverse, then transpose -> combined into one indexing.
    inverse = [
        (e * i - f * h) * inverse_determinant,
        (c * h - b * i) * inverse_determinant,
        (b * f - c * e) * inverse_determinant,
        (f * g - d * i) * inverse_determinant,
        (a * i - c * g) * inverse_determinant,
        (c * d - a * f) * inverse_determinant,
        (d * h - e * g) * inverse_determinant,
        (b * g - a * h) * inverse_determinant,
        (a * e - b * d) * inverse_determinant,
    ]
    # Transpose of the inverse.
    return [
        inverse[0], inverse[3], inverse[6],
        inverse[1], inverse[4], inverse[7],
        inverse[2], inverse[5], inverse[8],
    ]


def _transform_direction(
    normal_matrix: List[float], direction: Tuple[float, float, float]
) -> Tuple[float, float, float]:
    """Apply a row-major 3x3 to a direction and normalize it."""
    x, y, z = direction
    tx = normal_matrix[0] * x + normal_matrix[1] * y + normal_matrix[2] * z
    ty = normal_matrix[3] * x + normal_matrix[4] * y + normal_matrix[5] * z
    tz = normal_matrix[6] * x + normal_matrix[7] * y + normal_matrix[8] * z
    length = (tx * tx + ty * ty + tz * tz) ** 0.5
    if length < 1e-12:
        return (0.0, 1.0, 0.0)
    return (tx / length, ty / length, tz / length)


# --------------------------------------------------------------------------
# Container decoding.
# --------------------------------------------------------------------------
def _load_glb(raw: bytes) -> Tuple[Dict[str, Any], Optional[bytes]]:
    """Split a ``.glb`` byte string into its JSON dict and binary chunk."""
    if len(raw) < 12:
        raise ValueError("GLB file too short for a 12-byte header")
    magic, version, _total_length = struct.unpack_from("<III", raw, 0)
    if magic != _GLB_MAGIC:
        raise ValueError("not a GLB file (bad magic word)")
    if version != 2:
        raise ValueError(f"unsupported GLB version {version} (expected 2)")
    offset = 12
    json_document: Optional[Dict[str, Any]] = None
    binary_chunk: Optional[bytes] = None
    while offset + 8 <= len(raw):
        chunk_length, chunk_type = struct.unpack_from("<II", raw, offset)
        offset += 8
        chunk_data = raw[offset:offset + chunk_length]
        offset += chunk_length
        if chunk_type == _GLB_CHUNK_JSON:
            # GLB blobs embedded in compiled MSFS BGLs sometimes pad the
            # JSON chunk with trailing junk beyond the closing brace (the
            # spec allows only 0x20 padding). raw_decode parses the first
            # complete JSON value and ignores whatever follows.
            chunk_text = chunk_data.decode("utf-8", errors="replace")
            json_document, _ = json.JSONDecoder().raw_decode(chunk_text)
        elif chunk_type == _GLB_CHUNK_BIN:
            binary_chunk = chunk_data
    if json_document is None:
        raise ValueError("GLB file has no JSON chunk")
    return json_document, binary_chunk


def _decode_data_uri(uri: str) -> bytes:
    """Decode a ``data:`` URI's payload (base64 or percent-less plain)."""
    header, _, payload = uri.partition(",")
    if ";base64" in header:
        return base64.b64decode(payload)
    # Non-base64 data URIs are URL-encoded text; rare for binary but handle it.
    from urllib.parse import unquote_to_bytes
    return unquote_to_bytes(payload)


def _resolve_buffer(
    buffer: Dict[str, Any], base_directory: Path, embedded_binary: Optional[bytes]
) -> bytes:
    """Return the raw bytes backing a glTF buffer entry."""
    uri = buffer.get("uri")
    if uri is None:
        # GLB-embedded buffer.
        if embedded_binary is None:
            raise ValueError("buffer without uri but no embedded binary chunk")
        return embedded_binary
    if uri.startswith("data:"):
        return _decode_data_uri(uri)
    from urllib.parse import unquote
    return (base_directory / unquote(uri)).read_bytes()


# --------------------------------------------------------------------------
# Accessor decoding.
# --------------------------------------------------------------------------
def _decode_accessor(
    document: Dict[str, Any],
    buffers: List[bytes],
    accessor_index: int,
    semantic: str = "",
) -> List[List[float]]:
    """Decode an accessor into a list of component tuples (as float lists).

    Honors ``byteStride`` (interleaved buffer views) and per-accessor
    ``byteOffset``.  Sparse accessors are not supported and raise ValueError.
    """
    accessor = document["accessors"][accessor_index]
    if "sparse" in accessor:
        raise ValueError("sparse accessors are not supported")
    component_format, component_size = _COMPONENT_TYPES[accessor["componentType"]]
    # ASOBO deviation (MSFS package-optimizer output, verified empirically
    # 2026-07-19; matches the FSDeveloper MDL wiki's "half-precision
    # texture coords"): TEXCOORD accessors are declared componentType
    # 5122 (SHORT, non-normalized) but the stored 16-bit words are
    # FLOAT16 bit patterns. Spec-conformant files never use
    # non-normalized SHORT for TEXCOORD, so the combination is
    # unambiguous and reinterpreted as binary16.
    if (
        semantic.startswith("TEXCOORD")
        and accessor["componentType"] == 5122
        and not accessor.get("normalized", False)
    ):
        component_format = "e"
    component_count = _TYPE_COMPONENT_COUNTS[accessor["type"]]
    element_count = accessor["count"]
    accessor_offset = accessor.get("byteOffset", 0)

    buffer_view_index = accessor.get("bufferView")
    if buffer_view_index is None:
        # No buffer view -> all-zero data per spec.
        return [[0.0] * component_count for _ in range(element_count)]

    buffer_view = document["bufferViews"][buffer_view_index]
    buffer_bytes = buffers[buffer_view["buffer"]]
    view_offset = buffer_view.get("byteOffset", 0)
    tightly_packed_stride = component_size * component_count
    stride = buffer_view.get("byteStride") or tightly_packed_stride

    start = view_offset + accessor_offset
    element_struct = struct.Struct("<" + component_format * component_count)
    values: List[List[float]] = []
    for element in range(element_count):
        element_start = start + element * stride
        components = element_struct.unpack_from(buffer_bytes, element_start)
        values.append([float(component) for component in components])
    return values


def _decode_indices(
    document: Dict[str, Any],
    buffers: List[bytes],
    accessor_index: int,
) -> List[int]:
    """Decode a scalar index accessor into a flat list of ints."""
    decoded = _decode_accessor(document, buffers, accessor_index)
    return [int(entry[0]) for entry in decoded]


def _compute_flat_normals(
    positions: List[List[float]], indices: List[int]
) -> List[List[float]]:
    """Compute a per-vertex normal by assigning each triangle's face normal
    to its three vertices (flat shading).  Last write wins on shared vertices.
    """
    normals: List[List[float]] = [[0.0, 1.0, 0.0] for _ in positions]
    for triangle_start in range(0, len(indices) - 2, 3):
        i0, i1, i2 = indices[triangle_start:triangle_start + 3]
        p0, p1, p2 = positions[i0], positions[i1], positions[i2]
        ax, ay, az = p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]
        bx, by, bz = p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2]
        nx = ay * bz - az * by
        ny = az * bx - ax * bz
        nz = ax * by - ay * bx
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        if length < 1e-12:
            face = [0.0, 1.0, 0.0]
        else:
            face = [nx / length, ny / length, nz / length]
        normals[i0] = list(face)
        normals[i1] = list(face)
        normals[i2] = list(face)
    return normals


# --------------------------------------------------------------------------
# Node hierarchy flattening.
# --------------------------------------------------------------------------
def _node_local_matrix(node: Dict[str, Any]) -> List[float]:
    """Return a node's local transform as a row-major matrix."""
    if "matrix" in node:
        return _matrix_from_columns(node["matrix"])
    translation = tuple(node.get("translation", (0.0, 0.0, 0.0)))
    rotation = tuple(node.get("rotation", (0.0, 0.0, 0.0, 1.0)))
    scale = tuple(node.get("scale", (1.0, 1.0, 1.0)))
    return _matrix_from_translation_rotation_scale(
        translation, rotation, scale  # type: ignore[arg-type]
    )


def _collect_root_nodes(document: Dict[str, Any]) -> List[int]:
    """Return the indices of scene-root nodes to traverse."""
    scenes = document.get("scenes")
    if scenes:
        default_scene = document.get("scene", 0)
        if 0 <= default_scene < len(scenes):
            return list(scenes[default_scene].get("nodes", []))
        roots: List[int] = []
        for scene in scenes:
            roots.extend(scene.get("nodes", []))
        return roots
    # No scenes: treat nodes not referenced as children as roots.
    nodes = document.get("nodes", [])
    child_indices = set()
    for node in nodes:
        for child in node.get("children", []):
            child_indices.add(child)
    return [index for index in range(len(nodes)) if index not in child_indices]


# --------------------------------------------------------------------------
# Image and material resolution.
# --------------------------------------------------------------------------
def _resolve_images(
    document: Dict[str, Any],
    buffers: List[bytes],
    base_directory: Path,
    warnings: List[str],
) -> List[Dict[str, Any]]:
    """Resolve every image entry to raw bytes plus metadata."""
    images: List[Dict[str, Any]] = []
    for image_index, image in enumerate(document.get("images", [])):
        name = image.get("name") or f"image_{image_index}"
        mime_type = image.get("mimeType")
        data: Optional[bytes] = None
        uri = image.get("uri")
        source_name = name
        try:
            if uri is not None:
                if uri.startswith("data:"):
                    data = _decode_data_uri(uri)
                else:
                    from urllib.parse import unquote
                    file_path = base_directory / unquote(uri)
                    data = file_path.read_bytes()
                    source_name = file_path.name
                    if mime_type is None:
                        mime_type = _guess_mime_from_name(file_path.name)
            elif "bufferView" in image:
                buffer_view = document["bufferViews"][image["bufferView"]]
                buffer_bytes = buffers[buffer_view["buffer"]]
                offset = buffer_view.get("byteOffset", 0)
                length = buffer_view["byteLength"]
                data = buffer_bytes[offset:offset + length]
        except (OSError, ValueError, KeyError, IndexError) as error:
            warnings.append(f"image '{name}' could not be resolved: {error}")
        is_dds = _looks_like_dds(mime_type, source_name, data)
        images.append({
            "name": source_name,
            "mime_type": mime_type,
            "data": data,
            "is_dds": is_dds,
        })
    return images


def _guess_mime_from_name(file_name: str) -> Optional[str]:
    lowered = file_name.lower()
    if lowered.endswith(".png"):
        return "image/png"
    if lowered.endswith(".jpg") or lowered.endswith(".jpeg"):
        return "image/jpeg"
    if lowered.endswith(".dds"):
        return "image/vnd-ms-dds"
    return None


def _looks_like_dds(
    mime_type: Optional[str], name: str, data: Optional[bytes]
) -> bool:
    if mime_type and "dds" in mime_type.lower():
        return True
    if name.lower().endswith(".dds"):
        return True
    if data is not None and data[:4] == b"DDS ":
        return True
    return False


def _resolve_texture_images(
    textures: List[Dict[str, Any]], texture_reference: Optional[Dict[str, Any]]
) -> Tuple[Optional[int], Optional[int]]:
    """Resolve one texture reference to (standard image, DDS image) indices.

    ``texture_reference`` is a glTF ``textureInfo`` object such as
    ``baseColorTexture`` or ``normalTexture`` (or ``None``).  Returns the
    standard ``texture.source`` image index and, when the ``MSFT_texture_dds``
    extension is present, the DDS image index it redirects to (per the
    extension, the DDS source overrides the standard one for renderers that
    understand it).  Either element is ``None`` when absent.
    """
    if texture_reference is None:
        return None, None
    texture_index = texture_reference.get("index")
    if texture_index is None or not (0 <= texture_index < len(textures)):
        return None, None
    texture = textures[texture_index]
    standard_image = texture.get("source")
    dds_extension = texture.get("extensions", {}).get("MSFT_texture_dds")
    dds_image = (
        dds_extension["source"]
        if dds_extension is not None and "source" in dds_extension
        else None
    )
    return standard_image, dds_image


def _resolve_materials(
    document: Dict[str, Any], warnings: List[str]
) -> List[Dict[str, Any]]:
    """Resolve materials to their base-color/normal images and PBR scalars.

    In addition to the base-color texture image indices (standard and, when
    ``MSFT_texture_dds`` is present, the DDS override), this records the
    normal-texture image indices and every scalar/factor a material-fidelity
    pass needs: ``base_color_factor`` (linear RGBA), ``roughness_factor`` and
    ``metallic_factor`` (scalars), ``emissive_factor`` (linear RGB),
    ``alpha_mode`` ("OPAQUE"/"MASK"/"BLEND"), ``alpha_cutoff``, and
    ``has_day_night_switch`` (the ASOBO night-window extension).  glTF factor
    colors are LINEAR; the converter is responsible for the sRGB transfer
    when it paints them.
    """
    textures = document.get("textures", [])
    materials: List[Dict[str, Any]] = []
    for material_index, material in enumerate(document.get("materials", [])):
        name = material.get("name") or f"material_{material_index}"
        pbr = material.get("pbrMetallicRoughness", {})
        base_standard, base_dds = _resolve_texture_images(
            textures, pbr.get("baseColorTexture")
        )
        normal_standard, normal_dds = _resolve_texture_images(
            textures, material.get("normalTexture")
        )
        base_color_factor = tuple(
            float(component)
            for component in pbr.get("baseColorFactor", (1.0, 1.0, 1.0, 1.0))
        )
        emissive_factor = tuple(
            float(component)
            for component in material.get("emissiveFactor", (0.0, 0.0, 0.0))
        )
        extensions = material.get("extensions", {})
        materials.append({
            "name": name,
            "base_color_image": base_standard,
            "base_color_dds_image": base_dds,
            "normal_image": normal_standard,
            "normal_dds_image": normal_dds,
            "base_color_factor": base_color_factor,
            "roughness_factor": float(pbr.get("roughnessFactor", 1.0)),
            "metallic_factor": float(pbr.get("metallicFactor", 1.0)),
            "emissive_factor": emissive_factor,
            "alpha_mode": material.get("alphaMode", "OPAQUE"),
            "alpha_cutoff": float(material.get("alphaCutoff", 0.5)),
            "has_day_night_switch": (
                "ASOBO_material_day_night_switch" in extensions
            ),
        })
    return materials


# --------------------------------------------------------------------------
# Top-level entry point.
# --------------------------------------------------------------------------
def parse_gltf(path: str | Path) -> Dict[str, Any]:
    """Parse a ``.gltf`` or ``.glb`` file into plain Python structures.

    Returns a dict with keys:

    ``primitives``
        List of dicts, one per (node, primitive) instance that carries
        renderable triangles.  Each has ``positions`` and ``normals``
        (world-transformed float triples), ``texcoords`` (float pairs,
        v NOT yet flipped), ``indices`` (ints), ``material`` (index or
        ``None``), and ``mirrored`` (bool: the node's world transform has a
        negative determinant, so the world-space winding of the triangles is
        the REVERSE of their index order).
    ``materials``
        List of ``{name, base_color_image, base_color_dds_image}``.
    ``images``
        List of ``{name, mime_type, data (bytes|None), is_dds (bool)}``.
    ``extensions_used``
        The document's ``extensionsUsed`` list (drives, for example, the
        ASOBO DirectX->OpenGL normal-map green-channel flip).
    ``warnings``
        Human-readable strings for anything skipped or defaulted.
    """
    path = Path(path)
    raw = path.read_bytes()
    warnings: List[str] = []

    if raw[:4] == struct.pack("<I", _GLB_MAGIC) or path.suffix.lower() == ".glb":
        document, embedded_binary = _load_glb(raw)
    else:
        document = json.loads(raw.decode("utf-8"))
        embedded_binary = None

    base_directory = path.parent
    buffers = [
        _resolve_buffer(buffer, base_directory, embedded_binary)
        for buffer in document.get("buffers", [])
    ]

    images = _resolve_images(document, buffers, base_directory, warnings)
    materials = _resolve_materials(document, warnings)

    meshes = document.get("meshes", [])
    nodes = document.get("nodes", [])

    primitives: List[Dict[str, Any]] = []

    def visit(node_index: int, parent_matrix: List[float]) -> None:
        node = nodes[node_index]
        world_matrix = _matrix_multiply(parent_matrix, _node_local_matrix(node))
        mesh_index = node.get("mesh")
        if mesh_index is not None:
            is_skinned = "skin" in node
            _emit_mesh_primitives(
                document, buffers, meshes[mesh_index], mesh_index,
                world_matrix, is_skinned, primitives, warnings,
            )
        for child in node.get("children", []):
            visit(child, world_matrix)

    for root in _collect_root_nodes(document):
        visit(root, _identity_matrix())

    # Meshes referenced by no node still hold geometry; emit them untransformed
    # so a file that is "just a mesh" (common for extracted assets) is usable.
    referenced_meshes = {
        node.get("mesh") for node in nodes if node.get("mesh") is not None
    }
    for mesh_index, mesh in enumerate(meshes):
        if mesh_index not in referenced_meshes:
            _emit_mesh_primitives(
                document, buffers, mesh, mesh_index,
                _identity_matrix(), False, primitives, warnings,
            )

    return {
        "primitives": primitives,
        "materials": materials,
        "images": images,
        "extensions_used": list(document.get("extensionsUsed", [])),
        "warnings": warnings,
    }


def _emit_mesh_primitives(
    document: Dict[str, Any],
    buffers: List[bytes],
    mesh: Dict[str, Any],
    mesh_index: int,
    world_matrix: List[float],
    is_skinned: bool,
    primitives: List[Dict[str, Any]],
    warnings: List[str],
) -> None:
    """Decode and world-transform one mesh's primitives into ``primitives``."""
    normal_matrix = _normal_matrix(world_matrix)
    mirrored = _determinant3(world_matrix) < 0.0
    mesh_name = mesh.get("name") or f"mesh_{mesh_index}"
    for primitive_index, primitive in enumerate(mesh.get("primitives", [])):
        label = f"{mesh_name}/primitive_{primitive_index}"
        # Only triangle topology is handled (mode 4, the default).
        mode = primitive.get("mode", 4)
        if mode != 4:
            warnings.append(f"{label}: non-triangle mode {mode} skipped")
            continue
        attributes = primitive.get("attributes", {})
        if is_skinned or "JOINTS_0" in attributes or "WEIGHTS_0" in attributes:
            warnings.append(f"{label}: skinned mesh skipped")
            continue
        if primitive.get("targets"):
            warnings.append(f"{label}: morph-target primitive skipped")
            continue
        if "POSITION" not in attributes:
            warnings.append(f"{label}: no POSITION attribute, skipped")
            continue

        try:
            raw_positions = _decode_accessor(
                document, buffers, attributes["POSITION"]
            )
            if "indices" in primitive:
                indices = _decode_indices(
                    document, buffers, primitive["indices"]
                )
            else:
                indices = list(range(len(raw_positions)))
            if "NORMAL" in attributes:
                raw_normals = _decode_accessor(
                    document, buffers, attributes["NORMAL"]
                )
                # ASOBO-optimized content quantizes normals to raw int8;
                # renormalize so OBJ8 output carries unit normals.
                for normal in raw_normals:
                    length = (
                        normal[0] * normal[0]
                        + normal[1] * normal[1]
                        + normal[2] * normal[2]
                    ) ** 0.5
                    if length > 1e-9 and abs(length - 1.0) > 1e-3:
                        normal[0] /= length
                        normal[1] /= length
                        normal[2] /= length
            else:
                raw_normals = None
            if "TEXCOORD_0" in attributes:
                raw_texcoords = _decode_accessor(
                    document, buffers, attributes["TEXCOORD_0"],
                    semantic="TEXCOORD_0",
                )
            else:
                raw_texcoords = None
                warnings.append(
                    f"{label}: no TEXCOORD_0, using zeroed UVs"
                )
        except (KeyError, IndexError, ValueError, struct.error) as error:
            warnings.append(f"{label}: decode failed ({error}), skipped")
            continue

        # World-transform positions.
        positions = [
            list(_transform_point(world_matrix, (p[0], p[1], p[2])))
            for p in raw_positions
        ]
        if raw_normals is not None:
            normals = [
                list(_transform_direction(
                    normal_matrix, (n[0], n[1], n[2])
                ))
                for n in raw_normals
            ]
        else:
            normals = _compute_flat_normals(positions, indices)
        if raw_texcoords is not None:
            texcoords = [[t[0], t[1]] for t in raw_texcoords]
        else:
            texcoords = [[0.0, 0.0] for _ in positions]

        primitives.append({
            "positions": positions,
            "normals": normals,
            "texcoords": texcoords,
            "indices": indices,
            "material": primitive.get("material"),
            "mirrored": mirrored,
        })
