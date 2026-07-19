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
    detect_foot_clusters,
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


# ── connector split (2026-07-18, EGGW floating buildings) ─────────────


class TestConnectorSplit:
    def _structures_for(self, *boxes):
        geometry = compound_geometry(*boxes)
        placement = make_placement(
            "chain.obj", PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
        )
        pool = ObjectPool(
            placements=[placement],
            resolved_paths={"chain.obj": "/nonexistent/chain.obj"},
        )
        return partition_structures(
            pool,
            {"chain.obj": geometry},
            epsilon_metres=CONTACT_EPSILON_METRES,
        )

    def test_kilometre_fence_chain_splits_into_buildings(self):
        # Two buildings a kilometre apart chained by one thin fence: the
        # EGGW class (fences/barriers glued 40 structures into 2.6-3.1 km
        # components; the span gate then left them ALL floating at their
        # authored y).  The oversized component re-partitions: each
        # building seats alone, the fence becomes its own singleton.
        structures = self._structures_for(
            (0.0, 20.0, 0.0, 8.0, 0.0, 20.0),       # west building
            (1000.0, 1020.0, 0.0, 8.0, 0.0, 20.0),  # east building
            (19.9, 1000.1, 0.0, 1.5, 4.0, 4.4),     # fence joining both
        )
        assert len(structures) == 3

    def test_compact_complex_with_thin_members_stays_whole(self):
        # The same shape under the 800 m threshold is a real building
        # complex (KCLT: terminal concourses with thin load-bearing
        # canopy members measured 100-400 m and must never split).
        structures = self._structures_for(
            (0.0, 20.0, 0.0, 8.0, 0.0, 20.0),
            (100.0, 120.0, 0.0, 8.0, 0.0, 20.0),
            (19.9, 100.1, 0.0, 1.5, 4.0, 4.4),
        )
        assert len(structures) == 1

    def test_internal_trim_reattaches_to_its_building(self):
        # A thin parapet strip touching ONLY its own building must ride
        # with it through an oversized split (a fence elsewhere triggers
        # the split); leaving it out shattered KCLT 220 -> 343.
        structures = self._structures_for(
            (0.0, 20.0, 0.0, 8.0, 0.0, 20.0),        # west building
            (0.0, 30.0, 7.9, 8.4, 0.2, 0.6),         # its parapet strip
            (1000.0, 1020.0, 0.0, 8.0, 0.0, 20.0),   # east building
            (19.9, 1000.1, 0.0, 1.5, 4.0, 4.4),      # true fence
        )
        # west building + parapet reattached = 1, east building = 1,
        # fence singleton = 1.
        assert len(structures) == 3


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
        # Amendment A19: structure-level skips are VISIBLE at the
        # resource level — one aggregated entry naming the resource.
        assert any(
            resource == "walker.obj" and "left unbaked" in reason
            for resource, reason in decision.skipped
        )
        assert "walker.obj" in decision.anchor_ground_by_resource

    def test_partially_skipped_resource_keeps_its_passing_deltas(
        self, plane_sampler
    ):
        # Amendment A21: ONE resource, TWO structures — a box at the
        # anchor (bakes) and a box ~500 m east whose centroid falls off
        # the mesh (skipped).  The resource must NOT land in
        # ``decision.skipped`` (``object_rebake.apply`` refuses every
        # resource listed there): the passing structure's deltas bake,
        # and the skipped structure keeps its ``skip_reason`` for the
        # per-structure report and provenance detail.
        geometry = compound_geometry(
            (0.0, 10.0, 0.0, 5.0, 0.0, 10.0),
            (495.0, 505.0, 0.0, 5.0, 0.0, 10.0),
        )
        placement = make_placement(
            "partial.obj", PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
        )
        geometry_by_resource = {"partial.obj": geometry}
        pool = ObjectPool(
            placements=[placement],
            resolved_paths={"partial.obj": "/nonexistent/partial.obj"},
        )
        structures = partition_structures(
            pool,
            geometry_by_resource,
            epsilon_metres=CONTACT_EPSILON_METRES,
        )
        assert len(structures) == 2
        decision = structure_deltas(
            pool, geometry_by_resource, structures, plane_sampler
        )
        skip_reasons = [
            structure.skip_reason for structure in decision.structures
        ]
        assert sum(1 for reason in skip_reasons if reason) == 1
        assert any(
            reason and "outside the built mesh" in reason
            for reason in skip_reasons
        )
        # The resource still bakes: no resource-level skip entry ...
        assert decision.skipped == []
        # ... and the delta map holds exactly the passing structure's
        # vertices (8 of the 16 — the skipped box carries no delta).
        deltas = decision.delta_by_resource_and_vertex["partial.obj"]
        baked_structure = next(
            structure
            for structure in decision.structures
            if not structure.skip_reason
        )
        baked_vertices = {
            vertex_index
            for triangle in baked_structure.triangles_by_resource[
                "partial.obj"
            ]
            for vertex_index in triangle
        }
        assert set(deltas) == baked_vertices
        assert len(deltas) == 8

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
        # Two entries: the anchor-level skip, and the amendment-A19
        # aggregated structure-skip visibility entry.
        assert len(decision.skipped) == 2
        skipped_resource, skipped_reason = decision.skipped[0]
        assert skipped_resource == "orphan.obj"
        assert all(
            resource == "orphan.obj" for resource, _ in decision.skipped
        )
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
    def test_large_ground_span_is_left_at_authored_elevations(
        self, plane_sampler
    ):
        # One structure of three parts chained by sub-epsilon gaps: two
        # ground slabs 50 m apart (centroids at east 5 and 55) and an
        # elevated beam bridging them.  On the plane's slope the ground
        # span far exceeds DSF_OBJECT_BAKE_MAX_GROUND_SPAN_M (3 m).
        # SUPERSEDED BEHAVIOR (lead ruling 2026-07-17, EGGW mega
        # components): amendment A3's bake-and-flag is replaced by the
        # rigid-seat limit — one rigid offset cannot seat such a span
        # (one end floats past the tolerance wherever the offset lands),
        # so the structure stays at its AUTHORED elevations, still
        # flagged needs_pad (Phase-1 pads carry its buildings).
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
        assert updated.skip_reason is not None
        assert "rigid-seat limit" in updated.skip_reason
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
        # A span-skipped structure emits NO deltas — its file stays at
        # the authored geometry (and the reversion pass un-bakes any
        # stale earlier bake).
        assert not decision.delta_by_resource_and_vertex.get(
            "span.obj"
        )

    def test_pit_centroid_no_longer_tricks_the_seating(self, pit_sampler):
        # Before amendment A19 this was the do-not-bake case: a
        # symmetric structure whose ground parts sit at the anchor's own
        # elevation while its CENTROID hangs over the pit centre — the
        # centroid-sampled seating would have pushed both slabs ~2.8 m
        # down, so the A3 arithmetic skipped the whole structure.  With
        # the seating elevation taken as the MEDIAN of the ground parts'
        # grounds, the pit centroid no longer misleads: the structure
        # BAKES with a near-zero offset and no skip fires.
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
        assert updated.skip_reason is None
        assert "pathological.obj" in decision.delta_by_resource_and_vertex
        # Median seating: the offset lands the slabs on THEIR ground,
        # not the pit's — near zero, since the slab grounds bracket the
        # anchor's own elevation.
        deltas = set(
            decision.delta_by_resource_and_vertex["pathological.obj"].values()
        )
        assert len(deltas) == 1
        assert abs(next(iter(deltas))) < 1.0
        assert decision.skipped == []


# ── multi-ground-cluster (foot) re-anchor ─────────────────────────────
#
# Project memory kbna-gantry-pond-multi-foot-objects: the KBNA
# water-treatment stairs carry author-BAKED vertical offsets (lowest
# solid vertex at local y = +6.5 m) and TWO ground-contact feet whose
# authored bases differ by 1.17 m.  The absolute elevated test called
# them rooftop clutter and every seating path skipped them.


def two_foot_gantry_geometry(
    foot_a_base_y=6.5, foot_b_base_y=7.7, deck_y=9.2, span_axis="south"
):
    """Three welded quads: two vertical FEET 38..40 m apart joined by a
    DECK, mimicking the KBNA stair shape.  ``span_axis`` 'south' puts
    the feet at the same plane-mesh ground (the plane's elevation is
    constant in latitude); 'east' puts them across the plane's slope."""

    def point(along, y, across):
        if span_axis == "south":
            return (across, y, along)
        return (along, y, across)

    vertices = [
        # foot A (quad in the vertical plane, along 0..2)
        point(0.0, foot_a_base_y, 0.0),
        point(2.0, foot_a_base_y, 0.0),
        point(2.0, deck_y, 0.0),
        point(0.0, deck_y, 0.0),
        # deck (horizontal quad, along 0..40)
        point(0.0, deck_y, 0.0),
        point(40.0, deck_y, 0.0),
        point(40.0, deck_y, 2.0),
        point(0.0, deck_y, 2.0),
        # foot B (vertical quad, along 38..40)
        point(38.0, foot_b_base_y, 0.0),
        point(40.0, foot_b_base_y, 0.0),
        point(40.0, deck_y, 0.0),
        point(38.0, deck_y, 0.0),
    ]
    triangles = [
        (0, 1, 2), (0, 2, 3),
        (4, 5, 6), (4, 6, 7),
        (8, 9, 10), (8, 10, 11),
    ]
    return make_geometry(vertices, triangles)


def _single_object_decision(geometry, plane_sampler, resource="gantry.obj"):
    placement = make_placement(
        resource, PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
    )
    geometry_by_resource = {resource: geometry}
    pool = ObjectPool(
        placements=[placement],
        resolved_paths={resource: f"/nonexistent/{resource}"},
    )
    structures = partition_structures(
        pool, geometry_by_resource, epsilon_metres=CONTACT_EPSILON_METRES
    )
    assert len(structures) == 1
    decision = structure_deltas(
        pool, geometry_by_resource, structures, plane_sampler
    )
    return structures, decision


class TestFootReanchor:
    def test_two_foot_baked_gantry_seats_across_both_feet(
        self, plane_sampler
    ):
        # Feet along the SOUTH axis: same ground under both, so the
        # seat-target spread equals the authored base difference
        # (1.2 m, inside the contact tolerance) and BOTH feet stay in
        # the fit.  The midpoint seat leaves each foot 0.6 m off — the
        # best any rigid body can do.
        structures, decision = _single_object_decision(
            two_foot_gantry_geometry(span_axis="south"), plane_sampler
        )
        assert not structures[0].is_ground_touching
        updated = decision.structures[0]
        assert updated.skip_reason is None
        assert updated.inherited_from_structure_index is None

        feet = decision.foot_clusters_by_structure_index[0]
        assert len(feet) == 2
        assert sorted(foot.base_y for foot in feet) == pytest.approx(
            [6.5, 7.7], abs=1e-9
        )
        assert all(foot.kept_for_fit for foot in feet)
        assert sorted(foot.residual_metres for foot in feet) == (
            pytest.approx([-0.6, +0.6], abs=1e-3)
        )
        assert decision.foot_pad_requests == []

        # The rigid offset: seat = midpoint of the two per-foot targets
        # ground − base = ground − 7.1, measured from the anchor ground.
        anchor_ground = decision.anchor_ground_by_resource["gantry.obj"]
        deltas = set(
            decision.delta_by_resource_and_vertex["gantry.obj"].values()
        )
        assert len(deltas) == 1
        ground_under_feet = plane_ground(PLANE_ANCHOR_LONGITUDE)
        assert next(iter(deltas)) == pytest.approx(
            ground_under_feet - 7.1 - anchor_ground, abs=1e-3
        )

    def test_slope_drops_low_target_foot_and_requests_a_pad(
        self, plane_sampler
    ):
        # Feet along the EAST axis: ~10.6 m of plane slope between the
        # feet dwarfs the 1.2 m authored base difference, so no rigid
        # offset can seat both.  The body rests on the topmost target
        # (the east foot); the west foot is EXCLUDED from the fit,
        # floats by slope − base difference, and raises a terrain-pad
        # request with the ground elevation that would seat it.
        structures, decision = _single_object_decision(
            two_foot_gantry_geometry(span_axis="east"), plane_sampler
        )
        updated = decision.structures[0]
        assert updated.skip_reason is None

        feet = decision.foot_clusters_by_structure_index[0]
        assert len(feet) == 2
        west_foot, east_foot = feet  # ordered by frame x
        assert west_foot.base_y == pytest.approx(6.5, abs=1e-9)
        assert east_foot.base_y == pytest.approx(7.7, abs=1e-9)
        assert east_foot.kept_for_fit
        assert not west_foot.kept_for_fit
        # The kept foot seats exactly; the dropped foot floats above
        # its ground (never sinks below the bearing foot).
        assert east_foot.residual_metres == pytest.approx(0.0, abs=1e-3)
        assert west_foot.residual_metres > 1.5

        assert len(decision.foot_pad_requests) == 1
        request = decision.foot_pad_requests[0]
        assert request.structure_index == 0
        assert request.resource_path == "gantry.obj"
        assert request.base_y == pytest.approx(6.5, abs=1e-9)
        assert request.residual_metres == pytest.approx(
            west_foot.residual_metres, abs=1e-9
        )
        # Raising the ground under the foot to the recorded target
        # would zero the residual.
        assert request.target_ground_metres == pytest.approx(
            west_foot.ground_metres + west_foot.residual_metres, abs=1e-6
        )
        assert len(request.contact_points_lonlat) == len(
            west_foot.contact_points
        )

    def test_baked_rooftop_clutter_still_inherits(self, plane_sampler):
        # A SEPARATE object file whose whole geometry is baked to sit
        # on another object's roof: its feet lie over the building's
        # bounding box, so inheritance — not foot-anchoring — remains
        # the seating (the building's own delta carries the clutter).
        building_geometry = compound_geometry(
            (0.0, 10.0, 0.0, 20.0, 0.0, 10.0)
        )
        # Hovering just past the contact epsilon so the clutter stays
        # its own structure (in contact it would simply weld into the
        # building and share its delta anyway).
        clutter_geometry = compound_geometry(
            (2.0, 6.0, 20.5, 22.5, 2.0, 6.0)
        )
        placements = [
            make_placement(
                "building.obj", PLANE_ANCHOR_LATITUDE, PLANE_ANCHOR_LONGITUDE
            ),
            make_placement(
                "clutter.obj",
                PLANE_ANCHOR_LATITUDE,
                PLANE_ANCHOR_LONGITUDE,
                definition_index=1,
            ),
        ]
        geometry_by_resource = {
            "building.obj": building_geometry,
            "clutter.obj": clutter_geometry,
        }
        pool = ObjectPool(
            placements=placements,
            resolved_paths={
                resource: f"/nonexistent/{resource}"
                for resource in geometry_by_resource
            },
        )
        structures = partition_structures(
            pool, geometry_by_resource, epsilon_metres=CONTACT_EPSILON_METRES
        )
        assert len(structures) == 2
        building_index = next(
            index
            for index, structure in enumerate(structures)
            if structure.is_ground_touching
        )
        clutter_index = 1 - building_index

        decision = structure_deltas(
            pool, geometry_by_resource, structures, plane_sampler
        )
        updated_clutter = decision.structures[clutter_index]
        assert updated_clutter.skip_reason is None
        assert (
            updated_clutter.inherited_from_structure_index == building_index
        )
        assert decision.foot_clusters_by_structure_index == {}
        assert decision.foot_pad_requests == []
        building_delta = decision.delta_by_resource_and_vertex[
            "building.obj"
        ][0]
        clutter_delta = decision.delta_by_resource_and_vertex[
            "clutter.obj"
        ][0]
        assert clutter_delta == pytest.approx(building_delta, abs=1e-9)

    def test_gate_off_restores_the_elevated_skip(
        self, plane_sampler, monkeypatch
    ):
        from auto_patch import config

        monkeypatch.setattr(config, "DSF_OBJECT_FOOT_ANCHOR", False)
        structures, decision = _single_object_decision(
            two_foot_gantry_geometry(span_axis="south"), plane_sampler
        )
        # Pre-change behaviour: no ground-touching part, no supporter —
        # skipped, and no foot machinery ran.
        assert decision.foot_clusters_by_structure_index == {}
        assert decision.foot_pad_requests == []
        assert decision.structures[0].skip_reason is not None
        assert "no ground-touching part" in decision.structures[0].skip_reason


class TestDetectFootClusters:
    def test_stairs_and_deck_do_not_become_feet(self):
        # Foot A at y 6.5, a staircase climbing towards the deck, the
        # deck underside at 9.0 (its own local minimum mid-span, where
        # the 5 m window cannot see either foot), and foot B at 7.6.
        # Stage 1's LOCAL band cuts the staircase chain right above
        # foot A; stage 3's base gate drops the deck cluster.
        points = [
            (0.0, 6.5, 0.0), (1.0, 6.5, 0.0),               # foot A
            (1.5, 6.8, 0.0), (2.0, 7.1, 0.0),               # stair steps
            (2.5, 7.4, 0.0), (3.0, 7.7, 0.0),
            (39.0, 7.6, 0.0), (40.0, 7.6, 0.0),             # foot B
        ] + [(float(x), 9.0, 0.0) for x in range(8, 35)]    # deck
        resources = ["gantry.obj"] * len(points)
        feet = detect_foot_clusters(
            points,
            resources,
            band_metres=0.5,
            cluster_gap_metres=5.0,
            maximum_base_spread_metres=1.65,
        )
        assert [foot.base_y for foot in feet] == pytest.approx(
            [6.5, 7.6], abs=1e-9
        )
        # Foot A's contact band reaches the first stair step (6.8 is
        # within 0.5 of the cluster base) and no further.
        assert len(feet[0].contact_points) == 3
        assert len(feet[1].contact_points) == 2

    def test_single_low_band_is_one_foot(self):
        points = [
            (0.0, 3.0, 0.0), (2.0, 3.0, 0.0),
            (2.0, 3.0, 2.0), (0.0, 3.0, 2.0),
            (1.0, 8.0, 1.0),
        ]
        feet = detect_foot_clusters(
            points,
            ["tower.obj"] * len(points),
            band_metres=0.5,
            cluster_gap_metres=5.0,
            maximum_base_spread_metres=1.65,
        )
        assert len(feet) == 1
        assert feet[0].base_y == pytest.approx(3.0, abs=1e-9)
        assert feet[0].centroid_x == pytest.approx(1.0, abs=1e-9)
        assert feet[0].centroid_z == pytest.approx(1.0, abs=1e-9)


class TestFootPadRing:
    def test_ring_covers_the_contact_points_with_margin(self):
        from shapely.geometry import Point, Polygon

        from auto_patch.object_footprints import foot_pad_ring

        metres_per_degree = metres_per_degree_longitude_at(
            PLANE_ANCHOR_LATITUDE
        )
        contact_points = [
            (
                PLANE_ANCHOR_LONGITUDE + east / metres_per_degree,
                PLANE_ANCHOR_LATITUDE + north / METRES_PER_DEGREE_LATITUDE,
            )
            for east, north in [
                (0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)
            ]
        ]
        ring = foot_pad_ring(contact_points, margin_metres=2.0)
        assert ring is not None and len(ring) >= 3
        pad = Polygon(ring)
        assert all(
            pad.contains(Point(longitude, latitude))
            for longitude, latitude in contact_points
        )
        # The dilation reaches roughly the margin past the hull: a
        # point ~1.5 m outside the square is still covered, one ~3 m
        # outside is not.
        inside_probe = Point(
            PLANE_ANCHOR_LONGITUDE + 2.5 / metres_per_degree,
            PLANE_ANCHOR_LATITUDE + 0.5 / METRES_PER_DEGREE_LATITUDE,
        )
        outside_probe = Point(
            PLANE_ANCHOR_LONGITUDE + 4.0 / metres_per_degree,
            PLANE_ANCHOR_LATITUDE + 0.5 / METRES_PER_DEGREE_LATITUDE,
        )
        assert pad.contains(inside_probe)
        assert not pad.contains(outside_probe)


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
    # Rigid-seat limit (lead ruling 2026-07-17): a handful of KCLT's
    # chained terminal structures span 3.5-4.2 m of terrain and are now
    # left at authored elevations rather than baked to a median offset
    # with metre-class residuals; every skip must carry exactly that
    # reason, and everything else still bakes.
    span_skipped_structures = [
        structure for structure in decision.structures
        if structure.skip_reason is not None
    ]
    assert all(
        "rigid-seat limit" in structure.skip_reason
        for structure in span_skipped_structures
    )
    assert 0 < len(span_skipped_structures) <= 5
    baked_structures = [
        structure for structure in decision.structures
        if structure.skip_reason is None
    ]
    assert baked_structures
    # All eight bakes share one bit-identical anchor, so the anchor
    # grounds are identical...
    assert len(set(decision.anchor_ground_by_resource.values())) == 1
    # ...and therefore each structure's deltas are EQUAL across its
    # resources (the shared-anchor special case of invariant I-3).
    for structure in baked_structures:
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


# ── CONNECTOR pre-filter metrics (defect 2026-07-17) ──────────────────
# A per-object span + hull-fill test that recognises a bridging connector
# (fence / road / slab that would chain real buildings into one
# field-spanning structure).  Both conditions must hold to flag.

from auto_patch.object_anchor import (  # noqa: E402
    _convex_hull_area_square_metres,
    is_connector_resource,
    resource_connector_metrics,
)


def _make_geometry(vertices, solid_triangles):
    return obj8_reader.ObjectGeometry(
        vertices=list(vertices),
        solid_triangles=list(solid_triangles),
        draped_triangles=[],
        positional_commands=[],
        animation_block_count=0,
        level_of_detail_count=0,
        vertex_line_indices=list(range(len(vertices))),
    )


class TestConvexHullArea:
    def test_square(self):
        # A 10 x 10 square (with an interior point) — hull area 100.
        points = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0),
                  (0.0, 10.0), (5.0, 5.0)]
        assert _convex_hull_area_square_metres(points) == pytest.approx(100.0)

    def test_collinear_is_zero(self):
        points = [(0.0, 0.0), (5.0, 0.0), (10.0, 0.0)]
        assert _convex_hull_area_square_metres(points) == 0.0

    def test_fewer_than_three_is_zero(self):
        assert _convex_hull_area_square_metres([(0.0, 0.0), (1.0, 1.0)]) == 0.0


class TestConnectorMetrics:
    def test_filled_slab_is_not_a_connector(self):
        # A 400 x 400 flat slab: span 400 (> 300) but fill ~1.0 — a large
        # FILLED footprint (a real mega-terminal) is never a connector.
        vertices = [(0.0, 0.0, 0.0), (400.0, 0.0, 0.0),
                    (400.0, 0.0, 400.0), (0.0, 0.0, 400.0)]
        triangles = [(0, 1, 2), (0, 2, 3)]
        geometry = _make_geometry(vertices, triangles)
        metrics = resource_connector_metrics(geometry)
        assert metrics.span_metres == pytest.approx(400.0)
        assert metrics.hull_fill_ratio == pytest.approx(1.0, abs=1e-6)
        is_connector, _ = is_connector_resource(
            geometry, connector_span_metres=300.0, connector_maximum_fill=0.20)
        assert is_connector is False

    def test_long_sparse_fence_is_a_connector(self):
        # A vertical fence tracing a right angle over a 400 x 400 field:
        # its posts span the field but the walls are vertical, so the
        # horizontal footprint area is ~0 while the hull is large — span
        # large AND fill low → connector.
        base = [(0.0, 0.0), (200.0, 0.0), (400.0, 0.0),
                (400.0, 200.0), (400.0, 400.0)]
        vertices = []
        for x, z in base:
            vertices.append((x, 0.0, z))   # base post
            vertices.append((x, 3.0, z))   # top post
        triangles = []
        for post in range(len(base) - 1):
            lower_a = 2 * post
            upper_a = 2 * post + 1
            lower_b = 2 * (post + 1)
            upper_b = 2 * (post + 1) + 1
            triangles.append((lower_a, lower_b, upper_b))
            triangles.append((lower_a, upper_b, upper_a))
        geometry = _make_geometry(vertices, triangles)
        metrics = resource_connector_metrics(geometry)
        assert metrics.span_metres == pytest.approx(400.0)
        assert metrics.hull_fill_ratio < 0.01
        assert metrics.hull_area_square_metres > 50000.0
        is_connector, returned = is_connector_resource(
            geometry, connector_span_metres=300.0, connector_maximum_fill=0.20)
        assert is_connector is True
        assert returned is metrics or returned == metrics

    def test_small_object_is_never_a_connector(self):
        # A 50 x 50 sparse cross: low fill but span 50 < 300 — the span
        # floor protects compact objects (e.g. the ~50 m KBNA gantry).
        vertices = [(0.0, 0.0, 25.0), (50.0, 3.0, 25.0),
                    (25.0, 0.0, 0.0), (25.0, 3.0, 50.0)]
        triangles = [(0, 1, 2), (0, 1, 3)]
        geometry = _make_geometry(vertices, triangles)
        metrics = resource_connector_metrics(geometry)
        assert metrics.span_metres == pytest.approx(50.0)
        is_connector, _ = is_connector_resource(
            geometry, connector_span_metres=300.0, connector_maximum_fill=0.20)
        assert is_connector is False

    def test_no_solid_geometry_is_not_a_connector(self):
        geometry = _make_geometry([(0.0, 0.0, 0.0)], [])
        is_connector, metrics = is_connector_resource(
            geometry, connector_span_metres=300.0, connector_maximum_fill=0.20)
        assert is_connector is False
        assert metrics.span_metres == 0.0
