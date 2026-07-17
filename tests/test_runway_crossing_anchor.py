"""Runway-CROSSING-slab contact anchor (KBNA 13/31 defect H, 2026-07-16).

A taxi/junction join that TERMINATES on a ROLE_RUNWAY_CROSSING slab (the
surface that replaces the runway where two runways intersect) found no
ROLE_RUNWAY within RUNWAY_CONTACT_M and so got NO runway anchor — its
node floated off the slab edge (KBNA 13/31: 0.31 m steps).  The fix
includes the crossing slab in ``grade_graph._runway_anchors``' target
set; gate ``O4_RUNWAY_CROSSING_ANCHOR=0`` reverts.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from shapely.geometry import Polygon, LineString

from auto_patch import grade_graph as GG
from auto_patch.layout import (PavementLayout, BuiltShape, ROLE_RUNWAY,
                               ROLE_RUNWAY_CROSSING, ROLE_PRIMARY_PARALLEL)
from auto_patch.canonical_points import CanonicalPointRegistry


def _build_case():
    """A flat crossing slab with a taxi rect terminating just off its
    top edge, plus the terminating centerline.  Returns
    (layout, G, bucket_to_idx, taxi_node_indices)."""
    reg = CanonicalPointRegistry()
    # Flat crossing slab, x in [-50, 50], y in [-20, 20], at 100 m.
    slab_poly = Polygon([(-50, -20), (50, -20), (50, 20), (-50, 20)])
    slab = BuiltShape(polygon=slab_poly, role=ROLE_RUNWAY_CROSSING,
                      ref="13/31", altitude=100.0)
    # Taxi rect abutting the slab's top edge (y = 20) from y = 21..40.
    taxi_poly = Polygon([(-3, 21), (3, 21), (3, 40), (-3, 40)])
    taxi = BuiltShape(polygon=taxi_poly, role=ROLE_PRIMARY_PARALLEL,
                      ref="A", node_altitudes=[100.4, 100.4, 101.0, 101.0,
                                               100.4])
    # A centerline that runs down to (0, 21) — 1 m outside the slab edge,
    # inside RUNWAY_CONTACT_M (12 m).
    centerline = LineString([(0, 40), (0, 21)])
    layout = PavementLayout(
        icao="TEST", anchor=(36.0, -86.0),
        shapes=[slab, taxi],
        canonical_points=reg,
        apt_taxi_centerlines=[(centerline, "A")])

    # bucket_to_idx over every shape vertex (the node registry the graph
    # keys on).
    bucket_to_idx = {}
    idx = 0
    taxi_nodes = set()
    for s in layout.shapes:
        for (x, y) in list(s.polygon.exterior.coords)[:-1]:
            b = reg.get_or_add(float(x), float(y))
            if b not in bucket_to_idx:
                bucket_to_idx[b] = idx
                if s is taxi:
                    taxi_nodes.add(idx)
                idx += 1
    G = GG.UnifiedGraph()
    return layout, G, bucket_to_idx, taxi_nodes


def test_crossing_slab_anchors_taxi_join(monkeypatch):
    monkeypatch.setenv("O4_RUNWAY_CROSSING_ANCHOR", "1")
    layout, G, bucket_to_idx, taxi_nodes = _build_case()
    GG._runway_anchors(layout, G, bucket_to_idx)
    anchored = set(G.runway_anchor)
    assert anchored & taxi_nodes, (
        "taxi join terminating on the crossing slab must receive a runway "
        f"anchor; runway_anchor={G.runway_anchor}")
    # Anchor value is the slab surface value at the contact.
    for i in anchored & taxi_nodes:
        assert abs(G.runway_anchor[i] - 100.0) < 1e-6


def test_gate_off_leaves_slab_join_unanchored(monkeypatch):
    monkeypatch.setenv("O4_RUNWAY_CROSSING_ANCHOR", "0")
    layout, G, bucket_to_idx, taxi_nodes = _build_case()
    GG._runway_anchors(layout, G, bucket_to_idx)
    # No ROLE_RUNWAY present, so with crossings excluded there is no target
    # and the taxi join floats (the pre-fix behaviour that produced the step).
    assert not (set(G.runway_anchor) & taxi_nodes)


def test_real_runway_still_anchors_with_gate_on(monkeypatch):
    """The added crossing target must not break the existing runway path:
    a join onto a real ROLE_RUNWAY still anchors."""
    monkeypatch.setenv("O4_RUNWAY_CROSSING_ANCHOR", "1")
    reg = CanonicalPointRegistry()
    rwy_poly = Polygon([(-50, -20), (50, -20), (50, 20), (-50, 20)])
    rwy = BuiltShape(polygon=rwy_poly, role=ROLE_RUNWAY, ref="09/27",
                     altitude=100.0)
    taxi_poly = Polygon([(-3, 21), (3, 21), (3, 40), (-3, 40)])
    taxi = BuiltShape(polygon=taxi_poly, role=ROLE_PRIMARY_PARALLEL, ref="A",
                      node_altitudes=[100.4, 100.4, 101.0, 101.0, 100.4])
    centerline = LineString([(0, 40), (0, 21)])
    layout = PavementLayout(
        icao="TEST", anchor=(36.0, -86.0), shapes=[rwy, taxi],
        canonical_points=reg, apt_taxi_centerlines=[(centerline, "A")])
    bucket_to_idx = {}
    idx = 0
    taxi_nodes = set()
    for s in layout.shapes:
        for (x, y) in list(s.polygon.exterior.coords)[:-1]:
            b = reg.get_or_add(float(x), float(y))
            if b not in bucket_to_idx:
                bucket_to_idx[b] = idx
                if s is taxi:
                    taxi_nodes.add(idx)
                idx += 1
    G = GG.UnifiedGraph()
    GG._runway_anchors(layout, G, bucket_to_idx)
    assert set(G.runway_anchor) & taxi_nodes
