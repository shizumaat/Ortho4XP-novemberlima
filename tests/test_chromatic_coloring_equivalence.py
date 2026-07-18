"""Equivalence tests for the 2026-07-18 chromatic-projection performance
round (perf plan: hub-accelerated exact first-fit coloring, incremental
coloring across lazy rounds, vectorized per-color array build, feasibility
pre-check) — every item must reproduce the ORIGINAL results exactly, so the
solver output stays byte-identical.

Hermetic — no build, no fixtures.  Covers:
  (a) coloring identity — the accelerated first-fit
      (``_extend_edge_coloring_by_write``) against a verbatim reimplementation
      of the original per-edge ``forbidden``-union greedy, on random mixed-kind
      graphs, a degree-5000 hub, empty and single-edge lists;
  (b) incremental extension — carrying coloring state over an appended suffix
      equals coloring the full list from scratch (prefix-stability);
  (c) feasibility pre-check — ``_project_chromatic`` returns identical values,
      return tuple and counters with the pre-check forced on and off, for
      feasible (pre-check fires) and infeasible (it does not) systems, and the
      certified re-projection call pattern is a bit-identical no-op.
"""
import random

import pytest

from auto_patch.elevation_per_surface.route_profile import one_solve as OS


# ── (a) reference oracle: the ORIGINAL greedy, verbatim ───────────────────

def _reference_color_edges_by_write(iter_edges):
    """Verbatim reimplementation of the pre-2026-07-18 greedy first-fit
    coloring (per-edge ``forbidden`` union copy + scan-from-zero), kept as the
    oracle the accelerated production version must match exactly."""
    used: dict = {}                 # node -> set of colors of edges writing it
    ncolors = 0
    edge_color = [0] * len(iter_edges)
    for edge_index in range(len(iter_edges)):
        i, j, _budget, kind = iter_edges[edge_index]
        if kind == 0:
            write_nodes = (i, j)
        elif kind == 1:
            write_nodes = (j,)
        else:
            write_nodes = (i,)
        forbidden: set = set()
        for node in write_nodes:
            s = used.get(node)
            if s:
                forbidden |= s
        color = 0
        while color in forbidden:
            color += 1
        edge_color[edge_index] = color
        if color + 1 > ncolors:
            ncolors = color + 1
        for node in write_nodes:
            s = used.get(node)
            if s is None:
                used[node] = {color}
            else:
                s.add(color)
    colors: list = [[] for _ in range(ncolors)]
    for edge_index in range(len(iter_edges)):
        colors[edge_color[edge_index]].append(edge_index)
    return colors


def _random_iter_edges(seed, node_count, edge_target,
                       interval_fraction=0.2):
    """Random ``iter_edges`` list mixing kinds 0/1/2 and interval-sentinel
    (``budget is None``) entries — the coloring reads only endpoints + kind,
    so interval edges exercise the same code path with the sentinel present."""
    rng = random.Random(seed)
    out = []
    for _ in range(edge_target):
        i = rng.randrange(node_count)
        j = rng.randrange(node_count)
        if i == j:
            continue
        kind = rng.choice((0, 0, 0, 1, 2))
        budget = (None if rng.random() < interval_fraction
                  else round(rng.uniform(0.1, 3.0), 3))
        out.append((i, j, budget, kind))
    return out


def test_coloring_matches_reference_on_random_mixed_graphs():
    for seed in range(25):
        iter_edges = _random_iter_edges(seed, node_count=30, edge_target=250)
        got = OS._color_edges_by_write(iter_edges)
        expected = _reference_color_edges_by_write(iter_edges)
        # list-of-lists equality pins BOTH the per-edge colors and the count
        assert got == expected, f"seed {seed}"


def test_coloring_matches_reference_on_dense_near_clique():
    # the OTHH pathology: a lazy-expanded all-pair apron body — every node
    # pair regulated, all kind 0 — where the original union copy went
    # quadratic.  Partition identity must hold exactly.
    rng = random.Random(99)
    node_count = 28
    iter_edges = []
    for i in range(node_count):
        for j in range(i + 1, node_count):
            iter_edges.append((i, j, round(rng.uniform(0.1, 2.0), 3), 0))
    assert (OS._color_edges_by_write(iter_edges)
            == _reference_color_edges_by_write(iter_edges))


def test_coloring_matches_reference_on_degree_5000_hub():
    # one hub written by every edge (kind 0 star) plus a leaf-to-leaf tail so
    # the scan start (max of the two per-node lower bounds) is exercised.
    hub_degree = 5000
    iter_edges = [(0, leaf, 1.0, 0) for leaf in range(1, hub_degree + 1)]
    iter_edges += [(leaf, leaf + 1, 1.0, 0)
                   for leaf in range(1, hub_degree, 7)]
    assert (OS._color_edges_by_write(iter_edges)
            == _reference_color_edges_by_write(iter_edges))


def test_coloring_matches_reference_one_directional_hub_single_color():
    # kind-1 hub (host fixed, zones follow): all edges write DISTINCT zone
    # endpoints — one color, exactly as before.
    iter_edges = [(0, leaf, 1.0, 1) for leaf in range(1, 4001)]
    got = OS._color_edges_by_write(iter_edges)
    assert got == _reference_color_edges_by_write(iter_edges)
    assert len(got) == 1


def test_coloring_empty_and_single_edge():
    assert OS._color_edges_by_write([]) == []
    for kind in (0, 1, 2):
        single = [(3, 7, 1.5, kind)]
        assert (OS._color_edges_by_write(single)
                == _reference_color_edges_by_write(single) == [[0]])


# ── (b) incremental extension across appended suffixes ────────────────────

def test_incremental_extension_matches_fresh_full_coloring():
    for seed in range(12):
        full = _random_iter_edges(seed, node_count=25, edge_target=180)
        fresh_color, fresh_count = OS._extend_edge_coloring_by_write(full, {})
        for split in (0, 1, len(full) // 3, len(full) // 2, len(full)):
            state: dict = {}
            OS._extend_edge_coloring_by_write(full[:split], state)
            edge_color, color_count = \
                OS._extend_edge_coloring_by_write(full, state)
            assert edge_color == fresh_color, f"seed {seed} split {split}"
            assert color_count == fresh_count, f"seed {seed} split {split}"


def test_incremental_extension_over_multiple_rounds():
    # three append rounds (the lazy-expansion call pattern) — the carried
    # state must land on the from-scratch coloring after every round.
    full = _random_iter_edges(7, node_count=20, edge_target=150)
    cut_a, cut_b = len(full) // 4, (2 * len(full)) // 3
    state: dict = {}
    OS._extend_edge_coloring_by_write(full[:cut_a], state)
    OS._extend_edge_coloring_by_write(full[:cut_b], state)
    edge_color, color_count = OS._extend_edge_coloring_by_write(full, state)
    fresh_color, fresh_count = OS._extend_edge_coloring_by_write(full, {})
    assert edge_color == fresh_color and color_count == fresh_count


# ── (c) feasibility pre-check: value- and counter-identical ───────────────

def _run_chromatic(elev_seed, iter_edges, n, run_precheck, bounds=None):
    elev = list(elev_seed)
    stats: dict = {}
    result = OS._project_chromatic(elev, iter_edges, n, 4000, 1e-3,
                                   bounds or {}, stats=stats,
                                   run_feasibility_precheck=run_precheck)
    return elev, result, stats


_COMPARABLE_STATS = ("edges", "sweeps", "sweeps_avoided", "certified",
                     "worst")


def test_precheck_feasible_system_identical_with_and_without():
    # feasible (incl. an edge over budget by LESS than tol — the sweep's own
    # tolerance — and an in-slab interval edge): the pre-check must fire and
    # reproduce the certified sweep-1 exit bit for bit.
    n = 5
    iter_edges = [(0, 1, 5.0, 1), (1, 2, 1.0, 0),
                  (2, 3, 5.0, 0), (3, 4, 5.0, 2),
                  (1, 3, None, 0)]
    bounds = {4: (-3.0, 3.0)}
    elev_seed = [0.0, 1.0, 2.0005, 3.0, 4.0]   # edge (1,2): over by 5e-4 < tol
    elev_on, result_on, stats_on = \
        _run_chromatic(elev_seed, iter_edges, n, True, bounds)
    elev_off, result_off, stats_off = \
        _run_chromatic(elev_seed, iter_edges, n, False, bounds)
    assert elev_on == elev_off                 # exact float equality
    assert result_on == result_off == (1, True)
    for key in _COMPARABLE_STATS:
        assert stats_on[key] == stats_off[key], key
    assert stats_on["certified"] is True
    assert stats_on["sweeps"] == 1
    assert stats_on["sweeps_avoided"] == 3999
    assert stats_on["worst"] == 0.0


def test_precheck_infeasible_system_identical_with_and_without():
    # infeasible at entry (symmetric over-cap AND an out-of-slab interval):
    # the pre-check must NOT fire and the full path runs either way —
    # everything identical, including the colors counter.
    n = 4
    iter_edges = [(0, 1, 1.0, 1), (1, 2, 1.0, 0), (2, 3, 1.0, 2),
                  (0, 3, None, 0)]
    bounds = {3: (-0.5, 0.5)}
    elev_seed = [0.0, 6.0, -3.0, 2.0]
    elev_on, result_on, stats_on = \
        _run_chromatic(elev_seed, iter_edges, n, True, bounds)
    elev_off, result_off, stats_off = \
        _run_chromatic(elev_seed, iter_edges, n, False, bounds)
    assert elev_on == elev_off
    assert result_on == result_off
    assert stats_on == stats_off               # colors included: same path


def test_precheck_random_feasible_instances_identical():
    for seed in range(15):
        rng = random.Random(seed)
        n = rng.randint(6, 20)
        true = [rng.uniform(-15.0, 15.0) for _ in range(n)]
        iter_edges = []
        for _ in range(n * 3):
            i, j = rng.randrange(n), rng.randrange(n)
            if i == j:
                continue
            budget = abs(true[i] - true[j]) + rng.uniform(0.001, 2.0)
            iter_edges.append((i, j, round(budget, 6),
                               rng.choice((0, 1, 2))))
        elev_on, result_on, stats_on = \
            _run_chromatic(true, iter_edges, n, True)
        elev_off, result_off, stats_off = \
            _run_chromatic(true, iter_edges, n, False)
        assert elev_on == elev_off, f"seed {seed}"
        assert result_on == result_off == (1, True), f"seed {seed}"
        for key in _COMPARABLE_STATS:
            assert stats_on[key] == stats_off[key], f"seed {seed}: {key}"


def test_precheck_empty_edge_list_identical():
    elev_on, result_on, stats_on = _run_chromatic([1.0, 2.0], [], 2, True)
    elev_off, result_off, stats_off = _run_chromatic([1.0, 2.0], [], 2, False)
    assert elev_on == elev_off == [1.0, 2.0]
    assert result_on == result_off == (1, True)
    for key in _COMPARABLE_STATS:
        assert stats_on[key] == stats_off[key], key


def test_reprojection_of_projected_surface_is_bit_identical_no_op():
    # the OTHH call pattern the pre-check targets: a second projection over an
    # already-projected surface must certify and change NOTHING.
    n = 6
    iter_edges = [(k, k + 1, 0.75, 0) for k in range(n - 1)]
    elev = [0.0, 4.0, -3.0, 5.0, -1.0, 2.0]
    OS._project_chromatic(elev, iter_edges, n, 4000, 1e-3, {})
    first_pass = list(elev)
    stats: dict = {}
    result = OS._project_chromatic(elev, iter_edges, n, 4000, 1e-3, {},
                                   stats=stats)
    assert elev == first_pass                  # bit-identical values
    assert result == (1, True)
    assert stats["certified"] is True and stats["sweeps"] == 1


def test_precheck_respects_zero_iteration_cap():
    # max_iters == 0 historically returns (0, False) without certifying — the
    # pre-check must not convert that into a certificate.
    elev = [0.0, 0.0]
    result = OS._project_chromatic(elev, [(0, 1, 1.0, 0)], 2, 0, 1e-3, {})
    assert result == (0, False)


def test_hub_coloring_is_fast():
    # regression guard for the quadratic hub (generous bound: the accelerated
    # scan is O(k); the original union-copy greedy took ~1 s at k = 8000).
    import time
    hub_degree = 8000
    iter_edges = [(0, leaf, 1.0, 0) for leaf in range(1, hub_degree + 1)]
    start = time.perf_counter()
    colors = OS._color_edges_by_write(iter_edges)
    elapsed = time.perf_counter() - start
    assert len(colors) == hub_degree           # star: every edge its own color
    assert elapsed < 0.5, f"hub coloring took {elapsed:.3f}s"
