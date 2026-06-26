"""ACCEPTANCE GATE for the single-graph route-profile solver (user 2026-06-26).

For ~10 sessions "one graph" has been *claimed* done while the code kept TWO
graphs (a route graph that SETS elevations, a grade graph that CHECKS them,
bridged by ``geo_key`` with two context builders).  "Done" was judged by
eyeballing spine numbers — which a local bridge/hack can satisfy.

These tests make "done" OBJECTIVE and HACK-RESISTANT.  They must all pass, and
they are written so that a two-graph workaround CANNOT make them green:

  * ``test_cyxy_spine_zero`` — OUTCOME, and the load-bearing guard.  Zero spine
    violations on the strict extended validator.  This is what catches a setter
    on a different graph than the checker: whatever graph SETS the elevations, if
    the surface it produces doesn't satisfy the validator's graph, this is RED.
    RED today (18); GREEN only when the elevations the setter assigns satisfy the
    exact pairs the validator checks (= one graph in effect).
  * ``test_validator_detects_spine_step`` — ANTI-GAMING.  A known step injected
    on a spine vertex MUST be flagged, so the validator cannot be quietly
    weakened (looser cap, dropped pairs, inflated noise) to fake spine=0.

NOTE (2026-06-26): a ``test_solver_validator_same_spine_pairs`` was removed — it
compared ``spine_adjacency`` to the validator, but BOTH are derived from the same
``grade_graph``, so it was trivially green and proved nothing about the route
graph that actually sets the spine elevations.  False assurance is the exact
failure mode this file exists to prevent, so the outcome test is the guard.  A
genuine structural test must compare the ELEVATION-SETTING graph (the route
graph / the emitted z) to the validator — see STATUS.md.

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


def test_solver_and_validator_same_nodes():
    """STRUCTURAL (Step 2): the graph the SOLVER builds and sets elevations on
    (``grade_graph.build_unified_graph`` — geometry node indices) must cover the
    EXACT spine the VALIDATOR checks (``grade_graph_validate.within_violations``,
    via ``checked_spine_geometry``).

    Compared in coordinate space so it is NOT a derivative of one source: the
    solver graph is keyed by node index, the validator by per-shape ring index,
    and they are built by independent code paths — equality proves there is one
    graph in effect (the route-graph/``geo_key`` bridge could never satisfy this,
    because it sets on nodes the validator does not check)."""
    from auto_patch import grade_graph as GG
    from auto_patch.grade_graph_validate import checked_spine_geometry
    from auto_patch.elevation_per_surface.unified_jacobi import _build_node_list

    layout = _cyxy()
    nodes, b2i = _build_node_list(layout)
    G = GG.build_unified_graph(layout, b2i)

    def _k(i):
        x, y = G.pos[i]
        return (round(x, 2), round(y, 2))
    solver_nodes = {_k(i) for i in G.spine_nodes()}
    solver_edges = {tuple(sorted((_k(a), _k(b)))) for (a, b) in G.spine_edge_set()}

    val_nodes, val_edges = checked_spine_geometry(layout)

    assert solver_nodes == val_nodes, (
        f"solver graph and validator check DIFFERENT spine nodes: "
        f"solver-only={len(solver_nodes - val_nodes)}, "
        f"validator-only={len(val_nodes - solver_nodes)}")
    assert solver_edges == val_edges, (
        f"solver graph and validator check DIFFERENT spine edges: "
        f"solver-only={len(solver_edges - val_edges)}, "
        f"validator-only={len(val_edges - solver_edges)}")


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
