"""Phase 2: split a large/blobby apron at its narrow NECKS into convex pads.

An apron often pinches to roughly taxiway width between parking areas (or where
a taxiway enters/leaves).  Cutting at those pinch points turns one blob into
convex "pads" joined by short connectors, which (a) keeps every apron all-pair
surface convex so the grade cap is exact, and (b) gives the directional
elevation solver a clean pad/connector hierarchy.

Method — the medial axis (Voronoi skeleton) carries a clearance field
``clr(p) = p.distance(boundary)`` which IS the local half-width.  Along the
skeleton a neck is a PROMINENT local minimum of clearance in the taxiway-width
band: clearance dips to ~taxi width and rises to a wider pad on each side.
Cut at the "waist chord" — the minimum-width crossing at the neck (the
perpendicular segment through the skeleton point) — which lands on the real
pavement walls and is short (~taxi width), unlike the legacy runway-axis cut.

Guards against over-cutting (the sliver-fragmentation failure mode):
prominence test + a minimum pad area on every resulting piece.
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, MultiPoint, Point, Polygon
from shapely.ops import nearest_points, split

_GEOM_EXC = (ValueError, ZeroDivisionError, AttributeError, TypeError)

# Defaults (metres).
_HW_MIN = 3.0           # neck half-width floor (= width 6 m, a road)
_HW_MAX = 16.0          # neck half-width ceiling (= width 32 m, wide taxilane)
_SAMPLE = 3.0           # boundary densify spacing for the Voronoi skeleton
_PROMINENCE = 1.6       # a pad's clearance must exceed neck clr by this factor
_PROM_RADIUS_FACTOR = 3.0   # search radius for prominence = factor * hw_max
_MIN_PAD_AREA = 1500.0  # reject a cut that yields a piece below this (anti-sliver)
_MIN_SPLIT_AREA = 8000.0    # only neck-split pavement pieces bigger than this
_VERT_SNAP_M = 4.0      # snap waist-chord ends to a boundary vertex within this


def _flatten_lines(geom) -> list[LineString]:
    out, stack = [], [geom]
    while stack:
        g = stack.pop()
        if g is None or g.is_empty:
            continue
        if g.geom_type == "LineString":
            out.append(g)
        elif hasattr(g, "geoms"):
            stack.extend(g.geoms)
    return out


def _boundary_verts(P: Polygon) -> list[tuple[float, float]]:
    pts = list(P.exterior.coords)
    for h in P.interiors:
        pts += list(h.coords)
    return pts


def _polys(geom) -> list[Polygon]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", [])
            if g.geom_type == "Polygon" and not g.is_empty]


def _waist_chord(center, flow, P: Polygon, hw_max: float,
                 verts: "MultiPoint | None") -> LineString | None:
    """Cross-cut at a waist: a line through ``center`` PERPENDICULAR to the
    ``flow`` direction (the line connecting the two pad cores), clipped to the
    sub-segment of ``P`` that straddles ``center``, snapped to boundary
    vertices and extended slightly so ``shapely.split`` cleaves cleanly."""
    fm = math.hypot(flow[0], flow[1])
    if fm < 1e-6:
        return None
    px, py = -flow[1] / fm, flow[0] / fm          # perpendicular = waist dir
    cx, cy = center
    probe = LineString([(cx - px * 6 * hw_max, cy - py * 6 * hw_max),
                        (cx + px * 6 * hw_max, cy + py * 6 * hw_max)])
    try:
        inter = probe.intersection(P)
    except _GEOM_EXC:
        return None
    cpt = Point(cx, cy)
    best = None
    for seg in _flatten_lines(inter):
        if seg.distance(cpt) < 1.0:
            best = seg
            break
    if best is None or best.length < 1.0:
        return None
    a, b = best.coords[0], best.coords[-1]
    if verts is not None:
        a = _snap(a, verts)
        b = _snap(b, verts)
    dx, dy = b[0] - a[0], b[1] - a[1]
    dm = math.hypot(dx, dy)
    if dm < 1e-6:
        return None
    ux, uy = dx / dm, dy / dm
    return LineString([(a[0] - ux, a[1] - uy), (b[0] + ux, b[1] + uy)])


def _snap(pt, verts: "MultiPoint") -> tuple[float, float]:
    p = Point(pt)
    best = None
    bestd = _VERT_SNAP_M
    for g in verts.geoms:
        d = p.distance(g)
        if d < bestd:
            bestd = d
            best = (g.x, g.y)
    return best if best is not None else (pt[0], pt[1])


def _arc_len(edges: list[float], tot: float, i: int, j: int) -> float:
    """Shorter ring-arc spatial length between vertices i<j."""
    f = sum(edges[k] for k in range(i, j))
    return min(f, tot - f)


def neck_cuts(poly: Polygon,
              taxi_hw: float = _HW_MAX,
              max_mouth: float = 2.0 * _HW_MAX,
              min_excursion: float = 2.0 * _HW_MAX,
              min_neck_len: float = 12.0) -> list[tuple[tuple, tuple]]:
    """Return ``[(A, B), ...]`` mouth chords that cut ``poly`` at its necks.

    A mouth = two boundary vertices < ``max_mouth`` apart, non-adjacent on the
    ring, with a real boundary excursion (>= ``min_excursion``) between them
    and the chord crossing interior pavement.  Validated as a *neck* when the
    excursion beyond the chord has a narrow neck: eroding the excursion by
    ``taxi_hw`` leaves any wide pad core EMPTY (a thin arm throughout — CUT1)
    or at least ``min_neck_len`` away from the mouth (narrow neck then a pad —
    CUT2).  A false mouth opens straight into a wide core at the chord."""
    if poly.is_empty or poly.geom_type != "Polygon":
        return []
    ring = list(poly.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    n = len(ring)
    if n < 6:
        return []
    edges = [math.hypot(ring[(i + 1) % n][0] - ring[i][0],
                        ring[(i + 1) % n][1] - ring[i][1]) for i in range(n)]
    tot = sum(edges)

    raw = []
    for i in range(n):
        for j in range(i + 2, n):
            ax, ay = ring[i]
            bx, by = ring[j]
            d = math.hypot(ax - bx, ay - by)
            if d >= max_mouth:
                continue
            if min(j - i, n - (j - i)) < 2:
                continue
            if _arc_len(edges, tot, i, j) < min_excursion:
                continue
            mid = ((ax + bx) / 2.0, (ay + by) / 2.0)
            if not poly.contains(Point(mid)):
                continue
            raw.append((d, ring[i], ring[j], mid))
    raw.sort(key=lambda r: r[0])

    out = []
    for d, A, B, mid in raw:
        if any(math.hypot(mid[0] - m[0], mid[1] - m[1]) < max_mouth
               for _, _, _, m in out):
            continue                        # dedup: one cut per mouth
        # Cut and validate the excursion neck.
        dx, dy = B[0] - A[0], B[1] - A[1]
        dm = math.hypot(dx, dy) or 1.0
        ux, uy = dx / dm, dy / dm
        chord = LineString([(A[0] - ux, A[1] - uy), (B[0] + ux, B[1] + uy)])
        try:
            sub = _polys(split(poly, chord))
        except _GEOM_EXC:
            continue
        if len(sub) < 2:
            continue
        excursion = min(sub, key=lambda p: p.area)
        try:
            core = excursion.buffer(-taxi_hw)
        except _GEOM_EXC:
            continue
        chord_mouth = LineString([A, B])
        if not (core.is_empty
                or core.distance(chord_mouth) >= min_neck_len):
            continue                        # opens straight into a wide pad
        out.append((d, A, B, mid))
    return [(A, B) for _, A, B, _ in out]


def split_polygon_at_necks(poly: Polygon,
                           min_pad_area: float = _MIN_PAD_AREA,
                           min_arm_area: float = 200.0) -> list[Polygon]:
    """Split ``poly`` at every detected neck (:func:`neck_cuts`).  Each cut is
    applied to the piece containing its mouth, kept only if the larger half is
    >= ``min_pad_area`` and the smaller >= ``min_arm_area`` (anti-sliver).
    Returns ``[poly]`` when there is no qualifying neck."""
    if (poly.is_empty or poly.geom_type != "Polygon"
            or poly.area < _MIN_SPLIT_AREA):
        return [poly]
    cuts = neck_cuts(poly)
    if not cuts:
        return [poly]
    pieces = [poly]
    for A, B in cuts:
        dx, dy = B[0] - A[0], B[1] - A[1]
        dm = math.hypot(dx, dy) or 1.0
        ux, uy = dx / dm, dy / dm
        chord = LineString([(A[0] - ux, A[1] - uy), (B[0] + ux, B[1] + uy)])
        mid = ((A[0] + B[0]) / 2.0, (A[1] + B[1]) / 2.0)
        mp = Point(mid)
        nxt = []
        for pc in pieces:
            if pc.distance(mp) > 1.0:
                nxt.append(pc)
                continue
            try:
                sub = _polys(split(pc, chord))
            except _GEOM_EXC:
                nxt.append(pc)
                continue
            if (len(sub) >= 2
                    and max(p.area for p in sub) >= min_pad_area
                    and min(p.area for p in sub) >= min_arm_area):
                nxt.extend(sub)
            else:
                nxt.append(pc)
        pieces = nxt
    return pieces
