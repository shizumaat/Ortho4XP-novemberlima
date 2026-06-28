"""Top-level orchestration for the one-profile elevation solve.

``solve_route_profile`` (docs/one_profile_solve.md) is the ONLY pass that sets
airside elevations.  It reuses the proven, elevation-neutral primitives from
``solver_primitives`` (node list, seed, DEM sample, shape grade graph, level
coupling, writeback) and the route-feasibility building levels from
``building_feasibility`` — then runs the single :func:`one_profile_solve`
(one graph: the taxi-route reach band) over them.

Wiring (``solver.solve`` dispatches here unconditionally):

    nodes/seed/dem  →  reach band + building seats  →  one solve  →  writeback
"""
from __future__ import annotations

import os as _os
import time as _time

from .anchors import (
    apron_body_nodes, build_building_seats, building_spine_floor, node_bands,
    reach_band_for)
from .one_solve import one_profile_solve


def solve_route_profile(layout, icao: str,
                        dem=None, tile_lat: int = 0, tile_lon: int = 0) -> None:
    """Run the one-profile solve and write elevations back onto ``layout``.

    Mutates ``layout`` in place (rect ``altitude_high``/``altitude_low``,
    junction/apron ``node_altitudes``, terminal ``altitude``).  Runway segments
    (HARD anchors) are left untouched.
    """
    from auto_patch.elevation_per_surface.solver_primitives import (
        _build_node_list, _build_shape_constraints, _build_level_coupling,
        _runway_node_set, _sample_node_dem, _seed_elevations, _writeback,
        _report,
    )

    t0 = _time.time()
    nodes, bucket_to_idx = _build_node_list(layout)
    if not nodes:
        return

    elev, base_hard, _have_initial = _seed_elevations(
        layout, nodes, bucket_to_idx, dem=dem,
        tile_lat=tile_lat, tile_lon=tile_lon)
    if not any(base_hard):
        return

    dem_elev = _sample_node_dem(layout, nodes, dem, tile_lat, tile_lon)
    runway_nodes = _runway_node_set(layout, bucket_to_idx)

    # The within-shape grade graph (per-edge cap budgets + rect flat-end pairs)
    # and the rigid flat-across-width coupling — both elevation-neutral.
    shape_constraints = _build_shape_constraints(layout, bucket_to_idx)
    coupling = _build_level_coupling(shape_constraints)

    # ── THE ONE GRAPH (user 2026-06-27) ──────────────────────────────────────
    # Build the unified grade graph ONCE, FIRST — it is the single graph the reach
    # band, the building seats, the spine solve AND the validator all use.  The
    # reach band is now computed ON it (``reach_band_unified``): reachability is a
    # cap-Dijkstra over ``G.spine_adj`` from ``G.runway_anchor``, so the ceiling is
    # the spine's ACHIEVABLE level and cap-consistent by construction — no separate
    # route graph, no ``spine_adjacency`` re-derivation, no ceiling-consistency
    # bridge.  ``G.spine_adj`` already covers every spine node + edge the old
    # ``spine_adjacency`` produced (verified redundant), so the merge is gone.
    from auto_patch import grade_graph as _GG
    G = _GG.build_unified_graph(layout, bucket_to_idx)
    u_spine_adj = G.spine_adj
    band, dem_fn, runway_pts, _G = reach_band_for(
        layout, elev, bucket_to_idx, dem, tile_lat, tile_lon, unified_graph=G)
    node_band = node_bands(nodes, band)
    building_seats = build_building_seats(
        layout, bucket_to_idx, band, dem_fn, runway_pts)
    apron_body = apron_body_nodes(layout, bucket_to_idx)

    # NO-BUILDING APRON FILL (user 2026-06-26): a no-building apron has no pad to
    # anchor it, so where the DEM is wrong-low it sags below the level its feeder
    # taxiways can reach.  Seat each such apron FLAT at the closest-DEM level
    # reachable from ALL its routes (filled above the bad DEM) so the taxiways
    # grade smoothly from their runway anchors to it.  Treated like building seats
    # (heaviest anchors); spine nodes that cross the apron keep their taxi grade.
    # NO-BUILDING APRON FILL (user 2026-06-26): the closest-DEM level reachable
    # from all routes, per no-building apron.  Applied below as a per-node FLOOR
    # (raising node_band) so a no-building apron can't sag below its reachable
    # level into a wrong-low DEM pit, while still rising to follow a higher local
    # network (so it does not drag a high-route junction down).

    # ── THE ONE GRAPH (user 2026-06-26, docs/goal_merge_one_graph.md) ─────────
    # Solve the spine DIRECTLY on the geometry nodes the validator checks
    # (``grade_graph.build_unified_graph``) — there is no separate route graph and
    # no read-by-index bridge.  The spine is smoothed (never hard-frozen at an
    # over-cap profile value), runway-adjacent geometry nodes anchor at their OWN
    # LOCAL runway elevation, and a final feasibility-projection drives every
    # grade-graph edge ≤cap (only edges between two hard anchors — runway/building
    # — are left, the genuine steps).  This is what makes the validator's spine
    # zero: build and validate use the exact same nodes.
    if True:
        from .one_solve import feasibility_project
        # G + u_spine_adj already built above (before seating).
        n = len(elev)
        # Runway anchors: every geometry node a taxi spine joins the runway at is
        # HARD at the LOCAL runway elevation (the single hard anchor; the building
        # floor yields).  Never override an existing CIFP/seam hard value (it IS
        # the local runway surface there).
        # Anchor the node the validator's runway-join picks (nearest to each
        # taxi-centerline runway contact) at the LOCAL runway elevation — even if
        # it is already a hard runway node: at a runway INTERSECTION the crossing
        # node sits at a compromise between the two runways (694.8), but the taxi
        # that contacts ONE of them must meet THAT runway's surface (695.3).  ``re``
        # is the runway profile, so this is a no-op for a true runway-end node and
        # only corrects the intersection-compromised crossing node.
        for i, re in G.runway_anchor.items():
            if i < n:
                elev[i] = float(re)
                base_hard[i] = True
        u_spine_nodes = set(u_spine_adj) | G.spine_nodes()
        # Building-frontage spine floor (the serving arm climbs to its pads),
        # cap-Lipschitz on the unified spine chain.
        u_spine_floor = building_spine_floor(
            layout, nodes, bucket_to_idx, building_seats, node_band, u_spine_adj)
        # A building seat that IS a spine node (a pad node on a taxi centerline)
        # is anchored at its ACTUAL seat level DURING the spine solve — so the
        # spine grades its neighbours to within cap of the building (buildings are
        # heaviest).  Otherwise the spine grades to the softer floor (715.35) and
        # PHASE B then slams the seat to its real level (715.63), breaking the cap
        # to the neighbour (the 5.4% junction).  The seat and the spine now agree.
        for i, lv in building_seats.items():
            if i < n and lv is not None and i in u_spine_adj:
                elev[i] = float(lv)
                base_hard[i] = True

        # SEAM SPINE ANCHORS (user 2026-06-28): where a taxi centerline crosses a
        # tile seam, pin the nearest SPINE node to the SMOOTHED seam DEM as a HARD
        # anchor — so the spine solve below SPREADS the route→seam drop along the
        # centerline (≤cap over its length) instead of leaving the spine at the
        # plateau level and the body cliffing to the seam.  The seam is terrain-
        # pinned for cross-tile stitching; both tiles' route reaches the same seam
        # value → no cross-tile cliff AND no within-apron cliff.  Wires the
        # otherwise-dead ``SEAM_FIELD_ANCHORS`` concept onto the unified graph.
        from auto_patch.config import SEAM_FIELD_ANCHORS
        _cut_lines = getattr(layout, "_seam_cut_lines", None) or []
        if SEAM_FIELD_ANCHORS and dem is not None and _cut_lines:
            _seam_spine_anchors(layout, G, u_spine_adj, elev, base_hard,
                                dem, tile_lat, tile_lon, _cut_lines)

        # PHASE A — dedicated SMOOTH spine solve on the unified graph (geometry
        # nodes), runway/seam HARD at their LOCAL value, building floors honoured.
        # The spine is min-curvature and ≤cap by construction, then FROZEN so the
        # body grades to it (the body twists to meet the spine, never the reverse).
        frozen = _solve_spine_profile(
            elev, base_hard, u_spine_adj, u_spine_floor, node_band)
        for i in frozen:
            if i < n:
                base_hard[i] = True

        # Seat every sloping taxi RECT as a flat-ended tilted plane (read from the
        # solved spine), freezing its corners so the body grades to it.  Returns
        # each rect's plane (end node indices) for the final cap re-stamp.
        rect_planes = _flatten_rect_ends(
            layout, bucket_to_idx, elev, base_hard, frozen)

        # PHASE B — body fill (apron/junction interiors + rect bodies + caps) with
        # the spine frozen.  Apron body = 1% VISIBILITY/GEODESIC smoothing within
        # the reach band [floor, ceiling] (apron_smooth=True) — graded ≤1% from its
        # anchored edges/spine, NOT draped on raw DEM bumps (user 2026-06-26).  The
        # band still fills it to the reachable level (west apron → ~693).
        n_free = one_profile_solve(
            elev, shape_constraints, base_hard, nodes, dem_elev,
            runway_nodes, building_seats, apron_body, u_spine_nodes, u_spine_adj,
            node_band, u_spine_floor, coupling, apron_smooth=True)
        # Guarantee compliance: project EVERY grade-graph edge ≤cap with the
        # spine + runway + buildings + seams HARD; only the apron/junction body
        # flexes.  Edges left over cap have both ends hard = genuine steps.
        hard = {i for i in range(n) if base_hard[i]}
        hard |= {i for i in runway_nodes if i < n}
        hard |= {i for i in building_seats if i < n}
        rem, bh = feasibility_project(elev, shape_constraints, hard)
        # Project on the UNIFIED graph's OWN edges too (the EXACT pairs/caps the
        # validator checks — rects/caps all-pair, which shape_constraints only
        # approximates with axial edges), so build and validate cannot leave a
        # residual between them.  The spine stays HARD; only body nodes flex.
        u_edges = [(a, b, cap.at(_GG._dist(G.pos.get(a), G.pos.get(b)), 0.0))
                   for (a, b, cap, _sp) in G.edges
                   if a in G.pos and b in G.pos]
        rem, bh = feasibility_project(elev, [{"edges": u_edges}], hard)
        # FINAL re-stamp: continue each end-cap as a planar extension of its
        # parent rect's FINAL plane (rect ends may have flexed in feasibility),
        # skipping any cap corner the spine already owns — done LAST so nothing
        # moves it (the route-graph path's _restamp_caps, on geometry nodes).
        _restamp_caps_unified(layout, bucket_to_idx, elev, rect_planes, frozen)
        # GROUNDSIDE REACH + MOUTH WELD (user 2026-06-27).  Done LAST — after the
        # body solve + feasibility project — so buildings + aprons are anchored:
        #   1. Re-level each groundside piece a service road connects to an apron, to
        #      the elevation the connector can REACH within the service-road grade
        #      cap (apron mouth ± cap·route_len, clamped toward DEM) — so the
        #      connector grades <=cap instead of ramping steeply to the raw DEM.  A
        #      piece with no apron-connected service road stays DEM.
        #   2. Weld each service-road connector mouth to the (re-levelled) groundside
        #      altitude, so connector and groundside emit as ONE node (no cliff).
        # Gate off → elev + groundside untouched → byte-identical.
        if _os.environ.get("O4_GROUNDSIDE_MOUTH_ANCHOR", "1") == "1":
            from auto_patch.config import SERVICE_ROAD_MAX_GRADE
            from .anchors import apply_groundside_reach
            _nrl, _gs_hard = apply_groundside_reach(
                layout, bucket_to_idx, elev, SERVICE_ROAD_MAX_GRADE)
            if _gs_hard:
                # The truck route (apron arm + connector + groundside mouth) is now
                # pinned on its rising <=cap profile; re-project so the apron BODY
                # grades into the raised arm and nothing else exceeds its cap.
                _ghard = hard | {i for i in runway_nodes if i < n} | _gs_hard
                feasibility_project(elev, shape_constraints, _ghard)
                feasibility_project(elev, [{"edges": u_edges}], _ghard)
            # Service roads FOLLOW DEM at <=cap (a ground road climbs toward terrain,
            # anchored only at its airside/groundside welds) — SVC4 was held flat in
            # the bowl ~6-11 m below DEM.
            from .anchors import apply_service_road_dem_follow
            _svc_moved = apply_service_road_dem_follow(
                layout, bucket_to_idx, elev, dem_elev, SERVICE_ROAD_MAX_GRADE,
                anchor_extra=_gs_hard)
            if (_nrl or _svc_moved) and _os.environ.get("O4_STEP_DEBUG") == "1":
                print(f"  [groundside-reach] {icao}: re-levelled {_nrl} "
                      f"groundside piece(s); pinned {len(_gs_hard)} route node(s); "
                      f"DEM-followed {len(_svc_moved)} service node(s).")
        n_terms, n_rects, n_juncs = _writeback(layout, elev, bucket_to_idx)
        if _os.environ.get("O4_STEP_DEBUG") == "1":
            print(f"  [unified] {icao}: {len(frozen)} spine node(s) solved, "
                  f"{n_free} body node(s); feasibility-project → {rem} edge(s) "
                  f"over cap ({bh} both-hard = genuine).")
        _report(icao, n_free, n_free, _time.time() - t0,
                n_terms, n_rects, n_juncs)
        return


def _open4(poly):
    c = list(poly.exterior.coords)
    return c[:-1] if c and c[0] == c[-1] else c


def _flatten_rect_ends(layout, bucket_to_idx, elev, base_hard, frozen_spine):
    """Make each sloping taxi rect a FLAT-ENDED tilted plane: both corners of each
    short end take that end's solved-spine elevation (mean over the corners the
    spine solve actually set), so the rect emits as a clean plane (the validator
    checks all-pair).  Freezes the corners.  Returns each rect's plane as
    ``(corner_idx_set, (e0x,e0y), e0_node, (e1x,e1y), e1_node)`` for the final cap
    re-stamp (the node indices let the cap re-read the rect's FINAL ends)."""
    import math
    from auto_patch.junction_rules import SLOPING_RECT_ROLES
    cps = layout.canonical_points
    n = len(elev)

    def _idx(x, y):
        return bucket_to_idx.get(cps.get_or_add(float(x), float(y)))

    from auto_patch.grade_graph import _rect_ends
    planes = []
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        coords = _open4(s.polygon)
        if len(coords) < 4:
            continue
        endA, endB, _ext = _rect_ends(coords)
        if endA is None:
            continue
        ends_mid, end_node, ckeys, ok = [], [], set(), True
        for grp in (endA, endB):
            cis = [_idx(*coords[k]) for k in grp]
            if any(c is None or c >= n for c in cis):
                ok = False
                break
            solved = [c for c in cis if c in frozen_spine]
            if not solved:
                ok = False
                break
            ez = sum(elev[c] for c in solved) / len(solved)
            for c in cis:
                elev[c] = ez            # flatten the whole end
                ckeys.add(c)
            mx = sum(coords[k][0] for k in grp) / len(grp)
            my = sum(coords[k][1] for k in grp) / len(grp)
            ends_mid.append((mx, my))
            end_node.append(cis[0])
        if ok and len(ends_mid) == 2:
            planes.append((ckeys, ends_mid[0], end_node[0],
                           ends_mid[1], end_node[1]))
    return planes


def _restamp_caps_unified(layout, bucket_to_idx, elev, rect_planes, frozen_spine):
    """Continue each end-cap as a planar extension of its parent rect's FINAL
    plane (matched by ≥2 shared corners), set LAST so nothing moves it.  A cap
    corner the SPINE owns (``frozen_spine`` — a junction node) is left untouched:
    the spine is paramount, the cap yields there.  Mutates ``elev`` in place."""
    cps = layout.canonical_points
    n = len(elev)

    def _idx(x, y):
        return bucket_to_idx.get(cps.get_or_add(float(x), float(y)))

    for s in layout.shapes:
        if (not getattr(s, "is_rect_cap", False) or s.polygon is None
                or s.polygon.is_empty):
            continue
        coords = _open4(s.polygon)
        if len(coords) < 3:
            continue
        ckeys = {_idx(x, y) for (x, y) in coords}
        best, best_sh = None, 1
        for pl in rect_planes:
            sh = len(pl[0] & ckeys)
            if sh > best_sh:
                best_sh, best = sh, pl
        if best is None:
            continue
        _ck, e0, e0n, e1, e1n = best
        z0 = elev[e0n] if e0n < n else None
        z1 = elev[e1n] if e1n < n else None
        if z0 is None or z1 is None:
            continue
        ax, ay = e1[0] - e0[0], e1[1] - e0[1]
        L2 = ax * ax + ay * ay
        if L2 < 1e-9:
            continue
        for (x, y) in coords:
            ci = _idx(x, y)
            if ci is None or ci >= n or ci in frozen_spine:
                continue
            t = ((x - e0[0]) * ax + (y - e0[1]) * ay) / L2
            elev[ci] = z0 + t * (z1 - z0)


def _seam_spine_anchors(layout, G, spine_adj, elev, base_hard,
                        dem, tile_lat, tile_lon, cut_lines):
    """Pin the nearest SPINE node to each taxi-centerline × tile-seam crossing at
    the SMOOTHED seam DEM (HARD), so ``_solve_spine_profile`` grades the route
    DOWN to the seam over the centerline length instead of leaving the spine at
    the plateau level (the SPLP tile-77 seam: spine stuck ~74.6, seam 72.2 → the
    apron body cliffed).  Returns the count pinned."""
    from shapely.geometry import Point          # noqa: F401  (geom predicates)
    from auto_patch.elevation import _sample_dem
    n = len(elev)
    spine_pts = [(i, G.pos[i]) for i in spine_adj if i in G.pos and i < n]
    if not spine_pts:
        return 0
    pinned = 0
    seen: set = set()
    for entry in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = entry[0] if isinstance(entry, (tuple, list)) else entry
        if ln is None or ln.is_empty:
            continue
        for cut in cut_lines:
            try:
                inter = ln.intersection(cut)
            except Exception:                              # pragma: no cover
                continue
            if inter.is_empty:
                continue
            pts = ([inter] if inter.geom_type == "Point"
                   else [g for g in getattr(inter, "geoms", [])
                         if g.geom_type == "Point"])
            for p in pts:
                bi, (bx, by) = min(
                    spine_pts,
                    key=lambda t: (t[1][0] - p.x) ** 2 + (t[1][1] - p.y) ** 2)
                if bi in seen or (bx - p.x) ** 2 + (by - p.y) ** 2 > 30.0 ** 2:
                    continue
                lat, lon = layout.m_to_ll(bx, by)
                v = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
                if v is None or v != v:
                    continue
                elev[bi] = float(v)
                base_hard[bi] = True
                seen.add(bi)
                pinned += 1
    return pinned


def _solve_spine_profile(elev, base_hard, spine_adj, spine_floor,
                         node_band=None,
                         *, max_sweeps=5000, tol=1e-3, curvature=0.25):
    """Dedicated SMOOTH spine solve on the unified graph's geometry nodes.

    Min-curvature (inverse-budget² harmonic mean blended with the plain mean),
    clamped into the neighbour cap slabs ``[z_j − budget, z_j + budget]``, the
    building-frontage floor, AND the per-node REACH BAND ``node_band[i] =
    (floor, ceiling)`` (user 2026-06-26) — so the spine is closest-DEM-FEASIBLE
    too: it can't sit BELOW its reachable floor (CYXY TX3 at 677 when its floor is
    ~685) nor above its ceiling.  Anchors = the nodes already HARD (runway
    contacts at their LOCAL runway elevation + tile seams).  Mutates ``elev`` in
    place; returns the set of spine node indices it solved (to be frozen for the
    body fill)."""
    import math
    INF = float("inf")
    anchors = {i for i in spine_adj if i < len(base_hard) and base_hard[i]}
    nodes = [k for k in spine_adj if k < len(elev)]
    free = [k for k in nodes if k not in anchors]

    def _band(k):
        b = node_band[k] if (node_band is not None and k < len(node_band)) else None
        if b is None:
            return -INF, INF
        lo, hi = b
        return (lo, hi) if lo <= hi else (0.5 * (lo + hi), 0.5 * (lo + hi))

    # warm start free nodes onto their reach-band floor / serving floor (fill UP
    # out of a wrong-low DEM; the serving arm climbs to its pads).
    for k in free:
        bf, _bh = _band(k)
        f = spine_floor.get(k, -INF)
        target = max(bf, f)
        if target > -INF and target > elev[k]:
            elev[k] = target
    for _ in range(max_sweeps):
        moved = 0.0
        for k in free:
            nb = spine_adj.get(k, ())
            if not nb:
                continue
            sw = acc = 0.0
            for (j, w) in nb:
                wt = 1.0 / max(w, 1e-3) ** 2
                sw += wt
                acc += elev[j] * wt
            harm = acc / sw if sw > 0 else elev[k]
            pm = sum(elev[j] for (j, _w) in nb) / len(nb)
            tgt = (1.0 - curvature) * harm + curvature * pm
            lo, hi = _band(k)
            for (j, w) in nb:
                if elev[j] - w > lo:
                    lo = elev[j] - w
                if elev[j] + w < hi:
                    hi = elev[j] + w
            f = spine_floor.get(k)
            if f is not None and f > lo:
                lo = f
            tgt = (min(max(tgt, lo), hi) if lo <= hi else 0.5 * (lo + hi))
            d = tgt - elev[k]
            if d:
                elev[k] = tgt
                if abs(d) > moved:
                    moved = abs(d)
        if moved < tol:
            break
    # Final EXACT cap-Lipschitz projection on the spine edges (only the runway/
    # seam anchors are hard) — the Gauss-Seidel's harmonic compromise can leave a
    # ~cap residual where several centerlines meet at a junction node; this drives
    # every free↔free spine pair ≤cap (a both-anchor pair stays = genuine step).
    from .one_solve import feasibility_project
    s_edges = []
    seen = set()
    for i, lst in spine_adj.items():
        for (j, w) in lst:
            e = (i, j) if i < j else (j, i)
            if e in seen:
                continue
            seen.add(e)
            s_edges.append((e[0], e[1], w))
    feasibility_project(elev, [{"edges": s_edges}], anchors)
    return set(nodes)


def _merge_spine_adj(a, b):
    """Union two ``{i: [(j, budget), ...]}`` spine adjacencies (the apron/junction
    consecutive chain + the unified graph's rect-axis links), keeping ONE edge per
    pair (min budget = tightest cap)."""
    out: dict = {}
    seen: dict = {}
    for src in (a, b):
        for i, lst in src.items():
            for (j, w) in lst:
                e = (min(i, j), max(i, j))
                if e in seen:
                    if w < seen[e]:
                        seen[e] = w
                    continue
                seen[e] = w
    for (i, j), w in seen.items():
        out.setdefault(i, []).append((j, w))
        out.setdefault(j, []).append((i, w))
    return out
