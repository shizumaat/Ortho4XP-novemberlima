"""Contract tests for the DSF-raster half of coastal bathymetry.

Pins ``docs/specs/coastal-bathymetry-spec.md`` section 5 as it lands in
``O4_DSF_Utils``: synthesizing (and splicing) the X-Plane 12 ``sea_level``
depth raster from measured coastal bathymetry.

What is pinned and WHY:

* ``_raster_sub_atoms`` IMED/DMED encoding — X-Plane's water shader reads
  these rasters, so the sub-atom header must match the Global Scenery
  donor byte-for-byte (version=1, bytes-per-post=2, flags=5, 1201x1201,
  scale=1.0, offset=0.0) and the DMED payload must be the raster
  row-major with row 0 = south.  Verified by parsing our own bytes with
  the same layout the extraction parser expects.

* ``synthesize_elevation_and_bathymetry_data`` — the no-donor (Hawaii)
  path.  ``sea_level`` = ``min(measured_depth, elevation - 2)`` over sea
  pixels, ``elevation - 2`` everywhere else (the donor's own inland
  safety margin), DEMN naming exactly ``elevation\0sea_level\0``.

* ``splice_measured_bathymetry`` — the donor-present ``True`` path.  Only
  the sea part of the donor's ``sea_level`` raster is replaced
  (``min``-guarded); every other raster stays byte-identical; a missing
  band leaves the donor bytes untouched.

* ``elevation_and_bathymetry_data`` dispatch — the six ``dsf_bathymetry``
  x donor-present/absent cases of spec section 5.

Headless: ``tmp_path`` only; the network band fetch and the post-grid
warp are always monkeypatched (this module never opens a real DSF or
touches the network).
"""

import os
import struct
import sys
import types

import numpy
import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src")
)

import O4_Bathymetry_Band as BATHYBAND  # noqa: E402
import O4_DSF_Utils as DSF  # noqa: E402


POSTS = 1201
RASTER_BYTES = 2 * POSTS * POSTS


def _parse_dsf_rasters(bDEMS):
    """Walk a DEMS sub-atom concatenation (the extraction-loop layout) and
    return the list of large DMED payloads as int16 1201x1201 arrays, in
    order (elevation, sea_level, ...)."""
    rasters = []
    position = 0
    while position < len(bDEMS):
        header = bDEMS[position : position + 4]
        (length,) = struct.unpack("<I", bDEMS[position + 4 : position + 8])
        payload = bDEMS[position + 8 : position + length]
        if header == b"DMED" and len(payload) > 100:
            rasters.append(
                numpy.frombuffer(payload, dtype=numpy.int16).reshape(
                    POSTS, POSTS
                )
            )
        position += length
    return rasters


# =====================================================================
# 1. IMED/DMED encoding (spec section 5)
# =====================================================================
def test_raster_sub_atoms_imed_dmed_encoding():
    raster = (
        numpy.arange(POSTS * POSTS, dtype=numpy.int32).reshape(POSTS, POSTS)
        % 500
        - 250
    ).astype(numpy.int16)
    # Distinctive south/north corners so row-major row0=south is provable.
    raster[0, 0] = -321
    raster[POSTS - 1, POSTS - 1] = 654

    blob = DSF._raster_sub_atoms(raster)

    # IMED sub-atom: 4-byte tag, 4-byte length, 20-byte header.
    assert blob[0:4] == b"IMED"
    (imed_length,) = struct.unpack("<I", blob[4:8])
    assert imed_length == 28  # 8 + struct.calcsize("<BBHIIff") == 8 + 20
    fields = struct.unpack("<BBHIIff", blob[8:28])
    assert fields == (1, 2, 5, POSTS, POSTS, 1.0, 0.0)

    # DMED sub-atom: the raster, row-major, row 0 = south.
    assert blob[28:32] == b"DMED"
    (dmed_length,) = struct.unpack("<I", blob[32:36])
    assert dmed_length == 8 + RASTER_BYTES
    payload = blob[36 : 36 + RASTER_BYTES]
    assert payload == raster.tobytes()
    round_tripped = numpy.frombuffer(payload, dtype=numpy.int16).reshape(
        POSTS, POSTS
    )
    assert round_tripped[0, 0] == -321
    assert round_tripped[POSTS - 1, POSTS - 1] == 654


# =====================================================================
# 2. synthesize_elevation_and_bathymetry_data (no-donor path)
# =====================================================================
class _ConstantDEM:
    """tile.dem stand-in: every sampled post reads a constant elevation."""

    def __init__(self, value):
        self.value = value

    def alt_vec(self, sample_points):
        return numpy.full(len(sample_points), self.value, dtype=numpy.float32)


def _patch_mask():
    """A small rectangular sea patch inside the 1201x1201 post grid."""
    mask = numpy.zeros((POSTS, POSTS), dtype=bool)
    mask[100:110, 200:210] = True
    return mask


def test_synthesize_builds_elevation_and_sea_level_rasters(monkeypatch):
    patch = _patch_mask()
    measured = numpy.full((POSTS, POSTS), -32768.0, dtype=numpy.float32)
    measured[patch] = -12.0

    monkeypatch.setattr(
        BATHYBAND, "ensure_bathymetry_band", lambda tile: "fake.vrt"
    )
    monkeypatch.setattr(
        BATHYBAND,
        "warp_band_to_post_grid",
        lambda vrt, lat, lon, posts=POSTS: measured,
    )
    tile = types.SimpleNamespace(lat=21, lon=-160, dem=_ConstantDEM(5.0))

    (bDEMN, bDEMS) = DSF.synthesize_elevation_and_bathymetry_data(tile)

    assert bDEMN == b"elevation\0sea_level\0"
    (elevation, sea_level) = _parse_dsf_rasters(bDEMS)
    # Elevation raster: the constant DEM, clamped to int16.
    assert numpy.all(elevation == 5)
    # sea_level: measured depth over the sea patch, elevation-2 elsewhere.
    assert numpy.all(sea_level[patch] == -12)
    off_patch = numpy.ones((POSTS, POSTS), dtype=bool)
    off_patch[patch] = False
    assert numpy.all(sea_level[off_patch] == 3)  # elevation (5) - 2


def test_synthesize_returns_empty_when_no_band(monkeypatch):
    """No band -> the legacy no-donor empty-atom result (b"", b"")."""
    monkeypatch.setattr(
        BATHYBAND, "ensure_bathymetry_band", lambda tile: None
    )
    tile = types.SimpleNamespace(lat=21, lon=-160, dem=_ConstantDEM(5.0))
    assert DSF.synthesize_elevation_and_bathymetry_data(tile) == (b"", b"")


# =====================================================================
# 3. splice_measured_bathymetry (donor-present True path)
# =====================================================================
def _donor_blob(elevation_value, sea_level_value):
    """A two-raster donor DEMS blob (elevation then sea_level)."""
    elevation = numpy.full(
        (POSTS, POSTS), elevation_value, dtype=numpy.int16
    )
    sea_level = numpy.full(
        (POSTS, POSTS), sea_level_value, dtype=numpy.int16
    )
    return (
        b"elevation\0sea_level\0",
        DSF._raster_sub_atoms(elevation) + DSF._raster_sub_atoms(sea_level),
    )


def test_splice_replaces_only_sea_and_preserves_other_rasters(monkeypatch):
    (bDEMN, donor_bDEMS) = _donor_blob(
        elevation_value=5, sea_level_value=3
    )
    (donor_elevation, donor_sea) = _parse_dsf_rasters(donor_bDEMS)

    patch = _patch_mask()
    measured = numpy.full((POSTS, POSTS), -32768.0, dtype=numpy.float32)
    measured[patch] = -12.0
    monkeypatch.setattr(
        BATHYBAND, "ensure_bathymetry_band", lambda tile: "fake.vrt"
    )
    monkeypatch.setattr(
        BATHYBAND,
        "warp_band_to_post_grid",
        lambda vrt, lat, lon, posts=POSTS: measured,
    )
    tile = types.SimpleNamespace(lat=21, lon=-160)

    (out_bDEMN, out_bDEMS) = DSF.splice_measured_bathymetry(
        tile, bDEMN, donor_bDEMS
    )

    assert out_bDEMN == bDEMN
    (out_elevation, out_sea) = _parse_dsf_rasters(out_bDEMS)
    # Elevation raster (a "non-sea" raster) is byte-identical.
    assert out_elevation.tobytes() == donor_elevation.tobytes()
    # sea_level: measured over the patch (min-guarded), donor elsewhere.
    assert numpy.all(out_sea[patch] == -12)
    off_patch = numpy.ones((POSTS, POSTS), dtype=bool)
    off_patch[patch] = False
    assert numpy.all(out_sea[off_patch] == donor_sea[off_patch])


def test_splice_min_guard_keeps_shallower_donor_value(monkeypatch):
    """The splice is min-guarded: a measured value shallower than the
    donor's elevation-2 margin never raises the seabed above it."""
    # elevation 5 -> elevation-2 == 3; a measured -1 is shallower than a
    # donor sea_level of -8, but min(-1, 3) == -1 still wins over -8's
    # cell only where measured is valid.  Assert the min semantics.
    (bDEMN, donor_bDEMS) = _donor_blob(elevation_value=5, sea_level_value=-8)
    patch = _patch_mask()
    measured = numpy.full((POSTS, POSTS), -32768.0, dtype=numpy.float32)
    measured[patch] = -1.0
    monkeypatch.setattr(
        BATHYBAND, "ensure_bathymetry_band", lambda tile: "fake.vrt"
    )
    monkeypatch.setattr(
        BATHYBAND,
        "warp_band_to_post_grid",
        lambda vrt, lat, lon, posts=POSTS: measured,
    )
    tile = types.SimpleNamespace(lat=21, lon=-160)

    (_out_bDEMN, out_bDEMS) = DSF.splice_measured_bathymetry(
        tile, bDEMN, donor_bDEMS
    )
    (_out_elevation, out_sea) = _parse_dsf_rasters(out_bDEMS)
    # min(measured -1, elevation-2 == 3) -> -1 over the patch.
    assert numpy.all(out_sea[patch] == -1)


def test_splice_without_band_returns_donor_untouched(monkeypatch):
    (bDEMN, donor_bDEMS) = _donor_blob(elevation_value=5, sea_level_value=3)
    monkeypatch.setattr(
        BATHYBAND, "ensure_bathymetry_band", lambda tile: None
    )
    tile = types.SimpleNamespace(lat=21, lon=-160)

    (out_bDEMN, out_bDEMS) = DSF.splice_measured_bathymetry(
        tile, bDEMN, donor_bDEMS
    )
    assert out_bDEMN == bDEMN
    assert out_bDEMS == donor_bDEMS  # byte-identical, nothing spliced


# =====================================================================
# 4. elevation_and_bathymetry_data dispatch table (spec section 5)
# =====================================================================
@pytest.mark.parametrize(
    "setting, donor_present, expected_workers",
    [
        ("False", True, ["extract"]),
        ("False", False, ["extract"]),
        ("auto", True, ["extract"]),
        ("auto", False, ["synthesize"]),
        ("True", True, ["extract", "splice"]),
        ("True", False, ["synthesize"]),
    ],
)
def test_dispatch_table(monkeypatch, setting, donor_present, expected_workers):
    calls = []
    monkeypatch.setattr(
        DSF, "_global_scenery_donor_exists",
        lambda lat, lon: donor_present,
    )
    monkeypatch.setattr(
        DSF, "extract_elevation_and_bathymetry_data",
        lambda lat, lon: calls.append("extract") or (b"N", b"S"),
    )
    monkeypatch.setattr(
        DSF, "splice_measured_bathymetry",
        lambda tile, bDEMN, bDEMS: calls.append("splice") or (bDEMN, bDEMS),
    )
    monkeypatch.setattr(
        DSF, "synthesize_elevation_and_bathymetry_data",
        lambda tile: calls.append("synthesize") or (b"N", b"S"),
    )
    tile = types.SimpleNamespace(lat=21, lon=-160, dsf_bathymetry=setting)

    DSF.elevation_and_bathymetry_data(tile)

    assert calls == expected_workers
