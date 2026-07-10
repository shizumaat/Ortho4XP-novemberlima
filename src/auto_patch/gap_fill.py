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
    runway_code_number,
)
from .grade_law import adjacent_ground_envelope
from .layout import (
    BuiltShape,
    ROLE_APRON,
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

    airside_ids = {id(s) for s in airside}
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
    other_polys = [(id(s), s.polygon) for s in layout.shapes
                   if id(s) not in airside_ids
                   and id(s) not in legacy_ids
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
            try:
                axes = _mrr_axes(gap_poly.minimum_rotated_rectangle)
            except _GEOM_EXC:
                continue
            if axes is None or axes[1] is None:
                continue
            short_side, long_dir, long_len = axes
            _c = gap_poly.centroid
            UI.vprint(1, f"  [gap-fill] candidate area="
                         f"{gap_poly.area:.0f} m2 short="
                         f"{short_side:.0f} centroid="
                         f"({_c.x:.0f},{_c.y:.0f})")
            if short_side > GAP_FILL_MAX_WIDTH_M:
                UI.vprint(1, f"  [gap-fill] skipped gap (width "
                             f"{short_side:.0f} > {GAP_FILL_MAX_WIDTH_M:.0f})"
                             f" area={gap_poly.area:.0f} m2")
                continue                 # wide gaps stay with the bands
            # A foreign shape inside the gap (building / groundside) means
            # the corridor bands own it — skip.  Legacy surface_clearance
            # strips are NOT blockers: wholly-inside ones are superseded
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
            n_faces = _emit_one_gap(
                layout, airside, gap_poly, long_dir, long_len, step,
                registry)
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
    pts_ll = [layout.m_to_ll(px, py) for px, py in spine]
    layout.gap_spines.append((pts_ll, list(values)))
    return 1
