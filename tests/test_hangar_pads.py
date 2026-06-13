"""Hangar pads (s81) — ingestion gating + taxilane trim at building edges.

User rulings 2026-06-12 (docs/hangar_pads.md):
* hangars feed the SAME building-pad list as terminals (gate
  ``HANGAR_PADS``); ``tower`` stays fallback-only;
* a taxilane intersecting a building stops at the building edge.
"""
import pytest

from shapely.geometry import LineString, Polygon

import auto_patch.terminals as terminals
from auto_patch.terminals import (
    _extract_osm_terminals,
    trim_centerlines_at_buildings,
)


def _to_m(lon, lat):
    # Flat test projection: 1 deg = 1 m, identity-ish.
    return (lon, lat)


def _square_way(wid, x0, y0, size, tags):
    """Closed square way + its nodes dict fragment."""
    nids = [f"{wid}_n{i}" for i in range(4)]
    nodes = {
        nids[0]: (y0, x0),
        nids[1]: (y0, x0 + size),
        nids[2]: (y0 + size, x0 + size),
        nids[3]: (y0 + size, x0),
    }
    return (wid, nids + [nids[0]], tags), nodes


@pytest.fixture
def osm_airport():
    """One explicit terminal + one hangar, both 50 m squares."""
    nodes = {}
    term_way, n1 = _square_way("w1", 0, 0, 50, {"aeroway": "terminal"})
    hang_way, n2 = _square_way("w2", 100, 0, 50, {"aeroway": "hangar"})
    nodes.update(n1)
    nodes.update(n2)
    return nodes, [term_way, hang_way], []


def test_hangar_admitted_alongside_explicit_terminal(
        osm_airport, monkeypatch):
    monkeypatch.setattr(terminals, "HANGAR_PADS", True)
    nodes, ways, rels = osm_airport
    polys = _extract_osm_terminals(nodes, ways, rels, _to_m)
    assert len(polys) == 2


def test_gate_off_preserves_terminal_only_guard(osm_airport, monkeypatch):
    # Pre-s81 behaviour: explicit terminal present -> hangar excluded.
    monkeypatch.setattr(terminals, "HANGAR_PADS", False)
    nodes, ways, rels = osm_airport
    polys = _extract_osm_terminals(nodes, ways, rels, _to_m)
    assert len(polys) == 1


def test_no_terminal_fallback_unchanged(monkeypatch):
    # No explicit terminal: hangar AND tower admitted (2026-04-28
    # fallback) regardless of the gate.
    nodes = {}
    hang_way, n1 = _square_way("w1", 0, 0, 50, {"aeroway": "hangar"})
    tower_way, n2 = _square_way("w2", 100, 0, 20, {"aeroway": "tower"})
    nodes.update(n1)
    nodes.update(n2)
    for gate in (True, False):
        monkeypatch.setattr(terminals, "HANGAR_PADS", gate)
        polys = _extract_osm_terminals(nodes, [hang_way, tower_way], [],
                                       _to_m)
        assert len(polys) == 2, f"gate={gate}"


def test_tower_stays_fallback_only(monkeypatch):
    # Explicit terminal present: tower must NOT be admitted even
    # with the hangar gate on.
    monkeypatch.setattr(terminals, "HANGAR_PADS", True)
    nodes = {}
    term_way, n1 = _square_way("w1", 0, 0, 50, {"aeroway": "terminal"})
    tower_way, n2 = _square_way("w2", 100, 0, 20, {"aeroway": "tower"})
    nodes.update(n1)
    nodes.update(n2)
    polys = _extract_osm_terminals(nodes, [term_way, tower_way], [], _to_m)
    assert len(polys) == 1


# ──────────────────────────────────────────────────────────────────────
# Taxilane trim at building edges
# ──────────────────────────────────────────────────────────────────────
PAD = Polygon([(10, -5), (20, -5), (20, 5), (10, 5)])


def test_trim_lane_crossing_building_splits():
    out, n = trim_centerlines_at_buildings(
        [(LineString([(0, 0), (30, 0)]), "A")], PAD)
    assert n == 1
    assert [r for _a, r in out] == ["A", "A"]
    for axis, _r in out:
        assert axis.length == pytest.approx(10.0)
        assert not axis.crosses(PAD)


def test_trim_lane_ending_inside_building_stops_at_edge():
    out, n = trim_centerlines_at_buildings(
        [(LineString([(0, 0), (15, 0)]), "B")], PAD)
    assert n == 1
    assert len(out) == 1
    axis = out[0][0]
    assert axis.length == pytest.approx(10.0)
    # The trimmed endpoint sits ON the pad boundary -> weldable.
    assert PAD.exterior.distance(axis.boundary.geoms[1]) < 1e-9


def test_trim_lane_fully_inside_building_dropped():
    out, n = trim_centerlines_at_buildings(
        [(LineString([(12, 0), (18, 0)]), "C")], PAD)
    assert n == 1
    assert out == []


def test_trim_clear_lane_untouched_and_empty_union_noop():
    lanes = [(LineString([(0, 10), (30, 10)]), "D")]
    out, n = trim_centerlines_at_buildings(lanes, PAD)
    assert n == 0 and out == lanes
    out2, n2 = trim_centerlines_at_buildings(lanes, None)
    assert n2 == 0 and out2 == lanes
