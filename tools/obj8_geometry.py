"""OBJ8 geometry and DSF object-placement primitives.

X-Plane renders a DSF ``OBJECT`` command by placing the object's mesh
rigidly at the terrain elevation sampled under its *anchor* — the
placement lon/lat, i.e. the object's local origin — never under the
geometry itself.  Scenery authors routinely bake many buildings into a
single ``.obj`` whose local origin sits hundreds of metres from any
actual geometry (or share one anchor across a whole family of objects).
On flat terrain this is harmless.  On real terrain every structure in
that object inherits the anchor's elevation and ends up floating above
or buried beneath the ground.

This module supplies the primitives needed to detect and correct that:

* :func:`load_object_file`                  parse OBJ8 VT/IDX/TRIS
* :func:`connected_components`              split the triangle soup
* :func:`group_components_into_structures`  merge nearby meshes
* :func:`area_weighted_centroid`            per-structure anchor point
* :func:`local_offset_to_lonlat`            apply the placement heading
* :func:`read_dsf_object_placements`        walk a DSFTool text dump
* :func:`resolve_object_resource`           pack-relative, then library.txt

Coordinate conventions (X-Plane OBJ8), before the placement heading::

    local +x = east      +y = up      +z = south

The heading rotates the object clockwise from north.  This matches the
transform already used by ``O4_Vector_Map.keep_obj8``::

    east  = x * cos(heading) - z * sin(heading)
    south = x * sin(heading) + z * cos(heading)

Gotchas encoded here — every one of these was observed in the wild in
the Nimbus KCLT pack, and each silently corrupts a naive implementation:

* ``VT`` / ``IDX`` lines may be TAB separated (XPlane2Blender exports).
  Testing ``line.startswith("VT ")`` drops such files entirely — 232 of
  334 object definitions, in the KCLT case.  Always split on whitespace.
* ``ATTR_draped`` triangles conform to the terrain mesh and are immune
  to the anchor problem.  They must be excluded, or a flat ground decal
  spanning a runway reports a large bogus elevation error.
* ``OBJECT_AGL`` / ``OBJECT_MSL`` carry an explicit elevation in a
  column plain ``OBJECT`` lacks, SHIFTING THE HEADING ONE COLUMN RIGHT.
  Verified against DSFTool text dumps on 2026-07-09 (EGLL, KBNA)::

      OBJECT     <def_index> <lon> <lat>              <heading>
      OBJECT_AGL <def_index> <lon> <lat> <agl_offset> <heading>
      OBJECT_MSL <def_index> <lon> <lat> <msl_elev>   <heading>

  Heading is token 4 for ``OBJECT`` but token 5 for the other two.  The
  AGL offset is SIGNED (negative = below grade); the MSL value is an
  absolute elevation above sea level.  Only plain ``OBJECT`` is
  terrain-draped; ``OBJECT_AGL`` is terrain-relative at its anchor.
* Objects declaring ``POINT_COUNTS 0 0 0 0`` are light-only and hold no
  geometry at all.
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from typing import Callable, Iterable, NamedTuple

# Metres per degree of latitude; longitude is scaled by cos(latitude).
METRES_PER_DEGREE_LATITUDE = 111320.0

# Two vertices closer than this (in every axis) are treated as the same
# point when welding triangle-soup seams before component extraction.
VERTEX_WELD_DECIMALS = 3


class ObjectPlacement(NamedTuple):
    """One object placement from a DSF text dump.

    Parity copy of ``auto_patch.obj8_reader.ObjectPlacement`` (the two
    modules share this reader; see that module for the full field
    contract).  Covers plain ``OBJECT``, ``OBJECT_AGL`` (amendment A18,
    signed ``above_ground_level_metres`` — negative = below grade) and,
    opt-in only, ``OBJECT_MSL`` (absolute ``mean_sea_level_elevation_m``).
    ``placement_kind`` records the source keyword.
    """

    definition_index: int
    resource_path: str
    longitude: float
    latitude: float
    heading_degrees: float
    above_ground_level_metres: float = 0.0
    placement_kind: str = "OBJECT"
    mean_sea_level_elevation_m: float | None = None


class ObjectGeometry(NamedTuple):
    """Parsed OBJ8 geometry, draped and solid triangles kept apart.

    ``solid_triangle_hardness`` is parallel to ``solid_triangles`` and
    records the collision state each triangle was emitted under —
    ``""`` (not hard), ``"hard"`` (``ATTR_hard``) or ``"hard_deck"``
    (``ATTR_hard_deck``, hard from above only, the drivable-deck marker
    used by taxiway bridges and tunnel decks).  Parity copy of the field
    on ``auto_patch.obj8_reader.ObjectGeometry`` (see that module for the
    full verified semantics); a tuple with an immutable ``()`` default so
    callers built before this field keep working.
    """

    vertices: list[tuple[float, float, float]]
    solid_triangles: list[tuple[int, int, int]]
    draped_triangles: list[tuple[int, int, int]]
    solid_triangle_hardness: tuple[str, ...] = ()

    @property
    def has_solid_geometry(self) -> bool:
        return bool(self.solid_triangles)

    def hard_deck_solid_triangles(self) -> list[tuple[int, int, int]]:
        """The subset of ``solid_triangles`` emitted under
        ``ATTR_hard_deck`` — the drivable deck.  Defensive against a
        hardness tuple shorter than ``solid_triangles``."""
        return [
            triangle
            for index, triangle in enumerate(self.solid_triangles)
            if index < len(self.solid_triangle_hardness)
            and self.solid_triangle_hardness[index] == "hard_deck"
        ]

    def solid_reach_metres(self) -> float:
        """Greatest horizontal distance from the local origin to any
        vertex used by a solid (non-draped) triangle.

        This is the headline detector metric: a compact object correctly
        anchored on its own geometry has a reach of a few metres.
        """
        used = {index for triangle in self.solid_triangles for index in triangle}
        if not used:
            return 0.0
        return max(
            math.hypot(self.vertices[index][0], self.vertices[index][2])
            for index in used
        )


def load_object_file(path: str) -> ObjectGeometry:
    """Parse an OBJ8 file into vertices plus solid/draped triangle lists.

    Whitespace-tolerant (handles both space- and tab-separated exports)
    and tracks ``ATTR_draped`` / ``ATTR_no_draped`` and — for the
    object-terrain-features feature — ``ATTR_hard`` / ``ATTR_hard_deck`` /
    ``ATTR_no_hard`` state across the ``TRIS`` commands that follow.
    """
    vertices: list[tuple[float, float, float]] = []
    indices: list[int] = []
    triangle_ranges: list[tuple[int, int, bool, str]] = []
    currently_draped = False
    currently_hard = ""  # "" | "hard" | "hard_deck"

    with open(path, errors="replace") as handle:
        for line in handle:
            tokens = line.split()
            if not tokens:
                continue
            keyword = tokens[0]
            if keyword == "VT":
                vertices.append(
                    (float(tokens[1]), float(tokens[2]), float(tokens[3]))
                )
            elif keyword.startswith("IDX"):
                indices.extend(int(token) for token in tokens[1:])
            elif keyword == "ATTR_draped":
                currently_draped = True
            elif keyword == "ATTR_no_draped":
                currently_draped = False
            elif keyword == "ATTR_hard_deck":
                currently_hard = "hard_deck"
            elif keyword == "ATTR_hard":
                currently_hard = "hard"
            elif keyword == "ATTR_no_hard":
                currently_hard = ""
            elif keyword == "TRIS":
                triangle_ranges.append(
                    (
                        int(tokens[1]),
                        int(tokens[2]),
                        currently_draped,
                        currently_hard,
                    )
                )

    solid: list[tuple[int, int, int]] = []
    draped: list[tuple[int, int, int]] = []
    solid_hardness: list[str] = []
    index_count = len(indices)
    for offset, count, is_draped, hardness in triangle_ranges:
        for position in range(offset, min(offset + count, index_count - 2), 3):
            triangle = (
                indices[position],
                indices[position + 1],
                indices[position + 2],
            )
            if is_draped:
                draped.append(triangle)
            else:
                solid.append(triangle)
                solid_hardness.append(hardness)
    return ObjectGeometry(vertices, solid, draped, tuple(solid_hardness))


def connected_components(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int]],
) -> list[list[tuple[int, int, int]]]:
    """Partition triangles into connected components.

    Vertices at the same position are welded first: exporters routinely
    duplicate a position once per texture seam or smoothing group, which
    would otherwise shatter a single wall into dozens of components.
    """
    parent = list(range(len(vertices)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[left_root] = right_root

    position_to_vertex: dict[tuple[float, float, float], int] = {}
    for triangle in triangles:
        for index in triangle:
            vertex = vertices[index]
            key = (
                round(vertex[0], VERTEX_WELD_DECIMALS),
                round(vertex[1], VERTEX_WELD_DECIMALS),
                round(vertex[2], VERTEX_WELD_DECIMALS),
            )
            if key in position_to_vertex:
                union(index, position_to_vertex[key])
            else:
                position_to_vertex[key] = index

    for first, second, third in triangles:
        union(first, second)
        union(second, third)

    grouped: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for triangle in triangles:
        grouped[find(triangle[0])].append(triangle)
    return list(grouped.values())


def horizontal_bounding_box(
    vertices: list[tuple[float, float, float]],
    triangles: Iterable[tuple[int, int, int]],
) -> tuple[float, float, float, float]:
    """Return ``(min_x, max_x, min_z, max_z)`` over the triangles' vertices."""
    used = {index for triangle in triangles for index in triangle}
    x_values = [vertices[index][0] for index in used]
    z_values = [vertices[index][2] for index in used]
    return min(x_values), max(x_values), min(z_values), max(z_values)


def _bounding_box_gap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    """Shortest distance between two axis-aligned boxes (0 if they touch)."""
    a_min_x, a_max_x, a_min_z, a_max_z = first
    b_min_x, b_max_x, b_min_z, b_max_z = second
    delta_x = max(0.0, max(b_min_x - a_max_x, a_min_x - b_max_x))
    delta_z = max(0.0, max(b_min_z - a_max_z, a_min_z - b_max_z))
    return math.hypot(delta_x, delta_z)


def group_components_into_structures(
    vertices: list[tuple[float, float, float]],
    components: list[list[tuple[int, int, int]]],
    gap_metres: float = 20.0,
    grid_cell_metres: float = 60.0,
) -> list[list[tuple[int, int, int]]]:
    """Single-link merge of components whose bounding boxes lie within
    ``gap_metres`` of each other — i.e. group meshes into buildings.

    A uniform grid keeps this near-linear; the naive all-pairs form is
    O(n^2) and chokes on the 7000-component wall objects at KCLT.

    ``gap_metres`` is a heuristic, not a constant: raising it merges
    adjacent buildings, lowering it separates a roof from its walls.
    """
    box_by_component = [
        horizontal_bounding_box(vertices, component) for component in components
    ]
    parent = list(range(len(components)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    for component_index, box in enumerate(box_by_component):
        min_x, max_x, min_z, max_z = box
        first_cell_x = int((min_x - gap_metres) // grid_cell_metres)
        last_cell_x = int((max_x + gap_metres) // grid_cell_metres)
        first_cell_z = int((min_z - gap_metres) // grid_cell_metres)
        last_cell_z = int((max_z + gap_metres) // grid_cell_metres)
        for cell_x in range(first_cell_x, last_cell_x + 1):
            for cell_z in range(first_cell_z, last_cell_z + 1):
                buckets[(cell_x, cell_z)].append(component_index)

    for bucket in buckets.values():
        for position, left in enumerate(bucket):
            for right in bucket[position + 1 :]:
                if (
                    _bounding_box_gap(
                        box_by_component[left], box_by_component[right]
                    )
                    < gap_metres
                ):
                    left_root, right_root = find(left), find(right)
                    if left_root != right_root:
                        parent[left_root] = right_root

    grouped: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
    for component_index, component in enumerate(components):
        grouped[find(component_index)].extend(component)
    return list(grouped.values())


def area_weighted_centroid(
    vertices: list[tuple[float, float, float]],
    triangles: Iterable[tuple[int, int, int]],
) -> tuple[float, float, float]:
    """Return ``(surface_area, centroid_x, centroid_z)``.

    Weighting by 3D triangle area — rather than averaging vertices —
    keeps densely tessellated detail (railings, rooftop clutter) from
    dragging the anchor away from the building's bulk.
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


def local_offset_to_lonlat(
    anchor_latitude: float,
    anchor_longitude: float,
    heading_degrees: float,
    local_x: float,
    local_z: float,
) -> tuple[float, float]:
    """Project an OBJ8 local ``(x, z)`` offset to ``(latitude, longitude)``.

    Mirrors ``O4_Vector_Map.keep_obj8``.  Verified against the KCLT
    ``Charlotte_Airport_007_ALB.obj`` placement: the opposite rotation
    sign puts the geometry a kilometre away.
    """
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


def read_dsf_object_placements(
    dsf_text_lines: Iterable[str],
    accept_resource: Callable[[str], bool] | None = None,
    include_object_msl: bool = False,
) -> list[ObjectPlacement]:
    """Collect ``OBJECT`` placements from a DSF text dump.

    Parity copy of ``auto_patch.obj8_reader.read_dsf_object_placements``.
    ``OBJECT`` and ``OBJECT_AGL`` (amendment A18) rows are always
    collected; ``OBJECT_AGL`` puts its signed offset in
    ``above_ground_level_metres`` and its heading in the fifth column.
    ``OBJECT_MSL`` rows are skipped unless ``include_object_msl=True``,
    in which case they return with ``placement_kind == "OBJECT_MSL"`` and
    their absolute elevation in ``mean_sea_level_elevation_m``.
    """
    definitions: list[str] = []
    placements: list[ObjectPlacement] = []
    for line in dsf_text_lines:
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "OBJECT_DEF":
            definitions.append(line.split(None, 1)[1].strip())
        elif tokens[0] in ("OBJECT", "OBJECT_AGL", "OBJECT_MSL"):
            keyword = tokens[0]
            if keyword == "OBJECT_MSL" and not include_object_msl:
                continue
            index = int(tokens[1])
            if index >= len(definitions):
                continue
            resource = definitions[index]
            if accept_resource is not None and not accept_resource(resource):
                continue
            # OBJECT_AGL / OBJECT_MSL have an elevation column that plain
            # OBJECT lacks, so the heading is token 5 there, token 4 here.
            has_elevation_column = keyword in ("OBJECT_AGL", "OBJECT_MSL")
            heading_degrees = float(
                tokens[5] if has_elevation_column else tokens[4]
            )
            above_ground_level_metres = (
                float(tokens[4]) if keyword == "OBJECT_AGL" else 0.0
            )
            mean_sea_level_elevation_m = (
                float(tokens[4]) if keyword == "OBJECT_MSL" else None
            )
            placements.append(
                ObjectPlacement(
                    definition_index=index,
                    resource_path=resource,
                    longitude=float(tokens[2]),
                    latitude=float(tokens[3]),
                    heading_degrees=heading_degrees,
                    above_ground_level_metres=above_ground_level_metres,
                    placement_kind=keyword,
                    mean_sea_level_elevation_m=mean_sea_level_elevation_m,
                )
            )
    return placements


def resolve_object_resource(
    resource_path: str,
    pack_root: str | None,
    xplane_root: str | None,
) -> str | None:
    """Map a DSF resource string to a file on disk.

    A path relative to the scenery pack wins over ``library.txt``, which
    is how X-Plane itself resolves it.  ``auto_patch.agp_reader``'s
    ``resolve_library_path`` only consults ``library.txt``, so a
    pack-local resource such as ``Terminals/Hangar/Foo.obj`` resolves to
    nothing without the pack-relative probe added here.
    """
    if pack_root:
        candidate = os.path.join(pack_root, resource_path)
        if os.path.isfile(candidate):
            return candidate
    if xplane_root:
        from auto_patch.agp_reader import resolve_library_path

        return resolve_library_path(resource_path, xplane_root)
    return None
