"""Anchors + bounds for the one-profile solve — all from THE ONE graph.

There is a single reachability graph: the reach band computed on THE unified
grade graph (``building_feasibility.reach_band_unified``).  It sets the building
levels AND bounds every apron / spine / rect node, so they agree by
construction.  This module never builds a second graph.

* ``reach_band_for`` — build the band (+ a DEM sampler + the runway-edge anchors)
  once per solve.
* ``build_building_seats`` — seat each airside building FLAT at the level its
  FRONTAGE can reach (the band intersected over the pad ring), not the centroid:
  the band is a per-point envelope and a serving centerline climbs along a pad,
  so the centroid may reach higher than the apron around the pad can grade to.
* ``node_bands`` — the per-node ``(floor, ceiling)`` the solve clamps into.
* ``apron_body_nodes`` — apron-body vs taxi-route role split (target only).
"""
from __future__ import annotations

from auto_patch.layout import (
    ROLE_APRON, ROLE_BUILDING, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_SERVICE_JUNCTION,
    ROLE_SERVICE_ROAD, ROLE_STUB,
)

_INF = float("inf")

# The TAXI ROUTE (smoothness target, bounded by the reach band): taxi rects +
# junctions.  A node shared by an apron AND a route shape is a route node.
_ROUTE_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
})
# DEM-FOLLOWING body (closest-to-DEM target, NO taxi-band bound): aprons AND
# service roads/junctions.  A service road is NOT a taxiway — it grades at 4% and
# ties to the ground road network / terrain, so it must NOT be clamped to the
# taxi reach band (which would cap it metres below DEM — user 2026-06-25).
_DEM_BODY_ROLES = frozenset({
    ROLE_APRON, ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION,
})


def _open_ring(coords):
    if coords and coords[0] == coords[-1]:
        return list(coords[:-1])
    return list(coords)


def reach_band_for(layout, elev, bucket_to_idx, dem, tile_lat, tile_lon,
                   unified_graph=None):
    """Build the one reach band, a DEM sampler, and the runway-edge anchors.

    The band is computed on THE unified grade graph the spine solves on
    (``reach_band_unified``) — one graph, no route-graph drift, no
    ceiling-consistency bridge.  ``unified_graph`` is the prebuilt
    ``build_unified_graph`` (the caller already needs it); also returned so the
    solve reuses the same object."""
    from auto_patch.elevation import _sample_dem
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_unified)
    from auto_patch.elevation_per_surface.solver_primitives import _runway_edge_pts

    runway_pts = _runway_edge_pts(layout, elev, bucket_to_idx)
    G = unified_graph
    band = reach_band_unified(layout, G)

    def _dem(x, y):
        try:
            lat, lon = layout.m_to_ll(x, y)
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except Exception:                                     # pragma: no cover
            return None

    return band, _dem, runway_pts, G


def build_building_seats(layout, bucket_to_idx, band, dem_fn, runway_pts):
    """``{pad_node_idx: flat_level}`` for every airside-touching building, seated
    at the level its FRONTAGE can reach (the band intersected over the pad ring)
    closest to DEM."""
    import os as _os
    from auto_patch.layout import ROLE_APRON
    from auto_patch.elevation_per_surface.building_feasibility import (
        building_feasible_levels)

    cps = layout.canonical_points
    # ``building_feasible_levels`` decides WHICH buildings are airside-served (its
    # touch test) + gives the centroid level as a fallback for off-network pads.
    levels = building_feasible_levels(layout, runway_pts, dem_fn, band=band)

    # FRONTAGE-EDGE seat (user 2026-06-27): seat the flat pad at the feasible level
    # reachable at the CENTRE of its FRONTAGE edge — the apron-shared building edge
    # facing the MOST-CONSTRAINED taxi route (the lowest band ceiling among the
    # apron-shared edges).  The straight route from that centre to the binding
    # taxiway IS what ``band`` measures, so the apron can grade ≤1 % from the
    # frontage down to the taxiway and the far frontages descend to the pad.  This
    # supersedes the whole-ring MEDIAN, which over-pinned the low (route-limited)
    # frontage corner by averaging in the far high corners — CYXY building15 was
    # seated 709.4 (median over 707.6..712.5) while its A2 frontage centre reaches
    # only 708.4, pinning the A2-end apron 1.8 m high → the 20 % apron cliff.
    # Gate off → whole-ring median (legacy, byte-identical).
    _frontage = _os.environ.get("O4_BUILDING_FRONTAGE_SEAT", "1") == "1"
    # Large buildings (≥ area) seat at the FULL-FRONTAGE feasible level (user
    # 2026-06-27): the entire frontage must grade to the spine ≤1 %, so the seat is
    # the band intersected over the whole frontage (computed by
    # ``building_feasible_levels``), not the single lowest-ceiling frontage edge.
    from auto_patch.grade_law import building_requires_full_frontage
    apron_keys: set = set()
    if _frontage:
        # Frontage = a building edge shared with any SOFT pavement ring.
        # Under the route-arc GLOBAL SLICE the face a building fronts onto
        # is usually ROLE_JUNCTION (a corridor face), not ROLE_APRON —
        # apron-only keys silently dropped every such frontage back to the
        # legacy whole-ring MEDIAN seat, re-creating the over-pinned
        # frontage conflicts the frontage seat was built to fix (CYXY
        # pads seated 1-2 m apart at close quarters).
        from auto_patch.layout import (
            ROLE_JUNCTION as _RJ, ROLE_SERVICE_JUNCTION as _RSJ)
        for a in layout.shapes:
            if (a.role in (ROLE_APRON, _RJ, _RSJ) and a.polygon is not None
                    and not a.polygon.is_empty):
                for (x, y) in _open_ring(list(a.polygon.exterior.coords)):
                    apron_keys.add((round(x, 2), round(y, 2)))

    def _median(ring, de):
        ceils = sorted(b[1] for (x, y) in ring if (b := band(x, y)) is not None)
        if not ceils:
            return None
        m = len(ceils)
        med = (ceils[m // 2] if m % 2
               else 0.5 * (ceils[m // 2 - 1] + ceils[m // 2]))
        return min(de, med) if de is not None else med

    def _frontage_box(ring):
        """Feasible seat interval from the centres of the building's apron-shared
        edges (both endpoints shared with an apron): ``(max floor, min ceiling)``
        — the ceiling is the most-constrained frontage (the legacy seat rule),
        the floor the highest any frontage must stay above.  None when no edge
        is apron-shared (→ caller falls back)."""
        n = len(ring)
        flo, fhi = None, None
        for i in range(n):
            a = (round(ring[i][0], 2), round(ring[i][1], 2))
            b = (round(ring[(i + 1) % n][0], 2), round(ring[(i + 1) % n][1], 2))
            if a in apron_keys and b in apron_keys:
                cx = 0.5 * (ring[i][0] + ring[(i + 1) % n][0])
                cy = 0.5 * (ring[i][1] + ring[(i + 1) % n][1])
                bc = band(cx, cy)
                if bc is not None:
                    flo = bc[0] if flo is None else max(flo, bc[0])
                    fhi = bc[1] if fhi is None else min(fhi, bc[1])
        if fhi is None:
            return None
        return (min(flo, fhi) if flo is not None else -_INF, fhi)

    # ── Per-pad independent target + feasible box ────────────────────────────
    # target = the legacy independent seat (DEM biased into the frontage band);
    # box    = the reach-band interval the seat may move within when the JOINT
    #          projection below reconciles neighbouring pads.
    pads: list = []             # (shape, ring, target_level, lo, hi)
    for s in layout.shapes:
        lv = levels.get(id(s))
        if lv is None or s.polygon is None or s.polygon.is_empty:
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        de = dem_fn(s.polygon.centroid.x, s.polygon.centroid.y)
        if building_requires_full_frontage(s.polygon.area):
            # ``lv`` IS the full-frontage feasible level for a large building;
            # its box is the frontage-band intersection ``lv`` was clamped into.
            from auto_patch.grade_law import BUILDING_REACH_CORRIDOR_M
            from auto_patch.elevation_per_surface.building_feasibility import (
                _frontage_band, _pavement_visibility)
            from auto_patch.config import VISIBLE_CHORD_CONNECT
            level = float(lv)
            _cls = [cl.line for cl in
                    (getattr(layout, "apt_taxi_centerlines", None) or [])
                    if cl.line is not None and not cl.line.is_empty
                    and not cl.is_service]
            _vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None
            fb = (_frontage_band(s.polygon, band, _cls, _vis,
                                 BUILDING_REACH_CORRIDOR_M) if _cls else None)
            if fb is None:
                fb = band(s.polygon.centroid.x, s.polygon.centroid.y)
            lo, hi = (min(*fb), max(*fb)) if fb is not None else (level, level)
        else:
            box = _frontage_box(ring) if _frontage else None
            if box is not None:
                lo, hi = box
                level = min(de, hi) if de is not None else hi
            else:                                    # no apron-shared edge / off
                level = _median(ring, de)
                if level is None:
                    level = float(lv)                # off-network → fallback
                # Box = the band intersected over the pad's own ring, so the
                # coupling can still move a fallback pad within its reachable
                # range (an immovable DEM-low seat forced the serving spine
                # 5 m below its own profile — building26).
                blos = [b[0] for (x, y) in ring
                        if (b := band(x, y)) is not None]
                bhis = [b[1] for (x, y) in ring
                        if (b := band(x, y)) is not None]
                if bhis:
                    lo, hi = min(max(blos), min(bhis)), min(bhis)
                else:
                    lo = hi = level                  # off-network: immovable
        pads.append((s, ring, float(level), lo, hi))

    # ── SEAT COUPLING (user 2026-07-03): jointly-feasible pad levels ─────────
    # Each pad pins nearby spine/apron nodes to ``seat ± 1%·d`` (the building↔
    # spine law, never blended/relaxed), so two pads across shared pavement must
    # satisfy ``|L_i − L_j| ≤ APRON_MAX_GRADE · gap`` — independent seats left
    # neighbouring pads ≤2.6 m apart and made the surrounding faces infeasible
    # (the SPJC >3% class; the feasibility audit proves joint levels exist).
    # Project the independent targets onto the coupled polytope (POCS, same
    # solver as the no-building apron seats).  Straight-line gap is a LOWER
    # bound on the in-pavement route, so the coupling is conservative; pairs
    # couple only within the reach corridor and over a pavement-visible chord
    # (pads separated by grass/roads never constrain each other).
    _couple = _os.environ.get("O4_BUILDING_SEAT_COUPLING", "1") == "1"
    if _couple and len(pads) >= 2:
        from shapely.geometry import LineString
        from shapely.ops import nearest_points
        from auto_patch.config import APRON_MAX_GRADE, VISIBLE_CHORD_CONNECT
        from auto_patch.grade_law import BUILDING_REACH_CORRIDOR_M
        from auto_patch.elevation_per_surface.building_feasibility import (
            _pavement_visibility, _VIS_ON_PAV_FRAC)
        vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None
        pairs: dict = {}
        for i in range(len(pads)):
            pi = pads[i][0].polygon
            for j in range(i + 1, len(pads)):
                pj = pads[j][0].polygon
                gap = pi.distance(pj)
                if gap > BUILDING_REACH_CORRIDOR_M:
                    continue
                if vis is not None and gap > 1e-6:
                    a, b = nearest_points(pi, pj)
                    chord = LineString([(a.x, a.y), (b.x, b.y)])
                    if not vis.contains(chord):
                        try:    # tolerate tiny weld-seam gaps
                            frac = (chord.intersection(vis.context).length
                                    / chord.length)
                        except Exception:           # pragma: no cover
                            frac = 0.0
                        if frac < _VIS_ON_PAV_FRAC:
                            continue                # across grass → uncoupled
                pairs[(i, j)] = APRON_MAX_GRADE * gap
        if pairs:
            targets = [p[2] for p in pads]
            boxes = [(p[3], p[4]) for p in pads]
            L = _pocs_project_levels(targets, boxes, pairs)
            _dbg = _os.environ.get("O4_SEAT_DEBUG") == "1"
            if _dbg:
                pre = sorted(
                    ((abs(targets[i] - targets[j]) - lim, i, j, lim)
                     for (i, j), lim in pairs.items()), reverse=True)
                print(f"  [seats] {len(pads)} pads, {len(pairs)} coupled "
                      f"pairs, polytope "
                      f"{'FEASIBLE' if L is not None else 'EMPTY'}")
                for ex, i, j, lim in pre[:8]:
                    if ex <= 0:
                        break
                    print(f"    pre-conflict {ex:+.2f}m over lim {lim:.2f}: "
                          f"{pads[i][0].ref or '?'} t={targets[i]:.2f} "
                          f"box=({pads[i][3]:.2f},{pads[i][4]:.2f})  vs  "
                          f"{pads[j][0].ref or '?'} t={targets[j]:.2f} "
                          f"box=({pads[j][3]:.2f},{pads[j][4]:.2f})")
            if L is not None:
                moved = sum(1 for k in range(len(pads))
                            if abs(L[k] - targets[k]) > 0.01)
                if moved:
                    try:
                        import O4_UI_Utils as _UI
                        _UI.vprint(1, f"  [seats] coupled {len(pads)} pads / "
                                      f"{len(pairs)} pairs: moved {moved}, max "
                                      f"{max(abs(L[k] - targets[k]) for k in range(len(pads))):.2f} m")
                    except Exception:
                        pass
                pads = [(s, ring, L[k], lo, hi)
                        for k, (s, ring, _t, lo, hi) in enumerate(pads)]
            elif _dbg:
                print("  [seats] EMPTY polytope -> independent seats kept")
            # L is None (empty polytope) → keep independent targets: no
            # regression vs the uncoupled model, conflicts stay as they were.

    seats: dict = {}
    for (s, ring, level, _lo, _hi) in pads:
        for (x, y) in ring:
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is not None:
                seats[i] = float(level)
    return seats


def _pocs_project_levels(targets, boxes, pairs, max_iter=300, tol=1e-4):
    """Project per-item target levels onto (box ∩ pairwise-coupling polytope).

    Find ``L_i`` minimising ``Σ(L_i − t_i)²`` s.t. ``|L_i − L_j| ≤ pairs[(i,j)]``
    and ``f_i ≤ L_i ≤ ce_i``.  Cyclic projection (POCS): push each violated pair
    together by half the excess, then re-clamp to the boxes; repeat.  Returns
    ``[L_i]`` on convergence, or ``None`` when the polytope is EMPTY (boxes
    incompatible with the couplings = the FUNDAMENTAL case)."""
    n = len(targets)
    L = [min(max(targets[i], boxes[i][0]), boxes[i][1]) for i in range(n)]
    for _ in range(max_iter):
        worst = 0.0
        for (i, j), lim in pairs.items():
            d = L[i] - L[j]
            if d > lim:
                e = 0.5 * (d - lim)
                L[i] -= e
                L[j] += e
                worst = max(worst, d - lim)
            elif -d > lim:
                e = 0.5 * (-d - lim)
                L[i] += e
                L[j] -= e
                worst = max(worst, -d - lim)
        for i in range(n):
            L[i] = min(max(L[i], boxes[i][0]), boxes[i][1])
        if worst <= tol:
            break
    ok = all(abs(L[i] - L[j]) <= lim + 1e-3
             for (i, j), lim in pairs.items())
    return L if ok else None


def _project_apron_contacts(targets, boxes, positions, cap,
                            max_iter=300, tol=1e-4):
    """Project per-feeder target levels onto (box ∩ apron-cap polytope):
    ``|L_i − L_j| ≤ cap·d_ij`` with ``d_ij`` = straight gap (a LOWER bound on
    the in-apron route, so the cap constraint is conservative).  See
    :func:`_pocs_project_levels` for the projection itself."""
    import math
    n = len(targets)
    pairs = {(i, j): cap * math.hypot(positions[i][0] - positions[j][0],
                                      positions[i][1] - positions[j][1])
             for i in range(n) for j in range(i + 1, n)}
    return _pocs_project_levels(targets, boxes, pairs,
                                max_iter=max_iter, tol=tol)


# Minimum apron area to ANCHOR a no-building apron (user 2026-06-30).  A
# sub-threshold apron is a decomposition fragment of a larger apron-blob, not a
# real expanse; pinning it to its DEM-feasible level over-constrains the network
# for no benefit, so it is left to flex with its feeders instead.  This replaces
# the old apron→junction demotion (which mutated role purely to dodge anchoring
# and broke the junction invariants on non-HECA airports).
_NOBUILD_APRON_SEAT_MIN_AREA_M2 = 2000.0


def build_nobuilding_apron_seats(layout, bucket_to_idx, band, dem_fn):
    """``{feeder_contact_node_idx: feasible_level}`` for every NO-BUILDING apron —
    the FEEDER-CONVERGENCE rule (user 2026-06-26 directive #3; tilt model
    2026-06-28).

    A no-building apron has no pad to anchor it, so its feeder taxiways each grade
    to their own DEM-driven level and can arrive INCOMPATIBLE (the ``route_reach``
    violation: feeder contacts whose elevation gap exceeds the apron cap over their
    separation).  Rather than force the apron FLAT (one level for all feeders, which
    over-constrains and wastes the apron's own grade budget), anchor EACH feeder
    contact at the level feasible THERE — its reach band, biased to DEM — projected
    onto the apron-cap polytope so the apron TILTS ≤cap between contacts:

        minimise Σ(L_i − t_i)²  s.t.  |L_i − L_j| ≤ cap·d_ij  and  f_i ≤ L_i ≤ ce_i

    (:func:`_project_apron_contacts`).  ``t_i = clamp(DEM_i, band_i)`` pulls a feeder
    floating ABOVE its reach band back down to a reachable level; the projection
    then shares the apron's cap so close feeders need not be equal, only gradeable.
    A solution clears ``route_reach`` BY CONSTRUCTION (the constraints ARE its
    condition); an EMPTY polytope (a feeder's band can't reconcile with another's
    across the cap) is FUNDAMENTAL → skipped (documented transition, not a gate).

    Aprons that abut a building are skipped (the pad anchors the level).  The caller
    (``solve.py``) ANCHORS the returned ``{contact_node: L_i}`` like a building seat
    (heaviest), so the feeder SPINES grade to meet the apron (user 2026-06-28 — the
    apron must anchor for the spines to adjust to it; a SOFT ``node_band`` clamp let
    whatever pinned a feeder win and didn't converge).  Only the per-feeder CONTACTS
    are anchored — at their OWN reachable level — so the apron body still flexes and
    the feeder reaches L_i without an over-cap step (the earlier FLAT whole-ring seat
    forced unreachable levels → regressed ``cyxy_spine_zero`` + HECA runway).  Gate
    ``O4_NOBUILD_APRON_SEAT=0`` disables (no apron seats, byte-identical)."""
    import os as _os
    if _os.environ.get("O4_NOBUILD_APRON_SEAT", "1") != "1":
        return {}
    from shapely.geometry import Point
    from auto_patch.layout import ROLE_APRON, ROLE_BUILDING, ROLE_JUNCTION
    from auto_patch.junction_rules import SLOPING_RECT_ROLES
    from auto_patch.config import APRON_MAX_GRADE
    cps = layout.canonical_points
    buildings = [b.polygon for b in layout.shapes
                 if b.role == ROLE_BUILDING and b.polygon is not None
                 and not b.polygon.is_empty]
    # The taxi-network shapes whose contact feeds an apron (the SAME set
    # ``route_reach_violations`` measures): sloping rects + junctions, not SVC.
    route_roles = set(SLOPING_RECT_ROLES) | {ROLE_JUNCTION}
    routes = [t for t in layout.shapes
              if t.role in route_roles and t.polygon is not None
              and not t.polygon.is_empty
              and not str(t.ref or "").upper().startswith("SVC")]
    seats: dict = {}
    for s in layout.shapes:
        if (s.role != ROLE_APRON or s.polygon is None or s.polygon.is_empty):
            continue
        if s.polygon.area <= _NOBUILD_APRON_SEAT_MIN_AREA_M2:
            continue            # too small to anchor — flexes with its feeders
        if any(s.polygon.distance(b) < 1.0 for b in buildings):
            continue                            # a building anchors the level
        # Each feeder's CONTACT = its nearest vertex to the apron (what route_reach
        # measures), with its reach band + DEM-biased target.
        idxs, tgts, boxes, poss = [], [], [], []
        for t in routes:
            if t is s or s.polygon.distance(t.polygon) > 1.5:
                continue
            best = None
            for (x, y) in _open_ring(list(t.polygon.exterior.coords)):
                d2 = s.polygon.exterior.distance(Point(x, y))
                if best is None or d2 < best[0]:
                    best = (d2, (x, y))
            if best is None:
                continue
            px, py = best[1]
            b = band(px, py)
            if b is None:
                continue
            i = bucket_to_idx.get(cps.get_or_add(float(px), float(py)))
            if i is None:
                continue
            de = dem_fn(px, py)
            tgt = de if de is not None else 0.5 * (b[0] + b[1])
            idxs.append(i)
            tgts.append(min(max(tgt, b[0]), b[1]))
            boxes.append(b)
            poss.append((px, py))
        if len(idxs) < 2:
            continue
        L = _project_apron_contacts(tgts, boxes, poss, APRON_MAX_GRADE)
        if L is None:
            continue                            # fundamental → documented transition
        for i, Li in zip(idxs, L):
            seats[i] = float(Li)
    return seats


def build_apron_contact_floors(layout, bucket_to_idx, band, dem_fn, building_seats):
    """``{feeder_contact_node_idx: floor_level}`` for taxiways/junctions that meet a
    BUILDING-ANCHORED apron's edge — so the feeder SPINE grades UP to the apron
    instead of the (senior) apron sagging down to the feeder's DEM-low mouth.

    The complement of :func:`build_nobuilding_apron_seats`, which handles ONLY
    no-building aprons (it bails on any apron within 1 m of a building).  A building
    apron is held high by its pad seat, but where the apron edge is FAR from the
    building (beyond ``BUILDING_REACH_CORRIDOR_M``, so the building-frontage spine
    floor never reaches it) a feeder taxiway contacting that edge falls through every
    floor rule and solves to its own low DEM — dragging the apron edge into a cliff
    (OEMA TX8 #275: apron #198 held at 639 by a building 310 m away, TX8 mouth at the
    DEM 629 → a 96 % within-apron step).  This was the documented authority inverted:
    "a taxiway/apron node is apron-owned; the taxi yields", not the reverse.

    The floor is the apron's OWN guaranteed-reachable level at the contact: the apron
    grades ≤ ``APRON_MAX_GRADE`` from each adjacent building seat, so at a contact
    ``d`` metres from a building seated at ``S`` the apron is at least ``S − cap·d``.
    Taking the max over the apron's buildings and clamping to the contact's reach band
    gives the level the feeder must rise to (never above the band ceiling, so it stays
    runway-reachable; never below the band floor).  A FLOOR (not a hard seat) so the
    feeder spine still grades smoothly up from its runway anchor and the apron body
    flexes — the taxi yields UP, the apron keeps its cap.  Gate
    ``O4_APRON_CONTACT_FLOOR=0`` disables (no floors, byte-identical)."""
    import os as _os
    if _os.environ.get("O4_APRON_CONTACT_FLOOR", "1") != "1":
        return {}
    from shapely.geometry import Point
    from auto_patch.layout import ROLE_APRON, ROLE_BUILDING, ROLE_JUNCTION
    from auto_patch.junction_rules import SLOPING_RECT_ROLES
    from auto_patch.config import APRON_MAX_GRADE
    cps = layout.canonical_points
    cap = APRON_MAX_GRADE

    # Each building's seat level (its pad nodes all share one seat in building_seats)
    # paired with its polygon, for the seat − cap·d reach bound.
    bseats: list = []
    for b in layout.shapes:
        if (b.role != ROLE_BUILDING or b.polygon is None or b.polygon.is_empty):
            continue
        lv = None
        for (x, y) in _open_ring(list(b.polygon.exterior.coords)):
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is not None and building_seats.get(i) is not None:
                lv = building_seats[i]
                break
        if lv is not None:
            bseats.append((b.polygon, float(lv)))
    if not bseats:
        return {}

    route_roles = set(SLOPING_RECT_ROLES) | {ROLE_JUNCTION}
    routes = [t for t in layout.shapes
              if t.role in route_roles and t.polygon is not None
              and not t.polygon.is_empty
              and not str(t.ref or "").upper().startswith("SVC")]

    floors: dict = {}
    for s in layout.shapes:
        if (s.role != ROLE_APRON or s.polygon is None or s.polygon.is_empty):
            continue
        # Only BUILDING-anchored aprons (no-building ones use the seat path above).
        near = [(poly, lv) for (poly, lv) in bseats if s.polygon.distance(poly) < 1.0]
        if not near:
            continue
        for t in routes:
            if t is s or s.polygon.distance(t.polygon) > 1.5:
                continue
            # contact = the feeder vertex nearest the apron (what route_reach measures)
            best = None
            for (x, y) in _open_ring(list(t.polygon.exterior.coords)):
                d2 = s.polygon.exterior.distance(Point(x, y))
                if best is None or d2 < best[0]:
                    best = (d2, (x, y))
            if best is None:
                continue
            px, py = best[1]
            bnd = band(px, py)
            if bnd is None:
                continue
            cpt = Point(px, py)
            # the apron's guaranteed-reachable level here: max_b(seat_b − cap·d_b),
            # i.e. the lowest level the apron still grades to each building within cap.
            reach = max(lv - cap * poly.distance(cpt) for (poly, lv) in near)
            floor = min(max(reach, bnd[0]), bnd[1])         # clamp into reach band
            i = bucket_to_idx.get(cps.get_or_add(float(px), float(py)))
            if i is None:
                continue
            if floor > floors.get(i, -float("inf")):
                floors[i] = float(floor)
    return floors


def node_bands(nodes, band):
    """Per-node ``(floor, ceiling)`` from the one reach band (``None`` off-net)."""
    return [band(x, y) for (x, y) in nodes]


def _spine_floor_per_node(layout, nodes, bucket_to_idx, building_seats,
                          node_band, spine_adj):
    """``{spine_node_idx: floor}`` — floor EVERY spine node directly from its own
    VISIBLE chord to the nearest spine-facing building edge (user 2026-06-27,
    replacing the single centroid foot).

    For each spine node, take the straight chord to the closest point on each
    building within the frontage corridor; if that chord stays on pavement (a real
    apron path, not across grass / through another building) the node is floored at
    ``seat − 1%·chord`` — the elevation the spine must reach so the apron grades
    ≤1 % up to the flat pad.  A node takes the MAX over the buildings it faces.
    No centroid, no cap-decay propagation: ``seat − 1%·dist`` sampled per node is
    already cap-Lipschitz along the spine (adjacent nodes differ by ≤1 %·spacing ≤
    cap·spacing), so a big terminal's WHOLE frontage lifts the spine, not just one
    foot.  Each floor is clamped to the node's band ceiling (never above what the
    runway route reaches)."""
    from shapely.geometry import Point, LineString
    from shapely.ops import nearest_points
    from auto_patch.config import APRON_MAX_GRADE, VISIBLE_CHORD_CONNECT
    from auto_patch.grade_law import BUILDING_REACH_CORRIDOR_M
    from auto_patch.layout import ROLE_BUILDING
    from auto_patch.elevation_per_surface.building_feasibility import (
        _pavement_visibility, _VIS_ON_PAV_FRAC)

    cps = layout.canonical_points
    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None
    # The lift reaches a building over a VISIBLE on-pavement chord at any range up
    # to THE single reach corridor (the visibility gate below, not the distance, is
    # the real limit) — so a building anchors its serving spine even across a wide
    # single apron (CYXY building22 at 219 m).  ONE rule, shared with the seat band.
    corridor = BUILDING_REACH_CORRIDOR_M

    builds = []
    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty):
            continue
        lv = None
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i in building_seats:
                lv = building_seats[i]
                break
        if lv is not None:
            builds.append((s.polygon, float(lv)))
    if not builds:
        return {}

    floor: dict = {}
    for i in spine_adj:
        if i >= len(nodes):
            continue
        px, py = nodes[i]
        p = Point(px, py)
        best = None
        for (poly, lv) in builds:
            d = poly.distance(p)
            if d > corridor:
                continue
            near = nearest_points(poly, p)[0]   # spine-facing building edge point
            chord = LineString([(px, py), (near.x, near.y)])
            if vis is not None and chord.length > 1e-6 and not vis.contains(chord):
                try:                            # tolerate tiny weld-seam gaps
                    frac = chord.intersection(vis.context).length / chord.length
                except Exception:               # pragma: no cover
                    frac = 0.0
                if frac < _VIS_ON_PAV_FRAC:
                    continue                    # chord leaves pavement → not facing
            t = lv - APRON_MAX_GRADE * d        # 1 % apron from spine up to the pad
            if best is None or t > best:
                best = t
        if best is None:
            continue
        nb = node_band[i] if i < len(node_band) else None
        if nb is not None and best > nb[1]:
            best = nb[1]                        # never above the reachable ceiling
        floor[i] = best
    return floor


def building_spine_floor(layout, nodes, bucket_to_idx, building_seats,
                         node_band, spine_adj):
    """``{spine_node_idx: floor}`` — make the serving spine RISE to serve its
    buildings (user 2026-06-25): the taxi arm exists to serve its pads, so the
    SAME trace that set a building's feasible level anchors the spine at the
    precise elevation it must reach there, and that anchor is GRADED SMOOTHLY
    along the centerline chain ("grade smoothly between anchors").

    User 2026-06-27: the default is now :func:`_spine_floor_per_node` — every
    spine node floored directly from its own visible chord to the spine-facing
    building edge (the centroid foot under-covered large terminals).  The legacy
    centroid/full-frontage-foot path below is kept for A/B
    (``O4_SPINE_FLOOR_PER_NODE=0``).

    For each airside building, the serving centerline is the one the reach band
    used (``_nearest_visible_centerline`` across the continuous apron — NOT the
    geometric nearest, so the anchor is exactly the point the building was made
    consistent with).  The spine node nearest the building's perpendicular FOOT
    is anchored at ``seat − APRON_MAX_GRADE·dist`` — the elevation the spine needs
    so the apron grades ≤1 % up to the flat pad.

    That foot anchor is then propagated along the CONSECUTIVE centerline chain
    (``spine_adj``, budget ``cap·dist``) as a floor that DECREASES at exactly the
    cap rate: ``floor_j = anchor − capdist(foot → j)``.  This builds the whole
    climbing ramp, and because the floor is cap-Lipschitz along the chain it is
    grade-consistent BY CONSTRUCTION — it can never force a spine grade break, and
    (since every chain node's neighbour is also floored) the solve's "envelope
    yields" fallback no longer drops it.  A single un-propagated floor was dropped
    whenever the foot's flat runway-side neighbour capped it low → the arm stayed
    flat (CYXY ~U12 694.5 vs building19 700.2, 106 m away).  Each floor is clamped
    to the node's band ceiling (never above what the runway route reaches)."""
    import os as _os0
    if _os0.environ.get("O4_SPINE_FLOOR_PER_NODE", "1") == "1":
        return _spine_floor_per_node(
            layout, nodes, bucket_to_idx, building_seats, node_band, spine_adj)

    import heapq
    from shapely.geometry import Point
    from shapely.strtree import STRtree
    from auto_patch.config import APRON_MAX_GRADE, VISIBLE_CHORD_CONNECT
    from auto_patch.grade_graph import SPINE_PERP_TOL_M
    from auto_patch.layout import ROLE_BUILDING
    from auto_patch.elevation_per_surface.building_feasibility import (
        _nearest_visible_centerline, _pavement_visibility)

    cps = layout.canonical_points
    cl_items = [(cl.line, cl.name) for cl
                in (getattr(layout, "apt_taxi_centerlines", None) or [])
                if cl.line is not None and not cl.line.is_empty
                and not cl.is_service]
    clines = [ln for (ln, _n) in cl_items]
    if not clines:
        return {}
    vis = _pavement_visibility(layout) if VISIBLE_CHORD_CONNECT else None
    cl_index = {id(ln): k for k, ln in enumerate(clines)}
    # spine nodes on each centerline, with arc position.
    pts = [Point(x, y) for (x, y) in nodes]
    tree = STRtree(pts)
    on_cl: list = []                       # per centerline: [(arc, node_idx), ...]
    for ln in clines:
        members = []
        try:
            cand = tree.query(ln.buffer(SPINE_PERP_TOL_M))
        except Exception:                                     # pragma: no cover
            cand = []
        for qi in cand:
            i = int(qi)
            if ln.distance(pts[i]) <= SPINE_PERP_TOL_M:
                members.append((ln.project(pts[i]), i))
        on_cl.append(members)

    # FOOT ANCHORS: lift the spine to serve each building's frontage.  For a LARGE
    # building (full-frontage gate) anchor EVERY qualifying frontage foot — the same
    # sides that set its seat (a taxi corridor within range + a visible chord) — so
    # the spine rises to ``seat − 1%·perp`` along the WHOLE frontage, not only the
    # centroid's foot (user 2026-06-27, the dual of the full-frontage seating rule:
    # the pad is seated to clear the whole frontage, so the serving spine must rise
    # to it everywhere).  A small building (or one with no qualifying side) anchors
    # the single centroid foot, as before.
    import os as _os
    from auto_patch.config import (
        BUILDING_FULL_FRONTAGE, BUILDING_FULL_FRONTAGE_AREA_M2)
    from auto_patch.grade_law import BUILDING_REACH_CORRIDOR_M
    from auto_patch.elevation_per_surface.building_feasibility import (
        _has_visible_corridor)
    # ⚠ BANKED DEFAULT OFF: raising the spine to the WHOLE frontage regresses OEMA
    # (423→635 within-grade violations) — the pad sits at the route-reachable
    # CEILING (DEM-clamped), higher than the LOCAL spine can climb to from its
    # runway connection within grade, so lifting the spine to it breaks the
    # spine↔runway grade instead of fixing the apron.  Kept for A/B; needs the
    # seat reconciled with the local spine before it can default on.
    _full = (BUILDING_FULL_FRONTAGE
             and _os.environ.get("O4_BUILDING_FULL_FRONTAGE", "1") == "1"
             and _os.environ.get("O4_FRONTAGE_SPINE_RISE", "0") == "1")

    src: dict = {}

    def _anchor(px, py, lv, dist_geom):
        """Raise the spine node nearest ``(px, py)``'s perpendicular foot to
        ``lv − 1%·(dist_geom → that node)`` — the elevation the apron needs so it
        grades ≤1 % up to the flat pad."""
        c = Point(px, py)
        ln = (_nearest_visible_centerline(c, clines, vis) if vis is not None
              else min(clines, key=lambda L: L.distance(c)))
        members = on_cl[cl_index[id(ln)]]
        if not members:
            return
        foot = ln.interpolate(ln.project(c))
        _, i = min((foot.distance(pts[k]), k) for (_arc, k) in members)
        t = lv - APRON_MAX_GRADE * dist_geom.distance(pts[i])  # 1% spine→pad
        if t > src.get(i, -float("inf")):
            src[i] = t

    for s in layout.shapes:
        if (s.role != ROLE_BUILDING or s.polygon is None
                or s.polygon.is_empty):
            continue
        lv = None
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i in building_seats:
                lv = building_seats[i]
                break
        if lv is None:
            continue
        anchored = False
        if _full and s.polygon.area >= BUILDING_FULL_FRONTAGE_AREA_M2:
            ring = list(s.polygon.exterior.coords)
            for k in range(len(ring) - 1):
                ax, ay = ring[k]
                bx, by = ring[k + 1]
                mx, my = 0.5 * (ax + bx), 0.5 * (ay + by)
                for (px, py) in ((ax, ay), (mx, my), (bx, by)):
                    if _has_visible_corridor(px, py, clines, vis,
                                             BUILDING_REACH_CORRIDOR_M):
                        _anchor(px, py, lv, Point(px, py))
                        anchored = True
        if not anchored:                     # small / no qualifying frontage side
            c = s.polygon.centroid
            _anchor(c.x, c.y, lv, s.polygon)

    if not src:
        return {}

    # Propagate the anchors along the consecutive spine chain as a cap-Lipschitz
    # floor (Dijkstra on a MAX value: floor_j = max_src(target − capdist)).  budget
    # is ``cap·dist`` so the floor declines at exactly the per-letter cap — the
    # smooth ramp that serves the pad.
    floor: dict = {}
    pq = [(-t, i) for i, t in src.items()]
    heapq.heapify(pq)
    while pq:
        negt, i = heapq.heappop(pq)
        t = -negt
        if t <= floor.get(i, -float("inf")):
            continue
        floor[i] = t
        for (j, budget) in spine_adj.get(i, ()):     # budget = cap·dist
            nt = t - budget
            if nt > floor.get(j, -float("inf")):
                heapq.heappush(pq, (-nt, j))

    # clamp every floor to its node's band ceiling (never above the reachable).
    for i in list(floor):
        nb = node_band[i] if i < len(node_band) else None
        if nb is not None and floor[i] > nb[1]:
            floor[i] = nb[1]
    return floor


def apply_groundside_reach(layout, bucket_to_idx, elev, cap):
    """Re-level each groundside piece a service road connects to an apron, to the
    elevation the connector can REACH within the service-road grade cap — so the
    connector grades <=cap instead of ramping steeply to the groundside's raw DEM
    (user 2026-06-27, refining the accept-the-ramp model).

    "After buildings and aprons are anchored, check groundside pieces: if they have
    a service road, and if that road reaches an apron, follow that route to find
    what elevation the groundside can reach within grade and anchor it there.  If it
    has no service roads they just stay DEM."

    The service road that meets a groundside piece may reach the apron through a
    CHAIN of service roads/junctions (an out-and-back route, a yard loop), so the
    binding reference is the connector's OWN apron-ward mouth elevation (already
    solved), not the distant apron: the groundside mouth can sit at most
    ``cap * route_len`` from it (``route_len`` = the binding apron-ward->groundside
    edge).  Whether to re-level at all is gated by APRON REACHABILITY — the piece's
    service road must connect (directly or through the service network) to an apron;
    a groundside-only yard road never re-levels its piece.

    The piece is shifted by a UNIFORM offset (preserving its DEM relief) so its
    mouth(s) sit at the closest-to-DEM reachable level; the connector then grades the
    short climb at <=cap.  A piece reached by several connectors must satisfy them
    ALL (interval INTERSECTION of the per-connector shift bounds).

    Mutates groundside ``node_altitudes`` in place and returns ``(n_relevelled,
    welds)`` where ``welds = {node_idx: shifted_groundside_alt}`` for the mouths of
    the APRON-REACHABLE connectors only (the caller pins ``elev`` to these so the
    connector and groundside emit as one welded node).  A service road that does NOT
    reach an apron is left untouched — its piece stays DEM and its mouth is not
    pinned (the user's "stays DEM" case).  Safe to shift a whole piece because a
    groundside lot shares no nodes with airside (a clearance gap separates them) —
    only the connector mouth, which is welded to the shifted level."""
    import math
    import os as _os
    from auto_patch.layout import (
        ROLE_GROUNDSIDE_PAVEMENT, ROLE_APRON,
        ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)

    cps = layout.canonical_points

    def _key(x, y):
        return cps.get_or_add(float(x), float(y))

    # apron-owned canonical node keys (a service road TOUCHES the apron here).
    apron_keys: set = set()
    for s in layout.shapes:
        if s.role != ROLE_APRON or s.polygon is None or s.polygon.is_empty:
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            apron_keys.add(_key(x, y))

    # groundside pieces: per-key DEM altitude (the connector mouth shares a key);
    # plus the UNION of every groundside key (to split a connector's nodes into
    # groundside-mouth vs apron-ward).
    gs_pieces = []
    gs_all_keys: set = set()
    for g in layout.shapes:
        if (g.role != ROLE_GROUNDSIDE_PAVEMENT or g.polygon is None
                or g.polygon.is_empty or not g.node_altitudes):
            continue
        gcoords = list(g.polygon.exterior.coords)
        galts = list(g.node_altitudes)
        kalt: dict = {}
        for k in range(min(len(gcoords), len(galts))):
            if galts[k] is not None:
                kalt.setdefault(_key(*gcoords[k]), float(galts[k]))
        if kalt:
            gs_pieces.append((g, kalt))
            gs_all_keys |= set(kalt)
    if not gs_pieces:
        return 0, set()

    # Service-road network: each shape's node keys, an apron-touch flag, and an
    # adjacency (two service shapes are adjacent when they share a node key).  BFS
    # from the apron-touching shapes marks every APRON-REACHABLE service shape.
    svc = []                   # [(shape, keyset)]
    for c in layout.shapes:
        if c.role not in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION):
            continue
        if c.polygon is None or c.polygon.is_empty:
            continue
        ks = {_key(x, y) for (x, y) in _open_ring(list(c.polygon.exterior.coords))}
        svc.append((c, ks))
    if not svc:
        return 0, set()
    key_to_svc: dict = {}
    for si, (_c, ks) in enumerate(svc):
        for k in ks:
            key_to_svc.setdefault(k, []).append(si)
    reachable: set = set()
    stack = [si for si, (_c, ks) in enumerate(svc) if ks & apron_keys]
    reachable.update(stack)
    while stack:
        si = stack.pop()
        for k in svc[si][1]:
            for sj in key_to_svc.get(k, ()):
                if sj not in reachable:
                    reachable.add(sj)
                    stack.append(sj)

    from shapely.geometry import Point

    MAX_ROUTE = 90.0           # cap the route distance budgeted (m)
    RAISE_W = 14.0             # half-width of the truck-route corridor to raise

    # Apron nodes (x, y, idx) — for the route ANCHOR elevation (apron at the deep
    # end of the truck route) and for the apron-arm RAISE along the route; plus the
    # connector/service nodes (the corridor includes the connector itself).
    apron_pts = []
    pav_pts = []
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        if s.role == ROLE_APRON:
            tgt_apron = True
        elif s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION):
            tgt_apron = False
        else:
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(_key(x, y))
            if i is not None and i < len(elev):
                pav_pts.append((x, y, i))
                if tgt_apron:
                    apron_pts.append((x, y, i))
    centerlines = [cl.line for cl in
                   (getattr(layout, "apt_service_centerlines", None) or [])
                   if cl.line is not None and not cl.line.is_empty]

    def _nearest_apron_elev(px, py, tol=16.0):
        best = None
        for (ax, ay, ai) in apron_pts:
            d = math.hypot(ax - px, ay - py)
            if d <= tol and (best is None or d < best[0]):
                best = (d, elev[ai])
        return best[1] if best else None

    # ── Per apron-reachable connector: follow its TRUCK ROUTE to the apron ────
    # The route is the truck centerline through the connector; budget the reach
    # over its FULL length (groundside edge → apron base, ~55 m) against the apron's
    # elevation at that base — NOT just the connector's own span — so the groundside
    # can sit ``cap·route_len`` above the apron (user 2026-06-27).  Stash each route
    # (with its groundside-mouth arc + direction) for the RAISE pass.
    bounds: dict = {}          # id(g) -> [g, lo, hi]
    routes = []                # (id(g), ln, gm_s, apron_dir, route_len, dem_mouth)
    for si in reachable:
        c, _ks = svc[si]
        cnodes = [(x, y, bucket_to_idx.get(_key(x, y)))
                  for (x, y) in _open_ring(list(c.polygon.exterior.coords))]
        cen = c.polygon.centroid
        # the SHORTEST centerline that actually runs through this connector (avoid a
        # long through-airport route whose far end is hundreds of metres away).
        local = [L for L in centerlines if L.distance(cen) <= 8.0]
        ln = min(local, key=lambda L: L.length) if local else None
        for (g, kalt) in gs_pieces:
            gmouth = [(x, y) for (x, y, _i) in cnodes if _key(x, y) in kalt]
            if not gmouth:
                continue
            gmx = sum(p[0] for p in gmouth) / len(gmouth)
            gmy = sum(p[1] for p in gmouth) / len(gmouth)
            dem_gs = sum(kalt[_key(x, y)] for (x, y) in gmouth) / len(gmouth)
            gm_s = apron_dir = route_len = base_elev = None
            if ln is not None:
                gm_s = ln.project(Point(gmx, gmy))
                # apron side = the centerline end FARTHER from the groundside piece.
                e0, e1 = ln.coords[0], ln.coords[-1]
                apron_end_s = (0.0 if g.polygon.distance(Point(e0))
                               >= g.polygon.distance(Point(e1)) else ln.length)
                apron_dir = 1.0 if apron_end_s > gm_s else -1.0
                route_len = min(abs(apron_end_s - gm_s), MAX_ROUTE)
                bp = ln.interpolate(max(0.0, min(ln.length,
                                                 gm_s + apron_dir * route_len)))
                base_elev = _nearest_apron_elev(bp.x, bp.y)
            if base_elev is None:
                # Fallback: no usable centerline → reference the connector's own
                # apron-ward mouth, budget over its span (the earlier model).
                ref_nodes = [i for (x, y, i) in cnodes
                             if i is not None and i < len(elev)
                             and _key(x, y) not in gs_all_keys]
                if not ref_nodes:
                    continue
                base_elev = sum(elev[i] for i in ref_nodes) / len(ref_nodes)
                route_len = min(math.hypot(x - gmx, y - gmy)
                                for (x, y, i) in cnodes if i in ref_nodes)
                ln = None
            if route_len < 1e-6:
                continue
            budget = cap * route_len
            lo = base_elev - budget - dem_gs
            hi = base_elev + budget - dem_gs
            b = bounds.get(id(g))
            if b is None:
                bounds[id(g)] = [g, lo, hi]
            else:
                b[1] = max(b[1], lo)
                b[2] = min(b[2], hi)
            routes.append((id(g), ln, gm_s, apron_dir, route_len, dem_gs,
                           (gmx, gmy)))

    n = 0
    # Groundside-mouth points per piece (stashed with each route above) —
    # the anchor geometry for the mouth-decay relevel below.
    mouth_pts: dict = {}
    for (gid, _ln, _gm_s, _dir, _rl, _dm, (gmx, gmy)) in routes:
        mouth_pts.setdefault(gid, []).append((gmx, gmy))
    _mouth_decay = _os.environ.get(
        "O4_GROUNDSIDE_MOUTH_DECAY", "1") == "1"
    deltas: dict = {}
    for gid, (g, lo, hi) in bounds.items():
        # Closest-to-DEM shift inside the feasible band; if the connectors'
        # reaches don't overlap (no uniform shift keeps them all <=cap) fall back
        # to the band midpoint, which minimises the worst residual.
        delta = (min(max(0.0, lo), hi) if lo <= hi else 0.5 * (lo + hi))
        deltas[gid] = delta
        if abs(delta) < 1e-6:
            continue
        mpts = mouth_pts.get(gid) or []
        if _mouth_decay and mpts:
            # MOUTH-DECAY relevel (user 2026-07-04, CYXY lot #35): the
            # UNIFORM shift sank a 12 k m² lot 3.8 m below terrain
            # everywhere because its 53 m route can only climb
            # ``cap·53`` — but only the MOUTH must meet the road; the
            # lot interior is existing terrain-level pavement.  Each
            # node takes the shift the mouth needs, decayed toward zero
            # at ``cap`` per metre of distance from the nearest mouth —
            # the mouth still sits exactly at the reachable level (the
            # weld + RAISE below read the shifted ring), the interior
            # stays at DEM, and the in-between ramps at ≤cap.  A small
            # piece (everything within ``|delta|/cap`` of its mouth)
            # degenerates to the uniform shift.
            coords = list(g.polygon.exterior.coords)
            new_alts = []
            for k, a in enumerate(g.node_altitudes):
                if a is None:
                    new_alts.append(None)
                    continue
                x, y = coords[min(k, len(coords) - 1)]
                d = min(math.hypot(x - mx, y - my) for (mx, my) in mpts)
                mag = max(0.0, abs(delta) - cap * d)
                new_alts.append(a + math.copysign(mag, delta)
                                if mag > 0.0 else a)
            g.node_altitudes = new_alts
        else:
            g.node_altitudes = [
                (a + delta) if a is not None else None
                for a in g.node_altitudes]
        n += 1

    # CHORD-LIMIT every welded piece BEFORE the weld reads it (lockstep
    # with the post-solve ``_grade_limit_groundside_chords``): the weld
    # pins service-road nodes to these ring values, and the late limiter
    # rewrites the LOT ring only — two writers for the same physical
    # node left the road pinned 1.5 m off the emitted lot (CYXY #41,
    # 15 % road chords after emit consensus).  Limiting here makes the
    # solve-time field the FINAL field (the late pass is idempotent on
    # an already-limited ring).
    from auto_patch.groundside import chord_limit_ring_altitudes
    from auto_patch.config import GROUNDSIDE_MAX_GRADE
    for (g, _lo, _hi) in bounds.values():
        if not g.node_altitudes:
            continue
        g.node_altitudes = chord_limit_ring_altitudes(
            list(g.polygon.exterior.coords), g.node_altitudes,
            cap=GROUNDSIDE_MAX_GRADE)

    # ── LOT↔LOT WELD RECONCILIATION on service rings ─────────────────────
    # (user 2026-07-06, HECA service_road #522).  One road ring can weld to
    # TWO different lots whose re-levelled mouth values disagree beyond the
    # road cap * distance — an unfixable step between two hard welds (the
    # DEM-follow break blend only evaluates INTERIOR nodes, and both ends
    # are anchors).  Lots are FINAL at this point (only the connector reach
    # above moves them), so reconciling here is sound: the SMALLER lot
    # adopts the larger's ±cap·d band (largest-piece-first precedent
    # below), applied as a decay cone (fading at the groundside cap toward
    # the lot interior) so the ring stays Lipschitz and the chord limiter
    # stays idempotent.  Conflicts against BUILDING PADS / APRON bodies are
    # NOT handled here — those move later in the movable-pad yield
    # projection, so they are verified and relaxed post-yield instead
    # (``solve.py`` mouth verify-and-relax).
    if _os.environ.get("O4_GS_MOUTH_RECONCILE", "1") == "1":
        _BAND_MARGIN_M = 0.01      # stay inside the band after emit rounding
        svc_ring_pts = []          # per service shape: [(key, (x, y)), ...]
        for (_c, _ks) in svc:
            _pts = [(_key(x, y), (x, y))
                    for (x, y) in _open_ring(list(_c.polygon.exterior.coords))]
            svc_ring_pts.append(_pts)
        # Current (post-decay, post-limit) lot value per key; largest lot
        # owns a shared key, mirroring the gs_key_alt precedence below.
        lot_key_val: dict = {}     # key -> (area, lot shape, current value)
        for (g, _kalt) in sorted(gs_pieces,
                                 key=lambda t: -t[0].polygon.area):
            gcoords = list(g.polygon.exterior.coords)
            galts = list(g.node_altitudes or [])
            for kidx in range(min(len(gcoords), len(galts))):
                if galts[kidx] is None:
                    continue
                kk = _key(*gcoords[kidx])
                if kk not in lot_key_val:
                    lot_key_val[kk] = (g.polygon.area, g, float(galts[kidx]))
        # Collect per-lot clamp deltas from lot↔lot pairs that share a
        # service ring (the pair the within-shape law measures).
        adjustments: dict = {}     # id(lot) -> [lot, [((x, y), delta)]]

        def _clamp_into(target_list, pt, cur, lo_b, hi_b):
            tgt = min(max(cur, lo_b), hi_b)
            if abs(tgt - cur) > 1e-4:
                target_list.append((pt, tgt, tgt - cur))

        for _pts in svc_ring_pts:
            lots = [(k, p, lot_key_val[k]) for (k, p) in _pts
                    if k in lot_key_val]
            if len({id(v[1]) for (_k, _p, v) in lots}) < 2:
                continue
            for ai in range(len(lots)):
                for bi in range(ai + 1, len(lots)):
                    (_ka, pa, (aa, ga, va)) = lots[ai]
                    (_kb, pb, (ab, gb, vb)) = lots[bi]
                    if ga is gb:
                        continue   # same ring: its own chord limit governs
                    d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                    band = max(0.0, cap * d - _BAND_MARGIN_M)
                    if abs(va - vb) <= band:
                        continue
                    if aa >= ab:   # smaller lot adopts the larger's band
                        entry = adjustments.setdefault(id(gb), [gb, []])
                        _clamp_into(entry[1], pb, vb, va - band, va + band)
                    else:
                        entry = adjustments.setdefault(id(ga), [ga, []])
                        _clamp_into(entry[1], pa, va, vb - band, vb + band)
        n_reconciled = 0
        for (g, adjs) in adjustments.values():
            if not adjs:
                continue
            gcoords = list(g.polygon.exterior.coords)
            new_alts = list(g.node_altitudes)
            # ABSOLUTE Lipschitz support around each moved mouth (not a
            # relative delta cone): the ring near a mouth typically sits
            # exactly at the cap already, so ``old + (delta − cap·d)``
            # under-raises neighbours by the pre-existing slope and leaves
            # the mouth pair over cap (CYXY #184: an at-cap 4.00 % pair
            # re-emitted at 4.64 %).  Support = the new mouth value minus
            # (plus) cap·distance — the tightest field containing the
            # adopted mouth.
            for j in range(min(len(gcoords), len(new_alts))):
                if new_alts[j] is None:
                    continue
                xj, yj = gcoords[j]
                val = new_alts[j]
                for ((ax, ay), tgt, dv) in adjs:
                    dd = math.hypot(xj - ax, yj - ay)
                    if dv > 0.0:
                        val = max(val, tgt - GROUNDSIDE_MAX_GRADE * dd)
                    else:
                        val = min(val, tgt + GROUNDSIDE_MAX_GRADE * dd)
                new_alts[j] = val
            g.node_altitudes = chord_limit_ring_altitudes(
                gcoords, new_alts, cap=GROUNDSIDE_MAX_GRADE)
            n_reconciled += 1
        if n_reconciled and _os.environ.get("O4_STEP_DEBUG") == "1":
            print(f"  [groundside-reach] mouth reconciliation adjusted "
                  f"{n_reconciled} lot ring(s).")

    # (now-shifted) groundside altitude per key, for the weld.  LARGEST
    # piece first: where a big lot and a sliver connector piece share a
    # mouth key with different altitudes, the mouth serves the LOT
    # (user 2026-07-04, CYXY P4: welding to the 100 m² demoted
    # connector at 698.5 left the road 3 m under the 49 k m² lot).
    gs_key_alt: dict = {}
    gs_key_owner: dict = {}
    for (g, _kalt) in sorted(gs_pieces,
                             key=lambda t: -t[0].polygon.area):
        gcoords = list(g.polygon.exterior.coords)
        galts = list(g.node_altitudes)
        for k in range(min(len(gcoords), len(galts))):
            if galts[k] is not None:
                kk = _key(*gcoords[k])
                if kk not in gs_key_alt:
                    gs_key_alt[kk] = float(galts[k])
                    gs_key_owner[kk] = id(g)

    # ``hard`` = the returned truth-pin set.  Only WELDS go in it (shared
    # road/apron↔lot geometry takes the lot's value — physical identity).
    # The RAISE below writes elevation SEEDS but does NOT pin: a raised
    # taper value is a heuristic floor, and pinning it hard froze arm
    # nodes 1.3 m under the adjacent welded mouth (CYXY route D, 61 %
    # chord after planarize mixed the two fields into one ring) — the
    # post-reach projections grade the arm into the welds instead.
    hard: set = set()

    # ── RAISE the apron arm + connector along the truck route ────────────────
    # The narrow apron arm is welded to the connector, so as the connector climbs at
    # <=cap to the (now higher) groundside, that climb is carried BACK along the
    # truck route: every apron/connector node in the route corridor takes the
    # SELF-TAPERING profile ``gs_level − cap·(arc back from the groundside mouth)``.
    # The taper auto-stops where it drops below the apron's own elevation (the base),
    # so the raise is confined to the arm; the caller grades the apron body into it.
    for (gid, ln, gm_s, apron_dir, route_len, dem_mouth, (gmx, gmy)) in routes:
        delta = deltas.get(gid, 0.0)
        gs_level = dem_mouth + delta
        # The arm must rise whenever the groundside ends up ABOVE the apron base —
        # even when the piece was LOWERED toward a reachable level (delta < 0, its
        # DEM was higher than reachable).  The self-taper raises only where needed.
        if ln is None:
            continue
        for (px, py, pi) in pav_pts:
            p = Point(px, py)
            if ln.distance(p) > RAISE_W:
                continue
            # corridor membership = along the route (apron side, within route_len);
            # but the PROFILE tapers by STRAIGHT distance from the groundside mouth,
            # so the connector rect (graded on its straight span, not the curved
            # centerline arc) comes out at exactly <=cap, not the arc-inflated rate.
            s = ln.project(p)
            if (s - gm_s) * apron_dir < -2.0 or (s - gm_s) * apron_dir \
                    > route_len + 5.0:
                continue
            straight = math.hypot(px - gmx, py - gmy)
            tgt = gs_level - cap * straight
            if tgt > elev[pi] + 1e-3:
                elev[pi] = tgt
                if _os.environ.get("O4_GS_RAISE_HARD", "0") == "1":
                    hard.add(pi)      # legacy: raised taper values pinned

    # ── WELD each connector's groundside mouth to the shifted groundside ─────
    # Reachable connectors weld as before.  An UNREACHABLE connector still
    # welds where its truck ROUTE ENDS at the lot — a destination road must
    # CLIMB to the lot it serves (user 2026-07-04, CYXY P4: the road emitted
    # 3.1 m below the lot at coincident nodes).  Blanket-welding every
    # unreachable lot-touching connector measured +215 within-shape pairs
    # (mouth pins fighting DEM-followed road surfaces mid-network); the
    # route-END scope pins only the served destination mouth.
    route_end_points = []
    for ln in centerlines:
        try:
            route_end_points.append(Point(*ln.coords[0]))
            route_end_points.append(Point(*ln.coords[-1]))
        except (ValueError, IndexError):
            continue
    # Coordinate keys this pass welded (rounded like the emit consensus) —
    # persisted on the layout so the POST-solve groundside chord limiter
    # can re-adopt its re-limited values onto exactly these nodes (and no
    # others: a road passing a DEM-stay lot keeps its by-design seam).
    weld_coord_keys: set = set()
    for si in range(len(svc)):
        c, _ks = svc[si]
        is_reachable = si in reachable
        for (x, y) in _open_ring(list(c.polygon.exterior.coords)):
            k = _key(x, y)
            a = gs_key_alt.get(k)
            if a is None:
                continue
            if not is_reachable:
                p = Point(x, y)
                if not any(p.distance(ep) <= 15.0
                           for ep in route_end_points):
                    continue
            i = bucket_to_idx.get(k)
            if i is not None and i < len(elev):
                elev[i] = a
                hard.add(i)
                weld_coord_keys.add((round(x, 2), round(y, 2)))

    # ── WELD every pavement node ON a re-levelled lot ring ───────────────────
    # The svc-ring weld above misses the MOUTH vertex when it lives on the
    # APRON arm instead of a service shape (CYXY route D: the shared lot
    # vertex belonged to the apron at solve time, the RAISE floored it
    # 1.3 m under the lot's welded level, and post-solve planarize copied
    # that value into the road ring → 15 % mixed-field chords).  The
    # road↔lot connection is FIRST-CLASS shared geometry no matter which
    # role carries the vertex: any solver node whose canonical key lies on
    # a re-levelled piece's ring takes that ring's value.  Scoped to
    # pieces the reach actually processed (``bounds``) — pieces with no
    # reachable connector stay DEM and pin nothing (the blanket-weld
    # regression class, +215).
    relevelled_gids = {gid for gid in bounds}
    if _os.environ.get("O4_GS_PAV_WELD", "1") == "1":
        for (px, py, pi) in pav_pts:
            k = _key(px, py)
            a = gs_key_alt.get(k)
            if a is None or gs_key_owner.get(k) not in relevelled_gids:
                continue
            if pi < len(elev):
                elev[pi] = a
                hard.add(pi)
                weld_coord_keys.add((round(px, 2), round(py, 2)))
    layout._groundside_weld_keys = weld_coord_keys
    return n, hard


def _svc_spine_station_seeds(layout, svc_nodes, node_pos, anchors,
                             dem_elev, cap, node_ceil, node_floor,
                             node_ceil_dist, node_floor_dist,
                             prox_pairs=()):
    """SPINE-FIRST seed field (config.SVC_SPINE_FIRST, part 30m): the service
    network's DEM-follow computed per spine STATION and shared by the whole
    cross-section, instead of per ring vertex.

    Per-vertex DEM-follow let a road's two long edges bind to DIFFERENT
    anchor regimes (each side clamps into the reach band of ITS nearest
    welds), which rendered a cross-road tear — CYXY 2.49 m at
    60.7092306,-135.0738928.  Here the ROAD HUGS TERRAIN LONGITUDINALLY
    within its cap along the spine, and every ring vertex of a cross-section
    takes the SAME station value, so a tear across the road cannot even be
    seeded.  These are SEEDS ONLY (soft): the road's within-shape law edges
    (``grade_graph.SOFT_VISIBILITY_ROLES`` + the service lateral pass, same
    gate) are the authority and the solve's final projections remain the
    sole writer.

    Mechanism, mirroring the per-vertex operator 1:1 but on stations:
      * stations = clusters of the service ring vertices' perpendicular
        projections onto the service (truck-route) centerlines — the spine
        arclength is the station coordinate, so opposite-edge partners
        (aligned by ``insert_service_lateral_nodes``) share one station;
      * station DEM = mean vertex DEM of the cluster, LOW-PASSED along the
        line (±~1.5 station steps) — the seed follows terrain at station
        wavelength, not raster noise (a lone unpaired station otherwise
        imprints its own DEM sample as a cross/diagonal step);
      * station band = the INTERSECTION of the member vertices' node-graph
        reach bands (``[max member floor, min member ceil]``) — the SAME
        cap-Lipschitz reach the per-vertex operator used, so connectivity
        to the mouth welds is inherited from the proven node graph (an
        earlier separate station-graph Dijkstra left whole chains
        anchor-unreachable), while the INTERSECTION makes both edges obey
        BOTH sides' anchors at once;
      * clamp + the SAME distance-weighted break blend as the per-vertex
        path (an empty intersection is exactly the old two-regime
        contradiction, now surfaced once per cross-section); broken
        stations quarantine their members through the existing
        ``service_break`` machinery.

    Returns ``(node_target, broken_nodes)``: seed values for the non-anchor
    vertices that found a station (vertices with no spine within reach — wide
    service-junction yards — keep the legacy per-vertex path), and the subset
    belonging to genuinely broken stations."""
    import math as _m
    from auto_patch.config import ROAD_CARVE_MAX_WIDTH_M, SPINE_STEP_M

    try:
        from shapely.geometry import LineString, Point
        from shapely.strtree import STRtree
    except Exception:                                   # pragma: no cover
        return {}, set()

    lines = []
    for cl in (getattr(layout, "apt_taxi_centerlines", None) or []):
        if not getattr(cl, "is_service", False):
            continue
        ln = getattr(cl, "line", None)
        if ln is None or getattr(ln, "is_empty", True):
            continue
        try:
            cs = list(ln.coords)
        except Exception:
            continue
        if len(cs) >= 2:
            lines.append(LineString(cs))
    if not lines:
        return {}, set()

    R = ROAD_CARVE_MAX_WIDTH_M / 2.0 + 2.0
    tree = STRtree(lines)

    # node → (line_idx, arclength) for the nearest service line within R.
    node_station_raw: dict = {}
    for i in sorted(svc_nodes):
        p = node_pos.get(i)
        if p is None:
            continue
        P = Point(p)
        try:
            cand = tree.query(P.buffer(R))
        except Exception:
            continue
        best = None
        for qi in cand:
            li = int(qi)
            d = lines[li].distance(P)
            if d <= R and (best is None or d < best[0]):
                best = (d, li, lines[li].project(P))
        if best is not None:
            node_station_raw[i] = (best[1], best[2])
    if not node_station_raw:
        return {}, set()

    # Cluster per-line arclengths into stations (cross-section partners
    # project to near-identical s; 2.0 m absorbs foot/weld noise while
    # staying far under the ~12 m station spacing).
    _CLUSTER_GAP_M = 2.0
    by_line: dict = {}
    for i, (li, s) in node_station_raw.items():
        by_line.setdefault(li, []).append((s, i))
    stations: list = []          # station → dict(line, s, members)
    node_station: dict = {}
    for li, lst in by_line.items():
        lst.sort()
        cur = None
        for (s, i) in lst:
            if cur is None or s - cur["s_max"] > _CLUSTER_GAP_M:
                cur = {"line": li, "s_sum": 0.0, "s_max": s, "n": 0,
                       "members": []}
                stations.append(cur)
            cur["s_sum"] += s
            cur["s_max"] = max(cur["s_max"], s)
            cur["n"] += 1
            cur["members"].append(i)
            node_station[i] = len(stations) - 1
    for st in stations:
        st["s"] = st["s_sum"] / st["n"]

    # Station XY + per-line ordered station lists.
    st_xy = {}
    for sid, st in enumerate(stations):
        q = lines[st["line"]].interpolate(st["s"])
        st_xy[sid] = (q.x, q.y)
    by_line_sid: dict = {}
    for sid, st in enumerate(stations):
        by_line_sid.setdefault(st["line"], []).append(sid)

    # PARALLEL-ROAD STATION MERGE — the station-level analogue of the node
    # graph's O4_SVC_PROXIMITY_COUPLE (part 27, HECA #510↔#517): two service
    # lines running < ~2 m apart carry separate station chains, so each
    # road's cross-section would seed from ITS line alone and the pair can
    # re-open the metre-scale wall the node coupling closed (measured at
    # HECA #576↔#584: cross-shape 0.16 m → 0.84 m without this merge).
    # Stations of DIFFERENT lines within the window share ONE merged member
    # set → one DEM mean, one band intersection, one target.  Union-find;
    # the merged station keeps the first sid as root.
    _PROX_M = 2.0
    parent = list(range(len(stations)))

    def _find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    _grid: dict = {}
    for sid, (x, y) in st_xy.items():
        _grid.setdefault((int(x // _PROX_M), int(y // _PROX_M)),
                         []).append(sid)
    for (cx, cy), cell in _grid.items():
        neigh = []
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                neigh.extend(_grid.get((cx + ox, cy + oy), ()))
        for a in cell:
            ax, ay = st_xy[a]
            for b in neigh:
                if b <= a or stations[b]["line"] == stations[a]["line"]:
                    continue
                bx, by = st_xy[b]
                if _m.hypot(ax - bx, ay - by) <= _PROX_M:
                    ra, rb = _find(a), _find(b)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
    # … and through the NODE couples (the exact part-27 proximity notion):
    # two parallel lines' stations are longitudinally OFFSET in general, so
    # the XY merge above can miss them (HECA #576↔#584 stayed 0.84 m apart
    # with XY-merge alone) — but their RING nodes across the sliver are
    # coupled, and coupled nodes' stations must share one cross-section.
    for (i, j) in prox_pairs:
        si, sj = node_station.get(i), node_station.get(j)
        if si is None or sj is None or si == sj:
            continue
        ra, rb = _find(si), _find(sj)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    _merged = 0
    for sid in range(len(stations)):
        r = _find(sid)
        if r != sid:
            stations[r]["members"].extend(stations[sid]["members"])
            for i in stations[sid]["members"]:
                node_station[i] = r
            stations[sid]["members"] = []
            _merged += 1

    # Raw station DEM = mean member DEM; then LOW-PASS along each line so a
    # lone unpaired station cannot imprint a raster-noise step on the seed
    # (measured at CYXY -10193: adjacent raw stations 718.86/719.07/718.99
    # → a 4.4 % diagonal pair the projections had already frozen into the
    # clearance welds by emit time).
    raw_de: dict = {}
    for sid, st in enumerate(stations):
        dems = [dem_elev[i] for i in st["members"]
                if i < len(dem_elev) and dem_elev[i] is not None]
        if dems:
            raw_de[sid] = sum(dems) / len(dems)
    _SMOOTH_M = 1.5 * SPINE_STEP_M
    smooth_de: dict = {}
    for li, sids in by_line_sid.items():
        sids.sort(key=lambda k: stations[k]["s"])
        with_de = [k for k in sids if k in raw_de]
        for k in with_de:
            s0 = stations[k]["s"]
            window = [raw_de[j] for j in with_de
                      if abs(stations[j]["s"] - s0) <= _SMOOTH_M]
            smooth_de[k] = sum(window) / len(window)

    # Station reach band = INTERSECTION of the member vertices' node-graph
    # bands — the same anchors, the same cap-Lipschitz metric, the proven
    # connectivity (ring edges + proximity couples), but binding BOTH edges
    # of the cross-section to BOTH sides' anchors at once.
    import os as _os
    _dbg_spec = _os.environ.get("O4_SVC_SPINE_DEBUG_LL")
    _dbg_xy = None
    if _dbg_spec:
        try:
            _dla, _dlo = (float(v) for v in _dbg_spec.split(","))
            _dbg_xy = layout.ll_to_m(_dla, _dlo)
        except Exception:
            _dbg_xy = None

    node_target: dict = {}
    broken_nodes: set = set()
    for sid, st in enumerate(stations):
        de = smooth_de.get(sid)
        if de is None:
            continue                    # no DEM sample → legacy per-vertex
        m_ceil = [node_ceil[i] for i in st["members"] if i in node_ceil]
        m_floor = [node_floor[i] for i in st["members"] if i in node_floor]
        c = min(m_ceil) if m_ceil else None
        f = max(m_floor) if m_floor else None
        broken = False
        if c is None:                   # unreachable from any anchor → DEM
            tgt = de
        elif f is not None and f > c + 1e-9:
            # genuine break — SAME distance-weighted blend as the
            # per-vertex operator, computed once for the cross-section
            # (weights = mean member reach distances to each regime).
            dcs = [node_ceil_dist[i] for i in st["members"]
                   if i in node_ceil_dist]
            dfs = [node_floor_dist[i] for i in st["members"]
                   if i in node_floor_dist]
            dc = (sum(dcs) / len(dcs)) if dcs else 0.0
            df = (sum(dfs) / len(dfs)) if dfs else 0.0
            t = dc / (dc + df) if (dc + df) > 1e-9 else 0.5
            tgt = c + (f - c) * t
            broken = True
        else:
            lo = f if f is not None else -float("inf")
            tgt = min(max(de, lo), c)
        for i in st["members"]:
            node_target[i] = tgt
            if broken:
                broken_nodes.add(i)
        if _dbg_xy is not None:
            sx, sy = st_xy[sid]
            if _m.hypot(sx - _dbg_xy[0], sy - _dbg_xy[1]) < 12.0:
                print(f"    [svc-spine-dbg] sid={sid} line={st['line']} "
                      f"s={st['s']:.1f} n={st['n']} de_raw={raw_de.get(sid)} "
                      f"de={de:.2f} ceil={c} floor={f} "
                      f"tgt={tgt:.2f} broken={broken} "
                      f"members={sorted(st['members'])}")
    return node_target, broken_nodes


def apply_service_road_dem_follow(layout, bucket_to_idx, elev, dem_elev, cap,
                                  anchor_extra=()):
    """Grade the service-road network to FOLLOW DEM at <=cap (user 2026-06-27).

    A ground-vehicle road is NOT airside: it rises/falls toward terrain, anchored
    only where it WELDS to the airside (taxi/apron/runway, kept at their solved
    bowl elevation) or to a groundside piece (``anchor_extra``).  Every other
    service node sits at ``clamp(DEM, reach-band-from-anchors-at-cap)`` where the
    reach band is the cap-Lipschitz envelope along the SERVICE graph (axial, edge by
    edge) — so a road ramps from its airside connection toward DEM at <=4% instead
    of being held flat in the bowl (SVC4 was ~6-11 m below terrain).  The
    road-vs-airside seam is by design (``check_grade._airside_groundside_pair``), so
    rising past a flat neighbour is not a step.

    SPINE-FIRST (config.SVC_SPINE_FIRST, default ON, part 30m): the DEM target
    is computed per spine STATION (shared by the whole cross-section) instead
    of per vertex — see ``_svc_spine_station_seeds``.  ``O4_SVC_SPINE_FIRST=0``
    restores the per-vertex behaviour below byte-identically.

    Mutates ``elev`` in place; returns the set of node indices it moved."""
    import heapq
    import os as _os
    from collections import defaultdict
    from auto_patch.layout import (
        ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION, ROLE_GROUNDSIDE_PAVEMENT)

    cps = layout.canonical_points

    def _key(x, y):
        return cps.get_or_add(float(x), float(y))

    SVC = (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)
    svc_nodes: set = set()
    adj = defaultdict(list)
    node_pos: dict = {}
    node_shape: dict = {}
    for s in layout.shapes:
        if s.role not in SVC or s.polygon is None or s.polygon.is_empty:
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        idxs = [bucket_to_idx.get(_key(x, y)) for (x, y) in ring]
        for k in range(len(ring)):
            i, j = idxs[k], idxs[(k + 1) % len(ring)]
            if i is None or i >= len(elev):
                continue
            svc_nodes.add(i)
            node_pos.setdefault(i, ring[k])
            node_shape.setdefault(i, id(s))
            if j is not None and j != i and j < len(elev):
                import math as _m
                dd = _m.hypot(ring[k][0] - ring[(k + 1) % len(ring)][0],
                              ring[k][1] - ring[(k + 1) % len(ring)][1])
                adj[i].append((j, dd))
                adj[j].append((i, dd))
    if not svc_nodes:
        return set()

    # PROXIMITY COUPLING between near-parallel roads (user 2026-07-06,
    # HECA #510↔#517): two service shapes whose free edges run < ~2 m
    # apart carry NO shared node, so each grades to its OWN anchors and
    # the pair can emit a metre-scale wall across an unrenderable sliver
    # (measured 1.8 m over 0.9 m).  Couple nodes of DIFFERENT service
    # shapes within the window into the reach graph — both roads then
    # grade against the union of their anchors at ≤cap across the gap,
    # and genuinely contradictory anchors resolve through the same
    # break blend as any interior node.
    prox_pairs: list = []       # (i, j) couples — also merges spine stations
    if _os.environ.get("O4_SVC_PROXIMITY_COUPLE", "1") == "1":
        import math as _m
        _PROX_M = 2.0
        _cell = _PROX_M
        _grid: dict = {}
        for i, (px, py) in node_pos.items():
            _grid.setdefault((int(px // _cell), int(py // _cell)),
                             []).append(i)
        for (cx, cy), members in _grid.items():
            neighbors = []
            for ox in (-1, 0, 1):
                for oy in (-1, 0, 1):
                    neighbors.extend(_grid.get((cx + ox, cy + oy), ()))
            for i in members:
                (ix, iy) = node_pos[i]
                for j in neighbors:
                    if (j <= i
                            or node_shape.get(j) == node_shape.get(i)):
                        continue
                    (jx, jy) = node_pos[j]
                    dd = _m.hypot(ix - jx, iy - jy)
                    if 1e-6 < dd <= _PROX_M:
                        adj[i].append((j, dd))
                        adj[j].append((i, dd))
                        prox_pairs.append((i, j))

    # Anchors = service nodes that are ALSO a corner of a NON-service pavement shape
    # (the road welds to the airside there), held at their solved elevation; plus
    # any groundside-welded nodes passed in.
    anchors: dict = {}
    for s in layout.shapes:
        if (s.role in SVC or s.role == ROLE_GROUNDSIDE_PAVEMENT
                or s.polygon is None or s.polygon.is_empty):
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(_key(x, y))
            if i in svc_nodes:
                anchors[i] = elev[i]
    for i in anchor_extra:
        if i in svc_nodes and i < len(elev):
            anchors[i] = elev[i]

    def _reach(sign):                       # +1 → ceil, −1 → floor
        # Lazy Dijkstra over the (positive) cap·distance metric: the heap
        # pops each node first at its OPTIMAL value, so every later pop
        # is skipped (>= / <=, NO epsilon — an epsilon-tolerant skip lets
        # equal-value duplicates re-expand, which goes combinatorial on
        # service networks with many equal-length parallel paths: CYXY
        # hung for 27 min here).  Each node therefore expands exactly
        # once and pushes are bounded by the edge count.
        best: dict = {}
        dist: dict = {}                     # graph distance to the
        pq = [((av if sign > 0 else -av), 0.0, a)   # value-optimal anchor
              for a, av in anchors.items()]
        heapq.heapify(pq)
        while pq:
            v, dk, k = heapq.heappop(pq)
            t = v if sign > 0 else -v
            if k in best:
                continue
            best[k] = t
            dist[k] = dk
            for (j, dd) in adj[k]:
                if j in best:
                    continue
                nt = t + sign * cap * dd
                heapq.heappush(pq, ((nt if sign > 0 else -nt),
                                    dk + dd, j))
        return best, dist

    ceil, ceil_dist = _reach(+1) if anchors else ({}, {})
    floor, floor_dist = _reach(-1) if anchors else ({}, {})
    _dbg_spec = _os.environ.get("O4_SVC_DEBUG_LL")
    if _dbg_spec:
        try:
            import math as _dbg_m
            _dla, _dlo = (float(v) for v in _dbg_spec.split(","))
            _dx, _dy = layout.ll_to_m(_dla, _dlo)
            for _i in sorted(svc_nodes):
                _p = node_pos.get(_i)
                if _p is None or _dbg_m.hypot(_p[0] - _dx,
                                              _p[1] - _dy) > 8.0:
                    continue
                print(f"    [svc-dbg] i={_i} pos=({_p[0]:.1f},{_p[1]:.1f})"
                      f" anchor={_i in anchors}"
                      f" elev={elev[_i]:.2f}"
                      f" dem={dem_elev[_i] if _i < len(dem_elev) else None}"
                      f" ceil={ceil.get(_i)} floor={floor.get(_i)}")
        except Exception as _e:
            print(f"    [svc-dbg] error {_e!r}")
    changed: set = set()
    # BREAK-BLEND EXPORT (user 2026-07-06, handover fix (b)): nodes whose
    # welded anchors contradict (floor > ceil) render the designed blend
    # below — persist them so the caller can quarantine their over-cap
    # pairs/steps instead of reporting the contained blend as actionable
    # (HECA #578↔#64: a junction weld 1 m from a road capped 0.8 m lower).
    service_break: set = getattr(layout, "_service_break_idx", None) or set()
    layout._service_break_idx = service_break
    # SPINE-FIRST (config.SVC_SPINE_FIRST, part 30m): DEM-follow computed per
    # spine STATION and shared by the whole cross-section — see
    # ``_svc_spine_station_seeds``.  Vertices with no station (wide
    # service-junction yards beyond spine reach) keep the legacy per-vertex
    # path below; anchor (weld) vertices are never reseeded on either path.
    from auto_patch.config import SVC_SPINE_FIRST as _SPINE_FIRST
    spine_target: dict = {}
    spine_broken: set = set()
    if _SPINE_FIRST:
        spine_target, spine_broken = _svc_spine_station_seeds(
            layout, svc_nodes, node_pos, anchors, dem_elev, cap,
            ceil, floor, ceil_dist, floor_dist, prox_pairs)
    for i in svc_nodes:
        if i in anchors:
            continue
        if i in spine_target:
            tgt = spine_target[i]
            if i in spine_broken:
                service_break.add(i)
            if abs(tgt - elev[i]) > 1e-3:
                elev[i] = tgt
                changed.add(i)
            continue
        de = dem_elev[i] if i < len(dem_elev) else None
        if de is None:
            continue
        c = ceil.get(i)
        f = floor.get(i)
        if c is None:                       # unreachable from any anchor → DEM
            tgt = de
        elif f is not None and f > c + 1e-9:
            # GENUINE break: the road's welded anchors (airside mouth vs
            # groundside/other weld) contradict through this node — no
            # <=cap profile connects them (user 2026-07-04: break-blend
            # support for service roads).  Same operator as
            # ``feasibility_project``'s broken-node fill: the
            # distance-weighted blend puts the surface ON the descent
            # field of each anchor at that anchor (t→0 ⇒ z=ceil field,
            # t→1 ⇒ z=floor field, continuous at the region boundary)
            # and spreads the deficit between them as one gentle
            # over-cap ramp.  Ceiling-clamping instead (the previous
            # behaviour, silently) parked the WHOLE deficit as a wall
            # at the floor-side anchor — typically the groundside mouth.
            dc = ceil_dist.get(i, 0.0)
            df = floor_dist.get(i, 0.0)
            t = dc / (dc + df) if (dc + df) > 1e-9 else 0.5
            tgt = c + (f - c) * t
            service_break.add(i)
        else:
            lo = f if f is not None else -float("inf")
            tgt = min(max(de, lo), c)
        if abs(tgt - elev[i]) > 1e-3:
            elev[i] = tgt
            changed.add(i)
    return changed


def _groundside_lot_rings(layout, bucket_to_idx):
    """Per groundside lot with per-vertex altitudes: the ring vertex list
    ``[(ring_index, solver_index_or_None, (x, y)), ...]`` (open ring)."""
    from auto_patch.layout import ROLE_GROUNDSIDE_PAVEMENT
    cps = layout.canonical_points
    out = []
    for g in layout.shapes:
        if (g.role != ROLE_GROUNDSIDE_PAVEMENT or g.polygon is None
                or g.polygon.is_empty or not g.node_altitudes):
            continue
        coords = list(g.polygon.exterior.coords)
        verts = []
        for j in range(min(len(coords), len(g.node_altitudes))):
            x, y = coords[j]
            idx = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            verts.append((j, idx, (float(x), float(y))))
        out.append((g, verts))
    return out


def expand_mouth_cluster(layout, bucket_to_idx, conflicted, welded_idx,
                         window_m: float = 12.0):
    """Grow a conflicted-mouth set to the full mouth CLUSTER: every welded
    solver node on the SAME groundside lot ring within ``window_m`` of a
    conflicted node.  Freeing the whole cluster lets the joint solve place
    one consistent mouth profile instead of wedging a single freed vertex
    between its still-hard neighbours."""
    import math as _m
    freed = set(conflicted)
    for (_g, verts) in _groundside_lot_rings(layout, bucket_to_idx):
        ring_welded = [(j, idx, p) for (j, idx, p) in verts
                       if idx is not None and idx in welded_idx]
        seeds = [(j, idx, p) for (j, idx, p) in ring_welded
                 if idx in conflicted]
        if not seeds:
            continue
        for (_j, idx, p) in ring_welded:
            if idx in freed:
                continue
            if any(_m.hypot(p[0] - sp[0], p[1] - sp[1]) <= window_m
                   for (_sj, _si, sp) in seeds):
                freed.add(idx)
    return freed


def adopt_projected_mouths(layout, bucket_to_idx, elev, freed, welded_idx):
    """LOT ADOPTS THE SOLVED MOUTH (user 2026-07-06, HECA #541/#546): after
    the mouth verify-and-relax re-projection, write the projected values of
    the freed mouth vertices back onto their groundside lot rings — exact at
    each freed vertex, cap-decay filled across non-welded ring vertices.
    Non-freed welded vertices are held fixed during the fill (their solver
    values did not move).  Deliberately NO chord-limit here: the downward-
    only limiter would drag an adopted-high mouth toward the lot's low DEM
    interior (measured: HECA #522 mouth 103.9 → 101.8, a 2.1 m weld tear);
    ring lawfulness stays with the post-solve groundside chord limiter,
    which re-adopts welded values properly.  Returns the count of adopted
    lot rings."""
    import math as _m
    from auto_patch.config import GROUNDSIDE_MAX_GRADE
    n_adopted = 0
    for (g, verts) in _groundside_lot_rings(layout, bucket_to_idx):
        alts = list(g.node_altitudes)
        freed_verts = [(j, idx, p) for (j, idx, p) in verts
                       if idx is not None and idx in freed
                       and j < len(alts) and alts[j] is not None]
        if not freed_verts:
            continue
        held = {j for (j, idx, _p) in verts
                if idx is not None and idx in welded_idx
                and idx not in freed}
        # ABSOLUTE Lipschitz support around each adopted mouth (see the
        # reach-time reconciliation for why a relative delta cone is
        # wrong: an at-cap ring re-emits over cap).
        sources = [(p, float(elev[idx]), float(elev[idx]) - float(alts[j]))
                   for (j, idx, p) in freed_verts]
        new_alts = list(alts)
        for (j, _idx, p) in [(j, i, p) for (j, i, p) in verts
                             if j < len(alts) and alts[j] is not None]:
            if j in held:
                continue
            val = float(alts[j])
            for (fp, tgt, dv) in sources:
                dd = _m.hypot(p[0] - fp[0], p[1] - fp[1])
                if dv > 0.0:
                    val = max(val, tgt - GROUNDSIDE_MAX_GRADE * dd)
                elif dv < 0.0:
                    val = min(val, tgt + GROUNDSIDE_MAX_GRADE * dd)
            new_alts[j] = val
        # exact adoption at the freed vertices themselves
        for (j, idx, _p) in freed_verts:
            new_alts[j] = float(elev[idx])
        # keep a closed ring closed (mirrors chord_limit's own handling)
        coords = list(g.polygon.exterior.coords)
        if (len(new_alts) == len(coords) and len(coords) > 1
                and tuple(coords[0]) == tuple(coords[-1])
                and new_alts[0] is not None):
            new_alts[-1] = new_alts[0]
        g.node_altitudes = new_alts
        n_adopted += 1
    return n_adopted


def apron_body_nodes(layout, bucket_to_idx):
    """Node indices that follow DEM (apron bodies + service roads/junctions) and
    are NOT part of the taxi route — closest-to-DEM target, no taxi-band bound.
    The rest of airside is the taxi route (smooth, band-bounded)."""
    cps = layout.canonical_points
    body: set = set()
    route: set = set()
    for s in layout.shapes:
        if s.polygon is None or s.polygon.is_empty:
            continue
        if s.role in _DEM_BODY_ROLES:
            tgt = body
        elif s.role in _ROUTE_ROLES:
            tgt = route
        else:
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            i = bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
            if i is not None:
                tgt.add(i)
    return body - route
