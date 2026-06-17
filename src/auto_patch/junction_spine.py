"""Self-partitioned junctions with a centerline "spine" (rib-quad model).

Problem (docs/junction_centerline_spine.md): junctions emit today as a
single ring polygon with boundary-only ``node_altitudes``.  X-Plane /
Triangle4XP triangulate the interior by interpolating between boundary
vertices, so a taxi centerline crossing the junction INTERIOR — where
there are no vertices on it — renders a waving surface instead of the
solver's clean ≤1.5% corridor profile (OMAA taxiway H @ junction -10225:
field is a flat 1.5% but the emitted surface spikes to 3.6%).

Fix (user's SLICE model): give the junction interior real nodes ON each
crossing centerline, then SLICE the junction along that centerline so the
spine becomes a shared edge that carries the smooth profile:

  1. Densify each crossing taxi centerline through the junction every
     ``SPINE_STEP_M`` — "spine" nodes placed strictly INBOARD (none on the
     boundary), each pinned to the network-profile field value
     (``_network_profile_field.sample``).  Nodes are clipped to
     ``pav_union − runway`` — none ever lands in a runway / off source
     pavement (user 2026-06-17).
  2. Cap each centerline END to the NEAREST EXISTING boundary vertex (the
     feeding rect's corner — the "mini junction polygon" at the entry).
     This is the ONLY place a cut touches the boundary, and it reuses an
     existing node, so the rect stays conformant (the crossing point is
     the interior of the rect's flat edge, where a new node is illegal —
     test_no_vertex_on_sloping_rect_flat_edge).
  3. Polygonize the junction with the capped centerline polylines as the
     ONLY cut lines.  The junction's boundary edges are kept exactly
     as-is — NO perpendicular ribs, NO new perimeter nodes — so the
     partition is a conforming tiling and every vertex is source-anchored
     (a boundary corner or a centerline node).
  4. Spine nodes carry the dominant smooth profile (field z); boundary /
     cap nodes keep the solver altitude.  Triangle4XP fills the lateral
     gradient between the centerline and the untouched boundary.

X-Plane renders the emitted polygons as-is, so a taxi centerline now
grades through the junction interior at the corridor profile.

Public API:
    apply_junction_centerline_spine(layout) -> int
        Replace each ROLE_JUNCTION shape with its spine partition.
        Returns the number of junctions re-partitioned.  No-op (returns
        0) when the gate is off or the field is missing.
"""
from __future__ import annotations

import math
from typing import Dict, List, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

import O4_UI_Utils as UI

from .config import JUNCTION_CENTERLINE_SPINE, SPINE_STEP_M
from .layout import (
    BuiltShape, ROLE_APRON, ROLE_CROSS_CONNECTOR, ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL, ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    ROLE_SECONDARY_PARALLEL, ROLE_STUB)

# Sloping 4-corner rects + runways: a junction/apron vertex may share
# only their CORNERS, never a mid-edge node — so a slice end meeting one
# of these gets a rectangle cap rather than a node on the edge.  Service
# ROADS are 4-corner sloping rects too (per
# verification.check_vertex_on_flat_edge), so they belong here.
_HARD_END_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR, ROLE_RUNWAY, ROLE_RUNWAY_CROSSING,
    "service_road"})

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["apply_junction_centerline_spine"]


# Lateral grade cap from the centerline out to a new arrangement vertex
# (a centerline×centerline crossing inside the junction) when assigning
# its altitude.
_LAT_GRADE = 0.015
# Spine nodes are densified ~this far inboard of each boundary crossing.
_END_INSET_M = 1.5
# A boundary crossing within this distance of a sloping-rect / runway
# boundary is a HARD end (gets a rectangle cap, no node on its flat edge).
_HARD_EDGE_TOL_M = 1.5
# A HARD-end cap's inboard nodes (E1, M, E2) sit at LEAST this far
# PERPENDICULAR from the rect/runway flat edge — strictly beyond
# verification.check_vertex_on_flat_edge's EDGE_PROX_M (1.5 m), so a
# junction/apron vertex never lands in that rect's exclusion band.
_CAP_DEPTH_M = 2.0


def _perp_dist(qx, qy, c1, c2):
    """Perpendicular distance of ``(qx,qy)`` from the line through
    ``c1``–``c2`` (the rect's flat edge)."""
    ex, ey = c2[0] - c1[0], c2[1] - c1[1]
    eL = math.hypot(ex, ey)
    if eL < 1e-9:
        return math.hypot(qx - c1[0], qy - c1[1])
    return abs((qx - c1[0]) * ey - (qy - c1[1]) * ex) / eL


def _make_cap(mx, my, c1, c2, poly):
    """Rectangle CAP cut-lines bridging the rect's two corners (C1,C2) to
    a 3-node inboard side — E1, E2 at the pavement edges and M
    (``mx,my``, on the centerline) in the middle.  Returns
    ``(cut_lines, [e1, e2])`` or ``(None, None)`` when infeasible.  M is
    assumed already ≥ ``_CAP_DEPTH_M`` perpendicular from the edge, so E1,
    E2 (at M's depth, offset along the edge) clear it too."""
    ex, ey = c2[0] - c1[0], c2[1] - c1[1]
    eL = math.hypot(ex, ey)
    if eL < 1e-6:
        return None, None
    ux, uy = ex / eL, ey / eL
    half_w = 0.5 * eL
    e1 = (mx - half_w * ux, my - half_w * uy)
    e2 = (mx + half_w * ux, my + half_w * uy)
    if (math.hypot(e1[0] - c1[0], e1[1] - c1[1])
            > math.hypot(e1[0] - c2[0], e1[1] - c2[1])):
        e1, e2 = e2, e1
    try:
        if not (poly.contains(Point(*e1)) and poly.contains(Point(*e2))):
            return None, None
    except _GEOM_EXC:
        return None, None
    cuts = [LineString([c1, e1]), LineString([c2, e2]),
            LineString([e1, (mx, my)]), LineString([(mx, my), e2])]
    return cuts, [e1, e2]
# Coordinate rounding (m) for the node→altitude map / polygonize snap.
_RND = 3
# Drop emitted pieces smaller than this (slivers).
_MIN_PIECE_AREA = 0.25


def _open(ring):
    pts = list(ring)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _key(x, y):
    return (round(x, _RND), round(y, _RND))


def _full_centerlines(layout):
    """Every TAXI centerline of the route graph as plain LineStrings:
    apt.dat taxi network + discovered/unreferenced lanes.

    Runway long-axes are deliberately EXCLUDED (user 2026-06-17: never
    add spine nodes or ribs inside a runway).  A runway long-axis would
    plant spine nodes straight down the runway — off the taxi pavement and
    off ``pav_union`` (which excludes runways) — and runway grade is owned
    by the FAA runway profile, not the junction spine."""
    out: List[LineString] = []
    for item in (getattr(layout, "apt_taxi_centerlines", None) or []):
        ln = item[0] if isinstance(item, tuple) else item
        if ln is not None and not ln.is_empty:
            out.append(ln)
    for item in (getattr(layout, "_discovered_centerlines", None) or []):
        ln = item[0] if isinstance(item, tuple) else item
        if ln is not None and not ln.is_empty:
            out.append(ln)
    return out




def _boundary_z_at(ring, ring_z, x, y, tol=0.30):
    """Solver altitude interpolated along the nearest original boundary
    edge to ``(x,y)`` (within ``tol`` m), else None."""
    n = len(ring)
    best = None
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        s2 = dx * dx + dy * dy
        if s2 < 1e-12:
            continue
        t = ((x - ax) * dx + (y - ay) * dy) / s2
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        cx, cy = ax + t * dx, ay + t * dy
        d = math.hypot(x - cx, y - cy)
        if best is None or d < best[0]:
            z = ring_z[i] + t * (ring_z[(i + 1) % n] - ring_z[i])
            best = (d, z)
    if best is not None and best[0] <= tol:
        return best[1]
    return None


def _partition_junction(s: BuiltShape, centerlines, field,
                        pav_union, runway_union, near_hard):
    """Return a list of (Polygon, node_altitudes) pieces for junction
    ``s`` under the SLICE model, or None to leave the ring unchanged.

    Slice the junction along each crossing taxi centerline: densify the
    centerline to interior "spine" nodes (field z), cap each end to the
    nearest EXISTING boundary vertex (the feeding rect's corner — never a
    new node on the rect's flat edge), and cut the polygon along that
    polyline.  The junction's boundary edges are kept exactly as-is (no
    side ribs, no new perimeter nodes), so the partition stays a
    conforming tiling and every vertex is source-anchored.  The spine
    nodes are clipped to ``pav_union − runway_union`` — none ever lands in
    a runway or off source pavement (user 2026-06-17).  Triangle4XP fills
    the lateral gradient between the centerline and the boundary."""
    poly = s.polygon
    ring = list(poly.exterior.coords)
    ropen = _open(ring)
    n = len(ropen)
    if n < 3:
        return None
    if s.node_altitudes and len(s.node_altitudes) >= len(ring):
        bz = [float(e) for e in s.node_altitudes[:n]]
    elif s.altitude is not None:
        bz = [float(s.altitude)] * n
    else:
        return None
    if len(bz) != n:
        return None

    # Spine nodes live only where the junction overlaps SOURCE pavement
    # (apt.dat + DSF) MINUS runways — never in a runway / off pavement.
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
        return None

    # Every boundary segment (exterior + hole rings) for the LOCAL cap.
    _ring_lists = [ropen]
    for _hole in poly.interiors:
        _hl = _open(list(_hole.coords))
        if len(_hl) >= 3:
            _ring_lists.append(_hl)

    def _edge_corners(px, py):
        """The two endpoints of the boundary EDGE closest to ``(px,py)``
        — the crossing point's flanking corners.  Local to the crossed
        edge (never the globally-nearest vertex, whose cap line can jump
        across a concave apron)."""
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

    def _node_z(px, py):
        v, gap = (field.sample(px, py) if field is not None
                  else (None, float("inf")))
        if v is None:
            v = _boundary_z_at(ropen, bz, px, py, tol=1e9)
            if v is None:
                v = sum(bz) / n
        return float(v)

    # ── Slice lines: centerline polylines that STAY on the centerline ──
    # The slice must not skew off the centerline at its ends.  Densify a
    # node just inside each boundary crossing; then attach to the
    # boundary by neighbour type:
    #   * HARD end (sloping rect / runway — can't take a mid-edge node):
    #     a TINY CAP fans the near-edge node to the crossed edge's two
    #     corners, so the centerline stays straight until ~END_INSET of
    #     the edge.
    #   * SOFT end (node_altitudes apron / junction / free boundary):
    #     extend the slice to the crossing point P right ON the centerline
    #     (a legal mid-edge node there); an abutting sliced shape places
    #     the same P, so the corridor stays continuous across the seam.
    cut_lines: List[LineString] = []
    spine_map: Dict[Tuple[float, float], float] = {}
    soft_pts: List[Tuple[float, float, float]] = []
    for ln in centerlines:
        try:
            seg = poly.intersection(ln)
        except _GEOM_EXC:
            continue
        if seg.is_empty:
            continue
        parts = []
        if seg.geom_type == "LineString":
            parts = [seg]
        elif seg.geom_type == "MultiLineString":
            parts = list(seg.geoms)
        elif seg.geom_type == "GeometryCollection":
            parts = [g for g in seg.geoms if g.geom_type == "LineString"]
        for part in parts:
            L = part.length
            if L < max(2.0, 0.5 * SPINE_STEP_M):
                continue
            # Candidate centerline nodes (not yet committed).
            nseg = max(2, int(round(L / SPINE_STEP_M)))
            ds = {min(_END_INSET_M, 0.45 * L), L - min(_END_INSET_M,
                                                       0.45 * L)}
            for i in range(1, nseg):
                ds.add(i * L / nseg)
            cand: List[Tuple[float, float]] = []
            for d in sorted(x for x in ds if 0.0 < x < L):
                p = part.interpolate(d)
                if not _on_pav(p.x, p.y):
                    continue          # never a node in a runway / off-pav
                cand.append((p.x, p.y))
            if not cand:
                continue
            lo, hi = 0, len(cand) - 1
            extra: List[LineString] = []
            cap_pts: List[Tuple[float, float]] = []
            end_soft = {True: False, False: False}
            for is_entry in (True, False):
                px, py = (part.coords[0] if is_entry else part.coords[-1])
                c1, c2 = _edge_corners(px, py)
                if not (near_hard(px, py) and c1 is not None
                        and c2 is not None):
                    end_soft[is_entry] = True
                    continue
                # HARD end: M must clear the rect flat edge by > EDGE_PROX
                # (a junction vertex inside it trips check_vertex_on_flat_
                # edge).  Walk inward to the first node ≥ _CAP_DEPTH_M
                # PERPENDICULAR from the edge; drop the closer nodes.
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
                cuts, ce = _make_cap(cand[mi][0], cand[mi][1], c1, c2, poly)
                if cuts is None:
                    end_soft[is_entry] = True      # cap infeasible
                else:
                    extra.extend(cuts)
                    cap_pts.extend(ce)
            if lo > hi:
                continue
            spine_xy = list(cand[lo:hi + 1])
            for (x, y) in spine_xy + cap_pts:
                k = _key(x, y)
                if k not in spine_map:
                    spine_map[k] = _node_z(x, y)
            slice_pts: List[Tuple[float, float]] = list(spine_xy)
            for is_entry in (True, False):
                if not end_soft[is_entry]:
                    continue
                # SOFT end: extend the slice onto the boundary at P, a node
                # right on the centerline, and record P for the weld.
                px, py = (part.coords[0] if is_entry else part.coords[-1])
                pz = _node_z(px, py)
                if _key(px, py) not in spine_map:
                    spine_map[_key(px, py)] = pz
                soft_pts.append((px, py, pz))
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
    if not spine_map:
        return None

    # ── Partition ──
    # Use the FULL boundary (exterior + any interior hole rings) so the
    # slice pieces respect holes — an apron with a building cutout must
    # not be filled (that would overlap the building; test_no_self_-
    # overlap).  Keep only faces whose interior is inside the polygon
    # (drops the hole faces).
    try:
        arrangement = unary_union([poly.boundary] + cut_lines)
        faces = []
        for f in polygonize(arrangement):
            if f.is_empty or f.geom_type != "Polygon":
                continue
            try:
                if not poly.contains(f.representative_point()):
                    continue
            except _GEOM_EXC:
                continue
            faces.append(f)
    except _GEOM_EXC:
        return None
    if not faces:
        return None

    # ── Node → altitude map ──
    # Originals (solver z) first; spine nodes override (field z).
    nodez: Dict[Tuple[float, float], float] = {}
    for (x, y), z in zip(ropen, bz):
        nodez[_key(x, y)] = z
    nodez.update(spine_map)

    def _nearest_spine(x, y):
        best = None
        for (kx, ky), kz in spine_map.items():
            d = math.hypot(kx - x, ky - y)
            if best is None or d < best[0]:
                best = (d, kz)
        return best  # (dist, z) or None

    def _zfor(x, y):
        k = _key(x, y)
        if k in nodez:
            return nodez[k]
        # New arrangement vertex (rib hit / cut intersection).
        ns = _nearest_spine(x, y)
        bnd = _boundary_z_at(ropen, bz, x, y)
        if bnd is not None:
            z = bnd
            if ns is not None:
                lo = ns[1] - _LAT_GRADE * ns[0]
                hi = ns[1] + _LAT_GRADE * ns[0]
                z = lo if z < lo else (hi if z > hi else z)
        elif field is not None:
            v, gap = field.sample(x, y)
            if v is None:
                z = ns[1] if ns is not None else sum(bz) / n
            else:
                z = v
                if ns is not None:
                    lo = ns[1] - _LAT_GRADE * ns[0]
                    hi = ns[1] + _LAT_GRADE * ns[0]
                    z = lo if z < lo else (hi if z > hi else z)
        else:
            z = ns[1] if ns is not None else sum(bz) / n
        nodez[k] = z
        return z

    out: List[Tuple[Polygon, List[float]]] = []
    for f in faces:
        if f.area < _MIN_PIECE_AREA:
            continue
        try:
            cring = list(f.exterior.coords)
        except _GEOM_EXC:
            continue
        ce = [round(_zfor(x, y), 1) for (x, y) in cring]
        out.append((f, ce))
    return (out, soft_pts) if out else None


# Roles that may RECEIVE a welded mid-edge node — node_altitudes shapes
# only.  NOT sloping rects (incl. service ROADS, which are 4-corner
# sloping rects) or runways, whose flat form admits only corner sharing.
_WELD_RECEIVER_ROLES = frozenset({
    ROLE_JUNCTION, ROLE_APRON, "groundside_pavement"})
# A soft node welds into a neighbour edge within this distance.
_WELD_TOL_M = 0.25


def _weld_soft_nodes(layout, soft_pts, near_hard) -> int:
    """Insert each soft-end centerline boundary node into any abutting
    receiver shape whose edge passes through it but that lacks the vertex
    — collinear, with the edge-interpolated altitude (no area/grade
    change).  Targeted (only these nodes) so it cannot reshape pieces the
    way a full conformance pass does.  Skips any node near a sloping rect
    / runway (``near_hard``) — welding onto a rect-coincident edge would
    plant a mid-edge node on the rect.  Returns the count inserted."""
    if not soft_pts:
        return 0
    pts = []
    seen = set()
    for (x, y, _z) in soft_pts:
        k = _key(x, y)
        if k not in seen and not near_hard(x, y):
            seen.add(k)
            pts.append((x, y))
    if not pts:
        return 0
    tree = STRtree([Point(p) for p in pts])
    inserted = 0
    for s in layout.shapes:
        if (s.role or "") not in _WELD_RECEIVER_ROLES:
            continue
        poly = s.polygon
        if poly is None or poly.is_empty or poly.geom_type != "Polygon":
            continue
        ring = _open(list(poly.exterior.coords))
        nr = len(ring)
        if nr < 3:
            continue
        alts = s.node_altitudes
        open_alts = (list(alts[:nr]) if alts is not None
                     and len(alts) >= nr else None)
        ownset = {_key(*v) for v in ring}
        new_ring: List[Tuple[float, float]] = []
        new_alts: List[float] = [] if open_alts is not None else None
        changed = False
        for i in range(nr):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % nr]
            new_ring.append((ax, ay))
            if new_alts is not None:
                new_alts.append(open_alts[i])
            dx, dy = bx - ax, by - ay
            s2 = dx * dx + dy * dy
            if s2 < 1e-12:
                continue
            # Candidate soft points near this edge's bbox.
            seg = LineString([(ax, ay), (bx, by)])
            on_edge = []
            for idx in tree.query(seg.buffer(_WELD_TOL_M)):
                px, py = pts[idx]
                if _key(px, py) in ownset:
                    continue
                t = ((px - ax) * dx + (py - ay) * dy) / s2
                if t <= 1e-4 or t >= 1.0 - 1e-4:
                    continue
                cx, cy = ax + t * dx, ay + t * dy
                if math.hypot(px - cx, py - cy) <= _WELD_TOL_M:
                    on_edge.append((t, (px, py)))
            for t, (px, py) in sorted(on_edge):
                new_ring.append((px, py))
                if new_alts is not None:
                    a_i = open_alts[i]
                    a_j = open_alts[(i + 1) % nr]
                    new_alts.append(a_i + t * (a_j - a_i))
                ownset.add(_key(px, py))
                changed = True
                inserted += 1
        if not changed:
            continue
        try:
            np_ = Polygon(new_ring)
            if not np_.is_valid or np_.is_empty:
                continue
        except _GEOM_EXC:
            continue
        s.polygon = np_
        if new_alts is not None:
            s.node_altitudes = new_alts + [new_alts[0]]
    return inserted


def apply_junction_centerline_spine(layout) -> int:
    """Re-emit every ROLE_JUNCTION shape as a centerline-spine partition
    (slice model).  Mutates ``layout.shapes`` in place.  Returns the
    count of junctions re-partitioned."""
    if not JUNCTION_CENTERLINE_SPINE:
        return 0
    field = getattr(layout, "_network_profile_field", None)
    centerlines = _full_centerlines(layout)
    pav_union = getattr(layout, "_source_pav_union", None)
    runway_union = getattr(layout, "runway_union", None)

    # Index sloping-rect / runway boundaries so a slice END can tell
    # whether it meets a HARD edge (tiny corner-cap) or a SOFT one
    # (node on the centerline).
    hard_lines = []
    for s in layout.shapes:
        if (s.role in _HARD_END_ROLES and s.polygon is not None
                and not s.polygon.is_empty
                and s.polygon.geom_type == "Polygon"):
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

    # Slice JUNCTIONS and APRONS along the centerlines that cross them
    # (user 2026-06-17: extend the slice to aprons so a taxi centerline
    # grading THROUGH an apron splits it into a few connected pieces that
    # follow the corridor).  Buildings/terminals stay flat — never sliced.
    new_shapes: List[BuiltShape] = []
    soft_pts_all: List[Tuple[float, float, float]] = []
    n_done = 0
    n_pieces = 0
    for s in layout.shapes:
        if s.role not in (ROLE_JUNCTION, ROLE_APRON):
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
        result = (_partition_junction(s, crossing, field,
                                      pav_union, runway_union, _near_hard)
                  if crossing else None)
        if not result:
            new_shapes.append(s)
            continue
        pieces, soft_pts = result
        soft_pts_all.extend(soft_pts)
        for f, ce in pieces:
            ns = BuiltShape(polygon=f, role=s.role, ref=s.ref)
            if max(ce) - min(ce) < 0.05:
                ns.altitude = round(sum(ce[:-1]) / max(1, len(ce) - 1), 1)
            else:
                ns.node_altitudes = ce
            new_shapes.append(ns)
            n_pieces += 1
        n_done += 1

    layout.shapes = new_shapes
    if n_done:
        # Weld each soft-end centerline boundary node into the abutting
        # node_altitudes neighbour that shares that edge (collinear,
        # altitude-interpolated → no area/grade change) so the corridor
        # node is SHARED — no T-junction.  Targeted to just these nodes
        # (the full enforce_conformance reshaped dense apron pieces into
        # self-overlap; this does not).
        n_weld = _weld_soft_nodes(layout, soft_pts_all, _near_hard)
        UI.vprint(1,
            f"  [pav-builder] {getattr(layout, 'icao', '')}: "
            f"junction-spine sliced {n_done} junction/apron(s) into "
            f"{n_pieces} piece(s); welded {n_weld} corridor seam node(s).")
    return n_done
