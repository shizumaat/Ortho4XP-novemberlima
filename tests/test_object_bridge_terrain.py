"""Workstream W-B tests — object-derived bridge terrain (feature B of
``docs/object_terrain_features_spec.md``, stage 1: assembler, gate
replacement, DECK_CARRIED corridor re-source, TERRAIN/PROFILE_CARRIED
suppression, gate-off neutrality).

Fixtures are synthetic (ruling R6): :class:`BridgeStructure` records and a
minimal fake layout / DEM / DSF road network are built in code — no
third-party pack content enters the repository.  The tests drive the
DECISION logic (corridor floor, deck datum, contract partition, road
sourcing, suppression, gate off) rather than the full mesh so they stay
deterministic and independent of a scenery install.
"""

from __future__ import annotations

import math
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from shapely.geometry import Polygon  # noqa: E402

from auto_patch import bridges  # noqa: E402
from auto_patch import config  # noqa: E402
from auto_patch import object_terrain_assembly as assembly  # noqa: E402
from auto_patch.obj8_reader import local_offset_to_lonlat  # noqa: E402
from auto_patch.object_terrain_features import (  # noqa: E402
    BridgeStructure,
    DECK_CARRIED,
    TERRAIN_CARRIED,
    PROFILE_CARRIED,
    AMBIGUOUS,
    DECK_HARDNESS_HARD_DECK,
    DECK_HARDNESS_HARD,
    DECK_HARDNESS_COSMETIC,
)
from auto_patch.dsf_road_network import (  # noqa: E402
    RoadNetwork,
    RoadSegment,
    RoadShapePoint,
)

# A KBNA-ish anchor; the structure frame origin is the same point, so the
# frame→lon/lat→meter round trip is (numerically) the identity and frame
# coordinates read directly as local metres in assertions.
ANCHOR_LATITUDE = 36.124
ANCHOR_LONGITUDE = -86.678
ANCHOR = (ANCHOR_LATITUDE, ANCHOR_LONGITUDE)


@pytest.fixture(autouse=True)
def sandbox_ortho4xp_data_root(tmp_path, monkeypatch):
    """USER RULING 2026-07-15 moved the sidecar caches under the
    Ortho4XP data root (``Airport_mod_cache/<pack>/``).  In a source
    checkout the data root resolves to the current working directory, so
    without this pin any test that exercises the classification /
    road-network cache paths would write ``Airport_mod_cache/`` into the
    repository.  Sandbox every test in this module
    (``ORTHO4XP_DATA_ROOT`` wins ``O4_File_Names.resolve_data_root``)."""
    monkeypatch.setenv("ORTHO4XP_DATA_ROOT",
                       str(tmp_path / "o4_data_root"))


# ---------------------------------------------------------------------------
# synthetic fixtures
# ---------------------------------------------------------------------------

class _FakeDem:
    """A flat DEM: ``alt`` returns a constant for any tile-frame point."""

    def __init__(self, elevation_m: float) -> None:
        self.elevation_m = elevation_m
        self.nodata = -32768

    def alt(self, _xy) -> float:
        return self.elevation_m


class _LateralRidgeDem:
    """A DEM that drops off LATERALLY (north/south) from a flat crest.

    The KBNA 02C portals sit on a hill that carries the runway over the
    tunnel body but falls away to either SIDE of the object — so the
    portal's back rides the crest while its flanks stand over lower
    ground.  ``alt`` holds ``crest_m`` along the tunnel axis (north ~ 0)
    and subtracts ``falloff_m_per_m`` per metre of |north|, giving the
    collar's exposed flank rim a demonstrably lower ground to feather
    into than its crown.  Frame: ``xy`` is ``(lon - tile_lon,
    lat - tile_lat)`` (see ``_sample_dem``); tile is (36, -87)."""

    nodata = -32768

    def __init__(self, crest_m: float = 180.0,
                 falloff_m_per_m: float = 0.30) -> None:
        self.crest_m = crest_m
        self.falloff = falloff_m_per_m

    def alt(self, xy) -> float:
        longitude = xy[0] + (-87.0)
        latitude = xy[1] + 36.0
        north_m = (latitude - ANCHOR_LATITUDE) * 111132.0
        return self.crest_m - self.falloff * abs(north_m)


class _FakeLayout:
    """Minimal layout stand-in: anchor, shapes, and the projection /
    canonical-registry surface the pin writers and the solver seeding
    touch (stage 2)."""

    def __init__(self) -> None:
        self.anchor = ANCHOR
        self.shapes: list = []
        self.icao = "TEST"
        self._to_meters, self._meters_to_lat_lon = (
            bridges._local_meter_projections(ANCHOR)
        )
        from auto_patch.canonical_points import CanonicalPointRegistry
        self.canonical_points = CanonicalPointRegistry()

    def ll_to_m(self, latitude: float, longitude: float):
        return self._to_meters(longitude, latitude)

    def m_to_ll(self, x: float, y: float):
        return self._meters_to_lat_lon(x, y)


def _deck_rectangle_frame(
    length_m: float = 131.0, half_width_m: float = 27.5
) -> Polygon:
    """A deck footprint in the structure frame: a rectangle from x=0 to
    x=length along the axis, centred on z=0."""
    return Polygon(
        [
            (0.0, -half_width_m),
            (length_m, -half_width_m),
            (length_m, half_width_m),
            (0.0, half_width_m),
        ]
    )


def _bridge(
    *,
    contract: str = DECK_CARRIED,
    deck_hardness: str = DECK_HARDNESS_HARD_DECK,
    hard_deck: bool = True,
    deck_top_y_m: float = 5.99,
    clearance_underside_y_m: float | None = 4.2,
    ceiling_y_m: float | None = 4.8,
    absolute_deck_elevation_m: float | None = 167.0,
    length_m: float = 131.0,
    resource: str = "Objects/Bridges/taxiway_L.obj",
) -> BridgeStructure:
    deck_polygon = _deck_rectangle_frame(length_m=length_m)
    return BridgeStructure(
        object_resources=[resource],
        anchor_longitude_latitude=(ANCHOR_LONGITUDE, ANCHOR_LATITUDE),
        frame_origin_longitude_latitude=(ANCHOR_LONGITUDE, ANCHOR_LATITUDE),
        heading_degrees=0.0,
        deck_polygon=deck_polygon,
        deck_top_profile=[(0.0, deck_top_y_m), (length_m, deck_top_y_m)],
        deck_top_y_m=deck_top_y_m,
        deck_end_elevations_y_m=(deck_top_y_m, deck_top_y_m),
        deck_length_m=length_m,
        deck_width_m=55.0,
        ceiling_y_m=ceiling_y_m,
        clearance_underside_y_m=clearance_underside_y_m,
        abutment_lines=[
            ((0.0, -27.5), (0.0, 27.5)),
            ((length_m, -27.5), (length_m, 27.5)),
        ],
        abutment_reaches_grade=(True, True),
        contract=contract,
        absolute_deck_elevation_m=absolute_deck_elevation_m,
        hard_deck=hard_deck,
        deck_hardness=deck_hardness,
    )


class _Classification:
    """Just enough of ``ClassificationResult`` for the emitter."""

    def __init__(self, bridges_list) -> None:
        self.bridges = list(bridges_list)
        self.tunnels: list = []
        self.exclusions: list = []
        self.refusals: list = []


def _draped_road_network_across_deck(length_m: float = 131.0) -> RoadNetwork:
    """A single fully-draped (level 0) road segment crossing UNDER the
    deck perpendicular to its axis (the physical reality — Donelson Pike
    crosses the taxiway-L deck width and exits through the LONG sides,
    clear of the causeway zones off the short ends): x = length/2, z
    from -80 to +80 in the structure frame."""
    shape_points = []
    for across in (-80.0, 0.0, 80.0):
        latitude, longitude = local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, length_m / 2.0, across
        )
        shape_points.append(
            RoadShapePoint(longitude, latitude, 0.0, True)
        )
    segment = RoadSegment(
        network_definition_index=0,
        network_definition_path="lib/g10/roads_EU.net",
        road_subtype=20,
        start_junction_id=1,
        end_junction_id=2,
        shape_points=shape_points,
    )
    return RoadNetwork(
        network_definitions=["lib/g10/roads_EU.net"],
        segments=[segment],
        skipped_line_count=0,
    )


# ---------------------------------------------------------------------------
# pure decision helpers
# ---------------------------------------------------------------------------

class TestCorridorFloor:
    def test_deck_carried_floor_from_msl_and_girder(self):
        # Amendment A10: the floor is GEOMETRY-DRIVEN — absolute deck
        # elevation minus the hard-deck height above anchor terrain (the
        # anchor-terrain datum), never clearance-driven.
        bridge = _bridge()  # deck 167.0, top +5.99, girder +4.2
        floor = bridges._bridge_corridor_floor_m(bridge, 167.0)
        assert floor == pytest.approx(167.0 - 5.99, abs=1e-6)

    def test_kbna_calibration_regression_a10(self):
        # The A10 three-way calibration numbers: deck 167.0, floor ~161.0
        # (author mesh 161.0), girder underside 165.21, clearance 4.2 —
        # at or above the acceptance bound, never below.
        bridge = _bridge()
        floor = bridges._bridge_corridor_floor_m(bridge, 167.0)
        assert floor == pytest.approx(161.01, abs=0.01)
        girder = bridges._bridge_girder_underside_m(bridge, 167.0)
        assert girder == pytest.approx(165.21, abs=1e-6)
        clearance = girder - floor
        assert clearance == pytest.approx(4.2, abs=1e-6)
        assert clearance >= config.BRIDGE_ROAD_CLEARANCE_MINIMUM_M - 1e-9

    def test_floor_ignores_underside_planes(self):
        # Geometry-driven: the same deck height gives the same floor with
        # or without underside data (the clearance is a check, not the
        # driver).
        with_girder = bridges._bridge_corridor_floor_m(_bridge(), 167.0)
        without_girder = bridges._bridge_corridor_floor_m(
            _bridge(clearance_underside_y_m=None, ceiling_y_m=None), 167.0
        )
        assert with_girder == pytest.approx(without_girder)

    def test_girder_underside_fallbacks(self):
        # clearance_underside preferred, ceiling as fallback, None when
        # the object exposes no underside plane at all.
        assert bridges._bridge_girder_underside_m(
            _bridge(), 167.0
        ) == pytest.approx(167.0 - (5.99 - 4.2), abs=1e-6)
        assert bridges._bridge_girder_underside_m(
            _bridge(clearance_underside_y_m=None, ceiling_y_m=4.8), 167.0
        ) == pytest.approx(167.0 - (5.99 - 4.8), abs=1e-6)
        assert bridges._bridge_girder_underside_m(
            _bridge(clearance_underside_y_m=None, ceiling_y_m=None), 167.0
        ) is None


class TestDeckElevation:
    def test_absolute_msl_wins(self):
        bridge = _bridge(absolute_deck_elevation_m=167.0)
        elevation = bridges._bridge_deck_elevation_m(
            bridge, _FakeDem(90.0), 36, -87
        )
        assert elevation == pytest.approx(167.0)

    def test_datum_at_anchor_plus_crest_when_no_msl(self):
        bridge = _bridge(absolute_deck_elevation_m=None, deck_top_y_m=6.0)
        elevation = bridges._bridge_deck_elevation_m(
            bridge, _FakeDem(100.0), 36, -87
        )
        assert elevation == pytest.approx(106.0)

    def test_none_when_dem_unavailable(self):
        bridge = _bridge(absolute_deck_elevation_m=None)
        assert bridges._bridge_deck_elevation_m(bridge, None, 36, -87) is None


class TestContractPartition:
    def test_deck_carried_is_a_corridor(self):
        corridor, suppress, refused, _road_carried, _portals = bridges._partition_bridges_for_corridors(
            _Classification([_bridge(contract=DECK_CARRIED)])
        )
        assert len(corridor) == 1 and not suppress and not refused

    def test_terrain_and_profile_are_suppressed(self):
        classification = _Classification([
            _bridge(contract=TERRAIN_CARRIED, deck_top_y_m=0.0,
                    absolute_deck_elevation_m=None),
            _bridge(contract=PROFILE_CARRIED, deck_hardness=DECK_HARDNESS_HARD,
                    hard_deck=False),
        ])
        corridor, suppress, refused, _road_carried, _portals = bridges._partition_bridges_for_corridors(
            classification
        )
        assert not corridor and len(suppress) == 2 and not refused

    def test_ambiguous_is_refused(self):
        corridor, suppress, refused, _road_carried, _portals = bridges._partition_bridges_for_corridors(
            _Classification([_bridge(contract=AMBIGUOUS)])
        )
        assert not corridor and not suppress and len(refused) == 1

    def test_cosmetic_deck_is_a_corridor_regardless_of_contract(self):
        # Murfreesboro class: no hard deck, trucks ride the terrain, so the
        # causeway-plus-corridor is mandatory even if coverage read as
        # terrain-carried.
        classification = _Classification([
            _bridge(contract=TERRAIN_CARRIED,
                    deck_hardness=DECK_HARDNESS_COSMETIC, hard_deck=False),
        ])
        corridor, suppress, refused, _road_carried, _portals = bridges._partition_bridges_for_corridors(
            classification
        )
        assert len(corridor) == 1 and not suppress


# ---------------------------------------------------------------------------
# object-sourced corridor emission
# ---------------------------------------------------------------------------


def _deck_route_shape() -> "BuiltShape":
    """A junction rect crossing the deck footprint (x 0..131, y ∓27.5 in
    layout meters) so the stage-2b road-carried discriminator reads the
    span as a taxi/truck bridge, not a road overpass."""
    from auto_patch.layout import BuiltShape as _BuiltShape
    from auto_patch.layout import ROLE_JUNCTION as _ROLE_JUNCTION
    return _BuiltShape(
        polygon=Polygon([(40.0, -3.0), (90.0, -3.0), (90.0, 3.0),
                         (40.0, 3.0)]),
        role=_ROLE_JUNCTION, ref="DECK-ROUTE",
    )


class TestObjectSourcedCorridors:
    def test_deck_carried_emits_corridor_at_object_floor(self):
        layout = _FakeLayout()
        layout.shapes.append(_deck_route_shape())
        classification = _Classification([_bridge()])
        network = _draped_road_network_across_deck()
        count, suppression, covered = (
            bridges._emit_object_sourced_bridge_corridors(
                layout, _FakeDem(150.0), 36, -87, classification, [network],
                road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
            )
        )
        assert count == 1
        assert not suppression
        assert len(covered) == 1
        # R12: the corridor emitter owns only the road APPROACHES — the
        # trench is born pre-solve by ``build_bridge_layout_shapes``.
        assert any(
            s.ref == "object_bridge_approach" for s in layout.shapes
        )
        assert not any(
            s.ref == "object_bridge_corridor" for s in layout.shapes
        )

    def test_terrain_carried_suppresses_and_emits_no_corridor(self):
        layout = _FakeLayout()
        classification = _Classification([
            _bridge(contract=TERRAIN_CARRIED, deck_top_y_m=0.0,
                    clearance_underside_y_m=None, ceiling_y_m=None,
                    absolute_deck_elevation_m=None),
        ])
        network = _draped_road_network_across_deck()
        count, suppression, covered = (
            bridges._emit_object_sourced_bridge_corridors(
                layout, _FakeDem(150.0), 36, -87, classification, [network],
                road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
            )
        )
        assert count == 0
        assert len(suppression) == 1
        assert not covered
        assert not layout.shapes

    def test_dsf_draped_road_is_preferred_over_openstreetmap(self, monkeypatch):
        # If the DSF network supplies a draped road, the OSM fallback must
        # never be consulted.
        def _boom(*args, **kwargs):
            raise AssertionError("OSM fallback should not be reached")

        monkeypatch.setattr(bridges, "_load_underpass_osm_road_lines", _boom)
        layout = _FakeLayout()
        layout.shapes.append(_deck_route_shape())
        classification = _Classification([_bridge()])
        network = _draped_road_network_across_deck()
        count, _suppression, _covered = (
            bridges._emit_object_sourced_bridge_corridors(
                layout, _FakeDem(150.0), 36, -87, classification, [network],
                road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
            )
        )
        assert count == 1

    def test_elevated_dsf_road_is_ignored_falls_back_to_osm(self, monkeypatch):
        # A level-1 (elevated) segment is not draped, so it must not source a
        # corridor; with no OSM road either, nothing is emitted.
        calls = {"osm": 0}

        def _no_osm(_layout, _to_meters):
            calls["osm"] += 1
            return []

        monkeypatch.setattr(
            bridges, "_load_underpass_osm_road_lines", _no_osm
        )
        elevated_points = []
        for along in (-40.0, 65.0, 171.0):
            latitude, longitude = local_offset_to_lonlat(
                ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, along, 0.0
            )
            elevated_points.append(
                RoadShapePoint(longitude, latitude, 1.0, False)
            )
        elevated = RoadNetwork(
            network_definitions=[""],
            segments=[RoadSegment(0, "", 60, 1, 2, elevated_points)],
            skipped_line_count=0,
        )
        layout = _FakeLayout()
        layout.shapes.append(_deck_route_shape())
        classification = _Classification([_bridge()])
        count, _s, _c = bridges._emit_object_sourced_bridge_corridors(
            layout, _FakeDem(150.0), 36, -87, classification, [elevated],
            road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
        )
        assert count == 0
        assert calls["osm"] == 1  # OSM fallback was consulted once


# ---------------------------------------------------------------------------
# portal OUTWARD ramp width (user ruling 2026-07-15, KBNA 02C)
# ---------------------------------------------------------------------------

class TestMappedOsmCarriagewayWidth:
    """``_mapped_osm_carriageway_width_m`` re-associates the portal
    footprint to the tagged big-road ways by geometry (the draped/OSM
    lines the corridor walks carry no tags)."""

    def _cross_road(self, tags):
        """OSM ``(nodes, ways)`` with one road crossing a footprint
        centred on the layout origin (x=0, y from -50 to +50 m)."""
        _to_m, m_to_ll = bridges._local_meter_projections(ANCHOR)
        lat0, lon0 = m_to_ll(0.0, -50.0)
        lat1, lon1 = m_to_ll(0.0, 50.0)
        nodes = {"n0": (lat0, lon0), "n1": (lat1, lon1)}
        ways = [("w0", ["n0", "n1"], dict(tags))]
        return nodes, ways

    def test_lanes_derive_carriageway_width(self, monkeypatch):
        from auto_patch import pipeline
        layout = _FakeLayout()
        to_meters, _m = bridges._local_meter_projections(ANCHOR)
        footprint = Polygon([(-30, -30), (30, -30), (30, 30), (-30, 30)])
        monkeypatch.setattr(
            pipeline, "_load_osm_big_roads",
            lambda _a, _b: self._cross_road(
                {"highway": "primary", "lanes": "6"}))
        width = bridges._mapped_osm_carriageway_width_m(
            layout, footprint, to_meters)
        assert width == pytest.approx(21.0)  # 6 × LANE_WIDTH_M (3.5)

    def test_no_road_near_footprint_returns_none(self, monkeypatch):
        from auto_patch import pipeline
        layout = _FakeLayout()
        to_meters, m_to_ll = bridges._local_meter_projections(ANCHOR)
        # A footprint 1 km away from the crossing road.
        footprint = Polygon([(970, -30), (1030, -30),
                             (1030, 30), (970, 30)])
        monkeypatch.setattr(
            pipeline, "_load_osm_big_roads",
            lambda _a, _b: self._cross_road(
                {"highway": "primary", "lanes": "6"}))
        assert bridges._mapped_osm_carriageway_width_m(
            layout, footprint, to_meters) is None

    def test_non_carriageway_type_ignored(self, monkeypatch):
        from auto_patch import pipeline
        layout = _FakeLayout()
        to_meters, _m = bridges._local_meter_projections(ANCHOR)
        footprint = Polygon([(-30, -30), (30, -30), (30, 30), (-30, 30)])
        # A footway has no carriageway table entry and no width/lanes.
        monkeypatch.setattr(
            pipeline, "_load_osm_big_roads",
            lambda _a, _b: self._cross_road({"highway": "footway"}))
        assert bridges._mapped_osm_carriageway_width_m(
            layout, footprint, to_meters) is None

    def test_missing_cache_returns_none(self, monkeypatch):
        from auto_patch import pipeline
        layout = _FakeLayout()
        to_meters, _m = bridges._local_meter_projections(ANCHOR)
        footprint = Polygon([(-30, -30), (30, -30), (30, 30), (-30, 30)])
        monkeypatch.setattr(
            pipeline, "_load_osm_big_roads", lambda _a, _b: ({}, []))
        assert bridges._mapped_osm_carriageway_width_m(
            layout, footprint, to_meters) is None


class TestPortalOutwardRampWidth:
    """The resolution ORDER (user ruling 2026-07-15): mapped OSM
    carriageway + shoulder → classified deck-face width → mouth-face,
    always capped at the mouth-face width."""

    def _portal(self, deck_width_m):
        import dataclasses
        bridge = dataclasses.replace(_bridge(), deck_width_m=deck_width_m)
        return {"bridge": bridge,
                "footprint": Polygon([(-5, -5), (5, -5), (5, 5), (-5, 5)])}

    def _resolve(self, deck_width_m, mouth_face_width_m):
        layout = _FakeLayout()
        to_meters, _m = bridges._local_meter_projections(ANCHOR)
        portal = self._portal(deck_width_m)
        return bridges._portal_outward_ramp_width_m(
            layout, portal, portal["footprint"], to_meters,
            mouth_face_width_m)

    def test_mapped_osm_wins_with_shoulder(self, monkeypatch):
        monkeypatch.setattr(
            bridges, "_mapped_osm_carriageway_width_m", lambda *a: 21.0)
        width, provenance = self._resolve(
            deck_width_m=17.0, mouth_face_width_m=84.0)
        assert width == pytest.approx(
            21.0 + bridges.PORTAL_RAMP_SHOULDER_MARGIN_M)
        assert "mapped OSM" in provenance

    def test_deck_face_when_no_mapped_road(self, monkeypatch):
        monkeypatch.setattr(
            bridges, "_mapped_osm_carriageway_width_m", lambda *a: None)
        width, provenance = self._resolve(
            deck_width_m=17.0, mouth_face_width_m=84.0)
        assert width == pytest.approx(17.0)
        assert "deck-face" in provenance

    def test_mouth_face_last_resort(self, monkeypatch):
        monkeypatch.setattr(
            bridges, "_mapped_osm_carriageway_width_m", lambda *a: None)
        width, provenance = self._resolve(
            deck_width_m=0.0, mouth_face_width_m=30.0)
        assert width == pytest.approx(30.0)
        assert "mouth-face" in provenance

    def test_mapped_width_capped_at_mouth_face(self, monkeypatch):
        monkeypatch.setattr(
            bridges, "_mapped_osm_carriageway_width_m", lambda *a: 100.0)
        width, provenance = self._resolve(
            deck_width_m=17.0, mouth_face_width_m=30.0)
        assert width == pytest.approx(30.0)
        assert "capped" in provenance

    def test_deck_face_capped_at_mouth_face(self, monkeypatch):
        monkeypatch.setattr(
            bridges, "_mapped_osm_carriageway_width_m", lambda *a: None)
        width, provenance = self._resolve(
            deck_width_m=200.0, mouth_face_width_m=30.0)
        assert width == pytest.approx(30.0)
        assert "capped" in provenance


# ---------------------------------------------------------------------------
# gate-off neutrality
# ---------------------------------------------------------------------------

class TestGateOff:
    def test_attach_is_noop_with_gate_off(self, monkeypatch):
        # Default config gate is off; the assembler attaches nothing.
        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", False)
        layout = _FakeLayout()
        layout.apt_dat_path = "/nonexistent/Earth nav data/apt.dat"
        result = assembly.attach_bridge_classification(layout, "/nonexistent")
        assert result is None
        assert not hasattr(layout, assembly.CLASSIFICATION_ATTRIBUTE)

    def test_classification_reader_ignores_attribute_when_gate_off(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", False)
        layout = _FakeLayout()
        setattr(
            layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
            _Classification([_bridge()]),
        )
        # Gate off: the reader returns None even though the attribute is
        # present, so the emitters take the legacy path.
        assert bridges._object_bridge_classification(layout) is None

    def test_classification_reader_honours_attribute_when_gate_on(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", True)
        layout = _FakeLayout()
        classification = _Classification([_bridge()])
        setattr(
            layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
            classification,
        )
        assert bridges._object_bridge_classification(layout) is classification

    def test_scenery_grep_replaced_by_classifier_when_gate_on(
        self, monkeypatch
    ):
        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", True)
        layout = _FakeLayout()
        # A classifier that found a bridge ⇒ pack carries its own 3D bridge.
        setattr(
            layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
            _Classification([_bridge()]),
        )
        assert bridges._scenery_has_bridge_objects(layout) is True
        # A classifier that found no bridge ⇒ no scenery bridge object.
        setattr(
            layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
            _Classification([]),
        )
        assert bridges._scenery_has_bridge_objects(layout) is False


# ---------------------------------------------------------------------------
# assembler tile-path helper
# ---------------------------------------------------------------------------

class TestAssemblerHelpers:
    def test_tile_dsf_path_naming(self):
        path = assembly._tile_dsf_path("/x/Earth nav data", 36, -87)
        assert path == os.path.join(
            "/x/Earth nav data", "+30-090", "+36-087.dsf"
        )

    def test_tile_dsf_path_positive_hemisphere(self):
        path = assembly._tile_dsf_path("/x/Earth nav data", 51, 0)
        assert path == os.path.join(
            "/x/Earth nav data", "+50+000", "+51+000.dsf"
        )


# ---------------------------------------------------------------------------
# ruling R4 — Phase 2 y-bake exclusion wiring
# ---------------------------------------------------------------------------

_BRIDGE_RESOURCE = "Objects/Bridges/taxiway_L.obj"
_OTHER_RESOURCE = "Objects/Other/shed.obj"

_SYNTHETIC_DSF_LINES = [
    f"OBJECT_DEF {_BRIDGE_RESOURCE}",
    f"OBJECT_DEF {_OTHER_RESOURCE}",
    "OBJECT 0 -86.678000 36.124000 108.0",
    "OBJECT 1 -86.679000 36.125000 0.0",
]

_R4_REASON_FRAGMENT = "excluded from the Phase 2 y-bake (ruling R4)"


class TestExclusionWiringR4:
    def _run_discover(self, tmp_path, monkeypatch, excluded_resources):
        """Drive ``discover_and_rebake_airport`` against a synthetic DSF
        dump (loader monkeypatched; resources unresolvable on purpose, so
        discovery ends after the R4 filter — exactly the surface under
        test)."""
        from auto_patch import post_mesh
        from auto_patch import dsf_reader

        monkeypatch.setattr(
            dsf_reader, "_load_dsf_text",
            lambda _path: list(_SYNTHETIC_DSF_LINES),
        )
        pack_root = str(tmp_path / "SomePack")
        os.makedirs(pack_root, exist_ok=True)
        result = post_mesh.discover_and_rebake_airport(
            str(tmp_path / "fake.dsf"),
            str(tmp_path / "fake_mesh.mesh"),
            pack_root,
            None,
            excluded_resources=excluded_resources,
        )
        return result, pack_root

    def test_excluded_resource_dropped_and_reported(
        self, tmp_path, monkeypatch
    ):
        pack_root = str(tmp_path / "SomePack")
        result, pack_root = self._run_discover(
            tmp_path, monkeypatch,
            excluded_resources={(pack_root, _BRIDGE_RESOURCE)},
        )
        r4_skips = [
            (resource, reason)
            for resource, reason in result["skipped"]
            if _R4_REASON_FRAGMENT in reason
        ]
        assert len(r4_skips) == 1, (
            f"expected exactly one R4 skip, got {result['skipped']}"
        )
        assert r4_skips[0][0] == _BRIDGE_RESOURCE
        # The non-excluded resource was NOT R4-skipped (it proceeds into
        # discovery; here it silently fails resolution, which produces no
        # skip entry) and nothing was baked.
        assert all(
            resource != _OTHER_RESOURCE for resource, _ in result["skipped"]
        )
        assert result["structures_baked"] == 0
        assert result["objects_written"] == []

    def test_all_placements_excluded_returns_early_with_reports(
        self, tmp_path, monkeypatch
    ):
        pack_root = str(tmp_path / "SomePack")
        result, pack_root = self._run_discover(
            tmp_path, monkeypatch,
            excluded_resources={
                (pack_root, _BRIDGE_RESOURCE),
                (pack_root, _OTHER_RESOURCE),
            },
        )
        r4_skipped_resources = sorted(
            resource
            for resource, reason in result["skipped"]
            if _R4_REASON_FRAGMENT in reason
        )
        assert r4_skipped_resources == sorted(
            [_BRIDGE_RESOURCE, _OTHER_RESOURCE]
        )
        assert result["objects_written"] == []
        assert result["structures_baked"] == 0

    def test_no_exclusions_is_the_pre_change_behaviour(
        self, tmp_path, monkeypatch
    ):
        result, _pack_root = self._run_discover(
            tmp_path, monkeypatch, excluded_resources=None
        )
        assert not any(
            _R4_REASON_FRAGMENT in reason
            for _resource, reason in result["skipped"]
        )

    def test_exclusion_set_gate_off_reads_nothing(self, monkeypatch):
        from auto_patch import dsf_reader

        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", False)

        def _explode(_path):
            raise AssertionError("gate off must not read the DSF")

        monkeypatch.setattr(dsf_reader, "_load_dsf_text", _explode)
        assert assembly.exclusion_set_for_dsf(
            "/anywhere/fake.dsf", None
        ) == set()

    def test_exclusion_set_gate_on_returns_classifier_exclusions(
        self, tmp_path, monkeypatch
    ):
        from auto_patch import dsf_reader
        from auto_patch import object_terrain_features as otf_module

        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", True)
        dsf_path = tmp_path / "fake.dsf"
        dsf_path.write_bytes(b"")
        monkeypatch.setattr(
            dsf_reader, "_load_dsf_text",
            lambda _path: list(_SYNTHETIC_DSF_LINES),
        )
        monkeypatch.setattr(
            assembly, "_load_object_geometry_by_resource",
            lambda _placements, _pack_root, _xplane_root: {
                _BRIDGE_RESOURCE: object()
            },
        )
        expected_exclusions = [("PACK", _BRIDGE_RESOURCE)]

        class _FakeResult:
            exclusions = expected_exclusions

        def _fake_classify(placements, geometry_by_resource, **kwargs):
            # Post-mesh classification runs without pavement and with the
            # caller-supplied pack root (key-match with the filter side).
            assert (
                kwargs.get("pavement_polygons_longitude_latitude") is None
            )
            assert kwargs.get("pack_root") == "PACK"
            assert placements  # the synthetic placements arrived
            return _FakeResult()

        monkeypatch.setattr(
            otf_module,
            "classify_object_terrain_features",
            _fake_classify,
        )
        assert assembly.exclusion_set_for_dsf(
            str(dsf_path), None, pack_root="PACK"
        ) == {("PACK", _BRIDGE_RESOURCE)}

    def _cache_harness(self, tmp_path, monkeypatch, classify_calls):
        """Gate on, synthetic DSF lines, a REAL pack_root directory (so
        ``airport_mod_cache_dir`` resolves) and a counting classifier."""
        from auto_patch import dsf_reader
        from auto_patch import object_terrain_features as otf_module

        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", True)
        pack_root = tmp_path / "Fake Pack"
        pack_root.mkdir()
        dsf_path = pack_root / "fake.dsf"
        dsf_path.write_bytes(b"")
        monkeypatch.setattr(
            dsf_reader, "_load_dsf_text",
            lambda _path: list(_SYNTHETIC_DSF_LINES),
        )
        geometry_holder = {"payload": ("geometry", 1)}
        monkeypatch.setattr(
            assembly, "_load_object_geometry_by_resource",
            lambda _placements, _pack_root, _xplane_root: {
                _BRIDGE_RESOURCE: geometry_holder["payload"]
            },
        )

        class _FakeResult:
            exclusions = [(str(pack_root), _BRIDGE_RESOURCE)]

        def _counting_classify(placements, geometry_by_resource, **kwargs):
            classify_calls.append(1)
            return _FakeResult()

        monkeypatch.setattr(
            otf_module,
            "classify_object_terrain_features",
            _counting_classify,
        )
        return dsf_path, pack_root, geometry_holder

    def test_exclusion_set_second_call_served_from_sidecar_cache(
        self, tmp_path, monkeypatch
    ):
        """The R4 exclusion set is pure pack content (2026-07-15 profile:
        recomputed 46 s per mesh build at KBNA) — call two must hit the
        content-hash sidecar, and changed geometry content must miss."""
        classify_calls = []
        dsf_path, pack_root, geometry_holder = self._cache_harness(
            tmp_path, monkeypatch, classify_calls
        )
        expected = {(str(pack_root), _BRIDGE_RESOURCE)}

        assert assembly.exclusion_set_for_dsf(
            str(dsf_path), None, pack_root=str(pack_root)
        ) == expected
        assert assembly.exclusion_set_for_dsf(
            str(dsf_path), None, pack_root=str(pack_root)
        ) == expected
        assert len(classify_calls) == 1  # second call from the sidecar

        geometry_holder["payload"] = ("geometry", 2)  # content changed
        assert assembly.exclusion_set_for_dsf(
            str(dsf_path), None, pack_root=str(pack_root)
        ) == expected
        assert len(classify_calls) == 2

    def test_exclusion_cache_disabled_by_environment_flag(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("O4_OBJECT_EXCLUSION_CACHE", "0")
        classify_calls = []
        dsf_path, pack_root, _geometry_holder = self._cache_harness(
            tmp_path, monkeypatch, classify_calls
        )
        for _ in range(2):
            assembly.exclusion_set_for_dsf(
                str(dsf_path), None, pack_root=str(pack_root)
            )
        assert len(classify_calls) == 2


# ---------------------------------------------------------------------------
# stage 2 — bridge laws (grade_law lockstep source)
# ---------------------------------------------------------------------------

from auto_patch import grade_law  # noqa: E402
from auto_patch.layout import (  # noqa: E402
    BuiltShape,
    ROLE_JUNCTION,
    vertex_bucket,
)


class TestBridgeLaws:
    def test_deck_end_pin_elevation_kbna(self):
        # KBNA: datum = 167.0 − 5.99; flat deck end at +5.99 → pin 167.0.
        datum = 167.0 - 5.99
        assert grade_law.bridge_deck_end_pin_elevation_m(
            datum, 5.99
        ) == pytest.approx(167.0)

    def test_profile_pin_interpolates_and_clamps(self):
        profile = [(5.0, 0.0), (15.0, 2.0), (25.0, 6.0)]
        # Interior: linear between bins.
        assert grade_law.bridge_profile_pin_elevation_m(
            100.0, profile, 10.0
        ) == pytest.approx(101.0)
        assert grade_law.bridge_profile_pin_elevation_m(
            100.0, profile, 20.0
        ) == pytest.approx(104.0)
        # Clamped outside the sampled range.
        assert grade_law.bridge_profile_pin_elevation_m(
            100.0, profile, -50.0
        ) == pytest.approx(100.0)
        assert grade_law.bridge_profile_pin_elevation_m(
            100.0, profile, 500.0
        ) == pytest.approx(106.0)
        # Empty profile degrades to the datum.
        assert grade_law.bridge_profile_pin_elevation_m(
            100.0, [], 10.0
        ) == pytest.approx(100.0)

    def test_crossing_floor_law(self):
        floor = grade_law.bridge_crossing_floor_m(100.0, 2.3)
        assert floor == pytest.approx(
            100.0 + config.BRIDGE_ROAD_CLEARANCE_M + 2.3
        )
        # Negative thickness (degenerate object) never LOWERS the floor.
        assert grade_law.bridge_crossing_floor_m(
            100.0, -3.0
        ) == pytest.approx(100.0 + config.BRIDGE_ROAD_CLEARANCE_M)


# ---------------------------------------------------------------------------
# stage 2 — deck-end pin insertion (seam-anchor idiom)
# ---------------------------------------------------------------------------

def _junction_rect_across_start_abutment() -> BuiltShape:
    """A junction rect straddling the bridge's START abutment line.  The
    frame start abutment runs x=0, z −27.5..27.5; in layout meters that
    is the segment x≈0, y ∓27.5.  This rect spans x −30..30, y −5..5, so
    the extended abutment line cuts its two long edges at (0, ±5).

    Carries warm-start ``node_altitudes`` (the post-seam-pipeline state
    of a junction) so the pin writer's node-altitude stamping path — the
    solver's fallback and the validator's read — is exercised; a shape
    with NO altitude representation still gets registry pins (the solver
    reads those directly) but nothing to stamp."""
    polygon = Polygon([(-30.0, -5.0), (30.0, -5.0), (30.0, 5.0),
                       (-30.0, 5.0)])
    return BuiltShape(polygon=polygon, role=ROLE_JUNCTION, ref="J1",
                      node_altitudes=[150.0] * 4 + [150.0])


def _gate_on_layout_with_bridge(monkeypatch, bridge) -> _FakeLayout:
    monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", True)
    layout = _FakeLayout()
    setattr(
        layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
        _Classification([bridge]),
    )
    return layout


class TestDeckEndPins:
    def test_ring_vertices_inserted_and_pinned_at_deck_end(
        self, monkeypatch
    ):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_junction_rect_across_start_abutment())
        pinned = bridges.insert_bridge_deck_end_pins(layout, None, 36, -87)
        assert pinned >= 2, "both long-edge crossings must be pinned"
        # The ring gained the two crossing vertices at x≈0.
        ring = list(layout.shapes[0].polygon.exterior.coords)[:-1]
        crossing_vertices = [
            (x, y) for x, y in ring if abs(x) < 0.01 and abs(abs(y) - 5.0) < 0.1
        ]
        assert len(crossing_vertices) == 2
        # The pin registry carries the KBNA deck-end value (MSL 167.0).
        pin_values = getattr(layout, "_object_bridge_pin_values")
        assert pin_values, "pin registry must be populated"
        for value in pin_values.values():
            assert value == pytest.approx(167.0, abs=0.01)
        # node_altitudes at the pinned vertices carry the pin value.
        node_altitudes = layout.shapes[0].node_altitudes
        assert node_altitudes is not None
        pinned_alts = [
            node_altitudes[index]
            for index, (x, y) in enumerate(ring)
            if abs(x) < 0.01 and abs(abs(y) - 5.0) < 0.1
        ]
        assert pinned_alts and all(
            a == pytest.approx(167.0, abs=0.01) for a in pinned_alts
        )

    def test_gate_off_inserts_nothing(self, monkeypatch):
        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", False)
        layout = _FakeLayout()
        setattr(
            layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
            _Classification([_bridge()]),
        )
        layout.shapes.append(_junction_rect_across_start_abutment())
        assert bridges.insert_bridge_deck_end_pins(
            layout, None, 36, -87
        ) == 0
        assert not hasattr(layout, "_object_bridge_pin_values")
        assert len(
            list(layout.shapes[0].polygon.exterior.coords)
        ) == 5  # untouched 4-corner ring

    def test_no_ring_crossing_logs_and_pins_zero(self, monkeypatch):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        # A rect far away from both abutments.
        far_polygon = Polygon([(500.0, 500.0), (560.0, 500.0),
                               (560.0, 510.0), (500.0, 510.0)])
        layout.shapes.append(
            BuiltShape(polygon=far_polygon, role=ROLE_JUNCTION, ref="FAR")
        )
        assert bridges.insert_bridge_deck_end_pins(
            layout, None, 36, -87
        ) == 0


# ---------------------------------------------------------------------------
# stage 2 — solver seeding honours the bridge pin registry
# ---------------------------------------------------------------------------

class TestSolverBridgePins:
    def test_seed_elevations_hard_pins_bridge_buckets(self):
        from auto_patch.elevation_per_surface.solver_primitives import (
            _seed_elevations,
        )
        layout = _FakeLayout()
        corners = [(-30.0, -5.0), (30.0, -5.0), (30.0, 5.0), (-30.0, 5.0)]
        polygon = Polygon(corners)
        layout.shapes.append(BuiltShape(
            polygon=polygon, role=ROLE_JUNCTION, ref="J1",
            node_altitudes=[150.0] * 4 + [150.0],
        ))
        # Pin ONE corner via the bridge registry.
        layout._object_bridge_pin_values = {
            vertex_bucket(-30.0, -5.0): 167.0
        }
        nodes = list(corners)
        bucket_to_idx = {
            layout.canonical_points.get_or_add(x, y): index
            for index, (x, y) in enumerate(corners)
        }
        elev, is_hard, have_initial = _seed_elevations(
            layout, nodes, bucket_to_idx, dem=None,
            tile_lat=36, tile_lon=-87,
        )
        assert is_hard[0] is True
        assert elev[0] == pytest.approx(167.0)
        assert have_initial[0] is True
        # Other corners stay soft (warm-started at 150).
        assert not any(is_hard[1:])
        # Pinned indices are protected like seam pins.
        assert 0 in getattr(layout, "_seam_pin_idx")

    def test_seed_elevations_without_registry_is_untouched(self):
        from auto_patch.elevation_per_surface.solver_primitives import (
            _seed_elevations,
        )
        layout = _FakeLayout()
        corners = [(-30.0, -5.0), (30.0, -5.0), (30.0, 5.0), (-30.0, 5.0)]
        layout.shapes.append(BuiltShape(
            polygon=Polygon(corners), role=ROLE_JUNCTION, ref="J1",
            node_altitudes=[150.0] * 4 + [150.0],
        ))
        nodes = list(corners)
        bucket_to_idx = {
            layout.canonical_points.get_or_add(x, y): index
            for index, (x, y) in enumerate(corners)
        }
        elev, is_hard, _have_initial = _seed_elevations(
            layout, nodes, bucket_to_idx, dem=None,
            tile_lat=36, tile_lon=-87,
        )
        assert not any(is_hard)
        assert not hasattr(layout, "_seam_pin_idx")


# ---------------------------------------------------------------------------
# stage 2 — crossing floor producer + validator lockstep
# ---------------------------------------------------------------------------

def _elevated_terrain_carried_bridge() -> BridgeStructure:
    """The EDDF-elevated-span class: pavement-continuous (TERRAIN_CARRIED)
    with a crest well above grade and a girder underside — the crossing
    must rise over the draped road beneath."""
    return _bridge(
        contract=TERRAIN_CARRIED,
        deck_top_y_m=6.5,
        clearance_underside_y_m=4.2,
        ceiling_y_m=4.8,
        absolute_deck_elevation_m=None,
        deck_hardness=DECK_HARDNESS_HARD,
        hard_deck=False,
    )


class TestCrossingFloor:
    def _floors(self, monkeypatch, bridge, nodes, dem):
        layout = _gate_on_layout_with_bridge(monkeypatch, bridge)
        setattr(
            layout, bridges._OBJECT_BRIDGE_ROAD_NETWORKS_ATTRIBUTE,
            [_draped_road_network_across_deck()],
        )
        return layout, bridges.bridge_crossing_floor_nodes(
            layout, nodes, dem, 36, -87
        )

    def test_floor_applied_inside_footprint(self, monkeypatch):
        inside_node = (65.0, 0.0)
        outside_node = (500.0, 500.0)
        _layout, floors = self._floors(
            monkeypatch, _elevated_terrain_carried_bridge(),
            [inside_node, outside_node], _FakeDem(100.0),
        )
        # floor = road (DEM 100) + clearance 5.1 + thickness (6.5 − 4.2).
        expected = 100.0 + config.BRIDGE_ROAD_CLEARANCE_M + (6.5 - 4.2)
        assert 0 in floors and floors[0] == pytest.approx(expected)
        assert 1 not in floors

    def test_flush_deck_gets_restraint_not_floor(self, monkeypatch):
        flush = _bridge(
            contract=TERRAIN_CARRIED, deck_top_y_m=0.0,
            clearance_underside_y_m=None, ceiling_y_m=None,
            absolute_deck_elevation_m=None,
            deck_hardness=DECK_HARDNESS_HARD, hard_deck=False,
        )
        _layout, floors = self._floors(
            monkeypatch, flush, [(65.0, 0.0)], _FakeDem(100.0)
        )
        assert floors == {}

    def test_deck_carried_gets_no_crossing_floor(self, monkeypatch):
        _layout, floors = self._floors(
            monkeypatch, _bridge(), [(65.0, 0.0)], _FakeDem(100.0)
        )
        assert floors == {}

    def test_validator_agrees_with_producer(self, monkeypatch):
        bridge = _elevated_terrain_carried_bridge()
        layout = _gate_on_layout_with_bridge(monkeypatch, bridge)
        setattr(
            layout, bridges._OBJECT_BRIDGE_ROAD_NETWORKS_ATTRIBUTE,
            [_draped_road_network_across_deck()],
        )
        expected_floor = (
            100.0 + config.BRIDGE_ROAD_CLEARANCE_M + (6.5 - 4.2)
        )
        # A pavement rect inside the footprint SOLVED BELOW the floor.
        low_polygon = Polygon([(50.0, -4.0), (80.0, -4.0), (80.0, 4.0),
                               (50.0, 4.0)])
        layout.shapes.append(BuiltShape(
            polygon=low_polygon, role=ROLE_JUNCTION, ref="LOW",
            altitude=100.0,
        ))
        from auto_patch import verification
        findings = verification.check_bridge_crossing_floor(
            layout, _FakeDem(100.0), 36, -87
        )
        assert findings, "a node below the floor must be reported"
        kind, _reference, below, _tolerance, _location = findings[0]
        assert kind == "crossing_floor"
        assert below == pytest.approx(expected_floor - 100.0, abs=0.01)
        # Raise the pavement to the floor: the validator goes quiet.
        layout.shapes[-1] = BuiltShape(
            polygon=low_polygon, role=ROLE_JUNCTION, ref="OK",
            altitude=expected_floor,
        )
        assert verification.check_bridge_crossing_floor(
            layout, _FakeDem(100.0), 36, -87
        ) == []

    def test_validators_gate_off_return_empty(self, monkeypatch):
        from auto_patch import verification
        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", False)
        layout = _FakeLayout()
        assert verification.check_bridge_crossing_floor(
            layout, None, 36, -87
        ) == []
        assert verification.check_bridge_deck_end_pins(
            layout, None, 36, -87
        ) == []


class TestDeckEndPinValidator:
    def test_solved_at_law_value_passes_perturbed_fails(self, monkeypatch):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_junction_rect_across_start_abutment())
        pinned = bridges.insert_bridge_deck_end_pins(layout, None, 36, -87)
        assert pinned >= 2
        from auto_patch import verification
        # The writer stamped node_altitudes at the pin value, simulating
        # a solve that held the pins: the validator agrees.
        assert verification.check_bridge_deck_end_pins(
            layout, None, 36, -87
        ) == []
        # Perturb one pinned vertex: exactly that deviation is reported.
        shape = layout.shapes[0]
        ring = list(shape.polygon.exterior.coords)[:-1]
        node_altitudes = list(shape.node_altitudes[:len(ring)])
        for index, (x, y) in enumerate(ring):
            if abs(x) < 0.01 and abs(abs(y) - 5.0) < 0.1:
                node_altitudes[index] = 165.0  # 2 m below the law value
                break
        shape.node_altitudes = node_altitudes + [node_altitudes[0]]
        findings = verification.check_bridge_deck_end_pins(
            layout, None, 36, -87
        )
        assert len(findings) == 1
        kind, reference, deviation, tolerance, _location = findings[0]
        assert kind == "deck_end_pin"
        assert "end0" in reference or "end1" in reference
        assert deviation == pytest.approx(2.0, abs=0.02)
        assert tolerance == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# stage 2b — capture band, causeway plates, road-carried, deconflict order
# ---------------------------------------------------------------------------

def _kbna_gap_rect(gap_m: float = 9.6) -> BuiltShape:
    """The measured KBNA geometry: a junction rect whose edge stops
    ``gap_m`` short of the START abutment line (x = 0), on the approach
    side (negative x).  KBNA taxiway-L measures 9.62-9.69 m at both
    ends."""
    polygon = Polygon([(-40.0, -5.0), (-gap_m, -5.0), (-gap_m, 5.0),
                       (-40.0, 5.0)])
    return BuiltShape(polygon=polygon, role=ROLE_JUNCTION, ref="APPROACH",
                      node_altitudes=[150.0] * 4 + [150.0])


class TestCaptureBand:
    def test_kbna_gap_vertices_pinned_at_deck_end(self, monkeypatch):
        # The stage-2b defect fixture: pavement cut 9.6 m short of the
        # abutment captured ZERO pins under the old 0.25 m tolerance;
        # the measured 12 m band must pin the two facing edge vertices.
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_kbna_gap_rect())
        pinned = bridges.insert_bridge_deck_end_pins(layout, None, 36, -87)
        assert pinned == 2
        pin_values = getattr(layout, "_object_bridge_pin_values")
        assert len(pin_values) == 2
        for value in pin_values.values():
            assert value == pytest.approx(167.0, abs=0.01)
        # The pinned vertices are the gap-facing edge pair at x = -9.6.
        ring = list(layout.shapes[-1].polygon.exterior.coords)[:-1]
        node_altitudes = layout.shapes[-1].node_altitudes
        pinned_positions = [
            ring[i] for i in range(len(ring))
            if node_altitudes[i] == pytest.approx(167.0, abs=0.01)
        ]
        assert len(pinned_positions) == 2
        assert all(abs(x + 9.6) < 0.01 for x, _y in pinned_positions)

    def test_vertex_beyond_band_not_pinned(self, monkeypatch):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_kbna_gap_rect(gap_m=15.0))  # beyond 12 m
        pinned = bridges.insert_bridge_deck_end_pins(layout, None, 36, -87)
        assert pinned == 0


class TestCausewayPlates:
    def test_gap_plate_flat_at_deck_end_value(self, monkeypatch):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_kbna_gap_rect())
        _trench, emitted, _pads = bridges.build_bridge_layout_shapes(
            layout, None, 36, -87)
        assert emitted == 2  # one plate per end
        from auto_patch.layout import ROLE_BRIDGE_CAUSEWAY
        plates = [s for s in layout.shapes
                  if s.ref == "object_bridge_causeway"]
        assert len(plates) == 2
        for plate in plates:
            assert plate.role == ROLE_BRIDGE_CAUSEWAY
            assert plate.node_altitudes is not None
            assert all(a == pytest.approx(167.0, abs=0.01)
                       for a in plate.node_altitudes)
        # The start-end plate spans the measured gap (9.6 m + 2 m weld
        # overlap) and the overlap is CLIPPED by the pavement (ruling
        # R2 — pavement wins at contact): zero residual intersection,
        # and the dirt-gap midpoint is covered at the deck-end value.
        start_plate = min(plates, key=lambda s: s.polygon.centroid.x)
        minimum_x = min(x for x, _y in start_plate.polygon.exterior.coords)
        assert minimum_x == pytest.approx(-11.6, abs=0.5)
        pavement = _kbna_gap_rect().polygon
        assert start_plate.polygon.intersection(pavement).area < 1e-6
        from shapely.geometry import Point as _Point
        assert start_plate.polygon.covers(_Point(-4.8, 0.0))

    def test_no_pavement_plate_capped_at_named_constant(self, monkeypatch):
        # Murfreesboro class: no pavement anywhere near — the plate runs
        # the full capped length back from the lip.
        bridge = _bridge(absolute_deck_elevation_m=None, deck_top_y_m=7.76)
        layout = _gate_on_layout_with_bridge(monkeypatch, bridge)
        # A truck route crossing the DECK keeps it out of road-carried,
        # but sits entirely inside the footprint (no pavement behind
        # either abutment).
        from auto_patch.layout import ROLE_SERVICE_ROAD
        layout.shapes.append(BuiltShape(
            polygon=Polygon([(40.0, -2.0), (90.0, -2.0), (90.0, 2.0),
                             (40.0, 2.0)]),
            role=ROLE_SERVICE_ROAD, ref="TRUCK",
        ))
        _trench, emitted, _pads = bridges.build_bridge_layout_shapes(
            layout, _FakeDem(180.66), 36, -87)
        assert emitted == 2
        plates = [s for s in layout.shapes
                  if s.ref == "object_bridge_causeway"]
        expected = 180.66 + 7.76  # datum + deck end (flat deck fixture)
        for plate in plates:
            assert plate.node_altitudes is not None
            assert all(a == pytest.approx(expected, abs=0.01)
                       for a in plate.node_altitudes)
        start_plate = min(plates, key=lambda s: s.polygon.centroid.x)
        minimum_x = min(x for x, _y in start_plate.polygon.exterior.coords)
        assert minimum_x == pytest.approx(
            -config.BRIDGE_CAUSEWAY_MAX_LENGTH_M, abs=1.0)

    def test_gate_off_emits_nothing(self, monkeypatch):
        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", False)
        layout = _FakeLayout()
        setattr(layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
                _Classification([_bridge()]))
        assert bridges.build_bridge_layout_shapes(
            layout, None, 36, -87) == (0, 0, 0)
        assert not layout.shapes


class TestDeckLipWeldStrips:
    """Deck-lip weld strips (user directive 2026-07-15): the resumed
    pavement must slightly OVERLAP the deck-elevation terrain at every
    pavement-facing rim of a hard deck — coplanar (both pinned at the
    deck-top profile law value), so aircraft taxi smoothly onto the
    deck with no raw-mesh sliver diving to the trench at the lip."""

    @staticmethod
    def _crossing_junction() -> BuiltShape:
        """A junction crossing the deck box (x 0..131, y ∓27.5) through
        both LONG sides — the R8 hard-deck cut splits it into two
        resumed-pavement pieces whose cut edges lie exactly on the box
        boundary (the KBNA taxiway-L geometry class)."""
        return BuiltShape(
            polygon=Polygon([(40.0, -45.0), (90.0, -45.0),
                             (90.0, 45.0), (40.0, 45.0)]),
            role=ROLE_JUNCTION, ref="XING",
            node_altitudes=[150.0] * 4 + [150.0])

    def test_strips_overlap_resumed_pavement_coplanar(self, monkeypatch):
        from shapely.geometry import LineString
        from shapely.ops import unary_union

        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(self._crossing_junction())
        bridges.build_bridge_layout_shapes(layout, None, 36, -87)

        strips = [shape for shape in layout.shapes
                  if shape.ref == "object_bridge_deck_weld"]
        assert strips, "deck-lip weld strips must be emitted"
        # Flat KBNA-class profile: every strip vertex at the deck value.
        for shape in strips:
            assert all(value == pytest.approx(167.0, abs=0.01)
                       for value in shape.node_altitudes)

        pavement = [shape for shape in layout.shapes
                    if shape.role == ROLE_JUNCTION]
        assert len(pavement) == 2, "R8 must split the crossing junction"
        strip_union = unary_union([shape.polygon for shape in strips])
        # The pavement cut edges (y = ±27.5) lie INSIDE the strip union
        # with >= 0.2 m ring depth: the overlap the owner asked for.
        eroded = strip_union.buffer(-0.19)
        assert eroded.covers(LineString([(41.0, 27.5), (89.0, 27.5)]))
        assert eroded.covers(LineString([(41.0, -27.5), (89.0, -27.5)]))
        overlap_area = sum(
            shape.polygon.intersection(strip_union).area
            for shape in pavement)
        assert overlap_area > 5.0, "the strips must genuinely overlap"

        # Fronting pavement ring vertices are pinned at the same law
        # value — the overlap is coplanar.
        for shape in pavement:
            ring = list(shape.polygon.exterior.coords)[:-1]
            fronting = [
                shape.node_altitudes[index]
                for index, (x, y) in enumerate(ring)
                if abs(abs(y) - 27.5) < 0.45 and 39.9 < x < 90.1
            ]
            assert fronting, "cut-edge vertices must exist"
            assert all(value == pytest.approx(167.0, abs=0.01)
                       for value in fronting)
        pin_values = getattr(layout, "_object_bridge_pin_values")
        assert any(value == pytest.approx(167.0, abs=0.01)
                   for value in pin_values.values())

        # R2 node-split wall: the strips never come within the 0.5 m
        # node-interning tolerance of the trench plate.
        trench = [shape for shape in layout.shapes
                  if shape.ref == "object_bridge_corridor"]
        assert trench
        assert strip_union.distance(trench[0].polygon) > 0.5

    def test_cosmetic_deck_emits_no_strips(self, monkeypatch):
        # Cosmetic decks keep their pavement over the box (R2 pavement
        # wins) — there is no lip cut and nothing to weld.
        bridge = _bridge(deck_hardness=DECK_HARDNESS_COSMETIC,
                         hard_deck=False)
        layout = _gate_on_layout_with_bridge(monkeypatch, bridge)
        layout.shapes.append(self._crossing_junction())
        bridges.build_bridge_layout_shapes(layout, _FakeDem(161.0), 36, -87)
        assert not [shape for shape in layout.shapes
                    if shape.ref == "object_bridge_deck_weld"]

    def test_pavement_beyond_reach_gets_no_strips(self, monkeypatch):
        # The KBNA gap-rect class (pavement 9.6 m short of the abutment)
        # is causeway territory, not lip-weld territory.
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_kbna_gap_rect())
        bridges.build_bridge_layout_shapes(layout, None, 36, -87)
        assert not [shape for shape in layout.shapes
                    if shape.ref == "object_bridge_deck_weld"]


class TestRoadCarriedOverpass:
    def test_no_route_on_deck_is_road_carried(self, monkeypatch):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        # No shape crosses the deck footprint at all.
        corridor, _s, _r, road_carried, _portals = (
            bridges._partition_bridges_for_corridors(
                _Classification([_bridge()]), layout)
        )
        assert not corridor and len(road_carried) == 1

    def test_road_carried_gets_no_pins_no_causeway_no_corridor(
        self, monkeypatch
    ):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        assert bridges.insert_bridge_deck_end_pins(
            layout, None, 36, -87) == 0
        assert bridges.build_bridge_layout_shapes(
            layout, None, 36, -87) == (0, 0, 0)
        count, _sup, covered = bridges._emit_object_sourced_bridge_corridors(
            layout, _FakeDem(150.0), 36, -87,
            _Classification([_bridge()]),
            [_draped_road_network_across_deck()],
            road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
        )
        assert count == 0 and not covered
        assert not [s for s in layout.shapes
                    if (s.ref or "").startswith("object_bridge")]

    def test_route_on_deck_is_not_road_carried(self, monkeypatch):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        corridor, _s, _r, road_carried, _portals = (
            bridges._partition_bridges_for_corridors(
                _Classification([_bridge()]), layout)
        )
        assert len(corridor) == 1 and not road_carried


class TestDeconflictObjectSeniority:
    def test_object_corridor_survives_earlier_legacy_piece(self):
        # Stage-2b root cause: legacy portal pieces emitted EARLIER
        # covered the object plate and earlier-wins dropped it.  The
        # object-first walk order must keep the object plate and drop
        # the covered legacy piece instead — with no object refs the
        # order is the emission order (gate-off byte identity).
        from auto_patch import finalize
        from auto_patch.layout import ROLE_TUNNEL_RAMP
        area = Polygon([(0.0, 0.0), (30.0, 0.0), (30.0, 10.0), (0.0, 10.0)])
        layout = _FakeLayout()
        legacy = BuiltShape(polygon=area, role=ROLE_TUNNEL_RAMP,
                            ref="portal_ramp", altitude=163.0)
        object_plate = BuiltShape(polygon=Polygon(area.exterior.coords),
                                  role=ROLE_TUNNEL_RAMP,
                                  ref="object_bridge_corridor",
                                  altitude=161.0)
        layout.shapes.extend([legacy, object_plate])  # legacy FIRST
        finalize.deconflict_road_features(layout, "TEST")
        remaining = [s for s in layout.shapes
                     if s.role == ROLE_TUNNEL_RAMP]
        refs = {s.ref for s in remaining}
        assert "object_bridge_corridor" in refs
        assert "portal_ramp" not in refs


# ---------------------------------------------------------------------------
# stage 2b iteration 3 — routing-evidence discriminator + R8 flush seat
# ---------------------------------------------------------------------------

class _FakeRouteCenterline:
    """The two fields the discriminator reads off a
    ``apt_dat_reader.TaxiCenterline``: the polyline and the service flag."""

    def __init__(self, line, is_service=True):
        self.line = line
        self.is_service = is_service


class TestRoutingEvidenceDiscriminator:
    def test_truck_route_polyline_across_deck_defeats_road_carried(
        self, monkeypatch
    ):
        # The Murfreesboro reality: truck-strip SHAPES sit 36.7-60.9 m
        # short of the deck (no shape evidence), but the apt.dat 1206
        # ROUTE polyline crosses it — the routing graph is the primary
        # evidence, so the bridge stays in the truck-bridge class.
        from shapely.geometry import LineString as _LineString
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.apt_taxi_centerlines = [
            _FakeRouteCenterline(
                _LineString([(-80.0, 0.0), (210.0, 0.0)]), is_service=True)
        ]
        corridor, _s, _r, road_carried, _portals = (
            bridges._partition_bridges_for_corridors(
                _Classification([_bridge()]), layout)
        )
        assert len(corridor) == 1 and not road_carried

    def test_no_routing_and_no_shapes_is_road_carried(self, monkeypatch):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.apt_taxi_centerlines = [
            # A route far away (never near the deck) is not evidence.
            _FakeRouteCenterline(
                __import__("shapely.geometry", fromlist=["LineString"])
                .LineString([(4000.0, 4000.0), (4200.0, 4000.0)]))
        ]
        corridor, _s, _r, road_carried, _portals = (
            bridges._partition_bridges_for_corridors(
                _Classification([_bridge()]), layout)
        )
        assert not corridor and len(road_carried) == 1


class TestR8FlushSeat:
    def _emit(self, monkeypatch, bridge, spanning_shape):
        layout = _gate_on_layout_with_bridge(monkeypatch, bridge)
        layout.shapes.append(spanning_shape)
        n_trench, _n_causeway, _pads = bridges.build_bridge_layout_shapes(
            layout, _FakeDem(150.0), 36, -87)
        return layout, n_trench

    def test_hard_deck_cuts_spanning_pavement(self, monkeypatch):
        # A junction rect spanning the whole deck box (x -30..161 across
        # the 0..131 footprint) is CUT at the abutments (ruling R8) and
        # survives as two approach pieces; the trench plate exists.
        spanning = BuiltShape(
            polygon=Polygon([(-30.0, -5.0), (161.0, -5.0), (161.0, 5.0),
                             (-30.0, 5.0)]),
            role=ROLE_JUNCTION, ref="SPAN",
            node_altitudes=[167.0] * 4 + [167.0],
        )
        layout, count = self._emit(monkeypatch, _bridge(), spanning)
        assert count == 1
        pieces = [s for s in layout.shapes if s.ref == "SPAN"]
        assert len(pieces) == 2, "the deck cut must split the rect"
        for piece in pieces:
            maximum_reach = max(
                min(x for x, _y in piece.polygon.exterior.coords),
                -999.0,
            )
            # No piece extends into the footprint interior.
            assert piece.polygon.buffer(-0.05).intersection(
                Polygon([(0.0, -27.5), (131.0, -27.5), (131.0, 27.5),
                         (0.0, 27.5)])
            ).area < 1.0
            # Solved values survived the cut (resampled 167).
            assert piece.node_altitudes is not None
        from auto_patch.layout import ROLE_BRIDGE_TRENCH
        trench = [s for s in layout.shapes
                  if s.ref == "object_bridge_corridor"]
        assert len(trench) == 1
        assert trench[0].role == ROLE_BRIDGE_TRENCH
        assert trench[0].node_altitudes is not None
        assert len(trench[0].node_altitudes) > 50, "densified ring"
        assert all(a == pytest.approx(161.01, abs=0.05)
                   for a in trench[0].node_altitudes)

    def test_cosmetic_deck_keeps_pavement_and_carves_around(
        self, monkeypatch
    ):
        cosmetic = _bridge(
            deck_hardness=DECK_HARDNESS_COSMETIC, hard_deck=False,
            absolute_deck_elevation_m=None, deck_top_y_m=7.76,
        )
        spanning = BuiltShape(
            polygon=Polygon([(-30.0, -5.0), (161.0, -5.0), (161.0, 5.0),
                             (-30.0, 5.0)]),
            role=ROLE_JUNCTION, ref="SPAN",
            node_altitudes=[188.0] * 4 + [188.0],
        )
        layout, count = self._emit(monkeypatch, cosmetic, spanning)
        assert count == 1
        pieces = [s for s in layout.shapes if s.ref == "SPAN"]
        assert len(pieces) == 1, "cosmetic deck: pavement wins, no cut"
        assert pieces[0].polygon.area == pytest.approx(
            191.0 * 10.0, rel=1e-6)
        trench = [s for s in layout.shapes
                  if s.ref == "object_bridge_corridor"]
        assert len(trench) == 1
        # The trench carves AROUND the kept pavement (R2 pavement wins).
        assert trench[0].polygon.intersection(
            pieces[0].polygon).area < 1e-6


# ---------------------------------------------------------------------------
# ruling R12 — building-pad removal + by-construction pass immunity
# ---------------------------------------------------------------------------

class TestBridgeObjectBuildingPads:
    def test_pad_over_deck_removed_never_stack(self, monkeypatch):
        # The measured KBNA defect: Phase 1 turned the bridge OBJECTS
        # into building pads (building2 covered the taxiway-L footprint
        # 2959/2959 m2) — a pad mostly inside a classified footprint is
        # removed at layout time.
        from auto_patch.layout import ROLE_BUILDING
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        pad = BuiltShape(
            polygon=Polygon([(5.0, -25.0), (126.0, -25.0), (126.0, 25.0),
                             (5.0, 25.0)]),
            role=ROLE_BUILDING, ref="building2", altitude=167.0,
        )
        layout.shapes.append(pad)
        _t, _c, pads_removed = bridges.build_bridge_layout_shapes(
            layout, None, 36, -87)
        assert pads_removed == 1
        assert not any(s.ref == "building2" for s in layout.shapes)

    def test_unrelated_building_kept(self, monkeypatch):
        from auto_patch.layout import ROLE_BUILDING
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        far_pad = BuiltShape(
            polygon=Polygon([(900.0, 900.0), (960.0, 900.0),
                             (960.0, 960.0), (900.0, 960.0)]),
            role=ROLE_BUILDING, ref="terminal9", altitude=170.0,
        )
        layout.shapes.append(far_pad)
        _t, _c, pads_removed = bridges.build_bridge_layout_shapes(
            layout, None, 36, -87)
        assert pads_removed == 0
        assert any(s.ref == "terminal9" for s in layout.shapes)


class TestR12PassImmunityByConstruction:
    def test_roles_absent_from_every_mutating_role_set(self):
        from auto_patch.layout import (
            ROLE_BRIDGE_TRENCH, ROLE_BRIDGE_CAUSEWAY,
        )
        new_roles = {ROLE_BRIDGE_TRENCH, ROLE_BRIDGE_CAUSEWAY}
        # Solver: FIRST-CLASS GRAPH MEMBERS (user directive, round 8) —
        # ring vertices enter the canonical registry; immunity comes
        # from every vertex being a HARD PIN at the law value, not from
        # exclusion.
        from auto_patch.elevation_per_surface.solver_primitives import (
            PAVEMENT_ROLES,
        )
        assert new_roles <= set(PAVEMENT_ROLES)
        # Deconflict: not road features — never walked, never dropped.
        from auto_patch.layout import (
            ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
        )
        assert not (new_roles & {ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL})
        # Seam machinery: not split at tile seams as pavement.
        from auto_patch.seam_anchors import _SEAM_SPLIT_ROLES
        assert not (new_roles & set(_SEAM_SPLIT_ROLES))
        # Registered everywhere a first-class role must be.
        from auto_patch.layout import AEROWAY_FOR_ROLE
        from auto_patch.config import ROLE_GRADE_LIMITS
        for role in new_roles:
            assert role in AEROWAY_FOR_ROLE
            assert role in ROLE_GRADE_LIMITS
            assert ROLE_GRADE_LIMITS[role] is None  # flat by law

    def test_deconflict_ignores_bridge_plates(self):
        from auto_patch import finalize
        from auto_patch.layout import (
            ROLE_BRIDGE_TRENCH, ROLE_TUNNEL_RAMP, ROLE_JUNCTION,
        )
        area = Polygon([(0.0, 0.0), (30.0, 0.0), (30.0, 10.0), (0.0, 10.0)])
        layout = _FakeLayout()
        # Airside pavement covering the same area (the seed) + a trench
        # plate: deconflict must not touch the trench (not a road
        # feature), even though the seed fully covers it.
        layout.shapes.append(BuiltShape(
            polygon=Polygon(area.exterior.coords), role=ROLE_JUNCTION,
            ref="J", altitude=167.0))
        layout.shapes.append(BuiltShape(
            polygon=Polygon(area.exterior.coords), role=ROLE_BRIDGE_TRENCH,
            ref="object_bridge_corridor",
            node_altitudes=[161.0] * 5))
        # And a legacy portal piece that SHOULD be dropped (covered).
        layout.shapes.append(BuiltShape(
            polygon=Polygon(area.exterior.coords), role=ROLE_TUNNEL_RAMP,
            ref="portal_ramp", altitude=163.0))
        finalize.deconflict_road_features(layout, "TEST")
        refs = {s.ref for s in layout.shapes}
        assert "object_bridge_corridor" in refs
        assert "portal_ramp" not in refs


# ---------------------------------------------------------------------------
# stage 2b iteration 5 — approach keep-out + lip coverage geometry
# ---------------------------------------------------------------------------

class TestApproachKeepOut:
    def test_no_approach_rect_intrudes_into_the_deck_box(self, monkeypatch):
        from shapely.geometry import Polygon as _Polygon
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        count, _s, _c = bridges._emit_object_sourced_bridge_corridors(
            layout, _FakeDem(150.0), 36, -87,
            _Classification([_bridge()]),
            [_draped_road_network_across_deck()],
            road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
        )
        assert count == 1
        footprint = _Polygon([(0.0, -27.5), (131.0, -27.5),
                              (131.0, 27.5), (0.0, 27.5)])
        approaches = [s for s in layout.shapes
                      if s.ref == "object_bridge_approach"]
        assert approaches, "perpendicular road must produce approaches"
        for approach in approaches:
            assert approach.polygon.intersection(footprint).area <= 0.5, \
                "approach rect intrudes into the deck box"

    def test_axial_road_gets_an_exit_lane(self, monkeypatch):
        # Round 10 (KBNA audit 11 — the overturned round-3 rule): a road
        # exiting through the abutment ends must NOT be dammed by the
        # causeway.  The exit lane through the keep-out stays open, so
        # the approach walk emits its descending rects along the road,
        # and none of them overlaps the (cut) plates or the trench.
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        # Persistent routing evidence (raw apt.dat polyline): the R8 cut
        # removes the deck-spanning SHAPE, so the taxi-route line keeps
        # the bridge in the corridor class for the post-solve partition.
        from shapely.geometry import LineString as _LineString
        layout._object_bridge_route_lines = [
            _LineString([(65.0, -80.0), (65.0, 80.0)])]
        # Road networks are read from the layout cache in round 10 (the
        # builder and the keep-out both consult it).
        setattr(layout, bridges._OBJECT_BRIDGE_ROAD_NETWORKS_ATTRIBUTE,
                [_axial_road_network()])
        bridges.build_bridge_layout_shapes(layout, _FakeDem(150.0), 36, -87)
        bridges._emit_object_sourced_bridge_corridors(
            layout, _FakeDem(150.0), 36, -87,
            _Classification([_bridge()]), [_axial_road_network()],
            road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
        )
        approaches = [s for s in layout.shapes
                      if s.ref == "object_bridge_approach"]
        assert approaches, "the exit lane must let the walk through"
        plates = [s for s in layout.shapes
                  if (s.ref or "").startswith("object_bridge_c")]
        for approach in approaches:
            for plate in plates:
                assert approach.polygon.intersection(
                    plate.polygon).area < 0.5, \
                    "approach must not overlay a plate (cut, not overlay)"


def _axial_road_network(length_m: float = 131.0) -> RoadNetwork:
    """A fully-draped road ALONG the deck axis, exiting through both
    abutment ends (the measured KBNA Donelson Pike geometry, segments
    local x -64..+77)."""
    axis_points = []
    for along in (-80.0, length_m / 2.0, length_m + 80.0):
        latitude, longitude = local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, along, 0.0
        )
        axis_points.append(RoadShapePoint(longitude, latitude, 0.0, True))
    return RoadNetwork(
        network_definitions=["lib/g10/roads_EU.net"],
        segments=[RoadSegment(0, "lib/g10/roads_EU.net", 20, 1, 2,
                              axis_points)],
        skipped_line_count=0,
    )


class TestRoadExitCut:
    def test_axial_road_splits_each_causeway_into_flanks(
        self, monkeypatch
    ):
        # The author-mesh target shape (A10 / spec 2.2): the corridor
        # runs THROUGH the span and out both ends, flanked by fill at
        # deck-end elevation on BOTH sides.
        from shapely.geometry import LineString as _LineString
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        setattr(layout, bridges._OBJECT_BRIDGE_ROAD_NETWORKS_ATTRIBUTE,
                [_axial_road_network()])
        _t, n_causeway, _p = bridges.build_bridge_layout_shapes(
            layout, _FakeDem(150.0), 36, -87)
        assert n_causeway == 4, "two flank parts per end"
        plates = [s for s in layout.shapes
                  if s.ref == "object_bridge_causeway"]
        assert len(plates) == 4
        # The road centerline crosses NO plate.
        road = _LineString([(-80.0, 0.0), (211.0, 0.0)])
        for plate in plates:
            assert not plate.polygon.intersects(road.buffer(
                bridges._ROAD_EXIT_CUT_HALF_WIDTH_M - 0.1)), \
                "the exit lane must be clear of causeway fill"
            assert all(a == pytest.approx(167.0, abs=0.01)
                       for a in plate.node_altitudes)

    def test_perpendicular_road_keeps_whole_causeways(self, monkeypatch):
        # A road exiting through the LONG sides never crosses the
        # causeway plates: no cut, one part per end (the pre-round-10
        # shape preserved where physics does not demand a lane).
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        setattr(layout, bridges._OBJECT_BRIDGE_ROAD_NETWORKS_ATTRIBUTE,
                [_draped_road_network_across_deck()])
        _t, n_causeway, _p = bridges.build_bridge_layout_shapes(
            layout, _FakeDem(150.0), 36, -87)
        assert n_causeway == 2


class TestLipCoverageGeometry:
    def test_lip_line_samples_inside_the_causeway(self, monkeypatch):
        # Audit-5 regression: mesh samples exactly ON the abutment line
        # read the wall slope / raw terrain because the plate boundary
        # WAS the line (node wobble pushes samples off).  The plate now
        # overlaps the lip inward and past both corners: all 9 audit
        # sample positions (including t=0 and t=1) lie strictly INSIDE.
        from shapely.geometry import Point as _Point
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_kbna_gap_rect())
        bridges.build_bridge_layout_shapes(layout, None, 36, -87)
        plates = [s for s in layout.shapes
                  if s.ref == "object_bridge_causeway"]
        start_plate = min(plates, key=lambda s: s.polygon.centroid.x)
        # Start abutment line: x = 0, y -27.5..27.5 (frame south = -y).
        for i in range(9):
            t = i / 8.0
            sample = _Point(0.0, -27.5 + t * 55.0)
            assert start_plate.polygon.buffer(1e-9).contains(sample) or \
                start_plate.polygon.covers(sample), f"t={t} off-plate"
        # And strictly interior points 0.3 m inward of the lip.
        assert start_plate.polygon.covers(_Point(0.3, 0.0))

    def test_wall_gap_stays_above_weld_tolerance(self, monkeypatch):
        # Trench rim (inset 1.2) to causeway inner edge (lip - 0.6):
        # the node-split wall gap is 0.6 m > the 0.5 m weld tolerance.
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        bridges.build_bridge_layout_shapes(layout, None, 36, -87)
        trench = [s for s in layout.shapes
                  if s.ref == "object_bridge_corridor"][0]
        plates = [s for s in layout.shapes
                  if s.ref == "object_bridge_causeway"]
        from auto_patch.layout import SHARED_VERTEX_TOL_M
        for plate in plates:
            gap = trench.polygon.distance(plate.polygon)
            assert gap > SHARED_VERTEX_TOL_M + 0.05, \
                f"wall gap {gap:.2f} m would weld shut"


# ---------------------------------------------------------------------------
# ruling R4 breadth (round 6) — anchor-family sibling exclusion
# ---------------------------------------------------------------------------

class TestAnchorFamilyExclusions:
    def _placement(self, resource, longitude, latitude):
        from auto_patch.obj8_reader import ObjectPlacement
        return ObjectPlacement(
            definition_index=0, resource_path=resource,
            longitude=longitude, latitude=latitude, heading_degrees=0.0,
        )

    def test_same_anchor_siblings_join_the_exclusion_list(self):
        # The KBNA round-6 defect, synthesized: the classifier consumed
        # p1/p4/p5/p6; p2/p3 sit on the SAME anchor with no qualifying
        # faces and were re-baked build after build until their drifted
        # geometry moved the deck box.  A GPU cart 1.5 m away (the
        # MEASURED nearest foreign placement) must stay bakeable.
        class _Result:
            exclusions = [("PACK", f"Objects/B/p{i}.obj")
                          for i in (1, 4, 5, 6)]
        anchor_lon, anchor_lat = ANCHOR_LONGITUDE, ANCHOR_LATITUDE
        placements = [
            self._placement(f"Objects/B/p{i}.obj", anchor_lon, anchor_lat)
            for i in (1, 2, 3, 4, 5, 6)
        ]
        foreign_lon = anchor_lon + 1.5 / (
            111320.0 * __import__("math").cos(
                __import__("math").radians(anchor_lat))
        )
        placements.append(
            self._placement("Objects/Misc/GPU_1.obj", foreign_lon,
                            anchor_lat)
        )
        result = _Result()
        added = assembly._expand_exclusions_to_anchor_families(
            result, placements, "PACK")
        assert added == ["Objects/B/p2.obj", "Objects/B/p3.obj"]
        excluded = {r for _p, r in result.exclusions}
        assert {f"Objects/B/p{i}.obj" for i in (1, 2, 3, 4, 5, 6)} \
            <= excluded
        assert "Objects/Misc/GPU_1.obj" not in excluded

    def test_no_consumed_structures_is_a_no_op(self):
        class _Result:
            exclusions = []
        placements = [self._placement("Objects/B/p1.obj",
                                      ANCHOR_LONGITUDE, ANCHOR_LATITUDE)]
        assert assembly._expand_exclusions_to_anchor_families(
            _Result(), placements, "PACK") == []


# ---------------------------------------------------------------------------
# round 8 — flat-by-law plates ship per-node alt_abs (mesh-consumer reality)
# ---------------------------------------------------------------------------

class TestPerNodeEmissionForBridgePlates:
    def _write_patch(self, tmp_path, role, ref):
        from auto_patch.layout import PavementLayout
        layout = PavementLayout(icao="TEST", anchor=ANCHOR)
        polygon = Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 20.0),
                           (0.0, 20.0)])
        layout.shapes.append(BuiltShape(
            polygon=polygon, role=role, ref=ref,
            node_altitudes=[161.01] * 5))
        out = str(tmp_path / "plate.osm")
        layout.to_osm(out)
        return open(out).read()

    def test_trench_emits_per_node_alt_abs_never_flat(self, tmp_path):
        # Round-8 measured defect: to_osm collapsed all-equal
        # node_altitudes into a way-level altitude tag, and the mesh
        # consumer's flat-way branch demonstrably never landed (three
        # fresh KBNA meshes kept DEM z at every trench vertex while
        # per-node alt_abs ways landed exactly).
        from auto_patch.layout import ROLE_BRIDGE_TRENCH
        import re
        text = self._write_patch(tmp_path, ROLE_BRIDGE_TRENCH,
                                 "object_bridge_corridor")
        way = re.search(r"<way .*?</way>", text, re.S).group(0)
        assert "k='altitude'" not in way, "flat collapse must not happen"
        alt_abs_nodes = re.findall(r"k='alt_abs' v='161\.01'", text)
        assert len(alt_abs_nodes) >= 4, "every ring vertex carries alt_abs"

    def test_causeway_emits_per_node_alt_abs(self, tmp_path):
        from auto_patch.layout import ROLE_BRIDGE_CAUSEWAY
        import re
        text = self._write_patch(tmp_path, ROLE_BRIDGE_CAUSEWAY,
                                 "object_bridge_causeway")
        way = re.search(r"<way .*?</way>", text, re.S).group(0)
        assert "k='altitude'" not in way
        assert len(re.findall(r"k='alt_abs' v='161\.01'", text)) >= 4

    def test_other_flat_roles_still_collapse(self, tmp_path):
        # The collapse survives for every other role — byte-identical
        # legacy behaviour (gate-off neutrality).
        from auto_patch.layout import ROLE_TUNNEL_RAMP
        import re
        text = self._write_patch(tmp_path, ROLE_TUNNEL_RAMP, "legacy")
        way = re.search(r"<way .*?</way>", text, re.S).group(0)
        assert "k='altitude' v='161.01'" in way
        assert "alt_abs" not in text


class TestPlatesAreSolverGraphMembers:
    def test_plate_vertices_hard_pinned_at_law_values_in_seeding(
        self, monkeypatch
    ):
        # User directive (round 8): every plate ring vertex is a graph
        # node hard-pinned at its law value, protected like a seam pin.
        from auto_patch.elevation_per_surface.solver_primitives import (
            _seed_elevations,
        )
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        bridges.build_bridge_layout_shapes(layout, None, 36, -87)
        plates = [s for s in layout.shapes
                  if (s.ref or "").startswith("object_bridge_c")]
        assert plates
        nodes = []
        bucket_to_idx = {}
        expected = {}
        for shape in plates:
            ring = list(shape.polygon.exterior.coords)[:-1]
            for (x, y), value in zip(ring, shape.node_altitudes):
                key = layout.canonical_points.get_or_add(float(x), float(y))
                if key not in bucket_to_idx:
                    bucket_to_idx[key] = len(nodes)
                    nodes.append((float(x), float(y)))
                expected[bucket_to_idx[key]] = value
        elev, is_hard, have_initial = _seed_elevations(
            layout, nodes, bucket_to_idx, dem=None,
            tile_lat=36, tile_lon=-87,
        )
        assert all(is_hard[i] for i in expected), \
            "every plate vertex must seed HARD"
        for index, value in expected.items():
            assert elev[index] == pytest.approx(value, abs=0.01)
        # Protected from downstream relax passes like seam pins.
        protected = getattr(layout, "_seam_pin_idx")
        assert set(expected) <= protected


class TestBridgePlateExclusivity:
    def test_portal_pieces_cut_out_of_plates(self, monkeypatch):
        # The round-9 stray class: legacy tunnel-portal ramp/wall pieces
        # (fired by tunnel=yes OSM ways under the deck) constrained at
        # DEM values inside the trench box.  The non-overlap rule drops
        # a piece fully inside and clips a straddler, converting its
        # values to resampled per-vertex node_altitudes.
        from auto_patch.layout import (
            ROLE_BRIDGE_TRENCH, ROLE_TUNNEL_RAMP, ROLE_RETAINING_WALL,
        )
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        plate = Polygon([(10.0, -20.0), (120.0, -20.0), (120.0, 20.0),
                         (10.0, 20.0)])
        layout.shapes.append(BuiltShape(
            polygon=plate, role=ROLE_BRIDGE_TRENCH,
            ref="object_bridge_corridor", node_altitudes=[161.01] * 5))
        # Fully-inside portal wall: dropped.
        layout.shapes.append(BuiltShape(
            polygon=Polygon([(40.0, -5.0), (60.0, -5.0), (60.0, 5.0),
                             (40.0, 5.0)]),
            role=ROLE_RETAINING_WALL, ref="tunnel_wall", altitude=178.3))
        # Straddling sloped ramp: clipped to the outside remainder.
        layout.shapes.append(BuiltShape(
            polygon=Polygon([(100.0, -5.0), (140.0, -5.0), (140.0, 5.0),
                             (100.0, 5.0)]),
            role=ROLE_TUNNEL_RAMP, ref="tunnel_ramp",
            altitude_high=172.0, altitude_low=170.0))
        touched = bridges.enforce_bridge_plate_exclusivity(layout)
        assert touched == 2
        refs = [(s.ref, s.role) for s in layout.shapes]
        assert ("tunnel_wall", ROLE_RETAINING_WALL) not in refs
        ramps = [s for s in layout.shapes if s.ref == "tunnel_ramp"]
        assert len(ramps) == 1
        assert ramps[0].polygon.intersection(plate).area < 1e-6
        minimum_x = min(x for x, _y in ramps[0].polygon.exterior.coords)
        assert minimum_x >= 120.0 - 1e-6  # only the outside part remains
        # The plate itself is untouched.
        trench = [s for s in layout.shapes
                  if s.ref == "object_bridge_corridor"][0]
        assert trench.polygon.area == pytest.approx(plate.area)

    def test_gate_off_touches_nothing(self):
        from auto_patch.layout import ROLE_TUNNEL_RAMP
        layout = _FakeLayout()
        layout.shapes.append(BuiltShape(
            polygon=Polygon([(0.0, 0.0), (10.0, 0.0), (10.0, 5.0),
                             (0.0, 5.0)]),
            role=ROLE_TUNNEL_RAMP, ref="tunnel_ramp", altitude=170.0))
        assert bridges.enforce_bridge_plate_exclusivity(layout) == 0
        assert len(layout.shapes) == 1


class TestLawValueEmissionExemptions:
    def _emit(self, tmp_path, shapes):
        from auto_patch.layout import PavementLayout
        layout = PavementLayout(icao="TEST", anchor=ANCHOR)
        layout.shapes.extend(shapes)
        out = str(tmp_path / "law.osm")
        layout.to_osm(out)
        return open(out).read()

    def test_lip_node_keeps_law_value_against_coincident_rect(
        self, tmp_path
    ):
        # Post-merge delta 1: an approach-rect corner coinciding with a
        # causeway lip node within the merge tolerance averaged the lip
        # to 166.66-166.93.  The law tier wins: the shared node emits
        # at EXACTLY the law value.
        import re
        from auto_patch.layout import (
            ROLE_BRIDGE_CAUSEWAY, ROLE_TUNNEL_RAMP,
        )
        causeway = BuiltShape(
            polygon=Polygon([(0.0, 0.0), (40.0, 0.0), (40.0, 20.0),
                             (0.0, 20.0)]),
            role=ROLE_BRIDGE_CAUSEWAY, ref="object_bridge_causeway",
            node_altitudes=[167.0] * 5)
        # Approach rect sharing the (0,0) corner at a near-miss value.
        approach = BuiltShape(
            polygon=Polygon([(0.0, 0.0), (-20.0, 0.0), (-20.0, 20.0),
                             (0.0, 20.0)]),
            role=ROLE_TUNNEL_RAMP, ref="object_bridge_approach",
            node_altitudes=[166.3, 166.0, 166.0, 166.3, 166.3])
        text = self._emit(tmp_path, [causeway, approach])
        values = [float(m) for m in re.findall(
            r"k='alt_abs' v='([-0-9.]+)'", text)]
        # Every causeway node at exactly 167.00 — no 166.6x average.
        assert 167.0 in values
        assert not any(166.5 < v < 166.99 for v in values), values

    def test_sweep_preserves_densified_plate_nodes(self, tmp_path):
        # Post-merge delta 2: the to_osm decimation sweep took the
        # densified trench ring 75 -> 17 nodes (all values equal =
        # perfectly collinear in 3D).  The law-plate exemption keeps
        # every densified vertex.
        import re
        from auto_patch.layout import ROLE_BRIDGE_TRENCH
        polygon = Polygon([(0.0, 0.0), (120.0, 0.0), (120.0, 50.0),
                           (0.0, 50.0)]).segmentize(5.0)
        ring_count = len(list(polygon.exterior.coords)) - 1
        assert ring_count > 60
        trench = BuiltShape(
            polygon=polygon, role=ROLE_BRIDGE_TRENCH,
            ref="object_bridge_corridor",
            node_altitudes=[161.01] * (ring_count + 1))
        text = self._emit(tmp_path, [trench])
        way = re.search(r"<way .*?</way>", text, re.S).group(0)
        emitted_nodes = len(re.findall(r"<nd ref='(-?\d+)'", way)) - 1
        assert emitted_nodes == ring_count, \
            f"sweep removed {ring_count - emitted_nodes} plate node(s)"
        assert len(re.findall(r"k='alt_abs' v='161\.01'", text)) \
            == ring_count


class TestTunnelPortalPairs:
    """Portal-pair tunnels (user ruling 2026-07-10, the KBNA runway-02C
    class): two aligned structures with airside pavement over the
    connecting body are the two mouths of one buried tunnel — no bridge
    treatment; mouth plates at road grade; approaches climb away."""

    @staticmethod
    def _portal(north_offset_m: float, east_offset_m: float = 0.0,
                heading: float = 0.0, resource: str = "P.obj"):
        origin_latitude, origin_longitude = local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0,
            north_offset_m, east_offset_m,
        )
        square = Polygon([(-10.0, -10.0), (10.0, -10.0),
                          (10.0, 10.0), (-10.0, 10.0)])
        return BridgeStructure(
            object_resources=[resource],
            anchor_longitude_latitude=(origin_longitude, origin_latitude),
            frame_origin_longitude_latitude=(
                origin_longitude, origin_latitude),
            heading_degrees=heading,
            deck_polygon=square,
            deck_top_profile=[(0.0, 7.5), (20.0, 7.5)],
            deck_top_y_m=7.5,
            deck_end_elevations_y_m=(7.5, 7.5),
            deck_length_m=20.0,
            deck_width_m=20.0,
            ceiling_y_m=6.0,
            clearance_underside_y_m=6.0,
            abutment_lines=[((-10.0, -10.0), (-10.0, 10.0)),
                            ((10.0, -10.0), (10.0, 10.0))],
            abutment_reaches_grade=(True, True),
            contract=DECK_CARRIED,
            absolute_deck_elevation_m=None,
            hard_deck=False,
            deck_hardness=DECK_HARDNESS_COSMETIC,
        )

    @staticmethod
    def _runway_between(layout, north_m: float = 150.0):
        from auto_patch.layout import BuiltShape, ROLE_RUNWAY
        x0, y0 = layout.ll_to_m(*local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, north_m, 0.0))
        layout.shapes.append(BuiltShape(
            polygon=Polygon([(x0 - 500.0, y0 - 20.0),
                             (x0 + 500.0, y0 - 20.0),
                             (x0 + 500.0, y0 + 20.0),
                             (x0 - 500.0, y0 + 20.0)]),
            role=ROLE_RUNWAY, ref="02C", altitude=188.0))

    def _paired_layout(self):
        # ``local_offset_to_lonlat`` at heading 0 maps local_x → EAST,
        # so the pair axis runs east-west: portal headings are 90.
        layout = _FakeLayout()
        classification = _Classification(
            [self._portal(0.0, heading=90.0, resource="A.obj"),
             self._portal(300.0, heading=90.0, resource="B.obj")])
        setattr(layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
                classification)
        self._runway_between(layout)
        return layout

    def test_pavement_over_body_pairs_the_portals(self):
        layout = self._paired_layout()
        dem = _FakeDem(180.0)
        pairs = bridges._detect_tunnel_portal_pairs(layout, dem, 36, -87)
        assert len(pairs) == 1
        for portal in pairs[0]["portals"]:
            assert portal["mouth_floor_m"] == pytest.approx(180.0)

    def test_partition_diverts_portals_from_bridge_treatment(self):
        layout = self._paired_layout()
        dem = _FakeDem(180.0)
        bridges._detect_tunnel_portal_pairs(layout, dem, 36, -87)
        classification = bridges._object_bridge_classification(layout)
        corridor, _s, _r, _rc, portals = (
            bridges._partition_bridges_for_corridors(classification, layout)
        )
        assert len(portals) == 2
        assert not corridor

    def test_mouth_plates_born_no_causeways(self):
        from auto_patch.layout import ROLE_BRIDGE_CAUSEWAY, ROLE_BRIDGE_TRENCH
        layout = self._paired_layout()
        dem = _FakeDem(180.0)
        n_trench, n_causeway, _pads = bridges.build_bridge_layout_shapes(
            layout, dem, 36, -87)
        assert n_causeway == 0
        mouths = [shape for shape in layout.shapes
                  if shape.role == ROLE_BRIDGE_TRENCH
                  and shape.ref == "object_tunnel_portal_mouth"]
        # Crown split (user rulings 2026-07-14/b/c, REVISED 2026-07-17):
        # each portal is THREE plates — the open-mouth half at road
        # grade, the buried half as a CROWN, and a COLLAR band around
        # the buried half's back.  The OBJECT is the terrain authority
        # (user 2026-07-17): the crown seats no lower than the object's
        # roof plane (mouth + deck_top; the classifier's dominant plane
        # already excludes parapet caps), with the DEM acting only as an
        # upward override.  On this flat 180 m terrain the crowns sit at
        # 180 + 7.5 = 187.5; the collar is a TRANSITION ring feathering
        # from the crown at the object-hidden face (187.5) down to the
        # surrounding ground / mouth floor (180.0) at the exposed rim.
        crowns = [shape for shape in layout.shapes
                  if shape.role == ROLE_BRIDGE_TRENCH
                  and shape.ref == "object_tunnel_portal_crown"]
        collars = [shape for shape in layout.shapes
                   if shape.role == ROLE_BRIDGE_TRENCH
                   and shape.ref == "object_tunnel_portal_collar"]
        assert (len(mouths), len(crowns), len(collars)) == (2, 2, 2)
        assert n_trench == 6
        for mouth in mouths:
            assert set(mouth.node_altitudes) == {180.0}
        for plate in crowns:
            assert set(plate.node_altitudes) == {187.5}
        for plate in collars:
            values = set(plate.node_altitudes)
            assert min(values) == pytest.approx(180.0, abs=0.1)
            assert max(values) == pytest.approx(187.5, abs=0.1)
            assert all(179.9 <= v <= 187.6 for v in values)
        assert not [shape for shape in layout.shapes
                    if shape.role == ROLE_BRIDGE_CAUSEWAY]

    def test_owned_crossing_masks_adjacent_ground_and_clearance(
            self, monkeypatch):
        """User ruling 2026-07-14 (``BRIDGE_CROSSING_MASK``), REFINED
        2026-07-15 (KBNA round 6), REBUILT for Phase 1 of docs/specs/
        crossing-terrain-ownership.md: the recognized crossing is
        published as the ONE influence zone that bands, skirts,
        clearance, and gap-fill all consult — the open crossing (portal
        footprints, collar rings) is zone, while the roof over the
        BURIED tunnel body is normal graded ground and stays bandable
        BY CONSTRUCTION (the zone over the buried span carries only the
        road bore).  ``O4_ADJACENT_GROUND_BURIED_BODY_BAND=0`` restores
        the full-span mask; ``BRIDGE_CROSSING_MASK=0`` unpublishes the
        classifier components."""
        from shapely.geometry import Point

        import auto_patch.osm_load as OL
        from auto_patch import config, crossing_terrain

        # Hermetic road corridor: the dev machine carries real caches.
        monkeypatch.setattr(OL, "_load_osm_big_roads",
                            lambda lat, lon, *a, **k: ({}, []))
        layout = self._paired_layout()
        dem = _FakeDem(180.0)
        pairs = bridges._detect_tunnel_portal_pairs(layout, dem, 36, -87)
        assert pairs  # sanity: the pair is recognized
        assert crossing_terrain.publish_crossing_influence_zones(layout) > 0
        zone = crossing_terrain.crossing_influence_zone_union(layout)
        assert zone is not None and not zone.is_empty
        centroid_a = pairs[0]["portals"][0]["footprint"].centroid
        centroid_b = pairs[0]["portals"][1]["footprint"].centroid
        middle = Point((centroid_a.x + centroid_b.x) / 2.0,
                       (centroid_a.y + centroid_b.y) / 2.0)
        # Buried-roof banding ON (default): the roof midpoint is NOT in
        # the zone — bands may march there — while the portal footprints
        # themselves are zone.
        assert not zone.covers(middle)
        assert zone.intersects(pairs[0]["portals"][0]["footprint"])
        assert zone.intersects(pairs[0]["portals"][1]["footprint"])

        # Buried-roof knob OFF restores the 2026-07-14 full-span mask
        # (the knob is import-frozen; patch the module attribute).
        monkeypatch.setattr(crossing_terrain, "_BURIED_BODY_BAND", False)
        assert crossing_terrain.publish_crossing_influence_zones(layout) > 0
        zone_full = crossing_terrain.crossing_influence_zone_union(layout)
        assert zone_full is not None and zone_full.covers(middle)

        # Crossing mask off: the classifier components unpublish, and with
        # no mapped depressed road either, nothing remains.
        monkeypatch.setattr(config, "BRIDGE_CROSSING_MASK", False)
        assert crossing_terrain.publish_crossing_influence_zones(layout) == 0
        assert crossing_terrain.crossing_influence_zone_union(layout) is None
        # The march-side consumers read the same nothing.
        from auto_patch import adjacent_ground
        assert adjacent_ground._crossing_zone_union(layout) is None

    def test_portal_crown_gate_off_restores_single_plate(self, monkeypatch):
        from auto_patch import config
        from auto_patch.layout import ROLE_BRIDGE_TRENCH

        monkeypatch.setattr(config, "TUNNEL_PORTAL_CROWN", False)
        layout = self._paired_layout()
        dem = _FakeDem(180.0)
        n_trench, _n_causeway, _pads = bridges.build_bridge_layout_shapes(
            layout, dem, 36, -87)
        mouths = [shape for shape in layout.shapes
                  if shape.role == ROLE_BRIDGE_TRENCH
                  and shape.ref == "object_tunnel_portal_mouth"]
        crowns = [shape for shape in layout.shapes
                  if shape.ref == "object_tunnel_portal_crown"]
        assert len(mouths) == 2 and n_trench == 2 and crowns == []
        for mouth in mouths:
            assert set(mouth.node_altitudes) == {180.0}

    def test_side_by_side_parallel_decks_do_not_pair(self):
        layout = _FakeLayout()
        # Separation along local_z (south) with headings 90 (east):
        # the connecting segment is PERPENDICULAR to both headings —
        # the side-by-side double-deck geometry that must never pair.
        classification = _Classification(
            [self._portal(0.0, heading=90.0, resource="A.obj"),
             self._portal(0.0, east_offset_m=40.0, heading=90.0,
                          resource="B.obj")])
        setattr(layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
                classification)
        self._runway_between(layout, north_m=0.0)
        dem = _FakeDem(180.0)
        pairs = bridges._detect_tunnel_portal_pairs(layout, dem, 36, -87)
        assert pairs == []

    def test_open_ground_pair_does_not_pair(self):
        layout = self._paired_layout()
        layout.shapes = []  # no pavement over the body, flat ground
        dem = _FakeDem(180.0)
        pairs = bridges._detect_tunnel_portal_pairs(layout, dem, 36, -87)
        assert pairs == []

    def test_near_end_inversion_guard_refuses_raised_floor(self):
        from shapely.geometry import LineString as _LineString
        layout = _FakeLayout()
        dem = _FakeDem(174.0)
        walk = _LineString([(0.0, 0.0), (200.0, 0.0)])
        refused = bridges._emit_corridor_ramp_chain(
            layout, dem, 36, -87, layout.m_to_ll,
            walk, 200.0, 181.6, 11.0, 20.0, refuse_inverted=True)
        assert refused is False and not layout.shapes
        emitted = bridges._emit_corridor_ramp_chain(
            layout, dem, 36, -87, layout.m_to_ll,
            walk, 200.0, 174.5, 11.0, 20.0, refuse_inverted=True)
        assert emitted is True and layout.shapes

    def test_portal_collar_is_a_transition_not_a_flat_plate(self):
        # Defect (2026-07-15): the collar was emitted FLAT at the crown
        # elevation, so its exposed rim was a vertical cliff (KBNA:
        # 7.76/7.09 m steps to the approach, 4.44 m to the
        # adjacent_ground band).  It must be a TRANSITION ring: the
        # inner face hugs the crown, the exposed flank rim feathers down
        # to the surrounding ground.  With a laterally-dropping DEM the
        # collar must carry PER-VERTEX altitudes that span from the
        # crown down toward that lower ground — never a single value.
        layout = self._paired_layout()
        dem = _LateralRidgeDem(crest_m=180.0, falloff_m_per_m=0.30)
        bridges.build_bridge_layout_shapes(layout, dem, 36, -87)

        crowns = [shape for shape in layout.shapes
                  if shape.ref == "object_tunnel_portal_crown"]
        collars = [shape for shape in layout.shapes
                   if shape.ref == "object_tunnel_portal_collar"]
        assert crowns and collars

        # The crown stays a single terrain-true elevation (unchanged).
        crown_elevation = crowns[0].node_altitudes[0]
        assert all(value == pytest.approx(crown_elevation, abs=0.05)
                   for value in crowns[0].node_altitudes)

        collar_altitudes = [value for shape in collars
                            for value in (shape.node_altitudes or [])]
        assert collar_altitudes
        # NOT a flat plate: the ring feathers over a real range.
        assert max(collar_altitudes) - min(collar_altitudes) > 3.0, (
            "collar must be a transition, not a flat plate "
            f"(range {max(collar_altitudes) - min(collar_altitudes):.2f} m)")
        # Never rises above the crown; the exposed rim drops well below.
        assert max(collar_altitudes) <= crown_elevation + 0.1
        assert min(collar_altitudes) <= crown_elevation - 3.0

        # The per-vertex values TRACK the synthetic DEM: every collar
        # vertex feathers toward the ground it drops into (the DEM
        # sampled AT-or-just-OUTWARD of the vertex — the value the
        # abutting band will read), never rising above the crown nor
        # cliffing below that ground.
        def _ground_floor(vertex_x, vertex_y):
            """Lowest DEM within a small disk around the vertex — a lower
            bound on the ground the collar may feather into (the emitter
            samples the same neighbourhood; a slightly wider disk here is
            a safe lower bound regardless of the exact emitter radius)."""
            samples = []
            for offset_x, offset_y in (
                    (0.0, 0.0), (6.0, 0.0), (-6.0, 0.0),
                    (0.0, 6.0), (0.0, -6.0)):
                lat, lon = layout.m_to_ll(vertex_x + offset_x,
                                          vertex_y + offset_y)
                samples.append(dem.alt((lon - (-87.0), lat - 36.0)))
            return min(samples)

        lowest = None
        for shape in collars:
            ring = list(shape.polygon.exterior.coords)
            for (vertex_x, vertex_y), value in zip(
                    ring, shape.node_altitudes):
                ground_low = _ground_floor(vertex_x, vertex_y)
                # bounded ABOVE by the crown and never a cliff standing
                # above the ground it feathers into
                assert value <= crown_elevation + 0.1
                assert value >= ground_low - 0.6
                if lowest is None or value < lowest[0]:
                    lowest = (value, vertex_x, vertex_y)
        low_value, low_x, low_y = lowest
        # the lowest collar vertex is an EXPOSED-flank vertex over
        # demonstrably lower ground than the crown, and it feathered
        # down toward that ground (not left standing at the crown).
        assert low_value < crown_elevation - 2.0
        low_lat, low_lon = layout.m_to_ll(low_x, low_y)
        assert dem.alt((low_lon - (-87.0), low_lat - 36.0)) \
            < crown_elevation - 1.5

    def test_crowns_floor_at_object_roof_and_follow_higher_terrain(self):
        # REVISED 2026-07-17 (user ruling: the OBJECT is the terrain
        # authority).  The crown seats at max(DEM over the buried body,
        # mouth_floor + deck_top): the object's flat roof plane is the
        # FLOOR — where the DEM sits below it (KBNA Murfreesboro: DEM
        # 171 vs roof 176.8, the collar-at-road-level defect), the crown
        # takes the roof plane; where the DEM stands ABOVE the roof
        # plane (a hillside portal buried deeper), the crown stays
        # terrain-true to the DEM (round-8 fact 2 semantics preserved as
        # the upward override).
        meters_per_degree_longitude = 111320.0 * math.cos(
            math.radians(ANCHOR_LATITUDE))

        class _GradientDem:
            """East-running gradient with a configurable base."""

            nodata = -32768

            def __init__(self, base_m):
                self.base_m = base_m

            def alt(self, xy):
                longitude = xy[0] + (-87.0)
                east_m = ((longitude - ANCHOR_LONGITUDE)
                          * meters_per_degree_longitude)
                return self.base_m + 0.01 * east_m

        # Case 1 — DEM below the roof plane everywhere: each crown =
        # its own mouth floor + deck_top (7.5).  The mouth floors track
        # the terrain gradient, so the two crowns still diverge — they
        # are never clamped to one shared value.
        layout = self._paired_layout()
        dem = _GradientDem(175.0)
        bridges.build_bridge_layout_shapes(layout, dem, 36, -87)
        mouths = [shape for shape in layout.shapes
                  if shape.ref == "object_tunnel_portal_mouth"]
        crowns = [shape for shape in layout.shapes
                  if shape.ref == "object_tunnel_portal_crown"]
        assert len(mouths) == 2 and len(crowns) == 2
        crown_values = []
        for mouth, crown in zip(mouths, crowns):
            values = set(round(v, 2) for v in crown.node_altitudes)
            assert len(values) == 1
            crown_value = next(iter(values))
            crown_values.append(crown_value)
            mouth_value = next(iter(set(mouth.node_altitudes)))
            assert crown_value == pytest.approx(
                mouth_value + 7.5, abs=0.05), (
                f"crown {crown_value} not floored at the object roof "
                f"plane (mouth {mouth_value} + 7.5)")
        assert abs(crown_values[0] - crown_values[1]) > 1.5

        # Case 2 — DEM ABOVE the roof plane over the buried body only
        # (a hillside portal buried deeper): terrain-true upward
        # override — the crown takes the hill's DEM, not the roof
        # plane.  The hill covers the footprints; the outward mouth
        # rays read the surrounding 180 m plain, so the roof plane is
        # 180 + 7.5 = 187.5, well under the 200 m hill.
        meters_per_degree_latitude = 111132.0

        class _HillOverBodiesDem:
            nodata = -32768

            def alt(self, xy):
                longitude = xy[0] + (-87.0)
                latitude = xy[1] + 36.0
                east_m = ((longitude - ANCHOR_LONGITUDE)
                          * meters_per_degree_longitude)
                north_m = ((latitude - ANCHOR_LATITUDE)
                           * meters_per_degree_latitude)
                near_a = (abs(east_m - 0.0) <= 12.0
                          and abs(north_m) <= 12.0)
                near_b = (abs(east_m - 300.0) <= 12.0
                          and abs(north_m) <= 12.0)
                return 200.0 if (near_a or near_b) else 180.0

        layout = self._paired_layout()
        hill_dem = _HillOverBodiesDem()
        bridges.build_bridge_layout_shapes(layout, hill_dem, 36, -87)
        crowns = [shape for shape in layout.shapes
                  if shape.ref == "object_tunnel_portal_crown"]
        mouths = [shape for shape in layout.shapes
                  if shape.ref == "object_tunnel_portal_mouth"]
        assert len(crowns) == 2
        for mouth, crown in zip(mouths, crowns):
            crown_value = next(iter(set(round(v, 2)
                                        for v in crown.node_altitudes)))
            mouth_value = next(iter(set(mouth.node_altitudes)))
            assert mouth_value == pytest.approx(180.0, abs=0.1)
            assert crown_value == pytest.approx(200.0, abs=0.3), (
                f"crown {crown_value} not terrain-true to the 200 m "
                "hill over its buried body")

    def test_clip_collar_to_mouth_front_removes_the_road_side_lobe(self):
        # Round-8 fact 3: the forward-face clip removes any collar region
        # ahead of the mouth face while keeping the back band.  A square
        # footprint spanning north -10..10 has its front face at north 11
        # (extent + 1 m); outward is +north.
        from shapely.ops import unary_union

        footprint = Polygon([(-10.0, -10.0), (10.0, -10.0),
                             (10.0, 10.0), (-10.0, 10.0)])
        outward = (0.0, 1.0)
        back_band = Polygon([(-12.0, -22.0), (12.0, -22.0),
                            (12.0, -12.0), (-12.0, -12.0)])   # behind
        front_lobe = Polygon([(-5.0, 40.0), (5.0, 40.0),
                             (5.0, 50.0), (-5.0, 50.0)])       # ahead
        collar = unary_union([back_band, front_lobe])
        clipped = bridges._clip_collar_to_mouth_front(
            collar, footprint, outward)
        # The road-side lobe is gone; the back band survives whole.
        assert clipped.intersection(front_lobe).area == pytest.approx(
            0.0, abs=1e-6)
        assert clipped.intersection(back_band).area == pytest.approx(
            back_band.area, abs=1e-6)

    def test_clip_collar_front_works_far_from_the_layout_origin(self):
        # Regression (KBNA 02C round 8): the clip rectangle was anchored
        # at ``outward * front_extent`` — on the front line but up to
        # kilometres away LATERALLY from the footprint (layout meters
        # are anchored at the airport reference, not the portal), so the
        # finite rectangle missed the site and the clip was a silent
        # no-op (the measured 34 m² front lobe survived the first fixed
        # build).  The same geometry translated 2.4 km from the origin
        # must clip identically to the at-origin case.
        from shapely.affinity import translate as shapely_translate

        outward = (-0.888, 0.459)
        norm = math.hypot(*outward)
        outward = (outward[0] / norm, outward[1] / norm)
        footprint = Polygon([(-10.0, -10.0), (10.0, -10.0),
                             (10.0, 10.0), (-10.0, 10.0)])
        lobe = Polygon([
            (outward[0] * 15.0 - 5.0, outward[1] * 15.0 - 5.0),
            (outward[0] * 25.0 - 5.0, outward[1] * 25.0 - 5.0),
            (outward[0] * 25.0 + 5.0, outward[1] * 25.0 + 5.0),
            (outward[0] * 15.0 + 5.0, outward[1] * 15.0 + 5.0),
        ])
        for offset_x, offset_y in ((0.0, 0.0), (-2005.0, -1295.0)):
            moved_footprint = shapely_translate(
                footprint, xoff=offset_x, yoff=offset_y)
            moved_lobe = shapely_translate(
                lobe, xoff=offset_x, yoff=offset_y)
            clipped = bridges._clip_collar_to_mouth_front(
                moved_lobe, moved_footprint, outward)
            assert clipped.area < 0.2 * moved_lobe.area, (
                f"lobe forward of the face must clip at offset "
                f"({offset_x}, {offset_y}); "
                f"{clipped.area:.1f} of {moved_lobe.area:.1f} m² left")


class TestClassificationSidecar:
    """Classification sidecar cache (user directive 2026-07-10):
    fingerprint covers the DSF, every .obj in the pack, the pavement
    evidence, and the version salt — any pack edit invalidates.  Per the
    user ruling 2026-07-15 the sidecar lives under the data root's
    ``Airport_mod_cache/<pack name>/`` — never inside the pack — and any
    pre-ruling in-pack sidecar is removed on resolution."""

    @pytest.fixture(autouse=True)
    def _isolated_data_root(self, tmp_path, monkeypatch):
        """Pin the data root under ``tmp_path`` — ``ORTHO4XP_DATA_ROOT``
        wins ``O4_File_Names.resolve_data_root`` — so sidecars never
        escape the test sandbox."""
        self.data_root = tmp_path / "o4root"
        monkeypatch.setenv("ORTHO4XP_DATA_ROOT", str(self.data_root))

    @staticmethod
    def _pack(tmp_path):
        pack = tmp_path / "US-TEST Airport"
        (pack / "Objects").mkdir(parents=True)
        dsf = pack / "overlay.dsf"
        dsf.write_bytes(b"dsf-bytes")
        (pack / "Objects" / "a.obj").write_text("VT 0 0 0\n")
        (pack / "Objects" / "b.obj").write_text("VT 1 1 1\n")
        return pack, dsf

    def test_fingerprint_stable_and_path_under_data_root(self, tmp_path):
        pack, dsf = self._pack(tmp_path)
        path_1, print_1 = assembly._classification_sidecar(
            str(dsf), str(pack), None)
        path_2, print_2 = assembly._classification_sidecar(
            str(dsf), str(pack), None)
        assert print_1 == print_2
        assert path_1 == path_2
        # The sidecar lands under the data root's Airport_mod_cache —
        # never inside the pack (user ruling 2026-07-15) — and carries
        # the DSF stem so two DSFs of one pack cannot collide.
        assert path_1 == os.path.join(
            str(self.data_root), "Airport_mod_cache", "US-TEST Airport",
            "o4_object_terrain_classification_overlay.cache")
        assert not path_1.startswith(str(pack))

    def test_stale_legacy_in_pack_sidecar_removed(self, tmp_path):
        pack, dsf = self._pack(tmp_path)
        legacy = pack / "o4_object_terrain_classification.cache"
        legacy.write_bytes(b"pre-ruling in-pack sidecar")
        assembly._classification_sidecar(str(dsf), str(pack), None)
        # The old in-pack file was cleaned up on sidecar resolution.
        assert not legacy.exists()

    def test_object_edit_invalidates(self, tmp_path):
        import os as _os
        pack, dsf = self._pack(tmp_path)
        _path, before = assembly._classification_sidecar(
            str(dsf), str(pack), None)
        target = pack / "Objects" / "a.obj"
        _os.utime(target, (1e9, 1e9))
        _path, after = assembly._classification_sidecar(
            str(dsf), str(pack), None)
        assert before != after

    def test_backup_files_do_not_count(self, tmp_path):
        pack, dsf = self._pack(tmp_path)
        _path, before = assembly._classification_sidecar(
            str(dsf), str(pack), None)
        (pack / "Objects" / "a.obj.anchor_bak").write_text("backup")
        _path, after = assembly._classification_sidecar(
            str(dsf), str(pack), None)
        assert before == after

    def test_pavement_evidence_changes_fingerprint(self, tmp_path):
        pack, dsf = self._pack(tmp_path)
        ring = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])
        _path, without = assembly._classification_sidecar(
            str(dsf), str(pack), None)
        _path, with_ring = assembly._classification_sidecar(
            str(dsf), str(pack), [ring])
        assert without != with_ring

    def test_no_pack_root_means_no_sidecar(self, tmp_path):
        pack, dsf = self._pack(tmp_path)
        assert assembly._classification_sidecar(
            str(dsf), None, None) == (None, None)
        assert assembly._classification_sidecar(
            str(dsf), str(tmp_path / "missing"), None) == (None, None)

    def test_apt_dat_edit_invalidates(self, tmp_path):
        import os as _os
        pack, dsf = self._pack(tmp_path)
        apt_dat = pack / "Earth nav data"
        apt_dat.mkdir()
        apt_dat = apt_dat / "apt.dat"
        apt_dat.write_text("1 599 KBNA\n")
        _path, before = assembly._classification_sidecar(
            str(dsf), str(pack), None, apt_dat_path=str(apt_dat))
        _os.utime(apt_dat, (1e9, 1e9))
        _path, after = assembly._classification_sidecar(
            str(dsf), str(pack), None, apt_dat_path=str(apt_dat))
        assert before != after

    def test_dsf_edit_invalidates(self, tmp_path):
        import os as _os
        pack, dsf = self._pack(tmp_path)
        _path, before = assembly._classification_sidecar(
            str(dsf), str(pack), None)
        _os.utime(dsf, (1e9, 1e9))
        _path, after = assembly._classification_sidecar(
            str(dsf), str(pack), None)
        assert before != after


# ---------------------------------------------------------------------------
# per-DSF sibling road-network sidecar cache
# ---------------------------------------------------------------------------

_ROAD_DUMP = "\n".join([
    "NETWORK_DEF lib/g10/roads_EU.net",
    "BEGIN_SEGMENT 0 20 1 -86.678000 36.124000 0.0",
    "END_SEGMENT 2 -86.679000 36.125000 0.0",
]) + "\n"


class TestRoadNetworkSidecarCache:
    """The per-DSF road-network cache in ``_discover_sibling_road_networks``
    — a warm hit must skip both the DSFTool dump and the parse while
    reproducing the same :class:`RoadNetwork`; the gate turns it off; the
    sidecar helper declines when no pack root resolves.  Per the user
    ruling 2026-07-15 the sidecar lives under the data root's
    ``Airport_mod_cache/<pack name>/`` — never inside the roads pack —
    and any pre-ruling in-pack sidecar is removed on resolution."""

    _TILE_LATITUDE = 36
    _TILE_LONGITUDE = -87

    @pytest.fixture(autouse=True)
    def _isolated_data_root(self, tmp_path, monkeypatch):
        """Pin the data root under ``tmp_path`` — ``ORTHO4XP_DATA_ROOT``
        wins ``O4_File_Names.resolve_data_root`` — so sidecars never
        escape the test sandbox."""
        self.data_root = tmp_path / "o4root"
        monkeypatch.setenv("ORTHO4XP_DATA_ROOT", str(self.data_root))

    def _sidecar_path(self, pack_root):
        pack_name = os.path.basename(os.path.abspath(pack_root))
        return os.path.join(
            str(self.data_root), "Airport_mod_cache", pack_name,
            "o4_dsf_road_network_+36-087.cache")

    def _roads_pack(self, tmp_path):
        """Build ``<xplane>/Custom Scenery/<pack>/Earth nav data/
        +30-090/+36-087.dsf`` and return ``(xplane_root, pack_root)``."""
        xplane_root = tmp_path / "X-Plane 12"
        pack_root = xplane_root / "Custom Scenery" / "US-KBNA Nashville Roads"
        earth_nav_data = pack_root / "Earth nav data" / "+30-090"
        earth_nav_data.mkdir(parents=True)
        (earth_nav_data / "+36-087.dsf").write_text("binary-placeholder")
        return str(xplane_root), str(pack_root)

    def _patch_pack_order_and_loader(self, monkeypatch):
        from auto_patch import agp_reader
        monkeypatch.setattr(
            agp_reader, "_scenery_pack_order",
            lambda _root: ["US-KBNA Nashville Roads"])
        load_calls = []

        def counting_load_dsf_text(dsf_path, *args, **keyword_arguments):
            load_calls.append(dsf_path)
            return _ROAD_DUMP.splitlines()

        monkeypatch.setattr(assembly.dsf_reader, "_load_dsf_text",
                            counting_load_dsf_text)
        return load_calls

    def test_warm_hit_skips_dump_and_parse(self, tmp_path, monkeypatch):
        xplane_root, pack_root = self._roads_pack(tmp_path)
        load_calls = self._patch_pack_order_and_loader(monkeypatch)

        first = assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        assert len(first) == 1 and first[0].segments
        assert len(load_calls) == 1
        assert os.path.isfile(self._sidecar_path(pack_root))

        second = assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        # Cache hit: no second dump/parse, identical network.
        assert len(load_calls) == 1
        assert second == first

    def test_sidecar_lands_under_data_root_not_in_pack(
            self, tmp_path, monkeypatch):
        xplane_root, pack_root = self._roads_pack(tmp_path)
        self._patch_pack_order_and_loader(monkeypatch)

        assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        sidecar = self._sidecar_path(pack_root)
        assert os.path.isfile(sidecar)
        assert sidecar.startswith(
            os.path.join(str(self.data_root), "Airport_mod_cache"))
        # Nothing cache-shaped may land inside the roads pack (user
        # ruling 2026-07-15).
        pack_files = []
        for directory, _subdirectories, file_names in os.walk(pack_root):
            pack_files.extend(file_names)
        assert not any(name.endswith(".cache") for name in pack_files)

    def test_stale_legacy_in_pack_sidecar_removed(
            self, tmp_path, monkeypatch):
        xplane_root, pack_root = self._roads_pack(tmp_path)
        self._patch_pack_order_and_loader(monkeypatch)
        legacy = os.path.join(pack_root, "o4_dsf_road_network.cache")
        with open(legacy, "wb") as handle:
            handle.write(b"pre-ruling in-pack sidecar")

        assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        # The old in-pack file was cleaned up on sidecar resolution.
        assert not os.path.exists(legacy)

    def test_gate_zero_disables_read_and_write(
            self, tmp_path, monkeypatch):
        xplane_root, pack_root = self._roads_pack(tmp_path)
        load_calls = self._patch_pack_order_and_loader(monkeypatch)
        monkeypatch.setenv("O4_DSF_ROAD_NETWORK_CACHE", "0")

        assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        assert not os.path.isfile(self._sidecar_path(pack_root))
        assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        # Both calls dumped/parsed (no cache in play).
        assert len(load_calls) == 2

    def test_dsf_edit_invalidates(self, tmp_path, monkeypatch):
        xplane_root, pack_root = self._roads_pack(tmp_path)
        load_calls = self._patch_pack_order_and_loader(monkeypatch)

        assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        assert len(load_calls) == 1
        dsf = os.path.join(pack_root, "Earth nav data", "+30-090",
                           "+36-087.dsf")
        os.utime(dsf, (1e9, 1e9))
        assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        # Stale fingerprint → recompute (second dump/parse).
        assert len(load_calls) == 2

    def test_corrupt_sidecar_falls_back(self, tmp_path, monkeypatch):
        xplane_root, pack_root = self._roads_pack(tmp_path)
        load_calls = self._patch_pack_order_and_loader(monkeypatch)

        assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        sidecar = self._sidecar_path(pack_root)
        with open(sidecar, "wb") as handle:
            handle.write(b"garbled \x00\x01")
        assembly._discover_sibling_road_networks(
            xplane_root, self._TILE_LATITUDE, self._TILE_LONGITUDE)
        # A corrupt sidecar never raises — it recomputes (second load).
        assert len(load_calls) == 2

    def test_no_pack_root_means_no_sidecar(self, tmp_path, monkeypatch):
        # When the pack root cannot be resolved the helper declines and
        # the discovery loop falls back to its uncached dump/parse.
        loose_dsf = tmp_path / "loose.dsf"
        loose_dsf.write_text("binary-placeholder")
        monkeypatch.setattr(assembly.dsf_reader, "_pack_root_for_dsf",
                            lambda _path: None)
        assert assembly._road_network_sidecar(
            str(loose_dsf)) == (None, None)


# ---------------------------------------------------------------------------
# 2026-07-15 defects A+B (KBNA Donelson) — approach-chain continuity,
# [H,L,L,H] slope orientation, and the corridor-plate weld
# ---------------------------------------------------------------------------

def _draped_curved_road_across_deck(length_m: float = 131.0) -> RoadNetwork:
    """A fully-draped road crossing UNDER the deck perpendicular to its
    axis but CURVING away from it (radius ~400 m) — the geometry that
    made consecutive same-chain steps overlap and get registry-dropped
    (defect B: every-other-step terrain holes at KBNA)."""
    shape_points = []
    for across in range(-300, 301, 30):
        along = length_m / 2.0 + (across * across) / 800.0
        latitude, longitude = local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, along, float(across)
        )
        shape_points.append(RoadShapePoint(longitude, latitude, 0.0, True))
    return RoadNetwork(
        network_definitions=["lib/g10/roads_EU.net"],
        segments=[RoadSegment(0, "lib/g10/roads_EU.net", 20, 1, 2,
                              shape_points)],
        skipped_line_count=0,
    )


def _approach_shapes(layout):
    return [s for s in layout.shapes if s.ref == "object_bridge_approach"]


def _chain_sides(approaches, footprint_centroid_y=0.0):
    """Split approach quads into the two exit chains by which side of
    the deck they sit on, each ordered by distance from the deck."""
    sides: dict = {"+": [], "-": []}
    for shape in approaches:
        key = "+" if shape.polygon.centroid.y > footprint_centroid_y else "-"
        sides[key].append(shape)
    for members in sides.values():
        members.sort(key=lambda s: abs(s.polygon.centroid.y))
    return [members for members in sides.values() if members]


def _open_ring(polygon):
    ring = list(polygon.exterior.coords)
    return ring[:-1] if ring[0] == ring[-1] else ring


class TestApproachChainContinuity:
    def test_curved_road_chain_contiguous_and_complete(self):
        # Defect B regression: under curvature the per-step rects used
        # to overlap and the shared registry dropped every other SAME-
        # CHAIN step.  The reworked emitter shares one corner pair per
        # station: all steps emit, consecutive quads share their facing
        # edge VERBATIM, and no pair overlaps above the audit floor.
        layout = _FakeLayout()
        layout.shapes.append(_deck_route_shape())
        count, _s, _c = bridges._emit_object_sourced_bridge_corridors(
            layout, _FakeDem(150.0), 36, -87,
            _Classification([_bridge()]),
            [_draped_curved_road_across_deck()],
            road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
        )
        assert count == 1
        approaches = _approach_shapes(layout)
        # The depressed walk runs BRIDGE_CORRIDOR_DEPRESSED_LENGTH_M
        # (the caller's 80 m only widens) at 20 m steps, both sides.
        steps_per_side = int(
            config.BRIDGE_CORRIDOR_DEPRESSED_LENGTH_M // 20.0)
        assert len(approaches) == 2 * steps_per_side, \
            "every station step must emit — no same-chain registry drops"
        for chain in _chain_sides(approaches):
            for near_shape, far_shape in zip(chain, chain[1:]):
                shared = (set(_open_ring(near_shape.polygon))
                          & set(_open_ring(far_shape.polygon)))
                assert len(shared) >= 2, \
                    "consecutive quads must share their facing edge verbatim"
        for index_a, shape_a in enumerate(approaches):
            for shape_b in approaches[index_a + 1:]:
                assert shape_a.polygon.intersection(
                    shape_b.polygon).area <= 0.5, \
                    "no approach-versus-approach overlap (audit invariant 1)"

    def test_sloped_quads_follow_high_low_corner_convention(self):
        # [H,L,L,H]: ring corners 0,3 carry altitude_high.  With DEM 150
        # below the 161.01 floor the chain DESCENDS outward, so the high
        # end of every quad is its NEAR (deck-side) end — corners 0,3
        # must sit nearer the deck than corners 1,2.  (Measured KBNA
        # defect: the fixed near-first ring order shipped every climbing
        # rect inverted — floor value on the far edge.)
        from shapely.geometry import Point
        from shapely.geometry import Polygon as _Polygon
        layout = _FakeLayout()
        layout.shapes.append(_deck_route_shape())
        bridges._emit_object_sourced_bridge_corridors(
            layout, _FakeDem(150.0), 36, -87,
            _Classification([_bridge()]),
            [_draped_road_network_across_deck()],
            road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
        )
        deck_box = _Polygon([(0.0, -27.5), (131.0, -27.5),
                             (131.0, 27.5), (0.0, 27.5)])
        sloped = [s for s in _approach_shapes(layout)
                  if s.altitude_high is not None]
        assert sloped, "descending chains must emit sloped quads"
        for shape in sloped:
            ring = _open_ring(shape.polygon)
            assert len(ring) == 4
            high_pair = (deck_box.distance(Point(ring[0]))
                         + deck_box.distance(Point(ring[3])))
            low_pair = (deck_box.distance(Point(ring[1]))
                        + deck_box.distance(Point(ring[2])))
            assert high_pair < low_pair, (
                "high corners (0,3) must be the deck-side end of a "
                "descending quad — inverted [H,L,L,H] order")

    def test_deck_corridor_chain_welds_to_plate_exit_edge(
        self, monkeypatch
    ):
        # Defect A regression: the chain's first quad copies the
        # corridor plate's exit-edge ring coordinates VERBATIM (the
        # weld) and keeps the plate's floor value on that edge — no
        # more 1.09 m open seam at the trench inset, no 4-5 m lateral
        # offset.
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        from shapely.geometry import LineString as _LineString
        layout._object_bridge_route_lines = [
            _LineString([(65.0, -80.0), (65.0, 80.0)])]
        setattr(layout, bridges._OBJECT_BRIDGE_ROAD_NETWORKS_ATTRIBUTE,
                [_axial_road_network()])
        bridges.build_bridge_layout_shapes(layout, _FakeDem(150.0), 36, -87)
        bridges._emit_object_sourced_bridge_corridors(
            layout, _FakeDem(150.0), 36, -87,
            _Classification([_bridge()]), [_axial_road_network()],
            road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
        )
        plates = [s for s in layout.shapes
                  if s.ref == "object_bridge_corridor"]
        assert len(plates) == 1
        plate_ring = set(_open_ring(plates[0].polygon))
        approaches = _approach_shapes(layout)
        assert approaches, "the axial road must produce approach chains"
        welded = []
        for shape in approaches:
            shared = set(_open_ring(shape.polygon)) & plate_ring
            if len(shared) >= 2:
                welded.append(shape)
        # The road exits BOTH plate ends — each chain's first quad
        # welds to its end edge.
        assert len(welded) == 2, (
            "each chain's first quad must share the plate end edge "
            f"verbatim (welded quads: {len(welded)})"
        )
        floor = bridges._bridge_corridor_floor_m(_bridge(), 167.0)
        for shape in welded:
            assert shape.polygon.distance(plates[0].polygon) < 1e-9
            # DEM 150 < floor: descending chain, near (plate) edge is
            # the HIGH end and must carry the plate's floor value.
            assert shape.altitude_high == pytest.approx(
                round(floor, 1), abs=0.05)


class TestPortalCollarCoverage:
    def test_collar_spans_full_object_footprint_width(self):
        # Defect C (2026-07-15, KBNA 02C): the collar's lateral extent
        # derives from the object's FULL solid footprint (the captured
        # never-stack building pad), not the narrow deck-face union —
        # measured KBNA: collars covered 86-87 % of the portal back
        # width.  Here the pad is 3x wider than the 20 m deck square;
        # the collar must span >= 95 % of the pad width (the deck-
        # sourced band would reach only 40 of 60 m).
        from auto_patch.layout import BuiltShape as _BuiltShape
        from auto_patch.layout import ROLE_BUILDING
        layout = TestTunnelPortalPairs._paired_layout(
            TestTunnelPortalPairs())
        anchor_x, anchor_y = layout.ll_to_m(*local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, 0.0, 0.0))
        pad = Polygon([
            (anchor_x - 10.0, anchor_y - 30.0),
            (anchor_x + 10.0, anchor_y - 30.0),
            (anchor_x + 10.0, anchor_y + 30.0),
            (anchor_x - 10.0, anchor_y + 30.0),
        ])
        layout.shapes.append(_BuiltShape(
            polygon=pad, role=ROLE_BUILDING, ref="padA"))
        _n_trench, _n_causeway, pads_removed = (
            bridges.build_bridge_layout_shapes(
                layout, _FakeDem(180.0), 36, -87))
        assert pads_removed == 1, "the portal pad must be captured"
        collars = [shape for shape in layout.shapes
                   if shape.ref == "object_tunnel_portal_collar"
                   and shape.polygon.distance(pad) < 30.0]
        assert collars, "portal A must emit a collar"
        lateral_low = min(
            min(y for _x, y in shape.polygon.exterior.coords)
            for shape in collars)
        lateral_high = max(
            max(y for _x, y in shape.polygon.exterior.coords)
            for shape in collars)
        pad_width = 60.0
        assert lateral_high - lateral_low >= 0.95 * pad_width, (
            "collar must span the full object footprint width "
            f"(spanned {lateral_high - lateral_low:.1f} of {pad_width} m)")

    def test_collar_without_pad_keeps_deck_derived_band(self):
        # Fallback: with no captured pad the collar still emits from
        # the deck-face crown source (the pre-defect-C behaviour, now
        # keep-all-parts).
        layout = TestTunnelPortalPairs._paired_layout(
            TestTunnelPortalPairs())
        bridges.build_bridge_layout_shapes(layout, _FakeDem(180.0), 36, -87)
        collars = [shape for shape in layout.shapes
                   if shape.ref == "object_tunnel_portal_collar"]
        assert len(collars) == 2
