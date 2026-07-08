"""Tests for ``auto_patch.object_anchor`` (workstream W4).

Hermetic tiers use synthetic geometry and small hand-written mesh files
in ``tmp_path`` (the ``O4_Mesh_Utils.write_mesh_file`` format, same
idiom as ``tests/fixtures/mesh/synthetic_fan_three_triangles.mesh``):

* a PLANE mesh whose elevation is linear in longitude — barycentric
  interpolation reproduces the plane exactly, so every expected ground
  value is computable in closed form; and
* a PIT mesh (four corners at 100 m, centre at 90 m) for the
  amendment-A3 pathological case where correction worsens the seating.

The one test that matters most is the invariant-I-3 test: two objects
with anchors ~10 metres apart on sloped terrain contributing abutting
parts to ONE structure must receive DIFFERENT deltas — and their
world-coincident vertices must land at the same post-bake rendered
elevation ``ground(anchor(O)) + y + delta(O)`` (the invariant-I-21
form; deltas are never compared for equality across anchors).  The
prototype ``tools/reanchor_kclt_terminal_bakes.py`` cannot pass it.

The integration smoke test at the bottom runs the REAL KCLT eight-bake
pool end-to-end against the installed pack and built mesh (read-only:
geometry comes from the ``.anchor_bak`` originals); it skips when
either is absent.
"""

from __future__ import annotations

import math
import os

import pytest

from auto_patch import obj8_reader
from auto_patch.mesh_sampler import MeshElevationSampler
from auto_patch.object_anchor import (
    ObjectPool,
    discover_object_pools,
    partition_structures,
    structure_deltas,
)

METRES_PER_DEGREE_LATITUDE = obj8_reader.METRES_PER_DEGREE_LATITUDE

CONTACT_EPSILON_METRES = 0.25


# ── construction helpers ──────────────────────────────────────────────


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


def make_placement(
    resource_path,
    latitude,
    longitude,
    heading_degrees=0.0,
    definition_index=0,
):
    return obj8_reader.ObjectPlacement(
        definition_index=definition_index,
        resource_path=resource_path,
        longitude=longitude,
        latitude=latitude,
        heading_degrees=heading_degrees,
    )


def box_vertices_and_triangles(
    minimum_x,
    maximum_x,
    minimum_y,
    maximum_y,
    minimum_z,
    maximum_z,
    index_offset=0,
):
    """A closed axis-aligned box: 8 vertices, 12 triangles."""
    vertices = [
        (minimum_x, minimum_y, minimum_z),
        (maximum_x, minimum_y, minimum_z),
        (maximum_x, minimum_y, maximum_z),
        (minimum_x, minimum_y, maximum_z),
        (minimum_x, maximum_y, minimum_z),
        (maximum_x, maximum_y, minimum_z),
        (maximum_x, maximum_y, maximum_z),
        (minimum_x, maximum_y, maximum_z),
    ]
    corner_triangles = [
        (0, 1, 2), (0, 2, 3),   # bottom
        (4, 5, 6), (4, 6, 7),   # top
        (0, 1, 5), (0, 5, 4),   # side z = minimum
        (1, 2, 6), (1, 6, 5),   # side x = maximum
        (2, 3, 7), (2, 7, 6),   # side z = maximum
        (3, 0, 4), (3, 4, 7),   # side x = minimum
    ]
    triangles = [
        tuple(index + index_offset for index in triangle)
        for triangle in corner_triangles
    ]
    return vertices, triangles


def compound_geometry(*boxes):
    """One geometry holding several disjoint boxes (each box is
    ``(minimum_x, maximum_x, minimum_y, maximum_y, minimum_z,
    maximum_z)``)."""
    vertices = []
    triangles = []
    for box in boxes:
        box_vertices, box_triangles = box_vertices_and_triangles(
            *box, index_offset=len(vertices)
        )
        vertices.extend(box_vertices)
        triangles.extend(box_triangles)
    return make_geometry(vertices, triangles)


def metres_per_degree_longitude_at(latitude):
    return METRES_PER_DEGREE_LATITUDE * math.cos(math.radians(latitude))


# ── synthetic mesh files ──────────────────────────────────────────────


def write_mesh_file(path, vertices, one_based_triangles):
    """Write a mesh in the exact ``O4_Mesh_Utils.write_mesh_file``
    format: elevation column divided by 100000, 1-based triangle
    indices, a ``Normals`` section between vertices and triangles."""
    lines = ["MeshVersionFormatted 2", "Dimension 3", "", "Vertices",
             str(len(vertices))]
    for longitude, latitude, elevation_metres in vertices:
        lines.append(
            f"{longitude:.9f} {latitude:.9f} "
            f"{elevation_metres / 100000.0:.12f} 0"
        )
    lines.extend(["", "Normals", str(len(vertices))])
    lines.extend(["0 0"] * len(vertices))
    lines.extend(["", "Triangles", str(len(one_based_triangles))])
    for first, second, third in one_based_triangles:
        lines.append(f"{first} {second} {third} 0")
    lines.append("End")
    with open(path, "w") as handle:
        handle.write("\n".join(lines) + "\n")


# The PLANE mesh: elevation linear in longitude over a 0.004-degree
# square, so barycentric interpolation reproduces it exactly anywhere.
PLANE_MINIMUM_LONGITUDE = 10.0
PLANE_MINIMUM_LATITUDE = 50.0
PLANE_SPAN_DEGREES = 0.004
PLANE_BASE_ELEVATION_METRES = 100.0
PLANE_ELEVATION_PER_DEGREE_LONGITUDE = 20000.0

# The default anchor for plane-mesh tests, comfortably inside the mesh.
PLANE_ANCHOR_LATITUDE = 50.002
PLANE_ANCHOR_LONGITUDE = 10.0015


def plane_ground(longitude):
    return PLANE_BASE_ELEVATION_METRES + (
        PLANE_ELEVATION_PER_DEGREE_LONGITUDE
        * (longitude - PLANE_MINIMUM_LONGITUDE)
    )


@pytest.fixture()
def plane_sampler(tmp_path):
    maximum_longitude = PLANE_MINIMUM_LONGITUDE + PLANE_SPAN_DEGREES
    maximum_latitude = PLANE_MINIMUM_LATITUDE + PLANE_SPAN_DEGREES
    corners = [
        (PLANE_MINIMUM_LONGITUDE, PLANE_MINIMUM_LATITUDE),
        (maximum_longitude, PLANE_MINIMUM_LATITUDE),
        (maximum_longitude, maximum_latitude),
        (PLANE_MINIMUM_LONGITUDE, maximum_latitude),
    ]
    vertices = [
        (longitude, latitude, plane_ground(longitude))
        for longitude, latitude in corners
    ]
    mesh_path = os.path.join(tmp_path, "anchor_plane.mesh")
    write_mesh_file(mesh_path, vertices, [(1, 2, 3), (1, 3, 4)])
    return MeshElevationSampler(
        mesh_path,
        (
            PLANE_MINIMUM_LONGITUDE,
            PLANE_MINIMUM_LATITUDE,
            maximum_longitude,
            maximum_latitude,
        ),
        margin_degrees=0.0,
    )


# The PIT mesh: four corners at 100 m, the centre vertex at 90 m,
# fanned into four triangles.  Symmetric about the centre, so two
# points mirrored through the centre sample the same elevation.
PIT_MINIMUM_LONGITUDE = 10.0
PIT_MINIMUM_LATITUDE = 50.0
PIT_SPAN_DEGREES = 0.002
PIT_CENTRE_LONGITUDE = PIT_MINIMUM_LONGITUDE + PIT_SPAN_DEGREES / 2.0
PIT_CENTRE_LATITUDE = PIT_MINIMUM_LATITUDE + PIT_SPAN_DEGREES / 2.0
PIT_RIM_ELEVATION_METRES = 100.0
PIT_CENTRE_ELEVATION_METRES = 90.0


@pytest.fixture()
def pit_sampler(tmp_path):
    maximum_longitude = PIT_MINIMUM_LONGITUDE + PIT_SPAN_DEGREES
    maximum_latitude = PIT_MINIMUM_LATITUDE + PIT_SPAN_DEGREES
    vertices = [
        (PIT_MINIMUM_LONGITUDE, PIT_MINIMUM_LATITUDE,
         PIT_RIM_ELEVATION_METRES),
        (maximum_longitude, PIT_MINIMUM_LATITUDE,
         PIT_RIM_ELEVATION_METRES),
        (maximum_longitude, maximum_latitude, PIT_RIM_ELEVATION_METRES),
        (PIT_MINIMUM_LONGITUDE, maximum_latitude,
         PIT_RIM_ELEVATION_METRES),
        (PIT_CENTRE_LONGITUDE, PIT_CENTRE_LATITUDE,
         PIT_CENTRE_ELEVATION_METRES),
    ]
    triangles = [(1, 2, 5), (2, 3, 5), (3, 4, 5), (4, 1, 5)]
    mesh_path = os.path.join(tmp_path, "anchor_pit.mesh")
    write_mesh_file(mesh_path, vertices, triangles)
    return MeshElevationSampler(
        mesh_path,
        (
            PIT_MINIMUM_LONGITUDE,
            PIT_MINIMUM_LATITUDE,
            maximum_longitude,
            maximum_latitude,
        ),
        margin_degrees=0.0,
    )


# ── THE invariant-I-3 test ────────────────────────────────────────────


class TestPerObjectDeltas:
    """Spec section 2.4 / invariant I-3: the y offset belongs to the
    (structure, object) pair.  The prototype (one shared anchor assumed)
    cannot pass this."""

    def _build_two_anchor_structure(self, plane_sampler):
        anchor_latitude = PLANE_ANCHOR_LATITUDE
        walls_longitude = PLANE_ANCHOR_LONGITUDE
        metres_per_degree = metres_per_degree_longitude_at(anchor_latitude)
        # The roof object's anchor sits ~10 m east of the walls object's.
        roof_longitude = walls_longitude + 10.0 / metres_per_degree

        # walls.obj: a box spanning local x 0..10, y 0..5.  roof.obj: a
        # box spanning local x 0..10, y 5..10 — anchored 10 m east, so in
        # WORLD space it spans east 10..20 of the walls anchor and abuts
        # the walls box along the east = 10 plane, with world-coincident
        # vertices at (east 10, y 5, z 0) and (east 10, y 5, z 10).
        walls_geometry = compound_geometry((0.0, 10.0, 0.0, 5.0, 0.0, 10.0))
        roof_geometry = compound_geometry((0.0, 10.0, 5.0, 10.0, 0.0, 10.0))
        placements = [
            make_placement("walls.obj", anchor_latitude, walls_longitude),
            make_placement(
                "roof.obj",
                anchor_latitude,
                roof_longitude,
                definition_index=1,
            ),
        ]
        geometry_by_resource = {
            "walls.obj": walls_geometry,
            "roof.obj": roof_geometry,
        }
        resolved_paths = {
            "walls.obj": "/nonexistent/walls.obj",
            "roof.obj": "/nonexistent/roof.obj",
        }
        pools = discover_object_pools(
            placements,
            resolved_paths,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(pools) == 1
        structures = partition_structures(
            pools[0],
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(structures) == 1
        decision = structure_deltas(
            pools[0], geometry_by_resource, structures, plane_sampler
        )
        return structures[0], decision

    def test_two_anchors_one_structure_different_deltas_same_rendered_elevation(
        self, plane_sampler
    ):
        structure, decision = self._build_two_anchor_structure(plane_sampler)
        assert decision.skipped == []
        assert decision.structures[0].skip_reason is None
        assert set(structure.triangles_by_resource) == {
            "walls.obj",
            "roof.obj",
        }

        walls_anchor_ground = decision.anchor_ground_by_resource["walls.obj"]
        roof_anchor_ground = decision.anchor_ground_by_resource["roof.obj"]
        # 10 m east on the plane slope is a substantial ground change.
        assert abs(roof_anchor_ground - walls_anchor_ground) > 2.0

        walls_deltas = decision.delta_by_resource_and_vertex["walls.obj"]
        roof_deltas = decision.delta_by_resource_and_vertex["roof.obj"]
        # Every vertex of each object's box received a delta.
        assert set(walls_deltas) == set(range(8))
        assert set(roof_deltas) == set(range(8))
        # One delta per (structure, object) pair — constant within each
        # object here (one structure), DIFFERENT across the two anchors.
        walls_delta = walls_deltas[0]
        roof_delta = roof_deltas[0]
        assert all(
            delta == pytest.approx(walls_delta, abs=1e-9)
            for delta in walls_deltas.values()
        )
        assert all(
            delta == pytest.approx(roof_delta, abs=1e-9)
            for delta in roof_deltas.values()
        )
        assert abs(walls_delta - roof_delta) > 2.0
        # delta(S, O) = ground(centroid(S)) - ground(anchor(O)), so the
        # delta difference is exactly the anchor-ground difference.
        assert walls_delta - roof_delta == pytest.approx(
            roof_anchor_ground - walls_anchor_ground, abs=1e-9
        )

        # Invariant I-21 (the hard-tear form): the world-coincident
        # vertices land at the same post-bake RENDERED elevation
        # ground(anchor(O)) + y + delta(O) — never compare deltas.
        # walls.obj vertex 5 is (10, 5, 0); roof.obj vertex 0 is
        # (0, 5, 0), world-coincident with it.
        walls_vertex_index = 5
        roof_vertex_index = 0
        walls_authored_y = 5.0
        roof_authored_y = 5.0
        rendered_walls = (
            walls_anchor_ground
            + walls_authored_y
            + walls_deltas[walls_vertex_index]
        )
        rendered_roof = (
            roof_anchor_ground
            + roof_authored_y
            + roof_deltas[roof_vertex_index]
        )
        assert rendered_walls == pytest.approx(rendered_roof, abs=1e-6)
        # And both equal ground(centroid(S)) + y.
        structure_ground = plane_sampler.elevation_at(
            structure.centroid_latitude, structure.centroid_longitude
        )
        assert rendered_walls == pytest.approx(
            structure_ground + walls_authored_y, abs=1e-6
        )

    def test_structure_spanning_two_resources_keeps_original_indices(
        self, plane_sampler
    ):
        structure, _decision = self._build_two_anchor_structure(
            plane_sampler
        )
        # Per-resource triangles carry the ORIGINAL per-object vertex
        # indices: each object contributed its whole 12-triangle box.
        walls_geometry_triangles = compound_geometry(
            (0.0, 10.0, 0.0, 5.0, 0.0, 10.0)
        ).solid_triangles
        assert sorted(structure.triangles_by_resource["walls.obj"]) == (
            sorted(walls_geometry_triangles)
        )
        assert sorted(structure.triangles_by_resource["roof.obj"]) == (
            sorted(walls_geometry_triangles)
        )  # same local box shape for both objects
        for triangles in structure.triangles_by_resource.values():
            assert all(
                0 <= vertex_index < 8
                for triangle in triangles
                for vertex_index in triangle
            )
        assert structure.minimum_base_y_by_resource == {
            "walls.obj": 0.0,
            "roof.obj": 5.0,
        }
        assert structure.is_ground_touching


# ── pooling (invariant I-1) ───────────────────────────────────────────


class TestDiscoverObjectPools:
    def _single_box_object(self, resource_path, anchor_latitude,
                           anchor_longitude, heading_degrees=0.0,
                           box=(0.0, 10.0, 0.0, 5.0, 0.0, 10.0)):
        placement = make_placement(
            resource_path, anchor_latitude, anchor_longitude,
            heading_degrees=heading_degrees,
        )
        return placement, compound_geometry(box)

    def test_overlapping_boxes_pool_and_disjoint_boxes_do_not(self):
        anchor_latitude = PLANE_ANCHOR_LATITUDE
        anchor_longitude = PLANE_ANCHOR_LONGITUDE
        metres_per_degree = metres_per_degree_longitude_at(anchor_latitude)
        placement_a, geometry_a = self._single_box_object(
            "a.obj", anchor_latitude, anchor_longitude
        )
        # 8 m east: world box 8..18 overlaps a.obj's 0..10.
        placement_near, geometry_near = self._single_box_object(
            "near.obj",
            anchor_latitude,
            anchor_longitude + 8.0 / metres_per_degree,
        )
        # 15 m east: world box 15..25, disjoint from a.obj's.
        placement_far, geometry_far = self._single_box_object(
            "far.obj",
            anchor_latitude,
            anchor_longitude + 15.0 / metres_per_degree,
        )
        geometry_by_resource = {
            "a.obj": geometry_a,
            "near.obj": geometry_near,
            "far.obj": geometry_far,
        }
        resolved_paths = {
            resource: f"/nonexistent/{resource}"
            for resource in geometry_by_resource
        }
        pools = discover_object_pools(
            [placement_a, placement_near, placement_far],
            resolved_paths,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        pooled_resources = [
            sorted(
                placement.resource_path for placement in pool.placements
            )
            for pool in pools
        ]
        # near.obj overlaps BOTH: a (8..10) and far (15..18) — one chain.
        assert pooled_resources == [["a.obj", "far.obj", "near.obj"]]

        # Without the bridge, a and far are separate pools.
        pools = discover_object_pools(
            [placement_a, placement_far],
            resolved_paths,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert [
            [placement.resource_path for placement in pool.placements]
            for pool in pools
        ] == [["a.obj"], ["far.obj"]]
        assert all(
            set(pool.resolved_paths)
            == {placement.resource_path for placement in pool.placements}
            for pool in pools
        )

    def test_transitive_chaining(self):
        anchor_latitude = PLANE_ANCHOR_LATITUDE
        anchor_longitude = PLANE_ANCHOR_LONGITUDE
        metres_per_degree = metres_per_degree_longitude_at(anchor_latitude)
        # a: 0..10, b: 9..19, c: 18..28 — a overlaps b, b overlaps c,
        # a and c are 8 m apart.
        placements = []
        geometry_by_resource = {}
        for name, east_offset in (("a.obj", 0.0), ("b.obj", 9.0),
                                  ("c.obj", 18.0)):
            placement, geometry = self._single_box_object(
                name,
                anchor_latitude,
                anchor_longitude + east_offset / metres_per_degree,
            )
            placements.append(placement)
            geometry_by_resource[name] = geometry
        resolved_paths = {
            resource: f"/nonexistent/{resource}"
            for resource in geometry_by_resource
        }
        pools = discover_object_pools(
            placements,
            resolved_paths,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(pools) == 1
        assert len(pools[0].placements) == 3

    def test_rotated_placement_pools_by_world_geometry(self):
        """Invariant I-1: a ~90-degree heading difference still pools
        when the PLACED geometry overlaps in world space — and the same
        geometry unrotated (whose box then lands elsewhere) does not.
        This fails if the box is projected through fewer than all four
        corners or without the heading rotation."""
        anchor_latitude = PLANE_ANCHOR_LATITUDE
        anchor_longitude = PLANE_ANCHOR_LONGITUDE
        metres_per_degree = metres_per_degree_longitude_at(anchor_latitude)
        placement_a, geometry_a = self._single_box_object(
            "a.obj", anchor_latitude, anchor_longitude
        )
        # b.obj's anchor is 5 m east, 30 m south of a.obj's.  Its local
        # box spans x -25..-15, z -2..2.  At heading 90 (east = -z,
        # south = x) the world box is east 3..7, south 5..15 — inside
        # a.obj's east 0..10, south 0..10 band.  At heading 0 the world
        # box is east -20..-10, south 28..32 — nowhere near it.
        b_latitude = anchor_latitude - 30.0 / METRES_PER_DEGREE_LATITUDE
        b_longitude = anchor_longitude + 5.0 / metres_per_degree
        b_box = (-25.0, -15.0, 0.0, 5.0, -2.0, 2.0)
        geometry_b = compound_geometry(b_box)
        geometry_by_resource = {"a.obj": geometry_a, "b.obj": geometry_b}
        resolved_paths = {
            resource: f"/nonexistent/{resource}"
            for resource in geometry_by_resource
        }

        rotated = make_placement(
            "b.obj", b_latitude, b_longitude, heading_degrees=90.0
        )
        pools = discover_object_pools(
            [placement_a, rotated],
            resolved_paths,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(pools) == 1

        unrotated = make_placement(
            "b.obj", b_latitude, b_longitude, heading_degrees=0.0
        )
        pools = discover_object_pools(
            [placement_a, unrotated],
            resolved_paths,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(pools) == 2


# ── inheritance (invariant I-8) ───────────────────────────────────────


class TestElevatedStructureInheritance:
    def test_containing_supporter_wins_over_nearest(self, plane_sampler):
        # One object, three structures: a small west slab (centroid at
        # east 5), a large east slab spanning 20..100 (centroid at 60),
        # and a hovering clutter box at 22..26 (centroid 24) with a base
        # 3 m up.  The clutter's centroid is INSIDE the east slab's box
        # but NEARER to the west slab's centroid — containment must win.
        geometry = compound_geometry(
            (0.0, 10.0, 0.0, 1.0, 0.0, 10.0),      # west slab
            (20.0, 100.0, 0.0, 1.0, 0.0, 10.0),    # east slab
            (22.0, 26.0, 3.0, 5.0, 2.0, 8.0),      # hovering clutter
        )
        placement = make_placement(
            "one.obj", PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
        )
        geometry_by_resource = {"one.obj": geometry}
        pool = ObjectPool(
            placements=[placement],
            resolved_paths={"one.obj": "/nonexistent/one.obj"},
        )
        structures = partition_structures(
            pool,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(structures) == 3
        elevated_indices = [
            index
            for index, structure in enumerate(structures)
            if not structure.is_ground_touching
        ]
        assert len(elevated_indices) == 1
        elevated_index = elevated_indices[0]
        east_slab_index = max(
            (
                index
                for index, structure in enumerate(structures)
                if structure.is_ground_touching
            ),
            key=lambda index: structures[index].surface_area_square_metres,
        )
        west_slab_index = next(
            index
            for index, structure in enumerate(structures)
            if structure.is_ground_touching and index != east_slab_index
        )

        decision = structure_deltas(
            pool, geometry_by_resource, structures, plane_sampler
        )
        updated_elevated = decision.structures[elevated_index]
        assert updated_elevated.skip_reason is None
        assert (
            updated_elevated.inherited_from_structure_index
            == east_slab_index
        )
        assert updated_elevated.ground_span_metres == 0.0

        # The inherited delta equals the supporter's delta (same single
        # anchor here), and differs from the west slab's on the slope.
        deltas = decision.delta_by_resource_and_vertex["one.obj"]
        elevated_vertex = structures[elevated_index].triangles_by_resource[
            "one.obj"
        ][0][0]
        east_vertex = structures[east_slab_index].triangles_by_resource[
            "one.obj"
        ][0][0]
        west_vertex = structures[west_slab_index].triangles_by_resource[
            "one.obj"
        ][0][0]
        assert deltas[elevated_vertex] == pytest.approx(
            deltas[east_vertex], abs=1e-9
        )
        assert abs(deltas[elevated_vertex] - deltas[west_vertex]) > 1.0
        # Ground structures inherit nothing.
        assert (
            decision.structures[east_slab_index]
            .inherited_from_structure_index
            is None
        )

    def test_nearest_by_centroid_fallback(self, plane_sampler):
        # Two ground slabs (centroids at east 5 and 105) and clutter
        # hovering at 60..70 (centroid 65) — no box contains it, so it
        # inherits the NEAREST ground structure: the east slab.
        geometry = compound_geometry(
            (0.0, 10.0, 0.0, 1.0, 0.0, 10.0),        # west slab
            (100.0, 110.0, 0.0, 1.0, 0.0, 10.0),     # east slab
            (60.0, 70.0, 3.0, 5.0, 0.0, 10.0),       # hovering clutter
        )
        placement = make_placement(
            "one.obj", PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
        )
        geometry_by_resource = {"one.obj": geometry}
        pool = ObjectPool(
            placements=[placement],
            resolved_paths={"one.obj": "/nonexistent/one.obj"},
        )
        structures = partition_structures(
            pool,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(structures) == 3
        elevated_index = next(
            index
            for index, structure in enumerate(structures)
            if not structure.is_ground_touching
        )
        east_slab_index = max(
            (
                index
                for index, structure in enumerate(structures)
                if structure.is_ground_touching
            ),
            key=lambda index: structures[index].centroid_longitude,
        )
        decision = structure_deltas(
            pool, geometry_by_resource, structures, plane_sampler
        )
        updated_elevated = decision.structures[elevated_index]
        assert updated_elevated.skip_reason is None
        assert (
            updated_elevated.inherited_from_structure_index
            == east_slab_index
        )


# ── skip-and-report (invariant I-13) ──────────────────────────────────


class TestOutsideMeshSkips:
    def test_structure_centroid_outside_mesh_is_skipped(
        self, plane_sampler
    ):
        # The anchor is on the mesh but the geometry sits ~500 m east of
        # it — past the mesh edge.
        geometry = compound_geometry((495.0, 505.0, 0.0, 5.0, 0.0, 10.0))
        placement = make_placement(
            "walker.obj", PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
        )
        geometry_by_resource = {"walker.obj": geometry}
        pool = ObjectPool(
            placements=[placement],
            resolved_paths={"walker.obj": "/nonexistent/walker.obj"},
        )
        structures = partition_structures(
            pool,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(structures) == 1
        decision = structure_deltas(
            pool, geometry_by_resource, structures, plane_sampler
        )
        assert decision.structures[0].skip_reason is not None
        assert "outside the built mesh" in decision.structures[0].skip_reason
        assert "walker.obj" not in decision.delta_by_resource_and_vertex
        # The anchor itself was fine, so this is a structure-level skip:
        # no object-level skipped entry.
        assert decision.skipped == []
        assert "walker.obj" in decision.anchor_ground_by_resource

    def test_anchor_outside_mesh_skips_every_structure_of_that_object(
        self, plane_sampler
    ):
        # The anchor is NORTH of the mesh; the geometry hangs 500 m
        # south of it, back inside the mesh.  The structures are
        # sampleable — but the object's y = 0 plane is not, so every
        # structure touching the object is skipped (invariant I-13).
        anchor_latitude = 50.006
        geometry = compound_geometry((0.0, 10.0, 0.0, 5.0, 495.0, 505.0))
        placement = make_placement(
            "orphan.obj", anchor_latitude, PLANE_ANCHOR_LONGITUDE
        )
        geometry_by_resource = {"orphan.obj": geometry}
        pool = ObjectPool(
            placements=[placement],
            resolved_paths={"orphan.obj": "/nonexistent/orphan.obj"},
        )
        structures = partition_structures(
            pool,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(structures) == 1
        # Sanity: the structure centroid IS on the mesh.
        assert (
            plane_sampler.elevation_at_or_none(
                structures[0].centroid_latitude,
                structures[0].centroid_longitude,
            )
            is not None
        )
        decision = structure_deltas(
            pool, geometry_by_resource, structures, plane_sampler
        )
        assert len(decision.skipped) == 1
        skipped_resource, skipped_reason = decision.skipped[0]
        assert skipped_resource == "orphan.obj"
        assert "anchor" in skipped_reason
        assert decision.structures[0].skip_reason is not None
        assert decision.delta_by_resource_and_vertex == {}
        assert "orphan.obj" not in decision.anchor_ground_by_resource

    def test_mixed_draped_solid_object_is_excluded_and_reported(
        self, plane_sampler
    ):
        vertices, triangles = box_vertices_and_triangles(
            0.0, 10.0, 0.0, 5.0, 0.0, 10.0
        )
        mixed_geometry = obj8_reader.ObjectGeometry(
            vertices=vertices,
            solid_triangles=triangles,
            draped_triangles=[triangles[0]],  # shares vertices with solid
            positional_commands=[],
            animation_block_count=0,
            level_of_detail_count=0,
            vertex_line_indices=list(range(len(vertices))),
        )
        assert mixed_geometry.has_mixed_draped_solid_vertices
        placement = make_placement(
            "mixed.obj", PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
        )
        geometry_by_resource = {"mixed.obj": mixed_geometry}
        pool = ObjectPool(
            placements=[placement],
            resolved_paths={"mixed.obj": "/nonexistent/mixed.obj"},
        )
        structures = partition_structures(
            pool,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert structures == []
        decision = structure_deltas(
            pool, geometry_by_resource, structures, plane_sampler
        )
        assert len(decision.skipped) == 1
        assert decision.skipped[0][0] == "mixed.obj"
        assert "I-9" in decision.skipped[0][1]


# ── amendment A3: bake-and-flag, and the arithmetic do-not-bake ───────


class TestAmendmentA3:
    def test_large_ground_span_is_baked_and_flagged(self, plane_sampler):
        # One structure of three parts chained by sub-epsilon gaps: two
        # ground slabs 50 m apart (centroids at east 5 and 55) and an
        # elevated beam bridging them.  On the plane's slope the ground
        # span far exceeds DSF_OBJECT_PAD_FLAG_SPAN_M (2 m) — the
        # structure is STILL baked, and flagged needs_pad.
        geometry = compound_geometry(
            (0.0, 10.0, 0.0, 1.0, 0.0, 10.0),        # west ground slab
            (10.1, 49.9, 0.6, 1.0, 0.0, 10.0),       # elevated beam
            (50.0, 60.0, 0.0, 1.0, 0.0, 10.0),       # east ground slab
        )
        placement = make_placement(
            "span.obj", PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
        )
        geometry_by_resource = {"span.obj": geometry}
        pool = ObjectPool(
            placements=[placement],
            resolved_paths={"span.obj": "/nonexistent/span.obj"},
        )
        structures = partition_structures(
            pool,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(structures) == 1
        decision = structure_deltas(
            pool, geometry_by_resource, structures, plane_sampler
        )
        updated = decision.structures[0]
        assert updated.skip_reason is None          # baked, not refused
        assert updated.needs_pad
        # Slab centroids sit 50 m apart east; expected span is the
        # plane's elevation change over those 50 metres.
        metres_per_degree = metres_per_degree_longitude_at(
            PLANE_ANCHOR_LATITUDE
        )
        expected_span = PLANE_ELEVATION_PER_DEGREE_LONGITUDE * (
            50.0 / metres_per_degree
        )
        assert updated.ground_span_metres == pytest.approx(
            expected_span, rel=0.05
        )
        assert updated.ground_span_metres > 2.0
        # Every vertex still got a delta (24 vertices: three boxes).
        assert set(
            decision.delta_by_resource_and_vertex["span.obj"]
        ) == set(range(24))

    def test_correction_worse_than_uncorrected_is_skipped_with_arithmetic(
        self, pit_sampler
    ):
        # The pathological case: a symmetric structure whose ground
        # parts sit at the anchor's own elevation (uncorrected residual
        # ~0) while its centroid hangs over the pit centre (correction
        # would push both parts ~2.8 m down).  Arithmetic, not a
        # threshold, decides the skip.
        metres_per_degree = metres_per_degree_longitude_at(
            PIT_CENTRE_LATITUDE
        )
        # Anchor at the WEST slab's centroid: 20 m west of the pit
        # centre.
        anchor_longitude = PIT_CENTRE_LONGITUDE - 20.0 / metres_per_degree
        geometry = compound_geometry(
            (-5.0, 5.0, 0.0, 1.0, -5.0, 5.0),        # west slab (at anchor)
            (5.1, 34.9, 0.6, 1.0, -5.0, 5.0),        # elevated beam
            (35.0, 45.0, 0.0, 1.0, -5.0, 5.0),       # east slab (mirrored)
        )
        placement = make_placement(
            "pathological.obj", PIT_CENTRE_LATITUDE, anchor_longitude
        )
        geometry_by_resource = {"pathological.obj": geometry}
        pool = ObjectPool(
            placements=[placement],
            resolved_paths={
                "pathological.obj": "/nonexistent/pathological.obj"
            },
        )
        structures = partition_structures(
            pool,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(structures) == 1
        # Sanity: the structure centroid sits at the pit centre, well
        # below the terrain under the slabs.
        centroid_ground = pit_sampler.elevation_at(
            structures[0].centroid_latitude,
            structures[0].centroid_longitude,
        )
        anchor_ground = pit_sampler.elevation_at(
            placement.latitude, placement.longitude
        )
        assert anchor_ground - centroid_ground > 2.0

        decision = structure_deltas(
            pool, geometry_by_resource, structures, pit_sampler
        )
        updated = decision.structures[0]
        assert updated.skip_reason is not None
        assert "amendment A3" in updated.skip_reason
        assert "corrected" in updated.skip_reason
        assert "uncorrected" in updated.skip_reason
        # Both numbers are in the reason, and the corrected one is the
        # larger.
        assert (
            "pathological.obj" not in decision.delta_by_resource_and_vertex
        )
        # This is a structure-level arithmetic skip, not an object-level
        # refusal.
        assert decision.skipped == []


# ── integration smoke: the real KCLT eight-bake pool ──────────────────

KCLT_PACK_ROOT = (
    "/Users/noah/X-Plane 12/Custom Scenery/"
    "Nimbus Simulation - KCLT V1.4 - Charlotte XP12"
)
KCLT_MESH_PATH = (
    "/Users/noah/X-Plane 12/Custom Scenery/zOrtho4XP_+35-081/"
    "Data+35-081.mesh"
)
KCLT_DSF_PATH = os.path.join(
    KCLT_PACK_ROOT, "Earth nav data", "+30-090", "+35-081.dsf"
)
KCLT_RESOURCES = [
    f"Terminals/Hangar/Charlotte_Airport_{number:03d}_ALB.obj"
    for number in range(1, 9)
]


def _kclt_backup_path(resource_path):
    return os.path.join(
        KCLT_PACK_ROOT, *resource_path.split("/")
    ) + ".anchor_bak"


def _kclt_pool_available():
    if not os.path.isfile(KCLT_MESH_PATH):
        return False
    if not os.path.isfile(KCLT_DSF_PATH):
        return False
    return all(
        os.path.isfile(_kclt_backup_path(resource))
        for resource in KCLT_RESOURCES
    )


@pytest.mark.skipif(
    not _kclt_pool_available(),
    reason="KCLT pack (with .anchor_bak originals) or built mesh absent",
)
def test_kclt_eight_bake_pool_end_to_end():
    """The real eight co-anchored KCLT bakes, read-only (geometry from
    the ``.anchor_bak`` originals): one pool, ~220 structures at the
    epsilon-0.25 knee, equal per-structure deltas across all eight
    resources (shared anchor), zero skips."""
    from auto_patch import dsf_reader

    dsf_text_lines = dsf_reader._load_dsf_text(KCLT_DSF_PATH)
    if not dsf_text_lines:
        pytest.skip("DSF text unavailable (DSFTool missing?)")
    wanted = set(KCLT_RESOURCES)
    placements = obj8_reader.read_dsf_object_placements(
        dsf_text_lines,
        accept_resource=lambda resource: resource in wanted,
    )
    assert len(placements) == 8

    geometry_by_resource = {
        resource: obj8_reader.load_object_file(_kclt_backup_path(resource))
        for resource in KCLT_RESOURCES
    }
    resolved_paths = {
        resource: _kclt_backup_path(resource)
        for resource in KCLT_RESOURCES
    }

    pools = discover_object_pools(
        placements,
        resolved_paths,
        geometry_by_resource,
        epsilon_metres=0.25,
    )
    assert len(pools) == 1
    assert len(pools[0].placements) == 8

    structures = partition_structures(
        pools[0], geometry_by_resource, epsilon_metres=0.25
    )
    # The measured epsilon-0.25 knee (partition document section 2.3).
    assert 215 <= len(structures) <= 225

    anchor = placements[0]
    reach_metres = max(
        geometry.solid_reach_metres()
        for geometry in geometry_by_resource.values()
    )
    margin_degrees = (reach_metres + 200.0) / METRES_PER_DEGREE_LATITUDE
    sampler = MeshElevationSampler(
        KCLT_MESH_PATH,
        (
            anchor.longitude - margin_degrees,
            anchor.latitude - margin_degrees,
            anchor.longitude + margin_degrees,
            anchor.latitude + margin_degrees,
        ),
        margin_degrees=0.0,
    )

    decision = structure_deltas(
        pools[0], geometry_by_resource, structures, sampler
    )
    assert decision.skipped == []
    assert all(
        structure.skip_reason is None for structure in decision.structures
    )
    # All eight bakes share one bit-identical anchor, so the anchor
    # grounds are identical...
    assert len(set(decision.anchor_ground_by_resource.values())) == 1
    # ...and therefore each structure's deltas are EQUAL across its
    # resources (the shared-anchor special case of invariant I-3).
    for structure in decision.structures:
        per_resource_deltas = []
        for resource, triangles in structure.triangles_by_resource.items():
            first_vertex_index = triangles[0][0]
            per_resource_deltas.append(
                decision.delta_by_resource_and_vertex[resource][
                    first_vertex_index
                ]
            )
        assert max(per_resource_deltas) - min(per_resource_deltas) < 1e-6

    all_deltas = [
        delta
        for deltas_by_vertex in (
            decision.delta_by_resource_and_vertex.values()
        )
        for delta in deltas_by_vertex.values()
    ]
    assert all_deltas
    needing_pad = sum(
        1 for structure in decision.structures if structure.needs_pad
    )
    inherited = sum(
        1
        for structure in decision.structures
        if structure.inherited_from_structure_index is not None
    )
    print(
        f"\nKCLT eight-bake pool: {len(structures)} structures, "
        f"delta range {min(all_deltas):+.2f}..{max(all_deltas):+.2f} m, "
        f"{needing_pad} needing a pad, {inherited} inherited, "
        f"{len(decision.skipped)} skipped"
    )
