"""Wingtip / RESA terrain-clearance grading.

Aircraft wingspans exceed the paved width of taxiways and runways, so
the design standards (FAA AC 150/5300-13 Taxiway Object Free Area;
ICAO Annex 14 graded runway strip + Runway End Safety Area) reserve a
clear, gently-graded band on each side of a surface and a graded area
off each runway end.  A hill that rises into that band — into the
wingtip envelope alongside, or into the approach off the end — must be
graded DOWN so it transitions smoothly to the pavement edge.

This module samples the DEM along each surface edge/centerline and
emits grading polygons.  The LATERAL strips are FLAT shadows of the
surface they protect: at each station the strip sits at the local
pavement-edge altitude and extends out level, so it follows the
surface's longitudinal profile like an extension of the pavement.
Terrain is cut down to that surface level ONLY where the DEM rises
above it within the protected (code-letter wingtip) width; terrain at
or below the surface is left alone (the wingtip clears it).  Because
the cut floor IS the surface level, a lateral strip can never push
terrain below the pavement it protects — so a pavement that sits below
its surroundings (cut into a hillside, or sunk by the elevation solver)
no longer carves a canyon.

The runway-end RESA is the exception: it RAMPS from the runway-end
elevation at a gentle slope and daylights where it meets the DEM, so an
over-run/undershoot meets a slope rather than a wall.

Three passes share one strip builder:
  * taxiway lateral strips   (ROLE_TAXIWAY_CLEARANCE) — flat shadow
  * runway lateral strips    (ROLE_RUNWAY_CLEARANCE)  — flat shadow
  * runway-end RESA areas     (ROLE_RUNWAY_CLEARANCE)  — ramp

Public API:
    emit_surface_clearance_cuts(layout, dem, tile_lat, tile_lon)
"""
from __future__ import annotations

import math
from collections import defaultdict

import numpy as np
import shapely
import O4_UI_Utils as UI
from shapely import STRtree
from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

# Narrow exception tuple — shapely degeneracy / DEM I/O.  Programming
# errors propagate (see boundary.py for the rationale).
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

from .config import (
    CLEARANCE_MAX_REACH_M,
    CLEARANCE_OBSTRUCTION_THRESHOLD_M,
    CLEARANCE_STATION_STEP_M,
    CLEARANCE_LATERAL_MAX_SLOPE,
    RUNWAY_END_RESA_MAX_SLOPE,
    runway_strip_half_width_m,
    taxiway_clearance_half_width_for_letter,
    taxiway_clearance_half_width_m,
)
from .layout import (
    BuiltShape,
    PavementLayout,
    R_EARTH,
    ROLE_APRON,
    ROLE_CROSS_CONNECTOR,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_RUNWAY_CLEARANCE,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL,
    ROLE_STUB,
    ROLE_TAXIWAY_CLEARANCE,
    SHARED_VERTEX_TOL_M,
    vertex_bucket,
)
from .elevation import _sample_dem
from .pavement.junctions import _decompose_polygon_with_holes
from .pavement.runways import _sample_runway_segment_elev

__all__ = ["emit_surface_clearance_cuts"]


_TAXIWAY_ROLES = (
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR,
)
# Minimum emitted cut area; smaller residue is dropped as noise.
_MIN_CUT_AREA_M2 = 25.0
# Keep every emitted cut vertex this far OUTSIDE pavement so it never
# lands on a sloping rect's edge (``test_no_vertex_on_sloping_rect_-
# edge`` flags non-rect vertices within 1 m of a rect edge interior).
_PAVEMENT_GAP_M = 1.5
# Only build lateral strips for shapes that are genuinely elongated
# (a taxiway / runway).  Chunky absorbed pieces (aspect < this) are
# blob-like — "edge clearance" is ill-defined and they'd otherwise
# infer a huge code letter from their large short edge.
_MIN_LATERAL_ASPECT = 2.0
# Cap the pavement width used to infer the code letter, so a
# mis-shaped wide piece can't push the band beyond code F.
_MAX_TAXIWAY_WIDTH_M = 45.0
# Decimation tolerances: drop a ring vertex when it is within this
# perpendicular distance of the chord through its neighbours AND its
# altitude is within this much of the linear interpolation along that
# chord.  Collapses the redundant nodes along straight, planar runs
# (≈ all of them) while keeping nodes where the daylight contour bends
# or the cut surface curves with the terrain.
_DECIMATE_GEOM_TOL_M = 0.3
_DECIMATE_ALT_TOL_M = 0.15
# Airside pavement a taxi centerline can run over — used to find the
# pavement edge (raycast) and the edge altitude, regardless of whether
# that pavement was emitted as a rect, junction, or apron.
_AIRSIDE_PAVEMENT_ROLES = (
    ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR,
    ROLE_JUNCTION, ROLE_APRON,
)
# Raycasting a centerline outward to its pavement edge: step size and
# the max half-width we'll search.  Beyond this the centerline is in
# the interior of a large apron (no nearby edge) and that station-side
# is skipped — no wingtip-obstruction risk in the middle of pavement.
_RAY_STEP_M = 2.0
_RAY_MAX_HALF_WIDTH_M = 35.0
# How far past the runway end to search for the outer pavement edge
# (blast-pad / stopway / apron) the RESA should anchor on.
_RESA_PAVEMENT_PROBE_MAX_M = 300.0


# ──────────────────────────────────────────────────────────────────
# Small geometry helpers
# ──────────────────────────────────────────────────────────────────
def _open_coords(poly: Polygon) -> list[tuple[float, float]]:
    """Exterior ring as an OPEN coord list (closing repeat dropped)."""
    try:
        coords = list(poly.exterior.coords)
    except _GEOM_EXC:
        return []
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    return [(float(x), float(y)) for x, y in coords]


def _unit(dx: float, dy: float) -> tuple[float, float] | None:
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return None
    return (dx / d, dy / d)


def _outward_normal(poly: Polygon, a: tuple[float, float],
                    b: tuple[float, float]) -> tuple[float, float] | None:
    """Unit normal of edge ``a→b`` pointing AWAY from the polygon
    interior (so marching along it leaves the surface)."""
    u = _unit(b[0] - a[0], b[1] - a[1])
    if u is None:
        return None
    nx, ny = -u[1], u[0]
    mx, my = 0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1])
    try:
        c = poly.centroid
    except _GEOM_EXC:
        return (nx, ny)
    # Flip so the normal points away from the centroid.
    if (mx - c.x) * nx + (my - c.y) * ny < 0.0:
        nx, ny = -nx, -ny
    return (nx, ny)


def _stations(a: tuple[float, float], b: tuple[float, float],
              step: float) -> list[tuple[float, float]]:
    """Sample ``a→b`` (inclusive of both ends) at ≤ ``step`` spacing."""
    d = math.hypot(b[0] - a[0], b[1] - a[1])
    n = max(1, int(math.ceil(d / step)))
    return [(a[0] + (b[0] - a[0]) * k / n,
             a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]


def _decimate(coords: list[tuple[float, float]], alts: list[float]):
    """Collapse ring vertices that are redundant in BOTH geometry
    (collinear with their neighbours) AND altitude (on the linear
    interpolation between them).  Returns ``(coords, alts)`` open-form.

    Removes at most every other vertex per pass (so a gently-curving
    arc isn't collapsed to its chord in one sweep) and repeats until
    stable, keeping detail only where the daylight contour bends or the
    cut surface follows curving terrain.
    """
    coords = [(float(x), float(y)) for x, y in coords]
    alts = [float(a) for a in alts]
    n = min(len(coords), len(alts))
    coords, alts = coords[:n], alts[:n]
    changed = True
    while changed and len(coords) > 3:
        changed = False
        n = len(coords)
        keep = [True] * n
        i = 0
        while i < n:
            p0 = coords[(i - 1) % n]
            p1 = coords[i]
            p2 = coords[(i + 1) % n]
            dx, dy = p2[0] - p0[0], p2[1] - p0[1]
            seg2 = dx * dx + dy * dy
            if seg2 > 1e-9:
                t = ((p1[0] - p0[0]) * dx + (p1[1] - p0[1]) * dy) / seg2
                perp = math.hypot(p1[0] - (p0[0] + t * dx),
                                  p1[1] - (p0[1] + t * dy))
                a_lin = alts[(i - 1) % n] + t * (
                    alts[(i + 1) % n] - alts[(i - 1) % n])
                if (perp < _DECIMATE_GEOM_TOL_M
                        and abs(alts[i] - a_lin) < _DECIMATE_ALT_TOL_M):
                    keep[i] = False
                    changed = True
                    i += 2     # skip neighbour: no two adjacent removals
                    continue
            i += 1
        if changed:
            coords = [c for c, k in zip(coords, keep) if k]
            alts = [a for a, k in zip(alts, keep) if k]
    return coords, alts


def _largest_poly(geom):
    """Largest Polygon member of ``geom`` (Polygon / MultiPolygon /
    GeometryCollection), or None."""
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Polygon":
        return geom
    polys = [g for g in getattr(geom, "geoms", [])
             if g.geom_type == "Polygon" and not g.is_empty]
    if not polys:
        return None
    return max(polys, key=lambda g: g.area)


def _drop_sharp_corners(coords: list[tuple[float, float]],
                        min_deg: float = 3.0) -> list[tuple[float, float]]:
    """Remove ring vertices whose interior angle is below ``min_deg``.

    Decimation / daylight-contour clipping can leave needle-tip corners
    that to_osm would reject (sub-2° → X-Plane mesh-builder crash),
    dropping the whole cut.  Trim the sharpest offending vertex and
    repeat so the shape survives emission."""
    coords = [(float(x), float(y)) for x, y in coords]
    while len(coords) > 3:
        n = len(coords)
        worst_i, worst_ang = -1, min_deg
        for i in range(n):
            a, b, c = coords[(i - 1) % n], coords[i], coords[(i + 1) % n]
            v1 = (a[0] - b[0], a[1] - b[1])
            v2 = (c[0] - b[0], c[1] - b[1])
            n1, n2 = math.hypot(*v1), math.hypot(*v2)
            if n1 < 1e-6 or n2 < 1e-6:
                worst_i = i
                break
            cosang = max(-1.0, min(1.0,
                                   (v1[0] * v2[0] + v1[1] * v2[1]) / (n1 * n2)))
            ang = math.degrees(math.acos(cosang))
            if ang < worst_ang:
                worst_ang, worst_i = ang, i
        if worst_i < 0:
            break
        del coords[worst_i]
    return coords


def _resample_alts_over_strips(ring_open, strips):
    """Per-vertex altitude for ``ring_open`` sampled from the set of raw
    graded ``strips`` (each ``(open_ring, open_alts)``).

    For each vertex: the altitude interpolated along the NEAREST strip
    edge within ``EDGE_TOL_M`` (where strips overlap, the nearest edge
    wins — i.e. the closest pavement-edge profile governs), else the
    altitude of the nearest strip vertex.  Lets a single polygon unioned
    from many strips carry a faithful per-vertex elevation.

    Vectorised: an STRtree ``dwithin`` query cuts each vertex's candidate
    edges to the handful within ``EDGE_TOL_M`` (instead of scanning every
    strip edge — the previous O(V·E) loop was the dominant build cost on
    apron-heavy airports), then the EXACT same perpendicular-foot
    projection + nearest-edge tie-break is applied over those candidates.
    Points with no in-range edge fall back to the nearest strip vertex."""
    EDGE_TOL_M = 0.5
    EDGE_TOL2 = EDGE_TOL_M * EDGE_TOL_M
    if not ring_open:
        return []

    # Flatten strip segments (edge interpolation) and strip vertices
    # (nearest-vertex fallback) into parallel arrays.
    seg_geoms: list = []
    sxl, syl, dxl, dyl, seg2l, a0l, a1l = [], [], [], [], [], [], []
    vxl, vyl, vatl = [], [], []
    for ring, alts in strips:
        m = min(len(ring), len(alts))
        for k in range(m):
            sx, sy = ring[k]
            vxl.append(sx)
            vyl.append(sy)
            vatl.append(alts[k])
            tx, ty = ring[(k + 1) % m]
            dx, dy = tx - sx, ty - sy
            seg2 = dx * dx + dy * dy
            if seg2 < 1e-9:
                continue
            seg_geoms.append(LineString([(sx, sy), (tx, ty)]))
            sxl.append(sx)
            syl.append(sy)
            dxl.append(dx)
            dyl.append(dy)
            seg2l.append(seg2)
            a0l.append(alts[k])
            a1l.append(alts[(k + 1) % m])

    n = len(ring_open)
    rx = np.fromiter((p[0] for p in ring_open), dtype=float, count=n)
    ry = np.fromiter((p[1] for p in ring_open), dtype=float, count=n)
    best_alt = np.full(n, np.nan)

    if seg_geoms:
        sx = np.asarray(sxl)
        sy = np.asarray(syl)
        dx = np.asarray(dxl)
        dy = np.asarray(dyl)
        seg2 = np.asarray(seg2l)
        a0 = np.asarray(a0l)
        a1 = np.asarray(a1l)
        qpts = shapely.points(rx, ry)
        tree = STRtree(seg_geoms)
        # pairs[0] = ring-vertex index, pairs[1] = candidate segment index.
        pairs = tree.query(qpts, predicate="dwithin", distance=EDGE_TOL_M)
        if pairs.size:
            pi = pairs[0]
            si = pairs[1]
            nx = rx[pi]
            ny = ry[pi]
            t = (((nx - sx[si]) * dx[si] + (ny - sy[si]) * dy[si])
                 / seg2[si])
            in_range = (t >= -1e-3) & (t <= 1.0 + 1e-3)
            tc = np.clip(t, 0.0, 1.0)
            px = sx[si] + tc * dx[si]
            py = sy[si] + tc * dy[si]
            d2 = (nx - px) ** 2 + (ny - py) ** 2
            ok = in_range & (d2 < EDGE_TOL2)
            if ok.any():
                pio = pi[ok]
                d2o = d2[ok]
                sio = si[ok]
                alto = a0[si][ok] + tc[ok] * (a1[si][ok] - a0[si][ok])
                # Per vertex keep the nearest edge; break exact ties by
                # lowest segment index (= the original's first-in-order
                # ``if d2 < best_d2``).  lexsort orders by the LAST key
                # first → primary vertex, then distance, then seg index.
                order = np.lexsort((sio, d2o, pio))
                pis = pio[order]
                first = np.empty(pis.shape, dtype=bool)
                first[0] = True
                first[1:] = pis[1:] != pis[:-1]
                best_alt[pis[first]] = alto[order][first]

    # Fallback: nearest strip vertex for any ring vertex with no in-range
    # edge match (matches the original unbounded nearest-vertex search,
    # including its first-in-order tie-break — keep all tied nearest, then
    # pick the lowest vertex index).
    missing = np.isnan(best_alt)
    if missing.any() and vxl:
        vat = np.asarray(vatl)
        vtree = STRtree(shapely.points(np.asarray(vxl), np.asarray(vyl)))
        mi = np.flatnonzero(missing)
        nn = vtree.query_nearest(shapely.points(rx[mi], ry[mi]),
                                 all_matches=True)
        order = np.lexsort((nn[1], nn[0]))   # by input, then vertex index
        inps = nn[0][order]
        firstm = np.empty(inps.shape, dtype=bool)
        firstm[0] = True
        firstm[1:] = inps[1:] != inps[:-1]
        best_alt[mi[inps[firstm]]] = vat[nn[1][order][firstm]]

    return [round(float(a), 1) if not np.isnan(a) else 0.0
            for a in best_alt]


def _rect_long_short_edges(coords: list[tuple[float, float]]):
    """For a 4-corner ring, return ``(long_edges, short_len)`` where
    ``long_edges`` is the two longest edges as ``((a, b), ...)`` corner
    pairs and ``short_len`` is the mean of the two shortest edges."""
    if len(coords) != 4:
        return None
    edges = []
    for i in range(4):
        a = coords[i]
        b = coords[(i + 1) % 4]
        edges.append((math.hypot(b[0] - a[0], b[1] - a[1]), a, b))
    edges.sort(key=lambda e: e[0])
    short_len = 0.5 * (edges[0][0] + edges[1][0])
    long_edges = [(edges[2][1], edges[2][2]), (edges[3][1], edges[3][2])]
    long_len = 0.5 * (edges[2][0] + edges[3][0])
    return long_edges, short_len, long_len


# ──────────────────────────────────────────────────────────────────
# Core: build cut strips off one edge
# ──────────────────────────────────────────────────────────────────
def _build_graded_strips(edge_stations, edge_alts, outwards,
                         band_caps, slope, trigger, step, sample_dem):
    """Build clearance cut-strip rings off an edge / pavement-edge
    polyline.  At each station the ceiling rises from the pavement edge
    altitude at ``slope`` (rise/run):

        ceiling(d) = edge_alt + slope · d

    Two regimes share this code:
      * ``slope == 0`` (LATERAL strips) — the ceiling is FLAT at the
        pavement-edge altitude, so the strip is a level extension of the
        surface following its longitudinal profile (each station carries
        its own ``edge_alt``).  Terrain is cut to surface level only
        where it rises above it; the strip daylights where the DEM drops
        back to the surface.  Never produces a sub-surface floor, so it
        can't carve a canyon beside a pavement that sits below grade.
      * ``slope > 0`` (RESA end-caps) — the ceiling is a gentle ramp, so
        an over-run meets a slope rather than a wall.

    Terrain above the ceiling is cut down to it, and the cut DAYLIGHTS
    where the ceiling meets the DEM — so the graded patch is only as wide
    as it needs to be, capped at ``band_caps[i]`` (the code-letter
    wingtip width minus the pavement half-width).  Cut-only: a station
    contributes a strip ONLY where the terrain rises more than
    ``trigger`` m above the ceiling; flat or falling terrain is left
    untouched.

    ``edge_stations`` / ``edge_alts`` / ``outwards`` / ``band_caps`` are
    matched per-station lists (so the edge may curve, e.g. a centerline).
    Returns ``(ring_open, alts_open)`` pairs.
    """
    n = len(edge_stations)
    outer: list[float] = [0.0] * n
    obstructed: list[bool] = [False] * n
    for i, (sx, sy) in enumerate(edge_stations):
        ref = edge_alts[i]
        if ref is None:
            continue
        nx, ny = outwards[i]
        cap = band_caps[i]
        if cap <= _PAVEMENT_GAP_M:
            continue
        nst = max(1, int(math.ceil(cap / step)))
        last = 0.0
        for k in range(1, nst + 1):
            d = min(cap, k * step)
            ceil = ref + slope * d
            dd = sample_dem(sx + nx * d, sy + ny * d)
            if dd is not None and dd > ceil + trigger:
                last = d
        if last > 0.0:
            obstructed[i] = True
            outer[i] = min(cap, last + step)
    # Group consecutive obstructed stations into runs (1-station slack).
    idx = [i for i in range(n) if obstructed[i]]
    if not idx:
        return []
    runs: list[list[int]] = []
    cur = [idx[0]]
    for j in idx[1:]:
        if j - cur[-1] <= 2:
            cur.append(j)
        else:
            runs.append(cur)
            cur = [j]
    runs.append(cur)

    out: list[tuple[list, list]] = []
    for run in runs:
        i0, i1 = run[0], run[-1]
        # Widen the run by one station each side so the cut tapers
        # longitudinally to its neighbours instead of ending in a wall.
        lo = max(0, i0 - 1)
        hi = min(n - 1, i1 + 1)
        inner_pts, inner_alts = [], []
        outer_pts, outer_alts = [], []
        for i in range(lo, hi + 1):
            ref = edge_alts[i]
            if ref is None:
                continue
            nx, ny = outwards[i]
            off = outer[i] if outer[i] > 0.0 else step
            sx, sy = edge_stations[i]
            # Inner edge: a small gap outside the pavement, at the
            # pavement edge altitude (clean shoulder tie-in).
            ix, iy = sx + nx * _PAVEMENT_GAP_M, sy + ny * _PAVEMENT_GAP_M
            inner_alts.append(round(float(ref + slope * _PAVEMENT_GAP_M), 1))
            inner_pts.append((ix, iy))
            # Outer edge: at the daylight point, on the gentle ceiling
            # (= DEM there), so the ramp meets natural ground, no cliff.
            ox, oy = sx + nx * off, sy + ny * off
            outer_pts.append((ox, oy))
            outer_alts.append(round(float(ref + slope * off), 1))
        if len(inner_pts) < 2:
            continue
        ring = inner_pts + outer_pts[::-1]
        alts = inner_alts + outer_alts[::-1]
        out.append((ring, alts))
    return out


# ──────────────────────────────────────────────────────────────────
# Runway-end (RESA) edge detection
# ──────────────────────────────────────────────────────────────────
def _runway_end_edges(runway_shapes):
    """Return the two true extremities of each runway designation as
    ``(shape, end_a, end_b, full_len)``.

    A runway is usually split into many segments (crossings, FAA
    profile redistribution, tile cuts), so an internal-seam test is
    fragile — when segments don't abut cleanly every seam looks like an
    "end".  Instead we collect every segment's two short edges per ref
    and pick the PAIR of short-edge midpoints that are FARTHEST apart:
    those are the runway's two thresholds; everything between them is
    interior.  ``full_len`` is that farthest-pair distance (the whole
    runway length), used for the ICAO code number.
    """
    by_ref: dict[str, list] = defaultdict(list)
    for s in runway_shapes:
        coords = _open_coords(s.polygon)
        info = _rect_long_short_edges(coords)
        if info is None:
            continue
        long_edges, _short_len, _long_len = info
        long_set = set()
        for (a, b) in long_edges:
            long_set.add((a, b))
            long_set.add((b, a))
        for i in range(4):
            a = coords[i]
            b = coords[(i + 1) % 4]
            if (a, b) in long_set:
                continue
            mid = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
            by_ref[s.ref].append((s, a, b, mid))

    ends = []
    for ref, ses in by_ref.items():
        if len(ses) <= 2:
            # Single segment: both short edges are thresholds.
            full = (math.hypot(ses[0][3][0] - ses[1][3][0],
                               ses[0][3][1] - ses[1][3][1])
                    if len(ses) == 2 else 0.0)
            for s, a, b, _mid in ses:
                ends.append((s, a, b, full))
            continue
        # Farthest-apart short-edge midpoints = the two thresholds.
        best = (-1.0, 0, 1)
        for i in range(len(ses)):
            for j in range(i + 1, len(ses)):
                d = math.hypot(ses[i][3][0] - ses[j][3][0],
                               ses[i][3][1] - ses[j][3][1])
                if d > best[0]:
                    best = (d, i, j)
        full = best[0]
        for k in (best[1], best[2]):
            s, a, b, _mid = ses[k]
            ends.append((s, a, b, full))
    return ends


def _pavement_exit_along(prep_pav, mx, my, dx, dy, max_d, step) -> float:
    """Distance from ``(mx, my)`` (a point ON pavement) along unit
    ``(dx, dy)`` to where the ray leaves the pavement union — i.e. the
    OUTER pavement edge (a blast-pad / stopway / apron end).  ``0.0`` if
    the start isn't on pavement; ``max_d`` if it never exits."""
    if prep_pav is None or not prep_pav.contains(Point(mx, my)):
        return 0.0
    d = step
    while d <= max_d:
        if not prep_pav.contains(Point(mx + dx * d, my + dy * d)):
            return d - 0.5 * step
        d += step
    return max_d


# ──────────────────────────────────────────────────────────────────
# Taxi-centerline edge tracing (covers junction/apron taxiways)
# ──────────────────────────────────────────────────────────────────
def _ray_edge(prep_pav, sx, sy, dx, dy) -> float | None:
    """Distance from ``(sx, sy)`` along unit ``(dx, dy)`` to the
    pavement edge (where the ray leaves the prepared pavement union).
    ``None`` if it never exits within ``_RAY_MAX_HALF_WIDTH_M`` (the
    centerline is in the interior of a large apron — no nearby edge)."""
    last = 0.0
    d = _RAY_STEP_M
    while d <= _RAY_MAX_HALF_WIDTH_M:
        if prep_pav.contains(Point(sx + dx * d, sy + dy * d)):
            last = d
            d += _RAY_STEP_M
        else:
            return last + 0.5 * _RAY_STEP_M  # edge ~ midway to exit
    return None


def _edge_interp_alt(shape, x, y) -> float | None:
    """Pavement-surface altitude near ``(x, y)``, interpolated the way
    Triangle4XP renders it: linearly along the boundary EDGE between the two
    endpoint node altitudes.

    For a ``node_altitudes`` shape (apron / junction / tile-cut piece),
    ``_sample_runway_segment_elev`` returns the NEAREST-NODE value — a
    piecewise-constant Voronoi field that STEPS at cell boundaries.  When the
    clearance shadow samples a pavement edge that way, a point near the edge
    can pick up a distant node's altitude (a 2 m phantom step the apron never
    renders).  Projecting to the nearest boundary edge and interpolating its
    endpoint altitudes mirrors the rendered surface, so the shadow stays
    smooth.  Flat / ``altitude_high``-``low`` shapes fall through to the
    existing sampler (no node_altitudes to interpolate)."""
    na = shape.node_altitudes
    if not na or shape.polygon is None:
        return _sample_runway_segment_elev(shape, x, y)
    try:
        coords = list(shape.polygon.exterior.coords)
    except _GEOM_EXC:
        return _sample_runway_segment_elev(shape, x, y)
    n = min(len(coords), len(na))
    if n < 2:
        return _sample_runway_segment_elev(shape, x, y)
    best_d2 = float("inf")
    best: float | None = None
    for i in range(n - 1):
        sx, sy = coords[i]
        tx, ty = coords[i + 1]
        dx, dy = tx - sx, ty - sy
        seg2 = dx * dx + dy * dy
        if seg2 < 1e-9:
            continue
        t = max(0.0, min(1.0, ((x - sx) * dx + (y - sy) * dy) / seg2))
        px, py = sx + t * dx, sy + t * dy
        d2 = (x - px) ** 2 + (y - py) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best = na[i] + t * (na[i + 1] - na[i])
    return best if best is not None else _sample_runway_segment_elev(shape, x, y)


def _pav_alt(pav_shapes, x, y) -> float | None:
    """Altitude of the airside pavement at ``(x, y)`` — the shape
    containing the point, edge-interpolated (see :func:`_edge_interp_alt`)
    so a ``node_altitudes`` apron/junction is shadowed at its RENDERED
    altitude, not a stepped nearest-node sample."""
    pt = Point(x, y)
    for s in pav_shapes:
        try:
            if s.polygon.contains(pt):
                e = _edge_interp_alt(s, x, y)
                if e is not None:
                    return e
        except _GEOM_EXC:
            continue
    return None


def _centerline_edge_runs(line, prep_pav, pav_shapes, step, letter=None):
    """Walk a taxi centerline and, for each side, yield maximal
    contiguous runs of pavement-edge stations as
    ``(edge_pts, edge_alts, outwards, band_ws)`` ready for
    :func:`_build_graded_strips`.

    At each densified centerline point we raycast perpendicular to the
    local tangent to find the pavement EDGE on that side (where the cut
    begins and the edge altitude is sampled).  The clearance half-width
    comes from the apt.dat ICAO size ``letter`` when known (authoritative
    width class); otherwise it is inferred from the measured pavement
    width (both half-widths) as a fallback.  Stations whose centerline
    point is off pavement, or in the interior of a large apron, break the
    run.
    """
    clear_half_fixed = (taxiway_clearance_half_width_for_letter(letter)
                        if letter else None)
    try:
        coords = list(line.coords)
    except _GEOM_EXC:
        return []
    if len(coords) < 2:
        return []
    # Densify the centerline.
    pts: list[tuple[float, float]] = []
    for i in range(len(coords) - 1):
        ax, ay = coords[i]
        bx, by = coords[i + 1]
        pts.append((ax, ay))
        d = math.hypot(bx - ax, by - ay)
        if d > step:
            k = int(math.ceil(d / step))
            for j in range(1, k):
                t = j / k
                pts.append((ax + t * (bx - ax), ay + t * (by - ay)))
    pts.append(coords[-1])
    n = len(pts)
    runs = []  # (edge_pts, edge_alts, outwards, band_ws)
    for side in (1.0, -1.0):
        cur_pts, cur_alts, cur_out, cur_bw = [], [], [], []

        def _flush():
            if len(cur_pts) >= 2:
                runs.append((list(cur_pts), list(cur_alts),
                             list(cur_out), list(cur_bw)))
            cur_pts.clear()
            cur_alts.clear()
            cur_out.clear()
            cur_bw.clear()

        for i in range(n):
            sx, sy = pts[i]
            ax, ay = pts[max(0, i - 1)]
            bx, by = pts[min(n - 1, i + 1)]
            tan = _unit(bx - ax, by - ay)
            if tan is None or not prep_pav.contains(Point(sx, sy)):
                _flush()
                continue
            perp = (-tan[1] * side, tan[0] * side)
            half = _ray_edge(prep_pav, sx, sy, perp[0], perp[1])
            if half is None:
                _flush()
                continue
            if clear_half_fixed is not None:
                clear_half = clear_half_fixed
            else:
                # Fallback (OSM / no size class): infer from measured
                # full width (this side + opposite side).
                half_o = _ray_edge(prep_pav, sx, sy, -perp[0], -perp[1])
                width = half + (half_o if half_o is not None else half)
                clear_half = taxiway_clearance_half_width_m(
                    min(width, _MAX_TAXIWAY_WIDTH_M))
            band = clear_half - half
            if band <= _PAVEMENT_GAP_M + 1.0:
                _flush()
                continue
            ex, ey = sx + perp[0] * half, sy + perp[1] * half
            # Sample the edge altitude just INSIDE the pavement.
            inq = max(0.0, half - 1.0)
            ref = _pav_alt(pav_shapes, sx + perp[0] * inq, sy + perp[1] * inq)
            if ref is None:
                ref = _pav_alt(pav_shapes, sx, sy)
            if ref is None:
                _flush()
                continue
            cur_pts.append((ex, ey))
            cur_alts.append(ref)
            cur_out.append(perp)
            cur_bw.append(band)
        _flush()
    return runs


# ──────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────
def emit_surface_clearance_cuts(layout: PavementLayout, dem,
                                tile_lat: int, tile_lon: int) -> int:
    """Emit wingtip/RESA terrain-clearance cut polygons.  Mutates
    ``layout.shapes``.  Returns the number of cut shapes emitted."""
    if dem is None:
        return 0
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH
    step = CLEARANCE_STATION_STEP_M

    def sample_dem(x: float, y: float) -> float | None:
        try:
            lat = lat0 + math.degrees(y / R)
            lon = lon0 + math.degrees(x / (R * cos0))
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None

    # Surfaces we build clearance off of (4-corner sloping/flat rects).
    def _usable(s) -> bool:
        if s.polygon is None or s.polygon.is_empty:
            return False
        if len(_open_coords(s.polygon)) != 4:
            return False
        return (s.altitude is not None
                or (s.altitude_high is not None
                    and s.altitude_low is not None))

    runway_shapes = [s for s in layout.shapes
                     if s.role == ROLE_RUNWAY and _usable(s)]
    taxi_shapes = [s for s in layout.shapes
                   if s.role in _TAXIWAY_ROLES and _usable(s)]

    # FULL runway length per designation (the ICAO code number comes
    # from the whole runway, not a single segment — runways are split
    # into segments at crossings/seams).  Approximated as the longest
    # distance between any two corners of all segments sharing a ref.
    _ref_pts: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for s in runway_shapes:
        _ref_pts[s.ref].extend(_open_coords(s.polygon))
    runway_len_by_ref: dict[str, float] = {}
    for ref, pts in _ref_pts.items():
        mx = 0.0
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
                if d > mx:
                    mx = d
        runway_len_by_ref[ref] = mx

    def _runway_full_len(s, fallback) -> float:
        return runway_len_by_ref.get(s.ref, fallback) or fallback

    # Existing geometry the cut must not overlap.  Buffer it so emitted
    # vertices stay clear of any pavement edge (sloping-rect-edge test).
    static_polys = [s.polygon for s in layout.shapes
                    if s.polygon is not None and not s.polygon.is_empty]
    static_block = None
    if static_polys:
        try:
            static_block = unary_union(static_polys).buffer(_PAVEMENT_GAP_M)
        except _GEOM_EXC:
            static_block = None

    # Collect every raw graded strip across all three passes, then
    # resolve them ONCE into minimal geometry.  Building per-strip and
    # clipping each new strip against the previously-emitted cuts (the
    # old approach) carved overlapping runway/taxiway bands into slivers
    # at junctions; unioning the raw strips up front and emitting one
    # shape per connected region yields a single clean cut wherever the
    # area is contiguous.
    raw_strips: list[tuple[Polygon, list, list, str]] = []

    def _collect(ring, alts, role) -> None:
        """Validate a raw strip ring and stash it for the finalize pass."""
        try:
            raw = Polygon(ring)
            if not raw.is_valid:
                raw = raw.buffer(0)
        except _GEOM_EXC:
            return
        if (raw.is_empty or raw.geom_type != "Polygon"
                or raw.area < _MIN_CUT_AREA_M2):
            return
        raw_strips.append((raw, list(ring), list(alts), role))

    def _finalize() -> int:
        """Union all collected strips, subtract pavement once, and emit
        one ``node_altitudes`` shape per connected region — decomposed
        into simple polygons only where a real pavement hole forces a
        split.  Per-vertex altitudes are sampled from the nearest source
        strip edge, so overlapping bands resolve to a single surface
        instead of abutting slivers."""
        if not raw_strips:
            return 0
        strips = [(ring, alts) for _p, ring, alts, _r in raw_strips]
        try:
            region = unary_union([p for p, _r, _a, _ro in raw_strips])
            if static_block is not None and not static_block.is_empty:
                region = region.difference(static_block)
        except _GEOM_EXC:
            return 0
        if region.is_empty:
            return 0
        try:
            runway_block = unary_union(
                [p for p, _r, _a, role in raw_strips
                 if role == ROLE_RUNWAY_CLEARANCE])
        except _GEOM_EXC:
            runway_block = None

        if region.geom_type == "Polygon":
            components = [region]
        elif region.geom_type in ("MultiPolygon", "GeometryCollection"):
            components = [g for g in region.geoms if g.geom_type == "Polygon"]
        else:
            components = []

        # Cross-piece 1:1 seams: where a pavement hole splits a region
        # into sibling pieces, a coincident vertex adopts the altitude
        # the first sibling already wrote (no consensus step).
        adopt: dict[tuple[int, int], tuple[float, float, float]] = {}

        def _adopt_alt(x: float, y: float) -> float | None:
            bx, by = vertex_bucket(x, y)
            best: float | None = None
            best_d2 = SHARED_VERTEX_TOL_M * SHARED_VERTEX_TOL_M
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    rec = adopt.get((bx + dx, by + dy))
                    if rec is None:
                        continue
                    ex, ey, ea = rec
                    d2 = (ex - x) ** 2 + (ey - y) ** 2
                    if d2 <= best_d2:
                        best_d2 = d2
                        best = ea
            return best

        n = 0
        for comp in components:
            for simple in _decompose_polygon_with_holes(
                    comp, min_area_m2=_MIN_CUT_AREA_M2):
                if simple.is_empty or simple.area < _MIN_CUT_AREA_M2:
                    continue
                # Morphological open removes hairline slivers / spikes
                # that would trip to_osm's sub-2° corner guard.
                try:
                    opened = simple.buffer(-0.1).buffer(0.1)
                except _GEOM_EXC:
                    opened = simple
                if (opened is not None and not opened.is_empty
                        and opened.geom_type == "Polygon"
                        and opened.area >= _MIN_CUT_AREA_M2):
                    simple = opened
                ring = _open_coords(simple)
                if len(ring) < 3:
                    continue
                # Collapse redundant collinear+planar nodes, trim sharp
                # corners, then sample the final ring's altitudes.
                alts0 = _resample_alts_over_strips(ring, strips)
                dec_xy, _dec_a = _decimate(ring, alts0)
                dec_xy = _drop_sharp_corners(dec_xy)
                if len(dec_xy) < 3:
                    continue
                try:
                    poly = Polygon(dec_xy)
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                    poly = _largest_poly(poly)
                except _GEOM_EXC:
                    continue
                if (poly is None or poly.geom_type != "Polygon"
                        or poly.is_empty or poly.area < _MIN_CUT_AREA_M2):
                    continue
                final_ring = _open_coords(poly)
                node_open = _resample_alts_over_strips(final_ring, strips)
                for vi, (vx, vy) in enumerate(final_ring):
                    a = _adopt_alt(vx, vy)
                    if a is not None:
                        node_open[vi] = a
                for (vx, vy), a in zip(final_ring, node_open):
                    adopt.setdefault(vertex_bucket(vx, vy), (vx, vy, a))
                node_alts = node_open + [node_open[0]]
                # Classify by the band that covers most of the piece.
                role = ROLE_TAXIWAY_CLEARANCE
                if runway_block is not None and not runway_block.is_empty:
                    try:
                        if (poly.intersection(runway_block).area
                                > 0.5 * poly.area):
                            role = ROLE_RUNWAY_CLEARANCE
                    except _GEOM_EXC:
                        pass
                layout.shapes.append(BuiltShape(
                    polygon=poly, role=role, ref="surface_clearance",
                    node_altitudes=node_alts))
                n += 1
        return n

    n_emitted = 0

    # Airside pavement union (shared by the taxiway-centerline trace and
    # the RESA pavement-edge anchor).
    airside = [s for s in layout.shapes
               if s.role in _AIRSIDE_PAVEMENT_ROLES
               and s.polygon is not None and not s.polygon.is_empty]
    prep_pav = None
    if airside:
        try:
            prep_pav = prep(unary_union([s.polygon for s in airside]))
        except _GEOM_EXC:
            prep_pav = None

    # ── Pass A: taxiway lateral strips, traced from the taxi CENTERLINE
    # network.  This follows the centerline and raycasts out to whatever
    # pavement edge actually borders it — so it covers taxiways no matter
    # whether they were emitted as rects, junctions, or aprons (the
    # rect-only approach missed the junction/apron portions).  Falls back
    # to taxiway rect long-edges when no centerline network is present.
    tx_threshold = CLEARANCE_OBSTRUCTION_THRESHOLD_M["taxiway"]
    tx_slope = CLEARANCE_LATERAL_MAX_SLOPE
    centerlines = getattr(layout, "apt_taxi_centerlines", None) or []
    # Authoritative ICAO size letter per taxiway name (apt.dat row 1202).
    letters = getattr(layout, "apt_taxi_letters", None) or {}
    if centerlines and prep_pav is not None:
        for entry in centerlines:
            line = entry[0] if isinstance(entry, tuple) else entry
            ref = entry[1] if (isinstance(entry, tuple)
                               and len(entry) > 1) else ""
            if not isinstance(line, LineString) or line.is_empty:
                continue
            letter = letters.get(ref)
            for e_pts, e_alts, e_out, e_bw in _centerline_edge_runs(
                    line, prep_pav, airside, step, letter=letter):
                for ring, ralts in _build_graded_strips(
                        e_pts, e_alts, e_out, e_bw, tx_slope,
                        tx_threshold, step, sample_dem):
                    _collect(ring, ralts, ROLE_TAXIWAY_CLEARANCE)
    else:
        # Fallback: taxiway rect long-edges (wingtip basis).
        for s in taxi_shapes:
            coords = _open_coords(s.polygon)
            info = _rect_long_short_edges(coords)
            if info is None:
                continue
            long_edges, short_len, long_len = info
            if short_len <= 0 or long_len / short_len < _MIN_LATERAL_ASPECT:
                continue
            clear_half = taxiway_clearance_half_width_m(
                min(short_len, _MAX_TAXIWAY_WIDTH_M))
            band_w = clear_half - 0.5 * short_len
            if band_w <= _PAVEMENT_GAP_M + 1.0:
                continue
            for (a, b) in long_edges:
                outward = _outward_normal(s.polygon, a, b)
                if outward is None:
                    continue
                pts = _stations(a, b, step)
                m = len(pts)
                alts = [_sample_runway_segment_elev(s, px, py)
                        for px, py in pts]
                for ring, ralts in _build_graded_strips(
                        pts, alts, [outward] * m, [band_w] * m,
                        tx_slope, tx_threshold, step, sample_dem):
                    _collect(ring, ralts, ROLE_TAXIWAY_CLEARANCE)

    # ── Pass B: runway lateral graded-strip cuts (rect long-edges) ──
    rw_threshold = CLEARANCE_OBSTRUCTION_THRESHOLD_M["runway"]
    rw_max_reach = CLEARANCE_MAX_REACH_M["runway"]
    rw_slope = CLEARANCE_LATERAL_MAX_SLOPE
    for s in runway_shapes:
        coords = _open_coords(s.polygon)
        info = _rect_long_short_edges(coords)
        if info is None:
            continue
        long_edges, short_len, long_len = info
        if short_len <= 0 or long_len / short_len < _MIN_LATERAL_ASPECT:
            continue
        clear_half = runway_strip_half_width_m(_runway_full_len(s, long_len))
        band_w = clear_half - 0.5 * short_len
        if band_w <= _PAVEMENT_GAP_M + 1.0:
            continue
        for (a, b) in long_edges:
            outward = _outward_normal(s.polygon, a, b)
            if outward is None:
                continue
            pts = _stations(a, b, step)
            m = len(pts)
            alts = [_sample_runway_segment_elev(s, px, py) for px, py in pts]
            for ring, ralts in _build_graded_strips(
                    pts, alts, [outward] * m, [band_w] * m,
                    rw_slope, rw_threshold, step, sample_dem):
                _collect(ring, ralts, ROLE_RUNWAY_CLEARANCE)

    # ── Pass C: runway-end safety area (RESA) ──
    # A graded rectangle off each runway end, symmetric about the
    # extended centreline and anchored at the OUTER pavement edge (the
    # blast-pad / stopway end, found by marching the centreline out
    # through the pavement union).  Width ≥ 2× runway width / graded-
    # strip width; the surface is a gentle ramp (≤ RESA_MAX_SLOPE) rising
    # from the pavement-end elevation, cutting terrain above it and
    # daylighting where it meets the DEM — so an undershoot/overrun meets
    # a smooth slope, not a wall.
    for s, a, b, full_len in _runway_end_edges(runway_shapes):
        outward = _outward_normal(s.polygon, a, b)
        if outward is None:
            continue
        nx, ny = outward
        mid = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
        info = _rect_long_short_edges(_open_coords(s.polygon))
        runway_width = info[1] if info else math.hypot(b[0] - a[0], b[1] - a[1])
        # Anchor at the outer pavement edge along the extended centreline.
        start = _pavement_exit_along(prep_pav, mid[0], mid[1], nx, ny,
                                     _RESA_PAVEMENT_PROBE_MAX_M, step)
        p0 = (mid[0] + nx * start, mid[1] + ny * start)
        # Pavement-end elevation (just inside the edge), else runway end.
        ref = _pav_alt(airside, mid[0] + nx * max(0.0, start - 1.0),
                       mid[1] + ny * max(0.0, start - 1.0))
        if ref is None:
            ref = _sample_runway_segment_elev(s, mid[0], mid[1])
        if ref is None:
            continue
        # RESA half-width: ≥ runway width and ≥ graded-strip half-width.
        half = max(runway_width, runway_strip_half_width_m(full_len))
        perp = (-ny, nx)
        ea = (p0[0] - perp[0] * half, p0[1] - perp[1] * half)
        eb = (p0[0] + perp[0] * half, p0[1] + perp[1] * half)
        stations = _stations(ea, eb, step)
        m = len(stations)
        for ring, ralts in _build_graded_strips(
                stations, [ref] * m, [outward] * m, [rw_max_reach] * m,
                RUNWAY_END_RESA_MAX_SLOPE, rw_threshold, step, sample_dem):
            _collect(ring, ralts, ROLE_RUNWAY_CLEARANCE)

    # Resolve all collected strips into minimal geometry in one pass.
    n_emitted = _finalize()
    return n_emitted
