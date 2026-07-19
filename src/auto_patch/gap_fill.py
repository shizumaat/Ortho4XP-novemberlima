"""Gap-fill + drainage SPINE emitter (user design ruling 2026-07-09,
docs/chain_identity_one_solve_plan.md "GAP-FILL + DRAINAGE SPINE").

Ground ENCLOSED between pavements — an interior ring of the airside
pavement union, e.g. the hole bounded by a runway, a parallel taxiway and
two connector stubs — is graded as ONE unit:

  * the BOUNDARY is the pavement chains VERBATIM (zero new boundary
    geometry: no buffer / simplify / snap / clean touches any boundary
    coordinate — this codebase Ruppert-explodes on near-parallel
    constrained pairs, one sub-µm pair minting 10^5-10^6 tile triangles,
    so a shared coordinate must stay bit-identical), and
  * the INTERIOR is a single drainage SPINE polyline that splits the gap
    into two half-gap faces sharing the spine chain.  ALL new nodes live
    on the spine; chain identity is free by construction.

DOCTRINE: all grade law comes from ``grade_law`` (the drainage solve
reads ``adjacent_ground_envelope`` per bounding parent — no rule numbers
here); the spine is the ONLY new geometry; the boundary is verbatim.  The
spine is an OPEN WAY floating >= 2 m inside the gap (the 2026-07-09
round-2 redesign): it never touches the ring, so there is no landing
geometry, no T-vertex insertion, and no polygon split.

Behind ``config.GAP_FILL_SPINE_ENABLED`` (env ``O4_GAP_FILL_SPINE``); the
module checks the gate itself so the pipeline wiring stays one call.

SLICE B STAGE B2 (one-solve terrain absorption, gate
``O4_ONE_SOLVE_TERRAIN`` + ``O4_ONE_SOLVE_TERRAIN_GAP_FILL_SPINE``,
docs/slice_b_solver_absorption_design.md §B2): gate-ON, the spine
GEOMETRY is constructed PRE-SOLVE (``construct_gap_fill_presolve``) so
every spine vertex becomes a FREE solver variable — envelope INTERVAL
edges to its two frozen-nearest bounding pavement stations, a DEM seed,
and a ``TAXIWAY_MAX_GRADE_CHANGE_PER_M`` second-difference fairing along
the chain (the solver side lives in ``elevation_per_surface``).  The
analytic valuation below (``_spine_interval`` / ``_drain_target`` /
``_smooth_spine``) DIES gate-ON: the emitter reads the solve's writeback
from ``layout.gap_fill_presolve`` instead.  Face EMISSION (census,
blockers, legacy supersession, verbatim rings) stays at the post-solve
slot in both modes — the blocker subjects (legacy surface_clearance
strips, groundside, ribbons) only exist post-solve, and the emitted ring
must be the pavement chain as it stands AT emission
(conformance-densified) to stay verbatim.
"""
from __future__ import annotations

import bisect
import math
import os

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import split, unary_union

import O4_UI_Utils as UI

# Module-local catch tuple, matching adjacent_ground's convention
# (shapely-domain + ValueError; never built-ins).
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

from .config import (
    ADJACENT_GROUND_LIP_WIDTH_M,
    APRON_SHOULDER_WIDTH_M,
    GAP_FILL_INTERIOR_FLOOR_DEPTH_M,
    GAP_FILL_INTERIOR_RINGS_ENABLED,
    GAP_FILL_MAX_WIDTH_M,
    GAP_FILL_MIN_AREA_M2,
    GAP_FILL_SPINE_ENABLED,
    GAP_FILL_SPINE_STEP_M,
    OPEN_FRONTAGE_CLOSE_M,
    RUNWAY_STRIP_HALF_WIDTH_BY_CODE,
    runway_code_number,
    taxiway_strip_graded_half_width_for_letter,
)
from .grade_law import adjacent_ground_envelope
from .layout import (
    BuiltShape,
    ROLE_APRON,
    ROLE_BUILDING,
    ROLE_CROSS_CONNECTOR,
    ROLE_GRADED_STRIP,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL,
    ROLE_STUB,
    taxi_shape_code_letter,
)
from .clearance import (
    _AIRSIDE_PAVEMENT_ROLES,
    _edge_interp_alt,
    _nearest_pav_alt,
    _open_coords,
)
from .emit_decimate import _key
from .geom_safe import min_rotated_rect

__all__ = ["emit_gap_fill_spines", "construct_gap_fill_presolve"]

_GAP_FILL_REF = "gap_fill_spine"
# Pit-fill patches from the enclosed-pocket interior depth floor
# (``emit_gap_interior_floor``, owner ruling 2026-07-19).
_GAP_PIT_FLOOR_REF = "gap_pit_floor"
# Open-frontage corridor faces carry their OWN ref so they are
# distinguishable from enclosed-gap faces and from the legacy
# ``adjacent_ground`` bands they supersede (the DEM-free tear sentinel
# in tools/check_grade.py keys on ref=="adjacent_ground", so a corridor
# face is never mis-flagged as a band tear — and, having no band-vs-band
# clip seams, it produces none).
_OPEN_FRONTAGE_REF = "open_frontage_spine"
# Standoff buffered around every foreign shape before it is subtracted
# from the corridor closing (the groundside no-weld ruling, 2026-07-09:
# grading strips keep >= 1 m off groundside pavement + buildings).  A
# corridor slab therefore never welds onto a foreign shape; the corridor
# band / daylight law owns the 1 m collar.
_OPEN_FRONTAGE_FOREIGN_STANDOFF_M = 1.0
# A cross-section thinner than this is not a gradeable half-gap — the
# station is a pinch of the ring, not the drainage body.
_MIN_CROSS_WIDTH_M = 2.0
# Target the corridor DRAINAGE offset a quarter of the way UP from the
# floor: fall from both pavement edges at >= the law minimum without
# cutting to the 5 % floor (user design ruling 2026-07-09).
_DRAIN_FROM_CEILING = 0.25
# Longitudinal relaxation sweeps over the spine (second-difference,
# endpoints pinned to their boundary pavement values).
_SMOOTH_SWEEPS = 20

# ── GAP INTERIOR RING constants (ratified 2026-07-11, REVISED per the
# in-sim round-8 ruling: rings are ALWAYS complete closed loops; the
# gating lives in the VALUES, not the geometry) ────────────────────────
# A ring station whose clamped value sits within this of the terrain is
# a VALUE NO-OP (the ring rides the terrain there — invisible in the
# mesh).  A gap where EVERY station of BOTH rings is a no-op skips its
# rings entirely (the per-gap economy gate — all-or-nothing, never
# per-arc: arc ends read as ragged walls in the sim, round-8 verdict).
_RING_VALUE_NOOP_TOLERANCE_M = 0.05
# Minimum clearance a ring node keeps from the gap boundary, the spine
# and every other ring chain — the near-parallel Ruppert guard (the
# codebase's spine standoff is 2.0 m; rings reuse the same class of
# floor, slightly tighter so a lip ring at 3 m offset survives corners).
_RING_MIN_CLEARANCE_M = 1.5
# Minimum spacing between consecutive accepted ring nodes (corner fans
# converge inward offsets; closer nodes are dropped).
_RING_MIN_NODE_SPACING_M = 2.0
# ROUND-9 (Noah 2026-07-14): the rings are boundaries of TRUE POLYGON
# INWARD OFFSETS (the round-8 per-station offset walk self-crossed at
# boundary concavities and parent-width transitions — 9 of 27 CYXY
# loops).  Region smoothing = morphological opening + closing at this
# minimum-feature radius: fingers/spikes thinner than it are dropped,
# boundary notches narrower than it are NOT traced (calibrated against
# Noah's hand-edited reference loop, CYXY_auto_MOD way -68615 — the
# reference expresses GEOMETRY only).
_RING_MIN_FEATURE_RADIUS_M = 10.0
# Hard clearance collar against the gap boundary: every smoothed ring
# region is clipped to gap.buffer(-this), so no notch-fill can push a
# loop into the zero-lens danger zone at the pavement chain.
_RING_BOUNDARY_CLEARANCE_M = 1.6
# Maximum along-ring VALUE drop per meter (max-relaxation raising the
# low side, capped at each node's point-law ceiling): the point-law
# BOUNDS step where the governing parent switches family (a 75 m
# runway band floor sits meters below a 12.5 m junction band floor),
# and terrain alone cannot be asked to absorb that step between two
# 15 m stations (round-8: no cliffs ALONG the ring).  5 % = the band
# maximum down slope — the steepest lawful graded transition.
_RING_ALONG_BENCH_SLOPE = 0.05
# Ring 1 (lip) is suppressed when the effective ring-2 offset comes
# within this of the lip offset — two near-coincident parallel
# breaklines are exactly the lens class the zero-lens law forbids.
_RING_MIN_SEPARATION_M = 2.0

_RUNWAY_ROLES = (ROLE_RUNWAY, ROLE_RUNWAY_CROSSING)
_APRON_ROLES = (ROLE_APRON,)
_TAXIWAY_ROLES = (
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
)


def _unit(dx: float, dy: float):
    d = math.hypot(dx, dy)
    if d < 1e-12:
        return None
    return (dx / d, dy / d)


def _long_side_length(polygon) -> float:
    """Length of the polygon's longest vertex chord — the runway
    length proxy for code-number keying (no source_runways here)."""
    try:
        ring = list(polygon.exterior.coords)
    except _GEOM_EXC:
        return 0.0
    best = 0.0
    n = len(ring)
    for i in range(n):
        xi, yi = ring[i]
        for j in range(i + 1, n):
            xj, yj = ring[j]
            d = math.hypot(xj - xi, yj - yi)
            if d > best:
                best = d
    return best


def _parent_family_code(layout, shape):
    """Resolve ``(role, code_number, code_letter)`` for a bounding
    pavement ``shape`` so ``adjacent_ground_envelope`` picks the family's
    corridor.  Replicates adjacent_ground's ``_family_params`` role/code
    logic MINIMALLY — that helper needs ``rw_axes`` built from
    ``source_runways``, which this emitter's signature does not receive,
    so the runway code number is read from the shape's OWN long-chord
    length via ``runway_code_number`` instead of a row-100 axis.  The
    envelope keys runways by code NUMBER and taxiways by code LETTER; the
    shape's actual role string is passed through (it already lives in the
    envelope's per-family role sets)."""
    role = shape.role
    if role in _RUNWAY_ROLES:
        return (role, runway_code_number(_long_side_length(shape.polygon)),
                None)
    if role in _APRON_ROLES:
        return (role, None, None)
    if role in _TAXIWAY_ROLES:
        return (role, None, taxi_shape_code_letter(layout, shape))
    return (role, None, None)


def _parent_flat_value(parent):
    """A gap parent's FLAT value — the primary representation for
    building pads (user ruling: buildings are flat; the ROLE_BUILDING
    writeback stores avg of corners → ``altitude``) and the FALLBACK for
    any parent whose per-vertex ``node_altitudes`` do not align with the
    ring being registered (per-vertex values are preferred at the call
    site — runway-end skirts carry the governed runway-end profile
    per vertex)."""
    if getattr(parent, "altitude", None) is not None:
        return float(parent.altitude)
    na = getattr(parent, "node_altitudes", None)
    if na:
        vals = [v for v in na if v is not None]
        if vals:
            return float(sum(vals) / len(vals))
    return None


def _face_is_verbatim(face_poly, chain_keys) -> bool:
    """True when EVERY boundary vertex of ``face_poly`` (exterior and any
    interior parent ring) is a VERBATIM pavement-or-parent ring vertex —
    chain identity.  A residual difference that mints a foreign crossing
    point (a parent edge cutting a pavement edge mid-span) fails this:
    that vertex is new boundary geometry and would Ruppert-explode, so
    the part is blocked."""
    try:
        rings = [face_poly.exterior] + list(face_poly.interiors)
    except _GEOM_EXC:
        return False
    for ring in rings:
        for vx, vy in ring.coords:
            if _key(vx, vy) not in chain_keys:
                return False
    return True


def _parent_residual_faces(gap_poly, parents, chain_keys):
    """The gradeable face(s) for one enclosed gap.  With no bounding
    parent inside it is the gap itself.  With parent shape(s) inside —
    a BUILDING PAD (flat value authority, user design 2026-07-09 queue
    item 5) or a RUNWAY-END SKIRT (NON-flat value authority whose ring
    vertices carry the governed inverse-RESA runway-end profile,
    supervisor follow-up 2026-07-09) — the parent BOUNDS the gap the
    way pavement does, and the gradeable ground is the RESIDUAL
    ``gap minus parent_union``, split into its chain-safe parts:

      * a parent that FILLS its hole leaves no residual above
        ``GAP_FILL_MIN_AREA_M2`` → the gap lawfully vanishes (the
        parent surface IS the ground there — nothing left to drain);
      * a wholly-interior parent leaves an ANNULAR residual whose inner
        ring is the parent chain VERBATIM (the emitted face covers the
        parent footprint; the parent's own way prevails there by
        X-Plane seed-region processing, the junction-hole precedent in
        to_osm — the parent shape itself still emits exactly as today);
      * a residual part whose boundary is NOT verbatim (a
        difference-minted crossing vertex — e.g. a parent ring cutting
        a pavement edge mid-span) is blocked (zero-lens law).

    Every candidate + skip is logged (no silent skip)."""
    parents_in = []
    for p in parents:
        try:
            if gap_poly.intersection(p.polygon).area > 1.0:
                parents_in.append(p)
        except _GEOM_EXC:
            continue
    if not parents_in:
        return [gap_poly]
    try:
        parent_union = unary_union([p.polygon for p in parents_in])
        residual = gap_poly.difference(parent_union)
    except _GEOM_EXC:
        residual = None
    parts = ([] if residual is None or residual.is_empty
             else [residual] if residual.geom_type == "Polygon"
             else [g for g in getattr(residual, "geoms", [])
                   if g.geom_type == "Polygon"])
    refs = ",".join(str(getattr(p, "ref", None) or p.role)
                    for p in parents_in)
    residual_area = sum(g.area for g in parts)
    _c = gap_poly.centroid
    if residual_area < GAP_FILL_MIN_AREA_M2:
        UI.vprint(1, f"  [gap-fill] parent fills gap (residual "
                     f"{residual_area:.0f} < {GAP_FILL_MIN_AREA_M2:.0f} m2, "
                     f"parent(s)={refs}) — the parent IS the surface; "
                     f"centroid=({_c.x:.0f},{_c.y:.0f}) skipped.")
        return []
    faces = []
    for g in parts:
        if g.is_empty or g.area < GAP_FILL_MIN_AREA_M2:
            continue
        if not _face_is_verbatim(g, chain_keys):
            _cc = g.centroid
            UI.vprint(1, f"  [gap-fill] parent-residual part non-verbatim "
                         f"boundary (parent(s)={refs}) area={g.area:.0f} m2 "
                         f"centroid=({_cc.x:.0f},{_cc.y:.0f}) — blocked.")
            continue
        faces.append(g)
    UI.vprint(1, f"  [gap-fill] parent-bounded gap (parent(s)={refs}): "
                 f"{len(faces)} residual face(s) of {residual_area:.0f} m2 "
                 f"centroid=({_c.x:.0f},{_c.y:.0f}).")
    return faces


def _grade_face(layout, airside, face_poly, step, registry,
                dem=None, tile_lat=None, tile_lon=None,
                rw_axes=None) -> int:
    """Area/width-gate ONE gradeable face (a whole enclosed gap, or a
    pad-residual part) and emit its drainage spine.  Logs the candidate
    and any lawful width/area skip.  Returns the emitted face count.

    ``dem``/``tile_lat``/``tile_lon``/``rw_axes`` feed the interior-ring
    construction (gate ``O4_GAP_FILL_INTERIOR_RINGS``); with ``dem``
    None (the open-frontage path, synthetic fixtures without terrain)
    the violation trigger cannot fire and no ring emits."""
    if face_poly.is_empty or not face_poly.is_valid:
        return 0
    if face_poly.area < GAP_FILL_MIN_AREA_M2:
        return 0
    try:
        axes = _mrr_axes(min_rotated_rect(face_poly))
    except _GEOM_EXC:
        return 0
    if axes is None or axes[1] is None:
        return 0
    short_side, long_dir, long_len = axes
    _c = face_poly.centroid
    UI.vprint(1, f"  [gap-fill] candidate area="
                 f"{face_poly.area:.0f} m2 short="
                 f"{short_side:.0f} centroid=({_c.x:.0f},{_c.y:.0f})")
    if short_side > GAP_FILL_MAX_WIDTH_M:
        UI.vprint(1, f"  [gap-fill] skipped gap (width "
                     f"{short_side:.0f} > {GAP_FILL_MAX_WIDTH_M:.0f})"
                     f" area={face_poly.area:.0f} m2")
        return 0
    return _emit_one_gap(layout, airside, face_poly, long_dir, long_len,
                         step, registry, dem=dem, tile_lat=tile_lat,
                         tile_lon=tile_lon, rw_axes=rw_axes)


def _mrr_axes(mrr):
    """``(short_side_m, long_unit_dir, long_side_m)`` of a minimum
    rotated rectangle polygon."""
    coords = list(mrr.exterior.coords)
    if len(coords) < 4:
        return None
    p0, p1, p2 = coords[0], coords[1], coords[2]
    e1 = (p1[0] - p0[0], p1[1] - p0[1])
    e2 = (p2[0] - p1[0], p2[1] - p1[1])
    l1 = math.hypot(*e1)
    l2 = math.hypot(*e2)
    if l1 >= l2:
        long_dir = _unit(*e1)
        return (l2, long_dir, l1)
    long_dir = _unit(*e2)
    return (l1, long_dir, l2)


def _boundary_intersection(p_from, direction, gap_poly, reach):
    """First point where a ray from ``p_from`` along ``direction`` meets
    the gap ring — the exact boundary coordinate the spine endpoint takes
    (a T-vertex the conformance weld inserts)."""
    fx, fy = p_from
    dx, dy = direction
    ray = LineString([(fx, fy),
                      (fx + dx * reach, fy + dy * reach)])
    try:
        inter = ray.intersection(gap_poly.exterior)
    except _GEOM_EXC:
        return None
    if inter.is_empty:
        return None
    pts = ([inter] if inter.geom_type == "Point"
           else [g for g in getattr(inter, "geoms", [])
                 if g.geom_type == "Point"])
    best = None
    best_d = None
    for p in pts:
        d = math.hypot(p.x - fx, p.y - fy)
        if d < 1e-6:
            continue
        if best_d is None or d < best_d:
            best_d, best = d, (p.x, p.y)
    return best


def _build_spine(gap_poly, long_dir, long_len, step):
    """March cross-sections along the long axis, take each widest
    section's midpoint, then extend both ends exactly onto the gap ring.
    Returns the ordered spine coordinate list (>= 3 points), or None."""
    ux, uy = long_dir
    vx, vy = (-uy, ux)                    # perpendicular unit
    cen = gap_poly.centroid
    cx, cy = cen.x, cen.y
    span = long_len + step               # half-length of a cutting line
    try:
        ring = list(gap_poly.exterior.coords)
    except _GEOM_EXC:
        return None
    projs = [(x - cx) * ux + (y - cy) * uy for x, y in ring]
    s_min, s_max = min(projs), max(projs)
    mids: list[tuple[float, float]] = []
    n_st = int(math.floor((s_max - s_min) / step))
    for i in range(n_st + 1):
        s = s_min + i * step
        bx, by = cx + ux * s, cy + uy * s
        cutter = LineString([(bx - vx * span, by - vy * span),
                             (bx + vx * span, by + vy * span)])
        try:
            inter = cutter.intersection(gap_poly)
        except _GEOM_EXC:
            continue
        segs = ([inter] if inter.geom_type == "LineString"
                else [g for g in getattr(inter, "geoms", [])
                      if g.geom_type == "LineString"])
        segs = [g for g in segs if not g.is_empty]
        if not segs:
            continue
        widest = max(segs, key=lambda g: g.length)
        if widest.length < _MIN_CROSS_WIDTH_M:
            continue
        mid = widest.interpolate(0.5, normalized=True)
        mids.append((mid.x, mid.y))
    if len(mids) < 2:
        return None
    # OPEN-WAY SPINE (user design 2026-07-09, round 2): the spine
    # never touches the gap boundary — it floats inside as an open
    # constrained way (the crown-spine mechanism), so the boundary
    # stays the pavement chain verbatim EVERYWHERE, there is no
    # landing geometry (the shallow-landing 96 mm sliver class dies
    # by construction), no polygon split, and U-shaped / partially
    # open gaps need no special casing.  Hold the spine ends >= 2 m
    # off the ring.
    if list(gap_poly.interiors):
        # ANNULAR face (a gap parent — building pad / runway-end skirt
        # — wholly inside): the parent's ring is a constrained chain
        # too, so EVERY spine point keeps >= 2 m off the FULL boundary
        # (exterior + parent rings) — a spine vertex hugging the parent
        # ring would mint a near-parallel pair.  Segments that would
        # cross the parent hole are handled by the sub-chain split at
        # emission.  Faces WITHOUT interiors keep the original
        # end-trim path byte-identical.
        ring_ls = gap_poly.boundary
        out = [p for p in mids if ring_ls.distance(Point(p)) >= 2.0]
    else:
        ring_ls = gap_poly.exterior
        out = [p for p in mids]
        while out and ring_ls.distance(Point(out[0])) < 2.0:
            out = out[1:]
        while out and ring_ls.distance(Point(out[-1])) < 2.0:
            out = out[:-1]
    # De-duplicate consecutive coincident points.
    dedup: list[tuple[float, float]] = []
    for p in out:
        if not dedup or math.hypot(p[0] - dedup[-1][0],
                                   p[1] - dedup[-1][1]) > 1e-6:
            dedup.append(p)
    return dedup if len(dedup) >= 2 else None


def _spine_interval(layout, airside, px, py):
    """The drainage interval ``(lo, hi)`` and reference edge altitudes at
    spine point ``(px, py)``: the two nearest DISTINCT bounding pavement
    parents each contribute ``[edge + floor(d), edge + ceil(d)]`` from
    ``adjacent_ground_envelope``; the combined interval is
    ``[max(floors), min(ceils)]``.  On an empty intersection it falls back
    to the nearer parent's own interval (user design ruling 2026-07-09)."""
    p = Point(px, py)
    cands = []
    for s in airside:
        try:
            d = s.polygon.exterior.distance(p)
        except _GEOM_EXC:
            continue
        cands.append((d, s))
    cands.sort(key=lambda t: t[0])
    parents = cands[:2]
    per_parent = []                      # (edge_alt, floor_abs|None, ceil_abs|None)
    edge_alts = []
    for d, s in parents:
        e = _edge_interp_alt(s, px, py)
        if e is None:
            e = _nearest_pav_alt(airside, px, py, max_distance_m=1e9)
        if e is None:
            continue
        role, cn, cl = _parent_family_code(layout, s)
        try:
            floor_off, ceil_off = adjacent_ground_envelope(
                role, cn, cl, max(0.0, d))
        except _GEOM_EXC:
            continue
        if floor_off is None and ceil_off is None:
            edge_alts.append(float(e))
            continue
        edge_alts.append(float(e))
        per_parent.append((
            float(e),
            None if floor_off is None else float(e) + floor_off,
            None if ceil_off is None else float(e) + ceil_off))
    floors = [q[1] for q in per_parent if q[1] is not None]
    ceils = [q[2] for q in per_parent if q[2] is not None]
    lo = max(floors) if floors else None
    hi = min(ceils) if ceils else None
    if lo is not None and hi is not None and lo > hi and per_parent:
        # Empty intersection — the nearer (first) parent's interval alone.
        lo, hi = per_parent[0][1], per_parent[0][2]
    return lo, hi, edge_alts


def _drain_target(lo, hi, edge_alts):
    """Value inside the drainage interval: a quarter up from the floor
    (fall from both edges at >= the law minimum, not cut to the 5 %
    floor).  Falls back to a single bound, then to the lower pavement
    seed (user design ruling 2026-07-09)."""
    if lo is not None and hi is not None:
        return hi - _DRAIN_FROM_CEILING * (hi - lo)
    if hi is not None:
        return hi
    if lo is not None:
        return lo
    return min(edge_alts) if edge_alts else None


def _smooth_spine(vals, intervals, sweeps):
    """Second-difference relaxation clamped into each vertex interval;
    endpoints pinned."""
    n = len(vals)
    if n < 3:
        return list(vals)
    v = list(vals)
    for _ in range(sweeps):
        for i in range(1, n - 1):
            cand = 0.5 * (v[i - 1] + v[i + 1])
            lo, hi = intervals[i]
            if lo is not None:
                cand = max(cand, lo)
            if hi is not None:
                cand = min(cand, hi)
            v[i] = cand
    return v


def _interp_along_spine(spine_line, cum, vals, px, py):
    """Value at a spine-collinear point by arc-length interpolation."""
    s = spine_line.project(Point(px, py))
    k = bisect.bisect_right(cum, s) - 1
    k = max(0, min(k, len(vals) - 2))
    seg = cum[k + 1] - cum[k]
    t = 0.0 if seg <= 0 else (s - cum[k]) / seg
    return vals[k] + t * (vals[k + 1] - vals[k])


# ══════════════════════════════════════════════════════════════════════
# GAP INTERIOR RINGS (ratified 2026-07-11, gate
# O4_GAP_FILL_INTERIOR_RINGS, requires O4_GAP_FILL_SPINE; REVISED per
# Noah's in-sim round-8 ruling — complete loops, value-gated)
#
# A single mid-gap spine cannot enforce the graded-band law when the
# enclosed interior genuinely drops: the mesh spans pavement edge to
# spine in ONE leg, so a low spine puts the whole drop AT the pavement
# edge (CYXY evidence node 60.7210897,-135.0776149 — 73 % where the
# band allows 5 %).  Rings mirror the EXTERIOR adjacent-ground band
# cross-section bent around the gap: the verbatim gap boundary is the
# d=0 row (pavement values), ring 1 the drainage-lip breakpoint row,
# ring 2 the graded band-edge row — the finite-to-open floor
# transition locus.
#
# ROUND-8 REVISION (the first cut violation-gated the GEOMETRY into
# per-arc runs; 66 fragmented chains read as ragged walls in the sim):
# both rings are ALWAYS complete, unbroken, concentric closed loops —
# no arcs, no taper, nothing to fall off.  The gating lives in the
# VALUES: each station carries clamp(terrain, floor, ceiling) at its
# point-law distances (the exterior per-vertex law verbatim — the
# ``_make_edge_projection_resampler`` semantics applied at the ring
# stations).  Where terrain is lawful the ring RIDES the terrain (a
# value no-op, invisible in the mesh); where it violates, the clamp
# pins to the floor (fill) or the ceiling (cut).  Terrain is
# continuous and the clamp is continuous, so the ring has no cliffs
# ALONG it anywhere, by construction.  A gap where EVERY station of
# BOTH rings is a value no-op skips its rings entirely (per-gap node
# economy — all-or-nothing, never per-arc).
#
# ENCODING (ratified answer 2, unchanged in round 8): a DERIVED value
# read from the post-solve pavement edges at emission — NOT new solver
# variables.  An equality-pinned solver variable adds zero information
# to the solve while adding interval edges that can conflict inside
# the machinery; deriving at emission guarantees exact clamp equality
# with no float round-trip, keeps the solve byte-identical gate-ON vs
# gate-OFF, and cannot regress solver feasibility.  Rings are emitted
# as constrained BREAKLINE ways inside the (single, verbatim) gap face
# via the crown-spine mechanism (layout.gap_interior_rings → to_osm →
# include_patches DUMMY edges) — never a polygon split (an inward
# offset ring of a concave gap self-intersects, and a split would mint
# the parallel shared-edge pair class that Ruppert-explodes).
# ══════════════════════════════════════════════════════════════════════


def _ring_runway_axes(layout, source_runways):
    """Runway centerline axes ``(LineString, unit, length)`` in local
    meters — the TRUE ICAO code source for runway-bounded ring widths
    (ratified answer 1: the adjacent_ground ``_family_params`` approach;
    a tile-cut runway SEGMENT's own chord under-keys the code)."""
    axes: list[tuple] = []
    if not source_runways:
        return axes
    for r in source_runways:
        try:
            rax, ray = layout.ll_to_m(r.lat_a, r.lon_a)
            rbx, rby = layout.ll_to_m(r.lat_b, r.lon_b)
        except (_GEOM_EXC + (AttributeError, TypeError)):
            continue
        rlen = math.hypot(rbx - rax, rby - ray)
        if rlen < 1.0:
            continue
        axes.append((LineString([(rax, ray), (rbx, rby)]),
                     ((rbx - rax) / rlen, (rby - ray) / rlen), rlen))
    return axes


def _ring_parent_band(layout, shape, rw_axes):
    """``(role, code_number, code_letter, band_half_width_m)`` for one
    bounding airside ``shape`` — the family key + graded band-edge
    distance the interior ring is built at.  Runway shapes key their
    ICAO code from the nearest RUNWAY AXIS length when axes are
    available (ratified answer 1), falling back to the segment-chord
    proxy only without them.  Returns None for a family with no finite
    band floor (nothing to pin a ring to)."""
    role = shape.role
    if role in _RUNWAY_ROLES:
        code = None
        if rw_axes:
            try:
                cen = shape.polygon.centroid
                axis = min(rw_axes, key=lambda a: a[0].distance(cen))
                code = runway_code_number(axis[2])
            except _GEOM_EXC:
                code = None
        if code is None:
            code = runway_code_number(_long_side_length(shape.polygon))
        return (role, code, None, RUNWAY_STRIP_HALF_WIDTH_BY_CODE[code])
    if role in _TAXIWAY_ROLES:
        letter = taxi_shape_code_letter(layout, shape)
        return (role, None, letter,
                taxiway_strip_graded_half_width_for_letter(letter))
    if role in _APRON_ROLES:
        return (role, None, None, APRON_SHOULDER_WIDTH_M)
    return None


def _build_gap_interior_rings(layout, airside, gap_poly, spine, values,
                              dem, tile_lat, tile_lon, rw_axes, step):
    """Construct the interior ring breaklines for ONE emitted gap face
    (round-9 rebuild: TRUE POLYGON INWARD OFFSETS — the per-station
    offset walk of round 8 self-crossed at boundary concavities and
    parent-width transitions, 9 self-intersecting loops at CYXY).

    REGIONS, not stations:

      * ring-2 CORE  = gap minus the union of every bounding parent's
        polygon buffered by ITS band width (per-parent widths, true
        runway codes) — the exact distance-law band annulus;
      * ring-1 region = gap shrunk by the drainage lip
        (``gap.buffer(-ADJACENT_GROUND_LIP_WIDTH_M)``).

    Both regions are SMOOTHED by a morphological opening + closing at
    ``_RING_MIN_FEATURE_RADIUS_M`` (drops fingers/spikes, fills
    boundary-notch tracing — calibrated against Noah's hand-edited
    reference loop, CYXY_auto_MOD way -68615) and clipped to
    ``gap.buffer(-_RING_BOUNDARY_CLEARANCE_M)`` (the zero-lens
    boundary floor).  Shapely polygon boundaries are SIMPLE BY
    CONSTRUCTION; simplicity is a HARD INVARIANT re-asserted on every
    resampled loop (a non-simple product raises — a bug, never a
    repair case).  A concave gap naturally splitting into several
    core components is CORRECT — each sub-area gets its collar; the
    hole-in-the-middle rung falls out of the geometry.

    Kept from round 8: closed loops; per-node values
    ``clamp(terrain, floor, ceiling)`` at TRUE two-nearest-parent
    distances (``_point_interval`` — the smoothed loop's varying
    offset is handled by true-distance evaluation); the two-sided
    along-ring value bench; the per-gap economy skip; ring 1
    all-or-nothing per gap (dropped when any ring-1 loop crowds a
    ring-2 loop); the spine trimmed to the core; the ring-2 ceiling
    re-coupling.  Returns ``(chains, clamped_values, stats,
    spine_chains)`` exactly as before."""
    lip = ADJACENT_GROUND_LIP_WIDTH_M
    boundary_ls = gap_poly.boundary
    spine_ls = LineString(spine) if len(spine) >= 2 else None

    def _dem_at(x, y):
        try:
            from .elevation import _sample_dem
            lat, lon = layout.m_to_ll(x, y)
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None

    band_cache: dict[int, tuple | None] = {}

    def _band_of(s):
        key = id(s)
        if key not in band_cache:
            band_cache[key] = _ring_parent_band(layout, s, rw_axes)
        return band_cache[key]

    def _point_interval(pt):
        """The lawful ``(floor_abs, ceiling_abs)`` interval AT a ring
        point: the point's TRUE distance to each of its two nearest
        bounding parents (the ``_spine_interval`` two-nearest
        convention), each parent's envelope at that distance on top of
        its interpolated edge altitude — combined as
        ``[max(floors), min(ceilings)]``; an empty intersection falls
        back to the NEARER parent's own interval.  The FLOOR is
        evaluated at ``min(d, band width)`` (the exterior emitter's
        ``floor_depth`` clamp — the band-edge floor never opens where
        the smoothed loop swings past the band edge).  ``(None,
        None)`` = no law governs the point."""
        p = Point(pt)
        cands = []
        for s in airside:
            try:
                d = s.polygon.exterior.distance(p)
            except _GEOM_EXC:
                continue
            cands.append((d, s))
        cands.sort(key=lambda t: t[0])
        per_parent = []
        for d, s in cands[:2]:
            pband = _band_of(s)
            if pband is None:
                continue
            prole, pcn, pcl, pw = pband
            try:
                fo, _co_at_w = adjacent_ground_envelope(
                    prole, pcn, pcl, max(0.0, min(d, pw)))
                _fo_raw, co = adjacent_ground_envelope(
                    prole, pcn, pcl, max(0.0, d))
            except _GEOM_EXC:
                continue
            if fo is None and co is None:
                continue
            e = _edge_interp_alt(s, pt[0], pt[1])
            if e is None:
                e = _nearest_pav_alt(airside, pt[0], pt[1],
                                     max_distance_m=1e9)
            if e is None:
                continue
            per_parent.append((
                None if fo is None else float(e) + float(fo),
                None if co is None else float(e) + float(co)))
        floors = [q[0] for q in per_parent if q[0] is not None]
        ceils = [q[1] for q in per_parent if q[1] is not None]
        lo = max(floors) if floors else None
        hi = min(ceils) if ceils else None
        if lo is not None and hi is not None and lo > hi and per_parent:
            lo, hi = per_parent[0]          # nearer parent's interval
        return lo, hi

    def _level(pt):
        """Per-node record: VALUE = clamp(terrain, floor, ceiling) at
        the point-law interval (round-8 value semantics, unchanged in
        round 9).  Lawful terrain → value no-op (the ring rides the
        ground); drop below floor → floor pin (fill); rise above
        ceiling → ceiling pin (cut)."""
        if pt is None:
            return None
        terrain = _dem_at(*pt)
        lo, hi = _point_interval(pt)
        if terrain is None:
            v = lo if lo is not None else hi
            if v is None:
                return None
            return {"pt": pt, "v": float(v), "terrain": None,
                    "lo": lo, "hi": hi, "noop": False,
                    "floor_engaged": False}
        v = float(terrain)
        if lo is not None:
            v = max(v, lo)
        if hi is not None:
            v = min(v, hi)
        return {"pt": pt, "v": v, "terrain": float(terrain),
                "lo": lo, "hi": hi,
                "noop": abs(v - terrain) <= _RING_VALUE_NOOP_TOLERANCE_M,
                "floor_engaged": (lo is not None and
                                  v > terrain +
                                  _RING_VALUE_NOOP_TOLERANCE_M)}

    stats = {"stations": 0, "eligible": 0, "noop_stations": 0,
             "engaged_stations": 0, "chains": 0, "nodes": 0,
             "skipped": False}

    # ── REGIONS (round-9): polygon inward offsets ─────────────────────
    zones = []
    for s in airside:
        pband = _band_of(s)
        if pband is None:
            continue
        w = pband[3]
        try:
            if s.polygon.distance(gap_poly) > w:
                continue
            zones.append(s.polygon.buffer(w, quad_segs=4))
        except _GEOM_EXC:
            continue
    try:
        core = (gap_poly.difference(unary_union(zones)) if zones
                else None)
        lip_region = gap_poly.buffer(-lip, quad_segs=4)
    except _GEOM_EXC:
        return [], list(values), stats, None

    def _smooth_region(region):
        """Morphological opening (drop fingers/spikes thinner than the
        minimum-feature radius) then closing (fill notch tracing),
        clipped to the hard boundary-clearance collar; returns the
        polygon parts above the noise floor."""
        if region is None or region.is_empty:
            return []
        r = _RING_MIN_FEATURE_RADIUS_M
        try:
            region = region.buffer(-r, quad_segs=4).buffer(r, quad_segs=4)
            region = region.buffer(r, quad_segs=4).buffer(-r, quad_segs=4)
            region = region.intersection(
                gap_poly.buffer(-_RING_BOUNDARY_CLEARANCE_M, quad_segs=4))
        except _GEOM_EXC:
            return []
        return [g for g in _poly_parts(region)
                if g.area >= GAP_FILL_MIN_AREA_M2]

    core_parts = _smooth_region(core)
    lip_parts = _smooth_region(lip_region)

    def _region_loops(parts):
        loops = []
        for pg in parts:
            for ring in [pg.exterior] + list(pg.interiors):
                try:
                    if ring.length >= 6.0 * _RING_MIN_NODE_SPACING_M:
                        loops.append(ring)
                except _GEOM_EXC:
                    continue
        return loops

    ring2_loops = _region_loops(core_parts)
    ring1_loops = _region_loops(lip_parts)
    # Ring 1 all-or-nothing per gap (round-8 rung 3): drop it whole
    # when any ring-1 loop crowds a ring-2 loop.
    if ring1_loops and ring2_loops:
        try:
            if any(l1.distance(l2) < _RING_MIN_SEPARATION_M
                   for l1 in ring1_loops for l2 in ring2_loops):
                ring1_loops = []
        except _GEOM_EXC:
            ring1_loops = []
    if not ring2_loops and not ring1_loops:
        return [], list(values), stats, None

    # ── Resample each loop at the station step + point-law values ────
    sampled = []                # (level_tag, pts, recs)
    inner_cover = None
    try:
        inner_cover = gap_poly.buffer(-0.8, quad_segs=4)
    except _GEOM_EXC:
        inner_cover = None
    for tag, loops in (("ring2", ring2_loops), ("ring1", ring1_loops)):
        for loop in loops:
            perim = loop.length
            n = max(8, int(round(perim / step)))
            if perim / n < _RING_MIN_NODE_SPACING_M:
                n = max(3, int(perim // _RING_MIN_NODE_SPACING_M))
            if n < 3:
                continue
            pts = None
            # Chords of a coarse resample can cut inside pavement at a
            # concave boundary detail (a foreign-crossing mint).
            # Densify until the closed polyline stays covered by the
            # gap; final fallback = the smoothed loop's own vertices,
            # topology-preserving-simplified (simple by construction).
            for n_try in (n, 2 * n, 4 * n):
                cand = []
                for i in range(n_try):
                    q = loop.interpolate((i / n_try) * perim)
                    cand.append((float(q.x), float(q.y)))
                try:
                    ok = (inner_cover is None or inner_cover.covers(
                        LineString(cand + [cand[0]])))
                except _GEOM_EXC:
                    ok = False
                if ok:
                    pts = cand
                    break
            if pts is None:
                try:
                    simp = loop.simplify(0.75, preserve_topology=True)
                    pts = [(float(x), float(y))
                           for x, y in list(simp.coords)[:-1]]
                except _GEOM_EXC:
                    continue
                if len(pts) < 3:
                    continue
            # SIMPLICITY — the round-9 hard invariant.  The source is
            # a shapely polygon boundary (simple by construction); the
            # resampled chord polygon must stay simple too (the
            # minimum-feature radius bounds curvature well above the
            # station step).  A violation is a BUG, never a repair
            # case.
            try:
                if not Polygon(pts).is_valid:
                    raise ValueError(
                        "gap interior ring loop resampled NON-SIMPLE "
                        f"(gap centroid {gap_poly.centroid.wkt}) — "
                        "round-9 invariant violated")
            except _GEOM_EXC:
                raise
            recs = [_level(pt) for pt in pts]
            if any(r is None for r in recs):
                continue                    # no law + no terrain: drop
            sampled.append((tag, pts, recs))

    if not sampled:
        return [], list(values), stats, None

    # ── Per-gap economy gate (round-8, all-or-nothing) ────────────────
    engaged = noop = 0
    for _tag, _pts, recs in sampled:
        for r in recs:
            if r["noop"]:
                noop += 1
            else:
                engaged += 1
    stats["stations"] = stats["eligible"] = engaged + noop
    stats["noop_stations"] = noop
    stats["engaged_stations"] = engaged
    if engaged == 0:
        stats["skipped"] = True
        return [], list(values), stats, None

    def _bench_along(pts, alts, los, his):
        """Two-sided along-ring value bench (round-8 continuity law,
        unchanged): FILL side raises the low neighbor (ceiling-capped),
        CUT side lowers the high neighbor (floor-capped) at
        ``_RING_ALONG_BENCH_SLOPE``; every loop is closed, so the
        relaxation is cyclic.  A residual step survives only where the
        caps themselves demand it (clamp-limited)."""
        n = len(alts)
        if n < 3:
            return [round(x, 2) for x in alts]
        v = list(alts)
        order = list(range(n))
        for _ in range(2):
            for rng in (order, order[::-1]):
                prev = rng[-1]              # cyclic
                for k in rng:
                    pk, pp = pts[k], pts[prev]
                    dist = math.hypot(pk[0] - pp[0], pk[1] - pp[1])
                    allow = _RING_ALONG_BENCH_SLOPE * dist
                    cand = v[prev] - allow   # FILL: raise the low side
                    if cand > v[k]:
                        cap = his[k]
                        v[k] = cand if cap is None else min(cand, cap)
                    cand = v[prev] + allow   # CUT: lower the high side
                    if cand < v[k]:
                        cap = los[k]
                        v[k] = cand if cap is None else max(cand, cap)
                    prev = k
        return [round(x, 2) for x in v]

    chains: list[tuple[list, list]] = []
    ring2_recs: list[dict] = []
    emitted_loop_lines: list = []
    for tag, pts, recs in sampled:
        # Cross-loop zero-lens guard: a loop hugging an already
        # accepted loop below the clearance floor is dropped whole
        # (loops are region boundaries — they never cross, but two
        # components can crowd a thin sliver).
        try:
            cand_ls = LineString(pts + [pts[0]])
        except _GEOM_EXC:
            continue
        too_close = False
        for other in emitted_loop_lines:
            try:
                if cand_ls.distance(other) < _RING_MIN_CLEARANCE_M:
                    too_close = True
                    break
            except _GEOM_EXC:
                too_close = True
                break
        if too_close:
            continue
        alts = _bench_along(pts, [r["v"] for r in recs],
                            [r["lo"] for r in recs],
                            [r["hi"] for r in recs])
        out_pts = pts + [pts[0]]
        out_alts = alts + [alts[0]]
        chains.append((out_pts, out_alts))
        emitted_loop_lines.append(cand_ls)
        stats["chains"] += 1
        stats["nodes"] += len(pts)
        if tag == "ring2":
            for r, a in zip(recs, alts):
                r["benched"] = a
                ring2_recs.append(r)

    if not chains:
        return [], list(values), stats, None

    # ── Spine re-coupling (unchanged law): ring 2 is the spine's
    # CEILING where the nearest ring-2 node is FLOOR-ENGAGED and the
    # spine node sits INSIDE the core.  Values only ever move DOWN. ────
    clamped = list(values)
    core_union = unary_union(core_parts) if core_parts else None
    # The spine's TRIM REGION is the innermost emitted region: the
    # core when ring-2 loops exist; the lip region when the bands
    # cover the whole gap (core empty, ring 1 alone) — the drainage
    # crest survives inside the lip ring instead of dying with the
    # core (the zones-fully-overlap rung keeps its central spine).
    if ring2_loops and core_union is not None:
        trim_region = core_union
    else:
        try:
            trim_region = unary_union(lip_parts) if lip_parts else None
        except _GEOM_EXC:
            trim_region = None
    in_core: list[bool] = []
    for j, (sx, sy) in enumerate(spine):
        p = Point(sx, sy)
        inside = False
        try:
            inside = (trim_region is not None
                      and trim_region.covers(p))
        except _GEOM_EXC:
            inside = False
        in_core.append(inside)
        if not inside or not ring2_recs:
            continue
        best = None
        for r in ring2_recs:
            d = math.hypot(r["pt"][0] - sx, r["pt"][1] - sy)
            if best is None or d < best[0]:
                best = (d, r)
        if best is not None and best[1]["floor_engaged"]:
            clamped[j] = min(clamped[j], round(best[1]["benched"], 1))

    # ── SPINE TRIM to the core (round-8, unchanged rationale): a
    # full-length spine would cross the closed loops at the gap ends.
    # Keep spine nodes inside the (smoothed) core with 2 m of loop
    # clearance; split at any remaining segment that clips a loop. ─────
    spine_chains: list[list[int]] | None = []
    keep = []
    for j, (sx, sy) in enumerate(spine):
        ok = in_core[j]
        if ok:
            p = Point(sx, sy)
            for g in emitted_loop_lines:
                try:
                    if g.distance(p) < 2.0:
                        ok = False
                        break
                except _GEOM_EXC:
                    ok = False
                    break
        keep.append(ok)
    cur: list[int] = []
    for j in range(len(spine)):
        if not keep[j]:
            if len(cur) >= 2:
                spine_chains.append(cur)
            cur = []
            continue
        if cur:
            seg = LineString([spine[cur[-1]], spine[j]])
            clipped = False
            try:
                for g in emitted_loop_lines:
                    if seg.distance(g) < _RING_MIN_CLEARANCE_M / 2.0:
                        clipped = True
                        break
            except _GEOM_EXC:
                clipped = True
            if clipped:
                if len(cur) >= 2:
                    spine_chains.append(cur)
                cur = []
        cur.append(j)
    if len(cur) >= 2:
        spine_chains.append(cur)
    stats["spine_nodes_kept"] = sum(1 for x in keep if x)
    stats["spine_nodes_total"] = len(spine)
    return chains, clamped, stats, spine_chains


# ══════════════════════════════════════════════════════════════════════
# OPEN-FRONTAGE CORRIDOR SPINE (slice B pilot, user design ruling 3
# 2026-07-09; docs/chain_identity_one_solve_plan.md §Slice B)
#
# The OPEN generalization of the enclosed-gap spine.  An enclosed gap is
# an INTERIOR RING of the airside union; a corridor between a runway and
# a parallel taxiway is bounded by pavement on its two long sides but OPEN
# at the ends, so it is NOT an interior ring and the enclosed path never
# owns it.  With the legacy surface_clearance chain deleted the corridor
# bands inherit that open frontage and are the WRONG tool there (facing
# bands off different edge references disagree at the clip seam → tears +
# coincident-twin lenses).  Emit instead ONE face per corridor:
#   * long sides = the two facing pavement chains VERBATIM (a subsequence
#     of existing pavement ring vertices — chain identity, zero new
#     boundary geometry on pavement);
#   * ends = STRAIGHT closures across the corridor mouth between two
#     pavement ring vertices — TRUE outer edges facing free terrain (a
#     lawful vertical face lives ONLY here, per ruling 3; the corridor-law
#     march / daylight rules own everything beyond the mouth);
#   * interior = ONE drainage spine (the crown/valley), emitted via the
#     proven open-way crown mechanism (layout.gap_spines).
# ══════════════════════════════════════════════════════════════════════


def _poly_parts(geom):
    """Polygon components of a shapely geometry (drops non-areal parts)."""
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]


def _touching_shapes(poly, airside, tol):
    """The airside shapes whose exterior runs within ``tol`` of ``poly`` —
    the pavement chains a candidate corridor is bounded by.  A genuine
    corridor faces >= 2 DISTINCT shapes (a concave notch of ONE shape
    faces only itself and is not a between-pavement corridor)."""
    out = []
    for s in airside:
        try:
            if s.polygon.exterior.distance(poly) <= tol:
                out.append(s)
        except _GEOM_EXC:
            continue
    return out


def _detect_open_corridors(union, close_r, subtract):
    """Morphological CLOSING of the airside union bridges every open
    channel up to ``2 * close_r`` wide; the difference against the union
    is the newly-covered ground — enclosed gaps (interior rings) AND open
    corridors.  The closing-difference keeps the union's exact boundary on
    every pavement-facing side VERBATIM (GEOS does not perturb ``union``'s
    coordinates on a shared boundary — only the buffered end caps are new
    geometry).  ``subtract`` (enclosed-gap union + a standoff-buffered union
    of every foreign shape) is then removed so a single coarse closing blob
    SPLITS into the individual corridor slabs between pavements instead of
    being wholly blocked by one obstacle inside it; the subtraction only
    touches the FOREIGN-facing side of a corridor (foreign shapes sit in the
    corridor interior / far edge, never on the pavement-facing boundary),
    so the pavement chains stay verbatim.  Returns the polygon parts."""
    try:
        closed = union.buffer(close_r, quad_segs=2,
                              join_style=1).buffer(-close_r, quad_segs=2,
                                                   join_style=1)
        bridged = closed.difference(union)
        if subtract is not None and not subtract.is_empty:
            bridged = bridged.difference(subtract)
    except _GEOM_EXC:
        return []
    return _poly_parts(bridged)


def _corridor_verbatim_face(corridor_poly, airside, registry, air_ext,
                            ring_verts):
    """Rebuild a corridor region as a face whose PAVEMENT-facing boundary
    is a verbatim pavement SUBSEQUENCE and whose ENDS are straight closures
    across the mouth between two pavement ring vertices.  Walk the exterior
    ring and classify each vertex:

      * VERBATIM pavement ring vertex (in ``registry``) → keep, verbatim
        value;
      * a TRANSITION point ON a pavement edge (<= 0.15 m, mid-edge — where
        the morphological-closing end cap crossed the pavement line): the
        pavement-node rule (Noah, 2026-07-09 — grading shapes never mint a
        node on a pavement edge) says extend to the BRACKETING ring vertex
        — snap to the nearest pavement ring vertex within
        ``OPEN_FRONTAGE_CLOSE_M`` (the buffer radius bounds how far the
        cut sits from the true pavement end).  The corridor side then runs
        to a real vertex and the mouth closure spans two real vertices
        (zero new pavement-edge nodes).  A colinear on-edge FOOT is the
        fallback if no ring vertex is in range;
      * a FAR non-verbatim vertex (a buffered end-cap point facing free
        terrain — a TRUE outer edge) → DROP it; the segment between the
        flanking kept vertices is the straight mouth closure.

    Returns ``(face_poly, ring_coords, ring_alts)`` or None."""
    ring = _open_coords(corridor_poly)
    if len(ring) < 3:
        return None
    new_ring: list[tuple[float, float]] = []
    alts: list[float] = []
    for vx, vy in ring:
        k = _key(vx, vy)
        if k in registry:
            new_ring.append((vx, vy))
            alts.append(registry[k])        # boundary vertex, verbatim
            continue
        pt = Point(vx, vy)
        d_pav = None
        for ext in air_ext:
            try:
                d = ext.distance(pt)
            except _GEOM_EXC:
                continue
            if d_pav is None or d < d_pav:
                d_pav = d
        if d_pav is not None and d_pav <= 0.15:
            # ON a pavement edge → a transition point.  Extend to the
            # nearest pavement RING VERTEX (pavement-node rule).
            best_vd, best_v = None, None
            for rx, ry in ring_verts:
                d = math.hypot(vx - rx, vy - ry)
                if d <= OPEN_FRONTAGE_CLOSE_M and (
                        best_vd is None or d < best_vd):
                    best_vd, best_v = d, (rx, ry)
            if best_v is not None:
                rk = _key(best_v[0], best_v[1])
                if rk in registry:
                    new_ring.append((best_v[0], best_v[1]))
                    alts.append(registry[rk])
                    continue
            # Fallback: keep the colinear on-edge foot (an exact T-vertex,
            # the survivable class — never a near-parallel lens).
            e = _nearest_pav_alt(airside, vx, vy, max_distance_m=5.0)
            if e is not None:
                new_ring.append((vx, vy))
                alts.append(float(e))
                continue
        # FAR non-verbatim: an end-closure / true-outer-edge vertex — drop
        # it (the flanking kept vertices close the mouth with a straight
        # segment).
        continue
    # De-duplicate consecutive coincident kept vertices (the extension can
    # pull two adjacent transition points onto the same ring vertex).
    dr: list[tuple[float, float]] = []
    da: list[float] = []
    for (x, y), a in zip(new_ring, alts):
        if not dr or math.hypot(x - dr[-1][0], y - dr[-1][1]) > 1e-6:
            dr.append((x, y))
            da.append(a)
    if len(dr) < 3:
        return None
    try:
        face_poly = Polygon(dr)
    except _GEOM_EXC:
        return None
    if face_poly.is_empty or not face_poly.is_valid:
        return None
    return face_poly, dr, da


def _emit_open_corridor(layout, airside, face_poly, ring, alts,
                        step) -> int:
    """Grade ONE clean corridor face (verbatim ring + straight closures):
    build the drainage spine, solve its values, append the face + spine.
    Returns 1 on emit, 0 on a lawful skip (logged)."""
    try:
        axes = _mrr_axes(min_rotated_rect(face_poly))
    except _GEOM_EXC:
        return 0
    if axes is None or axes[1] is None:
        return 0
    short_side, long_dir, long_len = axes
    _c = face_poly.centroid
    UI.vprint(1, f"  [open-frontage] corridor face area="
                 f"{face_poly.area:.0f} m2 short={short_side:.0f} "
                 f"centroid=({_c.x:.0f},{_c.y:.0f})")
    if short_side > GAP_FILL_MAX_WIDTH_M:
        UI.vprint(1, f"  [open-frontage] skipped corridor (width "
                     f"{short_side:.0f} > {GAP_FILL_MAX_WIDTH_M:.0f}) "
                     f"area={face_poly.area:.0f} m2")
        return 0
    spine = _build_spine(face_poly, long_dir, long_len, step)
    if spine is None:
        UI.vprint(1, "  [open-frontage] no spine for corridor "
                     f"(area={face_poly.area:.0f} m2) — skipped.")
        return 0
    intervals: list[tuple] = []
    targets: list[float] = []
    for px, py in spine:
        lo, hi, edge_alts = _spine_interval(layout, airside, px, py)
        target = _drain_target(lo, hi, edge_alts)
        if target is None:
            UI.vprint(1, "  [open-frontage] no pavement value at spine — "
                         "skipped.")
            return 0
        intervals.append((lo, hi))
        targets.append(target)
    values = _smooth_spine(targets, intervals, _SMOOTH_SWEEPS)
    values = [round(v, 1) for v in values]
    layout.shapes.append(BuiltShape(
        polygon=face_poly, role=ROLE_GRADED_STRIP, ref=_OPEN_FRONTAGE_REF,
        node_altitudes=list(alts) + [alts[0]]))
    if getattr(layout, "gap_spines", None) is None:
        layout.gap_spines = []
    pts_ll = [layout.m_to_ll(px, py) for px, py in spine]
    layout.gap_spines.append((pts_ll, list(values)))
    return 1


def _emit_open_frontage(layout, airside, comps, union, registry,
                        chain_keys, other_polys, parents, step) -> int:
    """Detect + grade every OPEN corridor between facing pavement chains
    (behind ``O4_OPEN_FRONTAGE_SPINE``, checked by the caller).  Every
    candidate region is logged with an emit / skip reason — no silent
    skips.  Returns the corridor-face count."""
    # Enclosed gaps (interior rings) are owned by the enclosed-gap path;
    # every foreign shape (groundside / service / retaining wall / building)
    # carries a standoff (the groundside 1 m no-weld ruling).  Both are
    # SUBTRACTED from the closing so one coarse blob splits into individual
    # corridor slabs instead of being blocked whole by a single obstacle
    # inside it.  The subtraction only touches a corridor's FOREIGN-facing
    # side (foreign shapes sit in the interior / far edge, never on the
    # pavement-facing boundary), so the pavement chains stay verbatim.
    enclosed = []
    for comp in comps:
        for interior in comp.interiors:
            try:
                enclosed.append(Polygon(interior.coords))
            except _GEOM_EXC:
                continue
    try:
        enclosed_union = unary_union(enclosed) if enclosed else None
    except _GEOM_EXC:
        enclosed_union = None
    subtract_geoms = []
    if enclosed_union is not None and not enclosed_union.is_empty:
        subtract_geoms.append(enclosed_union)
    if other_polys:
        try:
            foreign_block = unary_union(
                [op for _oid, op in other_polys]).buffer(
                    _OPEN_FRONTAGE_FOREIGN_STANDOFF_M)
            if not foreign_block.is_empty:
                subtract_geoms.append(foreign_block)
        except _GEOM_EXC:
            pass
    try:
        subtract = unary_union(subtract_geoms) if subtract_geoms else None
    except _GEOM_EXC:
        subtract = None
    corridors = _detect_open_corridors(
        union, OPEN_FRONTAGE_CLOSE_M, subtract)
    if not corridors:
        return 0
    region = layout.airport_boundary
    air_ext = []
    ring_verts: list[tuple[float, float]] = []
    for _s in airside:
        try:
            _ext = _s.polygon.exterior
        except _GEOM_EXC:
            continue
        air_ext.append(_ext)
        ring_verts.extend((float(x), float(y)) for x, y in _ext.coords[:-1])
    emitted = 0
    for corr in corridors:
        if corr.is_empty or corr.area < GAP_FILL_MIN_AREA_M2:
            continue
        _c = corr.centroid
        # Enclosed-gap overlap → owned by the interior-ring path.
        if enclosed_union is not None:
            try:
                if corr.intersection(enclosed_union).area > 0.5 * corr.area:
                    UI.vprint(1, f"  [open-frontage] skipped region "
                                 f"(enclosed gap — interior-ring path owns "
                                 f"it) area={corr.area:.0f} m2 "
                                 f"centroid=({_c.x:.0f},{_c.y:.0f})")
                    continue
            except _GEOM_EXC:
                pass
        # Outside the airport region → not our ground.
        if region is not None:
            try:
                if not region.contains(corr.representative_point()):
                    UI.vprint(1, f"  [open-frontage] skipped region "
                                 f"(outside airport boundary) area="
                                 f"{corr.area:.0f} m2 "
                                 f"centroid=({_c.x:.0f},{_c.y:.0f})")
                    continue
            except _GEOM_EXC:
                pass
        # A genuine corridor faces >= 2 DISTINCT pavement shapes.
        touching = _touching_shapes(corr, airside, tol=0.5)
        if len(touching) < 2:
            UI.vprint(1, f"  [open-frontage] skipped region (faces "
                         f"{len(touching)} pavement shape(s), need >= 2 — "
                         f"concave notch, not a corridor) area="
                         f"{corr.area:.0f} m2 "
                         f"centroid=({_c.x:.0f},{_c.y:.0f})")
            continue
        # A foreign shape inside (groundside / service / retaining wall)
        # means the corridor-band / daylight law owns it — skip.
        overlapped = False
        for _oid, op in other_polys:
            try:
                if corr.intersection(op).area > 1.0:
                    overlapped = True
                    break
            except _GEOM_EXC:
                continue
        if overlapped:
            UI.vprint(1, f"  [open-frontage] skipped corridor (foreign "
                         f"shape inside) area={corr.area:.0f} m2 "
                         f"centroid=({_c.x:.0f},{_c.y:.0f})")
            continue
        # Parents (building pads / runway-end skirts) inside → reuse the
        # enclosed-gap parent machinery (residual faces + annular spine).
        parents_in = []
        for p in parents:
            try:
                if corr.intersection(p.polygon).area > 1.0:
                    parents_in.append(p)
            except _GEOM_EXC:
                continue
        if parents_in:
            faces = _parent_residual_faces(corr, parents, chain_keys)
            n = 0
            for face_poly in faces:
                n += _grade_face(layout, airside, face_poly, step, registry)
            emitted += n
            continue
        # Clean corridor: verbatim pavement long-sides + straight-closure
        # ends, then the drainage spine.
        built = _corridor_verbatim_face(
            corr, airside, registry, air_ext, ring_verts)
        if built is None:
            UI.vprint(1, f"  [open-frontage] skipped corridor "
                         f"(non-verbatim / degenerate face) area="
                         f"{corr.area:.0f} m2 "
                         f"centroid=({_c.x:.0f},{_c.y:.0f})")
            continue
        face_poly, face_ring, face_alts = built
        emitted += _emit_open_corridor(
            layout, airside, face_poly, face_ring, face_alts, step)
    if emitted:
        UI.vprint(1, f"  [open-frontage] emitted {emitted} open-corridor "
                     f"drainage-spine face(s).")
    return emitted


# ══════════════════════════════════════════════════════════════════════
# SLICE B STAGE B2 — PRE-SOLVE spine construction (one-solve absorption)
# ══════════════════════════════════════════════════════════════════════


def _airside_shapes(layout):
    """The airside pavement shapes the gap detection runs on — ONE
    definition shared by the pre-solve construction and the post-solve
    emitter so both see the identical union (parity is load-bearing:
    the emitter matches its spines against the pre-solve store by
    coordinate)."""
    return [s for s in layout.shapes
            if s.role in _AIRSIDE_PAVEMENT_ROLES
            and s.polygon is not None and not s.polygon.is_empty
            and s.polygon.geom_type == "Polygon"]


def _gap_parents(layout):
    """The gap-parent shapes (building pads + runway-end skirts) per
    their sub-gates — shared by construction and emission (same parity
    argument as ``_airside_shapes``).  Gate-ON both exist PRE-solve:
    pads are phase-1 shapes; skirts are pre-solve under the B1 sub-gate
    (which the B2 gate REQUIRES — pipeline hard-error)."""
    _pad_parents = os.environ.get("O4_GAP_FILL_PAD_PARENTS", "1") == "1"
    _skirt_parents = os.environ.get(
        "O4_GAP_FILL_SKIRT_PARENTS", "1") == "1"
    pads = [s for s in layout.shapes
            if s.role == ROLE_BUILDING and s.polygon is not None
            and not s.polygon.is_empty
            and s.polygon.geom_type in ("Polygon", "MultiPolygon")] \
        if _pad_parents else []
    skirts = [s for s in layout.shapes
              if getattr(s, "ref", None) == "runway_end_skirt"
              and s.polygon is not None and not s.polygon.is_empty
              and s.polygon.geom_type == "Polygon"] \
        if _skirt_parents else []
    return pads, skirts


def _freeze_spine_parent_specs(layout, airside, px, py):
    """FROZEN-NEAREST station mapping (design open question 1, START
    FROZEN-NEAREST — ratified 2026-07-10): for spine point ``(px, py)``,
    the two nearest DISTINCT bounding pavement shapes (the same
    parent selection as the analytic ``_spine_interval``), each frozen
    to (a) its nearest ring VERTEX — the pavement chain station the
    envelope interval edge couples to — and (b) the envelope offsets
    ``adjacent_ground_envelope(role, code_number, code_letter, d)`` at
    the CONSTRUCTION-TIME lateral distance ``d`` to that parent's edge.
    The station identity and ``d`` never re-derive as the solve moves
    elevations; the elevation coupling itself stays live through the
    interval edge.  A parent whose envelope is fully open
    ``(None, None)`` contributes no edge (mirrors the analytic path,
    where such a parent contributes only its edge altitude).

    Returns ``[(station_xy, floor_offset, ceiling_offset), ...]``
    (0-2 entries)."""
    p = Point(px, py)
    cands = []
    for s in airside:
        try:
            d = s.polygon.exterior.distance(p)
        except _GEOM_EXC:
            continue
        cands.append((d, s))
    cands.sort(key=lambda t: t[0])
    specs = []
    for d, s in cands[:2]:
        role, cn, cl = _parent_family_code(layout, s)
        try:
            floor_off, ceil_off = adjacent_ground_envelope(
                role, cn, cl, max(0.0, d))
        except _GEOM_EXC:
            continue
        if floor_off is None and ceil_off is None:
            continue
        # Frozen station = the parent's nearest ring vertex (every
        # pavement ring vertex is a solver node, so the station is
        # mappable to a node index at constraint-build time).
        try:
            ring = _open_coords(s.polygon)
        except _GEOM_EXC:
            continue
        if not ring:
            continue
        sx, sy = min(ring, key=lambda v: (v[0] - px) ** 2
                                         + (v[1] - py) ** 2)
        specs.append(((float(sx), float(sy)),
                      None if floor_off is None else float(floor_off),
                      None if ceil_off is None else float(ceil_off)))
    return specs


def construct_gap_fill_presolve(layout) -> int:
    """Stage B2 PRE-SOLVE construction: detect the enclosed gaps and
    build their drainage-spine GEOMETRY before ``per_surface_solve`` so
    the spine vertices join the solver node list as FREE variables (the
    B0 admission hook reads ``layout.gap_fill_presolve``).  Values are
    NOT computed here — they come from the solve's writeback.

    The detection mirrors ``emit_gap_fill_spines`` geometry-for-geometry
    EXCEPT the blockers whose subjects do not exist yet pre-solve
    (legacy surface_clearance strips and the other post-solve features):
    those are evaluated at EMISSION, where they exist, exactly as today.
    Construction is therefore a SUPERSET of emission — a spine built for
    a gap that emission later blocks simply never emits (its solver
    variables settle inside their lawful envelope and are dropped).

    Stores ``layout.gap_fill_presolve = [{"spine": [(x, y), ...],
    "specs": [per-node ``_freeze_spine_parent_specs`` list],
    "values": None}, ...]`` and returns the entry count."""
    if not GAP_FILL_SPINE_ENABLED:
        return 0
    airside = _airside_shapes(layout)
    if len(airside) < 2:
        return 0
    try:
        union = unary_union([s.polygon for s in airside])
    except _GEOM_EXC:
        return 0
    if union.is_empty:
        return 0
    comps = ([union] if union.geom_type == "Polygon"
             else [g for g in getattr(union, "geoms", [])
                   if g.geom_type == "Polygon"])
    pads, skirts = _gap_parents(layout)
    parents = pads + skirts
    # Geometry-only chain-key set (the ``_face_is_verbatim`` gate needs
    # keys, not values): every airside + parent ring vertex.
    chain_keys: set[tuple[int, int]] = set()
    for s in airside:
        try:
            for vx, vy in s.polygon.exterior.coords:
                chain_keys.add(_key(vx, vy))
        except _GEOM_EXC:
            continue
    for p in parents:
        geoms = ([p.polygon] if p.polygon.geom_type == "Polygon"
                 else list(p.polygon.geoms))
        for g in geoms:
            try:
                for vx, vy in g.exterior.coords:
                    chain_keys.add(_key(vx, vy))
            except _GEOM_EXC:
                continue
    airside_ids = {id(s) for s in airside}
    parent_ids = {id(s) for s in parents}
    # Foreign blockers PRESENT pre-solve (bridge plates, boundary…).
    # Post-solve-only features are checked at emission instead.
    other_polys = [(id(s), s.polygon) for s in layout.shapes
                   if id(s) not in airside_ids
                   and id(s) not in parent_ids
                   and s.polygon is not None and not s.polygon.is_empty
                   and s.polygon.geom_type in ("Polygon", "MultiPolygon")]
    # CROSSING INFLUENCE ZONE (Phase 1, docs/specs/crossing-terrain-
    # ownership.md): the published zone blocks a gap exactly like a
    # foreign shape — a gap-fill face must never bury a crossing or its
    # depressed public road (round-8 finding: gap-fill was the fourth
    # corridor consumer, and the only one that never clipped).  Published
    # pre-solve, so this construct pass and the emitter see the identical
    # geometry (the coordinate-matching parity both rely on).
    from .crossing_terrain import crossing_influence_zone_union
    _crossing_zone = crossing_influence_zone_union(layout)
    if _crossing_zone is not None:
        other_polys.append((0, _crossing_zone))
    step = GAP_FILL_SPINE_STEP_M
    entries: list[dict] = []
    for comp in comps:
        for interior in comp.interiors:
            ring_coords = list(interior.coords)
            try:
                gap_poly = Polygon(ring_coords)
            except _GEOM_EXC:
                continue
            if gap_poly.is_empty or not gap_poly.is_valid:
                continue
            if gap_poly.area < GAP_FILL_MIN_AREA_M2:
                continue
            overlapped = False
            for _oid, op in other_polys:
                try:
                    if gap_poly.intersection(op).area > 1.0:
                        overlapped = True
                        break
                except _GEOM_EXC:
                    continue
            if overlapped:
                continue
            faces = (_parent_residual_faces(gap_poly, parents, chain_keys)
                     if parents else [gap_poly])
            for face_poly in faces:
                if face_poly.is_empty or not face_poly.is_valid:
                    continue
                if face_poly.area < GAP_FILL_MIN_AREA_M2:
                    continue
                try:
                    axes = _mrr_axes(min_rotated_rect(face_poly))
                except _GEOM_EXC:
                    continue
                if axes is None or axes[1] is None:
                    continue
                short_side, long_dir, long_len = axes
                if short_side > GAP_FILL_MAX_WIDTH_M:
                    continue
                spine = _build_spine(face_poly, long_dir, long_len, step)
                if spine is None:
                    continue
                specs = [_freeze_spine_parent_specs(layout, airside, px, py)
                         for px, py in spine]
                entries.append({"spine": [(float(px), float(py))
                                          for px, py in spine],
                                "specs": specs,
                                "values": None})
    layout.gap_fill_presolve = entries
    if entries:
        n_pts = sum(len(e["spine"]) for e in entries)
        UI.vprint(1, f"  [gap-fill] PRE-SOLVE constructed {len(entries)} "
                     f"drainage spine(s), {n_pts} solver node(s) "
                     f"(one-solve terrain absorption, stage B2).")
    return len(entries)


def _solved_spine_values(layout, spine):
    """Stage B2 gate-ON valuation: the solve-writeback values for
    ``spine``, matched against ``layout.gap_fill_presolve`` by
    coordinate (same station count, every point within 0.01 m — the
    construction is deterministic from the gap geometry, so pre-solve
    and emission-time spines coincide; the tolerance absorbs
    float-level drift from conformance-densified rings).  Returns the
    value list or None (no store / no match / unwritten entry)."""
    entries = getattr(layout, "gap_fill_presolve", None)
    if not entries:
        return None
    for entry in entries:
        vals = entry.get("values")
        if not vals or len(entry["spine"]) != len(spine):
            continue
        if any(v is None for v in vals):
            continue
        if all(math.hypot(ex - px, ey - py) <= 0.01
               for (ex, ey), (px, py) in zip(entry["spine"], spine)):
            return list(vals)
    return None


def emit_gap_fill_spines(layout, dem, tile_lat, tile_lon,
                         source_runways=None) -> int:
    """Grade every enclosed gap of the airside pavement union as one unit
    (gate ``GAP_FILL_SPINE_ENABLED``).  Mutates ``layout.shapes``; returns
    the number of ``graded_strip`` half-gap faces emitted.

    ``dem`` / ``tile_lat`` / ``tile_lon``: the spine drainage solve is
    PURE law + pavement reads (an enclosed gap is bounded by pavement on
    all sides, so the DEM never enters the interior SPINE value); the
    INTERIOR-RING violation trigger (gate ``O4_GAP_FILL_INTERIOR_RINGS``)
    does read the DEM — rings emit only where the interior genuinely
    drops below the band floor.  ``source_runways`` (the apt.dat runway
    rows) keys runway-bounded ring widths by the TRUE ICAO code
    (ratified answer 1); None falls back to the segment-chord proxy.
    """
    if GAP_FILL_INTERIOR_RINGS_ENABLED and not GAP_FILL_SPINE_ENABLED:
        # HARD ERROR, not silent no-op (fail-loudly doctrine, the B2
        # gate-dependency pattern): the rings are constructed BY the
        # gap emitter, so this configuration can produce nothing.
        # RuntimeError deliberately — the pipeline's _GEOM_EXC wrapper
        # (ValueError + shapely) must NOT swallow a configuration error.
        raise RuntimeError(
            "O4_GAP_FILL_INTERIOR_RINGS requires O4_GAP_FILL_SPINE=1: "
            "interior rings are constructed by the gap-fill emitter "
            "(ratified design 2026-07-11); enable both or neither.")
    if not GAP_FILL_SPINE_ENABLED:
        return 0
    airside = _airside_shapes(layout)
    if len(airside) < 2:
        return 0
    try:
        union = unary_union([s.polygon for s in airside])
    except _GEOM_EXC:
        return 0
    if union.is_empty:
        return 0
    comps = ([union] if union.geom_type == "Polygon"
             else [g for g in getattr(union, "geoms", [])
                   if g.geom_type == "Polygon"])

    # WELD-VALUE registry (mm key): every airside ring vertex → its solved
    # value, so a gap-ring vertex (which IS a pavement ring vertex) emits
    # the pavement value VERBATIM.  First writer wins.
    registry: dict[tuple[int, int], float] = {}
    for s in airside:
        na = s.node_altitudes
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        for i, (vx, vy) in enumerate(coords):
            if na and i < len(na) and na[i] is not None:
                value = float(na[i])
            elif not na and s.altitude is not None:
                value = float(s.altitude)
            else:
                continue
            k = _key(vx, vy)
            if k not in registry:
                registry[k] = value

    # GAP PARENTS (user design 2026-07-09 queue item 5 + supervisor
    # follow-up).  Two parent families, each behind its own sub-gate
    # (separate gates keep each law independently A/B-able against the
    # shipped gap-fill — a pad regression and a skirt regression bisect
    # apart):
    #   * BUILDING PADS (O4_GAP_FILL_PAD_PARENTS, default ON): FLAT with
    #     an authoritative value (user ruling: buildings are flat) —
    #     apron-family envelope members.
    #   * RUNWAY-END SKIRTS (O4_GAP_FILL_SKIRT_PARENTS, default ON):
    #     NON-flat — their ring vertices carry the governed inverse-RESA
    #     runway-end profile (per-vertex node_altitudes; skirt anchored
    #     at the runway end, dev 9345739).  The skirt shape itself keeps
    #     emitting exactly as today; the gap fills AROUND it.
    # A parent BOUNDS a gap the way pavement does, so it is NOT a
    # blocker.  A hole with a parent inside is graded on the RESIDUAL
    # ground only; the parent's value WINS at parent-ring nodes —
    # per-vertex for skirts, flat for pads — registered here AFTER
    # pavement (first-writer-wins keeps pavement winning at any shared
    # node: the pavement-value-wins ruling) — and the parent ring is a
    # VERBATIM boundary chain (zero new boundary vertices).
    pads, skirts = _gap_parents(layout)
    parents = pads + skirts
    # Geometry-only key set for the verbatim gate: every pavement +
    # parent ring vertex.  A residual boundary vertex outside this set
    # is a difference-minted crossing point (not chain-safe).
    chain_keys: set[tuple[int, int]] = set(registry)
    for p in parents:
        flat_value = _parent_flat_value(p)
        geoms = ([p.polygon] if p.polygon.geom_type == "Polygon"
                 else list(p.polygon.geoms))
        for g in geoms:
            try:
                coords = list(g.exterior.coords)
            except _GEOM_EXC:
                continue
            # Per-vertex values only when the altitude list aligns with
            # THIS ring (single-Polygon shapes — skirts always; pads in
            # synthetic fixtures); a MultiPolygon pad falls back to its
            # flat value.
            na = (p.node_altitudes
                  if (g is p.polygon and p.node_altitudes) else None)
            for i, (vx, vy) in enumerate(coords):
                k = _key(vx, vy)
                chain_keys.add(k)
                if na and i < len(na) and na[i] is not None:
                    registry.setdefault(k, float(na[i]))  # pavement wins
                elif flat_value is not None:
                    registry.setdefault(k, flat_value)    # pavement wins

    airside_ids = {id(s) for s in airside}
    parent_ids = {id(s) for s in parents}
    # LEGACY SUPERSESSION (user 2026-07-09: 13 of 27 CYXY holes were
    # blocked ONLY by legacy surface_clearance strips — the chain the
    # gap-fill replaces).  A legacy strip lying WHOLLY inside a gap is
    # removable (a whole-piece drop is chain-safe; the gap's drainage
    # surface supersedes the strip's cut).  Skirts and every other
    # feature stay blockers.
    # Gate: default ON since the OPEN-WAY spine redesign (2026-07-09
    # round 2) — the boundary-landing sliver class died by construction
    # and the CYXY supersession audit reads zero new lenses (the
    # near-parallel count equals the pre-gap baseline's own).
    _supersede = os.environ.get(
        "O4_GAP_FILL_SUPERSEDE", "1") == "1"
    legacy_strips = [s for s in layout.shapes
                     if getattr(s, "ref", None) == "surface_clearance"
                     and s.polygon is not None
                     and not s.polygon.is_empty] if _supersede else []
    legacy_ids = {id(s) for s in legacy_strips}
    # Gap parents (building pads / runway-end skirts, per their gates)
    # are EXCLUDED from the blocker set — they bound the gap (handled in
    # _parent_residual_faces), not block it.  Every other foreign shape
    # (groundside, service, retaining wall …) still blocks.
    other_polys = [(id(s), s.polygon) for s in layout.shapes
                   if id(s) not in airside_ids
                   and id(s) not in legacy_ids
                   and id(s) not in parent_ids
                   and s.polygon is not None and not s.polygon.is_empty
                   and s.polygon.geom_type in ("Polygon", "MultiPolygon")]
    # CROSSING INFLUENCE ZONE (Phase 1, docs/specs/crossing-terrain-
    # ownership.md): the published zone blocks a gap exactly like a
    # foreign shape, and the open-frontage path subtracts it so a
    # corridor SPLITS around the depressed road instead of burying it
    # (round-8 finding: a gap-fill strip buried the tunnel=yes road at
    # 36.1106,-86.6834 — gap-fill was the only corridor consumer that
    # never clipped).  Same published geometry the pre-solve construct
    # pass consulted, so the coordinate-matching parity holds.
    from .crossing_terrain import crossing_influence_zone_union
    _crossing_zone = crossing_influence_zone_union(layout)
    if _crossing_zone is not None:
        other_polys.append((0, _crossing_zone))

    step = GAP_FILL_SPINE_STEP_M
    # Runway axes for the interior-ring width keying (gate-ON only —
    # gate-OFF nothing reads them, keeping the plain path untouched).
    _ring_axes = (_ring_runway_axes(layout, source_runways)
                  if GAP_FILL_INTERIOR_RINGS_ENABLED else None)
    emitted = 0
    for comp in comps:
        for interior in comp.interiors:
            # Verbatim ring coords — no cleaning op touches the boundary.
            ring_coords = list(interior.coords)
            try:
                gap_poly = Polygon(ring_coords)
            except _GEOM_EXC:
                continue
            if gap_poly.is_empty or not gap_poly.is_valid:
                continue
            if gap_poly.area < GAP_FILL_MIN_AREA_M2:
                continue
            # A foreign shape inside the gap (groundside / service /
            # retaining wall …) means the corridor bands own it — skip.
            # Gap parents (building pads / runway-end skirts) are NOT in
            # ``other_polys`` when their law is on: they bound the gap
            # (handled below).  Legacy surface_clearance strips are NOT
            # blockers either: wholly-inside ones are superseded
            # (removed) when the gap emits; a PARTIALLY-inside strip
            # blocks (cutting it would mutate a welded ring — only
            # whole-piece drops are chain-safe).
            overlapped = False
            for _oid, op in other_polys:
                try:
                    if gap_poly.intersection(op).area > 1.0:
                        overlapped = True
                        break
                except _GEOM_EXC:
                    continue
            if overlapped:
                _c = gap_poly.centroid
                UI.vprint(1, f"  [gap-fill] skipped gap (foreign shape "
                             f"inside) area={gap_poly.area:.0f} m2 "
                             f"centroid=({_c.x:.0f},{_c.y:.0f})")
                continue
            superseded = []
            for s in legacy_strips:
                try:
                    inside = gap_poly.intersection(s.polygon).area
                except _GEOM_EXC:
                    inside = 0.0
                if inside <= 1.0:
                    continue
                if inside < 0.99 * s.polygon.area:
                    overlapped = True     # partial straddle — block
                    break
                superseded.append(s)
            if overlapped:
                _c = gap_poly.centroid
                UI.vprint(1, f"  [gap-fill] skipped gap (partial-"
                             f"straddle legacy strip) area="
                             f"{gap_poly.area:.0f} m2 "
                             f"centroid=({_c.x:.0f},{_c.y:.0f})")
                continue
            # FACES to grade: the whole gap, or — when gap parent(s)
            # (building pads / runway-end skirts) bound it — the
            # RESIDUAL ground around the parent(s), each a chain-safe
            # part (parent-fill → lawful vanish; non-verbatim →
            # blocked; both logged in the helper).
            faces = (_parent_residual_faces(gap_poly, parents, chain_keys)
                     if parents else [gap_poly])
            n_faces = 0
            for face_poly in faces:
                n_faces += _grade_face(
                    layout, airside, face_poly, step, registry,
                    dem=dem, tile_lat=tile_lat, tile_lon=tile_lon,
                    rw_axes=_ring_axes)
            if n_faces and superseded:
                _sup_ids = {id(s) for s in superseded}
                layout.shapes[:] = [s for s in layout.shapes
                                    if id(s) not in _sup_ids]
                legacy_strips[:] = [s for s in legacy_strips
                                    if id(s) not in _sup_ids]
                UI.vprint(1,
                    f"  [gap-fill] superseded "
                    f"{len(_sup_ids)} legacy surface_clearance "
                    f"strip(s) inside an emitted gap.")
            emitted += n_faces

    # ── OPEN-FRONTAGE CORRIDOR SPINE (slice B pilot, ruling 3) ──────────
    # The enclosed-gap loop above owns interior rings.  This pilot, behind
    # its OWN sub-gate (default OFF — Noah has not reviewed it in-sim),
    # additionally owns OPEN corridors between facing pavements (a runway ↔
    # parallel-taxiway strip and similar): ground bounded by two pavement
    # chains on its long sides but open at the ends, which the legacy
    # surface_clearance chain used to grade and the corridor bands do
    # badly once it is deleted.  A no-op with the gate off.
    if os.environ.get("O4_OPEN_FRONTAGE_SPINE", "0") == "1":
        emitted += _emit_open_frontage(
            layout, airside, comps, union, registry, chain_keys,
            other_polys, parents, step)
    # Stage B2 movement report: solved-vs-analytic spine value deltas
    # accumulated per emitted gap (gate-ON only — the store is empty or
    # absent gate-OFF).
    _deltas = getattr(layout, "_gap_spine_value_deltas", None)
    if _deltas:
        _ds = sorted(_deltas)
        UI.vprint(1, f"  [gap-fill] stage B2 solved-vs-analytic spine "
                     f"values: n={len(_ds)} worst={_ds[-1]:.2f} m "
                     f"median={_ds[len(_ds) // 2]:.2f} m.")
    _grings_total = getattr(layout, "gap_interior_rings", None)
    if _grings_total:
        UI.vprint(1, f"  [gap-fill] interior rings TOTAL: "
                     f"{len(_grings_total)} chain(s), "
                     f"{sum(len(_gp) for _gp, _ga in _grings_total)} "
                     f"node(s) (gate O4_GAP_FILL_INTERIOR_RINGS).")
    return emitted


def emit_gap_interior_floor(layout, dem, tile_lat, tile_lon) -> int:
    """Clamp enclosed-pocket interiors to a drainage-depth floor (owner
    ruling 2026-07-19; gate ``GAP_FILL_INTERIOR_FLOOR_DEPTH_M`` > 0).

    Runs AFTER ``emit_gap_fill_spines``: a treated gap is covered by its
    emitted ``graded_strip`` face and skips this pass by coverage; the
    pass targets the pockets the emitter lawfully SKIPPED (wider than
    ``GAP_FILL_MAX_WIDTH_M``, foreign shape inside, parent straddle),
    whose interiors ride raw DEM.  For each such pocket:

    * lip = median solved pavement value at the pocket's own ring
      vertices (the enclosing pavement edge);
    * floor = lip − ``GAP_FILL_INTERIOR_FLOOR_DEPTH_M``;
    * grid-sample the DEM inside the pocket; union the violating cells
      (DEM < floor) into pit regions, clear of every existing shape;
    * emit each pit region as a FLAT ``graded_strip`` patch at the floor
      value (ref ``gap_pit_floor``).

    No-op economy: a pocket whose terrain never drops below the floor
    emits nothing — large infields keep following terrain, down to
    drainage depth.  Mutates ``layout.shapes``; returns the number of
    pit patches emitted.
    """
    depth = GAP_FILL_INTERIOR_FLOOR_DEPTH_M
    if depth <= 0.0 or dem is None:
        return 0
    airside = _airside_shapes(layout)
    if len(airside) < 2:
        return 0
    try:
        union = unary_union([s.polygon for s in airside])
    except _GEOM_EXC:
        return 0
    if union.is_empty:
        return 0
    comps = ([union] if union.geom_type == "Polygon"
             else [g for g in getattr(union, "geoms", [])
                   if g.geom_type == "Polygon"])

    # Solved pavement value at every airside ring vertex (mm key).
    registry: dict[tuple[int, int], float] = {}
    for s in airside:
        na = s.node_altitudes
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        for i, (vx, vy) in enumerate(coords):
            if na and i < len(na) and na[i] is not None:
                registry.setdefault(_key(vx, vy), float(na[i]))
            elif not na and s.altitude is not None:
                registry.setdefault(_key(vx, vy), float(s.altitude))

    # Every OTHER shape (emitted gap faces, bands, groundside, parents,
    # …): coverage test + keep-clear region for the pit patches.
    airside_ids = {id(s) for s in airside}
    other_polys = [s.polygon for s in layout.shapes
                   if id(s) not in airside_ids
                   and s.polygon is not None and not s.polygon.is_empty
                   and s.polygon.geom_type in ("Polygon", "MultiPolygon")]

    from .elevation import _sample_dem

    def _dem_at(px: float, py: float):
        lat, lon = layout.m_to_ll(px, py)
        return _sample_dem(dem, tile_lat, tile_lon, lat, lon)

    emitted = 0
    for comp in comps:
        for interior in comp.interiors:
            try:
                gap_poly = Polygon(list(interior.coords))
            except _GEOM_EXC:
                continue
            if (gap_poly.is_empty or not gap_poly.is_valid
                    or gap_poly.area < GAP_FILL_MIN_AREA_M2):
                continue
            # Treated gaps are covered by their emitted faces — skip any
            # pocket mostly covered by existing non-airside shapes.
            try:
                covered = sum(
                    gap_poly.intersection(op).area
                    for op in other_polys
                    if op.intersects(gap_poly))
            except _GEOM_EXC:
                covered = 0.0
            if covered >= 0.4 * gap_poly.area:
                continue
            # Pavement lip at THIS pocket's ring.
            lip_values = [registry[k] for k in
                          (_key(vx, vy) for vx, vy in interior.coords)
                          if k in registry]
            if len(lip_values) < 3:
                _c = gap_poly.centroid
                UI.vprint(1, f"  [gap-floor] pocket at ({_c.x:.0f},"
                             f"{_c.y:.0f}) has no pavement values on "
                             f"its ring — skipped.")
                continue
            lip_values.sort()
            lip = lip_values[len(lip_values) // 2]
            floor = lip - depth
            # Grid-sample the interior for violations.
            minx, miny, maxx, maxy = gap_poly.bounds
            span = max(maxx - minx, maxy - miny)
            cell = max(8.0, span / 150.0)
            violating_cells = []
            y = miny
            while y < maxy:
                x = minx
                while x < maxx:
                    cx, cy = x + 0.5 * cell, y + 0.5 * cell
                    if gap_poly.contains(Point(cx, cy)):
                        alt = _dem_at(cx, cy)
                        if alt is not None and alt < floor:
                            violating_cells.append(Polygon([
                                (x, y), (x + cell, y),
                                (x + cell, y + cell), (x, y + cell)]))
                    x += cell
                y += cell
            if not violating_cells:
                continue
            try:
                pit_region = unary_union(violating_cells).simplify(
                    1.0, preserve_topology=True)
                pit_region = pit_region.intersection(
                    gap_poly.buffer(-0.5))
                for op in other_polys:
                    if op.intersects(pit_region):
                        pit_region = pit_region.difference(
                            op.buffer(0.25))
            except _GEOM_EXC:
                continue
            parts = ([pit_region] if pit_region.geom_type == "Polygon"
                     else [g for g in getattr(pit_region, "geoms", [])
                           if g.geom_type == "Polygon"])
            n_pocket = 0
            pocket_area = 0.0
            for part in parts:
                if part.is_empty or part.area < 25.0:
                    continue
                ring = _open_coords(part)
                if len(ring) < 3:
                    continue
                alts = [round(floor, 2)] * len(ring)
                layout.shapes.append(BuiltShape(
                    polygon=part, role=ROLE_GRADED_STRIP,
                    ref=_GAP_PIT_FLOOR_REF,
                    node_altitudes=alts + [alts[0]]))
                n_pocket += 1
                pocket_area += part.area
            if n_pocket:
                emitted += n_pocket
                _c = gap_poly.centroid
                UI.vprint(1,
                    f"  [gap-floor] pocket at ({_c.x:.0f},{_c.y:.0f}) "
                    f"area={gap_poly.area:.0f} m2: {n_pocket} pit "
                    f"patch(es) totalling {pocket_area:.0f} m2 clamped "
                    f"to floor {floor:.1f} m (lip {lip:.1f} - "
                    f"{depth:.1f}).")
    return emitted


def _emit_one_gap(layout, airside, gap_poly, long_dir, long_len, step,
                  registry, dem=None, tile_lat=None, tile_lon=None,
                  rw_axes=None) -> int:
    """Build the drainage spine, solve its values, split the gap into
    half-gap faces and emit them.  Returns the face count."""
    spine = _build_spine(gap_poly, long_dir, long_len, step)
    if spine is None:
        UI.vprint(1, "  [gap-fill] no spine for enclosed gap "
                     f"(area={gap_poly.area:.0f} m2) — skipped.")
        return 0

    # STAGE B2 (one-solve absorption): gate-ON the spine values come
    # from the solve writeback (``layout.gap_fill_presolve``); the
    # analytic valuation below then serves ONLY the solved-vs-analytic
    # movement report.  Gate-OFF there is no store, ``solved`` is None
    # and the analytic path is byte-identical to before.
    solved = _solved_spine_values(layout, spine)

    # Per-vertex drainage interval + target.
    intervals: list[tuple] = []
    targets: list[float] = []
    ok = True
    for px, py in spine:
        lo, hi, edge_alts = _spine_interval(layout, airside, px, py)
        target = _drain_target(lo, hi, edge_alts)
        if target is None:
            ok = False
            break
        intervals.append((lo, hi))
        targets.append(target)
    if not ok and solved is None:
        UI.vprint(1, "  [gap-fill] no pavement value at spine — skipped.")
        return 0

    # Open-way spine (2026-07-09 round 2): the ends float >= 2 m
    # inside the gap, so they take their own corridor target like
    # every station — the surface between spine end and boundary
    # lerps in the mesh; the pavement value lives on the ring itself.
    if solved is not None:
        values = [round(v, 1) for v in solved]
        if ok:
            # Movement report (ratified 2026-07-10): how far the solve
            # writeback sits from the retired analytic target.
            analytic = _smooth_spine(targets, intervals, _SMOOTH_SWEEPS)
            store = getattr(layout, "_gap_spine_value_deltas", None)
            if store is None:
                store = layout._gap_spine_value_deltas = []
            store.extend(abs(v - a) for v, a in zip(solved, analytic))
    else:
        if getattr(layout, "gap_fill_presolve", None) is not None:
            UI.vprint(1, "  [gap-fill] WARN: stage B2 gate is ON but no "
                         "pre-solve spine matches this emitted gap — "
                         "analytic valuation fallback.")
        values = _smooth_spine(targets, intervals, _SMOOTH_SWEEPS)
        values = [round(v, 1) for v in values]

    # ── GAP INTERIOR RINGS (ratified 2026-07-11, round-8 revision:
    # complete closed loops, value-gated) — band-breakpoint breaklines
    # inside the face; the spine values may only move DOWN (ring-2
    # ceiling re-coupling).  Failure degrades loudly to the ring-less
    # face (never blocks the gap emission). ────────────────────────────
    _spine_chains = None
    if GAP_FILL_INTERIOR_RINGS_ENABLED and dem is not None:
        ring_stats = None
        try:
            (ring_chains, values, ring_stats,
             _spine_chains) = _build_gap_interior_rings(
                layout, airside, gap_poly, spine, values, dem,
                tile_lat, tile_lon, rw_axes, step)
        except _GEOM_EXC as _ring_exc:
            ring_chains = []
            _spine_chains = None
            UI.vprint(1, f"  [gap-fill] interior-ring construction "
                         f"FAILED (face kept ring-less): {_ring_exc!r}")
        _c = gap_poly.centroid
        if ring_chains:
            if getattr(layout, "gap_interior_rings", None) is None:
                layout.gap_interior_rings = []
            for _rc_pts, _rc_alts in ring_chains:
                layout.gap_interior_rings.append(
                    ([layout.m_to_ll(_rx, _ry) for _rx, _ry in _rc_pts],
                     list(_rc_alts)))
            UI.vprint(1, f"  [gap-fill] interior rings: "
                         f"{ring_stats['chains']} loop(s)/chain(s), "
                         f"{ring_stats['nodes']} node(s), "
                         f"{ring_stats['engaged_stations']} engaged / "
                         f"{ring_stats['noop_stations']} terrain-riding "
                         f"of {ring_stats['stations']} station(s) "
                         f"(centroid=({_c.x:.0f},{_c.y:.0f})).")
        elif ring_stats is not None and ring_stats.get("skipped"):
            UI.vprint(1, f"  [gap-fill] interior rings SKIPPED (economy "
                         f"gate: every station of both rings is a value "
                         f"no-op — terrain lawful throughout; "
                         f"{ring_stats['stations']} station(s), "
                         f"centroid=({_c.x:.0f},{_c.y:.0f})).")

    # OPEN-WAY EMISSION (user design 2026-07-09, round 2): ONE face —
    # the gap polygon itself, ring verbatim — plus the spine as an
    # interior open constrained way (layout.gap_spines → the
    # crown-spine mechanism, o4_feature=gap_drainage_spine).  No
    # split, no landing geometry, no keyhole rails.
    _air_ext = []
    for _s in airside:
        try:
            _air_ext.append(_s.polygon.exterior)
        except _GEOM_EXC:
            continue
    ring = _open_coords(gap_poly)
    if len(ring) < 3:
        return 0
    new_ring: list[tuple[float, float]] = []
    alts = []
    for vx, vy in ring:
        k = _key(vx, vy)
        if k in registry:
            new_ring.append((vx, vy))
            alts.append(registry[k])        # boundary vertex, verbatim
            continue
        # UNION-DIVERGENCE point — where two pavement rings disagree
        # by millimetres the union outline follows neither, and an
        # un-snapped gap vertex mints a near-parallel lens (measured
        # 96 mm at CYXY hole 22).  Snap onto the nearest airside
        # exterior within 0.15 m and adopt the pavement edge value.
        pt = Point(vx, vy)
        best_d, best_pt = None, None
        for ext in _air_ext:
            try:
                d = ext.distance(pt)
            except _GEOM_EXC:
                continue
            if d <= 0.15 and (best_d is None or d < best_d):
                best_d = d
                best_pt = ext.interpolate(ext.project(pt))
        if best_pt is not None:
            e = _nearest_pav_alt(airside, best_pt.x, best_pt.y,
                                 max_distance_m=5.0)
            if e is not None:
                new_ring.append((best_pt.x, best_pt.y))
                alts.append(float(e))
                continue
        e = _nearest_pav_alt(airside, vx, vy, max_distance_m=5.0)
        new_ring.append((vx, vy))
        alts.append(float(e) if e is not None else values[0])
    # PARENT-HOLE PRESERVATION (test_no_self_overlap fix).  When a gap
    # parent (runway-end skirt / building pad) sits WHOLLY inside the
    # gap, ``_parent_residual_faces`` already carved it out — the
    # ``gap_poly`` handed here is an ANNULUS whose interior ring is the
    # parent footprint (residual = gap − parent_union).  ``_open_coords``
    # keeps only the EXTERIOR ring, though, so without re-adding the
    # holes the emitted face refills the parent footprint and BURIES it
    # (CYXY: a graded_strip covered runway_end_skirt #242 by 1,925 m²).
    # Re-attach the residual's interior rings so the emitted polygon is
    # clipped against its bounding parents exactly as the residual was.
    # The exterior ``new_ring`` (and its ``alts``) is untouched, so the
    # per-vertex ``node_altitudes`` contract (exterior-ring aligned, the
    # only ring ``to_osm`` reads) still holds.  A parent that only
    # partially straddles the gap leaves no interior ring here — its bite
    # already lives in the exterior — so nothing changes for that case.
    parent_holes = [list(r.coords) for r in gap_poly.interiors]
    try:
        face_poly = Polygon(new_ring, parent_holes)
        if not face_poly.is_valid or face_poly.is_empty:
            face_poly = gap_poly
            new_ring = list(_open_coords(gap_poly))
    except _GEOM_EXC:
        face_poly = gap_poly
    if face_poly.area < GAP_FILL_MIN_AREA_M2:
        # The residual passed the min-area gate, but carving the parent
        # hole(s) can drop a thin annulus below it — a sliver graded_strip
        # is not worth an emitted shape.  Skip (no spine ways emit either).
        _c = gap_poly.centroid
        UI.vprint(1, f"  [gap-fill] parent-clipped face below min area "
                     f"({face_poly.area:.0f} < {GAP_FILL_MIN_AREA_M2:.0f} "
                     f"m2) centroid=({_c.x:.0f},{_c.y:.0f}) — skipped.")
        return 0
    layout.shapes.append(BuiltShape(
        polygon=face_poly, role=ROLE_GRADED_STRIP, ref=_GAP_FILL_REF,
        node_altitudes=alts + [alts[0]]))
    if getattr(layout, "gap_spines", None) is None:
        layout.gap_spines = []
    # Spine ways to emit: the full spine as today, or — when interior
    # rings emitted (round-8) — the ring-core TRIMMED sub-chains from
    # the builder (a full-length spine would cross the closed loops at
    # the gap ends).  Gate-OFF ``_spine_chains`` is None and the path
    # below is byte-identical to before.
    base_chains: list[list[int]] = (
        _spine_chains if _spine_chains is not None
        else [list(range(len(spine)))])
    if list(gap_poly.interiors):
        # ANNULAR face (gap parent wholly inside): a straight segment
        # between two spine stations can cross the parent hole — that
        # open constrained way would transversally cross the parent's
        # ring (a fresh lens mint).  Split each spine sub-chain at any
        # connecting segment not COVERED by the face; each surviving
        # sub-chain (>= 2 points) emits as its own open way.  Faces
        # without interiors keep the single-way path byte-identical.
        chains: list[list[int]] = []
        for bc in base_chains:
            if not bc:
                continue
            chains.append([bc[0]])
            for a, b in zip(bc, bc[1:]):
                seg = LineString([spine[a], spine[b]])
                covered = False
                try:
                    covered = gap_poly.covers(seg)
                except _GEOM_EXC:
                    covered = False
                if covered:
                    chains[-1].append(b)
                else:
                    chains.append([b])
        emitted_ways = 0
        for chain in chains:
            if len(chain) < 2:
                continue
            pts_ll = [layout.m_to_ll(*spine[j]) for j in chain]
            layout.gap_spines.append(
                (pts_ll, [values[j] for j in chain]))
            emitted_ways += 1
        if emitted_ways == 0:
            # No drainage way survived the parent hole — the face is
            # already appended and keeps its boundary values; log it.
            UI.vprint(1, "  [gap-fill] annular face emitted without a "
                         "drainage spine (every spine segment crossed "
                         "the parent ring).")
        elif emitted_ways > 1:
            UI.vprint(1, f"  [gap-fill] annular face spine split into "
                         f"{emitted_ways} open ways around the parent "
                         f"ring.")
    else:
        for chain in base_chains:
            if len(chain) < 2:
                continue
            pts_ll = [layout.m_to_ll(*spine[j]) for j in chain]
            layout.gap_spines.append(
                (pts_ll, [values[j] for j in chain]))
    return 1
