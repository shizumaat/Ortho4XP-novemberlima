"""Post-mesh stage: re-anchor DSF objects against the freshly built mesh.

Contract frozen by workstream W1 (``docs/dsf_object_integration_spec.md``
section 4-W7, as amended by A4/A5); implementation lands in workstream W7.

Phase 2 of the DSF object integration cannot run before the mesh exists —
the y offsets encode one specific built ``Data<tile>.mesh``.  The hook is
a single guarded call at the END of ``O4_Mesh_Utils.build_mesh`` (and
``sort_mesh``), not in ``build_all``'s callers: the GUI's per-step Mesh
button and Shift-click sort_mesh bypass ``build_all`` entirely, and an
out-of-band rebuild is exactly how the 1.19 m staleness incident happened
(amendment A4).  The mesh path comes from ``FNAMES.mesh_file(
tile.build_dir, ...)`` — never the ``Custom Scenery/zOrtho4XP_*`` symlink
the prototype hard-coded.

Inputs arrive via the worklist sidecar
``Patches/<lon lat>/o4_object_anchor_worklist.json``, written once per
tile by the driver's MAIN process (workers race, amendment A5) before the
rebuild-skip gate, carrying identification only::

    {"version": 1, "tile": "+35-081", "xplane_root": ...,
     "airports": [{"icao": ..., "dsf_path": ..., "dsf_mtime": ...,
                   "pack_root": ...}]}

Discovery (placements, pools, partition) happens here, post-mesh — the
geometry caches key on (path, mtime), so repeat builds are cheap and
there is no stale-groups special case.

Reporting follows ``verification.verify_and_log``: runtime validators are
pure reporters; an exception here must NEVER fail the tile (the caller
wraps this in try/except).  Console gets one summary line per airport;
full detail goes to the per-tile debug log, plus the "restart X-Plane,
objects are cached" reminder once per corrected pack.
"""

from __future__ import annotations


def rebake_dsf_objects(tile) -> dict:
    """Run Phase 2 for every airport in ``tile``'s worklist.

    Returns a ``counts``-style dict (structures baked, vertices offset,
    packs corrected, skipped, needing pads) for the caller's summary
    line.  Returns immediately — before reading anything — unless
    ``DSF_OBJECT_REANCHOR`` is on (function-local config import so tests
    can drive the flag).  A missing worklist means nothing to do.
    """
    raise NotImplementedError("workstream W7")
