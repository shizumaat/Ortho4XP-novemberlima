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

import pytest

# Every test here is CYXY (hardcoded, not icao-parametrised), so the conftest's
# auto xdist_group misses them. Pin the whole module to CYXY's group so they
# share the worker that already builds CYXY instead of rebuilding it.
pytestmark = pytest.mark.xdist_group("CYXY")


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


def test_solver_and_validator_same_nodes(tmp_path):
    """STRUCTURAL (rewritten 2026-07-05, user-approved semantics change):
    post-solve vertex inserts (planarize inserts, final T-vertex weld
    adoptions) are ACCEPTED architecture, so exact solver-graph ↔
    emitted-node equality can never pass again — the pass that exists to
    grade the FINAL geometry is ``final_grade_projection``
    (``elevation_per_surface/route_profile/solve.py``), which rebuilds the
    law graph on the final rings via ``_build_node_list`` +
    ``build_unified_graph``.

    The invariant now: every EMITTED airside vertex (what X-Plane renders,
    read back from the OSM patch through the same canonical-point registry
    the projection keys on) must be a node of THAT law graph — i.e. no
    airside pavement vertex escapes the last-word grade projection."""
    import xml.etree.ElementTree as ET
    from auto_patch.elevation_per_surface.solver_primitives import (
        PAVEMENT_ROLES, _build_node_list)

    layout = _cyxy()
    # The law graph's node registry EXACTLY as final_grade_projection builds
    # it on the final (post-planarize / post-T-weld) rings.
    _nodes, b2i = _build_node_list(layout)
    cps = layout.canonical_points

    out = tmp_path / "CYXY_final.osm"
    layout.to_osm(str(out))
    root = ET.parse(str(out)).getroot()
    node_ll = {nd.get("id"): (float(nd.get("lat")), float(nd.get("lon")))
               for nd in root.iter("node")}

    missing = []
    checked = 0
    for way in root.iter("way"):
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        role = tags.get("role")
        if role not in PAVEMENT_ROLES:
            continue
        for nd in way.findall("nd"):
            la, lo = node_ll[nd.get("ref")]
            x, y = layout.ll_to_m(la, lo)
            checked += 1
            k = cps.find_nearest(x, y, cps.tol_m)
            if k is None or k not in b2i:
                missing.append((role, round(x, 1), round(y, 1)))

    assert checked > 1000, (
        f"too few emitted airside vertices checked ({checked}) — "
        f"role tags / parse broke?")
    assert not missing, (
        f"{len(missing)}/{checked} emitted airside vertices are NOT nodes of "
        f"final_grade_projection's law graph — they were emitted without the "
        f"last-word grade projection ever grading them.  First 10: "
        f"{missing[:10]}")


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


def test_solver_validator_same_edge_budgets(monkeypatch):
    """LOCKSTEP (p5): with the anisotropic edge law ON, the SOLVER's unified-graph
    per-edge budget ``cap.at(Δs∥,Δs⊥)`` must equal the VALIDATOR's for every
    SHARED edge — not just the same node set.  Both go through one
    ``grade_graph.shape_constraints`` that bakes the route decomposition once, so a
    drift here would mean the build and the check disagree on the budget."""
    import math
    from auto_patch import grade_graph as GG
    from auto_patch.grade_graph_validate import _iter_checked_pairs
    from auto_patch.elevation_per_surface.solver_primitives import _build_node_list
    monkeypatch.setattr(GG, "ANISO_EDGES", True)

    layout = _cyxy()
    _nodes, b2i = _build_node_list(layout)
    G = GG.build_unified_graph(layout, b2i)

    def _k(x, y):
        return (round(x, 2), round(y, 2))

    # Aggregate by MIN per coordinate pair: a node pair can carry
    # SEVERAL law edges (the shapes' shared ring edge plus a route-arc
    # spine edge that references global nodes and has no ring
    # identity), and last-writer-wins made the comparison depend on
    # iteration order — the BINDING budget is the law both sides must
    # agree on (measured CYXY: ring edge 0.0187 + solver-only arc edge
    # 0.0238 on one coordinate pair read as a phantom mismatch).
    solver = {}
    for (a, b, cap, _is_sp) in G.edges:
        pa, pb = G.pos.get(a), G.pos.get(b)
        if pa is None or pb is None:
            continue
        d = math.hypot(pa[0] - pb[0], pa[1] - pb[1])
        if d < 1e-9:
            continue
        key = tuple(sorted((_k(*pa), _k(*pb))))
        budget = cap.at(d, 0.0)
        if key not in solver or budget < solver[key]:
            solver[key] = budget

    val = {}
    for (_role, _sp, (xa, ya), _za, (xb, yb), _zb, cap) in _iter_checked_pairs(layout):
        d = math.hypot(xa - xb, ya - yb)
        if d < 1e-9:
            continue
        key = tuple(sorted((_k(xa, ya), _k(xb, yb))))
        budget = cap.at(d, 0.0)
        if key not in val or budget < val[key]:
            val[key] = budget

    shared = set(solver) & set(val)
    assert len(shared) > 100, f"too few shared edges ({len(shared)}) to prove lockstep"
    bad = [(k, solver[k], val[k]) for k in shared
           if abs(solver[k] - val[k]) > 1e-6]
    assert not bad, (f"{len(bad)}/{len(shared)} shared edges have mismatched "
                     f"budgets (build≠check), e.g. {bad[:3]}")
