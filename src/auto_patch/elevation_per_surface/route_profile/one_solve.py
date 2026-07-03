"""The ONE-PROFILE solve (user spec 2026-06-24; docs/one_profile_solve.md).

The single source of elevation truth for the airside network, and it uses ONE
graph: the reach band on the unified grade graph
(``building_feasibility.reach_band_unified``).
That band sets the building levels AND bounds every apron / spine / rect node, so
they agree by construction — there is no second reachability graph.

Every free node gets ``[floor_i, ceil_i]`` from ``node_band`` (the reach band at
the node) and is pulled to a per-role target, clamped into ``[floor_i, ceil_i] ∩
the neighbour cap slabs`` by a projected Gauss-Seidel pass:

* APRON BODY → closest-to-DEM within the band (it rises to the building it fronts
  through the shared frontage edge, a neighbour cap slab — no building floor
  anchor needed).
* taxi SPINE + RECT ends → smoothest (min curvature); the spine clamps ONLY to
  its centerline-consecutive neighbours (a 1-D feasible chain) so the apron
  yields to it.  A rect tilts as a plane (flat across width via the ``cap=0``
  cross edges + the final couple); it climbs because its band ceiling near a
  high building/terminal is high.

Buildings, runway contacts and tile seams are fixed anchors (buildings flat at
their FRONTAGE-reachable level — see ``anchors.build_building_seats``).
"""
from __future__ import annotations

import math
import os as _os

_INF = float("inf")

# EXPERIMENTAL (user 2026-06-30): vectorise feasibility_project's Gauss-Seidel
# projection with numpy.  This converts it to a DEGREE-NORMALISED JACOBI sweep
# (all edges updated from the same snapshot each iteration, per-node corrections
# averaged for stability) — ~orders faster per iteration, but a DIFFERENT (still
# grade-compliant) feasible surface, so NOT byte-identical.  Default OFF; the
# scalar path stays the byte-identical default until this is validated (elevation
# delta small, residual violations equivalent-or-better) and re-baselined.
_FP_VECTORIZE = _os.environ.get("O4_FP_VECTORIZE", "0") == "1"


def _build_adjacency(shape_constraints, n):
    """``adj[i] = [(j, budget), ...]`` where ``budget = cap·length`` (the max
    |Δelev| the edge may carry).  ``budget`` may be 0 (a rect flat-cross edge →
    the two corners stay equal).  Unregulated edges (``None``/negative) are
    skipped — they impose no cap and so do not bound the envelope."""
    adj: dict = {}
    for sc in shape_constraints:
        for (i, j, lim) in sc["edges"]:
            if lim is None or lim < 0 or i >= n or j >= n or i == j:
                continue
            adj.setdefault(i, []).append((j, lim))
            adj.setdefault(j, []).append((i, lim))
    return adj


def _project_vectorized(elev, iter_edges, n, max_iters, tol):
    """Vectorised DEGREE-NORMALISED JACOBI variant of the feasibility projection
    (gated by ``_FP_VECTORIZE``).  Mutates ``elev`` (a list) in place.

    Every iteration updates ALL nodes from the same snapshot (Jacobi, not
    Gauss-Seidel), so it vectorises with numpy — but a node touched by many
    over-cap edges would OVERSHOOT if the corrections were summed, so each node's
    correction is AVERAGED over its active edges (``acc / cnt``).  A hard
    endpoint's weight (``wi``/``wj``) is 0 on every edge it touches, so hard nodes
    never move — same invariant as the scalar path.  Converges to a DIFFERENT
    (still ≤cap) feasible surface than Gauss-Seidel, hence not byte-identical."""
    import numpy as np
    m = len(iter_edges)
    I = np.fromiter((e[0] for e in iter_edges), dtype=np.intp, count=m)
    J = np.fromiter((e[1] for e in iter_edges), dtype=np.intp, count=m)
    B = np.fromiter((e[2] for e in iter_edges), dtype=np.float64, count=m)
    K = np.fromiter((e[3] for e in iter_edges), dtype=np.int8, count=m)
    wi = np.where(K == 0, 0.5, np.where(K == 2, 1.0, 0.0))   # i's share of the fix
    wj = np.where(K == 0, 0.5, np.where(K == 1, 1.0, 0.0))   # j's share
    z = np.asarray(elev, dtype=np.float64)
    for _it in range(max_iters):
        d = z[I] - z[J]
        over = np.abs(d) - B
        active = over > tol
        if not active.any():
            break
        # signed excess per ACTIVE edge (0 elsewhere) — scatter-add to endpoints
        # via bincount (true C scatter, far faster than np.add.at).
        se = np.where(active, np.sign(d) * over, 0.0)
        acc = (np.bincount(I, weights=-se * wi, minlength=n)
               + np.bincount(J, weights=se * wj, minlength=n))
        af = active.astype(np.float64)
        cnt = (np.bincount(I, weights=af, minlength=n)
               + np.bincount(J, weights=af, minlength=n))
        nz = cnt > 0.0
        z[nz] += acc[nz] / cnt[nz]                          # degree-normalised step
    elev[:] = z.tolist()


def feasibility_project(elev, shape_constraints, hard, *,
                        max_iters=4000, tol=1e-3, force_scalar=False,
                        flat_groups=None):
    """Drive EVERY grade-graph edge to ``|Δelev| ≤ budget`` by iterative
    constraint projection (user 2026-06-25: nothing may violate a grade cap).

    This is a Gauss-Seidel relaxation of the difference-constraint system
    ``|z_i − z_j| ≤ cap_ij·d_ij`` on the SAME graph the validator checks
    (``shape_constraints`` = ``grade_graph``).  ``hard`` nodes (buildings, runway,
    seams) are immovable; everything else — including the route SKELETON — may
    flex.  An over-cap edge moves its free endpoint(s) just enough to satisfy it
    (split the excess when both are free, all of it onto the free one otherwise);
    repeated sweeps converge to a cap-Lipschitz surface whenever the anchors admit
    one.  Edges between two hard nodes are genuinely infeasible and reported, not
    forced.  Mutates ``elev`` in place; returns ``(remaining_over_cap, both_hard)``.

    ``flat_groups`` — optional list of node-index sets, each a RIGID FLAT group
    (a building pad): its members share ONE elevation that the projection may
    move as a unit (the feasibility-audit model — buildings are movable flat
    groups; holding every pad hard at its pre-picked seat makes the polytope
    infeasible through chained paths even when no single edge is both-hard).
    Each group collapses to a representative node; member↔member edges vanish,
    member↔outside edges re-anchor to the representative with their own budget,
    and the representative's final level is broadcast back to all members.  A
    group containing a ``hard`` node stays entirely hard (never moved).
    """
    import heapq
    n = len(elev)

    # ── flat groups → representative mapping ─────────────────────────────
    gmap: dict = {}
    groups_eff: list = []
    if flat_groups:
        # merge overlapping groups (two touching pads sharing a ring node act
        # as one rigid unit), then map member → representative.
        pool = [set(g) for g in flat_groups if g]
        merged: list = []
        for g in pool:
            attached = None
            for mg in merged:
                if mg & g:
                    mg |= g
                    attached = mg
                    break
            if attached is None:
                merged.append(set(g))
        for g in merged:
            g = {i for i in g if 0 <= i < n}
            if len(g) < 2:
                continue
            if g & hard:
                continue                      # runway/seam-welded pad: stays hard
            rep = min(g)
            groups_eff.append((rep, g))
            for m in g:
                if m != rep:
                    gmap[m] = rep
        # a skipped (hard-welded) group must stay rigid: hold all its members.
        hard = set(hard)
        for g in merged:
            g = {i for i in g if 0 <= i < n}
            if len(g) >= 2 and (g & hard):
                hard |= g
        # seed each representative at the group's current (flat) level.
        for rep, g in groups_eff:
            elev[rep] = sum(elev[m] for m in g) / len(g)

    def _r(i):
        return gmap.get(i, i)

    edges = []
    seen = set()
    adj: dict = {}
    for sc in shape_constraints:
        for (i, j, lim) in sc["edges"]:
            if lim is None or lim < 0 or i >= n or j >= n:
                continue
            i, j = _r(i), _r(j)
            if i == j:
                continue
            e = (i, j) if i < j else (j, i)
            if e in seen:
                continue
            seen.add(e)
            edges.append((e[0], e[1], lim))
            adj.setdefault(i, []).append((j, lim))
            adj.setdefault(j, []).append((i, lim))
    if not edges:
        return 0, 0

    # EXACT reachability envelope: ceil_i = min over hard anchors a of
    # (z_a + capdist(a→i)), floor_i = max of (z_a − capdist).  ``budget`` is the
    # edge's cap·length, so this is the steepest-compliant reach of every anchor.
    # Both envelopes are cap-Lipschitz, so clamping into [floor, ceil] removes all
    # gross (anchor-driven) infeasibility in ONE shot — the iterative pass then
    # only resolves free↔free edges, which converges fast.
    INF = float("inf")

    def _reach(sign):                       # sign +1 → ceil, −1 → floor
        best: dict = {}
        pq = [((elev[a] if sign > 0 else -elev[a]), a) for a in hard if a < n]
        heapq.heapify(pq)
        while pq:
            val, k = heapq.heappop(pq)
            t = val if sign > 0 else -val
            if k in best and ((sign > 0 and t >= best[k])
                              or (sign < 0 and t <= best[k])):
                continue
            best[k] = t
            for (j, lim) in adj.get(k, ()):
                nt = t + sign * lim
                pj = best.get(j)
                if pj is None or (sign > 0 and nt < pj) or (sign < 0 and nt > pj):
                    heapq.heappush(pq, ((nt if sign > 0 else -nt), j))
        return best

    if hard:
        ceil = _reach(+1)
        floor = _reach(-1)
        for i in range(n):
            if i in hard:
                continue
            lo = floor.get(i, -INF)
            hi = ceil.get(i, INF)
            if lo > hi:
                elev[i] = 0.5 * (lo + hi)            # genuine: minimise the break
            else:
                elev[i] = min(max(elev[i], lo), hi)  # clamp into the envelope

    # Pre-split the edges ONCE by hard-membership.  The inner loop otherwise ran
    # two ``in hard`` set lookups PER edge PER iteration (up to ~0.5 B lookups on
    # a big airport).  Both-hard edges can never move, so drop them from the
    # iteration entirely (they are only counted in the final tally below).
    # ``kind``: 0 = both free (split the excess), 1 = i hard (move j), 2 = j hard.
    iter_edges = []
    for (i, j, budget) in edges:
        hi = i in hard
        hj = j in hard
        if hi and hj:
            continue
        iter_edges.append((i, j, budget, 1 if hi else (2 if hj else 0)))

    # Under the GLOBAL-SLICE spine the graph is ~4x the rect model's
    # (SPJC 110k edges) and the scalar loop costs ~60 s/build across its
    # call sites — the vectorised Jacobi is the default there (the
    # byte-identity concern only ever applied to the legacy rect path).
    # ``force_scalar`` (the FINAL projection): degree-normalised Jacobi has
    # no convergence guarantee on a difference-constraint system — it stalls
    # with thousands of edges marginally over cap, while the scalar loop is
    # cyclic Gauss-Seidel POCS, which converges to a point of the (non-empty)
    # polytope (the feasibility audit measures 0-fundamental and its own POCS
    # reaches residual ~0 in <100 sweeps).  The last projection before
    # writeback therefore runs scalar — seeded by the fast Jacobi passes, so
    # it needs few sweeps.
    _vec = not force_scalar and _FP_VECTORIZE
    if not _vec and not force_scalar:
        try:
            from auto_patch.config import (CURVE_NATIVE_SPINE as _CNS,
                                           ROUTE_ARC_SPINE as _RAS)
            _vec = _CNS or _RAS
        except Exception:
            _vec = False
    _sweeps_run = 0
    _last_worst = 0.0
    if _vec and iter_edges:
        _project_vectorized(elev, iter_edges, n, max_iters, tol)
    else:
        for _it in range(max_iters):
            _sweeps_run = _it + 1
            worst = 0.0
            for (i, j, budget, kind) in iter_edges:
                d = elev[i] - elev[j]
                ad = -d if d < 0.0 else d                  # inline abs() (hot path)
                if ad <= budget + tol:
                    continue
                ex = ad - budget
                s = 1.0 if d > 0 else -1.0
                if kind == 0:
                    elev[i] -= s * ex * 0.5
                    elev[j] += s * ex * 0.5
                elif kind == 1:
                    elev[j] += s * ex                      # i fixed → move j up to i
                else:
                    elev[i] -= s * ex                      # j fixed → move i
                if ex > worst:
                    worst = ex
            _last_worst = worst
            if worst < tol:
                break
    # broadcast each flat group's representative level back to its members.
    for rep, g in (groups_eff if flat_groups else ()):
        for m in g:
            elev[m] = elev[rep]
    # final tally
    rem = bh = 0
    worst_ex = 0.0
    for (i, j, budget) in edges:
        ex = abs(elev[i] - elev[j]) - budget
        if ex > tol:
            rem += 1
            worst_ex = max(worst_ex, ex)
            if i in hard and j in hard:
                bh += 1
    if _os.environ.get("O4_STEP_DEBUG") == "1" and force_scalar:
        print(f"    [fp-scalar] sweeps={_sweeps_run} last_worst={_last_worst:.4f} "
              f"rem={rem} worst_ex={worst_ex:.3f} groups={len(groups_eff)}")
    return rem, bh


def one_profile_solve(
        elev, shape_constraints, base_hard, nodes, dem_elev,
        runway_nodes, building_seats, apron_body, spine_nodes, spine_adj,
        node_band, spine_floor, coupling, *,
        max_sweeps=3000, tol=0.001, omega=None, curvature=0.25,
        apron_smooth=None):
    """Run the one-profile solve.  Mutates ``elev`` in place; returns #free nodes.

    ``base_hard`` — runway + seam HARD mask (anchors at their seeded elevation).
    ``runway_nodes`` — runway / runway-crossing node indices (anchors).
    ``building_seats`` — ``{pad_node: flat_level}`` (anchors, the heaviest).
    ``dem_elev`` — per-node DEM (the closest-to-DEM target).
    ``node_band`` — per-node ``(floor, ceiling)`` reachability from THE ONE graph
      (``building_feasibility.reach_band_unified`` — the SAME band that
      sets the building levels, so building and apron/spine agree by construction)
      or ``None`` (off-network → unconstrained, the neighbour cap slabs bound it).
    ``coupling`` — rect flat-end groups (members share one elevation).
    """
    n = len(elev)
    if omega is None:
        omega = float(_os.environ.get("O4_RP_OMEGA", "1.0"))
    # Apron body target: closest-to-DEM (default) vs SMOOTH (grade between the
    # apron's anchored edges + spine — user model "aprons grade building→edge/
    # spine, NOT DEM").
    _apron_smooth = (apron_smooth if apron_smooth is not None
                     else _os.environ.get("O4_RP_APRON_SMOOTH", "0") == "1")
    adj = _build_adjacency(shape_constraints, n)
    if not adj:
        return 0

    # ANCHORS — fixed elevations: runway contacts, tile seams, and the building
    # pads (the heaviest, flat at their FRONTAGE-reachable level).  Everything
    # else is bounded by ``node_band`` (the ONE taxi-route reach band) and graded
    # ≤cap to its neighbours by the projection — the apron rises to the building
    # it fronts through the shared frontage edge (a neighbour cap slab), so no
    # second reachability graph is needed.
    anchors: dict = {}
    for i in range(n):
        if base_hard[i]:
            anchors[i] = elev[i]
    for i in runway_nodes:
        if i < n:
            anchors[i] = elev[i]
    for i, lv in building_seats.items():            # buildings win (heaviest)
        if lv is not None and i < n:
            elev[i] = float(lv)
            anchors[i] = float(lv)

    # Per-node reachability bounds from the ONE graph (the reach band) — applied
    # to EVERY node, the apron body included (user 2026-06-26): an apron node sits
    # at CLOSEST-DEM-FEASIBLE = its DEM clamped into [floor, ceiling], so a
    # wrong-LOW DEM fills UP to the floor (the west apron 662–685 → ~693) and a
    # wrong-HIGH DEM pulls DOWN to the ceiling (#156's terminal 715 → 707–710,
    # graded toward runway 02).  The apron does NOT inherit the band ceiling's 3 %
    # climb directly because the within-shape 1 % NEIGHBOUR cap slab (below) also
    # bounds each node — so it grades ≤1 % within the band, exactly the model.
    floor: dict = {}
    ceil: dict = {}
    for i in range(n):
        b = node_band[i] if i < len(node_band) else None
        if b is not None:
            lo, hi = b
            if lo > hi:                              # rare band inversion
                lo = hi = 0.5 * (lo + hi)
            floor[i], ceil[i] = lo, hi
    # BUILDING-FRONTAGE SPINE FLOORS (user 2026-06-25): the serving spine RISES
    # to serve its pads.  ``spine_floor`` is a cap-LIPSCHITZ floor propagated
    # along the consecutive centerline chain from each building's foot anchor
    # (``anchors.building_spine_floor``) — decreasing at exactly the cap rate, so
    # it is grade-consistent BY CONSTRUCTION and can never force a spine break.
    # Because each chain node's neighbour is also floored, the "envelope yields"
    # fallback below no longer drops it (the single-node floor it replaced was
    # dropped whenever the flat runway-side neighbour capped it low → arm stayed
    # flat, CYXY ~U12 694.5 vs building19 700.2).
    for i, f in spine_floor.items():
        if i < n:
            hi = ceil.get(i, _INF)
            cur = floor.get(i, -_INF)
            ff = min(f, hi) if hi < _INF else f
            if ff > cur:
                floor[i] = ff

    free = [k for k in adj if k not in anchors]
    if not free:
        return 0
    free_set = set(free)

    # inverse-budget² weights for the ROUTE (smoothness / min-curvature) target.
    wadj: dict = {}
    for i in free:
        wadj[i] = [(j, lim, 1.0 / max(lim, 1e-3) ** 2) for (j, lim) in adj[i]]
    # SPINE nodes clamp ONLY to their centerline-CONSECUTIVE neighbours (a 1-D,
    # always-feasible chain within the envelope) — so the apron body yields to
    # the spine instead of squeezing it out of grade.  Consecutive-only is
    # essential: pulling in the non-consecutive within-shape spine pairs
    # re-couples the spine to the apron squeeze it must stay clear of.
    wspine: dict = {}
    for i in spine_nodes:
        if i in free_set:
            nb = [(j, lim, 1.0 / max(lim, 1e-3) ** 2)
                  for (j, lim) in spine_adj.get(i, ())
                  if j in free_set or j in anchors]
            if nb:
                wspine[i] = nb

    def _dem_target(i):
        """Closest-to-DEM within the node's reachable envelope (the APRON BODY
        target).  Midpoint when DEM is missing or the envelope is degenerate."""
        lo = floor.get(i, -_INF)
        hi = ceil.get(i, _INF)
        de = dem_elev[i] if i < len(dem_elev) else None
        if lo > hi:                                  # unreachable conflict
            return 0.5 * (lo + hi)
        if de is None:
            if lo == -_INF or hi == _INF:
                return elev[i]                       # unconstrained → hold seed
            return 0.5 * (lo + hi)
        return min(max(de, lo), hi)

    # INITIALISE every free node at its closest-DEM-in-envelope value (a warm
    # start near the answer; route nodes then smooth toward min curvature).
    for i in free:
        elev[i] = _dem_target(i)

    # Coupled groups (rect flat-ends) restricted to free members.
    groups: list = []
    seen_g: set = set()
    for i in free:
        grp = coupling.get(i)
        if not grp or i in seen_g:
            continue
        members = [m for m in grp if m in free_set]
        for m in grp:
            seen_g.add(m)
        if len(members) > 1:
            groups.append(members)

    moved = _INF
    for _it in range(max_sweeps):
        moved = 0.0
        for i in free:
            spine = wspine.get(i)
            if spine is not None:
                lst = spine                          # spine: centerline only
            else:
                lst = wadj[i]                         # body / rect: all neighbours
            if spine is None and i in apron_body and not _apron_smooth:
                tgt = _dem_target(i)                 # apron body → closest-DEM
            else:
                # spine + rect ends → smoothest (min curvature): inverse-budget²
                # harmonic mean blended with the plain mean.
                sw = acc = 0.0
                for (j, _l, w) in lst:
                    sw += w
                    acc += elev[j] * w
                harm = acc / sw if sw > 0 else elev[i]
                pm = sum(elev[j] for (j, _l, _w) in lst) / len(lst)
                tgt = (1.0 - curvature) * harm + curvature * pm
            # neighbour cap slab (spine: centerline chain only; else all edges)
            n_lo, n_hi = -_INF, _INF
            for (j, lim, _w) in lst:
                ej = elev[j]
                if ej - lim > n_lo:
                    n_lo = ej - lim
                if ej + lim < n_hi:
                    n_hi = ej + lim
            lo_e = max(n_lo, floor.get(i, -_INF))
            hi_e = min(n_hi, ceil.get(i, _INF))
            if lo_e > hi_e and spine is not None:
                # the 1-D spine chain is paramount: where the DEM-reach envelope
                # conflicts with the centerline within-grade, the envelope YIELDS
                # (the apron/building frontage takes the step, not the spine).
                lo_e, hi_e = n_lo, n_hi
            if lo_e <= hi_e:
                tgt = min(max(tgt, lo_e), hi_e)
            else:
                tgt = 0.5 * (lo_e + hi_e)            # locally over-constrained
            d = omega * (tgt - elev[i])
            if d:
                elev[i] = elev[i] + d
                if abs(d) > moved:
                    moved = abs(d)
        if moved < tol:
            break
    # Equalise each rect flat-end group ONCE at convergence (the cap=0 cross
    # edges hold them near-equal during iteration; this cleans the sub-mm
    # residual so the rect emits as an exact tilted plane).  A spine node in the
    # group is AUTHORITATIVE — the rect corner conforms to the spine (averaging
    # would pull the spine off its solved profile → a spine grade break); a group
    # with several spine nodes uses their mean (already ≤cap along the chain).
    for members in groups:
        sp = [m for m in members if m in spine_nodes]
        src = sp if sp else members
        mv = sum(elev[m] for m in src) / len(src)
        for m in members:
            elev[m] = mv

    if _os.environ.get("O4_STEP_DEBUG") == "1":
        _report_residual(elev, adj, nodes, free_set, building_seats,
                         runway_nodes, base_hard, floor, ceil, n,
                         _it + 1, moved)
        # SPINE residual (internal, pre-writeback): how many centerline-
        # consecutive pairs are still over cap in the solved field.
        sworst: list = []
        seen_s: set = set()
        for i, lst in spine_adj.items():
            for (j, w) in lst:
                e = (min(i, j), max(i, j))
                if e in seen_s:
                    continue
                seen_s.add(e)
                ex = abs(elev[i] - elev[j]) - w
                if ex > 1e-3:
                    sworst.append((ex, i, j, elev[i], elev[j], w))
        sworst.sort(reverse=True)
        big = [s for s in sworst if s[0] > 0.05]      # ignore convergence noise
        print(f"  [one-profile] internal spine residual: {len(big)} pair(s) "
              f">0.05m ({len(sworst)} total)")
        for (ex, i, j, ei, ej, w) in big[:6]:
            print(f"    [spine-resid] {i}@({nodes[i][0]:.0f},{nodes[i][1]:.0f})"
                  f"={ei:.2f} {j}@({nodes[j][0]:.0f},{nodes[j][1]:.0f})={ej:.2f}"
                  f" budget={w:.2f} ex={ex:.2f}m")
    return len(free)


def _report_residual(elev, adj, nodes, free_set, building_seats, runway_nodes,
                     base_hard, floor, ceil, n, sweeps, moved):
    """O4_STEP_DEBUG diagnostics: residual over-cap edges + envelope coverage."""
    def _typ(k):
        if k in building_seats:
            return "bldg"
        if k >= n or k in runway_nodes:
            return "rwy"
        if base_hard[k]:
            return "seam"
        return "free"
    seen: set = set()
    bh = hf = 0
    worst: list = []
    for i in adj:
        for (j, lim) in adj[i]:
            e = (min(i, j), max(i, j))
            if e in seen:
                continue
            seen.add(e)
            ex = abs(elev[i] - elev[j]) - lim
            if ex > 1e-3:
                if (i not in free_set) and (j not in free_set):
                    bh += 1
                else:
                    hf += 1
                d = math.hypot(nodes[i][0] - nodes[j][0],
                               nodes[i][1] - nodes[j][1])
                worst.append((ex, _typ(i), _typ(j), d, elev[i], elev[j], lim))
    worst.sort(reverse=True)
    n_inv = sum(1 for k in free_set
                if floor.get(k, -_INF) > ceil.get(k, _INF))
    print(f"  [one-profile] {len(free_set)} free node(s), {sweeps} sweep(s), "
          f"anchors={len(base_hard) and sum(base_hard)}+bldg{len(building_seats)} "
          f"moved={moved:.4f}; band-inverted={n_inv}; "
          f"residual edges both-hard={bh} has-free={hf}")
    from collections import Counter
    c = Counter((t1, t2) for (_e, t1, t2, *_r) in worst)
    print(f"    [resid by type] {dict(c)}")
    for (ex, t1, t2, d, ei, ej, lim) in worst[:12]:
        print(f"    [resid] {t1}/{t2} ex={ex:.2f}m d={d:.1f}m "
              f"lev {ei:.1f}/{ej:.1f} lim={lim:.2f}")
