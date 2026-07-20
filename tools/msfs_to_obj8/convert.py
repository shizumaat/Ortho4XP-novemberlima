"""Convert an MSFS-scenery glTF/GLB model into X-Plane OBJ8 objects.

Usage
-----
    venv/bin/python tools/msfs_to_obj8/convert.py INPUT.{gltf,glb} \\
        -o OUTPUT_DIR [--name BASE]

For every glTF material that carries triangles this writes one OBJ8 file
(``BASE_<materialname>.obj``, sanitized) into ``OUTPUT_DIR``, one PNG per
used base-color texture, and a ``manifest.json`` describing the result::

    {
      "objects": [{"file", "material", "triangles", "texture",
                   "bounds_xz" (horizontal footprint [min_x, min_z,
                   max_x, max_z] in OBJ8 meters, +X east +Z south)}, ...],
      "warnings": [...]
    }

Scope (prototype): geometry and base-color textures only.  No BGL placement
parsing, no animations, no skinning.  Skinned / morph-target primitives are
skipped with a warning.

Dependencies: Python standard library plus Pillow (already a repo
dependency).  No new dependency is introduced.

Build-time impact: none -- this is a stand-alone offline tool and is never
imported or invoked by the tile build pipeline.

LICENSING NOTE
--------------
Converted third-party scenery models are for PERSONAL USE unless the
original author grants redistribution rights; this tool must not be used to
republish others' work.

Coordinate-mapping derivation (the classic silent failure)
----------------------------------------------------------
MSFS scenery-model space at heading 0 is right-handed with +Y up,
NORTH along +Z, and +X pointing WEST (the glTF "front faces +Z"
convention with front = north).  X-Plane is +X east, +Y up, +Z south,
so the conversion is a 180-degree rotation about Y:

    (x, y, z)_msfs_gltf  ->  (-x, y, -z)_obj8      (ROTATION, det +1).

MEASURED 2026-07-19 against ground truth: fitting the converted
BullfrogSim KRDM terminal's wall vertices to the airport's OSM
footprint across all 16 horizontal orthogonal-map x heading-sign
combinations, this rotation with headings passed through unchanged fits
at 2.8 m mean error; the runner-up is 2.5x worse, and the z-negation
reflection originally shipped rendered every model mirrored in-sim.
A rotation preserves orientation, so winding converts as:

  * spec-conformant glTF sources (CCW-front) -> REVERSE each triangle
    (X-Plane OBJ8 is CW-front, verified against stock objects);
  * DirectX-wound sources (CW-front, i.e. glTF blobs carved from
    compiled MSFS BGLs) -> KEEP the original index order.

Placement headings pass through unchanged (a rotation does not
conjugate rotations; the old reflection would have required negating
them).

Positions and normals rotate together (negate x and z); the UV origin
also differs: glTF's V origin is TOP-left, OpenGL/OBJ8's V origin is
BOTTOM-left, so v_obj8 = 1 - v_gltf.

Source-winding caveat (measured 2026-07-19): glTF blobs CARVED OUT OF
COMPILED MSFS BGL FILES are wound CLOCKWISE-front in glTF space --
DirectX convention, violating the glTF specification (95,637 of 95,637
sampled triangles in the BullfrogSim KRDM model library agree). Loose
SDK-exported .gltf files follow the spec. The converter AUTO-DETECTS the
source winding per file by testing whether right-handed geometric
normals agree with the authored vertex normals, keeps CW-front sources
as-is, and reverses spec-CCW sources so the OBJ8 output is always
CW-front. Override with --winding gltf|directx if a file lies about its
normals.

Per-node correction: the glTF spec reverses the winding of a node whose
world transform has a NEGATIVE determinant (mirrored, e.g. a negative
scale). The reader records that sign per primitive; detection votes are
sign-corrected so mirrored instances cannot outvote the authored
convention, and each primitive's reversal is the XOR of the file-level
decision with its own mirror flag — a mirrored node no longer renders
inside-out.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Ensure the tool package is importable whether run as a module or a script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from tools.msfs_to_obj8 import atlas_pack, gltf_reader, material_fidelity
else:
    from . import atlas_pack, gltf_reader, material_fidelity

_PLACEHOLDER_SIZE = 64
_PLACEHOLDER_GRAY = (128, 128, 128)


def sanitize_name(name: str) -> str:
    """Reduce ``name`` to filename-safe characters (alnum, dash, underscore)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    return cleaned or "unnamed"


# --------------------------------------------------------------------------
# DDS header inspection (used only to name the compression in a warning).
# --------------------------------------------------------------------------
# DXGI formats relevant to BC7; the full table is large, we name what we meet.
_DXGI_FORMAT_NAMES: Dict[int, str] = {
    71: "BC1_UNORM",
    72: "BC1_UNORM_SRGB",
    74: "BC2_UNORM",
    75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM",
    78: "BC3_UNORM_SRGB",
    80: "BC4_UNORM",
    83: "BC5_UNORM",
    95: "BC6H_UF16",
    96: "BC6H_SF16",
    98: "BC7_UNORM",
    99: "BC7_UNORM_SRGB",
}


def describe_dds_compression(data: bytes) -> Optional[str]:
    """Read a DDS header and return a human name for its compression.

    The DDS header is a fixed 128 bytes; a ``DX10`` FourCC adds a further
    20-byte ``DDS_HEADER_DXT10`` block whose first field is the dxgiFormat.
    Returns ``None`` if the header cannot be interpreted.
    """
    if len(data) < 128 or data[:4] != b"DDS ":
        return None
    # The pixel-format FourCC lives at byte offset 84 (4 bytes).
    four_cc = data[84:88]
    if four_cc == b"DX10":
        if len(data) < 148:
            return "DX10 (truncated header)"
        dxgi_format = struct.unpack_from("<I", data, 128)[0]
        return _DXGI_FORMAT_NAMES.get(
            dxgi_format, f"DX10 dxgiFormat {dxgi_format}"
        )
    try:
        decoded = four_cc.decode("ascii")
    except UnicodeDecodeError:
        return "uncompressed / unknown"
    if decoded.strip("\x00"):
        return decoded
    return "uncompressed (no FourCC)"


# --------------------------------------------------------------------------
# Texture handling.
# --------------------------------------------------------------------------
def _save_placeholder(output_path: Path) -> None:
    """Write a 64x64 mid-gray PNG placeholder."""
    from PIL import Image

    image = Image.new("RGB", (_PLACEHOLDER_SIZE, _PLACEHOLDER_SIZE),
                      _PLACEHOLDER_GRAY)
    image.save(output_path, format="PNG")


def convert_texture(
    image_entry: Dict[str, Any],
    output_path: Path,
    warnings: List[str],
) -> None:
    """Decode one glTF image to a PNG at ``output_path``.

    PNG/JPEG images are re-encoded to PNG.  DDS images are decoded via Pillow
    when it can (BC1/BC2/BC3 and some BC5); if Pillow raises (for example on
    BC7), a mid-gray placeholder is written and a warning names the file and
    the compression, read from the DDS header directly.
    """
    from PIL import Image

    data = image_entry.get("data")
    name = image_entry.get("name", "texture")
    if data is None:
        warnings.append(f"texture '{name}': no image data, placeholder written")
        _save_placeholder(output_path)
        return
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image.convert("RGBA").save(output_path, format="PNG")
    except Exception as error:  # noqa: BLE001 - Pillow raises many types.
        compression = None
        if image_entry.get("is_dds") or data[:4] == b"DDS ":
            compression = describe_dds_compression(data)
        detail = f" ({compression})" if compression else ""
        warnings.append(
            f"texture '{name}': could not decode{detail}; "
            f"64x64 gray placeholder written [{type(error).__name__}]"
        )
        _save_placeholder(output_path)


def _decode_texture_image(
    image_entry: Dict[str, Any], warnings: List[str]
) -> Any:
    """Decode one glTF image to an in-memory Pillow image for atlasing.

    Mirrors :func:`convert_texture` but returns the decoded image instead of
    writing a PNG.  On a decode failure (for example a BC7 DDS Pillow cannot
    read) it returns a 64x64 mid-gray placeholder and records a warning that
    names the file and, when known, the compression.
    """
    from PIL import Image

    data = image_entry.get("data")
    name = image_entry.get("name", "texture")
    if data is None:
        warnings.append(f"texture '{name}': no image data, gray atlas cell used")
        return Image.new("RGB", (_PLACEHOLDER_SIZE, _PLACEHOLDER_SIZE),
                         _PLACEHOLDER_GRAY)
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return image.convert("RGBA")
    except Exception as error:  # noqa: BLE001 - Pillow raises many types.
        compression = None
        if image_entry.get("is_dds") or data[:4] == b"DDS ":
            compression = describe_dds_compression(data)
        detail = f" ({compression})" if compression else ""
        warnings.append(
            f"texture '{name}': could not decode{detail}; "
            f"64x64 gray atlas cell used [{type(error).__name__}]"
        )
        return Image.new("RGB", (_PLACEHOLDER_SIZE, _PLACEHOLDER_SIZE),
                         _PLACEHOLDER_GRAY)


# --------------------------------------------------------------------------
# OBJ8 writing (self-contained local writer per the spec).
# --------------------------------------------------------------------------
def _write_obj8(
    output_path: Path,
    vertices: List[Tuple[float, float, float, float, float, float, float, float]],
    indices: List[int],
    texture_file_name: Optional[str],
    comments: List[str],
    *,
    texture_lit_file_name: Optional[str] = None,
    texture_normal_file_name: Optional[str] = None,
    blend_glass: bool = False,
    no_blend: bool = False,
    shiny_ratio: Optional[float] = None,
) -> None:
    """Write an OBJ8 object: header, textures, POINT_COUNTS, VT, IDX, TRIS.

    ``vertices`` are 8-tuples ``(px, py, pz, nx, ny, nz, u, v)`` already in
    OBJ8 coordinates.  Winding is left exactly as given (see the module
    docstring's derivation).

    Material-fidelity directives, when supplied, are written in X-Plane's
    required order: ``TEXTURE_LIT`` and ``TEXTURE_NORMAL`` follow ``TEXTURE``;
    ``BLEND_GLASS`` (X-Plane 12 glass blending) sits right after the texture
    block; ``ATTR_no_blend`` (alpha cutout) and ``ATTR_shiny_rat`` (portable
    gloss control) sit immediately before ``TRIS``, the gloss line last so it
    is closest to the geometry it governs.
    """
    lines: List[str] = ["I", "800", "OBJ", ""]
    for comment in comments:
        lines.append(f"# {comment}")
    if comments:
        lines.append("")
    if texture_file_name is not None:
        lines.append(f"TEXTURE {texture_file_name}")
        if texture_lit_file_name is not None:
            lines.append(f"TEXTURE_LIT {texture_lit_file_name}")
        if texture_normal_file_name is not None:
            lines.append(f"TEXTURE_NORMAL {texture_normal_file_name}")
        if blend_glass:
            lines.append("BLEND_GLASS")
        lines.append("")
    lines.append(f"POINT_COUNTS {len(vertices)} 0 0 {len(indices)}")
    lines.append("")
    for px, py, pz, nx, ny, nz, u, v in vertices:
        lines.append(
            f"VT {px:.4f} {py:.4f} {pz:.4f} "
            f"{nx:.4f} {ny:.4f} {nz:.4f} {u:.5f} {v:.5f}"
        )
    lines.append("")
    full_chunks = len(indices) // 10
    for chunk in range(full_chunks):
        values = indices[10 * chunk:10 * chunk + 10]
        lines.append("IDX10 " + " ".join(str(value) for value in values))
    for value in indices[10 * full_chunks:]:
        lines.append(f"IDX {value}")
    lines.append("")
    if no_blend:
        lines.append("ATTR_no_blend")
    if shiny_ratio is not None:
        lines.append(f"ATTR_shiny_rat {shiny_ratio:.3f}")
    lines.append(f"TRIS 0 {len(indices)}")
    lines.append("")
    output_path.write_text("\n".join(lines), encoding="ascii")


# --------------------------------------------------------------------------
# Conversion core.
# --------------------------------------------------------------------------
def detect_source_winding(primitives: List[Dict[str, Any]]) -> str:
    """Classify the source winding convention: "gltf" (spec CCW-front) or
    "directx" (CW-front, as found in glTF blobs carved from compiled MSFS
    BGL model libraries).

    Tests whether right-handed geometric triangle normals agree with the
    authored vertex normals across a sample of triangles.  A mirrored
    (negative-determinant) node reverses its world-space winding relative
    to the index order AND flips the geometric-vs-authored comparison, so
    its votes are sign-corrected — the result describes the AUTHORED
    convention, independent of how many instances happen to be mirrored.
    """
    agree = disagree = 0
    for primitive in primitives:
        positions = primitive["positions"]
        normals = primitive["normals"]
        indices = primitive["indices"]
        vote_sign = -1.0 if primitive.get("mirrored") else 1.0
        for k in range(0, min(len(indices), 600), 3):
            i0, i1, i2 = indices[k : k + 3]
            edge_a = [positions[i1][j] - positions[i0][j] for j in range(3)]
            edge_b = [positions[i2][j] - positions[i0][j] for j in range(3)]
            cross = [
                edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
                edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
                edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
            ]
            authored = [
                (normals[i0][j] + normals[i1][j] + normals[i2][j]) / 3.0
                for j in range(3)
            ]
            dot = vote_sign * sum(cross[j] * authored[j] for j in range(3))
            if dot > 1e-12:
                agree += 1
            elif dot < -1e-12:
                disagree += 1
        if agree + disagree >= 5000:
            break
    return "directx" if disagree > agree else "gltf"


# --------------------------------------------------------------------------
# Material-fidelity grouping and geometry.
# --------------------------------------------------------------------------
# ATTR_shiny_rat below this magnitude is not worth emitting (near-matte).
_SHINY_THRESHOLD = 0.05


def _describe_material(
    materials: List[Dict[str, Any]], material_index: Optional[int]
) -> Dict[str, Any]:
    """Resolve one primitive's material into the fidelity fields we key on.

    ``material_index`` ``None`` (or out of range) yields a neutral opaque
    white factor material, so a primitive with no material joins the factor
    palette rather than a lost gray object.  ``base_image`` prefers the
    standard glTF image and falls back to the ``MSFT_texture_dds`` override;
    ``normal_image`` resolves the same way.
    """
    if material_index is None or not (0 <= material_index < len(materials)):
        return {
            "name": "untextured",
            "base_image": None,
            "normal_image": None,
            "base_color_factor": (1.0, 1.0, 1.0, 1.0),
            "roughness": 1.0,
            "emissive": (0.0, 0.0, 0.0),
            "alpha_mode": "OPAQUE",
        }
    material = materials[material_index]
    base_image = material.get("base_color_image")
    if base_image is None:
        base_image = material.get("base_color_dds_image")
    normal_image = material.get("normal_image")
    if normal_image is None:
        normal_image = material.get("normal_dds_image")
    return {
        "name": material.get("name", "material"),
        "base_image": base_image,
        "normal_image": normal_image,
        "base_color_factor": tuple(
            material.get("base_color_factor", (1.0, 1.0, 1.0, 1.0))
        ),
        "roughness": material.get("roughness_factor", 1.0),
        "emissive": tuple(material.get("emissive_factor", (0.0, 0.0, 0.0))),
        "alpha_mode": material.get("alpha_mode", "OPAQUE"),
    }


def _descriptor_is_glass(descriptor: Dict[str, Any]) -> bool:
    """Return ``True`` for a translucent or explicitly blended material."""
    return (
        descriptor["base_color_factor"][3] < 0.95
        or descriptor["alpha_mode"] == "BLEND"
    )


def _group_key(descriptor: Dict[str, Any]) -> Tuple[Any, ...]:
    """Compute the render-state group key a primitive is batched under.

    Factor-only materials (no base-color texture) share per-model palette
    objects split only by translucency and emissive-ness -- at most a handful
    of objects for hundreds of materials.  Textured materials additionally
    split by normal image and alpha mode so each object's render state
    (``TEXTURE_LIT``/``TEXTURE_NORMAL``/``BLEND_GLASS``/``ATTR_no_blend``) is
    uniform across its members.
    """
    emissive = material_fidelity.is_emissive(descriptor["emissive"])
    if descriptor["base_image"] is None:
        return ("palette", _descriptor_is_glass(descriptor), emissive)
    return (
        "textured",
        descriptor["base_image"],
        descriptor["normal_image"],
        emissive,
        descriptor["alpha_mode"],
    )


def _map_vertex(
    position: Tuple[float, float, float],
    normal: Tuple[float, float, float],
    uv: Tuple[float, float],
) -> Tuple[float, ...]:
    """Apply the MSFS glTF -> OBJ8 axis map (180-degree rotation about Y).

    ``uv`` is already in OBJ8 (bottom-left origin) convention.  See the module
    docstring for the derivation of the coordinate mapping; it is not touched
    here.
    """
    px, py, pz = position
    nx, ny, nz = normal
    u, v = uv
    return (-px, py, -pz, -nx, ny, -nz, u, v)


def _build_group_geometry(
    primitives: List[Dict[str, Any]],
    reverse_triangles: bool,
    uv_source: Any,
) -> Dict[str, Any]:
    """Merge a group's primitives into OBJ8 geometry plus PBR weighting.

    ``uv_source(primitive, vertex_index) -> (u, v)`` supplies each vertex's
    OBJ8-space UV (texture groups pass through the flipped source UV; palette
    groups return their cell centre).  Alongside the merged vertices/indices
    this accumulates the triangle-area-weighted roughness and emissive so the
    caller can emit an object-level ``ATTR_shiny_rat`` and scale a LIT map.

    ``reverse_triangles`` is the file-level decision for the AUTHORED
    convention; a primitive from a mirrored (negative-determinant) node has
    its world-space winding already reversed by the transform, so the
    per-primitive reversal is the XOR of the two.
    """
    vertices: List[Tuple[float, ...]] = []
    indices: List[int] = []
    area_sum = 0.0
    roughness_weighted = 0.0
    emissive_weighted = [0.0, 0.0, 0.0]
    for primitive in primitives:
        base_offset = len(vertices)
        positions = primitive["positions"]
        normals = primitive["normals"]
        descriptor = primitive["_fidelity"]
        roughness = descriptor["roughness"]
        emissive = descriptor["emissive"]
        for vertex_index in range(len(positions)):
            vertices.append(_map_vertex(
                positions[vertex_index],
                normals[vertex_index],
                uv_source(primitive, vertex_index),
            ))
        primitive_indices = primitive["indices"]
        if reverse_triangles != bool(primitive.get("mirrored")):
            for k in range(0, len(primitive_indices) - 2, 3):
                indices.append(base_offset + primitive_indices[k])
                indices.append(base_offset + primitive_indices[k + 2])
                indices.append(base_offset + primitive_indices[k + 1])
        else:
            for index in primitive_indices:
                indices.append(base_offset + index)
        # Triangle area is invariant under the pure rotation, so the raw
        # positions give the same weighting as the mapped vertices.
        for k in range(0, len(primitive_indices) - 2, 3):
            area = material_fidelity.triangle_area(
                positions[primitive_indices[k]],
                positions[primitive_indices[k + 1]],
                positions[primitive_indices[k + 2]],
            )
            area_sum += area
            roughness_weighted += area * roughness
            for channel in range(3):
                emissive_weighted[channel] += area * emissive[channel]
    return {
        "vertices": vertices,
        "indices": indices,
        "area": area_sum,
        "roughness_weighted": roughness_weighted,
        "emissive_weighted": tuple(emissive_weighted),
    }


def _mean_roughness(geometry: Dict[str, Any]) -> float:
    """Triangle-area-weighted mean roughness of an object (1.0 when empty)."""
    area = geometry["area"]
    return geometry["roughness_weighted"] / area if area > 0.0 else 1.0


def _mean_emissive(geometry: Dict[str, Any]) -> Tuple[float, float, float]:
    """Triangle-area-weighted mean emissive RGB of an object."""
    area = geometry["area"]
    if area <= 0.0:
        return (0.0, 0.0, 0.0)
    weighted = geometry["emissive_weighted"]
    return (weighted[0] / area, weighted[1] / area, weighted[2] / area)


def convert(
    input_path: str | Path,
    output_directory: str | Path,
    base_name: Optional[str] = None,
    winding: str = "auto",
    atlas: bool = True,
) -> Dict[str, Any]:
    """Convert a glTF/GLB model to OBJ8 objects and textures.

    Groups every primitive by its resolved texture, emits OBJ8 objects, exports
    the textures, and writes a ``manifest.json``.  Returns the manifest dict.

    ``winding``: "auto" (detect per file), "gltf" (spec CCW-front source,
    reverse each triangle to reach OBJ8's CW-front), or "directx"
    (CW-front source, keep index order).  Whichever way it is decided,
    a primitive from a mirrored (negative-determinant) node additionally
    flips that decision (see the module docstring).

    ``atlas`` (default ``True``): when two or more plain textured groups have
    UVs inside the unit square, pack their textures into one power-of-two
    atlas (spilling to ``_atlas2`` etc. when a 4096x4096 atlas is full) and
    emit a single ``<base>_atlas.obj`` per atlas, so the model approaches one
    OBJ8 object (one texture bind, one draw batch).  Groups whose UVs tile
    beyond the unit square, or that need per-object render state (a LIT/normal
    map, glass blending, an alpha cutout), keep their own object.  A lone
    atlasable group is emitted the legacy per-texture way.  ``atlas=False``
    restores the strict one-object-per-texture behavior.

    Material fidelity: factor-only materials (a solid ``baseColorFactor`` with
    no texture) are recovered into per-model *factor palette* objects instead
    of one lost gray group -- one small PNG whose 16x16 cells each hold a
    distinct linear->sRGB factor colour, with opaque and glass (translucent /
    ``BLEND``) factors split into ``<base>_palette.obj`` and
    ``<base>_palette_glass.obj``.  Emissive groups (all members
    ``emissiveFactor > 0``) gain a ``TEXTURE_LIT`` night map; groups carry an
    ``ATTR_shiny_rat`` gloss from their area-weighted roughness, a
    ``TEXTURE_NORMAL`` when a material has one (green-flipped under
    ``ASOBO_normal_map_convention``, gloss in alpha), ``BLEND_GLASS`` for
    blended textures, and ``ATTR_no_blend`` for alpha cutouts.
    """
    input_path = Path(input_path)
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    if base_name is None:
        base_name = input_path.stem
    base_name = sanitize_name(base_name)

    parsed = gltf_reader.parse_gltf(input_path)
    warnings: List[str] = list(parsed["warnings"])
    materials = parsed["materials"]
    images = parsed["images"]
    extensions_used = parsed.get("extensions_used", [])
    flip_normal_green = "ASOBO_normal_map_convention" in extensions_used

    if winding not in ("auto", "gltf", "directx"):
        raise ValueError(f"unknown winding mode {winding!r}")
    source_winding = (
        detect_source_winding(parsed["primitives"]) if winding == "auto" else winding
    )
    reverse_triangles = source_winding == "gltf"
    if winding == "auto":
        warnings.append(f"winding auto-detected: {source_winding}")

    # Attach a fidelity descriptor to every primitive and group by render
    # state (see _group_key): factor-only materials collapse into a handful of
    # per-model palette objects; textured materials split by texture, normal
    # map, emissive-ness, and alpha mode so each object's directives are
    # uniform across its members.
    groups: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    group_descriptor: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for primitive in parsed["primitives"]:
        descriptor = _describe_material(materials, primitive["material"])
        primitive["_fidelity"] = descriptor
        key = _group_key(descriptor)
        groups.setdefault(key, []).append(primitive)
        group_descriptor.setdefault(key, descriptor)

    # Export each used albedo texture once, keyed by image index.
    exported_textures: Dict[int, str] = {}

    def ensure_texture(image_index: Optional[int]) -> Optional[str]:
        if image_index is None or not (0 <= image_index < len(images)):
            return None
        if image_index in exported_textures:
            return exported_textures[image_index]
        image_entry = images[image_index]
        raw_stem = Path(image_entry.get("name") or f"texture_{image_index}").stem
        png_name = f"{sanitize_name(raw_stem)}.png"
        # Guard against two different images sanitizing to the same name.
        if png_name in exported_textures.values():
            png_name = f"{sanitize_name(raw_stem)}_{image_index}.png"
        convert_texture(image_entry, output_directory / png_name, warnings)
        exported_textures[image_index] = png_name
        return png_name

    objects: List[Dict[str, Any]] = []
    used_material_slug_counts: Dict[str, int] = {}

    def emit_object(
        material_name: str,
        material_field: str,
        texture_png: Optional[str],
        geometry: Dict[str, Any],
        *,
        texture_lit: Optional[str] = None,
        texture_normal: Optional[str] = None,
        blend_glass: bool = False,
        no_blend: bool = False,
    ) -> None:
        """Write one OBJ8 file (with fidelity directives) and its manifest row."""
        material_slug = sanitize_name(material_name)
        # Disambiguate duplicate sanitized material names.
        count = used_material_slug_counts.get(material_slug, 0)
        used_material_slug_counts[material_slug] = count + 1
        if count:
            material_slug = f"{material_slug}_{count}"

        obj_file_name = f"{base_name}_{material_slug}.obj"
        comments = [
            f"Converted from {input_path.name} by tools/msfs_to_obj8",
            f"material: {material_name}",
            "PERSONAL USE ONLY unless the original author grants "
            "redistribution rights.",
        ]
        shiny = 1.0 - _mean_roughness(geometry)
        shiny_ratio = round(shiny, 3) if shiny > _SHINY_THRESHOLD else None
        indices = geometry["indices"]
        _write_obj8(
            output_directory / obj_file_name,
            geometry["vertices"],  # type: ignore[arg-type]
            indices,
            texture_png,
            comments,
            texture_lit_file_name=texture_lit,
            texture_normal_file_name=texture_normal,
            blend_glass=blend_glass,
            no_blend=no_blend,
            shiny_ratio=shiny_ratio,
        )
        entry: Dict[str, Any] = {
            "file": obj_file_name,
            "material": material_field,
            "triangles": len(indices) // 3,
            "texture": texture_png,
        }
        vertices = geometry["vertices"]
        if vertices:
            # Horizontal footprint in OBJ8 meters (+X east, +Z south),
            # for placement-time exclusion zones sized to the model.
            entry["bounds_xz"] = [
                round(min(vertex[0] for vertex in vertices), 3),
                round(min(vertex[2] for vertex in vertices), 3),
                round(max(vertex[0] for vertex in vertices), 3),
                round(max(vertex[2] for vertex in vertices), 3),
            ]
        if texture_lit is not None:
            entry["texture_lit"] = texture_lit
        if texture_normal is not None:
            entry["texture_normal"] = texture_normal
        if blend_glass:
            entry["blend_glass"] = True
        if no_blend:
            entry["no_blend"] = True
        if shiny_ratio is not None:
            entry["shiny_rat"] = shiny_ratio
        objects.append(entry)

    # ------------------------------------------------------------------
    # Factor palette: recover every untextured (baseColorFactor) material.
    # ------------------------------------------------------------------
    palette_keys = [key for key in groups if key[0] == "palette"]
    palette_png: Optional[str] = None
    palette_lit_png: Optional[str] = None
    palette_layout: Optional[material_fidelity.PaletteLayout] = None
    if palette_keys:
        cell_index_by_key: Dict[Tuple[Any, ...], int] = {}
        cells: List[material_fidelity.FactorCell] = []
        for key in palette_keys:
            for primitive in groups[key]:
                descriptor = primitive["_fidelity"]
                cell = material_fidelity.FactorCell(
                    base_color=descriptor["base_color_factor"],
                    roughness=descriptor["roughness"],
                    emissive=descriptor["emissive"],
                )
                cell_identity = cell.key()
                if cell_identity not in cell_index_by_key:
                    cell_index_by_key[cell_identity] = len(cells)
                    cells.append(cell)
                primitive["_cell_index"] = cell_index_by_key[cell_identity]
        palette_layout = material_fidelity.palette_layout(len(cells))
        palette_png = f"{base_name}_palette.png"
        material_fidelity.bake_palette_image(cells, palette_layout).save(
            output_directory / palette_png, format="PNG"
        )
        if any(material_fidelity.is_emissive(cell.emissive) for cell in cells):
            palette_lit_png = f"{base_name}_palette_LIT.png"
            material_fidelity.bake_palette_lit_image(cells, palette_layout).save(
                output_directory / palette_lit_png, format="PNG"
            )

    def palette_uv(
        primitive: Dict[str, Any], _vertex_index: int
    ) -> Tuple[float, float]:
        assert palette_layout is not None
        return material_fidelity.cell_center_uv(
            primitive["_cell_index"], palette_layout
        )

    # Emit the palette objects (at most one per opaque/glass x emissive split).
    for key in sorted(palette_keys, key=lambda k: (k[1], k[2])):
        _, is_glass, emissive = key
        geometry = _build_group_geometry(groups[key], reverse_triangles, palette_uv)
        if not geometry["indices"]:
            continue
        name = "palette"
        if is_glass:
            name += "_glass"
        if emissive:
            name += "_lit"
        emit_object(
            name, "palette", palette_png, geometry,
            texture_lit=palette_lit_png if emissive else None,
            blend_glass=is_glass,
        )

    # ------------------------------------------------------------------
    # Textured groups: build geometry, then atlas the plain opaque ones.
    # ------------------------------------------------------------------
    def textured_uv(
        primitive: Dict[str, Any], vertex_index: int
    ) -> Tuple[float, float]:
        u, v = primitive["texcoords"][vertex_index]
        return (u, 1.0 - v)

    textured_keys = sorted(
        (key for key in groups if key[0] == "textured"),
        key=lambda k: (k[1] if k[1] is not None else -1, repr(k)),
    )
    textured_geometry: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for key in textured_keys:
        geometry = _build_group_geometry(groups[key], reverse_triangles, textured_uv)
        if geometry["indices"]:
            textured_geometry[key] = geometry

    def _is_atlasable(key: Tuple[Any, ...]) -> bool:
        """A textured group can atlas only when it needs no extra state."""
        _, base_image, normal_image, emissive, alpha_mode = key
        return (
            atlas
            and base_image is not None
            and normal_image is None
            and not emissive
            and alpha_mode == "OPAQUE"
            and atlas_pack.uvs_within_unit_square(
                textured_geometry[key]["vertices"]
            )
        )

    def emit_textured_own_object(key: Tuple[Any, ...]) -> None:
        """Emit one textured group as its own OBJ8 with fidelity directives."""
        _, base_image, normal_image, emissive, alpha_mode = key
        geometry = textured_geometry[key]
        texture_png = ensure_texture(base_image)
        material_name = (
            Path(texture_png).stem if texture_png is not None
            else group_descriptor[key]["name"]
        )
        texture_lit = None
        if emissive and texture_png is not None and 0 <= base_image < len(images):
            albedo = _decode_texture_image(images[base_image], warnings)
            lit_image = material_fidelity.bake_textured_lit_image(
                albedo, _mean_emissive(geometry)
            )
            texture_lit = f"{Path(texture_png).stem}_LIT.png"
            lit_image.save(output_directory / texture_lit, format="PNG")
        texture_normal = None
        if normal_image is not None and 0 <= normal_image < len(images):
            normal_source = _decode_texture_image(images[normal_image], warnings)
            gloss = 1.0 - _mean_roughness(geometry)
            normal_out = material_fidelity.build_normal_image(
                normal_source, flip_normal_green, gloss
            )
            normal_stem = Path(
                images[normal_image].get("name") or f"normal_{normal_image}"
            ).stem
            texture_normal = f"{sanitize_name(normal_stem)}_NML.png"
            normal_out.save(output_directory / texture_normal, format="PNG")
        emit_object(
            material_name, material_name, texture_png, geometry,
            texture_lit=texture_lit,
            texture_normal=texture_normal,
            blend_glass=(alpha_mode == "BLEND"),
            no_blend=(alpha_mode == "MASK"),
        )

    atlasable_keys = [key for key in textured_keys if _is_atlasable(key)]
    own_object_keys = [key for key in textured_keys if not _is_atlasable(key)]

    textures_packed = 0
    atlas_used = atlas
    if atlas and len(atlasable_keys) >= 2:
        sources: List[atlas_pack.SourceTexture] = []
        for key in atlasable_keys:
            base_image = key[1]
            image = _decode_texture_image(images[base_image], warnings)
            sources.append(atlas_pack.SourceTexture(key, image))

        result = atlas_pack.pack_textures(sources)
        warnings.extend(result.warnings)
        textures_packed = len(atlasable_keys)

        # Remap each atlasable group's UVs into its cell, and carry its area /
        # roughness weighting so the merged object still emits a correct gloss.
        per_atlas: Dict[int, Dict[str, Any]] = {}
        for key in atlasable_keys:
            geometry = textured_geometry[key]
            placed = result.placements[key]
            remapped = [
                (px, py, pz, nx, ny, nz, *atlas_pack.remap_uv(u, v, placed))
                for (px, py, pz, nx, ny, nz, u, v) in geometry["vertices"]
            ]
            bucket = per_atlas.setdefault(
                placed.atlas_index,
                {"parts": [], "area": 0.0, "roughness_weighted": 0.0},
            )
            bucket["parts"].append((remapped, geometry["indices"]))
            bucket["area"] += geometry["area"]
            bucket["roughness_weighted"] += geometry["roughness_weighted"]

        for atlas_index in range(len(result.atlas_images)):
            suffix = "atlas" if atlas_index == 0 else f"atlas{atlas_index + 1}"
            png_name = f"{base_name}_{suffix}.png"
            result.atlas_images[atlas_index].save(
                output_directory / png_name, format="PNG"
            )
            bucket = per_atlas.get(
                atlas_index, {"parts": [], "area": 0.0, "roughness_weighted": 0.0}
            )
            merged_vertices: List[Tuple[float, ...]] = []
            merged_indices: List[int] = []
            for remapped, part_indices in bucket["parts"]:
                offset = len(merged_vertices)
                merged_vertices.extend(remapped)
                merged_indices.extend(offset + index for index in part_indices)
            emit_object(
                suffix, "atlas", png_name,
                {
                    "vertices": merged_vertices,
                    "indices": merged_indices,
                    "area": bucket["area"],
                    "roughness_weighted": bucket["roughness_weighted"],
                    "emissive_weighted": (0.0, 0.0, 0.0),
                },
            )
    else:
        # Fewer than two atlasable groups: emit each on its own.
        atlas_used = False
        for key in atlasable_keys:
            emit_textured_own_object(key)

    for key in own_object_keys:
        emit_textured_own_object(key)

    manifest: Dict[str, Any] = {"objects": objects, "warnings": warnings}
    if atlas:
        manifest["atlas"] = {
            "textures_packed": textures_packed,
            "tiling_kept": len(own_object_keys) if atlas_used else 0,
        }
    (output_directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return manifest


def main(argv: Optional[List[str]] = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(
        description="Convert an MSFS glTF/GLB model to X-Plane OBJ8 objects. "
        "Converted third-party models are for PERSONAL USE only unless the "
        "author grants redistribution rights."
    )
    parser.add_argument("input", help="input .gltf or .glb file")
    parser.add_argument(
        "-o", "--output", required=True, help="output directory"
    )
    parser.add_argument(
        "--name", default=None,
        help="base name for output objects (default: input file stem)",
    )
    parser.add_argument(
        "--winding", default="auto", choices=("auto", "gltf", "directx"),
        help="source winding convention (default: auto-detect per file)",
    )
    parser.add_argument(
        "--no-atlas", dest="atlas", action="store_false",
        help="disable per-model texture atlasing (emit one object per "
             "texture instead of packing unit-square textures into an atlas)",
    )
    parser.set_defaults(atlas=True)
    arguments = parser.parse_args(argv)

    manifest = convert(
        arguments.input, arguments.output, arguments.name,
        winding=arguments.winding, atlas=arguments.atlas,
    )
    print(
        f"Wrote {len(manifest['objects'])} object(s) to {arguments.output}"
    )
    for entry in manifest["objects"]:
        print(
            f"  {entry['file']}: {entry['triangles']} tris, "
            f"texture={entry['texture']}"
        )
    for warning in manifest["warnings"]:
        print(f"  warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
