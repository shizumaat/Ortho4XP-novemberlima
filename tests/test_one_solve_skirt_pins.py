"""Slice B stage B1 — runway-end-skirt absorption into the one-solve
graph (docs/slice_b_solver_absorption_design.md §B1).

Hermetic unit tests on a tiny hand-built layout (no fixtures).  Verify:
  * gate-ON, a runway-end-skirt shape's ring vertices are admitted to the
    canonical registry / solver node list (the object-bridge plate
    admission pattern) and every vertex is a HARD PIN at its birth-computed
    ``node_altitudes`` value that holds through the seed;
  * a skirt vertex shared with pavement interns to the SAME node (identity)
    and that node is hard-pinned to the skirt value;
  * gate-OFF, the skirt role is not admitted — its vertices are absent from
    the node list and nothing is pinned (structural no-op).
"""
from shapely.geometry import Polygon

import auto_patch.config as cfg
from auto_patch.canonical_points import CanonicalPointRegistry
from auto_patch.elevation_per_surface import solver_primitives as SP
from auto_patch.layout import ROLE_APRON, ROLE_RUNWAY_CLEARANCE


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

    def m_to_ll(self, x, y):        # only used when dem is not None
        return (x, y)


def _apron_square():
    # Apron 0..10 in x, 0..10 in y, flat at 100 m.
    poly = Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)])
    return _FakeShape(ROLE_APRON, poly, altitude=100.0)


def _skirt_shape():
    # A runway-end skirt abutting the apron's top edge (shares the two
    # top-edge vertices 0,10 and 10,10) and stepping DOWN to 97 m on its
    # terrain-side row (12,12)/(-2,12).  node_altitudes closed ring.
    coords = [(0.0, 10.0), (10.0, 10.0), (12.0, 12.0), (-2.0, 12.0)]
    poly = Polygon(coords)
    na = [100.0, 100.0, 97.0, 97.0]
    return _FakeShape(ROLE_RUNWAY_CLEARANCE, poly, ref="runway_end_skirt",
                      node_altitudes=na + [na[0]])


def _layout_with_skirt():
    return _FakeLayout([_apron_square(), _skirt_shape()])


# ── node-list admission ──────────────────────────────────────────────
def test_skirt_excluded_from_node_list_gate_off(monkeypatch):
    # The one-solve-terrain gates default ON since fad621da (round-7
    # slice-B bundle) — force them OFF explicitly: this test asserts
    # the gate-OFF contract, not the shipping default.
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)
    nodes, b2i = SP._build_node_list(_layout_with_skirt())
    # Only the apron's 4 corners; the skirt terrain-side vertices absent.
    assert (12.0, 12.0) not in nodes
    assert (-2.0, 12.0) not in nodes
    assert len(nodes) == 4


def test_skirt_admitted_to_node_list_gate_on(monkeypatch):
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)
    nodes, b2i = SP._build_node_list(_layout_with_skirt())
    # Apron 4 corners + the 2 NEW skirt terrain-side vertices = 6 (the two
    # top-edge vertices are SHARED with the apron -> identity, not new).
    assert (12.0, 12.0) in nodes
    assert (-2.0, 12.0) in nodes
    assert len(nodes) == 6


# ── HARD PIN through the seed ────────────────────────────────────────
def test_skirt_vertices_hard_pinned_at_birth_values(monkeypatch):
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", True)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GAP_FILL_SPINE", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_GRADED_STRIP", False)
    layout = _layout_with_skirt()
    nodes, b2i = SP._build_node_list(layout)
    elev, is_hard, have_initial = SP._seed_elevations(layout, nodes, b2i)
    cps = layout.canonical_points

    def _idx(x, y):
        return b2i[cps.get_or_add(x, y)]

    # Every skirt ring vertex is a HARD PIN at its node_altitudes value.
    for x, y, val in ((0.0, 10.0, 100.0), (10.0, 10.0, 100.0),
                      (12.0, 12.0, 97.0), (-2.0, 12.0, 97.0)):
        i = _idx(x, y)
        assert is_hard[i], f"skirt vertex ({x},{y}) must be hard-pinned"
        assert have_initial[i]
        assert abs(elev[i] - val) < 1e-9, (
            f"pin at ({x},{y}) = {elev[i]}, expected {val}")
    # The shared top-edge vertices are ONE node with both apron & skirt.
    assert _idx(0.0, 10.0) == b2i[cps.get_or_add(0.0, 10.0)]
    # Pins are published in the protected seam-pin set.
    assert getattr(layout, "_seam_pin_idx", None)
    assert _idx(12.0, 12.0) in layout._seam_pin_idx


def test_gate_off_no_skirt_pins(monkeypatch):
    # Gates default ON since fad621da — force OFF; see the note on
    # ``test_skirt_excluded_from_node_list_gate_off``.
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN", False)
    monkeypatch.setattr(cfg, "ONE_SOLVE_TERRAIN_RUNWAY_END_SKIRT", False)
    layout = _layout_with_skirt()
    nodes, b2i = SP._build_node_list(layout)
    elev, is_hard, have_initial = SP._seed_elevations(layout, nodes, b2i)
    # Gate off -> skirt not admitted -> only apron nodes, none from the
    # skirt terrain-side; nothing published to the seam-pin set by a skirt.
    assert len(nodes) == 4
    assert not getattr(layout, "_seam_pin_idx", set())
