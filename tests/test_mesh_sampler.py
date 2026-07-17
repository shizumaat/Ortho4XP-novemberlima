"""Tests for ``auto_patch.mesh_sampler`` (workstream W3).

Fixture: ``tests/fixtures/mesh/synthetic_fan_three_triangles.mesh``, a
hand-written mesh in the exact ``O4_Mesh_Utils.write_mesh_file`` format
(``MeshVersionFormatted`` header, ``Normals`` section, 1-based triangle
indices, elevation column divided by 100000).  Five vertices on a
0.001-degree square with a centre vertex, fanned into THREE triangles —
the top quadrant of the square is deliberately left empty so a query
there falls inside retained-triangle bounding boxes yet outside every
triangle, exercising the invariant-I-13 path (raise, never fall back to
the nearest vertex).

Fixture geometry (longitude, latitude, elevation in metres)::

    vertex 1: (10.0000, 50.0000, 100.0)
    vertex 2: (10.0010, 50.0000, 200.0)
    vertex 3: (10.0010, 50.0010, 300.0)
    vertex 4: (10.0000, 50.0010, 400.0)
    vertex 5: (10.0005, 50.0005, 250.0)   # centre

    triangle (1, 2, 5)  bottom
    triangle (2, 3, 5)  right
    triangle (1, 5, 4)  left
    (top triangle (4, 5, 3) deliberately absent)
"""

from __future__ import annotations

import os
import random

import numpy
import pytest

from auto_patch.mesh_sampler import MeshElevationSampler, OutsideMeshError

FIXTURE_MESH_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "fixtures",
    "mesh",
    "synthetic_fan_three_triangles.mesh",
)

FIXTURE_BOUNDS = (10.0, 50.0, 10.001, 50.001)

# (longitude, latitude, elevation_metres) — must match the fixture file.
FIXTURE_VERTICES = [
    (10.0000, 50.0000, 100.0),
    (10.0010, 50.0000, 200.0),
    (10.0010, 50.0010, 300.0),
    (10.0000, 50.0010, 400.0),
    (10.0005, 50.0005, 250.0),
]

# 0-based into FIXTURE_VERTICES (the file itself is 1-based).
FIXTURE_TRIANGLES = [(0, 1, 4), (1, 2, 4), (0, 4, 3)]


@pytest.fixture(scope="module")
def fixture_sampler() -> MeshElevationSampler:
    return MeshElevationSampler(FIXTURE_MESH_PATH, FIXTURE_BOUNDS)


def test_every_fixture_vertex_returns_its_own_elevation(fixture_sampler):
    for longitude, latitude, elevation_metres in FIXTURE_VERTICES:
        assert (
            abs(
                fixture_sampler.elevation_at(latitude, longitude)
                - elevation_metres
            )
            < 1e-6
        )


def test_edge_midpoints_fall_within_their_endpoint_bracket(fixture_sampler):
    for triangle in FIXTURE_TRIANGLES:
        for first_corner, second_corner in (
            (triangle[0], triangle[1]),
            (triangle[1], triangle[2]),
            (triangle[2], triangle[0]),
        ):
            first_longitude, first_latitude, first_elevation = (
                FIXTURE_VERTICES[first_corner]
            )
            second_longitude, second_latitude, second_elevation = (
                FIXTURE_VERTICES[second_corner]
            )
            midpoint_elevation = fixture_sampler.elevation_at(
                (first_latitude + second_latitude) / 2.0,
                (first_longitude + second_longitude) / 2.0,
            )
            assert (
                min(first_elevation, second_elevation)
                <= midpoint_elevation
                <= max(first_elevation, second_elevation)
            )
            # Barycentric interpolation is linear along an edge, so the
            # midpoint is exactly the endpoint average.
            assert (
                abs(
                    midpoint_elevation
                    - (first_elevation + second_elevation) / 2.0
                )
                < 1e-6
            )


def test_barycentric_interior_query_matches_hand_computed_plane(
    fixture_sampler,
):
    # Point with barycentric weights (0.5, 0.25, 0.25) in the bottom
    # triangle (vertices 1, 2, 5):
    #   longitude = 0.5*10.0 + 0.25*10.001  + 0.25*10.0005 = 10.000375
    #   latitude  = 0.5*50.0 + 0.25*50.0    + 0.25*50.0005 = 50.000125
    #   elevation = 0.5*100  + 0.25*200     + 0.25*250     = 162.5
    assert (
        abs(fixture_sampler.elevation_at(50.000125, 10.000375) - 162.5)
        < 1e-6
    )


def test_point_outside_every_triangle_raises_outside_mesh_error(
    fixture_sampler,
):
    """Regression test against the prototype's deleted nearest-vertex
    fallback (invariant I-13): a point off the retained mesh must raise,
    never silently return the nearest vertex's plausible elevation."""
    # Far outside the whole fixture — fails the bounding-box prefilter.
    with pytest.raises(OutsideMeshError):
        fixture_sampler.elevation_at(50.05, 10.05)
    # Inside the square's empty top quadrant: this point lies inside the
    # RIGHT triangle's bounding box (candidates are found and scanned)
    # but outside every retained triangle.  The old fallback would have
    # returned vertex 4's or 5's elevation here.
    with pytest.raises(OutsideMeshError):
        fixture_sampler.elevation_at(50.0009, 10.0007)


def test_elevation_at_or_none_returns_none_outside_the_mesh(fixture_sampler):
    assert fixture_sampler.elevation_at_or_none(50.05, 10.05) is None
    assert fixture_sampler.elevation_at_or_none(50.0009, 10.0007) is None
    # And still returns a value where elevation_at does.
    assert (
        abs(fixture_sampler.elevation_at_or_none(50.0, 10.0) - 100.0) < 1e-6
    )


def test_bounds_excluding_every_triangle_raise_a_clear_error():
    with pytest.raises(ValueError, match="no mesh triangles"):
        MeshElevationSampler(FIXTURE_MESH_PATH, (20.0, 60.0, 21.0, 61.0))


# ---------------------------------------------------------------------------
# Optional smoke test against a real built tile (skipped when absent)
# ---------------------------------------------------------------------------

REAL_MESH_PATH = (
    "/Users/noah/X-Plane 12/Custom Scenery/zOrtho4XP_+35-081/"
    "Data+35-081.mesh"
)

KCLT_ANCHOR_LATITUDE = 35.207360571
KCLT_ANCHOR_LONGITUDE = -80.935041390


@pytest.mark.skipif(
    not os.path.exists(REAL_MESH_PATH),
    reason=f"real built mesh not present: {REAL_MESH_PATH}",
)
def test_real_mesh_vertex_queries_are_self_consistent():
    # Tight bounds around the KCLT anchor — the file is 96 MB and the
    # retained-triangle subset is what keeps queries cheap.
    bounds = (
        KCLT_ANCHOR_LONGITUDE - 0.005,
        KCLT_ANCHOR_LATITUDE - 0.005,
        KCLT_ANCHOR_LONGITUDE + 0.005,
        KCLT_ANCHOR_LATITUDE + 0.005,
    )
    sampler = MeshElevationSampler(REAL_MESH_PATH, bounds)
    retained_vertex_indices = numpy.unique(sampler._triangles)
    generator = random.Random(20260708)
    sampled_indices = generator.sample(
        list(retained_vertex_indices),
        min(100, len(retained_vertex_indices)),
    )
    for vertex_index in sampled_indices:
        longitude, latitude, elevation_metres = sampler._vertices[
            vertex_index
        ]
        assert (
            abs(sampler.elevation_at(latitude, longitude) - elevation_metres)
            < 1e-6
        )


# ── parse cache (one full-mesh text parse per (path, mtime, size)) ───


def test_parse_cache_shares_one_parse_across_constructions(monkeypatch):
    """Phase 2 constructs a sampler per object pool against the same
    tile mesh (~80 constructions at KBNA, 2026-07-15 profile); the
    module-level parse cache must serve every construction after the
    first from one parse, and samplers must behave identically."""
    from auto_patch import mesh_sampler as sampler_module

    monkeypatch.setattr(sampler_module, "_parse_cache_key", None)
    monkeypatch.setattr(sampler_module, "_parse_cache_arrays", None)
    parse_calls = []
    real_read_mesh = MeshElevationSampler._read_mesh

    def counting_read_mesh(mesh_path):
        parse_calls.append(mesh_path)
        return real_read_mesh(mesh_path)

    monkeypatch.setattr(
        MeshElevationSampler, "_read_mesh",
        staticmethod(counting_read_mesh))

    first = MeshElevationSampler(FIXTURE_MESH_PATH, FIXTURE_BOUNDS)
    second = MeshElevationSampler(FIXTURE_MESH_PATH, FIXTURE_BOUNDS)
    assert len(parse_calls) == 1
    for longitude, latitude, elevation_metres in FIXTURE_VERTICES:
        assert (
            first.elevation_at(latitude, longitude)
            == second.elevation_at(latitude, longitude)
        )


def test_parse_cache_invalidates_when_the_mesh_file_changes(
        tmp_path, monkeypatch):
    from auto_patch import mesh_sampler as sampler_module

    monkeypatch.setattr(sampler_module, "_parse_cache_key", None)
    monkeypatch.setattr(sampler_module, "_parse_cache_arrays", None)
    mesh_path = tmp_path / "Data+00+000.mesh"
    with open(FIXTURE_MESH_PATH) as handle:
        original = handle.read()
    mesh_path.write_text(original)

    parse_calls = []
    real_read_mesh = MeshElevationSampler._read_mesh

    def counting_read_mesh(path):
        parse_calls.append(path)
        return real_read_mesh(path)

    monkeypatch.setattr(
        MeshElevationSampler, "_read_mesh",
        staticmethod(counting_read_mesh))

    MeshElevationSampler(str(mesh_path), FIXTURE_BOUNDS)
    assert len(parse_calls) == 1

    # Rewrite with different bytes (a rebuilt mesh): size changes, so
    # the (path, mtime_ns, size) key misses even on coarse filesystems.
    mesh_path.write_text(original + "\n")
    MeshElevationSampler(str(mesh_path), FIXTURE_BOUNDS)
    assert len(parse_calls) == 2
