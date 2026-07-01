"""Pavement-based taxi-spine synthesis — a navigable airport-diagram network
derived from ``pav_union`` alone.

Design invariant (user 2026-07-01): an aircraft starting anywhere on the
spine must be able to reach every part of it by rolling ALONG spine edges —
no right-angle turns, wheels never leaving the line.  So the spine is a
curvature-limited network: one clean centerline per taxiway, straights
passing straight THROUGH junctions, and every junction transition made by a
tangent fillet arc, exactly like the centerline paint on an airport diagram.

Construction is from the pavement footprint ONLY (painted centerlines and
apt.dat routes are inconsistent across airports/packages; the apt.dat route
graph is used by the diagnostic tooling as a cross-reference, never as a
construction input):

1. **Medial skeleton** of ``pav_union − runways − buildings``
   (``pav_skeleton``) provides topology, coverage, junction nodes, and the
   local half-width at every vertex.
2. **Through-merge**: chains whose ends meet near-collinearly at a node are
   concatenated, so a straight taxiway continues through the junction as
   one spine.
3. **Fairing**: each chain is simplified to its dominant straight runs
   (Douglas-Peucker) and every bend is replaced with a tangent arc whose
   radius comes from the local half-width (FAA AC 150/5300-13B / EASA
   CS-ADR-DSN turn-radius classes), shrunk until the arc fits the pavement.
   Gentle medial wobble collapses to true straights; real curves become arc
   chains an aircraft can follow.
4. **Junction arcs**: at each skeleton junction NODE (never at arbitrary
   geometric crossings — that way lies duplication), every pair of incident
   branch directions gets ONE tangent fillet arc if it fits the pavement.
   A turn whose fillet cannot fit is not a movement; traffic goes around.
5. **Runway contacts**: a chain meeting the runway edge square keeps its
   single edge node.  A DIAGONAL (rapid-exit style, 25–65° to the runway
   axis) additionally gets the sharp-turn arc — tangent to the diagonal and
   to the runway edge line — landing a second edge node in the sharp-side
   fillet wedge (the straight contact IS the high-speed connection).
6. **Buildings**: subtracted from the pavement before skeletonizing (a
   spine never enters one).  Small buildings (< ``SMALL_BUILDING_M2``) get
   a lead-in stub to the midpoint of their longest pavement-facing edge;
   larger buildings/terminals get a merged-cluster perimeter ring at
   ``TERMINAL_SETBACK_M``, kept only where it sees its building across
   pavement and does not ride an existing lane.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, unary_union

from .pav_skeleton import build_pavement_skeleton, _polygons

# ── provisional standards constants ──────────────────────────────────────────
# Taxiway centerline turn radius by local lane HALF-WIDTH (m) — a pavement-
# derived proxy for the ICAO code letter (code A/B lanes are ~7.5–10.5 m wide,
# C ~18 m, D/E ~23 m, F ~25 m+; shoulders widen the paved half-width).
# Radii follow FAA AC 150/5300-13B / EASA CS-ADR-DSN fillet design.
# PROVISIONAL — move to config.py + docs/STANDARDS.md at pipeline wiring.
def _radius_for_halfwidth(w: float) -> float:
    if w < 5.0:
        return 22.5      # code A/B
    if w < 8.0:
        return 30.0      # code C
    if w < 12.0:
        return 45.0      # code D/E
    return 51.0          # code F

_MIN_FILLET_R = 12.0      # below this a bend stays a plain corner
_TURN_MIN_DEG = 12.0      # bends flatter than this are "straight"
_TURN_MAX_DEG = 150.0     # sharper pairs are turn-backs, not fillets
_THROUGH_MAX_DEG = 40.0   # body-axis pairs this collinear merge into a through
_CHORD_CLEAR_M = 2.0      # a straight chord needs this edge clearance to win
_NODE_TOL_M = 0.75        # chain endpoints within this share a node
# Runway-contact classification (angle between spine and runway axis).
_DIAG_MIN_DEG = 20.0
_DIAG_MAX_DEG = 65.0

SMALL_BUILDING_M2 = 2000.0   # user 2026-07-01: lead-in stub vs perimeter ring
TERMINAL_SETBACK_M = 100.0   # perimeter-ring distance from large buildings
_STUB_MAX_REACH_M = 80.0
_RING_MIN_ARC_M = 30.0
_RING_LANE_CLEAR_M = 15.0    # ring stretches this close to a lane are skipped
_FAIR_SIMPLIFY_M = 1.2       # DP tolerance: medial wobble below this = straight


@dataclass
class SpineWay:
    line: LineString
    kind: str          # line | arc | rwy_turn | building_stub | building_ring
    halfwidth: float = 0.0


# ── geometry helpers ─────────────────────────────────────────────────────────

def _unit(dx: float, dy: float):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-9 else (0.0, 0.0)


def _angle_deg(u, v) -> float:
    d = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
    return math.degrees(math.acos(d))


def _end_dir(coords, at_start: bool, back_m: float = 20.0):
    """Unit travel direction ARRIVING at the given end of the chain."""
    cs = np.asarray(coords)
    if at_start:
        cs = cs[::-1]
    acc, i = 0.0, len(cs) - 1
    while i > 0 and acc < back_m:
        acc += float(np.hypot(*(cs[i] - cs[i - 1])))
        i -= 1
    v = cs[-1] - cs[i]
    return _unit(float(v[0]), float(v[1]))


def _arc(center, r, a0, a1, ccw: bool, step_m: float = 4.0):
    if ccw:
        while a1 < a0:
            a1 += 2 * math.pi
    else:
        while a1 > a0:
            a1 -= 2 * math.pi
    n = max(3, int(abs(a1 - a0) * r / step_m) + 1)
    ts = np.linspace(a0, a1, n)
    return [(center[0] + r * math.cos(t), center[1] + r * math.sin(t))
            for t in ts]


def _fillet_between(P, u_in, u_out, r):
    """Tangent arc for a corner at ``P``: arrive along unit ``u_in`` (into P),
    depart along ``u_out`` (out of P).  Returns (arc_coords, tangent_len) or
    (None, 0) when the corner is effectively straight / a U-turn."""
    gamma = math.acos(max(-1.0, min(1.0,
        u_in[0] * u_out[0] + u_in[1] * u_out[1])))   # deflection, 0=straight
    if gamma < math.radians(_TURN_MIN_DEG) or gamma > math.radians(168.0):
        return None, 0.0
    t = r * math.tan(gamma / 2.0)
    ta = (P[0] - u_in[0] * t, P[1] - u_in[1] * t)
    tb = (P[0] + u_out[0] * t, P[1] + u_out[1] * t)
    cross = u_in[0] * u_out[1] - u_in[1] * u_out[0]
    sgn = 1.0 if cross > 0 else -1.0
    n_in = (-u_in[1] * sgn, u_in[0] * sgn)
    c = (ta[0] + n_in[0] * r, ta[1] + n_in[1] * r)
    a0 = math.atan2(ta[1] - c[1], ta[0] - c[0])
    a1 = math.atan2(tb[1] - c[1], tb[0] - c[0])
    return _arc(c, r, a0, a1, ccw=(sgn > 0)), t


def _local_dirs(line: LineString, P: Point, window: float = 12.0):
    """Travel directions available AT ``P`` on the line (both signs at an
    interior point, one at an end)."""
    s = line.project(P)
    a = line.interpolate(max(0.0, s - window))
    b = line.interpolate(min(line.length, s + window))
    t = _unit(b.x - a.x, b.y - a.y)
    if t == (0.0, 0.0):
        return []
    dirs = []
    if s > 3.0:
        dirs.append(t)
    if s < line.length - 3.0:
        dirs.append((-t[0], -t[1]))
    return dirs


# ── steps 2-3: through-merge + fairing ───────────────────────────────────────

def _merge_through(chains):
    """Concatenate chains whose ends meet near-collinearly (a straight
    taxiway continues through the junction).  Pairing at each node is by
    BEST continuation angle over all end pairs, not first match — at a
    dual-lane island throat the tip-wrap chain also meets the lane at a
    shallow angle, and a greedy first match can steal the lane's true
    continuation.  Collinearity is judged from the chain BODY (45 m back):
    the medial arm curves into the throat, the design axis does not.
    ``chains`` = [[coords, radii]]."""
    def _key(tip):
        return (round(tip[0] / _NODE_TOL_M), round(tip[1] / _NODE_TOL_M))

    changed = True
    while changed:
        changed = False
        ends: dict = {}
        for ci, ch in enumerate(chains):
            if ch is None:
                continue
            for at_start in (True, False):
                tip = ch[0][0] if at_start else ch[0][-1]
                ends.setdefault(_key(tip), []).append((ci, at_start))
        for lst in ends.values():
            if len(lst) < 2:
                continue
            cand = []
            for a in range(len(lst)):
                for b in range(a + 1, len(lst)):
                    ia, sa = lst[a]
                    ib, sb = lst[b]
                    if ia == ib or chains[ia] is None or chains[ib] is None:
                        continue
                    u = _end_dir(chains[ia][0], at_start=sa, back_m=45.0)
                    v = _end_dir(chains[ib][0], at_start=sb, back_m=45.0)
                    ang = _angle_deg(u, (-v[0], -v[1]))
                    if ang <= _THROUGH_MAX_DEG:
                        cand.append((ang, ia, sa, ib, sb))
            used: set = set()
            for ang, ia, sa, ib, sb in sorted(cand, key=lambda t: t[0]):
                if ia in used or ib in used \
                        or chains[ia] is None or chains[ib] is None:
                    continue
                used.add(ia)
                used.add(ib)
                A, RA = chains[ia]
                B, RB = chains[ib]
                a_c, a_r = (A[::-1], RA[::-1]) if sa else (A, RA)
                b_c, b_r = (B, RB) if sb else (B[::-1], RB[::-1])
                chains[ia] = [np.vstack([a_c, b_c[1:]]),
                              np.concatenate([a_r, b_r[1:]])]
                chains[ib] = None
                changed = True
            if changed:
                break                     # endpoint map is stale; rebuild
    return [c for c in chains if c is not None]


def _chord_straighten(coords, radii, chord_ok):
    """THE "straight path" rule: greedily replace each stretch of the chain
    with the longest straight CHORD that (a) fits the pavement with edge
    clearance and (b) stays laterally near the medial (no shortcutting to a
    different corridor).  Medial wobble — throat bows, island-tip wraps,
    widening bulges — collapses onto the design axis; genuine curves keep
    their vertices and become arcs in the bend pass."""
    cs = np.asarray(coords)
    n = len(cs)
    out_idx = [0]
    i = 0
    while i < n - 1:
        j = n - 1
        chosen = i + 1
        while j > i + 1:
            a, b = cs[i], cs[j]
            ab = b - a
            L = float(np.hypot(*ab))
            if L < 1e-6:
                j -= 1
                continue
            # lateral deviation of the medial from the chord
            rel = cs[i:j + 1] - a
            dev = np.abs(rel[:, 0] * ab[1] - rel[:, 1] * ab[0]) / L
            w_med = float(np.median(radii[i:j + 1])) if len(radii) else 8.0
            if float(dev.max()) <= max(3.0, 0.9 * w_med) \
                    and chord_ok(LineString([tuple(a), tuple(b)])):
                chosen = j
                break
            j = i + max(1, int((j - i) * 0.7))
        out_idx.append(chosen)
        i = chosen
    return cs[out_idx]


def _fair_chain(coords, radii, pav_ok, chord_ok):
    """Straight chords + tangent bend arcs from a wobbly medial chain."""
    radii = np.asarray(radii, dtype=float)
    simp = _chord_straighten(coords, radii, chord_ok)
    if len(simp) > 2:
        simp = np.asarray(LineString(simp).simplify(_FAIR_SIMPLIFY_M).coords)
    ln = LineString(coords)
    # carry half-widths onto the simplified vertices (arc-position lookup)
    pos = [0.0]
    for k in range(1, len(coords)):
        pos.append(pos[-1] + float(np.hypot(*(coords[k] - coords[k - 1]))))
    r_at = lambda p: float(np.interp(p, pos, radii))

    out = [simp[0]]
    acc = 0.0
    for i in range(1, len(simp) - 1):
        P = simp[i]
        acc += float(np.hypot(*(simp[i] - simp[i - 1])))
        u_in = _unit(*(P - out[-1]))
        u_out = _unit(*(simp[i + 1] - P))
        w = r_at(ln.project(Point(tuple(P))))
        r = _radius_for_halfwidth(w)
        placed = False
        while r >= _MIN_FILLET_R:
            arc, t = _fillet_between(tuple(P), u_in, u_out, r)
            if arc is None:
                break
            d_prev = float(np.hypot(*(P - out[-1])))
            d_next = float(np.hypot(*(simp[i + 1] - P)))
            if t <= d_prev * 0.7 and t <= d_next * 0.7 \
                    and pav_ok(LineString(arc)):
                out.extend(arc)
                placed = True
                break
            r *= 0.7
        if not placed:
            out.append(P)
    out.append(simp[-1])
    return np.asarray(out)


# ── step 3b: throat S-connectors (replace medial rungs) ─────────────────────

_RUNG_MAX_LEN_M = 90.0        # a lane-to-lane rung is short-ish; the
#                               near-parallel-hosts test is the real filter
_RUNG_PARALLEL_MAX_DEG = 30.0  # ...and joins near-parallel lanes


def _hermite(a, ta, b, tb, scale, n=16):
    """Cubic Hermite from ``a`` (tangent ta) to ``b`` (tangent tb)."""
    a, b = np.asarray(a), np.asarray(b)
    m0 = np.asarray(ta) * scale
    m1 = np.asarray(tb) * scale
    ts = np.linspace(0.0, 1.0, n)
    h00 = 2 * ts**3 - 3 * ts**2 + 1
    h10 = ts**3 - 2 * ts**2 + ts
    h01 = -2 * ts**3 + 3 * ts**2
    h11 = ts**3 - ts**2
    return (h00[:, None] * a + h10[:, None] * m0
            + h01[:, None] * b + h11[:, None] * m1)


def _min_turn_radius(coords) -> float:
    """Smallest circumradius along a sampled path (∞ for straight)."""
    best = float("inf")
    cs = np.asarray(coords)
    for k in range(1, len(cs) - 1):
        a, b, c = cs[k - 1], cs[k], cs[k + 1]
        ab, bc, ca = (np.hypot(*(b - a)), np.hypot(*(c - b)),
                      np.hypot(*(a - c)))
        area2 = abs((b[0] - a[0]) * (c[1] - a[1])
                    - (b[1] - a[1]) * (c[0] - a[0]))
        if area2 < 1e-9:
            continue
        best = min(best, ab * bc * ca / (2.0 * area2))
    return best


def _throat_connectors(ways, rungs, pav_ok):
    """Replace each lane-to-lane RUNG with tangent S-connectors — the two
    mirrored reverse curves an aircraft actually follows to change lanes
    through the throat (one per travel direction).  This is the airport-
    diagram geometry at dual-lane island throats: the lanes stay straight,
    the movements are smooth diagonals."""
    out = []
    lines = [w.line for w in ways]

    def _host(tip):
        best = None
        for i, ln in enumerate(lines):
            if ln.length < 80.0:
                continue
            d = ln.distance(Point(tip))
            if d < 30.0 and (best is None or d < best[0]):
                anchor = nearest_points(Point(tip), ln)[1]
                ds = _local_dirs(ln, anchor)
                if len(ds) >= 2:
                    best = (d, i, anchor, ds[0])
        return best

    for (P, Q) in rungs:
        ha, hb = _host(P), _host(Q)
        if ha is None or hb is None or ha[1] == hb[1]:
            continue
        _da, ia, anch_a, ta = ha
        _db, ib, anch_b, _tb = hb
        gap = float(anch_a.distance(anch_b))
        span = max(1.6 * gap, 30.0)
        for sgn in (1.0, -1.0):           # the two travel directions
            u = (ta[0] * sgn, ta[1] * sgn)
            # pick lane B direction best aligned with u
            db = _local_dirs(lines[ib], anch_b)
            if len(db) < 2:
                continue
            v = max(db, key=lambda d: d[0] * u[0] + d[1] * u[1])
            if _angle_deg(u, v) > _RUNG_PARALLEL_MAX_DEG + 15.0:
                continue
            a = (anch_a.x - u[0] * span * 0.5, anch_a.y - u[1] * span * 0.5)
            b = (anch_b.x + v[0] * span * 0.5, anch_b.y + v[1] * span * 0.5)
            # anchor the endpoints ON the lanes
            pa = nearest_points(Point(a), lines[ia])[1]
            pb = nearest_points(Point(b), lines[ib])[1]
            for stretch in (1.0, 1.5, 2.2):
                cs = _hermite((pa.x, pa.y), u, (pb.x, pb.y), v,
                              scale=span * stretch)
                ln_s = LineString(cs)
                if pav_ok(ln_s) and _min_turn_radius(cs) >= _MIN_FILLET_R:
                    out.append(SpineWay(ln_s, "connector"))
                    break
    return out


# ── step 4: junction arcs ────────────────────────────────────────────────────

def _junction_arcs(ways, nodes, pav_ok, halfwidth_at):
    """One tangent fillet per branch-direction pair at each junction node."""
    lines = [w.line for w in ways]
    arcs, seen = [], set()
    for node in nodes:
        P = Point(node)
        # chord-straightening pulls faired lanes several metres off the raw
        # medial node; the branches of this junction are the lines nearby
        incident = [i for i, ln in enumerate(lines)
                    if ln.distance(P) < 10.0]
        if len(incident) < 2:
            continue
        w_local = halfwidth_at(node)
        branches = []                     # (way_idx, arrive_dir, anchor_pt)
        for i in incident:
            anchor = nearest_points(P, lines[i])[1]
            for d in _local_dirs(lines[i], anchor):
                branches.append((i, d, anchor))
        for a in range(len(branches)):
            for b in range(a + 1, len(branches)):
                ia, ua, pa = branches[a]
                ib, ub, pb = branches[b]
                if ia == ib:
                    continue              # same chain: through or bend, done
                # both u are ARRIVING directions; departure along branch b
                # is -ub.
                u_out = (-ub[0], -ub[1])
                ang = _angle_deg(ua, u_out)
                if ang < _TURN_MIN_DEG or ang > _TURN_MAX_DEG:
                    continue
                # corner = intersection of the two tangent LINES, so the
                # arc is tangent to the lanes as-built, not to the node
                den = ua[0] * u_out[1] - ua[1] * u_out[0]
                if abs(den) < 1e-6:
                    continue
                dx, dy = pb.x - pa.x, pb.y - pa.y
                s = (dx * u_out[1] - dy * u_out[0]) / den
                corner = (pa.x + ua[0] * s, pa.y + ua[1] * s)
                if math.hypot(corner[0] - P.x, corner[1] - P.y) > 40.0:
                    continue              # degenerate near-parallel pair
                r = _radius_for_halfwidth(w_local)
                while r >= _MIN_FILLET_R:
                    arc, _t = _fillet_between(corner, ua, u_out, r)
                    if arc is None:
                        break
                    ln_arc = LineString(arc)
                    if pav_ok(ln_arc):
                        m = ln_arc.interpolate(0.5, normalized=True)
                        key = (round(m.x, 1), round(m.y, 1))
                        if key not in seen:
                            seen.add(key)
                            arcs.append(SpineWay(ln_arc, "arc", w_local))
                        break
                    r *= 0.7
    return arcs


# ── step 5: runway diagonal sharp-turn arcs ──────────────────────────────────

def _runway_turn_arcs(ways, runway_union, pav_ok):
    """A diagonal (rapid-exit style) runway contact gets the sharp-turn arc:
    tangent to the diagonal spine and to the runway edge line, landing a
    second node on the edge in the sharp-side fillet wedge."""
    if runway_union is None or runway_union.is_empty:
        return []
    edge = runway_union.boundary
    out = []
    for w in ways:
        if w.kind != "line":
            continue
        cs = np.asarray(w.line.coords)
        for at_start in (True, False):
            tip = cs[0] if at_start else cs[-1]
            P = Point(tuple(tip))
            if edge.distance(P) > 1.5:
                continue
            u_in = _end_dir(cs, at_start=not at_start)
            if at_start:
                u_in = (-u_in[0], -u_in[1])
            # u_in now ARRIVES at the tip (travel toward the runway).
            # Runway edge direction at the contact:
            s = edge.project(P)
            e_len = getattr(edge, "length", 0.0) or 1.0
            q0 = edge.interpolate(max(0.0, s - 10.0))
            q1 = edge.interpolate(min(e_len, s + 10.0))
            e_dir = _unit(q1.x - q0.x, q1.y - q0.y)
            if e_dir == (0.0, 0.0):
                continue
            ang = _angle_deg(u_in, e_dir)
            ang = min(ang, 180.0 - ang)   # vs runway axis, sign-free
            if not (_DIAG_MIN_DEG <= ang <= _DIAG_MAX_DEG):
                continue                  # square contact: single node is right
            # Sharp turn departs along the OBTUSE edge direction.
            for e_sgn in (1.0, -1.0):
                u_out = (e_dir[0] * e_sgn, e_dir[1] * e_sgn)
                if _angle_deg(u_in, u_out) < 95.0:
                    continue              # that is the high-speed (straight) side
                r = _radius_for_halfwidth(w.halfwidth or 10.0) * 1.5
                while r >= _MIN_FILLET_R:
                    arc, _t = _fillet_between((P.x, P.y), u_in, u_out, r)
                    if arc is None:
                        break
                    ln_arc = LineString(arc)
                    if pav_ok(ln_arc):
                        out.append(SpineWay(ln_arc, "rwy_turn", w.halfwidth))
                        break
                    r *= 0.7
    return out


# ── step 6: buildings ────────────────────────────────────────────────────────

def _building_ways(buildings, pav_eff, spine_union, setback: float):
    ways = []
    bpolys = [b for b, _r in buildings]
    ball = unary_union(bpolys) if bpolys else None
    pav_allow = shapely.buffer(pav_eff, 1.0)
    for bpoly, role in buildings:
        big = (role == "terminal") or bpoly.area >= SMALL_BUILDING_M2
        if big or spine_union is None or spine_union.is_empty:
            continue
        cs = list(bpoly.exterior.coords)
        best = None
        for i in range(len(cs) - 1):
            a, b = np.asarray(cs[i]), np.asarray(cs[i + 1])
            length = float(np.hypot(*(b - a)))
            if length < 6.0:
                continue
            mid = (a + b) / 2.0
            u = _unit(*(b - a))
            for sgn in (1.0, -1.0):
                nrm = (-u[1] * sgn, u[0] * sgn)
                probe = Point(mid[0] + nrm[0] * 2.0, mid[1] + nrm[1] * 2.0)
                if pav_eff.contains(probe) and not bpoly.contains(probe) \
                        and (best is None or length > best[0]):
                    best = (length, mid)
        if best is None:
            continue
        mid = best[1]
        target = nearest_points(Point(tuple(mid)), spine_union)[1]
        if target.distance(Point(tuple(mid))) > _STUB_MAX_REACH_M:
            continue
        stub = LineString([tuple(mid), (target.x, target.y)])
        if ball is not None and stub.crosses(ball):
            continue
        if not pav_allow.contains(stub):
            continue
        ways.append(SpineWay(stub, "building_stub"))

    bigs = [b for b, role in buildings
            if role == "terminal" or b.area >= SMALL_BUILDING_M2]
    if bigs:
        big_union = unary_union(bigs)
        keep_zone = shapely.buffer(pav_eff, -0.3)
        if ball is not None:
            keep_zone = keep_zone.difference(shapely.buffer(ball, 3.0))
        see_zone = shapely.buffer(pav_eff, 1.0)
        for comp in _polygons(big_union.buffer(setback, quad_segs=12)):
            clipped = LineString(comp.exterior.coords).intersection(keep_zone)
            for seg in getattr(clipped, "geoms", [clipped]):
                if seg.geom_type != "LineString" \
                        or seg.length < _RING_MIN_ARC_M:
                    continue
                n = max(2, int(seg.length / 8))
                pts = [seg.interpolate(k * seg.length / n)
                       for k in range(n + 1)]
                run = []
                for k, p in enumerate(pts):
                    sight = LineString([nearest_points(p, big_union)[1], p])
                    ok = see_zone.contains(sight) and (
                        spine_union is None
                        or spine_union.distance(p) > _RING_LANE_CLEAR_M)
                    if ok:
                        run.append(p)
                    if (not ok or k == n) and len(run) >= 2:
                        piece = LineString([(q.x, q.y) for q in run])
                        if piece.length >= _RING_MIN_ARC_M:
                            ways.append(SpineWay(piece, "building_ring"))
                        run = []
    return ways


# ── main ─────────────────────────────────────────────────────────────────────

def synthesize_spine(
    pav, runway_union=None, buildings=None, *,
    terminal_setback: float = TERMINAL_SETBACK_M,
) -> list[SpineWay]:
    """Navigable pavement-based spine (see module docstring)."""
    buildings = [(b, r) for (b, r) in (buildings or [])
                 if b is not None and not b.is_empty]
    building_union = unary_union([b for b, _ in buildings]) \
        if buildings else None

    pav_nav = pav
    if building_union is not None:
        try:
            pav_nav = pav.difference(shapely.buffer(building_union, 0.5))
        except Exception:
            pav_nav = pav
    pav_eff = pav_nav
    if runway_union is not None and not runway_union.is_empty:
        try:
            pav_eff = pav_nav.difference(runway_union)
        except Exception:
            pass
    allow = shapely.buffer(pav_eff, 0.3)
    strict = shapely.buffer(pav_eff, -_CHORD_CLEAR_M)

    def pav_ok(line: LineString) -> bool:
        return allow.contains(line)

    def chord_ok(line: LineString) -> bool:
        return strict.contains(line)

    # 1: medial skeleton (building-free pavement)
    skel = build_pavement_skeleton(pav_nav, runway_union=runway_union)
    chains = [[np.asarray(ch.line.coords, dtype=float),
               np.asarray(ch.radii, dtype=float)] for ch in skel]

    # junction nodes BEFORE merging (shared chain endpoints, >=3 incident)
    from collections import defaultdict
    node_count: dict = defaultdict(int)
    for coords, _r in chains:
        for tip in (coords[0], coords[-1]):
            node_count[(round(tip[0], 1), round(tip[1], 1))] += 1
    nodes = [k for k, cnt in node_count.items() if cnt >= 3]

    # 2-3: through-merge, then fair to straights+arcs
    merged = _merge_through(chains)
    ways: list[SpineWay] = []
    for coords, radii in merged:
        faired = _fair_chain(coords, radii, pav_ok, chord_ok)
        if len(faired) < 2:
            continue
        ln = LineString(faired)
        if ln.length < 2.0:
            continue
        ways.append(SpineWay(ln, "line", float(np.median(radii))))

    # 3b: classify lane-to-lane RUNGS (short chain, both ends interior to a
    # longer near-parallel through-lane) and replace them with tangent
    # S-connectors — lanes stay straight, movements are smooth diagonals.
    lane_lines = [w.line for w in ways]
    rungs, rung_idx, rung_nodes = [], set(), set()
    for wi, w in enumerate(ways):
        if w.line.length > _RUNG_MAX_LEN_M:
            continue
        cs = np.asarray(w.line.coords)
        P, Q = cs[0], cs[-1]
        hosts = []
        for tip in (P, Q):
            host = None
            for oi, oln in enumerate(lane_lines):
                if oi == wi or oln.length < 80.0:
                    continue
                # chord-straightening pulls the lane well off the raw medial
                # node at a throat (the medial bulges into the gap) — search
                # wide, anchor at the projection onto the lane as-built
                if oln.distance(Point(tuple(tip))) < 30.0:
                    anchor = nearest_points(Point(tuple(tip)), oln)[1]
                    ds = _local_dirs(oln, anchor)
                    if len(ds) >= 2:      # anchor sits INSIDE the through lane
                        host = (oi, ds[0])
                        break
            hosts.append(host)
        if hosts[0] is None or hosts[1] is None \
                or hosts[0][0] == hosts[1][0]:
            continue
        axis_ang = _angle_deg(hosts[0][1], hosts[1][1])
        axis_ang = min(axis_ang, 180.0 - axis_ang)
        if axis_ang > _RUNG_PARALLEL_MAX_DEG:
            continue
        rungs.append((tuple(P), tuple(Q)))
        rung_idx.add(wi)
        rung_nodes.add((round(P[0], 1), round(P[1], 1)))
        rung_nodes.add((round(Q[0], 1), round(Q[1], 1)))
    ways = [w for wi, w in enumerate(ways) if wi not in rung_idx]
    ways.extend(_throat_connectors(ways, rungs, pav_ok))
    nodes = [n for n in nodes if n not in rung_nodes]

    # 4: one fillet per branch pair per junction node

    def halfwidth_at(node) -> float:
        best, bw = None, 8.0
        for coords, radii in merged:
            ln = LineString(coords)
            d = ln.distance(Point(node))
            if best is None or d < best:
                best = d
                bw = float(np.interp(
                    ln.project(Point(node)) / max(ln.length, 1e-9),
                    np.linspace(0, 1, len(radii)), radii))
        return bw

    ways.extend(_junction_arcs(ways, nodes, pav_ok, halfwidth_at))

    # 5: runway diagonal sharp-turn arcs
    ways.extend(_runway_turn_arcs(ways, runway_union, pav_ok))

    # 6: building stubs + rings
    spine_union = unary_union([w.line for w in ways]) if ways else None
    ways.extend(_building_ways(buildings, pav_eff, spine_union,
                               terminal_setback))
    return ways
