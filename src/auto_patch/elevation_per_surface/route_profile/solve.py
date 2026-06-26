"""Top-level orchestration for the one-profile elevation solve.

``solve_route_profile`` is the next-gen replacement for ``unified_jacobi.solve``
(docs/one_profile_solve.md).  It is the ONLY pass that sets airside elevations:
the legacy multi-pass cascade is bypassed entirely.  It reuses the proven,
elevation-neutral primitives from ``unified_jacobi`` (node list, seed, DEM
sample, shape grade graph, level coupling, writeback) and the route-feasibility
building levels from ``building_feasibility`` — then runs the single
:func:`one_profile_solve` (one graph: the taxi-route reach band) over them.

Wiring (``solver.solve`` dispatches here behind ``O4_ROUTE_PROFILE_SOLVE``):

    nodes/seed/dem  →  reach band + building seats  →  one solve  →  writeback
"""
from __future__ import annotations

import os as _os
import time as _time

from .anchors import (
    apron_body_nodes, build_building_seats, building_spine_floor, node_bands,
    reach_band_for)
from .one_solve import one_profile_solve
from .spine import spine_adjacency


def solve_route_profile(layout, icao: str,
                        dem=None, tile_lat: int = 0, tile_lon: int = 0) -> None:
    """Run the one-profile solve and write elevations back onto ``layout``.

    Mutates ``layout`` in place (rect ``altitude_high``/``altitude_low``,
    junction/apron ``node_altitudes``, terminal ``altitude``).  Runway segments
    (HARD anchors) are left untouched.
    """
    from auto_patch.elevation_per_surface.unified_jacobi import (
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

    # THE ONE GRAPH: the taxi-route reach band sets the building levels AND
    # bounds every apron / spine / rect node, so they agree by construction.
    band, dem_fn, runway_pts = reach_band_for(
        layout, elev, bucket_to_idx, dem, tile_lat, tile_lon)
    node_band = node_bands(nodes, band)
    building_seats = build_building_seats(
        layout, bucket_to_idx, band, dem_fn, runway_pts)
    apron_body = apron_body_nodes(layout, bucket_to_idx)
    # The taxi-spine sub-graph (centerline-consecutive nodes) — spine nodes clamp
    # only to these, so the apron yields to the spine and the spine stays ≤cap.
    spine_nodes, spine_adj = spine_adjacency(layout, nodes, bucket_to_idx)

    # ── GRAPH-FIRST PURE-READ (gated, user 2026-06-25) ───────────────────────
    # Solve ONE continuous route profile on the dense centerline graph, then
    # every geometry node READS it (route nodes → the profile; body nodes →
    # closest-DEM within the spine band; buildings → their seat).  No per-shape
    # relaxation, so nothing pulls on anything — rect/cap/junction-spine line up
    # by construction.
    if _os.environ.get("O4_RP_GRAPH_FIELD", "0") == "1":
        from .graph_field import build_route_field
        field = build_route_field(layout, runway_pts, dem_fn)
        if field is not None:
            # ROUTE skeleton = pure read: anchor every rect/cap/spine node at the
            # profile, so the route is fixed and nothing can pull it.  Buildings
            # keep their seat (heaviest).  Then the existing solve fills ONLY the
            # body (apron / junction interiors) between those fixed anchors — a
            # consistent body fill, but the route can't be dragged out of shape.
            route_set = _route_node_set(layout, bucket_to_idx, spine_nodes)
            anchors = dict(building_seats)
            for i in route_set:
                if i in anchors or base_hard[i] or i in runway_nodes:
                    continue
                z = field.route_z(*nodes[i])
                if z is not None:
                    anchors[i] = float(z)
            n_free = one_profile_solve(
                elev, shape_constraints, base_hard, nodes, dem_elev,
                runway_nodes, anchors, apron_body, spine_nodes, spine_adj,
                node_band, {}, coupling)
            n_terms, n_rects, n_juncs = _writeback(layout, elev, bucket_to_idx)
            if _os.environ.get("O4_STEP_DEBUG") == "1":
                print(f"  [graph-field] {icao}: {len(route_set)} route node(s) "
                      f"read from the profile, {n_free} body node(s) filled.")
            _report(icao, n_free, n_free, _time.time() - t0,
                    n_terms, n_rects, n_juncs)
            return

    # ── SINGLE-GRAPH ROUTE SKELETON → BODY FILL (user 2026-06-25) ─────────────
    # PHASE A: solve the route SKELETON (spine + sloping-rect ends) on the SINGLE
    # taxi-route graph (``route_graph.solve_route_graph``, zero over-cap residual),
    # then apply it to geometry as a PURE READ BY INDEX — freezing every spine node
    # and rect corner HARD.  PHASE B: the apron / junction BODY interiors are then
    # filled by ``one_profile_solve`` on the existing within-shape grade graph with
    # the skeleton frozen (so the body can only grade TO the route, never drag it),
    # min-curvature for BOTH aprons and junctions (user 2026-06-25).
    if _os.environ.get("O4_RP_ROUTE_GRAPH", "1") == "1":
        from .route_graph import solve_route_graph
        from .one_solve import feasibility_project
        z, residual, geo_key, rect_end_keys, _bfloor = solve_route_graph(
            layout, nodes, spine_nodes, runway_pts, dem_fn)
        if z:
            flex = float(_os.environ.get("O4_RP_SKEL_FLEX", "2.0"))
            cap_records = _seed_route_skeleton(
                layout, nodes, bucket_to_idx, elev, node_band, z,
                geo_key, rect_end_keys, flex, base_hard)
            # Body fill: min-curvature; the skeleton is HARD (the smooth spine is
            # protected — the body twists to meet it, never the reverse).
            n_free = one_profile_solve(
                elev, shape_constraints, base_hard, nodes, dem_elev,
                runway_nodes, building_seats, apron_body, spine_nodes, spine_adj,
                node_band, {}, coupling, apron_smooth=True)
            # Guarantee compliance: project EVERY grade-graph edge ≤cap with the
            # skeleton + buildings + runway + seams hard, so ONLY apron/junction
            # body nodes move (they twist within their caps to meet the spine).
            # Edges left over cap with both ends hard are GENUINELY infeasible
            # (a flat pad the smooth route cannot reach at cap) — reported, not
            # forced.  Then re-stamp caps onto each rect's final plane.
            hard = {i for i in range(len(elev)) if base_hard[i]}
            hard |= {i for i in runway_nodes if i < len(elev)}
            hard |= {i for i in building_seats if i < len(elev)}
            rem, bh = feasibility_project(elev, shape_constraints, hard)
            _restamp_caps(elev, cap_records)
            if _os.environ.get("O4_RP_DEBUG_STASH") == "1":
                try:
                    from auto_patch.taxi_routing import shared_taxi_route_graph
                    _G = shared_taxi_route_graph(layout)
                    layout._rg_debug = {
                        "z": dict(z), "geo_key": dict(geo_key),
                        "rect_end_keys": dict(rect_end_keys),
                        "residual": list(residual),
                        "zc": [(_G.coord[k][0], _G.coord[k][1], z[k])
                               for k in z if k in _G.coord],
                        "adj": {k: [j for (j, _w) in lst]
                                for k, lst in _G.adj.items()},
                        "coord": dict(_G.coord),
                        "runway_pts": list(runway_pts)}
                except (AttributeError, TypeError, KeyError):
                    pass
            n_terms, n_rects, n_juncs = _writeback(layout, elev, bucket_to_idx)
            if _os.environ.get("O4_STEP_DEBUG") == "1":
                print(f"  [route-graph] {icao}: profile residual={len(residual)}, "
                      f"{n_free} body node(s); feasibility-project → {rem} edge(s) "
                      f"still over cap ({bh} between two hard anchors = genuine).")
            _report(icao, n_free, n_free, _time.time() - t0,
                    n_terms, n_rects, n_juncs)
            return

    # RECT-END PROFILE NODES (user 2026-06-25): add a VIRTUAL node to the
    # elevation graph at each rect end (NOT to the geometry — the rect shape is
    # untouched), inserted into the spine chain between the junction-spine and the
    # cap-spine.  The rect's end corners couple to the virtual node AT THEIR OWN
    # end (≈0 m away on the axis → equal is correct, flat across width), so the
    # rect tilts between two points exactly ON the profile and its cap is
    # co-planar by construction.  (Coupling to the cap-spine 12 m away was the
    # earlier bug → spurious cap·12 m steps.)
    if _os.environ.get("O4_RP_RECT_BRIDGE", "0") == "1":
        _add_rect_end_nodes(layout, nodes, bucket_to_idx, elev, dem_elev,
                            node_band, base_hard, shape_constraints,
                            spine_nodes, spine_adj, coupling, band, dem_fn)

    # Building-frontage spine FLOOR — the arm rises to serve the pads it fronts.
    # A foot anchor (seat − apron-climb) propagated along the consecutive
    # centerline chain as a cap-Lipschitz floor, so the whole ramp climbs
    # smoothly and the floor is grade-consistent by construction.
    spine_floor = building_spine_floor(
        layout, nodes, bucket_to_idx, building_seats, node_band, spine_adj)

    # The BODY (apron + junction interiors) is min-curvature, grade-compliant
    # between anchors — NOT DEM-following (user 2026-06-25: the DEM is unreliable;
    # we are grading pavement, so compliance between anchors is the model).  This
    # is neutral vs the old closest-DEM apron target (CYXY 468 vs 469, HECA 4535
    # vs 4538) and removes the DEM dependence.  ``O4_RP_APRON_SMOOTH=0`` reverts.
    _apron_sm = _os.environ.get("O4_RP_APRON_SMOOTH", "1") == "1"
    n_free = one_profile_solve(
        elev, shape_constraints, base_hard, nodes, dem_elev,
        runway_nodes, building_seats, apron_body, spine_nodes, spine_adj,
        node_band, spine_floor, coupling, apron_smooth=_apron_sm)

    n_terms, n_rects, n_juncs = _writeback(layout, elev, bucket_to_idx)

    if _os.environ.get("O4_STEP_DEBUG") == "1":
        print(f"  [one-profile] {icao}: solved {n_free} free node(s), "
              f"{len(building_seats)} building pad node(s) anchored.")
    _report(icao, n_free, n_free, _time.time() - t0,
            n_terms, n_rects, n_juncs)


def _seed_route_skeleton(layout, nodes, bucket_to_idx, elev, node_band, z,
                         geo_key, rect_end_keys, flex, base_hard=None):
    """SEED the route-graph profile onto the geometry and bound the skeleton to a
    tight FLEX band around it (NOT a hard freeze — user 2026-06-25: some skeleton
    flexibility is fine, it just can't break a grade cap).  The body then grades
    from a smooth, near-profile skeleton, and the skeleton can give a little where
    the body would otherwise be over-constrained.

      * spine / junction-spine node ``i`` → seed ``z[geo_key[i]]``, band ``z±flex``;
      * each sloping rect → both short-edge corners seed that end's profile node,
        band ``z±flex`` (the rect stays planar via the flat-end coupling); its
        PLANE is stashed so the cap can continue it;
      * each rect END-CAP → seed + band from the parent rect's plane continuation.

    Mutates ``elev`` and ``node_band`` in place.  Returns ``rect_planes`` —
    ``[(end0_node_idx, e0, end1_node_idx, e1, cap_corner_idxs...)]`` style records
    used by :func:`_restamp_caps` to re-derive each cap from the rect's FINAL
    (post-solve) plane.
    """
    import math
    from auto_patch.junction_rules import SLOPING_RECT_ROLES

    cps = layout.canonical_points
    n = len(elev)
    # Nodes already HARD before we seed (runway CIFP corners + tile seams) are the
    # AUTHORITY: a spine/rect/cap node that coincides with one must MATCH it, never
    # overwrite it (user 2026-06-25: the spine touching the runway anchors AT the
    # runway).  Overwriting a runway corner with the route-profile value deformed
    # the runway (the F/14R valley).  Protect them.
    protected = ({i for i in range(n) if base_hard[i]}
                 if base_hard is not None else set())

    def _seed(i, val, hard=False):
        if i is None or i >= n or val is None or i in protected:
            return
        elev[i] = float(val)
        node_band[i] = (float(val) - flex, float(val) + flex)
        if hard and base_hard is not None:
            base_hard[i] = True

    def _idx(x, y):
        return bucket_to_idx.get(cps.get_or_add(float(x), float(y)))

    def _open4(poly):
        c = list(poly.exterior.coords)
        if c and c[0] == c[-1]:
            c = c[:-1]
        return c

    # spine nodes — the centerline profile, by index.  HARD: the smooth spine is
    # protected (it is paramount); the rects/caps/body twist to meet it.
    for i, key in geo_key.items():
        _seed(i, z.get(key), hard=True)

    # sloping rects — both short-edge corners seed that end's profile node.  Stash
    # the rect's two end NODE indices + midpoints + corner-key set for the cap.
    planes = []          # (corner_key_set, e0, end0_idx, e1, end1_idx)
    for s in layout.shapes:
        ends = rect_end_keys.get(id(s))
        if (ends is None or s.role not in SLOPING_RECT_ROLES
                or s.polygon is None or s.polygon.is_empty):
            continue
        coords = _open4(s.polygon)
        if len(coords) != 4:
            continue
        elens = [math.hypot(coords[(k + 1) % 4][0] - coords[k][0],
                            coords[(k + 1) % 4][1] - coords[k][1])
                 for k in range(4)]
        short = sorted(range(4), key=lambda k: elens[k])[:2]   # SAME order as enrich
        end_mid = [None, None]
        end_idx = [None, None]
        ckeys = set()
        ok = True
        for ei, e in enumerate(short):
            val = z.get(ends[ei]) if ei < len(ends) else None
            if val is None:
                ok = False
            a, b = coords[e], coords[(e + 1) % 4]
            end_mid[ei] = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
            for (x, y) in (a, b):
                ckeys.add(cps.get_or_add(float(x), float(y)))
                _seed(_idx(x, y), val)
            end_idx[ei] = _idx(*a)
        if ok and end_idx[0] is not None and end_idx[1] is not None:
            planes.append((ckeys, end_mid[0], end_idx[0], end_mid[1], end_idx[1]))

    # rect END-CAPS — seed each cap corner from the parent rect's plane (matched
    # by ≥2 shared corners), continuing the smoothed slope past the rect end.
    cap_records = []
    for s in layout.shapes:
        if (not getattr(s, "is_rect_cap", False) or s.polygon is None
                or s.polygon.is_empty):
            continue
        coords = _open4(s.polygon)
        if len(coords) < 3:
            continue
        cap_keys = {cps.get_or_add(float(x), float(y)) for (x, y) in coords}
        best, best_sh = None, 1
        for pl in planes:
            sh = len(pl[0] & cap_keys)
            if sh > best_sh:
                best_sh, best = sh, pl
        if best is None:
            continue
        _ck, e0, e0i, e1, e1i = best
        ax, ay = e1[0] - e0[0], e1[1] - e0[1]
        L2 = ax * ax + ay * ay
        if L2 < 1e-9:
            continue
        z0, z1 = elev[e0i], elev[e1i]
        corner_t = []
        for (x, y) in coords:
            t = ((x - e0[0]) * ax + (y - e0[1]) * ay) / L2
            _seed(_idx(x, y), z0 + t * (z1 - z0))
            ci = _idx(x, y)
            if ci is not None:
                corner_t.append((ci, t))
        cap_records.append((e0i, e1i, corner_t))
    return cap_records


def _restamp_caps(elev, cap_records):
    """Re-derive each rect end-cap's corners from its parent rect's FINAL plane
    (the rect ends may have flexed during the solve/projection), so the cap stays
    a planar continuation of the rect.  Mutates ``elev``; returns #corners set."""
    n = 0
    for (e0i, e1i, corner_t) in cap_records:
        if e0i >= len(elev) or e1i >= len(elev):
            continue
        z0, z1 = elev[e0i], elev[e1i]
        for (ci, t) in corner_t:
            if ci < len(elev):
                elev[ci] = z0 + t * (z1 - z0)
                n += 1
    return n


def _add_rect_end_nodes(layout, nodes, bucket_to_idx, elev, dem_elev, node_band,
                        base_hard, shape_constraints, spine_nodes, spine_adj,
                        coupling, band, dem_fn):
    """Add a VIRTUAL elevation-graph node at each sloping rect's two flat-end
    midpoints (the rect axis ends) — geometry is NOT touched.  Each virtual node
    is woven into the spine chain (edge to the nearest existing spine node at the
    rect's cap, and to the rect's other end), so the 1-D profile solve runs
    continuously through the rect.  The rect's end CORNERS couple to the virtual
    node at THEIR end (same axis position → equal = flat across width, on the
    profile), so the rect tilts between two on-profile points and its cap is
    co-planar.  Mutates the per-node arrays + graphs in place; returns #added."""
    import math
    from auto_patch.config import taxi_grade_cap_for_letter
    from auto_patch.junction_rules import SLOPING_RECT_ROLES

    cps = layout.canonical_points
    letters = getattr(layout, "apt_taxi_letters", None) or {}
    sp_list = [(i, nodes[i][0], nodes[i][1]) for i in spine_nodes]

    def nearest_spine(x, y, maxd):
        best, bd = None, maxd * maxd
        for (i, sx, sy) in sp_list:
            d = (sx - x) ** 2 + (sy - y) ** 2
            if d < bd:
                bd, best = d, i
        return best

    def couple(a, b):
        ga = coupling.get(a, [a])
        gb = coupling.get(b, [b])
        grp = list(dict.fromkeys(list(ga) + list(gb) + [a, b]))
        for m in grp:
            coupling[m] = grp

    virt_edges = []
    n_added = 0
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        coords = list(s.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            continue
        idx = [bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
               for (x, y) in coords]
        if any(i is None for i in idx):
            continue
        elens = [math.hypot(coords[(k + 1) % 4][0] - coords[k][0],
                            coords[(k + 1) % 4][1] - coords[k][1])
                 for k in range(4)]
        ends = sorted(range(4), key=lambda k: elens[k])[:2]   # 2 flat-end edges
        axislen = max(elens)
        cap = float(taxi_grade_cap_for_letter(letters.get(s.ref)))
        end_v = []
        for e in ends:
            a, b = coords[e], coords[(e + 1) % 4]
            mx, my = 0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1])
            vi = len(nodes)
            nodes.append((mx, my))
            node_band.append(band(mx, my))
            de = dem_fn(mx, my)
            dem_elev.append(de if de is not None else 0.0)
            base_hard.append(False)
            adj_sp = nearest_spine(mx, my, 18.0)
            elev.append(elev[adj_sp] if adj_sp is not None
                        else (de if de is not None else 0.0))
            spine_nodes.add(vi)
            if adj_sp is not None:                      # weave into the chain
                d = math.hypot(nodes[adj_sp][0] - mx, nodes[adj_sp][1] - my)
                budget = cap * max(d, 1e-3)
                spine_adj.setdefault(vi, []).append((adj_sp, budget))
                spine_adj.setdefault(adj_sp, []).append((vi, budget))
                virt_edges.append((vi, adj_sp, budget))
            # corners ride the profile — budget-0 edges so the body co-solves
            # against them EVERY sweep (a coupling-only end-snap left the body
            # solved against the corners' free value → it jumped at the snap).
            couple(idx[e], vi)
            couple(idx[(e + 1) % 4], vi)
            virt_edges.append((vi, idx[e], 0.0))
            virt_edges.append((vi, idx[(e + 1) % 4], 0.0))
            end_v.append(vi)
            n_added += 1
        if len(end_v) == 2:                             # the rect axis itself
            budget = cap * max(axislen, 1e-3)
            spine_adj.setdefault(end_v[0], []).append((end_v[1], budget))
            spine_adj.setdefault(end_v[1], []).append((end_v[0], budget))
            virt_edges.append((end_v[0], end_v[1], budget))
    if virt_edges:                                      # so they're in the solve
        shape_constraints.append({"edges": virt_edges, "nodes": [], "flat": False})
    return n_added


def _route_node_set(layout, bucket_to_idx, spine_nodes):
    """The ROUTE skeleton node indices: every sloping-rect + cap shape vertex,
    plus the spine nodes.  These are read from the profile (anchored); the rest
    is body, filled between them."""
    from auto_patch.junction_rules import SLOPING_RECT_ROLES
    cps = layout.canonical_points
    route = set(spine_nodes)
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        if s.role in SLOPING_RECT_ROLES or getattr(s, "is_rect_cap", False):
            coords = list(s.polygon.exterior.coords)
            if coords and coords[0] == coords[-1]:
                coords = coords[:-1]
            for (x, y) in coords:
                i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
                if i is not None:
                    route.add(i)
    return route
