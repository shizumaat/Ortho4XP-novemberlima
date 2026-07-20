"""Build synthetic MSFS scenery packages that mimic real compiled output.

Three packages, each isolating a question about the O4_MSFS_* converter:

  SYNTH_CUSTOM  - "custom-heavy" airport: 3 embedded models (textured
                  terminal w/ ASOBO-quantized accessors + CW winding,
                  glass-cab tower, mirrored-wing building), placements
                  incl. one scaled 1.5x and one at +8 m AGL.
  SYNTH_LIBMIX  - "library-heavy" (KRDM-shaped): 1 embedded model, but
                  placements dominated by foreign (stock-library) GUIDs,
                  plus one extended record with an AttachedObject-style
                  tail to exercise the size-20 GUID heuristic.
  SYNTH_CONTROL - the same terminal authored spec-standard (float32 UVs,
                  CCW winding) to diff the decode paths.

Everything is placed around KRDM (44.253, -121.161) for realism.
"""
from __future__ import annotations

import importlib.util
import io
import json
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "scratchpad" / "synth_msfs"
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tools"))

import O4_MSFS_Package as MSFS  # noqa: E402

# Import the repo's synthetic-BGL helpers straight from the test module.
_spec = importlib.util.spec_from_file_location(
    "msfs_pkg_tests", REPO / "tests" / "test_msfs_package.py"
)
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)

from PIL import Image, ImageDraw  # noqa: E402

KRDM_LAT, KRDM_LON = 44.253, -121.161
M_PER_DEG_LAT = 111_320.0


def offset(lat: float, lon: float, north_m: float, east_m: float):
    import math
    return (
        lat + north_m / M_PER_DEG_LAT,
        lon + east_m / (M_PER_DEG_LAT * math.cos(math.radians(lat))),
    )


# ---------------------------------------------------------------------------
# Texture authoring: orientation-revealing PNGs.
# ---------------------------------------------------------------------------
def make_texture(kind: str) -> bytes:
    img = Image.new("RGB", (256, 256), (200, 190, 170))
    d = ImageDraw.Draw(img)
    if kind == "terminal":
        d.rectangle([0, 0, 255, 63], fill=(60, 90, 160))       # roof band (top)
        d.rectangle([0, 192, 255, 255], fill=(90, 70, 50))     # base band (bottom)
        for x in range(16, 256, 48):                            # windows
            d.rectangle([x, 96, x + 24, 160], fill=(40, 60, 80))
        d.polygon([(128, 70), (108, 90), (148, 90)], fill=(220, 40, 40))  # UP arrow
        d.text((100, 170), "TOP^", fill=(0, 0, 0))
    elif kind == "glass":
        img = Image.new("RGB", (256, 256), (30, 45, 60))
        d = ImageDraw.Draw(img)
        for x in range(0, 256, 32):
            d.line([(x, 0), (x, 255)], fill=(80, 110, 140), width=3)
    elif kind == "wing":
        d.rectangle([0, 0, 127, 255], fill=(180, 60, 60))       # left half red
        d.rectangle([128, 0, 255, 255], fill=(60, 140, 60))     # right half green
        d.text((40, 120), "L", fill=(255, 255, 255))
        d.text((200, 120), "R", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Geometry authoring (glTF Y-up, meters). Boxes with per-face UVs.
# ---------------------------------------------------------------------------
def box(sx, sy, sz, cx=0.0, cy=None, cz=0.0):
    """Axis-aligned box; cy default sits base on the ground (y=0)."""
    if cy is None:
        cy = sy / 2.0
    hx, hy, hz = sx / 2.0, sy / 2.0, sz / 2.0
    P, N, UV, I = [], [], [], []
    # (normal, corner loop CCW seen from outside, uv corners)
    faces = [
        ((0, 0, 1),  [(-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]),
        ((0, 0, -1), [(hx, -hy, -hz), (-hx, -hy, -hz), (-hx, hy, -hz), (hx, hy, -hz)]),
        ((1, 0, 0),  [(hx, -hy, hz), (hx, -hy, -hz), (hx, hy, -hz), (hx, hy, hz)]),
        ((-1, 0, 0), [(-hx, -hy, -hz), (-hx, -hy, hz), (-hx, hy, hz), (-hx, hy, -hz)]),
        ((0, 1, 0),  [(-hx, hy, hz), (hx, hy, hz), (hx, hy, -hz), (-hx, hy, -hz)]),
        ((0, -1, 0), [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy, hz), (-hx, -hy, hz)]),
    ]
    # glTF UV origin is TOP-left: v=0 at texture top. A wall's top edge
    # (max y) must get v=0 so the roof band renders at the top.
    uvs4 = [(0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]
    for normal, corners in faces:
        base = len(P)
        for (px, py, pz), (u, v) in zip(corners, uvs4):
            P.append((px + cx, py + cy, pz + cz))
            N.append(normal)
            UV.append((u, v))
        I += [base, base + 1, base + 2, base, base + 2, base + 3]  # CCW
    return P, N, UV, I


def lshape():
    """Asymmetric L: long bar +X with a stub toward +Z at the +X end."""
    P, N, UV, I = box(16, 6, 6, cx=8)
    P2, N2, UV2, I2 = box(6, 6, 10, cx=13, cz=8)
    base = len(P)
    P += P2
    N += N2
    UV += UV2
    I += [i + base for i in I2]
    return P, N, UV, I


# ---------------------------------------------------------------------------
# GLB assembly.
# ---------------------------------------------------------------------------
def f16(value: float) -> int:
    return struct.unpack("<H", struct.pack("<e", value))[0]


class GlbBuilder:
    def __init__(self):
        self.bin = bytearray()
        self.buffer_views = []
        self.accessors = []
        self.images = []
        self.textures = []
        self.materials = []
        self.meshes = []
        self.nodes = []
        self.scene_nodes = []

    def _view(self, data: bytes) -> int:
        while len(self.bin) % 4:
            self.bin.append(0)
        self.buffer_views.append(
            {"buffer": 0, "byteOffset": len(self.bin), "byteLength": len(data)}
        )
        self.bin += data
        return len(self.buffer_views) - 1

    def acc_positions(self, positions):
        data = b"".join(struct.pack("<fff", *p) for p in positions)
        mins = [min(p[i] for p in positions) for i in range(3)]
        maxs = [max(p[i] for p in positions) for i in range(3)]
        self.accessors.append({
            "bufferView": self._view(data), "componentType": 5126,
            "count": len(positions), "type": "VEC3", "min": mins, "max": maxs,
        })
        return len(self.accessors) - 1

    def acc_normals_int8(self, normals):
        data = b"".join(
            struct.pack("<bbbb", int(n[0] * 127), int(n[1] * 127), int(n[2] * 127), 0)
            for n in normals
        )
        self.accessors.append({
            "bufferView": self._view(data), "componentType": 5120,
            "count": len(normals), "type": "VEC3",
        })
        view = self.buffer_views[-1]
        view["byteStride"] = 4
        return len(self.accessors) - 1

    def acc_normals_f32(self, normals):
        data = b"".join(struct.pack("<fff", *n) for n in normals)
        self.accessors.append({
            "bufferView": self._view(data), "componentType": 5126,
            "count": len(normals), "type": "VEC3",
        })
        return len(self.accessors) - 1

    def acc_uv_asobo(self, uvs):
        data = b"".join(struct.pack("<HH", f16(u), f16(v)) for u, v in uvs)
        self.accessors.append({
            "bufferView": self._view(data), "componentType": 5122,
            "count": len(uvs), "type": "VEC2",
        })
        return len(self.accessors) - 1

    def acc_uv_f32(self, uvs):
        data = b"".join(struct.pack("<ff", *uv) for uv in uvs)
        self.accessors.append({
            "bufferView": self._view(data), "componentType": 5126,
            "count": len(uvs), "type": "VEC2",
        })
        return len(self.accessors) - 1

    def acc_indices(self, indices):
        data = b"".join(struct.pack("<H", i) for i in indices)
        self.accessors.append({
            "bufferView": self._view(data), "componentType": 5123,
            "count": len(indices), "type": "SCALAR",
        })
        return len(self.accessors) - 1

    def add_image_png(self, png: bytes) -> int:
        self.images.append({
            "bufferView": self._view(png), "mimeType": "image/png",
        })
        return len(self.images) - 1

    def add_material(self, name, image_index=None, alpha_mode="OPAQUE",
                     base_color=None, roughness=1.0, emissive=None):
        material = {
            "name": name,
            "pbrMetallicRoughness": {
                "metallicFactor": 0.0, "roughnessFactor": roughness,
            },
        }
        if image_index is not None:
            self.textures.append({"source": image_index})
            material["pbrMetallicRoughness"]["baseColorTexture"] = {
                "index": len(self.textures) - 1
            }
        if base_color is not None:
            material["pbrMetallicRoughness"]["baseColorFactor"] = list(base_color)
        if alpha_mode != "OPAQUE":
            material["alphaMode"] = alpha_mode
        if emissive is not None:
            material["emissiveFactor"] = list(emissive)
        self.materials.append(material)
        return len(self.materials) - 1

    def add_mesh(self, primitives) -> int:
        """primitives: list of dicts {P,N,UV,I,material,style}."""
        prims = []
        for spec in primitives:
            style = spec.get("style", "asobo")
            indices = list(spec["I"])
            if style == "asobo":
                # Compiled-BGL convention: CW front in raw index order.
                for k in range(0, len(indices) - 2, 3):
                    indices[k + 1], indices[k + 2] = indices[k + 2], indices[k + 1]
                pos = self.acc_positions(spec["P"])
                nrm = self.acc_normals_int8(spec["N"])
                uv = self.acc_uv_asobo(spec["UV"])
            else:
                pos = self.acc_positions(spec["P"])
                nrm = self.acc_normals_f32(spec["N"])
                uv = self.acc_uv_f32(spec["UV"])
            prims.append({
                "attributes": {"POSITION": pos, "NORMAL": nrm, "TEXCOORD_0": uv},
                "indices": self.acc_indices(indices),
                "material": spec["material"],
                "mode": 4,
            })
        self.meshes.append({"primitives": prims})
        return len(self.meshes) - 1

    def add_node(self, mesh_index, translation=None, scale=None, root=True):
        node = {"mesh": mesh_index}
        if translation:
            node["translation"] = list(translation)
        if scale:
            node["scale"] = list(scale)
        self.nodes.append(node)
        if root:
            self.scene_nodes.append(len(self.nodes) - 1)
        return len(self.nodes) - 1

    def build(self) -> bytes:
        while len(self.bin) % 4:
            self.bin.append(0)
        doc = {
            "asset": {"version": "2.0", "generator": "synth-msfs"},
            "buffers": [{"byteLength": len(self.bin)}],
            "bufferViews": self.buffer_views,
            "accessors": self.accessors,
            "meshes": self.meshes,
            "nodes": self.nodes,
            "scenes": [{"nodes": self.scene_nodes}],
            "scene": 0,
        }
        if self.images:
            doc["images"] = self.images
            doc["samplers"] = [{}]
            for t in self.textures:
                t["sampler"] = 0
            doc["textures"] = self.textures
        if self.materials:
            doc["materials"] = self.materials
        js = json.dumps(doc, separators=(",", ":")).encode()
        js += b" " * ((4 - len(js) % 4) % 4)
        total = 12 + 8 + len(js) + 8 + len(self.bin)
        out = struct.pack("<III", 0x46546C67, 2, total)
        out += struct.pack("<II", len(js), 0x4E4F534A) + js
        out += struct.pack("<II", len(self.bin), 0x004E4942) + bytes(self.bin)
        return out


# ---------------------------------------------------------------------------
# Model builders.
# ---------------------------------------------------------------------------
def build_terminal(style: str) -> bytes:
    g = GlbBuilder()
    tex = g.add_image_png(make_texture("terminal"))
    m = g.add_material("terminal_walls", image_index=tex, roughness=0.8)
    P, N, UV, I = box(60, 15, 20)
    g.add_mesh([{"P": P, "N": N, "UV": UV, "I": I, "material": m, "style": style}])
    g.add_node(0)
    return g.build()


def build_tower() -> bytes:
    g = GlbBuilder()
    tex = g.add_image_png(make_texture("terminal"))
    glass = g.add_image_png(make_texture("glass"))
    m_shaft = g.add_material("tower_shaft", image_index=tex, roughness=0.9)
    m_cab = g.add_material(
        "tower_cab_glass", image_index=glass, alpha_mode="BLEND",
        base_color=(1.0, 1.0, 1.0, 0.55), roughness=0.05,
    )
    P1, N1, UV1, I1 = box(6, 25, 6)
    P2, N2, UV2, I2 = box(9, 4, 9, cy=27.0)
    g.add_mesh([
        {"P": P1, "N": N1, "UV": UV1, "I": I1, "material": m_shaft, "style": "asobo"},
        {"P": P2, "N": N2, "UV": UV2, "I": I2, "material": m_cab, "style": "asobo"},
    ])
    g.add_node(0)
    return g.build()


def build_mirrored() -> bytes:
    """One L-wing mesh instanced twice: +X normal, -X mirrored (scale -1)."""
    g = GlbBuilder()
    tex = g.add_image_png(make_texture("wing"))
    m = g.add_material("wing", image_index=tex, roughness=0.7)
    P, N, UV, I = lshape()
    mesh = g.add_mesh([{"P": P, "N": N, "UV": UV, "I": I, "material": m, "style": "asobo"}])
    g.add_node(mesh, translation=(4, 0, 0))
    g.add_node(mesh, translation=(-4, 0, 0), scale=(-1.0, 1.0, 1.0))
    return g.build()


# ---------------------------------------------------------------------------
# Package assembly.
# ---------------------------------------------------------------------------
def guid_bytes(n: int) -> bytes:
    """Deterministic distinct 16-byte GUIDs."""
    return struct.pack("<IHH", 0xA0000000 + n, 0x1111, 0x2222) + bytes(
        [0x33, 0x44] + [n & 0xFF] * 6
    )


def make_extended_library_object(lon, lat, heading, guid: bytes, scale: float,
                                 tail_extra: int) -> bytes:
    """A LibraryObject record with an AttachedObject-style extension after
    the scale float — the fixed-offset layout of real records (GUID at 0x2C,
    scale at 0x3C) followed by `tail_extra` bytes of sub-record data. The
    current size-relative heuristic will misread GUID/scale on this record.
    """
    size = 64 + tail_extra
    record = bytearray(size)
    struct.pack_into("<H", record, 0, MSFS._RECORD_TYPE_LIBRARY_OBJECT)
    struct.pack_into("<H", record, 2, size)
    struct.pack_into("<I", record, 4, T._encode_longitude(lon))
    struct.pack_into("<I", record, 8, T._encode_latitude(lat))
    struct.pack_into("<i", record, 12, 0)
    struct.pack_into("<H", record, 16, 1)  # AGL
    struct.pack_into("<H", record, 22, T._encode_angle(heading))
    record[0x2C:0x2C + 16] = guid
    struct.pack_into("<f", record, 0x3C, scale)
    # AttachedObject sub-record filler (type 0x1002).
    struct.pack_into("<HH", record, 64, 0x1002, tail_extra)
    return bytes(record)


def write_package(root: Path, models: list[tuple[bytes, bytes]],
                  placement_records: list[bytes]):
    scenery = root / "scenery" / "global" / "scenery"
    scenery.mkdir(parents=True, exist_ok=True)
    model_section = T._make_model_section(models)
    (scenery / "modelLib.bgl").write_bytes(
        T._make_bgl([(MSFS._SECTION_TYPE_MODEL_DATA, model_section)])
    )
    payload = b"".join(placement_records)
    (scenery / "objects.bgl").write_bytes(
        T._make_bgl([(MSFS._SECTION_TYPE_SCENERY_OBJECT, payload)])
    )
    (root / "manifest.json").write_text(json.dumps(
        {"title": root.name, "content_type": "SCENERY"}, indent=2))


def main():
    OUT.mkdir(parents=True, exist_ok=True)

    terminal_asobo = build_terminal("asobo")
    terminal_std = build_terminal("standard")
    tower = build_tower()
    mirrored = build_mirrored()

    g_term, g_tower, g_mirror = guid_bytes(1), guid_bytes(2), guid_bytes(3)

    # --- SYNTH_CUSTOM ---------------------------------------------------
    lat0, lon0 = KRDM_LAT, KRDM_LON
    placements = []

    def rec(model_guid, north, east, heading, scale=1.0, alt_mm=0, flags=1):
        lat, lon = offset(lat0, lon0, north, east)
        return T._make_library_object(
            model_guid, lon, lat, heading,
            altitude_mm=alt_mm, flags=flags, scale=scale,
        )

    placements += [
        rec(g_term, 0, 0, 0.0),                       # terminal, plain
        rec(g_term, 0, 140, 30.0, scale=1.5),         # scaled 1.5x
        rec(g_tower, 90, -60, 45.0),                  # tower
        rec(g_tower, 90, 60, 45.0, alt_mm=8000),      # tower on +8 m AGL
        rec(g_mirror, -80, 0, 90.0),                  # mirrored wings
    ]
    write_package(
        OUT / "SYNTH_CUSTOM",
        [(g_term, terminal_asobo), (g_tower, tower), (g_mirror, mirrored)],
        placements,
    )

    # --- SYNTH_LIBMIX ---------------------------------------------------
    placements = [
        rec(g_term, 0, 0, 0.0),
        rec(g_term, 0, 90, 0.0),
        rec(g_term, 0, -90, 0.0),
    ]
    # Nine foreign/stock GUIDs (jetways, vehicles, lights...) not in the lib.
    for k in range(9):
        placements.append(rec(guid_bytes(100 + k), 60 + 10 * k, -40 + 10 * k,
                              float(k * 40 % 360)))
    # One extended record (AttachedObject tail) referencing the terminal.
    lat, lon = offset(lat0, lon0, -60.0, 0.0)
    placements.append(
        make_extended_library_object(lon, lat, 180.0, g_term, 1.0, 16)
    )
    write_package(OUT / "SYNTH_LIBMIX", [(g_term, terminal_asobo)], placements)

    # --- SYNTH_CONTROL --------------------------------------------------
    write_package(OUT / "SYNTH_CONTROL", [(g_term, terminal_std)],
                  [rec(g_term, 0, 0, 0.0)])

    print("built:", [p.name for p in sorted(OUT.iterdir()) if p.is_dir()])


if __name__ == "__main__":
    main()
