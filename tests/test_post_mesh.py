"""Post-mesh DSF object re-anchor stage — workstream W7 of the DSF
object integration (``docs/dsf_object_integration_spec.md`` section
4-W7, as amended by A4/A5/A13).

Everything is hermetic under ``tmp_path``: a fake tile is a
``types.SimpleNamespace``, the scenery pack and its ``.dsf`` are
synthetic (harness pattern (b) from ``tests/test_dsf_buildings.py`` —
fake ``.dsf`` plus a pre-seeded, mtime-backdated ``.dsf.text`` and a
monkeypatched ``_dsftool_path``), and the mesh is a small hand-written
two-triangle plane in the exact ``O4_Mesh_Utils.write_mesh_file`` format
(see ``tests/test_mesh_sampler.py``'s fixture).  No real X-Plane packs
or meshes are ever touched.

The synthetic terrain is a plane whose elevation depends only on the
longitude, so the expected per-structure offset is computable in closed
form: ``delta = (centroid_longitude - anchor_longitude) * SLOPE``.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import types

import pytest

import O4_File_Names as FNAMES
import O4_Mesh_Utils
import O4_UI_Utils as UI
from auto_patch import config
from auto_patch import driver
from auto_patch import dsf_reader as D
from auto_patch import obj8_reader, post_mesh

TILE_LATITUDE = 35
TILE_LONGITUDE = -81
TILE_NAME = "+35-081"

ANCHOR_LATITUDE = 35.21
ANCHOR_LONGITUDE = -80.93

# The synthetic mesh: one square split into two triangles, elevation a
# linear function of the longitude alone (barycentric interpolation of a
# linear function is exact, so the sampler reproduces the plane).
MESH_WEST_LONGITUDE = ANCHOR_LONGITUDE - 0.002
MESH_EAST_LONGITUDE = ANCHOR_LONGITUDE + 0.002
MESH_SOUTH_LATITUDE = ANCHOR_LATITUDE - 0.002
MESH_NORTH_LATITUDE = ANCHOR_LATITUDE + 0.002
ELEVATION_SLOPE_PER_DEGREE = 10000.0
BASE_ELEVATION = 100.0


def _plane_elevation(longitude: float) -> float:
    return BASE_ELEVATION + (
        longitude - MESH_WEST_LONGITUDE
    ) * ELEVATION_SLOPE_PER_DEGREE


def _write_synthetic_mesh(mesh_path: str) -> None:
    corner_positions = [
        (MESH_WEST_LONGITUDE, MESH_SOUTH_LATITUDE),
        (MESH_EAST_LONGITUDE, MESH_SOUTH_LATITUDE),
        (MESH_EAST_LONGITUDE, MESH_NORTH_LATITUDE),
        (MESH_WEST_LONGITUDE, MESH_NORTH_LATITUDE),
    ]
    lines = ["MeshVersionFormatted 2", "Dimension 3", "", "Vertices", "4"]
    for longitude, latitude in corner_positions:
        scaled_elevation = _plane_elevation(longitude) / 100000.0
        lines.append(
            f"{longitude:.15f} {latitude:.15f} {scaled_elevation:.15f} 0"
        )
    lines += ["", "Normals", "0", "", "Triangles", "2",
              "1 2 3 0", "1 3 4 0"]
    os.makedirs(os.path.dirname(mesh_path), exist_ok=True)
    with open(mesh_path, "w") as handle:
        handle.write("\n".join(lines) + "\n")


# A 10 x 10 metre slab whose geometry sits 30..40 m east and south of
# its anchor (solid reach ~56.6 m, past the 25 m detector floor), at
# authored y = 0 — the offset-geometry case Phase 2 exists for.
OFFSET_SLAB_OBJECT = "\n".join([
    "A",
    "800",
    "OBJ",
    "",
    "POINT_COUNTS 4 0 0 6",
    "VT 30.000000 0.000000 30.000000 0.0 1.0 0.0 0.0 0.0",
    "VT 40.000000 0.000000 30.000000 0.0 1.0 0.0 0.0 0.0",
    "VT 40.000000 0.000000 40.000000 0.0 1.0 0.0 0.0 0.0",
    "VT 30.000000 0.000000 40.000000 0.0 1.0 0.0 0.0 0.0",
    "IDX10 0 1 2 0 2 3",
    "TRIS 0 6",
]) + "\n"

# The slab's local centroid — used to compute the expected offset.
SLAB_LOCAL_CENTROID_EAST = 35.0
SLAB_LOCAL_CENTROID_SOUTH = 35.0


def _expected_slab_offset() -> float:
    _centroid_latitude, centroid_longitude = (
        obj8_reader.local_offset_to_lonlat(
            ANCHOR_LATITUDE,
            ANCHOR_LONGITUDE,
            0.0,
            SLAB_LOCAL_CENTROID_EAST,
            SLAB_LOCAL_CENTROID_SOUTH,
        )
    )
    return (
        centroid_longitude - ANCHOR_LONGITUDE
    ) * ELEVATION_SLOPE_PER_DEGREE


def _make_pack(base_directory, pack_name: str, dsf_body: str,
               objects_by_resource: dict) -> tuple[str, str]:
    """Harness pattern (b): fake ``.dsf`` + pre-seeded, backdated
    ``.dsf.text`` under ``<pack>/Earth nav data/<group>/``, plus the
    pack's ``.obj`` files.  Returns ``(dsf_path, pack_root)``."""
    pack_root = base_directory / pack_name
    dsf_directory = pack_root / "Earth nav data" / "+30-090"
    dsf_directory.mkdir(parents=True)
    dsf = dsf_directory / (TILE_NAME + ".dsf")
    dsf.write_text("binary-placeholder")
    text = dsf_directory / (TILE_NAME + ".dsf.text")
    text.write_text(dsf_body)
    now = os.path.getmtime(text)
    os.utime(dsf, (now - 10, now - 10))
    for resource_path, content in objects_by_resource.items():
        physical = pack_root.joinpath(*resource_path.split("/"))
        physical.parent.mkdir(parents=True, exist_ok=True)
        physical.write_text(content)
    return str(dsf), str(pack_root)


def _vertex_y_values(object_path: str) -> list[float]:
    values = []
    with open(object_path) as handle:
        for line in handle:
            tokens = line.split()
            if tokens and tokens[0] == "VT":
                values.append(float(tokens[2]))
    return values


SINGLE_PLACEMENT_DSF_BODY = "\n".join([
    "OBJECT_DEF objects/offset_bake.obj",
    f"OBJECT 0 {ANCHOR_LONGITUDE:.9f} {ANCHOR_LATITUDE:.9f} 0.000000",
]) + "\n"

# A two-foot gantry with an author-BAKED vertical offset (multi-ground-
# cluster re-anchor, project memory kbna-gantry-pond-multi-foot-objects):
# two vertical foot quads (authored bases +6.5 and +7.7) joined by a
# 40 m deck along the EAST axis — across the synthetic plane's slope,
# so no rigid offset can seat both feet and the west foot must raise a
# terrain-pad request.
def _two_foot_gantry_object(span_metres: float) -> str:
    east_far = span_metres
    east_foot_b = span_metres - 2.0
    return "\n".join([
        "A",
        "800",
        "OBJ",
        "",
        "POINT_COUNTS 12 0 0 18",
        "VT 0.000000 6.500000 0.000000 0.0 1.0 0.0 0.0 0.0",
        "VT 2.000000 6.500000 0.000000 0.0 1.0 0.0 0.0 0.0",
        "VT 2.000000 9.200000 0.000000 0.0 1.0 0.0 0.0 0.0",
        "VT 0.000000 9.200000 0.000000 0.0 1.0 0.0 0.0 0.0",
        "VT 0.000000 9.200000 0.000000 0.0 1.0 0.0 0.0 0.0",
        f"VT {east_far:.6f} 9.200000 0.000000 0.0 1.0 0.0 0.0 0.0",
        f"VT {east_far:.6f} 9.200000 2.000000 0.0 1.0 0.0 0.0 0.0",
        "VT 0.000000 9.200000 2.000000 0.0 1.0 0.0 0.0 0.0",
        f"VT {east_foot_b:.6f} 7.700000 0.000000 0.0 1.0 0.0 0.0 0.0",
        f"VT {east_far:.6f} 7.700000 0.000000 0.0 1.0 0.0 0.0 0.0",
        f"VT {east_far:.6f} 9.200000 0.000000 0.0 1.0 0.0 0.0 0.0",
        f"VT {east_foot_b:.6f} 9.200000 0.000000 0.0 1.0 0.0 0.0 0.0",
        "IDX10 0 1 2 0 2 3 4 5 6 4",
        "IDX10 6 7 8 9 10 8 10 11",
        "TRIS 0 18",
    ]) + "\n"


TWO_FOOT_GANTRY_OBJECT = _two_foot_gantry_object(40.0)

TWO_FOOT_GANTRY_DSF_BODY = "\n".join([
    "OBJECT_DEF objects/gantry.obj",
    f"OBJECT 0 {ANCHOR_LONGITUDE:.9f} {ANCHOR_LATITUDE:.9f} 0.000000",
]) + "\n"


class Harness:
    pass


@pytest.fixture(autouse=True)
def sandbox_ortho4xp_data_root(tmp_path, monkeypatch):
    """The partition sidecar cache lands under the Ortho4XP data root
    (``Airport_mod_cache/<pack>/``), which in a source checkout resolves
    to the current working directory — without this pin the tests here
    would write ``Airport_mod_cache/`` into the repository (same sandbox
    as test_dsf_object_buildings.py)."""
    monkeypatch.setenv(
        "ORTHO4XP_DATA_ROOT", str(tmp_path / "o4_data_root"))


@pytest.fixture()
def phase_two_harness(tmp_path, monkeypatch):
    """A fake tile, a synthetic mesh at ``FNAMES.mesh_file(...)``, a
    Patches directory redirected into ``tmp_path``, the DSFTool
    monkeypatch, and the re-anchor flag ON."""
    monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
    monkeypatch.setattr(config, "DSF_OBJECT_REANCHOR", True)

    patches_directory = tmp_path / "Patches" / TILE_NAME
    patches_directory.mkdir(parents=True)
    monkeypatch.setattr(
        FNAMES, "patch_dir",
        lambda latitude, longitude: str(patches_directory))

    build_directory = tmp_path / "Tiles" / ("zOrtho4XP_" + TILE_NAME)
    build_directory.mkdir(parents=True)
    tile = types.SimpleNamespace(
        lat=TILE_LATITUDE, lon=TILE_LONGITUDE,
        build_dir=str(build_directory))
    mesh_path = FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)
    _write_synthetic_mesh(mesh_path)

    harness = Harness()
    harness.tmp_path = tmp_path
    harness.tile = tile
    harness.mesh_path = mesh_path
    harness.patches_directory = patches_directory
    harness.worklist_path = patches_directory / (
        post_mesh.OBJECT_ANCHOR_WORKLIST_FILENAME)

    def write_worklist(airports):
        payload = {
            "version": post_mesh.OBJECT_ANCHOR_WORKLIST_VERSION,
            "tile": TILE_NAME,
            "xplane_root": None,
            "airports": airports,
        }
        harness.worklist_path.write_text(
            json.dumps(payload, indent=2) + "\n")

    harness.write_worklist = write_worklist

    def worklist_entry(icao, dsf_path, pack_root, dsf_mtime=None):
        return {
            "icao": icao,
            "dsf_path": dsf_path,
            "dsf_mtime": (
                dsf_mtime if dsf_mtime is not None
                else os.path.getmtime(dsf_path)),
            "pack_root": pack_root,
            "xplane_root": None,
        }

    harness.worklist_entry = worklist_entry
    return harness


# ── flag gating and worklist presence ────────────────────────────────

def test_flag_off_returns_empty_without_reading_anything(monkeypatch):
    """With the flag off (the default) the function returns ``{}``
    BEFORE deriving the worklist path or reading any file — the sentinel
    ``patch_dir`` raises if consulted."""
    monkeypatch.setattr(config, "DSF_OBJECT_REANCHOR", False)

    def exploding_patch_dir(latitude, longitude):
        raise AssertionError(
            "the worklist path must not be derived with the flag off")

    monkeypatch.setattr(FNAMES, "patch_dir", exploding_patch_dir)
    tile = types.SimpleNamespace(lat=35, lon=-81, build_dir="/nonexistent")
    assert post_mesh.rebake_dsf_objects(tile) == {}


def test_missing_worklist_returns_empty_without_error(phase_two_harness):
    assert post_mesh.rebake_dsf_objects(phase_two_harness.tile) == {}


def test_missing_mesh_reports_and_returns_zero_counts(
        phase_two_harness, monkeypatch):
    harness = phase_two_harness
    os.remove(harness.mesh_path)
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    harness.write_worklist(
        [harness.worklist_entry("KTST", dsf_path, pack_root)])
    messages = []
    monkeypatch.setattr(
        UI, "vprint",
        lambda level, *message_parts: messages.append(
            (level, " ".join(str(part) for part in message_parts))))
    counts = post_mesh.rebake_dsf_objects(harness.tile)
    assert counts == {key: 0 for key in post_mesh._COUNT_KEYS}
    assert any("mesh not found" in message for _level, message in messages)


# ── the end-to-end bake ──────────────────────────────────────────────

def test_end_to_end_bake_rewrites_live_file_with_backup_and_provenance(
        phase_two_harness):
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    harness.write_worklist(
        [harness.worklist_entry("KTST", dsf_path, pack_root)])

    counts = post_mesh.rebake_dsf_objects(harness.tile)

    assert counts["airports_processed"] == 1
    assert counts["packs_corrected"] == 1
    assert counts["structures_baked"] == 1
    assert counts["structures_needing_pad"] == 0
    assert counts["vertices_offset"] == 4
    assert counts["objects_skipped"] == 0
    assert counts["airports_failed"] == 0

    live_path = os.path.join(pack_root, "objects", "offset_bake.obj")
    backup_path = live_path + ".anchor_bak"
    assert os.path.isfile(backup_path)
    with open(backup_path) as handle:
        assert handle.read() == OFFSET_SLAB_OBJECT

    expected_offset = _expected_slab_offset()
    assert expected_offset > 1.0  # the fixture genuinely slopes
    for y_value in _vertex_y_values(live_path):
        assert y_value == pytest.approx(expected_offset, abs=1e-4)

    provenance_path = os.path.join(
        pack_root, ".o4_reanchor_provenance.json")
    assert os.path.isfile(provenance_path)
    with open(provenance_path) as handle:
        provenance = json.load(handle)
    assert "objects/offset_bake.obj" in provenance["objects"]
    assert TILE_NAME in provenance["meshes"]


def test_idempotent_through_the_full_path(phase_two_harness):
    """Two full ``rebake_dsf_objects`` runs leave the pack byte-identical
    (delegates the guarantee to invariant I-15, proves the wiring passes
    backups correctly — including discovery reading the ``.anchor_bak``
    original on the second run)."""
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    harness.write_worklist(
        [harness.worklist_entry("KTST", dsf_path, pack_root)])
    live_path = os.path.join(pack_root, "objects", "offset_bake.obj")
    backup_path = live_path + ".anchor_bak"

    first_counts = post_mesh.rebake_dsf_objects(harness.tile)
    with open(live_path, "rb") as handle:
        live_after_first_run = handle.read()
    with open(backup_path, "rb") as handle:
        backup_after_first_run = handle.read()

    second_counts = post_mesh.rebake_dsf_objects(harness.tile)
    with open(live_path, "rb") as handle:
        live_after_second_run = handle.read()
    with open(backup_path, "rb") as handle:
        backup_after_second_run = handle.read()

    assert live_after_first_run == live_after_second_run
    assert backup_after_first_run == backup_after_second_run
    assert first_counts["structures_baked"] == 1
    assert second_counts["structures_baked"] == 1


def test_idempotent_rerun_does_not_touch_the_live_file(phase_two_harness):
    """A byte-identical re-bake must not REWRITE the live ``.obj`` — the
    old unconditional write churned mtimes, invalidating every
    mtime-fingerprinted pack sidecar (classification, footprints, road
    network) and X-Plane's object cache on every mesh build."""
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    harness.write_worklist(
        [harness.worklist_entry("KTST", dsf_path, pack_root)])
    live_path = os.path.join(pack_root, "objects", "offset_bake.obj")

    post_mesh.rebake_dsf_objects(harness.tile)
    backdated = os.path.getmtime(live_path) - 1000.0
    os.utime(live_path, (backdated, backdated))

    post_mesh.rebake_dsf_objects(harness.tile)
    assert os.path.getmtime(live_path) == pytest.approx(backdated), (
        "identical re-bake rewrote the live file (mtime churn)")


# ── partition sidecar cache (Airport_mod_cache/<pack>/) ─────────────


def test_partition_cache_serves_second_run_and_content_invalidates(
        phase_two_harness, monkeypatch):
    """Run 2 takes its partition from the pack sidecar cache (the
    partition is pure pack geometry — 2026-07-15 profile put it at 195 s
    of the 385 s KBNA rebake) and bakes identically; changing the
    geometry SOURCE bytes invalidates the content-hash key."""
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    harness.write_worklist(
        [harness.worklist_entry("KTST", dsf_path, pack_root)])
    live_path = os.path.join(pack_root, "objects", "offset_bake.obj")

    partition_calls = []
    real_partition = post_mesh.object_anchor.partition_structures

    def counting_partition(*args, **kwargs):
        partition_calls.append(1)
        return real_partition(*args, **kwargs)

    monkeypatch.setattr(
        post_mesh.object_anchor, "partition_structures",
        counting_partition)

    first_counts = post_mesh.rebake_dsf_objects(harness.tile)
    assert first_counts["structures_baked"] == 1
    assert len(partition_calls) == 1
    with open(live_path, "rb") as handle:
        live_after_first_run = handle.read()

    second_counts = post_mesh.rebake_dsf_objects(harness.tile)
    assert second_counts["structures_baked"] == 1
    assert len(partition_calls) == 1  # served from the sidecar cache
    with open(live_path, "rb") as handle:
        assert handle.read() == live_after_first_run

    # Content invalidation: the geometry source after run 1 is the
    # ``.anchor_bak`` original (ruling R1) — rewriting it with different
    # bytes must recompute the partition, not serve the stale entry.
    backup_path = live_path + ".anchor_bak"
    with open(backup_path) as handle:
        original = handle.read()
    with open(backup_path, "w") as handle:
        handle.write(original + "# trailing comment changes the bytes\n")
    post_mesh.rebake_dsf_objects(harness.tile)
    assert len(partition_calls) == 2


def test_partition_cache_disabled_by_environment_flag(
        phase_two_harness, monkeypatch):
    monkeypatch.setenv("O4_OBJECT_PARTITION_CACHE", "0")
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    harness.write_worklist(
        [harness.worklist_entry("KTST", dsf_path, pack_root)])

    partition_calls = []
    real_partition = post_mesh.object_anchor.partition_structures

    def counting_partition(*args, **kwargs):
        partition_calls.append(1)
        return real_partition(*args, **kwargs)

    monkeypatch.setattr(
        post_mesh.object_anchor, "partition_structures",
        counting_partition)

    post_mesh.rebake_dsf_objects(harness.tile)
    post_mesh.rebake_dsf_objects(harness.tile)
    assert len(partition_calls) == 2  # no cache with the flag off


# ── multi-ground-cluster foot pads (sidecar) ─────────────────────────


def test_foot_pad_sidecar_written_and_removed(
        phase_two_harness, monkeypatch):
    """A baked-offset two-foot gantry across the plane's slope: the
    rigid offset seats only the topmost-target foot, the other raises a
    terrain-pad request, and ``rebake_dsf_objects`` records it in the
    per-tile sidecar — refreshed on the next run (removed here, since
    the gate turned off leaves no request)."""
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Gantry Pack", TWO_FOOT_GANTRY_DSF_BODY,
        {"objects/gantry.obj": TWO_FOOT_GANTRY_OBJECT})
    harness.write_worklist(
        [harness.worklist_entry("KTST", dsf_path, pack_root)])

    counts = post_mesh.rebake_dsf_objects(harness.tile)
    assert counts["structures_baked"] == 1
    assert counts["foot_pad_requests"] == 1

    sidecar_path = harness.patches_directory / (
        post_mesh.OBJECT_FOOT_PAD_SIDECAR_FILENAME)
    payload = json.loads(sidecar_path.read_text())
    assert payload["version"] == post_mesh.OBJECT_FOOT_PAD_SIDECAR_VERSION
    (airport,) = payload["airports"]
    assert airport["icao"] == "KTST"
    assert airport["pack_root"] == pack_root
    (request,) = airport["requests"]
    assert request["resource_path"] == "objects/gantry.obj"
    assert request["base_y"] == pytest.approx(6.5, abs=1e-9)
    # The west foot floats by the slope between the feet minus the
    # authored base difference (contact centroids 38 m apart east).
    west_ground = _plane_elevation(obj8_reader.local_offset_to_lonlat(
        ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, 1.0, 0.0)[1])
    east_ground = _plane_elevation(obj8_reader.local_offset_to_lonlat(
        ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, 39.0, 0.0)[1])
    expected_residual = (east_ground - west_ground) - (7.7 - 6.5)
    assert expected_residual > config.DSF_OBJECT_FOOT_PAD_RESIDUAL_M
    assert request["residual_metres"] == pytest.approx(
        expected_residual, abs=1e-2)
    assert request["target_ground_metres"] == pytest.approx(
        west_ground + expected_residual, abs=1e-2)
    ring = request["ring_lonlat"]
    assert ring is not None and len(ring) >= 3
    assert all(len(point) == 2 for point in ring)

    # Gate off, run again: no request remains, the stale sidecar goes.
    monkeypatch.setattr(config, "DSF_OBJECT_FOOT_ANCHOR", False)
    second_counts = post_mesh.rebake_dsf_objects(harness.tile)
    assert second_counts["foot_pad_requests"] == 0
    assert not sidecar_path.exists()


REACH_FLOOR_DSF_BODY = "\n".join([
    "OBJECT_DEF objects/short_gantry.obj",
    "OBJECT_DEF objects/small_slab.obj",
    f"OBJECT 0 {ANCHOR_LONGITUDE:.9f} {ANCHOR_LATITUDE:.9f} 0.000000",
    f"OBJECT 1 {ANCHOR_LONGITUDE:.9f} {ANCHOR_LATITUDE + 0.001:.9f} "
    "0.000000",
]) + "\n"

# A compact base-0 slab (reach ~21 m): correctly anchored, X-Plane's
# business — must stay below the standard 25 m discovery floor.
SMALL_SLAB_OBJECT = "\n".join([
    "A",
    "800",
    "OBJ",
    "",
    "POINT_COUNTS 4 0 0 6",
    "VT 5.000000 0.000000 5.000000 0.0 1.0 0.0 0.0 0.0",
    "VT 15.000000 0.000000 5.000000 0.0 1.0 0.0 0.0 0.0",
    "VT 15.000000 0.000000 15.000000 0.0 1.0 0.0 0.0 0.0",
    "VT 5.000000 0.000000 15.000000 0.0 1.0 0.0 0.0 0.0",
    "IDX10 0 1 2 0 2 3",
    "TRIS 0 6",
]) + "\n"


def test_baked_offset_geometry_admitted_at_reduced_reach_floor(
        phase_two_harness):
    """The KBNA gap: the stairs reach 24.3 / 20.6 m — under the 25 m
    discovery floor — so Phase 2 never saw them.  Baked-offset geometry
    (lowest solid vertex above the elevated threshold) is admitted at
    the reduced DSF_OBJECT_FOOT_MIN_REACH_M floor; compact base-0
    geometry keeps the standard floor."""
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Short Gantry Pack", REACH_FLOOR_DSF_BODY,
        {
            # Reach ~20 m: over the 15 m foot floor, under the 25 m one.
            "objects/short_gantry.obj": _two_foot_gantry_object(20.0),
            "objects/small_slab.obj": SMALL_SLAB_OBJECT,
        })

    result = post_mesh.discover_and_rebake_airport(
        dsf_path, harness.mesh_path, pack_root, None)

    assert result["objects_written"] == ["objects/short_gantry.obj"]
    assert result["structures_baked"] == 1
    # Two feet, both kept: over 18 m the plane's slope (~2.1 m) less
    # the 1.2 m base difference is within the contact tolerance.
    ((_pool, decision),) = result["decisions"]
    (feet,) = decision.foot_clusters_by_structure_index.values()
    assert len(feet) == 2
    assert all(foot.kept_for_fit for foot in feet)


# ── invariant I-4 (enforced at Phase 2 discovery, amendment A13) ─────

I4_DSF_BODY = "\n".join([
    "OBJECT_DEF objects/offset_bake.obj",
    "OBJECT_DEF objects/double_bake.obj",
    f"OBJECT 0 {ANCHOR_LONGITUDE:.9f} {ANCHOR_LATITUDE:.9f} 0.000000",
    f"OBJECT 1 {ANCHOR_LONGITUDE + 0.001:.9f} {ANCHOR_LATITUDE:.9f} "
    "0.000000",
    f"OBJECT 1 {ANCHOR_LONGITUDE - 0.001:.9f} {ANCHOR_LATITUDE:.9f} "
    "0.000000",
]) + "\n"


def test_multi_placement_resource_excluded_single_sibling_baked(
        phase_two_harness):
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", I4_DSF_BODY,
        {
            "objects/offset_bake.obj": OFFSET_SLAB_OBJECT,
            "objects/double_bake.obj": OFFSET_SLAB_OBJECT,
        })

    result = post_mesh.discover_and_rebake_airport(
        dsf_path, harness.mesh_path, pack_root, None)

    assert result["objects_written"] == ["objects/offset_bake.obj"]
    skipped_by_resource = dict(result["skipped"])
    assert "objects/double_bake.obj" in skipped_by_resource
    assert "invariant I-4" in skipped_by_resource[
        "objects/double_bake.obj"]

    # The excluded file is untouched: no rewrite, no backup.
    double_path = os.path.join(pack_root, "objects", "double_bake.obj")
    with open(double_path) as handle:
        assert handle.read() == OFFSET_SLAB_OBJECT
    assert not os.path.isfile(double_path + ".anchor_bak")

    single_path = os.path.join(pack_root, "objects", "offset_bake.obj")
    assert _vertex_y_values(single_path)[0] == pytest.approx(
        _expected_slab_offset(), abs=1e-4)


def test_multi_placement_exclusion_counts_through_the_worklist(
        phase_two_harness):
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", I4_DSF_BODY,
        {
            "objects/offset_bake.obj": OFFSET_SLAB_OBJECT,
            "objects/double_bake.obj": OFFSET_SLAB_OBJECT,
        })
    harness.write_worklist(
        [harness.worklist_entry("KTST", dsf_path, pack_root)])
    counts = post_mesh.rebake_dsf_objects(harness.tile)
    assert counts["structures_baked"] == 1
    assert counts["objects_skipped"] == 1
    assert counts["airports_failed"] == 0


# ── worklist staleness (amendment A5: identification only) ───────────

def test_stale_dsf_mtime_still_processes_against_current_dsf(
        phase_two_harness):
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    harness.write_worklist([
        harness.worklist_entry(
            "KTST", dsf_path, pack_root,
            dsf_mtime=os.path.getmtime(dsf_path) - 500.0),
    ])
    counts = post_mesh.rebake_dsf_objects(harness.tile)
    assert counts["airports_processed"] == 1
    assert counts["structures_baked"] == 1
    assert counts["airports_failed"] == 0


# ── per-airport failure containment ──────────────────────────────────

def test_one_broken_airport_never_blocks_the_next(phase_two_harness):
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    harness.write_worklist([
        harness.worklist_entry(
            "KBAD", str(harness.tmp_path / "no-such.dsf"), pack_root,
            dsf_mtime=0.0),
        harness.worklist_entry("KTST", dsf_path, pack_root),
    ])
    counts = post_mesh.rebake_dsf_objects(harness.tile)
    assert counts["airports_failed"] == 1
    assert counts["airports_processed"] == 1
    assert counts["structures_baked"] == 1


# ── the command line (tools/reanchor_dsf_objects.py) ─────────────────

_CLI_TOOL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "reanchor_dsf_objects.py",
)


@pytest.fixture(scope="module")
def cli_module():
    specification = importlib.util.spec_from_file_location(
        "reanchor_dsf_objects_tool", _CLI_TOOL_PATH)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _mode_two_arguments(dsf_path, mesh_path, pack_root):
    return ["--dsf", dsf_path, "--mesh", mesh_path,
            "--pack-root", pack_root]


def test_cli_dry_run_writes_nothing(phase_two_harness, cli_module,
                                    capsys):
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    live_path = os.path.join(pack_root, "objects", "offset_bake.obj")

    exit_code = cli_module.main(
        _mode_two_arguments(dsf_path, harness.mesh_path, pack_root)
        + ["--dry-run"])
    assert exit_code == 0

    with open(live_path) as handle:
        assert handle.read() == OFFSET_SLAB_OBJECT
    assert not os.path.isfile(live_path + ".anchor_bak")
    assert not os.path.isfile(
        os.path.join(pack_root, ".o4_reanchor_provenance.json"))
    output = capsys.readouterr().out
    assert "would bake 1 structure(s)" in output
    assert "4 vertices offset" in output


def test_cli_apply_check_and_restore(phase_two_harness, cli_module,
                                     capsys):
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    live_path = os.path.join(pack_root, "objects", "offset_bake.obj")
    common_arguments = _mode_two_arguments(
        dsf_path, harness.mesh_path, pack_root)

    # Apply (the default action).
    assert cli_module.main(common_arguments) == 0
    assert _vertex_y_values(live_path)[0] == pytest.approx(
        _expected_slab_offset(), abs=1e-4)
    capsys.readouterr()

    # --check reports CURRENT against the same mesh.
    assert cli_module.main(common_arguments + ["--check"]) == 0
    assert "CURRENT" in capsys.readouterr().out

    # --restore puts the original back byte-identically and removes the
    # provenance sidecar.
    assert cli_module.main(common_arguments + ["--restore"]) == 0
    with open(live_path) as handle:
        assert handle.read() == OFFSET_SLAB_OBJECT
    assert not os.path.isfile(
        os.path.join(pack_root, ".o4_reanchor_provenance.json"))


def test_cli_worklist_mode_processes_every_airport(
        phase_two_harness, cli_module, capsys):
    harness = phase_two_harness
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    harness.write_worklist(
        [harness.worklist_entry("KTST", dsf_path, pack_root)])

    exit_code = cli_module.main([
        "--worklist", str(harness.worklist_path),
        "--mesh", harness.mesh_path,
    ])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "KTST" in output and "baked 1 structure(s)" in output
    live_path = os.path.join(pack_root, "objects", "offset_bake.obj")
    assert _vertex_y_values(live_path)[0] == pytest.approx(
        _expected_slab_offset(), abs=1e-4)


# ── the driver's worklist writer (amendment A5) ──────────────────────

def test_driver_worklist_write_is_atomic_and_versioned(tmp_path):
    patch_directory = str(tmp_path / "Patches" / TILE_NAME)
    entries = [{
        "icao": "KTST",
        "dsf_path": "/somewhere/+35-081.dsf",
        "dsf_mtime": 1.0,
        "pack_root": "/somewhere",
        "xplane_root": "/xplane",
    }]
    driver._write_object_anchor_worklist(
        patch_directory, TILE_LATITUDE, TILE_LONGITUDE, entries,
        "/xplane")
    worklist_path = os.path.join(
        patch_directory, post_mesh.OBJECT_ANCHOR_WORKLIST_FILENAME)
    with open(worklist_path) as handle:
        payload = json.load(handle)
    assert payload["version"] == post_mesh.OBJECT_ANCHOR_WORKLIST_VERSION
    assert payload["tile"] == TILE_NAME
    assert payload["xplane_root"] == "/xplane"
    assert payload["airports"] == entries
    # The atomic-write temporary never survives.
    assert not os.path.exists(worklist_path + ".tmp")


def test_driver_worklist_refreshes_to_empty_but_never_creates_empty(
        tmp_path):
    patch_directory = str(tmp_path / "Patches" / TILE_NAME)
    worklist_path = os.path.join(
        patch_directory, post_mesh.OBJECT_ANCHOR_WORKLIST_FILENAME)

    # No entries and no existing file: nothing is created.
    driver._write_object_anchor_worklist(
        patch_directory, TILE_LATITUDE, TILE_LONGITUDE, [], None)
    assert not os.path.exists(worklist_path)

    # Entries, then none: the stale file is refreshed to empty rather
    # than left lying about airports that no longer resolve.
    entries = [{
        "icao": "KTST",
        "dsf_path": "/somewhere/+35-081.dsf",
        "dsf_mtime": 1.0,
        "pack_root": "/somewhere",
        "xplane_root": "/xplane",
    }]
    driver._write_object_anchor_worklist(
        patch_directory, TILE_LATITUDE, TILE_LONGITUDE, entries,
        "/xplane")
    driver._write_object_anchor_worklist(
        patch_directory, TILE_LATITUDE, TILE_LONGITUDE, [], None)
    with open(worklist_path) as handle:
        assert json.load(handle)["airports"] == []


# ── the O4_Mesh_Utils hook (amendment A4) ────────────────────────────

def test_mesh_hook_swallows_exceptions(monkeypatch):
    """``build_mesh``'s tail (and ``sort_mesh``'s) calls the shared
    guard ``_auto_patch_post_mesh_rebake``; a raising
    ``rebake_dsf_objects`` must never propagate out of it."""
    def exploding_rebake(tile):
        raise RuntimeError("synthetic post-mesh failure")

    monkeypatch.setattr(
        post_mesh, "rebake_dsf_objects", exploding_rebake)
    messages = []
    monkeypatch.setattr(
        O4_Mesh_Utils.UI, "vprint",
        lambda level, *message_parts: messages.append(
            " ".join(str(part) for part in message_parts)))

    tile = types.SimpleNamespace(lat=35, lon=-81, build_dir="/nonexistent")
    O4_Mesh_Utils._auto_patch_post_mesh_rebake(tile)  # must not raise

    assert any("re-anchor failed" in message for message in messages)


def test_mesh_hook_is_wired_into_build_mesh_and_sort_mesh():
    """The guard must be CALLED from both tails (amendment A4).  Source
    inspection keeps this hermetic — running a real mesh build is out of
    the question."""
    import inspect

    build_mesh_source = inspect.getsource(O4_Mesh_Utils.build_mesh)
    sort_mesh_source = inspect.getsource(O4_Mesh_Utils.sort_mesh)
    assert "_auto_patch_post_mesh_rebake(tile)" in build_mesh_source
    assert "_auto_patch_post_mesh_rebake(tile)" in sort_mesh_source


# ---------------------------------------------------------------------------
# amendment A15: base/global scenery and library-resolved resources are
# never rebaked (found live: Global Airports static airliners pass the
# reach floor, and only an unwritable directory stopped a base-sim write)
# ---------------------------------------------------------------------------

def test_protected_scenery_root_is_never_rebaked(phase_two_harness):
    harness = phase_two_harness
    global_scenery = harness.tmp_path / "Global Scenery"
    global_scenery.mkdir()
    dsf_path, pack_root = _make_pack(
        global_scenery, "Global Airports", SINGLE_PLACEMENT_DSF_BODY,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})

    result = post_mesh.discover_and_rebake_airport(
        dsf_path, harness.mesh_path, pack_root, None)

    assert result["objects_written"] == []
    assert result["structures_baked"] == 0
    assert any("never rebaked" in reason for _, reason in result["skipped"])
    live_path = os.path.join(pack_root, "objects", "offset_bake.obj")
    assert not os.path.isfile(live_path + ".anchor_bak")
    with open(live_path) as handle:
        assert handle.read() == OFFSET_SLAB_OBJECT


def test_library_resolved_resource_outside_the_pack_is_skipped(
        phase_two_harness, monkeypatch):
    harness = phase_two_harness
    dsf_body = "\n".join([
        "OBJECT_DEF objects/offset_bake.obj",
        "OBJECT_DEF lib/airport/shared_hangar.obj",
        f"OBJECT 0 {ANCHOR_LONGITUDE:.9f} {ANCHOR_LATITUDE:.9f} 0.000000",
        f"OBJECT 1 {ANCHOR_LONGITUDE:.9f} {ANCHOR_LATITUDE:.9f} 0.000000",
    ]) + "\n"
    dsf_path, pack_root = _make_pack(
        harness.tmp_path, "Fake Pack", dsf_body,
        {"objects/offset_bake.obj": OFFSET_SLAB_OBJECT})
    # The shared library object lives in ANOTHER pack entirely.
    library_pack = harness.tmp_path / "Library Pack"
    library_object = library_pack / "shared_hangar.obj"
    library_object.parent.mkdir(parents=True)
    library_object.write_text(OFFSET_SLAB_OBJECT)

    real_resolve = post_mesh.obj8_reader.resolve_object_resource

    def resolving_through_a_library(resource_path, pack, xplane):
        if resource_path == "lib/airport/shared_hangar.obj":
            return str(library_object)
        return real_resolve(resource_path, pack, xplane)

    monkeypatch.setattr(
        post_mesh.obj8_reader, "resolve_object_resource",
        resolving_through_a_library)

    result = post_mesh.discover_and_rebake_airport(
        dsf_path, harness.mesh_path, pack_root, None)

    # The library resource is skipped with the A15 reason...
    assert any(
        resource == "lib/airport/shared_hangar.obj"
        and "library.txt outside the pack" in reason
        for resource, reason in result["skipped"])
    # ...its file is untouched...
    assert not os.path.isfile(str(library_object) + ".anchor_bak")
    with open(library_object) as handle:
        assert handle.read() == OFFSET_SLAB_OBJECT
    # ...and the pack-local sibling still bakes.
    assert result["objects_written"] == ["objects/offset_bake.obj"]
