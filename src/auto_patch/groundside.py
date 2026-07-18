"""Groundside (curbside / drop-off / parking) pavement emit.

Pulls roads tagged as airport-access or service highway out of the
OSM cache, lifts them off the DEM, and emits matching curbside
ribbon polygons.  Then prunes orphan junction polygons that are
fully contained inside (or touch only) the groundside ribbon —
those are road-island fragments that don't belong with airside
pavement.

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    _emit_groundside_pavement_dem
    _reclassify_groundside_orphan_junctions
"""
from __future__ import annotations

import math
import os as _os
from typing import Dict, List, Optional, Sequence, Set, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import (
    LineString, MultiLineString, MultiPolygon, Point, Polygon, box)
from shapely.ops import linemerge, nearest_points, snap, unary_union

from .layout import (
    AEROWAY_FOR_ROLE,
    BuiltShape,
    PavementLayout,
    R_EARTH,
    ROLE_APRON,
    ROLE_BOUNDARY,
    ROLE_CROSS_CONNECTOR,
    ROLE_GROUNDSIDE_PAVEMENT,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_SECONDARY_PARALLEL,
    ROLE_SERVICE_JUNCTION,
    ROLE_SERVICE_ROAD,
    ROLE_STUB,
    ROLE_BUILDING,
    ROLE_RETAINING_WALL,
    ROLE_TUNNEL_RAMP,
    SHARED_VERTEX_TOL_M,
)
from .pavement.vertices import _snap_polygon_vertices_to_rect_corners
from .elevation import _sample_dem, _resample_node_altitudes_nn
# Groundside ramp-grade cap (rise/run, user 2026-05-22) — single source of
# truth in ``config``; groundside follows the DEM but is graded to this
# cap so steep terrain becomes a navigable car/parking surface.
from .config import GROUNDSIDE_MAX_GRADE

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


__all__ = [
    "_absorb_apron_enclosed_groundside",
    "merge_small_apron_fragments",
    "_reclassify_groundside_orphan_junctions",
    "_emit_groundside_pavement_dem",
    "_separate_groundside_from_airside",
    "_merge_touching_groundside",
    "consolidate_full_width_service_corridors",
]


def _dem_sampler(layout, dem, tile_lat, tile_lon):
    """Return ``_dem_at(x, y) -> Optional[float]`` sampling ``dem`` in
    layout-metre space (anchored at ``layout.anchor``)."""
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH

    def _dem_at(x: float, y: float) -> Optional[float]:
        try:
            lat = lat0 + math.degrees(y / R)
            lon = lon0 + math.degrees(x / (R * cos0))
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None
    return _dem_at


# Douglas-Peucker tolerance for the groundside simplify pass (user
# 2026-05-22): drop over-resolved boundary detail (sub-meter apt.dat /
# DSF curve steps) before the densify+per-vertex-DEM emit, so groundside
# polygons don't carry needless node density into the patch.
GROUNDSIDE_SIMPLIFY_TOL_M = 2.0

# Width (m) of the corridor cut that opens an enclosed hole to the polygon
# exterior (see ``_open_polygon_holes``).  Wide enough to survive the 2 m
# groundside simplify without collapsing (which would re-close the hole),
# narrow enough that the driveway-like notch loses negligible lot area.
_HOLE_OPEN_CUT_WIDTH_M = 3.0


def _open_polygon_holes(p, _depth: int = 0):
    """Return ``p`` re-expressed with NO interior ring, as a list of
    hole-free ``Polygon`` pieces.

    The OSM emitter (``layout.to_osm``) writes only a shape's EXTERIOR
    ring — interior rings are dropped for X-Plane patch compatibility.  A
    groundside polygon that ENCLOSES a building (or any airside shape the
    separation pass subtracted out) therefore re-covers that footprint
    once emitted, producing a 100 %-contained self-overlap (SPJC: building
    #31 inside groundside #455).  Opening the hole here — cutting a thin
    mitre corridor from the hole to the nearest exterior edge so the
    enclosed void becomes an open notch (a simply-connected C-shape) —
    keeps the void real in BOTH the shapely geometry and the emitted
    exterior ring.

    ``[p]`` unchanged when ``p`` already has no holes; ``[]`` if ``p`` is
    unusable.  Usually one piece — the nearest-edge cut reaches only just
    past the exterior on the near side, so the far-side material keeps the
    ring connected.
    """
    if p is None or p.is_empty or p.geom_type != "Polygon":
        return []
    if not p.interiors or _depth > 4:
        return [p]
    cutters = []
    ext = p.exterior
    for interior in p.interiors:
        try:
            hole_ring = LineString(interior)
            a, b = nearest_points(hole_ring, ext)   # a on hole, b on ext
        except _GEOM_EXC:
            continue
        dx, dy = b.x - a.x, b.y - a.y
        d = math.hypot(dx, dy)
        if d < 1e-9:
            # Hole boundary touches the exterior — push the corridor
            # radially outward from the hole centroid instead.
            try:
                cx, cy = Polygon(interior).representative_point().coords[0]
            except _GEOM_EXC:
                continue
            dx, dy = a.x - cx, a.y - cy
            d = math.hypot(dx, dy) or 1.0
        ux, uy = dx / d, dy / d
        start = (a.x - ux * 0.5, a.y - uy * 0.5)     # inside the hole
        end = (b.x + ux * 0.5, b.y + uy * 0.5)       # just past exterior
        try:
            cutters.append(LineString([start, end]).buffer(
                _HOLE_OPEN_CUT_WIDTH_M / 2.0,
                cap_style=2, join_style=2))
        except _GEOM_EXC:
            continue
    if not cutters:
        return [p]
    try:
        opened = p.difference(unary_union(cutters))
    except _GEOM_EXC:
        return [p]
    if opened is None or opened.is_empty:
        return [p]
    pieces: List[Polygon] = []
    parts = ([opened] if opened.geom_type == "Polygon"
             else list(getattr(opened, "geoms", [])))
    for part in parts:
        if part.geom_type != "Polygon" or part.is_empty:
            continue
        if part.interiors:
            pieces.extend(_open_polygon_holes(part, _depth + 1))
        else:
            pieces.append(part)
    return pieces or [p]


def _grade_limit_ring(coords, alts, max_grade, iters=None):
    """Relax per-vertex altitudes so no adjacent ring edge exceeds
    ``max_grade`` (rise/run).  Each pass pulls the steeper end of a
    violating edge toward the other by half the excess; iterates to a
    ≤max_grade profile (ramp-like).  Modifies and returns ``alts``.

    A perturbation propagates ~one vertex per pass in each direction, so
    convergence needs O(n) passes — iters defaults to ``4*n`` so large
    curbside rings fully flatten to the cap."""
    n = len(coords)
    if n < 2 or len(alts) != n:
        return alts
    if iters is None:
        iters = max(300, 4 * n)
    for _ in range(iters):
        worst = 0.0
        for i in range(n):
            j = (i + 1) % n
            d = math.hypot(coords[j][0] - coords[i][0],
                           coords[j][1] - coords[i][1])
            if d < 1e-6:
                continue
            maxd = max_grade * d
            diff = alts[j] - alts[i]
            if abs(diff) > maxd:
                half = (abs(diff) - maxd) / 2.0
                worst = max(worst, abs(diff) - maxd)
                if diff > 0:
                    alts[j] -= half
                    alts[i] += half
                else:
                    alts[j] += half
                    alts[i] -= half
        if worst < 1e-3:
            break
    return alts


def _dem_follow_polygon(p, _dem_at, densify_step_m: float = 15.0,
                        simplify_tol: float = GROUNDSIDE_SIMPLIFY_TOL_M):
    """Densify ``p`` and sample the DEM at every vertex, returning
    ``(densified_polygon, node_altitudes)`` (node_altitudes closed with a
    repeated first value, matching the OSM emitter's convention) or
    ``None`` if it can't be built.

    Shared by ``_emit_groundside_pavement_dem`` and the groundside-orphan
    reclassify so both follow the DEM identically — a polygon that abuts
    DEM-following groundside stays flush with it (no cliff).
    """
    if p is None or p.is_empty or p.geom_type != "Polygon":
        return None
    # Open any interior ring (hole) to the exterior before emitting: the
    # OSM emitter writes only the exterior ring, so an enclosed hole (a
    # building the separation pass subtracted out) would silently re-cover
    # its footprint (SPJC building #31 ⊂ groundside #455).  The nearest-
    # edge corridor keeps the polygon a single connected piece; if it ever
    # splits, follow the largest piece (this function returns one shape).
    if p.interiors:
        opened = _open_polygon_holes(p)
        if not opened:
            return None
        p = max(opened, key=lambda g: g.area)
    # Simplify pass: drop over-resolved boundary detail before densifying
    # so the per-vertex-DEM emit carries fewer nodes.  Densify below
    # re-establishes uniform altitude sampling on the simplified ring.
    # The separation pass passes a SMALL tol (< its clearance) so this
    # only removes the sub-metre clip-boundary edges that would otherwise
    # inflate the per-vertex grade after 0.1 m altitude rounding — without
    # moving the boundary back across the clearance gap it just cut.
    if simplify_tol > 0:
        try:
            s = p.simplify(simplify_tol, preserve_topology=True)
            if s.geom_type == "Polygon" and not s.is_empty and s.is_valid:
                p = s
        except _GEOM_EXC:
            pass
    try:
        ring = list(p.exterior.coords)
    except _GEOM_EXC:
        return None
    if not ring:
        return None
    if ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        return None
    # Truncate needle-tip corners (interior angle below the Triangle4XP
    # sliver threshold) at SOURCE: the OSM emitter drops any polygon that
    # still carries one, and dropping a whole groundside shape uncovers
    # its entire source footprint (HECA: a 30-vertex strip lost to one
    # 1.62° tip → two interior coverage gaps).  Truncation loses only the
    # sub-50 m² wedge beyond the chord.
    from .pavement.junctions import _drop_sliver_corners
    ring = _drop_sliver_corners(ring)
    if len(ring) < 3:
        return None
    # Densify so per-vertex altitudes resolve well across long edges.
    densified: List[Tuple[float, float]] = []
    n_r = len(ring)
    for i in range(n_r):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n_r]
        densified.append((ax, ay))
        edge_len = math.hypot(bx - ax, by - ay)
        if edge_len <= densify_step_m:
            continue
        n_intermediate = int(edge_len // densify_step_m)
        for k in range(1, n_intermediate + 1):
            t = (k * densify_step_m) / edge_len
            if t >= 1.0:
                break
            densified.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    if len(densified) < 3:
        return None
    # Sample DEM at every densified vertex; walk outward to the nearest
    # valid sample for any point that lands outside the DEM tile.
    alts: List[Optional[float]] = [_dem_at(x, y) for x, y in densified]
    if all(a is None for a in alts):
        return None
    for k, a in enumerate(alts):
        if a is not None:
            continue
        found: Optional[float] = None
        for off in range(1, len(alts)):
            left = (k - off) % len(alts)
            right = (k + off) % len(alts)
            if alts[left] is not None:
                found = alts[left]
                break
            if alts[right] is not None:
                found = alts[right]
                break
        assert found is not None, (
            "groundside: walk-outward DEM neighbour search failed despite "
            "precondition ensuring at least one valid sample")
        alts[k] = found
    # Rebuild from densified coords so the polygon and node_altitudes
    # stay 1-for-1; re-sample if buffer(0) validity repair changed the
    # vertex count.
    try:
        new_poly = Polygon(densified)
        if not new_poly.is_valid:
            new_poly = new_poly.buffer(0)
        if new_poly.geom_type != "Polygon" or new_poly.is_empty:
            return None
    except _GEOM_EXC:
        return None
    rebuilt = list(new_poly.exterior.coords)
    if rebuilt and rebuilt[0] == rebuilt[-1]:
        rebuilt = rebuilt[:-1]
    if len(rebuilt) != len(densified):
        alts = [(_dem_at(x, y) or 0.0) for x, y in rebuilt]
    else:
        alts = [float(a) for a in alts]
    # Grade-limit the DEM profile to GROUNDSIDE_MAX_GRADE (ramp-graded,
    # user 2026-05-22) before rounding.  2 decimals, matching the emit
    # resolution — 0.1 m quantization on sub-metre groundside chords
    # reads as 10-15 % stairs (the V15 waviness class).
    alts = _grade_limit_ring(rebuilt, alts, GROUNDSIDE_MAX_GRADE)
    alts = [round(float(a), 2) for a in alts]
    return new_poly, alts + [alts[0]]


def carve_narrow_service_strips(
        layout: "PavementLayout",
        pav_union,
        terminal_union=None,
        *,
        narrow_width_m: float = 25.0,
        sample_step_m: float = 5.0,
        min_run_m: float = 12.0,
        mouth_extension_m: float = 2.5,
        min_piece_m2: float = 25.0,
        ) -> int:
    """Carve NARROW ground-truck-route strips out of apron/junction faces
    as ``ROLE_SERVICE_JUNCTION`` corridors CENTERED on the truck spine
    (user 2026-07-04, CYXY): where the CONTIGUOUS pavement cross-section
    at a service centerline is ≤ ``narrow_width_m``, the WHOLE strip is
    service-road pavement.  The global slice cuts pavement ALONG the
    route line, so each half of a narrow strip merges into whatever big
    face it touches at its ends and no "narrow face" ever exists for
    ``classify_faces`` — one side (or neither) read as road while the
    strip physically IS the road.

    Downstream this cascades through the existing classifiers: the
    service corridor severs the aircraft touch-chain, so lots and pads
    beyond it (reachable only via the road) demote to DEM-following
    groundside in ``_reclassify_runway_disconnected_to_groundside``.
    Wide pavement crossed by a truck route stays apron (standing user
    ruling 2026-07-02) — the carve happens only where the contiguous
    cross-section is narrow.

    Corridor intervals that reach a route END are extended by
    ``mouth_extension_m`` so a road that stops at a groundside lot's
    edge (the pre-slice groundside subtraction leaves a ~1 m gap)
    touches the lot again — the groundside mouth-anchor machinery then
    grades the road to CLIMB to the lot (user ruling 4).

    Supersedes the retired ``O4_SVC_CURVED_JUNCTION`` experiment
    (2026-06-29, net-negative): its corridors graded all-pair 4 %,
    while these faces ride the v14.1 service SPINES (longitudinal 5 %
    along the route).

    Returns the number of carved service pieces added.
    """
    from shapely.ops import substring
    service_lines = getattr(layout, "apt_service_centerlines", None) or []
    if not service_lines or pav_union is None or pav_union.is_empty:
        return 0

    def _contiguous_width(line, arc, probe=60.0):
        p = line.interpolate(arc)
        q = line.interpolate(min(arc + 1.0, line.length))
        dx, dy = q.x - p.x, q.y - p.y
        dn = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / dn, dx / dn
        cross = LineString([(p.x - nx * probe, p.y - ny * probe),
                            (p.x + nx * probe, p.y + ny * probe)])
        try:
            inter = cross.intersection(pav_union)
        except _GEOM_EXC:
            return None
        parts = ([inter] if inter.geom_type == "LineString"
                 else list(getattr(inter, "geoms", ())))
        for part in parts:
            if part.geom_type == "LineString" and part.distance(p) < 2.0:
                return part.length
        return 0.0

    corridors = []
    for centerline in service_lines:
        line = getattr(centerline, "line", None)
        if line is None or line.is_empty or line.length < min_run_m:
            continue
        n_stations = max(2, int(line.length / sample_step_m) + 1)
        arcs = [line.length * k / (n_stations - 1)
                for k in range(n_stations)]
        narrow = []
        for arc in arcs:
            w = _contiguous_width(line, arc)
            narrow.append(w is not None and 0.0 < w <= narrow_width_m)
        # group consecutive narrow stations into intervals
        k = 0
        while k < len(arcs):
            if not narrow[k]:
                k += 1
                continue
            k2 = k
            while k2 + 1 < len(arcs) and narrow[k2 + 1]:
                k2 += 1
            a1, a2 = arcs[k], arcs[k2]
            k = k2 + 1
            if a2 - a1 < min_run_m:
                continue
            try:
                seg = substring(line, a1, a2)
                coords = list(seg.coords)
                # extend intervals that reach a route END so the road
                # mouth re-touches the groundside lot it feeds
                if a1 <= sample_step_m and len(coords) >= 2:
                    (x1, y1), (x2, y2) = coords[0], coords[1]
                    d = math.hypot(x2 - x1, y2 - y1) or 1.0
                    coords[0] = (x1 - (x2 - x1) / d * mouth_extension_m,
                                 y1 - (y2 - y1) / d * mouth_extension_m)
                if a2 >= line.length - sample_step_m and len(coords) >= 2:
                    (x1, y1), (x2, y2) = coords[-2], coords[-1]
                    d = math.hypot(x2 - x1, y2 - y1) or 1.0
                    coords[-1] = (x2 + (x2 - x1) / d * mouth_extension_m,
                                  y2 + (y2 - y1) / d * mouth_extension_m)
                # mitre joins: arc-free carve edges, so the pre-solve
                # conformance passes can stitch neighbours cleanly
                corridors.append(LineString(coords).buffer(
                    narrow_width_m / 2.0 + 1.0, cap_style=2,
                    join_style=2))
            except _GEOM_EXC:
                continue
    if not corridors:
        return 0
    try:
        corridor_union = unary_union(corridors)
        if terminal_union is not None and not terminal_union.is_empty:
            corridor_union = corridor_union.difference(terminal_union)
    except _GEOM_EXC:
        return 0
    if corridor_union.is_empty:
        return 0

    n_carved = 0
    new_shapes = []
    # Post-slice faces can still OVERLAP each other slightly (the
    # overlap-clip pass runs later) — an overlap region inside the
    # corridor must be emitted as service ONCE, and EVERY face loses
    # its full corridor intersection (else face B keeps pavement face
    # A already emitted as road: cross-role overlap, zero-tolerance
    # test).  ``emitted_corridor`` accumulates the pieces emitted so
    # far; each face's carve dedupes against it.
    emitted_corridor = []
    for shape in layout.shapes:
        if shape.role not in (ROLE_APRON, ROLE_JUNCTION) \
                or shape.polygon is None or shape.polygon.is_empty:
            new_shapes.append(shape)
            continue
        try:
            carved = shape.polygon.intersection(corridor_union)
            if emitted_corridor and not carved.is_empty:
                carved = carved.difference(unary_union(emitted_corridor))
        except _GEOM_EXC:
            new_shapes.append(shape)
            continue
        carved_parts = [g for g in
                        ([carved] if carved.geom_type == "Polygon"
                         else list(getattr(carved, "geoms", ())))
                        if g.geom_type == "Polygon"
                        and g.area >= min_piece_m2]
        try:
            remainder = shape.polygon.difference(corridor_union)
        except _GEOM_EXC:
            new_shapes.append(shape)
            continue
        rem_parts = [g for g in
                     ([remainder] if remainder.geom_type == "Polygon"
                      else list(getattr(remainder, "geoms", ())))
                     if g.geom_type == "Polygon" and g.area >= 1.0]
        if not carved_parts and remainder.area \
                >= shape.polygon.area - min_piece_m2:
            # corridor barely grazes this face — leave it whole
            new_shapes.append(shape)
            continue
        if not rem_parts and carved_parts:
            # whole face is road territory
            shape.role = ROLE_SERVICE_JUNCTION
            new_shapes.append(shape)
            emitted_corridor.append(shape.polygon)
            n_carved += 1
            continue
        if not rem_parts:
            new_shapes.append(shape)
            continue
        rem_parts.sort(key=lambda g: -g.area)
        shape.polygon = rem_parts[0]
        new_shapes.append(shape)
        for extra in rem_parts[1:]:
            new_shapes.append(BuiltShape(
                polygon=extra, role=shape.role, ref=shape.ref,
                source_axis=shape.source_axis))
        for piece in carved_parts:
            new_shapes.append(BuiltShape(
                polygon=piece, role=ROLE_SERVICE_JUNCTION, ref="",
                source_axis=None))
            emitted_corridor.append(piece)
            n_carved += 1
    if n_carved:
        layout.shapes = new_shapes
    return n_carved


def consolidate_full_width_service_corridors(
        layout: "PavementLayout",
        *,
        touch_tol_m: float = 0.5,
        min_shared_m: float = 1.0,
        max_corridor_halfwidth_m: float = 15.0,
        route_through_min_m: float = 2.0,
        absorb_fragment_area_m2: float = 60.0,
        ) -> int:
    """Merge the shattered pieces of ONE service-road corridor into
    full-width corridor shapes (user 2026-07-05 full-width corridor):
    a service road is ONE corridor — its spine (the apt.dat truck-route
    centerline) has pavement on BOTH sides, and the two half-strips are
    the same surface.  The global slice cuts pavement by every
    centerline INCLUDING the road's own spine, so each road is born as
    two half-strips flanking the spine, and the carve / re-role /
    conversion passes add along-route fragment chains on top (CYXY
    around (60.70588, -135.07070): five sub-100 m² pieces for one
    ~30 m corridor run).  Solved independently they cannot grade as
    one surface.

    A shape is a corridor MEMBER when it is service pavement ridden by
    exactly ONE truck route (≥ ``route_through_min_m`` of the route
    inside/along it — a piece carrying two routes' interiors is a
    genuine road↔road intersection and is left alone) and its whole
    ring lies within ``max_corridor_halfwidth_m`` of that route (a wide
    lot only meets its route at the mouth and never qualifies).
    Members of the SAME route whose rings share a ≥ ``min_shared_m``
    boundary run merge — this joins both opposite-side half-strips
    (they share the spine seam itself) and along-route fragment chains.
    Small junction / service_junction slivers (≤
    ``absorb_fragment_area_m2``) that sit ON the route inside the
    corridor width DERIVED FROM THE MEMBERS BEING MERGED (never a
    constant — the road's actual pavement extent) are absorbed too
    (CYXY: the 8 m² full-width junction sliver at the corridor mouth).

    The merged shapes carry ``ROLE_SERVICE_ROAD`` (a corridor with one
    route through it grades AXIALLY along that route) and no altitudes
    — this runs PRE-solve so the corridor grades as one surface.

    Gate ``O4_FULL_WIDTH_SERVICE_CORRIDOR`` (default ON); off restores
    the old shattered-pieces behaviour byte-identically.  Returns the
    net number of shapes eliminated by merging.
    """
    if _os.environ.get("O4_FULL_WIDTH_SERVICE_CORRIDOR", "1") != "1":
        return 0
    routes = [centerline.line for centerline in
              (getattr(layout, "apt_service_centerlines", None) or [])
              if centerline.line is not None
              and not centerline.line.is_empty]
    if not routes:
        return 0

    def _max_lateral_extent_m(polygon, route_line):
        """Farthest ring vertex from the route spine — the half-width
        this piece actually spans (user 2026-07-05 full-width corridor:
        corridor width is derived from the faces, never a constant)."""
        try:
            return max(route_line.distance(Point(x, y))
                       for (x, y) in polygon.exterior.coords)
        except _GEOM_EXC:
            return float("inf")

    _debug_at = None
    _debug_spec = _os.environ.get("O4_FW_DEBUG_LL", "")
    if _debug_spec and layout.anchor is not None:
        try:
            _dbg_lat, _dbg_lon = (float(v) for v in _debug_spec.split(","))
            _lat0, _lon0 = layout.anchor
            _debug_at = (
                math.radians(_dbg_lon - _lon0) * R_EARTH
                * math.cos(math.radians(_lat0)),
                math.radians(_dbg_lat - _lat0) * R_EARTH)
        except ValueError:
            _debug_at = None

    def _debug_near(polygon):
        return (_debug_at is not None
                and polygon.distance(Point(*_debug_at)) <= 60.0)

    # ── corridor members: service pavement riding exactly ONE route ──
    member_indices: List[int] = []
    member_route: Dict[int, int] = {}
    member_lateral: Dict[int, float] = {}
    for index, shape in enumerate(layout.shapes):
        if shape.role not in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION):
            continue
        if shape.polygon is None or shape.polygon.is_empty \
                or shape.polygon.geom_type != "Polygon":
            continue
        if getattr(shape, "is_bridge", False):
            continue
        # Measure each route's run along the piece against the piece
        # BUFFERED by the touch tolerance: a half-strip's spine seam IS
        # the route line, running exactly ON the ring — the unbuffered
        # polygon∩line length reads ~0 there whenever a weld/snap moved
        # a seam vertex by millimetres, and the flanking half never
        # qualified (user 2026-07-05 full-width corridor: pavement on
        # BOTH sides of the spine is the same surface, so both flanks
        # must ride the route).
        try:
            reach_polygon = shape.polygon.buffer(touch_tol_m)
        except _GEOM_EXC:
            continue
        riding_routes = []
        for route_index, route_line in enumerate(routes):
            try:
                run_m = route_line.intersection(reach_polygon).length
            except _GEOM_EXC:
                run_m = 0.0
            if run_m >= route_through_min_m:
                riding_routes.append((route_index, run_m))
        if _debug_near(shape.polygon):
            print(f"[fw-debug] shape#{index} role={shape.role} "
                  f"area={shape.polygon.area:.1f} riding={riding_routes}")
        if not riding_routes:
            continue          # routeless piece — not corridor pavement
        riding_routes.sort(key=lambda entry: -entry[1])
        primary_route, primary_run_m = riding_routes[0]
        # A piece is a genuine road↔road INTERSECTION — left alone —
        # only when a SECOND route runs through it comparably to the
        # first.  A side-route MOUTH poking a few metres into a long
        # corridor to meet the spine must not disqualify the corridor
        # (CYXY: an 81 m route-7 corridor touched by 4.7 m of route 2).
        if len(riding_routes) > 1 and riding_routes[1][1] \
                >= max(route_through_min_m, 0.25 * primary_run_m):
            continue          # comparable second route → intersection
        lateral = _max_lateral_extent_m(shape.polygon,
                                        routes[primary_route])
        if _debug_near(shape.polygon):
            print(f"[fw-debug]   lateral={lateral:.1f}")
        if lateral > max_corridor_halfwidth_m:
            continue          # lot-like piece, not a corridor flank
        member_indices.append(index)
        member_route[index] = primary_route
        member_lateral[index] = lateral
    if len(member_indices) < 2:
        return 0

    def _shared_boundary_run_m(polygon_a, polygon_b):
        """Length of ring A running within ``touch_tol_m`` of ring B
        (identity on exactly-shared boundaries; 0 for point touches)."""
        try:
            shared = polygon_a.exterior.intersection(
                polygon_b.exterior.buffer(touch_tol_m))
            return getattr(shared, "length", 0.0)
        except _GEOM_EXC:
            return 0.0

    # ── union-find: same-route members sharing a real boundary run ──
    parent = {index: index for index in member_indices}

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for position, index_a in enumerate(member_indices):
        polygon_a = layout.shapes[index_a].polygon
        for index_b in member_indices[position + 1:]:
            if member_route[index_a] != member_route[index_b]:
                continue      # only pieces flanking the SAME spine merge
            polygon_b = layout.shapes[index_b].polygon
            try:
                if polygon_a.distance(polygon_b) > touch_tol_m:
                    continue
            except _GEOM_EXC:
                continue
            if _shared_boundary_run_m(polygon_a, polygon_b) \
                    < min_shared_m:
                continue      # point / sliver touch — not one corridor
            root_a, root_b = _find(index_a), _find(index_b)
            if root_a != root_b:
                parent[root_a] = root_b

    groups: Dict[int, List[int]] = {}
    for index in member_indices:
        groups.setdefault(_find(index), []).append(index)

    # ── absorb tiny on-route slivers into the corridor they shatter ──
    # (junction / service_junction fragments spanning the corridor —
    # CYXY's 8 m² mouth sliver.)  The lateral cap comes from the GROUP
    # being merged: the corridor's own measured half-width, +1 m slack.
    absorbed: Dict[int, List[int]] = {}
    claimed: Set[int] = set(member_indices)
    for index, shape in enumerate(layout.shapes):
        if index in claimed:
            continue
        if shape.role not in (ROLE_JUNCTION, ROLE_SERVICE_JUNCTION):
            continue
        if shape.polygon is None or shape.polygon.is_empty \
                or shape.polygon.geom_type != "Polygon":
            continue
        if getattr(shape, "is_bridge", False) \
                or shape.polygon.area > absorb_fragment_area_m2:
            continue
        for root, group in groups.items():
            route_line = routes[member_route[group[0]]]
            try:
                if route_line.distance(shape.polygon) > touch_tol_m:
                    continue          # not on this corridor's spine
            except _GEOM_EXC:
                continue
            group_halfwidth = max(member_lateral[member]
                                  for member in group)
            if _max_lateral_extent_m(shape.polygon, route_line) \
                    > group_halfwidth + 1.0:
                continue              # pokes past the corridor width
            if not any(_shared_boundary_run_m(
                        shape.polygon, layout.shapes[member].polygon)
                       >= min_shared_m for member in group):
                continue              # doesn't actually abut the group
            absorbed.setdefault(root, []).append(index)
            claimed.add(index)
            break

    # ── merge each group into full-width corridor shape(s) ──────────
    removed_indices: Set[int] = set()
    merged_shapes: List[BuiltShape] = []
    n_eliminated = 0
    for root, group in groups.items():
        piece_indices = group + absorbed.get(root, [])
        if len(piece_indices) < 2:
            continue
        try:
            union = unary_union([layout.shapes[index].polygon
                                 for index in piece_indices])
        except _GEOM_EXC:
            continue
        union_pieces = ([union] if union.geom_type == "Polygon"
                        else [g for g in getattr(union, "geoms", ())
                              if g.geom_type == "Polygon"])
        union_pieces = [g for g in union_pieces if not g.is_empty]
        if not union_pieces or len(union_pieces) >= len(piece_indices):
            continue                  # nothing actually merged
        if any(any(Polygon(ring).area > 0.05 for ring in g.interiors)
               for g in union_pieces):
            continue    # a real hole appeared — keep the pieces as-is
        for g in union_pieces:
            if g.interiors:           # drop numeric-noise micro holes
                g = Polygon(g.exterior)
            merged_shapes.append(BuiltShape(
                polygon=g, role=ROLE_SERVICE_ROAD, ref="",
                source_axis=None))
        removed_indices.update(piece_indices)
        n_eliminated += len(piece_indices) - len(union_pieces)
    if not removed_indices:
        return 0
    layout.shapes = [shape for index, shape in enumerate(layout.shapes)
                     if index not in removed_indices] + merged_shapes
    return n_eliminated


def reclassify_groundside_route_corridors(
        layout: "PavementLayout",
        *,
        corridor_halfwidth_m: float = 13.5,
        min_cover_frac: float = 0.70,
        min_run_m: float = 30.0,
        ) -> int:
    """Re-role a groundside piece that IS a truck-route road corridor to
    ``ROLE_SERVICE_ROAD`` (user 2026-07-04, CYXY #206): OSM-captured
    groundside pavement (curbside / parking capture) can coincide with an
    apt.dat ground-truck route end-to-end — an 835 m road emitted as a
    DEM "lot", which ``apply_groundside_reach`` then rigid-shifted 9 m
    below terrain to satisfy its apron connector.  A piece whose area
    lies ≥``min_cover_frac`` within ``corridor_halfwidth_m`` of the
    service centerlines AND that carries ≥``min_run_m`` of route is ROAD
    pavement: as ``service_road`` it grades AXIALLY along the route
    (DEM-following ramp at the road cap) instead of being re-levelled as
    a destination lot.  A genuine lot only meets its route at the mouth
    (coverage ≪ ``min_cover_frac``), so it never converts.  Short mouth
    pieces DO convert (measured: leaving CYXY's three 100-250 m² mouth
    pieces groundside re-opens the 40-80 % road-weld cliffs — the
    original 414-count disease).

    Returns the number of shapes re-roled."""
    if _os.environ.get("O4_GROUNDSIDE_ROUTE_CORRIDOR", "1") != "1":
        return 0
    service_lines = [c.line for c in
                     (getattr(layout, "apt_service_centerlines", None) or [])
                     if c.line is not None and not c.line.is_empty]
    if not service_lines:
        return 0
    try:
        line_union = unary_union(service_lines)
        corridor = line_union.buffer(corridor_halfwidth_m)
    except _GEOM_EXC:
        return 0
    # Existing pavement union (any non-groundside, non-feature role):
    # groundside was allowed to OVERLAP service pavement until the
    # separation pass trimmed it — a converted piece leaves that regime,
    # so trim the overlap NOW or it emits as service∩service
    # (zero-tolerance test_no_self_overlap, CYXY 0.4 m²).
    _FEATURE_ROLES = {ROLE_BOUNDARY, ROLE_GROUNDSIDE_PAVEMENT}
    try:
        pav_union = unary_union(
            [t.polygon for t in layout.shapes
             if t.polygon is not None and not t.polygon.is_empty
             and t.role not in _FEATURE_ROLES
             and not str(t.role).endswith("clearance")])
    except _GEOM_EXC:
        pav_union = None
    n_reroled = 0
    extra_shapes: list = []
    for s in layout.shapes:
        if s.role != ROLE_GROUNDSIDE_PAVEMENT or s.polygon is None \
                or s.polygon.is_empty:
            continue
        try:
            run_m = line_union.intersection(s.polygon).length
            cover = s.polygon.intersection(corridor).area / s.polygon.area
        except _GEOM_EXC:
            continue
        # A piece ENTIRELY inside the truck corridor with the route
        # genuinely through it is road pavement no matter how short the
        # run — a loop's turnaround pad (CYXY #56: 98 m², cover 1.00,
        # 14 m of 'Crew cars' through it) sat as groundside one
        # clearance-gap cliff off its own road.
        _full_corridor = cover >= 0.95 and run_m >= 5.0
        if (run_m < min_run_m and not _full_corridor) \
                or cover < min_cover_frac:
            continue
        parts = [s.polygon]
        if pav_union is not None:
            try:
                trimmed = s.polygon.difference(pav_union)
            except _GEOM_EXC:
                trimmed = None
            parts = ([] if trimmed is None or trimmed.is_empty
                     else [trimmed] if trimmed.geom_type == "Polygon"
                     else [g for g in getattr(trimmed, "geoms", ())
                           if g.geom_type == "Polygon"])
            parts = [g for g in parts if g.area >= _GROUNDSIDE_MIN_AREA_M2]
            if not parts:
                continue                  # fully covered — nothing to convert
            parts.sort(key=lambda g: -g.area)
        s.polygon = parts[0]
        s.role = ROLE_SERVICE_ROAD
        s.ref = ""
        s.node_altitudes = None
        s.altitude = None
        s.altitude_high = None
        s.altitude_low = None
        for extra in parts[1:]:
            extra_shapes.append(BuiltShape(
                polygon=extra, role=ROLE_SERVICE_ROAD, ref=""))
        n_reroled += 1
    if extra_shapes:
        layout.shapes.extend(extra_shapes)
    return n_reroled


def conform_service_mouths_to_groundside(
        layout: "PavementLayout",
        touch_tol_m: float = 0.5,
        ) -> int:
    """Insert shared vertices into groundside lot rings where a SERVICE
    shape's ring vertex lies on the lot boundary — the road↔lot
    connection is identified EARLY and becomes first-class shared
    geometry (user 2026-07-04, CYXY P4): the lot is served BY the road,
    so the two must emit welded nodes there, and the groundside
    mouth-anchor machinery can bind by canonical key instead of failing
    on a mouth that lands mid-edge (the road then CLIMBS to the lot).

    The inserted vertex takes the lot edge's interpolated DEM altitude,
    so the lot's surface is unchanged — only its ring gains a node.
    Returns the number of vertices inserted.
    """
    service_shapes = [s for s in layout.shapes
                      if s.role in (ROLE_SERVICE_ROAD,
                                    ROLE_SERVICE_JUNCTION)
                      and s.polygon is not None
                      and not s.polygon.is_empty]
    if not service_shapes:
        return 0
    n_inserted = 0
    for lot in layout.shapes:
        if lot.role != ROLE_GROUNDSIDE_PAVEMENT or lot.polygon is None \
                or lot.polygon.is_empty or not lot.node_altitudes:
            continue
        near = [s for s in service_shapes
                if s.polygon.distance(lot.polygon) <= touch_tol_m]
        if not near:
            continue
        ring = list(lot.polygon.exterior.coords)
        closed = len(ring) > 1 and ring[0] == ring[-1]
        if closed:
            ring = ring[:-1]
        alts = list(lot.node_altitudes[:len(ring)])
        if len(alts) < len(ring):
            alts += [alts[-1] if alts else None] * (len(ring) - len(alts))
        changed = False
        for s in near:
            s_ring = list(s.polygon.exterior.coords)
            if len(s_ring) > 1 and s_ring[0] == s_ring[-1]:
                s_ring = s_ring[:-1]
            boundary = lot.polygon.exterior
            for (vx, vy) in s_ring:
                p = Point(vx, vy)
                if boundary.distance(p) > touch_tol_m:
                    continue
                if any(math.hypot(vx - rx, vy - ry) <= touch_tol_m
                       for (rx, ry) in ring):
                    continue          # a lot vertex is already there
                # insert into the lot edge nearest the mouth vertex
                best = None
                for k in range(len(ring)):
                    ax, ay = ring[k]
                    bx, by = ring[(k + 1) % len(ring)]
                    dx, dy = bx - ax, by - ay
                    seg2 = dx * dx + dy * dy
                    if seg2 < 1e-9:
                        continue
                    t = ((vx - ax) * dx + (vy - ay) * dy) / seg2
                    t = min(1.0, max(0.0, t))
                    px, py = ax + t * dx, ay + t * dy
                    d = math.hypot(vx - px, vy - py)
                    if best is None or d < best[0]:
                        best = (d, k, t, px, py)
                if best is None or best[0] > touch_tol_m:
                    continue
                _d, k, t, px, py = best
                a0 = alts[k]
                a1 = alts[(k + 1) % len(ring)]
                new_alt = (round(a0 + t * (a1 - a0), 2)
                           if a0 is not None and a1 is not None else a0)
                ring.insert(k + 1, (px, py))
                alts.insert(k + 1, new_alt)
                changed = True
                n_inserted += 1
        if changed:
            try:
                lot.polygon = Polygon(ring)
            except _GEOM_EXC:
                continue
            lot.node_altitudes = alts + [alts[0]]
    return n_inserted


def conform_parallel_service_edges(layout, window_m: float = 2.0,
                                   min_gap_m: float = 0.05,
                                   dedup_m: float = 0.75) -> int:
    """Insert projected vertices where two SERVICE shapes run parallel
    within ``window_m`` of each other (user 2026-07-06, HECA #578↔#64).

    Two near-parallel roads carry no shared geometry, and their ring
    nodes are offset along-track — the service DEM-follow's node↔node
    proximity coupling then has nothing to bind (a 1 m gap emitted a
    0.9 m mid-edge wall between a flat road and a climbing one).  For
    every vertex of shape A within the window of shape B's boundary,
    insert the projection foot into B's ring (skipped when a B vertex
    already sits within ``dedup_m``), so matching nodes exist for the
    coupling.  Runs PRE-SOLVE (no altitudes exist yet on service rings).
    Returns the number of inserted vertices."""
    import os as _os
    if _os.environ.get("O4_SVC_PARALLEL_CONFORM", "1") != "1":
        return 0
    svc = [s for s in layout.shapes
           if s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)
           and s.polygon is not None and not s.polygon.is_empty
           and s.polygon.geom_type == "Polygon"]
    n_inserted = 0
    for receiver in svc:
        try:
            ring = list(receiver.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        closed = len(ring) > 1 and ring[0] == ring[-1]
        if closed:
            ring = ring[:-1]
        changed = False
        for donor in svc:
            if donor is receiver:
                continue
            try:
                if donor.polygon.distance(receiver.polygon) > window_m:
                    continue
            except _GEOM_EXC:
                continue
            try:
                donor_ring = list(donor.polygon.exterior.coords)[:-1]
            except _GEOM_EXC:
                continue
            for (vx, vy) in donor_ring:
                # nearest point on the receiver ring
                best = None
                for k in range(len(ring)):
                    ax, ay = ring[k]
                    bx, by = ring[(k + 1) % len(ring)]
                    dx, dy = bx - ax, by - ay
                    seg2 = dx * dx + dy * dy
                    if seg2 < 1e-9:
                        continue
                    t = ((vx - ax) * dx + (vy - ay) * dy) / seg2
                    t = min(1.0, max(0.0, t))
                    px, py = ax + t * dx, ay + t * dy
                    d = math.hypot(vx - px, vy - py)
                    if best is None or d < best[0]:
                        best = (d, k, px, py)
                if (best is None or best[0] > window_m
                        or best[0] < min_gap_m):
                    continue
                _d, k, px, py = best
                if any(math.hypot(px - rx, py - ry) <= dedup_m
                       for (rx, ry) in ring):
                    continue          # a receiver vertex is already there
                ring.insert(k + 1, (px, py))
                changed = True
                n_inserted += 1
        if changed:
            try:
                receiver.polygon = Polygon(ring)
            except _GEOM_EXC:
                continue
    return n_inserted


def chord_limit_ring_altitudes(coords, alts,
                               cap: float = GROUNDSIDE_MAX_GRADE,
                               sweeps: int = 4):
    """Largest ``cap``-Lipschitz field ≤ ``alts`` over straight-line CHORD
    pairs of ONE ring (the within-shape validator metric) — the single-ring
    core of ``_grade_limit_groundside_chords``, callable at SOLVE time.

    ``apply_groundside_reach`` welds service-road nodes to the groundside
    ring values it computes, but the post-solve chord limiter used to
    rewrite the LOT ring only — two writers for the same physical nodes
    (CYXY road #41: pinned 698.15/699.66 at solve time while the emitted
    lot said 699.5/699.4 → 15 % road chords after emit consensus).
    Limiting the ring BEFORE the weld reads it makes the solve-time field
    identical to the post-solve one (the late limiter is idempotent on an
    already-limited ring).

    ``coords`` may be closed (last == first); ``alts`` may carry ``None``
    (skipped).  Returns a new list shaped like ``alts``."""
    m = min(len(coords), len(alts))
    vals = [None if alts[k] is None else float(alts[k]) for k in range(m)]
    live = [k for k in range(m) if vals[k] is not None]
    for _sweep in range(sweeps):
        changed = False
        for a in live:
            xa, ya = coords[a]
            best = vals[a]
            for b in live:
                if b == a:
                    continue
                xb, yb = coords[b]
                lim = vals[b] + cap * math.hypot(xa - xb, ya - yb)
                if lim < best:
                    best = lim
            if best < vals[a] - 1e-6:
                vals[a] = best
                changed = True
        if not changed:
            break
    out = list(alts)
    for k in range(m):
        if vals[k] is not None:
            out[k] = round(vals[k], 2)
    # keep a closed ring closed
    if len(out) == len(coords) and len(coords) > 1 \
            and tuple(coords[0]) == tuple(coords[-1]) and out[0] is not None:
        out[-1] = out[0]
    return out


def _grade_limit_groundside_chords(layout) -> int:
    """Pull every groundside shape's altitude field down to the largest
    ``GROUNDSIDE_MAX_GRADE``-Lipschitz field ≤ its current (DEM) values,
    measured over straight-line CHORD pairs — the within-shape validator
    metric.  ``_dem_follow_polygon``'s ring-ramp limit only bounds
    CONSECUTIVE ring vertices; a ring-compliant hillside piece still
    reads >4 % across its interior (HECA #230: 4.7-5.5 %).  Shared
    boundary nodes are UNIFIED across shapes (keyed by rounded coords)
    so abutting groundside pieces stay flush.  Runs ONCE, late, over
    ALL groundside shapes regardless of which pass created them.
    Returns the number of shapes whose altitudes changed."""
    node_alt: dict = {}
    rings: dict = {}
    for i, s in enumerate(layout.shapes):
        if s.role != ROLE_GROUNDSIDE_PAVEMENT:
            continue
        if (s.polygon is None or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"):
            continue
        if not s.node_altitudes:
            continue
        try:
            ring = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        alts = list(s.node_altitudes)
        if len(alts) == len(ring) + 1:
            alts = alts[:-1]
        if len(alts) != len(ring) or len(ring) < 3:
            continue
        keys = [(round(x, 2), round(y, 2)) for x, y in ring]
        rings[i] = keys
        for kxy, a in zip(keys, alts):
            v = float(a)
            node_alt[kxy] = min(node_alt.get(kxy, v), v)
    if not rings:
        return 0
    for _sweep in range(4):
        changed = False
        for i, keys in rings.items():
            m = len(keys)
            for ai in range(m):
                xa, ya = keys[ai]
                best = node_alt[keys[ai]]
                for bj in range(m):
                    if bj == ai:
                        continue
                    xb, yb = keys[bj]
                    dd = math.hypot(xa - xb, ya - yb)
                    cap = node_alt[keys[bj]] + GROUNDSIDE_MAX_GRADE * dd
                    if cap < best:
                        best = cap
                if best < node_alt[keys[ai]] - 1e-6:
                    node_alt[keys[ai]] = best
                    changed = True
        if not changed:
            break
    n_changed = 0
    for i, keys in rings.items():
        s = layout.shapes[i]
        # 2 decimals, matching the emit resolution end-to-end — 0.1 m
        # quantization on sub-metre groundside chords reads as 10-15 %
        # stairs (the V15 waviness class).
        alts = [round(node_alt[k], 2) for k in keys]
        closed = alts + [alts[0]]
        if closed != list(s.node_altitudes):
            s.node_altitudes = closed
            n_changed += 1
    # ADOPT the limited values onto coincident SERVICE nodes AT THE SOLVE-
    # TIME WELD KEYS (the road↔lot shared geometry): the roads were welded
    # to the lot's SOLVE-time ring, and re-limiting only the lot re-splits
    # the two writers — the emit consensus then averages them and the lot
    # reads over-cap chords again (CYXY #207: 8 % over 7.8 m from
    # 0.3-0.4 m road-vs-lot disagreements).  The lot is senior at its own
    # ring (the mouth serves the LOT).  Scoped STRICTLY to the keys
    # ``apply_groundside_reach`` welded — a road passing a DEM-stay lot
    # merely shares geometry and keeps its by-design road-vs-lot seam
    # (blanket adoption measured 5 m road yanks, 125 % chords).
    weld_keys = getattr(layout, "_groundside_weld_keys", None) or ()
    # Weld keys whose re-adoption MOVED the road value materially: the
    # limiter re-levelled the lot around a weld the road's own law had
    # placed elsewhere — residual multi-authority tension neither side
    # may fully absorb (the projection cannot see lot rings or road
    # diagonal chords).  Recorded so the caller can quarantine them
    # (CYXY #26: a 4 cm re-adoption tore three road diagonals over cap).
    moved_weld_xy: list = getattr(layout, "_weld_relimit_moved_xy", None)
    if moved_weld_xy is None:
        moved_weld_xy = []
        layout._weld_relimit_moved_xy = moved_weld_xy
    _MOVED_TOL_M = 0.02
    for s in layout.shapes:
        if s.role not in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION):
            continue
        if (s.polygon is None or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"
                or not s.node_altitudes):
            continue
        try:
            ring = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        alts = list(s.node_altitudes)
        changed = False
        for k in range(min(len(ring), len(alts))):
            kxy = (round(ring[k][0], 2), round(ring[k][1], 2))
            if kxy not in weld_keys:
                continue
            v = node_alt.get(kxy)
            if v is not None and alts[k] is not None \
                    and abs(alts[k] - v) > 1e-6:
                if abs(alts[k] - v) > _MOVED_TOL_M:
                    moved_weld_xy.append((ring[k][0], ring[k][1]))
                alts[k] = round(v, 2)
                changed = True
        if changed:
            s.node_altitudes = alts
    return n_changed


def _perimeter_frac_near(poly, region, radius_m: float = 1.5,
                         step_m: float = 1.5) -> float:
    """Fraction of ``poly``'s exterior perimeter lying within ``radius_m``
    of ``region`` (a Polygon/MultiPolygon, or None → 0.0).

    A genuine curbside strip faces a road / open terrain on its outer
    side (that perimeter is NOT near any apron), so its apron-bounded
    fraction is low.  An apron island wrongly carved out by a groundside
    strip is bounded by apron almost all the way around."""
    if region is None or getattr(region, "is_empty", True):
        return 0.0
    try:
        ring = list(poly.exterior.coords)
    except _GEOM_EXC:
        return 0.0
    near = 0.0
    total = 0.0
    for i in range(len(ring) - 1):
        ax, ay = ring[i]
        bx, by = ring[i + 1]
        seg = math.hypot(bx - ax, by - ay)
        if seg < 1e-6:
            continue
        n = max(1, int(seg // step_m))
        sub = seg / n
        for k in range(n):
            t = (k + 0.5) / n
            try:
                if region.distance(Point(ax + (bx - ax) * t,
                                         ay + (by - ay) * t)) <= radius_m:
                    near += sub
            except _GEOM_EXC:
                pass
            total += sub
    return (near / total) if total else 0.0


def _shape_repr_alt(s: "BuiltShape") -> Optional[float]:
    """One representative elevation for a shape, whichever altitude
    convention it carries (flat / sloped / per-vertex)."""
    if s.altitude is not None:
        return float(s.altitude)
    if s.altitude_high is not None and s.altitude_low is not None:
        return 0.5 * (float(s.altitude_high) + float(s.altitude_low))
    if s.node_altitudes:
        vals = [float(a) for a in s.node_altitudes if a is not None]
        if vals:
            return sum(vals) / len(vals)
    return None


# Apron-island / airside-wedged absorption (user 2026-06-03).  A piece is
# reclassified from groundside to flush ``apron`` when it has essentially
# NO open-terrain / road frontage (it is wedged inside the airside: an
# apron island, an apron-hugging clip residue, or a sliver between apron
# and the terminal) AND it touches at least some apron.  Genuine curbside
# faces a road / open terrain on its outer side, so its open fraction is
# well above the gate and it stays groundside.
#
# Measured on the EMITTED groundside shapes, AFTER ``_emit_..._dem`` has
# subtracted apron/terminal (so the perimeter reflects true adjacency) but
# BEFORE ``_separate_..._airside`` opens the 1 m clearance gap (so an
# absorbed piece is still flush with the apron, not 1 m off it).
_APRON_ISLAND_OPEN_MAX = 0.15    # max road/open frontage to still absorb
_APRON_ISLAND_APRON_MIN = 0.15   # must touch at least this much apron
# A qualifying piece whose perimeter is more than this fraction terminal-
# bordered is absorbed into the TERMINAL (flat building pad), not the apron
# (user 2026-06-03: "merge any apron inside a terminal with the terminal").
_APRON_ISLAND_TERM_MAJORITY = 0.5


def _best_bordering_shape(piece: "Polygon", shapes, radius_m: float):
    """The shape in ``shapes`` sharing the most boundary length with ``piece``
    (``None`` if none shares more than 1 m)."""
    pb = piece.buffer(radius_m)
    best = None
    best_share = 1.0
    for a in shapes:
        if a.polygon is None or a.polygon.is_empty:
            continue
        try:
            share = a.polygon.boundary.intersection(pb).length
        except _GEOM_EXC:
            continue
        if share > best_share:
            best_share = share
            best = a
    return best


def _clean_merge(merged):
    """Clean a merged polygon for emit: ``buffer(0)``, keep the largest part,
    and DROP needle/sliver corners — the thin-gap bridge can leave a near-zero-
    angle spike at the seam, and a sliver corner makes the X-Plane emit DROP the
    WHOLE shape (it dropped terminal1 / terminal9 at HECA).  Returns a valid
    ``Polygon`` whose corners clear ``SLIVER_ANGLE_THRESHOLD_DEG``, or ``None``
    (caller bails and the piece falls back) if it can't be made clean."""
    from .pavement.junctions import _drop_sliver_corners
    if merged is None or merged.is_empty:
        return None
    try:
        if not merged.is_valid:
            merged = merged.buffer(0)
        if merged.geom_type == "MultiPolygon":
            merged = max(merged.geoms, key=lambda g: g.area)
        if merged.geom_type != "Polygon" or merged.is_empty:
            return None
        base = merged        # exact intended coverage of the kept part
        ring = list(merged.exterior.coords)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        ring = _drop_sliver_corners(ring)
        if len(ring) < 3:
            return None
        # Keep the merged polygon's interior rings — a bare
        # Polygon(ring) FILLED them, re-covering a terminal pad the
        # overlap-clip had carved out of the apron (KPHL terminal11,
        # 6,352 m² overlap; no clip pass runs after this absorb).
        from .junction_rules import _rebuild_ring_with_holes
        cleaned = _rebuild_ring_with_holes(ring, merged,
                                           normalize=False)
        if cleaned is None:
            return None
        if not cleaned.is_valid:
            cleaned = cleaned.buffer(0)
        if cleaned.geom_type != "Polygon" or cleaned.is_empty:
            return None
        # The sliver-corner drop may CHORD the ring across a DEEP concave
        # notch and pave ground that was in NEITHER input (CYXY item B: at
        # a 24 m spine step the wedge-absorb chorded ~1.5 k m² of yard into
        # junction #91 → the emitted apron rested 27 % on source).  Needle
        # cleanup only ever changes sliver-scale area, so subtract any
        # ADDED part big enough to be real ground.
        from shapely.ops import unary_union as _uu
        added = cleaned.difference(base)
        big = [g for g in getattr(added, "geoms", [added])
               if g.geom_type == "Polygon" and g.area > 5.0]
        if big:
            reclipped = cleaned.difference(_uu(big))
            if not reclipped.is_valid:
                reclipped = reclipped.buffer(0)
            if reclipped.geom_type == "MultiPolygon":
                reclipped = max(reclipped.geoms, key=lambda g: g.area)
            if reclipped.geom_type != "Polygon" or reclipped.is_empty:
                return None
            cleaned = reclipped
        return cleaned
    except _GEOM_EXC:
        return None



def _has_interior(g) -> bool:
    """True if ``g`` (Polygon / MultiPolygon) has any interior ring (hole)."""
    if g is None or g.is_empty:
        return False
    if g.geom_type == "Polygon":
        return len(list(g.interiors)) > 0
    if g.geom_type == "MultiPolygon":
        return any(len(list(p.interiors)) > 0 for p in g.geoms)
    return False


def merge_small_apron_fragments(layout: "PavementLayout",
                                radius_m: float = 1.5,
                                max_area_m2: float = 600.0) -> int:
    """PRE-SOLVE: fold a SMALL apron piece fully enclosed by apron/terminal into
    its larger neighbour (PURE GEOMETRY — runs before the elevation solver, so
    the merged shape's edges/elevation simply disappear and the solver grades
    the one unified apron; no post-solve step to reconcile).

    Only genuine slivers (< ``max_area_m2``) with NO taxi/runway/open frontage
    qualify, so real aprons (incl. neck-split pads) are left alone.  HOLE-SLICE
    SAFE: never fuses a union that would enclose a void, so the hole-router's
    intentional hole-opening cuts are preserved.  Returns the count merged."""
    aprons = [s for s in layout.shapes if s.role == ROLE_APRON
              and s.polygon is not None and not s.polygon.is_empty
              and s.polygon.geom_type == "Polygon"]
    if len(aprons) < 2:
        return 0
    try:
        other_union = unary_union([
            s.polygon for s in layout.shapes
            if s.role not in (ROLE_APRON, ROLE_BUILDING)
            and s.polygon is not None and not s.polygon.is_empty
            and s.polygon.geom_type in ("Polygon", "MultiPolygon")])
    except _GEOM_EXC:
        other_union = None
    try:
        road_union = unary_union([
            s.polygon for s in layout.shapes
            if s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)
            and s.polygon is not None and not s.polygon.is_empty])
    except _GEOM_EXC:
        road_union = None
    n = 0
    for s in sorted(aprons, key=lambda a: a.polygon.area):   # smallest first
        p = s.polygon
        if p is None or p.is_empty or p.area >= max_area_m2:
            continue
        if other_union is not None and \
                _perimeter_frac_near(p, other_union, radius_m) > 0.05:
            continue                       # touches taxi/runway -> real apron
        hosts = [a for a in aprons if a is not s and a.polygon is not None
                 and not a.polygon.is_empty and a.polygon.area > p.area]
        host = _best_bordering_shape(p, hosts, radius_m)
        if host is None:
            continue
        try:
            merged = unary_union([host.polygon, p])
        except _GEOM_EXC:
            continue
        if _has_interior(merged):
            continue                       # would re-bury a void (hole slice)
        cleaned = _clean_merge(merged)
        if cleaned is None:
            continue
        # (s79) the sliver-corner drop can chord the merged ring ACROSS
        # a carved ROAD corridor (the KPHL terminal-incursion class with
        # a road instead of a pad — CYXY pav[1] ramp, 13.5 m² onto
        # SVC11): clip the merge result back off the road rects.
        if road_union is not None and not road_union.is_empty:
            try:
                clipped = cleaned.difference(road_union)
            except _GEOM_EXC:
                clipped = None
            if clipped is not None and clipped.geom_type == "Polygon" \
                    and not clipped.is_empty:
                cleaned = clipped
            elif clipped is not None \
                    and clipped.geom_type == "MultiPolygon":
                cleaned = max(clipped.geoms, key=lambda g: g.area)
        host.polygon = cleaned             # solver assigns node_altitudes later
        s.polygon = None
        n += 1
    if n:
        layout.shapes = [s for s in layout.shapes if s.polygon is not None]
    return n


def _merge_piece_into_apron(piece: "Polygon", apron, radius_m: float,
                            clip_against=None) -> bool:
    """Union ``piece`` into ``apron`` as ONE continuous, node-shared polygon and
    rebuild the apron's per-vertex altitudes: old vertices keep theirs, new
    (piece) vertices sample the apron's PRE-merge surface (``_edge_interp_alt``).
    Returns ``False`` (caller falls back) if the union isn't a clean single
    polygon.  ``clip_against`` (e.g. the terminal-pad union) is subtracted
    from the cleaned merge — ``_clean_merge``'s sliver-corner drop can chord
    a notch ACROSS a terminal edge (KPHL terminal22: a 0.4 m × 2.5 m
    incursion pocket), and no overlap-clip pass runs after this absorb."""
    from types import SimpleNamespace
    from .clearance import _edge_interp_alt
    before = apron.polygon
    before_na = list(apron.node_altitudes) if apron.node_altitudes else None
    try:
        merged = unary_union([before, piece])
        if merged is not None and merged.geom_type != "Polygon":
            # The piece touches the apron only at a POINT / is offset by a
            # sub-metre gap (near-coincident boundaries, not edge-shared), so
            # the plain union can't fuse them into one polygon (e.g. HECA
            # #2169 ↔ #305).  Bridge ONLY the thin gap between them — the
            # region within ``d`` of BOTH, in NEITHER — so the host apron's
            # other boundaries are left untouched (no morphological close that
            # would round corners / fill notches and desync shared edges).
            d = 0.6
            gap = (before.buffer(d).intersection(piece.buffer(d))
                   .difference(before).difference(piece))
            merged = unary_union([before, piece, gap])
    except _GEOM_EXC:
        return False
    merged = _clean_merge(merged)
    if merged is None:
        return False
    if clip_against is not None and not clip_against.is_empty:
        try:
            if merged.intersects(clip_against):
                clipped = merged.difference(clip_against)
                if clipped.geom_type == "MultiPolygon":
                    clipped = max(clipped.geoms, key=lambda g: g.area)
                if (clipped.geom_type == "Polygon"
                        and not clipped.is_empty):
                    merged = clipped
        except _GEOM_EXC:
            pass
    if before_na is None:
        # Flat apron: the union stays flat at the same altitude, nothing to
        # rebuild per-vertex.
        apron.polygon = merged
        return True
    old = list(before.exterior.coords)
    if old and old[0] == old[-1]:
        old = old[:-1]
    oldmap = {(round(x, 2), round(y, 2)): before_na[k]
              for k, (x, y) in enumerate(old) if k < len(before_na)}
    src = SimpleNamespace(node_altitudes=before_na, polygon=before)
    mean = sum(before_na) / len(before_na)
    new_ring = list(merged.exterior.coords)
    if new_ring and new_ring[0] == new_ring[-1]:
        new_ring = new_ring[:-1]
    new_na = []
    for (x, y) in new_ring:
        z = oldmap.get((round(x, 2), round(y, 2)))
        if z is None:
            try:
                z = _edge_interp_alt(src, x, y)
            except _GEOM_EXC:
                z = None
        new_na.append(z if z is not None else mean)
    apron.polygon = merged
    apron.node_altitudes = new_na
    apron.altitude = None
    apron.altitude_high = None
    apron.altitude_low = None
    return True


def _merge_piece_into_terminal(piece: "Polygon", terminal, radius_m: float,
                               clip_against=None) -> bool:
    """Union ``piece`` into ``terminal`` as one continuous polygon, kept FLAT at
    the terminal's level (building pads are flat).  Same thin-gap bridge as the
    apron merge for point-touching pieces.  ``False`` if the union isn't a clean
    single polygon.  ``clip_against`` (the apron union) is subtracted from the
    cleaned merge: the 0.6 m gap-bridge can lap onto an adjacent apron's
    footprint, and the apron-side absorb bridges the SAME thin gap from the
    other side — the two independently-bridged rings overlapped by a ~0.1 m²
    sliver at KPHL terminal22, with no overlap-clip pass running after."""
    before = terminal.polygon
    try:
        merged = unary_union([before, piece])
        if merged is not None and merged.geom_type != "Polygon":
            d = 0.6
            gap = (before.buffer(d).intersection(piece.buffer(d))
                   .difference(before).difference(piece))
            merged = unary_union([before, piece, gap])
    except _GEOM_EXC:
        return False
    merged = _clean_merge(merged)
    if merged is None:
        return False
    if clip_against is not None and not clip_against.is_empty:
        try:
            if merged.intersects(clip_against):
                clipped = merged.difference(clip_against)
                if clipped.geom_type == "MultiPolygon":
                    clipped = max(clipped.geoms, key=lambda g: g.area)
                if (clipped.geom_type == "Polygon"
                        and not clipped.is_empty):
                    merged = clipped
        except _GEOM_EXC:
            pass
    lvl = _shape_repr_alt(terminal)
    terminal.polygon = merged
    if lvl is not None:
        terminal.altitude = round(lvl, 1)
        terminal.node_altitudes = None
        terminal.altitude_high = None
        terminal.altitude_low = None
    return True


def _absorb_apron_enclosed_groundside(
        layout: "PavementLayout",
        radius_m: float = 1.5) -> int:
    """Reclassify emitted groundside shapes that sit wedged inside the
    airside — apron islands, apron-hugging clip residue, apron/terminal
    sandwich slivers — back into flush ``apron`` pavement at the
    neighbouring aprons' elevation, instead of leaving them as
    DEM-following groundside that would otherwise become a 1 m-gapped
    sliver after the separation pass.

    A piece qualifies when its perimeter has at most ``_APRON_ISLAND_
    OPEN_MAX`` open (road / terrain) frontage and touches at least
    ``_APRON_ISLAND_APRON_MIN`` apron.  Genuine curbside — which faces a
    road on its outer side — keeps a high open fraction and is left alone.

    Runs AFTER ``_emit_groundside_pavement_dem`` and BEFORE
    ``_separate_groundside_from_airside``.  Returns the number absorbed.
    """
    gs_shapes = [s for s in layout.shapes
                 if s.role == ROLE_GROUNDSIDE_PAVEMENT
                 and s.polygon is not None and not s.polygon.is_empty]
    if not gs_shapes:
        return 0
    apron_shapes = [s for s in layout.shapes
                    if s.role == ROLE_APRON
                    and s.polygon is not None and not s.polygon.is_empty]
    if not apron_shapes:
        return 0
    try:
        apron_union = unary_union([s.polygon for s in apron_shapes])
    except _GEOM_EXC:
        return 0
    terminal_shapes = [s for s in layout.shapes
                       if s.role == ROLE_BUILDING
                       and s.polygon is not None and not s.polygon.is_empty]
    term_union = None
    try:
        if terminal_shapes:
            term_union = unary_union([t.polygon for t in terminal_shapes])
    except _GEOM_EXC:
        term_union = None
    absorbed = 0
    # Running clip unions: a piece absorbed into the APRON earlier in
    # this loop is apron footprint the next TERMINAL merge must not lap
    # onto (and vice versa) — the start-of-pass unions don't contain
    # it, and both sides' 0.6 m gap-bridges can otherwise claim the
    # same thin gap (KPHL terminal22 ∩ apron, ~0.1 m² sliver).
    apron_clip = apron_union
    term_clip = term_union
    for s in gs_shapes:
        p = s.polygon
        apron_f = _perimeter_frac_near(p, apron_union, radius_m)
        term_f = _perimeter_frac_near(p, term_union, radius_m)
        open_f = max(0.0, 1.0 - apron_f - term_f)
        # Enclosed (no open road/terrain frontage) AND touches apron OR is
        # mostly terminal-surrounded.
        if not (open_f <= _APRON_ISLAND_OPEN_MAX
                and (apron_f >= _APRON_ISLAND_APRON_MIN
                     or term_f >= _APRON_ISLAND_TERM_MAJORITY)):
            continue
        # Majority-terminal perimeter -> absorb into the TERMINAL (flat pad).
        if term_f >= _APRON_ISLAND_TERM_MAJORITY and terminal_shapes:
            host_t = _best_bordering_shape(p, terminal_shapes, radius_m)
            if host_t is not None and _merge_piece_into_terminal(
                    p, host_t, radius_m, clip_against=apron_clip):
                s.polygon = None
                absorbed += 1
                try:
                    term_clip = unary_union(
                        [g for g in (term_clip, host_t.polygon)
                         if g is not None])
                except _GEOM_EXC:
                    pass
                continue
        # Clip to the terminal footprint so aircraft apron never intrudes
        # under the building; keep the largest surviving piece.
        q = p
        if term_union is not None:
            try:
                d = p.difference(term_union)
            except _GEOM_EXC:
                d = p
            if d is None or d.is_empty:
                q = None
            elif d.geom_type == "Polygon":
                q = d
            elif d.geom_type == "MultiPolygon":
                parts = [g for g in d.geoms
                         if g.geom_type == "Polygon" and not g.is_empty]
                q = max(parts, key=lambda g: g.area) if parts else None
        if q is None or q.area < _GROUNDSIDE_MIN_AREA_M2:
            # Entirely under the terminal / too small once clipped — drop
            # it (mark the source shape empty; it is removed below).
            s.polygon = None
            absorbed += 1
            continue
        # Double-source coverage (s70 Phoenix triage): when the emitted
        # aprons ALREADY cover essentially the whole piece — apt.dat
        # apron and OSM groundside both map the same pocket — merging or
        # re-tagging emits a duplicate on top of pavement that is
        # already there (KLUF apron#74∩island#106 5 752 m², KSDL
        # #72∩#152 104 m²).  The piece is fully redundant: drop it.
        # Terminal-wedged islands (HECA) keep >1 % outside the aprons
        # and are unaffected.
        try:
            if q.difference(apron_union).area <= max(1.0, 0.01 * q.area):
                s.polygon = None
                absorbed += 1
                continue
        except _GEOM_EXC:
            pass
        # (user 2026-06-03) Genuinely MERGE the piece into the apron it borders
        # most — one continuous, node-shared polygon — instead of leaving a
        # standalone flat "apron-island" whose coincident-but-unshared vertices
        # tear into cliffs when the apron surface moves.  This MERGE is
        # geometry-only (no altitude needed) and is the PRE-solve path: the
        # solver grades the unified apron, so the cliff is gone at the source.
        # The flush standalone re-tag below is the fallback when there is no
        # apron to merge into.
        host = _best_bordering_shape(q, apron_shapes, radius_m)
        if host is not None and _merge_piece_into_apron(
                q, host, radius_m, clip_against=term_clip):
            s.polygon = None
            absorbed += 1
            try:
                apron_clip = unary_union(
                    [g for g in (apron_clip, host.polygon)
                     if g is not None])
            except _GEOM_EXC:
                pass
            continue
        # Standalone re-tag fallback (no bordering apron to merge into): the
        # piece becomes a flush apron-island at the mean representative
        # altitude of the neighbouring aprons.  PRE-solve the aprons have no
        # altitude yet → leave the piece as groundside (it gets a DEM altitude
        # + the separation gap) rather than guessing a flat level.
        try:
            halo = p.buffer(radius_m + 0.5)
            neigh = [_shape_repr_alt(a) for a in apron_shapes
                     if a.polygon.intersects(halo)]
        except _GEOM_EXC:
            neigh = []
        neigh = [a for a in neigh if a is not None]
        if not neigh:
            neigh = [a for a in (_shape_repr_alt(a) for a in apron_shapes)
                     if a is not None]
        if not neigh:
            continue                # no usable altitude — leave as gs
        alt = round(sum(neigh) / len(neigh), 1)
        s.polygon = q
        s.role = ROLE_APRON
        s.ref = "apron-island"
        s.altitude = alt
        s.node_altitudes = None
        s.altitude_high = None
        s.altitude_low = None
        absorbed += 1
    if absorbed:
        layout.shapes = [s for s in layout.shapes if s.polygon is not None]
    return absorbed



def _emit_groundside_pavement_dem(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        densify_step_m: float = 15.0,
        terminal_gap_m: float = 0.1,
        ) -> int:
    """Emit each saved groundside pavement polygon as a DEM-following
    shape with per-vertex altitudes.

    Per user 2026-04-29: pavement that wraps around the GROUNDSIDE
    of a terminal building (curbside, drop-off, parking) sits at a
    different elevation than the airside apron — at CYXY the
    terminal is cut into the hill so the airside apron is several
    metres lower than the road frontage.  The earlier subtraction
    pass (see ``_terminal_groundside_zone``) keeps the airside
    pavement clean of these strips, but they still belong in the
    output: they should render at local DEM elevation and should
    NOT touch the terminal building footprint (a 0.1 m gap is
    already applied during capture).

    Implementation:
      1. Iterate ``layout._groundside_polys`` (captured during
         ``build_airport_pavement`` immediately before the
         groundside subtraction).
      2. Densify each polygon's exterior to ``densify_step_m`` so
         per-vertex altitudes resolve at the same spatial
         frequency as the boundary ribbon (15 m step → typical
         curbside has 5–10 vertices per side).
      3. Sample DEM at every vertex; emit as ``BuiltShape`` with
         role ``ROLE_GROUNDSIDE_PAVEMENT``, ``node_altitudes`` set,
         and ``altitude``/``altitude_high``/``altitude_low`` left
         None so the OSM emitter writes per-vertex altitude tags.

    Returns the number of polygons emitted.
    """
    from .pipeline import _load_osm_big_roads
    polys = list(getattr(layout, "_groundside_polys", []) or [])
    if not polys:
        return 0
    # Build a buffered union of every emitted terminal shape — we
    # subtract this from each groundside polygon so the result
    # leaves a ``terminal_gap_m`` clearance to every actual
    # terminal polygon in the final layout.  Using layout shapes
    # (not OSM source) handles cases where apt.dat row-110 /
    # DSF residue absorption produced a slightly different ring.
    _term_buf = None
    try:
        _t_polys = [s.polygon for s in layout.shapes
                    if s.role == ROLE_BUILDING
                    and s.polygon is not None
                    and not s.polygon.is_empty]
        if _t_polys:
            _term_buf = unary_union(
                [tp.buffer(terminal_gap_m) for tp in _t_polys])
            if _term_buf.is_empty:
                _term_buf = None
    except _GEOM_EXC:
        _term_buf = None
    # Also subtract every other pavement-bearing layout shape so
    # the groundside pavement never overlaps a rect / junction /
    # apron / runway / terminal / wall / ramp.  The boundary
    # ribbon is excluded — by design it traces over everything.
    NON_OVERLAP_ROLES = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
        ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_APRON, ROLE_JUNCTION,
        ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
    }
    _other_buf = None
    try:
        _other_polys = [s.polygon for s in layout.shapes
                        if s.role in NON_OVERLAP_ROLES
                        and s.polygon is not None
                        and not s.polygon.is_empty]
        if _other_polys:
            _other_buf = unary_union(_other_polys)
            if _other_buf.is_empty:
                _other_buf = None
    except _GEOM_EXC:
        _other_buf = None
    cuts = []
    if _term_buf is not None:
        cuts.append(_term_buf)
    if _other_buf is not None:
        cuts.append(_other_buf)
    if cuts:
        try:
            cut_union = unary_union(cuts) if len(cuts) > 1 else cuts[0]
        except _GEOM_EXC:
            cut_union = None
        if cut_union is not None and not cut_union.is_empty:
            clipped: List[Polygon] = []
            for p in polys:
                try:
                    q = p.difference(cut_union)
                except _GEOM_EXC:
                    continue
                if q is None or q.is_empty:
                    continue
                if q.geom_type == "Polygon":
                    if q.area >= 5.0:
                        clipped.append(q)
                elif q.geom_type == "MultiPolygon":
                    for g in q.geoms:
                        if (g.geom_type == "Polygon"
                                and not g.is_empty
                                and g.area >= 5.0):
                            clipped.append(g)
            polys = clipped
    if not polys:
        return 0
    # Two captured groundside polygons can themselves overlap (the cut
    # above only subtracts terminals / airside, not other groundside) —
    # leaving a self-overlap (HECA #2222 ∩ #2223, 0.1 m²).  Clip each
    # against the union of already-accepted polygons (largest first, so
    # the smaller piece yields) so groundside never overlaps groundside.
    polys.sort(key=lambda g: -g.area)
    _emitted_union = None
    deconflicted: List[Polygon] = []
    for p in polys:
        if _emitted_union is not None:
            try:
                q = p.difference(_emitted_union)
            except _GEOM_EXC:
                q = p
            if q is None or q.is_empty:
                continue
            if q.geom_type == "Polygon":
                p = q if q.area >= 5.0 else None
            elif q.geom_type == "MultiPolygon":
                pieces = [g for g in q.geoms
                          if g.geom_type == "Polygon" and g.area >= 5.0]
                p = max(pieces, key=lambda g: g.area) if pieces else None
            else:
                p = None
            if p is None:
                continue
        deconflicted.append(p)
        try:
            _emitted_union = (p if _emitted_union is None
                              else unary_union([_emitted_union, p]))
        except _GEOM_EXC:
            _emitted_union = p
    polys = deconflicted
    if not polys:
        return 0
    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    n_emitted = 0
    for p in polys:
        built = _dem_follow_polygon(p, _dem_at, densify_step_m)
        if built is None:
            continue
        new_poly, node_alts = built
        layout.shapes.append(BuiltShape(
            polygon=new_poly,
            role=ROLE_GROUNDSIDE_PAVEMENT,
            ref="groundside",
            node_altitudes=node_alts))
        n_emitted += 1
    return n_emitted


def _reclassify_groundside_orphan_junctions(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        vertex_match_tol_m: float = 0.5,
        ) -> int:
    """RECLASSIFY junction polygons that connect ONLY to groundside
    pavement (no path through shared vertices to any airside rect /
    runway / terminal) into DEM-following groundside pavement.

    Per user 2026-04-29 (CYXY -10111 + -10115): the rect/junction
    tessellator can leave junction polygons sitting next to a groundside
    polygon when the apt.dat row-110 / DSF union has pavement outside the
    groundside-zone subtraction's perpendicular extent.  Those junctions
    get the airside-flat altitude during the solver (they were classified
    airside even though they don't touch any airside pavement), then they
    share an edge with the DEM-following groundside polygon at an altitude
    mismatch — X-Plane renders that as a cliff.

    Earlier versions DROPPED these junctions.  That was wrong (user
    2026-05-21): pav_union is the source of truth and these junctions
    cover REAL pavement — at HECA the DSF adds large terminal aprons with
    no taxi centerline that are vertex-disconnected from the airside
    network, and dropping them erased ~44k m² of genuine apron.  Instead
    we KEEP the pavement and re-elevate it to follow the DEM (like the
    groundside ribbon it abuts), which both preserves coverage AND removes
    the cliff — the original goal.

    Detection rule (a junction is reclassified if BOTH):
        1. It shares ≥1 vertex with a ``ROLE_GROUNDSIDE_PAVEMENT``
           polygon.
        2. It does NOT share any vertex with an airside seed shape
           (runway / primary_parallel / secondary_parallel / stub /
           cross_connector / terminal), directly or transitively through
           other junction polygons (BFS over junction-junction shared
           vertices) — so genuine apron→runway/terminal connectors are
           left airside (SPJC primary_parallels U and M relied on this).

    Returns the number of junctions reclassified.
    """
    # APRON is airside (aircraft pavement), user 2026-05-22: a junction
    # abutting an apron is airside-connected, so it must NOT be
    # reclassified to groundside (groundside = cars/buildings).  Without
    # APRON here, large no-centerline terminal *aircraft* aprons were
    # reclassified to groundside and ended up sharing nodes/edges (and
    # overlapping) airside aprons — violating the no-shared-boundary
    # invariant.  With it, they stay airside (kept as junction → apron),
    # and only junctions touching ONLY groundside become groundside.
    AIRSIDE_SEED_ROLES = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB,
        ROLE_CROSS_CONNECTOR, ROLE_BUILDING, ROLE_APRON,
    }
    bucket_size = vertex_match_tol_m

    def _verts_buckets(s: "BuiltShape") -> List[Tuple[int, int]]:
        if s.polygon is None or s.polygon.is_empty:
            return []
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            return []
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        out = []
        for x, y in coords:
            out.append((int(round(x / bucket_size)),
                        int(round(y / bucket_size))))
        return out
    # Index every junction's vertex buckets (1-bucket halo so
    # near-misses still match neighbours).
    junction_idxs = [i for i, s in enumerate(layout.shapes)
                      if s.role == ROLE_JUNCTION
                      and s.polygon is not None
                      and not s.polygon.is_empty]
    if not junction_idxs:
        return 0
    junction_buckets: Dict[int, set] = {}
    bucket_to_jidx: Dict[Tuple[int, int], List[int]] = {}
    for ji in junction_idxs:
        bs = _verts_buckets(layout.shapes[ji])
        halo: set = set()
        for bx, by in bs:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    halo.add((bx + dx, by + dy))
        junction_buckets[ji] = halo
        for b in bs:
            bucket_to_jidx.setdefault(b, []).append(ji)
    # Airside seeds (rect / runway / terminal vertex buckets).
    seed_buckets: set = set()
    for s in layout.shapes:
        if s.role not in AIRSIDE_SEED_ROLES:
            continue
        for b in _verts_buckets(s):
            seed_buckets.add(b)
    # Build airside connectivity component over junctions: BFS
    # starting from junctions that share a bucket with any
    # airside seed, propagating through junction-junction
    # shared buckets.
    airside_set: set = set()
    for ji in junction_idxs:
        if junction_buckets[ji] & seed_buckets:
            airside_set.add(ji)
    queue = list(airside_set)
    while queue:
        ji = queue.pop()
        for b in junction_buckets[ji]:
            for kj in bucket_to_jidx.get(b, []):
                if kj in airside_set:
                    continue
                airside_set.add(kj)
                queue.append(kj)
    # Groundside vertex buckets (1-bucket halo).
    gs_buckets: set = set()
    for s in layout.shapes:
        if s.role != ROLE_GROUNDSIDE_PAVEMENT:
            continue
        for bx, by in _verts_buckets(s):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    gs_buckets.add((bx + dx, by + dy))
    if not gs_buckets:
        return 0
    orphan_set: set = set()
    for ji in junction_idxs:
        if ji in airside_set:
            continue
        if junction_buckets[ji] & gs_buckets:
            orphan_set.add(ji)
    if not orphan_set:
        return 0
    # Building union (buffered to the groundside clearance): an orphan
    # junction can fully enclose a building footprint, and re-roling it to
    # groundside verbatim would cover that building — a 100 %-contained
    # self-overlap once emitted (the OSM emitter drops interior rings, so
    # the hole a later ``_separate`` cut would leave does not survive).
    # Subtract buildings HERE so the re-roled groundside honours the gap at
    # its source, like every other groundside rebuild path.
    _bldg_buf = None
    try:
        _bpolys = [b.polygon for b in layout.shapes
                   if b.role == ROLE_BUILDING
                   and b.polygon is not None and not b.polygon.is_empty]
        if _bpolys:
            _bldg_buf = unary_union(
                [bp.buffer(GROUNDSIDE_CLEARANCE_M) for bp in _bpolys])
            if _bldg_buf.is_empty:
                _bldg_buf = None
    except _GEOM_EXC:
        _bldg_buf = None
    # Re-elevate each orphan junction to follow the DEM and reclassify it
    # as groundside pavement — keep the pavement, lose the cliff.  If the
    # DEM-follow can't be built, LEAVE the shape unchanged (never erase
    # real pavement).
    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    n = 0
    new_shapes: List["BuiltShape"] = []
    for ji in orphan_set:
        s = layout.shapes[ji]
        # Cut buildings out first — the result may be a polygon with a hole
        # (opened by ``_dem_follow_polygon``) or split into several pieces.
        src_polys: List[Polygon] = [s.polygon]
        if _bldg_buf is not None:
            try:
                diff = s.polygon.difference(_bldg_buf)
            except _GEOM_EXC:
                diff = s.polygon
            if diff is not None and not diff.is_empty:
                src_polys = ([diff] if diff.geom_type == "Polygon"
                             else [g for g in getattr(diff, "geoms", [])
                                   if g.geom_type == "Polygon"
                                   and not g.is_empty
                                   and g.area >= _GROUNDSIDE_MIN_AREA_M2])
        builts = []
        for sp in src_polys:
            b = _dem_follow_polygon(sp, _dem_at)
            if b is not None:
                builts.append(b)
        if not builts:
            continue          # never erase real pavement — leave unchanged
        # Largest piece keeps the shape's identity; extras are appended.
        builts.sort(key=lambda t: -t[0].area)
        new_poly, node_alts = builts[0]
        s.polygon = new_poly
        s.role = ROLE_GROUNDSIDE_PAVEMENT
        s.ref = "groundside"
        s.node_altitudes = node_alts
        for extra_poly, extra_alts in builts[1:]:
            new_shapes.append(BuiltShape(
                polygon=extra_poly, role=ROLE_GROUNDSIDE_PAVEMENT,
                ref="groundside", node_altitudes=extra_alts))
        n += 1
    layout.shapes.extend(new_shapes)
    return n


# Clearance (m) groundside pavement must keep from any terminal / airside
# polygon (user 2026-05-22): groundside is for cars/buildings and follows
# the DEM, so it sits at a different elevation than the graded airside and
# must NOT share a node or edge with it.  A clearance just over the
# shared-vertex snap tolerance guarantees separation (no shared node after
# snapping, no degenerate seam slivers in Triangle4XP).
GROUNDSIDE_CLEARANCE_M = SHARED_VERTEX_TOL_M + 0.5  # 1.0 m
_GROUNDSIDE_MIN_AREA_M2 = 5.0


def _merge_touching_groundside(
        layout: "PavementLayout", dem, tile_lat: int, tile_lon: int,
        touch_tol: float = 0.5, min_shared_m: float = 2.0) -> int:
    """Merge groundside pavement pieces that share a real boundary into ONE
    shape (user 2026-06-26).  Groundside is DEM-following pavement with no spine
    or internal structure, so two pieces sharing a ≥``min_shared_m`` boundary were
    SPLIT upstream (junction-emit ``pav_union.difference(rects)`` / overlap-clip on
    a multi-polygon source union) — they should be a single surface (CYXY parking
    lot @(-465,408): two pieces 899+1276 m² touching along a 55 m seam).  Pieces
    that merely touch at a point are left alone (no ``min_shared_m`` seam).
    """
    if _os.environ.get("O4_MERGE_GROUNDSIDE", "1") != "1":
        return 0
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    gs = [s for s in layout.shapes
          if s.role == ROLE_GROUNDSIDE_PAVEMENT and s.polygon is not None
          and not s.polygon.is_empty and s.polygon.geom_type == "Polygon"]
    if len(gs) < 2:
        return 0
    polys = [s.polygon for s in gs]
    n = len(gs)
    parent = list(range(n))

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tree = STRtree(polys)
    for i in range(n):
        try:
            cand = tree.query(polys[i].buffer(touch_tol))
        except _GEOM_EXC:
            continue
        for qj in cand:
            j = int(qj)
            if j <= i:
                continue
            try:
                if polys[i].distance(polys[j]) > touch_tol:
                    continue
                # NEAR-coincident flush runs count as shared boundary
                # (user 2026-07-04, CYXY P4): a demoted connector meets
                # the lot it serves within millimetres but rarely
                # EXACTLY, so the exact ring∩ring length reads 0 while
                # the pieces are physically one surface — left unmerged,
                # their independent DEM-follow/shift left coincident
                # nodes 2.6 m apart.  Measure the run of ring i within
                # ``touch_tol`` of ring j instead (identity on exactly-
                # shared boundaries).
                shared = polys[i].exterior.intersection(
                    polys[j].exterior.buffer(touch_tol))
                if getattr(shared, "length", 0.0) < min_shared_m:
                    continue            # point/sliver touch — not a split seam
            except _GEOM_EXC:
                continue
            ri, rj = _find(i), _find(j)
            if ri != rj:
                parent[ri] = rj

    groups: dict = {}
    for i in range(n):
        groups.setdefault(_find(i), []).append(i)

    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    merged_objs: set = set()
    new_shapes: list = []
    n_merged = 0
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        try:
            # Snap each piece onto the accumulated union before
            # unioning: near-coincident flush pairs (see above) stay
            # TWO polygons under a plain union — the sub-tolerance
            # hairline between them never dissolves.  ``snap`` moves
            # only vertices already within ``touch_tol`` of the other
            # ring; exactly-shared boundaries are untouched.
            u = polys[idxs[0]]
            for k in idxs[1:]:
                u = unary_union([u, snap(polys[k], u, touch_tol)])
        except _GEOM_EXC:
            continue
        if u.is_empty:
            continue
        pieces = ([u] if u.geom_type == "Polygon"
                  else [g for g in getattr(u, "geoms", []) if g.geom_type == "Polygon"])
        if not pieces:
            continue
        for k in idxs:
            merged_objs.add(id(gs[k]))
        for p in pieces:
            built = _dem_follow_polygon(p, _dem_at, simplify_tol=0.0)
            if built is None:
                continue
            np_, na = built
            new_shapes.append(BuiltShape(
                polygon=np_, role=ROLE_GROUNDSIDE_PAVEMENT,
                ref="groundside", node_altitudes=na))
        n_merged += len(idxs) - 1
    if not merged_objs:
        return 0
    layout.shapes = [s for s in layout.shapes
                     if id(s) not in merged_objs] + new_shapes
    return n_merged


def _separate_groundside_from_airside(
        layout: "PavementLayout", dem, tile_lat: int, tile_lon: int,
        clearance: float = GROUNDSIDE_CLEARANCE_M,
        preserve_field: bool = False) -> int:
    """Clip every groundside polygon so it keeps ``clearance`` from all
    terminal / airside pavement — enforcing the invariant that groundside
    shares no node or edge with terminal or airside (it is separate
    car/building pavement at DEM elevation).  Re-derives DEM + grade-
    limited altitudes for the clipped result.  Returns shapes clipped.

    Robust to the non-conformance case the apron-seed rule can't catch:
    a groundside polygon that *overlaps* airside without sharing a vertex
    is still cut back to the clearance gap.

    ``preserve_field=True`` (the POST-solve call sites): a clipped piece
    keeps its existing altitude FIELD — each rebuilt vertex takes
    ``DEM + deviation-of-nearest-original-vertex`` instead of a raw
    DEM re-follow.  Post-solve, groundside carries the reach re-level /
    chord-limit / weld field; resetting a clipped piece to raw DEM
    detached it from every road welded to it at solve time (CYXY: a
    mouth lot reset 695.8 → 700.6 = 5 m road↔lot yanks).
    """
    AIRSIDE_ROLES = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
        ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_APRON, ROLE_JUNCTION,
        ROLE_BUILDING, ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
    }
    # Groundside MAY share an edge with a SERVICE ROAD / junction (user
    # 2026-06-26): a parking lot is SERVED by its service road, so they touch —
    # opening the 1 m clearance gap there DISCONNECTS the road from the lot it
    # feeds (CYXY SVC1 ↔ lot @(-472,404): a cliff across the gap).  Groundside is
    # still cut back from BUILDINGS (kept above) and aircraft pavement.  Off =
    # legacy (clearance from roads too).
    _share_svc = _os.environ.get("O4_GROUNDSIDE_SHARE_SVC", "1") == "1"
    if not _share_svc:
        AIRSIDE_ROLES |= {ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION}
    # Truck-route END mouths (user ruling 2026-07-04, CYXY P4): a lot is
    # groundside BECAUSE its connection is a service road — the connection
    # is identified EARLY and the clearance gap is never cut across it.
    # Apron/junction pavement carrying a truck-route END abuts the lot
    # that route serves; the demotion sweeps later re-role that connector
    # to groundside, but a gap cut now is never re-closed, leaving the
    # connector and its lot two disjoint DEM-followed surfaces (CYXY
    # route N: 165 m² connector 1.00 m from its 6.8 k m² lot).  Such a
    # shape keeps the shared edge inside a mouth window around the route
    # end: its clearance buffer is subtracted there and the RAW polygon
    # joins the clip instead (overlap still trimmed, touching edge
    # survives — the same treatment service shapes get below).
    _keep_route_end_edge = _os.environ.get(
        "O4_GROUNDSIDE_ROUTE_END_EDGE", "1") == "1"
    _MOUTH_END_ON_PAVEMENT_TOL_M = 1.0
    _MOUTH_WINDOW_RADIUS_M = 15.0
    _DEMOTABLE_ROLES = {ROLE_APRON, ROLE_JUNCTION}
    route_end_points: list = []
    if _keep_route_end_edge:
        for centerline in (getattr(layout, "apt_service_centerlines", None)
                           or []):
            line = getattr(centerline, "line", None)
            if line is None or line.is_empty:
                continue
            try:
                route_end_points.append(Point(*line.coords[0]))
                route_end_points.append(Point(*line.coords[-1]))
            except (ValueError, IndexError):
                continue
    clip_polys = []
    for s in layout.shapes:
        if s.role in AIRSIDE_ROLES and s.polygon is not None \
                and not s.polygon.is_empty:
            try:
                # Mitre join: the buffered boundary is straight-edged
                # (no rounded-corner arc segments), so the clip cut
                # doesn't introduce sub-metre edges that would inflate the
                # per-vertex grade after altitude rounding.
                buffered = s.polygon.buffer(clearance, join_style=2)
                mouth_ends = ([ep for ep in route_end_points
                               if s.polygon.distance(ep)
                               <= _MOUTH_END_ON_PAVEMENT_TOL_M]
                              if s.role in _DEMOTABLE_ROLES else [])
                if mouth_ends:
                    # Square windows (shapely ``box``), not point buffers:
                    # the window edge is part of the clip boundary and
                    # must stay arc-free for the same reason as the mitre
                    # join above.
                    windows = unary_union([
                        box(ep.x - _MOUTH_WINDOW_RADIUS_M,
                            ep.y - _MOUTH_WINDOW_RADIUS_M,
                            ep.x + _MOUTH_WINDOW_RADIUS_M,
                            ep.y + _MOUTH_WINDOW_RADIUS_M)
                        for ep in mouth_ends])
                    buffered = buffered.difference(windows)
                    clip_polys.append(s.polygon)
                clip_polys.append(buffered)
            except _GEOM_EXC:
                continue
    # Groundside may TOUCH a service road (shared edge, kept above) but must not
    # OVERLAP it (area overlap = self-overlap, not a shared edge — e.g. a curved
    # SVC connector emitted as service_junction that straddles the lot it feeds).
    # Add the service polys at ZERO clearance so an overlap is trimmed while the
    # touching edge survives (no disconnecting gap).
    if _share_svc:
        for s in layout.shapes:
            if s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION) \
                    and s.polygon is not None and not s.polygon.is_empty:
                clip_polys.append(s.polygon)
    if not clip_polys:
        return 0
    try:
        clip = unary_union(clip_polys)
    except _GEOM_EXC:
        return 0
    if clip is None or clip.is_empty:
        return 0
    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    out_shapes = []
    n_clipped = 0
    for s in layout.shapes:
        if s.role != ROLE_GROUNDSIDE_PAVEMENT or s.polygon is None \
                or s.polygon.is_empty:
            out_shapes.append(s)
            continue
        try:
            diff = s.polygon.difference(clip)
        except _GEOM_EXC:
            out_shapes.append(s)
            continue
        if diff.is_empty:
            n_clipped += 1            # entirely inside the gap → drop
            continue
        parts = ([diff] if diff.geom_type == "Polygon"
                 else list(getattr(diff, "geoms", [])))
        # Original per-vertex DEVIATION field (alt − DEM), for the
        # preserve_field rebuild below.
        orig_field = []
        if preserve_field and s.node_altitudes:
            orig_ring = list(s.polygon.exterior.coords)
            for kv in range(min(len(orig_ring), len(s.node_altitudes))):
                a = s.node_altitudes[kv]
                if a is None:
                    continue
                ox, oy = orig_ring[kv]
                dv = _dem_at(ox, oy)
                if dv is not None:
                    orig_field.append((ox, oy, float(a) - dv))
        changed = False
        kept = []
        for part in parts:
            if part.geom_type != "Polygon" or part.is_empty \
                    or part.area < _GROUNDSIDE_MIN_AREA_M2:
                changed = True
                continue
            if part.equals(s.polygon):
                kept.append(s)        # untouched
                continue
            # No re-simplify: the source groundside was already 2 m-
            # simplified at emit, and re-simplifying would move the
            # boundary back across the clearance gap.  The mitre-buffered
            # clip above already yields clean straight edges.
            built = _dem_follow_polygon(part, _dem_at, simplify_tol=0.0)
            if built is None:
                continue
            np_, na = built
            if orig_field:
                # Preserve the solved field: DEM here + the deviation of
                # the nearest ORIGINAL vertex (a rigid local carry of the
                # reach shift / chord limit across the clip rebuild).
                new_ring = list(np_.exterior.coords)
                for kv in range(min(len(new_ring), len(na))):
                    nx, ny = new_ring[kv]
                    dv = _dem_at(nx, ny)
                    if dv is None:
                        continue
                    dev = min(orig_field,
                              key=lambda t: (t[0] - nx) ** 2
                              + (t[1] - ny) ** 2)[2]
                    na[kv] = round(dv + dev, 2)
                if len(na) == len(new_ring) and len(new_ring) > 1 \
                        and new_ring[0] == new_ring[-1]:
                    na[-1] = na[0]
            kept.append(BuiltShape(
                polygon=np_, role=ROLE_GROUNDSIDE_PAVEMENT,
                ref="groundside", node_altitudes=na))
            changed = True
        out_shapes.extend(kept)
        if changed:
            n_clipped += 1
    layout.shapes = out_shapes
    return n_clipped


def _clip_shape_yielding_to(ys, kept_polygon):
    """Clip shape ``ys`` so it yields its overlap with ``kept_polygon``:
    snap-then-difference (the contact chain passes exactly through the
    kept vertices), largest surviving part, kept-vertex projections
    inserted on the new ring (no residual T-junction), and
    ``node_altitudes`` carried from the nearest original vertex.

    Returns the new ``Polygon`` (``ys`` already mutated), or ``None``
    when nothing survives — the yielder lies (essentially) wholly
    inside the kept geometry and the caller should drop it.
    Extracted verbatim from the service↔service loop so the
    senior-pavement stage shares one clip semantics."""
    try:
        diff = snap(ys.polygon, kept_polygon, 0.25).difference(
            kept_polygon)
    except _GEOM_EXC:
        return ys.polygon
    parts = ([diff] if diff.geom_type == "Polygon"
             else [g for g in getattr(diff, "geoms", ())
                   if g.geom_type == "Polygon"])
    parts = [g for g in parts if g.area >= 1.0]
    if not parts:
        return None
    new_poly = max(parts, key=lambda g: g.area)
    new_ring = list(new_poly.exterior.coords)
    if new_ring and new_ring[0] == new_ring[-1]:
        new_ring = new_ring[:-1]
    kept_ring = list(kept_polygon.exterior.coords)
    inserts = []          # (segment index, u along segment, point)
    for (kx, ky) in kept_ring:
        if any(math.hypot(kx - nx, ky - ny) <= 0.02
               for (nx, ny) in new_ring):
            continue
        best = None
        for t in range(len(new_ring)):
            ax, ay = new_ring[t]
            bx, by = new_ring[(t + 1) % len(new_ring)]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            if seg2 < 1e-9:
                continue
            u = ((kx - ax) * dx + (ky - ay) * dy) / seg2
            u = min(1.0, max(0.0, u))
            px, py = ax + u * dx, ay + u * dy
            d = math.hypot(kx - px, ky - py)
            if best is None or d < best[0]:
                best = (d, t, u, (px, py))
        if best is not None and best[0] <= 0.25:
            inserts.append(best[1:])
    for (t, u, pt) in sorted(inserts, key=lambda e: (-e[0], -e[1])):
        new_ring.insert(t + 1, pt)
    try:
        new_poly = Polygon(new_ring)
    except _GEOM_EXC:
        pass
    old_ring = list(ys.polygon.exterior.coords)
    old_alts = list(ys.node_altitudes or [])
    ys.polygon = new_poly
    if old_alts and len(old_alts) >= len(old_ring) - 1:
        out_ring = list(new_poly.exterior.coords)
        new_alts = []
        for (nx, ny) in out_ring:
            best_k = min(
                range(min(len(old_ring), len(old_alts))),
                key=lambda t: (old_ring[t][0] - nx) ** 2
                + (old_ring[t][1] - ny) ** 2)
            new_alts.append(old_alts[best_k])
        ys.node_altitudes = new_alts
    return new_poly


def _deconflict_service_overlaps(
        layout: "PavementLayout", min_overlap_m2: float = 1e-3) -> int:
    """Clip lens-scale overlaps between SERVICE shapes (last word, before
    emit).  The canonical vertex weld can cross two near-coincident
    service boundaries whose contact chains carry different vertex
    sequences (CYXY: a corridor-converted road hugging the strip-carved
    junction it was trimmed against — 0.38 m² lens after welding), and no
    earlier pass owns service↔service overlap.  Larger piece is
    canonical; the smaller piece yields the overlap.  Altitudes for the
    rebuilt ring carry over from the nearest original vertex (the
    surfaces are welded along the contact, so the values agree there).
    Returns the number of shapes clipped."""
    svc = [(i, s) for i, s in enumerate(layout.shapes)
           if s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)
           and s.polygon is not None and not s.polygon.is_empty
           and s.polygon.geom_type == "Polygon"]
    if len(svc) < 2:
        return 0
    from shapely.strtree import STRtree
    polys = [s.polygon for _i, s in svc]
    tree = STRtree(polys)
    n_clipped = 0
    drop_ids: set = set()   # yielders wholly inside a kept shape → removed
    for a in range(len(svc)):
        ia, sa = svc[a]
        if id(sa) in drop_ids:
            continue
        try:
            cand = tree.query(sa.polygon)
        except _GEOM_EXC:
            continue
        for qb in cand:
            b = int(qb)
            if b <= a:
                continue
            ib, sb = svc[b]
            if id(sb) in drop_ids:
                continue
            try:
                overlap = sa.polygon.intersection(sb.polygon).area
            except _GEOM_EXC:
                continue
            if overlap <= min_overlap_m2:
                continue
            # smaller yields
            yi, ys = (ia, sa) if sa.polygon.area < sb.polygon.area \
                else (ib, sb)
            ki, ks = (ib, sb) if ys is sa else (ia, sa)
            new_poly = _clip_shape_yielding_to(ys, ks.polygon)
            if new_poly is None:
                # Nothing survives the difference → the yielder lies
                # (essentially) WHOLLY inside the kept shape.  A plain
                # ``continue`` here left the fully-covered yielder in place
                # (KEQY: service_junction #23, 109 m², entirely inside #21) —
                # a 100 %-area self-overlap.  Drop the redundant yielder: the
                # kept shape already covers its footprint at the same role, so
                # removing it loses no coverage and kills the overlap.
                drop_ids.add(id(ys))
                n_clipped += 1
                continue
            # (Snap-difference, largest part, kept-vertex conformance
            # inserts and altitude carry-over all happen inside
            # ``_clip_shape_yielding_to`` — one clip semantics shared
            # with the senior-pavement stage below.)
            # keep the STRtree list coherent for later pairs
            polys[a if ys is sa else b] = new_poly
            n_clipped += 1

    # SENIOR-PAVEMENT SENIORITY (2026-07-17, SPJC apron #89 ∩
    # service_junction #96, 9.4 m²): a service shape overlapping APRON
    # or JUNCTION pavement YIELDS its overlap — the apron-edge-service
    # ruling grades service portions inside an apron as apron anyway,
    # and no earlier pass owns the cross-role pair (the fixed-shape
    # overlap ladder has no service tier; the groundside separation
    # cuts only groundside).  Same clip semantics as service↔service.
    from .layout import ROLE_APRON as _R_AP, ROLE_JUNCTION as _R_JN
    senior_polys = [s.polygon for s in layout.shapes
                    if s.role in (_R_AP, _R_JN)
                    and s.polygon is not None and not s.polygon.is_empty
                    and s.polygon.geom_type == "Polygon"]
    if senior_polys:
        senior_tree = STRtree(senior_polys)
        for s in layout.shapes:
            if (s.role not in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)
                    or id(s) in drop_ids or s.polygon is None
                    or s.polygon.is_empty
                    or s.polygon.geom_type != "Polygon"):
                continue
            try:
                candidates = senior_tree.query(s.polygon)
            except _GEOM_EXC:
                continue
            for qk in candidates:
                kept_polygon = senior_polys[int(qk)]
                try:
                    overlap = s.polygon.intersection(kept_polygon).area
                except _GEOM_EXC:
                    continue
                if overlap <= min_overlap_m2:
                    continue
                new_poly = _clip_shape_yielding_to(s, kept_polygon)
                if new_poly is None:
                    drop_ids.add(id(s))
                    n_clipped += 1
                    break
                n_clipped += 1

    if drop_ids:
        layout.shapes = [s for s in layout.shapes if id(s) not in drop_ids]
    return n_clipped


def _deconflict_groundside_overlaps(
        layout: "PavementLayout", dem, tile_lat: int, tile_lon: int,
        min_overlap_m2: float = 0.5) -> int:
    """Clip overlapping groundside-vs-groundside pavement so no two
    groundside polygons share interior area.

    ``_separate_groundside_from_airside`` removes groundside↔airside
    overlap but never groundside↔groundside — an orphan junction
    reclassified to groundside, or two independently DEM-followed
    pieces, can overlap each other (LMML: piece #238 covered #185 by
    32.7 m² and #182 by 3.0 m²).  Larger pieces are canonical; each
    smaller piece YIELDS the overlap (subtract the running union of the
    already-kept larger pieces), is rebuilt with DEM altitudes, and
    sub-minimum remnants are dropped.  Pieces that merely ABUT a larger
    one are untouched (difference of a touching polygon is a no-op).

    Returns the number of groundside shapes modified or dropped."""
    gs = [(i, s) for i, s in enumerate(layout.shapes)
          if s.role == ROLE_GROUNDSIDE_PAVEMENT
          and s.polygon is not None and not s.polygon.is_empty]
    if len(gs) < 2:
        return 0
    # Largest-first, deterministic index tie-break.
    order = sorted(gs, key=lambda t: (-t[1].polygon.area, t[0]))
    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    kept_union = None
    replace: Dict[int, list] = {}   # original idx → [BuiltShape, …] ([] = drop)
    n_mod = 0
    for i, s in order:
        poly = s.polygon
        if kept_union is not None and not kept_union.is_empty:
            try:
                overlap = poly.intersection(kept_union).area
            except _GEOM_EXC:
                overlap = 0.0
            if overlap > min_overlap_m2:
                try:
                    diff = poly.difference(kept_union)
                except _GEOM_EXC:
                    diff = None
                parts = ([] if diff is None or diff.is_empty
                         else [diff] if diff.geom_type == "Polygon"
                         else list(getattr(diff, "geoms", [])))
                new_pieces = []
                for part in parts:
                    if (part.geom_type != "Polygon" or part.is_empty
                            or part.area < _GROUNDSIDE_MIN_AREA_M2):
                        continue
                    built = _dem_follow_polygon(part, _dem_at,
                                                simplify_tol=0.0)
                    if built is None:
                        continue
                    np_, na = built
                    new_pieces.append(BuiltShape(
                        polygon=np_, role=ROLE_GROUNDSIDE_PAVEMENT,
                        ref="groundside", node_altitudes=na))
                replace[i] = new_pieces
                n_mod += 1
                try:
                    poly = (unary_union([p.polygon for p in new_pieces])
                            if new_pieces else None)
                except _GEOM_EXC:
                    poly = None
        if poly is not None and not poly.is_empty:
            try:
                kept_union = (poly if kept_union is None
                              else unary_union([kept_union, poly]))
            except _GEOM_EXC:
                pass
    if not replace:
        return 0
    out = []
    for i, s in enumerate(layout.shapes):
        if i in replace:
            out.extend(replace[i])
        else:
            out.append(s)
    layout.shapes = out
    return n_mod


