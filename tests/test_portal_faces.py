"""Tests for the bare tunnel-portal FACE feature (user 2026-07-17, EGGW).

Some packs author a road tunnel's portals as nothing but a textured face
quad hanging BELOW grade — a handful of soft triangles from ``y ~ 0`` down
to the road deck.  Such a face matches neither the tunnel signature nor
any bridge signature, so two new pieces recognise and use it:

A. ``object_terrain_features._detect_portal_faces`` — the resource-level
   signature gate (single plain-``OBJECT`` placement, 1..8 soft solid
   triangles, ``min y <= -2``, ``max y <= +1``, height ``>= 2``, min-
   rotated-rect long side ``4..60 m``), plus the exclusion wiring that
   drops a recognised face's resource from the Phase 2 y-bake.

B. ``bridges._detect_tunnel_portal_pairs`` — face candidates pair only
   with each other, by mutual parallelism of the two face lines AND the
   connecting segment CROSSING the mean face line, inside the spacing
   window, over a buried body; a mapped ``tunnel=yes`` way between the
   faces suppresses the pair (OSM owns the crossing).

All fixtures are synthetic (ruling R6): geometry, classification records,
DEM sampler and road-layer loader are built / monkeypatched in code — no
third-party pack content enters the repository.  Mirrors the geometry-
builder idiom of ``tests/test_object_terrain_features.py`` and the
classification/layout idiom of ``tests/test_object_bridge_terrain.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from shapely.geometry import Polygon, box

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from auto_patch import bridges  # noqa: E402
from auto_patch import config  # noqa: E402
from auto_patch import object_terrain_features as otf  # noqa: E402
from auto_patch.obj8_reader import ObjectGeometry, ObjectPlacement  # noqa: E402
from auto_patch.object_terrain_features import PortalFaceStructure  # noqa: E402
from auto_patch.layout import (  # noqa: E402
    BuiltShape,
    PavementLayout,
    ROLE_JUNCTION,
)

ANCHOR_LATITUDE = 51.874
ANCHOR_LONGITUDE = -0.368
ANCHOR = (ANCHOR_LATITUDE, ANCHOR_LONGITUDE)
TILE_LATITUDE = 51
TILE_LONGITUDE = -1

_PORTAL_RESOURCE = "Objects/Airport/portal_face.obj"


# ---------------------------------------------------------------------------
# geometry + placement builders (synthetic, object local metre frame)
# ---------------------------------------------------------------------------
def _portal_face_geometry(
    *,
    width_m: float = 20.0,
    min_y_m: float = -8.0,
    max_y_m: float = 0.0,
    segments: int = 1,
    first_triangle_hardness: str | None = None,
    tilt_m: float = 0.2,
) -> ObjectGeometry:
    """A near-vertical face quad spanning ``width_m`` horizontally (along
    local z) and ``min_y_m..max_y_m`` vertically, split into ``segments``
    (2 triangles each).  The small ``tilt_m`` in x keeps the projected
    footprint a genuine 2-D sliver so the min-rotated-rect width is exact.
    """
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []
    hardness: list[str] = []
    z_start = -width_m / 2.0
    for segment in range(segments):
        z_a = z_start + width_m * segment / segments
        z_b = z_start + width_m * (segment + 1) / segments
        base = len(vertices)
        vertices.extend([
            (0.0, min_y_m, z_a),
            (0.0, min_y_m, z_b),
            (tilt_m, max_y_m, z_b),
            (tilt_m, max_y_m, z_a),
        ])
        triangles.append((base, base + 1, base + 2))
        triangles.append((base, base + 2, base + 3))
        hardness.extend(["", ""])
    if first_triangle_hardness is not None:
        hardness[0] = first_triangle_hardness
    return ObjectGeometry(
        vertices=vertices,
        solid_triangles=triangles,
        draped_triangles=[],
        positional_commands=[],
        animation_block_count=0,
        level_of_detail_count=0,
        vertex_line_indices=list(range(len(vertices))),
        solid_triangle_hardness=tuple(hardness),
    )


def _placement(
    resource: str = _PORTAL_RESOURCE,
    *,
    placement_kind: str = "OBJECT",
) -> ObjectPlacement:
    return ObjectPlacement(
        definition_index=0,
        resource_path=resource,
        longitude=ANCHOR_LONGITUDE,
        latitude=ANCHOR_LATITUDE,
        heading_degrees=0.0,
        placement_kind=placement_kind,
    )


# ---------------------------------------------------------------------------
# A. portal-face detection
# ---------------------------------------------------------------------------
class TestPortalFaceDetection:
    """``_detect_portal_faces`` recognises a single below-grade soft face
    quad and rejects everything outside the signature."""

    def test_vertical_soft_quad_is_detected(self) -> None:
        faces = otf._detect_portal_faces(
            [_placement()], {_PORTAL_RESOURCE: _portal_face_geometry()})
        assert len(faces) == 1
        face = faces[0]
        assert face.object_resources == [_PORTAL_RESOURCE]
        assert face.face_width_m == pytest.approx(20.0, abs=0.1)
        assert face.face_min_y_m == pytest.approx(-8.0)
        assert face.face_max_y_m == pytest.approx(0.0)
        assert face.deck_top_y_m == pytest.approx(8.0)
        # A north-south face line (long side along north); the implied
        # tunnel axis is its perpendicular (+90 deg).
        assert face.face_line_bearing_degrees == pytest.approx(0.0, abs=0.5)
        assert face.heading_degrees == pytest.approx(90.0, abs=0.5)
        assert face.face_hangs_below is True

    def test_oblique_face_line_carries_its_bearing(self) -> None:
        # A portal face is NOT necessarily perpendicular to the tunnel axis
        # (EGGW: the taxiway edge crosses the road obliquely).  A vertical
        # face along the x=z diagonal projects to a face line at 135 deg;
        # the implied axis is its perpendicular (45 deg), never derived by
        # assuming face-perpendicular-equals-axis.
        half_diagonal = 14.142  # long side ~40 m along the x=z diagonal
        vertices = [
            (-half_diagonal, -8.0, -half_diagonal),
            (half_diagonal, -8.0, half_diagonal),
            (half_diagonal + 0.14, 0.0, half_diagonal - 0.14),
            (-half_diagonal + 0.14, 0.0, -half_diagonal - 0.14),
        ]
        geometry = ObjectGeometry(
            vertices=vertices,
            solid_triangles=[(0, 1, 2), (0, 2, 3)],
            draped_triangles=[],
            positional_commands=[],
            animation_block_count=0,
            level_of_detail_count=0,
            vertex_line_indices=[0, 1, 2, 3],
            solid_triangle_hardness=("", ""),
        )
        faces = otf._detect_portal_faces(
            [_placement()], {_PORTAL_RESOURCE: geometry})
        assert len(faces) == 1
        face = faces[0]
        # A genuinely oblique face line (not axis-aligned).
        assert face.face_line_bearing_degrees == pytest.approx(135.0, abs=1.0)
        assert face.face_width_m == pytest.approx(40.0, abs=0.5)
        expected_axis = (face.face_line_bearing_degrees + 90.0) % 180.0
        assert face.heading_degrees == pytest.approx(expected_axis, abs=0.5)
        assert face.heading_degrees == pytest.approx(45.0, abs=1.0)

    def test_hard_triangle_is_rejected(self) -> None:
        # Anything drivable is the A6 tunnel signature's business.
        faces = otf._detect_portal_faces(
            [_placement()],
            {_PORTAL_RESOURCE: _portal_face_geometry(
                first_triangle_hardness="hard")})
        assert faces == []

    def test_too_many_triangles_is_rejected(self) -> None:
        # 5 segments => 10 solid triangles > PORTAL_FACE_MAX_SOLID_TRIANGLES.
        assert otf.PORTAL_FACE_MAX_SOLID_TRIANGLES == 8
        faces = otf._detect_portal_faces(
            [_placement()],
            {_PORTAL_RESOURCE: _portal_face_geometry(segments=5)})
        assert faces == []

    def test_too_shallow_face_is_rejected(self) -> None:
        # min y = -1 sits above the -2 m depth floor.
        faces = otf._detect_portal_faces(
            [_placement()],
            {_PORTAL_RESOURCE: _portal_face_geometry(
                min_y_m=-1.0, max_y_m=0.0)})
        assert faces == []

    def test_too_narrow_face_is_rejected(self) -> None:
        faces = otf._detect_portal_faces(
            [_placement()],
            {_PORTAL_RESOURCE: _portal_face_geometry(width_m=2.0)})
        assert faces == []

    def test_too_wide_face_is_rejected(self) -> None:
        faces = otf._detect_portal_faces(
            [_placement()],
            {_PORTAL_RESOURCE: _portal_face_geometry(width_m=80.0)})
        assert faces == []

    def test_multiple_placements_of_one_resource_are_rejected(self) -> None:
        # A face shared by N placements cannot mark N distinct mouths.
        faces = otf._detect_portal_faces(
            [_placement(), _placement()],
            {_PORTAL_RESOURCE: _portal_face_geometry()})
        assert faces == []

    def test_object_agl_placement_is_rejected(self) -> None:
        # Negative-AGL rows already carry the A6 tunnel signature.
        faces = otf._detect_portal_faces(
            [_placement(placement_kind="OBJECT_AGL")],
            {_PORTAL_RESOURCE: _portal_face_geometry()})
        assert faces == []


class TestPortalFaceClassificationExclusion:
    """A recognised face surfaces in ``ClassificationResult.portal_faces``
    and its resource joins ``exclusions`` (dropped from the Phase 2 bake)."""

    def test_face_resource_lands_in_exclusions(self) -> None:
        result = otf.classify_object_terrain_features(
            [_placement()],
            {_PORTAL_RESOURCE: _portal_face_geometry()},
            pack_root="PACK",
        )
        assert len(result.portal_faces) == 1
        assert result.portal_faces[0].object_resources == [_PORTAL_RESOURCE]
        assert (("PACK", _PORTAL_RESOURCE)) in result.exclusions

    def test_non_face_object_is_not_excluded(self) -> None:
        # A shallow (non-portal) quad is neither a portal face nor excluded.
        result = otf.classify_object_terrain_features(
            [_placement()],
            {_PORTAL_RESOURCE: _portal_face_geometry(
                min_y_m=-1.0, max_y_m=0.0)},
            pack_root="PACK",
        )
        assert result.portal_faces == []
        assert ("PACK", _PORTAL_RESOURCE) not in result.exclusions


# ---------------------------------------------------------------------------
# B. portal-face pairing
# ---------------------------------------------------------------------------
def _to_meters_and_inverse():
    return bridges._local_meter_projections(ANCHOR)


def _face_record(
    centre_east_m: float,
    centre_north_m: float,
    face_line_bearing_degrees: float,
    resource: str,
    *,
    half_footprint_m: float = 5.0,
    deck_top_y_m: float = 8.0,
) -> PortalFaceStructure:
    """A :class:`PortalFaceStructure` whose lon/lat footprint centres on
    ``(centre_east_m, centre_north_m)`` in the layout metre frame.  Pairing
    reads only the centroid, the stored ``face_line_bearing_degrees`` and
    ``deck_top_y_m``, so a small square footprint is sufficient."""
    _to_meters, meters_to_lat_lon = _to_meters_and_inverse()
    corners_m = [
        (centre_east_m - half_footprint_m, centre_north_m - half_footprint_m),
        (centre_east_m + half_footprint_m, centre_north_m - half_footprint_m),
        (centre_east_m + half_footprint_m, centre_north_m + half_footprint_m),
        (centre_east_m - half_footprint_m, centre_north_m + half_footprint_m),
    ]
    ring = []
    for east, north in corners_m:
        latitude, longitude = meters_to_lat_lon(east, north)
        ring.append((longitude, latitude))
    return PortalFaceStructure(
        object_resources=[resource],
        anchor_longitude_latitude=ring[0],
        heading_degrees=(face_line_bearing_degrees + 90.0) % 180.0,
        face_polygon_longitude_latitude=Polygon(ring),
        face_min_y_m=-deck_top_y_m,
        face_max_y_m=0.0,
        face_width_m=2.0 * half_footprint_m,
        deck_top_y_m=deck_top_y_m,
        face_line_bearing_degrees=face_line_bearing_degrees % 180.0,
    )


class _FaceClassification:
    """Just enough of ``ClassificationResult`` for the pairing reader: no
    bridge candidates, two portal-face records."""

    def __init__(self, faces) -> None:
        self.bridges: list = []
        self.portal_faces = list(faces)
        self.tunnels: list = []
        self.exclusions: list = []
        self.refusals: list = []


def _layout_with_faces(faces, *, pavement_between: bool = True) -> PavementLayout:
    layout = PavementLayout(icao="ZZZZ", anchor=ANCHOR)
    if pavement_between:
        # A taxiway strip crossing the middle of the east-west connecting
        # segment (buried signal a: airside pavement over the body).
        layout.shapes.append(BuiltShape(
            polygon=box(-5.0, -40.0, 5.0, 40.0),
            role=ROLE_JUNCTION, ref="taxiway"))
    setattr(layout, bridges._OBJECT_BRIDGE_CLASSIFICATION_ATTRIBUTE,
            _FaceClassification(faces))
    return layout


def _install_pairing_scene(
    monkeypatch, *, mapped_tunnel_between: bool
) -> None:
    """Gate feature B on, flat DEM (mouth floors resolve), and a road-layer
    loader that optionally supplies a mapped ``tunnel=yes`` way crossing the
    connecting segment."""
    monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", True)
    monkeypatch.setattr(bridges, "_sample_dem", lambda *_args: 90.0)
    _to_meters, meters_to_lat_lon = _to_meters_and_inverse()

    def _road_network(_layout):
        if not mapped_tunnel_between:
            return {}, [], set(), {}
        # A north-south way at x = 0 crosses the east-west segment between
        # the two faces (centred at x = -/+50).
        nodes_r = {
            "t0": meters_to_lat_lon(0.0, -30.0),
            "t1": meters_to_lat_lon(0.0, 30.0),
        }
        ways_r = [("MAPPED", ["t0", "t1"],
                   {"highway": "unclassified", "tunnel": "yes"})]
        return nodes_r, ways_r, set(), {}

    monkeypatch.setattr(
        bridges, "_load_tunnel_road_network", _road_network)


class TestPortalFacePairing:
    """Two portal faces pair into one buried tunnel only when parallel, the
    connecting segment crosses them, and OSM maps no corroborating bore."""

    def test_parallel_faces_over_pavement_pair(self, monkeypatch) -> None:
        _install_pairing_scene(monkeypatch, mapped_tunnel_between=False)
        faces = [
            _face_record(-50.0, 0.0, 0.0, "west_face"),
            _face_record(50.0, 0.0, 0.0, "east_face"),
        ]
        layout = _layout_with_faces(faces)
        pairs = bridges._detect_tunnel_portal_pairs(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        assert len(pairs) == 1
        pair = pairs[0]
        assert pair["is_face"] is True
        # Both mouth floors read the flat DEM grade.
        for portal in pair["portals"]:
            assert portal["mouth_floor_m"] == pytest.approx(90.0)

    def test_mapped_tunnel_between_corroborates_the_pair(
        self, monkeypatch
    ) -> None:
        # Owner ruling 2026-07-18: a mapped OSM bore between the faces
        # STRENGTHENS the pair (recorded as corroboration) — it never
        # stands the pair down.  The pair owns the crossing and the
        # OSM-side emitters yield through the crossing-ownership union.
        _install_pairing_scene(monkeypatch, mapped_tunnel_between=True)
        faces = [
            _face_record(-50.0, 0.0, 0.0, "west_face"),
            _face_record(50.0, 0.0, 0.0, "east_face"),
        ]
        layout = _layout_with_faces(faces)
        pairs = bridges._detect_tunnel_portal_pairs(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        assert len(pairs) == 1
        assert pairs[0]["is_face"] is True
        assert pairs[0]["osm_corroborated"] is True

    def test_perpendicular_faces_do_not_pair(self, monkeypatch) -> None:
        # Two faces whose lines are 90 deg apart are not two ends of one
        # structure (parallelism gate fails).
        _install_pairing_scene(monkeypatch, mapped_tunnel_between=False)
        faces = [
            _face_record(-50.0, 0.0, 0.0, "west_face"),
            _face_record(50.0, 0.0, 90.0, "east_face"),
        ]
        layout = _layout_with_faces(faces)
        pairs = bridges._detect_tunnel_portal_pairs(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        assert pairs == []

    def test_side_by_side_faces_do_not_pair(self, monkeypatch) -> None:
        # Parallel faces whose connecting segment runs ALONG the face line
        # (side by side, not end to end) fail the crossing gate.
        _install_pairing_scene(monkeypatch, mapped_tunnel_between=False)
        faces = [
            _face_record(0.0, -50.0, 0.0, "south_face"),
            _face_record(0.0, 50.0, 0.0, "north_face"),
        ]
        layout = _layout_with_faces(faces)
        pairs = bridges._detect_tunnel_portal_pairs(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        assert pairs == []


# ---------------------------------------------------------------------------
# C. portal-face plate emission — the anchor seat
# ---------------------------------------------------------------------------
class TestPortalFaceAnchorSeat:
    """User screenshots 2026-07-18b (EGGW): the deck-grade cover that keeps
    ``terrain(anchor)`` at the crown for a hanging face was a 5 m ROUND disk
    centred on the anchor — mid-road it rendered as a ~10 m arc-shaped
    tower, and the road-grade mouth hole cut from the SAME disk shared its
    rim coordinates with the crown (one mesh node bucket per rim vertex,
    altitude decided by first-writer interning).  The seat must be a
    face-aligned rectangle with a minimal outward lip, and no mouth vertex
    may share a node bucket with a crown vertex."""

    def _emitted_plates(self, monkeypatch):
        from auto_patch.layout import ROLE_TUNNEL_TRENCH

        _install_pairing_scene(monkeypatch, mapped_tunnel_between=False)
        faces = [
            _face_record(-50.0, 0.0, 0.0, "west_face"),
            _face_record(50.0, 0.0, 0.0, "east_face"),
        ]
        layout = _layout_with_faces(faces)
        n_trench, n_causeway, _pads = bridges.build_bridge_layout_shapes(
            layout, object(), TILE_LATITUDE, TILE_LONGITUDE)
        assert n_causeway == 0 and n_trench >= 2
        plates = {"mouth": [], "crown": [], "collar": []}
        for shape in layout.shapes:
            if getattr(shape, "role", None) != ROLE_TUNNEL_TRENCH:
                continue
            for kind in plates:
                if shape.ref == f"object_tunnel_portal_{kind}":
                    plates[kind].append(shape)
        assert len(plates["mouth"]) == 2 and len(plates["crown"]) == 2
        return layout, plates

    def test_seat_is_a_face_hugging_lip_not_a_disk(self, monkeypatch) -> None:
        from shapely.geometry import Point

        _layout, plates = self._emitted_plates(monkeypatch)
        to_meters, _inverse = _to_meters_and_inverse()
        # Anchors sit on the face lines: ring[0] of each face polygon.
        # West portal outward is -x (away from the east partner), east
        # portal outward +x.
        seat_lip = config.PORTAL_FACE_ANCHOR_SEAT_OUTWARD_M
        for anchor_east, outward_sign in ((-55.0, -1.0), (45.0, 1.0)):
            anchor = Point(anchor_east, -5.0)
            crown = min(
                plates["crown"],
                key=lambda shape: shape.polygon.distance(anchor))
            # The crown still COVERS the anchor (the object drapes at
            # terrain(anchor) and must read deck grade).  With the
            # zero-lip seat the anchor sits ON the seat's front edge —
            # the drape interpolates that edge's two deck-grade nodes —
            # and the edge must SURVIVE locally around the anchor.
            # covers() is float-fragile for a point exactly on the edge
            # — distance 0 (within a micron) is the robust containment.
            assert crown.polygon.distance(anchor) < 1e-6
            assert crown.polygon.intersection(
                anchor.buffer(0.6)).area >= 0.4
            # No crown vertex protrudes past the seat lip into the road
            # (the old disk reached 5 m outward of the face line; the
            # v20 rectangle's 1 m lip rendered as a squared fin).
            for vertex_x, _vertex_y in crown.polygon.exterior.coords:
                protrusion = (vertex_x - anchor_east) * outward_sign
                assert protrusion <= seat_lip + 0.05, (
                    "crown protrudes into the road past the anchor-seat "
                    f"lip: {protrusion:.2f} m > {seat_lip:.2f} m")

    def test_mouth_and_crown_share_no_node_bucket(self, monkeypatch) -> None:
        # The canonical mesh node registry interns coordinates in ~0.5 m
        # buckets, first writer wins — a mouth vertex and a crown vertex
        # at the same spot collapse to ONE node whose altitude is
        # effectively random between road grade and deck grade (the v18
        # face-meeting trap; the v19 disk shipped exactly that on its
        # whole rim).
        _layout, plates = self._emitted_plates(monkeypatch)
        mouth_vertices = [
            (x, y) for shape in plates["mouth"]
            for x, y in shape.polygon.exterior.coords]
        crown_vertices = [
            (x, y) for shape in plates["crown"]
            for x, y in shape.polygon.exterior.coords]
        assert mouth_vertices and crown_vertices
        closest = min(
            ((mx - cx) ** 2 + (my - cy) ** 2) ** 0.5
            for mx, my in mouth_vertices
            for cx, cy in crown_vertices)
        assert closest > 0.55, (
            "a mouth vertex and a crown vertex fall in the same mesh "
            f"node bucket (min separation {closest:.3f} m)")

    def test_mouth_plate_leaves_the_anchor_uncovered(
        self, monkeypatch
    ) -> None:
        from shapely.geometry import Point

        _layout, plates = self._emitted_plates(monkeypatch)
        for anchor_east in (-55.0, 45.0):
            anchor = Point(anchor_east, -5.0)
            for mouth in plates["mouth"]:
                assert not mouth.polygon.covers(anchor), (
                    "road-grade mouth plate covers the face anchor — the "
                    "object would drape at road grade and the face would "
                    "sink by its own height")
