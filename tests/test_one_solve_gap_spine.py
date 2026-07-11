"""Slice B stage B2 — gap-fill drainage-spine absorption into the
one-solve graph (docs/slice_b_solver_absorption_design.md §B2, ratified
mechanism 2026-07-10).

Hermetic unit tests on a tiny hand-built layout (no fixtures): four
apron slabs enclosing one rectangular gap.  Verify:
  * gate-ON, the pre-solve construction stages the spine geometry and
    every spine vertex becomes a canonical-registry member and a solver
    node (the dedicated spine admission path — spine vertices are
    INTERIOR points, so the B0 ring hook alone can never admit them);
  * the envelope interval edges carry ``grade_law.adjacent_ground_
    envelope`` values at the frozen construction-time distance, with
    ``None`` sides preserved (the law's own open-side semantics);
  * spine endpoints FLOAT — the open-way design stands: >= 2 m off the
    gap ring, and no spine coordinate interns onto a ring vertex;
  * gate-OFF, everything is a structural no-op (no store, no admitted
    nodes, no edges);
  * the ``TAXIWAY_MAX_GRADE_CHANGE_PER_M`` second-difference fairing
    smooths a kinked chain within the envelope clamp.
"""
import math

from shapely.geometry import Point, Polygon

import auto_patch.config as cfg
from auto_patch.canonical_points import CanonicalPointRegistry
from auto_patch.elevation_per_surface import solver_primitives as SP
from auto_patch.elevation_per_surface.route_profile.solve import (
    _fair_gap_spine_chains)
from auto_patch.gap_fill import (
    _freeze_spine_parent_specs, construct_gap_fill_presolve)
from auto_patch.grade_law import adjacent_ground_envelope
from auto_patch.layout import ROLE_APRON


class _FakeShape:
    def __init__(self, role, polygon, *, ref=None, altitude=None,
                 altitude_high=None, altitude_low=None, node_altitudes=None):
        self.role = role
        self.polygon = polygon
        self.ref = ref
        self.altitude = altitude
        self.altitude_high = altitude_high
        self.altitude_low = altitude_low
        self.node_altitudes = node_altitudes


class _FakeLayout:
    def __init__(self, shapes):
        self.shapes = shapes
        self.canonical_points = CanonicalPointRegistry()

    def m_to_ll(self, x, y):
        return (x, y)


def _annulus_layout():
    """Four apron slabs enclosing the gap (20,20)-(180,20)-(180,60)-
    (20,60) — 160 m x 40 m, well over ``GAP_FILL_MIN_AREA_M2`` and under
    ``GAP_FILL_MAX_WIDTH_M``."""
    slabs = [
        Polygon([(0, 0), (200, 0), (200, 20), (0, 20)]),        # south
        Polygon([(0, 60), (200, 60), (200, 80), (0, 80)]),      # north
        Polygon([(0, 20), (20, 20), (20, 60), (0, 60)]),        # west
        Polygon([(180, 20), (200, 20), (200, 60), (180, 60)]),  # east
    ]
    return _FakeLayout([
        _FakeShape(ROLE_APRON, p, altitude=100.0) for p in slabs])


def _gate_on(monkeypatch):
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)


def _gate_off(monkeypatch):
    # Explicit gate-OFF pinning (defaults flipped ON, dev fad621d): the
    # master gate off keeps the whole admission path closed.
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)


# ── gate-OFF structural no-op ────────────────────────────────────────
def test_gate_off_no_admission_no_store(monkeypatch):
    _gate_off(monkeypatch)
    layout = _annulus_layout()
    # No pipeline construction gate-OFF -> no store.
    assert getattr(layout, "gap_fill_presolve", None) is None
    nodes, b2i = SP._build_node_list(layout)
    assert len(nodes) == 12   # 16 slab corners, 4 shared -> 12 unique
    # Even WITH a store, the gate keeps the admission path closed.
    construct_gap_fill_presolve(layout)
    assert layout.gap_fill_presolve            # geometry did construct
    nodes2, _ = SP._build_node_list(layout)
    assert len(nodes2) == 12                   # spine NOT admitted


# ── pre-solve construction + admission ───────────────────────────────
def test_spine_constructed_and_admitted_gate_on(monkeypatch):
    _gate_on(monkeypatch)
    layout = _annulus_layout()
    n_chains = construct_gap_fill_presolve(layout)
    assert n_chains == 1
    entry = layout.gap_fill_presolve[0]
    spine = entry["spine"]
    assert len(spine) >= 3
    gap = Polygon([(20, 20), (180, 20), (180, 60), (20, 60)])
    for px, py in spine:
        assert gap.contains(Point(px, py))
    nodes, b2i = SP._build_node_list(layout)
    assert len(nodes) == 12 + len(spine)
    cps = layout.canonical_points
    for px, py in spine:
        assert cps.get_or_add(px, py) in b2i   # registry member + node


def test_spine_endpoints_float_off_the_ring(monkeypatch):
    """The OPEN-WAY design stands (ratified): endpoints keep >= 2 m off
    the gap ring and no spine coordinate interns onto a ring vertex."""
    _gate_on(monkeypatch)
    layout = _annulus_layout()
    construct_gap_fill_presolve(layout)
    spine = layout.gap_fill_presolve[0]["spine"]
    ring = Polygon([(20, 20), (180, 20), (180, 60), (20, 60)]).exterior
    for px, py in spine:
        assert ring.distance(Point(px, py)) >= 2.0 - 1e-9
    ring_keys = set()
    cps = layout.canonical_points
    for s in layout.shapes:
        for vx, vy in s.polygon.exterior.coords:
            ring_keys.add(cps.get_or_add(vx, vy))
    for px, py in spine:
        assert cps.get_or_add(px, py) not in ring_keys


# ── envelope interval edges: law values, None sides ──────────────────
def test_frozen_specs_carry_law_values_with_none_sides():
    layout = _annulus_layout()
    airside = layout.shapes
    px, py = 100.0, 40.0        # mid-gap: 20 m from both long slabs
    specs = _freeze_spine_parent_specs(layout, airside, px, py)
    assert 1 <= len(specs) <= 2
    for (sx, sy), floor_off, ceil_off in specs:
        d = None
        best = None
        for s in airside:
            dd = s.polygon.exterior.distance(Point(px, py))
            if best is None or dd < best:
                best, d = dd, dd
        # every candidate parent here is an apron at the same distance
        exp_floor, exp_ceil = adjacent_ground_envelope(
            ROLE_APRON, None, None, 20.0)
        assert floor_off == exp_floor          # None preserved if None
        assert ceil_off == exp_ceil
        # the frozen station is a real ring vertex of some parent
        assert any((sx, sy) in
                   [(vx, vy) for vx, vy in s.polygon.exterior.coords]
                   for s in airside)


def test_interval_edges_built_from_specs(monkeypatch):
    _gate_on(monkeypatch)
    layout = _annulus_layout()
    construct_gap_fill_presolve(layout)
    nodes, b2i = SP._build_node_list(layout)
    sc_out, spine_idx, chains = SP._build_gap_spine_constraints(
        layout, b2i)
    assert len(sc_out) == 1
    entry = layout.gap_fill_presolve[0]
    assert spine_idx == {
        b2i[layout.canonical_points.get_or_add(px, py)]
        for px, py in entry["spine"]}
    sc = sc_out[0]
    assert sc["role"] == "graded_strip" and sc["ref"] == "gap_fill_spine"
    assert sc["edges"], "expected envelope interval edges"
    for edge in sc["edges"]:
        assert len(edge) == 4                  # B0 interval 4-tuple
        i, j, floor_off, ceil_off = edge
        assert i in spine_idx
        assert j not in spine_idx              # station = pavement node
        assert not (floor_off is None and ceil_off is None)
    # Edge count == resolvable spec count (nothing silently dropped).
    n_specs = sum(len(s) for s in entry["specs"])
    assert len(sc["edges"]) == n_specs


# ── second-difference fairing under the envelope clamp ───────────────
def test_fairing_smooths_kink_within_cap():
    # 5-station straight chain, 15 m spacing, values with a sharp kink.
    xy = [(0.0, 0.0), (15.0, 0.0), (30.0, 0.0), (45.0, 0.0), (60.0, 0.0)]
    idx = [0, 1, 2, 3, 4]
    # station node 5 far below/above never binds (open intervals).
    specs = [[(5, -10.0, 10.0)] for _ in xy]
    chains = [{"idx": idx, "xy": xy, "specs": specs}]
    elev = [100.0, 100.0, 101.5, 100.0, 100.0, 100.0]   # node 5 = station
    k_rate = cfg.TAXIWAY_MAX_GRADE_CHANGE_PER_M
    n_over = _fair_gap_spine_chains(elev, chains, k_rate)
    assert n_over == 0
    for t in range(1, 4):
        g1 = (elev[t] - elev[t - 1]) / 15.0
        g2 = (elev[t + 1] - elev[t]) / 15.0
        assert abs(g2 - g1) <= k_rate * 15.0 + 1e-3
    # endpoints pinned (no triple centres them)
    assert elev[0] == 100.0 and elev[4] == 100.0


def test_empty_intersection_keeps_nearer_parent(monkeypatch):
    """Two parents whose seed-time intervals are DISJOINT (a high and a
    low pavement whose envelopes cannot both hold) keep only the nearer
    (first) parent's interval edge — the analytic law's own
    empty-intersection rule (``gap_fill._spine_interval`` fallback),
    encoded at build time so the POCS sweep never receives an
    infeasible slab pair (the measured 27.9 M-visit livelock)."""
    _gate_on(monkeypatch)
    layout = _annulus_layout()
    construct_gap_fill_presolve(layout)
    entry = layout.gap_fill_presolve[0]
    # Force a synthetic conflict on the FIRST spine node: parent A
    # (nearer) demands z - z_a in [5, 6]; parent B demands [-6, -5].
    a_xy = (20.0, 20.0)
    b_xy = (180.0, 60.0)
    entry["specs"][0] = [(a_xy, 5.0, 6.0), (b_xy, -6.0, -5.0)]
    nodes, b2i = SP._build_node_list(layout)
    seed = [100.0] * len(nodes)      # equal seeds -> intervals disjoint
    sc_out, spine_idx, chains = SP._build_gap_spine_constraints(
        layout, b2i, seed_elev=seed)
    cps = layout.canonical_points
    i0 = b2i[cps.get_or_add(*entry["spine"][0])]
    ja = b2i[cps.get_or_add(*a_xy)]
    jb = b2i[cps.get_or_add(*b_xy)]
    edges0 = [e for e in sc_out[0]["edges"] if e[0] == i0]
    assert (i0, ja, 5.0, 6.0) in edges0          # nearer parent kept
    assert all(e[1] != jb for e in edges0)       # farther parent pruned
    # chains' specs mirror the pruning (the fairing clamp agrees).
    assert chains[0]["specs"][0] == [(ja, 5.0, 6.0)]
    # Without seed_elev (unit-test mode) both edges survive.
    sc2, _, _ = SP._build_gap_spine_constraints(layout, b2i)
    edges2 = [e for e in sc2[0]["edges"] if e[0] == i0]
    assert len(edges2) == 2


def test_fairing_respects_envelope_floor():
    # A tight FLOOR (z_spine - z_station >= 1.0 with station at 100)
    # blocks the smoother from cutting the centre below 101.
    xy = [(0.0, 0.0), (15.0, 0.0), (30.0, 0.0)]
    chains = [{"idx": [0, 1, 2], "xy": xy,
               "specs": [[(3, -10.0, 10.0)],
                         [(3, 1.0, 10.0)],
                         [(3, -10.0, 10.0)]]}]
    elev = [100.0, 103.0, 100.0, 100.0]
    n_over = _fair_gap_spine_chains(
        elev, chains, cfg.TAXIWAY_MAX_GRADE_CHANGE_PER_M)
    assert elev[1] >= 101.0 - 1e-9             # floor held
    assert n_over >= 1                         # honest residual kink
