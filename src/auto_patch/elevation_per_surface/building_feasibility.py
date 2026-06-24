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
from typing import Callable, Dict, List, Tuple

from auto_patch.config import TAXI_MAX_GRADE, VISIBLE_CHORD_CONNECT
from auto_patch.layout import (
    ROLE_APRON, ROLE_BUILDING, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
)

__all__ = ["building_feasible_levels", "reach_band_sampler",
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


def reach_band_sampler(layout, runway_pts_xyz):
    """The shared taxi-route FEASIBILITY-BAND sampler used by BOTH the building
    levels and the spine climb (the one model, user 2026-06-23).

    ``runway_pts_xyz``: ``[(x, y, elev)]`` every runway ring vertex at its solved
    elevation.  Returns ``band(x, y) -> (floor, ceiling) | None``: the range a
    point may sit at while reachable within grade from EVERY runway it
    taxi-connects to.

    Anchors = taxiway↔runway CONTACTS — EVERY taxi centerline that touches a
    runway provides one (user 2026-06-24).  A contact is a centerline vertex
    INSIDE a runway polygon, or a centerline ENDPOINT within
    ``_CONTACT_EDGE_TOL_M`` of a runway EDGE; anchored at the runway elevation
    there.  Measured against the runway POLYGON (not its sparse ring vertices, a
    test that missed taxiways meeting a long edge mid-span).  Each contact's
    reach is measured along the taxi route with FOOT edge projection and
    intersected over ALL contacts (the most-deviating route binds: ``ceiling =
    min(elev_a + budget_a)``).  The building/spine entry to the route uses a
    VISIBLE CHORD (``VISIBLE_CHORD_CONNECT``) — the nearest centerline reachable
    without leaving pavement, not the straight-line nearest."""
    from shapely.geometry import Point

    from auto_patch.taxi_routing import shared_taxi_route_graph
    G = shared_taxi_route_graph(layout)
    if not getattr(G, "coord", None) or not runway_pts_xyz:
        return lambda x, y: None

    def _cap(u, v):
        return G.edge_cap.get(G._ekey(u, v), TAXI_MAX_GRADE)

    def _capdist_from(src):
        dist = {src: 0.0}
        pq = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist.get(u, _INF):
                continue
            for v, w in G.adj.get(u, ()):
                nd = d + _cap(u, v) * w
                if nd < dist.get(v, _INF):
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    # taxiway↔runway CONTACTS: every centerline vertex INSIDE a runway polygon, or
    # an ENDPOINT within _CONTACT_EDGE_TOL_M of a runway EDGE.  Anchor that
    # contact's graph node at the runway surface elevation there (one cap-Dijkstra
    # per contact, amortised).  Edge/inside distance — NOT runway-ring-vertex
    # distance — so a taxiway meeting a long runway edge mid-span still anchors.
    from auto_patch.layout import ROLE_RUNWAY as _ROLE_RWY
    from shapely.ops import unary_union as _uu
    _rpolys = [s.polygon for s in layout.shapes
               if s.role == _ROLE_RWY and s.polygon is not None
               and not s.polygon.is_empty]
    _rbnd = _uu([p.boundary for p in _rpolys]) if _rpolys else None
    _runi = _uu(_rpolys) if _rpolys else None

    def _rwy_elev_at(px, py):
        return min(runway_pts_xyz,
                   key=lambda v: (v[0] - px) ** 2 + (v[1] - py) ** 2)[2]

    anchors: List[Tuple[float, dict]] = []
    if _rbnd is not None:
        _seen_anchor: set = set()
        for entry in (getattr(layout, "apt_taxi_centerlines", None) or []):
            ln = entry[0] if isinstance(entry, (tuple, list)) else entry
            ref = entry[1] if (isinstance(entry, (tuple, list))
                               and len(entry) > 1) else None
            if (ln is None or ln.is_empty
                    or str(ref or "").upper().startswith("SVC")):
                continue
            coords = list(ln.coords)
            n_c = len(coords)
            for vi, (vx, vy) in enumerate(coords):
                p = Point(vx, vy)
                is_end = (vi == 0 or vi == n_c - 1)
                if not (_runi.contains(p)
                        or (is_end
                            and _rbnd.distance(p) <= _CONTACT_EDGE_TOL_M)):
                    continue
                k, _ = G.nearest_key(vx, vy)
                if k is None or k in _seen_anchor:
                    continue
                _seen_anchor.add(k)
                anchors.append((_rwy_elev_at(vx, vy), _capdist_from(k)))
    if not anchors:
        return lambda x, y: None

    cls = [ln for (ln, n) in (getattr(layout, "apt_taxi_centerlines", None)
                              or [])
           if ln is not None and not ln.is_empty
           and not str(n or "").upper().startswith("SVC")]
    if not cls:
        return lambda x, y: None

    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None

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
        kA, _ = G.nearest_key(*A[0])
        kB, _ = G.nearest_key(*B[0])
        ecap = G.edge_cap.get(G._ekey(kA, kB), TAXI_MAX_GRADE)
        # perpendicular climb: taxiway-corridor part at the taxiway cap, the
        # rest (real apron) at 1 % (zero for an on-centerline spine node).
        perp_climb = (ecap * min(perp, _TAXI_HALF_W_M)
                      + _APRON_CAP * max(0.0, perp - _TAXI_HALF_W_M))
        floor, ceil = -_INF, _INF
        for (ae, cdm) in anchors:
            cands = []
            if kA in cdm:
                cands.append(cdm[kA] + ecap * A[1])   # + partial first edge
            if kB in cdm:
                cands.append(cdm[kB] + ecap * B[1])
            if not cands:
                continue                          # this connection can't reach
            budget = min(cands) + perp_climb
            ceil = min(ceil, ae + budget)
            floor = max(floor, ae - budget)
        if ceil >= _INF:
            return None
        return (floor, ceil)

    return band


def building_feasible_levels(
        layout,
        runway_pts_xyz: List[Tuple[float, float, float]],
        dem_sampler: Callable[[float, float], "float | None"],
        band=None,
) -> Dict[int, float]:
    """Return ``{id(building_shape): seated_level_m}`` for every
    ``ROLE_BUILDING`` that touches airside pavement.

    ``runway_pts_xyz``: ``[(x_m, y_m, elev_m)]`` every runway ring vertex at its
    solved elevation (the runway-edge anchors — see :func:`reach_band_sampler`).
    ``dem_sampler(x, y)``: DEM (m) at a layout-local point, or None.  The level
    is ``clamp(DEM, floor, ceiling)`` from the shared route-feasibility band — if
    DEM is not reachable within grade from every runway route, the level is
    pulled into the band (the building is adjusted to be feasible).  Buildings
    not touching airside pavement are omitted (the caller keeps them at DEM).

    ``band``: a pre-built sampler from :func:`reach_band_sampler` — pass the
    spine's band so buildings are placed on the SAME graph the spine is graded on
    (the single graph; they then agree by construction).  If omitted, a band is
    built here (identical inputs → identical band)."""
    from shapely.ops import unary_union

    if band is None:
        band = reach_band_sampler(layout, runway_pts_xyz)
    polys = [s.polygon for s in layout.shapes
             if s.role in _AIRSIDE_ROLES and s.polygon is not None
             and not s.polygon.is_empty]
    if not polys:
        return {}
    airside = unary_union(polys)

    out: Dict[int, float] = {}
    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty):
            continue
        if airside.is_empty or s.polygon.distance(airside) > _TOUCH_TOL_M:
            continue                            # not airside-served → DEM
        c = s.polygon.centroid
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
