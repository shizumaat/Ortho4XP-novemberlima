"""Rect end-caps (gate ``RECT_END_CAPS``) — carved just BEFORE the spine.

Problem (STATUS.md 20260618-01): the centerline-spine
(`apply_junction_centerline_spine`) slices a junction along a crossing
centerline and, where that slice meets a SLOPING taxi rect, welds a
mid-edge node onto the rect's long (sloping) edge.  `enforce_conformance`
then flips the clean 4-corner sloping plane to ``node_altitudes``, so the
rect no longer grades as ONE plane and descends only ~half its length
(SPJC taxiway L: 3.8 m of a 7.5 m drop), starving the apron of slack.

Fix: carve a small flat cap off each junction-facing flat end of every
sloping rect.  The carved strip becomes a ``ROLE_JUNCTION`` shape that sits
in the rect's vacated 2 m; the rect body shrinks to a full-length 4-corner
plane.  The spine's HARD-end test now reads the junction entry as SOFT
(it is ``_CAP_DEPTH_M`` clear of the shrunk rect), so the spine welds its
node onto the flat cap, the rect's sloping edges stay node-free, and the
solver grades the rect across its whole length to the junction level.

WHY just before the spine (not at rect-build time): the pre-solve repair
chain dissolves small junctions long before the spine runs
(`_merge_sliver_junctions_into_neighbours`, the orphan/off-source drops,
overlap-clip ×3) and rebuilds junction ``BuiltShape``s without preserving
markers.  A cap created at rect-build time is eaten by step ~9 of ~30.
Carving immediately before the spine (after every dissolve pass, and after
the junctions already exist) means the cap only faces the spine + the
unify/conformance pass — see docs/pipeline_geometry_audit.md.

At this point the junctions already exist (residue was emitted earlier), so
the cap simply takes over the rect's last 2 m: its OUTER edge coincides
with the existing junction's boundary (the rect's original flat end) and
its INNER edge with the shrunk rect's new flat end.  No residue maths.

Public API:
    carve_rect_end_caps_before_spine(layout, *, depth_m) -> int
        Shrinks sloping-rect polygons in place and appends cap shapes.
        Returns the number of caps carved.  No-op when no junctions/aprons.
"""
from __future__ import annotations

import math
from typing import List, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

import O4_UI_Utils as UI

from .layout import (
    BuiltShape, ROLE_APRON, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB)

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["carve_rect_end_caps_before_spine", "rect_axis_length"]

# Sloping 4-corner taxi rects (the shapes whose plane the spine can break).
_CAP_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR})
# Junction-like surfaces the spine slices (a rect end facing one is capped).
_FACING_ROLES = frozenset({ROLE_JUNCTION, ROLE_APRON})
# How far beyond a flat end we probe to decide it faces a junction/apron.
_PROBE_M = 1.0
# Reject a cap below this area (degenerate sliver, m²).
_MIN_CAP_AREA_M2 = 0.5


def _axis_unit(axis):
    if axis is None or axis.is_empty:
        return None
    pts = list(axis.coords)
    if len(pts) < 2:
        return None
    dx, dy = pts[-1][0] - pts[0][0], pts[-1][1] - pts[0][1]
    L = math.hypot(dx, dy)
    if L < 1e-6:
        return None
    return dx / L, dy / L


def rect_axis_length(polygon, axis):
    """Mean length of a 4-corner rect's two edges most parallel to ``axis``
    (its travel-direction extent), or ``None`` when ``polygon`` is not a clean
    4-corner ring.  This is the SINGLE definition of a sloping rect's "length"
    — both the cap-carve gate here and the spine's HARD-end gate
    (``junction_spine``) call it, so the two passes agree on what "short" means.
    Without an axis the flat ends fall back to the 2 SHORTEST edges (a sloping
    rect's flat ends are its short sides)."""
    if (polygon is None or polygon.is_empty
            or polygon.geom_type != "Polygon"):
        return None
    ring = list(polygon.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) != 4:
        return None
    au = _axis_unit(axis)
    edges = []  # (i, dot_to_axis_or_None, length)
    for i in range(4):
        a, b = ring[i], ring[(i + 1) % 4]
        ex, ey = b[0] - a[0], b[1] - a[1]
        el = math.hypot(ex, ey)
        dot = None
        if au is not None and el >= 1e-6:
            dot = abs(ex * au[0] + ey * au[1]) / el
        edges.append((i, dot, el))
    if au is not None:
        flat = sorted(edges, key=lambda e: (e[1] if e[1] is not None else 1.0))[:2]
    else:
        flat = sorted(edges, key=lambda e: e[2])[:2]
    sloping = [e for e in edges if e not in flat]
    return (sum(e[2] for e in sloping) / len(sloping)) if sloping else 0.0


def _carve_one(rect, axis, facing_geom, depth_m):
    """Carve caps off the junction-facing flat ends of one rect.  Returns
    ``(new_rect_poly, [cap_poly, ...])`` or ``None`` to leave it unchanged.
    ``facing_geom`` is a prepared-ish union/STRtree-backed predicate object
    answering ``contains(Point)`` for junction/apron coverage."""
    if rect is None or rect.is_empty or rect.geom_type != "Polygon":
        return None
    ring = list(rect.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) != 4:
        return None

    au = _axis_unit(axis)
    edges = []  # (i, a, b, dot_to_axis_or_None, length)
    for i in range(4):
        a, b = ring[i], ring[(i + 1) % 4]
        ex, ey = b[0] - a[0], b[1] - a[1]
        el = math.hypot(ex, ey)
        dot = None
        if au is not None and el >= 1e-6:
            dot = abs(ex * au[0] + ey * au[1]) / el
        edges.append((i, a, b, dot, el))

    # Flat (end) edges = the 2 LEAST parallel to the axis; without an axis
    # fall back to the 2 SHORTEST edges (a sloping rect's flat ends are its
    # short sides).
    if au is not None:
        flat = sorted(edges, key=lambda e: (e[3] if e[3] is not None else 1.0))[:2]
    else:
        flat = sorted(edges, key=lambda e: e[4])[:2]

    # Length along the slope; refuse to cap a rect shorter than the configured
    # minimum (user 2026-06-19: a rect < RECT_END_CAP_MIN_RECT_LEN_M gets NO
    # cap and just converts to node_altitudes where the spine crosses it) — and
    # never shorter than two depth bites plus a 2 m middle.
    from .config import RECT_END_CAP_MIN_RECT_LEN_M
    rect_len = rect_axis_length(rect, axis)
    if rect_len is None:
        return None
    if rect_len < max(RECT_END_CAP_MIN_RECT_LEN_M, 2.0 * depth_m + 2.0):
        return None

    cx, cy = rect.centroid.x, rect.centroid.y
    new_ring = list(ring)
    caps: List[Polygon] = []
    for i, a, b, _dot, el in flat:
        if el < 1e-6:
            continue
        ex, ey = b[0] - a[0], b[1] - a[1]
        nx, ny = -ey / el, ex / el          # outward normal of the flat edge
        mx, my = 0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1])
        if (mx - cx) * nx + (my - cy) * ny < 0.0:
            nx, ny = -nx, -ny
        # Cap only an end that faces a junction/apron (the spine slices
        # those).  Ends abutting another rect, a runway, or open boundary
        # are left alone.
        if not facing_geom(mx + nx * _PROBE_M, my + ny * _PROBE_M):
            continue
        ia = (a[0] - nx * depth_m, a[1] - ny * depth_m)
        ib = (b[0] - nx * depth_m, b[1] - ny * depth_m)
        try:
            cap = Polygon([ia, a, b, ib])      # flat 4-corner strip
            if not cap.is_valid:
                cap = cap.buffer(0)
            if (cap.geom_type == "Polygon" and not cap.is_empty
                    and cap.area >= _MIN_CAP_AREA_M2):
                caps.append(cap)
            else:
                continue
        except _GEOM_EXC:
            continue
        new_ring[i] = ia
        new_ring[(i + 1) % 4] = ib

    if not caps:
        return None
    try:
        new_rect = Polygon(new_ring)
        if not new_rect.is_valid:
            new_rect = new_rect.buffer(0)
    except _GEOM_EXC:
        return None
    if (new_rect.geom_type != "Polygon" or new_rect.is_empty
            or new_rect.area < 0.3 * rect.area):
        return None
    return new_rect, caps


def carve_rect_end_caps_before_spine(layout, *, depth_m) -> int:
    """Carve flat end-caps off junction-facing rect ends, in place.

    Shrinks each sloping rect's polygon and appends one ``ROLE_JUNCTION``
    cap shape per carved end (``is_rect_cap=True``).  Returns the cap count.
    Run immediately before ``apply_junction_centerline_spine``.
    """
    facing_polys = [s.polygon for s in layout.shapes
                    if s.role in _FACING_ROLES and s.polygon is not None
                    and not s.polygon.is_empty
                    and s.polygon.geom_type == "Polygon"]
    if not facing_polys:
        return 0
    tree = STRtree(facing_polys)

    def _faces(px, py):
        p = Point(px, py)
        for idx in tree.query(p):
            try:
                if facing_polys[idx].contains(p):
                    return True
            except _GEOM_EXC:
                continue
        return False

    new_caps: List[BuiltShape] = []
    n_caps = 0
    for s in layout.shapes:
        if s.role not in _CAP_ROLES or s.polygon is None or s.polygon.is_empty:
            continue
        carved = _carve_one(s.polygon, s.source_axis, _faces, depth_m)
        if carved is None:
            continue
        new_rect, caps = carved
        s.polygon = new_rect
        for cap in caps:
            new_caps.append(BuiltShape(
                polygon=cap, role=ROLE_JUNCTION, ref=s.ref,
                is_rect_cap=True))
            n_caps += 1
    if new_caps:
        layout.shapes.extend(new_caps)
        UI.vprint(1,
            f"  [pav-builder] {getattr(layout, 'icao', '')}: carved "
            f"{n_caps} rect end-cap(s) before the spine.")
    return n_caps
