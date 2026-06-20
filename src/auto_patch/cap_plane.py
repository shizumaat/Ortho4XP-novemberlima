"""De-bulge the centre node on a rect end-cap's outer edge (user 2026-06-19).

A sloping taxi rect is flat PERPENDICULAR to its centerline; its end-cap
(``is_rect_cap``, role junction) should join the junction smoothly.  The cap's
outer edge is ``corner - M - corner`` where M is the centerline node the spine
welds in.  The per-surface solver grades the cap as a free junction, so M
drifts 0.1-0.2 m OFF the straight line between its two outer corners → a small
step where the junction meets the cap centre.

This LATE pass sets each such middle node M to the LINEAR INTERPOLATION of its
two outer corners (so the outer edge ``corner-M-corner`` is straight — no
bulge) and propagates the new elevation to every shape sharing M (the junction
on the other side), so the join is one welded level.  It moves ONLY the
interior centerline nodes — the outer corners keep their solved network level,
so junction grades are unchanged (stamping the cap to the rect PLANE instead
pulled the corners off the network and manufactured grade violations).

Public API:
    debulge_cap_centre_nodes(layout) -> int   # nodes moved
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

from shapely.errors import GEOSException, TopologicalError

import O4_UI_Utils as UI

from .layout import (
    ROLE_CROSS_CONNECTOR, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB)

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["debulge_cap_centre_nodes"]

_RECT_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR, "service_road"})
_KEY_Q = 1e-2


def _key(x: float, y: float) -> Tuple[int, int]:
    return (int(round(x / _KEY_Q)), int(round(y / _KEY_Q)))


def _open(poly):
    cs = list(poly.exterior.coords)
    if len(cs) > 1 and cs[0] == cs[-1]:
        cs = cs[:-1]
    return cs


def debulge_cap_centre_nodes(layout) -> int:
    # All sloping-rect corner positions — a cap node here is an INNER (rect-
    # side) node; the rest of the cap's nodes are its OUTER edge.
    rect_keys = set()
    for s in layout.shapes:
        if (s.role in _RECT_ROLES and s.polygon is not None
                and not s.polygon.is_empty
                and s.polygon.geom_type == "Polygon"):
            for (x, y) in _open(s.polygon):
                rect_keys.add(_key(x, y))
    if not rect_keys:
        return 0

    caps = [s for s in layout.shapes
            if getattr(s, "is_rect_cap", False) and s.node_altitudes
            and s.polygon is not None and not s.polygon.is_empty
            and s.polygon.geom_type == "Polygon"]
    if not caps:
        return 0

    new_elev: Dict[Tuple[int, int], float] = {}
    n_nodes = 0
    for c in caps:
        ring = _open(c.polygon)
        na = c.node_altitudes
        if len(na) != len(ring) + 1:
            continue
        n = len(ring)
        inner = [i for i in range(n) if _key(*ring[i]) in rect_keys]
        if len(inner) < 2:
            continue
        # OUTER nodes = the run NOT shared with the rect.  An interior outer
        # node (both ring-neighbours are also outer) is a centerline node M
        # the spine welded in; the outer CORNERS each have an inner neighbour.
        innerset = set(inner)
        for i in range(n):
            if i in innerset:
                continue
            prev_in = (i - 1) % n in innerset
            next_in = (i + 1) % n in innerset
            if prev_in or next_in:
                continue   # outer CORNER (adjacent to an inner node) — keep
            # Interior outer node M: straddle to the two outer corners along
            # the ring (skip past any other interior outer nodes) and lerp.
            a = (i - 1) % n
            while a not in innerset and (a - 1) % n not in innerset and a != i:
                a = (a - 1) % n
            b = (i + 1) % n
            while b not in innerset and (b + 1) % n not in innerset and b != i:
                b = (b + 1) % n
            ax, ay = ring[a]
            bx, by = ring[b]
            mx, my = ring[i]
            L2 = (bx - ax) ** 2 + (by - ay) ** 2
            t = (((mx - ax) * (bx - ax) + (my - ay) * (by - ay)) / L2
                 if L2 > 1e-9 else 0.5)
            t = max(0.0, min(1.0, t))
            lerp = na[a] + t * (na[b] - na[a])
            if abs(lerp - na[i]) > 1e-6:
                new_elev[_key(mx, my)] = round(lerp, 2)
                n_nodes += 1
    if not new_elev:
        return 0

    # Propagate to every node_altitudes shape sharing a moved centre node.
    for s in layout.shapes:
        na = s.node_altitudes
        if (na is None or s.polygon is None or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"):
            continue
        ring = _open(s.polygon)
        if len(na) != len(ring) + 1:
            continue
        changed = False
        new_na = list(na[:-1])
        for i, (x, y) in enumerate(ring):
            k = _key(x, y)
            if k in new_elev and abs(new_na[i] - new_elev[k]) > 1e-6:
                new_na[i] = new_elev[k]
                changed = True
        if changed:
            s.node_altitudes = new_na + [new_na[0]]

    if n_nodes:
        UI.vprint(1,
            f"  [pav-builder] {getattr(layout, 'icao', '')}: de-bulged "
            f"{n_nodes} cap centre node(s) to the outer-edge line "
            f"(smooth junction join).")
    return n_nodes
