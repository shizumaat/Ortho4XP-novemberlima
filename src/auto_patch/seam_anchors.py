"""Insert seam vertices at integer lat/lon tile-boundary lines.

For each pavement shape whose polygon's exterior boundary crosses an
integer latitude or longitude line within the airport footprint, this
module:

  1. Inserts new ring vertices at the crossing points (deterministic
     from polygon geometry alone, independent of which tile is being
     built — both tile builds produce the same vertices).
  2. Converts sloped 4-corner rects to ``node_altitudes`` representation
     so each vertex (including seam crossings) carries its own altitude.
  3. Records the seam-vertex bucket keys in
     ``layout._seam_anchor_keys`` for the Phase-2 elevation solver to
     HARD-anchor against ``dem.alt_strict``.

Cross-tile parity: each tile build runs over the same pavement
geometry, finds the same cut lines, and inserts vertices at the same
(x, y) positions.  Phase-2 then samples the same SRTM pixel at each
seam vertex (SRTM .hgt overlap row), so all tile builds compute the
same altitude.  ``cut_layout_at_tile_boundaries`` then keeps only the
shape pieces falling in the current tile.
"""
from __future__ import annotations

import math

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from .layout import (
    BuiltShape, PavementLayout, R_EARTH, SHARED_VERTEX_TOL_M,
    ROLE_APRON, ROLE_BOUNDARY, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_BUILDING, ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
    ROLE_GROUNDSIDE_PAVEMENT, vertex_bucket,
    corner_alts_from_high_low,
)

__all__ = ["split_pavement_at_seams", "apply_seam_dem_anchors"]

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

# Shape roles whose polygons participate in seam-splitting.
# Tile-cut bridges are intentionally excluded — they're emitted later
# (in tile_cut.py) for backwards-compat with bridge-based seam pinning;
# once the new seam-anchor pass proves out we can drop bridges.
_SEAM_SPLIT_ROLES = {
    ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_APRON, ROLE_BUILDING,
    ROLE_JUNCTION, ROLE_BOUNDARY, ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
    ROLE_GROUNDSIDE_PAVEMENT,
}

# Sloping taxi rect roles — per user 2026-05-19 these stay 4-corner
# through seam-crossings (split-at-seam, not insert-vertex-at-seam).
# Runways are intentionally NOT in this set: the runway seam pipeline
# converts to node_altitudes to preserve per-vertex precision at
# DEM-noisy tile boundaries (user 2026-05-13).
_TAXI_RECT_ROLES = {
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR,
}

# Sub-meter tolerance for skipping insertions at existing vertices.
_EDGE_T_TOL = 1e-4


def _bucket_key(x: float, y: float) -> tuple[int, int]:
    """Bucket key matching ``elevation._corner_elevation_bucket`` so
    Phase-2 can look up seam anchors directly against the solver's
    vertex graph.  Delegates to ``layout.vertex_bucket`` (the single
    source of truth) so the scheme can't diverge."""
    return vertex_bucket(x, y)


def _split_ring_at_seam(ring, seam_line):
    """Split a 4-corner ring at one seam line into 2 4-corner rings.

    Returns ``[ring_a, ring_b]`` when the seam cleanly intersects 2
    non-adjacent edges, ``None`` otherwise (no crossing, single
    crossing, or seam clips a corner — fall back to insert-in-place
    in those cases since a clean 4-corner split isn't available).
    """
    n = len(ring)
    if n != 4:
        return None
    intersections: list[tuple[int, float, tuple[float, float]]] = []
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        edge = LineString([(ax, ay), (bx, by)])
        try:
            inter = edge.intersection(seam_line)
        except _GEOM_EXC:
            continue
        if inter.is_empty or inter.geom_type != "Point":
            continue
        dx = bx - ax
        dy = by - ay
        L2 = dx * dx + dy * dy
        if L2 < 1e-9:
            continue
        t = ((inter.x - ax) * dx + (inter.y - ay) * dy) / L2
        if t <= _EDGE_T_TOL or t >= 1.0 - _EDGE_T_TOL:
            continue
        intersections.append((i, t, (inter.x, inter.y)))
    if len(intersections) != 2:
        return None
    intersections.sort(key=lambda r: r[0])
    (ea, _ta, pa), (eb, _tb, pb) = intersections
    # The seam must cross 2 NON-ADJACENT edges (opposite sides of
    # the rect) for a clean 2 × 4-corner split.  Adjacent-edge
    # crossings clip a corner off and produce a triangle + pentagon
    # — not the canonical 4-corner form.
    if (eb - ea) % n != 2:
        return None
    # Build sub-rect A: ring[0..ea], pa, pb, ring[eb+1..n-1]
    ring_a: list[tuple[float, float]] = []
    for i in range(ea + 1):
        ring_a.append(ring[i])
    ring_a.append(pa)
    ring_a.append(pb)
    for i in range(eb + 1, n):
        ring_a.append(ring[i])
    # Build sub-rect B: pa, ring[ea+1..eb], pb
    ring_b: list[tuple[float, float]] = [pa]
    for i in range(ea + 1, eb + 1):
        ring_b.append(ring[i])
    ring_b.append(pb)
    if len(ring_a) != 4 or len(ring_b) != 4:
        return None
    return [ring_a, ring_b]


def _split_taxi_rect_at_seams(
        shape: BuiltShape,
        cut_lines: list[LineString],
        anchor_keys: set[tuple[int, int]],
        layout: PavementLayout,
) -> list[BuiltShape] | None:
    """Replace a 4-corner taxi rect with sub-rects produced by
    splitting at each seam crossing, preserving 4-corner geometry.

    Per user 2026-05-19: a taxi rect's slope rendering depends on
    the canonical 4-corner [HI-LEFT, LO-LEFT, LO-RIGHT, HI-RIGHT]
    ring convention (or equivalent CCW rotation), and EVERY
    downstream pass that operates on rects assumes 4 corners
    (absorption, junction-rule tests,
    sloping-edge identification).  Inserting seam vertices into a
    sloping edge breaks that assumption: even a vertex collinear
    with its neighbours produces 5- or 6-corner rings that the
    rest of the pipeline rejects.

    Splitting the rect at each seam produces N + 1 sub-rects, each
    still 4-corner.  Sub-rect altitudes are intentionally left
    unset — the elevation solver's first pass fills them after
    HARD-anchoring the seam corners (recorded in ``anchor_keys``)
    to ``dem.alt_strict``.

    Returns ``None`` if any seam crossing doesn't admit a clean
    2 × 4-corner split (e.g. a seam clips a single corner); the
    caller then falls back to the legacy insert-vertices path so
    no shape is dropped.
    """
    if shape.polygon is None or shape.polygon.is_empty:
        return None
    ring = list(shape.polygon.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) != 4:
        return None

    rings: list[list[tuple[float, float]]] = [
        [(float(x), float(y)) for x, y in ring]]
    new_corner_pts: list[tuple[float, float]] = []
    original_pt_keys = {_bucket_key(x, y) for x, y in ring}
    for seam_line in cut_lines:
        next_rings: list[list[tuple[float, float]]] = []
        for r in rings:
            poly_r = Polygon(r)
            try:
                if not poly_r.boundary.intersects(seam_line):
                    next_rings.append(r)
                    continue
            except _GEOM_EXC:
                next_rings.append(r)
                continue
            split = _split_ring_at_seam(r, seam_line)
            if split is None:
                # Couldn't split cleanly — abort and let caller fall
                # back to insert-in-place.
                return None
            next_rings.extend(split)
            for sub in split:
                for x, y in sub:
                    if _bucket_key(x, y) in original_pt_keys:
                        continue
                    new_corner_pts.append((x, y))
        rings = next_rings

    if len(rings) < 2:
        return None

    # Route every new corner through the canonical-point registry
    # (so adjacent shapes see the same coords) and record bucket
    # keys for the solver's HARD-anchor pass.
    registry = getattr(layout, "canonical_points", None)

    def _canon(x: float, y: float) -> tuple[float, float]:
        if registry is None:
            return (x, y)
        return registry.get_or_add(float(x), float(y))

    for x, y in new_corner_pts:
        cx, cy = _canon(x, y)
        anchor_keys.add(_bucket_key(cx, cy))

    out: list[BuiltShape] = []
    import copy
    for r in rings:
        canon_ring = [_canon(x, y) for x, y in r]
        # Drop degenerate rings (sub-tol vertices).
        canon_ring_dedup: list[tuple[float, float]] = []
        for pt in canon_ring:
            if not canon_ring_dedup or canon_ring_dedup[-1] != pt:
                canon_ring_dedup.append(pt)
        if (len(canon_ring_dedup) >= 2
                and canon_ring_dedup[0] == canon_ring_dedup[-1]):
            canon_ring_dedup = canon_ring_dedup[:-1]
        if len(canon_ring_dedup) != 4:
            return None
        new_s = copy.copy(shape)
        new_s.polygon = Polygon(canon_ring_dedup + [canon_ring_dedup[0]])
        new_s.altitude = None
        new_s.altitude_high = None
        new_s.altitude_low = None
        new_s.node_altitudes = None
        # source_axis inherited via copy.copy — both sub-rects share
        # the parent's axis direction, which is what
        # ``_canonicalise_rect`` needs at writeback time.
        out.append(new_s)
    return out


def split_pavement_at_seams(layout: PavementLayout) -> int:
    """Insert seam vertices and convert sloped rects to ``node_altitudes``.

    Records seam-vertex bucket keys on ``layout._seam_anchor_keys``.

    Returns the net change in shape count (always 0 today — this pass
    modifies existing shapes in place rather than splitting them).
    """
    layout._seam_anchor_keys = set()  # type: ignore[attr-defined]

    if not layout.shapes or layout.anchor is None:
        return 0
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))

    # Build footprint of all pavement shapes to identify which
    # integer lines pass through the airport.
    pav_polys = [s.polygon for s in layout.shapes
                 if s.polygon is not None and not s.polygon.is_empty]
    if not pav_polys:
        return 0
    try:
        pav_union = unary_union(pav_polys)
    except _GEOM_EXC:
        return 0
    minx, miny, maxx, maxy = pav_union.bounds
    min_lat = lat0 + math.degrees(miny / R_EARTH)
    max_lat = lat0 + math.degrees(maxy / R_EARTH)
    min_lon = lon0 + math.degrees(minx / (R_EARTH * cos0))
    max_lon = lon0 + math.degrees(maxx / (R_EARTH * cos0))

    cut_lines: list[LineString] = []
    for lat_int in range(int(math.ceil(min_lat)),
                          int(math.floor(max_lat)) + 1):
        if min_lat < lat_int < max_lat:
            y_int = math.radians(lat_int - lat0) * R_EARTH
            cut_lines.append(LineString([
                (minx - 100.0, y_int), (maxx + 100.0, y_int)]))
    for lon_int in range(int(math.ceil(min_lon)),
                          int(math.floor(max_lon)) + 1):
        if min_lon < lon_int < max_lon:
            x_int = math.radians(lon_int - lon0) * R_EARTH * cos0
            cut_lines.append(LineString([
                (x_int, miny - 100.0), (x_int, maxy + 100.0)]))
    # Stash the seam cut-lines for the elevation solver's network-profile
    # field, which adds a HARD anchor where each centerline crosses a seam
    # (config.SEAM_FIELD_ANCHORS) so the route grades smoothly to the seam
    # DEM value instead of stepping to it.
    layout._seam_cut_lines = list(cut_lines)  # type: ignore[attr-defined]
    if not cut_lines:
        return 0

    anchor_keys: set[tuple[int, int]] = set()
    # Per user 2026-05-19: don't add vertices to a sloping taxi rect
    # — every downstream pass (absorption, junction-rule tests,
    # sloping-edge identification) assumes a canonical 4-corner ring and
    # breaks when extra vertices appear on a sloping edge.  Instead,
    # for taxi rect roles, SPLIT the rect at each seam into 2
    # 4-corner sub-rects so each sub-rect remains 4-corner.  Altitude
    # handling: leave altitudes unset on the sub-rects; the elevation
    # solver fills them after seeding seam corners from DEM (the
    # seam keys recorded here drive ``_seed_elevations``' HARD-anchor
    # pass).
    new_shapes_extra: list[BuiltShape] = []
    indices_to_drop: list[int] = []
    for i, shape in enumerate(layout.shapes):
        if shape.role not in _SEAM_SPLIT_ROLES:
            continue
        if shape.polygon is None or shape.polygon.is_empty:
            continue
        # Quick reject: skip if no boundary crossing.
        try:
            if not shape.polygon.boundary.intersects(
                    unary_union(cut_lines)):
                continue
        except _GEOM_EXC:
            continue
        if shape.role in _TAXI_RECT_ROLES:
            sub_rects = _split_taxi_rect_at_seams(
                shape, cut_lines, anchor_keys, layout)
            if sub_rects is not None and len(sub_rects) >= 2:
                indices_to_drop.append(i)
                new_shapes_extra.extend(sub_rects)
            # Per user 2026-05-23: when the clean 2×4-corner split is NOT
            # available (the rect only grazes / ENDS at the seam — e.g.
            # SPLP taxiway A, whose oblique end short-edge straddles
            # lon=-77 so the seam clips a single corner), do NOT fall
            # back to inserting seam vertices in place: that converts the
            # whole sloping rect to node_altitudes and loses the
            # flat-cross-section guarantee (a long taxiway turns into a
            # tilted node_altitudes polygon).  Leave it a 4-corner rect;
            # the tile-cut slice (``_clip_sloping_rect_piece``) clips the
            # end to a clean perpendicular edge clear of the seam and
            # emits a small terrain-pinned wedge there, keeping the bulk
            # a rect.
            continue
        new_shape = _insert_seam_vertices(shape, cut_lines, anchor_keys)
        if new_shape is not None:
            layout.shapes[i] = new_shape
    if indices_to_drop:
        keep_set = set(range(len(layout.shapes))) - set(indices_to_drop)
        layout.shapes = [layout.shapes[i] for i in sorted(keep_set)]
        layout.shapes.extend(new_shapes_extra)

    # Per user 2026-05-13: when ANY sub-rect of a runway has been
    # seam-converted to node_altitudes, ALL sub-rects of that same
    # runway need node_altitudes too — otherwise altitude_high/low's
    # planar-surface assumption forces averaging across adjacent
    # sub-rects' shared corners that no longer agree (the
    # seam-crossing sub-rect has its H corner pinned to DEM while
    # the neighbour sub-rect has its L corner at CIFP).  Convert the
    # entire runway chain to node_altitudes so each corner carries
    # its own altitude through the solver.
    seam_runway_refs: set[str] = set()
    for shape in layout.shapes:
        if (shape.role == ROLE_RUNWAY
                and shape.node_altitudes
                and shape.ref):
            seam_runway_refs.add(shape.ref)
    if seam_runway_refs:
        for shape in layout.shapes:
            if shape.role != ROLE_RUNWAY:
                continue
            if shape.ref not in seam_runway_refs:
                continue
            if shape.node_altitudes:
                continue
            if shape.polygon is None or shape.polygon.is_empty:
                continue
            ring = list(shape.polygon.exterior.coords)
            if ring and ring[0] == ring[-1]:
                ring = ring[:-1]
            if (len(ring) == 4
                    and shape.altitude_high is not None
                    and shape.altitude_low is not None):
                alts = corner_alts_from_high_low(
                    shape.altitude_high, shape.altitude_low)
                shape.node_altitudes = alts + [alts[0]]
                shape.altitude_high = None
                shape.altitude_low = None
            elif shape.altitude is not None:
                alts = [float(shape.altitude)] * len(ring)
                shape.node_altitudes = alts + [alts[0]]
                shape.altitude = None

    layout._seam_anchor_keys = anchor_keys  # type: ignore[attr-defined]
    return 0


def _insert_seam_vertices(
        shape: BuiltShape,
        cut_lines: list[LineString],
        anchor_keys: set[tuple[int, int]]) -> BuiltShape | None:
    """Insert intersection points of cut_lines with the shape's
    exterior ring, return a new BuiltShape with seam vertices added.

    Inserted-vertex altitudes are interpolated from the bracketing
    original-vertex altitudes; Phase-2 then overwrites them with
    ``dem.alt_strict`` at the recorded anchor keys.  Existing-vertex
    altitudes are preserved (sloped rect → unpacked via [H, L, L, H]
    convention; flat → broadcast; per-vertex → carried through).

    When the shape arrives with no altitude representation at all —
    the corner-clip fallback path from
    ``_split_taxi_rect_at_seams`` for a taxi rect awaiting solver
    assignment — geometric vertices and anchor keys are still
    inserted/recorded, but ``node_altitudes`` is left ``None`` on
    the returned shape.  The solver assigns altitudes downstream.
    """
    poly = shape.polygon
    ring = list(poly.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    n_orig = len(ring)
    if n_orig < 3:
        return None

    # Determine the original per-vertex altitudes.  ``old_alts`` is
    # ``None`` when the shape has no altitude representation yet —
    # taxi rects awaiting solver assignment (see
    # ``_split_taxi_rect_at_seams`` docstring) that fell through to
    # this function via the corner-clip fallback path.  In that case
    # we still insert geometric seam vertices and record anchor keys
    # for the solver's HARD-anchor pass; we just don't fabricate
    # altitudes (the previous ``[0.0] * n_orig`` placeholder produced
    # silent sea-level cliffs whenever Phase 2 didn't cover the
    # affected vertices — e.g. the MMOX boundary-bridge 1000 m drop).
    old_alts: list[float] | None
    if shape.node_altitudes:
        old_alts = list(shape.node_altitudes[:n_orig])
        if len(old_alts) < n_orig:
            old_alts += [old_alts[-1]] * (n_orig - len(old_alts))
    elif (shape.altitude_high is not None
            and shape.altitude_low is not None
            and n_orig == 4):
        old_alts = corner_alts_from_high_low(
            shape.altitude_high, shape.altitude_low)
    elif shape.altitude is not None:
        old_alts = [float(shape.altitude)] * n_orig
    else:
        old_alts = None

    # Walk each edge, find intersections with each cut line, insert in
    # parametric order.  Track which inserted vertices are seam-anchored
    # AND which existing vertices sit on a seam.
    new_ring: list[tuple[float, float]] = []
    new_alts: list[float] | None = [] if old_alts is not None else None
    inserted_idxs: list[int] = []
    existing_on_seam: list[int] = []  # indices in new_ring of original
                                       # ring vertices that lie on a seam

    for i in range(n_orig):
        p1 = ring[i]
        p2 = ring[(i + 1) % n_orig]
        new_ring.append(p1)
        if new_alts is not None and old_alts is not None:
            new_alts.append(old_alts[i])
        edge = LineString([p1, p2])
        edge_len = edge.length
        if edge_len < 1e-6:
            continue
        ips: list[tuple[float, tuple[float, float]]] = []
        for cl in cut_lines:
            try:
                inter = edge.intersection(cl)
            except _GEOM_EXC:
                continue
            if inter.is_empty:
                continue
            if inter.geom_type == "Point":
                pt = (inter.x, inter.y)
            else:
                continue
            dx = pt[0] - p1[0]
            dy = pt[1] - p1[1]
            t = math.hypot(dx, dy) / edge_len
            if t < _EDGE_T_TOL:
                # Cut line passes through p1 (current vertex).
                anchor_keys.add(_bucket_key(p1[0], p1[1]))
                # Record it for the shape-level conversion check.
                existing_on_seam.append(len(new_ring) - 1)
                continue
            if t > 1.0 - _EDGE_T_TOL:
                # Cut line passes through p2 (handled next iteration).
                anchor_keys.add(_bucket_key(p2[0], p2[1]))
                continue
            ips.append((t, pt))
        ips.sort(key=lambda x: x[0])
        for t, pt in ips:
            inserted_idxs.append(len(new_ring))
            new_ring.append(pt)
            if new_alts is not None and old_alts is not None:
                a1 = old_alts[i]
                a2 = old_alts[(i + 1) % n_orig]
                new_alts.append(a1 + t * (a2 - a1))
            anchor_keys.add(_bucket_key(pt[0], pt[1]))

    if not inserted_idxs and not existing_on_seam:
        return None

    # Build the new BuiltShape.  Switch to node_altitudes representation
    # since the original [H, L, L, H] or flat scheme no longer applies
    # cleanly with N+ vertices.
    try:
        new_poly = Polygon(new_ring)
        if not new_poly.is_valid:
            new_poly = new_poly.buffer(0)
            if (new_poly.geom_type != "Polygon"
                    or new_poly.is_empty):
                return None
    except _GEOM_EXC:
        return None
    # node_altitudes carries the CLOSING repeat per layout convention.
    # When the input had no altitude rep (taxi-rect awaiting solver),
    # leave node_altitudes=None so the solver assigns; the geometric
    # vertices and anchor keys recorded above are still enough for
    # cross-tile parity and HARD-anchoring.
    closed_alts: list[float] | None
    if new_alts is not None:
        closed_alts = new_alts + [new_alts[0]]
    else:
        closed_alts = None
    # If the source was a sloped 4-corner rect without an explicit
    # source_axis (typical of runway shapes built from CIFP), derive
    # one from the H→L pair so downstream Stage A regrade can project
    # vertices onto the runway centerline.  For non-rect shapes the
    # original source_axis (if any) is carried through.
    derived_axis = shape.source_axis
    if (derived_axis is None
            and shape.altitude_high is not None
            and shape.altitude_low is not None
            and n_orig == 4):
        c0, c1, c2, c3 = ring[0], ring[1], ring[2], ring[3]
        h_mid = (0.5 * (c0[0] + c3[0]), 0.5 * (c0[1] + c3[1]))
        l_mid = (0.5 * (c1[0] + c2[0]), 0.5 * (c1[1] + c2[1]))
        if (h_mid[0] - l_mid[0]) ** 2 + (h_mid[1] - l_mid[1]) ** 2 > 1e-6:
            derived_axis = LineString([h_mid, l_mid])

    new_shape = BuiltShape(
        polygon=new_poly,
        role=shape.role,
        ref=shape.ref,
        source_axis=derived_axis,
        altitude=None,
        altitude_high=None,
        altitude_low=None,
        node_altitudes=closed_alts,
        is_bridge=shape.is_bridge,
        # A seam-split de-seg ring is still a de-seg ring: every
        # from_single_poly consumer (join-anchor boundary projection,
        # ring corner reads, per-station profile validation) must keep
        # seeing it, or a seam-crossing runway builds half-legacy.
        from_single_poly=shape.from_single_poly,
    )
    return new_shape


# Clamp grade for AIRSIDE seam pins: the TAXI cap, so every pin is
# REACHABLE from the runway by construction (straight-line distance is a
# lower bound on any taxi path, so ``runway_e − cap·d_straight`` is the
# highest floor any path could require — a raw-DEM pin below it made the
# pin↔runway chain infeasible by inches and the final GS midpointed the
# conflict into a V-notch: the SPLP seam dips).  The boundary RIBBON keeps
# its own separate 3 % rule.
SEAM_CLAMP_GRADE = 0.015

# Roles whose seam pins take the runway clamp floor.  Boundary /
# groundside / feature shapes keep the raw-DEM pin (they follow terrain
# by design); RUNWAY keeps profile authority via
# ``redistribute_runway_profile``.  Shared by every seam-pin writer
# (``apply_seam_dem_anchors``, ``tile_cut._pin_slice_edge_to_dem``, and
# the solver's seam hard-anchor block in ``solver_primitives``).
SEAM_CLAMP_ROLES = frozenset({
    "apron", "junction", "service_junction",
    "primary_parallel", "secondary_parallel", "stub",
    "cross_connector", "runway_crossing",
})


def runway_clamp_floor(layout, x: float, y: float):
    """``max`` over CIFP-profiled runways of ``runway_elev_at_nearest_point −
    SEAM_CLAMP_GRADE·d`` — the deterministic cross-tile floor for AIRSIDE
    seam pins (both tiles share the same runways + profile, so both compute
    the same value without seeing each other).  ``None`` when no runway has
    an elevation yet.

    Post-redistribute callers (tile_cut pins, the solver's seam block)
    evaluate the PERSISTED redistributed profiles — axis projection +
    sample-list interpolation — NEVER the surviving runway shapes: after
    ``cut_layout_at_tile_boundaries`` each tile build keeps only ITS
    pieces, so a shape walk computes different floors on the two sides of
    a seam (SPLP: the −77 build lifted a junction pin to 65.7 against a
    runway the −78 build had dropped, whose twin pin stayed at 62.4 — a
    3.3 m step across the 10 m gap).

    The profile floor is the MAX over sampled axis positions of
    ``profile(t) − SEAM_CLAMP_GRADE · distance(P, cross_section(t))``,
    with the runway's half-width credited (the profile value spans the
    full width).  The earlier nearest-axis-point-only floor guaranteed
    the pin within cap of ONE profile point but let the straight chord
    to a runway-welded node farther ALONG the axis read over cap — the
    L1-vs-L2 gap (SPLP 2026-07-05: junction seam pin 62.9 vs the
    runway-edge weld at 64.3, a both-hard 2.17 % pair no projection can
    fix).  With the cross-section distance the triangle inequality
    bounds the pin against EVERY point of the runway surface in both
    directions.  Shape walk remains as the fallback for pre-redistribute
    callers (``apply_seam_dem_anchors``) and refs with no CIFP state.
    """
    best = None
    profiles = getattr(layout, "_runway_redistributed_profiles", None)
    if profiles:
        from .runway_redistribute import _interp_profile
        for p in profiles.values():
            ax_x, ax_y = p['axis_a']
            dx, dy = p['axis_d']
            axis_len = math.sqrt(p['axis_len2'])
            if axis_len < 1.0:
                continue
            half_width = float(p.get('half_width_m', 0.0))
            # P in axis coordinates: s = along-axis metres, r = lateral.
            ux, uy = dx / axis_len, dy / axis_len
            s = (x - ax_x) * ux + (y - ax_y) * uy
            r = abs(-(x - ax_x) * uy + (y - ax_y) * ux)
            lateral = max(0.0, r - half_width)
            step = 2.0
            n_samples = max(2, int(axis_len / step) + 1)
            for k in range(n_samples + 1):
                u = min(axis_len, k * step)
                d = math.hypot(abs(s - u), lateral)
                e = _interp_profile(p['fractions'], p['elevs'],
                                    u / axis_len)
                f = float(e) - SEAM_CLAMP_GRADE * d
                if best is None or f > best:
                    best = f
        return best
    from shapely.geometry import Point as _P
    from shapely.ops import nearest_points as _np
    from .pavement.runways import _sample_runway_segment_elev
    from .layout import ROLE_RUNWAY
    p = _P(x, y)
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY or s.polygon is None \
                or s.polygon.is_empty:
            continue
        try:
            q = _np(s.polygon, p)[0]
            d = p.distance(q)
            e = _sample_runway_segment_elev(s, q.x, q.y)
        except _GEOM_EXC:
            continue
        if e is None:
            continue
        f = float(e) - SEAM_CLAMP_GRADE * d
        if best is None or f > best:
            best = f
    return best


def apply_seam_dem_anchors(
    layout: PavementLayout,
    dem,
    tile_lat: int,
    tile_lon: int,
) -> int:
    """Sample DEM at every seam vertex and overwrite the placeholder
    interp altitude in ``node_altitudes`` with the DEM value.

    Uses the SMOOTHED DEM (``_sample_dem`` → ``dem.alt``, the same sampler the
    rest of the build uses), NEVER raw ``alt_strict`` (user 2026-06-28).  Raw
    ``alt_strict`` returns nodata (-32768) AT the tile edge (the seam sits on the
    DEM's coverage boundary), so the pin below was SKIPPED there — leaving the
    seam vertex unpinned at its solved apron/junction level on the tile whose edge
    the seam falls on, while the adjacent tile (seam in-bounds) pinned it: a
    cross-tile cliff (SPLP -77 left a seam apron at 74.7 vs -78's 72.3, DEM flat
    ~72.3).  The smoothed sampler interpolates a real value at the edge, so BOTH
    tiles pin the same seam point to the same value → continuity.

    Must be called AFTER ``split_pavement_at_seams`` (which populates
    ``layout._seam_anchor_keys``) and BEFORE the elevation solver.

    Returns the number of vertices updated.
    """
    from .elevation import _sample_dem
    anchor_keys = getattr(layout, "_seam_anchor_keys", None)
    if not anchor_keys:
        return 0
    if dem is None:
        return 0
    nodata = getattr(dem, "nodata", -32768)

    # AIRSIDE seam pins are RUNWAY-CLAMPED (user SPLP report 2026-07-03):
    # pinning pavement to RAW seam DEM created a both-hard chain conflict
    # wherever the design surface sits above terrain — a taxiway crossing
    # the SPLP seam 45 m from the runway needed an infeasible 8.7 % drop,
    # so the final GS split the violation into a V-notch (mirrored on both
    # tiles, invisible to the law because seam pairs were exempt).  The
    # clamp floor ``runway_e − 1.5 %·d`` is computed from CIFP-profiled
    # RUNWAYS ONLY, which BOTH tiles share identically — cross-tile
    # continuity is preserved without either tile seeing the other.
    # Boundary / groundside / feature shapes keep the raw-DEM pin (they
    # follow terrain by design).
    def _runway_floor(x: float, y: float):
        return runway_clamp_floor(layout, x, y)

    n_updated = 0
    for shape in layout.shapes:
        if not shape.node_altitudes:
            continue
        if shape.polygon is None or shape.polygon.is_empty:
            continue
        ring = list(shape.polygon.exterior.coords)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        alts = list(shape.node_altitudes[:len(ring)])
        changed = False
        clamp = shape.role in SEAM_CLAMP_ROLES
        for i, (x, y) in enumerate(ring):
            if _bucket_key(x, y) not in anchor_keys:
                continue
            lat, lon = layout.m_to_ll(x, y)
            try:
                v = _sample_dem(dem, tile_lat, tile_lon, lat, lon)
            except _GEOM_EXC:
                continue
            if v is None or v != v or v == nodata:  # None / NaN / no-data
                continue
            v = float(v)
            if clamp:
                f = _runway_floor(x, y)
                if f is not None and f > v:
                    v = f
            alts[i] = round(v, 2)
            changed = True
            n_updated += 1
        if changed:
            shape.node_altitudes = alts + [alts[0]]
    return n_updated
