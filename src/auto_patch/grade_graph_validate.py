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

import math

from . import grade_graph as GG
from .config import ELEV_ROUNDING_NOISE_M, taxi_grade_cap_for_letter


def _open_ring(coords):
    c = list(coords)
    return c[:-1] if c and c[0] == c[-1] else c


def _context(layout):
    """Build the shared grade-graph context from a layout (mirrors the solver's
    ``_grade_graph_context`` but keyed by rounded coordinate so it works on the
    emitted geometry)."""
    from .elevation_per_surface.unified_jacobi import (
        SLOPING_RECT_ROLES, _shape_grade)
    from .layout import ROLE_BUILDING
    letters = getattr(layout, "apt_taxi_letters", {}) or {}
    cls = []
    for ln, name in (getattr(layout, "apt_taxi_centerlines", []) or []):
        if ln is None or getattr(ln, "is_empty", True):
            continue
        if name and str(name).upper().startswith("SVC"):
            continue            # service roads are NOT taxi spines (own role)
        try:
            pts = list(ln.coords)
        except Exception:
            continue
        if len(pts) >= 2:
            cls.append(GG.Centerline(
                pts=pts, cap=taxi_grade_cap_for_letter(letters.get(name))))
    rect_cap_at: dict = {}
    for s in layout.shapes:
        if (s.role not in SLOPING_RECT_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        cap = _shape_grade(layout, s)
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            k = (round(x, 3), round(y, 3))
            if rect_cap_at.get(k, -1.0) < cap:
                rect_cap_at[k] = cap

    def _inherited(shape):
        best = None
        for (x, y) in shape.ring:
            c = rect_cap_at.get((round(x, 3), round(y, 3)))
            if c is not None and (best is None or c > best):
                best = c
        return best if best is not None else GG.TAXI_MAX_GRADE

    bld_keys = set()
    for s in layout.shapes:
        if (s.role == ROLE_BUILDING and s.polygon is not None
                and not s.polygon.is_empty):
            for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
                bld_keys.add((round(x, 3), round(y, 3)))
    return GG.GradeContext(centerlines=cls, inherited_junction_cap=_inherited,
                           building_keys=frozenset(bld_keys))


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


def within_violations(layout, noise=ELEV_ROUNDING_NOISE_M):
    """Return the apron/junction within-shape grade violations of the emitted
    ``layout``, as ``[(pct, cap, dist, role, is_spine, x, y), ...]`` (worst
    first).  Uses the unified grade graph — identical constraints to the solver.
    """
    ctx = _context(layout)
    viol = []
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
        keys = list(range(nlen))
        gs = GG.GradeShape(role=s.role, ring=[(x, y) for (x, y) in ring],
                           keys=keys)
        sc = GG.shape_constraints(gs, ctx)
        # spine edges = pairs whose cap is the (steeper) spine cap, i.e. both on
        # a common centerline; recover via the spine chains for the split.
        spine_pairs = set()
        for chain in sc.spine_chains:
            for a, b in zip(chain, chain[1:]):
                spine_pairs.add((min(a, b), max(a, b)))
        pos = {i: ring[i] for i in range(nlen)}
        for (a, b, cap) in sc.edges:
            xa, ya = pos[a]
            xb, yb = pos[b]
            d = math.hypot(xa - xb, ya - yb)
            if d < 1e-6:
                continue
            de = abs(elevs[a] - elevs[b])
            if de > cap * d + noise:
                is_spine = (min(a, b), max(a, b)) in spine_pairs
                viol.append(((de / d) * 100.0, cap * 100.0, d, s.role,
                             is_spine, 0.5 * (xa + xb), 0.5 * (ya + yb)))

    # The route-graph solver also emits the SLOPING TAXI RECTS and their END-CAPS
    # from the spine profile, and anchors the spine INTO the runway — so the spine
    # validator must check those too (user 2026-06-26: test the same thing we
    # build).  Rects/runway are not ``SOFT_VISIBILITY_ROLES`` so the loop above
    # skips them; add them here at the SAME per-letter (width-based) cap the solver
    # used.  All are flagged ``is_spine`` (they ARE the taxi spine) so the spine
    # gate covers them end to end.
    viol.extend(_rect_grade_violations(layout, noise))
    viol.extend(_spine_runway_join_violations(layout, noise))

    viol.sort(reverse=True)
    return viol


def _rect_grade_violations(layout, noise):
    """Within-shape grade of every sloping taxi RECT (and its end-cap), at the
    width-based per-letter cap.  A clean rect is a tilted plane (max grade =
    axial); checking all corner pairs also catches a warped/non-coplanar rect or
    a cap that does not continue its parent's plane."""
    from auto_patch.junction_rules import SLOPING_RECT_ROLES
    from auto_patch.layout import taxi_shape_code_letter
    out = []
    # parent-rect cap for each cap, by shared corners (a cap is role junction with
    # no letter of its own — it must validate at the rect's cap it continues).
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
        _append_allpair(out, ring, _shape_elevs(s, len(ring)), cap, s.role, noise)

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
        _append_allpair(out, ring, _shape_elevs(s, len(ring)), cap, "rect_cap",
                        noise)
    return out


def _append_allpair(out, ring, elevs, cap, role, noise):
    if elevs is None:
        return
    n = len(ring)
    for i in range(n):
        xa, ya = ring[i]
        for j in range(i + 1, n):
            xb, yb = ring[j]
            d = math.hypot(xa - xb, ya - yb)
            if d < 1e-6:
                continue
            de = abs(elevs[i] - elevs[j])
            if de > cap * d + noise:
                out.append(((de / d) * 100.0, cap * 100.0, d, role, True,
                            0.5 * (xa + xb), 0.5 * (ya + yb)))


def _spine_runway_join_violations(layout, noise):
    """The taxi spine ANCHORS into the runway (user 2026-06-25): where a taxi
    centerline meets a runway, the grade from the runway SURFACE at the contact to
    the nearest emitted taxiway/junction node must be ≤ the centerline's per-letter
    cap.  Catches a spine that drops below the runway at the join (the F/14R
    valley) — invisible to the per-shape graph because the runway is not in it."""
    from shapely.geometry import Point
    from auto_patch.layout import ROLE_RUNWAY
    from auto_patch.pavement.runways import _sample_runway_segment_elev
    _CONTACT_M = 12.0
    _NEAR_M = 18.0

    runways = [s for s in layout.shapes
               if s.role == ROLE_RUNWAY and s.polygon is not None
               and not s.polygon.is_empty]
    if not runways:
        return []
    # emitted taxiway / junction nodes (the spine side of the join).
    nx, ny, ne = [], [], []
    for s in layout.shapes:
        if (s.role in (ROLE_RUNWAY,) or s.polygon is None or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        elevs = _shape_elevs(s, len(ring))
        if elevs is None:
            continue
        for (x, y), e in zip(ring, elevs):
            nx.append(x); ny.append(y); ne.append(e)
    if not nx:
        return []

    letters = getattr(layout, "apt_taxi_letters", {}) or {}
    out = []
    for entry in (getattr(layout, "apt_taxi_centerlines", []) or []):
        ln = entry[0] if isinstance(entry, (tuple, list)) else entry
        ref = entry[1] if (isinstance(entry, (tuple, list))
                           and len(entry) > 1) else None
        if ln is None or ln.is_empty or str(ref or "").upper().startswith("SVC"):
            continue
        cap = float(taxi_grade_cap_for_letter(letters.get(ref)))
        cs = list(ln.coords)
        for (ex, ey) in (cs[0], cs[-1]):
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
