"""Groundside (curbside / drop-off / parking) pavement emit.

Pulls roads tagged as airport-access or service highway out of the
OSM cache, lifts them off the DEM, and emits matching curbside
ribbon polygons.  Then prunes orphan junction polygons that are
fully contained inside (or touch only) the groundside ribbon —
those are road-island fragments that don't belong with airside
pavement.

Public API (leading-underscore preserved for backward compatibility
with internal callers in ``O4_Airport_Pavement_Builder``):

    _emit_groundside_pavement_dem
    _reclassify_groundside_orphan_junctions
"""
from __future__ import annotations

import math
import os as _os
from typing import Dict, List, Optional, Sequence, Set, Tuple

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon
from shapely.ops import linemerge, nearest_points, unary_union

from .layout import (
    AEROWAY_FOR_ROLE,
    BuiltShape,
    PavementLayout,
    R_EARTH,
    ROLE_APRON,
    ROLE_BOUNDARY,
    ROLE_CROSS_CONNECTOR,
    ROLE_GROUNDSIDE_PAVEMENT,
    ROLE_JUNCTION,
    ROLE_PRIMARY_PARALLEL,
    ROLE_RUNWAY,
    ROLE_SECONDARY_PARALLEL,
    ROLE_SERVICE_JUNCTION,
    ROLE_SERVICE_ROAD,
    ROLE_STUB,
    ROLE_BUILDING,
    ROLE_RETAINING_WALL,
    ROLE_TUNNEL_RAMP,
    SHARED_VERTEX_TOL_M,
)
from .pavement.vertices import _snap_polygon_vertices_to_rect_corners
from .elevation import _sample_dem, _resample_node_altitudes_nn
# Groundside ramp-grade cap (rise/run, user 2026-05-22) — single source of
# truth in ``config``; groundside follows the DEM but is graded to this
# cap so steep terrain becomes a navigable car/parking surface.
from .config import GROUNDSIDE_MAX_GRADE

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


__all__ = [
    "_absorb_apron_enclosed_groundside",
    "merge_small_apron_fragments",
    "_reclassify_groundside_orphan_junctions",
    "_emit_groundside_pavement_dem",
    "_separate_groundside_from_airside",
    "_merge_touching_groundside",
]


def _dem_sampler(layout, dem, tile_lat, tile_lon):
    """Return ``_dem_at(x, y) -> Optional[float]`` sampling ``dem`` in
    layout-metre space (anchored at ``layout.anchor``)."""
    lat0, lon0 = layout.anchor
    cos0 = math.cos(math.radians(lat0))
    R = R_EARTH

    def _dem_at(x: float, y: float) -> Optional[float]:
        try:
            lat = lat0 + math.degrees(y / R)
            lon = lon0 + math.degrees(x / (R * cos0))
            return _sample_dem(dem, tile_lat, tile_lon, lat, lon)
        except _GEOM_EXC:
            return None
    return _dem_at


# Douglas-Peucker tolerance for the groundside simplify pass (user
# 2026-05-22): drop over-resolved boundary detail (sub-meter apt.dat /
# DSF curve steps) before the densify+per-vertex-DEM emit, so groundside
# polygons don't carry needless node density into the patch.
GROUNDSIDE_SIMPLIFY_TOL_M = 2.0


def _grade_limit_ring(coords, alts, max_grade, iters=None):
    """Relax per-vertex altitudes so no adjacent ring edge exceeds
    ``max_grade`` (rise/run).  Each pass pulls the steeper end of a
    violating edge toward the other by half the excess; iterates to a
    ≤max_grade profile (ramp-like).  Modifies and returns ``alts``.

    A perturbation propagates ~one vertex per pass in each direction, so
    convergence needs O(n) passes — iters defaults to ``4*n`` so large
    curbside rings fully flatten to the cap."""
    n = len(coords)
    if n < 2 or len(alts) != n:
        return alts
    if iters is None:
        iters = max(300, 4 * n)
    for _ in range(iters):
        worst = 0.0
        for i in range(n):
            j = (i + 1) % n
            d = math.hypot(coords[j][0] - coords[i][0],
                           coords[j][1] - coords[i][1])
            if d < 1e-6:
                continue
            maxd = max_grade * d
            diff = alts[j] - alts[i]
            if abs(diff) > maxd:
                half = (abs(diff) - maxd) / 2.0
                worst = max(worst, abs(diff) - maxd)
                if diff > 0:
                    alts[j] -= half
                    alts[i] += half
                else:
                    alts[j] += half
                    alts[i] -= half
        if worst < 1e-3:
            break
    return alts


def _dem_follow_polygon(p, _dem_at, densify_step_m: float = 15.0,
                        simplify_tol: float = GROUNDSIDE_SIMPLIFY_TOL_M):
    """Densify ``p`` and sample the DEM at every vertex, returning
    ``(densified_polygon, node_altitudes)`` (node_altitudes closed with a
    repeated first value, matching the OSM emitter's convention) or
    ``None`` if it can't be built.

    Shared by ``_emit_groundside_pavement_dem`` and the groundside-orphan
    reclassify so both follow the DEM identically — a polygon that abuts
    DEM-following groundside stays flush with it (no cliff).
    """
    if p is None or p.is_empty or p.geom_type != "Polygon":
        return None
    # Simplify pass: drop over-resolved boundary detail before densifying
    # so the per-vertex-DEM emit carries fewer nodes.  Densify below
    # re-establishes uniform altitude sampling on the simplified ring.
    # The separation pass passes a SMALL tol (< its clearance) so this
    # only removes the sub-metre clip-boundary edges that would otherwise
    # inflate the per-vertex grade after 0.1 m altitude rounding — without
    # moving the boundary back across the clearance gap it just cut.
    if simplify_tol > 0:
        try:
            s = p.simplify(simplify_tol, preserve_topology=True)
            if s.geom_type == "Polygon" and not s.is_empty and s.is_valid:
                p = s
        except _GEOM_EXC:
            pass
    try:
        ring = list(p.exterior.coords)
    except _GEOM_EXC:
        return None
    if not ring:
        return None
    if ring[0] == ring[-1]:
        ring = ring[:-1]
    if len(ring) < 3:
        return None
    # Truncate needle-tip corners (interior angle below the Triangle4XP
    # sliver threshold) at SOURCE: the OSM emitter drops any polygon that
    # still carries one, and dropping a whole groundside shape uncovers
    # its entire source footprint (HECA: a 30-vertex strip lost to one
    # 1.62° tip → two interior coverage gaps).  Truncation loses only the
    # sub-50 m² wedge beyond the chord.
    from .pavement.junctions import _drop_sliver_corners
    ring = _drop_sliver_corners(ring)
    if len(ring) < 3:
        return None
    # Densify so per-vertex altitudes resolve well across long edges.
    densified: List[Tuple[float, float]] = []
    n_r = len(ring)
    for i in range(n_r):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n_r]
        densified.append((ax, ay))
        edge_len = math.hypot(bx - ax, by - ay)
        if edge_len <= densify_step_m:
            continue
        n_intermediate = int(edge_len // densify_step_m)
        for k in range(1, n_intermediate + 1):
            t = (k * densify_step_m) / edge_len
            if t >= 1.0:
                break
            densified.append((ax + (bx - ax) * t, ay + (by - ay) * t))
    if len(densified) < 3:
        return None
    # Sample DEM at every densified vertex; walk outward to the nearest
    # valid sample for any point that lands outside the DEM tile.
    alts: List[Optional[float]] = [_dem_at(x, y) for x, y in densified]
    if all(a is None for a in alts):
        return None
    for k, a in enumerate(alts):
        if a is not None:
            continue
        found: Optional[float] = None
        for off in range(1, len(alts)):
            left = (k - off) % len(alts)
            right = (k + off) % len(alts)
            if alts[left] is not None:
                found = alts[left]
                break
            if alts[right] is not None:
                found = alts[right]
                break
        assert found is not None, (
            "groundside: walk-outward DEM neighbour search failed despite "
            "precondition ensuring at least one valid sample")
        alts[k] = found
    # Rebuild from densified coords so the polygon and node_altitudes
    # stay 1-for-1; re-sample if buffer(0) validity repair changed the
    # vertex count.
    try:
        new_poly = Polygon(densified)
        if not new_poly.is_valid:
            new_poly = new_poly.buffer(0)
        if new_poly.geom_type != "Polygon" or new_poly.is_empty:
            return None
    except _GEOM_EXC:
        return None
    rebuilt = list(new_poly.exterior.coords)
    if rebuilt and rebuilt[0] == rebuilt[-1]:
        rebuilt = rebuilt[:-1]
    if len(rebuilt) != len(densified):
        alts = [(_dem_at(x, y) or 0.0) for x, y in rebuilt]
    else:
        alts = [float(a) for a in alts]
    # Grade-limit the DEM profile to GROUNDSIDE_MAX_GRADE (ramp-graded,
    # user 2026-05-22) before rounding.
    alts = _grade_limit_ring(rebuilt, alts, GROUNDSIDE_MAX_GRADE)
    alts = [round(float(a), 1) for a in alts]
    return new_poly, alts + [alts[0]]


def _grade_limit_groundside_chords(layout) -> int:
    """Pull every groundside shape's altitude field down to the largest
    ``GROUNDSIDE_MAX_GRADE``-Lipschitz field ≤ its current (DEM) values,
    measured over straight-line CHORD pairs — the within-shape validator
    metric.  ``_dem_follow_polygon``'s ring-ramp limit only bounds
    CONSECUTIVE ring vertices; a ring-compliant hillside piece still
    reads >4 % across its interior (HECA #230: 4.7-5.5 %).  Shared
    boundary nodes are UNIFIED across shapes (keyed by rounded coords)
    so abutting groundside pieces stay flush.  Runs ONCE, late, over
    ALL groundside shapes regardless of which pass created them.
    Returns the number of shapes whose altitudes changed."""
    node_alt: dict = {}
    rings: dict = {}
    for i, s in enumerate(layout.shapes):
        if s.role != ROLE_GROUNDSIDE_PAVEMENT:
            continue
        if (s.polygon is None or s.polygon.is_empty
                or s.polygon.geom_type != "Polygon"):
            continue
        if not s.node_altitudes:
            continue
        try:
            ring = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            continue
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        alts = list(s.node_altitudes)
        if len(alts) == len(ring) + 1:
            alts = alts[:-1]
        if len(alts) != len(ring) or len(ring) < 3:
            continue
        keys = [(round(x, 2), round(y, 2)) for x, y in ring]
        rings[i] = keys
        for kxy, a in zip(keys, alts):
            v = float(a)
            node_alt[kxy] = min(node_alt.get(kxy, v), v)
    if not rings:
        return 0
    for _sweep in range(4):
        changed = False
        for i, keys in rings.items():
            m = len(keys)
            for ai in range(m):
                xa, ya = keys[ai]
                best = node_alt[keys[ai]]
                for bj in range(m):
                    if bj == ai:
                        continue
                    xb, yb = keys[bj]
                    dd = math.hypot(xa - xb, ya - yb)
                    cap = node_alt[keys[bj]] + GROUNDSIDE_MAX_GRADE * dd
                    if cap < best:
                        best = cap
                if best < node_alt[keys[ai]] - 1e-6:
                    node_alt[keys[ai]] = best
                    changed = True
        if not changed:
            break
    n_changed = 0
    for i, keys in rings.items():
        s = layout.shapes[i]
        alts = [round(node_alt[k], 1) for k in keys]
        closed = alts + [alts[0]]
        if closed != list(s.node_altitudes):
            s.node_altitudes = closed
            n_changed += 1
    return n_changed


def _perimeter_frac_near(poly, region, radius_m: float = 1.5,
                         step_m: float = 1.5) -> float:
    """Fraction of ``poly``'s exterior perimeter lying within ``radius_m``
    of ``region`` (a Polygon/MultiPolygon, or None → 0.0).

    A genuine curbside strip faces a road / open terrain on its outer
    side (that perimeter is NOT near any apron), so its apron-bounded
    fraction is low.  An apron island wrongly carved out by a groundside
    strip is bounded by apron almost all the way around."""
    if region is None or getattr(region, "is_empty", True):
        return 0.0
    try:
        ring = list(poly.exterior.coords)
    except _GEOM_EXC:
        return 0.0
    near = 0.0
    total = 0.0
    for i in range(len(ring) - 1):
        ax, ay = ring[i]
        bx, by = ring[i + 1]
        seg = math.hypot(bx - ax, by - ay)
        if seg < 1e-6:
            continue
        n = max(1, int(seg // step_m))
        sub = seg / n
        for k in range(n):
            t = (k + 0.5) / n
            try:
                if region.distance(Point(ax + (bx - ax) * t,
                                         ay + (by - ay) * t)) <= radius_m:
                    near += sub
            except _GEOM_EXC:
                pass
            total += sub
    return (near / total) if total else 0.0


def _shape_repr_alt(s: "BuiltShape") -> Optional[float]:
    """One representative elevation for a shape, whichever altitude
    convention it carries (flat / sloped / per-vertex)."""
    if s.altitude is not None:
        return float(s.altitude)
    if s.altitude_high is not None and s.altitude_low is not None:
        return 0.5 * (float(s.altitude_high) + float(s.altitude_low))
    if s.node_altitudes:
        vals = [float(a) for a in s.node_altitudes if a is not None]
        if vals:
            return sum(vals) / len(vals)
    return None


# Apron-island / airside-wedged absorption (user 2026-06-03).  A piece is
# reclassified from groundside to flush ``apron`` when it has essentially
# NO open-terrain / road frontage (it is wedged inside the airside: an
# apron island, an apron-hugging clip residue, or a sliver between apron
# and the terminal) AND it touches at least some apron.  Genuine curbside
# faces a road / open terrain on its outer side, so its open fraction is
# well above the gate and it stays groundside.
#
# Measured on the EMITTED groundside shapes, AFTER ``_emit_..._dem`` has
# subtracted apron/terminal (so the perimeter reflects true adjacency) but
# BEFORE ``_separate_..._airside`` opens the 1 m clearance gap (so an
# absorbed piece is still flush with the apron, not 1 m off it).
_APRON_ISLAND_OPEN_MAX = 0.15    # max road/open frontage to still absorb
_APRON_ISLAND_APRON_MIN = 0.15   # must touch at least this much apron
# A qualifying piece whose perimeter is more than this fraction terminal-
# bordered is absorbed into the TERMINAL (flat building pad), not the apron
# (user 2026-06-03: "merge any apron inside a terminal with the terminal").
_APRON_ISLAND_TERM_MAJORITY = 0.5


def _best_bordering_shape(piece: "Polygon", shapes, radius_m: float):
    """The shape in ``shapes`` sharing the most boundary length with ``piece``
    (``None`` if none shares more than 1 m)."""
    pb = piece.buffer(radius_m)
    best = None
    best_share = 1.0
    for a in shapes:
        if a.polygon is None or a.polygon.is_empty:
            continue
        try:
            share = a.polygon.boundary.intersection(pb).length
        except _GEOM_EXC:
            continue
        if share > best_share:
            best_share = share
            best = a
    return best


def _clean_merge(merged):
    """Clean a merged polygon for emit: ``buffer(0)``, keep the largest part,
    and DROP needle/sliver corners — the thin-gap bridge can leave a near-zero-
    angle spike at the seam, and a sliver corner makes the X-Plane emit DROP the
    WHOLE shape (it dropped terminal1 / terminal9 at HECA).  Returns a valid
    ``Polygon`` whose corners clear ``SLIVER_ANGLE_THRESHOLD_DEG``, or ``None``
    (caller bails and the piece falls back) if it can't be made clean."""
    from .pavement.junctions import _drop_sliver_corners
    if merged is None or merged.is_empty:
        return None
    try:
        if not merged.is_valid:
            merged = merged.buffer(0)
        if merged.geom_type == "MultiPolygon":
            merged = max(merged.geoms, key=lambda g: g.area)
        if merged.geom_type != "Polygon" or merged.is_empty:
            return None
        ring = list(merged.exterior.coords)
        if ring and ring[0] == ring[-1]:
            ring = ring[:-1]
        ring = _drop_sliver_corners(ring)
        if len(ring) < 3:
            return None
        # Keep the merged polygon's interior rings — a bare
        # Polygon(ring) FILLED them, re-covering a terminal pad the
        # overlap-clip had carved out of the apron (KPHL terminal11,
        # 6,352 m² overlap; no clip pass runs after this absorb).
        from .junction_rules import _rebuild_ring_with_holes
        cleaned = _rebuild_ring_with_holes(ring, merged,
                                           normalize=False)
        if cleaned is None:
            return None
        if not cleaned.is_valid:
            cleaned = cleaned.buffer(0)
        if cleaned.geom_type != "Polygon" or cleaned.is_empty:
            return None
        return cleaned
    except _GEOM_EXC:
        return None



def _has_interior(g) -> bool:
    """True if ``g`` (Polygon / MultiPolygon) has any interior ring (hole)."""
    if g is None or g.is_empty:
        return False
    if g.geom_type == "Polygon":
        return len(list(g.interiors)) > 0
    if g.geom_type == "MultiPolygon":
        return any(len(list(p.interiors)) > 0 for p in g.geoms)
    return False


def merge_small_apron_fragments(layout: "PavementLayout",
                                radius_m: float = 1.5,
                                max_area_m2: float = 600.0) -> int:
    """PRE-SOLVE: fold a SMALL apron piece fully enclosed by apron/terminal into
    its larger neighbour (PURE GEOMETRY — runs before the elevation solver, so
    the merged shape's edges/elevation simply disappear and the solver grades
    the one unified apron; no post-solve step to reconcile).

    Only genuine slivers (< ``max_area_m2``) with NO taxi/runway/open frontage
    qualify, so real aprons (incl. neck-split pads) are left alone.  HOLE-SLICE
    SAFE: never fuses a union that would enclose a void, so the hole-router's
    intentional hole-opening cuts are preserved.  Returns the count merged."""
    aprons = [s for s in layout.shapes if s.role == ROLE_APRON
              and s.polygon is not None and not s.polygon.is_empty
              and s.polygon.geom_type == "Polygon"]
    if len(aprons) < 2:
        return 0
    try:
        other_union = unary_union([
            s.polygon for s in layout.shapes
            if s.role not in (ROLE_APRON, ROLE_BUILDING)
            and s.polygon is not None and not s.polygon.is_empty
            and s.polygon.geom_type in ("Polygon", "MultiPolygon")])
    except _GEOM_EXC:
        other_union = None
    try:
        road_union = unary_union([
            s.polygon for s in layout.shapes
            if s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION)
            and s.polygon is not None and not s.polygon.is_empty])
    except _GEOM_EXC:
        road_union = None
    n = 0
    for s in sorted(aprons, key=lambda a: a.polygon.area):   # smallest first
        p = s.polygon
        if p is None or p.is_empty or p.area >= max_area_m2:
            continue
        if other_union is not None and \
                _perimeter_frac_near(p, other_union, radius_m) > 0.05:
            continue                       # touches taxi/runway -> real apron
        hosts = [a for a in aprons if a is not s and a.polygon is not None
                 and not a.polygon.is_empty and a.polygon.area > p.area]
        host = _best_bordering_shape(p, hosts, radius_m)
        if host is None:
            continue
        try:
            merged = unary_union([host.polygon, p])
        except _GEOM_EXC:
            continue
        if _has_interior(merged):
            continue                       # would re-bury a void (hole slice)
        cleaned = _clean_merge(merged)
        if cleaned is None:
            continue
        # (s79) the sliver-corner drop can chord the merged ring ACROSS
        # a carved ROAD corridor (the KPHL terminal-incursion class with
        # a road instead of a pad — CYXY pav[1] ramp, 13.5 m² onto
        # SVC11): clip the merge result back off the road rects.
        if road_union is not None and not road_union.is_empty:
            try:
                clipped = cleaned.difference(road_union)
            except _GEOM_EXC:
                clipped = None
            if clipped is not None and clipped.geom_type == "Polygon" \
                    and not clipped.is_empty:
                cleaned = clipped
            elif clipped is not None \
                    and clipped.geom_type == "MultiPolygon":
                cleaned = max(clipped.geoms, key=lambda g: g.area)
        host.polygon = cleaned             # solver assigns node_altitudes later
        s.polygon = None
        n += 1
    if n:
        layout.shapes = [s for s in layout.shapes if s.polygon is not None]
    return n


def _merge_piece_into_apron(piece: "Polygon", apron, radius_m: float,
                            clip_against=None) -> bool:
    """Union ``piece`` into ``apron`` as ONE continuous, node-shared polygon and
    rebuild the apron's per-vertex altitudes: old vertices keep theirs, new
    (piece) vertices sample the apron's PRE-merge surface (``_edge_interp_alt``).
    Returns ``False`` (caller falls back) if the union isn't a clean single
    polygon.  ``clip_against`` (e.g. the terminal-pad union) is subtracted
    from the cleaned merge — ``_clean_merge``'s sliver-corner drop can chord
    a notch ACROSS a terminal edge (KPHL terminal22: a 0.4 m × 2.5 m
    incursion pocket), and no overlap-clip pass runs after this absorb."""
    from types import SimpleNamespace
    from .clearance import _edge_interp_alt
    before = apron.polygon
    before_na = list(apron.node_altitudes) if apron.node_altitudes else None
    try:
        merged = unary_union([before, piece])
        if merged is not None and merged.geom_type != "Polygon":
            # The piece touches the apron only at a POINT / is offset by a
            # sub-metre gap (near-coincident boundaries, not edge-shared), so
            # the plain union can't fuse them into one polygon (e.g. HECA
            # #2169 ↔ #305).  Bridge ONLY the thin gap between them — the
            # region within ``d`` of BOTH, in NEITHER — so the host apron's
            # other boundaries are left untouched (no morphological close that
            # would round corners / fill notches and desync shared edges).
            d = 0.6
            gap = (before.buffer(d).intersection(piece.buffer(d))
                   .difference(before).difference(piece))
            merged = unary_union([before, piece, gap])
    except _GEOM_EXC:
        return False
    merged = _clean_merge(merged)
    if merged is None:
        return False
    if clip_against is not None and not clip_against.is_empty:
        try:
            if merged.intersects(clip_against):
                clipped = merged.difference(clip_against)
                if clipped.geom_type == "MultiPolygon":
                    clipped = max(clipped.geoms, key=lambda g: g.area)
                if (clipped.geom_type == "Polygon"
                        and not clipped.is_empty):
                    merged = clipped
        except _GEOM_EXC:
            pass
    if before_na is None:
        # Flat apron: the union stays flat at the same altitude, nothing to
        # rebuild per-vertex.
        apron.polygon = merged
        return True
    old = list(before.exterior.coords)
    if old and old[0] == old[-1]:
        old = old[:-1]
    oldmap = {(round(x, 2), round(y, 2)): before_na[k]
              for k, (x, y) in enumerate(old) if k < len(before_na)}
    src = SimpleNamespace(node_altitudes=before_na, polygon=before)
    mean = sum(before_na) / len(before_na)
    new_ring = list(merged.exterior.coords)
    if new_ring and new_ring[0] == new_ring[-1]:
        new_ring = new_ring[:-1]
    new_na = []
    for (x, y) in new_ring:
        z = oldmap.get((round(x, 2), round(y, 2)))
        if z is None:
            try:
                z = _edge_interp_alt(src, x, y)
            except _GEOM_EXC:
                z = None
        new_na.append(z if z is not None else mean)
    apron.polygon = merged
    apron.node_altitudes = new_na
    apron.altitude = None
    apron.altitude_high = None
    apron.altitude_low = None
    return True


def _merge_piece_into_terminal(piece: "Polygon", terminal, radius_m: float,
                               clip_against=None) -> bool:
    """Union ``piece`` into ``terminal`` as one continuous polygon, kept FLAT at
    the terminal's level (building pads are flat).  Same thin-gap bridge as the
    apron merge for point-touching pieces.  ``False`` if the union isn't a clean
    single polygon.  ``clip_against`` (the apron union) is subtracted from the
    cleaned merge: the 0.6 m gap-bridge can lap onto an adjacent apron's
    footprint, and the apron-side absorb bridges the SAME thin gap from the
    other side — the two independently-bridged rings overlapped by a ~0.1 m²
    sliver at KPHL terminal22, with no overlap-clip pass running after."""
    before = terminal.polygon
    try:
        merged = unary_union([before, piece])
        if merged is not None and merged.geom_type != "Polygon":
            d = 0.6
            gap = (before.buffer(d).intersection(piece.buffer(d))
                   .difference(before).difference(piece))
            merged = unary_union([before, piece, gap])
    except _GEOM_EXC:
        return False
    merged = _clean_merge(merged)
    if merged is None:
        return False
    if clip_against is not None and not clip_against.is_empty:
        try:
            if merged.intersects(clip_against):
                clipped = merged.difference(clip_against)
                if clipped.geom_type == "MultiPolygon":
                    clipped = max(clipped.geoms, key=lambda g: g.area)
                if (clipped.geom_type == "Polygon"
                        and not clipped.is_empty):
                    merged = clipped
        except _GEOM_EXC:
            pass
    lvl = _shape_repr_alt(terminal)
    terminal.polygon = merged
    if lvl is not None:
        terminal.altitude = round(lvl, 1)
        terminal.node_altitudes = None
        terminal.altitude_high = None
        terminal.altitude_low = None
    return True


def _absorb_apron_enclosed_groundside(
        layout: "PavementLayout",
        radius_m: float = 1.5) -> int:
    """Reclassify emitted groundside shapes that sit wedged inside the
    airside — apron islands, apron-hugging clip residue, apron/terminal
    sandwich slivers — back into flush ``apron`` pavement at the
    neighbouring aprons' elevation, instead of leaving them as
    DEM-following groundside that would otherwise become a 1 m-gapped
    sliver after the separation pass.

    A piece qualifies when its perimeter has at most ``_APRON_ISLAND_
    OPEN_MAX`` open (road / terrain) frontage and touches at least
    ``_APRON_ISLAND_APRON_MIN`` apron.  Genuine curbside — which faces a
    road on its outer side — keeps a high open fraction and is left alone.

    Runs AFTER ``_emit_groundside_pavement_dem`` and BEFORE
    ``_separate_groundside_from_airside``.  Returns the number absorbed.
    """
    gs_shapes = [s for s in layout.shapes
                 if s.role == ROLE_GROUNDSIDE_PAVEMENT
                 and s.polygon is not None and not s.polygon.is_empty]
    if not gs_shapes:
        return 0
    apron_shapes = [s for s in layout.shapes
                    if s.role == ROLE_APRON
                    and s.polygon is not None and not s.polygon.is_empty]
    if not apron_shapes:
        return 0
    try:
        apron_union = unary_union([s.polygon for s in apron_shapes])
    except _GEOM_EXC:
        return 0
    terminal_shapes = [s for s in layout.shapes
                       if s.role == ROLE_BUILDING
                       and s.polygon is not None and not s.polygon.is_empty]
    term_union = None
    try:
        if terminal_shapes:
            term_union = unary_union([t.polygon for t in terminal_shapes])
    except _GEOM_EXC:
        term_union = None
    absorbed = 0
    # Running clip unions: a piece absorbed into the APRON earlier in
    # this loop is apron footprint the next TERMINAL merge must not lap
    # onto (and vice versa) — the start-of-pass unions don't contain
    # it, and both sides' 0.6 m gap-bridges can otherwise claim the
    # same thin gap (KPHL terminal22 ∩ apron, ~0.1 m² sliver).
    apron_clip = apron_union
    term_clip = term_union
    for s in gs_shapes:
        p = s.polygon
        apron_f = _perimeter_frac_near(p, apron_union, radius_m)
        term_f = _perimeter_frac_near(p, term_union, radius_m)
        open_f = max(0.0, 1.0 - apron_f - term_f)
        # Enclosed (no open road/terrain frontage) AND touches apron OR is
        # mostly terminal-surrounded.
        if not (open_f <= _APRON_ISLAND_OPEN_MAX
                and (apron_f >= _APRON_ISLAND_APRON_MIN
                     or term_f >= _APRON_ISLAND_TERM_MAJORITY)):
            continue
        # Majority-terminal perimeter -> absorb into the TERMINAL (flat pad).
        if term_f >= _APRON_ISLAND_TERM_MAJORITY and terminal_shapes:
            host_t = _best_bordering_shape(p, terminal_shapes, radius_m)
            if host_t is not None and _merge_piece_into_terminal(
                    p, host_t, radius_m, clip_against=apron_clip):
                s.polygon = None
                absorbed += 1
                try:
                    term_clip = unary_union(
                        [g for g in (term_clip, host_t.polygon)
                         if g is not None])
                except _GEOM_EXC:
                    pass
                continue
        # Clip to the terminal footprint so aircraft apron never intrudes
        # under the building; keep the largest surviving piece.
        q = p
        if term_union is not None:
            try:
                d = p.difference(term_union)
            except _GEOM_EXC:
                d = p
            if d is None or d.is_empty:
                q = None
            elif d.geom_type == "Polygon":
                q = d
            elif d.geom_type == "MultiPolygon":
                parts = [g for g in d.geoms
                         if g.geom_type == "Polygon" and not g.is_empty]
                q = max(parts, key=lambda g: g.area) if parts else None
        if q is None or q.area < _GROUNDSIDE_MIN_AREA_M2:
            # Entirely under the terminal / too small once clipped — drop
            # it (mark the source shape empty; it is removed below).
            s.polygon = None
            absorbed += 1
            continue
        # Double-source coverage (s70 Phoenix triage): when the emitted
        # aprons ALREADY cover essentially the whole piece — apt.dat
        # apron and OSM groundside both map the same pocket — merging or
        # re-tagging emits a duplicate on top of pavement that is
        # already there (KLUF apron#74∩island#106 5 752 m², KSDL
        # #72∩#152 104 m²).  The piece is fully redundant: drop it.
        # Terminal-wedged islands (HECA) keep >1 % outside the aprons
        # and are unaffected.
        try:
            if q.difference(apron_union).area <= max(1.0, 0.01 * q.area):
                s.polygon = None
                absorbed += 1
                continue
        except _GEOM_EXC:
            pass
        # (user 2026-06-03) Genuinely MERGE the piece into the apron it borders
        # most — one continuous, node-shared polygon — instead of leaving a
        # standalone flat "apron-island" whose coincident-but-unshared vertices
        # tear into cliffs when the apron surface moves.  This MERGE is
        # geometry-only (no altitude needed) and is the PRE-solve path: the
        # solver grades the unified apron, so the cliff is gone at the source.
        # The flush standalone re-tag below is the fallback when there is no
        # apron to merge into.
        host = _best_bordering_shape(q, apron_shapes, radius_m)
        if host is not None and _merge_piece_into_apron(
                q, host, radius_m, clip_against=term_clip):
            s.polygon = None
            absorbed += 1
            try:
                apron_clip = unary_union(
                    [g for g in (apron_clip, host.polygon)
                     if g is not None])
            except _GEOM_EXC:
                pass
            continue
        # Standalone re-tag fallback (no bordering apron to merge into): the
        # piece becomes a flush apron-island at the mean representative
        # altitude of the neighbouring aprons.  PRE-solve the aprons have no
        # altitude yet → leave the piece as groundside (it gets a DEM altitude
        # + the separation gap) rather than guessing a flat level.
        try:
            halo = p.buffer(radius_m + 0.5)
            neigh = [_shape_repr_alt(a) for a in apron_shapes
                     if a.polygon.intersects(halo)]
        except _GEOM_EXC:
            neigh = []
        neigh = [a for a in neigh if a is not None]
        if not neigh:
            neigh = [a for a in (_shape_repr_alt(a) for a in apron_shapes)
                     if a is not None]
        if not neigh:
            continue                # no usable altitude — leave as gs
        alt = round(sum(neigh) / len(neigh), 1)
        s.polygon = q
        s.role = ROLE_APRON
        s.ref = "apron-island"
        s.altitude = alt
        s.node_altitudes = None
        s.altitude_high = None
        s.altitude_low = None
        absorbed += 1
    if absorbed:
        layout.shapes = [s for s in layout.shapes if s.polygon is not None]
    return absorbed



def _emit_groundside_pavement_dem(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        densify_step_m: float = 15.0,
        terminal_gap_m: float = 0.1,
        ) -> int:
    """Emit each saved groundside pavement polygon as a DEM-following
    shape with per-vertex altitudes.

    Per user 2026-04-29: pavement that wraps around the GROUNDSIDE
    of a terminal building (curbside, drop-off, parking) sits at a
    different elevation than the airside apron — at CYXY the
    terminal is cut into the hill so the airside apron is several
    metres lower than the road frontage.  The earlier subtraction
    pass (see ``_terminal_groundside_zone``) keeps the airside
    pavement clean of these strips, but they still belong in the
    output: they should render at local DEM elevation and should
    NOT touch the terminal building footprint (a 0.1 m gap is
    already applied during capture).

    Implementation:
      1. Iterate ``layout._groundside_polys`` (captured during
         ``build_airport_pavement`` immediately before the
         groundside subtraction).
      2. Densify each polygon's exterior to ``densify_step_m`` so
         per-vertex altitudes resolve at the same spatial
         frequency as the boundary ribbon (15 m step → typical
         curbside has 5–10 vertices per side).
      3. Sample DEM at every vertex; emit as ``BuiltShape`` with
         role ``ROLE_GROUNDSIDE_PAVEMENT``, ``node_altitudes`` set,
         and ``altitude``/``altitude_high``/``altitude_low`` left
         None so the OSM emitter writes per-vertex altitude tags.

    Returns the number of polygons emitted.
    """
    from .pipeline import _load_osm_big_roads
    polys = list(getattr(layout, "_groundside_polys", []) or [])
    if not polys:
        return 0
    # Build a buffered union of every emitted terminal shape — we
    # subtract this from each groundside polygon so the result
    # leaves a ``terminal_gap_m`` clearance to every actual
    # terminal polygon in the final layout.  Using layout shapes
    # (not OSM source) handles cases where apt.dat row-110 /
    # DSF residue absorption produced a slightly different ring.
    _term_buf = None
    try:
        _t_polys = [s.polygon for s in layout.shapes
                    if s.role == ROLE_BUILDING
                    and s.polygon is not None
                    and not s.polygon.is_empty]
        if _t_polys:
            _term_buf = unary_union(
                [tp.buffer(terminal_gap_m) for tp in _t_polys])
            if _term_buf.is_empty:
                _term_buf = None
    except _GEOM_EXC:
        _term_buf = None
    # Also subtract every other pavement-bearing layout shape so
    # the groundside pavement never overlaps a rect / junction /
    # apron / runway / terminal / wall / ramp.  The boundary
    # ribbon is excluded — by design it traces over everything.
    NON_OVERLAP_ROLES = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
        ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_APRON, ROLE_JUNCTION,
        ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
    }
    _other_buf = None
    try:
        _other_polys = [s.polygon for s in layout.shapes
                        if s.role in NON_OVERLAP_ROLES
                        and s.polygon is not None
                        and not s.polygon.is_empty]
        if _other_polys:
            _other_buf = unary_union(_other_polys)
            if _other_buf.is_empty:
                _other_buf = None
    except _GEOM_EXC:
        _other_buf = None
    cuts = []
    if _term_buf is not None:
        cuts.append(_term_buf)
    if _other_buf is not None:
        cuts.append(_other_buf)
    if cuts:
        try:
            cut_union = unary_union(cuts) if len(cuts) > 1 else cuts[0]
        except _GEOM_EXC:
            cut_union = None
        if cut_union is not None and not cut_union.is_empty:
            clipped: List[Polygon] = []
            for p in polys:
                try:
                    q = p.difference(cut_union)
                except _GEOM_EXC:
                    continue
                if q is None or q.is_empty:
                    continue
                if q.geom_type == "Polygon":
                    if q.area >= 5.0:
                        clipped.append(q)
                elif q.geom_type == "MultiPolygon":
                    for g in q.geoms:
                        if (g.geom_type == "Polygon"
                                and not g.is_empty
                                and g.area >= 5.0):
                            clipped.append(g)
            polys = clipped
    if not polys:
        return 0
    # Two captured groundside polygons can themselves overlap (the cut
    # above only subtracts terminals / airside, not other groundside) —
    # leaving a self-overlap (HECA #2222 ∩ #2223, 0.1 m²).  Clip each
    # against the union of already-accepted polygons (largest first, so
    # the smaller piece yields) so groundside never overlaps groundside.
    polys.sort(key=lambda g: -g.area)
    _emitted_union = None
    deconflicted: List[Polygon] = []
    for p in polys:
        if _emitted_union is not None:
            try:
                q = p.difference(_emitted_union)
            except _GEOM_EXC:
                q = p
            if q is None or q.is_empty:
                continue
            if q.geom_type == "Polygon":
                p = q if q.area >= 5.0 else None
            elif q.geom_type == "MultiPolygon":
                pieces = [g for g in q.geoms
                          if g.geom_type == "Polygon" and g.area >= 5.0]
                p = max(pieces, key=lambda g: g.area) if pieces else None
            else:
                p = None
            if p is None:
                continue
        deconflicted.append(p)
        try:
            _emitted_union = (p if _emitted_union is None
                              else unary_union([_emitted_union, p]))
        except _GEOM_EXC:
            _emitted_union = p
    polys = deconflicted
    if not polys:
        return 0
    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    n_emitted = 0
    for p in polys:
        built = _dem_follow_polygon(p, _dem_at, densify_step_m)
        if built is None:
            continue
        new_poly, node_alts = built
        layout.shapes.append(BuiltShape(
            polygon=new_poly,
            role=ROLE_GROUNDSIDE_PAVEMENT,
            ref="groundside",
            node_altitudes=node_alts))
        n_emitted += 1
    return n_emitted


def _reclassify_groundside_orphan_junctions(
        layout: "PavementLayout",
        dem,
        tile_lat: int,
        tile_lon: int,
        vertex_match_tol_m: float = 0.5,
        ) -> int:
    """RECLASSIFY junction polygons that connect ONLY to groundside
    pavement (no path through shared vertices to any airside rect /
    runway / terminal) into DEM-following groundside pavement.

    Per user 2026-04-29 (CYXY -10111 + -10115): the rect/junction
    tessellator can leave junction polygons sitting next to a groundside
    polygon when the apt.dat row-110 / DSF union has pavement outside the
    groundside-zone subtraction's perpendicular extent.  Those junctions
    get the airside-flat altitude during the solver (they were classified
    airside even though they don't touch any airside pavement), then they
    share an edge with the DEM-following groundside polygon at an altitude
    mismatch — X-Plane renders that as a cliff.

    Earlier versions DROPPED these junctions.  That was wrong (user
    2026-05-21): pav_union is the source of truth and these junctions
    cover REAL pavement — at HECA the DSF adds large terminal aprons with
    no taxi centerline that are vertex-disconnected from the airside
    network, and dropping them erased ~44k m² of genuine apron.  Instead
    we KEEP the pavement and re-elevate it to follow the DEM (like the
    groundside ribbon it abuts), which both preserves coverage AND removes
    the cliff — the original goal.

    Detection rule (a junction is reclassified if BOTH):
        1. It shares ≥1 vertex with a ``ROLE_GROUNDSIDE_PAVEMENT``
           polygon.
        2. It does NOT share any vertex with an airside seed shape
           (runway / primary_parallel / secondary_parallel / stub /
           cross_connector / terminal), directly or transitively through
           other junction polygons (BFS over junction-junction shared
           vertices) — so genuine apron→runway/terminal connectors are
           left airside (SPJC primary_parallels U and M relied on this).

    Returns the number of junctions reclassified.
    """
    # APRON is airside (aircraft pavement), user 2026-05-22: a junction
    # abutting an apron is airside-connected, so it must NOT be
    # reclassified to groundside (groundside = cars/buildings).  Without
    # APRON here, large no-centerline terminal *aircraft* aprons were
    # reclassified to groundside and ended up sharing nodes/edges (and
    # overlapping) airside aprons — violating the no-shared-boundary
    # invariant.  With it, they stay airside (kept as junction → apron),
    # and only junctions touching ONLY groundside become groundside.
    AIRSIDE_SEED_ROLES = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL,
        ROLE_SECONDARY_PARALLEL, ROLE_STUB,
        ROLE_CROSS_CONNECTOR, ROLE_BUILDING, ROLE_APRON,
    }
    bucket_size = vertex_match_tol_m

    def _verts_buckets(s: "BuiltShape") -> List[Tuple[int, int]]:
        if s.polygon is None or s.polygon.is_empty:
            return []
        try:
            coords = list(s.polygon.exterior.coords)
        except _GEOM_EXC:
            return []
        if coords and coords[0] == coords[-1]:
            coords = coords[:-1]
        out = []
        for x, y in coords:
            out.append((int(round(x / bucket_size)),
                        int(round(y / bucket_size))))
        return out
    # Index every junction's vertex buckets (1-bucket halo so
    # near-misses still match neighbours).
    junction_idxs = [i for i, s in enumerate(layout.shapes)
                      if s.role == ROLE_JUNCTION
                      and s.polygon is not None
                      and not s.polygon.is_empty]
    if not junction_idxs:
        return 0
    junction_buckets: Dict[int, set] = {}
    bucket_to_jidx: Dict[Tuple[int, int], List[int]] = {}
    for ji in junction_idxs:
        bs = _verts_buckets(layout.shapes[ji])
        halo: set = set()
        for bx, by in bs:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    halo.add((bx + dx, by + dy))
        junction_buckets[ji] = halo
        for b in bs:
            bucket_to_jidx.setdefault(b, []).append(ji)
    # Airside seeds (rect / runway / terminal vertex buckets).
    seed_buckets: set = set()
    for s in layout.shapes:
        if s.role not in AIRSIDE_SEED_ROLES:
            continue
        for b in _verts_buckets(s):
            seed_buckets.add(b)
    # Build airside connectivity component over junctions: BFS
    # starting from junctions that share a bucket with any
    # airside seed, propagating through junction-junction
    # shared buckets.
    airside_set: set = set()
    for ji in junction_idxs:
        if junction_buckets[ji] & seed_buckets:
            airside_set.add(ji)
    queue = list(airside_set)
    while queue:
        ji = queue.pop()
        for b in junction_buckets[ji]:
            for kj in bucket_to_jidx.get(b, []):
                if kj in airside_set:
                    continue
                airside_set.add(kj)
                queue.append(kj)
    # Groundside vertex buckets (1-bucket halo).
    gs_buckets: set = set()
    for s in layout.shapes:
        if s.role != ROLE_GROUNDSIDE_PAVEMENT:
            continue
        for bx, by in _verts_buckets(s):
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    gs_buckets.add((bx + dx, by + dy))
    if not gs_buckets:
        return 0
    orphan_set: set = set()
    for ji in junction_idxs:
        if ji in airside_set:
            continue
        if junction_buckets[ji] & gs_buckets:
            orphan_set.add(ji)
    if not orphan_set:
        return 0
    # Re-elevate each orphan junction to follow the DEM and reclassify it
    # as groundside pavement — keep the pavement, lose the cliff.  If the
    # DEM-follow can't be built, LEAVE the shape unchanged (never erase
    # real pavement).
    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    n = 0
    for ji in orphan_set:
        s = layout.shapes[ji]
        built = _dem_follow_polygon(s.polygon, _dem_at)
        if built is None:
            continue
        new_poly, node_alts = built
        s.polygon = new_poly
        s.role = ROLE_GROUNDSIDE_PAVEMENT
        s.ref = "groundside"
        s.node_altitudes = node_alts
        n += 1
    return n


# Clearance (m) groundside pavement must keep from any terminal / airside
# polygon (user 2026-05-22): groundside is for cars/buildings and follows
# the DEM, so it sits at a different elevation than the graded airside and
# must NOT share a node or edge with it.  A clearance just over the
# shared-vertex snap tolerance guarantees separation (no shared node after
# snapping, no degenerate seam slivers in Triangle4XP).
GROUNDSIDE_CLEARANCE_M = SHARED_VERTEX_TOL_M + 0.5  # 1.0 m
_GROUNDSIDE_MIN_AREA_M2 = 5.0


def _merge_touching_groundside(
        layout: "PavementLayout", dem, tile_lat: int, tile_lon: int,
        touch_tol: float = 0.5, min_shared_m: float = 2.0) -> int:
    """Merge groundside pavement pieces that share a real boundary into ONE
    shape (user 2026-06-26).  Groundside is DEM-following pavement with no spine
    or internal structure, so two pieces sharing a ≥``min_shared_m`` boundary were
    SPLIT upstream (junction-emit ``pav_union.difference(rects)`` / overlap-clip on
    a multi-polygon source union) — they should be a single surface (CYXY parking
    lot @(-465,408): two pieces 899+1276 m² touching along a 55 m seam).  Pieces
    that merely touch at a point are left alone (no ``min_shared_m`` seam).
    """
    if _os.environ.get("O4_MERGE_GROUNDSIDE", "1") != "1":
        return 0
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    gs = [s for s in layout.shapes
          if s.role == ROLE_GROUNDSIDE_PAVEMENT and s.polygon is not None
          and not s.polygon.is_empty and s.polygon.geom_type == "Polygon"]
    if len(gs) < 2:
        return 0
    polys = [s.polygon for s in gs]
    n = len(gs)
    parent = list(range(n))

    def _find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tree = STRtree(polys)
    for i in range(n):
        try:
            cand = tree.query(polys[i].buffer(touch_tol))
        except _GEOM_EXC:
            continue
        for qj in cand:
            j = int(qj)
            if j <= i:
                continue
            try:
                if polys[i].distance(polys[j]) > touch_tol:
                    continue
                shared = polys[i].exterior.intersection(polys[j].exterior)
                if getattr(shared, "length", 0.0) < min_shared_m:
                    continue            # point/sliver touch — not a split seam
            except _GEOM_EXC:
                continue
            ri, rj = _find(i), _find(j)
            if ri != rj:
                parent[ri] = rj

    groups: dict = {}
    for i in range(n):
        groups.setdefault(_find(i), []).append(i)

    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    merged_objs: set = set()
    new_shapes: list = []
    n_merged = 0
    for idxs in groups.values():
        if len(idxs) < 2:
            continue
        try:
            u = unary_union([polys[k] for k in idxs])
        except _GEOM_EXC:
            continue
        if u.is_empty:
            continue
        pieces = ([u] if u.geom_type == "Polygon"
                  else [g for g in getattr(u, "geoms", []) if g.geom_type == "Polygon"])
        if not pieces:
            continue
        for k in idxs:
            merged_objs.add(id(gs[k]))
        for p in pieces:
            built = _dem_follow_polygon(p, _dem_at, simplify_tol=0.0)
            if built is None:
                continue
            np_, na = built
            new_shapes.append(BuiltShape(
                polygon=np_, role=ROLE_GROUNDSIDE_PAVEMENT,
                ref="groundside", node_altitudes=na))
        n_merged += len(idxs) - 1
    if not merged_objs:
        return 0
    layout.shapes = [s for s in layout.shapes
                     if id(s) not in merged_objs] + new_shapes
    return n_merged


def _separate_groundside_from_airside(
        layout: "PavementLayout", dem, tile_lat: int, tile_lon: int,
        clearance: float = GROUNDSIDE_CLEARANCE_M) -> int:
    """Clip every groundside polygon so it keeps ``clearance`` from all
    terminal / airside pavement — enforcing the invariant that groundside
    shares no node or edge with terminal or airside (it is separate
    car/building pavement at DEM elevation).  Re-derives DEM + grade-
    limited altitudes for the clipped result.  Returns shapes clipped.

    Robust to the non-conformance case the apron-seed rule can't catch:
    a groundside polygon that *overlaps* airside without sharing a vertex
    is still cut back to the clearance gap.
    """
    AIRSIDE_ROLES = {
        ROLE_RUNWAY, ROLE_PRIMARY_PARALLEL, ROLE_SECONDARY_PARALLEL,
        ROLE_STUB, ROLE_CROSS_CONNECTOR, ROLE_APRON, ROLE_JUNCTION,
        ROLE_BUILDING, ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
    }
    # Groundside MAY share an edge with a SERVICE ROAD / junction (user
    # 2026-06-26): a parking lot is SERVED by its service road, so they touch —
    # opening the 1 m clearance gap there DISCONNECTS the road from the lot it
    # feeds (CYXY SVC1 ↔ lot @(-472,404): a cliff across the gap).  Groundside is
    # still cut back from BUILDINGS (kept above) and aircraft pavement.  Off =
    # legacy (clearance from roads too).
    _share_svc = _os.environ.get("O4_GROUNDSIDE_SHARE_SVC", "1") == "1"
    if not _share_svc:
        AIRSIDE_ROLES |= {ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION}
    clip_polys = []
    for s in layout.shapes:
        if s.role in AIRSIDE_ROLES and s.polygon is not None \
                and not s.polygon.is_empty:
            try:
                # Mitre join: the buffered boundary is straight-edged
                # (no rounded-corner arc segments), so the clip cut
                # doesn't introduce sub-metre edges that would inflate the
                # per-vertex grade after altitude rounding.
                clip_polys.append(s.polygon.buffer(clearance, join_style=2))
            except _GEOM_EXC:
                continue
    # Groundside may TOUCH a service road (shared edge, kept above) but must not
    # OVERLAP it (area overlap = self-overlap, not a shared edge — e.g. a curved
    # SVC connector emitted as service_junction that straddles the lot it feeds).
    # Add the service polys at ZERO clearance so an overlap is trimmed while the
    # touching edge survives (no disconnecting gap).
    if _share_svc:
        for s in layout.shapes:
            if s.role in (ROLE_SERVICE_ROAD, ROLE_SERVICE_JUNCTION) \
                    and s.polygon is not None and not s.polygon.is_empty:
                clip_polys.append(s.polygon)
    if not clip_polys:
        return 0
    try:
        clip = unary_union(clip_polys)
    except _GEOM_EXC:
        return 0
    if clip is None or clip.is_empty:
        return 0
    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    out_shapes = []
    n_clipped = 0
    for s in layout.shapes:
        if s.role != ROLE_GROUNDSIDE_PAVEMENT or s.polygon is None \
                or s.polygon.is_empty:
            out_shapes.append(s)
            continue
        try:
            diff = s.polygon.difference(clip)
        except _GEOM_EXC:
            out_shapes.append(s)
            continue
        if diff.is_empty:
            n_clipped += 1            # entirely inside the gap → drop
            continue
        parts = ([diff] if diff.geom_type == "Polygon"
                 else list(getattr(diff, "geoms", [])))
        changed = False
        kept = []
        for part in parts:
            if part.geom_type != "Polygon" or part.is_empty \
                    or part.area < _GROUNDSIDE_MIN_AREA_M2:
                changed = True
                continue
            if part.equals(s.polygon):
                kept.append(s)        # untouched
                continue
            # No re-simplify: the source groundside was already 2 m-
            # simplified at emit, and re-simplifying would move the
            # boundary back across the clearance gap.  The mitre-buffered
            # clip above already yields clean straight edges.
            built = _dem_follow_polygon(part, _dem_at, simplify_tol=0.0)
            if built is None:
                continue
            np_, na = built
            kept.append(BuiltShape(
                polygon=np_, role=ROLE_GROUNDSIDE_PAVEMENT,
                ref="groundside", node_altitudes=na))
            changed = True
        out_shapes.extend(kept)
        if changed:
            n_clipped += 1
    layout.shapes = out_shapes
    return n_clipped


def _deconflict_groundside_overlaps(
        layout: "PavementLayout", dem, tile_lat: int, tile_lon: int,
        min_overlap_m2: float = 0.5) -> int:
    """Clip overlapping groundside-vs-groundside pavement so no two
    groundside polygons share interior area.

    ``_separate_groundside_from_airside`` removes groundside↔airside
    overlap but never groundside↔groundside — an orphan junction
    reclassified to groundside, or two independently DEM-followed
    pieces, can overlap each other (LMML: piece #238 covered #185 by
    32.7 m² and #182 by 3.0 m²).  Larger pieces are canonical; each
    smaller piece YIELDS the overlap (subtract the running union of the
    already-kept larger pieces), is rebuilt with DEM altitudes, and
    sub-minimum remnants are dropped.  Pieces that merely ABUT a larger
    one are untouched (difference of a touching polygon is a no-op).

    Returns the number of groundside shapes modified or dropped."""
    gs = [(i, s) for i, s in enumerate(layout.shapes)
          if s.role == ROLE_GROUNDSIDE_PAVEMENT
          and s.polygon is not None and not s.polygon.is_empty]
    if len(gs) < 2:
        return 0
    # Largest-first, deterministic index tie-break.
    order = sorted(gs, key=lambda t: (-t[1].polygon.area, t[0]))
    _dem_at = _dem_sampler(layout, dem, tile_lat, tile_lon)
    kept_union = None
    replace: Dict[int, list] = {}   # original idx → [BuiltShape, …] ([] = drop)
    n_mod = 0
    for i, s in order:
        poly = s.polygon
        if kept_union is not None and not kept_union.is_empty:
            try:
                overlap = poly.intersection(kept_union).area
            except _GEOM_EXC:
                overlap = 0.0
            if overlap > min_overlap_m2:
                try:
                    diff = poly.difference(kept_union)
                except _GEOM_EXC:
                    diff = None
                parts = ([] if diff is None or diff.is_empty
                         else [diff] if diff.geom_type == "Polygon"
                         else list(getattr(diff, "geoms", [])))
                new_pieces = []
                for part in parts:
                    if (part.geom_type != "Polygon" or part.is_empty
                            or part.area < _GROUNDSIDE_MIN_AREA_M2):
                        continue
                    built = _dem_follow_polygon(part, _dem_at,
                                                simplify_tol=0.0)
                    if built is None:
                        continue
                    np_, na = built
                    new_pieces.append(BuiltShape(
                        polygon=np_, role=ROLE_GROUNDSIDE_PAVEMENT,
                        ref="groundside", node_altitudes=na))
                replace[i] = new_pieces
                n_mod += 1
                try:
                    poly = (unary_union([p.polygon for p in new_pieces])
                            if new_pieces else None)
                except _GEOM_EXC:
                    poly = None
        if poly is not None and not poly.is_empty:
            try:
                kept_union = (poly if kept_union is None
                              else unary_union([kept_union, poly]))
            except _GEOM_EXC:
                pass
    if not replace:
        return 0
    out = []
    for i, s in enumerate(layout.shapes):
        if i in replace:
            out.extend(replace[i])
        else:
            out.append(s)
    layout.shapes = out
    return n_mod


