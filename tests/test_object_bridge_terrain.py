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


class _FakeLayout:
    def __init__(self) -> None:
        self.anchor = ANCHOR
        self.shapes: list = []
        self.icao = "TEST"


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
    """A single fully-draped (level 0) road segment running along the deck
    axis, from x=-40 to x=length+40 at z=0, in the structure frame."""
    shape_points = []
    for along in (-40.0, length_m / 2.0, length_m + 40.0):
        latitude, longitude = local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, along, 0.0
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
        bridge = _bridge()  # deck 167.0, top +5.99, girder +4.2
        floor = bridges._bridge_corridor_floor_m(bridge, 167.0)
        # girder underside = 167 - (5.99 - 4.2) = 165.21; minus 5.1 margin.
        assert floor == pytest.approx(165.21 - 5.1, abs=1e-6)

    def test_missing_underside_treats_thickness_as_zero(self):
        bridge = _bridge(clearance_underside_y_m=None, ceiling_y_m=None)
        floor = bridges._bridge_corridor_floor_m(bridge, 167.0)
        assert floor == pytest.approx(167.0 - config.BRIDGE_ROAD_CLEARANCE_M)

    def test_ceiling_used_when_no_clearance_underside(self):
        bridge = _bridge(clearance_underside_y_m=None, ceiling_y_m=4.8)
        floor = bridges._bridge_corridor_floor_m(bridge, 167.0)
        assert floor == pytest.approx(167.0 - (5.99 - 4.8) - 5.1, abs=1e-6)

    def test_clearance_margin_is_config_driven(self, monkeypatch):
        monkeypatch.setattr(config, "BRIDGE_ROAD_CLEARANCE_M", 4.5)
        bridge = _bridge(clearance_underside_y_m=None, ceiling_y_m=None)
        floor = bridges._bridge_corridor_floor_m(bridge, 100.0)
        assert floor == pytest.approx(100.0 - 4.5)


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
        corridor, suppress, refused = bridges._partition_bridges_for_corridors(
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
        corridor, suppress, refused = bridges._partition_bridges_for_corridors(
            classification
        )
        assert not corridor and len(suppress) == 2 and not refused

    def test_ambiguous_is_refused(self):
        corridor, suppress, refused = bridges._partition_bridges_for_corridors(
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
        corridor, suppress, refused = bridges._partition_bridges_for_corridors(
            classification
        )
        assert len(corridor) == 1 and not suppress


# ---------------------------------------------------------------------------
# object-sourced corridor emission
# ---------------------------------------------------------------------------

class TestObjectSourcedCorridors:
    def test_deck_carried_emits_corridor_at_object_floor(self):
        layout = _FakeLayout()
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
        # A flat under-deck plate at the object floor (160.11) was emitted.
        floor = round(165.21 - 5.1, 1)
        plates = [
            s for s in layout.shapes
            if s.ref == "object_bridge_corridor"
        ]
        assert plates, "expected an under-deck corridor plate"
        assert any(
            getattr(s, "altitude", None) == floor for s in plates
        ), f"expected a plate at {floor} m, got {[s.altitude for s in plates]}"
        # Approach ramps stepping the floor up toward the DEM were emitted.
        assert any(
            s.ref == "object_bridge_approach" for s in layout.shapes
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
        classification = _Classification([_bridge()])
        count, _s, _c = bridges._emit_object_sourced_bridge_corridors(
            layout, _FakeDem(150.0), 36, -87, classification, [elevated],
            road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
        )
        assert count == 0
        assert calls["osm"] == 1  # OSM fallback was consulted once


# ---------------------------------------------------------------------------
# gate-off neutrality
# ---------------------------------------------------------------------------

class TestGateOff:
    def test_attach_is_noop_with_gate_off(self):
        # Default config gate is off; the assembler attaches nothing.
        assert config.OBJECT_BRIDGE_TERRAIN is False
        layout = _FakeLayout()
        layout.apt_dat_path = "/nonexistent/Earth nav data/apt.dat"
        result = assembly.attach_bridge_classification(layout, "/nonexistent")
        assert result is None
        assert not hasattr(layout, assembly.CLASSIFICATION_ATTRIBUTE)

    def test_classification_reader_ignores_attribute_when_gate_off(self):
        layout = _FakeLayout()
        setattr(
            layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
            _Classification([_bridge()]),
        )
        # Gate off (default): the reader returns None even though the
        # attribute is present, so the emitters take the legacy path.
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

        assert config.OBJECT_BRIDGE_TERRAIN is False  # default gate

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
