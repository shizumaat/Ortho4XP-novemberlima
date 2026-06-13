"""Interior-path distance through the airside pavement union.

★★ USER RULING (2026-06-11): no shape may ever check grade ACROSS
GRASS.  Every off-graph point that enters the centerline route graph
(route-band law anchors and check vertices, the network-profile
field's law-entry gap edges and band anchors, `_runway_reach_bands`
gap charging) used to charge its entry as a STRAIGHT gap at cap —
pavement-blind.  This module supplies the lawful replacement: the
length of the shortest path that stays INSIDE pavement, or ``None``
when no such path exists (a true island — then no coupling exists at
all; anchor-less components seed from the current surface).

Measured basis (s79, docs/interior_path_entries.md): every
load-bearing straight-gap coupling at CYXY has a real interior path
(the binding pair: 89 m straight vs 103 m interior), so the law
barely moves where the coupling is genuine and disappears where the
chord crossed real grass.

Design:
  * FAST PATH — the straight chord lies inside the (slightly
    buffered) union: return its length.  This answers almost every
    query; entry gaps are usually small and on-pavement.
  * Else a LOCAL visibility-graph Dijkstra inside the airside
    component shared by both endpoints, windowed around the pair;
    the window escalates once before concluding ``None`` (a falsely
    orphaned component writes drift — the 25 %-walls class).
  * Deterministic: vertices sorted, no set/dict order leaks.

Shared by the solver (``elevation_per_surface/unified_jacobi``), the
field (``network_profile``) and the validator (``tools/check_grade``)
— ONE measure, so law-graph parity holds by construction (the s78p5 /
s79 partial-application failure mode).
"""
from __future__ import annotations

import heapq
import math
from typing import Dict, List, Optional, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union
from shapely.prepared import prep

_GEOM_EXC = (ValueError, TypeError, GEOSException, TopologicalError)

# The airside-pavement role set the measure's union is built from —
# ONE canonical list (string form) so the SOLVER (unified_jacobi
# ``_interior_entry_dist``) and the VALIDATOR (tools/check_grade) build
# the SAME geometry: mirrors ``unified_jacobi.PAVEMENT_ROLES``
# (runway + sloping rects incl. service_road + apron/terminal/junction
# families).  Groundside / boundary / clearance are NOT pavement for
# the law — a path through them is not an airside surface connection.
AIRSIDE_MEASURE_ROLES = frozenset((
    "runway", "runway_crossing",
    "primary_parallel", "secondary_parallel", "stub", "cross_connector",
    "service_road", "service_junction",
    "apron", "building", "junction",
))

# Buffer applied to the raw airside union (mirrors the prox
# ``bridge_test``'s 0.5 m: emit-time float noise must not read as
# grass).
_UNION_BUFFER_M = 0.5
# Endpoint-to-component attachment tolerance (a vertex ON the
# boundary is inside its component).
_ATTACH_TOL_M = 1.0
# Visibility-window half-extent = max(this, straight distance);
# escalated ×3 once when the windowed search fails but the endpoints
# share a component.
_WINDOW_MIN_M = 150.0
# Boundary simplification + vertex cap keep the O(V²) visibility test
# bounded; windows are entry-gap sized so this is rarely binding.
_SIMPLIFY_M = 1.5
_MAX_VERTS = 240
# Entry gaps at or below this return the straight length without ANY
# geometry test: the band effect is ≤ cap·5 m ≈ 7.5 cm — sub-noise
# (ELEV_ROUNDING_NOISE scale), and a 5 m chord cannot span meaningful
# grass.
_TRIVIAL_GAP_M = 5.0
# A chord whose OUTSIDE-pavement length is at most this is a GRAZE
# (float noise / sliver notch): charge straight + 2×outside (a
# conservative lower bound on the wrap-around) instead of paying the
# visibility Dijkstra.  Real grass spans run the full search.
_GRAZE_MAX_M = 2.0


class InteriorPathMeasure:
    """In-pavement path length between two points; ``None`` = no path."""

    def __init__(self, airside_union, *, pre_buffered: bool = False):
        geom = airside_union if pre_buffered \
            else airside_union.buffer(_UNION_BUFFER_M)
        self._geom = geom
        self._prep = prep(geom)
        parts = (list(geom.geoms) if geom.geom_type == "MultiPolygon"
                 else [geom])
        # deterministic component order
        self._parts = sorted((p for p in parts if not p.is_empty),
                             key=lambda p: (-p.area, p.bounds))
        self._cache: Dict[Tuple, Optional[float]] = {}
        # Lazy TILE cache: clipping the (potentially huge — HECA's
        # slab has an ~83 km perimeter) union once per 400 m cell makes
        # every subsequent local test (graze difference, window
        # extraction) operate on a small piece instead of the full
        # geometry — the difference-against-the-slab cost was 100+ s
        # of HECA enforce time.
        self._tile_m = 400.0
        self._tiles: Dict[Tuple[int, int], object] = {}
        self.stats = {"trivial": 0, "cached": 0, "covered": 0,
                      "graze": 0, "dijkstra": 0, "none": 0}

    def _local_geom(self, minx, miny, maxx, maxy):
        """Union of cached 400 m tiles covering the bbox (None when the
        bbox touches no pavement)."""
        t = self._tile_m
        keys = [(gx, gy)
                for gx in range(int(minx // t), int(maxx // t) + 1)
                for gy in range(int(miny // t), int(maxy // t) + 1)]
        pieces = []
        for k in keys:
            g = self._tiles.get(k)
            if g is None:
                cell = box(k[0] * t - 2.0, k[1] * t - 2.0,
                           (k[0] + 1) * t + 2.0, (k[1] + 1) * t + 2.0)
                try:
                    g = self._geom.intersection(cell)
                except _GEOM_EXC:
                    g = cell.buffer(-1e9)        # empty
                self._tiles[k] = g
            if not g.is_empty:
                pieces.append(g)
        if not pieces:
            return None
        if len(pieces) == 1:
            return pieces[0]
        try:
            return unary_union(pieces)
        except _GEOM_EXC:
            return None

    # ── public ────────────────────────────────────────────────────
    def distance(self, pa: Tuple[float, float],
                 pb: Tuple[float, float]) -> Optional[float]:
        straight = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
        if straight <= _TRIVIAL_GAP_M:
            self.stats["trivial"] += 1
            return straight
        key = (round(pa[0], 1), round(pa[1], 1),
               round(pb[0], 1), round(pb[1], 1))
        if key in self._cache:
            self.stats["cached"] += 1
            return self._cache[key]
        result: Optional[float]
        try:
            chord = LineString([pa, pb])
            if self._prep.covers(chord):
                self.stats["covered"] += 1
                result = straight
            else:
                local = self._local_geom(*chord.bounds)
                out_len = (chord.difference(local).length
                           if local is not None else straight)
                if out_len <= _GRAZE_MAX_M:
                    self.stats["graze"] += 1
                    result = straight + 2.0 * out_len
                else:
                    self.stats["dijkstra"] += 1
                    result = self._slow_path(pa, pb, straight)
        except _GEOM_EXC:
            result = None
        if result is None:
            self.stats["none"] += 1
        self._cache[key] = result
        return result

    # ── internals ─────────────────────────────────────────────────
    def _component(self, p: Point):
        for part in self._parts:
            try:
                if part.distance(p) <= _ATTACH_TOL_M:
                    return part
            except _GEOM_EXC:
                continue
        return None

    def _slow_path(self, pa, pb, straight) -> Optional[float]:
        A, B = Point(pa), Point(pb)
        ca = self._component(A)
        if ca is None:
            return None
        cb = self._component(B)
        if cb is None or cb is not ca:
            return None              # different islands: no coupling
        d = self._window_dijkstra(ca, pa, pb, straight, scale=1.0)
        if d is None:
            # same component but the window cut the path — widen once
            d = self._window_dijkstra(ca, pa, pb, straight, scale=3.0)
        return d

    def _window_dijkstra(self, part, pa, pb, straight,
                         scale: float) -> Optional[float]:
        e = max(_WINDOW_MIN_M, straight) * scale
        win = box(min(pa[0], pb[0]) - e, min(pa[1], pb[1]) - e,
                  max(pa[0], pb[0]) + e, max(pa[1], pb[1]) + e)
        try:
            base = self._local_geom(*win.bounds)
            if base is None:
                return None
            local = base.intersection(win)
            if local.is_empty:
                return None
            local = local.simplify(_SIMPLIFY_M)
            local = local.buffer(0.2)      # heal simplify nicks
        except _GEOM_EXC:
            return None
        polys = (list(local.geoms) if local.geom_type == "MultiPolygon"
                 else [local] if local.geom_type == "Polygon" else [])
        # keep the piece(s) holding the endpoints
        keep = [g for g in polys
                if g.distance(Point(pa)) <= _ATTACH_TOL_M
                or g.distance(Point(pb)) <= _ATTACH_TOL_M]
        if not keep:
            return None
        try:
            geom = unary_union(keep)
            # Edge tests run against a slightly grown copy: a chord
            # ENDING at a concave ring corner grazes outside the exact
            # polygon by centimetres (corner tangency) and would fail
            # ``covers`` — 0.3 m forgives the graze without bridging
            # any real grass gap (notches are metres wide).
            gprep = prep(geom.buffer(0.3))
        except _GEOM_EXC:
            return None

        verts: List[Tuple[float, float]] = [pa, pb]
        rings = []
        for g in (geom.geoms if geom.geom_type == "MultiPolygon"
                  else [geom]):
            rings.append(g.exterior)
            rings.extend(g.interiors)
        for ring in rings:
            verts.extend((c[0], c[1]) for c in ring.coords[:-1])
        # dedupe + deterministic order; endpoints stay at 0/1
        seen = {}
        uniq: List[Tuple[float, float]] = []
        for v in verts[:2]:
            seen[(round(v[0], 2), round(v[1], 2))] = len(uniq)
            uniq.append(v)
        rest = sorted(set((round(v[0], 2), round(v[1], 2))
                          for v in verts[2:])
                      - set(seen))
        uniq.extend(rest)
        if len(uniq) > _MAX_VERTS:
            stride = (len(uniq) - 2) // (_MAX_VERTS - 2) + 1
            uniq = uniq[:2] + uniq[2::stride]
        n = len(uniq)

        # K-NEAREST sparsification: testing every pair is O(V²) covers
        # calls (~90 ms per query, 371 queries = the 33 s HECA enforce
        # overhead).  Visibility edges to the K nearest candidates keep
        # the path valid; any length overestimate is CONSERVATIVE (a
        # longer interior path = a looser band).
        K = 24
        adj: List[List[Tuple[int, float]]] = [[] for _ in range(n)]
        seen_e: set = set()
        for i in range(n):
            xi, yi = uniq[i]
            cand = sorted(
                ((math.hypot(xi - uniq[j][0], yi - uniq[j][1]), j)
                 for j in range(n) if j != i))[:K]
            for dd, j in cand:
                if dd < 1e-9:
                    continue
                ek = (i, j) if i < j else (j, i)
                if ek in seen_e:
                    continue
                seen_e.add(ek)
                try:
                    if gprep.covers(LineString([uniq[i], uniq[j]])):
                        adj[i].append((j, dd))
                        adj[j].append((i, dd))
                except _GEOM_EXC:
                    continue

        dist = [float("inf")] * n
        dist[0] = 0.0
        pq: List[Tuple[float, int]] = [(0.0, 0)]
        while pq:
            dcur, u = heapq.heappop(pq)
            if dcur > dist[u]:
                continue
            if u == 1:
                return dcur
            for (v, w) in adj[u]:
                nd = dcur + w
                if nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(pq, (nd, v))
        return None


def measure_from_polys(polys) -> Optional[InteriorPathMeasure]:
    """Build a measure from raw airside polygons (solver and validator
    both come through here — parity by sharing)."""
    keep = [p for p in (polys or ())
            if p is not None and not p.is_empty]
    if not keep:
        return None
    try:
        return InteriorPathMeasure(unary_union(keep))
    except _GEOM_EXC:
        return None
