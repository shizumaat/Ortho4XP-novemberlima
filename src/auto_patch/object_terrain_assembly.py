"""Assemble a bridge/tunnel :class:`ClassificationResult` for one airport.

Workstream W-B of ``docs/object_terrain_features_spec.md`` — the *impure*
front end to the pure classifier (:mod:`object_terrain_features`).  Where
the classifier takes placements + geometry and returns records, this
module does the file work the classifier deliberately refuses: locate the
airport pack's overlay DSF, run the (cached) DSFTool text dump, read the
placements (``include_object_msl=True`` so KBNA's absolute deck fixtures
survive), load the referenced OBJ8 geometry, hand the pipeline's own
draped pavement in as the contract-coverage evidence, and — for the
depressed-road corridor re-source (spec section 3.2 step 3) — discover the
sibling roads pack's DSF road network on the same tile.

Everything here is gated behind ``config.OBJECT_BRIDGE_TERRAIN`` (default
off).  With the gate off :func:`attach_bridge_classification` is a no-op
that touches nothing and attaches nothing, so every downstream reader in
``bridges.py`` sees ``getattr(layout, ATTRIBUTE, None) is None`` and takes
its unchanged legacy path — the build is byte-identical to today.

The two artefacts cached on the layout when the gate is on:

* ``layout._object_bridge_classification`` — the
  :class:`object_terrain_features.ClassificationResult` (may itself carry
  empty ``bridges``/``tunnels`` when the pack has no such objects — KDFW —
  which is the designed "feature B does not fire, legacy handles it" path).
* ``layout._object_bridge_road_networks`` — a list of
  :class:`dsf_road_network.RoadNetwork` from every Custom Scenery pack that
  carries a vector road network on the airport's tile (the KBNA "US-KBNA
  Nashville Roads" pack is the exemplar; the bridged roads exist ONLY
  there).

The reader/loader APIs consumed here are all already merged and tested
(W-R1/W-R2/W-R3); this module wires them together and adds no new parsing.

ACCEPTANCE-LOOP RULE (round 6, measured the hard way): iteration builds
must run with ``O4_DSF_OBJECT_REANCHOR=0``.  The Phase 2 y-bake mutates
pack OBJ8 files; across repeated builds an unexcluded sibling part
(KBNA Taxiway-L p3) drifted until it qualified into the classifier pool
and moved the deck box.  The bake belongs after a FINAL mesh only.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle

import O4_UI_Utils as UI

from . import dsf_reader
from . import obj8_reader
from . import dsf_road_network
from . import object_terrain_features
from . import config


# The layout attribute names the bridge emitters read.  Kept as module
# constants so the producer here and the consumers in ``bridges.py`` can
# never drift on a spelling.
CLASSIFICATION_ATTRIBUTE = "_object_bridge_classification"
ROAD_NETWORKS_ATTRIBUTE = "_object_bridge_road_networks"
ROUTE_LINES_ATTRIBUTE = "_object_bridge_route_lines"

# A resource placed more often than this is scenery clutter (trees, lamp
# posts, fence posts) and is never a tunnel/bridge structure — skip it so
# the classifier is not fed thousands of identical footprints.  Mirrors
# Phase 1's posture of not pooling mass-placed resources; a real
# tunnel/bridge object is placed a handful of times (EGLL: one placement
# per tunnel; KBNA taxiway-L: six part objects).
MAXIMUM_PLACEMENTS_PER_RESOURCE = 50

# Ruling R4 exclusion breadth (round 6): every resource placed at the
# SAME ANCHOR as a consumed structure belongs to that structure's part
# family and must be excluded from the Phase 2 y-bake — the classifier's
# ``object_resources`` lists only the parts that carried usable deck
# geometry (KBNA taxiway-L pools p1/p4/p5/p6; p2/p3 have no qualifying
# faces yet sit on the SAME anchor and got re-baked build after build
# until their drifted geometry qualified into the pool and moved the
# deck box).  MEASURED at KBNA: all six Taxiway-L parts (and every
# Crossing / Murfreesboro part set) share ONE anchor to the millimetre
# (max intra-family spread 0.00 m), while the nearest FOREIGN placement
# (GPU_1.obj) sits 1.5 m from a bridge anchor — 0.5 m separates the
# families from neighbours with a 3x margin both ways (the 2 m
# structure-grouping epsilon would wrongly swallow the GPU).
ANCHOR_FAMILY_RADIUS_M = 0.5


def _expand_exclusions_to_anchor_families(result, placements, pack_root):
    """Append to ``result.exclusions`` every resource with a placement
    anchored within :data:`ANCHOR_FAMILY_RADIUS_M` of a consumed
    structure's placements (the whole part family: p1..p6, shell+deck
    pairs).  Returns the sorted list of newly excluded resource paths."""
    consumed = {resource for _root, resource in result.exclusions}
    if not consumed:
        return []
    family_anchors = [
        (placement.longitude, placement.latitude)
        for placement in placements
        if placement.resource_path in consumed
    ]
    if not family_anchors:
        return []
    added = set()
    for placement in placements:
        if placement.resource_path in consumed:
            continue
        cosine = math.cos(math.radians(placement.latitude))
        for anchor_longitude, anchor_latitude in family_anchors:
            distance = math.hypot(
                (placement.latitude - anchor_latitude) * 111320.0,
                (placement.longitude - anchor_longitude)
                * 111320.0 * cosine,
            )
            if distance <= ANCHOR_FAMILY_RADIUS_M:
                added.add(placement.resource_path)
                break
    for resource_path in sorted(added):
        result.exclusions.append((pack_root, resource_path))
    return sorted(added)


def _tile_dsf_path(earth_nav_data_dir: str, tile_lat: int, tile_lon: int) -> str:
    """The ``<grp>/<tile>.dsf`` path under an ``Earth nav data`` dir for a
    1-degree tile, using X-Plane's ``+NN-MMM`` naming (the exact scheme
    :func:`dsf_reader.find_associated_dsf` builds internally)."""
    group_lat = (tile_lat // 10) * 10
    group_lon = (tile_lon // 10) * 10

    def _format(value: int, pad: int) -> str:
        sign = "+" if value >= 0 else "-"
        return f"{sign}{abs(value):0{pad}d}"

    group = f"{_format(group_lat, 2)}{_format(group_lon, 3)}"
    tile = f"{_format(tile_lat, 2)}{_format(tile_lon, 3)}"
    return os.path.join(earth_nav_data_dir, group, tile + ".dsf")


def _pavement_polygons_longitude_latitude(layout) -> list | None:
    """Project the pipeline's own draped source pavement into
    ``(longitude, latitude)`` rings for the classifier's contract-coverage
    test.  ``None`` when the layout carries no pavement union yet — the
    classifier then falls back to the crest-height contract and the caller
    records that the height fallback governed."""
    union = getattr(layout, "source_pavement_union", None)
    if union is None or getattr(union, "is_empty", True):
        return None
    parts = (
        list(union.geoms)
        if union.geom_type == "MultiPolygon"
        else [union]
    )
    rings: list = []
    from shapely.geometry import Polygon

    for part in parts:
        if part.geom_type != "Polygon" or part.is_empty:
            continue
        ring_longitude_latitude = []
        for x, y in part.exterior.coords:
            latitude, longitude = layout.m_to_ll(x, y)
            ring_longitude_latitude.append((longitude, latitude))
        if len(ring_longitude_latitude) >= 3:
            rings.append(Polygon(ring_longitude_latitude))
    return rings or None


def _load_object_geometry_by_resource(
    placements, pack_root, xplane_root
):
    """Resolve and load OBJ8 geometry for every terrain-relative placement
    resource, skipping light-only objects (no solid geometry) and
    mass-placed clutter (more than :data:`MAXIMUM_PLACEMENTS_PER_RESOURCE`
    placements) — the same two skips Phase 1 applies."""
    placement_count_by_resource: dict[str, int] = {}
    for placement in placements:
        placement_count_by_resource[placement.resource_path] = (
            placement_count_by_resource.get(placement.resource_path, 0) + 1
        )
    geometry_by_resource: dict = {}
    for resource_path in sorted(
        {placement.resource_path for placement in placements}
    ):
        if (
            placement_count_by_resource[resource_path]
            > MAXIMUM_PLACEMENTS_PER_RESOURCE
        ):
            continue
        physical_path = obj8_reader.resolve_object_resource(
            resource_path, pack_root, xplane_root
        )
        if physical_path is None:
            continue
        geometry = dsf_reader._load_object_geometry(physical_path)
        if geometry is None or not geometry.has_solid_geometry:
            continue
        geometry_by_resource[resource_path] = geometry
    return geometry_by_resource


# Bump when ``dsf_road_network.parse_dsf_road_networks`` changes its
# record shapes — invalidates every road-network sidecar.
_ROAD_NETWORK_CACHE_VERSION = 1

# Sidecar file name prefix; the full name carries the DSF stem
# (``o4_dsf_road_network_<dsf-stem>.cache``).  Lives under
# ``dsf_reader.airport_mod_cache_dir`` — NOT in the roads pack (user
# ruling 2026-07-15, no Ortho4XP clutter in scenery packs).
_ROAD_NETWORK_SIDECAR_PREFIX = "o4_dsf_road_network"

# Pre-ruling in-pack sidecar name, removed on sight (legacy cleanup).
_ROAD_NETWORK_LEGACY_SIDECAR_NAME = "o4_dsf_road_network.cache"


def _road_network_sidecar(dsf_path: str) -> tuple[str | None, str | None]:
    """Sidecar path + fingerprint for one sibling DSF's road network.

    ``parse_dsf_road_networks`` output is a pure function of the DSF text
    dump, which is itself a pure function of the DSF file, so the
    fingerprint is just a code version salt plus the DSF's own size and
    mtime — any layout edit to a roads pack necessarily rewrites the DSF.
    The sidecar lives under ``dsf_reader.airport_mod_cache_dir`` for that
    roads pack (user ruling 2026-07-15 — Ortho4XP-only caches stay out of
    scenery pack folders); a pre-ruling in-pack sidecar found at the pack
    root is removed here.  Returns ``(None, None)`` when no pack root
    resolves or the DSF cannot be stat-ed."""
    pack_root = dsf_reader._pack_root_for_dsf(dsf_path)
    cache_directory = dsf_reader.airport_mod_cache_dir(pack_root)
    if cache_directory is None:
        return None, None
    # Legacy cleanup: exactly the old filename at the pack root, every
    # OSError swallowed.
    try:
        os.remove(os.path.join(pack_root,
                               _ROAD_NETWORK_LEGACY_SIDECAR_NAME))
    except OSError:
        pass
    try:
        dsf_stat = os.stat(dsf_path)
    except OSError:
        return None, None
    fingerprint = (
        f"{_ROAD_NETWORK_CACHE_VERSION}:{os.path.basename(dsf_path)}"
        f":{dsf_stat.st_size}:{dsf_stat.st_mtime}"
    )
    dsf_stem = os.path.splitext(os.path.basename(dsf_path))[0]
    return (
        os.path.join(
            cache_directory,
            f"{_ROAD_NETWORK_SIDECAR_PREFIX}_{dsf_stem}.cache",
        ),
        fingerprint,
    )


def _discover_sibling_road_networks(
    xplane_root: str, tile_lat: int, tile_lon: int
) -> list:
    """Every Custom Scenery pack's vector road network that covers the
    airport's tile (spec section 3.2 step 3 — the KBNA bridged roads live
    only in the sibling "US-KBNA Nashville Roads" pack).

    Scans the ``scenery_packs.ini`` pack order, builds each pack's tile
    DSF path, and — for those that exist and dump to text — parses the
    road network, keeping any that carries at least one segment.  Base and
    global scenery are skipped (never a bespoke roads pack)."""
    if not xplane_root:
        return []
    try:
        from .agp_reader import _scenery_pack_order
    except ImportError:
        return []
    custom_scenery = os.path.join(xplane_root, "Custom Scenery")
    networks: list = []
    for pack_name in _scenery_pack_order(xplane_root):
        earth_nav_data = os.path.join(
            custom_scenery, pack_name, "Earth nav data"
        )
        if not os.path.isdir(earth_nav_data):
            continue
        dsf_path = _tile_dsf_path(earth_nav_data, tile_lat, tile_lon)
        if not os.path.isfile(dsf_path):
            continue

        # ── Per-DSF road-network sidecar cache (default ON) ──  A hit
        # skips BOTH the DSFTool text dump (``_load_dsf_text``) and the
        # parse, the two costs the profile attributes to this discovery.
        # ``O4_DSF_ROAD_NETWORK_CACHE=0`` disables read and write.
        cache_enabled = (
            os.environ.get("O4_DSF_ROAD_NETWORK_CACHE", "1") == "1"
        )
        sidecar_path = None
        fingerprint = None
        if cache_enabled:
            import pickle
            sidecar_path, fingerprint = _road_network_sidecar(dsf_path)
            if (
                sidecar_path
                and fingerprint
                and os.path.isfile(sidecar_path)
            ):
                try:
                    with open(sidecar_path, "rb") as sidecar_file:
                        payload = pickle.load(sidecar_file)
                    if payload.get("fingerprint") == fingerprint:
                        network = payload["result"]
                        if network.segments:
                            networks.append(network)
                        continue
                except Exception:
                    pass

        lines = dsf_reader._load_dsf_text(dsf_path)
        if not lines:
            continue
        network = dsf_road_network.parse_dsf_road_networks(lines)

        if sidecar_path is not None and fingerprint is not None:
            import pickle
            try:
                os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
                with open(sidecar_path, "wb") as sidecar_file:
                    pickle.dump(
                        {"fingerprint": fingerprint, "result": network},
                        sidecar_file,
                    )
            except Exception:
                pass

        if network.segments:
            networks.append(network)
    return networks


# Bump when classifier logic or record shapes change — invalidates every
# pack-sidecar classification cache (see attach_bridge_classification).
# Version 3: classifier performance round 2026-07-10 (evidence
# pre-screen, composed placement transform, bulk footprint unions) —
# results are equivalent within float tolerance but must be rebuilt on
# the new code path.
_CLASSIFICATION_CACHE_VERSION = 4  # 4: portal-face records (EGGW class)

# Sidecar file name prefix; the full name carries the DSF stem
# (``o4_object_terrain_classification_<dsf-stem>.cache``).  Lives under
# ``dsf_reader.airport_mod_cache_dir`` — NOT in the airport pack (user
# ruling 2026-07-15: Ortho4XP-only caches stay out of scenery pack
# folders; the ``.anchor_bak`` object BACKUPS explicitly stay in-pack
# next to the files they back up).
_CLASSIFICATION_SIDECAR_PREFIX = "o4_object_terrain_classification"

# Pre-ruling in-pack sidecar name, removed on sight (legacy cleanup).
_CLASSIFICATION_LEGACY_SIDECAR_NAME = (
    "o4_object_terrain_classification.cache"
)


def _classification_sidecar(dsf_path, pack_root, pavement_polygons,
                            apt_dat_path=None):
    """Sidecar path + input fingerprint for the pack classification
    cache.  The fingerprint covers everything the classification reads:

    * the overlay DSF (path, size, mtime) — any airport layout change
      necessarily rewrites it (user ruling 2026-07-10);
    * the airport's ``apt.dat`` (size, mtime) when known — layout edits
      usually rewrite it too, and the pavement evidence derives from it;
    * every ``.obj`` under the pack root (relative path, size, mtime) —
      needed BESIDE the DSF check because object-geometry edits (our own
      Phase 2 y-bake rewrites included) change no DSF byte;
      ``.anchor_bak`` backups are not ``.obj`` files and stay out of it;
    * the pavement-coverage evidence (well-known-binary hash of the
      rings — contract selection depends on it);
    * :data:`_CLASSIFICATION_CACHE_VERSION`.

    O3 verdict (spec section 7, verified 2026-07-11): the fingerprint
    DELIBERATELY does NOT include the built mesh or the ``.alt`` elevation
    raster, and that is CORRECT.  The classifier's output is a geometric
    bridge/tunnel/draped classification (plus the DSF-fixture absolute deck
    MSL) — it samples no terrain elevation (``object_terrain_features`` does
    "no mesh sampling"), so a rebuilt mesh with unchanged pack files cannot
    change the classification and reusing it is sound.  The elevation-
    DEPENDENT artefact is the Phase 2 object y-bake, and that is recomputed
    against the current mesh on EVERY mesh build (``post_mesh`` reruns
    ``structure_deltas`` fresh and ``object_rebake.apply`` re-reads geometry
    from the ``.anchor_bak`` backup; the reanchor provenance sidecar is a
    diagnostic, not a rebuild-skip gate), so no stale delta is ever reused
    across a mesh change — the ``O4_AUTO_PATCH_REBUILD=1`` gotcha class does
    not apply here.

    The sidecar lives under ``dsf_reader.airport_mod_cache_dir`` (user
    ruling 2026-07-15 — Ortho4XP-only caches stay out of scenery pack
    folders); a pre-ruling in-pack sidecar found at the pack root is
    removed here so the pack stays clean.

    Returns ``(None, None)`` when no pack root is known (nowhere to key
    a sidecar on) or fingerprinting fails."""
    cache_directory = dsf_reader.airport_mod_cache_dir(pack_root)
    if cache_directory is None:
        return None, None
    # Legacy cleanup: exactly the old filename at the pack root, every
    # OSError swallowed.
    try:
        os.remove(os.path.join(pack_root,
                               _CLASSIFICATION_LEGACY_SIDECAR_NAME))
    except OSError:
        pass
    import hashlib
    digest = hashlib.sha1()
    try:
        digest.update(str(_CLASSIFICATION_CACHE_VERSION).encode())
        dsf_stat = os.stat(dsf_path)
        digest.update(
            f"{os.path.basename(dsf_path)}:{dsf_stat.st_size}"
            f":{dsf_stat.st_mtime}".encode()
        )
        if apt_dat_path:
            try:
                apt_dat_stat = os.stat(apt_dat_path)
                digest.update(
                    f"apt:{apt_dat_stat.st_size}"
                    f":{apt_dat_stat.st_mtime}".encode()
                )
            except OSError:
                digest.update(b"apt:unreadable")
        object_entries = []
        for directory, _subdirectories, file_names in os.walk(pack_root):
            for file_name in file_names:
                if not file_name.lower().endswith(".obj"):
                    continue
                full_path = os.path.join(directory, file_name)
                try:
                    file_stat = os.stat(full_path)
                except OSError:
                    continue
                object_entries.append(
                    f"{os.path.relpath(full_path, pack_root)}"
                    f":{file_stat.st_size}:{file_stat.st_mtime}"
                )
        for entry in sorted(object_entries):
            digest.update(entry.encode())
        if pavement_polygons:
            for polygon in pavement_polygons:
                try:
                    digest.update(polygon.wkb)
                except Exception:
                    digest.update(b"?")
        else:
            digest.update(b"no-pavement-evidence")
    except OSError:
        return None, None
    dsf_stem = os.path.splitext(os.path.basename(dsf_path))[0]
    return (
        os.path.join(
            cache_directory,
            f"{_CLASSIFICATION_SIDECAR_PREFIX}_{dsf_stem}.cache",
        ),
        digest.hexdigest(),
    )


def attach_bridge_classification(layout, xplane_root: str):
    """Classify the airport pack's bridge/tunnel objects and cache the
    result (plus sibling road networks) on ``layout``.

    Gated by ``config.OBJECT_BRIDGE_TERRAIN`` OR
    ``config.OBJECT_TUNNEL_TERRAIN`` (feature A shares this one classifier
    pass — spec section 3.1): with BOTH gates OFF this is a complete no-op —
    nothing is read, nothing is attached, and every emitter takes its
    unchanged legacy path (flag-off byte identity).

    Returns the :class:`object_terrain_features.ClassificationResult` (also
    cached on ``layout``) or ``None`` when both gates are off or no overlay
    DSF could be located.

    Idempotent: stage 2 attaches PRE-solve (the pin writers need the
    records before the seam hook), and the post-solve emitter hook calls
    this again as a fallback — a result already cached on the layout is
    returned as-is, never recomputed."""
    if not (config.OBJECT_BRIDGE_TERRAIN or config.OBJECT_TUNNEL_TERRAIN):
        return None
    cached = getattr(layout, CLASSIFICATION_ATTRIBUTE, None)
    if cached is not None:
        return cached
    apt_dat_path = getattr(layout, "apt_dat_path", None)
    anchor = getattr(layout, "anchor", None)
    if not apt_dat_path or anchor is None:
        return None
    anchor_latitude, anchor_longitude = anchor[0], anchor[1]

    dsf_path = dsf_reader.find_associated_dsf(
        apt_dat_path, anchor_latitude, anchor_longitude
    )
    if dsf_path is None or not os.path.isfile(dsf_path):
        UI.vprint(
            1,
            "   [object-bridge] no overlay DSF for "
            f"{getattr(layout, 'icao', '?')} — feature B inactive",
        )
        return None

    # ── Pack-sidecar classification cache (user directive 2026-07-10,
    # default ON) ──  The read → load → classify chain is recomputed
    # byte-identically on every build of an unchanged pack.  The
    # FINISHED result (R4 family expansion included) is pickled as a
    # sidecar under the data root's ``Airport_mod_cache/<pack>/`` (user
    # ruling 2026-07-15: never inside the pack) guarded by a fingerprint
    # of everything the classification reads: the overlay DSF, every
    # ``.obj`` in the pack (a Phase 2 y-bake rewrite invalidates
    # automatically), the pavement-coverage evidence, and a code
    # version salt.  ``O4_OBJECT_CLASSIFICATION_CACHE=0`` disables.
    pavement_polygons = _pavement_polygons_longitude_latitude(layout)
    pack_root_early = dsf_reader._pack_root_for_dsf(dsf_path)
    sidecar_path = None
    fingerprint = None
    if os.environ.get("O4_OBJECT_CLASSIFICATION_CACHE", "1") == "1":
        import pickle
        sidecar_path, fingerprint = _classification_sidecar(
            dsf_path, pack_root_early, pavement_polygons,
            apt_dat_path=apt_dat_path,
        )
        if sidecar_path and fingerprint and os.path.isfile(sidecar_path):
            try:
                with open(sidecar_path, "rb") as sidecar_file:
                    payload = pickle.load(sidecar_file)
                if payload.get("fingerprint") == fingerprint:
                    UI.vprint(
                        1,
                        "   [object-bridge] classification read from the "
                        "pack sidecar cache (fingerprint match)",
                    )
                    return _attach_classification_tail(
                        layout, payload["result"], xplane_root,
                        anchor_latitude, anchor_longitude,
                    )
                UI.vprint(
                    1,
                    "   [object-bridge] pack sidecar cache STALE "
                    "(pack edited since it was written) — reclassifying",
                )
            except Exception:
                pass

    lines = dsf_reader._load_dsf_text(dsf_path)
    if not lines:
        UI.vprint(
            1,
            "   [object-bridge] DSF text unavailable (missing file or "
            "DSFTool) — feature B inactive",
        )
        return None

    all_placements = obj8_reader.read_dsf_object_placements(
        lines,
        accept_resource=lambda resource: resource.lower().endswith(".obj"),
        include_object_msl=True,
    )
    mean_sea_level_placements = [
        placement
        for placement in all_placements
        if placement.placement_kind == "OBJECT_MSL"
    ]
    terrain_placements = [
        placement
        for placement in all_placements
        if placement.placement_kind != "OBJECT_MSL"
    ]
    if not terrain_placements:
        return None

    pack_root = dsf_reader._pack_root_for_dsf(dsf_path)
    geometry_by_resource = _load_object_geometry_by_resource(
        terrain_placements, pack_root, xplane_root
    )
    if not geometry_by_resource:
        return None

    # (``pavement_polygons`` computed above, ahead of the sidecar
    # fingerprint — contract selection depends on it.)
    if pavement_polygons is None:
        UI.vprint(
            2,
            "   [object-bridge] no draped pavement available at the "
            "classifier hook — contract falls back to deck-crest height",
        )

    result = object_terrain_features.classify_object_terrain_features(
        terrain_placements,
        geometry_by_resource,
        pavement_polygons_longitude_latitude=pavement_polygons,
        mean_sea_level_placements=mean_sea_level_placements,
        pack_root=pack_root or "",
    )

    # Ruling R4 breadth: pull the whole anchor family of every consumed
    # structure onto the exclusion list (sibling parts the classifier's
    # records do not carry — see ANCHOR_FAMILY_RADIUS_M).
    family_added = _expand_exclusions_to_anchor_families(
        result, terrain_placements, pack_root or ""
    )
    if family_added:
        UI.vprint(
            1,
            f"   [object-bridge] R4 exclusions widened to {len(family_added)} "
            f"anchor-family sibling resource(s): "
            f"{[r.split('/')[-1] for r in family_added]}",
        )

    if sidecar_path is not None and fingerprint is not None:
        import pickle
        try:
            os.makedirs(os.path.dirname(sidecar_path), exist_ok=True)
            with open(sidecar_path, "wb") as sidecar_file:
                pickle.dump(
                    {"fingerprint": fingerprint, "result": result},
                    sidecar_file,
                )
            UI.vprint(
                1,
                "   [object-bridge] classification written to the pack "
                f"sidecar cache ({os.path.basename(sidecar_path)})",
            )
        except Exception:
            pass

    return _attach_classification_tail(
        layout, result, xplane_root, anchor_latitude, anchor_longitude
    )


def _attach_classification_tail(layout, result, xplane_root,
                                anchor_latitude, anchor_longitude):
    """Common tail of :func:`attach_bridge_classification` for both the
    fresh-classify and lab-cache paths: cache on the layout, discover
    sibling road networks and route lines, log the summary."""
    setattr(layout, CLASSIFICATION_ATTRIBUTE, result)

    tile_lat = int(math.floor(anchor_latitude))
    tile_lon = int(math.floor(anchor_longitude))
    road_networks = _discover_sibling_road_networks(
        xplane_root, tile_lat, tile_lon
    )
    setattr(layout, ROAD_NETWORKS_ATTRIBUTE, road_networks)
    setattr(
        layout, ROUTE_LINES_ATTRIBUTE,
        _raw_route_lines_layout_meters(layout),
    )

    _log_classification_summary(
        getattr(layout, "icao", "?"), result, road_networks
    )
    return result


def _raw_route_lines_layout_meters(layout) -> list:
    """The RAW apt.dat routing polylines (row-1202 taxi edges + row-1206
    truck edges) as layout-meter LineStrings — the road-carried
    discriminator's primary evidence (stage 2b iteration 4).

    The QUALIFIED centerline set (``layout.apt_taxi_centerlines``) is the
    wrong evidence: at KBNA the Murfreesboro truck runs are disqualified
    before reaching it (0 centerlines within the reach band of either
    deck), while the raw 1206 rows genuinely cross — measured: 3 and 5
    truck edges in the two decks' bands, 3 taxi edges at taxiway-L, zero
    of anything at the Crossing_Bridge road overpass.  Empty list when
    the apt.dat carries no routing rows or cannot be read."""
    from shapely.geometry import LineString

    apt_dat_path = getattr(layout, "apt_dat_path", None)
    icao = getattr(layout, "icao", None)
    if not apt_dat_path or not icao:
        return []
    try:
        from .apt_dat_reader import load_airport
        airport = load_airport(apt_dat_path, icao)
    except (OSError, ValueError):
        return []
    if airport is None:
        return []
    nodes = airport.taxi_nodes  # dict id -> TaxiNode
    lines: list = []
    for edge in list(airport.taxi_edges) + list(airport.truck_edges):
        node_a = nodes.get(edge.node_from)
        node_b = nodes.get(edge.node_to)
        if node_a is None or node_b is None:
            continue
        try:
            lines.append(LineString([
                layout.ll_to_m(node_a.lat, node_a.lon),
                layout.ll_to_m(node_b.lat, node_b.lon),
            ]))
        except (ValueError, TypeError):
            continue
    return lines


# Bump together with classifier-behavior changes that
# ``_CLASSIFICATION_CACHE_VERSION`` alone would not capture for the
# post-mesh exclusion path (both versions salt the exclusion cache key).
_EXCLUSION_CACHE_VERSION = 1


def _cached_exclusion_pairs(
    pack_root: str,
    terrain_placements,
    mean_sea_level_placements,
    geometry_by_resource,
    compute,
) -> set[tuple[str, str]]:
    """Content-hash sidecar cache for the ruling-R4 exclusion set.

    The set is a pure function of the DSF placements, the loaded OBJ8
    geometry and the classifier version — never the mesh — yet it was
    recomputed on every mesh build (profiled 2026-07-15: 46 s of the
    KBNA rebake, the classifier being the bulk).  Keyed by CONTENT
    (placements + a digest of each loaded geometry), not file mtimes:
    the Phase 2 y-bake rewrites pack ``.obj`` files each run, churning
    mtimes while the classification inputs stay put.  Stored as JSON
    under ``Airport_mod_cache/<pack>/``.  ``O4_OBJECT_EXCLUSION_CACHE=0``
    disables; any read problem silently recomputes.
    """
    cache_directory = dsf_reader.airport_mod_cache_dir(pack_root)
    if (
        cache_directory is None
        or os.environ.get("O4_OBJECT_EXCLUSION_CACHE", "1") != "1"
    ):
        return compute()

    digest = hashlib.sha1()
    digest.update(
        repr(
            (
                _EXCLUSION_CACHE_VERSION,
                _CLASSIFICATION_CACHE_VERSION,
                pack_root,
            )
        ).encode()
    )
    for placement in terrain_placements:
        digest.update(repr(placement).encode())
    digest.update(b"|mean-sea-level|")
    for placement in mean_sea_level_placements:
        digest.update(repr(placement).encode())
    for resource_path in sorted(geometry_by_resource):
        digest.update(resource_path.encode())
        digest.update(
            hashlib.sha1(
                pickle.dumps(geometry_by_resource[resource_path])
            ).digest()
        )
    cache_path = os.path.join(
        cache_directory,
        "o4_object_exclusions_%s.cache" % digest.hexdigest()[:16],
    )

    if os.path.isfile(cache_path):
        try:
            with open(cache_path) as handle:
                payload = json.load(handle)
            if payload.get("version") == _EXCLUSION_CACHE_VERSION:
                return {
                    (pair[0], pair[1]) for pair in payload["exclusions"]
                }
        except Exception:
            pass  # corrupt/unreadable — recompute below

    exclusions = compute()
    try:
        os.makedirs(cache_directory, exist_ok=True)
        temporary_path = cache_path + ".tmp"
        with open(temporary_path, "w") as handle:
            json.dump(
                {
                    "version": _EXCLUSION_CACHE_VERSION,
                    "exclusions": sorted(exclusions),
                },
                handle,
            )
        os.replace(temporary_path, cache_path)
    except OSError:
        pass  # best effort — the set is already computed
    return exclusions


def exclusion_set_for_dsf(
    dsf_path: str,
    xplane_root: str | None,
    pack_root: str | None = None,
) -> set[tuple[str, str]]:
    """The ruling-R4 exclusion set for one overlay DSF: every
    ``(pack_root, resource_path)`` pair whose terrain is carved or seated
    to match the object (a structure consumed by terrain feature A or B),
    for :func:`post_mesh.discover_and_rebake_airport` to drop from the
    Phase 2 y-bake — terrain-to-object and object-to-terrain corrections
    must never stack.

    Gate-checked: with BOTH ``O4_OBJECT_BRIDGE_TERRAIN`` and
    ``O4_OBJECT_TUNNEL_TERRAIN`` off this returns an empty set having read
    NOTHING (Phase 2 behaviour unchanged).  With either gate on, it reruns
    the same cached read→load→classify chain as
    :func:`attach_bridge_classification` — deterministic over the same
    DSF, and the pipeline-time layout is gone by post-mesh time, so
    recomputing beats threading state.  Classification here passes
    ``pavement=None`` (the contract falls back to deck-crest height); the
    R4 exclusion list is contract-independent — every consumed structure
    is excluded whichever contract it classifies to — so the fallback
    cannot change the set's membership, only the (unused here) contract
    label.

    ``pack_root`` should be the same string the caller hands to
    ``discover_and_rebake_airport`` so the pair keys match exactly;
    defaults to ``dsf_reader._pack_root_for_dsf``.

    Feature A (W-T) shares this list: tunnel structures the classifier
    consumes land on the same exclusion list (spec section 3.3 step 5), so
    the gate below is an either-gate check.
    """
    if not (config.OBJECT_BRIDGE_TERRAIN or config.OBJECT_TUNNEL_TERRAIN):
        return set()
    if not dsf_path or not os.path.isfile(dsf_path):
        return set()
    lines = dsf_reader._load_dsf_text(dsf_path)
    if not lines:
        return set()
    all_placements = obj8_reader.read_dsf_object_placements(
        lines,
        accept_resource=lambda resource: resource.lower().endswith(".obj"),
        include_object_msl=True,
    )
    mean_sea_level_placements = [
        placement
        for placement in all_placements
        if placement.placement_kind == "OBJECT_MSL"
    ]
    terrain_placements = [
        placement
        for placement in all_placements
        if placement.placement_kind != "OBJECT_MSL"
    ]
    if not terrain_placements:
        return set()
    if pack_root is None:
        pack_root = dsf_reader._pack_root_for_dsf(dsf_path)
    geometry_by_resource = _load_object_geometry_by_resource(
        terrain_placements, pack_root, xplane_root
    )
    if not geometry_by_resource:
        return set()

    def compute() -> set[tuple[str, str]]:
        result = object_terrain_features.classify_object_terrain_features(
            terrain_placements,
            geometry_by_resource,
            pavement_polygons_longitude_latitude=None,
            mean_sea_level_placements=mean_sea_level_placements,
            pack_root=pack_root or "",
        )
        _expand_exclusions_to_anchor_families(
            result, terrain_placements, pack_root or ""
        )
        return set(result.exclusions)

    return _cached_exclusion_pairs(
        pack_root or "",
        terrain_placements,
        mean_sea_level_placements,
        geometry_by_resource,
        compute,
    )


def _log_classification_summary(icao, result, road_networks) -> None:
    contract_counts: dict[str, int] = {}
    for bridge in result.bridges:
        contract_counts[bridge.contract] = (
            contract_counts.get(bridge.contract, 0) + 1
        )
    contract_summary = ", ".join(
        f"{contract}={count}"
        for contract, count in sorted(contract_counts.items())
    ) or "none"
    total_segments = sum(len(network.segments) for network in road_networks)
    UI.vprint(
        1,
        f"   [object-bridge] {icao}: "
        f"{len(result.bridges)} bridge(s) [{contract_summary}], "
        f"{len(result.tunnels)} tunnel(s), "
        f"{len(result.refusals)} refused, "
        f"{len(road_networks)} road network(s) "
        f"({total_segments} segment(s))",
    )
    for refusal in result.refusals:
        UI.vprint(
            2,
            "   [object-bridge] refused "
            f"{refusal.object_resources}: {refusal.reason}",
        )


# ---------------------------------------------------------------------------
# Feature A — object-derived tunnel terrain (spec section 3.3 + amendment A1,
# ruling R12).  Born pre-solve as first-class layout shapes, mirroring
# bridges.build_bridge_layout_shapes.
# ---------------------------------------------------------------------------


def _tunnel_footprint_meters_parts(tunnel, to_meters) -> list:
    """Project a tunnel's WHOLE-BODY deck footprint (amendment A1: the
    author cuts the entire body, not the mouths alone) to layout-meter
    shapely ``Polygon`` parts.  Empty list on absent/degenerate geometry."""
    from shapely.geometry import Polygon
    from .object_terrain_features import frame_polygon_to_longitude_latitude

    if tunnel.deck_footprint is None or tunnel.deck_footprint.is_empty:
        return []
    footprint_longitude_latitude = frame_polygon_to_longitude_latitude(
        tunnel.deck_footprint, tunnel.frame_origin_longitude_latitude
    )
    parts = (
        list(footprint_longitude_latitude.geoms)
        if footprint_longitude_latitude.geom_type == "MultiPolygon"
        else [footprint_longitude_latitude]
    )
    meter_polygons: list = []
    for part in parts:
        ring = [to_meters(lon, lat) for lon, lat in part.exterior.coords]
        if len(ring) < 3:
            continue
        try:
            polygon = Polygon(ring)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
        except Exception:
            continue
        if polygon.geom_type == "Polygon" and not polygon.is_empty:
            meter_polygons.append(polygon)
        elif polygon.geom_type == "MultiPolygon":
            meter_polygons.extend(
                geometry for geometry in polygon.geoms
                if geometry.geom_type == "Polygon" and not geometry.is_empty
            )
    return meter_polygons


def _split_annulus_to_simple_parts(geometry) -> list:
    """Split a rim-collar annulus (a polygon with a hole over the floor
    pan) into simply-connected ``Polygon`` parts so the flat-plate birth
    primitive — which triangulates from the exterior ring only — cannot
    fill the hole and bury the floor.  A polygon WITHOUT a hole passes
    through unchanged; an annulus is cut by a thin centroid cross into
    simply-connected arc pieces (the idiom
    ``bridges._emit_deck_lip_weld_strips`` uses for its box-wrapping
    strips)."""
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    parts = (
        list(geometry.geoms)
        if geometry.geom_type == "MultiPolygon" else [geometry]
    )
    simple_parts: list = []
    for part in parts:
        if part.geom_type != "Polygon" or part.is_empty:
            continue
        if not part.interiors:
            simple_parts.append(part)
            continue
        centroid = part.centroid
        minimum_x, minimum_y, maximum_x, maximum_y = part.bounds
        reach = max(maximum_x - minimum_x, maximum_y - minimum_y) + 10.0
        try:
            cross = unary_union([
                LineString([(centroid.x - reach, centroid.y),
                            (centroid.x + reach, centroid.y)]).buffer(0.05),
                LineString([(centroid.x, centroid.y - reach),
                            (centroid.x, centroid.y + reach)]).buffer(0.05),
            ])
            opened = part.difference(cross)
        except Exception:
            continue
        for piece in (
                opened.geoms if hasattr(opened, "geoms") else [opened]):
            if (piece.geom_type == "Polygon" and not piece.is_empty
                    and not piece.interiors):
                simple_parts.append(piece)
    return simple_parts


def build_tunnel_layout_shapes(layout, dem, tile_lat, tile_lon):
    """Feature A (``O4_OBJECT_TUNNEL_TERRAIN``, spec section 3.3 + amendment
    A1, ruling R12): born pre-solve tunnel-trench terrain as FIRST-CLASS
    layout shapes — the one-solve doctrine, mirroring
    ``bridges.build_bridge_layout_shapes``.

    Per classified tunnel (``classification.tunnels``, cached by
    :func:`attach_bridge_classification`):

    * **Datum** (the circular-datum rule): the object drapes at
      ``terrain(anchor)``, so the anchor's terrain is PINNED at layout as a
      solver INPUT — the DEM value at the placement anchor.
    * **Whole-body trench** (amendment A1): the WHOLE deck footprint (body +
      mouths) is cut, not the mouths alone — the roof OBJECT is the visible
      ground over the body, and terrain left at grade there would z-fight
      the roof slab.  A flat floor pan is born at the law floor
      (``grade_law.tunnel_trench_floor_elevation_m`` = datum − body depth −
      ``TUNNEL_FLOOR_BELOW_OBJECT_DECK_M``) inset ``_TRENCH_INSET_M``, and a
      rim collar is born at the datum
      (``grade_law.tunnel_trench_rim_elevation_m``) — the two coincident
      rows a node-split (``_CAUSEWAY_INWARD_OVERLAP_M``) apart form the
      near-vertical R2 wall.
    * **PAVEMENT WINS** (rulings R2/R8): the airside pavement union is
      subtracted from the body before birth and the yielded area is logged.

    Both plates carry ``layout.ROLE_TUNNEL_TRENCH`` — a flat-by-law terrain
    role wired (decimation exemption, weld LAW tier, per-node ``alt_abs`` at
    ``to_osm``, no within-shape grade rule) exactly like the bridge plates
    with ONE deliberate difference: it is OFF-PAVEMENT terrain (R2 subtracts
    the airside pavement from the body before birth), so it is NOT a
    pavement solver member (absent from ``PAVEMENT_ROLES``) and is born with
    ``record_pins=False``.  That keeps the deep floor from dragging adjacent
    airside pavement through the one-solve while the flat-by-law per-node
    ``alt_abs`` still cuts the trench and wins the LAW-tier weld at any
    shared vertex.  Returns ``(floor_plate_count, rim_plate_count)``; all
    zeros when the gate is off or no tunnel classified."""
    if not config.OBJECT_TUNNEL_TERRAIN:
        return 0, 0
    classification = getattr(layout, CLASSIFICATION_ATTRIBUTE, None)
    if classification is None or not getattr(classification, "tunnels", None):
        return 0, 0

    from shapely.geometry import Point
    from shapely.ops import unary_union

    from .bridges import (
        _local_meter_projections,
        _BRIDGE_PIN_ROLES,
        _TRENCH_INSET_M,
        _CAUSEWAY_INWARD_OVERLAP_M,
        born_flat_solver_plate,
    )
    from .elevation import _sample_dem
    from .grade_law import (
        tunnel_trench_floor_elevation_m,
        tunnel_trench_rim_elevation_m,
    )
    from .layout import ROLE_TUNNEL_TRENCH

    to_meters, _meters_to_lat_lon = _local_meter_projections(layout.anchor)

    # Airside pavement union (rulings R2/R8: pavement always wins over the
    # trench — the roof-slab-versus-pavement coplanarity is open question 5).
    pavement_polygons = [
        shape.polygon for shape in layout.shapes
        if shape.role in _BRIDGE_PIN_ROLES
        and shape.polygon is not None and not shape.polygon.is_empty
    ]
    try:
        pavement_union = (
            unary_union(pavement_polygons) if pavement_polygons else None
        )
    except Exception:
        pavement_union = None

    floor_plate_count = 0
    rim_plate_count = 0
    for tunnel in classification.tunnels:
        resources = tunnel.object_resources
        if tunnel.body_depth_m is None or tunnel.body_depth_m <= 0.0:
            UI.vprint(
                1,
                f"   [object-tunnel] {resources}: no below-grade body depth "
                "— skipped",
            )
            continue
        anchor_longitude, anchor_latitude = tunnel.anchor_longitude_latitude
        datum = _sample_dem(
            dem, tile_lat, tile_lon, anchor_latitude, anchor_longitude
        )
        if datum is None or datum != datum:
            # Silent-zero rule (this project's classic failure mode): a
            # missing datum is announced at verbosity 1, never swallowed.
            UI.vprint(
                1,
                f"   [object-tunnel] {resources}: no DEM datum at the "
                "anchor — skipped",
            )
            continue
        # The deck's effective level is negative below grade; the AGL offset
        # is already folded into ``body_depth_m`` (classifier effective
        # height), so ``-body_depth_m`` is passed straight to the law.
        floor_elevation = tunnel_trench_floor_elevation_m(
            float(datum), -float(tunnel.body_depth_m)
        )
        rim_elevation = tunnel_trench_rim_elevation_m(float(datum))

        body_parts = _tunnel_footprint_meters_parts(tunnel, to_meters)
        if not body_parts:
            UI.vprint(
                1,
                f"   [object-tunnel] {resources}: no deck footprint to cut "
                "— skipped",
            )
            continue

        yielded_area = 0.0
        for body in body_parts:
            trench_region = body
            if pavement_union is not None:
                try:
                    kept = body.intersection(pavement_union)
                    if not kept.is_empty:
                        yielded_area += kept.area
                    trench_region = body.difference(pavement_union)
                except Exception:
                    trench_region = body
            region_parts = (
                list(trench_region.geoms)
                if trench_region.geom_type == "MultiPolygon"
                else [trench_region]
            )
            for region in region_parts:
                if (region.geom_type != "Polygon" or region.is_empty
                        or region.area < 4.0):
                    continue
                # Floor pan inset _TRENCH_INSET_M; rim collar band
                # _CAUSEWAY_INWARD_OVERLAP_M wide at the datum — the same
                # geometry the bridge trench/causeway pair uses, so the
                # node-split wall gap is the identical
                # (_TRENCH_INSET_M - _CAUSEWAY_INWARD_OVERLAP_M) metres,
                # above the 0.5 m node-interning tolerance (ruling R2).
                try:
                    floor_geometry = region.buffer(-_TRENCH_INSET_M)
                    collar_geometry = region.difference(
                        region.buffer(-_CAUSEWAY_INWARD_OVERLAP_M)
                    )
                except Exception:
                    continue
                floor_parts = (
                    list(floor_geometry.geoms)
                    if floor_geometry.geom_type == "MultiPolygon"
                    else [floor_geometry]
                )
                region_floor_born = 0
                for floor_part in floor_parts:
                    if born_flat_solver_plate(
                            layout, floor_part, ROLE_TUNNEL_TRENCH,
                            "object_tunnel_trench", floor_elevation,
                            record_pins=False):
                        region_floor_born += 1
                if not region_floor_born:
                    # A body too thin to seat a floor pan at the trench
                    # inset (the tiny negative-AGL shells) is left at grade
                    # rather than emitting a floorless rim ring — the rim is
                    # meaningless without a floor to wall down to.
                    continue
                floor_plate_count += region_floor_born
                for collar_part in _split_annulus_to_simple_parts(
                        collar_geometry):
                    if collar_part.area < 1.0:
                        continue
                    if born_flat_solver_plate(
                            layout, collar_part, ROLE_TUNNEL_TRENCH,
                            "object_tunnel_rim", rim_elevation,
                            record_pins=False):
                        rim_plate_count += 1

        if yielded_area > 1.0:
            UI.vprint(
                1,
                f"   [object-tunnel] {resources}: {yielded_area:.0f} m2 of "
                "body under airside pavement kept at pavement grade",
            )
        UI.vprint(
            1,
            f"   [object-tunnel] {resources}: trench floor {floor_elevation:.2f} "
            f"m, rim {rim_elevation:.2f} m (datum {float(datum):.2f}, body "
            f"depth {float(tunnel.body_depth_m):.2f} m)",
        )
    return floor_plate_count, rim_plate_count
