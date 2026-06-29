"""Per-building route-feasibility elevation (user model, 2026-06-22).

A building that touches airside pavement is seated FLAT at the elevation
closest to its DEM that keeps it reachable WITHIN GRADE from EVERY runway
threshold along the real taxi route — the "heaviest anchor" the rest of the
network then grades to (docs/taxi_centerline_grading_plan.md §9).

The route to a building (user's definition, validated on CYXY):

  1. a PERPENDICULAR from the building centroid to the NEAREST taxi
     centerline (named or not — geometry only).  The part of that
     perpendicular inside the taxiway-width corridor climbs at the
     taxiway's own cap; the part beyond (real apron) at the apron cap (1%).
  2. from the foot point, the taxi-route to each threshold along the
     centerline graph at the PER-EDGE per-letter caps (narrow A/B = 3 %,
     wide C–F = 1.5 %) — including the partial first edge from the foot
     point to its graph node (the bit that snapping to the node would drop).

The feasibility BAND is the intersection over ALL thresholds:

  ceiling = min_t ( thr_elev_t + climb_t )
  floor   = max_t ( thr_elev_t − climb_t )

and the seated level is ``clamp(DEM, floor, ceiling)`` — closest to DEM,
could be above or below it.  Buildings NOT touching airside pavement are
omitted (the caller leaves them at their DEM).

This routes on the SAME `TaxiRouteGraph.edge_cap` the reach-bands use, so a
narrow code-A/B arm contributes 3 %.  Unnamed routes carry their real apt.dat
size via the synthetic ``~U`` name (apt_dat_reader.unnamed_edge_component_names),
so the per-letter cap is correct without any geometry recovery.
"""

from __future__ import annotations

import heapq
import math
import os
from typing import Callable, Dict, List, Tuple

from auto_patch.config import (
    BUILDING_FRONTAGE_CORRIDOR_M, BUILDING_FULL_FRONTAGE,
    BUILDING_FULL_FRONTAGE_AREA_M2, TAXI_MAX_GRADE, VISIBLE_CHORD_CONNECT,
)
from auto_patch.layout import (
    ROLE_APRON, ROLE_BUILDING, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
)

__all__ = ["building_feasible_levels", "reach_band_unified",
           "runway_edge_anchors"]

# A taxi centerline ENDPOINT counts as a runway CONTACT (a real route entry onto
# the runway) when it lies within this distance of the runway POLYGON EDGE (or
# inside the polygon).  Measured against the runway EDGE, NOT its sparse ring
# VERTICES (user 2026-06-24: EVERY taxiway that touches the runway must anchor —
# a taxiway meeting a long runway edge mid-span is far from any ring VERTEX but
# ~0 from the EDGE; the old vertex-distance test missed 37/51 HECA contacts →
# detour-credited corridors → spine ramps).
_CONTACT_EDGE_TOL_M = 12.0

# Pavement a building must touch to count as airside-served (else → DEM).
_AIRSIDE_ROLES = frozenset({
    ROLE_APRON, ROLE_JUNCTION, ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
    ROLE_SECONDARY_PARALLEL, ROLE_STUB, ROLE_CROSS_CONNECTOR,
})
_APRON_CAP = 0.01          # apron grade outside the taxiway-width corridor
_ENTRY_CAP = 0.015         # runway-threshold → nearest taxi node stub
_TAXI_HALF_W_M = 7.5       # taxiway half-width corridor (perp split point)
_TOUCH_TOL_M = 2.0         # building↔airside distance to count as "touching"
_INF = float("inf")


_VIS_BUFFER_M = 0.5        # bridge weld-seam slivers between abutting shapes
_VIS_ON_PAV_FRAC = 0.97    # chord counts as visible if ≥ this fraction is paved


def _pavement_visibility(layout):
    """Prepared airside-pavement geometry (∪ building pads) for the visible-chord
    test — a building→taxiway connection is legal only if its chord stays within
    pavement (the user's rule: never taxi across grass / a service road; a spine
    is a centerline, so apron pavement counts).  Building pads are included so a
    chord may start inside the building's own pad.  Buffered slightly to bridge
    numerical weld-seam slivers between abutting shapes."""
    from shapely.ops import unary_union
    from shapely.prepared import prep
    polys = [s.polygon for s in layout.shapes
             if (s.role in _AIRSIDE_ROLES or s.role == ROLE_BUILDING)
             and s.polygon is not None and not s.polygon.is_empty]
    if not polys:
        return None
    try:
        u = unary_union(polys).buffer(_VIS_BUFFER_M)
        return prep(u)
    except Exception:                                      # pragma: no cover
        return None


def _nearest_visible_centerline(c, cls, vis):
    """The nearest centerline to point ``c`` whose connecting chord stays within
    pavement (``vis``).  Falls back to the straight-line nearest if none is
    visible (e.g. a building wholly off pavement — the caller's touch test has
    already gated that out)."""
    from shapely.geometry import LineString
    from shapely.ops import nearest_points
    for ln in sorted(cls, key=lambda L: L.distance(c)):
        foot = nearest_points(ln, c)[0]
        chord = LineString([(c.x, c.y), (foot.x, foot.y)])
        if chord.length < 1e-6 or vis.contains(chord):
            return ln
        # tolerate tiny seam gaps: accept when ≥ _VIS_ON_PAV_FRAC is paved
        try:
            inside = chord.intersection(vis.context).length
            if inside / chord.length >= _VIS_ON_PAV_FRAC:
                return ln
        except Exception:                                  # pragma: no cover
            pass
    return min(cls, key=lambda L: L.distance(c))


def _build_skeleton_band(layout, G):
    """EDGE-SKELETON reach for NO-CENTERLINE pavement (user 2026-06-28).

    A taxiway/apron that has no taxi centerline never enters the centerline spine,
    so :func:`reach_band_unified`'s centerline path returns ``None`` for it and the
    apron it feeds becomes an unreachable island (no band → ``route_reach`` flags,
    feeders land at incompatible DEM levels).  This builds a SECOND reach over the
    pavement's WELDED EDGE SKELETON — ``G.edges`` (abutting shapes share EXACT node
    indices, so connectivity needs no perpendicular tolerance) unioned with the
    centerline ``spine_adj`` — anchored at BOTH the centerline→runway joins
    (``G.runway_anchor``) AND every pavement node COINCIDENT with a runway segment
    (a no-centerline taxiway reaches the runway through a CROSSING the
    centerline-endpoint anchor misses).  Returns ``band(x, y) -> (floor, ceiling)
    | None`` = the nearest skeleton node's reach interval, widened by the apron cap
    over the offset to it.  Used ONLY where the centerline band is ``None``, so
    centerlined airports are unchanged."""
    from shapely.geometry import Point
    from shapely.strtree import STRtree
    from auto_patch.layout import ROLE_RUNWAY
    from auto_patch.grade_graph_validate import _shape_elevs, _open_ring

    def _d(a, b):
        pa, pb = G.pos.get(a), G.pos.get(b)
        return math.hypot(pa[0] - pb[0], pa[1] - pb[1]) if pa and pb else 1e-3

    # Augmented adjacency: centerline spine ∪ welded edge skeleton.
    adj: dict = {}

    def _add(a, b, w):
        adj.setdefault(a, []).append((b, w))
        adj.setdefault(b, []).append((a, w))

    for u, nbrs in G.spine_adj.items():
        for (v, b) in nbrs:
            _add(u, v, b)
    for (a, b, cap, _sp) in G.edges:
        if a in G.pos and b in G.pos:
            _add(a, b, cap.at(_d(a, b), 0.0))

    # Anchors: centerline→runway joins + runway-coincident pavement nodes.
    anchor_elev = dict(G.runway_anchor)
    pos_to_idx = {(round(x, 3), round(y, 3)): i for (i, (x, y)) in G.pos.items()}
    for s in layout.shapes:
        if (s.role != ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        elevs = _shape_elevs(s, len(ring))
        if elevs is None:
            continue
        for (x, y), e in zip(ring, elevs):
            i = pos_to_idx.get((round(x, 3), round(y, 3)))
            if i is not None and e is not None and i not in anchor_elev:
                anchor_elev[i] = float(e)
    if not anchor_elev:
        return lambda x, y: None

    def _capdist(src):
        dist = {src: 0.0}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, _INF):
                continue
            for (v, budget) in adj.get(u, ()):
                nd = d + budget
                if nd < dist.get(v, _INF):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    node_floor: dict = {}
    node_ceil: dict = {}
    for k, ae in anchor_elev.items():
        if k not in adj:
            continue
        for v, dd in _capdist(k).items():
            node_ceil[v] = min(node_ceil.get(v, _INF), ae + dd)
            node_floor[v] = max(node_floor.get(v, -_INF), ae - dd)
    if not node_ceil:
        return lambda x, y: None

    bidx = list(node_ceil.keys())
    bpts = [Point(*G.pos[i]) for i in bidx if i in G.pos]
    bidx = [i for i in bidx if i in G.pos]
    if not bpts:
        return lambda x, y: None
    tree = STRtree(bpts)

    def band(x, y):
        try:
            j = bidx[int(tree.nearest(Point(x, y)))]
        except Exception:                                     # pragma: no cover
            return None
        off = math.hypot(G.pos[j][0] - x, G.pos[j][1] - y)
        slack = _APRON_CAP * off
        return (node_floor[j] - slack, node_ceil[j] + slack)

    return band


def reach_band_unified(layout, G):
    """The reach band computed on THE unified grade graph — the SAME graph the
    spine solves on and the validator checks (user 2026-06-27, "stop building the
    same thing in different ways").

    Reachability is a cap-Dijkstra over ``G.spine_adj`` from ``G.runway_anchor``
    (the exact nodes + elevations the spine solve pins), so the ceiling is the
    spine's ACHIEVABLE level and is cap-consistent along the spine BY CONSTRUCTION
    — no separate route graph, no per-node inconsistency, no ceiling-consistency
    bridge.  Geometry of the perpendicular foot still uses the taxi centerlines (an
    accurate perp), but the foot's reachable elevation comes from the nearest
    unified spine node.  The band contract is
    ``band(x, y) -> (floor, ceiling) | None``."""
    import heapq
    from shapely.geometry import Point
    from shapely.strtree import STRtree

    if not getattr(G, "runway_anchor", None) or not getattr(G, "spine_adj", None):
        return lambda x, y: None

    def _capdist(src):
        dist = {src: 0.0}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, _INF):
                continue
            for (v, budget) in G.spine_adj.get(u, ()):
                nd = d + budget
                if nd < dist.get(v, _INF):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    anchors = [(float(ae), _capdist(k)) for (k, ae) in G.runway_anchor.items()]
    if not anchors:
        return lambda x, y: None

    # Unified spine-node positions for the perp-foot → reachable-node lookup.
    sidx = [i for i in G.spine_adj if i in G.pos]
    if not sidx:
        return lambda x, y: None
    spts = [Point(*G.pos[i]) for i in sidx]
    tree = STRtree(spts)

    def _nn(pt):
        try:
            return sidx[int(tree.nearest(Point(pt[0], pt[1])))]
        except Exception:                                     # pragma: no cover
            return None

    # Per-centerline longitudinal cap from its OWN ICAO code letter (apt.dat
    # row-1202) — the SAME per-letter cap the spine edges carry.  Used for the
    # foot climb (along the serving centerline + the taxiway-width perp) so the
    # band credits a code-A/B taxiway at its real 3 %, CONSISTENTLY at every query
    # point.  The old spine-edge lookup read the cap off the edge between the two
    # nearest spine nodes to the foot's centerline segment, falling back to 1.5 %
    # whenever those nodes were not a DIRECT edge — which happens on any long
    # apt.dat segment — so the SAME taxiway was credited 3 % at a building
    # frontage but only 1.5 % at an apron 60 m further along it: the building seat
    # and the route-band check then disagreed though both go through this one band
    # (CYXY A2 = code B, a 457 m segment → 118 false apron ceil flags).
    from auto_patch.config import taxi_grade_cap_for_letter
    letters = getattr(layout, "apt_taxi_letters", None) or {}
    cls_cap = [(ln, float(taxi_grade_cap_for_letter(letters.get(n))))
               for (ln, n) in (getattr(layout, "apt_taxi_centerlines", None)
                               or [])
               if ln is not None and not ln.is_empty
               and not str(n or "").upper().startswith("SVC")]
    if not cls_cap:
        return lambda x, y: None
    cls = [ln for (ln, _c) in cls_cap]
    cap_of = {id(ln): c for (ln, c) in cls_cap}
    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None

    # EDGE-SKELETON fallback (user 2026-06-28): no-centerline pavement returns
    # None below; the skeleton band reaches it over the welded edge graph +
    # runway-contact anchors.  Used ONLY where the centerline path is None, so
    # centerlined airports are unchanged.  Gate O4_SKELETON_REACH=0 disables.
    _skel_band = (_build_skeleton_band(layout, G)
                  if os.environ.get("O4_SKELETON_REACH", "1") == "1" else None)

    def _fallback(x, y):
        return _skel_band(x, y) if _skel_band is not None else None

    def band(x, y):
        c = Point(x, y)
        ln = (_nearest_visible_centerline(c, cls, vis) if vis is not None
              else min(cls, key=lambda L: L.distance(c)))
        perp = c.distance(ln)
        coords = list(ln.coords)
        sp = ln.project(c)
        acc = 0.0
        A = B = None
        for i in range(len(coords) - 1):
            seg_len = math.hypot(coords[i + 1][0] - coords[i][0],
                                 coords[i + 1][1] - coords[i][1])
            if acc - 1e-6 <= sp <= acc + seg_len + 1e-6:
                A = (coords[i], sp - acc)
                B = (coords[i + 1], (acc + seg_len) - sp)
                break
            acc += seg_len
        if A is None:
            A = (coords[0], 0.0)
            B = (coords[-1], ln.length)
        kA = _nn(A[0])
        kB = _nn(B[0])
        if kA is None and kB is None:
            return _fallback(x, y)
        ecap = cap_of.get(id(ln), TAXI_MAX_GRADE)
        perp_climb = (ecap * min(perp, _TAXI_HALF_W_M)
                      + _APRON_CAP * max(0.0, perp - _TAXI_HALF_W_M))
        floor, ceil = -_INF, _INF
        for (ae, cdm) in anchors:
            cands = []
            if kA in cdm:
                cands.append(cdm[kA] + ecap * A[1])
            if kB in cdm:
                cands.append(cdm[kB] + ecap * B[1])
            if not cands:
                continue
            budget = min(cands) + perp_climb
            ceil = min(ceil, ae + budget)
            floor = max(floor, ae - budget)
        if ceil >= _INF:
            return _fallback(x, y)
        return (floor, ceil)

    return band


def _has_visible_corridor(px, py, cls, vis, max_m):
    """True when a taxi corridor lies within ``max_m`` of ``(px, py)`` AND a chord
    from the point to that corridor's spine stays on pavement (a VISIBLE chord —
    the user's gate, 2026-06-27).  ``vis`` None (visibility disabled) → distance
    gate only."""
    from shapely.geometry import Point, LineString
    from shapely.ops import nearest_points
    c = Point(px, py)
    cand = [L for L in cls if L.distance(c) <= max_m]
    if not cand:
        return False
    if vis is None:
        return True
    for ln in sorted(cand, key=lambda L: L.distance(c)):
        foot = nearest_points(ln, c)[0]
        chord = LineString([(px, py), (foot.x, foot.y)])
        if chord.length < 1e-6 or vis.contains(chord):
            return True
        try:                                 # tolerate tiny weld-seam gaps
            inside = chord.intersection(vis.context).length
            if inside / chord.length >= _VIS_ON_PAV_FRAC:
                return True
        except Exception:                                  # pragma: no cover
            pass
    return False


def _frontage_band(poly, band, cls, vis, max_corridor_m):
    """Intersect the route-feasibility ``band`` over the building's ENTIRE
    qualifying FRONTAGE (user 2026-06-27, large buildings only).

    A frontage SAMPLE (every ring vertex + each edge midpoint) qualifies when a
    taxi corridor lies within ``max_corridor_m`` AND a visible on-pavement chord
    reaches that corridor's spine (:func:`_has_visible_corridor`) — i.e. every
    SIDE flanked by a taxi route, not only the apron the building abuts.  The
    band is sampled at every qualifying point and the feasible interval is the
    INTERSECTION — ``(max floor, min ceiling)`` — so the seated flat level keeps
    EVERY such frontage point gradeable to the spine at ≤1 % (not just the central
    chord).  Returns ``(floor, ceiling)`` or ``None`` when no side qualifies
    (caller falls back to the central chord)."""
    ring = list(poly.exterior.coords)
    floor, ceil = -_INF, _INF
    got = False
    for i in range(len(ring) - 1):
        ax, ay = ring[i]
        bx, by = ring[i + 1]
        mx, my = 0.5 * (ax + bx), 0.5 * (ay + by)
        for (px, py) in ((ax, ay), (mx, my), (bx, by)):
            if not _has_visible_corridor(px, py, cls, vis, max_corridor_m):
                continue
            bb = band(px, py)
            if bb is None:
                continue
            floor = max(floor, bb[0])
            ceil = min(ceil, bb[1])
            got = True
    return (floor, ceil) if got else None


def building_feasible_levels(
        layout,
        runway_pts_xyz: List[Tuple[float, float, float]],
        dem_sampler: Callable[[float, float], "float | None"],
        band=None,
) -> Dict[int, float]:
    """Return ``{id(building_shape): seated_level_m}`` for every
    ``ROLE_BUILDING`` that touches airside pavement.

    ``runway_pts_xyz``: ``[(x_m, y_m, elev_m)]`` every runway ring vertex at its
    solved elevation (the runway-edge anchors — see :func:`runway_edge_anchors`).
    ``dem_sampler(x, y)``: DEM (m) at a layout-local point, or None.  The level
    is ``clamp(DEM, floor, ceiling)`` from the shared route-feasibility band — if
    DEM is not reachable within grade from every runway route, the level is
    pulled into the band (the building is adjusted to be feasible).  Buildings
    not touching airside pavement are omitted (the caller keeps them at DEM).

    ``band``: the pre-built unified-graph band from :func:`reach_band_unified`
    (required) — buildings are placed on the SAME graph the spine is graded on
    (the single graph; they then agree by construction)."""
    from shapely.ops import unary_union

    if band is None:
        raise ValueError(
            "building_feasible_levels requires a prebuilt band "
            "(reach_band_unified); the legacy route-graph sampler was removed")
    polys = [s.polygon for s in layout.shapes
             if s.role in _AIRSIDE_ROLES and s.polygon is not None
             and not s.polygon.is_empty]
    if not polys:
        return {}
    airside = unary_union(polys)

    # Buildings ≥ this footprint must clear their ENTIRE frontage, not just a
    # single central chord (user 2026-06-27); small buildings keep the centroid
    # chord.  Gate off → central chord for every building (legacy, byte-identical).
    full_frontage = (BUILDING_FULL_FRONTAGE
                     and os.environ.get("O4_BUILDING_FULL_FRONTAGE", "1") == "1")
    # Taxi corridors + pavement-visibility for the frontage qualifier (a side
    # grades at 1 % only when a corridor is within range AND visibly chord-reachable).
    cls = vis = None
    if full_frontage:
        cls = [ln for (ln, n) in
               (getattr(layout, "apt_taxi_centerlines", None) or [])
               if ln is not None and not ln.is_empty
               and not str(n or "").upper().startswith("SVC")]
        vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None

    out: Dict[int, float] = {}
    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty):
            continue
        if airside.is_empty or s.polygon.distance(airside) > _TOUCH_TOL_M:
            continue                            # not airside-served → DEM
        c = s.polygon.centroid
        # LARGE building → intersect the band over the whole frontage; SMALL (or no
        # qualifying frontage side) → the single central chord from the centroid.
        b = None
        if (full_frontage and cls
                and s.polygon.area >= BUILDING_FULL_FRONTAGE_AREA_M2):
            b = _frontage_band(s.polygon, band, cls, vis,
                               BUILDING_FRONTAGE_CORRIDOR_M)
        if b is None:
            b = band(c.x, c.y)
        if b is None:
            continue
        floor, ceil = b
        de = dem_sampler(c.x, c.y)
        if de is None:
            continue
        if floor > ceil:                        # infeasible → midpoint (adjust)
            out[id(s)] = 0.5 * (floor + ceil)
        else:
            out[id(s)] = min(max(de, floor), ceil)
    return out
