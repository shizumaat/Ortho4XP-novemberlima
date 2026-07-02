"""V12 PURE WALL-TRACE spine (user prototype spec 2026-07-02).

No medial, no straightener — the pavement is already straight, so the
spine follows it.  ONE rule generates everything:

    every boundary wall is traced at offset d(s) = min(w, L(s)/2)

where L(s) is the local width (ray cast from the wall sample along its
inward normal to the opposite wall).  Consequences, matching the user's
steps 1-4:

* a 2w-wide corridor: both walls' traces coincide on the CENTERLINE
  (the clamp), so a runway contact of normal width starts one line at
  the contact midpoint;
* a contact >20% wider: the two walls' traces stay separate — one line
  at w from each side, exactly step 2;
* runway contacts split each boundary ring into wall ARCS, so every
  trace starts and ends on a runway edge (steps 1-3); the runway edge
  itself is never traced;
* island/hole rings with no runway contact trace as closed loops and
  meet the arc traces wherever a passage narrows below 2w — both clamp
  onto the same centerline (step 4's connect-and-stop).

Coincident twins are merged; rule-4 reachability reports what the pure
trace misses (kept behind O4_MR_NO_PRUNE for review).
"""

from __future__ import annotations

import math
import os

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree

from .pav_skeleton import _polygons, build_pavement_skeleton
from .spine_synthesis import (
    SpineWay, _Graph, _add_junction_arcs, _add_runway_turns,
    _attribute_sizes, _fix_dangles, _runway_axes,
)
from .edge_trace import _dominant_halfwidth, _split_line_by_mask
from .outline_trace import TAXIWAY_WIDTH_BY_SIZE


def _ring_samples(ring, step: float):
    n = max(8, int(math.ceil(ring.length / step)))
    ts = np.arange(n) * (ring.length / n)
    pts = np.asarray([ring.interpolate(t).coords[0] for t in ts])
    return pts


def _trace_arcs(poly: Polygon, runway_union, w: float, step: float = 2.0):
    """Clamped wall-offset traces for one pavement face.  Yields
    (coords, closed) polylines."""
    rings = [poly.exterior, *list(poly.interiors)]
    # segment soup of the whole face boundary for ray casting
    segs = []
    for ring in rings:
        xy = np.asarray(ring.coords)
        for k in range(len(xy) - 1):
            segs.append(LineString([tuple(xy[k]), tuple(xy[k + 1])]))
    tree = STRtree(segs)
    rwy_b = runway_union.boundary if runway_union is not None \
        and not runway_union.is_empty else None

    for ring in rings:
        pts = _ring_samples(ring, step)
        n = len(pts)
        if n < 8:
            continue
        # inward normal: pick the rotation whose test point falls inside
        tang = np.roll(pts, -1, axis=0) - np.roll(pts, 1, axis=0)
        norms = np.stack([-tang[:, 1], tang[:, 0]], axis=1)
        ln = np.hypot(norms[:, 0], norms[:, 1])
        ln[ln < 1e-9] = 1.0
        norms /= ln[:, None]
        k_mid = n // 2
        probe = pts[k_mid] + norms[k_mid] * 0.8
        if not poly.contains(Point(tuple(probe))):
            norms = -norms

        # a boundary sample lies ON the runway edge only if it is close
        # to the runway AND its own tangent runs PARALLEL to the runway
        # edge there.  True overlap cuts sit at <0.5 m; east-side source
        # polygons under-lap the runway rect by 1.5-3 m, so the distance
        # tolerance must be loose — parallelism is what keeps the mouth
        # CORNERS (perpendicular side edges near the cut) out.
        on_rwy = np.zeros(n, dtype=bool)
        if rwy_b is not None:
            for k in range(n):
                p = Point(tuple(pts[k]))
                if rwy_b.distance(p) >= 3.5:
                    continue
                s0 = rwy_b.project(p)
                a2 = rwy_b.interpolate(max(0.0, s0 - 4.0))
                b2 = rwy_b.interpolate(s0 + 4.0)
                rv = np.asarray([b2.x - a2.x, b2.y - a2.y])
                tv = tang[k]
                nr = float(np.hypot(*rv))
                ntv = float(np.hypot(*tv))
                if nr < 1e-6 or ntv < 1e-6:
                    continue
                cosang = abs(float(np.dot(rv, tv)) / (nr * ntv))
                if cosang > 0.90:              # within ~25 deg of parallel
                    on_rwy[k] = True

        # offset each sample by min(w, L/2); L = local width by ray cast
        out = np.empty_like(pts)
        depth = np.empty(n)
        for k in range(n):
            p = pts[k]
            nvec = norms[k]
            ray = LineString([tuple(p + nvec * 0.05),
                              tuple(p + nvec * 8.0 * w)])
            L = None
            for si in tree.query(ray):
                x = ray.intersection(segs[int(si)])
                for q in getattr(x, "geoms", [x]):
                    if q.geom_type != "Point":
                        continue
                    d = math.hypot(q.x - p[0], q.y - p[1])
                    if d > 0.5 and (L is None or d < L):
                        L = d
            if L is None:
                L = 2.0 * w
            depth[k] = L
            out[k] = p + nvec * min(w, L / 2.0)

        # a runway CONTACT sample splits the wall into arcs only where a
        # corridor actually leads away (deep pavement behind = a MOUTH,
        # where the line must end on the runway edge).  Shallow abutment
        # — pavement riding along the runway edge — is just another
        # wall; its clamped trace IS the parallel-taxiway line.
        mouth = on_rwy & (depth >= 1.6 * w)
        ok = ~mouth
        if mouth.any():
            for piece in _split_line_by_mask(out, ok):
                if LineString(piece).length >= 8.0:
                    yield np.asarray(piece), False
        else:
            coords = out[ok]
            if len(coords) >= 4 and LineString(coords).length >= 8.0:
                closed = np.vstack([coords, coords[:1]])
                yield closed, True


def synthesize_spine_v12(
    pav, runway_union=None, buildings=None, routes=None, *,
    terminal_setback: float = 100.0, recognized=None, ramps=None,
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
    # the spine is traced AS IF THE RUNWAY DOES NOT EXIST (user ruling
    # 2026-07-02): runway rects union into the pavement — one continuous
    # footprint, no runway edges, no contact logic.  (The rects are NOT
    # part of source_pavement_union on their own.)  Spine-vs-runway
    # elevation authority is a wiring-time question, out of scope here.
    pav_eff = pav_nav
    if runway_union is not None and not runway_union.is_empty:
        pav_eff = unary_union([pav_nav, runway_union])
    runway_union = None

    chains = build_pavement_skeleton(pav_nav, runway_union=runway_union)
    w = _dominant_halfwidth(chains)
    a_skip = TAXIWAY_WIDTH_BY_SIZE["A"]
    r_close = 0.5 * a_skip
    pav_closed = shapely.buffer(shapely.buffer(pav_eff, r_close), -r_close)
    dbg = bool(os.environ.get("O4_ET_DEBUG"))

    g = _Graph()
    n_arcs = n_loops = 0
    for poly in _polygons(pav_closed):
        if poly.area < 400.0:
            continue
        for coords, closed in _trace_arcs(poly, runway_union, w):
            coords = np.asarray(LineString(coords).simplify(0.3).coords)
            g.add_edge(coords, "lane", "", w)
            if closed:
                n_loops += 1
            else:
                n_arcs += 1
    if dbg:
        print(f"[pure] wall arcs={n_arcs} loops={n_loops} w={w:.1f}",
              flush=True)

    if os.environ.get("O4_MR_RAW"):
        g.consolidate()
        return g.ways()

    from .edge_trace import _planarize_crossings
    from .outline_trace import _merge_coincident, _prune_unreachable
    _planarize_crossings(g)
    _merge_coincident(g, gap=5.0)
    _planarize_crossings(g)
    _fix_dangles(g, pav_eff)
    if not os.environ.get("O4_MR_NO_PRUNE"):
        _prune_unreachable(g, runway_union)
    g.consolidate()

    allow = shapely.buffer(pav_eff, 0.5)

    def pav_ok(line: LineString) -> bool:
        return allow.contains(line)

    _attribute_sizes(g, routes)
    _add_runway_turns(g, runway_union, pav_eff)
    g.consolidate()
    return g.ways()
