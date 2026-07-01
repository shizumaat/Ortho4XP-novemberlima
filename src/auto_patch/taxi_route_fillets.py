#!/usr/bin/env python3
"""Synthetic taxiway turn fillets (user 2026-06-30, gate O4_TAXI_FILLET).

The apt.dat 1201/1202 routing network is a graph of STRAIGHT segments between
sparse nodes: where two taxi routes meet at a junction they simply share a node,
with NO connecting arc — so a plane's turn from one route to another is modelled
as a hard corner.  Real taxiways ALWAYS fillet that turn on a radius set by the
taxiway size AND the sharpness of the turn.  The sparse data rarely carries the
arc, and its absence makes the junction grade badly (the climb has no smooth path
across the corner).

This pass adds those missing arcs to the ROUTES GRAPH (``apt_taxi_centerlines``)
*before* the spine is generated, so the existing junction-centerline-spine slice
cuts them into the junctions automatically — no junction or pavement code touched.

Radius model (user 2026-06-30, from Google-Earth verification):
  * base radius per ICAO code letter (the size AT the junction, not the whole
    route's widest — a small connector meeting a big taxiway keeps its own size);
  * SCALED by the turn (deflection) angle: a SHARP acute turn needs a LARGER
    sweep further from the vertex (aircraft can't hinge a sharp turn tightly); a
    ~90° turn a smaller one.  The old fixed tangent cap did the opposite and is
    gone — the tangent offset is bounded only by the routes' own length.

Dedup: skip if an existing centerline already runs along the arc (an arc is
already present, or one within a couple of metres), so we never double up.
"""
from __future__ import annotations

import math
import os
from collections import defaultdict

from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from .apt_dat_reader import TaxiCenterline

# Centerline turn radius (m) at a reference 90° turn, by ICAO code letter
# (representative; ICAO Annex 14 / Doc 9157 / FAA AC 150/5300-13B — pin later).
_R90_BY_SIZE = {"A": 12.0, "B": 16.0, "C": 22.0, "D": 30.0, "E": 36.0, "F": 45.0,
                "": 20.0}
_SIZE_ORDER = "ABCDEF"

# Radius scales ~linearly with the turn angle: a 90° turn is the reference (×1),
# a sharp acute turn is larger, a gentle turn smaller — clamped to a sane band.
_TURN_REF_DEG = 90.0
_TURN_FACTOR_LO = 0.6
_TURN_FACTOR_HI = 2.4

# Only join THROUGH taxi routes — a route shorter than this is a junction-internal
# connector stub, not a taxiway a plane turns between, so it is not a fillet
# endpoint (user 2026-06-30: fillet the real taxiways, not the short connectors).
_MIN_THROUGH_LEN_M = 40.0
_CLUSTER_TOL_M = 2.5      # route ends within this = one junction
_MIN_ALPHA_DEG = 25.0     # skip fold-backs (rays too closely aligned)
_MAX_ALPHA_DEG = 155.0    # skip straight-throughs (no meaningful turn)
_DEDUP_TOL_M = 3.0        # an existing centerline this near the arc ⇒ already there
_ARC_NODES = 7            # samples along each fillet arc
_TANGENT_MAX_M = 120.0    # hard sanity bound on tangent offset (route len clamps first)

# Painted-arc extraction (geometry, NOT paint codes): a real fillet rides both
# route centerlines; an edge line sits a half-width off, so it fails the tol.
_PAINT_END_TOL_M = 7.0       # an arc END must reach within this of a route centerline
_PAINT_SEARCH_M = 55.0       # painted lines within this of the junction are candidates
_PAINT_TURN_TOL_DEG = 45.0   # extracted arc's total turn must ≈ the turn's deflection
_PAINT_MIN_CURVE_DEG = 15.0  # a real arc actually curves (rejects straight paint)
_PAINT_THROUGH_J_M = 20.0    # the arc must pass this near the junction


def _unit(dx, dy):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-9 else (0.0, 0.0)


def _wider(a: str, b: str) -> str:
    ia = _SIZE_ORDER.find(a) if a in _SIZE_ORDER else -1
    ib = _SIZE_ORDER.find(b) if b in _SIZE_ORDER else -1
    return a if ia >= ib else b


def _narrower(a: str, b: str) -> str:
    """The smaller ICAO code of the two (a turn between taxiways is governed by
    the NARROWER one — the smaller aircraft using it).  Unknown ("") yields to a
    known size."""
    ia = _SIZE_ORDER.find(a) if a in _SIZE_ORDER else 99
    ib = _SIZE_ORDER.find(b) if b in _SIZE_ORDER else 99
    if ia == 99 and ib == 99:
        return ""
    return a if ia <= ib else b


def _arc_points(J, ua, ub, R, T):
    """Sample the tangent fillet arc of radius ``R`` (tangent length ``T``) at the
    corner ``J`` between outgoing rays ``ua``/``ub``.  ``None`` if degenerate."""
    Pa = (J[0] + T * ua[0], J[1] + T * ua[1])
    Pb = (J[0] + T * ub[0], J[1] + T * ub[1])
    bis = _unit(ua[0] + ub[0], ua[1] + ub[1])
    if bis == (0.0, 0.0):
        return None
    half = math.acos(max(-1.0, min(1.0, ua[0]*ub[0] + ua[1]*ub[1]))) / 2.0
    if math.sin(half) < 1e-6:
        return None
    C = (J[0] + (R / math.sin(half)) * bis[0], J[1] + (R / math.sin(half)) * bis[1])
    a0 = math.atan2(Pa[1] - C[1], Pa[0] - C[0])
    a1 = math.atan2(Pb[1] - C[1], Pb[0] - C[0])
    d = a1 - a0
    while d > math.pi:
        d -= 2 * math.pi
    while d < -math.pi:
        d += 2 * math.pi
    pts = [(C[0] + R * math.cos(a0 + d*k/(_ARC_NODES-1)),
            C[1] + R * math.sin(a0 + d*k/(_ARC_NODES-1))) for k in range(_ARC_NODES)]
    pts[0], pts[-1] = Pa, Pb
    return pts


def _total_turn_deg(pts) -> float:
    tot = 0.0
    for k in range(1, len(pts) - 1):
        d1 = math.atan2(pts[k][1]-pts[k-1][1], pts[k][0]-pts[k-1][0])
        d2 = math.atan2(pts[k+1][1]-pts[k][1], pts[k+1][0]-pts[k][0])
        t = abs(math.degrees(d2 - d1))
        tot += min(t, 360.0 - t)
    return tot


def _painted_arc(J, lineA, lineB, deflection_deg, painted, ptree):
    """The real fillet arc from the painted lines for the A→B turn at ``J``, or
    ``None``.  Ruleset (paint codes ignored): a painted sub-path that CURVES near
    ``J`` by ≈ the turn's deflection, and whose two ENDS reach two DIFFERENT route
    centerlines (one A, one B) within ``_PAINT_END_TOL_M``.  An edge line sits a
    half-width off the centerlines, so its ends never reach both (fails the tol);
    the curve test rejects a straight painted segment that merely spans two routes.
    Painted junction fillets are usually standalone arc segments, so the whole
    line (or the span between the two route-reaching ends) is the arc."""
    if ptree is None:
        return None
    Jp = Point(*J)
    best = None
    for idx in ptree.query(Jp.buffer(_PAINT_SEARCH_M)):
        pts = list(painted[int(idx)].coords)
        if len(pts) < 3:
            continue
        # vertices reaching route A / route B
        ra = [k for k, p in enumerate(pts)
              if lineA.distance(Point(p)) < _PAINT_END_TOL_M]
        rb = [k for k, p in enumerate(pts)
              if lineB.distance(Point(p)) < _PAINT_END_TOL_M]
        if not ra or not rb:
            continue
        # try spans whose ends reach A and B (extreme vertices of each set)
        for a in (min(ra), max(ra)):
            for b in (min(rb), max(rb)):
                lo, hi = sorted((a, b))
                if hi - lo < 2:
                    continue
                sub = pts[lo:hi+1]
                arc = LineString(sub)
                if arc.length < 2.0 or arc.distance(Jp) > _PAINT_THROUGH_J_M:
                    continue
                turn = _total_turn_deg(sub)
                if turn < _PAINT_MIN_CURVE_DEG:
                    continue                        # straight ⇒ not a fillet
                if abs(turn - deflection_deg) > _PAINT_TURN_TOL_DEG:
                    continue
                # ends must reach DIFFERENT routes
                ea_a = lineA.distance(Point(pts[lo])) < _PAINT_END_TOL_M
                eb_b = lineB.distance(Point(pts[hi])) < _PAINT_END_TOL_M
                ea_b = lineB.distance(Point(pts[lo])) < _PAINT_END_TOL_M
                eb_a = lineA.distance(Point(pts[hi])) < _PAINT_END_TOL_M
                if not ((ea_a and eb_b) or (ea_b and eb_a)):
                    continue
                if best is None or arc.length < best.length:
                    best = arc
    return best


def add_junction_fillet_arcs(layout, icao: str = "") -> int:
    """Append synthetic turn-fillet arcs to ``layout.apt_taxi_centerlines``.
    Returns the number of arcs added."""
    if os.environ.get("O4_TAXI_FILLET", "0") != "1":
        return 0
    cls = list(getattr(layout, "apt_taxi_centerlines", []) or [])
    if not cls:
        return 0

    existing_geoms = []
    rep: dict = {}
    # Each route's OWN terminal-segment size at each end — keyed by ROUTE, never by
    # the shared junction coord (else a narrow stem and the wide crossbar meeting
    # at a T share a coord and the stem wrongly reads as the wider crossbar).
    rend: dict = {}                     # route_key -> {"s": start_size, "e": end_size}

    def _close(a, b):
        return abs(a[0]-b[0]) < 0.5 and abs(a[1]-b[1]) < 0.5

    for tcl in cls:
        ln = getattr(tcl, "line", None)
        if ln is not None and not ln.is_empty:
            existing_geoms.append(ln)
        if getattr(tcl, "is_service", False) or ln is None or ln.is_empty:
            continue
        rl = tcl.chained_line
        if rl is None or rl.is_empty or rl.length <= 1e-6:
            continue
        key = id(rl)
        rep[key] = rl
        ss = tcl.seg_sizes or []
        if not ss:
            continue
        lcs = list(ln.coords)
        rcs = list(rl.coords)
        d = rend.setdefault(key, {"s": "", "e": ""})
        # a piece touching a route END contributes that end's terminal size
        if _close(lcs[0], rcs[0]):
            d["s"] = _wider(d["s"], ss[0])
        if _close(lcs[-1], rcs[0]):
            d["s"] = _wider(d["s"], ss[-1])
        if _close(lcs[0], rcs[-1]):
            d["e"] = _wider(d["e"], ss[0])
        if _close(lcs[-1], rcs[-1]):
            d["e"] = _wider(d["e"], ss[-1])

    # Route ends: (point, unit-tangent-away-from-end, size, route_key, route_len).
    # Skip short connector routes entirely — only through taxiways are filleted.
    ends = []
    for key, rl in rep.items():
        if rl.length < _MIN_THROUGH_LEN_M:
            continue
        cs = list(rl.coords)
        if len(cs) < 2:
            continue
        d = rend.get(key, {"s": "", "e": ""})
        ends.append((cs[0], _unit(cs[1][0]-cs[0][0], cs[1][1]-cs[0][1]),
                     d["s"], key, rl.length))
        ends.append((cs[-1], _unit(cs[-2][0]-cs[-1][0], cs[-2][1]-cs[-1][1]),
                     d["e"], key, rl.length))

    # Cluster ends into junctions.
    clusters: list[list] = []
    for e in ends:
        for c in clusters:
            if math.hypot(e[0][0]-c[0][0][0], e[0][1]-c[0][0][1]) <= _CLUSTER_TOL_M:
                c.append(e)
                break
        else:
            clusters.append([e])

    tree = STRtree(existing_geoms) if existing_geoms else None
    painted = getattr(layout, "_painted_lines_m", None) or []
    ptree = STRtree(painted) if painted else None
    added = adopted = skipped_dup = 0
    new_cls: list = []
    for c in clusters:
        if len(c) < 2:
            continue
        J = (sum(e[0][0] for e in c) / len(c), sum(e[0][1] for e in c) / len(c))
        for i in range(len(c)):
            for j in range(i + 1, len(c)):
                ea, eb = c[i], c[j]
                if ea[3] == eb[3]:
                    continue                       # same route's two ends
                ua, ub = ea[1], eb[1]
                dot = max(-1.0, min(1.0, ua[0]*ub[0] + ua[1]*ub[1]))
                alpha = math.acos(dot)             # angle between the outgoing rays
                if not (math.radians(_MIN_ALPHA_DEG) < alpha
                        < math.radians(_MAX_ALPHA_DEG)):
                    continue                       # fold-back / straight-through
                size = _narrower(ea[2], eb[2])   # turn governed by the smaller taxiway
                turn_deg = 180.0 - math.degrees(alpha)      # deflection
                # PREFER the REAL painted arc (correct radius/position, only where
                # a real curve exists); synthesize only as fallback.
                arc = _painted_arc(J, rep[ea[3]], rep[eb[3]], turn_deg, painted, ptree)
                is_paint = arc is not None
                if not is_paint:
                    # Radius is the size's base — NOT scaled by the turn angle.  A
                    # sharp/acute turn already gets a far-out centre for free from
                    # the tangent geometry (T = R/tan(α/2) grows as α shrinks).
                    R = _R90_BY_SIZE.get(size, _R90_BY_SIZE[""])
                    T = R / math.tan(alpha / 2.0)
                    Tmax = min(ea[4] * 0.8, eb[4] * 0.8, _TANGENT_MAX_M)
                    if T > Tmax:
                        T = Tmax
                        R = T * math.tan(alpha / 2.0)
                    if R < 3.0:
                        continue
                    pts = _arc_points(J, ua, ub, R, T)
                    if pts is None:
                        continue
                    arc = LineString(pts)
                if arc.is_empty or arc.length < 1.0:
                    continue
                if tree is not None:
                    mid = arc.interpolate(0.5, normalized=True)
                    if any(existing_geoms[k].distance(mid) < _DEDUP_TOL_M
                           for k in tree.query(mid.buffer(_DEDUP_TOL_M))):
                        skipped_dup += 1
                        continue
                nseg = max(1, len(arc.coords) - 1)
                new_cls.append(TaxiCenterline(
                    line=arc, seg_sizes=[size] * nseg, is_service=False,
                    name="fillet_paint" if is_paint else "fillet", route_line=None))
                existing_geoms.append(arc)
                tree = STRtree(existing_geoms)
                added += 1
                if is_paint:
                    adopted += 1

    if new_cls:
        layout.apt_taxi_centerlines = cls + new_cls
    if added or skipped_dup:
        import O4_UI_Utils as UI
        UI.vprint(1, f"  [pav-builder] {icao}: added {added} taxiway turn-fillet "
                  f"arc(s) to the route graph ({adopted} from painted centerlines, "
                  f"{added - adopted} synthesized; {skipped_dup} skipped — already present).")
    return added
