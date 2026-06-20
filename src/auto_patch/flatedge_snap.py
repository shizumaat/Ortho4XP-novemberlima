"""Drop non-corner nodes off sloping-rect flat edges (user 2026-06-19).

Only the 2 CORNERS of a sloping rect's flat (cross) end are legal shared
vertices — a junction/apron vertex landing mid-flat-edge is an illegal shared
vertex (``verification.check_vertex_on_flat_edge``) that triangulates to a
Triangle4XP T-junction tear (SPJC SVC13: an apron ran corner→MID→corner along
service-road SVC13's flat end, the MID node teeing into SVC13's edge).

The node is introduced AFTER the solve (the spine slice + the weld /
conformance chain), so this runs as a LATE pass on the final geometry: it
DROPS every non-rect vertex that lies on a sloping-rect flat-edge interior,
joining its neighbours (which sit at / beyond the rect corners, so the edge
straightens corner-to-corner) and dropping the matching ``node_altitudes``
entry so the per-vertex list stays index-aligned.  Done globally (every shape
sharing the vertex drops it) so the patch stays a conforming partition.

Public API:
    drop_flatedge_nodes(layout) -> int   # number of vertices dropped
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.strtree import STRtree

import O4_UI_Utils as UI

from .layout import (
    ROLE_CROSS_CONNECTOR, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB)

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["drop_flatedge_nodes"]

# Sloping 4-corner rects whose flat ends admit only corner shared vertices.
_RECT_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR, "service_road"})
_ON_EDGE_TOL_M = 0.25     # perpendicular distance to count as "on" the edge
_CORNER_TOL_M = 0.40      # within this of a corner = already legal
_MIN_RING_VERTS = 4
_KEY_Q = 1e-3


def _key(x: float, y: float) -> Tuple[int, int]:
    return (int(round(x / _KEY_Q)), int(round(y / _KEY_Q)))


def _axis_unit(axis):
    if axis is None or axis.is_empty:
        return None
    pts = list(axis.coords)
    if len(pts) < 2:
        return None
    dx, dy = pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]
    L = math.hypot(dx, dy)
    return (dx / L, dy / L) if L > 1e-6 else None


def _flat_edges(poly: Polygon, axis):
    ring = list(poly.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) != 4:
        return []
    au = _axis_unit(axis)
    edges = []
    for i in range(4):
        a, b = ring[i], ring[(i + 1) % 4]
        el = math.hypot(b[0] - a[0], b[1] - a[1])
        if el < 1e-6:
            continue
        dot = (abs((b[0] - a[0]) * au[0] + (b[1] - a[1]) * au[1]) / el
               if au is not None else None)
        edges.append((a, b, dot, el))
    if len(edges) < 2:
        return []
    if all(e[2] is not None for e in edges):
        edges.sort(key=lambda e: e[2])
    else:
        edges.sort(key=lambda e: e[3])
    return [(e[0], e[1]) for e in edges[:2]]


def _open(poly: Polygon):
    cs = list(poly.exterior.coords)
    if len(cs) > 1 and cs[0] == cs[-1]:
        cs = cs[:-1]
    return cs


def drop_flatedge_nodes(layout) -> int:
    rect_shapes = [s for s in layout.shapes
                   if s.role in _RECT_ROLES and s.polygon is not None
                   and not s.polygon.is_empty
                   and s.polygon.geom_type == "Polygon"]
    if not rect_shapes:
        return 0

    segs: List[LineString] = []
    rect_corner_keys = set()
    for s in rect_shapes:
        for (x, y) in _open(s.polygon):
            rect_corner_keys.add(_key(x, y))
        for (a, b) in _flat_edges(s.polygon, s.source_axis):
            try:
                segs.append(LineString([a, b]))
            except _GEOM_EXC:
                continue
    if not segs:
        return 0
    tree = STRtree(segs)

    def _on_flatedge_interior(x, y) -> bool:
        if _key(x, y) in rect_corner_keys:
            return False
        p = Point(x, y)
        for idx in tree.query(p):
            seg = segs[idx]
            if seg.distance(p) > _ON_EDGE_TOL_M:
                continue
            a, b = seg.coords[0], seg.coords[1]
            if (math.hypot(x - a[0], y - a[1]) <= _CORNER_TOL_M
                    or math.hypot(x - b[0], y - b[1]) <= _CORNER_TOL_M):
                continue                          # at a corner → legal
            return True
        return False

    # Working open rings + aligned open altitudes for the droppable shapes.
    dec = []  # (shape, ring, alts_or_None)
    for s in layout.shapes:
        if (s.role in _RECT_ROLES or s.polygon is None or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"):
            continue
        ring = _open(s.polygon)
        na = s.node_altitudes
        a_open = (list(na[:-1]) if (na and len(na) == len(ring) + 1)
                  else None)
        if a_open is not None and len(a_open) != len(ring):
            a_open = None
        dec.append((s, ring, a_open))
    if not dec:
        return 0

    total = 0
    for _round in range(50):
        # Keys to drop = every droppable-shape vertex on a flat-edge interior.
        drop_keys = set()
        for (_s, ring, _a) in dec:
            if len(ring) <= _MIN_RING_VERTS:
                continue
            for (x, y) in ring:
                if _on_flatedge_interior(x, y):
                    drop_keys.add(_key(x, y))
        if not drop_keys:
            break
        changed = False
        for di in range(len(dec)):
            s, ring, a = dec[di]
            if len(ring) <= _MIN_RING_VERTS:
                continue
            new_ring, new_a = [], ([] if a is not None else None)
            dropped_here = False
            for pos, (x, y) in enumerate(ring):
                if _key(x, y) in drop_keys and len(ring) - (
                        sum(1 for q in ring
                            if _key(*q) in drop_keys)) >= 3:
                    dropped_here = True
                    continue
                new_ring.append((x, y))
                if new_a is not None:
                    new_a.append(a[pos])
            if not dropped_here or len(new_ring) < 3:
                continue
            try:
                np_poly = Polygon(new_ring + [new_ring[0]])
            except _GEOM_EXC:
                continue
            if (not np_poly.is_valid or np_poly.is_empty
                    or np_poly.geom_type != "Polygon"
                    or len(_open(np_poly)) != len(new_ring)):
                continue
            total += len(ring) - len(new_ring)
            dec[di] = (s, new_ring, new_a)
            changed = True
        if not changed:
            break

    if total == 0:
        return 0
    n_shapes = 0
    for (s, ring, a) in dec:
        if len(ring) < 3:
            continue
        try:
            np_poly = Polygon(ring + [ring[0]])
        except _GEOM_EXC:
            continue
        if (not np_poly.is_valid or np_poly.is_empty
                or np_poly.geom_type != "Polygon"):
            continue
        if np_poly.equals(s.polygon):
            continue
        s.polygon = np_poly
        if a is not None:
            s.node_altitudes = a + [a[0]]
        n_shapes += 1

    if total:
        UI.vprint(1,
            f"  [pav-builder] {getattr(layout, 'icao', '')}: dropped {total} "
            f"non-corner node(s) off sloping-rect flat edges "
            f"({n_shapes} shape(s)).")
    return total
