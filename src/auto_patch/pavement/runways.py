"""Runway rect emission, crossing resolution, and shoulder detection.

Owns the *static* runway geometry produced from apt.dat row-100
records: a 4-vertex rect including blast pads, plus the geometric
post-passes that operate on runway shapes after the initial emit:

* Runway-segment elevation sampling (interpolated from
  ``altitude_high`` / ``altitude_low``).
* Runway-crossing resolution (replace pairs of crossing runway
  segments with a single junction polygon).
* Runway-chain bridging (fill head-to-head gaps in a runway
  designation's segment chain so the surrounding apron-junction
  altitude doesn't dominate the runway's own CIFP profile).
* Runway-shoulder detection (absorb adjacent long thin pavement
  polygons into the runway's perpendicular extent).

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    _runway_rect_m
    _sample_runway_segment_elev
    _resolve_runway_crossings
    _insert_runway_chain_bridges
    _detect_runway_shoulders
"""
from __future__ import annotations

import math

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..layout import (
    BuiltShape,
    PavementLayout,
    R_EARTH,
    ROLE_CROSS_CONNECTOR,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL,
    ROLE_STUB,
    corner_alts_from_high_low,
)
from ..geom_safe import min_rotated_rect
from .vertices import _snap_polygon_vertices_to_rect_corners

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


__all__ = [
    "_runway_rect_m",
    "_sample_runway_segment_elev",
    "_resolve_runway_crossings",
    "_insert_runway_chain_bridges",
    "_detect_runway_shoulders",
    "_detect_runway_border_strip_shoulders",
    "_detect_runway_shoulder_extent",
    "_widen_runway_rect",
]


def _runway_rect_m(runway, to_m) -> Polygon:
    """4-vertex runway rect spanning end-to-end including blast pads.

    Targets are drawn with blast pads included (matches how the
    runway paint looks in satellite imagery).
    """
    ax, ay = to_m(runway.lon_a, runway.lat_a)
    bx, by = to_m(runway.lon_b, runway.lat_b)
    dx, dy = bx - ax, by - ay
    mag = math.hypot(dx, dy)
    if mag < 1e-9:
        return Polygon()
    ux, uy = dx / mag, dy / mag
    a_extra = runway.blast_a_m or 0.0
    b_extra = runway.blast_b_m or 0.0
    ax2 = ax - ux * a_extra
    ay2 = ay - uy * a_extra
    bx2 = bx + ux * b_extra
    by2 = by + uy * b_extra
    px, py = -uy, ux
    half = runway.width_m / 2.0
    return Polygon([
        (ax2 + px * half, ay2 + py * half),
        (bx2 + px * half, by2 + py * half),
        (bx2 - px * half, by2 - py * half),
        (ax2 - px * half, ay2 - py * half),
    ])


def _sample_runway_segment_elev(
        shape: "BuiltShape",
        x: float,
        y: float) -> "float | None":
    """Linearly interpolate a runway segment's elevation at point
    ``(x, y)`` from its ``altitude_high`` / ``altitude_low``
    (sloped 4-corner rect) or ``altitude`` (flat).

    Returns None when the segment carries no elevation info.

    Convention used by :func:`_compute_elevations` segment emit
    (line ~2742): corners ``[0, 3]`` are the HIGH-elevation short
    edge, corners ``[1, 2]`` are the LOW-elevation short edge.
    Point's projection onto the high → low axis gives the
    interpolation parameter ``t`` (clipped to [0, 1] outside the
    segment's extent).
    """
    if shape.altitude is not None:
        return float(shape.altitude)
    # Per-vertex node_altitudes — the UNIFIED runway representation
    # (user 2026-07-06; previously only tile-cut pieces).  AXIS-PROJECTED
    # linear interpolation over the ring's (t, value) pairs: project the
    # query and every ring vertex onto the piece's long axis and
    # interpolate value(t).  Exact for the near-planar quads runways are
    # built from, and — unlike the earlier least-squares PLANE FIT —
    # correct for CURVED pieces too: the runway FLEX re-writes a bent
    # profile into pieces cut for the old breakpoints, and the plane fit
    # extrapolated ~3 m wrong at a flexed piece's ends (HECA 05L: the
    # runway-join anchor stamped 58.30 over the flexed 61.21 hard node
    # → a 24 % step inside the runway).
    if shape.node_altitudes and shape.polygon is not None:
        try:
            coords = list(shape.polygon.exterior.coords)
        except _GEOM_EXC:
            coords = []
        n = min(len(coords), len(shape.node_altitudes))
        if n >= 3:
            # long axis = the ring's DIAMETER (the farthest vertex pair).
            # A bounding-box diagonal always points into the (+x, +y)
            # quadrant and is perpendicular-ish to a SE-heading runway
            # (SPJC 16L/34R: garbage interpolation → 0.4 m join-anchor
            # errors); the diameter follows the true heading.
            far_a = far_b = 0
            far_d2 = -1.0
            for i in range(n):
                for j in range(i + 1, n):
                    d2 = ((coords[i][0] - coords[j][0]) ** 2
                          + (coords[i][1] - coords[j][1]) ** 2)
                    if d2 > far_d2:
                        far_d2 = d2
                        far_a, far_b = i, j
            span = math.sqrt(far_d2) if far_d2 > 0 else 0.0
            if span > 1e-6:
                axis_dx = (coords[far_b][0] - coords[far_a][0]) / span
                axis_dy = (coords[far_b][1] - coords[far_a][1]) / span
                samples = sorted(
                    ((coords[i][0] * axis_dx + coords[i][1] * axis_dy,
                      float(shape.node_altitudes[i]))
                     for i in range(n)),
                    key=lambda p: p[0])
                tq = x * axis_dx + y * axis_dy
                if tq <= samples[0][0]:
                    return samples[0][1]
                if tq >= samples[-1][0]:
                    return samples[-1][1]
                for k in range(1, len(samples)):
                    t0, v0 = samples[k - 1]
                    t1, v1 = samples[k]
                    if tq <= t1:
                        if t1 - t0 < 1e-9:
                            return 0.5 * (v0 + v1)
                        frac = (tq - t0) / (t1 - t0)
                        return v0 + frac * (v1 - v0)
        if n >= 1:
            best_d2 = float("inf")
            best_alt: float | None = None
            for i in range(n):
                cx, cy = coords[i]
                d2 = (x - cx) ** 2 + (y - cy) ** 2
                if d2 < best_d2:
                    best_d2 = d2
                    best_alt = shape.node_altitudes[i]
            if best_alt is not None:
                return float(best_alt)
    if (shape.altitude_high is None
            or shape.altitude_low is None
            or shape.polygon is None):
        return None
    try:
        coords = list(shape.polygon.exterior.coords)
    except _GEOM_EXC:
        return None
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    if len(coords) < 4:
        return 0.5 * (float(shape.altitude_high)
                      + float(shape.altitude_low))
    high_mid_x = 0.5 * (coords[0][0] + coords[3][0])
    high_mid_y = 0.5 * (coords[0][1] + coords[3][1])
    low_mid_x = 0.5 * (coords[1][0] + coords[2][0])
    low_mid_y = 0.5 * (coords[1][1] + coords[2][1])
    ax = low_mid_x - high_mid_x
    ay = low_mid_y - high_mid_y
    L2 = ax * ax + ay * ay
    if L2 < 1e-6:
        return 0.5 * (float(shape.altitude_high)
                      + float(shape.altitude_low))
    t = ((x - high_mid_x) * ax + (y - high_mid_y) * ay) / L2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return (float(shape.altitude_high) + t
            * (float(shape.altitude_low)
               - float(shape.altitude_high)))


def _runway_segment_centerline(poly):
    """Return a LineString through a runway segment's long axis,
    or ``None`` if the geometry isn't usable.

    A runway segment is a sloped rect built by ``_rect_from_axis_
    extended`` with the canonical convention: corners 0 and 3 at one
    axis-end (HIGH), corners 1 and 2 at the other (LOW).  The
    centerline runs from midpoint(0,3) to midpoint(1,2) regardless
    of which polygon side happens to be longer — for very-short
    runway sub-rects (e.g. CYXY 14R/32L at 30 m long × 45 m wide,
    typical after seam-driven subdivision) the runway's WIDTH
    exceeds its segment length and an OBB-based axis-finder would
    pick the wrong direction.
    """
    if poly is None or poly.is_empty:
        return None
    try:
        coords = list(poly.exterior.coords)
    except _GEOM_EXC:
        return None
    if coords and coords[0] == coords[-1]:
        coords = coords[:-1]
    n = len(coords)
    if n < 3:
        return None
    if n == 4:
        mid_a = (0.5 * (coords[0][0] + coords[3][0]),
                 0.5 * (coords[0][1] + coords[3][1]))
        mid_b = (0.5 * (coords[1][0] + coords[2][0]),
                 0.5 * (coords[1][1] + coords[2][1]))
        if math.hypot(mid_a[0] - mid_b[0], mid_a[1] - mid_b[1]) < 1.0:
            return None
        return LineString([mid_a, mid_b])
    # Non-4-corner (seam-inserted): the original two short edges
    # remain the shortest two sides.  Find the perpendicular pair
    # via the oriented bounding box and project polygon vertices to
    # find each axis-end's midpoint.
    try:
        obb = min_rotated_rect(poly)
        obb_coords = list(obb.exterior.coords)
        if obb_coords and obb_coords[0] == obb_coords[-1]:
            obb_coords = obb_coords[:-1]
    except _GEOM_EXC:
        return None
    if len(obb_coords) != 4:
        return None
    side_lens = [
        math.hypot(obb_coords[(i + 1) % 4][0] - obb_coords[i][0],
                   obb_coords[(i + 1) % 4][1] - obb_coords[i][1])
        for i in range(4)]
    # The long-axis side runs between two short-edge midpoints.
    # The runway's long axis is parallel to the SHORTER OBB side
    # only when the polygon is wider than long (post-seam subdivision);
    # parallel to the LONGER OBB side otherwise.  Without segment-
    # length context, fall back to the longer-side assumption,
    # matching pre-seam behaviour for fragments larger than the
    # runway width.
    long_idx = 0 if side_lens[0] >= side_lens[1] else 1
    short1 = ((long_idx + 1) % 4, (long_idx + 2) % 4)
    short2 = ((long_idx + 3) % 4, (long_idx + 0) % 4)
    mid_a = (0.5 * (obb_coords[short1[0]][0] + obb_coords[short1[1]][0]),
             0.5 * (obb_coords[short1[0]][1] + obb_coords[short1[1]][1]))
    mid_b = (0.5 * (obb_coords[short2[0]][0] + obb_coords[short2[1]][0]),
             0.5 * (obb_coords[short2[0]][1] + obb_coords[short2[1]][1]))
    if math.hypot(mid_a[0] - mid_b[0], mid_a[1] - mid_b[1]) < 1.0:
        return None
    return LineString([mid_a, mid_b])


def _resolve_runway_crossings(
        layout: "PavementLayout",
        min_overlap_m2: float = 20.0,
        proximity_buffer_m: float = 2.0,
        ) -> int:
    """When two runway segments cross significantly, replace BOTH
    with a single junction polygon covering their union, with
    per-vertex altitudes interpolated from the source segments.

    Per user 2026-04-27: the existing overlap-clip pass clips one
    runway segment against another when they cross, producing a
    non-rectangular shape (5+ vertices) that still carries
    ``altitude_high`` / ``altitude_low`` tags — but X-Plane's patch
    format only renders 4-corner rects with H/L tagging, so the
    extra vertex breaks the slope rendering.  Crossings need
    multi-directional sloping which only a junction polygon
    (with per-vertex altitudes and triangulated rendering) can
    express.

    Detection uses an STRtree + union-find so transitively-
    overlapping segment groups are resolved together (e.g. CYXY's
    crosswind 02/20 crosses BOTH 14R/32L and 14L/32R; all three
    segment groups merge into one junction at the triple crossing).

    Returns the number of crossing groups resolved.
    """
    rwy_indices = [i for i, s in enumerate(layout.shapes)
                    if s.role == ROLE_RUNWAY
                    and s.polygon is not None
                    and not s.polygon.is_empty]
    if len(rwy_indices) < 2:
        return 0
    rwy_shapes = [layout.shapes[i] for i in rwy_indices]
    rwy_polys = [s.polygon for s in rwy_shapes]
    rwy_refs = [s.ref for s in rwy_shapes]
    rwy_centerlines = [_runway_segment_centerline(p) for p in rwy_polys]
    from shapely.strtree import STRtree
    try:
        tree = STRtree(rwy_polys)
    except _GEOM_EXC:
        return 0

    # Union-find for transitive grouping.
    n = len(rwy_indices)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union_uf(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # A "real" runway crossing has two CENTERLINES that intersect —
    # this is the only case where two runways physically share a
    # surface and their elevations must reconcile.  Rect-polygon
    # overlap alone (the previous criterion, area > 20 m²) caught
    # close-pass non-crossing runways: at CYXY runway 02/20's rect
    # corner intrudes into runway 14R/32L's rect width by ~3-15 m,
    # producing a triangular overlap region that the old code
    # treated as a crossing.  But the two centerlines don't meet,
    # so each runway follows its own CIFP profile and the two
    # disagree by ~4 m at the close-pass — the resulting "crossing"
    # junction had HARD-anchored corners from both runways that
    # couldn't simultaneously satisfy the 1.5 % grade rule.  Real
    # geometric overlap of two non-crossing runways is the overlap-
    # clip pass's job; ``_resolve_runway_crossings`` only fires
    # when there's an actual centerline meeting point.
    # ── Pass 1: identify crossing runway-REF PAIRS ──────────────
    # Two runways cross when any of their sub-rect CENTERLINES
    # intersect (+ a non-trivial rect overlap so a stray tip-touch
    # doesn't count).  Centerline crossing is the discriminator
    # that separates a real crossing from a close-pass: at CYXY
    # 02/20's rect corner intrudes ~3-15 m into 14R/32L's width
    # without the centerlines meeting — that's the overlap-clip
    # pass's job, not a crossing.
    crossing_ref_pairs: set = set()
    for ai in range(n):
        pa = rwy_polys[ai]
        ca = rwy_centerlines[ai]
        try:
            cands = tree.query(pa)
        except _GEOM_EXC:
            continue
        for ci in cands:
            bi = int(ci)
            if bi <= ai:
                continue
            if rwy_refs[ai] and rwy_refs[ai] == rwy_refs[bi]:
                continue
            cb = rwy_centerlines[bi]
            if ca is None or cb is None:
                continue
            try:
                if not ca.intersects(cb):
                    continue
                inter = rwy_polys[ai].intersection(rwy_polys[bi])
                if inter.is_empty or inter.area < min_overlap_m2:
                    continue
            except _GEOM_EXC:
                continue
            crossing_ref_pairs.add(
                frozenset((rwy_refs[ai], rwy_refs[bi])))

    # ── Pass 2: merge ALL overlapping sub-rect pairs of a crossing
    # runway-ref pair ──────────────────────────────────────────────
    # Once two runways are known to cross, the crossing junction must
    # cover the FULL geometric overlap of their two footprints — not
    # just the single sub-rect pair whose centerlines happen to meet.
    # A runway crossing at an angle spans ~100 m of one runway's axis,
    # which overlaps 1-2 sub-rects of the OTHER runway (segmented every
    # 100 m).  Merging only the centerline-crossing pair left the
    # adjacent overlapping sub-rect to be clipped away by the
    # overlap-clip pass while the junction didn't extend to replace it
    # — producing an uncovered gap at the crossing (CYXY 14R/32L:
    # ~3300 m² hole that X-Plane fills with terrain DEM, creating a
    # ridge across the runway).  Restricting to known crossing
    # ref-pairs keeps close-pass non-crossing runways out (their
    # ref-pair never enters ``crossing_ref_pairs``).
    for ai in range(n):
        pa = rwy_polys[ai]
        try:
            cands = tree.query(pa)
        except _GEOM_EXC:
            continue
        for ci in cands:
            bi = int(ci)
            if bi <= ai:
                continue
            if rwy_refs[ai] and rwy_refs[ai] == rwy_refs[bi]:
                continue
            if (frozenset((rwy_refs[ai], rwy_refs[bi]))
                    not in crossing_ref_pairs):
                continue
            try:
                inter = rwy_polys[ai].intersection(rwy_polys[bi])
                if inter.is_empty or inter.area < min_overlap_m2:
                    continue
                union_uf(ai, bi)
            except _GEOM_EXC:
                continue

    # Group by root; ignore singleton groups.
    groups: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, []).append(i)

    # Pre-compute sloping-rect corner snap targets for the corner-
    # alignment pass below.  Sloping rects = runway + parallel +
    # stub + cross-connector; junction vertices can only land on
    # their corners, never on their edges.
    sloping_roles_for_snap = (ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
                              ROLE_SECONDARY_PARALLEL, ROLE_STUB,
                              ROLE_CROSS_CONNECTOR)
    all_sloping_indices = [i for i, s in enumerate(layout.shapes)
                            if s.role in sloping_roles_for_snap
                            and s.polygon is not None
                            and not s.polygon.is_empty]

    drop_set: set = set()
    new_shapes: list[BuiltShape] = []
    n_resolved = 0
    for members in groups.values():
        if len(members) <= 1:
            continue
        seg_shapes = [layout.shapes[rwy_indices[m]] for m in members]
        seg_polys = [s.polygon for s in seg_shapes]
        member_set = {rwy_indices[m] for m in members}
        try:
            union_poly = unary_union(seg_polys)
            if not union_poly.is_valid:
                union_poly = union_poly.buffer(0)
        except _GEOM_EXC:
            continue
        if union_poly.is_empty:
            continue
        if union_poly.geom_type != "Polygon":
            polys = [g for g in getattr(union_poly, "geoms", [])
                      if g.geom_type == "Polygon"]
            polys.sort(key=lambda p: -p.area)
            if not polys:
                continue
            union_poly = polys[0]
        # Snap any union-polygon vertex that lands within 5 m of a
        # surviving sloping-rect corner to that corner.  Without
        # this, ``unary_union``'s intersection points can sit a few
        # metres along a surviving rect's edge — violating the
        # corner-only-junction-vertex invariant.
        other_sloping_polys = [
            layout.shapes[i].polygon
            for i in all_sloping_indices
            if i not in member_set]
        union_poly = _snap_polygon_vertices_to_rect_corners(
            union_poly, other_sloping_polys, snap_tol_m=5.0)
        # Route through the canonical-point registry (user
        # 2026-05-18) so the runway-crossing junction's corners
        # are registered as canonical sources for any adjacent
        # rect / junction that subsequently snaps near them.
        try:
            from ..canonical_points import (
                snap_polygon_through_registry as _snap_reg)
            _reg = getattr(layout, "canonical_points", None)
            if _reg is not None:
                union_poly = _snap_reg(union_poly, _reg)
                if (union_poly is None
                        or union_poly.is_empty
                        or union_poly.geom_type != "Polygon"):
                    continue
        except _GEOM_EXC:
            pass
        try:
            coords = list(union_poly.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) < 3:
            continue

        # Per-vertex altitudes — INVERSE-DISTANCE-WEIGHTED average
        # across EVERY crossing segment.  Per user 2026-04-29
        # (CYXY -10075 ridge): the previous "buffer-containment"
        # selection picked a single nearest segment per vertex,
        # so two adjacent ring vertices on opposite sides of a
        # cross-runway gap could pick up different source-runway
        # altitudes (e.g. 14R/32L's 696.7 m vs 02/20's 693.7 m),
        # producing local steps as steep as 10 % between
        # neighbours and a +3.2 m ridge across runway 32L/16R.
        # Inverse-distance weighting blends every vertex's
        # altitude smoothly between all crossing runways — the
        # vertex that's right on a 14R/32L edge gets near-100 %
        # weight from that segment (d→0 ⇒ w→∞), while a vertex
        # equidistant between two runways gets a 50/50 average,
        # and the transition between the two regimes is smooth.
        ring_alts: list[float | None] = []
        for (cx, cy) in coords:
            pt = Point(cx, cy)
            weighted_sum = 0.0
            weight_sum = 0.0
            for s in seg_shapes:
                e = _sample_runway_segment_elev(s, cx, cy)
                if e is None:
                    continue
                try:
                    d = s.polygon.distance(pt)
                except _GEOM_EXC:
                    continue
                # ε floor so a vertex exactly on a segment edge
                # still has a finite weight (just very large).
                d_eff = max(d, 0.1)
                w = 1.0 / (d_eff ** 2)
                weighted_sum += float(e) * w
                weight_sum += w
            if weight_sum > 0:
                ring_alts.append(
                    round(weighted_sum / weight_sum, 1))
            else:
                ring_alts.append(None)
        if any(a is None for a in ring_alts):
            continue
        # node_altitudes spans the closed ring.
        closed_alts: list[float] = list(ring_alts) + [ring_alts[0]]
        ref_combined = "+".join(
            s.ref for s in seg_shapes if s.ref)
        new_shape = BuiltShape(
            polygon=union_poly,
            role=ROLE_RUNWAY_CROSSING,
            ref=ref_combined,
            node_altitudes=closed_alts)
        new_shapes.append(new_shape)
        for m in members:
            drop_set.add(rwy_indices[m])
        n_resolved += 1

    if drop_set:
        layout.shapes = [s for i, s in enumerate(layout.shapes)
                          if i not in drop_set]
        layout.shapes.extend(new_shapes)
    if n_resolved:
        _absorb_crossing_vertices_into_adjacent_rects(layout)
    return n_resolved


def _absorb_crossing_vertices_into_adjacent_rects(
        layout: "PavementLayout",
        perp_tol_m: float = 0.5,
        corner_tol_m: float = 0.5,
) -> int:
    """Convert any canonical 4-corner runway sub-rect whose sloping
    edge has a runway_crossing vertex on its INTERIOR to per-vertex
    ``node_altitudes`` form, with the foreign vertex inserted into
    the rect's ring.

    Per user 2026-05-19: when a runway-crossing polygon's boundary
    walks past an adjacent runway sub-rect's corner without quite
    reaching it (the union/snap pipeline produces vertices a few
    metres from the rect's nearest corner because the runways meet
    at an oblique angle), the crossing's vertex lands on the rect's
    sloping edge interior.  ``test_no_vertex_on_sloping_rect_edge``
    enforces "junctions share only CORNERS, never edge interiors"
    on canonical rects; the architectural escape hatch is to admit
    the rect can no longer maintain its planar 4-corner contract
    along that shared boundary, and convert it to ``node_altitudes``
    (which the invariant test legitimately exempts).  Altitudes
    along the new ring are interpolated from the original
    altitude_high/low profile (linear along each edge), preserving
    the same planar surface — just expressed per-vertex now.

    Returns the number of rects converted.
    """
    rwy_shapes: list[tuple[int, "BuiltShape"]] = []
    for i, s in enumerate(layout.shapes):
        if s.role != ROLE_RUNWAY:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        if s.node_altitudes is not None:
            continue
        if s.altitude_high is None or s.altitude_low is None:
            continue
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            continue
        rwy_shapes.append((i, s))
    rc_vertices: list[tuple[float, float]] = []
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY_CROSSING:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            rc_coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if rc_coords and rc_coords[0] == rc_coords[-1]:
            rc_coords = rc_coords[:-1]
        rc_vertices.extend((float(x), float(y)) for x, y in rc_coords)
    if not rwy_shapes or not rc_vertices:
        return 0

    n_converted = 0
    for _idx, r in rwy_shapes:
        coords = list(r.polygon.exterior.coords)
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        if len(coords) != 4:
            continue
        # Canonical [HI-LEFT, LO-LEFT, LO-RIGHT, HI-RIGHT] convention:
        # corners 0, 3 at HI altitude; corners 1, 2 at LO altitude.
        # This matches how ``runway_segments.add_rect_patch`` builds
        # runway sub-rects (using ``runway_corners(lat_A, lon_A,
        # lat_B, lon_B, width)`` with the HIGH end first).
        eh = float(r.altitude_high)
        el = float(r.altitude_low)
        corner_alts = corner_alts_from_high_low(eh, el)
        new_ring: list[tuple[float, float]] = []
        new_alts: list[float] = []
        any_insert = False
        for k in range(4):
            new_ring.append((float(coords[k][0]), float(coords[k][1])))
            new_alts.append(corner_alts[k])
            ax, ay = coords[k]
            bx, by = coords[(k + 1) % 4]
            dx = bx - ax
            dy = by - ay
            edge_L2 = dx * dx + dy * dy
            if edge_L2 < 1.0:
                continue
            edge_L = math.sqrt(edge_L2)
            # Skip edges shorter than corner_tol_m × 2 — there's no
            # interior to land on.
            if edge_L < 2.0 * corner_tol_m:
                continue
            edge_inserts: list[tuple[float, float, float]] = []
            for fx, fy in rc_vertices:
                t = ((fx - ax) * dx + (fy - ay) * dy) / edge_L2
                if t * edge_L <= corner_tol_m:
                    continue
                if (1.0 - t) * edge_L <= corner_tol_m:
                    continue
                px = ax + t * dx
                py = ay + t * dy
                perp_d = math.hypot(fx - px, fy - py)
                if perp_d > perp_tol_m:
                    continue
                # Deduplicate near-coincident foreign vertices on the
                # same edge (e.g. multiple runway_crossing polygons
                # contributing the same boundary point).
                if any(abs(t - et) * edge_L < corner_tol_m
                       for et, _, _ in edge_inserts):
                    continue
                edge_inserts.append((t, float(fx), float(fy)))
            edge_inserts.sort(key=lambda r: r[0])
            for t, fx, fy in edge_inserts:
                # Altitude interpolation along this edge between
                # corner k and corner k+1.
                a_alt = corner_alts[k]
                b_alt = corner_alts[(k + 1) % 4]
                new_ring.append((fx, fy))
                new_alts.append(a_alt + t * (b_alt - a_alt))
                any_insert = True
        if not any_insert:
            continue
        if len(new_ring) < 4:
            continue
        try:
            new_poly = Polygon(new_ring + [new_ring[0]])
            if not new_poly.is_valid or new_poly.is_empty:
                continue
        except _GEOM_EXC:
            continue
        r.polygon = new_poly
        r.node_altitudes = new_alts + [new_alts[0]]
        r.altitude_high = None
        r.altitude_low = None
        n_converted += 1
    return n_converted


def _insert_runway_chain_bridges(
        layout: "PavementLayout",
        min_gap_m: float = 5.0,
        max_gap_m: float = 500.0,
        ) -> int:
    """For each runway designation, find pairs of surviving
    runway segments whose facing short edges are head-to-head
    with a gap > ``min_gap_m`` and < ``max_gap_m``, and insert a
    bridging RUNWAY shape between them with linearly
    interpolated altitudes.

    Per user 2026-04-30 (CYXY runway 32L/16R ridge): when one
    runway is absorbed into another via the runway-crossing
    resolver (or into a big apron via the apron-merge drop), the
    remaining runway segments leave a gap in the chain.  The
    surrounding apron-junction polygon's altitudes (DEM-derived,
    700+m at CYXY) then dominate the surface at the gap, sitting
    several metres above the runway's CIFP profile (693-697m at
    CYXY).  X-Plane renders this as a ridge crossing the runway.

    Inserting a bridging runway shape with altitudes anchored at
    the surviving segments' adjacent corners restores a runway-
    altitude plate at the gap.  The shape is emitted as
    ``ROLE_RUNWAY`` so downstream snap passes treat it the same
    way they treat a normal runway segment (corner-bucket
    altitude propagation, sloping rect H/L tagging, etc.).

    Detection algorithm (works at any airport):
      1. Group runway segments by their ``ref`` value (CIFP
         designation, e.g. "RW14R/RW32L").
      2. Within each group, sort segments along their shared
         axis direction.
      3. For each consecutive pair in axis order, measure the
         distance between the FIRST segment's "far" short edge
         and the SECOND segment's "near" short edge.  If
         ``min_gap_m < distance < max_gap_m`` AND the bridging
         rect's footprint is empty of any other runway segment
         (so we don't bridge across an active crossing), emit
         a bridge.

    Returns the number of bridge segments inserted.
    """
    rwy_shapes = [s for s in layout.shapes
                   if s.role == ROLE_RUNWAY
                   and s.polygon is not None
                   and not s.polygon.is_empty
                   and s.ref]
    if len(rwy_shapes) < 2:
        return 0
    # Group by ref.
    by_ref: dict[str, list[BuiltShape]] = {}
    for s in rwy_shapes:
        by_ref.setdefault(s.ref, []).append(s)
    n_inserted = 0
    other_rwy_polys = [s.polygon for s in rwy_shapes]
    try:
        from shapely.strtree import STRtree as _STRtree
        rwy_tree = _STRtree(other_rwy_polys)
    except _GEOM_EXC:
        rwy_tree = None
    new_shapes: list[BuiltShape] = []
    for ref, segs in by_ref.items():
        if len(segs) < 2:
            continue
        # Determine the runway's axis direction from the FIRST
        # segment's altitude_high → altitude_low axis (corners
        # 0,3 = HIGH, 1,2 = LOW).
        s0 = segs[0]
        try:
            c0 = list(s0.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if c0 and c0[0] == c0[-1]:
            c0 = c0[:-1]
        if len(c0) != 4:
            continue
        h_mid_0 = (0.5 * (c0[0][0] + c0[3][0]),
                   0.5 * (c0[0][1] + c0[3][1]))
        l_mid_0 = (0.5 * (c0[1][0] + c0[2][0]),
                   0.5 * (c0[1][1] + c0[2][1]))
        ax = l_mid_0[0] - h_mid_0[0]
        ay = l_mid_0[1] - h_mid_0[1]
        alen = math.hypot(ax, ay)
        if alen < 1.0:
            continue
        ux = ax / alen
        uy = ay / alen
        # Project each segment's centroid onto the axis to sort.
        def axis_pos(seg: BuiltShape) -> float:
            cx, cy = seg.polygon.centroid.x, seg.polygon.centroid.y
            return (cx - h_mid_0[0]) * ux + (cy - h_mid_0[1]) * uy
        segs_sorted = sorted(segs, key=axis_pos)
        # Walk consecutive pairs.
        for i in range(len(segs_sorted) - 1):
            a, b = segs_sorted[i], segs_sorted[i + 1]
            try:
                ca = list(a.polygon.exterior.coords)
                cb = list(b.polygon.exterior.coords)
            except _GEOM_EXC:
                continue
            if ca and ca[0] == ca[-1]:
                ca = ca[:-1]
            if cb and cb[0] == cb[-1]:
                cb = cb[:-1]
            if len(ca) != 4 or len(cb) != 4:
                continue
            # Find a's "far end" (the short edge facing b) and
            # b's "near end" (the short edge facing a).  Use
            # axis projection: a's far end has higher axis_pos,
            # b's near end has lower axis_pos.
            a_h_mid = (0.5 * (ca[0][0] + ca[3][0]),
                        0.5 * (ca[0][1] + ca[3][1]))
            a_l_mid = (0.5 * (ca[1][0] + ca[2][0]),
                        0.5 * (ca[1][1] + ca[2][1]))
            b_h_mid = (0.5 * (cb[0][0] + cb[3][0]),
                        0.5 * (cb[0][1] + cb[3][1]))
            b_l_mid = (0.5 * (cb[1][0] + cb[2][0]),
                        0.5 * (cb[1][1] + cb[2][1]))
            ah_pos = ((a_h_mid[0] - h_mid_0[0]) * ux
                       + (a_h_mid[1] - h_mid_0[1]) * uy)
            al_pos = ((a_l_mid[0] - h_mid_0[0]) * ux
                       + (a_l_mid[1] - h_mid_0[1]) * uy)
            bh_pos = ((b_h_mid[0] - h_mid_0[0]) * ux
                       + (b_h_mid[1] - h_mid_0[1]) * uy)
            bl_pos = ((b_l_mid[0] - h_mid_0[0]) * ux
                       + (b_l_mid[1] - h_mid_0[1]) * uy)
            # a's far short edge: whichever of (a_h_mid, a_l_mid)
            # has higher axis_pos.  Same for b's near edge: whichever
            # has lower axis_pos.
            if ah_pos > al_pos:
                a_far_corners = (ca[0], ca[3])
                a_far_alt = a.altitude_high
            else:
                a_far_corners = (ca[1], ca[2])
                a_far_alt = a.altitude_low
            if bh_pos < bl_pos:
                b_near_corners = (cb[0], cb[3])
                b_near_alt = b.altitude_high
            else:
                b_near_corners = (cb[1], cb[2])
                b_near_alt = b.altitude_low
            if (a_far_alt is None and a.altitude is not None):
                a_far_alt = a.altitude
            if (b_near_alt is None and b.altitude is not None):
                b_near_alt = b.altitude
            if a_far_alt is None or b_near_alt is None:
                continue
            # Distance between a's far midpoint and b's near
            # midpoint.
            a_far_mid = (0.5 * (a_far_corners[0][0]
                                  + a_far_corners[1][0]),
                          0.5 * (a_far_corners[0][1]
                                  + a_far_corners[1][1]))
            b_near_mid = (0.5 * (b_near_corners[0][0]
                                   + b_near_corners[1][0]),
                           0.5 * (b_near_corners[0][1]
                                   + b_near_corners[1][1]))
            gap = math.hypot(b_near_mid[0] - a_far_mid[0],
                             b_near_mid[1] - a_far_mid[1])
            if gap < min_gap_m or gap > max_gap_m:
                continue
            # Build bridge polygon.  Corners:
            #   0,3 = HIGH end (whichever of a/b has higher alt)
            #   1,2 = LOW end
            if a_far_alt >= b_near_alt:
                # a's far end is HIGH, b's near end is LOW.
                bridge_corners = [
                    a_far_corners[0],   # 0: A-side corner 1 (HIGH)
                    b_near_corners[0],  # 1: B-side corner 1 (LOW)
                    b_near_corners[1],  # 2: B-side corner 2 (LOW)
                    a_far_corners[1],   # 3: A-side corner 2 (HIGH)
                ]
                eh, el = float(a_far_alt), float(b_near_alt)
            else:
                bridge_corners = [
                    b_near_corners[0],
                    a_far_corners[0],
                    a_far_corners[1],
                    b_near_corners[1],
                ]
                eh, el = float(b_near_alt), float(a_far_alt)
            try:
                poly = Polygon(bridge_corners)
                if not poly.is_valid:
                    poly = poly.buffer(0)
                if poly.is_empty or poly.geom_type != "Polygon":
                    continue
            except _GEOM_EXC:
                continue
            # Reject if the bridge polygon overlaps any OTHER
            # runway segment (we'd be bridging across an active
            # crossing).
            overlap_with_other = False
            if rwy_tree is not None:
                try:
                    for hit in rwy_tree.query(poly):
                        idx = (int(hit) if hasattr(hit, "__int__")
                               else hit)
                        if not isinstance(idx, int):
                            continue
                        other = other_rwy_polys[idx]
                        if other is a.polygon or other is b.polygon:
                            continue
                        try:
                            inter = poly.intersection(other)
                            if (not inter.is_empty
                                    and inter.area > 1.0):
                                overlap_with_other = True
                                break
                        except _GEOM_EXC:
                            continue
                except _GEOM_EXC:
                    pass
            if overlap_with_other:
                continue
            shape = BuiltShape(
                polygon=poly, role=ROLE_RUNWAY, ref=ref)
            if abs(eh - el) >= 0.1:
                shape.altitude_high = round(eh, 1)
                shape.altitude_low = round(el, 1)
            else:
                shape.altitude = round(0.5 * (eh + el), 1)
            new_shapes.append(shape)
            n_inserted += 1
    if new_shapes:
        layout.shapes.extend(new_shapes)
    return n_inserted


def _detect_runway_shoulders(
        runway,
        to_m,
        pav_polys: "list[Polygon]",
        max_lat_gap_m: float = 1.0,
        min_self_inside_frac: float = 0.80,
        min_runway_overlap_frac: float = 0.40,
        min_axial_aspect: float = 2.5,
        min_strip_length_m: float = 50.0,
        ) -> "tuple[float, float, list[int]]":
    """Scan ``pav_polys`` for polygons that are runway pavement
    (shoulders or the runway's own envelope polygon often labelled
    as a "taxiway" by apt.dat) and return the perpendicular extent
    those polygons + the runway itself jointly occupy.

    Per user 2026-04-27: long thin pavement polygons parallel to a
    runway and touching it (within 1 m of the runway edge) are
    SHOULDERS — fold them into the runway emit so the runway
    pavement reflects actual paved area, not just the apt.dat row-100
    designation.  At HECA, runway 05R/23L's apt.dat row-100 width is
    60 m but a row-110 polygon "New Taxiway 1" sits centered on the
    runway at 75.6 m wide (the runway-with-shoulders envelope).
    Without this absorption, the 7.8 m-per-side shoulder strip
    emits as junctions wrapping the runway.  At CYXY's short
    crosswind 02/20 (22.9 m wide), narrow strips "North of 02" and
    "South of 02" act as shoulders extending the runway pavement
    asymmetrically.

    Returns ``(new_left, new_right, absorbed_indices)``:
      * ``new_left``  — most-negative perpendicular offset from the
        runway centerline that emitted runway pavement should reach.
      * ``new_right`` — most-positive perpendicular offset.
      * ``absorbed_indices`` — indices into ``pav_polys`` of the
        polygons folded into the runway.  Caller should remove them
        from ``pav_polys`` (and ``apt_only_pav_polys`` if applicable)
        so they don't re-emit as separate junction shapes.

    When no shoulders are found, returns ``(-runway_half, +runway_half,
    [])``.

    Detection rules per polygon:
      1. Long axis aligned with runway (axial extent ≥
         ``min_axial_aspect`` × perpendicular extent, axial extent ≥
         ``min_strip_length_m``).
      2. Polygon mostly inside the runway's longitudinal extent:
         ``polygon's u-extent inside [0, L] ≥ min_self_inside_frac
         × polygon's total u-extent``.
      3. Polygon covers a significant fraction of the runway's
         length: ``u-overlap with [0, L] ≥ min_runway_overlap_frac
         × runway length L``.  Filters out tiny end-only blast pads
         (e.g. CYXY's '32L' polygon, 79 m at one end of a 2899 m
         runway) while admitting partial-length shoulders that
         cover roughly half the runway (e.g. CYXY's "North of 02"
         covering 47 % of the 02/20 runway).
      4. Perpendicular interval overlaps or touches
         ``[-runway_half, +runway_half]`` within ``max_lat_gap_m``.
      5. Side-extension limit: the polygon's extension PAST the
         runway edge on each side is less than the runway width.
         Filters out large aprons (e.g. CYXY's "Apron 1 and E",
         329 m wide, that nibbles the runway edge by 4 m) while
         admitting both wide envelopes (HECA's runway-with-
         shoulders polygon, 7.8 m past each edge) and narrow
         single-side shoulders (CYXY's "North of 02", 18 m past
         the north edge).
    """
    ax, ay = to_m(runway.lon_a, runway.lat_a)
    bx, by = to_m(runway.lon_b, runway.lat_b)
    udx = bx - ax
    udy = by - ay
    L = math.hypot(udx, udy)
    runway_half = runway.width_m / 2.0
    if L < 1.0:
        return (-runway_half, runway_half, [])
    ux, uy = udx / L, udy / L
    nx, ny = -uy, ux  # perpendicular (rotated 90° CCW from u)

    runway_width = 2.0 * runway_half
    new_left = -runway_half
    new_right = runway_half
    absorbed: list[int] = []
    for idx, pav in enumerate(pav_polys):
        if pav is None or pav.is_empty:
            continue
        try:
            coords = list(pav.exterior.coords)
        except _GEOM_EXC:
            continue
        if not coords:
            continue
        u_proj = [(cx - ax) * ux + (cy - ay) * uy for cx, cy in coords]
        n_proj = [(cx - ax) * nx + (cy - ay) * ny for cx, cy in coords]
        u_min, u_max = min(u_proj), max(u_proj)
        n_min, n_max = min(n_proj), max(n_proj)
        u_extent = u_max - u_min
        n_extent = n_max - n_min
        if u_extent < min_strip_length_m:
            continue
        if u_extent < min_axial_aspect * n_extent:
            continue
        # Polygon must be mostly inside the runway's longitudinal
        # extent.
        u_overlap = min(u_max, L) - max(u_min, 0.0)
        if u_overlap < min_self_inside_frac * u_extent:
            continue
        # Polygon must cover a significant fraction of the runway
        # length (filters out end-only blast pads / stopways).
        if u_overlap < min_runway_overlap_frac * L:
            continue
        # Perpendicular adjacency / overlap.
        if n_max < -runway_half - max_lat_gap_m:
            continue
        if n_min > runway_half + max_lat_gap_m:
            continue
        # Side-extension limit: the polygon's extension PAST the
        # runway edge on each side must be less than the runway
        # width.  Catches both:
        #   - HECA's "New Taxiway 1" envelope (8.7 m past each edge,
        #     well under 60 m runway width — accepted).
        #   - CYXY's "North of 02" shoulder (18 m past the north
        #     edge, under 22.9 m runway width — accepted).
        # Rejects:
        #   - CYXY's "Apron 1 and E" (extends 325 m past the east
        #     edge of 14R/32L, way over the 45.7 m runway width).
        north_ext = max(0.0, n_max - runway_half)
        south_ext = max(0.0, -runway_half - n_min)
        if north_ext > runway_width or south_ext > runway_width:
            continue
        absorbed.append(idx)
        if n_min < new_left:
            new_left = n_min
        if n_max > new_right:
            new_right = n_max
    return (new_left, new_right, absorbed)


def _detect_runway_border_strip_shoulders(
        runway,
        to_m,
        border_lines,
        *,
        edge_tol_m: float,
        sample_step_m: float,
        min_strip_cover_m: float,
        min_side_cover_m: float,
        min_w: float,
        max_w: float,
        ) -> "tuple[float, float] | None":
    """Derive per-side runway shoulder widths from wide draped ``.lin``
    border strips traced along the runway's own outline.

    Construction style (KBNA): the runway ships as exact-runway-width
    draped ``.pol`` pieces plus a wide ``.lin`` border traced ON the
    ``.pol`` outline.  X-Plane centers a line texture on its path, so
    the strip's outer half renders as pavement past the ``.pol`` edge —
    that outer half IS the author's shoulder, and the resource-declared
    strip width states the shoulder width exactly: ``width / 2`` per
    side (KBNA 13/31: 24 m border ⇒ 12 m shoulder; 02C/20C: 20 m ⇒
    10 m).

    ``border_lines``: ``[(line, width_m)]`` with ``line`` a meter-space
    ``LineString`` (the pipeline's ``dsf_border_line_candidates``).

    A strip contributes to a side when at least ``min_strip_cover_m``
    of its arc length runs within ``edge_tol_m`` of that runway edge
    (inside the runway's longitudinal extent ± 20 m) — taxiway borders
    that merely cross the runway at exits stay below the floor.  The
    RUNWAY qualifies when either side's contributing strips jointly
    cover at least ``min_side_cover_m`` of edge length.

    Shoulders are a PER-RUNWAY, SYMMETRIC property (user 2026-07-17):
    real-world shoulders run both sides, and the side without border
    evidence is simply the side where abutting taxiway/apron ``.pol``
    pavement covers the shoulder band (KBNA 13/31's right edge).  The
    caller injects the returned width into the apt.dat coded-shoulder
    path ("this runway HAS 12 m shoulders"), which widens
    symmetrically and cuts junctions at the shoulder edge — the
    OMAA-proven model.

    Returns the shoulder width in meters (the arc-length-weighted
    median of all contributing strips' ``width / 2`` across both
    sides, clamped to ``[min_w, max_w]``), or ``None`` when no side
    qualifies.
    """
    ax, ay = to_m(runway.lon_a, runway.lat_a)
    bx, by = to_m(runway.lon_b, runway.lat_b)
    L = math.hypot(bx - ax, by - ay)
    if L < 50.0:
        return None
    ux, uy = (bx - ax) / L, (by - ay) / L
    nx, ny = -uy, ux
    half = runway.width_m / 2.0

    # side key: -1.0 = left (n < 0), +1.0 = right (n > 0).
    cover_by_side: dict[float, list[tuple[float, float]]] = {
        -1.0: [], 1.0: []}
    for line, width_m in border_lines:
        if line is None or line.is_empty or width_m is None:
            continue
        try:
            length = float(line.length)
        except _GEOM_EXC:
            continue
        if length < min_strip_cover_m:
            continue
        n_samples = max(2, int(length / sample_step_m))
        on_edge = {-1.0: 0, 1.0: 0}
        try:
            for k in range(n_samples):
                p = line.interpolate((k + 0.5) / n_samples,
                                     normalized=True)
                u = (p.x - ax) * ux + (p.y - ay) * uy
                if u < -20.0 or u > L + 20.0:
                    continue
                n = (p.x - ax) * nx + (p.y - ay) * ny
                if abs(n + half) <= edge_tol_m:
                    on_edge[-1.0] += 1
                elif abs(n - half) <= edge_tol_m:
                    on_edge[1.0] += 1
        except _GEOM_EXC:
            continue
        step = length / n_samples
        for side in (-1.0, 1.0):
            cover_m = on_edge[side] * step
            if cover_m >= min_strip_cover_m:
                cover_by_side[side].append((cover_m, float(width_m)))

    if not any(
            sum(c for c, _w in contributions) >= min_side_cover_m
            for contributions in cover_by_side.values()):
        return None
    # Arc-length-weighted median of ALL contributing strips'
    # half-widths (both sides pooled — the shoulder is one per-runway
    # width).
    contributions = cover_by_side[-1.0] + cover_by_side[1.0]
    total_cover = sum(c for c, _w in contributions)
    if total_cover <= 0.0:
        return None
    ranked = sorted(((w / 2.0, c) for c, w in contributions))
    accumulated = 0.0
    shoulder_w = ranked[-1][0]
    for half_width, cover_m in ranked:
        accumulated += cover_m
        if accumulated >= 0.5 * total_cover:
            shoulder_w = half_width
            break
    return min(max_w, max(min_w, shoulder_w))


def _detect_runway_shoulder_extent(
        runway,
        to_m,
        pav_union,
        apt_only_union,
        station_m: float,
        step_m: float,
        min_w: float,
        max_w: float,
        min_coverage: float,
        max_apt_frac: float,
        ) -> "tuple[float, float] | None":
    """Measure DSF-carried shoulder strips along a runway's edges.

    Walks perpendicular outward from each runway edge through
    ``pav_union`` (the final apt.dat ⊕ DSF source union) at stations
    every ``station_m`` along the centerline, recording the contiguous
    pavement extent past the edge.  A side is a shoulder when:

      1. Coverage: ≥ ``min_coverage`` of stations have pavement
         immediately past the edge (within ``step_m``) — "consistent
         along the runway".
      2. Width: the 25th-percentile extent is ≥ ``min_w`` (filters
         union-simplify noise).  The widening width is the
         75th-percentile extent clamped to [min_w, max_w] — wide-biased
         on purpose: the graded area beside a runway is the runway
         strip the standards require smooth anyway, while under-
         covering leaves on-source residue slivers that re-emit as
         apron pieces hugging the runway (the bug this pass kills).
         Stations where the walk runs past ``max_w`` are exits /
         taxiway connections; the junction shapes own that pavement
         and the percentile clamp ignores them.
      3. Attribution: < ``max_apt_frac`` of the strip's mid-points lie
         on ``apt_only_union`` — row-110-carried shoulders belong to
         the established passes (whole-polygon absorption / the
         INTERSECTION_PROX_M junction-cut budget), only the DSF gap
         (KPHL StarSim's whole-airport asphalt.pol ring) fires here.

    Returns ``(new_left, new_right)`` perpendicular offsets from the
    centerline (a non-qualifying side keeps ±half), or ``None`` when
    neither side qualifies.
    """
    try:
        from shapely.prepared import prep
    except ImportError:                    # pragma: no cover
        return None
    if pav_union is None or pav_union.is_empty:
        return None
    ax, ay = to_m(runway.lon_a, runway.lat_a)
    bx, by = to_m(runway.lon_b, runway.lat_b)
    L = math.hypot(bx - ax, by - ay)
    if L < 2.0 * station_m:
        return None
    ux, uy = (bx - ax) / L, (by - ay) / L
    nx, ny = -uy, ux
    half = runway.width_m / 2.0
    walk_cap = max_w + step_m            # one step past max = "open"
    try:
        pav_prep = prep(pav_union)
    except _GEOM_EXC:
        return None
    apt_prep = None
    if apt_only_union is not None and not apt_only_union.is_empty:
        try:
            apt_prep = prep(apt_only_union)
        except _GEOM_EXC:
            apt_prep = None

    n_st = int(L / station_m)
    new_left = -half
    new_right = half
    qualified = False
    for side in (1.0, -1.0):
        extents: list[float] = []
        for k in range(1, n_st):
            sx = ax + ux * (k * station_m)
            sy = ay + uy * (k * station_m)
            ext = 0.0
            t = step_m
            while t <= walk_cap:
                if pav_prep.contains(Point(
                        sx + side * nx * (half + t),
                        sy + side * ny * (half + t))):
                    ext = t
                    t += step_m
                else:
                    break
            extents.append(ext)
        if not extents:
            continue
        sv = sorted(extents)
        n = len(sv)
        coverage = sum(1 for e in extents if e >= step_m) / n
        if coverage < min_coverage:
            continue
        q1 = sv[n // 4]
        if q1 < min_w:
            continue
        width = min(max_w, sv[(3 * n) // 4])
        if width < min_w:
            continue
        if apt_prep is not None:
            on_apt = sum(
                1 for k in range(1, n_st)
                if apt_prep.contains(Point(
                    ax + ux * (k * station_m)
                    + side * nx * (half + 0.5 * width),
                    ay + uy * (k * station_m)
                    + side * ny * (half + 0.5 * width))))
            if on_apt / n > max_apt_frac:
                continue
        qualified = True
        if side > 0:
            new_right = half + width
        else:
            new_left = -(half + width)
    if not qualified:
        return None
    return (new_left, new_right)


def _widen_runway_rect(
        runway,
        anchor: "tuple[float, float]",
        new_left: float,
        new_right: float,
        to_m,
        ) -> "Polygon | None":
    """Widen (and, when asymmetric, recentre) a runway's apt.dat record
    so its rect spans perpendicular offsets ``[new_left, new_right]``
    from the current centerline.

    Mutates ``runway.lat_a/lon_a/lat_b/lon_b/width_m`` in place and
    returns the rebuilt 4-corner rect, so downstream CIFP segmenting
    (which reads ``width_m``) picks up the new width.  Returns ``None``
    and leaves the record untouched when the rebuild degenerates.

    The perpendicular recentre offset is applied back through the
    inverse of the meter projection (anchored at ``anchor`` =
    ``(lat0, lon0)`` with ``cos(lat0)``), matching ``layout._projection``.
    """
    new_width = new_right - new_left
    offset = 0.5 * (new_left + new_right)
    ax, ay = to_m(runway.lon_a, runway.lat_a)
    bx, by = to_m(runway.lon_b, runway.lat_b)
    udx, udy = bx - ax, by - ay
    L = math.hypot(udx, udy)
    if L < 1.0:
        return None
    ux, uy = udx / L, udy / L
    nx, ny = -uy, ux
    lat0, _lon0 = anchor
    cos0 = math.cos(math.radians(lat0))
    d_lat = math.degrees(ny * offset / R_EARTH)
    d_lon = (math.degrees(nx * offset / (R_EARTH * cos0))
             if cos0 > 1e-9 else 0.0)
    saved = (runway.lat_a, runway.lon_a, runway.lat_b,
             runway.lon_b, runway.width_m)
    if abs(offset) > 0.05:
        runway.lat_a += d_lat
        runway.lon_a += d_lon
        runway.lat_b += d_lat
        runway.lon_b += d_lon
    runway.width_m = new_width
    rect = _runway_rect_m(runway, to_m)
    if rect.is_empty:
        (runway.lat_a, runway.lon_a, runway.lat_b,
         runway.lon_b, runway.width_m) = saved
        return None
    return rect
