"""Canonical-point registry for shared-vertex management.

The pavement builder constructs many shapes (rects, junctions, aprons,
boundary pieces) whose perimeters meet at intersection points.  Each
of those meeting points should be a SINGLE geometric vertex shared
by every adjacent shape — at exact floating-point equality, so that
``pav_union.difference(rects)`` and the OSM emitter's vertex
bucketing produce one node ID per real-world point.

Without a shared registry, each shape's corner is snapped
independently to ``pav.boundary`` and ends up at a slightly
different location than its neighbour's "same" corner.  The
sub-millimetre to multi-metre drift cascades downstream:
``buffer(0)`` validity repairs, sliver-corner removal, T-junction
splits, and merge-corner-junctions all exist as workarounds for
this single root cause.

A canonical-point registry replaces independent snapping with a
deterministic ``get_or_add`` lookup.  Every shape that needs a
corner near (x, y) queries the registry; if any prior shape has
already registered a canonical point within
``SHARED_VERTEX_TOL_M``, the same coordinates are returned.
Otherwise (x, y) becomes the new canonical point for that bucket.

Seeded with the input fixed-geometry vertices (apt.dat row-110
pavement polygon vertices + runway corners) so the canonical
set is anchored to real apt.dat data rather than to whatever
rect happened to register a point first.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import Polygon


__all__ = ["CanonicalPointRegistry", "snap_polygon_through_registry",
           "weld_layout_vertices", "weld_flanking_corners"]


_GEOM_EXC = (ValueError, TypeError,
             GEOSException, TopologicalError, IndexError)


class CanonicalPointRegistry:
    """Spatial-index registry of canonical (x, y) points.

    All shape corners that should be SHARED must go through
    ``get_or_add`` so they pick up the same exact coordinates.

    ``tol_m`` is the bucket radius — points within ``tol_m`` of an
    existing entry resolve to that entry.  Default matches
    ``layout.SHARED_VERTEX_TOL_M`` so registry sharing aligns with
    the OSM emitter's vertex bucketing.
    """

    def __init__(self, tol_m: float = 0.5):
        self.tol_m = tol_m
        # Cell size = tol so neighbours-of-neighbours covers the
        # full lookup radius.
        self._cell = max(tol_m, 0.1)
        self._points: list[tuple[float, float]] = []
        # cell key (ix, iy) → list of indices into self._points
        self._index: dict = {}

    # ── public API ────────────────────────────────────────────────

    def seed(self, points) -> int:
        """Bulk-add anchor points (apt.dat row-110 + runway corners).

        Duplicates within ``tol_m`` collapse to a single entry.
        Returns the number of NEW canonical points added.
        """
        before = len(self._points)
        for p in points:
            self.get_or_add(float(p[0]), float(p[1]))
        return len(self._points) - before

    def get_or_add(self, x: float, y: float
                    ) -> tuple[float, float]:
        """Return the canonical (x, y) for the given input point.

        If an existing canonical point sits within ``tol_m`` of
        (x, y), return its coordinates exactly.  Otherwise insert
        (x, y) as a new canonical point and return it.
        """
        nearby = self._find_nearest(x, y, self.tol_m)
        if nearby is not None:
            return nearby
        return self._add(x, y)

    def find_nearest(self, x: float, y: float,
                      max_d: float) -> tuple[float, float] | None:
        """Find the nearest canonical point within ``max_d`` of
        (x, y).  Does NOT add.  Returns None if no entry qualifies.
        """
        return self._find_nearest(x, y, max_d)

    @property
    def size(self) -> int:
        return len(self._points)

    def points(self) -> list[tuple[float, float]]:
        """Return a snapshot of every canonical point (insertion
        order).  Caller-owned list."""
        return list(self._points)

    # ── internals ─────────────────────────────────────────────────

    def _cell_key(self, x: float, y: float) -> tuple[int, int]:
        return (int(math.floor(x / self._cell)),
                int(math.floor(y / self._cell)))

    def _add(self, x: float, y: float) -> tuple[float, float]:
        idx = len(self._points)
        coords = (float(x), float(y))
        self._points.append(coords)
        self._index.setdefault(
            self._cell_key(x, y), []).append(idx)
        return coords

    def _find_nearest(self, x: float, y: float,
                       max_d: float) -> tuple[float, float] | None:
        # Number of cells in each direction we have to scan to cover
        # the lookup radius.  +1 to be safe at cell boundaries.
        n_cells = int(math.ceil(max_d / self._cell)) + 1
        cx, cy = self._cell_key(x, y)
        best: tuple[float, float] | None = None
        best_d = max_d
        for dx in range(-n_cells, n_cells + 1):
            for dy in range(-n_cells, n_cells + 1):
                key = (cx + dx, cy + dy)
                bucket = self._index.get(key)
                if not bucket:
                    continue
                for idx in bucket:
                    px, py = self._points[idx]
                    d = math.hypot(px - x, py - y)
                    if d < best_d:
                        best_d = d
                        best = (px, py)
        return best


def snap_polygon_through_registry(
        poly: Polygon | None,
        registry: CanonicalPointRegistry | None,
) -> Polygon | None:
    """Route every vertex of ``poly``'s exterior + interior rings
    through ``registry.get_or_add`` so drift introduced by
    ``buffer(0)`` / ``unary_union`` / ``simplify`` resolves to
    canonical (x, y) coordinates shared with adjacent shapes.

    Returns the snapped polygon (a new ``Polygon`` if any vertex
    moved, the input otherwise), or ``None`` if the snap produces
    a degenerate shape.  If either input is ``None`` / empty, the
    input is returned unchanged.
    """
    if registry is None or poly is None or poly.is_empty:
        return poly

    def _snap_ring(coords):
        snapped = []
        for x, y in coords:
            cp = registry.get_or_add(float(x), float(y))
            snapped.append(cp)
        return snapped

    try:
        ext = _snap_ring(poly.exterior.coords)
        if len(set(ext)) < 3:
            return None
        interiors = []
        for ring in poly.interiors:
            ri = _snap_ring(ring.coords)
            if len(set(ri)) < 3:
                continue
            interiors.append(ri)
        snapped_poly = Polygon(ext, interiors)
        if not snapped_poly.is_valid:
            snapped_poly = snapped_poly.buffer(0)
            if (snapped_poly.is_empty
                    or snapped_poly.geom_type not in (
                        "Polygon", "MultiPolygon")):
                return None
            if snapped_poly.geom_type == "MultiPolygon":
                snapped_poly = max(
                    snapped_poly.geoms, key=lambda g: g.area)
        return snapped_poly
    except _GEOM_EXC:
        return None


def weld_flanking_corners(layout, roles, tol_m: float = 1.0) -> int:
    """Weld near-coincident corners that FLANK a genuinely-shared node.

    After :func:`weld_layout_vertices` two abutting shapes share exact corner
    coordinates along a common boundary, but at the END of that boundary their
    rings can DIVERGE: shape A turns to vertex Va and shape B to Vb, where Va
    and Vb are the SAME physical corner up to ~``tol_m`` apart but were never
    unified (the proximity weld's strict 0.5 m tolerance just misses them — the
    gap is a 3-4-5 ≈ 0.50 m).  The sub-metre gap leaves a thin sliver and, after
    the solve, a CROSS-SHAPE elevation step at the unshared corner (HECA apron
    #303 / #369: 0.5 m apart, 0.3 m step — the within/cross grade gate trips).

    Unlike a blanket proximity weld (which would also merge legitimately
    DISTINCT vertices that merely sit < ``tol_m`` apart — 56 such non-flanking
    pairs at HECA), this welds a pair ONLY when BOTH are ring-neighbours of a
    common shared corner, so it touches true conformance slivers and nothing
    else.  Runs PRE-solve (no ``node_altitudes`` yet → index alignment is moot);
    any snap that would collapse/duplicate a ring vertex is skipped.

    Returns the number of shapes modified.
    """
    targets = [s for s in layout.shapes
               if s.role in roles and s.polygon is not None
               and not s.polygon.is_empty
               and s.polygon.geom_type == "Polygon"]

    def key(x, y):
        return (round(float(x), 3), round(float(y), 3))

    rings: dict = {}                      # id(shape) -> (shape, open ring coords)
    owners: dict = {}                     # coord key -> [(shape, ring index), ...]
    for s in targets:
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        ring = (coords[:-1] if len(coords) > 1 and coords[0] == coords[-1]
                else coords)
        rings[id(s)] = (s, ring)
        for i, (x, y) in enumerate(ring):
            owners.setdefault(key(x, y), []).append((s, i))

    # Union-find over coordinate keys: cluster the flanking neighbours of every
    # shared corner that lie within ``tol_m`` of each other (cross-shape).
    parent: dict = {}

    def find(k):
        parent.setdefault(k, k)
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for coord, own in owners.items():
        if len(own) < 2:
            continue                       # not a shared corner
        flank = []                         # (id(shape), coord key, (x, y))
        for (s, i) in own:
            _, ring = rings[id(s)]
            n = len(ring)
            for nb in (ring[i - 1], ring[(i + 1) % n]):
                flank.append((id(s), key(*nb), (nb[0], nb[1])))
        for a in range(len(flank)):
            for b in range(a + 1, len(flank)):
                ida, ka, pa = flank[a]
                idb, kb, pb = flank[b]
                if ida == idb or ka == kb:
                    continue               # same shape / already-shared corner
                d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
                if 1e-6 < d <= tol_m:
                    parent[find(ka)] = find(kb)

    if not parent:
        return 0
    # Snap every clustered key onto its cluster representative (the min key).
    snap_to: dict = {}
    for k in list(parent):
        r = find(k)
        if k != r:
            snap_to[k] = (float(r[0]), float(r[1]))
    if not snap_to:
        return 0

    modified = 0
    for s in targets:
        _, ring = rings[id(s)]
        changed = False
        new_ring = []
        for (x, y) in ring:
            k = key(x, y)
            if k in snap_to:
                new_ring.append(snap_to[k])
                changed = True
            else:
                new_ring.append((float(x), float(y)))
        if not changed:
            continue
        try:
            newpoly = Polygon(new_ring)
        except _GEOM_EXC:
            continue
        # Skip if the snap collapsed/duplicated a vertex (ring length changes)
        # or produced an invalid polygon — keep the original then.
        if (newpoly.is_empty or not newpoly.is_valid
                or newpoly.geom_type != "Polygon"
                or len(newpoly.exterior.coords) != len(ring) + 1):
            continue
        s.polygon = newpoly
        modified += 1
    return modified


def weld_layout_vertices(layout, roles, tol_m: float = 0.5) -> int:
    """Weld near-coincident vertices across the given shape ``roles`` to
    a single shared coordinate.

    Adjacent shapes (a taxi rect and the junction carved beside it) are
    snapped to ``pav.boundary`` at different pipeline stages, and
    post-emit passes (conformance T-junction insertion, absorption clips)
    add fresh vertices that were never routed through the build-time
    registry.  Two such "same point" vertices then sit up to ``tol_m``
    apart, and their edges cross by a sub-tol sliver — the residue model
    is seamless as a set operation, but the per-shape vertex coordinates
    drift.  This pass re-welds them.

    A FRESH registry is built (so it can't carry stale near-duplicate
    canonical points from earlier stages): pass 1 registers every
    target-shape vertex — the first occurrence within ``tol_m`` wins, so
    a rect corner and the junction vertex beside it collapse to ONE
    canonical coordinate.  Pass 2 snaps each shape's vertices to those
    welded points.

    Only snaps that PRESERVE a shape's vertex count are applied, so any
    ``node_altitudes`` list stays index-aligned with the ring (snapping
    moves coordinates in place; it never reorders or drops a vertex).

    Returns the number of shapes modified.
    """
    reg = CanonicalPointRegistry(tol_m=tol_m)
    targets = [s for s in layout.shapes
               if s.role in roles and s.polygon is not None
               and not s.polygon.is_empty
               and s.polygon.geom_type == "Polygon"]
    # Pass 1: register every vertex (welds near-coincident to first seen).
    for s in targets:
        try:
            for (x, y) in s.polygon.exterior.coords:
                reg.get_or_add(float(x), float(y))
            for ring in s.polygon.interiors:
                for (x, y) in ring.coords:
                    reg.get_or_add(float(x), float(y))
        except _GEOM_EXC:
            continue
    # Pass 2: snap each shape's vertices to the welded canonical points.
    modified = 0
    for s in targets:
        try:
            n_before = len(s.polygon.exterior.coords)
            snapped = snap_polygon_through_registry(s.polygon, reg)
        except _GEOM_EXC:
            continue
        if (snapped is None or snapped.is_empty
                or snapped.geom_type != "Polygon"):
            continue
        # Vertex collapsed (two ring points welded together) — skip so
        # node_altitudes alignment is preserved.
        if len(snapped.exterior.coords) != n_before:
            continue
        if snapped.equals(s.polygon):
            continue
        s.polygon = snapped
        modified += 1
    return modified
