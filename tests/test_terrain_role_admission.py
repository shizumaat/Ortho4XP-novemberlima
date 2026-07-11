"""Stage B0 terrain-role admission scaffolding tests
(docs/slice_b_solver_absorption_design.md).

Hermetic — a tiny hand-built layout, no fixtures.  Verifies:
  * ``admitted_terrain_roles`` is empty with the master gate off (default) AND
    with the master gate on but every per-role sub-gate off (an empty admitted
    set = a structural no-op — Stage B0's landing condition);
  * each per-role sub-gate admits exactly its terrain role;
  * ``_build_node_list`` is byte-identical (same node list) when the admitted
    set is empty, and grows to include a terrain-role shape's ring vertices only
    when that role is admitted — the object-bridge plate admission pattern.
"""
from shapely.geometry import Polygon

import auto_patch.config as cfg
from auto_patch.canonical_points import CanonicalPointRegistry
from auto_patch.elevation_per_surface import solver_primitives as SP
from auto_patch.layout import (
    ROLE_APRON, ROLE_GRADED_STRIP, ROLE_RUNWAY_CLEARANCE,
)


class _FakeShape:
    def __init__(self, role, polygon, ref=None):
        self.role = role
        self.polygon = polygon
        self.ref = ref


class _FakeLayout:
    def __init__(self, shapes):
        self.shapes = shapes
        self.canonical_points = CanonicalPointRegistry()


def _square(x0, y0, side=10.0):
    return Polygon([(x0, y0), (x0 + side, y0), (x0 + side, y0 + side),
                    (x0, y0 + side)])


def _pin_all_gates_off(monkeypatch):
    # Explicit gate-OFF pinning (defaults flipped ON, dev fad621d): the
    # master gate off alone keeps every admission path closed, but we pin
    # the whole stack so the state is unambiguous and env-independent.
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", False)


# ── admitted_terrain_roles gate logic ────────────────────────────────────
def test_admitted_empty_with_master_gate_off(monkeypatch):
    _pin_all_gates_off(monkeypatch)
    assert not cfg.ONE_SOLVE_TERRAIN                    # pinned OFF
    assert SP.admitted_terrain_roles() == frozenset()


def test_admitted_empty_with_master_on_but_subgates_off(monkeypatch):
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)
    # Master ON, nothing admitted → the Stage B0 no-op condition.
    assert SP.admitted_terrain_roles() == frozenset()


def test_subgates_require_the_master_gate(monkeypatch):
    # A sub-gate alone (master off) admits NOTHING.
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    assert SP.admitted_terrain_roles() == frozenset()


def test_each_subgate_admits_its_role(monkeypatch):
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)
    assert SP.admitted_terrain_roles() == frozenset({ROLE_RUNWAY_CLEARANCE})

    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    assert SP.admitted_terrain_roles() == frozenset({ROLE_GRADED_STRIP})

    # The band-admission sub-gate is HARD-CHAINED (B3 order 2) onto the
    # construct gate and the B1 + B2 sub-gates — the full stack admits
    # both roles.
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    monkeypatch.setattr(
        cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", True)
    assert SP.admitted_terrain_roles() == frozenset(
        {ROLE_RUNWAY_CLEARANCE, ROLE_GRADED_STRIP})


def test_declared_terrain_roles_are_not_already_pavement_roles():
    # The declared set must be DISJOINT from PAVEMENT_ROLES, else admitting a
    # role would silently double-count today's pavement.
    assert not (SP.TERRAIN_GRAPH_ROLES & SP.PAVEMENT_ROLES)


# ── admitted_terrain_refs (role, ref) granularity (B3 order 1) ────────────
def test_admitted_refs_empty_with_master_gate_off(monkeypatch):
    _pin_all_gates_off(monkeypatch)
    assert not cfg.ONE_SOLVE_TERRAIN                    # pinned OFF
    assert SP.admitted_terrain_refs() == frozenset()


def test_each_subgate_admits_its_role_ref_pair(monkeypatch):
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)
    assert SP.admitted_terrain_refs() == frozenset(
        {(ROLE_RUNWAY_CLEARANCE, "runway_end_skirt")})

    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    assert SP.admitted_terrain_refs() == frozenset(
        {(ROLE_GRADED_STRIP, "gap_fill_spine")})

    # The band-admission gate maps to the adjacent_ground ref specifically —
    # NOT the gap_fill_spine ref that shares ROLE_GRADED_STRIP.  Since B3
    # order 2 it is HARD-CHAINED onto construct + B1 + B2, so the full
    # stack is required and the admitted set is the full ref set.
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    monkeypatch.setattr(
        cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", True)
    assert SP.admitted_terrain_refs() == frozenset(
        {(ROLE_RUNWAY_CLEARANCE, "runway_end_skirt"),
         (ROLE_GRADED_STRIP, "gap_fill_spine"),
         (ROLE_GRADED_STRIP, "adjacent_ground")})


def test_band_admission_gate_hard_errors_on_partial_chain(monkeypatch):
    # B3 order 2 (coordinator ruling): the band-admission sub-gate
    # HARD-ERRORS unless the construct gate and the B1 + B2 sub-gates
    # are ALL on — a partial gate set would silently measure the wrong
    # thing.
    import pytest
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    monkeypatch.setattr(
        cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", True)
    with pytest.raises(RuntimeError, match="GRADED_STRIP_CONSTRUCT"):
        SP.admitted_terrain_refs()
    monkeypatch.setattr(
        cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)
    with pytest.raises(RuntimeError, match="GAP_FILL_SPINE"):
        SP.admitted_terrain_refs()
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    monkeypatch.setattr(
        cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)
    with pytest.raises(RuntimeError, match="RUNWAY_END_SKIRT"):
        SP.admitted_terrain_refs()
    # Master gate OFF keeps every sub-gate inert (no error, no admission).
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", False)
    assert SP.admitted_terrain_refs() == frozenset()


def test_admitted_roles_is_the_ref_set_role_projection(monkeypatch):
    # The back-compat role projection collapses both ROLE_GRADED_STRIP families
    # to the single role.
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    monkeypatch.setattr(
        cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", True)
    assert SP.admitted_terrain_roles() == frozenset(
        {ROLE_RUNWAY_CLEARANCE, ROLE_GRADED_STRIP})


# ── _build_node_list admission hook ──────────────────────────────────────
def _layout_with_terrain(ref="adjacent_ground"):
    return _FakeLayout([
        _FakeShape(ROLE_APRON, _square(0.0, 0.0)),
        _FakeShape(ROLE_GRADED_STRIP, _square(100.0, 100.0), ref=ref),
    ])


def test_node_list_excludes_terrain_role_with_gates_off(monkeypatch):
    _pin_all_gates_off(monkeypatch)
    nodes, b2i = SP._build_node_list(_layout_with_terrain())
    # Only the apron's 4 corners — the graded_strip is not admitted.
    assert len(nodes) == 4
    assert (100.0, 100.0) not in nodes


def _enable_band_admission_chain(monkeypatch):
    # The full hard-chained gate stack the band admission requires
    # (B3 order 2).
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    monkeypatch.setattr(
        cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", True)


def test_node_list_admits_band_ref_when_band_gate_on(monkeypatch):
    _enable_band_admission_chain(monkeypatch)
    nodes, b2i = SP._build_node_list(_layout_with_terrain())
    # Apron (4) + adjacent_ground band (4) now share the registry / node list.
    assert len(nodes) == 8
    assert (100.0, 100.0) in nodes


def test_gap_face_ref_not_admitted_by_band_gate(monkeypatch):
    # The collision resolution: a ROLE_GRADED_STRIP shape whose ref is the
    # GAP-FILL FACE family is NOT admitted through the ring hook by the band
    # admission stack — admission is (role, ref)-keyed, and the gap FACE ref
    # ("gap_fill") is not a terrain graph family at all (only the spine
    # store admits gap variables).
    _enable_band_admission_chain(monkeypatch)
    nodes, b2i = SP._build_node_list(_layout_with_terrain(ref="gap_fill"))
    assert len(nodes) == 4                              # only the apron
    assert (100.0, 100.0) not in nodes


def test_band_ref_not_admitted_by_gap_gate(monkeypatch):
    # The mirror: with only the GAP sub-gate on, an adjacent_ground BAND shape
    # placed pre-solve (B3 order 1 construction) is NOT admitted as a solver
    # variable — construction moves pre-solve but values stay analytic.
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)
    nodes, b2i = SP._build_node_list(_layout_with_terrain(ref="adjacent_ground"))
    assert len(nodes) == 4                              # only the apron
    assert (100.0, 100.0) not in nodes


def test_node_list_identical_object_when_admitted_empty(monkeypatch):
    # Master ON but no sub-gate → admitted empty → the node list is exactly
    # what the gates-off build produces (byte-identical membership).  Pin
    # every sub-gate OFF explicitly (defaults flipped ON, dev fad621d).
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP_CONSTRUCT", False)
    layout_a = _layout_with_terrain()
    nodes_a, _ = SP._build_node_list(layout_a)
    assert len(nodes_a) == 4                            # graded_strip excluded
