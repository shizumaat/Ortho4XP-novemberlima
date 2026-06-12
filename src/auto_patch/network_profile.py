"""Network Profile Model (#4) — ONE elevation field solved over the FULL
taxi-centerline graph (docs/network_profile_model.md, user-approved s77p4).

Today's corridor machinery solves elevations on RECT CHAINS and uses the
centerline graph only as a measuring tape; everywhere two chains meet, a
tie layer (crossing inserts, consensus, freeze, blocker rescue, …) stitches
the independent profiles together, and every s77 failure class is the two
representations disagreeing.  This module solves elevation ONCE, per
CENTERLINE-GRAPH VERTEX, over the complete surviving apt.dat taxi network
plus discovered-rect axes:

  * intersections are SHARED VERTICES (segments are split where they
    cross) — crossing profiles agree by construction, exactly like
    crossing runways (M2);
  * runway contacts (centerline x runway-edge intersections) are HARD
    anchors at the solved runway surface values; where the network finds
    a contact infeasible against the rest of the field it emits the
    runway-flex demand DIRECTLY — the profile value the network wants at
    the contact (M3);
  * the law on edges: |de| <= cap * len per edge and a grade-rate limit
    through vertices along incident-edge pairs, curve-aware lengths
    (polyline vertices carry true arc lengths; exit-fan entries get
    caller-measured arc overrides) (M4);
  * objective: stay DEM-near — seed at DEM, Gauss-Seidel projection onto
    the constraint polytope with anchor-feasibility band clamps computed
    by Dijkstra over the SAME graph (they cannot disagree with the
    solve).  Jointly-infeasible anchors are spread MINIMAX by uniform
    per-component cap relaxation, never concentrated at a seam (M5).

Geometry then grades FROM this field (rect planes sample it, junctions
twist between crossing profiles, aprons bind through the interior-geodesic
value bands seeded at field samples, terminals leaf) and the validator
reads the SAME field — see ``elevation_per_surface/unified_jacobi.py``
(gate ``config.NETWORK_PROFILE_MODEL``) and ``route_field.py``.

Determinism: every iteration runs over sorted structures; no set/dict
iteration order leaks into values (PYTHONHASHSEED byte-identity is a
shipping gate).
"""
from __future__ import annotations

import heapq
import math
from typing import Callable, Dict, List, Optional, Sequence, Tuple

__all__ = ["NetworkProfileField", "build_and_solve"]

# Same node-snap tolerance as taxi_routing._SNAP_TOL_M / route_field — the
# graph must agree with the measuring-tape graphs about which centerline
# vertices coincide.
_SNAP_TOL_M = 3.0
# Spatial-grid cell for segment intersection search and point sampling.
_GRID_CELL_M = 80.0
# A polyline END this close to a runway boundary bridges to it as a
# contact (the apt.dat taxi rows stop at/near the runway edge — same reach
# as taxi_routing.augment_with_runway_centerlines).
_END_BRIDGE_M = 40.0
# Demand deadband: a contact must want to dip at least this much before a
# runway-flex demand is emitted (the established corridor-tie deadband).
_DEMAND_MIN_DIP_M = 0.5
# Gauss-Seidel convergence.
_SOLVE_TOL_M = 0.001
_SOLVE_MAX_SWEEPS = 600
# dg pairs across edges shorter than this are numerically unstable (the
# faa rate pass has the same guard at station-merge scale).
_DG_MIN_EDGE_M = 2.0


class NetworkProfileField:
    """The solved field.  ``nodes[i] = (x, y)``; ``elev[i]`` solved metres;
    ``hard[i]`` True at runway-contact / runway-interior anchors;
    ``adj[i] = [(j, w_m, eff_cap), ...]`` sorted; ``contacts`` =
    ``[(node, value, ref, (x, y))]``; ``demands`` = runway-flex wishes
    ``[(xy, wanted_value, ref, "dip"|"rise")]`` (M3, dip-or-rise
    symmetric — user 2026-06-11); ``comp_of[i]`` component id;
    ``relax`` = per-component minimax cap factor (1.0 = feasible);
    ``audit`` = build/solve statistics for the connectivity probe."""

    __slots__ = ("nodes", "elev", "hard", "adj", "contacts", "demands",
                 "comp_of", "relax", "audit", "_segs", "_grid", "_cell",
                 "band_lo", "band_hi", "aug", "prox")

    def __init__(self):
        self.aug: set = set()
        self.prox: set = set()      # proximity-coupling edge pairs (i<j)
        self.nodes: List[Tuple[float, float]] = []
        self.elev: List[float] = []
        self.hard: List[bool] = []
        self.adj: List[List[Tuple[int, float, float]]] = []
        self.contacts: List[Tuple[int, float, str, Tuple[float, float]]] = []
        self.demands: List[Tuple[Tuple[float, float], float, str]] = []
        self.comp_of: List[int] = []
        self.relax: Dict[int, float] = {}
        self.audit: Dict = {}
        self._segs: List[Tuple[int, int]] = []
        self._grid: Dict[Tuple[int, int], List[int]] = {}
        self._cell: float = _GRID_CELL_M
        self.band_lo: List[float] = []
        self.band_hi: List[float] = []

    # ── point sampling (the write layer's entry point) ────────────────
    def _build_sample_grid(self):
        # aug (runway-midline) edges are routing-only — a point sample
        # entering through them returns runway values for taxi queries
        # (the TX1 plain-entry lesson)
        self._segs = []
        seen = set()
        for i in range(len(self.nodes)):
            if i in self.aug:
                continue
            for (j, _w, _c) in self.adj[i]:
                if j in self.aug:
                    continue
                key = (i, j) if i < j else (j, i)
                if key in seen or key in self.prox:
                    continue                 # prox edges aren't lanes
                seen.add(key)
                self._segs.append(key)
        self._segs.sort()
        self._grid = {}
        cell = self._cell
        for k, (i, j) in enumerate(self._segs):
            (ax, ay), (bx, by) = self.nodes[i], self.nodes[j]
            for gx in range(int(min(ax, bx) // cell),
                            int(max(ax, bx) // cell) + 1):
                for gy in range(int(min(ay, by) // cell),
                                int(max(ay, by) // cell) + 1):
                    self._grid.setdefault((gx, gy), []).append(k)

    def sample(self, x: float, y: float) -> Tuple[Optional[float], float]:
        """Field value at the nearest point of the nearest graph edge and
        the straight gap to it: ``(value, gap_m)``.  Exact for gaps up to
        one grid cell; ``(None, inf)`` beyond (a coverage hole — the
        caller keeps its local value there)."""
        cell = self._cell
        gx0, gy0 = int(x // cell), int(y // cell)
        best = (float("inf"), None, 0.0)
        for dgx in (-1, 0, 1):
            for dgy in (-1, 0, 1):
                for k in self._grid.get((gx0 + dgx, gy0 + dgy), ()):
                    i, j = self._segs[k]
                    (ax, ay), (bx, by) = self.nodes[i], self.nodes[j]
                    dx, dy = bx - ax, by - ay
                    s2 = dx * dx + dy * dy
                    if s2 < 1e-12:
                        continue
                    t = ((x - ax) * dx + (y - ay) * dy) / s2
                    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                    px, py = ax + t * dx, ay + t * dy
                    d = math.hypot(x - px, y - py)
                    if d < best[0]:
                        best = (d, k, t)
        if best[1] is None:
            return None, float("inf")
        i, j = self._segs[best[1]]
        v = self.elev[i] + best[2] * (self.elev[j] - self.elev[i])
        return v, best[0]

    def route_graph_view(self):
        """A ``taxi_routing.TaxiRouteGraph`` over THIS graph (index-keyed)
        — the route-band law's measuring graph under the network profile
        model.  Contains every lane the field knows (apt rows, discovered
        axes, runway midlines as aug, proximity/gap couplings), so the
        enforce's and validator's bands cannot under-connect relative to
        the solve (the one-law rule).  Aug nodes are excluded from plain
        queries exactly like the runway-augmented graph's midline nodes."""
        from auto_patch.taxi_routing import TaxiRouteGraph
        adj = {i: [(j, w) for (j, w, _c) in self.adj[i]]
               for i in range(len(self.nodes)) if self.adj[i]}
        coord = {i: self.nodes[i] for i in range(len(self.nodes))}
        return TaxiRouteGraph(adj, coord, _SNAP_TOL_M, set(self.aug))

    def vertices_with_values(self) -> List[Tuple[float, float, float]]:
        """``[(x, y, elev)]`` per PLAIN graph vertex (aug/runway-midline
        nodes excluded) — the validator's field anchors (route_field) and
        the apron-seed sources."""
        return [(self.nodes[i][0], self.nodes[i][1], self.elev[i])
                for i in range(len(self.nodes)) if i not in self.aug]


def _key_of(x: float, y: float) -> Tuple[int, int]:
    return (int(round(x / _SNAP_TOL_M)), int(round(y / _SNAP_TOL_M)))


def _ring_open(ring) -> list:
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _piece_axis_value(ring_pts, ring_elevs, x, y) -> Optional[float]:
    """Runway surface value at an interior point of a 4-corner piece:
    project onto the long axis between the short-end midpoints and lerp
    the end-pair levels (each end pair is co-level on the emitted plane)."""
    if len(ring_pts) != 4 or len(ring_elevs) < 4:
        return None
    if any(e is None for e in ring_elevs[:4]):
        return None
    edges = [(k, (k + 1) % 4) for k in range(4)]
    edges.sort(key=lambda ab: math.hypot(
        ring_pts[ab[1]][0] - ring_pts[ab[0]][0],
        ring_pts[ab[1]][1] - ring_pts[ab[0]][1]))
    (a0, b0), (a1, b1) = edges[0], edges[1]      # two short ends
    m0 = ((ring_pts[a0][0] + ring_pts[b0][0]) / 2.0,
          (ring_pts[a0][1] + ring_pts[b0][1]) / 2.0)
    m1 = ((ring_pts[a1][0] + ring_pts[b1][0]) / 2.0,
          (ring_pts[a1][1] + ring_pts[b1][1]) / 2.0)
    e0 = (ring_elevs[a0] + ring_elevs[b0]) / 2.0
    e1 = (ring_elevs[a1] + ring_elevs[b1]) / 2.0
    dx, dy = m1[0] - m0[0], m1[1] - m0[1]
    s2 = dx * dx + dy * dy
    if s2 < 1e-9:
        return None
    t = ((x - m0[0]) * dx + (y - m0[1]) * dy) / s2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return e0 + t * (e1 - e0)


def _seg_closest(P0, P1, Q0, Q1):
    """Closest approach between two segments: ``(d, t, u)`` with clamped
    parameters.  Standard quadratic minimisation; robust for parallel
    segments (falls back to endpoint projections)."""
    ux, uy = P1[0] - P0[0], P1[1] - P0[1]
    vx, vy = Q1[0] - Q0[0], Q1[1] - Q0[1]
    wx, wy = P0[0] - Q0[0], P0[1] - Q0[1]
    a = ux * ux + uy * uy
    b = ux * vx + uy * vy
    c = vx * vx + vy * vy
    d = ux * wx + uy * wy
    e = vx * wx + vy * wy
    den = a * c - b * b
    if den > 1e-9:
        t = (b * e - c * d) / den
    else:
        t = 0.0
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    if c > 1e-9:
        u = (b * t + e) / c
    else:
        u = 0.0
    u = 0.0 if u < 0.0 else (1.0 if u > 1.0 else u)
    if a > 1e-9:
        t = (b * u - d) / a
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    px, py = P0[0] + t * ux, P0[1] + t * uy
    qx, qy = Q0[0] + u * vx, Q0[1] + u * vy
    return math.hypot(px - qx, py - qy), t, u


def _seg_intersection(P0, P1, Q0, Q1):
    """Proper-interior intersection parameters ``(t, u)`` or None.  The
    ends are excluded with a small ABSOLUTE margin (0.5 m on each
    segment) so shared endpoints / already-split nodes don't re-split."""
    rX, rY = P1[0] - P0[0], P1[1] - P0[1]
    sX, sY = Q1[0] - Q0[0], Q1[1] - Q0[1]
    den = rX * sY - rY * sX
    if abs(den) < 1e-12:
        return None
    qpX, qpY = Q0[0] - P0[0], Q0[1] - P0[1]
    t = (qpX * sY - qpY * sX) / den
    u = (qpX * rY - qpY * rX) / den
    lp = math.hypot(rX, rY)
    lq = math.hypot(sX, sY)
    if lp < 1e-9 or lq < 1e-9:
        return None
    ep = min(0.5 / lp, 0.49)
    eq = min(0.5 / lq, 0.49)
    if ep < t < 1.0 - ep and eq < u < 1.0 - eq:
        return t, u
    return None


def build_and_solve(
        segments_xy: Sequence[Tuple[Tuple[float, float],
                                    Tuple[float, float]]],
        runway_rings: Sequence[Tuple[Sequence[Tuple[float, float]],
                                     Sequence[Optional[float]], str]],
        cap: float,
        dg_per_m: float,
        seed_at: Optional[Callable[[float, float], Optional[float]]] = None,
        exit_overrides: Sequence[Tuple[Tuple[float, float],
                                       Tuple[float, float],
                                       float, float]] = (),
        fallback_at: Optional[Callable[[float, float],
                                       Optional[float]]] = None,
        bridge_test: Optional[Callable[[Tuple[float, float],
                                        Tuple[float, float]], bool]] = None,
        bridge_m: float = 60.0,
        n_apt_segments: Optional[int] = None,
        extra_band_anchors: Sequence[Tuple[float, float, float]] = (),
        entry_dist: Optional[Callable[[Tuple[float, float],
                                       Tuple[float, float]],
                                      Optional[float]]] = None,
) -> Optional[NetworkProfileField]:
    """Build the centerline graph and solve the field.

    ``segments_xy``: every centerline segment (FULL apt.dat set + rect
    source axes — ``unified_jacobi._corridor_segments``), layout-local m.
    ``runway_rings``: ``(open_ring_coords, ring_elevs, ref)`` per emitted
    runway PIECE at SOLVED elevations — contact anchors come from these.
    ``cap``: legal long-range grade (taxi 1.5 %), ``dg_per_m``: grade-rate
    limit through vertices (TAXIWAY_MAX_GRADE_CHANGE_PER_M).
    ``seed_at(x, y)``: DEM elevation lookup (None entries fall back to the
    nearest anchor value).  ``exit_overrides``: per exit-fan terminus the
    caller measured ``(mouth_xy, contact_xy, arc_m, contact_value)`` —
    promoted into the graph as an arc-weighted contact edge (the apt.dat
    line cuts the fan corner; the curve-aware arc is the physical taxi
    path — s73-p10 / design prereq 1).

    ``fallback_at(x, y)``: CURRENT solved-surface lookup.  Components
    with NO runway contact seed from it instead of DEM — an anchor-less
    fragment must smooth the already-graded surface along its lane, not
    drag it back to terrain (measured at CYXY: DEM-seeded fragments wrote
    687 into a 697 surface = 25 % junction walls).
    ``bridge_test(pa, pb)``: True when the straight connector lies inside
    the airside pavement union.  Fragment END vertices within
    ``bridge_m`` of another component bridge through it — discovered-rect
    medial axes and apt.dat rows stop at junction/apron boundaries where
    the taxi surface physically continues (the graph fragmentation is a
    data artifact, not a topology truth).

    ``extra_band_anchors``: ``[(x, y, value)]`` pavement HARD pins the
    field must not contradict (base_hard: thresholds, tile-seam pins) —
    they seed the band Dijkstras through their nearest plain node +
    straight gap, exactly as the enforce will later anchor on them.
    Without these the field can legally sit where a seam pin's ceiling
    forbids the surface to follow (a 1 m lo>hi pinch at CYXY #63).

    ``entry_dist(pa, pb) -> float | None``: the INTERIOR-PATH measure
    (docs/interior_path_entries.md — "no grade checks across grass").
    When provided, the law-entry gap edges and the anchor band entries
    charge the in-pavement path length instead of the straight chord;
    ``None`` = no interior path = no coupling.  ``None`` (the
    parameter) keeps the legacy straight-gap behaviour (gate off).
    """
    import os as _os
    import time as _time
    _perf_on = _os.environ.get("O4_PERF") == "1"
    _pf: list = []
    _pt = [_time.time()]

    def _mark(label9):
        if _perf_on:
            now9 = _time.time()
            _pf.append((label9, now9 - _pt[0]))
            _pt[0] = now9

    raw = []
    raw_is_apt = []
    for k, (pa, pb) in enumerate(segments_xy):
        if math.hypot(pb[0] - pa[0], pb[1] - pa[1]) <= 1e-6:
            continue
        raw.append((pa, pb))
        raw_is_apt.append(n_apt_segments is None or k < n_apt_segments)
    if not raw or not runway_rings:
        return None

    # ── splitter set: lane×lane crossings + lane×runway-boundary
    # crossings (the contact sites).  Runway ring edges are NOT graph
    # edges — runways keep their own profile machinery (M1).
    cell = _GRID_CELL_M
    grid: Dict[Tuple[int, int], List[int]] = {}
    for k, (pa, pb) in enumerate(raw):
        for gx in range(int(min(pa[0], pb[0]) // cell),
                        int(max(pa[0], pb[0]) // cell) + 1):
            for gy in range(int(min(pa[1], pb[1]) // cell),
                            int(max(pa[1], pb[1]) // cell) + 1):
                grid.setdefault((gx, gy), []).append(k)

    splits: Dict[int, List[float]] = {}        # seg idx → [t, ...]
    contact_pts: List[Tuple[float, float, float, str]] = []  # x,y,val,ref

    # lane × lane
    done_pairs = set()
    for k in range(len(raw)):
        pa, pb = raw[k]
        cands = set()
        for gx in range(int(min(pa[0], pb[0]) // cell) - 1,
                        int(max(pa[0], pb[0]) // cell) + 2):
            for gy in range(int(min(pa[1], pb[1]) // cell) - 1,
                            int(max(pa[1], pb[1]) // cell) + 2):
                cands.update(grid.get((gx, gy), ()))
        for k2 in sorted(cands):
            if k2 <= k:
                continue
            pair = (k, k2)
            if pair in done_pairs:
                continue
            done_pairs.add(pair)
            qa, qb = raw[k2]
            hit = _seg_intersection(pa, pb, qa, qb)
            if hit is not None:
                t, u = hit
                splits.setdefault(k, []).append(t)
                splits.setdefault(k2, []).append(u)
                continue
            # NEAR-PASS split: two lanes passing within the proximity
            # radius without touching get vertices at the closest
            # approach, so node-proximity coupling can see the pass — a
            # LONG edge sailing 20 m from another lane otherwise carries
            # a field value metres off it with no constraint between
            # (CYXY #95: a 709.2 station lerp 30 m from 702.4 lanes).
            dd, t, u = _seg_closest(pa, pb, qa, qb)
            if dd <= 0.01 or dd > bridge_m:
                continue
            lp = math.hypot(pb[0] - pa[0], pb[1] - pa[1])
            lq = math.hypot(qb[0] - qa[0], qb[1] - qa[1])
            ep = min(0.5 / lp, 0.49) if lp > 1e-9 else 0.5
            eq = min(0.5 / lq, 0.49) if lq > 1e-9 else 0.5
            if ep < t < 1.0 - ep:
                splits.setdefault(k, []).append(t)
            if eq < u < 1.0 - eq:
                splits.setdefault(k2, []).append(u)

    # lane × runway boundary → contact anchors
    ring_data = []
    for ring, elevs, ref in runway_rings:
        pts = _ring_open(ring)
        ring_data.append((pts, list(elevs), ref))
    for k in range(len(raw)):
        pa, pb = raw[k]
        for (pts, elevs, ref) in ring_data:
            m = len(pts)
            for e in range(m):
                qa, qb = pts[e], pts[(e + 1) % m]
                hit = _seg_intersection(pa, pb, qa, qb)
                if hit is None:
                    continue
                t, u = hit
                splits.setdefault(k, []).append(t)
                x = pa[0] + t * (pb[0] - pa[0])
                y = pa[1] + t * (pb[1] - pa[1])
                ea = elevs[e] if e < len(elevs) else None
                eb = elevs[(e + 1) % m] if (e + 1) % m < len(elevs) else None
                if ea is None or eb is None:
                    continue
                contact_pts.append((x, y, ea + u * (eb - ea), ref))

    _mark("splits+contacts")
    # ── graph build (snapped keys; weights = true sub-segment lengths)
    coord: Dict[Tuple[int, int], Tuple[float, float]] = {}
    edge_w: Dict[Tuple[Tuple[int, int], Tuple[int, int]], float] = {}

    def _add_edge(p, q, w=None):
        ka, kb = _key_of(*p), _key_of(*q)
        if ka not in coord:
            coord[ka] = p
        if kb not in coord:
            coord[kb] = q
        if ka == kb:
            return
        if w is None:
            w = math.hypot(q[0] - p[0], q[1] - p[1])
        ek = (ka, kb) if ka < kb else (kb, ka)
        old = edge_w.get(ek)
        if old is None or w < old:
            edge_w[ek] = w

    apt_node_keys: set = set()
    for k, (pa, pb) in enumerate(raw):
        ts = sorted(set(min(max(t, 0.0), 1.0)
                        for t in splits.get(k, ())))
        pts = [pa]
        for t in ts:
            pts.append((pa[0] + t * (pb[0] - pa[0]),
                        pa[1] + t * (pb[1] - pa[1])))
        pts.append(pb)
        for p, q in zip(pts, pts[1:]):
            _add_edge(p, q)
        if raw_is_apt[k]:
            apt_node_keys.update(_key_of(*p) for p in pts)

    # exit-arc overrides: contact edge at the measured curve-aware arc
    for (mouth_xy, contact_xy, arc_m, _val, _ref) in exit_overrides:
        _add_edge(tuple(mouth_xy), tuple(contact_xy),
                  w=max(float(arc_m), 1.0))

    # ── contact anchors on graph nodes
    hard_val: Dict[Tuple[int, int], Tuple[float, str]] = {}
    for (x, y, v, ref) in sorted(contact_pts):
        kk = _key_of(x, y)
        if kk not in coord:
            coord[kk] = (x, y)
        old = hard_val.get(kk)
        # two crossings collapsing onto one snapped node: keep the LOWER
        # value (the conservative ceiling — they differ by <= cap*3 m).
        if old is None or v < old[0]:
            hard_val[kk] = (v, ref)
    for (_mouth_xy, contact_xy, _arc_m, val, ref) in exit_overrides:
        kk = _key_of(*contact_xy)
        if kk not in coord:
            coord[kk] = tuple(contact_xy)
        old = hard_val.get(kk)
        if old is None or val < old[0]:
            hard_val[kk] = (float(val), ref or (old[1] if old else ""))

    # polyline ENDS near a runway bridge to it as contacts (the apt.dat
    # rows stop at/near the edge; without this the network never reaches
    # the threshold anchors — the s68 lesson, same reach as the runway
    # augmentation).
    end_keys: Dict[Tuple[int, int], Tuple[float, float]] = {}
    deg_count: Dict[Tuple[int, int], int] = {}
    for (ka, kb) in edge_w:
        deg_count[ka] = deg_count.get(ka, 0) + 1
        deg_count[kb] = deg_count.get(kb, 0) + 1
    for kk, dcount in deg_count.items():
        if dcount == 1 and kk not in hard_val:
            end_keys[kk] = coord[kk]
    try:
        from shapely.geometry import Point as _ShPoint, Polygon as _ShPoly
        from shapely.prepared import prep as _shprep
    except Exception:                              # pragma: no cover
        _ShPoint = _ShPoly = _shprep = None
    rw_polys = []
    if _ShPoly is not None:
        for (pts, elevs, ref) in ring_data:
            try:
                rw_polys.append((_ShPoly(pts), _shprep(_ShPoly(pts)),
                                 pts, elevs, ref))
            except Exception:
                continue
    for kk in sorted(end_keys):
        x, y = end_keys[kk]
        best = None
        for (poly, _pp, pts, elevs, ref) in rw_polys:
            try:
                d = poly.exterior.distance(_ShPoint((x, y)))
            except Exception:
                continue
            if d > _END_BRIDGE_M:
                continue
            m = len(pts)
            for e in range(m):
                qa, qb = pts[e], pts[(e + 1) % m]
                dx, dy = qb[0] - qa[0], qb[1] - qa[1]
                s2 = dx * dx + dy * dy
                if s2 < 1e-9:
                    continue
                t = ((x - qa[0]) * dx + (y - qa[1]) * dy) / s2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                px, py = qa[0] + t * dx, qa[1] + t * dy
                dd = math.hypot(x - px, y - py)
                ea = elevs[e] if e < len(elevs) else None
                eb = elevs[(e + 1) % m] if (e + 1) % m < len(elevs) else None
                if dd <= _END_BRIDGE_M and ea is not None and eb is not None:
                    val = ea + t * (eb - ea)
                    if best is None or dd < best[0]:
                        best = (dd, (px, py), val, ref)
        if best is not None:
            dd, cxy, val, ref = best
            _add_edge(end_keys[kk], cxy, w=max(dd, 0.5))
            ck = _key_of(*cxy)
            old = hard_val.get(ck)
            if old is None or val < old[0]:
                hard_val[ck] = (val, ref)

    # ── runway MIDLINE chains (hard at the piece end-pair values): the
    # enforce's and validator's route graphs carry runway-centerline
    # shortcut edges (augment_with_runway_centerlines) — a field solved
    # WITHOUT them is legal on a longer graph and writes values the
    # downstream band law then rejects (measured at CYXY apron #68:
    # held 713.1 vs a 702.5 ceiling that arrives THROUGH the runway).
    # The field graph must be at least as constrained as every measure
    # downstream of it.  Midline nodes are AUG — routing only, never a
    # sampling target (the TX1 plain-entry lesson).
    aug_keys: set = set()
    plain_snapshot = sorted(coord)
    for (pts, elevs, _ref) in ring_data:
        if len(pts) != 4:
            continue
        edges4 = [(k, (k + 1) % 4) for k in range(4)]
        edges4.sort(key=lambda ab: math.hypot(
            pts[ab[1]][0] - pts[ab[0]][0],
            pts[ab[1]][1] - pts[ab[0]][1]))
        mids4 = []
        for (a4, b4) in edges4[:2]:                    # two short ends
            ea4 = elevs[a4] if a4 < len(elevs) else None
            eb4 = elevs[b4] if b4 < len(elevs) else None
            if ea4 is None or eb4 is None:
                mids4 = []
                break
            mids4.append((((pts[a4][0] + pts[b4][0]) / 2.0,
                           (pts[a4][1] + pts[b4][1]) / 2.0),
                          (ea4 + eb4) / 2.0))
        if len(mids4) != 2:
            continue
        (m0, e0), (m1, e1) = mids4
        _add_edge(m0, m1)
        for (mp, me) in ((m0, e0), (m1, e1)):
            mk = _key_of(*mp)
            aug_keys.add(mk)
            old = hard_val.get(mk)
            if old is None or me < old[0]:
                hard_val[mk] = (me, _ref)
            # bridge to the nearest pre-existing PLAIN node (the taxi
            # rows stop at/near the runway edge) — same 40 m reach as
            # taxi_routing.augment_with_runway_centerlines
            best4 = None
            for kk2 in plain_snapshot:
                x2, y2 = coord[kk2]
                d4 = math.hypot(x2 - mp[0], y2 - mp[1])
                if d4 <= 40.0 and (best4 is None or d4 < best4[0]):
                    best4 = (d4, kk2)
            if best4 is not None:
                _add_edge(mp, coord[best4[1]], w=max(best4[0], 0.5))
    # contacts sit ON the runway edge: tie each to the nearest midline
    # node within ~60 m so a route may continue along the runway from
    # the very point it touches it (the half-width transverse hop).
    if aug_keys:
        aug_sorted = sorted(aug_keys)
        for ck in sorted(hard_val):
            if ck in aug_keys:
                continue
            x, y = coord.get(ck, (None, None))
            if x is None:
                continue
            best4 = None
            for mk in aug_sorted:
                x2, y2 = coord[mk]
                d4 = math.hypot(x2 - x, y2 - y)
                if d4 <= 60.0 and (best4 is None or d4 < best4[0]):
                    best4 = (d4, mk)
            if best4 is not None:
                _add_edge((x, y), coord[best4[1]], w=max(best4[0], 0.5))

    _mark("edges+midlines")
    # ── PROXIMITY COUPLING through pavement: any two graph nodes whose
    # straight connector lies inside the airside union are points on ONE
    # contiguous surface — the downstream band law couples them through
    # its straight-line entry gaps at cap, so a field that treats them as
    # independent writes values the law then pinches (measured at CYXY:
    # an anchor-less 704.9 fragment 62 m from an anchored 712.9 lane —
    # enforce bands [710.3, 702.5] = a 7.8 m self-pinch, 25 % apron
    # walls).  Proximity edges carry the CAP only, never the grade-rate
    # law (they are surface continuity, not taxi paths).  This subsumes
    # fragment bridging (connectivity follows from coupling).
    prox_keys: set = set()
    if bridge_test is not None and bridge_m > 0.0:
        kcell: Dict[Tuple[int, int], List] = {}
        for kk in sorted(coord):
            x, y = coord[kk]
            kcell.setdefault((int(x // bridge_m),
                              int(y // bridge_m)), []).append(kk)
        for kk in sorted(coord):
            x, y = coord[kk]
            cx, cy = int(x // bridge_m), int(y // bridge_m)
            for dgx in (-1, 0, 1):
                for dgy in (-1, 0, 1):
                    for kk2 in kcell.get((cx + dgx, cy + dgy), ()):
                        if kk2 <= kk:
                            continue
                        ek = (kk, kk2)
                        if ek in edge_w:
                            continue
                        x2, y2 = coord[kk2]
                        d = math.hypot(x2 - x, y2 - y)
                        if d > bridge_m:
                            continue
                        try:
                            w9 = bridge_test((x, y), (x2, y2))
                        except Exception:
                            w9 = None
                        if w9 is None or w9 is False:
                            continue
                        # bridge_test returns the coupling WEIGHT (the
                        # straight distance, inflated around small
                        # non-pavement notches); True means "use the
                        # straight distance" (older callers)
                        w9 = d if w9 is True else float(w9)
                        edge_w[ek] = max(w9, 0.5)
                        prox_keys.add(ek)

    _mark("proximity")
    # ── LAW-ENTRY GAP EDGES: the downstream route-band law enters every
    # anchor at its nearest APT-CENTERLINE node across a straight gap at
    # cap — for field nodes off the apt rows (discovered-rect axes,
    # contacts on lanes the route graph lacks) that coupling exists in
    # the LAW but not in the raw segment graph, and the field writes
    # value pairs the enforce then pinches (CYXY n31@718.5 vs n42@711.9,
    # law route 99 m, field route ∞).  Mirror the entry: one gap edge
    # per non-apt node to its nearest apt node (cap-only, like prox).
    _GAP_MAX_M = 300.0           # beyond this the law band is ≥ ~4.7 m —
    apt_cell: Dict[Tuple[int, int], List] = {}        # negligible
    # the law graph = apt rows + runway midlines (anchors enter EITHER —
    # G.nearest_key without plain_only), so gap edges target both
    for kk in sorted(apt_node_keys | aug_keys):
        if kk not in coord:
            continue
        x, y = coord[kk]
        apt_cell.setdefault((int(x // 100.0), int(y // 100.0)),
                            []).append(kk)
    for kk in sorted(coord):
        if kk in apt_node_keys or kk in aug_keys:
            continue
        x, y = coord[kk]
        cx, cy = int(x // 100.0), int(y // 100.0)
        best = None
        for r in range(4):
            for dgx in range(-r, r + 1):
                for dgy in range(-r, r + 1):
                    if max(abs(dgx), abs(dgy)) != r:
                        continue
                    for kk2 in apt_cell.get((cx + dgx, cy + dgy), ()):
                        x2, y2 = coord[kk2]
                        d = math.hypot(x2 - x, y2 - y)
                        if d <= _GAP_MAX_M and (best is None
                                                or d < best[0]):
                            best = (d, kk2)
            if best is not None and best[0] <= (r * 100.0):
                break
        if best is None:
            continue
        d, kk2 = best
        if entry_dist is not None:
            # interior-path entries (docs/interior_path_entries.md):
            # charge the in-pavement path; no path = no coupling.
            d = entry_dist(coord[kk], coord[kk2])
            if d is None:
                continue
        ek = (kk, kk2) if kk < kk2 else (kk2, kk)
        if ek not in edge_w:
            edge_w[ek] = max(d, 0.5)
            prox_keys.add(ek)            # cap-only, not a taxi path

    _mark("gap-edges")
    # ── index the nodes (sorted keys → deterministic ids)
    keys = sorted(coord)
    idx_of = {kk: i for i, kk in enumerate(keys)}
    F = NetworkProfileField()
    F.aug = {idx_of[kk] for kk in aug_keys}
    F.prox = {(min(idx_of[ka], idx_of[kb]), max(idx_of[ka], idx_of[kb]))
              for (ka, kb) in prox_keys}
    F.nodes = [coord[kk] for kk in keys]
    n = len(F.nodes)
    F.adj = [[] for _ in range(n)]
    for (ka, kb), w in sorted(edge_w.items()):
        ia, ib = idx_of[ka], idx_of[kb]
        F.adj[ia].append((ib, w, cap))
        F.adj[ib].append((ia, w, cap))
    for i in range(n):
        F.adj[i].sort()

    F.hard = [False] * n
    F.elev = [0.0] * n
    for kk in sorted(hard_val):
        i = idx_of[kk]
        v, ref = hard_val[kk]
        F.hard[i] = True
        F.elev[i] = v
        if kk not in aug_keys:           # midline nodes anchor, but only
            F.contacts.append((i, v, ref, coord[kk]))  # contacts demand

    # runway-INTERIOR vertices (crossing-lane runs across the runway) are
    # part of the runway surface: anchor them at the containing piece's
    # axis-lerped value (the crossing carries the runway profile).
    n_interior = 0
    interior_ref: Dict[int, str] = {}
    if rw_polys:
        for i in range(n):
            if F.hard[i]:
                continue
            x, y = F.nodes[i]
            for (_poly, pp, pts, elevs, _ref) in rw_polys:
                try:
                    inside = pp.contains(_ShPoint((x, y)))
                except Exception:
                    inside = False
                if not inside:
                    continue
                v = _piece_axis_value(pts, elevs, x, y)
                if v is not None:
                    F.hard[i] = True
                    F.elev[i] = v
                    interior_ref[i] = _ref
                    n_interior += 1
                break

    _mark("index+interior")
    # ── components
    parent = list(range(n))

    def _find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(n):
        for (j, _w, _c) in F.adj[i]:
            ra, rb = _find(i), _find(j)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    F.comp_of = [_find(i) for i in range(n)]

    # ── per-component MINIMAX cap relaxation (M5): jointly-infeasible
    # hard anchors spread as a uniform slight over-cap, never a wall.
    anchors_by_comp: Dict[int, List[int]] = {}
    for i in range(n):
        if F.hard[i]:
            anchors_by_comp.setdefault(F.comp_of[i], []).append(i)

    def _dijkstra_from(src_list, cap_scale, comp=None,
                       values: Optional[List[float]] = None,
                       point_seeds: Optional[List[Tuple[int,
                                                        float]]] = None):
        """Multi-source: dist[i] = min over sources s of
        (values[s] + cap_scale * route(s, i)); plain route distance when
        ``values`` is None (then cap_scale multiplies length only).
        ``point_seeds`` = extra ``(node, base_value)`` entries (off-graph
        anchors entering at a node with their gap already charged)."""
        dist = [float("inf")] * n
        pq = []
        for s in src_list:
            v0 = (values[s] if values is not None else 0.0)
            if v0 < dist[s]:
                dist[s] = v0
                heapq.heappush(pq, (v0, s))
        for (s, v0) in (point_seeds or ()):
            if comp is not None and F.comp_of[s] != comp:
                continue
            if v0 < dist[s]:
                dist[s] = v0
                heapq.heappush(pq, (v0, s))
        while pq:
            d, u = heapq.heappop(pq)
            if d > dist[u]:
                continue
            for (v, w, _c) in F.adj[u]:
                if comp is not None and F.comp_of[v] != comp:
                    continue
                nd = d + w * cap_scale
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return dist

    # Anchor refs for the relax scope: SAME-runway contact pairs never
    # drive the relax — a runway whose own profile descends faster than
    # the taxi cap over the parallel route (SPLP: 17.2 m over a 688 m
    # route at the tile seam, need 1.67) is the runway's own
    # terrain/threshold story; smearing it minimax degraded EVERY
    # taxiway to 2.5 % (197 violations).  That residual stays local
    # (band pinch → least-violation placement — the established
    # deferred-lump class).  CROSS-runway squeezes keep the M5 spread
    # (HECA: relax 1.09 → flex → 1.03, measured correct).
    ref_of_anchor: Dict[int, str] = dict(interior_ref)
    for (i, _v, r, _xy) in F.contacts:
        ref_of_anchor[i] = r
    for kk in sorted(aug_keys):
        i = idx_of[kk]
        if F.hard[i]:
            ref_of_anchor.setdefault(i, hard_val.get(kk, ("", ""))[1])
    F.relax = {}
    for comp in sorted(anchors_by_comp):
        anchors = anchors_by_comp[comp]
        factor = 1.0
        if len(anchors) > 1:
            for s in anchors:
                dist = _dijkstra_from([s], 1.0, comp=comp)
                es = F.elev[s]
                rs = ref_of_anchor.get(s, "")
                for t in anchors:
                    if t == s:
                        continue
                    rt = ref_of_anchor.get(t, "")
                    if rs and rt and rs == rt:
                        continue          # same-runway pair: not ours
                    d = dist[t]
                    if d == float("inf") or d < 5.0:
                        continue
                    need = abs(F.elev[t] - es) / (cap * d)
                    if need > factor:
                        factor = need
        F.relax[comp] = factor

    _mark("relax")
    # ── runway-flex demands (M3, BEFORE relaxing): per runway ref, the
    # ceiling AND floor its contacts get from every OTHER ref's contacts
    # — "the profile value the network wants at the contact".  DIP and
    # RISE are both legal (user 2026-06-11): on the field the basis is
    # HARD-anchored contact-vs-contact, so a rise demand cannot come
    # from DEM-settled free pavement (the class the old DIP-only rule
    # excluded — s73-p9's false 05L +1.1 rise).
    refs = sorted({ref for (_i, _v, ref, _xy) in F.contacts})
    try:
        from auto_patch.config import ROUTE_NOISE_FRAC as _NOISE
    except Exception:                                  # pragma: no cover
        _NOISE = 0.04
    for ref in refs:
        own = [i for (i, _v, r, _xy) in F.contacts if r == ref]
        others = [i for (i, _v, r, _xy) in F.contacts if r != ref]
        if not own or not others:
            continue
        vals = list(F.elev)
        # demand bounds carry the validator's route-noise margin —
        # without it the dip over-shoots by noise·cap·route (HECA 05C:
        # 106.9 vs the user-blessed ~108.5 over a ~3.2 km route)
        capm9 = cap * (1.0 + _NOISE)
        ceil = _dijkstra_from(others, capm9, values=vals)
        negv = [-v for v in vals]
        flr = _dijkstra_from(others, capm9, values=negv)
        for i in sorted(own):
            wanted_hi = ceil[i]
            wanted_lo = -flr[i] if flr[i] < float("inf") else float("-inf")
            if (wanted_hi < float("inf")
                    and F.elev[i] - wanted_hi >= _DEMAND_MIN_DIP_M):
                F.demands.append((F.nodes[i], wanted_hi, ref, "dip"))
            elif (wanted_lo > float("-inf")
                    and wanted_lo - F.elev[i] >= _DEMAND_MIN_DIP_M):
                F.demands.append((F.nodes[i], wanted_lo, ref, "rise"))

    _mark("demands")
    # ── anchor-feasibility bands at the (relaxed) effective cap
    INF = float("inf")
    F.band_lo = [-INF] * n
    F.band_hi = [INF] * n
    # base_hard pins enter at their nearest PLAIN node + straight gap —
    # the same way the enforce will anchor on them later
    plain_cell: Dict[Tuple[int, int], List[int]] = {}
    for i in range(n):
        if i in F.aug:
            continue
        x, y = F.nodes[i]
        plain_cell.setdefault((int(x // 100.0), int(y // 100.0)),
                              []).append(i)
    extra_entry: List[Tuple[int, float, float]] = []   # (node, gap, value)
    for (xa, ya, va) in (extra_band_anchors or ()):
        cx, cy = int(xa // 100.0), int(ya // 100.0)
        best = None
        for r in range(3):
            for dgx in range(-r, r + 1):
                for dgy in range(-r, r + 1):
                    if max(abs(dgx), abs(dgy)) != r:
                        continue
                    for i in plain_cell.get((cx + dgx, cy + dgy), ()):
                        x, y = F.nodes[i]
                        d = math.hypot(x - xa, y - ya)
                        if best is None or d < best[0]:
                            best = (d, i)
            if best is not None and best[0] <= r * 100.0:
                break
        if best is not None and best[0] <= 200.0:
            gap9 = best[0]
            if entry_dist is not None and gap9 > 0.5:
                # interior-path anchor entry (the s79 measurement:
                # gating these is FREE at CYXY; the tile-seam case is
                # the SPLP watch item — docs/interior_path_entries.md
                # §6)
                gap9 = entry_dist((xa, ya), F.nodes[best[1]])
                if gap9 is None:
                    continue
            extra_entry.append((best[1], gap9, float(va)))
    band_comps = set(anchors_by_comp)
    band_comps.update(F.comp_of[i] for (i, _g, _v) in extra_entry)
    if band_comps:
        # effective cap varies per component → run per component
        for comp in sorted(band_comps):
            eff = cap * F.relax.get(comp, 1.0) + 1e-9
            srcs = anchors_by_comp.get(comp, [])
            up_seeds = [(i, v + eff * g) for (i, g, v) in extra_entry]
            dn_seeds = [(i, -v + eff * g) for (i, g, v) in extra_entry]
            up = _dijkstra_from(srcs, eff, comp=comp, values=F.elev,
                                point_seeds=up_seeds)
            neg = [-F.elev[s] for s in range(n)]
            dn = _dijkstra_from(srcs, eff, comp=comp, values=neg,
                                point_seeds=dn_seeds)
            for i in range(n):
                if F.comp_of[i] != comp:
                    continue
                if up[i] < INF:
                    F.band_hi[i] = up[i] + 0.02
                if dn[i] < INF:
                    F.band_lo[i] = -dn[i] - 0.02
                if F.band_lo[i] > F.band_hi[i]:      # least-violation
                    mid = 0.5 * (F.band_lo[i] + F.band_hi[i])
                    F.band_lo[i] = F.band_hi[i] = mid

    _mark("bands")
    # ── seed (M5), clamp into bands.  ANCHORED components seed at DEM —
    # the runway-profile pattern, correct grade is king and DEM the
    # starting point.  ANCHOR-LESS components seed at the CURRENT SOLVED
    # SURFACE: they carry no route demand of their own, so their job is
    # to smooth the already-graded surface along the lane (DEM-seeding
    # them wrote raw terrain into graded junctions — 25 % walls at CYXY).
    for i in range(n):
        if F.hard[i]:
            continue
        anchored_comp = F.comp_of[i] in anchors_by_comp
        x9, y9 = F.nodes[i]
        if anchored_comp:
            sv = seed_at(x9, y9) if seed_at is not None else None
            if sv is None and fallback_at is not None:
                sv = fallback_at(x9, y9)
        else:
            sv = fallback_at(x9, y9) if fallback_at is not None else None
            if sv is None and seed_at is not None:
                sv = seed_at(x9, y9)
        if sv is None:
            if F.band_lo[i] > -INF and F.band_hi[i] < INF:
                sv = 0.5 * (F.band_lo[i] + F.band_hi[i])
            else:
                sv = 0.0
        F.elev[i] = min(max(float(sv), F.band_lo[i]), F.band_hi[i])

    # ── Gauss-Seidel projection: per-edge cap, then grade-rate through
    # vertices, then band clamp — sorted order, fixed sweep structure.
    edge_list = []
    seen = set()
    for i in range(n):
        for (j, w, _c) in F.adj[i]:
            ek = (i, j) if i < j else (j, i)
            if ek in seen:
                continue
            seen.add(ek)
            edge_list.append((ek[0], ek[1], w))
    edge_list.sort()
    eff_of = [cap * F.relax.get(F.comp_of[i], 1.0) for i in range(n)]

    # Phase 1 alternates cap projection with the grade-rate pass; the two
    # OSCILLATE where a smooth ramp wants >cap (measured at CYXY: a
    # 14 m / 724 m descent left every edge ~2 %).  The cap is LAW, the
    # rate limit a smoothness preference — phase 2 finishes cap+band
    # only, to convergence.
    for _sweep in range(2 * _SOLVE_MAX_SWEEPS):
        dg_active = (dg_per_m > 0.0 and _sweep < _SOLVE_MAX_SWEEPS)
        moved = 0.0
        for (a, b, w) in edge_list:
            lim = max(eff_of[a], eff_of[b]) * w + 1e-6
            diff = F.elev[a] - F.elev[b]
            ex = abs(diff) - lim
            if ex <= 0.0:
                continue
            sgn = 1.0 if diff > 0 else -1.0
            ha, hb = F.hard[a], F.hard[b]
            if ha and hb:
                continue                  # relaxation already minimax
            if ha:
                nv = F.elev[b] + sgn * ex
                nv = min(max(nv, F.band_lo[b]), F.band_hi[b])
                moved = max(moved, abs(nv - F.elev[b]))
                F.elev[b] = nv
            elif hb:
                nv = F.elev[a] - sgn * ex
                nv = min(max(nv, F.band_lo[a]), F.band_hi[a])
                moved = max(moved, abs(nv - F.elev[a]))
                F.elev[a] = nv
            else:
                na = F.elev[a] - sgn * ex / 2.0
                nb = F.elev[b] + sgn * ex / 2.0
                na = min(max(na, F.band_lo[a]), F.band_hi[a])
                nb = min(max(nb, F.band_lo[b]), F.band_hi[b])
                moved = max(moved, abs(na - F.elev[a]),
                            abs(nb - F.elev[b]))
                F.elev[a], F.elev[b] = na, nb
        # grade-rate limit through vertices (M4): for each soft vertex
        # and incident-edge pair, |g_out - g_in| <= dg * mean(span).
        if dg_active:
            for v in range(n):
                if F.hard[v]:
                    continue
                nbrs = [(j, w) for (j, w, _c) in F.adj[v]
                        if w >= _DG_MIN_EDGE_M
                        and (min(v, j), max(v, j)) not in F.prox]
                if len(nbrs) < 2:
                    continue
                lo_v, hi_v = F.band_lo[v], F.band_hi[v]
                ev = F.elev[v]
                for a2 in range(len(nbrs)):
                    ju, wu = nbrs[a2]
                    for b2 in range(a2 + 1, len(nbrs)):
                        jw, ww = nbrs[b2]
                        L = dg_per_m * (wu + ww) / 2.0
                        k2 = 1.0 / wu + 1.0 / ww
                        base = F.elev[ju] / wu + F.elev[jw] / ww
                        lo2 = (base - L) / k2
                        hi2 = (base + L) / k2
                        if ev < lo2:
                            ev = min(max(lo2, lo_v), hi_v)
                        elif ev > hi2:
                            ev = min(max(hi2, lo_v), hi_v)
                if ev != F.elev[v]:
                    moved = max(moved, abs(ev - F.elev[v]))
                    F.elev[v] = ev
        if moved < _SOLVE_TOL_M:
            if not dg_active:
                break
            # rate pass converged jointly with the caps — go straight
            # to the cap-only phase (usually an immediate no-op)
            dg_per_m = 0.0

    _mark("gs-solve")
    F._build_sample_grid()

    comp_stats: Dict[int, Dict] = {}
    for i in range(n):
        c = F.comp_of[i]
        st = comp_stats.setdefault(
            c, {"nodes": 0, "len_m": 0.0, "contacts": 0, "refs": set()})
        st["nodes"] += 1
    for (a, b, w) in edge_list:
        comp_stats[F.comp_of[a]]["len_m"] += w
    for (i, _v, ref, _xy) in F.contacts:
        st = comp_stats[F.comp_of[i]]
        st["contacts"] += 1
        st["refs"].add(ref)
    if _perf_on:
        parts9 = " ".join(f"{k}={v:.2f}s" for (k, v) in _pf if v >= 0.01)
        print(f"  [perf]   field build: {parts9}")
    F.audit = {
        "nodes": n,
        "edges": len(edge_list),
        "contacts": len(F.contacts),
        "interior_anchors": n_interior,
        "proximity_edges": len(prox_keys),
        "components": {
            c: {"nodes": st["nodes"], "len_m": round(st["len_m"], 1),
                "contacts": st["contacts"],
                "refs": sorted(st["refs"]),
                "relax": round(F.relax.get(c, 1.0), 4)}
            for c, st in sorted(comp_stats.items())},
        "demands": [((round(x, 1), round(y, 1)), round(v, 2), r, kind)
                    for ((x, y), v, r, kind) in F.demands],
    }
    return F
