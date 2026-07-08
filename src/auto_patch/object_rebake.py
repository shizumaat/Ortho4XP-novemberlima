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

from dataclasses import dataclass, field

from .object_anchor import RebakeDecision


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


def apply(
    decision: RebakeDecision,
    pack_root: str,
    mesh_path: str,
) -> RebakeReport:
    """Rewrite the pool's ``.obj`` files per ``decision`` and write
    provenance.  Byte-idempotent (reads from ``.anchor_bak``, I-15);
    refuses objects with ``ANIM_begin`` unless ``DSF_OBJECT_ALLOW_ANIM``
    (invariant I-11) and any definition with more than one ``OBJECT``
    placement (invariant I-4)."""
    raise NotImplementedError("workstream W5")


def check(pack_root: str, mesh_path: str) -> str:
    """Compare recorded provenance against the mesh on disk.

    Returns ``"CURRENT"``, ``"STALE"`` (the mesh was rebuilt since the
    bake — re-run, it reads from the backups) or ``"NONE"`` (no bake
    recorded).
    """
    raise NotImplementedError("workstream W5")


def restore(pack_root: str) -> int:
    """Put the ``.anchor_bak`` originals back, byte-identically, remove
    the provenance sidecar, and return the number of files restored."""
    raise NotImplementedError("workstream W5")
