"""Discover unreferenced taxiway centerlines from raw pavement geometry.

Small / remote airports (and the fringes of larger ones) have real taxiways
that carry NO apt.dat row-1201/1202 edge and NO OSM ``aeroway=taxiway`` way.
Such pavement otherwise dissolves into the all-pair junction/apron residue
(``pav_union − rects``), where a long thin lane becomes an all-pair surface
that cannot articulate as a travel path and is a grade liability.

This module extracts the **medial axis** (skeleton) of the lane-width pavement
and synthesises a centerline along each lane, so the SINGLE downstream
``_build_taxi_rects`` pass turns them into axial taxi rects exactly like a
referenced taxiway.  A lane that runs through / alongside an apron is handled
the same way a referenced taxiway is — the rect emits and the existing
apron-absorption pass dissolves it where it shares an edge with the apron.

Why a medial axis (not rectangle fitting): unreferenced lanes form a connected
*web*; fitting one rectangle per residue blob collapses the web into a single
low-fill shape.  The medial axis follows each lane individually, bends and all,
and splits naturally at junctions.

Method (shapely only — no scipy/skimage):
  1. Densify the pavement boundary and take its Voronoi diagram; the Voronoi
     edges interior to the polygon approximate the medial axis.
  2. Keep medial edges whose clearance to the boundary is in the taxiway
     half-width band (so apron cores, which have large clearance, drop out).
  3. Merge edges into polylines, prune short spurs, and keep lanes ≥ ``min_len``
     that a real centerline doesn't already cover.
"""
from __future__ import annotations

import math

from shapely.geometry import LineString, MultiPoint, Point
from shapely.ops import voronoi_diagram, linemerge, unary_union
from shapely import segmentize

from ..apt_dat_reader import TaxiCenterline

_GEOM_EXC = (ValueError, ZeroDivisionError, AttributeError, TypeError)

# Defaults (metres). Width band → clearance band is width/2.
_WIDTH_MIN = 6.0
_WIDTH_MAX = 32.0        # ICAO F taxiway = 25 m; allow wide taxilanes
_MIN_LEN = 40.0          # only substantial lanes (shorter = apron/GA web)
_SAMPLE = 2.5            # boundary densify spacing
_PRUNE_LEN = 15.0        # spur removal threshold
_MAX_BOUNDARY_PTS = 40000   # perf guard for very large airports
_MIN_POLY_AREA = 500.0      # skip tiny pavement scraps
# Discovered centerlines that run nearly PARALLEL to and CLOSE TO a runway are
# medial artifacts along the apron/runway edge, not real lanes (user 2026-05-28,
# SPJC TX24/25) — drop them so that pavement stays a single junction/apron.
_RUNWAY_PARALLEL_MAX_DEG = 15.0
_RUNWAY_NEAR_M = 25.0


def _line_bearing_deg(ls) -> float:
    c = list(ls.coords)
    return math.degrees(math.atan2(c[-1][1] - c[0][1], c[-1][0] - c[0][0]))


def _bearings_aligned(a: float, b: float, tol: float) -> bool:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d) < tol


def _flatten_lines(geom) -> list[LineString]:
    out: list[LineString] = []
    stack = [geom]
    while stack:
        g = stack.pop()
        if g is None or g.is_empty:
            continue
        if g.geom_type == "LineString":
            out.append(g)
        elif hasattr(g, "geoms"):
            stack.extend(g.geoms)
    return out


def _key(pt) -> tuple[float, float]:
    return (round(pt[0] * 2) / 2, round(pt[1] * 2) / 2)


def _medial_segments(P, hw_min: float, hw_max: float, sample: float):
    """Voronoi medial-axis segments of ``P`` whose clearance is in band."""
    bnd = P.boundary
    perim = bnd.length
    step = sample
    if perim / step > _MAX_BOUNDARY_PTS:
        step = perim / _MAX_BOUNDARY_PTS
    dense = segmentize(bnd, step)
    coords: list[tuple[float, float]] = []
    for g in (dense.geoms if dense.geom_type.startswith("Multi") else [dense]):
        coords += list(g.coords)
    if len(coords) < 4:
        return []
    try:
        vor = voronoi_diagram(MultiPoint(coords), edges=True)
    except _GEOM_EXC:
        return []
    except Exception:  # shapely GEOS can raise base Exception subclasses
        return []
    segs: list[LineString] = []
    for ln in _flatten_lines(vor):
        cc = list(ln.coords)
        for i in range(len(cc) - 1):
            a, b = cc[i], cc[i + 1]
            mid = Point((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
            if not P.contains(mid):
                continue
            if not (hw_min <= mid.distance(bnd) <= hw_max):
                continue
            if not (P.contains(Point(a)) and P.contains(Point(b))):
                continue
            segs.append(LineString([a, b]))
    return segs


def _prune(polylines: list[LineString], prune_len: float) -> list[LineString]:
    """Drop short spurs: a polyline with a degree-1 (free) endpoint and length
    < ``prune_len`` is removed; iterate until stable."""
    pls = list(polylines)
    changed = True
    while changed:
        changed = False
        deg: dict[tuple[float, float], int] = {}
        for p in pls:
            for end in (_key(p.coords[0]), _key(p.coords[-1])):
                deg[end] = deg.get(end, 0) + 1
        keep = []
        for p in pls:
            free = (deg[_key(p.coords[0])] == 1
                    or deg[_key(p.coords[-1])] == 1)
            if free and p.length < prune_len:
                changed = True
                continue
            keep.append(p)
        pls = keep
    return pls


def _polys(geom) -> list:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [g for g in geom.geoms if g.geom_type == "Polygon"
                and not g.is_empty]
    return []


# Max corner-angle deviation from 90° for a KEEPABLE discovered taxi rect.
# A clean free-strip lane on regular pavement yields a near-rectangle; an
# apron-EMBEDDED lane gets snap-distorted by the rect builder (corners pulled
# onto the ragged apron boundary) and comes out sheared.  Discovery scopes
# itself to the free strips; sheared (apron-embedded) lanes are deferred to
# the narrow-neck apron-decomposition pass (Phase 2), which splits the apron
# into pads rather than ramming a rect through it.
_MAX_SHEAR_DEG = 10.0


def _rect_corner_shear_deg(poly) -> float:
    """Max deviation (degrees) of a polygon's corner angles from 90°.
    Collinear nodes (turn < 8°) are ignored; 0 for a perfect rectangle."""
    try:
        rc = list(poly.exterior.coords)
    except _GEOM_EXC:
        return 0.0
    if rc and rc[0] == rc[-1]:
        rc = rc[:-1]
    n = len(rc)
    if n < 3:
        return 0.0
    worst = 0.0
    for i in range(n):
        a, b, c = rc[(i - 1) % n], rc[i], rc[(i + 1) % n]
        t = abs(math.degrees(math.atan2(c[1] - b[1], c[0] - b[0])
                             - math.atan2(b[1] - a[1], b[0] - a[0])))
        t = min(t, 360.0 - t)
        if t < 8.0:
            continue
        worst = max(worst, abs(t - 90.0))
    return worst


def _perp_exceeds_axial(rect, axis) -> bool:
    """True when the rect's extent PERPENDICULAR to its own axis exceeds its
    extent ALONG the axis — i.e. it's wider than it is long relative to its
    centerline (user 2026-05-28).  A real taxi lane runs LONG along its axis
    (discovery requires len >= 40 m, width <= 32 m), so this only fires on a
    mis-built apron blob: a short/refless centerline whose snap-to-pavement
    blew the perpendicular width out past the axial length (SPJC #44 = 27 m
    axial x 122 m perpendicular, source_axis along its short side)."""
    try:
        ac = list(axis.coords)
        rc = list(rect.exterior.coords)
    except _GEOM_EXC:
        return False
    if len(ac) < 2 or len(rc) < 4:
        return False
    if rc and rc[0] == rc[-1]:
        rc = rc[:-1]
    ax, ay = ac[-1][0] - ac[0][0], ac[-1][1] - ac[0][1]
    al = math.hypot(ax, ay)
    if al < 1e-6:
        return False
    ux, uy = ax / al, ay / al
    nx, ny = -uy, ux
    along = [x * ux + y * uy for x, y in rc]
    perp = [x * nx + y * ny for x, y in rc]
    return (max(perp) - min(perp)) > (max(along) - min(along))


def reject_curved_discovered_rects(taxi_rects, max_shear_deg=_MAX_SHEAR_DEG):
    """Drop UNREFERENCED rects (discovered ``TX…`` or ``ref=''``) that aren't
    real lanes — they stay as residue (handled by Phase-2 apron decomposition).
    Free-strip lanes (clean rects) and any apt.dat-referenced taxiway are kept.
    Returns ``(kept_list, n_dropped)``.

    Two rejection criteria, both apron-blob signatures:
      * SHEARED — corner angles deviate > ``max_shear_deg`` from 90° (an
        apron-embedded lane the rect builder's snap distorted);
      * WIDER-THAN-LONG — extent perpendicular to the axis exceeds the axial
        extent (a short/refless centerline snapped into a wide apron rect,
        e.g. SPJC #44).  Real lanes are long along their axis, never this.
    """
    kept = []
    dropped = 0
    for entry in taxi_rects:
        rect, axis, _role, ref = entry
        unref = (not ref) or ref.startswith("TX")
        if (unref and rect is not None and not rect.is_empty
                and (_rect_corner_shear_deg(rect) > max_shear_deg
                     or _perp_exceeds_axial(rect, axis))):
            dropped += 1
            continue
        kept.append(entry)
    return kept, dropped


def discover_unreferenced_centerlines(
    pav_union,
    existing_centerlines: list,
    rwy_centerlines: list | None = None,
    *,
    runway_union=None,
    building_union=None,
    width_min: float = _WIDTH_MIN,
    width_max: float = _WIDTH_MAX,
    min_len: float = _MIN_LEN,
    sample: float = _SAMPLE,
    prune_len: float = _PRUNE_LEN,
) -> list[tuple[LineString, str]]:
    """Return ``[(centerline, ref)]`` for lane-width pavement skeletons in
    ``pav_union`` that no ``existing_centerlines`` line already covers.

    ``existing_centerlines`` is the pipeline's ``[(LineString, ref), ...]``
    (a bare list of LineStrings is also accepted).  Synthesised refs are
    ``"TX1", "TX2", …`` so downstream role/dedup logic treats them as
    unreferenced (they classify by geometry, not name).

    Each raw medial polyline is fed through the SHARED
    :func:`pavement.centerlines.split_merged_centerline` — the same RDP +
    bend-split / curve→junction machinery referenced taxiways use — so a
    curving lane emits straight rects and leaves the curve as junction
    territory, identical to a referenced centerline (no special path).
    """
    from .centerlines import split_merged_centerline
    parts = [p for p in _polys(pav_union) if p.area >= _MIN_POLY_AREA]
    if not parts:
        return []
    hw_min, hw_max = width_min / 2.0, width_max / 2.0

    existing_lines = []
    for c in existing_centerlines or ():
        ls = c.line if hasattr(c, "line") else (c[0] if isinstance(c, tuple) else c)
        if isinstance(ls, LineString) and not ls.is_empty:
            existing_lines.append(ls)
    covered = None
    if existing_lines:
        try:
            covered = unary_union(existing_lines).buffer(hw_max + 2.0)
        except _GEOM_EXC:
            covered = None

    # Runway bearings (for the parallel-to-runway artifact filter below).
    rwy_bearings: list[float] = []
    for rc in rwy_centerlines or ():
        ls = rc.line if hasattr(rc, "line") else (rc[0] if isinstance(rc, tuple) else rc)
        if isinstance(ls, LineString) and len(ls.coords) >= 2:
            rwy_bearings.append(_line_bearing_deg(ls))
    rwy_near = (runway_union if (runway_union is not None
                                 and not runway_union.is_empty) else None)

    out: list[tuple[LineString, str]] = []
    n = 0
    for P in parts:
        segs = _medial_segments(P, hw_min, hw_max, sample)
        if not segs:
            continue
        try:
            merged = linemerge(unary_union(segs))
        except _GEOM_EXC:
            continue
        pls = _prune(_flatten_lines(merged), prune_len)
        if not pls:
            continue
        try:
            remerged = linemerge(unary_union(pls))
        except _GEOM_EXC:
            continue
        for ln in _flatten_lines(remerged):
            if ln.length < min_len:
                continue
            n += 1
            ref = f"TX{n}"
            # Shared splitter: RDP-simplify, split at sharp bends, leave
            # curves as junction territory.  Each returned piece is a
            # straight rect axis (or the whole lane if straight enough).
            for piece, _r in split_merged_centerline(ln, ref, rwy_centerlines):
                if piece.is_empty or piece.length < 1.0:
                    continue
                # Drop runway-parallel apron-edge artifacts: a synthetic lane
                # within _RUNWAY_PARALLEL_MAX_DEG of a runway's bearing AND
                # within _RUNWAY_NEAR_M of the runway is the medial path along
                # the apron/runway edge, not a real lane — leave that pavement
                # as a single junction/apron (user 2026-05-28).
                if rwy_near is not None and rwy_bearings:
                    try:
                        near = piece.distance(rwy_near) < _RUNWAY_NEAR_M
                    except _GEOM_EXC:
                        near = False
                    if near:
                        pb = _line_bearing_deg(piece)
                        if any(_bearings_aligned(pb, rb, _RUNWAY_PARALLEL_MAX_DEG)
                               for rb in rwy_bearings):
                            continue
                if covered is not None:
                    try:
                        if (piece.intersection(covered).length
                                / piece.length) > 0.6:
                            continue
                    except _GEOM_EXC:
                        pass
                # Drop a discovered lane that runs THROUGH a building (user
                # 2026-06-26): the medial skeleton can thread a strip of
                # building-shadowed pavement (CYXY TX16) — not a real route.
                if building_union is not None:
                    try:
                        if piece.intersection(building_union).length > 1.0:
                            continue
                    except _GEOM_EXC:
                        pass
                out.append(TaxiCenterline(
                    line=piece, name=ref,
                    seg_sizes=[""] * max(0, len(piece.coords) - 1)))
    return out
