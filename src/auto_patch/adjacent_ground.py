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
    ROLE_BUILDING,
    ROLE_CROSS_CONNECTOR,
    ROLE_GRADED_STRIP,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RETAINING_WALL,
    ROLE_RUNWAY,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL,
    ROLE_STUB,
    ROLE_TUNNEL_RAMP,
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
# Slice B stage B3 order 3, SCOPE A — taxiway-end WRAP (Noah ruling
# 2026-07-10, site 60.6972471,-135.0608669; O4_ADJACENT_GROUND_END_WRAP,
# default OFF — new coverage = new terrain output, flips at B4).  The
# terrain-facing probe in ``_station_reference`` skips any station whose
# outward ray lands on an existing shape; at a taxiway end that abuts a
# runway-END skirt that skip halts coverage before the taxiway end.  With
# the wrap ON, a TAXIWAY station whose probe lands ONLY on a
# ``runway_end_skirt`` is NOT skipped (the skirt is the JOIN target, not an
# obstruction): the corridor marches around the taxiway end at the family's
# clearance distance, then the exact ``difference(static_union)`` clip +
# ``_snap_ring_to_static`` land the wrap ring ON the skirt chain verbatim
# (shared vertices; the to_osm skirt-tier consensus supplies the value —
# the same identity/adoption bands read pavement with today).  Runways keep
# the END-edge skip (their skirt law owns them); aprons are unaffected.
# (Round-7 review default ON, Noah 2026-07-11 — judged in-sim with the
# slice-B bundle; env var 0 falls back.)
_END_WRAP = os.environ.get("O4_ADJACENT_GROUND_END_WRAP", "1") == "1"
# Slice B stage B3 order 3, SCOPE B — tunnel-ramp STANDOFF (acceptance
# criterion 6; the ledgered SPJC strip-onto-mouth-ramp tears;
# O4_ADJACENT_GROUND_TUNNEL_STANDOFF, default OFF).  Band construction
# excludes a ``_PAVEMENT_GAP_M`` standoff around tunnel mouth pieces
# (``tunnel_ramp`` sloped rects + the ``retaining_wall`` U-walls the tunnel
# portal emits pre-band) — the building/groundside standoff pattern — so a
# strip never welds onto the steep tunnel-mouth floor.  Object-bridge plates
# (``bridge_trench`` / ``bridge_causeway``) are HARD graph members bands
# treat as pavement and are deliberately NOT stood off.  The block is built
# from ``layout.shapes`` at emit entry, before ``_emit_apron_walls`` appends
# its own ``retaining_wall`` pieces, so apron-edge walls are naturally
# excluded (only pre-existing tunnel walls are captured).  Independently
# flippable from the wrap (they may gate differently at B4).
# (Round-7 review default ON, Noah 2026-07-11 — inert everywhere probed;
# fires only where tunnel ramps emit, e.g. the CYUL mapped-portal class.)
_TUNNEL_STANDOFF = os.environ.get(
    "O4_ADJACENT_GROUND_TUNNEL_STANDOFF", "1") == "1"
# CROSSING INFLUENCE ZONE (Phase 1, docs/specs/crossing-terrain-
# ownership.md): every crossing-specific exclusion this module used to
# build itself — the crossing-union branch of the standoff block, the
# road-lane station drop and emit-time lane clip, and the buried-span
# carve-out (O4_ADJACENT_GROUND_WRAP_LANE_EXCLUSION /
# O4_ADJACENT_GROUND_BURIED_BODY_BAND, whose semantics now live in
# ``crossing_terrain``) — is replaced by the ONE zone the pipeline
# publishes pre-solve.  The march drops stations inside it and the
# emitter differences band pieces against it; ``None`` (nothing
# published — no crossings, no depressed road) is byte-inert.
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

# ── Apparatus hit counters (Slice B stage B3 order 2 instrumentation;
# coordinator ruling 2026-07-11: measure the weld / adoption / seam-taper
# apparatus OFF vs ON admission.  Registry identity is expected to retire
# the two VALUE-AGREEMENT rows — value-changing adoptions and band-corner
# weld snaps — to zero, while the seam-taper pin is FOOTPRINT machinery
# (it pins daylight DEPTHS at pavement-partition seams, it never touches
# a value) and must stay unchanged; if it moves at all, investigate.) ──
_APPARATUS_KEYS = (
    "value_changing_adoptions", "band_corner_weld_snaps",
    "seam_taper_flagged_stations", "solved_exact_variable",
    "solved_row_on", "solved_row_interpolated",
    "solved_beyond_coverage", "solved_analytic_fallback",
    "solved_store_missing_shape", "zone_static_keepout_dropped",
    "static_edge_weld_vertices", "wrap_crossing_zone_excluded_stations",
)
_APPARATUS_HITS: dict[str, int] = {}


def _reset_apparatus_hits():
    for _hit_key in _APPARATUS_KEYS:
        _APPARATUS_HITS[_hit_key] = 0


_reset_apparatus_hits()

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
                     at_continuation_seam=None, zone_collect=None,
                     force_full_reach=False):
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
        if force_full_reach:
            # Full-extent coverage grid (ADJACENT_GROUND_FULL_EXTENT_
            # COVERAGE): obstruct every stationed edge to the whole family
            # reach regardless of the worst-case terrain trigger, so the
            # staged zone-row grid bounds any solved-edge cut the emit
            # re-march produces (over-coverage = unused solved variables).
            last = cap - 1e-3
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
            # Zone-row provenance (Slice B stage B3 order 2): (station
            # index, lateral depth) aligned 1:1 with ``outer_pts`` (the
            # loop below appends both in lockstep, and the outer-jump
            # flush fires BEFORE appending) — the free-variable grid the
            # pre-solve constructor admits to the solver.  ``zone_collect``
            # None (the default, and every post-solve caller): dead lists,
            # byte-identical geometry.
            outer_prov: list[tuple[int, float]] = []

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
                if zone_collect is not None:
                    # Collect BEFORE the thin-inner mutations below: the
                    # d0 == 0 inner (weld) row is pavement, never a zone
                    # row — the collector drops it; every d0 > 0 row and
                    # every outer row is free-variable grid.
                    zone_collect(
                        d0,
                        [(e[0], e[1][0], e[1][1]) for e in inner_row],
                        [(pi, px, py, pd)
                         for (px, py), (pi, pd)
                         in zip(outer_pts, outer_prov)])
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
                outer_prov.clear()

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
                outer_prov.append((i, float(off)))
                keep = (not thin_inner) or bool(is_ring_vertex[i])
                inner_row.append((i, (ix, iy),
                                  round(float(ref + co0), 1), keep))
            _flush_segment()
    return out


def _build_fill_bands(edge_stations, edge_alts, outwards, band_caps,
                      floor_depth, band_edges, trigger, step, sample_dem,
                      is_ring_vertex=None, at_continuation_seam=None,
                      zone_collect=None, force_full_reach=False):
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
        if force_full_reach:
            # Full-extent coverage grid (see _build_cut_bands): drop the
            # whole reach so the fill zone-row grid bounds any solved-edge
            # fill the emit re-march produces.
            last = cap
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
            # Zone-row provenance — see ``_build_cut_bands``.
            outer_prov: list[tuple[int, float]] = []

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
                if zone_collect is not None:
                    # See ``_build_cut_bands``: collect before the
                    # thin-inner mutations; the collector drops the
                    # d0 == 0 (pavement weld) inner row itself.
                    zone_collect(
                        d0,
                        [(e[0], e[1][0], e[1][1]) for e in inner_row],
                        [(pi, px, py, pd)
                         for (px, py), (pi, pd)
                         in zip(outer_pts, outer_prov)])
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
                outer_prov.clear()

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
                outer_prov.append((i, float(off)))
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


def _heal_band_tears(ring, alts, weld, tear_max, min_jump,
                     wall_max=CLEARANCE_STATION_STEP_M):
    """Collapse SUB-METRE near-vertical ring edges a band clip/cap leaves
    behind — the TEAR sentinel class (``check_grade._check_adjacent_ground
    _edges``): a ring edge shorter than ``tear_max`` whose two endpoints'
    altitudes differ by more than ``min_jump``.

    SECOND class (``wall_max``, 2026-07-17): a WELD-ADJACENT WALL edge —
    one endpoint on the pavement weld row, the other a zone vertex, edge
    shorter than a station step (``wall_max``) with a jump exceeding both
    ``min_jump`` and the edge length (grade > 100 %).  No corridor slope
    reaches 100 %; the zone value is a resampler / solved-store escape
    riding raw DEM against the pavement row (CYXY apron #677: weld 706.4
    vs zone 712.7 over 4.0 m).  The sub-metre sentinel misses it at emit,
    but any conformance insert splitting the edge mints a flagged
    sub-metre segment.  Weld-adjacent ONLY: a fill band's outer row
    lawfully rides raw DEM, so two NON-weld vertices across a real
    terrain cliff are never collapsed.

    Cause: the ``difference()`` clips and the band END caps can pinch a
    band's INNER (pavement weld) row to within a metre of its OUTER
    (terrain) row while each keeps its own lawful value, minting a
    near-vertical face no lateral corridor slope produces (the lateral law
    tops out at ~5 %, so a >1 m step over <1 m is impossible by
    construction; it is always a clip residue / pinch, never a graded
    row).  The DEM-aware corridor validator accepts the WIDE band edges
    that ride terrain up a hillside, so the ONLY unlawful thing here is the
    sub-metre pinch edge — remove it geometrically.

    Resolution: drop ONE endpoint of each flagged edge, preferring to
    keep a weld vertex (its position is ON the pavement boundary and its
    value is the pavement weld value the conformance pass welds to —
    never move it).  Between two OUTER vertices (a clip-minted
    near-duplicate) drop the bigger spike.  Iterated to a fixed point so
    a collapse that exposes a fresh short edge is healed too.  Returns
    the healed open ring + aligned altitudes (input objects returned
    unchanged when nothing collapses).  NOTE: both classes key on the
    VALUE jump, so the healed footprint is value-dependent — the
    construct-move footprint-equality acceptance neutralizes this heal
    (see ``test_emit_admission_footprints_equal_gate_off``)."""
    n = len(ring)
    if n < 4:
        return ring, alts
    keep = [True] * n
    changed = True
    guard = 0
    while changed and guard < 2 * n:
        guard += 1
        changed = False
        idxs = [i for i in range(n) if keep[i]]
        m = len(idxs)
        if m <= 3:
            break
        for a in range(m):
            i = idxs[a]
            j = idxs[(a + 1) % m]
            ax, ay = ring[i]
            bx, by = ring[j]
            d = math.hypot(bx - ax, by - ay)
            de = abs(float(alts[i]) - float(alts[j]))
            wi, wj = bool(weld[i]), bool(weld[j])
            is_tear = (d < tear_max and de > min_jump)
            # Weld-adjacent wall edge (see docstring): exactly one weld
            # end, station-scale length, grade over 100 %.
            is_wall = (wall_max is not None and (wi != wj)
                       and d < wall_max and de > min_jump and de > d)
            if not (is_tear or is_wall):
                continue
            if wi and not wj:
                drop = j
            elif wj and not wi:
                drop = i
            elif not wi and not wj:
                # Two outer vertices: drop the spike (the one whose value
                # deviates more from its OTHER ring neighbour).
                pi = idxs[(a - 1) % m]
                nj = idxs[(a + 2) % m]
                dev_i = abs(float(alts[i]) - float(alts[pi]))
                dev_j = abs(float(alts[j]) - float(alts[nj]))
                drop = i if dev_i >= dev_j else j
            else:
                # Two weld vertices sub-metre apart at a >1 m step is a
                # pavement-side discontinuity, not a band pinch — leave it
                # (moving a weld vertex would unweld the pavement seam).
                continue
            keep[drop] = False
            changed = True
            break
    if all(keep):
        return ring, alts
    new_ring = [ring[i] for i in range(n) if keep[i]]
    new_alts = [alts[i] for i in range(n) if keep[i]]
    return new_ring, new_alts


def _raster_reach_band_active() -> bool:
    """Whether the rasterized reach band is the active band producer (the
    runtime env ``O4_RASTER_REACH_BAND`` overriding the ``config`` default —
    the exact resolution :func:`building_feasibility.reach_band_unified`
    uses, so the emitter's reconciliation and the band producer agree)."""
    from .config import RASTER_REACH_BAND
    env = os.environ.get("O4_RASTER_REACH_BAND")
    return (env == "1") if env is not None else bool(RASTER_REACH_BAND)


def _heal_emitted_band_tears(emitted_shapes, layout):
    """FINAL tear-heal over the emitted ``graded_strip`` group (2026-07-18).

    The per-piece ``_heal_band_tears`` at emit runs BEFORE the piece's own
    re-deconflict ``difference()`` / remap and before neighbour bands settle,
    so a tear the LATER geometry mints escapes it.  Two classes the raster
    reach band exposes (an apron/junction the tighter, CORRECT ceiling clamps
    ~2 m down, its strips then bridging the resulting step) survive to emit:

      1. WITHIN-STRIP PINCH: a strip clips against a NOT-dropped abutting
         pavement and pinches its own host-weld row and the clip row
         sub-metre apart.
      2. CROSS-STRIP SEAM: two neighbour strips grading off pavements now
         ~2 m apart share a seam node through the emit VALUE CONSENSUS
         (``to_osm``: a shared node carries the mean of the strips touching
         it), so one strip inherits the other's value and tears against its
         own adjacent vertex — a tear invisible in the strip's OWN
         ``node_altitudes`` (it lives only in the shared consensus).

    Under the legacy band the two references sat within the tear jump, so
    neither fired; this is a raster-band reconciliation, not a new law.  The
    ruled resolution is unchanged (``_heal_band_tears`` doctrine: the sub-metre
    near-vertical edge is the ONLY unlawful thing — the DEM-aware corridor
    validator accepts the wide terrain-riding edges — so it is removed
    geometrically).  This pass detects the tear against the EFFECTIVE emitted
    value (the cross-strip shared-coordinate consensus, so class 2 is visible)
    and collapses it by dropping an UNSHARED, non-donor vertex — never a vertex
    another strip or a donor pavement also owns (moving it would un-weld that
    seam).  Runs before the group decimation (healed rings decimate cleanly)
    and before the pipeline's final epsilon-wedge weld (which re-conforms the
    dropped-vertex seam).  Returns the number of strips healed.
    """
    from collections import defaultdict
    from shapely.strtree import STRtree
    from .layout import WELD_DONOR_ROLES
    # RETAINING WALLS protect like donors (no-stacked-nodes unit): a
    # strip vertex welded onto a wall row must never be dropped — the
    # heal's spring-back straightens the strip edge ACROSS the wall
    # face (measured SPJC: a 2.5 m² strip∩wall overlap, zero-tolerance
    # self-overlap invariant).
    donor_ext = [s.polygon.exterior for s in layout.shapes
                 if ((s.role or "") in WELD_DONOR_ROLES
                     or s.role == ROLE_RETAINING_WALL)
                 and s.polygon is not None and not s.polygon.is_empty
                 and s.polygon.geom_type == "Polygon"]
    try:
        tree = STRtree(donor_ext) if donor_ext else None
    except _GEOM_EXC:
        tree = None
    _WELD_TOL_M = 0.05

    def _on_donor(x, y):
        if tree is None:
            return False
        try:
            hit = tree.query_nearest(Point(x, y), max_distance=_WELD_TOL_M)
        except _GEOM_EXC:
            return False
        return len(hit) > 0

    # Cross-strip shared-coordinate consensus (mirrors ``to_osm``'s soft-mean
    # rule): millimetre vertex key -> list of contributing strip values, so a
    # vertex two strips share resolves to the value the patch actually emits
    # there (class 2).  NO-STACKED-NODES (owner ruling 2026-07-19): to_osm
    # hard-merges EVERY same-canonical-point claim into one node whose soft
    # consensus is the mean — there is no "clean wall" split any more, so a
    # same-key spread of ANY size resolves to the mean and is a tear risk
    # against the ring neighbours.
    strip_vals: "defaultdict[tuple, list]" = defaultdict(list)
    strip_rings: list = []
    for sh in emitted_shapes:
        if (sh.ref != _ADJACENT_REF or sh.polygon is None
                or sh.polygon.is_empty
                or sh.polygon.geom_type != "Polygon"
                or sh.node_altitudes is None):
            continue
        try:
            ring = list(sh.polygon.exterior.coords)[:-1]
        except _GEOM_EXC:
            continue
        alts = list(sh.node_altitudes[:len(ring)])
        if len(ring) != len(alts) or len(ring) < 4:
            continue
        keys = [_vertex_key(vx, vy) for (vx, vy) in ring]
        for k, a in zip(keys, alts):
            strip_vals[k].append(float(a))
        strip_rings.append((sh, ring, alts, keys))

    # AUTHORITY value table (no-stacked-nodes hard merge): a strip
    # vertex coincident with any pavement/solver shape EMITS at the
    # authority consensus, not at its own value — the heal must judge
    # tears against the value the patch will actually carry (a strip
    # step of exactly the threshold can emit ABOVE it after adoption:
    # the CYXY #392 shoulder read 1.00 m in strip values but 1.03 m
    # emitted).
    from .layout import SOFT_RECEIVER_ROLES as _SOFT_ROLES
    authority_vals: "defaultdict[tuple, list]" = defaultdict(list)
    for sh in layout.shapes:
        if (sh.role or "") in _SOFT_ROLES:
            continue
        if (sh.polygon is None or sh.polygon.is_empty
                or sh.polygon.geom_type != "Polygon"):
            continue
        try:
            a_ring = list(sh.polygon.exterior.coords)[:-1]
        except _GEOM_EXC:
            continue
        if sh.node_altitudes is not None:
            a_alts = list(sh.node_altitudes[:len(a_ring)])
            if len(a_alts) != len(a_ring):
                continue
        elif sh.altitude is not None:
            a_alts = [float(sh.altitude)] * len(a_ring)
        else:
            continue
        for (vx, vy), a in zip(a_ring, a_alts):
            authority_vals[_vertex_key(vx, vy)].append(float(a))

    def _effective(k, own):
        # to_osm precedence: authority claims win the node outright.
        av = authority_vals.get(k)
        if av:
            return sum(av) / float(len(av))
        vals = strip_vals.get(k)
        if not vals or len(vals) <= 1:
            return own
        # Same-key values ALWAYS intern to one node whose emitted soft
        # consensus is their mean (to_osm hard merge, ruling 2026-07-19).
        return sum(vals) / float(len(vals))

    healed = 0
    for (sh, ring, alts, keys) in strip_rings:
        n = len(ring)
        on_donor = [_on_donor(vx, vy) for (vx, vy) in ring]
        # SHARED = another strip owns this exact coordinate, or a donor
        # pavement edge does: dropping it would un-weld that seam, so it is
        # protected; the tear is resolved by dropping the strip's OWN vertex.
        shared = [on_donor[i] or len(strip_vals.get(keys[i], ())) >= 2
                  for i in range(n)]
        eff = [_effective(keys[i], alts[i]) for i in range(n)]
        keep = [True] * n
        changed = True
        guard = 0
        while changed and guard < 2 * n:
            guard += 1
            changed = False
            idxs = [i for i in range(n) if keep[i]]
            m = len(idxs)
            if m <= 3:
                break
            for a in range(m):
                i = idxs[a]
                j = idxs[(a + 1) % m]
                d = math.hypot(ring[j][0] - ring[i][0],
                               ring[j][1] - ring[i][1])
                de = abs(eff[i] - eff[j])
                if not (d < 0.2 * CLEARANCE_STATION_STEP_M and de > 1.0):
                    continue
                # Drop an UNSHARED endpoint (prefer the larger spike relative
                # to its far ring neighbour); never a shared/donor vertex.
                cand = []
                if not shared[i]:
                    pi = idxs[(a - 1) % m]
                    cand.append((abs(eff[i] - eff[pi]), i))
                if not shared[j]:
                    nj = idxs[(a + 2) % m]
                    cand.append((abs(eff[j] - eff[nj]), j))
                if not cand:
                    continue                       # both protected — leave it
                keep[max(cand)[1]] = False
                changed = True
                break
        if all(keep):
            continue
        new_ring = [ring[i] for i in range(n) if keep[i]]
        new_alts = [alts[i] for i in range(n) if keep[i]]
        if len(new_ring) < 3:
            continue
        try:
            # Interior rings ride along (exterior-only fills the holes).
            poly = Polygon(new_ring + [new_ring[0]],
                           [list(h.coords)
                            for h in sh.polygon.interiors])
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.is_empty or poly.geom_type != "Polygon":
                continue
        except _GEOM_EXC:
            continue
        rebuilt = _open_coords(poly)
        if len(rebuilt) < 3:
            continue
        if len(rebuilt) == len(new_ring):
            out_alts = new_alts
        else:
            out_alts = [round(float(_nearest_alt(
                new_ring, new_alts, vx, vy)), 1) for vx, vy in rebuilt]
        sh.polygon = poly
        sh.node_altitudes = out_alts + [out_alts[0]]
        healed += 1
    return healed


# Cross-strip SEAM-STEP blend thresholds (2026-07-18, SPJC east-side
# cliffs): two strips grading off DIFFERENT host pavements run side by
# side, and their near-adjacent — but not coordinate-shared — boundary
# vertices disagree by the host delta.  Measured SPJC: 152 pairs, worst
# 4.4 m over 1.26 m (348 %), at 1-6 m spacing — ABOVE the sub-metre
# pinch class ``_heal_emitted_band_tears`` collapses, and invisible to
# it because each tear spans TWO rings.  Legitimate terracing steps are
# ~0.3 m, so a 1.0 m step floor cleanly separates the classes.
SEAM_STEP_RADIUS_M = 6.0
SEAM_STEP_MIN_DELTA_M = 1.0
# Grade floor: on steep relief (CYXY) neighbour strips LEGITIMATELY
# differ by >1 m at 4-6 m spacing — real hillside drape tops out around
# 30-40 %, while genuine seam cliffs run 100-350 %.  Requiring the step
# to ALSO imply >50 % keeps lawful terrain-following untouched (measured
# CYXY without the floor: 1404 vertices moved, 5 new sub-metre pinches
# minted; with it the blend touches only true cliffs).
SEAM_STEP_MIN_GRADE = 0.5


def blend_cross_strip_seam_steps(strip_shapes, layout):
    """Reconcile metre-scale value steps between NEAR-ADJACENT vertices of
    different ``graded_strip`` shapes (2026-07-18, SPJC in-sim cliffs).

    Model: ``to_osm`` interns same-millimetre-key vertices whose values
    sit within the emit merge tolerance into ONE node, so the blend
    operates on that LOGICAL node graph — a logical node (the key plus
    its agreeing twins across strips) moves as a unit; same-key vertices
    whose spread EXCEEDS the tolerance emit as stacked separate nodes (a
    bare vertical terrain wall, the SPJC 3.8 m class) and are separate
    logical nodes that may blend against each other.  Qualifying pairs —
    planar distance under ``SEAM_STEP_RADIUS_M`` (zero included: the
    stacked-wall case), altitude delta over ``SEAM_STEP_MIN_DELTA_M``,
    not both exclusively owned by the same single strip — are clustered
    by union-find.  Donor-pavement-welded logical nodes are immovable
    ANCHORS; free logical nodes snap to the anchors\' mean, or to the
    cluster mean when no anchor exists.  A cluster whose every node is
    anchored is left alone (a genuine step — retaining-wall territory,
    never silently flattened).  ``strip_shapes`` must be the COMPLETE
    final strip population (every emitter: adjacent-ground bands,
    gap-fill spines) — running per emitter group misses exactly the
    cross-family seams that tear.  Returns the number of ring vertices
    re-levelled."""
    from collections import defaultdict
    from shapely.strtree import STRtree
    from .crown import _point_in_seam_band
    from .layout import WELD_DONOR_ROLES

    donor_ext = [s.polygon.exterior for s in layout.shapes
                 if (s.role or "") in WELD_DONOR_ROLES
                 and s.polygon is not None and not s.polygon.is_empty
                 and s.polygon.geom_type == "Polygon"]
    try:
        donor_tree = STRtree(donor_ext) if donor_ext else None
    except _GEOM_EXC:
        donor_tree = None

    # Per-strip vertex tables.
    entries = []            # (shape, ring, alts) — alts open (no closing twin)
    by_key: "defaultdict[tuple, list]" = defaultdict(list)
    for sh in strip_shapes:
        if ((sh.role or "") != "graded_strip" or sh.polygon is None
                or sh.polygon.is_empty
                or sh.polygon.geom_type != "Polygon"
                or sh.node_altitudes is None):
            continue
        try:
            ring = list(sh.polygon.exterior.coords)[:-1]
        except _GEOM_EXC:
            continue
        alts = [float(a) for a in sh.node_altitudes[:len(ring)]]
        if len(ring) != len(alts) or len(ring) < 4:
            continue
        entry_index = len(entries)
        entries.append((sh, ring, alts))
        for position, (vx, vy) in enumerate(ring):
            by_key[_vertex_key(vx, vy)].append(
                (entry_index, position, alts[position], vx, vy))
    if len(entries) < 2:
        return 0

    # LOGICAL nodes: per key, greedy-cluster members by value within the
    # merge tolerance (ascending — deterministic).  Same-key groups whose
    # values disagree beyond it become SEPARATE logical nodes (stacked
    # wall) and may blend against each other at distance zero.
    logical = []    # dict: members [(entry,pos)], strips set, value, xy
    for key in sorted(by_key):
        members = sorted(by_key[key], key=lambda m: (m[2], m[0], m[1]))
        group: list = []
        for member in members:
            if group and member[2] - group[0][2] > VERTEX_ALT_MERGE_TOL_M:
                logical.append(group)
                group = []
            group.append(member)
        if group:
            logical.append(group)
    node_xy = []
    node_value = []
    node_strips = []
    for group in logical:
        node_xy.append((group[0][3], group[0][4]))
        node_value.append(sum(m[2] for m in group) / float(len(group)))
        node_strips.append({m[0] for m in group})

    def _anchored(node_index):
        (vx, vy) = node_xy[node_index]
        # TILE-SEAM protection (2026-07-18 SPLP regression): seam-band
        # vertices are cross-tile terrain contracts — each tile builds
        # independently with a different strip population, so a blended
        # seam value diverges between neighbour tiles and emits a step
        # AT the tile boundary (measured: two -78-side vertices moved
        # +2.00 m against the immutable seam DEM).  Same predicate the
        # crown and tile_cut use.
        if _point_in_seam_band(layout, vx, vy):
            return True
        if donor_tree is None:
            return False
        try:
            hit = donor_tree.query_nearest(Point(vx, vy), max_distance=0.05)
        except _GEOM_EXC:
            return False
        return len(hit) > 0

    points = [Point(vx, vy) for (vx, vy) in node_xy]
    try:
        vertex_tree = STRtree(points)
        left, right = vertex_tree.query(points, predicate="dwithin",
                                        distance=SEAM_STEP_RADIUS_M)
    except _GEOM_EXC:
        return 0
    parent = list(range(len(points)))

    def _find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    paired = False
    for a, b in zip(left.tolist(), right.tolist()):
        if a >= b:
            continue
        delta = abs(node_value[a] - node_value[b])
        if delta < SEAM_STEP_MIN_DELTA_M:
            continue
        (ax, ay), (bx, by) = node_xy[a], node_xy[b]
        planar = math.hypot(ax - bx, ay - by)
        if delta < SEAM_STEP_MIN_GRADE * max(planar, 0.01):
            continue        # steep-terrain drape, not a cliff
        if (len(node_strips[a]) == 1 and node_strips[a] == node_strips[b]):
            continue        # within one ring: the pinch healer\'s domain
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
        paired = True
    if not paired:
        return 0

    clusters: "defaultdict[int, list]" = defaultdict(list)
    for node_index in range(len(points)):
        clusters[_find(node_index)].append(node_index)

    moved = 0
    changed_entries: set = set()
    for root in sorted(clusters):
        members = clusters[root]
        if len(members) < 2:
            continue                # never paired: not a seam cluster
        anchors = [m for m in members if _anchored(m)]
        free_nodes = [m for m in members if m not in set(anchors)]
        if not free_nodes:
            continue                # all anchored: genuine step, leave it
        source = anchors if anchors else members
        target = round(
            sum(node_value[m] for m in source) / float(len(source)), 2)
        for m in free_nodes:
            if abs(node_value[m] - target) < 1e-9:
                continue
            node_value[m] = target
            for (entry_index, position, _a, _x, _y) in logical[m]:
                entries[entry_index][2][position] = target
                changed_entries.add(entry_index)
                moved += 1
    # Write back ONLY the touched strips — untouched shapes keep their
    # exact original altitude lists (byte-identity everywhere no seam
    # cluster exists).
    for entry_index in sorted(changed_entries):
        (sh, _ring, alts) = entries[entry_index]
        sh.node_altitudes = list(alts) + [alts[0]]
    return moved


# ── Stacked-conflict wall emission (owner ruling 2026-07-19) ──────
# NO-STACKED-NODES INVARIANT: the emitter hard-merges every coincident
# vertex into ONE node with ONE consensus elevation (layout.to_osm),
# so a strip vertex that coincides with a designed-split authority
# corner (building pad, service road, terminal, groundside — the
# WELD_DONOR_ROLES complement) can no longer render its level change
# as a stacked "clean wall" twin.  This pass resolves the residue AS
# GEOMETRY before emit: the strip's conflicting boundary run retreats
# horizontally into its own interior, and a ``retaining_wall`` face
# fills the vacated band — top row ON the old boundary at the
# authority values (welds to the designed shape's chain), bottom row
# at the retreated strip edge at the strip's own values.  The level
# change survives as deliberate, horizontally-extended wall geometry.
#
# The retreat must exceed the emitter's canonical-point proximity
# tolerance (SHARED_VERTEX_TOL_M = 0.5 m) or the moved vertex would
# re-intern into the very node it retreats from.
STACKED_WALL_RETREAT_M = 0.6
# Run-extension floor: ring neighbours of a primary conflict join the
# wall run whenever they are coincident with the authority boundary at
# all (any spread above emit-rounding noise) — the run must terminate
# at a NON-coincident vertex, where the strip's own drape is
# continuous.  Terminating at a still-coincident neighbour leaves that
# neighbour silently merged UP to the authority value beside a
# retreated vertex at the strip's own value: a shoulder step of the
# full conflict height (the CYXY #392 tear, 1.03 m over 0.99 m,
# survived a 0.3 m floor exactly this way — the shoulder's own spread
# was small but its merged value was not).
STACKED_WALL_TAPER_MIN_M = 0.05


def emit_stacked_conflict_walls(layout) -> int:
    """Resolve strip-vs-authority coincident level conflicts as offset
    wall geometry (owner ruling 2026-07-19: nodes are never stacked; a
    genuine level change is horizontal wall geometry).

    For every ``graded_strip`` ring vertex whose canonical point also
    carries a NON-donor authority claim differing by more than
    ``VERTEX_ALT_MERGE_TOL_M``: retreat the strip vertex
    ``STACKED_WALL_RETREAT_M`` into the strip's interior and emit a
    ``retaining_wall`` face over the vacated band.  Tile-seam-band
    vertices are cross-tile contracts and are never moved (their
    conflicts fall back to the emit consensus merge).  Returns the
    number of wall faces emitted.
    """
    from .crown import _point_in_seam_band
    from .layout import (
        SOFT_RECEIVER_ROLES, WELD_DONOR_ROLES, corner_alts_from_high_low)

    registry = getattr(layout, "canonical_points", None)
    if registry is None:
        return 0

    def _ring_values(shape):
        """Open exterior ring + aligned per-vertex values, mirroring
        ``to_osm``'s derivation (node_altitudes > flat altitude
        broadcast > sloped-rect high/low corners).  None when the
        shape carries no elevation or the lists misalign."""
        poly = shape.polygon
        if (poly is None or poly.is_empty
                or poly.geom_type != "Polygon"):
            return None
        try:
            coords = list(poly.exterior.coords)
        except _GEOM_EXC:
            return None
        if len(coords) > 1 and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) < 3:
            return None
        if shape.node_altitudes is not None:
            alts = list(shape.node_altitudes)
            if len(alts) == len(coords) + 1:
                alts = alts[:-1]
            if len(alts) != len(coords):
                return None
            return coords, [float(a) for a in alts]
        if shape.altitude is not None:
            return coords, [float(shape.altitude)] * len(coords)
        if (shape.altitude_high is not None
                and shape.altitude_low is not None
                and len(coords) == 4):
            return coords, corner_alts_from_high_low(
                float(shape.altitude_high), float(shape.altitude_low))
        return None

    # Authority claim table over canonical points: value claims from
    # NON-donor, non-soft shapes (the designed-split classes whose
    # corners the strip adoption never welded to).  Donor-pavement and
    # soft claims merge fine through the emit consensus and need no
    # wall.  Alongside the vertex table, keep each shape's exterior +
    # aligned values for the EDGE-coincident case: a strip boundary
    # clipped along a designed shape shares the CHAIN, not necessarily
    # the vertices — its vertices lie ON the foreign edge, the weld
    # pass splices them into it, and the consensus then bends the
    # strip mid-edge (the CYXY #392 service-road class).
    from shapely.strtree import STRtree
    authority_claims: dict = {}
    authority_edges: list = []      # (LineString exterior, coords, alts)
    # Soft strip-vs-strip claims (the ALL-ANCHORED residue): the seam
    # blend levels every FREE vertex first, so a coincident strip-strip
    # spread still exceeding the merge tolerance afterwards is two
    # host-welded (anchored) strips holding a genuine level change —
    # exactly the class the ruling turns into wall geometry.  The
    # LOWER strip retreats; the top holder keeps the point.
    strip_claims: dict = {}
    strip_edges: list = []   # (exterior, coords, alts, id(shape))
    for shape in layout.shapes:
        role = shape.role or ""
        if role == ROLE_GRADED_STRIP:
            rv = _ring_values(shape)
            if rv is None:
                continue
            coords, alts = rv
            for (vx, vy), value in zip(coords, alts):
                key = registry.get_or_add(float(vx), float(vy))
                strip_claims.setdefault(key, []).append(
                    (value, id(shape)))
            try:
                strip_edges.append(
                    (shape.polygon.exterior, coords, alts, id(shape)))
            except _GEOM_EXC:
                pass
            continue
        if role in SOFT_RECEIVER_ROLES or role in WELD_DONOR_ROLES:
            continue
        rv = _ring_values(shape)
        if rv is None:
            continue
        coords, alts = rv
        for (vx, vy), value in zip(coords, alts):
            key = registry.get_or_add(float(vx), float(vy))
            authority_claims.setdefault(key, []).append(value)
        try:
            authority_edges.append(
                (shape.polygon.exterior, coords, alts))
        except _GEOM_EXC:
            pass

    if (not authority_claims and not authority_edges
            and not strip_claims):
        return 0
    edge_tree = None
    if authority_edges:
        try:
            edge_tree = STRtree([e[0] for e in authority_edges])
        except _GEOM_EXC:
            edge_tree = None
    _EDGE_COINCIDE_TOL_M = 0.01

    def _edge_conflict_value(vx, vy):
        """Authority value at (vx, vy) when the point lies ON a
        non-donor authority exterior (edge-interpolated); None when no
        edge passes through the point."""
        if edge_tree is None:
            return None
        try:
            idxs = edge_tree.query_nearest(
                Point(vx, vy), max_distance=_EDGE_COINCIDE_TOL_M)
        except _GEOM_EXC:
            return None
        if idxs is None or len(idxs) == 0:
            return None
        exterior, coords_a, alts_a = authority_edges[int(idxs[0])]
        # Walk the ring segments for the projection and interpolate
        # the two segment-end values.
        best = None
        na = len(coords_a)
        for i in range(na):
            ax, ay = coords_a[i]
            bx, by = coords_a[(i + 1) % na]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            if L2 < 1e-12:
                continue
            t = ((vx - ax) * dx + (vy - ay) * dy) / L2
            t = min(1.0, max(0.0, t))
            px, py = ax + dx * t, ay + dy * t
            d = math.hypot(vx - px, vy - py)
            if best is None or d < best[0]:
                best = (d, (1.0 - t) * alts_a[i]
                        + t * alts_a[(i + 1) % na])
        if best is None or best[0] > _EDGE_COINCIDE_TOL_M:
            return None
        return best[1]

    strip_edge_tree = None
    if strip_edges:
        try:
            strip_edge_tree = STRtree([e[0] for e in strip_edges])
        except _GEOM_EXC:
            strip_edge_tree = None

    def _interp_on_ring(coords_a, alts_a, vx, vy):
        """Edge-interpolated value of a ring at (vx, vy), or None when
        the point is farther than the coincidence tolerance."""
        best = None
        na = len(coords_a)
        for i in range(na):
            ax, ay = coords_a[i]
            bx, by = coords_a[(i + 1) % na]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            if L2 < 1e-12:
                continue
            t = ((vx - ax) * dx + (vy - ay) * dy) / L2
            t = min(1.0, max(0.0, t))
            px, py = ax + dx * t, ay + dy * t
            d = math.hypot(vx - px, vy - py)
            if best is None or d < best[0]:
                best = (d, (1.0 - t) * alts_a[i]
                        + t * alts_a[(i + 1) % na])
        if best is None or best[0] > _EDGE_COINCIDE_TOL_M:
            return None
        return best[1]

    def _strip_edge_conflict_value(vx, vy, own_id):
        """Highest OTHER-strip chain value at (vx, vy) when the point
        lies on another strip's exterior; None otherwise."""
        if strip_edge_tree is None:
            return None
        try:
            idxs = strip_edge_tree.query(
                Point(vx, vy), predicate="dwithin",
                distance=_EDGE_COINCIDE_TOL_M)
        except _GEOM_EXC:
            return None
        best = None
        for k in idxs:
            exterior, coords_a, alts_a, sid = strip_edges[int(k)]
            if sid == own_id:
                continue
            val = _interp_on_ring(coords_a, alts_a, vx, vy)
            if val is not None and (best is None or val > best):
                best = val
        return best

    emitted = 0
    new_walls: list = []
    for shape in layout.shapes:
        if shape.role != ROLE_GRADED_STRIP:
            continue
        rv = _ring_values(shape)
        if rv is None or shape.node_altitudes is None:
            continue
        coords, alts = rv
        n = len(coords)
        if n < 4:
            continue
        # Coincidence spread per vertex (vertex- or edge-coincident with
        # a non-donor authority).  PRIMARY conflicts (spread beyond the
        # merge tolerance) seed wall runs; each run then EXTENDS along
        # ring neighbours holding smaller-but-real spreads
        # (> STACKED_WALL_TAPER_MIN_M) so the level change tapers
        # INSIDE the wall face — without the extension, the run's
        # shoulder (a neighbour that merged silently) steps against the
        # first retreated vertex by the full conflict height (the CYXY
        # #392 shoulder tear).
        coincident_top: list = [None] * n
        spread: list = [0.0] * n
        for i, ((vx, vy), own) in enumerate(zip(coords, alts)):
            key = registry.get_or_add(float(vx), float(vy))
            claims = authority_claims.get(key)
            if claims:
                top = sum(claims) / len(claims)
            else:
                top = _edge_conflict_value(vx, vy)
            if top is None:
                # Soft strip-vs-strip cluster: this strip retreats only
                # when another strip holds a HIGHER value at the point
                # (the top holder keeps the weld; equal values are the
                # ordinary shared seam).
                cluster = strip_claims.get(key)
                if cluster and len(cluster) > 1:
                    others_max = max(
                        (v for (v, sid) in cluster if sid != id(shape)),
                        default=None)
                    if others_max is not None and others_max > own:
                        top = others_max
                if top is None:
                    # Edge-coincident soft case: this vertex lies mid-
                    # edge on a HIGHER strip's chain (the weld would
                    # splice a valley node into that chain).
                    se = _strip_edge_conflict_value(vx, vy, id(shape))
                    if se is not None and se > own:
                        top = se
                if top is None:
                    continue
            sp = abs(top - own)
            if sp <= 0.05:
                continue
            if _point_in_seam_band(layout, vx, vy):
                continue  # cross-tile contract — consensus merge only
            coincident_top[i] = top
            spread[i] = sp
        primary = [i for i in range(n)
                   if spread[i] > VERTEX_ALT_MERGE_TOL_M]
        if not primary:
            continue
        selected: set = set(primary)
        for i in primary:
            for step in (1, -1):
                j = i
                while True:
                    j = (j + step) % n
                    if (j in selected
                            or coincident_top[j] is None
                            or spread[j] <= STACKED_WALL_TAPER_MIN_M):
                        break
                    selected.add(j)
        conflict_top = [coincident_top[i] if i in selected else None
                        for i in range(n)]
        # Inward retreat per conflict vertex: candidate normals are the
        # ±perpendicular of the adjacent-edge mean direction; keep the
        # one whose probe point lands inside the strip.
        poly = shape.polygon
        moved_pos: list = [None] * n
        for i in range(n):
            if conflict_top[i] is None:
                continue
            ax, ay = coords[(i - 1) % n]
            bx, by = coords[i]
            cx, cy = coords[(i + 1) % n]
            tx, ty = (cx - ax), (cy - ay)
            norm = math.hypot(tx, ty)
            if norm < 1e-9:
                continue
            tx, ty = tx / norm, ty / norm
            for (nx_, ny_) in ((-ty, tx), (ty, -tx)):
                px = bx + nx_ * STACKED_WALL_RETREAT_M
                py = by + ny_ * STACKED_WALL_RETREAT_M
                try:
                    if poly.contains(Point(px, py)):
                        moved_pos[i] = (px, py)
                        break
                except _GEOM_EXC:
                    continue
        run_indices = [i for i in range(n) if moved_pos[i] is not None]
        if not run_indices:
            continue
        # Group into consecutive ring runs (wrap-aware).
        runs: list[list[int]] = []
        current = [run_indices[0]]
        for i in run_indices[1:]:
            if (i - current[-1]) % n == 1:
                current.append(i)
            else:
                runs.append(current)
                current = [i]
        runs.append(current)
        if (len(runs) > 1 and runs[0][0] == 0
                and (runs[-1][-1] + 1) % n == 0):
            runs[0] = runs[-1] + runs[0]
            runs.pop()
        # Build one wall face per run; commit the retreat only for the
        # runs whose face built cleanly, and only if the retreated ring
        # stays a valid simple polygon (else the whole shape falls back
        # to the emit consensus merge, walls withdrawn).
        new_coords = list(coords)
        new_alts = list(alts)
        shape_walls: list = []
        for run in runs:
            top_pts = [coords[i] for i in run]
            top_alts_run = [round(float(conflict_top[i]), 1) for i in run]
            bot_pts = [moved_pos[i] for i in run]
            bot_alts_run = [round(float(alts[i]), 1) for i in run]
            # Pinch the wall closed with the ring neighbours on the TOP
            # row (unmoved, at the strip's own value — zero face height
            # at the taper stations).
            prev_i = (run[0] - 1) % n
            next_i = (run[-1] + 1) % n
            ring_pts = ([coords[prev_i]] + top_pts + [coords[next_i]]
                        + bot_pts[::-1])
            ring_alts = ([round(float(alts[prev_i]), 1)] + top_alts_run
                         + [round(float(alts[next_i]), 1)]
                         + bot_alts_run[::-1])
            try:
                wall_poly = Polygon(ring_pts)
                if not wall_poly.is_valid:
                    wall_poly = wall_poly.buffer(0)
                if (wall_poly.is_empty
                        or wall_poly.geom_type != "Polygon"):
                    continue
            except _GEOM_EXC:
                continue
            rebuilt = _open_coords(wall_poly)
            if len(rebuilt) < 3:
                continue
            if len(rebuilt) == len(ring_pts):
                wall_alts = ring_alts
            else:
                wall_alts = [round(float(_nearest_alt(
                    ring_pts, ring_alts, vx, vy)), 1)
                    for (vx, vy) in rebuilt]
            shape_walls.append(BuiltShape(
                polygon=wall_poly, role=ROLE_RETAINING_WALL,
                ref="stacked_conflict_wall",
                node_altitudes=wall_alts + [wall_alts[0]]))
            for i in run:
                new_coords[i] = moved_pos[i]
        if not shape_walls:
            continue
        try:
            # Interior rings ride along (exterior-only fills the holes).
            moved_poly = Polygon(new_coords + [new_coords[0]],
                                 [list(h.coords)
                                  for h in shape.polygon.interiors])
            if not moved_poly.is_valid:
                moved_poly = moved_poly.buffer(0)
            if (moved_poly.is_empty
                    or moved_poly.geom_type != "Polygon"
                    or len(_open_coords(moved_poly)) != n):
                continue  # retreat degenerated the ring — fall back
            shape.polygon = moved_poly
            rebuilt_open = _open_coords(moved_poly)
            if rebuilt_open != new_coords:
                # buffer(0) may rotate the ring start; re-map the
                # altitude list to the rebuilt vertex order.
                new_alts = [float(_nearest_alt(
                    new_coords, new_alts, vx, vy))
                    for (vx, vy) in rebuilt_open]
            shape.node_altitudes = (
                [round(a, 2) for a in new_alts]
                + [round(new_alts[0], 2)])
        except _GEOM_EXC:
            continue
        # Clip each wall out of the RETREATED strip's footprint (same
        # discipline as ``_emit_apron_walls``): on a concave boundary
        # the wedge between the old and new edges can lap onto strip
        # area the retreat kept — the zero-tolerance self-overlap
        # invariant forbids any lap.
        clipped_walls: list = []
        for wall in shape_walls:
            try:
                clipped = wall.polygon.difference(shape.polygon)
                if clipped.is_empty:
                    continue
                if clipped.geom_type == "MultiPolygon":
                    clipped = max(clipped.geoms, key=lambda g: g.area)
                if (clipped.geom_type != "Polygon"
                        or clipped.is_empty or clipped.area < 1e-6):
                    continue
                pr = _open_coords(clipped)
                if len(pr) < 3:
                    continue
                src_ring = list(wall.polygon.exterior.coords)[:-1]
                src_alts = wall.node_altitudes[:len(src_ring)]
                walts = [round(float(_nearest_alt(
                    src_ring, src_alts, vx, vy)), 1) for (vx, vy) in pr]
                wall.polygon = clipped
                wall.node_altitudes = walts + [walts[0]]
                clipped_walls.append(wall)
            except _GEOM_EXC:
                continue
        new_walls.extend(clipped_walls)
        emitted += len(clipped_walls)
    layout.shapes.extend(new_walls)
    return emitted


def _ring_edge_reference(coords, ring_alts):
    """The shared ring linear-reference (code motion out of
    ``_make_edge_projection_resampler``, Slice B stage B3 order 2 — the
    solved-surface resampler needs the identical weld-row detection and
    pavement-edge read): returns ``(line, edge_alt_at)`` where ``line``
    is the closed ring ``LineString`` and ``edge_alt_at(s)`` the
    pavement edge altitude at arc length ``s``."""
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

    return line, _edge_alt_at


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
    line, _edge_alt_at = _ring_edge_reference(coords, ring_alts)

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


def _make_solved_band_resampler(entry, coords, ring_alts,
                                analytic_resample):
    """Slice B stage B3 order 2 GATE-ON band valuation: every emitted
    band vertex reads the SOLVED band surface instead of the analytic
    corridor clamp (which runs only gate-OFF — the valuation dies
    gate-ON).  ``entry`` is this shape's construct-store record
    (``zone_rows`` + the writeback's ``zone_values``); ``coords`` /
    ``ring_alts`` are the FINAL solved pavement ring the emit-time march
    ran on.

    Value rule, in precedence order (coordinator ruling 2026-07-11):
      1. WELD ROW (lateral distance <= 2 cm): the pavement edge value
         verbatim — pavement identity, unchanged from the analytic path.
      2. EXACT VARIABLE: a vertex within the canonical-registry
         tolerance (0.5 m) of a solved zone node takes that variable's
         solved value — registry identity, the mechanism that retires
         the adoption/weld apparatus.
      3. ROW INTERPOLATION: otherwise the vertex takes LINEAR
         INTERPOLATION ALONG THE SOLVED ROW it lands on (arc-length
         weighted between the bracketing solver-valued stations —
         the ruled clip-minted-vertex valuation), and where it sits
         BETWEEN two rows, the depth-weighted blend of the two rows'
         interpolants (the value the solved, piecewise-linear band
         surface already implies at that point; the pavement edge
         itself serves as the depth-0 row).  Sound BECAUSE the band law
         has no neighbour coupling — every zone value is independent,
         so regrouping independently-valued vertices onto the emit-time
         footprint cannot violate anything (the order-2 scout
         refutation, ratified).
      4. NO SOLVED ROW in range (an emit-time run the pre-solve
         estimate did not cover): the analytic resampler values the
         vertex and the case is COUNTED loudly
         (``solved_analytic_fallback`` — the B2 degrade convention).
    """
    line, _edge_alt_at = _ring_edge_reference(coords, ring_alts)
    zone_values = entry.get("zone_values") or {}
    # Exact-variable spatial hash at the canonical registry tolerance.
    _EXACT_CELL_M = 0.5
    exact_cells: dict[tuple[int, int],
                      list[tuple[float, float, float]]] = {}
    rows_prepared: list[dict] = []
    for row in entry.get("zone_rows", ()):
        pts = row.get("pts") or []
        vals = [zone_values.get(_vertex_key(px, py)) for px, py in pts]
        keep = [(p, v) for p, v in zip(pts, vals) if v is not None]
        if not keep:
            continue
        for (px, py), v in keep:
            cx = int(math.floor(px / _EXACT_CELL_M))
            cy = int(math.floor(py / _EXACT_CELL_M))
            exact_cells.setdefault((cx, cy), []).append((px, py, v))
        if len(keep) < 2:
            continue
        row_pts = [p for p, _v in keep]
        row_vals = [v for _p, v in keep]
        cum = [0.0]
        for i in range(len(row_pts) - 1):
            cum.append(cum[-1] + math.hypot(
                row_pts[i + 1][0] - row_pts[i][0],
                row_pts[i + 1][1] - row_pts[i][1]))
        try:
            row_line = LineString(row_pts)
        except _GEOM_EXC:
            continue
        depths = row.get("depths") or []
        row_depths = [d for (_p, _v), d in zip(zip(pts, vals), depths)
                      if _v is not None]
        if len(row_depths) != len(row_pts):
            row_depths = [row.get("d0", 0.0)] * len(row_pts)
        rows_prepared.append({"kind": row["kind"], "line": row_line,
                              "cum": cum, "vals": row_vals,
                              "depths": row_depths})

    def _lerp_along(cum, vals, s):
        k = bisect.bisect_right(cum, s) - 1
        k = max(0, min(k, len(cum) - 2))
        seg = cum[k + 1] - cum[k]
        t = 0.0 if seg <= 0 else (s - cum[k]) / seg
        return vals[k] + t * (vals[k + 1] - vals[k])

    # Beyond this lateral distance a row is not a plausible value source
    # for the query (reach-scale sanity bound).
    _ROW_RANGE_M = 30.0
    # A projected row depth within this of the query's own lateral
    # distance means the query sits ON that row (the on-edge lerp case).
    _ON_ROW_TOL_M = 0.5

    def resample(x, y, kind):
        p = Point(x, y)
        d = p.distance(line)
        if d <= 0.02:
            # Weld row: pavement edge value verbatim (identity).
            edge_alt = _edge_alt_at(line.project(p))
            if edge_alt is None:
                return (0.0, False)
            return (float(edge_alt), True)
        # 1) exact variable adoption (registry-tolerance hash).
        cx = int(math.floor(x / _EXACT_CELL_M))
        cy = int(math.floor(y / _EXACT_CELL_M))
        best_v = None
        best_d = _EXACT_CELL_M
        for ox in (cx - 1, cx, cx + 1):
            for oy in (cy - 1, cy, cy + 1):
                for px, py, v in exact_cells.get((ox, oy), ()):
                    dd = math.hypot(x - px, y - py)
                    if dd < best_d:
                        best_d, best_v = dd, v
        if best_v is not None:
            _APPARATUS_HITS["solved_exact_variable"] += 1
            return (round(float(best_v), 1), False)
        # 2) row interpolation on the solved band surface.
        candidates = []
        for row in rows_prepared:
            if row["kind"] != kind:
                continue
            try:
                s = row["line"].project(p)
                dist = row["line"].distance(p)
            except _GEOM_EXC:
                continue
            if dist > _ROW_RANGE_M:
                continue
            candidates.append(
                (dist, _lerp_along(row["cum"], row["depths"], s),
                 _lerp_along(row["cum"], row["vals"], s)))
        if not candidates:
            _APPARATUS_HITS["solved_analytic_fallback"] += 1
            return analytic_resample(x, y, kind)
        candidates.sort(key=lambda c: c[0])
        prim_dist, prim_depth, prim_val = candidates[0]
        if abs(prim_depth - d) <= _ON_ROW_TOL_M:
            _APPARATUS_HITS["solved_row_on"] += 1
            return (round(float(prim_val), 1), False)
        below = None      # deepest row not deeper than the query
        above = None      # shallowest row not shallower than the query
        for dist, depth, val in candidates:
            if depth <= d and (below is None or dist < below[0]):
                below = (dist, depth, val)
            if depth >= d and (above is None or dist < above[0]):
                above = (dist, depth, val)
        if below is None:
            # The pavement edge is the depth-0 row.
            edge_alt = _edge_alt_at(line.project(p))
            if edge_alt is None:
                _APPARATUS_HITS["solved_analytic_fallback"] += 1
                return analytic_resample(x, y, kind)
            below = (0.0, 0.0, float(edge_alt))
        if above is None:
            # Outward of the deepest solved row: clamp to it (counted —
            # the geometry is reported rather than a fallback invented).
            _APPARATUS_HITS["solved_beyond_coverage"] += 1
            return (round(float(below[2]), 1), False)
        span = above[1] - below[1]
        t = 0.0 if span <= 0 else (d - below[1]) / span
        value = below[2] + t * (above[2] - below[2])
        _APPARATUS_HITS["solved_row_interpolated"] += 1
        return (round(float(value), 1), False)

    return resample


def _runway_end_skirt_prep(layout):
    """Prepared union of the runway-END skirt polygons, or ``None`` — the
    WRAP join target (scope A).  Skirts carry ref ``runway_end_skirt`` on
    the ``runway_clearance`` role.  Prepared once per emission/construction
    and passed to the taxiway march so the terrain-facing probe can tell a
    skirt (join) from real pavement (skip)."""
    polys = [s.polygon for s in layout.shapes
             if getattr(s, "ref", None) == "runway_end_skirt"
             and s.polygon is not None and not s.polygon.is_empty]
    if not polys:
        return None
    try:
        return prep(unary_union(polys))
    except _GEOM_EXC:
        return None


def _crossing_zone_union(layout):
    """The published crossing influence zone union, or ``None`` (Phase 1,
    docs/specs/crossing-terrain-ownership.md).  Replaces every crossing
    reconstruction this module used to do itself: the crossing-union
    branch of the standoff block, the road-lane exclusion, and the
    buried-span carve-out (the buried roof is bandable BY CONSTRUCTION —
    the published zone contains only the road bore over the buried span)."""
    from .crossing_terrain import crossing_influence_zone_union
    return crossing_influence_zone_union(layout)


def _crossing_zone_prep(layout):
    """Prepared form of the published crossing zone for the march's
    station test, or ``None``."""
    from .crossing_terrain import crossing_influence_zone_prepared
    return crossing_influence_zone_prepared(layout)


def _tunnel_ramp_standoff_block(layout):
    """1 m buffered union of the tunnel mouth pieces to stand strips off
    (scope B), or ``None``.  The set is ``tunnel_ramp`` sloped rects + the
    ``retaining_wall`` U-walls the tunnel portal emits BEFORE the band
    stage; object-bridge PLATES (bridge_trench / bridge_causeway) are
    excluded by construction (they are pavement-equivalent graph members).
    Built from the current ``layout.shapes`` at emit entry, so the apron-edge
    ``retaining_wall`` pieces ``_emit_apron_walls`` appends later are not yet
    present and are naturally excluded.

    LEGACY pieces only: recognized crossings (corridor deck boxes,
    tunnel-portal-pair regions, collar rings, the depressed-road corridor)
    are covered by the published crossing influence zone
    (``_crossing_zone_union``), not reconstructed here — Phase 1 of
    docs/specs/crossing-terrain-ownership.md."""
    polys = [s.polygon for s in layout.shapes
             if s.role in (ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL)
             and s.polygon is not None and not s.polygon.is_empty]
    if not polys:
        return None
    try:
        block = unary_union(polys).buffer(_PAVEMENT_GAP_M)
        return None if block.is_empty else block
    except _GEOM_EXC:
        return None


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


def _build_construct_reach_band(layout):
    """Slice B stage B3 ORDER 3 (coverage-gap closure): the pavement REACH
    BAND, built at PRE-SOLVE construct time.

    The band contract is ``band(x, y) -> (floor, ceiling) | None`` — the
    interval every solved pavement/spine node's elevation is confined to by
    the unified grade graph (``reach_band_unified``, the SAME band the solve
    and the validator use).  It is a pure function of the pavement geometry,
    the taxi centerlines and the CIFP runway anchors — none of which the
    solve has yet moved at construct time (runway profiles carry real values
    from birth; ``build_unified_graph`` derives its runway anchors from the
    shapes, not from a solved ``elev`` vector) — so the band is computable
    BEFORE ``per_surface_solve``.  Building it is cheap (~1 s at CYXY: the
    graph build dominates; the Dijkstra reach fields and the closure are
    sub-0.1 s).  Returns the closure, or ``None`` on any failure (the caller
    then keeps the DEM-seeded march — a loud degrade, never a crash).

    NOTE the double build: the solve builds the same band again inside
    ``reach_band_for``.  The band is a pure geometry closure (index-
    independent), so the two agree; sharing it across the pre-solve/solve
    boundary would couple the construct to the runway-flex re-anchoring the
    solve performs after this point, so the construct deliberately keeps its
    own (pre-flex) band — a valid bound on the solved value regardless (flex
    only tightens the reachable interval).
    """
    try:
        from .elevation_per_surface.solver_primitives import _build_node_list
        from . import grade_graph as _GG
        from .elevation_per_surface.building_feasibility import (
            reach_band_unified)
        _nodes, bucket_to_idx = _build_node_list(layout)
        ctx = _GG.build_context(layout, bucket_to_idx)
        G = _GG.build_unified_graph(layout, bucket_to_idx, ctx=ctx)
        return reach_band_unified(layout, G)
    except Exception as _band_exc:                            # pragma: no cover
        UI.vprint(1, f"  [adjacent-ground] WARN: construct reach-band build "
                     f"FAILED ({_band_exc!r}) — DEM-seeded march kept "
                     f"(coverage degrade).")
        return None


def _worst_case_ring_alts(s, coords, band, sample_dem):
    """Slice B stage B3 ORDER 3 worst-case pavement-edge references for the
    pre-solve construct march.

    Returns ``(ring_alts_cut, ring_alts_fill)`` — two per-vertex edge-altitude
    arrays that bound the SOLVED edge value from the two violating sides, so
    the marched band FOOTPRINT is a SUPERSET of any solved outcome BY
    CONSTRUCTION (no magic margin — the bound IS the reach band):

      * ``ring_alts_cut`` uses the band FLOOR: a CUT fires where terrain rises
        above ``edge + ceiling_offset``; the solved edge can drop no lower
        than the floor, so the floor maximises the cut set — every solved
        cut station is covered (``solved_edge >= floor`` ⇒
        ``{terrain > solved+ceil} ⊆ {terrain > floor+ceil}``).
      * ``ring_alts_fill`` uses the band CEILING: a FILL fires where terrain
        falls below ``edge − floor_depth``; the solved edge can rise no higher
        than the ceiling, so the ceiling maximises the fill set.

    (SCOUT CORRECTION 2026-07-11: the work-order's parenthetical had these
    swapped — "band CEILING for cut … band FLOOR for fill" — which yields the
    SUBSET, not the superset.  Cut needs the LOWEST edge, fill the HIGHEST.
    Derived above and confirmed against the marchers' detection tests
    ``dd > ref + ceil`` / ``dd < ref − floor``.)

    Only entries the shape does NOT yet carry a real value for (the unsolved
    taxi/apron/junction rings, ``node_altitudes[i] is None``) are substituted;
    runway rings keep their CIFP values in both arrays.  Where the band is
    ``None`` (an unreachable vertex) the DEM seed is kept — the pre-order-3
    behaviour, no regression."""
    base = _shape_ring_alts(s, coords)     # no seed → None where unsolved
    ring_alts_cut = list(base)
    ring_alts_fill = list(base)
    for i, (x, y) in enumerate(coords):
        if base[i] is not None:
            continue
        bv = band(x, y) if band is not None else None
        if bv is not None and bv[0] is not None and bv[1] is not None:
            ring_alts_cut[i] = float(bv[0])       # floor  → cut coverage
            ring_alts_fill[i] = float(bv[1])      # ceiling → fill coverage
        else:
            dd = sample_dem(x, y)
            if dd is not None:
                ring_alts_cut[i] = float(dd)
                ring_alts_fill[i] = float(dd)
    return ring_alts_cut, ring_alts_fill


def _coverage_grid_edges(base_edges, cap):
    """Densify a band-edge set with intermediate depths every
    ``ADJACENT_GROUND_COVERAGE_DEPTH_STEP_M`` out to ``cap`` — the depth
    grid the full-extent coverage grid stages so a solved zone row of each
    kind sits within the resampler's ``_ROW_RANGE_M`` of every emit-time
    band vertex.  Off the coverage gate this is never called."""
    edges = set(base_edges)
    from .config import ADJACENT_GROUND_COVERAGE_DEPTH_STEP_M as _dstep
    d = _dstep
    while d < cap:
        edges.add(float(d))
        d += _dstep
    return edges


def _derive_shape_stations_and_bands(coords, ccw, ring_alts, axis, width,
                                     reach, trigger, floor_depth, ceil_off,
                                     step, prep_static, seam_keys,
                                     sample_dem, zone_rows_out=None,
                                     wrap_skirt_prep=None, ring_alts_fill=None,
                                     coverage_grid=False,
                                     crossing_zone_prep=None):
    """Frontage detection + corridor MARCH for one airside shape — the band
    FOOTPRINT geometry (everything that decides WHERE the bands are, given the
    edge-altitude references ``ring_alts``).  Returns
    ``(fill_bands, cut_bands, stations, st_alts, outs)``; the two band lists
    are the raw ``(ring_open, alts_open)`` pairs the emitter later clips and
    values.  Extracted verbatim from the emitter's per-shape setup so the
    pre-solve constructor and the post-solve emitter march identically (the
    B2 shared-helper pattern — parity single-sourced, not duplicated).

    ``zone_rows_out`` (Slice B stage B3 order 2, pre-solve constructor
    only): a list that receives one dict per band ZONE ROW —
    ``{"kind", "d0", "pts", "depths", "hosts"}`` — where a zone row is
    every band row at lateral depth > 0 (the d0 == 0 weld row is the
    pavement chain itself, never a zone row) and ``hosts`` is the
    frozen-nearest pavement RING VERTEX per station (the B2
    frozen-nearest pattern: every ring vertex is a solver variable, so
    the envelope interval edge is mappable to a node index at
    constraint-build time).  ``None`` (every post-solve caller): no
    collection, byte-identical.

    ``wrap_skirt_prep`` (Slice B stage B3 order 3, scope A — taxiway-end
    wrap; passed only for TAXIWAY shapes with O4_ADJACENT_GROUND_END_WRAP
    ON): a prepared runway-END skirt union.  A station whose terrain-facing
    probe lands ONLY on a skirt is then kept (the skirt is the wrap's join
    target, not an obstruction) so the corridor wraps the taxiway end onto
    the skirt.  ``None`` (default; runways, aprons, gate OFF): the probe
    skips every static hit exactly as before — byte-identical.

    ``ring_alts_fill`` (Slice B stage B3 order 3, coverage closure; passed
    only by the pre-solve construct march under band admission): a SECOND
    per-vertex edge-altitude array — the reach-band CEILING — used for FILL
    detection, while the positional ``ring_alts`` carries the reach-band
    FLOOR used for CUT detection (see ``_worst_case_ring_alts``).  ``None``
    (default; every post-solve/emit caller): both directions read the single
    ``ring_alts`` — byte-identical to before order 3.

    ``crossing_zone_prep`` (Phase 1, docs/specs/crossing-terrain-
    ownership.md; supersedes the round-6 road-lane exclusion): the
    PREPARED published crossing influence zone (``_crossing_zone_prep``).
    A station whose seed point OR outward probe falls inside the zone is
    dropped exactly like the end-edge / covered-probe skips, so the
    taxiway-end wrap fan never sweeps into a crossing or its depressed
    road corridor (bands never wrap a ramp/approach end — user ruling
    2026-07-15).  ``None`` (default; nothing published — no crossings, no
    depressed road): no zone test — byte-identical."""
    if ring_alts_fill is None:
        ring_alts_fill = ring_alts
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
        probe = Point(sx + out[0] * _RING_PROBE_M,
                      sy + out[1] * _RING_PROBE_M)
        # CROSSING-ZONE EXCLUSION (Phase 1): a station whose seed or
        # outward probe falls inside the published crossing influence
        # zone is dropped — bands never wrap a ramp/approach end and
        # never march into a crossing.  Inert without a published zone
        # (``crossing_zone_prep is None``).
        if crossing_zone_prep is not None and (
                crossing_zone_prep.contains(Point(sx, sy))
                or crossing_zone_prep.contains(probe)):
            _APPARATUS_HITS["wrap_crossing_zone_excluded_stations"] += 1
            return None
        if prep_static.contains(probe):
            # WRAP (scope A): a taxiway station whose outward probe lands
            # ONLY on a runway-END skirt is the JOIN target, not an
            # obstruction — keep it so the corridor wraps the taxiway end
            # and lands on the skirt chain (the exact clip + snap-to-static
            # weld the wrap ring onto the skirt verbatim).  Any other static
            # hit (pavement, another junction) still skips.
            if not (wrap_skirt_prep is not None
                    and wrap_skirt_prep.contains(probe)):
                return None
        return alt_value

    stations, st_alts, outs = [], [], []
    # FILL-direction reference per station (order 3 worst-case coverage).
    # Identical object as ``st_alts`` when ``ring_alts_fill is ring_alts``
    # (the default) so the fill/cut split is a structural no-op off order 3.
    st_alts_fill: list = st_alts if ring_alts_fill is ring_alts else []
    _split_refs = st_alts_fill is not st_alts
    is_ring_vertex: list[bool] = []
    at_seam: list[bool] = []
    # Frozen-nearest host pavement ring vertex per station (zone-row
    # admission; built unconditionally — cheap — consumed only through
    # ``zone_rows_out``).  A fan station sits AT its corner (a ring
    # vertex); an edge station takes the nearer edge endpoint.
    hosts: list[tuple[float, float]] = []
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
        a0f = ring_alts_fill[i]
        a1f = ring_alts_fill[i + 1]
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
                    if _split_refs:
                        st_alts_fill.append(_station_reference(
                            eax, eay, fan_out, a0f))
                    outs.append(fan_out)
                    is_ring_vertex.append(True)
                    at_seam.append(False)
                    hosts.append((eax, eay))
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
            if _split_refs:
                ref_f = None
                if a0f is not None and a1f is not None:
                    ref_f = _station_reference(
                        sx, sy, out, a0f + t * (a1f - a0f))
                st_alts_fill.append(ref_f)
            outs.append(out)
            is_ring_vertex.append(k == 0)
            at_seam.append((k == 0 and edge_a_seam)
                           or (k == nseg - 1 and edge_b_seam))
            hosts.append((eax, eay) if t < 0.5 else (ebx, eby))
    if len(stations) < 2:
        return [], [], stations, st_alts, outs
    m = len(stations)
    # Seam-taper pin instrumentation (order-2 confirmation row): the
    # count of stations flagged AT a pavement-partition seam — the pin's
    # input firing set (the pin itself acts on footprint depths inside
    # ``grade_law.adjacent_ground_supported_depths``).  Expected
    # UNCHANGED between admission OFF and ON.
    _APPARATUS_HITS["seam_taper_flagged_stations"] += sum(
        1 for _f in at_seam if _f)

    # Zone-row collectors (order 2): translate the builders' per-run
    # provenance into row dicts with frozen-nearest hosts.  The d0 == 0
    # INNER row is the pavement weld chain — dropped here (pavement
    # variables already exist; a band never mints one).
    def _make_zone_collector(kind):
        def _collect(d0, inner_entries, outer_entries):
            if d0 > 0.0 and len(inner_entries) >= 1:
                zone_rows_out.append({
                    "kind": kind, "d0": float(d0),
                    "pts": [(float(x), float(y))
                            for _i, x, y in inner_entries],
                    "depths": [float(d0)] * len(inner_entries),
                    "hosts": [hosts[i] for i, _x, _y in inner_entries]})
            if outer_entries:
                zone_rows_out.append({
                    "kind": kind, "d0": float(d0),
                    "pts": [(float(x), float(y))
                            for _i, x, y, _d in outer_entries],
                    "depths": [float(dd)
                               for _i, _x, _y, dd in outer_entries],
                    "hosts": [hosts[i]
                              for i, _x, _y, _d in outer_entries]})
        return _collect

    _collect_fill = (_make_zone_collector("fill")
                     if zone_rows_out is not None else None)
    _collect_cut = (_make_zone_collector("cut")
                    if zone_rows_out is not None else None)

    # FILL (DEM below floor, zones 1-2) then CUT (DEM above ceiling): the
    # runway-end skirt fill/cut builders' lateral twins.  ORDER 3 worst-case
    # coverage: FILL detects against the reach-band CEILING (``st_alts_fill``),
    # CUT against the reach-band FLOOR (``st_alts``); off order 3 the two
    # arrays are the same object, so this is byte-identical.
    fill_edges = {ADJACENT_GROUND_LIP_WIDTH_M}
    cut_edges = {ADJACENT_GROUND_LIP_WIDTH_M, width}
    if coverage_grid:
        fill_edges = _coverage_grid_edges(fill_edges, width)
        cut_edges = _coverage_grid_edges(cut_edges, reach)
    fill_bands = _build_fill_bands(
        stations, st_alts_fill, outs, [width] * m, floor_depth,
        fill_edges, trigger, step, sample_dem,
        is_ring_vertex, at_seam, zone_collect=_collect_fill,
        force_full_reach=coverage_grid)
    cut_bands = _build_cut_bands(
        stations, st_alts, outs, [reach] * m, ceil_off,
        cut_edges, trigger, step, sample_dem,
        is_ring_vertex, at_seam, zone_collect=_collect_cut,
        force_full_reach=coverage_grid)
    return fill_bands, cut_bands, stations, st_alts, outs


def _split_zone_rows_off_static(zone_rows, prep_static, static_boundary):
    """Drop every zone-row point that lies INSIDE static pavement or within
    ``ADJACENT_GROUND_ZONE_STATIC_KEEPOUT_M`` of any static-shape boundary
    (B4 flip defect 1 — see the constant's rationale in ``config.py``: such
    a point can intern onto a pavement ring vertex's canonical bucket and
    stamp its DEM-clamped value onto the pavement profile).  A row whose
    interior points are dropped is SPLIT into its surviving contiguous runs
    (one row dict per run) so the emit-time resampler never interpolates a
    row polyline straight across the pavement it used to cross."""
    from .config import ADJACENT_GROUND_ZONE_STATIC_KEEPOUT_M as _margin
    import numpy as _np
    from shapely import points as _sh_points, dwithin as _sh_dwithin
    out: list[dict] = []
    n_dropped = 0
    for row in zone_rows:
        pts = row.get("pts") or []
        if not pts:
            continue
        arr = _sh_points(_np.asarray(pts, dtype=float))
        try:
            near = _sh_dwithin(static_boundary, arr, _margin)
        except _GEOM_EXC:
            near = _np.zeros(len(pts), dtype=bool)
        runs: list[list[int]] = []
        current: list[int] = []
        for j in range(len(pts)):
            bad = bool(near[j])
            if not bad:
                try:
                    bad = prep_static.contains(arr[j])
                except _GEOM_EXC:
                    bad = False
            if bad:
                n_dropped += 1
                if current:
                    runs.append(current)
                    current = []
            else:
                current.append(j)
        if current:
            runs.append(current)
        if len(runs) == 1 and len(runs[0]) == len(pts):
            out.append(row)
            continue
        depths = row.get("depths") or []
        hosts = row.get("hosts") or []
        for run in runs:
            out.append({
                "kind": row["kind"], "d0": row["d0"],
                "pts": [pts[j] for j in run],
                "depths": [depths[j] for j in run],
                "hosts": [hosts[j] for j in run]})
    if n_dropped:
        _APPARATUS_HITS["zone_static_keepout_dropped"] += n_dropped
    return out


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
    "cut": [...], "zone_rows": [...], "zone_nodes": [...],
    "zone_values": None}, ...]`` (the ``shape`` reference is preserved across
    the layout pickle — same object as the ``layout.shapes`` element — so the
    emitter rebuilds the resampler from the by-then-solved shape).  The
    ``zone_rows`` / ``zone_nodes`` fields are the ORDER-2 schema split: the
    free-variable zone-row grid (row polylines with per-vertex depth +
    frozen-nearest host), admitted to the solver under the ADMISSION sub-gate
    ``ONE_SOLVE_TERRAIN_GRADED_STRIP``; ``zone_values`` is filled by the
    solve's writeback ({millimetre key: solved value}).  Returns the
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
    # Zone-node static keep-out (B4 flip defect 1): the boundary of the
    # SAME static block the march probes, prepared once for the vectorized
    # dwithin test in ``_split_zone_rows_off_static``.
    try:
        from shapely import prepare as _sh_prepare
        _zone_static_boundary = static_union.boundary
        _sh_prepare(_zone_static_boundary)
    except _GEOM_EXC:
        _zone_static_boundary = None

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
    # Taxiway-end WRAP join target (scope A): built once, passed only for
    # taxiway shapes with the gate ON (byte-inert otherwise).
    wrap_skirt_prep = (_runway_end_skirt_prep(layout)
                       if _END_WRAP else None)
    # Crossing-zone exclusion (Phase 1): the published zone, prepared
    # once, passed for taxiway shapes (byte-inert when nothing published).
    crossing_zone_prep = _crossing_zone_prep(layout)

    # ── ORDER 3 WORST-CASE COVERAGE (coverage-gap closure) ──────────────
    # The pre-solve march seeds unsolved pavement edges from the DEM, which
    # tracks the in-corridor terrain and so emits NO band exactly where the
    # SOLVED edge departs from the DEM (flattened taxiways, raised aprons,
    # the taxiway end onto a skirt) — the 1,285 analytic-fallback vertices
    # and 24 store-missing shapes.  Under band ADMISSION (the only path that
    # solves the zone nodes and reads them back), march instead against the
    # reach-band WORST CASE per edge (``_worst_case_ring_alts``): floor for
    # cut, ceiling for fill.  The band bounds every solved edge, so the
    # footprint is a SUPERSET of any solved outcome by construction and the
    # emit-time resampler always finds a covering row.  Gate OFF (order-1
    # construct-only, no admission): DEM seed kept, byte-identical.
    from .config import (ONE_SOLVE_TERRAIN_GRADED_STRIP as _ADMIT_COVERAGE,
                         ADJACENT_GROUND_FULL_EXTENT_COVERAGE
                         as _FULL_EXTENT_COVERAGE)
    _reach_band = (_build_construct_reach_band(layout)
                   if _ADMIT_COVERAGE else None)
    # Full-extent coverage grid (B4 prerequisite): only meaningful under
    # band admission (the path that solves + reads back the zone nodes);
    # a no-op otherwise, so the flag stays False without admission.
    _coverage_grid = bool(_FULL_EXTENT_COVERAGE and _ADMIT_COVERAGE)

    entries: list[dict] = []
    for s in scoped:
        params = _family_params(layout, s, rw_axes)
        if params is None:
            continue
        family, code_number, code_letter, reach, width, axis = params
        trigger = trigger_by_family[family]
        ceil_off, envelope_at, floor_depth = _band_family_closures(
            family, code_number, code_letter, width)
        try:
            coords = list(s.polygon.exterior.coords)
            ccw = bool(s.polygon.exterior.is_ccw)
        except _GEOM_EXC:
            continue
        if len(coords) < 4:
            continue
        if _reach_band is not None:
            # Worst-case coverage: floor-referenced CUT ring + ceiling-
            # referenced FILL ring (runway edges keep CIFP values in both).
            ring_alts, ring_alts_fill = _worst_case_ring_alts(
                s, coords, _reach_band, sample_dem)
        else:
            ring_alts = _shape_ring_alts(s, coords, sample_dem, seed=True)
            ring_alts_fill = None
        zone_rows: list[dict] = []
        fill_bands, cut_bands, _st, _sa, _ou = \
            _derive_shape_stations_and_bands(
                coords, ccw, ring_alts, axis, width, reach, trigger,
                floor_depth, ceil_off, step, prep_static, seam_keys,
                sample_dem, zone_rows_out=zone_rows,
                wrap_skirt_prep=(wrap_skirt_prep
                                 if family == "taxiway" else None),
                ring_alts_fill=ring_alts_fill,
                coverage_grid=_coverage_grid,
                crossing_zone_prep=(crossing_zone_prep
                                    if family == "taxiway" else None))
        if not fill_bands and not cut_bands:
            continue
        # Zone-node static keep-out (B4 flip defect 1): no zone point on,
        # inside, or hugging static pavement ever becomes a solver variable.
        if zone_rows and _zone_static_boundary is not None:
            zone_rows = _split_zone_rows_off_static(
                zone_rows, prep_static, _zone_static_boundary)
        # ZONE-NODE GRID (order 2, schema split): the free-variable
        # admission list — one record per unique zone-row vertex
        # (millimetre dedup; a row vertex shared between two abutting
        # band slabs is ONE variable), each with its frozen-nearest
        # host pavement ring vertex and the law's envelope offsets at
        # the vertex's construction-time lateral depth:
        #   * "cut" rows: ceiling only (fill bands own below-floor
        #     terrain — the analytic resampler's own kind rule);
        #   * "fill" rows: both bounds at depth clamped to the graded
        #     width (zones 1-2 by construction — same rule).
        zone_nodes: list[dict] = []
        seen_zone_keys: set[tuple[int, int]] = set()
        for row in zone_rows:
            for (zx, zy), zd, zhost in zip(row["pts"], row["depths"],
                                           row["hosts"]):
                zkey = _vertex_key(zx, zy)
                if zkey in seen_zone_keys:
                    continue
                seen_zone_keys.add(zkey)
                if row["kind"] == "cut":
                    zone_floor = None
                    zone_ceil = envelope_at(zd)[1]
                else:
                    zone_floor, zone_ceil = envelope_at(min(zd, width))
                zone_nodes.append({
                    "xy": (float(zx), float(zy)),
                    "host": (float(zhost[0]), float(zhost[1])),
                    "floor_off": (None if zone_floor is None
                                  else float(zone_floor)),
                    "ceil_off": (None if zone_ceil is None
                                 else float(zone_ceil))})
        entries.append({"shape": s, "fill": fill_bands, "cut": cut_bands,
                        "zone_rows": zone_rows,
                        "zone_nodes": zone_nodes,
                        "zone_values": None})
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
    # Order-2 apparatus instrumentation: emit-phase counters only (the
    # pre-solve construct march also increments the seam counter — the
    # reset here scopes the reported numbers to THIS emission).
    _reset_apparatus_hits()
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
    # BUILDING STANDOFF block (2026-07-17): a building footprint sits at
    # its PAD altitude — often metres below the terrain the band rides
    # (a building in a graded pit).  The exact ``static_union`` clip makes
    # the band abut the building edge and share its corners, so the
    # ``to_osm`` nid-weld + authority-consensus stamps the pad value onto
    # a single band corner while its terrain neighbours stay high — a
    # sub-metre near-vertical TEAR (CYXY building1: 705.5 pad welded into
    # a 714.6 band edge, 9 m over 0.9 m).  A building is a DESIGNED SPLIT,
    # not a weld partner (the same ruling ``_WELD_DONOR_ROLES`` encodes:
    # bands never adopt a building/pad/terminal value), so the band stands
    # 1 m off the footprint — the tunnel-ramp / groundside standoff pattern
    # — and the pad corner never lands in a band vertex's canonical bucket.
    # A 1 m raw-DEM groove at the building base renders harmlessly (the
    # building occupies it).
    _bld_polys = [s.polygon for s in layout.shapes
                  if s.role == ROLE_BUILDING
                  and s.polygon is not None and not s.polygon.is_empty]
    building_block = None
    if _bld_polys:
        try:
            building_block = unary_union(_bld_polys).buffer(_PAVEMENT_GAP_M)
        except _GEOM_EXC:
            building_block = None
    # Tunnel-ramp STANDOFF block (scope B): 1 m around the tunnel mouth
    # pieces, so a strip stands off the steep mouth-ramp floor exactly like
    # a building.  Built here (before ``_emit_apron_walls`` adds its own
    # retaining_wall pieces) and only with the gate ON.  Empty (None) at
    # airports without a mapped tunnel — CYXY carries no tunnel_ramp or
    # pre-band retaining_wall, so gate-ON is byte-identical there.
    tunnel_ramp_block = (_tunnel_ramp_standoff_block(layout)
                         if _TUNNEL_STANDOFF else None)
    static_union = None
    _static_polys = [s.polygon for s in layout.shapes
                     if s.polygon is not None and not s.polygon.is_empty
                     and s.role != "groundside_pavement"]
    try:
        static_union = unary_union(_static_polys)
    except _GEOM_EXC:
        static_union = None
    if static_union is None or static_union.is_empty:
        return 0
    try:
        prep_static = prep(static_union)
    except _GEOM_EXC:
        return 0
    boundary = layout.airport_boundary
    # Taxiway-end WRAP join target (scope A): built once, passed to the
    # inline (legacy-path) march for taxiway shapes with the gate ON.  The
    # gate-ON solver path consumes pre-built footprints
    # (``construct_adjacent_ground_presolve`` already wrapped there), so this
    # only feeds the inline re-march; byte-inert with the gate OFF.
    wrap_skirt_prep = (_runway_end_skirt_prep(layout)
                       if _END_WRAP else None)
    # CROSSING INFLUENCE ZONE (Phase 1, docs/specs/crossing-terrain-
    # ownership.md): the ONE published zone replaces the crossing-union
    # branch of the standoff block AND the road-lane exclusion — the band
    # pieces are DIFFERENCED against it below and the station-level drop
    # feeds the inline re-march path.  Published pre-solve, so construct
    # and emit consult the identical geometry.  Byte-inert when nothing
    # is published (both are None).
    crossing_zone_union = _crossing_zone_union(layout)
    crossing_zone_prep = (_crossing_zone_prep(layout)
                          if crossing_zone_union is not None else None)
    if (os.environ.get("O4_CROSSING_ZONE_DEBUG") == "1"
            and crossing_zone_union is not None):
        # (The zone WKT dump lives at PUBLICATION — O4_CROSSING_ZONE_DUMP
        # in ``crossing_terrain`` — never here: two writers to one path
        # clobbered the publication dump.)
        _area = (crossing_zone_union.area
                 if not crossing_zone_union.is_empty else 0.0)
        print(f"  [crossing-zone-debug] band-emit crossing zone union "
              f"area = {_area:.1f} m2")
    # The clip block carries a margin over the zone itself (the buffered-
    # standoff pattern, ``_PAVEMENT_GAP_M``): a band stands off the zone
    # boundary rather than welding onto it, and the post-clip ring
    # decimation/simplification cannot bow a long clipped edge back INTO
    # the zone (measured KBNA: a 7 m2 sliver on a 36,000 m2 merged strip
    # from a 0-margin lane clip).
    crossing_zone_clip_block = None
    if crossing_zone_union is not None:
        try:
            crossing_zone_clip_block = crossing_zone_union.buffer(
                _PAVEMENT_GAP_M)
        except _GEOM_EXC:
            crossing_zone_clip_block = crossing_zone_union

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
    _static_ext_shape = []   # owner shape per exterior (edge-weld values)
    for _s in layout.shapes:
        if _s.role == "groundside_pavement":
            continue        # never adopt groundside chains (ruling)
        if _s.polygon is not None and not _s.polygon.is_empty:
            try:
                _static_ext.append(_s.polygon.exterior)
            except _GEOM_EXC:
                continue
            _static_ext_shape.append(_s)
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

    # STATIC-EDGE VALUE WELD (B4 flip defect 2, 2026-07-15).  A band clip
    # vertex lying ON a FOREIGN static shape's edge (the exact
    # ``difference`` clip and ``_snap_ring_to_static`` both mint them) is
    # part of THAT surface's chain — its value is that surface's solved
    # edge altitude, never the band's own corridor/zone reading.  The
    # solved-band resampler's weld test knows only the band's HOST ring,
    # so before this weld a junction-edge clip vertex took a zone-row
    # value up to the corridor envelope away from the junction's solved
    # surface (CYXY: 709.8 vs 709.94), and the final epsilon-wedge weld
    # then stamped that value into the junction ring — the B4 junction
    # spine-grade violations the legacy clearance strips used to mask by
    # occupying the ground.  Lazy per-exterior edge references (the same
    # ``_ring_edge_reference`` read every resampler uses).
    _STATIC_WELD_TOL_M = 0.02
    _static_edge_ref_cache: dict = {}
    # Value-donor scope: the surfaces the band law grades TO — airside
    # pavement plus the pavement-pinned clearance family (runway-end
    # skirts, legacy clearance strips).  Bridge plates, tunnel walls/
    # ramps, pads, terminals never donate: a band meeting one of those is
    # a DESIGNED split (deck cliff / wall / standoff), not a weld.
    from .layout import (ROLE_RUNWAY_CLEARANCE as _R_RWCL,
                         ROLE_TAXIWAY_CLEARANCE as _R_TWCL)
    # THE single donor-role source lives in layout.py (WELD_DONOR_ROLES,
    # 2026-07-17) — shared with to_osm's strip-adoption consensus so the
    # static-edge weld and the emit consensus can never disagree on who
    # may donate a value to a soft strip.
    from .layout import WELD_DONOR_ROLES as _WELD_DONOR_ROLES

    def _static_edge_weld_alt(x, y):
        if _static_ext_tree is None:
            return None
        pt = Point(x, y)
        try:
            cand = _static_ext_tree.query_nearest(
                pt, max_distance=_STATIC_WELD_TOL_M)
        except _GEOM_EXC:
            return None
        cand = [gi for gi in cand
                if (_static_ext_shape[gi].role or "") in _WELD_DONOR_ROLES]
        if not cand:
            return None
        best_gi = int(min(cand))    # deterministic among exact ties
        ref = _static_edge_ref_cache.get(best_gi)
        if ref is None:
            ext_coords = list(_static_ext[best_gi].coords)
            ref = _ring_edge_reference(
                ext_coords,
                _shape_ring_alts(_static_ext_shape[best_gi], ext_coords))
            _static_edge_ref_cache[best_gi] = ref
        ref_line, ref_alt_at = ref
        alt = ref_alt_at(ref_line.project(pt))
        return None if alt is None else float(alt)

    # Row-100 runway axes (authoritative length + direction) for runway
    # code-number keying and END-edge skipping — as the ring-edge sweep.
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
    # DONOR-GATED (2026-07-17, the WELD_DONOR_ROLES ruling applied to
    # this registry — the third foreign-value writer alongside the
    # static-edge weld and to_osm's strip adoption): unconditional
    # adoption is reserved for keys registered by a WELD_DONOR_ROLES
    # shape (runway/taxi/apron/clearance families).  A designed split
    # (service road, building pad, terminal, groundside, bridge plate)
    # is an authority for its OWN nodes but never donates: a band clip
    # vertex landing on its ring corner keeps the band's lawful value —
    # the value step renders as the designed wall (measured CYXY strip
    # #518: service-road 709.5 spliced into a 705.7 band edge = 3.9 m
    # over 1.3 m, collapsing to a 0-length in-ring tear at emit).
    donor_value_keys: set[tuple[int, int]] = set()
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
                _APPARATUS_HITS["band_corner_weld_snaps"] += 1
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

    # PRIOR-BAND FOOTPRINT INDEX (robust deconflict, 2026-07-17): a coarse
    # bbox-cell bucket of every band polygon emitted so far, so each new
    # piece can subtract the FEW earlier bands it actually meets one at a
    # time (robust) instead of trusting the giant accumulated union's
    # ``difference`` (which GEOS silently no-ops once the union is large —
    # the band∩band overlap source).  Keyed by 32 m cells over each band's
    # bounding box.
    _PRIOR_BAND_CELL_M = 32.0
    _prior_band_index: dict[tuple[int, int], list] = {}

    def _band_cells(bounds):
        x0, y0, x1, y1 = bounds
        cx0 = int(math.floor(x0 / _PRIOR_BAND_CELL_M))
        cy0 = int(math.floor(y0 / _PRIOR_BAND_CELL_M))
        cx1 = int(math.floor(x1 / _PRIOR_BAND_CELL_M))
        cy1 = int(math.floor(y1 / _PRIOR_BAND_CELL_M))
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                yield (cx, cy)

    def _register_prior_band(band_poly):
        try:
            b = band_poly.bounds
        except _GEOM_EXC:
            return
        for cell in _band_cells(b):
            _prior_band_index.setdefault(cell, []).append(band_poly)

    def _nearby_prior_bands(poly):
        try:
            cells = set(_band_cells(poly.bounds))
        except _GEOM_EXC:
            return []
        seen: set[int] = set()
        out = []
        for cell in cells:
            for bp in _prior_band_index.get(cell, ()):
                if id(bp) not in seen:
                    seen.add(id(bp))
                    out.append(bp)
        return out

    # STATIC-FOOTPRINT INDEX (same robustness class as the band index):
    # ``poly.difference(static_union)`` above shares the GEOS overlay
    # no-op failure once ``static_union`` is large, so a band can survive
    # lapping a foreign pavement / junction by a sliver (CYXY: 0.14 m² over
    # a service_junction).  Index the individual static footprints so the
    # deconflict can re-subtract the few the piece actually meets (robust).
    _static_poly_index: dict[tuple[int, int], list] = {}
    for _sp in _static_polys:
        if _sp is None or _sp.is_empty or _sp.geom_type != "Polygon":
            continue
        try:
            for _cell in _band_cells(_sp.bounds):
                _static_poly_index.setdefault(_cell, []).append(_sp)
        except _GEOM_EXC:
            continue

    def _nearby_static_polys(poly):
        try:
            cells = set(_band_cells(poly.bounds))
        except _GEOM_EXC:
            return []
        seen: set[int] = set()
        out = []
        for cell in cells:
            for sp in _static_poly_index.get(cell, ()):
                if id(sp) not in seen:
                    seen.add(id(sp))
                    out.append(sp)
        return out

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
        s_is_donor = (s.role or "") in _WELD_DONOR_ROLES
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
                if s_is_donor:
                    donor_value_keys.add(k)
            elif (s_is_authority and s_is_donor
                    and k not in donor_value_keys):
                # A DONOR authority (pavement the band welds to)
                # outranks an earlier non-donor authority at a shared
                # seam corner — the band must adopt the pavement side
                # of the seam, not the designed split's.
                vertex_value_registry[k] = value
                donor_value_keys.add(k)
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
    #
    # ORDER 2 (variable ADMISSION, gate ``ONE_SOLVE_TERRAIN_GRADED_STRIP``
    # — hard-error-chained onto construct + B1 + B2 in
    # ``solver_primitives.admitted_terrain_refs``): the pre-built
    # footprints are NOT consumed.  The loop marches inline over the
    # FINAL solved, decimated, densified pavement ring — so the emitted
    # band inner (weld) chain is BY CONSTRUCTION the final pavement
    # chain (the order-1 82-band census inflation collapses back to the
    # gate-OFF footprint), and the construct store's role narrows to the
    # ZONE-NODE VARIABLE GRID plus its solved values.  Re-deriving the
    # run/band segmentation over the final chain and re-mapping the
    # solved values onto the re-derived rows is SOUND because the band
    # law has no neighbour coupling — every zone value is independent
    # (clamp of the DEM into its own host-referenced corridor), so
    # regrouping independently-valued vertices cannot violate anything
    # (order-2 scout refutation 1, ratified 2026-07-11).
    from .config import (ONE_SOLVE_TERRAIN as _OST,
                         ONE_SOLVE_TERRAIN_GRADED_STRIP as _OST_ADMIT,
                         ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT as _OST_C)
    _presolve_bands = None
    _solved_store = None
    if _OST and _OST_ADMIT:
        # Reuses the hard dependency chain: reading the admitted set
        # raises loudly on a partial gate configuration.
        from .elevation_per_surface.solver_primitives import (
            admitted_terrain_refs as _admitted_refs_fn)
        if (ROLE_GRADED_STRIP, _ADJACENT_REF) in _admitted_refs_fn():
            _store = getattr(layout, "adjacent_ground_presolve", None)
            if _store:
                _solved_store = {id(e["shape"]): e for e in _store}
    elif _OST and _OST_C:
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
                sample_dem,
                wrap_skirt_prep=(wrap_skirt_prep
                                 if family == "taxiway" else None),
                crossing_zone_prep=(crossing_zone_prep
                                    if family == "taxiway" else None))
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
        # ORDER-2 gate-ON valuation swap: the analytic corridor clamp
        # above becomes the per-vertex FALLBACK only (counted); every
        # reachable vertex reads the SOLVED band surface instead.
        if _solved_store is not None:
            _solved_entry = _solved_store.get(id(s))
            if (_solved_entry is not None
                    and _solved_entry.get("zone_values")):
                resample_alt = _make_solved_band_resampler(
                    _solved_entry, coords, ring_alts, resample_alt)
            else:
                _APPARATUS_HITS["solved_store_missing_shape"] += 1
                UI.vprint(1, f"  [adjacent-ground] WARN: no solved "
                             f"zone values for shape role={s.role} "
                             f"ref={s.ref} — analytic valuation kept "
                             f"for this shape (degrade, counted).")
        # STATIC-EDGE VALUE WELD (B4 flip defect 2): a band vertex ON any
        # static shape's edge takes THAT surface's solved edge value
        # (weld precedence), with the host-corridor/zone valuation above
        # as the fallback for every off-edge vertex.
        _corridor_resample = resample_alt

        def resample_alt(vx, vy, kind, _base=_corridor_resample):
            weld_alt = _static_edge_weld_alt(vx, vy)
            if weld_alt is not None:
                _APPARATUS_HITS["static_edge_weld_vertices"] += 1
                return (weld_alt, True)
            return _base(vx, vy, kind)

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
                    if (building_block is not None
                            and not building_block.is_empty):
                        # Buffered standoff: a strip never welds onto a
                        # building's pad value (designed split, tear source
                        # — see building_block).
                        poly = poly.difference(building_block)
                    if (tunnel_ramp_block is not None
                            and not tunnel_ramp_block.is_empty):
                        # Buffered standoff (scope B): a strip stands 1 m
                        # off the tunnel mouth ramp/wall rather than welding
                        # onto its steep floor — the building standoff
                        # pattern.  A groove of raw DEM renders harmlessly.
                        poly = poly.difference(tunnel_ramp_block)
                    if (crossing_zone_clip_block is not None
                            and not crossing_zone_clip_block.is_empty):
                        # CROSSING INFLUENCE ZONE (Phase 1): no band piece
                        # may enter a published crossing zone — crossings,
                        # collar rings, and the depressed-road corridor —
                        # bands stay along the taxiway edge and never wrap
                        # a ramp/approach end (user ruling 2026-07-15).
                        # Same buffered-standoff render argument as above.
                        poly = poly.difference(crossing_zone_clip_block)
                    if (previous_shapes_union is not None
                            and not previous_shapes_union.is_empty):
                        poly = poly.difference(previous_shapes_union)
                    if (current_shape_union is not None
                            and not current_shape_union.is_empty):
                        # Same-shape exact clip (welded seam).
                        poly = poly.difference(current_shape_union)
                    if boundary is not None and not boundary.is_empty:
                        poly = poly.intersection(boundary)
                    # ROBUST DECONFLICT (2026-07-17): the accumulated
                    # ``previous_shapes_union`` grows into a large multipart
                    # whose ``difference`` intermittently returns the input
                    # UNCHANGED under GEOS overlay robustness failure — the
                    # clip above silently no-ops and the band laps a sibling
                    # (CYXY: 139 m² of #567 over #367, both valid, yet
                    # ``poly.difference(prev_union)`` removed nothing while
                    # differencing the single sibling removed it cleanly).
                    # Re-subtract the individual EARLIER band footprints poly
                    # actually meets (small, robust operands) so no band∩band
                    # overlap survives regardless of the union's robustness.
                    if not poly.is_empty and _prior_band_index:
                        for _pb in _nearby_prior_bands(poly):
                            try:
                                if poly.intersects(_pb):
                                    poly = poly.difference(_pb)
                            except _GEOM_EXC:
                                continue
                            if poly.is_empty:
                                break
                    if not poly.is_empty and _static_poly_index:
                        for _sp in _nearby_static_polys(poly):
                            try:
                                if poly.intersects(_sp):
                                    poly = poly.difference(_sp)
                            except _GEOM_EXC:
                                continue
                            if poly.is_empty:
                                break
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
                        # Unconditional adoption is DONOR-gated (see
                        # ``donor_value_keys``): a non-donor authority
                        # key (service road / building / terminal /
                        # groundside corner) only welds when the values
                        # already agree — otherwise the band keeps its
                        # own lawful value and the step renders as the
                        # designed wall.
                        adopted = [
                            (k in vertex_value_registry
                             and (k in donor_value_keys
                                  or abs(vertex_value_registry[k] - o)
                                  <= VERTEX_ALT_MERGE_TOL_M))
                            for k, o in zip(keys, own)]
                        # Order-2 retirement row: an adoption only
                        # MATTERS when it changes the value (identity
                        # makes own == registry, so gate-ON this count
                        # is expected to fall to zero).
                        _APPARATUS_HITS["value_changing_adoptions"] += \
                            sum(1 for k, a, o in zip(keys, adopted, own)
                                if a and abs(vertex_value_registry[k]
                                             - o) > 1e-6)
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
                        # TEAR HEAL (2026-07-17): collapse any sub-metre
                        # near-vertical ring edge the clips / band cap left
                        # — a pinch where the inner (pavement weld) row and
                        # the outer (terrain) row come within a metre while
                        # carrying their lawful >1 m step (the DEM-free TEAR
                        # sentinel class; no lateral corridor slope makes
                        # it).  Keep weld vertices (pavement seam), drop the
                        # pinched outer / spike vertex, then rebuild simple.
                        healed_ring, healed_alts = _heal_band_tears(
                            piece_ring, alts, weld,
                            0.2 * CLEARANCE_STATION_STEP_M, 1.0,
                            wall_max=CLEARANCE_STATION_STEP_M)
                        if healed_ring is not piece_ring:
                            if len(healed_ring) < 3:
                                continue
                            try:
                                healed_poly = Polygon(
                                    healed_ring + [healed_ring[0]])
                                if not healed_poly.is_valid:
                                    healed_poly = healed_poly.buffer(0)
                                if (healed_poly.is_empty
                                        or healed_poly.geom_type
                                        != "Polygon"):
                                    continue
                                # RE-DECONFLICT (2026-07-17): dropping a
                                # concave pinch vertex reconnects the
                                # ring across it, and the new closing
                                # edge can swing INTO an earlier band /
                                # static footprint the pre-heal clip had
                                # already deconflicted (measured CYXY:
                                # 0.22 m² band∩band re-minted by the
                                # heal).  Re-subtract the few nearby
                                # operands (robust, per-piece).
                                _redeconflicted = False
                                for _hp in (_nearby_prior_bands(
                                                healed_poly)
                                            + _nearby_static_polys(
                                                healed_poly)):
                                    try:
                                        if healed_poly.intersects(_hp):
                                            healed_poly = \
                                                healed_poly.difference(_hp)
                                            _redeconflicted = True
                                    except _GEOM_EXC:
                                        continue
                                    if healed_poly.is_empty:
                                        break
                                if (healed_poly.is_empty
                                        or healed_poly.area < 1e-6):
                                    continue
                                if healed_poly.geom_type != "Polygon":
                                    _hparts = [g for g in getattr(
                                        healed_poly, "geoms", [])
                                        if g.geom_type == "Polygon"]
                                    if not _hparts:
                                        continue
                                    healed_poly = max(
                                        _hparts, key=lambda g: g.area)
                            except _GEOM_EXC:
                                continue
                            rebuilt = _open_coords(healed_poly)
                            if len(rebuilt) < 3:
                                continue
                            if _redeconflicted:
                                # A difference() can keep the vertex
                                # COUNT while shifting vertices (a
                                # positional ``alts = healed_alts``
                                # copy then misaligns — measured SPJC:
                                # a 34.0 spike onto a 32.9 row = a
                                # fresh 1.1 m in-band tear), and its
                                # minted intersection vertices have no
                                # healed partner at all.  Identity-
                                # gated remap: an unchanged vertex
                                # (within 1 cm) keeps its own healed
                                # value; a minted vertex takes the
                                # band's OWN lawful valuation.
                                _remapped = []
                                for _vx, _vy in rebuilt:
                                    _bestv = None
                                    _bestd = 1e9
                                    for _hi, (_hx, _hy) in enumerate(
                                            healed_ring):
                                        _hd = math.hypot(_hx - _vx,
                                                         _hy - _vy)
                                        if _hd < _bestd:
                                            _bestd = _hd
                                            _bestv = healed_alts[_hi]
                                    if _bestd > 0.01 or _bestv is None:
                                        _bestv = resample_alt(
                                            _vx, _vy, kind)[0]
                                    _remapped.append(
                                        round(float(_bestv), 1))
                                alts = _remapped
                            elif len(rebuilt) == len(healed_ring):
                                alts = healed_alts
                            else:
                                # buffer(0) reshaped the ring — remap by
                                # nearest so alts stay aligned.
                                alts = [round(float(_nearest_alt(
                                    healed_ring, healed_alts, vx, vy)), 1)
                                    for vx, vy in rebuilt]
                            piece_ring = rebuilt
                            simple = healed_poly
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
                        # Register the footprint for the robust per-piece
                        # deconflict of later bands (see _prior_band_index).
                        _register_prior_band(simple)
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

        # FINAL TEAR HEAL (raster-reach-band reconciliation, 2026-07-18):
        # collapse any sub-metre near-vertical pinch the per-piece heal could
        # not see because a later geometry op (this piece's re-deconflict, a
        # neighbour band's clip) minted it — the class a tighter, correct
        # reach ceiling exposes at an apron/junction it clamps down.  Runs
        # before decimation so the healed rings decimate cleanly.  Scoped to
        # the raster reach band (the path this reconciles): gate-OFF keeps its
        # established byte-identical baseline — the legacy band does not drop
        # aprons, so the pinch class does not arise there.
        # (The cross-strip SEAM-STEP blend runs at PIPELINE level, after
        # every strip emitter — this group is only one strip family, and
        # the tearing seams are precisely the cross-family ones.)
        _n_final_heal = (_heal_emitted_band_tears(emitted_shapes, layout)
                         if _raster_reach_band_active() else 0)
        if _n_final_heal:
            UI.vprint(1, f"  [adjacent-ground] final tear heal: collapsed "
                         f"pinch edge(s) in {_n_final_heal} strip(s).")
        removed = decimate_shape_group(
            emitted_shapes, Z_TOL_BOUNDARY_M,
            protect_predicate=_on_foreign_boundary)
        if removed:
            UI.vprint(1, f"  [pav-builder] adjacent-ground: decimated "
                         f"{removed} 3D-collinear band vertex(es) "
                         f"(±{Z_TOL_BOUNDARY_M} m).")

    # Order-2 apparatus hit report (always printed — the OFF-vs-ON
    # retirement table reads these from both configurations).
    UI.vprint(1, "  [adjacent-ground] apparatus hits: "
                 + " ".join(f"{k}={_APPARATUS_HITS[k]}"
                            for k in _APPARATUS_KEYS))
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
