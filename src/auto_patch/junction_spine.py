"""Self-partitioned junctions/aprons with a centerline "spine" (slice).

Problem (docs/junction_centerline_spine.md): junctions/aprons emit as a
single ring polygon.  X-Plane / Triangle4XP triangulate the interior by
interpolating between boundary vertices, so a taxi centerline crossing
the INTERIOR — where there are no vertices on it — renders a waving
surface instead of the corridor's clean ≤1.5% profile.

Fix (user's SLICE model): SLICE each junction/apron along every crossing
taxi centerline so the centerline becomes a real shared EDGE with nodes
on it.  This pass is PURE GEOMETRY and runs PRE-SOLVE (right after the
hole cuts, before ``_unify_airside_geometry`` and the per-surface
solver): it only cuts the polygons; the unify pass then welds the new
shared nodes and the elevation solver grades the whole sliced surface
coherently (centerline + boundary together), so the corridor profile is
the solver's job, not this pass's.

  1. Densify each crossing taxi centerline through the shape — interior
     "spine" nodes, clipped to ``pav_union − runway`` (never in a runway
     or off source pavement; user 2026-06-17).
  2. Attach each centerline END to the boundary:
       * SOFT end (node_altitudes apron / junction / free): the slice
         runs to the crossing point P right ON the centerline.
       * HARD end (sloping rect / runway): a CAP — a rectangle bridging
         the rect's two corners (C1,C2) to a 3-node inboard side (E1, E2
         at the pavement edges + M on the centerline), placed
         ≥ ``_CAP_DEPTH_M`` perpendicular from the flat edge so no
         junction/apron vertex lands in the rect's exclusion band
         (verification.check_vertex_on_flat_edge).  The centerline
         attaches at M, dead-centre — it never skews to a corner.
  3. Polygonize the shape with the capped centerline polylines as cut
     lines (honouring hole rings), keeping faces inside the polygon.
     Emit each piece as a geometry-only BuiltShape (NO altitudes — the
     solver assigns them).

Public API:
    apply_junction_centerline_spine(layout) -> int
        Slice every ROLE_JUNCTION / ROLE_APRON shape along its crossing
        centerlines.  Returns the count sliced.  No-op (0) when the gate
        is off.
"""
from __future__ import annotations

import math
from typing import List, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

import O4_UI_Utils as UI

from .config import (
    JUNCTION_CENTERLINE_SPINE, JUNCTION_SPINE_INTERIOR_STITCH,
    RECT_END_CAP_DEPTH_M, RECT_END_CAP_MIN_RECT_LEN_M, SPINE_PIECE_ROLE_REEVAL,
    SPINE_STEP_M)
from .layout import (
    BuiltShape, ROLE_APRON, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB)
from .rect_end_caps import rect_axis_length

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["apply_junction_centerline_spine"]

# Sloping 4-corner taxi rects: a junction/apron vertex may share only their
# CORNERS, never a mid-edge node (verification.check_vertex_on_flat_edge /
# check_vertex_on_sloping_edge step the rect's linear-corner plane), so a
# slice end meeting one gets a rectangle cap rather than a node on the edge.
# Service ROADS are 4-corner sloping rects too.
#
# RUNWAYS are deliberately NOT hard ends (user 2026-06-17): a runway segment
# may carry a node on its edge where a centerline crosses.  By spine time the
# seam/redistribute pipeline has already set runway altitudes, and the
# pre-solve ``enforce_conformance`` inserts the junction's crossing vertex
# into the runway edge at the linearly-interpolated (FAA-profile) altitude,
# converting the runway to ``node_altitudes`` — which both runway-edge checks
# exempt.  So a runway end is SOFT (the slice lands right on the crossing
# point); no rectangle cap, no extra shape around the runway edge.
_HARD_END_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR, "service_road"})
# Cappable taxi-rect roles (== rect_end_caps._CAP_ROLES, service_road excluded).
# A rect of one of these roles SHORTER than the rect-end-cap length threshold
# gets NO cap (rect_end_caps withholds it) and must therefore read as a SOFT
# spine end so the centerline node welds onto its edge and enforce_conformance
# converts it to node_altitudes — instead of the spine building its own
# HARD-end cap (the duplicate "cap generator" that produced the R1/R2 strips).
# Service roads and non-quad rects are NEVER capped, so they stay HARD.
_CAPPABLE_TAXI_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR})
# Min rect length to host a cap (mirrors rect_end_caps._carve_one's gate, so
# the two passes agree on "short"); a cappable rect below this is SOFT.
_CAP_MIN_LEN_M = max(RECT_END_CAP_MIN_RECT_LEN_M, 2.0 * RECT_END_CAP_DEPTH_M + 2.0)
# A boundary crossing within this distance of a hard boundary is a HARD
# end (rectangle cap, no node on the flat edge).
_HARD_EDGE_TOL_M = 1.5
# A HARD cap's inboard nodes sit ≥ this far PERPENDICULAR from the flat
# edge — strictly beyond verification.check_vertex_on_flat_edge's
# EDGE_PROX_M (1.5 m), so no junction/apron vertex lands in the band.
_CAP_DEPTH_M = 2.0
# Painted-curve filter (user 2026-06-17): a row-120 painted centerline is
# kept only where it TRACKS a taxi route — within this distance of, AND
# roughly parallel to, a 1201/1202 route segment.  Off-route lobes and
# perpendicular crossers (painted edge lines, hold bars, one taxiway's paint
# crossing a DIFFERENT taxiway's route) are dropped so the spine slices each
# corridor along ONE line, not several near-parallel / crossing ones.  The
# straight route segments won't follow a painted CURVE point-for-point, so
# the match is by proximity-and-parallelism per short interval, not overlap.
_PAINTED_ROUTE_NEAR_M = 5.0
# Keep a painted interval when |cos(angle to a near route segment)| ≥ this:
# cos 60° = 0.5 admits taxi turns up to ~120° while rejecting near-
# perpendicular crossings.
_PAINTED_ROUTE_PARALLEL_DOT = 0.5
# Painted walk step + minimum kept-run length (m).
_PAINTED_WALK_STEP_M = 4.0
_PAINTED_MIN_RUN_M = 6.0
# Coordinate rounding (m) for polygonize snap.
_RND = 3
# Drop emitted pieces smaller than this (slivers).
_MIN_PIECE_AREA = 0.25


def _open(ring):
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _coords(items):
    out: List[LineString] = []
    for item in (items or []):
        ln = item.line if hasattr(item, "line") else (item[0] if isinstance(item, tuple) else item)
        if ln is not None and not ln.is_empty:
            out.append(ln)
    return out


def _filter_painted_to_routes(painted, apt_cl):
    """Keep painted-curve geometry only where it runs within
    ``_PAINTED_ROUTE_NEAR_M`` of, AND roughly parallel to, a taxi-route
    (1201/1202) segment; drop the off-route lobes and perpendicular
    crossers.  Walks each painted line in short intervals: an interval is
    kept when SOME route segment is both near (≤ NEAR_M) and parallel
    (|cos| ≥ PARALLEL_DOT) to it — so at a crossing the painted line keeps
    the part parallel to its OWN route and drops a line perpendicular to
    every nearby route.  Consecutive kept intervals are stitched back into
    a run; runs shorter than ``_PAINTED_MIN_RUN_M`` are dropped.  With no
    routes to validate against, returns the painted lines unchanged (a
    painted-only airport — nothing to de-dup against)."""
    seg_lines: List[LineString] = []
    seg_dir: List[Tuple[float, float]] = []
    for ln in apt_cl:
        if ln is None or ln.is_empty:
            continue
        cs = list(ln.coords)
        for i in range(len(cs) - 1):
            ax, ay = cs[i]
            bx, by = cs[i + 1]
            dx, dy = bx - ax, by - ay
            L = math.hypot(dx, dy)
            if L < 1e-6:
                continue
            seg_lines.append(LineString([(ax, ay), (bx, by)]))
            seg_dir.append((dx / L, dy / L))
    if not seg_lines:
        return list(painted)
    tree = STRtree(seg_lines)
    out: List[LineString] = []
    for ln in painted:
        if ln is None or ln.is_empty or ln.geom_type != "LineString":
            continue
        L = ln.length
        if L < 1e-6:
            continue
        n = max(1, int(round(L / _PAINTED_WALK_STEP_M)))
        pts = [ln.interpolate(k * L / n) for k in range(n + 1)]
        run: List[Tuple[float, float]] = []
        for k in range(n):
            p0, p1 = pts[k], pts[k + 1]
            sdx, sdy = p1.x - p0.x, p1.y - p0.y
            sL = math.hypot(sdx, sdy)
            keep = False
            if sL >= 1e-6:
                sdx, sdy = sdx / sL, sdy / sL
                mp = Point(0.5 * (p0.x + p1.x), 0.5 * (p0.y + p1.y))
                for j in tree.query(mp.buffer(_PAINTED_ROUTE_NEAR_M)):
                    try:
                        if seg_lines[j].distance(mp) > _PAINTED_ROUTE_NEAR_M:
                            continue
                    except _GEOM_EXC:
                        continue
                    rdx, rdy = seg_dir[j]
                    if (abs(sdx * rdx + sdy * rdy)
                            >= _PAINTED_ROUTE_PARALLEL_DOT):
                        keep = True
                        break
            if keep:
                if not run:
                    run.append((p0.x, p0.y))
                run.append((p1.x, p1.y))
            elif run:
                if LineString(run).length >= _PAINTED_MIN_RUN_M:
                    out.append(LineString(run))
                run = []
        if run and LineString(run).length >= _PAINTED_MIN_RUN_M:
            out.append(LineString(run))
    return out


def _is_service_ref(name) -> bool:
    """A 1206 ground-vehicle (truck) route — merged into
    ``apt_taxi_centerlines`` for the road carve with a ``SVC*`` ref."""
    return isinstance(name, str) and name.upper().startswith("SVC")


def _full_centerlines(layout):
    """The spine's taxi centerlines: the apt.dat 1201/1202 ROUTE network
    ONLY (user 2026-06-17 experiment).  Painted (row-120) curves and
    discovered ("TX") lanes are EXCLUDED.  Ground-vehicle 1206 service
    routes (ref ``SVC*``, merged into ``apt_taxi_centerlines`` for the
    road carve) are filtered OUT — they are truck paths, not aircraft taxi
    corridors.  Runway long-axes are excluded upstream; a spine node must
    never land in a runway."""
    out: List[LineString] = []
    for item in (getattr(layout, "apt_taxi_centerlines", None) or []):
        ln = item.line if hasattr(item, "line") else (item[0] if isinstance(item, tuple) else item)
        name = item.name if hasattr(item, "name") else (item[1] if (isinstance(item, tuple) and len(item) > 1) else "")
        is_svc = item.is_service if hasattr(item, "is_service") else _is_service_ref(name)
        if ln is None or ln.is_empty:
            continue
        if is_svc:
            continue
        out.append(ln)
    return out


def _perp_dist(qx, qy, c1, c2):
    """Perpendicular distance of ``(qx,qy)`` from the line through
    ``c1``–``c2`` (the rect's flat edge)."""
    ex, ey = c2[0] - c1[0], c2[1] - c1[1]
    eL = math.hypot(ex, ey)
    if eL < 1e-9:
        return math.hypot(qx - c1[0], qy - c1[1])
    return abs((qx - c1[0]) * ey - (qy - c1[1]) * ex) / eL


def _make_cap(mx, my, c1, c2, poly):
    """Rectangle CAP cut-lines bridging the rect corners (C1,C2) to a
    3-node inboard side — E1, E2 at the pavement edges and M
    (``mx,my``, on the centerline) in the middle.  Returns ``cut_lines``
    or ``None`` when infeasible.  M is assumed already ≥ ``_CAP_DEPTH_M``
    perpendicular from the edge, so E1, E2 clear it too."""
    ex, ey = c2[0] - c1[0], c2[1] - c1[1]
    eL = math.hypot(ex, ey)
    if eL < 1e-6:
        return None
    ux, uy = ex / eL, ey / eL
    half_w = 0.5 * eL
    e1 = (mx - half_w * ux, my - half_w * uy)
    e2 = (mx + half_w * ux, my + half_w * uy)
    if (math.hypot(e1[0] - c1[0], e1[1] - c1[1])
            > math.hypot(e1[0] - c2[0], e1[1] - c2[1])):
        e1, e2 = e2, e1
    try:
        if not (poly.contains(Point(*e1)) and poly.contains(Point(*e2))):
            return None
    except _GEOM_EXC:
        return None
    return [LineString([c1, e1]), LineString([c2, e2]),
            LineString([e1, (mx, my)]), LineString([(mx, my), e2])]


_STITCH_INTERIOR_TOL_M = 0.5


def _stitch_interior_joints(pieces, poly, tol=_STITCH_INTERIOR_TOL_M):
    """Concatenate clipped centerline ``pieces`` that share an endpoint lying
    strictly INSIDE ``poly`` (a route bend within the junction), so a route
    that bends inside the shape cuts boundary-to-boundary as one line instead
    of dead-ending piece-by-piece.  Pieces that meet ON the boundary (within
    ``tol``) are left separate — each is a real crossing whose endpoint is a
    shared ring/neighbour corner, so straight crossings stay byte-identical
    and no boundary corner is swallowed into a cut interior.  Only degree-2
    interior joints are chained (a fork inside a junction stays unmerged)."""
    ext = poly.exterior

    def _key(pt):
        return (round(pt[0], 3), round(pt[1], 3))

    def _interior(pt):
        try:
            return ext.distance(Point(pt[0], pt[1])) > tol
        except _GEOM_EXC:
            return False

    chains = [list(p.coords) for p in pieces if len(p.coords) >= 2]
    # Count how many chain-ends land on each interior endpoint; only a joint
    # where EXACTLY two ends meet (degree 2) is an unambiguous through-bend.
    changed = True
    while changed:
        changed = False
        for i in range(len(chains)):
            a = chains[i]
            if a is None:
                continue
            for ai in (-1, 0):
                aend = a[ai]
                if not _interior(aend):
                    continue
                # degree of this joint across all live chain-ends
                deg = 0
                for c in chains:
                    if c is None:
                        continue
                    if _key(c[0]) == _key(aend):
                        deg += 1
                    if _key(c[-1]) == _key(aend):
                        deg += 1
                if deg != 2:
                    continue
                # find the partner chain-end sharing aend
                jj = None
                for j in range(len(chains)):
                    if j == i or chains[j] is None:
                        continue
                    b = chains[j]
                    if _key(b[0]) == _key(aend):
                        jj, brev = j, False
                        break
                    if _key(b[-1]) == _key(aend):
                        jj, brev = j, True
                        break
                if jj is None:
                    continue
                b = chains[jj] if not brev else chains[jj][::-1]
                # b now starts at aend; splice, dropping the duplicate joint
                merged = (a + b[1:]) if ai == -1 else (b[::-1] + a[1:])
                chains[i] = merged
                chains[jj] = None
                changed = True
                break
            if changed:
                break
    out = []
    for c in chains:
        if c is None:
            continue
        try:
            out.append(LineString(c))
        except _GEOM_EXC:
            continue
    return out


def _partition_junction(s, centerlines, pav_union, runway_union, near_hard,
                        guide_sink=None):
    """Slice junction/apron ``s`` along its crossing centerlines and
    return the list of piece Polygons (geometry only), or None to leave
    the shape unchanged."""
    poly = s.polygon
    ropen = _open(list(poly.exterior.coords))
    if len(ropen) < 3:
        return None, "degenerate" 

    # Spine nodes live only where the shape overlaps SOURCE pavement
    # (apt.dat + DSF) MINUS runways.
    pav_clip = poly
    if pav_union is not None and not pav_union.is_empty:
        try:
            pav_clip = poly.intersection(pav_union)
        except _GEOM_EXC:
            pav_clip = poly
    if runway_union is not None and not runway_union.is_empty:
        try:
            pav_clip = pav_clip.difference(runway_union)
        except _GEOM_EXC:
            pass
    if pav_clip.is_empty:
        return None, "off_pavement" 

    _ring_lists = [ropen]
    for _hole in poly.interiors:
        _hl = _open(list(_hole.coords))
        if len(_hl) >= 3:
            _ring_lists.append(_hl)

    def _edge_corners(px, py):
        best = None
        for rl in _ring_lists:
            m = len(rl)
            for i in range(m):
                ax, ay = rl[i]
                bx, by = rl[(i + 1) % m]
                dx, dy = bx - ax, by - ay
                s2 = dx * dx + dy * dy
                if s2 < 1e-12:
                    continue
                t = ((px - ax) * dx + (py - ay) * dy) / s2
                t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
                cx, cy = ax + t * dx, ay + t * dy
                d = (px - cx) ** 2 + (py - cy) ** 2
                if best is None or d < best[0]:
                    best = (d, (ax, ay), (bx, by))
        if best is None:
            return None, None
        return best[1], best[2]

    def _on_pav(px, py):
        try:
            return pav_clip.distance(Point(px, py)) <= 0.1
        except _GEOM_EXC:
            return True

    # Clip every crossing centerline to the junction, then STITCH the
    # clipped pieces that meet at a bend INSIDE the junction into continuous
    # crossings (user 2026-07-01).  The apt.dat route is bend-split into
    # TaxiCenterline pieces, so a route that bends within the junction arrives
    # as several segments sharing a bend node in the interior.  Sliced piece-
    # by-piece, the segment on either side of the bend dead-ends inside the
    # polygon (its cut spans no boundary) and a short runway-reaching stub is
    # dropped by the min-cut length gate below — so polygonize never splits
    # the shape and no spine forms (HECA T5→05C: a 77 m piece dead-ends 1.5 m
    # short of the runway edge, its 4.8 m continuation to the edge is < the
    # 6 m gate).  ``_stitch_interior_joints`` joins ONLY at shared endpoints
    # strictly inside the polygon, so a stitched cut stays wholly within this
    # junction and pieces meeting ON the boundary (real crossings / shared
    # neighbour corners) are left untouched — byte-identical for straight
    # crossings, no A/B-boundary corner is absorbed into a cut interior.
    clipped: List[LineString] = []
    for ln in centerlines:
        try:
            seg = poly.intersection(ln)
        except _GEOM_EXC:
            continue
        if seg.is_empty:
            continue
        if seg.geom_type == "LineString":
            clipped.append(seg)
        elif seg.geom_type == "MultiLineString":
            clipped.extend(seg.geoms)
        elif seg.geom_type == "GeometryCollection":
            clipped.extend(g for g in seg.geoms if g.geom_type == "LineString")
    from shapely import union_all

    def _slice(parts, guide_out):
        """Build spine cuts from ``parts`` and polygonize.  Returns
        ``(faces, reason)`` — faces is None when the cut does not split the
        shape.  Interior guide nodes are appended to ``guide_out`` so a
        rejected attempt contributes no guides."""
        cut_lines: List[LineString] = []
        for part in parts:
            L = part.length
            if L < max(2.0, 0.5 * SPINE_STEP_M):
                continue
            # Spread interior nodes EVENLY along the cut, endpoint to endpoint
            # (user 2026-06-19): the old code also placed a fixed node 1.5 m
            # inboard of each boundary crossing, sitting right beside the
            # crossing node itself — a built-in ~1.5 m cluster that
            # triangulated to slivers (X-Plane tearing).  Dividing [0, L] into
            # ``nseg`` equal parts keeps every node ~SPINE_STEP_M apart from
            # its neighbours AND from the endpoints (boundary crossings/cap M).
            nseg = max(2, int(round(L / SPINE_STEP_M)))
            ds = set()
            for i in range(1, nseg):
                ds.add(i * L / nseg)
            cand: List[Tuple[float, float]] = []
            for d in sorted(x for x in ds if 0.0 < x < L):
                p = part.interpolate(d)
                if _on_pav(p.x, p.y):
                    cand.append((p.x, p.y))
            if not cand:
                continue
            lo, hi = 0, len(cand) - 1
            extra: List[LineString] = []
            end_soft = {True: False, False: False}
            for is_entry in (True, False):
                px, py = (part.coords[0] if is_entry else part.coords[-1])
                c1, c2 = _edge_corners(px, py)
                if not (near_hard(px, py) and c1 is not None
                        and c2 is not None):
                    end_soft[is_entry] = True
                    continue
                # HARD end: M must clear the rect flat edge by > EDGE_PROX.
                if is_entry:
                    while (lo <= hi and _perp_dist(cand[lo][0], cand[lo][1],
                                                   c1, c2) < _CAP_DEPTH_M):
                        lo += 1
                    mi = lo
                else:
                    while (hi >= lo and _perp_dist(cand[hi][0], cand[hi][1],
                                                   c1, c2) < _CAP_DEPTH_M):
                        hi -= 1
                    mi = hi
                if lo > hi:
                    break
                cuts = _make_cap(cand[mi][0], cand[mi][1], c1, c2, poly)
                if cuts is None:
                    end_soft[is_entry] = True
                else:
                    extra.extend(cuts)
            if lo > hi:
                continue
            slice_pts: List[Tuple[float, float]] = list(cand[lo:hi + 1])
            # The interior centerline points are GUIDE nodes (user
            # 2026-06-17): they mark where the corridor runs through the
            # shape so the solver grades a smooth profile, but they carry
            # NO runway-reach obligation of their own — the solver must
            # leave them flexible (never band-pin them) so the network can
            # pull slack to keep buildings flat.  Boundary-crossing
            # endpoints are NOT guides (they are shared with the neighbour
            # and keep their normal band).
            guide_out.extend(slice_pts)
            for is_entry in (True, False):
                if not end_soft[is_entry]:
                    continue
                px, py = (part.coords[0] if is_entry else part.coords[-1])
                if is_entry:
                    slice_pts = [(px, py)] + slice_pts
                else:
                    slice_pts = slice_pts + [(px, py)]
            if len(slice_pts) >= 2:
                try:
                    cut_lines.append(LineString(slice_pts))
                except _GEOM_EXC:
                    pass
            cut_lines.extend(extra)
        if not cut_lines:
            return None, "no_cut"

        # Polygonize against the FULL boundary (exterior + hole rings).  Union
        # with a GRID_SIZE so the cut endpoints — which land a few µm off the
        # boundary edge in the raw pre-solve geometry — snap onto it and NODE;
        # without grid-snapped noding polygonize leaves clean boundary-to-
        # boundary cuts unsplit.
        try:
            arrangement = union_all([poly.boundary] + cut_lines, grid_size=0.01)
            raw = [f for f in polygonize(arrangement)
                   if not f.is_empty and f.geom_type == "Polygon"]
            faces = []
            for f in raw:
                if f.area < _MIN_PIECE_AREA:
                    continue
                try:
                    if not poly.contains(f.representative_point()):
                        continue
                except _GEOM_EXC:
                    continue
                faces.append(f)
        except _GEOM_EXC:
            return None, "polygonize_err"
        if len(faces) > 1:
            return faces, "ok"
        return None, f"single_face(raw={len(raw)},cuts={len(cut_lines)})"

    # Try the plain per-piece slice FIRST — byte-identical to the pre-stitch
    # code for every junction that already splits.  Only when it fails to
    # split (a route bends inside the junction, so each piece dead-ends and no
    # cut spans boundary-to-boundary — HECA T5→05C) do we retry with the
    # interior-stitched cut.  Adopting the stitch solely on failure means a
    # junction that already slices keeps its exact rings/corners (SPJC
    # neighbour-corner invariant unchanged); only shapes that got NO spine
    # before can change.
    plain_guides: List[Tuple[float, float]] = []
    faces, reason = _slice(clipped, plain_guides)
    if faces is not None:
        if guide_sink is not None:
            guide_sink.extend(plain_guides)
        return faces, reason
    if JUNCTION_SPINE_INTERIOR_STITCH and len(clipped) > 1:
        stitched = _stitch_interior_joints(clipped, poly)
        if len(stitched) < sum(1 for p in clipped if len(p.coords) >= 2):
            st_guides: List[Tuple[float, float]] = []
            faces2, _ = _slice(stitched, st_guides)
            if faces2 is not None:
                if guide_sink is not None:
                    guide_sink.extend(st_guides)
                return faces2, "ok_stitched"
    return None, reason


def _reeval_apron_piece_role(poly, cen, cap_m, step_m=2.0):
    """Re-derive apron vs junction for a single spine PIECE carved from an
    APRON parent, using the SAME geometry rule + cap as
    ``junction_repair._reclassify_apron_junctions``: a piece whose whole
    boundary stays within ``cap_m`` of a taxi/runway centerline is a
    corridor (→ ROLE_JUNCTION, taxi-rate grade); one that strays beyond is
    apron-territory (→ ROLE_APRON, 1 %).

    Applied per piece AFTER the slice, this promotes the narrow corridor
    pieces sliced out of a wide apron blob — where a taxiway runs through
    it (CYXY taxiway G) — back to junction so they can climb at taxi rate,
    while the wide flanks left when a taxiway crosses a real apron (taxiway
    E through the main apron) stay apron.  PROMOTION-ONLY: slicing only
    removes area, so a piece's max boundary-to-centerline distance is
    always <= its parent's; this is only ever called on apron parents.
    """
    if cen is None or cen.is_empty:
        return ROLE_APRON
    try:
        bnd = poly.boundary
        L = bnd.length
    except _GEOM_EXC:
        return ROLE_APRON
    if L <= 0:
        return ROLE_APRON
    n_steps = max(2, int(L / step_m) + 1)
    for i in range(n_steps):
        u = min(L, i * step_m)
        try:
            if cen.distance(bnd.interpolate(u)) > cap_m:
                return ROLE_APRON
        except _GEOM_EXC:
            continue
    return ROLE_JUNCTION


def apply_junction_centerline_spine(layout) -> int:
    """Slice every ROLE_JUNCTION / ROLE_APRON shape along its crossing
    taxi centerlines (geometry only — the solver grades the pieces).
    Mutates ``layout.shapes``.  Returns the count of shapes sliced."""
    if not JUNCTION_CENTERLINE_SPINE:
        return 0
    centerlines = _full_centerlines(layout)
    if not centerlines:
        return 0
    import os as _os
    _DEBUG = _os.environ.get("O4_JCT_SPINE_DEBUG") == "1"
    _skips = []
    if _DEBUG:
        UI.vprint(1, "  [pav-builder] junction-spine source: %d centerline(s)"
                  " (painted=%d, apt=%d, disc=%d)" % (
                      len(centerlines),
                      len(getattr(layout, "_painted_centerlines", None) or []),
                      len(getattr(layout, "apt_taxi_centerlines", None) or []),
                      len(getattr(layout, "_discovered_centerlines", None)
                          or [])))
    pav_union = getattr(layout, "_source_pav_union", None)
    runway_union = getattr(layout, "runway_union", None)

    # Spine-piece role re-evaluation (apron-spine grade model): the full
    # taxi/runway centerline union + the 55 m cap that
    # ``_reclassify_apron_junctions`` uses, so a narrow corridor sliced out
    # of a wide apron blob is promoted back to ROLE_JUNCTION.  Lazy import
    # (junction_repair <-> elevation cycle is already resolved by solve
    # time).  Computed ONCE — slicing never moves a centerline.
    _reeval_cen = None
    _reeval_cap = 0.0
    if SPINE_PIECE_ROLE_REEVAL:
        from .junction_repair import (
            _aeroway_centerlines_union, _APRON_RECLASSIFY_MAX_DISTANCE_M)
        _reeval_cen = _aeroway_centerlines_union(layout)
        _reeval_cap = _APRON_RECLASSIFY_MAX_DISTANCE_M
    _n_promoted = 0

    # Index sloping-rect / runway boundaries for the HARD-end test.
    hard_lines = []
    for s in layout.shapes:
        if (s.role in _HARD_END_ROLES and s.polygon is not None
                and not s.polygon.is_empty
                and s.polygon.geom_type == "Polygon"):
            # A cappable taxi rect too short to host a rect-end-cap reads SOFT:
            # leave its boundary OUT of the hard index so a centerline end lands
            # on its edge (→ node_altitudes) rather than triggering a spine cap.
            # rect_axis_length is None for non-quad rects → those stay HARD.
            if s.role in _CAPPABLE_TAXI_ROLES:
                rlen = rect_axis_length(s.polygon, s.source_axis)
                if rlen is not None and rlen < _CAP_MIN_LEN_M:
                    continue
            try:
                hard_lines.append(s.polygon.exterior)
            except _GEOM_EXC:
                continue
    hard_tree = STRtree(hard_lines) if hard_lines else None

    def _near_hard(px, py):
        if hard_tree is None:
            return False
        p = Point(px, py)
        for idx in hard_tree.query(p.buffer(_HARD_EDGE_TOL_M)):
            try:
                if hard_lines[idx].distance(p) <= _HARD_EDGE_TOL_M:
                    return True
            except _GEOM_EXC:
                continue
        return False

    new_shapes: List[BuiltShape] = []
    guide_pts: List[Tuple[float, float]] = []
    apron_pts: List[Tuple[float, float]] = []
    n_done = 0
    n_pieces = 0
    for s in layout.shapes:
        # Never slice a rect end-cap (user 2026-06-19): the cap IS the rect-end
        # transition (a planar extension of the rect), not a sliceable junction.
        # A thin 2 m cap survived by luck (no boundary-to-boundary through-path
        # = single_face), but a deeper cap (e.g. 12 m) has one and gets sliced,
        # stripping its is_rect_cap flag → it loses the planar-cap treatment and
        # the cap-adjacent junction grade returns.  Skip it.
        if s.role not in (ROLE_JUNCTION, ROLE_APRON) \
                or getattr(s, "is_rect_cap", False):
            new_shapes.append(s)
            continue
        poly = s.polygon
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            new_shapes.append(s)
            continue
        crossing = []
        for ln in centerlines:
            try:
                if poly.intersects(ln):
                    crossing.append(ln)
            except _GEOM_EXC:
                continue
        if not crossing:
            new_shapes.append(s)
            continue
        pieces, reason = _partition_junction(
            s, crossing, pav_union, runway_union, _near_hard,
            guide_sink=guide_pts)
        if not pieces:
            new_shapes.append(s)
            if _DEBUG:
                try:
                    c = s.polygon.representative_point()
                    la, lo = layout.m_to_ll(c.x, c.y)
                    _skips.append((reason, s.role, round(la, 5), round(lo, 5)))
                except Exception:
                    _skips.append((reason, s.role, 0, 0))
            continue
        for f in pieces:
            # Geometry only — no altitudes; the per-surface solver grades
            # these pieces (the unify pass first welds the new nodes).
            piece_role = s.role
            # Spine-piece role re-evaluation: a piece sliced from an APRON
            # parent whose whole boundary hugs a centerline is the taxiway
            # corridor running through that apron — promote it back to
            # ROLE_JUNCTION so it grades at taxi rate (CYXY taxiway G).
            # Only apron parents are re-tested (slicing can't grow a
            # junction parent past the cap), so junction parents stay
            # byte-identical.
            if (_reeval_cen is not None and s.role == ROLE_APRON):
                piece_role = _reeval_apron_piece_role(
                    f, _reeval_cen, _reeval_cap)
                if piece_role != s.role:
                    _n_promoted += 1
            new_shapes.append(BuiltShape(polygon=f, role=piece_role,
                                         ref=s.ref))
            # An APRON sliced by the spine becomes a follower of the taxi
            # network + its building (cascade APRON-tier): collect ALL its
            # piece vertices so the solver can drop the runway-reach band on
            # the APRON-OWNED ones.  Slicing injects the centerline into the
            # reach graph and falsely inverts the bands of deep-apron nodes
            # (115 m from any taxiway), freezing them at terrain and
            # defeating the building-flatten.  Junction pieces are NOT
            # collected — junctions ARE the taxi network and keep their
            # reach bands (the OMAA waving fix needs them).
            #
            # Gate by the PARENT role (s.role), not the piece role: a piece
            # promoted to junction above is still an apron-corridor and keeps
            # its apron sibling's flexible band treatment.  This isolates the
            # re-evaluation to PURELY the grade cap (apron 1% -> junction
            # taxi/3%) — the solver's reach-band logic stays byte-identical to
            # gate-off, so the only thing the promotion changes is which cap
            # the field is held to.
            if s.role == ROLE_APRON:
                apron_pts.extend(_open(list(f.exterior.coords)))
            n_pieces += 1
        n_done += 1

    layout.shapes = new_shapes
    # Stash the interior centerline GUIDE points + the sliced-apron piece
    # points so the solver can flag those nodes as flexible (no runway-reach
    # band / never band-pinned).  Coordinates are matched through the
    # canonical registry (≤0.5 m) at solve time, so the post-polygonize
    # grid-snap drift is absorbed.
    layout._spine_guide_points = guide_pts
    layout._spine_apron_points = apron_pts
    if n_done:
        _promo = (f"; promoted {_n_promoted} apron corridor piece(s) "
                  f"to junction" if _n_promoted else "")
        UI.vprint(1,
            f"  [pav-builder] {getattr(layout, 'icao', '')}: "
            f"junction-spine sliced {n_done} junction/apron(s) into "
            f"{n_pieces} piece(s) (pre-solve geometry){_promo}.")
    if _DEBUG and _skips:
        from collections import Counter
        UI.vprint(1, "  [pav-builder] junction-spine SKIPPED %d: %s" % (
            len(_skips), dict(Counter(r for r, *_ in _skips))))
    return n_done
