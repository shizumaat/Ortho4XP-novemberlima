"""Byte-identity parity tests for the vectorized visibility-graph adjacency
builder (Wave 3, ``O4_VECTORIZED_GEOMETRY``).

The vectorized shapely-2 batch path in ``hole_router._build_adjacency_vectorized``
must produce an adjacency structure IDENTICAL to the reference scalar double
loop (``_build_adjacency_scalar``): same edge set, same per-node list order, and
identical edge weights — so any downstream Dijkstra tie breaks the same way and
the emitted cuts are byte-identical.  Pure synthetic geometry, headless."""
import math

import pytest
import shapely
from shapely.geometry import LineString, Polygon

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


# ── Targeted parity cases for the sound prunes (relate_pattern boundary-run
#    prefilter + sampled contains_xy prefilter) inside the vectorized path ────


def _adjacency_edge(graph, point_a, point_b):
    """The ``(weight)`` of edge point_a→point_b in ``graph`` or ``None``."""
    idx_a = hr._node_idx(graph.index, *point_a)
    idx_b = hr._node_idx(graph.index, *point_b)
    assert idx_a is not None and idx_b is not None
    for neighbor, weight in graph.adj[idx_a]:
        if neighbor == idx_b:
            return weight
    return None


def test_boundary_run_chord_still_rejected():
    """A chord lying along a collinear boundary run (longer than eps) must be
    rejected by the boundary-run stage in BOTH paths — proving the
    relate_pattern prune forwards the hit to the intersection measurement
    instead of changing the verdict."""
    poly = _dense_holed()
    g_scalar = _build(poly, (), vectorized=False)
    g_vector = _build(poly, (), vectorized=True)
    assert g_vector.adj == g_scalar.adj

    chord_a, chord_b = (0.0, 0.0), (10.0, 0.0)
    # The chord passes the contains stage (it lies inside the eps-buffered
    # pavement), so its absence can only come from the boundary-run stage.
    buffered = poly.buffer(hr._EPS_M)
    chord = LineString([chord_a, chord_b])
    assert buffered.contains(chord)
    assert hr._max_line_len(chord.intersection(poly.boundary)) > hr._EPS_M
    assert _adjacency_edge(g_scalar, chord_a, chord_b) is None
    assert _adjacency_edge(g_vector, chord_a, chord_b) is None


def test_sub_eps_boundary_overlap_still_kept():
    """A chord with a 1-D boundary overlap SHORTER than eps is a relate_pattern
    hit but must survive the measurement — proving the prune does not blanket-
    reject its hits."""
    tiny = 0.04                       # < _EPS_M = 0.05
    ring = [(0.0, 0.0), (10.0, 0.0), (10.0 + tiny, 0.0), (40.0, 0.0),
            (40.0, 40.0), (0.0, 40.0)]
    poly = Polygon(ring, [[(15, 15), (25, 15), (25, 25), (15, 25)]])
    chord_a, chord_b = (10.0, 0.0), (10.0 + tiny, 0.0)
    chord = LineString([chord_a, chord_b])
    # The chord IS a relate_pattern boundary-run hit (1-D interior overlap)…
    assert shapely.relate_pattern(chord, poly.boundary, "1********")
    # …but its overlap is below eps, so both paths must keep the edge.
    g_scalar = _build(poly, (), vectorized=False)
    g_vector = _build(poly, (), vectorized=True)
    assert g_vector.adj == g_scalar.adj
    weight_scalar = _adjacency_edge(g_scalar, chord_a, chord_b)
    weight_vector = _adjacency_edge(g_vector, chord_a, chord_b)
    assert weight_scalar is not None
    assert (weight_vector == weight_scalar
            == math.hypot(chord_b[0] - chord_a[0], chord_b[1] - chord_a[1]))


def _notched_polygon() -> Polygon:
    """Rectangle with a 1 m-wide notch descending from the top edge at
    x ∈ [19.5, 20.5]: the chord (0, 10)–(40, 10) crosses the notch void at
    t = 0.5 — a parameter the contains_xy prefilter does NOT sample — so the
    pair reaches the full ``contains`` stage and must still be rejected
    there (the prefilter is necessary-only, not sufficient)."""
    ring = [(0.0, 0.0), (40.0, 0.0), (40.0, 10.0), (40.0, 20.0),
            (20.5, 20.0), (20.5, 8.0), (19.5, 8.0), (19.5, 20.0),
            (0.0, 20.0), (0.0, 10.0)]
    return Polygon(ring, [[(5, 3), (12, 3), (12, 7), (5, 7)]])


def test_prefilter_missed_chord_still_rejected_by_full_contains():
    """A chord whose sampled points ALL land inside the buffered pavement but
    which exits the pavement between samples must still be rejected — the full
    ``contains`` on prefilter survivors is load-bearing."""
    poly = _notched_polygon()
    chord_a, chord_b = (0.0, 10.0), (40.0, 10.0)
    buffered = poly.buffer(hr._EPS_M)
    # Every prefilter sample lands inside the buffered pavement…
    for fraction in hr._PREFILTER_SAMPLE_FRACTIONS:
        sample_x = chord_a[0] + fraction * (chord_b[0] - chord_a[0])
        sample_y = chord_a[1] + fraction * (chord_b[1] - chord_a[1])
        assert shapely.contains_xy(buffered, sample_x, sample_y)
    # …but the chord itself leaves the pavement through the notch.
    assert not buffered.contains(LineString([chord_a, chord_b]))
    g_scalar = _build(poly, (), vectorized=False)
    g_vector = _build(poly, (), vectorized=True)
    assert g_vector.adj == g_scalar.adj
    assert _adjacency_edge(g_scalar, chord_a, chord_b) is None
    assert _adjacency_edge(g_vector, chord_a, chord_b) is None


def test_near_boundary_and_notched_parity_with_obstacles():
    """Near-boundary / notched geometry parity with an obstacle in play, so
    the prunes are exercised together with the per-obstacle rejection stage."""
    poly = _notched_polygon()
    obstacles = hr.build_obstacles(
        [Polygon([(30.0, 2.0), (36.0, 2.0), (36.0, 6.0), (30.0, 6.0)])])
    g_scalar = _build(poly, obstacles, vectorized=False)
    g_vector = _build(poly, obstacles, vectorized=True)
    assert g_scalar is not None and g_vector is not None
    assert g_vector.nodes == g_scalar.nodes
    assert g_vector.adj == g_scalar.adj
    assert sum(len(neighbors) for neighbors in g_scalar.adj) > 0
