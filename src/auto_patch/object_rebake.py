"""Apply, check and restore per-structure y offsets in pack ``.obj`` files.

Contract frozen by workstream W1 (``docs/dsf_object_integration_spec.md``
section 3.5 + workstream W5, as amended by A2/A6); implementation lands in
workstream W5, generalising the verified prototype
``tools/reanchor_kclt_terminal_bakes.py``.

Rulings in force (spec section 0):

* R1 — writes IN PLACE into the scenery pack, keeping ``<name>.anchor_bak``
  originals.  Geometry is always re-read from the backup, never from the
  live file, so applying is byte-idempotent and cannot stack (I-15).
* R2 — re-bake after every mesh build; the provenance sidecar
  (``<pack_root>/.o4_reanchor_provenance.json``) is a diagnostic, not a
  gate.  Corrected packs must never be redistributed (the sidecar carries
  that warning).

Only the ``y`` token of ``VT`` lines and positional-command lines changes;
whitespace, decimal precision and line count are preserved verbatim
(invariant I-16, via ``ObjectGeometry.vertex_line_indices`` and
``PositionalCommand.y_token_index`` — do not re-parse).

Backup adoption (amendment A2 — CRITICAL, a naive hash guard destroys the
KCLT originals, which are live-baked by the prototype today)::

    recorded hashes exist  -> three-way logic (invariant I-14):
        live == written_sha256  -> normal: re-bake from backup
        live == backup_sha256   -> someone restored: re-bake from backup
        neither                 -> the pack changed: move the stale backup
                                   to <name>.anchor_bak.orphaned, re-backup
                                   from live, re-bake, log loudly
    no recorded hashes     -> NEVER orphan.  An existing .anchor_bak is
                              authoritative (prototype semantics: created
                              once from the pristine file).  Adopt it,
                              compute both hashes, upgrade provenance.
    no backup at all       -> the live file is the original; back it up.

Provenance is keyed per (pack, mesh): the sidecar's ``meshes`` map is
keyed by tile, and each object entry names its tile (amendment A6).
``apply`` verifies the pack directory is writable before touching
anything and refuses the whole pool otherwise — a half-baked pool is torn
geometry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
from dataclasses import dataclass, field

from .obj8_reader import (
    POSITIONAL_COMMAND_COORDINATE_TOKEN_INDICES,
    ObjectGeometry,
    horizontal_bounding_box,
    load_object_file,
)
from .object_anchor import RebakeDecision, Structure

_LOGGER = logging.getLogger(__name__)

BACKUP_SUFFIX = ".anchor_bak"
ORPHANED_SUFFIX = ".orphaned"
PROVENANCE_FILENAME = ".o4_reanchor_provenance.json"
PROVENANCE_VERSION = 1
REDISTRIBUTION_WARNING = (
    "These .obj files were modified by Ortho4XP auto_patch to match "
    "locally graded terrain. DO NOT REDISTRIBUTE. Run "
    "tools/reanchor_dsf_objects.py --restore to undo."
)

# On a ``VT x y z …`` line the y value is the third whitespace-delimited
# token (the keyword is token 0).  Fixed by the OBJ8 format; the writer
# addresses WHICH lines are ``VT`` through
# ``ObjectGeometry.vertex_line_indices`` (invariant I-16), never by
# re-detecting the keyword.
VERTEX_Y_TOKEN_INDEX = 2

# Two per-structure offsets closer than this are the same offset.  The
# deltas of one structure's vertices are a single shared float, so any
# genuine cross-structure difference is far larger.
OFFSET_AGREEMENT_TOLERANCE_METRES = 1e-9


@dataclass(frozen=True)
class RebakeReport:
    """What ``apply`` did, for the pipeline reporter and the command line."""

    objects_written: list[str] = field(default_factory=list)
    vertices_offset_total: int = 0
    structures_baked: int = 0
    structures_needing_pad: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)
    orphaned_backups: list[str] = field(default_factory=list)
    provenance_path: str | None = None
    # Objects that carried a live bake but are EXCLUDED from the current
    # decision (no delta applied): restored byte-exact from ``.anchor_bak``
    # so the live pack always reflects exactly the current decision.
    objects_reverted: list[str] = field(default_factory=list)
    # Excluded objects whose live file still carries a bake but whose
    # ``.anchor_bak`` is gone: cannot be reverted, reported loudly, never
    # written blindly.
    reversions_missing_backup: list[str] = field(default_factory=list)
    # (resource_path, summary) for objects written with SOME structures
    # left unbaked (amendment A21): the passing structures' vertices
    # moved, the skipped structures' vertices keep their authored y.
    # Per-structure detail lands in the provenance sidecar entry.
    partially_baked: list[tuple[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# small shared helpers
# ---------------------------------------------------------------------------

def _sha256_of_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tile_name_from_mesh_path(mesh_path: str) -> str:
    """``.../Data+35-081.mesh`` -> ``+35-081`` (amendment A6: the meshes
    map and each object entry are keyed by tile name)."""
    base_name = os.path.basename(mesh_path)
    if base_name.startswith("Data") and base_name.endswith(".mesh"):
        return base_name[len("Data"):-len(".mesh")]
    return os.path.splitext(base_name)[0]


def _mesh_signature(mesh_path: str) -> dict:
    stat_result = os.stat(mesh_path)
    return {
        "path": mesh_path,
        "size": stat_result.st_size,
        "mtime": int(stat_result.st_mtime),
    }


def _provenance_path(pack_root: str) -> str:
    return os.path.join(pack_root, PROVENANCE_FILENAME)


def _fresh_provenance() -> dict:
    return {
        "version": PROVENANCE_VERSION,
        "warning": REDISTRIBUTION_WARNING,
        "meshes": {},
        "objects": {},
    }


def _normalise_provenance(raw: dict) -> dict:
    """Return a version-1 provenance dict, upgrading the prototype format.

    The prototype (``tools/reanchor_kclt_terminal_bakes.py``) wrote flat
    ``mesh``/``size``/``mtime`` keys, a single top-level ``anchor`` /
    ``anchor_ground``, and ``objects`` as a LIST of resource paths — and
    recorded no hashes.  The absence of hashes is what routes those
    objects through the amendment-A2 adoption path (never orphan).
    """
    if raw.get("version") == PROVENANCE_VERSION:
        raw.setdefault("warning", REDISTRIBUTION_WARNING)
        raw.setdefault("meshes", {})
        raw.setdefault("objects", {})
        return raw

    upgraded = _fresh_provenance()
    recorded_mesh_path = raw.get("mesh")
    tile = _tile_name_from_mesh_path(recorded_mesh_path or "")
    if recorded_mesh_path:
        upgraded["meshes"][tile] = {
            "path": recorded_mesh_path,
            "size": raw.get("size"),
            "mtime": raw.get("mtime"),
        }
    prototype_resources = raw.get("objects") or []
    if isinstance(prototype_resources, dict):  # defensive: already a map
        upgraded["objects"] = dict(prototype_resources)
    else:
        for resource_path in prototype_resources:
            upgraded["objects"][resource_path] = {
                "anchor": raw.get("anchor"),
                "anchor_ground_m": raw.get("anchor_ground"),
                "tile": tile,
            }
    return upgraded


def _load_provenance(pack_root: str) -> dict:
    sidecar_path = _provenance_path(pack_root)
    if not os.path.isfile(sidecar_path):
        return _fresh_provenance()
    with open(sidecar_path) as handle:
        return _normalise_provenance(json.load(handle))


# ---------------------------------------------------------------------------
# the y-token rewriter (invariants I-15 and I-16)
# ---------------------------------------------------------------------------

def _rewrite_y_tokens(
    source_path: str,
    destination_path: str,
    rewrite_plan_by_line: dict[int, tuple[float, int]],
    vertex_lines: set[int],
) -> int:
    """Rewrite exactly one whitespace token per planned line.

    ``rewrite_plan_by_line`` maps a 0-based line index to
    ``(elevation_delta, y_token_index)``.  Every other byte of the file —
    untouched lines, each touched line's whitespace runs (tabs included),
    the y value's decimal precision, and the line ending — is preserved
    verbatim (invariant I-16), ported from the prototype's
    ``_rewrite_vertex_elevations``.

    The file is read and written as latin-1 with ``newline=""`` so every
    byte (including non-UTF-8 bytes and ``\\r\\n`` endings) round-trips
    exactly on lines the plan does not touch.

    When the destination already holds EXACTLY the bytes about to be
    written (the byte-idempotent re-run, invariant I-15), the write is
    skipped so the file's mtime survives: the re-run used to rewrite
    identical bytes into every corrected ``.obj``, churning mtimes and
    with them every mtime-fingerprinted pack sidecar (classification,
    footprints, road network — ~40-55 s of recompute per pipeline run,
    2026-07-15 profile) plus X-Plane's own object cache.

    Returns the number of VERTEX lines rewritten (``vertex_lines``
    members), the honest ``vertices_offset_total`` contribution;
    positional-command rewrites are applied but not counted as vertices.
    """
    output: list[str] = []
    vertices_moved = 0
    with open(source_path, newline="", encoding="latin-1") as handle:
        for line_index, line in enumerate(handle):
            plan = rewrite_plan_by_line.get(line_index)
            if plan is None:
                output.append(line)
                continue
            elevation_delta, y_token_index = plan
            body = line.rstrip("\r\n")
            line_ending = line[len(body):]
            parts = re.split(r"([ \t]+)", body)
            value_positions = [
                position
                for position, part in enumerate(parts)
                if position % 2 == 0 and part != ""
            ]
            # value_positions[0] is the keyword; the y value sits at the
            # whitespace-token index the caller supplied.
            token_position_in_line = y_token_index
            if token_position_in_line >= len(value_positions):
                # Malformed line; leave it untouched rather than corrupt it.
                output.append(line)
                continue
            part_position = value_positions[token_position_in_line]
            original = parts[part_position]
            decimal_count = (
                len(original.split(".", 1)[1]) if "." in original else 6
            )
            parts[part_position] = (
                f"{float(original) + elevation_delta:.{decimal_count}f}"
            )
            output.append("".join(parts) + line_ending)
            if line_index in vertex_lines:
                vertices_moved += 1
    new_content = "".join(output)
    try:
        with open(
            destination_path, newline="", encoding="latin-1"
        ) as handle:
            if handle.read() == new_content:
                return vertices_moved   # byte-identical — keep the mtime
    except OSError:
        pass
    with open(
        destination_path, "w", newline="", encoding="latin-1"
    ) as handle:
        handle.write(new_content)
    return vertices_moved


# ---------------------------------------------------------------------------
# positional commands: which structure does a light belong to?
# ---------------------------------------------------------------------------

def _structure_boxes_and_deltas(
    geometry: ObjectGeometry,
    structures: list[Structure],
    resource_path: str,
    elevation_delta_by_vertex: dict[int, float],
) -> list[tuple[tuple[float, float, float, float], float]]:
    """Per structure contributing triangles to this resource: its
    horizontal bounding box in THIS object's local frame, and the single
    per-(structure, object) offset its vertices carry."""
    boxes_and_deltas = []
    for structure in structures:
        triangles = structure.triangles_by_resource.get(resource_path)
        if not triangles:
            continue
        bounding_box = horizontal_bounding_box(geometry.vertices, triangles)
        first_vertex_index = triangles[0][0]
        elevation_delta = elevation_delta_by_vertex.get(
            first_vertex_index, 0.0
        )
        boxes_and_deltas.append((bounding_box, elevation_delta))
    return boxes_and_deltas


def _horizontal_distance_to_box(
    bounding_box: tuple[float, float, float, float],
    local_x: float,
    local_z: float,
) -> float:
    minimum_x, maximum_x, minimum_z, maximum_z = bounding_box
    outside_x = max(minimum_x - local_x, 0.0, local_x - maximum_x)
    outside_z = max(minimum_z - local_z, 0.0, local_z - maximum_z)
    return math.hypot(outside_x, outside_z)


def _positional_command_rewrite_plan(
    geometry: ObjectGeometry,
    structures: list[Structure],
    resource_path: str,
    elevation_delta_by_vertex: dict[int, float],
) -> dict[int, tuple[float, int]]:
    """Assign each positional command (a light, a smoke puff, a magnet)
    the offset of the structure whose horizontal bounding box contains its
    ``(x, z)`` in the object's local frame; a command inside no box takes
    the nearest structure's offset (invariant I-10).  Ties resolve to the
    first structure in decision order, deterministically."""
    boxes_and_deltas = _structure_boxes_and_deltas(
        geometry, structures, resource_path, elevation_delta_by_vertex
    )
    if not boxes_and_deltas:
        return {}
    plan: dict[int, tuple[float, int]] = {}
    for command in geometry.positional_commands:
        _distance, elevation_delta = min(
            (
                (
                    _horizontal_distance_to_box(
                        bounding_box, command.x, command.z
                    ),
                    delta,
                )
                for bounding_box, delta in boxes_and_deltas
            ),
            key=lambda candidate: candidate[0],
        )
        plan[command.line_index] = (elevation_delta, command.y_token_index)
    return plan


# ---------------------------------------------------------------------------
# animation blocks (invariant I-11)
# ---------------------------------------------------------------------------

def _reconcile_animation_blocks(
    backup_path: str,
    geometry: ObjectGeometry,
    elevation_delta_by_vertex: dict[int, float],
    command_plan: dict[int, tuple[float, int]],
) -> dict[int, tuple[float, int]] | str:
    """With ``DSF_OBJECT_ALLOW_ANIM`` on, every maximal ``ANIM_begin`` …
    ``ANIM_end`` region must move as ONE rigid unit: all vertices its
    ``TRIS`` reference must carry the same offset (they belong to one
    structure), and positional commands inside the region take that same
    offset.  If a region's vertices span structures with differing
    offsets, return a skip reason string — baking would bend the
    animation's pivot (invariant I-11).

    ``obj8_reader`` counts ``ANIM_begin`` but does not expose block
    extents, so this helper scans the backup for the block line ranges
    and the index table.  It is read-only analysis; the writer itself
    never re-parses (invariant I-16).
    """
    command_by_line = {
        command.line_index: command
        for command in geometry.positional_commands
    }
    index_table: list[int] = []
    blocks: list[dict] = []
    current_block: dict | None = None
    animation_depth = 0
    with open(backup_path, errors="replace") as handle:
        for line_index, line in enumerate(handle):
            tokens = line.split()
            if not tokens:
                continue
            keyword = tokens[0]
            if keyword.startswith("IDX"):
                index_table.extend(int(token) for token in tokens[1:])
            elif keyword == "ANIM_begin":
                if animation_depth == 0:
                    current_block = {"triangle_ranges": [], "command_lines": []}
                    blocks.append(current_block)
                animation_depth += 1
            elif keyword == "ANIM_end":
                animation_depth = max(0, animation_depth - 1)
                if animation_depth == 0:
                    current_block = None
            elif animation_depth > 0 and current_block is not None:
                if keyword == "TRIS":
                    current_block["triangle_ranges"].append(
                        (int(tokens[1]), int(tokens[2]))
                    )
                elif keyword in POSITIONAL_COMMAND_COORDINATE_TOKEN_INDICES:
                    current_block["command_lines"].append(line_index)

    updated_plan = dict(command_plan)
    for block in blocks:
        vertex_indices: set[int] = set()
        for offset, count in block["triangle_ranges"]:
            vertex_indices.update(index_table[offset:offset + count])
        if not vertex_indices:
            # A light-only block keeps the normal per-command assignment.
            continue
        offsets = [
            elevation_delta_by_vertex.get(vertex_index, 0.0)
            for vertex_index in vertex_indices
        ]
        if max(offsets) - min(offsets) > OFFSET_AGREEMENT_TOLERANCE_METRES:
            return (
                "an ANIM_begin block's vertices span structures with "
                "differing offsets — baking would bend the animation "
                "pivot (invariant I-11)"
            )
        block_offset = offsets[0]
        for command_line_index in block["command_lines"]:
            command = command_by_line.get(command_line_index)
            if command is not None:
                updated_plan[command_line_index] = (
                    block_offset,
                    command.y_token_index,
                )
    return updated_plan


# ---------------------------------------------------------------------------
# the public contract
# ---------------------------------------------------------------------------

def apply(
    decision: RebakeDecision,
    pack_root: str,
    mesh_path: str,
) -> RebakeReport:
    """Rewrite the pool's ``.obj`` files per ``decision`` and write
    provenance.  Byte-idempotent (reads from ``.anchor_bak``, I-15);
    refuses objects with ``ANIM_begin`` unless ``DSF_OBJECT_ALLOW_ANIM``
    (invariant I-11) and any definition with more than one ``OBJECT``
    placement (invariant I-4).

    Baking is per STRUCTURE, not all-or-nothing per resource (amendment
    A21): a resource where some structures were skipped still bakes its
    passing structures' deltas — the skipped structures' vertices carry
    no delta, so the rewrite (always from ``.anchor_bak``) leaves them at
    their authored y.  Such objects are reported in
    ``RebakeReport.partially_baked`` and their provenance entry records
    each skipped structure (centroid, surface area, reason).  Only a
    resource in ``decision.skipped`` — every structure skipped, or a
    resource-level refusal — is refused entirely.

    The live pack always reflects EXACTLY the current decision: any object
    the decision EXCLUDES (its structures skipped, so it carries no delta)
    yet still holding a live bake from an earlier run is un-baked from its
    ``.anchor_bak`` and its stale provenance cleared (reversion pass,
    gated by ``O4_OBJECT_REBAKE_REVERT_EXCLUDED`` — default on; set 0 for
    the old keep-stale behaviour).  A missing backup is reported loudly and
    never overwritten."""
    # Function-local import so tests (and the environment) can drive the
    # flag at call time — the dsf_reader module-level-import trap, spec
    # section 4-W1.
    from .config import DSF_OBJECT_ALLOW_ANIM

    skipped: list[tuple[str, str]] = list(decision.skipped)
    skipped_upstream = {resource for resource, _reason in decision.skipped}
    resources = list(decision.delta_by_resource_and_vertex)
    structures_needing_pad = sum(
        1 for structure in decision.structures if structure.needs_pad
    )

    def _skip(resource_path: str, reason: str) -> None:
        _LOGGER.warning(
            "object re-anchor skipped %s: %s", resource_path, reason
        )
        skipped.append((resource_path, reason))

    def _refused_report(reason: str) -> RebakeReport:
        _LOGGER.warning(
            "object re-anchor refused the whole pool under %s: %s",
            pack_root,
            reason,
        )
        for resource_path in resources:
            skipped.append((resource_path, reason))
        return RebakeReport(
            skipped=skipped,
            structures_needing_pad=structures_needing_pad,
        )

    # --- pool-wide prechecks: nothing is touched until they all pass ---
    if not os.path.isfile(mesh_path):
        return _refused_report(f"mesh not found: {mesh_path}")

    target_directories = {pack_root}
    for resource_path in resources:
        target_directories.add(
            os.path.dirname(os.path.join(pack_root, resource_path))
        )
    unwritable = sorted(
        directory
        for directory in target_directories
        if not (os.path.isdir(directory) and os.access(directory, os.W_OK))
    )
    if unwritable:
        return _refused_report(
            "pack not writable — refusing the whole pool, a half-baked "
            "pool is torn geometry: " + ", ".join(unwritable)
        )

    # Defence in depth against invariant I-4: two decision resources that
    # resolve to the same file would bake one file twice.
    resources_by_normalised_path: dict[str, list[str]] = {}
    for resource_path in resources:
        normalised = os.path.normpath(
            os.path.join(pack_root, resource_path)
        )
        resources_by_normalised_path.setdefault(normalised, []).append(
            resource_path
        )
    duplicated_resources = {
        resource_path
        for group in resources_by_normalised_path.values()
        if len(group) > 1
        for resource_path in group
    }

    # Amendment A21 — per-structure skips within resources that still
    # bake.  The decision's structures carry their own ``skip_reason``;
    # aggregate them per resource here so a written object with skipped
    # structures gets (a) a ``partially_baked`` report entry and (b)
    # per-structure detail in its provenance entry.  Deterministic:
    # decision order.
    skipped_structures_by_resource: dict[str, list[Structure]] = {}
    for structure in decision.structures:
        if not structure.skip_reason:
            continue
        for structure_resource in structure.triangles_by_resource:
            skipped_structures_by_resource.setdefault(
                structure_resource, []
            ).append(structure)

    provenance = _load_provenance(pack_root)
    tile = _tile_name_from_mesh_path(mesh_path)
    objects_written: list[str] = []
    vertices_offset_total = 0
    orphaned_backups: list[str] = []
    partially_baked: list[tuple[str, str]] = []

    for resource_path in resources:
        if resource_path in duplicated_resources:
            _skip(
                resource_path,
                "duplicate resource in the decision — baking one file "
                "twice tears it (invariant I-4 defence)",
            )
            continue
        if resource_path in skipped_upstream:
            # Already reported by the decision; never bake a resource the
            # solver refused.
            continue

        live_path = os.path.join(pack_root, resource_path)
        backup_path = live_path + BACKUP_SUFFIX
        recorded_entry = provenance["objects"].get(resource_path, {})
        recorded_backup_hash = recorded_entry.get("backup_sha256")
        recorded_written_hash = recorded_entry.get("written_sha256")

        if os.path.isfile(backup_path):
            if recorded_backup_hash or recorded_written_hash:
                # Invariant I-14: three-way hash logic.
                if os.path.isfile(live_path):
                    live_hash = _sha256_of_file(live_path)
                    if live_hash not in (
                        recorded_backup_hash,
                        recorded_written_hash,
                    ):
                        orphaned_path = backup_path + ORPHANED_SUFFIX
                        os.replace(backup_path, orphaned_path)
                        shutil.copy2(live_path, backup_path)
                        orphaned_backups.append(orphaned_path)
                        _LOGGER.warning(
                            "PACK CHANGED: %s matches neither the recorded "
                            "backup nor the recorded written hash — the "
                            "stale backup was moved to %s and the live "
                            "file adopted as the new original "
                            "(invariant I-14)",
                            live_path,
                            orphaned_path,
                        )
            # No recorded hashes (prototype provenance, or none at all):
            # NEVER orphan — the existing backup is authoritative
            # (amendment A2); adopt it and upgrade provenance below.
        else:
            if not os.path.isfile(live_path):
                _skip(resource_path, "file not found in the pack")
                continue
            shutil.copy2(live_path, backup_path)

        geometry = load_object_file(backup_path)
        if geometry.animation_block_count > 0 and not DSF_OBJECT_ALLOW_ANIM:
            _skip(
                resource_path,
                f"{geometry.animation_block_count} ANIM_begin block(s) "
                "and DSF_OBJECT_ALLOW_ANIM is off (invariant I-11)",
            )
            continue
        if geometry.has_mixed_draped_solid_vertices:
            _skip(
                resource_path,
                "vertices shared between draped and solid triangles "
                "(invariant I-9)",
            )
            continue

        elevation_delta_by_vertex = decision.delta_by_resource_and_vertex[
            resource_path
        ]
        if any(
            not math.isfinite(delta)
            for delta in elevation_delta_by_vertex.values()
        ):
            _skip(resource_path, "non-finite offset in the decision")
            continue

        command_plan = _positional_command_rewrite_plan(
            geometry,
            decision.structures,
            resource_path,
            elevation_delta_by_vertex,
        )
        if geometry.animation_block_count > 0:
            reconciled = _reconcile_animation_blocks(
                backup_path,
                geometry,
                elevation_delta_by_vertex,
                command_plan,
            )
            if isinstance(reconciled, str):
                _skip(resource_path, reconciled)
                continue
            command_plan = reconciled

        rewrite_plan_by_line: dict[int, tuple[float, int]] = {}
        vertex_lines: set[int] = set()
        for vertex_index, line_index in enumerate(
            geometry.vertex_line_indices
        ):
            delta = elevation_delta_by_vertex.get(vertex_index)
            if delta:
                rewrite_plan_by_line[line_index] = (
                    delta,
                    VERTEX_Y_TOKEN_INDEX,
                )
                vertex_lines.add(line_index)
        for line_index, (delta, y_token_index) in command_plan.items():
            if delta:
                rewrite_plan_by_line[line_index] = (delta, y_token_index)

        vertices_offset_total += _rewrite_y_tokens(
            backup_path, live_path, rewrite_plan_by_line, vertex_lines
        )
        objects_written.append(resource_path)
        decision_anchor = getattr(decision, "anchor_by_resource", {}).get(
            resource_path
        )
        provenance_entry = {
            # Amendment A13: the decision carries each object's anchor;
            # a prototype-era recorded anchor survives as the fallback.
            "anchor": (
                list(decision_anchor)
                if decision_anchor is not None
                else recorded_entry.get("anchor")
            ),
            "anchor_ground_m": decision.anchor_ground_by_resource.get(
                resource_path, recorded_entry.get("anchor_ground_m")
            ),
            "tile": tile,
            "backup_sha256": _sha256_of_file(backup_path),
            "written_sha256": _sha256_of_file(live_path),
        }
        skipped_structures = skipped_structures_by_resource.get(
            resource_path
        )
        if skipped_structures:
            # Amendment A21: this object baked its passing structures
            # only.  Record each skipped structure so the sidecar shows
            # exactly which pieces stayed at their authored y and why.
            provenance_entry["structures_skipped"] = [
                {
                    "centroid_latitude": structure.centroid_latitude,
                    "centroid_longitude": structure.centroid_longitude,
                    "surface_area_square_metres": (
                        structure.surface_area_square_metres
                    ),
                    "reason": structure.skip_reason,
                }
                for structure in skipped_structures
            ]
            summary = (
                f"{len(skipped_structures)} structure(s) left at their "
                "authored y (skipped), passing structures baked; first "
                f"reason: {skipped_structures[0].skip_reason}"
            )
            partially_baked.append((resource_path, summary))
            _LOGGER.info(
                "object re-anchor partially baked %s: %s",
                resource_path,
                summary,
            )
        provenance["objects"][resource_path] = provenance_entry

    # --- reversion pass: the live pack always reflects the current
    # decision.  An object EXCLUDED from this decision (its structures
    # skipped, so it carries no delta) but still holding a live bake from
    # an earlier run keeps floating at that stale offset — nothing above
    # ever visits it, because the main loop iterates only baked resources.
    # Un-bake it from its ``.anchor_bak`` and clear the stale provenance
    # (a roof at its authored height beats a roof at a stale-mesh height —
    # the WED-authored placement the pack author validated).  The
    # conservative default is ON; O4_OBJECT_REBAKE_REVERT_EXCLUDED=0
    # restores the old keep-stale behaviour.
    objects_reverted: list[str] = []
    reversions_missing_backup: list[str] = []
    revert_excluded = (
        os.environ.get("O4_OBJECT_REBAKE_REVERT_EXCLUDED", "1") != "0"
    )
    if revert_excluded:
        skip_reason_by_resource: dict[str, str] = {}
        for skipped_resource, reason in skipped:
            skip_reason_by_resource.setdefault(skipped_resource, reason)
        # Every resource this pool's decision knows about — baked,
        # skipped, or merely present in a structure — minus the ones just
        # written.  Scoped to this decision (one resource lives in exactly
        # one pool, invariant I-4), so this never touches another pool's
        # objects.
        decision_known: set[str] = set(resources)
        decision_known.update(decision.anchor_ground_by_resource)
        decision_known.update(
            getattr(decision, "anchor_by_resource", {})
        )
        for skipped_resource, _reason in decision.skipped:
            decision_known.add(skipped_resource)
        for structure in decision.structures:
            decision_known.update(structure.triangles_by_resource)
        written_this_run = set(objects_written)
        for resource_path in sorted(decision_known - written_this_run):
            live_path = os.path.join(pack_root, resource_path)
            backup_path = live_path + BACKUP_SUFFIX
            recorded_entry = provenance["objects"].get(resource_path, {})
            recorded_backup_hash = recorded_entry.get("backup_sha256")
            recorded_written_hash = recorded_entry.get("written_sha256")
            provenance_says_baked = (
                recorded_written_hash is not None
                and recorded_written_hash != recorded_backup_hash
            )
            backup_exists = os.path.isfile(backup_path)
            live_exists = os.path.isfile(live_path)
            live_differs_from_backup = (
                backup_exists
                and live_exists
                and _sha256_of_file(live_path)
                != _sha256_of_file(backup_path)
            )
            if not (provenance_says_baked or live_differs_from_backup):
                # No live bake to undo — leave it untouched.
                continue
            if not backup_exists:
                # Safety rule: never write a pack file without its backup.
                _LOGGER.warning(
                    "object re-anchor cannot revert %s: it is excluded "
                    "from the current decision and its live file still "
                    "carries a bake, but %s is missing — left as-is, NOT "
                    "overwritten",
                    live_path,
                    os.path.basename(backup_path),
                )
                reversions_missing_backup.append(resource_path)
                continue
            reason = skip_reason_by_resource.get(
                resource_path,
                "excluded from the current decision — no bake applied",
            )
            if live_differs_from_backup:
                # Byte-exact restore from the authored original.
                shutil.copy2(backup_path, live_path)
                objects_reverted.append(resource_path)
                _LOGGER.warning(
                    "object re-anchor REVERTED %s to its authored "
                    "placement (%s): %s",
                    live_path,
                    os.path.basename(backup_path),
                    reason,
                )
            backup_hash = _sha256_of_file(backup_path)
            provenance["objects"][resource_path] = {
                "anchor": (
                    list(decision.anchor_by_resource[resource_path])
                    if resource_path in getattr(
                        decision, "anchor_by_resource", {}
                    )
                    else recorded_entry.get("anchor")
                ),
                # Stale anchor_ground must not survive a mesh change for an
                # excluded object: the current sample if we have one, else
                # dropped rather than left pointing at the old mesh.
                "anchor_ground_m": (
                    decision.anchor_ground_by_resource.get(resource_path)
                ),
                "tile": tile,
                "backup_sha256": backup_hash,
                # Applied delta 0 — live now equals the backup.
                "written_sha256": backup_hash,
                "excluded_reason": reason,
            }

    provenance_path: str | None = None
    if objects_written or objects_reverted:
        provenance["meshes"][tile] = _mesh_signature(mesh_path)
        provenance_path = _provenance_path(pack_root)
        with open(provenance_path, "w") as handle:
            json.dump(provenance, handle, indent=2)
            handle.write("\n")

    written_set = set(objects_written)
    structures_baked = 0
    for structure in decision.structures:
        if structure.skip_reason:
            # A skipped structure's resource may still have been written
            # for its OTHER structures (amendment A21) — the skipped one
            # carried no delta and did not bake.
            continue
        contributing = {
            resource_path
            for resource_path, triangles in (
                structure.triangles_by_resource.items()
            )
            if triangles
        }
        if contributing and contributing <= written_set:
            structures_baked += 1

    return RebakeReport(
        objects_written=objects_written,
        vertices_offset_total=vertices_offset_total,
        structures_baked=structures_baked,
        structures_needing_pad=structures_needing_pad,
        skipped=skipped,
        orphaned_backups=orphaned_backups,
        provenance_path=provenance_path,
        objects_reverted=objects_reverted,
        reversions_missing_backup=reversions_missing_backup,
        partially_baked=partially_baked,
    )


def check(pack_root: str, mesh_path: str) -> str:
    """Compare recorded provenance against the mesh on disk.

    Returns ``"CURRENT"``, ``"STALE"`` (the mesh was rebuilt since the
    bake — re-run, it reads from the backups) or ``"NONE"`` (no bake
    recorded).

    Comparison is size + mtime per mesh (amendment A6: the sidecar's
    ``meshes`` map is keyed by tile).  A prototype-format sidecar (flat
    ``mesh``/``size``/``mtime`` keys, no ``version``) is tolerated: it is
    normalised in memory before the comparison.
    """
    sidecar_path = _provenance_path(pack_root)
    if not os.path.isfile(sidecar_path):
        return "NONE"
    with open(sidecar_path) as handle:
        provenance = _normalise_provenance(json.load(handle))
    recorded = provenance["meshes"].get(_tile_name_from_mesh_path(mesh_path))
    if recorded is None or not os.path.isfile(mesh_path):
        # A bake exists but not against this mesh (or the mesh is gone):
        # whatever is baked cannot match this mesh.
        return "STALE"
    current = _mesh_signature(mesh_path)
    if (
        recorded.get("size") == current["size"]
        and recorded.get("mtime") == current["mtime"]
    ):
        return "CURRENT"
    return "STALE"


def restore(pack_root: str) -> int:
    """Put the ``.anchor_bak`` originals back, byte-identically, remove
    the provenance sidecar, and return the number of files restored.

    ``<name>.anchor_bak.orphaned`` files (invariant I-14 relics) are left
    alone: they are not originals of the current pack.
    """
    restored = 0
    for directory, _subdirectories, filenames in os.walk(pack_root):
        for filename in filenames:
            if not filename.endswith(BACKUP_SUFFIX):
                continue
            backup_path = os.path.join(directory, filename)
            live_path = backup_path[:-len(BACKUP_SUFFIX)]
            shutil.copy2(backup_path, live_path)
            restored += 1
    sidecar_path = _provenance_path(pack_root)
    if os.path.isfile(sidecar_path):
        os.remove(sidecar_path)
    return restored
