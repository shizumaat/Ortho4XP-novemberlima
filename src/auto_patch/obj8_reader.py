"""OBJ8 file reading and placement primitives for the DSF object integration.

Contract frozen by workstream W1 of ``docs/dsf_object_integration_spec.md``
(section 3.1, as amended by section 10 / A10).  Implementations land in
workstream W2, ported from the verified prototype ``tools/obj8_geometry.py``;
until then every function body raises ``NotImplementedError``.
``tests/test_contracts.py`` asserts these signatures — change them only by
amending the spec first.

Coordinate conventions (X-Plane OBJ8), before the placement heading::

    local +x = east      +y = up      +z = south

The heading rotates the object clockwise from north, matching
``O4_Vector_Map.keep_obj8``::

    east  = x * cos(heading) - z * sin(heading)
    south = x * sin(heading) + z * cos(heading)

Verify the sign against the golden fixture before trusting new code: the
KCLT ``Charlotte_Airport_007_ALB.obj`` hangar lands at ``35.216591,
-80.929272``; the opposite sign puts it 1021 metres away.

Gotchas the implementation must preserve (every one observed in the wild,
each silently corrupts a naive implementation — plan section 8.8):

* ``VT`` / ``IDX`` lines may be TAB separated (XPlane2Blender exports).
  ``line.startswith("VT ")`` dropped 232 of 334 definitions at KCLT.
  Always split on whitespace (invariant I-17).
* ``ATTR_draped`` triangles conform to the terrain mesh and are immune to
  the anchor problem; they are kept apart from solid triangles (I-9).
* ``OBJECT_MSL`` / ``OBJECT_AGL`` carry an explicit elevation and shift
  the heading column; only a plain ``OBJECT`` is terrain-draped.
* Objects declaring ``POINT_COUNTS 0 0 0 0`` are light-only.
* ``LIGHT_*`` / ``VLIGHT`` / ``SMOKE_*`` / ``EMITTER`` / ``MAGNET`` carry
  their own y coordinates and must move with their structure (I-10) —
  hence :class:`PositionalCommand`.
"""

from __future__ import annotations

from typing import Callable, Iterable, NamedTuple

# Metres per degree of latitude; longitude is scaled by cos(latitude).
METRES_PER_DEGREE_LATITUDE = 111320.0

# Two vertices closer than this (in every axis) are treated as the same
# point when welding triangle-soup seams.
VERTEX_WELD_DECIMALS = 3


class ObjectPlacement(NamedTuple):
    """One terrain-draped ``OBJECT`` command from a DSF text dump."""

    definition_index: int
    resource_path: str
    longitude: float
    latitude: float
    heading_degrees: float


class PositionalCommand(NamedTuple):
    """A non-``VT`` OBJ8 command carrying its own ``(x, y, z)`` position —
    ``LIGHT_*``, ``VLIGHT``, ``SMOKE_*``, ``EMITTER``, ``MAGNET``.

    ``line_index`` addresses the source file (0-based); ``y_token_index``
    is the whitespace-token position of the y value on that line, so the
    rebake writer can replace exactly one token without re-parsing
    (invariant I-16).  The per-keyword column table is derived from the
    OBJ8 specification and verified against objects on disk — never
    guessed (workstream W2, item 1).
    """

    line_index: int
    keyword: str
    x: float
    y: float
    z: float
    y_token_index: int


class ObjectGeometry(NamedTuple):
    """Parsed OBJ8 geometry, draped and solid triangles kept apart.

    ``vertex_line_indices`` is parallel to ``vertices`` and gives each
    ``VT`` line's 0-based index in the source file, so the rebake writer
    can rewrite the y token in place (invariant I-16).
    """

    vertices: list[tuple[float, float, float]]
    solid_triangles: list[tuple[int, int, int]]
    draped_triangles: list[tuple[int, int, int]]
    positional_commands: list[PositionalCommand]
    animation_block_count: int
    level_of_detail_count: int
    vertex_line_indices: list[int]

    @property
    def has_solid_geometry(self) -> bool:
        return bool(self.solid_triangles)

    @property
    def has_mixed_draped_solid_vertices(self) -> bool:
        """True when any vertex index is used by both a draped and a solid
        triangle.  Such an object cannot be corrected: offsetting the solid
        use would tear the draped use off the terrain (invariant I-9) —
        refuse and report."""
        raise NotImplementedError("workstream W2")

    def solid_reach_metres(self) -> float:
        """Greatest horizontal distance from the local origin to any vertex
        used by a solid (non-draped) triangle — the headline detector
        metric.  A compact, correctly anchored object has a reach of a few
        metres; the actionable KCLT set all exceed 25 m."""
        raise NotImplementedError("workstream W2")


def load_object_file(path: str) -> ObjectGeometry:
    """Parse an OBJ8 file.

    Whitespace-tolerant (space- and tab-separated exports, invariant
    I-17); tracks ``ATTR_draped`` / ``ATTR_no_draped`` state across the
    ``TRIS`` commands; collects :class:`PositionalCommand` entries and
    counts ``ANIM_begin`` / ``ATTR_LOD``.
    """
    raise NotImplementedError("workstream W2")


def area_weighted_centroid(
    vertices: list[tuple[float, float, float]],
    triangles: Iterable[tuple[int, int, int]],
) -> tuple[float, float, float]:
    """Return ``(surface_area, centroid_x, centroid_z)``.

    Weighting by 3D triangle area — rather than averaging vertices —
    keeps densely tessellated detail (railings, rooftop clutter) from
    dragging the centroid away from the building's bulk.  With
    ``ATTR_LOD`` copies present, compute from the first level-of-detail
    bucket only (invariant I-12).
    """
    raise NotImplementedError("workstream W2")


def horizontal_bounding_box(
    vertices: list[tuple[float, float, float]],
    triangles: Iterable[tuple[int, int, int]],
) -> tuple[float, float, float, float]:
    """Return ``(min_x, max_x, min_z, max_z)`` over the triangles' vertices."""
    raise NotImplementedError("workstream W2")


def local_offset_to_lonlat(
    anchor_latitude: float,
    anchor_longitude: float,
    heading_degrees: float,
    local_x: float,
    local_z: float,
) -> tuple[float, float]:
    """Project an OBJ8 local ``(x, z)`` offset to ``(latitude, longitude)``
    through a placement.  Mirrors ``O4_Vector_Map.keep_obj8``; verified
    against the golden hangar fixture (the wrong rotation sign is 1021
    metres out)."""
    raise NotImplementedError("workstream W2")


def lonlat_to_local_offset(
    anchor_latitude: float,
    anchor_longitude: float,
    heading_degrees: float,
    latitude: float,
    longitude: float,
) -> tuple[float, float]:
    """Inverse of :func:`local_offset_to_lonlat`: map a world position into
    a placement's local ``(x, z)`` frame.  Round-trips to 1e-6 metres over
    random headings (workstream W2 acceptance)."""
    raise NotImplementedError("workstream W2")


def read_dsf_object_placements(
    dsf_text_lines: Iterable[str],
    accept_resource: Callable[[str], bool] | None = None,
) -> list[ObjectPlacement]:
    """Collect terrain-draped ``OBJECT`` placements from a DSF text dump.

    ``OBJECT_MSL`` and ``OBJECT_AGL`` are deliberately skipped: they carry
    an explicit elevation and are not subject to the distant-anchor
    problem.  Takes lines, not a path, so tests feed synthetic text
    (harness pattern (a), ``tests/test_agp_reader.py``).
    """
    raise NotImplementedError("workstream W2")


def resolve_object_resource(
    resource_path: str,
    pack_root: str | None,
    xplane_root: str | None,
) -> str | None:
    """Map a DSF resource string to a file on disk.

    A path relative to the scenery pack wins over ``library.txt`` — the
    resolution order X-Plane itself uses.  ``agp_reader.resolve_library_path``
    alone cannot see pack-local resources such as
    ``Terminals/Hangar/Charlotte_Airport_007_ALB.obj``.
    """
    raise NotImplementedError("workstream W2")
