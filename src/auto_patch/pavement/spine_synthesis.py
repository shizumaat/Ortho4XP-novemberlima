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

from ..geom_safe import min_rotated_rect
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

_OPEN_HALFWIDTH_M = 38.0  # lanes with more clearance than this are open-
#                            pavement medial, not taxiway spine — dropped
_SVC_HALFWIDTH_M = 5.5    # ...and lanes NARROWER than this are service roads,
#                            not aircraft taxiways (target keeps none)

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


def _fillet(P, u_in, u_out, r, gamma_max=_TURN_MAX_DEG,
            gamma_min=_TURN_MIN_DEG):
    """Tangent arc at corner ``P``: arrive along ``u_in``, depart along
    ``u_out``.  Returns (arc_coords, tangent_len) or (None, 0)."""
    gamma = math.acos(max(-1.0, min(1.0,
        u_in[0] * u_out[0] + u_in[1] * u_out[1])))
    if gamma < math.radians(gamma_min) \
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


def _runway_axes(runway_union):
    """Unit direction of each runway (long side of its minimum rotated
    rectangle), deduplicated.  The taxiway grid is parallel/perpendicular to
    these (user 2026-07-01)."""
    axes = []
    if runway_union is None or runway_union.is_empty:
        return axes
    for poly in _polygons(runway_union):
        try:
            mrr = min_rotated_rect(poly)
            cs = np.asarray(mrr.exterior.coords)
        except Exception:
            continue
        best = None
        for k in range(min(4, len(cs) - 1)):
            v = cs[k + 1] - cs[k]
            L = float(np.hypot(*v))
            if best is None or L > best[0]:
                best = (L, _unit(float(v[0]), float(v[1])))
        if best is None:
            continue
        u = best[1]
        if all(min(_angle_deg(u, a), 180 - _angle_deg(u, a)) > 5.0
               for a in axes):
            axes.append(u)
    return axes


def _snap_direction(d, axes, tol_deg: float = 15.0):
    """The runway-grid direction (axis or its perpendicular) nearest to
    ``d``, or None if ``d`` is genuinely diagonal."""
    best = None
    for a in axes:
        for cand in (a, (-a[1], a[0])):
            ang = min(_angle_deg(d, cand), 180.0 - _angle_deg(d, cand))
            if ang <= tol_deg and (best is None or ang < best[0]):
                # orient along d
                s = 1.0 if d[0] * cand[0] + d[1] * cand[1] >= 0 else -1.0
                best = (ang, (cand[0] * s, cand[1] * s))
    return best[1] if best else None


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
        # nodes where through-path pairing is forbidden (regime changes)
        self.no_through: set = set()

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
        if s <= 1.0:
            return e["a"]                 # reuse the end node — no sliver
        if s >= ln.length - 1.0:
            return e["b"]
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

def _split_curvature_regimes(g: _Graph):
    """Insert nodes where a lane changes CHARACTER — a chain that carries a
    straight corridor plus a curved end hook must not straighten as one
    piece (the full-extent snap fails containment and the whole chain falls
    back to corner-cutting chords).  Split at bends >20° between successive
    ~straight runs so each regime is its own edge/path."""
    for ei in range(len(g.edges)):
        e = g.edges[ei]
        if not e["alive"] or e["kind"] != "lane":
            continue
        ln = LineString(e["cs"])
        if ln.length < 120.0:
            continue
        simp = np.asarray(ln.simplify(2.5).coords)
        if len(simp) < 3:
            continue
        # arc positions of split candidates (interior DP vertices with a
        # real bend and substantial runs on both sides)
        splits = []
        acc = 0.0
        for k in range(1, len(simp) - 1):
            acc += float(np.hypot(*(simp[k] - simp[k - 1])))
            u = _unit(*(simp[k] - simp[k - 1]))
            v = _unit(*(simp[k + 1] - simp[k]))
            if _angle_deg(u, v) > 20.0:
                run_prev = float(np.hypot(*(simp[k] - simp[k - 1])))
                run_next = float(np.hypot(*(simp[k + 1] - simp[k])))
                if run_prev >= 40.0 or run_next >= 40.0:
                    splits.append(acc)
        cur_ei, consumed = ei, 0.0
        for s in splits:
            e_cur = g.edges[cur_ei]
            if not e_cur["alive"]:
                break
            L = LineString(e_cur["cs"]).length
            s_local = s - consumed
            if not (8.0 <= s_local <= L - 8.0):
                continue
            mid_node = g.split_edge(cur_ei, s_local)
            g.no_through.add(mid_node)
            # the second half is the last edge appended
            cur_ei = len(g.edges) - 1
            consumed = s
    return


def _assemble_through_paths(g: _Graph):
    """Pair edge-ends at every node by BEST continuation angle (body axis,
    45 m back); paths of lane edges linked by pairs are the taxiway lanes."""
    inc = g.incident()
    partner: dict = {}                     # (ei, at_a) -> (ej, at_a_j)
    for ni, ends in inc.items():
        if ni in g.no_through:
            continue          # a curvature-regime boundary, never a through
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


def _collect_path(g: _Graph, path):
    seq, radii, node_seq = [], [], []
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
    return np.asarray(seq), np.asarray(radii, dtype=float), node_seq


def _narrow_sel(rr):
    """Vertices at the corridor's MODAL clearance — its uniform-width body.
    (The minimum-clearance vertices are junction pinches that sit off the
    corridor axis; the target lanes ride the uniform sections' center,
    measured ~20 m from each edge on SPJC's big taxiways.)"""
    if len(rr) >= 5:
        hist, edges = np.histogram(rr, bins=max(4, int((rr.max() - rr.min())
                                                       / 1.5) + 1))
        w_mode = 0.5 * (edges[np.argmax(hist)] + edges[np.argmax(hist) + 1])
        sel = np.abs(rr - w_mode) <= max(1.8, 0.15 * w_mode)
        if sel.sum() >= 3:
            return sel
    return np.ones(len(rr), dtype=bool)


def _free_fit(coords, rr):
    """Best-fit straight direction for a diagonal lane, weighted toward the
    NARROW cross-sections (the lane keeps half-width from its pavement
    edges; widenings must not swing it).  None if the path is not straight
    enough to be a line."""
    sel = _narrow_sel(rr)
    pts = coords[sel]
    if len(pts) < 3:
        return None
    mu = pts.mean(axis=0)
    X = pts - mu
    cov = X.T @ X
    evals, evecs = np.linalg.eigh(cov)
    d = evecs[:, -1]
    d = _unit(float(d[0]), float(d[1]))
    if d == (0.0, 0.0):
        return None
    nvec = np.asarray((-d[1], d[0]))
    dev = np.abs((coords - mu) @ nvec)
    w_med = float(np.median(rr))
    if float(dev.max()) > max(3.0, 0.9 * w_med):
        return None                        # genuinely curved — keep chords
    return np.asarray(d)


def _straighten_paths(g: _Graph, paths, chord_ok, axes, pav_eff):
    """Straighten through paths onto DESIGN LINES.

    Grid lanes (within 15° of a runway axis or its perpendicular) snap to
    EXACTLY that direction; diagonals snap to their own best-fit line — in
    both cases the offset comes from the NARROW cross-sections, i.e. the
    lane keeps half-width from its pavement edges (user 2026-07-01).
    Collinear snapped paths that SHARE a node are then unified onto ONE
    line (a full-length parallel must never fragment into offset pieces),
    junction nodes land on the line intersections, and dead-end tips extend
    to the pavement boundary.  Curved paths keep the chord treatment."""
    node_lines: dict = defaultdict(list)   # node -> [(n_vec, c)]
    snapped_edges: list = []
    bnd = pav_eff.boundary

    infos = []
    for path in paths:
        coords, rr, node_seq = _collect_path(g, path)
        if len(coords) < 2:
            continue
        d_chord = _unit(*(coords[-1] - coords[0]))
        path_len = float(LineString(coords).length)
        d = None
        if path_len > 12.0:
            snap = _snap_direction(d_chord, axes)
            if snap is not None:
                d = np.asarray(snap)
            elif path_len > 60.0:
                d = _free_fit(coords, rr)
        infos.append(dict(path=path, coords=coords, rr=rr,
                          node_seq=node_seq, d=d, c=None))

    # per-path offset from the narrow cross-sections
    for info in infos:
        if info["d"] is None:
            continue
        nvec = np.asarray((-info["d"][1], info["d"][0]))
        sel = _narrow_sel(info["rr"])
        info["c"] = float(np.median((info["coords"] @ nvec)[sel]))

    # ── unify collinear snapped paths that share a node ─────────────────────
    snapped = [i for i, info in enumerate(infos) if info["d"] is not None]
    parent = {i: i for i in snapped}

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    node_of = defaultdict(set)
    for i in snapped:
        for ni in infos[i]["node_seq"]:
            node_of[ni].add(i)
    for members in node_of.values():
        mem = sorted(members)
        for a in range(len(mem)):
            for b in range(a + 1, len(mem)):
                ia, ib = mem[a], mem[b]
                da, db = infos[ia]["d"], infos[ib]["d"]
                ang = _angle_deg(tuple(da), tuple(db))
                ang = min(ang, 180.0 - ang)
                if ang > 3.0:
                    continue
                na = np.asarray((-da[1], da[0]))
                sel_b = _narrow_sel(infos[ib]["rr"])
                cb_in_a = float(np.median(
                    (infos[ib]["coords"] @ na)[sel_b]))
                if abs(infos[ia]["c"] - cb_in_a) <= 6.0:
                    ra, rb = _find(ia), _find(ib)
                    if ra != rb:
                        parent[ra] = rb
    clusters = defaultdict(list)
    for i in snapped:
        clusters[_find(i)].append(i)
    for members in clusters.values():
        if len(members) < 2:
            continue
        # one line for the whole taxiway: direction of the longest member,
        # offset over ALL members' narrow vertices
        longest = max(members,
                      key=lambda i: LineString(infos[i]["coords"]).length)
        d = infos[longest]["d"]
        nvec = np.asarray((-d[1], d[0]))
        cs_all = []
        for i in members:
            sel = _narrow_sel(infos[i]["rr"])
            cs_all.extend((infos[i]["coords"] @ nvec)[sel].tolist())
        c = float(np.median(cs_all))
        for i in members:
            infos[i]["d"] = d
            infos[i]["c"] = c

    # ── apply geometry ───────────────────────────────────────────────────────
    allow = shapely.buffer(pav_eff, 0.3)
    for info in infos:
        coords, rr, node_seq = info["coords"], info["rr"], info["node_seq"]
        applied = False
        if info["d"] is not None:
            d = np.asarray(info["d"])
            nvec = np.asarray((-d[1], d[0]))
            c_all = coords @ nvec
            arc_pos = np.concatenate([[0.0], np.cumsum(
                np.hypot(*(coords[1:] - coords[:-1]).T))])
            # A parallel can JOG mid-path: the lateral offset steps between
            # two plateaus (two design lines joined by a short S-turn).
            best_gap, best_k = 0.0, None
            if arc_pos[-1] > 400.0 and len(info["path"]) >= 2:
                for k in range(3, len(coords) - 3):
                    if arc_pos[k] < 150.0 or arc_pos[-1] - arc_pos[k] < 150.0:
                        continue
                    gap = abs(np.median(c_all[:k]) - np.median(c_all[k:]))
                    if gap > best_gap:
                        best_gap, best_k = gap, k

            def _try_single():
                sel = _narrow_sel(rr)
                c = float(np.median(c_all[sel]))
                t_all = coords @ d
                probe = LineString([tuple(d * t_all.min() + nvec * c),
                                    tuple(d * t_all.max() + nvec * c)])
                if not (chord_ok(probe) or allow.contains(probe)):
                    return False
                for ni in node_seq:
                    node_lines[ni].append((nvec, c))
                for ni in node_seq:
                    t = float(g.nodes[ni] @ d)
                    g.move_node(ni, d * t + nvec * c)
                for (ei, _at_a) in info["path"]:
                    e = g.edges[ei]
                    e["cs"] = np.asarray([g.nodes[e["a"]], g.nodes[e["b"]]])
                    snapped_edges.append(ei)
                return True

            def _try_jog():
                # cut at the interior node nearest the step
                node_arc = [float(arc_pos[int(np.argmin(
                    np.hypot(*(coords - g.nodes[ni]).T)))])
                    for ni in node_seq]
                s_step = arc_pos[best_k]
                interior = list(range(1, len(node_seq) - 1))
                if not interior:
                    return False
                cut = min(interior, key=lambda kk: abs(node_arc[kk] - s_step))
                g1 = list(range(0, cut))
                g2 = list(range(cut, len(info["path"])))
                if not g1 or not g2:
                    return False
                m1 = arc_pos <= node_arc[cut] + 1.0
                m2 = arc_pos >= node_arc[cut] - 1.0
                cs_ = []
                for m in (m1, m2):
                    if m.sum() < 3:
                        return False
                    sel = _narrow_sel(rr[m])
                    cs_.append(float(np.median((c_all[m])[sel])))
                c1, c2 = cs_
                if abs(c1 - c2) < 3.0:
                    return False
                for m, c in ((m1, c1), (m2, c2)):
                    t_sub = (coords[m]) @ d
                    probe = LineString([tuple(d * t_sub.min() + nvec * c),
                                        tuple(d * t_sub.max() + nvec * c)])
                    if not (chord_ok(probe) or allow.contains(probe)):
                        return False
                ni_cut = node_seq[cut]
                t_cut = float(g.nodes[ni_cut] @ d)
                p1 = d * t_cut + nvec * c1
                p2 = d * t_cut + nvec * c2
                # group 1 keeps ni_cut on line 1
                for ni in node_seq[:cut + 1]:
                    t = float(g.nodes[ni] @ d)
                    cc = c1
                    g.move_node(ni, d * t + nvec * cc)
                    node_lines[ni].append((nvec, c1))
                # group 2 gets a NEW node on line 2 + jog edge
                ni_new = g.add_node(p2)
                g.no_through.add(ni_cut)
                g.no_through.add(ni_new)
                first2 = info["path"][cut]
                e2 = g.edges[first2[0]]
                if first2[1]:
                    e2["a"] = ni_new
                    e2["cs"][0] = g.nodes[ni_new]
                else:
                    e2["b"] = ni_new
                    e2["cs"][-1] = g.nodes[ni_new]
                for ni in node_seq[cut + 1:]:
                    t = float(g.nodes[ni] @ d)
                    g.move_node(ni, d * t + nvec * c2)
                    node_lines[ni].append((nvec, c2))
                node_lines[ni_new].append((nvec, c2))
                g.add_edge(np.asarray([p1, p2]), "lane", "", 10.0)
                for gi in g1:
                    (ei, _a) = info["path"][gi]
                    e = g.edges[ei]
                    e["cs"] = np.asarray([g.nodes[e["a"]], g.nodes[e["b"]]])
                    snapped_edges.append(ei)
                for gi in g2:
                    (ei, _a) = info["path"][gi]
                    e = g.edges[ei]
                    e["cs"] = np.asarray([g.nodes[e["a"]], g.nodes[e["b"]]])
                    snapped_edges.append(ei)
                return True

            if best_gap > 4.0 and best_k is not None:
                applied = _try_jog() or _try_single()
            else:
                applied = _try_single()
        if applied:
            continue
        # fallback: chord-straighten (curved paths)
        straight, _r = _chord_straighten(coords, rr, chord_ok)
        path_ln = LineString(straight)
        s_of_node = [0.0]
        for ni in node_seq[1:-1]:
            s_of_node.append(path_ln.project(Point(tuple(g.nodes[ni]))))
        s_of_node.append(path_ln.length)
        for k in range(1, len(s_of_node)):
            s_of_node[k] = max(s_of_node[k], s_of_node[k - 1] + 0.5)
        for k, ni in enumerate(node_seq[1:-1], start=1):
            p = path_ln.interpolate(min(s_of_node[k], path_ln.length))
            g.move_node(ni, [p.x, p.y])
        for k, (ei, at_a) in enumerate(info["path"]):
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

    # ── node reconciliation: shared nodes land on line intersections ────────
    inc = g.incident()
    for ni, lines in node_lines.items():
        if len(lines) >= 2:
            best = None
            for x in range(len(lines)):
                for y in range(x + 1, len(lines)):
                    n1, c1 = lines[x]
                    n2, c2 = lines[y]
                    det = abs(float(n1[0] * n2[1] - n1[1] * n2[0]))
                    if best is None or det > best[0]:
                        best = (det, (n1, c1), (n2, c2))
            det, (n1, c1), (n2, c2) = best
            if det > 0.5:
                D = n1[0] * n2[1] - n1[1] * n2[0]
                x = (c1 * n2[1] - c2 * n1[1]) / D
                y = (n1[0] * c2 - n2[0] * c1) / D
                if math.hypot(x - g.nodes[ni][0], y - g.nodes[ni][1]) < 40.0:
                    g.move_node(ni, [x, y])
        if len(inc.get(ni, [])) == 1 and len(lines) >= 1:
            (ei, at_a) = inc[ni][0]
            e = g.edges[ei]
            if e["alive"]:
                other = e["b"] if at_a else e["a"]
                u = _unit(*(g.nodes[ni] - g.nodes[other]))
                if u != (0.0, 0.0):
                    ray = LineString([tuple(g.nodes[ni] - np.asarray(u) * 3.0),
                                      tuple(g.nodes[ni] + np.asarray(u) * 60.0)])
                    hit = ray.intersection(bnd)
                    pts = [q for q in getattr(hit, "geoms", [hit])
                           if q.geom_type == "Point"]
                    if pts:
                        q = min(pts, key=lambda q: q.distance(
                            Point(tuple(g.nodes[ni]))))
                        if q.distance(Point(tuple(g.nodes[ni]))) < 45.0:
                            g.move_node(ni, [q.x - u[0] * 0.1,
                                             q.y - u[1] * 0.1])
    for ei in snapped_edges:
        e = g.edges[ei]
        if e["alive"]:
            e["cs"] = np.asarray([g.nodes[e["a"]], g.nodes[e["b"]]])


def _collapse_straight_edges(g: _Graph, pav_ok, tol: float = 2.5):
    """Node reconciliation moves edge ENDPOINTS; a multi-vertex edge then
    carries a kink at its second vertex.  Any lane edge whose interior stays
    within ``tol`` of the endpoint chord (and whose chord fits the pavement)
    collapses to the clean 2-point straight."""
    for e in g.edges:
        if not e["alive"] or e["kind"] != "lane":
            continue
        cs = e["cs"]
        if len(cs) <= 2:
            continue
        a, b = cs[0], cs[-1]
        ab = b - a
        L = float(np.hypot(*ab))
        if L < 1e-6:
            continue
        rel = cs - a
        dev = np.abs(rel[:, 0] * ab[1] - rel[:, 1] * ab[0]) / L
        if float(dev.max()) <= tol \
                and pav_ok(LineString([tuple(a), tuple(b)])):
            e["cs"] = np.asarray([a, b])


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


def _prune_components(g: _Graph, runway_union, pav_eff):
    """Keep only lane components that reach a RUNWAY edge (every taxiway
    system serves the runways; apron-bay networks that only connected via
    open pavement are exactly what the hand-edited target leaves empty).
    Very large components survive regardless (isolated pavement islands
    with their own internal taxiways)."""
    if runway_union is None or runway_union.is_empty:
        return
    edge_b = runway_union.boundary
    adj = defaultdict(set)
    comp_edges = defaultdict(list)
    for ei, e in enumerate(g.edges):
        if e["alive"] and e["kind"] == "lane":
            adj[e["a"]].add(e["b"])
            adj[e["b"]].add(e["a"])
    seen: set = set()
    comps: list = []
    for start in list(adj.keys()):
        if start in seen:
            continue
        comp = [start]
        seen.add(start)
        stack = [start]
        while stack:
            cur = stack.pop()
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    comp.append(nxt)
                    stack.append(nxt)
        comp_set = set(comp)
        touches = any(edge_b.distance(Point(tuple(g.nodes[ni]))) < 2.5
                      for ni in comp)
        length = sum(LineString(e["cs"]).length for e in g.edges
                     if e["alive"] and e["kind"] == "lane"
                     and e["a"] in comp_set)
        comps.append((comp_set, touches, length))
    # keep: touches a runway, or big, or the biggest network on its own
    # pavement piece (isolated islands still get their spine)
    pieces = _polygons(pav_eff)
    best_on_piece: dict = {}
    for idx, (comp_set, touches, length) in enumerate(comps):
        anyn = g.nodes[next(iter(comp_set))]
        pi = next((k for k, pc in enumerate(pieces)
                   if pc.distance(Point(tuple(anyn))) < 2.0), -1)
        if pi >= 0 and length > best_on_piece.get(pi, (0.0, -1))[0]:
            best_on_piece[pi] = (length, idx)
    keep_idx = {v[1] for v in best_on_piece.values()}
    for idx, (comp_set, touches, length) in enumerate(comps):
        if touches or length >= 400.0 or idx in keep_idx:
            continue
        for e in g.edges:
            if e["alive"] and e["kind"] == "lane" and e["a"] in comp_set:
                e["alive"] = False


def _fix_dangles(g: _Graph, pav_eff):
    """The coverage policy severs lanes where they met open-pavement medial.
    A dangling lane tip either EXTENDS along its tangent to the pavement
    boundary (user: an opening into a small apron gets the spine drawn
    across to the edge) or, if the boundary is unreachable, the short
    dangling fragment is trimmed back to its junction."""
    bnd = pav_eff.boundary
    changed = True
    while changed:
        changed = False
        for ni, ends in list(g.incident().items()):
            live = [(ei, aa) for ei, aa in ends if g.edges[ei]["alive"]]
            if len(live) != 1:
                continue
            (ei, at_a) = live[0]
            e = g.edges[ei]
            tip = g.nodes[ni]
            if bnd.distance(Point(tuple(tip))) <= 2.0:
                continue                   # already a legitimate edge end
            other = e["b"] if at_a else e["a"]
            u = _unit(*(tip - g.nodes[other]))
            done = False
            if u != (0.0, 0.0):
                # "an opening into a small apron: draw the spine ACROSS to
                # the edge" — reach far enough to span the whole bay
                ray = LineString([tuple(tip),
                                  (tip[0] + u[0] * 220.0,
                                   tip[1] + u[1] * 220.0)])
                hit = ray.intersection(bnd)
                pts = [q for q in getattr(hit, "geoms", [hit])
                       if q.geom_type == "Point"]
                if pts:
                    q = min(pts, key=lambda q: q.distance(Point(tuple(tip))))
                    probe = LineString([tuple(tip), (q.x, q.y)])
                    if shapely.buffer(pav_eff, 0.5).contains(probe):
                        g.move_node(ni, [q.x - u[0] * 0.1,
                                         q.y - u[1] * 0.1])
                        done = True
            if not done and LineString(e["cs"]).length < 40.0:
                e["alive"] = False
                changed = True


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


def _add_junction_arcs(g: _Graph, pav_ok, runway_union=None,
                       r_start_for=None, gamma_max=None):
    """At every node, STANDARD arcs for every branch pair with a real turn —
    all arcs of one junction share ONE radius (user: where arcs come
    together they are the same size, mirrored), so symmetric pairs land on
    shared tangent nodes.  Tangent points split the branch edges (endpoint
    reuse welds coincident tangents), keeping the graph coherent.  An H
    junction is a straight rung + four EQUAL quarter-circles.

    ``r_start_for(P, r_std)`` (optional) sets the LARGEST radius to try at
    a node — wide-open junction crossings take the biggest mirrored arcs
    that fit (v8 target evidence), corridor junctions stay standard.
    ``gamma_max`` (optional) caps the pair turn angle below the module
    default — the route model uses ~120°: at standard radius a sharper
    pair sweeps a long arc across the junction interior (v13 user
    review, the way-276 bulge), and those movements are runway-turn
    territory, not ordinary junction fillets."""
    rwy_b = runway_union.boundary \
        if runway_union is not None and not runway_union.is_empty else None
    node_ids = list(g.incident().keys())
    for ni in node_ids:
        P = g.nodes[ni]
        if rwy_b is not None and rwy_b.distance(Point(tuple(P))) < 3.0:
            continue        # square runway contact: straight line, no arcs

        def _ends():
            return [(ei, at_a) for (ei, at_a) in g.incident().get(ni, [])
                    if g.edges[ei]["alive"]
                    and g.edges[ei]["kind"] == "lane"]

        ends = _ends()
        if len(ends) < 2:
            continue
        # ── dry-run: per-pair max fitting radius, then the junction's
        #    COMMON radius = the smallest of them (mirrored equal arcs)
        pair_list = []
        r_common = None
        for x in range(len(ends)):
            for y in range(x + 1, len(ends)):
                (ea, aa), (eb, ab) = ends[x], ends[y]
                if ea == eb:
                    continue
                u = g.edge_dir_at(ea, aa)
                v = g.edge_dir_at(eb, ab)
                u_out = (-v[0], -v[1])
                gamma = _angle_deg(u, u_out)
                if gamma < _TURN_MIN_DEG or gamma > (
                        gamma_max if gamma_max is not None
                        else _TURN_MAX_DEG):
                    continue
                size = min(g.edges[ea]["size"] or "C",
                           g.edges[eb]["size"] or "C")
                r_std = _radius_for(size)
                len_a = LineString(g.edges[ea]["cs"]).length
                len_b = LineString(g.edges[eb]["cs"]).length
                r_fit = None
                r = r_std if r_start_for is None \
                    else max(r_std, float(r_start_for(P, r_std)))
                while r >= _MIN_ARC_FIT * r_std:
                    arc, t = _fillet(tuple(P), u, u_out, r)
                    if arc is None:
                        break
                    # half-length budget: a branch may host arcs at BOTH
                    # of its ends (H rung), so each side gets half.  In
                    # walk mode (v8: planarize fragments are short) the
                    # tangent may continue across collinear fragments.
                    if r_start_for is not None:
                        fits = (_walk_locate(g, ea, aa, t) is not None
                                and _walk_locate(g, eb, ab, t) is not None)
                    else:
                        fits = t <= 0.55 * len_a and t <= 0.55 * len_b
                    if fits and pav_ok(LineString(arc)):
                        r_fit = r
                        break
                    r *= 0.85
                if r_fit is None:
                    continue
                pair_list.append((u, u_out, size))
                r_common = r_fit if r_common is None else min(r_common, r_fit)
        if not pair_list or r_common is None:
            continue
        # ── place every pair at the common radius
        for (u, u_out, size) in pair_list:
            ends_now = _ends()
            # re-resolve the two branches by direction match
            best_a = best_b = None
            for (ei, at_a) in ends_now:
                d = g.edge_dir_at(ei, at_a)
                if _angle_deg(d, u) < 10.0 and best_a is None:
                    best_a = (ei, at_a)
                elif _angle_deg(d, (-u_out[0], -u_out[1])) < 10.0 \
                        and best_b is None:
                    best_b = (ei, at_a)
            if best_a is None or best_b is None or best_a[0] == best_b[0]:
                continue
            arc, t = _fillet(tuple(P), u, u_out, r_common)
            if arc is None:
                continue
            ln_arc = LineString(arc)
            if not pav_ok(ln_arc):
                continue
            (ea, aa), (eb, ab) = best_a, best_b
            len_a = LineString(g.edges[ea]["cs"]).length
            len_b = LineString(g.edges[eb]["cs"]).length
            w_pair = g.edges[ea]["w"]
            if r_start_for is not None:
                loc_a = _walk_locate(g, ea, aa, t)
                loc_b = _walk_locate(g, eb, ab, t)
                if loc_a is None or loc_b is None:
                    continue
                na = g.split_edge(loc_a[0], loc_a[1])
                nb = g.split_edge(loc_b[0], loc_b[1])
            else:
                if t > len_a + 1.0 or t > len_b + 1.0:
                    continue
                na = g.split_edge(ea, t if aa else len_a - t)
                nb = g.split_edge(eb, t if ab else len_b - t)
            cs = np.asarray(arc)
            cs[0] = g.nodes[na]
            cs[-1] = g.nodes[nb]
            g.add_edge(cs, "arc", size, w_pair)
    return


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
    """Runway-contact arcs at DIAGONAL (rapid-exit style) lane tips.

    Sharp side: one long STANDARD-radius arc (the ~135° turn), tangent to
    the diagonal, landing on the runway edge.  Shallow side: the rare
    gentle LARGE-radius blend into the runway (550 m down to 120 m, first
    that fits).  Both weld into the graph; tangent points may lie beyond
    the tip fragment, so they walk across collinear continuations.  The
    live tip edge is re-resolved before every placement because a previous
    split may have retired it."""
    if runway_union is None or runway_union.is_empty:
        return
    allow_rwy = shapely.buffer(pav_eff, 1.0)
    edge_b = runway_union.boundary
    n_edges0 = len(g.edges)
    for ei in range(n_edges0):
        e = g.edges[ei]
        if not e["alive"] or e["kind"] != "lane":
            continue
        for at_a in (True, False):
            tip = e["cs"][0] if at_a else e["cs"][-1]
            P = Point(tuple(tip))
            if edge_b.distance(P) > 1.5:
                continue
            tip_node = e["a"] if at_a else e["b"]
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
            # a lane that CROSSES the runway (continuation on the opposite
            # side) is a crossing, not a rapid exit — no diagonal arcs
            probe = LineString([tuple(tip),
                                (tip[0] + u_in[0] * 400.0,
                                 tip[1] + u_in[1] * 400.0)])
            exit_pt = None
            inter = probe.intersection(edge_b)
            cand = [q for q in getattr(inter, "geoms", [inter])
                    if q.geom_type == "Point"
                    and q.distance(P) > 5.0]
            if cand:
                exit_pt = min(cand, key=lambda q: q.distance(P))
            if exit_pt is not None:
                near_far = any(
                    o["alive"] and o["kind"] == "lane"
                    and LineString(o["cs"]).distance(exit_pt) < 35.0
                    for o in g.edges)
                if near_far:
                    continue
            r_std = _radius_for(e["size"])

            def _live_tip_edge():
                for (ej, aj) in g.incident().get(tip_node, []):
                    if g.edges[ej]["alive"] \
                            and g.edges[ej]["kind"] == "lane":
                        return ej, aj
                return None, None

            for e_sgn in (1.0, -1.0):
                u_out = (e_dir[0] * e_sgn, e_dir[1] * e_sgn)
                cei, cat_a = _live_tip_edge()
                if cei is None:
                    break
                if _angle_deg(u_in, u_out) < 95.0:
                    # SHALLOW side: the gentle large-radius blend mostly
                    # lives ON the runway (the paint eases from the runway
                    # centerline onto the diagonal), so at our domain edge
                    # the arc is CLIPPED at the runway boundary: keep the
                    # taxi-side piece — a gentle ease-off from the diagonal
                    # ending on the edge.
                    for r_blend in (550.0, 400.0, 275.0, 180.0, 120.0):
                        arc_b, t_b = _fillet(tuple(tip), u_in, u_out,
                                             r_blend, gamma_max=70.0,
                                             gamma_min=10.0)
                        if arc_b is None:
                            break
                        loc_b = _walk_locate(g, cei, cat_a, t_b)
                        if loc_b is None:
                            continue
                        # smooth prefix of the arc while it stays on
                        # pavement (clip artifacts would kink the line)
                        keep = [arc_b[0]]
                        for q in arc_b[1:]:
                            if not allow_rwy.contains(Point(q)):
                                break
                            keep.append(q)
                        if len(keep) < 3:
                            continue
                        piece = LineString(keep)
                        if piece.length < 25.0:
                            continue
                        nb2 = g.split_edge(loc_b[0], loc_b[1])
                        cs_b = np.asarray(piece.coords)
                        cs_b[0] = g.nodes[nb2]
                        g.add_edge(cs_b, "blend", e["size"], e["w"])
                        break
                    continue
                # SHARP side: long standard-radius turn arc.  A hook at a
                # fraction of the standard radius is paint that does not
                # exist — skip rather than shrink below half standard.
                r, arc, t, loc = r_std, None, 0.0, None
                while r >= 0.5 * r_std:
                    arc, t = _fillet(tuple(tip), u_in, u_out, r,
                                     gamma_max=162.0)
                    if arc is not None:
                        loc = _walk_locate(g, cei, cat_a, t)
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

    # 1: medial skeleton → graph (welded by construction).  Runway SHOULDER
    # strips (pav − runway leaves thin flankers along the edge) get no lane:
    # the runway profile owns them, and their medial otherwise junction-arcs
    # into every square taxiway contact (user: a 90° contact is a bare
    # straight line stopping at the edge).
    rwy_zone = shapely.buffer(runway_union, 25.0) \
        if runway_union is not None and not runway_union.is_empty else None
    g = _Graph()
    chains_all = []
    for ch in build_pavement_skeleton(pav_nav, runway_union=runway_union):
        w = float(np.median(ch.radii)) if ch.radii else 8.0
        chains_all.append((ch, w))
    kept_tips = []
    dropped_bay = []                       # bay-mouth chains → spur candidates
    for ch, w in chains_all:
        # COVERAGE POLICY (user 2026-07-01, from the hand-edited target):
        # open pavement stays EMPTY — the spine covers the taxiway system,
        # rides holes at half-width for its turns, and crosses small apron
        # openings to their edge.  The medial clearance radius IS the
        # openness measure: corridor lanes, hole-riding turns and small-bay
        # spurs all have small clearance; big-apron medial does not.
        if w < _SVC_HALFWIDTH_M:
            continue
        if w > _OPEN_HALFWIDTH_M:
            dropped_bay.append(ch)
            continue
        if rwy_zone is not None and ch.line.length > 1.0:
            n = max(2, int(ch.line.length / 10))
            inside = sum(1 for k in range(n + 1)
                         if rwy_zone.contains(
                             ch.line.interpolate(k * ch.line.length / n)))
            if inside / (n + 1) >= 0.6:
                # only a PARALLEL rider is a shoulder lane — perpendicular
                # and diagonal contacts legitimately live near the edge
                cs0 = np.asarray(ch.line.coords)
                d_ch = _unit(*(cs0[-1] - cs0[0]))
                is_par = False
                for a in _runway_axes(runway_union):
                    ang = _angle_deg(d_ch, a)
                    if min(ang, 180.0 - ang) <= 15.0:
                        is_par = True
                        break
                if is_par and w < 9.0:
                    continue
        g.add_edge(np.asarray(ch.line.coords), "lane", "", w)
        cs0 = np.asarray(ch.line.coords)
        kept_tips.append(cs0[0])
        kept_tips.append(cs0[-1])

    # 1c: BAY SPURS — an opening into a small apron gets the spine drawn
    # straight ACROSS to its far edge (user).  A dropped wide-mouth chain
    # that attached to the kept network marks such an opening: cast a
    # straight ray from the attachment point along the chain's initial
    # direction to the pavement boundary.
    if kept_tips:
        kept_union = unary_union(
            [LineString(e["cs"]) for e in g.edges
             if e["alive"] and e["kind"] == "lane"])
        bnd0 = pav_eff.boundary
        allow0 = shapely.buffer(pav_eff, 0.5)
        # STRAIGHT BRIDGES across open pavement (user: the spine is drawn
        # ACROSS; their hand-drawn crossings are straight lines).  PORTS =
        # points where dropped open-area chains attach to the kept network;
        # any port pair with a clear straight line over pavement gets a
        # bridge, greedily, skipping redundant ones.
        ports = []
        for ch in dropped_bay:
            cs0 = np.asarray(ch.line.coords)
            for tip in (cs0[0], cs0[-1]):
                if kept_union.distance(Point(tuple(tip))) <= 1.5:
                    if all(np.hypot(*(np.asarray(q) - tip)) > 8.0
                           for q in ports):
                        ports.append(tuple(tip))
        strict0 = shapely.buffer(pav_eff, -2.0)
        bridges = []
        cand = []
        for a in range(len(ports)):
            for b in range(a + 1, len(ports)):
                pa, pb = np.asarray(ports[a]), np.asarray(ports[b])
                d = float(np.hypot(*(pb - pa)))
                if 90.0 <= d <= 650.0:
                    cand.append((d, ports[a], ports[b]))
        for d, pa, pb in sorted(cand, key=lambda t: t[0]):
            ln0 = LineString([pa, pb])
            if not strict0.contains(ln0):
                continue
            mid = ln0.interpolate(0.5, normalized=True)
            if any(b0.distance(mid) < 60.0 for b0 in bridges):
                continue
            if kept_union.distance(mid) < 30.0:
                continue                   # runs beside an existing lane
            bridges.append(ln0)
            g.add_edge(np.asarray(ln0.coords), "lane", "", 15.0)

        # HOLE-OFFSET RINGS: a large hole in OPEN pavement guides the turn
        # around it at even distance from its edge (the medial sits far too
        # deep in open pavement to serve).  Ring = hole buffered by the
        # airport's typical lane half-width, clipped to pavement.
        w_lane = float(np.median([wk for (_c, wk) in chains_all
                                  if _SVC_HALFWIDTH_M <= wk
                                  <= _OPEN_HALFWIDTH_M]) or 12.0)
        for poly in _polygons(pav_eff):
            for hole in poly.interiors:
                hp = Polygon(hole)
                if hp.area < 1500.0:
                    continue
                ring0 = LineString(hp.buffer(w_lane, quad_segs=10)
                                   .exterior.coords)
                if kept_union.distance(ring0) < 8.0 \
                        and kept_union.distance(
                            ring0.interpolate(0.5, normalized=True)) < 60.0:
                    continue               # corridor lanes already ride it
                clip = ring0.intersection(shapely.buffer(pav_eff, -1.0))
                segs = [s0 for s0 in getattr(clip, "geoms", [clip])
                        if s0.geom_type == "LineString" and s0.length > 30.0]
                keep_len = sum(s0.length for s0 in segs)
                if keep_len < 0.5 * ring0.length:
                    continue
                for s0 in segs:
                    g.add_edge(np.asarray(s0.coords), "lane", "", w_lane)

        dropped_bay = [ch for ch in dropped_bay
                       if float(np.median(ch.radii) if ch.radii else 99)
                       <= 80.0 and ch.line.length <= 240.0]
        for ch in dropped_bay:
            cs0 = np.asarray(ch.line.coords)
            for at_start in (True, False):
                tip = cs0[0] if at_start else cs0[-1]
                if kept_union.distance(Point(tuple(tip))) > 1.5:
                    continue               # not attached to the kept network
                ref = cs0[min(len(cs0) - 1, 4)] if at_start \
                    else cs0[max(0, len(cs0) - 5)]
                u = _unit(*(np.asarray(ref) - tip)) if at_start \
                    else _unit(*(tip - np.asarray(ref)))
                u = _unit(*(np.asarray(ch.line.interpolate(
                    min(30.0, ch.line.length)).coords[0]) - tip)) \
                    if at_start else _unit(*(tip - np.asarray(
                        ch.line.interpolate(
                            max(0.0, ch.line.length - 30.0)).coords[0])))
                if u == (0.0, 0.0):
                    continue
                start = tip if at_start else tip
                ray = LineString([tuple(start),
                                  (start[0] + u[0] * 240.0,
                                   start[1] + u[1] * 240.0)])
                hit = ray.intersection(bnd0)
                pts = [q for q in getattr(hit, "geoms", [hit])
                       if q.geom_type == "Point"]
                if not pts:
                    continue
                q = min(pts, key=lambda q: q.distance(Point(tuple(start))))
                if q.distance(Point(tuple(start))) < 15.0:
                    continue
                spur = LineString([tuple(start), (q.x - u[0] * 0.1,
                                                  q.y - u[1] * 0.1)])
                if allow0.contains(spur):
                    g.add_edge(np.asarray(spur.coords), "lane", "", 12.0)
                break

    # 1b: drop networks that never reach a runway (target leaves them empty)
    _prune_components(g, runway_union, pav_eff)

    # 1d: separate straight corridors from curved hooks before snapping
    _split_curvature_regimes(g)

    # 2: through paths + runway-grid straightening (graph-preserving)
    axes = _runway_axes(runway_union)
    paths = _assemble_through_paths(g)
    _straighten_paths(g, paths, chord_ok, axes, pav_eff)

    # 2b: collapse kinks left by node reconciliation
    _collapse_straight_edges(g, pav_ok)

    # 3: sizes (routes = attribute lookup only) → standard radii
    _attribute_sizes(g, routes)

    # 3b: interior lane bends become standard arcs (no corners mid-lane)
    _fair_edge_bends(g, pav_ok)

    # 4: standard arcs at every junction (welded; H = rung + 4 equal arcs)
    _add_junction_arcs(g, pav_ok, runway_union)

    # 5: runway diagonal sharp-turn arcs
    _add_runway_turns(g, runway_union, pav_eff)

    # 6: dangling tips extend to the pavement edge or trim away
    _fix_dangles(g, pav_eff)

    # (building stubs/rings retired — the hand-edited target keeps none;
    # buildings only shape the pavement via pav_nav subtraction)

    # 7: merge split fragments back into clean long ways
    g.consolidate()

    return g.ways()
