"""Synthesize a SPINE (taxi route) through junctions that have none.

User model (2026-06-26): the whole junction body is not "spine", but EVERY
junction must have a SPINE pass through it — because there must be a route to
every part of airside pavement, even where apt.dat defines no explicit taxi
route.  Without a spine a junction's grade is "body" (off-route) and slips the
spine gate (CYXY junction #88: TX2 drops 4.2 m in 41 m, invisible).

This pass runs PRE-SLICE (before ``junction_spine.apply_junction_centerline_
spine``): for each junction with NO crossing taxi centerline it finds the
junction's **mouths** — the edges it shares with a TAXI-NETWORK neighbour (a
sloping taxi rect, a runway, or a junction that already has a spine; NOT an
apron, which is a destination) — and synthesizes a route:

  * 2 mouths → a straight segment mouth → mouth (a corridor through),
  * 3+ mouths → a STAR: each mouth → the junction centroid.

The synthetic centerlines are appended to ``layout.apt_taxi_centerlines`` (with a
``~SJ`` ref + the connecting taxiway's letter), so the existing slice pass cuts
them in and the grade graph treats them as spine at the taxi cap.
"""
from __future__ import annotations

import math
import os as _os

import O4_UI_Utils as UI

from .apt_dat_reader import TaxiCenterline
from .layout import ROLE_JUNCTION, ROLE_RUNWAY, taxi_shape_code_letter

__all__ = ["synthesize_junction_spines"]

_TOUCH_M = 1.5          # a neighbour this close shares a boundary (a mouth)
_ON_EDGE_M = 1.0        # a ring-edge midpoint this close to the neighbour edge
_MERGE_M = 6.0          # merge mouths closer than this (one shared boundary)


def _open(poly):
    cs = list(poly.exterior.coords)
    return cs[:-1] if len(cs) > 1 and cs[0] == cs[-1] else cs


def _has_spine(layout, s, ctx):
    from .grade_graph import GradeShape, shape_constraints
    ring = _open(s.polygon)
    if len(ring) < 3:
        return True                         # degenerate → leave alone
    gs = GradeShape(role=s.role, ring=[(x, y) for (x, y) in ring],
                    keys=list(range(len(ring))))
    sc = shape_constraints(gs, ctx)
    return any(len(ch) >= 2 for ch in sc.spine_chains)


def synthesize_junction_spines(layout, icao: str = "") -> int:
    """Append synthetic centerlines for spineless junctions.  Returns the count
    of junctions given a synthetic spine."""
    from shapely.geometry import LineString, Point
    from .grade_graph import build_context
    from .junction_rules import SLOPING_RECT_ROLES

    junctions = [s for s in layout.shapes
                 if s.role == ROLE_JUNCTION and s.polygon is not None
                 and not s.polygon.is_empty]
    if not junctions:
        return 0
    ctx = build_context(layout)

    # taxi-network anchors a mouth may connect to: sloping taxi rects, runways,
    # and junctions that ALREADY have a spine (NOT aprons — a destination).
    spined = {id(s) for s in junctions if _has_spine(layout, s, ctx)}
    anchors = [s for s in layout.shapes
               if s.polygon is not None and not s.polygon.is_empty
               and (s.role in SLOPING_RECT_ROLES or s.role == ROLE_RUNWAY
                    or (s.role == ROLE_JUNCTION and id(s) in spined))]

    new_cls = []
    n_syn = 0
    serial = 0
    for s in junctions:
        if id(s) in spined:
            continue
        ring = _open(s.polygon)
        if len(ring) < 3:
            continue
        nb = [t for t in anchors if t.polygon.distance(s.polygon) < _TOUCH_M]
        if len(nb) < 2:
            continue
        # mouths: ring-edge midpoints lying ON a neighbour boundary, tagged with
        # the neighbour's taxi letter + whether the neighbour is a RUNWAY.
        mouths = []                          # (x, y, letter, is_runway)
        for ei in range(len(ring)):
            ax, ay = ring[ei]
            bx, by = ring[(ei + 1) % len(ring)]
            mid = Point(0.5 * (ax + bx), 0.5 * (ay + by))
            for t in nb:
                if t.polygon.exterior.distance(mid) < _ON_EDGE_M:
                    let = (taxi_shape_code_letter(layout, t)
                           if t.role in SLOPING_RECT_ROLES else None)
                    mouths.append((mid.x, mid.y, let, t.role == ROLE_RUNWAY))
                    break
        # merge mouths sharing one boundary.
        merged = []
        for (mx, my, let, is_rwy) in mouths:
            for j, (qx, qy, _ql, _qr) in enumerate(merged):
                if math.hypot(mx - qx, my - qy) < _MERGE_M:
                    break
            else:
                merged.append((mx, my, let, is_rwy))
        # IMPOSSIBLE-ROUTE GUARD (user 2026-06-26): do NOT route BETWEEN runway
        # contacts.  A junction wrapping a runway end (CYXY runway-20 blastpad)
        # touches the SAME runway on several sides; a star spine between those
        # contacts is a route runway→wrap→runway — not a taxi path — and the slice
        # FRAGMENTS the wrap.  Keep every TAXIWAY / spined-junction mouth plus AT
        # MOST ONE runway mouth (a single taxi→runway ENTRY is a real route), so:
        #   * taxiway↔taxiway through-junction  → kept,
        #   * taxiway→runway entry (1 taxi+rwy) → kept (taxi to the runway),
        #   * runway-only wrap (0 taxi mouths)  → ≤1 mouth → skipped (stays apron).
        if _os.environ.get("O4_SYNTH_SPINE_NO_RUNWAY_MOUTH", "1") == "1":
            non_rwy = [m for m in merged if not m[3]]
            rwy = [m for m in merged if m[3]]
            merged = non_rwy + (rwy[:1] if rwy else [])
        if len(merged) < 2:
            continue
        # the synthetic spine's letter = the LOOSER (steeper-cap) connecting
        # taxiway, so the route carries its corridor cap through the junction.
        from .config import taxi_grade_cap_for_letter
        best_let, best_cap = None, -1.0
        for (_x, _y, let, _r) in merged:
            c = taxi_grade_cap_for_letter(let)
            if c > best_cap:
                best_cap, best_let = c, let
        ref = f"~SJ{serial}"
        serial += 1
        cen = s.polygon.centroid
        pts = [(mx, my) for (mx, my, _l, _r) in merged]
        if len(pts) == 2:
            segs = [LineString([pts[0], pts[1]])]
        else:                                # star: each mouth → centroid
            segs = [LineString([p, (cen.x, cen.y)]) for p in pts]
        for ln in segs:
            if ln.length > 1.0:
                new_cls.append(TaxiCenterline(
                    line=ln, name=ref,
                    seg_sizes=[best_let or ""] * max(0, len(ln.coords) - 1)))
        n_syn += 1

    if new_cls:
        layout.apt_taxi_centerlines = (
            list(getattr(layout, "apt_taxi_centerlines", []) or []) + new_cls)
        UI.vprint(1, f"  [pav-builder] {icao}: synthesized {n_syn} junction "
                  f"spine(s) ({len(new_cls)} route segment(s)) for junctions "
                  f"with no taxi centerline.")
    return n_syn
