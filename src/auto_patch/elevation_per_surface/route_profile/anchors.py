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
    import os as _os
    from auto_patch.layout import ROLE_APRON
    from auto_patch.elevation_per_surface.building_feasibility import (
        building_feasible_levels)

    cps = layout.canonical_points
    # ``building_feasible_levels`` decides WHICH buildings are airside-served (its
    # touch test) + gives the centroid level as a fallback for off-network pads.
    levels = building_feasible_levels(layout, runway_pts, dem_fn, band=band)

    # FRONTAGE-EDGE seat (user 2026-06-27): seat the flat pad at the feasible level
    # reachable at the CENTRE of its FRONTAGE edge — the apron-shared building edge
    # facing the MOST-CONSTRAINED taxi route (the lowest band ceiling among the
    # apron-shared edges).  The straight route from that centre to the binding
    # taxiway IS what ``band`` measures, so the apron can grade ≤1 % from the
    # frontage down to the taxiway and the far frontages descend to the pad.  This
    # supersedes the whole-ring MEDIAN, which over-pinned the low (route-limited)
    # frontage corner by averaging in the far high corners — CYXY building15 was
    # seated 709.4 (median over 707.6..712.5) while its A2 frontage centre reaches
    # only 708.4, pinning the A2-end apron 1.8 m high → the 20 % apron cliff.
    # Gate off → whole-ring median (legacy, byte-identical).
    _frontage = _os.environ.get("O4_BUILDING_FRONTAGE_SEAT", "1") == "1"
    # Large buildings (≥ area) seat at the FULL-FRONTAGE feasible level (user
    # 2026-06-27): the entire frontage must grade to the spine ≤1 %, so the seat is
    # the band intersected over the whole frontage (computed by
    # ``building_feasible_levels``), not the single lowest-ceiling frontage edge.
    from auto_patch.config import (
        BUILDING_FULL_FRONTAGE, BUILDING_FULL_FRONTAGE_AREA_M2)
    _full_frontage = (BUILDING_FULL_FRONTAGE
                      and _os.environ.get(
                          "O4_BUILDING_FULL_FRONTAGE", "1") == "1")
    apron_keys: set = set()
    if _frontage:
        for a in layout.shapes:
            if (a.role == ROLE_APRON and a.polygon is not None
                    and not a.polygon.is_empty):
                for (x, y) in _open_ring(list(a.polygon.exterior.coords)):
                    apron_keys.add((round(x, 2), round(y, 2)))

    def _median(ring, de):
        ceils = sorted(b[1] for (x, y) in ring if (b := band(x, y)) is not None)
        if not ceils:
            return None
        m = len(ceils)
        med = (ceils[m // 2] if m % 2
               else 0.5 * (ceils[m // 2 - 1] + ceils[m // 2]))
        return min(de, med) if de is not None else med

    def _frontage_level(ring, de):
        """Lowest band ceiling among the centres of the building's apron-shared
        edges (both endpoints shared with an apron) — the most-constrained
        frontage.  None when no edge is apron-shared (→ caller falls back)."""
        n = len(ring)
        best = None
        for i in range(n):
            a = (round(ring[i][0], 2), round(ring[i][1], 2))
            b = (round(ring[(i + 1) % n][0], 2), round(ring[(i + 1) % n][1], 2))
            if a in apron_keys and b in apron_keys:
                cx = 0.5 * (ring[i][0] + ring[(i + 1) % n][0])
                cy = 0.5 * (ring[i][1] + ring[(i + 1) % n][1])
                bc = band(cx, cy)
                if bc is not None and (best is None or bc[1] < best):
                    best = bc[1]
        if best is None:
            return None
        return min(de, best) if de is not None else best

    seats: dict = {}
    for s in layout.shapes:
        lv = levels.get(id(s))
        if lv is None or s.polygon is None or s.polygon.is_empty:
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        de = dem_fn(s.polygon.centroid.x, s.polygon.centroid.y)
        if _full_frontage and s.polygon.area >= BUILDING_FULL_FRONTAGE_AREA_M2:
            # ``lv`` IS the full-frontage feasible level for a large building.
            level = float(lv)
        else:
            level = _frontage_level(ring, de) if _frontage else None
            if level is None:                        # no apron-shared edge / off
                level = _median(ring, de)
            if level is None:
                level = float(lv)                    # off-network → fallback
        for (x, y) in ring:
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is not None:
                seats[i] = float(level)
    return seats


def node_bands(nodes, band):
    """Per-node ``(floor, ceiling)`` from the one reach band (``None`` off-net)."""
    return [band(x, y) for (x, y) in nodes]


def _spine_floor_per_node(layout, nodes, bucket_to_idx, building_seats,
                          node_band, spine_adj):
    """``{spine_node_idx: floor}`` — floor EVERY spine node directly from its own
    VISIBLE chord to the nearest spine-facing building edge (user 2026-06-27,
    replacing the single centroid foot).

    For each spine node, take the straight chord to the closest point on each
    building within the frontage corridor; if that chord stays on pavement (a real
    apron path, not across grass / through another building) the node is floored at
    ``seat − 1%·chord`` — the elevation the spine must reach so the apron grades
    ≤1 % up to the flat pad.  A node takes the MAX over the buildings it faces.
    No centroid, no cap-decay propagation: ``seat − 1%·dist`` sampled per node is
    already cap-Lipschitz along the spine (adjacent nodes differ by ≤1 %·spacing ≤
    cap·spacing), so a big terminal's WHOLE frontage lifts the spine, not just one
    foot.  Each floor is clamped to the node's band ceiling (never above what the
    runway route reaches)."""
    from shapely.geometry import Point, LineString
    from shapely.ops import nearest_points
    from auto_patch.config import (
        APRON_MAX_GRADE, BUILDING_FRONTAGE_CORRIDOR_M, VISIBLE_CHORD_CONNECT)
    from auto_patch.layout import ROLE_BUILDING
    from auto_patch.elevation_per_surface.building_feasibility import (
        _pavement_visibility, _VIS_ON_PAV_FRAC)

    cps = layout.canonical_points
    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None

    builds = []
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
        if lv is not None:
            builds.append((s.polygon, float(lv)))
    if not builds:
        return {}

    floor: dict = {}
    for i in spine_adj:
        if i >= len(nodes):
            continue
        px, py = nodes[i]
        p = Point(px, py)
        best = None
        for (poly, lv) in builds:
            d = poly.distance(p)
            if d > BUILDING_FRONTAGE_CORRIDOR_M:
                continue
            near = nearest_points(poly, p)[0]   # spine-facing building edge point
            chord = LineString([(px, py), (near.x, near.y)])
            if vis is not None and chord.length > 1e-6 and not vis.contains(chord):
                try:                            # tolerate tiny weld-seam gaps
                    frac = chord.intersection(vis.context).length / chord.length
                except Exception:               # pragma: no cover
                    frac = 0.0
                if frac < _VIS_ON_PAV_FRAC:
                    continue                    # chord leaves pavement → not facing
            t = lv - APRON_MAX_GRADE * d        # 1 % apron from spine up to the pad
            if best is None or t > best:
                best = t
        if best is None:
            continue
        nb = node_band[i] if i < len(node_band) else None
        if nb is not None and best > nb[1]:
            best = nb[1]                        # never above the reachable ceiling
        floor[i] = best
    return floor


def building_spine_floor(layout, nodes, bucket_to_idx, building_seats,
                         node_band, spine_adj):
    """``{spine_node_idx: floor}`` — make the serving spine RISE to serve its
    buildings (user 2026-06-25): the taxi arm exists to serve its pads, so the
    SAME trace that set a building's feasible level anchors the spine at the
    precise elevation it must reach there, and that anchor is GRADED SMOOTHLY
    along the centerline chain ("grade smoothly between anchors").

    User 2026-06-27: the default is now :func:`_spine_floor_per_node` — every
    spine node floored directly from its own visible chord to the spine-facing
    building edge (the centroid foot under-covered large terminals).  The legacy
    centroid/full-frontage-foot path below is kept for A/B
    (``O4_SPINE_FLOOR_PER_NODE=0``).

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
    import os as _os0
    if _os0.environ.get("O4_SPINE_FLOOR_PER_NODE", "1") == "1":
        return _spine_floor_per_node(
            layout, nodes, bucket_to_idx, building_seats, node_band, spine_adj)

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

    # FOOT ANCHORS: lift the spine to serve each building's frontage.  For a LARGE
    # building (full-frontage gate) anchor EVERY qualifying frontage foot — the same
    # sides that set its seat (a taxi corridor within range + a visible chord) — so
    # the spine rises to ``seat − 1%·perp`` along the WHOLE frontage, not only the
    # centroid's foot (user 2026-06-27, the dual of the full-frontage seating rule:
    # the pad is seated to clear the whole frontage, so the serving spine must rise
    # to it everywhere).  A small building (or one with no qualifying side) anchors
    # the single centroid foot, as before.
    import os as _os
    from auto_patch.config import (
        BUILDING_FRONTAGE_CORRIDOR_M, BUILDING_FULL_FRONTAGE,
        BUILDING_FULL_FRONTAGE_AREA_M2)
    from auto_patch.elevation_per_surface.building_feasibility import (
        _has_visible_corridor)
    # ⚠ BANKED DEFAULT OFF: raising the spine to the WHOLE frontage regresses OEMA
    # (423→635 within-grade violations) — the pad sits at the route-reachable
    # CEILING (DEM-clamped), higher than the LOCAL spine can climb to from its
    # runway connection within grade, so lifting the spine to it breaks the
    # spine↔runway grade instead of fixing the apron.  Kept for A/B; needs the
    # seat reconciled with the local spine before it can default on.
    _full = (BUILDING_FULL_FRONTAGE
             and _os.environ.get("O4_BUILDING_FULL_FRONTAGE", "1") == "1"
             and _os.environ.get("O4_FRONTAGE_SPINE_RISE", "0") == "1")

    src: dict = {}

    def _anchor(px, py, lv, dist_geom):
        """Raise the spine node nearest ``(px, py)``'s perpendicular foot to
        ``lv − 1%·(dist_geom → that node)`` — the elevation the apron needs so it
        grades ≤1 % up to the flat pad."""
        c = Point(px, py)
        ln = (_nearest_visible_centerline(c, clines, vis) if vis is not None
              else min(clines, key=lambda L: L.distance(c)))
        members = on_cl[cl_index[id(ln)]]
        if not members:
            return
        foot = ln.interpolate(ln.project(c))
        _, i = min((foot.distance(pts[k]), k) for (_arc, k) in members)
        t = lv - APRON_MAX_GRADE * dist_geom.distance(pts[i])  # 1% spine→pad
        if t > src.get(i, -float("inf")):
            src[i] = t

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
        anchored = False
        if _full and s.polygon.area >= BUILDING_FULL_FRONTAGE_AREA_M2:
            ring = list(s.polygon.exterior.coords)
            for k in range(len(ring) - 1):
                ax, ay = ring[k]
                bx, by = ring[k + 1]
                mx, my = 0.5 * (ax + bx), 0.5 * (ay + by)
                for (px, py) in ((ax, ay), (mx, my), (bx, by)):
                    if _has_visible_corridor(px, py, clines, vis,
                                             BUILDING_FRONTAGE_CORRIDOR_M):
                        _anchor(px, py, lv, Point(px, py))
                        anchored = True
        if not anchored:                     # small / no qualifying frontage side
            c = s.polygon.centroid
            _anchor(c.x, c.y, lv, s.polygon)

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


def apply_groundside_reach(layout, bucket_to_idx, elev, cap):
    """Re-level each groundside piece a service road connects to an apron, to the
    elevation the connector can REACH within the service-road grade cap — so the
    connector grades <=cap instead of ramping steeply to the groundside's raw DEM
    (user 2026-06-27, refining the accept-the-ramp model).

    "After buildings and aprons are anchored, check groundside pieces: if they have
    a service road, and if that road reaches an apron, follow that route to find
    what elevation the groundside can reach within grade and anchor it there.  If it
    has no service roads they just stay DEM."

    The service road that meets a groundside piece may reach the apron through a
    CHAIN of service roads/junctions (an out-and-back route, a yard loop), so the
    binding reference is the connector's OWN apron-ward mouth elevation (already
    solved), not the distant apron: the groundside mouth can sit at most
    ``cap * route_len`` from it (``route_len`` = the binding apron-ward->groundside
    edge).  Whether to re-level at all is gated by APRON REACHABILITY — the piece's
    service road must connect (directly or through the service network) to an apron;
    a groundside-only yard road never re-levels its piece.

    The piece is shifted by a UNIFORM offset (preserving its DEM relief) so its
    mouth(s) sit at the closest-to-DEM reachable level; the connector then grades the
    short climb at <=cap.  A piece reached by several connectors must satisfy them
    ALL (interval INTERSECTION of the per-connector shift bounds).

    Mutates groundside ``node_altitudes`` in place and returns ``(n_relevelled,
    welds)`` where ``welds = {node_idx: shifted_groundside_alt}`` for the mouths of
    the APRON-REACHABLE connectors only (the caller pins ``elev`` to these so the
    connector and groundside emit as one welded node).  A service road that does NOT
    reach an apron is left untouched — its piece stays DEM and its mouth is not
    pinned (the user's "stays DEM" case).  Safe to shift a whole piece because a
    groundside lot shares no nodes with airside (a clearance gap separates them) —
    only the connector mouth, which is welded to the shifted level."""
    import math
    from auto_patch.layout import (
        ROLE_GROUNDSIDE_PAVEMENT, ROLE_APRON,
        ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)

    cps = layout.canonical_points

    def _key(x, y):
        return cps.get_or_add(float(x), float(y))

    # apron-owned canonical node keys (a service road TOUCHES the apron here).
    apron_keys: set = set()
    for s in layout.shapes:
        if s.role != ROLE_APRON or s.polygon is None or s.polygon.is_empty:
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            apron_keys.add(_key(x, y))

    # groundside pieces: per-key DEM altitude (the connector mouth shares a key);
    # plus the UNION of every groundside key (to split a connector's nodes into
    # groundside-mouth vs apron-ward).
    gs_pieces = []
    gs_all_keys: set = set()
    for g in layout.shapes:
        if (g.role != ROLE_GROUNDSIDE_PAVEMENT or g.polygon is None
                or g.polygon.is_empty or not g.node_altitudes):
            continue
        gcoords = list(g.polygon.exterior.coords)
        galts = list(g.node_altitudes)
        kalt: dict = {}
        for k in range(min(len(gcoords), len(galts))):
            if galts[k] is not None:
                kalt.setdefault(_key(*gcoords[k]), float(galts[k]))
        if kalt:
            gs_pieces.append((g, kalt))
            gs_all_keys |= set(kalt)
    if not gs_pieces:
        return 0, set()

    # Service-road network: each shape's node keys, an apron-touch flag, and an
    # adjacency (two service shapes are adjacent when they share a node key).  BFS
    # from the apron-touching shapes marks every APRON-REACHABLE service shape.
    svc = []                   # [(shape, keyset)]
    for c in layout.shapes:
        if c.role not in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION):
            continue
        if c.polygon is None or c.polygon.is_empty:
            continue
        ks = {_key(x, y) for (x, y) in _open_ring(list(c.polygon.exterior.coords))}
        svc.append((c, ks))
    if not svc:
        return 0, set()
    key_to_svc: dict = {}
    for si, (_c, ks) in enumerate(svc):
        for k in ks:
            key_to_svc.setdefault(k, []).append(si)
    reachable: set = set()
    stack = [si for si, (_c, ks) in enumerate(svc) if ks & apron_keys]
    reachable.update(stack)
    while stack:
        si = stack.pop()
        for k in svc[si][1]:
            for sj in key_to_svc.get(k, ()):
                if sj not in reachable:
                    reachable.add(sj)
                    stack.append(sj)

    from shapely.geometry import Point

    MAX_ROUTE = 90.0           # cap the route distance budgeted (m)
    RAISE_W = 14.0             # half-width of the truck-route corridor to raise

    # Apron nodes (x, y, idx) — for the route ANCHOR elevation (apron at the deep
    # end of the truck route) and for the apron-arm RAISE along the route; plus the
    # connector/service nodes (the corridor includes the connector itself).
    apron_pts = []
    pav_pts = []
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        if s.role == ROLE_APRON:
            tgt_apron = True
        elif s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION):
            tgt_apron = False
        else:
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(_key(x, y))
            if i is not None and i < len(elev):
                pav_pts.append((x, y, i))
                if tgt_apron:
                    apron_pts.append((x, y, i))
    centerlines = [ln for (ln, _r) in
                   (getattr(layout, "apt_service_centerlines", None) or [])
                   if ln is not None and not ln.is_empty]

    def _nearest_apron_elev(px, py, tol=16.0):
        best = None
        for (ax, ay, ai) in apron_pts:
            d = math.hypot(ax - px, ay - py)
            if d <= tol and (best is None or d < best[0]):
                best = (d, elev[ai])
        return best[1] if best else None

    # ── Per apron-reachable connector: follow its TRUCK ROUTE to the apron ────
    # The route is the truck centerline through the connector; budget the reach
    # over its FULL length (groundside edge → apron base, ~55 m) against the apron's
    # elevation at that base — NOT just the connector's own span — so the groundside
    # can sit ``cap·route_len`` above the apron (user 2026-06-27).  Stash each route
    # (with its groundside-mouth arc + direction) for the RAISE pass.
    bounds: dict = {}          # id(g) -> [g, lo, hi]
    routes = []                # (id(g), ln, gm_s, apron_dir, route_len, dem_mouth)
    for si in reachable:
        c, _ks = svc[si]
        cnodes = [(x, y, bucket_to_idx.get(_key(x, y)))
                  for (x, y) in _open_ring(list(c.polygon.exterior.coords))]
        cen = c.polygon.centroid
        # the SHORTEST centerline that actually runs through this connector (avoid a
        # long through-airport route whose far end is hundreds of metres away).
        local = [L for L in centerlines if L.distance(cen) <= 8.0]
        ln = min(local, key=lambda L: L.length) if local else None
        for (g, kalt) in gs_pieces:
            gmouth = [(x, y) for (x, y, _i) in cnodes if _key(x, y) in kalt]
            if not gmouth:
                continue
            gmx = sum(p[0] for p in gmouth) / len(gmouth)
            gmy = sum(p[1] for p in gmouth) / len(gmouth)
            dem_gs = sum(kalt[_key(x, y)] for (x, y) in gmouth) / len(gmouth)
            gm_s = apron_dir = route_len = base_elev = None
            if ln is not None:
                gm_s = ln.project(Point(gmx, gmy))
                # apron side = the centerline end FARTHER from the groundside piece.
                e0, e1 = ln.coords[0], ln.coords[-1]
                apron_end_s = (0.0 if g.polygon.distance(Point(e0))
                               >= g.polygon.distance(Point(e1)) else ln.length)
                apron_dir = 1.0 if apron_end_s > gm_s else -1.0
                route_len = min(abs(apron_end_s - gm_s), MAX_ROUTE)
                bp = ln.interpolate(max(0.0, min(ln.length,
                                                 gm_s + apron_dir * route_len)))
                base_elev = _nearest_apron_elev(bp.x, bp.y)
            if base_elev is None:
                # Fallback: no usable centerline → reference the connector's own
                # apron-ward mouth, budget over its span (the earlier model).
                ref_nodes = [i for (x, y, i) in cnodes
                             if i is not None and i < len(elev)
                             and _key(x, y) not in gs_all_keys]
                if not ref_nodes:
                    continue
                base_elev = sum(elev[i] for i in ref_nodes) / len(ref_nodes)
                route_len = min(math.hypot(x - gmx, y - gmy)
                                for (x, y, i) in cnodes if i in ref_nodes)
                ln = None
            if route_len < 1e-6:
                continue
            budget = cap * route_len
            lo = base_elev - budget - dem_gs
            hi = base_elev + budget - dem_gs
            b = bounds.get(id(g))
            if b is None:
                bounds[id(g)] = [g, lo, hi]
            else:
                b[1] = max(b[1], lo)
                b[2] = min(b[2], hi)
            routes.append((id(g), ln, gm_s, apron_dir, route_len, dem_gs,
                           (gmx, gmy)))

    n = 0
    deltas: dict = {}
    for gid, (g, lo, hi) in bounds.items():
        # Closest-to-DEM shift inside the feasible band; if the connectors'
        # reaches don't overlap (no uniform shift keeps them all <=cap) fall back
        # to the band midpoint, which minimises the worst residual.
        delta = (min(max(0.0, lo), hi) if lo <= hi else 0.5 * (lo + hi))
        deltas[gid] = delta
        if abs(delta) < 1e-6:
            continue
        g.node_altitudes = [
            (a + delta) if a is not None else None for a in g.node_altitudes]
        n += 1

    # (now-shifted) groundside altitude per key, for the weld.
    gs_key_alt: dict = {}
    for (g, _kalt) in gs_pieces:
        gcoords = list(g.polygon.exterior.coords)
        galts = list(g.node_altitudes)
        for k in range(min(len(gcoords), len(galts))):
            if galts[k] is not None:
                gs_key_alt.setdefault(_key(*gcoords[k]), float(galts[k]))

    hard: set = set()

    # ── RAISE the apron arm + connector along the truck route ────────────────
    # The narrow apron arm is welded to the connector, so as the connector climbs at
    # <=cap to the (now higher) groundside, that climb is carried BACK along the
    # truck route: every apron/connector node in the route corridor takes the
    # SELF-TAPERING profile ``gs_level − cap·(arc back from the groundside mouth)``.
    # The taper auto-stops where it drops below the apron's own elevation (the base),
    # so the raise is confined to the arm; the caller grades the apron body into it.
    for (gid, ln, gm_s, apron_dir, route_len, dem_mouth, (gmx, gmy)) in routes:
        delta = deltas.get(gid, 0.0)
        gs_level = dem_mouth + delta
        # The arm must rise whenever the groundside ends up ABOVE the apron base —
        # even when the piece was LOWERED toward a reachable level (delta < 0, its
        # DEM was higher than reachable).  The self-taper raises only where needed.
        if ln is None:
            continue
        for (px, py, pi) in pav_pts:
            p = Point(px, py)
            if ln.distance(p) > RAISE_W:
                continue
            # corridor membership = along the route (apron side, within route_len);
            # but the PROFILE tapers by STRAIGHT distance from the groundside mouth,
            # so the connector rect (graded on its straight span, not the curved
            # centerline arc) comes out at exactly <=cap, not the arc-inflated rate.
            s = ln.project(p)
            if (s - gm_s) * apron_dir < -2.0 or (s - gm_s) * apron_dir \
                    > route_len + 5.0:
                continue
            straight = math.hypot(px - gmx, py - gmy)
            tgt = gs_level - cap * straight
            if tgt > elev[pi] + 1e-3:
                elev[pi] = tgt
                hard.add(pi)

    # ── WELD each connector's groundside mouth to the shifted groundside ─────
    for si in reachable:
        c, _ks = svc[si]
        for (x, y) in _open_ring(list(c.polygon.exterior.coords)):
            k = _key(x, y)
            a = gs_key_alt.get(k)
            if a is None:
                continue
            i = bucket_to_idx.get(k)
            if i is not None and i < len(elev):
                elev[i] = a
                hard.add(i)
    return n, hard


def apply_service_road_dem_follow(layout, bucket_to_idx, elev, dem_elev, cap,
                                  anchor_extra=()):
    """Grade the service-road network to FOLLOW DEM at <=cap (user 2026-06-27).

    A ground-vehicle road is NOT airside: it rises/falls toward terrain, anchored
    only where it WELDS to the airside (taxi/apron/runway, kept at their solved
    bowl elevation) or to a groundside piece (``anchor_extra``).  Every other
    service node sits at ``clamp(DEM, reach-band-from-anchors-at-cap)`` where the
    reach band is the cap-Lipschitz envelope along the SERVICE graph (axial, edge by
    edge) — so a road ramps from its airside connection toward DEM at <=4% instead
    of being held flat in the bowl (SVC4 was ~6-11 m below terrain).  The
    road-vs-airside seam is by design (``check_grade._airside_groundside_pair``), so
    rising past a flat neighbour is not a step.

    Mutates ``elev`` in place; returns the set of node indices it moved."""
    import heapq
    from collections import defaultdict
    from auto_patch.layout import (
        ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION, ROLE_GROUNDSIDE_PAVEMENT)

    cps = layout.canonical_points

    def _key(x, y):
        return cps.get_or_add(float(x), float(y))

    SVC = (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)
    svc_nodes: set = set()
    adj = defaultdict(list)
    for s in layout.shapes:
        if s.role not in SVC or s.polygon is None or s.polygon.is_empty:
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        idxs = [bucket_to_idx.get(_key(x, y)) for (x, y) in ring]
        for k in range(len(ring)):
            i, j = idxs[k], idxs[(k + 1) % len(ring)]
            if i is None or i >= len(elev):
                continue
            svc_nodes.add(i)
            if j is not None and j != i and j < len(elev):
                import math as _m
                dd = _m.hypot(ring[k][0] - ring[(k + 1) % len(ring)][0],
                              ring[k][1] - ring[(k + 1) % len(ring)][1])
                adj[i].append((j, dd))
                adj[j].append((i, dd))
    if not svc_nodes:
        return set()

    # Anchors = service nodes that are ALSO a corner of a NON-service pavement shape
    # (the road welds to the airside there), held at their solved elevation; plus
    # any groundside-welded nodes passed in.
    anchors: dict = {}
    for s in layout.shapes:
        if (s.role in SVC or s.role == ROLE_GROUNDSIDE_PAVEMENT
                or s.polygon is None or s.polygon.is_empty):
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(_key(x, y))
            if i in svc_nodes:
                anchors[i] = elev[i]
    for i in anchor_extra:
        if i in svc_nodes and i < len(elev):
            anchors[i] = elev[i]

    def _reach(sign):                       # +1 → ceil, −1 → floor
        best: dict = {}
        pq = [((av if sign > 0 else -av), a) for a, av in anchors.items()]
        heapq.heapify(pq)
        while pq:
            v, k = heapq.heappop(pq)
            t = v if sign > 0 else -v
            if k in best and ((sign > 0 and t >= best[k] + 1e-9)
                              or (sign < 0 and t <= best[k] - 1e-9)):
                continue
            best.setdefault(k, t)
            for (j, dd) in adj[k]:
                nt = t + sign * cap * dd
                pj = best.get(j)
                if pj is None or (sign > 0 and nt < pj) or (sign < 0 and nt > pj):
                    best[j] = nt
                    heapq.heappush(pq, ((nt if sign > 0 else -nt), j))
        return best

    ceil = _reach(+1) if anchors else {}
    floor = _reach(-1) if anchors else {}
    changed: set = set()
    for i in svc_nodes:
        if i in anchors:
            continue
        de = dem_elev[i] if i < len(dem_elev) else None
        if de is None:
            continue
        c = ceil.get(i)
        f = floor.get(i)
        if c is None:                       # unreachable from any anchor → DEM
            tgt = de
        else:
            lo = f if f is not None else -float("inf")
            tgt = min(max(de, lo), c)
        if abs(tgt - elev[i]) > 1e-3:
            elev[i] = tgt
            changed.add(i)
    return changed


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
