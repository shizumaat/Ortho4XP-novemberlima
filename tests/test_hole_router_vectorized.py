"""Byte-identity parity tests for the vectorized visibility-graph adjacency
builder (Wave 3, ``O4_VECTORIZED_GEOMETRY``).

The vectorized shapely-2 batch path in ``hole_router._build_adjacency_vectorized``
must produce an adjacency structure IDENTICAL to the reference scalar double
loop (``_build_adjacency_scalar``): same edge set, same per-node list order, and
identical edge weights — so any downstream Dijkstra tie breaks the same way and
the emitted cuts are byte-identical.  Pure synthetic geometry, headless."""
import math

import pytest
from shapely.geometry import Polygon

from auto_patch.pavement import hole_router as hr


def _holed(side: float, holes) -> Polygon:
    ext = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side)]
    return Polygon(ext, holes)


# A spread of holed polygons: single hole, multiple holes, off-grid holes,
# and dense boundaries (many collinear nodes → the boundary-run rejection path).
_CASES = [
    _holed(40.0, [[(5, 5), (12, 5), (12, 12), (5, 12)]]),
    _holed(40.0, [[(5, 5), (12, 5), (12, 12), (5, 12)],
                  [(20, 18), (30, 18), (30, 30), (20, 30)],
                  [(6, 25), (11, 25), (11, 33), (6, 33)]]),
    _holed(50.0, [[(7.3, 8.1), (18.9, 6.4), (17.2, 19.7), (6.1, 20.3)],
                  [(28.4, 30.2), (41.1, 29.6), (40.5, 43.8), (27.9, 42.1)]]),
]


def _dense_holed() -> Polygon:
    # Exterior with a vertex every 5 m (collinear runs) + two holes.
    side = 60.0
    step = 5.0
    n = int(round(side / step))
    ring = []
    for i in range(n):
        ring.append((i * step, 0.0))
    for i in range(n):
        ring.append((side, i * step))
    for i in range(n):
        ring.append((side - i * step, side))
    for i in range(n):
        ring.append((0.0, side - i * step))
    holes = [[(12, 12), (22, 12), (22, 22), (12, 22)],
             [(35, 30), (48, 30), (48, 45), (35, 45)]]
    return Polygon(ring, holes)


_CASES.append(_dense_holed())

# Obstacle variants: an un-subtracted rect whose interior is a hard no-cross.
_OBSTACLE_POLYS = [Polygon([(14.5, 2.0), (19.0, 2.0), (19.0, 6.5), (14.5, 6.5)])]


def _build(poly, obstacles, vectorized):
    old = hr._build_adjacency
    import auto_patch.config as cfg
    saved = cfg.VECTORIZED_GEOMETRY
    cfg.VECTORIZED_GEOMETRY = vectorized
    try:
        return hr.build_graph(poly, obstacles=obstacles)
    finally:
        cfg.VECTORIZED_GEOMETRY = saved
        assert hr._build_adjacency is old  # dispatch untouched


@pytest.mark.parametrize("idx", range(len(_CASES)))
@pytest.mark.parametrize("with_obstacle", [False, True])
def test_vectorized_adjacency_is_byte_identical(idx, with_obstacle):
    poly = _CASES[idx]
    obstacles = hr.build_obstacles(_OBSTACLE_POLYS) if with_obstacle else ()
    g_scalar = _build(poly, obstacles, vectorized=False)
    g_vector = _build(poly, obstacles, vectorized=True)
    assert g_scalar is not None and g_vector is not None
    # Node list, ring membership, and full adjacency (order + weights) identical.
    assert g_vector.nodes == g_scalar.nodes
    assert g_vector.ext_idx == g_scalar.ext_idx
    assert g_vector.hole_rings == g_scalar.hole_rings
    assert g_vector.adj == g_scalar.adj
    # At least one edge exists (guards against a trivially-empty parity pass).
    assert sum(len(a) for a in g_scalar.adj) > 0


@pytest.mark.parametrize("idx", range(len(_CASES)))
def test_vectorized_weights_match_hypot(idx):
    """Edge weights are the exact Euclidean node distance (no drift)."""
    poly = _CASES[idx]
    g = _build(poly, (), vectorized=True)
    for i, lst in enumerate(g.adj):
        xi, yi = g.nodes[i]
        for j, w in lst:
            xj, yj = g.nodes[j]
            assert w == math.hypot(xi - xj, yi - yj)


def test_chunking_preserves_order():
    """A tiny pair-chunk size must not change the result (order-preservation
    across chunk boundaries)."""
    poly = _dense_holed()
    saved = hr._VIS_PAIR_CHUNK
    g_full = _build(poly, (), vectorized=True)
    try:
        hr._VIS_PAIR_CHUNK = 7   # force many chunk boundaries
        g_chunked = _build(poly, (), vectorized=True)
    finally:
        hr._VIS_PAIR_CHUNK = saved
    assert g_chunked.adj == g_full.adj
