"""Crossing influence zone publication (Phase 1,
docs/specs/crossing-terrain-ownership.md — ``crossing_terrain.py``).

The zone is published ON THE LAYOUT once, pre-solve, and every terrain
writer (adjacent-ground bands, runway-end skirts, clearance, gap-fill)
consults it as a hard keep-out.  These tests pin the publication API:

  * nothing to publish -> 0 records, consumers read ``None``;
  * the mapped depressed-road corridor publishes UNgated (protecting a
    depressed public road was never subject to the crossing mask);
  * per-crossing records carry their kind; the cached union covers them;
  * ``road_lanes`` extra seed geometries anchor corridor growth without
    ever joining the returned corridor themselves.

The portal-pair zone content (full footprints, collar rings, the
buried-roof-by-construction rule and its rollback knob) is pinned in
``test_adjacent_ground_wrap_standoff.py`` and
``test_object_bridge_terrain.py``.
"""
import types

from shapely.geometry import Point, Polygon

from auto_patch import crossing_terrain as CT
from auto_patch import road_lanes as RL


def _shape(role, polygon, ref=None):
    return types.SimpleNamespace(role=role, ref=ref, polygon=polygon)


class _Layout:
    """Minimal layout: shapes + anchor + an identity ``ll_to_m`` so a fake
    OSM node stored as ``(lat, lon) = (y, x)`` maps back to local
    ``(x, y)`` (the ``test_road_lanes`` fixture convention)."""

    def __init__(self, shapes, anchor=None):
        self.shapes = shapes
        self.anchor = anchor

    def ll_to_m(self, lat, lon):
        return (lon, lat)


def _patch_roads(monkeypatch, nodes, ways):
    import auto_patch.osm_load as OL
    monkeypatch.setattr(OL, "_load_osm_big_roads",
                        lambda lat, lon, *a, **k: (nodes, ways))


def _airside():
    return _shape("junction",
                  Polygon([(-40, -40), (240, -40), (240, 40), (-40, 40)]))


class TestPublicationApi:
    def test_no_anchor_publishes_nothing(self, monkeypatch):
        _patch_roads(monkeypatch, {}, [])
        lay = _Layout([_airside()], anchor=None)
        assert CT.publish_crossing_influence_zones(lay) == 0
        assert CT.crossing_influence_zone_union(lay) is None
        assert CT.crossing_influence_zone_prepared(lay) is None
        assert getattr(lay, CT.CROSSING_INFLUENCE_ZONES_ATTRIBUTE) == []

    def test_nothing_recognized_publishes_nothing(self, monkeypatch):
        _patch_roads(monkeypatch, {}, [])
        lay = _Layout([_airside()], anchor=(0.0, 0.0))
        assert CT.publish_crossing_influence_zones(lay) == 0
        assert CT.crossing_influence_zone_union(lay) is None

    def test_unpublished_layout_reads_none(self):
        lay = _Layout([_airside()], anchor=(0.0, 0.0))
        assert CT.crossing_influence_zone_union(lay) is None
        assert CT.crossing_influence_zone_prepared(lay) is None

    def test_mapped_bore_corridor_publishes_ungated(self, monkeypatch):
        # A mapped tunnel=yes bore near airside publishes a road_corridor
        # record even with the crossing mask OFF — the depressed-road
        # protection was never subject to BRIDGE_CROSSING_MASK.
        from auto_patch import config
        monkeypatch.setattr(config, "BRIDGE_CROSSING_MASK", False)
        nodes = {"n1": (0.0, 20.0), "n2": (0.0, 120.0)}
        _patch_roads(monkeypatch, nodes, [
            ("bore", ["n1", "n2"],
             {"highway": "primary", "tunnel": "yes"})])
        lay = _Layout([_airside()], anchor=(0.0, 0.0))
        assert CT.publish_crossing_influence_zones(lay) == 1
        records = getattr(lay, CT.CROSSING_INFLUENCE_ZONES_ATTRIBUTE)
        assert [r["kind"] for r in records] == ["road_corridor"]
        zone = CT.crossing_influence_zone_union(lay)
        assert zone is not None
        assert zone.contains(Point(70.0, 0.0))     # on the bore

    def test_union_covers_every_record(self, monkeypatch):
        nodes = {"n1": (0.0, 20.0), "n2": (0.0, 120.0)}
        _patch_roads(monkeypatch, nodes, [
            ("bore", ["n1", "n2"],
             {"highway": "primary", "tunnel": "yes"})])
        lay = _Layout([_airside()], anchor=(0.0, 0.0))
        assert CT.publish_crossing_influence_zones(lay) >= 1
        zone = CT.crossing_influence_zone_union(lay)
        for record in getattr(lay, CT.CROSSING_INFLUENCE_ZONES_ATTRIBUTE):
            assert zone.covers(record["zone"])


class TestRoadLanesExtraSeeds:
    def test_extra_seed_anchors_growth_but_never_joins(self, monkeypatch):
        # A plain SURFACE road (no tunnel tag) passing under a recognized
        # deck: without seeds the corridor is None; with the deck box as
        # an extra seed the road joins the corridor — but the deck box
        # itself is NOT part of the returned geometry.
        nodes = {"n1": (0.0, 20.0), "n2": (0.0, 120.0)}
        ways = [("under", ["n1", "n2"], {"highway": "primary"})]
        _patch_roads(monkeypatch, nodes, ways)
        lay = _Layout([_airside()], anchor=(0.0, 0.0))
        assert RL.road_lane_exclusion_union(lay) is None
        deck_box = Polygon([(60.0, -30.0), (80.0, -30.0),
                            (80.0, 30.0), (60.0, 30.0)])
        corridor = RL.road_lane_exclusion_union(
            lay, extra_seed_geometries=[deck_box])
        assert corridor is not None
        assert corridor.contains(Point(30.0, 0.0))      # road joined
        # The seed's own interior AWAY from the road is not corridor
        # (the road buffer crossing the box is, of course).
        assert not corridor.contains(Point(61.0, 25.0))

    def test_empty_extra_seeds_change_nothing(self, monkeypatch):
        nodes = {"n1": (0.0, 20.0), "n2": (0.0, 120.0)}
        _patch_roads(monkeypatch, nodes, [
            ("bore", ["n1", "n2"],
             {"highway": "primary", "tunnel": "yes"})])
        lay = _Layout([_airside()], anchor=(0.0, 0.0))
        a = RL.road_lane_exclusion_union(lay)
        b = RL.road_lane_exclusion_union(lay, extra_seed_geometries=[])
        assert a is not None and b is not None
        assert a.equals(b)
