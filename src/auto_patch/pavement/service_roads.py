"""Ground-vehicle ``service_road`` network builder (session 47).

Builds a 4 %-grade rect + junction network for ground-vehicle routes —
apt.dat 1206 truck routes and OSM small roads — but ONLY where a route is
a dedicated strip OUTSIDE aircraft pavement.  Where a route instead
crosses an aircraft movement area (apron / taxiway / runway), nothing is
emitted: that surface's stricter aircraft grade rules already apply (per
user 2026-05-24).

Most service roads have no apt.dat / DSF pavement polygon, so a standard
corridor width is synthesised (``config.SERVICE_ROAD_WIDTH_M``).  The
network mirrors the taxiway model: ``service_road`` rects along straight
runs (graded along their axis at 4 %) + ``service_junction`` fill polygons
at bends / intersections (all-direction 4 %).  Cars handle steeper terrain
than aircraft, so these also double as apron↔DEM transition ramps.
"""
from __future__ import annotations

import math

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon
from shapely.ops import unary_union

from ..layout import ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION

_GEOM_EXC = (ValueError, TypeError, GEOSException, TopologicalError, IndexError)

# A route vertex within this distance of aircraft pavement counts as
# "on the movement area" and is excluded from the service-road network.
_PAV_CLEAR_TOL_M = 1.0
# Turn angle (deg) above which a polyline is split into a new straight run.
_BEND_ANGLE_DEG = 25.0
# Minimum service_junction fill-polygon area to keep (drop slivers).
_MIN_JUNCTION_AREA_M2 = 2.0


def _as_linestrings(geom) -> list[LineString]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "LineString":
        return [geom]
    if geom.geom_type == "MultiLineString":
        return [g for g in geom.geoms if not g.is_empty]
    return []


def _as_polygons(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type == "MultiPolygon":
        return [g for g in geom.geoms if not g.is_empty]
    return []


def _split_at_bends(coords: list[tuple[float, float]]
                    ) -> list[list[tuple[float, float]]]:
    """Split a polyline into straight-ish runs at vertices whose turn
    angle exceeds ``_BEND_ANGLE_DEG``."""
    if len(coords) < 2:
        return []
    runs: list[list[tuple[float, float]]] = []
    cur = [coords[0], coords[1]]
    cos_tol = math.cos(math.radians(_BEND_ANGLE_DEG))
    for k in range(1, len(coords) - 1):
        ax, ay = coords[k - 1]
        bx, by = coords[k]
        cx, cy = coords[k + 1]
        v1x, v1y = bx - ax, by - ay
        v2x, v2y = cx - bx, cy - by
        n1 = math.hypot(v1x, v1y)
        n2 = math.hypot(v2x, v2y)
        straight = (n1 > 1e-6 and n2 > 1e-6
                    and (v1x * v2x + v1y * v2y) / (n1 * n2) >= cos_tol)
        if straight:
            cur.append((cx, cy))
        else:
            runs.append(cur)
            cur = [(bx, by), (cx, cy)]
    runs.append(cur)
    return runs


def _rect_from_endpoints(ax: float, ay: float, bx: float, by: float,
                         width: float) -> tuple[Polygon, LineString] | None:
    """A 4-corner rect of ``width`` centred on A→B, corners in canonical
    ``[hi-left, lo-left, lo-right, hi-right]`` order, plus its axis."""
    dx, dy = bx - ax, by - ay
    L = math.hypot(dx, dy)
    if L < 1e-6:
        return None
    ux, uy = dx / L, dy / L
    nx, ny = -uy, ux
    h = width / 2.0
    try:
        poly = Polygon([
            (ax + nx * h, ay + ny * h),
            (bx + nx * h, by + ny * h),
            (bx - nx * h, by - ny * h),
            (ax - nx * h, ay - ny * h),
        ])
        axis = LineString([(ax, ay), (bx, by)])
    except _GEOM_EXC:
        return None
    if poly.is_empty or not poly.is_valid:
        return None
    return poly, axis


def build_service_road_network(
        centerlines: list[tuple[LineString, str]],
        pav_union: Polygon | None,
        *,
        width: float,
        min_len: float,
) -> tuple[list[tuple[Polygon, LineString, str, str]],
           list[tuple[Polygon, str, str]]]:
    """Build the ground-vehicle network from ``centerlines``
    (``[(LineString_m, name)]``).

    Returns ``(rects, junctions)``:
      * ``rects``     = ``[(rect, axis, ROLE_SERVICE_ROAD, name)]``
      * ``junctions`` = ``[(polygon, ROLE_SERVICE_JUNCTION, name)]``

    Only the portions of each route OUTSIDE aircraft pavement
    (``pav_union``) contribute.  Rects cover straight runs (trimmed back
    from bends / ends so they never overlap); junctions are the corridor
    residue (``corridor − rects``) at bends and intersections.  Width is
    the synthesised ``config.SERVICE_ROAD_WIDTH_M`` (most service roads
    have no pavement polygon).
    """
    rects: list[tuple[Polygon, LineString, str, str]] = []
    junctions: list[tuple[Polygon, str, str]] = []
    if not centerlines:
        return rects, junctions

    pav_buf = None
    pav_prep = None
    if pav_union is not None and not pav_union.is_empty:
        try:
            from shapely.prepared import prep
            pav_buf = pav_union.buffer(_PAV_CLEAR_TOL_M)
            pav_prep = prep(pav_buf)
        except _GEOM_EXC:
            pav_buf = None
            pav_prep = None

    # External (off-pavement) centerline pieces.  Most service roads are
    # already entirely off aircraft pavement — skip the expensive
    # difference() unless the road actually touches it (prepared check).
    ext: list[tuple[LineString, str]] = []
    for line, name in centerlines:
        if line is None or line.is_empty:
            continue
        if pav_buf is not None and pav_prep.intersects(line):
            g = line.difference(pav_buf)
        else:
            g = line
        for piece in _as_linestrings(g):
            if piece.length >= 1.0:
                ext.append((piece, name))
    if not ext:
        return rects, junctions

    half = width / 2.0

    # Corridor = standard-width buffer of every external piece, clipped
    # to stay off aircraft pavement.  Flat caps / mitre joins keep it
    # tight to the routes.
    try:
        corridor = unary_union(
            [p.buffer(half, cap_style=2, join_style=2) for p, _ in ext])
        if pav_buf is not None:
            corridor = corridor.difference(pav_buf)
        if not corridor.is_valid:
            corridor = corridor.buffer(0)
    except _GEOM_EXC:
        return rects, junctions
    if corridor.is_empty:
        return rects, junctions

    # Rects on straight runs, trimmed back from each end by ``half`` so
    # adjacent runs / crossing roads don't overlap (the gap becomes
    # junction fill).  Skip a rect that would touch aircraft pavement or
    # an already-kept rect.
    kept_polys: list[Polygon] = []
    for piece, name in ext:
        coords = list(piece.coords)
        for run in _split_at_bends(coords):
            if len(run) < 2:
                continue
            ax, ay = run[0]
            bx, by = run[-1]
            seg_len = math.hypot(bx - ax, by - ay)
            if seg_len <= 2.0 * half:
                continue
            ux, uy = (bx - ax) / seg_len, (by - ay) / seg_len
            ax2, ay2 = ax + ux * half, ay + uy * half      # trim start
            bx2, by2 = bx - ux * half, by - uy * half      # trim end
            if math.hypot(bx2 - ax2, by2 - ay2) < min_len:
                continue
            built = _rect_from_endpoints(ax2, ay2, bx2, by2, width)
            if built is None:
                continue
            rect, axis = built
            try:
                if pav_buf is not None and rect.intersects(pav_buf):
                    continue                       # pokes into aircraft pavement
                if any(rect.intersection(kp).area > 1.0 for kp in kept_polys):
                    continue                       # overlaps a kept rect
            except _GEOM_EXC:
                continue
            rects.append((rect, axis, ROLE_SERVICE_ROAD, name))
            kept_polys.append(rect)

    # Junctions = corridor − rects, keeping pieces above the sliver floor.
    try:
        residue = corridor
        if kept_polys:
            residue = corridor.difference(unary_union(kept_polys))
    except _GEOM_EXC:
        residue = corridor
    for poly in _as_polygons(residue):
        if poly.is_empty or not poly.is_valid:
            continue
        if poly.area >= _MIN_JUNCTION_AREA_M2:
            junctions.append((poly, ROLE_SERVICE_JUNCTION, "service"))

    return rects, junctions
