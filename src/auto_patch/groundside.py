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
from typing import Dict, List, Optional, Sequence, Set, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import linemerge, nearest_points, unary_union

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
    ROLE_STUB,
    ROLE_TERMINAL,
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
    "_reclassify_groundside_orphan_junctions",
    "_emit_groundside_pavement_dem",
    "_separate_groundside_from_airside",
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
    # user 2026-05-22) before rounding.
    alts = _grade_limit_ring(rebuilt, alts, GROUNDSIDE_MAX_GRADE)
    alts = [round(float(a), 1) for a in alts]
    return new_poly, alts + [alts[0]]


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
                    if s.role == ROLE_TERMINAL
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
        ROLE_CROSS_CONNECTOR, ROLE_TERMINAL, ROLE_APRON,
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
    # Re-elevate each orphan junction to follow the DEM and reclassify it
    # as groundside pavement — keep the pavement, lose the cliff.  If the
    # DEM-follow can't be built, LEAVE the shape unchanged (never erase
    # real pavement).
    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    n = 0
    for ji in orphan_set:
        s = layout.shapes[ji]
        built = _dem_follow_polygon(s.polygon, _dem_at)
        if built is None:
            continue
        new_poly, node_alts = built
        s.polygon = new_poly
        s.role = ROLE_GROUNDSIDE_PAVEMENT
        s.ref = "groundside"
        s.node_altitudes = node_alts
        n += 1
    return n


# Clearance (m) groundside pavement must keep from any terminal / airside
# polygon (user 2026-05-22): groundside is for cars/buildings and follows
# the DEM, so it sits at a different elevation than the graded airside and
# must NOT share a node or edge with it.  A clearance just over the
# shared-vertex snap tolerance guarantees separation (no shared node after
# snapping, no degenerate seam slivers in Triangle4XP).
GROUNDSIDE_CLEARANCE_M = SHARED_VERTEX_TOL_M + 0.5  # 1.0 m
_GROUNDSIDE_MIN_AREA_M2 = 5.0


def _separate_groundside_from_airside(
        layout: "PavementLayout", dem, tile_lat: int, tile_lon: int,
        clearance: float = GROUNDSIDE_CLEARANCE_M) -> int:
    """Clip every groundside polygon so it keeps ``clearance`` from all
    terminal / airside pavement — enforcing the invariant that groundside
    shares no node or edge with terminal or airside (it is separate
    car/building pavement at DEM elevation).  Re-derives DEM + grade-
    limited altitudes for the clipped result.  Returns shapes clipped.

    Robust to the non-conformance case the apron-seed rule can't catch:
    a groundside polygon that *overlaps* airside without sharing a vertex
    is still cut back to the clearance gap.
    """
    AIRSIDE_ROLES = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
        ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_APRON, ROLE_JUNCTION,
        ROLE_TERMINAL, ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
    }
    clip_polys = []
    for s in layout.shapes:
        if s.role in AIRSIDE_ROLES and s.polygon is not None \
                and not s.polygon.is_empty:
            try:
                # Mitre join: the buffered boundary is straight-edged
                # (no rounded-corner arc segments), so the clip cut
                # doesn't introduce sub-metre edges that would inflate the
                # per-vertex grade after altitude rounding.
                clip_polys.append(s.polygon.buffer(clearance, join_style=2))
            except _GEOM_EXC:
                continue
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
            kept.append(BuiltShape(
                polygon=np_, role=ROLE_GROUNDSIDE_PAVEMENT,
                ref="groundside", node_altitudes=na))
            changed = True
        out_shapes.extend(kept)
        if changed:
            n_clipped += 1
    layout.shapes = out_shapes
    return n_clipped


