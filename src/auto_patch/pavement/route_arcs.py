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
from .edge_trace import _planarize_crossings


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
            # per-vertex signed deflection
            for k0 in range(1, len(cs) - 1):
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
                while k1 + 2 < len(cs):
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
                placed = None
                rr = r_std
                while rr >= 0.25 * r_std:
                    arc, t = _fillet(tuple(P), u_in, u_out, rr)
                    if arc is not None and t <= leg_in - 0.5 \
                            and t <= leg_out - 0.5 \
                            and pav_ok(LineString(arc)):
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


def _absorb_turn_chords(g: _Graph, gap: float = 6.0):
    """Kill short LANE edges that duplicate an arc — the apt.dat
    chord-connector variant of a segmented turn, where the chords are a
    separate edge between two routes rather than vertices inside one.
    The arc's tangent welds carry the connection afterwards."""
    from shapely.strtree import STRtree
    arcs = [e for e in g.edges
            if e["alive"] and e["kind"] in ("arc", "blend", "rwy_turn")]
    if not arcs:
        return
    arc_lines = [LineString(e["cs"]) for e in arcs]
    tree = STRtree(arc_lines)
    killed = 0
    for e in g.edges:
        if not e["alive"] or e["kind"] != "lane":
            continue
        ln = LineString(e["cs"])
        if ln.length > 90.0:
            continue
        n = max(3, int(ln.length / 6.0))
        pts = [ln.interpolate(t, normalized=True)
               for t in np.linspace(0.05, 0.95, n)]
        for i in tree.query(ln.buffer(gap)):
            i = int(i)
            if not arcs[i]["alive"]:
                continue
            near = sum(1 for p in pts if arc_lines[i].distance(p) <= gap)
            if near >= 0.8 * n:
                e["alive"] = False
                killed += 1
                break
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[routearc] turn chords absorbed into arcs: {killed}",
              flush=True)


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
    dbg = bool(os.environ.get("O4_ET_DEBUG"))

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
    # turns INSIDE polylines (single corners and chord-approximated
    # curves alike) are replaced in place — the overlap merge
    _replace_polyline_turns(g, pav_ok)
    _add_junction_arcs(g, pav_ok, runway_union=None)
    _add_runway_turns(g, runway_union, pav_all)
    # chord-connector edges duplicating a junction arc yield to it;
    # their neighbor legs then dead-end a few metres from the arc —
    # weld those tips onto it (same exact-touch machinery, wider reach)
    _absorb_turn_chords(g)
    _weld_touching_tips(g, reach=8.0)
    g.consolidate()
    if dbg:
        n_arc = sum(1 for e in g.edges if e["alive"]
                    and e["kind"] in ("arc", "blend", "rwy_turn"))
        len_after = sum(LineString(e["cs"]).length
                        for e in g.edges if e["alive"])
        print(f"[routearc] arcs={n_arc} length {len_before/1000:.2f} -> "
              f"{len_after/1000:.2f} km (corner rounding only)",
              flush=True)
    return g.ways()
