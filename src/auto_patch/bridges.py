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
from shapely.ops import linemerge, nearest_points, unary_union

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
    SHARED_VERTEX_TOL_M,
)
from .pavement.vertices import _snap_polygon_vertices_to_rect_corners
from .pavement.runways import _sample_runway_segment_elev
from .elevation import _resample_node_altitudes_nn, _sample_dem
from .config import (
    SKIP_TUNNEL_RAMPS_NEAR_ROADS,
    TUNNEL_ADJACENT_ROAD_DIST_M,
    TUNNEL_FORK_THROAT,
)


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
    "motorway":         24.0,  # 6+ lanes per direction in some places
    "motorway_link":     8.0,
    "trunk":            22.0,
    "trunk_link":        8.0,
    "primary":          18.0,
    "primary_link":      7.0,
    "secondary":        11.0,  # ~half of trunk per user 2026-05-03
    "secondary_link":    7.0,
    "tertiary":          9.0,
    "tertiary_link":     6.0,
    "residential":       7.0,
    "service":           6.0,
    # Pseudo-type for railway tunnel bores (user 2026-06-12):
    # double-track right-of-way — far narrower than the 22 m road
    # default the KPHL RWY 26 rail branch first emitted with.
    "railway":          10.0,
}


def _carriageway_width_for(highway_type: str | None,
                            default_m: float) -> float:
    """Return the carriageway width in metres for an OSM highway
    type, falling back to ``default_m`` for unknown types.
    """
    if highway_type is None:
        return default_m
    return HIGHWAY_CARRIAGEWAY_WIDTH_M.get(highway_type, default_m)


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
        # carriageway.  40 m is wide enough for typical separated
        # carriageways; tighter values would split them into
        # separate caps which is wrong.
        portal_cluster_dist_m: float = 40.0,
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
    from .pipeline import _load_osm_airports, _load_osm_big_roads
    # Load big-roads OSM cache for this tile — AND small_roads (user
    # 2026-06-12, KPHL): the big/small highway split puts tertiary /
    # residential / service ways in small_roads, so a minor-road
    # tunnel bore (KPHL's road+rail tunnel under the RWY 26 hill:
    # highway=tertiary, 151 m past the threshold) was invisible to
    # this emitter even though its type is in HW_TUNNEL_TYPES.
    nodes_r, ways_r = _load_osm_big_roads(
        layout.anchor[0], layout.anchor[1])
    _big_way_ids = {w[0] for w in ways_r}
    from .osm_load import _load_osm_small_roads as _losr
    nodes_s, ways_s = _losr(layout.anchor[0], layout.anchor[1])
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
        ways_r = list(ways_r) + [
            ("S|" + wid, ["S|" + n for n in nrefs], tags)
            for wid, nrefs, tags in ways_s]
    if not ways_r:
        return 0
    # Project nodes to meter space.
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH

    def _to_m(lon: float, lat: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * R * cos0,
                math.radians(lat - lat0) * R)

    def _m_to_ll(x: float, y: float) -> tuple[float, float]:
        return (lat0 + math.degrees(y / R),
                lon0 + math.degrees(x / (R * cos0)))
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
        from shapely.ops import unary_union as _uu8
        _airside_gate_u = _uu8(
            [s.polygon for s in layout.shapes
             if s.polygon is not None and not s.polygon.is_empty
             and s.role in _AIRSIDE_GATE_ROLES])
        if _airside_gate_u.is_empty:
            _airside_gate_u = None
    except _GEOM_EXC:
        _airside_gate_u = None
    HW_TUNNEL_TYPES = {
        "motorway", "trunk", "primary", "secondary",
        "tertiary", "motorway_link", "trunk_link",
        "primary_link", "residential", "service",
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
    # Build node-to-way and way-by-id indices for surface-road walking.
    way_by_id: dict[str, tuple[list[str], dict[str, str]]] = {}
    node_to_ways: dict[str, list[str]] = {}
    for wid, nrefs, tags in ways_r:
        way_by_id[wid] = (nrefs, tags)
        for n in nrefs:
            node_to_ways.setdefault(n, []).append(wid)
    # We walk a generous maximum, then truncate per-portal based
    # on actual DEM at the far end so the resulting grade never
    # exceeds ``max_ramp_grade``.  The truncated length is at
    # least ``ramp_min_length_m`` so even flat-road portals get
    # a recognisable approach.  The planning grade is reduced by
    # 0.005 to leave headroom for the 0.1 m altitude rounding —
    # without it, short segments (e.g. 9 m) could round up to
    # ~4.4 % when the design grade is exactly 4 %.
    grade_safety_margin = 0.005
    plan_grade = max(max_ramp_grade - grade_safety_margin, 1e-3)
    arm_walk_max_m = max(arm_max_length_m,
                         ramp_min_length_m,
                         tunnel_depth_m / plan_grade)
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
    # Helper: orient ``o_nrefs`` so it starts at ``anchor_nid`` and
    # walks AWAY from ``anchor_nid``.  When the anchor is mid-way,
    # picks the longer side.  Returns None if anchor isn't on the way.
    def _orient_away(o_nrefs: list[str],
                      anchor_nid: str) -> list[str] | None:
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
                      length_m: float
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
            t_oriented = _orient_away(tw_nrefs, portal_nid)
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
            refs = _orient_away(o_nrefs, portal_nid)
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
                refs = _orient_away(c_nrefs, last_nid)
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

        if len(pts) >= 2 and cum > 5.0:
            return pts
        if len(pts) >= 2:
            return pts
        return None
    # Build a boundary line (union of all ROLE_BOUNDARY shapes' rings)
    # for the per-portal proximity filter.  Tunnels whose portal lies
    # more than ``max_boundary_dist_m`` from any airport-boundary edge
    # are skipped — they don't affect the airport mesh and the chained
    # surface walk would otherwise emit ramps along urban roads far
    # from the airport.
    boundary_line = None
    try:
        from shapely.geometry import LineString as _LS, MultiLineString
        from shapely.ops import unary_union as _uu
        b_lines = []
        for s in layout.shapes:
            if s.role != ROLE_BOUNDARY:
                continue
            try:
                rcoords = list(s.polygon.exterior.coords)
            except _GEOM_EXC:
                continue
            if len(rcoords) >= 2:
                b_lines.append(_LS(rcoords))
        if b_lines:
            boundary_line = _uu(b_lines)
    except _GEOM_EXC:
        boundary_line = None

    # Collect portal data: (portal_node_id, tunnel_wid, walk_pts,
    # hw_type, apt_elev_at_portal, dem_at_far_end).
    portal_data: list[tuple[str, str, list[tuple[float, float]],
                              str, float, float]] = []
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
    if skip_if_adjacent_road:
        try:
            from shapely.strtree import STRtree as _STRtree
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
                _other_road_tree = _STRtree(
                    [ln for ln, _, _ in _other_road_lines])
        except _GEOM_EXC:
            _other_road_tree = None

    def _tunnel_has_adjacent_road(tw_id2, t_nrefs2) -> bool:
        if _other_road_tree is None:
            return False
        _pts = [nodes_m[n] for n in t_nrefs2 if n in nodes_m]
        if len(_pts) < 2:
            return False
        try:
            _tline = LineString(_pts)
            _buf = _tline.buffer(adjacent_road_dist_m)
        except _GEOM_EXC:
            return False
        _tnodes = set(t_nrefs2)
        for _qi in _other_road_tree.query(_buf):
            _oline, _onodes, _owid = _other_road_lines[int(_qi)]
            if _owid == tw_id2 or (_tnodes & _onodes):
                continue
            try:
                if (_tline.crosses(_oline)
                        or _tline.distance(_oline)
                        < adjacent_road_dist_m):
                    return True
            except _GEOM_EXC:
                continue
        return False

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
        # OLD candidates (big_roads + highway type — the only ways the
        # emitter saw before 2026-06-12) keep the original behaviour
        # verbatim: no new gates (SPJC's user-approved tunnels emit
        # bit-identically).  NEW candidates (small_roads bores, rail)
        # carry the gates below — they widened the input enough to
        # surface the dead-boundary-gate strays at KPHL.
        _is_new_cand = not (tw_id in _big_way_ids
                            and hw in HW_TUNNEL_TYPES)
        if len(t_nrefs) < 2:
            continue
        # Skip OSM way IDs already handled by the through-
        # airport depressed-road emit (which produces a single
        # uniform depression instead of per-bridge ramps).
        if tw_id in excluded:
            continue
        # Skip tunnels running under / alongside other roads — their
        # surface walk traces a dense interchange whose ramps overlap
        # (user 2026-06-12, LMML).  Both portals are skipped.
        if _tunnel_has_adjacent_road(tw_id, t_nrefs):
            _n_adj_skip += 1
            continue
        for portal_idx in (0, len(t_nrefs) - 1):
            portal_nid = t_nrefs[portal_idx]
            if portal_nid not in nodes_m:
                continue
            # Airport-proximity gate against the AIRSIDE PAVEMENT
            # union (user 2026-06-12, KPHL): the boundary_line gate
            # below is DEAD in production — the boundary ribbon is
            # emitted AFTER this pass, so ROLE_BOUNDARY is empty and
            # far portals sailed through (caps at 19-62 km once
            # small_roads/rail widened the candidate set; the latent
            # bug never fired from big_roads alone).  The pavement
            # union exists at this point and scales with the airport.
            if _is_new_cand and _airside_gate_u is not None:
                _ppx, _ppy = nodes_m[portal_nid]
                try:
                    if _airside_gate_u.distance(
                            Point(_ppx, _ppy)) > max_boundary_dist_m:
                        continue
                except _GEOM_EXC:
                    pass
            if boundary_line is not None:
                px, py = nodes_m[portal_nid]
                try:
                    if boundary_line.distance(
                            Point(px, py)) > max_boundary_dist_m:
                        continue
                except _GEOM_EXC:
                    pass
            walk = _walk_surface(portal_nid, tw_id, arm_walk_max_m)
            if walk is None or len(walk) < 2:
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
            apt_elev = _airport_elevation_at(*portal_xy)
            if apt_elev is None:
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
            # Truncate walk to the OK length (or to last vertex
            # reached if we never satisfied the grade — best
            # effort, the resulting ramp will be the gentlest
            # achievable on the available roadway).
            walk = kept_pts
            far_xy = walk[-1]
            try:
                far_lat, far_lon = _m_to_ll(*far_xy)
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
            portal_data.append(
                (portal_nid, tw_id, walk, hw,
                 float(apt_elev), float(far_dem), _is_new_cand))
    if _n_adj_skip:
        try:
            UI.vprint(1,
                f"  [pav-builder] skipped {_n_adj_skip} tunnel(s) with "
                f"an adjacent/crossing road (ramps not modelled).")
        except _GEOM_EXC:
            pass
    if not portal_data:
        return 0
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
    # Per-cluster: build cap + arm walls + ramp chain.
    exclusion_zones: list[Polygon] = []
    n_emitted = 0
    half_wall_w = retaining_wall_width_m / 2.0
    for cl in clusters:
        # All portals in cluster share approximately the same
        # location.  Use the first portal's walk as the canonical
        # arm path; combine widths for divided highways.
        head = portal_data[cl[0]]
        (portal_nid, _wid_unused, walk_pts, hw_type, apt_elev,
         far_dem, _head_new) = head
        _cl_all_new = all(portal_data[k][6] for k in cl)
        if len(walk_pts) < 2:
            continue
        # Per-OSM-highway-type carriageway width (user 2026-05-03):
        # was a fixed 22 m default; now varies by classification
        # so secondary tunnels are ~half the width of trunk tunnels.
        carriage_w = _carriageway_width_for(
            hw_type, carriageway_width_m)
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
            continue
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
            continue
        first_dir = (first_seg[0] / first_len,
                     first_seg[1] / first_len)
        first_perp = (-first_dir[1], first_dir[0])
        spans = []
        for k in cl:
            ni = portal_data[k][0]
            if ni not in nodes_m:
                continue
            p = nodes_m[ni]
            spans.append(
                (p[0] - walk_pts[0][0]) * first_perp[0]
                + (p[1] - walk_pts[0][1]) * first_perp[1])
        cluster_span = max(spans) - min(spans) if spans else 0.0
        cluster_perp_offset = (
            (max(spans) + min(spans)) / 2.0 if spans else 0.0)
        combined_half = half_carriage + 0.5 * cluster_span
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
                from shapely.ops import unary_union as _uu9
                if _uu9(exclusion_zones).buffer(2.0).contains(
                        Point(walk_pts[0])):
                    continue
        except _GEOM_EXC:
            pass
        # 1) Cap wall AT the portal cluster's centroid, perpendicular
        #    to the first segment.  The cap's centre line passes
        #    through the cluster centroid (so divided-highway
        #    tunnels are centered between the carriageways, user
        #    2026-05-03), its width spans the combined carriageways
        #    + 2 × wall_gap, its thickness is
        #    retaining_wall_width_m.
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

            for i in range(n_c - 1):
                p_a = chain_pts[i]
                p_b = chain_pts[i + 1]
                d_a = c_cums[i]
                d_b = c_cums[i + 1]
                seg_len = d_b - d_a
                if seg_len < 0.5:
                    continue
                frac_a = d_a / c_total
                frac_b = d_b / c_total
                e_a = (1 - frac_a) * e_lo_c + frac_a * e_hi_c
                e_b = (1 - frac_b) * e_lo_c + frac_b * e_hi_c
                wall_top = apt_elev
                wall_thresh = wall_top - 0.05
                seg_e_lo = min(e_a, e_b)
                seg_e_hi = max(e_a, e_b)
                if seg_e_lo >= wall_thresh:
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
                        if abs(eh - el) >= 0.1:
                            layout.shapes.append(BuiltShape(
                                polygon=rp,
                                role=ROLE_TUNNEL_RAMP,
                                ref="tunnel_ramp",
                                altitude_high=round(eh, 1),
                                altitude_low=round(el, 1)))
                        else:
                            layout.shapes.append(BuiltShape(
                                polygon=rp,
                                role=ROLE_TUNNEL_RAMP,
                                ref="tunnel_ramp",
                                altitude=round(
                                    0.5 * (eh + el), 1)))
                        exclusion_zones.append(rp)
                except _GEOM_EXC:
                    pass

        def _emit_fork_throat(throat_pts, throat_half, e_throat,
                              wall_alt, arms):
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
            if os.environ.get("O4_FORK_DEBUG"):
                UI.vprint(0, f"  [fork] F={F} t_dir={t_dir} "
                    f"combined_half={throat_half:.1f} narms={len(arm_info)}")
                for a in arm_info:
                    UI.vprint(0, f"    arm E={a['E']} half={a['half']:.1f} "
                        f"adv={a['adv']:.1f} cL={a['cL']} cR={a['cR']}")
                UI.vprint(0, f"    ring={[(round(p[0],1),round(p[1],1)) for p in ring]}")

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
            na = [round(e_throat, 1)] * (len(ring) + 1)
            layout.shapes.append(BuiltShape(
                polygon=poly, role=ROLE_TUNNEL_RAMP,
                ref="tunnel_ramp", node_altitudes=na))
            exclusion_zones.append(poly)

            # Walls: each flagged perimeter edge, offset OUTWARD (away
            # from the fan centroid) by wall_gap, thickness
            # retaining_wall_width_m, flat at DEM.  Honour the
            # wall_thresh contract — no wall where the landing already
            # sits at/above DEM.
            if e_throat < wall_alt - 0.05:
                cx, cy = poly.centroid.x, poly.centroid.y
                w = retaining_wall_width_m
                for i in range(len(ring)):
                    if not wall_edge[i]:
                        continue
                    p_a = ring[i]
                    p_b = ring[(i + 1) % len(ring)]
                    sx, sy = p_b[0] - p_a[0], p_b[1] - p_a[1]
                    sl = math.hypot(sx, sy)
                    if sl < 0.5:
                        continue
                    nx, ny = -sy / sl, sx / sl
                    mx, my = 0.5 * (p_a[0] + p_b[0]), 0.5 * (p_a[1] + p_b[1])
                    if (mx - cx) * nx + (my - cy) * ny < 0.0:
                        nx, ny = -nx, -ny
                    corners = [
                        (p_a[0] + nx * wall_gap_m, p_a[1] + ny * wall_gap_m),
                        (p_b[0] + nx * wall_gap_m, p_b[1] + ny * wall_gap_m),
                        (p_b[0] + nx * (wall_gap_m + w),
                         p_b[1] + ny * (wall_gap_m + w)),
                        (p_a[0] + nx * (wall_gap_m + w),
                         p_a[1] + ny * (wall_gap_m + w)),
                    ]
                    try:
                        wp = Polygon(corners)
                        if not wp.is_valid:
                            wp = wp.buffer(0)
                        if (wp.geom_type == "Polygon" and not wp.is_empty
                                and wp.area > 0.5):
                            layout.shapes.append(BuiltShape(
                                polygon=wp, role=ROLE_RETAINING_WALL,
                                ref="tunnel_wall",
                                altitude=round(wall_alt, 1)))
                            exclusion_zones.append(wp)
                    except _GEOM_EXC:
                        pass
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
                s = 10.0
                while s < probe_max:
                    pts_at = [_point_at(w, c, s)
                              for (_k, w, c) in member_chains]
                    spread = max(
                        math.hypot(p1[0] - p2[0], p1[1] - p2[1])
                        for x1, p1 in enumerate(pts_at)
                        for p2 in pts_at[x1 + 1:])
                    if spread > cluster_span + 8.0:
                        s_div = s
                        break
                    s += 5.0
                if s_div is not None and (probe_max - s_div) < 10.0:
                    s_div = None     # fork too close to the end

        if s_div is None:
            _emit_chain(walk_pts, combined_half,
                        elev_low, elev_high, True)
        else:
            # Shared throat on the (centred) canonical walk.
            throat = [walk_pts[0]]
            for i in range(1, len(walk_pts)):
                if cum_dists[i] < s_div:
                    throat.append(walk_pts[i])
                else:
                    break
            throat.append(_point_at(walk_pts, cum_dists, s_div))
            e_div = (elev_low + (elev_high - elev_low)
                     * (s_div / total_walk if total_walk > 0
                        else 0.0))
            _cl_start_idx = len(layout.shapes)
            _emit_chain(throat, combined_half,
                        elev_low, e_div, True)
            # Per-member branches along their OWN alignments —
            # widest first; each later branch starts where it CLEARS
            # the corridors of the ones already emitted (the fork
            # crotch otherwise overlaps ramp-on-ramp).
            ordered = []
            for k, w_k, c_k in member_chains:
                hw_k = portal_data[k][3]
                half_k = 0.5 * _carriageway_width_for(
                    hw_k, carriageway_width_m)
                ordered.append((half_k, k, w_k, c_k))
            ordered.sort(key=lambda t: -t[0])
            # Every branch must clear the THROAT corridor too — a
            # member's own arc-s_div point can sit slightly inside
            # it (the throat runs on the canonical alignment), which
            # lapped the branch's first ramp onto the throat's last.
            prior: list = []          # (LineString, half)
            try:
                prior.append((LineString(throat), combined_half))
            except _GEOM_EXC:
                pass
            # Collect the (advanced) arm chains first; the fork throat
            # (when gated on) must see every arm's fork-side start
            # BEFORE the arms emit, so the bridging polygon + Y-tracing
            # walls land between the bore and the arms.  Gate OFF: the
            # arm geometry computed here is identical to the legacy
            # inline emit (same prior-clearance order), so emitting them
            # in the loop below is byte-identical.
            arm_specs: list = []      # (branch_pts, half_k, far_k)
            for half_k, k, w_k, c_k in ordered:
                branch = [_point_at(w_k, c_k, s_div)]
                for i in range(1, len(w_k)):
                    if c_k[i] > s_div:
                        branch.append(w_k[i])
                if len(branch) < 2:
                    continue
                if prior:
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
            # Fork throat junction (bridge bore→arms, trace the Y) —
            # before the arms so it abuts the bore's far edge.
            if TUNNEL_FORK_THROAT and len(arm_specs) >= 2:
                _emit_fork_throat(throat, combined_half, e_div,
                                  apt_elev, arm_specs)
            for branch, half_k, far_k in arm_specs:
                _emit_chain(branch, half_k, e_div, far_k, False)
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
                    from shapely.ops import unary_union as _uu7
                    ramp_u9 = _uu7(ramps9).buffer(0.3)
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
        n_emitted += 1
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
    if _airside_gate_u is not None:
        _kept9 = []
        _n_clip = 0
        for _k9, s9 in enumerate(layout.shapes):
            if (id(s9) not in _pre_emit_ids
                    and getattr(s9, "ref", "") in
                    ("tunnel_cap", "tunnel_wall", "tunnel_ramp")
                    and s9.polygon is not None
                    and not s9.polygon.is_empty):
                try:
                    if s9.polygon.intersection(
                            _airside_gate_u).area > 0.25:
                        _n_clip += 1
                        continue
                except _GEOM_EXC:
                    pass
            _kept9.append(s9)
        if _n_clip:
            layout.shapes = _kept9
            try:
                UI.vprint(1,
                    f"  [pav-builder] dropped {_n_clip} tunnel "
                    f"piece(s) under pavement (covered stretch — no "
                    f"visible structure).")
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
                   if id(s9) not in _pre_emit_ids
                   and getattr(s9, "ref", "") == "tunnel_ramp"
                   and s9.polygon is not None
                   and not s9.polygon.is_empty]
    if _ramp_polys:
        try:
            from shapely.ops import unary_union as _uuR
            _ramp_u = _uuR(_ramp_polys)
        except _GEOM_EXC:
            _ramp_u = None
        if _ramp_u is not None and not _ramp_u.is_empty:
            _keptW = []
            _n_wclip = 0
            for s9 in layout.shapes:
                if (id(s9) not in _pre_emit_ids
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
    """
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
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH

    def _to_m(lon_v: float, lat_v: float) -> tuple[float, float]:
        return (math.radians(lon_v - lon0) * R * cos0,
                math.radians(lat_v - lat0) * R)
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
    from .pipeline import _load_osm_airports, _load_osm_big_roads
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
        lat0, lon0 = layout.anchor
        cos0 = math.cos(math.radians(lat0))

        def _to_m(lon: float, lat: float) -> tuple[float, float]:
            return (math.radians(lon - lon0) * R_EARTH * cos0,
                    math.radians(lat - lat0) * R_EARTH)

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
    """
    from .pipeline import _load_osm_airports, _load_osm_big_roads
    # Collect underpass surfaces.
    bridge_shapes = [s for s in layout.shapes
                     if getattr(s, "is_bridge", False)
                     and s.polygon is not None
                     and not s.polygon.is_empty]
    if not bridge_shapes:
        return 0
    nodes_r, ways_r = _load_osm_big_roads(
        layout.anchor[0], layout.anchor[1])
    if not ways_r:
        return 0
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH
    def _to_m(lon: float, lat: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * R * cos0,
                math.radians(lat - lat0) * R)
    def _m_to_ll(x: float, y: float) -> tuple[float, float]:
        return (lat0 + math.degrees(y / R),
                lon0 + math.degrees(x / (R * cos0)))
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
        return 0
    n_processed = 0
    for s in bridge_shapes:
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
    return n_processed


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

    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH

    def _to_m(lon: float, lat: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * R * cos0,
                math.radians(lat - lat0) * R)

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

    grade_safety_margin = 0.005
    plan_grade = max(max_ramp_grade - grade_safety_margin, 1e-3)
    arm_walk_max_m = max(arm_max_length_m, ramp_min_length_m,
                         depression_depth_m / plan_grade)

    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH

    def _to_m(lon: float, lat: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * R * cos0,
                math.radians(lat - lat0) * R)

    def _m_to_ll(x: float, y: float) -> tuple[float, float]:
        return (lat0 + math.degrees(y / R),
                lon0 + math.degrees(x / (R * cos0)))

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

            for i in range(n_w - 1):
                d_a = cum_dists[i]
                d_b = cum_dists[i + 1]
                seg_len = d_b - d_a
                if seg_len < 0.5:
                    continue
                frac_a = d_a / total_walk if total_walk > 0 else 0.0
                frac_b = d_b / total_walk if total_walk > 0 else 0.0
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
                        altitude_high=round(eh, 1),
                        altitude_low=round(el, 1)))
                else:
                    layout.shapes.append(BuiltShape(
                        polygon=rp,
                        role=ROLE_TUNNEL_RAMP,
                        ref="depressed_approach",
                        altitude=round(0.5 * (eh + el), 1)))
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
