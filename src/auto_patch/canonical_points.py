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
           "snap_polygon_parts_through_registry", "weld_layout_vertices"]


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

    parts = snap_polygon_parts_through_registry(poly, registry)
    if not parts:
        return None
    return max(parts, key=lambda g: g.area)


def snap_polygon_parts_through_registry(
        poly: Polygon | None,
        registry: CanonicalPointRegistry | None,
) -> list:
    """Parts-preserving variant of ``snap_polygon_through_registry``:
    when the snap pinches the ring into a self-intersection and the
    ``buffer(0)`` validity repair splits it into a MultiPolygon, return
    EVERY Polygon part instead of silently keeping only the largest
    (the discarded siblings are real pavement — at KGYR a 26 000 m²
    junction piece vanished this way, leaving taxi rect ends in
    mid-air over covered source pavement).  Returns ``[poly]``
    unchanged when there is nothing to snap, ``[]`` when the snap
    degenerates."""
    if registry is None or poly is None or poly.is_empty:
        return [] if poly is None or poly.is_empty else [poly]

    def _snap_ring(coords):
        snapped = []
        for x, y in coords:
            cp = registry.get_or_add(float(x), float(y))
            snapped.append(cp)
        return snapped

    try:
        ext = _snap_ring(poly.exterior.coords)
        if len(set(ext)) < 3:
            return []
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
                return []
        if snapped_poly.geom_type == "MultiPolygon":
            return [g for g in snapped_poly.geoms
                    if g.geom_type == "Polygon" and not g.is_empty]
        return [snapped_poly]
    except _GEOM_EXC:
        return []


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
