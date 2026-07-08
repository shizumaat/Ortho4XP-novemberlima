"""OBJ8 file reading and placement primitives for the DSF object integration.

Contract frozen by workstream W1 of ``docs/dsf_object_integration_spec.md``
(section 3.1, as amended by section 10 / A10).  Implemented in workstream
W2, ported from the verified prototype ``tools/obj8_geometry.py``.
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

import math
import os
from typing import Callable, Iterable, NamedTuple

# Metres per degree of latitude; longitude is scaled by cos(latitude).
METRES_PER_DEGREE_LATITUDE = 111320.0

# Two vertices closer than this (in every axis) are treated as the same
# point when welding triangle-soup seams.
VERTEX_WELD_DECIMALS = 3

# Per-keyword whitespace-token positions of the (x, y, z) coordinates on
# non-``VT`` positional commands, with the keyword itself at token 0.
#
# Derived from the OBJ8 file-format specification
# (https://developer.x-plane.com/article/obj8-file-format-specification/):
#
#     LIGHT_NAMED        <name> <x> <y> <z>
#     LIGHT_CUSTOM       <x> <y> <z> <r> <g> <b> <a> <s> <s1> <t1> <s2> <t2> <dataref>
#     LIGHT_PARAM        <name> <x> <y> <z> [<additional params>]
#     LIGHT_SPILL_CUSTOM <x> <y> <z> <r> <g> <b> <a> <s> <dx> <dy> <dz> <semi> <dataref>
#     VLIGHT             <x> <y> <z> <r> <g> <b>
#     smoke_black        <x> <y> <z> <s>
#     smoke_white        <x> <y> <z> <s>
#     EMITTER            <name> <x> <y> <z> <psi> <the> <phi> [index]
#     MAGNET             <name> <type> <x> <y> <z> <psi> <the> <phi>
#
# Each row was additionally confirmed against real objects on disk
# (2026-07-08), because guessing a column detaches a floodlight from its
# mast (plan section 8.6):
#
# * LIGHT_NAMED / LIGHT_PARAM — the Nimbus KCLT pack
#   (``Custom Scenery/Nimbus Simulation - KCLT V1.4 - Charlotte XP12``),
#   e.g. ``LIGHT_NAMED amb_street_light2 0.000000 9.536036 0.898227``
#   (tab separated in the wild) and
#   ``LIGHT_PARAM spot_params_sp 1.317350 7.200240 -18.993299 ...``.
# * LIGHT_CUSTOM / LIGHT_SPILL_CUSTOM / VLIGHT / EMITTER — X-Plane 12
#   default scenery and Laminar aircraft objects, e.g.
#   ``EMITTER fountain 0 40 0 0 0 0``.
# * MAGNET — ``Resources/default scenery/sim objects/vr/iPad.obj``:
#   ``MAGNET xPad1 xpad 0.0 0.0 -0.0061 0.0 0.0 0.0``.
# * SMOKE_BLACK / SMOKE_WHITE — specification only: no instance exists
#   anywhere in the local X-Plane 12 install (the command family is
#   legacy).  The specification spells them lowercase; both spellings are
#   accepted here because missing one would silently strand a smoke
#   puff's y coordinate.
POSITIONAL_COMMAND_COORDINATE_TOKEN_INDICES: dict[str, tuple[int, int, int]] = {
    "LIGHT_NAMED": (2, 3, 4),
    "LIGHT_CUSTOM": (1, 2, 3),
    "LIGHT_PARAM": (2, 3, 4),
    "LIGHT_SPILL_CUSTOM": (1, 2, 3),
    "VLIGHT": (1, 2, 3),
    "SMOKE_BLACK": (1, 2, 3),
    "smoke_black": (1, 2, 3),
    "SMOKE_WHITE": (1, 2, 3),
    "smoke_white": (1, 2, 3),
    "EMITTER": (2, 3, 4),
    "MAGNET": (3, 4, 5),
}


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
    guessed (workstream W2, item 1); see
    :data:`POSITIONAL_COMMAND_COORDINATE_TOKEN_INDICES`.
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
        solid_vertex_indices = {
            index for triangle in self.solid_triangles for index in triangle
        }
        draped_vertex_indices = {
            index for triangle in self.draped_triangles for index in triangle
        }
        return bool(solid_vertex_indices & draped_vertex_indices)

    def solid_reach_metres(self) -> float:
        """Greatest horizontal distance from the local origin to any vertex
        used by a solid (non-draped) triangle — the headline detector
        metric.  A compact, correctly anchored object has a reach of a few
        metres; the actionable KCLT set all exceed 25 m."""
        used = {
            index for triangle in self.solid_triangles for index in triangle
        }
        if not used:
            return 0.0
        return max(
            math.hypot(self.vertices[index][0], self.vertices[index][2])
            for index in used
        )


def load_object_file(path: str) -> ObjectGeometry:
    """Parse an OBJ8 file.

    Whitespace-tolerant (space- and tab-separated exports, invariant
    I-17); tracks ``ATTR_draped`` / ``ATTR_no_draped`` state across the
    ``TRIS`` commands; collects :class:`PositionalCommand` entries and
    counts ``ANIM_begin`` / ``ATTR_LOD``.
    """
    vertices: list[tuple[float, float, float]] = []
    vertex_line_indices: list[int] = []
    indices: list[int] = []
    triangle_ranges: list[tuple[int, int, bool]] = []
    positional_commands: list[PositionalCommand] = []
    animation_block_count = 0
    level_of_detail_count = 0
    currently_draped = False

    with open(path, errors="replace") as handle:
        for line_index, line in enumerate(handle):
            tokens = line.split()
            if not tokens:
                continue
            keyword = tokens[0]
            if keyword == "VT":
                vertices.append(
                    (float(tokens[1]), float(tokens[2]), float(tokens[3]))
                )
                vertex_line_indices.append(line_index)
            elif keyword.startswith("IDX"):
                indices.extend(int(token) for token in tokens[1:])
            elif keyword == "ATTR_draped":
                currently_draped = True
            elif keyword == "ATTR_no_draped":
                currently_draped = False
            elif keyword == "TRIS":
                triangle_ranges.append(
                    (int(tokens[1]), int(tokens[2]), currently_draped)
                )
            elif keyword == "ANIM_begin":
                animation_block_count += 1
            elif keyword == "ATTR_LOD":
                level_of_detail_count += 1
            elif keyword in POSITIONAL_COMMAND_COORDINATE_TOKEN_INDICES:
                x_token, y_token, z_token = (
                    POSITIONAL_COMMAND_COORDINATE_TOKEN_INDICES[keyword]
                )
                positional_commands.append(
                    PositionalCommand(
                        line_index=line_index,
                        keyword=keyword,
                        x=float(tokens[x_token]),
                        y=float(tokens[y_token]),
                        z=float(tokens[z_token]),
                        y_token_index=y_token,
                    )
                )

    solid: list[tuple[int, int, int]] = []
    draped: list[tuple[int, int, int]] = []
    index_count = len(indices)
    for offset, count, is_draped in triangle_ranges:
        target = draped if is_draped else solid
        for position in range(offset, min(offset + count, index_count - 2), 3):
            target.append(
                (
                    indices[position],
                    indices[position + 1],
                    indices[position + 2],
                )
            )
    return ObjectGeometry(
        vertices=vertices,
        solid_triangles=solid,
        draped_triangles=draped,
        positional_commands=positional_commands,
        animation_block_count=animation_block_count,
        level_of_detail_count=level_of_detail_count,
        vertex_line_indices=vertex_line_indices,
    )


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
    total_area = 0.0
    weighted_x = 0.0
    weighted_z = 0.0
    for first, second, third in triangles:
        a = vertices[first]
        b = vertices[second]
        c = vertices[third]
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        normal = (
            u[1] * v[2] - u[2] * v[1],
            u[2] * v[0] - u[0] * v[2],
            u[0] * v[1] - u[1] * v[0],
        )
        area = 0.5 * math.sqrt(
            normal[0] ** 2 + normal[1] ** 2 + normal[2] ** 2
        )
        total_area += area
        weighted_x += area * (a[0] + b[0] + c[0]) / 3.0
        weighted_z += area * (a[2] + b[2] + c[2]) / 3.0
    if total_area <= 0.0:
        return 0.0, 0.0, 0.0
    return total_area, weighted_x / total_area, weighted_z / total_area


def horizontal_bounding_box(
    vertices: list[tuple[float, float, float]],
    triangles: Iterable[tuple[int, int, int]],
) -> tuple[float, float, float, float]:
    """Return ``(min_x, max_x, min_z, max_z)`` over the triangles' vertices."""
    used = {index for triangle in triangles for index in triangle}
    x_values = [vertices[index][0] for index in used]
    z_values = [vertices[index][2] for index in used]
    return min(x_values), max(x_values), min(z_values), max(z_values)


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
    heading = math.radians(heading_degrees)
    sine, cosine = math.sin(heading), math.cos(heading)
    east = local_x * cosine - local_z * sine
    south = local_x * sine + local_z * cosine
    metres_per_degree_longitude = METRES_PER_DEGREE_LATITUDE * math.cos(
        math.radians(anchor_latitude)
    )
    return (
        anchor_latitude - south / METRES_PER_DEGREE_LATITUDE,
        anchor_longitude + east / metres_per_degree_longitude,
    )


def lonlat_to_local_offset(
    anchor_latitude: float,
    anchor_longitude: float,
    heading_degrees: float,
    latitude: float,
    longitude: float,
) -> tuple[float, float]:
    """Inverse of :func:`local_offset_to_lonlat`: map a world position into
    a placement's local ``(x, z)`` frame.  Round-trips to 1e-6 metres over
    random headings (workstream W2 acceptance).

    The forward rotation is ``east = x*cos - z*sin``, ``south = x*sin +
    z*cos`` (a rotation matrix), so the inverse is its transpose:
    ``x = east*cos + south*sin``, ``z = -east*sin + south*cos``.
    """
    heading = math.radians(heading_degrees)
    sine, cosine = math.sin(heading), math.cos(heading)
    metres_per_degree_longitude = METRES_PER_DEGREE_LATITUDE * math.cos(
        math.radians(anchor_latitude)
    )
    south = (anchor_latitude - latitude) * METRES_PER_DEGREE_LATITUDE
    east = (longitude - anchor_longitude) * metres_per_degree_longitude
    local_x = east * cosine + south * sine
    local_z = -east * sine + south * cosine
    return local_x, local_z


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
    definitions: list[str] = []
    placements: list[ObjectPlacement] = []
    for line in dsf_text_lines:
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "OBJECT_DEF":
            definitions.append(line.split(None, 1)[1].strip())
        elif tokens[0] == "OBJECT":
            index = int(tokens[1])
            if index >= len(definitions):
                continue
            resource = definitions[index]
            if accept_resource is not None and not accept_resource(resource):
                continue
            placements.append(
                ObjectPlacement(
                    definition_index=index,
                    resource_path=resource,
                    longitude=float(tokens[2]),
                    latitude=float(tokens[3]),
                    heading_degrees=float(tokens[4]),
                )
            )
    return placements


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
    if pack_root:
        candidate = os.path.join(pack_root, resource_path)
        if os.path.isfile(candidate):
            return candidate
    if xplane_root:
        from .agp_reader import resolve_library_path

        return resolve_library_path(resource_path, xplane_root)
    return None
