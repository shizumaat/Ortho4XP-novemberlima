"""Taxi/road bridges + tunnel portals + depressed-road segments.

Five emitters:

* ``_emit_tunnel_portals`` — short ramp polygons at tunnel-OSM
  entrance / exit so the road dives under airport pavement.
* ``_scenery_has_bridge_objects`` — DSF-pavement check that gates
  bridge emission (no point emitting bridge polygons if the
  underlying scenery already provides bridge meshes).
* ``_emit_taxi_bridges`` — taxi-over-road bridges.
* ``_emit_underpass_road_approaches`` — road approaches sloping
  down toward an underpass.
* ``_emit_through_airport_depressed_roads`` — road segments
  depressed below airport surface where they cut through.

**All five are gated by ``EMIT_BRIDGES_AND_TUNNELS`` in
``O4_Pavement_Config`` (currently True).**  Each emitter carves
its footprint out of overlapping airside / groundside pavement
before emitting so ``test_no_self_overlap`` stays green.

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    _emit_tunnel_portals
    _scenery_has_bridge_objects
    _emit_taxi_bridges
    _emit_underpass_road_approaches
    _emit_through_airport_depressed_roads
"""
from __future__ import annotations

import math
import os
import re

import O4_UI_Utils as UI

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.affinity import translate as shapely_translate
from shapely.ops import linemerge, nearest_points, substring, unary_union
from shapely.strtree import STRtree

# Narrow exception tuple for shapely / numeric-geometry failure
# modes + file I/O.  Programming errors propagate so they surface
# immediately rather than being silently masked at runtime.
_GEOM_EXC = (OSError, ValueError,
             GEOSException, TopologicalError)

from .layout import (
    AEROWAY_FOR_ROLE,
    BuiltShape,
    PavementLayout,
    R_EARTH,
    ROLE_APRON,
    ROLE_BOUNDARY,
    ROLE_CROSS_CONNECTOR,
    ROLE_GROUNDSIDE_PAVEMENT,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_SECONDARY_PARALLEL,
    ROLE_STUB,
    ROLE_BUILDING,
    ROLE_RETAINING_WALL,
    ROLE_RUNWAY_CROSSING,
    ROLE_TUNNEL_RAMP,
    ROLE_TUNNEL_TRENCH,
    SHARED_VERTEX_TOL_M,
)
from .pavement.vertices import _snap_polygon_vertices_to_rect_corners
from .pavement.runways import _sample_runway_segment_elev
from .elevation import _resample_node_altitudes_nn, _sample_dem
from .geom_safe import min_rotated_rect
from . import config as _CFG
from . import dsf_road_network
from .config import (
    IMPLIED_CROSSING_TUNNELS,
    SKIP_TUNNEL_RAMPS_NEAR_ROADS,
    TUNNEL_ADJACENT_ROAD_DIST_M,
    TUNNEL_DEM_CUT_MIN_DROP_M,
    TUNNEL_DEM_CUT_WINDOW_M,
    TUNNEL_FORK_THROAT,
    TUNNEL_LOW_CONNECTOR_MAX_OPEN_GAP_M,
    TUNNEL_MOUTH_PLATE_LENGTH_M,
    TUNNEL_MOUTH_WINDOW_M,
    TUNNEL_ROOF_PLATE_MAX_LENGTH_M,
)

# Feature B (object-derived bridge terrain, docs/object_terrain_features_
# spec.md).  The two layout attributes the assembler
# (``object_terrain_assembly``) caches the classifier output under; read
# here at emission time.  Read the live ``_CFG.OBJECT_BRIDGE_TERRAIN`` /
# ``_CFG.BRIDGE_ROAD_CLEARANCE_M`` (never a bound copy) so the gate and
# the clearance constant honour env + monkeypatch at call time.
_OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE = "_object_bridge_classification"
_TUNNEL_PORTAL_PAIRS_ATTRIBUTE = "_object_tunnel_portal_pairs"
_OBJECT_BRIDGE_ROAD_NETWORKS_ATTRIBUTE = "_object_bridge_road_networks"
_OBJECT_BRIDGE_ROUTE_LINES_ATTRIBUTE = "_object_bridge_route_lines"


__all__ = [
    "_emit_taxi_bridges",
    "_emit_through_airport_depressed_roads",
    "_emit_tunnel_portals",
    "_emit_underpass_road_approaches",
    "_scenery_has_bridge_objects",
]


# Per-OSM-highway-type carriageway width (user 2026-05-03).
# Was a single 22 m default, which made every tunnel look like a
# 6-lane motorway.  Real-world widths vary by classification; the
# numbers below match typical FAA-relevant standards (single
# carriageway including shoulders).
HIGHWAY_CARRIAGEWAY_WIDTH_M = {
    "motorway":         25.0,  # 25 m corridor per user 2026-07-04 (KDFW)
    "motorway_link":     8.0,
    "trunk":            22.0,
    "trunk_link":        8.0,
    "primary":          18.0,
    "primary_link":      7.0,
    "secondary":        15.0,  # 15 m corridor per user 2026-07-04 (KDFW)
    "secondary_link":    7.0,
    "tertiary":          9.0,
    "tertiary_link":     6.0,
    "residential":       7.0,
    "service":           6.0,
    # Pseudo-type for railway tunnel bores.  Single-track right-of-way
    # (one ``railway=rail`` line, no ``tracks=2``): a rail bore is
    # NARROWER than the road carriageway it forks from — user 2026-06-13,
    # "roads are supposed to be wider (double I think) than rail".  Was
    # 10 m (double-track, user 2026-06-12); a 9 m tertiary road is now
    # ~2× this.
    "railway":          5.0,
    # TWIN parallel rails emitted as ONE bore (user 2026-07-04, KCLT):
    # OSM maps each track as its own ``railway=rail`` line; two lines
    # side by side are one double-track corridor — ~5 m per rail plus
    # margin, "12 m to 15 m for two rails".
    "railway_twin":    14.0,
}

# Two rail tunnel lines closer than this run in ONE corridor — the pair
# emits a single ``railway_twin`` bore (the other line's portals are
# suppressed).
TWIN_RAIL_NEAR_M = 10.0

# Every tunnel break (portal split point) lands this far OUTSIDE the
# taxiway pavement edge (user 2026-07-04, KDFW): the portal's 1 m-thick
# retaining-wall cap then occupies exactly [pavement edge, edge + 1 m],
# with the ramp's low end right behind it.
TAXI_EDGE_BREAK_MARGIN_M = 1.0

# OSM ways less than this far apart group into a SINGLE underpass
# corridor (user 2026-07-04, KDFW): a motorway + its frontage road +
# a rail line running together get one combined ramp, never
# overlapping per-way ramps.  KDFW's two motorway carriageways run
# ~113 m apart and correctly stay separate corridors.
UNDERPASS_GROUP_DIST_M = 35.0


def _carriageway_width_for(highway_type: str | None,
                            default_m: float) -> float:
    """Return the carriageway width in metres for an OSM highway
    type, falling back to ``default_m`` for unknown types.
    """
    if highway_type is None:
        return default_m
    return HIGHWAY_CARRIAGEWAY_WIDTH_M.get(highway_type, default_m)


# Per-lane width for ``lanes=``-derived carriageway sizing (typical
# rural/urban lane, between the 3.0 m urban minimum and the 3.65 m
# trunk standard).
LANE_WIDTH_M = 3.5


def _carriageway_width_from_tags(highway_type: str | None,
                                 tags: dict | None,
                                 default_m: float) -> float:
    """Carriageway width in metres for a road way, preferring the way's
    own OSM measurements over the per-type table (user 2026-07-16,
    EGPB: the table's 18 m ``primary`` entry sized the A970's ramps at
    ~3× the mapped ~6.5 m single carriageway):

      1. ``width=`` — metres (bare number, ``6.5 m``, or a comma
         decimal), sanity-clamped to [2.5, 40] m;
      2. ``lanes=`` × ``LANE_WIDTH_M``;
      3. the ``HIGHWAY_CARRIAGEWAY_WIDTH_M`` table via
         :func:`_carriageway_width_for` (which also serves the
         ``railway`` / ``railway_twin`` pseudo-types).

    Road caches written before the 2026-07-16 tag-schema bump carry
    neither tag, so they fall through to the table unchanged.
    """
    if tags:
        raw_width = tags.get("width")
        if raw_width:
            text = str(raw_width).strip().lower()
            if text.endswith("m"):
                text = text[:-1].strip()
            try:
                width_value = float(text.replace(",", "."))
            except ValueError:
                width_value = None
            if width_value is not None and 2.5 <= width_value <= 40.0:
                return width_value
        raw_lanes = tags.get("lanes")
        if raw_lanes:
            try:
                lane_count = int(str(raw_lanes).strip())
            except ValueError:
                lane_count = None
            if lane_count is not None and 1 <= lane_count <= 10:
                return max(4.0, lane_count * LANE_WIDTH_M)
    return _carriageway_width_for(highway_type, default_m)


# ── Portal OUTWARD ramp width (user ruling 2026-07-15, KBNA runway-02C,
# supersedes the 2026-07-14c "as wide as the MOUTH FACE" rule) ────────
# A tunnel-portal object whose sides slant up into retaining walls has a
# footprint far wider than the DRIVABLE road that emerges from it (KBNA
# portal 1: an 84 m footprint over a 6-lane, 21 m carriageway).  The
# outward approach ramp must match the drivable road, never the full
# footprint, resolved as the first available of:
#   1. the mapped OSM carriageway width of the road crossing the
#      footprint (``_carriageway_width_from_tags``) PLUS a small shoulder
#      margin;
#   2. else the classified deck cross-section width
#      (``BridgeStructure.deck_width_m`` — the narrower rotated-rectangle
#      dimension of the drivable deck surface);
#   3. else the mouth-face width as a last resort.
# The result is ALWAYS capped at the mouth-face width (never wider than
# the object it emerges from).
PORTAL_RAMP_SHOULDER_MARGIN_M = 2.0
# Planning-grade headroom under TUNNEL_RAMP_MAX_GRADE for the legacy
# tunnel ramp chains: absorbs the 0.1 m altitude rounding on short
# quads (a 9 m segment could otherwise round up to ~4.4 % when the
# design grade is exactly 4 %).  Shared by the walk-truncation sizing
# and the effective-space grade clamp in ``_emit_chain``.
TUNNEL_RAMP_GRADE_SAFETY_MARGIN = 0.005
# A big-road highway way within this distance of the portal footprint is
# the road the outward ramp follows; the draped/OSM centrelines the
# corridor walks carry no tags, so the mapped width is re-associated to
# the tagged big-road ways by geometry (the plumbing chosen 2026-07-15).
PORTAL_ROAD_ASSOCIATION_M = 15.0


def _mapped_osm_carriageway_width_m(layout, footprint, to_meters):
    """Widest mapped OSM carriageway (via ``_carriageway_width_from_tags``)
    of a big-road highway way crossing — within
    :data:`PORTAL_ROAD_ASSOCIATION_M` of — the portal ``footprint``, in
    metres.  ``None`` when no highway way is near the footprint or the
    big-road cache is absent.

    The draped road centrelines the corridor already walks
    (``_draped_road_centerlines_meters``) come from the sibling DSF road
    network and carry NO OSM tags, and the OSM fallback
    (``_load_underpass_osm_road_lines``) drops the tags too — so the
    mapped width cannot be read off the walked lines.  It is instead
    re-associated by geometry to the tagged big-road ways
    (``_load_osm_big_roads``), the same cache the level-crossing veto and
    the underpass fallback read."""
    try:
        from .pipeline import _load_osm_big_roads
        nodes_raw, ways_raw = _load_osm_big_roads(
            layout.anchor[0], layout.anchor[1]
        )
    except Exception:
        return None
    if not ways_raw:
        return None
    nodes_meters: dict[str, tuple[float, float]] = {}
    for node_id, (latitude, longitude) in nodes_raw.items():
        nodes_meters[node_id] = to_meters(longitude, latitude)
    best = None
    for _way_id, node_refs, tags in ways_raw:
        if tags.get("highway") is None:
            continue  # railway ways carry no carriageway width
        points = [nodes_meters[n] for n in node_refs if n in nodes_meters]
        if len(points) < 2:
            continue
        try:
            line = LineString(points)
            if (line.is_empty
                    or line.distance(footprint) > PORTAL_ROAD_ASSOCIATION_M):
                continue
        except _GEOM_EXC:
            continue
        # default 0.0 → an unknown highway type resolves to 0 and is
        # skipped; width= / lanes= / a known type table entry all resolve
        # to a positive carriageway width.
        width = _carriageway_width_from_tags(tags.get("highway"), tags, 0.0)
        if width > 0.0 and (best is None or width > best):
            best = width
    return best


def _portal_outward_ramp_width_m(
        layout, portal, footprint, to_meters, mouth_face_width_m):
    """Resolve the portal OUTWARD ramp width (user ruling 2026-07-15):
    mapped OSM carriageway + shoulder → classified deck-face width →
    mouth-face width, always capped at the mouth-face width.  Returns
    ``(width_m, provenance)``; ``width_m`` is ``None`` only when no source
    resolves (no OSM road, no deck width, no mouth face)."""
    cap = mouth_face_width_m
    mapped = _mapped_osm_carriageway_width_m(layout, footprint, to_meters)
    if mapped is not None and mapped > 0.0:
        width = mapped + PORTAL_RAMP_SHOULDER_MARGIN_M
        provenance = (
            f"mapped OSM {mapped:.1f} m + "
            f"{PORTAL_RAMP_SHOULDER_MARGIN_M:.0f} m shoulder")
    else:
        deck_face = getattr(portal["bridge"], "deck_width_m", None)
        if deck_face is not None and deck_face > 0.0:
            width = float(deck_face)
            provenance = f"deck-face {deck_face:.1f} m"
        elif cap is not None and cap > 0.0:
            width = float(cap)
            provenance = "mouth-face (last resort)"
        else:
            return None, "unresolved"
    if cap is not None and cap > 0.0 and width > cap:
        width = float(cap)
        provenance += f" (capped at mouth face {cap:.1f} m)"
    return width, provenance


# ── At-grade level-crossing veto for IMPLIED bores (user 2026-07-16,
# EGPB / Gibraltar) ──────────────────────────────────────────────────
# A public through-road crossing runway pavement is normally assumed to
# tunnel under it (``IMPLIED_CROSSING_TUNNELS``) — but a handful of
# airports have a genuine GATED LEVEL CROSSING (Sumburgh's A970 across
# runway 09/27, Gibraltar's Winston Churchill Avenue).  OSM maps these
# with positive at-grade evidence on the crossing way's own nodes:
# ``aeroway=aircraft_crossing`` (usually with ``crossing:aircraft=*``)
# at the intersection, plus ``barrier`` gates / lift gates where the
# road is closed for aircraft movements.  Evidence within the radii
# below of a crossing segment vetoes the synthetic bore — the road
# stays at grade.  Mapped ``tunnel=yes`` ways are never vetoed (the
# mapper's word beats the heuristic in both directions).
AIRCRAFT_CROSSING_VETO_DIST_M = 60.0    # tag sits ON the crossing
LEVEL_CROSSING_GATE_VETO_DIST_M = 120.0  # gates flank the runway strip
LEVEL_CROSSING_BARRIER_VALUES = frozenset((
    "gate", "lift_gate", "swing_gate", "sliding_gate",
))


def _local_meter_projections(anchor: tuple[float, float]):
    """Return ``(to_meters, meters_to_lat_lon)`` closures converting
    between (lon, lat) degrees and the local-meter frame anchored at
    ``anchor`` — the one equirectangular projection every emitter in
    this module shares.
    """
    anchor_lat, anchor_lon = anchor
    cos_anchor = math.cos(math.radians(anchor_lat))

    def to_meters(lon: float, lat: float) -> tuple[float, float]:
        return (math.radians(lon - anchor_lon) * R_EARTH * cos_anchor,
                math.radians(lat - anchor_lat) * R_EARTH)

    def meters_to_lat_lon(x: float, y: float) -> tuple[float, float]:
        return (anchor_lat + math.degrees(y / R_EARTH),
                anchor_lon + math.degrees(x / (R_EARTH * cos_anchor)))

    return to_meters, meters_to_lat_lon


HW_TUNNEL_TYPES = {
    "motorway", "trunk", "primary", "secondary",
    "tertiary", "motorway_link", "trunk_link",
    "primary_link", "residential", "service",
    # ``unclassified`` is the standard OSM class for minor public
    # roads (user 2026-07-17, EGGW: the airside service-road tunnel
    # under the taxiway is ``highway=unclassified tunnel=yes`` and
    # was invisible while ``service``/``residential`` qualified).
    "unclassified",
}
# Rail tunnels qualify too (user 2026-06-12, KPHL: a combined
# road+rail tunnel passes under the hill past the RWY 26
# threshold — the rail bore is railway=rail tunnel=yes and has
# no highway tag at all, so the highway-only filter dropped it).
RAIL_TUNNEL_TYPES = {
    "rail", "light_rail", "subway", "narrow_gauge", "tram",
}

def _tunnelable(tags9: dict) -> bool:
    return (tags9.get("highway") in HW_TUNNEL_TYPES
            or tags9.get("railway") in RAIL_TUNNEL_TYPES)
TUNNEL_VALUES = {"yes", "building_passage"}
# Portal EMISSION qualifies only real excavated tunnels (user
# 2026-06-12): ``building_passage`` is a BUILDING built over an
# at-grade road — no trench, no ramps, nothing for the patch to
# model (KPHL's terminal-complex service passages).  The broader
# TUNNEL_VALUES set stays for the surface-walk exclusions — a
# ramp should not continue INTO a passage either way.
PORTAL_TUNNEL_VALUES = {"yes"}


def _load_tunnel_road_network(layout: "PavementLayout"):
    """Load the big-roads + small-roads OSM caches for the tile
    and merge them under namespaced ids.  Returns ``(nodes_r,
    ways_r, big_way_ids, node_tags_r)`` where ``big_way_ids`` is the
    id set of the big-roads ways (pre-2026-06-12 candidate class) and
    ``node_tags_r`` maps node id → tag dict for the (few) nodes whose
    tags the road-layer download whitelist retained (level-crossing
    evidence: ``aeroway=aircraft_crossing``, ``barrier`` gates —
    empty for caches written before the tag-schema bump).
    """
    from .osm_load import _load_osm_road_layer
    # Load big-roads OSM cache for this tile — AND small_roads (user
    # 2026-06-12, KPHL): the big/small highway split puts tertiary /
    # residential / service ways in small_roads, so a minor-road
    # tunnel bore (KPHL's road+rail tunnel under the RWY 26 hill:
    # highway=tertiary, 151 m past the threshold) was invisible to
    # this emitter even though its type is in HW_TUNNEL_TYPES.
    nodes_r, ways_r, node_tags_r = _load_osm_road_layer(
        "big_roads", layout.anchor[0], layout.anchor[1])
    _big_way_ids = {w[0] for w in ways_r}
    nodes_s, ways_s, node_tags_s = _load_osm_road_layer(
        "small_roads", layout.anchor[0], layout.anchor[1])
    if nodes_s:
        # ⚠ The road caches use SYNTHETIC per-layer negative ids:
        # ``r-13-078:-202`` in big_roads and in small_roads are
        # DIFFERENT real-world features.  A raw dict merge overwrote
        # big-road node coordinates with unrelated small-road points
        # and displaced whole tunnel ways by kilometres (SPJC's 4
        # user-approved tunnels measured 5-12 km from the boundary
        # and vanished).  Namespace every small-cache id instead.
        merged_n = dict(nodes_r)
        for nid, ll in nodes_s.items():
            merged_n["S|" + nid] = ll
        nodes_r = merged_n
        node_tags_r = dict(node_tags_r)
        for nid, tags in node_tags_s.items():
            node_tags_r["S|" + nid] = tags
        ways_r = list(ways_r) + [
            ("S|" + wid, ["S|" + n for n in nrefs], tags)
            for wid, nrefs, tags in ways_s]
    return nodes_r, ways_r, _big_way_ids, node_tags_r


def _synthesize_implied_crossing_bores(
        layout: "PavementLayout",
        nodes_m: dict,
        ways_r: list,
        excluded_way_ids: set | None,
        low_connector_max_gap_m: float = 0.0,
        node_tags: dict | None = None) -> tuple:
    """Split public through-roads / railways that cross taxi/runway
    pavement into approach + synthetic ``tunnel=yes`` bore pieces.
    Mutates ``nodes_m`` (synthetic split nodes) and returns
    ``(ways_r, low_connector_gaps)``.

    ``node_tags`` (node id → tag dict, from the road-layer caches)
    carries the at-grade level-crossing evidence: a crossing with an
    ``aeroway=aircraft_crossing`` node or nearby ``barrier`` gates is a
    real gated level crossing (EGPB, Gibraltar — user 2026-07-16) and
    gets NO implied bore.

    User 2026-07-04 (KDFW wide underpasses), three behaviours on top
    of the original implied-bore split:

    * MAPPED ``tunnel=yes`` ways of the same public classes are
      RE-SPLIT by the same pavement intersections (gate
      ``O4_TUNNEL_TAXI_BREAKS``): the OSM mapper's tunnel-segment
      endpoints land wherever they were drawn, so breaks came at the
      taxiway edge for implied bores but at arbitrary spots for
      mapped ones ("some seem to be doing that, and others not").
      Deriving every break from OUR pavement makes them uniform.  A
      mapped tunnel that crosses NO taxi/runway pavement is a road
      built over (terminal buildings, aprons) — retagged
      ``building_passage``: no trench, no ramps, but ramp walks
      still refuse to route through it.
    * Every break lands ``TAXI_EDGE_BREAK_MARGIN_M`` (1 m) OUTSIDE
      the pavement edge, so the portal's 1 m retaining-wall cap
      occupies exactly [edge, edge+1 m] — wall at the taxiway edge,
      ramp low end right behind it.
    * Consecutive bores along one way whose surface gap is shorter
      than ``low_connector_max_gap_m`` (too short to ramp up to DEM
      and back — the double-parallel-taxiway case) MERGE into one
      long bore and the gap is recorded in ``low_connector_gaps``
      as ``(gap_line, corridor_width_m)``: the caller emits it as a
      single flat rect at the low elevation with retaining walls,
      instead of two facing overlapping ramps.  Gate
      ``O4_TUNNEL_LOW_CONNECTORS``.
    """
    # ── IMPLIED CROSSING TUNNELS (user 2026-07-04) ────────────────────
    # A PUBLIC through-road or railway that crosses taxiway/runway
    # pavement cannot do so at grade — assume a tunnel under the
    # pavement even when OSM carries no tunnel tag.  The way is SPLIT at
    # the pavement-edge crossing points into approach + synthetic
    # ``tunnel=yes`` bore + approach pieces; everything downstream
    # (portal walks, ramps, retaining walls, twin-bore clustering, the
    # adjacent-road system veto) then treats the bore exactly like a
    # mapped tunnel, so ramps emit on either side of the pavement.
    # Service/residential roads are excluded — airport service roads
    # legitimately cross taxi routes at grade.
    low_connector_gaps: list = []
    if IMPLIED_CROSSING_TUNNELS:
        _IMPLIED_HW_TYPES = {
            "motorway", "trunk", "primary", "secondary", "tertiary",
            "motorway_link", "trunk_link", "primary_link",
        }
        _IMPLIED_CROSS_ROLES = (
            "runway", "runway_crossing", "primary_parallel",
            "secondary_parallel", "stub", "cross_connector", "junction")
        _IMPLIED_MIN_BORE_M = 6.0      # narrower = a sliver graze
        _IMPLIED_MAX_BORE_M = 500.0    # longer = through-airport road
        _IMPLIED_END_MARGIN_M = 2.0    # way must CROSS, not END inside
        # Only a near-COINCIDENT mapped tunnel suppresses an implied
        # bore (a duplicate line of the same physical feature).  Was
        # 40 m — but parallel carriageways/frontage roads within 35 m
        # now GROUP into one corridor (user 2026-07-04), so a mapped
        # motorway tunnel must not silently swallow the frontage
        # road's own crossing.
        _IMPLIED_MAPPED_NEAR_M = 6.0
        # Gate: derive mapped-tunnel breaks from OUR pavement too.
        _taxi_breaks = os.environ.get(
            "O4_TUNNEL_TAXI_BREAKS", "1") == "1"
        _low_connectors = (low_connector_max_gap_m > 0.0
                           and os.environ.get(
                               "O4_TUNNEL_LOW_CONNECTORS", "1") == "1")
        try:
            _cross_pav_u = unary_union(
                [s.polygon for s in layout.shapes
                 if s.polygon is not None and not s.polygon.is_empty
                 and s.role in _IMPLIED_CROSS_ROLES])
            if _cross_pav_u.is_empty:
                _cross_pav_u = None
        except _GEOM_EXC:
            _cross_pav_u = None
        # Built-over cover (buildings / apron pads): a mapped tunnel
        # under THESE is a road built up and over — no trench, no
        # ramps.  A mapped tunnel under mere grass/RESA (CYUL's
        # runway-24-end underpass, KPHL's hill bore) is a REAL trench
        # and keeps its mapped portals when it crosses no taxiway.
        try:
            _built_over_u = unary_union(
                [s.polygon for s in layout.shapes
                 if s.polygon is not None and not s.polygon.is_empty
                 and s.role in ("building", "apron")])
            if _built_over_u.is_empty:
                _built_over_u = None
        except _GEOM_EXC:
            _built_over_u = None

        def _resplittable(_tags) -> bool:
            # A mapped ``tunnel=yes`` way of the public classes gets its
            # breaks re-derived from pavement like an unmarked way.
            return (_taxi_breaks
                    and _tags.get("tunnel") == "yes"
                    and (_tags.get("highway") in _IMPLIED_HW_TYPES
                         or _tags.get("railway") in RAIL_TUNNEL_TYPES))
        _mapped_tunnel_lines = []
        if _cross_pav_u is not None:
            for _wid, _nrefs, _tags in ways_r:
                if _tags.get("tunnel") not in TUNNEL_VALUES:
                    continue
                if _resplittable(_tags):
                    continue    # re-split below — must not self-suppress
                _pts = [nodes_m[n] for n in _nrefs if n in nodes_m]
                if len(_pts) >= 2:
                    try:
                        _mapped_tunnel_lines.append(LineString(_pts))
                    except _GEOM_EXC:
                        continue
        _excluded_early = excluded_way_ids or set()
        _n_implied = 0
        _n_level_crossings = 0
        if _cross_pav_u is not None:
            _split_ways: list = []
            for _wid, _nrefs, _tags in ways_r:
                _had_tunnel = _resplittable(_tags)
                _eligible = (
                    (_tags.get("tunnel") not in TUNNEL_VALUES
                     or _had_tunnel)
                    and not _tags.get("bridge")
                    and _wid not in _excluded_early
                    and (_tags.get("highway") in _IMPLIED_HW_TYPES
                         or _tags.get("railway") in RAIL_TUNNEL_TYPES))
                _present = ([(n, nodes_m[n]) for n in _nrefs
                             if n in nodes_m] if _eligible else [])
                if not _eligible or len(_present) < 2:
                    _split_ways.append((_wid, _nrefs, _tags))
                    continue
                try:
                    _line = LineString([p for (_n, p) in _present])
                    if not _line.intersects(_cross_pav_u):
                        if (_had_tunnel and _built_over_u is not None
                                and _line.intersects(_built_over_u)):
                            # Mapped tunnel crossing NO taxi/runway
                            # pavement but running under a BUILDING /
                            # apron pad = a road built up and over
                            # (KDFW terminals): no trench, no ramps —
                            # but ramp walks still must not route
                            # through it (user 2026-07-04).  A mapped
                            # tunnel under mere grass keeps its mapped
                            # portals (CYUL runway-24 end).
                            _ptags = dict(_tags)
                            _ptags["tunnel"] = "building_passage"
                            _split_ways.append((_wid, _nrefs, _ptags))
                        else:
                            _split_ways.append((_wid, _nrefs, _tags))
                        continue
                    _inter = _line.intersection(_cross_pav_u)
                except _GEOM_EXC:
                    _split_ways.append((_wid, _nrefs, _tags))
                    continue
                _parts = ([_inter] if _inter.geom_type == "LineString"
                          else [g for g in getattr(_inter, "geoms", ())
                                if g.geom_type == "LineString"])
                # At-grade level-crossing evidence on THIS way's nodes
                # (user 2026-07-16, EGPB/Gibraltar): the crossing node
                # itself (``aeroway=aircraft_crossing`` /
                # ``crossing:aircraft``) or the barrier gates that close
                # the road for aircraft movements.  Collected once per
                # way, tested per crossing segment below.
                _level_evidence: list = []   # (Point, is_crossing_tag)
                if node_tags and not _had_tunnel:
                    for _en, _ep in _present:
                        _ent = node_tags.get(_en)
                        if not _ent:
                            continue
                        if (_ent.get("aeroway") == "aircraft_crossing"
                                or "crossing:aircraft" in _ent):
                            _level_evidence.append((Point(_ep), True))
                        elif (_ent.get("barrier")
                                in LEVEL_CROSSING_BARRIER_VALUES):
                            _level_evidence.append((Point(_ep), False))
                _intervals: list = []
                for _part in _parts:
                    if not (_IMPLIED_MIN_BORE_M <= _part.length
                            <= _IMPLIED_MAX_BORE_M):
                        continue
                    if _level_evidence and not _had_tunnel:
                        _veto = False
                        for _epoint, _is_crossing_tag in _level_evidence:
                            _radius = (AIRCRAFT_CROSSING_VETO_DIST_M
                                       if _is_crossing_tag
                                       else LEVEL_CROSSING_GATE_VETO_DIST_M)
                            try:
                                if _part.distance(_epoint) <= _radius:
                                    _veto = True
                                    break
                            except _GEOM_EXC:
                                continue
                        if _veto:
                            _n_level_crossings += 1
                            continue
                    try:
                        _s1 = _line.project(Point(*_part.coords[0]))
                        _s2 = _line.project(Point(*_part.coords[-1]))
                    except _GEOM_EXC:
                        continue
                    if _s2 < _s1:
                        _s1, _s2 = _s2, _s1
                    # must CROSS the pavement (extend beyond both
                    # sides).  A previously-MAPPED tunnel is a KNOWN
                    # underpass — it may legitimately start/end right
                    # at (or under) the pavement, so it skips this.
                    if not _had_tunnel and (
                            _s1 < _IMPLIED_END_MARGIN_M
                            or _s2 > _line.length - _IMPLIED_END_MARGIN_M):
                        continue
                    # a mapped tunnel already models this underpass
                    if not _had_tunnel and any(
                            _part.distance(_tl) < _IMPLIED_MAPPED_NEAR_M
                            for _tl in _mapped_tunnel_lines):
                        continue
                    _intervals.append((_s1, _s2))
                if not _intervals:
                    if (_had_tunnel and _built_over_u is not None
                            and _line.intersects(_built_over_u)):
                        # Its only pavement contacts were slivers /
                        # over-long grazes and it runs under a
                        # building/apron — treat as built-over.
                        _ptags = dict(_tags)
                        _ptags["tunnel"] = "building_passage"
                        _split_ways.append((_wid, _nrefs, _ptags))
                    else:
                        _split_ways.append((_wid, _nrefs, _tags))
                    continue
                _intervals.sort()
                # The break lands TAXI_EDGE_BREAK_MARGIN_M outside the
                # pavement edge (user 2026-07-04): the portal's 1 m wall
                # cap then occupies exactly [edge, edge+1 m].
                _intervals = [
                    (max(0.05, _s1 - TAXI_EDGE_BREAK_MARGIN_M),
                     min(_line.length - 0.05,
                         _s2 + TAXI_EDGE_BREAK_MARGIN_M),
                     _s1, _s2)
                    for (_s1, _s2) in _intervals]
                # Merge overlapping bores, and — when the surface gap
                # between consecutive bores is too short for a ramp
                # pair to reach DEM and come back — merge ACROSS the
                # gap into one long bore, recording the gap for the
                # flat low-connector emit (user 2026-07-04: the area
                # between double parallel taxiways is all at the low
                # elevation).
                _merged: list = [list(_intervals[0])]
                for _iv in _intervals[1:]:
                    _prev = _merged[-1]
                    _gap = _iv[0] - _prev[1]
                    if _gap <= 0.0:
                        _prev[1] = max(_prev[1], _iv[1])
                        _prev[3] = max(_prev[3], _iv[3])
                    elif _low_connectors and _gap < low_connector_max_gap_m:
                        # The road cannot surface and return within this
                        # gap, so the bores merge either way.  The gap's
                        # VISIBLE form depends on its width (user
                        # 2026-07-10, SPJC big NW crossing): a narrow gap
                        # (KDFW double-parallel-taxiway median, 30-70 m)
                        # is dug open as a flat low connector; anything
                        # wider stays COVERED — no trench recorded, no
                        # mid-gap portals, ground bridged over the bore.
                        if _gap < TUNNEL_LOW_CONNECTOR_MAX_OPEN_GAP_M:
                            try:
                                _gline = substring(_line, _prev[3], _iv[2])
                            except _GEOM_EXC:
                                _gline = None
                            if (_gline is not None
                                    and _gline.geom_type == "LineString"
                                    and _gline.length > 1.0):
                                _gw = _carriageway_width_from_tags(
                                    _tags.get("highway") or "railway",
                                    _tags, 22.0)
                                low_connector_gaps.append((_gline, _gw))
                        _prev[1] = _iv[1]
                        _prev[3] = _iv[3]
                    else:
                        _merged.append(list(_iv))
                _intervals = [(_a, _b) for (_a, _b, _r1, _r2) in _merged]
                # MAPPED-END PRESERVATION (user 2026-07-10, SPJC big NW
                # tunnel): the re-split derives a mapped tunnel's breaks
                # from OUR pavement (KDFW mapper-break cleanup), which
                # plants portals at the pavement edges INSIDE the real
                # bore when the mapped tunnel extends far beyond the
                # crossing (SPJC: 1.27 km mapped trunk bores with the
                # crossings mid-way — ramps emitted inside the tunnel).
                # A mapped end stretch longer than the open-gap design
                # cap is genuine covered tunnel, not mapper sloppiness:
                # keep it BORE by clamping the outermost interval to the
                # way end, so the portal (and the approach ramps walked
                # beyond it) sit at the TRUE mapped mouth.  Short end
                # stretches keep the re-split cleanup (KDFW-validated:
                # portal cap at [pavement edge, edge + 1 m]).
                if _had_tunnel and _intervals:
                    if _intervals[0][0] \
                            > TUNNEL_LOW_CONNECTOR_MAX_OPEN_GAP_M:
                        _intervals[0] = (0.05, _intervals[0][1])
                    if (_line.length - _intervals[-1][1]
                            > TUNNEL_LOW_CONNECTOR_MAX_OPEN_GAP_M):
                        _intervals[-1] = (_intervals[-1][0],
                                          _line.length - 0.05)
                # split the way: approach | bore | approach | bore | ...
                _arcs = [0.0]
                for _k in range(1, len(_present)):
                    _arcs.append(_arcs[-1] + math.hypot(
                        _present[_k][1][0] - _present[_k - 1][1][0],
                        _present[_k][1][1] - _present[_k - 1][1][1]))
                _pieces: list = []      # (nref list, is_bore)
                _cur: list = []
                _idx = 0
                _syn = 0
                for (_s1, _s2) in _intervals:
                    while _idx < len(_present) and _arcs[_idx] <= _s1 - 0.01:
                        _cur.append(_present[_idx][0])
                        _idx += 1
                    _pin = _line.interpolate(_s1)
                    _sid_in = f"IMP|{_wid}|{_syn}"
                    _syn += 1
                    nodes_m[_sid_in] = (_pin.x, _pin.y)
                    _cur.append(_sid_in)
                    if len(_cur) >= 2:
                        _pieces.append((_cur, False))
                    _bore = [_sid_in]
                    while _idx < len(_present) and _arcs[_idx] < _s2 - 0.01:
                        _bore.append(_present[_idx][0])
                        _idx += 1
                    _pout = _line.interpolate(_s2)
                    _sid_out = f"IMP|{_wid}|{_syn}"
                    _syn += 1
                    nodes_m[_sid_out] = (_pout.x, _pout.y)
                    _bore.append(_sid_out)
                    _pieces.append((_bore, True))
                    _cur = [_sid_out]
                while _idx < len(_present):
                    _cur.append(_present[_idx][0])
                    _idx += 1
                if len(_cur) >= 2:
                    _pieces.append((_cur, False))
                for _j, (_refs, _is_bore) in enumerate(_pieces):
                    _ptags = dict(_tags)
                    if _is_bore:
                        _ptags["tunnel"] = "yes"
                        _ptags["o4_implied_tunnel"] = "1"
                        _n_implied += 1
                    elif _had_tunnel:
                        # A re-split mapped tunnel's leftover pieces
                        # are surface approaches — drop the tunnel tag
                        # so portal walks can route along them.
                        _ptags.pop("tunnel", None)
                    _split_ways.append((f"{_wid}|IMP{_j}", _refs, _ptags))
            ways_r = _split_ways
        if _n_implied:
            try:
                UI.vprint(1,
                    f"  [pav-builder] implied {_n_implied} tunnel bore(s) "
                    f"under taxi/runway pavement (unmarked road/rail "
                    f"crossings).")
            except _GEOM_EXC:
                pass
        if _n_level_crossings:
            try:
                UI.vprint(1,
                    f"  [pav-builder] kept {_n_level_crossings} road/rail "
                    f"crossing(s) AT GRADE — OSM level-crossing evidence "
                    f"(aircraft-crossing node / barrier gates), no "
                    f"implied tunnel.")
            except _GEOM_EXC:
                pass
    return ways_r, low_connector_gaps


def _build_surface_way_indices(ways_r: list):
    """Index the road ways for surface-road walking.  Returns
    ``(way_by_id, node_to_ways)``.
    """
    # Build node-to-way and way-by-id indices for surface-road walking.
    way_by_id: dict[str, tuple[list[str], dict[str, str]]] = {}
    node_to_ways: dict[str, list[str]] = {}
    for wid, nrefs, tags in ways_r:
        way_by_id[wid] = (nrefs, tags)
        for n in nrefs:
            node_to_ways.setdefault(n, []).append(wid)
    return way_by_id, node_to_ways


# Helper: orient ``o_nrefs`` so it starts at ``anchor_nid`` and
# walks AWAY from ``anchor_nid``.  When the anchor is mid-way,
# picks the longer side.  Returns None if anchor isn't on the way.
def _orient_away(o_nrefs: list[str],
                 anchor_nid: str,
                 nodes_m: dict) -> list[str] | None:
    try:
        idx = o_nrefs.index(anchor_nid)
    except ValueError:
        return None
    forward = o_nrefs[idx:]
    backward = list(reversed(o_nrefs[:idx + 1]))
    if idx == 0:
        return forward
    if idx == len(o_nrefs) - 1:
        return backward
    # Mid-way: pick the longer leg.
    def _leg_len(refs: list[str]) -> float:
        return sum(
            math.hypot(
                nodes_m[refs[i + 1]][0] - nodes_m[refs[i]][0],
                nodes_m[refs[i + 1]][1] - nodes_m[refs[i]][1])
            for i in range(len(refs) - 1)
            if (refs[i] in nodes_m
                and refs[i + 1] in nodes_m))
    return forward if _leg_len(forward) >= _leg_len(backward) \
        else backward


# Helper: from a portal node, walk a chain of connecting non-
# tunnel surface roads OUTWARD for ``length_m`` metres.  When
# the current OSM way ends, follow the connected highway way
# whose first segment best continues the current direction
# (smallest turn angle) — keeps us on the main road through
# OSM-imposed splits at intersections instead of bailing into
# a side street.  Returns the walked path as a list of (x, y)
# points starting at the portal, or None if no valid surface
# way connects.
def _walk_surface(portal_nid: str,
                  tunnel_wid: str,
                  length_m: float,
                  nodes_m: dict,
                  way_by_id: dict,
                  node_to_ways: dict,
                  carriageway_width_m: float
                  ) -> list[tuple[float, float]] | None:
    if portal_nid not in nodes_m:
        return None
    # Pick the FIRST surface highway way leaving the portal.
    # When several candidates connect, prefer the one whose
    # first segment is most-aligned with the tunnel direction
    # (so divided-highway crossings don't turn into a service
    # road off the main carriageway).
    if tunnel_wid in way_by_id:
        tw_nrefs = way_by_id[tunnel_wid][0]
        t_oriented = _orient_away(tw_nrefs, portal_nid,
                                      nodes_m)
        if t_oriented and len(t_oriented) >= 2 \
                and t_oriented[1] in nodes_m:
            tp = nodes_m[portal_nid]
            tn = nodes_m[t_oriented[1]]
            tdx, tdy = tn[0] - tp[0], tn[1] - tp[1]
            tlen = math.hypot(tdx, tdy) or 1.0
            # Tunnel direction points INTO the tunnel; the
            # surface walk goes the OPPOSITE way.
            tunnel_outward_dir: tuple[float, float] | None = (
                -tdx / tlen, -tdy / tlen)
        else:
            tunnel_outward_dir = None
    else:
        tunnel_outward_dir = None

    first_way: str | None = None
    first_refs: list[str] | None = None
    best_align: float = -2.0
    for other_wid in node_to_ways.get(portal_nid, []):
        if other_wid == tunnel_wid:
            continue
        if other_wid not in way_by_id:
            continue
        o_nrefs, o_tags = way_by_id[other_wid]
        if o_tags.get("tunnel") in TUNNEL_VALUES:
            continue
        if not _tunnelable(o_tags):
            continue
        refs = _orient_away(o_nrefs, portal_nid, nodes_m)
        if refs is None or len(refs) < 2 \
                or refs[1] not in nodes_m:
            continue
        if tunnel_outward_dir is None:
            first_way, first_refs = other_wid, refs
            break
        cp = nodes_m[portal_nid]
        cn = nodes_m[refs[1]]
        cdx, cdy = cn[0] - cp[0], cn[1] - cp[1]
        clen = math.hypot(cdx, cdy) or 1.0
        align = (cdx * tunnel_outward_dir[0]
                 + cdy * tunnel_outward_dir[1]) / clen
        if align > best_align:
            best_align = align
            first_way, first_refs = other_wid, refs
    if first_refs is None or first_way is None:
        return None

    pts: list[tuple[float, float]] = []
    cums: list[float] = []
    cum = 0.0
    visited_ways = {tunnel_wid, first_way}
    current_refs = first_refs
    current_hw = way_by_id[first_way][1].get("highway")
    # Loop detection: a surface road that folds back on itself
    # (roundabout, hairpin) brings the walk back into the corridor
    # of an EARLIER segment.  Continuing would emit ramp polygons
    # overlapping the ramps already laid down (LMML SW Kirkop:
    # the walk looped a roundabout and the returning tail overlapped
    # its own start by 104 m² with a 3.8 m elevation step).  Stop
    # the walk when a new node lands within one corridor width of an
    # earlier node that is more than ``_loop_ignore_m`` back along
    # the path (the back-distance gate keeps gentle curves and
    # normal forward progress from tripping it).
    _loop_hit_m = carriageway_width_m
    _loop_ignore_m = max(2.0 * carriageway_width_m, 40.0)

    def _append_node(p: tuple[float, float]) -> bool:
        """Append ``p`` to ``pts``; truncate at ``length_m`` or
        where the path loops back on itself.  Returns True if the
        walk should stop."""
        nonlocal cum
        if not pts:
            pts.append(p)
            cums.append(0.0)
            return False
        seg_len = math.hypot(
            p[0] - pts[-1][0], p[1] - pts[-1][1])
        new_cum = cum + seg_len
        # Self-intersection (loop) check against non-recent points.
        # ``cums`` is monotonically increasing, so once the back-
        # distance drops below the ignore band the remaining points
        # are all recent — stop scanning.
        for i in range(len(pts)):
            if new_cum - cums[i] < _loop_ignore_m:
                break
            if math.hypot(p[0] - pts[i][0],
                          p[1] - pts[i][1]) < _loop_hit_m:
                return True
        if new_cum >= length_m:
            if seg_len > 0:
                t = (length_m - cum) / seg_len
                tx = pts[-1][0] + t * (p[0] - pts[-1][0])
                ty = pts[-1][1] + t * (p[1] - pts[-1][1])
                pts.append((tx, ty))
                cums.append(length_m)
            cum = length_m
            return True
        cum = new_cum
        pts.append(p)
        cums.append(cum)
        return False

    while True:
        stopped = False
        for n in current_refs:
            if n not in nodes_m:
                stopped = True
                break
            if _append_node(nodes_m[n]):
                return pts
        if stopped:
            break
        # Try to chain to a connected highway way at the end
        # of ``current_refs``.  Skip tunnels and ways already
        # visited; prefer same highway type, then most-straight
        # continuation (smallest turn angle).
        last_nid = current_refs[-1]
        if len(pts) < 2:
            break
        end_dir_x = pts[-1][0] - pts[-2][0]
        end_dir_y = pts[-1][1] - pts[-2][1]
        ed_len = math.hypot(end_dir_x, end_dir_y) or 1.0
        end_dir = (end_dir_x / ed_len, end_dir_y / ed_len)
        best_score = -2.0
        best_wid: str | None = None
        best_refs: list[str] | None = None
        for cand_wid in node_to_ways.get(last_nid, []):
            if cand_wid in visited_ways:
                continue
            if cand_wid not in way_by_id:
                continue
            c_nrefs, c_tags = way_by_id[cand_wid]
            if c_tags.get("tunnel") in TUNNEL_VALUES:
                continue
            if c_tags.get("highway") not in HW_TUNNEL_TYPES:
                continue
            refs = _orient_away(c_nrefs, last_nid, nodes_m)
            if refs is None or len(refs) < 2 \
                    or refs[1] not in nodes_m:
                continue
            first_p = nodes_m[refs[1]]
            last_p = nodes_m[last_nid]
            fdx = first_p[0] - last_p[0]
            fdy = first_p[1] - last_p[1]
            fl = math.hypot(fdx, fdy) or 1.0
            align = (fdx * end_dir[0] + fdy * end_dir[1]) / fl
            same_hw = 1 if c_tags.get("highway") == current_hw else 0
            # Tie-break: same highway type beats alignment by
            # ~0.1 (~25° turn), so we follow trunk → trunk over
            # trunk → service even when both are similar angle.
            score = align + 0.1 * same_hw
            if score > best_score:
                best_score = score
                best_wid = cand_wid
                best_refs = refs
        if best_refs is None or best_wid is None:
            break
        visited_ways.add(best_wid)
        current_hw = way_by_id[best_wid][1].get("highway")
        # Skip refs[0]; it's the connecting node already in pts.
        current_refs = best_refs[1:]

    if len(pts) >= 2:
        return pts
    return None


def _build_adjacent_road_index(ways_r: list, nodes_m: dict,
                               skip_if_adjacent_road: bool):
    """Build the other-road line index + STRtree and the
    all-tunnel node set for the adjacent-road veto.  Returns
    ``(other_road_lines, other_road_tree, tunnel_all_nodes)``.
    """
    # Adjacent-road skip (user 2026-06-12, LMML): tunnels that run
    # under / alongside OTHER roads sit in a dense interchange where the
    # surface walk traces a tangle of parallel carriageways, slip roads
    # and roundabouts, and the ramps overlap.  Rather than model that,
    # skip ramp emission for a tunnel whose line is CROSSED by — or runs
    # within ``adjacent_road_dist_m`` of — another road.  The "other
    # road" set excludes ``highway=service`` (minor aisles/driveways),
    # other tunnels (a divided highway's own clustered carriageway), and
    # — per tunnel, below — any way sharing a node with it (the surface
    # continuation the ramp is meant to follow).  This skips all 6 LMML
    # tunnels and keeps SPJC's user-approved tunnels (crossed only by
    # service roads; parallel carriageway > dist away).
    _other_road_lines: list = []   # (LineString, frozenset(nodes), wid)
    _other_road_tree = None
    # Every node of ANY tunnel-tagged way (any tunnel value): a bore's
    # covered middle sections may carry different tags than the portal
    # candidates, and the twin continuation shares nodes with THOSE —
    # a per-candidate node set misses them (CYUL).
    _tunnel_all_nodes: set = set()
    if skip_if_adjacent_road:
        for _w2, _n2, _t2 in ways_r:
            if _t2.get("tunnel") in TUNNEL_VALUES:
                _tunnel_all_nodes.update(_n2)
        try:
            for _w2, _n2, _t2 in ways_r:
                if _t2.get("highway") is None:
                    continue
                if _t2.get("highway") == "service":
                    continue
                if _t2.get("tunnel") in TUNNEL_VALUES:
                    continue
                _pts2 = [nodes_m[n] for n in _n2 if n in nodes_m]
                if len(_pts2) < 2:
                    continue
                try:
                    _other_road_lines.append(
                        (LineString(_pts2), frozenset(_n2), _w2))
                except _GEOM_EXC:
                    continue
            if _other_road_lines:
                _other_road_tree = STRtree(
                    [ln for ln, _, _ in _other_road_lines])
        except _GEOM_EXC:
            _other_road_tree = None
    return _other_road_lines, _other_road_tree, _tunnel_all_nodes


def _tunnel_has_adjacent_road(tw_id2, t_nrefs2, system_nodes,
                              nodes_m: dict,
                              adjacent_road_dist_m: float,
                              other_road_lines: list,
                              other_road_tree,
                              parent_nodes: set | None = None) -> bool:
    """True when the tunnel way is crossed by — or runs within
    ``adjacent_road_dist_m`` of — a foreign (non-service) road;
    see the adjacent-road skip comment in
    ``_build_adjacent_road_index``.

    ``parent_nodes``: every node of the candidate's ORIGINAL
    (pre-re-split) way.  The share-a-node exemption must evaluate at
    that scope — the re-split moves a way's original end nodes into
    its approach pieces, so a bore clamped to the mapped tunnel end
    no longer shares a node with the surface continuation it hands
    off to, and the veto read the continuation as a foreign road at
    0 m (SPJC true-mouth clamp, 2026-07-10).
    """
    if other_road_tree is None:
        return False
    _pts = [nodes_m[n] for n in t_nrefs2 if n in nodes_m]
    if len(_pts) < 2:
        return False
    try:
        _tline = LineString(_pts)
        _buf = _tline.buffer(adjacent_road_dist_m)
    except _GEOM_EXC:
        return False
    _tnodes = set(t_nrefs2) | (parent_nodes or set())
    for _qi in other_road_tree.query(_buf):
        _oline, _onodes, _owid = other_road_lines[int(_qi)]
        if _owid == tw_id2 or (_tnodes & _onodes):
            continue
        try:
            _crosses = _tline.crosses(_oline)
            if not _crosses and _tline.distance(_oline) \
                    >= adjacent_road_dist_m:
                continue
            # Parallel continuation of a twin bore in THIS way's own
            # underpass system: not a veto (crossing roads always
            # veto; a continuation of a FOREIGN tunnel still vetoes —
            # exempting those half-emitted the LMML tangle).
            if (not _crosses and system_nodes is not None
                    and (_onodes & system_nodes)):
                continue
            if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                _mx, _my = _pts[len(_pts) // 2]
                print(f"    [tunnel-skip] way {tw_id2} blocked by "
                      f"road {_owid} (crosses={_crosses}, "
                      f"d={_tline.distance(_oline):.0f}m) "
                      f"mid local ({_mx:.0f},{_my:.0f})")
            return True
        except _GEOM_EXC:
            continue
    return False


def _compute_tunnel_system_veto(
        ways_r: list, nodes_m: dict, excluded: set,
        adjacent_road_dist_m: float, skip_if_adjacent_road: bool,
        other_road_lines: list, other_road_tree,
        tunnel_all_nodes: set) -> dict:
    """Group tunnel candidates into systems by proximity and
    propagate the adjacent-road veto across each system.
    Returns the per-way veto map.
    """
    # ── SYSTEM-LEVEL veto propagation (user 2026-07-04) ──────────────
    # The per-way twin-bore exemption alone half-emits an interchange:
    # at LMML the parallel bores of a tangle were exempted while their
    # CROSSING mates stayed vetoed, and the emitted ramps overlapped the
    # vetoed roads (baseline 0 → 8 vertex + 25 mid-edge steps,
    # measured).  Group tunnel candidates into SYSTEMS by geometric
    # proximity (twin carriageways never share nodes) and veto ALL
    # members when ANY member is vetoed — a clean divided-highway
    # underpass (CYUL runway-24 end: parallel twins only, no crossing
    # road) emits whole, an interchange tangle stays out whole.
    _system_veto: dict = {}      # tw_id -> True (skip) / False (emit)
    if skip_if_adjacent_road:
        _cands = []              # (tw_id, t_nrefs, LineString)
        for _tw, _tn, _tt in ways_r:
            if _tt.get("tunnel") not in PORTAL_TUNNEL_VALUES:
                continue
            if not _tunnelable(_tt):
                continue
            if _tw in excluded or len(_tn) < 2:
                continue
            _p = [nodes_m[nn] for nn in _tn if nn in nodes_m]
            if len(_p) < 2:
                continue
            try:
                _cands.append((_tw, _tn, LineString(_p)))
            except _GEOM_EXC:
                continue
        parent = list(range(len(_cands)))

        def _find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        for a in range(len(_cands)):
            for b in range(a + 1, len(_cands)):
                try:
                    if _cands[a][2].distance(_cands[b][2]) \
                            < adjacent_road_dist_m * 1.5:
                        ra, rb = _find(a), _find(b)
                        if ra != rb:
                            parent[rb] = ra
                except _GEOM_EXC:
                    continue
        _sys_bad: dict = {}
        # Node sets of the candidates' ORIGINAL ways (the re-split
        # appends pieces as ``<parent>|IMP<j>``) — the share-a-node
        # exemption evaluates at whole-original-way scope, see
        # ``_tunnel_has_adjacent_road``.
        _parent_nodes: dict = {}
        for _tw9, _tn9, _tt9 in ways_r:
            _pid9 = _tw9.split("|IMP", 1)[0]
            _parent_nodes.setdefault(_pid9, set()).update(_tn9)
        _raw = [_tunnel_has_adjacent_road(
                    _cands[k][0], _cands[k][1], tunnel_all_nodes,
                    nodes_m, adjacent_road_dist_m,
                    other_road_lines, other_road_tree,
                    parent_nodes=_parent_nodes.get(
                        _cands[k][0].split("|IMP", 1)[0]))
                for k in range(len(_cands))]
        for k in range(len(_cands)):
            r = _find(k)
            _sys_bad[r] = _sys_bad.get(r, False) or _raw[k]
        for k, (_tw, _tn, _ln) in enumerate(_cands):
            _system_veto[_tw] = _sys_bad[_find(k)]
            if (not _sys_bad[_find(k)]
                    and os.environ.get("O4_TUNNEL_DEBUG") == "1"):
                _mx, _my = list(_ln.coords)[len(list(_ln.coords)) // 2]
                print(f"    [tunnel-emit] way {_tw} len={_ln.length:.0f}m "
                      f"raw_veto={_raw[k]} mid local ({_mx:.0f},{_my:.0f})")
    return _system_veto


def _gather_portal_walks(
        ways_r: list, nodes_m: dict, way_by_id: dict,
        node_to_ways: dict, excluded: set, system_veto: dict,
        big_way_ids: set, airside_gate_union,
        max_boundary_dist_m: float, arm_walk_max_m: float,
        carriageway_width_m: float, airport_elevation_at,
        meters_to_lat_lon, dem, tile_lat: int, tile_lon: int,
        tunnel_depth_m: float, plan_grade: float,
        ramp_min_length_m: float) -> list:
    """Walk every qualifying tunnel portal's surface approach
    and collect the per-portal ramp data (twin-rail merge, the
    per-portal gates, walk merge / densify / grade truncation).
    """
    # Collect portal data: (portal_node_id, tunnel_wid, walk_pts,
    # hw_type, apt_elev_at_portal, dem_at_far_end, is_new_candidate,
    # carriageway_width_m — from the way's own ``width=`` / ``lanes=``
    # tags when mapped, else the per-type table, dem_cut_detected,
    # mouth_grade_m, bore_inward_pts, deck_reference_m — the last four
    # feed the DEM-cut light-touch mode, see TUNNEL_DEM_CUT_MIN_DROP_M).
    portal_data: list[tuple] = []
    # Rail tunnel lines, for the TWIN-corridor pairing below (user
    # 2026-07-04, KCLT: two parallel ``railway=rail`` tracks are one
    # double-track corridor — one wide bore, not two overlapping ones).
    _rail_tunnel_lines: dict = {}
    for _rw_id, _rw_refs, _rw_tags in ways_r:
        if (_rw_tags.get("tunnel") in PORTAL_TUNNEL_VALUES
                and _rw_tags.get("highway") is None
                and _rw_tags.get("railway") in RAIL_TUNNEL_TYPES):
            _rw_pts = [nodes_m[nn] for nn in _rw_refs if nn in nodes_m]
            if len(_rw_pts) >= 2:
                try:
                    _rail_tunnel_lines[_rw_id] = LineString(_rw_pts)
                except _GEOM_EXC:
                    continue

    _n_adj_skip = 0
    for tw_id, t_nrefs, t_tags in ways_r:
        if t_tags.get("tunnel") not in PORTAL_TUNNEL_VALUES:
            continue
        hw = t_tags.get("highway")
        if not _tunnelable(t_tags):
            continue
        if hw is None and t_tags.get("railway") in RAIL_TUNNEL_TYPES:
            # Pseudo-type so the width table can size rail bores
            # (10 m double-track vs the 22 m road default).  Never in
            # HW_TUNNEL_TYPES, so rail stays NEW-class for the gates.
            hw = "railway"
            _my_rail = _rail_tunnel_lines.get(tw_id)
            if _my_rail is not None:
                _twins = sorted(
                    _oid for _oid, _ol in _rail_tunnel_lines.items()
                    if _oid != tw_id
                    and _ol.distance(_my_rail) < TWIN_RAIL_NEAR_M)
                if _twins:
                    # Canonical member (smallest id) carries the ONE
                    # wide corridor bore; the twin's portals are
                    # suppressed entirely.
                    if str(tw_id) > min(str(tw_id),
                                        *[str(t) for t in _twins]):
                        if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                            print(f"    [tunnel-drop] way {tw_id}: "
                                  f"rail twin of {min(_twins)}")
                        continue
                    hw = "railway_twin"
        # OLD candidates (big_roads + highway type — the only ways the
        # emitter saw before 2026-06-12) keep the original behaviour
        # verbatim: no new gates (SPJC's user-approved tunnels emit
        # bit-identically).  NEW candidates (small_roads bores, rail)
        # carry the gates below — they widened the input enough to
        # surface the dead-boundary-gate strays at KPHL.
        _is_new_cand = not (tw_id in big_way_ids
                            and hw in HW_TUNNEL_TYPES)
        # Effective carriageway width: the way's own ``width=`` /
        # ``lanes=`` measurements beat the per-type table (user
        # 2026-07-16, EGPB).  Computed HERE — the only place the way's
        # tags are in hand — and carried through ``portal_data`` for
        # the cluster emit.
        way_carriage_w = _carriageway_width_from_tags(
            hw, t_tags, carriageway_width_m)
        if len(t_nrefs) < 2:
            continue
        # Skip OSM way IDs already handled by the through-
        # airport depressed-road emit (which produces a single
        # uniform depression instead of per-bridge ramps).
        if tw_id in excluded:
            continue
        # Skip tunnels running under / alongside other roads — their
        # surface walk traces a dense interchange whose ramps overlap
        # (user 2026-06-12, LMML).  Both portals are skipped.  The
        # verdict is SYSTEM-level (``_system_veto`` above): a clean
        # divided-highway underpass emits whole, a tangle stays out
        # whole (user 2026-07-04, CYUL runway-24 end).
        if system_veto.get(tw_id, False):
            _n_adj_skip += 1
            continue
        for portal_idx in (0, len(t_nrefs) - 1):
            portal_nid = t_nrefs[portal_idx]
            if portal_nid not in nodes_m:
                if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                    print(f"    [tunnel-drop] way {tw_id}: portal node "
                          f"{portal_nid} not in nodes_m")
                continue
            # Airport-proximity gate against the AIRSIDE PAVEMENT
            # union (user 2026-06-12, KPHL; ALL candidate classes
            # 2026-07-04): distant urban strays are skipped by their
            # distance to the airport's PAVEMENT.  The old
            # ROLE_BOUNDARY-distance gate is RETIRED: since the at-DEM
            # ribbon skip (2026-07-03) only a few ribbon scraps
            # survive, and at KDFW the 3 leftovers sat >1 km from the
            # central underpass corridor — the gate silently dropped
            # every portal of a 5x7 km airport's main tunnels.  The
            # pavement union always exists here and scales with the
            # airport.
            if airside_gate_union is not None:
                _ppx, _ppy = nodes_m[portal_nid]
                try:
                    if airside_gate_union.distance(
                            Point(_ppx, _ppy)) > max_boundary_dist_m:
                        if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                            print(f"    [tunnel-drop] way {tw_id} portal "
                                  f"({_ppx:.0f},{_ppy:.0f}): "
                                  f"{airside_gate_union.distance(Point(_ppx, _ppy)):.0f} m "
                                  f"from airside pavement")
                        continue
                except _GEOM_EXC:
                    pass
            walk = _walk_surface(portal_nid, tw_id, arm_walk_max_m,
                                 nodes_m, way_by_id, node_to_ways,
                                 way_carriage_w)
            if walk is None or len(walk) < 2:
                if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                    _px, _py = nodes_m[portal_nid]
                    print(f"    [tunnel-drop] way {tw_id} portal "
                          f"({_px:.0f},{_py:.0f}): no surface walk")
                continue
            # Merge very short consecutive segments so altitude
            # rounding to 0.1 m can't push the per-segment grade
            # above ``max_ramp_grade``.  At a 0.05 m worst-case
            # round-up, a 15 m segment rounds to ≤ 0.33 %/error.
            min_segment_m = 15.0
            merged: list[tuple[float, float]] = [walk[0]]
            for k in range(1, len(walk)):
                d = math.hypot(walk[k][0] - merged[-1][0],
                               walk[k][1] - merged[-1][1])
                if d < min_segment_m and k != len(walk) - 1:
                    continue
                merged.append(walk[k])
            walk = merged
            if len(walk) < 2:
                continue
            # Densify long segments so the visible ramp tracks the
            # road with multiple sloped pieces — user 2026-05-03
            # ("SW tunnel only has one ramp segment, should be
            # multiple following the road up to DEM elevation").
            # Sparse OSM ways often have ~150-200 m gaps between
            # nodes; without densification a 200 m approach renders
            # as a single straight ramp.  Target ~50 m segments.
            # Use ceil so segments never exceed ``target_seg_m``;
            # ``round`` would leave a 72 m gap as a single segment
            # (round(72/50) == 1) and the user noticed those single-
            # segment ramps don't track the road's grade closely.
            target_seg_m = 50.0
            densified: list[tuple[float, float]] = [walk[0]]
            for k in range(1, len(walk)):
                px, py = densified[-1]
                qx, qy = walk[k]
                d = math.hypot(qx - px, qy - py)
                n_sub = max(1, math.ceil(d / target_seg_m))
                for s in range(1, n_sub + 1):
                    t = s / n_sub
                    densified.append(
                        (px + t * (qx - px), py + t * (qy - py)))
            walk = densified
            portal_xy = walk[0]
            apt_elev = airport_elevation_at(*portal_xy)
            if apt_elev is None:
                if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                    print(f"    [tunnel-drop] way {tw_id} portal "
                          f"({portal_xy[0]:.0f},{portal_xy[1]:.0f}): "
                          f"no airport elevation")
                continue
            elev_low = apt_elev - tunnel_depth_m
            # Find the truncation point that keeps grade ≤
            # max_ramp_grade.  Walk along the polyline summing
            # cumulative distance; sample DEM at each vertex; the
            # required-length-so-far is (DEM - elev_low) /
            # max_ramp_grade.  Stop walking once the actual walk
            # length matches or exceeds that requirement (i.e. the
            # grade from portal to here is ≤ max_ramp_grade).
            cum = 0.0
            kept_pts: list[tuple[float, float]] = [walk[0]]
            grade_ok_at: float = 0.0  # cum dist where grade is OK
            for i in range(1, len(walk)):
                seg_len = math.hypot(
                    walk[i][0] - walk[i - 1][0],
                    walk[i][1] - walk[i - 1][1])
                cum += seg_len
                kept_pts.append(walk[i])
                try:
                    plat, plon = meters_to_lat_lon(*walk[i])
                    dem_h = _sample_dem(
                        dem, tile_lat, tile_lon, plat, plon)
                except _GEOM_EXC:
                    dem_h = None
                if dem_h is None:
                    continue
                drop = float(dem_h) - elev_low
                req = (drop / plan_grade if drop > 0 else 0.0)
                if cum >= req and cum >= ramp_min_length_m:
                    grade_ok_at = cum
                    break
                grade_ok_at = cum
            # Truncate walk to the OK length (or to last vertex
            # reached if we never satisfied the grade — best
            # effort, the resulting ramp will be the gentlest
            # achievable on the available roadway).
            walk = kept_pts
            far_xy = walk[-1]
            try:
                far_lat, far_lon = meters_to_lat_lon(*far_xy)
                far_dem = _sample_dem(
                    dem, tile_lat, tile_lon, far_lat, far_lon)
            except _GEOM_EXC:
                far_dem = None
            if far_dem is None:
                far_dem = apt_elev
            # If the resulting ramp would still violate grade
            # (DEM very high, road too short), cap far_dem at
            # elev_low + grade*length so we still emit something
            # sensible.  The visible top edge then sits LOWER
            # than DEM (subtle terrain dip) — preferable to a
            # spike or a mid-tunnel cap.
            max_drop = plan_grade * grade_ok_at
            if (far_dem - elev_low) > max_drop:
                far_dem = elev_low + max_drop
            # ── DEM-CUT DETECTION (user 2026-07-17, EGGW) ─────────
            # With a lidar elevation inset the bare-earth DEM already
            # carries the descending approach cut; when it does, the
            # cluster emit switches to the light-touch mode (face cap
            # + short mouth plate + roof cover, NO synthetic ramps or
            # wall chains — user: "we don't want a tunnel ramp
            # running around the entire parking garage").
            #
            # The signal is LOCAL RELIEF ACROSS THE ROAD — the trench
            # floor on the walk versus the deck BESIDE it at the same
            # station — never an absolute drop against ``apt_elev``:
            # ★``_airport_elevation_at`` falls back to the DEM at the
            # portal point when no boundary node is near (mid-field
            # tunnels), which samples the trench floor itself and
            # reads "no drop"; ★an absolute test against surrounding
            # ground also false-fires on any hillside bore whose DEM
            # legitimately has NO cut (KPHL class) and which still
            # needs the synthetic ramp.  ``mouth_grade`` = the DEM's
            # own road grade near the face (minimum inside the mouth
            # window — minimum, not first-sample, because smoothing
            # mixes the face pixels with the deck above).
            # ``deck_reference`` = the highest cross-road sample near
            # the portal: the grade the face cap and roof cover hold.
            # Fields ride at the END of the portal tuple (indices
            # 8-11) so every existing positional consumer (≤ 7) is
            # untouched.
            def _dem_at_local(px: float, py: float) -> float | None:
                try:
                    _plat, _plon = meters_to_lat_lon(px, py)
                    _value = _sample_dem(
                        dem, tile_lat, tile_lon, _plat, _plon)
                except _GEOM_EXC:
                    return None
                return None if _value is None else float(_value)

            _half_road = 0.5 * way_carriage_w
            _mouth_min: float | None = None
            _deck_reference: float | None = None
            _trench_depths: list[float] = []
            _cum2 = 0.0
            for i in range(len(walk)):
                if i > 0:
                    _cum2 += math.hypot(
                        walk[i][0] - walk[i - 1][0],
                        walk[i][1] - walk[i - 1][1])
                if _cum2 > TUNNEL_DEM_CUT_WINDOW_M:
                    break
                if i + 1 < len(walk):
                    _sdx = walk[i + 1][0] - walk[i][0]
                    _sdy = walk[i + 1][1] - walk[i][1]
                else:
                    _sdx = walk[i][0] - walk[i - 1][0]
                    _sdy = walk[i][1] - walk[i - 1][1]
                _slen = math.hypot(_sdx, _sdy) or 1.0
                _perp = (-_sdy / _slen, _sdx / _slen)
                _centre = _dem_at_local(*walk[i])
                if _centre is None:
                    continue
                _side_best: float | None = None
                for _offset in (_half_road + 6.0, _half_road + 15.0):
                    for _sign in (+1.0, -1.0):
                        _side = _dem_at_local(
                            walk[i][0] + _perp[0] * _offset * _sign,
                            walk[i][1] + _perp[1] * _offset * _sign)
                        if _side is not None and (
                                _side_best is None
                                or _side > _side_best):
                            _side_best = _side
                if _side_best is not None:
                    _trench_depths.append(_side_best - _centre)
                    if _cum2 <= TUNNEL_MOUTH_WINDOW_M and (
                            _deck_reference is None
                            or _side_best > _deck_reference):
                        _deck_reference = _side_best
                if _cum2 <= TUNNEL_MOUTH_WINDOW_M and (
                        _mouth_min is None or _centre < _mouth_min):
                    _mouth_min = _centre
            _median_depth: float | None = None
            if _trench_depths:
                _sorted_depths = sorted(_trench_depths)
                _median_depth = _sorted_depths[
                    len(_sorted_depths) // 2]
            cut_detected = (
                os.environ.get("O4_TUNNEL_DEM_CUT", "1") == "1"
                and _median_depth is not None
                and len(_trench_depths) >= 2
                and _median_depth >= TUNNEL_DEM_CUT_MIN_DROP_M)
            if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                print(f"    [tunnel-dem-probe] way {tw_id} portal "
                      f"({walk[0][0]:.0f},{walk[0][1]:.0f}): "
                      f"apt_elev={apt_elev:.2f} "
                      f"median_cross_road_depth={_median_depth} "
                      f"deck_reference={_deck_reference} "
                      f"mouth_min={_mouth_min} "
                      f"cut_detected={cut_detected}")
            mouth_grade = (_mouth_min if _mouth_min is not None
                           else apt_elev - tunnel_depth_m)
            # Bore polyline INWARD from this portal (for the roof
            # plates over the covered body), truncated at the bore
            # MIDPOINT — each end covers its own half with its own
            # local deck reference (★a full-bore plate from one end
            # lands the PARTNER cluster's head inside this cluster's
            # exclusion zone and silently drops it — measured EGGW
            # v4: "inside an emitted portal's exclusion zone" at the
            # south-west mouth) — and at the roof plate cap.
            _inward_refs = (t_nrefs if portal_idx == 0
                            else list(reversed(t_nrefs)))
            _bore_pts = [nodes_m[_nref] for _nref in _inward_refs
                         if _nref in nodes_m]
            _bore_length = sum(
                math.hypot(_bore_pts[i + 1][0] - _bore_pts[i][0],
                           _bore_pts[i + 1][1] - _bore_pts[i][1])
                for i in range(len(_bore_pts) - 1))
            _roof_limit = min(TUNNEL_ROOF_PLATE_MAX_LENGTH_M,
                              0.5 * _bore_length + 0.5)
            inward_pts: list[tuple[float, float]] = []
            _cum3 = 0.0
            for _pt in _bore_pts:
                if inward_pts:
                    _segment = math.hypot(
                        _pt[0] - inward_pts[-1][0],
                        _pt[1] - inward_pts[-1][1])
                    if _cum3 + _segment > _roof_limit:
                        _fraction = ((_roof_limit - _cum3) / _segment
                                     if _segment > 1e-9 else 0.0)
                        inward_pts.append((
                            inward_pts[-1][0]
                            + (_pt[0] - inward_pts[-1][0]) * _fraction,
                            inward_pts[-1][1]
                            + (_pt[1] - inward_pts[-1][1]) * _fraction))
                        break
                    _cum3 += _segment
                inward_pts.append(_pt)
            # Deck grade at the INNER (pavement-side) end of the roof
            # cover — the highest cross-bore sample there.  The roof
            # plate GRADES from this at its inner end down to the
            # face-local ``deck_reference`` at the mouth (user
            # 2026-07-17: "a clean wall that grades the terrain from
            # the taxiway flat over to the tunnel portal") — a flat
            # plate at the face grade left a ~3 m scarp against the
            # taxiway edge (measured EGGW v5: 154.8 plate against
            # 157.6 pavement).
            _inner_deck: float | None = None
            if len(inward_pts) >= 2:
                _idx = inward_pts[-1][0] - inward_pts[-2][0]
                _idy = inward_pts[-1][1] - inward_pts[-2][1]
                _ilen = math.hypot(_idx, _idy) or 1.0
                _iperp = (-_idy / _ilen, _idx / _ilen)
                for _offset in (_half_road + 6.0, _half_road + 15.0):
                    for _sign in (+1.0, -1.0):
                        _side = _dem_at_local(
                            inward_pts[-1][0]
                            + _iperp[0] * _offset * _sign,
                            inward_pts[-1][1]
                            + _iperp[1] * _offset * _sign)
                        if _side is not None and (
                                _inner_deck is None
                                or _side > _inner_deck):
                            _inner_deck = _side
            portal_data.append(
                (portal_nid, tw_id, walk, hw,
                 float(apt_elev), float(far_dem), _is_new_cand,
                 float(way_carriage_w), bool(cut_detected),
                 float(mouth_grade), inward_pts,
                 (float(_deck_reference)
                  if _deck_reference is not None else None),
                 (float(_inner_deck)
                  if _inner_deck is not None else None)))
    if _n_adj_skip:
        try:
            UI.vprint(1,
                f"  [pav-builder] skipped {_n_adj_skip} tunnel(s) with "
                f"an adjacent/crossing road (ramps not modelled).")
        except _GEOM_EXC:
            pass
    if os.environ.get("O4_TUNNEL_DEBUG") == "1":
        print(f"    [tunnel-portals] {len(portal_data)} portal walk(s) "
              f"built")
    return portal_data


def _dedup_portal_walks(portal_data: list) -> list:
    """Drop portals whose surface walk substantially overlaps a
    kept walk from another way (see WALK DEDUP comment below).
    """
    # WALK DEDUP (user 2026-07-04): twin carriageways that MERGE beyond
    # their portals give two portals the SAME surface walk — two ramps
    # emitted on one stretch of road, one per bore profile (LMML:
    # coincident tunnel_ramp pieces 4.3 m apart in z).  Drop a portal
    # whose walk substantially overlaps a kept one from ANOTHER way.
    # The 4 m buffer keeps genuinely PARALLEL twin walks (≥ 8 m apart —
    # each carriageway its own ramp / merged by the portal clustering
    # below) while catching same-road walks at ~0 m.  A way's own two
    # portals never dedup (opposite tunnel mouths, user 2026-05-03).
    _kept_portals: list = []
    _kept_walk_lines: list = []       # (LineString, tw_id)
    for _pd in portal_data:
        _walk_pts = _pd[2]
        _wline = (LineString(_walk_pts) if len(_walk_pts) >= 2 else None)
        _dup = False
        if _wline is not None:
            for (_kl, _kw) in _kept_walk_lines:
                if _kw == _pd[1]:
                    continue
                try:
                    _ov = _wline.buffer(4.0).intersection(_kl).length
                    if _ov > 0.5 * min(_wline.length, _kl.length):
                        _dup = True
                        break
                except _GEOM_EXC:
                    continue
        if _dup:
            if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                print(f"    [tunnel-walk-dedup] dropped portal of way "
                      f"{_pd[1]} (walk overlaps a kept ramp)")
            continue
        _kept_portals.append(_pd)
        if _wline is not None:
            _kept_walk_lines.append((_wline, _pd[1]))
    return _kept_portals


def _cluster_portals(portal_data: list, nodes_m: dict,
                     portal_cluster_dist_m: float
                     ) -> list[list[int]]:
    """Group portals into combined-entrance clusters by portal-
    node proximity.  Returns index lists into ``portal_data``.
    """
    # Cluster portals by node-coord proximity (divided highways
    # with two parallel carriageways have a portal per carriageway
    # at each tunnel end; cluster them so we emit one combined
    # entrance per end).
    #
    # Per user 2026-05-03: do NOT cluster the two portals of the
    # SAME tunnel way (a short tunnel ≤ ``portal_cluster_dist_m``
    # long has both ends within cluster distance, but they're the
    # OPPOSITE ends of the same tunnel — emitting only one cluster
    # would skip one tunnel mouth, which is what was happening on
    # the small 33 m secondary tunnel south of Terminal 2).
    clusters: list[list[int]] = []
    used: set = set()
    for i in range(len(portal_data)):
        if i in used:
            continue
        wid_i = portal_data[i][1]
        nid_i = portal_data[i][0]
        cl = [i]
        used.add(i)
        pi = nodes_m[nid_i]
        for j in range(i + 1, len(portal_data)):
            if j in used:
                continue
            wid_j = portal_data[j][1]
            if wid_j == wid_i:
                # Same tunnel way — two ends, do not cluster.
                continue
            pj = nodes_m[portal_data[j][0]]
            if (math.hypot(pi[0] - pj[0], pi[1] - pj[1])
                    < portal_cluster_dist_m):
                cl.append(j)
                used.add(j)
        clusters.append(cl)
    return clusters


def _emit_portal_cluster(
        cl: list[int], portal_data: list, nodes_m: dict,
        layout: "PavementLayout", exclusion_zones: list,
        carriageway_width_m: float, tunnel_depth_m: float,
        wall_gap_m: float, retaining_wall_width_m: float,
        half_wall_w: float, dem_at,
        airside_gate_union=None) -> int:
    """Emit one portal cluster's cap + arm walls + ramp chain
    (plus fork throat / perimeter wall band when gated on).
    Appends the emitted footprints to ``exclusion_zones``.
    Returns 1 when the cluster emitted, 0 when skipped.
    """
    # All portals in cluster share approximately the same
    # location.  Use the first portal's walk as the canonical
    # arm path; combine widths for divided highways.
    head = portal_data[cl[0]]
    # Slice, never destructure whole: the portal tuple grew DEM-cut
    # fields at indices 8-10 (2026-07-17) and may grow again.
    (portal_nid, _wid_unused, walk_pts, hw_type, apt_elev,
     far_dem, _head_new, head_carriage_w) = head[:8]
    _cl_all_new = all(portal_data[k][6] for k in cl)
    if len(walk_pts) < 2:
        return 0
    # Carriageway width from the way's own ``width=`` / ``lanes=``
    # tags when mapped, else the per-OSM-highway-type table (user
    # 2026-05-03 / 2026-07-16) — computed in ``_gather_portal_walks``
    # and carried per portal.
    carriage_w = head_carriage_w
    half_carriage = 0.5 * carriage_w
    elev_low = apt_elev - tunnel_depth_m
    elev_high = far_dem
    # Compute the walk's cumulative distance for elevation
    # interpolation.
    cum_dists = [0.0]
    for i in range(1, len(walk_pts)):
        cum_dists.append(cum_dists[-1] + math.hypot(
            walk_pts[i][0] - walk_pts[i - 1][0],
            walk_pts[i][1] - walk_pts[i - 1][1]))
    total_walk = cum_dists[-1]
    if total_walk < 5.0:
        if os.environ.get("O4_TUNNEL_DEBUG") == "1":
            print(f"    [tunnel-drop] cluster at "
                  f"({walk_pts[0][0]:.0f},{walk_pts[0][1]:.0f}): "
                  f"walk only {total_walk:.1f} m")
        return 0
    # Cluster spread for combined width: project each cluster
    # member's portal node onto the perpendicular at the head
    # portal.  Cap, arms and ramps are all centred on the
    # cluster centroid (midpoint between carriageway portals),
    # not on the head portal — user 2026-05-03 ("trunk highway
    # tunnels not centered on OSM ways, offset with one edge on
    # one of the ways").  We apply a constant translation to
    # ``walk_pts`` so every downstream geometry inherits the
    # centring; this keeps the cap and ramps coplanar across
    # both carriageways of a divided highway.  Constant shift
    # is exact at the portal and stays close-to-correct for the
    # length of the walk because parallel carriageways follow
    # parallel curves.
    first_seg = (walk_pts[1][0] - walk_pts[0][0],
                 walk_pts[1][1] - walk_pts[0][1])
    first_len = math.hypot(*first_seg)
    if first_len < 0.1:
        return 0
    first_dir = (first_seg[0] / first_len,
                 first_seg[1] / first_len)
    first_perp = (-first_dir[1], first_dir[0])
    # Project each member's portal node onto the perpendicular AND
    # carry its own carriageway half-width, so the combined bore spans
    # from the leftmost member's OUTER edge to the rightmost member's
    # OUTER edge — covering the whole tunnel mouth even when the
    # members differ in width (e.g. a 9 m road + a 5 m rail).  Using
    # only the head member's half-width left the bore short on the
    # wider member's side (user 2026-06-13).
    spans = []          # centre-projection per member (for divergence)
    _edges = []         # (outer_left, outer_right) per member
    for k in cl:
        ni = portal_data[k][0]
        if ni not in nodes_m:
            continue
        p = nodes_m[ni]
        proj = ((p[0] - walk_pts[0][0]) * first_perp[0]
                + (p[1] - walk_pts[0][1]) * first_perp[1])
        half_k = 0.5 * portal_data[k][7]
        spans.append(proj)
        _edges.append((proj - half_k, proj + half_k))
    cluster_span = max(spans) - min(spans) if spans else 0.0
    if _edges:
        _ml = min(e[0] for e in _edges)
        _mr = max(e[1] for e in _edges)
        cluster_perp_offset = 0.5 * (_ml + _mr)
        combined_half = 0.5 * (_mr - _ml)
    else:
        cluster_perp_offset = 0.0
        combined_half = half_carriage
    if abs(cluster_perp_offset) > 1e-6:
        shift_x = first_perp[0] * cluster_perp_offset
        shift_y = first_perp[1] * cluster_perp_offset
        walk_pts = [(p[0] + shift_x, p[1] + shift_y)
                    for p in walk_pts]

    def _build_wall_segment(p_a: tuple[float, float],
                             p_b: tuple[float, float],
                             perp_off: float
                             ) -> Polygon | None:
        """4-corner wall polygon parallel to segment ``a-b``,
        offset by ``perp_off`` from the segment's centre line,
        ``retaining_wall_width_m`` thick."""
        seg = (p_b[0] - p_a[0], p_b[1] - p_a[1])
        slen = math.hypot(*seg)
        if slen < 0.1:
            return None
        ux, uy = seg[0] / slen, seg[1] / slen
        nx, ny = -uy, ux
        # Sign of perp_off picks which side.  Inner edge of
        # wall is at perp_off, outer edge at perp_off ± width.
        inner = perp_off
        outer = (perp_off + half_wall_w * 2.0
                 if perp_off >= 0
                 else perp_off - half_wall_w * 2.0)
        corners = [
            (p_a[0] + nx * inner, p_a[1] + ny * inner),
            (p_b[0] + nx * inner, p_b[1] + ny * inner),
            (p_b[0] + nx * outer, p_b[1] + ny * outer),
            (p_a[0] + nx * outer, p_a[1] + ny * outer),
        ]
        try:
            p = Polygon(corners)
            if not p.is_valid:
                p = p.buffer(0)
            if p.geom_type == "Polygon" and not p.is_empty:
                return p
        except _GEOM_EXC:
            return None
        return None
    # AIRSIDE / DOUBLE-EMIT GATE (user 2026-06-12, KPHL): with
    # small_roads + railways feeding this emitter, service-tunnel
    # bores UNDER the apron/terminal complex now qualify — but a
    # tunnel under solid pavement has no visible ramp to model
    # (the surface above it is the graded apron).  The
    # discriminator is the RAMP, not the portal: a legitimate
    # portal's ramp leads AWAY from pavement (SPJC's runway
    # tunnels: portals at the pavement FACE, ramps off-airport),
    # a buried bore's ramp stays ON it (KPHL terminal-area
    # service tunnels).  Skip when more than half the ramp walk
    # runs over airside pavement; also skip a cap landing inside
    # an already-emitted portal's footprint (road and rail bores
    # of one tunnel cluster emitting twice).
    try:
        if _cl_all_new and exclusion_zones:
            if unary_union(exclusion_zones).buffer(2.0).contains(
                    Point(walk_pts[0])):
                if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                    print(f"    [tunnel-drop] cluster at "
                          f"({walk_pts[0][0]:.0f},"
                          f"{walk_pts[0][1]:.0f}): inside an "
                          f"emitted portal's exclusion zone")
                return 0
    except _GEOM_EXC:
        pass
    # 1) Cap wall AT the portal cluster's centroid, perpendicular
    #    to the first segment.  The cap's centre line passes
    #    through the cluster centroid (so divided-highway
    #    tunnels are centered between the carriageways, user
    #    2026-05-03), its width spans the combined carriageways
    #    + 2 × wall_gap, its thickness is
    #    retaining_wall_width_m.
    # Index of the first shape THIS cluster emits — the gate-on
    # perimeter wall band (emitted at cluster end) unions every
    # tunnel_ramp from here on.
    _cl_start_idx = len(layout.shapes)
    # Far (surface) end of every ramp arm this cluster emits —
    # the perimeter wall band must be cut OPEN there (the road
    # continues at grade; a band crossing it walls off the live
    # roadway).  (endpoint, previous point, half width) per arm.
    _cl_arm_ends: list = []
    cap_half_len = combined_half + wall_gap_m
    cap_centre = walk_pts[0]
    c0 = (cap_centre[0] + first_perp[0] * cap_half_len,
          cap_centre[1] + first_perp[1] * cap_half_len)
    c1 = (cap_centre[0] - first_perp[0] * cap_half_len,
          cap_centre[1] - first_perp[1] * cap_half_len)
    # Move cap thickness INTO the tunnel direction (negative
    # first_dir) — cap occupies the strip from portal back
    # by retaining_wall_width_m.
    c0_back = (c0[0] - first_dir[0] * retaining_wall_width_m,
               c0[1] - first_dir[1] * retaining_wall_width_m)
    c1_back = (c1[0] - first_dir[0] * retaining_wall_width_m,
               c1[1] - first_dir[1] * retaining_wall_width_m)
    # ── DEM-CUT MODE (user 2026-07-17, EGGW): the mesh already has
    # the ramps.  When every member's walk found the approach cut
    # already carved in the DEM (lidar bare-earth inset), synthetic
    # ramps/walls/throat would fight the real — often steeper —
    # lidar profile, so emit only the pieces the bare-earth model
    # CANNOT supply: the portal face cap at airport grade (seats a
    # portal object whose anchor sits on the face at DECK grade), a
    # short mouth plate at the DEM's own road grade (keeps the face
    # wall crisp instead of a Triangle smear), and flat roof plates
    # at airport grade over the covered bore between the face and
    # the airside pavement (a bare-earth model strips the structure
    # above the bore, leaving an open trench that pavement grading
    # alone does not fill).
    if all(len(portal_data[k]) > 11 and portal_data[k][8]
           for k in cl):
        emitted_any = False
        # The grade the face cap holds: the measured cross-road deck
        # beside the trench (index 11) — never ``apt_elev``, whose
        # mid-field fallback samples the trench floor itself.
        deck_grade = max(
            (portal_data[k][11] for k in cl
             if portal_data[k][11] is not None),
            default=apt_elev)
        mouth_grade = min(
            (portal_data[k][9] for k in cl
             if portal_data[k][9] is not None),
            default=apt_elev - tunnel_depth_m)
        # 1) GRADED roof cover per member, emitted FIRST: a chain of
        #    quads along each carriageway's own bore from ITS mapped
        #    face to the airside pavement edge (upstream truncation at
        #    the bore midpoint keeps it off the partner's half), every
        #    corner carrying its own ABSOLUTE altitude (user
        #    2026-07-17: per-corner values — the sloped high/low rect
        #    encoding is 4-corner-fragile).  Corner altitudes lerp
        #    from face-top grade to the pavement-seam deck.  Quads
        #    share corners only with each other at equal values, so
        #    node-bucket sharing stays value-consistent (★arbitrary
        #    difference polygons instead LOSE per-node values to
        #    foreign ways' buckets — measured EGGW v6).
        roof_polygons: list = []
        for k in cl:
            bore_pts = portal_data[k][10]
            if not bore_pts or len(bore_pts) < 2:
                continue
            face_deck = (portal_data[k][11]
                         if portal_data[k][11] is not None
                         else deck_grade)
            inner_deck = (portal_data[k][12]
                          if len(portal_data[k]) > 12
                          and portal_data[k][12] is not None
                          else face_deck)
            try:
                bore_line = LineString(bore_pts)
                clear_line = bore_line
                if airside_gate_union is not None:
                    clipped = bore_line.difference(airside_gate_union)
                    pieces = [g for g in getattr(
                        clipped, "geoms", [clipped])
                        if g.geom_type == "LineString"
                        and g.length > 1.0]
                    # Only the piece CONTAINING the face end is the
                    # roof strip; when the pavement reaches (almost)
                    # to the face there is nothing to cover — never
                    # fall back to a beyond-pavement piece (★measured
                    # EGGW v9: nearest-piece selection wandered to a
                    # mid-body segment INSIDE the crossing and graded
                    # quads over the taxiway).
                    face_point = Point(bore_pts[0])
                    containing = [g for g in pieces
                                  if g.distance(face_point) < 1.0]
                    if containing:
                        clear_line = containing[0]
                    else:
                        continue
            except _GEOM_EXC:
                continue
            clear_length = clear_line.length
            if clear_length < 2.0:
                continue
            # Refine the inner deck at the strip's actual far end
            # (the pavement seam) when the DEM offers a sample there.
            try:
                far_point = clear_line.interpolate(clear_length)
                near_point = clear_line.interpolate(
                    max(0.0, clear_length - 5.0))
                _fdx = far_point.x - near_point.x
                _fdy = far_point.y - near_point.y
                _flen = math.hypot(_fdx, _fdy) or 1.0
                _fperp = (-_fdy / _flen, _fdx / _flen)
                half_member = 0.5 * portal_data[k][7]
                for _offset in (half_member + 6.0, half_member + 15.0):
                    for _sign in (+1.0, -1.0):
                        _side = dem_at(
                            far_point.x + _fperp[0] * _offset * _sign,
                            far_point.y + _fperp[1] * _offset * _sign)
                        if _side is not None and _side > inner_deck:
                            inner_deck = float(_side)
            except _GEOM_EXC:
                pass
            half_roof = 0.5 * portal_data[k][7] + wall_gap_m
            target_quad_m = 12.0
            quad_count = max(1, math.ceil(clear_length / target_quad_m))
            for quad_index in range(quad_count):
                s_low = clear_length * quad_index / quad_count
                s_high = clear_length * (quad_index + 1) / quad_count
                try:
                    p_low = clear_line.interpolate(s_low)
                    p_high = clear_line.interpolate(s_high)
                except _GEOM_EXC:
                    continue
                _qdx = p_high.x - p_low.x
                _qdy = p_high.y - p_low.y
                _qlen = math.hypot(_qdx, _qdy)
                if _qlen < 0.5:
                    continue
                _qperp = (-_qdy / _qlen, _qdx / _qlen)
                elevation_low = round(
                    face_deck + (inner_deck - face_deck)
                    * (s_low / clear_length), 2)
                elevation_high = round(
                    face_deck + (inner_deck - face_deck)
                    * (s_high / clear_length), 2)
                corners = [
                    (p_high.x + _qperp[0] * half_roof,
                     p_high.y + _qperp[1] * half_roof),
                    (p_low.x + _qperp[0] * half_roof,
                     p_low.y + _qperp[1] * half_roof),
                    (p_low.x - _qperp[0] * half_roof,
                     p_low.y - _qperp[1] * half_roof),
                    (p_high.x - _qperp[0] * half_roof,
                     p_high.y - _qperp[1] * half_roof),
                ]
                try:
                    quad = Polygon(corners)
                    if not quad.is_valid or quad.is_empty \
                            or quad.area < 2.0:
                        continue
                except _GEOM_EXC:
                    continue
                # 4-corner high/low encoding (corners 0,3 = high /
                # 1,2 = low).  ★Per-corner ``node_altitudes`` on
                # these post-solve quads measurably LOSES most values
                # between emission and the written patch (EGGW v9:
                # ways shipped with alt_abs on 2 of 6 nodes and the
                # strip collapsed toward the trench) — mechanism not
                # yet root-caused; until it is, the high/low pair is
                # the encoding that demonstrably reaches the mesh.
                if abs(elevation_high - elevation_low) >= 0.1:
                    layout.shapes.append(BuiltShape(
                        polygon=quad,
                        role=ROLE_RETAINING_WALL,
                        ref="tunnel_roof",
                        altitude_high=elevation_high,
                        altitude_low=elevation_low))
                else:
                    layout.shapes.append(BuiltShape(
                        polygon=quad,
                        role=ROLE_RETAINING_WALL,
                        ref="tunnel_roof",
                        altitude=round(0.5 * (
                            elevation_high + elevation_low), 2)))
                roof_polygons.append(quad)
                exclusion_zones.append(quad)
                emitted_any = True
        try:
            roof_union = (unary_union(roof_polygons)
                          if roof_polygons else None)
        except _GEOM_EXC:
            roof_union = None
        # 2) Face cap: a flat bar at the deck grade across the
        #    combined carriageways at the cluster face line.  Overlap
        #    with the roof cover is at the SAME grade — benign.
        try:
            cap_poly = Polygon([c0, c1, c1_back, c0_back])
            if not cap_poly.is_valid:
                cap_poly = cap_poly.buffer(0)
            if (cap_poly.geom_type == "Polygon"
                    and not cap_poly.is_empty):
                layout.shapes.append(BuiltShape(
                    polygon=cap_poly,
                    role=ROLE_RETAINING_WALL,
                    ref="tunnel_cap",
                    altitude=round(deck_grade, 1)))
                exclusion_zones.append(cap_poly)
                emitted_any = True
        except _GEOM_EXC:
            pass
        # 3) Mouth plate at the DEM's own road grade, MINUS the roof
        #    cover: with staggered twin-carriageway mapped ends the
        #    cluster-wide rect crosses the more-recessed member's
        #    bore, where the roof (real structure at deck grade) must
        #    win (measured EGGW v7: 28.7 m² of road-grade plate under
        #    the partner's roof).  A flat plate tolerates any ring
        #    shape, so the difference result ships as-is.
        try:
            _mouth_near = wall_gap_m
            _mouth_far = wall_gap_m + TUNNEL_MOUTH_PLATE_LENGTH_M
            _mc = cap_centre
            _m0 = (_mc[0] + first_dir[0] * _mouth_near,
                   _mc[1] + first_dir[1] * _mouth_near)
            _m1 = (_mc[0] + first_dir[0] * _mouth_far,
                   _mc[1] + first_dir[1] * _mouth_far)
            mouth_geometry = Polygon([
                (_m0[0] + first_perp[0] * combined_half,
                 _m0[1] + first_perp[1] * combined_half),
                (_m1[0] + first_perp[0] * combined_half,
                 _m1[1] + first_perp[1] * combined_half),
                (_m1[0] - first_perp[0] * combined_half,
                 _m1[1] - first_perp[1] * combined_half),
                (_m0[0] - first_perp[0] * combined_half,
                 _m0[1] - first_perp[1] * combined_half),
            ])
            if not mouth_geometry.is_valid:
                mouth_geometry = mouth_geometry.buffer(0)
            if roof_union is not None and not mouth_geometry.is_empty:
                mouth_geometry = mouth_geometry.difference(roof_union)
            for part in getattr(
                    mouth_geometry, "geoms", [mouth_geometry]):
                if (part.geom_type != "Polygon" or part.is_empty
                        or part.area < 1.0):
                    continue
                layout.shapes.append(BuiltShape(
                    polygon=part,
                    role=ROLE_TUNNEL_RAMP,
                    ref="tunnel_mouth",
                    altitude=round(mouth_grade, 2)))
                exclusion_zones.append(part)
                emitted_any = True
        except _GEOM_EXC:
            pass
        if os.environ.get("O4_TUNNEL_DEBUG") == "1":
            print(f"    [tunnel-dem-cut] cluster at "
                  f"({walk_pts[0][0]:.0f},{walk_pts[0][1]:.0f}): "
                  f"cap+roof@{deck_grade:.1f}, mouth@{mouth_grade:.1f}, "
                  f"no synthetic ramps (DEM cut present)")
        return 1 if emitted_any else 0
    # Gate ON folds the cap into the continuous perimeter wall band
    # (which wraps the portal end too); gate OFF keeps the separate
    # flat cap.
    if not TUNNEL_FORK_THROAT:
        try:
            cap_poly = Polygon([c0, c1, c1_back, c0_back])
            if not cap_poly.is_valid:
                cap_poly = cap_poly.buffer(0)
            if (cap_poly.geom_type == "Polygon"
                    and not cap_poly.is_empty):
                layout.shapes.append(BuiltShape(
                    polygon=cap_poly,
                    role=ROLE_RETAINING_WALL,
                    ref="tunnel_cap",
                    altitude=round(apt_elev, 1)))
                exclusion_zones.append(cap_poly)
        except _GEOM_EXC:
            pass
    # Per user 2026-05-04: the cap + arm walls form a continuous
    # "U" — arms touch the cap on both sides (their inner-front
    # corner sits exactly at the cap's outer-front corner, since
    # ``cap_half_len`` and ``arm_off - half_wall_w`` both equal
    # ``combined_half + wall_gap_m``).  Only the RAMP starts
    # ``wall_gap_m`` further into the tunnel so its near edge
    # (lowest elevation) leaves the same clearance from the cap
    # as it already does from the side walls.  We achieve that
    # by offsetting the FIRST ramp segment's near corners
    # individually below; ``walk_pts`` itself stays at the portal.

    # Y-split throat polygons emitted for THIS cluster: a branch ramp
    # segment mostly covered by the throat is redundant pavement at
    # the same elevation — skip it rather than emit an overlapping
    # sloped rect (user 2026-07-04: ramps must not overlap; a sloped
    # ``altitude_high/low`` rect cannot be clipped without breaking
    # its two-corner elevation semantics).
    cluster_throat_polys: list = []

    def _emit_chain(chain_pts, chain_half, e_lo_c, e_hi_c,
                    cap_gap):
        """Emit arm walls + ramp chain along ``chain_pts`` at
        half-width ``chain_half``, elevations linear from
        ``e_lo_c`` (start) to ``e_hi_c`` (end).  ``cap_gap``
        applies the first-segment wall_gap offset (the chain
        abuts the portal cap).  Extracted verbatim from the
        single-ramp emit so the parallel-bores path is
        unchanged; the Y-split calls it once for the shared
        throat and once per diverging branch (user 2026-06-12,
        KPHL RWY 26 north portal: road+rail share the tunnel,
        then fork right outside — the ramp must fork too)."""
        n_c = len(chain_pts)
        if n_c < 2:
            return
        c_cums = [0.0]
        for i in range(1, n_c):
            c_cums.append(c_cums[-1] + math.hypot(
                chain_pts[i][0] - chain_pts[i - 1][0],
                chain_pts[i][1] - chain_pts[i - 1][1]))
        c_total = c_cums[-1]
        if c_total < 1.0:
            return
        c_first = (chain_pts[1][0] - chain_pts[0][0],
                   chain_pts[1][1] - chain_pts[0][1])
        c_first_len = math.hypot(*c_first)
        if c_first_len < 0.1:
            return
        c_first_dir = (c_first[0] / c_first_len,
                       c_first[1] / c_first_len)
        arm_off = chain_half + wall_gap_m + half_wall_w
        verts_perp = []
        verts_scale = []
        for i in range(n_c):
            if i == 0:
                s = (chain_pts[1][0] - chain_pts[0][0],
                     chain_pts[1][1] - chain_pts[0][1])
                sl = math.hypot(*s)
                verts_perp.append((-s[1] / sl, s[0] / sl))
                verts_scale.append(1.0)
            elif i == n_c - 1:
                s = (chain_pts[i][0] - chain_pts[i - 1][0],
                     chain_pts[i][1] - chain_pts[i - 1][1])
                sl = math.hypot(*s)
                verts_perp.append((-s[1] / sl, s[0] / sl))
                verts_scale.append(1.0)
            else:
                s1 = (chain_pts[i][0] - chain_pts[i - 1][0],
                      chain_pts[i][1] - chain_pts[i - 1][1])
                s2 = (chain_pts[i + 1][0] - chain_pts[i][0],
                      chain_pts[i + 1][1] - chain_pts[i][1])
                l1 = math.hypot(*s1)
                l2 = math.hypot(*s2)
                u1 = (s1[0] / l1, s1[1] / l1)
                u2 = (s2[0] / l2, s2[1] / l2)
                avg = ((u1[0] + u2[0]) / 2.0,
                       (u1[1] + u2[1]) / 2.0)
                al = math.hypot(*avg)
                if al < 1e-6:
                    verts_perp.append((-u1[1], u1[0]))
                    verts_scale.append(1.0)
                    continue
                tangent = (avg[0] / al, avg[1] / al)
                perp = (-tangent[1], tangent[0])
                dot = u1[0] * u2[0] + u1[1] * u2[1]
                cos_half = max(0.1, math.sqrt(
                    max(0.0, (1.0 + dot) / 2.0)))
                verts_perp.append(perp)
                verts_scale.append(1.0 / cos_half)

        def _vertex_offset(idx, off):
            px, py = chain_pts[idx]
            nx, ny = verts_perp[idx]
            scaled = off * verts_scale[idx]
            return (px + nx * scaled, py + ny * scaled)

        # Station elevations lerp over EFFECTIVE cumulative length: each
        # segment weighs min(centerline, both road-edge lengths).  On a
        # bend the miter join shortens the INNER quad edge below the
        # centerline arc, so a centerline-proportional Δe read as an
        # over-cap grade along that edge (SPJC 2026-07-06: 0.70 m over a
        # 15.25 m inner edge on a ~20 m segment = 4.59 % vs the 4 % ramp
        # law).  Weighting by the shortest edge caps every quad-edge
        # grade at ~total_de/Σeffective, which the walk sizing keeps at
        # the plan grade (the safety margin absorbs the tiny Σ shrink).
        effective_cums = [0.0]
        for i in range(n_c - 1):
            seg_len = c_cums[i + 1] - c_cums[i]
            edge_plus = math.dist(_vertex_offset(i, +chain_half),
                                  _vertex_offset(i + 1, +chain_half))
            edge_minus = math.dist(_vertex_offset(i, -chain_half),
                                   _vertex_offset(i + 1, -chain_half))
            effective_cums.append(
                effective_cums[-1]
                + min(seg_len, edge_plus, edge_minus))
        effective_total = effective_cums[-1]
        if effective_total < 1.0:
            return
        # EFFECTIVE-SPACE GRADE CLAMP (2026-07-17, SPJC #499/#500/#502):
        # the walk TRUNCATION sizes the chain on CENTERLINE length
        # (drop / plan_grade), but elevations lerp over the EFFECTIVE
        # (min-edge) length — on a curving chain the miter-shortened
        # edges shrink Σeffective 15-20 % below the centerline sum, so
        # every quad edge realized drop/Σeffective = plan × (Σcenter /
        # Σeffective) = 4.13-4.21 % against the 4 % ramp law (the
        # 0.5 pp safety margin only covers rounding).  Clamp the
        # chain-top elevation so the effective-space grade never
        # exceeds the plan grade; the ramp top then sits slightly
        # below the outside DEM — the same accepted "subtle terrain
        # dip" trade the walk gatherer already makes when the roadway
        # is too short (its far_dem cap).
        if e_hi_c > e_lo_c:
            plan_grade_local = max(
                float(_CFG.TUNNEL_RAMP_MAX_GRADE)
                - TUNNEL_RAMP_GRADE_SAFETY_MARGIN, 1e-3)
            maximum_effective_drop = plan_grade_local * effective_total
            if (e_hi_c - e_lo_c) > maximum_effective_drop:
                e_hi_c = e_lo_c + maximum_effective_drop

        for i in range(n_c - 1):
            p_a = chain_pts[i]
            p_b = chain_pts[i + 1]
            d_a = c_cums[i]
            d_b = c_cums[i + 1]
            seg_len = d_b - d_a
            if seg_len < 0.5:
                continue
            frac_a = effective_cums[i] / effective_total
            frac_b = effective_cums[i + 1] / effective_total
            e_a = (1 - frac_a) * e_lo_c + frac_a * e_hi_c
            e_b = (1 - frac_b) * e_lo_c + frac_b * e_hi_c
            # Legacy per-segment flat walls (gate OFF only — byte-
            # identical to the pre-2026-06-13 behaviour).  Gate ON
            # traces ONE continuous DEM-following wall band around the
            # whole cluster ramp union after all ramps are emitted
            # (see ``_emit_perimeter_wall``), so per-segment walls are
            # skipped here.
            wall_top = apt_elev
            wall_thresh = wall_top - 0.05
            seg_e_lo = min(e_a, e_b)
            seg_e_hi = max(e_a, e_b)
            if TUNNEL_FORK_THROAT or seg_e_lo >= wall_thresh:
                pass
            else:
                if seg_e_hi > wall_thresh \
                        and abs(e_b - e_a) > 1e-3:
                    frac_cross = (
                        (wall_thresh - e_a) / (e_b - e_a))
                    frac_cross = max(0.0, min(1.0, frac_cross))
                else:
                    frac_cross = 1.0
                pa = chain_pts[i]
                pb = chain_pts[i + 1]
                cross = (
                    pa[0] + frac_cross * (pb[0] - pa[0]),
                    pa[1] + frac_cross * (pb[1] - pa[1]))
                for sign in (+1, -1):
                    inner = sign * (arm_off - half_wall_w)
                    outer = sign * (arm_off + half_wall_w)
                    ai = _vertex_offset(i, inner)
                    ao = _vertex_offset(i, outer)
                    if frac_cross >= 0.999:
                        bi = _vertex_offset(i + 1, inner)
                        bo = _vertex_offset(i + 1, outer)
                    else:
                        sx, sy = (pb[0] - pa[0], pb[1] - pa[1])
                        sl = math.hypot(sx, sy) or 1.0
                        nx, ny = -sy / sl, sx / sl
                        bi = (cross[0] + nx * inner,
                              cross[1] + ny * inner)
                        bo = (cross[0] + nx * outer,
                              cross[1] + ny * outer)
                    try:
                        wp = Polygon([ai, bi, bo, ao])
                        if not wp.is_valid:
                            wp = wp.buffer(0)
                        if (wp.geom_type == "Polygon"
                                and not wp.is_empty
                                and wp.area > 0.5):
                            layout.shapes.append(BuiltShape(
                                polygon=wp,
                                role=ROLE_RETAINING_WALL,
                                ref="tunnel_wall",
                                altitude=round(apt_elev, 1)))
                            exclusion_zones.append(wp)
                    except _GEOM_EXC:
                        continue
            if cap_gap and i == 0 \
                    and c_first_len > wall_gap_m + 0.5:
                near_xy = (
                    chain_pts[0][0] + c_first_dir[0] * wall_gap_m,
                    chain_pts[0][1] + c_first_dir[1] * wall_gap_m)
                npx, npy = verts_perp[0]
                ra = (near_xy[0] + npx * chain_half,
                      near_xy[1] + npy * chain_half)
                rd = (near_xy[0] - npx * chain_half,
                      near_xy[1] - npy * chain_half)
                if c_cums[1] > 0:
                    e_a = (
                        e_a + (e_b - e_a)
                        * (wall_gap_m / c_cums[1]))
            else:
                ra = _vertex_offset(i, +chain_half)
                rd = _vertex_offset(i, -chain_half)
            rb = _vertex_offset(i + 1, +chain_half)
            rc = _vertex_offset(i + 1, -chain_half)
            if e_b >= e_a:
                ramp_corners = [rb, ra, rd, rc]
                eh, el = e_b, e_a
            else:
                ramp_corners = [ra, rb, rc, rd]
                eh, el = e_a, e_b
            try:
                rp = Polygon(ramp_corners)
                if not rp.is_valid:
                    rp = rp.buffer(0)
                if (rp.geom_type == "Polygon"
                        and not rp.is_empty
                        and rp.area > 0.5):
                    covered = 0.0
                    for tp in cluster_throat_polys:
                        try:
                            covered += rp.intersection(tp).area
                        except _GEOM_EXC:
                            continue
                    if covered > 0.5 * rp.area:
                        continue    # throat already paves this spot
                    if abs(eh - el) >= 0.1:
                        layout.shapes.append(BuiltShape(
                            polygon=rp,
                            role=ROLE_TUNNEL_RAMP,
                            ref="tunnel_ramp",
                            altitude_high=round(eh, 2),
                            altitude_low=round(el, 2)))
                    else:
                        layout.shapes.append(BuiltShape(
                            polygon=rp,
                            role=ROLE_TUNNEL_RAMP,
                            ref="tunnel_ramp",
                            altitude=round(
                                0.5 * (eh + el), 2)))
                    exclusion_zones.append(rp)
            except _GEOM_EXC:
                pass

    def _emit_fork_throat(throat_pts, throat_half, e_throat,
                          arms):
        """Bridge the shared bore (ending at the fork point ``F``)
        to the per-arm sloping rects with ONE ``node_altitudes``
        "throat" polygon carrying a V-notch, then trace the whole Y
        with retaining walls (outer fan edges + the inner V between
        the arms).  No pavement is graded between the arms — each
        crotch wedge is carved out by an apex vertex.  Modelled on
        the taxiway sloping-rect + junction pattern (user
        2026-06-12, KPHL RWY 26 north portal).  Generalises to N
        arms (N-1 crotches, a star-shaped fan about ``F``).

        ``arms`` = list of ``(branch_pts, half_k, far_k)``; each
        branch starts at the arm's (advanced) fork-side end, so its
        near-edge corners intern 1:1 with the arm ramp's near edge.
        Returns True when a throat polygon was emitted."""
        if len(throat_pts) < 2 or len(arms) < 2:
            return False
        F = throat_pts[-1]
        # Perp of the throat's FAR edge — matches _emit_chain's
        # last-vertex offset so NL/NR intern with the bore's far
        # corners (continuity at the bore→throat seam).
        tdx = throat_pts[-1][0] - throat_pts[-2][0]
        tdy = throat_pts[-1][1] - throat_pts[-2][1]
        tl = math.hypot(tdx, tdy)
        if tl < 1e-6:
            return False
        t_dir = (tdx / tl, tdy / tl)
        t_perp = (-t_dir[1], t_dir[0])
        NL = (F[0] + t_perp[0] * throat_half,
              F[1] + t_perp[1] * throat_half)
        NR = (F[0] - t_perp[0] * throat_half,
              F[1] - t_perp[1] * throat_half)
        # Per-arm fork-side geometry.  Near corners are computed
        # exactly as _emit_chain's vertex-0 offset (±half along the
        # branch's first-segment perp) so they share arm-ramp nodes.
        arm_info = []
        for branch, half_k, _far_k in arms:
            if len(branch) < 2:
                continue
            E = branch[0]
            adx = branch[1][0] - branch[0][0]
            ady = branch[1][1] - branch[0][1]
            al = math.hypot(adx, ady)
            if al < 1e-6:
                continue
            a_dir = (adx / al, ady / al)
            a_perp = (-a_dir[1], a_dir[0])
            cP = (E[0] + a_perp[0] * half_k, E[1] + a_perp[1] * half_k)
            cM = (E[0] - a_perp[0] * half_k, E[1] - a_perp[1] * half_k)
            adv = math.hypot(E[0] - F[0], E[1] - F[1])
            arm_info.append({"E": E, "dir": a_dir, "half": half_k,
                             "cP": cP, "cM": cM, "adv": adv})
        if len(arm_info) < 2:
            return False
        # Nothing to fill if no arm advanced past the fork.
        if max(a["adv"] for a in arm_info) < 2.0:
            return False

        def _ang_about_F(p):
            vx, vy = p[0] - F[0], p[1] - F[1]
            return math.atan2(vx * t_perp[0] + vy * t_perp[1],
                              vx * t_dir[0] + vy * t_dir[1])
        # Order arms left→right (decreasing angle about the bore
        # forward axis) so the fan ring stays simple.
        arm_info.sort(key=lambda a: _ang_about_F(a["E"]),
                      reverse=True)
        # Classify each arm's two near corners as more-left (cL) and
        # more-right (cR) about F.
        for a in arm_info:
            if _ang_about_F(a["cP"]) >= _ang_about_F(a["cM"]):
                a["cL"], a["cR"] = a["cP"], a["cM"]
            else:
                a["cL"], a["cR"] = a["cM"], a["cP"]

        def _fwd(p):
            return (p[0] - F[0]) * t_dir[0] + (p[1] - F[1]) * t_dir[1]

        def _apex(a_left, a_right):
            # Crotch apex = where the two facing inner edges (left
            # arm's right edge, right arm's left edge), extended back
            # toward F, meet — the natural fork point.  Fall back to
            # a pulled-back midpoint when near-parallel or the meet
            # lands behind F / past the inner corners.
            p1, d1 = a_left["cR"], a_left["dir"]
            p2, d2 = a_right["cL"], a_right["dir"]
            denom = d1[0] * (-d2[1]) - d1[1] * (-d2[0])
            if abs(denom) > 1e-9:
                rx, ry = p2[0] - p1[0], p2[1] - p1[1]
                t1 = (rx * (-d2[1]) - ry * (-d2[0])) / denom
                mx, my = p1[0] + t1 * d1[0], p1[1] + t1 * d1[1]
                fwd = (mx - F[0]) * t_dir[0] + (my - F[1]) * t_dir[1]
                cap = max(_fwd(p1), _fwd(p2))
                if 0.0 < fwd <= cap + 0.5:
                    return (mx, my)
            mid = (0.5 * (p1[0] + p2[0]), 0.5 * (p1[1] + p2[1]))
            return (F[0] + 0.6 * (mid[0] - F[0]),
                    F[1] + 0.6 * (mid[1] - F[1]))

        # Build the fan ring (CCW from NL).  Edge i→i+1 carries a
        # wall unless it abuts a ramp: arm near edges (cL→cR) and the
        # bore near edge (NR→NL) do, everything else is a wall.
        ring, wall_edge = [], []

        def _push(p, wall):
            ring.append(p)
            wall_edge.append(wall)
        _push(NL, True)                      # NL → first arm: outer wall
        for idx, a in enumerate(arm_info):
            _push(a["cL"], False)            # arm near edge: no wall
            _push(a["cR"], True)             # cR → apex / NR: wall
            if idx != len(arm_info) - 1:
                _push(_apex(a, arm_info[idx + 1]), True)
        _push(NR, False)                     # NR → NL (bore): no wall

        try:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.geom_type == "MultiPolygon":
                poly = max(poly.geoms, key=lambda g: g.area)
            if (poly.geom_type != "Polygon" or poly.is_empty
                    or poly.area < 1.0):
                return False
        except _GEOM_EXC:
            return False
        # Flat landing at the bore-handoff elevation, carried as
        # node_altitudes (the junction representation) so a future
        # change with differing per-arm start elevations bridges
        # them per-vertex with no further work.
        _np = len(poly.exterior.coords) - 1
        na = [round(e_throat, 1)] * (_np + 1)
        layout.shapes.append(BuiltShape(
            polygon=poly, role=ROLE_TUNNEL_RAMP,
            ref="tunnel_ramp", node_altitudes=na))
        exclusion_zones.append(poly)
        cluster_throat_polys.append(poly)

        # No walls here — the continuous perimeter wall band is traced
        # around the whole cluster ramp union after all ramps emit
        # (``_emit_perimeter_wall``); the throat is just one more ramp
        # piece in that union.
        return True

    # ── Y-SPLIT (user 2026-06-12): when cluster members share the
    # portal but their ways DIVERGE just outside (KPHL RWY 26
    # north: road and rail fork right after the tunnel), emit a
    # shared throat to the fork station, then per-member branch
    # ramps following each way separately as they grade to DEM.
    # Parallel members (south side, SPJC divided highways) keep
    # the single combined ramp unchanged.
    def _point_at(pts, cums, s):
        for i in range(1, len(pts)):
            if cums[i] >= s:
                seg = cums[i] - cums[i - 1]
                t = ((s - cums[i - 1]) / seg) if seg > 0 else 0.0
                return (pts[i - 1][0]
                        + t * (pts[i][0] - pts[i - 1][0]),
                        pts[i - 1][1]
                        + t * (pts[i][1] - pts[i - 1][1]))
        return pts[-1]

    s_div = None
    member_chains = []
    if len(cl) > 1:
        for k in cl:
            w_k = portal_data[k][2]
            if w_k and len(w_k) >= 2:
                c_k = [0.0]
                for i in range(1, len(w_k)):
                    c_k.append(c_k[-1] + math.hypot(
                        w_k[i][0] - w_k[i - 1][0],
                        w_k[i][1] - w_k[i - 1][1]))
                member_chains.append((k, w_k, c_k))
        if len(member_chains) > 1:
            probe_max = min(c[2][-1] for c in member_chains)
            # Gate-on ends the shared bore as soon as the ways START
            # to diverge (small margin, fine probe) so the bore is
            # short enough for the throat to widen smoothly into the
            # arms (user 2026-06-13).  The legacy bare-crotch path
            # keeps the wider 8 m margin.
            _div_margin = 2.0 if TUNNEL_FORK_THROAT else 8.0
            _div_step = 2.5 if TUNNEL_FORK_THROAT else 5.0
            s = 5.0 if TUNNEL_FORK_THROAT else 10.0
            while s < probe_max:
                pts_at = [_point_at(w, c, s)
                          for (_k, w, c) in member_chains]
                spread = max(
                    math.hypot(p1[0] - p2[0], p1[1] - p2[1])
                    for x1, p1 in enumerate(pts_at)
                    for p2 in pts_at[x1 + 1:])
                if spread > cluster_span + _div_margin:
                    s_div = s
                    break
                s += _div_step
            if s_div is not None and (probe_max - s_div) < 10.0:
                s_div = None     # fork too close to the end

    if s_div is None:
        _emit_chain(walk_pts, combined_half,
                    elev_low, elev_high, True)
        if len(walk_pts) >= 2:
            _cl_arm_ends.append((walk_pts[-1], walk_pts[-2],
                                 combined_half))
    else:
        # Per-member branches (widest first).
        ordered = []
        for k, w_k, c_k in member_chains:
            half_k = 0.5 * portal_data[k][7]
            ordered.append((half_k, k, w_k, c_k))
        ordered.sort(key=lambda t: -t[0])
        # Gate-on: arms start at the common station where every pair has
        # separated by their combined half-widths + a gap (they start
        # CLOSE together), and the bore (a sloping rect) ends EARLY
        # enough to leave at least a ``_throat_min`` junction that
        # cleanly widens from the bore to the arms — no jog (user
        # 2026-06-13).  The legacy path keeps s_div + per-arm advance.
        _throat_min = 10.0
        s_arm = s_div
        s_bore_end = s_div
        if TUNNEL_FORK_THROAT and len(ordered) >= 2:
            _half_of = {k: hk for hk, k, _w, _c in ordered}
            _chain_of = {k: (w, c) for _h, k, w, c in ordered}
            d_adv = 0.0
            while s_div + d_adv < probe_max - 4.0:
                _pa = {k: _point_at(_chain_of[k][0], _chain_of[k][1],
                                    s_div + d_adv) for k in _half_of}
                _clear = True
                _ks = list(_half_of)
                for _i9 in range(len(_ks)):
                    for _j9 in range(_i9 + 1, len(_ks)):
                        _ka, _kb = _ks[_i9], _ks[_j9]
                        _need = (_half_of[_ka] + _half_of[_kb]
                                 + 2.0 * wall_gap_m + 2.0)
                        if math.hypot(
                                _pa[_ka][0] - _pa[_kb][0],
                                _pa[_ka][1] - _pa[_kb][1]) < _need:
                            _clear = False
                            break
                    if not _clear:
                        break
                if _clear:
                    break
                d_adv += 1.0
            s_arm = s_div + d_adv
            # Shorten the bore so the junction spans ≥ _throat_min; the
            # bore never extends past the fork (s_div).
            s_bore_end = max(1.0, min(s_div, s_arm - _throat_min))
        # Shared bore (sloping rect) on the centred canonical walk,
        # ending at s_bore_end.
        throat = [walk_pts[0]]
        for i in range(1, len(walk_pts)):
            if cum_dists[i] < s_bore_end:
                throat.append(walk_pts[i])
            else:
                break
        throat.append(_point_at(walk_pts, cum_dists, s_bore_end))
        e_div = (elev_low + (elev_high - elev_low)
                 * (s_bore_end / total_walk if total_walk > 0
                    else 0.0))
        _emit_chain(throat, combined_half,
                    elev_low, e_div, True)
        prior: list = []          # (LineString, half)
        try:
            prior.append((LineString(throat), combined_half))
        except _GEOM_EXC:
            pass
        # Collect the arm chains.  Gate ON: every arm starts at the
        # common ``s_arm`` station (mutually clear by construction, so
        # no per-arm advance).  Gate OFF: legacy per-arm advance past
        # the throat + prior siblings (byte-identical to before).
        arm_specs: list = []      # (branch_pts, half_k, far_k)
        for half_k, k, w_k, c_k in ordered:
            _start_s = s_arm if TUNNEL_FORK_THROAT else s_div
            branch = [_point_at(w_k, c_k, _start_s)]
            for i in range(1, len(w_k)):
                if c_k[i] > _start_s:
                    branch.append(w_k[i])
            if len(branch) < 2:
                continue
            if not TUNNEL_FORK_THROAT and prior:
                # advance the start until clear of the throat +
                # every prior sibling corridor (sample 2 m).
                bl = LineString(branch)
                s9 = 0.0
                while s9 < bl.length - 4.0:
                    pt9 = bl.interpolate(s9)
                    if all(pt9.distance(pl) >= half_k + ph + 0.5
                           for pl, ph in prior):
                        break
                    s9 += 2.0
                if s9 > 0.0:
                    if bl.length - s9 < 6.0:
                        continue
                    head9 = bl.interpolate(s9)
                    branch = ([(head9.x, head9.y)]
                              + [pp for i9, pp in enumerate(branch)
                                 if bl.project(Point(pp)) > s9])
                    if len(branch) < 2:
                        continue
            far_k = portal_data[k][5]
            arm_specs.append((branch, half_k, far_k))
            try:
                prior.append((LineString(branch), half_k))
            except _GEOM_EXC:
                pass
        # Throat bridges bore→arms; emitted before the arms so it abuts
        # the bore's far edge.
        if TUNNEL_FORK_THROAT and len(arm_specs) >= 2:
            _emit_fork_throat(throat, combined_half, e_div,
                              arm_specs)
        for branch, half_k, far_k in arm_specs:
            _emit_chain(branch, half_k, e_div, far_k, False)
            if len(branch) >= 2:
                _cl_arm_ends.append((branch[-1], branch[-2], half_k))
        # WALL OPENINGS: a diverging branch must cross the
        # throat's (or a sibling's) side wall — clip every wall
        # piece of THIS cluster against the cluster's ramp
        # polygons (walls are flat; clipping is safe).
        try:
            ramps9 = [s9.polygon for s9 in
                      layout.shapes[_cl_start_idx:]
                      if getattr(s9, 'ref', '') == 'tunnel_ramp'
                      and s9.polygon is not None]
            if ramps9:
                ramp_u9 = unary_union(ramps9).buffer(0.3)
                for s9 in layout.shapes[_cl_start_idx:]:
                    if getattr(s9, 'ref', '') != 'tunnel_wall' \
                            or s9.polygon is None:
                        continue
                    if not s9.polygon.intersects(ramp_u9):
                        continue
                    d9 = s9.polygon.difference(ramp_u9)
                    if d9.is_empty:
                        s9.polygon = None
                        continue
                    parts9 = sorted(
                        (g for g in getattr(d9, 'geoms', [d9])
                         if g.geom_type == 'Polygon'
                         and g.area >= 0.5),
                        key=lambda g: -g.area)
                    s9.polygon = parts9[0] if parts9 else None
                layout.shapes = [
                    s9 for s9 in layout.shapes
                    if not (getattr(s9, 'ref', '') == 'tunnel_wall'
                            and s9.polygon is None)]
        except _GEOM_EXC:
            pass
    # ── CONTINUOUS PERIMETER WALL (gate ON, user 2026-06-13): ONE
    # wall traced around the WHOLE cluster ramp-union perimeter,
    # regardless of whether the tunnel forks.  The band is the ramp
    # union's outward offset annulus (shapely buffer → clean corners,
    # no self-overlap on curves/sharp ends), node_altitudes FOLLOWING
    # THE DEM like the airport boundary ribbon.  The annulus is "slit"
    # into a single hole-free ring (to_osm drops interior rings, which
    # would otherwise emit a filled disc over the ramp).  Replaces the
    # per-segment / cap / throat walls entirely.
    if TUNNEL_FORK_THROAT:
        try:
            _ramps_b = [
                s.polygon for s in layout.shapes[_cl_start_idx:]
                if getattr(s, 'ref', '') == 'tunnel_ramp'
                and s.polygon is not None
                and not s.polygon.is_empty]
            _ru = unary_union(_ramps_b) if _ramps_b else None
            _ru_polys = [g for g in getattr(_ru, 'geoms', [_ru] if _ru
                                            else [])
                         if g.geom_type == 'Polygon'
                         and not g.is_empty]
            _g0 = wall_gap_m
            _g1 = wall_gap_m + retaining_wall_width_m
            # Openings at every arm's FAR (surface) end: the annulus
            # crosses the roadway at BOTH ends, but only the PORTAL
            # crossing is the cap — at the far end the road continues
            # at grade and the crossing walls it off (user 2026-07-04,
            # CYUL east: the slit knife then severed the true cap at
            # the narrow portal, leaving the wall "flipped" with the
            # cap at the high end).  Cutting the far ends open also
            # makes the band simply connected, so the portal cap
            # survives the hole-slitting untouched.
            _openings = []
            for (_ep, _pp, _hk) in _cl_arm_ends:
                _odx, _ody = _ep[0] - _pp[0], _ep[1] - _pp[1]
                _odl = math.hypot(_odx, _ody) or 1.0
                _oux, _ouy = _odx / _odl, _ody / _odl
                _open_line = LineString([
                    (_ep[0] - _oux * 1.0, _ep[1] - _ouy * 1.0),
                    (_ep[0] + _oux * (_g1 + 2.0),
                     _ep[1] + _ouy * (_g1 + 2.0))])
                try:
                    _openings.append(_open_line.buffer(
                        max(_hk + _g0 - 0.05, 0.5), cap_style=2))
                except _GEOM_EXC:
                    continue
            _open_u = unary_union(_openings) if _openings else None
            for _rp in _ru_polys:
                try:
                    _outer = _rp.buffer(_g1, join_style=2,
                                        mitre_limit=2.0)
                    _inner = _rp.buffer(_g0, join_style=2,
                                        mitre_limit=2.0)
                    _band = _outer.difference(_inner)
                    if _open_u is not None:
                        _band = _band.difference(_open_u)
                except _GEOM_EXC:
                    continue
                for _bp in getattr(_band, 'geoms', [_band]):
                    if (_bp.geom_type != 'Polygon' or _bp.is_empty
                            or _bp.area < 0.5):
                        continue
                    # Slit EVERY interior hole, not just the first.  A
                    # Y-fork band has TWO holes — the central hole AND
                    # the crotch wedge between the diverging arms — so
                    # the old single-hole self-touching slit left the
                    # second hole, the ring filled into a solid disc
                    # over the ramps, and the wall-vs-ramp clip then
                    # dropped it (the fork lost its wall).  Cut a thin
                    # radial knife from each hole out to the band
                    # exterior, collapsing the multiply-connected
                    # annulus into one simply-connected hole-free ring
                    # (to_osm drops interior rings, which would fill the
                    # ramp with a disc).
                    _slit = _bp
                    _guard = 0
                    while (_slit is not None
                           and _slit.geom_type == 'Polygon'
                           and _slit.interiors and _guard < 8):
                        _guard += 1
                        try:
                            _pa, _pb = nearest_points(
                                _slit.interiors[0], _slit.exterior)
                        except _GEOM_EXC:
                            _slit = None
                            break
                        _kdx, _kdy = _pb.x - _pa.x, _pb.y - _pa.y
                        _kl = math.hypot(_kdx, _kdy) or 1.0
                        _kux, _kuy = _kdx / _kl, _kdy / _kl
                        _knife = LineString([
                            (_pa.x - _kux * 0.1, _pa.y - _kuy * 0.1),
                            (_pb.x + _kux * 0.1, _pb.y + _kuy * 0.1),
                        ]).buffer(0.02, cap_style=2, join_style=2)
                        try:
                            _cut = _slit.difference(_knife)
                        except _GEOM_EXC:
                            _slit = None
                            break
                        if _cut.geom_type == 'MultiPolygon':
                            _cut = max(_cut.geoms, key=lambda g: g.area)
                        _slit = (_cut if _cut.geom_type == 'Polygon'
                                 and not _cut.is_empty else None)
                    if (_slit is None or _slit.geom_type != 'Polygon'
                            or _slit.is_empty or _slit.interiors):
                        continue
                    _ring = list(_slit.exterior.coords)
                    if _ring and _ring[0] == _ring[-1]:
                        _ring = _ring[:-1]
                    if len(_ring) < 4:
                        continue
                    _na = []
                    for _vx, _vy in _ring:
                        _d = dem_at(_vx, _vy)
                        _na.append(round(_d if _d is not None
                                         else apt_elev, 1))
                    _na.append(_na[0])
                    try:
                        _wp = Polygon(_ring)
                        if _wp.is_empty:
                            continue
                        layout.shapes.append(BuiltShape(
                            polygon=_wp, role=ROLE_RETAINING_WALL,
                            ref="tunnel_wall", node_altitudes=_na))
                        exclusion_zones.append(_bp)
                    except _GEOM_EXC:
                        continue
        except _GEOM_EXC:
            pass
    return 1


def _low_connector_corridors(low_connector_gaps: list) -> list:
    """Dissolve the recorded per-way gap rects into corridor polygons.

    Each gap is ``(gap_line, corridor_width_m)``; gaps of grouped
    parallel ways (< ``UNDERPASS_GROUP_DIST_M`` apart) dissolve into
    ONE corridor via a morphological closing, so the group renders as
    a single depressed trench spanning its combined width.
    """
    if not low_connector_gaps:
        return []
    rects = []
    for (gap_line, corridor_width_m) in low_connector_gaps:
        try:
            r = gap_line.buffer(corridor_width_m / 2.0,
                                cap_style=2, join_style=2)
            if not r.is_empty:
                rects.append(r)
        except _GEOM_EXC:
            continue
    if not rects:
        return []
    try:
        merged = unary_union(rects)
        close_r = UNDERPASS_GROUP_DIST_M / 2.0
        merged = (merged.buffer(close_r, join_style=2)
                        .buffer(-close_r, join_style=2))
    except _GEOM_EXC:
        merged = unary_union(rects)
    return ([merged] if merged.geom_type == "Polygon"
            else [g for g in getattr(merged, "geoms", ())
                  if g.geom_type == "Polygon"])


def _suppress_portals_in_low_corridors(
        portal_data: list, corridors: list) -> list:
    """Drop portals whose mouth sits INSIDE a low-connector corridor.

    OSM splits ways at intersections, so a frontage road's crossings
    of taxiway A and taxiway B often live on DIFFERENT ways — the
    per-way gap merge cannot see across them, and both leftover
    portals would ramp INTO the flat low corridor (overlapping it and
    each other).  The corridor covers the whole grouped surface, so
    any portal starting inside it is superseded by the flat rect.
    """
    if not corridors or not portal_data:
        return portal_data
    kept = []
    for pd in portal_data:
        walk_pts = pd[2]
        px, py = walk_pts[0]
        inside = False
        for corridor in corridors:
            try:
                if corridor.buffer(2.0).contains(Point(px, py)):
                    inside = True
                    break
            except _GEOM_EXC:
                continue
        if inside:
            if os.environ.get("O4_TUNNEL_DEBUG") == "1":
                print(f"    [tunnel-drop] way {pd[1]} portal "
                      f"({px:.0f},{py:.0f}): inside a flat "
                      f"low-connector corridor")
            continue
        kept.append(pd)
    return kept


def _emit_low_corridor_connectors(
        layout: "PavementLayout",
        corridors: list,
        exclusion_zones: list,
        airside_gate_union,
        airport_elevation_at,
        dem_at,
        tunnel_depth_m: float,
        wall_gap_m: float,
        retaining_wall_width_m: float) -> int:
    """Emit the depressed surface BETWEEN two merged bores.

    User 2026-07-04 (KDFW double parallel taxiways): when the surface
    gap between two taxiway underpasses is too short for a ramp pair
    to climb to DEM and come back, the whole area between them stays
    at the LOW elevation — a single corridor-width flat
    ``ROLE_TUNNEL_RAMP`` rect at ``apt_elev − tunnel_depth_m`` with a
    retaining wall around its open sides.  Gaps of grouped parallel
    ways (< ``UNDERPASS_GROUP_DIST_M`` apart) dissolve into ONE
    corridor via a morphological closing, so the group renders as a
    single depressed trench, never overlapping per-way rects.

    Walls follow the DEM per vertex like every other tunnel wall
    (user 2026-06-13); the strip under the taxiways themselves is
    NOT walled or paved here — the bores continue beneath.  All
    emitted pieces join ``exclusion_zones`` so the boundary ribbon
    and DEM bridges avoid them.  Takes the dissolved ``corridors``
    from :func:`_low_connector_corridors` (also used to suppress
    superseded portals).  Returns the number of corridor rects
    emitted.
    """
    if not corridors:
        return 0
    n_rects = 0
    for corridor in corridors:
        if corridor.is_empty or corridor.area < 20.0:
            continue
        try:
            centre = corridor.centroid
            apt_elev = airport_elevation_at(centre.x, centre.y)
        except _GEOM_EXC:
            apt_elev = None
        if apt_elev is None:
            continue
        elev_low = float(apt_elev) - tunnel_depth_m
        # The visible depressed surface: the corridor minus airside
        # pavement (a graze against a service road / building pad
        # must not put a −8 m rect under real pavement).
        try:
            open_part = (corridor if airside_gate_union is None
                         else corridor.difference(
                             airside_gate_union.buffer(0.5)))
        except _GEOM_EXC:
            open_part = corridor
        surf_parts = ([open_part] if open_part.geom_type == "Polygon"
                      else [g for g in getattr(open_part, "geoms", ())
                            if g.geom_type == "Polygon"])
        for part in surf_parts:
            if part.is_empty or part.area < 4.0:
                continue
            simple = part.simplify(0.05)
            if simple.geom_type != "Polygon" or simple.is_empty:
                simple = part
            n_vertices = len(simple.exterior.coords)
            layout.shapes.append(BuiltShape(
                polygon=simple, role=ROLE_TUNNEL_RAMP,
                ref="tunnel_low_connector",
                node_altitudes=[round(elev_low, 2)] * n_vertices))
            exclusion_zones.append(simple)
            n_rects += 1
        # Retaining wall around the corridor's open sides: a 1 m band
        # offset by the standard wall gap, minus airside pavement (the
        # bores continue under the taxiways — no wall across the road).
        try:
            band = (corridor.buffer(
                        wall_gap_m + retaining_wall_width_m,
                        join_style=2)
                    .difference(corridor.buffer(wall_gap_m,
                                                join_style=2)))
            if airside_gate_union is not None:
                band = band.difference(airside_gate_union.buffer(0.5))
        except _GEOM_EXC:
            band = None
        band_parts = ([] if band is None else
                      ([band] if band.geom_type == "Polygon"
                       else [g for g in getattr(band, "geoms", ())
                             if g.geom_type == "Polygon"]))
        for wall in band_parts:
            if wall.is_empty or wall.area < 1.0:
                continue
            # to_osm drops interior rings — slit any annulus open at
            # its narrowest point (same trick as the perimeter wall).
            slit = wall
            guard = 0
            while (slit is not None and slit.geom_type == "Polygon"
                   and slit.interiors and guard < 8):
                guard += 1
                try:
                    pa, pb = nearest_points(
                        slit.interiors[0], slit.exterior)
                    kdx, kdy = pb.x - pa.x, pb.y - pa.y
                    kl = math.hypot(kdx, kdy) or 1.0
                    kux, kuy = kdx / kl, kdy / kl
                    knife = LineString([
                        (pa.x - kux * 0.1, pa.y - kuy * 0.1),
                        (pb.x + kux * 0.1, pb.y + kuy * 0.1),
                    ]).buffer(0.02, cap_style=2, join_style=2)
                    cut = slit.difference(knife)
                    if cut.geom_type == "MultiPolygon":
                        cut = max(cut.geoms, key=lambda g: g.area)
                    slit = (cut if cut.geom_type == "Polygon"
                            and not cut.is_empty else None)
                except _GEOM_EXC:
                    slit = None
            if (slit is None or slit.geom_type != "Polygon"
                    or slit.is_empty or slit.interiors):
                continue
            ring = list(slit.exterior.coords)
            if ring and ring[0] == ring[-1]:
                ring = ring[:-1]
            if len(ring) < 4:
                continue
            wall_alts = []
            for (vx, vy) in ring:
                ground = dem_at(vx, vy)
                wall_alts.append(round(
                    ground if ground is not None else float(apt_elev), 1))
            wall_alts.append(wall_alts[0])
            try:
                wall_poly = Polygon(ring)
                if wall_poly.is_empty:
                    continue
                layout.shapes.append(BuiltShape(
                    polygon=wall_poly, role=ROLE_RETAINING_WALL,
                    ref="tunnel_wall", node_altitudes=wall_alts))
                exclusion_zones.append(wall)
            except _GEOM_EXC:
                continue
    if n_rects:
        try:
            UI.vprint(1,
                f"  [pav-builder] emitted {n_rects} flat low-corridor "
                f"connector(s) between close taxiway underpasses.")
        except _GEOM_EXC:
            pass
    return n_rects


# Clearance kept between a graze-clipped tunnel piece and the airside
# pavement that grazed it.  Must exceed the OSM-emit shared-vertex
# bucket (SHARED_VERTEX_TOL_M = 0.5 m) so a clipped ramp/wall edge
# never hashes onto a pavement node — same rationale as ``wall_gap_m``
# (user 2026-05-03: one node with two altitudes renders as a vertical
# glitch).
_TUNNEL_GRAZE_CLEARANCE_M = 0.6


def _sloped_rect_clipped_altitudes(orig_poly, alt_high, alt_low,
                                   new_poly):
    """Per-vertex altitudes for a clipped ``altitude_high/low`` rect.

    A sloped rect encodes its gradient by RING ORDER (corners 0,3 =
    high edge, corners 1,2 = low edge — the ``_rect_from_axis_extended``
    convention), which any polygon clip destroys.  Convert to explicit
    ``node_altitudes``: project every new ring vertex onto the rect's
    slope axis (low-edge midpoint → high-edge midpoint) and lerp.
    Returns the closed altitude list for ``new_poly``'s exterior ring,
    or None when the original ring is not the 4-corner rect the
    convention promises (caller should then drop the piece).
    """
    try:
        ring = list(orig_poly.exterior.coords)
    except _GEOM_EXC:
        return None
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) != 4:
        return None
    c0, c1, c2, c3 = ring
    mid_high = ((c0[0] + c3[0]) / 2.0, (c0[1] + c3[1]) / 2.0)
    mid_low = ((c1[0] + c2[0]) / 2.0, (c1[1] + c2[1]) / 2.0)
    ax = mid_high[0] - mid_low[0]
    ay = mid_high[1] - mid_low[1]
    axis_len_sq = ax * ax + ay * ay
    if axis_len_sq < 1e-6:
        return None
    alts = []
    try:
        new_ring = list(new_poly.exterior.coords)
    except _GEOM_EXC:
        return None
    for (vx, vy) in new_ring:
        t = ((vx - mid_low[0]) * ax + (vy - mid_low[1]) * ay) \
            / axis_len_sq
        t = max(0.0, min(1.0, t))
        alts.append(round(alt_low + t * (alt_high - alt_low), 2))
    return alts


def _finalize_tunnel_emission(
        layout: "PavementLayout", exclusion_zones: list,
        boundary_clearance_m: float, airside_gate_union,
        pre_emit_shape_ids: set, n_emitted: int) -> int:
    """Post-emission coordination: boundary-ribbon subtraction,
    under-pavement piece drop, and the wall-vs-ramp clip.
    Returns the emitted-portal count.
    """
    # Boundary coordination: clip every ROLE_BOUNDARY shape so
    # it doesn't overlap the actual tunnel-polygon footprint.
    # The boundary ribbon is built by line-buffering the apt.dat
    # row-130 line, which extends ~2.5 m to either side of the
    # boundary line — so when a tunnel ramp crosses the boundary,
    # the ribbon overlaps both the inside-airport (LOW-end) and
    # outside-airport (HIGH-end) parts of the ramp.  Subtracting
    # the actual ramp+walls union (buffered by 0.5 m for a small
    # visible gap) handles both cases without carving exclusion
    # discs outside the tunnel's footprint — the boundary still
    # traces the rest of the perimeter unchanged.
    if exclusion_zones:
        try:
            tunnel_union = unary_union(exclusion_zones)
        except _GEOM_EXC:
            tunnel_union = None
        if tunnel_union is None or tunnel_union.is_empty:
            return n_emitted
        excl_union = tunnel_union.buffer(boundary_clearance_m)
        kept_shapes: list[BuiltShape] = []
        for s in layout.shapes:
            if s.role != ROLE_BOUNDARY:
                kept_shapes.append(s)
                continue
            # Capture the old ring + altitudes BEFORE the clip so
            # we can NN-resample altitudes for the new ring.  Per
            # user 2026-04-29 (HECA crash investigation): leaving
            # the boundary ribbon with no altitudes after a clip
            # made X-Plane crash on load — the 1066-vertex polygon
            # without elevation guidance was unrenderable.
            try:
                _old_ring = list(s.polygon.exterior.coords)
            except _GEOM_EXC:
                _old_ring = []
            if _old_ring and _old_ring[0] == _old_ring[-1]:
                _old_ring = _old_ring[:-1]
            _old_alts = (list(s.node_altitudes)
                          if s.node_altitudes else None)
            try:
                new_poly = s.polygon.difference(excl_union)
            except _GEOM_EXC:
                kept_shapes.append(s)
                continue
            if new_poly.is_empty:
                continue
            if new_poly.geom_type == "Polygon":
                s.polygon = new_poly
                resampled = _resample_node_altitudes_nn(
                    new_poly, _old_ring, _old_alts)
                if resampled is not None:
                    s.node_altitudes = resampled
                # else: keep existing s.altitude / node_altitudes
                # (possibly mismatched but better than nothing).
                kept_shapes.append(s)
            elif new_poly.geom_type == "MultiPolygon":
                # Split the boundary shape into the resulting pieces.
                for g in new_poly.geoms:
                    if (g.geom_type != "Polygon"
                            or g.is_empty
                            or g.area < 5.0):
                        continue
                    resampled = _resample_node_altitudes_nn(
                        g, _old_ring, _old_alts)
                    kept_shapes.append(BuiltShape(
                        polygon=g,
                        role=s.role,
                        ref=s.ref,
                        altitude=(s.altitude if resampled is None
                                  else None),
                        node_altitudes=resampled))
        layout.shapes = kept_shapes
    # PAVEMENT-OVERLAP CLIP (user 2026-06-12, KPHL): tunnel structure
    # is emitted for REAL under-pavement service roads too (the
    # small_roads ``building_passage`` ways threading the terminal
    # complex) — but a cap/wall/ramp piece may not overlap any
    # pavement shape; the covered stretch has no visible structure.
    # Pieces are short, so dropping the overlapping ones lets the
    # trench dive under a taxiway and re-emerge on the far side.
    if airside_gate_union is not None:
        # COVERED vs GRAZE discriminator (2026-07-10, SPJC big NW
        # crossing): the drop is meant for pieces UNDER pavement (a
        # covered stretch has no visible structure — overlap ≈ the
        # whole piece).  An implied bore crossing pavement OBLIQUELY
        # grazes the pavement corner with the piece nearest its portal
        # (SPJC: ~6 % of the piece area against the shoulder-widened
        # runway edge) — the old absolute 0.25 m² threshold wholesale-
        # dropped exactly the mouth descent and the perimeter wall
        # band, so the tunnel had no visible entrances.  Now: mostly-
        # covered pieces (≥ 50 %) still drop whole; grazes are CLIPPED
        # off the pavement instead, converting sloped rects to
        # node_altitudes (ring-order slope semantics do not survive a
        # clip) and NN-resampling clipped node_altitudes rings.
        _graze_clip = os.environ.get(
            "O4_TUNNEL_GRAZE_CLIP", "1") == "1"
        _gate_buf = None
        _kept9 = []
        _n_clip = 0
        _n_graze = 0
        for _k9, s9 in enumerate(layout.shapes):
            if not (id(s9) not in pre_emit_shape_ids
                    and getattr(s9, "ref", "") in
                    ("tunnel_cap", "tunnel_wall", "tunnel_ramp")
                    and s9.polygon is not None
                    and not s9.polygon.is_empty):
                _kept9.append(s9)
                continue
            try:
                _ov = s9.polygon.intersection(
                    airside_gate_union).area
            except _GEOM_EXC:
                _ov = 0.0
            if _ov <= 0.25:
                _kept9.append(s9)
                continue
            if not _graze_clip or _ov >= 0.5 * s9.polygon.area:
                _n_clip += 1    # covered stretch — no visible structure
                continue
            # A graze — clip the piece off the pavement (with vertex-
            # bucket clearance) and keep the visible remainder.
            if _gate_buf is None:
                try:
                    _gate_buf = airside_gate_union.buffer(
                        _TUNNEL_GRAZE_CLEARANCE_M)
                except _GEOM_EXC:
                    _gate_buf = airside_gate_union
            try:
                _cutg = s9.polygon.difference(_gate_buf)
            except _GEOM_EXC:
                _n_clip += 1
                continue
            if _cutg.geom_type == "MultiPolygon":
                _cutg = max(_cutg.geoms, key=lambda g: g.area)
            if (_cutg.geom_type != "Polygon" or _cutg.is_empty
                    or _cutg.area < 1.0):
                _n_clip += 1
                continue
            if s9.node_altitudes:
                try:
                    _oring = list(s9.polygon.exterior.coords)
                except _GEOM_EXC:
                    _oring = []
                if _oring and _oring[0] == _oring[-1]:
                    _oring = _oring[:-1]
                _res = _resample_node_altitudes_nn(
                    _cutg, _oring, list(s9.node_altitudes),
                    interior_edge_project=True)
                if _res is None:
                    _n_clip += 1
                    continue
                s9.node_altitudes = _res
            elif (s9.altitude_high is not None
                    and s9.altitude_low is not None):
                _res = _sloped_rect_clipped_altitudes(
                    s9.polygon, s9.altitude_high, s9.altitude_low,
                    _cutg)
                if _res is None:
                    _n_clip += 1
                    continue
                s9.altitude_high = None
                s9.altitude_low = None
                s9.node_altitudes = _res
            # (flat ``altitude`` pieces keep their altitude verbatim)
            s9.polygon = _cutg
            _n_graze += 1
            _kept9.append(s9)
        if _n_clip or _n_graze:
            layout.shapes = _kept9
            try:
                UI.vprint(1,
                    f"  [pav-builder] dropped {_n_clip} tunnel "
                    f"piece(s) under pavement (covered stretch — no "
                    f"visible structure)"
                    + (f"; graze-clipped {_n_graze} portal piece(s) "
                       f"off the pavement edge." if _n_graze
                       else "."))
            except _GEOM_EXC:
                pass
    # WALL-vs-RAMP CLIP (user 2026-06-12, LMML): a retaining wall / cap
    # is flat at apt_elev and must never sit ON TOP of a tunnel ROAD
    # ramp surface.  Where two portal walks of one tunnel reach past
    # each other (LMML east cluster), one portal's walls land on the
    # other portal's ramps — walls entirely covered (#1327: 33 m² wall,
    # 32.7 m² on a ramp).  The per-cluster wall-opening clip above only
    # sees its OWN cluster's ramps; clip every emitted wall/cap against
    # the union of ALL emitted ramps.  The 0.6 m ``wall_gap_m`` keeps a
    # wall clear of its OWN ramp, so only cross-walk coverage is removed.
    _ramp_polys = [s9.polygon for s9 in layout.shapes
                   if id(s9) not in pre_emit_shape_ids
                   and getattr(s9, "ref", "") == "tunnel_ramp"
                   and s9.polygon is not None
                   and not s9.polygon.is_empty]
    if _ramp_polys:
        try:
            _ramp_u = unary_union(_ramp_polys)
        except _GEOM_EXC:
            _ramp_u = None
        if _ramp_u is not None and not _ramp_u.is_empty:
            _keptW = []
            _n_wclip = 0
            for s9 in layout.shapes:
                if (id(s9) not in pre_emit_shape_ids
                        and getattr(s9, "ref", "") in
                        ("tunnel_cap", "tunnel_wall")
                        and s9.polygon is not None
                        and not s9.polygon.is_empty):
                    try:
                        if (s9.polygon.intersection(_ramp_u).area
                                > 0.25):
                            _d = s9.polygon.difference(_ramp_u)
                            if _d.geom_type == "MultiPolygon":
                                _d = max(_d.geoms, key=lambda g: g.area)
                            if (_d.is_empty
                                    or _d.geom_type != "Polygon"
                                    or _d.area < 1.0):
                                _n_wclip += 1
                                continue
                            s9.polygon = _d
                            _n_wclip += 1
                    except _GEOM_EXC:
                        pass
                _keptW.append(s9)
            if _n_wclip:
                layout.shapes = _keptW
                try:
                    UI.vprint(1,
                        f"  [pav-builder] clipped {_n_wclip} tunnel "
                        f"wall/cap piece(s) off overlapping ramps.")
                except _GEOM_EXC:
                    pass
    return n_emitted


def _emit_tunnel_portals(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        tunnel_depth_m: float = 8.0,
        max_ramp_grade: float = 0.04,
        ramp_min_length_m: float = 200.0,
        arm_max_length_m: float = 500.0,
        carriageway_width_m: float = 22.0,
        retaining_wall_width_m: float = 1.0,
        # ``wall_gap_m`` must exceed the OSM emit's vertex bucket
        # size (SHARED_VERTEX_TOL_M = 0.5 m) so the ramp's road-edge
        # corners and the wall's inner corners don't hash to the
        # same node id.  At the portal end the ramp altitude is
        # apt_elev − tunnel_depth; the wall altitude is apt_elev.
        # Sharing the vertex would emit one node with two altitudes,
        # rendering as a vertical glitch (user 2026-05-03).
        wall_gap_m: float = 0.6,
        # Divided-highway carriageways with two parallel ways
        # cluster into a single combined entrance.  User 2026-05-03:
        # one entrance per end of the tunnel, not one per
        # carriageway.  User 2026-07-04 (KDFW): ways less than 35 m
        # apart group into ONE corridor (motorway + frontage road +
        # rail together); KDFW's two motorway carriageways at ~113 m
        # correctly stay separate.
        portal_cluster_dist_m: float = UNDERPASS_GROUP_DIST_M,
        boundary_clearance_m: float = 0.5,
        # Per user 2026-05-04: skip portals more than this far from
        # any airport boundary edge.  Tunnels far from the airport
        # don't affect X-Plane's airport mesh and were generating
        # spurious ramps along distant urban roads.
        max_boundary_dist_m: float = 1000.0,
        excluded_way_ids: set | None = None,
        skip_if_adjacent_road: bool = SKIP_TUNNEL_RAMPS_NEAR_ROADS,
        adjacent_road_dist_m: float = TUNNEL_ADJACENT_ROAD_DIST_M,
        ) -> int:
    """For each tunnel portal (each end of an OSM ``aeroway=*``
    ``tunnel=yes|building_passage`` way), emit the visible road-
    surface structure that transitions outside-DEM elevation down
    to ``apt_elev − tunnel_depth_m`` at the portal:

      1. A flat ``ROLE_RETAINING_WALL`` CAP polygon AT the portal
         node (perpendicular to road direction at portal).  The
         cap's centre line is the portal node — its width spans
         the road (carriageway width + wall_gap on each side),
         its thickness is ``retaining_wall_width_m`` (1 m).
      2. Two flat ``ROLE_RETAINING_WALL`` ARM polygons reaching
         OUTWARD from the cap along the surface roadway, on each
         side of the carriageway.  The arms follow the OSM
         surface road's polyline — multi-segment when the road
         curves.  Arm length adapts to terrain: we first walk up
         to ``arm_max_length_m`` (default 500 m), sample DEM at
         the far end, then truncate the walk so the grade from
         ``apt_elev − tunnel_depth_m`` (portal) up to that DEM
         height never exceeds ``max_ramp_grade``.  Floor at
         ``ramp_min_length_m`` (default 200 m) so a flat road
         still gets a substantial visible approach.
      3. A chain of sloped ``ROLE_TUNNEL_RAMP`` polygons matching
         the surface-road segments under the arms.  Each segment's
         elevation interpolates linearly from
         ``apt_elev − tunnel_depth_m`` at the portal to outside
         DEM at the far end of the walk, in proportion to its
         cumulative distance from the portal.

    Per user 2026-04-29 (SPJC review): the previous geometry put
    the cap PAST the portal INSIDE the airport, with arms going
    AWAY from the tunnel only as far as the OSM way extended
    outside the airport.  That made SPJC's SW tunnel "too short
    and inside the tunnel" because the OSM way ended right at the
    boundary.  Walking the SURFACE road OUTWARD from the OSM
    portal node correctly lands the cap at the tunnel mouth and
    the arms on the highway approach.

    Two-carriageway tunnels (divided highways) cluster by portal-
    node proximity (within ``portal_cluster_dist_m``).  Each
    carriageway in a cluster gets its own arm pair; the caps form
    a perpendicular line across all member portals.

    Boundary coordination: subtract the tunnel-polygon union
    (buffered by ``boundary_clearance_m``, default 1 m which
    exceeds the OSM-emit vertex bucket size of 0.5 m so boundary
    nodes never collapse onto wall nodes) from every
    ``ROLE_BOUNDARY`` shape.

    Returns the number of tunnel PORTALS emitted (each contributing
    1 cap + 2 arm walls + a ramp chain).
    """
    nodes_r, ways_r, _big_way_ids, _node_tags_r = (
        _load_tunnel_road_network(layout))
    if not ways_r:
        return 0
    # Project nodes to meter space.
    _to_m, _m_to_ll = _local_meter_projections(layout.anchor)
    nodes_m: dict[str, tuple[float, float]] = {}
    for nid, (lat, lon) in nodes_r.items():
        nodes_m[nid] = _to_m(lon, lat)
    # Airside pavement union for the per-portal gate (see the
    # AIRSIDE / DOUBLE-EMIT GATE comment below).
    _AIRSIDE_GATE_ROLES = (
        "runway", "runway_crossing", "primary_parallel",
        "secondary_parallel", "stub", "cross_connector", "junction",
        "apron", "building", "groundside_pavement", "service_road",
        "service_junction")
    try:
        _airside_gate_u = unary_union(
            [s.polygon for s in layout.shapes
             if s.polygon is not None and not s.polygon.is_empty
             and s.role in _AIRSIDE_GATE_ROLES])
        if _airside_gate_u.is_empty:
            _airside_gate_u = None
    except _GEOM_EXC:
        _airside_gate_u = None
    # We walk a generous maximum, then truncate per-portal based
    # on actual DEM at the far end so the resulting grade never
    # exceeds ``max_ramp_grade``.  The truncated length is at
    # least ``ramp_min_length_m`` so even flat-road portals get
    # a recognisable approach.  The planning grade is reduced by
    # 0.005 to leave headroom for the 0.1 m altitude rounding —
    # without it, short segments (e.g. 9 m) could round up to
    # ~4.4 % when the design grade is exactly 4 %.
    plan_grade = max(
        max_ramp_grade - TUNNEL_RAMP_GRADE_SAFETY_MARGIN, 1e-3)
    # A surface gap between two bores shorter than a full down+up ramp
    # pair cannot reach DEM and return — the bores MERGE across it (the
    # road stays below grade the whole way), so this threshold stays
    # purely KINEMATIC.  The DESIGN limit on the open-trench form
    # (``TUNNEL_LOW_CONNECTOR_MAX_OPEN_GAP_M``, user 2026-07-10, SPJC
    # big tunnel) applies inside the synthesizer at gap-RECORD time:
    # capping the merge threshold itself un-merged SPJC's bores and
    # minted phantom mid-gap portals whose ramps dug grooves into the
    # covered ground between the crossings.
    low_connector_max_gap_m = 2.0 * tunnel_depth_m / plan_grade
    ways_r, low_connector_gaps = _synthesize_implied_crossing_bores(
        layout, nodes_m, ways_r, excluded_way_ids,
        low_connector_max_gap_m=low_connector_max_gap_m,
        node_tags=_node_tags_r)
    way_by_id, node_to_ways = _build_surface_way_indices(ways_r)
    arm_walk_max_m = max(arm_max_length_m,
                         ramp_min_length_m,
                         tunnel_depth_m / plan_grade)
    # Helper: ground (DEM) elevation at a local-meter point.  Tunnel
    # retaining walls follow the DEM along their length (user 2026-06-13:
    # "the wall should work similar to the boundary, a chain of rects
    # following DEM elevations") so their top tracks the real ground the
    # trench is cut into, instead of a single flat apt_elev.
    def _dem_at(cx: float, cy: float) -> float | None:
        try:
            lat, lon = _m_to_ll(cx, cy)
            return float(_sample_dem(dem, tile_lat, tile_lon, lat, lon))
        except _GEOM_EXC:
            return None

    # Helper: airport surface elevation at (cx, cy).  Use the
    # boundary-ribbon ``node_altitudes`` (CIFP-anchored, grade-
    # clamped) when a vertex is nearby, else fall back to DEM.
    def _airport_elevation_at(cx: float, cy: float) -> float | None:
        best_d = float('inf')
        best_alt: float | None = None
        for s in layout.shapes:
            if s.role != ROLE_BOUNDARY:
                continue
            if s.ref != "airport_boundary":
                continue
            if not s.node_altitudes:
                continue
            try:
                rcoords = list(s.polygon.exterior.coords)
            except _GEOM_EXC:
                continue
            if rcoords and rcoords[0] == rcoords[-1]:
                rcoords = rcoords[:-1]
            for k, (vx, vy) in enumerate(rcoords):
                if k >= len(s.node_altitudes):
                    break
                d = math.hypot(vx - cx, vy - cy)
                if d < best_d:
                    best_d = d
                    best_alt = s.node_altitudes[k]
        if best_alt is not None and best_d <= 200.0:
            return float(best_alt)
        # Fall back to DEM at the point.
        try:
            lat, lon = _m_to_ll(cx, cy)
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None
    # (The old ROLE_BOUNDARY-distance portal gate lived here — retired
    # 2026-07-04, see the airside-pavement gate in the portal loop.)
    excluded = excluded_way_ids or set()
    # Identity-set of shapes that exist BEFORE this pass emits anything.
    # The under-pavement clip below (PAVEMENT-OVERLAP CLIP) must act
    # ONLY on pieces THIS pass emitted — but it cannot use a captured
    # start INDEX, because the boundary-coordination pass between emit
    # and clip rebuilds ``layout.shapes`` (drops empty ribbon pieces,
    # splits MultiPolygon results) and SHIFTS every index.  On LMML
    # that net-removed 23 shapes, so the first 23 tunnel pieces fell
    # below the stale index and skipped the clip — leaving ramps and
    # flat tunnel walls overlapping the apron.  Identity is stable:
    # the rebuild keeps tunnel pieces by object reference (only
    # ROLE_BOUNDARY shapes are replaced).
    _pre_emit_ids = {id(s) for s in layout.shapes}
    _other_road_lines, _other_road_tree, _tunnel_all_nodes = (
        _build_adjacent_road_index(ways_r, nodes_m,
                                   skip_if_adjacent_road))
    _system_veto = _compute_tunnel_system_veto(
        ways_r, nodes_m, excluded, adjacent_road_dist_m,
        skip_if_adjacent_road, _other_road_lines,
        _other_road_tree, _tunnel_all_nodes)
    portal_data = _gather_portal_walks(
        ways_r, nodes_m, way_by_id, node_to_ways, excluded,
        _system_veto, _big_way_ids, _airside_gate_u,
        max_boundary_dist_m, arm_walk_max_m, carriageway_width_m,
        _airport_elevation_at, _m_to_ll, dem, tile_lat, tile_lon,
        tunnel_depth_m, plan_grade, ramp_min_length_m)
    # Crossing OWNERSHIP (user 2026-07-10): where Feature B claims a
    # crossing (classified deck bridge or tunnel-portal pair), this
    # legacy machinery yields the WHOLE road — a portal whose tunnel
    # way or surface walk touches an owned region is dropped before
    # any dedup/cluster work.  Plan-space plate yield downstream is
    # NOT enough: the surviving approach pieces outside the plates
    # carried DEM values that dragged the mesh beside hard-pinned
    # plates (measured KBNA, taxiway-L and the runway-02C portals).
    _owned_union = _classifier_owned_crossing_union(layout)
    if _owned_union is not None and portal_data:
        _tunnel_way_lines: dict = {}
        for _w_id, _w_refs, _w_tags in ways_r:
            if _w_tags.get("tunnel") not in PORTAL_TUNNEL_VALUES:
                continue
            _way_points = [nodes_m[n] for n in _w_refs if n in nodes_m]
            if len(_way_points) >= 2:
                try:
                    _tunnel_way_lines[_w_id] = LineString(_way_points)
                except _GEOM_EXC:
                    continue
        _kept_portals = []
        _n_owned = 0
        for _portal_entry in portal_data:
            _way_line = _tunnel_way_lines.get(_portal_entry[1])
            _walk_line = None
            try:
                if len(_portal_entry[2]) >= 2:
                    _walk_line = LineString(_portal_entry[2])
            except _GEOM_EXC:
                _walk_line = None
            _owned_hit = False
            for _geometry in (_way_line, _walk_line):
                if _geometry is None:
                    continue
                try:
                    if _geometry.intersects(_owned_union):
                        _owned_hit = True
                        break
                except _GEOM_EXC:
                    continue
            if _owned_hit:
                _n_owned += 1
                continue
            _kept_portals.append(_portal_entry)
        if _n_owned:
            UI.vprint(
                1,
                f"  [tunnel] {_n_owned} legacy portal(s) dropped — "
                "Feature B owns the crossing (classified bridge / "
                "tunnel-portal records)")
            portal_data = _kept_portals
    # A low-connector gap inside an OWNED crossing stays COVERED (user
    # 2026-07-14): the classified portal pair says the road is a deep
    # bore there — digging the gap open leaves holes with no objects
    # over them (measured KBNA: two dug-open gaps exactly on the line
    # between the runway-02C portal mouths).
    if _owned_union is not None and low_connector_gaps:
        _kept_gaps = []
        _n_covered = 0
        for (_gap_line, _corridor_width_m) in low_connector_gaps:
            _inside_owned = False
            try:
                _inside_owned = _gap_line.intersects(_owned_union)
            except _GEOM_EXC:
                _inside_owned = False
            if _inside_owned:
                _n_covered += 1
                continue
            _kept_gaps.append((_gap_line, _corridor_width_m))
        if _n_covered:
            UI.vprint(
                1,
                f"  [tunnel] {_n_covered} low-connector gap(s) kept "
                "COVERED — Feature B owns the crossing (tunnel-portal "
                "pair / classified bridge)")
            low_connector_gaps = _kept_gaps
    # Flat low-connector corridors supersede any portal starting
    # inside them (cross-way facing portals the per-way gap merge
    # cannot see — user 2026-07-04, KDFW).
    _low_corridors = _low_connector_corridors(low_connector_gaps)
    portal_data = _suppress_portals_in_low_corridors(
        portal_data, _low_corridors)
    if not portal_data:
        return 0
    portal_data = _dedup_portal_walks(portal_data)
    if not portal_data:
        return 0
    clusters = _cluster_portals(portal_data, nodes_m,
                                portal_cluster_dist_m)
    # Per-cluster: build cap + arm walls + ramp chain.
    exclusion_zones: list[Polygon] = []
    n_emitted = 0
    half_wall_w = retaining_wall_width_m / 2.0
    for cl in clusters:
        n_emitted += _emit_portal_cluster(
            cl, portal_data, nodes_m, layout, exclusion_zones,
            carriageway_width_m, tunnel_depth_m, wall_gap_m,
            retaining_wall_width_m, half_wall_w, _dem_at,
            airside_gate_union=_airside_gate_u)
    _emit_low_corridor_connectors(
        layout, _low_corridors, exclusion_zones,
        _airside_gate_u, _airport_elevation_at, _dem_at,
        tunnel_depth_m, wall_gap_m, retaining_wall_width_m)
    return _finalize_tunnel_emission(
        layout, exclusion_zones, boundary_clearance_m,
        _airside_gate_u, _pre_emit_ids, n_emitted)


def _scenery_has_bridge_objects(
        layout: "PavementLayout",
        bridge_proximity_m: float = 30.0,
        ) -> bool:
    """Return True when the X-Plane scenery pack containing
    ``layout.apt_dat_path`` places at least one taxi-bridge OBJ
    near a bridge taxi rect.

    Detection logic (per user 2026-04-29):
      1. Find the DSF for the scenery pack via
         ``O4_DSF_Reader.find_associated_dsf``.
      2. Convert it to text (cached alongside the .dsf as
         ``.dsf.text``) using DSFTool when not already cached.
      3. Walk OBJECT_DEF lines.  Mark a def as a "bridge def" when
         its path matches ``bridge|elevated|viaduct|overpass``
         AND does NOT match ``sign|signage|trafficsign|wall|
         truss|crane`` — KPHX has 3 ``lib/g10/roadsigns/SignBridge
         *.obj`` defs that are road sign gantries, NOT taxi
         bridges, and the exclude regex filters them out.
      4. Walk OBJECT placement lines.  When a placement uses a
         "bridge def" AND its lat/lon lies within
         ``bridge_proximity_m`` of any taxi rect with
         ``is_bridge=True``, return True.

    Confirmed signal at the test set:
      KBNA  — 23 OBJECT_DEFs match ``Objects/KBNA Bridges/...``
              (KBNA_Bridge_Taxiway-L_p1..p6, KBNA_Crossing_Bridge,
              elevated_edge_twy_B, ...) — many placements within
              the bridge taxi rect → True.
      KPHX  — only ``lib/g10/roadsigns/SignBridge*`` defs which
              the exclude regex drops → False.

    Feature B gate replacement (``O4_OBJECT_BRIDGE_TERRAIN``): when the
    object-terrain classifier has run (its result cached on the layout by
    ``object_terrain_assembly.attach_bridge_classification``), that
    geometry-based bridge recognition supersedes the name-grep below — it
    catches the EDDF crossings and the Spanish-named KMCO "puente" objects
    the ``bridge|elevated|viaduct|overpass`` regex misses entirely (spec
    section 3.2, step 4).  A pack is treated as carrying its own 3D bridge
    structure when the classifier found ANY bridge record; the legacy
    name-grep runs unchanged whenever the gate is off (no cached result).
    """
    classification = _object_bridge_classification(layout)
    if classification is not None:
        return bool(classification.bridges)
    if not layout.apt_dat_path:
        return False
    bridge_rects = [s.polygon for s in layout.shapes
                     if getattr(s, "is_bridge", False)
                     and s.polygon is not None
                     and not s.polygon.is_empty]
    if not bridge_rects:
        return False
    try:
        from . import dsf_reader as _DSFR
    except _GEOM_EXC:
        return False
    dsf_path = _DSFR.find_associated_dsf(
        layout.apt_dat_path,
        layout.anchor[0], layout.anchor[1])
    if dsf_path is None or not os.path.isfile(dsf_path):
        return False
    text_path = dsf_path + ".text"
    needs_convert = (
        not os.path.isfile(text_path)
        or os.path.getmtime(text_path) < os.path.getmtime(dsf_path))
    if needs_convert:
        tool = _DSFR._dsftool_path()
        if tool is None:
            return False
        try:
            import subprocess as _sp
            _sp.run(
                [tool, "--dsf2text", dsf_path, text_path],
                check=True, capture_output=True, timeout=120)
        except _GEOM_EXC:
            return False
    BRIDGE_RE = re.compile(
        r"(?i)bridge|elevated|viaduct|overpass")
    EXCLUDE_RE = re.compile(
        r"(?i)sign|signage|trafficsign|truss|crane")
    bridge_def_idx: set = set()
    object_def_count = 0
    placements: list[tuple[int, float, float]] = []
    try:
        with open(text_path, "r", encoding="utf-8",
                  errors="replace") as f:
            for line in f:
                if line.startswith("OBJECT_DEF"):
                    parts = line.strip().split(maxsplit=1)
                    path = parts[1] if len(parts) > 1 else ""
                    if (BRIDGE_RE.search(path)
                            and not EXCLUDE_RE.search(path)):
                        bridge_def_idx.add(object_def_count)
                    object_def_count += 1
                elif line.startswith("OBJECT "):
                    tok = line.split()
                    if len(tok) >= 4:
                        try:
                            idx = int(tok[1])
                            lon = float(tok[2])
                            lat = float(tok[3])
                            placements.append((idx, lon, lat))
                        except ValueError:
                            continue
    except _GEOM_EXC:
        return False
    if not bridge_def_idx or not placements:
        return False
    # Project bridge rects to lat/lon for proximity check.  The
    # rects' polygons are in meter space anchored at the layout —
    # convert each placement to meters and test against the
    # buffered rect union.
    _to_m, _m_to_ll = _local_meter_projections(layout.anchor)
    try:
        bridge_buf = unary_union(bridge_rects).buffer(
            bridge_proximity_m)
    except _GEOM_EXC:
        return False
    for idx, lon_v, lat_v in placements:
        if idx not in bridge_def_idx:
            continue
        x, y = _to_m(lon_v, lat_v)
        if bridge_buf.contains(Point(x, y)):
            return True
    return False


def _emit_taxi_bridges(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        retaining_wall_width_m: float = 1.0,
        wall_gap_m: float = 0.5,
        boundary_clearance_m: float = 1.0,
        scenery_has_bridge_objects: bool = False,
        ) -> int:
    """For each taxi rect marked ``is_bridge=True``, optionally
    emit two flat retaining walls along its long edges at the
    rect's average elevation (the bridge deck altitude).

    Per user 2026-04-29 (KBNA vs KPHX): when the X-Plane scenery
    pack ALREADY contains a 3D taxi-bridge OBJ (detected by
    ``_scenery_has_bridge_objects``, e.g. KBNA's
    ``Objects/KBNA Bridges/KBNA_Bridge_Taxiway-L_*.obj``), the
    scenery's own bridge model is the visible structure — we
    skip the wall emission entirely so we don't double up.  When
    the scenery has NO bridge OBJ (KPHX), our emitted walls
    provide the only visible side-face structure.

    No end-cap walls — the bridge's short edges connect to
    adjacent taxis or junctions at apt_elev (the deck continues
    onto the surrounding airport surface), so an end-cap would
    visually block that join.

    Boundary coordination: when a bridge rect lies inside the
    airport boundary (typical at SPJC / KBNA / KPHX), the walls
    are also inside.  The inside-airport portion of (rect ∪
    walls) gets buffered by ``boundary_clearance_m`` (0.5 m) and
    subtracted from each ``ROLE_BOUNDARY`` shape — same pattern
    as ``_emit_tunnel_portals``.

    Returns the number of bridge rects whose walls were emitted
    (0 when the scenery already has bridge OBJs).
    """
    from .pipeline import _load_osm_big_roads
    bridge_shapes = [s for s in layout.shapes
                     if getattr(s, "is_bridge", False)
                     and s.polygon is not None
                     and not s.polygon.is_empty]
    if not bridge_shapes:
        return 0
    if scenery_has_bridge_objects:
        # The scenery's own 3D bridge OBJs are the visible
        # structure.  Emit nothing here — the deck itself
        # already exists as the taxi rect, and the surrounding
        # mesh is handled by the road-approach helper.
        return 0
    n_emitted = 0
    exclusion_zones: list[Polygon] = []
    for s in bridge_shapes:
        rc = list(s.polygon.exterior.coords)
        if rc and rc[0] == rc[-1]:
            rc = rc[:-1]
        if len(rc) != 4:
            continue
        # Per ``_rect_from_axis_extended`` corner convention:
        # corners 0,3 = one short edge, corners 1,2 = other.
        # Long edges: corners (0,1) and (2,3).
        # Compute the deck elevation for the wall: average of
        # altitude_high/altitude_low on a sloped rect, or
        # altitude on a flat rect.  Walls match the deck so the
        # join is seamless.
        if (s.altitude_high is not None
                and s.altitude_low is not None):
            deck_elev = 0.5 * (s.altitude_high + s.altitude_low)
        elif s.altitude is not None:
            deck_elev = s.altitude
        else:
            # No elevation set yet — skip.  The post-elevation
            # pass calls _emit_taxi_bridges after rect altitudes
            # are filled in by the unified solver.
            continue
        for (a, b) in ((0, 1), (2, 3)):
            ax, ay = rc[a]
            bx, by = rc[b]
            ex = bx - ax
            ey = by - ay
            elen = math.hypot(ex, ey)
            if elen < 1.0:
                continue
            ux = ex / elen
            uy = ey / elen
            # Outward normal — flip if the test point is INSIDE
            # the rect.
            n_x = -uy
            n_y = ux
            mid_x = 0.5 * (ax + bx)
            mid_y = 0.5 * (ay + by)
            if s.polygon.contains(
                    Point(mid_x + n_x * 0.1,
                          mid_y + n_y * 0.1)):
                n_x = -n_x
                n_y = -n_y
            # Wall sits ``wall_gap_m`` outboard of the rect's
            # long edge, ``retaining_wall_width_m`` thick.
            inner = wall_gap_m
            outer = wall_gap_m + retaining_wall_width_m
            wc = [
                (ax + n_x * inner, ay + n_y * inner),
                (bx + n_x * inner, by + n_y * inner),
                (bx + n_x * outer, by + n_y * outer),
                (ax + n_x * outer, ay + n_y * outer),
            ]
            try:
                wall_poly = Polygon(wc)
                if not wall_poly.is_valid:
                    wall_poly = wall_poly.buffer(0)
                if (wall_poly.geom_type == "Polygon"
                        and not wall_poly.is_empty):
                    layout.shapes.append(BuiltShape(
                        polygon=wall_poly,
                        role=ROLE_RETAINING_WALL,
                        ref="bridge_wall",
                        altitude=round(float(deck_elev), 1)))
                    exclusion_zones.append(wall_poly)
            except _GEOM_EXC:
                continue
        # Track the bridge rect itself so the boundary subtraction
        # below also clears the deck area.
        exclusion_zones.append(s.polygon)
        n_emitted += 1

    # ── Deck coverage of the FULL tunnel-tagged road segment ──────
    # (user 2026-06-10, KPHX) The taxi-bridge deck is the taxi rect,
    # but the OSM road segment tagged ``tunnel`` often extends past
    # the rect's footprint — that overhang would otherwise be open
    # terrain draped over an underground road (a dirt strip between
    # the deck edge and the portal).  Emit flat deck plates at the
    # adjacent bridge's deck elevation covering every tunnel-tagged
    # segment near a bridge rect, minus the airport pavement that
    # already covers it.
    if n_emitted:
        try:
            nodes_r, ways_r = _load_osm_big_roads(
                layout.anchor[0], layout.anchor[1])
        except _GEOM_EXC:
            nodes_r, ways_r = {}, []
        _to_m, _m_to_ll = _local_meter_projections(layout.anchor)

        tunnel_lines: list[LineString] = []
        for _wid, nrefs, tags in ways_r:
            if not tags.get("highway"):
                continue
            if tags.get("tunnel", "") not in ("yes",
                                              "building_passage"):
                continue
            pts = [_to_m(lon, lat) for n in nrefs
                   if n in nodes_r
                   for (lat, lon) in (nodes_r[n],)]
            if len(pts) < 2:
                continue
            try:
                ls = LineString(pts)
            except _GEOM_EXC:
                continue
            if not ls.is_empty and ls.length >= 2.0:
                tunnel_lines.append(ls)
        if tunnel_lines:
            DECK_HALF_W_M = 12.0     # 22 m road + 1 m overhang each side
            MIN_DECK_PIECE_M2 = 10.0
            try:
                airside_cover = unary_union(
                    [sh.polygon for sh in layout.shapes
                     if sh.polygon is not None
                     and not sh.polygon.is_empty
                     and sh.role in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
                                     ROLE_PRIMARY_PARALLEL,
                                     ROLE_SECONDARY_PARALLEL,
                                     ROLE_STUB, ROLE_CROSS_CONNECTOR,
                                     ROLE_JUNCTION, ROLE_APRON,
                                     ROLE_BUILDING)])
            except _GEOM_EXC:
                airside_cover = None
            n_deck = 0
            deck_union: Polygon | None = None
            for ls in tunnel_lines:
                # Associate with the nearest bridge deck; skip tunnel
                # ways nowhere near a taxi bridge (handled by the
                # portal emitter alone).
                best = None
                best_d = 60.0
                for s in bridge_shapes:
                    try:
                        d = s.polygon.distance(ls)
                    except _GEOM_EXC:
                        continue
                    if d < best_d:
                        best_d = d
                        best = s
                if best is None:
                    continue
                if (best.altitude_high is not None
                        and best.altitude_low is not None):
                    deck_elev = 0.5 * (best.altitude_high
                                       + best.altitude_low)
                elif best.altitude is not None:
                    deck_elev = best.altitude
                else:
                    continue
                try:
                    zone = ls.buffer(DECK_HALF_W_M, cap_style=2,
                                     join_style=2)
                    if airside_cover is not None:
                        zone = zone.difference(airside_cover)
                    # Walls + bridge rects already emitted above
                    # (exclusion_zones) win their footprint — with the
                    # standard 0.5 m standoff, so the deck never shares
                    # an exact edge with a wall (the post-solve feature
                    # conformance would graft wall vertices into a
                    # coincident deck edge and bulge it into an
                    # overlap).
                    if exclusion_zones:
                        zone = zone.difference(
                            unary_union(exclusion_zones)
                            .buffer(wall_gap_m))
                    if deck_union is not None:
                        zone = zone.difference(deck_union)
                except _GEOM_EXC:
                    continue
                for g in (zone.geoms if hasattr(zone, "geoms")
                          else [zone]):
                    if (g.geom_type != "Polygon" or g.is_empty
                            or g.area < MIN_DECK_PIECE_M2):
                        continue
                    layout.shapes.append(BuiltShape(
                        polygon=g,
                        role=ROLE_TUNNEL_RAMP,
                        ref="bridge_deck",
                        altitude=round(float(deck_elev), 1)))
                    exclusion_zones.append(g)
                    n_deck += 1
                    try:
                        deck_union = (g if deck_union is None
                                      else deck_union.union(g))
                    except _GEOM_EXC:
                        pass
            if n_deck:
                UI.vprint(1,
                    f"  [pav-builder] emitted {n_deck} bridge-deck "
                    f"plate(s) covering tunnel-tagged road "
                    f"segment(s).")

    # Boundary coordination: subtract the actual (rect ∪ walls)
    # footprint, buffered by 0.5 m, from each ROLE_BOUNDARY shape.
    # Same pattern as ``_emit_tunnel_portals``.
    if exclusion_zones:
        try:
            bridge_union = unary_union(exclusion_zones)
        except _GEOM_EXC:
            bridge_union = None
        if bridge_union is not None and not bridge_union.is_empty:
            try:
                excl = bridge_union.buffer(boundary_clearance_m)
                kept_shapes: list[BuiltShape] = []
                for s in layout.shapes:
                    if s.role != ROLE_BOUNDARY:
                        kept_shapes.append(s)
                        continue
                    try:
                        _old_ring = list(s.polygon.exterior.coords)
                    except _GEOM_EXC:
                        _old_ring = []
                    if (_old_ring
                            and _old_ring[0] == _old_ring[-1]):
                        _old_ring = _old_ring[:-1]
                    _old_alts = (list(s.node_altitudes)
                                  if s.node_altitudes else None)
                    try:
                        new_poly = s.polygon.difference(excl)
                    except _GEOM_EXC:
                        kept_shapes.append(s)
                        continue
                    if new_poly.is_empty:
                        continue
                    if new_poly.geom_type == "Polygon":
                        s.polygon = new_poly
                        resampled = _resample_node_altitudes_nn(
                            new_poly, _old_ring, _old_alts)
                        if resampled is not None:
                            s.node_altitudes = resampled
                        kept_shapes.append(s)
                    elif new_poly.geom_type == "MultiPolygon":
                        for g in new_poly.geoms:
                            if (g.geom_type != "Polygon"
                                    or g.is_empty
                                    or g.area < 5.0):
                                continue
                            resampled = _resample_node_altitudes_nn(
                                g, _old_ring, _old_alts)
                            kept_shapes.append(BuiltShape(
                                polygon=g, role=s.role,
                                ref=s.ref,
                                altitude=(s.altitude
                                          if resampled is None
                                          else None),
                                node_altitudes=resampled))
                layout.shapes = kept_shapes
            except _GEOM_EXC:
                pass
    return n_emitted


# ---------------------------------------------------------------------------
# Feature B — object-derived depressed-road corridors (spec section 3.2)
#
# When the object-terrain classifier has run (gate O4_OBJECT_BRIDGE_TERRAIN,
# result cached on the layout), the depressed corridor under a DECK_CARRIED
# taxiway bridge is re-sourced from the OBJECT'S OWN geometry — deck
# footprint, deck elevation, girder clearance — rather than inferred from an
# ``is_bridge`` taxi rect + OSM proximity.  TERRAIN_CARRIED and
# PROFILE_CARRIED spans (pavement drapes across, solved continuous) SUPPRESS
# corridor emission inside their footprints.  All of this is dormant with
# the gate off (no cached classification ⇒ every path below is skipped and
# the legacy emitter is byte-identical).
# ---------------------------------------------------------------------------

def _object_bridge_classification(layout):
    """The cached :class:`object_terrain_features.ClassificationResult`, or
    ``None`` when feature B is off or the assembler did not run.  Gated on
    the live config flag so a flipped env/monkeypatched gate is honoured."""
    if not _CFG.OBJECT_BRIDGE_TERRAIN:
        return None
    return getattr(layout, _OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE, None)


def _object_bridge_road_networks(layout):
    """The cached sibling DSF road networks (``[]`` when none discovered)."""
    return getattr(layout, _OBJECT_BRIDGE_ROAD_NETWORKS_ATTRIBUTE, None) or []


def _bridge_deck_elevation_m(bridge, dem, tile_lat, tile_lon):
    """Absolute deck-top elevation for a bridge record.

    ``absolute_deck_elevation_m`` (KBNA's OBJECT_MSL fixtures) when present;
    otherwise the terrain elevation sampled at the anchor plus the deck's
    effective crest height ``deck_top_y_m`` (spec section 3.2 step 1).

    **Anchor-datum caveat:** with no MSL fixture the datum is the DEM value
    at the single placement anchor — a proxy for the solved terrain there.
    The solved pavement network may differ by the solver's grading; this is
    the same one-sampled-anchor-point datum every object-terrain family
    lives with (spec section 3.4 anchor caution).  ``None`` when the DEM
    cannot be sampled."""
    if bridge.absolute_deck_elevation_m is not None:
        return float(bridge.absolute_deck_elevation_m)
    anchor_longitude, anchor_latitude = bridge.anchor_longitude_latitude
    try:
        datum = _sample_dem(
            dem, tile_lat, tile_lon, anchor_latitude, anchor_longitude
        )
    except _GEOM_EXC:
        return None
    if datum is None or datum != datum:
        return None
    return float(datum) + float(bridge.deck_top_y_m)


def _bridge_corridor_floor_m(bridge, deck_elevation_m):
    """The depressed-corridor floor elevation under a deck-carried span —
    GEOMETRY-DRIVEN per amendment A10: **floor = absolute deck elevation −
    hard-deck height above anchor terrain** (`deck_top_y_m`), which is the
    anchor-terrain datum the object was authored against.  KBNA
    calibration: 167.0 − 5.99 ≈ 161.0, matching the author mesh exactly;
    the previous clearance-driven floor (girder − 5.1) over-dug by ~0.9 m.

    The clearance constant is a CHECK, not the driver:
    ``config.BRIDGE_ROAD_CLEARANCE_MINIMUM_M`` (4.2, the measured
    in-the-wild girder clearance) is the validator's acceptance bound on
    floor-to-girder — see :func:`_bridge_girder_underside_m` and the
    emission-time warning in the corridor emitter."""
    return float(deck_elevation_m) - float(bridge.deck_top_y_m)


def _bridge_girder_underside_m(bridge, deck_elevation_m):
    """Absolute elevation of the clearance-limiting underside plane
    (girder line; the slab-underside ``ceiling_y_m`` as fallback), or
    ``None`` when the object exposes no underside plane.  Used by the
    corridor clearance CHECK (amendment A10: floor-to-girder must reach
    ``config.BRIDGE_ROAD_CLEARANCE_MINIMUM_M``), never by the floor
    computation."""
    underside = bridge.clearance_underside_y_m
    if underside is None:
        underside = bridge.ceiling_y_m
    if underside is None:
        return None
    return float(deck_elevation_m) - (
        float(bridge.deck_top_y_m) - float(underside)
    )


def _bridge_is_road_carried(bridge, layout, to_meters):
    """Road-overpass discriminator (stage 2b; KBNA Crossing_Bridge class):
    a deck-carried structure whose deck carries a ROAD, not a taxi/truck
    route — NO layout pavement or service shape crosses its deck
    footprint (measured: the nearest pavement to Crossing_Bridge is
    176 m away; every true taxi/truck bridge has pavement or a truck
    strip on the deck axis).  Terrain must NOT rise to a road deck: no
    abutment pins, no causeway, no object-sourced corridor — the
    existing road machinery owns the road beneath.  ``False`` when no
    layout is available (pure-classifier contexts cannot discriminate)."""
    if layout is None or to_meters is None:
        return False
    from .layout import ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION
    crossing_roles = _BRIDGE_PIN_ROLES | {
        ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION,
    }
    footprint = _bridge_footprint_meters(bridge, to_meters)
    if footprint is None:
        return False
    reach_band = footprint.buffer(
        float(_CFG.BRIDGE_ABUTMENT_PIN_CAPTURE_BAND_M)
    )
    # PRIMARY route evidence (stage 2b iteration 4): the RAW apt.dat
    # routing rows — 1202 taxi edges + 1206 truck edges — cached by the
    # assembler as layout-meter polylines.  The routing GRAPH says what
    # drives over the deck; neither emitted shapes (the Murfreesboro
    # truck strips end 36.7-60.9 m short) nor the QUALIFIED centerline
    # set (the Murfreesboro truck runs are disqualified before reaching
    # ``apt_taxi_centerlines`` — measured: 0 qualified centerlines
    # versus 3/5 raw truck edges in the two decks' reach bands, 3 raw
    # taxi edges at taxiway-L, zero of anything at the Crossing_Bridge
    # road overpass) carry the truth.
    for line in getattr(
            layout, _OBJECT_BRIDGE_ROUTE_LINES_ATTRIBUTE, None) or []:
        if line is None or line.is_empty:
            continue
        try:
            if line.intersects(reach_band):
                return False
        except _GEOM_EXC:
            continue
    # Secondary evidence: the qualified centerline set (for layouts
    # whose apt.dat carries no routing rows but centerlines exist).
    for centerline in getattr(layout, "apt_taxi_centerlines", None) or []:
        line = getattr(centerline, "line", None)
        if line is None or line.is_empty:
            continue
        try:
            if line.intersects(reach_band):
                return False
        except _GEOM_EXC:
            continue
    # Tertiary evidence: a pavement/service shape crossing or ending
    # within the pin capture band of the footprint (the KBNA cut: the
    # pack severs taxiway-L pavement 9.6 m short of the abutments).
    for shape in layout.shapes:
        if shape.role not in crossing_roles:
            continue
        if shape.polygon is None or shape.polygon.is_empty:
            continue
        try:
            if shape.polygon.intersects(reach_band):
                return False
        except _GEOM_EXC:
            continue
    return True


def _ambiguous_span_promoted_by_routes(bridge, layout, to_meters):
    """Dead-band AMBIGUOUS rescue (user 2026-07-17, KBNA Taxiway-L).

    The contract classifier's coverage dead band exists because partial
    mid-deck pavement coverage cannot say whether pavement drapes across
    (terrain-carried) or is cut at the abutments (deck-carried).  The
    KBNA border-line pavement feature (2026-07-16) pushed Taxiway-L's
    mid-deck coverage from ~0 to 0.20 — its wide ``.lin`` taxiway
    border runs across the deck — flipping the span from DECK_CARRIED
    to AMBIGUOUS and silently refusing the whole corridor treatment
    (trench, causeway, pins, approaches: the user-visible "underpass
    trench missing").

    The apt.dat ROUTING GRAPH is the truth about what drives over a
    deck (the ``_bridge_is_road_carried`` doctrine, tiers 1+2: raw
    1202/1206 routing rows, then qualified centerlines — NEVER the
    pavement-proximity tier, because nearby draped pavement is exactly
    what created the ambiguity).  A dead-band AMBIGUOUS span with a
    genuine hard deck and a taxi/truck route crossing it is a taxi
    bridge: promote to the corridor set.  High-coverage AMBIGUOUS
    (the flat-deck-with-draped-pavement contradiction) stays refused
    per ruling R5.
    """
    from .object_terrain_features import (
        BRIDGE_CONTRACT_PAVEMENT_COVERAGE_DECK_CARRIED_MAX,
        BRIDGE_CONTRACT_PAVEMENT_COVERAGE_TERRAIN_CARRIED_MIN,
        CONTRACT_EVIDENCE_PAVEMENT_COVERAGE,
        DECK_HARDNESS_HARD_DECK,
    )
    if layout is None or to_meters is None:
        return False
    if bridge.deck_hardness != DECK_HARDNESS_HARD_DECK:
        return False
    if bridge.contract_evidence != CONTRACT_EVIDENCE_PAVEMENT_COVERAGE:
        return False
    coverage = bridge.pavement_coverage_fraction
    if coverage is None:
        return False
    if not (BRIDGE_CONTRACT_PAVEMENT_COVERAGE_DECK_CARRIED_MAX
            < float(coverage)
            < BRIDGE_CONTRACT_PAVEMENT_COVERAGE_TERRAIN_CARRIED_MIN):
        return False
    footprint = _bridge_footprint_meters(bridge, to_meters)
    if footprint is None:
        return False
    try:
        reach_band = footprint.buffer(
            float(_CFG.BRIDGE_ABUTMENT_PIN_CAPTURE_BAND_M))
    except _GEOM_EXC:
        return False
    for line in getattr(
            layout, _OBJECT_BRIDGE_ROUTE_LINES_ATTRIBUTE, None) or []:
        if line is None or line.is_empty:
            continue
        try:
            if line.intersects(reach_band):
                return True
        except _GEOM_EXC:
            continue
    for centerline in getattr(layout, "apt_taxi_centerlines", None) or []:
        line = getattr(centerline, "line", None)
        if line is None or line.is_empty:
            continue
        try:
            if line.intersects(reach_band):
                return True
        except _GEOM_EXC:
            continue
    return False


def _detect_tunnel_portal_pairs(layout, dem, tile_lat, tile_lon):
    """Pair classified structures that are the two PORTALS of one buried
    tunnel (user ruling 2026-07-10, the KBNA runway-02C class) and cache
    the result on the layout.

    A pair is two would-be corridor records (deck-carried or cosmetic)
    whose centroid spacing lies inside the configured window, whose
    connecting segment aligns with BOTH objects' headings — side-by-side
    parallel decks fail this test because their connecting segment runs
    PERPENDICULAR to their headings — and whose connecting ground rises
    to at least the lower portal's top plus the buried margin (the hill
    that carries the runway over the bore).

    Portals never receive bridge treatment: no deck-end pins, no trench,
    no causeway plates, no cross-hill approaches.  Instead each mouth is
    seated at the ROAD grade so the portal object sits partly submerged
    with its bottom aligned to the road descending to it, the terrain on
    the runway side stays the natural hill (level with the object top at
    the face), and the road corridor climbs AWAY from the mouth.  For
    that, each portal record precomputes:

    * ``outward`` — unit vector away from the partner (out of the hill);
    * ``mouth_floor_m`` — MINIMUM digital-elevation-model sample along
      the outward ray (the descending road's grade at the face, robust
      against the embankment skirt inflating near samples);
    * ``footprint`` — the deck footprint in layout meters.

    Returns the cached pair list (computed once per layout; empty when
    the gate is off, the digital elevation model is unavailable, or no
    pair qualifies)."""
    cached = getattr(layout, _TUNNEL_PORTAL_PAIRS_ATTRIBUTE, None)
    if cached is not None:
        return cached
    pairs: list[dict] = []
    setattr(layout, _TUNNEL_PORTAL_PAIRS_ATTRIBUTE, pairs)
    classification = _object_bridge_classification(layout)
    if classification is None or dem is None:
        return pairs
    from .object_terrain_features import (
        DECK_CARRIED, DECK_HARDNESS_COSMETIC,
    )
    to_meters, meters_to_lat_lon = _local_meter_projections(layout.anchor)
    candidates = []
    for bridge in classification.bridges:
        is_cosmetic = bridge.deck_hardness == DECK_HARDNESS_COSMETIC
        if not (is_cosmetic or bridge.contract == DECK_CARRIED):
            continue
        footprint = _bridge_footprint_meters(bridge, to_meters)
        if footprint is None:
            continue
        candidates.append((bridge, footprint))
    # Portal-FACE candidates (user 2026-07-17, EGGW class): bare soft
    # face quads hanging below grade, recognized by the classifier as
    # ``portal_faces``.  They pair only with EACH OTHER (a face and a
    # cosmetic deck are different physical modelling conventions).
    # Owner ruling 2026-07-18: a recognized face pair OWNS its crossing
    # and receives the KBNA-style mouth/crown/collar treatment ALIGNED
    # ON THE MOUTH ANCHORS — a mapped OSM ``tunnel=yes`` way between
    # the faces is corroboration that strengthens the pair, never a
    # reason to stand down (the OSM-side emitters yield through the
    # crossing-ownership union, exactly as they do for KBNA pairs).
    #
    # A face's own horizontal projection is a sliver (a vertical quad
    # projects to its face LINE), useless for the mouth/crown split and
    # the collar — synthesize the KBNA-shaped plate footprint instead:
    # a rectangle CENTERED ON THE ANCHOR (the anchor sits on the face
    # line), spanning the face width plus a shoulder, reaching
    # half-depth outward (the mouth half) and half-depth inward (the
    # buried crown half).
    face_candidates = []
    _mapped_tunnel_lines_m: list = []
    for face in getattr(classification, "portal_faces", None) or []:
        try:
            anchor_x, anchor_y = to_meters(
                face.anchor_longitude_latitude[0],
                face.anchor_longitude_latitude[1])
            line_bearing = math.radians(
                float(getattr(face, "face_line_bearing_degrees", 0.0)))
            along = (math.sin(line_bearing), math.cos(line_bearing))
            across = (-along[1], along[0])
            half_width = (0.5 * float(face.face_width_m)
                          + float(_CFG.PORTAL_FACE_PLATE_SHOULDER_M))
            half_depth = 0.5 * float(_CFG.PORTAL_FACE_PLATE_DEPTH_M)
            footprint = Polygon([
                (anchor_x + along[0] * half_width
                 + across[0] * half_depth,
                 anchor_y + along[1] * half_width
                 + across[1] * half_depth),
                (anchor_x - along[0] * half_width
                 + across[0] * half_depth,
                 anchor_y - along[1] * half_width
                 + across[1] * half_depth),
                (anchor_x - along[0] * half_width
                 - across[0] * half_depth,
                 anchor_y - along[1] * half_width
                 - across[1] * half_depth),
                (anchor_x + along[0] * half_width
                 - across[0] * half_depth,
                 anchor_y + along[1] * half_width
                 - across[1] * half_depth),
            ])
            if not footprint.is_valid:
                footprint = footprint.buffer(0)
            if footprint.geom_type != "Polygon" or footprint.is_empty:
                continue
        except _GEOM_EXC:
            continue
        face_candidates.append((face, footprint))
    if face_candidates:
        try:
            nodes_r, ways_r, _big_ids, _ntags = (
                _load_tunnel_road_network(layout))
            for _wid, _nrefs, _tags in ways_r:
                if _tags.get("tunnel") not in TUNNEL_VALUES:
                    continue
                _pts = []
                for _nref in _nrefs:
                    if _nref in nodes_r:
                        _lat, _lon = nodes_r[_nref]
                        _pts.append(to_meters(_lon, _lat))
                if len(_pts) >= 2:
                    _mapped_tunnel_lines_m.append(LineString(_pts))
        except _GEOM_EXC:
            _mapped_tunnel_lines_m = []
    if len(candidates) < 2 and len(face_candidates) < 2:
        return pairs

    def _dem_at_meters(x, y):
        try:
            lat, lon = meters_to_lat_lon(x, y)
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None

    def _mouth_floor(footprint, outward_x, outward_y):
        centroid = footprint.centroid
        edge_distance = None
        probe = 0.0
        while probe <= 400.0:
            point = Point(centroid.x + outward_x * probe,
                          centroid.y + outward_y * probe)
            if not footprint.covers(point):
                edge_distance = probe
                break
            probe += 5.0
        if edge_distance is None:
            return None
        samples = []
        distance = 5.0
        while distance <= float(_CFG.TUNNEL_PORTAL_MOUTH_SAMPLE_RANGE_M):
            value = _dem_at_meters(
                centroid.x + outward_x * (edge_distance + distance),
                centroid.y + outward_y * (edge_distance + distance))
            if value is not None:
                samples.append(value)
            distance += 5.0
        return min(samples) if samples else None

    all_candidates = (
        [(bridge, footprint, False) for bridge, footprint in candidates]
        + [(face, footprint, True) for face, footprint in face_candidates])
    used: set[int] = set()
    for i, (bridge_i, footprint_i, is_face_i) in enumerate(all_candidates):
        if id(bridge_i) in used:
            continue
        for bridge_j, footprint_j, is_face_j in all_candidates[i + 1:]:
            if id(bridge_j) in used:
                continue
            if is_face_i != is_face_j:
                continue
            centroid_i = footprint_i.centroid
            centroid_j = footprint_j.centroid
            east = centroid_j.x - centroid_i.x
            north = centroid_j.y - centroid_i.y
            spacing = math.hypot(east, north)
            if not (float(_CFG.TUNNEL_PORTAL_PAIR_MIN_SPACING_M)
                    <= spacing
                    <= float(_CFG.TUNNEL_PORTAL_PAIR_MAX_SPACING_M)):
                continue
            segment_bearing = math.degrees(math.atan2(east, north)) % 180.0
            tolerance = float(
                _CFG.TUNNEL_PORTAL_PAIR_HEADING_TOLERANCE_DEGREES)
            if is_face_i:
                # Face pairs: a portal face parallels the STRUCTURE it
                # passes under, not the road's perpendicular (EGGW: the
                # taxiway crosses the road at ~58°), so test (a) the
                # two faces are mutually parallel — two ends of one
                # structure — and (b) the connecting segment genuinely
                # CROSSES the faces (never runs along them: side-by-
                # side faces of neighbouring structures fail here).
                line_i = float(getattr(
                    bridge_i, "face_line_bearing_degrees", 0.0)) % 180.0
                line_j = float(getattr(
                    bridge_j, "face_line_bearing_degrees", 0.0)) % 180.0
                parallel_delta = abs(line_i - line_j)
                if parallel_delta > 90.0:
                    parallel_delta = 180.0 - parallel_delta
                # Mean face line, wraparound-safe (5° and 175° must
                # average near 0, not 90).
                adjusted_j = line_j
                if abs(line_j - line_i) > 90.0:
                    adjusted_j += 180.0 if line_j < line_i else -180.0
                mean_line = (0.5 * (line_i + adjusted_j)) % 180.0
                crossing_delta = abs(segment_bearing - mean_line)
                if crossing_delta > 90.0:
                    crossing_delta = 180.0 - crossing_delta
                if parallel_delta > tolerance \
                        or crossing_delta < tolerance:
                    continue
            else:
                aligned = True
                for bridge in (bridge_i, bridge_j):
                    heading = float(bridge.heading_degrees or 0.0) % 180.0
                    delta = abs(heading - segment_bearing)
                    if delta > 90.0:
                        delta = 180.0 - delta
                    if delta > tolerance:
                        aligned = False
                        break
                if not aligned:
                    continue
            unit_x = east / spacing
            unit_y = north / spacing
            mouth_i = _mouth_floor(footprint_i, -unit_x, -unit_y)
            mouth_j = _mouth_floor(footprint_j, unit_x, unit_y)
            if mouth_i is None or mouth_j is None:
                continue
            top_i = mouth_i + float(bridge_i.deck_top_y_m or 0.0)
            top_j = mouth_j + float(bridge_j.deck_top_y_m or 0.0)
            # Buried test, two independent signals (either qualifies):
            #
            # (a) AIRSIDE PAVEMENT crosses the middle of the connecting
            #     segment — a road running between two portal structures
            #     UNDER a runway/taxiway is a tunnel by the same axiom
            #     the implied-crossing machinery rests on (roads never
            #     cross airside pavement at grade).  This signal is
            #     immune to the smoothed digital elevation model, which
            #     FLATTENS embankments near runways (measured KBNA 02C:
            #     mid-line maximum 181.45 m against portal tops 187-188 —
            #     the raster erased the hill the runway visibly sits on).
            # (b) The connecting ground rises above the lower portal's
            #     top (unsmoothed hills away from pavement).
            buried = False
            try:
                middle = LineString([
                    (centroid_i.x, centroid_i.y),
                    (centroid_j.x, centroid_j.y),
                ]).difference(
                    footprint_i.buffer(20.0)
                ).difference(footprint_j.buffer(20.0))
            except _GEOM_EXC:
                middle = None
            if middle is not None and not middle.is_empty:
                for shape in layout.shapes:
                    if (shape.role not in _BRIDGE_PIN_ROLES
                            or shape.polygon is None
                            or shape.polygon.is_empty):
                        continue
                    try:
                        if shape.polygon.intersects(middle):
                            buried = True
                            break
                    except _GEOM_EXC:
                        continue
            mid_max = None
            if not buried:
                distance = 0.0
                while distance <= spacing:
                    value = _dem_at_meters(
                        centroid_i.x + unit_x * distance,
                        centroid_i.y + unit_y * distance)
                    if value is not None and (mid_max is None
                                              or value > mid_max):
                        mid_max = value
                    distance += 10.0
                buried = mid_max is not None and mid_max >= (
                    min(top_i, top_j)
                    + float(_CFG.TUNNEL_PORTAL_PAIR_BURIED_MARGIN_M))
            if not buried:
                continue
            osm_corroborated = False
            if is_face_i and _mapped_tunnel_lines_m:
                # Owner ruling 2026-07-18: a mapped OSM bore between
                # the faces CORROBORATES the pair (it never stands the
                # pair down — the pair owns the crossing and the OSM
                # emitters yield through the crossing-ownership union).
                try:
                    connecting = LineString([
                        (centroid_i.x, centroid_i.y),
                        (centroid_j.x, centroid_j.y),
                    ]).buffer(20.0)
                    osm_corroborated = any(
                        line.intersects(connecting)
                        for line in _mapped_tunnel_lines_m)
                except _GEOM_EXC:
                    osm_corroborated = False
            pairs.append({
                "portals": (
                    {"bridge": bridge_i, "footprint": footprint_i,
                     "outward": (-unit_x, -unit_y),
                     "mouth_floor_m": mouth_i},
                    {"bridge": bridge_j, "footprint": footprint_j,
                     "outward": (unit_x, unit_y),
                     "mouth_floor_m": mouth_j},
                ),
                "spacing_m": spacing,
                "is_face": bool(is_face_i),
                "osm_corroborated": osm_corroborated,
            })
            used.add(id(bridge_i))
            used.add(id(bridge_j))
            buried_evidence = (
                "airside pavement over the body" if mid_max is None
                else f"hill to {mid_max:.1f} m")
            if osm_corroborated:
                buried_evidence += ", mapped OSM tunnel corroborates"
            UI.vprint(
                1,
                "   [object-tunnel] portal pair recognized "
                f"({spacing:.0f} m apart, {buried_evidence}, "
                f"mouth floors {mouth_i:.1f} / {mouth_j:.1f} m): "
                f"{bridge_i.object_resources} + "
                f"{bridge_j.object_resources} — bridge treatment "
                "suppressed, mouths seated at road grade",
            )
            break
    return pairs


def _tunnel_portal_ids(layout) -> set[int]:
    """Identity set of classifier records consumed as tunnel portals.
    Empty when detection has not run yet — callers then see the plain
    bridge partition (the pre-pairing behavior); the build always runs
    detection first (``build_bridge_layout_shapes``)."""
    pairs = getattr(layout, _TUNNEL_PORTAL_PAIRS_ATTRIBUTE, None) or []
    ids: set[int] = set()
    for pair in pairs:
        for portal in pair["portals"]:
            ids.add(id(portal["bridge"]))
    return ids


def _partition_bridges_for_corridors(classification, layout=None):
    """Split the classifier's bridge records into the corridor set, the
    suppression set, the refused (ambiguous) set and — with a layout to
    read routes from — the road-carried overpass set (spec section 3.2,
    stage 2b).

    * corridor — DECK_CARRIED spans, plus every cosmetic (``hard_deck``-less
      Murfreesboro-class) deck regardless of its coverage contract: trucks
      ride the terrain there so the causeway-plus-corridor is mandatory.
    * suppress — TERRAIN_CARRIED / PROFILE_CARRIED spans (pavement drapes
      across and is already solved continuous; a corridor would break it).
    * refused — AMBIGUOUS spans (ruling R5: reported, never guessed).
    * road_carried — corridor-shaped spans with NO taxi/truck route
      crossing the deck footprint (``_bridge_is_road_carried``): a road
      overpass; excluded from pins, causeway and the object-sourced
      corridor, logged, left to the existing road machinery.
    * tunnel_portals — records consumed as the paired portals of one
      buried tunnel (``_detect_tunnel_portal_pairs``): no bridge
      treatment; the mouth seating and outward corridor are emitted by
      the portal branch instead."""
    from .object_terrain_features import (
        DECK_CARRIED, TERRAIN_CARRIED, PROFILE_CARRIED, AMBIGUOUS,
        DECK_HARDNESS_COSMETIC,
    )
    to_meters = None
    portal_ids: set[int] = set()
    if layout is not None:
        to_meters, _meters_to_lat_lon = (
            _local_meter_projections(layout.anchor)
        )
        portal_ids = _tunnel_portal_ids(layout)
    corridor: list = []
    suppress: list = []
    refused: list = []
    road_carried: list = []
    tunnel_portals: list = []
    for bridge in classification.bridges:
        is_cosmetic = bridge.deck_hardness == DECK_HARDNESS_COSMETIC
        if id(bridge) in portal_ids:
            tunnel_portals.append(bridge)
        elif is_cosmetic or bridge.contract == DECK_CARRIED:
            if _bridge_is_road_carried(bridge, layout, to_meters):
                road_carried.append(bridge)
                UI.vprint(
                    1,
                    "   [object-bridge] road-carried overpass (no "
                    "taxi/truck route on the deck): "
                    f"{bridge.object_resources} — no pins, no causeway, "
                    "road machinery owns the corridor",
                )
            else:
                corridor.append(bridge)
        elif bridge.contract in (TERRAIN_CARRIED, PROFILE_CARRIED):
            suppress.append(bridge)
        elif bridge.contract == AMBIGUOUS:
            if _ambiguous_span_promoted_by_routes(
                    bridge, layout, to_meters):
                corridor.append(bridge)
                UI.vprint(
                    1,
                    "   [object-bridge] dead-band AMBIGUOUS span "
                    "promoted to corridor (taxi/truck route crosses "
                    "the hard deck; partial coverage is draped "
                    f"decoration): {bridge.object_resources}",
                )
            else:
                refused.append(bridge)
    return corridor, suppress, refused, road_carried, tunnel_portals


def _cut_pavement_over_hard_deck(layout, footprint) -> int:
    """Ruling R8 flush seating: cut every taxi/junction/apron/service
    pavement shape by a genuine ``ATTR_hard_deck`` span footprint — the
    object sits flush and carries the drivable surface between the
    abutments (the pattern the KBNA author already uses in the source;
    auto_patch rebuilds pavement from centerlines and must repeat the
    cut).  Solved per-vertex values on the surviving pieces are
    preserved by nearest-neighbour resampling (the boundary-cut pattern
    above).  Returns the number of shapes cut."""
    from .layout import ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION
    cut_roles = _BRIDGE_PIN_ROLES | {
        ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION,
    }
    n_cut = 0
    kept_shapes: list[BuiltShape] = []
    for shape in layout.shapes:
        if (shape.role not in cut_roles
                or shape.polygon is None
                or shape.polygon.is_empty):
            kept_shapes.append(shape)
            continue
        try:
            if not shape.polygon.intersects(footprint):
                kept_shapes.append(shape)
                continue
        except _GEOM_EXC:
            kept_shapes.append(shape)
            continue
        try:
            old_ring = list(shape.polygon.exterior.coords)
        except _GEOM_EXC:
            old_ring = []
        if old_ring and old_ring[0] == old_ring[-1]:
            old_ring = old_ring[:-1]
        old_altitudes = (
            list(shape.node_altitudes) if shape.node_altitudes else None
        )
        try:
            remainder = shape.polygon.difference(footprint)
        except _GEOM_EXC:
            kept_shapes.append(shape)
            continue
        n_cut += 1
        if remainder.is_empty:
            continue  # the shape lay entirely on the deck — removed
        parts = (
            list(remainder.geoms)
            if remainder.geom_type == "MultiPolygon" else [remainder]
        )
        for part in parts:
            if (part.geom_type != "Polygon" or part.is_empty
                    or part.area < 5.0):
                continue
            resampled = _resample_node_altitudes_nn(
                part, old_ring, old_altitudes
            )
            kept_shapes.append(BuiltShape(
                polygon=part,
                role=shape.role,
                ref=shape.ref,
                altitude=(shape.altitude if resampled is None else None),
                altitude_high=(
                    None if resampled is not None else shape.altitude_high),
                altitude_low=(
                    None if resampled is not None else shape.altitude_low),
                node_altitudes=resampled,
                is_bridge=getattr(shape, "is_bridge", False)))
    if n_cut:
        layout.shapes = kept_shapes
    return n_cut


def _bridge_footprint_meters(bridge, to_meters):
    """Project a bridge's deck footprint to a local-meter shapely polygon
    (``None`` on degenerate geometry)."""
    if bridge.deck_polygon is None:
        return None
    from .object_terrain_features import frame_polygon_to_longitude_latitude
    footprint_longitude_latitude = frame_polygon_to_longitude_latitude(
        bridge.deck_polygon, bridge.frame_origin_longitude_latitude
    )
    parts = (
        list(footprint_longitude_latitude.geoms)
        if footprint_longitude_latitude.geom_type == "MultiPolygon"
        else [footprint_longitude_latitude]
    )
    meter_polygons: list[Polygon] = []
    for part in parts:
        ring = [to_meters(lon, lat) for lon, lat in part.exterior.coords]
        if len(ring) < 3:
            continue
        try:
            polygon = Polygon(ring)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if polygon.geom_type == "Polygon" and not polygon.is_empty:
                meter_polygons.append(polygon)
        except _GEOM_EXC:
            continue
    if not meter_polygons:
        return None
    try:
        union = unary_union(meter_polygons)
    except _GEOM_EXC:
        return meter_polygons[0]
    if union.geom_type == "MultiPolygon":
        union = max(union.geoms, key=lambda geometry: geometry.area)
    return union if union.geom_type == "Polygon" else None


def _draped_road_centerlines_meters(bridge, road_networks, to_meters):
    """Fully-draped (level-0) road centerlines crossing a bridge footprint,
    from the sibling DSF road networks, as local-meter LineStrings (spec
    section 3.2 step 3 — an elevated ramp flies over on its own structure
    and is left alone; only draped roads want a depressed corridor)."""
    if bridge.deck_polygon is None or not road_networks:
        return []
    from .object_terrain_features import frame_polygon_to_longitude_latitude
    footprint_longitude_latitude = frame_polygon_to_longitude_latitude(
        bridge.deck_polygon, bridge.frame_origin_longitude_latitude
    )
    if footprint_longitude_latitude.geom_type == "MultiPolygon":
        footprint_longitude_latitude = max(
            footprint_longitude_latitude.geoms,
            key=lambda geometry: geometry.area,
        )
    ring = list(footprint_longitude_latitude.exterior.coords)
    lines: list[LineString] = []
    for network in road_networks:
        for segment in dsf_road_network.segments_crossing(network, ring):
            if not segment.is_fully_draped:
                continue
            points = [
                to_meters(point.longitude, point.latitude)
                for point in segment.shape_points
            ]
            if len(points) < 2:
                continue
            try:
                line = LineString(points)
            except _GEOM_EXC:
                continue
            if not line.is_empty and line.length >= 5.0:
                lines.append(line)
    return lines


def _dedup_parallel_road_lines(road_lines, half_width):
    """Twin carriageways give two near-coincident centerlines through one
    crossing — one full-width approach corridor each, overlapping in the
    written patch (the tunnel path's ``_dedup_portal_walks`` guard,
    applied at the road-line level).  Keep the longest line; drop any
    later line whose buffered corridor overlaps a kept corridor by more
    than half its own area."""
    kept: list = []
    kept_buffers: list = []
    for line in sorted(road_lines, key=lambda item: -item.length):
        try:
            buffered = line.buffer(half_width)
        except _GEOM_EXC:
            continue
        duplicate = False
        for other in kept_buffers:
            try:
                if (buffered.intersection(other).area
                        > 0.5 * buffered.area):
                    duplicate = True
                    break
            except _GEOM_EXC:
                continue
        if duplicate:
            continue
        kept.append(line)
        kept_buffers.append(buffered)
    return kept


def _split_portal_footprint(footprint, outward):
    """Split a paired portal's footprint at its centroid, perpendicular
    to the mouth (``outward``) direction: ``(mouth_half, buried_half,
    mouth_half_plane)``.  The mouth half faces the open road; the buried
    half faces the tunnel body under the runway; the half-plane is
    returned so the crown COLLAR can be clipped to the buried side.
    Halves may be ``None`` when the split degenerates (the caller then
    falls back to the whole footprint at road grade)."""
    try:
        centroid = footprint.centroid
        outward_x, outward_y = outward
        perpendicular_x, perpendicular_y = -outward_y, outward_x
        reach = 1000.0
        edge_a = (centroid.x + perpendicular_x * reach,
                  centroid.y + perpendicular_y * reach)
        edge_b = (centroid.x - perpendicular_x * reach,
                  centroid.y - perpendicular_y * reach)
        mouth_half_plane = Polygon([
            edge_a,
            edge_b,
            (edge_b[0] + outward_x * 2.0 * reach,
             edge_b[1] + outward_y * 2.0 * reach),
            (edge_a[0] + outward_x * 2.0 * reach,
             edge_a[1] + outward_y * 2.0 * reach),
        ])
        mouth_half = footprint.intersection(mouth_half_plane)
        buried_half = footprint.difference(mouth_half_plane)
    except _GEOM_EXC:
        return None, None, None

    def _largest_polygon(geometry):
        if geometry is None or geometry.is_empty:
            return None
        if geometry.geom_type == "Polygon":
            return geometry
        polygons = [part for part in getattr(geometry, "geoms", [])
                    if part.geom_type == "Polygon" and not part.is_empty]
        return max(polygons, key=lambda part: part.area) if polygons \
            else None

    return (_largest_polygon(mouth_half), _largest_polygon(buried_half),
            mouth_half_plane)


def _clip_collar_to_mouth_front(collar_geometry, footprint, outward):
    """Remove any collar region on the ROAD side of the mouth face
    (round-8 fact 3).

    The mouth face is the plane through ``footprint``'s forward-most
    extent along ``outward`` (the mouth->road direction), perpendicular
    to it — ``footprint`` is whichever reference polygon defines the
    face in the caller's frame (the object footprint against the pair
    axis; the emitted mouth plate against the opening axis).  The mouth
    plate and the road approaches own everything ahead of that face, so
    the collar keeps only the back and the lateral flanks up to the
    face.  The forward sweep already removes the band directly ahead of
    the footprint, but a diagonal lateral lobe can round-buffer past its
    end (measured KBNA 02C round-8 baseline: a 34 m2 collar lobe 46.7 m
    in front of the west mouth, flat at the crown elevation — a
    free-standing ~6 m face)."""
    try:
        front_extent = max(
            vertex_x * outward[0] + vertex_y * outward[1]
            for vertex_x, vertex_y in footprint.exterior.coords
        ) + 1.0
        centroid = footprint.centroid
    except _GEOM_EXC:
        return collar_geometry
    perpendicular = (-outward[1], outward[0])
    # Anchor the clip rectangle ON the footprint: project the centroid
    # onto the front line.  ``outward * front_extent`` is on the same
    # line but can sit KILOMETERS away laterally (layout meters are
    # anchored at the airport reference, not the portal), leaving the
    # finite rectangle to miss the site entirely — the exact silent
    # no-op measured at KBNA 02C (footprint 2070 m lateral of that
    # base point against a 2000 m reach).
    centroid_forward = centroid.x * outward[0] + centroid.y * outward[1]
    base = (centroid.x + outward[0] * (front_extent - centroid_forward),
            centroid.y + outward[1] * (front_extent - centroid_forward))
    reach = 2000.0
    mouth_front_plane = Polygon([
        (base[0] + perpendicular[0] * reach,
         base[1] + perpendicular[1] * reach),
        (base[0] - perpendicular[0] * reach,
         base[1] - perpendicular[1] * reach),
        (base[0] - perpendicular[0] * reach + outward[0] * reach,
         base[1] - perpendicular[1] * reach + outward[1] * reach),
        (base[0] + perpendicular[0] * reach + outward[0] * reach,
         base[1] + perpendicular[1] * reach + outward[1] * reach),
    ])
    try:
        return collar_geometry.difference(mouth_front_plane)
    except _GEOM_EXC:
        return collar_geometry


def _portal_pair_owned_polygons(pairs):
    """Plan-space region a tunnel portal pair OWNS: both portal
    footprints plus the connecting band over the buried body.  Approach
    rects must never land here (the hill carries the runway), and the
    legacy portal machinery yields the whole crossing to the pair."""
    polygons: list = []
    for pair in pairs:
        portal_a, portal_b = pair["portals"]
        polygons.append(portal_a["footprint"])
        polygons.append(portal_b["footprint"])
        centroid_a = portal_a["footprint"].centroid
        centroid_b = portal_b["footprint"].centroid
        half_width = 10.0 + 0.5 * max(
            math.sqrt(portal_a["footprint"].area),
            math.sqrt(portal_b["footprint"].area),
        )
        try:
            polygons.append(
                LineString([(centroid_a.x, centroid_a.y),
                            (centroid_b.x, centroid_b.y)]
                           ).buffer(half_width)
            )
        except _GEOM_EXC:
            continue
    return polygons


def _classifier_owned_crossing_union(layout):
    """Union (layout meters) of every crossing Feature B owns — corridor
    deck boxes plus tunnel-portal-pair regions (footprints and the band
    over the buried body).  The legacy OSM / implied-crossing portal
    machinery yields these crossings entirely (user 2026-07-10): its
    DEM-referenced ramps otherwise land beside hard-pinned plates at
    foreign values (measured KBNA: legacy ramps at 173.9-177.2 m over
    the 167.0 m taxiway-L plates).  ``None`` when the gate is off or
    nothing is classified.  Road-carried overpasses are deliberately
    NOT owned — the road machinery keeps those (spec section 3.2)."""
    classification = _object_bridge_classification(layout)
    if classification is None:
        return None
    to_meters, _meters_to_lat_lon = _local_meter_projections(layout.anchor)
    corridor_bridges, _suppress, _refused, _road_carried, _portals = (
        _partition_bridges_for_corridors(classification, layout)
    )
    polygons: list = []
    for bridge in corridor_bridges:
        box = _bridge_deck_box_meters(bridge, layout)
        if box is None:
            box = _bridge_footprint_meters(bridge, to_meters)
        if box is not None:
            polygons.append(box)
    pairs = getattr(layout, _TUNNEL_PORTAL_PAIRS_ATTRIBUTE, None) or []
    polygons.extend(_portal_pair_owned_polygons(pairs))
    # The portal CROWN/COLLAR ring is the object's terrain story too: a
    # transition from the crown height at the object back down to the
    # surrounding ground.  Adjacent-ground bands and clearance cuts must
    # be masked off it as well, or a band welds to the collar's outer rim
    # and then cuts its own graded corridor DOWN to the law floor right
    # beside it — reinstating the very cliff the collar exists to remove
    # (measured KBNA 02C: a band dropping 6.9-7.8 m over ~1.3 m off the
    # collar's welded edge).  Own a collar-reach ring around each portal
    # footprint so those bands start OUTSIDE it, from the natural ground,
    # instead of severing the collar's feather.
    collar_ring_m = float(_CFG.TUNNEL_PORTAL_CROWN_COLLAR_M) + 2.0
    for pair in pairs:
        for portal in pair.get("portals", ()):  # type: ignore[union-attr]
            footprint = portal.get("footprint")
            if footprint is None or footprint.is_empty:
                continue
            try:
                polygons.append(footprint.buffer(collar_ring_m))
            except _GEOM_EXC:
                continue
    if not polygons:
        return None
    try:
        return unary_union(polygons)
    except _GEOM_EXC:
        return None


def _emit_object_sourced_bridge_corridors(
        layout, dem, tile_lat, tile_lon, classification, road_networks,
        road_width_m, ramp_step_m, approach_length_m):
    """Emit depressed-road corridors under DECK_CARRIED bridge spans from
    the object records, and return ``(count, suppression_polygons_meters,
    covered_polygons_meters)`` for the legacy emitter to honour.

    Road source per span (spec section 3.2 step 3): the sibling DSF road
    network's fully-draped segments crossing the footprint, else the OSM
    big-roads fallback (unchanged from legacy).  The corridor floor is
    geometry-driven (``_bridge_corridor_floor_m``, amendment A10 —
    the anchor-terrain datum, with the girder clearance CHECKED against
    ``config.BRIDGE_ROAD_CLEARANCE_MINIMUM_M``); the depressed approach
    walks extend ``config.BRIDGE_CORRIDOR_DEPRESSED_LENGTH_M`` (240 m,
    the author-mesh measurement) per side — the caller's
    ``approach_length_m`` acts only as a wider override."""
    to_meters, meters_to_lat_lon = _local_meter_projections(layout.anchor)
    (corridor_bridges, suppress_bridges, refused_bridges, _road_carried,
     _tunnel_portal_records) = (
        _partition_bridges_for_corridors(classification, layout)
    )
    suppression_polygons: list[Polygon] = []
    covered_polygons: list[Polygon] = []
    for bridge in suppress_bridges:
        footprint = _bridge_footprint_meters(bridge, to_meters)
        if footprint is not None:
            suppression_polygons.append(footprint)
            UI.vprint(
                2,
                "   [object-bridge] corridor suppressed inside "
                f"{bridge.contract} span {bridge.object_resources}",
            )
    for bridge in refused_bridges:
        UI.vprint(
            2,
            "   [object-bridge] AMBIGUOUS span refused a corridor "
            f"(ruling R5): {bridge.object_resources}",
        )

    # Iteration 5 (audit 5): approach step rects must NEVER intrude
    # into a deck box or a causeway zone — the 240 m walks follow curved
    # interchange roads that loop BACK over the span (measured: approach
    # rects at 161-167.4 covered half the J/R centerline and both lip
    # lines, fighting the trench/causeway surfaces in the mesh).  The
    # keep-out is every corridor bridge's footprint plus its two
    # causeway zones at full cap length.
    keep_out_zones = []
    for bridge in corridor_bridges:
        footprint = _bridge_deck_box_meters(bridge, layout)
        if footprint is None:
            footprint = _bridge_footprint_meters(bridge, to_meters)
        if footprint is None:
            continue
        bridge_zones = [footprint]
        centroid = footprint.centroid
        for line in _abutment_lines_layout_meters(
                bridge, layout, extension_fraction=0.0):
            midpoint = line.interpolate(0.5, normalized=True)
            outward_x = midpoint.x - centroid.x
            outward_y = midpoint.y - centroid.y
            norm = math.hypot(outward_x, outward_y)
            if norm < 1.0:
                continue
            outward_x /= norm
            outward_y /= norm
            (ax, ay), (bx, by) = list(line.coords)[0], list(line.coords)[-1]
            reach = float(_CFG.BRIDGE_CAUSEWAY_MAX_LENGTH_M)
            bridge_zones.append(Polygon([
                (ax, ay), (bx, by),
                (bx + outward_x * reach, by + outward_y * reach),
                (ax + outward_x * reach, ay + outward_y * reach),
            ]))
        try:
            bridge_keep_out = unary_union(bridge_zones)
        except _GEOM_EXC:
            keep_out_zones.extend(bridge_zones)
            continue
        # Round 10: the road-exit lane stays OPEN — a road leaving the
        # span through an abutment end descends through the causeway
        # gap, so its approach rects must be allowed in the lane while
        # the rest of the box/causeway zones stay excluded.  The trench
        # is re-excluded afterwards (its plate owns the under-span
        # ground; approaches begin outside it).
        exit_corridor = _road_exit_corridor_meters(
            bridge, layout, to_meters
        )
        if exit_corridor is not None:
            try:
                # The lane opens WIDER than the draped-carriageway
                # corridor itself (user ruling 2026-07-14c): the
                # approach ramps now run at the TRENCH width (~53 m at
                # KBNA Donelson against a 34-38 m carriageway
                # corridor), and a lane only as wide as the corridor
                # starves every rect of the wide chain.
                bridge_keep_out = bridge_keep_out.difference(
                    exit_corridor.buffer(12.0)
                )
                trench_zone = footprint.buffer(-_TRENCH_INSET_M)
                if not trench_zone.is_empty:
                    bridge_keep_out = bridge_keep_out.union(trench_zone)
            except _GEOM_EXC:
                pass
        keep_out_zones.append(bridge_keep_out)
    # Tunnel portal pairs own their footprints AND the connecting band
    # over the buried body — no approach rect may land there (the hill
    # carries the runway between the mouths).
    portal_pairs = _detect_tunnel_portal_pairs(
        layout, dem, tile_lat, tile_lon
    )
    portal_owned_polygons = _portal_pair_owned_polygons(portal_pairs)
    # User ruling 2026-07-14c: the road LANE out of each MOUTH stays
    # open (mirrors the round-10 road-exit lane for deck bridges) — the
    # first approach rect must reach the mouth plate instead of being
    # skipped for grazing the owned footprint, which left the ramps
    # stopping short of the portal.  Only the outward half opens; the
    # buried band stays fully excluded.
    mouth_lane_polygons: list = []
    for pair in portal_pairs:
        for portal in pair["portals"]:
            outward_vector = portal.get("outward")
            if outward_vector is None:
                continue
            lane_lines = _draped_road_centerlines_meters(
                portal["bridge"], road_networks, to_meters
            )
            if not lane_lines:
                continue
            centroid_point = portal["footprint"].centroid
            reach = 1000.0
            perpendicular = (-outward_vector[1], outward_vector[0])
            outward_half_plane = Polygon([
                (centroid_point.x + perpendicular[0] * reach,
                 centroid_point.y + perpendicular[1] * reach),
                (centroid_point.x - perpendicular[0] * reach,
                 centroid_point.y - perpendicular[1] * reach),
                (centroid_point.x - perpendicular[0] * reach
                 + outward_vector[0] * 2.0 * reach,
                 centroid_point.y - perpendicular[1] * reach
                 + outward_vector[1] * 2.0 * reach),
                (centroid_point.x + perpendicular[0] * reach
                 + outward_vector[0] * 2.0 * reach,
                 centroid_point.y + perpendicular[1] * reach
                 + outward_vector[1] * 2.0 * reach),
            ])
            lane_half_width = 0.5 * max(
                road_width_m,
                math.sqrt(portal["footprint"].area),
            )
            for lane_line in lane_lines:
                try:
                    mouth_lane_polygons.append(
                        lane_line.buffer(lane_half_width)
                        .intersection(outward_half_plane)
                    )
                except _GEOM_EXC:
                    continue
    if mouth_lane_polygons:
        try:
            mouth_lane_union = unary_union(mouth_lane_polygons)
            portal_owned_polygons = [
                polygon.difference(mouth_lane_union)
                for polygon in portal_owned_polygons
            ]
        except _GEOM_EXC:
            pass
    keep_out_zones.extend(portal_owned_polygons)
    try:
        approach_keep_out = (
            unary_union(keep_out_zones) if keep_out_zones else None
        )
    except _GEOM_EXC:
        approach_keep_out = None

    # Cross-deck awareness (user 2026-07-10, the double-bridge overlap
    # class): a road under TWO parallel decks must be split by BOTH
    # footprints, or each deck walks its approach chain under the other
    # deck and out the far side — two chains double-covering one road.
    crossing_polygons: list = []
    for bridge in corridor_bridges:
        crossing = _bridge_deck_box_meters(bridge, layout)
        if crossing is None:
            crossing = _bridge_footprint_meters(bridge, to_meters)
        if crossing is not None:
            crossing_polygons.append(crossing)
    for pair in portal_pairs:
        for portal in pair["portals"]:
            crossing_polygons.append(portal["footprint"])
    try:
        all_crossings_union = (
            unary_union(crossing_polygons) if crossing_polygons else None
        )
    except _GEOM_EXC:
        all_crossings_union = None

    # Already-emitted approach rects (all decks, all portals in this
    # pass): a later chain never overlaps an earlier one — the same
    # earlier-emitter-wins order ``deconflict_road_features`` walks.
    emitted_registry: list = []

    osm_road_lines: list[LineString] | None = None
    n_emitted = 0
    for bridge in corridor_bridges:
        footprint = _bridge_footprint_meters(bridge, to_meters)
        if footprint is None:
            continue
        # Round 10: the corridor works on the abutment-to-abutment BOX
        # (the classified hard-face union can be multi-lobe; approach
        # walks measured from the LOBE edge left the road-exit lane
        # unconstrained between the lobe and the box at the J/R start
        # lip — audit 12: lane samples at raw DEM 171-174).
        deck_box = _bridge_deck_box_meters(bridge, layout)
        if deck_box is not None:
            footprint = deck_box
        covered_polygons.append(footprint)
        deck_elevation = _bridge_deck_elevation_m(
            bridge, dem, tile_lat, tile_lon
        )
        if deck_elevation is None:
            UI.vprint(
                2,
                "   [object-bridge] no deck datum (DEM unsampled, no MSL) "
                f"for {bridge.object_resources} — corridor skipped",
            )
            continue
        floor_elevation = _bridge_corridor_floor_m(bridge, deck_elevation)
        # Amendment A10 clearance CHECK (never the floor driver): the
        # floor-to-girder gap must reach the measured in-the-wild minimum.
        girder_underside = _bridge_girder_underside_m(bridge, deck_elevation)
        if girder_underside is not None:
            girder_clearance = girder_underside - floor_elevation
            if girder_clearance < float(
                _CFG.BRIDGE_ROAD_CLEARANCE_MINIMUM_M
            ) - 1e-6:
                UI.vprint(
                    1,
                    "   [object-bridge] WARNING: corridor clearance "
                    f"{girder_clearance:.2f} m under "
                    f"{bridge.object_resources} is below the "
                    f"{_CFG.BRIDGE_ROAD_CLEARANCE_MINIMUM_M} m acceptance "
                    "bound (amendment A10) — emitting anyway, audit "
                    "required",
                )

        road_lines = _draped_road_centerlines_meters(
            bridge, road_networks, to_meters
        )
        road_source = "dsf-road-network"
        if not road_lines:
            if osm_road_lines is None:
                osm_road_lines = _load_underpass_osm_road_lines(
                    layout, to_meters
                )
            road_lines = [
                line for line in osm_road_lines
                if line.intersects(footprint)
            ]
            road_source = "openstreetmap"
        if not road_lines:
            UI.vprint(
                1,
                "   [object-bridge] no draped road under "
                f"{bridge.object_resources} — corridor skipped",
            )
            continue
        # User ruling 2026-07-14c: the approach ramps match the WIDTH
        # of the bridge TRENCH they emerge from (the deck box minus the
        # trench inset — ~53 m at KBNA Donelson Pike), not one
        # carriageway each.  Deduping at that half-width merges the
        # parallel carriageway lines into one wide walk; genuinely
        # diverging branches (the Donelson Y) stay separate chains
        # whose full-width rects overlap through the fork.
        trench_width_m = None
        try:
            deck_box = _bridge_deck_box_meters(bridge, layout)
            if deck_box is not None and not deck_box.is_empty:
                rotated_rectangle = min_rotated_rect(deck_box)
                corners = list(rotated_rectangle.exterior.coords)
                if len(corners) >= 3:
                    edge_a = math.hypot(
                        corners[1][0] - corners[0][0],
                        corners[1][1] - corners[0][1],
                    )
                    edge_b = math.hypot(
                        corners[2][0] - corners[1][0],
                        corners[2][1] - corners[1][1],
                    )
                    trench_width_m = (
                        min(edge_a, edge_b) - 2.0 * _TRENCH_INSET_M
                    )
                    if trench_width_m < road_width_m:
                        trench_width_m = None
        except _GEOM_EXC:
            trench_width_m = None
        road_lines = _dedup_parallel_road_lines(
            road_lines, (trench_width_m or road_width_m) / 2.0
        )

        # The under-deck TRENCH and the R8 flush-seat cut moved to the
        # PRE-solve layout builder (``build_bridge_layout_shapes``, user
        # ruling R12) — this post-solve emitter now owns only the road
        # APPROACHES outside the footprint (DEM-coupled ramps, the
        # legacy-KDFW-proven block) and the suppression/refusal logs.

        # A10 point (iv): the depressed road runs >= 240 m per side
        # before rejoining grade; a caller may only widen that.
        depressed_length_m = max(
            float(approach_length_m),
            float(_CFG.BRIDGE_CORRIDOR_DEPRESSED_LENGTH_M),
        )
        _emit_corridor_for_footprint(
            layout, dem, tile_lat, tile_lon, meters_to_lat_lon,
            footprint, floor_elevation, road_lines,
            road_width_m, ramp_step_m, depressed_length_m,
            keep_out=approach_keep_out,
            crossing_union=all_crossings_union,
            emitted_registry=emitted_registry,
            width_override_m=trench_width_m,
        )
        n_emitted += 1
        UI.vprint(
            1,
            "   [object-bridge] corridor floor "
            f"{floor_elevation:.1f} m under {bridge.object_resources} "
            f"(deck {deck_elevation:.1f} m, road source {road_source})",
        )

    # ── Tunnel portal approaches (user ruling 2026-07-10): the road
    # corridor CLIMBS AWAY from each mouth — floor at the mouth's road
    # grade, rising to the digital elevation model outward.  Only
    # outward road pieces are walked; the hill side is owned by the
    # pair band in the keep-out.
    for pair in portal_pairs:
        for portal in pair["portals"]:
            footprint = portal["footprint"]
            mouth_floor = portal.get("mouth_floor_m")
            if mouth_floor is None:
                continue
            covered_polygons.append(footprint)
            road_lines = _draped_road_centerlines_meters(
                portal["bridge"], road_networks, to_meters
            )
            road_source = "dsf-road-network"
            if not road_lines:
                if osm_road_lines is None:
                    osm_road_lines = _load_underpass_osm_road_lines(
                        layout, to_meters
                    )
                road_lines = [
                    line for line in osm_road_lines
                    if line.intersects(footprint)
                ]
                road_source = "openstreetmap"
            if not road_lines:
                UI.vprint(
                    1,
                    "   [object-tunnel] no road through the portal of "
                    f"{portal['bridge'].object_resources} — outward "
                    "corridor skipped",
                )
                continue
            # Mouth-face width (the footprint's extent perpendicular to
            # the outward direction) — the CAP the ruling below enforces,
            # not the ramp width itself.
            mouth_face_width_m = None
            outward_vector = portal.get("outward")
            if outward_vector is not None:
                centroid_point = footprint.centroid
                minimum_p = maximum_p = 0.0
                try:
                    for vertex_x, vertex_y in footprint.exterior.coords:
                        projection = (
                            (vertex_x - centroid_point.x)
                            * -outward_vector[1]
                            + (vertex_y - centroid_point.y)
                            * outward_vector[0]
                        )
                        minimum_p = min(minimum_p, projection)
                        maximum_p = max(maximum_p, projection)
                    mouth_face_width_m = maximum_p - minimum_p
                except _GEOM_EXC:
                    mouth_face_width_m = None
            # User ruling 2026-07-15 (supersedes 2026-07-14c): the outward
            # ramp matches the DRIVABLE road width, not the full portal
            # footprint whose slanted wing walls flare far wider — mapped
            # OSM carriageway + shoulder, else the classified deck-face
            # width, else the mouth face, always capped at the mouth face.
            ramp_width_m, width_provenance = _portal_outward_ramp_width_m(
                layout, portal, footprint, to_meters, mouth_face_width_m)
            if ramp_width_m is None:
                ramp_width_m = mouth_face_width_m
            # The parallel-carriageway dedup keeps the mouth-face reach so
            # genuine twin carriageways still merge into one walk (the
            # ramp is then narrowed to ``ramp_width_m`` by the override).
            road_lines = _dedup_parallel_road_lines(
                road_lines,
                (mouth_face_width_m or road_width_m) / 2.0,
            )
            if _emit_corridor_for_footprint(
                layout, dem, tile_lat, tile_lon, meters_to_lat_lon,
                footprint, mouth_floor, road_lines,
                road_width_m, ramp_step_m,
                max(float(approach_length_m),
                    float(_CFG.BRIDGE_CORRIDOR_DEPRESSED_LENGTH_M)),
                keep_out=approach_keep_out,
                crossing_union=all_crossings_union,
                emitted_registry=emitted_registry,
                outward=outward_vector,
                width_override_m=ramp_width_m,
            ):
                n_emitted += 1
                UI.vprint(
                    1,
                    "   [object-tunnel] outward corridor from mouth "
                    f"floor {mouth_floor:.1f} m for "
                    f"{portal['bridge'].object_resources} "
                    f"(road source {road_source})",
                )
    return n_emitted, suppression_polygons, covered_polygons


def _load_underpass_osm_road_lines(layout, to_meters):
    """OSM big-road LineStrings (local meters) eligible to pass under a
    bridge — the same filter the legacy underpass emitter applies (skip
    ways tagged ``bridge`` or ``tunnel``)."""
    from .pipeline import _load_osm_big_roads
    nodes_raw, ways_raw = _load_osm_big_roads(
        layout.anchor[0], layout.anchor[1]
    )
    if not ways_raw:
        return []
    nodes_meters: dict[str, tuple[float, float]] = {}
    for node_id, (latitude, longitude) in nodes_raw.items():
        nodes_meters[node_id] = to_meters(longitude, latitude)
    highway_types = {
        "motorway", "trunk", "primary", "secondary", "tertiary",
        "motorway_link", "trunk_link", "primary_link", "residential",
        "service",
    }
    lines: list[LineString] = []
    for _way_id, node_refs, tags in ways_raw:
        if tags.get("highway") not in highway_types:
            continue
        if tags.get("bridge") and tags.get("bridge") != "no":
            continue
        if tags.get("tunnel") and tags.get("tunnel") != "no":
            continue
        points = [nodes_meters[n] for n in node_refs if n in nodes_meters]
        if len(points) < 2:
            continue
        try:
            line = LineString(points)
        except _GEOM_EXC:
            continue
        if not line.is_empty and line.length >= 5.0:
            lines.append(line)
    return lines


def _emit_corridor_for_footprint(
        layout, dem, tile_lat, tile_lon, meters_to_lat_lon,
        footprint, floor_elevation, road_lines,
        road_width_m, ramp_step_m, approach_length_m,
        keep_out=None, crossing_union=None, emitted_registry=None,
        outward=None, width_override_m=None):
    """Emit stepped approach ramps from ``floor_elevation`` up to the DEM
    for each road crossing a bridge footprint (the under-deck trench
    plate itself is emitted by the caller as the FULL footprint, stage
    2b).  Returns True when at least one polygon was emitted.

    Mirrors the legacy underpass emitter's per-step ramp shape (a sloped
    ``ROLE_TUNNEL_RAMP`` rect per ``ramp_step_m`` interpolating floor→DEM),
    driven by the object footprint rather than a taxi rect.  Deconfliction
    against airport pavement is the shared downstream
    ``deconflict_road_features`` pass, exactly as for the legacy shapes.

    ``crossing_union`` — the union of EVERY classified crossing (all
    deck boxes + portal footprints): the road is split by ALL of them,
    not just this bridge's own footprint, so a walk never runs under a
    sibling deck and out the far side (the double-bridge overlap class).
    ``emitted_registry`` — shared list of already-emitted approach rects;
    a later chain skips rects overlapping an earlier chain's.
    ``outward`` — optional unit vector: walk only road pieces on that
    side of the footprint (tunnel portals ramp AWAY from the hill).
    ``width_override_m`` — full corridor width for the ramp rects when
    the caller matches the crossing opening (the trench road-exit
    width, the portal mouth face) instead of one carriageway (user
    ruling 2026-07-14c: ramps as wide as the opening they emerge
    from)."""
    emitted = False
    half_width = (width_override_m if width_override_m else
                  road_width_m) / 2.0
    split_region = footprint
    if crossing_union is not None:
        split_region = crossing_union
    centroid = footprint.centroid
    for road_line in road_lines:
        try:
            outside = road_line.difference(split_region)
        except _GEOM_EXC:
            outside = None
        if outside is None or outside.is_empty:
            continue
        pieces = (
            list(outside.geoms) if hasattr(outside, "geoms") else [outside]
        )
        for piece in pieces:
            if piece.is_empty or piece.geom_type != "LineString":
                continue
            coordinates = list(piece.coords)
            if len(coordinates) < 2:
                continue
            # Only pieces that BEGIN at this bridge's own footprint —
            # with the full crossing union splitting the road, a sibling
            # deck's pieces also appear here and belong to that deck's
            # own emission turn.
            if piece.distance(footprint) > 1.0:
                continue
            if outward is not None:
                midpoint = piece.interpolate(0.5, normalized=True)
                side = ((midpoint.x - centroid.x) * outward[0]
                        + (midpoint.y - centroid.y) * outward[1])
                if side <= 0.0:
                    continue
            distance_start = footprint.distance(Point(coordinates[0]))
            distance_end = footprint.distance(Point(coordinates[-1]))
            if distance_start <= distance_end:
                walk = LineString(coordinates)
            else:
                walk = LineString(list(reversed(coordinates)))
            walk_length = min(walk.length, approach_length_m)
            # Plate weld (defect A, 2026-07-15): deck corridors register
            # the chain's near edge on the corridor PLATE's exit edge —
            # verbatim shared corner coordinates are the weld.  Portal
            # mouths keep their own (untouched) geometry.
            weld_edge = None
            if outward is None:
                weld_edge = _corridor_plate_exit_edge(
                    layout, footprint, walk.coords[0], half_width
                )
            if _emit_corridor_ramp_chain(
                layout, dem, tile_lat, tile_lon, meters_to_lat_lon,
                walk, walk_length, floor_elevation, half_width, ramp_step_m,
                keep_out=keep_out,
                emitted_registry=emitted_registry,
                refuse_inverted=(outward is not None),
                weld_edge=weld_edge,
            ):
                emitted = True
    return emitted


def _corridor_plate_exit_edge(layout, footprint, start_xy, half_width):
    """The corridor PLATE's short-end edge nearest an approach walk's
    start point, as the plate's two EXACT ring corner coordinates
    ``((ax, ay), (bx, by))`` — the verbatim-shared-coordinate weld the
    chain's first quad copies into its near edge (defect A, KBNA
    2026-07-15: the chain started on the deck-box edge while the plate
    lip sits ``_TRENCH_INSET_M`` inside it — a 1.09 m open seam at
    36.12202,-86.66612 — with a 4-5 m lateral offset between the road
    line and the plate axis).

    Donelson-class only (guarded): the plate end must match the chain
    width (the road runs ALONG the deck axis and exits the short end,
    so the trench end edge and the trench-width approach are the same
    span).  Roads exiting through a deck's LONG side keep the previous
    behaviour.  ``None`` when there is no emitted plate, the geometry
    is degenerate, or the guards fail."""
    plate = None
    best_area = 0.0
    for shape in layout.shapes:
        if getattr(shape, "ref", "") != "object_bridge_corridor":
            continue
        polygon = shape.polygon
        if polygon is None or polygon.is_empty:
            continue
        try:
            overlap_area = polygon.intersection(footprint).area
        except _GEOM_EXC:
            continue
        if overlap_area > best_area:
            best_area = overlap_area
            plate = polygon
    if plate is None:
        return None
    try:
        rectangle_corners = list(
            min_rotated_rect(plate).exterior.coords)[:4]
    except _GEOM_EXC:
        return None
    if len(rectangle_corners) < 4:
        return None
    edges = [
        (rectangle_corners[index], rectangle_corners[(index + 1) % 4])
        for index in range(4)
    ]
    edges.sort(key=lambda edge: math.hypot(
        edge[1][0] - edge[0][0], edge[1][1] - edge[0][1]))
    short_edges = edges[:2]

    def _midpoint_distance(edge):
        return math.hypot(
            (edge[0][0] + edge[1][0]) / 2.0 - start_xy[0],
            (edge[0][1] + edge[1][1]) / 2.0 - start_xy[1],
        )

    exit_edge = min(short_edges, key=_midpoint_distance)
    if _midpoint_distance(exit_edge) > 30.0:
        return None
    edge_length = math.hypot(
        exit_edge[1][0] - exit_edge[0][0],
        exit_edge[1][1] - exit_edge[0][1],
    )
    # Width guard: the plate end and the approach must be the same
    # span for a two-corner verbatim copy to make sense.
    if abs(edge_length - 2.0 * half_width) > 3.0:
        return None
    # Snap the rotated-rectangle corners to the plate's ACTUAL ring
    # vertices — only exact ring coordinates intern into shared nodes.
    ring = list(plate.exterior.coords)
    snapped = []
    for corner_x, corner_y in exit_edge:
        nearest = min(
            ring,
            key=lambda vertex: (
                (vertex[0] - corner_x) ** 2 + (vertex[1] - corner_y) ** 2
            ),
        )
        if math.hypot(nearest[0] - corner_x, nearest[1] - corner_y) > 1.5:
            return None
        snapped.append((nearest[0], nearest[1]))
    if math.hypot(
        snapped[1][0] - snapped[0][0], snapped[1][1] - snapped[0][1]
    ) < 1.0:
        return None
    return snapped[0], snapped[1]


def _emit_corridor_ramp_chain(
        layout, dem, tile_lat, tile_lon, meters_to_lat_lon,
        walk, walk_length, floor_elevation, half_width, ramp_step_m,
        keep_out=None, emitted_registry=None, refuse_inverted=False,
        weld_edge=None):
    """Step ``walk`` from the bridge edge (``floor_elevation``) out to the
    DEM in ``ramp_step_m`` increments, emitting one sloped
    ``ROLE_TUNNEL_RAMP`` quad per step.  Returns True when any quad was
    emitted.

    2026-07-15 rework (KBNA Donelson defects A+B, measured on the
    emitted patch):

    * **Shared facing edges.**  ONE corner pair is computed per chain
      station and used verbatim by both adjoining quads — the chain is
      contiguous by construction (zero facing gap, zero overlap on
      curved roads; the per-step independent rects overlapped under
      curvature and the registry then dropped every other step — the
      six measured terrain holes).
    * **Chain identity in the registry.**  A chain's quads are compared
      only against pieces registered BEFORE this chain started; the
      registry's purpose is cross-crossing / twin-carriageway
      protection (audit invariant 1), never self-comparison.
    * **[H,L,L,H] corner order.**  Quads are emitted with ring corners
      0,3 at the HIGH end, matching ``corner_alts_from_high_low`` (the
      legacy emitter's explicit reorder).  The fixed near-end-first
      order shipped every climbing rect INVERTED — the floor value on
      the FAR edge, a 2-4 m sawtooth per step (measured: rect -11066
      carried 161.0 on the edge away from the 161.01 plate).
    * **Plate weld** (``weld_edge``, deck corridors only): the first
      quad's near edge copies the corridor plate's two exit-edge ring
      coordinates VERBATIM (0.5 m interning makes them shared nodes —
      the weld), and the lateral offset between the plate axis and the
      road line decays linearly to zero over the next 3 stations so
      curved roads still track.  The near edge keeps the plate's floor
      elevation (fraction 0 of the floor→DEM interpolation); the far
      edge keeps the chain's existing climb values.

    ``refuse_inverted`` — the tunnel-portal inversion guard (user
    2026-07-10, the runway-02C climb): the mouth floor must never sit
    above the ground the chain welds to at its NEAR end — a floor above
    the adjacent road means the datum was wrong (embankment top instead
    of the road), producing a ramp climbing INTO the crossing and
    poking above the descending terrain.  The FAR end is deliberately
    not tested: terrain legitimately falls away from a mouth along the
    road (measured KBNA 02C west: 180 m at the face down to 174 m at
    240 m out).  Deck-carried BRIDGE corridors skip the guard entirely:
    a causeway over low ground sends its road DOWN to the valley."""
    if refuse_inverted:
        try:
            near_point = walk.interpolate(min(5.0, walk_length))
            near_lat, near_lon = meters_to_lat_lon(
                near_point.x, near_point.y)
            near_grade = _sample_dem(
                dem, tile_lat, tile_lon, near_lat, near_lon)
        except _GEOM_EXC:
            near_grade = None
        if near_grade is not None and floor_elevation > near_grade + 2.0:
            UI.vprint(
                1,
                "   [object-tunnel] refusing INVERTED portal approach: "
                f"mouth floor {floor_elevation:.1f} m sits above the "
                f"adjacent grade {near_grade:.1f} m",
            )
            return False
    # ── Chain stations ──
    stations = [0.0]
    position = 0.0
    while position < walk_length - 1.0:
        position = min(walk_length, position + ramp_step_m)
        stations.append(position)
    if len(stations) < 2:
        return False
    station_points = [
        (point.x, point.y)
        for point in (walk.interpolate(s) for s in stations)
    ]
    # Plate-weld lateral registration: shift station 0 onto the plate
    # exit edge's midpoint and decay the shift to zero over the next
    # 3 stations (fully back on the road line from station 3 on).
    weld_corners = None
    if weld_edge is not None:
        (weld_ax, weld_ay), (weld_bx, weld_by) = weld_edge
        edge_mid_x = (weld_ax + weld_bx) / 2.0
        edge_mid_y = (weld_ay + weld_by) / 2.0
        shift_x = edge_mid_x - station_points[0][0]
        shift_y = edge_mid_y - station_points[0][1]
        decay_stations = 3.0
        station_points = [
            (
                point_x + shift_x * max(0.0, 1.0 - index / decay_stations),
                point_y + shift_y * max(0.0, 1.0 - index / decay_stations),
            )
            for index, (point_x, point_y) in enumerate(station_points)
        ]
        weld_corners = ((weld_ax, weld_ay), (weld_bx, weld_by))

    # ── Per-station cross edges (bisector normals): ONE corner pair per
    # station, shared verbatim by the two adjoining quads. ──
    left_corners: list = []
    right_corners: list = []
    elevations: list = []
    usable_stations = len(station_points)
    for index, (point_x, point_y) in enumerate(station_points):
        behind_x, behind_y = station_points[max(0, index - 1)]
        ahead_x, ahead_y = station_points[
            min(len(station_points) - 1, index + 1)]
        tangent_x = ahead_x - behind_x
        tangent_y = ahead_y - behind_y
        tangent_norm = math.hypot(tangent_x, tangent_y)
        if tangent_norm < 1.0:
            usable_stations = index
            break
        tangent_x /= tangent_norm
        tangent_y /= tangent_norm
        normal_x = -tangent_y
        normal_y = tangent_x
        try:
            latitude, longitude = meters_to_lat_lon(point_x, point_y)
            ground = _sample_dem(
                dem, tile_lat, tile_lon, latitude, longitude)
        except _GEOM_EXC:
            ground = None
        if ground is None:
            usable_stations = index
            break
        fraction = stations[index] / walk_length
        elevations.append(
            (1.0 - fraction) * floor_elevation + fraction * ground)
        left_corners.append((point_x + normal_x * half_width,
                             point_y + normal_y * half_width))
        right_corners.append((point_x - normal_x * half_width,
                              point_y - normal_y * half_width))
        if index == 0 and weld_corners is not None:
            # Verbatim plate coordinates for the chain's near edge —
            # exact shared coordinates are the weld.
            corner_a, corner_b = weld_corners
            side_a = ((corner_a[0] - point_x) * normal_x
                      + (corner_a[1] - point_y) * normal_y)
            side_b = ((corner_b[0] - point_x) * normal_x
                      + (corner_b[1] - point_y) * normal_y)
            if side_a > 0.0 > side_b:
                left_corners[0], right_corners[0] = corner_a, corner_b
            elif side_b > 0.0 > side_a:
                left_corners[0], right_corners[0] = corner_b, corner_a
    if usable_stations < 2:
        return False

    # Chain identity (defect B): quads of THIS chain are tested only
    # against pieces registered before the chain started.
    pre_chain_registry = (
        list(emitted_registry) if emitted_registry is not None else [])

    emitted = False
    for index in range(usable_stations - 1):
        elevation_near = elevations[index]
        elevation_far = elevations[index + 1]
        # [H,L,L,H]: ring corners 0,3 at the HIGH end (the convention
        # ``corner_alts_from_high_low`` encodes; the legacy emitter's
        # explicit reorder).
        if elevation_far >= elevation_near:
            corners = [
                left_corners[index + 1], left_corners[index],
                right_corners[index], right_corners[index + 1],
            ]
        else:
            corners = [
                left_corners[index], left_corners[index + 1],
                right_corners[index + 1], right_corners[index],
            ]
        try:
            polygon = Polygon(corners)
            if not polygon.is_valid:
                polygon = polygon.buffer(0)
            if keep_out is not None and not polygon.is_empty:
                # FRACTIONAL skip (user ruling 2026-07-14c): a quad
                # merely GRAZING a keep-out boundary is emitted — the
                # absolute 0.5 m2 test dropped every leading rect at
                # the KBNA portal mouths (ramps stopped one step short
                # of the plate) and starved the wide Donelson chains.
                # A quad genuinely landing on owned ground still skips.
                try:
                    if (polygon.intersection(keep_out).area
                            > 0.25 * polygon.area):
                        continue
                except _GEOM_EXC:
                    pass
            # Approach-versus-approach exclusivity (user 2026-07-10):
            # a quad overlapping an EARLIER CHAIN's quad is skipped —
            # sloped quads can never be clipped without breaking their
            # two-corner altitude semantics, so overlap is prevented at
            # birth, not cleaned downstream.
            if not polygon.is_empty:
                overlapping = False
                for earlier in pre_chain_registry:
                    try:
                        if polygon.intersection(earlier).area > 0.5:
                            overlapping = True
                            break
                    except _GEOM_EXC:
                        continue
                if overlapping:
                    continue
            if polygon.geom_type == "Polygon" and not polygon.is_empty:
                if abs(elevation_near - elevation_far) >= 0.1:
                    layout.shapes.append(BuiltShape(
                        polygon=polygon,
                        role=ROLE_TUNNEL_RAMP,
                        ref="object_bridge_approach",
                        altitude_high=round(
                            max(elevation_near, elevation_far), 1),
                        altitude_low=round(
                            min(elevation_near, elevation_far), 1)))
                else:
                    layout.shapes.append(BuiltShape(
                        polygon=polygon,
                        role=ROLE_TUNNEL_RAMP,
                        ref="object_bridge_approach",
                        altitude=round(
                            0.5 * (elevation_near + elevation_far), 1)))
                if emitted_registry is not None:
                    emitted_registry.append(polygon)
                emitted = True
        except _GEOM_EXC:
            pass
    return emitted


# ---------------------------------------------------------------------------
# Feature B stage 2 — solve-side deck-end / profile pins + crossing floor
# (spec section 3.2 steps 1-2 and the bridge_crossing_floor law; all law
# values come from grade_law so the writers here and the verification
# checks can never drift — the lockstep pattern of the runway-end skirt.)
# ---------------------------------------------------------------------------

# Pavement roles eligible for bridge pins: the solved airside network the
# deck couples to.  Boundary / retaining walls / tunnel ramps / buildings
# are feature shapes, not the graded network the abutment meets.
_BRIDGE_PIN_ROLES = frozenset({
    ROLE_RUNWAY, ROLE_RUNWAY_CROSSING, ROLE_PRIMARY_PARALLEL,
    ROLE_SECONDARY_PARALLEL, ROLE_STUB, ROLE_CROSS_CONNECTOR,
    ROLE_APRON, ROLE_JUNCTION,
})

# A ring vertex within this distance (m) of an abutment line counts as
# lying ON it (inserted crossings are exact intersections; pre-existing
# deck-cut end vertices sit within layout snapping tolerance).
_BRIDGE_PIN_ON_LINE_TOLERANCE_M = 0.25

# Abutment lines are exactly deck-width; extend each end by this fraction
# of its own length so a ring crossing at the deck corner is still cut.
_ABUTMENT_LINE_EXTENSION_FRACTION = 0.25

# ── Stage 2b iteration 5: coverage-extent geometry (KBNA audit 5) ──
# The trench rim is inset this far inside the deck footprint; the
# causeway plate extends _CAUSEWAY_INWARD_OVERLAP_M INWARD past the
# abutment lip so a mesh sample exactly ON the lip line lands INSIDE
# the 167-flat plate even after to_osm's 0.5 m node interning wobbles
# the written edge (audit 5: on-line samples read the wall slope or
# raw terrain).  The remaining trench-to-causeway gap is the R2
# node-split wall and MUST stay above the 0.5 m weld tolerance:
# 1.2 − 0.6 = 0.6 m.
_TRENCH_INSET_M = 1.2
_CAUSEWAY_INWARD_OVERLAP_M = 0.6

# The causeway lip is widened this much per side beyond the deck width
# so the audit's line-END samples (t = 0 and t = 1, exactly at the deck
# corners) stay inside the plate under coordinate wobble.
_CAUSEWAY_WIDTH_MARGIN_M = 1.0

# ── Deck-lip weld strips (user directive 2026-07-15: aircraft must taxi
# SMOOTHLY onto the decks — the pavement must slightly OVERLAP the
# deck-elevation terrain, not merely touch it; at mesh-triangulation
# level edge-to-edge contact still leaves sliver triangles).  The R8
# hard-deck cut trims pavement at the deck-box boundary while the trench
# plate is ``deck_box.buffer(-_TRENCH_INSET_M)``, leaving a ~1.2 m ring
# of raw mesh whose triangles dive to the trench floor right at the lip
# (measured KBNA taxiway-L: pavement 167.0 pinned, plate edge 0.8-1.2 m
# away at 161.01).  On every pavement-facing rim segment a WELD STRIP is
# born at the deck-top profile law value, spanning from the causeway
# inward depth (keeping the R2 node-split wall to the trench) out to
# _DECK_WELD_OVERLAP_M INSIDE the pavement, whose fronting ring vertices
# are pinned at the same law value — a coplanar, invisible overlap.
# The depth must EXCEED to_osm's 0.5 m node interning (the audit-5
# causeway lesson): a 0.4 m offset merges the strip boundary into the
# pavement edge nodes and the written overlap collapses to contact
# (measured KBNA: eroded-overlap coverage 22-66 % at 0.4 m); above the
# 0.71 m worst-case bucket diagonal both rings survive verbatim.
_DECK_WELD_OVERLAP_M = 0.8
# Pavement within this reach OUTSIDE the deck box fronts the deck (the
# measured pavement cut sits within ~0.4 m of the box under the 0.5 m
# node-interning wobble; 2.5 m is generous without capturing bystanders).
_DECK_WELD_FRONT_REACH_M = 2.5

# Road-exit cut half-width (m) through a causeway plate (round 10): a
# road that leaves the span THROUGH an abutment end (KBNA: Donelson
# Pike runs along the deck axis, measured segments local x -64..+77)
# must not be dammed by the 167-flat causeway — the author mesh runs
# the 161 corridor ~144 m THROUGH the span and out both ends, flanked
# by the fill (A10 / spec section 2.2; corridor 34-38 m wide overall).
# Each fully-draped carriageway polyline is buffered by this half-width
# and the union is cut out of the plate; the parallel carriageways at
# KBNA (7 draped lines) union to the author's full corridor width.
_ROAD_EXIT_CUT_HALF_WIDTH_M = 11.0


def _road_exit_corridor_meters(bridge, layout, to_meters):
    """The union of the fully-draped road carriageway corridors crossing
    a bridge's deck footprint, buffered to carriageway width in layout
    meters — the ground the causeway must YIELD (round 10).  ``None``
    when no draped road crosses."""
    road_networks = _object_bridge_road_networks(layout)
    road_lines = _draped_road_centerlines_meters(
        bridge, road_networks, to_meters
    )
    if not road_lines:
        return None
    try:
        return unary_union([
            line.buffer(_ROAD_EXIT_CUT_HALF_WIDTH_M,
                        cap_style=2, join_style=2)
            for line in road_lines
        ])
    except _GEOM_EXC:
        return None


def _bridge_datum_elevation_m(bridge, dem, tile_lat, tile_lon):
    """Absolute elevation of the object's anchor-terrain plane (the datum
    every effective height is measured from): ``absolute_deck_elevation_m
    − deck_top_y_m`` when OBJECT_MSL fixtures pin the deck, else the DEM
    at the anchor.  ``None`` when neither source is available."""
    deck_elevation = _bridge_deck_elevation_m(bridge, dem, tile_lat, tile_lon)
    if deck_elevation is None:
        return None
    return float(deck_elevation) - float(bridge.deck_top_y_m)


def _abutment_lines_layout_meters(
        bridge, layout,
        extension_fraction=_ABUTMENT_LINE_EXTENSION_FRACTION):
    """The bridge's two abutment lines as layout-meter LineStrings,
    ordered [start end, far end] and extended by ``extension_fraction``
    per side (default :data:`_ABUTMENT_LINE_EXTENSION_FRACTION`; the
    causeway emitter passes 0.0 — its plate must be deck-width, not
    band-width).  Empty list on degenerate geometry."""
    from . import obj8_reader
    origin_longitude, origin_latitude = (
        bridge.frame_origin_longitude_latitude
    )
    lines: list[LineString] = []
    for (start_point, end_point) in bridge.abutment_lines:
        meter_points = []
        for frame_x, frame_z in (start_point, end_point):
            latitude, longitude = obj8_reader.local_offset_to_lonlat(
                origin_latitude, origin_longitude, 0.0, frame_x, frame_z
            )
            meter_points.append(layout.ll_to_m(latitude, longitude))
        (ax, ay), (bx, by) = meter_points
        length = math.hypot(bx - ax, by - ay)
        if length < 1.0:
            continue
        ux = (bx - ax) / length
        uy = (by - ay) / length
        reach = length * extension_fraction
        lines.append(LineString([
            (ax - ux * reach, ay - uy * reach),
            (bx + ux * reach, by + uy * reach),
        ]))
    return lines


def _bridge_deck_box_meters(bridge, layout):
    """The abutment-to-abutment deck BOX in layout meters — the convex
    hull of the two (unextended) abutment lines' endpoints.  Iteration 5:
    the classified ``deck_polygon`` is the union of HARD FACES and can be
    a partial, multi-lobe shape (KBNA taxiway-L pools parts p1/p4/p5/p6
    only — the under-deck trench built from it had a mid-deck gap that
    approach ramps legally filled at 162-165 m, breaking the <= 161.25
    corridor acceptance).  The physical under-deck corridor is the FULL
    box between the abutments — the author-mesh treatment.  ``None`` on
    degenerate geometry."""
    from shapely.geometry import MultiPoint
    lines = _abutment_lines_layout_meters(
        bridge, layout, extension_fraction=0.0)
    if len(lines) < 2:
        return None
    corners = [c for line in lines for c in line.coords]
    try:
        box = MultiPoint(corners).convex_hull
    except _GEOM_EXC:
        return None
    if box.geom_type != "Polygon" or box.is_empty:
        return None
    return box


def _record_pin(layout, x, y, value):
    """Record one bucket→elevation hard pin for the solver
    (``layout._object_bridge_pin_values``; consumed by the additive
    bridge-pin block in ``solver_primitives._seed_elevations``).  The
    bucket scheme is ``layout.vertex_bucket`` — the single source of
    truth, arithmetically identical to the solver's inline key."""
    from .layout import vertex_bucket
    pin_values = getattr(layout, "_object_bridge_pin_values", None)
    if pin_values is None:
        pin_values = {}
        setattr(layout, "_object_bridge_pin_values", pin_values)
    pin_values[vertex_bucket(float(x), float(y))] = float(value)


def born_flat_solver_plate(layout, polygon, role, ref, elevation,
                           record_pins: bool = True) -> int:
    """Append a densified flat plate (≈5 m vertex spacing) with per-vertex
    ``node_altitudes`` at ``elevation``; when ``record_pins`` is true (the
    default) also register EVERY ring vertex as a hard solver pin
    (``_record_pin`` → ``layout._object_bridge_pin_values``).

    THE shared birth primitive for object-derived terrain plates (ruling
    R12).  With ``record_pins`` the plate is a FIRST-CLASS solver graph
    member (its role is in the solver's PAVEMENT_ROLES): ``_seed_elevations``
    pins it exactly like a deck-end pin and protects it via the seam-pin
    index, the solve grades the neighbouring pavement to meet it, the
    writeback is the identity, and ``to_osm`` ships it per-node ``alt_abs``.
    Feature B's bridge trench and causeway birth this way.

    Feature A's tunnel trench passes ``record_pins=False``: it is
    OFF-PAVEMENT terrain (the airside pavement is subtracted from the body
    before birth, ruling R2), so it must NOT pin — and its role is NOT in
    PAVEMENT_ROLES — leaving it out of the pavement solve entirely.  It
    still ships per-node ``alt_abs`` (the flat-by-law encoding the mesh step
    consumes) and wins the LAW-tier weld at any shared vertex.  Returns the
    ring vertex count (0 on degenerate input)."""
    if (polygon is None or polygon.is_empty
            or polygon.geom_type != "Polygon"):
        return 0
    try:
        dense = polygon.segmentize(5.0)
    except (AttributeError, _GEOM_EXC):
        dense = polygon
    ring = list(dense.exterior.coords)
    if len(ring) < 4:
        return 0
    vertex_count = len(ring) - 1 if ring[0] == ring[-1] else len(ring)
    if record_pins:
        for x, y in ring[:vertex_count]:
            _record_pin(layout, x, y, elevation)
    layout.shapes.append(BuiltShape(
        polygon=dense,
        role=role,
        ref=ref,
        node_altitudes=[round(float(elevation), 2)] * (vertex_count + 1)))
    return vertex_count


def _pin_shape_vertices_on_line(layout, shape_index, line, pin_value,
                                capture_band_m=None):
    """Insert ring vertices where the shape crosses ``line`` (seam-anchor
    idiom, reusing ``seam_anchors._insert_seam_vertices``), then hard-pin
    every ring vertex within ``capture_band_m`` of the line at
    ``pin_value`` — both into the shape's ``node_altitudes`` (the
    solver's fallback and downstream readers) and into the solver pin
    registry.  Returns the number of vertices pinned.

    ``capture_band_m`` defaults to the tight on-line tolerance
    (:data:`_BRIDGE_PIN_ON_LINE_TOLERANCE_M`, PROFILE_CARRIED span-end
    pins on continuous pavement); DECK_CARRIED callers pass
    ``config.BRIDGE_ABUTMENT_PIN_CAPTURE_BAND_M`` — the pack cuts
    pavement up to ~10 m short of the abutment (measured, KBNA), and
    amendment A10's flat causeway makes the deck-end value exact
    anywhere in that band."""
    from .seam_anchors import _insert_seam_vertices
    if capture_band_m is None:
        capture_band_m = _BRIDGE_PIN_ON_LINE_TOLERANCE_M
    shape = layout.shapes[shape_index]
    inserted_keys: set = set()
    try:
        new_shape = _insert_seam_vertices(shape, [line], inserted_keys)
    except _GEOM_EXC:
        new_shape = None
    if new_shape is not None:
        layout.shapes[shape_index] = new_shape
        shape = new_shape
    if shape.polygon is None or shape.polygon.is_empty:
        return 0
    ring = list(shape.polygon.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    node_altitudes = (
        list(shape.node_altitudes[:len(ring)])
        if shape.node_altitudes else None
    )
    pinned = 0
    changed = False
    for vertex_index, (x, y) in enumerate(ring):
        try:
            if line.distance(Point(x, y)) > capture_band_m:
                continue
        except _GEOM_EXC:
            continue
        _record_pin(layout, x, y, pin_value)
        if node_altitudes is not None and vertex_index < len(node_altitudes):
            node_altitudes[vertex_index] = round(float(pin_value), 2)
            changed = True
        pinned += 1
    if changed and node_altitudes is not None:
        shape.node_altitudes = node_altitudes + [node_altitudes[0]]
    return pinned


def insert_bridge_deck_end_pins(layout, dem, tile_lat, tile_lon) -> int:
    """Feature B stage 2, step 1 (spec section 3.2): insert ring vertices
    where pavement rings cross a DECK_CARRIED (or cosmetic) bridge's
    abutment lines and hard-pin them at the deck-end elevation —
    ``grade_law.bridge_deck_end_pin_elevation_m`` (MSL-first datum).  The
    solved network grades up to the pins under the existing edge budgets.

    Runs pre-solve (called from the pipeline's seam-anchor hook region).
    No-op without a cached classification (gate off).  Returns the number
    of pinned vertices; abutment ends that pin NO vertex (pavement cut
    short of the abutment) are logged — the analytic causeway/approach
    emitters own that gap and take the same pin value (audited by W-V)."""
    classification = _object_bridge_classification(layout)
    if classification is None:
        return 0
    from .grade_law import bridge_deck_end_pin_elevation_m
    corridor_bridges, _suppress, _refused, _road_carried, _portals = (
        _partition_bridges_for_corridors(classification, layout)
    )
    capture_band = float(_CFG.BRIDGE_ABUTMENT_PIN_CAPTURE_BAND_M)
    total_pinned = 0
    for bridge in corridor_bridges:
        datum = _bridge_datum_elevation_m(bridge, dem, tile_lat, tile_lon)
        if datum is None:
            # Verbosity 1 ALWAYS: a silent skip here is the project's
            # classic silent-zero failure (stage 2b diagnosis — the
            # first gated KBNA build produced zero pins with every
            # diagnostic below the log's verbosity).
            UI.vprint(
                1,
                "   [object-bridge] no datum for deck-end pins of "
                f"{bridge.object_resources} — skipped",
            )
            continue
        abutment_lines = _abutment_lines_layout_meters(bridge, layout)
        for end_index, line in enumerate(abutment_lines):
            end_y = (
                bridge.deck_end_elevations_y_m[end_index]
                if end_index < len(bridge.deck_end_elevations_y_m)
                else bridge.deck_top_y_m
            )
            pin_value = bridge_deck_end_pin_elevation_m(datum, end_y)
            pinned_here = 0
            for shape_index, shape in enumerate(list(layout.shapes)):
                if shape.role not in _BRIDGE_PIN_ROLES:
                    continue
                if shape.polygon is None or shape.polygon.is_empty:
                    continue
                try:
                    near = shape.polygon.exterior.distance(line) \
                        <= capture_band \
                        or shape.polygon.exterior.intersects(line)
                except _GEOM_EXC:
                    continue
                if not near:
                    continue
                pinned_here += _pin_shape_vertices_on_line(
                    layout, shape_index, line, pin_value,
                    capture_band_m=capture_band,
                )
            # Verbosity 1 in BOTH branches (silent-zero rule): the
            # zero-pin end is precisely the signal that the causeway
            # plate must carry the coupling.
            if pinned_here:
                UI.vprint(
                    1,
                    f"   [object-bridge] {pinned_here} deck-end pin(s) at "
                    f"{pin_value:.2f} m (end {end_index}) for "
                    f"{bridge.object_resources}",
                )
            else:
                UI.vprint(
                    1,
                    "   [object-bridge] ZERO deck-end pins at end "
                    f"{end_index} of {bridge.object_resources} (no "
                    f"pavement ring within {capture_band:.0f} m) — the "
                    "causeway plate carries the pin value "
                    f"({pin_value:.2f} m)",
                )
            total_pinned += pinned_here
    return total_pinned


def insert_bridge_profile_pins(layout, dem, tile_lat, tile_lon) -> int:
    """Feature B stage 2, step 2 (spec section 3.2, amendment A4):
    per-vertex profile pins across each PROFILE_CARRIED span — pavement
    ring vertices inside the deck footprint are hard-pinned to
    ``grade_law.bridge_profile_pin_elevation_m`` (datum + profile at the
    vertex's along-axis position), the runway-profile per-vertex
    mechanism.  Ring vertices are also inserted where rings cross the
    span-end abutment lines so the pins start exactly at the deck tips.
    Returns the number of pinned vertices."""
    classification = _object_bridge_classification(layout)
    if classification is None:
        return 0
    from .grade_law import (
        bridge_deck_end_pin_elevation_m,
        bridge_profile_pin_elevation_m,
    )
    from .object_terrain_features import PROFILE_CARRIED
    to_meters, _meters_to_lat_lon = _local_meter_projections(layout.anchor)
    total_pinned = 0
    for bridge in classification.bridges:
        if bridge.contract != PROFILE_CARRIED:
            continue
        datum = _bridge_datum_elevation_m(bridge, dem, tile_lat, tile_lon)
        if datum is None:
            continue
        footprint = _bridge_footprint_meters(bridge, to_meters)
        if footprint is None:
            continue
        abutment_lines = _abutment_lines_layout_meters(bridge, layout)
        # Axis for along-position: start-end abutment midpoint → far-end
        # abutment midpoint (the profile's along coordinates are measured
        # from the deck rectangle's start edge; the law clamps outside the
        # sampled range).
        if len(abutment_lines) < 2:
            continue
        start_mid = abutment_lines[0].interpolate(0.5, normalized=True)
        far_mid = abutment_lines[1].interpolate(0.5, normalized=True)
        axis_length = math.hypot(
            far_mid.x - start_mid.x, far_mid.y - start_mid.y
        )
        if axis_length < 1.0:
            continue
        axis_unit = (
            (far_mid.x - start_mid.x) / axis_length,
            (far_mid.y - start_mid.y) / axis_length,
        )
        # Span-end insertion first (deck-tip pins).
        for end_index, line in enumerate(abutment_lines):
            end_y = (
                bridge.deck_end_elevations_y_m[end_index]
                if end_index < len(bridge.deck_end_elevations_y_m)
                else bridge.deck_top_y_m
            )
            end_pin = bridge_deck_end_pin_elevation_m(datum, end_y)
            for shape_index, shape in enumerate(list(layout.shapes)):
                if shape.role not in _BRIDGE_PIN_ROLES:
                    continue
                if shape.polygon is None or shape.polygon.is_empty:
                    continue
                try:
                    if not shape.polygon.exterior.intersects(line):
                        continue
                except _GEOM_EXC:
                    continue
                total_pinned += _pin_shape_vertices_on_line(
                    layout, shape_index, line, end_pin
                )
        # Interior per-vertex profile pins.
        pinned_interior = 0
        for shape in layout.shapes:
            if shape.role not in _BRIDGE_PIN_ROLES:
                continue
            if shape.polygon is None or shape.polygon.is_empty:
                continue
            try:
                if not shape.polygon.intersects(footprint):
                    continue
            except _GEOM_EXC:
                continue
            ring = list(shape.polygon.exterior.coords)
            if ring and ring[0] == ring[-1]:
                ring = ring[:-1]
            node_altitudes = (
                list(shape.node_altitudes[:len(ring)])
                if shape.node_altitudes else None
            )
            changed = False
            for vertex_index, (x, y) in enumerate(ring):
                try:
                    if not footprint.contains(Point(x, y)):
                        continue
                except _GEOM_EXC:
                    continue
                along = (
                    (x - start_mid.x) * axis_unit[0]
                    + (y - start_mid.y) * axis_unit[1]
                )
                pin_value = bridge_profile_pin_elevation_m(
                    datum, bridge.deck_top_profile, along
                )
                _record_pin(layout, x, y, pin_value)
                if (node_altitudes is not None
                        and vertex_index < len(node_altitudes)):
                    node_altitudes[vertex_index] = round(pin_value, 2)
                    changed = True
                pinned_interior += 1
            if changed and node_altitudes is not None:
                shape.node_altitudes = node_altitudes + [node_altitudes[0]]
        if pinned_interior:
            UI.vprint(
                2,
                f"   [object-bridge] {pinned_interior} profile pin(s) "
                f"across PROFILE_CARRIED span {bridge.object_resources}",
            )
        total_pinned += pinned_interior
    return total_pinned


def _emit_deck_lip_weld_strips(layout, bridge, deck_box, trench_polygon,
                               road_exit_corridor, causeway_parts, datum,
                               born_graded) -> int:
    """Deck-lip weld strips (user directive 2026-07-15: aircraft must
    taxi SMOOTHLY onto the decks — the resumed pavement must slightly
    OVERLAP the deck-elevation terrain, not merely touch it; at
    mesh-triangulation level edge-to-edge contact still leaves sliver
    triangles).

    Hard decks only: ruling R8 cuts the pavement AT the deck-box
    boundary while the trench plate is inset :data:`_TRENCH_INSET_M`,
    leaving a ring of raw mesh whose triangles dive to the trench floor
    right at the lip (measured KBNA taxiway-L: pavement pinned 167.0,
    trench edge 0.8-1.2 m away at 161.01 — the owner-visible gap onto
    the deck).  For every rim segment fronted by airside pavement
    (:data:`_BRIDGE_PIN_ROLES` within :data:`_DECK_WELD_FRONT_REACH_M`
    of the box — service roads descend through the road-exit cut and
    never weld to the deck), a strip is born at the deck-top PROFILE
    law value (``grade_law.bridge_profile_pin_elevation_m`` along the
    abutment-to-abutment axis — the deck-end pin law at the ends by
    construction), spanning from :data:`_CAUSEWAY_INWARD_OVERLAP_M`
    inside the box (preserving the R2 node-split wall against the
    trench) out to :data:`_DECK_WELD_OVERLAP_M` INSIDE the pavement.
    The fronting pavement ring vertices inside the overlap band are
    pinned at the same law value, so the overlap is coplanar and
    invisible.  The emitted causeway plates are cut out (the plates
    stay mutually exclusive); road faces get no strips by construction
    (the zone is pavement-driven and roads never cross airside pavement
    at grade), and pavement vertices inside the road-exit corridor are
    never pinned.  Returns the number of strip parts."""
    if not getattr(bridge, "hard_deck", False):
        # Cosmetic decks keep their pavement over the box (R2 pavement
        # wins) — no lip cut, nothing to weld.
        return 0
    from .grade_law import bridge_profile_pin_elevation_m
    from .layout import ROLE_BRIDGE_CAUSEWAY
    abutment_lines = _abutment_lines_layout_meters(
        bridge, layout, extension_fraction=0.0)
    if len(abutment_lines) < 2:
        return 0
    start_mid = abutment_lines[0].interpolate(0.5, normalized=True)
    far_mid = abutment_lines[1].interpolate(0.5, normalized=True)
    axis_x = far_mid.x - start_mid.x
    axis_y = far_mid.y - start_mid.y
    axis_norm = math.hypot(axis_x, axis_y)
    if axis_norm < 1.0:
        return 0
    axis_x /= axis_norm
    axis_y /= axis_norm
    profile = list(bridge.deck_top_profile or [])

    def _lip_value(x, y):
        along = (x - start_mid.x) * axis_x + (y - start_mid.y) * axis_y
        return bridge_profile_pin_elevation_m(datum, profile, along)

    fronting_indices = []
    for shape_index, shape in enumerate(layout.shapes):
        if shape.role not in _BRIDGE_PIN_ROLES:
            continue
        if shape.polygon is None or shape.polygon.is_empty:
            continue
        try:
            if (shape.polygon.distance(deck_box)
                    > _DECK_WELD_FRONT_REACH_M):
                continue
        except _GEOM_EXC:
            continue
        fronting_indices.append(shape_index)
    if not fronting_indices:
        return 0
    try:
        pavement_union = unary_union(
            [layout.shapes[i].polygon for i in fronting_indices])
        # Rim inside the box, down to the causeway-inward depth — only
        # the portions a fronting pavement edge actually faces.
        inner_rim = deck_box.difference(
            deck_box.buffer(-_CAUSEWAY_INWARD_OVERLAP_M))
        inner_rim = inner_rim.intersection(
            pavement_union.buffer(_DECK_WELD_FRONT_REACH_M))
        # The coplanar overlap: reach into the pavement past its cut.
        overlap_band = pavement_union.intersection(
            deck_box.buffer(_DECK_WELD_OVERLAP_M))
        strip = unary_union([inner_rim, overlap_band])
        if trench_polygon is not None:
            # R2 node-split wall: keep the same clearance to the trench
            # the causeway keeps (> the 0.5 m node-interning tolerance).
            strip = strip.difference(trench_polygon.buffer(
                _TRENCH_INSET_M - _CAUSEWAY_INWARD_OVERLAP_M))
        # The road-exit corridor is deliberately NOT subtracted from the
        # strip geometry: under the deck the road runs on the trench
        # floor a storey BELOW the lip strips (measured KBNA: the
        # trench-width Donelson corridor covers the whole box interior
        # and its polygon deleted every long-side strip), and the strip
        # zone is pavement-driven — roads never cross airside pavement
        # at grade, so no strip can land on the road's actual ground
        # opening at the trench mouths.  The pin loop below still skips
        # any pavement vertex inside the corridor (belt and braces).
        for causeway_part in causeway_parts:
            strip = strip.difference(causeway_part)
    except _GEOM_EXC:
        return 0
    strip_parts = list(strip.geoms) if hasattr(strip, "geoms") else [strip]
    # A strip wrapping the whole box is an ANNULUS (exterior + hole over
    # the trench); ring emission is exterior-only and would fill the
    # hole at the lip value, damming the corridor.  Break the loop with
    # a hair-line cut along the deck axis (crosses the rim at both
    # ends; the 0.1 m slot is negligible and object-hidden).
    simply_connected = []
    for part in strip_parts:
        if part.geom_type == "Polygon" and part.interiors:
            try:
                axis_cut = LineString([
                    (start_mid.x - axis_x * 5.0, start_mid.y - axis_y * 5.0),
                    (far_mid.x + axis_x * 5.0, far_mid.y + axis_y * 5.0),
                ]).buffer(0.05)
                split = part.difference(axis_cut)
                simply_connected.extend(
                    split.geoms if hasattr(split, "geoms") else [split])
                continue
            except _GEOM_EXC:
                continue
        simply_connected.append(part)
    emitted = 0
    for part in simply_connected:
        if (part.geom_type != "Polygon" or part.is_empty
                or part.area < 1.0 or part.interiors):
            continue
        try:
            born_graded(part, ROLE_BRIDGE_CAUSEWAY,
                        "object_bridge_deck_weld", _lip_value)
        except _GEOM_EXC:
            continue
        emitted += 1
    if not emitted:
        return 0
    # Pin the fronting pavement ring vertices inside the overlap band at
    # the same law value (node_altitudes stamp + solver pin registry) —
    # both surfaces of the overlap are then coplanar by construction.
    try:
        pin_zone = deck_box.buffer(_DECK_WELD_OVERLAP_M + 0.05)
    except _GEOM_EXC:
        return emitted
    for shape_index in fronting_indices:
        shape = layout.shapes[shape_index]
        try:
            ring = list(shape.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        node_altitudes = (
            list(shape.node_altitudes[:len(ring)])
            if shape.node_altitudes else None)
        changed = False
        for vertex_index, (x, y) in enumerate(ring):
            vertex_point = Point(x, y)
            if not pin_zone.covers(vertex_point):
                continue
            if (road_exit_corridor is not None
                    and road_exit_corridor.covers(vertex_point)):
                continue  # the road descends through the exit cut
            value = round(float(_lip_value(x, y)), 2)
            _record_pin(layout, x, y, value)
            if (node_altitudes is not None
                    and vertex_index < len(node_altitudes)):
                node_altitudes[vertex_index] = value
            changed = True
        if changed and node_altitudes is not None:
            shape.node_altitudes = node_altitudes + [node_altitudes[0]]
    return emitted


def build_bridge_layout_shapes(layout, dem, tile_lat, tile_lon):
    """User ruling R12 — bridge terrain as FIRST-CLASS layout shapes,
    born pre-solve with law values, immutable thereafter (the one-solve
    doctrine applied fully; replaces the post-solve trench emission and
    the late causeway plates).

    Per corridor (DECK_CARRIED / cosmetic, not road-carried) bridge:

    * **Building-pad removal** (the never-stack rule): the Phase 1 DSF
      building machinery footprint-extracts the bridge OBJECTS
      themselves into flat building pads over the decks (KBNA:
      ``building2`` covered the taxiway-L footprint 2959/2959 m² — the
      measured coverer that ate the trench in every gated build).  A
      building pad mostly inside ANY classified bridge footprint is a
      stacking artifact and is removed, logged per pad.
    * **Ruling R8 flush seat**: pavement cut over a genuine hard deck
      (``_cut_pavement_over_hard_deck``); a cosmetic deck keeps its
      pavement (R2 pavement wins) and the trench carves around it.
    * **Trench** (:data:`layout.ROLE_BRIDGE_TRENCH`): the under-deck
      footprint inset 0.6 m (> the 0.5 m weld tolerance — the R2
      node-split wall against the causeway lip), densified to ~5 m
      vertex spacing, per-vertex ``node_altitudes`` at the law floor
      (``_bridge_corridor_floor_m``, amendment A10 geometry-driven).
    * **Causeway** (:data:`layout.ROLE_BRIDGE_CAUSEWAY`): the flat plate
      from each abutment lip back along the outward approach axis to
      the first pavement edge (+2 m weld overlap, clipped by the
      pavement union — R2), capped at
      ``config.BRIDGE_CAUSEWAY_MAX_LENGTH_M``; per-vertex
      ``node_altitudes`` at ``grade_law.bridge_deck_end_pin_elevation_m``
      (the SAME law function as the pins and the validator).

    Both roles are outside every mutation pass by construction: not
    pavement (solver never reshapes them), not road features (deconflict
    never walks them), no within-shape grade rule
    (``config.ROLE_GRADE_LIMITS`` ``None``).  Returns
    ``(trench_count, causeway_count, pads_removed)``; all zeros when the
    gate is off (no classification cached)."""
    classification = _object_bridge_classification(layout)
    if classification is None:
        return 0, 0, 0
    from .grade_law import bridge_deck_end_pin_elevation_m
    from .layout import (
        ROLE_BRIDGE_CAUSEWAY,
        ROLE_BRIDGE_TRENCH,
        ROLE_BUILDING,
        ROLE_SERVICE_JUNCTION,
        ROLE_SERVICE_ROAD,
    )
    to_meters, _meters_to_lat_lon = _local_meter_projections(layout.anchor)

    # ── Building-pad removal over EVERY classified bridge footprint ──
    all_footprints = []
    for bridge in classification.bridges:
        footprint = _bridge_footprint_meters(bridge, to_meters)
        if footprint is not None:
            all_footprints.append((bridge, footprint))
    pads_removed = 0
    # Captured full-structure footprints (defect C, 2026-07-15): the
    # removed pads ARE the objects' FULL solid footprints
    # (``read_dsf_object_buildings`` → ``structure_ring``) — the portal
    # collar needs that full lateral extent (the deck-face union is
    # 6.3 m wide against the 13.3 m emitted back side at KBNA portal
    # 0), so keep the polygons per matched bridge instead of dropping
    # them on the floor.
    full_footprints_by_bridge_id: dict[int, list] = {}
    if all_footprints:
        kept_shapes = []
        for shape in layout.shapes:
            if (shape.role != ROLE_BUILDING
                    or shape.polygon is None or shape.polygon.is_empty):
                kept_shapes.append(shape)
                continue
            removed = False
            for bridge, footprint in all_footprints:
                try:
                    overlap = shape.polygon.intersection(footprint).area
                except _GEOM_EXC:
                    continue
                # Either-side criterion (measured, KBNA): the taxiway-L
                # pad is 100 % inside its footprint, but the Crossing /
                # Murfreesboro pads are LARGER than their deck boxes
                # (overlap 71 % / 98 % / 33 % of the FOOTPRINT while
                # under half of the pad) — a pad covering a third of a
                # deck box is still the bridge object's own pad.
                if (overlap >= 0.5 * shape.polygon.area
                        or overlap >= 0.3 * footprint.area):
                    pads_removed += 1
                    removed = True
                    full_footprints_by_bridge_id.setdefault(
                        id(bridge), []).append(shape.polygon)
                    UI.vprint(
                        1,
                        "   [object-bridge] removed building pad "
                        f"{shape.ref!r} over the deck of "
                        f"{bridge.object_resources} (terrain-to-object "
                        "corrections never stack)",
                    )
                    break
            if not removed:
                kept_shapes.append(shape)
        if pads_removed:
            layout.shapes = kept_shapes

    # Portal-pair detection runs HERE — the first Feature B consumer in
    # the pipeline — so every later partition call (pins, corridors,
    # validators) sees the same diversion via the layout cache.
    portal_pairs = _detect_tunnel_portal_pairs(
        layout, dem, tile_lat, tile_lon
    )
    (corridor_bridges, _suppress, _refused, _road_carried,
     _portal_records) = (
        _partition_bridges_for_corridors(classification, layout)
    )
    if not corridor_bridges and not portal_pairs:
        return 0, 0, pads_removed

    weld_roles = _BRIDGE_PIN_ROLES | {
        ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION,
    }

    def _pavement_union():
        polygons = [
            shape.polygon for shape in layout.shapes
            if shape.role in weld_roles
            and shape.polygon is not None and not shape.polygon.is_empty
        ]
        try:
            return unary_union(polygons) if polygons else None
        except _GEOM_EXC:
            return None

    def _born_flat(polygon, role, ref, elevation, record_pins=True):
        """Bind :func:`born_flat_solver_plate` to this layout (the shared
        R12 flat-plate birth primitive; see its docstring)."""
        return born_flat_solver_plate(layout, polygon, role, ref,
                                      elevation, record_pins=record_pins)

    def _born_graded(polygon, role, ref, altitude_at, record_pins=True):
        """Like :func:`_born_flat`, but every ring vertex takes its OWN
        law value from ``altitude_at(x, y)`` — a TRANSITION plate (the
        portal collar): crown-high at the object-hidden inner face,
        DEM-low at the exposed outer rim, so its perimeter feathers into
        the surrounding ground instead of standing as a vertical
        stretched-texture wall.  With ``record_pins`` (the default) each
        vertex is registered as a hard solver pin at its own value,
        exactly like ``_born_flat``; hanging-face portals pass False —
        their plates are DECOUPLED law plates (the Feature-A lesson:
        deep pinned plates couple through the one-solve and drag the
        neighbouring pavement toward the mouth floor)."""
        try:
            dense = polygon.segmentize(3.0)
        except (AttributeError, _GEOM_EXC):
            dense = polygon
        ring = list(dense.exterior.coords)
        if len(ring) < 4:
            return 0
        node_altitudes = [
            round(float(altitude_at(x, y)), 2) for x, y in ring
        ]
        # shapely closes exterior rings, so ring[0] == ring[-1]; keep the
        # closing altitude byte-identical to the first vertex's.
        if ring[0] == ring[-1]:
            node_altitudes[-1] = node_altitudes[0]
            vertex_count = len(ring) - 1
        else:
            vertex_count = len(ring)
        if record_pins:
            for (x, y), altitude in zip(
                    ring[:vertex_count], node_altitudes[:vertex_count]):
                _record_pin(layout, x, y, altitude)
        layout.shapes.append(BuiltShape(
            polygon=dense,
            role=role,
            ref=ref,
            node_altitudes=node_altitudes))
        return vertex_count

    maximum_length = float(_CFG.BRIDGE_CAUSEWAY_MAX_LENGTH_M)
    capture_band = float(_CFG.BRIDGE_ABUTMENT_PIN_CAPTURE_BAND_M)
    n_trench = 0
    n_causeway = 0

    # ── Tunnel portal mouths (user ruling 2026-07-10, runway-02C class):
    # a paired portal gets NO bridge treatment.  Its mouth is born as a
    # flat plate at the ROAD grade over the portal footprint — the object
    # drapes at terrain(anchor) = mouth floor, so it sits partly
    # submerged with its bottom aligned to the road descending to it,
    # and the plate's rim welds to the natural hill (level with the
    # object top at the face, rising to the runway beyond).  The ground
    # between the two portals is left UNTOUCHED — the hill carries the
    # runway; the outward road corridor is emitted by the corridors
    # stage (climbing away from the mouth by construction).
    # Airside pavement union for the FACE-portal plate clips (user JOSM
    # review 2026-07-18d: at EGGW the crown/collar sat 84-98 % under the
    # taxiway junctions over the bore — pavement owns its ground, R2).
    airside_pavement_union = _pavement_union() if portal_pairs else None
    for pair in portal_pairs:
        for portal in pair["portals"]:
            mouth_floor = portal.get("mouth_floor_m")
            footprint = portal.get("footprint")
            if mouth_floor is None or footprint is None:
                continue
            # FACE portals (user 2026-07-18, screenshots): the crown
            # split, forward sweep and collar clip must align with THE
            # FACE, not the pair axis — a road can enter the portal
            # obliquely (EGGW: 58° crossing), and splitting by the pair
            # axis draws the crown edge diagonally across the flat face
            # object.  Redirect this portal's working ``outward`` to
            # the FACE NORMAL (sign-matched to the road side) before
            # any plate geometry is derived; the road-following mouth
            # floor was already sampled along the true road direction
            # at pairing time and is unaffected.
            if pair.get("is_face") and portal.get("outward") is not None:
                face_line_bearing = math.radians(float(getattr(
                    portal["bridge"], "face_line_bearing_degrees", 0.0)))
                face_along = (math.sin(face_line_bearing),
                              math.cos(face_line_bearing))
                face_normal = (-face_along[1], face_along[0])
                road_outward = portal["outward"]
                if (face_normal[0] * road_outward[0]
                        + face_normal[1] * road_outward[1]) < 0.0:
                    face_normal = (-face_normal[0], -face_normal[1])
                portal["outward"] = face_normal
            # Crown split (user ruling 2026-07-14): the buried half of
            # the footprint — the side facing the runway over the
            # tunnel body — is seated at the OBJECT TOP (mouth floor +
            # deck top), so the terrain runway-side of the portal rides
            # over the tunnel roof instead of dipping to road grade.
            # Only the open-mouth half stays at the road.
            deck_top_metres = float(
                portal["bridge"].deck_top_y_m or 0.0)
            mouth_geometry = footprint
            crown_geometry = None
            mouth_half_plane = None
            if (_CFG.TUNNEL_PORTAL_CROWN and deck_top_metres > 0.5
                    and portal.get("outward") is not None):
                mouth_half, buried_half, mouth_half_plane = (
                    _split_portal_footprint(footprint, portal["outward"]))
                if (mouth_half is not None and buried_half is not None
                        and buried_half.area >= 4.0):
                    mouth_geometry = mouth_half
                    crown_geometry = buried_half
                    # NODE-SPLIT THE FACE MEETING for face portals
                    # (user 2026-07-18, screenshots): the mouth's back
                    # edge and the crown's front edge lie on the SAME
                    # face line — interned into one ~0.5 m node bucket
                    # the first writer's road-grade value wins and the
                    # crown renders as a RAMP across its whole depth
                    # (measured EGGW v18: 148.5 → 154.8 over the crown
                    # zone instead of a flat 157.8 shelf).  Trim the
                    # crown 1.0 m off the face so the two rows stay
                    # distinct nodes: mouth at road grade, crown flat
                    # at the object top, a near-vertical wall between
                    # them that the face object itself covers.  KBNA
                    # structural portals keep the coincident meeting —
                    # their objects hide it by design.
                    if pair.get("is_face") and mouth_half_plane is not None:
                        try:
                            outward_vector = portal["outward"]
                            face_setback = shapely_translate(
                                mouth_half_plane,
                                xoff=-outward_vector[0] * 1.0,
                                yoff=-outward_vector[1] * 1.0)
                            trimmed = crown_geometry.difference(
                                face_setback)
                            if (trimmed.geom_type == "MultiPolygon"
                                    and not trimmed.is_empty):
                                trimmed = max(
                                    trimmed.geoms,
                                    key=lambda part: part.area)
                            if (trimmed.geom_type == "Polygon"
                                    and not trimmed.is_empty
                                    and trimmed.area >= 4.0):
                                crown_geometry = trimmed
                        except _GEOM_EXC:
                            pass
            # Defect C (2026-07-15): the collar derives its lateral
            # extent from the object's FULL solid footprint (the
            # captured never-stack building pad), not the deck-face
            # union — measured KBNA: collars covered 86-87 % of the
            # portal back width, one side truncated by up to 8 m.
            # Clamped to the footprint's neighbourhood: the pads flow
            # through the building CLUSTERING passes and may be merged
            # with neighbours.
            plain_mouth_geometry = mouth_geometry
            anchor_seat = None
            anchor_seat_keep_out = None
            full_footprint = None
            captured_pads = full_footprints_by_bridge_id.get(
                id(portal["bridge"]), [])
            if captured_pads:
                try:
                    candidate = unary_union(
                        [footprint] + list(captured_pads)
                    ).intersection(footprint.buffer(25.0))
                    if candidate.geom_type == "MultiPolygon":
                        candidate = max(
                            candidate.geoms,
                            key=lambda geometry: geometry.area)
                    if (candidate.geom_type == "Polygon"
                            and not candidate.is_empty):
                        full_footprint = candidate
                except _GEOM_EXC:
                    full_footprint = None
            # Persist on the cached pair record (crossing influence zone,
            # spec Phase 1): the zone is published from the pairs AFTER
            # this function returns, and its portal pieces / collar rings
            # must reach the FULL solid extent the collar is cut from.
            portal["full_footprint"] = full_footprint
            # X-Plane drapes the portal object at terrain(anchor) — the
            # ROAD-GRADE plate must COVER the anchor point, or the
            # object seats on whatever solves beside it (a crown plate
            # under the anchor would LIFT the object by the deck top).
            # INVERTED for a portal-FACE pair (user 2026-07-17, EGGW
            # class): the face geometry HANGS BELOW its origin, so the
            # anchor must read DECK grade (the crown) — seating it on
            # the road-grade mouth would sink the whole face by the
            # face height.
            _face_seated = bool(pair.get("is_face")) or bool(
                getattr(portal["bridge"], "face_hangs_below", False))
            try:
                anchor_longitude, anchor_latitude = (
                    portal["bridge"].anchor_longitude_latitude
                )
                anchor_point = Point(
                    to_meters(anchor_longitude, anchor_latitude)
                )
                if _face_seated:
                    # SEAT REDUNDANCY (user JOSM review 2026-07-18d): an
                    # anchor lying UNDER the airside pavement needs no
                    # terrain seat at all — the pavement solves at the
                    # airside level the crown raises to, so the object
                    # drapes at deck grade off the pavement itself, and
                    # a seat there would double-grade the junction (the
                    # last 19-22 m2 plate-vs-pavement overlaps at EGGW).
                    _anchor_on_pavement = False
                    if airside_pavement_union is not None:
                        try:
                            _anchor_on_pavement = (
                                airside_pavement_union.covers(anchor_point))
                        except _GEOM_EXC:
                            _anchor_on_pavement = False
                    if (not _anchor_on_pavement
                            and footprint.distance(anchor_point) <= 30.0):
                        # FACE-ALIGNED anchor seat (user screenshots
                        # 2026-07-18b, EGGW): the previous 5 m ROUND
                        # disk protruded 5 m into the road at deck
                        # grade, and the mouth hole cut from the SAME
                        # disk shared its rim coordinates — one ~0.5 m
                        # mesh bucket per rim node, altitude decided by
                        # first-writer interning.  Mid-road it rendered
                        # as a ~10 m arc-shaped tower at both EGGW
                        # mouths.  Build a rectangle in the FACE frame
                        # instead — a minimal outward lip so the
                        # anchor's drape triangle stays wholly at deck
                        # grade, square edges flush against the face —
                        # and cut the mouth back an extra clearance
                        # margin so no seat node shares a bucket with a
                        # mouth node (the v18 face-meeting trap).
                        seat_outward = portal.get("outward")
                        if seat_outward is not None:
                            along_face = (-seat_outward[1],
                                          seat_outward[0])
                            half_width = float(
                                _CFG.PORTAL_FACE_ANCHOR_SEAT_HALF_WIDTH_M)
                            outward_lip = float(
                                _CFG.PORTAL_FACE_ANCHOR_SEAT_OUTWARD_M)
                            inward_reach = float(
                                _CFG.PORTAL_FACE_ANCHOR_SEAT_INWARD_M)
                            front = (
                                anchor_point.x
                                + seat_outward[0] * outward_lip,
                                anchor_point.y
                                + seat_outward[1] * outward_lip)
                            back = (
                                anchor_point.x
                                - seat_outward[0] * inward_reach,
                                anchor_point.y
                                - seat_outward[1] * inward_reach)
                            anchor_seat = Polygon([
                                (front[0] + along_face[0] * half_width,
                                 front[1] + along_face[1] * half_width),
                                (front[0] - along_face[0] * half_width,
                                 front[1] - along_face[1] * half_width),
                                (back[0] - along_face[0] * half_width,
                                 back[1] - along_face[1] * half_width),
                                (back[0] + along_face[0] * half_width,
                                 back[1] + along_face[1] * half_width),
                            ])
                            # An off-pavement anchor near the pavement
                            # edge: the seat's inward reach may cross
                            # under the junction — trim it back with the
                            # 0.6 m node-split margin, UNLESS that would
                            # eat the anchor's own drape neighbourhood
                            # (coverage beats overlap cleanliness; user
                            # JOSM review 2026-07-18d).  With the seat's
                            # front edge ON the face line the anchor
                            # legitimately sits on the exterior — the
                            # guard is that the front edge SURVIVES
                            # locally: the trimmed part still covers the
                            # anchor and keeps most of a 0.6 m half-disk
                            # behind it.
                            if airside_pavement_union is not None:
                                try:
                                    _seat_trim = anchor_seat.difference(
                                        airside_pavement_union.buffer(
                                            0.6, join_style=2,
                                            mitre_limit=2.0))
                                    _seat_parts = [
                                        part for part in getattr(
                                            _seat_trim, "geoms",
                                            [_seat_trim])
                                        if part.geom_type == "Polygon"
                                        # covers() is float-fragile for
                                        # the on-edge anchor; micron
                                        # distance is the robust test.
                                        and part.distance(anchor_point)
                                        < 1e-6]
                                    if _seat_parts and (
                                            _seat_parts[0].intersection(
                                                anchor_point.buffer(0.6))
                                            .area >= 0.4):
                                        anchor_seat = _seat_parts[0]
                                except _GEOM_EXC:
                                    pass
                            anchor_seat_keep_out = anchor_seat.buffer(
                                float(_CFG.
                                      PORTAL_FACE_ANCHOR_SEAT_CLEARANCE_M),
                                join_style=2, mitre_limit=2.0)
                        else:
                            # No face frame to align to — fall back to
                            # the round seat rather than leave the
                            # anchor over the road-grade mouth.
                            anchor_seat = anchor_point.buffer(5.0)
                            anchor_seat_keep_out = anchor_seat
                        mouth_geometry = mouth_geometry.difference(
                            anchor_seat_keep_out)
                        if mouth_geometry.geom_type != "Polygon":
                            mouth_geometry = max(
                                (part for part in getattr(
                                    mouth_geometry, "geoms", [])
                                 if part.geom_type == "Polygon"),
                                key=lambda part: part.area,
                                default=None)
                        if crown_geometry is not None:
                            crown_geometry = unary_union(
                                [crown_geometry, anchor_seat])
                            if crown_geometry.geom_type != "Polygon":
                                crown_geometry = (
                                    crown_geometry.convex_hull)
                        else:
                            crown_geometry = anchor_seat
                        if mouth_geometry is None \
                                or mouth_geometry.is_empty:
                            mouth_geometry = None
                elif (not mouth_geometry.covers(anchor_point)
                        and footprint.distance(anchor_point) <= 30.0):
                    anchor_seat = anchor_point.buffer(5.0)
                    anchor_seat_keep_out = anchor_seat
                    mouth_geometry = unary_union(
                        [mouth_geometry, anchor_seat]
                    )
                    if mouth_geometry.geom_type != "Polygon":
                        mouth_geometry = mouth_geometry.convex_hull
                    if crown_geometry is not None:
                        crown_geometry = crown_geometry.difference(
                            anchor_seat)
                        if crown_geometry.geom_type != "Polygon":
                            crown_geometry = max(
                                (part for part in getattr(
                                    crown_geometry, "geoms", [])
                                 if part.geom_type == "Polygon"),
                                key=lambda part: part.area,
                                default=None)
                        if (crown_geometry is not None
                                and (crown_geometry.is_empty
                                     or crown_geometry.area < 4.0)):
                            crown_geometry = None
            except (_GEOM_EXC, KeyError, TypeError):
                pass
            # PAVEMENT WINS over face-portal plates (user JOSM review
            # 2026-07-18d, ruling R2 applied to the portal family): at
            # EGGW the synthesized plate rectangles land 84-98 % under
            # the taxiway junctions crossing the bore — the junctions
            # own that ground (the raise pass already matches the crown
            # to the solved airside there), and double-graded ground
            # rendered as the JOSM overlap mess.  Clip mouth and crown
            # against the airside union with a 0.6 m node-split margin;
            # the ANCHOR SEAT is exempt (the object drapes at
            # terrain(anchor) — it must stay covered at deck grade).
            # KBNA-class structural portals keep their accepted
            # geometry (no clip).
            if (pair.get("is_face")
                    and airside_pavement_union is not None):
                try:
                    _pavement_keep_out = airside_pavement_union.buffer(
                        0.6, join_style=2, mitre_limit=2.0)
                    if (mouth_geometry is not None
                            and not mouth_geometry.is_empty):
                        _clipped_mouth = mouth_geometry.difference(
                            _pavement_keep_out)
                        if anchor_seat_keep_out is not None:
                            _clipped_mouth = _clipped_mouth.difference(
                                anchor_seat_keep_out)
                        _mouth_parts = [
                            part for part in getattr(
                                _clipped_mouth, "geoms", [_clipped_mouth])
                            if part.geom_type == "Polygon"
                            and part.area >= 4.0]
                        mouth_geometry = (
                            max(_mouth_parts, key=lambda p: p.area)
                            if _mouth_parts else None)
                    if (crown_geometry is not None
                            and not crown_geometry.is_empty):
                        _clipped_crown = crown_geometry.difference(
                            _pavement_keep_out)
                        if anchor_seat is not None:
                            _clipped_crown = unary_union(
                                [_clipped_crown, anchor_seat])
                        if _clipped_crown.is_empty:
                            crown_geometry = None
                        else:
                            crown_geometry = _clipped_crown
                except _GEOM_EXC:
                    pass
            # Hanging-face portals emit DECOUPLED law plates (the
            # Feature-A lesson, measured twice: solver-pinned deep
            # plates couple through the one-solve and drag the
            # neighbouring pavement toward the mouth floor — EGGW's
            # taxiway over the bore solved 2.6 m low with pinned
            # ROLE_BRIDGE_TRENCH mouths).  ``ROLE_TUNNEL_TRENCH`` is
            # the decoupled LAW-tier role: decimation-exempt,
            # force_per_node, never in the solver's pavement set.
            portal_plate_role = (ROLE_TUNNEL_TRENCH if _face_seated
                                 else ROLE_BRIDGE_TRENCH)
            portal_plate_pins = not _face_seated
            if mouth_geometry is None or mouth_geometry.is_empty:
                # Face-seated portal whose whole footprint became the
                # crown (thin face footprints): road grade continues in
                # the outward corridor; only the crown seat is emitted.
                vertex_count = 0
            else:
                try:
                    vertex_count = _born_flat(
                        mouth_geometry, portal_plate_role,
                        "object_tunnel_portal_mouth", mouth_floor,
                        record_pins=portal_plate_pins)
                except _GEOM_EXC:
                    continue
            n_trench += 1
            UI.vprint(
                1,
                "   [object-tunnel] portal mouth seated at road grade "
                f"{mouth_floor:.2f} m ({vertex_count} vertices) for "
                f"{portal['bridge'].object_resources}",
            )
            if crown_geometry is None:
                continue
            # User correction 2026-07-14c: the object top includes a
            # parapet/safety wall, so ``mouth + deck_top`` overshoots
            # the ground (KBNA: structure tops 185.0/185.3 against real
            # ground 174/177).  The crown holds the TERRAIN's own height
            # over the buried body.
            #
            # Round-8 fact 2 — TERRAIN-TRUE crown from the SAME
            # production inset DEM: the crown IS the runway embankment
            # riding over the tunnel roof, so seat it at the inset DEM
            # sampled over its OWN BURIED BODY (the crown geometry),
            # NOT at a single point 4 m beyond the buried edge.  That
            # prior mechanism read the mid-hill downslope on each
            # portal's inner face and diverged the two crowns (measured
            # KBNA 02C baseline: east crown 180.34 m vs west 181.32 m
            # over one embankment, each 1.5-1.9 m above its mouth).
            # Sample AT the crown centroid — the reported target — plus
            # a tight interior disk so a lone nodata / spike cell cannot
            # swing the plate, then clamp to a sane band around the
            # mouth (never above the object top, never a DEM dropout).
            crown_elevation = mouth_floor + deck_top_metres
            burial_x = -portal["outward"][0]
            burial_y = -portal["outward"][1]
            centroid_point = footprint.centroid
            buried_extent = 0.0
            try:
                for vertex_x, vertex_y in footprint.exterior.coords:
                    projection = (
                        (vertex_x - centroid_point.x) * burial_x
                        + (vertex_y - centroid_point.y) * burial_y
                    )
                    if projection > buried_extent:
                        buried_extent = projection
            except _GEOM_EXC:
                buried_extent = 0.0
            # ``ground_behind`` is retained ONLY as the collar's
            # outer-target fallback below (used when a collar vertex's
            # own DEM disk is entirely nodata).
            try:
                sample_lat, sample_lon = _meters_to_lat_lon(
                    centroid_point.x + burial_x * (buried_extent + 4.0),
                    centroid_point.y + burial_y * (buried_extent + 4.0),
                )
                ground_behind = _sample_dem(
                    dem, tile_lat, tile_lon, sample_lat, sample_lon
                )
            except _GEOM_EXC:
                ground_behind = None
            crown_centroid = crown_geometry.centroid
            crown_target = None
            try:
                target_lat, target_lon = _meters_to_lat_lon(
                    crown_centroid.x, crown_centroid.y)
                crown_target = _sample_dem(
                    dem, tile_lat, tile_lon, target_lat, target_lon)
            except _GEOM_EXC:
                crown_target = None
            disk_samples = []
            for radius in (2.0, 4.0):
                for step_index in range(6):
                    angle = math.pi * step_index / 3.0
                    try:
                        probe_lat, probe_lon = _meters_to_lat_lon(
                            crown_centroid.x + radius * math.cos(angle),
                            crown_centroid.y + radius * math.sin(angle))
                        sample = _sample_dem(
                            dem, tile_lat, tile_lon, probe_lat, probe_lon)
                    except (_GEOM_EXC, ValueError, TypeError):
                        sample = None
                    if sample is not None:
                        disk_samples.append(float(sample))
            crown_ground = (
                float(crown_target) if crown_target is not None
                else (sum(disk_samples) / len(disk_samples)
                      if disk_samples else None))
            # USER RULING 2026-07-17 — the OBJECT is the terrain
            # authority: its flat roof plane (``mouth_floor +
            # deck_top`` — the cosmetic classifier's dominant elevated
            # plane, parapet caps excluded by area) is the divider
            # between the below-grade road and the at-grade back
            # terrain, so the crown seats NO LOWER than that plane.
            # The DEM stays as an UPWARD override only (a hillside
            # portal buried deeper keeps the higher terrain).  The
            # former DEM-target seating left the Murfreesboro west
            # crown at 171.2 where the object roof is 176.8 — the
            # portal-mouth backside stood exposed toward the runway.
            # (The 2026-07-14c "DEM, not mouth+deck_top" correction
            # predates the mouth-floor fix; with today's mouth floors
            # the roof plane no longer overshoots real ground.)
            object_roof_elevation = mouth_floor + deck_top_metres
            if crown_ground is not None:
                crown_elevation = max(
                    float(crown_ground), object_roof_elevation)
            else:
                crown_elevation = object_roof_elevation
            crown_centroid_lat, crown_centroid_lon = _meters_to_lat_lon(
                crown_centroid.x, crown_centroid.y)
            UI.vprint(
                1,
                "   [object-tunnel] crown target: inset DEM at crown "
                f"centroid @{crown_centroid_lat:.5f},"
                f"{crown_centroid_lon:.5f} = "
                + (f"{crown_target:.2f} m" if crown_target is not None
                   else "nodata")
                + f" (mouth {mouth_floor:.2f}, object top "
                f"{mouth_floor + deck_top_metres:.2f})",
            )
            # The pavement clip can split the crown into parts (the seat
            # plus shoulder pieces flanking the taxiway) — birth each
            # part; the record carries them all for the airside raise.
            crown_part_list = [
                part for part in getattr(
                    crown_geometry, "geoms", [crown_geometry])
                if part.geom_type == "Polygon" and not part.is_empty
                and part.area >= 4.0]
            if not crown_part_list:
                continue
            crown_vertex_count = 0
            crown_shapes = []
            try:
                for crown_part in sorted(
                        crown_part_list, key=lambda p: -p.area):
                    part_count = _born_flat(
                        crown_part, portal_plate_role,
                        "object_tunnel_portal_crown", crown_elevation,
                        record_pins=portal_plate_pins)
                    if part_count:
                        crown_vertex_count += part_count
                        crown_shapes.append(layout.shapes[-1])
            except _GEOM_EXC:
                continue
            if not crown_shapes:
                continue
            # Portal terrain record (user ruling 2026-07-17): the
            # post-solve raise pass (``raise_portal_terrain_to_airside``,
            # called from finalize) lifts crown + collar to the
            # surrounding SOLVED airside level — capture the shape
            # references and blend parameters it needs.  Collar fields
            # are filled in below once computed; a ``continue`` before
            # then leaves a crown-only record, which the raise pass
            # handles.
            if not hasattr(layout, "portal_terrain_records"):
                layout.portal_terrain_records = []
            portal_terrain_record = {
                "crown_shape": crown_shapes[0],
                "crown_shapes": crown_shapes,
                "crown_geometry": crown_geometry,
                "crown_elevation": crown_elevation,
                "mouth_floor": mouth_floor,
                "collar_reach": float(_CFG.TUNNEL_PORTAL_CROWN_COLLAR_M),
                "collar_shapes": [],
                "mouth_geometry": None,
            }
            layout.portal_terrain_records.append(portal_terrain_record)
            n_trench += 1
            UI.vprint(
                1,
                "   [object-tunnel] portal crown seated TERRAIN-TRUE at "
                f"{crown_elevation:.2f} m ({crown_vertex_count} "
                "vertices) — the runway-side rim rides the tunnel "
                "roof",
            )
            # COLLAR (user ruling 2026-07-14; reworked for defect C,
            # 2026-07-15): a band around the BACK and sides of the
            # buried half, held at the crown elevation, so the ground
            # behind the portal keeps the deck/roof height while the
            # road grades down into the mouth on the other side.
            # Clipped to the buried side of the split, so it never
            # reaches the road.
            #
            # Defect C rework — the collar must span the WHOLE back
            # side of the portal OBJECT (measured KBNA: 86-87 %
            # coverage, up to 8 m truncated on one side):
            # * the band source is the FULL solid footprint (captured
            #   never-stack pad), not the disk-bitten crown plate
            #   built from the narrow deck-face union;
            # * the mouth-side clip is a FORWARD SWEEP of the
            #   footprint (translates along +outward over the collar
            #   depth), not the centroid half-plane — the measured
            #   KBNA portals are DIAGONAL bands relative to
            #   ``outward``, so a single global split line leaves the
            #   back side bare at one lateral end (the owner-observed
            #   slivers) while the sweep hugs the band's actual back
            #   edge and wraps the sides;
            # * subtract the PLAIN mouth half plus the anchor seat
            #   keep-out (mouth is the emitted road-grade plate; the
            #   seat is the deck-grade anchor cover) — never
            #   the convex-hull-enlarged mouth, whose hull fill ate
            #   up to 8 m of one collar side;
            # * keep ALL parts >= 4 m² instead of largest-part-only —
            #   coverage is the requirement; the largest-part rule
            #   was defensive against slivers and the 4 m² floor
            #   already handles those.
            if mouth_half_plane is None:
                continue
            outward_vector = portal.get("outward")
            if outward_vector is None:
                continue
            collar_footprint = (
                full_footprint if full_footprint is not None
                else footprint)
            collar_reach = float(_CFG.TUNNEL_PORTAL_CROWN_COLLAR_M)
            try:
                # Sweep length covers the collar reach PLUS the
                # footprint's own outward extent: on a diagonal band a
                # 10 m round buffer near the high-outward corner spills
                # 10-25 m forward of laterals whose own band column
                # sits far back (measured KBNA portal 1: a forward
                # wedge over the first approach quad).
                outward_extent = 0.0
                projections = [
                    (vertex_x * outward_vector[0]
                     + vertex_y * outward_vector[1])
                    for vertex_x, vertex_y
                    in collar_footprint.exterior.coords
                ]
                outward_extent = max(projections) - min(projections)
                sweep_length = collar_reach + outward_extent + 1.0
                sweep_steps = max(8, int(math.ceil(sweep_length / 1.5)))
                forward_sweep = unary_union([
                    shapely_translate(
                        collar_footprint,
                        xoff=outward_vector[0]
                        * sweep_length * step / sweep_steps,
                        yoff=outward_vector[1]
                        * sweep_length * step / sweep_steps,
                    )
                    for step in range(1, sweep_steps + 1)
                ])
                # Face portals get SQUARE collar corners (mitre join):
                # the round buffer's arcs read as curved ridges against
                # a flat straight-topped face object (user 2026-07-18
                # screenshots); KBNA-class structural portals keep the
                # round join against their natural hill.
                if pair.get("is_face"):
                    collar_band = collar_footprint.buffer(
                        collar_reach, join_style=2, mitre_limit=3.0)
                else:
                    collar_band = collar_footprint.buffer(collar_reach)
                collar_geometry = (
                    collar_band
                    .difference(collar_footprint)
                    .difference(footprint)
                    .difference(forward_sweep)
                    .difference(plain_mouth_geometry)
                )
                if anchor_seat_keep_out is not None:
                    collar_geometry = collar_geometry.difference(
                        anchor_seat_keep_out)
                # BACK-ONLY collar for FACE portals (user JOSM review
                # 2026-07-18d): the collar's job is to hold the ground
                # at the higher grade BEHIND the portal wall; terrain in
                # front slopes naturally down to the mouth.  The forward
                # sweep alone missed oblique roads (EGGW: the road
                # enters 25 deg off the face normal, and the south
                # collar's lateral lobe cut into the roadway).  Keep
                # ONLY the strict back half-plane, 1 m behind the face
                # line (matching the crown trim so no collar node lands
                # in the face-line bucket), and yield to airside
                # pavement with the same 0.6 m node-split margin as the
                # crown.  KBNA structural portals keep the sweep-based
                # wrap (accepted state).
                if pair.get("is_face"):
                    _face_centre = footprint.centroid
                    _along_face = (-outward_vector[1], outward_vector[0])
                    _back_reach = 500.0
                    _b0 = (_face_centre.x - outward_vector[0] * 1.0,
                           _face_centre.y - outward_vector[1] * 1.0)
                    back_half_plane = Polygon([
                        (_b0[0] + _along_face[0] * _back_reach,
                         _b0[1] + _along_face[1] * _back_reach),
                        (_b0[0] - _along_face[0] * _back_reach,
                         _b0[1] - _along_face[1] * _back_reach),
                        (_b0[0] - _along_face[0] * _back_reach
                         - outward_vector[0] * 2.0 * _back_reach,
                         _b0[1] - _along_face[1] * _back_reach
                         - outward_vector[1] * 2.0 * _back_reach),
                        (_b0[0] + _along_face[0] * _back_reach
                         - outward_vector[0] * 2.0 * _back_reach,
                         _b0[1] + _along_face[1] * _back_reach
                         - outward_vector[1] * 2.0 * _back_reach),
                    ])
                    collar_geometry = collar_geometry.intersection(
                        back_half_plane)
                    if airside_pavement_union is not None:
                        collar_geometry = collar_geometry.difference(
                            airside_pavement_union.buffer(
                                0.6, join_style=2, mitre_limit=2.0))
                # Round-8 fact 3 — NO collar part on the ROAD side of
                # the mouth face (a diagonal lateral lobe can round-
                # buffer past the forward sweep's end; measured KBNA 02C
                # baseline: a 34 m² lobe 46.7 m in front of the west
                # mouth @36.11196,-86.68564, flat at the crown height).
                # Two frames, both required for a diagonal-band portal:
                # * the PAIR AXIS (``outward``) against the footprint —
                #   the plane of the physical portal face the road
                #   emerges through;
                # * the OPENING AXIS (crown centroid → emitted mouth
                #   centroid) against the emitted MOUTH PLATE — a band
                #   end-lobe hugs the band's own tip BEHIND the pair-
                #   axis front line yet stands past the mouth plate over
                #   the descending road (the measured 20 m² survivor of
                #   the pair-axis clip alone).  Beyond the mouth plate's
                #   own extent along the opening, the terrain belongs to
                #   the road approaches, never the collar.
                collar_geometry = _clip_collar_to_mouth_front(
                    collar_geometry, footprint, outward_vector)
                try:
                    opening_x = (mouth_geometry.centroid.x
                                 - crown_geometry.centroid.x)
                    opening_y = (mouth_geometry.centroid.y
                                 - crown_geometry.centroid.y)
                    opening_norm = math.hypot(opening_x, opening_y)
                    if opening_norm > 0.5:
                        collar_geometry = _clip_collar_to_mouth_front(
                            collar_geometry, mouth_geometry,
                            (opening_x / opening_norm,
                             opening_y / opening_norm))
                except _GEOM_EXC:
                    pass
                # Road-lane clearance: the outward approach corridor —
                # the draped road buffered to the MOUTH-FACE width,
                # outward side only — is the approach chain's ground
                # (the road is offset from the band's centre, so its
                # lane reaches laterally past the band's end where no
                # sweep can exclude it; measured KBNA portal 1: the
                # collar end-lobe covered 201 m² of the first ramp
                # quad and the downstream plate cut left a 13 m²
                # quad-versus-quad overlap).  The collar yields the
                # lane; its BACK band (outward of the centroid split)
                # is untouched by the half-plane intersection.
                lane_lines = _draped_road_centerlines_meters(
                    portal["bridge"],
                    _object_bridge_road_networks(layout),
                    to_meters,
                )
                if lane_lines:
                    centroid_point = footprint.centroid
                    perpendicular = (-outward_vector[1],
                                     outward_vector[0])
                    face_projections = [
                        ((vertex_x - centroid_point.x) * perpendicular[0]
                         + (vertex_y - centroid_point.y)
                         * perpendicular[1])
                        for vertex_x, vertex_y
                        in footprint.exterior.coords
                    ]
                    mouth_face_width = (
                        max(face_projections) - min(face_projections))
                    reach = 1000.0
                    outward_half_plane = Polygon([
                        (centroid_point.x + perpendicular[0] * reach,
                         centroid_point.y + perpendicular[1] * reach),
                        (centroid_point.x - perpendicular[0] * reach,
                         centroid_point.y - perpendicular[1] * reach),
                        (centroid_point.x - perpendicular[0] * reach
                         + outward_vector[0] * 2.0 * reach,
                         centroid_point.y - perpendicular[1] * reach
                         + outward_vector[1] * 2.0 * reach),
                        (centroid_point.x + perpendicular[0] * reach
                         + outward_vector[0] * 2.0 * reach,
                         centroid_point.y + perpendicular[1] * reach
                         + outward_vector[1] * 2.0 * reach),
                    ])
                    lane_union = unary_union([
                        line.buffer(mouth_face_width / 2.0 + 1.0)
                        for line in lane_lines
                    ])
                    collar_geometry = collar_geometry.difference(
                        lane_union.intersection(outward_half_plane))
            except _GEOM_EXC:
                continue
            collar_parts = [
                part for part in (
                    collar_geometry.geoms
                    if hasattr(collar_geometry, "geoms")
                    else [collar_geometry])
                if part.geom_type == "Polygon" and not part.is_empty
                and part.area >= 4.0
            ]
            if not collar_parts:
                continue
            # Defect (2026-07-15): the collar was emitted FLAT at the
            # crown elevation, so its whole perimeter was a cliff (the
            # measured 7.76/7.09 m steps to the approach, 4.44 m to the
            # adjacent_ground band — vertical stretched-texture walls
            # flanking the portal objects).  It must be a TRANSITION
            # ring: the inner boundary meeting the crown keeps the crown
            # altitude (and the object facade hides its vertical meeting
            # with the low mouth — the by-design object-hidden face),
            # while the exposed outer rim tracks the surrounding ground.
            #
            # Per-vertex law value: sample the DEM at the vertex (the
            # smoothed tile DEM in production — the same source the crown
            # samples for ``ground_behind``, consistent with any
            # adjacent_ground band that abuts) and blend from the crown
            # elevation to that ground by DISTANCE TO THE CROWN polygon
            # (the buried half).  Vertices on the buried inner edge hug
            # the crown (distance ~ 0); vertices on the outer rim, and
            # everything laterally beyond the object where no facade
            # hides a face, reach the ground within one collar reach —
            # so no exposed vertical face survives.
            crown_for_collar = crown_geometry
            crown_reach = collar_reach if collar_reach > 1e-6 else 1.0
            # Round-8 fact 4 — a lateral flank BEYOND the object's own
            # width (the facade-hidden span) may not meet the low mouth
            # plate vertically.  Only the object-hidden span — within the
            # mouth face's lateral half-width — is allowed the by-design
            # vertical meeting (the facade covers it); a flank sticking
            # out sideways past the object must feather down to the mouth
            # floor where it abuts the mouth plate, exactly as the outer
            # rim feathers to the surrounding ground.  Measure the mouth
            # face half-width (footprint extent perpendicular to
            # ``outward``).
            face_centroid = footprint.centroid
            face_perpendicular = (-outward_vector[1], outward_vector[0])
            _face_lateral = [
                ((vertex_x - face_centroid.x) * face_perpendicular[0]
                 + (vertex_y - face_centroid.y) * face_perpendicular[1])
                for vertex_x, vertex_y in footprint.exterior.coords
            ]
            face_half_width = (
                (max(_face_lateral) - min(_face_lateral)) / 2.0
                if _face_lateral else 0.0)
            # Buried depth of the crown along the outward axis — bounds
            # the mouth-side VALUE-ceiling zone below, so on a shallow
            # portal the object-hidden back-face meeting stays at the
            # crown (the ceiling zone never reaches it).
            crown_depth_m = 0.0
            try:
                # The pavement clip can leave a MultiPolygon crown —
                # measure the depth across every part's ring.
                _crown_projections = [
                    (vertex_x * outward_vector[0]
                     + vertex_y * outward_vector[1])
                    for part in getattr(
                        crown_geometry, "geoms", [crown_geometry])
                    for vertex_x, vertex_y in part.exterior.coords]
                crown_depth_m = (max(_crown_projections)
                                 - min(_crown_projections))
            except (_GEOM_EXC, AttributeError, ValueError):
                crown_depth_m = 0.0
            # Complete the portal terrain record for the post-solve
            # airside raise (its mouth feather guard reuses the same
            # mouth plate geometry so the raise never rebuilds the
            # road-side cliff the feather removed).
            portal_terrain_record["mouth_geometry"] = plain_mouth_geometry
            portal_terrain_record["crown_depth_m"] = crown_depth_m

            def _collar_alt(vertex_x, vertex_y,
                            _crown=crown_for_collar,
                            _crown_elevation=crown_elevation,
                            _mouth_floor=mouth_floor,
                            _ground_behind=ground_behind,
                            _reach=crown_reach,
                            _mouth_geometry=plain_mouth_geometry,
                            _crown_depth=crown_depth_m):
                # The outer target is the ground the abutting
                # adjacent_ground band will drop to — NOT the smoothed
                # DEM AT the vertex.  Ortho4XP's airport-smoothed tile
                # DEM FLATTENS the runway embankment, so it holds the
                # crown height right up to the collar's outer edge and
                # only drops at the LIP just beyond; sampling at the
                # vertex therefore reads ~crown and the collar would
                # stay a plateau (measured KBNA 02C: the band welded to
                # a crown-high collar pin, then cliffed 6.9-7.8 m to the
                # DEM < 1 m further out).  Sample the DEM over a small
                # DISK around the vertex and take the MINIMUM, so the
                # exposed rim reaches the true surrounding ground the
                # band's first non-weld station reads — in whatever
                # direction the lip falls (a single outward ray misses
                # it: the drop is perpendicular to the collar arm, not
                # radial from the footprint centroid).  The
                # distance-to-crown blend below keeps the buried INNER
                # face at the crown regardless, so lowering the disk
                # target never disturbs the object-hidden meeting.
                # A tight disk: the embankment lip abuts the collar's
                # outer edge (measured KBNA: the DEM drop is < 1.5 m
                # outside the rim), so a few metres in every direction
                # catch it — while staying close enough that the collar
                # never dives BELOW the ground the band welds to just
                # outside it (a distant low would invert the step).
                ground_here = None
                offsets = [(0.0, 0.0)]
                for radius in (1.5, 3.0, 5.0):
                    for step_index in range(12):
                        angle = math.pi * step_index / 6.0
                        offsets.append((radius * math.cos(angle),
                                        radius * math.sin(angle)))
                for offset_x, offset_y in offsets:
                    try:
                        probe_lat, probe_lon = _meters_to_lat_lon(
                            vertex_x + offset_x, vertex_y + offset_y)
                        sample = _sample_dem(
                            dem, tile_lat, tile_lon, probe_lat, probe_lon)
                    except (_GEOM_EXC, ValueError, TypeError):
                        sample = None
                    if sample is not None:
                        ground_here = (
                            sample if ground_here is None
                            else min(ground_here, float(sample)))
                if ground_here is None:
                    ground_here = (
                        _ground_behind if _ground_behind is not None
                        else _crown_elevation)
                # Never above the crown, never an absurd DEM dropout
                # (mirror the crown's own clamp band).
                outer_target = min(
                    max(float(ground_here), _mouth_floor - 12.0),
                    _crown_elevation,
                )
                # Mouth transition — DISTANCE-BASED (2026-07-17,
                # supersedes the round-8 lateral gate): ANY collar
                # vertex within one reach of the low mouth plate
                # feathers its outer target down to the mouth floor,
                # UNLESS it hugs the crown's hidden face (distance to
                # the crown polygon < 2 m — the object facade hides
                # that by-design vertical meeting, and on a small
                # portal the whole back band is within a reach of the
                # mouth).  The old gate exempted the entire lateral
                # facade span, which left the collar's side wrap at
                # full crown height 0-2 m from the mouth plate — with
                # the crown now floored at the object roof plane
                # (mouth + deck_top) those seams measured 3.4-6.0 m
                # steps (audit check 7, 33 vertices at both
                # Murfreesboro portals).
                try:
                    distance_to_crown = _crown.distance(
                        Point(vertex_x, vertex_y))
                except (_GEOM_EXC, AttributeError):
                    distance_to_crown = _reach
                try:
                    distance_to_mouth = _mouth_geometry.distance(
                        Point(vertex_x, vertex_y))
                except (_GEOM_EXC, AttributeError):
                    distance_to_mouth = _reach
                if distance_to_crown >= 2.0 and distance_to_mouth < _reach:
                    mouth_blend = max(
                        0.0, min(1.0, distance_to_mouth / _reach))
                    outer_target = min(
                        outer_target,
                        (1.0 - mouth_blend) * _mouth_floor
                        + mouth_blend * outer_target)
                blend = max(0.0, min(1.0, distance_to_crown / _reach))
                value = ((1.0 - blend) * _crown_elevation
                         + blend * outer_target)
                # SPLIT-LINE VALUE CEILING (2026-07-17): at the mouth
                # plate's sides the collar hugs the CROWN's side edge
                # (distance_to_crown ~ 0), so the crown term dominates
                # the blend regardless of the outer-target feather and
                # the collar met the low mouth plate at ~70 % of the
                # crown height (audit: 3.2/4.7 m steps at both
                # Murfreesboro portals).  Cap the VALUE itself: within
                # the mouth zone a collar vertex may exceed the mouth
                # floor by at most (distance_to_mouth / reach) x
                # (crown - mouth) — zero step at the plate, full crown
                # one reach away.  The zone is bounded below the
                # crown's buried depth so a shallow portal's hidden
                # back-face meeting (which IS within a reach of the
                # mouth polygon) keeps the crown.
                ceiling_zone = min(_reach, max(0.0, _crown_depth - 1.0))
                if distance_to_mouth < ceiling_zone:
                    ceiling = (_mouth_floor
                               + (distance_to_mouth / _reach)
                               * max(0.0, _crown_elevation - _mouth_floor))
                    value = min(value, ceiling)
                return value

            collar_vertex_count = 0
            emitted_collar_parts = 0
            for collar_part in collar_parts:
                try:
                    part_vertex_count = _born_graded(
                        collar_part, portal_plate_role,
                        "object_tunnel_portal_collar", _collar_alt,
                        record_pins=portal_plate_pins)
                except _GEOM_EXC:
                    continue
                if part_vertex_count > 0:
                    # ``_born_graded`` appended the shape (a < 4-vertex
                    # ring returns 0 without appending).
                    portal_terrain_record["collar_shapes"].append(
                        layout.shapes[-1])
                collar_vertex_count += part_vertex_count
                emitted_collar_parts += 1
            if not emitted_collar_parts:
                continue
            n_trench += 1
            UI.vprint(
                1,
                "   [object-tunnel] portal collar feathers crown "
                f"{crown_elevation:.2f} m to ground around the portal "
                f"back ({emitted_collar_parts} part(s), "
                f"{collar_vertex_count} vertices)",
            )

    for bridge in corridor_bridges:
        datum = _bridge_datum_elevation_m(bridge, dem, tile_lat, tile_lon)
        if datum is None:
            UI.vprint(
                1,
                "   [object-bridge] no datum for bridge layout shapes of "
                f"{bridge.object_resources} — skipped",
            )
            continue
        footprint = _bridge_footprint_meters(bridge, to_meters)
        if footprint is None:
            continue
        # The under-deck working area is the abutment-to-abutment BOX
        # (iteration 5) — the hard-face union can be partial/multi-lobe.
        deck_box = _bridge_deck_box_meters(bridge, layout)
        if deck_box is None:
            deck_box = footprint
        deck_elevation = _bridge_deck_elevation_m(
            bridge, dem, tile_lat, tile_lon
        )
        floor_elevation = _bridge_corridor_floor_m(bridge, deck_elevation)
        # Round 10: the road-exit corridor — where a draped road leaves
        # the span through an abutment end, the causeway must yield.
        # User ruling 2026-07-14c: the exit opening matches the TRENCH
        # width (the approach ramps now run at trench width), so the
        # carriageway-width corridor is dilated up to the trench's
        # cross-section before it cuts the causeway plates.
        road_exit_corridor = _road_exit_corridor_meters(
            bridge, layout, to_meters
        )
        if road_exit_corridor is not None:
            try:
                rotated_rectangle = min_rotated_rect(deck_box)
                corners = list(rotated_rectangle.exterior.coords)
                short_side = min(
                    math.hypot(corners[1][0] - corners[0][0],
                               corners[1][1] - corners[0][1]),
                    math.hypot(corners[2][0] - corners[1][0],
                               corners[2][1] - corners[1][1]),
                )
                trench_width_m = short_side - 2.0 * _TRENCH_INSET_M
                dilation = (trench_width_m / 2.0
                            - _ROAD_EXIT_CUT_HALF_WIDTH_M)
                if dilation > 0.0:
                    road_exit_corridor = road_exit_corridor.buffer(
                        dilation)
            except _GEOM_EXC:
                pass

        # Ruling R8 flush seat (hard decks) / pavement wins (cosmetic).
        pavement_kept_union = None
        if bridge.hard_deck:
            n_cut = _cut_pavement_over_hard_deck(layout, deck_box)
            if n_cut:
                UI.vprint(
                    1,
                    f"   [object-bridge] R8 flush seat: cut {n_cut} "
                    "pavement shape(s) over the hard deck of "
                    f"{bridge.object_resources}",
                )
        else:
            crossing_polygons = [
                shape.polygon for shape in layout.shapes
                if shape.role in weld_roles
                and shape.polygon is not None
                and not shape.polygon.is_empty
                and shape.polygon.intersects(deck_box)
            ]
            try:
                pavement_kept_union = (
                    unary_union(crossing_polygons)
                    if crossing_polygons else None
                )
            except _GEOM_EXC:
                pavement_kept_union = None

        # Trench (born flat at the law floor) — spans the deck BOX.
        trench_polygon_emitted = None
        try:
            trench = deck_box.buffer(-_TRENCH_INSET_M)
            if pavement_kept_union is not None:
                trench = trench.difference(pavement_kept_union)
            if trench.geom_type == "MultiPolygon" and not trench.is_empty:
                trench = max(trench.geoms, key=lambda g: g.area)
            if trench.geom_type == "Polygon" and not trench.is_empty:
                vertex_count = _born_flat(
                    trench, ROLE_BRIDGE_TRENCH,
                    "object_bridge_corridor", floor_elevation)
                trench_polygon_emitted = trench
                n_trench += 1
                UI.vprint(
                    1,
                    "   [object-bridge] trench born at "
                    f"{floor_elevation:.2f} m ({vertex_count} vertices) "
                    f"under {bridge.object_resources}",
                )
        except _GEOM_EXC:
            pass

        # Causeway plates (born flat at the deck-end law value).
        pavement_union = _pavement_union()
        centroid = footprint.centroid
        causeway_parts_emitted: list = []
        abutment_lines = _abutment_lines_layout_meters(
            bridge, layout, extension_fraction=0.0
        )
        for end_index, line in enumerate(abutment_lines):
            end_y = (
                bridge.deck_end_elevations_y_m[end_index]
                if end_index < len(bridge.deck_end_elevations_y_m)
                else bridge.deck_top_y_m
            )
            plate_elevation = bridge_deck_end_pin_elevation_m(datum, end_y)
            midpoint = line.interpolate(0.5, normalized=True)
            outward_x = midpoint.x - centroid.x
            outward_y = midpoint.y - centroid.y
            outward_norm = math.hypot(outward_x, outward_y)
            if outward_norm < 1.0:
                continue
            outward_x /= outward_norm
            outward_y /= outward_norm
            (ax, ay), (bx, by) = list(line.coords)[0], list(line.coords)[-1]
            # Iteration 5 (audit 5): widen the lip beyond the deck
            # corners and start the plate INWARD of the lip so on-line
            # and corner samples stay inside the 167-flat surface under
            # the 0.5 m node-interning wobble; the trench inset grows in
            # step so the R2 node-split wall gap stays 0.6 m.
            line_length = math.hypot(bx - ax, by - ay)
            direction_x = (bx - ax) / line_length
            direction_y = (by - ay) / line_length
            ax -= direction_x * _CAUSEWAY_WIDTH_MARGIN_M
            ay -= direction_y * _CAUSEWAY_WIDTH_MARGIN_M
            bx += direction_x * _CAUSEWAY_WIDTH_MARGIN_M
            by += direction_y * _CAUSEWAY_WIDTH_MARGIN_M
            ax -= outward_x * _CAUSEWAY_INWARD_OVERLAP_M
            ay -= outward_y * _CAUSEWAY_INWARD_OVERLAP_M
            bx -= outward_x * _CAUSEWAY_INWARD_OVERLAP_M
            by -= outward_y * _CAUSEWAY_INWARD_OVERLAP_M

            def _outward_rectangle(length_m):
                reach = length_m + _CAUSEWAY_INWARD_OVERLAP_M
                return Polygon([
                    (ax, ay),
                    (bx, by),
                    (bx + outward_x * reach, by + outward_y * reach),
                    (ax + outward_x * reach, ay + outward_y * reach),
                ])

            plate_length = maximum_length
            if pavement_union is not None:
                try:
                    outward_pavement = pavement_union.intersection(
                        _outward_rectangle(maximum_length)
                    )
                    if not outward_pavement.is_empty:
                        gap = line.distance(outward_pavement)
                        plate_length = min(gap + 2.0, maximum_length)
                except _GEOM_EXC:
                    pass
            if plate_length < 1.0:
                plate_length = 2.0
            try:
                plate = _outward_rectangle(plate_length)
                if not plate.is_valid:
                    plate = plate.buffer(0)
                if pavement_union is not None:
                    plate = plate.difference(pavement_union)
                # Round 10 road-exit cut: the road corridor passes
                # THROUGH this end — cut it out of the plate; the
                # remainders flank the road at the deck-end elevation
                # (the author-mesh shape: 161 corridor out both ends,
                # 167 fill on both sides).
                if road_exit_corridor is not None:
                    plate = plate.difference(road_exit_corridor)
                parts = (
                    list(plate.geoms)
                    if plate.geom_type == "MultiPolygon" else [plate]
                )
                emitted_parts = 0
                for part in parts:
                    if (part.geom_type != "Polygon" or part.is_empty
                            or part.area < 25.0):
                        continue
                    _born_flat(part, ROLE_BRIDGE_CAUSEWAY,
                               "object_bridge_causeway", plate_elevation)
                    causeway_parts_emitted.append(part)
                    emitted_parts += 1
                # User ruling 2026-07-14b: the pavement the approach
                # RESUMES on around the plate zone is anchored AT the
                # deck height, so both sides of the crossing grade
                # smoothly to the same value.  The abutment-line pins
                # alone miss it — the resumed pavement across the
                # road-exit cut measures 13.3-13.6 m from the plate at
                # KBNA (its approach solved 6.3 m above the 167.0
                # deck, a quarantined wall at the lip).  Pinned around
                # the FULL outward plate rectangle, not the surviving
                # flank parts — with trench-width exit cuts (ruling
                # 2026-07-14c) the flanks can vanish entirely while the
                # anchoring must stay.  Taxi/runway/apron rings only —
                # service roads descend through the road-exit cut and
                # must never pin to the deck.
                weld_pinned = 0
                weld_band = float(
                    _CFG.BRIDGE_CAUSEWAY_WELD_PIN_BAND_M)
                try:
                    weld_line = LineString(list(
                        _outward_rectangle(plate_length)
                        .exterior.coords))
                except _GEOM_EXC:
                    weld_line = None
                if weld_line is not None:
                    for shape_index, shape in enumerate(
                            list(layout.shapes)):
                        if shape.role not in _BRIDGE_PIN_ROLES:
                            continue
                        if shape.polygon is None \
                                or shape.polygon.is_empty:
                            continue
                        try:
                            if shape.polygon.exterior.distance(
                                    weld_line) > weld_band:
                                continue
                        except _GEOM_EXC:
                            continue
                        weld_pinned += _pin_shape_vertices_on_line(
                            layout, shape_index, weld_line,
                            plate_elevation,
                            capture_band_m=weld_band,
                        )
                if weld_pinned:
                    UI.vprint(
                        1,
                        f"   [object-bridge] {weld_pinned} approach "
                        "weld pin(s) around the causeway zone "
                        f"({plate_elevation:.2f} m, end {end_index})",
                    )
                if not emitted_parts:
                    continue
                n_causeway += emitted_parts
                UI.vprint(
                    1,
                    "   [object-bridge] causeway born at "
                    f"{plate_elevation:.2f} m, {plate_length:.1f} m long, "
                    f"{emitted_parts} flank part(s)"
                    + (" (road-exit cut)" if road_exit_corridor is not None
                       and emitted_parts > 1 else "")
                    + f" (end {end_index}) for {bridge.object_resources}",
                )
            except _GEOM_EXC:
                continue

        # ── Deck-lip weld strips (user directive 2026-07-15): the R8
        # hard-deck cut trims pavement AT the deck box while the trench
        # is inset _TRENCH_INSET_M, leaving a ring of raw mesh whose
        # triangles dive to the trench floor right at the lip (measured
        # KBNA taxiway-L: pavement pinned 167.0, plate edge 0.8-1.2 m
        # away at 161.01 — the owner-visible gap onto the deck).  Every
        # pavement-facing rim segment gets a strip at the deck-top
        # PROFILE law value spanning from the causeway-inward depth
        # (preserving the R2 node-split wall to the trench) to
        # _DECK_WELD_OVERLAP_M INSIDE the pavement; the fronting
        # pavement ring vertices are pinned at the same law value, so
        # the overlap is coplanar and invisible.  Never on road faces:
        # the strip zone is pavement-driven (taxi/runway/apron roles
        # only — service roads descend through the road-exit cut) and
        # the road-exit corridor is cut out, so the approach chains and
        # their verbatim plate welds are untouched.
        n_weld_strips = _emit_deck_lip_weld_strips(
            layout, bridge, deck_box, trench_polygon_emitted,
            road_exit_corridor, causeway_parts_emitted, datum,
            _born_graded)
        if n_weld_strips:
            UI.vprint(
                1,
                f"   [object-bridge] {n_weld_strips} deck-lip weld "
                "strip(s) overlap the resumed pavement at the deck "
                f"profile value for {bridge.object_resources}",
            )
    return n_trench, n_causeway, pads_removed


def raise_portal_terrain_to_airside(layout) -> int:
    """POST-SOLVE raise of portal crown/collar terrain to the
    surrounding solved AIRSIDE level (user ruling 2026-07-17: the
    portal object's flat top sits close to level with the adjacent
    taxiway — the back terrain must RISE to meet the airside where the
    solved airside stands above the object-derived crown; it never
    falls, so the DEM feather survives wherever airside is lower or
    absent).

    Called from ``finalize.emit_terrain_transition_features`` — after
    the elevation solve (solved airside ring values exist), before
    ``final_grade_projection`` and adjacent-ground band emission.
    Every raised vertex re-records its solver pin (``_record_pin``)
    so the downstream scoped projection re-pins the RAISED value
    (post-writeback passes must re-pin every hard class).

    Returns the number of vertices raised.
    """
    if not getattr(_CFG, "TUNNEL_PORTAL_AIRSIDE_RAISE", True):
        return 0
    records = getattr(layout, "portal_terrain_records", None)
    if not records:
        return 0
    from .layout import corner_alts_from_high_low

    radius = float(_CFG.TUNNEL_PORTAL_AIRSIDE_SAMPLE_RADIUS_M)
    airside_roles = (
        ROLE_RUNWAY, ROLE_JUNCTION, ROLE_APRON, ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB, ROLE_CROSS_CONNECTOR)
    # Solved airside samples in a coarse grid (cell = radius, so a
    # query only visits the 3x3 neighbourhood).
    grid: dict = {}
    for s in layout.shapes:
        if (s.role not in airside_roles or s.polygon is None
                or s.polygon.is_empty):
            continue
        try:
            ring = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        node_values = getattr(s, "node_altitudes", None)
        if node_values and len(node_values) >= len(ring) - 1:
            values = list(node_values)
        elif getattr(s, "altitude", None) is not None:
            values = [float(s.altitude)] * len(ring)
        elif (getattr(s, "altitude_high", None) is not None
                and getattr(s, "altitude_low", None) is not None
                and len(ring) in (4, 5)):
            corner_values = corner_alts_from_high_low(
                s.altitude_high, s.altitude_low)
            values = corner_values + corner_values[:1]
        else:
            continue
        for (x, y), value in zip(ring, values):
            if value is None:
                continue
            key = (int(x // radius), int(y // radius))
            grid.setdefault(key, []).append(
                (float(x), float(y), float(value)))
    if not grid:
        return 0

    def airside_samples(x, y):
        """``(median, nearest_distance)`` of solved airside ring values
        within ``radius`` of the point, or ``(None, None)``."""
        key_x, key_y = int(x // radius), int(y // radius)
        found = []
        nearest_sq = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (sx, sy, sv) in grid.get(
                        (key_x + dx, key_y + dy), ()):
                    d_sq = (sx - x) ** 2 + (sy - y) ** 2
                    if d_sq <= radius * radius:
                        found.append(sv)
                        if nearest_sq is None or d_sq < nearest_sq:
                            nearest_sq = d_sq
        if not found:
            return (None, None)
        found.sort()
        mid = len(found) // 2
        median = (found[mid] if len(found) % 2
                  else 0.5 * (found[mid - 1] + found[mid]))
        return (median, math.sqrt(nearest_sq))

    def airside_level(x, y):
        return airside_samples(x, y)[0]

    # RAMP-LAW CEILING (2026-07-17, preventive): raised collar/crown
    # values CAN propagate into coincident tunnel-ramp quad corners at
    # the to_osm per-node consensus (which merges shared coordinates
    # toward the feature side), steepening an at-cap ramp past
    # TUNNEL_RAMP_MAX_GRADE.  (The SPJC 4.13-4.21 % ramp violations
    # that prompted this were measured to be LATENT in the legacy ramp
    # emitter — present with the raise AND crown gated off — not
    # raise-induced; the guard stays because the mechanism is real by
    # construction.)  The raise yields to the ramp law:
    # a raised value at a coordinate shared with a sloped ramp quad may
    # not exceed the quad's far-end value + cap x quad length.  Index
    # every sloped tunnel-ramp quad corner once per call.
    from .layout import corner_alts_from_high_low as _corner_alts
    ramp_cap = float(_CFG.TUNNEL_RAMP_MAX_GRADE)
    ramp_corner_ceilings: dict = {}
    for s in layout.shapes:
        if (s.role != ROLE_TUNNEL_RAMP or s.polygon is None
                or s.polygon.is_empty
                or s.altitude_high is None or s.altitude_low is None):
            continue
        try:
            ring = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        open_ring = ring[:-1] if ring and ring[0] == ring[-1] else ring
        if len(open_ring) != 4:
            continue
        corner_values = _corner_alts(s.altitude_high, s.altitude_low)
        # [H, L, L, H]: corners (0, 3) = high short edge, (1, 2) = low.
        high_mid = (0.5 * (open_ring[0][0] + open_ring[3][0]),
                    0.5 * (open_ring[0][1] + open_ring[3][1]))
        low_mid = (0.5 * (open_ring[1][0] + open_ring[2][0]),
                   0.5 * (open_ring[1][1] + open_ring[2][1]))
        quad_length = math.hypot(high_mid[0] - low_mid[0],
                                 high_mid[1] - low_mid[1])
        if quad_length < 1.0:
            continue
        for corner_index, (cx, cy) in enumerate(open_ring):
            far_value = (float(s.altitude_high)
                         if corner_index in (1, 2)
                         else float(s.altitude_low))
            ceiling = far_value + ramp_cap * quad_length
            key = (int(cx // SHARED_VERTEX_TOL_M),
                   int(cy // SHARED_VERTEX_TOL_M))
            ramp_corner_ceilings.setdefault(key, []).append(
                (cx, cy, ceiling))

    def ramp_law_ceiling(x, y):
        """Tightest ramp-law ceiling among ramp corners within the
        to_osm interning tolerance of the point, or None."""
        key_x = int(x // SHARED_VERTEX_TOL_M)
        key_y = int(y // SHARED_VERTEX_TOL_M)
        tightest = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (cx, cy, ceiling) in ramp_corner_ceilings.get(
                        (key_x + dx, key_y + dy), ()):
                    if (math.hypot(cx - x, cy - y)
                            <= SHARED_VERTEX_TOL_M
                            and (tightest is None
                                 or ceiling < tightest)):
                        tightest = ceiling
        return tightest

    n_raised = 0
    for record in records:
        crown_geometry = record.get("crown_geometry")
        crown_elevation = float(record.get("crown_elevation") or 0.0)
        reach = float(record.get("collar_reach") or 1.0)
        if reach <= 1e-6:
            reach = 1.0
        raised_crown = crown_elevation
        if crown_geometry is not None:
            try:
                level = airside_level(
                    crown_geometry.centroid.x, crown_geometry.centroid.y)
            except _GEOM_EXC:
                level = None
            if level is not None and level > crown_elevation + 0.05:
                raised_crown = float(level)
        crown_shape_list = [
            shape for shape in (
                record.get("crown_shapes") or [record.get("crown_shape")])
            if shape is not None and shape.polygon is not None
            and getattr(shape, "node_altitudes", None)]
        if raised_crown > crown_elevation + 0.05 and crown_shape_list:
            crown_rings = []
            for crown_shape in crown_shape_list:
                try:
                    crown_rings.append(
                        list(crown_shape.polygon.exterior.coords))
                except _GEOM_EXC:
                    crown_rings.append([])
            # A flat crown clamps as a whole to the tightest ramp-law
            # ceiling among its ramp-coincident ring vertices (rare —
            # ramps attach at the mouth side, the crown is the back
            # half).  All parts share one raised value.
            for crown_ring in crown_rings:
                for (x, y) in crown_ring:
                    ceiling = ramp_law_ceiling(x, y)
                    if ceiling is not None and raised_crown > ceiling:
                        raised_crown = max(crown_elevation, ceiling)
            raised_count = 0
            for crown_shape, crown_ring in zip(
                    crown_shape_list, crown_rings):
                crown_shape.node_altitudes = (
                    [round(raised_crown, 2)]
                    * len(crown_shape.node_altitudes))
                open_count = (
                    len(crown_ring) - 1
                    if crown_ring and crown_ring[0] == crown_ring[-1]
                    else len(crown_ring))
                for (x, y) in crown_ring[:open_count]:
                    _record_pin(layout, x, y, raised_crown)
                raised_count += open_count
            n_raised += raised_count
            UI.vprint(1,
                "   [object-tunnel] portal crown raised to airside "
                f"level {raised_crown:.2f} m (was "
                f"{crown_elevation:.2f} m)")

        mouth_geometry = record.get("mouth_geometry")
        mouth_floor = float(record.get("mouth_floor") or 0.0)
        crown_depth = float(record.get("crown_depth_m") or 0.0)
        ceiling_zone = min(reach, max(0.0, crown_depth - 1.0))
        for collar_shape in record.get("collar_shapes") or ():
            node_values = getattr(collar_shape, "node_altitudes", None)
            if (collar_shape.polygon is None or not node_values):
                continue
            try:
                ring = list(collar_shape.polygon.exterior.coords)
            except _GEOM_EXC:
                continue
            if len(node_values) < len(ring):
                continue
            changed = False
            new_values = list(node_values)
            open_count = (len(ring) - 1
                          if ring and ring[0] == ring[-1] else len(ring))
            for i in range(open_count):
                x, y = ring[i]
                old_value = float(node_values[i])
                try:
                    distance_to_crown = (
                        crown_geometry.distance(Point(x, y))
                        if crown_geometry is not None else reach)
                except (_GEOM_EXC, AttributeError):
                    distance_to_crown = reach
                # Mouth feather guard — DISTANCE-BASED, mirroring
                # ``_collar_alt``: any vertex within one reach of the
                # low mouth plate keeps its feathered value (raising
                # it would rebuild the cliff onto the road), unless it
                # hugs the crown's hidden face (the object facade
                # hides that meeting by design).
                distance_to_mouth = reach
                if mouth_geometry is not None:
                    try:
                        distance_to_mouth = mouth_geometry.distance(
                            Point(x, y))
                    except (_GEOM_EXC, AttributeError):
                        distance_to_mouth = reach
                if distance_to_crown >= 2.0 and distance_to_mouth < reach:
                    continue
                blend = max(0.0, min(1.0, distance_to_crown / reach))
                # DISTANCE-DECAYED rim target: the rim rises toward
                # the airside median only in proportion to how close
                # the nearest airside vertex actually is — a rim
                # vertex 5 m from the abutting band takes ~the band
                # level, one 70 m away keeps ~its DEM feather.  A flat
                # airside-median target regardless of distance
                # flattened the whole collar into a plateau wherever
                # ANY airside vertex sat within the sample radius
                # (measured in the raise unit probe).
                outer_level, nearest_airside_m = airside_samples(x, y)
                if outer_level is None:
                    outer_target = old_value
                else:
                    weight = max(0.0, min(
                        1.0, 1.0 - float(nearest_airside_m) / radius))
                    outer_target = (weight * float(outer_level)
                                    + (1.0 - weight) * old_value)
                candidate = ((1.0 - blend) * raised_crown
                             + blend * outer_target)
                # Split-line value ceiling — mirrors ``_collar_alt``:
                # within the mouth zone the raised value may exceed the
                # mouth floor by at most the per-reach fraction of the
                # (raised) crown height.
                if distance_to_mouth < ceiling_zone:
                    candidate = min(
                        candidate,
                        mouth_floor + (distance_to_mouth / reach)
                        * max(0.0, raised_crown - mouth_floor))
                # Ramp-law ceiling: the raise yields to an abutting
                # sloped tunnel-ramp quad's grade cap (see the index
                # above).
                ramp_ceiling = ramp_law_ceiling(x, y)
                if ramp_ceiling is not None:
                    candidate = min(candidate, ramp_ceiling)
                if candidate > old_value + 0.05:
                    new_values[i] = round(candidate, 2)
                    _record_pin(layout, x, y, float(new_values[i]))
                    changed = True
                    n_raised += 1
            if changed:
                if len(ring) > open_count:
                    new_values[len(ring) - 1] = new_values[0]
                collar_shape.node_altitudes = new_values
    if n_raised:
        UI.vprint(1,
            f"   [object-tunnel] portal terrain airside raise: "
            f"{n_raised} vertices lifted across "
            f"{len(records)} portal(s).")
    return n_raised


def enforce_bridge_plate_exclusivity(layout) -> int:
    """Round 9 (user ruling): the WRITTEN patch must contain strictly
    non-overlapping rings over the bridge plates — the base mesh
    machinery is never taught to tolerate our overlaps.  Every terrain
    FEATURE shape (tunnel ramps/walls from the legacy portal emitters
    that fire on the ``tunnel=yes`` OSM ways under a deck, clearance
    cuts, adjacent-ground strips) is CUT against the trench/causeway
    plate footprints; pieces mostly inside a plate are dropped whole
    (measured at KBNA: 14 portal ramp/wall vertices constrained at
    170.0-178.3 inside the 161.01 trench box kept the corridor off its
    floor — Triangle interpolated the interior from them).

    The plates themselves are already mutually exclusive by
    construction (trench inset vs causeway lip, pavement weld clips).
    Deconflict's sloped-quad exemption (small overlaps kept to preserve
    the 4-corner altitude convention) does NOT apply here: a sloped
    piece that loses corners is converted to resampled per-vertex
    ``node_altitudes``, exactly like the boundary-cut pattern.

    Called after the road-feature emitters (finalize) AND after the
    adjacent-ground bands (pipeline tail).  Gate off ⇒ no plates ⇒ 0.
    Returns the number of shapes cut or dropped."""
    classification = _object_bridge_classification(layout)
    if classification is None:
        return 0
    from .layout import (
        ROLE_BRIDGE_CAUSEWAY,
        ROLE_BRIDGE_TRENCH,
        ROLE_GRADED_STRIP,
        ROLE_RETAINING_WALL,
        ROLE_TAXIWAY_CLEARANCE,
        ROLE_RUNWAY_CLEARANCE,
    )
    plate_roles = (ROLE_BRIDGE_TRENCH, ROLE_BRIDGE_CAUSEWAY)
    cut_roles = {
        ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL, ROLE_GRADED_STRIP,
        ROLE_TAXIWAY_CLEARANCE, ROLE_RUNWAY_CLEARANCE,
    }
    plate_polygons = [
        shape.polygon for shape in layout.shapes
        if shape.role in plate_roles
        and shape.polygon is not None and not shape.polygon.is_empty
    ]
    if not plate_polygons:
        return 0
    try:
        plate_union = unary_union(plate_polygons)
    except _GEOM_EXC:
        return 0
    n_touched = 0
    kept_shapes: list[BuiltShape] = []
    for shape in layout.shapes:
        if (shape.role not in cut_roles
                or shape.polygon is None or shape.polygon.is_empty):
            kept_shapes.append(shape)
            continue
        # Our own plates never cut each other here.
        if (shape.ref or "").startswith("object_bridge"):
            kept_shapes.append(shape)
            continue
        try:
            overlap = shape.polygon.intersection(plate_union).area
        except _GEOM_EXC:
            kept_shapes.append(shape)
            continue
        if overlap < 0.5:
            kept_shapes.append(shape)
            continue
        n_touched += 1
        try:
            old_ring = list(shape.polygon.exterior.coords)
        except _GEOM_EXC:
            old_ring = []
        if old_ring and old_ring[0] == old_ring[-1]:
            old_ring = old_ring[:-1]
        old_altitudes = (
            list(shape.node_altitudes) if shape.node_altitudes else None
        )
        try:
            remainder = shape.polygon.difference(plate_union)
        except _GEOM_EXC:
            kept_shapes.append(shape)
            continue
        if remainder.is_empty:
            UI.vprint(
                2,
                "   [object-bridge] dropped "
                f"{shape.role}/{shape.ref!r} inside a bridge plate "
                "(non-overlap rule)",
            )
            continue
        parts = (
            list(remainder.geoms)
            if remainder.geom_type == "MultiPolygon" else [remainder]
        )
        from .elevation import _resample_node_altitudes_nn as _resample
        for part in parts:
            if (part.geom_type != "Polygon" or part.is_empty
                    or part.area < 1.0):
                continue
            resampled = _resample(part, old_ring, old_altitudes)
            kept_shapes.append(BuiltShape(
                polygon=part,
                role=shape.role,
                ref=shape.ref,
                altitude=(shape.altitude if resampled is None else None),
                altitude_high=(
                    None if resampled is not None else shape.altitude_high),
                altitude_low=(
                    None if resampled is not None else shape.altitude_low),
                node_altitudes=resampled))
    if n_touched:
        layout.shapes = kept_shapes
        UI.vprint(
            1,
            f"   [object-bridge] non-overlap rule: cut/dropped "
            f"{n_touched} feature shape(s) against the bridge plates",
        )
    return n_touched


def _bridge_crossing_floor_for_bridge(
        bridge, road_networks, dem, tile_lat, tile_lon,
        to_meters, meters_to_lat_lon):
    """The crossing-floor decision for ONE bridge record: ``(floor_value,
    footprint_meters)`` when the span must rise over an un-lowered draped
    road, else ``None``.  Shared verbatim by the solve-side producer
    (:func:`bridge_crossing_floor_nodes`) and the validator
    (``verification.check_bridge_crossing_floor``) so their guards, road
    sampling and law evaluation can never drift (lockstep beyond the law
    function itself).

    Guards: TERRAIN/PROFILE_CARRIED contracts only; flush decks
    (crest below ``config.BRIDGE_ROAD_CLEARANCE_M``) encode "the pack
    handles the road" and get restraint, never a floor (ruling R9); a
    fully-draped DSF road segment must cross the footprint.  Road
    surface = median DEM sample of the draped polyline inside the
    footprint (the road-untouched case; pack-trench and solved-corridor
    sources land with the W-V audits)."""
    from .grade_law import bridge_crossing_floor_m
    from .object_terrain_features import TERRAIN_CARRIED, PROFILE_CARRIED
    if bridge.contract not in (TERRAIN_CARRIED, PROFILE_CARRIED):
        return None
    if float(bridge.deck_top_y_m) < float(_CFG.BRIDGE_ROAD_CLEARANCE_M):
        return None  # flush deck: pack-handled road, restraint only
    footprint = _bridge_footprint_meters(bridge, to_meters)
    if footprint is None:
        return None
    road_lines = _draped_road_centerlines_meters(
        bridge, road_networks, to_meters
    )
    if not road_lines:
        return None
    road_samples: list[float] = []
    for line in road_lines:
        for x, y in line.coords:
            try:
                if not footprint.contains(Point(x, y)):
                    continue
                latitude, longitude = meters_to_lat_lon(x, y)
                sample = _sample_dem(
                    dem, tile_lat, tile_lon, latitude, longitude
                )
            except _GEOM_EXC:
                continue
            if sample is not None and sample == sample:
                road_samples.append(float(sample))
    if not road_samples:
        return None
    road_samples.sort()
    road_surface = road_samples[len(road_samples) // 2]
    underside = bridge.clearance_underside_y_m
    if underside is None:
        underside = bridge.ceiling_y_m
    structure_thickness = (
        max(0.0, float(bridge.deck_top_y_m) - float(underside))
        if underside is not None else 0.0
    )
    return bridge_crossing_floor_m(road_surface, structure_thickness), \
        footprint


def bridge_crossing_floor_nodes(layout, nodes, dem, tile_lat, tile_lon):
    """Feature B stage 2, step 3: per-node floors for TERRAIN/
    PROFILE_CARRIED spans whose road beneath is NOT lowered — the
    crossing must rise, and ``grade_law.bridge_crossing_floor_m`` (road
    surface + clearance + structure thickness) is merged into the
    one-solve's floor dict by max so the hump solves itself under the
    existing grade caps (spec section 3.2, amendment A2).

    Returns ``{node_index: floor_elevation_m}``.  Guards:

    * only spans with a fully-draped DSF road segment crossing the
      footprint (an elevated ramp flies over on its own structure);
    * only decks whose own crest stands at least the road clearance
      above the datum (``deck_top_y_m >= BRIDGE_ROAD_CLEARANCE_M``) —
      a flush deck (EDDF Bridge_2/3/4, crest ≈ 0) encodes "the pack
      handles the road" and gets restraint, never a floor (ruling R9:
      the vertical split is read from the object, not assumed).

    Road surface elevation source (spec order, stage-2 subset): the
    draped road polyline's DEM samples inside the footprint (median) —
    the road-untouched case; the pack-trench and solved-corridor
    sources land with the audits (W-V) once a measured record carries
    them."""
    classification = _object_bridge_classification(layout)
    if classification is None:
        return {}
    road_networks = _object_bridge_road_networks(layout)
    if not road_networks:
        return {}
    to_meters, meters_to_lat_lon = _local_meter_projections(layout.anchor)
    floors: dict = {}
    for bridge in classification.bridges:
        crossing = _bridge_crossing_floor_for_bridge(
            bridge, road_networks, dem, tile_lat, tile_lon,
            to_meters, meters_to_lat_lon,
        )
        if crossing is None:
            continue
        floor_value, footprint = crossing
        applied = 0
        for node_index, (x, y) in enumerate(nodes):
            try:
                if not footprint.contains(Point(x, y)):
                    continue
            except _GEOM_EXC:
                continue
            known = floors.get(node_index)
            if known is None or floor_value > known:
                floors[node_index] = floor_value
                applied += 1
        if applied:
            UI.vprint(
                2,
                f"   [object-bridge] crossing floor {floor_value:.2f} m "
                f"on {applied} node(s) inside {bridge.contract} span "
                f"{bridge.object_resources}",
            )
    return floors


def _emit_underpass_road_approaches(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        clearance_depth_m: float = 8.0,
        approach_length_m: float = 80.0,
        road_width_m: float = 22.0,
        ramp_step_m: float = 20.0,
        scenery_has_bridge_objects: bool = False,
        ) -> int:
    """For each underpass case (taxi BRIDGE rect or road TUNNEL
    portal), emit a chain of sloped road-following polygons that
    transition the road surface from outside-DEM elevation down
    to ``apt_elev − clearance_depth_m`` near the underpass and
    back up to DEM after.

    Per user 2026-04-29 (KBNA / KPHX taxi-bridge case): without
    these polygons, the patch's mesh under a bridge sits at
    apt_elev (interpolated from the surrounding airport pavement),
    and OSM-tagged roads rendered at DEM elevation by Ortho4XP's
    road layer get buried.  Emitting a chain of road-following
    polygons forces the mesh under the road to step down to a
    height low enough for the road to clear the bridge underside
    (default 8 m below airport surface), then ramp back up to DEM
    away from the airport.

    Algorithm per underpass surface (bridge rect or tunnel portal
    region):

      1. Find OSM road LineStrings that cross the surface's
         footprint (or pass within 5 m of its long edges, for
         tunnel portals where the road just barely touches).
      2. For each crossing road, find the exterior segments
         immediately approaching and departing the surface.
      3. Walk each approach segment in ``ramp_step_m`` (20 m)
         increments, emitting a sloped 4-corner rect per step
         where elevation interpolates between DEM (start) and
         ``apt_elev − clearance_depth_m`` (end).
      4. Inside the underpass surface itself, emit a flat road-
         following polygon at ``apt_elev − clearance_depth_m``.

    Width of every emitted road polygon: ``road_width_m``
    (22 m by default; matches tunnel-ramp width).

    Returns the number of UNDERPASS surfaces processed.

    Feature B re-source (``O4_OBJECT_BRIDGE_TERRAIN``, spec section 3.2):
    when the object-terrain classifier has run, DECK_CARRIED spans get
    their corridor from the OBJECT'S geometry (footprint, deck elevation,
    girder clearance) and the sibling DSF road network, computed FIRST;
    TERRAIN_CARRIED / PROFILE_CARRIED footprints then SUPPRESS the legacy
    OSM-driven corridor beneath them, and object-handled footprints are not
    re-emitted by the legacy path.  With the gate off there is no cached
    classification and the whole block below is byte-identical to today.
    """
    from .pipeline import _load_osm_big_roads
    # Feature-B object-sourced corridors (gated; no-op when off).
    object_corridor_count = 0
    object_suppression_polygons: list[Polygon] = []
    object_covered_polygons: list[Polygon] = []
    _classification = _object_bridge_classification(layout)
    if _classification is not None:
        (object_corridor_count,
         object_suppression_polygons,
         object_covered_polygons) = _emit_object_sourced_bridge_corridors(
            layout, dem, tile_lat, tile_lon, _classification,
            _object_bridge_road_networks(layout),
            road_width_m, ramp_step_m, approach_length_m,
        )
    _object_footprints = (
        object_suppression_polygons + object_covered_polygons
    )
    # Collect underpass surfaces.
    bridge_shapes = [s for s in layout.shapes
                     if getattr(s, "is_bridge", False)
                     and s.polygon is not None
                     and not s.polygon.is_empty]
    if not bridge_shapes:
        return object_corridor_count
    nodes_r, ways_r = _load_osm_big_roads(
        layout.anchor[0], layout.anchor[1])
    if not ways_r:
        return object_corridor_count
    _to_m, _m_to_ll = _local_meter_projections(layout.anchor)
    nodes_m: dict[str, tuple[float, float]] = {}
    for nid, (lat, lon) in nodes_r.items():
        nodes_m[nid] = _to_m(lon, lat)
    HW_TYPES = {
        "motorway", "trunk", "primary", "secondary",
        "tertiary", "motorway_link", "trunk_link",
        "primary_link", "residential", "service",
    }
    # Collect candidate road LineStrings (not bridge or tunnel
    # tagged — those are special cases).  A road that PASSES
    # UNDER a taxi bridge typically isn't tagged bridge=yes
    # itself; only the airport surface is.  But a ROAD that
    # itself bridges over something else IS tagged bridge=yes;
    # we skip those because we don't want to emit road shapes
    # for road-on-road bridges.
    road_lines: list[LineString] = []
    for _wid, nrefs, tags in ways_r:
        if tags.get("highway") not in HW_TYPES:
            continue
        if tags.get("bridge") and tags.get("bridge") != "no":
            continue
        if (tags.get("tunnel")
                and tags.get("tunnel") != "no"):
            continue
        pts = [nodes_m[n] for n in nrefs if n in nodes_m]
        if len(pts) < 2:
            continue
        try:
            ls = LineString(pts)
        except _GEOM_EXC:
            continue
        if ls.is_empty or ls.length < 5.0:
            continue
        road_lines.append(ls)
    if not road_lines:
        return object_corridor_count
    n_processed = 0
    for s in bridge_shapes:
        # Feature B: skip a bridge rect already handled by an
        # object-sourced corridor (DECK_CARRIED) or lying inside a
        # suppressed TERRAIN/PROFILE_CARRIED span footprint — the object
        # geometry, not the OSM inference, governs there (spec section 3.2).
        if _object_footprints:
            try:
                if any(s.polygon.intersects(footprint)
                       for footprint in _object_footprints):
                    continue
            except _GEOM_EXC:
                pass
        # Bridge deck elevation.
        if (s.altitude_high is not None
                and s.altitude_low is not None):
            deck_elev = 0.5 * (s.altitude_high + s.altitude_low)
        elif s.altitude is not None:
            deck_elev = s.altitude
        else:
            continue
        low_elev = float(deck_elev) - clearance_depth_m
        # Find road LineStrings that cross the bridge footprint.
        for road_ls in road_lines:
            try:
                inside = road_ls.intersection(s.polygon)
            except _GEOM_EXC:
                continue
            if inside.is_empty:
                continue
            # Pick the longest contiguous piece if multiple.
            if hasattr(inside, "geoms"):
                cand = [g for g in inside.geoms
                        if g.geom_type == "LineString"
                        and g.length > 1.0]
                if not cand:
                    continue
                inside = max(cand, key=lambda g: g.length)
            elif inside.geom_type != "LineString":
                continue
            # Inside-bridge flat polygon at low_elev — only emit
            # when the scenery has a 3D bridge OBJ (KBNA case).
            # In that case the road cuts straight through under
            # the bridge model.  Without a bridge OBJ (KPHX
            # case) the taxi rect itself acts as a flat plate
            # and the road approaches stop at the bridge edge.
            if scenery_has_bridge_objects:
                try:
                    inside_buf = inside.buffer(
                        road_width_m / 2.0,
                        cap_style=2, join_style=2)
                    if (inside_buf.geom_type == "Polygon"
                            and not inside_buf.is_empty):
                        layout.shapes.append(BuiltShape(
                            polygon=inside_buf,
                            role=ROLE_TUNNEL_RAMP,
                            ref="bridge_underpass",
                            altitude=round(low_elev, 1)))
                except _GEOM_EXC:
                    pass
            # Approach + departure ramp chains.  Find the parts
            # of the road OUTSIDE the bridge polygon, then walk
            # each in ramp_step_m steps emitting one sloped rect
            # per step.
            try:
                outside = road_ls.difference(s.polygon)
            except _GEOM_EXC:
                outside = None
            if outside is None or outside.is_empty:
                continue
            outside_pieces = (
                list(outside.geoms)
                if hasattr(outside, "geoms")
                else [outside])
            for piece in outside_pieces:
                if (piece.is_empty
                        or piece.geom_type != "LineString"):
                    continue
                # Decide which end of the piece TOUCHES the bridge.
                pcoords = list(piece.coords)
                if len(pcoords) < 2:
                    continue
                p_start = Point(pcoords[0])
                p_end = Point(pcoords[-1])
                d_start = s.polygon.distance(p_start)
                d_end = s.polygon.distance(p_end)
                if d_start <= d_end:
                    # piece runs FROM bridge edge OUT to far end —
                    # walk it as approach (low → high).
                    # Cap to approach_length_m.
                    walk_len = min(piece.length, approach_length_m)
                    bridge_end_pt = piece.interpolate(0.0)
                    far_end_pt = piece.interpolate(walk_len)
                    walk = LineString([(bridge_end_pt.x,
                                          bridge_end_pt.y),
                                        (far_end_pt.x,
                                          far_end_pt.y)])
                    # Use the actual sub-LineString shape for
                    # better following; but for simplicity use the
                    # straight sub-segment for ramp emission.
                    walk = LineString(pcoords[:])
                    # Trim walk to first ``walk_len`` metres.
                else:
                    walk_len = min(piece.length, approach_length_m)
                    walk = LineString(list(reversed(pcoords)))
                # Step through walk_len in ramp_step_m steps.
                # Each step is a sloped rect of (low_elev → DEM).
                u_prev = 0.0
                while u_prev < walk_len - 1.0:
                    u_next = min(walk_len, u_prev + ramp_step_m)
                    p0 = walk.interpolate(u_prev)
                    p1 = walk.interpolate(u_next)
                    seg_len = math.hypot(p1.x - p0.x, p1.y - p0.y)
                    if seg_len < 1.0:
                        break
                    # Tangent + perpendicular.
                    tx = (p1.x - p0.x) / seg_len
                    ty = (p1.y - p0.y) / seg_len
                    nx = -ty
                    ny = tx
                    half_w = road_width_m / 2.0
                    # Elevation interpolation: u_prev → low_elev
                    # at the bridge, DEM at the far end.
                    frac0 = u_prev / walk_len
                    frac1 = u_next / walk_len
                    try:
                        lat0_p, lon0_p = _m_to_ll(p0.x, p0.y)
                        lat1_p, lon1_p = _m_to_ll(p1.x, p1.y)
                        dem0 = _sample_dem(
                            dem, tile_lat, tile_lon,
                            lat0_p, lon0_p)
                        dem1 = _sample_dem(
                            dem, tile_lat, tile_lon,
                            lat1_p, lon1_p)
                    except _GEOM_EXC:
                        dem0 = dem1 = None
                    if dem0 is None or dem1 is None:
                        break
                    e0 = (1.0 - frac0) * low_elev + frac0 * dem0
                    e1 = (1.0 - frac1) * low_elev + frac1 * dem1
                    corners = [
                        (p0.x + nx * half_w,
                         p0.y + ny * half_w),
                        (p1.x + nx * half_w,
                         p1.y + ny * half_w),
                        (p1.x - nx * half_w,
                         p1.y - ny * half_w),
                        (p0.x - nx * half_w,
                         p0.y - ny * half_w),
                    ]
                    try:
                        seg_poly = Polygon(corners)
                        if not seg_poly.is_valid:
                            seg_poly = seg_poly.buffer(0)
                        if (seg_poly.geom_type == "Polygon"
                                and not seg_poly.is_empty):
                            # corners 0,3 = "high" end vs corners
                            # 1,2 = "low" end depends on which
                            # end is closer to the bridge.  e0
                            # (start = bridge side) is lower.
                            if abs(e0 - e1) >= 0.1:
                                layout.shapes.append(BuiltShape(
                                    polygon=seg_poly,
                                    role=ROLE_TUNNEL_RAMP,
                                    ref="bridge_approach",
                                    altitude_high=round(
                                        max(e0, e1), 1),
                                    altitude_low=round(
                                        min(e0, e1), 1)))
                            else:
                                layout.shapes.append(BuiltShape(
                                    polygon=seg_poly,
                                    role=ROLE_TUNNEL_RAMP,
                                    ref="bridge_approach",
                                    altitude=round(
                                        0.5 * (e0 + e1), 1)))
                    except _GEOM_EXC:
                        pass
                    u_prev = u_next
        n_processed += 1
    return n_processed + object_corridor_count


def _discover_depressed_roads(
        layout: "PavementLayout",
        xplane_root: str,
        icao: str,
        ) -> tuple[dict | None, set | None, BaseGeometry | None]:
    """Shared discovery for the depressed-road system (geometry only,
    no DEM): which OSM highway ways must be depressed through this
    airport.  A way qualifies when its inside-boundary stretch passes
    under an ``aeroway=*, bridge=yes`` way, plus every way CONNECTED
    to a qualifying one via shared OSM nodes inside the boundary
    (on/off ramps).  Factored out of
    ``_emit_through_airport_depressed_roads`` so the PRE-solve
    terminal-gap carve can see the same corridors the POST-solve
    plate emitter will pave.

    Returns ``(way_lookup, depressed_set, boundary)`` —
    ``way_lookup``: wid → (LineString in layout meters, node refs);
    ``depressed_set``: the qualifying wids — or ``(None, None,
    None)`` when the airport has no depressed roads.
    """
    from .pipeline import _load_osm_airports, _load_osm_big_roads
    if (layout.airport_boundary is None
            or layout.airport_boundary.is_empty):
        return (None, None, None)
    boundary = layout.airport_boundary
    # Build a slightly contracted boundary for inside-vs-outside
    # tests so a road point exactly ON the boundary doesn't bounce
    # between true / false on numeric jitter.
    try:
        boundary_strict = boundary.buffer(-0.5)
        if boundary_strict.is_empty:
            boundary_strict = boundary
    except _GEOM_EXC:
        boundary_strict = boundary

    # Load OSM airport-layer tile (for aeroway=bridge LineStrings).
    try:
        nodes_a, ways_a, _ = _load_osm_airports(
            xplane_root, icao,
            layout.anchor[0], layout.anchor[1])
    except _GEOM_EXC:
        return (None, None, None)
    if not ways_a:
        return (None, None, None)

    _to_m, _m_to_ll = _local_meter_projections(layout.anchor)

    # ── Bridge LineStrings (airport-layer OSM) ─────────────────
    bridge_lines: list[LineString] = []
    nodes_a_m: dict[str, tuple[float, float]] = {}
    for nid, (lat, lon) in nodes_a.items():
        nodes_a_m[nid] = _to_m(lon, lat)
    for wid, nrefs, tags in ways_a:
        if not tags.get("aeroway"):
            continue
        if tags.get("bridge", "") not in ("yes", "viaduct"):
            continue
        pts = [nodes_a_m[n] for n in nrefs if n in nodes_a_m]
        if len(pts) < 2:
            continue
        try:
            ls = LineString(pts)
        except _GEOM_EXC:
            continue
        if ls.is_empty or ls.length < 5.0:
            continue
        bridge_lines.append(ls)
    if not bridge_lines:
        return (None, None, None)

    # Load OSM big_roads (for highway ways) — only now that we know
    # the airport actually has aeroway bridges.
    nodes_r, ways_r = _load_osm_big_roads(
        layout.anchor[0], layout.anchor[1])
    if not ways_r:
        return (None, None, None)

    # ── Highway candidates (big_roads OSM) ─────────────────────
    HW_TYPES = {
        "motorway", "trunk", "primary", "secondary",
        "tertiary", "motorway_link", "trunk_link",
        "primary_link", "secondary_link", "tertiary_link",
        "residential", "service", "unclassified",
    }
    nodes_r_m: dict[str, tuple[float, float]] = {}
    for nid, (lat, lon) in nodes_r.items():
        nodes_r_m[nid] = _to_m(lon, lat)
    way_data: list[tuple[str, LineString, list[str]]] = []
    for wid, nrefs, tags in ways_r:
        if tags.get("highway") not in HW_TYPES:
            continue
        if tags.get("bridge", "") in ("yes", "viaduct"):
            # The road IS the bridge, not what's under — skip.
            continue
        # tunnel=building_passage tagging is INCLUDED.  At KPHX,
        # the under-bridge road segments use this tag; they're
        # exactly the seeds we want.
        pts = [nodes_r_m[n] for n in nrefs if n in nodes_r_m]
        if len(pts) < 2:
            continue
        try:
            ls = LineString(pts)
        except _GEOM_EXC:
            continue
        if ls.is_empty or ls.length < 5.0:
            continue
        way_data.append((wid, ls, list(nrefs)))
    if not way_data:
        return (None, None, None)

    # ── Seed: ways whose inside-boundary section crosses a bridge ──
    BRIDGE_PROXIMITY_M = 5.0
    seed_depressed: set = set()
    for wid, ls, _nrefs in way_data:
        try:
            inside = ls.intersection(boundary)
        except _GEOM_EXC:
            continue
        if inside.is_empty:
            continue
        segs = []
        if inside.geom_type == "LineString":
            segs = [inside]
        elif inside.geom_type == "MultiLineString":
            segs = list(inside.geoms)
        for seg in segs:
            if seg.is_empty or seg.length < 1.0:
                continue
            for bls in bridge_lines:
                try:
                    if seg.distance(bls) < BRIDGE_PROXIMITY_M:
                        seed_depressed.add(wid)
                        break
                except _GEOM_EXC:
                    continue
            if wid in seed_depressed:
                break
    if not seed_depressed:
        return (None, None, None)

    # ── BFS over OSM-graph node-sharing INSIDE the boundary ────
    # On-ramps/off-ramps inside the airport are separate OSM
    # ways; if they connect to a depressed seed at any node
    # INSIDE the boundary, they must be depressed too (otherwise
    # the seed and the connecting way disagree on altitude at
    # their shared node and X-Plane renders a cliff).
    node_to_ways: dict[str, list[str]] = {}
    way_lookup: dict[str, tuple[LineString, list[str]]] = {}
    for wid, ls, nrefs in way_data:
        way_lookup[wid] = (ls, nrefs)
        for n in nrefs:
            node_to_ways.setdefault(n, []).append(wid)
    depressed_set: set = set(seed_depressed)
    queue: list[str] = list(seed_depressed)
    while queue:
        wid = queue.pop()
        ls, nrefs = way_lookup[wid]
        for n in nrefs:
            n_xy = nodes_r_m.get(n)
            if n_xy is None:
                continue
            try:
                if not boundary_strict.contains(Point(n_xy)):
                    continue
            except _GEOM_EXC:
                continue
            for other_wid in node_to_ways.get(n, []):
                if other_wid in depressed_set:
                    continue
                # Only propagate if the other way also has an
                # inside-boundary portion (otherwise it's just a
                # surface road glancing the boundary node).
                _o_ls, _o_nrefs = way_lookup[other_wid]
                try:
                    if _o_ls.intersection(boundary).is_empty:
                        continue
                except _GEOM_EXC:
                    continue
                depressed_set.add(other_wid)
                queue.append(other_wid)
    return (way_lookup, depressed_set, boundary)


def _depressed_road_corridor_band(
        layout: "PavementLayout",
        xplane_root: str,
        icao: str,
        road_width_m: float = 22.0,
        clearance_m: float = 0.5,
        ) -> BaseGeometry | None:
    """The inside-boundary depressed-road corridor, buffered to road
    half-width + ``clearance_m`` — the band that must stay OPEN
    through terminal pads (per user 2026-06-10: terminals split and
    leave a gap for the road to pass through; the road then keeps
    ``clearance_m`` to the terminal edges).  ``None`` when the
    airport has no depressed roads."""
    way_lookup, depressed_set, boundary = _discover_depressed_roads(
        layout, xplane_root, icao)
    if not depressed_set:
        return None
    bands: list[BaseGeometry] = []
    half_w = road_width_m / 2.0
    for wid in sorted(depressed_set):
        ls, _nrefs = way_lookup[wid]
        try:
            inside = ls.intersection(boundary)
        except _GEOM_EXC:
            continue
        if inside.is_empty:
            continue
        try:
            band = inside.buffer(half_w + clearance_m,
                                 cap_style=2, join_style=2)
        except _GEOM_EXC:
            continue
        if not band.is_empty:
            bands.append(band)
    if not bands:
        return None
    try:
        return unary_union(bands)
    except _GEOM_EXC:
        return None


def _emit_through_airport_depressed_roads(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        xplane_root: str,
        icao: str,
        depression_depth_m: float = 8.0,
        max_ramp_grade: float = 0.04,
        ramp_min_length_m: float = 200.0,
        arm_max_length_m: float = 500.0,
        road_width_m: float = 22.0,
        retaining_wall_width_m: float = 1.0,
        wall_gap_m: float = 0.5,
        boundary_clearance_m: float = 1.0,
        ) -> tuple[int, set]:
    """For each public road that ENTERS the airport boundary
    AND passes under a tagged ``aeroway=*, bridge=yes`` way (or
    is connected via OSM-graph node-sharing to a road that does)
    inside the airport, emit:

      1. A flat road-following polygon along the entire inside-
         boundary stretch at ``apt_elev − depression_depth_m``.
         Subsequent OSM road rendering (Ortho4XP's own road
         layer) sits on top of this flat plate.
      2. At each boundary entry/exit point, a ramp polygon
         OUTSIDE the airport that climbs from the depressed
         level back up to the local DEM, capped at
         ``max_ramp_grade`` (default 4 %).

    Per user 2026-04-29 (KPHX Sky Harbor Blvd): when a road
    passes under multiple airport bridges, the road MUST be
    depressed for its entire inside-airport stretch — not just
    at the bridges.  Two bridges spanning the same continuous
    road imply the road can never come back up between them.
    The general solution: any road that enters the airport
    boundary and crosses any aeroway=bridge inside is treated
    this way, plus any road CONNECTED to it via shared OSM
    nodes within the boundary (so on/off ramps inside the
    airport stay coherent with the main road).

    OSM tags consulted:
      * ``aeroway=*, bridge=yes|viaduct`` — bridge LineStrings
        (the airport surface above the depression).
      * ``highway=*`` (motorway, trunk, primary, secondary,
        tertiary, residential, service + their *_link forms) —
        the road network candidates.
      * ``bridge=yes|viaduct`` on a highway — that road is
        ITSELF a bridge over something else (skip; we're
        looking for the road UNDERNEATH).
      * ``tunnel=yes|building_passage`` on a highway — INCLUDED
        as seeds (the airport-bridge case typically tags the
        under-bridge road segment as building_passage in OSM).
        ``_emit_tunnel_portals`` is told to skip every OSM way
        depressed here so we don't double-emit.

    Boundary coordination: the union of every emitted polygon
    is buffered by ``boundary_clearance_m`` and subtracted from
    each ``ROLE_BOUNDARY`` shape (same pattern as the tunnel
    and taxi-bridge emitters), with NN-resampling of per-vertex
    altitudes so the boundary ribbon retains its altitude tags
    after the clip.

    Returns ``(n_emitted, depressed_way_ids)``.  The way-id
    set is intended for ``_emit_tunnel_portals`` to skip — it
    contains every OSM highway way (raw road network) handled
    by this pass, so the explicit-tunnel emitter doesn't
    double-process the same building_passage segments.
    """
    way_lookup, depressed_set, boundary = _discover_depressed_roads(
        layout, xplane_root, icao)
    if not depressed_set:
        return (0, set())

    plan_grade = max(
        max_ramp_grade - TUNNEL_RAMP_GRADE_SAFETY_MARGIN, 1e-3)
    arm_walk_max_m = max(arm_max_length_m, ramp_min_length_m,
                         depression_depth_m / plan_grade)

    _to_m, _m_to_ll = _local_meter_projections(layout.anchor)

    def _airport_elevation_at(cx: float, cy: float) -> float | None:
        # Reuse pattern from _emit_tunnel_portals: prefer the
        # boundary-ribbon's per-vertex altitude near the point,
        # fall back to DEM.
        best_d = float('inf')
        best_alt: float | None = None
        for s in layout.shapes:
            if s.role != ROLE_BOUNDARY:
                continue
            if s.ref != "airport_boundary":
                continue
            if not s.node_altitudes:
                continue
            try:
                rcoords = list(s.polygon.exterior.coords)
            except _GEOM_EXC:
                continue
            if rcoords and rcoords[0] == rcoords[-1]:
                rcoords = rcoords[:-1]
            for k, (vx, vy) in enumerate(rcoords):
                if k >= len(s.node_altitudes):
                    break
                d = math.hypot(vx - cx, vy - cy)
                if d < best_d:
                    best_d = d
                    best_alt = s.node_altitudes[k]
        if best_alt is not None and best_d <= 400.0:
            return float(best_alt)
        try:
            lat, lon = _m_to_ll(cx, cy)
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None

    # ── Emit one set of polygons per depressed way ─────────────
    n_emitted = 0
    exclusion_zones: list[Polygon] = []
    half_w = road_width_m / 2.0

    # Airside clearance union (user 2026-06-10): a depressed-road
    # plate must STOP ``wall_gap_m`` (0.5 m) short of taxiway /
    # junction / apron / runway pavement — the airside surface IS
    # the bridge deck there — and resume on the other side.
    # Terminals are NOT in this union: they yield instead (the
    # pre-solve terminal-gap carve splits the pad around the road
    # corridor), so a plate crossing an uncarved terminal shows up
    # as an overlap warning rather than silently truncating the
    # road.
    _AIRSIDE_STOP_ROLES = {
        ROLE_RUNWAY, ROLE_RUNWAY_CROSSING, ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB, ROLE_CROSS_CONNECTOR,
        ROLE_JUNCTION, ROLE_APRON,
    }
    try:
        _airside_polys = [s.polygon for s in layout.shapes
                          if s.role in _AIRSIDE_STOP_ROLES
                          and s.polygon is not None
                          and not s.polygon.is_empty]
        airside_clear = (unary_union(_airside_polys)
                         .buffer(wall_gap_m)
                         if _airside_polys else None)
    except _GEOM_EXC:
        airside_clear = None
    # Terminal pads: the pre-solve carve already splits them around
    # the road corridor with the 0.5 m clearance; subtract them
    # UNBUFFERED here too so buffer-miter mismatches between the
    # carve band and the plate buffer can't leave cm² overlaps at
    # bends (KPHX terminal2, 0.4 m²).
    try:
        _term_polys = [s.polygon for s in layout.shapes
                       if s.role == ROLE_BUILDING
                       and s.polygon is not None
                       and not s.polygon.is_empty]
        terminal_union = (unary_union(_term_polys)
                          if _term_polys else None)
    except _GEOM_EXC:
        terminal_union = None
    # Running union of already-emitted plates: parallel
    # carriageways / ramp chains closer than the 22 m plate width
    # used to emit overlapping plates (KPHX Sky Harbor Blvd,
    # overlap storm up to 1 437 m²); subtracting the running union
    # (buffered 1 cm so shared-edge float noise becomes a hairline
    # gap, not an epsilon overlap) keeps the depressed surface
    # single-cover.
    plate_union: BaseGeometry | None = None
    MIN_PLATE_PIECE_M2 = 25.0

    def _smooth_walk(pts: list[tuple[float, float]],
                      min_segment_m: float = 15.0
                      ) -> list[tuple[float, float]]:
        """Drop near-colinear / closely-spaced intermediate
        vertices so altitude rounding can't push per-segment
        grade above the design limit."""
        if len(pts) < 3:
            return list(pts)
        merged: list[tuple[float, float]] = [pts[0]]
        for k in range(1, len(pts)):
            d = math.hypot(pts[k][0] - merged[-1][0],
                           pts[k][1] - merged[-1][1])
            if d < min_segment_m and k != len(pts) - 1:
                continue
            merged.append(pts[k])
        return merged

    for wid in sorted(depressed_set):
        ls, nrefs = way_lookup[wid]
        # 1) Inside-boundary flat plate(s).
        try:
            inside = ls.intersection(boundary)
        except _GEOM_EXC:
            continue
        if inside.is_empty:
            continue
        inside_segs = []
        if inside.geom_type == "LineString":
            inside_segs = [inside]
        elif inside.geom_type == "MultiLineString":
            inside_segs = list(inside.geoms)
        for seg in inside_segs:
            if seg.is_empty or seg.length < 5.0:
                continue
            ctr = seg.centroid
            apt_elev = _airport_elevation_at(ctr.x, ctr.y)
            if apt_elev is None:
                continue
            elev_low = apt_elev - depression_depth_m
            try:
                flat_poly = seg.buffer(
                    half_w, cap_style=2, join_style=2)
                if not flat_poly.is_valid:
                    flat_poly = flat_poly.buffer(0)
            except _GEOM_EXC:
                continue
            if flat_poly.is_empty:
                continue
            # Stop short of airside pavement (0.5 m) and of plates
            # already emitted; what survives on each side of a
            # bridge deck is its own plate ("start again on the
            # other side").
            clipped = flat_poly
            try:
                if airside_clear is not None:
                    clipped = clipped.difference(airside_clear)
                if terminal_union is not None:
                    clipped = clipped.difference(terminal_union)
                if plate_union is not None:
                    clipped = clipped.difference(
                        plate_union.buffer(0.01))
            except _GEOM_EXC:
                pass
            if clipped.is_empty:
                continue
            plate_pieces = [g for g in
                            (clipped.geoms
                             if hasattr(clipped, "geoms")
                             else [clipped])
                            if g.geom_type == "Polygon"
                            and not g.is_empty
                            and g.area >= MIN_PLATE_PIECE_M2]
            for plate in plate_pieces:
                layout.shapes.append(BuiltShape(
                    polygon=plate,
                    role=ROLE_TUNNEL_RAMP,
                    ref="depressed_road",
                    altitude=round(elev_low, 1)))
                exclusion_zones.append(plate)
                n_emitted += 1
            if plate_pieces:
                try:
                    new_u = unary_union(plate_pieces)
                    plate_union = (new_u if plate_union is None
                                   else plate_union.union(new_u))
                except _GEOM_EXC:
                    pass

        # 2) Outside-boundary ramp(s) — one per side of the
        #    boundary the way crosses.  Take the OSM polyline
        #    OUTSIDE the boundary; for each outside piece, walk
        #    OUTWARD from the boundary edge, truncate where the
        #    grade requirement is satisfied, then emit ramp
        #    polygons with bisector vertex sharing at bends.
        try:
            outside = ls.difference(boundary)
        except _GEOM_EXC:
            outside = None
        if outside is None or outside.is_empty:
            continue
        outside_pieces = ([outside]
                          if outside.geom_type == "LineString"
                          else (list(outside.geoms)
                                if outside.geom_type == "MultiLineString"
                                else []))
        for piece in outside_pieces:
            if piece.is_empty or piece.length < 5.0:
                continue
            coords = list(piece.coords)
            # Order coords so coords[0] is at the boundary,
            # coords[-1] is far outside.  The boundary edge is
            # the closest of the two endpoints to the boundary
            # exterior (distance 0 vs distance > 0).
            try:
                d_start = boundary.exterior.distance(
                    Point(coords[0]))
            except _GEOM_EXC:
                d_start = float('inf')
            try:
                d_end = boundary.exterior.distance(
                    Point(coords[-1]))
            except _GEOM_EXC:
                d_end = float('inf')
            if d_end < d_start:
                coords = list(reversed(coords))
            walk = _smooth_walk(coords)
            if len(walk) < 2:
                continue
            # Truncate the walk to a length whose grade keeps
            # ≤ plan_grade given DEM at the far end.
            apt_elev = _airport_elevation_at(*walk[0])
            if apt_elev is None:
                continue
            elev_low = apt_elev - depression_depth_m
            cum = 0.0
            kept_pts: list[tuple[float, float]] = [walk[0]]
            grade_ok_at: float = 0.0
            for i in range(1, len(walk)):
                seg_len = math.hypot(
                    walk[i][0] - walk[i - 1][0],
                    walk[i][1] - walk[i - 1][1])
                cum += seg_len
                kept_pts.append(walk[i])
                if cum > arm_walk_max_m:
                    grade_ok_at = cum
                    break
                try:
                    plat, plon = _m_to_ll(*walk[i])
                    dem_h = _sample_dem(
                        dem, tile_lat, tile_lon, plat, plon)
                except _GEOM_EXC:
                    dem_h = None
                if dem_h is None:
                    continue
                drop = float(dem_h) - elev_low
                req = (drop / plan_grade if drop > 0 else 0.0)
                if cum >= req and cum >= ramp_min_length_m:
                    grade_ok_at = cum
                    break
                grade_ok_at = cum
            walk = kept_pts
            if len(walk) < 2:
                continue
            far_xy = walk[-1]
            try:
                far_lat, far_lon = _m_to_ll(*far_xy)
                far_dem = _sample_dem(
                    dem, tile_lat, tile_lon, far_lat, far_lon)
            except _GEOM_EXC:
                far_dem = None
            if far_dem is None:
                far_dem = apt_elev
            max_drop = plan_grade * grade_ok_at
            if (far_dem - elev_low) > max_drop:
                far_dem = elev_low + max_drop
            elev_high = far_dem
            cum_dists = [0.0]
            for i in range(1, len(walk)):
                cum_dists.append(cum_dists[-1] + math.hypot(
                    walk[i][0] - walk[i - 1][0],
                    walk[i][1] - walk[i - 1][1]))
            total_walk = cum_dists[-1]
            if total_walk < 5.0:
                continue
            # Per-vertex bisector perpendicular for shared corners
            # at bends (same pattern as _emit_tunnel_portals).
            n_w = len(walk)
            verts_perp: list[tuple[float, float]] = []
            verts_scale: list[float] = []
            for i in range(n_w):
                if i == 0:
                    s = (walk[1][0] - walk[0][0],
                         walk[1][1] - walk[0][1])
                    sl = math.hypot(*s) or 1e-6
                    verts_perp.append((-s[1] / sl, s[0] / sl))
                    verts_scale.append(1.0)
                elif i == n_w - 1:
                    s = (walk[i][0] - walk[i - 1][0],
                         walk[i][1] - walk[i - 1][1])
                    sl = math.hypot(*s) or 1e-6
                    verts_perp.append((-s[1] / sl, s[0] / sl))
                    verts_scale.append(1.0)
                else:
                    s1 = (walk[i][0] - walk[i - 1][0],
                          walk[i][1] - walk[i - 1][1])
                    s2 = (walk[i + 1][0] - walk[i][0],
                          walk[i + 1][1] - walk[i][1])
                    l1 = math.hypot(*s1) or 1e-6
                    l2 = math.hypot(*s2) or 1e-6
                    u1 = (s1[0] / l1, s1[1] / l1)
                    u2 = (s2[0] / l2, s2[1] / l2)
                    avg = ((u1[0] + u2[0]) / 2.0,
                           (u1[1] + u2[1]) / 2.0)
                    al = math.hypot(*avg)
                    if al < 1e-6:
                        verts_perp.append((-u1[1], u1[0]))
                        verts_scale.append(1.0)
                        continue
                    tangent = (avg[0] / al, avg[1] / al)
                    perp = (-tangent[1], tangent[0])
                    dot = u1[0] * u2[0] + u1[1] * u2[1]
                    cos_half = max(0.1, math.sqrt(
                        max(0.0, (1.0 + dot) / 2.0)))
                    verts_perp.append(perp)
                    verts_scale.append(1.0 / cos_half)

            def _vertex_offset(idx: int, off: float
                               ) -> tuple[float, float]:
                px, py = walk[idx]
                nx, ny = verts_perp[idx]
                scaled = off * verts_scale[idx]
                return (px + nx * scaled, py + ny * scaled)

            # Same EFFECTIVE-length lerp as ``_emit_chain``: the miter
            # join shortens the inner quad edge on bends, so a
            # centerline-proportional Δe reads over the ramp cap along
            # that edge.
            effective_cums = [0.0]
            for i in range(n_w - 1):
                seg_len = cum_dists[i + 1] - cum_dists[i]
                edge_plus = math.dist(_vertex_offset(i, +half_w),
                                      _vertex_offset(i + 1, +half_w))
                edge_minus = math.dist(_vertex_offset(i, -half_w),
                                       _vertex_offset(i + 1, -half_w))
                effective_cums.append(
                    effective_cums[-1]
                    + min(seg_len, edge_plus, edge_minus))
            effective_total = effective_cums[-1]
            if effective_total < 1.0:
                continue

            for i in range(n_w - 1):
                d_a = cum_dists[i]
                d_b = cum_dists[i + 1]
                seg_len = d_b - d_a
                if seg_len < 0.5:
                    continue
                frac_a = effective_cums[i] / effective_total
                frac_b = effective_cums[i + 1] / effective_total
                e_a = (1 - frac_a) * elev_low + frac_a * elev_high
                e_b = (1 - frac_b) * elev_low + frac_b * elev_high
                ra = _vertex_offset(i, +half_w)
                rb = _vertex_offset(i + 1, +half_w)
                rc = _vertex_offset(i + 1, -half_w)
                rd = _vertex_offset(i, -half_w)
                # Corners [0, 3] = HIGH end, [1, 2] = LOW end.
                if e_b >= e_a:
                    ramp_corners = [rb, ra, rd, rc]
                    eh, el = e_b, e_a
                else:
                    ramp_corners = [ra, rb, rc, rd]
                    eh, el = e_a, e_b
                try:
                    rp = Polygon(ramp_corners)
                    if not rp.is_valid:
                        rp = rp.buffer(0)
                    if (rp.geom_type != "Polygon"
                            or rp.is_empty
                            or rp.area < 0.5):
                        continue
                except _GEOM_EXC:
                    continue
                if abs(eh - el) >= 0.1:
                    layout.shapes.append(BuiltShape(
                        polygon=rp,
                        role=ROLE_TUNNEL_RAMP,
                        ref="depressed_approach",
                        altitude_high=round(eh, 2),
                        altitude_low=round(el, 2)))
                else:
                    layout.shapes.append(BuiltShape(
                        polygon=rp,
                        role=ROLE_TUNNEL_RAMP,
                        ref="depressed_approach",
                        altitude=round(0.5 * (eh + el), 2)))
                exclusion_zones.append(rp)

    # ── Boundary coordination ─────────────────────────────────
    if exclusion_zones:
        try:
            depressed_union = unary_union(exclusion_zones)
        except _GEOM_EXC:
            depressed_union = None
        if (depressed_union is None
                or depressed_union.is_empty):
            return (n_emitted, depressed_set)
        excl_union = depressed_union.buffer(boundary_clearance_m)
        kept_shapes: list[BuiltShape] = []
        for s in layout.shapes:
            if s.role != ROLE_BOUNDARY:
                kept_shapes.append(s)
                continue
            try:
                _old_ring = list(s.polygon.exterior.coords)
            except _GEOM_EXC:
                _old_ring = []
            if _old_ring and _old_ring[0] == _old_ring[-1]:
                _old_ring = _old_ring[:-1]
            _old_alts = (list(s.node_altitudes)
                          if s.node_altitudes else None)
            try:
                new_poly = s.polygon.difference(excl_union)
            except _GEOM_EXC:
                kept_shapes.append(s)
                continue
            if new_poly.is_empty:
                continue
            if new_poly.geom_type == "Polygon":
                s.polygon = new_poly
                resampled = _resample_node_altitudes_nn(
                    new_poly, _old_ring, _old_alts)
                if resampled is not None:
                    s.node_altitudes = resampled
                kept_shapes.append(s)
            elif new_poly.geom_type == "MultiPolygon":
                for g in new_poly.geoms:
                    if (g.geom_type != "Polygon"
                            or g.is_empty
                            or g.area < 5.0):
                        continue
                    resampled = _resample_node_altitudes_nn(
                        g, _old_ring, _old_alts)
                    kept_shapes.append(BuiltShape(
                        polygon=g,
                        role=s.role,
                        ref=s.ref,
                        altitude=(s.altitude
                                  if resampled is None
                                  else None),
                        node_altitudes=resampled))
        layout.shapes = kept_shapes
    return (n_emitted, depressed_set)
