"""Anchors + bounds for the one-profile solve — all from THE ONE graph.

There is a single reachability graph: the taxi-route reach band
(``building_feasibility.reach_band_sampler``, over ``shared_taxi_route_graph``).
It sets the building levels AND bounds every apron / spine / rect node, so they
agree by construction.  This module never builds a second graph.

* ``reach_band_for`` — build the band (+ a DEM sampler + the runway-edge anchors)
  once per solve.
* ``build_building_seats`` — seat each airside building FLAT at the level its
  FRONTAGE can reach (the band intersected over the pad ring), not the centroid:
  the band is a per-point envelope and a serving centerline climbs along a pad,
  so the centroid may reach higher than the apron around the pad can grade to.
* ``node_bands`` — the per-node ``(floor, ceiling)`` the solve clamps into.
* ``apron_body_nodes`` — apron-body vs taxi-route role split (target only).
"""
from __future__ import annotations

from auto_patch.layout import (
    ROLE_APRON, ROLE_BUILDING, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_SERVICE_JUNCTION,
    ROLE_SERVICE_ROAD, ROLE_STUB,
)

_INF = float("inf")

# The TAXI ROUTE (smoothness target, bounded by the reach band): taxi rects +
# junctions.  A node shared by an apron AND a route shape is a route node.
_ROUTE_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
})
# DEM-FOLLOWING body (closest-to-DEM target, NO taxi-band bound): aprons AND
# service roads/junctions.  A service road is NOT a taxiway — it grades at 4% and
# ties to the ground road network / terrain, so it must NOT be clamped to the
# taxi reach band (which would cap it metres below DEM — user 2026-06-25).
_DEM_BODY_ROLES = frozenset({
    ROLE_APRON, ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION,
})


def _open_ring(coords):
    if coords and coords[0] == coords[-1]:
        return list(coords[:-1])
    return list(coords)


def reach_band_for(layout, elev, bucket_to_idx, dem, tile_lat, tile_lon):
    """Build the one reach band, a DEM sampler, and the runway-edge anchors."""
    from auto_patch.elevation import _sample_dem
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_sampler)
    from auto_patch.elevation_per_surface.unified_jacobi import _runway_edge_pts

    runway_pts = _runway_edge_pts(layout, elev, bucket_to_idx)
    band = reach_band_sampler(layout, runway_pts)

    def _dem(x, y):
        try:
            lat, lon = layout.m_to_ll(x, y)
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except Exception:                                     # pragma: no cover
            return None

    return band, _dem, runway_pts


def build_building_seats(layout, bucket_to_idx, band, dem_fn, runway_pts):
    """``{pad_node_idx: flat_level}`` for every airside-touching building, seated
    at the level its FRONTAGE can reach (the band intersected over the pad ring)
    closest to DEM."""
    from auto_patch.elevation_per_surface.building_feasibility import (
        building_feasible_levels)

    cps = layout.canonical_points
    # ``building_feasible_levels`` decides WHICH buildings are airside-served (its
    # touch test) + gives the centroid level as a fallback for off-network pads.
    levels = building_feasible_levels(layout, runway_pts, dem_fn, band=band)
    seats: dict = {}
    for s in layout.shapes:
        lv = levels.get(id(s))
        if lv is None or s.polygon is None or s.polygon.is_empty:
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        # FRONTAGE-MEDIAN (user 2026-06-25): seat the flat pad at the MEDIAN band
        # ceiling over its frontage ring.  The serving route climbs along the pad,
        # so its reachable ceiling spans a range; the median centres the apron's
        # TWIST (corners slope ± to meet the climbing route) while resisting a few
        # outlier-high/low frontage points — the warp is carried by the apron twist
        # zone (grade_graph back-edge cap).
        ceils = sorted(b[1] for (x, y) in ring if (b := band(x, y)) is not None)
        de = dem_fn(s.polygon.centroid.x, s.polygon.centroid.y)
        if ceils:
            m = len(ceils)
            med = (ceils[m // 2] if m % 2
                   else 0.5 * (ceils[m // 2 - 1] + ceils[m // 2]))
            level = min(de, med) if de is not None else med
        else:
            level = float(lv)                        # off-network → fallback
        for (x, y) in ring:
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is not None:
                seats[i] = float(level)
    return seats


def node_bands(nodes, band):
    """Per-node ``(floor, ceiling)`` from the one reach band (``None`` off-net)."""
    return [band(x, y) for (x, y) in nodes]


def building_spine_floor(layout, nodes, bucket_to_idx, building_seats,
                         node_band, spine_adj):
    """``{spine_node_idx: floor}`` — make the serving spine RISE to serve its
    buildings (user 2026-06-25): the taxi arm exists to serve its pads, so the
    SAME trace that set a building's feasible level anchors the spine at the
    precise elevation it must reach there, and that anchor is GRADED SMOOTHLY
    along the centerline chain ("grade smoothly between anchors").

    For each airside building, the serving centerline is the one the reach band
    used (``_nearest_visible_centerline`` across the continuous apron — NOT the
    geometric nearest, so the anchor is exactly the point the building was made
    consistent with).  The spine node nearest the building's perpendicular FOOT
    is anchored at ``seat − APRON_MAX_GRADE·dist`` — the elevation the spine needs
    so the apron grades ≤1 % up to the flat pad.

    That foot anchor is then propagated along the CONSECUTIVE centerline chain
    (``spine_adj``, budget ``cap·dist``) as a floor that DECREASES at exactly the
    cap rate: ``floor_j = anchor − capdist(foot → j)``.  This builds the whole
    climbing ramp, and because the floor is cap-Lipschitz along the chain it is
    grade-consistent BY CONSTRUCTION — it can never force a spine grade break, and
    (since every chain node's neighbour is also floored) the solve's "envelope
    yields" fallback no longer drops it.  A single un-propagated floor was dropped
    whenever the foot's flat runway-side neighbour capped it low → the arm stayed
    flat (CYXY ~U12 694.5 vs building19 700.2, 106 m away).  Each floor is clamped
    to the node's band ceiling (never above what the runway route reaches)."""
    import heapq
    from shapely.geometry import Point
    from shapely.strtree import STRtree
    from auto_patch.config import APRON_MAX_GRADE, VISIBLE_CHORD_CONNECT
    from auto_patch.grade_graph import SPINE_PERP_TOL_M
    from auto_patch.layout import ROLE_BUILDING
    from auto_patch.elevation_per_surface.building_feasibility import (
        _nearest_visible_centerline, _pavement_visibility)

    cps = layout.canonical_points
    cl_items = [(ln, n) for (ln, n)
                in (getattr(layout, "apt_taxi_centerlines", None) or [])
                if ln is not None and not ln.is_empty
                and not str(n or "").upper().startswith("SVC")]
    clines = [ln for (ln, _n) in cl_items]
    if not clines:
        return {}
    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None
    cl_index = {id(ln): k for k, ln in enumerate(clines)}
    # spine nodes on each centerline, with arc position.
    pts = [Point(x, y) for (x, y) in nodes]
    tree = STRtree(pts)
    on_cl: list = []                       # per centerline: [(arc, node_idx), ...]
    for ln in clines:
        members = []
        try:
            cand = tree.query(ln.buffer(SPINE_PERP_TOL_M))
        except Exception:                                     # pragma: no cover
            cand = []
        for qi in cand:
            i = int(qi)
            if ln.distance(pts[i]) <= SPINE_PERP_TOL_M:
                members.append((ln.project(pts[i]), i))
        on_cl.append(members)

    # FOOT ANCHORS: one per building, at the spine node nearest its foot.
    src: dict = {}
    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty):
            continue
        lv = None
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i in building_seats:
                lv = building_seats[i]
                break
        if lv is None:
            continue
        c = s.polygon.centroid
        ln = (_nearest_visible_centerline(c, clines, vis) if vis is not None
              else min(clines, key=lambda L: L.distance(c)))
        members = on_cl[cl_index[id(ln)]]
        if not members:
            continue
        foot = ln.interpolate(ln.project(c))
        _, i = min((foot.distance(pts[k]), k) for (_arc, k) in members)
        t = lv - APRON_MAX_GRADE * s.polygon.distance(pts[i])  # 1% spine→pad
        if t > src.get(i, -float("inf")):
            src[i] = t

    if not src:
        return {}

    # Propagate the anchors along the consecutive spine chain as a cap-Lipschitz
    # floor (Dijkstra on a MAX value: floor_j = max_src(target − capdist)).  budget
    # is ``cap·dist`` so the floor declines at exactly the per-letter cap — the
    # smooth ramp that serves the pad.
    floor: dict = {}
    pq = [(-t, i) for i, t in src.items()]
    heapq.heapify(pq)
    while pq:
        negt, i = heapq.heappop(pq)
        t = -negt
        if t <= floor.get(i, -float("inf")):
            continue
        floor[i] = t
        for (j, budget) in spine_adj.get(i, ()):     # budget = cap·dist
            nt = t - budget
            if nt > floor.get(j, -float("inf")):
                heapq.heappush(pq, (-nt, j))

    # clamp every floor to its node's band ceiling (never above the reachable).
    for i in list(floor):
        nb = node_band[i] if i < len(node_band) else None
        if nb is not None and floor[i] > nb[1]:
            floor[i] = nb[1]
    return floor


def apron_body_nodes(layout, bucket_to_idx):
    """Node indices that follow DEM (apron bodies + service roads/junctions) and
    are NOT part of the taxi route — closest-to-DEM target, no taxi-band bound.
    The rest of airside is the taxi route (smooth, band-bounded)."""
    cps = layout.canonical_points
    body: set = set()
    route: set = set()
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        if s.role in _DEM_BODY_ROLES:
            tgt = body
        elif s.role in _ROUTE_ROLES:
            tgt = route
        else:
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is not None:
                tgt.add(i)
    return body - route
