"""Self-partitioned junctions with a centerline "spine" (rib-quad model).

Problem (docs/junction_centerline_spine.md): junctions emit today as a
single ring polygon with boundary-only ``node_altitudes``.  X-Plane /
Triangle4XP triangulate the interior by interpolating between boundary
vertices, so a taxi centerline crossing the junction INTERIOR — where
there are no vertices on it — renders a waving surface instead of the
solver's clean ≤1.5% corridor profile (OMAA taxiway H @ junction -10225:
field is a flat 1.5% but the emitted surface spikes to 3.6%).

Fix (user's model): give the junction interior real nodes ON each
crossing centerline, then partition the junction so the centerline is a
SHARED EDGE that carries the smooth profile:

  1. Densify each crossing centerline through the junction every
     ``SPINE_STEP_M`` — "spine" nodes placed strictly INBOARD (the first
     ~one interval in from the boundary, NONE on the edge), each pinned to
     the network-profile field value (``_network_profile_field.sample``).
     Never anchoring a node on the boundary is what keeps the feeding
     rect conformant: the crossing point is the INTERIOR of that rect's
     flat edge, where only the rect's corners may be shared
     (test_no_vertex_on_sloping_rect_flat_edge).
  2. At each spine node cast a PERPENDICULAR rib to the junction edge both
     ways (full rib when the two reaches are ~equal, else the short side
     only).  A rib hitting boundary shared with a sloping rect / runway
     reuses an EXISTING corner (``near_hard`` → mandatory snap); on
     apron / free boundary it may land a new node (welded later).
  3. Polygonize the junction with {spine polylines ∪ ribs} as cut lines →
     quad strips along the corridor, with the entry/exit left as gap
     polygons bounded by the rect's existing corners.  No triangle fan,
     no unconstrained Delaunay (which poked chord edges across concave
     boundaries into neighbours).
  4. Spine nodes carry the dominant smooth profile (field z); boundary /
     rib-edge nodes keep the solver altitude (clamped to ≤1.5% lateral
     from the nearest spine where they are NEW rib hits).  A final
     conformance-heal welds new apron/junction-shared boundary nodes into
     the neighbour (collinear, altitude-neutral).

X-Plane renders the emitted polygons as-is, so a taxi centerline now
grades through the junction interior at the corridor profile.  Measured
OMAA: H @ junction -10225 emitted grade 3.58% spike → tracks the field's
1.50%; conformance 191 T-junctions / 51 crossings (naive rib model) → 3 /
5 (≈ the 2 / 2 ring baseline).

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

# Sloping 4-corner rects (and runways) whose edges a rib must NOT plant a
# new node on — inserting a vertex would break their planar form / trip
# test_no_vertex_on_sloping_rect_edge, and they cannot RECEIVE a heal
# vertex.  Where a junction's side boundary abuts one of these, a rib
# reuses the existing shared vertex instead of cutting a fresh node.
_HARD_RECT_ROLES = frozenset({
    ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL, ROLE_STUB,
    ROLE_CROSS_CONNECTOR, ROLE_RUNWAY, ROLE_RUNWAY_CROSSING})
# A rib hit within this distance of a hard-rect boundary is treated as
# landing on a rect-shared edge → snap mandatory.
_HARD_EDGE_TOL_M = 1.5
# Mandatory-snap search radius for a rect-shared rib hit (boundary
# vertices near rect connections are dense from pre-solve conformance).
_HARD_SNAP_TOL_M = 2.0 * SPINE_STEP_M

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["apply_junction_centerline_spine"]


# A rib's boundary hit snaps to an existing boundary vertex when one is
# very close (re-uses the shared node instead of crowding it), otherwise
# the rib lands a NEW node on the junction's side boundary — which is
# correct (the rib points sideways, away from the feeding rect's
# end-edge).  New nodes on a neighbour-shared edge are welded afterward
# by the conformance-heal pass.
_RIB_SNAP_TOL_M = 1.0
# Two perpendicular reaches count as "equal" (→ full rib) when their
# difference is within this fraction of the longer reach (or 2 m, abs).
_RIB_EQ_FRAC = 0.20
_RIB_EQ_ABS_M = 2.0
# Lateral grade cap from the centerline out to a radial edge node.
_LAT_GRADE = 0.015
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
    """Every centerline of the FULL route graph as plain LineStrings:
    apt.dat taxi network + discovered/unreferenced lanes + runway
    long-axes (for runway-crossing junctions)."""
    out: List[LineString] = []
    for item in (getattr(layout, "apt_taxi_centerlines", None) or []):
        ln = item[0] if isinstance(item, tuple) else item
        if ln is not None and not ln.is_empty:
            out.append(ln)
    for item in (getattr(layout, "_discovered_centerlines", None) or []):
        ln = item[0] if isinstance(item, tuple) else item
        if ln is not None and not ln.is_empty:
            out.append(ln)
    for s in layout.shapes:
        if s.role != ROLE_RUNWAY or s.polygon is None or s.polygon.is_empty:
            continue
        try:
            rc = _open(list(s.polygon.exterior.coords))
        except _GEOM_EXC:
            continue
        if len(rc) != 4:
            continue
        a_mid = (0.5 * (rc[0][0] + rc[3][0]), 0.5 * (rc[0][1] + rc[3][1]))
        b_mid = (0.5 * (rc[1][0] + rc[2][0]), 0.5 * (rc[1][1] + rc[2][1]))
        try:
            out.append(LineString([a_mid, b_mid]))
        except _GEOM_EXC:
            continue
    return out


def _nearest_boundary_hit(boundary, px, py, nx, ny, reach):
    """Cast the ray ``(px,py) + t*(nx,ny)``, ``t>0`` and return the
    nearest boundary intersection point and its distance, or None."""
    far = (px + nx * reach, py + ny * reach)
    try:
        inter = boundary.intersection(LineString([(px, py), far]))
    except _GEOM_EXC:
        return None
    if inter.is_empty:
        return None
    pts = []
    gt = inter.geom_type
    if gt == "Point":
        pts = [inter]
    elif gt in ("MultiPoint", "GeometryCollection"):
        pts = [g for g in inter.geoms if g.geom_type == "Point"]
    elif gt in ("LineString", "MultiLineString"):
        # Ray runs along an edge — take its far endpoint.
        coords = []
        geoms = inter.geoms if gt == "MultiLineString" else [inter]
        for g in geoms:
            coords.extend(list(g.coords))
        pts = [Point(c) for c in coords]
    best = None
    for p in pts:
        d = math.hypot(p.x - px, p.y - py)
        if d < 1e-6:
            continue
        if best is None or d < best[1]:
            best = ((p.x, p.y), d)
    return best


def _ribs_for_centerline(poly: Polygon, boundary, bverts, spine, reach,
                         near_hard):
    """Build rib cut-lines for one centerline's interior spine nodes.

    ``spine`` is ``[(x, y, z, d, tx, ty)]`` — interior nodes with the
    unit centerline tangent ``(tx, ty)``.  ``near_hard(x, y)`` is True
    when a point lies on the junction boundary shared with a sloping rect
    / runway — there a rib MUST reuse an existing boundary vertex (no
    fresh node), so the abutting rect stays conformant.  Returns
    ``rib_lines``."""
    rib_lines: List[LineString] = []

    def _snap_soft(hx, hy):
        # Re-use an existing boundary vertex only when the hit is almost
        # exactly on it; otherwise keep the true perpendicular hit (a new
        # side-boundary node, welded to the neighbour later).
        best = None
        for (vx, vy) in bverts:
            d = math.hypot(vx - hx, vy - hy)
            if d <= _RIB_SNAP_TOL_M and (best is None or d < best[1]):
                best = ((vx, vy), d)
        return best[0] if best else (hx, hy)

    def _snap_hard(hx, hy):
        # MANDATORY snap to the nearest existing boundary vertex (rib
        # lands on a rect-shared edge); None when none is close enough,
        # so the caller drops that rib side rather than plant a new node.
        best = None
        for (vx, vy) in bverts:
            d = math.hypot(vx - hx, vy - hy)
            if d <= _HARD_SNAP_TOL_M and (best is None or d < best[1]):
                best = ((vx, vy), d)
        return best[0] if best else None

    # Every spine node is interior (none on the boundary); each gets a
    # perpendicular rib.  Left-normal of the stored tangent is (-ty, tx).
    for (px, py, pz, pd, tx, ty) in spine:
        nx, ny = -ty, tx
        hp = _nearest_boundary_hit(boundary, px, py, nx, ny, reach)
        hm = _nearest_boundary_hit(boundary, px, py, -nx, -ny, reach)
        if hp is None and hm is None:
            continue
        dp = hp[1] if hp else float("inf")
        dm = hm[1] if hm else float("inf")
        longer = max(d for d in (dp, dm) if math.isfinite(d))
        equal = (math.isfinite(dp) and math.isfinite(dm) and
                 abs(dp - dm) <= max(_RIB_EQ_ABS_M, _RIB_EQ_FRAC * longer))
        sides = []
        if equal:
            sides = [hp, hm]
        else:
            sides = [hp] if dp <= dm else [hm]
        for h in sides:
            if h is None:
                continue
            (hx, hy), dist = h
            if near_hard(hx, hy):
                snapped = _snap_hard(hx, hy)
                if snapped is None:
                    continue
                sx, sy = snapped
            else:
                sx, sy = _snap_soft(hx, hy)
            if math.hypot(sx - px, sy - py) < 1e-6:
                continue
            rib_lines.append(LineString([(px, py), (sx, sy)]))
    return rib_lines


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


def _partition_junction(s: BuiltShape, centerlines, field, near_hard):
    """Return a list of (Polygon, node_altitudes) pieces for junction
    ``s`` under the rib-quad model, or None to leave the ring unchanged.
    ``near_hard(x, y)`` flags boundary shared with a sloping rect/runway
    (ribs there reuse existing nodes)."""
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

    boundary = poly.exterior
    minx, miny, maxx, maxy = poly.bounds
    reach = math.hypot(maxx - minx, maxy - miny) + 10.0

    # ── Densify every crossing centerline to spine nodes (field z) ──
    spines: List[List[Tuple[float, float, float, float]]] = []
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
            # Interior nodes only — evenly spaced, the first ~one interval
            # inboard of the entry and the last inboard of the exit, so no
            # node lands ON the boundary (the rect's flat edge).
            nseg = max(2, int(round(L / SPINE_STEP_M)))
            ds = [i * L / nseg for i in range(1, nseg)]
            nodes = []
            for d in ds:
                p = part.interpolate(d)
                # True centerline tangent at this node (for the rib's
                # perpendicular direction).
                a = part.interpolate(max(0.0, d - 0.75))
                b = part.interpolate(min(L, d + 0.75))
                tx, ty = b.x - a.x, b.y - a.y
                tn = math.hypot(tx, ty)
                if tn < 1e-9:
                    continue
                tx, ty = tx / tn, ty / tn
                v, gap = (field.sample(p.x, p.y) if field is not None
                          else (None, float("inf")))
                if v is None:
                    # Fall back to the boundary solver altitude so the
                    # node still has a level (rare — coverage hole).
                    v = _boundary_z_at(ropen, bz, p.x, p.y, tol=reach)
                    if v is None:
                        v = sum(bz) / n
                nodes.append((p.x, p.y, float(v), d, tx, ty))
            if nodes:
                spines.append(nodes)
    if not spines:
        return None

    # ── Spine cuts + ribs ──
    # Spine nodes are placed strictly INBOARD (first node ~one interval in
    # from the boundary, none AT the edge): the crossing point is the
    # interior of the feeding rect's flat edge, where a node is illegal
    # (only the rect's corners may be shared —
    # test_no_vertex_on_sloping_rect_flat_edge) and would T-junction the
    # rect.  Each interior spine node carries a perpendicular rib to the
    # side boundary; the entry region beyond the first rib is left as a
    # gap polygon bounded by the rect's existing corners.  No boundary
    # node is planted on a rect edge.
    bverts = list(ropen)
    cut_lines: List[LineString] = []
    spine_map: Dict[Tuple[float, float], float] = {}
    for spine in spines:
        for nd in spine:
            spine_map[_key(nd[0], nd[1])] = nd[2]
        if len(spine) >= 2:
            cut_lines.append(LineString([(p[0], p[1]) for p in spine]))
        ribs = _ribs_for_centerline(
            poly, boundary, bverts, spine, reach, near_hard)
        cut_lines.extend(ribs)

    # ── Partition ──
    try:
        arrangement = unary_union([boundary] + cut_lines)
        faces = [f for f in polygonize(arrangement)
                 if not f.is_empty and f.geom_type == "Polygon"]
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
    return out if out else None


def apply_junction_centerline_spine(layout) -> int:
    """Re-emit every ROLE_JUNCTION shape as a centerline-spine partition
    (rib-quad strips).  Mutates ``layout.shapes`` in place.  Returns the
    count of junctions re-partitioned."""
    if not JUNCTION_CENTERLINE_SPINE:
        return 0
    field = getattr(layout, "_network_profile_field", None)
    centerlines = _full_centerlines(layout)

    # Index every sloping-rect / runway boundary so a rib can tell when
    # its hit lands on a rect-shared junction edge (→ reuse an existing
    # node there instead of planting a fresh one the rect can't share).
    hard_lines = []
    for s in layout.shapes:
        if (s.role in _HARD_RECT_ROLES and s.polygon is not None
                and not s.polygon.is_empty
                and s.polygon.geom_type == "Polygon"):
            try:
                hard_lines.append(s.polygon.exterior)
            except _GEOM_EXC:
                continue
    hard_tree = STRtree(hard_lines) if hard_lines else None

    def _near_hard(x, y):
        if hard_tree is None:
            return False
        p = Point(x, y)
        for idx in hard_tree.query(p.buffer(_HARD_EDGE_TOL_M)):
            try:
                if hard_lines[idx].distance(p) <= _HARD_EDGE_TOL_M:
                    return True
            except _GEOM_EXC:
                continue
        return False

    new_shapes: List[BuiltShape] = []
    n_done = 0
    n_pieces = 0
    for s in layout.shapes:
        if s.role != ROLE_JUNCTION:
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
        pieces = (_partition_junction(s, crossing, field, _near_hard)
                  if crossing else None)
        if not pieces:
            new_shapes.append(s)
            continue
        for f, ce in pieces:
            ns = BuiltShape(polygon=f, role=ROLE_JUNCTION, ref=s.ref)
            if max(ce) - min(ce) < 0.05:
                ns.altitude = round(sum(ce[:-1]) / max(1, len(ce) - 1), 1)
            else:
                ns.node_altitudes = ce
            new_shapes.append(ns)
            n_pieces += 1
        n_done += 1

    layout.shapes = new_shapes
    if n_done:
        UI.vprint(1,
            f"  [pav-builder] {getattr(layout, 'icao', '')}: "
            f"junction-spine partitioned {n_done} junction(s) into "
            f"{n_pieces} piece(s).")
        # Conformance-heal: each rib that hit the junction's side boundary
        # planted a NEW node on an edge shared with an abutting apron /
        # junction.  Weld those nodes into the neighbour (collinear,
        # altitude-interpolated → no grade or area change) so the
        # partition stays a conforming tiling.  Restrict the RECEIVERS to
        # apron + junction (NOT taxi rects — inserting into a sloping
        # rect's edge would break its 4-corner planar form and trip
        # test_no_vertex_on_sloping_rect_edge); ribs hitting a rect-shared
        # edge are kept conformant by reusing the rect's existing nodes
        # (see _ribs_for_centerline snap).
        try:
            from .conformance import enforce_conformance
            _hs, _hv = enforce_conformance(
                layout, owner_roles={ROLE_JUNCTION, ROLE_APRON})
            if _hv:
                UI.vprint(1,
                    f"  [pav-builder] {getattr(layout, 'icao', '')}: "
                    f"junction-spine conformance-heal inserted {_hv} "
                    f"vertex(es) into {_hs} apron/junction shape(s).")
        except _GEOM_EXC:
            pass
    return n_done
