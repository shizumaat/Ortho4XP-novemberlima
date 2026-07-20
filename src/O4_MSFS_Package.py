"""Read a Microsoft Flight Simulator scenery package.

This is the *input* side of the "Convert MSFS airport" feature (the
orchestrator is :mod:`O4_MSFS_Airport_Convert`).  Two jobs:

* carve the embedded glTF-binary (``.glb``) models -- together with their
  GUIDs -- out of compiled *model-library* BGL files, and
* parse the object *placements* (which GUID goes where, at what heading)
  out of *scenery* BGL files.

Everything is derived empirically from a real package
(``KRDM_Redmond`` / bullfrogsim) and the byte-precise layouts found are
documented below.  Only the Python standard library is used
(``struct``, ``json`` is not needed, ``hashlib``, ``pathlib``,
``dataclasses``); no GUI toolkit is imported.

Build-time impact: none -- this module is not part of the per-tile build
pipeline, it only runs on demand from the Tools menu.

--------------------------------------------------------------------------
BGL container (FSX / MSFS "FS9-era" format)
--------------------------------------------------------------------------
* ``uint32`` magic ``0x19920201`` at offset ``0``.
* ``uint32`` section count at offset ``0x14``.
* Section table starts at offset ``0x38``; each entry is ``0x14`` bytes:
  ``(uint32 type, uint32 flags, uint32 subsection_count,
     uint32 subsection_offset, uint32 subsection_size)``.
  ``subsection_offset`` is an *absolute* file offset.
* Each subsection record is 16 bytes:
  ``(uint32 reserved_or_qmid, uint32 record_count,
     uint32 data_offset, uint32 data_size)``.
  ``data_offset`` / ``data_size`` are absolute file offsets/lengths of the
  section's payload.

--------------------------------------------------------------------------
Model library -- section type 0x2b (ModelData)
--------------------------------------------------------------------------
The section payload begins with a flat index of ``record_count`` entries,
each 24 bytes::

    GUID           16 bytes   (Microsoft mixed-endian layout, see below)
    blob_offset    uint32     offset of the model blob, RELATIVE to the
                              section payload start (data_offset)
    blob_size      uint32     length of the model blob

Each blob is a RIFF container whose form FourCC reads ``GLTF`` -- a
misnomer: the container itself is not glTF data.  Its chunks are
``GXML`` (an XML ``<ModelInfo guid="{...}" .../>`` descriptor whose GUID
equals the index GUID), ``GLBD`` (a collection of binary-glTF payloads,
one per LOD), and ``GLB\\0`` wrappers around each actual binary-glTF
(``glTF`` magic, ``version == 2``, first chunk ``JSON``).  We locate the
``glTF`` magic inside the blob, read the total length from its header,
and slice out exactly that many bytes -- which works regardless of the
container's FourCC labels.

Verified on ``rdm4.BGL``: 21 blobs, every extracted GLB has magic
``glTF`` / version 2 / JSON first chunk, and every index GUID matches its
embedded ``<ModelInfo guid=...>`` string exactly.

--------------------------------------------------------------------------
Scenery placements -- section type 0x25 (SceneryObject)
--------------------------------------------------------------------------
The section payload is a stream of records; each record starts with
``(uint16 record_type, uint16 record_size)`` and is ``record_size`` bytes
long.  ``record_type == 0x0b`` is a *LibraryObject* placement.  The
LibraryObject record (64 bytes in the sample) is::

    0x00  uint16  record_type = 0x0b
    0x02  uint16  record_size
    0x04  uint32  longitude   (see formula)
    0x08  uint32  latitude    (see formula)
    0x0c  int32   altitude    (millimetres; see note)
    0x10  uint16  flags       (bit 0 => altitude is above ground)
    0x12  uint16  pitch       raw * 360 / 2**16
    0x14  uint16  bank        raw * 360 / 2**16
    0x16  uint16  heading     raw * 360 / 2**16
    0x18  uint16  image_complexity
    ...   (reserved / padding, zero in the sample)
    0x2c  GUID   16 bytes   (same layout & byte order as the model index)
    0x3c  float  scale

The GUID and scale sit at the *fixed* offsets ``0x2c`` and ``0x3c``.  In
the 64-byte sample records those coincide with ``record_size - 20`` /
``record_size - 4``, but the offsets must not be derived from the record
size: ``AttachedObject`` (0x1002) sub-records may extend the record
beyond 64 bytes, and reading relative to its end then lands in the
sub-record data.

Other 0x25 record types seen in the wild -- 0x0a GenericBuilding,
0x0c Windsock, 0x0d Effect, 0x0e TaxiwaySign, 0x12 ExtrusionBridge --
are not converted; the reader counts them per source BGL and reports the
counts as warnings instead of skipping them silently.

Coordinate formulas (EMPIRICALLY DETERMINED for MSFS -- these deviate
from the classic FSX ``raw * 360 / 2**32 - 180`` mapping):

    latitude_degrees  = 90.0 - raw * (180.0 / 2**29)
    longitude_degrees = raw * (240.0 / 2**29) - 180.0

Latitude uses ``180 / 2**29``; longitude uses ``240 / 2**29`` (i.e. 4/3 of
the latitude scale, base -180).  These were fitted against the known
airport reference point (44.253 N, -121.161 W): all 893 placements land
within 2.22 km of it and every GUID that also exists in the model library
resolves, which pins both the field layout and the byte order.

Altitude note: 866 of 893 placements store 0; the non-zero values
(a few metres, one ~16 m tower, a couple of negatives) are consistent with
millimetres, so we divide by 1000.  The overlay-DSF writer maps a
non-zero altitude to ``OBJECT_AGL`` (flag bit 0 set) or ``OBJECT_MSL``
(clear); zero-altitude placements stay ground-draped ``OBJECT`` rows.

GUID string form: the 16 raw bytes are formatted in canonical Microsoft
layout with lower-case hex and no braces/dashes -- ``uint32`` + ``uint16``
+ ``uint16`` little-endian for the first three fields, then the trailing 8
bytes verbatim.  This matches the embedded ``<ModelInfo guid=...>`` value,
and because the model index and the placement records store identical raw
bytes, model GUIDs and placement GUIDs compare equal.
"""
from __future__ import annotations

import hashlib
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Container / section constants
# ---------------------------------------------------------------------------
_BGL_MAGIC = 0x19920201
_SECTION_COUNT_OFFSET = 0x14
_SECTION_TABLE_OFFSET = 0x38
_SECTION_ENTRY_SIZE = 0x14
_SUBSECTION_RECORD_SIZE = 16

_SECTION_TYPE_MODEL_DATA = 0x2B
_SECTION_TYPE_SCENERY_OBJECT = 0x25

_MODEL_INDEX_RECORD_SIZE = 24
_RECORD_TYPE_LIBRARY_OBJECT = 0x0B

# LibraryObject field offsets (fixed; see the module docstring -- the
# record may be extended past 64 bytes by AttachedObject sub-records, so
# these must never be derived from the record size).
_LIBRARY_OBJECT_GUID_OFFSET = 0x2C
_LIBRARY_OBJECT_SCALE_OFFSET = 0x3C
_LIBRARY_OBJECT_MIN_SIZE = 64

# Known-but-unconverted 0x25 record types, named for the skip warnings.
_SCENERY_RECORD_TYPE_NAMES = {
    0x0A: "GenericBuilding",
    0x0C: "Windsock",
    0x0D: "Effect",
    0x0E: "TaxiwaySign",
    0x12: "ExtrusionBridge",
}

# Coordinate / angle scaling (empirically determined for MSFS).
_LATITUDE_SCALE = 180.0 / 2 ** 29
_LONGITUDE_SCALE = 240.0 / 2 ** 29
_ANGLE_SCALE = 360.0 / 2 ** 16
_ALTITUDE_MILLIMETRES_PER_METRE = 1000.0


@dataclass(frozen=True)
class ModelEntry:
    """One glTF model carved out of a model-library BGL."""

    guid: str
    glb_bytes: bytes
    source_bgl: str
    texture_directory: Optional[str]


@dataclass(frozen=True)
class ObjectPlacement:
    """One placed library object parsed from a scenery BGL."""

    guid: str
    latitude: float
    longitude: float
    altitude_meters: float
    is_above_ground: bool
    heading_degrees_true: float
    pitch_degrees: float
    bank_degrees: float
    scale: float
    source_bgl: str


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _guid_to_string(raw: bytes) -> str:
    """Format 16 GUID bytes as lower-case hex, no braces or dashes.

    Uses the canonical Microsoft mixed-endian layout so the string matches
    the GUID printed in tools and in the embedded ``<ModelInfo>`` XML.
    """
    if len(raw) != 16:
        raise ValueError("a GUID must be exactly 16 bytes")
    data1 = struct.unpack_from("<I", raw, 0)[0]
    data2 = struct.unpack_from("<H", raw, 4)[0]
    data3 = struct.unpack_from("<H", raw, 6)[0]
    return f"{data1:08x}{data2:04x}{data3:04x}{raw[8:16].hex()}"


def _synthetic_guid(blob: bytes) -> str:
    """Fallback identifier when a blob's GUID cannot be recovered."""
    return "noguid" + hashlib.sha1(blob).hexdigest()[:24]


def _read_bgl_sections(data: bytes) -> List[Tuple[int, int, int, int, int]]:
    """Return ``(type, flags, sub_count, sub_offset, sub_size)`` per section.

    Returns an empty list if the file is not a recognisable BGL.
    """
    if len(data) < _SECTION_TABLE_OFFSET + _SECTION_ENTRY_SIZE:
        return []
    if struct.unpack_from("<I", data, 0)[0] != _BGL_MAGIC:
        return []
    section_count = struct.unpack_from("<I", data, _SECTION_COUNT_OFFSET)[0]
    sections: List[Tuple[int, int, int, int, int]] = []
    for index in range(section_count):
        entry_offset = _SECTION_TABLE_OFFSET + index * _SECTION_ENTRY_SIZE
        if entry_offset + _SECTION_ENTRY_SIZE > len(data):
            break
        sections.append(struct.unpack_from("<IIIII", data, entry_offset))
    return sections


def _iter_subsections(data: bytes, section: Tuple[int, int, int, int, int]):
    """Yield ``(data_offset, data_size)`` for each subsection of a section."""
    _type, _flags, sub_count, sub_offset, _sub_size = section
    for index in range(sub_count):
        record_offset = sub_offset + index * _SUBSECTION_RECORD_SIZE
        if record_offset + _SUBSECTION_RECORD_SIZE > len(data):
            break
        _reserved, _count, payload_offset, payload_size = struct.unpack_from(
            "<IIII", data, record_offset
        )
        yield payload_offset, payload_size


def _extract_glb_from_blob(blob: bytes) -> Optional[bytes]:
    """Slice the binary-glTF out of a RIFF model blob, or return None.

    Locates the ``glTF`` magic, checks ``version == 2`` and that the first
    chunk is ``JSON``, then returns exactly ``total_length`` bytes.
    """
    magic_index = blob.find(b"glTF")
    if magic_index < 0 or magic_index + 20 > len(blob):
        return None
    version = struct.unpack_from("<I", blob, magic_index + 4)[0]
    total_length = struct.unpack_from("<I", blob, magic_index + 8)[0]
    if version != 2:
        return None
    if magic_index + total_length > len(blob) or total_length < 20:
        return None
    first_chunk_type = blob[magic_index + 16 : magic_index + 20]
    if first_chunk_type != b"JSON":
        return None
    return blob[magic_index : magic_index + total_length]


def _sibling_texture_directory(bgl_path: Path) -> Optional[str]:
    """Return the BGL's sibling ``texture`` folder (case-insensitive)."""
    parent = bgl_path.parent
    if not parent.is_dir():
        return None
    for child in parent.iterdir():
        if child.is_dir() and child.name.lower() == "texture":
            return str(child)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def find_bgl_files(package_directory: Path) -> List[Path]:
    """Return every ``.bgl`` file under *package_directory* (recursive).

    The match is case-insensitive on the extension; the result is sorted
    for deterministic ordering.
    """
    package_directory = Path(package_directory)
    if not package_directory.is_dir():
        return []
    return sorted(
        path
        for path in package_directory.rglob("*")
        if path.is_file() and path.suffix.lower() == ".bgl"
    )


def read_model_library(bgl_path: Path) -> List[ModelEntry]:
    """Carve every embedded glTF model (with its GUID) out of *bgl_path*.

    Returns an empty list if the file holds no model-data section.
    """
    bgl_path = Path(bgl_path)
    try:
        data = bgl_path.read_bytes()
    except OSError:
        return []
    sections = _read_bgl_sections(data)
    if not sections:
        return []

    texture_directory = _sibling_texture_directory(bgl_path)
    source = str(bgl_path)
    entries: List[ModelEntry] = []
    for section in sections:
        if section[0] != _SECTION_TYPE_MODEL_DATA:
            continue
        for payload_offset, payload_size in _iter_subsections(data, section):
            payload_end = min(payload_offset + payload_size, len(data))
            record_offset = payload_offset
            while record_offset + _MODEL_INDEX_RECORD_SIZE <= payload_end:
                guid_bytes = data[record_offset : record_offset + 16]
                blob_offset, blob_size = struct.unpack_from(
                    "<II", data, record_offset + 16
                )
                # The first blob sits immediately after the index, so an
                # index record whose blob would start inside the index area
                # (or past the file) marks the end of the index table.
                blob_start = payload_offset + blob_offset
                if blob_offset < _MODEL_INDEX_RECORD_SIZE:
                    break
                if blob_start + blob_size > len(data) or blob_size <= 0:
                    break
                blob = data[blob_start : blob_start + blob_size]
                glb = _extract_glb_from_blob(blob)
                if glb is not None:
                    guid = _guid_to_string(guid_bytes)
                    entries.append(
                        ModelEntry(
                            guid=guid,
                            glb_bytes=glb,
                            source_bgl=source,
                            texture_directory=texture_directory,
                        )
                    )
                record_offset += _MODEL_INDEX_RECORD_SIZE
    return entries


def read_object_placements(
    bgl_path: Path, warnings: Optional[List[str]] = None
) -> List[ObjectPlacement]:
    """Parse library-object placements out of scenery BGL *bgl_path*.

    Returns an empty list if the file holds no scenery-object section.
    When ``warnings`` is given, unconverted 0x25 record types (windsocks,
    taxiway signs, effects, ...) are counted and reported into it instead
    of being skipped silently.
    """
    bgl_path = Path(bgl_path)
    try:
        data = bgl_path.read_bytes()
    except OSError:
        return []
    sections = _read_bgl_sections(data)
    if not sections:
        return []

    source = str(bgl_path)
    placements: List[ObjectPlacement] = []
    skipped_type_counts: dict[int, int] = {}
    for section in sections:
        if section[0] != _SECTION_TYPE_SCENERY_OBJECT:
            continue
        for payload_offset, payload_size in _iter_subsections(data, section):
            payload_end = min(payload_offset + payload_size, len(data))
            record_offset = payload_offset
            while record_offset + 4 <= payload_end:
                record_type, record_size = struct.unpack_from(
                    "<HH", data, record_offset
                )
                if record_size < 4:
                    break
                if record_offset + record_size > payload_end:
                    break
                if (
                    record_type == _RECORD_TYPE_LIBRARY_OBJECT
                    and record_size >= _LIBRARY_OBJECT_MIN_SIZE
                ):
                    placements.append(
                        _parse_library_object(data, record_offset, source)
                    )
                else:
                    skipped_type_counts[record_type] = (
                        skipped_type_counts.get(record_type, 0) + 1
                    )
                record_offset += record_size
    if warnings is not None and skipped_type_counts:
        parts = ", ".join(
            "{} x {}".format(
                count,
                _SCENERY_RECORD_TYPE_NAMES.get(
                    record_type, "type 0x{:02x}".format(record_type)
                ),
            )
            for record_type, count in sorted(skipped_type_counts.items())
        )
        warnings.append(
            f"{bgl_path.name}: skipped unconverted scenery records: {parts}"
        )
    return placements


def _parse_library_object(
    data: bytes, offset: int, source: str
) -> ObjectPlacement:
    """Decode one LibraryObject record into an :class:`ObjectPlacement`."""
    longitude_raw = struct.unpack_from("<I", data, offset + 4)[0]
    latitude_raw = struct.unpack_from("<I", data, offset + 8)[0]
    altitude_raw = struct.unpack_from("<i", data, offset + 12)[0]
    flags = struct.unpack_from("<H", data, offset + 16)[0]
    pitch_raw = struct.unpack_from("<H", data, offset + 18)[0]
    bank_raw = struct.unpack_from("<H", data, offset + 20)[0]
    heading_raw = struct.unpack_from("<H", data, offset + 22)[0]
    # GUID and scale live at fixed offsets; AttachedObject (0x1002)
    # sub-records may extend the record beyond 64 bytes, so end-relative
    # reads would land in sub-record data.
    guid_bytes = data[
        offset + _LIBRARY_OBJECT_GUID_OFFSET
        : offset + _LIBRARY_OBJECT_GUID_OFFSET + 16
    ]
    scale = struct.unpack_from(
        "<f", data, offset + _LIBRARY_OBJECT_SCALE_OFFSET
    )[0]

    longitude = longitude_raw * _LONGITUDE_SCALE - 180.0
    latitude = 90.0 - latitude_raw * _LATITUDE_SCALE
    return ObjectPlacement(
        guid=_guid_to_string(guid_bytes),
        latitude=latitude,
        longitude=longitude,
        altitude_meters=altitude_raw / _ALTITUDE_MILLIMETRES_PER_METRE,
        is_above_ground=bool(flags & 0x01),
        heading_degrees_true=(heading_raw * _ANGLE_SCALE) % 360.0,
        pitch_degrees=pitch_raw * _ANGLE_SCALE,
        bank_degrees=bank_raw * _ANGLE_SCALE,
        scale=scale,
        source_bgl=source,
    )


def read_package(
    package_directory: Path,
) -> Tuple[List[ModelEntry], List[ObjectPlacement], List[str]]:
    """Read every BGL in *package_directory*.

    Returns ``(models, placements, warnings)``.  Models are de-duplicated
    by GUID, keeping the entry with the largest ``glb_bytes`` (later,
    higher-detail definitions win over stubs).
    """
    package_directory = Path(package_directory)
    warnings: List[str] = []
    bgl_files = find_bgl_files(package_directory)
    if not bgl_files:
        warnings.append(f"no .bgl files found under {package_directory}")

    best_by_guid: dict[str, ModelEntry] = {}
    placements: List[ObjectPlacement] = []
    for bgl_path in bgl_files:
        try:
            models = read_model_library(bgl_path)
        except Exception as error:  # pragma: no cover - defensive
            warnings.append(f"failed to read model library {bgl_path}: {error}")
            models = []
        for entry in models:
            existing = best_by_guid.get(entry.guid)
            if existing is None or len(entry.glb_bytes) > len(existing.glb_bytes):
                best_by_guid[entry.guid] = entry

        try:
            placements.extend(read_object_placements(bgl_path, warnings))
        except Exception as error:  # pragma: no cover - defensive
            warnings.append(f"failed to read placements {bgl_path}: {error}")

    models = sorted(best_by_guid.values(), key=lambda entry: entry.guid)
    return models, placements, warnings


def stage_texture_directory(entry: ModelEntry, staging_directory: Path) -> None:
    """Make *entry*'s textures resolve next to a staged ``.glb``.

    Every file in ``entry.texture_directory`` is symlinked (falling back to
    a copy) into *staging_directory* so relative texture URIs resolve.
    Does nothing if the model has no texture directory, and never fails if
    the target files already exist.
    """
    if entry.texture_directory is None:
        return
    texture_directory = Path(entry.texture_directory)
    if not texture_directory.is_dir():
        return
    staging_directory = Path(staging_directory)
    staging_directory.mkdir(parents=True, exist_ok=True)
    for source in texture_directory.iterdir():
        if not source.is_file():
            continue
        target = staging_directory / source.name
        if target.exists() or target.is_symlink():
            continue
        try:
            target.symlink_to(source.resolve())
        except (OSError, NotImplementedError):
            try:
                shutil.copy2(source, target)
            except OSError:
                # A single unstageable texture must not abort the run.
                continue
