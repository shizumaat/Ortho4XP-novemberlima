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
        corridor, suppress, refused, _road_carried = bridges._partition_bridges_for_corridors(
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
        corridor, suppress, refused, _road_carried = bridges._partition_bridges_for_corridors(
            classification
        )
        assert not corridor and len(suppress) == 2 and not refused

    def test_ambiguous_is_refused(self):
        corridor, suppress, refused, _road_carried = bridges._partition_bridges_for_corridors(
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
        corridor, suppress, refused, _road_carried = bridges._partition_bridges_for_corridors(
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

    def test_gate_off_inserts_nothing(self):
        assert config.OBJECT_BRIDGE_TERRAIN is False
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

    def test_validators_gate_off_return_empty(self):
        from auto_patch import verification
        assert config.OBJECT_BRIDGE_TERRAIN is False
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

    def test_gate_off_emits_nothing(self):
        assert config.OBJECT_BRIDGE_TERRAIN is False
        layout = _FakeLayout()
        setattr(layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
                _Classification([_bridge()]))
        assert bridges.build_bridge_layout_shapes(
            layout, None, 36, -87) == (0, 0, 0)
        assert not layout.shapes


class TestRoadCarriedOverpass:
    def test_no_route_on_deck_is_road_carried(self, monkeypatch):
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        # No shape crosses the deck footprint at all.
        corridor, _s, _r, road_carried = (
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
        corridor, _s, _r, road_carried = (
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
        corridor, _s, _r, road_carried = (
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
        corridor, _s, _r, road_carried = (
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
        # Solver: not pavement — the solve never reshapes them.
        from auto_patch.elevation_per_surface.solver_primitives import (
            PAVEMENT_ROLES,
        )
        assert not (new_roles & set(PAVEMENT_ROLES))
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

    def test_road_through_causeway_zone_is_suppressed(self, monkeypatch):
        # A road exiting through the SHORT (abutment) ends runs straight
        # through the causeway zones — every step is keep-out territory
        # and the walk emits nothing there (the causeway owns that
        # ground; audit 5 measured these loop-back rects fighting the
        # 167 plates in the mesh).
        axis_points = []
        for along in (-40.0, 65.0, 171.0):
            latitude, longitude = local_offset_to_lonlat(
                ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, along, 0.0
            )
            axis_points.append(RoadShapePoint(longitude, latitude, 0.0, True))
        axis_network = RoadNetwork(
            network_definitions=["lib/g10/roads_EU.net"],
            segments=[RoadSegment(0, "lib/g10/roads_EU.net", 20, 1, 2,
                                  axis_points)],
            skipped_line_count=0,
        )
        layout = _gate_on_layout_with_bridge(monkeypatch, _bridge())
        layout.shapes.append(_deck_route_shape())
        bridges._emit_object_sourced_bridge_corridors(
            layout, _FakeDem(150.0), 36, -87,
            _Classification([_bridge()]), [axis_network],
            road_width_m=22.0, ramp_step_m=20.0, approach_length_m=80.0,
        )
        assert not [s for s in layout.shapes
                    if s.ref == "object_bridge_approach"]


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
