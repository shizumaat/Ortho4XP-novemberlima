"""Rect end-cap co-planarity for the one-profile solve (user 2026-06-25).

A rect end-cap (``is_rect_cap``, role junction) is the strip the rect VACATED at
its junction-facing end — geometrically it is the rect's own last ``depth`` m.  So
its surface must lie on the SAME plane as the parent rect: co-planar, the same
slope, grade-INVISIBLE.  The legacy ``O4_CAP_PLANAR`` constraint only bounds the
cap to *a* plane ≤ the taxi cap and lets the adjacent junction tug the cap's outer
edge off the rect plane — a 12 m tug-of-war the cap loses as a steep step (CYXY:
a 7.3 % rect with its cap at 15 %, the junction 0.9 m below the rect plane).

Fix (solve-time, no post-pass): the cap's OUTER nodes are DERIVED — each solve
sweep they are set to the parent rect's plane evaluated at their (x, y), and they
are excluded from the free solve.  The cap can no longer be squeezed (it is the
rect plane by construction, hence ≤cap), and because the junction SHARES those
outer nodes the junction now conforms to the cap edge — its remaining free nodes
grade away from it over the junction's whole extent (the transition lands in the
junction, not on the 12 m cap).
"""
from __future__ import annotations


def cap_plane_derivations(layout, nodes, bucket_to_idx):
    """Return ``(derive, derived_set)`` for the cap-coplanar solve rule.

    ``derive`` — ``[(i, (x, y), r0, r1, r2), ...]`` one entry per cap OUTER node:
    ``i`` is the solver node index, ``(x, y)`` its position, and ``r0/r1/r2`` are
    ``(idx, x, y)`` for three corners of the parent sloping rect (the plane).
    ``derived_set`` — the set of ``i`` (excluded from the free solve).
    """
    import math
    from auto_patch.junction_rules import SLOPING_RECT_ROLES

    cps = layout.canonical_points

    def kidx(x, y):
        return bucket_to_idx.get(cps.get_or_add(float(x), float(y)))

    def opn(p):
        cs = list(p.exterior.coords)
        return cs[:-1] if len(cs) > 1 and cs[0] == cs[-1] else cs

    # Sloping rects that emit as a PLANE (4 corners, altitude_high/low at
    # writeback → ``node_altitudes is None`` pre-solve, same test as
    # ``_build_shape_constraints``).  Record their corner solver-node indices
    # (+ xy) — the plane reference points.  NOTE: altitude_high/low are NOT set
    # yet at solve time, so DON'T test them here.
    rects = []                                  # (corner_idx_set, [(idx, x, y)])
    for s in layout.shapes:
        if (s.role in SLOPING_RECT_ROLES and s.polygon is not None
                and not s.polygon.is_empty and s.node_altitudes is None):
            r = opn(s.polygon)
            if len(r) != 4:
                continue
            ids = [kidx(x, y) for (x, y) in r]
            if any(i is None for i in ids):
                continue
            rects.append((set(ids), [(ids[k], r[k][0], r[k][1]) for k in range(4)]))
    if not rects:
        return [], set()

    derive = []
    derived_set: set = set()
    for c in layout.shapes:
        if (not getattr(c, "is_rect_cap", False) or c.polygon is None
                or c.polygon.is_empty):
            continue
        r = opn(c.polygon)
        ids = [kidx(x, y) for (x, y) in r]
        # parent rect = the one sharing ≥2 of this cap's nodes (its inner edge).
        parent = None
        for (rset, rcorners) in rects:
            if sum(1 for i in ids if i in rset) >= 2:
                parent = rcorners
                break
        if parent is None:
            continue
        pset = {ix for (ix, _x, _y) in parent}
        # 3 non-collinear plane references (ring order 0,1,2 = two adjacent edges).
        r0, r1, r2 = parent[0], parent[1], parent[2]
        # OUTER cap nodes = those NOT a corner of the parent rect (the inner edge
        # IS the rect end, already on the plane — leave it to the rect).
        for k, i in enumerate(ids):
            if i is None or i in pset or i in derived_set:
                continue
            derive.append((i, (r[k][0], r[k][1]), r0, r1, r2))
            derived_set.add(i)
    return derive, derived_set


def apply_cap_planes(elev, derive):
    """Set each derived cap-outer node to its parent rect's plane at (x, y),
    from the rect corners' CURRENT elevations.  Called each solve sweep."""
    for (i, (x, y), r0, r1, r2) in derive:
        z = _plane_z(elev, r0, r1, r2, x, y)
        if z is not None:
            elev[i] = z


def _plane_z(elev, r0, r1, r2, x, y):
    """z at (x, y) on the plane through the three rect corners (idx, x, y) at
    their current elevations.  ``None`` if the corners are (near-)collinear."""
    (i0, x0, y0), (i1, x1, y1), (i2, x2, y2) = r0, r1, r2
    z0, z1, z2 = elev[i0], elev[i1], elev[i2]
    ux, uy, uz = x1 - x0, y1 - y0, z1 - z0
    vx, vy, vz = x2 - x0, y2 - y0, z2 - z0
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    if abs(nz) < 1e-9:                          # degenerate footprint
        return None
    return z0 - (nx * (x - x0) + ny * (y - y0)) / nz
