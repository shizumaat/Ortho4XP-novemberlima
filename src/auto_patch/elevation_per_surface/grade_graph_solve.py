"""The connecting solve (single-grade-graph generation, Phase 3).

With the HARD anchors fixed (runway thresholds + tile seams + the route-feasible
closest-to-DEM building pads), solve every free airside node to the SMOOTHEST
(minimum grade + curvature) surface that stays within grade across the unified
within-shape graph.  See ``docs/single_grade_graph.md``.

Two stages, both direct — NO long iterative propagation:

1. **Feasibility bands** ``[floor_i, ceiling_i]`` per node, by a multi-source
   Dijkstra over the cap-weighted graph (edge weight = the per-edge limit
   ``cap*dist`` in metres).  ``ceiling_i = min over anchors a of (elev_a +
   capdist(i,a))``; ``floor_i`` symmetric.  The band IS the global constraint —
   it carries every anchor's reach to every node in one pass, so the smoothing
   that follows is purely LOCAL (a handful of sweeps, not thousands).

2. **Smooth within the band**: Gauss-Seidel toward the minimum grade+curvature
   value (harmonic neighbour mean blended with the curvature/Laplacian term),
   clamped to ``[floor, ceiling]`` and cap-projected each sweep.

The model says there is NO genuine infeasibility (every airport has a feasible
solution); if ``floor_i > ceiling_i`` anywhere, that is a measurement/data bug to
surface, not a surface to fudge — we log it.
"""
from __future__ import annotations

import heapq
import math
import os as _os


def _build_adjacency(shape_constraints, n):
    """``adj[i] = [(j, lim, w), ...]`` where ``lim`` = per-edge max |Δelev| (m)
    and ``w`` = grade-weight (1/dist²)."""
    adj: dict = {}
    for sc in shape_constraints:
        for (i, j, lim) in sc["edges"]:
            if lim is None or lim < 0:
                continue
            adj.setdefault(i, []).append((j, lim))
            adj.setdefault(j, []).append((i, lim))
    return adj


def _dijkstra_envelope(adj, sources, sign):
    """Multi-source Dijkstra lower envelope.  ``sources`` = ``{node: value}``;
    returns ``{node: min over s of (value_s + dist_lim(node, s))}`` where
    dist_lim sums the per-edge ``lim`` weights.  ``sign`` flips for the floor
    (run with negated source values, negate the result outside)."""
    best: dict = {}
    pq: list = []
    for s, v in sources.items():
        val = sign * v
        if s not in best or val < best[s]:
            best[s] = val
            heapq.heappush(pq, (val, s))
    while pq:
        d, u = heapq.heappop(pq)
        if d > best.get(u, math.inf):
            continue
        for (v, lim) in adj.get(u, ()):  # lim >= 0
            nd = d + lim
            if nd < best.get(v, math.inf):
                best[v] = nd
                heapq.heappush(pq, (nd, v))
    return best


def _bands(adj, anchors):
    """Return (floor, ceiling) dicts from the anchor set over the cap-weighted
    graph."""
    ceil = _dijkstra_envelope(adj, anchors, +1.0)               # min(elev+dist)
    floor_neg = _dijkstra_envelope(adj, anchors, -1.0)          # min(-elev+dist)
    floor = {k: -v for k, v in floor_neg.items()}
    return floor, ceil


def connecting_solve(elev, shape_constraints, base_hard, nodes, hard_extra,
                     building_pads=None, dem_elev=None, max_sweeps=400,
                     tol=0.002, curvature=0.25) -> int:
    """Solve the free airside nodes to min grade+curvature within their
    feasibility bands, holding the hard anchors.  Buildings are LOCKED on the
    connecting graph's OWN bands (closest-to-DEM within their pad band-
    intersection) — NOT pre-pinned from the route graph, which would
    manufacture infeasibility.  Mutates ``elev``; returns #free nodes solved."""
    adj = _build_adjacency(shape_constraints, len(elev))
    if not adj:
        return 0
    n = len(elev)
    building_pads = building_pads or []
    bld_nodes = {i for pad in building_pads for i in pad}

    # PASS 1 bands from the TRUE anchors only (runway thresholds + seams), NOT
    # buildings — so a building's reachable range is what the network can
    # actually deliver, and we seat the pad inside it (consistent by
    # construction).
    base_anchors = {k: elev[k] for k in adj
                    if (k in hard_extra
                        or (k < n and base_hard[k] and k not in bld_nodes))}
    if not base_anchors:
        return 0
    floor1, ceil1 = _bands(adj, base_anchors)

    # LOCK each building pad FLAT at closest-to-DEM within its band-intersection.
    locked: dict = {}
    for pad in building_pads:
        present = [i for i in pad if i in floor1 and i in ceil1]
        if not present:
            continue
        lo = max(floor1[i] for i in present)
        hi = min(ceil1[i] for i in present)
        dems = [dem_elev[i] for i in present
                if dem_elev is not None and dem_elev[i] is not None]
        tgt = (sorted(dems)[len(dems) // 2] if dems
               else 0.5 * (lo + hi))
        if lo <= hi:
            tgt = min(max(tgt, lo), hi)
        else:                                   # infeasible band → midpoint
            tgt = 0.5 * (lo + hi)
        for i in pad:
            elev[i] = tgt
            locked[i] = tgt

    def _hard(k):
        return (k >= n or base_hard[k] or k in hard_extra or k in locked)

    # PASS 2 bands now INCLUDE the locked buildings, so free nodes near a pad
    # are tightened toward it.
    anchors = {k: elev[k] for k in adj if _hard(k)}
    free = [k for k in adj if not _hard(k)]
    if not free:
        return 0
    floor, ceil = _bands(adj, anchors)
    infeasible = sum(1 for k in free
                     if floor.get(k, -math.inf) - ceil.get(k, math.inf) > 1e-3)
    if infeasible and _os.environ.get("O4_STEP_DEBUG") == "1":
        print(f"  [connecting-solve] WARN {infeasible} node(s) with "
              f"floor>ceiling — measurement/data bug (model says feasible)")

    # 2. seed each free node inside its band, closest to DEM / current
    for k in free:
        lo = floor.get(k, -math.inf)
        hi = ceil.get(k, math.inf)
        seed = elev[k]
        if dem_elev is not None and dem_elev[k] is not None:
            seed = dem_elev[k]
        if lo > hi:                      # infeasible — sit at the midpoint
            elev[k] = 0.5 * (lo + hi)
        else:
            elev[k] = min(max(seed, lo), hi)

    # weights for the harmonic (grade) term: 1/dist² via lim (lim = cap*dist,
    # but cap is ~uniform within a shape so 1/lim² ∝ 1/dist²; guard lim→0)
    wadj: dict = {}
    for i in free:
        lst = []
        for (j, lim) in adj[i]:
            w = 1.0 / max(lim, 1e-3) ** 2
            lst.append((j, lim, w))
        wadj[i] = lst

    # 3. PROJECTED Gauss-Seidel: each free node lands directly in its
    # cap-feasible interval (intersection of the neighbour slabs |z_i−z_j|≤lim
    # ∩ band), pulled toward the min grade+curvature target.  Landing in the
    # feasible interval each update makes this monotone/convergent — no
    # harmonic-vs-projection oscillation (the 400-sweep non-convergence).
    for _it in range(max_sweeps):
        moved = 0.0
        for i in free:
            lst = wadj[i]
            sw = acc = 0.0
            for (j, _lim, w) in lst:
                sw += w
                acc += elev[j] * w
            if sw <= 0.0:
                continue
            harm = acc / sw
            if curvature > 0.0:
                pm = sum(elev[j] for (j, _l, _w) in lst) / len(lst)
                tgt = (1.0 - curvature) * harm + curvature * pm
            else:
                tgt = harm
            # cap-feasible interval w.r.t. the CURRENT neighbours, ∩ band
            lo_e = floor.get(i, -math.inf)
            hi_e = ceil.get(i, math.inf)
            for (j, lim, _w) in lst:
                ej = elev[j]
                if ej - lim > lo_e:
                    lo_e = ej - lim
                if ej + lim < hi_e:
                    hi_e = ej + lim
            if lo_e <= hi_e:
                tgt = min(max(tgt, lo_e), hi_e)
            else:                          # locally over-constrained → midpoint
                tgt = 0.5 * (lo_e + hi_e)
            d = tgt - elev[i]
            if d:
                elev[i] = tgt
                if abs(d) > moved:
                    moved = abs(d)
        if moved < tol:
            break
    if _os.environ.get("O4_STEP_DEBUG") == "1":
        # in-solve residual: edges in OUR graph still over cap, split by whether
        # both endpoints are hard (unfixable here) vs has-a-free (solver gap)
        bh = hf = 0
        seen = set()
        for i in adj:
            for (j, lim) in adj[i]:
                e = (min(i, j), max(i, j))
                if e in seen:
                    continue
                seen.add(e)
                if abs(elev[i] - elev[j]) - lim > 1e-3:
                    if _hard(i) and _hard(j):
                        bh += 1
                    else:
                        hf += 1
        print(f"  [connecting-solve] {len(free)} free node(s), "
              f"{_it + 1} sweep(s), infeasible={infeasible}; "
              f"in-solve residual edges: both-hard={bh} has-free={hf}")
    return len(free)
