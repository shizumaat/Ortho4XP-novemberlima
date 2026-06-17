"""Self-partitioned junctions/aprons with a centerline "spine" (slice).

Problem (docs/junction_centerline_spine.md): junctions/aprons emit as a
single ring polygon.  X-Plane / Triangle4XP triangulate the interior by
interpolating between boundary vertices, so a taxi centerline crossing
the INTERIOR — where there are no vertices on it — renders a waving
surface instead of the corridor's clean ≤1.5% profile.

Fix (user's SLICE model): SLICE each junction/apron along every crossing
taxi centerline so the centerline becomes a real shared EDGE with nodes
on it.  This pass is PURE GEOMETRY and runs PRE-SOLVE (right after the
hole cuts, before ``_unify_airside_geometry`` and the per-surface
solver): it only cuts the polygons; the unify pass then welds the new
shared nodes and the elevation solver grades the whole sliced surface
coherently (centerline + boundary together), so the corridor profile is
the solver's job, not this pass's.

  1. Densify each crossing taxi centerline through the shape — interior
     "spine" nodes, clipped to ``pav_union − runway`` (never in a runway
     or off source pavement; user 2026-06-17).
  2. Attach each centerline END to the boundary:
       * SOFT end (node_altitudes apron / junction / free): the slice
         runs to the crossing point P right ON the centerline.
       * HARD end (sloping rect / runway): a CAP — a rectangle bridging
         the rect's two corners (C1,C2) to a 3-node inboard side (E1, E2
         at the pavement edges + M on the centerline), placed
         ≥ ``_CAP_DEPTH_M`` perpendicular from the flat edge so no
         junction/apron vertex lands in the rect's exclusion band
         (verification.check_vertex_on_flat_edge).  The centerline
         attaches at M, dead-centre — it never skews to a corner.
  3. Polygonize the shape with the capped centerline polylines as cut
     lines (honouring hole rings), keeping faces inside the polygon.
     Emit each piece as a geometry-only BuiltShape (NO altitudes — the
     solver assigns them).

Public API:
    apply_junction_centerline_spine(layout) -> int
        Slice every ROLE_JUNCTION / ROLE_APRON shape along its crossing
        centerlines.  Returns the count sliced.  No-op (0) when the gate
        is off.
"""
from __future__ import annotations

import math
from typing import List, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

import O4_UI_Utils as UI

from .config import JUNCTION_CENTERLINE_SPINE, SPINE_STEP_M
from .layout import (
    BuiltShape, ROLE_APRON, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL, ROLE_STUB)

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["apply_junction_centerline_spine"]

# Sloping 4-corner rects + runways: a junction/apron vertex may share only
# their CORNERS, never a mid-edge node — so a slice end meeting one gets a
# rectangle cap rather than a node on the edge.  Service ROADS are
# 4-corner sloping rects too (verification.check_vertex_on_flat_edge).
_HARD_END_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR, ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    "service_road"})
# A boundary crossing within this distance of a hard boundary is a HARD
# end (rectangle cap, no node on the flat edge).
_HARD_EDGE_TOL_M = 1.5
# A HARD cap's inboard nodes sit ≥ this far PERPENDICULAR from the flat
# edge — strictly beyond verification.check_vertex_on_flat_edge's
# EDGE_PROX_M (1.5 m), so no junction/apron vertex lands in the band.
_CAP_DEPTH_M = 2.0
# Spine nodes are densified ~this far inboard of each boundary crossing.
_END_INSET_M = 1.5
# Coordinate rounding (m) for polygonize snap.
_RND = 3
# Drop emitted pieces smaller than this (slivers).
_MIN_PIECE_AREA = 0.25


def _open(ring):
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _coords(items):
    out: List[LineString] = []
    for item in (items or []):
        ln = item[0] if isinstance(item, tuple) else item
        if ln is not None and not ln.is_empty:
            out.append(ln)
    return out


def _full_centerlines(layout):
    """The spine's taxi centerlines: PREFER the painted (row-120) BEZIER
    curves (continuous arcs through junctions) and fill gaps with the
    1201/1202 edges they do NOT cover; plus the discovered lanes.  Falls
    back to the plain 1201/1202 + discovered set when no painted curves
    were computed (e.g. the gate built them into ``apt_taxi_centerlines``
    already, or none exist).  Runway long-axes are EXCLUDED — runway grade
    is the FAA profile's job, and a spine node must never land in a runway
    (user 2026-06-17)."""
    painted = list(getattr(layout, "_painted_centerlines", None) or [])
    apt_cl = _coords(getattr(layout, "apt_taxi_centerlines", None))
    disc = _coords(getattr(layout, "_discovered_centerlines", None))
    if not painted:
        return apt_cl + disc
    # De-dup: keep only the 1201/1202 edges the painted curves don't
    # already cover (within ~6 m for most of their length), so no taxiway
    # is sliced twice by two slightly-offset lines.
    try:
        pbuf = unary_union(painted).buffer(6.0)
    except _GEOM_EXC:
        pbuf = None
    uncovered: List[LineString] = []
    for ln in apt_cl:
        if pbuf is None or ln.length < 1e-6:
            uncovered.append(ln)
            continue
        try:
            cov = ln.intersection(pbuf).length / ln.length
        except _GEOM_EXC:
            cov = 0.0
        if cov < 0.7:
            uncovered.append(ln)
    return painted + uncovered + disc


def _perp_dist(qx, qy, c1, c2):
    """Perpendicular distance of ``(qx,qy)`` from the line through
    ``c1``–``c2`` (the rect's flat edge)."""
    ex, ey = c2[0] - c1[0], c2[1] - c1[1]
    eL = math.hypot(ex, ey)
    if eL < 1e-9:
        return math.hypot(qx - c1[0], qy - c1[1])
    return abs((qx - c1[0]) * ey - (qy - c1[1]) * ex) / eL


def _make_cap(mx, my, c1, c2, poly):
    """Rectangle CAP cut-lines bridging the rect corners (C1,C2) to a
    3-node inboard side — E1, E2 at the pavement edges and M
    (``mx,my``, on the centerline) in the middle.  Returns ``cut_lines``
    or ``None`` when infeasible.  M is assumed already ≥ ``_CAP_DEPTH_M``
    perpendicular from the edge, so E1, E2 clear it too."""
    ex, ey = c2[0] - c1[0], c2[1] - c1[1]
    eL = math.hypot(ex, ey)
    if eL < 1e-6:
        return None
    ux, uy = ex / eL, ey / eL
    half_w = 0.5 * eL
    e1 = (mx - half_w * ux, my - half_w * uy)
    e2 = (mx + half_w * ux, my + half_w * uy)
    if (math.hypot(e1[0] - c1[0], e1[1] - c1[1])
            > math.hypot(e1[0] - c2[0], e1[1] - c2[1])):
        e1, e2 = e2, e1
    try:
        if not (poly.contains(Point(*e1)) and poly.contains(Point(*e2))):
            return None
    except _GEOM_EXC:
        return None
    return [LineString([c1, e1]), LineString([c2, e2]),
            LineString([e1, (mx, my)]), LineString([(mx, my), e2])]


def _partition_junction(s, centerlines, pav_union, runway_union, near_hard):
    """Slice junction/apron ``s`` along its crossing centerlines and
    return the list of piece Polygons (geometry only), or None to leave
    the shape unchanged."""
    poly = s.polygon
    ropen = _open(list(poly.exterior.coords))
    if len(ropen) < 3:
        return None, "degenerate" 

    # Spine nodes live only where the shape overlaps SOURCE pavement
    # (apt.dat + DSF) MINUS runways.
    pav_clip = poly
    if pav_union is not None and not pav_union.is_empty:
        try:
            pav_clip = poly.intersection(pav_union)
        except _GEOM_EXC:
            pav_clip = poly
    if runway_union is not None and not runway_union.is_empty:
        try:
            pav_clip = pav_clip.difference(runway_union)
        except _GEOM_EXC:
            pass
    if pav_clip.is_empty:
        return None, "off_pavement" 

    _ring_lists = [ropen]
    for _hole in poly.interiors:
        _hl = _open(list(_hole.coords))
        if len(_hl) >= 3:
            _ring_lists.append(_hl)

    def _edge_corners(px, py):
        best = None
        for rl in _ring_lists:
            m = len(rl)
            for i in range(m):
                ax, ay = rl[i]
                bx, by = rl[(i + 1) % m]
                dx, dy = bx - ax, by - ay
                s2 = dx * dx + dy * dy
                if s2 < 1e-12:
                    continue
                t = ((px - ax) * dx + (py - ay) * dy) / s2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                cx, cy = ax + t * dx, ay + t * dy
                d = (px - cx) ** 2 + (py - cy) ** 2
                if best is None or d < best[0]:
                    best = (d, (ax, ay), (bx, by))
        if best is None:
            return None, None
        return best[1], best[2]

    def _on_pav(px, py):
        try:
            return pav_clip.distance(Point(px, py)) <= 0.1
        except _GEOM_EXC:
            return True

    cut_lines: List[LineString] = []
    for ln in centerlines:
        try:
            seg = poly.intersection(ln)
        except _GEOM_EXC:
            continue
        if seg.is_empty:
            continue
        parts = []
        if seg.geom_type == "LineString":
            parts = [seg]
        elif seg.geom_type == "MultiLineString":
            parts = list(seg.geoms)
        elif seg.geom_type == "GeometryCollection":
            parts = [g for g in seg.geoms if g.geom_type == "LineString"]
        for part in parts:
            L = part.length
            if L < max(2.0, 0.5 * SPINE_STEP_M):
                continue
            nseg = max(2, int(round(L / SPINE_STEP_M)))
            ds = {min(_END_INSET_M, 0.45 * L),
                  L - min(_END_INSET_M, 0.45 * L)}
            for i in range(1, nseg):
                ds.add(i * L / nseg)
            cand: List[Tuple[float, float]] = []
            for d in sorted(x for x in ds if 0.0 < x < L):
                p = part.interpolate(d)
                if _on_pav(p.x, p.y):
                    cand.append((p.x, p.y))
            if not cand:
                continue
            lo, hi = 0, len(cand) - 1
            extra: List[LineString] = []
            end_soft = {True: False, False: False}
            for is_entry in (True, False):
                px, py = (part.coords[0] if is_entry else part.coords[-1])
                c1, c2 = _edge_corners(px, py)
                if not (near_hard(px, py) and c1 is not None
                        and c2 is not None):
                    end_soft[is_entry] = True
                    continue
                # HARD end: M must clear the rect flat edge by > EDGE_PROX.
                if is_entry:
                    while (lo <= hi and _perp_dist(cand[lo][0], cand[lo][1],
                                                   c1, c2) < _CAP_DEPTH_M):
                        lo += 1
                    mi = lo
                else:
                    while (hi >= lo and _perp_dist(cand[hi][0], cand[hi][1],
                                                   c1, c2) < _CAP_DEPTH_M):
                        hi -= 1
                    mi = hi
                if lo > hi:
                    break
                cuts = _make_cap(cand[mi][0], cand[mi][1], c1, c2, poly)
                if cuts is None:
                    end_soft[is_entry] = True
                else:
                    extra.extend(cuts)
            if lo > hi:
                continue
            slice_pts: List[Tuple[float, float]] = list(cand[lo:hi + 1])
            for is_entry in (True, False):
                if not end_soft[is_entry]:
                    continue
                px, py = (part.coords[0] if is_entry else part.coords[-1])
                if is_entry:
                    slice_pts = [(px, py)] + slice_pts
                else:
                    slice_pts = slice_pts + [(px, py)]
            if len(slice_pts) >= 2:
                try:
                    cut_lines.append(LineString(slice_pts))
                except _GEOM_EXC:
                    pass
            cut_lines.extend(extra)
    if not cut_lines:
        return None, "no_cut" 

    # Polygonize against the FULL boundary (exterior + hole rings).  Union
    # with a GRID_SIZE so the cut endpoints — which land a few µm off the
    # boundary edge in the raw pre-solve geometry — snap onto it and NODE;
    # without grid-snapped noding polygonize leaves clean boundary-to-
    # boundary cuts unsplit.
    from shapely import union_all
    try:
        arrangement = union_all([poly.boundary] + cut_lines, grid_size=0.01)
        raw = [f for f in polygonize(arrangement)
               if not f.is_empty and f.geom_type == "Polygon"]
        faces = []
        for f in raw:
            if f.area < _MIN_PIECE_AREA:
                continue
            try:
                if not poly.contains(f.representative_point()):
                    continue
            except _GEOM_EXC:
                continue
            faces.append(f)
    except _GEOM_EXC:
        return None, "polygonize_err"
    if len(faces) > 1:
        return faces, "ok"
    return None, f"single_face(raw={len(raw)},cuts={len(cut_lines)})"


def apply_junction_centerline_spine(layout) -> int:
    """Slice every ROLE_JUNCTION / ROLE_APRON shape along its crossing
    taxi centerlines (geometry only — the solver grades the pieces).
    Mutates ``layout.shapes``.  Returns the count of shapes sliced."""
    if not JUNCTION_CENTERLINE_SPINE:
        return 0
    centerlines = _full_centerlines(layout)
    if not centerlines:
        return 0
    import os as _os
    _DEBUG = _os.environ.get("O4_JCT_SPINE_DEBUG") == "1"
    _skips = []
    if _DEBUG:
        UI.vprint(1, "  [pav-builder] junction-spine source: %d centerline(s)"
                  " (painted=%d, apt=%d, disc=%d)" % (
                      len(centerlines),
                      len(getattr(layout, "_painted_centerlines", None) or []),
                      len(getattr(layout, "apt_taxi_centerlines", None) or []),
                      len(getattr(layout, "_discovered_centerlines", None)
                          or [])))
    pav_union = getattr(layout, "_source_pav_union", None)
    runway_union = getattr(layout, "runway_union", None)

    # Index sloping-rect / runway boundaries for the HARD-end test.
    hard_lines = []
    for s in layout.shapes:
        if (s.role in _HARD_END_ROLES and s.polygon is not None
                and not s.polygon.is_empty
                and s.polygon.geom_type == "Polygon"):
            try:
                hard_lines.append(s.polygon.exterior)
            except _GEOM_EXC:
                continue
    hard_tree = STRtree(hard_lines) if hard_lines else None

    def _near_hard(px, py):
        if hard_tree is None:
            return False
        p = Point(px, py)
        for idx in hard_tree.query(p.buffer(_HARD_EDGE_TOL_M)):
            try:
                if hard_lines[idx].distance(p) <= _HARD_EDGE_TOL_M:
                    return True
            except _GEOM_EXC:
                continue
        return False

    new_shapes: List[BuiltShape] = []
    n_done = 0
    n_pieces = 0
    for s in layout.shapes:
        if s.role not in (ROLE_JUNCTION, ROLE_APRON):
            new_shapes.append(s)
            continue
        poly = s.polygon
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            new_shapes.append(s)
            continue
        crossing = []
        for ln in centerlines:
            try:
                if poly.intersects(ln):
                    crossing.append(ln)
            except _GEOM_EXC:
                continue
        if not crossing:
            new_shapes.append(s)
            continue
        pieces, reason = _partition_junction(
            s, crossing, pav_union, runway_union, _near_hard)
        if not pieces:
            new_shapes.append(s)
            if _DEBUG:
                try:
                    c = s.polygon.representative_point()
                    la, lo = layout.m_to_ll(c.x, c.y)
                    _skips.append((reason, s.role, round(la, 5), round(lo, 5)))
                except Exception:
                    _skips.append((reason, s.role, 0, 0))
            continue
        for f in pieces:
            # Geometry only — no altitudes; the per-surface solver grades
            # these pieces (the unify pass first welds the new nodes).
            new_shapes.append(BuiltShape(polygon=f, role=s.role,
                                         ref=s.ref))
            n_pieces += 1
        n_done += 1

    layout.shapes = new_shapes
    if n_done:
        UI.vprint(1,
            f"  [pav-builder] {getattr(layout, 'icao', '')}: "
            f"junction-spine sliced {n_done} junction/apron(s) into "
            f"{n_pieces} piece(s) (pre-solve geometry).")
    if _DEBUG and _skips:
        from collections import Counter
        UI.vprint(1, "  [pav-builder] junction-spine SKIPPED %d: %s" % (
            len(_skips), dict(Counter(r for r, *_ in _skips))))
    return n_done
