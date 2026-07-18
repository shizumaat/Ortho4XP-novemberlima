"""Workstream W-R3 tests for ``auto_patch.object_terrain_features`` (§3.1)
plus the Part-1 hard-surface tracking added to the OBJ8 loader.

Fixtures are synthetic (ruling R6): geometry is built in code, and the
loader's hardness state machine is exercised through synthetic OBJ8 text
written by the tests — no third-party pack content enters the repository.
A skip-guarded smoke section runs the classifier against the real EGLL /
KBNA / EDDF dumps in the session scratchpad when they are present, mirroring
``TestKbnaSmoke`` in ``tests/test_dsf_road_network.py``.
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "src"))
_TOOLS = os.path.normpath(os.path.join(_HERE, "..", "tools"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
if _TOOLS not in sys.path:
    sys.path.append(_TOOLS)

from shapely.geometry import Polygon  # noqa: E402

from auto_patch import object_terrain_features as otf  # noqa: E402
from auto_patch import obj8_reader  # noqa: E402
from auto_patch.obj8_reader import (  # noqa: E402
    ObjectGeometry,
    ObjectPlacement,
    load_object_file,
    local_offset_to_lonlat,
)
import obj8_geometry as prototype_reader  # noqa: E402

ANCHOR_LATITUDE = 51.470
ANCHOR_LONGITUDE = -0.480


# ---------------------------------------------------------------------------
# geometry builders (synthetic, in the object's local metre frame, heading 0)
# ---------------------------------------------------------------------------

class _GeometryBuilder:
    """Accumulate flat rectangular strips (2 triangles each) and vertical
    walls into an :class:`ObjectGeometry`, tagging per-triangle hardness."""

    def __init__(self) -> None:
        self.vertices: list[tuple[float, float, float]] = []
        self.solid: list[tuple[int, int, int]] = []
        self.hardness: list[str] = []

    def _vertex(self, x: float, y: float, z: float) -> int:
        self.vertices.append((x, y, z))
        return len(self.vertices) - 1

    def add_horizontal_rectangle(
        self,
        x0: float,
        x1: float,
        z0: float,
        z1: float,
        y: float,
        *,
        hardness: str = "",
        segments: int = 1,
    ) -> None:
        """A flat, up-facing rectangle at height ``y``, optionally split into
        ``segments`` along x so features have several sampling triangles."""
        for segment in range(segments):
            sx0 = x0 + (x1 - x0) * segment / segments
            sx1 = x0 + (x1 - x0) * (segment + 1) / segments
            a = self._vertex(sx0, y, z0)
            b = self._vertex(sx1, y, z0)
            c = self._vertex(sx1, y, z1)
            d = self._vertex(sx0, y, z1)
            self.solid.append((a, b, c))
            self.solid.append((a, c, d))
            self.hardness.extend([hardness, hardness])

    def add_sloped_rectangle(
        self,
        x0: float,
        x1: float,
        z0: float,
        z1: float,
        y0: float,
        y1: float,
        *,
        hardness: str = "",
        segments: int = 1,
    ) -> None:
        """An up-facing rectangle whose height varies linearly from ``y0``
        at ``x0`` to ``y1`` at ``x1`` — a bridge approach ramp."""
        for segment in range(segments):
            fraction_start = segment / segments
            fraction_end = (segment + 1) / segments
            sx0 = x0 + (x1 - x0) * fraction_start
            sx1 = x0 + (x1 - x0) * fraction_end
            sy0 = y0 + (y1 - y0) * fraction_start
            sy1 = y0 + (y1 - y0) * fraction_end
            a = self._vertex(sx0, sy0, z0)
            b = self._vertex(sx1, sy1, z0)
            c = self._vertex(sx1, sy1, z1)
            d = self._vertex(sx0, sy0, z1)
            self.solid.append((a, b, c))
            self.solid.append((a, c, d))
            self.hardness.extend([hardness, hardness])

    def add_vertical_wall(
        self, x: float, z0: float, z1: float, y0: float, y1: float
    ) -> None:
        """A vertical wall (near-vertical normal) spanning ``y0..y1`` — a
        pier/abutment reaching the ground, invisible to the near-horizontal
        face tests."""
        a = self._vertex(x, y0, z0)
        b = self._vertex(x, y0, z1)
        c = self._vertex(x, y1, z1)
        d = self._vertex(x, y1, z0)
        self.solid.append((a, b, c))
        self.solid.append((a, c, d))
        self.hardness.extend(["", ""])

    def build(self) -> ObjectGeometry:
        return ObjectGeometry(
            vertices=list(self.vertices),
            solid_triangles=list(self.solid),
            draped_triangles=[],
            positional_commands=[],
            animation_block_count=0,
            level_of_detail_count=0,
            vertex_line_indices=list(range(len(self.vertices))),
            solid_triangle_hardness=tuple(self.hardness),
        )


def _placement(
    resource_path: str,
    *,
    above_ground_level_metres: float = 0.0,
    placement_kind: str = "OBJECT",
    mean_sea_level_elevation_m: float | None = None,
    longitude: float = ANCHOR_LONGITUDE,
    latitude: float = ANCHOR_LATITUDE,
) -> ObjectPlacement:
    return ObjectPlacement(
        definition_index=0,
        resource_path=resource_path,
        longitude=longitude,
        latitude=latitude,
        heading_degrees=0.0,
        above_ground_level_metres=above_ground_level_metres,
        placement_kind=placement_kind,
        mean_sea_level_elevation_m=mean_sea_level_elevation_m,
    )


def _frame_rectangle_to_pavement_polygon(
    x0: float, x1: float, z0: float, z1: float
) -> Polygon:
    """A ``(longitude, latitude)`` pavement polygon covering a frame
    rectangle, for the contract coverage test (the frame origin is the
    single test anchor, so the map is the identity round trip)."""
    corners = [(x0, z0), (x1, z0), (x1, z1), (x0, z1)]
    ring = []
    for frame_x, frame_z in corners:
        latitude, longitude = local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, frame_x, frame_z
        )
        ring.append((longitude, latitude))
    return Polygon(ring)


# ---------------------------------------------------------------------------
# Part 1 — hard-surface tracking in the loader
# ---------------------------------------------------------------------------

def _write_obj8(path, *body_lines: str) -> None:
    header = ["A", "800", "OBJ", ""]
    path.write_text("\n".join(header + list(body_lines)) + "\n")


class TestHardnessStateMachine:
    def _synthetic_object(self, tmp_path, separator: str):
        """A quad's worth of vertices then three TRIS groups: a plain group,
        an ATTR_hard_deck group, and an ATTR_no_hard group after it."""
        def row(*tokens: str) -> str:
            return separator.join(tokens)

        return [
            row("VT", "0", "0", "0", "0", "1", "0", "0", "0"),
            row("VT", "1", "0", "0", "0", "1", "0", "0", "0"),
            row("VT", "1", "0", "1", "0", "1", "0", "0", "0"),
            row("VT", "0", "0", "1", "0", "1", "0", "0", "0"),
            "IDX 0", "IDX 1", "IDX 2", "IDX 0", "IDX 2", "IDX 3",
            "IDX 0", "IDX 1", "IDX 2",
            "TRIS 0 3",            # one plain (not hard) triangle
            "ATTR_hard_deck",
            "TRIS 3 3",            # one hard_deck triangle
            "ATTR_no_hard",
            "TRIS 6 3",            # one triangle after hard cleared
        ]

    def test_hardness_parallel_to_solid_triangles(self, tmp_path):
        path = tmp_path / "hard.obj"
        _write_obj8(path, *self._synthetic_object(tmp_path, " "))
        geometry = load_object_file(str(path))
        assert len(geometry.solid_triangles) == 3
        assert geometry.solid_triangle_hardness == ("", "hard_deck", "")
        assert geometry.hard_deck_solid_triangles() == [
            geometry.solid_triangles[1]
        ]

    def test_attr_hard_and_no_hard_transitions(self, tmp_path):
        path = tmp_path / "hard2.obj"
        _write_obj8(
            path,
            "VT 0 0 0 0 1 0 0 0",
            "VT 1 0 0 0 1 0 0 0",
            "VT 1 0 1 0 1 0 0 0",
            "IDX 0", "IDX 1", "IDX 2",
            "IDX 0", "IDX 1", "IDX 2",
            "IDX 0", "IDX 1", "IDX 2",
            "ATTR_hard concrete",   # trailing surface token ignored
            "TRIS 0 3",
            "ATTR_no_hard",
            "TRIS 3 3",
            "ATTR_hard_deck asphalt",
            "TRIS 6 3",
        )
        geometry = load_object_file(str(path))
        assert geometry.solid_triangle_hardness == ("hard", "", "hard_deck")

    def test_tab_separated_hardness(self, tmp_path):
        path = tmp_path / "hard_tab.obj"
        _write_obj8(path, *self._synthetic_object(tmp_path, "\t"))
        geometry = load_object_file(str(path))
        assert geometry.solid_triangle_hardness == ("", "hard_deck", "")

    def test_default_hardness_is_empty_and_defensive(self):
        """Hand-constructed geometry passes no hardness; the recovery helper
        must not index past the empty tuple."""
        geometry = ObjectGeometry(
            vertices=[(0, 0, 0), (1, 0, 0), (1, 0, 1)],
            solid_triangles=[(0, 1, 2)],
            draped_triangles=[],
            positional_commands=[],
            animation_block_count=0,
            level_of_detail_count=0,
            vertex_line_indices=[0, 1, 2],
        )
        assert geometry.solid_triangle_hardness == ()
        assert geometry.hard_deck_solid_triangles() == []

    def test_production_prototype_hardness_parity(self, tmp_path):
        """The loader change is mirrored in tools/obj8_geometry.py."""
        path = tmp_path / "parity.obj"
        _write_obj8(path, *self._synthetic_object(tmp_path, " "))
        production = load_object_file(str(path))
        prototype = prototype_reader.load_object_file(str(path))
        assert (
            production.solid_triangle_hardness
            == prototype.solid_triangle_hardness
        )
        assert (
            production.hard_deck_solid_triangles()
            == prototype.hard_deck_solid_triangles()
        )


# ---------------------------------------------------------------------------
# Part 2 — tunnels
# ---------------------------------------------------------------------------

def _roofed_shell_tunnel_geometry() -> ObjectGeometry:
    """Roof slab at grade over the middle; deck at -5 running the full
    length, so the two uncovered ends are open mouths."""
    builder = _GeometryBuilder()
    # Deck at -5 over x in [-30, 30], z in [-10, 10], six segments.
    builder.add_horizontal_rectangle(
        -30, 30, -10, 10, -5.0, hardness="hard_deck", segments=6
    )
    # Roof at grade over the middle third x in [-10, 10].
    builder.add_horizontal_rectangle(
        -10, 10, -10, 10, 0.0, hardness="hard_deck", segments=2
    )
    return builder.build()


class TestTunnelRecognition:
    def test_roofed_shell_two_mouths(self):
        geometry = _roofed_shell_tunnel_geometry()
        placements = [_placement("tunnel/roofed.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"tunnel/roofed.obj": geometry}, pack_root="PACK"
        )
        assert len(result.tunnels) == 1
        assert result.bridges == []
        tunnel = result.tunnels[0]
        assert len(tunnel.mouth_polygons) == 2
        for mouth in tunnel.mouth_polygons:
            # Each open end is ~20 x 20 m minus the roof-dilation buffer.
            assert 300.0 < mouth.area < 450.0
            assert mouth.is_valid and not mouth.is_empty
            assert mouth.exterior.is_ring
        assert tunnel.body_depth_m == pytest.approx(5.0, abs=0.2)
        for sample in tunnel.mouth_depth_samples:
            assert sample.sample_count > 0
            assert sample.mean_depth_m == pytest.approx(5.0, abs=0.2)

    def test_negative_agl_tunnel_authored_above_zero(self):
        """Geometry authored 0..+7, placed at OBJECT_AGL -7: the effective
        grade plane is at -offset, so the same roof/deck/mouths emerge."""
        builder = _GeometryBuilder()
        # Deck authored at +2 (effective -5), roof authored at +7 (grade).
        builder.add_horizontal_rectangle(
            -30, 30, -10, 10, 2.0, hardness="hard_deck", segments=6
        )
        builder.add_horizontal_rectangle(
            -10, 10, -10, 10, 7.0, hardness="hard_deck", segments=2
        )
        geometry = builder.build()
        placements = [
            _placement(
                "tunnel/agl.obj",
                above_ground_level_metres=-7.0,
                placement_kind="OBJECT_AGL",
            )
        ]
        result = otf.classify_object_terrain_features(
            placements, {"tunnel/agl.obj": geometry}
        )
        assert len(result.tunnels) == 1
        tunnel = result.tunnels[0]
        assert tunnel.placement_kind == "OBJECT_AGL"
        assert tunnel.above_ground_offset_m == pytest.approx(-7.0)
        assert len(tunnel.mouth_polygons) == 2
        assert tunnel.body_depth_m == pytest.approx(5.0, abs=0.2)

    def test_shell_and_deck_as_separate_resources_group(self):
        """A tunnel arriving as a roof object and a deck object on one anchor
        is grouped into one structure (spec: EGLL N.obj / Na.obj)."""
        roof = _GeometryBuilder()
        roof.add_horizontal_rectangle(
            -10, 10, -10, 10, 0.0, hardness="hard_deck", segments=2
        )
        deck = _GeometryBuilder()
        deck.add_horizontal_rectangle(
            -30, 30, -10, 10, -5.0, hardness="hard_deck", segments=6
        )
        placements = [
            _placement("tunnel/roof.obj"),
            _placement("tunnel/deck.obj"),
        ]
        result = otf.classify_object_terrain_features(
            placements,
            {"tunnel/roof.obj": roof.build(), "tunnel/deck.obj": deck.build()},
            pack_root="PACK",
        )
        assert len(result.tunnels) == 1
        assert result.tunnels[0].object_resources == [
            "tunnel/deck.obj",
            "tunnel/roof.obj",
        ]
        assert len(result.tunnels[0].mouth_polygons) == 2


# ---------------------------------------------------------------------------
# Part 2 — bridges
# ---------------------------------------------------------------------------

def _hard_deck_bridge_geometry() -> ObjectGeometry:
    """Deck plane at +6, girder ceiling at +4.2, walls to the ground at the
    two ends."""
    builder = _GeometryBuilder()
    builder.add_horizontal_rectangle(
        -20, 20, -5, 5, 6.0, hardness="hard_deck", segments=8
    )
    builder.add_horizontal_rectangle(
        -20, 20, -5, 5, 4.2, hardness="", segments=8
    )
    builder.add_vertical_wall(-20, -5, 5, 0.0, 6.0)
    builder.add_vertical_wall(20, -5, 5, 0.0, 6.0)
    return builder.build()


class TestBridgeRecognition:
    def test_hard_deck_bridge_dimensions_and_clearance(self):
        geometry = _hard_deck_bridge_geometry()
        placements = [_placement("bridge/hard.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"bridge/hard.obj": geometry}, pack_root="PACK"
        )
        assert result.tunnels == []
        assert result.refusals == []
        assert len(result.bridges) == 1
        bridge = result.bridges[0]
        assert bridge.hard_deck is True
        assert bridge.deck_hardness == otf.DECK_HARDNESS_HARD_DECK
        assert bridge.deck_top_y_m == pytest.approx(6.0, abs=0.1)
        assert bridge.ceiling_y_m == pytest.approx(4.2, abs=0.2)
        assert bridge.clearance_underside_y_m == pytest.approx(4.2, abs=0.2)
        assert bridge.deck_length_m == pytest.approx(40.0, abs=1.0)
        assert bridge.deck_width_m == pytest.approx(10.0, abs=1.0)
        assert len(bridge.abutment_lines) == 2
        assert bridge.abutment_reaches_grade == (True, True)
        # Flat deck: profile constant, ends equal the crest.
        assert bridge.deck_end_elevations_y_m[0] == pytest.approx(6.0, abs=0.1)
        assert bridge.deck_end_elevations_y_m[1] == pytest.approx(6.0, abs=0.1)
        for _along, height in bridge.deck_top_profile:
            assert height == pytest.approx(6.0, abs=0.1)
        # No pavement supplied → flat profile → the crest-height
        # cross-check governs.
        assert bridge.contract == otf.DECK_CARRIED
        assert result.exclusions == [("PACK", "bridge/hard.obj")]

    def test_plain_attr_hard_deck_is_first_class(self):
        """Amendment A4: a drivable deck marked plain ATTR_hard (KMCO
        puente class, zero hard_deck triangles) is a first-class deck;
        ``deck_hardness`` says "hard" and the R8 flush-seating boolean
        stays False."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -20, 20, -5, 5, 6.0, hardness="hard", segments=8
        )
        builder.add_vertical_wall(-20, -5, 5, 0.0, 6.0)
        builder.add_vertical_wall(20, -5, 5, 0.0, 6.0)
        placements = [_placement("Buildings/puente_synthetic.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"Buildings/puente_synthetic.obj": builder.build()}
        )
        assert len(result.bridges) == 1
        bridge = result.bridges[0]
        assert bridge.deck_hardness == otf.DECK_HARDNESS_HARD
        assert bridge.hard_deck is False
        assert bridge.deck_top_y_m == pytest.approx(6.0, abs=0.1)

    def test_cosmetic_bridge_has_no_hard_deck(self):
        """No hard attributes, but a broad elevated surface, ground contact
        and a 'bridge' name → recognized, hard_deck False (Murfreesboro)."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -20, 20, -5, 5, 6.0, hardness="", segments=8
        )
        builder.add_vertical_wall(-20, -5, 5, 0.0, 6.0)
        builder.add_vertical_wall(20, -5, 5, 0.0, 6.0)
        placements = [_placement("Objects/Service_Bridge.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"Objects/Service_Bridge.obj": builder.build()}
        )
        assert len(result.bridges) == 1
        assert result.bridges[0].hard_deck is False
        assert result.bridges[0].deck_hardness == otf.DECK_HARDNESS_COSMETIC

    def test_name_hint_never_gates_a_hard_structure(self):
        """A structure WITH hard triangles below the deck-area floor is
        railing clutter, not a cosmetic bridge — the name-hint path applies
        only to structures with NO hard geometry (round-2 change 1)."""
        builder = _GeometryBuilder()
        # Elevated non-hard surface, plus a tiny hard railing strip.
        builder.add_horizontal_rectangle(
            -20, 20, -5, 5, 6.0, hardness="", segments=8
        )
        builder.add_horizontal_rectangle(
            -20, 20, -5.5, -5.0, 7.0, hardness="hard", segments=2
        )
        builder.add_vertical_wall(-20, -5, 5, 0.0, 6.0)
        builder.add_vertical_wall(20, -5, 5, 0.0, 6.0)
        placements = [_placement("Objects/Named_Bridge.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"Objects/Named_Bridge.obj": builder.build()}
        )
        assert result.bridges == []

    def test_no_hard_deck_and_no_name_hint_is_not_a_bridge(self):
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -20, 20, -5, 5, 6.0, hardness="", segments=8
        )
        builder.add_vertical_wall(-20, -5, 5, 0.0, 6.0)
        placements = [_placement("Objects/rooftop_platform.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"Objects/rooftop_platform.obj": builder.build()}
        )
        assert result.bridges == []
        assert result.tunnels == []

    def test_terrain_carried_flush_deck_with_pavement(self):
        """Deck flush at grade with pavement draping across the mid-span →
        TERRAIN_CARRIED."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -20, 20, -5, 5, -0.02, hardness="hard_deck", segments=8
        )
        geometry = builder.build()
        pavement = _frame_rectangle_to_pavement_polygon(-25, 25, -8, 8)
        placements = [_placement("bridge/flush.obj")]
        result = otf.classify_object_terrain_features(
            placements,
            {"bridge/flush.obj": geometry},
            pavement_polygons_longitude_latitude=[pavement],
        )
        assert len(result.bridges) == 1
        bridge = result.bridges[0]
        assert bridge.hard_deck is True
        assert bridge.contract == otf.TERRAIN_CARRIED

    def test_deck_carried_no_pavement_mid_span(self):
        """Elevated deck with pavement only off the span → DECK_CARRIED."""
        geometry = _hard_deck_bridge_geometry()
        # Pavement well clear of the deck box (no mid-span coverage).
        pavement = _frame_rectangle_to_pavement_polygon(200, 260, 200, 260)
        placements = [_placement("bridge/hard.obj")]
        result = otf.classify_object_terrain_features(
            placements,
            {"bridge/hard.obj": geometry},
            pavement_polygons_longitude_latitude=[pavement],
        )
        assert result.bridges[0].contract == otf.DECK_CARRIED

    def test_msl_median_on_deck(self):
        """Only the OBJECT_MSL fixtures whose lon/lat fall on the deck count
        toward the absolute deck elevation median."""
        geometry = _hard_deck_bridge_geometry()
        on_deck = []
        for offset, elevation in ((-10.0, 166.9), (0.0, 167.0), (10.0, 167.1)):
            latitude, longitude = local_offset_to_lonlat(
                ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, offset, 0.0
            )
            on_deck.append(
                _placement(
                    "bridge/hard.obj",
                    placement_kind="OBJECT_MSL",
                    mean_sea_level_elevation_m=elevation,
                    longitude=longitude,
                    latitude=latitude,
                )
            )
        far_latitude, far_longitude = local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, 500.0, 500.0
        )
        off_deck = _placement(
            "bridge/hard.obj",
            placement_kind="OBJECT_MSL",
            mean_sea_level_elevation_m=999.0,
            longitude=far_longitude,
            latitude=far_latitude,
        )
        result = otf.classify_object_terrain_features(
            [_placement("bridge/hard.obj")],
            {"bridge/hard.obj": geometry},
            mean_sea_level_placements=on_deck + [off_deck],
        )
        assert result.bridges[0].absolute_deck_elevation_m == pytest.approx(
            167.0, abs=0.01
        )


# ---------------------------------------------------------------------------
# crowned profiles and PROFILE_CARRIED (amendments A2 / A4, ruling R9)
# ---------------------------------------------------------------------------

def _crowned_bridge_geometry() -> ObjectGeometry:
    """KMCO-class hump: plain-ATTR_hard drivable surface rising 0.5 → 5.0
    over 30 m, a flat 20 m crest at 5.0, and descending back to 0.5 — the
    deck itself reaches grade at both ends (grounded abutments)."""
    builder = _GeometryBuilder()
    builder.add_sloped_rectangle(
        -40, -10, -5, 5, 0.5, 5.0, hardness="hard", segments=6
    )
    builder.add_horizontal_rectangle(
        -10, 10, -5, 5, 5.0, hardness="hard", segments=4
    )
    builder.add_sloped_rectangle(
        10, 40, -5, 5, 5.0, 0.5, hardness="hard", segments=6
    )
    # Abutment cladding reaching grade at both ends.
    builder.add_vertical_wall(-40, -5, 5, 0.0, 0.5)
    builder.add_vertical_wall(40, -5, 5, 0.0, 0.5)
    return builder.build()


class TestCrownedProfile:
    def test_profile_bins_capture_crest_and_ends(self):
        placements = [_placement("Buildings/hump.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"Buildings/hump.obj": _crowned_bridge_geometry()}
        )
        assert len(result.bridges) == 1
        bridge = result.bridges[0]
        assert bridge.deck_top_y_m == pytest.approx(5.0, abs=0.2)
        # The end bins hold the ramp's first ~10 m: their maxima sit just
        # above the true 0.5 m end elevation (a 15% ramp gains ~1.5 m per
        # bin), and far below the crest.
        for end_elevation in bridge.deck_end_elevations_y_m:
            assert 0.4 <= end_elevation <= 2.2
        # ~10 m bins over the 80 m deck; monotone rise to the crest then
        # monotone fall.
        heights = [height for _along, height in bridge.deck_top_profile]
        assert 6 <= len(heights) <= 10
        crest_index = heights.index(max(heights))
        assert all(
            heights[index] <= heights[index + 1] + 0.01
            for index in range(crest_index)
        )
        assert all(
            heights[index] >= heights[index + 1] - 0.01
            for index in range(crest_index, len(heights) - 1)
        )

    def test_profile_carried_with_continuous_pavement(self):
        pavement = _frame_rectangle_to_pavement_polygon(-45, 45, -8, 8)
        placements = [_placement("Buildings/hump.obj")]
        result = otf.classify_object_terrain_features(
            placements,
            {"Buildings/hump.obj": _crowned_bridge_geometry()},
            pavement_polygons_longitude_latitude=[pavement],
        )
        assert result.bridges[0].contract == otf.PROFILE_CARRIED

    def test_profile_carried_without_pavement(self):
        """No-pavement fallback (round-2 change 4): a non-flat profile with
        grounded abutments is PROFILE_CARRIED."""
        placements = [_placement("Buildings/hump.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"Buildings/hump.obj": _crowned_bridge_geometry()}
        )
        assert result.bridges[0].contract == otf.PROFILE_CARRIED


# ---------------------------------------------------------------------------
# viaduct refusal — the per-end abutment-reaches-grade test (amendment A4)
# ---------------------------------------------------------------------------

class TestViaductRefusal:
    def test_piered_viaduct_refused_entirely(self):
        """A deck whose solid geometry never reaches grade (KMCO via_tren,
        global minimum +3.45) is refused — no bridge record, no exclusion,
        one refusal with the reason."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -40, 40, -5, 5, 5.0, hardness="hard", segments=8
        )
        # Piers that stop well above grade.
        builder.add_vertical_wall(-40, -5, 5, 3.45, 5.0)
        builder.add_vertical_wall(40, -5, 5, 3.45, 5.0)
        placements = [_placement("Buildings/via_tren_synthetic.obj")]
        result = otf.classify_object_terrain_features(
            placements,
            {"Buildings/via_tren_synthetic.obj": builder.build()},
            pack_root="PACK",
        )
        assert result.bridges == []
        assert result.tunnels == []
        assert result.exclusions == []
        assert len(result.refusals) == 1
        refusal = result.refusals[0]
        assert refusal.object_resources == [
            "Buildings/via_tren_synthetic.obj"
        ]
        assert "piered viaduct" in refusal.reason

    def test_one_grounded_end_is_still_refused(self):
        """Grade contact near ONE end only fails the per-end test — both
        ends must ground (a half-causeway pin is as false as none)."""
        builder = _GeometryBuilder()
        # 100 m deck; grounded cladding at the start end only (the far end
        # midpoint is ~100 m from it, far past the 35 m search radius).
        builder.add_horizontal_rectangle(
            0, 100, -5, 5, 5.0, hardness="hard", segments=10
        )
        builder.add_vertical_wall(0, -5, 5, 0.0, 5.0)
        placements = [_placement("Buildings/half_bridge.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"Buildings/half_bridge.obj": builder.build()}
        )
        assert result.bridges == []
        assert len(result.refusals) == 1
        assert "far" in result.refusals[0].reason

    def test_deck_carried_ends_on_structure_still_pass(self):
        """The KBNA distinction: a deck-carried deck ENDS at +6 — the deck
        never reaches grade — but the abutment embankment cladding grounds
        NEAR the ends, and that is what the test looks for."""
        geometry = _hard_deck_bridge_geometry()  # walls ground at both ends
        placements = [_placement("bridge/hard.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"bridge/hard.obj": geometry}
        )
        assert len(result.bridges) == 1
        assert result.bridges[0].abutment_reaches_grade == (True, True)
        assert result.refusals == []


# ---------------------------------------------------------------------------
# tunnel-versus-basement discrimination (supervisor addendum, EGLL
# author-mesh correlation: depth is INVERTED as a signal)
# ---------------------------------------------------------------------------

class TestBasementNotTunnel:
    def test_buried_basement_floor_is_not_a_tunnel(self):
        """A building basement: broad NON-hard floor well below grade
        (T2_T3_3 class).  Below-grade area alone must not read as a tunnel
        — only a DRIVABLE (hard) below-grade surface does."""
        builder = _GeometryBuilder()
        # 400 m² non-hard basement floor at -5.
        builder.add_horizontal_rectangle(
            -10, 10, -10, 10, -5.0, hardness="", segments=4
        )
        # The building above grade.
        builder.add_horizontal_rectangle(
            -10, 10, -10, 10, 12.0, hardness="", segments=2
        )
        placements = [_placement("Airport/T2_3/basement_synthetic.obj")]
        result = otf.classify_object_terrain_features(
            placements,
            {"Airport/T2_3/basement_synthetic.obj": builder.build()},
        )
        assert result.tunnels == []
        assert result.bridges == []

    def test_hard_below_grade_deck_is_a_tunnel(self):
        """The same floor marked drivable IS a tunnel body."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -10, 10, -10, 10, -5.0, hardness="hard_deck", segments=4
        )
        placements = [_placement("Airport/Tunnel/synthetic.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"Airport/Tunnel/synthetic.obj": builder.build()}
        )
        assert len(result.tunnels) == 1

    def test_at_grade_hard_road_is_not_a_tunnel(self):
        """The EGLL ROADT23 class: hard_deck at grade, zero below-grade
        hard area — excluded by the below-grade requirement."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -30, 30, -5, 5, 0.0, hardness="hard_deck", segments=6
        )
        placements = [_placement("Airport/T2_3/ROADT23_synthetic.obj")]
        result = otf.classify_object_terrain_features(
            placements,
            {"Airport/T2_3/ROADT23_synthetic.obj": builder.build()},
        )
        assert result.tunnels == []

    def test_agl_threshold_is_one_metre(self):
        """EGLL tunnel 10 sits at OBJECT_AGL exactly -1.0 with NO hard
        triangles: the AGL clause must fire at -1.0."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -10, 10, -5, 5, 0.5, hardness="", segments=2
        )
        placements = [
            _placement(
                "Airport/Tunnel/10_synthetic.obj",
                above_ground_level_metres=-1.0,
                placement_kind="OBJECT_AGL",
            )
        ]
        result = otf.classify_object_terrain_features(
            placements,
            {"Airport/Tunnel/10_synthetic.obj": builder.build()},
        )
        assert len(result.tunnels) == 1


# ---------------------------------------------------------------------------
# stock-library exclusion + AGL-limb above-grade height cap (2026-07-18,
# tile +51-001: the EGKR Redhill control tower and the EGKK oil rig)
# ---------------------------------------------------------------------------

class TestStockLibraryExclusion:
    def test_predicate(self):
        assert otf.is_stock_library_resource(
            "lib/airport/control_towers/small/16m_Norway.obj"
        )
        assert otf.is_stock_library_resource("lib/ships/OilRig.obj")
        assert otf.is_stock_library_resource("LIB\\ships\\OilRig.obj")
        assert otf.is_stock_library_resource("./lib/cars/car.obj")
        assert not otf.is_stock_library_resource("Airport/Tunnel/6.obj")
        assert not otf.is_stock_library_resource("mylib/tunnel.obj")
        assert not otf.is_stock_library_resource("library/tunnel.obj")

    def test_library_tunnel_shape_is_not_consumed(self):
        """The roofed-shell geometry classifies as a tunnel under a pack
        path (TestTunnelRecognition) — the SAME geometry under a ``lib/``
        virtual path must produce nothing at all: a stock catalogue asset
        is never a pack-authored terrain shell."""
        geometry = _roofed_shell_tunnel_geometry()
        resource = "lib/airport/control_towers/small/16m_Norway.obj"
        result = otf.classify_object_terrain_features(
            [_placement(resource)], {resource: geometry}, pack_root="PACK"
        )
        assert result.tunnels == []
        assert result.bridges == []
        assert result.refusals == []
        assert result.exclusions == []
        assert result.ground_interfaces == []

    def test_library_bridge_shape_is_not_consumed(self):
        """The hard-deck bridge geometry under a ``lib/`` path (the EGKK
        oil rig class: deck on legs) must not classify as a bridge."""
        geometry = _hard_deck_bridge_geometry()
        resource = "lib/ships/OilRig.obj"
        result = otf.classify_object_terrain_features(
            [_placement(resource)], {resource: geometry}, pack_root="PACK"
        )
        assert result.bridges == []
        assert result.tunnels == []
        assert result.exclusions == []


class TestAglBuriedBuildingNotTunnel:
    def test_buried_tower_stands_above_grade(self):
        """EGKR Redhill (measured 2026-07-18): a control tower placed at
        OBJECT_AGL -4 carries real below-grade horizontal area (its buried
        floors) yet stands far above effective grade — the AGL limb must
        refuse anything reaching over
        TUNNEL_AGL_MAX_ABOVE_GRADE_HEIGHT_M."""
        builder = _GeometryBuilder()
        # Buried ground floor: authored 0, effective -4 — 100 m² of
        # below-grade near-horizontal area, well over the 25 m² gate.
        builder.add_horizontal_rectangle(-5, 5, -5, 5, 0.0, segments=2)
        # The tower roof: authored +16, effective +12.
        builder.add_horizontal_rectangle(-5, 5, -5, 5, 16.0, segments=2)
        resource = "Airport/Towers/tower_synthetic.obj"
        placements = [
            _placement(
                resource,
                above_ground_level_metres=-4.0,
                placement_kind="OBJECT_AGL",
            )
        ]
        result = otf.classify_object_terrain_features(
            placements, {resource: builder.build()}
        )
        assert result.tunnels == []

    def test_shell_with_low_parapet_still_fires(self):
        """A true AGL shell may poke slightly above grade (EGLL Tunnel/10
        tops out at +0.84 m effective): a +1.5 m parapet stays under the
        2.0 m cap and the limb must still fire."""
        builder = _GeometryBuilder()
        # Below-grade deck: authored 0, effective -2 — 200 m².
        builder.add_horizontal_rectangle(-10, 10, -5, 5, 0.0, segments=2)
        # Low parapet top: authored +3.5, effective +1.5.
        builder.add_horizontal_rectangle(-10, 10, -5, -4, 3.5, segments=2)
        resource = "Airport/Tunnel/parapet_synthetic.obj"
        placements = [
            _placement(
                resource,
                above_ground_level_metres=-2.0,
                placement_kind="OBJECT_AGL",
            )
        ]
        result = otf.classify_object_terrain_features(
            placements, {resource: builder.build()}
        )
        assert len(result.tunnels) == 1


class TestRealStockLibrarySmoke:
    def test_norway_tower_at_redhill_offset(self):
        """The real EGKR misclassification, end to end: the stock 16 m
        Norway control tower at its Global-Airports OBJECT_AGL -4.0 must
        classify as nothing — and even under a non-library alias the AGL
        height cap alone must refuse it (defense in depth)."""
        xplane_root = os.environ.get("XPLANE_ROOT", "/Users/noah/X-Plane 12")
        virtual = "lib/airport/control_towers/small/16m_Norway.obj"
        physical = (
            obj8_reader.resolve_object_resource(virtual, None, xplane_root)
            if os.path.isdir(xplane_root)
            else None
        )
        if not physical:
            pytest.skip("X-Plane default library not available")
        geometry = load_object_file(physical)
        placement = _placement(
            virtual,
            above_ground_level_metres=-4.0,
            placement_kind="OBJECT_AGL",
            longitude=-0.13837,
            latitude=51.21598,
        )
        result = otf.classify_object_terrain_features(
            [placement], {virtual: geometry}
        )
        assert result.tunnels == []
        assert result.exclusions == []

        alias = "Airport/Towers/norway_16m_alias.obj"
        aliased = otf.classify_object_terrain_features(
            [placement._replace(resource_path=alias)], {alias: geometry}
        )
        assert aliased.tunnels == []


# ---------------------------------------------------------------------------
# clearance planes: largest-area ceiling versus lowest limiting underside
# ---------------------------------------------------------------------------

class TestClearancePlanes:
    def test_lowest_underside_plane_limits_clearance(self):
        """KBNA shape: broad slab underside at +4.8, narrower girder line
        at +4.2 → ceiling_y_m = 4.8 (largest), clearance_underside_y_m =
        4.2 (lowest above the opening)."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -20, 20, -5, 5, 6.0, hardness="hard_deck", segments=8
        )
        # Broad slab underside.
        builder.add_horizontal_rectangle(
            -20, 20, -5, 5, 4.8, hardness="", segments=8
        )
        # Narrower girder line, lower.
        builder.add_horizontal_rectangle(
            -20, 20, -2, 2, 4.2, hardness="", segments=8
        )
        # Ground furniture under the span must NOT read as clearance.
        builder.add_horizontal_rectangle(
            -20, 20, -5, 5, 0.6, hardness="", segments=4
        )
        builder.add_vertical_wall(-20, -5, 5, 0.0, 6.0)
        builder.add_vertical_wall(20, -5, 5, 0.0, 6.0)
        placements = [_placement("bridge/girders.obj")]
        result = otf.classify_object_terrain_features(
            placements, {"bridge/girders.obj": builder.build()}
        )
        assert len(result.bridges) == 1
        bridge = result.bridges[0]
        assert bridge.ceiling_y_m == pytest.approx(4.8, abs=0.2)
        assert bridge.clearance_underside_y_m == pytest.approx(4.2, abs=0.2)


# ---------------------------------------------------------------------------
# feature C — structure ground interfaces (spec section 3.4, A5-A8, R10)
# ---------------------------------------------------------------------------

def _terminal_with_elevated_ramp_geometry() -> dict[str, ObjectGeometry]:
    """ELLX pattern: a terminal grounded at grade on all sides, plus a
    separate elevated-roadway ramp object rising 0 → +8 above it — the A5
    decoy.  Terrain must stay flat."""
    terminal = _GeometryBuilder()
    terminal.add_horizontal_rectangle(-30, 30, -20, 20, 0.0, segments=4)
    terminal.add_horizontal_rectangle(-30, 30, -20, 20, 12.0, segments=4)
    for x in range(-30, 31, 3):
        terminal.add_vertical_wall(float(x), -20, 20, 0.0, 12.0)
    ramp = _GeometryBuilder()
    ramp.add_sloped_rectangle(-30, 30, 22, 30, 0.0, 8.0, segments=6)
    return {
        "objects/terminal_hall.obj": terminal.build(),
        "objects/departures_ramp.obj": ramp.build(),
    }


class TestStructureGroundInterfaces:
    def test_flat_confirmed_with_elevated_ramp_decoy(self):
        geometry = _terminal_with_elevated_ramp_geometry()
        placements = [
            _placement("objects/terminal_hall.obj"),
            _placement("objects/departures_ramp.obj"),
        ]
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root="PACK"
        )
        assert result.tunnels == []
        assert result.bridges == []
        assert len(result.ground_interfaces) == 1
        interface = result.ground_interfaces[0]
        assert interface.interface_class == otf.INTERFACE_FLAT_CONFIRMED
        # The decoy is present and recorded — and did not decide.
        assert interface.elevated_deck_above is True
        assert interface.at_grade_wall_base_share > 0.5
        # Flat structures adapt no terrain: no exclusion.
        assert result.exclusions == []

    def test_bowl_under_deck_with_floor_bound(self):
        """LFPG T1 pattern: no ground contact, every facade based below
        grade, an elevated road deck above the same footprint — a bowl,
        with the floor as a BOUND (objects under-specify depth, A7)."""
        drum = _GeometryBuilder()
        drum.add_horizontal_rectangle(-20, 20, -20, 20, -3.4, segments=4)
        for x in range(-20, 21, 4):
            drum.add_vertical_wall(float(x), -20, 20, -3.4, 10.0)
        helix = _GeometryBuilder()
        helix.add_horizontal_rectangle(-20, 20, -20, 20, 6.0, segments=4)
        placements = [
            _placement("objects/t1_drum.obj"),
            _placement("objects/t1_helix_road.obj"),
        ]
        result = otf.classify_object_terrain_features(
            placements,
            {
                "objects/t1_drum.obj": drum.build(),
                "objects/t1_helix_road.obj": helix.build(),
            },
            pack_root="PACK",
        )
        assert len(result.ground_interfaces) == 1
        interface = result.ground_interfaces[0]
        assert interface.interface_class == otf.INTERFACE_BOWL_UNDER_DECK
        assert interface.floor_y_m == pytest.approx(-3.4, abs=0.3)
        assert interface.floor_is_bound_not_target is True
        assert interface.elevated_deck_above is True
        assert interface.ground_contact_fraction <= 0.10
        assert interface.below_grade_footprint is not None
        # Non-flat interfaces join the R4 exclusion list.
        assert len(result.exclusions) == 2

    def test_trench_spine_across_multiple_objects(self):
        """LFPG T2 pattern: several objects sharing one continuous
        below-grade level while their halls stand at grade."""
        geometry = {}
        placements = []
        for part_index in range(3):
            builder = _GeometryBuilder()
            x0 = -30.0 + part_index * 20.0
            x1 = x0 + 20.0
            builder.add_horizontal_rectangle(x0, x1, -10, 10, 0.0, segments=2)
            builder.add_horizontal_rectangle(
                x0, x1, -10, 10, -7.5, segments=2
            )
            builder.add_vertical_wall(x0, -10, 10, -7.5, 0.0)
            builder.add_vertical_wall(x1, -10, 10, -7.5, 0.0)
            resource = f"objects/t2_hall_{part_index}.obj"
            geometry[resource] = builder.build()
            placements.append(_placement(resource))
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root="PACK"
        )
        assert len(result.ground_interfaces) == 1
        interface = result.ground_interfaces[0]
        assert interface.interface_class == otf.INTERFACE_TRENCH_SPINE
        assert interface.floor_y_m == pytest.approx(-7.5, abs=0.3)
        assert interface.floor_is_bound_not_target is False
        assert interface.below_grade_footprint is not None
        assert interface.below_grade_footprint.area >= (
            otf.TRENCH_SPINE_MIN_FOOTPRINT_AREA_M2
        )

    def test_interior_cutout_with_pile_trap(self):
        """KDEN pattern: an enclosed hard platform at −5 under an at-grade
        terminal, with a non-hard foundation pile to −19.  The floor keys
        on the HARD content — −5, never −19 (A8's depth trap)."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(-40, 40, -30, 30, 0.0, segments=4)
        for x in range(-40, 41, 8):
            builder.add_vertical_wall(float(x), -30, 30, 0.0, 12.0)
        builder.add_horizontal_rectangle(
            -15, 15, -5, 5, -5.0, hardness="hard_deck", segments=4
        )
        # The pile trap: deeper NON-hard solid.
        builder.add_horizontal_rectangle(-2, 2, -2, 2, -19.0, segments=1)
        placements = [_placement("objects/concourse.obj")]
        result = otf.classify_object_terrain_features(
            placements,
            {"objects/concourse.obj": builder.build()},
            pack_root="PACK",
        )
        assert result.tunnels == []
        assert len(result.ground_interfaces) == 1
        interface = result.ground_interfaces[0]
        assert interface.interface_class == otf.INTERFACE_INTERIOR_CUTOUT
        assert interface.floor_y_m == pytest.approx(-5.0, abs=0.2)
        assert interface.below_grade_footprint is not None

    def test_tunnel_versus_cutout_precedence_pair(self):
        """The A8 discriminator: the SAME below-grade hard deck is a
        TUNNEL when its at-grade cover leaves open mouths, and an
        INTERIOR_CUTOUT when the cover fully encloses it."""
        def build(cover_x0: float, cover_x1: float) -> ObjectGeometry:
            builder = _GeometryBuilder()
            builder.add_horizontal_rectangle(
                -30, 30, -10, 10, -5.0, hardness="hard_deck", segments=6
            )
            builder.add_horizontal_rectangle(
                cover_x0, cover_x1, -12, 12, 0.0, segments=4
            )
            return builder.build()

        open_mouths = build(-10, 10)      # covers the middle third only
        enclosed = build(-32, 32)         # covers the whole deck
        result_open = otf.classify_object_terrain_features(
            [_placement("objects/pair_open.obj")],
            {"objects/pair_open.obj": open_mouths},
        )
        result_enclosed = otf.classify_object_terrain_features(
            [_placement("objects/pair_enclosed.obj")],
            {"objects/pair_enclosed.obj": enclosed},
        )
        assert len(result_open.tunnels) == 1
        assert all(
            interface.interface_class != otf.INTERFACE_INTERIOR_CUTOUT
            for interface in result_open.ground_interfaces
        )
        assert result_enclosed.tunnels == []
        assert len(result_enclosed.ground_interfaces) == 1
        assert (
            result_enclosed.ground_interfaces[0].interface_class
            == otf.INTERFACE_INTERIOR_CUTOUT
        )

    def test_jetway_slack_never_a_cutout(self):
        """A8: negative-OBJECT_AGL placements (KDEN jetway slack, −3.77)
        must never drive an interior cutout — no hard geometry, no
        enclosure.  KNOWN LIMIT, flagged to the supervisor: the A6 tunnel
        limb still fires on such placements (kept: EGLL's AGL tunnel
        shells rely on it), so the jetway lands as a tunnel record."""
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(-6, 6, -2, 2, 3.0, segments=2)
        builder.add_vertical_wall(-6, -2, 2, 0.0, 3.0)
        placements = [
            _placement(
                "objects/jetway.obj",
                above_ground_level_metres=-3.77,
                placement_kind="OBJECT_AGL",
            )
        ]
        result = otf.classify_object_terrain_features(
            placements, {"objects/jetway.obj": builder.build()}
        )
        assert all(
            interface.interface_class != otf.INTERFACE_INTERIOR_CUTOUT
            for interface in result.ground_interfaces
        )
        # Current (flagged) behaviour: the AGL tunnel limb fires.
        assert len(result.tunnels) == 1


class TestInterfaceLevelClustering:
    """Direct tests of the A5 clustering + A7 dominant-area exception."""

    def _sectors(self, deep_sector_indices, deep_value=-3.0):
        envelopes = {index: 0.0 for index in range(36)}
        for index in deep_sector_indices:
            envelopes[index] = deep_value
        return envelopes

    def test_below_share_dropped_without_dominant_carrier(self):
        levels = otf._cluster_interface_levels(
            self._sectors([7]),
            [(-3.0, frozenset({"objects/minor.obj"}))],
            "objects/dominant.obj",
        )
        assert [level_y for level_y, _s, _p in levels] == pytest.approx(
            [0.0]
        )

    def test_below_share_survives_via_dominant_carrier(self):
        """A7's T1 lesson: a below-grade level carried by the
        dominant-area object survives the 5% share filter."""
        levels = otf._cluster_interface_levels(
            self._sectors([7]),
            [(-3.0, frozenset({"objects/dominant.obj"}))],
            "objects/dominant.obj",
        )
        level_values = [level_y for level_y, _s, _p in levels]
        assert level_values[0] == pytest.approx(-3.0)

    def test_share_above_floor_survives_regardless(self):
        levels = otf._cluster_interface_levels(
            self._sectors([3, 4, 5]),
            [(-3.0, frozenset({"objects/minor.obj"}))],
            "objects/dominant.obj",
        )
        level_values = [level_y for level_y, _s, _p in levels]
        assert level_values[0] == pytest.approx(-3.0)

    def test_cluster_granularity_keeps_mezzanine_distinct(self):
        """LFLL: the −2 m mezzanine and −10 m rail floor must not merge."""
        envelopes = {index: 0.0 for index in range(20)}
        for index in range(20, 28):
            envelopes[index] = -2.0
        for index in range(28, 36):
            envelopes[index] = -10.0
        levels = otf._cluster_interface_levels(envelopes, [], None)
        level_values = sorted(level_y for level_y, _s, _p in levels)
        assert level_values == pytest.approx([-10.0, -2.0, 0.0])


# ---------------------------------------------------------------------------
# contract logic (spec sections 2.3 / 3.2, amended by A4) — branch coverage
# ---------------------------------------------------------------------------

FLAT_HIGH = (6.0, (6.0, 6.0))       # crest, end elevations
FLAT_FLUSH = (0.0, (0.0, 0.0))
CROWNED = (5.15, (1.72, 1.20))      # the measured KMCO puente shape
MONOTONE_RAMP = (6.0, (0.0, 6.0))   # the EDDF A3 shape


class TestContractClassification:
    def test_coverage_below_threshold_is_deck_carried(self):
        assert otf._classify_contract(*FLAT_HIGH, 0.02) == otf.DECK_CARRIED

    def test_coverage_above_threshold_flat_is_terrain_carried(self):
        assert (
            otf._classify_contract(*FLAT_FLUSH, 0.5) == otf.TERRAIN_CARRIED
        )

    def test_coverage_above_threshold_crowned_is_profile_carried(self):
        assert otf._classify_contract(*CROWNED, 0.6) == otf.PROFILE_CARRIED

    def test_coverage_in_dead_band_is_ambiguous(self):
        assert otf._classify_contract(*FLAT_HIGH, 0.15) == otf.AMBIGUOUS

    def test_height_contradicts_coverage_is_ambiguous(self):
        # Flat deck high above grade with pavement draping across → refused.
        assert otf._classify_contract(*FLAT_HIGH, 0.5) == otf.AMBIGUOUS
        # Flush deck with pavement cut at the abutments → refused.
        assert otf._classify_contract(*FLAT_FLUSH, 0.02) == otf.AMBIGUOUS

    def test_no_pavement_falls_back_to_profile_then_height(self):
        assert otf._classify_contract(*CROWNED, None) == otf.PROFILE_CARRIED
        assert otf._classify_contract(*FLAT_HIGH, None) == otf.DECK_CARRIED
        assert (
            otf._classify_contract(*FLAT_FLUSH, None) == otf.TERRAIN_CARRIED
        )

    def test_monotone_ramp_is_profile_carried(self):
        """Round-3 supervisor ruling: non-flat = crest − MIN(ends), so a
        MONOTONE ramp (EDDF A3, crest at one end, grade at the other) is
        PROFILE_CARRIED — pavement drapes over the whole slope (35.5%
        measured coverage) and the terrain must follow it.  Both the
        coverage path and the no-pavement fallback must agree."""
        assert (
            otf._classify_contract(*MONOTONE_RAMP, 0.355)
            == otf.PROFILE_CARRIED
        )
        assert (
            otf._classify_contract(*MONOTONE_RAMP, None)
            == otf.PROFILE_CARRIED
        )


# ---------------------------------------------------------------------------
# real-pack smoke tests — run only when the scratchpad dumps + packs exist
# ---------------------------------------------------------------------------

_SCRATCH_DEFAULT = (
    "/private/tmp/claude-501/-Users-noah-Ortho4XP-novemberlima/"
    "3f95dd9d-7e39-4a51-971d-478d7d47f51d/scratchpad"
)
_SCRATCH = os.environ.get("OTF_SCRATCHPAD", _SCRATCH_DEFAULT)
_CUSTOM_SCENERY = os.environ.get(
    "OTF_CUSTOM_SCENERY", "/Users/noah/X-Plane 12/Custom Scenery"
)
_EGLL_PACK = os.path.join(
    _CUSTOM_SCENERY, "c_GBR - 100_airport - EGLL_LONDON_TAIMODELS"
)
_KBNA_PACK = os.path.join(_CUSTOM_SCENERY, "US-KBNA Nashville Airport")
_EDDF_PACK = os.path.join(
    _CUSTOM_SCENERY, "Aviotek_Software_Frankfurt_International_Airport"
)


def _load_geometry(placements, pack_root):
    geometry_by_resource = {}
    for placement in placements:
        if placement.resource_path in geometry_by_resource:
            continue
        path = obj8_reader.resolve_object_resource(
            placement.resource_path, pack_root, None
        )
        if path and os.path.isfile(path):
            geometry_by_resource[placement.resource_path] = load_object_file(
                path
            )
    return geometry_by_resource


@pytest.mark.skipif(
    not (
        os.path.isfile(os.path.join(_SCRATCH, "egll.dsf.txt"))
        and os.path.isdir(_EGLL_PACK)
    ),
    reason="EGLL dump/pack not present",
)
class TestEgllSmoke:
    def test_tunnels_grouped_and_tunnel_two_mouths(self):
        lines = open(
            os.path.join(_SCRATCH, "egll.dsf.txt"), errors="replace"
        ).read().splitlines()
        placements = obj8_reader.read_dsf_object_placements(
            lines,
            accept_resource=lambda resource: resource.startswith(
                "Airport/Tunnel/"
            ),
        )
        geometry = _load_geometry(placements, _EGLL_PACK)
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root=_EGLL_PACK
        )
        # Most of the twenty tunnel objects group into cut-and-cover tunnels.
        assert len(result.tunnels) >= 8
        # Tunnel 2 has multiple open mouths, each in the observed area band.
        tunnel_two = next(
            tunnel
            for tunnel in result.tunnels
            if "Airport/Tunnel/2a.obj" in tunnel.object_resources
        )
        assert len(tunnel_two.mouth_polygons) >= 2
        for mouth in tunnel_two.mouth_polygons:
            assert 800.0 < mouth.area < 5200.0
        # Round-5 mega-pool regression: per-tunnel records, undiluted
        # metrics (the pooled run had body-depth medians 0.94-1.93 m),
        # and exclusions carrying only tunnel resources.
        assert tunnel_two.body_depth_m == pytest.approx(5.0, abs=0.3)
        assert tunnel_two.object_resources == [
            "Airport/Tunnel/2.obj",
            "Airport/Tunnel/2a.obj",
        ]
        assert all(
            resource.startswith("Airport/Tunnel/")
            for _pack, resource in result.exclusions
        )

    # The below-grade buildings the author-mesh correlation proved BURIED
    # (supervisor addendum): T2_T3_3 is the deepest object in the pack
    # (minimum y -9.22 m) and none of them is a tunnel.
    BURIED_BUILDING_RESOURCES = (
        "Airport/T2_3/T2_T3_3.obj",
        "Airport/T2_3/T2_T3_20.obj",
        "Airport/T2_3/T2_T3_22.obj",
        "Airport/T5/T5_2.obj",
        "Airport/HOTEL/HT_8.obj",
    )

    def test_buried_buildings_are_not_tunnels(self):
        lines = open(
            os.path.join(_SCRATCH, "egll.dsf.txt"), errors="replace"
        ).read().splitlines()
        targets = set(self.BURIED_BUILDING_RESOURCES)
        placements = obj8_reader.read_dsf_object_placements(
            lines, accept_resource=lambda resource: resource in targets
        )
        assert len(placements) == len(targets)
        geometry = _load_geometry(placements, _EGLL_PACK)
        assert set(geometry) == targets
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root=_EGLL_PACK
        )
        classified_as_tunnel = {
            resource
            for tunnel in result.tunnels
            for resource in tunnel.object_resources
        }
        assert classified_as_tunnel.isdisjoint(targets)
        # And none carries any below-grade hard area, so none becomes a
        # bridge either — the buildings produce no terrain feature at all.
        assert result.exclusions == []


@pytest.mark.skipif(
    not (
        os.path.isfile(os.path.join(_SCRATCH, "kbna_airport.txt"))
        and os.path.isdir(_KBNA_PACK)
    ),
    reason="KBNA dump/pack not present",
)
class TestKbnaSmoke:
    def test_taxiway_l_deck_carried_bridge(self):
        lines = open(
            os.path.join(_SCRATCH, "kbna_airport.txt"), errors="replace"
        ).read().splitlines()
        placements = obj8_reader.read_dsf_object_placements(
            lines,
            accept_resource=lambda resource: resource.startswith(
                "Objects/KBNA Bridges/"
            ),
        )
        mean_sea_level = [
            placement
            for placement in obj8_reader.read_dsf_object_placements(
                lines, include_object_msl=True
            )
            if placement.placement_kind == "OBJECT_MSL"
        ]
        geometry = _load_geometry(placements, _KBNA_PACK)
        result = otf.classify_object_terrain_features(
            placements,
            geometry,
            mean_sea_level_placements=mean_sea_level,
            pack_root=_KBNA_PACK,
        )
        assert result.tunnels == []
        taxiway_l = next(
            bridge
            for bridge in result.bridges
            if any(
                "Taxiway-L" in resource
                for resource in bridge.object_resources
            )
        )
        assert taxiway_l.hard_deck is True
        assert taxiway_l.deck_hardness == otf.DECK_HARDNESS_HARD_DECK
        assert taxiway_l.deck_top_y_m == pytest.approx(5.99, abs=0.3)
        assert taxiway_l.deck_length_m == pytest.approx(131.0, abs=15.0)
        assert taxiway_l.deck_width_m == pytest.approx(55.0, abs=15.0)
        assert taxiway_l.absolute_deck_elevation_m == pytest.approx(
            166.999, abs=0.1
        )
        # Flat deck: profile constant at the deck top, ends included.
        for end_elevation in taxiway_l.deck_end_elevations_y_m:
            assert end_elevation == pytest.approx(5.99, abs=0.3)
        # Girder underside (the clearance-limiting plane) ≈ +4.2; the slab
        # underside (largest plane) ≈ +4.8.
        assert taxiway_l.clearance_underside_y_m == pytest.approx(
            4.2, abs=0.3
        )
        assert taxiway_l.ceiling_y_m == pytest.approx(4.8, abs=0.3)
        # Deck-carried KBNA: the deck ends at +6, but the embankment
        # cladding grounds near the ends — the per-end test passes.
        assert taxiway_l.abutment_reaches_grade == (True, True)
        # No pavement fed here (draped .pol source is dsf_reader's job) →
        # the deck-height cross-check governs and it is deck-carried.
        assert taxiway_l.contract == otf.DECK_CARRIED
        assert result.refusals == []


@pytest.mark.skipif(
    not (
        os.path.isfile(os.path.join(_SCRATCH, "eddf.txt"))
        and os.path.isdir(_EDDF_PACK)
    ),
    reason="EDDF dump/pack not present",
)
class TestEddfSmoke:
    def test_hard_deck_bridges_are_terrain_carried(self):
        lines = open(
            os.path.join(_SCRATCH, "eddf.txt"), errors="replace"
        ).read().splitlines()
        placements = obj8_reader.read_dsf_object_placements(
            lines,
            accept_resource=lambda resource: (
                "Bridge" in resource or "Tunnel" in resource
            )
            and resource.endswith(".obj"),
        )
        geometry = _load_geometry(placements, _EDDF_PACK)
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root=_EDDF_PACK
        )
        hard_bridges = [
            bridge for bridge in result.bridges if bridge.hard_deck
        ]
        # The three Bridge_N_hard decks, each flush and terrain-carried.
        assert len(hard_bridges) == 3
        for bridge in hard_bridges:
            assert bridge.deck_top_y_m == pytest.approx(0.0, abs=0.5)
            assert bridge.contract == otf.TERRAIN_CARRIED

    def test_a3_east_ramp_bridge_reported_deliberately(self):
        """The A3-east elevated crossing: Frankfurt_Mesh_Tunnels_Walls_1_RAMP
        is a plain-ATTR_hard single-slope ramp (+0.06 → +5.96 over 63 m) —
        a REAL bridge, recognized geometrically via the amendment-A4
        plain-hard deck path, not by name-hint luck."""
        lines = open(
            os.path.join(_SCRATCH, "eddf.txt"), errors="replace"
        ).read().splitlines()
        placements = obj8_reader.read_dsf_object_placements(
            lines,
            accept_resource=lambda resource: (
                "Bridge" in resource or "Tunnel" in resource
            )
            and resource.endswith(".obj"),
        )
        geometry = _load_geometry(placements, _EDDF_PACK)
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root=_EDDF_PACK
        )
        ramp = next(
            bridge
            for bridge in result.bridges
            if any(
                "Walls_1_RAMP" in resource
                for resource in bridge.object_resources
            )
        )
        assert ramp.deck_hardness == otf.DECK_HARDNESS_HARD
        assert ramp.hard_deck is False
        # Monotone single-slope profile: one end near grade, crest ≈ +6 at
        # the other.
        assert min(ramp.deck_end_elevations_y_m) < 1.0
        assert max(ramp.deck_end_elevations_y_m) == pytest.approx(
            6.0, abs=0.5
        )
        assert ramp.deck_top_y_m == pytest.approx(6.0, abs=0.5)
        assert ramp.abutment_reaches_grade == (True, True)
        # Round-3 ruling (non-flat = crest − min(ends)): the monotone ramp
        # is PROFILE_CARRIED via the no-pavement fallback here (the
        # coverage path at the measured 35.5% agrees — unit-tested in
        # TestContractClassification).
        assert ramp.contract == otf.PROFILE_CARRIED


_KMCO_PACK = os.path.join(
    _CUSTOM_SCENERY, "c_USA - 100_airport - KMCO - Orlando (Nimbus Simulation)"
)


@pytest.mark.skipif(
    not (
        os.path.isfile(os.path.join(_SCRATCH, "kmco.txt"))
        and os.path.isdir(_KMCO_PACK)
    ),
    reason="KMCO dump/pack not present",
)
class TestKmcoSmoke:
    def _classify(self):
        lines = open(
            os.path.join(_SCRATCH, "kmco.txt"), errors="replace"
        ).read().splitlines()
        placements = obj8_reader.read_dsf_object_placements(
            lines,
            accept_resource=lambda resource: "puente" in resource.lower()
            or "via_tren" in resource.lower(),
        )
        geometry = _load_geometry(placements, _KMCO_PACK)
        return otf.classify_object_terrain_features(
            placements, geometry, pack_root=_KMCO_PACK
        )

    def test_crowned_humps_are_profile_carried(self):
        """puente / puente2: plain-ATTR_hard crowned decks, crests +5.15 /
        +5.18 (measured), profile ends near grade, PROFILE_CARRIED.

        Note on the measured end values (+1.72/+1.20 and +0.72/+1.73):
        those were sampled on a 328/820 m span that trims the grade-flush
        ramp tips.  The round-3 supervisor ruling adopted THIS classifier's
        definition as official: the deck is the FULL drivable hard surface
        (392/909 m), whose profile ends ramp all the way to 0.0 — the
        measured values reappear ~30 m inside the profile.  Asserted here:
        ends near grade (< 2.0 m), crests at the measured values."""
        result = self._classify()
        assert result.tunnels == []
        by_resource = {
            bridge.object_resources[0]: bridge for bridge in result.bridges
        }
        puente = by_resource["Buildings/puente.obj"]
        puente2 = by_resource["Buildings/puente2.obj"]
        assert puente.deck_top_y_m == pytest.approx(5.15, abs=0.3)
        assert puente2.deck_top_y_m == pytest.approx(5.18, abs=0.3)
        for bridge in (puente, puente2):
            assert bridge.deck_hardness == otf.DECK_HARDNESS_HARD
            assert bridge.hard_deck is False
            assert bridge.contract == otf.PROFILE_CARRIED
            assert bridge.abutment_reaches_grade == (True, True)
            for end_elevation in bridge.deck_end_elevations_y_m:
                assert end_elevation < 2.0
            # Crowned: the crest stands well above both ends.
            assert otf._profile_is_non_flat(
                bridge.deck_top_y_m, bridge.deck_end_elevations_y_m
            )

    def test_via_tren_viaduct_refused(self):
        """The rail viaduct never reaches grade (global minimum y +3.45):
        refused, reported, and NOT on the exclusion list."""
        result = self._classify()
        refused_resources = {
            resource
            for refusal in result.refusals
            for resource in refusal.object_resources
        }
        assert "Buildings/via_tren.obj" in refused_resources
        assert all(
            "via_tren" not in resource
            for _pack, resource in result.exclusions
        )
        assert all(
            "via_tren" not in resource
            for bridge in result.bridges
            for resource in bridge.object_resources
        )


# ---------------------------------------------------------------------------
# feature C real-pack smokes.  KDEN is deliberately NOT smoked here: its
# terminal/concourse buildings are .agp autogen tiles (55-110 part objects
# per anchor) and assembling .agp parts into placements is CALLER-side
# work owned by W-T/W-C (spec A8); the classifier contract for that path
# is documented on StructureGroundInterface and covered by the synthetic
# interior-cutout tests above.
# ---------------------------------------------------------------------------

_ELLX_PACK = os.path.join(
    _CUSTOM_SCENERY, "c_LUX - 100_airport - ELLX_JustSim_XPL12_v1.0"
)
_LFPG_PACK_ONE = os.path.join(
    _CUSTOM_SCENERY, "c_FRA - 100_airport - 1_LFPG_PARIS_T1_2_3_XP12"
)
_LFPG_PACK_TWO = os.path.join(
    _CUSTOM_SCENERY, "c_FRA - 100_airport - 2_LFPG_PARIS_TAIMODELS_XP12"
)


@pytest.mark.skipif(
    not (
        os.path.isfile(os.path.join(_SCRATCH, "ellx.txt"))
        and os.path.isdir(_ELLX_PACK)
    ),
    reason="ELLX dump/pack not present",
)
class TestEllxSmoke:
    def test_terminal_is_flat_confirmed_despite_ramp_decoy(self):
        """A5's ELLX verdict, mechanically: the terminal family (with the
        elevated departures roadway present and recorded) confirms FLAT.

        The SkyPark family is excluded here: its underground garage
        (1,134 m2 of plain-hard parking floor at -4) measures 93% enclosed
        - a boundary case between the EGLL open-mouth decks (~50%) and the
        KDEN halls (100%) that currently classifies as a tunnel under the
        A8-calibrated 95% threshold; flagged for a supervisor ruling in
        the workstream report rather than tuned."""
        lines = open(
            os.path.join(_SCRATCH, "ellx.txt"), errors="replace"
        ).read().splitlines()
        placements = obj8_reader.read_dsf_object_placements(
            lines,
            accept_resource=lambda resource: (
                resource.startswith("objects-2026/Terminal")
                and "SkyPark" not in resource
            ),
        )
        geometry = _load_geometry(placements, _ELLX_PACK)
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root=_ELLX_PACK
        )
        assert result.tunnels == []
        assert result.bridges == []
        assert result.refusals == []
        assert len(result.ground_interfaces) == 1
        interface = result.ground_interfaces[0]
        assert interface.interface_class == otf.INTERFACE_FLAT_CONFIRMED
        # The decoy (elevated departures roadway) is present and recorded.
        assert interface.elevated_deck_above is True
        # Flat: no terrain adapted, no exclusions.
        assert result.exclusions == []


@pytest.mark.skipif(
    not (
        os.path.isfile(os.path.join(_SCRATCH, "lfpg1.txt"))
        and os.path.isdir(_LFPG_PACK_ONE)
    ),
    reason="LFPG pack-1 dump/pack not present",
)
class TestLfpgTerminalOneSmoke:
    def test_terminal_one_is_a_bowl_with_floor_bound(self):
        """A7: the T1 pool (drum + 0 -> +17.6 helix road) is a bowl; the
        floor bound is the shell base (-3.42, versus the -3.43 measured),
        with the true depth (-8 in the reference hand patch)
        under-specified - hence bound-not-target."""
        lines = open(
            os.path.join(_SCRATCH, "lfpg1.txt"), errors="replace"
        ).read().splitlines()
        placements = obj8_reader.read_dsf_object_placements(
            lines,
            accept_resource=lambda resource: resource.startswith(
                "AIRPORT/TERMINAL_1/"
            ),
        )
        geometry = _load_geometry(placements, _LFPG_PACK_ONE)
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root=_LFPG_PACK_ONE
        )
        assert len(result.ground_interfaces) == 1
        interface = result.ground_interfaces[0]
        assert interface.interface_class == otf.INTERFACE_BOWL_UNDER_DECK
        assert interface.floor_y_m == pytest.approx(-3.43, abs=0.3)
        assert interface.floor_is_bound_not_target is True
        assert interface.elevated_deck_above is True
        assert interface.ground_contact_fraction <= 0.10
        assert interface.at_grade_wall_base_share <= 0.10


@pytest.mark.skipif(
    not (
        os.path.isfile(os.path.join(_SCRATCH, "lfpg2.txt"))
        and os.path.isdir(_LFPG_PACK_TWO)
    ),
    reason="LFPG pack-2 dump/pack not present",
)
class TestLfpgTerminalTwoSmoke:
    def test_terminal_two_spine_is_a_trench(self):
        """A7: one continuous ~-7.5 m below-grade level across the
        Terminal 2A hall family.  NOTE: the work order located the spine
        in pack 1; the -7.5 family (TRAIN.obj -7.56, 2A_* -7.4..-9.2 -
        exactly A7's measured values) actually ships in pack 2
        (TAIMODELS), KHU1 family - reported, not forced."""
        lines = open(
            os.path.join(_SCRATCH, "lfpg2.txt"), errors="replace"
        ).read().splitlines()
        placements = obj8_reader.read_dsf_object_placements(
            lines,
            accept_resource=lambda resource: resource.startswith(
                "AIRPORT/KHU1/"
            ),
        )
        geometry = _load_geometry(placements, _LFPG_PACK_TWO)
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root=_LFPG_PACK_TWO
        )
        spine = next(
            interface
            for interface in result.ground_interfaces
            if interface.interface_class == otf.INTERFACE_TRENCH_SPINE
        )
        assert spine.floor_y_m == pytest.approx(-7.5, abs=0.5)
        # One continuous level shared across a large object family
        # (A7: 23 objects; the pool carries the whole 2A hall family).
        assert len(spine.object_resources) >= 20
        assert spine.below_grade_footprint is not None
        assert spine.below_grade_footprint.area >= (
            otf.TRENCH_SPINE_MIN_FOOTPRINT_AREA_M2
        )


# ---------------------------------------------------------------------------
# round 5 — mega-pool refinement, coverage exposure, no silent absence
# ---------------------------------------------------------------------------

class TestMegaPoolRefinement:
    def _tunnel_geometry(self) -> ObjectGeometry:
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -30, 30, -10, 10, -5.0, hardness="hard_deck", segments=6
        )
        builder.add_horizontal_rectangle(
            -10, 10, -10, 10, 0.0, hardness="hard_deck", segments=2
        )
        return builder.build()

    def test_chained_pool_yields_per_tunnel_records(self):
        """Two tunnels chained into ONE pool by an overlapping clutter
        slab: records stay per tunnel (undiluted body depth), and the
        chain slab never reaches the R4 exclusion list."""
        chain = _GeometryBuilder()
        chain.add_horizontal_rectangle(-50, 350, -2, 2, 0.5, segments=8)
        east_latitude, east_longitude = local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, 300.0, 0.0
        )
        placements = [
            _placement("tunnel/west.obj"),
            _placement(
                "tunnel/east.obj",
                longitude=east_longitude,
                latitude=east_latitude,
            ),
            _placement("clutter/chain_slab.obj"),
        ]
        geometry = {
            "tunnel/west.obj": self._tunnel_geometry(),
            "tunnel/east.obj": self._tunnel_geometry(),
            "clutter/chain_slab.obj": chain.build(),
        }
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root="PACK"
        )
        assert len(result.tunnels) == 2
        for tunnel in result.tunnels:
            assert len(tunnel.object_resources) == 1
            assert tunnel.object_resources[0].startswith("tunnel/")
            assert tunnel.body_depth_m == pytest.approx(5.0, abs=0.3)
            assert len(tunnel.mouth_polygons) == 2
        excluded = {resource for _pack, resource in result.exclusions}
        assert excluded == {"tunnel/west.obj", "tunnel/east.obj"}

    def test_bridge_in_pool_with_far_building_still_classifies(self):
        """The Crossing_Bridge defect: a freestanding bridge chained into
        the same pool as a large building must still classify — never
        silently absent — and only its own parts are excluded."""
        bridge_geometry = _hard_deck_bridge_geometry()
        building = _GeometryBuilder()
        building.add_horizontal_rectangle(100, 180, -40, 40, 0.0, segments=4)
        for x in range(100, 181, 3):
            building.add_vertical_wall(float(x), -40, 40, 0.0, 12.0)
        chain = _GeometryBuilder()
        chain.add_horizontal_rectangle(-30, 110, -2, 2, 0.2, segments=4)
        building_latitude, building_longitude = local_offset_to_lonlat(
            ANCHOR_LATITUDE, ANCHOR_LONGITUDE, 0.0, 140.0, 0.0
        )
        placements = [
            _placement("bridge/free_standing.obj"),
            _placement(
                "objects/big_building.obj",
                longitude=building_longitude,
                latitude=building_latitude,
            ),
            _placement("clutter/chain_slab.obj"),
        ]
        geometry = {
            "bridge/free_standing.obj": bridge_geometry,
            "objects/big_building.obj": building.build(),
            "clutter/chain_slab.obj": chain.build(),
        }
        result = otf.classify_object_terrain_features(
            placements, geometry, pack_root="PACK"
        )
        assert len(result.bridges) == 1
        bridge = result.bridges[0]
        assert bridge.object_resources == ["bridge/free_standing.obj"]
        excluded = {resource for _pack, resource in result.exclusions}
        assert excluded == {"bridge/free_standing.obj"}
        # The building still surfaces through feature C.
        assert any(
            "objects/big_building.obj" in interface.object_resources
            for interface in result.ground_interfaces
        )

    def test_building_carried_roadway_still_routes_to_feature_c(self):
        """ELLX pattern under the per-component gate: a drivable roadway
        within the building's evidence radius stays feature C."""
        building = _GeometryBuilder()
        building.add_horizontal_rectangle(-40, 40, -30, 30, 0.0, segments=4)
        # A dense facade/pillar grid: real terminals carry thousands of
        # wall columns (ELLX 6,752); the building gate needs >= 500.
        for x in range(-40, 41, 4):
            for z in range(-30, 31, 5):
                building.add_vertical_wall(
                    float(x), float(z), z + 1.0, 0.0, 12.0
                )
        ramp = _GeometryBuilder()
        ramp.add_sloped_rectangle(
            -40, 40, 32, 40, 0.0, 8.0, hardness="hard", segments=8
        )
        placements = [
            _placement("objects/terminal.obj"),
            _placement("objects/ramp.obj"),
        ]
        geometry = {
            "objects/terminal.obj": building.build(),
            "objects/ramp.obj": ramp.build(),
        }
        result = otf.classify_object_terrain_features(placements, geometry)
        assert result.bridges == []
        assert result.refusals == []
        assert len(result.ground_interfaces) == 1
        assert (
            result.ground_interfaces[0].interface_class
            == otf.INTERFACE_FLAT_CONFIRMED
        )


class TestCoverageExposure:
    def test_coverage_fraction_and_evidence_on_record(self):
        builder = _GeometryBuilder()
        builder.add_horizontal_rectangle(
            -20, 20, -5, 5, -0.02, hardness="hard_deck", segments=8
        )
        pavement = _frame_rectangle_to_pavement_polygon(-25, 25, -8, 8)
        result = otf.classify_object_terrain_features(
            [_placement("bridge/flush.obj")],
            {"bridge/flush.obj": builder.build()},
            pavement_polygons_longitude_latitude=[pavement],
        )
        bridge = result.bridges[0]
        assert bridge.pavement_coverage_fraction == pytest.approx(
            1.0, abs=0.05
        )
        assert (
            bridge.contract_evidence
            == otf.CONTRACT_EVIDENCE_PAVEMENT_COVERAGE
        )

    def test_no_pavement_marks_deck_profile_fallback(self):
        result = otf.classify_object_terrain_features(
            [_placement("bridge/hard.obj")],
            {"bridge/hard.obj": _hard_deck_bridge_geometry()},
        )
        bridge = result.bridges[0]
        assert bridge.pavement_coverage_fraction is None
        assert bridge.contract_evidence == otf.CONTRACT_EVIDENCE_DECK_PROFILE

    def test_lateral_pavement_lap_is_not_span_crossing_evidence(self):
        """The round-5 KBNA calibration: at-grade pavement lapping the
        deck's lateral edge must not push a cut-at-abutments bridge into
        the dead band — the coverage band spans only the central half of
        the deck width."""
        geometry = _hard_deck_bridge_geometry()  # deck z in [-5, 5]
        # Pavement strip along one lateral edge, full deck length.
        lateral_lap = _frame_rectangle_to_pavement_polygon(-20, 20, 3.5, 12)
        result = otf.classify_object_terrain_features(
            [_placement("bridge/hard.obj")],
            {"bridge/hard.obj": geometry},
            pavement_polygons_longitude_latitude=[lateral_lap],
        )
        bridge = result.bridges[0]
        assert bridge.pavement_coverage_fraction == pytest.approx(
            0.0, abs=0.02
        )
        assert bridge.contract == otf.DECK_CARRIED


@pytest.mark.skipif(
    not (
        os.path.isfile(os.path.join(_SCRATCH, "kbna_airport.txt"))
        and os.path.isdir(_KBNA_PACK)
    ),
    reason="KBNA dump/pack not present",
)
class TestKbnaStandaloneDrapeEvidence:
    def test_taxiway_l_deck_carried_under_raw_drape_coverage(self):
        """Round-5 defect 2: the flagship deck-carried exemplar must
        classify DECK_CARRIED under STANDALONE raw-DSF drape evidence.
        The measured failure mode was lateral at-grade taxiways lapping
        the deck's side edges (14.5% of the full-width band); the
        central-half coverage band reads the true mid-span: 0%."""
        import glob as _glob
        import tempfile as _tempfile

        from auto_patch import dsf_reader

        dsf_candidates = _glob.glob(
            os.path.join(_KBNA_PACK, "Earth nav data", "*", "*.dsf")
        )
        if not dsf_candidates:
            pytest.skip("KBNA DSF not found")
        pavements = dsf_reader.read_dsf_pavements(
            dsf_candidates[0],
            cache_dir=_tempfile.gettempdir(),
            xplane_root=os.path.dirname(_CUSTOM_SCENERY),
        )
        pavement_polygons = []
        for outer_ring, _holes, _definition in pavements:
            try:
                polygon = Polygon(outer_ring)
                if not polygon.is_valid:
                    polygon = polygon.buffer(0)
                if not polygon.is_empty:
                    pavement_polygons.append(polygon)
            except (ValueError, Exception):
                continue
        lines = open(
            os.path.join(_SCRATCH, "kbna_airport.txt"), errors="replace"
        ).read().splitlines()
        placements = obj8_reader.read_dsf_object_placements(
            lines,
            accept_resource=lambda resource: resource.startswith(
                "Objects/KBNA Bridges/"
            ),
        )
        geometry = _load_geometry(placements, _KBNA_PACK)
        result = otf.classify_object_terrain_features(
            placements,
            geometry,
            pavement_polygons_longitude_latitude=pavement_polygons,
            pack_root=_KBNA_PACK,
        )
        taxiway_l = next(
            bridge
            for bridge in result.bridges
            if any(
                "Taxiway-L" in resource
                for resource in bridge.object_resources
            )
        )
        assert taxiway_l.pavement_coverage_fraction is not None
        assert taxiway_l.pavement_coverage_fraction <= 0.05
        assert (
            taxiway_l.contract_evidence
            == otf.CONTRACT_EVIDENCE_PAVEMENT_COVERAGE
        )
        assert taxiway_l.contract == otf.DECK_CARRIED
        # Crossing_Bridge present too — never silently absent.
        assert any(
            "Crossing_Bridge" in resource
            for bridge in result.bridges
            for resource in bridge.object_resources
        )
