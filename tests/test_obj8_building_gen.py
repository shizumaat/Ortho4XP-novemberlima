"""Tests for tools/obj8_building_gen: geometry, atlas, and OBJ8 writer.

The load-bearing invariant is winding: X-Plane OBJ8 is clockwise-front,
so for every emitted triangle the right-handed geometric normal must
point AWAY from the stored (outward) vertex normal.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from obj8_building_gen import (  # noqa: E402
    AtlasBand,
    AtlasRect,
    Frame,
    Mesh,
    box,
    canopy,
    extrude_footprint,
    footprint_area,
    gable_roof,
    normalize_footprint,
    polygon_cap,
    shed_roof,
    triangulate_footprint,
    write_obj8,
)

WALL_BAND = AtlasBand("wall", 0.0, 0.5, tile_width_meters=8.0, height_meters=6.0)
ROOF_BAND = AtlasBand("roof", 0.5, 0.8, tile_width_meters=10.0, height_meters=12.0)
CAP_RECT = AtlasRect("cap", 0.0, 0.8, 0.5, 1.0)
FASCIA_RECT = AtlasRect("fascia", 0.5, 0.8, 1.0, 1.0)

SQUARE = [(-5.0, -5.0), (5.0, -5.0), (5.0, 5.0), (-5.0, 5.0)]
L_SHAPE = [(0.0, 0.0), (10.0, 0.0), (10.0, 4.0), (4.0, 4.0), (4.0, 10.0), (0.0, 10.0)]


def geometric_normals_dot_stored(mesh: Mesh) -> list[float]:
    """dot(right-handed geometric normal, stored vertex normal) per triangle."""
    dots = []
    for k in range(0, len(mesh.indices), 3):
        v0, v1, v2 = (mesh.vertices[mesh.indices[k + j]] for j in range(3))
        edge_a = [v1[i] - v0[i] for i in range(3)]
        edge_b = [v2[i] - v0[i] for i in range(3)]
        cross = [
            edge_a[1] * edge_b[2] - edge_a[2] * edge_b[1],
            edge_a[2] * edge_b[0] - edge_a[0] * edge_b[2],
            edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0],
        ]
        stored = [(v0[3 + i] + v1[3 + i] + v2[3 + i]) / 3.0 for i in range(3)]
        dots.append(sum(cross[i] * stored[i] for i in range(3)))
    return dots


def build_sample_building() -> Mesh:
    """A composite exercising every primitive."""
    mesh = Mesh()
    frame = Frame(origin_x=3.0, origin_z=-2.0, bearing_degrees=62.0)
    ring = extrude_footprint(mesh, L_SHAPE, 0.0, 6.0, WALL_BAND)
    polygon_cap(mesh, ring, 6.0, CAP_RECT)
    box(mesh, frame, (0.0, 12.0), (0.0, 8.0), 0.0, 5.0, WALL_BAND, cap_rect=CAP_RECT)
    gable_roof(
        mesh, frame, (0.0, 12.0), (0.0, 8.0), 5.0, 8.0, 1.0,
        ROOF_BAND, FASCIA_RECT, end_wall_rect=CAP_RECT,
    )
    shed_roof(mesh, frame, (14.0, 20.0), (0.0, 6.0), 4.0, 3.0, 0.5, ROOF_BAND, FASCIA_RECT)
    canopy(
        mesh, frame, (0.0, 10.0), (9.0, 13.0), 3.5, 0.3,
        WALL_BAND, CAP_RECT, column_positions=[(1.0, 12.0), (9.0, 12.0)],
    )
    return mesh


def test_every_triangle_is_clockwise_front():
    mesh = build_sample_building()
    assert mesh.triangle_count > 50
    dots = geometric_normals_dot_stored(mesh)
    assert all(dot < 0.0 for dot in dots), (
        f"{sum(d >= 0 for d in dots)} of {len(dots)} triangles wound CCW-front"
    )


def test_stored_normals_are_unit_length():
    mesh = build_sample_building()
    for vertex in mesh.vertices:
        length = math.sqrt(sum(vertex[3 + i] ** 2 for i in range(3)))
        assert abs(length - 1.0) < 1e-6


def test_extruded_wall_normals_point_outward():
    mesh = Mesh()
    extrude_footprint(mesh, SQUARE, 0.0, 4.0, WALL_BAND)
    for vertex in mesh.vertices:
        px, _, pz, nx, ny, nz = vertex[:6]
        assert ny == 0.0
        assert px * nx + pz * nz > 0.0, "wall normal points inward"


def test_footprint_orientation_is_normalized():
    counter_clockwise = normalize_footprint(SQUARE)
    clockwise_input = list(reversed(SQUARE))
    assert normalize_footprint(clockwise_input) == counter_clockwise
    closed_input = SQUARE + [SQUARE[0]]
    assert normalize_footprint(closed_input) == counter_clockwise


def test_ear_clipping_covers_concave_polygon_exactly():
    ring, triangles = triangulate_footprint(L_SHAPE)
    assert len(triangles) == len(ring) - 2
    total = 0.0
    for ia, ib, ic in triangles:
        (xa, za), (xb, zb), (xc, zc) = ring[ia], ring[ib], ring[ic]
        total += abs((xb - xa) * (zc - za) - (xc - xa) * (zb - za)) / 2.0
    assert abs(total - footprint_area(L_SHAPE)) < 1e-6


def test_atlas_band_u_wraps_and_v_clamps():
    band = WALL_BAND
    assert band.u_for(16.0) == 2.0  # two full tiles; wrapping is GL's job
    assert band.v_for(-1.0) == band.v_for(0.0)
    assert band.v_for(99.0) == band.v_for(band.height_meters)
    assert band.v_bottom < band.v_for(0.0) < band.v_for(6.0) < band.v_top


def test_atlas_rect_pixel_rect_matches_uv_orientation():
    rect = AtlasRect("door", 0.25, 0.5, 0.5, 1.0)
    left, top, right, bottom = rect.pixel_rect(1024)
    assert (left, top, right, bottom) == (256, 0, 512, 512)


def test_frame_bearing_zero_is_north():
    frame = Frame()
    x, z = frame.to_world(10.0, 0.0)  # 10 m along at bearing 0 = north
    assert abs(x) < 1e-9 and abs(z + 10.0) < 1e-9
    x, z = frame.to_world(0.0, 10.0)  # across = right of north = east
    assert abs(x - 10.0) < 1e-9 and abs(z) < 1e-9


def test_writer_output_round_trips(tmp_path):
    mesh = build_sample_building()
    output = tmp_path / "sample.obj"
    write_obj8(mesh, output, texture_file_name="sample.png", comments=["test"])
    lines = output.read_text().splitlines()
    assert lines[0] == "I" and lines[1] == "800" and lines[2] == "OBJ"
    vertex_lines = [line for line in lines if line.startswith("VT ")]
    assert len(vertex_lines) == len(mesh.vertices)
    point_counts = next(line for line in lines if line.startswith("POINT_COUNTS"))
    assert point_counts.split() == [
        "POINT_COUNTS", str(len(mesh.vertices)), "0", "0", str(len(mesh.indices)),
    ]
    parsed_indices: list[int] = []
    for line in lines:
        if line.startswith("IDX10 "):
            parsed_indices.extend(int(v) for v in line.split()[1:])
        elif line.startswith("IDX "):
            parsed_indices.append(int(line.split()[1]))
    assert parsed_indices == mesh.indices
    tris = next(line for line in lines if line.startswith("TRIS"))
    assert tris == f"TRIS 0 {len(mesh.indices)}"
    assert "TEXTURE sample.png" in lines
