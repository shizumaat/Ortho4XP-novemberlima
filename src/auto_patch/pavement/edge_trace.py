"""V8 EDGE-TRACE spine synthesis (user model 2026-07-02).

The spine is what a draughtsman drawing the taxi diagram would produce from
the pavement footprint alone:

* Compute the dominant corridor half-width ``w`` (most corridors at an
  airport share it).  The spine FOLLOWS THE PAVEMENT EDGE keeping distance
  ``w``; where the opposite edge is closer than ``w`` (local width <= 2w)
  that trace degenerates to the corridor CENTERLINE (medial pinch).
* MINIMUM SEPARATION: two parallel spines share one pavement space only if
  they are far enough apart for two aircraft to pass — the taxiway-to-
  taxiway centreline separation standard for the airport's ICAO code
  letter.  An opening wider than 2w but narrower than that separation gets
  ONE spine at its CENTER; only wider spaces get two edge-hugging traces.
* Larger aprons: the edge trace outlines the apron interior at ``w`` and
  rings holes along the apron edge — never long crossings through open
  pavement, never stand-row spurs.
* The pavement is the BUILDING-SUBTRACTED footprint; the spine touches the
  runway at every pavement intersection but NEVER runs along the runway
  edge itself.

Implementation: both regimes come from one threshold on clearance.  A
medial-axis vertex with clearance radius ``c`` lies in an opening of width
``2c``; it is kept as centered spine while ``2c < separation``.  Where
``2c >= separation`` the spine is the boundary of the pavement ERODED by
``w`` (``buffer(-w)``), restricted to eroded regions wide enough for two
lanes (the same threshold: core width ``2c - 2w >= separation - 2w``) —
by construction the two regimes hand over exactly at the separation width
and are then welded into one graph.
"""

from __future__ import annotations

import math
import os
from collections import Counter, defaultdict

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, unary_union

from .pav_skeleton import build_pavement_skeleton, _polygons
from .spine_synthesis import (
    R90_BY_SIZE, SpineWay, _Graph, _add_junction_arcs, _add_runway_turns,
    _angle_deg, _assemble_through_paths, _attribute_sizes, _collect_path,
    _fillet, _fix_dangles, _runway_axes, _size_for_halfwidth, _unit,
    _SVC_HALFWIDTH_M,
)

# ICAO Annex 14 taxiway/taxiway centreline separation by code letter
# (provisional values, user ruling 2026-07-02 — pin in docs/STANDARDS.md
# when this is wired into the pipeline).
SEPARATION_BY_SIZE = {"A": 23.75, "B": 33.5, "C": 44.0, "D": 66.5,
                      "E": 80.0, "F": 97.5}

# Corridor half-width histogram band: narrower is a service road, wider is
# open-pavement medial (same policy the hand-edited target encodes).
_W_BAND = (_SVC_HALFWIDTH_M, 32.0)
# A trace segment at half-width from the RUNWAY cut edge is the runway's
# own shoulder line, never taxi spine.
_RWY_TRACE_SLACK_M = 3.0
# Free spine ends may extend along their tangent up to this reach to weld
# onto the rest of the network or the pavement boundary.
_WELD_REACH_M = 80.0
_WELD_SNAP_M = 12.0


def _dominant_halfwidth(chains) -> float:
    """Length-weighted mode of medial clearance radii over the corridor
    band — 'most corridors share the same half-width w'."""
    vals, wts = [], []
    for ch in chains:
        cs = np.asarray(ch.line.coords)
        rr = np.asarray(ch.radii, dtype=float)
        if len(cs) < 2 or len(rr) != len(cs):
            continue
        seg = np.hypot(*(cs[1:] - cs[:-1]).T)
        mid = 0.5 * (rr[1:] + rr[:-1])
        m = (mid >= _W_BAND[0]) & (mid <= _W_BAND[1])
        vals.extend(mid[m])
        wts.extend(seg[m])
    if not vals:
        return 12.0
    a, wt = np.asarray(vals), np.asarray(wts)
    hist, edges = np.histogram(a, bins=np.arange(_W_BAND[0], _W_BAND[1] + 1.0,
                                                 1.0), weights=wt)
    k = int(hist.argmax())
    sel = (a >= edges[k] - 1.5) & (a <= edges[k] + 2.5)
    return float(np.average(a[sel], weights=wt[sel])) if sel.any() \
        else float(edges[k] + 0.5)


def _dominant_size(routes, w: float) -> str:
    """Airport's governing ICAO code letter — from the route attributes
    (length-weighted majority) where present, half-width fallback."""
    cnt: Counter = Counter()
    for rt in routes or []:
        if getattr(rt, "is_service", False):
            continue
        s = (getattr(rt, "dominant_size", lambda: "")() or "").strip().upper()
        ln = getattr(rt, "chained_line", None) or getattr(rt, "line", None)
        if s in SEPARATION_BY_SIZE and ln is not None and not ln.is_empty:
            cnt[s] += ln.length
    if cnt:
        return cnt.most_common(1)[0][0]
    return _size_for_halfwidth(w)


def _split_by_clearance(ch, sep_half: float):
    """Split one medial chain into (keep, coords, radii) runs at the
    separation threshold: c < sep_half stays centered spine, wider parts
    belong to the edge trace."""
    cs = np.asarray(ch.line.coords)
    rr = np.asarray(ch.radii, dtype=float)
    if len(rr) != len(cs):
        rr = np.full(len(cs), float(np.median(rr)) if len(rr) else 8.0)
    out = []
    idx = [0]
    cur = bool(rr[0] < sep_half)
    for i in range(1, len(cs)):
        k = bool(rr[i] < sep_half)
        idx.append(i)
        if k != cur:
            out.append((cur, cs[idx], rr[idx]))
            idx = [i]
            cur = k
    out.append((cur, cs[idx], rr[idx]))
    return [(k, c, r) for (k, c, r) in out if len(c) >= 2]


def _split_line_by_mask(coords, keep_mask):
    """Contiguous kept runs of a vertex-mask over a polyline."""
    runs, cur = [], []
    for i, k in enumerate(keep_mask):
        if k:
            cur.append(i)
        elif len(cur) >= 2:
            runs.append(np.asarray(coords[cur]))
            cur = []
        else:
            cur = []
    if len(cur) >= 2:
        runs.append(np.asarray(coords[cur]))
    return runs


def _circulation_pieces(core, corridors, w: float, sep: float):
    """Split the open space (``core`` = pavement eroded by w) into pieces an
    aircraft CIRCULATES through vs dead-end parking/stand bays.  A piece is
    circulation only if at least TWO distinct corridor arms attach to it —
    you can enter and leave (the hand-edited target leaves bay interiors
    empty; the corridor arriving at a lone mouth just crosses straight to
    the far edge via the dangle rule)."""
    circ, bays = [], []
    for cp in _polygons(core):
        probe = cp.buffer(1.5 * w)
        pts = []
        for ln in corridors:
            if not probe.intersects(ln):
                continue
            for tip in (ln.coords[0], ln.coords[-1]):
                if probe.contains(Point(tip)):
                    pts.append(np.asarray(tip))
        # cluster attachment points into mouths (one cluster per opening)
        mouths = []
        for p in pts:
            for m in mouths:
                if float(np.hypot(*(p - m[0]))) < sep:
                    m.append(p)
                    break
            else:
                mouths.append([p])
        (circ if len(mouths) >= 2 else bays).append(cp)
    return circ, bays


def _edge_traces(core, pav_eff, w: float, runway_union, building_union,
                 trunk_union):
    """The trace regime of the V8 model: boundary of the pavement eroded by
    ``w``, split by the pavement edge it traces.  HOLE rings (grass
    islands — the spine rides holes for its turns) become ROUTING
    candidates ("ring", kept only where a route uses them); BUILDING
    frontage (stand/terminal lead lanes) is kept as spine ("front").
    Plain outer pavement edges get NO trace (open aprons stay empty), a
    stretch at ``w`` from a runway cut is the runway's own shoulder, and
    anything riding an existing corridor centerline is that corridor's own
    edge trace, already represented by the centerline."""
    if core.is_empty:
        return []
    traces = []
    bnd = core.boundary
    for ring in getattr(bnd, "geoms", [bnd]):
        if ring.geom_type != "LineString" or ring.length < 8.0:
            continue
        dense = ring.segmentize(4.0)
        cs = np.asarray(dense.coords)
        keep = []
        slack = w + _RWY_TRACE_SLACK_M
        for p in cs:
            q = Point(tuple(p))
            if runway_union is not None \
                    and runway_union.distance(q) <= slack:
                keep.append(False)              # runway shoulder line
            elif trunk_union is not None and trunk_union.distance(q) <= 4.0:
                keep.append(False)              # corridor lane already there
            else:
                keep.append(True)
        for run in _split_line_by_mask(cs, keep):
            if LineString(run).length >= 12.0:
                traces.append(("ring", LineString(run).simplify(0.4)))
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] trace segs={len(traces)} "
              f"len={sum(t.length for _k, t in traces):.0f}", flush=True)
    return traces


def _is_runway_shoulder(ch, w: float, rwy_zone, axes) -> bool:
    """A medial chain that RIDES PARALLEL along a runway inside its zone is
    the pav-minus-runway shoulder sliver — the runway profile owns it."""
    if rwy_zone is None or ch.line.length <= 1.0 or w >= 9.0:
        return False
    n = max(2, int(ch.line.length / 10))
    inside = sum(1 for k in range(n + 1)
                 if rwy_zone.contains(ch.line.interpolate(
                     k * ch.line.length / n)))
    if inside / (n + 1) < 0.6:
        return False
    cs = np.asarray(ch.line.coords)
    d_ch = _unit(*(cs[-1] - cs[0]))
    return any(min(_angle_deg(d_ch, a), 180.0 - _angle_deg(d_ch, a)) <= 15.0
               for a in axes)


def _prune_no_runway_components(g: _Graph, runway_union, pav_eff):
    """Every taxiway system serves a runway; a small connected component
    that never reaches one is apron-bay internals the target leaves empty
    (kind-agnostic version of spine_synthesis._prune_components)."""
    if runway_union is None or runway_union.is_empty:
        return
    edge_b = runway_union.boundary
    adj: dict = {}
    for e in g.edges:
        if not e["alive"]:
            continue
        adj.setdefault(e["a"], set()).add(e["b"])
        adj.setdefault(e["b"], set()).add(e["a"])
    seen: set = set()
    comps = []
    for start in list(adj):
        if start in seen:
            continue
        comp, stack = {start}, [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            for nxt in adj.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    comp.add(nxt)
                    stack.append(nxt)
        comps.append(comp)
    pieces = _polygons(pav_eff)
    best_on_piece: dict = {}
    infos = []
    for comp in comps:
        touches = any(edge_b.distance(Point(tuple(g.nodes[ni]))) < 2.5
                      for ni in comp)
        length = sum(LineString(e["cs"]).length for e in g.edges
                     if e["alive"] and e["a"] in comp)
        anyn = g.nodes[next(iter(comp))]
        pi = next((k for k, pc in enumerate(pieces)
                   if pc.distance(Point(tuple(anyn))) < 2.0), -1)
        infos.append((comp, touches, length, pi))
        if pi >= 0 and length > best_on_piece.get(pi, (0.0, None))[0]:
            best_on_piece[pi] = (length, id(comp))
    keep_ids = {v[1] for v in best_on_piece.values()}
    for comp, touches, length, pi in infos:
        if touches or length >= 400.0 or id(comp) in keep_ids:
            continue
        for e in g.edges:
            if e["alive"] and e["a"] in comp:
                e["alive"] = False


def _connect_free_ends(g: _Graph, pav_eff):
    """Weld the two regimes into ONE graph: every degree-1 tip that is not
    already on the pavement boundary extends along its tangent (or snaps
    sideways a few metres) onto the nearest other spine piece.  The hit
    edge is SPLIT at the junction so the connection is topological — a
    weld that merely touches another way's polyline leaves the components
    separate and the runway-reach prune then wrongly kills them."""
    allow = shapely.buffer(pav_eff, 0.5)
    bnd = pav_eff.boundary
    for _round in range(3):
        changed = False
        for ni in range(len(g.nodes)):
            ends = [(ei, aa) for ei, aa in g.incident().get(ni, [])
                    if g.edges[ei]["alive"]]
            if len(ends) != 1:
                continue
            ei, at_a = ends[0]
            tip = g.nodes[ni]
            p_tip = Point(tuple(tip))
            if bnd.distance(p_tip) <= 2.0:
                continue                    # legitimate edge-of-pavement end
            u = g.edge_dir_at(ei, at_a)
            if u == (0.0, 0.0):
                continue
            u = (-u[0], -u[1])              # ray leaves the tip outward
            ray = LineString([tuple(tip), (tip[0] + u[0] * _WELD_REACH_M,
                                           tip[1] + u[1] * _WELD_REACH_M)])
            hit_ray = None                  # (dist, q, ej) via tangent ray
            hit_snap = None                 # (dist, q, ej) via sideways snap
            for ej, e in enumerate(g.edges):
                if ej == ei or not e["alive"]:
                    continue
                ln = LineString(e["cs"])
                inter = ray.intersection(ln)
                for q in getattr(inter, "geoms", [inter]):
                    if q.geom_type != "Point":
                        continue
                    d = q.distance(p_tip)
                    if d > 0.5 and (hit_ray is None or d < hit_ray[0]):
                        hit_ray = (d, q, ej)
                d = ln.distance(p_tip)
                if 0.5 < d <= _WELD_SNAP_M \
                        and (hit_snap is None or d < hit_snap[0]):
                    hit_snap = (d, nearest_points(ln, p_tip)[0], ej)
            best = hit_ray or hit_snap
            if best is None:
                # tangent to the pavement boundary (draw across to the edge)
                hitb = ray.intersection(bnd)
                ptsb = [q for q in getattr(hitb, "geoms", [hitb])
                        if q.geom_type == "Point"]
                if ptsb:
                    q = min(ptsb, key=lambda q: q.distance(p_tip))
                    d = q.distance(p_tip)
                    if d > 0.5:
                        seg = LineString([tuple(tip), (q.x, q.y)])
                        if allow.contains(seg):
                            g.add_edge(np.asarray(seg.coords), "weld",
                                       g.edges[ei]["size"], g.edges[ei]["w"])
                            changed = True
                continue
            d, q, ej = best
            seg = LineString([tuple(tip), (q.x, q.y)])
            if not allow.contains(seg):
                continue
            target_line = LineString(g.edges[ej]["cs"])
            mid = g.split_edge(ej, target_line.project(q))
            qn = g.nodes[mid]
            if float(np.hypot(qn[0] - tip[0], qn[1] - tip[1])) < 0.5:
                continue
            g.add_edge(np.vstack([[tip], [qn]]), "weld",
                       g.edges[ei]["size"], g.edges[ei]["w"])
            changed = True
        if not changed:
            break


def _select_routes(g: _Graph, runway_union,
                   keep_kinds=("lane",),
                   center_bonus: float = 0.9):
    """ANCHOR-ROUTED NETWORK (R1/R3): corridor trunk lanes and building
    frontage are the spine's fixed anchoring structure; everything else
    (hole rings, centered opening lines, welds) exists only as ROUTING
    material.  A conditional edge survives iff it lies on the shortest
    path between some pair of anchors — the taut connections an aircraft
    actually rolls; the rest of the candidate field is discarded.
    Centered lines get a small weight bonus so a route through an opening
    prefers the center over hugging one side (user rule h)."""
    import networkx as nx
    G = nx.Graph()
    cond = set()
    for ei, e in enumerate(g.edges):
        if not e["alive"]:
            continue
        cs = np.asarray(e["cs"])
        L = float(LineString(cs).length)
        chord = float(np.hypot(*(cs[-1] - cs[0])))
        # taxi routes are straight-by-design: a wiggly edge (boundary hug)
        # costs more than its length, so straights win where both exist
        curvy = min(2.0, L / max(chord, 1e-6))
        wgt = L * curvy * curvy \
            * (center_bonus if e["kind"] == "center" else 1.0)
        if G.has_edge(e["a"], e["b"]) \
                and G[e["a"]][e["b"]]["weight"] <= wgt:
            pass
        else:
            G.add_edge(e["a"], e["b"], weight=wgt, ei=ei)
        if e["kind"] not in keep_kinds:
            cond.add(ei)
    if not cond:
        return []
    # anchors: nodes owned by anchoring structure that also touch routing
    # material, plus runway-contact tips (rule b: the spine meets the
    # runway at every pavement intersection).
    kind_at = {}
    for e in g.edges:
        if not e["alive"]:
            continue
        for ni in (e["a"], e["b"]):
            kind_at.setdefault(ni, set()).add(e["kind"])
    rwy_b = runway_union.boundary if runway_union is not None \
        and not runway_union.is_empty else None
    anchors = []
    for ni, ks in kind_at.items():
        if ks & set(keep_kinds) and ks - set(keep_kinds):
            anchors.append(ni)
        elif rwy_b is not None and len(ks) == 1 \
                and rwy_b.distance(Point(tuple(g.nodes[ni]))) < 2.5:
            anchors.append(ni)
    # candidate pairs: local direct connections (distant anchors reuse the
    # trunk network; two anchors closer than a mouth are the same mouth)
    max_direct = 8.0 * 80.0                     # ~ a few openings
    pairs = []
    for k, src in enumerate(anchors):
        if src not in G:
            continue
        dist, paths = nx.single_source_dijkstra(G, src, weight="weight",
                                                cutoff=max_direct)
        for dst in anchors[k + 1:]:
            d = dist.get(dst)
            if d is None or d < 30.0:
                continue
            pairs.append((d, src, dst, paths[dst]))
    pairs.sort(key=lambda t: t[0])
    # GREEDY SPANNER (minimal network, no duplicate parallels): accept a
    # direct path only if the network built so far makes the pair detour
    # more than beta x direct.
    beta = 1.35
    S = nx.Graph()
    for ei, e in enumerate(g.edges):
        if e["alive"] and e["kind"] in keep_kinds:
            L = float(LineString(e["cs"]).length)
            if not S.has_edge(e["a"], e["b"]) \
                    or S[e["a"]][e["b"]]["weight"] > L:
                S.add_edge(e["a"], e["b"], weight=L)
    used = set()
    accepted = 0
    accepted_paths = []
    for d, src, dst, path in pairs:
        try:
            d_net = nx.shortest_path_length(S, src, dst, weight="weight")
        except Exception:
            d_net = None
        if d_net is not None and d_net <= beta * d:
            continue                            # already served
        accepted += 1
        accepted_paths.append(
            (path, [G[a][b]["ei"] for a, b in zip(path, path[1:])]))
        for a, b in zip(path, path[1:]):
            used.add(G[a][b]["ei"])
            wgt = G[a][b]["weight"]
            if not S.has_edge(a, b) or S[a][b]["weight"] > wgt:
                S.add_edge(a, b, weight=wgt)
    killed = 0
    for ei in cond:
        if ei not in used:
            g.edges[ei]["alive"] = False
            killed += 1
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] route selection: anchors={len(anchors)} "
              f"pairs={len(pairs)} accepted={accepted} "
              f"conditional={len(cond)} kept={len(cond) - killed}",
              flush=True)
    return accepted_paths


def _add_mouth_projections(g: _Graph, pav_eff, max_len: float = 650.0):
    """Rule (j)/R5 candidates: where a corridor centerline ends at open
    space (its mouth), PROJECT it straight across the opening to the far
    pavement edge — the user's look-ahead/access-lane geometry ('the
    longest straight route ... parallel to the taxiway we fed from' is
    this projection once routing keeps it).  Conditional routing material
    like the rings; selection decides which projections are real lanes."""
    bnd = pav_eff.boundary
    allow = shapely.buffer(pav_eff, 0.5)
    added = 0
    for ni, ends in list(g.incident().items()):
        live = [(ei, aa) for ei, aa in ends if g.edges[ei]["alive"]]
        if len(live) != 1 or g.edges[live[0][0]]["kind"] != "lane":
            continue
        ei, at_a = live[0]
        tip = g.nodes[ni]
        if bnd.distance(Point(tuple(tip))) <= 2.0:
            continue                    # already ends on the pavement edge
        u = g.edge_dir_at(ei, at_a)
        if u == (0.0, 0.0):
            continue
        u = (-u[0], -u[1])
        ray = LineString([tuple(tip), (tip[0] + u[0] * max_len,
                                       tip[1] + u[1] * max_len)])
        hit = ray.intersection(bnd)
        pts = [q for q in getattr(hit, "geoms", [hit])
               if q.geom_type == "Point"]
        if not pts:
            continue
        q = min(pts, key=lambda q: q.distance(Point(tuple(tip))))
        d = q.distance(Point(tuple(tip)))
        if d < 30.0:
            continue                    # a dangle-fix job, not a projection
        seg = LineString([tuple(tip), (q.x - u[0] * 0.1, q.y - u[1] * 0.1)])
        if not allow.contains(seg):
            continue
        g.add_edge(np.asarray(seg.coords), "proj", g.edges[ei]["size"],
                   g.edges[ei]["w"])
        added += 1
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] mouth projections: {added}", flush=True)


def _planarize_crossings(g: _Graph):
    """Node every geometric crossing between candidate edges so routes can
    turn there and the junction-arc pass can fire (target evidence: X
    crossings carry four mirrored arcs).  Both edges are split at the
    crossing; the split point welds via the node-key quantum."""
    from shapely.strtree import STRtree
    for _round in range(4):
        alive = [(ei, LineString(e["cs"])) for ei, e in enumerate(g.edges)
                 if e["alive"]]
        tree = STRtree([ln for _ei, ln in alive])
        crossed = False
        done_pairs = set()
        for k, (ei, ln) in enumerate(alive):
            if not g.edges[ei]["alive"]:
                continue
            for j in tree.query(ln):
                j = int(j)
                if j <= k:
                    continue
                ej = alive[j][0]
                if ej == ei or not g.edges[ej]["alive"] \
                        or (ei, ej) in done_pairs:
                    continue
                lnj = alive[j][1]
                if not ln.crosses(lnj):
                    continue
                inter = ln.intersection(lnj)
                pts = [q for q in getattr(inter, "geoms", [inter])
                       if q.geom_type == "Point"]
                for q in pts[:1]:
                    sa = ln.project(q)
                    sb = lnj.project(q)
                    if min(sa, ln.length - sa) < 1.5 \
                            or min(sb, lnj.length - sb) < 1.5:
                        continue        # endpoint touch, not a crossing
                    g.split_edge(ei, sa)
                    g.split_edge(ej, sb)
                    crossed = True
                done_pairs.add((ei, ej))
                if not g.edges[ei]["alive"]:
                    break
        if not crossed:
            break


def _area_access_hugs(g: _Graph, circ_pieces, w: float, sep: float,
                      pav_eff):
    """R5 (user ruling 2026-07-02): a corridor mouth opening into a LARGE
    open area gets an access lane — trace through the mouth, then hug the
    area's edge/holes at half-width in BOTH directions until the hug
    merges with another centerline (topologically, or by coming closer
    than the separation standard to one — rule h kills the parallel
    duplicate) or reaches the pavement edge.  After big-picture
    straightening this is the longest straight route through the area,
    parallel to the feeding taxiway.  Fires only where route selection
    left the area EMPTY (junction pieces already carry routes)."""
    # network as selected so far (alive edges) — merge/separation datum
    alive_lines = [LineString(e["cs"]) for e in g.edges if e["alive"]]
    if not alive_lines:
        return
    net = unary_union(alive_lines)
    inc_all = defaultdict(list)                  # over ALL edges, dead too
    for ei, e in enumerate(g.edges):
        inc_all[e["a"]].append(ei)
        inc_all[e["b"]].append(ei)
    revived = 0
    n_big = n_ringed = n_empty = n_started = 0
    for piece in circ_pieces:
        if piece.area < 5000.0:
            continue
        n_big += 1
        near = piece.buffer(3.0)
        ring_eis = [ei for ei, e in enumerate(g.edges)
                    if e["kind"] == "ring" and near.intersects(
                        Point(tuple(np.asarray(e["cs"])[len(e["cs"]) // 2])))]
        if not ring_eis:
            continue
        n_ringed += 1
        len_alive = sum(LineString(g.edges[ei]["cs"]).length
                        for ei in ring_eis if g.edges[ei]["alive"])
        len_all = sum(LineString(g.edges[ei]["cs"]).length
                      for ei in ring_eis)
        if len_alive > 0.25 * len_all:
            continue                             # area already served
        n_empty += 1
        ring_set = set(ring_eis)
        ring_nodes = set()
        for ei in ring_eis:
            ring_nodes.add(g.edges[ei]["a"])
            ring_nodes.add(g.edges[ei]["b"])
        # mouths: alive nodes welded (via a dead conditional link) to the
        # area's ring candidates
        starts = []
        for ni in ring_nodes:
            for ej in inc_all[ni]:
                e2 = g.edges[ej]
                if e2["alive"] or ej in ring_set:
                    continue
                other = e2["b"] if e2["a"] == ni else e2["a"]
                if any(g.edges[ek]["alive"]
                       and g.edges[ek]["kind"] == "lane"
                       for ek in inc_all[other]):
                    starts.append((ni, ej))
                    break
        n_started += 1 if starts else 0
        revived_lines = []                       # hugs see each other
        for start_node, link_ei in starts:
            walked = False
            for first_ei in inc_all[start_node]:
                if first_ei not in ring_set or g.edges[first_ei]["alive"]:
                    continue
                # walk this direction until merge/separation/end
                cur_node, cur_ei = start_node, first_ei
                dist_walked = 0.0
                while True:
                    e2 = g.edges[cur_ei]
                    ln2 = LineString(e2["cs"])
                    mid = ln2.interpolate(0.5, normalized=True)
                    d_near = net.distance(mid)
                    for rl in revived_lines:
                        d2 = rl.distance(mid)
                        if d2 < d_near:
                            d_near = d2
                    if dist_walked > 0.5 * sep and d_near < 0.9 * sep:
                        # rule h: parallel duplicate — connect back & stop
                        tipn = cur_node
                        q = nearest_points(net, Point(
                            tuple(g.nodes[tipn])))[0]
                        conn = LineString([tuple(g.nodes[tipn]),
                                           (q.x, q.y)])
                        if conn.length > 0.5 and shapely.buffer(
                                pav_eff, 0.5).contains(conn):
                            g.add_edge(np.asarray(conn.coords), "weld",
                                       e2["size"], e2["w"])
                        break
                    e2["alive"] = True
                    revived += 1
                    walked = True
                    revived_lines.append(ln2)
                    nxt_node = e2["b"] if e2["a"] == cur_node else e2["a"]
                    if any(g.edges[ek]["alive"] and ek != cur_ei
                           and g.edges[ek]["kind"] not in ("ring",)
                           for ek in inc_all[nxt_node]):
                        break                    # merged with the network
                    dist_walked += ln2.length
                    nxt = [ek for ek in inc_all[nxt_node]
                           if ek in ring_set and not g.edges[ek]["alive"]]
                    if not nxt:
                        break                    # pavement edge / ring end
                    cur_node, cur_ei = nxt_node, nxt[0]
            if walked and not g.edges[link_ei]["alive"]:
                g.edges[link_ei]["alive"] = True
                revived += 1
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] R5 area hugs: big={n_big} ringed={n_ringed} "
              f"empty={n_empty} with-mouth={n_started} "
              f"revived {revived} edges", flush=True)


def _trim_proj_tails(g: _Graph, runway_union):
    """A kept projection is a CONNECTOR: once it has served its last
    junction, the ray's tail to the far pavement edge is spam — trim it
    back to the last node where other spine meets it.  A projection with
    NO interior junction is the access lane itself (mouth → far edge,
    user R5/z4 case) and keeps its full length, as does a tail that ends
    on the runway edge (rule b contact)."""
    rwy_b = runway_union.boundary if runway_union is not None \
        and not runway_union.is_empty else None
    changed = True
    while changed:
        changed = False
        for ni, ends in list(g.incident().items()):
            live = [(ei, aa) for ei, aa in ends if g.edges[ei]["alive"]]
            if len(live) != 1 or g.edges[live[0][0]]["kind"] != "proj":
                continue
            if rwy_b is not None and rwy_b.distance(
                    Point(tuple(g.nodes[ni]))) < 2.5:
                continue                        # runway contact tail
            ei, at_a = live[0]
            e = g.edges[ei]
            other = e["b"] if at_a else e["a"]
            others = [(ej, aa) for ej, aa in g.incident().get(other, [])
                      if g.edges[ej]["alive"] and ej != ei]
            if len(others) < 2:
                continue                        # not a junction: keep access
            u_tail = g.edge_dir_at(ei, not at_a)    # arriving at `other`
            sibling = any(
                g.edges[ej]["kind"] == "proj"
                and _angle_deg(u_tail, g.edge_dir_at(ej, aa)) > 155.0
                for ej, aa in others)
            if sibling:
                e["alive"] = False              # overshoot past a junction
                changed = True


def _prune_leaf_kinds(g: _Graph, kinds=("center", "weld")):
    """Center lines and edge traces are BRIDGES by definition (an opening
    between spaces, a frontage between corridors) — a dead-end branch made
    of them is open-pavement skeleton noise the target leaves empty.
    Corridor lanes may dead-end (real stub taxiways)."""
    changed = True
    while changed:
        changed = False
        for ni, ends in list(g.incident().items()):
            live = [(ei, aa) for ei, aa in ends if g.edges[ei]["alive"]]
            if len(live) != 1:
                continue
            ei, _aa = live[0]
            if g.edges[ei]["kind"] in kinds:
                g.edges[ei]["alive"] = False
                changed = True


def _max_chords(cs, d_orig, w, allow, bnd):
    """Maximal-straight-chord vertex subset for one polyline.  TWO-TIER
    rule (user rule i): a chord that keeps ~full w clearance may cut
    edge-following wiggles generously (big-picture straight beats the
    trace); a chord that relies on relaxed clearance (pinched corridor)
    must stay tight on the original centered path."""
    n = len(cs)
    out = [0]
    i = 0
    while i < n - 1:
        j = n - 1
        chosen = i + 1
        while j > i + 1:
            a, b = cs[i], cs[j]
            ab = b - a
            L = float(np.hypot(*ab))
            if L < 1e-6:
                j -= 1
                continue
            rel = cs[i:j + 1] - a
            dev = float((np.abs(rel[:, 0] * ab[1] - rel[:, 1] * ab[0]) / L)
                        .max())
            ln = LineString([tuple(a), tuple(b)])
            ok = dev <= 1.5 * w and allow.contains(ln)
            if ok:
                m = max(2, int(L / 6.0))
                clear = min(bnd.distance(ln.interpolate(k * L / m))
                            for k in range(1, m)) if L > 6.0 else \
                    bnd.distance(ln.interpolate(0.5, normalized=True))
                if clear >= 0.9 * w:
                    pass                       # full-clearance chord: take it
                elif dev <= 3.0 and clear >= max(
                        1.0, float(d_orig[i:j + 1].min()) - 0.5):
                    pass                       # tight chord through a pinch
                else:
                    ok = False
            if ok:
                chosen = j
                break
            j = i + max(1, int((j - i) * 0.7))
        out.append(chosen)
        i = chosen
    return out


def _straighten_routes(g: _Graph, accepted_paths, pav_eff, w: float):
    """Rule (i) applied to each accepted anchor-to-anchor ROUTE as one
    unit: chords span ring wiggles and weld kinks across every node of the
    route, producing the tangent straights the target draws through open
    space (through-path assembly alone stays pinned at ring junctions)."""
    paths = []
    for node_path, eis in accepted_paths:
        path = []
        for k, ei in enumerate(eis):
            e = g.edges[ei]
            if not e["alive"]:
                path = None
                break
            path.append((ei, bool(e["a"] == node_path[k])))
        if path:
            paths.append(path)
    _straighten_path_list(g, paths, pav_eff, w)


def _fit_line(pts):
    """PCA line fit → (centroid, unit direction oriented along the run)."""
    p0 = pts.mean(axis=0)
    q = pts - p0
    cov = q.T @ q
    evals, evecs = np.linalg.eigh(cov)
    u = evecs[:, -1]
    if u @ (pts[-1] - pts[0]) < 0:
        u = -u
    return p0, u


def _refit_chain(cs, d_orig_fn, w, r_std, allow, bnd):
    """N1 FILLET-CHAIN REFIT: replace a path polyline with fitted tangent
    geometry — least-squares straights on its low-curvature runs, one
    tangent arc per turn (radius = the route's own curvature clamped to
    the standards band, standard as fallback).  This is design fitting,
    not vertex decimation: chords that merely connect existing vertices
    displace arcs into corners and lose alignment (measured net-negative).
    Returns the new LineString or None (caller falls back to chords)."""
    ln = LineString(cs)
    if ln.length < 60.0:
        return None
    n = int(ln.length / 4.0)
    P = np.asarray([ln.interpolate(k * ln.length / n).coords[0]
                    for k in range(n + 1)])
    d = P[1:] - P[:-1]
    th = np.unwrap(np.arctan2(d[:, 1], d[:, 0]))
    dth = np.abs(np.diff(th)) / 4.0             # curvature rad/m
    turn = dth > 1.0 / (3.0 * r_std)
    # the refit is for paths with real turn content (edge-hugging rings,
    # junction curves); straight corridors keep the chord treatment that
    # already matches (blanket refit measured net-negative there)
    if float(turn.mean()) < 0.12:
        return None
    # straight runs = maximal non-turn stretches (indices into P)
    runs = []
    i = 0
    m = len(turn)
    while i < m:
        if turn[i]:
            i += 1
            continue
        j = i
        while j < m and not turn[j]:
            j += 1
        if (j - i) * 4.0 >= max(24.0, 1.2 * r_std):
            runs.append((i, j + 1))             # P[i..j+1] inclusive
        i = j
    if len(runs) < 2:
        return None
    fits = [(_fit_line(P[a:b]), a, b) for a, b in runs]
    out = [P[0]]
    ok_all = True
    for k in range(len(fits) - 1):
        (p1, u1), a1, b1 = fits[k]
        (p2, u2), a2, b2 = fits[k + 1]
        den = u1[0] * u2[1] - u1[1] * u2[0]
        gamma = _angle_deg(tuple(u1), tuple(u2))
        if abs(den) < math.sin(math.radians(8.0)) or gamma > 155.0:
            # near-parallel jog or fold-back: keep original geometry
            out.extend(P[b1 - 1:a2 + 1])
            continue
        dp = p2 - p1
        s1 = (dp[0] * u2[1] - dp[1] * u2[0]) / den
        corner = p1 + u1 * s1
        # radius: the route's own turn curvature, clamped to standards
        zone = dth[max(0, b1 - 1):min(m, a2 + 1)]
        r_est = 1.0 / max(float(np.median(zone)), 1e-6) if len(zone) else \
            r_std
        placed = False
        for r in (min(max(r_est, 0.4 * r_std), 3.0 * r_std), r_std):
            rr = r
            while rr >= 0.35 * r_std:
                arc, t = _fillet(tuple(corner), tuple(u1), tuple(u2), rr)
                if arc is None:
                    break
                # tangent points must stay within each run's extent (slack)
                ta = corner - u1 * t
                tb = corner + u2 * t
                if np.hypot(*(ta - out[-1])) < 2.0 \
                        or (ta - out[-1]) @ u1 < 0.0:
                    rr *= 0.8
                    continue
                if allow.contains(LineString(arc)) \
                        and allow.contains(LineString([tuple(out[-1]),
                                                       tuple(ta)])):
                    out.append(ta)
                    out.extend(np.asarray(arc)[1:-1])
                    out.append(tb)
                    placed = True
                    break
                rr *= 0.8
            if placed:
                break
        if not placed:
            out.extend(P[b1 - 1:a2 + 1])
            ok_all = False
    out.append(P[-1])
    pts = np.asarray([p for p in out], dtype=float)
    keep = [0]
    for k in range(1, len(pts)):
        if np.hypot(*(pts[k] - pts[keep[-1]])) > 0.05:
            keep.append(k)
    if len(keep) < 2:
        return None
    new_line = LineString(pts[keep])
    if not allow.contains(new_line):
        return None
    # the refit must stay a refit: bounded deviation from the original
    md = max(ln.distance(Point(tuple(pts[k])))
             for k in keep[:: max(1, len(keep) // 40)])
    if md > 0.8 * w:
        return None
    return new_line


def _straighten_paths(g: _Graph, pav_eff, w: float, r_std: float = 30.0):
    """BIG-PICTURE STRAIGHT (user rule i) applied to THROUGH-PATHS: target
    straights run through junction after junction, so chords must span
    node-to-node fragments.  Interior junction nodes are moved onto the
    straightened line (side branches follow via move_node)."""
    _straighten_path_list(g, _assemble_through_paths(g), pav_eff, w, r_std)


def _straighten_path_list(g: _Graph, paths, pav_eff, w: float,
                          r_std: float = 30.0):
    from shapely.ops import substring
    bnd = pav_eff.boundary
    allow = shapely.buffer(pav_eff, 0.5)
    for path in paths:
        cs, _rr, node_seq = _collect_path(g, path)
        if len(cs) < 3:
            continue
        d_orig = np.asarray([bnd.distance(Point(tuple(p))) for p in cs])
        new_line = None
        if not os.environ.get("O4_ET_NO_REFIT"):
            new_line = _refit_chain(cs, None, w, r_std, allow, bnd)
        if new_line is None:
            keep = _max_chords(cs, d_orig, w, allow, bnd)
            new_line = LineString(cs[keep])
        # each edge boundary index in cs: recompute by walking the path
        idx = [0]
        pos = 0
        for (ei, at_a) in path:
            pos += len(g.edges[ei]["cs"]) - 1
            idx.append(pos)
        proj_s = [new_line.project(Point(tuple(cs[k]))) for k in idx]
        if any(proj_s[k + 1] < proj_s[k] - 1.0
               for k in range(len(proj_s) - 1)):
            continue                    # refit folded relative to the nodes
        # move interior nodes onto the straightened line, then rebuild each
        # edge's polyline as the straightened portion between its nodes
        for k, ni in enumerate(node_seq):
            p = new_line.interpolate(proj_s[k])
            if float(np.hypot(p.x - g.nodes[ni][0],
                              p.y - g.nodes[ni][1])) > 0.01:
                g.move_node(ni, [p.x, p.y])
        for m2, (ei, at_a) in enumerate(path):
            s0, s1 = proj_s[m2], proj_s[m2 + 1]
            if s1 < s0:
                s0, s1 = s1, s0
            if s1 - s0 < 0.2:
                mid_pts = []
            else:
                piece = substring(new_line, s0 + 0.1, s1 - 0.1)
                mid_pts = [list(c) for c in piece.coords] \
                    if piece.geom_type == "LineString" else []
            pts = [list(g.nodes[node_seq[m2]])] + mid_pts + \
                [list(g.nodes[node_seq[m2 + 1]])]
            if not at_a:
                pts = pts[::-1]
            e = g.edges[ei]
            if len(pts) >= 2:
                cs_new = np.asarray(pts, dtype=float)
                cs_new[0] = g.nodes[e["a"]]
                cs_new[-1] = g.nodes[e["b"]]
                e["cs"] = cs_new


def synthesize_spine_v8(
    pav, runway_union=None, buildings=None, routes=None, *,
    terminal_setback: float = 100.0,
) -> list[SpineWay]:
    """Edge-trace spine (module docstring).  ``routes`` supplies ICAO size
    letters ONLY — geometry never comes from it."""
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

    chains = build_pavement_skeleton(pav_nav, runway_union=runway_union)
    w = _dominant_halfwidth(chains)
    size = _dominant_size(routes, w)
    sep = SEPARATION_BY_SIZE.get(size, 66.5)
    sep_half = sep / 2.0
    h = (sep - 2.0 * w) / 2.0
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] w={w:.1f} size={size} sep={sep} h={h:.1f}",
              flush=True)

    axes = _runway_axes(runway_union)
    rwy_zone = shapely.buffer(runway_union, 25.0) \
        if runway_union is not None and not runway_union.is_empty else None

    # regime A — corridor centerlines: pavement medial where the pavement
    # is corridor-width (the trace's two sides coincide there).  The
    # service cutoff SCALES with the airport: a strip much narrower than
    # the dominant taxiway width is a service road / stand row, whatever
    # its absolute width (fixed 5.5 m let stand-row strips at an E-width
    # airport spawn trunk anchors and outline every pocket).
    svc_cut = max(_SVC_HALFWIDTH_M, 0.35 * w)
    kept_runs = []                              # (coords, radii, w_med)
    corridors = []                              # unambiguous corridor arms
    for ch in chains:
        w_med = float(np.median(ch.radii)) if ch.radii else 8.0
        if w_med < svc_cut:
            continue                            # service road, not taxi spine
        if _is_runway_shoulder(ch, w_med, rwy_zone, axes):
            continue
        if w_med <= 1.3 * w and ch.line.length >= 40.0:
            corridors.append(ch.line)
        for keep, cs, rr in _split_by_clearance(ch, 1.1 * w):
            if not keep:
                continue
            if LineString(cs).length < 4.0:
                continue
            kept_runs.append((cs, rr, float(np.median(rr))))

    core = shapely.buffer(pav_eff, -w)
    # dead-end bays: parking space the target leaves empty (the corridor
    # arriving at a lone mouth crosses straight to the far edge instead).
    circ, bays = _circulation_pieces(core, corridors, w, sep)
    bay_union = unary_union([b.buffer(0.5) for b in bays]) if bays else None
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] core pieces: circ={len(circ)} bays={len(bays)}",
              flush=True)

    # regime B — openings wider than 2w but narrower than the separation
    # standard get ONE spine at the CENTER of the opening = medial of the
    # eroded space where it is thinner than 2h.  Only genuine PARALLEL
    # gaps qualify (run much longer than the opening is wide); short
    # oblique core-medial scraps in junction interiors are not openings —
    # routing hugs the islands there instead (user junction evidence).
    center_runs = []
    if not core.is_empty:
        for ch in build_pavement_skeleton(core, runway_union=None):
            for keep, cs, rr in _split_by_clearance(ch, h):
                if not keep:
                    continue
                run = LineString(cs)
                opening = 2.0 * (float(np.median(rr)) + w)
                if run.length < max(1.5 * opening, 30.0):
                    continue
                center_runs.append((cs, rr))

    g = _Graph()
    trunk_lines = []
    for cs, rr, w_med in kept_runs:
        if bay_union is not None and bay_union.contains(
                LineString(cs).interpolate(0.5, normalized=True)):
            continue                            # bay-interior medial
        g.add_edge(cs, "lane", "", w_med)
        trunk_lines.append(LineString(cs))
    trunk_union = unary_union(trunk_lines) if trunk_lines else None
    for cs, rr in center_runs:
        if bay_union is not None and bay_union.contains(
                LineString(cs).interpolate(0.5, normalized=True)):
            continue
        g.add_edge(cs, "center", "", w + float(np.median(rr)))

    # regime C — edge traces: building frontage (kept) + hole rings
    # (routing candidates).
    for kind, t in _edge_traces(core, pav_eff, w, runway_union,
                                building_union, trunk_union):
        if bay_union is not None and bay_union.contains(
                t.interpolate(0.5, normalized=True)):
            continue
        g.add_edge(np.asarray(t.coords), kind, "", w)

    _add_mouth_projections(g, pav_eff)
    _planarize_crossings(g)
    _connect_free_ends(g, pav_eff)
    if not os.environ.get("O4_ET_NO_SELECT"):
        _select_routes(g, runway_union)
    # R5 granularity: open-space BODIES = core opened at h (necks between
    # sub-separation connections cut), so a big apron is its own area even
    # when the eroded space is globally connected.
    area_bodies = []
    if not core.is_empty and h > 0.5:
        area_bodies = _polygons(shapely.buffer(
            shapely.buffer(core, -h), h + 0.05))
    _area_access_hugs(g, area_bodies, w, sep, pav_eff)
    _trim_proj_tails(g, runway_union)
    # NOTE: straightening each accepted route as one unit (endpoints at
    # anchors) was tried and is NET-NEGATIVE (coverage 60.8->59.1,
    # alignment 1.21->1.43): chords cut curves the target keeps.  The
    # through-path straightener below is the keeper.
    _prune_no_runway_components(g, runway_union, pav_eff)
    _fix_dangles(g, pav_eff)
    if not os.environ.get("O4_ET_KINDS"):        # keep kinds for debug only
        for e in g.edges:
            e["kind"] = "lane"
    g.consolidate()
    _straighten_paths(g, pav_eff, w, R90_BY_SIZE.get(size, 30.0))
    g.consolidate()

    # standard mirrored arcs at junction turns + runway diagonal hooks
    allow = shapely.buffer(pav_eff, 0.5)

    def pav_ok(line: LineString) -> bool:
        return allow.contains(line)

    _attribute_sizes(g, routes)
    bnd_arc = pav_eff.boundary

    def r_start_for(P, r_std):
        # wide-open junction crossings take the biggest mirrored arcs that
        # fit; the local clearance at the node is the openness measure
        return min(1.6 * r_std, max(r_std, bnd_arc.distance(Point(tuple(P)))))

    _add_junction_arcs(g, pav_ok, runway_union, r_start_for=r_start_for)
    _add_runway_turns(g, runway_union, pav_eff)
    _fix_dangles(g, pav_eff)
    g.consolidate()
    return g.ways()
