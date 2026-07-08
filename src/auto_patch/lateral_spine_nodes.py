"""Insert LATERAL corridor nodes on apron/junction edges (user 2026-06-26).

The within-shape grade check (and the solver) are vertex-pair based: a long
apron/junction edge running within taxi-width of a spine, with no intermediate
vertex, has nothing to sample — so a steep drop on the runway side of a risen
spine (CYXY building-19 apron) is invisible to BOTH the check and the solve, and
the apron just drapes to DEM.

This pass projects every spine centerline vertex perpendicularly onto any
apron/junction edge within ±half the taxi-width and inserts a vertex at the foot
(matching the spine nodes — no extra densification, per user).  The grade graph
then gains spine ↔ lateral-foot pairs (the lateral corridor grade is validated),
and the solver gains a node to grade that apron face down from the spine within
cap instead of draping it.

Runs PRE-SOLVE, after the spine is built and BEFORE the airside conformance, so
the inserted vertices are welded/propagated to neighbouring shapes too.
"""
from __future__ import annotations

import math
from collections import defaultdict

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import Point, Polygon
from shapely.strtree import STRtree

import O4_UI_Utils as UI

from .junction_rules import SLOPING_RECT_ROLES
from .layout import ROLE_APRON, ROLE_JUNCTION, ROLE_SERVICE_JUNCTION

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["insert_lateral_spine_nodes", "insert_service_lateral_nodes",
           "densify_junction_edges"]

# Body shapes that should sample the lateral corridor grade.
_LATERAL_BODY_ROLES = frozenset({ROLE_APRON, ROLE_JUNCTION, ROLE_SERVICE_JUNCTION})
_DEFAULT_HALF_W_M = 12.0          # fallback taxi half-width (≈ code C/D)
_CORNER_TOL_M = 0.5              # don't insert within this of an existing corner
_MERGE_TOL_M = 0.5              # merge feet closer than this on one edge


def _open(poly):
    cs = list(poly.exterior.coords)
    if len(cs) > 1 and cs[0] == cs[-1]:
        cs = cs[:-1]
    return cs


def densify_junction_edges(layout, icao: str = "", step: float = None) -> int:
    """Densify every JUNCTION's exterior edges to ~the spine node spacing (user
    2026-06-26).

    A junction is a taxiway that follows its spine, but a long exterior edge with
    only its two end corners interpolates FLAT between them and cannot track the
    spine's rise (CYXY junction #97: a 500 m edge stayed flat at 695.6 while the
    spine rose 694→699).  Subdividing every junction edge to the spine step gives
    the solver nodes to grade along that edge, so the whole junction surface tilts
    with its centerline.  Pure geometry; runs pre-solve next to the lateral pass.
    Returns the number of nodes inserted."""
    from .config import SPINE_STEP_M
    if step is None:
        step = SPINE_STEP_M
    # The spine edge already has its nodes (from junction_spine); densifying it
    # would add nodes WITHIN the spine perp-tolerance of the centerline, which
    # become NEW spine nodes and perturb the spine solve (CYXY: raised a
    # junction/A piece 0.5 m → stub/A 3.2 %).  So skip any edge that runs ON a
    # centerline; only densify the OFF-spine edges that drape flat.
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    from .layout import ROLE_RUNWAY
    from .grade_law import RUNWAY_JOIN_NEAR_M
    _SPINE_EDGE_TOL_M = 3.0
    _RUNWAY_TOL_M = RUNWAY_JOIN_NEAR_M   # ONE source = the runway-join _NEAR_M
    cls = [cl.line for cl in (getattr(layout, "apt_taxi_centerlines", None) or [])
           if cl.line is not None and not cl.line.is_empty
           and not cl.is_service]
    cl_union = None
    if cls:
        try:
            cl_union = unary_union(cls)
        except _GEOM_EXC:
            cl_union = None
    rwy_union = None
    try:
        rwys = [s.polygon for s in layout.shapes if s.role == ROLE_RUNWAY
                and s.polygon is not None and not s.polygon.is_empty]
        if rwys:
            rwy_union = unary_union(rwys)
    except _GEOM_EXC:
        rwy_union = None

    def _skip_edge(ax, ay, bx, by):
        """Skip densifying an edge that runs ON a centerline (its nodes would
        become spine nodes and perturb the spine solve) or ABUTS a runway (its
        nodes must match the runway surface, not the spine — runway-join check)."""
        e = LineString([(ax, ay), (bx, by)])
        try:
            if cl_union is not None and cl_union.distance(e) < _SPINE_EDGE_TOL_M:
                return True
            if rwy_union is not None and rwy_union.distance(e) < _RUNWAY_TOL_M:
                return True
        except _GEOM_EXC:
            return False
        return False

    n_junc = n_added = 0
    for s in layout.shapes:
        if (s.role != ROLE_JUNCTION or s.polygon is None or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"):
            continue
        ring = _open(s.polygon)
        if len(ring) < 3:
            continue
        new_ring = []
        added = 0
        for ei in range(len(ring)):
            ax, ay = ring[ei]
            bx, by = ring[(ei + 1) % len(ring)]
            new_ring.append((ax, ay))
            d = math.hypot(bx - ax, by - ay)
            k = max(0, int(round(d / step)) - 1)   # ~step spacing; 0 if already ≤step
            if k and _skip_edge(ax, ay, bx, by):
                k = 0                              # spine/runway edge → leave alone
            for j in range(1, k + 1):
                f = j / (k + 1)
                new_ring.append((ax + f * (bx - ax), ay + f * (by - ay)))
                added += 1
        if added:
            try:
                poly = Polygon(new_ring)
                if poly.is_valid and not poly.is_empty:
                    s.polygon = poly
                    n_junc += 1
                    n_added += added
            except _GEOM_EXC:
                continue
    if n_added:
        UI.vprint(1, f"  [pav-builder] {icao}: densified {n_junc} junction "
                  f"ring(s) (+{n_added} node(s)) to ~{step:.0f} m spine spacing "
                  f"so junction edges can follow the spine.")
    return n_added


def _short_edge_half_w(poly):
    cs = _open(poly)
    if len(cs) != 4:
        return None
    elens = [math.hypot(cs[(k + 1) % 4][0] - cs[k][0],
                        cs[(k + 1) % 4][1] - cs[k][1]) for k in range(4)]
    return 0.5 * min(elens)


def insert_lateral_spine_nodes(layout, icao: str = "") -> int:
    """Insert lateral-corridor vertices; returns the number inserted."""
    centerlines = getattr(layout, "apt_taxi_centerlines", None) or []
    targets = [s for s in layout.shapes
               if s.role in _LATERAL_BODY_ROLES and s.polygon is not None
               and not s.polygon.is_empty
               and s.polygon.geom_type == "Polygon"]
    if not targets or not centerlines:
        return 0

    # per-ref half-width from the taxi rects built on each centerline.
    hw_by_ref: dict = {}
    for s in layout.shapes:
        if (s.role in SLOPING_RECT_ROLES and s.polygon is not None
                and not s.polygon.is_empty):
            hw = _short_edge_half_w(s.polygon)
            if hw is not None:
                r = str(getattr(s, "ref", None))
                hw_by_ref[r] = max(hw_by_ref.get(r, 0.0), hw)

    polys = [s.polygon for s in targets]
    tree = STRtree(polys)

    # shape index -> {edge_index -> [(t, (fx, fy))]}
    inserts: dict = defaultdict(lambda: defaultdict(list))
    rings = [_open(p) for p in polys]

    for entry in centerlines:
        ln = entry.line if hasattr(entry, "line") else (entry[0] if isinstance(entry, (tuple, list)) else entry)
        ref = (entry[1] if (isinstance(entry, (tuple, list)) and len(entry) > 1)
               else None)
        if ln is None or ln.is_empty or str(ref or "").upper().startswith("SVC"):
            continue
        hw = hw_by_ref.get(str(ref), _DEFAULT_HALF_W_M)
        try:
            cs = list(ln.coords)
        except _GEOM_EXC:
            continue
        for (vx, vy) in cs:
            P = Point(vx, vy)
            try:
                cand = tree.query(P.buffer(hw))
            except _GEOM_EXC:
                continue
            for qi in cand:
                si = int(qi)
                ring = rings[si]
                n = len(ring)
                for ei in range(n):
                    ax, ay = ring[ei]
                    bx, by = ring[(ei + 1) % n]
                    dx, dy = bx - ax, by - ay
                    seg2 = dx * dx + dy * dy
                    if seg2 < 1e-9:
                        continue
                    t = ((vx - ax) * dx + (vy - ay) * dy) / seg2
                    if t <= 0.0 or t >= 1.0:
                        continue
                    fx, fy = ax + t * dx, ay + t * dy
                    if math.hypot(fx - vx, fy - vy) > hw:    # within taxi-width
                        continue
                    L = math.sqrt(seg2)
                    if t * L < _CORNER_TOL_M or (1.0 - t) * L < _CORNER_TOL_M:
                        continue                              # too near a corner
                    inserts[si][ei].append((t, (fx, fy)))

    if not inserts:
        return 0

    n_added = 0
    for si, by_edge in inserts.items():
        ring = rings[si]
        n = len(ring)
        new_ring = []
        for ei in range(n):
            new_ring.append(ring[ei])
            feet = sorted(by_edge.get(ei, []), key=lambda r: r[0])
            last = None
            for (_t, (fx, fy)) in feet:
                if last is not None and math.hypot(fx - last[0],
                                                   fy - last[1]) < _MERGE_TOL_M:
                    continue
                new_ring.append((fx, fy))
                last = (fx, fy)
                n_added += 1
        if len(new_ring) <= n:
            continue
        try:
            poly = Polygon(new_ring)
            if poly.is_valid and not poly.is_empty:
                targets[si].polygon = poly
        except _GEOM_EXC:
            continue

    if n_added:
        UI.vprint(1, f"  [pav-builder] {icao}: inserted {n_added} lateral "
                  f"corridor node(s) on apron/junction edges within taxi-width "
                  f"of a spine.")
    return n_added


def insert_service_lateral_nodes(layout, icao: str = "") -> int:
    """SPINE-FIRST service roads (config.SVC_SPINE_FIRST, part 30m): insert
    lateral cross-section vertices on SERVICE shape edges from the SERVICE
    (truck-route) centerlines, which :func:`insert_lateral_spine_nodes`
    deliberately skips (SVC lines must not couple APRONS to the road law).

    A service road's law edges (it joins ``grade_graph.SOFT_VISIBILITY_ROLES``
    under the gate) are vertex-pair based, and a road's long edges can run
    70-100 m with no intermediate vertex (the CYXY in-sim "ridge" report) —
    the 2 % transverse law then binds only at the far-apart corners, whose
    budget dwarfs the road width.  Projecting each spine STATION (centerline
    vertices, densified to ~SPINE_STEP_M) onto both road edges gives the law
    aligned cross-section pairs at station spacing: |Δz| across the road is
    then capped at SERVICE_ROAD_MAX_TRANSVERSE × width everywhere — the
    cross-road tear becomes unrepresentable.

    Same foot-insertion mechanics as :func:`insert_lateral_spine_nodes`
    (perpendicular foot, corner/merge tolerances); targets are the SERVICE
    roles only.  Runs pre-solve immediately after the taxi lateral pass, so
    conformance welds the new vertices into neighbouring shapes.  Returns the
    number of vertices inserted."""
    from .config import ROAD_CARVE_MAX_WIDTH_M, SPINE_STEP_M
    from .layout import ROLE_SERVICE_ROAD
    centerlines = getattr(layout, "apt_taxi_centerlines", None) or []
    targets = [s for s in layout.shapes
               if s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)
               and s.polygon is not None and not s.polygon.is_empty
               and s.polygon.geom_type == "Polygon"]
    svc_lines = [cl for cl in centerlines
                 if getattr(cl, "is_service", False)
                 and getattr(cl, "line", None) is not None
                 and not cl.line.is_empty]
    if not targets or not svc_lines:
        return 0

    # Cross-section half-width: the widest pavement the road carve classifies
    # (+ margin) so edge-hugging carved shapes still catch their feet.
    hw = ROAD_CARVE_MAX_WIDTH_M / 2.0 + 2.0

    polys = [s.polygon for s in targets]
    tree = STRtree(polys)
    inserts: dict = defaultdict(lambda: defaultdict(list))
    rings = [_open(p) for p in polys]

    def _stations(cs):
        """Centerline vertices densified to ≤ SPINE_STEP_M spacing (a 1206
        truck route can run long straight legs with sparse vertices — the
        exact stretches that tear)."""
        out = []
        for k in range(len(cs) - 1):
            ax, ay = cs[k]
            bx, by = cs[k + 1]
            out.append((ax, ay))
            d = math.hypot(bx - ax, by - ay)
            n_sub = max(0, int(math.ceil(d / SPINE_STEP_M)) - 1)
            for j in range(1, n_sub + 1):
                f = j / (n_sub + 1)
                out.append((ax + f * (bx - ax), ay + f * (by - ay)))
        out.append(cs[-1])
        return out

    for cl in svc_lines:
        try:
            cs = list(cl.line.coords)
        except _GEOM_EXC:
            continue
        if len(cs) < 2:
            continue
        for (vx, vy) in _stations(cs):
            P = Point(vx, vy)
            try:
                cand = tree.query(P.buffer(hw))
            except _GEOM_EXC:
                continue
            for qi in cand:
                si = int(qi)
                ring = rings[si]
                n = len(ring)
                for ei in range(n):
                    ax, ay = ring[ei]
                    bx, by = ring[(ei + 1) % n]
                    dx, dy = bx - ax, by - ay
                    seg2 = dx * dx + dy * dy
                    if seg2 < 1e-9:
                        continue
                    t = ((vx - ax) * dx + (vy - ay) * dy) / seg2
                    if t <= 0.0 or t >= 1.0:
                        continue
                    fx, fy = ax + t * dx, ay + t * dy
                    if math.hypot(fx - vx, fy - vy) > hw:
                        continue
                    L = math.sqrt(seg2)
                    if t * L < _CORNER_TOL_M or (1.0 - t) * L < _CORNER_TOL_M:
                        continue
                    inserts[si][ei].append((t, (fx, fy)))

    if not inserts:
        return 0

    n_added = 0
    for si, by_edge in inserts.items():
        ring = rings[si]
        n = len(ring)
        new_ring = []
        for ei in range(n):
            new_ring.append(ring[ei])
            feet = sorted(by_edge.get(ei, []), key=lambda r: r[0])
            last = None
            for (_t, (fx, fy)) in feet:
                if last is not None and math.hypot(fx - last[0],
                                                   fy - last[1]) < _MERGE_TOL_M:
                    continue
                new_ring.append((fx, fy))
                last = (fx, fy)
                n_added += 1
        if len(new_ring) <= n:
            continue
        try:
            poly = Polygon(new_ring)
            if poly.is_valid and not poly.is_empty:
                targets[si].polygon = poly
        except _GEOM_EXC:
            continue

    if n_added:
        UI.vprint(1, f"  [pav-builder] {icao}: inserted {n_added} service "
                  f"cross-section node(s) on road/service-junction edges from "
                  f"the truck-route spine (spine-first law sampling).")
    return n_added
