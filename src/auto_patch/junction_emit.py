"""Junction-polygon emission + pre-elevation geometry finalize.

Run AFTER all rect / terminal / runway shapes are emitted by
``pipeline.build_airport_pavement``.  Computes the residue
(apt.dat row-110 pavement minus rect / terminal / runway coverage),
decomposes it into junction polygons that share boundary vertices
with their rect / terminal / runway neighbours, then enforces the
"no overlap" + "shared vertex exact" invariants across the whole
layout.

Public API:
    emit_junctions(
        layout, *, pav_union, emitted_taxi_rects, terminal_union,
        taxi_rects, icao)
"""
from __future__ import annotations

import math

import O4_UI_Utils as UI
from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .config import EMIT_JUNCTIONS
from .layout import BuiltShape, ROLE_JUNCTION
from .pavement.junctions import _decompose_polygon_with_holes
from .canonical_points import snap_polygon_parts_through_registry


# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


__all__ = ["emit_junctions"]


def _drop_orphan_strips(pieces, fixed_shape_polys, min_other_perim_m=10.0):
    """Drop residue pieces whose perimeter is dominated by ONE rect /
    runway / terminal edge with no significant connection to any
    other fixed shape or apt.dat pavement boundary segment.

    Per user 2026-05-05: "Any strips left along a rect edge that
    only touch one rect, and no other part of pav_union should just
    be deleted."

    Heuristic: for each piece, sum the boundary length shared with
    each fixed shape's boundary.  If only ONE fixed shape contributes
    a meaningful share AND the remainder of the boundary is shorter
    than ``min_other_perim_m``, the piece is an orphan strip alongside
    that single rect — drop it.  Pieces with ≥2 fixed-shape contacts
    OR substantial open boundary (apt.dat pav) are kept.
    """
    if not fixed_shape_polys:
        return pieces
    out = []
    n_dropped = 0
    for p in pieces:
        if p.geom_type != "Polygon" or p.is_empty:
            out.append(p)
            continue
        shared_lens: list[float] = []
        total_shared = 0.0
        for fs in fixed_shape_polys:
            try:
                inter = p.boundary.intersection(fs.boundary)
            except _GEOM_EXC:
                continue
            if inter.is_empty:
                continue
            L = getattr(inter, "length", 0.0)
            if L > 0.5:
                shared_lens.append(L)
                total_shared += L
        try:
            total_perim = p.boundary.length
        except _GEOM_EXC:
            total_perim = 0.0
        non_shared = max(0.0, total_perim - total_shared)
        if (len(shared_lens) <= 1
                and non_shared < min_other_perim_m
                and shared_lens):
            n_dropped += 1
            continue
        out.append(p)
    if n_dropped:
        UI.vprint(1,
            f"  [pav-builder] dropped {n_dropped} orphan strip(s) "
            f"alongside a single fixed shape.")
    return out


def emit_junctions(layout, *, pav_union, emitted_taxi_rects,
                   terminal_union, taxi_rects, icao):
    """Emit junction polygons via simple residue subtraction.

    (Renamed from ``emit_junctions_and_finalize`` 2026-06-02: this function
    only EMITS junctions — the finalize/repair passes it once implied now run
    downstream in ``finalize.compute_elevations_and_repair_geometry``, so the
    emit↔finalize seam is the gap between the two pipeline calls.)

    Per user 2026-05-05 (minimal-emit approach): junctions are simply
    the connected components of ``pav_union − rects − terminals −
    runway`` after dropping orphan strips and cleaning sliver
    corners.  No seam injection, no shared-vertex clustering, no
    apron-interior decomposition heuristics — those passes
    accumulated state-dependent failure modes (self-touching rings
    after vertex clustering, dropped junctions at overlap-clip).
    Vertex sharing between adjacent shapes happens automatically
    because the residue boundary inherits exact rect / runway /
    terminal boundary segments from the subtraction operation, and
    ``to_osm`` buckets near-identical coords to the same node ID.

    Mutates layout in place.
    """
    # NOTE: this is the FULL source pavement union (apt.dat + DSF),
    # not apt-only — named accordingly. (Pipeline keeps a separate
    # apt-only ``apt_pav_union`` for the DSF-overlay gate.)
    layout._source_pav_union = pav_union
    # ── Junction emission (minimal): residue = pav_union − rects −
    # terminals − runway.  Each connected piece becomes one
    # junction.  Holes inside a piece are decomposed via
    # ``_decompose_polygon_with_holes``.  Sliver corners (interior
    # angle < SLIVER_ANGLE_THRESHOLD_DEG) are dropped from each
    # piece's exterior so the OSM-emit guard doesn't reject the
    # whole polygon.
    MIN_JUNCTION_AREA_M2 = 50.0
    MIN_HOLE_AREA_M2 = 100.0
    if pav_union is None:
        return

    # Per user 2026-05-05: NO buffers here.  Want exact match with
    # pavement.  Plain ``pav_union − rects − terminals − runway``.
    #
    # Alignment-by-construction:
    #   * pav_union is already simplified at construction (1 m
    #     tolerance, see pipeline.py).
    #   * Taxi rects' corners are snapped to pav_union.boundary at
    #     build time, so their edges coincide with pav_union edges
    #     by construction — re-simplifying them here would shift
    #     corners off pav_union and create thin sliver triangles
    #     in the residue.
    #   * Runway is authoritative apt.dat row-100 geometry — leave
    #     it alone.
    #   * Terminals are OSM building footprints with no relation to
    #     pav_union; simplify here so over-resolved building
    #     boundaries don't carry sub-meter noise into the residue.
    taxi_rect_union = (unary_union(emitted_taxi_rects)
                       if emitted_taxi_rects else None)
    cleaned_term_union = terminal_union
    if cleaned_term_union is not None and not cleaned_term_union.is_empty:
        from .pavement.union_helpers import _simplify_pavement_polygon
        cleaned_term_union = _simplify_pavement_polygon(
            cleaned_term_union, tol=1.0)
    effective_runway_union = getattr(layout, "_effective_runway_union",
                       layout.runway_union)

    residue = pav_union
    if taxi_rect_union is not None:
        residue = residue.difference(taxi_rect_union)
    if cleaned_term_union is not None and not cleaned_term_union.is_empty:
        residue = residue.difference(cleaned_term_union)
    if effective_runway_union is not None and not effective_runway_union.is_empty:
        residue = residue.difference(effective_runway_union)

    # Per user 2026-05-13 (CYXY missing-junctions bug): shapely's
    # difference can return a GeometryCollection when residue
    # boundary touches the subtracted geometries at points/edges
    # (producing dangling LineStrings alongside the Polygon pieces).
    # Treating GeometryCollection as a single non-Polygon piece used
    # to drop EVERY junction polygon — at CYXY all 31 residue
    # Polygons were silently filtered out, leaving only the 2
    # runway-crossing junctions.  Walk both MultiPolygon AND
    # GeometryCollection geoms to recover the Polygon members.
    if residue.geom_type == "MultiPolygon":
        raw_pieces = list(residue.geoms)
    elif residue.geom_type == "GeometryCollection":
        raw_pieces = list(residue.geoms)
    else:
        raw_pieces = [residue]
    pieces = [p for p in raw_pieces
              if p.geom_type == "Polygon"
              and not p.is_empty
              and p.area >= MIN_JUNCTION_AREA_M2]

    # Drop orphan strips: residue pieces alongside a single rect
    # with no other connection.  Per user 2026-05-05.
    fixed_polys = list(emitted_taxi_rects or [])
    if cleaned_term_union is not None and not cleaned_term_union.is_empty:
        if cleaned_term_union.geom_type == "MultiPolygon":
            fixed_polys.extend(cleaned_term_union.geoms)
        else:
            fixed_polys.append(cleaned_term_union)
    if effective_runway_union is not None and not effective_runway_union.is_empty:
        if effective_runway_union.geom_type == "MultiPolygon":
            fixed_polys.extend(effective_runway_union.geoms)
        else:
            fixed_polys.append(effective_runway_union)
    pieces = _drop_orphan_strips(pieces, fixed_polys)

    if EMIT_JUNCTIONS:
        from .junction_rules import longest_runway_axis_deg
        from .pavement.junctions import _drop_sliver_corners
        _runway_axis_deg = longest_runway_axis_deg(layout)
        # GLOBAL shared node set for the conforming-cuts hole router: the
        # canonical registry (apt.dat row-110 verts + runway + rect corners
        # registered at build time) plus every fixed-shape perimeter vertex.
        # The router admits the subset sitting ON a residue piece's boundary
        # as cut endpoints, so cuts end at the SAME node an abutting shape
        # already owns (conforming — no divergent wedge).
        _global_nodes: list = []
        _reg = getattr(layout, "canonical_points", None)
        if _reg is not None:
            try:
                _global_nodes.extend(_reg.points())
            except _GEOM_EXC:
                pass
        for _fp in fixed_polys:
            try:
                _coords = list(_fp.exterior.coords)
            except _GEOM_EXC:
                continue
            if _coords and _coords[0] == _coords[-1]:
                _coords = _coords[:-1]
            _global_nodes.extend(_coords)
        # (session 51) Apron neck-split was MOVED out of junction_emit and
        # into pipeline.py at the end of the geometry phase (just before the
        # solve).  Reason: at junction-emit time many of the mouth-pair
        # vertices that USER-identified necks need haven't been inserted
        # yet — they come from later stitches/snaps/conformance.  Running
        # neck-split at geometry-final catches the full vertex set.
        for part in pieces:
            # Decompose pieces with interior holes (shapely.difference
            # leaves them when a hole is fully interior to the residue).
            sub_pieces = (
                _decompose_polygon_with_holes(
                    part, min_area_m2=MIN_JUNCTION_AREA_M2,
                    runway_axis_deg=_runway_axis_deg,
                    corner_snap_pts=_global_nodes)
                if any(Polygon(h).area >= MIN_HOLE_AREA_M2
                       for h in part.interiors)
                else [part])
            for sp in sub_pieces:
                if sp.geom_type != "Polygon" or sp.is_empty:
                    continue
                # Drop sliver corners (interior angle below
                # SLIVER_ANGLE_THRESHOLD_DEG).  Without this the OSM
                # emit guard would drop the entire polygon over one
                # near-degenerate corner.
                ring = list(sp.exterior.coords)
                if ring and ring[0] == ring[-1]:
                    ring = ring[:-1]
                ring = _drop_sliver_corners(ring)
                if len(ring) < 3:
                    continue
                # Keep EVERY Polygon part of the validity repair, not
                # just the largest — a pinched residue ring splits into
                # siblings that are all real pavement; keep-largest
                # silently uncovered 26 000 m² at KGYR / 3 800 m² at
                # KCHD, leaving taxi rect ends in mid-air (short-edge
                # verify warnings) over covered source pavement.
                try:
                    cleaned = Polygon(ring)
                    if not cleaned.is_valid:
                        cleaned = cleaned.buffer(0)
                    if cleaned.geom_type == "MultiPolygon":
                        parts = [g for g in cleaned.geoms
                                 if g.geom_type == "Polygon"
                                 and not g.is_empty]
                    else:
                        parts = [cleaned]
                except _GEOM_EXC:
                    continue
                for part in parts:
                    if (part.geom_type != "Polygon"
                            or part.is_empty
                            or part.area < MIN_JUNCTION_AREA_M2):
                        continue
                    # Route every perimeter vertex through the canonical
                    # registry so any drift introduced by ``buffer(0)``
                    # validity repair (or by the upstream
                    # difference / decomposition) resolves to the same
                    # canonical (x, y) as the adjacent rect / runway /
                    # row-110 source point.  Per user 2026-05-18.
                    # Parts-preserving: the snap itself can pinch the
                    # ring apart; every split piece is kept.
                    for snapped in snap_polygon_parts_through_registry(
                            part, getattr(layout, "canonical_points",
                                          None)):
                        if (snapped is None or snapped.is_empty
                                or snapped.area < MIN_JUNCTION_AREA_M2):
                            continue
                        layout.shapes.append(BuiltShape(
                            polygon=snapped, role=ROLE_JUNCTION))
