"""Apron decomposition (session 47).

Splits each ``ROLE_APRON`` polygon into sub-shapes that each carry one
grade rule on REAL geometry, so the elevation solver and ``check_grade``
enforce the rule directly instead of hoping a boundary vertex lands in
the right place:

* **stand pads** — ramp-start (apt.dat 1300/1301) zones intersected with
  the apron → ``ROLE_STAND`` (1.0% all-direction).
* **lane corridors** — apt.dat taxilane centerlines (1202) buffered to
  taxiway width, intersected with the apron → ``ROLE_APRON`` thin shapes
  whose vertices follow the lane, so the per-axis treatment grades them
  directionally.
* **apron body** — the remainder → ``ROLE_APRON`` (1.5% all-direction).

This also de-blobs the giant single aprons (e.g. CYXY's 153k m²) whose
interiors otherwise carry no vertices at all.  The cut lines are explicit
apt.dat geometry, so the split is deterministic (unlike morphological
de-blobbing).
"""
from __future__ import annotations

import O4_UI_Utils as UI

from shapely.errors import GEOSException, TopologicalError
from shapely.ops import unary_union

from ..layout import BuiltShape, ROLE_APRON, ROLE_STAND
from ..canonical_points import snap_polygon_through_registry

_GEOM_EXC = (ValueError, TypeError, GEOSException, TopologicalError, IndexError)

# ICAO taxiway PAVEMENT widths (m) by code letter (A 7.5 … F 25).
_LANE_WIDTH_BY_LETTER = {
    "A": 7.5, "B": 10.5, "C": 15.0, "D": 18.0, "E": 23.0, "F": 25.0,
}
_DEFAULT_LANE_WIDTH_M = 18.0

# Minimum sub-shape areas (drop slivers / negligible carves).
_MIN_STAND_AREA_M2 = 60.0
_MIN_CORRIDOR_AREA_M2 = 40.0
_MIN_BODY_AREA_M2 = 25.0


def _lane_half_width(letter) -> float:
    if letter and letter.upper() in _LANE_WIDTH_BY_LETTER:
        return 0.5 * _LANE_WIDTH_BY_LETTER[letter.upper()]
    return 0.5 * _DEFAULT_LANE_WIDTH_M


def _polys(geom):
    if geom is None or geom.is_empty:
        return []
    t = geom.geom_type
    if t == "Polygon":
        return [geom]
    if t == "MultiPolygon":
        return [g for g in geom.geoms if not g.is_empty]
    if t == "GeometryCollection":
        return [g for g in geom.geoms
                if g.geom_type == "Polygon" and not g.is_empty]
    return []


def _clean(poly):
    try:
        if not poly.is_valid:
            poly = poly.buffer(0)
    except _GEOM_EXC:
        return None
    if poly is None or poly.is_empty or poly.geom_type != "Polygon":
        return None
    return poly


def decompose_aprons(layout, *, icao: str = "") -> int:
    """Replace each ``ROLE_APRON`` shape with stand / corridor / body
    sub-shapes.  Returns the net change in shape count.  No-op for aprons
    with no crossing stand zone or taxilane (kept whole)."""
    centerlines = list(getattr(layout, "apt_taxi_centerlines", []) or [])
    zones = list(getattr(layout, "apt_stand_zones", []) or [])
    letters = dict(getattr(layout, "apt_taxi_letters", {}) or {})
    if not centerlines and not zones:
        return 0

    out: list = []
    n_stand = 0
    n_corr = 0
    n_before_aprons = 0
    n_after_pieces = 0

    for s in layout.shapes:
        if s.role != ROLE_APRON or s.polygon is None or s.polygon.is_empty:
            out.append(s)
            continue
        poly = s.polygon
        n_before_aprons += 1

        # ── Stand pads ── (zones are oriented aircraft rectangles)
        stand_parts = []
        for sp in zones:
            try:
                if sp is None or sp.is_empty or not poly.intersects(sp):
                    continue
                inter = poly.intersection(sp)
            except _GEOM_EXC:
                continue
            for p in _polys(inter):
                if p.area >= _MIN_STAND_AREA_M2:
                    stand_parts.append(p)
        stand_u = None
        if stand_parts:
            try:
                stand_u = unary_union(stand_parts)
            except _GEOM_EXC:
                stand_u = None

        # ── Lane corridors ──
        corr_parts = []
        for ln, name in centerlines:
            if ln is None or ln.is_empty:
                continue
            try:
                if not poly.intersects(ln):
                    continue
                hw = _lane_half_width(letters.get(name))
                corr = poly.intersection(ln.buffer(hw, cap_style=2))
            except _GEOM_EXC:
                continue
            for p in _polys(corr):
                if p.area >= _MIN_CORRIDOR_AREA_M2:
                    corr_parts.append(p)
        corr_u = None
        if corr_parts:
            try:
                corr_u = unary_union(corr_parts)
                if stand_u is not None:
                    corr_u = corr_u.difference(stand_u)   # stand wins
            except _GEOM_EXC:
                corr_u = None

        if stand_u is None and corr_u is None:
            out.append(s)            # nothing to carve — keep whole
            n_after_pieces += 1
            continue

        # ── Body = apron − stands − corridors ──
        body = poly
        try:
            if stand_u is not None:
                body = body.difference(stand_u)
            if corr_u is not None:
                body = body.difference(corr_u)
        except _GEOM_EXC:
            out.append(s)
            n_after_pieces += 1
            continue

        # Route each carved piece's vertices through the layout's
        # canonical-point registry so the new carve-boundary vertices
        # resolve to the SAME coordinates as the adjacent junction / apron
        # / rect vertices already registered there.  Without this the
        # carve introduces vertices ~0.3-0.5 m off a neighbour's node,
        # they solve as separate nodes, and emit a >1 m cross-shape cliff
        # at the shared corner (CYXY upper-terrace apron↔junction).
        reg = getattr(layout, "canonical_points", None)

        def _finalize(p, min_area):
            cp = _clean(p)
            if cp is None or cp.area < min_area:
                return None
            snapped = snap_polygon_through_registry(cp, reg)
            if snapped is not None and not snapped.is_empty \
                    and snapped.geom_type == "Polygon" \
                    and snapped.area >= min_area:
                return snapped
            return cp

        emitted = []
        for p in _polys(stand_u):
            cp = _finalize(p, _MIN_STAND_AREA_M2)
            if cp is not None:
                emitted.append(BuiltShape(polygon=cp, role=ROLE_STAND,
                                          ref=s.ref))
                n_stand += 1
        for p in _polys(corr_u):
            cp = _finalize(p, _MIN_CORRIDOR_AREA_M2)
            if cp is not None:
                emitted.append(BuiltShape(polygon=cp, role=ROLE_APRON,
                                          ref=s.ref))
                n_corr += 1
        for p in _polys(body):
            cp = _finalize(p, _MIN_BODY_AREA_M2)
            if cp is not None:
                emitted.append(BuiltShape(polygon=cp, role=ROLE_APRON,
                                          ref=s.ref))

        if emitted:
            out.extend(emitted)
            n_after_pieces += len(emitted)
        else:
            out.append(s)            # degenerate carve — keep original
            n_after_pieces += 1

    layout.shapes = out
    if n_stand or n_corr:
        UI.vprint(1,
            f"  [pav-builder] {icao}: apron decomposition — {n_before_aprons} "
            f"apron(s) → {n_after_pieces} piece(s) ({n_stand} stand pad(s), "
            f"{n_corr} lane corridor(s)).")
    return n_after_pieces - n_before_aprons
