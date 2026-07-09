"""Workstream W5 acceptance tests for ``auto_patch.object_rebake``.

Everything is hermetic: synthetic scenery packs and mesh files are built
under ``tmp_path`` and the ``RebakeDecision`` / ``Structure`` dataclasses
are constructed by hand — the tests never wait on workstream W4 and never
touch a real X-Plane install (the real KCLT pack is live-baked and
read-only for this workstream).

Coverage map:

* invariant I-15 — byte-idempotent apply, exact restore;
* invariant I-16 — only the y token changes, whitespace (tabs included)
  and decimal precision preserved, line counts unchanged;
* invariant I-10 — positional commands move with their structure;
* invariant I-11 — ANIM refusal by default; one offset per block when
  ``DSF_OBJECT_ALLOW_ANIM`` is on; blocks spanning differing-offset
  structures are skipped;
* invariant I-9 — mixed draped/solid vertices refuse-and-report;
* invariant I-4 defence — duplicate resources in a decision are skipped;
* invariant I-14 + amendment A2 — the full five-branch backup-adoption
  matrix, including the prototype-live KCLT state (baked files, hashless
  prototype-format provenance, authoritative backups);
* amendment A6 — provenance keyed per (pack, mesh) with tile names;
* ``check`` CURRENT / STALE / NONE, prototype sidecar tolerated;
* whole-pool refusal on an unwritable pack directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import stat

import pytest

from auto_patch import config
from auto_patch.object_anchor import RebakeDecision, Structure
from auto_patch.object_rebake import (
    BACKUP_SUFFIX,
    PROVENANCE_FILENAME,
    apply,
    check,
    restore,
)

BOX_RESOURCE = "Objects/boxes.obj"
BOX_A_DELTA = 2.5
BOX_B_DELTA = -1.25


# ---------------------------------------------------------------------------
# synthetic pack builders
# ---------------------------------------------------------------------------

def _vertex_line(x: float, y: float, z: float, separator: str = " ") -> str:
    values = [f"{value:.6f}" for value in (x, y, z, 0.0, 1.0, 0.0, 0.0, 0.0)]
    return separator.join(["VT"] + values)


def _two_box_object_text(
    separator: str = " ",
    trailing_lines: list[str] | None = None,
    triangle_lines: list[str] | None = None,
) -> str:
    """Two 10 m boxes: box A spans x 0..10, box B spans x 100..110.

    Vertices 0-3 belong to box A, 4-7 to box B; the index table holds
    box A's two triangles first (index positions 0..5) then box B's
    (index positions 6..11).
    """
    lines = [
        "A",
        "800",
        "OBJ",
        "",
        "TEXTURE rebake.png",
        "POINT_COUNTS 8 0 0 12",
        "",
        _vertex_line(0.0, 0.0, 0.0, separator),
        _vertex_line(10.0, 0.0, 0.0, separator),
        _vertex_line(10.0, 5.0, 10.0, separator),
        _vertex_line(0.0, 5.0, 10.0, separator),
        _vertex_line(100.0, 0.0, 0.0, separator),
        _vertex_line(110.0, 0.0, 0.0, separator),
        _vertex_line(110.0, 5.0, 10.0, separator),
        _vertex_line(100.0, 5.0, 10.0, separator),
        "IDX10 0 1 2 0 2 3 4 5 6 4",
        "IDX 6",
        "IDX 7",
    ]
    lines.extend(triangle_lines if triangle_lines else ["TRIS 0 12"])
    lines.extend(trailing_lines or [])
    return "\n".join(lines) + "\n"


def _make_pack(tmp_path, contents: dict[str, str]) -> tuple[str, str]:
    """Write a synthetic pack and mesh under ``tmp_path``; return
    ``(pack_root, mesh_path)``."""
    pack_root = tmp_path / "pack"
    pack_root.mkdir(exist_ok=True)
    for resource_path, text in contents.items():
        target = pack_root / resource_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    mesh_path = tmp_path / "Data+35-081.mesh"
    if not mesh_path.exists():
        mesh_path.write_text("synthetic mesh for provenance stat only\n")
    return str(pack_root), str(mesh_path)


def _structure_over(
    triangles: list[tuple[int, int, int]],
    resource_path: str = BOX_RESOURCE,
    needs_pad: bool = False,
) -> Structure:
    return Structure(
        triangles_by_resource={resource_path: triangles},
        surface_area_square_metres=100.0,
        centroid_latitude=35.207,
        centroid_longitude=-80.935,
        minimum_base_y_by_resource={resource_path: 0.0},
        is_ground_touching=True,
        ground_span_metres=0.5,
        needs_pad=needs_pad,
        skip_reason=None,
        inherited_from_structure_index=None,
    )


def _two_box_decision(
    resource_path: str = BOX_RESOURCE,
    box_a_delta: float = BOX_A_DELTA,
    box_b_delta: float = BOX_B_DELTA,
    needs_pad_b: bool = False,
    skipped: list[tuple[str, str]] | None = None,
) -> RebakeDecision:
    structures = [
        _structure_over([(0, 1, 2), (0, 2, 3)], resource_path),
        _structure_over(
            [(4, 5, 6), (4, 6, 7)], resource_path, needs_pad=needs_pad_b
        ),
    ]
    deltas = {index: box_a_delta for index in range(4)}
    deltas.update({index: box_b_delta for index in range(4, 8)})
    return RebakeDecision(
        structures=structures,
        delta_by_resource_and_vertex={resource_path: deltas},
        anchor_ground_by_resource={resource_path: 219.83},
        skipped=list(skipped or []),
    )


def _sha256_of(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _live_path(pack_root: str, resource_path: str = BOX_RESOURCE) -> str:
    return os.path.join(pack_root, resource_path)


def _backup_path(pack_root: str, resource_path: str = BOX_RESOURCE) -> str:
    return _live_path(pack_root, resource_path) + BACKUP_SUFFIX


# ---------------------------------------------------------------------------
# invariant I-16 — only the y token changes
# ---------------------------------------------------------------------------

def test_apply_changes_only_the_vertex_y_token(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    report = apply(_two_box_decision(), pack_root, mesh_path)
    assert report.objects_written == [BOX_RESOURCE]
    assert report.vertices_offset_total == 8
    assert report.structures_baked == 2
    assert report.skipped == []

    backup_lines = _read_bytes(_backup_path(pack_root)).decode().split("\n")
    live_lines = _read_bytes(_live_path(pack_root)).decode().split("\n")
    assert len(backup_lines) == len(live_lines)

    vertex_row = 0
    for backup_line, live_line in zip(backup_lines, live_lines):
        backup_tokens = backup_line.split()
        live_tokens = live_line.split()
        if not backup_tokens or backup_tokens[0] != "VT":
            assert live_line == backup_line
            continue
        expected_delta = BOX_A_DELTA if vertex_row < 4 else BOX_B_DELTA
        vertex_row += 1
        # x, z, normals and texture coordinates are byte-identical...
        assert live_tokens[:2] == backup_tokens[:2]
        assert live_tokens[3:] == backup_tokens[3:]
        # ...and the y token moved by exactly the structure's offset,
        # keeping the original decimal count, and stayed finite.
        assert live_tokens[2] != backup_tokens[2]
        assert math.isfinite(float(live_tokens[2]))
        assert float(live_tokens[2]) == pytest.approx(
            float(backup_tokens[2]) + expected_delta
        )
        assert len(live_tokens[2].split(".")[1]) == len(
            backup_tokens[2].split(".")[1]
        )
    assert vertex_row == 8


def test_tab_separated_whitespace_is_preserved(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text(separator="\t")}
    )
    apply(_two_box_decision(), pack_root, mesh_path)
    backup_lines = _read_bytes(_backup_path(pack_root)).decode().split("\n")
    live_lines = _read_bytes(_live_path(pack_root)).decode().split("\n")
    assert len(backup_lines) == len(live_lines)
    for backup_line, live_line in zip(backup_lines, live_lines):
        if backup_line.split() and backup_line.split()[0] == "VT":
            assert live_line.count("\t") == backup_line.count("\t")
            assert "\t" in live_line
        else:
            assert live_line == backup_line


def test_decimal_precision_and_integer_y_tokens(tmp_path):
    """A 2-decimal y stays 2 decimals; a dotless y gains the prototype's
    6-decimal formatting (ported behaviour)."""
    resource_path = "Objects/precision.obj"
    object_text = "\n".join(
        [
            "A",
            "800",
            "OBJ",
            "",
            "POINT_COUNTS 3 0 0 3",
            "VT 0.5 0.25 0.5 0.0 1.0 0.0 0.0 0.0",
            "VT 1.0 3 1.0 0.0 1.0 0.0 0.0 0.0",
            "VT 2.0 0.1 2.0 0.0 1.0 0.0 0.0 0.0",
            "IDX 0",
            "IDX 1",
            "IDX 2",
            "TRIS 0 3",
        ]
    ) + "\n"
    pack_root, mesh_path = _make_pack(tmp_path, {resource_path: object_text})
    decision = RebakeDecision(
        structures=[_structure_over([(0, 1, 2)], resource_path)],
        delta_by_resource_and_vertex={
            resource_path: {0: 2.5, 1: 2.5, 2: 2.5}
        },
        anchor_ground_by_resource={resource_path: 100.0},
        skipped=[],
    )
    apply(decision, pack_root, mesh_path)
    live_lines = _read_bytes(
        _live_path(pack_root, resource_path)
    ).decode().split("\n")
    assert live_lines[5].split()[2] == "2.75"
    assert live_lines[6].split()[2] == "5.500000"
    assert live_lines[7].split()[2] == "2.6"


# ---------------------------------------------------------------------------
# invariant I-15 — byte-idempotent, exactly reversible
# ---------------------------------------------------------------------------

def test_apply_is_byte_idempotent(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    apply(_two_box_decision(), pack_root, mesh_path)
    first_bytes = _read_bytes(_live_path(pack_root))
    apply(_two_box_decision(), pack_root, mesh_path)
    assert _read_bytes(_live_path(pack_root)) == first_bytes


def test_restore_is_byte_exact_and_removes_provenance(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    original_bytes = _read_bytes(_live_path(pack_root))
    apply(_two_box_decision(), pack_root, mesh_path)
    assert _read_bytes(_live_path(pack_root)) != original_bytes
    sidecar_path = os.path.join(pack_root, PROVENANCE_FILENAME)
    assert os.path.isfile(sidecar_path)

    restored_count = restore(pack_root)
    assert restored_count == 1
    assert _read_bytes(_live_path(pack_root)) == original_bytes
    assert _read_bytes(_live_path(pack_root)) == _read_bytes(
        _backup_path(pack_root)
    )
    assert not os.path.exists(sidecar_path)


# ---------------------------------------------------------------------------
# invariant I-10 — positional commands move with their structure
# ---------------------------------------------------------------------------

def test_positional_command_inside_a_box_moves_with_that_structure(tmp_path):
    trailing = ["LIGHT_NAMED mast_light 5.000000 12.000000 5.000000"]
    pack_root, mesh_path = _make_pack(
        tmp_path,
        {BOX_RESOURCE: _two_box_object_text(trailing_lines=trailing)},
    )
    apply(_two_box_decision(), pack_root, mesh_path)
    live_text = _read_bytes(_live_path(pack_root)).decode()
    light_line = next(
        line for line in live_text.split("\n") if "mast_light" in line
    )
    # (5, 5) is inside box A: y moves by box A's offset.
    assert light_line.split()[3] == f"{12.0 + BOX_A_DELTA:.6f}"


def test_positional_command_outside_every_box_takes_nearest_structure(
    tmp_path,
):
    trailing = ["LIGHT_NAMED far_light 200.000000 3.000000 5.000000"]
    pack_root, mesh_path = _make_pack(
        tmp_path,
        {BOX_RESOURCE: _two_box_object_text(trailing_lines=trailing)},
    )
    apply(_two_box_decision(), pack_root, mesh_path)
    live_text = _read_bytes(_live_path(pack_root)).decode()
    light_line = next(
        line for line in live_text.split("\n") if "far_light" in line
    )
    # x = 200 is inside neither box; box B (x 100..110) is nearest.
    assert light_line.split()[3] == f"{3.0 + BOX_B_DELTA:.6f}"


# ---------------------------------------------------------------------------
# invariant I-11 — animation blocks
# ---------------------------------------------------------------------------

def _animated_object_text(block_triangle_lines: list[str],
                          after_block_lines: list[str]) -> str:
    return _two_box_object_text(
        triangle_lines=(
            [
                "ANIM_begin",
                "ANIM_trans 0.000000 0.000000 0.000000 0.000000 1.000000 "
                "0.000000 0.000000 1.000000 sim/graphics/animation/none",
            ]
            + block_triangle_lines
            + ["ANIM_end"]
            + after_block_lines
        )
    )


def test_animated_object_refused_and_untouched_when_flag_off(tmp_path):
    object_text = _animated_object_text(["TRIS 0 6"], ["TRIS 6 6"])
    pack_root, mesh_path = _make_pack(tmp_path, {BOX_RESOURCE: object_text})
    original_bytes = _read_bytes(_live_path(pack_root))

    assert config.DSF_OBJECT_ALLOW_ANIM is False
    report = apply(_two_box_decision(), pack_root, mesh_path)

    assert report.objects_written == []
    assert _read_bytes(_live_path(pack_root)) == original_bytes
    assert any(
        resource == BOX_RESOURCE and "ANIM" in reason
        for resource, reason in report.skipped
    )


def test_animation_block_within_one_structure_bakes_one_offset(
    tmp_path, monkeypatch
):
    # The block holds box A's triangles AND a light physically placed
    # over box B: the block's single offset (box A's) must win over the
    # bounding-box assignment.
    object_text = _animated_object_text(
        [
            "TRIS 0 6",
            "LIGHT_NAMED block_light 105.000000 2.000000 5.000000",
        ],
        ["TRIS 6 6"],
    )
    pack_root, mesh_path = _make_pack(tmp_path, {BOX_RESOURCE: object_text})
    monkeypatch.setattr(config, "DSF_OBJECT_ALLOW_ANIM", True)
    report = apply(_two_box_decision(), pack_root, mesh_path)

    assert report.objects_written == [BOX_RESOURCE]
    live_lines = _read_bytes(_live_path(pack_root)).decode().split("\n")
    vertex_y_values = [
        float(line.split()[2])
        for line in live_lines
        if line.split() and line.split()[0] == "VT"
    ]
    assert vertex_y_values[:4] == pytest.approx(
        [BOX_A_DELTA, BOX_A_DELTA, 5.0 + BOX_A_DELTA, 5.0 + BOX_A_DELTA]
    )
    assert vertex_y_values[4:] == pytest.approx(
        [BOX_B_DELTA, BOX_B_DELTA, 5.0 + BOX_B_DELTA, 5.0 + BOX_B_DELTA]
    )
    light_line = next(line for line in live_lines if "block_light" in line)
    assert light_line.split()[3] == f"{2.0 + BOX_A_DELTA:.6f}"


def test_animation_block_spanning_two_structures_is_skipped(
    tmp_path, monkeypatch
):
    object_text = _animated_object_text(["TRIS 0 12"], [])
    pack_root, mesh_path = _make_pack(tmp_path, {BOX_RESOURCE: object_text})
    original_bytes = _read_bytes(_live_path(pack_root))
    monkeypatch.setattr(config, "DSF_OBJECT_ALLOW_ANIM", True)
    report = apply(_two_box_decision(), pack_root, mesh_path)

    assert report.objects_written == []
    assert _read_bytes(_live_path(pack_root)) == original_bytes
    assert any(
        resource == BOX_RESOURCE and "differing offsets" in reason
        for resource, reason in report.skipped
    )


# ---------------------------------------------------------------------------
# invariant I-9 and invariant I-4 defence
# ---------------------------------------------------------------------------

def test_mixed_draped_and_solid_vertices_refused(tmp_path):
    object_text = _two_box_object_text(
        triangle_lines=["TRIS 0 3", "ATTR_draped", "TRIS 3 3"]
    )
    pack_root, mesh_path = _make_pack(tmp_path, {BOX_RESOURCE: object_text})
    original_bytes = _read_bytes(_live_path(pack_root))
    report = apply(_two_box_decision(), pack_root, mesh_path)
    assert report.objects_written == []
    assert _read_bytes(_live_path(pack_root)) == original_bytes
    assert any(
        resource == BOX_RESOURCE and "draped" in reason
        for resource, reason in report.skipped
    )


def test_duplicate_resources_in_a_decision_are_skipped(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    original_bytes = _read_bytes(_live_path(pack_root))
    duplicate_key = "Objects/./boxes.obj"
    deltas = {index: BOX_A_DELTA for index in range(8)}
    decision = RebakeDecision(
        structures=[_structure_over([(0, 1, 2), (0, 2, 3)])],
        delta_by_resource_and_vertex={
            BOX_RESOURCE: deltas,
            duplicate_key: deltas,
        },
        anchor_ground_by_resource={BOX_RESOURCE: 219.83},
        skipped=[],
    )
    report = apply(decision, pack_root, mesh_path)
    assert report.objects_written == []
    assert _read_bytes(_live_path(pack_root)) == original_bytes
    skipped_resources = {resource for resource, _reason in report.skipped}
    assert skipped_resources == {BOX_RESOURCE, duplicate_key}


# ---------------------------------------------------------------------------
# amendment A2 — the five-branch backup-adoption matrix (invariant I-14)
# ---------------------------------------------------------------------------

def test_a2_branch_a_live_matches_written_hash_rebakes_from_backup(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    first_report = apply(_two_box_decision(), pack_root, mesh_path)
    assert first_report.orphaned_backups == []
    first_bytes = _read_bytes(_live_path(pack_root))

    second_report = apply(_two_box_decision(), pack_root, mesh_path)
    assert second_report.orphaned_backups == []
    assert second_report.objects_written == [BOX_RESOURCE]
    assert _read_bytes(_live_path(pack_root)) == first_bytes
    assert not os.path.exists(_backup_path(pack_root) + ".orphaned")


def test_a2_branch_b_live_matches_backup_hash_after_user_restore(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    apply(_two_box_decision(), pack_root, mesh_path)
    baked_bytes = _read_bytes(_live_path(pack_root))

    # The user put the original back by hand (live == backup hash).
    shutil.copy2(_backup_path(pack_root), _live_path(pack_root))
    report = apply(_two_box_decision(), pack_root, mesh_path)

    assert report.orphaned_backups == []
    assert _read_bytes(_live_path(pack_root)) == baked_bytes
    assert not os.path.exists(_backup_path(pack_root) + ".orphaned")


def test_a2_branch_c_pack_changed_orphans_backup_loudly(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    pristine_bytes = _read_bytes(_live_path(pack_root))
    apply(_two_box_decision(), pack_root, mesh_path)

    # The pack was updated behind the tool's back: the live file matches
    # neither recorded hash.
    tampered_text = _two_box_object_text(
        trailing_lines=["# pack update marker"]
    )
    with open(_live_path(pack_root), "w") as handle:
        handle.write(tampered_text)
    tampered_bytes = _read_bytes(_live_path(pack_root))

    report = apply(_two_box_decision(), pack_root, mesh_path)

    orphaned_path = _backup_path(pack_root) + ".orphaned"
    assert report.orphaned_backups == [orphaned_path]
    # The stale backup (the true original of the OLD pack) was preserved.
    assert _read_bytes(orphaned_path) == pristine_bytes
    # The tampered live file is the new original.
    assert _read_bytes(_backup_path(pack_root)) == tampered_bytes
    # And the re-bake read from the NEW backup: vertex 0's y is the
    # tampered value (0.0) plus box A's offset.
    live_lines = _read_bytes(_live_path(pack_root)).decode().split("\n")
    first_vertex_line = next(
        line for line in live_lines if line.split()[:1] == ["VT"]
    )
    assert float(first_vertex_line.split()[2]) == pytest.approx(BOX_A_DELTA)
    assert "# pack update marker" in "\n".join(live_lines)


def test_a2_branch_d_prototype_state_backup_adopted_never_orphaned(tmp_path):
    """The real KCLT state: live files BAKED by the prototype, pristine
    ``.anchor_bak`` originals, and a prototype-format sidecar with flat
    keys and NO hashes.  The backup must be adopted — orphaning it would
    enshrine the baked files as originals and destroy the real ones."""
    pristine_text = _two_box_object_text()
    pack_root, mesh_path = _make_pack(tmp_path, {BOX_RESOURCE: pristine_text})

    # The prototype's bake: backup = pristine, live = offset geometry
    # (any bytes that differ from the backup reproduce the state).
    shutil.copy2(_live_path(pack_root), _backup_path(pack_root))
    prototype_baked_text = pristine_text.replace(
        "VT 0.000000 0.000000 0.000000",
        "VT 0.000000 1.190000 0.000000",
    )
    assert prototype_baked_text != pristine_text
    with open(_live_path(pack_root), "w") as handle:
        handle.write(prototype_baked_text)

    # The prototype's provenance: flat keys, objects as a LIST, no
    # version, no hashes (mirrors tools/reanchor_kclt_terminal_bakes.py).
    prototype_sidecar = {
        "mesh": mesh_path,
        "size": 12345,
        "mtime": 1749584021,
        "gap": 2.0,
        "structures": 2,
        "vertices_offset": 8,
        "anchor": [35.207360571, -80.935041390, 86.095674],
        "anchor_ground": 219.83,
        "objects": [BOX_RESOURCE],
    }
    with open(os.path.join(pack_root, PROVENANCE_FILENAME), "w") as handle:
        json.dump(prototype_sidecar, handle, indent=2)

    report = apply(_two_box_decision(), pack_root, mesh_path)

    # Never orphaned; the pristine backup survived byte-identically.
    assert report.orphaned_backups == []
    assert not os.path.exists(_backup_path(pack_root) + ".orphaned")
    assert _read_bytes(_backup_path(pack_root)) == pristine_text.encode()

    # The re-bake read from the pristine backup, so the result is the
    # same as baking a completely fresh pack.
    fresh_directory = tmp_path / "fresh"
    fresh_directory.mkdir()
    fresh_pack_root, fresh_mesh_path = _make_pack(
        fresh_directory, {BOX_RESOURCE: pristine_text}
    )
    apply(_two_box_decision(), fresh_pack_root, fresh_mesh_path)
    assert _read_bytes(_live_path(pack_root)) == _read_bytes(
        _live_path(fresh_pack_root)
    )

    # Provenance upgraded to version 1 with BOTH hashes and per-object
    # tile names (amendment A6); the prototype anchor survived.
    with open(os.path.join(pack_root, PROVENANCE_FILENAME)) as handle:
        upgraded = json.load(handle)
    assert upgraded["version"] == 1
    assert "DO NOT REDISTRIBUTE" in upgraded["warning"]
    assert "+35-081" in upgraded["meshes"]
    entry = upgraded["objects"][BOX_RESOURCE]
    assert entry["tile"] == "+35-081"
    assert entry["backup_sha256"] == _sha256_of(_backup_path(pack_root))
    assert entry["written_sha256"] == _sha256_of(_live_path(pack_root))
    assert entry["anchor"] == [35.207360571, -80.935041390, 86.095674]


def test_a2_branch_e_no_backup_live_becomes_the_original(tmp_path):
    pristine_text = _two_box_object_text()
    pack_root, mesh_path = _make_pack(tmp_path, {BOX_RESOURCE: pristine_text})
    assert not os.path.exists(_backup_path(pack_root))
    apply(_two_box_decision(), pack_root, mesh_path)
    assert _read_bytes(_backup_path(pack_root)) == pristine_text.encode()


# ---------------------------------------------------------------------------
# check — CURRENT / STALE / NONE, prototype format tolerated
# ---------------------------------------------------------------------------

def test_check_none_current_and_stale(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    assert check(pack_root, mesh_path) == "NONE"

    apply(_two_box_decision(), pack_root, mesh_path)
    assert check(pack_root, mesh_path) == "CURRENT"

    mesh_stat = os.stat(mesh_path)
    os.utime(mesh_path, (mesh_stat.st_atime, mesh_stat.st_mtime + 100))
    assert check(pack_root, mesh_path) == "STALE"


def test_check_reads_the_prototype_format_sidecar(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    mesh_stat = os.stat(mesh_path)
    prototype_sidecar = {
        "mesh": mesh_path,
        "size": mesh_stat.st_size,
        "mtime": int(mesh_stat.st_mtime),
        "gap": 2.0,
        "structures": 105,
        "objects": [BOX_RESOURCE],
    }
    sidecar_path = os.path.join(pack_root, PROVENANCE_FILENAME)
    with open(sidecar_path, "w") as handle:
        json.dump(prototype_sidecar, handle)
    assert check(pack_root, mesh_path) == "CURRENT"

    prototype_sidecar["mtime"] -= 5
    with open(sidecar_path, "w") as handle:
        json.dump(prototype_sidecar, handle)
    assert check(pack_root, mesh_path) == "STALE"


# ---------------------------------------------------------------------------
# whole-pool refusal on an unwritable pack (torn-geometry guard)
# ---------------------------------------------------------------------------

def test_unwritable_directory_refuses_the_whole_pool(tmp_path):
    locked_resource = "LockedDirectory/locked.obj"
    open_resource = "Objects/open.obj"
    pack_root, mesh_path = _make_pack(
        tmp_path,
        {
            locked_resource: _two_box_object_text(),
            open_resource: _two_box_object_text(),
        },
    )
    open_bytes_before = _read_bytes(_live_path(pack_root, open_resource))
    deltas = {index: BOX_A_DELTA for index in range(8)}
    decision = RebakeDecision(
        structures=[
            _structure_over([(0, 1, 2), (0, 2, 3)], locked_resource),
            _structure_over([(0, 1, 2), (0, 2, 3)], open_resource),
        ],
        delta_by_resource_and_vertex={
            locked_resource: dict(deltas),
            open_resource: dict(deltas),
        },
        anchor_ground_by_resource={
            locked_resource: 219.83,
            open_resource: 219.83,
        },
        skipped=[],
    )
    locked_directory = os.path.join(pack_root, "LockedDirectory")
    os.chmod(locked_directory, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP
             | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 0o555
    try:
        report = apply(decision, pack_root, mesh_path)
    finally:
        os.chmod(locked_directory, 0o755)

    assert report.objects_written == []
    assert report.structures_baked == 0
    assert report.provenance_path is None
    skipped_resources = {resource for resource, _reason in report.skipped}
    assert skipped_resources == {locked_resource, open_resource}
    # NOTHING was touched — not even the writable half of the pool.
    assert _read_bytes(_live_path(pack_root, open_resource)) == (
        open_bytes_before
    )
    assert not os.path.exists(_backup_path(pack_root, open_resource))
    assert not os.path.exists(
        os.path.join(pack_root, PROVENANCE_FILENAME)
    )


# ---------------------------------------------------------------------------
# report bookkeeping
# ---------------------------------------------------------------------------

def test_report_fields_are_filled_honestly(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    upstream_skip = ("Terminals/multi_placed.obj", "2 OBJECT placements")
    decision = _two_box_decision(needs_pad_b=True, skipped=[upstream_skip])
    report = apply(decision, pack_root, mesh_path)

    assert report.objects_written == [BOX_RESOURCE]
    assert report.vertices_offset_total == 8
    assert report.structures_baked == 2
    assert report.structures_needing_pad == 1
    assert upstream_skip in report.skipped
    assert report.orphaned_backups == []
    assert report.provenance_path == os.path.join(
        pack_root, PROVENANCE_FILENAME
    )
    with open(report.provenance_path) as handle:
        provenance = json.load(handle)
    assert provenance["version"] == 1
    mesh_entry = provenance["meshes"]["+35-081"]
    mesh_stat = os.stat(mesh_path)
    assert mesh_entry["path"] == mesh_path
    assert mesh_entry["size"] == mesh_stat.st_size
    assert mesh_entry["mtime"] == int(mesh_stat.st_mtime)
    object_entry = provenance["objects"][BOX_RESOURCE]
    assert object_entry["anchor_ground_m"] == 219.83
    assert object_entry["backup_sha256"] == _sha256_of(
        _backup_path(pack_root)
    )
    assert object_entry["written_sha256"] == _sha256_of(
        _live_path(pack_root)
    )


def test_non_finite_offset_is_refused(tmp_path):
    pack_root, mesh_path = _make_pack(
        tmp_path, {BOX_RESOURCE: _two_box_object_text()}
    )
    original_bytes = _read_bytes(_live_path(pack_root))
    decision = _two_box_decision(box_b_delta=float("nan"))
    report = apply(decision, pack_root, mesh_path)
    assert report.objects_written == []
    assert _read_bytes(_live_path(pack_root)) == original_bytes
    assert any(
        "non-finite" in reason for _resource, reason in report.skipped
    )
