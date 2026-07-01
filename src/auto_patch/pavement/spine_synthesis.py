"""Pavement-based taxi-spine synthesis — a WELDED graph of straight lanes
and standard arcs, like the centerline network on an airport diagram.

Design rules (user 2026-07-01, refined against the approved
``SPJC_curved_spine.kml`` target):

* **Navigability invariant.** An aircraft starting anywhere on the spine can
  reach every part of it rolling along spine edges — no right-angle turns,
  wheels never leaving the line.  Everything is straights + tangent arcs,
  and every way ends on a node shared with its neighbours (floating ends =
  defects, gated by the QA tool).
* **Lanes take the straight path.**  A taxiway centerline is the maximal
  straight chord down the middle of its pavement; junctions and widenings
  never deflect it.  Holes identify branches (and widths).
* **One STANDARD arc radius per taxiway size** (ICAO code letter; measured
  half-width as fallback).  Turn arcs everywhere use that radius — a sharper
  turn just makes the arc LONGER, not a different size.  Two parallels plus
  a connector form an "H": the connector stays a straight cross-piece and
  the four 90° corners get four EQUAL arcs (arc–straight–arc movements, not
  S-diagonals).
* **Runway contacts**: square-in keeps a single edge node.  A high-speed
  DIAGONAL keeps its straight (shallow) connection and adds one long
  standard-radius arc for the sharp (~135°) turn, welded to the diagonal
  and landing on the runway edge.
* **Buildings**: pads are subtracted before skeletonizing (a spine never
  enters one); small buildings get a lead-in stub welded to the nearest
  lane; larger buildings/terminals get a perimeter ring at a setback,
  pieces welded to the network or dropped (no floaters).

Construction is from the pavement footprint ONLY.  The apt.dat route data
is consulted ONLY to label a lane with its ICAO size letter (an attribute
lookup — geometry never comes from it); without routes the size falls back
to the measured half-width.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

from .pav_skeleton import build_pavement_skeleton, _polygons

# ── standards constants ──────────────────────────────────────────────────────
# Centerline turn radius (m) at a 90° turn by ICAO code letter — the values
# the user verified on Google Earth (taxi_route_fillets.py, 2026-06-30).
# The SAME radius serves every turn angle; only the arc length changes.
R90_BY_SIZE = {"A": 12.0, "B": 16.0, "C": 22.0, "D": 30.0, "E": 36.0,
               "F": 45.0, "": 22.0}
# Measured lane half-width → size letter fallback (no route data).
_SIZE_BY_HALFWIDTH = ((4.0, "A"), (5.5, "B"), (8.0, "C"), (10.5, "D"),
                      (13.5, "E"), (99.0, "F"))

_TURN_MIN_DEG = 25.0      # flatter meetings are through-continuations
_TURN_MAX_DEG = 155.0     # sharper pairs are fold-backs, no direct movement
_THROUGH_MAX_DEG = 40.0   # body-axis pairs this collinear form one through
_MIN_ARC_FIT = 0.25       # arc may shrink to this × standard before we skip
#                           (tight island-tip corners are pavement-limited —
#                            a smaller arc there is correct, not a defect)
_CHORD_CLEAR_M = 2.0      # a straight chord needs this edge clearance
_FAIR_SIMPLIFY_M = 1.2
_NODE_KEY_M = 0.05        # node weld quantum
_DIAG_MIN_DEG = 20.0      # runway diagonal band
_DIAG_MAX_DEG = 65.0

SMALL_BUILDING_M2 = 2000.0
TERMINAL_SETBACK_M = 100.0
_STUB_MAX_REACH_M = 80.0
_RING_MIN_ARC_M = 30.0
_RING_LANE_CLEAR_M = 15.0
_RING_WELD_REACH_M = 25.0


@dataclass
class SpineWay:
    line: LineString
    kind: str          # lane | arc | rwy_turn | building_stub | building_ring
    size: str = ""     # ICAO code letter
    halfwidth: float = 0.0


# ── small geometry helpers ───────────────────────────────────────────────────

def _unit(dx: float, dy: float):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-9 else (0.0, 0.0)


def _angle_deg(u, v) -> float:
    d = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1]))
    return math.degrees(math.acos(d))


def _arc_pts(center, r, a0, a1, ccw: bool, step_m: float = 4.0):
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


def _fillet(P, u_in, u_out, r, gamma_max=_TURN_MAX_DEG):
    """Tangent arc at corner ``P``: arrive along ``u_in``, depart along
    ``u_out``.  Returns (arc_coords, tangent_len) or (None, 0)."""
    gamma = math.acos(max(-1.0, min(1.0,
        u_in[0] * u_out[0] + u_in[1] * u_out[1])))
    if gamma < math.radians(_TURN_MIN_DEG) \
            or gamma > math.radians(gamma_max):
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
    return _arc_pts(c, r, a0, a1, ccw=(sgn > 0)), t


def _chord_straighten(coords, radii, chord_ok):
    """Longest straight chords that fit the pavement (2 m clearance) and stay
    laterally near the medial.  Endpoints are always preserved."""
    cs = np.asarray(coords)
    rr = np.asarray(radii, dtype=float) if len(radii) else np.full(len(cs), 8.0)
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
            rel = cs[i:j + 1] - a
            dev = np.abs(rel[:, 0] * ab[1] - rel[:, 1] * ab[0]) / L
            w_med = float(np.median(rr[i:j + 1]))
            if float(dev.max()) <= max(3.0, 0.9 * w_med) \
                    and chord_ok(LineString([tuple(a), tuple(b)])):
                chosen = j
                break
            j = i + max(1, int((j - i) * 0.7))
        out_idx.append(chosen)
        i = chosen
    return cs[out_idx], rr[out_idx]


# ── the welded spine graph ───────────────────────────────────────────────────

class _Graph:
    """Nodes are first-class; every edge's polyline STARTS and ENDS exactly
    at its nodes' positions.  All geometry edits go through node moves and
    edge splits, so the network can never come apart."""

    def __init__(self):
        self.nodes: list[np.ndarray] = []
        self._key2node: dict = {}
        # edge: dict(a, b, cs Nx2, kind, size, w, alive)
        self.edges: list[dict] = []

    def _key(self, xy):
        return (round(xy[0] / _NODE_KEY_M), round(xy[1] / _NODE_KEY_M))

    def add_node(self, xy) -> int:
        k = self._key(xy)
        if k in self._key2node:
            return self._key2node[k]
        self.nodes.append(np.asarray(xy, dtype=float))
        self._key2node[k] = len(self.nodes) - 1
        return len(self.nodes) - 1

    def add_edge(self, cs, kind, size="", w=0.0) -> int:
        cs = np.asarray(cs, dtype=float)
        a = self.add_node(cs[0])
        b = self.add_node(cs[-1])
        cs[0], cs[-1] = self.nodes[a], self.nodes[b]
        self.edges.append(dict(a=a, b=b, cs=cs, kind=kind, size=size,
                               w=w, alive=True))
        return len(self.edges) - 1

    def incident(self) -> dict:
        inc = defaultdict(list)
        for ei, e in enumerate(self.edges):
            if not e["alive"]:
                continue
            inc[e["a"]].append((ei, True))
            inc[e["b"]].append((ei, False))
        return inc

    def move_node(self, ni: int, xy):
        """Relocate a node; every incident edge's end vertex follows.  The
        key map MUST move with it — otherwise a later add_node at the new
        position mints a duplicate node and silently forks the graph."""
        old_k = self._key(self.nodes[ni])
        if self._key2node.get(old_k) == ni:
            del self._key2node[old_k]
        self.nodes[ni] = np.asarray(xy, dtype=float)
        # first owner keeps a contested key (rare exact-coincidence case)
        self._key2node.setdefault(self._key(xy), ni)
        for e in self.edges:
            if not e["alive"]:
                continue
            if e["a"] == ni:
                e["cs"][0] = self.nodes[ni]
            if e["b"] == ni:
                e["cs"][-1] = self.nodes[ni]

    def edge_dir_at(self, ei: int, at_a: bool, back_m: float = 20.0):
        """Unit direction ARRIVING at the given end along the edge."""
        cs = self.edges[ei]["cs"]
        if at_a:
            cs = cs[::-1]
        acc, i = 0.0, len(cs) - 1
        while i > 0 and acc < back_m:
            acc += float(np.hypot(*(cs[i] - cs[i - 1])))
            i -= 1
        v = cs[-1] - cs[i]
        return _unit(float(v[0]), float(v[1]))

    def split_edge(self, ei: int, s: float) -> int:
        """Split edge ``ei`` at arc length ``s``; returns the new mid node.
        The two halves keep the edge's kind/size."""
        e = self.edges[ei]
        ln = LineString(e["cs"])
        s = max(0.5, min(ln.length - 0.5, s))
        p = ln.interpolate(s)
        # build vertex lists
        first, second = [], []
        acc = 0.0
        cs = e["cs"]
        first.append(cs[0])
        done = False
        for k in range(1, len(cs)):
            d = float(np.hypot(*(cs[k] - cs[k - 1])))
            if not done and acc + d >= s:
                first.append([p.x, p.y])
                second.append([p.x, p.y])
                second.extend(cs[k:])
                done = True
                break
            acc += d
            first.append(cs[k])
        if not done:
            return e["b"]
        mid = self.add_node([p.x, p.y])
        e["alive"] = False
        self.add_edge(np.asarray(first), e["kind"], e["size"], e["w"])
        self.add_edge(np.asarray(second), e["kind"], e["size"], e["w"])
        return mid

    def consolidate(self):
        """Merge degree-2 nodes where two same-kind, same-size edges continue
        near-collinearly — undoes split fragmentation so the emitted ways are
        clean long lanes.  Genuine corners (real turns) keep their node."""
        changed = True
        while changed:
            changed = False
            for ni, ends in self.incident().items():
                live = [(ei, aa) for ei, aa in ends if self.edges[ei]["alive"]]
                if len(live) != 2:
                    continue
                (ea, aa), (eb, ab) = live
                if ea == eb:
                    continue
                A, B = self.edges[ea], self.edges[eb]
                if A["kind"] != B["kind"] or A["size"] != B["size"]:
                    continue
                u = self.edge_dir_at(ea, aa)
                v = self.edge_dir_at(eb, ab)
                if _angle_deg(u, (-v[0], -v[1])) > 30.0:
                    continue
                a_cs = A["cs"][::-1] if aa else A["cs"]     # node LAST
                b_cs = B["cs"] if ab else B["cs"][::-1]     # node FIRST
                merged = np.vstack([a_cs, b_cs[1:]])
                A["alive"] = False
                B["alive"] = False
                self.add_edge(merged, A["kind"], A["size"],
                              max(A["w"], B["w"]))
                changed = True
                break

    def ways(self) -> list[SpineWay]:
        out = []
        for e in self.edges:
            if not e["alive"]:
                continue
            ln = LineString(e["cs"])
            if ln.length < 0.5:
                continue
            out.append(SpineWay(ln, e["kind"], e["size"], e["w"]))
        return out


# ── through-path assembly + straightening (graph-preserving) ─────────────────

def _assemble_through_paths(g: _Graph):
    """Pair edge-ends at every node by BEST continuation angle (body axis,
    45 m back); paths of lane edges linked by pairs are the taxiway lanes."""
    inc = g.incident()
    partner: dict = {}                     # (ei, at_a) -> (ej, at_a_j)
    for ni, ends in inc.items():
        lane_ends = [(ei, at_a) for (ei, at_a) in ends
                     if g.edges[ei]["kind"] == "lane"]
        if len(lane_ends) < 2:
            continue
        cand = []
        for x in range(len(lane_ends)):
            for y in range(x + 1, len(lane_ends)):
                (ea, aa), (eb, ab) = lane_ends[x], lane_ends[y]
                if ea == eb:
                    continue
                u = g.edge_dir_at(ea, aa, back_m=45.0)
                v = g.edge_dir_at(eb, ab, back_m=45.0)
                ang = _angle_deg(u, (-v[0], -v[1]))
                if ang <= _THROUGH_MAX_DEG:
                    cand.append((ang, (ea, aa), (eb, ab)))
        used: set = set()
        for ang, A, B in sorted(cand, key=lambda t: t[0]):
            if A in used or B in used or A[0] == B[0]:
                continue
            used.add(A)
            used.add(B)
            partner[A] = B
            partner[B] = A
    # walk paths
    visited: set = set()
    paths = []
    for ei, e in enumerate(g.edges):
        if not e["alive"] or e["kind"] != "lane" or ei in visited:
            continue
        # find a path start: walk backwards from (ei, at_a=True)
        cur = (ei, True)
        seen_guard = set()
        while cur in partner and cur not in seen_guard:
            seen_guard.add(cur)
            nxt_e, nxt_end = partner[cur]
            cur = (nxt_e, not nxt_end)     # continue past the partner edge
        start = cur
        # walk forward collecting the ordered edge list
        path = []
        cur = start
        while True:
            ecur, at_a = cur
            if ecur in visited:
                break
            visited.add(ecur)
            path.append((ecur, at_a))
            other = (ecur, not at_a)
            if other not in partner:
                break
            cur_e, cur_end = partner[other]
            cur = (cur_e, cur_end)
        if path:
            paths.append(path)
    return paths


def _straighten_paths(g: _Graph, paths, chord_ok):
    """Chord-straighten each through path as ONE polyline, then relocate the
    path's interior nodes onto the new geometry and rebuild the member edges
    between them.  Endpoints and all welds survive by construction."""
    for path in paths:
        seq = []
        radii = []
        node_seq = []                      # nodes along the path, in order
        for k, (ei, at_a) in enumerate(path):
            e = g.edges[ei]
            cs = e["cs"] if at_a else e["cs"][::-1]
            node_seq.append(e["a"] if at_a else e["b"])
            if k == 0:
                seq.extend(cs.tolist())
            else:
                seq.extend(cs[1:].tolist())
            radii.extend([e["w"]] * (len(cs) - (0 if k == 0 else 1)))
        last = path[-1]
        node_seq.append(g.edges[last[0]]["b"] if last[1]
                        else g.edges[last[0]]["a"])
        coords = np.asarray(seq)
        straight, _r = _chord_straighten(coords, np.asarray(radii), chord_ok)
        path_ln = LineString(straight)
        # relocate interior nodes onto the straightened path
        s_of_node = [0.0]
        for ni in node_seq[1:-1]:
            s_of_node.append(path_ln.project(Point(tuple(g.nodes[ni]))))
        s_of_node.append(path_ln.length)
        # enforce monotonic order (projection can fold on tight geometry)
        for k in range(1, len(s_of_node)):
            s_of_node[k] = max(s_of_node[k], s_of_node[k - 1] + 0.5)
        for k, ni in enumerate(node_seq[1:-1], start=1):
            p = path_ln.interpolate(min(s_of_node[k], path_ln.length))
            g.move_node(ni, [p.x, p.y])
        # rebuild each member edge's geometry as its slice of the path
        for k, (ei, at_a) in enumerate(path):
            s0, s1 = s_of_node[k], s_of_node[k + 1]
            n_pts = max(2, int((s1 - s0) / 5) + 1)
            pts = [path_ln.interpolate(s).coords[0]
                   for s in np.linspace(s0, s1, n_pts)]
            e = g.edges[ei]
            na = e["a"] if at_a else e["b"]
            nb = e["b"] if at_a else e["a"]
            pts[0] = tuple(g.nodes[na])
            pts[-1] = tuple(g.nodes[nb])
            cs = np.asarray(pts)
            e["cs"] = cs if at_a else cs[::-1]


def _fair_edge_bends(g: _Graph, pav_ok):
    """Replace interior bend vertices of every lane edge with STANDARD-radius
    tangent arcs (shrunk to fit the pavement).  Endpoints are untouched, so
    all welds survive — an aircraft never meets a corner mid-lane."""
    for e in g.edges:
        if not e["alive"] or e["kind"] != "lane":
            continue
        cs = e["cs"]
        if len(cs) < 3:
            continue
        r_std = _radius_for(e["size"])
        out = [cs[0]]
        for i in range(1, len(cs) - 1):
            P = cs[i]
            u_in = _unit(*(P - out[-1]))
            u_out = _unit(*(cs[i + 1] - P))
            gamma = _angle_deg(u_in, u_out)
            if gamma < _TURN_MIN_DEG:
                out.append(P)
                continue
            r = r_std
            placed = False
            while r >= 0.25 * r_std:
                arc, t = _fillet(tuple(P), u_in, u_out, r)
                if arc is None:
                    break
                d_prev = float(np.hypot(*(P - out[-1])))
                d_next = float(np.hypot(*(cs[i + 1] - P)))
                if t <= 0.7 * d_prev and t <= 0.7 * d_next \
                        and pav_ok(LineString(arc)):
                    out.extend(arc)
                    placed = True
                    break
                r *= 0.75
            if not placed:
                out.append(P)
        out.append(cs[-1])
        e["cs"] = np.asarray(out)


# ── size attribution ─────────────────────────────────────────────────────────

def _size_for_halfwidth(w: float) -> str:
    for cap, letter in _SIZE_BY_HALFWIDTH:
        if w < cap:
            return letter
    return "F"


def _attribute_sizes(g: _Graph, routes):
    """ICAO size letters: from the nearest apt.dat route where available
    (ATTRIBUTE lookup only — geometry never comes from routes), else from
    the measured half-width."""
    route_geoms, route_sizes = [], []
    for rt in routes or []:
        ln = getattr(rt, "chained_line", None) or getattr(rt, "line", None)
        if ln is None or ln.is_empty or getattr(rt, "is_service", False):
            continue
        sz = getattr(rt, "dominant_size", lambda: "")() or ""
        if sz:
            route_geoms.append(ln)
            route_sizes.append(sz)
    tree = STRtree(route_geoms) if route_geoms else None
    for e in g.edges:
        if not e["alive"] or e["kind"] != "lane":
            continue
        size = ""
        if tree is not None:
            ln = LineString(e["cs"])
            mid = ln.interpolate(0.5, normalized=True)
            best = None
            for gi in tree.query(mid.buffer(30.0)):
                d = route_geoms[int(gi)].distance(mid)
                if d < 30.0 and (best is None or d < best[0]):
                    best = (d, route_sizes[int(gi)])
            if best:
                size = best[1]
        e["size"] = size or _size_for_halfwidth(e["w"] or 8.0)


# ── junction arcs (welded) ───────────────────────────────────────────────────

def _radius_for(size: str) -> float:
    return R90_BY_SIZE.get(size or "", R90_BY_SIZE[""])


def _add_junction_arcs(g: _Graph, pav_ok):
    """At every node, one STANDARD arc per branch-direction pair with a real
    turn between them.  Tangent points split the branch edges, so the arc is
    welded into the graph at real shared nodes.  This turns an H junction
    (two parallels + straight rung) into exactly four equal arcs.

    Splitting an edge retires it and re-creates its halves, so the incident
    edge list is re-resolved after every placement; pairs already handled
    (or judged unplaceable) are remembered by their direction signature."""
    node_ids = list(g.incident().keys())
    for ni in node_ids:
        P = g.nodes[ni]
        done: set = set()
        for _guard in range(24):           # max arcs per node, safety bound
            ends = [(ei, at_a) for (ei, at_a) in g.incident().get(ni, [])
                    if g.edges[ei]["alive"] and g.edges[ei]["kind"] == "lane"]
            placed_this_round = False
            for x in range(len(ends)):
                for y in range(x + 1, len(ends)):
                    (ea, aa), (eb, ab) = ends[x], ends[y]
                    if ea == eb:
                        continue
                    u = g.edge_dir_at(ea, aa)      # arriving along a
                    v = g.edge_dir_at(eb, ab)      # arriving along b
                    u_out = (-v[0], -v[1])         # departing along b
                    key = tuple(sorted((
                        (round(u[0], 1), round(u[1], 1)),
                        (round(v[0], 1), round(v[1], 1)))))
                    if key in done:
                        continue
                    done.add(key)
                    gamma = _angle_deg(u, u_out)
                    if gamma < _TURN_MIN_DEG or gamma > _TURN_MAX_DEG:
                        continue
                    size = min(g.edges[ea]["size"] or "C",
                               g.edges[eb]["size"] or "C")
                    r_std = _radius_for(size)
                    len_a = LineString(g.edges[ea]["cs"]).length
                    len_b = LineString(g.edges[eb]["cs"]).length
                    r = r_std
                    placed = None
                    while r >= _MIN_ARC_FIT * r_std:
                        arc, t = _fillet(tuple(P), u, u_out, r)
                        if arc is None:
                            break
                        if t <= 0.75 * len_a and t <= 0.75 * len_b \
                                and pav_ok(LineString(arc)):
                            placed = (arc, t)
                            break
                        r *= 0.75
                    if placed is None:
                        continue
                    arc, t = placed
                    w_pair = g.edges[ea]["w"]
                    s_a = t if aa else len_a - t
                    na = g.split_edge(ea, s_a)
                    s_b = t if ab else len_b - t
                    nb = g.split_edge(eb, s_b)
                    cs = np.asarray(arc)
                    cs[0] = g.nodes[na]
                    cs[-1] = g.nodes[nb]
                    g.add_edge(cs, "arc", size, w_pair)
                    placed_this_round = True
                    break
                if placed_this_round:
                    break
            if not placed_this_round:
                break
    return


# ── runway diagonals ─────────────────────────────────────────────────────────

def _walk_locate(g: _Graph, ei: int, from_a: bool, t: float, max_hops=6):
    """Locate arc length ``t`` measured from one end of edge ``ei``, walking
    across near-collinear lane continuations when ``t`` overruns the edge —
    a big sharp-turn arc's tangent point often lies beyond the tip fragment
    that junction-arc splits left behind.  Returns (edge, s) or None."""
    for _hop in range(max_hops):
        e = g.edges[ei]
        L = LineString(e["cs"]).length
        if t <= L - 1.0:
            return ei, (t if from_a else L - t)
        far = e["b"] if from_a else e["a"]
        u = g.edge_dir_at(ei, at_a=not from_a)     # arriving at the far end
        nxt = None
        for (ej, aj) in g.incident().get(far, []):
            if ej == ei or not g.edges[ej]["alive"] \
                    or g.edges[ej]["kind"] != "lane":
                continue
            v = g.edge_dir_at(ej, at_a=aj)
            if _angle_deg(u, (-v[0], -v[1])) <= 30.0:
                nxt = (ej, aj)
                break
        if nxt is None:
            return None
        t -= L
        ei, from_a = nxt
    return None


def _add_runway_turns(g: _Graph, runway_union, pav_eff):
    """The sharp-turn arc lands ON the runway edge and can sweep up to
    ~160 deg on shallow diagonals, so it gets its own containment slack
    (1 m) and gamma ceiling."""
    if runway_union is None or runway_union.is_empty:
        return
    allow_rwy = shapely.buffer(pav_eff, 1.0)
    edge_b = runway_union.boundary
    for ei in range(len(g.edges)):
        e = g.edges[ei]
        if not e["alive"] or e["kind"] != "lane":
            continue
        for at_a in (True, False):
            tip = e["cs"][0] if at_a else e["cs"][-1]
            P = Point(tuple(tip))
            if edge_b.distance(P) > 1.5:
                continue
            u_in = g.edge_dir_at(ei, not at_a)
            if at_a:
                u_in = (-u_in[0], -u_in[1])
            s = edge_b.project(P)
            q0 = edge_b.interpolate(max(0.0, s - 10.0))
            q1 = edge_b.interpolate(min(edge_b.length, s + 10.0))
            e_dir = _unit(q1.x - q0.x, q1.y - q0.y)
            if e_dir == (0.0, 0.0):
                continue
            ang = _angle_deg(u_in, e_dir)
            ang = min(ang, 180.0 - ang)
            if not (_DIAG_MIN_DEG <= ang <= _DIAG_MAX_DEG):
                continue
            r_std = _radius_for(e["size"])
            for e_sgn in (1.0, -1.0):
                u_out = (e_dir[0] * e_sgn, e_dir[1] * e_sgn)
                if _angle_deg(u_in, u_out) < 95.0:
                    continue               # shallow side = the straight itself
                r, arc, t, loc = r_std, None, 0.0, None
                while r >= _MIN_ARC_FIT * r_std:
                    arc, t = _fillet(tuple(tip), u_in, u_out, r,
                                     gamma_max=162.0)
                    if arc is not None:
                        loc = _walk_locate(g, ei, at_a, t)
                        if loc is not None \
                                and allow_rwy.contains(LineString(arc)):
                            break
                    arc = None
                    r *= 0.75
                if arc is None or loc is None:
                    continue
                na = g.split_edge(loc[0], loc[1])
                cs = np.asarray(arc)
                cs[0] = g.nodes[na]
                g.add_edge(cs, "rwy_turn", e["size"], e["w"])
                break                      # one sharp turn per tip
            if not e["alive"]:
                break
    return


# ── buildings ────────────────────────────────────────────────────────────────

def _add_building_ways(g: _Graph, buildings, pav_eff, setback: float):
    """Stubs and rings, WELDED into the graph (or dropped — no floaters)."""
    if not buildings:
        return
    lanes = [(ei, LineString(e["cs"])) for ei, e in enumerate(g.edges)
             if e["alive"] and e["kind"] == "lane"
             and LineString(e["cs"]).length > 20.0]

    def _weld_point(pt, reach):
        """Nearest lane point within reach → (edge idx, arc pos, Point)."""
        best = None
        for ei, ln in lanes:
            if not g.edges[ei]["alive"]:
                continue
            d = ln.distance(pt)
            if d < reach and (best is None or d < best[0]):
                best = (d, ei, ln.project(pt))
        return best

    ball = unary_union([b for b, _r in buildings])
    pav_allow = shapely.buffer(pav_eff, 1.0)
    for bpoly, role in buildings:
        big = (role == "terminal") or bpoly.area >= SMALL_BUILDING_M2
        if big:
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
        hit = _weld_point(Point(tuple(mid)), _STUB_MAX_REACH_M)
        if hit is None:
            continue
        _d, ei, s_pos = hit
        target_node = g.split_edge(ei, s_pos)
        stub = LineString([tuple(mid), tuple(g.nodes[target_node])])
        if stub.crosses(ball) or not pav_allow.contains(stub):
            continue
        g.add_edge(np.asarray(stub.coords), "building_stub")
        # refresh lane list (split invalidated one entry)
        lanes = [(k, LineString(e["cs"])) for k, e in enumerate(g.edges)
                 if e["alive"] and e["kind"] == "lane"
                 and LineString(e["cs"]).length > 20.0]

    bigs = [b for b, role in buildings
            if role == "terminal" or b.area >= SMALL_BUILDING_M2]
    if not bigs:
        return
    big_union = unary_union(bigs)
    spine_union = unary_union(
        [LineString(e["cs"]) for e in g.edges if e["alive"]])
    keep_zone = shapely.buffer(pav_eff, -0.3).difference(
        shapely.buffer(ball, 3.0))
    see_zone = shapely.buffer(pav_eff, 1.0)
    for comp in _polygons(big_union.buffer(setback, quad_segs=12)):
        clipped = LineString(comp.exterior.coords).intersection(keep_zone)
        for seg in getattr(clipped, "geoms", [clipped]):
            if seg.geom_type != "LineString" or seg.length < _RING_MIN_ARC_M:
                continue
            n = max(2, int(seg.length / 8))
            pts = [seg.interpolate(k * seg.length / n) for k in range(n + 1)]
            run = []
            for k, p in enumerate(pts):
                sight = LineString([nearest_points(p, big_union)[1], p])
                ok = see_zone.contains(sight) \
                    and spine_union.distance(p) > _RING_LANE_CLEAR_M
                if ok:
                    run.append(p)
                if (not ok or k == n) and len(run) >= 2:
                    piece = LineString([(q.x, q.y) for q in run])
                    run = []
                    if piece.length < _RING_MIN_ARC_M:
                        continue
                    # weld BOTH ends to the network or drop the piece
                    welded = []
                    ok_piece = True
                    for tip in (piece.coords[0], piece.coords[-1]):
                        hit = _weld_point(Point(tip), _RING_WELD_REACH_M)
                        if hit is None:
                            ok_piece = False
                            break
                        _d, ei, s_pos = hit
                        welded.append(g.split_edge(ei, s_pos))
                    if not ok_piece:
                        continue
                    cs = ([tuple(g.nodes[welded[0]])]
                          + list(piece.coords)
                          + [tuple(g.nodes[welded[1]])])
                    g.add_edge(np.asarray(cs), "building_ring")
                    lanes = [(k, LineString(e["cs"]))
                             for k, e in enumerate(g.edges)
                             if e["alive"] and e["kind"] == "lane"
                             and LineString(e["cs"]).length > 20.0]


# ── main ─────────────────────────────────────────────────────────────────────

def synthesize_spine(
    pav, runway_union=None, buildings=None, routes=None, *,
    terminal_setback: float = TERMINAL_SETBACK_M,
) -> list[SpineWay]:
    """Welded pavement-based spine (see module docstring).  ``routes`` is an
    attribute source for ICAO size letters ONLY — never geometry."""
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

    # 1: medial skeleton → graph (welded by construction)
    g = _Graph()
    for ch in build_pavement_skeleton(pav_nav, runway_union=runway_union):
        w = float(np.median(ch.radii)) if ch.radii else 8.0
        g.add_edge(np.asarray(ch.line.coords), "lane", "", w)

    # 2: through paths + straight chords (graph-preserving)
    paths = _assemble_through_paths(g)
    _straighten_paths(g, paths, chord_ok)

    # 3: sizes (routes = attribute lookup only) → standard radii
    _attribute_sizes(g, routes)

    # 3b: interior lane bends become standard arcs (no corners mid-lane)
    _fair_edge_bends(g, pav_ok)

    # 4: standard arcs at every junction (welded; H = rung + 4 equal arcs)
    _add_junction_arcs(g, pav_ok)

    # 5: runway diagonal sharp-turn arcs
    _add_runway_turns(g, runway_union, pav_eff)

    # 6: buildings (welded stubs + rings)
    _add_building_ways(g, buildings, pav_eff, terminal_setback)

    # 7: merge split fragments back into clean long ways
    g.consolidate()

    return g.ways()
