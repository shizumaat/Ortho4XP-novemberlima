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

# A resource placed more often than this is scenery clutter (trees, lamp
# posts, fence posts) and is never a tunnel/bridge structure — skip it so
# the classifier is not fed thousands of identical footprints.  Mirrors
# Phase 1's posture of not pooling mass-placed resources; a real
# tunnel/bridge object is placed a handful of times (EGLL: one placement
# per tunnel; KBNA taxiway-L: six part objects).
MAXIMUM_PLACEMENTS_PER_RESOURCE = 50


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

    pavement_polygons = _pavement_polygons_longitude_latitude(layout)
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

    setattr(layout, CLASSIFICATION_ATTRIBUTE, result)

    tile_lat = int(math.floor(anchor_latitude))
    tile_lon = int(math.floor(anchor_longitude))
    road_networks = _discover_sibling_road_networks(
        xplane_root, tile_lat, tile_lon
    )
    setattr(layout, ROAD_NETWORKS_ATTRIBUTE, road_networks)

    _log_classification_summary(
        getattr(layout, "icao", "?"), result, road_networks
    )
    return result


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
