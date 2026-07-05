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

A narrow code-A/B arm contributes its 3 % cap.  The ICAO size travels
PER-SEGMENT on each ``apt_dat_reader.TaxiCenterline`` (``seg_sizes``), so the
per-letter cap at the foot is read off the geometry — no name→letter table.
"""

from __future__ import annotations

import heapq
import math
import os
from typing import Callable, Dict, List, Tuple

from auto_patch.config import (
    ANISO_EDGES,
    BUILDING_FULL_FRONTAGE,
    BUILDING_FULL_FRONTAGE_AREA_M2, TAXI_MAX_GRADE, VISIBLE_CHORD_CONNECT,
)
from auto_patch.grade_law import APRON_MAX_GRADE, BUILDING_REACH_CORRIDOR_M
from auto_patch.layout import (
    ROLE_APRON, ROLE_BUILDING, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
)

__all__ = ["building_feasible_levels", "reach_band_unified",
           "runway_edge_anchors"]

# Pavement a building must touch to count as airside-served (else → DEM).
_AIRSIDE_ROLES = frozenset({
    ROLE_APRON, ROLE_JUNCTION, ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
    ROLE_SECONDARY_PARALLEL, ROLE_STUB, ROLE_CROSS_CONNECTOR,
})
_TAXI_HALF_W_M = 7.5       # taxiway half-width corridor (perp split point)
_TOUCH_TOL_M = 2.0         # building↔airside distance to count as "touching"
_MULTI_ROUTE_M = 30.0      # junction-band: widen ceiling over routes within this
_INF = float("inf")


_VIS_BUFFER_M = 0.5        # bridge weld-seam slivers between abutting shapes
_VIS_ON_PAV_FRAC = 0.97    # chord counts as visible if ≥ this fraction is paved
# A reach binding is PHANTOM only when the serving centerline is BOTH far AND
# only reachable across grass.  The CYXY south runway-crossing junction binds a
# centerline 367 m away over 77 % grass; a building/apron vertex a few tens of m
# from its serving centerline across a small grass sliver is NOT phantom (forcing
# IT onto the skeleton band shifts building seats — building12's pad).  The perp
# gate isolates the egregious case without disturbing normal apron-frontage reach.
_PHANTOM_MIN_PERP_M = 100.0


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


def _cl_by_distance(c, cls, tree=None, max_r=None):
    """Iterate centerlines in true-distance order from ``c`` — STRtree-backed
    when a ``tree`` (``STRtree(cls)``) is given, so a query touches the
    handful of nearby lines instead of distance-scanning the whole list (the
    reach band runs ~10k queries × ~500 lines; the full scans were ~half the
    solve time).  With ``max_r`` only lines within that distance are yielded.
    Without a tree this is the plain full sort, so behaviour is identical."""
    if tree is None:
        for L in sorted(cls, key=lambda L: L.distance(c)):
            if max_r is not None and L.distance(c) > max_r:
                break
            yield L
        return
    if max_r is not None:
        try:
            idxs = tree.query(c.buffer(max_r))
        except Exception:                                  # pragma: no cover
            idxs = range(len(cls))
        cand = sorted((cls[int(k)].distance(c), int(k)) for k in idxs)
        for d, k in cand:
            if d <= max_r:
                yield cls[k]
        return
    # expanding rings: everything within r is in the candidate set, so the
    # ≤r prefix of each round is exact global distance order.
    seen: set = set()
    r = 60.0
    while True:
        try:
            idxs = tree.query(c.buffer(r))
        except Exception:                                  # pragma: no cover
            for L in sorted(cls, key=lambda L: L.distance(c)):
                yield L
            return
        cand = sorted((cls[int(k)].distance(c), int(k))
                      for k in idxs if int(k) not in seen)
        for d, k in cand:
            if d <= r:
                seen.add(k)
                yield cls[k]
        if r > 1e5:                    # exhausted: yield any stragglers
            rest = sorted((cls[k].distance(c), k)
                          for k in range(len(cls)) if k not in seen)
            for _d, k in rest:
                yield cls[k]
            return
        r *= 4.0



def _paved_frac(chord, vis) -> float:
    """Fraction of ``chord`` on pavement, by VECTORIZED point sampling
    (``shapely.contains_xy`` on the prepared pavement — one C call) instead
    of an exact line∩polygon overlay (~0.5 ms each).  The overlay was 60 %
    of the whole CYUL build: the visible-chord walk tries ~50 candidates
    per node on a fragmented centerline network and paid it on every miss
    (1.27 M calls, 650 s).  Per-point *Python* shapely calls are no cheaper
    than the overlay (call overhead dominates) — the batch call is.
    Sampling at ≤1 m (capped 96 points) resolves ``_VIS_ON_PAV_FRAC``
    comfortably (a 3 % gap on a 30 m chord is ~1 m)."""
    import numpy as _np
    import shapely as _sh
    coords = list(chord.coords)
    (ax, ay), (bx, by) = coords[0], coords[-1]
    L = chord.length
    if L < 1e-9:
        return 1.0
    n = min(96, max(8, int(L)))
    t = (_np.arange(n) + 0.5) / n
    geom = getattr(vis, "context", vis)
    try:
        _sh.prepare(geom)          # idempotent; cached on the geometry
        hits = _sh.contains_xy(geom, ax + (bx - ax) * t, ay + (by - ay) * t)
        return float(hits.mean())
    except Exception:              # pragma: no cover — old shapely fallback
        from shapely.geometry import Point as _P
        hit = sum(1 for k in range(n)
                  if vis.contains(_P(ax + (bx - ax) * t[k],
                                     ay + (by - ay) * t[k])))
        return hit / n


def _nearest_visible_centerline(c, cls, vis, tree=None):
    """The nearest centerline to point ``c`` whose connecting chord stays within
    pavement (``vis``).  Falls back to the straight-line nearest if none is
    visible (e.g. a building wholly off pavement — the caller's touch test has
    already gated that out).  ``tree``: optional ``STRtree(cls)`` (see
    :func:`_cl_by_distance`)."""
    from shapely.geometry import LineString
    from shapely.ops import nearest_points
    first = None
    for ln in _cl_by_distance(c, cls, tree):
        if first is None:
            first = ln
        foot = nearest_points(ln, c)[0]
        chord = LineString([(c.x, c.y), (foot.x, foot.y)])
        if chord.length < 1e-6 or vis.contains(chord):
            return ln
        # tolerate tiny seam gaps: accept when ≥ _VIS_ON_PAV_FRAC is paved
        try:
            if _paved_frac(chord, vis) >= _VIS_ON_PAV_FRAC:
                return ln
        except Exception:                                  # pragma: no cover
            pass
    return first if first is not None else min(
        cls, key=lambda L: L.distance(c))


def _chord_on_pavement(c, foot, vis):
    """True when the straight chord from ``c`` to its centerline ``foot`` stays on
    pavement (≥ ``_VIS_ON_PAV_FRAC`` paved) — the SAME visible-chord rule
    :func:`_nearest_visible_centerline` uses to accept a connection.  A chord that
    mostly crosses grass is a PHANTOM route (you cannot taxi it), so the reach band
    must not be bound through it."""
    from shapely.geometry import LineString
    chord = LineString([(c.x, c.y), (foot.x, foot.y)])
    if chord.length < 1e-6 or vis.contains(chord):
        return True
    try:
        return _paved_frac(chord, vis) >= _VIS_ON_PAV_FRAC
    except Exception:                                      # pragma: no cover
        return False


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

    # Value-seeded reach FIELDS (perf 2026-07-04): the per-anchor loop
    # ran a full Dijkstra PER anchor (550 at KDFW → 140 s of the build,
    # cProfile) yet only ever consumed ``min(ae + dist)`` /
    # ``max(ae − dist)`` across anchors.  ONE multi-source pass per
    # field — every anchor seeded at its own elevation — settles each
    # node at its optimal (anchor, path) pair: the same min/max by
    # commutation.  ``dist`` is accumulated separately and the field
    # value formed as ``ae + dist`` (one addition, exactly the
    # per-anchor expression) so patches stay byte-identical.  Strict
    # settled-set pop guard — see the lazy-Dijkstra re-expand hang.
    def _anchor_value_field(sign):
        best: dict = {}
        pq = [((ae if sign > 0 else -ae), 0.0, ae, k)
              for k, ae in anchor_elev.items() if k in adj]
        heapq.heapify(pq)
        while pq:
            _key, dd, ae, u = heapq.heappop(pq)
            if u in best:
                continue
            best[u] = (ae + dd) if sign > 0 else (ae - dd)
            for (v, budget) in adj.get(u, ()):
                if v in best:
                    continue
                nd = dd + budget
                heapq.heappush(
                    pq, (((ae + nd) if sign > 0 else -(ae - nd)),
                         nd, ae, v))
        return best

    node_ceil = _anchor_value_field(+1)
    node_floor = _anchor_value_field(-1)
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
        slack = APRON_MAX_GRADE * off
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

    # Value-seeded reach FIELDS (perf 2026-07-04, same collapse as the
    # skeleton band above): the per-anchor Dijkstras were only ever
    # consumed as ``min over anchors (ae + dist + extras)`` /
    # ``max over anchors (ae − dist − extras)`` with anchor-independent
    # extras, so ONE multi-source pass per field replaces |anchors|
    # full-graph passes.  Each field stores the winning ``(dist, ae)``
    # PAIR so ``_band_via`` can form its candidate values with exactly
    # the original float expression (ae + ((dist + foot) + perp)) —
    # patches stay byte-identical.
    def _runway_value_field(sign):
        best: dict = {}
        pq = [((float(ae) if sign > 0 else -float(ae)), 0.0, float(ae), k)
              for (k, ae) in G.runway_anchor.items()]
        heapq.heapify(pq)
        while pq:
            _key, dd, ae, u = heapq.heappop(pq)
            if u in best:
                continue
            best[u] = (dd, ae)
            for (v, budget) in G.spine_adj.get(u, ()):
                if v in best:
                    continue
                nd = dd + budget
                heapq.heappush(
                    pq, (((ae + nd) if sign > 0 else -(ae - nd)),
                         nd, ae, v))
        return best

    ceiling_field = _runway_value_field(+1)
    floor_field = _runway_value_field(-1)
    if not ceiling_field:
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
    # PER-SEGMENT cap: keep the TaxiCenterline so the foot climb credits the cap of
    # the SEGMENT the foot lands on (no name→letter table; a route may change width
    # along its length).  ``cap_of`` maps a line's identity to its TaxiCenterline.
    cls_tcl = [cl for cl in (getattr(layout, "apt_taxi_centerlines", None) or [])
               if cl.line is not None and not cl.line.is_empty
               and not cl.is_service]
    if not cls_tcl:
        return lambda x, y: None
    cls = [cl.line for cl in cls_tcl]
    cap_of = {id(cl.line): cl for cl in cls_tcl}
    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None
    # STRtree over the centerlines: every band query needs them in distance
    # order (nearest-visible + the multi-route widening); the naive per-query
    # full sort was ~half the solve time (~10k queries × ~500 lines).
    try:
        from shapely.strtree import STRtree as _STRtree
        cl_tree = _STRtree(cls) if len(cls) > 8 else None
    except Exception:                                      # pragma: no cover
        cl_tree = None

    # ANISOTROPIC EDGES: a JUNCTION is graded UNIFORMLY at the taxi cap (its body
    # is the spine's per-letter cap, not 1 %), so a junction foot point beyond the
    # taxiway corridor must climb at the taxi cap too — NOT drop to APRON_MAX_GRADE.
    # Without this the band under-credits the junction interior and false-flags the
    # very junction-body points the new edge law lets climb (docs/anisotropic_edge_
    # handling_plan.md Phase 4).  Prepared junction union; tested per query.  Gate
    # OFF ⇒ ``junc_zone`` is None and the foot climb is byte-identical (apron drop).
    junc_zone = None
    if ANISO_EDGES:
        try:
            from shapely.ops import unary_union
            from shapely.prepared import prep
            jpolys = [s.polygon for s in layout.shapes
                      if s.role == ROLE_JUNCTION and s.polygon is not None
                      and not s.polygon.is_empty]
            if jpolys:
                junc_zone = prep(unary_union(jpolys))
        except Exception:                                     # pragma: no cover
            junc_zone = None

    # EDGE-SKELETON fallback (user 2026-06-28): no-centerline pavement returns
    # None below; the skeleton band reaches it over the welded edge graph +
    # runway-contact anchors.  Used ONLY where the centerline path is None, so
    # centerlined airports are unchanged.  Gate O4_SKELETON_REACH=0 disables.
    _skel_band = (_build_skeleton_band(layout, G)
                  if os.environ.get("O4_SKELETON_REACH", "1") == "1" else None)

    def _fallback(x, y):
        return _skel_band(x, y) if _skel_band is not None else None

    def _band_via(c, x, y, ln, in_junc):
        """``(floor, ceil)`` reachable for point ``c`` SERVED by centerline ``ln``,
        or None if ``ln`` cannot serve it (no anchored spine node).  ``in_junc`` ⇒
        the climb beyond the taxiway corridor stays at the taxi cap (uniform
        junction), not the apron cap."""
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
            return None
        _tcl = cap_of.get(id(ln))
        ecap = (taxi_grade_cap_for_letter(_tcl.size_at_arc(sp))
                if _tcl is not None else TAXI_MAX_GRADE)
        beyond_cap = ecap if in_junc else APRON_MAX_GRADE
        perp_climb = (ecap * min(perp, _TAXI_HALF_W_M)
                      + beyond_cap * max(0.0, perp - _TAXI_HALF_W_M))
        # Candidate values from the collapsed fields: each field entry is
        # the winning (dist, ae) pair for its node, and for fixed foot
        # extras the same anchor wins with the extras added — so forming
        # ``ae + ((dist + foot) + perp)`` here reproduces the per-anchor
        # loop's minima to the float bit.
        floor, ceil = -_INF, _INF
        for (k, foot) in ((kA, A[1]), (kB, B[1])):
            fc = ceiling_field.get(k)
            if fc is not None:
                dd, ae = fc
                ceil = min(ceil, ae + ((dd + ecap * foot) + perp_climb))
            ff = floor_field.get(k)
            if ff is not None:
                dd, ae = ff
                floor = max(floor, ae - ((dd + ecap * foot) + perp_climb))
        if ceil >= _INF:
            return None
        return (floor, ceil)

    def band(x, y):
        c = Point(x, y)
        ln = (_nearest_visible_centerline(c, cls, vis, tree=cl_tree)
              if vis is not None
              else next(_cl_by_distance(c, cls, cl_tree),
                        min(cls, key=lambda L: L.distance(c))))
        perp = c.distance(ln)
        if vis is not None and perp > _PHANTOM_MIN_PERP_M:
            from shapely.ops import nearest_points
            foot = nearest_points(ln, c)[0]
            if not _chord_on_pavement(c, foot, vis):
                # PHANTOM binding: the only nearby centerline is FAR and only
                # reachable ACROSS GRASS (CYXY south runway-crossing junction →
                # centerline D, 367 m, 23 % paved).  Binding the reach band to it
                # pins the floor far above the node's real route (3.3 m high → a
                # 36.7 % body cliff).  Reach it over the welded-edge SKELETON
                # instead — that taxiway over its OWN pavement.
                return _fallback(x, y)
        in_junc = junc_zone is not None and junc_zone.contains(c)
        prim = _band_via(c, x, y, ln, in_junc)
        if prim is None:
            return _fallback(x, y)
        floor, ceil = prim
        # ANISOTROPIC EDGES: the within-shape law lets a point be reached from ANY
        # nearby converging spine route, but the band above used the NEAREST
        # centerline only — under-crediting the ceiling and false-flagging the
        # junction/apron interiors the new law lets climb.  Widen to the
        # most-reachable route over the other on-pavement centerlines within a
        # BOUNDED reach (so a genuinely-too-high point still flags — no far route
        # reaches it) and only across a VISIBLE on-pavement chord, so band and law
        # agree without over-loosening.
        if junc_zone is not None:
            from shapely.ops import nearest_points
            others = [L for L in _cl_by_distance(c, cls, cl_tree,
                                                 max_r=_MULTI_ROUTE_M)
                      if L is not ln][:3]
            for L in others:
                if vis is not None:
                    foot = nearest_points(L, c)[0]
                    if not _chord_on_pavement(c, foot, vis):
                        continue
                r = _band_via(c, x, y, L, in_junc)
                if r is not None:
                    floor = min(floor, r[0])
                    ceil = max(ceil, r[1])
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
            if _paved_frac(chord, vis) >= _VIS_ON_PAV_FRAC:
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
        cls = [cl.line for cl in
               (getattr(layout, "apt_taxi_centerlines", None) or [])
               if cl.line is not None and not cl.line.is_empty
               and not cl.is_service]
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
                               BUILDING_REACH_CORRIDOR_M)
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
