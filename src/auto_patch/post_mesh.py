"""Post-mesh stage: re-anchor DSF objects against the freshly built mesh.

Contract frozen by workstream W1 (``docs/dsf_object_integration_spec.md``
section 4-W7, as amended by A4/A5); implemented in workstream W7.

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
                   "pack_root": ..., "xplane_root": ...}]}

Discovery (placements, pools, partition) happens here, post-mesh — the
geometry caches key on (path, mtime), so repeat builds are cheap and
there is no stale-groups special case.  A worklist ``dsf_mtime`` that
disagrees with disk is merely noted: discovery is authoritative, the
worklist is identification only (amendment A5), and the CURRENT DSF is
what discovery reads regardless.

Invariant I-4's enforcement point is HERE (amendment A13, item 2): a
resource with more than one terrain-draped ``OBJECT`` placement is
excluded from Phase 2 before any decision is built — a shared ``.obj``
file cannot carry per-placement offsets.  (Phase 1 ACCEPTS the same
resources: N placements are N buildings, invariant I-5.)

Reporting follows ``verification.verify_and_log``: runtime validators are
pure reporters; an exception here must NEVER fail the tile (the caller
wraps this in try/except, and the function additionally contains every
per-airport failure itself).  Console gets one summary line per airport;
full detail goes to verbosity level 2, plus the "restart X-Plane
(objects are cached)" reminder once per corrected pack.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle

import O4_UI_Utils as UI

from . import dsf_reader, obj8_reader, object_anchor, object_rebake
from .mesh_sampler import MeshElevationSampler

# One file per tile, next to the tile's auto-patches.  The driver writes
# it (main process only); this module and tools/reanchor_dsf_objects.py
# read it.
OBJECT_ANCHOR_WORKLIST_FILENAME = "o4_object_anchor_worklist.json"
OBJECT_ANCHOR_WORKLIST_VERSION = 1

# Per-tile record of the foot-pad REQUESTS the foot re-anchor raised
# (multi-ground-cluster objects whose best rigid offset still leaves a
# foot off the mesh past ``DSF_OBJECT_FOOT_PAD_RESIDUAL_M``).  Written
# next to the worklist after every rebake — refreshed each run, removed
# when no request remains — carrying, per foot, the pad ring
# (``object_footprints.foot_pad_ring``) and the target ground
# elevation.  A future terrain-shaping stream consumes it; until then
# it is the durable audit trail for feet a rigid body cannot seat.
OBJECT_FOOT_PAD_SIDECAR_FILENAME = "o4_object_foot_pads.json"
OBJECT_FOOT_PAD_SIDECAR_VERSION = 1

# The counts returned by rebake_dsf_objects, all starting at zero.
_COUNT_KEYS = (
    "airports_processed",
    "packs_corrected",
    "structures_baked",
    "structures_needing_pad",
    "vertices_offset",
    "objects_skipped",
    "objects_reverted",
    "objects_partially_baked",
    "airports_failed",
    "foot_pad_requests",
)


def object_anchor_worklist_path(tile) -> str:
    """The tile's worklist sidecar path, derived the same way the driver
    derives its auto-patch paths (``FNAMES.patch_dir``)."""
    import O4_File_Names as FNAMES

    return os.path.join(
        FNAMES.patch_dir(
            int(math.floor(tile.lat)), int(math.floor(tile.lon))
        ),
        OBJECT_ANCHOR_WORKLIST_FILENAME,
    )


def _mesh_is_newer_than_alt(tile, mesh_path: str) -> bool:
    """O3 ordering guard: is the built mesh at least as new as the tile's
    ``.alt`` elevation raster?

    The mesh is derived from the ``.alt`` written in step 1 (with airport
    elevation insets baked in).  Returns ``True`` when the mesh is newer
    than (or equal to) the newest ``.alt`` for this tile, so the object
    y-bake is sampling the current elevation state.  Returns ``False`` only
    when an ``.alt`` exists and is STRICTLY newer than the mesh (stale mesh
    -- the caller must not sample it).  When no ``.alt`` is found on disk
    (already cleaned up, or a standalone probe), returns ``True`` -- there
    is nothing to be stale against and the mesh is authoritative.
    """
    import glob

    import O4_File_Names as FNAMES

    try:
        mesh_mtime = os.path.getmtime(mesh_path)
    except OSError:
        return True
    # All .alt variants for this tile (plain ``Data<tile>.alt`` plus any
    # ``Data<tile>.<iterate>.alt`` from the densify path).
    tile_stub = os.path.join(
        tile.build_dir, "Data" + FNAMES.short_latlon(tile.lat, tile.lon)
    )
    alt_candidates = [tile_stub + ".alt"] + glob.glob(tile_stub + ".*.alt")
    newest_alt_mtime = None
    for alt_path in alt_candidates:
        try:
            alt_mtime = os.path.getmtime(alt_path)
        except OSError:
            continue
        if newest_alt_mtime is None or alt_mtime > newest_alt_mtime:
            newest_alt_mtime = alt_mtime
    if newest_alt_mtime is None:
        return True
    # A one-second slack absorbs filesystem mtime granularity (some FSes
    # store whole seconds); only a genuinely older mesh trips the guard.
    return mesh_mtime >= newest_alt_mtime - 1.0


def _is_protected_scenery_root(pack_root: str) -> bool:
    """True when ``pack_root`` lies inside the base simulator — any path
    with a ``Global Scenery`` or ``Resources`` component (amendment A15).
    Only Custom Scenery packs are ever rebake candidates."""
    components = os.path.normpath(pack_root).split(os.sep)
    return "Global Scenery" in components or "Resources" in components


def _resolved_path_is_inside_pack(physical_path: str, pack_root: str) -> bool:
    """True when the resolved object file lives under the pack that owns
    the DSF.  A ``library.txt``-resolved resource lands in ANOTHER pack:
    a shared library object serves many airports and must never carry one
    airport's offsets (amendment A15, guard 2)."""
    try:
        return (
            os.path.commonpath(
                [os.path.abspath(physical_path), os.path.abspath(pack_root)]
            )
            == os.path.abspath(pack_root)
        )
    except ValueError:  # different drives (Windows)
        return False


def _pool_world_bounds(
    pool: object_anchor.ObjectPool,
    geometry_by_resource: dict,
) -> tuple[float, float, float, float]:
    """``(minimum_longitude, minimum_latitude, maximum_longitude,
    maximum_latitude)`` covering every pool member's anchor expanded by
    its solid reach.  Reach bounds every solid vertex by definition, so
    the sampler (which adds its own ~200 m ``margin_degrees``) retains
    every triangle a structure or anchor sample can touch."""
    minimum_longitude = minimum_latitude = math.inf
    maximum_longitude = maximum_latitude = -math.inf
    for placement in pool.placements:
        geometry = geometry_by_resource.get(placement.resource_path)
        reach_metres = (
            geometry.solid_reach_metres() if geometry is not None else 0.0
        )
        latitude_degrees = (
            reach_metres / obj8_reader.METRES_PER_DEGREE_LATITUDE
        )
        metres_per_degree_longitude = (
            obj8_reader.METRES_PER_DEGREE_LATITUDE
            * math.cos(math.radians(placement.latitude))
        )
        longitude_degrees = (
            reach_metres / metres_per_degree_longitude
            if metres_per_degree_longitude > 0.0
            else 0.0
        )
        minimum_longitude = min(
            minimum_longitude, placement.longitude - longitude_degrees
        )
        maximum_longitude = max(
            maximum_longitude, placement.longitude + longitude_degrees
        )
        minimum_latitude = min(
            minimum_latitude, placement.latitude - latitude_degrees
        )
        maximum_latitude = max(
            maximum_latitude, placement.latitude + latitude_degrees
        )
    return (
        minimum_longitude,
        minimum_latitude,
        maximum_longitude,
        maximum_latitude,
    )


# Bump when partition_structures' output shape or semantics change, or
# when anything new starts feeding the partition (the pickle payload and
# the hash must both change meaning together).
_PARTITION_CACHE_VERSION = 2  # 2: oversized-chain connector split


def _cached_partition_structures(
    pool,
    geometry_by_resource,
    geometry_source_by_resource,
    pack_root,
    epsilon_metres,
):
    """``object_anchor.partition_structures`` behind a pack sidecar cache.

    The partition is a pure function of the pool's placements and the
    authored OBJ8 geometry (module contract in ``object_anchor``): it
    never touches the mesh or DEM, yet dominated the whole post-mesh
    rebake (profiled 2026-07-15: 195 s of 385 s at KBNA) and reruns on
    every mesh build.  Cached per pool under
    ``Airport_mod_cache/<pack>/`` (the ruling-established home for
    Ortho4XP-only sidecars).

    The key is a CONTENT hash — placements plus the bytes of each
    geometry source file (the ``.anchor_bak`` original when present,
    ruling R1) — not a size/mtime fingerprint: the rebake itself
    rewrites pack ``.obj`` files every run, so mtimes churn while the
    partition inputs stay identical.  ``object_anchor`` stays pure; the
    file input/output lives here with the rest of Phase 2's disk work.
    ``O4_OBJECT_PARTITION_CACHE=0`` disables.  Cache misses (corrupt or
    version-skewed files included) silently recompute.
    """
    cache_directory = dsf_reader.airport_mod_cache_dir(pack_root)
    if (
        cache_directory is None
        or os.environ.get("O4_OBJECT_PARTITION_CACHE", "1") != "1"
    ):
        return object_anchor.partition_structures(
            pool, geometry_by_resource, epsilon_metres=epsilon_metres
        )

    from .config import DSF_OBJECT_ELEVATED_BASE_M

    digest = hashlib.sha1()
    digest.update(
        repr(
            (
                _PARTITION_CACHE_VERSION,
                epsilon_metres,
                DSF_OBJECT_ELEVATED_BASE_M,
            )
        ).encode()
    )
    for placement in pool.placements:
        digest.update(repr(placement).encode())
    for resource_path in sorted(
        {placement.resource_path for placement in pool.placements}
    ):
        source_path = geometry_source_by_resource.get(resource_path)
        if source_path is None:
            continue
        digest.update(resource_path.encode())
        try:
            with open(source_path, "rb") as handle:
                digest.update(handle.read())
        except OSError:
            # Unreadable source — do not risk a stale key; skip caching.
            return object_anchor.partition_structures(
                pool, geometry_by_resource, epsilon_metres=epsilon_metres
            )
    cache_path = os.path.join(
        cache_directory,
        "o4_object_partition_%s.cache" % digest.hexdigest()[:16],
    )

    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "rb") as handle:
                payload = pickle.load(handle)
            if payload.get("version") == _PARTITION_CACHE_VERSION:
                return payload["structures"]
        except Exception:
            pass  # corrupt/unreadable cache — recompute below

    structures = object_anchor.partition_structures(
        pool, geometry_by_resource, epsilon_metres=epsilon_metres
    )
    try:
        os.makedirs(cache_directory, exist_ok=True)
        temporary_path = cache_path + ".tmp"
        with open(temporary_path, "wb") as handle:
            pickle.dump(
                {
                    "version": _PARTITION_CACHE_VERSION,
                    "structures": structures,
                },
                handle,
            )
        os.replace(temporary_path, cache_path)
    except OSError:
        pass  # caching is best-effort; the result is already computed
    return structures


def discover_and_rebake_airport(
    dsf_path: str,
    mesh_path: str,
    pack_root: str | None,
    xplane_root: str | None,
    *,
    epsilon_metres: float | None = None,
    write_changes: bool = True,
    excluded_resources: set[tuple[str, str]] | None = None,
) -> dict:
    """Run the full Phase 2 discovery pipeline for one airport's DSF.

    Discovery: ``_load_dsf_text`` → ``read_dsf_object_placements``
    (``.obj`` only) → resolve (pack-relative wins) → parse geometry
    (memoized ``dsf_reader._load_object_geometry``) → reach floor
    ``DSF_OBJECT_MIN_REACH_M`` → **exclude any resource with more than
    one terrain-draped placement, skip-and-report (invariant I-4,
    enforced here per amendment A13)** → ``discover_object_pools`` →
    per pool: one :class:`MeshElevationSampler` bounded by the pool's
    world extent plus margin → ``partition_structures`` →
    ``structure_deltas`` → ``object_rebake.apply``.

    Shared by :func:`rebake_dsf_objects` and the command line
    (``tools/reanchor_dsf_objects.py`` mode 2) so the two can never
    drift.  With ``write_changes=False`` nothing on disk is touched; the
    decisions are returned for reporting (the command line's
    ``--dry-run``).

    ``excluded_resources`` (ruling R4, object terrain features spec):
    ``(pack_root, resource_path)`` pairs whose terrain was carved or
    seated to match the object (a structure consumed by terrain feature
    A or B).  Terrain-to-object and object-to-terrain corrections must
    never stack, so these placements are dropped BEFORE discovery, each
    with a skip-and-report entry.  ``None`` / empty means no exclusions
    (the pre-change behaviour, and the only behaviour while the
    ``O4_OBJECT_BRIDGE_TERRAIN`` / tunnel gates are off).

    Returns a dict with ``objects_written`` (resource paths),
    ``vertices_offset``, ``structures_baked``, ``structures_needing_pad``,
    ``skipped`` (``(resource_path_or_dsf, reason)`` tuples, discovery and
    rebake levels merged), and ``decisions``
    (``(ObjectPool, RebakeDecision)`` pairs, for detailed reporting).
    Pure data out — printing is the caller's business.
    """
    from .config import (
        DSF_OBJECT_CONTACT_EPSILON_M,
        DSF_OBJECT_ELEVATED_BASE_M,
        DSF_OBJECT_FOOT_ANCHOR,
        DSF_OBJECT_FOOT_MIN_REACH_M,
        DSF_OBJECT_MIN_REACH_M,
    )

    if epsilon_metres is None:
        epsilon_metres = DSF_OBJECT_CONTACT_EPSILON_M

    result: dict = {
        "objects_written": [],
        "vertices_offset": 0,
        "structures_baked": 0,
        "structures_needing_pad": 0,
        "skipped": [],
        "decisions": [],
        # object_anchor.FootPadRequest instances, all pools merged.
        "foot_pad_requests": [],
        # Objects un-baked because they are excluded from the current
        # decision but still carried a stale live bake (reversion pass).
        "objects_reverted": [],
        "reversions_missing_backup": [],
        # Objects written with SOME structures left unbaked (amendment
        # A21): (resource_path, summary) pairs from the rebake report.
        "partially_baked": [],
    }

    lines = dsf_reader._load_dsf_text(dsf_path)
    if not lines:
        result["skipped"].append(
            (dsf_path, "DSF text unavailable (missing file or DSFTool)")
        )
        return result
    placements = obj8_reader.read_dsf_object_placements(
        lines,
        accept_resource=lambda resource: resource.lower().endswith(".obj"),
    )
    if not placements:
        return result
    if pack_root is None:
        pack_root = dsf_reader._pack_root_for_dsf(dsf_path)

    # Amendment A15, guard 1: base and global scenery are NEVER rebaked.
    # Small airports resolve to the Global Airports DSF, whose static
    # airliners and library hangars pass the reach floor (a large,
    # correctly anchored object has a large reach — the metric conflates
    # size with mis-anchoring), and on a writable install Phase 2 would
    # modify the base simulator.  Policy, not permission luck.
    if pack_root is None or _is_protected_scenery_root(pack_root):
        result["skipped"].append(
            (
                dsf_path,
                "pack is base or global scenery — never rebaked "
                "(amendment A15)",
            )
        )
        return result

    # Ruling R4: objects whose terrain was adapted TO them (feature A/B)
    # are excluded from the Phase 2 y-bake — the two corrections must
    # never stack.  Filter before discovery, skip-and-report each drop.
    if excluded_resources:
        dropped = sorted({
            placement.resource_path
            for placement in placements
            if (pack_root or "", placement.resource_path)
            in excluded_resources
        })
        if dropped:
            placements = [
                placement
                for placement in placements
                if (pack_root or "", placement.resource_path)
                not in excluded_resources
            ]
            for resource_path in dropped:
                result["skipped"].append(
                    (
                        resource_path,
                        "terrain adapted to this object (object terrain "
                        "feature A/B) — excluded from the Phase 2 y-bake "
                        "(ruling R4)",
                    )
                )
            if not placements:
                return result

    placement_count_by_resource: dict[str, int] = {}
    for placement in placements:
        placement_count_by_resource[placement.resource_path] = (
            placement_count_by_resource.get(placement.resource_path, 0) + 1
        )

    resolved_paths: dict[str, str] = {}
    geometry_by_resource: dict = {}
    geometry_source_by_resource: dict[str, str] = {}
    for resource_path in sorted(
        {placement.resource_path for placement in placements}
    ):
        physical_path = obj8_reader.resolve_object_resource(
            resource_path, pack_root, xplane_root
        )
        if physical_path is None:
            continue
        # Amendment A15, guard 2: a library-resolved resource lives in
        # ANOTHER pack; baking it would push one airport's offsets into
        # an object shared by many.  Skip-and-report.
        if not _resolved_path_is_inside_pack(physical_path, pack_root):
            result["skipped"].append(
                (
                    resource_path,
                    "resolved through library.txt outside the pack — "
                    "shared library objects are never rebaked "
                    "(amendment A15)",
                )
            )
            continue
        # Ruling R1: geometry is ALWAYS read from the backup when one
        # exists — on a re-run the live file already carries baked
        # offsets, and parsing it would misclassify every corrected
        # structure as elevated (its base y is no longer 0).  The
        # rebake writer makes the same choice (``object_rebake.apply``
        # reads ``<name>.anchor_bak``); vertex ordering is identical in
        # both files, so the per-vertex deltas line up.
        backup_path = physical_path + object_rebake.BACKUP_SUFFIX
        geometry_source_path = (
            backup_path if os.path.isfile(backup_path) else physical_path
        )
        geometry = dsf_reader._load_object_geometry(geometry_source_path)
        if geometry is None or not geometry.has_solid_geometry:
            continue
        # The reach floor keeps compact, correctly anchored objects out
        # of Phase 2 — but an author-BAKED vertical offset breaks the
        # metric's premise: X-Plane mis-places such an object no matter
        # how compact it is (the KBNA stairs reach 24.3 m and 20.6 m,
        # under the floor).  Baked-offset geometry — lowest solid vertex
        # above the elevated threshold — is admitted at the reduced
        # foot-re-anchor floor instead (config rationale at
        # DSF_OBJECT_FOOT_MIN_REACH_M).
        minimum_solid_y = min(
            geometry.vertices[vertex_index][1]
            for triangle in geometry.solid_triangles
            for vertex_index in triangle
        )
        reach_floor_metres = (
            DSF_OBJECT_FOOT_MIN_REACH_M
            if DSF_OBJECT_FOOT_ANCHOR
            and minimum_solid_y > DSF_OBJECT_ELEVATED_BASE_M
            else DSF_OBJECT_MIN_REACH_M
        )
        if geometry.solid_reach_metres() < reach_floor_metres:
            continue
        # Invariant I-4, enforced at Phase 2 discovery (amendment A13):
        # a resource with several terrain-draped placements would need a
        # different correction per placement, which one shared file
        # cannot carry.  Phase 1 accepts the same resource (N placements
        # = N buildings, invariant I-5); Phase 2 must not.
        placement_count = placement_count_by_resource[resource_path]
        if placement_count > 1:
            result["skipped"].append(
                (
                    resource_path,
                    f"{placement_count} terrain-draped OBJECT placements "
                    "— a shared file cannot carry per-placement offsets "
                    "(invariant I-4)",
                )
            )
            continue
        resolved_paths[resource_path] = physical_path
        geometry_by_resource[resource_path] = geometry
        geometry_source_by_resource[resource_path] = geometry_source_path
    if not resolved_paths:
        return result

    candidate_placements = [
        placement
        for placement in placements
        if placement.resource_path in resolved_paths
    ]
    pools = object_anchor.discover_object_pools(
        candidate_placements,
        resolved_paths,
        geometry_by_resource,
        epsilon_metres=epsilon_metres,
    )

    for pool in pools:
        pool_geometry_by_resource = {
            resource_path: geometry_by_resource[resource_path]
            for resource_path in pool.resolved_paths
        }
        bounds = _pool_world_bounds(pool, pool_geometry_by_resource)
        try:
            sampler = MeshElevationSampler(mesh_path, bounds)
        except ValueError as error:
            # No mesh triangles under this pool — a pool that walked off
            # the tile.  Skip-and-report, never guess (invariant I-13).
            for placement in pool.placements:
                result["skipped"].append(
                    (
                        placement.resource_path,
                        f"no mesh triangles under the pool ({error})",
                    )
                )
            continue
        structures = _cached_partition_structures(
            pool,
            pool_geometry_by_resource,
            geometry_source_by_resource,
            pack_root,
            epsilon_metres,
        )
        decision = object_anchor.structure_deltas(
            pool, pool_geometry_by_resource, structures, sampler
        )
        result["decisions"].append((pool, decision))
        result["foot_pad_requests"].extend(decision.foot_pad_requests)
        if write_changes:
            report = object_rebake.apply(decision, pack_root, mesh_path)
            result["objects_written"].extend(report.objects_written)
            result["vertices_offset"] += report.vertices_offset_total
            result["structures_baked"] += report.structures_baked
            result["structures_needing_pad"] += (
                report.structures_needing_pad
            )
            result["skipped"].extend(report.skipped)
            result["objects_reverted"].extend(report.objects_reverted)
            result["reversions_missing_backup"].extend(
                report.reversions_missing_backup
            )
            result["partially_baked"].extend(report.partially_baked)
        else:
            result["structures_baked"] += sum(
                1
                for structure in decision.structures
                if structure.skip_reason is None
            )
            result["structures_needing_pad"] += sum(
                1 for structure in decision.structures if structure.needs_pad
            )
            result["skipped"].extend(decision.skipped)
            # Amendment A21 parity with the write path: resources that
            # WOULD bake only their passing structures.
            for resource_path in sorted(
                decision.delta_by_resource_and_vertex
            ):
                resource_skipped = [
                    structure
                    for structure in decision.structures
                    if structure.skip_reason
                    and resource_path in structure.triangles_by_resource
                ]
                if resource_skipped:
                    result["partially_baked"].append(
                        (
                            resource_path,
                            f"{len(resource_skipped)} structure(s) would "
                            "stay at their authored y (skipped), passing "
                            "structures bake; first reason: "
                            + resource_skipped[0].skip_reason,
                        )
                    )
    return result


def rebake_dsf_objects(tile) -> dict:
    """Run Phase 2 for every airport in ``tile``'s worklist.

    Returns a counts dict (``airports_processed``, ``packs_corrected``,
    ``structures_baked``, ``structures_needing_pad``, ``vertices_offset``,
    ``objects_skipped``, ``airports_failed``) for the caller's summary
    line.  Returns immediately — before reading anything — unless
    ``DSF_OBJECT_REANCHOR`` is on (function-local config import so tests
    can drive the flag).  A missing worklist means nothing to do.

    One airport's exception is caught, counted and logged; the loop
    continues.  The function itself also never raises (the hook wraps it
    in try/except anyway — belt and braces).
    """
    from .config import DSF_OBJECT_REANCHOR

    if not DSF_OBJECT_REANCHOR:
        return {}

    counts = {key: 0 for key in _COUNT_KEYS}
    try:
        import O4_File_Names as FNAMES

        worklist_path = object_anchor_worklist_path(tile)
        if not os.path.isfile(worklist_path):
            return {}
        with open(worklist_path) as handle:
            worklist = json.load(handle)

        mesh_path = FNAMES.mesh_file(tile.build_dir, tile.lat, tile.lon)
        if not os.path.isfile(mesh_path):
            UI.vprint(
                1,
                f"  [object-anchor] mesh not found at {mesh_path}; "
                "DSF object re-anchor skipped",
            )
            return counts

        # O3 ordering guard (spec section 7): the Phase 2 y-bake samples the
        # BUILT mesh, which is derived from the ``.alt`` raster written in
        # step 1 (with the airport elevation insets baked in).  If the mesh
        # on disk is OLDER than the newest ``.alt`` for this tile, the mesh
        # predates the current elevation state and sampling it would seat
        # every object against a stale surface -- silently, and wrongly.
        # Fail LOUD and skip rather than bake against a stale mesh.
        if not _mesh_is_newer_than_alt(tile, mesh_path):
            UI.vprint(
                0,
                "  [object-anchor] STALE MESH: "
                f"{os.path.basename(mesh_path)} is older than the tile's "
                ".alt (elevation raster) -- the mesh predates the current "
                "insets/grading.  DSF object re-anchor SKIPPED to avoid "
                "seating objects against a stale surface; rebuild the mesh "
                "(step 2) after the .alt (step 1) and re-run.",
            )
            return counts

        corrected_pack_roots: set[str] = set()
        foot_pad_airports: list[dict] = []
        for airport in worklist.get("airports", []):
            icao = airport.get("icao", "?")
            try:
                dsf_path = airport["dsf_path"]
                pack_root = airport["pack_root"]
                xplane_root = airport.get("xplane_root") or worklist.get(
                    "xplane_root"
                )
                if not os.path.isfile(dsf_path):
                    raise OSError(f"DSF not found: {dsf_path}")
                recorded_mtime = airport.get("dsf_mtime")
                if (
                    recorded_mtime is not None
                    and abs(os.path.getmtime(dsf_path) - recorded_mtime)
                    > 1e-6
                ):
                    # Discovery is authoritative; the worklist is
                    # identification only (amendment A5).  Note it and
                    # proceed against the CURRENT DSF.
                    UI.vprint(
                        2,
                        f"  [object-anchor] {icao}: DSF changed since the "
                        "worklist was written; discovery proceeds against "
                        "the current DSF",
                    )
                # Ruling R4: feature-A/B-consumed objects (terrain
                # adapted TO them) never receive the Phase 2 y-bake.
                # Empty set — read nothing — while the object-terrain
                # gates are off.
                from .object_terrain_assembly import exclusion_set_for_dsf

                excluded_resources = exclusion_set_for_dsf(
                    dsf_path, xplane_root, pack_root=pack_root
                )
                airport_result = discover_and_rebake_airport(
                    dsf_path,
                    mesh_path,
                    pack_root,
                    xplane_root,
                    excluded_resources=excluded_resources,
                )
            except Exception as exception:
                # Per-airport containment: one broken airport never
                # blocks the next (pure-reporter philosophy,
                # verification.verify_and_log).
                counts["airports_failed"] += 1
                UI.vprint(
                    1,
                    f"  [object-anchor] {icao}: re-anchor failed "
                    f"({exception}); continuing with the next airport",
                )
                continue

            counts["airports_processed"] += 1
            counts["structures_baked"] += airport_result["structures_baked"]
            counts["structures_needing_pad"] += airport_result[
                "structures_needing_pad"
            ]
            counts["vertices_offset"] += airport_result["vertices_offset"]
            counts["objects_skipped"] += len(airport_result["skipped"])
            counts["objects_reverted"] += len(
                airport_result["objects_reverted"]
            )
            counts["objects_partially_baked"] += len(
                airport_result["partially_baked"]
            )
            for resource_path, summary in airport_result["partially_baked"]:
                UI.vprint(
                    2,
                    f"  [object-anchor] {icao}: partially baked "
                    f"{resource_path} — {summary}",
                )
            for resource_path in airport_result["objects_reverted"]:
                UI.vprint(
                    1,
                    f"  [object-anchor] {icao}: reverted {resource_path} "
                    "to its authored placement (excluded from the current "
                    "decision; stale live bake removed)",
                )
            for resource_path in airport_result["reversions_missing_backup"]:
                UI.vprint(
                    0,
                    f"  [object-anchor] {icao}: {resource_path} is "
                    "excluded and still carries a stale bake but its "
                    ".anchor_bak is missing — left untouched, NOT reverted",
                )
            counts["foot_pad_requests"] += len(
                airport_result["foot_pad_requests"]
            )
            if airport_result["foot_pad_requests"]:
                from . import object_footprints
                from .config import DSF_OBJECT_FOOT_PAD_MARGIN_M

                foot_pad_airports.append(
                    {
                        "icao": icao,
                        "pack_root": pack_root,
                        "requests": [
                            {
                                "resource_path": request.resource_path,
                                "latitude": request.latitude,
                                "longitude": request.longitude,
                                "base_y": request.base_y,
                                "residual_metres": request.residual_metres,
                                "target_ground_metres": (
                                    request.target_ground_metres
                                ),
                                "ring_lonlat": (
                                    object_footprints.foot_pad_ring(
                                        list(
                                            request.contact_points_lonlat
                                        ),
                                        DSF_OBJECT_FOOT_PAD_MARGIN_M,
                                    )
                                ),
                            }
                            for request in airport_result[
                                "foot_pad_requests"
                            ]
                        ],
                    }
                )
                UI.vprint(
                    1,
                    f"  [object-anchor] {icao}: "
                    f"{len(airport_result['foot_pad_requests'])} foot "
                    "pad request(s) — a rigid offset could not seat "
                    "every foot; recorded in "
                    + OBJECT_FOOT_PAD_SIDECAR_FILENAME,
                )

            for resource_path, reason in airport_result["skipped"]:
                UI.vprint(
                    2,
                    f"  [object-anchor] {icao}: skipped {resource_path}: "
                    f"{reason}",
                )
            had_work = (
                airport_result["objects_written"]
                or airport_result["skipped"]
                or airport_result["decisions"]
            )
            if had_work:
                UI.vprint(
                    1,
                    f"  [object-anchor] {icao}: "
                    f"{airport_result['structures_baked']} structure(s) "
                    f"re-baked across "
                    f"{len(airport_result['objects_written'])} object "
                    f"file(s), {airport_result['vertices_offset']} "
                    f"vertices offset"
                    + (
                        f", {airport_result['structures_needing_pad']} "
                        "structure(s) flagged as needing a pad"
                        if airport_result["structures_needing_pad"]
                        else ""
                    )
                    + (
                        f", {len(airport_result['skipped'])} skipped"
                        if airport_result["skipped"]
                        else ""
                    )
                    + (
                        f", {len(airport_result['partially_baked'])} "
                        "partially baked"
                        if airport_result["partially_baked"]
                        else ""
                    ),
                )
            structures_left_at_authored = sum(
                1
                for _pool, decision in airport_result["decisions"]
                for structure in decision.structures
                if structure.skip_reason
                and object_anchor.GROUND_SPAN_SKIP_REASON_PHRASE
                in structure.skip_reason
            )
            if structures_left_at_authored:
                UI.vprint(
                    1,
                    f"  [object-anchor] {icao}: "
                    f"{structures_left_at_authored} structure(s) left at "
                    "authored elevations (ground span > limit)",
                )
            if (
                (
                    airport_result["objects_written"]
                    or airport_result["objects_reverted"]
                )
                and pack_root not in corrected_pack_roots
            ):
                corrected_pack_roots.add(pack_root)
                counts["packs_corrected"] += 1
                UI.vprint(
                    1,
                    "  [object-anchor] restart X-Plane (objects are "
                    f"cached): {os.path.basename(pack_root) or pack_root}",
                )

        # Refresh the foot-pad sidecar every run: write it when any
        # request was raised, remove a stale one when none remains.
        sidecar_path = os.path.join(
            os.path.dirname(worklist_path),
            OBJECT_FOOT_PAD_SIDECAR_FILENAME,
        )
        if foot_pad_airports:
            with open(sidecar_path, "w") as handle:
                json.dump(
                    {
                        "version": OBJECT_FOOT_PAD_SIDECAR_VERSION,
                        "tile": worklist.get("tile"),
                        "airports": foot_pad_airports,
                    },
                    handle,
                    indent=1,
                )
        elif os.path.isfile(sidecar_path):
            os.remove(sidecar_path)
    except Exception as exception:
        # Belt and braces: a reporter must never fail the tile.
        try:
            UI.vprint(
                1,
                "  [object-anchor] post-mesh DSF object re-anchor failed: "
                f"{exception}",
            )
        except Exception:
            pass
    return counts
