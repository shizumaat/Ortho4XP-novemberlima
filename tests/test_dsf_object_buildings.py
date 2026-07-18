"""OBJ8 scenery-object building footprints — workstream W6 of the DSF
object integration (``docs/dsf_object_integration_spec.md`` section
4-W6, rulings R3/R4/R5, invariant I-5).

Three tiers:

(a) HERMETIC ``object_footprints.structure_ring`` tests — the
    ``Structure`` / ``ObjectGeometry`` inputs are constructed directly
    (no file parsing, no partition) and the placement projection is a
    local equirectangular fake mirroring ``obj8_reader``'s documented
    convention, so nothing here waits on workstream W2.
(b) ``dsf_reader.read_dsf_object_buildings`` plumbing tests — fake
    ``.dsf`` + pre-seeded backdated ``.dsf.text`` (harness pattern (b)
    from ``tests/test_dsf_buildings.py``), with
    ``object_anchor.discover_object_pools`` / ``partition_structures``
    monkeypatched to trivial fakes (they are Wave-2 stubs).  A final
    variant drops the ``obj8_reader`` monkeypatches and runs against the
    real workstream-W2 reader; it skips while that reader still raises
    ``NotImplementedError``.
(c) Flag gating and the shared building-admission helper the pipeline
    loop refactor introduced — the ``.fac`` facade path must be
    behavior-identical (``tests/test_dsf_buildings.py`` and
    ``tests/test_dsf_surface_pavement.py`` prove the reader side).
"""
import math
import os

import pytest
from shapely.geometry import Polygon

from auto_patch import config
from auto_patch import dsf_reader as D
from auto_patch import obj8_reader
from auto_patch import object_anchor
from auto_patch import object_footprints
from auto_patch import pipeline

METRES_PER_DEGREE_LATITUDE = obj8_reader.METRES_PER_DEGREE_LATITUDE

ANCHOR_LATITUDE = 35.0
ANCHOR_LONGITUDE = -80.0


@pytest.fixture(autouse=True)
def sandbox_ortho4xp_data_root(tmp_path, monkeypatch):
    """USER RULING 2026-07-15 moved the sidecar caches under the
    Ortho4XP data root (``Airport_mod_cache/<pack>/``).  In a source
    checkout the data root resolves to the current working directory, so
    without this pin any test that exercises ``read_dsf_object_buildings``
    would write ``Airport_mod_cache/`` into the repository.  Sandbox
    every test in this module (``ORTHO4XP_DATA_ROOT`` wins
    ``O4_File_Names.resolve_data_root``)."""
    monkeypatch.setenv("ORTHO4XP_DATA_ROOT",
                       str(tmp_path / "o4_data_root"))


@pytest.fixture(autouse=True)
def disable_minimum_building_height(monkeypatch):
    """The geometric fixtures in this file are deliberately FLAT slabs
    (walls would obscure the ring-shape assertions), which the
    amendment-A11 ground-plate filter would reject wholesale.  Default
    it off for this file; ``TestStructureRingMinimumBuildingHeight``
    re-enables it explicitly to test the filter itself."""
    monkeypatch.setattr(config, "DSF_OBJECT_MIN_BUILDING_HEIGHT_M", 0.0)


# ── shared construction helpers ──────────────────────────────────────

def equirectangular_local_offset_to_lonlat(
        anchor_latitude, anchor_longitude, heading_degrees,
        local_x, local_z):
    """Test double for ``obj8_reader.local_offset_to_lonlat``, mirroring
    its documented convention (local +x = east, +z = south; heading
    clockwise from north) so tier (a) never waits on workstream W2."""
    heading = math.radians(heading_degrees)
    east = local_x * math.cos(heading) - local_z * math.sin(heading)
    south = local_x * math.sin(heading) + local_z * math.cos(heading)
    metres_per_degree_longitude = (
        METRES_PER_DEGREE_LATITUDE
        * math.cos(math.radians(anchor_latitude)))
    return (anchor_latitude - south / METRES_PER_DEGREE_LATITUDE,
            anchor_longitude + east / metres_per_degree_longitude)


@pytest.fixture()
def fake_projection(monkeypatch):
    monkeypatch.setattr(obj8_reader, "local_offset_to_lonlat",
                        equirectangular_local_offset_to_lonlat)


def make_geometry(vertices, solid_triangles):
    return obj8_reader.ObjectGeometry(
        vertices=list(vertices),
        solid_triangles=list(solid_triangles),
        draped_triangles=[],
        positional_commands=[],
        animation_block_count=0,
        level_of_detail_count=0,
        vertex_line_indices=list(range(len(vertices))),
    )


def make_placement(resource_path,
                   longitude=ANCHOR_LONGITUDE,
                   latitude=ANCHOR_LATITUDE,
                   heading_degrees=0.0,
                   definition_index=0):
    return obj8_reader.ObjectPlacement(
        definition_index=definition_index,
        resource_path=resource_path,
        longitude=longitude,
        latitude=latitude,
        heading_degrees=heading_degrees,
    )


def make_structure(triangles_by_resource, minimum_base_y_by_resource,
                   *, is_ground_touching=True):
    return object_anchor.Structure(
        triangles_by_resource=triangles_by_resource,
        surface_area_square_metres=1.0,
        centroid_latitude=ANCHOR_LATITUDE,
        centroid_longitude=ANCHOR_LONGITUDE,
        minimum_base_y_by_resource=minimum_base_y_by_resource,
        is_ground_touching=is_ground_touching,
        ground_span_metres=None,
        needs_pad=False,
        skip_reason=None,
        inherited_from_structure_index=None,
    )


def ring_to_local_metres(ring,
                         anchor_latitude=ANCHOR_LATITUDE,
                         anchor_longitude=ANCHOR_LONGITUDE):
    """Invert the equirectangular projection back to heading-0 local
    ``(x = east, z = south)`` metres for geometric assertions."""
    metres_per_degree_longitude = (
        METRES_PER_DEGREE_LATITUDE
        * math.cos(math.radians(anchor_latitude)))
    return [
        ((longitude - anchor_longitude) * metres_per_degree_longitude,
         -(latitude - anchor_latitude) * METRES_PER_DEGREE_LATITUDE)
        for longitude, latitude in ring
    ]


# The L-shaped ground plan of tier (a): a 20 x 10 slab plus a 10 x 10
# wing, 300 square metres, whose convex hull (350 square metres) swallows
# the notch corner at (10, 10) — ruling R3's known, accepted cost.
L_SHAPE_VERTICES = [
    (0.0, 0.0, 0.0),     # 0
    (20.0, 0.0, 0.0),    # 1
    (20.0, 0.0, 10.0),   # 2
    (10.0, 0.0, 10.0),   # 3  the notch corner
    (10.0, 0.0, 20.0),   # 4
    (0.0, 0.0, 20.0),    # 5
    (0.0, 0.0, 10.0),    # 6
]
L_SHAPE_TRIANGLES = [(0, 1, 2), (2, 6, 0), (6, 3, 4), (4, 5, 6)]


# ── tier (a): hermetic structure_ring ────────────────────────────────

class TestStructureRingHull:
    def test_hull_swallows_l_shape_notch(self, fake_projection):
        geometry = make_geometry(L_SHAPE_VERTICES, L_SHAPE_TRIANGLES)
        structure = make_structure({"a.obj": L_SHAPE_TRIANGLES},
                                   {"a.obj": 0.0})
        ring = object_footprints.structure_ring(
            structure, {"a.obj": geometry}, [make_placement("a.obj")])
        assert ring is not None
        # Unclosed: the first vertex is not repeated.
        assert ring[0] != ring[-1]
        # (longitude, latitude) order: the first coordinate is the
        # longitude (near -80), not the latitude (near 35).
        for longitude, latitude in ring:
            assert abs(longitude - ANCHOR_LONGITUDE) < 0.01
            assert abs(latitude - ANCHOR_LATITUDE) < 0.01
        local = ring_to_local_metres(ring)
        # The EXPECTED hull is the 5-corner pentagon: the notch corner
        # (10, 10) is swallowed (ruling R3 — measure first, fidelity
        # behind DSF_OBJECT_FOOTPRINT_UNION).
        assert len(local) == 5
        assert all(math.hypot(x - 10.0, z - 10.0) > 0.5
                   for x, z in local)
        hull_polygon = Polygon(local)
        assert hull_polygon.area == pytest.approx(350.0, abs=2.0)
        # The swallowed notch region is inside the pad.
        from shapely.geometry import Point
        assert hull_polygon.contains(Point(14.0, 14.0))

    def test_footprint_height_filter_excludes_roof_overhang(
            self, fake_projection):
        # 10 x 10 walls on the ground, roof overhanging to 15 m at
        # y = 8 — the overhang must not inflate the pad.
        vertices = [
            (0.0, 0.0, 0.0), (10.0, 0.0, 0.0),
            (10.0, 0.0, 10.0), (0.0, 0.0, 10.0),
            (-5.0, 8.0, -5.0), (15.0, 8.0, -5.0),
            (15.0, 8.0, 15.0), (-5.0, 8.0, 15.0),
        ]
        triangles = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
        geometry = make_geometry(vertices, triangles)
        structure = make_structure({"a.obj": triangles}, {"a.obj": 0.0})
        ring = object_footprints.structure_ring(
            structure, {"a.obj": geometry}, [make_placement("a.obj")])
        assert ring is not None
        local = ring_to_local_metres(ring)
        xs = [x for x, _ in local]
        zs = [z for _, z in local]
        assert max(xs) == pytest.approx(10.0, abs=0.1)
        assert min(xs) == pytest.approx(0.0, abs=0.1)
        assert max(zs) == pytest.approx(10.0, abs=0.1)
        assert min(zs) == pytest.approx(0.0, abs=0.1)

    def test_fewer_than_three_base_vertices_falls_back_to_all(
            self, fake_projection):
        # Only 2 vertices within the 1.5 m base window — the footprint
        # falls back to ALL solid vertices (low flat objects).
        vertices = [
            (0.0, 0.0, 0.0), (10.0, 0.0, 0.0),
            (0.0, 5.0, 20.0), (10.0, 5.0, 20.0),
            (0.0, 5.0, 10.0), (10.0, 5.0, 10.0),
        ]
        triangles = [(0, 1, 4), (1, 5, 4), (4, 5, 2), (5, 3, 2)]
        geometry = make_geometry(vertices, triangles)
        structure = make_structure({"a.obj": triangles}, {"a.obj": 0.0})
        ring = object_footprints.structure_ring(
            structure, {"a.obj": geometry}, [make_placement("a.obj")])
        assert ring is not None
        local = ring_to_local_metres(ring)
        zs = [z for _, z in local]
        # Two base points alone are a degenerate (line) hull; the ring
        # spanning the full 0..20 depth proves the fallback engaged.
        assert max(zs) == pytest.approx(20.0, abs=0.1)
        assert min(zs) == pytest.approx(0.0, abs=0.1)

    def test_no_ground_contact_returns_none(self, fake_projection):
        geometry = make_geometry(L_SHAPE_VERTICES, L_SHAPE_TRIANGLES)
        structure = make_structure({"a.obj": L_SHAPE_TRIANGLES},
                                   {"a.obj": 6.0},
                                   is_ground_touching=False)
        assert object_footprints.structure_ring(
            structure, {"a.obj": geometry},
            [make_placement("a.obj")]) is None


class TestStructureRingMinimumBuildingHeight:
    """Amendment A11 (HECA Tai Models pack): a building has walls; a
    ground plate, sign or decal does not.  A near-flat structure gets no
    Phase-1 pad — ``heca_ground_polygon.obj`` spans 2.1 km and must
    never become a 2 km flat building pad."""

    # A 20 x 20 plate: solid, ground-touching, 0.2 m of vertical extent.
    PLATE_VERTICES = [
        (0.0, 0.0, 0.0), (20.0, 0.0, 0.0),
        (20.0, 0.2, 20.0), (0.0, 0.2, 20.0),
    ]
    PLATE_TRIANGLES = [(0, 1, 2), (0, 2, 3)]

    def _plate_ring(self):
        geometry = make_geometry(self.PLATE_VERTICES, self.PLATE_TRIANGLES)
        structure = make_structure({"a.obj": self.PLATE_TRIANGLES},
                                   {"a.obj": 0.0})
        return object_footprints.structure_ring(
            structure, {"a.obj": geometry}, [make_placement("a.obj")])

    def test_flat_plate_gets_no_pad(self, fake_projection, monkeypatch):
        monkeypatch.setattr(
            config, "DSF_OBJECT_MIN_BUILDING_HEIGHT_M", 2.5)
        assert self._plate_ring() is None

    def test_zero_disables_the_filter(self, fake_projection, monkeypatch):
        monkeypatch.setattr(
            config, "DSF_OBJECT_MIN_BUILDING_HEIGHT_M", 0.0)
        assert self._plate_ring() is not None

    def test_walled_building_passes_the_filter(self, fake_projection,
                                               monkeypatch):
        monkeypatch.setattr(
            config, "DSF_OBJECT_MIN_BUILDING_HEIGHT_M", 2.5)
        # The same plate with an 8 m roof slab above it: real walls.
        vertices = self.PLATE_VERTICES + [
            (0.0, 8.0, 0.0), (20.0, 8.0, 0.0),
            (20.0, 8.0, 20.0), (0.0, 8.0, 20.0),
        ]
        triangles = self.PLATE_TRIANGLES + [(4, 5, 6), (4, 6, 7)]
        geometry = make_geometry(vertices, triangles)
        structure = make_structure({"a.obj": triangles}, {"a.obj": 0.0})
        assert object_footprints.structure_ring(
            structure, {"a.obj": geometry},
            [make_placement("a.obj")]) is not None

    def test_area_cap_returns_none_and_reports(self, fake_projection,
                                               monkeypatch):
        geometry = make_geometry(L_SHAPE_VERTICES, L_SHAPE_TRIANGLES)
        structure = make_structure({"a.obj": L_SHAPE_TRIANGLES},
                                   {"a.obj": 0.0})
        placements = [make_placement("a.obj")]
        # Under the 100,000 m2 backstop default (defect 2026-07-17): the
        # 350 m2 hull is far below the cap and admitted.
        assert object_footprints.structure_ring(
            structure, {"a.obj": geometry}, placements) is not None
        # Cap enabled below the hull area: None, and reported.
        reports = []
        monkeypatch.setattr(
            object_footprints.UI, "vprint",
            lambda level, message: reports.append(message))
        monkeypatch.setattr(config, "DSF_OBJECT_MAX_FOOTPRINT_AREA_M2",
                            100.0)
        assert object_footprints.structure_ring(
            structure, {"a.obj": geometry}, placements) is None
        assert len(reports) == 1
        assert "exceeds" in reports[0] and "cap" in reports[0]

    def test_structure_span_gate_returns_none_and_reports(
            self, fake_projection, monkeypatch):
        # Defect 2026-07-17: a field-spanning structure (residual chained
        # hull) is skipped-and-reported through the same path as the area
        # cap.  A 600 m long, 10 m wide flat slab: 6,000 m2 (well under the
        # backstop) but its 600 m span trips the structure span gate.
        long_slab_vertices = [
            (0.0, 0.0, 0.0), (600.0, 0.0, 0.0),
            (600.0, 0.0, 10.0), (0.0, 0.0, 10.0),
        ]
        long_slab_triangles = [(0, 1, 2), (0, 2, 3)]
        geometry = make_geometry(long_slab_vertices, long_slab_triangles)
        structure = make_structure({"a.obj": long_slab_triangles},
                                   {"a.obj": 0.0})
        placements = [make_placement("a.obj")]
        # Gate disabled (the 0.0 shipping default): the slab is admitted.
        assert object_footprints.structure_ring(
            structure, {"a.obj": geometry}, placements) is not None
        # Gate enabled at 500 m: the 600 m-spanning slab is skipped-and-
        # reported through the same path as the area cap.
        reports = []
        monkeypatch.setattr(
            object_footprints.UI, "vprint",
            lambda level, message: reports.append(message))
        monkeypatch.setattr(config, "DSF_OBJECT_MAX_STRUCTURE_SPAN_M", 500.0)
        assert object_footprints.structure_ring(
            structure, {"a.obj": geometry}, placements) is None
        assert any("structure span gate" in message for message in reports)

    def test_two_placements_project_through_their_own_anchor(
            self, fake_projection):
        # One structure fed by TWO objects with different anchors and
        # headings (spec section 2.4: the anchor is a per-object
        # property).  Each object is the same local 10 x 10 slab; the
        # ring must span both PLACED positions.
        slab_vertices = [
            (0.0, 0.0, 0.0), (10.0, 0.0, 0.0),
            (10.0, 0.0, 10.0), (0.0, 0.0, 10.0),
        ]
        slab_triangles = [(0, 1, 2), (0, 2, 3)]
        geometry = make_geometry(slab_vertices, slab_triangles)
        metres_per_degree_longitude = (
            METRES_PER_DEGREE_LATITUDE
            * math.cos(math.radians(ANCHOR_LATITUDE)))
        placements = [
            make_placement("walls.obj", definition_index=0),
            # 100 m east, rotated 90 degrees clockwise: local +x maps
            # to south, local +z maps to west.
            make_placement(
                "roof.obj",
                longitude=(ANCHOR_LONGITUDE
                           + 100.0 / metres_per_degree_longitude),
                heading_degrees=90.0,
                definition_index=1),
        ]
        structure = make_structure(
            {"walls.obj": slab_triangles, "roof.obj": slab_triangles},
            {"walls.obj": 0.0, "roof.obj": 0.0})
        ring = object_footprints.structure_ring(
            structure,
            {"walls.obj": geometry, "roof.obj": geometry},
            placements)
        assert ring is not None
        local = ring_to_local_metres(ring)
        xs = [x for x, _ in local]
        zs = [z for _, z in local]
        # walls.obj covers x 0..10, z 0..10.  roof.obj at heading 90:
        # east = -z (90..100 from its +100 m anchor), south = x (0..10).
        assert min(xs) == pytest.approx(0.0, abs=0.1)
        assert max(xs) == pytest.approx(100.0, abs=0.1)
        assert min(zs) == pytest.approx(0.0, abs=0.1)
        assert max(zs) == pytest.approx(10.0, abs=0.1)


class TestStructureRingUnion:
    def test_union_ring_preserves_l_shape(self, fake_projection,
                                          monkeypatch):
        monkeypatch.setattr(config, "DSF_OBJECT_FOOTPRINT_UNION", True)
        geometry = make_geometry(L_SHAPE_VERTICES, L_SHAPE_TRIANGLES)
        structure = make_structure({"a.obj": L_SHAPE_TRIANGLES},
                                   {"a.obj": 0.0})
        ring = object_footprints.structure_ring(
            structure, {"a.obj": geometry}, [make_placement("a.obj")])
        assert ring is not None
        assert ring[0] != ring[-1]
        union_polygon = Polygon(ring_to_local_metres(ring))
        # The faithful ring keeps the notch: 300 m2, not the hull's 350.
        assert union_polygon.area == pytest.approx(300.0, abs=5.0)


# ── tier (b): read_dsf_object_buildings plumbing ─────────────────────

def _write_fake_dsf(tmp_path, body):
    """Harness pattern (b) from ``tests/test_dsf_buildings.py``: a fake
    ``.dsf`` plus a pre-seeded, mtime-backdated ``.dsf.text`` so
    ``_load_dsf_text`` uses the cache and DSFTool never runs — laid out
    as ``<pack>/Earth nav data/<group>/<tile>.dsf`` so
    ``_pack_root_for_dsf`` resolves the pack."""
    pack_root = tmp_path / "Fake Scenery Pack"
    dsf_directory = pack_root / "Earth nav data" / "+30-090"
    dsf_directory.mkdir(parents=True)
    dsf = dsf_directory / "+35-081.dsf"
    dsf.write_text("binary-placeholder")
    text = dsf_directory / "+35-081.dsf.text"
    text.write_text(body)
    now = os.path.getmtime(text)
    os.utime(dsf, (now - 10, now - 10))
    return str(dsf), str(pack_root)


def _parse_placements_like_the_real_reader(dsf_text_lines,
                                           accept_resource=None,
                                           include_object_msl=False):
    """Trivial stand-in for ``obj8_reader.read_dsf_object_placements``
    (plain ``OBJECT`` only, honouring ``accept_resource``).

    Accepts (and ignores) ``include_object_msl`` to match the real
    reader's signature: ``read_dsf_object_buildings`` opts in so the
    Feature-B terrain classifier can see absolute-deck ``OBJECT_MSL``
    rows.  These synthetic DSFs carry only plain ``OBJECT`` rows, so
    there is nothing extra to emit."""
    definitions = []
    placements = []
    for line in dsf_text_lines:
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "OBJECT_DEF":
            definitions.append(line.split(None, 1)[1].strip())
        elif tokens[0] == "OBJECT":
            index = int(tokens[1])
            resource = definitions[index]
            if accept_resource is not None \
                    and not accept_resource(resource):
                continue
            placements.append(obj8_reader.ObjectPlacement(
                definition_index=index,
                resource_path=resource,
                longitude=float(tokens[2]),
                latitude=float(tokens[3]),
                heading_degrees=float(tokens[4]),
            ))
    return placements


class FakeObjectGeometry:
    """Duck-typed stand-in for ``obj8_reader.ObjectGeometry`` exposing
    exactly what the Phase 1 path consumes."""

    def __init__(self, vertices, solid_triangles):
        self.vertices = list(vertices)
        self.solid_triangles = list(solid_triangles)

    @property
    def has_solid_geometry(self):
        return bool(self.solid_triangles)

    def solid_reach_metres(self):
        used = {index for triangle in self.solid_triangles
                for index in triangle}
        return max((math.hypot(self.vertices[index][0],
                               self.vertices[index][2])
                    for index in used), default=0.0)


def _square_slab_geometry(side_metres):
    half = side_metres / 2.0
    vertices = [(-half, 0.0, -half), (half, 0.0, -half),
                (half, 0.0, half), (-half, 0.0, half)]
    return FakeObjectGeometry(vertices, [(0, 1, 2), (0, 2, 3)])


_DSF_BODY = "\n".join([
    "OBJECT_DEF Terminals/Hangar/big_bake.obj",
    "OBJECT_DEF objects/row_warehouse.obj",
    "OBJECT_DEF otros/cone.obj",
    "OBJECT_DEF Terminals/Hangar/missing_shed.obj",
    "OBJECT_DEF lib/airport/Common_Elements/Hangars/Lg_Maint.agp",
    "OBJECT 0 -80.930000 35.210000 0.000000",
    "OBJECT 1 -80.931000 35.211000 0.000000",     # I-5: two placements
    "OBJECT 1 -80.932000 35.212000 90.000000",    #      of one .obj
    "OBJECT 2 -80.933000 35.213000 0.000000",
    "OBJECT 3 -80.934000 35.214000 0.000000",
    "OBJECT 4 -80.935000 35.215000 0.000000",     # .agp: never a candidate
]) + "\n"


@pytest.fixture()
def object_building_harness(tmp_path, monkeypatch, fake_projection):
    """Fake DSF + monkeypatched ``obj8_reader`` + trivial
    ``object_anchor`` fakes (the Wave-2 stubs raise
    ``NotImplementedError`` until workstream W4 lands)."""
    monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
    dsf_path, pack_root = _write_fake_dsf(tmp_path, _DSF_BODY)

    # Physical files exist so the (absolute path, mtime) geometry cache
    # has an mtime to key on; the parse itself is monkeypatched.
    geometry_by_resource = {
        "Terminals/Hangar/big_bake.obj": _square_slab_geometry(80.0),
        "objects/row_warehouse.obj": _square_slab_geometry(60.0),
        "otros/cone.obj": _square_slab_geometry(1.0),  # reach << 25
    }
    physical_by_resource = {}
    geometry_by_physical = {}
    for resource_path, geometry in geometry_by_resource.items():
        physical = os.path.join(pack_root, *resource_path.split("/"))
        os.makedirs(os.path.dirname(physical), exist_ok=True)
        with open(physical, "w") as handle:
            handle.write("placeholder\n")
        physical_by_resource[resource_path] = physical
        geometry_by_physical[os.path.abspath(physical)] = geometry
    # "Terminals/Hangar/missing_shed.obj" stays unresolvable.

    resolve_calls = []

    def fake_resolve_object_resource(resource_path, pack_root_argument,
                                     xplane_root_argument):
        resolve_calls.append((resource_path, pack_root_argument,
                              xplane_root_argument))
        return physical_by_resource.get(resource_path)

    load_calls = []

    def fake_load_object_file(path):
        load_calls.append(path)
        return geometry_by_physical[os.path.abspath(path)]

    monkeypatch.setattr(obj8_reader, "read_dsf_object_placements",
                        _parse_placements_like_the_real_reader)
    monkeypatch.setattr(obj8_reader, "resolve_object_resource",
                        fake_resolve_object_resource)
    monkeypatch.setattr(obj8_reader, "load_object_file",
                        fake_load_object_file)

    pool_calls = []

    def fake_discover_object_pools(placements, resolved_paths,
                                   geometry_by_resource_argument, *,
                                   epsilon_metres):
        pool_calls.append((list(placements), dict(resolved_paths),
                           epsilon_metres))
        return [object_anchor.ObjectPool(
            placements=list(placements),
            resolved_paths=dict(resolved_paths))]

    partition_calls = []

    def fake_partition_structures(pool, geometry_by_resource_argument,
                                  *, epsilon_metres):
        partition_calls.append((pool, epsilon_metres))
        structures = []
        for placement in pool.placements:
            geometry = geometry_by_resource_argument[
                placement.resource_path]
            structures.append(make_structure(
                {placement.resource_path:
                     list(geometry.solid_triangles)},
                {placement.resource_path:
                     min(vertex[1] for vertex in geometry.vertices)},
            ))
        return structures

    monkeypatch.setattr(object_anchor, "discover_object_pools",
                        fake_discover_object_pools)
    monkeypatch.setattr(object_anchor, "partition_structures",
                        fake_partition_structures)

    class Harness:
        pass

    harness = Harness()
    harness.dsf_path = dsf_path
    harness.pack_root = pack_root
    harness.resolve_calls = resolve_calls
    harness.load_calls = load_calls
    harness.pool_calls = pool_calls
    harness.partition_calls = partition_calls
    return harness


class TestReadDsfObjectBuildings:
    def test_placements_become_buildings(self, object_building_harness):
        harness = object_building_harness
        buildings = D.read_dsf_object_buildings(
            harness.dsf_path, xplane_root="/nonexistent-xplane")
        # big_bake (1 placement) + row_warehouse (2 placements, each a
        # building — invariant I-5).  cone fails the 25 m reach floor,
        # missing_shed is unresolvable, the .agp is not a .obj.
        assert len(buildings) == 3
        for outer_ring, holes, role in buildings:
            assert role == "object"
            assert holes == []
            assert len(outer_ring) >= 3
            assert outer_ring[0] != outer_ring[-1]

    def test_multi_placement_definition_yields_one_building_each(
            self, object_building_harness):
        harness = object_building_harness
        buildings = D.read_dsf_object_buildings(
            harness.dsf_path, xplane_root=None)
        warehouse_centroids = []
        for outer_ring, _holes, _role in buildings:
            polygon = Polygon(outer_ring)
            centroid = polygon.centroid
            if abs(centroid.x - -80.930) > 2e-4:     # not big_bake
                warehouse_centroids.append((centroid.x, centroid.y))
        assert len(warehouse_centroids) == 2
        # Each footprint sits at its OWN placement anchor.
        anchors = {(-80.931, 35.211), (-80.932, 35.212)}
        for centroid_longitude, centroid_latitude in warehouse_centroids:
            assert min(
                math.hypot(centroid_longitude - anchor_longitude,
                           centroid_latitude - anchor_latitude)
                for anchor_longitude, anchor_latitude in anchors) < 2e-5
        # And the multi-placement resource never entered the shared
        # pooling (an ObjectPool carries one placement per resource).
        (pooled_placements, _resolved, _epsilon) = \
            harness.pool_calls[0]
        assert [p.resource_path for p in pooled_placements] == [
            "Terminals/Hangar/big_bake.obj"]

    def test_resolution_receives_pack_root_and_xplane_root(
            self, object_building_harness):
        harness = object_building_harness
        D.read_dsf_object_buildings(
            harness.dsf_path, xplane_root="/some/xplane/root")
        assert harness.resolve_calls, "resolver was never consulted"
        resolved_resources = set()
        for (resource_path, pack_root_argument,
                xplane_root_argument) in harness.resolve_calls:
            resolved_resources.add(resource_path)
            assert pack_root_argument == harness.pack_root
            assert xplane_root_argument == "/some/xplane/root"
        # Only .obj resources reach resolution; the .agp was filtered by
        # the accept_resource predicate.
        assert resolved_resources == {
            "Terminals/Hangar/big_bake.obj",
            "objects/row_warehouse.obj",
            "otros/cone.obj",
            "Terminals/Hangar/missing_shed.obj",
        }

    def test_reach_floor_filters_compact_objects(
            self, object_building_harness):
        harness = object_building_harness
        buildings = D.read_dsf_object_buildings(
            harness.dsf_path, xplane_root=None)
        for outer_ring, _holes, _role in buildings:
            centroid = Polygon(outer_ring).centroid
            # Nothing footprinted at the cone's anchor.
            assert math.hypot(centroid.x - -80.933,
                              centroid.y - 35.213) > 1e-4

    def test_partition_epsilon_comes_from_config(
            self, object_building_harness, monkeypatch):
        harness = object_building_harness
        monkeypatch.setattr(config, "DSF_OBJECT_CONTACT_EPSILON_M", 0.4)
        D.read_dsf_object_buildings(harness.dsf_path, xplane_root=None)
        assert harness.pool_calls[-1][2] == 0.4
        assert all(epsilon == 0.4
                   for _pool, epsilon in harness.partition_calls[-3:])

    def test_geometry_cache_parses_each_file_once(
            self, object_building_harness):
        harness = object_building_harness
        D.read_dsf_object_buildings(harness.dsf_path, xplane_root=None)
        first_load_count = len(harness.load_calls)
        assert first_load_count > 0
        D.read_dsf_object_buildings(harness.dsf_path, xplane_root=None)
        # Second read: every geometry served from the
        # (absolute path, mtime) cache.
        assert len(harness.load_calls) == first_load_count

    def test_no_object_placements_returns_empty(self, tmp_path,
                                                monkeypatch,
                                                fake_projection):
        monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
        monkeypatch.setattr(obj8_reader, "read_dsf_object_placements",
                            _parse_placements_like_the_real_reader)
        dsf_path, _pack_root = _write_fake_dsf(
            tmp_path, "POLYGON_DEF lib/airport/pavement/asphalt.pol\n")
        assert D.read_dsf_object_buildings(dsf_path,
                                           xplane_root=None) == []

    def test_terrain_classified_resource_excluded_from_pool(
            self, object_building_harness, monkeypatch):
        """Defect 2026-07-17 (EGLL Building36): a resource the Feature-B
        classifier consumes as tunnel/bridge/deck terrain is dropped
        from the building pool BEFORE pooling, so it can never chain
        into a pad.  Here ``objects/row_warehouse.obj`` (its two
        placements would be two buildings) is returned in the
        classifier's exclusions; only ``big_bake.obj``'s single building
        must survive."""
        from auto_patch import object_terrain_features

        harness = object_building_harness
        pack_root = harness.pack_root

        def fake_classify(placements, geometry_by_resource, *,
                          pavement_polygons_longitude_latitude=None,
                          mean_sea_level_placements=None, pack_root="",
                          **kwargs):
            return object_terrain_features.ClassificationResult(
                tunnels=[], bridges=[],
                exclusions=[(pack_root, "objects/row_warehouse.obj")])

        monkeypatch.setattr(object_terrain_features,
                            "classify_object_terrain_features",
                            fake_classify)

        buildings = D.read_dsf_object_buildings(
            harness.dsf_path, xplane_root="/nonexistent-xplane")
        # big_bake alone remains; the two row_warehouse placements are
        # excluded as classified terrain — the reader never pooled them.
        assert len(buildings) == 1
        pooled_resources = {
            resource
            for _placements, resolved_paths, _epsilon in harness.pool_calls
            for resource in resolved_paths}
        assert "objects/row_warehouse.obj" not in pooled_resources
        assert "Terminals/Hangar/big_bake.obj" in pooled_resources

    def test_terrain_exclusion_off_when_feature_gate_off(
            self, object_building_harness, monkeypatch):
        """With ``OBJECT_BRIDGE_TERRAIN`` off the classifier never runs,
        so no resource is excluded and the full building set is
        emitted (the pre-feature behaviour)."""
        from auto_patch import object_terrain_features

        monkeypatch.setattr(config, "OBJECT_BRIDGE_TERRAIN", False)

        def exploding_classify(*args, **kwargs):
            raise AssertionError(
                "classifier must not run when the gate is off")

        monkeypatch.setattr(object_terrain_features,
                            "classify_object_terrain_features",
                            exploding_classify)

        harness = object_building_harness
        buildings = D.read_dsf_object_buildings(
            harness.dsf_path, xplane_root="/nonexistent-xplane")
        assert len(buildings) == 3


# ── footprint sidecar cache (data root Airport_mod_cache/<pack>/) ────

_FOOTPRINT_LEGACY_SIDECAR_NAME = "o4_object_footprints.cache"


class TestObjectFootprintCache:
    """The sidecar cache around ``read_dsf_object_buildings`` — a warm
    hit must skip the O(n^2) contact-graph partition entirely and
    reproduce the ring set byte-for-byte, invalidate on any ``.obj``
    edit, and degrade safely on a corrupt sidecar or a disabled gate.
    Per the user ruling 2026-07-15 the sidecar lives under the data
    root's ``Airport_mod_cache/<pack name>/`` — never inside the pack —
    and any pre-ruling in-pack sidecar is removed on resolution."""

    @pytest.fixture(autouse=True)
    def _isolated_data_root(self, tmp_path, monkeypatch):
        """Pin the data root under ``tmp_path`` — ``ORTHO4XP_DATA_ROOT``
        wins ``O4_File_Names.resolve_data_root`` — so sidecars never
        escape the test sandbox."""
        self.data_root = tmp_path / "o4root"
        monkeypatch.setenv("ORTHO4XP_DATA_ROOT", str(self.data_root))

    def _sidecar_path(self, harness):
        pack_name = os.path.basename(
            os.path.abspath(harness.pack_root))
        dsf_stem = os.path.splitext(
            os.path.basename(harness.dsf_path))[0]
        return os.path.join(
            str(self.data_root), "Airport_mod_cache", pack_name,
            f"o4_object_footprints_{dsf_stem}.cache")

    def _bump_obj_mtime(self, harness, resource_relative_path):
        obj_path = os.path.join(harness.pack_root,
                                *resource_relative_path.split("/"))
        file_stat = os.stat(obj_path)
        os.utime(obj_path,
                 (file_stat.st_atime + 100, file_stat.st_mtime + 100))

    def test_warm_hit_returns_equal_and_skips_partition(
            self, object_building_harness):
        harness = object_building_harness
        first = D.read_dsf_object_buildings(harness.dsf_path,
                                            xplane_root=None)
        partitions_after_first = len(harness.partition_calls)
        assert partitions_after_first > 0
        assert os.path.isfile(self._sidecar_path(harness))

        second = D.read_dsf_object_buildings(harness.dsf_path,
                                             xplane_root=None)
        # Identical ring set …
        assert second == first
        # … produced WITHOUT re-running the partition (cache hit).
        assert len(harness.partition_calls) == partitions_after_first

    def test_touching_an_obj_invalidates(self, object_building_harness):
        harness = object_building_harness
        D.read_dsf_object_buildings(harness.dsf_path, xplane_root=None)
        partitions_before = len(harness.partition_calls)
        assert os.path.isfile(self._sidecar_path(harness))

        self._bump_obj_mtime(harness, "Terminals/Hangar/big_bake.obj")
        D.read_dsf_object_buildings(harness.dsf_path, xplane_root=None)
        # Stale fingerprint forces a full recompute (partition ran again).
        assert len(harness.partition_calls) > partitions_before

    def test_gate_zero_disables_read_and_write(
            self, object_building_harness, monkeypatch):
        harness = object_building_harness
        monkeypatch.setenv("O4_OBJECT_FOOTPRINT_CACHE", "0")
        D.read_dsf_object_buildings(harness.dsf_path, xplane_root=None)
        partitions_first = len(harness.partition_calls)
        # No sidecar is written when the gate is off.
        assert not os.path.isfile(self._sidecar_path(harness))
        D.read_dsf_object_buildings(harness.dsf_path, xplane_root=None)
        # And the second call recomputes rather than serving a cache.
        assert len(harness.partition_calls) > partitions_first

    def test_corrupt_sidecar_falls_back_to_recompute(
            self, object_building_harness):
        harness = object_building_harness
        first = D.read_dsf_object_buildings(harness.dsf_path,
                                            xplane_root=None)
        sidecar = self._sidecar_path(harness)
        assert os.path.isfile(sidecar)
        with open(sidecar, "wb") as handle:
            handle.write(b"not a valid pickle \x00\x01\x02")
        partitions_before = len(harness.partition_calls)

        second = D.read_dsf_object_buildings(harness.dsf_path,
                                             xplane_root=None)
        # A garbled sidecar never raises — it recomputes and rewrites …
        assert second == first
        assert len(harness.partition_calls) > partitions_before

    def test_no_pack_root_disables_caching(self, tmp_path):
        # With no resolvable pack root (None) or a non-directory, the
        # sidecar helper declines — the reader then behaves as it did
        # before the cache existed.
        loose_dsf = tmp_path / "loose.dsf"
        loose_dsf.write_text("binary-placeholder")
        assert D._object_footprint_sidecar(
            str(loose_dsf), None, 1.0, 25.0) == (None, None)
        assert D._object_footprint_sidecar(
            str(loose_dsf), str(tmp_path / "missing"), 1.0, 25.0) \
            == (None, None)

    def test_sidecar_lands_under_data_root_not_in_pack(
            self, object_building_harness):
        harness = object_building_harness
        D.read_dsf_object_buildings(harness.dsf_path, xplane_root=None)
        sidecar = self._sidecar_path(harness)
        assert os.path.isfile(sidecar)
        assert sidecar.startswith(
            os.path.join(str(self.data_root), "Airport_mod_cache"))
        # Nothing cache-shaped may land inside the scenery pack (user
        # ruling 2026-07-15).
        pack_files = []
        for directory, _subdirectories, file_names in os.walk(
                harness.pack_root):
            pack_files.extend(file_names)
        assert not any(name.endswith(".cache") for name in pack_files)

    def test_stale_legacy_in_pack_sidecar_removed(
            self, object_building_harness):
        harness = object_building_harness
        legacy = os.path.join(harness.pack_root,
                              _FOOTPRINT_LEGACY_SIDECAR_NAME)
        with open(legacy, "wb") as handle:
            handle.write(b"pre-ruling in-pack sidecar")
        D.read_dsf_object_buildings(harness.dsf_path, xplane_root=None)
        # The old in-pack file was cleaned up on sidecar resolution.
        assert not os.path.exists(legacy)


# ── tier (b), real workstream-W2 reader ──────────────────────────────

def _obj8_reader_is_implemented():
    try:
        obj8_reader.read_dsf_object_placements([])
    except NotImplementedError:
        return False
    except Exception:
        return True
    return True


_REAL_BOX_OBJ = "\n".join([
    "A",
    "800",
    "OBJ",
    "",
    "POINT_COUNTS 8 0 0 12",
    # 40 x 40 base at y = 0 (reach ~28 m > the 25 m floor), roof at y=3.
    "VT -20.0 0.0 -20.0 0 1 0 0 0",
    "VT 20.0 0.0 -20.0 0 1 0 0 0",
    "VT 20.0 0.0 20.0 0 1 0 0 0",
    "VT -20.0 0.0 20.0 0 1 0 0 0",
    "VT -20.0 3.0 -20.0 0 1 0 0 0",
    "VT 20.0 3.0 -20.0 0 1 0 0 0",
    "VT 20.0 3.0 20.0 0 1 0 0 0",
    "VT -20.0 3.0 20.0 0 1 0 0 0",
    "IDX10 0 1 2 0 2 3 4 5 6 4",
    "IDX 6",
    "IDX 7",
    "TRIS 0 12",
]) + "\n"

_REAL_SMALL_BOX_OBJ = _REAL_BOX_OBJ.replace("20.0", "5.0")


@pytest.mark.skipif(
    not _obj8_reader_is_implemented(),
    reason="pending workstream W2 (obj8_reader still raises "
           "NotImplementedError)")
class TestReadDsfObjectBuildingsRealReader:
    """Tier (b) with the REAL obj8_reader (no reader monkeypatch; the
    Wave-2 partition fakes remain)."""

    @pytest.fixture()
    def partition_fakes(self, monkeypatch):
        def fake_discover_object_pools(placements, resolved_paths,
                                       geometry_by_resource, *,
                                       epsilon_metres):
            return [object_anchor.ObjectPool(
                placements=list(placements),
                resolved_paths=dict(resolved_paths))]

        def fake_partition_structures(pool, geometry_by_resource, *,
                                      epsilon_metres):
            structures = []
            for placement in pool.placements:
                geometry = geometry_by_resource[placement.resource_path]
                structures.append(make_structure(
                    {placement.resource_path:
                         list(geometry.solid_triangles)},
                    {placement.resource_path:
                         min(vertex[1]
                             for vertex in geometry.vertices)},
                ))
            return structures

        monkeypatch.setattr(object_anchor, "discover_object_pools",
                            fake_discover_object_pools)
        monkeypatch.setattr(object_anchor, "partition_structures",
                            fake_partition_structures)

    def test_synthetic_boxes_footprint_end_to_end(
            self, tmp_path, monkeypatch, partition_fakes):
        monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
        body = "\n".join([
            "OBJECT_DEF Terminals/Hangar/pack_box.obj",
            "OBJECT 0 -80.930000 35.210000 0.000000",
        ]) + "\n"
        dsf_path, pack_root = _write_fake_dsf(tmp_path, body)
        physical = os.path.join(pack_root, "Terminals", "Hangar",
                                "pack_box.obj")
        os.makedirs(os.path.dirname(physical), exist_ok=True)
        with open(physical, "w") as handle:
            handle.write(_REAL_BOX_OBJ)

        buildings = D.read_dsf_object_buildings(dsf_path,
                                                xplane_root=None)
        assert len(buildings) == 1
        outer_ring, holes, role = buildings[0]
        assert role == "object" and holes == []
        polygon = Polygon(outer_ring)
        centroid = polygon.centroid
        assert centroid.x == pytest.approx(-80.930, abs=2e-5)
        assert centroid.y == pytest.approx(35.210, abs=2e-5)
        # 40 m base box → ~1600 m2 in local metres.
        metres_per_degree_longitude = (
            METRES_PER_DEGREE_LATITUDE
            * math.cos(math.radians(35.210)))
        area_square_metres = (polygon.area
                              * METRES_PER_DEGREE_LATITUDE
                              * metres_per_degree_longitude)
        assert area_square_metres == pytest.approx(1600.0, rel=0.02)

    def test_pack_relative_resolution_beats_library(
            self, tmp_path, monkeypatch, partition_fakes):
        monkeypatch.setattr(D, "_dsftool_path", lambda: "/bin/true")
        body = "\n".join([
            "OBJECT_DEF Terminals/Hangar/pack_box.obj",
            "OBJECT 0 -80.930000 35.210000 0.000000",
        ]) + "\n"
        dsf_path, pack_root = _write_fake_dsf(tmp_path, body)
        # Pack-relative candidate: the 40 m box.
        pack_physical = os.path.join(pack_root, "Terminals", "Hangar",
                                     "pack_box.obj")
        os.makedirs(os.path.dirname(pack_physical), exist_ok=True)
        with open(pack_physical, "w") as handle:
            handle.write(_REAL_BOX_OBJ)
        # A library.txt in a synthetic X-Plane root exports the SAME
        # virtual path to a DIFFERENT (10 m) box.
        xplane_root = tmp_path / "XPlane"
        library_pack = xplane_root / "Custom Scenery" / "TestLibrary"
        library_pack.mkdir(parents=True)
        library_physical = library_pack / "library_box.obj"
        library_physical.write_text(_REAL_SMALL_BOX_OBJ)
        (library_pack / "library.txt").write_text(
            "A\n800\nLIBRARY\n\n"
            "EXPORT Terminals/Hangar/pack_box.obj library_box.obj\n")

        buildings = D.read_dsf_object_buildings(
            dsf_path, xplane_root=str(xplane_root))
        assert len(buildings) == 1
        polygon = Polygon(buildings[0][0])
        metres_per_degree_longitude = (
            METRES_PER_DEGREE_LATITUDE
            * math.cos(math.radians(35.210)))
        area_square_metres = (polygon.area
                              * METRES_PER_DEGREE_LATITUDE
                              * metres_per_degree_longitude)
        # Pack-relative wins: the 40 m box (~1600 m2), not the library's
        # 10 m box (~100 m2).
        assert area_square_metres == pytest.approx(1600.0, rel=0.02)


# ── tier (c): flag gating and the shared admission helper ────────────

class TestPipelineFlagGating:
    def test_flag_off_object_reader_never_called(self, monkeypatch):
        def sentinel(*arguments, **keyword_arguments):
            raise AssertionError(
                "read_dsf_object_buildings must not be called with "
                "DSF_OBJECT_BUILDINGS off")

        monkeypatch.setattr(D, "read_dsf_object_buildings", sentinel)
        monkeypatch.setattr(config, "DSF_OBJECT_BUILDINGS", False)
        admitted = pipeline._collect_dsf_object_building_footprints(
            "/nonexistent.dsf", None,
            lambda outer_ring, hole_rings: True)
        assert admitted == 0

    def test_flag_on_object_reader_feeds_admission(self, monkeypatch):
        ring = [(-80.930, 35.210), (-80.929, 35.210), (-80.929, 35.211)]
        monkeypatch.setattr(
            D, "read_dsf_object_buildings",
            lambda dsf_path, cache_dir=None, xplane_root=None: [
                (ring, [], "object"),
                ([(-80.0, 35.0)], [], "object"),    # < 3 vertices: skipped
            ])
        monkeypatch.setattr(config, "DSF_OBJECT_BUILDINGS", True)
        admitted_rings = []

        def admit(outer_ring, hole_rings):
            admitted_rings.append((outer_ring, hole_rings))
            return True

        admitted = pipeline._collect_dsf_object_building_footprints(
            "/fake.dsf", "/fake/xplane", admit)
        assert admitted == 1
        assert admitted_rings == [(ring, [])]


def _scaled_to_metres_transform(longitude, latitude, altitude=None):
    """A tiny stand-in for the pipeline's local-metres projection."""
    return longitude * 1000.0, latitude * 1000.0


class TestAdmissionHelper:
    """The single downstream path both DSF building sources share
    (the ``.fac`` behavior itself is pinned by the untouched
    ``tests/test_dsf_buildings.py`` / ``tests/test_dsf_surface_pavement.py``)."""

    def test_valid_ring_is_admitted(self):
        pool = []
        assert pipeline._admit_dsf_building_footprint(
            [(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01)],
            [], _scaled_to_metres_transform, None, None, pool)
        assert len(pool) == 1
        assert pool[0].area == pytest.approx(100.0)

    def test_bounding_box_reject(self):
        pool = []
        assert not pipeline._admit_dsf_building_footprint(
            [(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01)],
            [], _scaled_to_metres_transform,
            (100.0, 100.0, 200.0, 200.0),   # far away
            None, pool)
        assert pool == []

    def test_boundary_centroid_gate(self):
        inside_gate = Polygon([(-1.0, -1.0), (20.0, -1.0),
                               (20.0, 20.0), (-1.0, 20.0)])
        pool = []
        assert pipeline._admit_dsf_building_footprint(
            [(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01)],
            [], _scaled_to_metres_transform, None, inside_gate, pool)
        far_gate = Polygon([(500.0, 500.0), (600.0, 500.0),
                            (600.0, 600.0), (500.0, 600.0)])
        assert not pipeline._admit_dsf_building_footprint(
            [(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01)],
            [], _scaled_to_metres_transform, None, far_gate, pool)
        assert len(pool) == 1

    def test_invalid_ring_repaired_or_rejected(self):
        # A bow-tie ring is buffer(0)-repaired to a valid Polygon and
        # admitted — exactly the pre-refactor inline behavior.
        pool = []
        assert pipeline._admit_dsf_building_footprint(
            [(0.0, 0.0), (0.01, 0.01), (0.01, 0.0), (0.0, 0.01)],
            [], _scaled_to_metres_transform, None, None, pool)
        assert len(pool) == 1 and pool[0].is_valid
        # A degenerate (collinear, zero-area) ring repairs to empty and
        # is rejected.
        assert not pipeline._admit_dsf_building_footprint(
            [(0.0, 0.0), (0.01, 0.01), (0.02, 0.02), (0.03, 0.03)],
            [], _scaled_to_metres_transform, None, None, pool)
        assert len(pool) == 1

    def test_short_hole_rings_are_ignored(self):
        pool = []
        assert pipeline._admit_dsf_building_footprint(
            [(0.0, 0.0), (0.01, 0.0), (0.01, 0.01), (0.0, 0.01)],
            [[(0.001, 0.001), (0.002, 0.002)]],     # only 2 vertices
            _scaled_to_metres_transform, None, None, pool)
        assert len(pool) == 1
        assert pool[0].area == pytest.approx(100.0)
