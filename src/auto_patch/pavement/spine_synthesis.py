"""Route-guided spine synthesis — straights from the taxi route network,
curves inferred from the pavement, buildings respected.

The medial-axis skeleton (``pav_skeleton``) covers everything but has two
systematic mismatches with how airports are actually designed (user
2026-07-01):

* At junctions/aprons the medial axis bulges toward the middle of the wide
  pavement, where a real taxi lane KEEPS ITS HALF-WIDTH around each grass
  island and a long straight taxiway PASSES STRAIGHT THROUGH the junction.
* It happily routes through building pads.

So the spine is synthesized from design intent instead:

1. **Through-lines** — the apt.dat taxi ROUTE network (``chained_line``,
   connectivity-merged, straight segments) is the topological guide.  Chains
   meeting near-collinearly at a junction node are merged so a long straight
   continues through; dead ends are extended along their tangent to the
   pavement boundary (runway edges, stub end caps).
2. **Fillet arcs** — at genuine turns (interior bends and junction turn
   pairs) a tangent arc is inserted with the EASA/FAA centerline turn radius
   for the local ICAO code letter, shrunk until it fits inside the pavement.
   Throughs are never trimmed: the turn is an additional connector way, like
   real fillet paint.
3. **Island loops** — every hole in ``pav_union`` that is not a building is
   ringed at CONSTANT lane half-width (measured from the hole to the nearest
   other pavement boundary), i.e. ``hole.buffer(w)``, clipped to pavement.
   The buffer's rounded joins ARE the curve geometry the holes imply.
4. **Buildings** — spines never enter a building.  Small buildings
   (< ``SMALL_BUILDING_M2``) get one lead-in stub to the midpoint of their
   longest pavement-facing edge; larger buildings and terminals get a
   perimeter ring at ``TERMINAL_SETBACK_M``, clipped to pavement.
5. **Medial fallback** — the ``pav_skeleton`` chains cover whatever pavement
   none of the above reached (service roads, unrouted aprons).

Output ways are tagged by construct kind so a JOSM preview shows what each
line is and why it exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

from .pav_skeleton import build_pavement_skeleton, _polygons

# ── provisional standards constants ──────────────────────────────────────────
# Taxiway centerline turn radius by ICAO/FAA design code letter (m).  Values
# follow FAA AC 150/5300-13B / EASA CS-ADR-DSN judgment-oversteer fillet
# design; PROVISIONAL — move to config.py + docs/STANDARDS.md when this module
# is wired into the pipeline.
FILLET_RADIUS_BY_CODE = {
    "A": 22.5, "B": 22.5, "C": 30.0, "D": 45.0, "E": 45.0, "F": 51.0}
_DEFAULT_CODE = "C"
_MIN_FILLET_R = 12.0          # below this a turn is emitted as a plain corner
_TURN_MIN_DEG = 15.0          # bends flatter than this stay straight
_COLLINEAR_MAX_DEG = 12.0     # chains this straight across a node merge
_NODE_SNAP_M = 1.0            # route endpoints within this share a node

SMALL_BUILDING_M2 = 2000.0    # user 2026-07-01: lead-in stub vs perimeter ring
TERMINAL_SETBACK_M = 100.0    # perimeter-ring distance from large buildings
_STUB_MAX_REACH_M = 80.0      # lead-in stub searches this far for a spine
_ISLAND_HALFWIDTH_CLAMP = (4.0, 25.0)
_ISLAND_MIN_ARC_M = 12.0      # island-loop pieces shorter than this drop
_RING_MIN_ARC_M = 30.0        # terminal-ring pieces shorter than this drop
_MEDIAL_FAR_M = 35.0          # medial chains mostly farther than this survive
_MEDIAL_FAR_FRAC = 0.6


@dataclass
class SpineWay:
    line: LineString
    kind: str          # through | fillet | island | building_stub |
    #                    building_ring | medial
    size: str = ""     # ICAO code letter where known
    service: bool = False


# ── small geometry helpers ───────────────────────────────────────────────────

def _unit(dx: float, dy: float):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-9 else (0.0, 0.0)


def _end_dir(coords, at_start: bool, back_m: float = 25.0):
    """Unit direction of the chain AT an end, pointing OUT of the chain,
    averaged over the last ``back_m`` metres so a resample jitter doesn't
    swing it."""
    cs = np.asarray(coords)
    if at_start:
        cs = cs[::-1]
    # walk back back_m from the end
    acc, i = 0.0, len(cs) - 1
    while i > 0 and acc < back_m:
        acc += float(np.hypot(*(cs[i] - cs[i - 1])))
        i -= 1
    v = cs[-1] - cs[i]
    return _unit(float(v[0]), float(v[1]))


def _angle_deg(u, v) -> float:
    d = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
    return math.degrees(math.acos(d))


def _arc(center, r, a0, a1, ccw: bool, step_m: float = 4.0):
    """Sampled circular arc from angle a0 to a1 (radians)."""
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
    """Tangent arc for a corner at ``P`` between an incoming direction
    ``u_in`` (unit, INTO P) and outgoing ``u_out`` (unit, OUT of P).
    Returns (arc_coords, tangent_len) or (None, 0) for a straight corner."""
    # deflection angle between the two straights (0 = straight through)
    gamma = math.acos(max(-1.0, min(1.0,
        u_in[0] * u_out[0] + u_in[1] * u_out[1])))
    if gamma < math.radians(_TURN_MIN_DEG) or gamma > math.radians(170):
        return None, 0.0
    t = r * math.tan(gamma / 2.0)
    ta = (P[0] - u_in[0] * t, P[1] - u_in[1] * t)      # tangent pt on incoming
    tb = (P[0] + u_out[0] * t, P[1] + u_out[1] * t)    # tangent pt on outgoing
    # center: offset perpendicular from ta toward the inside of the turn
    cross = u_in[0] * u_out[1] - u_in[1] * u_out[0]
    sgn = 1.0 if cross > 0 else -1.0                   # left turn = ccw
    n_in = (-u_in[1] * sgn, u_in[0] * sgn)
    c = (ta[0] + n_in[0] * r, ta[1] + n_in[1] * r)
    a0 = math.atan2(ta[1] - c[1], ta[0] - c[0])
    a1 = math.atan2(tb[1] - c[1], tb[0] - c[0])
    return _arc(c, r, a0, a1, ccw=(sgn > 0)), t


def _radius_for(size: str) -> float:
    return FILLET_RADIUS_BY_CODE.get(size or _DEFAULT_CODE,
                                     FILLET_RADIUS_BY_CODE[_DEFAULT_CODE])


# ── step 1: through-lines from the route network ─────────────────────────────

def _route_chains(routes):
    """Unique continuous route polylines: [(coords Nx2, size, service)]."""
    seen, out = set(), []
    for tc in routes or []:
        ln = getattr(tc, "chained_line", None) or getattr(tc, "line", None)
        if ln is None or ln.is_empty or ln.length < 5.0:
            continue
        if id(ln) in seen:
            continue
        seen.add(id(ln))
        out.append((np.asarray(ln.coords, dtype=float),
                    getattr(tc, "dominant_size", lambda: "")() or "",
                    bool(getattr(tc, "is_service", False))))
    return out


def _merge_collinear(chains):
    """Merge chains whose ends meet (within _NODE_SNAP_M) near-collinearly, so
    a straight taxiway continues through the junction as ONE spine."""
    chains = [list(c) for c in chains]        # [coords, size, service]
    merged = True
    while merged:
        merged = False
        for i in range(len(chains)):
            if chains[i] is None:
                continue
            for j in range(len(chains)):
                if i == j or chains[j] is None:
                    continue
                ci, cj = chains[i][0], chains[j][0]
                for ei, ej, flip_i, flip_j in (
                        (-1, 0, False, False), (-1, -1, False, True),
                        (0, 0, True, False), (0, -1, True, True)):
                    if np.hypot(*(ci[ei] - cj[ej])) > _NODE_SNAP_M:
                        continue
                    u = _end_dir(ci, at_start=(ei == 0))
                    v = _end_dir(cj, at_start=(ej == 0))
                    # u points out of chain i, v out of chain j; continuing
                    # straight means u ≈ -v.
                    if _angle_deg(u, (-v[0], -v[1])) > _COLLINEAR_MAX_DEG:
                        continue
                    a = ci[::-1] if flip_i else ci
                    b = cj[::-1] if flip_j else cj
                    chains[i][0] = np.vstack([a, b[1:]])
                    chains[i][1] = chains[i][1] or chains[j][1]
                    chains[i][2] = chains[i][2] and chains[j][2]
                    chains[j] = None
                    merged = True
                    break
                if merged:
                    break
            if merged:
                break
    return [c for c in chains if c is not None]


def _smooth_interior_bends(coords, size, pav_ok) -> np.ndarray:
    """Replace interior bends sharper than _TURN_MIN_DEG with tangent fillet
    arcs (shrunk until they fit the pavement), keeping straights straight."""
    out = [coords[0]]
    i = 1
    while i < len(coords) - 1:
        P = coords[i]
        u_in = _unit(*(P - out[-1]))
        u_out = _unit(*(coords[i + 1] - P))
        r = _radius_for(size)
        placed = False
        while r >= _MIN_FILLET_R:
            arc, t = _fillet_between(tuple(P), u_in, u_out, r)
            if arc is None:
                break
            d_prev = float(np.hypot(*(P - out[-1])))
            d_next = float(np.hypot(*(coords[i + 1] - P)))
            if t <= d_prev * 0.7 and t <= d_next * 0.7 \
                    and pav_ok(LineString(arc)):
                out.extend(arc)
                placed = True
                break
            r *= 0.7
        if not placed:
            out.append(P)
        i += 1
    out.append(coords[-1])
    return np.asarray(out)


def _local_dirs(coords, P: Point, window: float = 12.0):
    """Travel directions available AT point ``P`` on the chain: the tangent
    (averaged over ±window m) in both signs at an interior point, one sign
    at a chain end."""
    ln = LineString(coords)
    s = ln.project(P)
    a = ln.interpolate(max(0.0, s - window))
    b = ln.interpolate(min(ln.length, s + window))
    t = _unit(b.x - a.x, b.y - a.y)
    if t == (0.0, 0.0):
        return []
    dirs = []
    if s > 3.0:
        dirs.append(t)                     # arriving/leaving toward +t
    if s < ln.length - 3.0:
        dirs.append((-t[0], -t[1]))
    return dirs


def _turn_fillets(chains, pav_ok):
    """Turn arcs wherever two chains CROSS or MEET — X-crossings get up to
    four quadrant fillets, T's and shared end-nodes get theirs; only arcs
    that fit inside the pavement survive (a turn with no fillet pavement is
    not a real movement).  Throughs are never trimmed; the fillet is its own
    way, tangent-welded."""
    lines = [LineString(c[0]) for c in chains]
    fillets, seen = [], set()
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            if not lines[i].intersects(lines[j]):
                continue
            inter = lines[i].intersection(lines[j])
            pts = [g for g in getattr(inter, "geoms", [inter])
                   if g.geom_type == "Point"]
            size = min(chains[i][1] or _DEFAULT_CODE,
                       chains[j][1] or _DEFAULT_CODE)
            service = chains[i][2] and chains[j][2]
            for P in pts:
                dirs_i = _local_dirs(chains[i][0], P)
                dirs_j = _local_dirs(chains[j][0], P)
                for u_in in dirs_i:            # arrive along chain i
                    for u_out in dirs_j:       # depart along chain j
                        ang = _angle_deg(u_in, u_out)
                        if ang < _TURN_MIN_DEG or ang > 170.0:
                            continue
                        r = _radius_for(size)
                        while r >= _MIN_FILLET_R:
                            arc, _t = _fillet_between((P.x, P.y),
                                                      u_in, u_out, r)
                            if arc is None:
                                break
                            ln_arc = LineString(arc)
                            if pav_ok(ln_arc):
                                m = ln_arc.interpolate(0.5, normalized=True)
                                key = (round(m.x), round(m.y))
                                if key not in seen:
                                    seen.add(key)
                                    fillets.append(SpineWay(
                                        ln_arc, "fillet", size, service))
                                break
                            r *= 0.7
    return fillets


def _extend_to_boundary(coords, pav, allow, max_reach: float = 120.0
                        ) -> np.ndarray:
    """Extend both ends along their tangent to the pavement boundary (runway
    edges, stub caps) so a route that stops short still spans the pavement."""
    bnd = pav.boundary
    for at_start in (True, False):
        tip = coords[0] if at_start else coords[-1]
        u = _end_dir(coords, at_start)
        ray = LineString([tuple(tip),
                          (tip[0] + u[0] * max_reach, tip[1] + u[1] * max_reach)])
        hit = ray.intersection(bnd)
        pts = [g for g in getattr(hit, "geoms", [hit])
               if g.geom_type == "Point"]
        if not pts:
            continue
        near = min(pts, key=lambda p: (p.x - tip[0]) ** 2 + (p.y - tip[1]) ** 2)
        d = math.hypot(near.x - tip[0], near.y - tip[1])
        if d < 0.5:
            continue
        # only extend through pavement, never across a gap
        probe = LineString([tuple(tip), (near.x, near.y)])
        if not allow.contains(probe):
            continue
        end = np.asarray([near.x - u[0] * 0.10, near.y - u[1] * 0.10])
        coords = (np.vstack([[end], coords]) if at_start
                  else np.vstack([coords, [end]]))
    return coords


# ── step 3: island loops ─────────────────────────────────────────────────────

def _island_loops(pav_eff, building_union):
    """Constant half-width loop around every non-building hole."""
    loops = []
    for poly in _polygons(pav_eff):
        rings = [LineString(poly.exterior.coords)] + \
                [LineString(h.coords) for h in poly.interiors]
        tree = STRtree(rings)
        for hi, hole in enumerate(poly.interiors):
            hole_poly = Polygon(hole)
            if hole_poly.area < 40.0:
                continue
            if building_union is not None:
                ov = hole_poly.intersection(building_union).area
                # A building PAD hole is often larger than its building —
                # any substantial overlap disqualifies it as a grass island.
                if ov > 20.0 or ov > 0.3 * hole_poly.area:
                    continue                   # building pad, handled apart
            ring = rings[hi + 1]
            # lane half-width: distance from the hole to the nearest OTHER
            # boundary, halved; median over samples for robustness.
            ds = []
            n = max(8, int(ring.length / 10))
            for k in range(n):
                p = ring.interpolate(k * ring.length / n)
                best = None
                for oi in tree.query(p.buffer(80.0)):
                    if int(oi) == hi + 1:
                        continue
                    d = rings[int(oi)].distance(p)
                    best = d if best is None else min(best, d)
                if best is not None:
                    ds.append(best)
            if not ds:
                continue
            w = float(np.median(ds)) / 2.0
            w = max(_ISLAND_HALFWIDTH_CLAMP[0],
                    min(_ISLAND_HALFWIDTH_CLAMP[1], w))
            loop = hole_poly.buffer(w, quad_segs=12).exterior
            clipped = LineString(loop.coords).intersection(
                shapely.buffer(pav_eff, -0.3))
            for seg in getattr(clipped, "geoms", [clipped]):
                if seg.geom_type == "LineString" \
                        and seg.length >= _ISLAND_MIN_ARC_M:
                    loops.append(SpineWay(seg, "island"))
    return loops


# ── step 4: buildings ────────────────────────────────────────────────────────

def _building_ways(buildings, pav_eff, spine_union, setback: float):
    """Lead-in stubs for small buildings; setback perimeter rings for large
    buildings/terminals.  Nothing enters a building."""
    ways = []
    bpolys = [b for b, _r in buildings]
    ball = unary_union(bpolys) if bpolys else None
    pav_allow = shapely.buffer(pav_eff, 1.0)
    for bpoly, role in buildings:
        big = (role == "terminal") or bpoly.area >= SMALL_BUILDING_M2
        if not big:
            # one stub: midpoint of the longest pavement-facing edge,
            # outward to the nearest existing spine.
            if spine_union is None or spine_union.is_empty:
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
                    if pav_eff.contains(probe) \
                            and not bpoly.contains(probe) \
                            and (best is None or length > best[0]):
                        best = (length, mid, nrm)
            if best is None:
                continue
            _l, mid, _nrm = best
            target = nearest_points(Point(tuple(mid)), spine_union)[1]
            if target.distance(Point(tuple(mid))) > _STUB_MAX_REACH_M:
                continue
            stub = LineString([tuple(mid), (target.x, target.y)])
            if ball is not None and stub.crosses(ball):
                continue
            if not pav_allow.contains(stub):
                continue
            ways.append(SpineWay(stub, "building_stub"))
    # Perimeter rings: buffer the UNION of all large buildings so a terminal
    # cluster gets one merged contour instead of overlapping per-building
    # circles, then keep only ring stretches that actually SEE their building
    # across pavement (a ring arc separated from the building by grass is a
    # geometric accident, not an apron lane).
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
                # visibility split: walk the arc, keep runs whose sightline
                # to the building stays on pavement.
                n = max(2, int(seg.length / 8))
                good = []
                for k in range(n + 1):
                    p = seg.interpolate(k * seg.length / n)
                    sight = LineString([nearest_points(p, big_union)[1], p])
                    good.append(see_zone.contains(sight))
                run = []
                for k in range(n + 1):
                    if good[k]:
                        run.append(seg.interpolate(k * seg.length / n))
                    if (not good[k] or k == n) and len(run) >= 2:
                        piece = LineString([(q.x, q.y) for q in run])
                        if piece.length >= _RING_MIN_ARC_M:
                            ways.append(SpineWay(piece, "building_ring"))
                        run = []
    return ways


# ── main ─────────────────────────────────────────────────────────────────────

def synthesize_spine(
    pav, runway_union=None, routes=None, buildings=None, *,
    terminal_setback: float = TERMINAL_SETBACK_M,
    medial_fallback: bool = True,
) -> list[SpineWay]:
    """Route-guided spine over ``pav`` (see module docstring).

    ``routes`` — iterable of ``TaxiCenterline`` (or anything with
    ``chained_line``/``line``, ``dominant_size()``, ``is_service``).
    ``buildings`` — iterable of ``(polygon, role)`` with role
    ``"building" | "terminal"``.
    """
    pav_eff = pav
    if runway_union is not None and not runway_union.is_empty:
        try:
            pav_eff = pav.difference(runway_union)
        except Exception:
            pav_eff = pav
    allow = shapely.buffer(pav_eff, 0.3)

    def pav_ok(line: LineString) -> bool:
        return allow.contains(line)

    buildings = [(b, r) for (b, r) in (buildings or [])
                 if b is not None and not b.is_empty]
    building_union = unary_union([b for b, _ in buildings]) \
        if buildings else None
    forbid = shapely.buffer(building_union, 1.0) \
        if building_union is not None else None

    ways: list[SpineWay] = []

    # 1-2: throughs (clip to pavement, merge collinear, fillet bends, extend)
    chains = _route_chains(routes)
    clipped = []
    for coords, size, service in chains:
        inter = LineString(coords).intersection(allow)
        for seg in getattr(inter, "geoms", [inter]):
            if seg.geom_type == "LineString" and seg.length > 5.0:
                clipped.append([np.asarray(seg.coords), size, service])
    merged = _merge_collinear(clipped)
    final_chains = []
    for coords, size, service in merged:
        coords = _smooth_interior_bends(coords, size, pav_ok)
        coords = _extend_to_boundary(coords, pav_eff, allow)
        ln = LineString(coords)
        if forbid is not None and ln.intersects(forbid):
            segs = [s for s in getattr(ln.difference(forbid), "geoms",
                                       [ln.difference(forbid)])
                    if s.geom_type == "LineString"]
        else:
            segs = [ln]
        for seg in segs:
            if seg.length < 20.0:      # clip fragment, not a taxi lane
                continue
            ways.append(SpineWay(seg, "through", size, service))
            final_chains.append([np.asarray(seg.coords), size, service])
    ways.extend(_turn_fillets(final_chains, pav_ok))

    # 3: island loops at constant half-width
    ways.extend(_island_loops(pav_eff, building_union))

    # 4: building stubs + terminal rings.  Ring stretches that ride an
    # existing lane (within a lane's width of a through/island/fillet) are
    # redundant crossings, not frontage — the ring only lives in open apron.
    spine_union = unary_union([w.line for w in ways]) if ways else None
    for w in _building_ways(buildings, pav_eff, spine_union,
                            terminal_setback):
        if w.kind != "building_ring" or spine_union is None:
            ways.append(w)
            continue
        n = max(2, int(w.line.length / 8))
        pts = [w.line.interpolate(k * w.line.length / n) for k in range(n + 1)]
        run = []
        for k, p in enumerate(pts):
            keep = spine_union.distance(p) > 15.0
            if keep:
                run.append(p)
            if (not keep or k == n) and len(run) >= 2:
                piece = LineString([(q.x, q.y) for q in run])
                if piece.length >= _RING_MIN_ARC_M:
                    ways.append(SpineWay(piece, "building_ring"))
                run = []

    # 5: medial fallback where nothing reached
    if medial_fallback:
        spine_union = unary_union([w.line for w in ways]) if ways else None
        # Runway SHOULDER strips (pav minus runway leaves thin flankers) get
        # no medial spine — the runway profile owns them.
        rwy_zone = shapely.buffer(runway_union, 25.0) \
            if runway_union is not None and not runway_union.is_empty else None
        for ch in build_pavement_skeleton(pav, runway_union=runway_union):
            ln = ch.line
            if forbid is not None and ln.intersects(forbid):
                parts = [s for s in getattr(ln.difference(forbid), "geoms",
                                            [ln.difference(forbid)])
                         if s.geom_type == "LineString" and s.length > 8.0]
            else:
                parts = [ln]
            for part in parts:
                if part.length < 20.0:         # crumb, not coverage
                    continue
                n = max(2, int(part.length / 10))
                samples = [part.interpolate(k * part.length / n)
                           for k in range(n + 1)]
                if rwy_zone is not None:
                    on_shoulder = sum(1 for p in samples
                                      if rwy_zone.contains(p))
                    if on_shoulder / len(samples) >= 0.6:
                        continue
                if spine_union is None or spine_union.is_empty:
                    ways.append(SpineWay(part, "medial"))
                    continue
                far = sum(1 for p in samples
                          if spine_union.distance(p) > _MEDIAL_FAR_M)
                if far / len(samples) >= _MEDIAL_FAR_FRAC:
                    ways.append(SpineWay(part, "medial"))
    return ways
