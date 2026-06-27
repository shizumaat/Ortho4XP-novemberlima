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

__all__ = ["insert_lateral_spine_nodes", "densify_junction_edges"]

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
        ln = entry[0] if isinstance(entry, (tuple, list)) else entry
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
