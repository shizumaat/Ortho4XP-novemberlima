"""Cuts-parity tests for the T3c mid-edge pair-enumeration prune
(``O4_HOLE_ROUTER_MID_EDGE_PRUNE``).

The v2 conforming-cuts planner blocks collinear mid-edge ring vertices in
every Dijkstra call — they are never sources, waypoints, bridge feet, or
targets — so visibility edges incident to them are provably dead.  The T3c
prune therefore skips those pairs at graph-build time.  These tests pin the
contract:

* the planned cuts are IDENTICAL with the prune on and off (the parity gate);
* pruning removes exactly the excluded-incident edges and nothing else;
* the scalar and vectorized adjacency builders agree under exclusion;
* shared ``extra_nodes`` stay exempt (legal attachment points keep edges);
* v1 planner paths keep the full graph.

Pure synthetic geometry, headless.
"""
import math

import pytest
from shapely.geometry import Polygon
from shapely.prepared import prep

import auto_patch.config as cfg
from auto_patch.pavement import hole_router as hr


def _densify_ring(corners, step):
    """Ring through ``corners`` with collinear vertices every ``step`` m."""
    ring = []
    n = len(corners)
    for k in range(n):
        ax, ay = corners[k]
        bx, by = corners[(k + 1) % n]
        seg_len = math.hypot(bx - ax, by - ay)
        pieces = max(1, int(round(seg_len / step)))
        for t in range(pieces):
            f = t / pieces
            ring.append((ax + f * (bx - ax), ay + f * (by - ay)))
    return ring


def _residue_like() -> Polygon:
    """A rect-residue-style apron: densified exterior (collinear runs on
    every edge) + one plain hole + one densified hole."""
    ext = _densify_ring([(0, 0), (60, 0), (60, 60), (0, 60)], 5.0)
    plain_hole = [(12, 12), (22, 12), (22, 22), (12, 22)]
    dense_hole = _densify_ring([(35, 30), (48, 30), (48, 45), (35, 45)], 3.0)
    return Polygon(ext, [plain_hole, dense_hole])


_CASES = [
    # No collinear vertices at all: prune is a no-op by construction.
    Polygon([(0, 0), (40, 0), (40, 40), (0, 40)],
            [[(5, 5), (12, 5), (12, 12), (5, 12)],
             [(20, 18), (30, 18), (30, 30), (20, 30)]]),
    _residue_like(),
    # Dense exterior only, off-grid holes.
    Polygon(_densify_ring([(0, 0), (50, 0), (50, 50), (0, 50)], 2.5),
            [[(7.3, 8.1), (18.9, 6.4), (17.2, 19.7), (6.1, 20.3)],
             [(28.4, 30.2), (41.1, 29.6), (40.5, 43.8), (27.9, 42.1)]]),
]


def _v2_cuts(poly, prune, **kwargs):
    saved = cfg.HOLE_ROUTER_MID_EDGE_PRUNE
    cfg.HOLE_ROUTER_MID_EDGE_PRUNE = prune
    try:
        return hr.plan_hole_cuts_v2(poly, min_hole_area=50.0, **kwargs)
    finally:
        cfg.HOLE_ROUTER_MID_EDGE_PRUNE = saved


@pytest.mark.parametrize("idx", range(len(_CASES)))
def test_v2_cuts_parity_prune_on_off(idx):
    poly = _CASES[idx]
    cuts_off = _v2_cuts(poly, prune=False)
    cuts_on = _v2_cuts(poly, prune=True)
    assert [c.wkt for c in cuts_on] == [c.wkt for c in cuts_off]
    # The fixtures are all openable: parity of empty-vs-empty proves nothing.
    assert cuts_on


def test_v2_cuts_parity_with_shared_extra_nodes():
    poly = _residue_like()
    # (30, 0) coincides with a collinear exterior vertex — as a shared extra
    # it is EXEMPT from mid-edge classification; (2.5, 20) sits mid-edge on
    # the west ring edge between ring vertices.
    extras = [(30.0, 0.0), (0.0, 22.5)]
    cuts_off = _v2_cuts(poly, prune=False, extra_nodes=extras)
    cuts_on = _v2_cuts(poly, prune=True, extra_nodes=extras)
    assert [c.wkt for c in cuts_on] == [c.wkt for c in cuts_off]
    assert cuts_on


def test_prune_removes_only_excluded_incident_edges():
    poly = _residue_like()
    keys = hr._collinear_mid_edge_keys(poly, set())
    assert keys  # the densified fixture must actually classify mid-edge nodes

    g_full = hr.build_graph(poly)
    g_pruned = hr.build_graph(poly, excluded_pair_keys=keys)
    assert [tuple(n) for n in g_pruned.nodes] == [tuple(n) for n in g_full.nodes]

    excluded = {i for i, (x, y) in enumerate(g_full.nodes)
                if hr._coord_key(x, y) in keys}
    assert excluded
    for i in range(len(g_full.nodes)):
        if i in excluded:
            assert g_pruned.adj[i] == []
        else:
            expected = [e for e in g_full.adj[i] if e[0] not in excluded]
            assert g_pruned.adj[i] == expected


def test_scalar_vectorized_parity_with_exclusion():
    poly = _residue_like()
    keys = hr._collinear_mid_edge_keys(poly, set())
    saved = cfg.VECTORIZED_GEOMETRY
    try:
        cfg.VECTORIZED_GEOMETRY = False
        g_scalar = hr.build_graph(poly, excluded_pair_keys=keys)
        cfg.VECTORIZED_GEOMETRY = True
        g_vector = hr.build_graph(poly, excluded_pair_keys=keys)
    finally:
        cfg.VECTORIZED_GEOMETRY = saved
    assert g_scalar.adj == g_vector.adj


def test_exempt_extra_node_keeps_its_edges():
    poly = _residue_like()
    extras = [(30.0, 0.0)]  # coincides with a collinear exterior vertex
    extra_keys = {hr._coord_key(x, y) for x, y in extras}
    keys = hr._collinear_mid_edge_keys(poly, extra_keys)
    assert hr._coord_key(30.0, 0.0) not in keys

    g = hr.build_graph(poly, extra_nodes=extras, excluded_pair_keys=keys)
    idx = hr._node_idx(g.index, 30.0, 0.0)
    assert idx is not None
    assert g.adj[idx]  # the shared corner stays connected


def test_v1_paths_keep_full_graph():
    # v1 planners never pass an exclusion set: identical results either way,
    # and mid-edge vertices remain usable waypoints there.
    poly = _residue_like()
    saved = cfg.HOLE_ROUTER_MID_EDGE_PRUNE
    try:
        cfg.HOLE_ROUTER_MID_EDGE_PRUNE = True
        cuts_on = hr.plan_hole_cuts(poly, min_hole_area=50.0)
        route_on = hr.route_hole_opening(poly, hole_index=0)
        cfg.HOLE_ROUTER_MID_EDGE_PRUNE = False
        cuts_off = hr.plan_hole_cuts(poly, min_hole_area=50.0)
        route_off = hr.route_hole_opening(poly, hole_index=0)
    finally:
        cfg.HOLE_ROUTER_MID_EDGE_PRUNE = saved
    assert [c.wkt for c in cuts_on] == [c.wkt for c in cuts_off]
    assert (route_on is None) == (route_off is None)
    if route_on is not None:
        assert route_on.line.wkt == route_off.line.wkt
