"""V13 ROUTE-GRAPH + ARCS spine (user architecture ruling 2026-07-02).

The solver's anchors come from FEASIBLE ELEVATIONS, which come from
ACCURATE TAXI DISTANCES between runway intersections and buildings — so
the spine's first obligation is metric fidelity, and the apt.dat
1201/1202 taxi-route graph is the authoritative distance source (it is
what production builds the spine from).  Its one shortcoming is the
lack of curves: every junction turn and every bend is a hard corner,
which the grading cannot climb smoothly.

V13 keeps the route graph as-is — every straight segment, every true
distance — and adds ONLY the missing arcs, in the key spots the tracing
work identified:

* JUNCTION TURNS: every branch pair with a real deflection at every
  welded node — including T-junctions where a stem meets a through
  route's interior (the production route-END fillet pass misses those;
  planarizing first makes them ordinary nodes);
* BENDS inside chained routes (split at deflection vertices so the arc
  pass sees them as nodes);
* RUNWAY entries/exits: hooks/diagonal blends via the existing
  runway-turn machinery.

Arcs replace corners, so path lengths only shrink toward the real
aircraft path — never grow.  Radii: R90_BY_SIZE per the segment's ICAO
size letter, mirrored equal per junction, shrink-to-fit the pavement
(the full thru-runway pavement: routes cross runways freely).
"""

from __future__ import annotations

import math
import os

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from .spine_synthesis import (
    SpineWay, _Graph, _add_junction_arcs, _add_runway_turns, _angle_deg,
)
from .edge_trace import _planarize_crossings


def _split_route_bends(g: _Graph, min_defl_deg: float = 12.0,
                       min_leg_m: float = 15.0):
    """Insert nodes at interior bend vertices of route edges so the arc
    pass fillets them.  apt.dat routes are straight BETWEEN vertices; a
    real bend shows as a deflection at one vertex."""
    ei = -1
    while ei + 1 < len(g.edges):        # split halves re-enter the scan
        ei += 1
        e = g.edges[ei]
        if not e["alive"]:
            continue
        cs = e["cs"]
        if len(cs) < 3:
            continue
        # find the sharpest interior deflection with decent legs
        best = None
        acc = 0.0
        for k in range(1, len(cs) - 1):
            a = cs[k] - cs[k - 1]
            b = cs[k + 1] - cs[k]
            na, nb = np.hypot(*a), np.hypot(*b)
            acc += na
            if na < 1e-6 or nb < 1e-6:
                continue
            defl = _angle_deg(tuple(a / na), tuple(b / nb))
            if defl < min_defl_deg:
                continue
            tail = float(LineString(cs[k:]).length)
            if acc < min_leg_m or tail < min_leg_m:
                continue
            if best is None or defl > best[1]:
                best = (acc, defl)
        if best is not None:
            g.split_edge(ei, best[0])   # halves re-enter the loop later


def synthesize_spine_v13(
    pav, runway_union=None, buildings=None, routes=None, *,
    terminal_setback: float = 100.0, recognized=None, ramps=None,
    rwy_full=None,
) -> list[SpineWay]:
    del recognized, ramps, terminal_setback
    buildings = [(b, r) for (b, r) in (buildings or [])
                 if b is not None and not b.is_empty]
    building_union = unary_union([b for b, _ in buildings]) \
        if buildings else None
    pav_nav = pav
    if building_union is not None:
        try:
            pav_nav = pav.difference(shapely.buffer(building_union, 0.5))
        except Exception:
            pav_nav = pav
    # thru-runway pavement (user ruling): arcs may live anywhere on the
    # continuous footprint, runway included
    parts = [pav_nav]
    if rwy_full is not None and not rwy_full.is_empty:
        parts.append(rwy_full)
    elif runway_union is not None and not runway_union.is_empty:
        parts.append(runway_union)
    pav_all = unary_union(parts)
    dbg = bool(os.environ.get("O4_ET_DEBUG"))

    g = _Graph()
    n_routes = 0
    for tc in routes or []:
        if getattr(tc, "is_service", False):
            continue
        ln = getattr(tc, "chained_line", None) or getattr(tc, "line", None)
        if ln is None or ln.is_empty or ln.length < 2.0:
            continue
        size = ""
        try:
            size = tc.dominant_size() or ""
        except Exception:
            pass
        g.add_edge(np.asarray(ln.coords, dtype=float), "lane", size, 0.0)
        n_routes += 1

    # weld crossings and stem-on-interior T junctions into real nodes,
    # then make interior bends nodes too — the arc pass only sees nodes
    _planarize_crossings(g)
    _split_route_bends(g)
    if dbg:
        alive = sum(1 for e in g.edges if e["alive"])
        L = sum(LineString(e["cs"]).length for e in g.edges if e["alive"])
        print(f"[routearc] routes={n_routes} edges={alive} "
              f"len={L/1000:.1f}km", flush=True)

    allow = shapely.buffer(pav_all, 0.5)

    def pav_ok(line: LineString) -> bool:
        return allow.contains(line)

    len_before = sum(LineString(e["cs"]).length
                     for e in g.edges if e["alive"])
    _add_junction_arcs(g, pav_ok, runway_union=None)
    _add_runway_turns(g, runway_union, pav_all)
    g.consolidate()
    if dbg:
        n_arc = sum(1 for e in g.edges if e["alive"]
                    and e["kind"] in ("arc", "blend", "rwy_turn"))
        len_after = sum(LineString(e["cs"]).length
                        for e in g.edges if e["alive"])
        print(f"[routearc] arcs={n_arc} length {len_before/1000:.2f} -> "
              f"{len_after/1000:.2f} km (corner rounding only)",
              flush=True)
    return g.ways()
