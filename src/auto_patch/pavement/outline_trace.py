"""V10 OUTLINE-TRACE spine (user model 2026-07-02, simplified ruleset).

The spine is a deterministic, total trace of the pavement footprint —
no route selection, no paint, no demand model.  Acceptance = the rules'
own invariants + user visual review (the SPJC hand target remains a
reference metric only).

USER RULES (verbatim intent):
 1. Pavement narrower than the widest documented taxiway width gets ONE
    centered line; where two edge-following corridors would overlap they
    merge into a single shared centerline.
 2. Runway contacts as before: diagonal straight to the edge + hook;
    square/straight contact = single line straight to the pavement edge,
    centered in its corridor.
 3. Never die into a corner.
 4. Every line must trace to a runway unbroken — no dead ends mid-
    pavement, no floating/free-standing lines, no lines in pavement with
    no path to a runway.  (Tips ON the pavement edge are legitimate.)
 5. Lines meet with smooth arcs.
 6. A trace running along a pavement edge SKIPS straight over any
    opening narrower than a size-A taxi corridor (never enters).
 7. Entering a mouth, the wing's size decides: big enough that an
    interior ring never self-overlaps -> trace the interior perimeter;
    otherwise go straight through and stop centered at the back edge.

OFFSETS (user ruling): the two-sided corridor width is MEASURED from the
pavement (shoulders are part of our footprint); one-sided traces (apron
perimeters, hole rings — nothing to measure against) fall back to the
DOCUMENTED width of the airport's widest documented taxiway size.
"""

from __future__ import annotations

import os

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from .pav_skeleton import _fair_chain, _polygons, build_pavement_skeleton
from .spine_synthesis import (
    SpineWay, _Graph, _add_junction_arcs, _add_runway_turns,
    _attribute_sizes, _fix_dangles, _runway_axes, _SVC_HALFWIDTH_M,
)
from .edge_trace import (
    _add_mouth_projections, _close_floating_tips, _connect_free_ends,
    _dominant_halfwidth, _is_runway_shoulder, _planarize_crossings,
    _prune_no_runway_components, _split_by_clearance, _split_line_by_mask,
)

# ICAO Annex 14 taxiway pavement width (no shoulders) by code letter —
# provisional values; pin in docs/STANDARDS.md when wired into the
# pipeline.  Used for the ONE-SIDED trace offset (half width) and the
# rule-6 skip-opening scale (size A).
TAXIWAY_WIDTH_BY_SIZE = {"A": 7.5, "B": 10.5, "C": 18.0,
                         "D": 23.0, "E": 23.0, "F": 25.0}
_SIZE_ORDER = "ABCDEF"


def _widest_documented_size(routes) -> str:
    best = ""
    for tc in routes or []:
        s = ""
        try:
            s = tc.dominant_size() or ""
        except Exception:
            s = getattr(tc, "size", "") or ""
        if s in _SIZE_ORDER and (not best or
                                 _SIZE_ORDER.index(s) > _SIZE_ORDER.index(best)):
            best = s
    return best


def _bridge_facing_tips(g: _Graph, pav_eff, reach: float):
    """Corridor lines continue STRAIGHT through junction openings (rules
    2/7): a free tip whose tangent faces another free tip across open
    pavement gets a straight bridge.  This is the junction glue — the
    medial stops where the opening widens, leaving facing stubs each
    side (measured: 158 components without it)."""
    from collections import Counter
    allow = shapely.buffer(pav_eff, 0.5)
    deg = Counter()
    for e in g.edges:
        if e["alive"]:
            deg[e["a"]] += 1
            deg[e["b"]] += 1
    tips = []                                   # (node, unit tangent OUT)
    for ei, e in enumerate(g.edges):
        if not e["alive"]:
            continue
        for ni, at_a in ((e["a"], True), (e["b"], False)):
            if deg[ni] != 1:
                continue
            u = g.edge_dir_at(ei, at_a)         # direction ARRIVING at tip
            tips.append((ni, u))
    cands = []
    for i in range(len(tips)):
        for j in range(i + 1, len(tips)):
            (na, ua), (nb, ub) = tips[i], tips[j]
            A, B = g.nodes[na], g.nodes[nb]
            d = float(np.hypot(*(B - A)))
            if not 1.0 <= d <= reach:
                continue
            v = ((B[0] - A[0]) / d, (B[1] - A[1]) / d)
            # A's outgoing tangent must continue toward B, and B's back
            if _angle(ua, v) > 30.0 or _angle(ub, (-v[0], -v[1])) > 30.0:
                continue
            if not allow.contains(LineString([tuple(A), tuple(B)])):
                continue
            cands.append((d, na, nb))
    used = set()
    added = 0
    for d, na, nb in sorted(cands):
        if na in used or nb in used:
            continue
        used.add(na)
        used.add(nb)
        g.add_edge(np.asarray([g.nodes[na], g.nodes[nb]]), "lane", "", 0.0)
        added += 1
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[outline] tip bridges: {added}", flush=True)


def _angle(u, v) -> float:
    du = np.degrees(np.arctan2(u[1], u[0]) - np.arctan2(v[1], v[0]))
    du = abs((du + 180.0) % 360.0 - 180.0)
    return du


def _weld_components(g: _Graph, pav_eff, max_gap: float):
    """Rule 4 constructively: the network must be ONE fabric.  Every
    disconnected component gets welded to the rest through its shortest
    pavement-contained connector (node -> split point on a foreign
    edge), smallest component first, until no bridgeable gap remains.
    Tip-tangent heuristics cannot do this — measured 131 components with
    20-60 m gaps at every junction family."""
    import networkx as nx
    allow = shapely.buffer(pav_eff, 0.5)
    welded = 0
    for _round in range(250):
        G = nx.Graph()
        for ei, e in enumerate(g.edges):
            if e["alive"]:
                G.add_edge(e["a"], e["b"], ei=ei)
        comps = sorted(nx.connected_components(G), key=len)
        if len(comps) <= 1:
            break
        merged_any = False
        for comp in comps[:-1]:
            comp_edges = {G[a][b]["ei"] for a, b in G.subgraph(comp).edges}
            cands = []                     # (d, node, ei_other, s_along)
            for ni in comp:
                p = Point(tuple(g.nodes[ni]))
                for ei, e in enumerate(g.edges):
                    if not e["alive"] or ei in comp_edges:
                        continue
                    if e["a"] in comp or e["b"] in comp:
                        continue
                    ln = LineString(e["cs"])
                    d = ln.distance(p)
                    if d < 0.5 or d > max_gap:
                        continue
                    cands.append((d, ni, ei, float(ln.project(p))))
            # shortest PAVEMENT-CONTAINED connector wins; a blocked best
            # (hole/building in the way) falls through to the next
            for d, ni, ei, s in sorted(cands)[:12]:
                if not g.edges[ei]["alive"]:
                    continue
                conn = LineString([tuple(g.nodes[ni]),
                                   LineString(g.edges[ei]["cs"])
                                   .interpolate(s).coords[0]])
                if conn.length >= 1.0 and not allow.contains(conn):
                    continue
                mid = g.split_edge(ei, s)
                if mid != ni:
                    g.add_edge(np.asarray([g.nodes[ni], g.nodes[mid]]),
                               "lane", "", 0.0)
                welded += 1
                merged_any = True
                break
            if merged_any:
                break                      # graph changed; recompute comps
        if not merged_any:
            break
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[outline] component welds: {welded}", flush=True)


def _prune_unreachable(g: _Graph, runway_union, tol: float = 3.0):
    """Rule 4, strict form: a component with no runway contact has no
    path to a runway — no lines there, whatever its size (the v8 prune's
    >=400 m keep-heuristic does not apply to this model)."""
    if runway_union is None or runway_union.is_empty:
        return
    import networkx as nx
    edge_b = runway_union.boundary
    G = nx.Graph()
    for ei, e in enumerate(g.edges):
        if e["alive"]:
            G.add_edge(e["a"], e["b"])
    for comp in nx.connected_components(G):
        if any(edge_b.distance(Point(tuple(g.nodes[ni]))) < tol
               for ni in comp):
            continue
        for e in g.edges:
            if e["alive"] and (e["a"] in comp or e["b"] in comp):
                e["alive"] = False


def _trim_interior_stubs(g: _Graph, pav_eff, runway_union,
                         max_len: float = 22.0):
    """Rule 4: short leaf edges whose free tip hangs mid-pavement are
    noise (a legitimate line ends on the pavement edge or a runway)."""
    from collections import Counter
    bnd = pav_eff.boundary
    rwy_b = runway_union.boundary if runway_union is not None \
        and not runway_union.is_empty else None
    for _ in range(3):
        deg = Counter()
        for e in g.edges:
            if e["alive"]:
                deg[e["a"]] += 1
                deg[e["b"]] += 1
        changed = False
        for e in g.edges:
            if not e["alive"] or LineString(e["cs"]).length > max_len:
                continue
            for tip in (e["a"], e["b"]):
                if deg[tip] != 1:
                    continue
                p = Point(tuple(g.nodes[tip]))
                if bnd.distance(p) < 3.0:
                    continue
                if rwy_b is not None and rwy_b.distance(p) < 3.0:
                    continue
                e["alive"] = False
                changed = True
                break
        if not changed:
            break


def synthesize_spine_v10(
    pav, runway_union=None, buildings=None, routes=None, *,
    terminal_setback: float = 100.0, recognized=None, ramps=None,
) -> list[SpineWay]:
    """Outline-trace spine.  ``recognized``/``ramps`` accepted for caller
    compatibility and ignored — this model is pavement-only."""
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
    pav_eff = pav_nav
    if runway_union is not None and not runway_union.is_empty:
        try:
            pav_eff = pav_nav.difference(runway_union)
        except Exception:
            pass

    # ── widths ────────────────────────────────────────────────────────────
    chains = build_pavement_skeleton(pav_nav, runway_union=runway_union)
    w = _dominant_halfwidth(chains)                # measured (two-sided)
    size_doc = _widest_documented_size(routes)
    w_doc = 0.5 * TAXIWAY_WIDTH_BY_SIZE.get(size_doc, 2.0 * 11.5) \
        if size_doc else 0.5 * min(23.0, 2.0 * w)  # one-sided fallback
    a_skip = TAXIWAY_WIDTH_BY_SIZE["A"]            # rule-6 opening scale
    dbg = bool(os.environ.get("O4_ET_DEBUG"))
    if dbg:
        print(f"[outline] w_meas={w:.1f} widest_doc={size_doc or '?'} "
              f"w_doc={w_doc:.1f} a_skip={a_skip}", flush=True)

    axes = _runway_axes(runway_union)
    rwy_zone = shapely.buffer(runway_union, 25.0) \
        if runway_union is not None and not runway_union.is_empty else None

    # ── rule 1 — corridor centerlines (merge-when-overlapping) ────────────
    svc_cut = max(_SVC_HALFWIDTH_M, 0.35 * w)
    center_runs = []
    center_lines = []
    for ch in chains:
        w_med = float(np.median(ch.radii)) if ch.radii else 8.0
        if w_med < svc_cut:
            continue                    # service-road width (prior rulings)
        if _is_runway_shoulder(ch, w_med, rwy_zone, axes):
            continue
        for keep, cs, rr in _split_by_clearance(ch, 1.1 * w):
            if not keep or LineString(cs).length < 4.0:
                continue
            center_runs.append((cs, float(np.median(rr))))
            center_lines.append(LineString(cs))
    center_union = unary_union(center_lines) if center_lines else None

    # ── rules 6+7 — one-sided traces of the closed footprint ─────────────
    # closing at the size-A scale: boundary notches / mouths an aircraft
    # cannot use simply do not exist for the trace
    r_close = 0.5 * a_skip
    pav_closed = shapely.buffer(shapely.buffer(pav_eff, r_close), -r_close)
    core = shapely.buffer(pav_closed, -w_doc)

    trace_runs = []
    n_ring_drop = 0
    for poly in _polygons(core):
        # rule 7: an interior ring must never self-overlap — its own
        # corridor (w_doc each side) must fit; a pocket core thinner than
        # 2*w_doc is served by the centerline regime / straight-through
        ring_ok = not shapely.buffer(poly, -w_doc).is_empty
        rings = [poly.exterior, *list(poly.interiors)]
        for ring in rings:
            if not ring_ok and poly.area < (4.0 * w_doc) ** 2:
                n_ring_drop += 1
                continue
            ln = LineString(ring)
            n = max(4, int(ln.length / 4.0))
            cs = np.asarray(
                [ln.interpolate(k * ln.length / n).coords[0]
                 for k in range(n + 1)])
            keep = np.ones(len(cs), dtype=bool)
            for k, p in enumerate(cs):
                q = Point(tuple(p))
                # never trace along the runway edge (rule 2 family)
                if runway_union is not None and not runway_union.is_empty \
                        and runway_union.distance(q) <= w_doc + 3.0:
                    keep[k] = False
                    continue
                # merge rule: a trace riding a corridor the centerline
                # already carries is the same line — drop it there
                if center_union is not None \
                        and center_union.distance(q) <= 1.0 * w:
                    keep[k] = False
            for piece in _split_line_by_mask(cs, keep):
                # sub-corridor-length scraps are junction-flare noise, not
                # traceable edges (they read as ticks along the corridors)
                if LineString(piece).length >= 28.0:
                    trace_runs.append(np.asarray(piece))
    if dbg:
        print(f"[outline] centers={len(center_runs)} traces={len(trace_runs)} "
              f"rings_dropped={n_ring_drop}", flush=True)

    # smooth the trace polylines (offset boundaries carry the pavement's
    # corner vertices; rule 5 wants smooth geometry between arc joins)
    allow = shapely.buffer(pav_eff, 0.5)
    faired = []
    for cs in trace_runs:
        rr = np.full(len(cs), w_doc)
        out, _r = _fair_chain(np.asarray(cs, dtype=float), rr, allow,
                              step=6.0, sigma=0.35)
        faired.append(out)

    # ── weld into one graph ───────────────────────────────────────────────
    g = _Graph()
    for cs, w_med in center_runs:
        g.add_edge(np.asarray(cs, dtype=float), "lane", "", w_med)
    for cs in faired:
        g.add_edge(np.asarray(cs, dtype=float), "trace", "", w_doc)

    _planarize_crossings(g)
    _bridge_facing_tips(g, pav_eff, reach=4.0 * w)
    _connect_free_ends(g, pav_eff)
    # rule 7 straight-through: corridor mouths project straight across
    # open space to the far pavement edge (serves pockets whose ring was
    # dropped and dead-end wings)
    _add_mouth_projections(g, pav_eff)
    _planarize_crossings(g)
    _bridge_facing_tips(g, pav_eff, reach=4.0 * w)
    _connect_free_ends(g, pav_eff)

    # rule 4 — unbroken trace to a runway for every line
    _weld_components(g, pav_eff, max_gap=3.0 * w)
    _close_floating_tips(g, pav_eff, building_union)
    _prune_unreachable(g, runway_union)
    _fix_dangles(g, pav_eff)
    _trim_interior_stubs(g, pav_eff, runway_union)

    if not os.environ.get("O4_ET_KINDS"):
        for e in g.edges:
            e["kind"] = "lane"
    g.consolidate()

    # ── rules 2 + 5 — arcs at joins, runway hooks ─────────────────────────
    def pav_ok(line: LineString) -> bool:
        return allow.contains(line)

    _attribute_sizes(g, routes)
    bnd_arc = pav_eff.boundary

    def r_start_for(P, r_std):
        return min(1.6 * r_std, max(r_std, bnd_arc.distance(Point(tuple(P)))))

    _add_junction_arcs(g, pav_ok, runway_union, r_start_for=r_start_for)
    _add_runway_turns(g, runway_union, pav_eff)
    _fix_dangles(g, pav_eff)
    _close_floating_tips(g, pav_eff, building_union)
    g.consolidate()
    return g.ways()
