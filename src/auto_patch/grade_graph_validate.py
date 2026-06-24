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
    viol.sort(reverse=True)
    return viol
