"""Crossing influence zone — Phase 1 of the crossing terrain ownership
architecture (docs/specs/crossing-terrain-ownership.md).

THE RULE (spec section 2): inside a crossing's influence zone, exactly ONE
assembly builds ALL terrain-valued geometry; everything else — adjacent-
ground bands, runway-end skirts, clearance strips, gap-fill spines — never
enters the zone at all.  Phase 1 publishes that zone ON THE LAYOUT at
classification time (pre-solve), replacing the five per-consumer exclusion
systems the rounds 5-8 defect arc showed to be the disease: each consumer
reconstructed "the crossing" independently at ITS phase, and every defect
in that arc was a coordination failure between those reconstructions.

WHAT THE ZONE IS
----------------
One polygon (published both per crossing and as a cached union) covering,
for every crossing the object classifier recognizes:

* corridor bridges / causeways — the deck box;
* tunnel portal pairs — each portal's FULL solid footprint (the captured
  never-stack building pad, round 5: the collar derives its lateral extent
  from it) plus the collar-reach ring around it
  (``TUNNEL_PORTAL_CROWN_COLLAR_M`` + 2 m — bands must start OUTSIDE the
  collar feather or they cut a cliff right beside it, round 6);
* the mapped depressed-road corridor those crossings carry — built by the
  neutral ``road_lanes`` loader (mapped carriageway at mapped width; the
  round-8 phase-independent rework), with the recognized crossings' own
  footprints added as growth seeds so a road passing under a recognized
  deck is protected even where the mapper omitted ``tunnel=yes``.

THE BURIED-ROOF EXCEPTION LIVES HERE (spec section 2 point 1): over the
portal-to-portal BURIED span the zone deliberately contains only the road
bore (the mapped corridor), never the wide connecting band — so pavement
bands march the tunnel roof normally, which is the round-6/7 ruling, now
true BY CONSTRUCTION instead of by a carve-out subtracted inside one
consumer's mask.  ``O4_ADJACENT_GROUND_BURIED_BODY_BAND=0`` (the knob's
historical name, kept for operator continuity; it moved here from
``adjacent_ground``) restores the fully-masked crossing: the wide
connecting band joins the zone and the roof stops banding.

PHASE CONTRACT (spec section 6, owner direction): the zone is published
PRE-solve — after ``bridges.build_bridge_layout_shapes`` (which caches the
portal pairs and captures the full footprints), before the skirt / gap-fill
/ band constructions — so a later phase can register the zone's coupling to
neighboring pavement as shared solver nodes instead of post-emit adoption.
Consumers NEVER rebuild crossing geometry themselves: they call
:func:`crossing_influence_zone_union` and treat the result as a hard
keep-out.  A layout on which nothing was published (gate off, no anchor,
geometry-only tests) reads ``None`` — no keep-out, byte-inert.

GATES
-----
* ``config.BRIDGE_CROSSING_MASK`` — gates the classifier-owned components
  (deck boxes, portal footprints, collar rings), exactly the scope it
  historically masked.  OFF: only the road corridor publishes.
* ``O4_ADJACENT_GROUND_BURIED_BODY_BAND`` — see above (default ON).
* ``O4_CROSSING_ZONE_PROBE="lat,lon;lat,lon"`` — forensic: logs each
  point's zone containment at publication (the patch-probe companion).
"""
from __future__ import annotations

import math
import os

from shapely.geometry import LineString, Point
from shapely.errors import GEOSException, TopologicalError
from shapely.ops import unary_union
from shapely.prepared import prep

import O4_UI_Utils as UI

from . import config as _CFG
from .road_lanes import road_lane_exclusion_union

__all__ = [
    "publish_crossing_influence_zones",
    "crossing_influence_zone_union",
    "crossing_influence_zone_prepared",
    "CROSSING_INFLUENCE_ZONES_ATTRIBUTE",
    "CROSSING_INFLUENCE_ZONE_UNION_ATTRIBUTE",
]

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

# Per-crossing zone records: ``list[dict]`` of
# ``{"kind": "bridge_corridor" | "tunnel_portal_pair" | "road_corridor",
#    "zone": <shapely polygon>}`` — the per-crossing granularity is what
# Phase 2 hangs the per-crossing height model (and any per-crossing config,
# spec section 5 question 3) off of.
CROSSING_INFLUENCE_ZONES_ATTRIBUTE = "_crossing_influence_zones"
# The cached union every consumer reads (``None`` = nothing published).
CROSSING_INFLUENCE_ZONE_UNION_ATTRIBUTE = "_crossing_influence_zone_union"

# Buried-roof rollback knob — historical name kept (it governed the same
# physical behavior when the carve-out lived inside ``adjacent_ground``).
_BURIED_BODY_BAND = os.environ.get(
    "O4_ADJACENT_GROUND_BURIED_BODY_BAND", "1") == "1"

_ZONE_PROBE_LATLON: list[tuple[float, float]] = []
for _pair in (os.environ.get("O4_CROSSING_ZONE_PROBE") or "").split(";"):
    if "," in _pair:
        _pa, _pb = _pair.split(",", 1)
        try:
            _ZONE_PROBE_LATLON.append((float(_pa), float(_pb)))
        except ValueError:
            continue


def _classifier_zone_components(layout):
    """``(components, seed_polygons)`` for the classifier-owned crossings:
    per-crossing zone records (deck boxes; portal full footprints + collar
    rings; the rollback connecting band when the buried-roof gate is OFF)
    and the raw crossing polygons handed to the road-corridor loader as
    growth seeds.  Both empty when nothing is classified."""
    from . import bridges as _BRIDGES

    components: list[dict] = []
    seed_polygons: list = []
    to_meters = None
    classification = _BRIDGES._object_bridge_classification(layout)
    if classification is not None:
        to_meters, _meters_to_lat_lon = _BRIDGES._local_meter_projections(
            layout.anchor)
        (corridor_bridges, _suppress, _refused, _road_carried,
         _portal_records) = _BRIDGES._partition_bridges_for_corridors(
            classification, layout)
        for bridge in corridor_bridges:
            box = _BRIDGES._bridge_deck_box_meters(bridge, layout)
            if box is None:
                box = _BRIDGES._bridge_footprint_meters(bridge, to_meters)
            if box is not None and not box.is_empty:
                components.append({"kind": "bridge_corridor", "zone": box})
                seed_polygons.append(box)

    pairs = getattr(
        layout, _BRIDGES._TUNNEL_PORTAL_PAIRS_ATTRIBUTE, None) or []
    collar_ring_m = float(_CFG.TUNNEL_PORTAL_CROWN_COLLAR_M) + 2.0
    for pair in pairs:
        parts: list = []
        deck_footprints: list = []
        for portal in pair.get("portals", ()):
            # The FULL solid footprint (captured never-stack building pad,
            # round 5) where ``build_bridge_layout_shapes`` persisted one;
            # the deck footprint otherwise.  The collar is cut from the
            # full footprint, so the zone (and its collar ring) must reach
            # at least as far.
            footprint = (portal.get("full_footprint")
                         or portal.get("footprint"))
            if footprint is None or footprint.is_empty:
                continue
            parts.append(footprint)
            seed_polygons.append(footprint)
            deck = portal.get("footprint")
            if deck is not None and not deck.is_empty:
                deck_footprints.append(deck)
            try:
                parts.append(footprint.buffer(collar_ring_m))
            except _GEOM_EXC:
                pass
        # BURIED-ROOF EXCEPTION, by construction: with the gate ON the wide
        # portal-to-portal connecting band is NOT part of the zone — over
        # the buried span only the road bore (the mapped corridor) keeps
        # writers out, so the roof bands normally.  Gate OFF restores the
        # historical fully-masked crossing.
        if not _BURIED_BODY_BAND and len(deck_footprints) == 2:
            centroid_a = deck_footprints[0].centroid
            centroid_b = deck_footprints[1].centroid
            half_width = 10.0 + 0.5 * max(
                math.sqrt(deck_footprints[0].area),
                math.sqrt(deck_footprints[1].area))
            try:
                parts.append(
                    LineString([(centroid_a.x, centroid_a.y),
                                (centroid_b.x, centroid_b.y)]
                               ).buffer(half_width))
            except _GEOM_EXC:
                pass
        if not parts:
            continue
        try:
            zone = unary_union(parts)
        except _GEOM_EXC:
            continue
        if zone is not None and not zone.is_empty:
            components.append({"kind": "tunnel_portal_pair", "zone": zone})
    return components, seed_polygons


def publish_crossing_influence_zones(layout, sample_dem=None) -> int:
    """Build and publish the crossing influence zones on ``layout``.

    Called ONCE per build, pre-solve, right after
    ``bridges.build_bridge_layout_shapes`` (the portal pairs and full
    footprints it needs are cached there) and before every consumer
    construction (skirts, gap-fill, band march).  Returns the number of
    published zone records (0 = nothing to publish; consumers read
    ``None`` and are byte-inert).

    ``sample_dem`` (``(x, y) -> alt | None``, optional) is forwarded to the
    road-corridor loader's vertical-sanity trim; pre-solve there are no
    emitted approach pieces to derive a road grade from, so it is
    typically inert here — passed anyway so a caller with a better grade
    source keeps the hook."""
    setattr(layout, CROSSING_INFLUENCE_ZONES_ATTRIBUTE, [])
    setattr(layout, CROSSING_INFLUENCE_ZONE_UNION_ATTRIBUTE, None)
    if getattr(layout, "anchor", None) is None:
        return 0

    components: list[dict] = []
    seed_polygons: list = []
    # Classifier-owned crossing components (their historical gate).
    if _CFG.BRIDGE_CROSSING_MASK:
        try:
            components, seed_polygons = _classifier_zone_components(layout)
        except _GEOM_EXC:
            components, seed_polygons = [], []

    # The mapped depressed-road corridor — UNgated (protecting a depressed
    # public road from burial was never subject to the crossing mask; the
    # skirt and band lane clips it replaces ran regardless).  The crossing
    # polygons seed its growth so the road under a recognized deck is
    # covered even without a ``tunnel=yes`` tag.
    try:
        corridor = road_lane_exclusion_union(
            layout, sample_dem=sample_dem,
            extra_seed_geometries=seed_polygons or None)
    except _GEOM_EXC:
        corridor = None
    if corridor is not None and not corridor.is_empty:
        components.append({"kind": "road_corridor", "zone": corridor})

    if not components:
        return 0
    try:
        union = unary_union([c["zone"] for c in components])
    except _GEOM_EXC:
        return 0
    if union is None or union.is_empty:
        return 0
    setattr(layout, CROSSING_INFLUENCE_ZONES_ATTRIBUTE, components)
    setattr(layout, CROSSING_INFLUENCE_ZONE_UNION_ATTRIBUTE, union)
    _log_zone_probe(layout, union)
    # Forensic: O4_CROSSING_ZONE_DUMP=<path> writes the published union at
    # publication — JSON with the layout anchor and the union's WKT
    # (layout meters) — for patch-level conformance tooling
    # (tools/crossing_zone_conformance.py).
    _dump_path = os.environ.get("O4_CROSSING_ZONE_DUMP")
    if _dump_path:
        try:
            import json
            with open(_dump_path, "w") as _dump_file:
                json.dump({"anchor": [float(layout.anchor[0]),
                                      float(layout.anchor[1])],
                           "wkt": union.wkt}, _dump_file)
        except OSError:
            pass
    return len(components)


def crossing_influence_zone_union(layout):
    """The published crossing influence zone union, or ``None``.

    THE consumer entry point: adjacent-ground bands, runway-end skirts,
    clearance strips, and gap-fill spines take this single geometry as a
    hard keep-out and build NO crossing geometry of their own.  ``None``
    (nothing published) means no keep-out."""
    union = getattr(layout, CROSSING_INFLUENCE_ZONE_UNION_ATTRIBUTE, None)
    if union is None or union.is_empty:
        return None
    return union


def crossing_influence_zone_prepared(layout):
    """Prepared-geometry form of the zone union for point-containment
    loops (the band march's station test), or ``None``."""
    union = crossing_influence_zone_union(layout)
    if union is None:
        return None
    try:
        return prep(union)
    except _GEOM_EXC:
        return None


def _log_zone_probe(layout, union):
    """Forensic: report each ``O4_CROSSING_ZONE_PROBE`` point's containment
    in the published zone.  No-op unless the probe env is set."""
    if not _ZONE_PROBE_LATLON or layout.anchor is None:
        return
    for lat, lon in _ZONE_PROBE_LATLON:
        try:
            x, y = layout.ll_to_m(lat, lon)
            in_zone = bool(union is not None
                           and union.contains(Point(x, y)))
        except _GEOM_EXC:
            continue
        UI.vprint(
            1, f"  [crossing-zone][probe] {lat:.7f},{lon:.7f}: "
               f"in_zone={in_zone}")
