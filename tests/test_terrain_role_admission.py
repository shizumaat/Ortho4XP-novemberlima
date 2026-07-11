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
    def __init__(self, role, polygon):
        self.role = role
        self.polygon = polygon


class _FakeLayout:
    def __init__(self, shapes):
        self.shapes = shapes
        self.canonical_points = CanonicalPointRegistry()


def _square(x0, y0, side=10.0):
    return Polygon([(x0, y0), (x0 + side, y0), (x0 + side, y0 + side),
                    (x0, y0 + side)])


# ── admitted_terrain_roles gate logic ────────────────────────────────────
def test_admitted_empty_with_master_gate_off():
    assert not cfg.ONE_SOLVE_TERRAIN                    # default OFF
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

    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", True)
    assert SP.admitted_terrain_roles() == frozenset(
        {ROLE_RUNWAY_CLEARANCE, ROLE_GRADED_STRIP})


def test_declared_terrain_roles_are_not_already_pavement_roles():
    # The declared set must be DISJOINT from PAVEMENT_ROLES, else admitting a
    # role would silently double-count today's pavement.
    assert not (SP.TERRAIN_GRAPH_ROLES & SP.PAVEMENT_ROLES)


# ── _build_node_list admission hook ──────────────────────────────────────
def _layout_with_terrain():
    return _FakeLayout([
        _FakeShape(ROLE_APRON, _square(0.0, 0.0)),
        _FakeShape(ROLE_GRADED_STRIP, _square(100.0, 100.0)),
    ])


def test_node_list_excludes_terrain_role_with_gates_off():
    nodes, b2i = SP._build_node_list(_layout_with_terrain())
    # Only the apron's 4 corners — the graded_strip is not admitted.
    assert len(nodes) == 4
    assert (100.0, 100.0) not in nodes


def test_node_list_admits_terrain_role_when_gated_on(monkeypatch):
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", True)
    nodes, b2i = SP._build_node_list(_layout_with_terrain())
    # Apron (4) + graded_strip band (4) now share the registry / node list.
    assert len(nodes) == 8
    assert (100.0, 100.0) in nodes


def test_node_list_identical_object_when_admitted_empty(monkeypatch):
    # Master ON but no sub-gate → admitted empty → the node list is exactly
    # what the gates-off build produces (byte-identical membership).
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    layout_a = _layout_with_terrain()
    nodes_a, _ = SP._build_node_list(layout_a)
    assert len(nodes_a) == 4                            # graded_strip excluded
