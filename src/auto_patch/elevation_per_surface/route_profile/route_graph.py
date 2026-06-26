"""Route-profile solve ON THE SINGLE route graph (user 2026-06-25).

This solves the smooth, ≤cap elevation profile on ``shared_taxi_route_graph`` —
THE SAME graph (nodes, edges, per-letter caps) the reach band uses to set
building elevations.  There is exactly one graph: no new structure is built
here, and the runway contacts come from the same ``_runway_route_contacts`` the
band uses, so the route profile and the building levels are consistent by
construction.

The solve is pure min-curvature between the real anchors (runway contacts +
building-serving floors); NO DEM target and NO reach-band clamp on an
intermediate node — those are not anchors and would pull the route off the
smooth path the anchors define.  ``solve_route_graph`` returns the per-node
elevation ``z`` and the residual (edges still over cap = genuine anchor
conflicts, e.g. routes that meet at incompatible elevations).
"""
from __future__ import annotations

import heapq
import math

_INF = float("inf")


def enrich_route_graph(layout, nodes, spine_nodes):
    """Insert every geometry route node + each rect-end synthetic into the SINGLE
    route graph (``shared_taxi_route_graph``), as collinear nodes on the
    centerlines they already lie on.  Each inserted node just splits an existing
    edge, so every path length — and therefore the band's building levels — is
    UNCHANGED (runway contacts stay on their centerline vertices, which remain
    nodes).  Mutates the shared graph in place; returns:

      * ``geo_key``       — ``{spine_node_idx: graph_key}`` (read the profile by
        index, no sampling);
      * ``rect_end_keys`` — ``{id(rect_shape): (key_end0, key_end1)}`` the rect's
        two synthetic end nodes (for emitting its tilted plane).
    """
    from collections import defaultdict
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree
    from auto_patch.config import TAXI_MAX_GRADE, taxi_grade_cap_for_letter
    from auto_patch.junction_rules import SLOPING_RECT_ROLES
    from auto_patch.layout import taxi_shape_code_letter
    from auto_patch.taxi_routing import shared_taxi_route_graph

    G = shared_taxi_route_graph(layout)

    # geometry route points to weave in: spine nodes + rect-end axis midpoints.
    geo_pts = [(nodes[i][0], nodes[i][1], ("spine", i))
               for i in spine_nodes if i < len(nodes)]
    rect_info: dict = {}        # id(rect) -> (mid0, mid1, per-letter cap)
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            continue
        elens = [math.hypot(coords[(k + 1) % 4][0] - coords[k][0],
                            coords[(k + 1) % 4][1] - coords[k][1])
                 for k in range(4)]
        rcap = float(taxi_grade_cap_for_letter(taxi_shape_code_letter(layout, s)))
        rmids = [None, None]
        for ei, e in enumerate(sorted(range(4), key=lambda k: elens[k])[:2]):
            a, b = coords[e], coords[(e + 1) % 4]
            mx, my = 0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1])
            rmids[ei] = (mx, my)
            geo_pts.append((mx, my, ("rect", id(s), ei)))
        rect_info[id(s)] = (rmids[0], rmids[1], rcap)

    # original edges (the existing topology — NEVER add a new connection).
    orig_edges = []
    seen = set()
    for u, lst in G.adj.items():
        for (v, _w) in lst:
            e = (u, v) if u <= v else (v, u)
            if e not in seen:
                seen.add(e)
                orig_edges.append((u, v))
    elines = [LineString([G.coord[u], G.coord[v]]) for (u, v) in orig_edges]
    tree = STRtree(elines) if elines else None

    geo_key: dict = {}
    rect_end_keys: dict = {}

    def _rec(payload, key):
        if payload[0] == "spine":
            geo_key[payload[1]] = key
        else:
            rect_end_keys.setdefault(payload[1], [None, None])[payload[2]] = key

    # Assign each geometry point to its ONE nearest existing edge; snap to an
    # endpoint if within SNAP, else record an interior split at parameter t.
    SNAP = 0.10
    edge_splits = defaultdict(list)             # edge_idx -> [(t, payload, xy)]
    for (x, y, payload) in geo_pts:
        if tree is None:
            continue
        p = Point(x, y)
        best, bd = None, 3.0
        for qi in tree.query(p.buffer(3.0)):
            ei = int(qi)
            d = elines[ei].distance(p)
            if d < bd:
                bd, best = d, ei
        if best is None:
            continue
        u, v = orig_edges[best]
        (ax, ay), (bx, by) = G.coord[u], G.coord[v]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = (((x - ax) * dx + (y - ay) * dy) / L2) if L2 > 1e-12 else 0.0
        L = math.sqrt(L2)
        if t * L <= SNAP:
            _rec(payload, u)
        elif (1.0 - t) * L <= SNAP:
            _rec(payload, v)
        elif 0.0 < t < 1.0:
            edge_splits[best].append((t, payload, (ax + t * dx, ay + t * dy)))

    # Rebuild adjacency: keep every node/edge, but split an edge through its
    # interior points (UNIQUE keys → nothing merges across centerlines, so the
    # topology and every path length are preserved → band unchanged).
    new_adj: dict = {k: [] for k in G.coord}
    new_coord = dict(G.coord)
    # RETAIN the original direct-edge caps: the band's ``band(x, y)`` looks up
    # ``edge_cap[(kA, kB)]`` for two CONSECUTIVE centerline vertices, and once we
    # split that edge the direct entry would vanish → default 1.5 %, collapsing a
    # 3 % taxiway's budget (building16 dropped 6.4 m).  The Dijkstra only walks
    # the adjacency (the sub-edges), so keeping the stale direct entry is
    # harmless to it but keeps the band byte-identical.
    new_cap: dict = dict(G.edge_cap)
    # synthetic split-node keys are (int, int) — same type as G's quantised
    # coordinate keys (so ``_ekey``'s ``<=`` works) but far outside the coord
    # range, so they can never collide with a real node or each other.
    _RP_BASE = 10 ** 7
    _ctr = [0]

    def _add(a, b, c):
        (xa, ya), (xb, yb) = new_coord[a], new_coord[b]
        w = math.hypot(xb - xa, yb - ya)
        new_adj[a].append((b, w))
        new_adj[b].append((a, w))
        ek = (a, b) if a <= b else (b, a)
        prev = new_cap.get(ek)
        new_cap[ek] = c if prev is None else max(prev, c)

    MERGE = 2.0                                 # merge split points within 2 m
    for ei, (u, v) in enumerate(orig_edges):
        c = G.edge_cap.get((u, v) if u <= v else (v, u), TAXI_MAX_GRADE)
        chain = [u]
        last_xy = new_coord[u]
        for (t, payload, xy) in sorted(edge_splits.get(ei, []), key=lambda r: r[0]):
            if math.dist(xy, last_xy) < MERGE:  # coincides with the last node
                _rec(payload, chain[-1])        # → map to it, no tiny edge
                continue
            k = (_RP_BASE + _ctr[0], 0)         # unique (int,int) key
            _ctr[0] += 1
            new_coord[k] = xy
            new_adj[k] = []
            chain.append(k)
            last_xy = xy
            _rec(payload, k)
        if len(chain) > 1 and math.dist(new_coord[v], last_xy) < MERGE:
            # last split too close to the far endpoint → drop it onto v
            drop = chain.pop()
            for pl, kk in list(geo_key.items()):
                if kk == drop:
                    geo_key[pl] = v
            for rid, ks in rect_end_keys.items():
                ks[:] = [v if kk == drop else kk for kk in ks]
            new_coord.pop(drop, None)
            new_adj.pop(drop, None)
        chain.append(v)
        for a, b in zip(chain, chain[1:]):
            _add(a, b, c)

    G.adj.clear(); G.adj.update(new_adj)
    G.coord.clear(); G.coord.update(new_coord)
    G.edge_cap.clear(); G.edge_cap.update(new_cap)

    # COVERAGE GUARANTEE (user 2026-06-26): every rect must be a graph SEGMENT —
    # a flat-end node at each short-edge midpoint joined by the rect AXIS — even
    # when its centerline ends short of the rect (stub / cross-connector extend
    # past the apt.dat route → the ≤3 m snap above misses them) or the centerline
    # is absent (discovered TX taxiways).  Add any missing end node and the axis
    # edge so the rect reads its profile from the graph by index, never off-graph.
    def _add_edge(a, b, c):
        if a == b:
            return
        (xa, ya), (xb, yb) = G.coord[a], G.coord[b]
        w = math.hypot(xb - xa, yb - ya)
        G.adj.setdefault(a, []).append((b, w))
        G.adj.setdefault(b, []).append((a, w))
        ek = (a, b) if a <= b else (b, a)
        prev = G.edge_cap.get(ek)
        G.edge_cap[ek] = c if prev is None else max(prev, c)

    def _nearest_existing(px, py, maxd, exclude):
        best, bd = None, maxd * maxd
        for k, (cx, cy) in G.coord.items():
            if k in exclude:
                continue
            d = (cx - px) ** 2 + (cy - py) ** 2
            if d < bd:
                bd, best = d, k
        return best

    for rid, (m0, m1, rcap) in rect_info.items():
        if m0 is None or m1 is None:
            continue
        ends = rect_end_keys.setdefault(rid, [None, None])
        mids = [m0, m1]
        had = [ends[0] is not None, ends[1] is not None]   # already on-graph?
        for ei in (0, 1):
            if ends[ei] is None:                            # off-graph end → add
                k = (_RP_BASE + _ctr[0], 0)
                _ctr[0] += 1
                G.coord[k] = mids[ei]
                G.adj.setdefault(k, [])
                ends[ei] = k
        _add_edge(ends[0], ends[1], rcap)                   # the rect AXIS
        # Fully off-graph rect (e.g. discovered TX, centerline absent): bridge one
        # end into the network so it is reachable (a stopgap until the centerline
        # is folded in).  A rect with one end already on-graph is connected via the
        # axis, so it needs no extra (possibly wrong) proximity edge.
        if not any(had):
            nk = _nearest_existing(*mids[0], 40.0, {ends[0], ends[1]})
            if nk is not None:
                _add_edge(ends[0], nk, rcap)

    return geo_key, {rid: tuple(v) for rid, v in rect_end_keys.items()}


def solve_route_graph(layout, nodes, spine_nodes, runway_pts, dem_fn,
                      building_levels=None, *, max_sweeps=5000, tol=1e-3,
                      curvature=0.25):
    """Return ``(z, residual, geo_key, rect_end_keys)``.

    ``z`` — ``{graph_key: elevation}`` over every node of the (geometry-enriched)
    single route graph.  ``residual`` — ``[(over_m, i, j, dist, cap_budget, mx,
    my)]`` edges still over cap (worst first).  ``geo_key`` / ``rect_end_keys`` —
    the read-by-index maps from :func:`enrich_route_graph`.
    """
    from auto_patch.config import (APRON_MAX_GRADE, TAXI_MAX_GRADE,
                                   VISIBLE_CHORD_CONNECT,
                                   taxi_grade_cap_for_letter)
    from auto_patch.taxi_routing import shared_taxi_route_graph
    from auto_patch.elevation_per_surface.building_feasibility import (
        _runway_route_contacts, _nearest_visible_centerline,
        _pavement_visibility, building_feasible_levels, reach_band_sampler,
        _TAXI_HALF_W_M)
    from shapely.geometry import Point

    # Weave the completed geometry's route nodes + rect-end synthetics into the
    # SINGLE graph (distance-preserving → band unchanged) so the profile reads
    # back by index and the building feet land on real nearby nodes.
    geo_key, rect_end_keys = enrich_route_graph(layout, nodes, spine_nodes)

    G = shared_taxi_route_graph(layout)
    if not getattr(G, "coord", None) or not runway_pts:
        return {}, [], geo_key, rect_end_keys
    keys = list(G.coord)

    def cap(u, v):
        return G.edge_cap.get(G._ekey(u, v), TAXI_MAX_GRADE)

    # ── anchors: runway contacts (the SAME source the band uses) ──────────────
    anchors: dict = {}
    for (k, ae) in _runway_route_contacts(layout, G, runway_pts):
        anchors[k] = ae

    # ── building-serving floors, propagated cap-Lipschitz ON G ────────────────
    if building_levels is None:
        building_levels = building_feasible_levels(layout, runway_pts, dem_fn)
    cl_items = [(ln, n) for (ln, n) in (getattr(layout, "apt_taxi_centerlines",
                                                 None) or [])
                if ln is not None and not ln.is_empty
                and not str(n or "").upper().startswith("SVC")]
    clines = [ln for (ln, _n) in cl_items]
    ref_of = {id(ln): n for (ln, n) in cl_items}
    letters = getattr(layout, "apt_taxi_letters", None) or {}
    vis = _pavement_visibility(layout) if (VISIBLE_CHORD_CONNECT and clines) else None
    src: dict = {}
    src_ceil: dict = {}
    for s in layout.shapes:
        lv = building_levels.get(id(s))
        if lv is None or s.polygon is None or s.polygon.is_empty or not clines:
            continue
        c = s.polygon.centroid
        ln = (_nearest_visible_centerline(c, clines, vis) if vis is not None
              else min(clines, key=lambda L: L.distance(c)))
        foot = ln.interpolate(ln.project(c))
        fk, _ = G.nearest_key(foot.x, foot.y)
        if fk is None:
            continue
        # SAME perp-climb rule as the band (so the floor demands no more than the
        # level was set to need): the taxiway-corridor first 7.5 m at the route's
        # OWN cap, the rest at the apron 1 %.  A flat 1 % under-credits a 3 %
        # taxiway's corridor → over-demands the spine (building16 by 0.15 m → the
        # whole E residual).
        perp = c.distance(ln)
        ecap = float(taxi_grade_cap_for_letter(letters.get(ref_of.get(id(ln)))))
        perp_climb = (ecap * min(perp, _TAXI_HALF_W_M)
                      + APRON_MAX_GRADE * max(0.0, perp - _TAXI_HALF_W_M))
        # The apron grades ≤perp_climb either way, so the spine must sit WITHIN
        # ±perp_climb of the pad: a high building floors it (serve from below), a
        # building BELOW the runway ceils it (the apron must be able to DESCEND
        # to the low pad).  Floor = level − perp_climb (max wins), ceiling =
        # level + perp_climb (min wins).
        fl = float(lv) - perp_climb
        cl = float(lv) + perp_climb
        if fl > src.get(fk, -_INF):
            src[fk] = fl
        if cl < src_ceil.get(fk, _INF):
            src_ceil[fk] = cl

    def _propagate(sources, *, raise_floor):
        """Cap-Lipschitz spread of a per-foot demand over G.  ``raise_floor``:
        floor (max of source − capdist) vs ceiling (min of source + capdist)."""
        out: dict = {}
        sign = -1.0 if raise_floor else 1.0
        pq = [((-s if raise_floor else s), k) for k, s in sources.items()]
        heapq.heapify(pq)
        while pq:
            key, k = heapq.heappop(pq)
            t = -key if raise_floor else key
            cur = out.get(k)
            if cur is not None and (
                    (raise_floor and t <= cur) or (not raise_floor and t >= cur)):
                continue
            out[k] = t
            for (j, w) in G.adj.get(k, ()):
                nt = t + sign * cap(k, j) * w
                pj = out.get(j)
                if pj is None or (nt > pj if raise_floor else nt < pj):
                    heapq.heappush(pq, ((-nt if raise_floor else nt), j))
        return out

    bfloor = _propagate(src, raise_floor=True) if src else {}
    bceil = _propagate(src_ceil, raise_floor=False) if src_ceil else {}

    # A building's serving demand can never push a node OUTSIDE its own
    # reachability — the SAME band [floor, ceil] the band used to set the level.
    # Floor clamped to the band CEILING (can't demand above reachability — else a
    # building's demand spread onto a cross-taxiway it doesn't need over-demands
    # it, the E residual); ceiling clamped to the band FLOOR (can't demand below
    # the minimum reachable).  Clamps the DEMAND only — the route z is still free
    # to ride its anchors above/below the band (the band is not an anchor).
    if bfloor or bceil:
        _band = reach_band_sampler(layout, runway_pts)
        for k in set(bfloor) | set(bceil):
            bc = _band(*G.coord[k])
            if bc is None:
                continue
            if k in bfloor and bfloor[k] > bc[1]:
                bfloor[k] = bc[1]
            if k in bceil and bceil[k] < bc[0]:
                bceil[k] = bc[0]

    # ── solve: pure min-curvature, ≤cap, anchored + floored (NO DEM, NO band) ─
    z = {k: (anchors[k] if k in anchors else bfloor.get(k, 0.0)) for k in keys}
    free = [k for k in keys if k not in anchors]
    for _ in range(max_sweeps):
        moved = 0.0
        for k in free:
            nb = G.adj.get(k, ())
            if not nb:
                continue
            sw = acc = 0.0
            for (j, w) in nb:
                budget = cap(k, j) * max(w, 1e-3)
                wt = 1.0 / max(budget, 1e-3) ** 2
                sw += wt
                acc += z[j] * wt
            harm = acc / sw if sw > 0 else z[k]
            pm = sum(z[j] for (j, _w) in nb) / len(nb)
            tgt = (1.0 - curvature) * harm + curvature * pm
            n_lo, n_hi = -_INF, _INF
            for (j, w) in nb:
                budget = cap(k, j) * max(w, 1e-3)
                zj = z[j]
                if zj - budget > n_lo:
                    n_lo = zj - budget
                if zj + budget < n_hi:
                    n_hi = zj + budget
            lo_e = n_lo
            f = bfloor.get(k)
            if f is not None:
                lo_e = max(lo_e, f)
            hi_e = n_hi
            g = bceil.get(k)
            if g is not None:
                hi_e = min(hi_e, g)
            tgt = (min(max(tgt, lo_e), hi_e) if lo_e <= hi_e
                   else 0.5 * (lo_e + hi_e))
            d = tgt - z[k]
            if d:
                z[k] = tgt
                if abs(d) > moved:
                    moved = abs(d)
        if moved < tol:
            break

    residual = []
    seen = set()
    for k in keys:
        for (j, w) in G.adj.get(k, ()):
            e = (k, j) if k <= j else (j, k)
            if e in seen:
                continue
            seen.add(e)
            budget = cap(k, j) * max(w, 1e-3)
            ex = abs(z[k] - z[j]) - budget
            if ex > 1e-3:
                (xa, ya), (xb, yb) = G.coord[k], G.coord[j]
                residual.append((ex, k, j, w, budget,
                                 0.5 * (xa + xb), 0.5 * (ya + yb)))
    residual.sort(reverse=True)
    return z, residual, geo_key, rect_end_keys, bfloor
