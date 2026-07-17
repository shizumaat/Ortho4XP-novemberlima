"""Unit tests for the shared depressed-public-road exclusion corridor
(road_lanes.py, round-8 rework 2026-07-16).

The corridor is now built PHASE-INDEPENDENTLY from the tile's mapped big
roads, scoped to the airport's built geometry, seeded on the mapped
``tunnel=yes`` bores and the emitted approach OBB lanes, grown along the
connected road chain, and vertically sanity-trimmed.  These tests cover:

  * OBB-lane seeds (a tunnel-ramp / bridge-approach piece);
  * mapped ``tunnel=yes`` bore seeds (the PRE-SOLVE fix: a corridor with NO
    emitted ramp yet);
  * connected-chain growth of a surface continuation past a bore (the
    between-crossings reach fix), and its scoping / connectivity bounds;
  * the vertical-sanity trim of an at-grade grown segment; and
  * ``bridge=yes`` ways being dropped, plus the no-seed -> ``None`` rule.
"""
import types

from shapely.geometry import Point, Polygon

from auto_patch import road_lanes as RL


def _shape(role, polygon, ref=None, altitude=None, altitude_low=None):
    return types.SimpleNamespace(role=role, ref=ref, polygon=polygon,
                                 altitude=altitude, altitude_low=altitude_low)


class _Layout:
    """Minimal layout: shapes + anchor + an identity ``ll_to_m`` so a fake
    OSM node stored as ``(lat, lon) = (y, x)`` maps back to local ``(x, y)``."""

    def __init__(self, shapes, anchor=None):
        self.shapes = shapes
        self.anchor = anchor

    def ll_to_m(self, lat, lon):
        return (lon, lat)


def _patch_roads(monkeypatch, nodes, ways):
    import auto_patch.osm_load as OL
    monkeypatch.setattr(OL, "_load_osm_big_roads",
                        lambda lat, lon, *a, **k: (nodes, ways))


# A generous airside pad so the 120 m relevance buffer is not the binding
# constraint in the growth tests (roads run along y=0, x up to ~260).
def _airside():
    return _shape("junction",
                  Polygon([(-40, -40), (240, -40), (240, 40), (-40, 40)]))


class TestSeeds:
    def test_none_without_any_seed(self, monkeypatch):
        # A plain SURFACE road near airside is not a depression anchor.
        nodes = {"n1": (0.0, 0.0), "n2": (0.0, 100.0)}
        _patch_roads(monkeypatch, nodes,
                     [("r1", ["n1", "n2"], {"highway": "primary"})])
        lay = _Layout([_airside()], anchor=(0.0, 0.0))
        assert RL.road_lane_exclusion_union(lay) is None

    def test_none_when_roadless_and_rampless(self):
        lay = _Layout([_airside()], anchor=(0.0, 0.0))
        assert RL.road_lane_exclusion_union(lay) is None

    def test_obb_lane_seed_covers_ramp_axis(self):
        # 40 m x 8 m ramp: lane = axis buffered to width/2 + 2 = 6 m.
        ramp = Polygon([(0.0, -4.0), (40.0, -4.0), (40.0, 4.0), (0.0, 4.0)])
        union = RL.road_lane_exclusion_union(
            _Layout([_shape("tunnel_ramp", ramp)], anchor=(0.0, 0.0)))
        assert union is not None
        assert union.contains(Point(20.0, 5.0))        # 5 m off axis: inside
        assert not union.contains(Point(20.0, 9.0))    # 9 m off axis: outside
        assert union.contains(Point(44.0, 0.0))        # round cap past end

    def test_object_bridge_approach_is_a_seed(self):
        appr = Polygon([(0.0, -10.0), (50.0, -10.0), (50.0, 10.0), (0.0, 10.0)])
        lay = _Layout([_shape("tunnel_ramp", appr, ref="object_bridge_approach")],
                      anchor=(0.0, 0.0))
        assert RL.road_lane_exclusion_union(lay) is not None


class TestMappedBoreSeed:
    """The PRE-SOLVE fix: a mapped ``tunnel=yes`` bore seeds the corridor
    with NO emitted ramp piece present."""

    def test_tunnel_bore_seeds_corridor(self, monkeypatch):
        nodes = {"n1": (0.0, 0.0), "n2": (0.0, 50.0)}
        _patch_roads(monkeypatch, nodes, [(
            "b1", ["n1", "n2"],
            {"highway": "primary", "tunnel": "yes", "lanes": "4"})])
        union = RL.road_lane_exclusion_union(_Layout([_airside()],
                                                     anchor=(0.0, 0.0)))
        assert union is not None
        assert union.contains(Point(25.0, 0.0))         # on the bore

    def test_bridge_way_is_never_included(self, monkeypatch):
        # A bridge=yes way is elevated; even alongside a bore it is dropped.
        nodes = {"n1": (0.0, 0.0), "n2": (0.0, 50.0),
                 "b1": (30.0, 0.0), "b2": (30.0, 50.0)}
        _patch_roads(monkeypatch, nodes, [
            ("bore", ["n1", "n2"], {"highway": "primary", "tunnel": "yes"}),
            ("br", ["b1", "b2"], {"highway": "primary", "bridge": "yes"}),
        ])
        union = RL.road_lane_exclusion_union(_Layout([_airside()],
                                                     anchor=(0.0, 0.0)))
        assert union is not None
        assert union.contains(Point(25.0, 0.0))          # bore in
        assert not union.contains(Point(25.0, 30.0))     # bridge NOT in


class TestGrowthAndScoping:
    """A bore seed + a connected surface continuation grows the corridor
    past the bore (the between-crossings reach fix), bounded by connectivity
    and by the airside-relevance scope."""

    def _bore_plus_surface(self, monkeypatch, surface_end=260.0):
        nodes = {"n1": (0.0, 0.0), "n2": (0.0, 50.0),
                 "n3": (0.0, surface_end)}
        _patch_roads(monkeypatch, nodes, [
            ("bore", ["n1", "n2"],
             {"highway": "primary", "tunnel": "yes", "lanes": "4"}),
            ("surf", ["n2", "n3"], {"highway": "primary", "lanes": "4"}),
        ])

    def test_surface_continuation_grows_from_bore(self, monkeypatch):
        self._bore_plus_surface(monkeypatch)
        union = RL.road_lane_exclusion_union(_Layout([_airside()],
                                                     anchor=(0.0, 0.0)))
        assert union is not None
        assert union.contains(Point(25.0, 0.0))          # bore
        assert union.contains(Point(120.0, 0.0))         # grown continuation

    def test_growth_bounded_by_relevance_scope(self, monkeypatch):
        # Surface runs to x=400 but airside ends at x=240 -> relevance clips
        # the corridor near x=360 (240 + 120 m buffer); x=390 is outside.
        self._bore_plus_surface(monkeypatch, surface_end=400.0)
        union = RL.road_lane_exclusion_union(_Layout([_airside()],
                                                     anchor=(0.0, 0.0)))
        assert union is not None
        assert union.contains(Point(200.0, 0.0))         # within relevance
        assert not union.contains(Point(390.0, 0.0))     # beyond relevance

    def test_disconnected_surface_road_is_not_grown(self, monkeypatch):
        # A parallel surface road 30 m off the bore never connects (> tol).
        nodes = {"n1": (0.0, 0.0), "n2": (0.0, 50.0),
                 "p1": (30.0, 0.0), "p2": (30.0, 200.0)}
        _patch_roads(monkeypatch, nodes, [
            ("bore", ["n1", "n2"], {"highway": "primary", "tunnel": "yes"}),
            ("par", ["p1", "p2"], {"highway": "primary", "lanes": "4"}),
        ])
        union = RL.road_lane_exclusion_union(_Layout([_airside()],
                                                     anchor=(0.0, 0.0)))
        assert union is not None
        assert union.contains(Point(25.0, 0.0))          # bore in
        assert not union.contains(Point(100.0, 30.0))    # parallel NOT grown

    def test_far_tunnel_road_scoped_out(self, monkeypatch):
        # A bore 500 m from the airside pad is beyond the relevance scope
        # and seeds nothing.
        nodes = {"n1": (500.0, 0.0), "n2": (500.0, 50.0)}
        _patch_roads(monkeypatch, nodes, [(
            "bore", ["n1", "n2"], {"highway": "primary", "tunnel": "yes"})])
        union = RL.road_lane_exclusion_union(_Layout([_airside()],
                                                     anchor=(0.0, 0.0)))
        assert union is None


class TestVerticalSanity:
    """A grown SURFACE segment is trimmed where the road runs AT the local
    terrain grade, and kept where it is depressed below it."""

    def _layout_with_ramp_grade(self, monkeypatch, ramp_low):
        # OBB-seed ramp at x[0,40] carrying the road's local grade, plus a
        # connected mapped surface continuation to x=200.
        ramp = Polygon([(0.0, -4.0), (40.0, -4.0), (40.0, 4.0), (0.0, 4.0)])
        nodes = {"n2": (0.0, 40.0), "n3": (0.0, 200.0)}
        _patch_roads(monkeypatch, nodes,
                     [("surf", ["n2", "n3"], {"highway": "primary",
                                              "lanes": "4"})])
        return _Layout([_airside(),
                        _shape("tunnel_ramp", ramp, altitude_low=ramp_low)],
                       anchor=(0.0, 0.0))

    def test_at_grade_surface_is_trimmed(self, monkeypatch):
        lay = self._layout_with_ramp_grade(monkeypatch, ramp_low=100.0)
        # DEM ~ road grade (0.5 m above) everywhere -> not depressed -> trim.
        union = RL.road_lane_exclusion_union(lay, sample_dem=lambda x, y: 100.5)
        assert union is not None
        assert union.contains(Point(20.0, 0.0))          # OBB seed kept
        assert not union.contains(Point(120.0, 0.0))     # grown surface trimmed

    def test_depressed_surface_is_kept(self, monkeypatch):
        lay = self._layout_with_ramp_grade(monkeypatch, ramp_low=100.0)
        # DEM 6 m above the road grade -> depressed -> keep.
        union = RL.road_lane_exclusion_union(lay, sample_dem=lambda x, y: 106.0)
        assert union is not None
        assert union.contains(Point(120.0, 0.0))         # grown surface kept

    def test_no_dem_keeps_grown_segment(self, monkeypatch):
        # Pre-solve (no sample_dem): a bore-anchored grown segment is kept.
        lay = self._layout_with_ramp_grade(monkeypatch, ramp_low=100.0)
        union = RL.road_lane_exclusion_union(lay)
        assert union is not None
        assert union.contains(Point(120.0, 0.0))
