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
and are clipped EXACTLY against every existing shape + the airport
boundary (weld ruling 2026-07-09: the band's inner row sits ON the
pavement ring with the pavement edge values verbatim — no standoff
groove; shared boundaries weld by shared coordinates + the guarded
value adoption below).

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
from .grade_law import (
    adjacent_ground_envelope,
    adjacent_ground_supported_depths,
)
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
# Cross-shape run-end taper seam pin (user 2026-07-10, default ON): suppress
# the daylight bench-in at pavement-PARTITION seams so abutting shapes' terminal
# stations agree on outer depth (no seam notch).  O4_SEAM_TAPER_PIN=0 disables
# it (A/B lever); the validator reads the SAME env so the lockstep pair stays
# aligned.
_SEAM_TAPER_PIN = os.environ.get("O4_SEAM_TAPER_PIN", "1") != "0"
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


def airside_seam_vertex_keys(layout):
    """Millimetre vertex keys shared between TWO OR MORE airside pavement
    shapes — the CONTINUATION SEAMS where one shape's terrain-facing frontage
    hands off to an abutting shape (user 2026-07-10, cross-shape run-end
    taper).  A band station adjacent to one of these corners sits at a run
    boundary that exists because of the pavement PARTITION, not because the
    frontage ends, so the daylight bench-in is suppressed there (see
    ``grade_law.adjacent_ground_supported_depths``).

    Computed over the SAME airside pavement roles the emitter marches
    (``_RUNWAY_ROLES + _TAXIWAY_ROLES + _APRON_ROLES`` ==
    ``clearance._AIRSIDE_PAVEMENT_ROLES``); the emitted ``graded_strip`` bands
    are NOT counted, so the emitter (pre-emit) and the validator (post-emit)
    derive the identical seam set — lockstep by construction.  A shape's ring
    is de-duplicated first so its own closing vertex is not miscounted as a
    second shape."""
    from collections import Counter
    in_scope = _RUNWAY_ROLES + _TAXIWAY_ROLES + _APRON_ROLES
    counts: "Counter[tuple[int, int]]" = Counter()
    for s in layout.shapes:
        if s.role not in in_scope:
            continue
        if s.polygon is None or s.polygon.is_empty \
                or s.polygon.geom_type != "Polygon":
            continue
        try:
            ring = list(s.polygon.exterior.coords)[:-1]
        except _GEOM_EXC:
            continue
        for k in {_vertex_key(vx, vy) for vx, vy in ring}:
            counts[k] += 1
    return {k for k, c in counts.items() if c >= 2}


def _dedup_ring(ring, alts):
    """Drop consecutive duplicate ring coordinates, keeping any aligned
    altitude list in step.  With the inner boundary AT the pavement edge
    (d0 = 0) every corner-fan station shares the corner coordinate, so a
    fan's inner row degenerates to one point — deduplicated, the band is
    the valid fan SECTOR polygon instead of a self-touching ring."""
    if not ring:
        return ring, alts
    kept_ring = [ring[0]]
    kept_alts = [alts[0]] if alts else []
    for i in range(1, len(ring)):
        if ring[i] == kept_ring[-1]:
            continue
        kept_ring.append(ring[i])
        if alts:
            kept_alts.append(alts[i])
    if len(kept_ring) > 1 and kept_ring[0] == kept_ring[-1]:
        kept_ring.pop()
        if kept_alts:
            kept_alts.pop()
    return kept_ring, kept_alts


def _repair_self_lenses(g):
    """Split near-degenerate self-pinches in a band polygon.

    A band rail snapped onto a static chain can double back over its
    OWN other rail sub-µm apart (thin cut residue collapsed onto the
    runway line) — an in-ring near-parallel lens Triangle4XP Ruppert-
    refines catastrophically (the CYXY 60.717 hotspot: 182k triangles
    from ONE pinched ring).  Insert the ring's own vertices into edges
    they graze (≤5 mm), forcing the pinch into an EXACT self-touch
    that ``buffer(0)`` resolves into clean lobes; the zero-width
    excursion vanishes, the real lobe keeps its adopted chain."""
    try:
        ring = list(g.exterior.coords)[:-1]
    except _GEOM_EXC:
        return [g]
    n = len(ring)
    if n < 4:
        return [g]
    out: list[tuple[float, float]] = []
    changed = False
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        out.append((ax, ay))
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 < 1e-12:
            continue
        L = math.sqrt(L2)
        ins = []
        for j in range(n):
            if j == i or j == (i + 1) % n:
                continue
            px, py = ring[j]
            t = ((px - ax) * dx + (py - ay) * dy) / L2
            if t <= 0.0 or t >= 1.0:
                continue
            if t * L < 0.005 or (1.0 - t) * L < 0.005:
                continue
            perp = abs((px - ax) * dy - (py - ay) * dx) / L
            if perp < 0.005:
                ins.append((t, (px, py)))
        for _t, p in sorted(ins):
            if out[-1] != p:
                out.append(p)
                changed = True
    if not changed:
        return [g]
    try:
        rep = Polygon(out, [list(h.coords) for h in g.interiors])
        if not rep.is_valid:
            rep = rep.buffer(0)
        if rep.geom_type == "Polygon":
            parts = [rep]
        else:
            parts = [q for q in getattr(rep, "geoms", [])
                     if q.geom_type == "Polygon"]
        parts = [q for q in parts if not q.is_empty]
        return parts or [g]
    except _GEOM_EXC:
        return [g]


def _build_cut_bands(edge_stations, edge_alts, outwards, band_caps,
                     ceiling_offset, band_edges, trigger, step,
                     sample_dem, is_ring_vertex=None,
                     at_continuation_seam=None):
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

    ``is_ring_vertex`` (per station, aligned with ``edge_stations``; None =
    off) thins the d0 == 0 WELD row to the pavement-chain subsequence: at
    the edge the inner altitude IS the pavement value, which interpolates
    identically along the ring's own straight edges, so a mid-edge station
    adds a node (a T-vertex the conformance pass must insert into the
    pavement ring) without adding information.  Only ring vertices +
    each run's surviving endpoints are kept on that row; the outer row and
    every d0 > 0 row keep full station density.
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
    # DAYLIGHT slope-limit (grade_law.adjacent_ground_supported_depths, user
    # 2026-07-09): couple the independently-scanned per-station depths so the
    # daylight line benches along the frontage — an isolated deep ray no
    # neighbour corroborates is clamped to a shallow benched entry instead of
    # a knife-slot blade (CYXY 417).  A station whose clamped depth falls to
    # <= a slab's d0 simply drops out of that slab's runs via the
    # ``outer[i] > d0`` tests below (``obstructed[i]`` stays True, but the run
    # membership test already gates on the clamped ``outer``).  Fan stations
    # share the corner coordinate (dist = 0), so a fan ray earns no allowance
    # and is suppressed to the corner's depth.  Continuation-seam terminal
    # stations (``at_continuation_seam``) are pinned to their raw depth so the
    # daylight line stays continuous across a pavement partition (user
    # 2026-07-10; see grade_law).
    outer = adjacent_ground_supported_depths(
        outer, edge_stations, at_continuation_seam)
    # Inner boundary AT the pavement edge (d = 0): the band WELDS to the
    # pavement ring it grades off (user ruling 2026-07-09 — no standoff
    # gap; a 1 m groove of raw DEM rendered as a knife-edge wall/trench
    # along the pavement at CYXY).  Weld-row values are the pavement
    # edge values themselves (corridor at d = 0 is [0, 0]).
    edges = [0.0]
    for b in sorted(band_edges):
        if 1.0 < b < cap_max - 1.0:
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
            # Bridge by PHYSICAL distance, not index count: on a ring
            # with long edges one station index can be 50-150 m from
            # the next, and an index-gap bridge spans that whole
            # unobstructed frontage as a spike band far beyond the
            # graded corridor (CYXY shapeIDs 447-449, user 2026-07-09).
            jx, jy = edge_stations[j]
            cx_, cy_ = edge_stations[cur[-1]]
            if (j - cur[-1] <= 2
                    and math.hypot(jx - cx_, jy - cy_) <= 2.5 * step):
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
            # The d0 == 0 weld row uses EXISTING pavement ring vertices
            # ONLY (user ruling 2026-07-09: grading shapes never create
            # a node on a pavement edge — a mid-edge value is the lerp
            # between pavement vertices, identical on both sides by
            # definition).  A segment whose stations include no ring
            # vertex EXTENDS to the nearest bracketing ring-vertex
            # stations; the outer row keeps every surviving station.
            thin_inner = d0 == 0.0 and is_ring_vertex is not None
            inner_row: list[tuple[int, tuple, float, bool]] = []
            outer_pts, outer_alts = [], []

            def _ring_vertex_entry(from_i, direction):
                """Nearest ring-vertex station outward of ``from_i``
                with a usable reference — the weld chain's extension
                point (an EXISTING pavement vertex)."""
                j = from_i + direction
                for _ in range(64):
                    if j < 0 or j >= n:
                        return None
                    if is_ring_vertex[j] and edge_alts[j] is not None:
                        co0_ = ceiling_offset(d0)
                        if co0_ is None:
                            return None
                        sx_, sy_ = edge_stations[j]
                        return (j, (sx_, sy_),
                                round(float(edge_alts[j] + co0_), 1),
                                True)
                    j += direction
                return None

            def _flush_segment():
                if not inner_row:
                    return
                if thin_inner:
                    kept = [e for e in inner_row if e[3]]
                    if not kept or kept[0][0] != inner_row[0][0]:
                        ext = _ring_vertex_entry(inner_row[0][0], -1)
                        if ext is not None:
                            inner_row.insert(0, ext)
                        else:
                            inner_row[0] = (inner_row[0][0],
                                            inner_row[0][1],
                                            inner_row[0][2], True)
                    if not kept or kept[-1][0] != inner_row[-1][0]:
                        ext = _ring_vertex_entry(inner_row[-1][0], 1)
                        if ext is not None:
                            inner_row.append(ext)
                        else:
                            inner_row[-1] = (inner_row[-1][0],
                                             inner_row[-1][1],
                                             inner_row[-1][2], True)
                else:
                    inner_row[0] = (inner_row[0][0], inner_row[0][1],
                                    inner_row[0][2], True)
                    inner_row[-1] = (inner_row[-1][0], inner_row[-1][1],
                                     inner_row[-1][2], True)
                inner_pts = [p for _i, p, _a, k in inner_row if k]
                inner_alts = [a for _i, _p, a, k in inner_row if k]
                if len(inner_pts) >= 2:
                    ring, alts = _dedup_ring(
                        inner_pts + outer_pts[::-1],
                        inner_alts + outer_alts[::-1])
                    if len(ring) >= 3:
                        out.append((ring, alts))
                inner_row.clear()
                outer_pts.clear()
                outer_alts.clear()

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
                ox, oy = sx + nx * off, sy + ny * off
                # OUTER-JUMP FLUSH (user in-sim report 2026-07-09,
                # CYXY shapeIDs 447-449): at a corner FAN adjacent to
                # a skipped sweep (runway-end rays), two surviving
                # rays sit at index gap 1 and station distance 0 —
                # both bridge tests pass — while their OUTER points
                # land 100-220 m apart, and the ring chords straight
                # across the un-graded end zone as a spike triangle.
                # Any outer jump beyond 4 stations closes the ring;
                # the next station starts a fresh one.
                if outer_pts and math.hypot(
                        ox - outer_pts[-1][0],
                        oy - outer_pts[-1][1]) > 4.0 * step:
                    _flush_segment()
                outer_pts.append((ox, oy))
                outer_alts.append(round(float(ref + co1), 1))
                keep = (not thin_inner) or bool(is_ring_vertex[i])
                inner_row.append((i, (ix, iy),
                                  round(float(ref + co0), 1), keep))
            _flush_segment()
    return out


def _build_fill_bands(edge_stations, edge_alts, outwards, band_caps,
                      floor_depth, band_edges, trigger, step, sample_dem,
                      is_ring_vertex=None, at_continuation_seam=None):
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

    ``is_ring_vertex`` thins the d0 == 0 weld row to ring vertices + run
    endpoints exactly as in ``_build_cut_bands`` (see there).
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
    # DAYLIGHT slope-limit — the fill twin of the cut clamp (see
    # _build_cut_bands): bench the fill daylight line along the frontage so an
    # isolated deep fill ray no neighbour corroborates drops out of the deep
    # slabs' runs via the ``outer[i] > d0`` tests below (``dropped[i]`` stays
    # True; the run membership already gates on the clamped ``outer``).
    # Continuation-seam terminal stations are pinned (see _build_cut_bands).
    outer = adjacent_ground_supported_depths(
        outer, edge_stations, at_continuation_seam)
    # Inner boundary AT the pavement edge — the fill welds to the ring
    # (see _build_cut_bands; same user ruling).
    edges = [0.0]
    for b in sorted(band_edges):
        if 1.0 < b < cap_max - 1.0:
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
            # Physical-distance bridge (see the cut twin): an index
            # bridge on long ring edges mints spike bands.
            jx, jy = edge_stations[j]
            cx_, cy_ = edge_stations[cur[-1]]
            if (j - cur[-1] <= 2
                    and math.hypot(jx - cx_, jy - cy_) <= 2.5 * step):
                cur.append(j)
            else:
                runs.append(cur)
                cur = [j]
        runs.append(cur)
        for run in runs:
            i0, i1 = run[0], run[-1]
            lo = max(0, i0 - 1)
            hi = min(n - 1, i1 + 1)
            # The d0 == 0 weld row thins to the pavement-chain subsequence
            # (see _build_cut_bands); the outer row keeps full density.
            thin_inner = d0 == 0.0 and is_ring_vertex is not None
            inner_row: list[tuple[int, tuple[float, float], bool]] = []
            outer_pts: list[tuple[float, float]] = []

            def _ring_vertex_point(from_i, direction):
                # Nearest ring-vertex station outward of ``from_i`` —
                # the weld chain extends to an EXISTING pavement vertex
                # (user ruling 2026-07-09: never create a node on a
                # pavement edge).
                j = from_i + direction
                for _ in range(64):
                    if j < 0 or j >= n:
                        return None
                    if is_ring_vertex[j] and edge_alts[j] is not None:
                        return (j, edge_stations[j], True)
                    j += direction
                return None

            def _flush_segment():
                if not inner_row:
                    return
                if thin_inner:
                    kept = [e for e in inner_row if e[2]]
                    if not kept or kept[0][0] != inner_row[0][0]:
                        ext = _ring_vertex_point(inner_row[0][0], -1)
                        if ext is not None:
                            inner_row.insert(0, ext)
                        else:
                            inner_row[0] = (inner_row[0][0],
                                            inner_row[0][1], True)
                    if not kept or kept[-1][0] != inner_row[-1][0]:
                        ext = _ring_vertex_point(inner_row[-1][0], 1)
                        if ext is not None:
                            inner_row.append(ext)
                        else:
                            inner_row[-1] = (inner_row[-1][0],
                                             inner_row[-1][1], True)
                else:
                    inner_row[0] = (inner_row[0][0], inner_row[0][1],
                                    True)
                    inner_row[-1] = (inner_row[-1][0], inner_row[-1][1],
                                     True)
                inner_pts = [p for _i, p, k in inner_row if k]
                if len(inner_pts) >= 2:
                    ring, _ = _dedup_ring(
                        inner_pts + outer_pts[::-1], [])
                    if len(ring) >= 3:
                        out.append((ring, []))
                inner_row.clear()
                outer_pts.clear()

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
                ox, oy = sx + nx * off, sy + ny * off
                # Outer-jump flush — see the cut twin (corner-fan rays
                # flanking a skipped sweep chord across the gap).
                if outer_pts and math.hypot(
                        ox - outer_pts[-1][0],
                        oy - outer_pts[-1][1]) > 4.0 * step:
                    _flush_segment()
                outer_pts.append((ox, oy))
                keep = (not thin_inner) or bool(is_ring_vertex[i])
                inner_row.append((i, (sx + nx * d0, sy + ny * d0),
                                  keep))
            _flush_segment()
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
    """Return ``resample(x, y, kind) -> (alt, is_weld_row)`` for band
    vertices of one shape: the DEM **CLAMPED INTO the corridor** at the
    vertex's true lateral distance ``d`` to the pavement edge (shapely
    projection); ``is_weld_row`` marks a vertex ON the ring (d ≤ 2 cm),
    whose value is the pavement edge value verbatim (unrounded),

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
    # Fill None entries (pavement-facing / unsampled ring vertices) so every
    # arc position resolves.  An INTERIOR None run is the LOCAL pavement-edge
    # read: LINEARLY interpolated by arc length between the bracketing known
    # vertices, NOT the previous known value carried forward.  A constant
    # carry-forward borrows the run-END reference across the whole None run,
    # so a band vertex whose foot lands there steps off the pavement line at
    # a seam (shadow rows must mirror the pavement line).  Leading/trailing
    # None runs have no bracket and extend the nearest known value.
    known = [i for i in range(len(alt)) if alt[i] is not None]
    if known:
        lo_ptr = 0
        for i in range(len(alt)):
            if alt[i] is not None:
                continue
            while lo_ptr + 1 < len(known) and known[lo_ptr + 1] < i:
                lo_ptr += 1
            lo = known[lo_ptr] if known[lo_ptr] < i else None
            hi = next((j for j in known if j > i), None)
            if lo is not None and hi is not None:
                span = cum[hi] - cum[lo]
                t = 0.0 if span <= 0 else (cum[i] - cum[lo]) / span
                alt[i] = alt[lo] + t * (alt[hi] - alt[lo])
            else:
                alt[i] = alt[lo if lo is not None else hi]

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
            return (0.0, False)
        d = p.distance(line)
        if d <= 0.02:
            # WELD ROW (user ruling 2026-07-09): a vertex ON the
            # pavement ring carries the pavement edge value EXACTLY —
            # unrounded, so the emit consensus at the shared node is a
            # no-op and the band abuts the pavement with zero step.
            return (float(edge_alt), True)
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
        return (round(value, 1), False)

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


def _band_family_closures(family, code_number, code_letter, width):
    """The three family-parameterized corridor closures the march + value
    passes share (``ceil_off``/``envelope_at`` unbounded on ``d``,
    ``floor_depth`` clamped to the graded width so the fill floor stays
    finite — see the inline docstrings the emitter carried).  Single-sourced
    so the pre-solve construction and the post-solve emitter read the corridor
    identically."""
    def ceil_off(d):
        return adjacent_ground_envelope(family, code_number, code_letter, d)[1]

    def envelope_at(d):
        return adjacent_ground_envelope(family, code_number, code_letter, d)

    def floor_depth(d):
        f = adjacent_ground_envelope(
            family, code_number, code_letter, min(d, width))[0]
        return None if f is None else -f

    return ceil_off, envelope_at, floor_depth


def _shape_ring_alts(s, coords, sample_dem=None, seed=False):
    """Per-CLOSED-ring node altitudes aligned with ``coords`` (the
    ``node_altitudes`` contract), else the shape's plane sampler.

    ``seed=True`` (pre-solve construction): entries the shape does not yet
    carry — the taxi/apron/junction rings are unsolved before
    ``per_surface_solve`` — are filled from the smoothed DEM
    (``sample_dem``) rather than left ``None``, so the pre-solve MARCH scans
    the corridor off the DEM-seeded pavement-edge estimate (the design's
    directive; runway rings are already CIFP-solved and keep their real
    values).  ``seed=False`` reproduces the emitter's exact solved-value
    read (byte-identical gate-OFF)."""
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
    if seed and sample_dem is not None:
        for i, (x, y) in enumerate(coords):
            if ring_alts[i] is None:
                dd = sample_dem(x, y)
                if dd is not None:
                    ring_alts[i] = float(dd)
    return ring_alts


def _derive_shape_stations_and_bands(coords, ccw, ring_alts, axis, width,
                                     reach, trigger, floor_depth, ceil_off,
                                     step, prep_static, seam_keys,
                                     sample_dem):
    """Frontage detection + corridor MARCH for one airside shape — the band
    FOOTPRINT geometry (everything that decides WHERE the bands are, given the
    edge-altitude references ``ring_alts``).  Returns
    ``(fill_bands, cut_bands, stations, st_alts, outs)``; the two band lists
    are the raw ``(ring_open, alts_open)`` pairs the emitter later clips and
    values.  Extracted verbatim from the emitter's per-shape setup so the
    pre-solve constructor and the post-solve emitter march identically (the
    B2 shared-helper pattern — parity single-sourced, not duplicated)."""
    def _station_reference(sx, sy, out, alt_value):
        # The station's edge altitude, or None when it is skipped — the
        # END-edge rule (skirt territory) + the terrain-facing probe,
        # applied per station exactly as the validator does.
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
    is_ring_vertex: list[bool] = []
    at_seam: list[bool] = []
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
        # CORNER FAN (coverage): at a CONVEX ring corner insert stations AT
        # the corner with normals interpolated across the turn so the band
        # outer row follows the fan arc piecewise (see the emitter's inline
        # docstring for the sagitta rationale).
        if previous_out is not None:
            cross = previous_out[0] * out[1] - previous_out[1] * out[0]
            convex = (cross > 1e-9) if ccw else (cross < -1e-9)
            # NO fan across a SKIPPED flank (runway END edge / covered
            # probe): interpolated fan rays would sweep into skipped
            # territory as blade spikes.
            if convex and (
                    _station_reference(eax, eay, previous_out,
                                       a0) is None
                    or _station_reference(eax, eay, out,
                                          a0) is None):
                convex = False
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
                    is_ring_vertex.append(True)
                    at_seam.append(False)
        previous_out = out
        nseg = max(1, int(math.ceil(
            math.hypot(ebx - eax, eby - eay) / step)))
        edge_a_seam = _vertex_key(eax, eay) in seam_keys
        edge_b_seam = _vertex_key(ebx, eby) in seam_keys
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
            is_ring_vertex.append(k == 0)
            at_seam.append((k == 0 and edge_a_seam)
                           or (k == nseg - 1 and edge_b_seam))
    if len(stations) < 2:
        return [], [], stations, st_alts, outs
    m = len(stations)

    # FILL (DEM below floor, zones 1-2) then CUT (DEM above ceiling): the
    # runway-end skirt fill/cut builders' lateral twins.
    fill_bands = _build_fill_bands(
        stations, st_alts, outs, [width] * m, floor_depth,
        {ADJACENT_GROUND_LIP_WIDTH_M}, trigger, step, sample_dem,
        is_ring_vertex, at_seam)
    cut_bands = _build_cut_bands(
        stations, st_alts, outs, [reach] * m, ceil_off,
        {ADJACENT_GROUND_LIP_WIDTH_M, width}, trigger, step, sample_dem,
        is_ring_vertex, at_seam)
    return fill_bands, cut_bands, stations, st_alts, outs


def construct_adjacent_ground_presolve(layout: PavementLayout, dem,
                                       tile_lat: int, tile_lon: int,
                                       source_runways=None) -> int:
    """Slice B stage B3 ORDER 1 PRE-SOLVE construction (gate
    ``ONE_SOLVE_TERRAIN`` + ``ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT``).

    Moves the adjacent-ground band FOOTPRINT march
    (``_derive_shape_stations_and_bands``) BEFORE ``per_surface_solve`` from a
    DEM-seeded pavement-edge estimate (``_shape_ring_alts(seed=True)``), and
    stages the raw band rings on ``layout.adjacent_ground_presolve`` for the
    post-solve emitter to CONSUME instead of re-marching.  Values are NOT
    computed here — the emitter values every stored footprint vertex through
    the existing analytic resampler off the SOLVED pavement altitudes, and the
    foreign-shape clip stays at emission (so gate-ON output is value-equivalent
    to gate-OFF up to the enumerated seed/late-feature footprint deltas).

    Stores ``layout.adjacent_ground_presolve = [{"shape": s, "fill": [...],
    "cut": [...]}, ...]`` (the ``shape`` reference is preserved across the
    layout pickle — same object as the ``layout.shapes`` element — so the
    emitter rebuilds the resampler from the by-then-solved shape).  Returns the
    number of shapes with at least one raw band."""
    if dem is None:
        return 0
    if layout.anchor is None:
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
        layout.adjacent_ground_presolve = []
        return 0

    # Terrain-facing probe reference: the union of the shapes PRESENT
    # pre-solve (pavement + pre-solve skirts under B1), minus groundside —
    # the same static block the emitter's ``_station_reference`` probe reads,
    # here at its pre-solve state (post-solve features are absent; their clip
    # is applied at emission, not here).
    try:
        static_union = unary_union(
            [s.polygon for s in layout.shapes
             if s.polygon is not None and not s.polygon.is_empty
             and s.role != "groundside_pavement"])
    except _GEOM_EXC:
        static_union = None
    if static_union is None or static_union.is_empty:
        layout.adjacent_ground_presolve = []
        return 0
    try:
        prep_static = prep(static_union)
    except _GEOM_EXC:
        layout.adjacent_ground_presolve = []
        return 0

    rw_axes: list[tuple] = []
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
            rw_axes.append((LineString([(rax, ray), (rbx, rby)]),
                            ((rbx - rax) / rlen, (rby - ray) / rlen),
                            rlen))

    trigger_by_family = {
        "runway": CLEARANCE_OBSTRUCTION_THRESHOLD_M["runway"],
        "taxiway": CLEARANCE_OBSTRUCTION_THRESHOLD_M["taxiway"],
        "apron": CLEARANCE_OBSTRUCTION_THRESHOLD_M["taxiway"],
    }
    seam_keys = (airside_seam_vertex_keys(layout)
                 if _SEAM_TAPER_PIN else set())

    entries: list[dict] = []
    for s in scoped:
        params = _family_params(layout, s, rw_axes)
        if params is None:
            continue
        family, code_number, code_letter, reach, width, axis = params
        trigger = trigger_by_family[family]
        ceil_off, _envelope_at, floor_depth = _band_family_closures(
            family, code_number, code_letter, width)
        try:
            coords = list(s.polygon.exterior.coords)
            ccw = bool(s.polygon.exterior.is_ccw)
        except _GEOM_EXC:
            continue
        if len(coords) < 4:
            continue
        ring_alts = _shape_ring_alts(s, coords, sample_dem, seed=True)
        fill_bands, cut_bands, _st, _sa, _ou = \
            _derive_shape_stations_and_bands(
                coords, ccw, ring_alts, axis, width, reach, trigger,
                floor_depth, ceil_off, step, prep_static, seam_keys,
                sample_dem)
        if not fill_bands and not cut_bands:
            continue
        entries.append({"shape": s, "fill": fill_bands, "cut": cut_bands})
    layout.adjacent_ground_presolve = entries
    if entries:
        n_bands = sum(len(e["fill"]) + len(e["cut"]) for e in entries)
        UI.vprint(1, f"  [adjacent-ground] PRE-SOLVE constructed raw bands "
                     f"for {len(entries)} shape(s), {n_bands} raw band(s) "
                     f"(one-solve terrain absorption, stage B3 order 1).")
    return len(entries)


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

    # Static block: EVERY existing shape, clipped EXACTLY (user ruling
    # 2026-07-09: the bands WELD to the pavement / features they grade
    # next to — the former 1 m standoff left a groove of raw DEM that
    # rendered as a knife-edge wall or trench along every constrained
    # edge at CYXY).  Shared boundaries carry the same coordinates and
    # (via the weld rows + value registry) agreeing values, so the mesh
    # welds them into one surface instead of minting wedges.
    # GROUNDSIDE EXCLUSION (user ruling 2026-07-09): no grading strip
    # touches groundside pavement — groundside follows the DEM (it IS
    # effectively terrain), so welding a law-floor strip onto its ring
    # imports conflicting values (the CYXY south-hangar violations); a
    # small standoff against it renders harmlessly (no cliff).
    # Groundside leaves the EXACT static union (no welded coordinates
    # against it) and instead blocks bands through a 1 m buffer.
    _gs_polys = [s.polygon for s in layout.shapes
                 if s.role == "groundside_pavement"
                 and s.polygon is not None and not s.polygon.is_empty]
    groundside_block = None
    if _gs_polys:
        try:
            groundside_block = unary_union(_gs_polys).buffer(1.0)
        except _GEOM_EXC:
            groundside_block = None
    static_union = None
    try:
        static_union = unary_union(
            [s.polygon for s in layout.shapes
             if s.polygon is not None and not s.polygon.is_empty
             and s.role != "groundside_pavement"])
    except _GEOM_EXC:
        static_union = None
    if static_union is None or static_union.is_empty:
        return 0
    try:
        prep_static = prep(static_union)
    except _GEOM_EXC:
        return 0
    boundary = layout.airport_boundary

    # CONFORM-TO-STATIC (chain identity, 2026-07-09): a band row that
    # runs just OUTSIDE a foreign shape's edge (10-15 cm — daylight
    # rows, taper stations, clip residue) is never cut by the exact
    # difference (no overlap) and never welded by the 1 cm conformance
    # pass — it survives as a near-parallel constrained pair, and ONE
    # such lens Ruppert-refines to ~10⁵-10⁶ tile triangles (measured
    # at CYXY).  Two moves make the soft ring ADOPT the static chain
    # wherever it runs within ``_SNAP_TO_STATIC_M`` of it:
    #   1. SPLIT every ring edge at the projections of nearby static
    #      VERTICES (a mid-span edge next to a static corner has no
    #      ring vertex to snap — the 88 mm skirt-corner lens class);
    #   2. SNAP every ring vertex (original + inserted) onto the
    #      nearest static exterior.
    # After both, the ring boundary follows the static chain
    # vertex-for-vertex and the final weld unifies them.  Under-
    # pavement grading needs no centimetre fidelity (user 2026-07-09),
    # so a ≤0.2 m lateral adopt is free.
    _SNAP_TO_STATIC_M = 0.2
    from shapely import STRtree as _STRtree
    _static_ext = []
    for _s in layout.shapes:
        if _s.role == "groundside_pavement":
            continue        # never adopt groundside chains (ruling)
        if _s.polygon is not None and not _s.polygon.is_empty:
            try:
                _static_ext.append(_s.polygon.exterior)
            except _GEOM_EXC:
                continue
    try:
        _static_ext_tree = _STRtree(_static_ext)
    except _GEOM_EXC:
        _static_ext_tree = None
    _static_verts = []
    for _ext in _static_ext:
        _static_verts.extend(list(_ext.coords)[:-1])
    try:
        _static_vert_tree = _STRtree(
            [Point(vx, vy) for vx, vy in _static_verts])
    except _GEOM_EXC:
        _static_vert_tree = None

    def _snap_ring_to_static(ring):
        if _static_ext_tree is None:
            return ring
        # 1. split edges at nearby static-vertex projections
        if _static_vert_tree is not None:
            split_ring = []
            n = len(ring)
            for i in range(n):
                ax, ay = ring[i]
                bx, by = ring[(i + 1) % n]
                split_ring.append((ax, ay))
                dx, dy = bx - ax, by - ay
                L2 = dx * dx + dy * dy
                if L2 < 1e-12:
                    continue
                try:
                    cand = _static_vert_tree.query(
                        LineString([(ax, ay), (bx, by)]).buffer(
                            _SNAP_TO_STATIC_M))
                except _GEOM_EXC:
                    continue
                inserts = []
                for gi in cand:
                    vx, vy = _static_verts[gi]
                    t = ((vx - ax) * dx + (vy - ay) * dy) / L2
                    if t <= 1e-6 or t >= 1.0 - 1e-6:
                        continue
                    px_, py_ = ax + t * dx, ay + t * dy
                    perp = math.hypot(vx - px_, vy - py_)
                    L = math.sqrt(L2)
                    if (perp > _SNAP_TO_STATIC_M or t * L < 0.05
                            or (1.0 - t) * L < 0.05):
                        continue
                    inserts.append((t, vx, vy))
                # insert the static VERTEX itself (not the foot):
                # the snapped ring must pass through the static
                # chain's own points to share its constrained edges.
                for _t, vx, vy in sorted(inserts):
                    if split_ring[-1] != (vx, vy):
                        split_ring.append((vx, vy))
            ring = split_ring
        # 2. snap all vertices onto the nearest static exterior
        snapped = []
        for x, y in ring:
            pt = Point(x, y)
            best_d, best_pt = None, None
            try:
                cand = _static_ext_tree.query(
                    pt.buffer(_SNAP_TO_STATIC_M + 0.01))
            except _GEOM_EXC:
                snapped.append((x, y))
                continue
            for gi in cand:
                ext = _static_ext[gi]
                try:
                    d = ext.distance(pt)
                except _GEOM_EXC:
                    continue
                if d <= _SNAP_TO_STATIC_M and (
                        best_d is None or d < best_d):
                    best_d, best_pt = d, ext.interpolate(
                        ext.project(pt))
            if best_pt is not None and best_d > 1e-9:
                snapped.append((best_pt.x, best_pt.y))
            else:
                snapped.append((x, y))
        return snapped

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
    # AUTHORITY keys (user ruling 2026-07-09, round 2: the PAVEMENT
    # value ALWAYS wins at a pavement node): keys registered by a
    # non-soft shape adopt UNCONDITIONALLY — no tolerance guard.  The
    # guard below stays only for soft↔soft coincidences (a skirt/strip
    # value a band happens to land on).  Without this, a band carrying
    # a skirt-derived value onto a junction ring vertex minted a
    # second node 1.71 m below the junction's — an unmerged-node cliff
    # (CYXY 60.6971601,-135.0592654, junction #111).
    authority_value_keys: set[tuple[int, int]] = set()
    # EMITTED-VERTEX POSITION weld (chain identity, site-2 fix 2026-07-10):
    # a later band trimmed against an earlier band's union by the exact
    # ``difference()`` clip is cut along the earlier band's EDGE, so GEOS
    # mints an intersection vertex a few millimetres from the earlier
    # band's CORNER rather than adopting the corner itself.  The two
    # graded_strip writers then emit a 5-6 mm near-parallel / T-vertex
    # pair (the site-2 residual: 60.7208676,-135.0790956).  The mm-keyed
    # ``vertex_value_registry`` cannot unify them (P and its 6 mm twin Q
    # hash to different millimetre keys) and ``_snap_ring_to_static`` does
    # not either — it snaps only to the PRE-EXISTING pavement/junction
    # shapes captured before the loop, never to a sibling band emitted
    # during it.  So keep a coarse spatial hash of every emitted-band
    # vertex and snap each freshly-clipped ring vertex onto a prior
    # emitted-band vertex within a TIGHT distance (the epsilon-wedge
    # class only).  GATED ON VALUE AGREEMENT: only weld when the two
    # bands' altitudes match within ``VERTEX_ALT_MERGE_TOL_M`` — that is
    # exactly the "should agree by construction" class the sub-centimetre
    # seam represents.  Where the two bands' corridors genuinely step
    # (>1 m, a lawful vertical wall between two taxiways' fills), leaving
    # the vertices at their clip positions preserves the pre-existing
    # ``to_osm`` distance-merge (one interned node); forcing them exactly
    # coincident there would instead mint a two-node wall ``to_osm`` keeps
    # (the ``VERTEX_ALT_MERGE_TOL_M`` split rule).  Insert-only in effect
    # (≤1 cm move); the same identity convention the mm value registry
    # uses.
    _BAND_CORNER_WELD_TOL_M = 0.01
    _WELD_CELL_M = 0.02
    # cell -> list of (x, y, altitude) for prior emitted-band vertices.
    emitted_vertex_cells: \
        dict[tuple[int, int], list[tuple[float, float, float]]] = {}

    def _weld_cell(vx, vy):
        return (int(math.floor(vx / _WELD_CELL_M)),
                int(math.floor(vy / _WELD_CELL_M)))

    def _weld_ring_to_prior_bands(ring_coords, own_alts):
        """Snap each ring vertex onto the nearest prior emitted-band
        vertex within ``_BAND_CORNER_WELD_TOL_M`` whose altitude agrees
        within ``VERTEX_ALT_MERGE_TOL_M``; return the snapped,
        consecutive-deduplicated open ring (unchanged object identity
        when nothing snaps)."""
        if not emitted_vertex_cells:
            return ring_coords
        snapped = []
        moved = False
        for (vx, vy), ov in zip(ring_coords, own_alts):
            cx, cy = _weld_cell(vx, vy)
            best = None
            best_d = _BAND_CORNER_WELD_TOL_M
            for ox in (cx - 1, cx, cx + 1):
                for oy in (cy - 1, cy, cy + 1):
                    for px, py, pv in emitted_vertex_cells.get(
                            (ox, oy), ()):
                        d = math.hypot(vx - px, vy - py)
                        if d < best_d and (
                                ov is None or pv is None
                                or abs(pv - ov) <= VERTEX_ALT_MERGE_TOL_M):
                            best_d, best = d, (px, py)
            if best is not None and best != (vx, vy):
                snapped.append(best)
                moved = True
            else:
                snapped.append((vx, vy))
        if not moved:
            return ring_coords
        dedup: list[tuple[float, float]] = []
        for p in snapped:
            if not dedup or dedup[-1] != p:
                dedup.append(p)
        if len(dedup) >= 2 and dedup[0] == dedup[-1]:
            dedup.pop()
        return dedup

    def _register_emitted_vertices(ring_coords, ring_alts):
        for (vx, vy), va in zip(ring_coords, ring_alts):
            emitted_vertex_cells.setdefault(
                _weld_cell(vx, vy), []).append((vx, vy, va))

    # WELD-VALUE PRELOAD (user ruling 2026-07-09): every EXISTING shape's
    # ring vertices register their exact solved values first, so a band
    # vertex landing on a pavement / skirt / strip vertex ADOPTS that
    # value verbatim — value authorities never move, the band adopts.
    from .layout import SOFT_RECEIVER_ROLES as _SOFT_ROLES
    for s in layout.shapes:
        if s.role == "groundside_pavement":
            continue        # groundside values never adopted (ruling)
        if s.polygon is None or s.polygon.is_empty \
                or s.polygon.geom_type != "Polygon":
            continue
        try:
            existing_coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        na = s.node_altitudes
        s_is_authority = (s.role or "") not in _SOFT_ROLES
        for i, (vx, vy) in enumerate(existing_coords):
            if na and i < len(na) and na[i] is not None:
                value = float(na[i])
            elif not na and s.altitude is not None:
                value = float(s.altitude)
            else:
                continue
            k = _vertex_key(vx, vy)
            if s_is_authority and k not in authority_value_keys:
                # Authority value WINS the registry even over an
                # earlier soft registration.
                vertex_value_registry[k] = value
                authority_value_keys.add(k)
            elif k not in vertex_value_registry:
                vertex_value_registry[k] = value

    # CONTINUATION-SEAM keys (user 2026-07-10): vertices shared between two
    # airside pavement shapes.  A terminal band station adjacent to one is a
    # run boundary from the pavement PARTITION, not a frontage end, so the
    # daylight bench-in is suppressed there (grade_law) — the two abutting
    # runs' terminal stations then agree on outer depth (no seam notch).
    seam_keys = airside_seam_vertex_keys(layout) if _SEAM_TAPER_PIN else set()

    # PRE-BUILT FOOTPRINT store (Slice B stage B3 order 1): gate-ON the raw
    # band rings were marched pre-solve; index them by source-shape identity so
    # the loop consumes them instead of re-marching.  Gate-OFF (default, or no
    # store) ``_presolve_bands`` stays None and the loop marches inline.
    from .config import (ONE_SOLVE_TERRAIN as _OST,
                         ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT as _OST_C)
    _presolve_bands = None
    if _OST and _OST_C:
        _store = getattr(layout, "adjacent_ground_presolve", None)
        if _store:
            _presolve_bands = {id(e["shape"]): e for e in _store}

    for s in scoped:
        current_shape_union = None
        params = _family_params(layout, s, rw_axes)
        if params is None:
            continue
        family, code_number, code_letter, reach, width, axis = params
        trigger = trigger_by_family[family]

        ceil_off, envelope_at, floor_depth = _band_family_closures(
            family, code_number, code_letter, width)

        # Per-CLOSED-ring node altitudes aligned with the ring coords (the
        # node_altitudes contract), else the shape's plane sampler.  Read
        # from the SOLVED shape (values are always analytic post-solve, both
        # configurations).
        try:
            coords = list(s.polygon.exterior.coords)
            ccw = bool(s.polygon.exterior.is_ccw)
        except _GEOM_EXC:
            continue
        if len(coords) < 4:
            continue
        ring_alts = _shape_ring_alts(s, coords)

        # FOOTPRINT SOURCE.  Gate-ON (B3 order 1) the raw band rings were
        # marched PRE-SOLVE from the DEM-seeded estimate
        # (``construct_adjacent_ground_presolve``) and staged on
        # ``layout.adjacent_ground_presolve``; consume them here instead of
        # re-marching.  The emitter still CLIPS them against the (post-solve)
        # static block and VALUES every vertex off the solved altitudes
        # below, so gate-ON is value-equivalent to gate-OFF up to the
        # enumerated seed/late-feature footprint deltas.  Gate-OFF the march
        # runs inline exactly as today through the shared helper
        # (byte-identical).
        _pre = _presolve_bands.get(id(s)) if _presolve_bands else None
        if _pre is not None:
            fill_bands, cut_bands = _pre["fill"], _pre["cut"]
            stations, st_alts, outs = [], [], []
        else:
            (fill_bands, cut_bands,
             stations, st_alts, outs) = _derive_shape_stations_and_bands(
                coords, ccw, ring_alts, axis, width, reach, trigger,
                floor_depth, ceil_off, step, prep_static, seam_keys,
                sample_dem)
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
        for qx, qy in (_ADJACENT_DEBUG_POINTS if stations else ()):
            # Lab forensics replay the marched stations; consumed pre-built
            # footprints carry no station arrays, so this scan is skipped
            # gate-ON (the pre-solve construct log covers that path).
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
                    ring = _snap_ring_to_static(ring)
                    poly = Polygon(ring)
                    raw_area = poly.area if poly.is_valid else None
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                        raw_area = poly.area
                    # EXACT clips everywhere (weld ruling 2026-07-09):
                    # shared boundaries keep shared coordinates, and the
                    # guarded adoption below welds agreeing values while
                    # a genuine disagreement emits the deliberate
                    # node-split wall — never a groove of raw DEM.
                    poly = poly.difference(static_union)
                    if (groundside_block is not None
                            and not groundside_block.is_empty):
                        # Buffered, NOT exact: strips never abut
                        # groundside (user ruling 2026-07-09).
                        poly = poly.difference(groundside_block)
                    if (previous_shapes_union is not None
                            and not previous_shapes_union.is_empty):
                        poly = poly.difference(previous_shapes_union)
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
                comps = [r for c in comps
                         for r in _repair_self_lenses(c)]
                for comp in comps:
                    for simple in _decompose_polygon_with_holes(
                            comp, min_area_m2=1.0):
                        if simple.is_empty:
                            continue
                        # CRESCENT-SLIVER gate (user in-sim report
                        # 2026-07-09, CYXY shapeIDs 447-449): a clip
                        # residue can survive as a ~200 m long ribbon
                        # nowhere wider than a metre or two — it reads
                        # as a spike triangle far outside the visibly
                        # graded area and protects nothing a
                        # neighbouring band does not already cover.
                        # A genuine band slab is at least the 3 m lip
                        # wide somewhere; drop pieces that vanish
                        # under a 0.75 m erosion regardless of area.
                        try:
                            if simple.buffer(-0.75).is_empty:
                                if _ADJACENT_DEBUG:
                                    b = simple.bounds
                                    UI.vprint(1,
                                        f"  [adjacent-debug] dropped "
                                        f"crescent sliver area="
                                        f"{simple.area:.1f} bbox={b}")
                                continue
                        except _GEOM_EXC:
                            pass
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
                                        static_union) <= 1.0)
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
                        # BAND-CORNER WELD (site-2 fix): collapse any
                        # clip-minted seam vertex onto a sibling band's
                        # exact corner (within 1 cm, value-agreeing) so
                        # abutting graded_strip bands share the vertex by
                        # construction instead of emitting a 6 mm
                        # near-parallel twin.  Rebuild ``simple`` from the
                        # snapped ring so the emitted polygon and the
                        # per-vertex value/key computation below agree.
                        # (No prior emitted vertices ⇒ the weld is a
                        # structural no-op; skip the per-vertex resample.)
                        if emitted_vertex_cells:
                            _pre_own = [resample_alt(vx, vy, kind)[0]
                                        for vx, vy in piece_ring]
                            welded_ring = _weld_ring_to_prior_bands(
                                piece_ring, _pre_own)
                        else:
                            welded_ring = piece_ring
                        if welded_ring is not piece_ring \
                                and len(welded_ring) >= 3:
                            try:
                                welded_poly = Polygon(welded_ring)
                                if not welded_poly.is_valid:
                                    welded_poly = welded_poly.buffer(0)
                                if (welded_poly.geom_type == "Polygon"
                                        and not welded_poly.is_empty):
                                    simple = welded_poly
                                    piece_ring = _open_coords(simple)
                                    if len(piece_ring) < 3:
                                        continue
                            except _GEOM_EXC:
                                pass
                        keys = [_vertex_key(vx, vy)
                                for vx, vy in piece_ring]
                        resampled = [resample_alt(vx, vy, kind)
                                     for vx, vy in piece_ring]
                        own = [value for value, _ in resampled]
                        weld = [is_weld for _, is_weld in resampled]
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
                             and (k in authority_value_keys
                                  or abs(vertex_value_registry[k] - o)
                                  <= VERTEX_ALT_MERGE_TOL_M))
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
                        # Re-assert adopted AND pavement-weld values
                        # (declaw/rounding must not move a shared-
                        # coordinate agreement or a pavement edge
                        # adoption), then register this piece's values
                        # for later bands.
                        for j, (k, a) in enumerate(zip(keys, adopted)):
                            if a:
                                alts[j] = vertex_value_registry[k]
                            elif weld[j]:
                                alts[j] = own[j]
                                if k not in vertex_value_registry:
                                    vertex_value_registry[k] = own[j]
                            elif k not in vertex_value_registry:
                                vertex_value_registry[k] = alts[j]
                        shape = BuiltShape(
                            polygon=simple, role=ROLE_GRADED_STRIP,
                            ref=_ADJACENT_REF,
                            node_altitudes=alts + [alts[0]])
                        layout.shapes.append(shape)
                        emitted_shapes.append(shape)
                        emitted += 1
                        # Register this band's final vertices + altitudes
                        # so LATER bands weld their clip seams onto this
                        # corner only where the values also agree.
                        _register_emitted_vertices(piece_ring, alts)
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
                sample_dem, static_union, boundary, wall_clip,
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
        # WELD PROTECTION (2026-07-09): a band vertex ON a non-band
        # shape's boundary traces that constrained edge exactly —
        # chord-cutting it diverges the chains and Ruppert-explodes the
        # tile (see decimate_shape_group).  Keeping it is triangle-free.
        from shapely.geometry import box as _box
        from shapely.strtree import STRtree as _STRtree
        _emitted_ids = {id(es) for es in emitted_shapes}
        _static_exteriors = [s.polygon.exterior for s in layout.shapes
                             if s.polygon is not None
                             and not s.polygon.is_empty
                             and s.polygon.geom_type == "Polygon"
                             and id(s) not in _emitted_ids]
        _ext_tree = None
        try:
            _ext_tree = _STRtree(_static_exteriors)
        except _GEOM_EXC:
            _ext_tree = None

        def _on_foreign_boundary(x, y):
            if _ext_tree is None:
                return False
            p = Point(x, y)
            try:
                cand = _ext_tree.query(
                    _box(x - 0.06, y - 0.06, x + 0.06, y + 0.06))
            except _GEOM_EXC:
                return False
            for gi in cand:
                try:
                    if _static_exteriors[gi].distance(p) <= 0.05:
                        return True
                except _GEOM_EXC:
                    continue
            return False

        removed = decimate_shape_group(
            emitted_shapes, Z_TOL_BOUNDARY_M,
            protect_predicate=_on_foreign_boundary)
        if removed:
            UI.vprint(1, f"  [pav-builder] adjacent-ground: decimated "
                         f"{removed} 3D-collinear band vertex(es) "
                         f"(±{Z_TOL_BOUNDARY_M} m).")

    return emitted


def _emit_apron_walls(layout, stations, st_alts, outs, ceil_off, step,
                      sample_dem, static_union, boundary,
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
        # Physical-distance bridge (see _build_cut_bands): an index
        # bridge on long ring edges mints spike walls.
        jx, jy = stations[j]
        cx_, cy_ = stations[cur[-1]]
        if (j - cur[-1] <= 2
                and math.hypot(jx - cx_, jy - cy_) <= 2.5 * step):
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
            if static_union is not None and not static_union.is_empty:
                poly = poly.difference(static_union)
            # Clip the wall out of the just-emitted graded strips'
            # footprint EXACTLY (weld ruling 2026-07-09): a shared
            # boundary welds at shared coordinates; a standoff would
            # leave a groove of raw DEM at the shoulder edge.
            if emitted_union is not None and not emitted_union.is_empty:
                poly = poly.difference(emitted_union)
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
