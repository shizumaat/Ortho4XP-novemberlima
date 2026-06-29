"""Validate the AS-BUILT within-shape grade with the unified grade graph.

This is the validator side of the single grade graph (docs/single_grade_graph.md):
it builds the SAME :mod:`auto_patch.grade_graph` constraints the solver used —
from the emitted ``layout.shapes`` (apron/junction rings + their elevations) —
and checks each constrained pair against the realised surface.  Because both the
solver and this validator call ``grade_graph.shape_constraints``, the surface we
BUILD and the surface we CHECK cannot drift for apron/junction shapes.

Rects / runways / terminals / groundside are NOT owned by the grade graph; the
caller's existing per-role audit keeps validating those.
"""
from __future__ import annotations

import dataclasses as _dc
import math

from . import grade_graph as GG
from .config import ELEV_ROUNDING_NOISE_M, taxi_grade_cap_for_letter


from .grade_graph import _open_ring


def _shape_elevs(s, n):
    """Per-vertex emitted elevations for a shape ring (open, length n) or None."""
    if s.altitude is not None:
        return [float(s.altitude)] * n
    na = s.node_altitudes
    if na is not None:
        na = list(na)
        if len(na) == n + 1:
            na = na[:-1]
        if len(na) == n and all(e is not None for e in na):
            return [float(e) for e in na]
    # Sloping 4-corner rect emitted as a tilted plane: the canonical convention
    # (``_writeback``/``_canonicalise_rect``) is corners [0,3]=HIGH, [1,2]=LOW.
    if (s.altitude_high is not None and s.altitude_low is not None and n == 4):
        hi, lo = float(s.altitude_high), float(s.altitude_low)
        return [hi, lo, lo, hi]
    return None


def _iter_checked_pairs(layout):
    """Yield EVERY within-shape constrained pair the validator checks, as
    ``(role, is_spine, (xa, ya), za, (xb, yb), zb, cap)``:

      * apron / junction within-shape edges (body + spine, ``grade_graph``);
      * sloping-rect + end-cap ALL-pair edges (the taxi spine as tilted planes).

    The single source both ``within_violations`` (applies the cap check) and
    ``checked_spine_geometry`` (extracts the spine node/edge set for the
    structural test) consume, so the checker and the structural gate cannot
    drift from each other.  Runway joins are handled separately (one side is a
    runway-surface sample, not a node)."""
    from auto_patch.junction_rules import SLOPING_RECT_ROLES
    from auto_patch.layout import taxi_shape_code_letter
    ctx = GG.build_context(layout)

    # apron / junction within-shape (body + spine).
    for s in layout.shapes:
        if (s.role not in GG.SOFT_VISIBILITY_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        nlen = len(ring)
        if nlen < 3:
            continue
        elevs = _shape_elevs(s, nlen)
        if elevs is None:
            continue
        gs = GG.GradeShape(role=s.role, ring=[(x, y) for (x, y) in ring],
                           keys=list(range(nlen)))
        # Activate the building-step exemption in INDEX key-space: ctx.building_keys
        # are rounded coords (validator mode) but this shape's keys are ring
        # indices, so resolve which ring vertices sit on a building pad and pass a
        # per-shape index set.  Matches the solver (whose global node-index keys
        # make the exemption active) and the grade test (check_grade, nid keys).
        bld_idx = frozenset(
            i for i, (x, y) in enumerate(ring)
            if (round(x, 3), round(y, 3)) in ctx.building_keys)
        ctx_s = _dc.replace(ctx, building_keys=bld_idx) if bld_idx else ctx
        sc = GG.shape_constraints(gs, ctx_s)
        spine_pairs = set()
        for chain in sc.spine_chains:
            for a, b in zip(chain, chain[1:]):
                spine_pairs.add((min(a, b), max(a, b)))
        for (a, b, cap) in sc.edges:
            is_spine = (min(a, b), max(a, b)) in spine_pairs
            yield (s.role, is_spine, ring[a], elevs[a], ring[b], elevs[b], cap)

    # sloping rects + end-caps (all-pair, the taxi spine as tilted planes).
    rect_caps = []                      # (corner_key_set, cap)
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        if len(ring) < 3:
            continue
        cap = float(taxi_grade_cap_for_letter(taxi_shape_code_letter(layout, s)))
        rect_caps.append(({(round(x, 3), round(y, 3)) for (x, y) in ring}, cap))
        yield from _all_pair_pairs(s.role, ring, _shape_elevs(s, len(ring)),
                                   cap, ctx)
    for s in layout.shapes:
        if (not getattr(s, "is_rect_cap", False) or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        if len(ring) < 3:
            continue
        ckeys = {(round(x, 3), round(y, 3)) for (x, y) in ring}
        cap, best = None, 0
        for (rkeys, rcap) in rect_caps:
            sh = len(rkeys & ckeys)
            if sh > best:
                best, cap = sh, rcap
        if cap is None:                 # unmatched cap → uniform taxi cap
            cap = float(taxi_grade_cap_for_letter(None))
        yield from _all_pair_pairs("rect_cap", ring, _shape_elevs(s, len(ring)),
                                   cap, ctx)


def _all_pair_pairs(role, ring, elevs, cap, ctx):
    """Plane shape (rect / cap) pairs via the SHARED plane rule
    (``grade_graph.plane_constraints`` → ``grade_law``) — the same one the solver
    enforces (``build_unified_graph``) and the OSM test checks (``check_grade``)."""
    if elevs is None:
        return
    n = len(ring)
    gs = GG.GradeShape(role=role, ring=[(x, y) for (x, y) in ring],
                       keys=list(range(n)))
    for (a, b, c) in GG.plane_constraints(gs, ctx, cap).edges:
        yield (role, True, ring[a], elevs[a], ring[b], elevs[b], c)


def within_violations(layout, noise=ELEV_ROUNDING_NOISE_M):
    """Return the apron/junction within-shape grade violations of the emitted
    ``layout``, as ``[(pct, cap, dist, role, is_spine, x, y), ...]`` (worst
    first).  Uses the unified grade graph — identical constraints to the solver.
    """
    viol = []
    for (role, is_spine, (xa, ya), za, (xb, yb), zb, cap) in \
            _iter_checked_pairs(layout):
        d = math.hypot(xa - xb, ya - yb)
        if d < 1e-6:
            continue
        de = abs(za - zb)
        # ``cap`` is a grade_law.Allowance; the per-pair budget is its anisotropic
        # evaluation ``cL·Δs∥ + cT·Δs⊥`` (today Δs∥=d, Δs⊥=0 → cL·d).  The reported
        # %-cap is the longitudinal cL (flat_cap while every rule is isotropic).
        if de > cap.at(d, 0.0) + noise:
            viol.append(((de / d) * 100.0, cap.flat_cap() * 100.0, d, role,
                         is_spine, 0.5 * (xa + xb), 0.5 * (ya + yb)))
    # The taxi spine also ANCHORS into the runway (one side is a runway-surface
    # sample, not a node) — checked separately, flagged is_spine.
    viol.extend(_spine_runway_join_violations(layout, noise))
    viol.sort(reverse=True)
    return viol


def checked_spine_geometry(layout):
    """Return ``(nodes, edges)`` — the coord-space SPINE node set and undirected
    spine edge set the validator checks (``is_spine`` pairs only).  ``nodes`` is
    ``{(round(x,2), round(y,2)), ...}``; ``edges`` is ``{(node_a, node_b)}`` with
    ``node_a <= node_b``.  Used by ``test_solver_and_validator_same_nodes`` to
    assert the SOLVER's unified graph (``grade_graph.build_unified_graph``) and
    the VALIDATOR check the exact same spine — one graph in effect."""
    def _k(x, y):
        return (round(x, 2), round(y, 2))
    nodes = set()
    edges = set()
    for (_role, is_spine, (xa, ya), _za, (xb, yb), _zb, _cap) in \
            _iter_checked_pairs(layout):
        if not is_spine:
            continue
        a, b = _k(xa, ya), _k(xb, yb)
        if a == b:
            continue
        nodes.add(a)
        nodes.add(b)
        edges.add((a, b) if a <= b else (b, a))
    return nodes, edges


def _spine_runway_join_violations(layout, noise):
    """The taxi spine ANCHORS into the runway (user 2026-06-25): where a taxi
    centerline meets a runway, the grade from the runway SURFACE at the contact to
    the nearest emitted taxiway/junction node must be ≤ the centerline's per-letter
    cap.  Catches a spine that drops below the runway at the join (the F/14R
    valley) — invisible to the per-shape graph because the runway is not in it."""
    from shapely.geometry import Point
    from auto_patch.layout import ROLE_RUNWAY, ROLE_RUNWAY_CROSSING
    from auto_patch.pavement.runways import _sample_runway_segment_elev
    from auto_patch.grade_law import RUNWAY_CONTACT_M, RUNWAY_JOIN_NEAR_M
    _CONTACT_M = RUNWAY_CONTACT_M
    _NEAR_M = RUNWAY_JOIN_NEAR_M
    # A runway_crossing is RUNWAY surface (a taxiway crossing ON the runway), not a
    # taxi-spine node — comparing it to a runway's profile is runway-vs-runway (the
    # runway profile's job at an intersection: the crossing sits at a compromise
    # between the two runways), NOT a taxi runway-join.  Exclude it from the
    # taxi-node search so the join is measured to the real spine node (user
    # 2026-06-26: a marginal ~U11/14L-32R join was mis-flagged on the crossing).
    _RUNWAY_SURFACE = (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)

    runways = [s for s in layout.shapes
               if s.role == ROLE_RUNWAY and s.polygon is not None
               and not s.polygon.is_empty]
    if not runways:
        return []
    # emitted taxiway / junction nodes (the spine side of the join).
    nx, ny, ne = [], [], []
    for s in layout.shapes:
        if (s.role in _RUNWAY_SURFACE or s.polygon is None or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        elevs = _shape_elevs(s, len(ring))
        if elevs is None:
            continue
        for (x, y), e in zip(ring, elevs):
            nx.append(x); ny.append(y); ne.append(e)
    if not nx:
        return []

    out = []
    for entry in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = entry.line if hasattr(entry, "line") else (entry[0] if isinstance(entry, (tuple, list)) else entry)
        is_svc = (entry.is_service if hasattr(entry, "is_service")
                  else str((entry[1] if isinstance(entry, (tuple, list))
                            and len(entry) > 1 else "") or "").upper().startswith("SVC"))
        if ln is None or ln.is_empty or is_svc:
            continue
        cs = list(ln.coords)
        # Per-segment cap at the ENDPOINT touching the runway (a route may change
        # width along its length; the join cap is the size at the contact end).
        for (ex, ey), _arc in ((cs[0], 0.0), (cs[-1], ln.length)):
            cap = float(taxi_grade_cap_for_letter(
                entry.size_at_arc(_arc) if hasattr(entry, "size_at_arc")
                else (entry.dominant_size() if hasattr(entry, "dominant_size")
                      else None)))
            P = Point(ex, ey)
            rwy = min(runways, key=lambda r: r.polygon.distance(P))
            if rwy.polygon.distance(P) > _CONTACT_M:
                continue
            re = _sample_runway_segment_elev(rwy, ex, ey)
            if re is None:
                continue
            # nearest emitted taxiway/junction node to the contact
            best_d2, best_e = _NEAR_M * _NEAR_M, None
            for k in range(len(nx)):
                d2 = (nx[k] - ex) ** 2 + (ny[k] - ey) ** 2
                if d2 < best_d2:
                    best_d2, best_e = d2, ne[k]
            if best_e is None:
                continue
            d = math.sqrt(best_d2)
            if d < 1e-6:
                continue
            de = abs(re - best_e)
            if de > cap * d + noise:
                out.append(((de / d) * 100.0, cap * 100.0, d, "runway_join",
                            True, ex, ey))
    return out


def route_reach_violations(layout, noise=ELEV_ROUNDING_NOISE_M):
    """Flag a soft airside shape (apron / junction) whose AIRSIDE-ROUTE CONTACTS
    are at mutually UNREACHABLE elevations.

    User model (2026-06-26): a no-building apron must get a single base elevation
    that is within-cap reachable via ALL the taxiways that feed it.  If two of its
    route contacts ``a, b`` (the taxiway/junction shapes that abut it) sit at
    elevations whose difference exceeds ``cap · dist(a, b)`` — where ``cap`` is the
    shape's own body cap and ``dist`` is the straight-line gap between the contacts
    (a LOWER bound on the in-pavement route distance) — then NO cap-compliant
    surface can connect them through the shape, so the shape is forced to a steep,
    partly-unreachable elevation (CYXY apron #85: TX2 690.2 vs TX3 677.0 = 13.2 m
    over 478 m = 2.76 % ≫ the 1 % apron cap).  The fix is upstream — the taxiways
    must converge toward a shared reachable level — so this is reported as a
    ``route_reach`` violation, not a within-shape one.

    Returns ``[(pct, cap_pct, dist, role, is_spine=True, x, y), ...]`` (worst
    first), matching ``within_violations``' tuple shape so callers can merge."""
    from auto_patch.layout import (ROLE_APRON, ROLE_BUILDING, ROLE_JUNCTION,
                                   ROLE_RUNWAY)
    from auto_patch.config import (APRON_MAX_GRADE, taxi_grade_cap_for_letter)
    from auto_patch.junction_rules import SLOPING_RECT_ROLES

    # the route shapes whose contact elevation feeds an apron/junction: the taxi
    # network (sloping rects + taxi junctions) — NOT service roads (own datum) and
    # NOT runways (handled by the runway-join check; a runway contact is not a
    # flat-apron constraint, it is the hard anchor itself).
    route_roles = set(SLOPING_RECT_ROLES) | {ROLE_JUNCTION}

    routes = []                 # (shape, elevs)
    for s in layout.shapes:
        if (s.role not in route_roles or s.polygon is None or s.polygon.is_empty
                or str(s.ref or "").upper().startswith("SVC")):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        elevs = _shape_elevs(s, len(ring))
        if elevs is None:
            continue
        routes.append((s, ring, elevs))

    buildings = [t.polygon for t in layout.shapes
                 if t.role == ROLE_BUILDING and t.polygon is not None
                 and not t.polygon.is_empty]

    out = []
    for s in layout.shapes:
        if (s.role != ROLE_APRON or s.polygon is None or s.polygon.is_empty):
            continue
        if any(s.polygon.distance(b) < 1.0 for b in buildings):
            continue                          # a building anchors the level
        cap = APRON_MAX_GRADE
        # contacts: the nearest route vertex to this apron, per touching route.
        contacts = []                         # (ref, elev, (x, y))
        for (t, tring, televs) in routes:
            if t is s or s.polygon.distance(t.polygon) > 1.5:
                continue
            best = None
            for (x, y), e in zip(tring, televs):
                d2 = s.polygon.exterior.distance(_pt(x, y))
                if best is None or d2 < best[0]:
                    best = (d2, e, (x, y))
            if best is not None:
                contacts.append((str(t.ref), best[1], best[2]))
        if len(contacts) < 2:
            continue
        worst = None
        for i in range(len(contacts)):
            for j in range(i + 1, len(contacts)):
                (_ra, ea, pa), (_rb, eb, pb) = contacts[i], contacts[j]
                dist = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                if dist < 1e-3:
                    continue
                de = abs(ea - eb)
                if de > cap * dist + noise:
                    g = de / dist
                    if worst is None or g > worst[0]:
                        c = s.polygon.centroid
                        worst = (g, dist, c.x, c.y)
        if worst is not None:
            g, dist, cx, cy = worst
            out.append((g * 100.0, cap * 100.0, dist, "route_reach", True,
                        cx, cy))
    out.sort(reverse=True)
    return out


def _band_roles():
    """Airside roles whose vertices must sit inside the runway-reach ROUTE BAND:
    the taxi network + the apron / junction / building surfaces it grades to.
    Runways are the band ANCHORS (excluded); groundside / service / boundary /
    clearance carry their own datum and are not runway-reach constrained."""
    from auto_patch.layout import (
        ROLE_APRON, ROLE_BUILDING, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
        ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB)
    return frozenset({
        ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
        ROLE_CROSS_CONNECTOR, ROLE_APRON, ROLE_JUNCTION, ROLE_BUILDING})


def route_band_violations(layout, noise=ELEV_ROUNDING_NOISE_M, G=None):
    """Confirm the runway-reach ROUTE BAND on THE unified grade graph ``G``.

    The solver bounds every airside node by the reach band
    (``building_feasibility.reach_band_unified``: a cap-Dijkstra over
    ``G.spine_adj`` from the runway anchors ``G.runway_anchor``); this is the
    AS-BUILT CONFIRMATION of that rule on the SAME graph ``G`` — one graph, no
    separate route-field.  For every airside taxi / apron / junction / building
    vertex, the emitted elevation must lie inside ``band(x, y) = (floor,
    ceiling)``: above the ceiling means the vertex is higher than the steepest
    cap-compliant climb from any runway can reach; below the floor, lower than
    any descent can reach.

    Vertices off the spine network (``band`` returns ``None`` — a coverage hole
    / weak band) are NOT constrained here; their local within-shape law
    (``within_violations``) still applies.  This is the in-memory home of the
    check (the layout carries the global spine ``G`` reach_band needs); rebuilding
    ``G`` from the shipped OSM is a documented follow-up (handover item 1).

    Three failure modes, all REPORTED (no airport is legitimately infeasible —
    every one is a solver bug, a missing rule, or a rule that needs adjusting, so
    none are silently dropped; the class just tells us which FIX it needs):

      * ``"ceil"`` — elev above the band ceiling (higher than any cap-compliant
        climb from a runway can reach).
      * ``"floor"`` — elev below the band floor (lower than any descent reaches).
      * ``"pinned"`` — the band itself is EMPTY (``floor > ceiling``): no single
        elevation is within-cap reachable from every runway at once.  A
        FUNDAMENTAL multi-anchor infeasibility — whatever the solver emits here
        violates the reach law from some anchor.  ``excess_m`` is the band
        deficit ``floor - ceiling`` (how over-constrained the point is); the
        FIX is upstream (a transition/relaxation rule, a yielded anchor, or a
        geometry bug), tracked alongside ``route_reach_violations`` /
        ``grade_feasibility_audit``.

    Vertices off the spine network (``band`` returns ``None`` — a coverage hole
    / weak band) are NOT constrained here; their local within-shape law
    (``within_violations``) still applies.  This is the in-memory home of the
    check (the layout carries the global spine ``G`` reach_band needs); rebuilding
    ``G`` from the shipped OSM is a documented follow-up (handover item 1).

    Returns ``[(excess_m, side, role, x, y, elev, lo, hi), ...]`` worst (largest
    ``excess_m``) first."""
    from .elevation_per_surface.solver_primitives import _build_node_list
    from .elevation_per_surface.building_feasibility import reach_band_unified
    if G is None:
        nodes, b2i = _build_node_list(layout)
        if not nodes:
            return []
        G = GG.build_unified_graph(layout, b2i)
    band = reach_band_unified(layout, G)
    roles = _band_roles()

    # SMALL buildings (grade_law.building_requires_full_frontage == False) are
    # LOCAL reach ANCHORS: ``build_building_seats`` seats such a pad at its
    # central-chord level and the body solve grades the surrounding apron FROM the
    # pad at the apron cap, so those points are reachable from the PAD, not the
    # runway route.  Recognise that here (the SAME reach the solver enforced, one
    # rule via ``building_requires_full_frontage``) so the looser small-building
    # frontage — its non-central pad and the apron stepping up to it — is not
    # falsely flagged.  A LARGE building is NOT an anchor: its whole frontage must
    # be route-reachable, so its pads stay checked per-vertex.
    from auto_patch.grade_law import (
        building_requires_full_frontage, BUILDING_REACH_CORRIDOR_M)
    from auto_patch.layout import ROLE_BUILDING
    from auto_patch.config import APRON_MAX_GRADE, VISIBLE_CHORD_CONNECT
    from .elevation_per_surface.building_feasibility import (
        _pavement_visibility, _VIS_ON_PAV_FRAC)
    small_pads = []                          # (polygon, seat)
    for s in layout.shapes:
        if (s.role == ROLE_BUILDING and s.polygon is not None
                and not s.polygon.is_empty
                and not building_requires_full_frontage(s.polygon.area)):
            ring = _open_ring(list(s.polygon.exterior.coords))
            el = _shape_elevs(s, len(ring))
            if el:
                small_pads.append((s.polygon, sum(el) / len(el)))
    _vis = (_pavement_visibility(layout)
            if (small_pads and VISIBLE_CHORD_CONNECT) else None)

    def _reached_from_small_pad(x, y, e):
        """True when ``(x, y, e)`` sits within the apron cap of a SMALL building's
        seat over an ON-PAVEMENT chord — the surface grades to it from the local
        pad (the looser small-building rule), not the runway route."""
        if not small_pads:
            return False
        from shapely.geometry import Point as _P, LineString as _LS
        from shapely.ops import nearest_points as _np
        p = _P(x, y)
        for (poly, seat) in small_pads:
            d = poly.distance(p)
            if d > BUILDING_REACH_CORRIDOR_M:
                continue
            if abs(e - seat) > APRON_MAX_GRADE * d + noise:
                continue
            if _vis is None:
                return True
            near = _np(poly, p)[0]
            chord = _LS([(x, y), (near.x, near.y)])
            if chord.length < 1e-6 or _vis.contains(chord):
                return True
            try:
                if (chord.intersection(_vis.context).length / chord.length
                        >= _VIS_ON_PAV_FRAC):
                    return True
            except Exception:                                  # pragma: no cover
                pass
        return False

    out = []
    seen = set()
    for s in layout.shapes:
        if (s.role not in roles or s.polygon is None or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        elevs = _shape_elevs(s, len(ring))
        if elevs is None:
            continue
        for (x, y), e in zip(ring, elevs):
            # dedupe by shared canonical node — the band is positional, so a
            # welded corner shared by N shapes is ONE band check, not N.
            key = (round(x, 2), round(y, 2))
            if key in seen:
                continue
            seen.add(key)
            b = band(x, y)
            if b is None:
                continue
            lo, hi = b
            # within a feasible runway-reach band → fine.
            if lo <= hi + noise and (lo - noise) <= e <= (hi + noise):
                continue
            # else reachable from a local SMALL-building pad → fine (the apron
            # grades from the pad at the apron cap; the small-building rule).
            if _reached_from_small_pad(x, y, e):
                continue
            if lo > hi + noise:
                # EMPTY band — no compliant elevation exists at this vertex
                # (mutually-unreachable runway anchors).  Reported, never
                # dropped: it is a fundamental infeasibility to root-cause.
                out.append((lo - hi, "pinned", s.role, x, y, e, lo, hi))
            elif e > hi + noise:
                out.append((e - hi, "ceil", s.role, x, y, e, lo, hi))
            else:  # e < lo - noise
                out.append((lo - e, "floor", s.role, x, y, e, lo, hi))
    out.sort(reverse=True, key=lambda t: t[0])
    return out


def _pt(x, y):
    from shapely.geometry import Point
    return Point(x, y)
