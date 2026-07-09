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

The machinery is the runway-end skirt's banded emission, with both
directions inline-duplicated MINIMALLY from clearance.py (flagged for
the cleanup slice): ``_build_fill_bands`` twins ``_build_filled_skirts``
(all-band run widening, no skirt-lift ring values), ``_build_cut_bands``
mirrors it for the corridor's piecewise-continuous ceiling (the skirt's
cut twin ``_build_graded_strips`` takes a single linear slope).  Bands are
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
import os

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
    VERTEX_ALT_MERGE_TOL_M,
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
    _declaw_alt_needles,
    _open_coords,
    _PAVEMENT_GAP_M,
    _RING_END_NORMAL_DOT,
    _RING_PROBE_M,
    _unit,
)
# Shared with the layout-wide emit decimation: the group decimation for the
# late-emitted bands and the millimetre vertex-identity key the
# value-agreement registry uses (one identity convention everywhere).
from .emit_decimate import (
    Z_TOL_BOUNDARY_M,
    _key as _vertex_key,
    decimate_shape_group,
)

# An adjacent-ground band is a SHALLOW corridor surface (a code-4 runway
# fill falls ≈2.3 m over 75 m, monotonically across many vertices), so a
# single-vertex altitude reversal larger than this is always a
# clip-introduced resample flip on a concave ring, not a real feature —
# ``_declaw_alt_needles`` clamps it to the neighbour mean.  Tighter than
# the runway-end skirt's 3 m needle tolerance because the lateral bands
# are shallower than an end skirt.
_ADJACENT_NEEDLE_TOL_M = 1.5
# Snap-to-bound band (round 2, triangle diet): a clamped band value within
# this of a corridor bound emits the BOUND itself, so near-bound runs are
# piecewise-linear and decimate away instead of tracing DEM jitter.  At
# the emit-quantization noise floor (values round to 0.1 m; the validator
# allows 0.15 m edge noise) — the emitted surface stays in the corridor.
_CORRIDOR_SNAP_TOL_M = 0.15
# Corner-fan resolution: intermediate fan stations are inserted at convex
# ring corners so consecutive normals never differ by more than this (the
# residual chord sagitta at the runway reach, R·(1−cos(θ/2)) ≈ 2.6 m at
# 15° / R=300, is inside one band step of coverage).
_FAN_MAX_STEP_RAD = math.radians(15.0)
# Lab forensics: O4_ADJACENT_GROUND_DEBUG=1 logs per-shape band counts and
# every dropped piece, for chasing validator coverage findings.
_ADJACENT_DEBUG = os.environ.get("O4_ADJACENT_GROUND_DEBUG") == "1"
# O4_ADJACENT_GROUND_DEBUG_POINTS="x,y;x,y" (local metres): per-shape
# station/coverage decisions near each point, for replaying a validator
# coverage finding against the emitter's exact choices.
_ADJACENT_DEBUG_POINTS: list[tuple[float, float]] = []
for _pair in (os.environ.get("O4_ADJACENT_GROUND_DEBUG_POINTS") or "") \
        .split(";"):
    if "," in _pair:
        _px, _py = _pair.split(",", 1)
        try:
            _ADJACENT_DEBUG_POINTS.append((float(_px), float(_py)))
        except ValueError:
            pass

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
            # Clamp INSIDE the cap: at exactly d == reach the corridor is
            # ungoverned (ceiling None) and the sample would be skipped,
            # leaving terrain in the last (reach − step, reach) ring
            # unprotected — the validator samples reach − 1e-3 and flags
            # it (CYXY round-2 addendum findings at d ≈ reach).
            d = min(cap - 1e-3, k * step)
            co = ceiling_offset(d)
            if co is None:      # unbounded up at/beyond the reach — no cut
                continue
            ceil = ref + co
            dd = sample_dem(sx + nx * d, sy + ny * d)
            if dd is not None and dd > ceil + trigger:
                last = d
        if last > 0.0:
            obstructed[i] = True
            outer[i] = min(cap - 1e-3, last + step)
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
            # Widen EVERY band's runs by one station each side (the skirt
            # widens only its first band): the lateral law is a COVERAGE
            # mandate, and a deep band ending exactly at its last
            # obstructed station leaves the half-station wedge past the
            # run end out-of-corridor and ungraded (the round-2 addendum
            # coverage class).  The widened neighbour tapers to d0+step.
            lo = max(0, i0 - 1)
            hi = min(n - 1, i1 + 1)
            inner_pts, inner_alts = [], []
            outer_pts, outer_alts = [], []
            for i in range(lo, hi + 1):
                ref = edge_alts[i]
                if ref is None:
                    # Taper neighbour beyond a skipped station: borrow the
                    # run-end station's edge altitude (the skirt's own
                    # short-run rescue).
                    if i < i0:
                        ref = edge_alts[i0]
                    elif i > i1:
                        ref = edge_alts[i1]
                    if ref is None:
                        continue
                nx, ny = outwards[i]
                if outer[i] > d0:
                    off = min(d1, outer[i])
                else:
                    off = min(d1, d0 + step)    # widened taper neighbour
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


def _build_fill_bands(edge_stations, edge_alts, outwards, band_caps,
                      floor_depth, band_edges, trigger, step, sample_dem):
    """FILL-direction band geometry — clearance._build_filled_skirts,
    inline-duplicated MINIMALLY (flagged for the cleanup slice) with two
    lateral-law differences the shared skirt builder must not inherit:

      * runs widen by one taper station in EVERY band (the skirt widens
        only its first band; a deep lateral band ending exactly at its
        last obstructed station leaves the half-station wedge past the
        run end out-of-corridor — the round-2 addendum coverage class);
      * ring altitudes are NOT computed (returned empty): the emitter
        values every vertex through the corridor-clamp resampler, so the
        builder's ``max(floor, DEM)`` skirt-lift rows would be dead work
        (and are the round-2 UNLAWFUL value rule besides).

    Same contract otherwise: ``(ring_open, alts_open)`` pairs, abutting
    bands split at ``band_edges``, daylight at the floor∧DEM meeting.
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
    edges = [_PAVEMENT_GAP_M]
    for b in sorted(band_edges):
        if _PAVEMENT_GAP_M + 1.0 < b < cap_max - 1.0:
            edges.append(float(b))
    edges.append(cap_max)

    out: list[tuple[list, list]] = []
    for b in range(len(edges) - 1):
        d0, d1 = edges[b], edges[b + 1]
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
            lo = max(0, i0 - 1)
            hi = min(n - 1, i1 + 1)
            inner_pts: list[tuple[float, float]] = []
            outer_pts: list[tuple[float, float]] = []
            for i in range(lo, hi + 1):
                ref = edge_alts[i]
                if ref is None:
                    if i < i0:
                        ref = edge_alts[i0]
                    elif i > i1:
                        ref = edge_alts[i1]
                    if ref is None:
                        continue
                nx, ny = outwards[i]
                if outer[i] > d0:
                    off = min(d1, outer[i])
                else:
                    off = min(d1, d0 + step)    # widened taper neighbour
                if off <= d0:
                    continue
                sx, sy = edge_stations[i]
                inner_pts.append((sx + nx * d0, sy + ny * d0))
                outer_pts.append((sx + nx * off, sy + ny * off))
            if len(inner_pts) < 2:
                continue
            ring = inner_pts + outer_pts[::-1]
            out.append((ring, []))
    return out


def _declaw_short_needle_runs(piece_ring, alts, tol, max_run=2,
                              max_span_m=3.0):
    """Clamp SHORT runs (≤ ``max_run`` consecutive vertices) of altitude
    needles to their flanking mean — the two-vertex extension of
    ``clearance._declaw_alt_needles`` (which by design only clamps single
    vertices).  A run qualifies when its flanking vertices agree within
    ``tol``, every run vertex deviates from the flank mean by more than
    ``tol`` in the same direction, and the flank-to-flank horizontal
    extent is under ``max_span_m`` — a metre-scale reversal packed into a
    couple of sub-metre ring edges is always a resampler foot-flip on a
    notched parent ring (SPJC round-2: a 1.5 m two-vertex dip over
    0.68 m edges), never a real corridor feature (the corridor is a
    ≤5 % surface: 3 m of run can lawfully carry ~0.15 m, not 1.5 m)."""
    n = len(alts)
    if n < max_run + 2:
        return list(alts)
    out = [float(a) for a in alts]
    for start in range(n):
        for run_length in range(1, max_run + 1):
            before = out[(start - 1) % n]
            after = out[(start + run_length) % n]
            if abs(before - after) > tol:
                continue
            flank_mean = 0.5 * (before + after)
            deltas = [out[(start + k) % n] - flank_mean
                      for k in range(run_length)]
            if not all(abs(d) > tol for d in deltas):
                continue
            if not (all(d > 0 for d in deltas)
                    or all(d < 0 for d in deltas)):
                continue
            bx, by = piece_ring[(start - 1) % n]
            ax, ay = piece_ring[(start + run_length) % n]
            if math.hypot(ax - bx, ay - by) > max_span_m:
                continue
            for k in range(run_length):
                out[(start + k) % n] = round(flank_mean, 1)
            break
    return out


def _make_edge_projection_resampler(coords, ring_alts, envelope_at,
                                    graded_width_m, sample_dem):
    """Return ``resample(x, y, kind) -> alt`` for band vertices of one
    shape: the DEM **CLAMPED INTO the corridor** at the vertex's true
    lateral distance ``d`` to the pavement edge (shapely projection),

        alt = min(max(dem, edge + floor(d)), edge + ceiling(d)).

    This is the corridor law applied verbatim (round 2, coordinator
    ruling): the adjacent-ground envelope bounds BOTH sides of the
    emitted surface — unlike the runway-end skirt law, which is a FLOOR
    only, the band may NOT ride a DEM bump above the ceiling (the round-1
    "skirt lift" convention produced 100%+ internal band slopes; unlawful
    here).  Where the DEM sits inside the corridor the clamp returns the
    DEM itself, so the band meets lawful terrain with no step at
    daylight.  Values within ``_CORRIDOR_SNAP_TOL_M`` of a bound emit the
    bound (triangle diet; see the constant).

    ``kind`` keeps the piece's value function CONTINUOUS across the
    corridor's floor discontinuity at the graded width W (finite → None):

      * ``"fill"`` pieces live in zones 1-2 by construction (their band
        cap IS W), so ``d`` clamps to W — outer-row vertices whose
        projection jitters past W stay on the shelf edge instead of
        plunging to the DEM (the round-2 CYXY 25 m in-piece cliff).
      * ``"cut"`` pieces are CEILING-only (floor unapplied): below-floor
        terrain inside a cut piece belongs to the FILL machinery (the
        fill bands emit first and own that footprint — the cut/fill split
        of the flat-shadow convention), and the ceiling is continuous
        over all three zones, so a cut piece spanning the W boundary has
        no value step.

    ``envelope_at(d) -> (floor_offset, ceiling_offset)`` is the family's
    law corridor (``grade_law.adjacent_ground_envelope`` partial).  The
    edge elevation is read by linear-referencing the query's foot along
    the ring against the per-vertex ``ring_alts`` (``None`` entries —
    pavement-facing / unsampled vertices — filled from their nearest
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
        if kind == "fill":
            # Zones 1-2 only (band cap = W); outer-row projection jitter
            # past W must not cross the floor discontinuity.
            d = min(d, graded_width_m)
        floor_offset, ceiling_offset = envelope_at(d)
        if kind == "cut":
            floor_offset = None     # fill bands own below-floor terrain
        dd = sample_dem(x, y)
        if dd is not None:
            value = float(dd)
        elif floor_offset is not None:
            value = float(edge_alt) + floor_offset      # no DEM: law floor
        elif ceiling_offset is not None:
            value = float(edge_alt) + ceiling_offset
        else:
            value = float(edge_alt)
        if floor_offset is not None:
            floor = float(edge_alt) + floor_offset
            # SNAP-TO-BOUND (triangle diet): a DEM within the emit noise
            # band of a corridor bound rides the BOUND, not the jitter —
            # the corridor functions are piecewise-linear, so long
            # near-bound runs become 3D-collinear and decimate away
            # (flat-airport bands otherwise keep every DEM-jitter vertex).
            # Well inside the validator's 0.15 m edge-noise allowance and
            # the DEM's own noise floor; the value stays in the corridor.
            if value <= floor + _CORRIDOR_SNAP_TOL_M:
                value = floor
            value = max(value, floor)
        if ceiling_offset is not None:
            ceiling = float(edge_alt) + ceiling_offset
            if value >= ceiling - _CORRIDOR_SNAP_TOL_M:
                value = ceiling
            value = min(value, ceiling)
        return round(value, 1)

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
    # emit once.  TWO overlap unions with DIFFERENT clip rules (round 2,
    # the strip↔strip wedge fix):
    #   * ``previous_shapes_union`` — bands of EARLIER parent shapes.
    #     Their corridors are computed off DIFFERENT edge references, so
    #     where two shapes' bands meet the values genuinely disagree;
    #     coincident traced boundaries then emit same-XY node pairs whose
    #     wall ENDPOINTS mint zero-angle duplicate-edge wedges (HECA
    #     round-2: 32).  Later shapes clip a full PAVEMENT-GAP standoff
    #     from earlier shapes' bands — a 1 m terrain groove instead of a
    #     shared boundary (no shared geometry, no wedge; the DEM reader
    #     exempts columns within the gap of any shape, so coverage holds).
    #   * ``current_shape_union`` — pieces of the SAME shape (fill vs cut
    #     vs zone splits).  One corridor, one resampler: values agree at
    #     shared coordinates, so these seams stay EXACT and weld into one
    #     continuous surface.
    emitted = 0
    previous_shapes_union = None
    current_shape_union = None
    emitted_shapes: list[BuiltShape] = []
    # VALUE-AGREEMENT registry (feature-weld rule): same-shape pieces abut
    # along clip boundaries coordinate-exactly; first writer wins so a
    # shared coordinate never carries two values (guarded adoption below).
    vertex_value_registry: dict[tuple[int, int], float] = {}

    for s in scoped:
        current_shape_union = None
        params = _family_params(layout, s, rw_axes)
        if params is None:
            continue
        family, code_number, code_letter, reach, width, axis = params
        trigger = trigger_by_family[family]

        def ceil_off(d, _r=family, _cn=code_number, _cl=code_letter):
            return adjacent_ground_envelope(_r, _cn, _cl, d)[1]

        def envelope_at(d, _r=family, _cn=code_number, _cl=code_letter):
            return adjacent_ground_envelope(_r, _cn, _cl, d)

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

        def _station_reference(sx, sy, out, alt_value):
            """The station's edge altitude, or None when it is skipped —
            the END-edge rule (skirt territory) + the terrain-facing
            probe, applied per station exactly as the validator does."""
            if alt_value is None:
                return None
            if (axis is not None
                    and abs(out[0] * axis[0] + out[1] * axis[1])
                    > _RING_END_NORMAL_DOT):
                return None
            if prep_static.contains(Point(sx + out[0] * _RING_PROBE_M,
                                          sy + out[1] * _RING_PROBE_M)):
                return None
            return alt_value

        stations, st_alts, outs = [], [], []
        previous_out = None
        for i in range(len(coords) - 1):
            eax, eay = coords[i]
            ebx, eby = coords[i + 1]
            u = _unit(ebx - eax, eby - eay)
            if u is None:
                continue
            out = (u[1], -u[0]) if ccw else (-u[1], u[0])
            a0 = ring_alts[i]
            a1 = ring_alts[i + 1]
            # CORNER FAN (round-2 addendum, coverage): at a CONVEX ring
            # corner the two edges' band rectangles leave an uncovered
            # circular-segment wedge between their outer rows (chord
            # sagitta ≈ R·(1−cos(θ/2)) — 16 m of un-graded plateau at a
            # CYXY 75 m runway band).  Insert stations AT the corner with
            # normals interpolated across the turn, so the band outer row
            # follows the fan arc piecewise.  Convex = the outward
            # normals sweep across terrain (matches the winding).
            if previous_out is not None:
                cross = previous_out[0] * out[1] - previous_out[1] * out[0]
                convex = (cross > 1e-9) if ccw else (cross < -1e-9)
                if convex:
                    angle_previous = math.atan2(previous_out[1],
                                                previous_out[0])
                    delta = math.atan2(out[1], out[0]) - angle_previous
                    while delta > math.pi:
                        delta -= 2.0 * math.pi
                    while delta < -math.pi:
                        delta += 2.0 * math.pi
                    fan_steps = int(math.ceil(abs(delta)
                                              / _FAN_MAX_STEP_RAD))
                    for f in range(1, fan_steps):
                        fan_angle = (angle_previous
                                     + delta * f / fan_steps)
                        fan_out = (math.cos(fan_angle),
                                   math.sin(fan_angle))
                        stations.append((eax, eay))
                        st_alts.append(_station_reference(
                            eax, eay, fan_out, a0))
                        outs.append(fan_out)
            previous_out = out
            nseg = max(1, int(math.ceil(
                math.hypot(ebx - eax, eby - eay) / step)))
            for k in range(nseg):    # next edge owns the far corner
                t = k / nseg
                sx = eax + (ebx - eax) * t
                sy = eay + (eby - eay) * t
                ref = None
                if a0 is not None and a1 is not None:
                    ref = _station_reference(
                        sx, sy, out, a0 + t * (a1 - a0))
                stations.append((sx, sy))
                st_alts.append(ref)
                outs.append(out)
        if len(stations) < 2:
            continue
        m = len(stations)

        # FILL (DEM below floor, zones 1-2): the skirt fill builder's
        # lateral twin (all-band run widening; see _build_fill_bands) —
        # floor_depth = -floor_offset, cap = graded width, band split at
        # the lip breakpoint.
        fill_bands = _build_fill_bands(
            stations, st_alts, outs, [width] * m, floor_depth,
            {ADJACENT_GROUND_LIP_WIDTH_M}, trigger, step, sample_dem)
        # CUT (DEM above ceiling): the corridor's piecewise ceiling out to
        # the family reach, split at the lip + graded-width kinks.
        cut_bands = _build_cut_bands(
            stations, st_alts, outs, [reach] * m, ceil_off,
            {ADJACENT_GROUND_LIP_WIDTH_M, width}, trigger, step, sample_dem)
        if not fill_bands and not cut_bands:
            continue

        # Band-vertex value rule.  Every emitted (possibly clipped) vertex
        # gets its altitude ANALYTICALLY from its TRUE lateral distance to
        # the pavement edge (shapely projection onto the shape's ring): the
        # DEM CLAMPED INTO the law corridor at that distance (see
        # ``_make_edge_projection_resampler``).  Robust for both edge and
        # INTERIOR clip vertices and on concave rings, so a clip introduces
        # no step; law-true by construction (the surface can never leave
        # the corridor).
        resample_alt = _make_edge_projection_resampler(
            coords, ring_alts, envelope_at, width, sample_dem)

        if _ADJACENT_DEBUG and (fill_bands or cut_bands):
            UI.vprint(1, f"  [adjacent-debug] shape role={s.role} "
                         f"ref={s.ref} family={family}: "
                         f"{len(fill_bands)} fill / {len(cut_bands)} cut "
                         f"raw band(s)")
        for qx, qy in _ADJACENT_DEBUG_POINTS:
            try:
                dq = s.polygon.distance(Point(qx, qy))
            except _GEOM_EXC:
                continue
            if dq > reach + 10.0:
                continue
            nearest = min(range(len(stations)),
                          key=lambda i: (stations[i][0] - qx) ** 2
                          + (stations[i][1] - qy) ** 2)
            sx, sy = stations[nearest]
            fl, ce = envelope_at(max(_PAVEMENT_GAP_M, dq))
            dem_q = sample_dem(qx, qy)
            raw_dist = None
            for band_list in (fill_bands, cut_bands):
                for ring, _ in band_list:
                    try:
                        rd = Polygon(ring).buffer(0).distance(Point(qx, qy))
                    except _GEOM_EXC:
                        continue
                    if raw_dist is None or rd < raw_dist:
                        raw_dist = rd
            # Replicate the builders' outward scan at the nearest station.
            scan = ""
            ref_alt = st_alts[nearest]
            if ref_alt is not None:
                nx_, ny_ = outs[nearest]
                last_fill = last_cut = 0.0
                nst = max(1, int(math.ceil(max(width, reach) / step)))
                for k in range(1, nst + 1):
                    dk = k * step
                    dd = sample_dem(sx + nx_ * dk, sy + ny_ * dk)
                    if dd is None:
                        continue
                    if dk <= width:
                        fd = floor_depth(dk)
                        if fd is not None and dd < ref_alt - fd - trigger:
                            last_fill = dk
                    if dk < reach:
                        co = ceil_off(min(dk, reach - 1e-3))
                        if co is not None and dd > ref_alt + co + trigger:
                            last_cut = dk
                scan = (f" scan(last_fill={last_fill:.0f},"
                        f"last_cut={last_cut:.0f})")
            UI.vprint(1,
                f"  [adjacent-debug-point] q=({qx:.0f},{qy:.0f}) "
                f"shape role={s.role} ref={s.ref} d={dq:.1f} "
                f"nearest_station=({sx:.0f},{sy:.0f}) "
                f"station_ref={st_alts[nearest]} dem={dem_q} "
                f"floor={fl} ceiling={ce} raw_band_dist={raw_dist}"
                f"{scan}")
        for kind, band_list in (("fill", fill_bands), ("cut", cut_bands)):
            for ring, _ralts in band_list:
                try:
                    poly = Polygon(ring)
                    raw_area = poly.area if poly.is_valid else None
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                        raw_area = poly.area
                    poly = poly.difference(static_block)
                    if (previous_shapes_union is not None
                            and not previous_shapes_union.is_empty):
                        # Cross-shape groove clip (see the union split).
                        poly = poly.difference(
                            previous_shapes_union.buffer(_PAVEMENT_GAP_M))
                    if (current_shape_union is not None
                            and not current_shape_union.is_empty):
                        # Same-shape exact clip (welded seam).
                        poly = poly.difference(current_shape_union)
                    if boundary is not None and not boundary.is_empty:
                        poly = poly.intersection(boundary)
                    if poly.is_empty:
                        if _ADJACENT_DEBUG and raw_area:
                            b = Polygon(ring).bounds
                            UI.vprint(1,
                                f"  [adjacent-debug] {kind} band clipped "
                                f"to EMPTY (raw {raw_area:.0f} m2) "
                                f"bbox={b}")
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
                        if simple.is_empty:
                            continue
                        if simple.area < _MIN_BAND_AREA_M2:
                            # The min-area gate rejects freestanding
                            # confetti — but a small fragment ATTACHED to
                            # existing geometry is a legitimate corner
                            # patch of continuous coverage (the runway-end
                            # skirt's own rule; dropping attached
                            # fragments left validator-visible coverage
                            # notches).  Keep attached; drop isolated.
                            attached = False
                            if simple.area >= 1.0:
                                try:
                                    attached = (simple.distance(
                                        static_block) <= 1.0)
                                except _GEOM_EXC:
                                    attached = False
                            if not attached:
                                if _ADJACENT_DEBUG:
                                    b = simple.bounds
                                    UI.vprint(1,
                                        f"  [adjacent-debug] dropped "
                                        f"isolated fragment area="
                                        f"{simple.area:.1f} bbox={b}")
                                continue
                        piece_ring = _open_coords(simple)
                        if len(piece_ring) < 3:
                            continue
                        keys = [_vertex_key(vx, vy)
                                for vx, vy in piece_ring]
                        own = [resample_alt(vx, vy, kind)
                               for vx, vy in piece_ring]
                        # GUARDED adoption (round 2): adopt a previously
                        # registered value at this coordinate only when it
                        # agrees with this band's OWN law value within the
                        # node-merge tolerance — that is exactly the case
                        # that would otherwise mint two coincident nodes /
                        # a zero-angle wedge.  A larger disagreement means
                        # the two bands' corridors genuinely differ here
                        # (different edge references); adopting it tears
                        # THIS band's surface (HECA round-2: a 4.8 m
                        # adopted step), while keeping our own value emits
                        # a deliberate wall of two separate nodes — the
                        # emitter's node-split convention, no wedge.
                        adopted = [
                            (k in vertex_value_registry
                             and abs(vertex_value_registry[k] - o)
                             <= VERTEX_ALT_MERGE_TOL_M)
                            for k, o in zip(keys, own)]
                        alts = [vertex_value_registry[k] if a else o
                                for k, a, o in zip(keys, adopted, own)]
                        # Clamp any residual single-vertex resample spike,
                        # then metre-scale SHORT dip runs a foot-flip
                        # mints on notched parent rings (≤2 vertices over
                        # ≤3 m — see _declaw_short_needle_runs).
                        alts = _declaw_alt_needles(
                            alts, tol=_ADJACENT_NEEDLE_TOL_M)
                        # Run threshold = the fill/cut trigger: over ≤3 m
                        # the corridor changes ≲0.15 m lawfully, so a >1 m
                        # short-run reversal is always the foot-flip class.
                        alts = _declaw_short_needle_runs(
                            piece_ring, alts, tol=trigger)
                        alts = [round(a, 1) for a in alts]
                        # Re-assert adopted weld values (declaw must not
                        # move a shared-coordinate agreement), then
                        # register this piece's values for later bands.
                        for j, (k, a) in enumerate(zip(keys, adopted)):
                            if a:
                                alts[j] = vertex_value_registry[k]
                            elif k not in vertex_value_registry:
                                vertex_value_registry[k] = alts[j]
                        shape = BuiltShape(
                            polygon=simple, role=ROLE_GRADED_STRIP,
                            ref=_ADJACENT_REF,
                            node_altitudes=alts + [alts[0]])
                        layout.shapes.append(shape)
                        emitted_shapes.append(shape)
                        emitted += 1
                        try:
                            current_shape_union = (
                                simple if current_shape_union is None
                                else unary_union(
                                    [current_shape_union, simple]))
                        except _GEOM_EXC:
                            pass

        # APRON retaining wall (ruling 3): where the DEM at the shoulder
        # OUTER edge sits more than the wall threshold below the shoulder
        # edge altitude, a vertical face replaces graded fill (aprons have
        # no fill mandate beyond the 3 m shoulder — the floor is free).
        if family == "apron":
            wall_clip = current_shape_union
            if previous_shapes_union is not None:
                try:
                    wall_clip = (previous_shapes_union
                                 if wall_clip is None
                                 else unary_union([wall_clip,
                                                   previous_shapes_union]))
                except _GEOM_EXC:
                    pass
            n_wall, wall_union = _emit_apron_walls(
                layout, stations, st_alts, outs, ceil_off, step,
                sample_dem, static_block, boundary, wall_clip,
                emitted_shapes)
            emitted += n_wall
            if n_wall and wall_union is not None:
                current_shape_union = wall_union

        # Fold this shape's pieces into the cross-shape union (groove
        # clip for every LATER shape).
        if current_shape_union is not None:
            try:
                previous_shapes_union = (
                    current_shape_union if previous_shapes_union is None
                    else unary_union([previous_shapes_union,
                                      current_shape_union]))
            except _GEOM_EXC:
                pass

    # TRIANGLE DIET (round 2): 3D-collinear decimation over the emitted
    # group.  The pipeline's layout-wide ``decimate_emit_nodes`` ran BEFORE
    # this emitter, so the bands' 5 m-stationed rows would otherwise reach
    # the triangulator undecimated (KCLT gate-on tripled the input nodes).
    # Post-clamp the band surface is piecewise-linear wherever the DEM is
    # outside the corridor, so straight lawful runs collapse to their zone
    # breakpoints.  Group-scoped: bands keep a 1 m standoff from all
    # earlier geometry, so their vertices are shared only among themselves
    # (the vote discipline then guarantees no T-vertex is minted).
    # Boundary-class Z tolerance (±0.1 m): band values carry smoothed-DEM
    # jitter, the same noise family as the boundary ribbon.
    if emitted_shapes:
        removed = decimate_shape_group(emitted_shapes, Z_TOL_BOUNDARY_M)
        if removed:
            UI.vprint(1, f"  [pav-builder] adjacent-ground: decimated "
                         f"{removed} 3D-collinear band vertex(es) "
                         f"(±{Z_TOL_BOUNDARY_M} m).")

    return emitted


def _emit_apron_walls(layout, stations, st_alts, outs, ceil_off, step,
                      sample_dem, static_block, boundary,
                      emitted_union=None, emitted_shapes=None):
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
        wall_shape = BuiltShape(
            polygon=poly, role=ROLE_RETAINING_WALL,
            ref=_ADJACENT_WALL_REF,
            node_altitudes=walts + [walts[0]])
        layout.shapes.append(wall_shape)
        if emitted_shapes is not None:
            emitted_shapes.append(wall_shape)
        emitted += 1
        try:
            emitted_union = (
                poly if emitted_union is None
                else unary_union([emitted_union, poly]))
        except _GEOM_EXC:
            pass
    return emitted, emitted_union
