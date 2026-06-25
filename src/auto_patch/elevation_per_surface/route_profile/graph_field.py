"""The graph-first route ELEVATION FIELD (user 2026-06-25).

The single source of airside elevation truth.  The taxi centerlines are
resampled into ONE dense, continuous graph (through junctions, aprons AND rects
— so the route is an unbroken 1-D path), and a single smooth profile is solved
on it: runway-route reachability bounds it (pinning runway contacts), the
building-serving climbs floor it, the free target is closest-to-DEM, and it is
min-curvature ≤cap along every edge.  Geometry then READS this field:

  * a point ON the route (rect end, cap, junction spine) reads the profile —
    so a rect, its cap, and the junction beyond all sample ONE continuous ≤cap
    curve and line up (co-planar, grade-legal) BY CONSTRUCTION;
  * a body point (apron / junction interior) reads ``body_z`` = closest-to-DEM
    within a band ``profile ± apron-cap·perp`` around the route.

Solving the route as an isolated 1-D min-curvature profile (not one constraint
among many in a 2-D relaxation) distributes every climb over the longest
available distance — the smoothest spine the geometry allows — and a pure read
stops the body from ever pulling the route back out of shape.
"""
from __future__ import annotations

import heapq
import math

_INF = float("inf")
_QTOL = 1.5            # m: centerline vertices within this coincide (junctions)
_STEP = 12.0          # m: dense resample step along each centerline


def _qkey(x, y):
    return (int(round(x / _QTOL)), int(round(y / _QTOL)))


def _build_dense_graph(layout):
    """Resample every (non-service) taxi centerline at ~``_STEP`` m into one
    connected graph: ``coord[k]=(x,y)``, ``adj[k]=[(j,len)]``, ``cap[ekey]``.

    CROSSINGS are made explicit shared nodes (user 2026-06-25): where two
    centerlines intersect — even mid-segment, with no shared vertex — both are
    forced to sample that exact point, so they quantise to ONE node and the
    crossing has ONE elevation by construction (else the two routes pass through
    the same point at different heights → a spine jump where geometry reads it)."""
    from auto_patch.config import taxi_grade_cap_for_letter
    letters = getattr(layout, "apt_taxi_letters", None) or {}
    coord: dict = {}
    adj: dict = {}
    cap: dict = {}

    def node(x, y):
        k = _qkey(x, y)
        if k not in coord:
            coord[k] = (float(x), float(y))
            adj[k] = []
        return k

    def edge(ka, kb, c):
        if ka == kb:
            return
        (xa, ya), (xb, yb) = coord[ka], coord[kb]
        w = math.hypot(xb - xa, yb - ya)
        adj[ka].append((kb, w))
        adj[kb].append((ka, w))
        ek = (ka, kb) if ka <= kb else (kb, ka)
        prev = cap.get(ek)
        cap[ek] = c if prev is None else max(prev, c)   # looser cap wins at joints

    lines = []                                          # (LineString, cap)
    for entry in (getattr(layout, "apt_taxi_centerlines", None) or []):
        ln = entry[0] if isinstance(entry, (tuple, list)) else entry
        ref = entry[1] if (isinstance(entry, (tuple, list)) and len(entry) > 1) else None
        if ln is None or ln.is_empty or str(ref or "").upper().startswith("SVC"):
            continue
        if ln.length > 1e-6:
            lines.append((ln, float(taxi_grade_cap_for_letter(letters.get(ref)))))

    # CROSSINGS: every pairwise centerline intersection point → a mandatory
    # sample arc on BOTH lines (so the two coincide at one quantised node).
    forced = [[] for _ in lines]                        # per-line: [arc positions]
    for i in range(len(lines)):
        li = lines[i][0]
        for j in range(i + 1, len(lines)):
            lj = lines[j][0]
            try:
                if not li.intersects(lj):
                    continue
                inter = li.intersection(lj)
            except Exception:                            # pragma: no cover
                continue
            geoms = (list(inter.geoms) if getattr(inter, "geom_type", "")
                     .startswith("Multi") or inter.geom_type == "GeometryCollection"
                     else [inter])
            for g in geoms:
                if getattr(g, "geom_type", "") != "Point":
                    continue                             # skip collinear overlaps
                forced[i].append(li.project(g))
                forced[j].append(lj.project(g))

    for (ln, c), arcs in zip(lines, forced):
        L = ln.length
        m = max(1, int(round(L / _STEP)))
        positions = sorted(set([L * s / m for s in range(m + 1)] + list(arcs)))
        prev_k = None
        for sp in positions:
            p = ln.interpolate(sp)
            k = node(p.x, p.y)
            if prev_k is not None:
                edge(prev_k, k, c)
            prev_k = k
    return coord, adj, cap


class RouteField:
    """Solved route profile + the band-derived body sampler."""

    def __init__(self, coord, adj, cap, elev):
        self._coord = coord
        self._elev = elev
        # edge list with endpoints + elevations, for nearest-edge sampling.
        self._edges = []
        seen = set()
        for k, nb in adj.items():
            for (j, _w) in nb:
                ek = (k, j) if k <= j else (j, k)
                if ek in seen:
                    continue
                seen.add(ek)
                (ax, ay), (bx, by) = coord[k], coord[j]
                self._edges.append((ax, ay, bx, by, elev[k], elev[j]))

    def _nearest(self, x, y):
        """(z_on_route, perp_dist) at the nearest graph edge."""
        best_d = _INF
        best_z = None
        for (ax, ay, bx, by, za, zb) in self._edges:
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            if L2 < 1e-12:
                t = 0.0
            else:
                t = ((x - ax) * dx + (y - ay) * dy) / L2
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
            px, py = ax + t * dx, ay + t * dy
            dd = (x - px) ** 2 + (y - py) ** 2
            if dd < best_d:
                best_d = dd
                best_z = za + t * (zb - za)
        return best_z, (math.sqrt(best_d) if best_d < _INF else _INF)

    def route_z(self, x, y):
        """Profile elevation at (x, y) — the value a route point reads."""
        z, _d = self._nearest(x, y)
        return z

    def body_z(self, x, y, dem):
        """Closest-to-DEM within the band ``route ± apron-cap·perp`` around the
        route — the value a body point reads."""
        from auto_patch.config import APRON_MAX_GRADE
        z, d = self._nearest(x, y)
        if z is None:
            return dem
        slack = APRON_MAX_GRADE * d
        lo, hi = z - slack, z + slack
        if dem is None:
            return z
        return min(max(dem, lo), hi)


def build_route_field(layout, runway_pts, dem_fn,
                      *, max_sweeps=2000, tol=1e-3, curvature=0.25):
    """Solve the dense route profile and return a :class:`RouteField`."""
    from auto_patch.config import APRON_MAX_GRADE, VISIBLE_CHORD_CONNECT
    from auto_patch.layout import ROLE_BUILDING
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_sampler, _nearest_visible_centerline, _pavement_visibility)

    coord, adj, cap = _build_dense_graph(layout)
    if not coord:
        return None
    band = reach_band_sampler(layout, runway_pts)
    keys = list(coord)

    def ecap(a, b):
        return cap.get((a, b) if a <= b else (b, a), APRON_MAX_GRADE)

    floor: dict = {}
    ceil: dict = {}
    dem: dict = {}
    for k in keys:
        cx, cy = coord[k]
        b = band(cx, cy)
        if b is not None:
            lo, hi = b
            if lo > hi:
                lo = hi = 0.5 * (lo + hi)
            floor[k], ceil[k] = lo, hi
        dem[k] = dem_fn(cx, cy)

    # building-serving floors at the foot node, propagated cap-Lipschitz.
    clines = [ln for (ln, n) in (getattr(layout, "apt_taxi_centerlines", None)
                                 or [])
              if ln is not None and not ln.is_empty
              and not str(n or "").upper().startswith("SVC")]
    vis = _pavement_visibility(layout) if (VISIBLE_CHORD_CONNECT and clines) else None
    src: dict = {}
    # The serving target per building is its frontage-reachable level (centroid
    # band ceiling, closest-to-DEM — same as build_building_seats); the spine
    # node nearest the building's foot must reach ``level − apron·perp`` to
    # serve the pad, propagated cap-Lipschitz along the route below.
    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty or not clines):
            continue
        c = s.polygon.centroid
        bc = band(c.x, c.y)
        if bc is None:
            continue
        de = dem_fn(c.x, c.y)
        lv = min(de, bc[1]) if de is not None else bc[1]
        ln = (_nearest_visible_centerline(c, clines, vis) if vis is not None
              else min(clines, key=lambda L: L.distance(c)))
        foot = ln.interpolate(ln.project(c))
        fk = _qkey(foot.x, foot.y)
        if fk not in coord:
            fk = min(keys, key=lambda k: (coord[k][0] - foot.x) ** 2
                     + (coord[k][1] - foot.y) ** 2)
        t = lv - APRON_MAX_GRADE * c.distance(ln)
        if t > src.get(fk, -_INF):
            src[fk] = t
    if src:
        pq = [(-t, k) for k, t in src.items()]
        heapq.heapify(pq)
        bfl: dict = {}
        while pq:
            negt, k = heapq.heappop(pq)
            t = -negt
            if t <= bfl.get(k, -_INF):
                continue
            bfl[k] = t
            for (j, w) in adj.get(k, ()):
                nt = t - ecap(k, j) * w
                if nt > bfl.get(j, -_INF):
                    heapq.heappush(pq, (-nt, j))
        for k, f in bfl.items():
            hi = ceil.get(k, _INF)
            floor[k] = max(floor.get(k, -_INF), min(f, hi))

    def target(k):
        lo, hi = floor.get(k, -_INF), ceil.get(k, _INF)
        d = dem.get(k)
        if lo > hi:
            return 0.5 * (lo + hi)
        if d is None:
            return 0.5 * (lo + hi) if (lo > -_INF and hi < _INF) else 0.0
        return min(max(d, lo), hi)

    elev = {k: target(k) for k in keys}
    for _ in range(max_sweeps):
        moved = 0.0
        for k in keys:
            nb = [(j, ecap(k, j) * w) for (j, w) in adj.get(k, ()) if w > 1e-9]
            if not nb:
                continue
            sw = acc = 0.0
            for (j, b) in nb:
                wt = 1.0 / max(b, 1e-3) ** 2
                sw += wt
                acc += elev[j] * wt
            harm = acc / sw if sw > 0 else elev[k]
            pm = sum(elev[j] for (j, _b) in nb) / len(nb)
            tgt = (1.0 - curvature) * harm + curvature * pm
            tgt = 0.5 * tgt + 0.5 * target(k)
            n_lo, n_hi = -_INF, _INF
            for (j, b) in nb:
                ej = elev[j]
                if ej - b > n_lo:
                    n_lo = ej - b
                if ej + b < n_hi:
                    n_hi = ej + b
            lo_e = max(n_lo, floor.get(k, -_INF))
            hi_e = min(n_hi, ceil.get(k, _INF))
            tgt = (min(max(tgt, lo_e), hi_e) if lo_e <= hi_e
                   else 0.5 * (lo_e + hi_e))
            d = tgt - elev[k]
            if d:
                elev[k] = tgt
                if abs(d) > moved:
                    moved = abs(d)
        if moved < tol:
            break

    return RouteField(coord, adj, cap, elev)
