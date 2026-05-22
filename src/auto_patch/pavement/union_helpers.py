"""Polygon-union helpers shared across the pavement pipeline.

Single-purpose: bridge sub-meter precision gaps between adjacent
apt.dat row-110 polygons that ``shapely.unary_union`` leaves
disjoint.  See ``_merge_near_touching`` for the rule.

Public API:
    _merge_near_touching(geom)
    PAVEMENT_BRIDGE_GAP_M
"""
from __future__ import annotations

import math

from shapely.errors import GEOSException, TopologicalError
from shapely.geometry import Polygon
from shapely.ops import unary_union

# Narrow exception tuple for shapely / numeric-geometry failure
# modes.  Programming errors propagate so they surface immediately.
_GEOM_EXC = (ValueError, GEOSException, TopologicalError)


__all__ = [
    "PAVEMENT_BRIDGE_GAP_M",
    "_merge_near_touching",
    "_simplify_pavement_polygon",
    "_drop_sliver_holes",
    "_trim_sliver_spurs",
    "_close_open_clean",
    # Backwards-compat alias.
    "_drop_close_nonadjacent_pairs",
]


# Sliver-hole removal threshold.  When apt.dat and DSF pavement share a
# boundary, their slightly-different vertex placements leave hairline
# gaps that ``unary_union`` records as thin interior rings.  These are
# seam residue, not real non-pavement.  The discriminator is the
# polygon's EFFECTIVE WIDTH ``2·area / perimeter`` (mean strip width),
# NOT area, elongation, or the bounding-rectangle short side: seam
# slivers are often tapering/slanted wedges whose min-rotated-rectangle
# over-reports the width (e.g. a HECA sliver measured 2.1 m by MRR but
# is really ~1 m of mean width).  Measured at HECA, the metric is bimodal
# with a clean empty band at 1.5–2.0 m: every seam sliver is ≤ 1.21 m,
# every genuine grass infield is ≥ 2.08 m.  ``2A/P`` also naturally
# catches LONG thin slivers (big area, hairline width).
SLIVER_HOLE_MAX_WIDTH_M = 1.5


def _hole_width_m(ring) -> float:
    """Effective (mean) width of a ring in meters, ``2·area /
    perimeter``.  For a long strip this is its width; for a tapering
    wedge it is the mean width.  Returns ``inf`` if it can't be
    measured, so callers treat it as "not a sliver"."""
    try:
        rp = Polygon(ring)
        per = rp.length
        if per <= 0:
            return float("inf")
        return 2.0 * rp.area / per
    except _GEOM_EXC:
        return float("inf")


def _drop_sliver_holes(geom, max_width: float = SLIVER_HOLE_MAX_WIDTH_M):
    """Drop hairline seam-residue interior rings — those whose
    effective width ``2·area/perimeter`` is below ``max_width``.  Real
    grass infields (mean width ≥ ~2 m) are always kept, regardless of
    how small or elongated.  Returns the same kind of geometry; falls
    back to the input on any failure.
    """
    if geom is None or geom.is_empty:
        return geom
    if geom.geom_type == "MultiPolygon":
        return type(geom)([
            _drop_sliver_holes(g, max_width) for g in geom.geoms])
    if geom.geom_type != "Polygon" or not geom.interiors:
        return geom
    try:
        keep = [ring for ring in geom.interiors
                if _hole_width_m(ring) >= max_width]
        if len(keep) == len(geom.interiors):
            return geom
        out = Polygon(geom.exterior, keep)
        if not out.is_valid:
            out = out.buffer(0)
            if out.geom_type == "MultiPolygon":
                out = max(out.geoms, key=lambda g: g.area)
        if (out.is_valid and not out.is_empty
                and out.geom_type == "Polygon"):
            return out
    except _GEOM_EXC:
        pass
    return geom


# Max effective width of an EXTERIOR seam spur to trim.  The seam
# between apt.dat and DSF pavement leaves not only thin interior gaps
# (handled by ``_drop_sliver_holes``) but also thin exterior LIPS
# where one source's boundary pokes a fraction of a metre past the
# other's.  Same metric and threshold as the hole filter.
SLIVER_SPUR_MAX_WIDTH_M = 1.5


def _trim_sliver_spurs(geom, max_width: float = SLIVER_SPUR_MAX_WIDTH_M):
    """Trim thin exterior protrusions ("seam lips") from the outer
    boundary — the exterior analogue of ``_drop_sliver_holes``.

    A mitre-join morphological OPEN (erode then dilate by
    ``max_width/2``) removes protrusions narrower than ``max_width``
    while PRESERVING real corners (mitre doesn't round them, so convex
    corners don't become spurious protrusion pieces).  The difference
    ``geom − opened`` is the set of protrusions; only the genuinely
    thin ones (effective width ``2·area/perimeter`` < ``max_width``)
    are subtracted, so real boundary detail and smooth bezier curves
    are untouched.  Returns the same kind of geometry; falls back to
    the input on any failure.
    """
    if geom is None or geom.is_empty:
        return geom
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        return geom
    try:
        probe = max_width / 2.0
        opened = geom.buffer(-probe, join_style=2).buffer(
            probe, join_style=2)
        if (opened.is_empty
                or opened.geom_type not in ("Polygon", "MultiPolygon")):
            return geom
        protrusions = geom.difference(opened)
        if protrusions.is_empty:
            return geom
        parts = ([protrusions] if protrusions.geom_type == "Polygon"
                 else list(getattr(protrusions, "geoms", [])))
        thin = []
        for g in parts:
            if g.geom_type != "Polygon" or g.is_empty:
                continue
            per = g.length
            if per > 0 and (2.0 * g.area / per) < max_width:
                thin.append(g)
        if not thin:
            return geom
        out = geom.difference(unary_union(thin))
        if (not out.is_empty
                and out.geom_type in ("Polygon", "MultiPolygon")):
            return out
    except _GEOM_EXC:
        pass
    return geom


# Effective width below which a morphological close-then-open removes
# seam residue.  Same threshold as the (now-superseded) hole/spur
# filters: HECA's metric is bimodal with a clean gap at 1.5–2.0 m.
SLIVER_CLOSE_OPEN_M = 1.5


def _close_open_clean(geom, width: float = SLIVER_CLOSE_OPEN_M):
    """Mitre-join morphological CLOSE-then-OPEN: in one fused buffer
    sequence, fill thin interior holes / seam gaps AND trim thin
    exterior spurs ("seam lips") narrower than ``width`` — with zero
    net displacement.

    ``buffer(+w)`` closes (dilate: bridges gaps, swallows < ``w`` holes);
    the fused ``buffer(-2w)`` finishes the close (erode back to size) and
    begins the open (erode away < ``w`` protrusions); ``buffer(+w)``
    restores the original size.  Net effect is the union of a
    morphological close and open.  Mitre joins (``join_style=2``)
    preserve sharp real corners and smooth bezier curves instead of
    rounding them, so only sub-``w`` seam residue is affected.

    This REPLACES the separate ``_drop_sliver_holes`` + ``_trim_sliver_
    spurs`` seam cleanup (user 2026-05-21): one op handles both sides of
    the apt.dat ⁄ DSF seam, no net area change.  Returns the same kind of
    geometry; falls back to the input on any failure.
    """
    if geom is None or geom.is_empty:
        return geom
    if geom.geom_type not in ("Polygon", "MultiPolygon"):
        return geom
    try:
        out = (geom.buffer(width, join_style=2)
                   .buffer(-2.0 * width, join_style=2)
                   .buffer(width, join_style=2))
        if (not out.is_empty
                and out.geom_type in ("Polygon", "MultiPolygon")):
            return out
    except _GEOM_EXC:
        return geom
    return geom


# Tolerance for bridging numerical / sub-meter gaps between near-
# touching apt.dat polygons.  apt.dat at busy airports often stores
# adjacent apron areas as separate row-110 polygons whose shared
# edges have sub-millimeter-to-cm floating-point differences (e.g.
# at SPJC's SE apron a 6.5 m thin "strip" appears between two large
# apron polygons because their shared boundary y-values differ by
# 5 cm).  unary_union doesn't merge these because they don't
# overlap — but for our purposes they ARE one continuous coverage.
# Closing 0.5 m gaps via buffer-shrink merges them while preserving
# real holes (typically meters-wide non-pavement islands).
PAVEMENT_BRIDGE_GAP_M = 0.1


def _simplify_pavement_polygon(geom, tol: float = 1.0):
    """Simplify a pavement polygon: drop sub-``tol`` detail and snip
    sliver-tip corners.

    Per user 2026-05-05: pavement should never have nodes closer than
    1 m, and X-Plane's mesh builder crashes on sub-``SLIVER_ANGLE_
    THRESHOLD_DEG`` (2°) corner spikes.  Apt.dat polygons routinely
    contain both: over-resolved curves stored as 100s of sub-meter
    steps, and 1° needle-tip features that look like real pavement
    tabs but were just floating-point doubled vertices in the
    source data.

    Two passes:

    1. **Douglas-Peucker simplify** (shapely's ``.simplify(tol,
       preserve_topology=True)``).  Drops verts whose perpendicular
       distance to the simplified edge is < ``tol``.  Eliminates
       over-resolution and most close-pair noise.  Preserves
       polygon topology.

    2. **Sliver-tip removal.**  After simplify, any remaining
       vertex with interior angle < ``SLIVER_ANGLE_THRESHOLD_DEG``
       is snipped; the two flanking vertices are joined directly,
       collapsing the needle into a chord.  Iterates so a freshly
       exposed sliver after one drop gets caught on the next pass.

    Returns the simplified polygon (Polygon or MultiPolygon, same
    type as input).  Falls back to the input on any failure.
    """
    if geom is None or geom.is_empty:
        return geom
    if geom.geom_type == "MultiPolygon":
        return type(geom)([_simplify_pavement_polygon(g, tol)
                            for g in geom.geoms])
    if geom.geom_type != "Polygon":
        return geom
    try:
        # Pass 1: DP simplify.
        simp = geom.simplify(tol, preserve_topology=True)
        if (simp.is_empty
                or simp.geom_type not in ("Polygon", "MultiPolygon")):
            return geom
        if simp.geom_type == "MultiPolygon":
            # Topology preservation can split the polygon if a
            # narrow neck collapses; keep the largest piece.
            simp = max(simp.geoms, key=lambda g: g.area)
        # Pass 2: drop sliver-tip corners (re-imported here to avoid
        # circular imports at module load time).
        from .junctions import _drop_sliver_corners
        from shapely.geometry import Polygon as _P

        def _clean_ring(ring_coords):
            coords = list(ring_coords)
            if coords and coords[0] == coords[-1]:
                coords = coords[:-1]
            cleaned = _drop_sliver_corners(coords)
            if len(cleaned) < 3:
                return None
            cleaned.append(cleaned[0])
            return cleaned

        ext = _clean_ring(simp.exterior.coords)
        if ext is None:
            return geom
        ints = []
        for ring in simp.interiors:
            cr = _clean_ring(ring.coords)
            if cr is not None and len(cr) >= 4:
                ints.append(cr)
        out = _P(ext, ints)
        if not out.is_valid:
            out = out.buffer(0)
            if out.geom_type == "MultiPolygon":
                out = max(out.geoms, key=lambda g: g.area)
        if (out.is_valid
                and not out.is_empty
                and out.geom_type == "Polygon"):
            return out
    except _GEOM_EXC:
        pass
    return geom


# Backwards-compatibility alias.  Previous incarnations of this
# helper had narrower behaviour (just non-adjacent close-pair
# removal); the new function does that and more.  Existing call
# sites can use either name.
_drop_close_nonadjacent_pairs = _simplify_pavement_polygon


def _merge_near_touching(geom: Polygon | None,
                         eps: float = PAVEMENT_BRIDGE_GAP_M
                         ) -> Polygon | None:
    """Force-merge near-touching components of ``geom`` (a possibly
    MultiPolygon) by a buffer-then-shrink.  Returns the same kind
    of geometry (Polygon if single, MultiPolygon if truly disjoint).

    Per user 2026-04-27: we want a single pavement union with holes
    only — apt.dat polygon boundaries shouldn't survive into the
    output.  This helper bridges sub-meter precision gaps between
    apt.dat polygons that ``unary_union`` leaves disjoint.
    """
    if geom is None or geom.is_empty:
        return geom
    try:
        merged = geom.buffer(eps, join_style=2).buffer(
            -eps, join_style=2)
        if merged.is_empty:
            return geom
        if merged.geom_type not in ("Polygon", "MultiPolygon"):
            return geom
        return merged
    except _GEOM_EXC:
        return geom
