"""Self-triangulated junctions with a centerline "spine".

Problem (docs/junction_centerline_spine.md): junctions emit as a single
ring polygon with boundary-only ``node_altitudes``.  X-Plane / Triangle4XP
triangulate the interior by interpolating between boundary vertices, so a
taxi centerline crossing the junction INTERIOR — where there are no
vertices on it — renders a waving surface instead of the solver's clean
≤1.5% corridor profile (OMAA taxiway H @ junction -10225: field is a flat
1.5% but the emitted surface spikes to 3.6%).

Fix: re-emit each junction as our OWN constrained triangulation that
includes interior "spine" nodes placed every ``SPINE_STEP_M`` along each
crossing centerline, each pinned to the network-profile field value at
that point (``layout._network_profile_field.sample``).  X-Plane renders
the triangles as-is, so the surface now follows the spine → the
centerline grades ≤1.5% through the junction.

Public API:
    apply_junction_centerline_spine(layout) -> int
        Replace each ROLE_JUNCTION shape with its spine triangulation.
        Returns the number of junctions re-triangulated.  No-op (returns
        0) when the gate is off or the field is missing.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.ops import triangulate as _delaunay

import O4_UI_Utils as UI

from .config import JUNCTION_CENTERLINE_SPINE, SPINE_STEP_M
from .layout import BuiltShape, ROLE_JUNCTION, ROLE_RUNWAY

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["apply_junction_centerline_spine"]


# Perpendicular tolerance for a vertex / spine node to be considered
# "on" a crossing centerline (taxi half-width + slack), mirrors
# unified_jacobi.JUNCTION_AXIS_PERP_TOL_M.
_SPINE_PERP_TOL_M = 15.0
# Spine nodes closer than this to a boundary vertex (or to each other)
# are dropped — avoids slivers / duplicate nodes the triangulator chokes
# on, and keeps the node welding in to_osm clean.
_MIN_NODE_SEP_M = 2.0


def _open(ring):
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _full_centerlines(layout):
    """Every centerline of the FULL route graph as plain LineStrings:
    apt.dat taxi network + discovered/unreferenced lanes + runway
    long-axes (for runway-crossing junctions)."""
    out: List[LineString] = []
    for item in (getattr(layout, "apt_taxi_centerlines", None) or []):
        ln = item[0] if isinstance(item, tuple) else item
        if ln is not None and not ln.is_empty:
            out.append(ln)
    for item in (getattr(layout, "_discovered_centerlines", None) or []):
        ln = item[0] if isinstance(item, tuple) else item
        if ln is not None and not ln.is_empty:
            out.append(ln)
    # Runway long-axes (midpoints of the two short edges).
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty:
            continue
        try:
            rc = _open(list(s.polygon.exterior.coords))
        except _GEOM_EXC:
            continue
        if len(rc) != 4:
            continue
        a_mid = (0.5 * (rc[0][0] + rc[3][0]), 0.5 * (rc[0][1] + rc[3][1]))
        b_mid = (0.5 * (rc[1][0] + rc[2][0]), 0.5 * (rc[1][1] + rc[2][1]))
        try:
            out.append(LineString([a_mid, b_mid]))
        except _GEOM_EXC:
            continue
    return out


def _spine_nodes_for_polygon(poly: Polygon, centerlines, field,
                             ring_pts) -> List[Tuple[float, float, float]]:
    """Interior spine nodes ``[(x, y, z)]`` for ``poly``: densify every
    crossing centerline inside the polygon at SPINE_STEP_M and assign each
    point the field value.  Nodes too close to a boundary vertex or to a
    previously-kept spine node are dropped."""
    kept: List[Tuple[float, float, float]] = []
    sep2 = _MIN_NODE_SEP_M * _MIN_NODE_SEP_M
    # Shrink the polygon slightly so spine nodes stay strictly interior
    # (a node exactly on the boundary breaks the contains-centroid clip
    # and duplicates a boundary node).
    try:
        inner = poly.buffer(-0.5)
    except _GEOM_EXC:
        inner = poly
    if inner.is_empty:
        inner = poly

    def _too_close(x, y):
        for (bx, by) in ring_pts:
            if (bx - x) ** 2 + (by - y) ** 2 < sep2:
                return True
        for (kx, ky, _kz) in kept:
            if (kx - x) ** 2 + (ky - y) ** 2 < sep2:
                return True
        return False

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
            if L < _MIN_NODE_SEP_M:
                continue
            n = max(1, int(math.floor(L / SPINE_STEP_M)))
            for k in range(1, n + 1):
                d = k * SPINE_STEP_M
                if d >= L:
                    break
                p = part.interpolate(d)
                x, y = p.x, p.y
                if not inner.contains(Point(x, y)):
                    continue
                if _too_close(x, y):
                    continue
                if field is not None:
                    v, gap = field.sample(x, y)
                else:
                    v, gap = None, float("inf")
                if v is None:
                    continue
                kept.append((x, y, float(v)))
    return kept


def _triangulate(poly: Polygon, verts, vz):
    """Delaunay-triangulate the point set ``verts`` (boundary + spine),
    keep triangles whose centroid lies inside ``poly``.  No Steiner
    points are introduced, so every triangle vertex is one of our nodes
    (with a known z).  Returns ``[(ring_xy[3], ring_z[3])]``."""
    if len(verts) < 3:
        return []
    zlut = {(round(x, 3), round(y, 3)): z for (x, y), z in zip(verts, vz)}
    try:
        tris = _delaunay(MultiPoint([Point(x, y) for (x, y) in verts]))
    except _GEOM_EXC:
        return []
    out = []
    for t in tris:
        try:
            c = t.centroid
            if not poly.contains(c):
                continue
            coords = list(t.exterior.coords)[:3]
        except _GEOM_EXC:
            continue
        zs = []
        ok = True
        for (px, py) in coords:
            z = zlut.get((round(px, 3), round(py, 3)))
            if z is None:
                ok = False
                break
            zs.append(z)
        if ok:
            out.append((coords, zs))
    return out


def apply_junction_centerline_spine(layout) -> int:
    """Re-emit every ROLE_JUNCTION shape as a centerline-spine triangle
    fan.  Mutates ``layout.shapes`` in place.  Returns the count of
    junctions re-triangulated."""
    if not JUNCTION_CENTERLINE_SPINE:
        return 0
    field = getattr(layout, "_network_profile_field", None)
    centerlines = _full_centerlines(layout)

    new_shapes: List[BuiltShape] = []
    n_done = 0
    n_tris = 0
    for s in layout.shapes:
        if s.role != ROLE_JUNCTION:
            new_shapes.append(s)
            continue
        poly = s.polygon
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            new_shapes.append(s)
            continue
        ring = list(poly.exterior.coords)
        ropen = _open(ring)
        n = len(ropen)
        # Boundary per-vertex elevations.
        if s.node_altitudes and len(s.node_altitudes) >= len(ring):
            bz = [float(e) for e in s.node_altitudes[:n]]
        elif s.altitude is not None:
            bz = [float(s.altitude)] * n
        else:
            # No usable elevation — leave the shape unchanged.
            new_shapes.append(s)
            continue
        if len(bz) != n:
            new_shapes.append(s)
            continue

        # Only crossing centerlines that actually pass through this poly.
        crossing = []
        for ln in centerlines:
            try:
                if poly.intersects(ln):
                    crossing.append(ln)
            except _GEOM_EXC:
                continue
        spine = (_spine_nodes_for_polygon(poly, crossing, field, ropen)
                 if crossing else [])
        if not spine:
            # No interior spine to add — keep the ring polygon as-is so
            # this is a strict no-change for junctions with no through
            # centerline (and so triangle count doesn't grow needlessly).
            new_shapes.append(s)
            continue

        verts = list(ropen) + [(x, y) for (x, y, _z) in spine]
        vz = list(bz) + [z for (_x, _y, z) in spine]
        tris = _triangulate(poly, verts, vz)
        if len(tris) < 1:
            new_shapes.append(s)
            continue

        for coords, zs in tris:
            try:
                tp = Polygon(coords)
                if not tp.is_valid:
                    tp = tp.buffer(0)
                if (tp.is_empty or tp.geom_type != "Polygon"
                        or tp.area < 0.25):
                    continue
            except _GEOM_EXC:
                continue
            # node_altitudes spans the closed ring; match the emitted
            # ring order (Polygon closes the first vertex).
            cring = list(tp.exterior.coords)
            ce = []
            for (rx, ry) in cring:
                best = zs[0]
                bd = float("inf")
                for (cx, cy), cze in zip(coords, zs):
                    d = (rx - cx) ** 2 + (ry - cy) ** 2
                    if d < bd:
                        bd = d
                        best = cze
                ce.append(round(float(best), 1))
            ns = BuiltShape(polygon=tp, role=ROLE_JUNCTION, ref=s.ref)
            if max(ce) - min(ce) < 0.05:
                ns.altitude = round(sum(ce[:-1]) / max(1, len(ce) - 1), 1)
            else:
                ns.node_altitudes = ce
            new_shapes.append(ns)
            n_tris += 1
        n_done += 1

    layout.shapes = new_shapes
    if n_done:
        UI.vprint(1,
            f"  [pav-builder] {getattr(layout, 'icao', '')}: "
            f"junction-spine triangulated {n_done} junction(s) into "
            f"{n_tris} triangle(s).")
    return n_done
