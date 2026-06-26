"""ACCEPTANCE GATE for the single-graph route-profile solver (user 2026-06-26).

For ~10 sessions "one graph" has been *claimed* done while the code kept TWO
graphs (a route graph that SETS elevations, a grade graph that CHECKS them,
bridged by ``geo_key`` with two context builders).  "Done" was judged by
eyeballing spine numbers — which a local bridge/hack can satisfy.

These tests make "done" OBJECTIVE and HACK-RESISTANT.  They must all pass, and
they are written so that a two-graph workaround CANNOT make them green:

  * ``test_solver_validator_same_spine_pairs`` — STRUCTURAL.  The elevation
    solver and the validator must constrain the SAME spine pairs (same nodes,
    same source).  Two graphs / two context builders differ → fails.  You cannot
    fake spine=0 by checking a different (weaker) pair set than you enforce.
  * ``test_validator_detects_spine_step`` — ANTI-GAMING.  A known step injected
    on a spine vertex MUST be flagged, so the validator cannot be quietly
    weakened (looser cap, dropped pairs, inflated noise) to fake spine=0.
  * ``test_cyxy_spine_zero`` — OUTCOME.  Zero spine violations on the strict
    extended validator.  A setter on a different graph than the checker leaves
    residual here.  RED today (18); GREEN only when the graph is truly unified.

RULE for the next session: do NOT add a bridge, a second graph, a ``geo_key``
mapping for emission, or a post-solve patch.  If you are writing any of those,
stop — that is the hack.  Make these tests green by unifying onto ONE node set,
ONE context builder, ONE runway-anchor rule (see STATUS.md / docs/
route_profile_solver_status.md).
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("O4_ROUTE_PROFILE_SOLVE", "1")


def _cyxy():
    from conftest import cached_airport_layout
    return cached_airport_layout("CYXY")


def _rc(xy):
    return (round(float(xy[0]), 2), round(float(xy[1]), 2))


def _validator_spine_pairs(layout):
    """The spine vertex-pairs the validator (within_violations) checks, keyed by
    rounded coordinate so they compare across the solver's index keying."""
    from auto_patch import grade_graph as GG
    from auto_patch.grade_graph_validate import _context, _open_ring
    ctx = _context(layout)
    pairs = set()
    for s in layout.shapes:
        if (s.role not in GG.SOFT_VISIBILITY_ROLES or s.polygon is None
                or s.polygon.is_empty):
            continue
        ring = _open_ring(list(s.polygon.exterior.coords))
        if len(ring) < 3:
            continue
        gs = GG.GradeShape(role=s.role, ring=[(x, y) for (x, y) in ring],
                           keys=list(range(len(ring))))
        sc = GG.shape_constraints(gs, ctx)
        for chain in sc.spine_chains:
            for a, b in zip(chain, chain[1:]):
                pairs.add(frozenset((_rc(ring[a]), _rc(ring[b]))))
    return pairs


def _solver_spine_pairs(layout):
    """The spine vertex-pairs the elevation solver constrains (spine_adjacency)."""
    from auto_patch.elevation_per_surface.unified_jacobi import _build_node_list
    from auto_patch.elevation_per_surface.route_profile.spine import (
        spine_adjacency)
    nodes, b2i = _build_node_list(layout)
    _spine_nodes, spine_adj = spine_adjacency(layout, nodes, b2i)
    pairs = set()
    for i, lst in spine_adj.items():
        for (j, _b) in lst:
            if i < len(nodes) and j < len(nodes):
                pairs.add(frozenset((_rc(nodes[i]), _rc(nodes[j]))))
    return pairs


def test_solver_validator_same_spine_pairs():
    """The solver must enforce EVERY spine pair the validator checks (one graph,
    one source of nodes).  A second graph / context builder leaves pairs the
    validator checks but the solver never constrained."""
    layout = _cyxy()
    val = _validator_spine_pairs(layout)
    solver = _solver_spine_pairs(layout)
    missing = val - solver
    assert not missing, (
        f"{len(missing)} spine pair(s) the VALIDATOR checks are NOT enforced by "
        f"the solver → two graphs / two sources.  e.g. {list(missing)[:3]}")


def test_validator_detects_spine_step():
    """ANTI-GAMING: the validator must flag a deliberately-injected spine step,
    so spine=0 cannot be faked by weakening the checker."""
    import copy
    from auto_patch.grade_graph_validate import within_violations, _open_ring
    from auto_patch.layout import ROLE_JUNCTION
    layout = copy.copy(_cyxy())
    layout.shapes = [copy.copy(s) for s in layout.shapes]
    # find a junction with node_altitudes and bump one vertex by a clear step.
    bumped = False
    for s in layout.shapes:
        if (s.role == ROLE_JUNCTION and s.node_altitudes
                and s.polygon is not None and not s.polygon.is_empty):
            na = list(s.node_altitudes)
            na[0] = float(na[0]) + 3.0       # 3 m step → grossly over any cap
            s.node_altitudes = na
            bumped = True
            break
    assert bumped, "no junction with node_altitudes to perturb"
    v = within_violations(layout)
    assert v, ("validator reported NO violation after a 3 m step was injected — "
               "the checker is too weak; do not relax it to fake spine=0.")


def test_cyxy_spine_zero():
    """OUTCOME: zero spine violations on the strict extended validator (spine +
    rects + caps + runway-joins, width-based, in centerline order).  RED until
    the single graph is genuinely built."""
    from auto_patch.grade_graph_validate import within_violations
    layout = _cyxy()
    v = within_violations(layout)
    spine = [x for x in v if x[4]]
    assert not spine, (
        f"{len(spine)} spine violation(s) — single graph not done.  worst: "
        f"{[(round(p, 1), round(c, 1), round(d, 1), r) for (p, c, d, r, *_ ) in sorted(spine, reverse=True)[:4]]}")
