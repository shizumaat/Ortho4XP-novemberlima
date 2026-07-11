"""Sample elevations from a built Ortho4XP ``Data<tile>.mesh``.

This is the terrain the simulator actually renders, *after* auto_patch's
runway/apron/taxiway grading.  Sampling the source DEM instead would
report the pre-grading surface and mislead by several metres anywhere
the pavement builder has moved the ground.

Mesh format, as written by ``O4_Mesh_Utils.write_mesh_file``::

    MeshVersionFormatted 2
    Dimension 3

    Vertices
    <count>
    <longitude> <latitude> <elevation/100000> <tag>
    ...
    Normals
    <count>
    ...
    Triangles
    <count>
    <vertex_a> <vertex_b> <vertex_c> <tag>

Note the ``/ 100000`` scaling on the elevation column, and that the
triangle vertex indices are 1-based (they come straight from Triangle's
``.ele`` output).
"""

from __future__ import annotations

import math

import numpy

# O4_Mesh_Utils writes elevation divided by this constant.
MESH_ELEVATION_SCALE = 100000.0


class MeshElevationSampler:
    """Point-in-triangle elevation lookup over a built Ortho4XP mesh.

    Only triangles overlapping ``bounds`` are retained, which keeps a
    3-million-triangle tile down to something a probe can hold and scan.
    """

    def __init__(
        self,
        mesh_path: str,
        bounds: tuple[float, float, float, float],
        margin_degrees: float = 0.002,
    ) -> None:
        """``bounds`` is ``(min_lon, min_lat, max_lon, max_lat)``."""
        min_lon, min_lat, max_lon, max_lat = bounds
        min_lon -= margin_degrees
        min_lat -= margin_degrees
        max_lon += margin_degrees
        max_lat += margin_degrees

        vertices, triangles = self._read_mesh(mesh_path)

        inside = (
            (vertices[:, 0] >= min_lon)
            & (vertices[:, 0] <= max_lon)
            & (vertices[:, 1] >= min_lat)
            & (vertices[:, 1] <= max_lat)
        )
        keep = inside[triangles].any(axis=1)
        self._triangles = triangles[keep]
        if not len(self._triangles):
            raise ValueError(
                f"no mesh triangles inside {bounds} — wrong tile?"
            )
        self._vertices = vertices

        corners = vertices[self._triangles]
        self._corner_a = corners[:, 0, :]
        self._corner_b = corners[:, 1, :]
        self._corner_c = corners[:, 2, :]
        self._triangle_min = corners[:, :, :2].min(axis=1)
        self._triangle_max = corners[:, :, :2].max(axis=1)

    @staticmethod
    def _read_mesh(
        mesh_path: str,
    ) -> tuple[numpy.ndarray, numpy.ndarray]:
        with open(mesh_path) as handle:
            while True:
                line = handle.readline()
                if not line:
                    raise ValueError(f"{mesh_path}: no Vertices section")
                if line.strip() == "Vertices":
                    break
            vertex_count = int(handle.readline())
            block = [handle.readline() for _ in range(vertex_count)]
            vertices = numpy.array(
                " ".join(block).split(), dtype=numpy.float64
            ).reshape(vertex_count, 4)[:, :3]
            vertices[:, 2] *= MESH_ELEVATION_SCALE

            while True:
                line = handle.readline()
                if not line:
                    raise ValueError(f"{mesh_path}: no Triangles section")
                if line.strip() == "Triangles":
                    break
            triangle_count = int(handle.readline())
            block = [handle.readline() for _ in range(triangle_count)]
            triangles = numpy.array(
                " ".join(block).split(), dtype=numpy.int64
            ).reshape(triangle_count, -1)[:, :3]

        # Triangle emits 1-based indices; normalise and verify.
        if triangles.min() >= 1:
            triangles = triangles - 1
        if triangles.max() >= len(vertices):
            raise ValueError(f"{mesh_path}: triangle index out of range")
        return vertices, triangles

    def elevation_at(self, latitude: float, longitude: float) -> float:
        """Barycentric-interpolated elevation, in metres.

        Falls back to the nearest mesh vertex when the point lands
        outside every retained triangle (which happens only outside the
        requested bounds).
        """
        candidates = numpy.nonzero(
            (self._triangle_min[:, 0] <= longitude)
            & (self._triangle_max[:, 0] >= longitude)
            & (self._triangle_min[:, 1] <= latitude)
            & (self._triangle_max[:, 1] >= latitude)
        )[0]

        for index in candidates:
            a = self._corner_a[index]
            b = self._corner_b[index]
            c = self._corner_c[index]
            denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (
                a[1] - c[1]
            )
            if abs(denominator) < 1e-18:
                continue
            weight_a = (
                (b[1] - c[1]) * (longitude - c[0])
                + (c[0] - b[0]) * (latitude - c[1])
            ) / denominator
            weight_b = (
                (c[1] - a[1]) * (longitude - c[0])
                + (a[0] - c[0]) * (latitude - c[1])
            ) / denominator
            weight_c = 1.0 - weight_a - weight_b
            if weight_a >= -1e-9 and weight_b >= -1e-9 and weight_c >= -1e-9:
                return float(
                    weight_a * a[2] + weight_b * b[2] + weight_c * c[2]
                )

        distances = numpy.hypot(
            self._vertices[:, 0] - longitude, self._vertices[:, 1] - latitude
        )
        return float(self._vertices[int(distances.argmin()), 2])
