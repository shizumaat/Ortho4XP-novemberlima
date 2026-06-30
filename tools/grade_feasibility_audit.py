"""Grade-feasibility oracle — classify every within-shape grade violation as
either FUNDAMENTALLY INFEASIBLE (no compliant field exists given the hard
anchors) or FEASIBLE-BUT-UNENFORCED (a compliant field exists; the solver
just didn't reach it).

This is the W1/W2 measurement foundation for the zero-violation plan
(docs/grade_enforcement_plan.md).  It treats the within-shape grade law as a
DIFFERENCE-CONSTRAINT system: every validated vertex pair (i,j) is
``|z_i - z_j| <= cap * d_ij``; runway/seam nodes are HARD (fixed at their
solved elevation); each building/terminal pad is a FLAT equality group.  The
tightest feasible interval for a free node v is

    hi[v] = min over hard anchors a of (z_a + shortest cap-weighted path a->v)
    lo[v] = max over hard anchors a of (z_a - shortest cap-weighted path a->v)

computed by two multi-source Dijkstras.  A node with ``lo > hi`` is
BAND-PINNED — the anchors are closer in the grade graph than their elevation
gap allows, so NO compliant field exists there (W3/W5: yield an anchor, flex
the corridor, or split with a transition).  A check_grade violation whose
endpoints are NOT band-pinned is the solver leaving feasible slack on the
table (W2 exact projection / W4 route-profile freedom).

The constrained pair set comes from ``check_grade.iter_shape_grade_constraints``
— the SAME generator the validator uses — so the oracle and the validator can
never drift (W1 graph lockstep).

Usage:
    venv/bin/python tools/grade_feasibility_audit.py SPJC [CYXY HECA ...]
"""
from __future__ import annotations

import heapq
import math
import os
import sys
import tempfile
from collections import defaultdict

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
for _p in (os.path.join(_ROOT, "src"), _ROOT, os.path.join(_ROOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import check_grade as CG
from shapely.geometry import LineString


class _UF:
    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _dijkstra(n_index, adj, sources):
    """Multi-source Dijkstra. ``sources`` = {node: init_dist}. Returns the
    min over sources of (init_dist[s] + path(s->node)). Non-negative weights."""
    INF = float("inf")
    dist = defaultdict(lambda: INF)
    pq = []
    for s, d0 in sources.items():
        if d0 < dist[s]:
            dist[s] = d0
            heapq.heappush(pq, (d0, s))
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u]:
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def _route_band_intervals(layout, ways, nodes, seam_nids):
    """Per-node route-band interval [lo,hi] = the runway-reach long-range law,
    read off THE unified grade graph via
    ``building_feasibility.reach_band_unified`` — the SAME band the spine solves
    against and ``grade_graph_validate.route_band_violations`` confirms (it
    replaces the retired ``route_field`` per-vertex band on a separate centerline
    graph).  The band is queried by position in the layout's OWN anchor-relative
    meter frame (``layout.ll_to_m``); the audit's mean-centred ``ll_to_m`` is a
    different frame the band does not share."""
    try:
        from auto_patch.elevation_per_surface.solver_primitives import (
            _build_node_list)
        from auto_patch.grade_graph import build_unified_graph
        from auto_patch.elevation_per_surface.building_feasibility import (
            reach_band_unified)
    except Exception:
        return {}, {}
    _bnodes, b2i = _build_node_list(layout)
    G = build_unified_graph(layout, b2i)
    band = reach_band_unified(layout, G)
    lo_d, hi_d = {}, {}
    SKIP = {"runway", "runway_crossing"}
    for w in ways:
        role = w.tags.get("role")
        if role in SKIP or CG._is_groundside(w):
            continue
        if CG._role_grade_limit(w, 0.015) is None:
            continue
        ring = (w.nids[:-1] if len(w.nids) > 1 and w.nids[0] == w.nids[-1]
                else w.nids)
        for k, nid in enumerate(ring):
            if nid not in nodes or nid in seam_nids or w.elevs[k] is None:
                continue
            b = band(*layout.ll_to_m(*nodes[nid]))
            if b is None:
                continue
            flo, cei = b
            if math.isfinite(cei):
                hi_d[nid] = min(hi_d.get(nid, float("inf")), cei)
            if math.isfinite(flo):
                lo_d[nid] = max(lo_d.get(nid, float("-inf")), flo)
    return lo_d, hi_d


def _pocs_solve(adj, hard, lo, hi, seed, max_sweeps=5000, tol=1e-4):
    """Cyclic POCS: project each difference constraint then clamp to the box,
    until the worst edge excess < tol.  Converges to a feasible point iff the
    polytope is non-empty.  Returns (sweeps, resid, n_edges_over_cap+0.15m)."""
    INF = float("inf")
    z = {}
    for r in (set(adj.keys()) | set(hard.keys())
              | set(lo.keys()) | set(hi.keys()) | set(seed.keys())):
        z[r] = hard[r] if r in hard else seed.get(r, 0.0)
    edges, seen = [], set()
    for u in adj:
        for (v, w) in adj[u]:
            key = (u, v) if u < v else (v, u)
            if key in seen:
                continue
            seen.add(key)
            edges.append((u, v, w))

    def _clamp():
        for r in z:
            if r in hard:
                continue
            l = lo.get(r, -INF)
            h = hi.get(r, INF)
            if l <= h:
                z[r] = min(max(z[r], l), h)
    _clamp()
    sweep = 0
    mx = 0.0
    for sweep in range(max_sweeps):
        mx = 0.0
        for (i, j, c) in edges:
            d = z[i] - z[j]
            ex = abs(d) - c
            if ex <= 0.0:
                continue
            hi_i = i in hard
            hj = j in hard
            if hi_i and hj:
                continue
            if ex > mx:
                mx = ex
            s = 1.0 if d > 0 else -1.0
            if hi_i:
                z[j] += s * ex
            elif hj:
                z[i] -= s * ex
            else:
                z[i] -= 0.5 * s * ex
                z[j] += 0.5 * s * ex
        _clamp()
        if mx < tol:
            break
    nviol = sum(1 for (i, j, c) in edges
                if abs(z[i] - z[j]) > c + 0.15)
    return sweep + 1, mx, nviol


def audit_layout(layout, icao):
    out = tempfile.NamedTemporaryFile(suffix=".osm", delete=False).name
    layout.to_osm(out)
    nodes, ways = CG._parse_osm(__import__("pathlib").Path(out))
    ll_to_m = CG._ll_to_m_factory(nodes)
    seam_nids = CG._seam_nids(nodes)

    # Per-axis taxi axes (match the build's constraint set exactly).
    taxi_axes = None
    routes_ll = None
    try:
        from auto_patch.verification import (taxi_axes_ll as _tall,
                                             taxi_routes_ll as _trll)
        tll = _tall(layout)
        if tll:
            taxi_axes = [([ll_to_m(la, lo) for la, lo in pts], cL, cT)
                         for pts, cL, cT in tll]
        routes_ll = _trll(layout) or None
    except Exception:
        taxi_axes = None
        routes_ll = None

    constraints = CG.iter_shape_grade_constraints(
        ways, nodes, ll_to_m, 0.015, seam_nids, taxi_axes, routes_ll)

    # Per-vertex emitted elevation lookup (nid -> elev) from the ways.
    elev: dict = {}
    role_of_nid = defaultdict(set)
    for w in ways:
        role = w.tags.get("role")
        ring = (w.nids[:-1] if len(w.nids) > 1 and w.nids[0] == w.nids[-1]
                else w.nids)
        for k, nid in enumerate(ring):
            if nid in nodes and w.elevs[k] is not None:
                elev[nid] = w.elevs[k]
                role_of_nid[nid].add(role)

    # Buildings/terminals are NOT rigid-flat — the law caps them at
    # TERMINAL_MAX_GRADE (1.5%, == apron), so they may slope a small amount
    # (user 2026-06-18: "worst case a couple buildings slope a small amount").
    # Forcing them to ONE level (a UF equality group) is STRICTER than the law
    # and manufactures false inversions wherever the network needs a building
    # to tilt slightly — that was the spurious "canyon" infeasibility.  Grade
    # them at their cap via their own within-shape edges, like any other shape.
    uf = _UF()
    import os as _os9
    if _os9.environ.get("O4_AUDIT_FLAT_BUILDINGS") == "1":   # diagnostic A/B
        for w in ways:
            if w.tags.get("role") not in ("building", "terminal", "stand"):
                continue
            ring = [nid for nid in w.nids if nid in nodes]
            for nid in ring[1:]:
                uf.union(ring[0], nid)

    def rep(nid):
        return uf.find(nid)

    # Build the difference-constraint graph over representatives.
    #   adj      = ALL in-pavement visible chords — used for the POCS grade
    #              feasibility test (the surface law: no distance window).
    #   adj_win  = SHORT (<=REACH_WINDOW) chords only — used for the 2-sided
    #              REACH bounds.  A long straight chord across an apron interior
    #              UNDER-measures the real curving taxi route, so using it for
    #              the runway-reach bound manufactures false "canyons" (the bug
    #              the old 80 m window was guarding against — but it must apply
    #              to REACH, not to grade enforcement).
    REACH_WINDOW = 80.0
    adj = defaultdict(list)
    adj_win = defaultdict(list)
    n_edges = 0
    for c in constraints:
        ra, rb = rep(c.nid_a), rep(c.nid_b)
        if ra == rb:
            continue                      # intra-flat-group: auto-satisfied
        # Edge budget = THE LAW's per-pair allowance (cap.at(Δs∥,Δs⊥)+noise from
        # iter_shape_grade_constraints), NOT a recomputed cap·dist — so the oracle
        # uses the same ANISOTROPIC budget the solver builds to and the validator
        # checks.  A curving route's arc-credited pair is no longer scored against
        # its shorter chord, so the audit can't report a false infeasibility there.
        w = c.allowance
        adj[ra].append((rb, w))
        adj[rb].append((ra, w))
        if c.dist <= REACH_WINDOW:
            adj_win[ra].append((rb, w))
            adj_win[rb].append((ra, w))
        n_edges += 1

    # HARD anchors: runway / runway_crossing / seam nodes, at emitted elev.
    HARD_ROLES = {"runway", "runway_crossing"}
    hard = {}
    for nid, roles in role_of_nid.items():
        if (roles & HARD_ROLES) or nid in seam_nids:
            if nid in elev:
                hard[rep(nid)] = elev[nid]
    if not hard:
        print(f"  {icao}: no hard anchors found — cannot bound. SKIP")
        return None

    # ROUTE-BAND law: the long-range runway-reach bound per node (what the
    # within-shape law alone cannot carry to deep pavement).  Combined model:
    # every node is seeded at its route band, then the within-shape edges
    # TIGHTEN between neighbours (multi-source Dijkstra).  hi[v] =
    # min(route_hi[v], min over within-shape paths from any seed); lo
    # symmetric.  A node still infeasible after BOTH laws (lo>hi) has no
    # compliant field = a true (fundamental) infeasibility.
    route_lo, route_hi = _route_band_intervals(layout, ways, nodes, seam_nids)

    ceil_seed: dict = {}
    floor_seed: dict = {}
    for r, z in hard.items():                       # hard anchors: exact
        ceil_seed[r] = min(ceil_seed.get(r, float("inf")), z)
        floor_seed[r] = min(floor_seed.get(r, float("inf")), -z)
    for nid, hv in route_hi.items():                # route ceiling per node
        r = rep(nid)
        ceil_seed[r] = min(ceil_seed.get(r, float("inf")), hv)
    for nid, lv in route_lo.items():                # route floor per node
        r = rep(nid)
        floor_seed[r] = min(floor_seed.get(r, float("inf")), -lv)

    n_route_bounded = len(set(map(rep, route_hi)) | set(map(rep, route_lo)))
    hi = _dijkstra(None, adj, ceil_seed)
    lo_neg = _dijkstra(None, adj, floor_seed)
    lo = {v: -lo_neg[v] for v in lo_neg}
    _ = adj_win  # (windowed-reach experiment reverted — made HECA worse)

    # ALGORITHM TEST: run plain POCS (cyclic edge-projection + box-clamp) on the
    # ORACLE's proven-feasible polytope (clean route+within bounds, hold only
    # truly-hard runway/seam), seeded at the emitted field.  POCS converges to a
    # point in the intersection iff the polytope is NON-EMPTY — so if this
    # reaches ~0 violations, the solver fix is "ONE clean projection" (the
    # current 3-pass stall is from artificial holds/movement-clamps emptying the
    # polytope), and we don't need a fancier exact solver.
    seed = {}
    for _nid, _e in elev.items():
        seed.setdefault(rep(_nid), _e)
    sweeps_p, mx_p, nv_p = _pocs_solve(adj, hard, lo, hi, seed)
    print(f"  [POCS on clean polytope] sweeps={sweeps_p} resid={mx_p:.4f}m "
          f"-> {nv_p} edges still >cap+0.15m")

    # Band-pinned representatives (no compliant field exists there).
    reps = (set(adj.keys()) | set(hard.keys())
            | set(map(rep, route_hi)) | set(map(rep, route_lo)))
    pinned = {}
    for v in reps:
        h = hi.get(v, float("inf"))
        l = lo.get(v, float("-inf"))
        if l > h + 1e-6:
            pinned[v] = l - h            # infeasibility margin (m)

    # check_grade violations (validator truth) and their feasibility class.
    vios = CG._check_within_shape(ways, nodes, ll_to_m, 0.015,
                                  seam_nids, taxi_axes, routes_ll)
    fundamental = 0
    unenforced = 0
    samples_f = []
    samples_u = []
    # Map violation endpoints back to reps via coordinate match within the way.
    nid_by_xy = {}
    for nid, (la, lo_) in nodes.items():
        x, y = ll_to_m(la, lo_)
        nid_by_xy[(round(x, 2), round(y, 2))] = nid

    def _rep_near(pt):
        nid = nid_by_xy.get((round(pt[0], 2), round(pt[1], 2)))
        return rep(nid) if nid is not None else None

    def _bounds(r):
        if r is None:
            return (float("-inf"), float("inf"))
        return (lo.get(r, float("-inf")), hi.get(r, float("inf")))

    n_unmapped = 0
    n_disconnected = 0       # violation with an endpoint that has NO finite bound
    for v in vios:
        ra, rb = _rep_near(v.pt_a), _rep_near(v.pt_b)
        if ra is None or rb is None:
            n_unmapped += 1
        la, ha = _bounds(ra)
        lb, hb = _bounds(rb)
        if not (math.isfinite(la) or math.isfinite(ha)) or \
           not (math.isfinite(lb) or math.isfinite(hb)):
            n_disconnected += 1
        # Pair-level feasibility: do the two endpoint bands admit
        # |za-zb| <= cap*d ?  Infeasible iff intervals can't get within cap*d.
        cap_d = 0.015 * v.distance_m   # approx (per-axis caps vary; lower bound)
        m_a = pinned.get(ra)
        m_b = pinned.get(rb)
        gap = max(la - hb, lb - ha)    # min achievable |za-zb| if >0
        fund = (m_a is not None) or (m_b is not None) or \
               (math.isfinite(gap) and gap > cap_d + 1e-6)
        if fund:
            fundamental += 1
            if len(samples_f) < 6:
                samples_f.append((v, (la, ha), (lb, hb)))
        else:
            unenforced += 1
            if len(samples_u) < 6:
                samples_u.append((v, (la, ha), (lb, hb)))

    print(f"\n=== {icao}: {len(vios)} within-shape violation(s) | "
          f"graph {len(reps)} reps / {n_edges} edges / {len(hard)} hard "
          f"anchors | {len(pinned)} band-pinned rep(s) ===")
    print(f"  FUNDAMENTAL (no compliant field exists → W3/W5): {fundamental}")
    print(f"  FEASIBLE-but-unenforced (→ W2/W4):              {unenforced}")
    print(f"  [sanity] route-bounded reps={n_route_bounded} | viol endpoints "
          f"unmapped={n_unmapped} unbounded={n_disconnected} (of {len(vios)})")

    def _fmt(b):
        l, h = b
        ls = f"{l:.1f}" if math.isfinite(l) else "-inf"
        hs = f"{h:.1f}" if math.isfinite(h) else "+inf"
        return f"[{ls},{hs}]"
    for v, ba, bb in samples_f:
        print(f"    [FUND]  {v.way_a.tags.get('role')} {v.grade_pct:.1f}% "
              f"d={v.distance_m:.1f} de={v.de_m:.2f} "
              f"bands {_fmt(ba)} {_fmt(bb)}")
    for v, ba, bb in samples_u:
        print(f"    [unenf] {v.way_a.tags.get('role')} {v.grade_pct:.1f}% "
              f"d={v.distance_m:.1f} de={v.de_m:.2f} "
              f"bands {_fmt(ba)} {_fmt(bb)}")
    return {"vios": len(vios), "fundamental": fundamental,
            "unenforced": unenforced, "pinned": len(pinned)}


def main(argv):
    from conftest import xplane_root
    from auto_patch.pipeline import build_airport_pavement
    icaos = argv[1:] or ["SPJC"]
    summary = {}
    for icao in icaos:
        layout = build_airport_pavement(icao, xplane_root(),
                                        compute_elevations=True)
        summary[icao] = audit_layout(layout, icao)
    print("\n=== SUMMARY ===")
    for icao, s in summary.items():
        if s:
            print(f"  {icao}: {s['vios']} viol = {s['fundamental']} fundamental "
                  f"+ {s['unenforced']} unenforced  ({s['pinned']} pinned reps)")


if __name__ == "__main__":
    main(sys.argv)
