"""V11 MEDIAL-TREE RESHAPE spine (user approval 2026-07-02).

ONE connected object instead of two generators + glue: the medial tree
of each pavement face, carrying its boundary CONTACT samples (a Voronoi
ridge between samples i and j IS the locus equidistant to them — the
contacts come free).  Then:

* THIN medial edges (clearance <= 2w, i.e. strip narrower than two full
  corridors) stay as they are — the centered single line of rule 1.
* FAT medial edges (clearance > 2w) are REPLACED by the pavement-
  boundary arcs their contacts cover, offset inward by w — the user's
  left-turn wall-following walk.  Offset-from-the-edge geometry cannot
  drift, turns at w instead of overshooting the middle, and a medial
  corner spoke collapses to the ring's corner arc automatically (its
  contacts converge at the corner).
* At each thin->fat transition the centered line FORKS onto the two
  offset curves via two short connectors along the contact normals
  (the fork vertex sits 2w from each contact, the offset points w) —
  deterministic, no welding search.
* Runs whose contacts lie on the runway cut are dropped (never trace
  along the runway edge); the ring ends AT the runway edge instead.

Connectivity is a THEOREM here (the medial of a connected polygon is
connected), so the v10 glue stack — tip bridges, component welds, mouth
projections, floating-tip closers, dead-end prunes — does not exist.
Kept from v10: rule-6 closing, service-width filter, runway hooks,
junction arcs, axis straightening, the QA gates.
"""

from __future__ import annotations

import math
import os

import networkx as nx
import numpy as np
import shapely
from scipy.spatial import Voronoi, cKDTree
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from .pav_skeleton import _fair_chain, _polygons, _sample_rings, \
    _adjacent_on_ring, build_pavement_skeleton
from .spine_synthesis import (
    SpineWay, _Graph, _add_junction_arcs, _add_runway_turns,
    _attribute_sizes, _fix_dangles, _runway_axes, _SVC_HALFWIDTH_M,
)
from .edge_trace import _dominant_halfwidth, _split_line_by_mask
from .outline_trace import TAXIWAY_WIDTH_BY_SIZE, _widest_documented_size


def _medial_graph_contacts(poly: Polygon, step: float, min_r: float):
    """Voronoi medial graph of one polygon face; every edge carries its
    generating boundary-sample pair (the CONTACTS).  Returns
    (graph, samples Nx2, ring_ids, seqs)."""
    pts, ring_ids, seqs = _sample_rings(poly, step)
    if len(pts) < 8:
        return nx.Graph(), pts, ring_ids, seqs
    vor = Voronoi(pts)
    verts = vor.vertices
    inside = shapely.contains_xy(poly, verts[:, 0], verts[:, 1])
    radii = cKDTree(pts).query(verts)[0]
    g = nx.Graph()
    for (p1, p2), (v1, v2) in zip(vor.ridge_points, vor.ridge_vertices):
        if v1 < 0 or v2 < 0:
            continue
        if not (inside[v1] and inside[v2]):
            continue
        if radii[v1] < min_r or radii[v2] < min_r:
            continue
        if _adjacent_on_ring(p1, p2, ring_ids, seqs):
            continue
        d = float(np.hypot(*(verts[v1] - verts[v2])))
        g.add_node(v1, pos=tuple(verts[v1]), r=float(radii[v1]))
        g.add_node(v2, pos=tuple(verts[v2]), r=float(radii[v2]))
        g.add_edge(v1, v2, w=d, contacts=(int(p1), int(p2)))
    return g, pts, ring_ids, seqs


def _prune_whiskers_g(g: nx.Graph, factor: float, r_keep: float) -> None:
    """Whisker pruning restricted to THIN leaves: a fat-region spoke's
    contacts are exactly what covers the ring's corner arc — deleting it
    punches a gap into every corner of the offset ring (measured: 124
    components).  Only boundary-noise whiskers (thin all the way) go."""
    while True:
        removed = False
        for leaf in [n for n in g.nodes if g.degree(n) == 1]:
            if leaf not in g:
                continue
            chain = [leaf]
            cur, prev, length = leaf, None, 0.0
            while cur in g and g.degree(cur) <= 2:
                nbrs = [n for n in g.neighbors(cur) if n != prev]
                if not nbrs:
                    break
                nxt = nbrs[0]
                length += g.edges[cur, nxt]["w"]
                prev, cur = cur, nxt
                chain.append(cur)
            if cur in g and g.degree(cur) >= 3 \
                    and length < factor * g.nodes[cur]["r"] \
                    and all(g.nodes[n]["r"] <= r_keep
                            for n in chain[:-1]):
                g.remove_nodes_from(chain[:-1])
                removed = True
        if not removed:
            return


def _inward_offset(samples, ring_pts, poly, w):
    """Offset each boundary sample inward by w (normal chosen by the
    containment test on the run's midpoint)."""
    out = []
    n = len(samples)
    for k, si in enumerate(samples):
        i0 = samples[max(0, k - 1)]
        i1 = samples[min(n - 1, k + 1)]
        t = ring_pts[i1] - ring_pts[i0]
        nt = float(np.hypot(*t))
        if nt < 1e-9:
            out.append(None)
            continue
        t = t / nt
        out.append((ring_pts[si], np.asarray([-t[1], t[0]])))
    # decide the sign once per run (midpoint containment)
    mid = out[len(out) // 2]
    if mid is None:
        return None
    for sgn in (1.0, -1.0):
        q = mid[0] + sgn * w * mid[1]
        if poly.contains(Point(tuple(q))):
            return np.asarray([p + sgn * w * nrm
                               for (p, nrm) in out if p is not None])
    return None


def _connect_ring_ends(g: _Graph, pav_eff, reach: float):
    """A trace ring that was cut (runway rule, crossing mouth) continues
    into the line that cut it: connect deg-1 TRACE tips to the nearest
    lane within ``reach`` (straight, pavement-contained)."""
    from collections import Counter
    from shapely.strtree import STRtree
    allow = shapely.buffer(pav_eff, 0.5)
    deg = Counter()
    for e in g.edges:
        if e["alive"]:
            deg[e["a"]] += 1
            deg[e["b"]] += 1
    lanes = [(ei, e) for ei, e in enumerate(g.edges)
             if e["alive"] and e["kind"] == "lane"]
    lines = [LineString(e["cs"]) for _ei, e in lanes]
    if not lines:
        return
    tree = STRtree(lines)
    for e in list(g.edges):
        if not e["alive"] or e["kind"] != "trace":
            continue
        for ni in (e["a"], e["b"]):
            if deg[ni] != 1:
                continue
            p = Point(tuple(g.nodes[ni]))
            best = None
            for k in tree.query(p.buffer(reach)):
                k = int(k)
                if not lanes[k][1]["alive"]:
                    continue
                d = lines[k].distance(p)
                if 0.5 < d <= reach and (best is None or d < best[0]):
                    best = (d, k)
            if best is None:
                continue
            _d, k = best
            ei2 = lanes[k][0]
            s = float(lines[k].project(p))
            q = lines[k].interpolate(s)
            seg = LineString([tuple(p.coords[0]), (q.x, q.y)])
            if not allow.contains(seg):
                continue
            mid = g.split_edge(ei2, s)
            if mid != ni:
                g.add_edge(np.asarray([g.nodes[ni], g.nodes[mid]]),
                           "lane", "", 0.0)
                deg[ni] += 1


def _stitch_tolerance(g: _Graph, reach: float = 4.0):
    """Numerical stitching, NOT topology glue: the reshape's pieces are
    geometrically coincident by construction, but 2 m boundary sampling
    and fairing leave sub-4 m seams (measured: components 1.2 m apart).
    Snap every free end onto the nearest edge within ``reach``."""
    from collections import Counter
    from shapely.strtree import STRtree
    for _ in range(3):
        deg = Counter()
        for e in g.edges:
            if e["alive"]:
                deg[e["a"]] += 1
                deg[e["b"]] += 1
        alive = [(ei, e) for ei, e in enumerate(g.edges) if e["alive"]]
        lines = [LineString(e["cs"]) for _ei, e in alive]
        tree = STRtree(lines)
        changed = False
        for ei, e in list(alive):
            if not e["alive"]:
                continue
            for ni in (e["a"], e["b"]):
                if deg[ni] != 1:
                    continue
                p = Point(tuple(g.nodes[ni]))
                best = None
                for k in tree.query(p.buffer(reach)):
                    k = int(k)
                    oj, eo = alive[k]
                    if oj == ei or not eo["alive"]:
                        continue
                    d = lines[k].distance(p)
                    if 0.001 < d <= reach and (best is None or d < best[0]):
                        best = (d, k)
                if best is None:
                    continue
                _d, k = best
                oj, eo = alive[k]
                s = float(lines[k].project(p))
                mid = g.split_edge(oj, s)
                if mid != ni:
                    g.add_edge(np.asarray([g.nodes[ni], g.nodes[mid]]),
                               "lane", "", 0.0)
                    deg[ni] += 1
                    deg[mid] += 1
                    changed = True
        if not changed:
            break


def synthesize_spine_v11(
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
    pav_eff = pav_nav
    if runway_union is not None and not runway_union.is_empty:
        try:
            pav_eff = pav_nav.difference(runway_union)
        except Exception:
            pass

    # measured dominant half-width (same estimator as v10)
    chains = build_pavement_skeleton(pav_nav, runway_union=runway_union)
    w = _dominant_halfwidth(chains)
    size_doc = _widest_documented_size(routes)
    svc_cut = max(_SVC_HALFWIDTH_M, 0.35 * w)
    a_skip = TAXIWAY_WIDTH_BY_SIZE["A"]
    dbg = bool(os.environ.get("O4_ET_DEBUG"))

    # rule 6 — openings narrower than a size-A corridor do not exist
    r_close = 0.5 * a_skip
    pav_closed = shapely.buffer(shapely.buffer(pav_eff, r_close), -r_close)
    allow = shapely.buffer(pav_eff, 0.5)
    # construction containment tests run against the model's OWN polygon
    # (closed faces) — checking forks against pav_eff rejects connectors
    # crossing rule-6-filled notches and severs the tree
    allow_c = shapely.buffer(pav_closed, 0.5)
    rwy_b = runway_union.boundary if runway_union is not None \
        and not runway_union.is_empty else None

    g = _Graph()
    n_thin = n_fat_runs = n_forks = 0
    for poly in _polygons(pav_closed):
        if poly.area < 400.0:
            continue
        med, pts, ring_ids, seqs = _medial_graph_contacts(
            poly, step=2.0, min_r=1.0)
        if med.number_of_nodes() == 0:
            continue
        _prune_whiskers_g(med, factor=1.3, r_keep=2.0 * w)
        # runway-cut samples (contacts on the cut = runway adjacency)
        on_rwy = np.zeros(len(pts), dtype=bool)
        if rwy_b is not None:
            d_r = np.asarray([rwy_b.distance(Point(tuple(p)))
                              for p in pts])
            on_rwy = d_r < 1.5

        thin = {n for n in med.nodes if med.nodes[n]["r"] <= 2.0 * w}
        # JUNCTION BULGES are small fat pockets between thin stretches —
        # the through line crosses an opening STRAIGHT (user rule); only
        # LARGE fat regions (aprons, max clearance > 3.2w) reshape into
        # offset rings.  Without this every junction flare cut the
        # corridor (ring collapsed to nothing, 124 components).
        fat_all = set(med.nodes) - thin
        for comp in nx.connected_components(med.subgraph(fat_all)):
            if max(med.nodes[n]["r"] for n in comp) <= 3.2 * w:
                thin |= comp

        # ── thin chains (centered lines, rule 1) ─────────────────────────
        sub = med.subgraph(thin)
        seen_e = set()
        for comp in nx.connected_components(sub):
            cg = sub.subgraph(comp)
            ends = [n for n in cg.nodes if cg.degree(n) != 2]
            starts = ends if ends else [next(iter(cg.nodes))]
            for s in starts:
                for nb in cg.neighbors(s):
                    if frozenset((s, nb)) in seen_e:
                        continue
                    chain = [s, nb]
                    seen_e.add(frozenset((s, nb)))
                    prev, cur = s, nb
                    while cg.degree(cur) == 2 and cur not in ends:
                        nxt = [x for x in cg.neighbors(cur) if x != prev]
                        if not nxt:
                            break
                        nxt = nxt[0]
                        if frozenset((cur, nxt)) in seen_e:
                            break
                        seen_e.add(frozenset((cur, nxt)))
                        prev, cur = cur, nxt
                        chain.append(cur)
                    cs = np.asarray([med.nodes[n]["pos"] for n in chain])
                    rr = np.asarray([med.nodes[n]["r"] for n in chain])
                    if LineString(cs).length < 6.0:
                        continue
                    # service-width strips: dropping a whole chain can
                    # SEVER the tree — only drop leaf-ended svc chains
                    # (dead-end service roads), never connecting ones
                    is_svc = float(np.median(rr)) < svc_cut
                    leaf_ended = cg.degree(chain[0]) == 1 \
                        or cg.degree(chain[-1]) == 1
                    if is_svc and leaf_ended:
                        continue
                    cs, rr2 = _fair_chain(cs, rr, allow, step=6.0,
                                          sigma=0.35)
                    g.add_edge(np.asarray(cs), "lane", "",
                               float(np.median(rr)))
                    n_thin += 1

        # ── fat regions -> contact-covered offset arcs ───────────────────
        fat = set(med.nodes) - thin
        fsub = med.subgraph(fat)
        offset_pt = {}                    # sample idx -> emitted offset xy
        for comp in nx.connected_components(fsub):
            contacts = set()
            for a, b in fsub.subgraph(comp).edges:
                p1, p2 = fsub.edges[a, b]["contacts"]
                contacts.add(p1)
                contacts.add(p2)
            # transitions: thin nodes adjacent to this fat component
            fork_pts = []
            for n in comp:
                for nb in med.neighbors(n):
                    if nb in thin:
                        e = med.edges[n, nb]
                        fork_pts.append((med.nodes[nb]["pos"],
                                         e["contacts"]))
            # group contacts into contiguous boundary runs per ring
            by_ring = {}
            for si in contacts:
                by_ring.setdefault(ring_ids[si], []).append(si)
            for ri, sis in by_ring.items():
                k_of = {si: seqs[si][0] for si in sis}
                n_ring = seqs[sis[0]][1]
                ks = sorted(k_of[si] for si in sis)
                # contiguous runs on the cyclic ring (gap > 3 samples splits)
                runs = [[ks[0]]]
                for k in ks[1:]:
                    if k - runs[-1][-1] <= 6:
                        runs[-1].append(k)
                    else:
                        runs.append([k])
                if len(runs) > 1 and (ks[0] + n_ring - ks[-1]) <= 6:
                    runs[0] = runs[-1] + runs[0]
                    runs.pop()
                idx_by_k = {}
                for si in sis:
                    idx_by_k[k_of[si]] = si
                for run in runs:
                    sids = [idx_by_k[k] for k in run]
                    if len(sids) < 4:
                        continue
                    keep = np.asarray([not on_rwy[si] for si in sids])
                    coords = _inward_offset(sids, pts, poly, w)
                    if coords is None or len(coords) != len(sids):
                        continue
                    # remember every emitted offset vertex by its sample —
                    # fork connectors reuse the EXACT coordinate so the
                    # weld is identity, not a search
                    for si, q, kp in zip(sids, coords, keep):
                        if kp:
                            offset_pt[si] = q
                    for piece in _split_line_by_mask(coords, keep):
                        if LineString(piece).length >= 10.0:
                            g.add_edge(np.asarray(piece), "trace", "", w)
                            n_fat_runs += 1
            # forks: connect the transition vertex to both offset points
            for pos, (c1, c2) in fork_pts:
                P = np.asarray(pos)
                for si in (c1, c2):
                    q = offset_pt.get(si)
                    if q is None:
                        # sample not emitted (runway-cut or short run):
                        # nearest emitted offset point of this region
                        near = [offset_pt[sj] for sj in (c1, c2)
                                if sj in offset_pt]
                        if not near:
                            continue
                        q = near[0]
                    seg = LineString([tuple(P), tuple(q)])
                    if seg.length < 0.5 or allow_c.contains(seg):
                        g.add_edge(np.asarray([P, q]), "lane", "", w)
                        n_forks += 1
    if dbg:
        print(f"[reshape] thin={n_thin} fat_runs={n_fat_runs} "
              f"forks={n_forks} w={w:.1f}", flush=True)

    if os.environ.get("O4_MR_RAW"):
        # review escape hatch: the pure construction (thin chains +
        # rings + forks), no stitching/straightening/arcs — judge the
        # MODEL's geometry, not the finishing passes
        g.consolidate()
        return g.ways()

    from .edge_trace import _planarize_crossings, _straighten_paths
    from .outline_trace import (
        _merge_coincident, _prune_unreachable, _trim_corner_deaths,
        _trim_interior_stubs)
    _planarize_crossings(g)
    _stitch_tolerance(g)
    # ring ends exist where the runway rule or a crossing mouth CUT the
    # offset ring — the face boundary continues across that mouth, so
    # the ring end connects into the crossing corridor's line
    _connect_ring_ends(g, pav_eff, reach=2.0 * w)
    # rule 2: spine tips extend to the pavement/runway edge BEFORE the
    # reachability test (contact stubs stop ~half-w short of the cut)
    _fix_dangles(g, pav_eff)
    # detached RINGS have no free tips — component-level stitching at
    # the same numerical tolerance (not a topology search)
    from .outline_trace import _weld_components
    _weld_components(g, pav_eff, max_gap=12.0)
    _merge_coincident(g)
    if not os.environ.get("O4_MR_NO_PRUNE"):
        # review escape hatch: keep unreachable fabric so the geometry
        # can be judged in JOSM while connectivity work is in flight
        _prune_unreachable(g, runway_union)
    _fix_dangles(g, pav_eff)
    _trim_interior_stubs(g, pav_eff, runway_union)

    if not os.environ.get("O4_ET_KINDS"):
        for e in g.edges:
            if e["kind"] != "trace":
                e["kind"] = "lane"
    g.consolidate()
    axes = _runway_axes(runway_union)
    from .spine_synthesis import R90_BY_SIZE
    _straighten_paths(g, pav_eff, w, R90_BY_SIZE.get(size_doc or "E", 30.0),
                      axes)
    g.consolidate()

    def pav_ok(line: LineString) -> bool:
        return allow.contains(line)

    _attribute_sizes(g, routes)
    bnd_arc = pav_eff.boundary

    def r_start_for(P, r_std):
        return min(1.6 * r_std, max(r_std, bnd_arc.distance(Point(tuple(P)))))

    _add_junction_arcs(g, pav_ok, runway_union, r_start_for=r_start_for)
    _add_runway_turns(g, runway_union, pav_eff)
    _fix_dangles(g, pav_eff)
    _trim_corner_deaths(g, pav_eff, runway_union)
    g.consolidate()
    return g.ways()
