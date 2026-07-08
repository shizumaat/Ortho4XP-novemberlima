"""Adjacent-ground grade law — the LATERAL banded emitter (slice 3).

The lateral generalization of the runway-END skirt: ground beside a
paved surface is a two-zone-plus-ungraded CORRIDOR off the pavement
EDGE (``grade_law.adjacent_ground_envelope``, single law source).  This
module MARCHES that corridor outward from every airside pavement edge
that faces unpaved ground and emits ``graded_strip`` surface polygons
wherever the smoothed DEM sits OUTSIDE the corridor:

  * DEM ABOVE the ceiling  → CUT down to the ceiling (rising terrain;
    under the enforce-fully mandate the zone-1/2 ceiling is BELOW the
    edge, so a flat surround is excavated to the lawful drainage slope).
  * DEM BELOW the floor     → FILL up to the floor (falling terrain,
    only inside zones 1-2 where the floor is finite — zone 3's floor is
    ``None``, so a cliff beyond the graded band renders as DEM: the
    boundary-bridge killer).
  * DEM INSIDE the corridor → emit NOTHING (the terrain is lawful).

The machinery is the runway-end skirt's banded emission
(``clearance._build_filled_skirts`` is REUSED verbatim for the fill
direction; the cut direction is the small mirror ``_build_cut_bands``
below — inline-duplicated because the skirt's cut twin
``_build_graded_strips`` takes a single linear slope, not the corridor's
piecewise-continuous ceiling; flagged for the cleanup slice).  Bands are
split at the law's zone breakpoints, carry per-band ``node_altitudes``,
and are clipped against every existing shape (buffered by the pavement
gap) + the airport boundary, exactly as the skirt clips.

Runway ENDS are OUT OF SCOPE (the runway-end skirt law owns them, and
the skirt shapes are already in the static block, so the cut/fill bands
clip against them and never double-write); runway-END ring edges are
skipped by the outward-normal test the ring-edge sweep uses.

Behind ``config.ADJACENT_GROUND_LAW_ENABLED`` (env
``O4_ADJACENT_GROUND_LAW``, default off).  With the gate off this module
is never imported (the pipeline import is inside the gated block), so it
is byte-inert.
"""
from __future__ import annotations

import bisect
import math

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

import O4_UI_Utils as UI

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

from .config import (
    ADJACENT_GROUND_LIP_WIDTH_M,
    APRON_BEYOND_SHOULDER_MAX_DOWN_SLOPE,
    APRON_EDGE_WALL_MIN_DROP_M,
    APRON_SHOULDER_WIDTH_M,
    CLEARANCE_MAX_REACH_M,
    CLEARANCE_OBSTRUCTION_THRESHOLD_M,
    CLEARANCE_STATION_STEP_M,
    RUNWAY_STRIP_HALF_WIDTH_BY_CODE,
    runway_code_number,
    taxiway_strip_graded_half_width_for_letter,
)
from .grade_law import adjacent_ground_envelope
from .layout import (
    BuiltShape,
    PavementLayout,
    R_EARTH,
    ROLE_APRON,
    ROLE_CROSS_CONNECTOR,
    ROLE_GRADED_STRIP,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RETAINING_WALL,
    ROLE_RUNWAY,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL,
    ROLE_STUB,
    taxi_shape_code_letter,
)
from .elevation import _sample_dem
from .pavement.junctions import _decompose_polygon_with_holes
from .pavement.runways import _sample_runway_segment_elev
# READ-ONLY reuse of the runway-end skirt machinery (clearance.py).  The
# fill direction is IDENTICAL to the skirt's, so its builder + shared
# constants are imported rather than re-implemented (the plan: reuse the
# skirt's patterns; inline-duplicate only what genuinely differs).
from .clearance import (
    _build_filled_skirts,
    _declaw_alt_needles,
    _open_coords,
    _PAVEMENT_GAP_M,
    _RING_END_NORMAL_DOT,
    _RING_PROBE_M,
    _unit,
)

# An adjacent-ground band is a SHALLOW corridor surface (a code-4 runway
# fill falls ≈2.3 m over 75 m, monotonically across many vertices), so a
# single-vertex altitude reversal larger than this is always a
# clip-introduced resample flip on a concave ring, not a real feature —
# ``_declaw_alt_needles`` clamps it to the neighbour mean.  Tighter than
# the runway-end skirt's 3 m needle tolerance because the lateral bands
# are shallower than an end skirt.
_ADJACENT_NEEDLE_TOL_M = 1.5

__all__ = ["emit_adjacent_ground_bands"]

# The emitted role's OSM ref (a terrain-grading overlay, NOT pavement).
_ADJACENT_REF = "adjacent_ground"
_ADJACENT_WALL_REF = "adjacent_ground_wall"
# Minimum emitted band area (m²); smaller freestanding residue is noise.
_MIN_BAND_AREA_M2 = 25.0

# Role → strip family (mirrors grade_law's ``_ADJACENT_*_ROLES`` so the
# emitter and the law agree on which corridor each surface takes).
_RUNWAY_ROLES = (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)
_TAXIWAY_ROLES = (
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
)
_APRON_ROLES = (ROLE_APRON,)


def _build_cut_bands(edge_stations, edge_alts, outwards, band_caps,
                     ceiling_offset, band_edges, trigger, step,
                     sample_dem):
    """CUT-direction mirror of ``clearance._build_filled_skirts``.

    At each station a CEILING sits at ``edge_alt + ceiling_offset(d)``
    (the corridor's upper bound, a piecewise-CONTINUOUS function of the
    lateral distance ``d`` — negative in the graded zones under the
    enforce-fully mandate, rising in the ungraded zone).  Terrain ABOVE
    the ceiling by more than ``trigger`` is cut down to it; the cut
    DAYLIGHTS where the ceiling meets the DEM, capped at ``band_caps[i]``
    (the family reach).  Cut-only — flat-or-below terrain is left to the
    fill twin.

    Emitted as ABUTTING BANDS split at ``band_edges`` (the law's zone
    breakpoints), so within a band the ceiling is one LINEAR piece and a
    two-row ring renders it exactly (no chord sag across a zone kink).
    Adjacent bands share their boundary row (same positions, same rounded
    altitudes) → one continuous surface.  Returns ``(ring_open,
    alts_open)`` pairs.
    """
    n = len(edge_stations)
    outer: list[float] = [0.0] * n
    obstructed: list[bool] = [False] * n
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
            co = ceiling_offset(d)
            if co is None:      # unbounded up at/beyond the reach — no cut
                continue
            ceil = ref + co
            dd = sample_dem(sx + nx * d, sy + ny * d)
            if dd is not None and dd > ceil + trigger:
                last = d
        if last > 0.0:
            obstructed[i] = True
            outer[i] = min(cap, last + step)
    if not any(obstructed):
        return []
    edges = [_PAVEMENT_GAP_M]
    for b in sorted(band_edges):
        if _PAVEMENT_GAP_M + 1.0 < b < cap_max - 1.0:
            edges.append(float(b))
    edges.append(cap_max)

    out: list[tuple[list, list]] = []
    for b in range(len(edges) - 1):
        d0, d1 = edges[b], edges[b + 1]
        idx = [i for i in range(n) if obstructed[i] and outer[i] > d0]
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
                    off = step
                else:
                    continue
                # The ceiling is unbounded exactly AT the reach; keep the
                # outer row a hair inside so it stays finite.
                off = min(off, cap_max - 1e-3)
                if off <= d0:
                    continue
                co0 = ceiling_offset(d0)
                co1 = ceiling_offset(off)
                if co0 is None or co1 is None:
                    continue
                sx, sy = edge_stations[i]
                ix, iy = sx + nx * d0, sy + ny * d0
                inner_alts.append(round(float(ref + co0), 1))
                inner_pts.append((ix, iy))
                ox, oy = sx + nx * off, sy + ny * off
                outer_pts.append((ox, oy))
                outer_alts.append(round(float(ref + co1), 1))
            if len(inner_pts) < 2:
                continue
            ring = inner_pts + outer_pts[::-1]
            alts = inner_alts + outer_alts[::-1]
            out.append((ring, alts))
    return out


def _make_edge_projection_resampler(coords, ring_alts, ceil_off,
                                    floor_depth, reach, sample_dem):
    """Return ``resample(x, y, kind) -> alt`` for band vertices of one
    shape.  A vertex's altitude is the pavement-EDGE elevation at its
    nearest ring point plus the corridor offset at its true lateral
    distance ``d`` (shapely projection).  ``kind`` = "cut" rides the
    ceiling; "fill" rides ``max(floor, DEM)`` (the skirt lift convention).

    The edge elevation is read by linear-referencing the query's foot
    along the ring against the per-vertex ``ring_alts`` (``None`` entries
    — pavement-facing / unsampled vertices — filled from their nearest
    known neighbour so a foot landing there still resolves).
    """
    pts = list(coords)
    if pts and pts[0] == pts[-1]:
        pass  # keep closed for a continuous LineString
    line = LineString(pts)
    # Cumulative arc length at each coord + a value-filled alt array.
    cum = [0.0]
    for i in range(len(pts) - 1):
        cum.append(cum[-1] + math.hypot(pts[i + 1][0] - pts[i][0],
                                        pts[i + 1][1] - pts[i][1]))
    alt = [ring_alts[i] if i < len(ring_alts) else None
           for i in range(len(pts))]
    # Forward- then back-fill None so every arc position resolves.
    last = None
    for i in range(len(alt)):
        if alt[i] is not None:
            last = alt[i]
        elif last is not None:
            alt[i] = last
    nxt = None
    for i in range(len(alt) - 1, -1, -1):
        if alt[i] is not None:
            nxt = alt[i]
        elif nxt is not None:
            alt[i] = nxt

    def _edge_alt_at(s):
        # Locate the ring segment containing arc length s.
        k = bisect.bisect_right(cum, s) - 1
        k = max(0, min(k, len(pts) - 2))
        seg = cum[k + 1] - cum[k]
        t = 0.0 if seg <= 0 else (s - cum[k]) / seg
        a0, a1 = alt[k], alt[k + 1]
        if a0 is None or a1 is None:
            return a0 if a0 is not None else a1
        return a0 + t * (a1 - a0)

    def resample(x, y, kind):
        p = Point(x, y)
        s = line.project(p)
        edge_alt = _edge_alt_at(s)
        if edge_alt is None:
            return 0.0
        d = max(_PAVEMENT_GAP_M, p.distance(line))
        if kind == "cut":
            co = ceil_off(min(d, reach - 1e-3))
            return round(float(edge_alt + (co if co is not None else 0.0)), 1)
        floor = float(edge_alt) - floor_depth(d)   # _fd clamps to width
        dd = sample_dem(x, y)
        return round(floor if dd is None else max(floor, float(dd)), 1)

    return resample


def _family_params(layout, shape, rw_axes):
    """Resolve ``(family, code_number, code_letter, reach, width, axis)``
    for one airside ``shape``; ``None`` if the shape is out of scope.

    ``axis`` is the nearest runway-axis unit vector (runway shapes only)
    used to skip END ring edges; ``width`` is the graded-band half-width
    (fill cap)."""
    role = shape.role
    if role in _RUNWAY_ROLES:
        if not rw_axes:
            return None
        try:
            cen = shape.polygon.centroid
            axis = min(rw_axes, key=lambda a: a[0].distance(cen))
        except (_GEOM_EXC + (ValueError,)):
            return None
        code_number = runway_code_number(axis[2])
        width = RUNWAY_STRIP_HALF_WIDTH_BY_CODE[code_number]
        return ("runway", code_number, None,
                CLEARANCE_MAX_REACH_M["runway"], width, axis[1])
    if role in _TAXIWAY_ROLES:
        letter = taxi_shape_code_letter(layout, shape)
        width = taxiway_strip_graded_half_width_for_letter(letter)
        return ("taxiway", None, letter,
                CLEARANCE_MAX_REACH_M["taxiway"], width, None)
    if role in _APRON_ROLES:
        return ("apron", None, None,
                CLEARANCE_MAX_REACH_M["taxiway"], APRON_SHOULDER_WIDTH_M,
                None)
    return None


def _nearest_alt(points, alts, x, y):
    """Altitude of the nearest ``(points, alts)`` sample to ``(x, y)`` —
    the resampler for clip-introduced band vertices.  Bands are stationed
    at ``step`` (5 m) and adjacent bands share boundary rows, so the
    nearest source vertex reproduces the graded surface to within a
    fraction of the fill trigger and never tears at a band seam."""
    best_d = None
    best_a = 0.0
    for (px, py), a in zip(points, alts):
        dd = (px - x) * (px - x) + (py - y) * (py - y)
        if best_d is None or dd < best_d:
            best_d = dd
            best_a = a
    return best_a


def emit_adjacent_ground_bands(layout: PavementLayout, dem,
                               tile_lat: int, tile_lon: int,
                               source_runways=None) -> int:
    """Emit the adjacent-ground graded bands (gate
    ``ADJACENT_GROUND_LAW_ENABLED``).  Mutates ``layout.shapes``; returns
    the number of ``graded_strip`` / ``retaining_wall`` shapes emitted.

    Called as one of the LAST emissions (after the runway-end skirts, so
    the skirt geometry is in the static block and the bands clip against
    it at runway ends).  The bands bake the corridor from edge-
    interpolated pavement reads (the same containment-free reads the
    skirt uses), so nothing later re-solves them.
    """
    if dem is None:
        return 0
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH
    step = CLEARANCE_STATION_STEP_M

    def _ll_to_m(lat: float, lon: float) -> tuple[float, float]:
        return (math.radians(lon - lon0) * R * cos0,
                math.radians(lat - lat0) * R)

    def sample_dem(x: float, y: float):
        try:
            lat = lat0 + math.degrees(y / R)
            lon = lon0 + math.degrees(x / (R * cos0))
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None

    in_scope = _RUNWAY_ROLES + _TAXIWAY_ROLES + _APRON_ROLES
    scoped = [s for s in layout.shapes
              if s.role in in_scope and s.polygon is not None
              and not s.polygon.is_empty
              and s.polygon.geom_type == "Polygon"]
    if not scoped:
        return 0

    # Static block: EVERY existing shape, buffered by the pavement gap.
    # A band vertex must stay this far outside any pavement / feature
    # edge (the runway-end skirt's own standoff), so no epsilon wedge is
    # minted against a constrained edge and the clip removes any overlap.
    static_union = None
    try:
        static_union = unary_union(
            [s.polygon for s in layout.shapes
             if s.polygon is not None and not s.polygon.is_empty])
    except _GEOM_EXC:
        static_union = None
    if static_union is None or static_union.is_empty:
        return 0
    try:
        prep_static = prep(static_union)
        static_block = static_union.buffer(_PAVEMENT_GAP_M)
    except _GEOM_EXC:
        return 0
    boundary = layout.airport_boundary

    # Row-100 runway axes (authoritative length + direction) for runway
    # code-number keying and END-edge skipping — as the ring-edge sweep.
    rw_axes: list[tuple] = []
    if source_runways:
        from shapely.geometry import LineString
        for r in source_runways:
            try:
                rax, ray = _ll_to_m(r.lat_a, r.lon_a)
                rbx, rby = _ll_to_m(r.lat_b, r.lon_b)
            except _GEOM_EXC:
                continue
            rlen = math.hypot(rbx - rax, rby - ray)
            if rlen < 1.0:
                continue
            rw_axes.append((LineString([(rax, ray), (rbx, rby)]),
                            ((rbx - rax) / rlen, (rby - ray) / rlen),
                            rlen))

    trigger_by_family = {
        "runway": CLEARANCE_OBSTRUCTION_THRESHOLD_M["runway"],
        "taxiway": CLEARANCE_OBSTRUCTION_THRESHOLD_M["taxiway"],
        "apron": CLEARANCE_OBSTRUCTION_THRESHOLD_M["taxiway"],
    }

    # Accumulate per-shape (ring, alts) bands + wall shapes, then clip +
    # emit once.
    emitted = 0
    emitted_union = None

    for s in scoped:
        params = _family_params(layout, s, rw_axes)
        if params is None:
            continue
        family, code_number, code_letter, reach, width, axis = params
        trigger = trigger_by_family[family]

        def ceil_off(d, _r=family, _cn=code_number, _cl=code_letter):
            return adjacent_ground_envelope(_r, _cn, _cl, d)[1]

        def floor_depth(d, _r=family, _cn=code_number, _cl=code_letter,
                        _w=width):
            # Fill only inside zones 1-2 (bounded by the graded width _w);
            # beyond it the floor is None (zone-3 cliff lawful — no fill).
            # The skirt builder's longitudinal TAPER can query one step
            # past the cap, and for aprons the 3 m shoulder is under one
            # station step, so clamp the query to the graded width — the
            # floor there is always finite and the fill geometry is still
            # bounded by the ``band_caps`` passed to the builder.
            f = adjacent_ground_envelope(_r, _cn, _cl, min(d, _w))[0]
            return None if f is None else -f

        # Per-CLOSED-ring node altitudes aligned with the ring coords (the
        # node_altitudes contract), else the shape's plane sampler.
        try:
            coords = list(s.polygon.exterior.coords)
            ccw = bool(s.polygon.exterior.is_ccw)
        except _GEOM_EXC:
            continue
        if len(coords) < 4:
            continue
        na = s.node_altitudes
        if na:
            nm = min(len(na), len(coords))
            ring_alts = [None if na[i] is None else float(na[i])
                         for i in range(nm)]
            ring_alts += [None] * (len(coords) - nm)
        elif s.altitude is not None:
            ring_alts = [float(s.altitude)] * len(coords)
        else:
            ring_alts = [_sample_runway_segment_elev(s, x, y)
                         for x, y in coords]

        stations, st_alts, outs = [], [], []
        for i in range(len(coords) - 1):
            eax, eay = coords[i]
            ebx, eby = coords[i + 1]
            u = _unit(ebx - eax, eby - eay)
            if u is None:
                continue
            out = (u[1], -u[0]) if ccw else (-u[1], u[0])
            a0 = ring_alts[i]
            a1 = ring_alts[i + 1]
            nseg = max(1, int(math.ceil(
                math.hypot(ebx - eax, eby - eay) / step)))
            for k in range(nseg):    # next edge owns the far corner
                t = k / nseg
                sx = eax + (ebx - eax) * t
                sy = eay + (eby - eay) * t
                ref = None
                if (a0 is not None and a1 is not None
                        # Runway END edges: the runway-end skirt law owns
                        # terrain beyond an end — skip axis-aligned edges.
                        and not (axis is not None
                                 and abs(out[0] * axis[0]
                                         + out[1] * axis[1])
                                 > _RING_END_NORMAL_DOT)
                        # Terrain-facing only: an outward point already
                        # covered by a shape owns its own band.
                        and not prep_static.contains(Point(
                            sx + out[0] * _RING_PROBE_M,
                            sy + out[1] * _RING_PROBE_M))):
                    ref = a0 + t * (a1 - a0)
                stations.append((sx, sy))
                st_alts.append(ref)
                outs.append(out)
        if len(stations) < 2:
            continue
        m = len(stations)

        # FILL (DEM below floor, zones 1-2): reuse the skirt builder
        # verbatim — floor_depth = -floor_offset, cap = graded width, band
        # split at the lip breakpoint.
        fill_bands = _build_filled_skirts(
            stations, st_alts, outs, [width] * m, floor_depth,
            {ADJACENT_GROUND_LIP_WIDTH_M}, trigger, step, sample_dem)
        # CUT (DEM above ceiling): the corridor's piecewise ceiling out to
        # the family reach, split at the lip + graded-width kinks.
        cut_bands = _build_cut_bands(
            stations, st_alts, outs, [reach] * m, ceil_off,
            {ADJACENT_GROUND_LIP_WIDTH_M, width}, trigger, step, sample_dem)
        if not fill_bands and not cut_bands:
            continue

        # Clip-vertex resampler.  Every emitted (possibly clipped) vertex
        # gets its altitude ANALYTICALLY from its TRUE lateral distance to
        # the pavement edge (shapely projection onto the shape's ring) plus
        # the corridor offset at that distance.  This is robust for both
        # edge and INTERIOR clip vertices and on concave rings (unlike a
        # nearest-station or nearest-band-vertex lookup, which mis-key an
        # interior clip vertex and spike it), so a clip introduces no step.
        resample_alt = _make_edge_projection_resampler(
            coords, ring_alts, ceil_off, floor_depth, reach, sample_dem)

        for kind, band_list in (("fill", fill_bands), ("cut", cut_bands)):
            for ring, _ralts in band_list:
                try:
                    poly = Polygon(ring)
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                    poly = poly.difference(static_block)
                    if (emitted_union is not None
                            and not emitted_union.is_empty):
                        poly = poly.difference(emitted_union)
                    if boundary is not None and not boundary.is_empty:
                        poly = poly.intersection(boundary)
                    if poly.is_empty:
                        continue
                except _GEOM_EXC:
                    continue
                if poly.geom_type == "Polygon":
                    comps = [poly]
                elif poly.geom_type in ("MultiPolygon",
                                        "GeometryCollection"):
                    comps = [g for g in poly.geoms
                             if g.geom_type == "Polygon"]
                else:
                    continue
                for comp in comps:
                    for simple in _decompose_polygon_with_holes(
                            comp, min_area_m2=1.0):
                        if (simple.is_empty
                                or simple.area < _MIN_BAND_AREA_M2):
                            continue
                        piece_ring = _open_coords(simple)
                        if len(piece_ring) < 3:
                            continue
                        alts = [resample_alt(vx, vy, kind)
                                for vx, vy in piece_ring]
                        # Clamp any residual single-vertex resample spike.
                        alts = _declaw_alt_needles(
                            alts, tol=_ADJACENT_NEEDLE_TOL_M)
                        layout.shapes.append(BuiltShape(
                            polygon=simple, role=ROLE_GRADED_STRIP,
                            ref=_ADJACENT_REF,
                            node_altitudes=[round(a, 1) for a in alts]
                            + [round(alts[0], 1)]))
                        emitted += 1
                        try:
                            emitted_union = (
                                simple if emitted_union is None
                                else unary_union([emitted_union, simple]))
                        except _GEOM_EXC:
                            pass

        # APRON retaining wall (ruling 3): where the DEM at the shoulder
        # OUTER edge sits more than the wall threshold below the shoulder
        # edge altitude, a vertical face replaces graded fill (aprons have
        # no fill mandate beyond the 3 m shoulder — the floor is free).
        if family == "apron":
            n_wall, emitted_union = _emit_apron_walls(
                layout, stations, st_alts, outs, ceil_off, step,
                sample_dem, static_block, boundary, emitted_union)
            emitted += n_wall

    return emitted


def _emit_apron_walls(layout, stations, st_alts, outs, ceil_off, step,
                      sample_dem, static_block, boundary,
                      emitted_union=None):
    """Emit ``retaining_wall`` faces along an apron edge where the DEM
    drops more than ``APRON_EDGE_WALL_MIN_DROP_M`` below the shoulder
    outer-edge altitude (reuses the ``ROLE_RETAINING_WALL`` emit contract
    — a thin vertical band, top row at the shoulder edge, bottom row at
    the DEM).  Grouped into runs of consecutive dropped stations."""
    w = APRON_SHOULDER_WIDTH_M
    n = len(stations)
    top_alt: list = [None] * n
    dem_alt: list = [None] * n
    dropped = [False] * n
    for i, (sx, sy) in enumerate(stations):
        ref = st_alts[i]
        if ref is None:
            continue
        nx, ny = outs[i]
        ox, oy = sx + nx * w, sy + ny * w
        shoulder_edge = ref + ceil_off(w)
        dd = sample_dem(ox, oy)
        if dd is None:
            continue
        if shoulder_edge - float(dd) > APRON_EDGE_WALL_MIN_DROP_M:
            dropped[i] = True
            top_alt[i] = shoulder_edge
            dem_alt[i] = float(dd)
    if not any(dropped):
        return 0, emitted_union
    idx = [i for i in range(n) if dropped[i]]
    runs: list[list[int]] = []
    cur = [idx[0]]
    for j in idx[1:]:
        if j - cur[-1] <= 2:
            cur.append(j)
        else:
            runs.append(cur)
            cur = [j]
    runs.append(cur)
    emitted = 0
    for run in runs:
        if len(run) < 2:
            continue
        top_pts, top_alts = [], []
        bot_pts, bot_alts = [], []
        for i in run:
            sx, sy = stations[i]
            nx, ny = outs[i]
            ox, oy = sx + nx * w, sy + ny * w
            top_pts.append((ox, oy))
            top_alts.append(round(float(top_alt[i]), 1))
            # Bottom row a hair further out so the face has extent.
            bx, by = sx + nx * (w + _PAVEMENT_GAP_M), sy + ny * (w + _PAVEMENT_GAP_M)
            bot_pts.append((bx, by))
            bot_alts.append(round(float(dem_alt[i]), 1))
        ring = top_pts + bot_pts[::-1]
        alts = top_alts + bot_alts[::-1]
        try:
            poly = Polygon(ring)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if static_block is not None and not static_block.is_empty:
                poly = poly.difference(static_block)
            # Keep the wall clear of the just-emitted graded strips so its
            # face never shares a near-parallel sub-mm edge with a strip
            # (the epsilon-wedge class): clip it out of their footprint.
            if emitted_union is not None and not emitted_union.is_empty:
                poly = poly.difference(emitted_union.buffer(_PAVEMENT_GAP_M))
            if boundary is not None and not boundary.is_empty:
                poly = poly.intersection(boundary)
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
        except _GEOM_EXC:
            continue
        pr = _open_coords(poly)
        if len(pr) < 3:
            continue
        walts = [round(float(_nearest_alt(
            ring, alts, vx, vy)), 1) for vx, vy in pr]
        layout.shapes.append(BuiltShape(
            polygon=poly, role=ROLE_RETAINING_WALL,
            ref=_ADJACENT_WALL_REF,
            node_altitudes=walts + [walts[0]]))
        emitted += 1
        try:
            emitted_union = (
                poly if emitted_union is None
                else unary_union([emitted_union, poly]))
        except _GEOM_EXC:
            pass
    return emitted, emitted_union
