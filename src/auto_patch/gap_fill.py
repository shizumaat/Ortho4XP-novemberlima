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
spine endpoints land ON the gap ring as T-vertices the pipeline's final
conformance weld inserts exactly — the ONE sanctioned insertion.

Behind ``config.GAP_FILL_SPINE_ENABLED`` (env ``O4_GAP_FILL_SPINE``); the
module checks the gate itself so the pipeline wiring stays one call.
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
    GAP_FILL_MAX_WIDTH_M,
    GAP_FILL_MIN_AREA_M2,
    GAP_FILL_SPINE_ENABLED,
    GAP_FILL_SPINE_STEP_M,
    OPEN_FRONTAGE_CLOSE_M,
    runway_code_number,
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

__all__ = ["emit_gap_fill_spines"]

_GAP_FILL_REF = "gap_fill_spine"
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


def _grade_face(layout, airside, face_poly, step, registry) -> int:
    """Area/width-gate ONE gradeable face (a whole enclosed gap, or a
    pad-residual part) and emit its drainage spine.  Logs the candidate
    and any lawful width/area skip.  Returns the emitted face count."""
    if face_poly.is_empty or not face_poly.is_valid:
        return 0
    if face_poly.area < GAP_FILL_MIN_AREA_M2:
        return 0
    try:
        axes = _mrr_axes(face_poly.minimum_rotated_rectangle)
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
                         step, registry)


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
        axes = _mrr_axes(face_poly.minimum_rotated_rectangle)
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


def emit_gap_fill_spines(layout, dem, tile_lat, tile_lon) -> int:
    """Grade every enclosed gap of the airside pavement union as one unit
    (gate ``GAP_FILL_SPINE_ENABLED``).  Mutates ``layout.shapes``; returns
    the number of ``graded_strip`` half-gap faces emitted.

    ``dem`` / ``tile_lat`` / ``tile_lon`` are part of the one-call
    pipeline contract; the drainage solve is PURE law + pavement reads (an
    enclosed gap is bounded by pavement on all sides, so the DEM never
    enters the interior value), so they are currently unused.
    """
    if not GAP_FILL_SPINE_ENABLED:
        return 0
    airside = [s for s in layout.shapes
               if s.role in _AIRSIDE_PAVEMENT_ROLES
               and s.polygon is not None and not s.polygon.is_empty
               and s.polygon.geom_type == "Polygon"]
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

    step = GAP_FILL_SPINE_STEP_M
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
                    layout, airside, face_poly, step, registry)
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
    return emitted


def _emit_one_gap(layout, airside, gap_poly, long_dir, long_len, step,
                  registry) -> int:
    """Build the drainage spine, solve its values, split the gap into
    half-gap faces and emit them.  Returns the face count."""
    spine = _build_spine(gap_poly, long_dir, long_len, step)
    if spine is None:
        UI.vprint(1, "  [gap-fill] no spine for enclosed gap "
                     f"(area={gap_poly.area:.0f} m2) — skipped.")
        return 0

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
    if not ok:
        UI.vprint(1, "  [gap-fill] no pavement value at spine — skipped.")
        return 0

    # Open-way spine (2026-07-09 round 2): the ends float >= 2 m
    # inside the gap, so they take their own corridor target like
    # every station — the surface between spine end and boundary
    # lerps in the mesh; the pavement value lives on the ring itself.
    values = _smooth_spine(targets, intervals, _SMOOTH_SWEEPS)
    values = [round(v, 1) for v in values]

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
    try:
        face_poly = Polygon(new_ring)
        if not face_poly.is_valid or face_poly.is_empty:
            face_poly = gap_poly
            new_ring = list(_open_coords(gap_poly))
    except _GEOM_EXC:
        face_poly = gap_poly
    layout.shapes.append(BuiltShape(
        polygon=face_poly, role=ROLE_GRADED_STRIP, ref=_GAP_FILL_REF,
        node_altitudes=alts + [alts[0]]))
    if getattr(layout, "gap_spines", None) is None:
        layout.gap_spines = []
    if list(gap_poly.interiors):
        # ANNULAR face (gap parent wholly inside): a straight segment
        # between two spine stations can cross the parent hole — that
        # open constrained way would transversally cross the parent's
        # ring (a fresh lens mint).  Split the spine into sub-chains of
        # consecutive stations whose connecting segments stay COVERED
        # by the face; each sub-chain (>= 2 points) emits as its own
        # open way.  Faces without interiors keep the single-way path
        # byte-identical.
        chains: list[list[int]] = [[0]]
        for i in range(len(spine) - 1):
            seg = LineString([spine[i], spine[i + 1]])
            covered = False
            try:
                covered = gap_poly.covers(seg)
            except _GEOM_EXC:
                covered = False
            if covered:
                chains[-1].append(i + 1)
            else:
                chains.append([i + 1])
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
        pts_ll = [layout.m_to_ll(px, py) for px, py in spine]
        layout.gap_spines.append((pts_ll, list(values)))
    return 1
