"""Emit-time 3D-collinear vertex decimation (user design 2026-07-03).

Node density should follow the SOLVED profile, not a fixed step: an interior
ring vertex that lies on the straight line between its kept neighbours in XY
AND in Z is lossless to remove — X-Plane interpolates linearly along the
segment, so the rendered surface is identical while the patch, the Ortho4XP
triangulation and the sim mesh all shrink.  This recovers the rect-era
economy (a flat straight = ONE segment, zero interior nodes) on the sliced
model, while vertical transitions keep exactly the nodes that carry their
curvature (those sit off the 3D line and are never dropped) and junctions /
arcs keep their density (XY deflection protects them).

Measured before building this (SPJC, step-24 patch): 39 % of airside ring
vertices are 3D-collinear at a 2 cm band.

CONFORMANCE BY CONSTRUCTION: a vertex is removed only if EVERY ring that
contains it (across all shapes, exteriors and holes, processed or not) agrees
it is removable — so a shared-edge chain drops the same nodes on both sides
and no T-vertices are minted.  The LAW can only improve: the grade of the
pair between two kept neighbours is the length-weighted mean of the removed
sub-segments' grades, and removed vertices only remove already-satisfied
pairs.

Gate ``O4_EMIT_DECIMATE`` (default on).  Z tolerances: airside 0.02 m; the
BOUNDARY ribbon 0.10 m — its per-station altitudes carry raw DEM jitter, and
the DEM under an airport is 3-arc-second SRTM (~90 m posts, metres of noise)
smoothed over ~700 m, so a 10 cm band is far below the data's own noise
floor.
"""
from __future__ import annotations

import math
import os

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import Polygon

_GEOM_EXC = (GEOSException, TopologicalError, ValueError, AttributeError)

# Max perpendicular XY deviation of a removed vertex from the kept chord.
XY_TOL_M = 0.02
# Max |z - z_interpolated| of a removed vertex against the kept chord.
Z_TOL_AIRSIDE_M = 0.02
Z_TOL_BOUNDARY_M = 0.10

# Roles whose exterior rings are decimated (everything else only VOTES KEEP
# through shared vertices).  Buildings/terminals are excluded — pads are
# small and their footprint fidelity is the point.
_AIRSIDE_ROLES = frozenset({
    "apron", "junction", "service_junction", "runway", "runway_crossing",
    "groundside_pavement",
})
_BOUNDARY_ROLES = frozenset({"boundary"})

# Vertex identity key across shapes (post-weld shared vertices are
# coordinate-identical to well below a millimetre).
def _key(x: float, y: float) -> tuple[int, int]:
    return (int(round(x * 1000.0)), int(round(y * 1000.0)))


def _ring_and_alts(shape):
    """Open exterior ring + per-vertex altitude list aligned to it (or None),
    plus whether the shape stored the CLOSED (dup-last) altitude convention."""
    coords = list(shape.polygon.exterior.coords)
    closed = len(coords) > 1 and coords[0] == coords[-1]
    ring = coords[:-1] if closed else coords
    alts = getattr(shape, "node_altitudes", None)
    if alts is None:
        return ring, None, False
    if len(alts) == len(ring):
        return ring, list(alts), False
    if len(alts) == len(ring) + 1:
        return ring, list(alts[:-1]), True
    return ring, None, False        # unknown alignment: XY-only test


def _span_ok(ring, alts, i, j, n, z_tol):
    """True iff every vertex strictly between ring[i] and ring[j] (circular)
    lies within XY_TOL_M of the chord AND (when alts exist) within ``z_tol``
    of the linearly interpolated altitude along it."""
    ax, ay = ring[i % n]
    bx, by = ring[j % n]
    cx, cy = bx - ax, by - ay
    cl = math.hypot(cx, cy)
    if cl < 1e-9:
        return False
    k = (i + 1) % n
    while k != j % n:
        px, py = ring[k]
        t = ((px - ax) * cx + (py - ay) * cy) / (cl * cl)
        if t < -1e-9 or t > 1.0 + 1e-9:
            return False
        fx, fy = ax + t * cx, ay + t * cy
        if math.hypot(px - fx, py - fy) > XY_TOL_M:
            return False
        if alts is not None:
            za, zb, zp = alts[i % n], alts[j % n], alts[k]
            if za is None or zb is None or zp is None:
                return False
            if abs(zp - (za * (1.0 - t) + zb * t)) > z_tol:
                return False
        k = (k + 1) % n
    return True


def _ring_keep_set(ring, alts, z_tol, forced=None):
    """Indices to KEEP.  Anchors = locally bent vertices (plus ``forced``);
    each anchor-to-anchor span is split recursively until every removed
    vertex fits the chord within tolerance (Douglas-Peucker with the law's
    absolute band)."""
    n = len(ring)
    if n < 5:
        return set(range(n))
    anchors = set(forced or ())
    for k in range(n):
        if not _span_ok(ring, alts, (k - 1) % n, (k + 1) % n, n, z_tol):
            anchors.add(k)
    if len(anchors) < 3:
        return set(range(n))
    keep = set(anchors)
    order = sorted(anchors)
    for a_pos in range(len(order)):
        i = order[a_pos]
        j = order[(a_pos + 1) % len(order)]
        stack = [(i, j)]
        while stack:
            (u, v) = stack.pop()
            span = (v - u) % n
            if span <= 1:
                continue
            if _span_ok(ring, alts, u, v, n, z_tol):
                continue        # whole span drops
            # split at the intermediate farthest (XY) from the chord
            ax, ay = ring[u % n]
            bx, by = ring[v % n]
            cx, cy = bx - ax, by - ay
            cl2 = max(cx * cx + cy * cy, 1e-12)
            best_k, best_d = None, -1.0
            k = (u + 1) % n
            while k != v % n:
                px, py = ring[k]
                t = ((px - ax) * cx + (py - ay) * cy) / cl2
                t = min(max(t, 0.0), 1.0)
                d = math.hypot(px - (ax + t * cx), py - (ay + t * cy))
                if alts is not None and alts[u % n] is not None \
                        and alts[v % n] is not None and alts[k] is not None:
                    dz = abs(alts[k] - (alts[u % n] * (1.0 - t)
                                        + alts[v % n] * t))
                    # weight Z deviation into the split choice at the band
                    # ratio so a pure-Z bend still becomes the split point
                    d = max(d, dz * (XY_TOL_M / max(z_tol, 1e-9)))
                if d > best_d:
                    best_d, best_k = d, k
                k = (k + 1) % n
            if best_k is None:
                continue
            keep.add(best_k)
            stack.append((u, best_k))
            stack.append((best_k, v))
    return keep


def decimate_emit_nodes(layout, icao: str = "") -> int:
    """Remove 3D-collinear ring vertices across the layout (see module doc).
    Mutates shape polygons + node_altitudes in place.  Returns count removed."""
    if os.environ.get("O4_EMIT_DECIMATE", "1") != "1":
        return 0

    shapes = [s for s in getattr(layout, "shapes", [])
              if s.polygon is not None and not s.polygon.is_empty
              and s.polygon.geom_type == "Polygon"]

    # membership: every occurrence of a coordinate in ANY ring (exterior +
    # holes, all roles) — a vertex may only vanish if every occurrence votes.
    membership: dict = {}
    for s in shapes:
        try:
            rings = [list(s.polygon.exterior.coords)[:-1]] + \
                    [list(r.coords)[:-1] for r in s.polygon.interiors]
        except _GEOM_EXC:
            continue
        for ring in rings:
            for (x, y) in ring:
                k = _key(x, y)
                membership[k] = membership.get(k, 0) + 1

    def _z_tol_for(s):
        role = getattr(s, "role", "") or ""
        if role in _AIRSIDE_ROLES:
            return Z_TOL_AIRSIDE_M
        if role in _BOUNDARY_ROLES:
            return Z_TOL_BOUNDARY_M
        return None

    # TILE-SEAM vertices are cross-tile anchors: the adjacent tile's patch
    # keeps its own seam nodes, so removing ours would mint cross-tile
    # T-vertices no in-layout vote can see.  Force-keep any vertex whose
    # lat/lon sits on an integer tile line (same test as check_grade's
    # seam detection).
    _m_to_ll = getattr(layout, "m_to_ll", None)

    def _on_seam(x, y):
        if _m_to_ll is None:
            return False
        try:
            la, lo = _m_to_ll(x, y)
        except _GEOM_EXC:
            return False
        return (abs(la - round(la)) < 1e-6 or abs(lo - round(lo)) < 1e-6)

    # round 1: per-ring drop votes
    votes: dict = {}
    prepared = []
    for s in shapes:
        z_tol = _z_tol_for(s)
        if z_tol is None:
            continue
        try:
            ring, alts, closed_alts = _ring_and_alts(s)
        except _GEOM_EXC:
            continue
        if len(ring) < 5:
            continue
        seam_idx = {i for i, (x, y) in enumerate(ring) if _on_seam(x, y)}
        keep = _ring_keep_set(ring, alts, z_tol, forced=seam_idx)
        keep |= seam_idx
        prepared.append((s, ring, alts, closed_alts, z_tol, seam_idx))
        for idx, (x, y) in enumerate(ring):
            if idx not in keep:
                k = _key(x, y)
                votes[k] = votes.get(k, 0) + 1

    removable = {k for k, v in votes.items() if v == membership.get(k, -1)}
    if not removable:
        return 0

    # round 2: rebuild rings dropping only globally-removable vertices; the
    # keep-set recursion runs again with the global keeps FORCED so every
    # dropped vertex is re-verified against its FINAL kept chord.
    removed = 0
    for (s, ring, alts, closed_alts, z_tol, seam_idx) in prepared:
        n = len(ring)
        forced = {i for i, (x, y) in enumerate(ring)
                  if _key(x, y) not in removable} | seam_idx
        if len(forced) == n:
            continue
        keep = _ring_keep_set(ring, alts, z_tol, forced=forced)
        keep |= forced
        if len(keep) == n or len(keep) < 3:
            continue
        order = sorted(keep)
        new_ring = [ring[i] for i in order]
        new_alts = ([alts[i] for i in order] if alts is not None else None)
        try:
            new_poly = Polygon(new_ring, [list(r.coords)
                                          for r in s.polygon.interiors])
            if not new_poly.is_valid or new_poly.is_empty:
                continue
        except _GEOM_EXC:
            continue
        s.polygon = new_poly
        if new_alts is not None:
            s.node_altitudes = (new_alts + [new_alts[0]] if closed_alts
                                else new_alts)
        removed += n - len(keep)

    if removed:
        try:
            import O4_UI_Utils as UI
            UI.vprint(1,
                f"  [pav-builder] {icao}: emit decimation — removed "
                f"{removed} 3D-collinear ring vertex(es) "
                f"(airside ±{Z_TOL_AIRSIDE_M} m, boundary "
                f"±{Z_TOL_BOUNDARY_M} m).")
        except Exception:
            pass
    return removed
