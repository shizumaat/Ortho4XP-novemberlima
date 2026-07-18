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

# Floor for the emit-quantization margin (see ``_margined_budget``): a budget
# at or below this is NEVER reduced.  Rect flat-cross edges are budget 0 BY
# DESIGN (the two corners stay equal) and must stay 0; and margining a
# coincident / sub-0.1 m chord's already-tiny budget toward 0 would make the
# projection try to FLATTEN genuinely-distinct close vertices, fighting the
# hard anchors.
_QUANT_MARGIN_FLOOR_M = 0.005


def _emit_quantization_margin():
    """The solver-side emit-quantization margin (metres) — lazy config read
    (one_solve keeps config imports call-time, matching the module style)."""
    try:
        from auto_patch.config import EMIT_QUANTIZATION_MARGIN_M
        return EMIT_QUANTIZATION_MARGIN_M
    except Exception:
        return 0.0


def _margined_budget(lim, margin):
    """SWEEP budget for a raw edge budget ``lim``: ``lim − margin``, floored.

    ``to_osm`` rounds each emitted elevation to the 0.01 m grid, so a pair's
    emitted |Δelev| can exceed the solved one by up to one full grid step —
    a pair solved exactly AT its budget then reads over the law in the
    emitted file (config.EMIT_QUANTIZATION_MARGIN_M has the full story).
    Enforcing ``lim − margin`` in the sweeps keeps the ROUNDED values inside
    the raw law.  Budgets at or below ``_QUANT_MARGIN_FLOOR_M`` pass through
    unchanged (0-budget flat-cross edges stay 0), and no budget is ever
    reduced below that floor.  ``margin ≤ 0`` (O4_QUANT_MARGIN=0) returns
    ``lim`` → byte-identical pre-margin behaviour."""
    if margin <= 0.0 or lim <= _QUANT_MARGIN_FLOOR_M:
        return lim
    reduced = lim - margin
    return reduced if reduced > _QUANT_MARGIN_FLOOR_M else _QUANT_MARGIN_FLOOR_M


def _margined_interval(interval_low, interval_high, margin):
    """SWEEP bounds for a SIGNED INTERVAL edge (Stage B0, docs/slice_b_solver_
    absorption_design.md): ``interval_low ≤ (z_i − z_j) ≤ interval_high`` with
    either side ``None`` (that side unbounded — a ``None`` ceiling permits any
    rise, a ``None`` floor any drop; the adjacent-ground envelope law's own
    semantics).

    Emit-quantization margin rule (generalises ``_margined_budget``): shrink
    each FINITE side INWARD by ``margin`` so the 0.01 m-rounded emitted
    difference still fits the raw interval — the ceiling moves DOWN
    (``high − margin``), the floor moves UP (``low + margin``), regardless of
    the side's sign (a positive floor 2.0 is still narrowed inward to 2.1).
    ``None`` sides are left untouched (an unbounded side cannot round out of
    the law).  ``_QUANT_MARGIN_FLOOR_M`` floor semantics carry over from the
    symmetric case:
      * a finite side whose magnitude is at or below the floor is left alone
        (mirrors ``_margined_budget`` passing a ≤-floor budget through — a
        near-zero bound stays enforceable rather than being narrowed away);
      * a two-sided interval is never narrowed tighter than the floor WIDTH
        (``2·_QUANT_MARGIN_FLOOR_M``): if the two shrinks would meet or invert,
        the interval collapses to ``[midpoint ∓ _QUANT_MARGIN_FLOOR_M]``
        (mirrors ``_margined_budget`` never reducing a symmetric half-width
        below the floor).
    ``margin ≤ 0`` returns the bounds unchanged.  The symmetric slab
    ``(−budget, +budget)`` fed through this yields ``(−(budget−margin),
    +(budget−margin))`` — identical to ``_margined_budget`` on both sides — but
    symmetric edges NEVER pass through here (they keep the untouched
    ``_margined_budget`` fast path), so symmetric behaviour is byte-identical by
    construction."""
    if margin <= 0.0:
        return interval_low, interval_high
    # A two-sided interval already at or under the floor WIDTH is left entirely
    # unchanged (mirrors ``_margined_budget`` passing a ≤-floor budget through):
    # a genuinely tight slab stays enforceable rather than being narrowed away
    # or widened.
    if (interval_low is not None and interval_high is not None
            and interval_high - interval_low <= 2.0 * _QUANT_MARGIN_FLOOR_M):
        return interval_low, interval_high
    new_high = interval_high
    if interval_high is not None and abs(interval_high) > _QUANT_MARGIN_FLOOR_M:
        new_high = interval_high - margin
    new_low = interval_low
    if interval_low is not None and abs(interval_low) > _QUANT_MARGIN_FLOOR_M:
        new_low = interval_low + margin
    # If the two inward shrinks meet or invert, collapse to the floor width
    # about the midpoint (mirrors ``_margined_budget`` never reducing a
    # half-width below ``_QUANT_MARGIN_FLOOR_M``).
    if (new_low is not None and new_high is not None
            and new_high - new_low < 2.0 * _QUANT_MARGIN_FLOOR_M):
        midpoint = 0.5 * (interval_low + interval_high)
        new_low = midpoint - _QUANT_MARGIN_FLOOR_M
        new_high = midpoint + _QUANT_MARGIN_FLOOR_M
    return new_low, new_high


def _build_adjacency(shape_constraints, n):
    """``adj[i] = [(j, budget), ...]`` where ``budget = cap·length`` (the max
    |Δelev| the edge may carry).  ``budget`` may be 0 (a rect flat-cross edge →
    the two corners stay equal).  Unregulated edges (``None``/negative) are
    skipped — they impose no cap and so do not bound the envelope.

    INTERVAL EDGES (Stage B0): a 4-tuple ``(i, j, interval_low, interval_high)``
    carries a SIGNED interval instead of a symmetric budget.  This adjacency
    feeds the neighbour-cap-slab heuristics in ``one_profile_solve`` /
    ``_project_triangle_planes`` / the final-projection edge fairing, all of
    which model an edge as a SYMMETRIC slab ``|z_i − z_j| ≤ budget`` and cannot
    represent an asymmetric or one-sided interval.  A conservative symmetric
    SURROGATE is used here: when both sides are finite, ``budget =
    max(|interval_low|, |interval_high|)`` (the loosest symmetric slab that
    still contains the interval — it never wrongly forbids a lawful level); a
    one-sided interval (either side ``None``) imposes no symmetric bound and is
    skipped, exactly like an unregulated edge.  The AUTHORITATIVE interval
    enforcement is the projection sweep in ``feasibility_project``, not this
    heuristic adjacency.  With every terrain gate off no interval edge is ever
    produced, so this branch is never taken and the adjacency is byte-identical
    to today."""
    adj: dict = {}
    for edge in shape_constraints_edges(shape_constraints):
        if len(edge) >= 4:
            i, j, interval_low, interval_high = (edge[0], edge[1],
                                                 edge[2], edge[3])
            if (interval_low is None or interval_high is None
                    or i >= n or j >= n or i == j):
                continue
            lim = max(abs(interval_low), abs(interval_high))
            if lim < 0:
                continue
            adj.setdefault(i, []).append((j, lim))
            adj.setdefault(j, []).append((i, lim))
            continue
        i, j, lim = edge
        if lim is None or lim < 0 or i >= n or j >= n or i == j:
            continue
        adj.setdefault(i, []).append((j, lim))
        adj.setdefault(j, []).append((i, lim))
    return adj


def shape_constraints_edges(shape_constraints):
    """Flatten every ``sc["edges"]`` list into one iterator.  A trivial helper
    that names the ``shape_constraints`` edge-tuple contract in one place:
    each edge is either a SYMMETRIC 3-tuple ``(i, j, budget)`` (``|z_i − z_j| ≤
    budget``; ``budget`` ``None``/negative = unregulated) or an INTERVAL
    4-tuple ``(i, j, interval_low, interval_high)`` (Stage B0)."""
    for sc in shape_constraints:
        for edge in sc["edges"]:
            yield edge


def _project_vectorized(elev, iter_edges, n, max_iters, tol,
                        interval_bounds_by_index=None):
    """Vectorised DEGREE-NORMALISED JACOBI variant of the feasibility projection
    (gated by ``_FP_VECTORIZE``).  Mutates ``elev`` (a list) in place.

    Every iteration updates ALL nodes from the same snapshot (Jacobi, not
    Gauss-Seidel), so it vectorises with numpy — but a node touched by many
    over-cap edges would OVERSHOOT if the corrections were summed, so each node's
    correction is AVERAGED over its active edges (``acc / cnt``).  A hard
    endpoint's weight (``wi``/``wj``) is 0 on every edge it touches, so hard nodes
    never move — same invariant as the scalar path.  Converges to a DIFFERENT
    (still ≤cap) feasible surface than Gauss-Seidel, hence not byte-identical.

    INTERVAL EDGES (Stage B0): entries with the ``None`` budget sentinel carry a
    signed slab ``[s_low, s_high]`` in ``interval_bounds_by_index`` (keyed by
    their position in ``iter_edges``).  They are partitioned out into a parallel
    numpy block whose SIGNED-excess corrections scatter into the SAME per-node
    ``acc``/``cnt`` accumulators in the same iteration, so symmetric and
    interval edges relax simultaneously under one degree-normalised step.  With
    no interval edges the symmetric arrays equal today's and the interval block
    is skipped — behaviour is unchanged (the Jacobi path is not byte-identical
    to the scalar path by design regardless)."""
    import numpy as np
    bounds = interval_bounds_by_index or {}
    sym_edges = []
    int_edges = []                    # (i, j, s_low, s_high, kind)
    for edge_index, e in enumerate(iter_edges):
        if e[2] is None:
            s_low, s_high = bounds.get(edge_index, (None, None))
            int_edges.append((e[0], e[1], s_low, s_high, e[3]))
        else:
            sym_edges.append(e)
    m = len(sym_edges)
    I = np.fromiter((e[0] for e in sym_edges), dtype=np.intp, count=m)
    J = np.fromiter((e[1] for e in sym_edges), dtype=np.intp, count=m)
    B = np.fromiter((e[2] for e in sym_edges), dtype=np.float64, count=m)
    K = np.fromiter((e[3] for e in sym_edges), dtype=np.int8, count=m)
    wi = np.where(K == 0, 0.5, np.where(K == 2, 1.0, 0.0))   # i's share of the fix
    wj = np.where(K == 0, 0.5, np.where(K == 1, 1.0, 0.0))   # j's share
    have_int = bool(int_edges)
    if have_int:
        mi = len(int_edges)
        _POS_INF = float("inf")
        _NEG_INF = float("-inf")
        Ii = np.fromiter((e[0] for e in int_edges), dtype=np.intp, count=mi)
        Ji = np.fromiter((e[1] for e in int_edges), dtype=np.intp, count=mi)
        # ``None`` floor → −inf (never violated below); ``None`` ceiling →
        # +inf (never violated above).
        Lo = np.fromiter(((_NEG_INF if e[2] is None else e[2])
                          for e in int_edges), dtype=np.float64, count=mi)
        Hi = np.fromiter(((_POS_INF if e[3] is None else e[3])
                          for e in int_edges), dtype=np.float64, count=mi)
        Ki = np.fromiter((e[4] for e in int_edges), dtype=np.int8, count=mi)
        wi_int = np.where(Ki == 0, 0.5, np.where(Ki == 2, 1.0, 0.0))
        wj_int = np.where(Ki == 0, 0.5, np.where(Ki == 1, 1.0, 0.0))
    z = np.asarray(elev, dtype=np.float64)
    for _it in range(max_iters):
        if m:
            d = z[I] - z[J]
            over = np.abs(d) - B
            active = over > tol
            any_active = bool(active.any())
            # signed excess per ACTIVE edge (0 elsewhere) — scatter-add to
            # endpoints via bincount (true C scatter, far faster than
            # np.add.at).
            se = np.where(active, np.sign(d) * over, 0.0)
            acc = (np.bincount(I, weights=-se * wi, minlength=n)
                   + np.bincount(J, weights=se * wj, minlength=n))
            af = active.astype(np.float64)
            cnt = (np.bincount(I, weights=af, minlength=n)
                   + np.bincount(J, weights=af, minlength=n))
        else:
            # ALL-INTERVAL edge set (every edge carries the ``None`` budget
            # sentinel — EGWN's scoped projection): np.bincount's empty-input
            # fast path returns int64 even when float weights are passed, so
            # deriving ``acc``/``cnt`` from the empty symmetric arrays would
            # birth int64 accumulators and the interval block's ``+=`` below
            # raises a same-kind casting error.  Born float64 instead.
            any_active = False
            acc = np.zeros(n, dtype=np.float64)
            cnt = np.zeros(n, dtype=np.float64)
        if have_int:
            di = z[Ii] - z[Ji]
            above = di - Hi                       # >tol ⇒ over the ceiling
            below = Lo - di                       # >tol ⇒ under the floor
            active_hi = above > tol
            active_lo = below > tol
            active_int = active_hi | active_lo
            any_active = any_active or bool(active_int.any())
            # signed excess: +（di−Hi) when above, −（Lo−di）=di−Lo when below.
            se_int = np.where(active_hi, above,
                              np.where(active_lo, di - Lo, 0.0))
            acc += (np.bincount(Ii, weights=-se_int * wi_int, minlength=n)
                    + np.bincount(Ji, weights=se_int * wj_int, minlength=n))
            afi = active_int.astype(np.float64)
            cnt += (np.bincount(Ii, weights=afi, minlength=n)
                    + np.bincount(Ji, weights=afi, minlength=n))
        if not any_active:
            break
        nz = cnt > 0.0
        z[nz] += acc[nz] / cnt[nz]                          # degree-normalised step
    elev[:] = z.tolist()


# ── Chromatic (graph-colored) Gauss-Seidel projection (Tier 3 wave 2c) ───────
# Routing-survey candidate 1 (docs/research/routing_optimization_survey.md).
# ``_CHROMATIC`` / ``_CHAIN_PREPASS`` are read lazily at call time (one_solve
# keeps config imports call-time, matching the module style) so the gate can be
# flipped per-process and the tests can toggle it without a reload.
def _chromatic_enabled():
    try:
        from auto_patch.config import CHROMATIC_PROJECTION
        return CHROMATIC_PROJECTION
    except Exception:
        return False


def _chain_prepass_enabled():
    try:
        from auto_patch.config import CHROMATIC_CHAIN_PREPASS
        return CHROMATIC_CHAIN_PREPASS
    except Exception:
        return False


def _extend_edge_coloring_by_write(iter_edges: list,
                                   coloring_state: dict) -> tuple:
    """Extend the greedy first-fit WRITE-conflict edge coloring held in
    ``coloring_state`` to cover every edge of ``iter_edges`` (perf 2026-07-18,
    partition-identical to the original per-edge ``forbidden`` union scan).

    ``coloring_state`` (mutated in place; pass ``{}`` to color from scratch)
    carries the coloring of a PREFIX of ``iter_edges``:

    * ``"edge_color"`` — per-edge color list (length = colored prefix);
    * ``"color_count"`` — number of colors used so far;
    * ``"used_colors_by_node"`` — node → set of colors of edges writing it;
    * ``"next_free_color_by_node"`` — node → smallest color index NOT in the
      node's used set (advanced lazily).

    Greedy first-fit is PREFIX-STABLE — an edge's color depends only on
    earlier edges in list order — so extending a carried prefix over appended
    edges (the lazy-expansion rounds, which only ever APPEND) yields exactly
    the coloring a from-scratch run over the full list would.

    HUB ACCELERATION (exact, not approximate): the first-fit color is
    ``c* = min{c ≥ 0 : c ∉ used[a] and c ∉ used[b]}`` over the edge's write
    nodes.  Every ``c < next_free[a]`` lies in ``used[a]`` (and likewise for
    ``b``), so ``c* ≥ max(next_free[a], next_free[b])``; scanning upward from
    that bound and testing membership in BOTH per-node sets directly reaches
    the same ``c*`` as the original scan-from-0 over the union — without
    materialising the O(write-degree) ``forbidden`` union copy per edge that
    made near-clique apron bodies quadratic (the 20.5 s OTHH leaf).  A
    single-write-node edge (``kind`` 1/2) takes ``next_free[w]`` outright —
    the zone→host hub still colors with a single color.

    Returns ``(edge_color, color_count)`` with ``edge_color`` the state's
    (now full-length) per-edge color list."""
    edge_color = coloring_state.setdefault("edge_color", [])
    used = coloring_state.setdefault("used_colors_by_node", {})
    next_free = coloring_state.setdefault("next_free_color_by_node", {})
    color_count = coloring_state.get("color_count", 0)
    colored_prefix = len(edge_color)
    if colored_prefix > len(iter_edges):
        # Defensive: the carried state outran the edge list (caller misuse) —
        # a stale prefix cannot be trusted, so recolor from scratch.
        edge_color.clear()
        used.clear()
        next_free.clear()
        color_count = 0
        colored_prefix = 0
    for edge_index in range(colored_prefix, len(iter_edges)):
        i, j, _budget, kind = iter_edges[edge_index]
        if kind == 0:
            used_i = used.get(i)
            used_j = used.get(j)
            if used_i is None:
                color = 0 if used_j is None else next_free[j]
            elif used_j is None:
                color = next_free[i]
            else:
                free_i = next_free[i]
                free_j = next_free[j]
                color = free_i if free_i > free_j else free_j
                while color in used_i or color in used_j:
                    color += 1
            write_nodes = (i, j)
        else:
            write_node = j if kind == 1 else i
            color = next_free.get(write_node, 0)
            write_nodes = (write_node,)
        edge_color.append(color)
        if color + 1 > color_count:
            color_count = color + 1
        for node in write_nodes:
            s = used.get(node)
            if s is None:
                used[node] = {color}
                next_free[node] = 1 if color == 0 else 0
            else:
                s.add(color)
                free = next_free[node]
                if color == free:
                    free += 1
                    while free in s:
                        free += 1
                    next_free[node] = free
    coloring_state["color_count"] = color_count
    return edge_color, color_count


def _color_edges_by_write(iter_edges):
    """Greedily partition ``iter_edges`` into color classes on the
    WRITE-conflict graph, deterministically.

    An edge WRITES the endpoint(s) the sweep moves — both for ``kind == 0``
    (split the excess), only ``j`` for ``kind == 1`` (``i`` fixed), only ``i``
    for ``kind == 2`` (``j`` fixed).  Two edges conflict iff they write a common
    node.  Within a color no two edges write the same node (a matching in the
    write-conflict graph), so their per-endpoint corrections land on DISJOINT
    entries and can be applied as one vectorized fancy-indexed update — a true
    Gauss-Seidel step, not the stalling degree-normalised Jacobi.

    Immovable endpoints are never written, so they impose no conflict: a hub of
    ``k`` one-directional (``kind`` 1/2) edges — the zone→host cluster — colors
    with a SINGLE color (all write distinct zone endpoints), the survey's
    high-degree-hub mitigation.

    Determinism: edges are processed in ``iter_edges`` order and each takes the
    smallest color not used by an already-colored write-neighbour; the result is
    invariant to intra-color order by construction (disjoint writes).  The
    greedy scan itself lives in :func:`_extend_edge_coloring_by_write` (exact
    hub-accelerated first-fit, perf 2026-07-18).  Returns a list (indexed by
    color) of edge-index lists."""
    edge_color, color_count = _extend_edge_coloring_by_write(iter_edges, {})
    colors: list = [[] for _ in range(color_count)]
    for edge_index in range(len(iter_edges)):
        colors[edge_color[edge_index]].append(edge_index)
    return colors


def _chain_envelope_clamp(elev, chain_nodes, chain_budgets, v_left, v_right):
    """Two-pass Lipschitz RUNNING clamp of ONE chain (survey candidate 2).

    ``chain_nodes`` are the free interior nodes ``c_1 … c_k`` in order;
    ``chain_budgets`` are the ``k + 1`` consecutive edge budgets
    ``b(L,c_1), b(c_1,c_2), …, b(c_k,R)``; ``v_left`` / ``v_right`` are the
    (held) boundary values.  A forward sweep clamps each node into
    ``[v_{i-1} − b, v_{i-1} + b]`` of its already-clamped LEFT neighbour, then a
    backward sweep clamps each into ``[v_{i+1} − b, v_{i+1} + b]`` of its
    already-clamped RIGHT neighbour (the running-clamp / repeated-median form).
    When the chain is feasible (``|v_left − v_right| ≤ Σb``) the two O(k) passes
    land a profile satisfying every consecutive edge with NO iteration — exactly
    the residual class POCS otherwise chains excess through node-by-node.  An
    infeasible chain (the boundaries contradict) is left near-feasible for the
    colored sweep / broken quarantine to own the genuine step.  Mutates
    ``elev`` in place; the boundaries themselves are never written."""
    k = len(chain_nodes)
    if k == 0:
        return
    # FORWARD: clamp against the running left neighbour (starts at v_left).
    prev = v_left
    for t in range(k):
        b = chain_budgets[t]                # budget (prev -> c_t)
        lo = prev - b
        hi = prev + b
        node = chain_nodes[t]
        v = elev[node]
        if v < lo:
            v = lo
        elif v > hi:
            v = hi
        elev[node] = v
        prev = v
    # BACKWARD: clamp against the running right neighbour (starts at v_right).
    prev = v_right
    for t in range(k - 1, -1, -1):
        b = chain_budgets[t + 1]            # budget (c_t -> next-right)
        lo = prev - b
        hi = prev + b
        node = chain_nodes[t]
        v = elev[node]
        if v < lo:
            v = lo
        elif v > hi:
            v = hi
        elev[node] = v
        prev = v


def _project_chain_prepass(elev, iter_edges, n, immovable):
    """Closed-form warm start (survey candidate 2): detect 1-D chain
    substructures in the regulated SYMMETRIC graph — maximal paths whose
    interior nodes are FREE with degree exactly 2 and which touch no interval
    edge — and solve each exactly with :func:`_chain_envelope_clamp`, bounded by
    the current values of its two endpoints.  Interval-touched nodes and free
    nodes of degree ≠ 2 (branch/leaf) bound a chain but are never moved here.

    A warm start only: the colored sweep runs afterward and re-checks every
    edge, so a chain that a later-considered chord would make cyclic can only
    cost sweeps, never correctness.  Returns the number of chains solved.

    Mutates ``elev`` in place; touches only free interior chain nodes."""
    m = len(iter_edges)
    # symmetric-only adjacency + interval-touched marks, tightest budget per pair
    adj: list = [None] * n
    interval_touched = bytearray(n)
    for edge_index in range(m):
        i, j, budget, _kind = iter_edges[edge_index]
        if budget is None:                              # interval edge
            interval_touched[i] = 1
            interval_touched[j] = 1
            continue
        li = adj[i]
        if li is None:
            adj[i] = {j: budget}
        else:
            prev = li.get(j)
            if prev is None or budget < prev:
                li[j] = budget
        lj = adj[j]
        if lj is None:
            adj[j] = {i: budget}
        else:
            prev = lj.get(i)
            if prev is None or budget < prev:
                lj[i] = budget

    def _is_interior(node):
        # a free, non-interval node with exactly two distinct regulated
        # symmetric neighbours (a genuine 1-D chain link).
        if node in immovable or interval_touched[node]:
            return False
        nb = adj[node]
        return nb is not None and len(nb) == 2

    visited = bytearray(n)
    n_chains = 0
    for start in range(n):
        if visited[start] or not _is_interior(start):
            continue
        # walk to one boundary, collecting the chain.  ``walk_seen`` guards
        # against a pure degree-2 RING (no boundary) — without it the walk of
        # an all-interior cycle would never terminate (``visited`` is only
        # stamped after assembly).
        nbrs = list(adj[start].keys())
        walk_seen = {start}
        # walk left from start via nbrs[0]
        left_nodes = []                     # interior nodes to the left (rev)
        prev = start
        cur = nbrs[0]
        while _is_interior(cur) and not visited[cur] and cur not in walk_seen:
            left_nodes.append(cur)
            walk_seen.add(cur)
            nxt = [x for x in adj[cur].keys() if x != prev]
            if len(nxt) != 1:
                break
            prev, cur = cur, nxt[0]
        left_boundary = cur                 # first non-interior (or visited)
        # walk right from start via nbrs[1]
        right_nodes = []
        prev = start
        cur = nbrs[1]
        while _is_interior(cur) and not visited[cur] and cur not in walk_seen:
            right_nodes.append(cur)
            walk_seen.add(cur)
            nxt = [x for x in adj[cur].keys() if x != prev]
            if len(nxt) != 1:
                break
            prev, cur = cur, nxt[0]
        right_boundary = cur
        # assemble ordered interior: left_boundary, rev(left_nodes), start,
        # right_nodes, right_boundary
        interior = list(reversed(left_nodes)) + [start] + right_nodes
        for node in interior:
            visited[node] = 1
        if (left_boundary == right_boundary
                or left_boundary in walk_seen or right_boundary in walk_seen):
            continue                        # degenerate ring — leave to the sweep
        # consecutive budgets along [left_boundary, interior..., right_boundary]
        seq = [left_boundary] + interior + [right_boundary]
        budgets = []
        ok = True
        for a, b in zip(seq[:-1], seq[1:]):
            na = adj[a]
            if na is None or b not in na:
                ok = False
                break
            budgets.append(na[b])
        if not ok or len(budgets) != len(interior) + 1:
            continue
        _chain_envelope_clamp(elev, interior, budgets,
                              elev[left_boundary], elev[right_boundary])
        n_chains += 1
    return n_chains


def _project_chromatic(elev, iter_edges, n, max_iters, tol,
                       interval_bounds_by_index=None, *, stats=None,
                       coloring_state=None, run_feasibility_precheck=True):
    """Colored Gauss-Seidel POCS (survey candidate 1) — the vectorized
    replacement for BOTH legacy inner sweeps.  Mutates ``elev`` in place.

    Precomputes one deterministic write-conflict coloring
    (:func:`_extend_edge_coloring_by_write`); each sweep relaxes the color
    classes in order, every class as a single vectorized fancy-indexed update
    whose writes are disjoint (a valid independent projection batch) and which
    reads the LATEST values from earlier classes — a true Gauss-Seidel step at
    numpy speed, converging where the degree-normalised Jacobi stalls.

    KKT / dual feasibility certificate (survey candidate 4): a full sweep that
    applies NO correction proves every constraint satisfied, so iteration stops
    on proof rather than the ``max_iters`` cap; the avoided sweeps are recorded
    in ``stats``.  ``stats`` (optional dict) receives ``colors``, ``edges``,
    ``sweeps``, ``sweeps_avoided``, ``certified`` and ``worst``.

    FEASIBILITY PRE-CHECK (perf 2026-07-18, byte-identical): one vectorized
    residual pass over the flat edge columns BEFORE the coloring.  Sweep 1
    applies zero corrections iff every edge is already within tolerance at
    entry (the first color class only moves when active, so an all-satisfied
    system passes through every class untouched) — so when no edge violates,
    this returns the exact certified-on-sweep-1 result (``(1, True)``, the
    same counters and writeback) without paying the coloring or the per-color
    array build at all.  The repeated re-projection call sites are usually
    already feasible, which is where the saving lands.  ``stats["colors"]`` is
    unknowable without coloring and reported as ``None`` on this path (it is
    only ever read by the ``O4_STEP_DEBUG`` print, under which the coloring IS
    computed so the line stays faithful).  ``run_feasibility_precheck=False``
    forces the full path (test hook; both paths are value-identical).

    ``coloring_state`` (optional dict, mutated) carries the incremental
    coloring across lazy-expansion rounds — see
    :func:`_extend_edge_coloring_by_write`; rounds only APPEND edges and the
    greedy coloring is prefix-stable, so extending is exact.

    Interval edges (``budget is None``) carry a signed slab
    ``[s_low, s_high]`` in ``interval_bounds_by_index`` (keyed by ``iter_edges``
    position); they relax in the same color classes as the symmetric edges."""
    import numpy as np
    bounds = interval_bounds_by_index or {}
    _NEG_INF = float("-inf")
    _POS_INF = float("inf")
    edge_count = len(iter_edges)
    # ONE flat pass over ``iter_edges`` (perf 2026-07-18, byte-identical):
    # the endpoint / weight / budget / slab columns feed BOTH the feasibility
    # pre-check and the per-color array build below (stable argsort slicing
    # replaces the former per-color Python append loop; the weight formulas
    # and the None→∓inf slab mapping are verbatim the old per-edge ones).
    flat_endpoint_i = [0] * edge_count
    flat_endpoint_j = [0] * edge_count
    flat_weight_i = [0.0] * edge_count
    flat_weight_j = [0.0] * edge_count
    flat_budget = [0.0] * edge_count        # dummy 0.0 at interval slots
    flat_slab_low = [_NEG_INF] * edge_count
    flat_slab_high = [_POS_INF] * edge_count
    flat_is_interval = [False] * edge_count
    for edge_index in range(edge_count):
        ei, ej, budget, kind = iter_edges[edge_index]
        flat_endpoint_i[edge_index] = ei
        flat_endpoint_j[edge_index] = ej
        flat_weight_i[edge_index] = \
            0.5 if kind == 0 else (1.0 if kind == 2 else 0.0)
        flat_weight_j[edge_index] = \
            0.5 if kind == 0 else (1.0 if kind == 1 else 0.0)
        if budget is None:
            flat_is_interval[edge_index] = True
            s_low, s_high = bounds.get(edge_index, (None, None))
            if s_low is not None:
                flat_slab_low[edge_index] = s_low
            if s_high is not None:
                flat_slab_high[edge_index] = s_high
        else:
            flat_budget[edge_index] = budget
    endpoint_i = np.asarray(flat_endpoint_i, dtype=np.intp)
    endpoint_j = np.asarray(flat_endpoint_j, dtype=np.intp)
    weight_i = np.asarray(flat_weight_i, dtype=np.float64)
    weight_j = np.asarray(flat_weight_j, dtype=np.float64)
    budget_column = np.asarray(flat_budget, dtype=np.float64)
    slab_low_column = np.asarray(flat_slab_low, dtype=np.float64)
    slab_high_column = np.asarray(flat_slab_high, dtype=np.float64)
    interval_mask = np.asarray(flat_is_interval, dtype=bool)
    z = np.asarray(elev, dtype=np.float64)
    # ── feasibility pre-check (see docstring): certified without coloring ──
    if run_feasibility_precheck and max_iters >= 1:
        feasible = True
        symmetric_rows = np.flatnonzero(~interval_mask)
        if symmetric_rows.size:
            d = z[endpoint_i[symmetric_rows]] - z[endpoint_j[symmetric_rows]]
            over = np.abs(d) - budget_column[symmetric_rows]
            feasible = not bool((over > tol).any())
        if feasible:
            interval_rows = np.flatnonzero(interval_mask)
            if interval_rows.size:
                di = (z[endpoint_i[interval_rows]]
                      - z[endpoint_j[interval_rows]])
                above = di - slab_high_column[interval_rows]
                below = slab_low_column[interval_rows] - di
                feasible = not bool(((above > tol) | (below > tol)).any())
        if feasible:
            # Mirror the certified-on-sweep-1 exit exactly: same writeback,
            # same counters, same return value (worst resets to 0.0 at sweep
            # start and no edge was active, so it stays 0.0).
            elev[:] = z.tolist()
            if stats is not None:
                color_count_report = None
                if _os.environ.get("O4_STEP_DEBUG") == "1":
                    _, color_count_report = _extend_edge_coloring_by_write(
                        iter_edges,
                        coloring_state if coloring_state is not None else {})
                stats["colors"] = color_count_report
                stats["edges"] = edge_count
                stats["sweeps"] = 1
                stats["sweeps_avoided"] = max(0, max_iters - 1)
                stats["certified"] = True
                stats["worst"] = 0.0
            return 1, True
    edge_color, color_count = _extend_edge_coloring_by_write(
        iter_edges, coloring_state if coloring_state is not None else {})
    # per-color numpy arrays, symmetric + interval partitioned, sliced from
    # the flat columns via ONE stable argsort: within a color the members come
    # out in ascending edge order — exactly the order the old per-color append
    # loop produced (and irrelevant to the math anyway: disjoint writes).
    edge_color_array = np.asarray(edge_color, dtype=np.int64)
    stable_order = np.argsort(edge_color_array, kind="stable")
    group_bounds = np.searchsorted(edge_color_array[stable_order],
                                   np.arange(color_count + 1))
    sym: list = []
    intv: list = []
    for color in range(color_count):
        members = stable_order[group_bounds[color]:group_bounds[color + 1]]
        member_is_interval = interval_mask[members]
        symmetric_members = members[~member_is_interval]
        interval_members = members[member_is_interval]
        sym.append((
            endpoint_i[symmetric_members], endpoint_j[symmetric_members],
            budget_column[symmetric_members],
            weight_i[symmetric_members], weight_j[symmetric_members]))
        intv.append((
            endpoint_i[interval_members], endpoint_j[interval_members],
            slab_low_column[interval_members],
            slab_high_column[interval_members],
            weight_i[interval_members], weight_j[interval_members]))
    sweeps = 0
    certified = False
    worst = 0.0
    for _sweep in range(max_iters):
        sweeps += 1
        any_active = False
        worst = 0.0
        for color_index in range(color_count):
            I, J, B, WI, WJ = sym[color_index]
            if I.size:
                d = z[I] - z[J]
                ad = np.abs(d)
                over = ad - B
                active = over > tol
                if active.any():
                    any_active = True
                    ex = np.where(active, over, 0.0)
                    w = ex.max()
                    if w > worst:
                        worst = float(w)
                    s = np.sign(d)
                    # disjoint writes within a color -> fancy-indexed add is a
                    # valid simultaneous update (immovable slots carry weight 0).
                    z[I] += -s * ex * WI
                    z[J] += s * ex * WJ
            Ii, Ji, Lo, Hi, IWI, IWJ = intv[color_index]
            if Ii.size:
                di = z[Ii] - z[Ji]
                above = di - Hi
                below = Lo - di
                active_hi = above > tol
                active_lo = below > tol
                act = active_hi | active_lo
                if act.any():
                    any_active = True
                    se = np.where(active_hi, above,
                                  np.where(active_lo, di - Lo, 0.0))
                    aw = np.abs(se).max()
                    if aw > worst:
                        worst = float(aw)
                    z[Ii] += -se * IWI
                    z[Ji] += se * IWJ
        if not any_active:
            certified = True
            break
    elev[:] = z.tolist()
    if stats is not None:
        stats["colors"] = color_count
        stats["edges"] = len(iter_edges)
        stats["sweeps"] = sweeps
        stats["sweeps_avoided"] = max(0, max_iters - sweeps) if certified else 0
        stats["certified"] = certified
        stats["worst"] = worst
    return sweeps, certified


def feasibility_project(elev, shape_constraints, hard, *,
                        max_iters=4000, tol=1e-3, force_scalar=False,
                        flat_groups=None, broken_out=None, pre_broken=None,
                        edge_couple_nodes=None, interval_yield_from=None):
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

    EMIT-QUANTIZATION MARGIN: the sweeps (and the reach envelope) enforce
    ``budget − config.EMIT_QUANTIZATION_MARGIN_M`` (floored, see
    ``_margined_budget``) so the 0.01 m-rounded emitted elevations still satisfy
    the raw law; the returned over-cap tally is measured against the RAW budget
    (the true law — reporting is never tightened).

    ``flat_groups`` — optional list of node-index sets, each a RIGID FLAT group
    (a building pad): its members share ONE elevation that the projection may
    move as a unit (the feasibility-audit model — buildings are movable flat
    groups; holding every pad hard at its pre-picked seat makes the polytope
    infeasible through chained paths even when no single edge is both-hard).
    Each group collapses to a representative node; member↔member edges vanish,
    member↔outside edges re-anchor to the representative with their own budget,
    and the representative's final level is broadcast back to all members.  A
    group containing a ``hard`` node stays entirely hard (never moved).

    ``broken_out`` — optional set the caller passes to receive the BROKEN
    node indices this call quarantined (genuine anchor contradictions,
    blended + immovable — see below).  ``pre_broken`` — node indices to
    quarantine AT THEIR CURRENT VALUES in addition to the envelope's own
    detections (scoped final projection, user 2026-07-05: the solve's FULL
    graph proved these nodes sit in infeasible pockets; the scoped graph's
    sparser envelope can miss the contradiction and the worklist then grinds
    POCS on the infeasible subsystem to the visit cap, smearing the pocket —
    the exact failure the broken quarantine exists to prevent).  Quarantining
    extra nodes is always law-safe: their over-cap pairs are reported, never
    hidden.
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

    # ── FLATNESS-CERTIFIED LAZY SHAPES (user 2026-07-05 flatness tier) ───
    # A certified entry carries only its O(n) ring-adjacent pairs eagerly;
    # its O(n²) body pairs are proven satisfied AT THE DEM SEED
    # (``solver_primitives._certify_flat_shape``).  Soundness invariant: the
    # moment any of the shape's nodes moves off that seed — BEFORE this call
    # (checked right here) or DURING it (checked in the scalar worklist /
    # after each vectorised pass) — the full pair set is generated and
    # enforced exactly like an eager shape's.  Expansion mutates the entry in
    # place (full edges merged, lazy keys dropped), so every LATER projection
    # call sees the expanded set too.  Final state either way: every
    # generated edge satisfied + every never-expanded shape satisfied by its
    # certificate ⇒ the same law coverage as eager generation.
    def _lazy_nodes_moved(entry):
        # A flat-group member's live value is carried by its REPRESENTATIVE
        # during this call (members are only broadcast back at the end), so
        # the effective elevation to compare against the certificate seed is
        # ``elev[_r(node_index)]``.
        # The movement tolerance is the certificate's SLACK-AWARE bound
        # (user 2026-07-05 tuning): certified pairs sit at ≤ 0.6·rate·d,
        # so both endpoints may drift 0.2·rate·d_min before any body pair
        # can reach its budget — mm-scale smoothing nudges no longer
        # expand every certificate.  Entries from older callers without
        # the field keep the strict 1e-6.
        movement_tolerance = entry.get("lazy_move_tolerance", 1e-6)
        for node_index, seed_value in zip(entry["lazy_nodes"],
                                          entry["lazy_seed"]):
            if 0 <= node_index < n and \
                    abs(elev[_r(node_index)] - seed_value) \
                    > movement_tolerance:
                return True
        return False

    def _expand_lazy_entry(entry):
        """Generate the entry's FULL pair set and merge it in; the lazy keys
        are dropped, so the entry is an ordinary eager entry from now on
        (``lazy_certified`` stays as the hit-rate marker).  A thunk failure
        PROPAGATES: a lazy bookkeeping error must never silently skip
        constraints, and the thunk is the same generation call the eager
        path would have made — it cannot be worked around, only heard."""
        thunk = entry.pop("lazy_expand")
        entry.pop("lazy_nodes", None)
        entry.pop("lazy_seed", None)
        entry.pop("lazy_move_tolerance", None)
        full_edges = list(thunk())
        # Ring pairs come back again inside the full set — the
        # min-budget-wins dedup below absorbs the duplicates.
        entry["edges"] = list(entry["edges"]) + full_edges
        return full_edges

    lazy_entries_pending = []
    for sc in shape_constraints:
        if sc.get("lazy_expand") is None:
            continue
        if _lazy_nodes_moved(sc):
            # Pre-call movement (an earlier pass moved a node off its seed):
            # expand NOW, before edge_lim is built, so the full set flows
            # through the ordinary min-budget-wins + margin pipeline.
            _expand_lazy_entry(sc)
        else:
            lazy_entries_pending.append(sc)

    # TIGHTEST budget wins across duplicate (remapped) pairs.  Every raw edge
    # is a constraint that must hold, so when several land on the same index
    # pair the binding one is the minimum.  This matters most under
    # ``flat_groups``: the group collapse aliases MANY physical chords (every
    # pad-ring vertex ↔ one apron node, budgets spanning 10-25×) onto ONE
    # representative pair — first-edge-wins enforced an arbitrary (usually
    # loose) budget while the validator checks each physical chord at its own
    # allowance (SPJC round 4: 138 of the 153 residual law-true violations
    # were exactly this; min-wins takes SPJC to 0).
    # SIGNED INTERVAL EDGES (Stage B0, docs/slice_b_solver_absorption_
    # design.md): an edge is either the SYMMETRIC 3-tuple ``(i, j, budget)``
    # (``|z_i − z_j| ≤ budget``) — the existing fast path, its arithmetic left
    # literally untouched below — or the INTERVAL 4-tuple ``(i, j,
    # interval_low, interval_high)`` (``interval_low ≤ z_i − z_j ≤
    # interval_high``, either side ``None`` = unbounded).  Interval edges are
    # collected into ``interval_lim`` and enforced by a SEPARATE projection
    # path; symmetric edges flow through ``edge_lim`` exactly as before.  With
    # every terrain gate off no interval edge is produced, so ``interval_lim``
    # stays empty and the whole interval apparatus below is inert — the
    # symmetric solve is byte-identical to today.
    edge_lim: dict = {}
    interval_lim: dict = {}          # canonical pair (a<b) -> (low, high)
    for sc in shape_constraints:
        for edge in sc["edges"]:
            if len(edge) >= 4:
                # INTERVAL EDGE — signed slab on ``z_i − z_j``.
                i, j, raw_low, raw_high = (edge[0], edge[1],
                                           edge[2], edge[3])
                if raw_low is None and raw_high is None:
                    continue         # unregulated (both sides open)
                if i >= n or j >= n:
                    continue
                i, j = _r(i), _r(j)
                if i == j:
                    continue
                if i < j:
                    pair, low, high = (i, j), raw_low, raw_high
                else:
                    # flipping the pair negates the difference: for (j, i),
                    # z_j − z_i ∈ [−raw_high, −raw_low] (a ``None`` bound maps
                    # to the opposite open side).
                    pair = (j, i)
                    low = None if raw_high is None else -raw_high
                    high = None if raw_low is None else -raw_low
                previous = interval_lim.get(pair)
                if previous is None:
                    interval_lim[pair] = (low, high)
                else:
                    # TIGHTEST wins per side (min-budget-wins analogue for a
                    # signed slab): intersect the intervals — the larger floor
                    # and the smaller ceiling, treating ``None`` as ∓∞.
                    prev_low, prev_high = previous
                    new_low = (low if prev_low is None
                               else low if (low is not None and low > prev_low)
                               else prev_low)
                    new_high = (high if prev_high is None
                                else high if (high is not None
                                              and high < prev_high)
                                else prev_high)
                    interval_lim[pair] = (new_low, new_high)
                continue
            # SYMMETRIC EDGE — existing tightest-budget-wins, untouched.
            i, j, lim = edge
            if lim is None or lim < 0 or i >= n or j >= n:
                continue
            i, j = _r(i), _r(j)
            if i == j:
                continue
            e = (i, j) if i < j else (j, i)
            prev = edge_lim.get(e)
            if prev is None or lim < prev:
                edge_lim[e] = lim
    if not edge_lim and not interval_lim:
        return 0, 0
    # EMIT-QUANTIZATION MARGIN: the SWEEP (and the reach envelope + break
    # detection, so the enforced system stays self-consistent) runs on
    # ``budget − margin`` — the rounded emit still fits the raw law — while
    # the final TALLY below keeps the RAW budget: violations are reported
    # against the true law, and a both-hard pair (never movable) can not be
    # tipped into a phantom violation by the margin.  ``edges`` carries both:
    # ``(i, j, raw_budget, sweep_budget)``.
    quant_margin = _emit_quantization_margin()
    edges = []
    adj: dict = {}
    # DIRECTED reach-envelope adjacencies (Stage B3, interval-aware envelope).
    # ``ceil_radj[k]`` / ``floor_radj[k]`` = ``[(j, w), ...]`` where the reach
    # relaxation is ``t_j := t_k + w`` (the sign is BAKED INTO the weight, so
    # both envelopes share one relaxation form and interval edges — whose
    # ceiling-forward and floor-forward weights are INDEPENDENT — embed
    # directly).  For a SYMMETRIC budget the ceiling weight is ``+lim`` both
    # ways and the floor weight ``−lim`` both ways, so with NO interval edge
    # present these reproduce the old single-``adj`` ``t + sign·lim`` /
    # ``dk + lim`` arithmetic BIT-FOR-BIT (``−1·lim`` and ``−lim`` are the
    # identical IEEE negation; ``|−lim| = lim``) — the gates-off byte-identity
    # gate.  ``adj`` (symmetric) is kept unchanged for ``_hard_neighbour_
    # interval`` below, which models symmetric welded neighbours only.
    ceil_radj: dict = {}
    floor_radj: dict = {}
    for (i, j), lim in edge_lim.items():
        sweep_lim = _margined_budget(lim, quant_margin)
        edges.append((i, j, lim, sweep_lim))
        adj.setdefault(i, []).append((j, sweep_lim))
        adj.setdefault(j, []).append((i, sweep_lim))
        ceil_radj.setdefault(i, []).append((j, sweep_lim))
        ceil_radj.setdefault(j, []).append((i, sweep_lim))
        floor_radj.setdefault(i, []).append((j, -sweep_lim))
        floor_radj.setdefault(j, []).append((i, -sweep_lim))
    # INTERVAL EDGES (Stage B0): each carries the RAW interval (for the final
    # tally, measured against the true law) and the SWEEP interval (raw shrunk
    # inward by the emit-quantization margin — see ``_margined_interval``).
    # ``interval_edges`` items: ``(i, j, raw_low, raw_high, sweep_low,
    # sweep_high)`` with ``i < j`` and the slab on ``z_i − z_j``.
    #
    # DIRECTED ENVELOPE PROPAGATION (Stage B3): a signed slab
    # ``low ≤ z_i − z_j ≤ high`` is the directed generalisation of the
    # symmetric budget and contributes to the envelope exactly as its two
    # implied inequalities do (``None`` side ⇒ that direction imposes no
    # bound, so the edge is skipped there):
    #   ceiling:  z_i ≤ z_j + high  ⇒  ceil_i ≤ ceil_j + high   (j→i, +high)
    #             z_j ≤ z_i − low   ⇒  ceil_j ≤ ceil_i − low    (i→j, −low)
    #   floor:    z_i ≥ z_j + low   ⇒  floor_i ≥ floor_j + low   (j→i, +low)
    #             z_j ≥ z_i − high  ⇒  floor_j ≥ floor_i − high  (i→j, −high)
    # The symmetric case ``low=−lim, high=+lim`` yields exactly the ±lim
    # both-way weights the loop above added, so this apparatus is byte-inert
    # with no interval edges (the loop below does not execute).  Interval-only
    # free nodes now GET the one-shot envelope warm-start AND, when their two
    # (or more) parent slabs cannot be jointly satisfied by the hard-anchor-
    # reachable station elevations, are caught by the ``floor > ceil`` break
    # detection and QUARANTINED — the exact livelock the POCS sweep otherwise
    # ping-pongs on (Stage B2 measured 27.7 M interval moves; the two-parent
    # gap-spine disjoint-slab class — see docs/slice_b_solver_absorption_
    # design.md and the ``_build_gap_spine_constraints`` empty-intersection
    # note, whose SEED-time prune misses slabs that only go disjoint as
    # stations move).
    interval_edges = []
    for (i, j), (raw_low, raw_high) in interval_lim.items():
        sweep_low, sweep_high = _margined_interval(raw_low, raw_high,
                                                   quant_margin)
        interval_edges.append((i, j, raw_low, raw_high,
                               sweep_low, sweep_high))
        # ENVELOPE EXCLUSION FOR ZONE EDGES (Slice B stage B3 solve-side
        # fix, gated by ``interval_yield_from``): a signed slab injects
        # SIGNED (often NEGATIVE) directed weights into the reach-envelope
        # adjacency, but ``_reach`` is a Dijkstra — non-negative weights
        # only.  A zone node is an envelope LEAF (its host is authoritative;
        # it never propagates a bound onward), so its 45k asymmetric slabs
        # add nothing the envelope needs while triggering cascading
        # re-expansion (KBNA gates-ON: the ``_reach`` setup alone runs for
        # tens of minutes; gap-spine's ~1.6k drained fine, but 45k zone
        # slabs blow it up — the lazy-Dijkstra re-expand class).  Skip the
        # zone<->host slabs here; the host-authoritative sweep (kind fix
        # below) positions the zone directly against its solved host, and
        # gap-spine / sub-threshold slabs keep their envelope contribution.
        _zone_slab = (interval_yield_from is not None
                      and ((i >= interval_yield_from)
                           != (j >= interval_yield_from)))
        if _zone_slab:
            continue
        if sweep_high is not None:
            ceil_radj.setdefault(j, []).append((i, sweep_high))
            floor_radj.setdefault(i, []).append((j, -sweep_high))
        if sweep_low is not None:
            ceil_radj.setdefault(i, []).append((j, -sweep_low))
            floor_radj.setdefault(j, []).append((i, sweep_low))

    # EXACT reachability envelope: ceil_i = min over hard anchors a of
    # (z_a + capdist(a→i)), floor_i = max of (z_a − capdist).  ``budget`` is the
    # edge's cap·length, so this is the steepest-compliant reach of every anchor.
    # Both envelopes are cap-Lipschitz, so clamping into [floor, ceil] removes all
    # gross (anchor-driven) infeasibility in ONE shot — the iterative pass then
    # only resolves free↔free edges, which converges fast.
    # LAZY SHAPES: the envelope + break detection run ONCE, here, on the
    # initial adjacency.  Still-lazy shapes contribute their RING edges to it,
    # so anchor-contradiction paths THROUGH a certified flat zone still exist
    # — at slightly looser ring-path budgets than the direct body chords would
    # give (the residual; fixtures gate it).  Mid-call expansions do NOT
    # recompute the envelope.
    INF = float("inf")

    def _reach(sign, radj):                 # sign +1 → ceil, −1 → floor
        # ``radj`` provides directed weights with the sign already baked in
        # (``ceil_radj`` for +1, ``floor_radj`` for −1); the relaxation is
        # ``nt = t + w`` and the budget-metric distance accumulates ``|w|``.
        best: dict = {}
        dist: dict = {}                     # budget-metric distance to the
        pq = [((elev[a] if sign > 0 else -elev[a]), 0.0, a)
              for a in hard if a < n]       # value-optimal anchor
        heapq.heapify(pq)
        while pq:
            val, dk, k = heapq.heappop(pq)
            t = val if sign > 0 else -val
            if k in best and ((sign > 0 and t >= best[k])
                              or (sign < 0 and t <= best[k])):
                continue
            best[k] = t
            dist[k] = dk
            for (j, w) in radj.get(k, ()):
                nt = t + w
                pj = best.get(j)
                if pj is None or (sign > 0 and nt < pj) or (sign < 0 and nt > pj):
                    heapq.heappush(pq, ((nt if sign > 0 else -nt),
                                        dk + (w if w >= 0.0 else -w), j))
        return best, dist

    from auto_patch.config import SVC_SPINE_EDGE_COUPLE as _EDGE_COUPLE

    def _hard_neighbour_interval(i):
        """The elevation interval node ``i`` may take while still obeying the
        within-shape grade cap to every one of its HARD welded neighbours:
        ``∩ over hard h of [z_h − budget_ih, z_h + budget_ih]``.  Returns
        ``(lo, hi)``; ``lo > hi`` means the hard neighbours themselves
        contradict (a genuine break the blend must own)."""
        nlo, nhi = -INF, INF
        for (h, lim) in adj.get(i, ()):
            if h in hard:
                if elev[h] - lim > nlo:
                    nlo = elev[h] - lim
                if elev[h] + lim < nhi:
                    nhi = elev[h] + lim
        return nlo, nhi

    broken: set = set()
    if hard:
        ceil, ceil_dist = _reach(+1, ceil_radj)
        floor, floor_dist = _reach(-1, floor_radj)
        for i in range(n):
            if i in hard:
                continue
            lo = floor.get(i, -INF)
            hi = ceil.get(i, INF)
            if lo > hi:
                # GENUINE break: the hard anchors contradict through this
                # node (e.g. a tile-seam terrain pin below a plateau the
                # runway anchors hold up).  Distance-weighted blend
                # (user 2026-07-04, "the seam is a hard anchor the solver
                # GRADES to"): ``t = d_ceil/(d_ceil + d_floor)`` puts the
                # surface ON the pin-descent field at the pins (t→0 ⇒
                # z=hi: smooth, seamless transition), ON the floor field
                # at the high anchors (t→1 ⇒ z=lo), and spreads the
                # deficit between them as a gentle over-cap ramp
                # (cap + deficit/path — e.g. 2.2 % instead of a 1.9 m
                # wall).  Continuous at the break-region boundary for any
                # ``t`` (lo = hi there).  A plain midpoint instead parks
                # HALF the deficit as a wall at the pin interface — the
                # exact bump the user reported at the SPLP band edge.
                dc = ceil_dist.get(i, 0.0)
                df = floor_dist.get(i, 0.0)
                t = dc / (dc + df) if (dc + df) > 1e-9 else 0.5
                elev[i] = hi + (lo - hi) * t
                # BROKEN-NODE EDGE COUPLING (config.SVC_SPINE_EDGE_COUPLE,
                # round-6 site-4): the global reach envelope may call a node
                # broken while its OWN hard welded neighbours still admit a
                # feasible level — the CYXY service_road #201 spine, draped
                # ~2.4 m below its 709.5 m edge welds by this blend.  Clamp the
                # blend into the interval those hard neighbours allow whenever
                # it is non-empty (the within-shape law: no spine below its
                # welded edges); an EMPTY interval is the genuine anchor
                # contradiction the blend is for (seam pin below a plateau) and
                # is left untouched — so the seam/plateau blends never regress.
                # SCOPED to ``edge_couple_nodes`` (the service-road / junction
                # ring nodes the final projection passes): the ravine class is
                # a road spine draped under its DEM-following adjacent-ground
                # welds; other broken nodes (apron/junction/seam blends) keep
                # the untouched blend so the pass stays a no-op for them.
                if (_EDGE_COUPLE and edge_couple_nodes is not None
                        and i in edge_couple_nodes):
                    nlo, nhi = _hard_neighbour_interval(i)
                    if nlo <= nhi:
                        elev[i] = min(max(elev[i], nlo), nhi)
                broken.add(i)
            else:
                elev[i] = min(max(elev[i], lo), hi)  # clamp into the envelope

    # BROKEN nodes stay AT their blended value and never move in the
    # sweeps.  Both reach envelopes are cap-Lipschitz along the edge graph
    # and the blend is continuous, so the break region is a smooth,
    # contained ramp.  Sweeping their edges instead cycles POCS on an
    # infeasible system and smears the break as ±1 m NOISE across the
    # whole region (SPLP seam approach: 10-14 % wiggles between ring
    # neighbours).  Their over-cap edges are reported in the final tally
    # like both-hard edges — infeasibility is never hidden.
    if pre_broken:
        # Caller-supplied quarantine (scoped final projection): the solve's
        # FULL graph proved these nodes sit in infeasible pockets.  Merged
        # AFTER the envelope pass: the envelope may first clamp them into
        # its own (sparser) [floor, ceil] exactly as the full rebuild's
        # envelope re-blends its broken set — measured at CYXY this
        # ordering reproduces the full rebuild's surface exactly, while
        # quarantining before the clamp diverges (the final projection's
        # anchor set differs from the solve's, so holding the solve's
        # blend values is NOT what the full rebuild does).
        for _pb in pre_broken:
            if 0 <= _pb < n and _pb not in hard:
                broken.add(_pb)
    if broken_out is not None:
        broken_out.update(broken)
    immovable = hard | broken if broken else hard

    # Pre-split the edges ONCE by hard-membership.  The inner loop otherwise ran
    # two ``in hard`` set lookups PER edge PER iteration (up to ~0.5 B lookups on
    # a big airport).  Both-immovable edges can never move, so drop them from the
    # iteration entirely (they are only counted in the final tally below).
    # ``kind``: 0 = both free (split the excess), 1 = i fixed (move j), 2 = j fixed.
    # The iteration enforces the MARGINED (sweep) budget — see above.
    iter_edges = []
    for (i, j, _raw_budget, sweep_budget) in edges:
        hi = i in immovable
        hj = j in immovable
        if hi and hj:
            continue
        iter_edges.append((i, j, sweep_budget, 1 if hi else (2 if hj else 0)))
    # INTERVAL EDGES (Stage B0) share the SAME worklist/Jacobi machinery as the
    # symmetric edges so a node moved by one re-triggers the other in lockstep.
    # They enter ``iter_edges`` with the SENTINEL ``budget=None`` in slot 2 and
    # their signed sweep bounds in ``interval_bounds_by_index`` (keyed by the
    # entry's ``iter_edges`` position).  Being real ``iter_edges`` entries, they
    # get stable indices and ride the existing ``incident``/``in_pending``/
    # ``pending`` bookkeeping and the mid-call lazy-append growth for free; the
    # scalar inner loop and the vectorised path branch on the ``None`` sentinel.
    # With gates off there are none, so slot 2 is never ``None`` and both sweep
    # paths run their untouched symmetric arithmetic.  ``kind`` as above (0 both
    # free, 1 i fixed, 2 j fixed); both-immovable pairs are tallied, never swept.
    interval_bounds_by_index: dict = {}
    for (i, j, _rl, _rh, sweep_low, sweep_high) in interval_edges:
        hi = i in immovable
        hj = j in immovable
        if hi and hj:
            continue
        kind = 1 if hi else (2 if hj else 0)
        # HOST-AUTHORITATIVE ZONE EDGES (Slice B stage B3 solve-side fix,
        # gated by ``interval_yield_from`` from the call site): an
        # adjacent-ground zone node (canonical index >= the threshold) is
        # graded TO its host pavement ring vertex — pavement value always
        # wins at a pavement node, an IDENTITY.  Its ONE envelope interval
        # edge to the host must therefore move ONLY the zone endpoint; the
        # default kind=0 (split the excess) instead drags the host, and a
        # host shared by k zones ping-pongs the whole cluster to the visit
        # cap (KBNA gates-ON: 47k such edges livelock the sweep for tens of
        # minutes; gates-OFF the same projection drains <1 s).  Fixing the
        # host (kind that moves the ZONE endpoint) makes it a one-directional
        # follow — the zone tracks the host, never the reverse — so the sweep
        # drains in one neighbourhood pass.  Only zone<->host pairs (exactly
        # one endpoint over the threshold, neither already immovable) are
        # redirected; zone<->zone or sub-threshold (gap-spine) interval edges
        # keep the split.  ``interval_yield_from=None`` restores kind=0 —
        # byte-inert.
        if interval_yield_from is not None and kind == 0:
            i_zone = i >= interval_yield_from
            j_zone = j >= interval_yield_from
            if i_zone and not j_zone:
                kind = 2                 # host j fixed, move zone i
            elif j_zone and not i_zone:
                kind = 1                 # host i fixed, move zone j
        interval_bounds_by_index[len(iter_edges)] = (sweep_low, sweep_high)
        iter_edges.append((i, j, None, kind))

    # ── lazy expansion plumbing for the projection loops ─────────────────
    # node → still-lazy entries, REPRESENTATIVE-keyed (a pad-group member's
    # movement shows on its representative).  Determinism: entries appear in
    # ``shape_constraints`` order and are expanded in that order; each
    # expansion appends edges in thunk order — no set is iterated anywhere.
    lazy_entries_by_node: dict = {}
    for lazy_entry in lazy_entries_pending:
        for node_index in lazy_entry["lazy_nodes"]:
            if 0 <= node_index < n:
                lazy_entries_by_node.setdefault(
                    _r(node_index), []).append(lazy_entry)

    def _expand_lazy_entry_into_projection(entry):
        """MID-CALL expansion: merge the entry's full pair set into the live
        projection state (``edge_lim`` / ``edges`` / ``iter_edges``).
        Returns the new ``iter_edges`` indices so the scalar worklist can
        wire them into ``incident``/``pending`` (the vectorised path instead
        re-runs on the grown ``iter_edges``).  A pair already present at a
        tighter-or-equal budget is skipped; a TIGHTER duplicate is appended
        alongside the looser one — both are enforced, the tightest binds.
        Both-immovable pairs are tallied (``edges``) but never swept, exactly
        like the entry-time path.  The reach envelope is NOT recomputed (see
        the envelope comment above)."""
        new_edge_indices = []
        for (raw_a, raw_b, raw_budget) in _expand_lazy_entry(entry):
            if raw_budget is None or raw_budget < 0 \
                    or raw_a >= n or raw_b >= n:
                continue
            node_a, node_b = _r(raw_a), _r(raw_b)
            if node_a == node_b:
                continue
            pair = (node_a, node_b) if node_a < node_b else (node_b, node_a)
            previous_budget = edge_lim.get(pair)
            if previous_budget is not None and previous_budget <= raw_budget:
                continue
            edge_lim[pair] = raw_budget
            sweep_budget = _margined_budget(raw_budget, quant_margin)
            edges.append((pair[0], pair[1], raw_budget, sweep_budget))
            a_immovable = pair[0] in immovable
            b_immovable = pair[1] in immovable
            if a_immovable and b_immovable:
                continue
            iter_edges.append((pair[0], pair[1], sweep_budget,
                               1 if a_immovable else (2 if b_immovable else 0)))
            new_edge_indices.append(len(iter_edges) - 1)
        return new_edge_indices

    # POST-ENVELOPE movement check: the reachability clamp (and the broken-
    # node blend) above moves band-infeasible nodes BEFORE any sweep — the
    # worklist only reacts to edge-driven moves and would never see those, so
    # a certified shape whose node was clamped off its seed must expand NOW.
    # New edges land in ``iter_edges`` before either projection path builds
    # its queue, so they are swept from the start.
    if lazy_entries_pending:
        _still_pending_entries = []
        for lazy_entry in lazy_entries_pending:
            if "lazy_expand" not in lazy_entry:
                continue
            if _lazy_nodes_moved(lazy_entry):
                _expand_lazy_entry_into_projection(lazy_entry)
            else:
                _still_pending_entries.append(lazy_entry)
        lazy_entries_pending = _still_pending_entries

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
    # CHROMATIC (graph-colored) Gauss-Seidel (Tier 3 wave 2c, survey candidate
    # 1): a numpy-vectorized TRUE Gauss-Seidel sweep that converges where the
    # Jacobi stalls, so it replaces BOTH legacy inner paths — the
    # ``force_scalar`` final projection (the 2400-sweep worklist) AND the
    # mid-solve vectorised Jacobi.  A DIFFERENT legal fixpoint than the FIFO
    # worklist (counts-not-worse, not byte-identical) — gate ``O4_CHROMATIC_
    # PROJECTION`` (default ON); OFF falls straight through to the legacy split
    # below, byte-identically.  The closed-form chain pre-pass (survey candidate
    # 2) warm-starts 1-D substructures exactly before the sweep.
    if _chromatic_enabled() and iter_edges:
        _chain_prepass = _chain_prepass_enabled()
        _chain_count = 0
        if _chain_prepass:
            _chain_count = _project_chain_prepass(elev, iter_edges, n, immovable)
        _chroma_stats: dict = {}
        # Incremental coloring across the lazy rounds (perf 2026-07-18,
        # partition-identical): expansion only APPENDS to ``iter_edges`` and
        # the greedy coloring is prefix-stable, so the carried state colors
        # just the appended suffix each round instead of the whole grown set.
        _coloring_state: dict = {}
        _project_chromatic(elev, iter_edges, n, max_iters, tol,
                           interval_bounds_by_index, stats=_chroma_stats,
                           coloring_state=_coloring_state)
        # Lazy shapes: as for the Jacobi path, only the FINAL state matters for
        # a certificate, so re-warm + re-sweep on the grown edge set until no
        # further shape expands (bounded: each round expands ≥1 entry).
        while lazy_entries_pending:
            still_pending = []
            expanded_any = False
            for lazy_entry in lazy_entries_pending:
                if "lazy_expand" not in lazy_entry:
                    continue
                if _lazy_nodes_moved(lazy_entry):
                    _expand_lazy_entry_into_projection(lazy_entry)
                    expanded_any = True
                else:
                    still_pending.append(lazy_entry)
            lazy_entries_pending = still_pending
            if not expanded_any:
                break
            if _chain_prepass:
                _project_chain_prepass(elev, iter_edges, n, immovable)
            _project_chromatic(elev, iter_edges, n, max_iters, tol,
                               interval_bounds_by_index, stats=_chroma_stats,
                               coloring_state=_coloring_state)
        _sweeps_run = _chroma_stats.get("sweeps", 0)
        _last_worst = _chroma_stats.get("worst", 0.0)
        if _os.environ.get("O4_STEP_DEBUG") == "1":
            print(f"    [fp-chromatic] colors={_chroma_stats.get('colors')} "
                  f"edges={_chroma_stats.get('edges')} "
                  f"sweeps={_sweeps_run} "
                  f"avoided={_chroma_stats.get('sweeps_avoided')} "
                  f"certified={_chroma_stats.get('certified')} "
                  f"chains={_chain_count} worst={_last_worst:.4f}")
    elif _vec and iter_edges:
        _project_vectorized(elev, iter_edges, n, max_iters, tol,
                            interval_bounds_by_index)
        # Lazy shapes under the vectorised Jacobi: only the FINAL state
        # matters for the certificate (a shape whose nodes END at their seed
        # has its body pairs satisfied at that seed, transient wiggles
        # notwithstanding), so movement is checked after each pass and the
        # projection re-run on the grown edge set until no further shape
        # expands — the same fixpoint the scalar worklist reaches
        # edge-by-edge.  Bounded: each round expands ≥1 entry, entries are
        # finite.
        while lazy_entries_pending:
            still_pending = []
            expanded_any = False
            for lazy_entry in lazy_entries_pending:
                if "lazy_expand" not in lazy_entry:
                    continue
                if _lazy_nodes_moved(lazy_entry):
                    _expand_lazy_entry_into_projection(lazy_entry)
                    expanded_any = True
                else:
                    still_pending.append(lazy_entry)
            lazy_entries_pending = still_pending
            if not expanded_any:
                break
            _project_vectorized(elev, iter_edges, n, max_iters, tol,
                                interval_bounds_by_index)
    else:
        # WORKLIST Gauss-Seidel (perf 2026-07-04): the cyclic sweep
        # re-examined EVERY edge up to ``max_iters`` times even when
        # 99 % start satisfied (flat airports: KDFW relief 0.485 % —
        # nearly the whole graph is law-true at the DEM seed).  An
        # edge only needs re-checking after one of its endpoints
        # MOVED, so the queue shrinks to the violation neighbourhoods
        # and the work scales with terrain difficulty, not graph
        # size.  Deterministic: initial order = edge order, FIFO
        # requeue.  The visit cap equals the old worst-case work
        # bound (max_iters full sweeps), so pathological cyclic
        # systems terminate exactly as before.
        # ``incident`` as a per-node list-of-lists indexed by node index
        # (0..n-1) rather than a dict: every edge endpoint is a canonical node
        # < n, so a plain list gives the same per-node adjacency (identical
        # insertion order = edge order) at list-subscript speed instead of a
        # dict hash+get on the hot re-enqueue path.  New lazy-expansion edges
        # reference existing nodes (< n), so their sublists already exist.
        incident: list = [None] * n
        for edge_index in range(len(iter_edges)):
            i = iter_edges[edge_index][0]
            j = iter_edges[edge_index][1]
            li = incident[i]
            if li is None:
                incident[i] = [edge_index]
            else:
                li.append(edge_index)
            lj = incident[j]
            if lj is None:
                incident[j] = [edge_index]
            else:
                lj.append(edge_index)
        from collections import deque
        pending = deque(range(len(iter_edges)))
        in_pending = bytearray(len(iter_edges))
        for edge_index in pending:
            in_pending[edge_index] = 1
        visits = 0
        visit_cap = max_iters * max(1, len(iter_edges))
        # RE-ENTRY DIAGNOSTIC (O4_FP_REENTRY_DEBUG=1, off by default → zero
        # cost and byte-identical): counts pops / no-op pops / moves keyed by
        # edge kind (interval vs symmetric) plus per-edge pop tallies, so the
        # livelock re-entry mechanism can be read off a single gate-ON build
        # (Slice B stage B3 prerequisite trace).  ``force_scalar`` only.
        _reentry_dbg = _os.environ.get("O4_FP_REENTRY_DEBUG") == "1"
        if _reentry_dbg:
            _pops_sym = _pops_int = 0
            _noop_sym = _noop_int = 0
            _edge_pops = [0] * len(iter_edges)
        # HOT-LOOP LOCALS (perf 2026-07-15, byte-identical): bind the bound
        # deque methods to locals and re-index the interval bounds as a
        # parallel list keyed by ``iter_edges`` position (the same key the dict
        # used), so each visit pays a list subscript instead of a deque/dict
        # attribute + hash lookup.  Every interval edge is created before the
        # loop, so a fixed-length list covers every ``budget is None`` index;
        # lazy-expansion edges are symmetric (budget not None) and never index
        # this list.  ``incident`` is now a per-node list (built above), so the
        # re-enqueue reads ``incident[node]`` directly (``None`` = no edges).
        pending_popleft = pending.popleft
        pending_append = pending.append
        # ``interval_bounds_by_index`` is a dict keyed by ``iter_edges``
        # position; re-index it as a parallel list so the hot loop pays a list
        # subscript instead of a dict hash+get on every interval visit.  All
        # interval edges are created before the loop, so a fixed-length list
        # covers every ``budget is None`` index; lazy-expansion edges are
        # symmetric (``budget`` not ``None``) and never index this list.
        # Each slot also carries the tolerance-shifted comparands
        # ``s_low - tol`` / ``s_high + tol`` precomputed ONCE (identical
        # operands ⇒ identical IEEE result ⇒ bit-identical to the inline
        # ``s_high + tol`` / ``s_low - tol``), so the no-op fast-path (the
        # dominant visit class) skips two float additions per interval pop.
        _interval_bounds = [None] * len(iter_edges)
        for _bi, (_sl, _sh) in interval_bounds_by_index.items():
            _interval_bounds[_bi] = (
                _sl, _sh,
                None if _sl is None else _sl - tol,
                None if _sh is None else _sh + tol)
        while pending and visits < visit_cap:
            edge_index = pending_popleft()
            in_pending[edge_index] = 0
            visits += 1
            i, j, budget, kind = iter_edges[edge_index]
            if _reentry_dbg:
                _edge_pops[edge_index] += 1
                if budget is None:
                    _pops_int += 1
                else:
                    _pops_sym += 1
            if budget is None:
                # INTERVAL EDGE (Stage B0): project the difference onto the
                # signed slab ``[s_low, s_high]``.  ``se`` is the SIGNED excess
                # (positive ⇒ z_i − z_j above the ceiling, reduce it; negative
                # ⇒ below the floor, raise it), so the same endpoint-weight
                # split as the symmetric case applies with ``se`` in place of
                # ``s·ex``.  A ``None`` side imposes no bound on that direction.
                s_low, s_high, s_low_tol, s_high_tol = \
                    _interval_bounds[edge_index]
                d = elev[i] - elev[j]
                if s_high is not None and d > s_high_tol:
                    se = d - s_high
                elif s_low is not None and d < s_low_tol:
                    se = d - s_low
                else:
                    if _reentry_dbg:
                        _noop_int += 1
                    continue
                if kind == 0:
                    elev[i] -= se * 0.5
                    elev[j] += se * 0.5
                    moved = (i, j)
                elif kind == 1:
                    elev[j] += se                      # i fixed → move j
                    moved = (j,)
                else:
                    elev[i] -= se                      # j fixed → move i
                    moved = (i,)
                ex = -se if se < 0.0 else se
                if ex > _last_worst:
                    _last_worst = ex
            else:
                d = elev[i] - elev[j]
                ad = -d if d < 0.0 else d              # inline abs() (hot path)
                if ad <= budget + tol:
                    if _reentry_dbg:
                        _noop_sym += 1
                    continue
                ex = ad - budget
                s = 1.0 if d > 0 else -1.0
                if kind == 0:
                    elev[i] -= s * ex * 0.5
                    elev[j] += s * ex * 0.5
                    moved = (i, j)
                elif kind == 1:
                    elev[j] += s * ex                  # i fixed → move j up to i
                    moved = (j,)
                else:
                    elev[i] -= s * ex                  # j fixed → move i
                    moved = (i,)
                if ex > _last_worst:
                    _last_worst = ex
            for moved_node in moved:
                nbrs = incident[moved_node]
                if nbrs is not None:
                    for neighbour_edge in nbrs:
                        if not in_pending[neighbour_edge]:
                            in_pending[neighbour_edge] = 1
                            pending_append(neighbour_edge)
            # MID-CALL lazy expansion: this node just moved — if it belongs
            # to still-certified shapes AND the move exceeds the entry's
            # slack-aware tolerance, the seed premise is gone; generate the
            # full pair set and enqueue the new edges (kind recomputed
            # against the SAME ``immovable`` set).  A move WITHIN the slack
            # keeps the entry lazy and still watched (the mapping is only
            # dropped once expanded), so a later larger drift re-triggers.
            if lazy_entries_by_node:
                for moved_node in moved:
                    entries_here = lazy_entries_by_node.get(moved_node)
                    if not entries_here:
                        continue
                    still_watching = []
                    for lazy_entry in entries_here:
                        if "lazy_expand" not in lazy_entry:
                            continue        # expanded via another node
                        if not _lazy_nodes_moved(lazy_entry):
                            still_watching.append(lazy_entry)
                            continue        # within certificate slack
                        for new_edge_index in \
                                _expand_lazy_entry_into_projection(lazy_entry):
                            edge_a, edge_b, _b2, _k2 = \
                                iter_edges[new_edge_index]
                            _la = incident[edge_a]
                            if _la is None:
                                incident[edge_a] = [new_edge_index]
                            else:
                                _la.append(new_edge_index)
                            _lb = incident[edge_b]
                            if _lb is None:
                                incident[edge_b] = [new_edge_index]
                            else:
                                _lb.append(new_edge_index)
                            in_pending.append(1)
                            pending_append(new_edge_index)
                    if still_watching:
                        lazy_entries_by_node[moved_node] = still_watching
                    else:
                        lazy_entries_by_node.pop(moved_node, None)
        _sweeps_run = visits
        if _reentry_dbg:
            _capped = visits >= visit_cap
            _n_int = len(interval_bounds_by_index)
            print(f"    [fp-reentry] visits={visits} cap={visit_cap} "
                  f"capped={_capped} edges={len(iter_edges)} "
                  f"interval_edges={_n_int}")
            print(f"    [fp-reentry] pops sym={_pops_sym} int={_pops_int} | "
                  f"no-op sym={_noop_sym} int={_noop_int} | "
                  f"moves sym={_pops_sym - _noop_sym} "
                  f"int={_pops_int - _noop_int}")
            # Top re-entered edges (by pop count), split by kind, to name the
            # ping-pong participants.
            _ranked = sorted(range(len(iter_edges)),
                             key=lambda e: _edge_pops[e], reverse=True)
            print("    [fp-reentry] top-20 re-entered edges "
                  "(idx kind i j budget pops):")
            for _e in _ranked[:20]:
                _ei, _ej, _eb, _ek = iter_edges[_e]
                _kind_s = "INT" if _eb is None else "sym"
                _bud_s = (repr(interval_bounds_by_index.get(_e))
                          if _eb is None else repr(round(_eb, 3)))
                print(f"        {_e} {_kind_s} i={_ei} j={_ej} "
                      f"bounds={_bud_s} pops={_edge_pops[_e]}")
            # How many INTERVAL edges were popped more than twice (the
            # re-admission / ping-pong signature).
            _int_reenter = sum(1 for _e in range(len(iter_edges))
                               if iter_edges[_e][2] is None
                               and _edge_pops[_e] > 2)
            _sym_reenter = sum(1 for _e in range(len(iter_edges))
                               if iter_edges[_e][2] is not None
                               and _edge_pops[_e] > 2)
            print(f"    [fp-reentry] edges popped >2x: "
                  f"interval={_int_reenter} symmetric={_sym_reenter}")
    # broadcast each flat group's representative level back to its members.
    for rep, g in (groups_eff if flat_groups else ()):
        for m in g:
            elev[m] = elev[rep]
    # final tally — against the RAW budget (the true law), NOT the margined
    # sweep budget: the margin tightens enforcement only, never reporting.
    rem = bh = 0
    worst_ex = 0.0
    for (i, j, budget, _sweep_budget) in edges:
        ex = abs(elev[i] - elev[j]) - budget
        if ex > tol:
            rem += 1
            worst_ex = max(worst_ex, ex)
            if i in hard and j in hard:
                bh += 1
    # INTERVAL EDGES (Stage B0) tally against their RAW signed bounds (the true
    # law), each ``None`` side never violated.  ``ex`` is the signed slab excess
    # (how far ``z_i − z_j`` sits outside ``[raw_low, raw_high]``).
    for (i, j, raw_low, raw_high, _sl, _sh) in interval_edges:
        d = elev[i] - elev[j]
        if raw_high is not None and d - raw_high > tol:
            ex = d - raw_high
        elif raw_low is not None and raw_low - d > tol:
            ex = raw_low - d
        else:
            continue
        rem += 1
        worst_ex = max(worst_ex, ex)
        if i in hard and j in hard:
            bh += 1
    if _os.environ.get("O4_STEP_DEBUG") == "1" and force_scalar:
        print(f"    [fp-scalar] sweeps={_sweeps_run} last_worst={_last_worst:.4f} "
              f"rem={rem} worst_ex={worst_ex:.3f} groups={len(groups_eff)} "
              f"broken={len(broken)}")
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
