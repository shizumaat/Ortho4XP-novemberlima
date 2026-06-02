"""Boundary-conformance invariant for the emitted patch.

DESIGN REQUIREMENT (user, long-standing): the emitted pavement shapes must
form a CONFORMING planar partition — adjacent shapes share *identical*
vertices along common edges, and no two shapes' edges cross.  "No area
overlap" (``test_no_self_overlap``) is necessary but NOT sufficient: two
shapes can be area-disjoint yet share a boundary non-conformingly (a
T-junction — one shape has a vertex mid-edge that its neighbour lacks).

Why it matters: Ortho4XP feeds the patch to Triangle4XP as constraint
edges.  A T-junction or crossing forces the constrained triangulation to
node the arrangement, spraying near-degenerate sub-cm² sliver triangles
along the seam.  At HECA this turned ~119k healthy airport triangles into
~2.36M (94 % sub-1 m²) and a 9m40s X-Plane load.  Area-overlap checks are
blind to it because a T-junction's intersection is a zero-area line.

This module both DETECTS violations (the runtime invariant + test gate)
and ENFORCES conformance (insert each neighbour vertex that lies on a
shape's edge, so shared boundaries become vertex-identical).  Run at the
end of the pipeline for EVERY airport, not just baselines.
"""
from __future__ import annotations

import math
from collections import defaultdict

from shapely.geometry import LineString
from shapely.strtree import STRtree

from .layout import (
    BuiltShape,
    PavementLayout,
    SHARED_VERTEX_TOL_M,
    corner_alts_from_high_low,
)

__all__ = [
    "find_conformance_violations",
    "enforce_conformance",
    "CONFORMANCE_TOL_M",
]

# Perpendicular distance under which a neighbour vertex is considered "on"
# a shape's edge (a T-junction to be inserted).  Matches the shared-vertex
# snap tolerance so a point already treated as a shared corner elsewhere is
# treated consistently here.
CONFORMANCE_TOL_M = SHARED_VERTEX_TOL_M

# Refs whose footprints intentionally OVERLAY other pavement rather than
# tiling with it, so they are exempt from the conformance partition.  The
# DEM bridge is a wide transition strip laid alongside/over the perimeter
# band; it is trimmed against pavement (no area overlap) but is not part
# of the airside constraint partition in the same way.
#
# NOTE (user 2026-05-22): the airport-boundary RIBBON (``ref ==
# "airport_boundary"``) is NO LONGER exempt.  It now lies entirely inside
# row-130 and pavement is clipped back to its inner edge
# (``_clip_pavement_to_boundary_interior``), so the ribbon and pavement
# must form a conforming partition — sharing seam nodes bidirectionally —
# or Triangle4XP nodes the seam into slivers.
# Wingtip / RESA clearance cuts (``ref == "surface_clearance"``) are
# terrain-grading overlays laid alongside pavement with a built-in gap
# (they share no edge with pavement), so — like the DEM bridge — they
# are not part of the airside conforming partition.
_OVERLAY_REFS = {"boundary_dem_bridge", "surface_clearance"}


def _open_ring(poly):
    """Exterior ring coords without the closing duplicate, or None."""
    if poly is None or poly.is_empty or poly.geom_type != "Polygon":
        return None
    coords = list(poly.exterior.coords)
    if len(coords) >= 2 and coords[0] == coords[-1]:
        coords = coords[:-1]
    return coords if len(coords) >= 3 else None


def _vertex_alts(shape, n):
    """Per-(open-ring-)vertex altitudes for ``shape`` (length ``n``), or
    None if the shape carries no usable altitude model.

    Used so an inserted vertex can be given the linearly-interpolated
    altitude along its edge, and the shape re-emitted as ``node_altitudes``
    when it was a sloped quad (which only supports exactly 4 corners)."""
    na = shape.node_altitudes
    if na is not None:
        a = list(na)
        if len(a) == n + 1:      # includes closing repeat
            a = a[:-1]
        if len(a) == n:
            return a
        return None
    if (shape.altitude_high is not None
            and shape.altitude_low is not None and n == 4):
        return corner_alts_from_high_low(
            shape.altitude_high, shape.altitude_low)
    if shape.altitude is not None:
        return [shape.altitude] * n
    return None


def _eligible(shape):
    p = getattr(shape, "polygon", None)
    if p is None or p.is_empty or p.geom_type != "Polygon":
        return False
    return getattr(shape, "ref", None) not in _OVERLAY_REFS


def _build_vertex_index(shapes):
    """Return (cell_size, grid) where grid maps a coarse cell to the list
    of (x, y) vertices in it, for fast 'points near an edge' queries."""
    cell = 5.0  # metres
    grid = defaultdict(list)
    for s in shapes:
        ring = _open_ring(s.polygon)
        if ring is None:
            continue
        for x, y in ring:
            grid[(int(x / cell), int(y / cell))].append((x, y))
    return cell, grid


def _points_near_edge(grid, cell, ax, ay, bx, by, tol):
    """Yield distinct (x, y) grid vertices within ``tol`` of segment a-b's
    bounding band (a cheap superset; precise test done by the caller)."""
    minx, maxx = (ax, bx) if ax <= bx else (bx, ax)
    miny, maxy = (ay, by) if ay <= by else (by, ay)
    i0 = int((minx - tol) / cell)
    i1 = int((maxx + tol) / cell)
    j0 = int((miny - tol) / cell)
    j1 = int((maxy + tol) / cell)
    seen = set()
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            for pt in grid.get((i, j), ()):
                if pt not in seen:
                    seen.add(pt)
                    yield pt


def _tjunctions_on_edge(ax, ay, bx, by, candidates, tol):
    """Return [(t, (px, py)), ...] for candidate points lying on the OPEN
    segment a-b (perpendicular dist < tol, projection strictly interior,
    not within tol of either endpoint)."""
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return []
    L = math.sqrt(L2)
    out = []
    for px, py in candidates:
        t = ((px - ax) * dx + (py - ay) * dy) / L2
        if t <= 0.0 or t >= 1.0:
            continue
        # perpendicular distance
        perp = abs((px - ax) * dy - (py - ay) * dx) / L
        if perp >= tol:
            continue
        # not coincident with an endpoint
        if t * L < tol or (1.0 - t) * L < tol:
            continue
        out.append((t, (px, py)))
    out.sort()
    return out


def find_conformance_violations(shapes, tol=CONFORMANCE_TOL_M):
    """Detect conformance violations among emitted shapes.

    Returns ``(t_junctions, crossings)`` where each is a list of
    ``(x, y)`` locations: a T-junction is a vertex of one shape lying on
    the interior of another shape's edge; a crossing is two shapes' edges
    intersecting at an interior point of both.  An empty result means the
    patch is a conforming partition (the invariant holds).
    """
    elig = [s for s in shapes if _eligible(s)]
    cell, grid = _build_vertex_index(elig)
    own = []          # set of own-ring vertices per shape (to exclude)
    rings = []
    for s in elig:
        ring = _open_ring(s.polygon)
        rings.append(ring)
        own.append(set(ring) if ring else set())

    t_junctions = []
    for ring, ownset in zip(rings, own):
        if ring is None:
            continue
        n = len(ring)
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            cands = [pt for pt in _points_near_edge(
                        grid, cell, ax, ay, bx, by, tol)
                     if pt not in ownset]
            for _, pt in _tjunctions_on_edge(ax, ay, bx, by, cands, tol):
                t_junctions.append(pt)

    # Crossings via edge STRtree.
    edges = []
    for ring in rings:
        if ring is None:
            continue
        n = len(ring)
        for i in range(n):
            a = ring[i]
            b = ring[(i + 1) % n]
            if a != b:
                edges.append((a, b))
    lines = [LineString([a, b]) for a, b in edges]
    crossings = []
    if lines:
        tree = STRtree(lines)
        for i, ln in enumerate(lines):
            for j in tree.query(ln):
                if j <= i:
                    continue
                a0, a1 = edges[i]
                b0, b1 = edges[j]
                if {a0, a1} & {b0, b1}:
                    continue          # share an endpoint: not a crossing
                inter = ln.intersection(lines[j])
                if inter.geom_type == "Point":
                    px = (inter.x, inter.y)
                    if px not in (a0, a1, b0, b1):
                        crossings.append(px)
    return t_junctions, crossings


def enforce_conformance(layout: "PavementLayout",
                        tol=CONFORMANCE_TOL_M,
                        owner_roles: "set[str] | None" = None
                        ) -> tuple[int, int]:
    """Make the emitted shapes a conforming partition by inserting, into
    each shape's edges, every NEIGHBOUR vertex that lies on that edge
    (a T-junction).  The inserted vertex takes the edge's linearly
    interpolated altitude; a sloped-quad shape that gains a vertex is
    converted to ``node_altitudes`` (it can no longer be a 4-corner quad).

    ``owner_roles``: when given, only shapes whose role is in this set may
    RECEIVE inserted vertices (every eligible shape still contributes its
    vertices as candidates).  Used for the PRE-SOLVE pass: conform only
    apron/junction edges so abutting aprons share a canonical node and the
    solver grades them to match — WITHOUT inserting vertices into taxi-rect
    sloping edges (which would break their 4-corner planar form before the
    solver assigns altitudes).

    Returns ``(shapes_modified, vertices_inserted)``.  Idempotent: a second
    call inserts nothing.  Overlay roles (boundary ribbon) are skipped.
    """
    elig = [s for s in layout.shapes if _eligible(s)]
    cell, grid = _build_vertex_index(elig)
    shapes_modified = 0
    vertices_inserted = 0
    from shapely.geometry import Polygon

    for s in elig:
        if owner_roles is not None and (s.role or "") not in owner_roles:
            continue
        ring = _open_ring(s.polygon)
        if ring is None:
            continue
        n = len(ring)
        ownset = set(ring)
        alts = _vertex_alts(s, n)
        # A shape emitted with a SINGLE ``altitude`` (no high/low, no
        # node_altitudes) is flat: every corner sits at that level, so a
        # vertex inserted on an edge between two equal-altitude corners is
        # also at that level — the shape stays flat.  Keep the single
        # ``altitude`` instead of converting to ``node_altitudes`` (which
        # for a TERMINAL would violate H26's flat-only rule — the HECA
        # terminal10 case — and is redundant for any other flat shape).
        flat_single_alt = (s.node_altitudes is None
                           and s.altitude_high is None
                           and s.altitude_low is None
                           and s.altitude is not None)
        # Build the new ring edge by edge, inserting T-junction points.
        new_ring = []
        new_alts = [] if alts is not None else None
        inserted_here = 0
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            new_ring.append((ax, ay))
            if new_alts is not None:
                new_alts.append(alts[i])
            cands = [pt for pt in _points_near_edge(
                        grid, cell, ax, ay, bx, by, tol)
                     if pt not in ownset]
            tjs = _tjunctions_on_edge(ax, ay, bx, by, cands, tol)
            for t, (px, py) in tjs:
                new_ring.append((px, py))
                if new_alts is not None:
                    a_i = alts[i]
                    a_j = alts[(i + 1) % n]
                    new_alts.append(a_i + t * (a_j - a_i))
                inserted_here += 1
        if not inserted_here:
            continue
        # Rebuild the polygon; bail (leave shape untouched) if invalid.
        try:
            new_poly = Polygon(new_ring)
            if not new_poly.is_valid or new_poly.is_empty:
                continue
        except Exception:
            continue
        s.polygon = new_poly
        if new_alts is not None and not flat_single_alt:
            # node_altitudes carries the closing repeat.
            s.node_altitudes = new_alts + [new_alts[0]]
            s.altitude_high = None
            s.altitude_low = None
        # flat_single_alt: leave s.altitude as-is (the new vertex inherits
        # it); the shape stays flat and keeps the single-altitude model.
        shapes_modified += 1
        vertices_inserted += inserted_here
    return shapes_modified, vertices_inserted
