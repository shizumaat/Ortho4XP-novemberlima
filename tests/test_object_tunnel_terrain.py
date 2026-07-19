"""Workstream W-T tests — object-derived tunnel terrain (feature A of
``docs/object_terrain_features_spec.md``, section 3.3 + amendment A1,
ruling R12).

Fixtures are synthetic (ruling R6): a hand-built :class:`TunnelStructure`
(plus a negative-``OBJECT_AGL`` variant) and a minimal fake layout / DEM
drive the pre-solve layout-shape birth
(``object_terrain_assembly.build_tunnel_layout_shapes``) — no third-party
pack content enters the repository.  The tests drive the DECISION logic
(datum, floor law, rim law, pavement-overlap subtraction, node-split wall
geometry, gate-off neutrality) rather than a full mesh, so they stay
deterministic and independent of a scenery install.
"""

from __future__ import annotations

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
from shapely.ops import unary_union  # noqa: E402

from auto_patch import bridges  # noqa: E402
from auto_patch import config  # noqa: E402
from auto_patch import grade_law  # noqa: E402
from auto_patch import object_terrain_assembly as assembly  # noqa: E402
from auto_patch.layout import (  # noqa: E402
    BuiltShape,
    ROLE_TUNNEL_TRENCH,
    ROLE_JUNCTION,
)
from auto_patch.object_terrain_features import TunnelStructure  # noqa: E402

# A KBNA-ish anchor used as BOTH the layout anchor and the structure frame
# origin, so the frame -> lon/lat -> meter round trip is (numerically) the
# identity and frame coordinates read directly as local metres in
# assertions (the same trick the bridge test uses).
ANCHOR_LATITUDE = 36.124
ANCHOR_LONGITUDE = -86.678
ANCHOR = (ANCHOR_LATITUDE, ANCHOR_LONGITUDE)

TILE_LATITUDE = 36
TILE_LONGITUDE = -87


@pytest.fixture(autouse=True)
def tunnel_gate_on(monkeypatch):
    """Every test drives the emitter with the feature gate ON (it is
    default OFF); the gate-off tests flip it back explicitly."""
    monkeypatch.setattr(config, "OBJECT_TUNNEL_TERRAIN", True)


# ---------------------------------------------------------------------------
# synthetic fixtures
# ---------------------------------------------------------------------------

class _FakeDem:
    """A flat DEM: ``alt`` returns a constant for any tile-frame point."""

    nodata = -32768

    def __init__(self, elevation_m: float) -> None:
        self.elevation_m = elevation_m

    def alt(self, _xy) -> float:
        return self.elevation_m


class _FakeLayout:
    """Minimal layout stand-in: anchor, shapes, and the projection /
    canonical-registry surface the pin writers touch."""

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


class _Classification:
    """Just enough of ``ClassificationResult`` for the emitter."""

    def __init__(self, tunnels) -> None:
        self.bridges: list = []
        self.tunnels = list(tunnels)
        self.exclusions: list = []
        self.refusals: list = []


def _body_rectangle_frame(
    length_m: float = 100.0, half_width_m: float = 15.0
) -> Polygon:
    """A tunnel body deck footprint in the structure frame: a rectangle
    x=0..length along the axis, centred on z=0."""
    return Polygon(
        [
            (0.0, -half_width_m),
            (length_m, -half_width_m),
            (length_m, half_width_m),
            (0.0, half_width_m),
        ]
    )


def _tunnel(
    *,
    body_depth_m: float = 5.0,
    above_ground_offset_m: float = 0.0,
    placement_kind: str = "OBJECT",
    deck_footprint: Polygon | None = None,
    length_m: float = 100.0,
    half_width_m: float = 15.0,
) -> TunnelStructure:
    footprint = (
        deck_footprint
        if deck_footprint is not None
        else _body_rectangle_frame(length_m=length_m, half_width_m=half_width_m)
    )
    # The roof covers most of the body; the emitter cuts the WHOLE deck
    # footprint regardless (amendment A1), so the roof is present only for
    # record realism.
    roof = footprint.buffer(-2.0)
    return TunnelStructure(
        object_resources=["Airport/Tunnel/2.obj", "Airport/Tunnel/2a.obj"],
        anchor_longitude_latitude=(ANCHOR_LONGITUDE, ANCHOR_LATITUDE),
        frame_origin_longitude_latitude=(ANCHOR_LONGITUDE, ANCHOR_LATITUDE),
        heading_degrees=0.0,
        placement_kind=placement_kind,
        above_ground_offset_m=above_ground_offset_m,
        roof_footprint=roof if not roof.is_empty else None,
        deck_footprint=footprint,
        mouth_polygons=[],
        mouth_depth_samples=[],
        body_depth_m=body_depth_m,
    )


def _tunnel_plates(layout):
    """Every born tunnel plate (floor + rim), by role and ref prefix."""
    return [
        shape for shape in layout.shapes
        if shape.role == ROLE_TUNNEL_TRENCH
        and str(shape.ref).startswith("object_tunnel")
    ]


def _floor_plates(layout):
    return [s for s in _tunnel_plates(layout) if s.ref == "object_tunnel_trench"]


def _rim_plates(layout):
    return [s for s in _tunnel_plates(layout) if s.ref == "object_tunnel_rim"]


# ---------------------------------------------------------------------------
# the grade law (lockstep single source)
# ---------------------------------------------------------------------------

class TestTunnelLaw:
    def test_rim_is_the_datum(self):
        assert grade_law.tunnel_trench_rim_elevation_m(21.0) == pytest.approx(
            21.0
        )

    def test_floor_is_datum_minus_depth_minus_offset(self):
        # datum 21, deck level -5 (body 5 m below grade): floor = 21 - 5 -
        # 0.5 = 15.5 (the author-mesh tunnel-2 floor was 15 at integer
        # precision — strictly below the 16 m deck).
        floor = grade_law.tunnel_trench_floor_elevation_m(21.0, -5.0)
        assert floor == pytest.approx(
            21.0 - 5.0 - config.TUNNEL_FLOOR_BELOW_OBJECT_DECK_M
        )
        assert floor == pytest.approx(15.5)

    def test_floor_strictly_below_deck(self):
        # For any positive body depth the floor is below the deck it renders.
        datum, deck_level = 100.0, -4.0
        deck_world = datum + deck_level
        floor = grade_law.tunnel_trench_floor_elevation_m(datum, deck_level)
        assert floor < deck_world


# ---------------------------------------------------------------------------
# floor / rim birth at the law values
# ---------------------------------------------------------------------------

class TestTrenchBirth:
    def test_floor_and_rim_born_at_law_values(self):
        layout = _FakeLayout()
        classification = _Classification([_tunnel(body_depth_m=5.0)])
        setattr(layout, assembly.CLASSIFICATION_ATTRIBUTE, classification)
        floors, rims = assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        assert floors >= 1
        assert rims >= 1
        # floor = 100 - 5 - 0.5 = 94.5, rim = 100.0.
        for plate in _floor_plates(layout):
            assert plate.node_altitudes
            assert all(a == pytest.approx(94.5) for a in plate.node_altitudes)
        for plate in _rim_plates(layout):
            assert plate.node_altitudes
            assert all(a == pytest.approx(100.0) for a in plate.node_altitudes)

    def test_tunnel_never_pins_the_pavement_solver(self):
        # OFF-PAVEMENT terrain (ruling R2): the tunnel trench must NOT enter
        # the pavement pin registry — a deep floor pin there would drag the
        # adjacent airside pavement DOWN through the one-solve (measured: 30 %
        # of EGLL pavement, up to 8 m, when the trench reused the pavement
        # bridge role).  The floor is carried by per-node ``alt_abs`` only.
        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=6.0)]),
        )
        floors, rims = assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(50.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        assert floors >= 1 and rims >= 1
        # No pavement pin registry written at all.
        assert not getattr(layout, "_object_bridge_pin_values", None)
        # The floor value (50 - 6 - 0.5 = 43.5) and the rim (50.0) live on
        # the shapes' per-node altitudes instead.
        floor_alts = {a for p in _floor_plates(layout) for a in p.node_altitudes}
        rim_alts = {a for p in _rim_plates(layout) for a in p.node_altitudes}
        assert all(a == pytest.approx(43.5) for a in floor_alts)
        assert all(a == pytest.approx(50.0) for a in rim_alts)

    def test_node_split_wall_gap_above_interning_tolerance(self):
        # R2 node-split wall: the rim-collar inner edge and the floor-plate
        # outer edge are a gap apart, and that gap exceeds the 0.5 m
        # node-interning tolerance so the two rows survive as a near-vertical
        # wall rather than merging.
        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=5.0)]),
        )
        assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        floor_union = unary_union([p.polygon for p in _floor_plates(layout)])
        rim_union = unary_union([p.polygon for p in _rim_plates(layout)])
        # The floor never overlaps the rim, and the smallest separation is
        # above the interning tolerance.
        assert floor_union.distance(rim_union) > 0.5
        # The rim tops the body edge (welds to surrounding terrain at datum).
        assert not floor_union.is_empty and not rim_union.is_empty


# ---------------------------------------------------------------------------
# negative-OBJECT_AGL variant (EGLL tunnels 6/7/10)
# ---------------------------------------------------------------------------

class TestNegativeAglVariant:
    def test_offset_not_double_counted(self):
        # The classifier folds the AGL offset into ``body_depth_m`` already
        # (effective_y = above_ground_level_metres + authored_y), so a
        # negative-AGL tunnel with the SAME effective body depth must land
        # at the SAME floor as a plain tunnel — the offset is not re-applied.
        plain = _FakeLayout()
        setattr(
            plain, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=5.0,
                                     above_ground_offset_m=0.0)]),
        )
        assembly.build_tunnel_layout_shapes(
            plain, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )

        agl = _FakeLayout()
        setattr(
            agl, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=5.0,
                                     above_ground_offset_m=-7.0,
                                     placement_kind="OBJECT_AGL")]),
        )
        assembly.build_tunnel_layout_shapes(
            agl, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )

        plain_floor = _floor_plates(plain)[0].node_altitudes[0]
        agl_floor = _floor_plates(agl)[0].node_altitudes[0]
        assert plain_floor == pytest.approx(agl_floor)
        assert agl_floor == pytest.approx(94.5)  # NOT 87.5 (double-counted)


# ---------------------------------------------------------------------------
# PAVEMENT WINS (rulings R2/R8)
# ---------------------------------------------------------------------------

class TestPavementSubtraction:
    def _pavement_band(self) -> BuiltShape:
        """An airside taxiway crossing the middle of the body (x 40..60 in
        layout metres, spanning the full width)."""
        polygon = Polygon([(40.0, -25.0), (60.0, -25.0),
                           (60.0, 25.0), (40.0, 25.0)])
        return BuiltShape(polygon=polygon, role=ROLE_JUNCTION, ref="TAXI")

    def test_trench_carved_around_pavement(self):
        layout = _FakeLayout()
        pavement = self._pavement_band()
        layout.shapes.append(pavement)
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=5.0)]),
        )
        floors, _rims = assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        assert floors >= 1
        # No tunnel plate lands inside the pavement interior (pavement wins).
        pavement_interior = pavement.polygon.buffer(-1.0)
        for plate in _tunnel_plates(layout):
            assert plate.polygon.intersection(pavement_interior).area < 1e-6

    def test_no_pavement_leaves_body_intact(self):
        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=5.0)]),
        )
        floors, rims = assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        # A single simply-connected body: one floor pan, and at least one
        # rim piece (the anchor-seat keep-out may open the annulus into a
        # single C-shaped band; long bands are chopped for terrain-true
        # sampling).
        assert floors == 1
        assert rims >= 1


# ---------------------------------------------------------------------------
# SAME-ANCHOR FACILITY GROUPING + ANCHOR SEAT (user 2026-07-18f)
# ---------------------------------------------------------------------------

class TestFacilityGrouping:
    def test_same_anchor_shells_join_with_a_corridor_cut(self):
        # EGLL west: the ramp skin and the crossing box share ONE
        # placement anchor; the open trench between them has no object.
        # Same-anchor tunnels are one facility — the corridor between
        # their shells is cut at the facility floor.
        from shapely.geometry import Point

        near_shell = _tunnel(body_depth_m=5.0)
        far_footprint = Polygon([
            (140.0, -8.0), (180.0, -8.0), (180.0, 8.0), (140.0, 8.0)])
        far_shell = _tunnel(body_depth_m=7.0, deck_footprint=far_footprint)
        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([near_shell, far_shell]),
        )
        floors, _rims = assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        assert floors >= 1
        floor_union = unary_union(
            [p.polygon for p in _floor_plates(layout)])
        # The open corridor between the shells (x 100..140) is floored.
        assert floor_union.covers(Point(120.0, 0.0))
        # One facility floor at the DEEPEST member's law value:
        # 100 - 7 - 0.5 = 92.5.
        floor_values = {a for p in _floor_plates(layout)
                        for a in p.node_altitudes}
        assert all(v == pytest.approx(92.5) for v in floor_values)

    def test_anchor_seat_pins_the_datum_inside_the_cut(self):
        # The shells drape at terrain(anchor); when the facility cut
        # reaches the anchor a seat plate pins it at the datum so the
        # objects never sink by the cut depth.
        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=5.0)]),
        )
        assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        seats = [s for s in layout.shapes
                 if s.ref == "object_tunnel_anchor_seat"]
        assert len(seats) == 1
        assert all(a == pytest.approx(100.0)
                   for a in seats[0].node_altitudes)
        # The floor keeps a node-split clearance around the seat.
        floor_union = unary_union(
            [p.polygon for p in _floor_plates(layout)])
        assert floor_union.distance(seats[0].polygon) >= (
            assembly._TUNNEL_WALL_SETBACK_M - 0.05)


# ---------------------------------------------------------------------------
# FLUSH WALLS (user screenshots 2026-07-18c)
# ---------------------------------------------------------------------------

class TestFlushWalls:
    """The floor pan reaches the shell's own wall plane (the mesh batter
    leans OUTWARD, never poking through the object base), the datum rim
    band sits OUTSIDE the body, and only pavement-abutting edges keep a
    bucket-safe floor clearance."""

    def _built(self, *, with_pavement: bool = False):
        layout = _FakeLayout()
        pavement = None
        if with_pavement:
            polygon = Polygon([(40.0, -25.0), (60.0, -25.0),
                               (60.0, 25.0), (40.0, 25.0)])
            pavement = BuiltShape(polygon=polygon, role=ROLE_JUNCTION,
                                  ref="TAXI")
            layout.shapes.append(pavement)
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=5.0)]),
        )
        assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        return layout, pavement

    def test_floor_lies_one_gap_inside_the_body(self):
        # INVERTED flush walls (user 2026-07-18f): the wall top is flush
        # ON the shell outline and the batter hides INSIDE the shell —
        # the floor sits one node-split gap inside the body, never
        # outside it.
        from shapely.geometry import box

        layout, _pavement = self._built()
        floor_union = unary_union(
            [p.polygon for p in _floor_plates(layout)])
        body = box(0.0, -15.0, 100.0, 15.0)
        assert body.contains(floor_union)
        gap = body.exterior.distance(floor_union)
        assert assembly._TUNNEL_WALL_SETBACK_M - 0.05 <= gap \
            <= assembly._TUNNEL_WALL_SETBACK_M + 0.1

    def test_rim_band_top_is_flush_on_the_body_outline(self):
        from shapely.geometry import box

        layout, _pavement = self._built()
        rim_union = unary_union([p.polygon for p in _rim_plates(layout)])
        body = box(0.0, -15.0, 100.0, 15.0)
        # The band starts exactly ON the outline (flush top, no outside
        # crevice) and lies outside the body interior.
        assert rim_union.distance(body.exterior) < 1e-6
        # Sub-0.1 m2 slivers are chopper rotation float-jitter (microns
        # deep over hundreds of metres of shared edge).
        assert rim_union.intersection(body).area < 0.1
        # Wall gap between the band's inner ring (the outline) and the
        # floor is the setback — node-split safe, near-vertical, hidden
        # within the shell's wall thickness.
        floor_union = unary_union(
            [p.polygon for p in _floor_plates(layout)])
        assert floor_union.distance(rim_union) == pytest.approx(
            assembly._TUNNEL_WALL_SETBACK_M, abs=0.1)

    def test_floor_keeps_clearance_from_pavement(self):
        layout, pavement = self._built(with_pavement=True)
        floor_union = unary_union(
            [p.polygon for p in _floor_plates(layout)])
        # Bucket-safe gap where the body abuts pavement...
        assert floor_union.distance(pavement.polygon) >= (
            assembly._TUNNEL_FLOOR_OWNED_CLEARANCE_M - 0.05)
        # ...while the free edges keep the uniform one-gap inset from
        # the body outline (y = +-15 minus the setback).
        minimum_x, minimum_y, maximum_x, maximum_y = floor_union.bounds
        inset = assembly._TUNNEL_WALL_SETBACK_M
        assert minimum_y == pytest.approx(-15.0 + inset, abs=0.05)
        assert maximum_y == pytest.approx(15.0 - inset, abs=0.05)

    def test_rim_band_is_terrain_true_not_datum_flat(self):
        # EGLL west end (user 2026-07-18c): Tunnel/6+7 anchor ~100 m from
        # their geometry, so the datum-flat rim stood ~5 m proud of the
        # surrounding ground as a raised berm box.  Each band part must
        # sample the DEM at its own centroid; the floor keeps the anchor
        # datum (that is where the draped object's solids land).
        class _SlopedDem:
            nodata = -32768

            def alt(self, xy) -> float:
                # ~0.3 m per metre of longitude away from the anchor.
                offset_degrees = xy[0] - (ANCHOR_LONGITUDE - TILE_LONGITUDE)
                metres = offset_degrees * 111320.0 * 0.62
                return 100.0 + 0.3 * metres

        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=5.0)]),
        )
        assembly.build_tunnel_layout_shapes(
            layout, _SlopedDem(), TILE_LATITUDE, TILE_LONGITUDE
        )
        rim_alts = {a for p in _rim_plates(layout) for a in p.node_altitudes}
        # The 100 m-long body spans real slope: band parts track their own
        # local ground, so the rim carries a RANGE of values, not one.
        assert len(rim_alts) > 1
        assert max(rim_alts) - min(rim_alts) > 3.0
        # The floor still keys on the anchor datum.
        floor_alts = {a for p in _floor_plates(layout)
                      for a in p.node_altitudes}
        assert all(a == pytest.approx(94.5, abs=0.3) for a in floor_alts)

    def test_remote_tunnel_beyond_airside_gate_is_skipped(self):
        # Global-Airports tile DSF: every airport's classifier sees every
        # object on the tile — twelve airports each cut a copy of the SAME
        # Redhill tower trench 10 km away and the near-identical rings
        # killed Triangle4XP.  A body farther than the airside gate from
        # this airport's own pavement is never cut.
        layout = _FakeLayout()
        pavement = BuiltShape(
            polygon=Polygon([(5000.0, -25.0), (5100.0, -25.0),
                             (5100.0, 25.0), (5000.0, 25.0)]),
            role=ROLE_JUNCTION, ref="TAXI")
        layout.shapes.append(pavement)
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=5.0)]),
        )
        floors, rims = assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        assert floors == 0 and rims == 0
        assert not _tunnel_plates(layout)

    def test_rim_band_yields_to_earlier_shapes_with_setback(self):
        layout, pavement = self._built(with_pavement=True)
        rim_union = unary_union([p.polygon for p in _rim_plates(layout)])
        # The band never touches the pavement and stays a full setback off
        # its boundary (a cut exactly on the edge would bucket-share the
        # pavement's nodes and race datum against the solved grade).
        assert rim_union.intersection(pavement.polygon).area < 1e-6
        assert rim_union.distance(pavement.polygon) >= (
            assembly._TUNNEL_WALL_SETBACK_M - 0.05)


# ---------------------------------------------------------------------------
# gate-off neutrality
# ---------------------------------------------------------------------------

class TestGateOff:
    def test_gate_off_births_nothing(self, monkeypatch):
        monkeypatch.setattr(config, "OBJECT_TUNNEL_TERRAIN", False)
        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel()]),
        )
        result = assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        assert result == (0, 0)
        assert layout.shapes == []
        assert not hasattr(layout, "_object_bridge_pin_values")

    def test_no_classification_is_noop(self):
        layout = _FakeLayout()  # gate on, but nothing attached
        result = assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        )
        assert result == (0, 0)
        assert layout.shapes == []

    def test_no_tunnels_is_noop(self):
        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE, _Classification([])
        )
        assert assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        ) == (0, 0)


# ---------------------------------------------------------------------------
# degenerate inputs never raise
# ---------------------------------------------------------------------------

class TestDegenerate:
    def test_zero_body_depth_skipped(self):
        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel(body_depth_m=0.0)]),
        )
        assert assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        ) == (0, 0)

    def test_missing_dem_datum_skipped(self):
        layout = _FakeLayout()
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([_tunnel()]),
        )
        # dem=None ⇒ _sample_dem returns None ⇒ tunnel skipped, no raise.
        assert assembly.build_tunnel_layout_shapes(
            layout, None, TILE_LATITUDE, TILE_LONGITUDE
        ) == (0, 0)

    def test_no_footprints_at_all_skipped(self):
        # Flush-outside rule (user 2026-07-18): the trench cuts the
        # UNION of deck and roof footprints, so a record missing only
        # its deck still cuts the roof extent.  Only a record with
        # NEITHER footprint is degenerate and skipped.
        layout = _FakeLayout()
        tunnel = _tunnel()
        object.__setattr__(tunnel, "deck_footprint", None)
        object.__setattr__(tunnel, "roof_footprint", None)
        setattr(
            layout, assembly.CLASSIFICATION_ATTRIBUTE,
            _Classification([tunnel]),
        )
        assert assembly.build_tunnel_layout_shapes(
            layout, _FakeDem(100.0), TILE_LATITUDE, TILE_LONGITUDE
        ) == (0, 0)
