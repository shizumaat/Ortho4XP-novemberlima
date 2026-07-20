"""Tests for :mod:`O4_MSFS_Package`.

Unit tests build minimal synthetic BGL byte streams (a section table plus
one ModelData / one SceneryObject subsection) so both readers run fully
headless with no external files.  Integration tests, guarded by the
presence of the real ``KRDM_Redmond`` package, exercise the readers
against genuine compiled scenery.
"""
from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import O4_MSFS_Package as MSFS  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent
_KRDM = _REPO_ROOT / "scratchpad" / "KRDM_Redmond"
_KRDM_LAT, _KRDM_LON = 44.253, -121.161


# ---------------------------------------------------------------------------
# Synthetic BGL construction helpers
# ---------------------------------------------------------------------------
def _make_bgl(sections: list[tuple[int, bytes]]) -> bytes:
    """Assemble a minimal BGL from ``(section_type, payload)`` pairs.

    Each section gets exactly one subsection pointing at its payload.  The
    layout mirrors the real container: header, section table at 0x38,
    one subsection record per section, then the payloads.
    """
    header_size = MSFS._SECTION_TABLE_OFFSET
    section_table_size = len(sections) * MSFS._SECTION_ENTRY_SIZE
    subsection_area = MSFS._SECTION_TABLE_OFFSET + section_table_size
    subsection_size_total = len(sections) * MSFS._SUBSECTION_RECORD_SIZE
    payload_area = subsection_area + subsection_size_total

    header = bytearray(header_size)
    struct.pack_into("<I", header, 0, MSFS._BGL_MAGIC)
    struct.pack_into("<I", header, MSFS._SECTION_COUNT_OFFSET, len(sections))

    section_table = bytearray()
    subsection_records = bytearray()
    payloads = bytearray()
    for index, (section_type, payload) in enumerate(sections):
        subsection_offset = subsection_area + index * MSFS._SUBSECTION_RECORD_SIZE
        payload_offset = payload_area + len(payloads)
        section_table += struct.pack(
            "<IIIII",
            section_type,
            0x1,  # flags (unused by the reader)
            1,  # subsection_count
            subsection_offset,
            MSFS._SUBSECTION_RECORD_SIZE,
        )
        subsection_records += struct.pack(
            "<IIII", 0, 1, payload_offset, len(payload)
        )
        payloads += payload

    return bytes(header + section_table + subsection_records + payloads)


def _make_minimal_glb(extra_json: int = 0) -> bytes:
    """A tiny but structurally valid binary-glTF (magic/version/JSON).

    ``extra_json`` appends whitespace inside the JSON chunk so the GLB's
    declared total length grows -- used to make one definition genuinely
    larger than another.
    """
    json_chunk = b'{"asset":{"version":"2.0"}}' + b" " * extra_json
    # pad JSON to a 4-byte boundary with spaces
    while len(json_chunk) % 4:
        json_chunk += b" "
    body = struct.pack("<II", len(json_chunk), 0x4E4F534A)  # length, "JSON"
    body += json_chunk
    total = 12 + len(body)
    header = struct.pack("<III", 0x46546C67, 2, total)  # "glTF", version, len
    return header + body


def _make_model_blob(guid_bytes: bytes, glb: bytes) -> bytes:
    """Wrap a GLB the way the real blobs do (GXML descriptor then GLB)."""
    guid_string = MSFS._guid_to_string(guid_bytes)
    xml = f'<ModelInfo guid="{{{guid_string}}}" name="unit"/>'.encode()
    # Real blobs use the upper-case RIFF form type "GLTF"; the lower-case
    # "glTF" magic belongs only to the embedded GLB chunk.
    riff = b"RIFF" + struct.pack("<I", 0) + b"GLTF"
    riff += b"GXML" + struct.pack("<I", len(xml)) + xml
    riff += b"GLB\x00" + struct.pack("<I", len(glb)) + glb
    return riff


def _make_model_section(models: list[tuple[bytes, bytes]]) -> bytes:
    """Build a ModelData payload: index table then concatenated blobs."""
    index_size = len(models) * MSFS._MODEL_INDEX_RECORD_SIZE
    index = bytearray()
    blobs = bytearray()
    for guid_bytes, glb in models:
        blob = _make_model_blob(guid_bytes, glb)
        blob_offset = index_size + len(blobs)
        index += guid_bytes + struct.pack("<II", blob_offset, len(blob))
        blobs += blob
    return bytes(index + blobs)


def _encode_longitude(longitude: float) -> int:
    return round((longitude + 180.0) / MSFS._LONGITUDE_SCALE)


def _encode_latitude(latitude: float) -> int:
    return round((90.0 - latitude) / MSFS._LATITUDE_SCALE)


def _encode_angle(degrees: float) -> int:
    return round(degrees / MSFS._ANGLE_SCALE) & 0xFFFF


def _make_library_object(
    guid_bytes: bytes,
    longitude: float,
    latitude: float,
    heading: float,
    altitude_mm: int = 0,
    flags: int = 1,
    scale: float = 1.0,
    attached_tail: bytes = b"",
) -> bytes:
    """Build a LibraryObject record; ``attached_tail`` appends sub-record
    bytes (e.g. an AttachedObject) that extend the record past 64 bytes."""
    record = bytearray(64 + len(attached_tail))
    struct.pack_into("<H", record, 0, MSFS._RECORD_TYPE_LIBRARY_OBJECT)
    struct.pack_into("<H", record, 2, len(record))
    struct.pack_into("<I", record, 4, _encode_longitude(longitude))
    struct.pack_into("<I", record, 8, _encode_latitude(latitude))
    struct.pack_into("<i", record, 12, altitude_mm)
    struct.pack_into("<H", record, 16, flags)
    struct.pack_into("<H", record, 18, 0)  # pitch
    struct.pack_into("<H", record, 20, 0)  # bank
    struct.pack_into("<H", record, 22, _encode_angle(heading))
    struct.pack_into("<H", record, 24, 0)  # image complexity
    record[0x2C:0x3C] = guid_bytes
    struct.pack_into("<f", record, 0x3C, scale)
    record[64:] = attached_tail
    return bytes(record)


_GUID_A = bytes.fromhex("06edc7ee5030a3492fefa2eff5126c93")
_GUID_B = bytes.fromhex("3855b497b9046844dc6c407a2855d91a")


# ---------------------------------------------------------------------------
# Unit tests -- GUID formatting
# ---------------------------------------------------------------------------
def test_guid_to_string_canonical_layout():
    # Mixed-endian first three fields, trailing 8 bytes verbatim.
    assert MSFS._guid_to_string(_GUID_A) == "eec7ed06305049a32fefa2eff5126c93"


def test_guid_to_string_rejects_wrong_length():
    with pytest.raises(ValueError):
        MSFS._guid_to_string(b"\x00" * 15)


# ---------------------------------------------------------------------------
# Unit tests -- model library reader
# ---------------------------------------------------------------------------
def test_read_model_library_synthetic(tmp_path):
    glb_a = _make_minimal_glb()
    glb_b = _make_minimal_glb() + b"\x00" * 8  # different length is fine
    payload = _make_model_section([(_GUID_A, glb_a), (_GUID_B, glb_b)])
    bgl = _make_bgl([(MSFS._SECTION_TYPE_MODEL_DATA, payload)])
    (tmp_path / "texture").mkdir()
    bgl_path = tmp_path / "lib.bgl"
    bgl_path.write_bytes(bgl)

    entries = MSFS.read_model_library(bgl_path)
    assert len(entries) == 2
    by_guid = {entry.guid: entry for entry in entries}
    assert set(by_guid) == {
        "eec7ed06305049a32fefa2eff5126c93",
        "97b4553804b94468dc6c407a2855d91a",
    }
    entry = by_guid["eec7ed06305049a32fefa2eff5126c93"]
    assert entry.glb_bytes[:4] == b"glTF"
    assert struct.unpack_from("<I", entry.glb_bytes, 4)[0] == 2
    assert entry.texture_directory == str(tmp_path / "texture")
    assert entry.source_bgl == str(bgl_path)


def test_read_model_library_no_model_section(tmp_path):
    bgl = _make_bgl([(MSFS._SECTION_TYPE_SCENERY_OBJECT, b"\x00" * 8)])
    bgl_path = tmp_path / "nomodels.bgl"
    bgl_path.write_bytes(bgl)
    assert MSFS.read_model_library(bgl_path) == []


def test_read_model_library_not_a_bgl(tmp_path):
    bgl_path = tmp_path / "junk.bgl"
    bgl_path.write_bytes(b"not a bgl file at all")
    assert MSFS.read_model_library(bgl_path) == []


# ---------------------------------------------------------------------------
# Unit tests -- placement reader
# ---------------------------------------------------------------------------
def test_read_object_placements_synthetic(tmp_path):
    records = (
        _make_library_object(_GUID_A, -121.161, 44.253, heading=57.5, scale=1.5)
        + _make_library_object(
            _GUID_B, -121.150, 44.260, heading=238.9, altitude_mm=16607, flags=1
        )
    )
    bgl = _make_bgl([(MSFS._SECTION_TYPE_SCENERY_OBJECT, records)])
    bgl_path = tmp_path / "scenery.bgl"
    bgl_path.write_bytes(bgl)

    placements = MSFS.read_object_placements(bgl_path)
    assert len(placements) == 2

    first = placements[0]
    assert first.guid == "eec7ed06305049a32fefa2eff5126c93"
    assert first.longitude == pytest.approx(-121.161, abs=1e-4)
    assert first.latitude == pytest.approx(44.253, abs=1e-4)
    assert first.heading_degrees_true == pytest.approx(57.5, abs=1e-2)
    assert first.scale == pytest.approx(1.5)
    assert first.is_above_ground is True
    assert first.altitude_meters == pytest.approx(0.0)
    assert first.source_bgl == str(bgl_path)

    second = placements[1]
    assert second.altitude_meters == pytest.approx(16.607)
    assert 0.0 <= second.heading_degrees_true < 360.0


def test_guid_scale_read_at_fixed_offsets_despite_attached_tail(tmp_path):
    # An AttachedObject (0x1002) sub-record extends the record past 64
    # bytes; end-relative reads (size-20 / size-4) would land inside the
    # tail. The tail bytes here are deliberately GUID/float-like garbage.
    tail = struct.pack("<HH", 0x1002, 20) + b"\xde\xad\xbe\xef" * 4
    record = _make_library_object(
        _GUID_A, -121.161, 44.253, heading=90.0, scale=1.5,
        attached_tail=tail,
    )
    assert struct.unpack_from("<H", record, 2)[0] == 64 + len(tail)
    bgl = _make_bgl([(MSFS._SECTION_TYPE_SCENERY_OBJECT, record)])
    bgl_path = tmp_path / "attached.bgl"
    bgl_path.write_bytes(bgl)

    placements = MSFS.read_object_placements(bgl_path)
    assert len(placements) == 1
    assert placements[0].guid == "eec7ed06305049a32fefa2eff5126c93"
    assert placements[0].scale == pytest.approx(1.5)


def test_unconverted_record_types_are_counted_not_silently_skipped(tmp_path):
    def other_record(record_type: int, size: int = 24) -> bytes:
        record = bytearray(size)
        struct.pack_into("<HH", record, 0, record_type, size)
        return bytes(record)

    records = (
        _make_library_object(_GUID_A, -121.161, 44.253, heading=0.0)
        + other_record(0x0C)          # Windsock
        + other_record(0x0C)          # Windsock
        + other_record(0x0E)          # TaxiwaySign
        + other_record(0x77)          # unknown type
    )
    bgl = _make_bgl([(MSFS._SECTION_TYPE_SCENERY_OBJECT, records)])
    bgl_path = tmp_path / "mixed.bgl"
    bgl_path.write_bytes(bgl)

    warnings: list[str] = []
    placements = MSFS.read_object_placements(bgl_path, warnings)
    assert len(placements) == 1
    assert len(warnings) == 1
    assert "mixed.bgl" in warnings[0]
    assert "2 x Windsock" in warnings[0]
    assert "1 x TaxiwaySign" in warnings[0]
    assert "1 x type 0x77" in warnings[0]

    # read_package surfaces the same warning.
    _models, _placements, package_warnings = MSFS.read_package(tmp_path)
    assert any("2 x Windsock" in w for w in package_warnings)


def test_read_object_placements_no_scenery_section(tmp_path):
    payload = _make_model_section([(_GUID_A, _make_minimal_glb())])
    bgl = _make_bgl([(MSFS._SECTION_TYPE_MODEL_DATA, payload)])
    bgl_path = tmp_path / "modelsonly.bgl"
    bgl_path.write_bytes(bgl)
    assert MSFS.read_object_placements(bgl_path) == []


# ---------------------------------------------------------------------------
# Unit tests -- find_bgl_files / read_package / staging
# ---------------------------------------------------------------------------
def test_find_bgl_files_recursive_case_insensitive(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.bgl").write_bytes(b"")
    (tmp_path / "TWO.BGL").write_bytes(b"")
    (tmp_path / "note.txt").write_bytes(b"")
    found = MSFS.find_bgl_files(tmp_path)
    assert [p.name for p in found] == ["TWO.BGL", "one.bgl"]


def test_read_package_dedup_keeps_largest_glb(tmp_path):
    small = _make_minimal_glb()
    large = _make_minimal_glb(extra_json=64)
    # Two libraries defining the same GUID with different-sized GLBs.
    (tmp_path / "libA").mkdir()
    (tmp_path / "libB").mkdir()
    (tmp_path / "libA" / "a.bgl").write_bytes(
        _make_bgl([(MSFS._SECTION_TYPE_MODEL_DATA, _make_model_section([(_GUID_A, small)]))])
    )
    (tmp_path / "libB" / "b.bgl").write_bytes(
        _make_bgl([(MSFS._SECTION_TYPE_MODEL_DATA, _make_model_section([(_GUID_A, large)]))])
    )
    models, placements, warnings = MSFS.read_package(tmp_path)
    assert len(models) == 1
    assert len(models[0].glb_bytes) == len(large)
    assert placements == []


def test_read_package_empty_directory_warns(tmp_path):
    models, placements, warnings = MSFS.read_package(tmp_path)
    assert models == [] and placements == []
    assert any("no .bgl files" in w for w in warnings)


def test_stage_texture_directory_symlink_or_copy(tmp_path):
    texture_dir = tmp_path / "texture"
    texture_dir.mkdir()
    (texture_dir / "AC1.DDS").write_bytes(b"texture-bytes")
    entry = MSFS.ModelEntry(
        guid="x", glb_bytes=b"", source_bgl="s", texture_directory=str(texture_dir)
    )
    staging = tmp_path / "staging"
    MSFS.stage_texture_directory(entry, staging)
    staged = staging / "AC1.DDS"
    assert staged.exists()
    assert staged.read_bytes() == b"texture-bytes"
    # Second call must not raise even though the file already exists.
    MSFS.stage_texture_directory(entry, staging)


def test_stage_texture_directory_none_is_noop(tmp_path):
    entry = MSFS.ModelEntry(
        guid="x", glb_bytes=b"", source_bgl="s", texture_directory=None
    )
    staging = tmp_path / "staging"
    MSFS.stage_texture_directory(entry, staging)  # must not raise


# ---------------------------------------------------------------------------
# Integration tests -- real KRDM package
# ---------------------------------------------------------------------------
_needs_krdm = pytest.mark.skipif(
    not _KRDM.exists(), reason="real KRDM_Redmond package not available"
)


@_needs_krdm
def test_read_package_krdm_models_and_placements():
    models, placements, warnings = MSFS.read_package(_KRDM)

    guids = {m.guid for m in models}
    assert len(models) >= 15
    assert len(guids) == len(models)  # all distinct
    for model in models:
        assert model.glb_bytes[:4] == b"glTF"
        assert struct.unpack_from("<I", model.glb_bytes, 4)[0] == 2

    assert len(placements) >= 30

    # Every placement is at the airport and has a sane heading.
    for placement in placements:
        distance_km = math.hypot(
            (placement.latitude - _KRDM_LAT) * 111.32,
            (placement.longitude - _KRDM_LON)
            * 111.32
            * math.cos(math.radians(_KRDM_LAT)),
        )
        assert distance_km < 5.0
        assert 0.0 <= placement.heading_degrees_true < 360.0


@_needs_krdm
def test_krdm_placements_resolve_to_package_models():
    # All 21 model GUIDs must be referenced by placements (this pins the
    # GUID byte order).  NOTE: only ~10.6% (95/893) of placements resolve
    # to package models -- the rest reference MSFS stock library objects.
    # That is far below the 60% figure in the feature brief; it reflects
    # the real content of this package, not a parsing defect.
    models, placements, _warnings = MSFS.read_package(_KRDM)
    model_guids = {m.guid for m in models}
    placement_guids = {p.guid for p in placements}
    assert model_guids <= placement_guids  # every model is placed
    resolving = [p for p in placements if p.guid in model_guids]
    assert len(resolving) >= 90
