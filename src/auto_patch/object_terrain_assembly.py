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

import math
import os

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
        lines = dsf_reader._load_dsf_text(dsf_path)
        if not lines:
            continue
        network = dsf_road_network.parse_dsf_road_networks(lines)
        if network.segments:
            networks.append(network)
    return networks


# Bump when classifier logic or record shapes change — invalidates every
# pack-sidecar classification cache (see attach_bridge_classification).
# Version 3: classifier performance round 2026-07-10 (evidence
# pre-screen, composed placement transform, bulk footprint unions) —
# results are equivalent within float tolerance but must be rebuilt on
# the new code path.
_CLASSIFICATION_CACHE_VERSION = 3

# Sidecar file name inside the airport package (pack-root level; the
# in-pack precedent is the ``.anchor_bak`` object backups the Phase 2
# y-bake writes next to each adjusted object).
_CLASSIFICATION_SIDECAR_NAME = "o4_object_terrain_classification.cache"


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

    Returns ``(None, None)`` when no pack root is known (nowhere to put
    a sidecar) or fingerprinting fails."""
    if not pack_root or not os.path.isdir(pack_root):
        return None, None
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
    return (
        os.path.join(pack_root, _CLASSIFICATION_SIDECAR_NAME),
        digest.hexdigest(),
    )


def attach_bridge_classification(layout, xplane_root: str):
    """Classify the airport pack's bridge/tunnel objects and cache the
    result (plus sibling road networks) on ``layout``.

    Gated by ``config.OBJECT_BRIDGE_TERRAIN``: with the gate OFF this is a
    complete no-op — nothing is read, nothing is attached, and the bridge
    emitters take their unchanged legacy paths (flag-off byte identity).

    Returns the :class:`object_terrain_features.ClassificationResult` (also
    cached on ``layout``) or ``None`` when the gate is off or no overlay
    DSF could be located.

    Idempotent: stage 2 attaches PRE-solve (the pin writers need the
    records before the seam hook), and the post-solve emitter hook calls
    this again as a fallback — a result already cached on the layout is
    returned as-is, never recomputed."""
    if not config.OBJECT_BRIDGE_TERRAIN:
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
    # sidecar INSIDE the airport package — the same in-pack convention
    # as the ``.anchor_bak`` object backups — guarded by a fingerprint
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

    Gate-checked: with ``O4_OBJECT_BRIDGE_TERRAIN`` off this returns an
    empty set having read NOTHING (Phase 2 behaviour unchanged).  Gate on,
    it reruns the same cached read→load→classify chain as
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

    Workstream W-T extends this with the ``O4_OBJECT_TUNNEL_TERRAIN``
    gate: tunnel structures land on the same exclusion list (spec
    section 3.3 step 5) — the bridge gate check below becomes an
    either-gate check when the tunnel feature lands.
    """
    if not config.OBJECT_BRIDGE_TERRAIN:
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
