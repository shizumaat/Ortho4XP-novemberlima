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

# Recognized painted centerlines are the DESIGN GEOMETRY where they exist
# (user ruling 2026-07-02: the hand-edited target measures median 1.3 m
# from the recognized-paint network — the target IS a selected subset of
# the paint).  Pavement-inferred candidates within this fraction of w of
# paint are duplicates and yield to it.
_PAINT_DEDUP_FRAC = 0.5


def _paint_candidates(recognized, pav_eff, w, corridors):
    """Recognized-paint lines as spine candidates: clipped to the taxi
    pavement (paint crossing a runway is cut at the runway edge — rule b
    contact machinery re-attaches the tips), split into corridor TRUNK
    (rides a regime-A corridor arm) vs conditional routing material.
    Returns (list[(cs, kind)], paint_union)."""
    corridor_union = unary_union(corridors) if corridors else None
    # recognition emits most centerlines TWICE (near-identical pieces) and
    # chains that share long stretches — dedup by coverage, longest first,
    # or the network carries a phantom twin of nearly every lane
    raw = []
    for ln in recognized or []:
        if ln is None or ln.is_empty or ln.length < 8.0:
            continue
        clipped = ln.intersection(shapely.buffer(pav_eff, 0.25))
        for p in ([clipped] if clipped.geom_type == "LineString"
                  else list(getattr(clipped, "geoms", []))):
            if p.geom_type == "LineString" and p.length >= 8.0:
                raw.append(p)
    raw.sort(key=lambda p: -p.length)
    dedup, kept_u = [], None
    for p in raw:
        if kept_u is None:
            dedup.append(p)
            kept_u = p
            continue
        rem = p.difference(shapely.buffer(kept_u, 2.0))
        parts = [rem] if rem.geom_type == "LineString" else \
            list(getattr(rem, "geoms", []))
        fresh = []
        for q in parts:
            if q.geom_type != "LineString" or q.length < 8.0:
                continue
            # re-attach the cut ends: the dedup cut leaves a ~2 m gap to
            # the kept twin — snap each free end onto it so the novel
            # stretch stays welded to the network instead of orphaning
            cs_q = list(q.coords)
            for end, idx in ((0, 0), (-1, len(cs_q))):
                pt = Point(cs_q[end])
                if kept_u.distance(pt) <= 3.0:
                    np_, _ = nearest_points(kept_u, pt)
                    if end == 0:
                        cs_q.insert(0, (np_.x, np_.y))
                    else:
                        cs_q.append((np_.x, np_.y))
            fresh.append(LineString(cs_q))
        if not fresh:
            continue
        # only genuinely new stretches survive; a piece that is mostly
        # covered contributes just its novel parts (welds re-attach them)
        dedup.extend(fresh)
        kept_u = unary_union([kept_u, *fresh])

    out = []
    kept_lines = []
    from shapely.ops import substring
    for whole in dedup:
        whole = whole.simplify(0.2)
        # classify LOCALLY: long painted lines run corridor->apron->
        # corridor, so trunk-vs-conditional is a per-chunk decision
        n_chunk = max(1, int(round(whole.length / 120.0)))
        step = whole.length / n_chunk
        for k in range(n_chunk):
            p = substring(whole, k * step, min((k + 1) * step,
                                               whole.length))
            if p.geom_type != "LineString" or p.length < 4.0:
                continue
            cs = np.asarray(p.coords, dtype=float)
            # recognition jitter cleanup: a near-straight painted run
            # is a design STRAIGHT (the hand target draws it clean) —
            # drop the wiggle, keep the welded endpoints
            if len(cs) > 2:
                a, b = cs[0], cs[-1]
                u = b - a
                L = float(np.hypot(*u))
                if L > 1.0:
                    u = u / L
                    dev = np.abs((cs - a) @ np.asarray([-u[1], u[0]]))
                    if float(dev.max()) < 1.2:
                        cs = np.asarray([a, b])
            out.append([cs, "paint"])
            kept_lines.append(LineString(cs))
    # TRUNK = the painted centerline OF a corridor, decided by NEAREST-
    # PAINT VOTING: each corridor-medial sample elects the closest paint
    # chunk; a chunk that wins a majority of its own length is that
    # corridor's centerline.  (Absolute-distance thresholds fail both
    # ways: 0.6w sweeps every parallel stand comb into 53 km of
    # unprunable trunk, 0.2w orphans real centerlines on asymmetric
    # shoulders.)
    if corridors and out:
        from shapely.strtree import STRtree
        chunk_lines = [LineString(cs) for cs, _k in out]
        tree = STRtree(chunk_lines)
        votes = Counter()
        for cor in corridors:
            n = max(2, int(cor.length / 15.0))
            for t in np.linspace(0.0, 1.0, n):
                q = cor.interpolate(t, normalized=True)
                i = int(tree.nearest(q))
                if chunk_lines[i].distance(q) <= 0.7 * w:
                    votes[i] += 1
        for i, v in votes.items():
            if v * 15.0 >= 0.5 * chunk_lines[i].length:
                out[i][1] = "paint_trunk"
    paint_union = unary_union(kept_lines) if kept_lines else None
    return out, paint_union


def _duplicates_paint(cs, paint_union, thr: float) -> bool:
    """True when the candidate polyline runs along existing paint — every
    probe sample within ``thr`` — so the paint carries that corridor."""
    if paint_union is None:
        return False
    ln = LineString(cs)
    n = max(3, min(9, int(ln.length / 25.0) + 2))
    for t in np.linspace(0.0, 1.0, n):
        if paint_union.distance(ln.interpolate(t, normalized=True)) > thr:
            return False
    return True


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
    sel = (a >= edges[k] - 2.0) & (a <= edges[k] + 2.0)
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


def _strip_stand_combs(g: _Graph):
    """PARKING IS NOT TAXIING (user deletion criterion): short dead-end
    painted lead-ins into stands are parking guidance, never spine — but
    each comb's attachment point marks a STAND the network must serve, so
    the roots come back as movement-demand anchors for selection."""
    demand = set()
    changed = True
    while changed:
        changed = False
        deg = Counter()
        for e in g.edges:
            if e["alive"]:
                deg[e["a"]] += 1
                deg[e["b"]] += 1
        for e in g.edges:
            if not e["alive"] or e["kind"] != "paint":
                continue
            if LineString(e["cs"]).length > 60.0:
                continue
            leaf_a, leaf_b = deg[e["a"]] == 1, deg[e["b"]] == 1
            if leaf_a == leaf_b:
                continue                        # interior or isolated
            e["alive"] = False
            demand.add(e["a"] if leaf_b else e["b"])
            changed = True
    demand = {ni for ni in demand
              if any(e["alive"] and ni in (e["a"], e["b"])
                     for e in g.edges)}
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] stand combs stripped; demand anchors: "
              f"{len(demand)}", flush=True)
    return demand


def _enforce_min_separation(g: _Graph, w: float, sep: float, pav_eff,
                            demand=()):
    """USER WIDTH RULE (hard invariant, 2026-07-02): two spine lines may
    run parallel through the SAME pavement space only when separated by
    the taxiway-to-taxiway standard; closer pairs keep ONE line.  A
    pavement hole/island/building between the pair means separate spaces
    — the racetrack sides around a stand island both live.  The worse
    edge of a violating pair dies (rank: corridor trunk > other paint >
    inferred lane; longer beats shorter)."""
    from shapely.prepared import prep
    from shapely.strtree import STRtree
    allow = prep(shapely.buffer(pav_eff, 0.5))
    rank = {"paint_trunk": 0, "lane": 1, "center": 1, "paint": 2}
    alive = [(ei, e) for ei, e in enumerate(g.edges)
             if e["alive"] and e["kind"] in rank]
    if not alive:
        return
    lines = [LineString(e["cs"]) for _ei, e in alive]
    order = sorted(range(len(alive)), key=lambda i: (
        -rank.get(alive[i][1]["kind"], 9), lines[i].length))
    tree = STRtree(lines)
    demand_pts = [Point(tuple(g.nodes[ni])) for ni in demand]
    killed = 0
    dead_local = set()
    for i in order:
        ei, e = alive[i]
        ln = lines[i]
        n = max(3, int(ln.length / 10.0))
        samples = [ln.interpolate(t, normalized=True)
                   for t in np.linspace(0.05, 0.95, n)]
        cands = [int(j) for j in tree.query(
            ln.buffer(0.95 * sep))
            if int(j) != i and int(j) not in dead_local]
        if not cands:
            continue
        my_rank = rank.get(e["kind"], 9)
        viol = 0
        for q in samples:
            t0 = ln.project(q)
            a0 = ln.interpolate(max(0.0, t0 - 3.0))
            a1 = ln.interpolate(min(ln.length, t0 + 3.0))
            u = np.asarray([a1.x - a0.x, a1.y - a0.y])
            nu = float(np.hypot(*u))
            if nu < 1e-6:
                continue
            u /= nu
            for j in cands:
                oj, eo = alive[j]
                lo = lines[j]
                r2 = rank.get(eo["kind"], 9)
                better = (r2, -lo.length) < (my_rank, -ln.length)
                if not better:
                    continue
                d = lo.distance(q)
                if not (3.0 <= d <= 0.95 * sep):
                    continue
                s0 = lo.project(q)
                b0 = lo.interpolate(max(0.0, s0 - 3.0))
                b1 = lo.interpolate(min(lo.length, s0 + 3.0))
                v = np.asarray([b1.x - b0.x, b1.y - b0.y])
                nv = float(np.hypot(*v))
                if nv < 1e-6:
                    continue
                v /= nv
                ang = _angle_deg(tuple(u), tuple(v))
                ang = min(ang, 180.0 - ang)
                if ang > 25.0:
                    continue
                p2 = lo.interpolate(s0)
                conn = LineString([(q.x, q.y), (p2.x, p2.y)])
                if conn.length >= 1.0 and not allow.covers(conn):
                    continue                    # a hole separates them
                viol += 1
                break
        if viol >= 0.75 * len(samples):
            # a line that serves stands directly is the frontage the
            # demand anchors live on — never the duplicate
            if demand_pts and any(ln.distance(q) < 2.0
                                  for q in demand_pts):
                continue
            e["alive"] = False
            dead_local.add(i)
            killed += 1
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] width invariant: killed {killed} parallel "
              f"duplicates", flush=True)


def _prune_parking_paint(g: _Graph, ramps, radius: float = 50.0,
                         majority: float = 0.6):
    """PARKING IS NOT TAXIING (user deletion criterion, measured 46%-vs-2%
    discrimination on SPJC): spine material that lives among the apt.dat
    parking positions is stand guidance, not taxi routing.  Kills any
    edge with a majority of samples within ``radius`` of a ramp start."""
    if not ramps:
        return
    ramp_u = unary_union([Point(x, y) for x, y in ramps])
    killed = 0
    for e in g.edges:
        if not e["alive"]:
            continue
        ln = LineString(e["cs"])
        n = max(3, int(ln.length / 12.0))
        near = sum(1 for t in np.linspace(0.05, 0.95, n)
                   if ramp_u.distance(
                       ln.interpolate(t, normalized=True)) <= radius)
        if near >= majority * n:
            e["alive"] = False
            killed += 1
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] parking prune: killed {killed} edges near "
              f"{len(ramps)} ramp starts", flush=True)


def _prune_paint_leaves(g: _Graph, runway_union, pav_eff, demand=()):
    """NO-MOVEMENT-NEEDS-IT: iteratively drop dead-end painted scraps
    whose free tip serves nothing — not a runway contact, not a pavement-
    edge termination, not a stand-demand anchor."""
    rwy_b = runway_union.boundary if runway_union is not None \
        and not runway_union.is_empty else None
    bnd = pav_eff.boundary
    demand = set(demand)
    removed = 0
    changed = True
    while changed:
        changed = False
        deg = Counter()
        for e in g.edges:
            if e["alive"]:
                deg[e["a"]] += 1
                deg[e["b"]] += 1
        for e in g.edges:
            if not e["alive"] or not e["kind"].startswith("paint"):
                continue
            for tip, other in ((e["a"], e["b"]), (e["b"], e["a"])):
                if deg[tip] != 1 or tip in demand:
                    continue
                p = Point(tuple(g.nodes[tip]))
                if rwy_b is not None and rwy_b.distance(p) < 6.0:
                    continue
                if bnd.distance(p) < 10.0:
                    continue
                e["alive"] = False
                removed += 1
                changed = True
                break
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] paint leaf prune: removed {removed}",
              flush=True)


def _select_routes(g: _Graph, runway_union,
                   keep_kinds=("lane", "paint_trunk"),
                   center_bonus: float = 0.9, extra_anchors=()):
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
        # costs more than its length, so straights win where both exist.
        # Painted lines are exempt — their curves ARE the design, and the
        # router must prefer them over any inferred alternative.
        if e["kind"].startswith("paint"):
            wgt = 0.85 * L
        else:
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
    anchors.extend(ni for ni in extra_anchors
                   if ni in kind_at and ni not in set(anchors))
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
        if len(live) != 1 or g.edges[live[0][0]]["kind"] not in (
                "lane", "paint_trunk", "paint"):
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
        # any selected line (e.g. PAINT) crossing the area serves it too
        try:
            if net.intersection(piece).length > 0.25 * len_all:
                continue
        except Exception:
            pass
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


def _close_floating_tips(g: _Graph, pav_eff, building_union):
    """FLOATING-ENDS GATE = 0: any degree-1 tip that is not a legitimate
    termination (pavement edge, building face) welds straight onto the
    nearest way (splitting it — topological, like every weld), and a tip
    with nothing reachable trims back.  Runs LAST, after arcs."""
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
            p = Point(tuple(tip))
            if bnd.distance(p) <= 2.0:
                continue
            if building_union is not None \
                    and building_union.distance(p) <= 2.0:
                continue
            best = None
            for ej, e2 in enumerate(g.edges):
                if ej == ei or not e2["alive"]:
                    continue
                ln2 = LineString(e2["cs"])
                d = ln2.distance(p)
                if d <= 70.0 and (best is None or d < best[0]):
                    best = (d, ej, ln2)
            done = False
            if best is not None and best[0] > 0.5:
                d, ej, ln2 = best
                q = nearest_points(ln2, p)[0]
                seg = LineString([tuple(tip), (q.x, q.y)])
                if allow.contains(seg):
                    mid = g.split_edge(ej, ln2.project(q))
                    qn = g.nodes[mid]
                    if float(np.hypot(qn[0] - tip[0],
                                      qn[1] - tip[1])) > 0.5:
                        g.add_edge(np.vstack([[tip], [qn]]), "lane",
                                   g.edges[ei]["size"], g.edges[ei]["w"])
                    done = True
                    changed = True
            elif best is not None:
                # touching but unnoded: split there so the weld is real
                d, ej, ln2 = best
                q = nearest_points(ln2, p)[0]
                mid = g.split_edge(ej, ln2.project(q))
                if mid != ni:
                    g.move_node(ni, list(g.nodes[mid]))
                done = True
                changed = True
            if not done:
                # the straight run to the NEAREST boundary point never
                # leaves the pavement — draw the tip out to the edge
                qb = nearest_points(bnd, p)[0]
                db = qb.distance(p)
                if 0.5 < db <= 40.0:
                    g.add_edge(np.vstack([[tip], [[qb.x, qb.y]]]), "lane",
                               g.edges[ei]["size"], g.edges[ei]["w"])
                    done = True
                    changed = True
            if not done:
                e = g.edges[ei]
                if LineString(e["cs"]).length < 120.0:
                    e["alive"] = False
                    changed = True
        if not changed:
            break


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
            ok = allow.contains(ln)
            if ok:
                m = max(2, int(L / 6.0))
                clear = min(bnd.distance(ln.interpolate(k * L / m))
                            for k in range(1, m)) if L > 6.0 else \
                    bnd.distance(ln.interpolate(0.5, normalized=True))
                if clear >= 0.9 * w:
                    # full-clearance chord: STRAIGHT WINS with no deviation
                    # cap — user rule (i) verbatim ("straight whenever the
                    # line keeps >=w clearance everywhere"); capping it
                    # was what blocked the big apron sweeps
                    pass
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


def _straighten_routes(g: _Graph, accepted_paths, pav_eff, w: float,
                       r_std: float = 30.0):
    """Rule (i) applied to each accepted anchor-to-anchor ROUTE: the taut
    version of the boundary-hugging geometry the router had to use.
    Routes split only at REAL junctions — nodes shared by another
    accepted route or touching a corridor lane — never at mere weld
    attachments (deg>=3 splitting chopped weld geometry and measured
    net-negative; unsplit chopping cut real turns and measured
    net-negative; route-use is the discriminator both needed)."""
    _visibility_reroute(g, accepted_paths, pav_eff, w, r_std)
    from collections import Counter as _Counter
    node_use: _Counter = _Counter()
    for node_path, _eis in accepted_paths:
        for ni in set(node_path):
            node_use[ni] += 1
    trunk_nodes = set()
    for e in g.edges:
        if e["alive"] and e["kind"] in ("lane", "paint_trunk"):
            trunk_nodes.add(e["a"])
            trunk_nodes.add(e["b"])
    paths = []
    for node_path, eis in accepted_paths:
        cur = []
        for k, ei in enumerate(eis):
            e = g.edges[ei]
            # painted stretches are design geometry — never tautened;
            # they bound the pieces the straightener may touch
            if not e["alive"] or e["kind"].startswith("paint"):
                if cur:
                    paths.append(cur)
                cur = []
                continue
            cur.append((ei, bool(e["a"] == node_path[k])))
            end_node = node_path[k + 1]
            if node_use[end_node] >= 2 or end_node in trunk_nodes:
                paths.append(cur)
                cur = []
        if cur:
            paths.append(cur)
    _straighten_path_list(g, [p for p in paths if p], pav_eff, w, r_std,
                          taut=True)


def _visibility_reroute(g: _Graph, accepted_paths, pav_eff, w: float,
                        r_std: float = 30.0):
    """PER-SEGMENT VISIBILITY ROUTER: wherever an accepted route crosses
    a free-space component (pavement eroded by the full half-width w, so
    splices land at the TARGET's offset), the stretch between entry and
    exit is replaced by the taut visibility path — but only on a STRONG
    gain (<=0.90x), so borderline reroutes never nudge matched geometry
    at the 5m/1m tolerances (the 0.9w/0.97x variant measured negative)."""
    import networkx as nx
    from shapely.prepared import prep
    from shapely.strtree import STRtree
    F = shapely.buffer(pav_eff, -w)
    pieces = [p for p in _polygons(F) if p.area > 2500.0]
    if not pieces:
        return
    tree = STRtree(pieces)
    prepped = [prep(p) for p in pieces]
    allow = shapely.buffer(pav_eff, 0.5)
    cache: dict = {}

    def vis_graph(pi):
        if pi in cache:
            return cache[pi]
        poly = pieces[pi]
        verts = []
        for r in [poly.exterior] + list(poly.interiors):
            rl = LineString(r.coords).simplify(2.0)
            verts.extend(tuple(c) for c in list(rl.coords)[:-1])
        pp = prep(poly.buffer(0.1))
        G = nx.Graph()
        n = len(verts)
        for i in range(n):
            G.add_node(i)
            for j in range(i + 1, n):
                d = math.hypot(verts[i][0] - verts[j][0],
                               verts[i][1] - verts[j][1])
                if 0.5 < d <= 700.0 and pp.covers(
                        LineString([verts[i], verts[j]])):
                    G.add_edge(i, j, weight=d)
        cache[pi] = (verts, G, pp)
        return cache[pi]

    def vis_path(pi, a, b):
        verts, G, pp = vis_graph(pi)
        euclid = math.hypot(a[0] - b[0], a[1] - b[1])
        if pp.covers(LineString([a, b])):
            return [a, b], euclid
        for tag, pt in (("A", a), ("B", b)):
            G.add_node(tag)
            for i, v in enumerate(verts):
                d = math.hypot(pt[0] - v[0], pt[1] - v[1])
                if 0.5 < d <= 700.0 and pp.covers(LineString([pt, v])):
                    G.add_edge(tag, i, weight=d)
        try:
            vp = nx.shortest_path(G, "A", "B", weight="weight")
            coords = [a] + [verts[i] for i in vp[1:-1]] + [b]
        except Exception:
            coords = None
        finally:
            G.remove_node("A")
            G.remove_node("B")
        if coords is None:
            return None, None
        # standard mirrored arcs at the visibility corners (sharp corners
        # nudged matched geometry off at the 5m/1m tolerances)
        out = [coords[0]]
        for k in range(1, len(coords) - 1):
            p0 = np.asarray(out[-1])
            pc = np.asarray(coords[k])
            p1 = np.asarray(coords[k + 1])
            u_in = _unit(*(pc - p0))
            u_out = _unit(*(p1 - pc))
            seg_in = float(np.hypot(*(pc - p0)))
            seg_out = float(np.hypot(*(p1 - pc)))
            placed = False
            rr = r_std
            while rr >= 0.3 * r_std:
                arc, t = _fillet(tuple(pc), u_in, u_out, rr)
                if arc is None:
                    break
                if t <= 0.5 * seg_in and t <= 0.5 * seg_out \
                        and allow.contains(LineString(arc)):
                    out.extend(list(arc))
                    placed = True
                    break
                rr *= 0.8
            if not placed:
                out.append(tuple(pc))
        out.append(coords[-1])
        L = sum(math.hypot(out[k + 1][0] - out[k][0],
                           out[k + 1][1] - out[k][1])
                for k in range(len(out) - 1))
        return out, L

    from shapely.ops import substring
    rerouted = 0
    for node_path, eis in accepted_paths:
        if any(not g.edges[ei]["alive"] for ei in eis):
            continue
        if any(g.edges[ei]["kind"].startswith("paint") for ei in eis):
            continue                    # painted routes keep their geometry
        pts = [list(g.nodes[node_path[0]])]
        for k, ei in enumerate(eis):
            e = g.edges[ei]
            cs = e["cs"] if e["a"] == node_path[k] else e["cs"][::-1]
            pts.extend([list(p) for p in cs[1:]])
        ln = LineString(pts)
        if ln.length < 100.0:
            continue
        n = max(4, int(ln.length / 6.0))
        P = [list(ln.interpolate(k * ln.length / n).coords[0])
             for k in range(n + 1)]
        pid = []
        for p in P:
            q = Point(tuple(p))
            hit = -1
            for i in tree.query(q):
                if prepped[int(i)].covers(q):
                    hit = int(i)
                    break
            pid.append(hit)
        runs = []
        i = 0
        while i <= n:
            if pid[i] < 0:
                i += 1
                continue
            j = i
            while j + 1 <= n and pid[j + 1] == pid[i]:
                j += 1
            if (j - i) * 6.0 >= 90.0:
                runs.append((i, j, pid[i]))
            i = j + 1
        changed = False
        for (i0, i1, pi) in reversed(runs):
            a, b = tuple(P[i0]), tuple(P[i1])
            run_len = (i1 - i0) * (ln.length / n)
            coords, L = vis_path(pi, a, b)
            if coords is None or L >= 0.95 * run_len:
                continue
            P[i0:i1 + 1] = [list(c) for c in coords]
            changed = True
        if not changed:
            continue
        new_line = LineString(P)
        if not allow.contains(new_line):
            continue
        proj_s = [new_line.project(Point(tuple(g.nodes[ni])))
                  for ni in node_path]
        if any(proj_s[k + 1] < proj_s[k] - 1.0
               for k in range(len(proj_s) - 1)):
            continue
        for k, ni in enumerate(node_path):
            p = new_line.interpolate(proj_s[k])
            if math.hypot(p.x - g.nodes[ni][0],
                          p.y - g.nodes[ni][1]) > 0.01:
                g.move_node(ni, [p.x, p.y])
        for k, ei in enumerate(eis):
            s0, s1 = proj_s[k], proj_s[k + 1]
            if s1 < s0:
                s0, s1 = s1, s0
            if s1 - s0 < 0.2:
                mid_pts = []
            else:
                piece2 = substring(new_line, s0 + 0.1, s1 - 0.1)
                mid_pts = [list(c) for c in piece2.coords] \
                    if piece2.geom_type == "LineString" else []
            e = g.edges[ei]
            pts2 = [list(g.nodes[node_path[k]])] + mid_pts + \
                [list(g.nodes[node_path[k + 1]])]
            if e["a"] != node_path[k]:
                pts2 = pts2[::-1]
            if len(pts2) >= 2:
                cs_new = np.asarray(pts2, dtype=float)
                cs_new[0] = g.nodes[e["a"]]
                cs_new[-1] = g.nodes[e["b"]]
                e["cs"] = cs_new
        rerouted += 1
    if os.environ.get("O4_ET_DEBUG"):
        print(f"[edge_trace] visibility reroutes: {rerouted}", flush=True)


def _taut_tighten(cs, w, bnd, max_iters: int = 30):
    """CONTINUOUS-SPACE taut path: constrained curve-shortening of a dense
    polyline — every interior vertex relaxes toward its neighbours'
    midpoint while keeping a clearance floor (0.9w in the open, the
    path's own clearance through pinches).  Unlike chord decimation this
    can LEAVE the boundary-offset material: the result is straight in the
    open and wraps obstacles at the floor — the sweep geometry the target
    draws through aprons."""
    ln = LineString(cs)
    if ln.length < 80.0:
        return None
    n = max(4, int(ln.length / 5.0))
    P = np.asarray([ln.interpolate(k * ln.length / n).coords[0]
                    for k in range(n + 1)])
    d0 = np.asarray([bnd.distance(Point(tuple(p))) for p in P])
    floor = np.minimum(0.9 * w, np.maximum(1.0, d0 - 0.5))
    moved_any = False
    for _it in range(max_iters):
        moved = 0
        for k in range(1, len(P) - 1):
            tgt = 0.5 * (P[k - 1] + P[k + 1])
            q = P[k] + 0.6 * (tgt - P[k])
            if float(np.hypot(*(q - P[k]))) < 0.03:
                continue
            if bnd.distance(Point(tuple(q))) >= floor[k]:
                P[k] = q
                moved += 1
        if moved == 0:
            break
        moved_any = True
    if not moved_any:
        return None
    # drop collinear interior vertices so downstream fitting sees runs
    keep = [0]
    for k in range(1, len(P) - 1):
        a, b, c2 = P[keep[-1]], P[k], P[k + 1]
        ab, cb = b - a, c2 - b
        cross = abs(ab[0] * cb[1] - ab[1] * cb[0])
        if cross > 0.05 * max(np.hypot(*ab), 1e-9) \
                * max(np.hypot(*cb), 1e-9) or k - keep[-1] > 8:
            keep.append(k)
    keep.append(len(P) - 1)
    return P[keep]


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


def _straighten_paths(g: _Graph, pav_eff, w: float, r_std: float = 30.0,
                      axes=None):
    """BIG-PICTURE STRAIGHT (user rule i) applied to THROUGH-PATHS: target
    straights run through junction after junction, so chords must span
    node-to-node fragments.  Interior junction nodes are moved onto the
    straightened line (side branches follow via move_node)."""
    _straighten_path_list(g, _assemble_through_paths(g), pav_eff, w, r_std,
                          axes)


def _straighten_path_list(g: _Graph, paths, pav_eff, w: float,
                          r_std: float = 30.0, axes=None,
                          taut: bool = False):
    from shapely.ops import substring
    bnd = pav_eff.boundary
    allow = shapely.buffer(pav_eff, 0.5)
    for path in paths:
        cs, _rr, node_seq = _collect_path(g, path)
        if len(cs) < 3:
            continue
        if taut:
            cs2 = _taut_tighten(cs, w, bnd)
            if cs2 is not None and allow.contains(LineString(cs2)):
                cs = cs2
        d_orig = np.asarray([bnd.distance(Point(tuple(p))) for p in cs])
        new_line = None
        if not os.environ.get("O4_ET_NO_REFIT"):
            new_line = _refit_chain(cs, None, w, r_std, allow, bnd)
        if new_line is not None:
            # rule (i) arbiter: if the pure chord route is MUCH shorter
            # than the curve-following refit, the "curves" were obstacle
            # detour, not design — straight wins (big apron sweeps)
            keep_c = _max_chords(cs, d_orig, w, allow, bnd)
            chord_line = LineString(cs[keep_c])
            if chord_line.length < 0.92 * new_line.length:
                new_line = chord_line
        if new_line is None:
            keep = _max_chords(cs, d_orig, w, allow, bnd)
            pts = cs[keep].astype(float).copy()
            # re-center each long chord by LSQ over ALL spanned medial
            # vertices — a chord through two endpoint vertices inherits
            # their sampling noise, the fitted axis does not (alignment);
            # a fit within 3° of the runway grid snaps EXACTLY to it
            # (taxiways are surveyed parallel/perpendicular to runways —
            # residual angular error over a long corridor is what keeps
            # the matched median above the gate)
            fits = []
            for k in range(len(keep) - 1):
                i, j = keep[k], keep[k + 1]
                span = LineString(cs[i:j + 1]).length if j > i else 0.0
                f = _fit_line(cs[i:j + 1]) \
                    if span >= 60.0 and j - i >= 4 else None
                if f is not None and axes:
                    from .spine_synthesis import _snap_direction
                    snapped = _snap_direction(tuple(f[1]), axes,
                                              tol_deg=3.0)
                    if snapped is not None:
                        f = (f[0], np.asarray(snapped))
                fits.append(f)

            def _proj(f, q):
                p0, u = f
                return p0 + u * float((q - p0) @ u)
            for k in range(1, len(pts) - 1):
                fa, fb = fits[k - 1], fits[k]
                if fa is not None and fb is not None:
                    ang = _angle_deg(tuple(fa[1]), tuple(fb[1]))
                    ang = min(ang, 180.0 - ang)
                    if ang > 5.0:
                        den = fa[1][0] * fb[1][1] - fa[1][1] * fb[1][0]
                        dp = fb[0] - fa[0]
                        s1 = (dp[0] * fb[1][1] - dp[1] * fb[1][0]) / den
                        cand = fa[0] + fa[1] * s1
                    else:
                        cand = 0.5 * (_proj(fa, pts[k]) + _proj(fb, pts[k]))
                elif fa is not None:
                    cand = _proj(fa, pts[k])
                elif fb is not None:
                    cand = _proj(fb, pts[k])
                else:
                    continue
                if float(np.hypot(*(cand - pts[k]))) <= 3.0:
                    pts[k] = cand
            cand_line = LineString(pts)
            new_line = cand_line if allow.contains(cand_line) \
                else LineString(cs[keep])
        # project the NODE positions themselves (cs may have been replaced
        # by the taut geometry, so index bookkeeping into it is invalid)
        proj_s = [new_line.project(Point(tuple(g.nodes[ni])))
                  for ni in node_seq]
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
    terminal_setback: float = 100.0, recognized=None, ramps=None,
) -> list[SpineWay]:
    """Edge-trace spine (module docstring).  ``routes`` supplies ICAO size
    letters ONLY — geometry never comes from it.  ``recognized`` supplies
    geometry-verified painted centerlines; where they exist they are the
    PRIMARY design geometry (user ruling 2026-07-02) and the pavement
    inference only fills unpainted areas."""
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

    # PAINT-PRIMARY (v9): recognized painted centerlines are the design
    # geometry wherever present; every pavement-inferred candidate that
    # merely re-derives a painted line yields to the paint.
    paint_edges, paint_union = _paint_candidates(
        recognized, pav_eff, w, corridors)
    dedup_thr = _PAINT_DEDUP_FRAC * w

    g = _Graph()
    trunk_lines = []
    for cs, rr, w_med in kept_runs:
        if bay_union is not None and bay_union.contains(
                LineString(cs).interpolate(0.5, normalized=True)):
            continue                            # bay-interior medial
        if _duplicates_paint(cs, paint_union, dedup_thr):
            continue                            # paint carries this corridor
        g.add_edge(cs, "lane", "", w_med)
        trunk_lines.append(LineString(cs))
    trunk_union = unary_union(trunk_lines) if trunk_lines else None
    for cs, rr in center_runs:
        if bay_union is not None and bay_union.contains(
                LineString(cs).interpolate(0.5, normalized=True)):
            continue
        if _duplicates_paint(cs, paint_union, dedup_thr):
            continue
        g.add_edge(cs, "center", "", w + float(np.median(rr)))

    # regime C — edge traces: building frontage (kept) + hole rings
    # (routing candidates).
    for kind, t in _edge_traces(core, pav_eff, w, runway_union,
                                building_union, trunk_union):
        if bay_union is not None and bay_union.contains(
                t.interpolate(0.5, normalized=True)):
            continue
        if _duplicates_paint(np.asarray(t.coords), paint_union, dedup_thr):
            continue
        g.add_edge(np.asarray(t.coords), kind, "", w)

    for cs, kind in paint_edges:
        if bay_union is not None and bay_union.contains(
                LineString(cs).interpolate(0.5, normalized=True)):
            continue                            # parking combs stay empty
        g.add_edge(cs, kind, "", w)
    if os.environ.get("O4_ET_DEBUG") and paint_edges:
        n_tr = sum(1 for _c, k in paint_edges if k == "paint_trunk")
        print(f"[edge_trace] paint: {len(paint_edges)} pieces "
              f"({n_tr} trunk)", flush=True)

    _add_mouth_projections(g, pav_eff)
    _planarize_crossings(g)
    demand = _strip_stand_combs(g)
    _connect_free_ends(g, pav_eff)
    if not os.environ.get("O4_ET_NO_SELECT"):
        # paint is design geometry — kept by default, pruned by the
        # deletion criteria (parking/leaf passes below), NOT by the
        # detour spanner: the spanner kills legitimate >=separation
        # parallels (measured -14 coverage) while the true overgen is
        # parking material the criteria remove directly
        keep = ("lane", "paint_trunk") \
            if os.environ.get("O4_ET_PAINT_SPAN") else \
            ("lane", "paint_trunk", "paint")
        accepted = _select_routes(g, runway_union, extra_anchors=demand,
                                  keep_kinds=keep)
        _straighten_routes(g, accepted, pav_eff, w,
                           R90_BY_SIZE.get(size, 30.0))
    # R5 granularity: open-space BODIES = core opened at h (necks between
    # sub-separation connections cut), so a big apron is its own area even
    # when the eroded space is globally connected.
    area_bodies = []
    if not core.is_empty and h > 0.5:
        area_bodies = _polygons(shapely.buffer(
            shapely.buffer(core, -h), h + 0.05))
    _area_access_hugs(g, area_bodies, w, sep, pav_eff)
    _trim_proj_tails(g, runway_union)
    # NOTE: a global parallel-distance invariant is REFUTED by the target's
    # own geometry (123 same-space parallel samples under 10 m: junction
    # fans, converging lanes) — the user width rule is per stand AREA, not
    # a spacing law.  _enforce_min_separation kept for a future scoped use.
    if os.environ.get("O4_ET_SEP_INVARIANT"):
        _enforce_min_separation(g, w, sep, pav_eff, demand=demand)
    _prune_parking_paint(g, ramps)
    _prune_paint_leaves(g, runway_union, pav_eff, demand=demand)
    # NOTE: straightening each accepted route as one unit (endpoints at
    # anchors) was tried and is NET-NEGATIVE (coverage 60.8->59.1,
    # alignment 1.21->1.43): chords cut curves the target keeps.  The
    # through-path straightener below is the keeper.
    _prune_no_runway_components(g, runway_union, pav_eff)
    _fix_dangles(g, pav_eff)
    if not os.environ.get("O4_ET_KINDS"):        # keep kinds for debug only
        for e in g.edges:
            # paint keeps its kind: it must stay outside through-path
            # assembly and every straightener (it IS the design geometry)
            if not e["kind"].startswith("paint"):
                e["kind"] = "lane"
    g.consolidate()
    _straighten_paths(g, pav_eff, w, R90_BY_SIZE.get(size, 30.0), axes)
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
    _close_floating_tips(g, pav_eff, building_union)
    g.consolidate()
    return g.ways()
