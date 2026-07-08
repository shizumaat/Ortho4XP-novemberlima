"""Elevation sampling over a built Ortho4XP ``Data<tile>.mesh``.

Contract frozen by workstream W1 (``docs/dsf_object_integration_spec.md``
section 3.2); implementation lands in workstream W3, ported from the
verified prototype ``tools/mesh_elevation_sampler.py`` with one
deliberate correctness change: **the nearest-vertex fallback is deleted,
not guarded**.  The prototype silently returned a plausible elevation for
a point outside every retained triangle — a structure that has walked off
the tile — and a plausible number is exactly what a caller must not get.
Callers skip-and-report instead (invariant I-13).

Use the mesh, not the DEM: the mesh is the terrain after auto_patch's
grading.  At the KCLT anchor the mesh reads 219.83 m where the source DEM
reads 218.95 m.

Mesh format, as written by ``O4_Mesh_Utils.write_mesh_file``::

    MeshVersionFormatted 2
    Dimension 3

    Vertices
    <count>
    <longitude> <latitude> <elevation/100000> 0
    ...
    Normals
    <count>
    ...
    Triangles
    <count>
    <vertex_a> <vertex_b> <vertex_c> <terrain_type>

Note the ``/ 100000`` scaling on the elevation column.  The vertex line's
4th field is a hardcoded literal ``0`` (never read back) — NOT a tag; only
the TRIANGLE line's 4th field is a real terrain-type attribute.  Triangle
vertex indices are 1-based (straight from Triangle's ``.ele`` output;
confirmed at ``O4_Mesh_Utils.py`` read side, which subtracts 1).
"""

from __future__ import annotations


class OutsideMeshError(Exception):
    """Raised when a query point lies outside every retained triangle.

    Deliberately loud: the prototype's silent nearest-vertex fallback is
    the failure mode this class exists to kill (invariant I-13).
    """


class MeshElevationSampler:
    """Barycentric point-in-triangle elevation lookup over a built mesh.

    Only triangles overlapping ``bounds`` are retained, which keeps a
    3-million-triangle tile down to something one airport's queries can
    scan.  ``bounds`` is ``(min_lon, min_lat, max_lon, max_lat)``.
    """

    def __init__(
        self,
        mesh_path: str,
        bounds: tuple[float, float, float, float],
        margin_degrees: float = 0.002,
    ) -> None:
        raise NotImplementedError("workstream W3")

    def elevation_at(self, latitude: float, longitude: float) -> float:
        """Barycentric-interpolated elevation in metres.

        Raises :class:`OutsideMeshError` outside every retained triangle.
        There is NO nearest-vertex fallback (invariant I-13).
        """
        raise NotImplementedError("workstream W3")

    def elevation_at_or_none(
        self, latitude: float, longitude: float
    ) -> float | None:
        """As :meth:`elevation_at`, returning ``None`` instead of raising."""
        raise NotImplementedError("workstream W3")
