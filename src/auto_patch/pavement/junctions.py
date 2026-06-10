"""Junction-polygon construction + residue decomposition.

Two responsibilities live in this module:

1. **Construction** — build junction polygons from rect-endpoint
   corner sets discovered along OSM centerlines:
   ``_find_junction_points``, ``_build_junctions_from_rect_endpoints``,
   ``_rect_end_corners``, ``_build_junction_constructive``,
   ``_build_junction_polys_from_corners``.

2. **Decomposition** — turn a residue Polygon (apt.dat pavement
   minus rect / runway / terminal coverage) into a set of
   junction-polygon pieces with hole splicing and sliver removal:
   ``_decompose_polygon_with_holes``, ``_polygon_min_thickness``,
   ``_merge_thin_decomposed_pieces``, ``_splice_holes``,
   ``_polygon_area``, ``_splice_one_hole``, ``_drop_sliver_corners``.

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    _build_junction_constructive
    _build_junction_polys_from_corners
    _build_junctions_from_rect_endpoints
    _decompose_polygon_with_holes
    _drop_sliver_corners
    _find_junction_points
    _merge_thin_decomposed_pieces
    _polygon_area
    _polygon_min_thickness
    _rect_end_corners
    _splice_holes
    _splice_one_hole
"""
from __future__ import annotations

import math

from ..geom_safe import min_rotated_rect
from collections.abc import Sequence

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from ..config import (
    JUNCTION_CLUSTER_DIST_M,
    SLIVER_ANGLE_THRESHOLD_DEG,
)
from ..layout import (
    BuiltShape,
    PavementLayout,
    ROLE_CROSS_CONNECTOR,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_SECONDARY_PARALLEL,
    ROLE_STUB,
)

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


__all__ = [
    "_build_junction_constructive",
    "_build_junction_polys_from_corners",
    "_build_junctions_from_rect_endpoints",
    "_decompose_polygon_with_holes",
    "_drop_sliver_corners",
    "_find_junction_points",
    "_merge_thin_decomposed_pieces",
    "_polygon_area",
    "_polygon_min_thickness",
    "_rect_end_corners",
    "_splice_holes",
    "_splice_one_hole",
]


def _decompose_polygon_with_holes(polygon: Polygon,
                                  min_area_m2: float = 50.0,
                                  max_depth: int = 8,
                                  runway_axis_deg: float | None = None,
                                  corner_snap_pts: list[tuple[float, float]] | None = None,
                                  corner_snap_tol_m: float = 5.0,
                                  _router: bool = True,
                                  ) -> list[Polygon]:
    """Return a list of simple (no-hole) polygons that tile the same
    area as ``polygon``.

    Strategy: cut the largest interior hole through its centroid in
    the runway-parallel-or-perpendicular direction that yields the
    most-balanced split.  Recurse on each piece.  Pieces with no
    holes are returned as-is.

    Per user 2026-05-04: this function should ideally be a no-op —
    if every apt.dat hole is bordered by a rect/runway/terminal
    upstream, every residue piece is simply connected and the cut
    machinery is unnecessary.  The cut path here is a defensive
    fallback for residue pieces that still have interior holes.
    """
    from shapely.ops import split as _shp_split
    from shapely.geometry import LineString as _LS

    if (polygon.is_empty or polygon.geom_type != "Polygon"):
        return []
    if not polygon.interiors:
        return [polygon]
    interiors = list(polygon.interiors)
    big_interiors = [h for h in interiors
                     if Polygon(h).area >= min_area_m2]
    if not big_interiors:
        return [Polygon(polygon.exterior.coords)]
    if max_depth <= 0:
        clean = Polygon(polygon.exterior.coords, big_interiors)
        spliced_coords = _splice_holes(clean)
        try:
            return [Polygon(spliced_coords).buffer(0)]
        except _GEOM_EXC:
            return [Polygon(polygon.exterior.coords)]
    # (session 61) Prefer the in-pavement VISIBILITY-GRAPH router: routed
    # two-bridge SPLIT cuts that bend around rects corner-to-corner instead of
    # the full-span centroid chord (which shears far corners on non-convex
    # aprons).  Returns None to fall back to the legacy guillotine below.
    from ..config import HOLE_ROUTER_ENABLED
    if _router and HOLE_ROUTER_ENABLED:
        routed = _decompose_via_router(polygon, min_area_m2, runway_axis_deg,
                                       extra_nodes=corner_snap_pts)
        if routed is not None:
            return routed
    big_interiors.sort(key=lambda h: -Polygon(h).area)
    hole = big_interiors[0]
    cx = float(hole.centroid.x)
    cy = float(hole.centroid.y)
    minx, miny, maxx, maxy = polygon.bounds
    span = max(maxx - minx, maxy - miny) + 2.0

    angle_rad = 0.0
    if runway_axis_deg is not None:
        bearing_rad = math.radians(runway_axis_deg)
        runway_x_angle = math.pi / 2.0 - bearing_rad
        cand_a = runway_x_angle % math.pi
        cand_b = (runway_x_angle + math.pi / 2.0) % math.pi

        def _min_piece_area(theta):
            ux, uy = math.cos(theta), math.sin(theta)
            line = _LS([(cx - span * ux, cy - span * uy),
                        (cx + span * ux, cy + span * uy)])
            try:
                result = _shp_split(polygon, line)
            except _GEOM_EXC:
                return -1.0
            geoms = (list(getattr(result, "geoms", []))
                     if result.geom_type != "Polygon" else [result])
            areas = [g.area for g in geoms
                     if g.geom_type == "Polygon" and not g.is_empty]
            return min(areas) if len(areas) >= 2 else 0.0

        angle_rad = (cand_a if _min_piece_area(cand_a) >= _min_piece_area(cand_b)
                     else cand_b)
    else:
        cs = [h.centroid for h in big_interiors]
        if len(cs) > 1:
            x_spread = max(c.x for c in cs) - min(c.x for c in cs)
            y_spread = max(c.y for c in cs) - min(c.y for c in cs)
            angle_rad = math.pi / 2.0 if x_spread > y_spread else 0.0

    dx = math.cos(angle_rad)
    dy = math.sin(angle_rad)
    cut = _LS([(cx - span * dx, cy - span * dy),
               (cx + span * dx, cy + span * dy)])

    try:
        result = _shp_split(polygon, cut)
    except _GEOM_EXC:
        # Fallback: emit the exterior with holes dropped.  Should
        # not occur for valid simple geometries.
        return [Polygon(polygon.exterior.coords)]
    pieces: list[Polygon] = []
    geoms = (list(getattr(result, "geoms", []))
             if result.geom_type != "Polygon" else [result])

    # Per user 2026-05-04: snap cut-induced vertices on each piece's
    # boundary to the nearest existing polygon vertex within 5 m
    # that's MORE aligned with the hole-centroid axis (smaller
    # perpendicular distance to the cut line).  This eliminates
    # mid-rect-edge nodes by replacing the cut crossing with an
    # existing on-axis vertex (typically a rect corner that was
    # already on the polygon's boundary via seam injection).
    SNAP_RADIUS_M = 5.0

    def _perp_to_cut(px, py):
        return abs((px - cx) * dy - (py - cy) * dx)

    # Pre-compute the natural crossing points so we can identify
    # cut-induced verts on each piece.
    try:
        natural_inter = polygon.exterior.intersection(cut)
    except _GEOM_EXC:
        natural_inter = None
    natural_pts: list[tuple[float, float]] = []
    if natural_inter is not None and not natural_inter.is_empty:
        if natural_inter.geom_type == "Point":
            natural_pts = [(natural_inter.x, natural_inter.y)]
        elif natural_inter.geom_type == "MultiPoint":
            natural_pts = [(p.x, p.y) for p in natural_inter.geoms]
        elif natural_inter.geom_type == "GeometryCollection":
            for g in natural_inter.geoms:
                if g.geom_type == "Point":
                    natural_pts.append((g.x, g.y))
    # Existing polygon boundary verts (exterior + interiors) — these
    # are the candidates for snapping.
    pre_cut_verts: list[tuple[float, float]] = []
    pe = list(polygon.exterior.coords)
    if pe and pe[0] == pe[-1]:
        pe = pe[:-1]
    pre_cut_verts.extend(pe)
    for h in polygon.interiors:
        hv = list(h.coords)
        if hv and hv[0] == hv[-1]:
            hv = hv[:-1]
        pre_cut_verts.extend(hv)

    def _snap_cut_verts(piece: Polygon) -> Polygon:
        """Replace each cut-induced vertex on this piece's boundary
        with the NEAREST existing pre-cut vertex within
        SNAP_RADIUS_M, provided that vertex is itself within
        ``ON_AXIS_TOL_M`` of the cut line (i.e. it's a candidate
        node that's already aligned with the hole-centroid axis).
        Prevents mid-rect-edge cut-induced verts when an existing
        rect corner sits near the cut line."""
        if not natural_pts:
            return piece
        ON_AXIS_TOL_M = 2.0  # max perp distance from cut line for a
                              # candidate to be considered "aligned"
        coords = list(piece.exterior.coords)
        if not coords:
            return piece
        had_close = (coords[0] == coords[-1])
        if had_close:
            coords = coords[:-1]
        modified = False
        for i, (vx, vy) in enumerate(coords):
            is_cut_vert = any(
                math.hypot(vx - nx, vy - ny) < 0.5
                for nx, ny in natural_pts)
            if not is_cut_vert:
                continue
            # Find the nearest existing pre-cut vertex within range
            # that's also on-axis.  Skip candidates farther from the
            # cut line than ON_AXIS_TOL_M — those would degrade
            # alignment.
            best_v: tuple[float, float] | None = None
            best_d = SNAP_RADIUS_M
            for px, py in pre_cut_verts:
                if abs(px - vx) > SNAP_RADIUS_M or abs(py - vy) > SNAP_RADIUS_M:
                    continue
                d = math.hypot(px - vx, py - vy)
                if d < 0.01:
                    continue  # same vertex
                if d > best_d:
                    continue
                if _perp_to_cut(px, py) > ON_AXIS_TOL_M:
                    continue
                best_d = d
                best_v = (float(px), float(py))
            if best_v is not None:
                coords[i] = best_v
                modified = True
        if not modified:
            return piece
        # Reconstruct the polygon, dedupe consecutive duplicates.
        deduped: list[tuple[float, float]] = []
        for c in coords:
            if deduped and (math.hypot(c[0] - deduped[-1][0],
                                        c[1] - deduped[-1][1]) < 0.01):
                continue
            deduped.append(c)
        if len(deduped) < 3:
            return piece
        try:
            new_p = Polygon(deduped, [list(h.coords) for h in piece.interiors])
            if new_p.is_valid and not new_p.is_empty:
                return new_p
            fixed = new_p.buffer(0)
            if (fixed.geom_type == "Polygon" and not fixed.is_empty):
                return fixed
        except _GEOM_EXC:
            pass
        return piece

    for g in geoms:
        if g.geom_type != "Polygon" or g.is_empty:
            continue
        if g.area < min_area_m2:
            continue
        g = _snap_cut_verts(g)
        if g.geom_type != "Polygon" or g.is_empty:
            continue
        pieces.extend(_decompose_polygon_with_holes(
            g, min_area_m2=min_area_m2, max_depth=max_depth - 1,
            runway_axis_deg=runway_axis_deg,
            corner_snap_pts=corner_snap_pts,
            corner_snap_tol_m=corner_snap_tol_m,
            _router=False))
    # Sliver clean-up: smart-cut alignment eliminates the most
    # egregious wide-band strips (5 m × 67 m, 10 m × 119 m) that
    # appeared with horizontal-only cuts, but recursive splits can
    # still leave narrow corner slivers when a hole's MRR axis
    # nearly parallels a polygon edge.  Merge anything thinner
    # than 12 m into its largest-shared-boundary neighbour so the
    # apron stays one continuous polygon and we don't get
    # cliff-rendering on thin corners.
    MIN_PIECE_THICKNESS_M = 12.0
    pieces = _merge_thin_decomposed_pieces(
        pieces, min_thickness_m=MIN_PIECE_THICKNESS_M)
    return pieces


def _on_boundary_extra_nodes(polygon: "Polygon",
                             pts,
                             tol_m: float = 0.05,
                             cap: int = 4000,
                             ) -> "list[tuple[float, float]]":
    """Filter the airport-wide shared/canonical point set ``pts`` down to the
    points sitting ON ``polygon``'s boundary (within ``tol_m``) — neighbour-
    shape corners that are legal conforming cut endpoints for this polygon
    but are not (yet) ring vertices.  Ring vertices themselves dedupe away
    inside the router's node bucketing, so no pre-exclusion is needed."""
    if not pts:
        return []
    try:
        minx, miny, maxx, maxy = polygon.bounds
    except _GEOM_EXC:
        return []
    cand = [(float(x), float(y)) for (x, y) in pts
            if minx - tol_m <= x <= maxx + tol_m
            and miny - tol_m <= y <= maxy + tol_m]
    if not cand:
        return []
    try:
        from shapely.prepared import prep
        near_boundary = prep(polygon.boundary.buffer(tol_m))
    except _GEOM_EXC:
        return []
    out = [p for p in cand if near_boundary.intersects(Point(p))]
    return out[:cap]


def _piece_has_needle_corner(p: "Polygon") -> bool:
    """True if any exterior-ring interior angle of ``p`` is below
    ``SLIVER_ANGLE_THRESHOLD_DEG`` — such a piece would be truncated by
    ``_drop_sliver_corners`` or dropped whole by the OSM-emit guard, leaving
    an uncovered-source wedge.  Detected at decompose time so the piece can
    be MERGED into a sibling (coverage preserved) instead."""
    ring = list(p.exterior.coords)
    if ring and ring[0] == ring[-1]:
        ring = ring[:-1]
    n = len(ring)
    if n < 3:
        return True
    cos_thresh = math.cos(math.radians(SLIVER_ANGLE_THRESHOLD_DEG))
    for i in range(n):
        ax, ay = ring[(i - 1) % n]
        bx, by = ring[i]
        cx, cy = ring[(i + 1) % n]
        v1x, v1y = ax - bx, ay - by
        v2x, v2y = cx - bx, cy - by
        n1 = math.hypot(v1x, v1y)
        n2 = math.hypot(v2x, v2y)
        if n1 < 1e-9 or n2 < 1e-9:
            continue
        if (v1x * v2x + v1y * v2y) / (n1 * n2) > cos_thresh:
            return True
    return False


def _union_reopens_hole(u, a: "Polygon", b: "Polygon",
                        min_area_m2: float) -> bool:
    """True if union ``u`` of pieces ``a`` + ``b`` carries a big interior
    ring that NEITHER input had — i.e. the merge re-encircled an opened
    hole (the two inputs were the two sides of a hole-opening cut)."""
    try:
        for h in u.interiors:
            ha = Polygon(h).area
            if ha < min_area_m2:
                continue
            pre = any(abs(Polygon(g).area - ha) < 1e-6
                      for src in (a, b) for g in src.interiors)
            if not pre:
                return True
    except _GEOM_EXC:
        return True
    return False


def _merge_pieces_into_siblings(keep: "list[Polygon]",
                                defer: "list[Polygon]",
                                min_area_m2: float,
                                ) -> "list[Polygon]":
    """Union each ``defer`` piece (sub-threshold or needle-cornered) into a
    ``keep`` sibling along their shared cut edge — preserving coverage.
    Siblings are tried longest-shared-boundary first; a union that would
    RE-ENCIRCLE an opened hole (merging the two sides of a hole-opening
    cut) or go non-Polygon is rejected and the next sibling tried.  A piece
    with no mergeable sibling is KEPT as-is (never silently dropped —
    dropping uncovers source pavement)."""
    out = list(keep)
    for p in defer:
        shares: list[tuple[float, int]] = []
        for j, q in enumerate(out):
            try:
                shared = p.boundary.intersection(q.boundary).length
            except _GEOM_EXC:
                continue
            if shared > 0.0:
                shares.append((shared, j))
        shares.sort(reverse=True)
        merged_at: int | None = None
        for _shared, j in shares[:4]:
            try:
                u = unary_union([p, out[j]])
            except _GEOM_EXC:
                continue
            if (u.geom_type != "Polygon" or u.is_empty
                    or _union_reopens_hole(u, p, out[j], min_area_m2)):
                continue
            out[j] = u
            merged_at = j
            break
        if merged_at is None:
            out.append(p)
    return out


def _decompose_via_router(polygon: "Polygon",
                          min_area_m2: float,
                          runway_axis_deg: float | None,
                          extra_nodes=None,
                          ) -> "list[Polygon] | None":
    """Open every interior hole with visibility-graph-routed SPLIT cuts and
    return the resulting simple-polygon pieces, or ``None`` to fall back to
    the legacy guillotine.

    The graph is built ONCE for ``polygon`` and every hole routed against it.
    Each cut bends around rects corner-to-corner and ends on existing
    vertices (plus ``extra_nodes`` — shared neighbour-shape corners on this
    polygon's boundary), so no mid-edge node is planted and no far corner is
    sheared.  With ``config.HOLE_ROUTER_V2`` the cuts come from the Prim
    min-spanning-forest planner (``plan_hole_cuts_v2`` — conforming, chained,
    needle-free); otherwise from the v1 per-hole planner.  A piece that still
    carries a big hole (no visible bridge pair) is handed to the legacy
    guillotine in isolation.  Sub-threshold and needle-cornered pieces are
    MERGED into the sibling sharing their cut edge — never silently dropped
    (a dropped piece uncovers source pavement: the HECA fan-wedge bug)."""
    from ..config import HOLE_ROUTER_V2
    from .hole_router import plan_hole_cuts, plan_hole_cuts_v2
    from shapely.ops import split as _shp_split
    use_v2 = HOLE_ROUTER_V2
    try:
        if use_v2:
            cuts = plan_hole_cuts_v2(
                polygon, min_hole_area=min_area_m2,
                extra_nodes=_on_boundary_extra_nodes(polygon,
                                                     extra_nodes or ()))
        else:
            cuts = plan_hole_cuts(polygon, min_hole_area=min_area_m2,
                                  runway_axis_deg=runway_axis_deg)
    except _GEOM_EXC:
        return None
    if not cuts:
        return None
    if use_v2:
        # Apply ALL cuts as one global arrangement: node the polygon
        # boundary with every cut, polygonize the linework, and clip each
        # face back to the polygon.  Unlike sequential ``shapely.split``
        # this is immune to cut interactions (a cut touching another cut,
        # or a ring, mid-application) — the faces tile the polygon EXACTLY,
        # and an unopened hole survives as an interior ring of its clipped
        # face (handed to the guillotine fallback below).
        from shapely.ops import polygonize as _polygonize
        try:
            noded = unary_union([polygon.boundary] + list(cuts))
            faces = list(_polygonize(noded))
        except _GEOM_EXC:
            return None
        pieces = []
        for f in faces:
            if f.geom_type != "Polygon" or f.is_empty or f.area < 1e-6:
                continue
            try:
                clipped = f.intersection(polygon)
            except _GEOM_EXC:
                continue
            for gpiece in (clipped.geoms
                           if hasattr(clipped, "geoms") else [clipped]):
                if (gpiece.geom_type == "Polygon"
                        and not gpiece.is_empty and gpiece.area > 1e-6):
                    pieces.append(gpiece)
        if not pieces:
            return None
    else:
        pieces = [polygon]
        for cut in cuts:
            nxt: list[Polygon] = []
            for p in pieces:
                if p.geom_type != "Polygon" or p.is_empty:
                    continue
                try:
                    if not cut.intersects(p):
                        nxt.append(p)
                        continue
                    res = _shp_split(p, cut)
                except _GEOM_EXC:
                    nxt.append(p)
                    continue
                geoms = (list(res.geoms) if res.geom_type != "Polygon"
                         else [res])
                nxt.extend(g for g in geoms
                           if g.geom_type == "Polygon" and not g.is_empty)
            pieces = nxt
    out: list[Polygon] = []
    defer: list[Polygon] = []
    for p in pieces:
        if p.is_empty or p.geom_type != "Polygon":
            continue
        if any(Polygon(h).area >= min_area_m2 for h in p.interiors):
            # A hole the router could not open — let the guillotine handle
            # just this piece (no further router attempts: _router=False).
            sub = _decompose_polygon_with_holes(
                p, min_area_m2=min_area_m2,
                runway_axis_deg=runway_axis_deg, _router=False)
            out.extend(sub)
            # Recover anything the guillotine dropped (its sub-threshold /
            # thin-strip discards): defer the lost fragments into the
            # sibling-merge below so coverage is preserved.
            try:
                lost = (p.difference(unary_union(sub)) if sub else p)
            except _GEOM_EXC:
                lost = None
            if lost is not None and not lost.is_empty:
                for lg in (lost.geoms if hasattr(lost, "geoms")
                           else [lost]):
                    if (lg.geom_type == "Polygon" and not lg.is_empty
                            and lg.area >= 1.0):
                        defer.append(lg)
            continue
        if p.interiors:
            p = Polygon(p.exterior.coords)   # drop sub-threshold holes
        if p.is_empty:
            continue
        # Defer for sibling-merge: sub-threshold fragments, and SMALL
        # needle-cornered pieces (cut-created wedge slivers the downstream
        # guards would truncate or drop — uncovering source).  Large pieces
        # with a needle corner keep the status-quo path (the emit-side
        # ``_drop_sliver_corners`` trims just the tip): they are not wedge
        # slices and merging them away would destabilise the partition.
        _NEEDLE_MERGE_MAX_M2 = 5000.0
        if (p.area < min_area_m2
                or (p.area < _NEEDLE_MERGE_MAX_M2
                    and _piece_has_needle_corner(p))):
            defer.append(p)
            continue
        out.append(p)
    if not out and not defer:
        return None
    out = _merge_pieces_into_siblings(out, defer, min_area_m2)
    if not out:
        return None
    return _merge_thin_decomposed_pieces(out, min_thickness_m=12.0,
                                         drop_unmergeable=False,
                                         min_hole_area_m2=min_area_m2)


def _polygon_min_thickness(poly: "Polygon") -> float:
    """Approximate minimum thickness of a polygon: half-width of
    the rotated minimum bounding rectangle.  Fast computation: try
    the polygon's minimum-rotated-rectangle and return the shorter
    side length."""
    try:
        mrr = min_rotated_rect(poly)
        if mrr.is_empty or mrr.geom_type != "Polygon":
            return 0.0
        coords = list(mrr.exterior.coords)
        if len(coords) < 5:
            return 0.0
        sides = []
        for i in range(4):
            ax, ay = coords[i]
            bx, by = coords[i + 1]
            sides.append(math.hypot(bx - ax, by - ay))
        return min(sides)
    except _GEOM_EXC:
        return 0.0


def _merge_thin_decomposed_pieces(
        pieces: "list[Polygon]",
        min_thickness_m: float = 4.0,
        max_iters: int = 50,
        drop_unmergeable: bool = True,
        min_hole_area_m2: float = 50.0,
        ) -> "list[Polygon]":
    """Merge any piece in ``pieces`` whose minimum-rotated-rectangle
    thickness is less than ``min_thickness_m`` into the neighbouring
    piece sharing the longest boundary.  Used by
    ``_decompose_polygon_with_holes`` to suppress 2 m-thick horizontal
    strips that arise when multiple holes have close y-centroids.
    Returns a possibly-shorter list with thin strips absorbed.

    ``drop_unmergeable=False`` (the conforming-cuts router path) KEEPS a
    thin piece whose merge fails instead of dropping it — a dropped piece
    uncovers source pavement (X-Plane interpolates raw terrain across the
    gap), which is worse than a thin-but-covered strip.
    """
    if not pieces:
        return pieces
    work = list(pieces)
    kept_thin: set[int] = set()
    for _it in range(max_iters):
        # Find the thinnest piece below threshold.
        thin_idx: int | None = None
        thin_thick = float('inf')
        for i, p in enumerate(work):
            if p is None or p.is_empty or i in kept_thin:
                continue
            t = _polygon_min_thickness(p)
            if t < min_thickness_m and t < thin_thick:
                thin_idx = i
                thin_thick = t
        if thin_idx is None:
            break
        thin = work[thin_idx]

        def _discard(idx: int) -> None:
            if drop_unmergeable:
                work[idx] = None
            else:
                kept_thin.add(idx)

        # Find the neighbour with the longest shared boundary.
        best_j: int | None = None
        best_share = 0.0
        thin_boundary = thin.boundary
        for j, p in enumerate(work):
            if j == thin_idx or p is None or p.is_empty:
                continue
            try:
                shared = thin_boundary.intersection(
                    p.boundary).length
            except _GEOM_EXC:
                shared = 0.0
            if shared > best_share:
                best_share = shared
                best_j = j
        if best_j is None or best_share <= 0.0:
            # No neighbour to merge with.
            _discard(thin_idx)
            continue
        try:
            merged = unary_union([thin, work[best_j]])
            if merged.is_empty:
                _discard(thin_idx)
                continue
            if merged.geom_type == "MultiPolygon":
                # The union didn't fully bridge.
                _discard(thin_idx)
                continue
            if merged.geom_type != "Polygon":
                _discard(thin_idx)
                continue
            if _union_reopens_hole(merged, thin, work[best_j],
                                   min_hole_area_m2):
                # The thin piece and this neighbour are the two sides of a
                # hole-opening cut — merging them would re-encircle the
                # hole.  Keep the thin piece instead.
                _discard(thin_idx)
                continue
            work[best_j] = merged
            work[thin_idx] = None
        except _GEOM_EXC:
            _discard(thin_idx)
    return [p for p in work if p is not None and not p.is_empty]


# ── Hole splicing ────────────────────────────────────────────────
#
# Defensive fallback for any polygon with holes that slips through
# decomposition (e.g. shapely.ops.split failed).  Splices each
# hole into the exterior via a zero-width bridge so ear-clipping
# can operate on a single ring; sliver triangles along the bridge
# are filtered downstream by the area threshold.


def _splice_holes(polygon: Polygon) -> list[tuple[float, float]]:
    """Return the vertex list of the spliced single-ring polygon
    (without closing repeat).  Holes are inserted one at a time by
    finding the closest exterior vertex / hole vertex pair and
    threading the hole into the exterior at that bridge.
    """
    ext = list(polygon.exterior.coords)
    if ext and ext[0] == ext[-1]:
        ext = ext[:-1]
    holes_list: list[list[tuple[float, float]]] = []
    for h in polygon.interiors:
        h_coords = list(h.coords)
        if h_coords and h_coords[0] == h_coords[-1]:
            h_coords = h_coords[:-1]
        if len(h_coords) >= 3:
            holes_list.append(h_coords)
    if not holes_list:
        return ext
    # Process holes from largest to smallest so big holes get the
    # "best" bridge slots; small holes thread into the still-clean
    # remainder.
    holes_list.sort(key=lambda h: -_polygon_area(h))
    ring = list(ext)
    for hole in holes_list:
        ring = _splice_one_hole(ring, hole)
    return ring


def _polygon_area(coords: Sequence[tuple[float, float]]) -> float:
    s = 0.0
    n = len(coords)
    for i in range(n):
        x1, y1 = coords[i]
        x2, y2 = coords[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _splice_one_hole(ring: list[tuple[float, float]],
                     hole: list[tuple[float, float]]
                     ) -> list[tuple[float, float]]:
    """Find the closest (ring_vertex, hole_vertex) pair and splice
    the hole into the ring at that bridge.  Hole is walked in its
    native (CW relative to a CCW exterior) direction so the spliced
    ring stays simple."""
    best_i = best_j = 0
    best_d2 = float("inf")
    for i, (rx, ry) in enumerate(ring):
        for j, (hx, hy) in enumerate(hole):
            d2 = (rx - hx) * (rx - hx) + (ry - hy) * (ry - hy)
            if d2 < best_d2:
                best_d2 = d2
                best_i, best_j = i, j
    # Spliced ring:
    #   ring[0..best_i] + hole[best_j..end] + hole[0..best_j]
    #   + ring[best_i..end]
    # The boundary touches ring[best_i] and hole[best_j] twice —
    # this is the bridge corridor.
    spliced: list[tuple[float, float]] = []
    spliced.extend(ring[: best_i + 1])
    spliced.extend(hole[best_j:])
    spliced.extend(hole[: best_j + 1])
    spliced.extend(ring[best_i:])
    return spliced


def _drop_sliver_corners(
    ring: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Drop ring vertices whose interior angle is below
    ``SLIVER_ANGLE_THRESHOLD_DEG``.

    A sliver corner is a needle-tip vertex: the polygon comes in
    along one edge, makes a near-180° fold, and goes back out
    almost on top of the incoming edge, leaving a thin wedge.
    Source: residue construction (apt.dat pav − rects − terminals)
    leaves wedges where two rect/terminal edges meet the boundary at
    nearly-collinear angles.  Shapely calls these polygons valid
    (the two long edges are parallel-but-not-equal, no self-
    intersection), but the polygon's needle-tip corner forces
    Triangle4XP downstream to emit at least one corner triangle
    with that interior angle — for sub-2° tips that triangle is
    near-degenerate (NaN normal) and crashes X-Plane's mesh builder.

    Dropping the tip vertex collapses the wedge into a single edge
    between the two flanking vertices.  Coverage cost: the area
    between the tip and the truncation chord (typically < 50 m²).

    Iterates to a fixed point — dropping one tip can expose
    another.  Capped at 8 passes.
    """
    if len(ring) < 4:
        return ring
    cos_thresh = math.cos(math.radians(SLIVER_ANGLE_THRESHOLD_DEG))
    for _ in range(8):
        n = len(ring)
        if n < 4:
            break
        keep = [True] * n
        for i in range(n):
            ax, ay = ring[(i - 1) % n]
            bx, by = ring[i]
            cx, cy = ring[(i + 1) % n]
            v1x, v1y = ax - bx, ay - by
            v2x, v2y = cx - bx, cy - by
            n1 = math.hypot(v1x, v1y)
            n2 = math.hypot(v2x, v2y)
            if n1 < 1e-9 or n2 < 1e-9:
                continue
            cos = (v1x * v2x + v1y * v2y) / (n1 * n2)
            if cos > cos_thresh:
                # Angle = acos(cos) is below threshold.
                keep[i] = False
        new_ring = [r for r, k in zip(ring, keep) if k]
        if len(new_ring) == n:
            break
        ring = new_ring
    return ring



def _find_junction_points(
    nodes: dict[str, tuple[float, float]],
    ways: list[tuple[str, list[str], dict[str, str]]],
    to_m,
    osm_centerlines: list[tuple[LineString, str]] | None = None,
) -> list[tuple[float, float]]:
    """Identify junction POINTS — OSM nodes shared by ≥ 2 DIFFERENT refs.

    Per user rule: pure bends within one taxi (two same-ref ways
    meeting at a node) are NOT junctions — they emit as adjacent
    same-role rects sharing a vertex line.  Only nodes where
    multiple distinct refs (or an unrefed way + a refed way) meet
    are junction candidates.

    Candidates within ``JUNCTION_CLUSTER_DIST_M`` of each other
    collapse into one cluster; the cluster centroid is the
    junction point.
    """
    from collections import defaultdict
    refs_at_node: dict[str, set] = defaultdict(set)
    refed_taxi_nodes: set = set()
    for wid, nds, tags in ways:
        if tags.get("aeroway") != "taxiway":
            continue
        ref = tags.get("ref", "")
        if not ref:
            continue
        refed_taxi_nodes.update(nds)
        for n in nds:
            refs_at_node[n].add(ref)

    # Per user 2026-05-05: any ``aeroway=taxiway`` (refed OR not)
    # AND any ``aeroway=parking_position`` way contributes a
    # connector node where it meets a refed taxiway.  At SPJC F
    # crosses 2 parking_position centerlines that split it into 3
    # rects; CYXY's V↔U pair has 6 short unrefed taxiway connectors
    # marking the chart-level intersections.  No length filter —
    # any aeroway crossing splits.  Apron-only nodes are still
    # filtered because the connector tag is added ONLY when the
    # node is shared with a refed taxiway.
    for wid, nds, tags in ways:
        aw = tags.get("aeroway")
        if aw == "taxiway" and tags.get("ref", ""):
            continue  # already handled in the refed-taxiway pass above
        if aw not in ("taxiway", "parking_position"):
            continue
        for n in nds:
            if n in refed_taxi_nodes:
                refs_at_node[n].add("_conn")

    candidates: list[tuple[float, float]] = []
    for nid, refs in refs_at_node.items():
        if len(refs) < 2:
            continue
        if nid not in nodes:
            continue
        lat, lon = nodes[nid]
        candidates.append(to_m(lon, lat))

    # ALSO add geometric crossing points between different-ref
    # centerlines (helps SPLP where few OSM nodes are shared).
    if osm_centerlines:
        for i in range(len(osm_centerlines)):
            ls1, ref1 = osm_centerlines[i]
            for j in range(i+1, len(osm_centerlines)):
                ls2, ref2 = osm_centerlines[j]
                if ref1 and ref2 and ref1 == ref2:
                    continue
                if not ls1.intersects(ls2):
                    continue
                try:
                    inter = ls1.intersection(ls2)
                except _GEOM_EXC:
                    continue
                if inter.is_empty:
                    continue
                if inter.geom_type == "Point":
                    candidates.append((inter.x, inter.y))
                elif inter.geom_type == "MultiPoint":
                    for p in inter.geoms:
                        candidates.append((p.x, p.y))
                elif inter.geom_type == "LineString":
                    candidates.append(inter.centroid.coords[0])

    # Cluster within JUNCTION_CLUSTER_DIST_M (greedy single-link)
    clusters: list[list[tuple[float, float]]] = []
    for pt in candidates:
        placed = False
        for cl in clusters:
            if any(math.hypot(pt[0]-q[0], pt[1]-q[1]) <= JUNCTION_CLUSTER_DIST_M
                   for q in cl):
                cl.append(pt)
                placed = True
                break
        if not placed:
            clusters.append([pt])

    return [(sum(p[0] for p in cl)/len(cl),
             sum(p[1] for p in cl)/len(cl)) for cl in clusters]


def _build_junctions_from_rect_endpoints(
    taxi_rects: list[tuple[Polygon, LineString, str, str]],
    merge_dist: float,
    pav_union: Polygon | None,
    terminal_union: Polygon | None = None,
) -> list[Polygon]:
    """Build junctions from rect endpoint clusters (user's approach).

    Algorithm:
      1. Collect each rect's 2 axis endpoints + 2 corner vertices at
         each end (total: 2 endpoints × 2 corners = 4 corners per rect).
      2. Cluster axis endpoints by single-link within ``merge_dist``.
      3. For each cluster of ≥ 2 endpoints:
         * If all endpoints share the same ref → same-taxi bend (no
           junction emitted; same-ref rects connect via shared vertices
           handled elsewhere).
         * Else → emit a junction polygon whose vertices are the 2
           corner vertices of each participating rect at the cluster.

    The polygon vertices are ordered angularly around the cluster
    centroid, giving a star polygon that wraps through each rect's
    corner pair.
    """
    if not taxi_rects:
        return []

    # Endpoint records: (rect_idx, end_index, axis_pt, corner_pair, ref)
    endpoints = []
    for i, (rect, axis, role, ref) in enumerate(taxi_rects):
        pairs = _rect_end_corners(rect, axis)
        if len(pairs) < 2:
            continue
        coords = list(axis.coords)
        endpoints.append((i, 0, coords[0], pairs[0], ref))
        endpoints.append((i, 1, coords[-1], pairs[1], ref))

    # Single-link cluster by axis-endpoint proximity
    n = len(endpoints)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i+1, n):
            ax = endpoints[i][2]
            bx = endpoints[j][2]
            if math.hypot(ax[0]-bx[0], ax[1]-bx[1]) <= merge_dist:
                union(i, j)

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        clusters.setdefault(r, []).append(i)

    out: list[Polygon] = []
    for cl in clusters.values():
        if len(cl) < 2:
            continue
        # Unique refs in cluster
        refs = {endpoints[i][4] for i in cl}
        # Unique rect ids (cluster can contain multiple ends of same rect)
        unique_rects = {endpoints[i][0] for i in cl}
        # Pure same-ref bend (only one ref AND only one rect pairs bends) — skip junction
        if len(refs) == 1 and len(unique_rects) <= 2:
            continue
        # Collect all corner vertices
        all_corners = []
        for i in cl:
            c1, c2 = endpoints[i][3]
            all_corners.append(c1)
            all_corners.append(c2)
        if len(all_corners) < 3:
            continue
        # Deduplicate near-identical corners
        uniq: list[tuple[float, float]] = []
        for c in all_corners:
            if not any(math.hypot(c[0]-u[0], c[1]-u[1]) < 0.1 for u in uniq):
                uniq.append(c)
        if len(uniq) < 3:
            continue
        cx = sum(p[0] for p in uniq) / len(uniq)
        cy = sum(p[1] for p in uniq) / len(uniq)
        ordered = sorted(uniq, key=lambda p: math.atan2(p[1]-cy, p[0]-cx))
        try:
            poly = Polygon(ordered).buffer(0)
        except _GEOM_EXC:
            continue
        if poly.is_empty:
            continue
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.geom_type != "Polygon" or poly.area < 100.0:
            continue
        # Don't let junction bleed into a terminal
        if terminal_union is not None:
            try:
                poly = poly.difference(terminal_union)
            except _GEOM_EXC:
                pass
            if poly.is_empty:
                continue
            if poly.geom_type == "MultiPolygon":
                poly = max(poly.geoms, key=lambda g: g.area)
            if poly.geom_type != "Polygon":
                continue
        out.append(poly)
    return out


def _rect_end_corners(rect: Polygon, axis: LineString
                      ) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Return the 2 pairs of corners at the rect's 2 short ends.

    For a 4-corner rect built as [p1+h*perp, p2+h*perp, p2-h*perp,
    p1-h*perp], the "start" end has corners [idx 0, idx 3] and
    the "end" end has corners [idx 1, idx 2].  This pairing
    matters because junction polygons are built from the pair at
    whichever end of the rect meets the junction centroid.
    """
    coords = list(rect.exterior.coords)
    if len(coords) < 5:
        return []
    # idx 0 = p1+perp, idx 1 = p2+perp, idx 2 = p2-perp, idx 3 = p1-perp
    start_pair = (coords[0], coords[3])   # corners at axis start
    end_pair = (coords[1], coords[2])     # corners at axis end
    return [start_pair, end_pair]


def _build_junction_constructive(
    cluster_centroid: tuple[float, float],
    taxi_rects: list[tuple[Polygon, LineString, str, str]],
    pav_union,
    terminal_union,
    max_corner_dist_m: float = 80.0,
    local_disc_radius_m: float = 120.0,
) -> Polygon | None:
    """Constructive junction-polygon build per the user's
    authoritative shape rule
    (memory: feedback_shape_rules):

      1. One vertex per incoming rect corner (outer corners at the
         rect end that meets this junction).
      2. Between consecutive corners belonging to DIFFERENT rects,
         trace apt.dat pavement vertices that lie on the local pav
         boundary arc between them.
      3. Between consecutive corners of the SAME rect, connect
         directly (that's the rect's short edge — the junction's
         inner face on that side).

    Arcs are bounded by clipping pav_union to a disc of radius
    ``local_disc_radius_m`` around the cluster centroid; this keeps
    the boundary walk local and avoids wrap-around issues at large
    multi-component pavements.

    Returns ``None`` if fewer than 2 rect ends cluster here or
    construction fails.
    """
    if pav_union is None or pav_union.is_empty:
        return None
    cx, cy = cluster_centroid
    cp = Point(cx, cy)
    if terminal_union is not None and not terminal_union.is_empty:
        try:
            if terminal_union.contains(cp):
                return None
        except _GEOM_EXC:
            pass

    # Gather rect ends near the cluster centroid.
    rect_ends: list[tuple[int, tuple[float, float], tuple[float, float]]] = []
    for i, (rect, axis, role, ref) in enumerate(taxi_rects):
        coords_ax = list(axis.coords)
        if len(coords_ax) < 2:
            continue
        ax_start = coords_ax[0]
        ax_end = coords_ax[-1]
        d_start = math.hypot(ax_start[0] - cx, ax_start[1] - cy)
        d_end = math.hypot(ax_end[0] - cx, ax_end[1] - cy)
        if min(d_start, d_end) > max_corner_dist_m:
            continue
        pairs = _rect_end_corners(rect, axis)
        if len(pairs) < 2:
            continue
        idx = 0 if d_start <= d_end else 1
        c1, c2 = pairs[idx]
        rect_ends.append((i, c1, c2))

    if len(rect_ends) < 2:
        return None

    # Local pavement: intersect pav with a disc around the cluster.
    try:
        disc = cp.buffer(local_disc_radius_m)
        local_pav = pav_union.intersection(disc)
    except _GEOM_EXC:
        return None
    if local_pav.is_empty:
        return None
    if local_pav.geom_type == "MultiPolygon":
        # Take the component containing (or closest to) the centroid.
        best = None
        best_d = float('inf')
        for g in local_pav.geoms:
            if g.geom_type != "Polygon":
                continue
            d = g.distance(cp)
            if d < best_d:
                best_d = d
                best = g
        if best is None:
            return None
        local_pav = best
    if local_pav.geom_type != "Polygon":
        return None

    # Projection helper: use the exterior ring as a line for param.
    ext_coords = list(local_pav.exterior.coords)
    if len(ext_coords) < 4:
        return None
    # LinearRing is closed (first == last); use as LineString for project()
    ext_ls = LineString(ext_coords)
    ext_length = ext_ls.length
    if ext_length <= 0:
        return None

    # Project each rect outer corner onto the exterior ring.
    corner_data: list[tuple[float, tuple[float, float], int]] = []
    for rect_idx, c1, c2 in rect_ends:
        for c in (c1, c2):
            try:
                param = ext_ls.project(Point(c))
            except _GEOM_EXC:
                continue
            proj_pt = ext_ls.interpolate(param)
            # If the projection is far (corner inside pav interior,
            # not on boundary — e.g. the local disc cut through a
            # rect short edge), use the original corner position.
            if proj_pt.distance(Point(c)) > 20.0:
                continue
            corner_data.append((param, c, rect_idx))

    # Need at least 3 corners for a polygon.
    if len(corner_data) < 3:
        return None

    # Sort corners by boundary param.  This orders them around the
    # local pav exterior ring; same-rect corner pairs will typically
    # be adjacent (a rect's 2 outer corners land close together on
    # the boundary).
    corner_data.sort(key=lambda t: t[0])

    # Collect all exterior-ring vertices with their params for arc walks.
    ext_verts = ext_coords[:-1] if ext_coords[0] == ext_coords[-1] else ext_coords
    ext_vert_params: list[tuple[float, tuple[float, float]]] = []
    acc = 0.0
    for i, v in enumerate(ext_verts):
        if i > 0:
            acc += math.hypot(v[0] - ext_verts[i-1][0],
                              v[1] - ext_verts[i-1][1])
        ext_vert_params.append((acc, (v[0], v[1])))

    # Build polygon by walking sorted corners.  Between consecutive
    # corners of different rects, insert all exterior-ring vertices
    # whose params lie between the 2 corner params (forward direction,
    # with wrap-around from last to first).
    n = len(corner_data)
    poly_coords: list[tuple[float, float]] = []
    for i in range(n):
        cur_param, cur_xy, cur_rect = corner_data[i]
        nxt_param, nxt_xy, nxt_rect = corner_data[(i + 1) % n]
        poly_coords.append(cur_xy)
        if cur_rect == nxt_rect:
            # Same-rect: direct connection (rect short edge), no arc.
            continue
        # Different rect: walk exterior ring from cur_param to nxt_param
        # in increasing-param direction (wrap at end).
        if i == n - 1 or nxt_param < cur_param:
            arc_verts = (
                [v for (vp, v) in ext_vert_params if vp > cur_param] +
                [v for (vp, v) in ext_vert_params if vp < nxt_param])
        else:
            arc_verts = [v for (vp, v) in ext_vert_params
                         if cur_param < vp < nxt_param]
        for v in arc_verts:
            poly_coords.append(v)

    if len(poly_coords) < 3:
        return None
    try:
        poly = Polygon(poly_coords).buffer(0)
    except _GEOM_EXC:
        return None
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.geom_type != "Polygon" or poly.area < 80.0:
        return None

    # Clip to the local pav (in case rect short-edges extend past it)
    try:
        poly = poly.intersection(local_pav)
    except _GEOM_EXC:
        pass
    if poly.is_empty:
        return None
    if poly.geom_type == "MultiPolygon":
        poly = max(poly.geoms, key=lambda g: g.area)
    if poly.geom_type != "Polygon":
        return None

    # Exclude terminal overlap
    if terminal_union is not None and not terminal_union.is_empty:
        try:
            poly = poly.difference(terminal_union)
        except _GEOM_EXC:
            pass
        if poly.is_empty or poly.geom_type not in ("Polygon", "MultiPolygon"):
            return None
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.geom_type != "Polygon":
            return None

    return poly


def _build_junction_polys_from_corners(
    junction_points: list[tuple[float, float]],
    taxi_rects: list[tuple[Polygon, LineString, str, str]],
    pav: Polygon | None,
    terminal_union: Polygon | None = None,
    max_corner_dist_m: float = 100.0,
) -> list[Polygon]:
    """Build each junction polygon from the CORNER VERTICES of the
    adjacent rects, per the user's rule:

        "One vertex for each corner vertex of the rects it's joining."

    For each cluster centroid:
      1. Gather rects whose axis start/end is within
         ``max_corner_dist_m`` of the centroid.
      2. For each gathered rect, take the 2 corner vertices at the
         NEARER end (start or end).
      3. Order all corner vertices angularly around the centroid.
      4. Emit as the junction polygon (simple polygon through these
         vertices).

    If fewer than 2 rect ends cluster here, no junction polygon is
    emitted (would be degenerate).

    Junction polys that overlap a terminal aren't emitted; terminal
    boundary is adjacent-direct (per user rule "aprons can join
    directly to taxiways without a junction").
    """
    if pav is None or not junction_points:
        return []

    polys: list[Polygon] = []
    for (cx, cy) in junction_points:
        jpt = Point(cx, cy)
        # Avoid emitting into a terminal
        if terminal_union is not None and terminal_union.contains(jpt):
            continue

        corner_pts: list[tuple[float, float]] = []
        for rect, axis, role, ref in taxi_rects:
            coords_ax = list(axis.coords)
            if len(coords_ax) < 2:
                continue
            ax_start = coords_ax[0]
            ax_end = coords_ax[-1]
            d_start = math.hypot(ax_start[0]-cx, ax_start[1]-cy)
            d_end = math.hypot(ax_end[0]-cx, ax_end[1]-cy)
            near_d = min(d_start, d_end)
            if near_d > max_corner_dist_m:
                continue
            pairs = _rect_end_corners(rect, axis)
            if not pairs:
                continue
            # Take the pair at the closer axis end
            idx = 0 if d_start <= d_end else 1
            corner_pts.extend(pairs[idx])

        if len(corner_pts) < 3:
            continue
        # Order corners angularly around centroid
        ordered = sorted(corner_pts,
                         key=lambda p: math.atan2(p[1]-cy, p[0]-cx))
        try:
            poly = Polygon(ordered).buffer(0)
        except _GEOM_EXC:
            continue
        if poly.is_empty:
            continue
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.geom_type != "Polygon":
            continue
        if poly.area < 80.0:
            continue
        # Clip to pavement so junction doesn't escape the pavement footprint
        poly = poly.intersection(pav)
        if poly.is_empty:
            continue
        if poly.geom_type == "MultiPolygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        if poly.geom_type != "Polygon":
            continue
        polys.append(poly)
    return polys
