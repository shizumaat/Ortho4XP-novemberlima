"""V13 ROUTE-GRAPH + ARCS spine (user architecture ruling 2026-07-02).

The solver's anchors come from FEASIBLE ELEVATIONS, which come from
ACCURATE TAXI DISTANCES between runway intersections and buildings — so
the spine's first obligation is metric fidelity, and the apt.dat
1201/1202 taxi-route graph is the authoritative distance source (it is
what production builds the spine from).  Its one shortcoming is the
lack of curves: every junction turn and every bend is a hard corner,
which the grading cannot climb smoothly.

V13 keeps the route graph as-is — every straight segment, every true
distance — and adds ONLY the missing arcs, in the key spots the tracing
work identified:

* JUNCTION TURNS: every branch pair with a real deflection at every
  welded node — including T-junctions where a stem meets a through
  route's interior (the production route-END fillet pass misses those;
  planarizing first makes them ordinary nodes);
* BENDS inside chained routes (split at deflection vertices so the arc
  pass sees them as nodes);
* RUNWAY entries/exits: hooks/diagonal blends via the existing
  runway-turn machinery.

Arcs replace corners, so path lengths only shrink toward the real
aircraft path — never grow.  Radii: R90_BY_SIZE per the segment's ICAO
size letter, mirrored equal per junction, shrink-to-fit the pavement
(the full thru-runway pavement: routes cross runways freely).
"""

from __future__ import annotations

import math
import os

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from .spine_synthesis import (
    R90_BY_SIZE, SpineWay, _Graph, _add_junction_arcs, _add_runway_turns,
    _angle_deg, _fillet, _unit,
)


def _planarize_crossings(g: _Graph):
    """Node every geometric crossing between edges so routes can turn
    there and the junction-arc pass can fire.  Both edges are split at
    the crossing; the split point welds via the node-key quantum.
    (Moved from the retired edge_trace experiment module.)"""
    from shapely.strtree import STRtree
    for _round in range(4):
        alive = [(ei, LineString(e["cs"])) for ei, e in enumerate(g.edges)
                 if e["alive"]]
        tree = STRtree([ln for _ei, ln in alive])
        crossed = False
        done_pairs = set()
        for k, (ei, ln) in enumerate(alive):
            if not g.edges[ei]["alive"]:
                continue
            for j in tree.query(ln):
                j = int(j)
                if j <= k:
                    continue
                ej = alive[j][0]
                if ej == ei or not g.edges[ej]["alive"] \
                        or (ei, ej) in done_pairs:
                    continue
                lnj = alive[j][1]
                if not ln.crosses(lnj):
                    continue
                inter = ln.intersection(lnj)
                pts = [q for q in getattr(inter, "geoms", [inter])
                       if q.geom_type == "Point"]
                for q in pts[:1]:
                    sa = ln.project(q)
                    sb = lnj.project(q)
                    if min(sa, ln.length - sa) < 1.5 \
                            or min(sb, lnj.length - sb) < 1.5:
                        continue        # endpoint touch, not a crossing
                    g.split_edge(ei, sa)
                    g.split_edge(ej, sb)
                    crossed = True
                done_pairs.add((ei, ej))
                if not g.edges[ei]["alive"]:
                    break
        if not crossed:
            break


def _replace_polyline_turns(g: _Graph, pav_ok, min_turn_deg: float = 15.0):
    """Replace turn windows INSIDE route polylines with tangent arcs —
    in place, so the edge stays one edge and the corner/chord path is
    GONE (user: merge the overlap, the segmented turn yields to the
    arc).  A window is a maximal run of same-direction deflection
    vertices: a single sharp corner (one vertex) and an apt.dat
    chord-approximated curve (several small-angle vertices) are the
    same case."""
    n_repl = 0
    for e in g.edges:
        if not e["alive"] or e["kind"] != "lane":
            continue
        r_std = R90_BY_SIZE.get(e.get("size") or "", R90_BY_SIZE[""])
        # vertices of arcs we place are DESIGN geometry — without this
        # guard the scan re-detects each arc as a fresh same-sign
        # deflection run and refits it forever
        protected: set = set()

        def _key(p):
            return (round(float(p[0]) * 2.0), round(float(p[1]) * 2.0))

        changed = True
        while changed:
            changed = False
            cs = e["cs"]
            if len(cs) < 3:
                break
            # per-vertex signed deflection.  STRICTLY INTERIOR windows
            # only (k0-1 >= 1, k1+1 <= len-2): the first and last
            # segments of an edge are its junction tangents — bending
            # them made the arc pass fit bulging arcs off corrupted
            # directions (user re-test: ways 276/424)
            for k0 in range(2, len(cs) - 2):
                if _key(cs[k0]) in protected:
                    continue
                a = cs[k0] - cs[k0 - 1]
                na = float(np.hypot(*a))
                if na < 1e-6:
                    continue
                b = cs[k0 + 1] - cs[k0]
                nb = float(np.hypot(*b))
                if nb < 1e-6:
                    continue
                d0 = _angle_deg(tuple(a / na), tuple(b / nb))
                if d0 < 3.0:
                    continue
                sgn0 = np.sign(a[0] * b[1] - a[1] * b[0])
                # grow the window over consecutive same-sign deflections
                k1 = k0
                total = d0
                while k1 + 3 < len(cs):
                    if _key(cs[k1 + 1]) in protected:
                        break
                    u = cs[k1 + 1] - cs[k1]
                    v = cs[k1 + 2] - cs[k1 + 1]
                    nu, nv = float(np.hypot(*u)), float(np.hypot(*v))
                    if nu < 1e-6 or nv < 1e-6:
                        break
                    dd = _angle_deg(tuple(u / nu), tuple(v / nv))
                    if dd < 3.0 or np.sign(
                            u[0] * v[1] - u[1] * v[0]) != sgn0:
                        break
                    # windows longer than a plausible fillet are real
                    # route geometry, not a chord-approximated turn
                    if LineString(cs[k0 - 1:k1 + 3]).length > 6.0 * r_std:
                        break
                    k1 += 1
                    total += dd
                if total < min_turn_deg:
                    continue
                # entry/exit tangents; corner = tangent intersection
                u_in = _unit(*(cs[k0] - cs[k0 - 1]))
                u_out = _unit(*(cs[k1 + 1] - cs[k1]))
                A, B = cs[k0 - 1], cs[k1 + 1]
                den = u_in[0] * u_out[1] - u_in[1] * u_out[0]
                if abs(den) < 1e-9:
                    continue
                dp = B - A
                s1 = (dp[0] * u_out[1] - dp[1] * u_out[0]) / den
                P = A + np.asarray(u_in) * s1
                leg_in = float(np.hypot(*(P - A)))
                leg_out = float(np.hypot(*(B - P)))
                if s1 <= 1.0 or leg_in < 1.0 or leg_out < 1.0:
                    continue
                # start from the window's OWN radius (arc length /
                # turn) so a long gentle sweep is reproduced, not cut
                # inside by a standard-radius corner (user: the route
                # 'pushed well past' the real one) — sharp segmented
                # turns give r_fit ~ r_std and behave as before
                # multi-vertex windows are chord-approximated CURVES —
                # start from their own radius so the curve is
                # reproduced; single/double corners have no own radius,
                # start standard and let the follow-the-route cap
                # shrink to a tight corner fillet
                old_win = cs[k0 - 1:k1 + 2]
                if k1 - k0 >= 2:
                    win_len = float(LineString(old_win).length)
                    r_fit = win_len / math.radians(max(total, 1.0))
                    rr = min(max(r_fit, 0.25 * r_std), 400.0)
                else:
                    rr = r_std
                placed = None
                while rr >= 0.12 * r_std:
                    arc, t = _fillet(tuple(P), u_in, u_out, rr)
                    if arc is not None and t <= leg_in - 0.5 \
                            and t <= leg_out - 0.5 \
                            and pav_ok(LineString(arc)):
                        # the replacement must FOLLOW the route — BOTH
                        # ways: every old vertex near the new path AND
                        # every new sample near the old path (route
                        # vertices are ~75 m apart; a big-radius arc
                        # can sag unseen between them otherwise)
                        cand = LineString(np.vstack(
                            [[A], np.asarray(arc), [B]]))
                        old_line = LineString(old_win)
                        ns = max(4, int(cand.length / 5.0))
                        ok_dev = max(
                            cand.distance(Point(tuple(q)))
                            for q in old_win) <= 4.0 and max(
                            old_line.distance(cand.interpolate(
                                t2, normalized=True))
                            for t2 in np.linspace(0, 1, ns)) <= 4.0
                        if ok_dev:
                            placed = arc
                            break
                    rr *= 0.8
                if placed is None:
                    continue
                for p in placed:
                    protected.add(_key(p))
                new_cs = np.vstack([cs[:k0], np.asarray(placed),
                                    cs[k1 + 1:]])
                e["cs"] = new_cs
                n_repl += 1
                changed = True
                break
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[routearc] polyline turns replaced in place: {n_repl}",
              flush=True)


def _merge_deg2_lane_nodes(g: _Graph):
    """Merge the two lane edges at every pure degree-2 node, whatever
    the angle — a corner between two route edges becomes an INTERIOR
    vertex, so the in-place smoothing handles it as a tight corner
    fillet under the follow-the-route cap ('just at the corner, then
    straight segments' — user SE-end review), instead of the junction
    pass decorating it with a standard-radius arc plus kept legs."""
    changed = True
    while changed:
        changed = False
        for ni, ends in list(g.incident().items()):
            live = [(ei, aa) for ei, aa in ends if g.edges[ei]["alive"]]
            if len(live) != 2:
                continue
            (ea, aa), (eb, ab) = live
            if ea == eb:
                continue
            A, B = g.edges[ea], g.edges[eb]
            if A["kind"] != "lane" or B["kind"] != "lane":
                continue
            a_cs = A["cs"][::-1] if aa else A["cs"]     # node LAST
            b_cs = B["cs"] if ab else B["cs"][::-1]     # node FIRST
            merged = np.vstack([a_cs, b_cs[1:]])
            A["alive"] = False
            B["alive"] = False
            g.add_edge(merged, "lane",
                       max(A["size"] or "", B["size"] or ""),
                       max(A["w"], B["w"]))
            changed = True
            break


def _weld_touching_tips(g: _Graph, reach: float = 1.5):
    """T-junctions in the route graph: a stem's endpoint lies ON (or
    within noise of) a through route's INTERIOR — a coordinate touch,
    not a shared node, so nothing pairs there (the v10 exact-touch
    lesson).  Split the through edge at the touch point; identical
    coordinates weld by node key, so no connector is needed at d=0."""
    from collections import Counter
    from shapely.strtree import STRtree
    deg = Counter()
    for e in g.edges:
        if e["alive"]:
            deg[e["a"]] += 1
            deg[e["b"]] += 1
    alive = [(ei, e) for ei, e in enumerate(g.edges) if e["alive"]]
    lines = [LineString(e["cs"]) for _ei, e in alive]
    tree = STRtree(lines)
    welded = 0
    for ei, e in list(alive):
        if not e["alive"]:
            continue
        for ni in (e["a"], e["b"]):
            if deg[ni] != 1:
                continue
            p = Point(tuple(g.nodes[ni]))
            best = None
            for k in tree.query(p.buffer(reach)):
                k = int(k)
                oj, eo = alive[k]
                if oj == ei or not eo["alive"]:
                    continue
                d = lines[k].distance(p)
                if d <= reach and (best is None or d < best[0]):
                    # skip when the tip already IS an endpoint of that edge
                    if ni in (eo["a"], eo["b"]):
                        continue
                    best = (d, k)
            if best is None:
                continue
            _d, k = best
            oj, eo = alive[k]
            s = float(lines[k].project(p))
            mid = g.split_edge(oj, s)
            if mid != ni:
                g.add_edge(np.asarray([g.nodes[ni], g.nodes[mid]]),
                           "lane", eo.get("size", ""), 0.0)
            deg[ni] += 1
            welded += 1
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[routearc] T-stem tips welded: {welded}", flush=True)


def _smooth_connector_edges(g: _Graph, pav_ok):
    """The chord-connector variant of a segmented turn: a short LANE
    edge between two junction nodes whose interior is all chords.  It
    is smoothed IN PLACE — same endpoints, same nodes, the interior
    chords replaced by one fillet between the edge's end tangents.
    CONNECTIVITY IS NEVER TOUCHED (user: routes must not disconnect;
    the earlier absorb-and-reweld variant broke through lines)."""
    n_sm = 0
    for e in g.edges:
        if not e["alive"] or e["kind"] != "lane":
            continue
        cs = e["cs"]
        ln = LineString(cs)
        if len(cs) < 3 or ln.length > 120.0:
            continue
        u_in = _unit(*(cs[1] - cs[0]))
        u_out = _unit(*(cs[-1] - cs[-2]))
        total = _angle_deg(u_in, u_out)
        if total < 15.0:
            continue
        A, B = cs[0], cs[-1]
        den = u_in[0] * u_out[1] - u_in[1] * u_out[0]
        if abs(den) < 1e-9:
            continue
        dp = B - A
        s1 = (dp[0] * u_out[1] - dp[1] * u_out[0]) / den
        P = A + np.asarray(u_in) * s1
        leg_in = float(np.hypot(*(P - A)))
        leg_out = float(np.hypot(*(B - P)))
        if s1 <= 1.0 or leg_in < 1.0 or leg_out < 1.0:
            continue
        r_fit = ln.length / math.radians(max(total, 1.0))
        placed = None
        rr = min(max(r_fit, 4.0), 400.0)
        while rr >= 3.0:
            arc, t = _fillet(tuple(P), u_in, u_out, rr)
            if arc is not None and t <= leg_in - 0.25 \
                    and t <= leg_out - 0.25:
                cand_cs = np.vstack([[A], np.asarray(arc), [B]])
                cand = LineString(cand_cs)
                if pav_ok(cand) and max(
                        cand.distance(Point(tuple(q)))
                        for q in cs) <= 4.0:
                    placed = cand_cs
                    break
            rr *= 0.8
        if placed is None:
            continue
        placed[0] = g.nodes[e["a"]]
        placed[-1] = g.nodes[e["b"]]
        e["cs"] = placed
        n_sm += 1
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[routearc] connector edges smoothed in place: {n_sm}",
              flush=True)


def _drop_duplicate_arcs(g: _Graph, gap: float = 3.0):
    """ADDED arcs that duplicate existing route geometry die — the
    route is authoritative for distance, the arc was only ever a
    smoothness addition (inverse of the retired absorb pass, which
    deleted route edges and broke through lines)."""
    from shapely.strtree import STRtree
    lanes = [e for e in g.edges if e["alive"] and e["kind"] == "lane"]
    if not lanes:
        return
    lane_lines = [LineString(e["cs"]) for e in lanes]
    tree = STRtree(lane_lines)
    killed = 0
    for e in g.edges:
        if not e["alive"] or e["kind"] not in ("arc", "blend"):
            continue
        ln = LineString(e["cs"])
        n = max(3, int(ln.length / 5.0))
        pts = [ln.interpolate(t, normalized=True)
               for t in np.linspace(0.1, 0.9, n)]
        for i in tree.query(ln.buffer(gap)):
            i = int(i)
            if not lanes[i]["alive"]:
                continue
            near = sum(1 for p in pts
                       if lane_lines[i].distance(p) <= gap)
            if near >= 0.85 * n:
                e["alive"] = False
                killed += 1
                break
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[routearc] duplicate arcs dropped: {killed}", flush=True)


def _build_route_arc_graph(routes, pav_all, runway_union, dbg=False):
    """The v13 core: route graph verbatim + welds + in-place turn
    smoothing + walk-mode standard-radius junction arcs + runway turns.
    Distances are preserved by construction — no route edge is ever
    deleted; every smoothing keeps its endpoints."""
    g = _Graph()
    n_routes = 0
    for tc in routes or []:
        if getattr(tc, "is_service", False):
            continue
        ln = getattr(tc, "chained_line", None) or getattr(tc, "line", None)
        if ln is None or ln.is_empty or ln.length < 2.0:
            continue
        size = ""
        try:
            size = tc.dominant_size() or ""
        except Exception:
            pass
        g.add_edge(np.asarray(ln.coords, dtype=float), "lane", size, 0.0)
        n_routes += 1

    # weld crossings and stem-on-interior T junctions into real nodes
    _planarize_crossings(g)
    _weld_touching_tips(g)
    _merge_deg2_lane_nodes(g)
    if dbg:
        alive = sum(1 for e in g.edges if e["alive"])
        L = sum(LineString(e["cs"]).length for e in g.edges if e["alive"])
        print(f"[routearc] routes={n_routes} edges={alive} "
              f"len={L/1000:.1f}km", flush=True)

    allow = shapely.buffer(pav_all, 0.5)

    def pav_ok(line: LineString) -> bool:
        return allow.contains(line)

    len_before = sum(LineString(e["cs"]).length
                     for e in g.edges if e["alive"])
    # turns INSIDE polylines are smoothed IN PLACE — strictly interior,
    # endpoints/nodes/junction-tangents untouched, connectivity
    # preserved by construction
    _replace_polyline_turns(g, pav_ok)
    # r_start_for at exactly r_std keeps the standard radii but switches
    # placement to _walk_locate: a branch hosting TWO arcs gets split by
    # the first, and the second's tangent point must walk across the
    # split fragment (the missing-quadrant bug)
    _add_junction_arcs(g, pav_ok, runway_union=None, gamma_max=120.0,
                       r_start_for=lambda P, r_std: r_std)
    _add_runway_turns(g, runway_union, pav_all)
    # an ADDED arc duplicating route geometry dies, never the route
    _drop_duplicate_arcs(g)
    g.consolidate()
    if dbg:
        n_arc = sum(1 for e in g.edges if e["alive"]
                    and e["kind"] in ("arc", "blend", "rwy_turn"))
        len_after = sum(LineString(e["cs"]).length
                        for e in g.edges if e["alive"])
        print(f"[routearc] arcs={n_arc} length {len_before/1000:.2f} -> "
              f"{len_after/1000:.2f} km (corner rounding only)",
              flush=True)
    return g


def synthesize_spine_v13(
    pav, runway_union=None, buildings=None, routes=None, *,
    terminal_setback: float = 100.0, recognized=None, ramps=None,
    rwy_full=None,
) -> list[SpineWay]:
    del recognized, ramps, terminal_setback
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
    # thru-runway pavement (user ruling): arcs may live anywhere on the
    # continuous footprint, runway included
    parts = [pav_nav]
    if rwy_full is not None and not rwy_full.is_empty:
        parts.append(rwy_full)
    elif runway_union is not None and not runway_union.is_empty:
        parts.append(runway_union)
    pav_all = unary_union(parts)
    g = _build_route_arc_graph(routes, pav_all, runway_union,
                               dbg=bool(os.environ.get("O4_ET_DEBUG")))
    return g.ways()


def apply_route_arc_spine(layout, icao: str = "") -> int:
    """PRODUCTION wiring (user 2026-07-02): rebuild the non-service taxi
    centerlines as the route-arc spine — the apt.dat route graph
    verbatim (metric-true distances for the feasibility/anchor math)
    plus standard-radius arcs at every junction turn, bend and runway
    contact — right before the global slice consumes them.  Service
    routes pass through untouched.

    Gate ``config.ROUTE_ARC_SPINE`` (env O4_ROUTE_ARC_SPINE).  Called from
    the pipeline's GLOBAL-SLICE stage (user ruling 2026-07-02): with the
    full route-arc spine the taxi-rect pipeline is disabled and pav_union
    is cut once by these ways — the spine runs everywhere."""
    from ..config import ROUTE_ARC_SPINE
    if not ROUTE_ARC_SPINE:
        return 0
    cls = list(getattr(layout, "apt_taxi_centerlines", None) or [])
    if not cls:
        return 0
    keep = [t for t in cls if getattr(t, "is_service", False)]
    # one entry per CONTINUOUS chained route (pieces share the parent)
    routes, seen = [], set()
    for tc in cls:
        if getattr(tc, "is_service", False):
            continue
        ln = getattr(tc, "chained_line", None) or getattr(tc, "line", None)
        if ln is None or ln.is_empty or id(ln) in seen:
            continue
        seen.add(id(ln))
        routes.append(tc)
    if not routes:
        return 0

    building_union = None
    try:
        polys = [s.polygon for s in getattr(layout, "shapes", []) or []
                 if getattr(s, "role", "") in ("building", "terminal")
                 and getattr(s, "polygon", None) is not None
                 and not s.polygon.is_empty]
        if polys:
            building_union = unary_union(polys)
    except Exception:
        building_union = None
    pav = getattr(layout, "source_pavement_union", None)
    rwy = getattr(layout, "runway_union", None)
    parts = []
    if pav is not None and not pav.is_empty:
        if building_union is not None:
            try:
                pav = pav.difference(shapely.buffer(building_union, 0.5))
            except Exception:
                pass
        parts.append(pav)
    if rwy is not None and not rwy.is_empty:
        parts.append(rwy)
    if not parts:
        return 0
    pav_all = unary_union(parts)

    g = _build_route_arc_graph(routes, pav_all, rwy,
                               dbg=bool(os.environ.get("O4_ET_DEBUG")))
    from ..apt_dat_reader import TaxiCenterline
    new_cls = []
    for w in g.ways():
        if w.line is None or w.line.is_empty or w.line.length < 1.0:
            continue
        nseg = max(1, len(w.line.coords) - 1)
        new_cls.append(TaxiCenterline(
            line=w.line, seg_sizes=[w.size or ""] * nseg,
            is_service=False,
            name="route_arc" if w.kind != "lane" else "route",
            route_line=None))
    if not new_cls:
        return 0
    layout.apt_taxi_centerlines = keep + new_cls
    try:
        import O4_UI_Utils as UI
        n_arc = sum(1 for t in new_cls if t.name == "route_arc")
        UI.vprint(1, f"  [pav-builder] {icao}: route-arc spine — "
                     f"{len(new_cls)} centerline(s), {n_arc} arc(s), "
                     f"{len(keep)} service route(s) kept.")
    except Exception:
        pass
    return len(new_cls)
