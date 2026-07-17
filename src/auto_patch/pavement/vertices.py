"""Shared-vertex enforcement and snap helpers.

Geometric-invariant utilities used after every shape-build pass to
enforce the "Shared vertices exact between adjacent shapes" rule
(memory: feedback_shape_rules.md, Coverage invariants).  These
functions run as a finalisation layer that walks every emitted
PavementLayout shape and snaps near-coincident vertices to the
same coordinate, drops spike vertices that would self-intersect
under .11f OSM precision, and validates that adjacent shapes share
exact corners.

Public API (all kept with leading-underscore names for backward
compatibility with internal callers in O4_Airport_Pavement_Builder):
    _snap_polygon_vertices_to_rect_corners
    _push_junction_vertices_off_taxi_rect_edges
    _drop_spike_vertices
    _enforce_shared_vertices
    _validate_shared_vertex_invariant
"""
from __future__ import annotations

import math
import os
import sys

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from ..layout import (
    BuiltShape,
    PavementLayout,
    ROLE_APRON,
    ROLE_BOUNDARY,
    ROLE_CROSS_CONNECTOR,
    ROLE_GROUNDSIDE_PAVEMENT,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_SECONDARY_PARALLEL,
    ROLE_STUB,
    ROLE_BUILDING,
    SHARED_VERTEX_TOL_M,
)
from ..config import (
    JUNCTION_CLUSTER_DIST_M,
    SLIVER_ANGLE_THRESHOLD_DEG,
)

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors (``NameError``, ``AttributeError``-on-
# typo, ``ImportError``) propagate so they surface immediately
# during testing rather than being silently masked at runtime.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)



# Max perpendicular distance from a non-adjacent ring edge below
# which a vertex is treated as a "spike" and dropped.  The .11f
# OSM-emit truncation can collapse a sub-mm spike onto its neighbour
# edge and produce a self-intersecting polygon, which crashes
# X-Plane's mesh builder.  5 mm is well below any visible precision
# but well above float-precision noise.
SPIKE_VERTEX_TOL_M = 0.005


__all__ = [
    "SPIKE_VERTEX_TOL_M",
    "open_ring",
    "close_ring",
    "_drop_spike_vertices",
    "_enforce_shared_vertices",
    "_push_junction_vertices_off_taxi_rect_edges",
    "_snap_polygon_vertices_to_rect_corners",
    "_validate_shared_vertex_invariant",
]


def open_ring(coords: "list[tuple[float, float]]"
              ) -> "list[tuple[float, float]]":
    """Return ``coords`` without a duplicated closing vertex (the OPEN
    form).  If the ring is already open it is returned unchanged.

    A polygon ring travels through this codebase in two forms: CLOSED
    (first vertex repeated as last, as shapely's ``exterior.coords``
    yields) and OPEN (no repeat).  Which form a ``coords``/``ring``
    variable holds is not encoded in its name or type, so ~75 sites
    re-test ``coords[0] == coords[-1]`` by hand — an off-by-one
    hazard, especially where a parallel ``node_altitudes`` list must
    be sliced in lockstep.  Use these helpers instead of open-coding
    the test.  (Callers that also carry per-vertex altitudes must
    still slice those in parallel — these helpers only touch coords.)
    """
    if coords and coords[0] == coords[-1]:
        return coords[:-1]
    return coords


def close_ring(coords: "list[tuple[float, float]]"
               ) -> "list[tuple[float, float]]":
    """Return ``coords`` with a duplicated closing vertex (the CLOSED
    form).  If already closed it is returned unchanged.  Inverse of
    :func:`open_ring`."""
    if coords and coords[0] != coords[-1]:
        return list(coords) + [coords[0]]
    return coords




# On-pavement snap-guard tolerances (see _snap_polygon_vertices_to_rect_corners).
_SNAP_PAV_BUFFER_M = 0.15   # absorb the boundary epsilon at the segment ends
_SNAP_PAV_GAP_M = 0.5       # min off-pavement run that counts as crossing a gap


def _snap_polygon_vertices_to_rect_corners(
        poly: "Polygon",
        sloping_rect_polys: "list[Polygon]",
        snap_tol_m: float = 5.0,
        ) -> "Polygon":
    """Snap every vertex of ``poly`` that lies within ``snap_tol_m``
    of any sloping-rect corner to that corner.

    Per user 2026-04-28 invariant: a sloping rect (runway, primary/
    secondary parallel, stub, cross-connector) can only share a
    *corner* with an adjacent junction polygon — never a point on
    one of its four edges.  Edge-interior coincidence breaks the
    rect's straight-line slope by injecting an extra elevation
    constraint at a non-corner location.

    The runway-crossing-junction emit (``_resolve_runway_crossings``)
    builds a junction polygon from the union of crossing runway
    segments.  Shapely's ``unary_union`` produces vertices at every
    boundary intersection point; some of those points land 2-5 m
    *along* a surviving rect's long edge instead of *at* the rect's
    corner because the dropped (in-crossing) and surviving (out-of-
    crossing) runway segments don't perfectly tile.  Snapping near-
    corner vertices fixes the immediate violation without distorting
    the union polygon's overall footprint.

    Consecutive duplicate vertices produced by the snap are deduped.
    Returns the input polygon unchanged if snapping would leave
    fewer than 3 distinct vertices or produce an invalid polygon.
    """
    try:
        coords = open_ring(list(poly.exterior.coords))
    except _GEOM_EXC:
        return poly
    if len(coords) < 3:
        return poly

    # Each corner remembers its owning rect so the on-pavement guard below can
    # test the snap path against ``poly ∪ that-rect`` (cheap, 2 polygons).
    corners: list[tuple[float, float, "Polygon"]] = []
    for r in sloping_rect_polys:
        if r is None or r.is_empty:
            continue
        try:
            rc = list(r.exterior.coords)
        except _GEOM_EXC:
            continue
        if rc and rc[0] == rc[-1]:
            rc = rc[:-1]
        corners.extend((float(x), float(y), r) for x, y in rc)
    if not corners:
        return poly

    # ON-PAVEMENT GUARD (user 2026-06-30): never snap a vertex to a rect corner
    # if the straight path V→corner leaves the pavement — that drags the vertex
    # ACROSS a no-pavement wedge onto the corner, collapsing the wedge into a
    # near-zero-width self-touch that the spike cleanup then severs (the HECA
    # stub-B/-707 gap).  ``poly ∪ owning-rect`` is the local pavement; if more
    # than ``_SNAP_PAV_GAP_M`` of the path lies outside it (buffered for the
    # boundary epsilon), the snap crosses a gap — leave the vertex put.  Gate
    # ``O4_SNAP_ON_PAV_GUARD=0`` restores the unguarded snap.
    _guard = os.environ.get("O4_SNAP_ON_PAV_GUARD", "1") == "1"

    def _snap_crosses_gap(vx, vy, cx, cy, rect) -> bool:
        try:
            seg = LineString([(vx, vy), (cx, cy)])
            if seg.length < 1e-9:
                return False
            local_pav = poly.union(rect).buffer(_SNAP_PAV_BUFFER_M)
            return seg.difference(local_pav).length > _SNAP_PAV_GAP_M
        except _GEOM_EXC:
            return False

    snap_tol2 = snap_tol_m * snap_tol_m
    snapped: list[tuple[float, float]] = []
    for vx, vy in coords:
        best_corner: tuple[float, float] | None = None
        best_rect = None
        best_d2 = snap_tol2
        for cx, cy, r in corners:
            d2 = (vx - cx) ** 2 + (vy - cy) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_corner = (cx, cy)
                best_rect = r
        if (best_corner is not None and not (
                _guard and _snap_crosses_gap(
                    vx, vy, best_corner[0], best_corner[1], best_rect))):
            snapped.append(best_corner)
        else:
            snapped.append((float(vx), float(vy)))

    # Dedupe consecutive duplicates (within 1 cm).
    deduped: list[tuple[float, float]] = []
    for v in snapped:
        if (not deduped
                or (v[0] - deduped[-1][0]) ** 2
                + (v[1] - deduped[-1][1]) ** 2 > 1e-4):
            deduped.append(v)
    if (len(deduped) > 1
            and (deduped[0][0] - deduped[-1][0]) ** 2
            + (deduped[0][1] - deduped[-1][1]) ** 2 < 1e-4):
        deduped.pop()
    if len(deduped) < 3:
        return poly

    try:
        new_poly = Polygon(deduped + [deduped[0]])
        if not new_poly.is_valid:
            new_poly = new_poly.buffer(0)
        if (new_poly.is_empty
                or new_poly.geom_type != "Polygon"):
            return poly
        return new_poly
    except _GEOM_EXC:
        return poly


def _push_junction_vertices_off_taxi_rect_edges(
        layout: "PavementLayout",
        edge_tol_m: float = 0.5,
        corner_tol_m: float = 2.0,
        edge_gap_m: float = 1.0,
        ) -> int:
    """Per the user 2026-04-24 invariant: a junction polygon may
    share a vertex with a taxi rect ONLY at one of the rect's 4
    corners.  A junction vertex landing on the INTERIOR of a rect
    edge would split that edge at render time and break the rect's
    altitude_high/altitude_low slope convention.

    Per user 2026-04-29: junctions and taxiway/stub/runway rects
    should be handled the SAME WAY as runway-runway crossings —
    the junction should connect to either the LOW or HIGH short
    edge of a rect (i.e. its corners), never to the rect's edge
    interior.

    Two-stage policy applied to every junction ring vertex:

      Stage 1 — collapse redundant edge-interior vertices.
        If a vertex lies on the interior of a rect edge AND its
        ring-adjacent neighbours are both at corners of the SAME
        rect (one each, on the two ends of THAT edge), the vertex
        is redundant.  The junction's ring already connects the
        two corners; the intermediate vertex just splits a single
        rect edge into two pieces.  Drop the vertex — the junction
        edge then runs corner-to-corner along the rect's short
        edge (HIGH-side or LOW-side) cleanly.
      Stage 2 — corner snap / push for survivors.
        For each remaining vertex:
          * Within ``corner_tol_m`` of a rect corner ⇒ snap to
            that corner.
          * Within ``edge_tol_m`` of a rect edge interior ⇒ push
            ``edge_gap_m`` perpendicular outside the rect.
          * Otherwise ⇒ leave alone.

    Geometric only: doesn't touch elevations.  Runway corners are
    treated identically to taxi rect corners so junction vertices
    snap there too — the user's "same logic for all runway
    intersections" requirement.

    Returns the number of junction polygons modified.
    """
    rect_roles = {
        ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
        ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_RUNWAY}
    # Sloped (non-runway) rects used as the no-overlap GUARD: pushing a
    # junction vertex 1 m off one rect's edge can land it INSIDE a
    # neighbouring rect, leaving a small junction-into-rect sliver
    # (HECA stub#107 3 m², secondary_parallel#136 6 m²).  Collect every
    # such rect (any corner count) so the guard below can reject a push
    # that grows the junction's overlap with one.
    guard_roles = {
        ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
        ROLE_STUB, ROLE_CROSS_CONNECTOR}
    guard_rect_polys: list[Polygon] = []
    rects: list[tuple[Polygon, list[tuple[float, float]], str]] = []
    for s in layout.shapes:
        if s.role not in rect_roles:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        if s.role in guard_roles and s.polygon.geom_type == "Polygon":
            guard_rect_polys.append(s.polygon)
        try:
            coords = open_ring(list(s.polygon.exterior.coords))
        except _GEOM_EXC:
            continue
        if len(coords) != 4:
            continue
        rects.append((s.polygon, coords, s.role))
    if not rects:
        return 0
    from ..junction_rules import _snap_grows_rect_overlap

    corner_tol2 = corner_tol_m * corner_tol_m

    def _on_edge_between_corners(
            x: float, y: float,
            corners: list[tuple[float, float]],
            ) -> int | None:
        """Return the edge index (0-3) the point lies on (within
        ``edge_tol_m`` of an edge interior, t ∈ (ε, 1-ε)), or
        None if the point isn't on any rect edge interior."""
        for i in range(4):
            ax, ay = corners[i]
            bx, by = corners[(i + 1) % 4]
            dx = bx - ax
            dy = by - ay
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq <= 0.01:
                continue
            t = ((x - ax) * dx + (y - ay) * dy) / seg_len_sq
            if t <= 0.001 or t >= 0.999:
                continue
            cx_proj = ax + t * dx
            cy_proj = ay + t * dy
            d_sq = ((x - cx_proj) ** 2
                    + (y - cy_proj) ** 2)
            if d_sq <= edge_tol_m * edge_tol_m:
                return i
        return None

    def _at_corner_index(
            x: float, y: float,
            corners: list[tuple[float, float]],
            ) -> int | None:
        """Return the corner index (0-3) the point lies at
        (within ``corner_tol_m``), or None."""
        for ci, (cx, cy) in enumerate(corners):
            if (x - cx) ** 2 + (y - cy) ** 2 <= corner_tol2:
                return ci
        return None

    def _push_off(
            x: float, y: float,
            rect_poly: Polygon,
            corners: list[tuple[float, float]],
            edge_idx: int,
            ) -> tuple[float, float]:
        """Push the point ``edge_gap_m`` perpendicular to the
        rect edge ``edge_idx``, toward the OUTSIDE of the rect."""
        ax, ay = corners[edge_idx]
        bx, by = corners[(edge_idx + 1) % 4]
        dx = bx - ax
        dy = by - ay
        seg_len = math.sqrt(dx * dx + dy * dy)
        if seg_len <= 0.01:
            return (x, y)
        t = ((x - ax) * dx + (y - ay) * dy) / (seg_len * seg_len)
        cx_proj = ax + t * dx
        cy_proj = ay + t * dy
        perp_x = -dy / seg_len
        perp_y = dx / seg_len
        test_x = cx_proj + perp_x * 0.1
        test_y = cy_proj + perp_y * 0.1
        if rect_poly.contains(Point(test_x, test_y)):
            perp_x = -perp_x
            perp_y = -perp_y
        return (cx_proj + perp_x * edge_gap_m,
                cy_proj + perp_y * edge_gap_m)

    n_modified = 0
    # Pieces split off a junction by the buffer(0) validity repair
    # below.  Keeping only the largest piece silently uncovered real
    # pavement (HECA: an 11,568 m² piece carrying the whole
    # Exit-2/Exit-3 ↔ 05R/23L runway connection) — every significant
    # piece is re-added as its own junction shape after the loop.
    _MIN_RECOVERED_PIECE_M2 = 50.0
    recovered: list[tuple[Polygon, str | None,
                          list[tuple[float, float]] | None,
                          list[float] | None]] = []
    for shape in layout.shapes:
        if shape.role != ROLE_JUNCTION:
            continue
        try:
            ring = list(shape.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        # Drop closing repeat for ring traversal.
        if ring and ring[0] == ring[-1]:
            ring_open = ring[:-1]
        else:
            ring_open = ring
        n_v = len(ring_open)
        if n_v < 3:
            continue

        # Stage 1: collapse redundant edge-interior vertices.
        # A vertex v is redundant if its ring-prev and ring-next
        # neighbours are at the two corners of the same rect edge
        # AND v itself lies on that edge's interior.
        keep_mask = [True] * n_v

        def _near_edge_line(x: float, y: float,
                            corners: list[tuple[float, float]],
                            edge_idx: int) -> bool:
            """True if the point lies within ``edge_tol_m`` of the
            INFINITE line through edge ``edge_idx``.  Used for the
            runway coincident-run test: a SEGMENTED runway's long
            edges are collinear across pieces, so a ring-neighbour
            sitting on the NEXT piece's edge (past the seam corner)
            still counts as part of the same run.  A neighbour on a
            perpendicular edge (runway end-cap) fails this test, so
            corner-wrapping rings are never collapsed."""
            ax, ay = corners[edge_idx]
            bx, by = corners[(edge_idx + 1) % 4]
            dx, dy = bx - ax, by - ay
            seg = math.hypot(dx, dy)
            if seg < 0.01:
                return False
            return (abs((x - ax) * dy - (y - ay) * dx) / seg
                    <= edge_tol_m)

        for i in range(n_v):
            vx, vy = ring_open[i]
            px, py = ring_open[(i - 1) % n_v]
            nx, ny = ring_open[(i + 1) % n_v]
            # Coincident-run collapse (user 2026-06-09): rect chains are
            # segmented with corners exactly where junction corners
            # should attach (runways at pavement-intersection seams,
            # taxi rects at their piece joints), so a junction edge that
            # COINCIDES with a rect's edge must conform corner-to-corner
            # — never be pushed 1 m off.  The push scalloped a 400 m
            # runway contact at HECA Exit-2/3 (1.4 m mid-edge cliffs)
            # and opened a 1 m unpaved ring beside the U cross-connector
            # (61 m² of real apt.dat pavement lost + the flanking aprons
            # de-coupled from U's grading, a 2 m seam).  A vertex on a
            # rect edge interior whose ring-neighbours BOTH lie on the
            # same edge line (interior or corner, collinear across the
            # chain's pieces) is part of such a run: drop it; the
            # surviving span corners snap in Stage 2.
            for rect_poly, corners, rect_role in rects:
                v_edge = _on_edge_between_corners(vx, vy, corners)
                if v_edge is None:
                    continue
                if (_near_edge_line(px, py, corners, v_edge)
                        and _near_edge_line(nx, ny, corners, v_edge)):
                    keep_mask[i] = False
                    break
            if not keep_mask[i]:
                continue
            for rect_poly, corners, _rect_role in rects:
                p_corner = _at_corner_index(px, py, corners)
                n_corner = _at_corner_index(nx, ny, corners)
                if p_corner is None or n_corner is None:
                    continue
                # The two neighbour corners must be adjacent
                # corners (i.e. share an edge).  Adjacent corner
                # pairs: (0,1), (1,2), (2,3), (3,0).
                diff = abs(p_corner - n_corner)
                if diff != 1 and diff != 3:
                    continue
                # The edge between them is index = min(...) if
                # adjacent, but for the wrap (0,3 / 3,0) it's
                # edge 3.  Just identify by the corner pair.
                edge_idx = (
                    min(p_corner, n_corner)
                    if diff == 1 else 3)
                v_edge = _on_edge_between_corners(vx, vy, corners)
                if v_edge != edge_idx:
                    continue
                # Vertex v sits on the edge between two corners
                # that are already in the ring as neighbours.
                # Drop it — the junction's ring will go
                # corner-to-corner along the rect edge.
                keep_mask[i] = False
                break
        # Survivor index list lets us drop the matching entries from
        # ``shape.node_altitudes`` after the rebuild — the dropped
        # vertices' altitudes are no longer needed but the kept
        # vertices' altitudes ARE still valid (the solver's elevation
        # field depends on the polygon being valid AND on per-vertex
        # values; nuking them all forces re-derivation from scratch
        # and reintroduces grade violations the solver already fixed).
        survivor_idx = [i for i in range(n_v) if keep_mask[i]]
        ring_after_collapse = [ring_open[i] for i in survivor_idx]
        n_collapsed = n_v - len(ring_after_collapse)

        # Stage 2: corner-snap / edge-push the survivors.
        new_ring: list[tuple[float, float]] = []
        n_snapped = 0
        n_pushed = 0
        n_keep = len(ring_after_collapse)
        for vi2, (vx, vy) in enumerate(ring_after_collapse):
            pvx, pvy = ring_after_collapse[(vi2 - 1) % n_keep]
            nvx, nvy = ring_after_collapse[(vi2 + 1) % n_keep]
            target = (vx, vy)
            for rect_poly, corners, _rect_role in rects:
                ci = _at_corner_index(vx, vy, corners)
                if ci is not None:
                    target = corners[ci]
                    if target != (vx, vy):
                        n_snapped += 1
                    break
                ei = _on_edge_between_corners(vx, vy, corners)
                if ei is not None:
                    # Span-end reconciliation (user 2026-06-09): rect
                    # chains carry piece corners exactly where junction
                    # corners should attach (runway pavement-
                    # intersection seams, taxi-rect joints), but
                    # simplification drift can leave the junction's
                    # span-end vertex a few metres from its corner —
                    # past the 2 m corner snap, so it used to get the
                    # 1 m push, leaving a tapered cliff wedge (HECA
                    # #379↔#182: vertex 5 m from the 181/182 seam).
                    # Snap ALONG the edge to the nearest corner of THIS
                    # edge instead; the movement stays on the shared
                    # boundary line, so distortion is minimal.  Runways
                    # get a wider tolerance (long pieces, larger drift)
                    # than taxi rects (pieces can be 20-50 m).
                    _along_snap = (10.0 if _rect_role == ROLE_RUNWAY
                                   else 5.0)
                    best_c = None
                    best_d = _along_snap
                    for cidx in (ei, (ei + 1) % 4):
                        cx2, cy2 = corners[cidx]
                        d = math.hypot(cx2 - vx, cy2 - vy)
                        if d < best_d:
                            best_d = d
                            best_c = (cx2, cy2)
                    if best_c is not None:
                        target = best_c
                        n_snapped += 1
                        break
                    # FLUSH-CONTACT span end on a SLOPING (long) edge
                    # (user 2026-06-09, HECA U connector): the piece
                    # runs ALONG this rect's edge (a ring-neighbour
                    # lies on the same edge line) and its span end is
                    # far from any rect corner — the rect simply has
                    # no joint here YET.  Pushing 1 m off opened an
                    # unpaved ring of real apt.dat pavement AND
                    # de-coupled the flanking pieces' grading (2 m
                    # seam).  Leave the vertex in place: the later
                    # ``_split_sloped_rects_at_violations`` pass splits
                    # the rect AT this vertex, making it a legal
                    # shared corner.  (Stray vertices — no neighbour
                    # on the edge line — keep the push.)
                    e_len = [math.hypot(
                        corners[(k + 1) % 4][0] - corners[k][0],
                        corners[(k + 1) % 4][1] - corners[k][1])
                        for k in range(4)]
                    is_long = e_len[ei] >= sorted(e_len)[2] - 0.01
                    if is_long and (
                            _near_edge_line(pvx, pvy, corners, ei)
                            or _near_edge_line(nvx, nvy, corners, ei)):
                        break               # leave in place
                    target = _push_off(
                        vx, vy, rect_poly, corners, ei)
                    if target != (vx, vy):
                        n_pushed += 1
                    break
            new_ring.append(target)

        if n_collapsed == 0 and n_snapped == 0 and n_pushed == 0:
            continue

        # Re-close the ring and rebuild the polygon.
        if new_ring and new_ring[0] != new_ring[-1]:
            new_ring_closed = new_ring + [new_ring[0]]
        else:
            new_ring_closed = new_ring
        try:
            new_poly = Polygon(new_ring_closed,
                                list(shape.polygon.interiors))
            buffer_repaired = False
            extra_pieces: list[Polygon] = []
            if not new_poly.is_valid:
                # The original ring may already be self-intersecting
                # — buffer(0) can return a MultiPolygon with one
                # main piece + tiny artifacts.  The largest piece
                # keeps this shape's slot; the OTHER pieces are NOT
                # artifacts in general (a bowtie split can carve off
                # thousands of m² of real pavement) — collect every
                # piece ≥ _MIN_RECOVERED_PIECE_M2 for re-adding.
                fixed = new_poly.buffer(0)
                if fixed.geom_type == "Polygon":
                    new_poly = fixed
                    buffer_repaired = True
                elif (fixed.geom_type == "MultiPolygon"
                        and not fixed.is_empty):
                    _parts = sorted(
                        (g for g in fixed.geoms
                         if g.geom_type == "Polygon"
                         and not g.is_empty),
                        key=lambda g: -g.area)
                    new_poly = _parts[0] if _parts else None
                    extra_pieces = [
                        g for g in _parts[1:]
                        if g.area >= _MIN_RECOVERED_PIECE_M2]
                    buffer_repaired = True
                else:
                    new_poly = None
            if (new_poly is not None
                    and new_poly.geom_type == "Polygon"
                    and not new_poly.is_empty
                    and not _snap_grows_rect_overlap(
                        shape.polygon, new_poly, guard_rect_polys)):
                # Capture the OLD ring + per-vertex altitudes before
                # we overwrite the polygon — needed for the nearest-
                # neighbour fallback below when the new ring's
                # vertex count differs.
                _old_alts = (list(shape.node_altitudes)
                              if shape.node_altitudes else None)
                _old_open = list(ring_open)  # captured pre-rebuild
                shape.polygon = new_poly
                for _g in extra_pieces:
                    recovered.append(
                        (_g, shape.ref, _old_open, _old_alts))
                n_new = len(list(new_poly.exterior.coords)) - 1
                # Preserve per-vertex altitudes where we can.  When
                # Stage 1 collapsed K vertices but Stage 2 only
                # snapped/pushed in place, the survivor index list
                # maps the new ring to the original altitudes 1:1.
                # When buffer(0) restructured the ring or vertex
                # counts otherwise don't match, fall back to a
                # NEAREST-NEIGHBOUR resampling against the old
                # ring so the polygon retains its elevation field.
                # Per user 2026-04-29 (CYXY apron regression):
                # just setting ``node_altitudes = None`` left
                # CYXY's 174 k m² apron junction with no altitude
                # at all — every X-Plane interpolation neighbour
                # was at a different elevation, producing the
                # "terrain all over the place" the user saw.
                # Nearest-neighbour resample of the new ring against
                # the captured OLD ring vertices.  Per user
                # 2026-04-29 (CYXY apron regression): the prior
                # behaviour of setting ``node_altitudes = None``
                # whenever the vertex count changed left the giant
                # 174 k m² apron junction with no altitude at all,
                # producing the "terrain all over the place" the
                # user saw.  NN resampling preserves the elevation
                # field through Stage-1 collapse, Stage-2 push, AND
                # buffer(0) MultiPolygon repair.
                if not _old_alts or not _old_open:
                    n_modified += 1
                    continue
                src_alts_open = (
                    _old_alts[:-1]
                    if (len(_old_alts) == len(_old_open) + 1
                        and _old_alts[0] == _old_alts[-1])
                    else _old_alts[:len(_old_open)])
                if not src_alts_open:
                    n_modified += 1
                    continue
                new_open = list(new_poly.exterior.coords)
                if new_open and new_open[0] == new_open[-1]:
                    new_open = new_open[:-1]
                new_alts: list[float] = []
                for nx, ny in new_open:
                    best_d2 = float("inf")
                    best_a = src_alts_open[0]
                    for k, (sx, sy) in enumerate(_old_open):
                        if k >= len(src_alts_open):
                            break
                        d2 = (nx - sx) ** 2 + (ny - sy) ** 2
                        if d2 < best_d2:
                            best_d2 = d2
                            best_a = src_alts_open[k]
                    new_alts.append(round(float(best_a), 1))
                if new_alts:
                    shape.node_altitudes = (
                        new_alts + [new_alts[0]])
                n_modified += 1
        except _GEOM_EXC:
            pass

    # Re-add the pieces the buffer(0) bowtie split carved off the
    # kept-largest junctions.  A piece that would overlap a sloped
    # rect is skipped (same no-overlap concern as the push guard).
    n_recovered = 0
    for piece, ref, old_open, old_alts in recovered:
        try:
            if any(piece.intersection(rp).area > 1.0
                   for rp in guard_rect_polys):
                continue
            ns = BuiltShape(polygon=piece, role=ROLE_JUNCTION, ref=ref)
            if old_alts and old_open:
                src_alts = (
                    old_alts[:-1]
                    if (len(old_alts) == len(old_open) + 1
                        and old_alts[0] == old_alts[-1])
                    else old_alts[:len(old_open)])
                p_open = list(piece.exterior.coords)
                if p_open and p_open[0] == p_open[-1]:
                    p_open = p_open[:-1]
                if src_alts and p_open:
                    alts = []
                    for nx, ny in p_open:
                        best_d2 = float("inf")
                        best_a = src_alts[0]
                        for k, (sx, sy) in enumerate(old_open):
                            if k >= len(src_alts):
                                break
                            d2 = (nx - sx) ** 2 + (ny - sy) ** 2
                            if d2 < best_d2:
                                best_d2 = d2
                                best_a = src_alts[k]
                        alts.append(round(float(best_a), 1))
                    ns.node_altitudes = alts + [alts[0]]
            layout.shapes.append(ns)
            n_recovered += 1
        except _GEOM_EXC:
            continue
    if n_recovered:
        try:
            import O4_UI_Utils as UI
            UI.vprint(1,
                f"  [pav-builder] junction vertex-push: re-added "
                f"{n_recovered} piece(s) split off by ring validity "
                f"repair (total {sum(p.area for p, *_ in recovered):,.0f}"
                f" m²).")
        except Exception:
            pass
        n_modified += n_recovered
    return n_modified


def _insert_rect_corners_into_grazing_junction_edges(
        layout: "PavementLayout",
        taxi_tol_m: float = 0.6,
        runway_tol_m: float = 1.5,
        ) -> int:
    """Insert a rect/runway CORNER into a junction EDGE that grazes past
    it — the inverse of the vertex-based machinery above (s73, the last
    two grade-gate steps).

    Stage 1/2 only act when a junction VERTEX lies near a rect; a long
    straight junction edge that passes within a hair of a rect corner
    with no junction vertex anywhere nearby is invisible to them, so the
    two shapes never share a node there and their solved surfaces step
    apart at emit: SPJC read a 0.61 m mid-edge step at a runway corner
    0.92 m off a junction edge; HECA junction -10193's single 314 m edge
    converges onto rect TX29's long edge (corners at 0.49 m and 0.01 m
    lateral) and stepped 0.66 m.  Routing the junction boundary THROUGH
    the corner is the designed contract (a corner touch is the one legal
    junction↔rect contact) and makes the node canonically shared, so the
    solver couples the surfaces and the in-between segment runs
    corner-to-corner exactly like the same junction's conformed sides.

    Runway corners get a wider capture (``runway_tol_m`` =
    RUNWAY_BOUNDARY_TOL_M's class) than taxi-rect corners.  Geometric
    only; per-vertex altitudes, when present, interpolate at the
    insertion point.  Returns the number of junctions modified.
    """
    rect_roles = {
        ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
        ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_RUNWAY}
    corners: list[tuple[float, float, bool]] = []
    rect_polys: list[Polygon] = []
    for s in layout.shapes:
        if s.role not in rect_roles:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            cs = open_ring(list(s.polygon.exterior.coords))
        except _GEOM_EXC:
            continue
        if len(cs) != 4:
            continue
        is_rwy = s.role == ROLE_RUNWAY
        corners.extend((x, y, is_rwy) for (x, y) in cs)
        rect_polys.append(s.polygon)
    if not corners:
        return 0
    n_modified = 0
    # Pieces pinched off by an exact-corner insertion (see below) —
    # re-added as their own junction shapes after the loop.
    _MIN_PIECE_M2 = 50.0
    pinched: list[tuple[Polygon, str | None,
                        list[tuple[float, float]],
                        list[float] | None]] = []
    for shape in layout.shapes:
        if shape.role != ROLE_JUNCTION:
            continue
        if shape.polygon is None or shape.polygon.is_empty:
            continue
        try:
            ring = list(shape.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        ring_open = ring[:-1] if (ring and ring[0] == ring[-1]) else ring
        n_v = len(ring_open)
        if n_v < 3:
            continue
        # Cheap reject: corner must be near the junction at all.
        minx, miny, maxx, maxy = shape.polygon.bounds
        ins: dict[int, list[tuple[float, tuple[float, float]]]] = {}
        _gdbg = os.environ.get("O4_GRAZE_DEBUG") == "1"
        pinch_inserted = False
        for (cx, cy, is_rwy) in corners:
            tol = runway_tol_m if is_rwy else taxi_tol_m
            if not (minx - tol <= cx <= maxx + tol
                    and miny - tol <= cy <= maxy + tol):
                continue
            # A ring vertex EXACTLY at the corner means the corner is
            # attached — but only on the edges INCIDENT to that vertex.
            # A different, non-incident edge of the same ring can still
            # graze past the corner (HECA -10193: the conformed inner
            # run holds TX29's corners while the 314 m outer boundary
            # edge runs collinear 0-0.5 m outside them, leaving a
            # sliver whose lerp steps off the rect plane mid-edge).
            # Candidate edges for an exact corner therefore exclude the
            # incident ones; inserting on a non-incident edge pinches
            # the ring at the corner (resolved by the buffer(0) split
            # below).
            exact_idxs = {
                vi for vi, (vx, vy) in enumerate(ring_open)
                if (cx - vx) ** 2 + (cy - vy) ** 2 <= 1e-4}
            near = any((cx - vx) ** 2 + (cy - vy) ** 2 <= 0.25
                       for (vx, vy) in ring_open)
            best = None
            for i in range(n_v):
                if exact_idxs and (
                        i in exact_idxs or (i + 1) % n_v in exact_idxs):
                    continue
                ax, ay = ring_open[i]
                bx, by = ring_open[(i + 1) % n_v]
                dx, dy = bx - ax, by - ay
                seg2 = dx * dx + dy * dy
                if seg2 < 1.0:
                    continue
                t = ((cx - ax) * dx + (cy - ay) * dy) / seg2
                if t <= 0.02 or t >= 0.98:
                    continue
                px, py = ax + t * dx, ay + t * dy
                d2 = (cx - px) ** 2 + (cy - py) ** 2
                if d2 <= tol * tol and (best is None or d2 < best[0]):
                    best = (d2, i, t)
            if _gdbg and (best is not None or near):
                print(f"[graze]  cand corner ({cx:.0f},{cy:.0f}) "
                      f"rwy={is_rwy} near_vert={near} "
                      f"exact={sorted(exact_idxs)} "
                      f"best={'-' if best is None else f'{best[0] ** 0.5:.2f}m@e{best[1]}t{best[2]:.2f}'}")
            if not exact_idxs and near:
                continue
            if best is not None:
                ins.setdefault(best[1], []).append((best[2], (cx, cy)))
                if exact_idxs:
                    pinch_inserted = True
        if not ins:
            continue
        alts = (list(shape.node_altitudes)
                if shape.node_altitudes else None)
        closed_alts = alts is not None and len(alts) == n_v + 1
        if alts is not None and len(alts) not in (n_v, n_v + 1):
            alts = None
        new_ring: list[tuple[float, float]] = []
        new_alts: list[float] | None = [] if alts is not None else None
        for i in range(n_v):
            new_ring.append(ring_open[i])
            if new_alts is not None:
                new_alts.append(alts[i])
            for t, pt in sorted(ins.get(i, ())):
                if (pt[0] - new_ring[-1][0]) ** 2 \
                        + (pt[1] - new_ring[-1][1]) ** 2 < 0.0025:
                    continue                 # duplicate corner
                new_ring.append(pt)
                if new_alts is not None:
                    a0 = alts[i]
                    a1 = alts[(i + 1) % n_v]
                    new_alts.append(a0 + t * (a1 - a0))
        try:
            new_poly = Polygon(new_ring + [new_ring[0]],
                               list(shape.polygon.interiors))
        except _GEOM_EXC:
            continue
        # Bending an edge ≤ tol can self-intersect a concave ring —
        # skip rather than buffer-repair (this pass is opportunistic).
        # EXCEPT for the deliberate exact-corner pinch: there the ring
        # touches itself at the inserted corner(s) by construction, and
        # buffer(0) resolves it into the real pieces (the grazing
        # sliver between the outer edge and the conformed run has
        # ~zero area and vanishes).  Guarded by area conservation so a
        # ring-fold that EATS a lobe (the s70 keep-largest lesson) is
        # never accepted.
        if (not new_poly.is_valid or new_poly.is_empty
                or new_poly.geom_type != "Polygon"):
            if not pinch_inserted:
                continue
            try:
                fixed = new_poly.buffer(0)
            except _GEOM_EXC:
                continue
            parts = []
            if fixed.geom_type == "Polygon" and not fixed.is_empty:
                parts = [fixed]
            elif fixed.geom_type == "MultiPolygon":
                parts = sorted(
                    (g for g in fixed.geoms
                     if g.geom_type == "Polygon" and not g.is_empty),
                    key=lambda g: -g.area)
            if not parts:
                continue
            if abs(sum(g.area for g in parts) - shape.polygon.area) \
                    > 0.01 * shape.polygon.area + 50.0:
                continue
            ring_for_alts = list(new_ring)
            alts_for_alts = (list(new_alts)
                             if new_alts is not None else None)

            def _nn_alts(poly: Polygon) -> list[float] | None:
                if alts_for_alts is None:
                    return None
                p_open = open_ring(list(poly.exterior.coords))
                out = []
                for nx, ny in p_open:
                    best_d2 = float("inf")
                    best_a = alts_for_alts[0]
                    for k, (sx, sy) in enumerate(ring_for_alts):
                        d2 = (nx - sx) ** 2 + (ny - sy) ** 2
                        if d2 < best_d2:
                            best_d2 = d2
                            best_a = alts_for_alts[k]
                    out.append(round(float(best_a), 1))
                return out + [out[0]]
            shape.polygon = parts[0]
            if alts_for_alts is not None:
                shape.node_altitudes = _nn_alts(parts[0])
            for g in parts[1:]:
                if g.area < _MIN_PIECE_M2:
                    continue
                if any(g.intersection(rp).area > 1.0
                       for rp in rect_polys):
                    continue
                pinched.append(
                    (g, shape.ref, ring_for_alts, alts_for_alts))
            n_modified += 1
            if os.environ.get("O4_GRAZE_DEBUG") == "1":
                print(f"[graze] junction ref={shape.ref or '?'} "
                      f"pinch split: kept {parts[0].area:,.0f} m², "
                      f"{len(parts) - 1} other piece(s)")
            continue
        shape.polygon = new_poly
        if new_alts is not None:
            if closed_alts:
                new_alts.append(new_alts[0])
            shape.node_altitudes = new_alts
        n_modified += 1
        if os.environ.get("O4_GRAZE_DEBUG") == "1":
            pts = [pt for lst in ins.values() for (_t, pt) in lst]
            print(f"[graze] junction ref={shape.ref or '?'} "
                  f"inserted {len(pts)} corner(s): "
                  + " ".join(f"({px:.0f},{py:.0f})" for px, py in pts))
    # Re-add the real pieces the exact-corner pinch split off (the
    # junction was genuinely two components joined by the grazing
    # sliver) — same recovered-pieces contract as the vertex-push pass.
    for piece, ref, src_open, src_alts in pinched:
        ns = BuiltShape(polygon=piece, role=ROLE_JUNCTION, ref=ref)
        if src_alts and src_open:
            try:
                p_open = open_ring(list(piece.exterior.coords))
            except _GEOM_EXC:
                p_open = []
            if p_open:
                alts = []
                for nx, ny in p_open:
                    best_d2 = float("inf")
                    best_a = src_alts[0]
                    for k, (sx, sy) in enumerate(src_open):
                        d2 = (nx - sx) ** 2 + (ny - sy) ** 2
                        if d2 < best_d2:
                            best_d2 = d2
                            best_a = src_alts[k]
                    alts.append(round(float(best_a), 1))
                ns.node_altitudes = alts + [alts[0]]
        layout.shapes.append(ns)
        n_modified += 1
        if os.environ.get("O4_GRAZE_DEBUG") == "1":
            print(f"[graze] re-added pinched piece "
                  f"{piece.area:,.0f} m² (ref={ref or '?'})")
    if os.environ.get("O4_GRAZE_DEBUG") == "1":
        print(f"[graze] insert pass: {n_modified} junction(s) modified")
    return n_modified


def _insert_junction_corners_into_grazing_apron_edges(
        layout: "PavementLayout", tol_m: float = 1.0) -> int:
    """Insert a junction ring CORNER into an APRON edge that grazes past
    it with no shared node — the junction↔apron counterpart of
    :func:`_insert_rect_corners_into_grazing_junction_edges`.

    HECA #199: a junction vertex sat 0.73 m off apron #194's edge
    BETWEEN two properly shared corners; the junction's solved value
    steps off the apron edge's straight lerp at emit (0.51/0.59 m
    vertex-to-edge + mid-edge steps — the grade gate's step assert).
    Routing the apron boundary THROUGH the corner makes the node
    canonically shared so the solver couples the surfaces.

    Geometric only, pre-solve; per-vertex altitudes, when present,
    interpolate at the insertion point.  Corners that already coincide
    with an apron vertex (≤0.05 m) are attached — skipped.  Returns the
    number of apron shapes modified.
    """
    corners: list[tuple[float, float]] = []
    for s in layout.shapes:
        if s.role != ROLE_JUNCTION:
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        try:
            corners.extend(open_ring(list(s.polygon.exterior.coords)))
        except _GEOM_EXC:
            continue
    if not corners:
        return 0
    n_modified = 0
    for shape in layout.shapes:
        if shape.role != ROLE_APRON:
            continue
        if shape.polygon is None or shape.polygon.is_empty:
            continue
        try:
            ring = list(shape.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        ring_open = ring[:-1] if (ring and ring[0] == ring[-1]) else ring
        n_v = len(ring_open)
        if n_v < 3:
            continue
        minx, miny, maxx, maxy = shape.polygon.bounds
        ins: dict[int, list[tuple[float, tuple[float, float]]]] = {}
        for (cx, cy) in corners:
            if not (minx - tol_m <= cx <= maxx + tol_m
                    and miny - tol_m <= cy <= maxy + tol_m):
                continue
            # already an apron vertex (shared/welded) → attached
            if any((cx - vx) ** 2 + (cy - vy) ** 2 <= 0.0025
                   for (vx, vy) in ring_open):
                continue
            best = None
            for i in range(n_v):
                ax, ay = ring_open[i]
                bx, by = ring_open[(i + 1) % n_v]
                dx, dy = bx - ax, by - ay
                seg2 = dx * dx + dy * dy
                if seg2 < 1.0:
                    continue
                t = ((cx - ax) * dx + (cy - ay) * dy) / seg2
                if t <= 0.02 or t >= 0.98:
                    continue
                px, py = ax + t * dx, ay + t * dy
                d2 = (cx - px) ** 2 + (cy - py) ** 2
                if d2 <= tol_m * tol_m and (best is None or d2 < best[0]):
                    best = (d2, i, t)
            if best is not None:
                ins.setdefault(best[1], []).append((best[2], (cx, cy)))
        if not ins:
            continue
        alts = (list(shape.node_altitudes)
                if shape.node_altitudes else None)
        closed_alts = alts is not None and len(alts) == n_v + 1
        if alts is not None and len(alts) not in (n_v, n_v + 1):
            alts = None
        new_ring: list[tuple[float, float]] = []
        new_alts: list[float] | None = [] if alts is not None else None
        for i in range(n_v):
            new_ring.append(ring_open[i])
            if new_alts is not None:
                new_alts.append(alts[i])
            for t, pt in sorted(ins.get(i, ())):
                if (pt[0] - new_ring[-1][0]) ** 2 \
                        + (pt[1] - new_ring[-1][1]) ** 2 < 0.0025:
                    continue
                new_ring.append(pt)
                if new_alts is not None:
                    a0 = alts[i]
                    a1 = alts[(i + 1) % n_v]
                    new_alts.append(a0 + t * (a1 - a0))
        try:
            new_poly = Polygon(new_ring + [new_ring[0]],
                               list(shape.polygon.interiors))
        except _GEOM_EXC:
            continue
        # Bending an apron edge ≤ tol can self-intersect a concave ring —
        # this pass is opportunistic, skip rather than repair.
        if (not new_poly.is_valid or new_poly.is_empty
                or new_poly.geom_type != "Polygon"):
            continue
        if abs(new_poly.area - shape.polygon.area) \
                > 0.01 * shape.polygon.area + 50.0:
            continue
        shape.polygon = new_poly
        if new_alts is not None:
            if closed_alts:
                new_alts.append(new_alts[0])
            shape.node_altitudes = new_alts
        n_modified += 1
        if os.environ.get("O4_GRAZE_DEBUG") == "1":
            pts = [pt for lst in ins.values() for (_t, pt) in lst]
            print(f"[graze] apron ref={shape.ref or '?'} inserted "
                  f"{len(pts)} junction corner(s): "
                  + " ".join(f"({px:.0f},{py:.0f})" for px, py in pts))
    if os.environ.get("O4_GRAZE_DEBUG") == "1":
        print(f"[graze] apron insert pass: {n_modified} apron(s) modified")
    return n_modified


def _drop_spike_vertices(
    ring: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Drop any ring vertex that lies within ``SPIKE_VERTEX_TOL_M``
    of a NON-adjacent edge of the same ring.

    Such a vertex represents a degenerate "stick-out-and-return"
    in the polygon boundary — the ring went away from a straight
    section and returned right onto it, leaving a near-zero-area
    lobe that's a self-touch / self-intersection in any reasonable
    coordinate precision.

    Iterates to a fixed point (dropping one spike can expose
    another).  Capped at 8 passes against pathological inputs.
    """
    if len(ring) < 4:
        return ring
    for _ in range(8):
        n = len(ring)
        if n < 4:
            break
        keep = [True] * n
        for i in range(n):
            vx, vy = ring[i]
            for j in range(n):
                if abs(i - j) <= 1 or (i == 0 and j == n - 1) or (j == 0 and i == n - 1):
                    continue
                ax, ay = ring[j]
                bx, by = ring[(j + 1) % n]
                dx = bx - ax
                dy = by - ay
                seg2 = dx * dx + dy * dy
                if seg2 < 1e-6:
                    continue
                t = ((vx - ax) * dx + (vy - ay) * dy) / seg2
                if t < 0.0 or t > 1.0:
                    continue
                cx = ax + t * dx
                cy = ay + t * dy
                d2 = (vx - cx) * (vx - cx) + (vy - cy) * (vy - cy)
                if d2 < SPIKE_VERTEX_TOL_M * SPIKE_VERTEX_TOL_M:
                    keep[i] = False
                    break
        new_ring = [r for r, k in zip(ring, keep) if k]
        if len(new_ring) == n:
            break
        ring = new_ring
    return ring




# Airside roles whose geometry the per-surface solver grades — a drop of any
# such piece over _AIRSIDE_DROP_MIN_M2 is a real pavement loss and must never
# be silent (KBNA Donelson 2026-07-16: the shared-vertex weld's largest-part-
# only selection deleted 4.9 k m² of taxiway pavement without a word).
_AIRSIDE_DROP_ROLES = frozenset({
    ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
    ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION, ROLE_APRON,
    ROLE_BUILDING})
_AIRSIDE_DROP_MIN_M2 = 100.0

# When the shared-vertex weld makes a ring invalid, buffer(0) is accepted as
# the healed geometry only if it preserves the shape's footprint to within
# this tolerance (rounding jitter of a clean pinch); a larger shortfall means
# the ring FOLDED over itself and buffer(0) dropped the overlapped half — the
# original polygon is kept instead (heal, never drop).
_WELD_FOOTPRINT_TOL_M2 = 5.0


def _record_airside_drop(layout, shape, poly, area_m2: float,
                         mechanism: str) -> None:
    """Record (and loudly log) that a shared-vertex weld could not preserve
    an airside piece, so the drop is never silent.  Appends an event to
    ``layout.airside_weld_drops`` (a build-time verify counter surfaced by
    ``verification.verify_and_log``) whenever the piece is an airside role
    larger than ``_AIRSIDE_DROP_MIN_M2``.

    The healed paths (keep-original / re-admit-all-lobes) do NOT call this;
    it fires only when a >100 m² airside piece is genuinely lost, so the
    counter reads ZERO on a healthy build."""
    role = getattr(shape, "role", None)
    if role not in _AIRSIDE_DROP_ROLES or area_m2 <= _AIRSIDE_DROP_MIN_M2:
        return
    lat = lon = 0.0
    try:
        if layout.anchor is not None:
            c = poly.centroid
            from ..layout import R_EARTH as _RE
            lat = layout.anchor[0] + math.degrees(c.y / _RE)
            lon = layout.anchor[1] + math.degrees(
                c.x / (_RE * math.cos(math.radians(layout.anchor[0]))))
    except Exception:
        pass
    events = getattr(layout, "airside_weld_drops", None)
    if events is None:
        events = []
        layout.airside_weld_drops = events           # type: ignore[attr-defined]
    events.append({"role": role, "area_m2": round(area_m2, 1),
                   "lat": round(lat, 6), "lon": round(lon, 6),
                   "mechanism": mechanism})
    import O4_UI_Utils as UI
    UI.vprint(1,
        f"  [pav-builder] VERIFY DROP: shared-vertex weld lost "
        f"{role} piece {area_m2:.0f} m² ({mechanism}) "
        f"@{lat:.6f},{lon:.6f} — pavement hole risk.")


def _enforce_shared_vertices(layout: "PavementLayout",
                             tol: float = 1.5) -> None:
    """Collapse all emitted-shape vertices that lie within ``tol``
    of each other to a single canonical point (the cluster mean),
    then rewrite each shape's polygon with those canonical vertices.

    Implements rule 16 (exact shared vertices between adjacent
    shapes).  Must run AFTER all shapes are emitted.
    """
    # Gather every vertex with a (shape_idx, is_interior, ring_idx,
    # vert_idx) handle so we can rewrite them in place.
    handles: list[tuple[int, int, int, int, tuple[float, float]]] = []
    for si, shape in enumerate(layout.shapes):
        poly = shape.polygon
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            continue
        ext = list(poly.exterior.coords)
        if ext and ext[0] == ext[-1]:
            ext = ext[:-1]
        for vi, v in enumerate(ext):
            handles.append((si, 0, 0, vi, (v[0], v[1])))
        for ri, ring in enumerate(poly.interiors):
            rc = list(ring.coords)
            if rc and rc[0] == rc[-1]:
                rc = rc[:-1]
            for vi, v in enumerate(rc):
                handles.append((si, 1, ri, vi, (v[0], v[1])))
    if not handles:
        return

    from collections import defaultdict

    # Union-find over a tol-sized GRID.  The original O(n²) pair scan
    # ("n is typically 200-2000") predates the global slice: at KDFW
    # n ≈ 47k vertices → ~1.1 BILLION pair checks per call, 190 s of
    # the build (cProfile 2026-07-04).  Hashing vertices into
    # tol-sized cells and comparing only the 3×3 neighbourhood visits
    # exactly the pairs the old |dx|,|dy| ≤ tol prefilter kept, so the
    # union COMPONENTS — and therefore the cluster means and the
    # rewritten rings — are byte-identical.
    n = len(handles)
    parent = list(range(n))

    def _find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    coords_only = [h[4] for h in handles]
    cell_size = tol if tol > 0 else 1.0
    grid_buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (ix, iy) in enumerate(coords_only):
        grid_buckets[(int(ix // cell_size), int(iy // cell_size))].append(i)
    for (cell_x, cell_y), members in grid_buckets.items():
        neighbourhood: list[int] = []
        for offset_x in (-1, 0, 1):
            for offset_y in (-1, 0, 1):
                neighbourhood.extend(grid_buckets.get(
                    (cell_x + offset_x, cell_y + offset_y), ()))
        for i in members:
            ix, iy = coords_only[i]
            for j in neighbourhood:
                if j <= i:
                    continue
                jx, jy = coords_only[j]
                dx = ix - jx
                dy = iy - jy
                if dx > tol or dx < -tol or dy > tol or dy < -tol:
                    continue
                if math.hypot(dx, dy) <= tol:
                    _union(i, j)

    # Compute the canonical point per cluster.  RUNWAY = GEOMETRY
    # AUTHORITY (R1, 2026-07-08 formation diagnosis): the raw cluster
    # MEAN detaches runway frontages from the runway contour — junction
    # frontage chains cluster among themselves (the runway edge has no
    # vertex mid-frontage), and the mean lands 0.014-0.27 m off the
    # runway boundary, minting the epsilon-wedge / sliver-overlap /
    # mixed-value classes at every runway frontage (measured at KCLT
    # 18L + SPJC 16L, gate-on AND gate-off).  Three rules:
    #   1. Cluster contains runway-owned vertices: the canonical point
    #      IS the runway vertex position — runway vertices (profile
    #      stations) never move.  Runway vertices at MATERIALLY
    #      different positions in one cluster (crossing areas, or two
    #      stations caught in one cluster) → leave the whole cluster
    #      unmoved: never average two authorities (also prevents the
    #      consecutive-dedup below from dropping a ring station and
    #      misaligning its per-vertex altitudes).
    #   2. No runway vertex, but the mean lies within ``tol`` of a
    #      runway boundary: canonical point = the mean PROJECTED onto
    #      the nearest runway boundary, so frontage clusters land ON
    #      the runway contour and the weld/conformance chain can bind
    #      them to the runway's nodes.
    #   3. Anything else: the cluster mean, exactly as before.
    _RUNWAY_AUTHORITY_AGREEMENT_M = 0.01
    runway_shape_indices = {
        shape_index for shape_index, shape in enumerate(layout.shapes)
        if shape.role == ROLE_RUNWAY
        and shape.polygon is not None
        and not shape.polygon.is_empty
        and shape.polygon.geom_type == "Polygon"}
    runway_boundaries: list[tuple[tuple[float, float, float, float],
                                  "LineString"]] = []
    for shape_index in runway_shape_indices:
        exterior = LineString(
            layout.shapes[shape_index].polygon.exterior.coords)
        min_x, min_y, max_x, max_y = exterior.bounds
        runway_boundaries.append(
            ((min_x - tol, min_y - tol, max_x + tol, max_y + tol),
             exterior))

    def _project_onto_runway_boundary(px: float, py: float):
        """Nearest point on any runway boundary within ``tol`` of
        (px, py), or None."""
        best = None
        for (min_x, min_y, max_x, max_y), exterior in runway_boundaries:
            if not (min_x <= px <= max_x and min_y <= py <= max_y):
                continue
            point = Point(px, py)
            distance = exterior.distance(point)
            if distance <= tol and (best is None or distance < best[0]):
                projected = exterior.interpolate(exterior.project(point))
                best = (distance, (projected.x, projected.y))
        return None if best is None else best[1]

    cluster_members: dict[int, list[int]] = defaultdict(list)
    for i in range(len(handles)):
        cluster_members[_find(i)].append(i)
    canonical: dict[int, tuple[float, float] | None] = {}
    n_mixed_authority_clusters = 0
    for root, members in cluster_members.items():
        runway_positions = [
            handles[m][4] for m in members
            if handles[m][0] in runway_shape_indices]
        if runway_positions:
            anchor = runway_positions[0]
            if any(math.hypot(px - anchor[0], py - anchor[1])
                   > _RUNWAY_AUTHORITY_AGREEMENT_M
                   for px, py in runway_positions[1:]):
                # Two runway authorities disagree — leave the whole
                # cluster untouched (rule 1 fallback).
                canonical[root] = None
                n_mixed_authority_clusters += 1
            else:
                canonical[root] = anchor
            continue
        sx = sum(handles[m][4][0] for m in members) / len(members)
        sy = sum(handles[m][4][1] for m in members) / len(members)
        projected = _project_onto_runway_boundary(sx, sy)
        canonical[root] = projected if projected is not None else (sx, sy)
    if n_mixed_authority_clusters:
        import O4_UI_Utils as UI
        UI.vprint(2,
            f"  [pav-builder] shared-vertex weld: left "
            f"{n_mixed_authority_clusters} mixed-runway-authority "
            f"cluster(s) unmoved.")

    # Rewrite each shape's rings with the canonical coords.
    new_coords_by_shape: dict[int, dict[tuple[int, int, int],
                                        tuple[float, float]]] = defaultdict(dict)
    for i, h in enumerate(handles):
        si, is_int, ri, vi, _orig = h
        canonical_point = canonical[_find(i)]
        if canonical_point is None:
            continue  # mixed-authority cluster: every member stays put
        new_coords_by_shape[si][(is_int, ri, vi)] = canonical_point

    # Extra polygon parts recovered from a pinched (self-touching) ring —
    # appended to layout.shapes AFTER the rewrite loop (mutating the list
    # mid-enumerate would shift indices).  Each entry is (source_shape,
    # part_polygon).
    _recovered_parts: list[tuple["BuiltShape", Polygon]] = []
    for si, shape in enumerate(layout.shapes):
        poly = shape.polygon
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            continue
        if si not in new_coords_by_shape:
            continue
        _area_before = poly.area
        # Rebuild exterior.
        ext = list(poly.exterior.coords)
        if ext and ext[0] == ext[-1]:
            ext = ext[:-1]
        new_ext = [new_coords_by_shape[si].get((0, 0, vi), ext[vi])
                   for vi in range(len(ext))]
        # Drop consecutive duplicates that arose from clustering.
        dedup_ext: list[tuple[float, float]] = []
        for c in new_ext:
            if not dedup_ext or math.hypot(
                    c[0] - dedup_ext[-1][0],
                    c[1] - dedup_ext[-1][1]) > 0.05:
                dedup_ext.append(c)
        if (len(dedup_ext) >= 2
                and math.hypot(dedup_ext[0][0] - dedup_ext[-1][0],
                               dedup_ext[0][1] - dedup_ext[-1][1]) < 0.05):
            dedup_ext = dedup_ext[:-1]
        if len(dedup_ext) < 3:
            # The CLUSTER-REWRITTEN ring collapsed to < 3 distinct
            # vertices (a thin sliver whose own opposite-edge vertices
            # got pulled together by the tol-sized weld).  Emptying it
            # here silently deletes real pavement (KBNA Donelson: 275 m²
            # aprons vanished this way).  HEAL, don't drop: keep the
            # ORIGINAL un-clustered polygon so the area survives into the
            # downstream weld / solve.  The shared-vertex INVARIANT
            # validator (_validate_shared_vertex_invariant) is never run
            # in production, so the un-welded sliver is harmless; a
            # genuine (real-pavement) drop is the far worse outcome.
            continue                                # keep original polygon
        # Rebuild interiors.
        new_interiors: list[list[tuple[float, float]]] = []
        for ri, ring in enumerate(poly.interiors):
            rc = list(ring.coords)
            if rc and rc[0] == rc[-1]:
                rc = rc[:-1]
            new_ring = [new_coords_by_shape[si].get((1, ri, vi), rc[vi])
                        for vi in range(len(rc))]
            dedup_ring: list[tuple[float, float]] = []
            for c in new_ring:
                if not dedup_ring or math.hypot(
                        c[0] - dedup_ring[-1][0],
                        c[1] - dedup_ring[-1][1]) > 0.05:
                    dedup_ring.append(c)
            if len(dedup_ring) >= 3:
                new_interiors.append(dedup_ring)
        try:
            new_poly = Polygon(dedup_ext, new_interiors)
            # The tol-sized weld can pull two NON-adjacent ring vertices of
            # the SAME shape together (the consecutive-dedup above catches
            # only adjacent duplicates), yielding an INVALID ring.  Two
            # cases, distinguished by whether buffer(0) preserves the
            # footprint:
            #   (a) PINCH — the ring folds into lobes that merely TOUCH at
            #       the shared corner (zero overlap); buffer(0) returns a
            #       MultiPolygon reproducing the full area.  Keep EVERY lobe
            #       (largest on this shape, the rest re-admitted as their own
            #       airside pieces) so none is discarded.
            #   (b) FOLD — a thin wedge folds back OVER itself (self-
            #       crossing); buffer(0) returns only the un-folded footprint
            #       and silently drops the overlapped half (KBNA Donelson: a
            #       7,982 m² junction collapsed to 3,054 → a 4,928 m² hole;
            #       plus 5,882 / 4,737 / 2,234 m² elsewhere airport-wide).
            #       There is no valid welded ring, so REVERT to the original
            #       un-welded polygon — the pavement survives and the
            #       authoritative pre-solve weld (_unify_airside_geometry)
            #       re-welds it against its neighbours.
            if (new_poly.geom_type == "Polygon"
                    and not new_poly.is_empty
                    and not new_poly.is_valid):
                healed = None
                try:
                    fixed = new_poly.buffer(0)
                    parts = [g for g in getattr(fixed, "geoms", [fixed])
                             if g.geom_type == "Polygon" and g.is_valid
                             and not g.is_empty]
                    if parts and (sum(g.area for g in parts)
                                  >= _area_before - _WELD_FOOTPRINT_TOL_M2):
                        parts.sort(key=lambda g: g.area, reverse=True)
                        healed = parts[0]
                        for extra in parts[1:]:
                            _recovered_parts.append((shape, extra))
                except _GEOM_EXC:
                    healed = None
                if healed is None:
                    # Fold: keep the original polygon (heal, never drop).
                    continue
                new_poly = healed
            if (new_poly.geom_type == "Polygon"
                    and not new_poly.is_empty):
                shape.polygon = new_poly
            # else: rewrite produced nothing usable — keep the original
            # polygon (shape.polygon unchanged) rather than dropping it.
        except _GEOM_EXC:
            pass

    # Re-admit the recovered pinch lobes as their own shapes.  They carry
    # the source role/ref so they flow through the rest of the pipeline
    # (spine slice / unify / per-surface solve) like any other airside
    # piece; per-vertex node_altitudes are left to the (downstream) solve,
    # matching how this function already reshapes the primary lobe's ring
    # without re-deriving its altitudes.
    for src_shape, part in _recovered_parts:
        new_shape = BuiltShape(
            polygon=part,
            role=src_shape.role,
            ref=src_shape.ref,
            source_axis=src_shape.source_axis,
            is_bridge=src_shape.is_bridge)
        if src_shape.altitude is not None:
            new_shape.altitude = src_shape.altitude
        elif (src_shape.altitude_high is not None
              and src_shape.altitude_low is not None):
            new_shape.altitude_high = src_shape.altitude_high
            new_shape.altitude_low = src_shape.altitude_low
        layout.shapes.append(new_shape)


def _validate_shared_vertex_invariant(layout: "PavementLayout",
                                      tol: float = 1.5) -> None:
    """Assert that every pair of shape vertices is EITHER exactly
    equal (< 0.01 m after clustering) OR > ``tol`` apart.  A
    "close but not equal" pair violates rule 16 and signals a
    clustering bug.  Raises RuntimeError on violation.
    """
    verts: list[tuple[int, tuple[float, float]]] = []
    for si, shape in enumerate(layout.shapes):
        poly = shape.polygon
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            continue
        ext = list(poly.exterior.coords)
        if ext and ext[0] == ext[-1]:
            ext = ext[:-1]
        for v in ext:
            verts.append((si, (v[0], v[1])))
    if len(verts) < 2:
        return
    # Grid-bucket check: every pair within tol must be within 0.01.
    from collections import defaultdict
    cell = tol * 2.0
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, (_, (x, y)) in enumerate(verts):
        buckets[(int(x // cell), int(y // cell))].append(i)
    for (gx, gy), idxs in buckets.items():
        neigh: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neigh.extend(buckets.get((gx + dx, gy + dy), []))
        for a in idxs:
            ax, ay = verts[a][1]
            for b in neigh:
                if b <= a:
                    continue
                bx, by = verts[b][1]
                d = math.hypot(ax - bx, ay - by)
                if 0.01 < d <= tol:
                    raise RuntimeError(
                        f"Shared-vertex invariant violated: shape {verts[a][0]}"
                        f" @ ({ax:.3f},{ay:.3f}) and shape {verts[b][0]}"
                        f" @ ({bx:.3f},{by:.3f}) are {d:.3f} m apart"
                        f" (within tol={tol} m but not exactly equal).")


# ──────────────────────────────────────────────────────────────────
# Centerline-based taxi rect builder
# ──────────────────────────────────────────────────────────────────

                                # — between 80 (too coarse, merged
                                # distinct crossings) and 25 (too
                                # fine, created spurious crossings at
                                # every sub-way endpoint)
JUNCTION_RADIUS_SCALE = 1.5     # disc radius = local_half_width × this


# ── Scoop tight under-sampled turns (user 2026-06-30, gate O4_ROUND_TURNBACK) ─
# At a few spots a SHORT boundary edge meets a STEEP TURN coming off a LONG edge
# (the boundary departs sharply with no intermediate nodes to model the turn —
# SPJC apron ring[29]→[30] / [2]→[3]).  Replace that short edge with a concave
# ``scoop`` — a ~5-node half-circle bulging toward the shape INTERIOR — so the
# boundary eases the turn on an arc (longer path ⇒ the grade spreads) instead of
# a single abrupt chord.  Concave (inward), not a convex bump.
_RTB_MAX_TURN_INTERIOR_DEG = 100.0   # interior angle below this = a steep turn
_RTB_LONG_EDGE_M = 18.0              # the approach edge into the turn
_RTB_SHORT_EDGE_M = 10.0             # the under-sampled edge to scoop
# The far end of the short edge must ease GENTLY but not run nearly straight —
# this separates a freeform apron turn-back (far angle obtuse, 100–165°) from a
# deliberate rectangular taxi-corner (≈180° straight-continuation or ≈90° next
# square corner), which must be left untouched.
_RTB_FAR_MIN_DEG = 100.0
_RTB_FAR_MAX_DEG = 165.0
_RTB_NODES = 5
# Scoop depth as a fraction of the chord (sagitta).  Shallow by default: a deep
# notch makes the solver pull the new interior nodes to terrain and the grade
# gets WORSE.  Env-tunable for in-sim experimentation.
_RTB_SAGITTA_FRAC = float(os.environ.get("O4_SCOOP_SAGITTA_FRAC", "0.35"))
# All airside pavement.  Junctions and rect-spanning edges are fair game; the
# gentle-far-side band (100–165°) excludes the ~90° square corners that make a
# rect SIDE, so an edge between two corners of the SAME rect is never scooped —
# only an edge that turns steeply off a long run while the far end eases.
_RTB_ROLES = frozenset({
    ROLE_JUNCTION, ROLE_APRON, ROLE_STUB, ROLE_CROSS_CONNECTOR,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL})


def _rtb_unit(dx, dy):
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-9 else (0.0, 0.0)


def _rtb_angle(a, v, b):
    u1 = _rtb_unit(a[0] - v[0], a[1] - v[1])
    u2 = _rtb_unit(b[0] - v[0], b[1] - v[1])
    return math.degrees(math.acos(max(-1.0, min(1.0, u1[0]*u2[0] + u1[1]*u2[1]))))


def _rtb_semicircle(P, Q, bulge, n=_RTB_NODES, sag_frac=None):
    """``n`` points on a circular arc through P and Q whose mid-point bulges
    toward ``bulge`` by ``sag_frac`` × |PQ| (sagitta).  sag_frac=0.5 is a true
    half-circle; a shallow scoop uses a small fraction.  out[0]==P, out[-1]==Q."""
    if sag_frac is None:
        sag_frac = _RTB_SAGITTA_FRAC
    chord = math.hypot(Q[0] - P[0], Q[1] - P[1])
    if chord < 0.2:
        return [P, Q]
    mx, my = (P[0] + Q[0]) / 2, (P[1] + Q[1]) / 2
    c = chord / 2.0
    h = sag_frac * chord                      # sagitta
    wx, wy = _rtb_unit(*( -(Q[1]-P[1]), Q[0]-P[0] ))   # unit ⟂ to chord
    if wx * bulge[0] + wy * bulge[1] < 0:
        wx, wy = -wx, -wy
    R = (c*c + h*h) / (2*h)                    # arc radius
    # circle centre is on the side OPPOSITE the bulge, at distance R-h from mid
    ox, oy = mx - (R - h) * wx, my - (R - h) * wy
    a0 = math.atan2(P[1]-oy, P[0]-ox)
    a1 = math.atan2(Q[1]-oy, Q[0]-ox)
    # take the short way that passes through the bulged mid-point
    while a1 - a0 > math.pi:  a1 -= 2*math.pi
    while a1 - a0 < -math.pi: a1 += 2*math.pi
    out = []
    for k in range(n):
        th = a0 + (a1 - a0) * k / (n - 1)
        out.append((ox + R * math.cos(th), oy + R * math.sin(th)))
    out[0], out[-1] = P, Q
    return out


def _round_turnback_corners(layout: "PavementLayout", icao: str = "") -> int:
    """Scoop a short under-sampled edge that meets a steep turn off a long edge
    into a concave ``_RTB_NODES``-node half-circle (bulging toward the shape
    interior).  Returns the count scooped; clears node_altitudes so the solver
    re-grades the arc."""
    if os.environ.get("O4_ROUND_TURNBACK", "0") != "1":
        return 0
    n_done = 0
    for s in layout.shapes:
        if (s.role not in _RTB_ROLES or s.polygon is None or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"):
            continue
        try:
            ring = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        n = len(ring)
        if n < 4:
            continue
        cen = s.polygon.centroid
        # Which edges to scoop: a SHORT edge whose ONE endpoint is a steep turn
        # arriving off a LONG edge (the under-sampled turn).
        scoop = set()
        for i in range(n):
            P = ring[i]; Q = ring[(i + 1) % n]
            Lpq = math.hypot(Q[0]-P[0], Q[1]-P[1])
            if not (1.0 < Lpq < _RTB_SHORT_EDGE_M):
                continue
            Pprev = ring[(i - 1) % n]; Qnext = ring[(i + 2) % n]
            # steep turn at one end off a LONG approach edge, while the OTHER
            # end eases gently (the under-sampled freeform turn-back).
            angP = _rtb_angle(Pprev, P, Q)
            longP = math.hypot(P[0]-Pprev[0], P[1]-Pprev[1]) > _RTB_LONG_EDGE_M
            angQ = _rtb_angle(P, Q, Qnext)
            longQ = math.hypot(Q[0]-Qnext[0], Q[1]-Qnext[1]) > _RTB_LONG_EDGE_M
            steepP = (angP < _RTB_MAX_TURN_INTERIOR_DEG and longP
                      and _RTB_FAR_MIN_DEG < angQ < _RTB_FAR_MAX_DEG)
            steepQ = (angQ < _RTB_MAX_TURN_INTERIOR_DEG and longQ
                      and _RTB_FAR_MIN_DEG < angP < _RTB_FAR_MAX_DEG)
            if steepP or steepQ:
                scoop.add(i)
        if not scoop:
            continue
        new_ring: list = []
        for i in range(n):
            new_ring.append(ring[i])
            if i in scoop:
                P = ring[i]; Q = ring[(i + 1) % n]
                mid = ((P[0]+Q[0])/2, (P[1]+Q[1])/2)
                # Bulge INWARD = remove material (scoop out the convex point),
                # not toward the centroid (wrong side on a concave corner).
                # Pick the chord-normal whose tiny offset lands INSIDE the poly.
                nx, ny = _rtb_unit(-(Q[1]-P[1]), Q[0]-P[0])
                if not s.polygon.contains(Point(mid[0]+0.25*nx, mid[1]+0.25*ny)):
                    nx, ny = -nx, -ny
                new_ring.extend(_rtb_semicircle(P, Q, (nx, ny))[1:-1])
        try:
            poly = Polygon(new_ring + [new_ring[0]])
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.geom_type == "Polygon" and not poly.is_empty:
                s.polygon = poly
                s.node_altitudes = None
                n_done += len(scoop)
        except _GEOM_EXC:
            continue
    if n_done:
        import O4_UI_Utils as UI
        UI.vprint(1, f"  [pav-builder] {icao}: scooped {n_done} under-sampled "
                  f"turn(s) into concave half-circle arcs.")
    return n_done
