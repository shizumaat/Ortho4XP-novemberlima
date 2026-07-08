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

Four passes share one strip builder:
  * taxiway lateral strips   (ROLE_TAXIWAY_CLEARANCE) — flat shadow,
    traced from the apt.dat taxi centerline network (+ enclosed-pocket
    perimeters, Pass A2)
  * airside ring-edge sweep  (Pass A3) — flat shadow off every
    TERRAIN-FACING pavement ring edge (junctions, aprons, per-node
    runway pieces, service roads) at the local rendered edge altitude
  * runway lateral strips    (ROLE_RUNWAY_CLEARANCE)  — flat shadow
  * runway-end RESA areas     (ROLE_RUNWAY_CLEARANCE)  — ramp

Public API:
    emit_surface_clearance_cuts(layout, dem, tile_lat, tile_lon)
"""
from __future__ import annotations

import math
import os
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
    RUNWAY_END_SKIRT_ENABLED,
    runway_end_approach_class,
    runway_strip_half_width_m,
    taxiway_clearance_half_width_for_letter,
    taxiway_clearance_half_width_m,
)
from .grade_law import (
    runway_end_constrained_length_m,
    runway_end_governed_length_m,
    runway_end_skirt_floor_profile,
    runway_end_skirt_profile_breakpoints,
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
    ROLE_SERVICE_JUNCTION,
    ROLE_SERVICE_ROAD,
    ROLE_STUB,
    ROLE_TAXIWAY_CLEARANCE,
    SHARED_VERTEX_TOL_M,
    vertex_bucket,
)
from .elevation import _sample_dem
from .pavement.junctions import _decompose_polygon_with_holes
from .pavement.runways import _sample_runway_segment_elev

__all__ = ["emit_surface_clearance_cuts", "emit_runway_end_skirts"]


_TAXIWAY_ROLES = (
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR,
)
# Minimum emitted cut area; smaller residue is dropped as noise.
_MIN_CUT_AREA_M2 = 25.0
# Keep every emitted cut vertex this far OUTSIDE pavement so it never
# lands on a sloping rect's edge (``check_vertex_on_sloping_edge`` /
# ``test_no_vertex_on_sloping_rect_edge`` flag a non-rect vertex within
# ``EDGE_PROX_M`` = 0.5 m of a rect edge interior) and never merges with
# a pavement ring vertex (``SHARED_VERTEX_TOL_M`` = 0.5 m).  Reduced from
# 1.5 → 1.0 m (user 2026-07-07: clearance cuts should hug the pavement
# they follow) — still 2× the 0.5 m merge/edge-proximity floor, so the
# inner edge never lands on or merges with pavement, but the visible
# crack band between pavement and cut halves.
_PAVEMENT_GAP_M = 1.0
# Enclosed-pocket wingtip clearance (Pass A2, user 2026-06-30): ring the full
# perimeter of a small NON-pavement pocket fully enclosed by taxi pavement, so
# sharp terrain a wingtip overhangs is cut even where no centerline reaches it.
# O4_POCKET_CLEARANCE=0 disables it (revert to centerline-only junction clearance).
_POCKET_CLEARANCE = os.environ.get("O4_POCKET_CLEARANCE", "1") == "1"
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
# Consecutive ring vertices closer than this are a degenerate zero-length
# edge that ``_decimate`` PRESERVES whenever their altitudes differ (it
# reads the altitude step as a real feature).  At emit they become two
# nodes at one spot several metres apart vertically — a torn vertical
# micro-cliff.  Below this tolerance the edge has no real length, so the
# pair is collapsed to one vertex at the mean altitude (see
# ``_merge_coincident_ring_vertices``).  Well under ``_DECIMATE_GEOM_TOL_M``
# so it never merges vertices ``_decimate`` keeps for genuine geometry.
_COINCIDENT_MERGE_TOL_M = 0.1
# An ALTITUDE needle: a single ring vertex whose altitude differs from
# BOTH of its ring neighbours by more than this, while the two
# neighbours agree with each other to within it.  The finalize resample
# assigns each final-ring vertex the altitude of the NEAREST source
# strip edge; where strips from different bands (e.g. a low apron cut
# beside a higher runway strip, or a thin corridor whose inner and
# outer rows pass within ``EDGE_TOL_M`` of one another at a concave
# jog) meet, one vertex can flip to the far edge and spike metres
# above/below its neighbours — the "terrain spike at a little jog" /
# "pointy cut" the user sees (CYXY carried 3 such needles before the
# part-30f declaw).  A real daylight-contour ramp changes monotonically
# across several vertices; an isolated single-vertex reversal is always
# this artifact, so it is clamped to the neighbour mean.
_NEEDLE_ALT_TOL_M = 3.0
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
# Pass A3 ring-edge sweep: a ring-edge station only faces TERRAIN when
# the point this far outward is not covered by any already-emitted
# shape (adjacent pavement / ribbon / building / groundside all own
# their band).  Just past ``_PAVEMENT_GAP_M`` so a shared shape edge is
# reliably detected, well under the narrowest service-road width.
# Tracks the reduced 1.0 m gap (user 2026-07-07 tighter standoff).
_RING_PROBE_M = 1.5
# Pass A3 skips a runway-family ring-edge station whose outward normal
# points mostly ALONG the runway axis (an END edge): the runway end is
# RESA / skirt territory (Pass C / D) and must stay exactly as it is.
_RING_END_NORMAL_DOT = 0.7
# How far past the runway end to search for the outer pavement edge
# (blast-pad / stopway / apron) the RESA should anchor on.
_RESA_PAVEMENT_PROBE_MAX_M = 300.0
# When the RESA end is taken from the authoritative apt.dat row-100
# centreline endpoint, that point sits ON the runway end edge, not in the
# interior — seed the outward pavement-exit march this far INSIDE so it
# starts on pavement.
_RESA_SEED_INSET_M = 3.0
# Window (m) over which the runway's own end grade is measured for the
# Pass D skirt, so a DESCENDING runway hands its grade to the skirt
# without a crease.  Module-level so the ``verification`` reader
# measures the entry grade EXACTLY as the emitter does (lockstep).
_SKIRT_END_GRADE_WINDOW_M = 30.0


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


def _merge_coincident_ring_vertices(
        coords: list[tuple[float, float]],
        alts: list[float],
        tol_m: float = _COINCIDENT_MERGE_TOL_M):
    """Collapse consecutive near-coincident ring vertices into one.

    ``_decimate`` keeps a vertex sitting a few mm from its neighbour
    whenever their altitudes differ (it preserves altitude features), so
    a zero-length edge across an altitude step survives into the emitted
    polygon — two nodes at one XY metres apart vertically, which X-Plane
    renders as a torn vertical micro-cliff.  Below ``tol_m`` the edge has
    no real length, so merge each coincident run to a single vertex at the
    GROUP-MEAN altitude (open-form ``(coords, alts)`` in, same out).
    """
    coords = [(float(x), float(y)) for x, y in coords]
    alts = [float(a) for a in alts]
    tol2 = tol_m * tol_m
    changed = True
    while changed and len(coords) > 3:
        changed = False
        n = len(coords)
        for i in range(n):
            j = (i + 1) % n
            dx = coords[i][0] - coords[j][0]
            dy = coords[i][1] - coords[j][1]
            if dx * dx + dy * dy <= tol2:
                coords[i] = ((coords[i][0] + coords[j][0]) / 2.0,
                             (coords[i][1] + coords[j][1]) / 2.0)
                alts[i] = round((alts[i] + alts[j]) / 2.0, 1)
                del coords[j]
                del alts[j]
                changed = True
                break
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


def _declaw_alt_needles(alts_open: list[float],
                        tol: float = _NEEDLE_ALT_TOL_M) -> list[float]:
    """Clamp isolated single-vertex altitude spikes to the neighbour mean.

    Operates on an OPEN (unclosed) per-vertex altitude ring.  A vertex is
    a needle when it differs from BOTH ring neighbours by more than
    ``tol`` in the SAME direction while the neighbours agree to within
    ``tol`` (see ``_NEEDLE_ALT_TOL_M``).  Such a vertex is a resample
    flip between the cut's inner (pavement-level) and outer (terrain-
    daylight) edges, not a real surface feature, so it is set to the mean
    of its two neighbours.  Repeats until stable so a two-vertex flip
    resolves.  Returns a new list; positions are untouched (geometry is
    unchanged — only the altitude is corrected)."""
    n = len(alts_open)
    if n < 3:
        return list(alts_open)
    a = [float(v) for v in alts_open]
    for _ in range(n):
        changed = False
        for i in range(n):
            p = a[(i - 1) % n]
            c = a[i]
            q = a[(i + 1) % n]
            d1 = c - p
            d2 = c - q
            if (d1 * d2 > 0.0
                    and min(abs(d1), abs(d2)) > tol
                    and abs(p - q) <= tol):
                a[i] = round((p + q) / 2.0, 1)
                changed = True
        if not changed:
            break
    return a


def _make_strip_alt_resampler(strips):
    """Build the strip-edge / strip-vertex index ONCE over ``strips``
    (each ``(open_ring, open_alts)``) and return a
    ``resample(ring_open) -> alts`` closure.

    For each ring vertex: the altitude interpolated along the NEAREST
    strip edge within ``EDGE_TOL_M`` (where strips overlap, the nearest
    edge wins — i.e. the closest pavement-edge profile governs), else
    the altitude of the nearest strip vertex.  Lets a single polygon
    unioned from many strips carry a faithful per-vertex elevation.

    Vectorised: an STRtree ``dwithin`` query cuts each vertex's candidate
    edges to the handful within ``EDGE_TOL_M`` (instead of scanning every
    strip edge — the previous O(V·E) loop was the dominant build cost on
    apron-heavy airports), then the EXACT same perpendicular-foot
    projection + nearest-edge tie-break is applied over those candidates.
    Points with no in-range edge fall back to the nearest strip vertex.

    The index is built ONCE here (not per call): ``_finalize`` resamples
    every emitted piece twice, and rebuilding the segment STRtree per
    piece made finalize the dominant build cost once the Pass A3 ring
    sweep multiplied the strip count (HECA: ~39 s -> ~4 s)."""
    EDGE_TOL_M = 0.5
    EDGE_TOL2 = EDGE_TOL_M * EDGE_TOL_M

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

    sx = np.asarray(sxl)
    sy = np.asarray(syl)
    dx = np.asarray(dxl)
    dy = np.asarray(dyl)
    seg2 = np.asarray(seg2l)
    a0 = np.asarray(a0l)
    a1 = np.asarray(a1l)
    tree = STRtree(seg_geoms) if seg_geoms else None
    if vxl:
        vat = np.asarray(vatl)
        vtree = STRtree(shapely.points(np.asarray(vxl), np.asarray(vyl)))
    else:
        vat = None
        vtree = None

    def resample(ring_open):
        if not ring_open:
            return []
        n = len(ring_open)
        rx = np.fromiter((p[0] for p in ring_open), dtype=float, count=n)
        ry = np.fromiter((p[1] for p in ring_open), dtype=float, count=n)
        best_alt = np.full(n, np.nan)

        if tree is not None:
            qpts = shapely.points(rx, ry)
            # pairs[0] = ring-vertex index, pairs[1] = candidate segment
            # index.
            pairs = tree.query(qpts, predicate="dwithin",
                               distance=EDGE_TOL_M)
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

        # Fallback: nearest strip vertex for any ring vertex with no
        # in-range edge match (matches the original unbounded nearest-
        # vertex search, including its first-in-order tie-break — keep
        # all tied nearest, then pick the lowest vertex index).
        missing = np.isnan(best_alt)
        if missing.any() and vtree is not None:
            mi = np.flatnonzero(missing)
            nn = vtree.query_nearest(shapely.points(rx[mi], ry[mi]),
                                     all_matches=True)
            order = np.lexsort((nn[1], nn[0]))  # by input, then vertex idx
            inps = nn[0][order]
            firstm = np.empty(inps.shape, dtype=bool)
            firstm[0] = True
            firstm[1:] = inps[1:] != inps[:-1]
            best_alt[mi[inps[firstm]]] = vat[nn[1][order][firstm]]

        return [round(float(a), 1) if not np.isnan(a) else 0.0
                for a in best_alt]

    return resample


def _resample_alts_over_strips(ring_open, strips):
    """One-shot convenience wrapper over
    :func:`_make_strip_alt_resampler` (kept for parity with older call
    sites/tests; hot paths build the resampler once and reuse it)."""
    return _make_strip_alt_resampler(strips)(ring_open)


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
                # Widened taper neighbour beyond a skipped (pavement-
                # facing / altitude-less) station: borrow the run-end
                # station's edge altitude so a SHORT run still emits a
                # strip.  A single obstructed station between two
                # skipped ones used to yield a 1-vertex row and drop
                # the whole cut (HECA apron/service-road corridors).
                if i < i0:
                    ref = edge_alts[i0]
                elif i > i1:
                    ref = edge_alts[i1]
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
            # Outer edge: at the daylight point, on the ceiling, so the
            # whole band is graded to the protective surface and the cut
            # meets natural ground where the terrain has daylit.  Where
            # the terrain has NOT daylit within the band cap (pavement
            # dug in below its surroundings) this leaves a cut FACE at
            # the band edge — that is the cut doing its job, not a
            # defect (part 30k, user in-sim ruling: an un-cut cliff
            # beside pavement is the complaint; the excavation is
            # wanted).  Part 30f briefly lifted this row to
            # max(ceiling, DEM): that tilted whole cut surfaces up to
            # the DEM so they capped nothing (HECA: 508/2281 cut
            # vertices rode the DEM vs 40 before; a trapped-station-only
            # lift still left 339 riding above even a 5% ramp allowance
            # because HECA is broadly dug-in) — reverted.  The single-
            # vertex spike class 30f also fixed stays fixed by
            # ``_declaw_alt_needles`` (FIX B) and the tighter standoff
            # (FIX C), both kept.
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
# Core: build FILL skirts off one edge (the inverse of the cut strips)
# ──────────────────────────────────────────────────────────────────
def _skirt_lift_alt(analytic_floor: float, dem_alt) -> float:
    """The lawful skirt altitude at a vertex: ``max(floor, DEM)``.

    The skirt is FILL-only (the exact mirror of the cut passes'
    flat-shadow convention — cuts never fill, fills never cut; see
    docs/STANDARDS.md "Lateral (wingtip) clearance").  A band triggers
    only where the terrain falls MORE than the trigger below the floor,
    but the emitted band can still SPAN vertices whose DEM sits AT or
    ABOVE the floor (a bump inside a hollow; the last+step overshoot at
    a run's end).  Grading those DOWN to the analytic floor would carve
    an unnecessary cut ramp — so every skirt vertex lifts to the higher
    of the analytic floor and the DEM.  A vertex at/above the floor that
    descends no lower than the floor profile is lawful; the down-grade
    caps bound how far BELOW the floor is reachable, not how the surface
    rides an existing bump back up.  Shared verbatim by the emitter's
    ring altitudes and its analytic ``alt_at`` closures so shared band
    boundary rows compute the SAME max'ed value and the surface never
    tears."""
    if dem_alt is None:
        return analytic_floor
    return max(analytic_floor, float(dem_alt))


def _build_filled_skirts(edge_stations, edge_alts, outwards,
                         band_caps, floor_depth, band_edges, trigger,
                         step, sample_dem):
    """Fill-direction twin of ``_build_graded_strips``: at each station a
    FLOOR descends from the pavement-end altitude,

        floor(d) = edge_alt − floor_depth(d)

    (``floor_depth`` is the law's lowest-lawful-surface profile,
    ``grade_law.runway_end_skirt_floor_profile``, as a per-distance
    callable).  Terrain BELOW the floor is filled up to it, and the fill
    DAYLIGHTS where the floor meets the DEM — so the skirt is only as
    long as it needs to be, capped at ``band_caps[i]`` (the governed
    length; beyond it a drop is lawful).  Fill-only: a station
    contributes ONLY where the terrain falls more than ``trigger`` m
    below the floor; flat or rising terrain is left untouched (that is
    the cut passes' domain).

    Unlike the cut twin (whose linear ceiling a two-row ring renders
    exactly), the floor is CURVED (piecewise quadratic), and a
    ``node_altitudes`` polygon renders as a ruled surface between its
    rows — a single inner/outer ring would sag metres below the law
    floor mid-span.  So the skirt is emitted as ABUTTING BANDS split at
    ``band_edges`` (the law's own grade breakpoints,
    ``grade_law.runway_end_skirt_profile_breakpoints``): within a band
    the floor is one quadratic, bounding the chord sagitta at
    ``rate·L²/8`` (≤ 0.31 m) — far inside the fill trigger.  Adjacent
    bands share their boundary row vertices (same positions, same
    rounded altitudes), so they render as one continuous surface.

    Returns ``(ring_open, alts_open)`` pairs, one per (band × station
    run); the first band's inner edge sits a small gap outside the
    pavement at the floor's start altitude, each band's outer edge at
    the band boundary — or earlier, at the daylight point on the floor
    (= DEM there), so the fill meets natural ground with no step.
    """
    n = len(edge_stations)
    outer: list[float] = [0.0] * n
    dropped: list[bool] = [False] * n
    cap_max = 0.0
    for i, (sx, sy) in enumerate(edge_stations):
        ref = edge_alts[i]
        if ref is None:
            continue
        nx, ny = outwards[i]
        cap = band_caps[i]
        if cap <= _PAVEMENT_GAP_M:
            continue
        cap_max = max(cap_max, cap)
        nst = max(1, int(math.ceil(cap / step)))
        last = 0.0
        for k in range(1, nst + 1):
            d = min(cap, k * step)
            floor = ref - floor_depth(d)
            dd = sample_dem(sx + nx * d, sy + ny * d)
            if dd is not None and dd < floor - trigger:
                last = d
        if last > 0.0:
            dropped[i] = True
            outer[i] = min(cap, last + step)
    if not any(dropped):
        return []
    # Band boundaries: pavement gap → law breakpoints → governed cap.
    edges = [_PAVEMENT_GAP_M]
    for b in sorted(band_edges):
        if _PAVEMENT_GAP_M + 1.0 < b < cap_max - 1.0:
            edges.append(float(b))
    edges.append(cap_max)

    out: list[tuple[list, list]] = []
    for b in range(len(edges) - 1):
        d0, d1 = edges[b], edges[b + 1]
        # Stations whose fill reaches into this band.
        idx = [i for i in range(n) if dropped[i] and outer[i] > d0]
        if not idx:
            continue
        runs: list[list[int]] = []
        cur = [idx[0]]
        for j in idx[1:]:
            if j - cur[-1] <= 2:
                cur.append(j)
            else:
                runs.append(cur)
                cur = [j]
        runs.append(cur)
        for run in runs:
            i0, i1 = run[0], run[-1]
            # Widen the FIRST band's runs by one station each side so
            # the fill tapers longitudinally to its neighbours instead
            # of ending in a wall; deeper bands end where their
            # stations do (their width-wise taper is the daylight).
            lo = max(0, i0 - 1) if b == 0 else i0
            hi = min(n - 1, i1 + 1) if b == 0 else i1
            inner_pts, inner_alts = [], []
            outer_pts, outer_alts = [], []
            for i in range(lo, hi + 1):
                ref = edge_alts[i]
                if ref is None:
                    continue
                nx, ny = outwards[i]
                if outer[i] > d0:
                    off = min(d1, outer[i])
                elif b == 0:
                    # Taper neighbour of a first-band run.
                    off = step
                else:
                    continue
                sx, sy = edge_stations[i]
                ix, iy = sx + nx * d0, sy + ny * d0
                # Lift-only: the ring rides the HIGHER of the analytic
                # floor and the DEM at each vertex, so a triggered band
                # spanning a bump above the floor never cuts it down.
                inner_alts.append(round(_skirt_lift_alt(
                    float(ref - floor_depth(d0)),
                    sample_dem(ix, iy)), 1))
                inner_pts.append((ix, iy))
                ox, oy = sx + nx * off, sy + ny * off
                outer_pts.append((ox, oy))
                outer_alts.append(round(_skirt_lift_alt(
                    float(ref - floor_depth(off)),
                    sample_dem(ox, oy)), 1))
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

    FALLBACK detector — used only when the authoritative apt.dat row-100
    runway list is unavailable (see ``emit_surface_clearance_cuts``'s
    ``source_runways``).  A runway is usually split into many segments
    (crossings, FAA profile redistribution, tile cuts), so an internal-seam
    test is fragile.  Instead we collect every segment's two short edges per
    ref and pick the PAIR of short-edge midpoints that are FARTHEST apart:
    those are the runway's two thresholds; everything between them is
    interior.  ``full_len`` is that farthest-pair distance (the whole runway
    length), used for the ICAO code number.
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


def _nearest_pav_alt(pav_shapes, x, y,
                     max_distance_m: float = 5.0) -> float | None:
    """Pavement altitude at ``(x, y)`` via the NEAREST shape's
    edge-interpolated read — containment-free.  ``_pav_alt`` requires a
    shape to CONTAIN the point, so a hairline inter-shape gap (or a
    decimated ring grazing the sample) flips the read to ``None`` and a
    caller's fallback silently changes the answer — the Pass D skirt and
    its ``verification`` reader measure the runway END GRADE from two
    such samples and MUST agree (a lost sample flattened the validator's
    entry grade at KCLT 18L and phantom-flagged a lawful skirt).  Points
    farther than ``max_distance_m`` from any pavement return ``None``."""
    pt = Point(x, y)
    best, best_distance = None, max_distance_m
    for s in pav_shapes:
        try:
            d = s.polygon.distance(pt)
        except _GEOM_EXC:
            continue
        if d < best_distance or (best is None and d <= best_distance):
            best_distance, best = d, s
    if best is None:
        return None
    return _edge_interp_alt(best, x, y)


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
                                tile_lat: int, tile_lon: int,
                                source_runways=None) -> int:
    """Emit wingtip/RESA terrain-clearance cut polygons.  Mutates
    ``layout.shapes``.  Returns the number of cut shapes emitted.

    ``source_runways`` is the apt.dat row-100 ``Runway`` list (centreline
    endpoints + width).  When supplied, the runway-end RESA anchors on that
    AUTHORITATIVE geometry — the exact threshold position and runway width —
    so it is independent of how the runway pavement was segmented/emitted.
    Without it, the RESA falls back to detecting ends from the emitted
    runway rects."""
    if dem is None:
        return 0
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH
    step = CLEARANCE_STATION_STEP_M

    def _ll_to_m(lat: float, lon: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * R * cos0,
                math.radians(lat - lat0) * R)

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
    # The UNbuffered union doubles as the Pass A3 terrain-facing test
    # (a ring-edge station whose outward probe lands on ANY emitted
    # shape — adjacent pavement, ribbon, building, groundside — is not
    # facing terrain: that shape owns its own band).
    static_polys = [s.polygon for s in layout.shapes
                    if s.polygon is not None and not s.polygon.is_empty]
    static_union = None
    static_block = None
    if static_polys:
        try:
            static_union = unary_union(static_polys)
            static_block = static_union.buffer(_PAVEMENT_GAP_M)
        except _GEOM_EXC:
            static_union = None
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
        """Validate a raw strip ring and stash it for the finalize pass.

        A strip ring built along a CONCAVE pavement edge (the Pass A3
        ring sweep; sharply bending centerline runs) can self-intersect,
        and ``buffer(0)`` then yields a MultiPolygon.  Keep every
        polygon part — each carries the SAME source ring/alts, which is
        what the finalize resample consumes (nearest strip edge wins) —
        instead of silently dropping the whole run (a junction-notch
        spike at HECA survived exactly that way)."""
        try:
            raw = Polygon(ring)
            if not raw.is_valid:
                raw = raw.buffer(0)
        except _GEOM_EXC:
            return
        if raw.is_empty:
            return
        parts = ([raw] if raw.geom_type == "Polygon"
                 else [g for g in getattr(raw, "geoms", [])
                       if g.geom_type == "Polygon" and not g.is_empty])
        ring_l, alts_l = list(ring), list(alts)   # ONE copy for all parts
        for part in parts:
            if part.area < _MIN_CUT_AREA_M2:
                continue
            raw_strips.append((part, ring_l, alts_l, role))

    def _finalize() -> int:
        """Union all collected strips, subtract pavement once, and emit
        one ``node_altitudes`` shape per connected region — decomposed
        into simple polygons only where a real pavement hole forces a
        split.  Per-vertex altitudes are sampled from the nearest source
        strip edge, so overlapping bands resolve to a single surface
        instead of abutting slivers."""
        if not raw_strips:
            return 0
        # One resample source per RING (a multi-part strip shares its
        # ring object across parts — do not multiply the edge tree).
        strips = []
        _seen_rings: set[int] = set()
        for _p, ring, alts, _r in raw_strips:
            if id(ring) in _seen_rings:
                continue
            _seen_rings.add(id(ring))
            strips.append((ring, alts))
        # ONE edge/vertex index over all strips; every piece resamples
        # against it (twice) — see _make_strip_alt_resampler.
        _resample = _make_strip_alt_resampler(strips)
        # Inner-edge snap: a cut vertex sitting at the pavement gap follows the
        # rendered edge altitude of the airside shape it ABUTS.  The strips of
        # runway + taxiway clearance are unioned into one region, so resampling
        # alone let a vertex beside an APRON pick up the far higher RUNWAY strip
        # and spike several metres (CYXY taxiway-A2 clearance sat 5 m above the
        # apron 1.5 m away).  Snapping to the nearest pavement makes the cut
        # follow the surface it protects, whatever that surface is.
        # Service roads are not in the airside union but the Pass A3
        # ring sweep cuts alongside them — include them so a cut vertex
        # beside a road follows the ROAD edge (no step against it).
        _pav_list = [s for s in airside
                     if s.polygon is not None and not s.polygon.is_empty]
        _pav_list += [s for s in layout.shapes
                      if s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)
                      and s.polygon is not None and not s.polygon.is_empty]
        _pav_tree = None
        if _pav_list:
            try:
                _pav_tree = STRtree([s.polygon for s in _pav_list])
            except _GEOM_EXC:
                _pav_tree = None
        # Inner vertices sit at the 1.5 m pavement gap; corners pulled in by
        # decimation/merge can reach ~2.5 m.  The OUTER (daylight) edge is a
        # full station step (5 m) out, so a 3.5 m window snaps every inner
        # vertex without ever catching the daylight edge.
        _SNAP_TOL_M = _PAVEMENT_GAP_M + 2.0

        def _adjacent_pav_alt(x: float, y: float):
            if _pav_tree is None:
                return None
            pt = Point(x, y)
            try:
                s = _pav_list[int(_pav_tree.nearest(pt))]
            except Exception:                                  # pragma: no cover
                return None
            if s.polygon.distance(pt) > _SNAP_TOL_M:
                return None
            return _edge_interp_alt(s, x, y)
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
                alts0 = _resample(ring)
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
                node_open = _resample(final_ring)
                for vi, (vx, vy) in enumerate(final_ring):
                    a = _adopt_alt(vx, vy)
                    if a is not None:
                        node_open[vi] = a
                # Inner edge follows the pavement it abuts (overrides the strip
                # resample / sibling adoption for pavement-adjacent vertices).
                for vi, (vx, vy) in enumerate(final_ring):
                    pa = _adjacent_pav_alt(vx, vy)
                    if pa is not None:
                        node_open[vi] = round(float(pa), 1)
                # Collapse degenerate zero-length edges across an altitude
                # step (the torn vertical micro-cliff — see
                # ``_merge_coincident_ring_vertices``).  Only adopt the
                # collapsed ring if it stays a valid cut of real area.
                merged_ring, merged_open = _merge_coincident_ring_vertices(
                    final_ring, node_open)
                if (len(merged_ring) >= 3
                        and len(merged_ring) < len(final_ring)):
                    try:
                        cand = Polygon(merged_ring)
                    except _GEOM_EXC:
                        cand = None
                    if (cand is not None and cand.is_valid
                            and not cand.is_empty
                            and cand.area >= _MIN_CUT_AREA_M2):
                        poly = cand
                        final_ring = merged_ring
                        node_open = merged_open
                # Clamp isolated resample flips between the inner (pavement
                # level) and outer (terrain daylight) edges — the single-
                # vertex spikes at concave jogs of a sunk-pavement corridor.
                # LAST altitude op before emit + seam store, so a spike from
                # resample, sibling adoption OR the coincident-vertex merge
                # is removed and never propagates through the ``adopt`` seam.
                node_open = _declaw_alt_needles(node_open)
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
    if centerlines and prep_pav is not None:
        for entry in centerlines:
            line = entry.line if hasattr(entry, "line") else (entry[0] if isinstance(entry, tuple) else entry)
            ref = entry[1] if (isinstance(entry, tuple)
                               and len(entry) > 1) else ""
            if not isinstance(line, LineString) or line.is_empty:
                continue
            letter = entry.dominant_size() if hasattr(entry, "dominant_size") else None
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

    # ── Pass A2: ENCLOSED-POCKET wingtip clearance (user 2026-06-30) ──
    # A taxi network can fully ENCLOSE a small non-pavement pocket (a hole in the
    # airside union) between converging junctions.  A taxiing aircraft's wingtip
    # overhangs INTO that pocket from the surrounding pavement, but Pass A
    # (centerline-perpendicular, reach = clear_half) leaves the pocket's oblique
    # far edges uncovered — no centerline's perpendicular reaches them as the
    # junction widens past clear_half — so sharp terrain in the pocket slips
    # through (CYXY jct134/jct148 throat).  Ring the ENTIRE perimeter of every
    # pocket small enough to lie within wingtip reach, regardless of centerlines.
    if airside and prep_pav is not None and _POCKET_CLEARANCE:
        try:
            _au = unary_union([s.polygon for s in airside])
            _au_polys = (list(_au.geoms) if _au.geom_type == "MultiPolygon"
                         else [_au])
        except _GEOM_EXC:
            _au_polys = []
        _cls = [e for e in (getattr(layout, "apt_taxi_centerlines", None) or [])
                if getattr(e, "line", None) is not None and not e.line.is_empty
                and not getattr(e, "is_service", False)]
        _default_half = taxiway_clearance_half_width_m(_MAX_TAXIWAY_WIDTH_M)

        def _pocket_edge_alt(x, y):
            """Pavement-surface altitude at a pocket-boundary point — the NEAREST
            airside shape's edge-interpolated altitude (the boundary may fall in a
            hairline inter-shape gap that ``_pav_alt`` containment misses)."""
            p = Point(x, y)
            best, bd = None, float("inf")
            for s in airside:
                try:
                    d = s.polygon.distance(p)
                except _GEOM_EXC:
                    continue
                if d < bd:
                    bd, best = d, s
            return _edge_interp_alt(best, x, y) if best is not None else None

        for _poly in _au_polys:
            for _hole in _poly.interiors:
                try:
                    pocket = Polygon(_hole)
                except _GEOM_EXC:
                    continue
                if pocket.is_empty or pocket.area < _MIN_CUT_AREA_M2:
                    continue
                # LOCAL wingtip reach: the nearest taxi centerline's code letter
                # (so a code-B pocket isn't ringed with a code-E band).
                cen = pocket.centroid
                pk_half = _default_half
                if _cls:
                    e = min(_cls, key=lambda e: e.line.distance(cen))
                    if hasattr(e, "dominant_size") and e.dominant_size():
                        pk_half = taxiway_clearance_half_width_for_letter(
                            e.dominant_size())
                # WINGTIP-POCKET gate: erode by the reach; a true pocket lies
                # entirely within reach so its core vanishes.  The open INFIELD
                # keeps a large core → skipped (Pass A covers its centerlined band).
                try:
                    core = pocket.buffer(-pk_half)
                except _GEOM_EXC:
                    continue
                if not core.is_empty and core.area > _MIN_CUT_AREA_M2:
                    continue
                ring_open = _open_coords(pocket)
                m = len(ring_open)
                stations, outs, alts, bws = [], [], [], []
                for i in range(m):
                    a = ring_open[i]
                    b = ring_open[(i + 1) % m]
                    out = _outward_normal(pocket, a, b)   # OUT of pocket → pavement
                    if out is None:
                        continue
                    into = (-out[0], -out[1])             # INTO pocket → terrain
                    for (sx, sy) in _stations(a, b, step)[:-1]:
                        ref = _pocket_edge_alt(sx + out[0] * 0.5, sy + out[1] * 0.5)
                        stations.append((sx, sy))
                        outs.append(into)
                        alts.append(ref)
                        bws.append(pk_half)
                if len(stations) >= 2:
                    for ring, ralts in _build_graded_strips(
                            stations, alts, outs, bws, tx_slope, tx_threshold,
                            step, sample_dem):
                        _collect(ring, ralts, ROLE_TAXIWAY_CLEARANCE)

    # ── Pass A3: airside ring-edge sweep (part 30) ──────────────────
    # Pass A traces the apt.dat taxi CENTERLINE network and Pass B walks
    # 4-corner runway rects carrying ``altitude``/hi-lo — but since part
    # 25 (hi/lo emission retired) every sloped shape, and since the
    # unified runway representation most runway pieces, emits per-node
    # ``node_altitudes`` polygons with arbitrary vertex counts.  So
    # junction / apron / service-road edges away from a centerline (and
    # per-node runway long edges) were INVISIBLE to the clearance
    # builder — terrain spikes survived right beside pavement (HECA,
    # user 2026-07-07: audit clusters beside exactly those roles).
    # Walk every TERRAIN-FACING exterior-ring edge of airside pavement
    # and service roads, sample the DEM outward at the same stations,
    # and cut exactly the way Pass A does: flat shadow at the LOCAL
    # rendered edge altitude (per-node altitudes interpolated along the
    # edge — the crowned, solved values), cut-only, daylighting at the
    # DEM.  Overlap with the Pass A/B strips resolves in the shared
    # ``_finalize`` union; pavement/building/ribbon overlap is removed
    # there by the ``static_block`` difference as for every other cut.
    prep_static = None
    if static_union is not None and not static_union.is_empty:
        try:
            prep_static = prep(static_union)
        except _GEOM_EXC:
            prep_static = None
    if prep_static is not None:
        # Local wingtip half-width beyond a taxi-family pavement edge:
        # the nearest aircraft-taxi centerline's ICAO code letter (the
        # Pass A2 pocket rule — an aircraft may occupy pavement right up
        # to the edge, so the band is the full wingtip reach), else the
        # capped-width fallback.
        _a3_wing_default = taxiway_clearance_half_width_m(
            _MAX_TAXIWAY_WIDTH_M)
        _a3_cls = [e for e in (getattr(layout, "apt_taxi_centerlines",
                                       None) or [])
                   if getattr(e, "line", None) is not None
                   and not e.line.is_empty
                   and not getattr(e, "is_service", False)]
        _a3_cl_tree = None
        if _a3_cls:
            try:
                _a3_cl_tree = STRtree([e.line for e in _a3_cls])
            except _GEOM_EXC:
                _a3_cl_tree = None

        def _a3_wing_half(poly) -> float:
            if _a3_cl_tree is not None:
                try:
                    e = _a3_cls[int(_a3_cl_tree.nearest(poly.centroid))]
                    letter = (e.dominant_size()
                              if hasattr(e, "dominant_size") else "")
                    if letter:
                        return taxiway_clearance_half_width_for_letter(
                            letter)
                except (_GEOM_EXC + (IndexError, KeyError)):
                    pass
            return _a3_wing_default

        # Row-100 runway centrelines (authoritative geometry — the same
        # source the RESA anchors on) give each runway-family station
        # its Annex-14 graded-strip band: the strip reaches
        # ``runway_strip_half_width_m(full_len)`` from the CENTERLINE,
        # so the band beyond the ring edge is that reach minus the
        # station's centerline distance.  Without ``source_runways``
        # the runway-family walk is skipped — Pass B still covers the
        # flat 4-corner rects, and RESA/skirts are untouched either way.
        _a3_rw_axes: list[tuple[LineString, tuple[float, float], float]] = []
        if source_runways:
            for r in source_runways:
                try:
                    rax, ray = _ll_to_m(r.lat_a, r.lon_a)
                    rbx, rby = _ll_to_m(r.lat_b, r.lon_b)
                except _GEOM_EXC:
                    continue
                rlen = math.hypot(rbx - rax, rby - ray)
                if rlen < 1.0:
                    continue
                _a3_rw_axes.append(
                    (LineString([(rax, ray), (rbx, rby)]),
                     ((rbx - rax) / rlen, (rby - ray) / rlen),
                     runway_strip_half_width_m(rlen)))

        _a3_usable_rw = {id(s) for s in runway_shapes}
        _a3_sv_threshold = CLEARANCE_OBSTRUCTION_THRESHOLD_M["service"]
        _a3_sv_band = CLEARANCE_MAX_REACH_M["service"]
        _a3_tx_reach = CLEARANCE_MAX_REACH_M["taxiway"]
        _a3_rw_threshold = CLEARANCE_OBSTRUCTION_THRESHOLD_M["runway"]
        _a3_rw_reach = CLEARANCE_MAX_REACH_M["runway"]
        _a3_taxi_roles = (ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
                          ROLE_STUB, ROLE_CROSS_CONNECTOR,
                          ROLE_JUNCTION, ROLE_APRON)
        for s in layout.shapes:
            if (s.polygon is None or s.polygon.is_empty
                    or s.polygon.geom_type != "Polygon"):
                continue
            role = s.role
            rw_axis = None
            if role in _a3_taxi_roles:
                band_cap = min(_a3_wing_half(s.polygon), _a3_tx_reach)
                threshold = tx_threshold
                out_role = ROLE_TAXIWAY_CLEARANCE
            elif role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION):
                band_cap = _a3_sv_band
                threshold = _a3_sv_threshold
                out_role = ROLE_TAXIWAY_CLEARANCE
            elif role in (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING):
                if not _a3_rw_axes or id(s) in _a3_usable_rw:
                    continue    # Pass B owns the flat 4-corner rects
                try:
                    cen = s.polygon.centroid
                    rw_axis = min(_a3_rw_axes,
                                  key=lambda a: a[0].distance(cen))
                except (_GEOM_EXC + (ValueError,)):
                    continue
                band_cap = min(rw_axis[2], _a3_rw_reach)
                threshold = _a3_rw_threshold
                out_role = ROLE_RUNWAY_CLEARANCE
            else:
                continue
            if (not s.node_altitudes and s.altitude is None
                    and (s.altitude_high is None or s.altitude_low is None)):
                continue        # no rendered elevation to shadow
            if band_cap <= _PAVEMENT_GAP_M + 1.0:
                continue
            try:
                coords = list(s.polygon.exterior.coords)
                ccw = bool(s.polygon.exterior.is_ccw)
            except _GEOM_EXC:
                continue
            if len(coords) < 4:
                continue
            # Per-CLOSED-ring-node altitudes aligned with ``coords``
            # (the ``node_altitudes`` contract — see _edge_interp_alt);
            # flat / hi-lo shapes fall back to their plane sampler.
            na = s.node_altitudes
            if na:
                nm = min(len(na), len(coords))
                ring_alts: list[float | None] = [
                    None if na[i] is None else float(na[i])
                    for i in range(nm)]
                ring_alts += [None] * (len(coords) - nm)
            elif s.altitude is not None:
                ring_alts = [float(s.altitude)] * len(coords)
            else:
                ring_alts = [_sample_runway_segment_elev(s, x, y)
                             for x, y in coords]
            stations, st_alts, outs, bws = [], [], [], []
            for i in range(len(coords) - 1):
                eax, eay = coords[i]
                ebx, eby = coords[i + 1]
                u = _unit(ebx - eax, eby - eay)
                if u is None:
                    continue
                # Outward normal from the ring ORIENTATION (the centroid
                # flip in ``_outward_normal`` is wrong on concave rings).
                out = (u[1], -u[0]) if ccw else (-u[1], u[0])
                a0 = ring_alts[i]
                a1 = ring_alts[i + 1]
                nseg = max(1, int(math.ceil(
                    math.hypot(ebx - eax, eby - eay) / step)))
                for k in range(nseg):   # next edge owns the far corner
                    t = k / nseg
                    sx = eax + (ebx - eax) * t
                    sy = eay + (eby - eay) * t
                    ref = None
                    if (a0 is not None and a1 is not None
                            # Runway END edges are RESA / skirt
                            # territory (Pass C / D) — leave them be.
                            and not (rw_axis is not None
                                     and abs(out[0] * rw_axis[1][0]
                                             + out[1] * rw_axis[1][1])
                                     > _RING_END_NORMAL_DOT)
                            # Terrain-facing only: any already-emitted
                            # shape outward owns its own band.
                            and not prep_static.contains(Point(
                                sx + out[0] * _RING_PROBE_M,
                                sy + out[1] * _RING_PROBE_M))):
                        ref = a0 + t * (a1 - a0)
                    band = band_cap
                    if ref is not None and rw_axis is not None:
                        # Annex-14 strip: reach measured from the
                        # runway centerline, not the pavement edge.
                        try:
                            band = min(band_cap,
                                       rw_axis[2] - rw_axis[0].distance(
                                           Point(sx, sy)))
                        except _GEOM_EXC:
                            band = 0.0
                        if band <= _PAVEMENT_GAP_M + 1.0:
                            ref = None
                            band = band_cap
                    # Stations that face pavement / carry no altitude
                    # stay in the list with ``ref=None`` so run-grouping
                    # keeps true ring adjacency (dropping them would
                    # bridge distant obstructed runs into one strip).
                    stations.append((sx, sy))
                    st_alts.append(ref)
                    outs.append(out)
                    bws.append(band)
            if len(stations) >= 2:
                for ring, ralts in _build_graded_strips(
                        stations, st_alts, outs, bws, tx_slope,
                        threshold, step, sample_dem):
                    _collect(ring, ralts, out_role)

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
    # A graded rectangle off each runway end, symmetric about the extended
    # centreline and anchored at the OUTER pavement edge (the blast-pad /
    # stopway end, found by marching the centreline out through the
    # pavement union).  Width ≥ runway width / graded-strip width; the
    # surface is a gentle ramp (≤ RESA_MAX_SLOPE) rising from the
    # pavement-end elevation, cutting terrain above it and daylighting
    # where it meets the DEM — so an undershoot/overrun meets a smooth
    # slope, not a wall.
    def _emit_resa(mid, outward, runway_width, full_len, seed, elev_fallback):
        """Build the RESA ramp off one runway end.  ``mid`` = the runway
        end point, ``outward`` = unit normal pointing AWAY from the runway,
        ``seed`` = a point on pavement to start the outer-edge march from."""
        nx, ny = outward
        # Anchor at the outer pavement edge along the extended centreline.
        start = _pavement_exit_along(prep_pav, seed[0], seed[1], nx, ny,
                                     _RESA_PAVEMENT_PROBE_MAX_M, step)
        p0 = (seed[0] + nx * start, seed[1] + ny * start)
        # Pavement-end elevation (just inside the outer edge), else fallback.
        ref = _pav_alt(airside, p0[0] - nx * 1.0, p0[1] - ny * 1.0)
        if ref is None and elev_fallback is not None:
            ref = elev_fallback()
        if ref is None:
            return
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

    if source_runways:
        # AUTHORITATIVE: anchor each end at the apt.dat row-100 centreline
        # endpoint + width, so the RESA position/size never depends on the
        # emitted runway segmentation (user 2026-05-31).
        for r in source_runways:
            try:
                ax, ay = _ll_to_m(r.lat_a, r.lon_a)
                bx, by = _ll_to_m(r.lat_b, r.lon_b)
            except _GEOM_EXC:
                continue
            dx, dy = bx - ax, by - ay
            full_len = math.hypot(dx, dy)
            width = float(getattr(r, "width_m", 0.0) or 0.0)
            if full_len < 1.0 or width <= 0.0:
                continue
            ux, uy = dx / full_len, dy / full_len
            for end_pt, outward in (((ax, ay), (-ux, -uy)),
                                    ((bx, by), (ux, uy))):
                seed = (end_pt[0] - outward[0] * _RESA_SEED_INSET_M,
                        end_pt[1] - outward[1] * _RESA_SEED_INSET_M)
                _emit_resa(end_pt, outward, width, full_len, seed,
                           lambda s=seed: _pav_alt(airside, s[0], s[1]))
    else:
        # FALLBACK: detect ends from the emitted runway rects.
        for s, a, b, full_len in _runway_end_edges(runway_shapes):
            outward = _outward_normal(s.polygon, a, b)
            if outward is None:
                continue
            mid = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
            info = _rect_long_short_edges(_open_coords(s.polygon))
            runway_width = (info[1] if info
                            else math.hypot(b[0] - a[0], b[1] - a[1]))
            _emit_resa(mid, outward, runway_width, full_len, mid,
                       lambda s=s, mid=mid:
                       _sample_runway_segment_elev(s, mid[0], mid[1]))

    # Resolve all collected strips into minimal geometry in one pass.
    n_emitted = _finalize()
    return n_emitted


# ──────────────────────────────────────────────────────────────────
# Pass D: runway-end down-slope SKIRT (inverse RESA)
# ──────────────────────────────────────────────────────────────────
# Corridor clearance around SURFACE roads/railways crossing the skirt
# footprint: half the class carriageway width plus this shoulder, each
# side — the skirt must not bury real infrastructure (user 2026-07-05).
_SKIRT_ROAD_SHOULDER_M = 1.0
# OSM railway ways carry no carriageway-width class; a single-track
# corridor with ballast shoulders.
_SKIRT_RAILWAY_CORRIDOR_M = 8.0


def _surface_road_corridors(layout, ll_to_m):
    """Union of SURFACE road / railway corridors near the airport, in
    the layout meter frame — the ground the runway-end skirt must not
    fill over.  One source for the Pass D emitter AND the
    ``verification`` reader (the validator exempts the same corridors
    the emitter leaves unfilled, or every road through a governed zone
    reads as a violation).

    Tunnel-tagged ways are EXCLUDED on purpose: filling over a tunnel
    is lawful and correct (the tunnel emitter owns its portals and
    trenches, and those shapes are already in ``layout.shapes``, which
    the skirt's static clip respects).  Returns ``None`` when the tile
    has no road caches or nothing is near.
    """
    try:
        from .bridges import (
            _carriageway_width_for, _load_tunnel_road_network)
        nodes_r, ways_r, _big_way_ids = _load_tunnel_road_network(layout)
    except _GEOM_EXC:
        return None
    if not ways_r:
        return None
    corridors = []
    for _wid, node_refs, tags in ways_r:
        highway_type = tags.get("highway")
        railway_type = tags.get("railway")
        if highway_type is None and railway_type is None:
            continue
        tunnel_tag = tags.get("tunnel", "no")
        if tunnel_tag not in ("", "no"):
            continue
        points = []
        for node_ref in node_refs:
            ll = nodes_r.get(node_ref)
            if ll is not None:
                points.append(ll_to_m(ll[0], ll[1]))
        if len(points) < 2:
            continue
        if railway_type is not None:
            width = _SKIRT_RAILWAY_CORRIDOR_M
        else:
            width = _carriageway_width_for(highway_type, 6.0)
        try:
            corridors.append(
                LineString(points).buffer(
                    0.5 * width + _SKIRT_ROAD_SHOULDER_M))
        except _GEOM_EXC:
            continue
    if not corridors:
        return None
    try:
        return unary_union(corridors)
    except _GEOM_EXC:
        return None


# Emitted-shape roles that mark a runway end as constrained the same
# way a mapped road does (the perimeter road may reach the layout as a
# service_road / groundside shape rather than an OSM way).
_SKIRT_CONSTRAINT_ROLES = frozenset({
    "service_road", "service_junction", "groundside_pavement",
    "tunnel_ramp",
})


def _end_constraint_block(layout, ll_to_m):
    """Geometry whose presence in a runway end zone marks the end as
    NON-STANDARD (EMAS inference, user ruling 2026-07-05): surface
    road / railway corridors (big + small OSM caches — service roads
    included), OSM WATER polygons, and emitted road-like infrastructure
    shapes.  One source for the Pass D emitter AND the ``verification``
    reader.  ``None`` when nothing is available."""
    parts = []
    road_block = _surface_road_corridors(layout, ll_to_m)
    if road_block is not None and not road_block.is_empty:
        parts.append(road_block)
    # OSM water polygons (closed natural=water / riverbank ways from
    # the tile ``water`` cache; multipolygon relations are not carried
    # by the layer loader — acceptable, the primary constraint class is
    # the pond/river beside the overrun).
    try:
        from .osm_load import _load_osm_road_layer
        nodes_w, ways_w = _load_osm_road_layer(
            "water", layout.anchor[0], layout.anchor[1])
    except _GEOM_EXC:
        nodes_w, ways_w = {}, []
    for _wid, node_refs, _tags in ways_w:
        if len(node_refs) < 4 or node_refs[0] != node_refs[-1]:
            continue
        points = []
        for node_ref in node_refs[:-1]:
            ll = nodes_w.get(node_ref)
            if ll is not None:
                points.append(ll_to_m(ll[0], ll[1]))
        if len(points) < 3:
            continue
        try:
            poly = Polygon(points)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty:
                parts.append(poly)
        except _GEOM_EXC:
            continue
    for s in layout.shapes:
        if (s.role in _SKIRT_CONSTRAINT_ROLES
                and s.polygon is not None and not s.polygon.is_empty):
            parts.append(s.polygon)
    if not parts:
        return None
    try:
        return unary_union(parts)
    except _GEOM_EXC:
        return None


def _end_constraint_distance(p0, outward, max_distance_m,
                             constraint_block) -> float | None:
    """Distance from the pavement exit ``p0`` along ``outward`` to the
    FIRST constraint crossing of the extended centerline, or ``None``
    when nothing constrains within ``max_distance_m``."""
    if constraint_block is None or constraint_block.is_empty:
        return None
    nx, ny = outward
    try:
        ray = LineString([
            (p0[0], p0[1]),
            (p0[0] + nx * max_distance_m, p0[1] + ny * max_distance_m)])
        crossing = ray.intersection(constraint_block)
    except _GEOM_EXC:
        return None
    if crossing.is_empty:
        return None
    nearest = None
    geoms = (list(crossing.geoms)
             if hasattr(crossing, "geoms") else [crossing])
    for geom in geoms:
        for x, y in getattr(geom, "coords", []):
            t = (x - p0[0]) * nx + (y - p0[1]) * ny
            if t >= 0.0 and (nearest is None or t < nearest):
                nearest = t
    return nearest


def emit_runway_end_skirts(layout: PavementLayout, dem,
                           tile_lat: int, tile_lon: int,
                           source_runways=None) -> int:
    """Emit the runway-end down-slope skirts (gate ``O4_RUNWAY_END_SKIRT``).
    Mutates ``layout.shapes``; returns the number of skirt shapes emitted.

    The exact mirror of the Pass C RESA cut: where Pass C cuts terrain
    that RISES above the 5 % up-ramp, the skirt fills terrain that DROPS
    away beyond the end, so a runway ending at a hillside brow meets a
    lawful ≤3 %/≤5 % descent instead of a cliff at the pavement edge
    (FAA AC 150/5300-13B §3.16.5 / ICAO Annex 14 §4.7 — the law lives in
    ``grade_law``; plan: docs/runway_end_skirt_plan.md).

    Called SEPARATELY from (and AFTER) ``emit_surface_clearance_cuts``,
    once ``final_grade_projection`` has settled the pavement profile:
    the skirt bakes the law floor from the pavement-end elevation and
    entry grade, and the final projection may move pad/junction
    altitudes at a blast-pad end AFTER the cuts are emitted (KCLT 18L
    rose 0.4 m, leaving an emit-time skirt too short under the settled
    floor).  Emitting here also means the static clip below sees every
    earlier shape — cuts, boundary ribbon, groundside, tunnels.

    The skirt is emitted as ABUTTING BANDS split at the law's grade
    breakpoints: the floor is piecewise quadratic and a two-row
    ``node_altitudes`` ring renders as a ruled chord that would sag
    metres below it mid-span; per-band the sagitta is ≤ rate·L²/8
    (≈ 0.31 m).  Bands are clipped against the static geometry and
    previously-emitted skirt pieces (crossing-runway ends; first wins),
    with per-vertex altitudes recomputed ANALYTICALLY from the outward
    projection so clipping can introduce vertices freely.
    """
    if not RUNWAY_END_SKIRT_ENABLED or dem is None:
        return 0
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH
    step = CLEARANCE_STATION_STEP_M
    trigger = CLEARANCE_OBSTRUCTION_THRESHOLD_M["runway"]

    def _ll_to_m(lat: float, lon: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * R * cos0,
                math.radians(lat - lat0) * R)

    def sample_dem(x: float, y: float) -> float | None:
        try:
            lat = lat0 + math.degrees(y / R)
            lon = lon0 + math.degrees(x / (R * cos0))
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None

    airside = [s for s in layout.shapes
               if s.role in _AIRSIDE_PAVEMENT_ROLES
               and s.polygon is not None and not s.polygon.is_empty]
    if not airside:
        return 0
    try:
        prep_pav = prep(unary_union([s.polygon for s in airside]))
    except _GEOM_EXC:
        return 0
    # Every existing shape (pavement, cuts, ribbon, groundside, …),
    # buffered so skirt vertices stay clear of their edges.
    static_block = None
    try:
        static_block = unary_union(
            [s.polygon for s in layout.shapes
             if s.polygon is not None and not s.polygon.is_empty]
        ).buffer(_PAVEMENT_GAP_M)
    except _GEOM_EXC:
        static_block = None

    # Each collected strip carries its own analytic altitude function
    # (``alt_at(x, y)``), so clipping in the finalize below can
    # introduce vertices freely: END strips descend from one reference
    # along the outward axis; FLANK strips (alongside the blast pad /
    # stopway between the runway end and the pavement exit) descend
    # from a per-station pavement-EDGE altitude along the side normal.
    # Constraint geometry for the EMAS inference (roads / water /
    # emitted road-like infrastructure) — built once, shared by every
    # end (and identically by the verification reader).
    constraint_block = _end_constraint_block(layout, _ll_to_m)

    skirt_strips: list[tuple] = []

    def _floor_depth_for(entry_grade: float):
        depth_cache: dict[float, float] = {}

        def _floor_depth(distance_m: float) -> float:
            depth = depth_cache.get(distance_m)
            if depth is None:
                depth = runway_end_skirt_floor_profile(
                    [distance_m], entry_grade)[0]
                depth_cache[distance_m] = depth
            return depth
        return _floor_depth

    def _emit_one_end(outward, runway_width, full_len, seed,
                      elev_fallback, approach_class):
        """Collect the down-slope skirt bands off one runway end.  Same
        anchor geometry as Pass C's ``_emit_resa``; the governed length
        scales with the end's approach class (better approaches earn a
        longer smoothed apron of terrain)."""
        nx, ny = outward
        start = _pavement_exit_along(prep_pav, seed[0], seed[1], nx, ny,
                                     _RESA_PAVEMENT_PROBE_MAX_M, step)
        p0 = (seed[0] + nx * start, seed[1] + ny * start)
        # Containment-free reads (``_nearest_pav_alt``): the skirt's law
        # floor and the verification reader MUST measure the same end
        # elevation and entry grade — a containment miss on a hairline
        # gap would silently flatten one side's entry grade.
        ref = _nearest_pav_alt(airside, p0[0] - nx * 1.0, p0[1] - ny * 1.0)
        if ref is None and elev_fallback is not None:
            ref = elev_fallback()
        if ref is None:
            return
        # The runway's own end grade (signed, positive = climbing toward
        # the end), sampled over a short window inside the pavement.
        inside = _nearest_pav_alt(
            airside,
            p0[0] - nx * (1.0 + _SKIRT_END_GRADE_WINDOW_M),
            p0[1] - ny * (1.0 + _SKIRT_END_GRADE_WINDOW_M))
        entry_grade = 0.0
        if inside is not None:
            entry_grade = (float(ref) - float(inside)) \
                / _SKIRT_END_GRADE_WINDOW_M
            entry_grade = max(-0.05, min(0.05, entry_grade))
        governed = runway_end_governed_length_m(full_len, approach_class)
        # EMAS inference (user 2026-07-05): a road / service road /
        # water crossing the end zone marks a NON-standard end — the
        # skirt stops short of the first constraint (or vanishes when
        # the constraint sits at the pavement end, KCLT 18L).
        governed = runway_end_constrained_length_m(
            governed,
            _end_constraint_distance(
                p0, outward, governed, constraint_block))
        _floor_depth = _floor_depth_for(entry_grade)

        half = max(runway_width, runway_strip_half_width_m(full_len))
        perp = (-ny, nx)
        ea = (p0[0] - perp[0] * half, p0[1] - perp[1] * half)
        eb = (p0[0] + perp[0] * half, p0[1] + perp[1] * half)
        stations = _stations(ea, eb, step)
        m = len(stations)
        band_edges = runway_end_skirt_profile_breakpoints(entry_grade)

        def _end_alt_at(vx, vy, p0=p0, nx=nx, ny=ny,
                        ref=float(ref), floor_depth=_floor_depth,
                        cap=governed, sample_dem=sample_dem):
            d = (vx - p0[0]) * nx + (vy - p0[1]) * ny
            floor = ref - floor_depth(max(_PAVEMENT_GAP_M, min(cap, d)))
            # Lift-only, exactly as _build_filled_skirts' ring altitudes
            # (clip-introduced vertices ride the DEM where it is above
            # the analytic floor rather than cutting it down).
            return _skirt_lift_alt(floor, sample_dem(vx, vy))

        for ring, _ralts in _build_filled_skirts(
                stations, [ref] * m, [outward] * m, [governed] * m,
                _floor_depth, band_edges, trigger, step, sample_dem):
            skirt_strips.append((ring, _end_alt_at))

        if os.environ.get("O4_SKIRT_DEBUG") == "1":
            print(f"  [skirt-debug] end at ({seed[0]:.1f},{seed[1]:.1f}) "
                  f"outward ({nx:.3f},{ny:.3f}) start={start:.1f} "
                  f"ref={ref} entry={entry_grade:.4f} "
                  f"governed={governed} strips_so_far={len(skirt_strips)}")

        # ── Blast-pad / stopway FLANK wrap (user 2026-07-05) ──
        # The end strip governs beyond the pavement exit; the FLANKS of
        # the overrun pavement between the runway end point and that
        # exit sit inside the same governed end zone, and a lateral drop
        # there is the same violation.  Fill strips hug the pavement
        # side edges out to the end-zone corridor (± ``half``), with a
        # flat-entry law floor (lateral cross-grades are small and a
        # climbing entry clamps to flat anyway).
        if start < 2.0 * step:
            return   # no meaningful overrun pavement — nothing to wrap
        flank_floor_depth = _floor_depth_for(0.0)
        flank_band_edges = runway_end_skirt_profile_breakpoints(0.0)
        n_axis = max(2, int(math.floor(start / step)) + 1)
        axis_distances = [min(start, float(k) * step)
                          for k in range(n_axis)]
        for side in (perp, (-perp[0], -perp[1])):
            sxn, syn = side
            edge_stations, edge_alts, edge_offsets, caps = [], [], [], []
            axis_kept = []
            for t in axis_distances:
                cx, cy = seed[0] + nx * t, seed[1] + ny * t
                lateral_exit = _pavement_exit_along(
                    prep_pav, cx, cy, sxn, syn, half, step)
                if lateral_exit >= half - 2.0 * _PAVEMENT_GAP_M:
                    continue   # pavement fills the corridor here
                ex, ey = cx + sxn * lateral_exit, cy + syn * lateral_exit
                edge_alt = _nearest_pav_alt(
                    airside, ex - sxn * 1.0, ey - syn * 1.0)
                if edge_alt is None:
                    continue
                edge_stations.append((ex, ey))
                edge_alts.append(float(edge_alt))
                edge_offsets.append(lateral_exit)
                caps.append(half - lateral_exit)
                axis_kept.append(t)
            if len(edge_stations) < 2:
                continue

            def _flank_alt_at(vx, vy, seed=seed, nx=nx, ny=ny,
                              sxn=sxn, syn=syn,
                              axis_kept=axis_kept,
                              edge_offsets=edge_offsets,
                              edge_alts=edge_alts,
                              floor_depth=flank_floor_depth,
                              half=half, sample_dem=sample_dem):
                # Along-axis position → interpolate the pavement edge
                # offset and altitude between the two nearest stations.
                s = (vx - seed[0]) * nx + (vy - seed[1]) * ny
                s = max(axis_kept[0], min(axis_kept[-1], s))
                j = 1
                while j < len(axis_kept) - 1 and axis_kept[j] < s:
                    j += 1
                span = axis_kept[j] - axis_kept[j - 1]
                w = 0.0 if span <= 0.0 else (s - axis_kept[j - 1]) / span
                offset = (edge_offsets[j - 1]
                          + (edge_offsets[j] - edge_offsets[j - 1]) * w)
                edge_alt = (edge_alts[j - 1]
                            + (edge_alts[j] - edge_alts[j - 1]) * w)
                lateral = (vx - seed[0]) * sxn + (vy - seed[1]) * syn
                d = max(_PAVEMENT_GAP_M,
                        min(half - offset, lateral - offset))
                # Lift-only (see _skirt_lift_alt): a flank vertex on a
                # bump above the local floor rides the DEM, not a cut.
                return _skirt_lift_alt(
                    edge_alt - floor_depth(d), sample_dem(vx, vy))

            flank_rings = _build_filled_skirts(
                edge_stations, edge_alts, [side] * len(edge_stations),
                caps, flank_floor_depth, flank_band_edges,
                trigger, step, sample_dem)
            if os.environ.get("O4_SKIRT_DEBUG") == "1":
                print(f"  [skirt-debug]   flank side ({sxn:.3f},{syn:.3f})"
                      f" stations={len(edge_stations)} "
                      f"caps={min(caps):.1f}..{max(caps):.1f} "
                      f"rings={len(flank_rings)}")
                for ring, _ralts in flank_rings:
                    ts = [(vx - seed[0]) * nx + (vy - seed[1]) * ny
                          for vx, vy in ring]
                    ls = [(vx - seed[0]) * sxn + (vy - seed[1]) * syn
                          for vx, vy in ring]
                    print(f"  [skirt-debug]     ring t "
                          f"{min(ts):.1f}..{max(ts):.1f} lateral "
                          f"{min(ls):.1f}..{max(ls):.1f}")
            for ring, _ralts in flank_rings:
                skirt_strips.append((ring, _flank_alt_at))

    if source_runways:
        # AUTHORITATIVE: anchor each end at the apt.dat row-100
        # centreline endpoint + width (as Pass C does).
        for r in source_runways:
            try:
                ax, ay = _ll_to_m(r.lat_a, r.lon_a)
                bx, by = _ll_to_m(r.lat_b, r.lon_b)
            except _GEOM_EXC:
                continue
            dx, dy = bx - ax, by - ay
            full_len = math.hypot(dx, dy)
            width = float(getattr(r, "width_m", 0.0) or 0.0)
            if full_len < 1.0 or width <= 0.0:
                continue
            ux, uy = dx / full_len, dy / full_len
            end_metadata = (
                (getattr(r, "markings_a", 0),
                 getattr(r, "approach_lights_a", 0)),
                (getattr(r, "markings_b", 0),
                 getattr(r, "approach_lights_b", 0)),
            )
            for (end_pt, outward), (markings, lights) in zip(
                    (((ax, ay), (-ux, -uy)), ((bx, by), (ux, uy))),
                    end_metadata):
                seed = (end_pt[0] - outward[0] * _RESA_SEED_INSET_M,
                        end_pt[1] - outward[1] * _RESA_SEED_INSET_M)
                _emit_one_end(
                    outward, width, full_len, seed,
                    lambda s=seed: _pav_alt(airside, s[0], s[1]),
                    runway_end_approach_class(markings, lights))
    else:
        # FALLBACK: detect ends from the emitted runway rects.  No
        # apt.dat metadata on this path — the blank-data ladder
        # classifies the end non_precision (errs long).
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
        for s, a, b, full_len in _runway_end_edges(runway_shapes):
            outward = _outward_normal(s.polygon, a, b)
            if outward is None:
                continue
            mid = (0.5 * (a[0] + b[0]), 0.5 * (a[1] + b[1]))
            info = _rect_long_short_edges(_open_coords(s.polygon))
            runway_width = (info[1] if info
                            else math.hypot(b[0] - a[0], b[1] - a[1]))
            _emit_one_end(
                outward, runway_width, full_len, mid,
                lambda s=s, mid=mid:
                _sample_runway_segment_elev(s, mid[0], mid[1]),
                runway_end_approach_class(0, 0))

    if not skirt_strips:
        return 0
    boundary = layout.airport_boundary
    # SURFACE roads / railways crossing the governed zones stay at
    # their own grade — the skirt clears their corridors instead of
    # burying them (the validator exempts the same corridors, via the
    # shared ``_surface_road_corridors``).
    road_block = _surface_road_corridors(layout, _ll_to_m)
    emitted_fill = None
    n = 0
    skirt_debug = os.environ.get("O4_SKIRT_DEBUG") == "1"
    # Lab forensics (O4_SKIRT_DEBUG_DUMP=<path>): record every raw
    # strip ring, every emitted piece and every DROPPED piece from THIS
    # in-pipeline run, so sliver analysis overlays like-for-like
    # geometry (post-hoc probe rebuilds have shown per-process
    # divergence in pavement-exit anchoring — never compare across
    # builds).
    skirt_dump_path = os.environ.get("O4_SKIRT_DEBUG_DUMP")
    skirt_dump = None
    if skirt_dump_path:
        skirt_dump = {"anchor": list(layout.anchor),
                      "strips": [], "pieces": [], "dropped": []}
    for strip_index, (ring, alt_at) in enumerate(skirt_strips):
        if skirt_dump is not None:
            skirt_dump["strips"].append(
                {"index": strip_index,
                 "ring": [[float(x), float(y)] for x, y in ring]})
        try:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            stage_areas = [("raw", poly.area)]
            for name, block in (("static", static_block),
                                ("road", road_block),
                                ("prior_fill", emitted_fill)):
                if block is not None and not block.is_empty:
                    poly = poly.difference(block)
                    stage_areas.append((name, poly.area))
            if boundary is not None and not boundary.is_empty:
                # No emitted shape may cross the airport boundary (the
                # post-clearance boundary clip has already run by now).
                poly = poly.intersection(boundary)
                stage_areas.append(("boundary", poly.area))
            if skirt_debug and stage_areas[-1][1] < 0.98 * stage_areas[0][1]:
                print(f"  [skirt-debug] strip {strip_index} clip: "
                      + " -> ".join(f"{nm} {ar:.0f}"
                                    for nm, ar in stage_areas))
            if poly.is_empty:
                continue
        except _GEOM_EXC:
            continue
        if poly.geom_type == "Polygon":
            components = [poly]
        elif poly.geom_type in ("MultiPolygon", "GeometryCollection"):
            components = [g for g in poly.geoms
                          if g.geom_type == "Polygon"]
        else:
            continue
        for comp in components:
            for simple in _decompose_polygon_with_holes(
                    comp, min_area_m2=1.0):
                if simple.is_empty:
                    continue
                if simple.area < _MIN_CUT_AREA_M2:
                    # The min-area gate exists to reject freestanding
                    # confetti — but a small fragment ATTACHED to the
                    # pavement or existing geometry is a legitimate
                    # corner patch of a continuous fill (the pad-corner
                    # wedges at KCLT are 9–21 m² and dropping them left
                    # validator-visible notches).  Keep attached
                    # fragments; drop isolated ones.
                    attached = False
                    if simple.area >= 1.0 and static_block is not None:
                        try:
                            attached = simple.distance(static_block) <= 1.0
                        except _GEOM_EXC:
                            attached = False
                    if not attached:
                        if skirt_dump is not None:
                            skirt_dump["dropped"].append(
                                {"strip": strip_index,
                                 "area": float(simple.area),
                                 "ring": [[float(x), float(y)] for x, y
                                          in _open_coords(simple)]})
                        continue
                piece_ring = _open_coords(simple)
                if len(piece_ring) < 3:
                    continue
                alts = [round(float(alt_at(vx, vy)), 1)
                        for vx, vy in piece_ring]
                layout.shapes.append(BuiltShape(
                    polygon=simple, role=ROLE_RUNWAY_CLEARANCE,
                    ref="runway_end_skirt",
                    node_altitudes=alts + [alts[0]]))
                if skirt_dump is not None:
                    skirt_dump["pieces"].append(
                        {"strip": strip_index,
                         "area": float(simple.area),
                         "ring": [[float(x), float(y)]
                                  for x, y in piece_ring]})
                try:
                    emitted_fill = (
                        simple if emitted_fill is None
                        else unary_union([emitted_fill, simple]))
                except _GEOM_EXC:
                    pass
                n += 1
    if skirt_dump is not None:
        import json
        try:
            with open(skirt_dump_path, "w") as handle:
                json.dump(skirt_dump, handle)
        except OSError:
            pass
    return n
