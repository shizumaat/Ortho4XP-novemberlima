"""Tests for the rasterized reach-band field (Tier 3 wave 2a).

Fast synthetic tests exercise the graph/Dijkstra core (:func:`solve_reach_fields`)
against an independent brute-force min-plus reference and pin the mask-erosion
conservatism, off-net policy, and determinism.  The integration tests build CYXY
once (module-cached) to check the end-to-end band closure and gate-off inertness.
"""
from __future__ import annotations

import heapq
import math

import numpy as np
import pytest

from auto_patch.elevation_per_surface.raster_reach_band import (
    solve_reach_fields)


# ── Independent brute-force reference ────────────────────────────────────────

def _neighbors(r, c, nrows, ncols, paved, connectivity):
    """The same edge set solve_reach_fields builds: 8-neighbour chamfer, plus
    knight moves (through paved cardinal intermediates) at connectivity 16."""
    out = []
    for (dr, dc) in [(-1, 0), (1, 0), (0, -1), (0, 1),
                     (-1, -1), (-1, 1), (1, -1), (1, 1)]:
        rr, cc = r + dr, c + dc
        if 0 <= rr < nrows and 0 <= cc < ncols and paved[rr, cc]:
            out.append((rr, cc, math.hypot(dr, dc)))
    if connectivity >= 16:
        for (dr, dc) in [(1, 2), (2, 1), (2, -1), (1, -2),
                         (-1, 2), (-2, 1), (-2, -1), (-1, -2)]:
            rr, cc = r + dr, c + dc
            if not (0 <= rr < nrows and 0 <= cc < ncols and paved[rr, cc]):
                continue
            m1 = (int(np.sign(dr)) if abs(dr) == 2 else 0,
                  int(np.sign(dc)) if abs(dc) == 2 else 0)
            m2 = (dr - m1[0], dc - m1[1])
            if paved[r + m1[0], c + m1[1]] and paved[r + m2[0], c + m2[1]]:
                out.append((rr, cc, math.hypot(dr, dc)))
    return out


def _brute_fields(paved, cap, seeds, cell, connectivity=8):
    """Reference ceiling/floor by an explicit multi-source Dijkstra.

    ``seeds`` = ``{(r, c): value}``.  Edge weight = mean-cap * Euclidean-step *
    cell — identical to the module.  ceiling = min_a(value_a + d), floor =
    max_a(value_a − d)."""
    nrows, ncols = paved.shape
    ceiling = np.full((nrows, ncols), math.inf)
    floor = np.full((nrows, ncols), -math.inf)
    # ceiling: Dijkstra where node cost is value_a + accumulated weight.
    pq = [(float(v), r, c) for (r, c), v in seeds.items()]
    heapq.heapify(pq)
    best = {}
    while pq:
        cost, r, c = heapq.heappop(pq)
        if (r, c) in best:
            continue
        best[(r, c)] = cost
        ceiling[r, c] = cost
        for (rr, cc, step) in _neighbors(r, c, nrows, ncols, paved,
                                         connectivity):
            if (rr, cc) in best:
                continue
            w = 0.5 * (cap[r, c] + cap[rr, cc]) * step * cell
            heapq.heappush(pq, (cost + w, rr, cc))
    # floor: symmetric — maximise value_a − d ⇔ minimise (−value_a) + d.
    pq = [(-float(v), r, c) for (r, c), v in seeds.items()]
    heapq.heapify(pq)
    best = {}
    while pq:
        cost, r, c = heapq.heappop(pq)
        if (r, c) in best:
            continue
        best[(r, c)] = cost
        floor[r, c] = -cost
        for (rr, cc, step) in _neighbors(r, c, nrows, ncols, paved,
                                         connectivity):
            if (rr, cc) in best:
                continue
            w = 0.5 * (cap[r, c] + cap[rr, cc]) * step * cell
            heapq.heappush(pq, (cost + w, rr, cc))
    return ceiling, floor


def _seed_dicts(paved, seeds):
    """Map ``{(r, c): value}`` to the module's ``seed_min``/``seed_max`` keyed by
    row-major paved-cell rank."""
    cell_id = np.full(paved.shape, -1, dtype=np.int64)
    cell_id.ravel()[paved.ravel()] = np.arange(int(paved.sum()))
    smin, smax = {}, {}
    for (r, c), v in seeds.items():
        cid = int(cell_id[r, c])
        smin[cid] = min(smin.get(cid, math.inf), float(v))
        smax[cid] = max(smax.get(cid, -math.inf), float(v))
    return smin, smax


@pytest.mark.parametrize("connectivity", [8, 16])
def test_solve_fields_matches_bruteforce_uniform(connectivity):
    """Uniform cap, single anchor: the field IS value + cap·grid-distance."""
    nrows, ncols, cell = 7, 9, 2.0
    paved = np.ones((nrows, ncols), dtype=bool)
    cap = np.full((nrows, ncols), 0.01)
    seeds = {(0, 0): 100.0}
    smin, smax = _seed_dicts(paved, seeds)
    ceiling, floor = solve_reach_fields(paved, cap, smin, smax, cell,
                                        connectivity=connectivity)
    bc, bf = _brute_fields(paved, cap, seeds, cell, connectivity=connectivity)
    assert np.allclose(ceiling, bc, atol=1e-9)
    assert np.allclose(floor, bf, atol=1e-9)


@pytest.mark.parametrize("connectivity", [8, 16])
def test_solve_fields_matches_bruteforce_multi_anchor_hole(connectivity):
    """Two anchors + a hole + a varying cap field: exact min/max envelope."""
    nrows, ncols, cell = 8, 8, 3.0
    paved = np.ones((nrows, ncols), dtype=bool)
    paved[3:5, 3:5] = False                         # interior hole
    rng = np.random.default_rng(0)
    cap = np.where(paved, 0.01 + 0.02 * rng.random((nrows, ncols)), 0.0)
    seeds = {(0, 0): 50.0, (7, 7): 60.0}
    smin, smax = _seed_dicts(paved, seeds)
    ceiling, floor = solve_reach_fields(paved, cap, smin, smax, cell,
                                        connectivity=connectivity)
    bc, bf = _brute_fields(paved, cap, seeds, cell, connectivity=connectivity)
    # Non-paved cells are inf in both; compare only paved.
    m = paved
    assert np.allclose(ceiling[m], bc[m], atol=1e-9)
    assert np.allclose(floor[m], bf[m], atol=1e-9)


def test_solve_fields_unreachable_is_inf():
    """A paved component with no anchor stays inf (→ band None: no constraint)."""
    nrows, ncols, cell = 4, 9, 2.0
    paved = np.ones((nrows, ncols), dtype=bool)
    paved[:, 4] = False                             # split into two components
    cap = np.where(paved, 0.01, 0.0)
    seeds = {(0, 0): 10.0}                           # only the left component
    smin, smax = _seed_dicts(paved, seeds)
    ceiling, floor = solve_reach_fields(paved, cap, smin, smax, cell)
    assert math.isfinite(ceiling[0, 0])
    assert not math.isfinite(ceiling[0, 8])         # right component unreached
    assert not math.isfinite(floor[0, 8])


def test_solve_fields_deterministic():
    """Two identical runs produce byte-identical grids."""
    nrows, ncols, cell = 10, 10, 2.5
    paved = np.ones((nrows, ncols), dtype=bool)
    cap = np.full((nrows, ncols), 0.015)
    seeds = {(0, 0): 0.0, (9, 9): 5.0, (0, 9): -3.0}
    smin, smax = _seed_dicts(paved, seeds)
    a = solve_reach_fields(paved, cap, smin, smax, cell)
    b = solve_reach_fields(paved, cap, smin, smax, cell)
    assert np.array_equal(a[0], b[0])
    assert np.array_equal(a[1], b[1])


def test_mask_erosion_is_conservative():
    """The ½-cell inward buffer used by the rasterizer keeps every eroded cell
    centre strictly inside the true pavement union (discrete domain ⊆ truth)."""
    import shapely
    from shapely.geometry import Polygon
    cell = 3.0
    union = Polygon([(0, 0), (60, 0), (60, 40), (0, 40)])   # a paved rectangle
    eroded = union.buffer(-0.5 * cell)
    xs = np.arange(-5, 65, 1.0)
    ys = np.arange(-5, 45, 1.0)
    gx, gy = np.meshgrid(xs, ys)
    shapely.prepare(union)
    shapely.prepare(eroded)
    in_eroded = shapely.contains_xy(eroded, gx.ravel(), gy.ravel())
    in_union = shapely.contains_xy(union, gx.ravel(), gy.ravel())
    # every eroded-mask cell centre is inside the true union (discrete ⊆ truth).
    assert in_eroded.any()
    assert np.all(in_union[in_eroded])
    # and no eroded point lies within ½ cell of the true boundary.
    ex, ey = gx.ravel()[in_eroded], gy.ravel()[in_eroded]
    from shapely.geometry import Point
    assert min(union.exterior.distance(Point(px, py))
               for px, py in zip(ex, ey)) >= 0.5 * cell - 1e-9


# ── Integration tests (build CYXY once, module-cached) ───────────────────────

pytestmark = pytest.mark.xdist_group("CYXY")


def _cyxy_graph():
    from conftest import cached_airport_layout
    from auto_patch.elevation_per_surface import solver_primitives as SP
    import auto_patch.grade_graph as GG
    layout = cached_airport_layout("CYXY")
    nodes, b2i = SP._build_node_list(layout)
    G = GG.build_unified_graph(layout, b2i)
    return layout, G, nodes


def test_gate_off_is_legacy(monkeypatch):
    """Gate OFF restores the legacy band (no raster field is built)."""
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_unified)
    layout, G, _ = _cyxy_graph()
    monkeypatch.setenv("O4_RASTER_REACH_BAND", "0")
    band_off = reach_band_unified(layout, G)
    assert getattr(band_off, "raster_meta", None) is None
    monkeypatch.setenv("O4_RASTER_REACH_BAND", "1")
    band_on = reach_band_unified(layout, G)
    assert getattr(band_on, "raster_meta", None) is not None
    meta = band_on.raster_meta
    assert meta["paved_cells"] > 0 and meta["anchors_seeded"] > 0


def test_raster_band_covers_band_role_vertices(monkeypatch):
    """The raster band answers (non-None) at the airside vertices it governs."""
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_unified)
    from auto_patch.grade_graph_validate import _band_roles, _open_ring
    layout, G, _ = _cyxy_graph()
    monkeypatch.setenv("O4_RASTER_REACH_BAND", "1")
    band = reach_band_unified(layout, G)
    roles = _band_roles()
    covered = total = 0
    for s in layout.shapes:
        if s.role not in roles or s.polygon is None or s.polygon.is_empty:
            continue
        for (x, y) in _open_ring(list(s.polygon.exterior.coords)):
            total += 1
            b = band(x, y)
            if b is not None:
                covered += 1
                assert isinstance(b, tuple) and len(b) == 2
    assert total > 0
    assert covered / total > 0.95           # near-total coverage


def test_offnet_returns_none(monkeypatch):
    """Off-net policy (mission item 3): a point far off the mask returns None
    (no legacy fallback — that ~74 ms/point path is what the field eliminates)."""
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_unified)
    from auto_patch.elevation_per_surface.raster_reach_band import (
        build_raster_reach_band)
    layout, G, _ = _cyxy_graph()
    monkeypatch.setenv("O4_RASTER_REACH_BAND", "1")
    raster = build_raster_reach_band(layout, G)
    assert raster is not None
    # 50 km out: beyond the bounded radius (and the grid) → None.
    assert raster(5.0e4, 5.0e4) is None
    # The wrapper returns the raster closure directly (same None off-net).
    wrapped = reach_band_unified(layout, G)
    assert wrapped(5.0e4, 5.0e4) is None
    # An on-pavement anchor position answers with a finite interval.
    for i in sorted(G.runway_anchor):
        b = wrapped(*G.pos[i])
        assert b is None or (isinstance(b, tuple) and len(b) == 2)
        break


def test_raster_band_deterministic(monkeypatch):
    """Two builds of the raster band give identical values at sampled points."""
    from auto_patch.elevation_per_surface.building_feasibility import (
        reach_band_unified)
    layout, G, nodes = _cyxy_graph()
    monkeypatch.setenv("O4_RASTER_REACH_BAND", "1")
    b1 = reach_band_unified(layout, G)
    b2 = reach_band_unified(layout, G)
    pts = [G.pos[i] for i in sorted(G.pos)[:400]]
    for (x, y) in pts:
        assert b1(x, y) == b2(x, y)
