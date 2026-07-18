"""Slice B stage B3 ORDER 2 — adjacent-ground band VARIABLE ADMISSION.

Covers the order-2 mechanisms in isolation (the five-minute-rule tier
below the CYXY acceptance builds):

  * the construct store's ORDER-2 SCHEMA SPLIT (``zone_rows`` /
    ``zone_nodes`` — the free-variable grid with frozen-nearest hosts
    and law envelope offsets; the d0 == 0 pavement weld row is never a
    zone row);
  * node-list admission + the one-interval-edge-per-zone-node
    constraint builder (including the identity-collision rule: a zone
    node interned with a pre-existing pavement variable gets NO edge);
  * the solved-surface resampler (weld verbatim -> exact variable ->
    along-row lerp -> depth-bracket interpolation -> counted
    beyond-coverage clamp -> counted analytic fallback);
  * gate-ON emission end-to-end on a hermetic fixture: every non-weld
    band vertex reads the solved store, not the analytic clamp.

The hard-error dependency chain is covered in
``test_terrain_role_admission.py``.
"""
import math

import pytest
from shapely.geometry import Polygon

import auto_patch.config as cfg
from auto_patch import adjacent_ground as AG
from auto_patch.canonical_points import CanonicalPointRegistry
from auto_patch.elevation_per_surface import solver_primitives as SP
from auto_patch.layout import BuiltShape, ROLE_APRON, ROLE_GRADED_STRIP

EDGE_ALT = 100.0
DEM_RISE = 6.0                                # terrain 6 m above the edge


class _FakeLayout:
    def m_to_ll(self, x, y):
        return (y / 111320.0, x / 111320.0)

    def __init__(self, shapes):
        self.shapes = shapes
        self.airport_boundary = None
        self.anchor = (0.0, 0.0)
        self.canonical_points = CanonicalPointRegistry()


def _apron(x0, y0, x1, y1):
    poly = Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
    coords = list(poly.exterior.coords)
    return BuiltShape(polygon=poly, role=ROLE_APRON,
                      ref="apron",
                      node_altitudes=[EDGE_ALT] * len(coords))


def _mk_layout():
    return _FakeLayout([_apron(0.0, 0.0, 200.0, 80.0)])


class _FakeRunway:
    lat_a, lon_a = 0.1, 0.1
    lat_b, lon_b = 0.1, 0.11


_RUNWAYS = [_FakeRunway()]


@pytest.fixture(autouse=True)
def _synthetic_dem(monkeypatch):
    monkeypatch.setattr(AG, "_sample_dem",
                        lambda dem, tl, tn, lat, lon: EDGE_ALT + DEM_RISE)
    yield


def _enable_full_chain(monkeypatch):
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    monkeypatch.setattr(
        cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", True)


# ── construct store schema split ─────────────────────────────────────────
def test_construct_builds_zone_grid():
    layout = _mk_layout()
    n = AG.construct_adjacent_ground_presolve(
        layout, dem=object(), tile_lat=0, tile_lon=0,
        source_runways=_RUNWAYS)
    assert n >= 1
    entry = layout.adjacent_ground_presolve[0]
    assert entry["zone_values"] is None                 # writeback slot
    rows = entry["zone_rows"]
    assert rows, "the 6 m rise must produce cut zone rows"
    ring_vertices = {(round(x, 3), round(y, 3))
                     for x, y in entry["shape"].polygon.exterior.coords}
    for row in rows:
        assert row["kind"] in ("fill", "cut")
        assert len(row["pts"]) == len(row["depths"]) == len(row["hosts"])
        # NO zone row at the pavement edge: the d0 == 0 weld row is the
        # pavement chain, never a free variable.
        assert all(d > 0.0 for d in row["depths"])
        # Frozen-nearest hosts are actual ring vertices.
        for hx, hy in row["hosts"]:
            assert (round(hx, 3), round(hy, 3)) in ring_vertices
    nodes = entry["zone_nodes"]
    assert nodes
    keys = [AG._vertex_key(*zn["xy"]) for zn in nodes]
    assert len(keys) == len(set(keys)), "zone nodes are millimetre-deduped"
    for zn in nodes:
        assert zn["floor_off"] is not None or zn["ceil_off"] is not None


def test_cut_zone_nodes_are_ceiling_only():
    layout = _mk_layout()
    AG.construct_adjacent_ground_presolve(
        layout, dem=object(), tile_lat=0, tile_lon=0,
        source_runways=_RUNWAYS)
    entry = layout.adjacent_ground_presolve[0]
    cut_keys = set()
    for row in entry["zone_rows"]:
        if row["kind"] == "cut":
            cut_keys.update(AG._vertex_key(x, y) for x, y in row["pts"])
    fill_keys = set()
    for row in entry["zone_rows"]:
        if row["kind"] == "fill":
            fill_keys.update(AG._vertex_key(x, y) for x, y in row["pts"])
    assert cut_keys, "constant rise fixture: cut rows expected"
    for zn in entry["zone_nodes"]:
        k = AG._vertex_key(*zn["xy"])
        if k in cut_keys and k not in fill_keys:
            # The analytic kind rule: cut pieces are ceiling-only (fill
            # bands own below-floor terrain).
            assert zn["floor_off"] is None
            assert zn["ceil_off"] is not None


# ── node-list admission + constraint builder ─────────────────────────────
def test_zone_nodes_admitted_and_one_edge_each(monkeypatch):
    _enable_full_chain(monkeypatch)
    layout = _mk_layout()
    AG.construct_adjacent_ground_presolve(
        layout, dem=object(), tile_lat=0, tile_lon=0,
        source_runways=_RUNWAYS)
    nodes, bucket_to_idx = SP._build_node_list(layout)
    first_zone = layout._adjacent_ground_first_zone_index
    assert first_zone == 4                     # the apron ring corners
    assert len(nodes) > first_zone, "zone nodes joined the node list"
    sc, zone_idx, (n_pavement, n_cross) = (
        SP._build_adjacent_ground_zone_constraints(layout, bucket_to_idx))
    assert sc and zone_idx
    # Every edge is a signed slab from a zone node to a HOST index below
    # the zone boundary (a pavement ring vertex).
    edge_count_by_node: dict = {}
    for entry in sc:
        assert entry["role"] == ROLE_GRADED_STRIP
        assert entry["ref"] == "adjacent_ground"
        for i, j, floor_off, ceil_off in entry["edges"]:
            assert i >= first_zone
            assert j < first_zone
            assert floor_off is not None or ceil_off is not None
            edge_count_by_node[i] = edge_count_by_node.get(i, 0) + 1
    # ONE envelope interval edge per edge-owning zone node — the law has
    # no neighbour coupling (order-2 scout refutation, ratified).
    assert edge_count_by_node
    assert set(edge_count_by_node.values()) == {1}
    assert n_cross == len(zone_idx) - len(edge_count_by_node) - n_pavement


def test_pavement_collision_gets_no_edge(monkeypatch):
    _enable_full_chain(monkeypatch)
    layout = _mk_layout()
    # A handcrafted store: one zone node EXACTLY on an apron corner (the
    # canonical registry interns it with the pavement variable) and one
    # clear of everything.
    layout.adjacent_ground_presolve = [{
        "shape": layout.shapes[0], "fill": [], "cut": [],
        "zone_rows": [], "zone_values": None,
        "zone_nodes": [
            {"xy": (0.0, 0.0), "host": (200.0, 0.0),
             "floor_off": -1.0, "ceil_off": 1.0},
            {"xy": (50.0, -10.0), "host": (0.0, 0.0),
             "floor_off": -1.0, "ceil_off": 1.0},
        ]}]
    nodes, bucket_to_idx = SP._build_node_list(layout)
    first_zone = layout._adjacent_ground_first_zone_index
    sc, zone_idx, (n_pavement, n_cross) = (
        SP._build_adjacent_ground_zone_constraints(layout, bucket_to_idx))
    assert n_pavement == 1 and n_cross == 0
    all_edges = [e for entry in sc for e in entry["edges"]]
    assert len(all_edges) == 1
    i, j, _f, _c = all_edges[0]
    assert nodes[i] == (50.0, -10.0)
    assert i >= first_zone and j < first_zone


# ── solved-surface resampler ─────────────────────────────────────────────
def _square_ring(side=20.0):
    # Closed ring, pavement at alt 100 everywhere; bands live at y < 0.
    coords = [(0.0, 0.0), (side, 0.0), (side, side), (0.0, side),
              (0.0, 0.0)]
    return coords, [EDGE_ALT] * len(coords)


def _entry_with_rows():
    row_3 = [(0.0, -3.0), (10.0, -3.0), (20.0, -3.0)]
    row_10 = [(0.0, -10.0), (20.0, -10.0)]
    zone_values = {
        AG._vertex_key(0.0, -3.0): 101.0,
        AG._vertex_key(10.0, -3.0): 102.0,
        AG._vertex_key(20.0, -3.0): 103.0,
        AG._vertex_key(0.0, -10.0): 105.0,
        AG._vertex_key(20.0, -10.0): 107.0,
    }
    return {
        "zone_rows": [
            {"kind": "cut", "d0": 3.0, "pts": row_3,
             "depths": [3.0] * 3, "hosts": [(0.0, 0.0)] * 3},
            {"kind": "cut", "d0": 3.0, "pts": row_10,
             "depths": [10.0] * 2, "hosts": [(0.0, 0.0)] * 2},
        ],
        "zone_values": zone_values,
    }


def test_solved_resampler_value_rules():
    coords, ring_alts = _square_ring()
    calls = []

    def analytic(x, y, kind):
        calls.append((x, y, kind))
        return (999.0, False)

    AG._reset_apparatus_hits()
    resample = AG._make_solved_band_resampler(
        _entry_with_rows(), coords, ring_alts, analytic)
    # 1. Weld row: pavement edge value verbatim.
    assert resample(5.0, 0.0, "cut") == (100.0, True)
    # 2. Exact variable adoption (registry-tolerance hash).
    assert resample(0.0, -3.0, "cut") == (101.0, False)
    assert AG._APPARATUS_HITS["solved_exact_variable"] == 1
    # 3. Along-row lerp (the ruled clip-minted-vertex valuation).
    value, is_weld = resample(5.0, -3.0, "cut")
    assert not is_weld and abs(value - 101.5) < 1e-6
    assert AG._APPARATUS_HITS["solved_row_on"] == 1
    # 4. Depth-bracket interpolation between the 3 m and 10 m rows:
    #    at (10, -6.5): row-3 value 102, row-10 value 106, t = 0.5.
    value, _ = resample(10.0, -6.5, "cut")
    assert abs(value - 104.0) < 1e-6
    assert AG._APPARATUS_HITS["solved_row_interpolated"] == 1
    # 5. Between the pavement edge (depth-0 row, value 100) and row 3.
    value, _ = resample(10.0, -1.5, "cut")
    assert abs(value - 101.0) < 1e-6
    # 6. Beyond the deepest solved row: clamp + counted.
    value, _ = resample(10.0, -15.0, "cut")
    assert abs(value - 106.0) < 1e-6
    assert AG._APPARATUS_HITS["solved_beyond_coverage"] == 1
    # 7. No row of the requested kind: counted analytic fallback.
    value, _ = resample(10.0, -5.0, "fill")
    assert value == 999.0 and calls
    assert AG._APPARATUS_HITS["solved_analytic_fallback"] == 1


# ── gate-ON emission end-to-end ──────────────────────────────────────────
def test_emit_admission_reads_solved_store(monkeypatch):
    _enable_full_chain(monkeypatch)
    layout = _mk_layout()
    AG.construct_adjacent_ground_presolve(
        layout, dem=object(), tile_lat=0, tile_lon=0,
        source_runways=_RUNWAYS)
    SOLVED = 123.4
    for entry in layout.adjacent_ground_presolve:
        entry["zone_values"] = {
            AG._vertex_key(*zn["xy"]): SOLVED
            for zn in entry["zone_nodes"]}
    n = AG.emit_adjacent_ground_bands(
        layout, dem=object(), tile_lat=0, tile_lon=0,
        source_runways=_RUNWAYS)
    assert n >= 1
    band_alts = set()
    for s in layout.shapes:
        if s.role == ROLE_GRADED_STRIP and s.ref == "adjacent_ground":
            band_alts.update(s.node_altitudes or [])
    assert band_alts, "bands emitted"
    # Every band value is either the pavement weld (100.0) or the solved
    # store constant — the analytic clamp never valued a vertex.
    assert band_alts <= {EDGE_ALT, SOLVED}, band_alts
    assert AG._APPARATUS_HITS["solved_analytic_fallback"] == 0
    assert AG._APPARATUS_HITS["solved_store_missing_shape"] == 0
    assert (AG._APPARATUS_HITS["solved_exact_variable"]
            + AG._APPARATUS_HITS["solved_row_on"]
            + AG._APPARATUS_HITS["solved_row_interpolated"]) > 0
    # Identity retirement rows on the hermetic fixture.
    assert AG._APPARATUS_HITS["value_changing_adoptions"] == 0


def test_emit_admission_footprints_equal_gate_off(monkeypatch):
    # THE census mechanism: under admission the emitter marches inline
    # over the FINAL pavement ring (pre-built footprints are not
    # consumed), so the gate-ON band CENSUS and footprint GEOMETRY equal
    # gate-OFF — the order-1 pre-solve-station census inflation is
    # structurally gone.  (Vertex SETS may differ: the post-emit
    # 3D-collinear decimation is value-dependent and the values differ
    # by design — solved store vs analytic clamp — so the comparison is
    # geometric, not WKT.)
    #
    # The emit-time TEAR/WALL heal keys on VALUE jumps and drops the
    # pinched vertex, so its footprint effect is value-dependent by
    # design — and this test's synthetic flat 123.4 zone surface mints
    # artificial >1 m walls gate-ON only.  Neutralize the heal here:
    # the invariant under test is the CONSTRUCT move's footprint
    # equivalence, not the heal (which has its own unit coverage).
    monkeypatch.setattr(AG, "_heal_band_tears",
                        lambda ring, alts, weld, tear_max, min_jump,
                        wall_max=None: (ring, alts))
    layout_off = _mk_layout()
    AG.emit_adjacent_ground_bands(
        layout_off, dem=object(), tile_lat=0, tile_lon=0,
        source_runways=_RUNWAYS)
    foot_off = [s.polygon for s in layout_off.shapes
                if s.role == ROLE_GRADED_STRIP
                and s.ref == "adjacent_ground"]

    _enable_full_chain(monkeypatch)
    layout_on = _mk_layout()
    AG.construct_adjacent_ground_presolve(
        layout_on, dem=object(), tile_lat=0, tile_lon=0,
        source_runways=_RUNWAYS)
    for entry in layout_on.adjacent_ground_presolve:
        entry["zone_values"] = {
            AG._vertex_key(*zn["xy"]): 123.4
            for zn in entry["zone_nodes"]}
    AG.emit_adjacent_ground_bands(
        layout_on, dem=object(), tile_lat=0, tile_lon=0,
        source_runways=_RUNWAYS)
    foot_on = [s.polygon for s in layout_on.shapes
               if s.role == ROLE_GRADED_STRIP
               and s.ref == "adjacent_ground"]
    assert len(foot_on) == len(foot_off)          # the band census
    from shapely.ops import unary_union
    union_on = unary_union(foot_on)
    union_off = unary_union(foot_off)
    assert union_on.symmetric_difference(union_off).area < 1e-6
