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

import O4_UI_Utils as UI

from .config import JUNCTION_CENTERLINE_SPINE, SPINE_STEP_M
from .layout import BuiltShape, ROLE_APRON, ROLE_JUNCTION

_GEOM_EXC = (ValueError, GEOSException, TopologicalError)

__all__ = ["apply_junction_centerline_spine"]


# Lateral grade cap from the centerline out to a new arrangement vertex
# (a centerline×centerline crossing inside the junction) when assigning
# its altitude.
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
                        pav_union, runway_union):
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

    boundary = poly.exterior

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

    def _nearest_bvert(px, py):
        best = None
        for (vx, vy) in ropen:
            d = (vx - px) ** 2 + (vy - py) ** 2
            if best is None or d < best[0]:
                best = (d, (vx, vy))
        return best[1] if best else None

    def _on_pav(px, py):
        try:
            return pav_clip.distance(Point(px, py)) <= 0.1
        except _GEOM_EXC:
            return True

    # ── Slice lines: capped centerline polylines ──
    cut_lines: List[LineString] = []
    spine_map: Dict[Tuple[float, float], float] = {}
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
            # Interior spine nodes — evenly spaced, none ON the boundary.
            nseg = max(2, int(round(L / SPINE_STEP_M)))
            spine_xy: List[Tuple[float, float]] = []
            for i in range(1, nseg):
                d = i * L / nseg
                p = part.interpolate(d)
                if not _on_pav(p.x, p.y):
                    continue          # never a node in a runway / off-pav
                v, gap = (field.sample(p.x, p.y) if field is not None
                          else (None, float("inf")))
                if v is None:
                    v = _boundary_z_at(ropen, bz, p.x, p.y, tol=1e9)
                    if v is None:
                        v = sum(bz) / n
                spine_map[_key(p.x, p.y)] = float(v)
                spine_xy.append((p.x, p.y))
            if not spine_xy:
                continue
            # Cap each end to the nearest existing boundary vertex (the
            # rect corner) so the slice reaches the boundary at a shared
            # node — never a fresh node on the rect's flat edge.
            cap_a = _nearest_bvert(*part.coords[0])
            cap_b = _nearest_bvert(*part.coords[-1])
            slice_pts = []
            if cap_a is not None and cap_a != spine_xy[0]:
                slice_pts.append(cap_a)
            slice_pts.extend(spine_xy)
            if cap_b is not None and cap_b != spine_xy[-1]:
                slice_pts.append(cap_b)
            if len(slice_pts) >= 2:
                try:
                    cut_lines.append(LineString(slice_pts))
                except _GEOM_EXC:
                    continue
    if not spine_map:
        return None

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
    (slice model).  Mutates ``layout.shapes`` in place.  Returns the
    count of junctions re-partitioned."""
    if not JUNCTION_CENTERLINE_SPINE:
        return 0
    field = getattr(layout, "_network_profile_field", None)
    centerlines = _full_centerlines(layout)
    pav_union = getattr(layout, "_source_pav_union", None)
    runway_union = getattr(layout, "runway_union", None)

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
        pieces = (_partition_junction(s, crossing, field,
                                      pav_union, runway_union)
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
        # Conformance-heal: the slice caps reuse existing boundary
        # vertices, so the junction perimeter is unchanged and normally
        # stays conformant.  Run a light heal anyway (RECEIVERS = apron +
        # junction only — never a sloping rect, whose 4-corner form must
        # not gain an edge node) to weld any cap vertex shared with an
        # abutting apron/junction (collinear, altitude-neutral).
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
