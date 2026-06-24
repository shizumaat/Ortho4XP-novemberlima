"""The SPINE-CARRIES-CLIMB connecting solve (single-grade-graph generation).

The HARD anchors are the runway thresholds (the FAA-solved runway surface) +
tile seams + the route-feasible closest-to-DEM BUILDING pads.  Every other
airside node (the taxi SPINE through aprons/junctions + the apron/junction BODY)
is solved to the SMOOTHEST grade (minimum Σ grade²) that connects those anchors
across the unified within-shape grade graph, subject to the per-edge caps.

The runway→building climb EMERGES along the taxi network — the spine carries it
because the smoothest cap-bounded surface between a low runway threshold and a
high building distributes the rise over the network's length, and the apron body
grades ≤cap off whatever the network settles at.  A monotone projected
Gauss-Seidel (each free node lands directly in its neighbour cap-slab
intersection, pulled toward the min grade+curvature target) converges without the
harmonic-vs-projection oscillation.  See ``docs/single_grade_graph.md``.
"""
from __future__ import annotations

import math
import os as _os


def _build_adjacency(shape_constraints, n):
    """``adj[i] = [(j, lim), ...]`` where ``lim`` = per-edge max |Δelev| (m)."""
    adj: dict = {}
    for sc in shape_constraints:
        for (i, j, lim) in sc["edges"]:
            if lim is None or lim < 0:
                continue
            adj.setdefault(i, []).append((j, lim))
            adj.setdefault(j, []).append((i, lim))
    return adj


def spine_carries_climb_solve(
        elev, shape_constraints, base_hard, nodes, hard_extra,
        hard_seats, dem_elev=None, max_sweeps=2000, tol=0.001,
        curvature=0.25) -> int:
    """SPINE-CARRIES-CLIMB connecting solve (docs/single_grade_graph.md ★).

    The HARD anchors are the runway thresholds (the FAA-solved runway surface —
    UNCHANGED here) + tile seams + the buildings (LOCKED FLAT at their
    route-feasible closest-to-DEM level, ``hard_seats`` — the heaviest anchor).
    Every other airside node (the taxi SPINE + the apron/junction BODY) is FREE.

    The solver finds the **smoothest grade** (minimum Σ grade²) surface that
    connects those anchors across the within-shape connecting graph, subject to
    the per-edge caps.  The runway→building climb therefore EMERGES along the
    taxi network: the spine carries it because the smoothest cap-bounded surface
    between a low runway threshold and a high building distributes the rise over
    the network's length, and the apron body grades ≤cap off whatever the
    network settles at.  No precomputed spine profile, no global band — just the
    harmonic min-grade minimiser + a cap projection, iterated to convergence.

    Mutates ``elev``; returns #free nodes solved.
    """
    adj = _build_adjacency(shape_constraints, len(elev))
    if not adj:
        return 0
    n = len(elev)

    # LOCK each building FLAT at its route-feasible closest-to-DEM level.
    locked: dict = {}
    for i, lv in hard_seats.items():
        if lv is None or i >= n:
            continue
        elev[i] = lv
        locked[i] = lv

    def _hard(k):
        return (k >= n or base_hard[k] or k in hard_extra or k in locked)

    free = [k for k in adj if not _hard(k)]
    if not free:
        return 0

    # inverse-distance² weights (1/lim² ∝ 1/dist² within a cap) for the harmonic
    # (min Σ grade²) minimiser; lim = per-edge cap·dist for the slab projection.
    wadj: dict = {}
    for i in free:
        wadj[i] = [(j, max(lim, 1e-3), 1.0 / max(lim, 1e-3) ** 2)
                   for (j, lim) in adj[i]]

    # PROJECTED Gauss-Seidel (monotone/convergent — no harmonic-vs-projection
    # oscillation): each free node lands DIRECTLY in the intersection of its
    # neighbour cap slabs |z_i−z_j|≤lim (the locally cap-feasible interval),
    # pulled toward the min grade+curvature target.  No global band, so the only
    # residual is a genuinely over-constrained spot (two hard anchors too close
    # at too-different levels = a real cliff / spurious short path to surface).
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
            lo_e, hi_e = -math.inf, math.inf
            for (j, lim, _w) in lst:
                ej = elev[j]
                if ej - lim > lo_e:
                    lo_e = ej - lim
                if ej + lim < hi_e:
                    hi_e = ej + lim
            if lo_e <= hi_e:
                tgt = min(max(tgt, lo_e), hi_e)
            else:
                # locally over-constrained (a canyon node between stepped
                # anchors): UNDER-RELAX toward the midpoint so it stops
                # ping-ponging its neighbours — that oscillation otherwise
                # never lets the rest of the network (the SPINE) converge.
                tgt = elev[i] + 0.5 * (0.5 * (lo_e + hi_e) - elev[i])
            d = tgt - elev[i]
            if d:
                elev[i] = tgt
                if abs(d) > moved:
                    moved = abs(d)
        if moved < tol:
            break
    # genuinely over-constrained nodes (slab intersection empty given the
    # current neighbours) — the irreducible cliffs (real terrain step or a
    # spurious short path that survived the spine-crossing drop)
    infeasible = 0
    for i in free:
        lo_e, hi_e = -math.inf, math.inf
        for (j, lim, _w) in wadj[i]:
            ej = elev[j]
            if ej - lim > lo_e:
                lo_e = ej - lim
            if ej + lim < hi_e:
                hi_e = ej + lim
        if lo_e - hi_e > 1e-3:
            infeasible += 1
    if _os.environ.get("O4_STEP_DEBUG") == "1":
        print(f"  [spine-climb] converged at sweep {_it + 1} "
              f"(moved={moved:.4f}); over-constrained free nodes={infeasible}")

    if _os.environ.get("O4_STEP_DEBUG") == "1":
        def _typ(k):
            if k in locked:
                return "lock"
            if k >= n or k in hard_extra:
                return "rwy"
            if base_hard[k]:
                return "seam"
            return "free"
        seen = set()
        bh = hf = 0
        worst = []
        for i in adj:
            for (j, lim) in adj[i]:
                e = (min(i, j), max(i, j))
                if e in seen:
                    continue
                seen.add(e)
                ex = abs(elev[i] - elev[j]) - lim
                if ex > 1e-3:
                    if _hard(i) and _hard(j):
                        bh += 1
                    else:
                        hf += 1
                    d = math.hypot(nodes[i][0] - nodes[j][0],
                                   nodes[i][1] - nodes[j][1])
                    worst.append((ex, _typ(i), _typ(j), d,
                                  elev[i], elev[j], lim))
        worst.sort(reverse=True)
        print(f"  [spine-climb] {len(free)} free node(s), {_it + 1} sweep(s), "
              f"locked={len(locked)} infeasible={infeasible}; "
              f"residual edges both-hard={bh} has-free={hf}")
        from collections import Counter
        c = Counter((t1, t2) for (_e, t1, t2, *_r) in worst)
        print(f"    [resid by type] {dict(c)}")
        for (ex, t1, t2, d, ei, ej, lim) in worst[:12]:
            print(f"    [resid] {t1}/{t2} ex={ex:.2f}m d={d:.1f}m "
                  f"lev {ei:.1f}/{ej:.1f} lim={lim:.2f}")
    return len(free)
