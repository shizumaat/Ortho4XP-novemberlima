"""Taxi-spine sub-graph for the one-profile solve — from the ONE grade graph.

The spine chains come from ``auto_patch.grade_graph`` (``shape_constraints`` /
``spine_chains``) — the SAME graph the elevation solver grades on (via
``unified_jacobi._build_shape_constraints`` → ``_grade_graph_edges``) and the
SAME graph the validator (``grade_graph_validate.within_violations``) checks.
Building and validating on one graph is the whole point of the single grade graph
(``docs/single_grade_graph.md``): the spine has clear nodes (~12 m, placed by
``junction_spine``), so there is no membership to re-derive and no drift.

Each centerline through an apron/junction yields an ordered chain of on-line node
keys; the keys are the SOLVER node indices (``grade_graph`` is keyed by whatever
the caller passes — here the same ``bucket_to_idx`` the edges use).  A spine node
clamps only to its centerline-CONSECUTIVE neighbours (a 1-D always-feasible
chain) so the apron body yields to the spine.
"""
from __future__ import annotations

import math


def spine_adjacency(layout, nodes, bucket_to_idx):
    """Return ``(spine_nodes, spine_adj)`` from the shared grade graph:

    * ``spine_nodes`` — node indices on a taxi centerline (a grade-graph spine);
    * ``spine_adj`` — ``{i: [(j, budget), ...]}`` over centerline-CONSECUTIVE
      spine nodes, ``budget = cap·dist`` (the same per-letter cap the validator
      grades the pair at).
    """
    from auto_patch import grade_graph as GG
    from auto_patch.layout import ROLE_APRON, ROLE_JUNCTION
    from auto_patch.elevation_per_surface.unified_jacobi import _open_ring

    ctx = GG.build_context(layout, bucket_to_idx)
    cps = layout.canonical_points
    spine_nodes: set = set()
    spine_adj: dict = {}
    _seen_edge: set = set()

    def _add(a, b, w):
        e = (min(a, b), max(a, b))
        if e in _seen_edge:                          # one chain edge per pair
            return
        _seen_edge.add(e)
        spine_adj.setdefault(a, []).append((b, w))
        spine_adj.setdefault(b, []).append((a, w))

    for s in layout.shapes:
        if s.role not in (ROLE_APRON, ROLE_JUNCTION):
            continue
        if s.polygon is None or s.polygon.is_empty:
            continue
        coords = _open_ring(list(s.polygon.exterior.coords))
        idx = [bucket_to_idx.get(cps.get_or_add(float(x), float(y)))
               for (x, y) in coords]
        keys = [i if i is not None else ("_n", p) for p, i in enumerate(idx)]
        gs = GG.GradeShape(role=s.role, ring=list(coords), keys=keys)
        sc = GG.shape_constraints(gs, ctx)
        # the spine cap of each within-shape spine pair (per-letter, the validator
        # caps consecutive chain nodes at exactly this).
        cap_at = {(min(a, b), max(a, b)): cap for (a, b, cap) in sc.edges}
        for chain in sc.spine_chains:
            for a, b in zip(chain, chain[1:]):
                if not isinstance(a, int) or not isinstance(b, int):
                    continue                     # sentinel (vertex w/o index)
                cap = cap_at.get((min(a, b), max(a, b)))
                if cap is None:
                    continue
                d = math.hypot(nodes[a][0] - nodes[b][0],
                               nodes[a][1] - nodes[b][1])
                spine_nodes.add(a)
                spine_nodes.add(b)
                _add(a, b, cap * max(d, 1e-3))
    return spine_nodes, spine_adj
