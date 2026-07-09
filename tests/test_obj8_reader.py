"""Workstream W2 acceptance tests for ``auto_patch.obj8_reader``.

Covers the parsing gotchas of ``docs/dsf_object_anchor_plan.md`` section
8.8 (invariant I-17: whitespace-split, never ``startswith``), the
draped/solid separation (invariant I-9), the positional-command column
table (invariant I-10), the rotation-sign convention of
``O4_Vector_Map.keep_obj8`` (synthetic hand-computed values plus the
real-pack golden hangar), and the ``lonlat_to_local_offset`` inverse.
"""

from __future__ import annotations

import math
import os
import random

import pytest

from auto_patch import obj8_partition, obj8_reader
from auto_patch.obj8_reader import (
    METRES_PER_DEGREE_LATITUDE,
    POSITIONAL_COMMAND_COORDINATE_TOKEN_INDICES,
    load_object_file,
    local_offset_to_lonlat,
    lonlat_to_local_offset,
    read_dsf_object_placements,
    resolve_object_resource,
)

FIXTURE_DIRECTORY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "obj8"
)

KCLT_PACK_ROOT = (
    "/Users/noah/X-Plane 12/Custom Scenery/"
    "Nimbus Simulation - KCLT V1.4 - Charlotte XP12"
)

# The eight co-anchored KCLT bakes share this placement exactly
# (plan section 3).
KCLT_BAKE_ANCHOR_LATITUDE = 35.207360571
KCLT_BAKE_ANCHOR_LONGITUDE = -80.935041390
KCLT_BAKE_ANCHOR_HEADING_DEGREES = 86.095674

# Independently measured position of the Charlotte_Airport_007_ALB.obj
# hangar structure (plan section 9): the correct rotation sign lands a
# structure centroid within ~39 m of it; the wrong sign is 1021 m out.
GOLDEN_HANGAR_LATITUDE = 35.216591
GOLDEN_HANGAR_LONGITUDE = -80.929272


def fixture_path(name: str) -> str:
    return os.path.join(FIXTURE_DIRECTORY, name)


# ---------------------------------------------------------------------------
# parsing — invariant I-17 (tab-separated exports) and structure counts
# ---------------------------------------------------------------------------

def test_tab_separated_vertex_and_index_lines_parse():
    """XPlane2Blender exports separate ``VT``/``IDX`` tokens with tabs;
    ``line.startswith("VT ")`` silently dropped 232 of 334 KCLT
    definitions.  The fixture is tab-delimited throughout."""
    geometry = load_object_file(fixture_path("two_boxes_tab_separated.obj"))
    assert len(geometry.vertices) == 16
    assert len(geometry.solid_triangles) == 24
    assert geometry.draped_triangles == []
    assert geometry.has_solid_geometry
    # Both 10 m boxes present: reach extends to the far box corner.
    expected_reach = math.hypot(23.0, 10.0)
    assert geometry.solid_reach_metres() == pytest.approx(expected_reach)


def test_vertex_line_indices_parallel_to_vertices():
    geometry = load_object_file(fixture_path("two_boxes_tab_separated.obj"))
    assert len(geometry.vertex_line_indices) == len(geometry.vertices)
    with open(fixture_path("two_boxes_tab_separated.obj")) as handle:
        lines = handle.read().splitlines()
    for vertex, line_index in zip(
        geometry.vertices, geometry.vertex_line_indices
    ):
        tokens = lines[line_index].split()
        assert tokens[0] == "VT"
        assert (
            float(tokens[1]),
            float(tokens[2]),
            float(tokens[3]),
        ) == vertex


def test_light_only_object_has_no_solid_geometry():
    geometry = load_object_file(fixture_path("light_only.obj"))
    assert not geometry.has_solid_geometry
    assert geometry.solid_reach_metres() == 0.0
    assert len(geometry.positional_commands) == 1


# ---------------------------------------------------------------------------
# draped/solid separation — invariant I-9
# ---------------------------------------------------------------------------

def test_draped_triangles_excluded_from_solid():
    geometry = load_object_file(fixture_path("draped_decal.obj"))
    assert geometry.solid_triangles == []
    assert len(geometry.draped_triangles) == 2
    assert not geometry.has_solid_geometry
    assert not geometry.has_mixed_draped_solid_vertices


def test_mixed_draped_solid_vertex_detection():
    """A vertex used by both a draped and a solid triangle makes the
    object un-correctable (offsetting the solid use tears the draped use
    off the terrain) — the reader must expose that."""
    geometry = load_object_file(fixture_path("mixed_draped_solid.obj"))
    assert len(geometry.solid_triangles) == 2
    assert len(geometry.draped_triangles) == 2
    assert geometry.has_mixed_draped_solid_vertices


def test_pure_solid_object_is_not_mixed():
    geometry = load_object_file(fixture_path("two_boxes_tab_separated.obj"))
    assert not geometry.has_mixed_draped_solid_vertices


# ---------------------------------------------------------------------------
# animation and level-of-detail counting — plan section 8.7
# ---------------------------------------------------------------------------

def test_animation_block_count():
    geometry = load_object_file(fixture_path("animated_door.obj"))
    assert geometry.animation_block_count == 1
    assert geometry.level_of_detail_count == 0


def test_level_of_detail_count():
    geometry = load_object_file(fixture_path("two_lod_hangar.obj"))
    assert geometry.level_of_detail_count == 2
    assert geometry.animation_block_count == 0


# ---------------------------------------------------------------------------
# positional commands — invariant I-10
# ---------------------------------------------------------------------------

def test_every_positional_command_keyword_round_trips_y():
    """For every keyword in the column table, re-reading the y token at
    ``y_token_index`` from the source line must reproduce the parsed y —
    this is exactly the operation the rebake writer performs."""
    path = fixture_path("positional_commands_all.obj")
    geometry = load_object_file(path)
    with open(path) as handle:
        lines = handle.read().splitlines()

    keywords_seen = {command.keyword for command in geometry.positional_commands}
    assert keywords_seen == set(POSITIONAL_COMMAND_COORDINATE_TOKEN_INDICES)

    for command in geometry.positional_commands:
        tokens = lines[command.line_index].split()
        assert tokens[0] == command.keyword
        assert float(tokens[command.y_token_index]) == command.y
        # x and z sit immediately around y in every keyword's layout.
        x_token, y_token, z_token = (
            POSITIONAL_COMMAND_COORDINATE_TOKEN_INDICES[command.keyword]
        )
        assert command.y_token_index == y_token
        assert float(tokens[x_token]) == command.x
        assert float(tokens[z_token]) == command.z


def test_lit_mast_light_position():
    geometry = load_object_file(fixture_path("lit_mast.obj"))
    assert len(geometry.positional_commands) == 1
    command = geometry.positional_commands[0]
    assert command.keyword == "LIGHT_NAMED"
    assert command.y == pytest.approx(12.0)


# ---------------------------------------------------------------------------
# rotation convention — the sign that is worth more than every other test
# ---------------------------------------------------------------------------

def test_rotation_sign_synthetic_hand_computed():
    """The keep_obj8 convention: east = x*cos(heading) - z*sin(heading),
    south = x*sin(heading) + z*cos(heading).  Hand-computed cases."""
    anchor_latitude, anchor_longitude = 40.0, -75.0
    metres_per_degree_longitude = METRES_PER_DEGREE_LATITUDE * math.cos(
        math.radians(anchor_latitude)
    )

    # Heading 0: local +x is due east, local +z is due south.
    latitude, longitude = local_offset_to_lonlat(
        anchor_latitude, anchor_longitude, 0.0, 100.0, 0.0
    )
    assert latitude == pytest.approx(anchor_latitude)
    assert longitude == pytest.approx(
        anchor_longitude + 100.0 / metres_per_degree_longitude
    )
    latitude, longitude = local_offset_to_lonlat(
        anchor_latitude, anchor_longitude, 0.0, 0.0, 100.0
    )
    assert latitude == pytest.approx(
        anchor_latitude - 100.0 / METRES_PER_DEGREE_LATITUDE
    )
    assert longitude == pytest.approx(anchor_longitude)

    # Heading 90 (object rotated clockwise from north): local +x now
    # points due south (east = x*cos90 = 0, south = x*sin90 = x), and
    # local +z points due west (east = -z*sin90 = -z).
    latitude, longitude = local_offset_to_lonlat(
        anchor_latitude, anchor_longitude, 90.0, 100.0, 0.0
    )
    assert latitude == pytest.approx(
        anchor_latitude - 100.0 / METRES_PER_DEGREE_LATITUDE
    )
    assert longitude == pytest.approx(anchor_longitude)
    latitude, longitude = local_offset_to_lonlat(
        anchor_latitude, anchor_longitude, 90.0, 0.0, 100.0
    )
    assert latitude == pytest.approx(anchor_latitude)
    assert longitude == pytest.approx(
        anchor_longitude - 100.0 / metres_per_degree_longitude
    )

    # Heading 30, x = 10, z = 20 — fully hand-computed:
    # east  = 10*cos30 - 20*sin30 = 8.66025... - 10 = -1.339746
    # south = 10*sin30 + 20*cos30 = 5 + 17.32051 = 22.320508
    latitude, longitude = local_offset_to_lonlat(
        anchor_latitude, anchor_longitude, 30.0, 10.0, 20.0
    )
    expected_east = 10.0 * math.cos(math.radians(30.0)) - 20.0 * math.sin(
        math.radians(30.0)
    )
    expected_south = 10.0 * math.sin(math.radians(30.0)) + 20.0 * math.cos(
        math.radians(30.0)
    )
    assert expected_east == pytest.approx(-1.3397459621, abs=1e-9)
    assert expected_south == pytest.approx(22.3205080757, abs=1e-9)
    assert latitude == pytest.approx(
        anchor_latitude - expected_south / METRES_PER_DEGREE_LATITUDE
    )
    assert longitude == pytest.approx(
        anchor_longitude + expected_east / metres_per_degree_longitude
    )


@pytest.mark.skipif(
    not os.path.isdir(KCLT_PACK_ROOT),
    reason="Nimbus KCLT pack not installed",
)
def test_rotation_sign_real_pack_golden_hangar():
    """``Charlotte_Airport_007_ALB.obj``'s hangar structure lands at
    35.216591, -80.929272 with the correct rotation sign; the wrong sign
    puts it 1021 metres away.  Reads the ``.anchor_bak`` original when
    the pack is live-baked (only y differs, but stay truthful anyway)."""
    resource = os.path.join(
        KCLT_PACK_ROOT, "Terminals", "Hangar", "Charlotte_Airport_007_ALB.obj"
    )
    backup = resource + ".anchor_bak"
    geometry = load_object_file(backup if os.path.isfile(backup) else resource)
    parts = obj8_partition.weld_parts(
        geometry.vertices, geometry.solid_triangles
    )
    edges = obj8_partition.contact_graph(geometry.vertices, parts, 0.25)
    structures = obj8_partition.connected_structures(len(parts), edges)

    def centroid_distance_metres(structure_part_indices) -> float:
        triangles = [
            triangle
            for part_index in structure_part_indices
            for triangle in parts[part_index]
        ]
        _, centroid_x, centroid_z = obj8_reader.area_weighted_centroid(
            geometry.vertices, triangles
        )
        latitude, longitude = local_offset_to_lonlat(
            KCLT_BAKE_ANCHOR_LATITUDE,
            KCLT_BAKE_ANCHOR_LONGITUDE,
            KCLT_BAKE_ANCHOR_HEADING_DEGREES,
            centroid_x,
            centroid_z,
        )
        north = (latitude - GOLDEN_HANGAR_LATITUDE) * METRES_PER_DEGREE_LATITUDE
        east = (
            (longitude - GOLDEN_HANGAR_LONGITUDE)
            * METRES_PER_DEGREE_LATITUDE
            * math.cos(math.radians(GOLDEN_HANGAR_LATITUDE))
        )
        return math.hypot(north, east)

    nearest = min(centroid_distance_metres(members) for members in structures)
    # 39 m measured with the correct sign; 1021 m with the wrong one.
    assert nearest < 100.0, (
        f"nearest structure centroid {nearest:.0f} m from the golden hangar "
        f"— rotation sign is wrong if this is ~1000 m"
    )


# ---------------------------------------------------------------------------
# lonlat_to_local_offset — the exact inverse
# ---------------------------------------------------------------------------

def test_local_offset_round_trips_over_random_headings():
    generator = random.Random(20260708)
    for _ in range(200):
        anchor_latitude = generator.uniform(-70.0, 70.0)
        anchor_longitude = generator.uniform(-179.0, 179.0)
        heading_degrees = generator.uniform(0.0, 360.0)
        local_x = generator.uniform(-2000.0, 2000.0)
        local_z = generator.uniform(-2000.0, 2000.0)
        latitude, longitude = local_offset_to_lonlat(
            anchor_latitude,
            anchor_longitude,
            heading_degrees,
            local_x,
            local_z,
        )
        round_trip_x, round_trip_z = lonlat_to_local_offset(
            anchor_latitude,
            anchor_longitude,
            heading_degrees,
            latitude,
            longitude,
        )
        assert abs(round_trip_x - local_x) < 1e-6
        assert abs(round_trip_z - local_z) < 1e-6


# ---------------------------------------------------------------------------
# DSF placement walking — harness pattern (a), in-memory line list
# ---------------------------------------------------------------------------

SYNTHETIC_DSF_TEXT_LINES = [
    "PROPERTY sim/west -81\n",
    "OBJECT_DEF Terminals/Hangar/Charlotte_Airport_007_ALB.obj\n",
    "OBJECT_DEF otros/cone_short.obj\n",
    "OBJECT_DEF lib/airport/Ramp_Equipment/Stair_Truck.obj\n",
    "OBJECT 0 -80.935041390 35.207360571 86.095674\n",   # kept
    "OBJECT 1 -80.940000000 35.210000000 0.000000\n",    # kept
    "OBJECT 1 -80.941000000 35.211000000 10.000000\n",   # kept (2nd placement)
    "OBJECT_MSL 0 -80.935041390 35.207360571 220.0 86.095674\n",  # skipped
    # Amendment A18: OBJECT_AGL is terrain-relative at the ANCHOR only —
    # it carries the distant-anchor disease with a constant offset, so
    # the reader ACCEPTS it (heading in the fifth column).
    "OBJECT_AGL 0 -80.935041390 35.207360571 5.0 86.095674\n",    # kept
    "OBJECT 99 -80.0 35.0 0.0\n",                        # index out of range
]


def test_read_dsf_object_placements_accepts_agl_and_skips_msl():
    placements = read_dsf_object_placements(SYNTHETIC_DSF_TEXT_LINES)
    assert len(placements) == 4
    first = placements[0]
    assert first.definition_index == 0
    assert first.resource_path == (
        "Terminals/Hangar/Charlotte_Airport_007_ALB.obj"
    )
    assert first.longitude == pytest.approx(-80.935041390)
    assert first.latitude == pytest.approx(35.207360571)
    assert first.heading_degrees == pytest.approx(86.095674)
    assert first.above_ground_level_metres == 0.0
    assert [p.resource_path for p in placements[1:3]] == [
        "otros/cone_short.obj",
        "otros/cone_short.obj",
    ]
    above_ground = placements[3]
    assert above_ground.above_ground_level_metres == pytest.approx(5.0)
    assert above_ground.heading_degrees == pytest.approx(86.095674)
    # OBJECT_MSL (absolute elevation) remains excluded.
    assert all(
        placement.resource_path
        != "lib/airport/Ramp_Equipment/Stair_Truck.obj"
        for placement in placements
    )


def test_read_dsf_object_placements_accept_resource_filter():
    placements = read_dsf_object_placements(
        SYNTHETIC_DSF_TEXT_LINES,
        accept_resource=lambda resource: resource.endswith("_ALB.obj"),
    )
    # The plain OBJECT row and the amendment-A18 AGL row of the same
    # definition both pass the filter.
    assert len(placements) == 2
    assert {placement.definition_index for placement in placements} == {0}
    assert sorted(
        placement.above_ground_level_metres for placement in placements
    ) == [0.0, 5.0]


# ---------------------------------------------------------------------------
# resource resolution — pack-relative wins over library.txt
# ---------------------------------------------------------------------------

def test_resolve_object_resource_pack_relative_wins(tmp_path):
    pack_root = tmp_path / "pack"
    resource_directory = pack_root / "Terminals"
    resource_directory.mkdir(parents=True)
    resource_file = resource_directory / "hangar.obj"
    resource_file.write_text("A\n800\nOBJ\n")
    resolved = resolve_object_resource(
        "Terminals/hangar.obj", str(pack_root), None
    )
    assert resolved == str(resource_file)


def test_resolve_object_resource_missing_everywhere(tmp_path):
    assert (
        resolve_object_resource("Terminals/hangar.obj", str(tmp_path), None)
        is None
    )
